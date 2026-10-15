"""Публикует локальные ordinary/weekly в manifest только после проверки witness."""

from decimal import Decimal

from pydantic import TypeAdapter

from meal_budget.catalog import load_catalog
from meal_budget.core.config import PROJECT_ROOT
from meal_budget.evaluation import SCENARIOS_PATH, load_scenarios, verify_preparation
from meal_budget.schemas.evaluation import EvaluationScenario
from meal_budget.schemas.generation import GeneratedPlan, GenerationRequest


def main() -> int:
    catalog = load_catalog(PROJECT_ROOT / "data/5ka-products.json")
    scenarios = load_scenarios()
    for index, scenario in enumerate(scenarios):
        if scenario.id not in {"ordinary", "weekly"}:
            continue
        prefix = PROJECT_ROOT / "examples/evaluation" / scenario.id
        request = GenerationRequest.model_validate_json(
            prefix.with_suffix(".request.json").read_bytes()
        )
        witness = GeneratedPlan.model_validate_json(
            prefix.with_suffix(".witness.json").read_bytes()
        )
        prepared = EvaluationScenario(
            id=scenario.id,
            title=scenario.title,
            status="ready",
            expected="success",
            preparation="Ручной план: ротация трех обедов, ежедневные повторы завтрака и ужина. "
            "Цели 2075/125/67/245, допуск ±10% без расширения; бюджет — корзина плюс около 5%. "
            "Регион и условия цены без карты не подтверждены. "
            "Подготовка и остатки: docs/EVALUATION.md.",
            catalog_version=catalog.catalog_version,
            request=request,
            witness=witness,
        )
        verify_preparation(prepared, request, catalog, Decimal("0.10"))
        scenarios[index] = prepared
    # Запись после успешной проверки ОБОИХ сценариев; witness не отправляется модели.
    SCENARIOS_PATH.write_bytes(
        TypeAdapter(list[EvaluationScenario]).dump_json(scenarios, indent=2) + b"\n"
    )
    print("ordinary and weekly verified; manifest updated without provider calls")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
