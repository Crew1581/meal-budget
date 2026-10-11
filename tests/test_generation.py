import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from meal_budget.schemas.generation import GenerationRequest


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("days", 0),
        ("days", 32),
        ("days", True),
        ("days", 1.5),
        ("budget", "0"),
        ("budget", "-1"),
        ("budget", "1.001"),
        ("budget", "NaN"),
        ("budget", "Infinity"),
        ("budget", "1000000.01"),
        ("max_cooking_minutes", 0),
        ("max_cooking_minutes", 241),
        ("meals_per_day", 0),
        ("meals_per_day", 11),
        ("price_region", " "),
        ("price_region", "x" * 121),
        ("wishes", "x" * 2001),
        ("excluded_product_ids", ["invalid id"]),
        ("disliked_product_ids", ["oats"] * 501),
        ("unknown", 1),
    ],
)
def test_invalid_generation_request(generation_payload, field, value):
    generation_payload[field] = value
    with pytest.raises(ValidationError):
        GenerationRequest.model_validate(generation_payload)


@pytest.mark.parametrize("field", ["calories_kcal", "protein_g", "fat_g", "carbs_g"])
@pytest.mark.parametrize("value", [0, -1, "NaN", "Infinity", 100_001])
def test_invalid_targets(generation_payload, field, value):
    generation_payload["daily_targets"][field] = value
    with pytest.raises(ValidationError):
        GenerationRequest.model_validate(generation_payload)


@pytest.mark.parametrize("budget", [89.90, "89.90", Decimal("89.90"), 90, "1000000.00"])
def test_money_parse_and_numeric_json(generation_payload, budget):
    generation_payload["budget"] = budget
    request = GenerationRequest.model_validate(generation_payload)
    assert isinstance(request.budget, Decimal)
    assert request.budget == Decimal(str(budget))
    encoded = request.model_dump_json()
    assert isinstance(json.loads(encoded)["budget"], (int, float))
    assert json.loads(encoded, parse_float=Decimal)["budget"] == request.budget
    assert GenerationRequest.model_validate_json(encoded).budget == request.budget


def test_decimal_arithmetic(generation_payload):
    generation_payload["budget"] = "0.10"
    first = GenerationRequest.model_validate(generation_payload).budget
    generation_payload["budget"] = "0.20"
    second = GenerationRequest.model_validate(generation_payload).budget
    assert isinstance(first + second, Decimal)
    assert first + second == Decimal("0.30")
    assert first * 3 == Decimal("0.30")


def test_default_days_and_lists(generation_payload):
    for field in ("days", "excluded_product_ids", "disliked_product_ids"):
        generation_payload.pop(field)
    request = GenerationRequest.model_validate(generation_payload)
    assert request.days == 7
    assert request.excluded_product_ids == request.disliked_product_ids == []
