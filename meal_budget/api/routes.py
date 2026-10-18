from typing import Literal

from fastapi import APIRouter, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette._utils import get_route_path

from meal_budget.api.generations import error_response, is_generation_path
from meal_budget.schemas.common import Contract
from meal_budget.schemas.menu import (
    CalculationErrorResponse,
    MenuCalculationRequest,
    MenuCalculationResponse,
)
from meal_budget.schemas.products import ProductCatalog
from meal_budget.services.app_admission import is_app_path
from meal_budget.services.menu_calculation import MenuCalculationError, calculate_menu

router = APIRouter()


class HealthResponse(Contract):
    status: Literal["ok"] = "ok"


@router.get("/health", tags=["technical"])
def health() -> HealthResponse:
    return HealthResponse()


@router.get("/v1/products", tags=["products"], response_model=ProductCatalog)
def products(request: Request) -> ProductCatalog:
    return request.app.state.catalog


async def calculation_validation_error(request: Request, exc: RequestValidationError):
    path = get_route_path(request.scope)
    if is_generation_path(path) or is_app_path(path):
        return error_response(422, "invalid_request", "Некорректная анкета генерации.")
    if path != "/v1/menu-calculations":
        return await request_validation_exception_handler(request, exc)
    return JSONResponse(
        status_code=422,
        content={
            "detail": [
                {
                    "code": error["type"],
                    "field": ".".join(map(str, error["loc"])),
                    "message": error["msg"],
                }
                for error in exc.errors()
            ]
        },
    )


@router.post(
    "/v1/menu-calculations",
    tags=["menu"],
    response_model=MenuCalculationResponse,
    responses={422: {"model": CalculationErrorResponse}},
)
def menu_calculations(menu: MenuCalculationRequest, request: Request):
    try:
        return calculate_menu(menu, request.app.state.catalog)
    except MenuCalculationError as exc:
        return JSONResponse(
            status_code=422,
            content={"detail": [{"code": exc.code, "field": exc.field, "message": exc.message}]},
        )
