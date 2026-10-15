"""Проверки конкретного снимка/подготовки, не unit-тесты финансовой арифметики."""

import copy
import json
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from meal_budget import evaluate_menu, generate_menu
from meal_budget.catalog import load_catalog
from meal_budget.core.config import PROJECT_ROOT
from meal_budget.evaluation import require_ready_scenario, verify_preparation
from meal_budget.schemas.generation import GeneratedPlan, GenerationRequest, GenerationResult
from meal_budget.schemas.products import Product
from meal_budget.services.deepseek import DeepSeekProvider, parse_json
from meal_budget.services.menu_calculation import NUTRIENTS, MenuCalculationError, calculate_menu
from meal_budget.services.menu_generation import validate_plan
from tools import import_5ka_snapshot, prepare_evaluation

VERSION = "5ka-2026-10-14-observed-v3"
TOLERANCE = Decimal("0.10")


@pytest.fixture
def imported_catalog():
    return load_catalog(PROJECT_ROOT / "data/5ka-products.json")


def test_imported_snapshot_provenance_and_demo_ids(imported_catalog):
    demo = load_catalog(PROJECT_ROOT / "data/products.json")
    assert {p.id for p in demo.products} == {"oats", "buckwheat", "chicken"}
    assert {p.id for p in demo.products}.isdisjoint(p.id for p in imported_catalog.products)
    assert imported_catalog.catalog_version == VERSION
    assert imported_catalog.data_status == "demo_unverified"
    assert len(imported_catalog.products) == 70
    for product in imported_catalog.products:
        assert product.id == f"5ka_{product.provenance.retailer_product_id}"
        assert isinstance(product.price, Decimal)
        assert product.state_basis == "as_sold"
        assert product.model_dump()["price_updated_at"] is None
        price = product.provenance.price
        assert price.price_type == "displayed"
        assert price.single_package_without_card is None
        assert not price.region_confirmed and price.region is None and price.store is None
        for source in (price, product.provenance.nutrition):
            assert source.observation_method == "web_tool"
            assert source.checked_at.utcoffset() is not None
    observations = parse_json((PROJECT_ROOT / "data/5ka-candidates.json").read_bytes())[
        "source_observations"
    ]
    ages = {item["id"]: item["web_cache_age"] for item in observations}
    assert all(
        p.provenance.price.web_cache_age == ages.get(p.id) for p in imported_catalog.products
    )
    assert any(p.provenance.price.web_cache_age == "last week" for p in imported_catalog.products)


@pytest.mark.parametrize(
    "change",
    [
        {"price_type": "regular"},
        {"price_type": "promotional"},
        {"single_package_without_card": True},
        {"single_package_without_card": False},
        {"conditions": None},
        {"channel": "offline_store"},
        {"observation_method": "direct_page", "web_cache_age": "last week"},
    ],
)
def test_observation_cannot_silently_become_confirmed(imported_catalog, change):
    raw = imported_catalog.products[0].model_dump()
    raw["provenance"]["price"].update(change)
    with pytest.raises(ValidationError):
        Product.model_validate(raw)


def test_candidate_inventory_is_not_a_calculation_catalog(imported_catalog):
    path = PROJECT_ROOT / "data/5ka-candidates.json"
    inventory = parse_json(path.read_bytes())
    candidates = {x["id"] for x in inventory["candidates"]}
    assert len(candidates) == 79
    assert candidates.isdisjoint(p.id for p in imported_catalog.products)
    with pytest.raises(RuntimeError, match="Некорректный каталог"):
        load_catalog(path)
    scenario = require_ready_scenario("ordinary")
    menu = scenario.witness.calculation_request(scenario.request.budget)
    menu.recipes[0].ingredients[0].product_id = inventory["candidates"][0]["id"]
    with pytest.raises(MenuCalculationError) as error:
        calculate_menu(menu, imported_catalog)
    assert error.value.code == "unknown_product_id"


@pytest.mark.parametrize("scenario_id", ["preferences", "quick", "tight-budget", "impossible"])
def test_unprepared_scenarios_with_imported_catalog_have_zero_calls(
    tmp_path, monkeypatch, scenario_id
):
    calls = []

    def forbidden(*args):
        calls.append(args)
        pytest.fail("blocked must not create or call provider")

    monkeypatch.setattr(generate_menu, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("CATALOG_PATH", str(PROJECT_ROOT / "data/5ka-products.json"))
    monkeypatch.setattr(generate_menu, "run_generation", forbidden)
    assert (
        generate_menu.main(
            [
                "--request",
                str(PROJECT_ROOT / "examples/evaluation/ordinary.request.json"),
                "--scenario",
                scenario_id,
                "--output",
                "menu.json",
                "--report",
                "report.json",
            ]
        )
        == 2
    )
    assert len(calls) == 0
    assert not (tmp_path / "generated").exists()


@pytest.mark.parametrize(
    "scenario_id,days,cost", [("ordinary", 3, "4718.80"), ("weekly", 7, "6800.69")]
)
def test_witnesses_match_manifest_and_all_constraints(imported_catalog, scenario_id, days, cost):
    prefix = PROJECT_ROOT / "examples/evaluation" / scenario_id
    request = GenerationRequest.model_validate_json(
        prefix.with_suffix(".request.json").read_bytes()
    )
    plan = GeneratedPlan.model_validate_json(prefix.with_suffix(".witness.json").read_bytes())
    scenario = require_ready_scenario(scenario_id)
    assert scenario.request == request and scenario.witness == plan
    assert request.days == days and request.meals_per_day == 3
    assert request.max_cooking_minutes == 45
    assert scenario.catalog_version == VERSION
    verify_preparation(scenario, request, imported_catalog, TOLERANCE)
    result, violations = validate_plan(plan, request, imported_catalog, TOLERANCE)
    assert not violations and result is not None
    assert result.total_cost == Decimal(cost)
    assert Decimal(0) < result.budget_remaining <= result.total_cost * Decimal("0.06")
    exact = calculate_menu(
        plan.calculation_request(request.budget), imported_catalog, round_output=False
    )
    for day in exact.days:
        assert len(day.meals) == 3
        for key in NUTRIENTS:
            target = getattr(request.daily_targets, key)
            assert (
                target * (1 - TOLERANCE) <= getattr(day.nutrition, key) <= target * (1 + TOLERANCE)
            )
    used = {i.product_id for r in plan.recipes for i in r.ingredients}
    assert used.isdisjoint(request.excluded_product_ids)
    assert all(r.cooking_minutes <= request.max_cooking_minutes for r in plan.recipes)
    assert all(r.servings == 1 for r in plan.recipes)
    assert all(m.servings == 1 for d in plan.days for m in d.meals)
    for item in exact.shopping_list:
        assert item.packages == (item.required_grams / item.package_size_g).to_integral_value(
            rounding=ROUND_CEILING
        )
        assert item.remaining_grams == item.purchased_grams - item.required_grams


@pytest.mark.parametrize("scenario_id", ["ordinary", "weekly"])
def test_prepared_cli_with_mock_transport_and_selected_version(tmp_path, monkeypatch, scenario_id):
    scenario = require_ready_scenario(scenario_id)
    requests = []

    def complete(request):
        requests.append(request)
        payload = {
            "model": "local-test-only",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": scenario.witness.model_dump_json()},
                }
            ],
        }
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(payload).encode()))

    transport = httpx.MockTransport(complete)
    monkeypatch.setattr(generate_menu, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("CATALOG_PATH", str(PROJECT_ROOT / "data/5ka-products.json"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("DEEPSEEK_MODEL", "local-test-only")
    monkeypatch.setenv("GENERATION_NUTRITION_TOLERANCE", "0.10")
    monkeypatch.setattr(
        generate_menu,
        "DeepSeekProvider",
        lambda settings: DeepSeekProvider(settings, transport=transport),
    )
    assert (
        generate_menu.main(
            [
                "--request",
                str(PROJECT_ROOT / f"examples/evaluation/{scenario_id}.request.json"),
                "--scenario",
                scenario_id,
                "--output",
                "menu.json",
                "--report",
                "report.json",
            ]
        )
        == 0
    )
    result = GenerationResult.model_validate_json((tmp_path / "generated/menu.json").read_bytes())
    report = evaluate_menu.read_report(Path("report.json"))
    assert (
        result.catalog_version
        == result.calculation.catalog_version
        == report.catalog_version
        == VERSION
    )
    assert result.metadata.calls == len(requests) == 1
    assert report.outcome == "success" and report.budget_pass and report.nutrition_pass
    assert result.warnings
    sent = json.loads(requests[0].content)["messages"]
    assert scenario.witness.recipes[0].name not in json.dumps(sent, ensure_ascii=False)
    catalog_message = json.loads(sent[2]["content"])
    assert not set(scenario.request.excluded_product_ids) & {
        p["id"] for p in catalog_message["server_catalog"]
    }


def test_import_is_reproducible_without_modifying_sources(tmp_path, monkeypatch, imported_catalog):
    # Восстанавливаем входной контракт v2 для теста преобразования; не веб-проверка.
    raw = imported_catalog.model_dump(mode="json")
    raw["catalog_version"] = "5ka-2026-10-14-web-v2"
    for p in raw["products"]:
        p.pop("state_basis")
        for source in (p["provenance"]["price"], p["provenance"]["nutrition"]):
            source.pop("observation_method")
            source.pop("web_cache_age")
        p["provenance"]["price"].update(price_type="regular", single_package_without_card=True)
    products = tmp_path / "input.json"
    candidates = tmp_path / "inventory.json"
    products.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    candidates.write_bytes((PROJECT_ROOT / "data/5ka-candidates.json").read_bytes())
    before = [p.read_bytes() for p in (products, candidates)]
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(import_5ka_snapshot, "PROJECT_ROOT", tmp_path)
    assert (
        import_5ka_snapshot.main(["--products", str(products), "--candidates", str(candidates)])
        == 0
    )
    assert before == [p.read_bytes() for p in (products, candidates)]
    assert load_catalog(tmp_path / "data/5ka-products.json") == imported_catalog


def test_preparation_does_not_publish_partial_ready_manifest(tmp_path, monkeypatch):
    scenarios = copy.deepcopy(prepare_evaluation.load_scenarios())
    manifest = tmp_path / "scenarios.json"
    manifest.write_text("unchanged", encoding="utf-8")
    monkeypatch.setattr(prepare_evaluation, "load_scenarios", lambda: scenarios)
    monkeypatch.setattr(prepare_evaluation, "SCENARIOS_PATH", manifest)

    checked = []

    def reject(scenario, *args):
        checked.append(scenario.id)
        if scenario.id == "weekly":
            raise ValueError("invalid witness")
        verify_preparation(scenario, *args)

    monkeypatch.setattr(prepare_evaluation, "verify_preparation", reject)
    with pytest.raises(ValueError, match="invalid witness"):
        prepare_evaluation.main()
    assert checked == ["ordinary", "weekly"]
    assert manifest.read_text() == "unchanged"
