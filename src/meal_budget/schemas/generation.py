from decimal import Decimal
from typing import Annotated

from pydantic import Field

from meal_budget.schemas.common import Contract, Money, PositiveNumber, ProductId, ShortText
from meal_budget.schemas.menu import (
    MenuCalculationRequest,
    MenuCalculationResponse,
    MenuDay,
    Recipe,
)


class DailyTargets(Contract):
    calories_kcal: PositiveNumber
    protein_g: PositiveNumber
    fat_g: PositiveNumber
    carbs_g: PositiveNumber


class GenerationRequest(Contract):
    """Запрос сервиса: технические пределы, не медицинские рекомендации."""

    days: Annotated[int, Field(strict=True, ge=1, le=31)] = 7
    budget: Money
    daily_targets: DailyTargets
    excluded_product_ids: list[ProductId] = Field(default_factory=list, max_length=500)
    disliked_product_ids: list[ProductId] = Field(default_factory=list, max_length=500)
    max_cooking_minutes: Annotated[int, Field(strict=True, ge=1, le=240)]
    meals_per_day: Annotated[int, Field(strict=True, ge=1, le=10)]
    price_region: ShortText
    wishes: str = Field(default="", max_length=2000)


class GeneratedRecipe(Recipe):
    steps: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        min_length=1, max_length=20
    )
    cooking_minutes: int = Field(strict=True, ge=1, le=240)


class GeneratedPlan(Contract):
    recipes: list[GeneratedRecipe] = Field(min_length=1, max_length=100)
    days: list[MenuDay] = Field(min_length=1, max_length=31)

    def calculation_request(self, budget: Decimal) -> MenuCalculationRequest:
        return MenuCalculationRequest(
            recipes=[
                Recipe.model_validate(recipe.model_dump(exclude={"steps", "cooking_minutes"}))
                for recipe in self.recipes
            ],
            days=self.days,
            budget=budget,
        )


class CallMetadata(Contract):
    model: str | None = None
    usage: dict[str, int] | None = None


class GenerationMetadata(Contract):
    calls: int
    duration_seconds: float
    responses: list[CallMetadata]


class GenerationResult(Contract):
    plan: GeneratedPlan
    calculation: MenuCalculationResponse
    warnings: list[str]
    nutrition_tolerance: Decimal
    metadata: GenerationMetadata
