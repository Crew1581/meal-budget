import asyncio
import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from main import create_app
from meal_budget import evaluate_menu, generate_menu
from meal_budget.catalog import load_catalog
from meal_budget.core.config import PROJECT_ROOT, Settings
from meal_budget.schemas.evaluation import EvaluationScenario, RunSummary
from meal_budget.schemas.generation import GeneratedPlan, GenerationRequest, GenerationResult
from meal_budget.schemas.products import Product, ProductCatalog
from meal_budget.services.deepseek import DeepSeekProvider, GenerationError
from meal_budget.services.menu_calculation import calculate_menu
from meal_budget.services.menu_generation import MenuGenerationService, validate_plan


@pytest.fixture
def web_product():
    # Вымышленные доказательства для теста СХЕМЫ, не собранная запись.
    product = json.loads((PROJECT_ROOT / "tests/fixtures/products.json").read_text())["products"][0]
    source = {
        "url": "https://5ka.ru/product/schema-test--1",
        "checked_at": "2026-10-13T12:00:00+03:00",
    }
    product["id"] = "schema-test"
    product["provenance"] = {
        "kind": "web",
        "chain": "Пятёрочка",
        "product_url": source["url"],
        "retailer_product_id": None,
        "nutrition": source.copy(),
        "price": {
            **source,
            "channel": "online_delivery",
            "region_confirmed": False,
            "region": None,
            "store": None,
            "price_type": "regular",
            "single_package_without_card": True,
        },
    }
    return product


def test_unconfirmed_region_and_numeric_product(web_product):
    product = Product.model_validate(web_product)
    assert product.provenance.price.region is None
    assert product.provenance.price.store is None
    assert product.provenance.price.checked_at.utcoffset().total_seconds() == 10800
    payload = product.model_dump(mode="json")
    assert payload["package_size_g"] == 500
    assert payload["nutrition_per_100g"]["calories_kcal"] == 370
    assert payload["price_region"] is None
    assert payload["price_updated_at"] == "2026-10-13"
    assert isinstance(product.price, Decimal)


@pytest.mark.parametrize("source", ["nutrition", "price"])
@pytest.mark.parametrize("field", ["url", "checked_at"])
def test_web_sources_required(web_product, source, field):
    del web_product["provenance"][source][field]
    with pytest.raises(ValidationError):
        Product.model_validate(web_product)


@pytest.mark.parametrize("source", ["nutrition", "price"])
def test_naive_verification_time_rejected(web_product, source):
    web_product["provenance"][source]["checked_at"] = "2026-10-13T12:00:00"
    with pytest.raises(ValidationError):
        Product.model_validate(web_product)


def test_legacy_region_cannot_override_unconfirmed_provenance(web_product):
    web_product["price_region"] = "Москва"
    with pytest.raises(ValidationError):
        Product.model_validate(web_product)


@pytest.mark.parametrize(
    "change",
    [
        {"region": "Москва"},
        {"store": "Магазин"},
        {"region_confirmed": True},
        {"channel": "offline_store"},
        {"price_type": "unclear"},
        {"price_type": "promotional"},
        {"single_package_without_card": False},
        {"url": "https://example.org/product/schema-test--1"},
        {"url": "https://5ka.ru/"},
    ],
)
def test_ambiguous_or_mislabelled_price_rejected(web_product, change):
    web_product["provenance"]["price"].update(change)
    with pytest.raises(ValidationError):
        Product.model_validate(web_product)


def test_confirmed_region_and_published_promotion(web_product):
    web_product["provenance"]["price"].update(
        region_confirmed=True,
        region="Москва",
        price_type="promotional",
        conditions="Одна упаковка без карты; опубликованная общая скидка (fixture)",
        valid_from="2026-10-13",
        valid_until="2026-10-14",
    )
    assert Product.model_validate(web_product).provenance.price.region == "Москва"
    web_product["provenance"]["price"]["valid_until"] = "2026-10-12"
    with pytest.raises(ValidationError):
        Product.model_validate(web_product)


def test_catalog_requires_version_and_unique_ids(web_product):
    catalog = load_catalog(PROJECT_ROOT / "data/5ka-products.json").model_dump()
    catalog["products"] = [web_product, copy.deepcopy(web_product)]
    with pytest.raises(ValidationError):
        ProductCatalog.model_validate(catalog)
    catalog["products"].pop()
    del catalog["catalog_version"]
    with pytest.raises(ValidationError):
        ProductCatalog.model_validate(catalog)


def test_active_snapshot_has_no_invented_products():
    catalog = load_catalog(PROJECT_ROOT / "data/5ka-products.json")
    assert catalog.products == []
    assert catalog.catalog_version == "5ka-2026-10-13-blocked-v1"
    assert "403" in catalog.warning


def test_empty_catalog_blocks_generation_before_provider():
    class ForbiddenProvider:
        def ensure_configured(self):
            pytest.fail("Empty catalog must not reach the provider")

    request = GenerationRequest.model_validate_json(
        (PROJECT_ROOT / "examples/generation-request.json").read_bytes()
    )
    catalog = load_catalog(PROJECT_ROOT / "data/5ka-products.json")
    service = MenuGenerationService(ForbiddenProvider(), Settings(_env_file=None))
    with pytest.raises(GenerationError) as error:
        asyncio.run(service.generate(request, catalog))
    assert error.value.code == "catalog_unavailable"
    assert error.value.metadata.calls == 0


def test_actual_snapshot_http_routes_without_network():
    settings = Settings(_env_file=None, catalog_path=PROJECT_ROOT / "data/5ka-products.json")
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").json() == {"status": "ok"}
        catalog = client.get("/v1/products").json()
        assert catalog["products"] == []
        assert catalog["catalog_version"]
        menu = json.loads((PROJECT_ROOT / "examples/menu-calculation.json").read_bytes())
        response = client.post("/v1/menu-calculations", json=menu)
        assert response.status_code == 422
        assert response.json()["detail"][0]["code"] == "unknown_product_id"
        assert client.post("/v1/menu-generations", json={}).status_code == 404


def test_empty_snapshot_generation_http_503_without_provider(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Empty catalog must not reach provider configuration or HTTP")

    monkeypatch.setattr(DeepSeekProvider, "ensure_configured", forbidden)
    monkeypatch.setattr(DeepSeekProvider, "complete", forbidden)
    settings = Settings(
        _env_file=None,
        catalog_path=PROJECT_ROOT / "data/5ka-products.json",
        generation_http_enabled=True,
        generation_access_token="test-access",
    )
    with TestClient(create_app(settings)) as client:
        request = json.loads((PROJECT_ROOT / "examples/generation-request.json").read_bytes())
        response = client.post(
            "/v1/menu-generations",
            json=request,
            headers={"Authorization": "Bearer test-access"},
        )
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "catalog_unavailable"
        assert response.json()["detail"]["metadata"]["calls"] == 0
        schema = client.get("/openapi.json").json()["components"]["schemas"]
        assert schema["GenerationResult"]["properties"]["nutrition_tolerance"]["type"] == "number"
        assert schema["Product"]["properties"]["package_size_g"]["type"] == "number"


def test_six_scenarios_are_explicitly_blocked():
    scenarios = evaluate_menu.load_scenarios()
    assert len(scenarios) == 6
    assert len({scenario.id for scenario in scenarios}) == 6
    for scenario in scenarios:
        assert scenario.status == "blocked"
        assert scenario.request is None
        assert scenario.witness is None


def test_ready_scenario_infrastructure_with_synthetic_witness():
    # Не доказательство выполнимости реального меню: проверяем механизм на fixture.
    catalog = load_catalog(PROJECT_ROOT / "tests/fixtures/products.json")
    request = GenerationRequest.model_validate_json(
        (PROJECT_ROOT / "examples/generation-request.json").read_bytes()
    )
    plan = GeneratedPlan.model_validate(
        {
            "recipes": [
                {
                    "id": "r",
                    "name": "fixture",
                    "servings": 1,
                    "ingredients": [{"product_id": "oats", "grams": "100"}],
                    "steps": ["Добавить обычную питьевую воду и приготовить."],
                    "cooking_minutes": 10,
                }
            ],
            "days": [{"day": 1, "meals": [{"recipe_id": "r", "servings": "1"}]}],
        }
    )
    scenario = EvaluationScenario(
        id="test",
        title="fixture",
        status="ready",
        expected="success",
        preparation="Только тест механизма",
        catalog_version=catalog.catalog_version,
        request=request,
        witness=plan,
    )
    calculation, violations = validate_plan(
        scenario.witness, scenario.request, catalog, Decimal("0.10")
    )
    assert not violations
    assert calculation.total_cost == Decimal("89.90")
    assert calculation == calculate_menu(plan.calculation_request(request.budget), catalog)
    result = GenerationResult(
        catalog_version=catalog.catalog_version,
        plan=plan,
        calculation=calculation,
        warnings=[catalog.warning],
        nutrition_tolerance="0.10",
        metadata={"calls": 0, "duration_seconds": 0, "responses": []},
    )
    payload = json.loads(result.model_dump_json())
    assert payload["catalog_version"] == payload["calculation"]["catalog_version"]
    assert payload["nutrition_tolerance"] == 0.1
    assert payload["plan"]["recipes"][0]["ingredients"][0]["grams"] == 100
    assert payload["plan"]["days"][0]["meals"][0]["servings"] == 1
    assert payload["calculation"]["total_cost"] == 89.9
    schema = GenerationResult.model_json_schema(mode="serialization")
    assert schema["properties"]["nutrition_tolerance"]["type"] == "number"
    assert schema["$defs"]["Ingredient"]["properties"]["grams"]["type"] == "number"


def test_summary_is_offline_and_uses_protected_generated(tmp_path, monkeypatch):
    monkeypatch.setattr(generate_menu, "PROJECT_ROOT", tmp_path)
    assert (
        evaluate_menu.main(["--scenario", "ordinary", "--output", "evaluation/summary.json"]) == 0
    )
    summary = evaluate_menu.read_report(Path("evaluation/summary.json"))
    assert summary.outcome == "blocked"
    assert summary.metadata is None
    assert summary.manual.conclusion is None
    assert (tmp_path / "generated/evaluation/summary.json").stat().st_mode & 0o777 == 0o600
    assert evaluate_menu.main(["--output", "../outside.json"]) == 2
    (tmp_path / "generated/link.json").symlink_to(tmp_path / "outside.json")
    with pytest.raises(OSError):
        evaluate_menu.read_report(Path("link.json"))
    assert (
        evaluate_menu.main(
            [
                "--scenario",
                "weekly",
                "--result",
                "evaluation/summary.json",
                "--output",
                "other.json",
            ]
        )
        == 2
    )
    assert not (tmp_path / "outside.json").exists()


def test_report_schema_does_not_estimate_api_cost():
    fields = RunSummary.model_fields
    assert "api_cost" not in fields
    assert "metadata" in fields
    assert "manual" in fields


def test_blocked_scenario_cannot_publish_success_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(generate_menu, "PROJECT_ROOT", tmp_path)
    summary = RunSummary(
        scenario="ordinary",
        catalog_version="synthetic-meal-3-v1",
        outcome="success",
        budget_pass=True,
        nutrition_pass=True,
    )
    generate_menu.save_result(Path("tampered.json"), summary.model_dump_json(), overwrite=False)
    assert (
        evaluate_menu.main(
            [
                "--scenario",
                "ordinary",
                "--result",
                "tampered.json",
                "--output",
                "summary.json",
            ]
        )
        == 2
    )
    assert not (tmp_path / "generated/summary.json").exists()


def test_default_demo_catalog_still_runs_without_fixture_selection(monkeypatch):
    monkeypatch.delenv("CATALOG_PATH")
    settings = Settings(_env_file=None, deepseek_api_key="")
    assert settings.catalog_path == PROJECT_ROOT / "data/products.json"
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").json() == {"status": "ok"}
        catalog = client.get("/v1/products").json()
        assert catalog["catalog_version"] == "demo-meal-3-v1"
        assert catalog["data_status"] == "demo_unverified"
        assert "не реальные товары" in catalog["warning"]
        assert len(catalog["products"]) == 3
        assert all(p["provenance"]["kind"] == "synthetic_demo" for p in catalog["products"])
        assert all(p["price_region"] is None for p in catalog["products"])
        menu = json.loads((PROJECT_ROOT / "examples/menu-calculation.json").read_bytes())
        response = client.post("/v1/menu-calculations", json=menu)
        assert response.status_code == 200
        assert response.json()["total_cost"] == 89.9
        assert response.json()["data_status"] == "demo_unverified"
