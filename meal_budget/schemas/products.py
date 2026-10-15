from datetime import date
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, HttpUrl, model_validator

from meal_budget.schemas.common import (
    Contract,
    Money,
    Nutrition,
    PositiveNumber,
    ProductId,
    ShortText,
)


class Source(Contract):
    url: HttpUrl
    checked_at: AwareDatetime
    observation_method: Literal["unspecified", "direct_page", "web_tool"] = "unspecified"
    web_cache_age: ShortText | None = None

    @model_validator(mode="after")
    def cache_observation(self) -> "Source":
        if self.web_cache_age is not None and self.observation_method != "web_tool":
            raise ValueError("cache age requires a web tool observation")
        return self


class PriceEvidence(Source):
    channel: Literal["online_delivery", "offline_store"]
    region_confirmed: bool = Field(strict=True)
    region: ShortText | None
    store: ShortText | None
    price_type: Literal["regular", "promotional", "displayed", "unclear"]
    single_package_without_card: Literal[True] | None
    conditions: str | None = Field(default=None, max_length=500)
    valid_from: date | None = None
    valid_until: date | None = None

    @model_validator(mode="after")
    def usable_price(self) -> "PriceEvidence":
        if self.region_confirmed != (self.region is not None):
            raise ValueError("region must only be set when confirmed")
        if self.store is not None and not self.region_confirmed:
            raise ValueError("store requires confirmed region")
        if self.channel == "offline_store" and self.store is None:
            raise ValueError("offline price requires a verified store")
        if self.price_type == "unclear":
            raise ValueError("unclear prices belong to candidates, not active products")
        if self.price_type == "displayed":
            if self.single_package_without_card is not None or not self.conditions:
                raise ValueError(
                    "displayed price requires unknown eligibility and observation notes"
                )
            if self.channel != "online_delivery":
                raise ValueError("displayed price is an online observation, not an offline price")
        elif self.single_package_without_card is not True:
            raise ValueError(
                "regular/promotional price requires confirmed single-package eligibility"
            )
        if self.price_type == "promotional" and not self.conditions:
            raise ValueError("promotional price requires published conditions")
        if self.valid_from and self.valid_until and self.valid_until < self.valid_from:
            raise ValueError("invalid promotion period")
        return self


class WebProvenance(Contract):
    kind: Literal["web"]
    chain: Literal["Пятёрочка"]
    product_url: HttpUrl
    retailer_product_id: ShortText | None
    nutrition: Source
    price: PriceEvidence

    @model_validator(mode="after")
    def official_card(self) -> "WebProvenance":
        for url in (self.product_url, self.price.url):
            if url.scheme != "https" or url.host not in {"5ka.ru", "www.5ka.ru"}:
                raise ValueError("product and price must reference official 5ka.ru pages")
            if not url.path or not url.path.startswith("/product/"):
                raise ValueError("product card URL required")
        if self.price.url != self.product_url:
            raise ValueError("price must reference the same product card")
        return self


class SyntheticProvenance(Contract):
    # Явные demo-данные и автономные fixtures; это не собранные товары сети.
    kind: Literal["synthetic_fixture", "synthetic_demo"]
    note: ShortText


class Product(Contract):
    id: ProductId
    name: ShortText
    state: Literal["dry", "raw", "cooked"]
    state_basis: Literal["physical_state", "as_sold"] = "physical_state"
    nutrition_per_100g: Nutrition
    package_size_g: PositiveNumber
    price: Money
    provenance: Annotated[WebProvenance | SyntheticProvenance, Field(discriminator="kind")]
    # Совместимость чтения MEAL-3; authoritative данные находятся в provenance.
    price_region: ShortText | None = Field(default=None, deprecated=True)
    price_source: str | None = Field(default=None, max_length=2083, deprecated=True)
    price_updated_at: date | None = Field(default=None, deprecated=True)

    @model_validator(mode="after")
    def legacy_price_fields(self) -> "Product":
        if isinstance(self.provenance, WebProvenance):
            region = self.provenance.price.region
            source = str(self.provenance.price.url)
            price = self.provenance.price
            updated_at = (
                None
                if price.observation_method == "web_tool" or price.price_type == "displayed"
                else price.checked_at.date()
            )
        else:
            region, source, updated_at = None, self.provenance.note, None
        for key, value in (
            ("price_region", region),
            ("price_source", source),
            ("price_updated_at", updated_at),
        ):
            supplied = self.__dict__[key]
            if supplied is not None and supplied != value:
                raise ValueError("legacy price field contradicts provenance")
            setattr(self, key, value)
        return self


class ProductCatalog(Contract):
    catalog_version: ShortText
    data_status: Literal["demo_unverified"]
    warning: str = Field(min_length=1, max_length=500)
    products: list[Product] = Field(max_length=1000)

    @model_validator(mode="after")
    def unique_ids(self) -> "ProductCatalog":
        ids = [product.id for product in self.products]
        if len(set(ids)) != len(ids):
            raise ValueError("product id must be unique")
        return self
