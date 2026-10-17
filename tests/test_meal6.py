"""Сохранность ассортимента и значений при сокращении контекста модели."""

import json
from decimal import Decimal

import pytest

from meal_budget.catalog import load_catalog
from meal_budget.core.config import PROJECT_ROOT
from meal_budget.schemas.generation import GeneratedPlan, GenerationRequest
from meal_budget.schemas.prompt import PromptProduct
from meal_budget.services.menu_generation import make_messages


@pytest.mark.parametrize("scenario", ["ordinary", "weekly"])
@pytest.mark.parametrize("exclude", [False, True])
def test_compact_prompt_preserves_every_allowed_product(scenario, exclude):
    catalog = load_catalog(PROJECT_ROOT / "data/5ka-products.json")
    before = catalog.model_dump_json()
    request = GenerationRequest.model_validate_json(
        (PROJECT_ROOT / f"examples/evaluation/{scenario}.request.json").read_bytes()
    )
    if not exclude:
        request.excluded_product_ids = []
    messages = make_messages(request, catalog, Decimal("0.10"))
    server_text = messages[2]["content"]
    server = json.loads(server_text, parse_float=Decimal)
    expected = [p for p in catalog.products if p.id not in request.excluded_product_ids]
    assert len(server["server_catalog"]) == (68 if exclude else 70)
    assert [p["id"] for p in server["server_catalog"]] == [p.id for p in expected]
    for actual, original in zip(server["server_catalog"], expected, strict=True):
        assert set(actual) == {
            "id",
            "name",
            "state",
            "state_basis",
            "nutrition_per_100g",
            "package_size_g",
            "price",
        }
        restored = PromptProduct.model_validate(actual)
        assert restored.model_dump() == {
            key: original.model_dump()[key] for key in PromptProduct.model_fields
        }
    assert server["catalog_version"] == catalog.catalog_version
    context = server["catalog_context"]
    assert context["warning"] == catalog.warning
    assert context["data_status"] == "demo_unverified"
    assert "не подтверждены" in context["geography_and_conditions"]
    assert "displayed" in context["prices"]
    assert "as_sold" in context["state_basis"]
    assert catalog.model_dump_json() == before
    for forbidden in (
        "provenance",
        "https://",
        "checked_at",
        "price_source",
        "price_region",
        "price_updated_at",
        "web_cache_age",
    ):
        assert forbidden not in server_text
    # Та же схема ответа, без вырезания ограничений ради размера.
    assert (
        json.loads(messages[0]["content"].split("\nJSON Schema:\n")[1])
        == GeneratedPlan.model_json_schema()
    )
    assert messages[1]["content"] == json.dumps(
        {"user_data": request.model_dump(mode="json")}, ensure_ascii=False, separators=(",", ":")
    )
    legacy_products = json.dumps([p.model_dump(mode="json") for p in expected], ensure_ascii=False)
    assert len(server_text) < len(legacy_products) / 3
    assert len(server_text.encode()) < len(legacy_products.encode()) / 3
