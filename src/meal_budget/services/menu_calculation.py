from decimal import ROUND_HALF_UP, Decimal, localcontext
from math import lcm

from meal_budget.schemas.menu import (
    CalculatedDay,
    CalculatedMeal,
    CalculatedNutrition,
    MenuCalculationRequest,
    MenuCalculationResponse,
    ShoppingItem,
)
from meal_budget.schemas.products import ProductCatalog

PRECISION = 80
CENT = Decimal("0.01")
NUTRIENTS = ("calories_kcal", "protein_g", "fat_g", "carbs_g")


class MenuCalculationError(ValueError):
    def __init__(self, code: str, field: str, message: str):
        super().__init__(message)
        self.code = code
        self.field = field
        self.message = message


def rounded(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def nutrition(values: dict[str, Decimal], denominator: int) -> CalculatedNutrition:
    return CalculatedNutrition(
        **{key: rounded(value / denominator) for key, value in values.items()}
    )


def calculate_menu(
    menu: MenuCalculationRequest, catalog: ProductCatalog
) -> MenuCalculationResponse:
    """Расчет без HTTP, изменения меню и округления промежуточных значений."""
    products = {product.id: product for product in catalog.products}
    recipes = {recipe.id: recipe for recipe in menu.recipes}
    for i, recipe in enumerate(menu.recipes):
        for j, ingredient in enumerate(recipe.ingredients):
            if ingredient.product_id not in products:
                raise MenuCalculationError(
                    "unknown_product_id",
                    f"recipes.{i}.ingredients.{j}.product_id",
                    f"Неизвестный product_id: {ingredient.product_id}",
                )
    for i, day in enumerate(menu.days):
        for j, meal in enumerate(day.meals):
            if meal.recipe_id not in recipes:
                raise MenuCalculationError(
                    "unknown_recipe_id",
                    f"days.{i}.meals.{j}.recipe_id",
                    f"Неизвестный recipe_id: {meal.recipe_id}",
                )
    used = {meal.recipe_id for day in menu.days for meal in day.meals}
    for i, recipe in enumerate(menu.recipes):
        if recipe.id not in used:
            raise MenuCalculationError(
                "unused_recipe", f"recipes.{i}.id", f"Рецепт {recipe.id} не используется"
            )

    with localcontext() as context:
        context.prec = PRECISION
        context.rounding = ROUND_HALF_UP
        recipe_nutrition = {}
        for recipe in sorted(menu.recipes, key=lambda item: item.id):
            values = dict.fromkeys(NUTRIENTS, Decimal(0))
            for ingredient in sorted(recipe.ingredients, key=lambda item: item.product_id):
                product = products[ingredient.product_id]
                for key in NUTRIENTS:
                    values[key] += ingredient.grams * getattr(product.nutrition_per_100g, key) / 100
            recipe_nutrition[recipe.id] = values

        # Общий целый знаменатель исключает погрешность 1/3 при ceil упаковок.
        denominator = lcm(*(recipe.servings for recipe in menu.recipes))
        requirements: dict[str, Decimal] = {}
        total = dict.fromkeys(NUTRIENTS, Decimal(0))
        days = []
        for day in menu.days:
            day_values = dict.fromkeys(NUTRIENTS, Decimal(0))
            meals = []
            for meal in day.meals:
                recipe = recipes[meal.recipe_id]
                values = {
                    key: value * meal.servings * (denominator // recipe.servings)
                    for key, value in recipe_nutrition[recipe.id].items()
                }
                for key in NUTRIENTS:
                    day_values[key] += values[key]
                meals.append(
                    CalculatedMeal(
                        recipe_id=meal.recipe_id,
                        servings=meal.servings,
                        nutrition=nutrition(values, denominator),
                    )
                )
                for ingredient in sorted(recipe.ingredients, key=lambda item: item.product_id):
                    numerator = ingredient.grams * meal.servings * (denominator // recipe.servings)
                    requirements[ingredient.product_id] = (
                        requirements.get(ingredient.product_id, Decimal(0)) + numerator
                    )
            for key in NUTRIENTS:
                total[key] += day_values[key]
            days.append(
                CalculatedDay(
                    day=day.day, meals=meals, nutrition=nutrition(day_values, denominator)
                )
            )

        shopping = []
        total_cost = Decimal(0)
        for product_id, numerator in sorted(requirements.items()):
            product = products[product_id]
            divisor = product.package_size_g * denominator
            whole, remainder = divmod(numerator, divisor)
            packages = int(whole) + int(remainder != 0)
            required = numerator / denominator
            purchased = packages * product.package_size_g
            cost = packages * product.price
            total_cost += cost
            shopping.append(
                ShoppingItem(
                    product_id=product_id,
                    name=product.name,
                    required_grams=rounded(required),
                    package_size_g=rounded(product.package_size_g),
                    packages=packages,
                    purchased_grams=rounded(purchased),
                    remaining_grams=rounded(purchased - required),
                    price=rounded(product.price),
                    cost=rounded(cost),
                )
            )
        return MenuCalculationResponse(
            data_status=catalog.data_status,
            warning=catalog.warning,
            days=days,
            nutrition=nutrition(total, denominator),
            shopping_list=shopping,
            budget=menu.budget,
            total_cost=rounded(total_cost),
            within_budget=total_cost <= menu.budget,
            budget_remaining=rounded(menu.budget - total_cost),
            over_budget_by=rounded(max(total_cost - menu.budget, Decimal(0))),
        )
