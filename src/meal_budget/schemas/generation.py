from typing import Annotated

from pydantic import Field

from meal_budget.schemas.common import Contract, Money, PositiveNumber, ProductId, ShortText


class DailyTargets(Contract):
    calories_kcal: PositiveNumber
    protein_g: PositiveNumber
    fat_g: PositiveNumber
    carbs_g: PositiveNumber


class GenerationRequest(Contract):
    """Будущий запрос: технические пределы, не медицинские рекомендации."""

    days: Annotated[int, Field(strict=True, ge=1, le=31)] = 7
    budget: Money
    daily_targets: DailyTargets
    excluded_product_ids: list[ProductId] = Field(default_factory=list, max_length=500)
    disliked_product_ids: list[ProductId] = Field(default_factory=list, max_length=500)
    max_cooking_minutes: Annotated[int, Field(strict=True, ge=1, le=240)]
    meals_per_day: Annotated[int, Field(strict=True, ge=1, le=10)]
    price_region: ShortText
    wishes: str = Field(default="", max_length=2000)
