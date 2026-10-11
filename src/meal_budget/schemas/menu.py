from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, PlainSerializer, model_validator
from pydantic_core import PydanticCustomError

from meal_budget.schemas.common import Contract, Money, ProductId, ShortText

Quantity = Annotated[
    Decimal,
    Field(gt=0, le=100_000, max_digits=12, decimal_places=6, allow_inf_nan=False),
]
MealServings = Annotated[
    Decimal, Field(gt=0, le=100, max_digits=9, decimal_places=6, allow_inf_nan=False)
]
OutputNumber = Annotated[
    Decimal,
    Field(allow_inf_nan=False),
    PlainSerializer(float, return_type=float, when_used="json"),
]


class Ingredient(Contract):
    product_id: ProductId
    grams: Quantity


class Recipe(Contract):
    id: ProductId
    name: ShortText
    servings: int = Field(strict=True, ge=1, le=100)
    ingredients: list[Ingredient] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_products(self) -> "Recipe":
        seen = set()
        for ingredient in self.ingredients:
            if ingredient.product_id in seen:
                raise PydanticCustomError(
                    "duplicate_product_id",
                    "Повтор product_id {id} в ingredients рецепта",
                    {"id": ingredient.product_id},
                )
            seen.add(ingredient.product_id)
        return self


class Meal(Contract):
    recipe_id: ProductId
    servings: MealServings


class MenuDay(Contract):
    day: int = Field(strict=True, ge=1, le=31)
    meals: list[Meal] = Field(min_length=1, max_length=10)


class MenuCalculationRequest(Contract):
    recipes: list[Recipe] = Field(min_length=1, max_length=100)
    days: list[MenuDay] = Field(min_length=1, max_length=31)
    budget: Money

    @model_validator(mode="after")
    def validate_menu(self) -> "MenuCalculationRequest":
        ids = set()
        for recipe in self.recipes:
            if recipe.id in ids:
                raise PydanticCustomError(
                    "duplicate_recipe_id", "Повтор recipes.id {id}", {"id": recipe.id}
                )
            ids.add(recipe.id)
        if [day.day for day in self.days] != list(range(1, len(self.days) + 1)):
            raise PydanticCustomError(
                "invalid_day_sequence", "days.day должны идти последовательно от 1"
            )
        return self


class CalculatedNutrition(Contract):
    calories_kcal: OutputNumber
    protein_g: OutputNumber
    fat_g: OutputNumber
    carbs_g: OutputNumber


class CalculatedMeal(Contract):
    recipe_id: ProductId
    servings: OutputNumber
    nutrition: CalculatedNutrition


class CalculatedDay(Contract):
    day: int
    meals: list[CalculatedMeal]
    nutrition: CalculatedNutrition


class ShoppingItem(Contract):
    product_id: ProductId
    name: ShortText
    required_grams: OutputNumber
    package_size_g: OutputNumber
    packages: int
    purchased_grams: OutputNumber
    remaining_grams: OutputNumber
    price: OutputNumber
    cost: OutputNumber


class MenuCalculationResponse(Contract):
    data_status: Literal["demo_unverified"]
    warning: str
    days: list[CalculatedDay]
    nutrition: CalculatedNutrition
    shopping_list: list[ShoppingItem]
    budget: OutputNumber
    total_cost: OutputNumber
    within_budget: bool
    budget_remaining: OutputNumber
    over_budget_by: OutputNumber


class CalculationErrorDetail(Contract):
    code: str
    field: str
    message: str


class CalculationErrorResponse(Contract):
    detail: list[CalculationErrorDetail]
