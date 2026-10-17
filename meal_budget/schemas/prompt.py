"""Представление товара только для модели; исходный каталог не изменяется."""

from typing import Literal

from meal_budget.schemas.common import (
    Contract,
    Money,
    Nutrition,
    PositiveNumber,
    ProductId,
    ShortText,
)
from meal_budget.schemas.products import Product


class PromptProduct(Contract):
    id: ProductId
    name: ShortText
    state: Literal["dry", "raw", "cooked"]
    state_basis: Literal["physical_state", "as_sold"]
    nutrition_per_100g: Nutrition
    package_size_g: PositiveNumber
    price: Money

    @classmethod
    def from_product(cls, product: Product) -> "PromptProduct":
        return cls(
            id=product.id,
            name=product.name,
            state=product.state,
            state_basis=product.state_basis,
            nutrition_per_100g=product.nutrition_per_100g,
            package_size_g=product.package_size_g,
            price=product.price,
        )
