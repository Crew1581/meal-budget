"""Локальная подготовка сценариев; провайдер не вызывается."""

from decimal import Decimal

from pydantic import TypeAdapter

from meal_budget.core.config import PROJECT_ROOT
from meal_budget.schemas.evaluation import EvaluationScenario
from meal_budget.schemas.generation import GenerationRequest
from meal_budget.schemas.products import ProductCatalog
from meal_budget.services.deepseek import parse_json
from meal_budget.services.menu_generation import validate_plan

SCENARIOS_PATH = PROJECT_ROOT / "examples/evaluation/scenarios.json"


class ScenarioPreparationError(ValueError):
    pass


def load_scenarios() -> list[EvaluationScenario]:
    scenarios = TypeAdapter(list[EvaluationScenario]).validate_python(
        parse_json(SCENARIOS_PATH.read_bytes())
    )
    if len({scenario.id for scenario in scenarios}) != len(scenarios):
        raise ValueError("duplicate scenario id")
    return scenarios


def require_ready_scenario(scenario_id: str) -> EvaluationScenario:
    try:
        scenarios = load_scenarios()
    except (OSError, ValueError, RecursionError):
        raise ScenarioPreparationError("Некорректные подготовленные данные сценариев.") from None
    scenario = next((item for item in scenarios if item.id == scenario_id), None)
    if scenario is None:
        raise ScenarioPreparationError("Неизвестный сценарий: выберите ID из manifest.")
    if scenario.status != "ready":
        raise ScenarioPreparationError(
            "Сценарий blocked: сначала подготовьте анкету и доказательство."
        )
    return scenario


def verify_preparation(
    scenario: EvaluationScenario,
    request: GenerationRequest,
    catalog: ProductCatalog,
    tolerance: Decimal,
) -> None:
    if scenario.status != "ready" or scenario.request is None:
        raise ScenarioPreparationError("Сценарий не готов к запуску.")
    if scenario.catalog_version != catalog.catalog_version:
        raise ScenarioPreparationError("Версия каталога не совпадает с подготовленным сценарием.")
    if scenario.request != request:
        raise ScenarioPreparationError("Анкета не совпадает с подготовленной анкетой сценария.")
    known = {product.id for product in catalog.products}
    if not set(request.excluded_product_ids + request.disliked_product_ids) <= known:
        raise ScenarioPreparationError("Предпочтения сценария ссылаются на неизвестные товары.")
    if scenario.expected == "success":
        if scenario.witness is None:
            raise ScenarioPreparationError("Не подготовлено свидетельство выполнимости.")
        calculation, violations = validate_plan(scenario.witness, request, catalog, tolerance)
        if calculation is None or violations:
            raise ScenarioPreparationError("Свидетельство не проходит расчетную проверку сценария.")
    else:
        eligible = [p for p in catalog.products if p.id not in request.excluded_product_ids]
        if not eligible or scenario.minimum_purchase != min(p.price for p in eligible):
            raise ScenarioPreparationError("Нижняя граница покупки не подтверждена каталогом.")
