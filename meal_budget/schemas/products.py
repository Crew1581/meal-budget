from datetime import date
from typing import Literal

from pydantic import Field, model_validator

from meal_budget.schemas.common import (
    Contract,
    Money,
    Nutrition,
    PositiveNumber,
    ProductId,
    ShortText,
)


class Product(Contract):
    id: ProductId
    name: ShortText
    state: Literal["dry", "raw", "cooked"]
    nutrition_per_100g: Nutrition
    package_size_g: PositiveNumber
    price: Money
    price_region: ShortText
    price_source: ShortText
    price_updated_at: date


class ProductCatalog(Contract):
    data_status: Literal["demo_unverified"]
    warning: str = Field(min_length=1, max_length=500)
    products: list[Product] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def unique_ids(self) -> "ProductCatalog":
        ids = [product.id for product in self.products]
        if len(set(ids)) != len(ids):
            raise ValueError("product id must be unique")
        return self
