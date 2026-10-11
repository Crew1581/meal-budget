from typing import Literal

from fastapi import APIRouter, Request

from meal_budget.schemas.common import Contract
from meal_budget.schemas.products import ProductCatalog

router = APIRouter()


class HealthResponse(Contract):
    status: Literal["ok"] = "ok"


@router.get("/health", tags=["technical"])
def health() -> HealthResponse:
    return HealthResponse()


@router.get("/v1/products", tags=["products"], response_model=ProductCatalog)
def products(request: Request) -> ProductCatalog:
    return request.app.state.catalog
