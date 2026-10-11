import copy
import json
from decimal import Decimal, localcontext

import pytest
from fastapi.testclient import TestClient

from meal_budget.core.config import Settings
from meal_budget.main import create_app
from meal_budget.schemas.menu import MenuCalculationRequest
from meal_budget.schemas.products import ProductCatalog
from meal_budget.services.menu_calculation import MenuCalculationError, calculate_menu


@pytest.fixture
def small_catalog():
    return ProductCatalog.model_validate(
        {
            "data_status": "demo_unverified",
            "warning": "Тестовые демонстрационные данные",
            "products": [
                {
                    "id": product_id,
                    "name": product_id,
                    "state": state,
                    "nutrition_per_100g": nutrients,
                    "package_size_g": 100,
                    "price": price,
                    "price_region": "Тест",
                    "price_source": "Тест",
                    "price_updated_at": "2026-10-10",
                }
                for product_id, state, nutrients, price in [
                    (
                        "a",
                        "dry",
                        {"calories_kcal": 100, "protein_g": 10, "fat_g": 2, "carbs_g": 20},
                        "0.10",
                    ),
                    (
                        "b",
                        "raw",
                        {"calories_kcal": 200, "protein_g": 20, "fat_g": 4, "carbs_g": 0},
                        "0.20",
                    ),
                ]
            ],
        }
    )


@pytest.fixture
def menu_payload():
    return {
        "recipes": [
            {
                "id": "mix",
                "name": "Смесь",
                "servings": 2,
                "ingredients": [
                    {"product_id": "a", "grams": 100},
                    {"product_id": "b", "grams": 50},
                ],
            },
            {
                "id": "single",
                "name": "Второй",
                "servings": 1,
                "ingredients": [{"product_id": "a", "grams": 25}],
            },
        ],
        "days": [
            {"day": 1, "meals": [{"recipe_id": "mix", "servings": "0.5"}]},
            {
                "day": 2,
                "meals": [
                    {"recipe_id": "mix", "servings": "1.5"},
                    {"recipe_id": "single", "servings": 1},
                ],
            },
        ],
        "budget": "0.40",
    }


@pytest.fixture
def calculation_client(tmp_path, small_catalog):
    path = tmp_path / "catalog.json"
    path.write_text(small_catalog.model_dump_json(), encoding="utf-8")
    with TestClient(create_app(Settings(_env_file=None, catalog_path=path))) as client:
        yield client


def compute(payload, catalog):
    return calculate_menu(MenuCalculationRequest.model_validate(payload), catalog)


def test_nutrition_and_aggregated_shopping(menu_payload, small_catalog):
    result = compute(menu_payload, small_catalog)
    assert result.days[0].meals[0].nutrition.model_dump() == {
        "calories_kcal": Decimal("50.00"),
        "protein_g": Decimal("5.00"),
        "fat_g": Decimal("1.00"),
        "carbs_g": Decimal("5.00"),
    }
    assert result.days[1].nutrition.model_dump() == {
        "calories_kcal": Decimal("175.00"),
        "protein_g": Decimal("17.50"),
        "fat_g": Decimal("3.50"),
        "carbs_g": Decimal("20.00"),
    }
    assert result.nutrition.model_dump() == {
        "calories_kcal": Decimal("225.00"),
        "protein_g": Decimal("22.50"),
        "fat_g": Decimal("4.50"),
        "carbs_g": Decimal("25.00"),
    }
    first, second = result.shopping_list
    assert (first.required_grams, first.packages, first.purchased_grams, first.remaining_grams) == (
        Decimal(125),
        2,
        Decimal(200),
        Decimal(75),
    )
    assert (second.required_grams, second.packages, second.remaining_grams) == (
        Decimal(50),
        1,
        Decimal(50),
    )
    assert result.total_cost == Decimal("0.40")
    assert result.within_budget
    assert result.budget_remaining == result.over_budget_by == 0
    assert result.data_status == "demo_unverified"
    assert result.warning == small_catalog.warning


@pytest.mark.parametrize(
    "budget,within,remaining,over",
    [
        ("0.40", True, "0.00", "0.00"),
        ("0.39", False, "-0.01", "0.01"),
        ("1.00", True, "0.60", "0.00"),
    ],
)
def test_budget(menu_payload, small_catalog, budget, within, remaining, over):
    menu_payload["budget"] = budget
    result = compute(menu_payload, small_catalog)
    assert result.within_budget is within
    assert result.budget_remaining == Decimal(remaining)
    assert result.over_budget_by == Decimal(over)


def one_recipe(grams=100, servings=1, meals=None):
    return {
        "recipes": [
            {
                "id": "r",
                "name": "Тест",
                "servings": servings,
                "ingredients": [{"product_id": "a", "grams": grams}],
            }
        ],
        "days": [{"day": 1, "meals": meals or [{"recipe_id": "r", "servings": 1}]}],
        "budget": "0.30",
    }


@pytest.mark.parametrize(
    "grams,packages,remaining",
    [
        ("100", 1, "0.00"),
        ("100.000001", 2, "100.00"),
        ("99.995", 1, "0.01"),
    ],
)
def test_package_boundaries(small_catalog, grams, packages, remaining):
    item = compute(one_recipe(grams), small_catalog).shopping_list[0]
    assert item.packages == packages
    assert item.remaining_grams == Decimal(remaining)


def test_aggregate_before_ceiling_and_exact_thirds(small_catalog):
    result = compute(one_recipe(100, 3, [{"recipe_id": "r", "servings": 1}] * 3), small_catalog)
    assert result.shopping_list[0].packages == 1
    assert result.shopping_list[0].required_grams == 100
    assert result.shopping_list[0].remaining_grams == 0
    assert result.nutrition.calories_kcal == 100


def test_financial_float_sensitive_case(menu_payload, small_catalog):
    menu_payload["recipes"][1]["ingredients"][0]["grams"] = 0.000001
    menu_payload["days"][1]["meals"][0]["servings"] = "1.499999"
    menu_payload["budget"] = "0.30"
    result = compute(menu_payload, small_catalog)
    assert result.total_cost == Decimal("0.30")
    assert result.within_budget


def test_round_only_output(small_catalog):
    result = compute(one_recipe("0.005", 1, [{"recipe_id": "r", "servings": 1}] * 3), small_catalog)
    assert [meal.nutrition.calories_kcal for meal in result.days[0].meals] == [Decimal("0.01")] * 3
    assert result.days[0].nutrition.calories_kcal == Decimal("0.02")
    assert result.nutrition.calories_kcal == Decimal("0.02")
    assert result.shopping_list[0].required_grams == Decimal("0.02")


def test_repeating_fractions_do_not_change_half_up_boundary(small_catalog):
    result = compute(one_recipe("1.005", 3, [{"recipe_id": "r", "servings": 1}] * 3), small_catalog)
    assert result.nutrition.calories_kcal == Decimal("1.01")
    assert result.days[0].nutrition.calories_kcal == Decimal("1.01")


def test_order_and_global_decimal_context(menu_payload, small_catalog):
    original = compute(menu_payload, small_catalog)
    menu_payload["recipes"].reverse()
    for recipe in menu_payload["recipes"]:
        recipe["ingredients"].reverse()
    with localcontext() as context:
        context.prec = 6
        assert compute(menu_payload, small_catalog) == original


@pytest.mark.parametrize(
    "kind,code,field",
    [
        ("product", "unknown_product_id", "recipes.0.ingredients.0.product_id"),
        ("recipe", "unknown_recipe_id", "days.0.meals.0.recipe_id"),
        ("unused", "unused_recipe", "recipes.1.id"),
    ],
)
def test_reference_errors(menu_payload, small_catalog, calculation_client, kind, code, field):
    if kind == "product":
        menu_payload["recipes"][0]["ingredients"][0]["product_id"] = "missing"
    elif kind == "recipe":
        menu_payload["days"][0]["meals"][0]["recipe_id"] = "missing"
    else:
        menu_payload["days"][1]["meals"].pop()
    with pytest.raises(MenuCalculationError) as error:
        compute(menu_payload, small_catalog)
    assert error.value.code == code
    response = calculation_client.post("/v1/menu-calculations", json=menu_payload)
    assert response.status_code == 422
    detail = response.json()["detail"][0]
    assert detail["code"] == code
    assert detail["field"] == field
    assert "missing" in detail["message"] if kind != "unused" else "single" in detail["message"]


@pytest.mark.parametrize("field", ["grams", "meal_servings", "recipe_servings", "day", "budget"])
@pytest.mark.parametrize("value", [0, -1, "NaN", "Infinity", "-Infinity", True])
def test_invalid_numbers(calculation_client, field, value):
    payload = one_recipe()
    if field == "grams":
        payload["recipes"][0]["ingredients"][0]["grams"] = value
    elif field == "meal_servings":
        payload["days"][0]["meals"][0]["servings"] = value
    elif field == "recipe_servings":
        payload["recipes"][0]["servings"] = value
    elif field == "day":
        payload["days"][0]["day"] = value
    else:
        payload["budget"] = value
    response = calculation_client.post("/v1/menu-calculations", json=payload)
    assert response.status_code == 422
    assert set(response.json()["detail"][0]) == {"code", "field", "message"}


@pytest.mark.parametrize(
    "kind,code",
    [
        ("duplicate_recipe", "duplicate_recipe_id"),
        ("duplicate_product", "duplicate_product_id"),
        ("skip_day", "invalid_day_sequence"),
        ("repeat_day", "invalid_day_sequence"),
        ("reverse_days", "invalid_day_sequence"),
        ("recipes_empty", "too_short"),
        ("ingredients_empty", "too_short"),
        ("days_empty", "too_short"),
        ("meals_empty", "too_short"),
        ("recipes_large", "too_long"),
        ("ingredients_large", "too_long"),
        ("days_large", "too_long"),
        ("meals_large", "too_long"),
        ("grams_large", "less_than_equal"),
        ("grams_precision", "decimal_max_places"),
        ("meal_large", "less_than_equal"),
        ("recipe_large", "less_than_equal"),
        ("recipe_fraction", "int_type"),
        ("unknown_field", "extra_forbidden"),
    ],
)
def test_structure_and_limits(calculation_client, menu_payload, kind, code):
    if kind == "duplicate_recipe":
        menu_payload["recipes"].append(copy.deepcopy(menu_payload["recipes"][0]))
    elif kind == "duplicate_product":
        menu_payload["recipes"][0]["ingredients"].append(
            copy.deepcopy(menu_payload["recipes"][0]["ingredients"][0])
        )
    elif kind == "skip_day":
        menu_payload["days"][1]["day"] = 3
    elif kind == "repeat_day":
        menu_payload["days"][1]["day"] = 1
    elif kind == "reverse_days":
        menu_payload["days"].reverse()
    elif kind in {"recipes_empty", "recipes_large", "days_empty", "days_large"}:
        field = kind.split("_")[0]
        menu_payload[field] = [] if kind.endswith("empty") else menu_payload[field] * 101
    elif kind.startswith("ingredients_"):
        menu_payload["recipes"][0]["ingredients"] = (
            [] if kind.endswith("empty") else menu_payload["recipes"][0]["ingredients"] * 101
        )
    elif kind.startswith("meals_"):
        menu_payload["days"][0]["meals"] = (
            [] if kind.endswith("empty") else menu_payload["days"][0]["meals"] * 11
        )
    elif kind.startswith("grams_"):
        menu_payload["recipes"][0]["ingredients"][0]["grams"] = (
            100001 if kind.endswith("large") else "0.0000001"
        )
    elif kind == "meal_large":
        menu_payload["days"][0]["meals"][0]["servings"] = 101
    elif kind.startswith("recipe_"):
        menu_payload["recipes"][0]["servings"] = 101 if kind.endswith("large") else 1.5
    else:
        menu_payload["price"] = 1
    response = calculation_client.post("/v1/menu-calculations", json=menu_payload)
    assert response.status_code == 422
    assert code in {error["code"] for error in response.json()["detail"]}


def test_api_success_numbers_and_exceeded_budget(calculation_client, menu_payload):
    menu_payload["budget"] = "0.39"
    response = calculation_client.post("/v1/menu-calculations", json=menu_payload)
    assert response.status_code == 200
    body = response.json()
    assert body["within_budget"] is False
    assert body["budget_remaining"] == -0.01
    assert body["data_status"] == "demo_unverified"
    assert body["warning"]
    assert json.loads(response.text, parse_float=Decimal)["total_cost"] == Decimal("0.40")

    def no_numeric_strings(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key not in {"product_id", "recipe_id", "name", "data_status", "warning"}:
                    no_numeric_strings(item)
        elif isinstance(value, list):
            for item in value:
                no_numeric_strings(item)
        else:
            assert isinstance(value, (int, float, bool))

    no_numeric_strings(body)
    assert calculation_client.get("/health").json() == {"status": "ok"}
    assert calculation_client.get("/v1/products").json()["products"][0]["package_size_g"] == "100"


def test_loaded_catalog_used_after_file_deleted(tmp_path, small_catalog):
    path = tmp_path / "catalog.json"
    path.write_text(small_catalog.model_dump_json(), encoding="utf-8")
    with TestClient(create_app(Settings(_env_file=None, catalog_path=path))) as client:
        path.unlink()
        assert client.post("/v1/menu-calculations", json=one_recipe()).status_code == 200


def test_invalid_json_does_not_expose_body(calculation_client):
    response = calculation_client.post(
        "/v1/menu-calculations", content='{"private":', headers={"content-type": "application/json"}
    )
    assert response.status_code == 422
    assert response.json()["detail"][0]["code"] == "json_invalid"
    assert "private" not in response.text
