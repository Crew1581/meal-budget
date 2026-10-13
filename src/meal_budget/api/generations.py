from typing import Annotated

from fastapi import APIRouter, Request, Security
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette._utils import get_route_path
from starlette.exceptions import HTTPException

from meal_budget.schemas.generation import (
    GenerationHTTPError,
    GenerationHTTPErrorResponse,
    GenerationMetadata,
    GenerationRequest,
    GenerationResult,
)
from meal_budget.services.deepseek import GenerationError

GENERATION_PATH = "/v1/menu-generations"
GUARDED_REQUEST_KEY = "meal_budget.guarded_generation_request"
bearer = HTTPBearer(auto_error=False, scheme_name="GenerationAccessToken")
router = APIRouter()

ERROR_STATUSES = {
    "invalid_request": 422,
    "attempts_exhausted": 422,
    "provider_timeout": 504,
    "generation_timeout": 504,
    "configuration_missing": 503,
    "provider_unavailable": 503,
    "provider_authorization": 503,
    "provider_rate_limit": 503,
    "invalid_response": 502,
}


def error_response(
    status: int, code: str, message: str, metadata: GenerationMetadata | None = None
) -> JSONResponse:
    error = GenerationHTTPErrorResponse(
        detail=GenerationHTTPError(code=code, message=message, metadata=metadata)
    )
    return JSONResponse(
        status_code=status,
        content=error.model_dump(mode="json"),
        headers={"WWW-Authenticate": "Bearer"} if status == 401 else None,
    )


async def generation_http_exception(request: Request, exc: HTTPException):
    # FastAPI использует 400 при RecursionError и других сбоях JSON-парсера.
    if get_route_path(request.scope) == GENERATION_PATH and exc.status_code == 400:
        return error_response(422, "invalid_request", "Некорректная анкета генерации.")
    return await http_exception_handler(request, exc)


@router.post(
    GENERATION_PATH,
    tags=["menu"],
    response_model=GenerationResult,
    responses={
        status: {"model": GenerationHTTPErrorResponse}
        for status in (401, 413, 422, 429, 502, 503, 504)
    },
)
async def menu_generations(
    menu: GenerationRequest,
    request: Request,
    access: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
):
    # HTTPBearer описывает OpenAPI, но сам не проверяет токен. Без guard — fail closed.
    if request.scope.get(GUARDED_REQUEST_KEY) is not request.scope:
        return error_response(401, "unauthorized", "Требуется токен доступа к генерации.")
    try:
        return await request.app.state.generation_service.generate(menu, request.app.state.catalog)
    except GenerationError as exc:
        return error_response(ERROR_STATUSES[exc.code], exc.code, str(exc), exc.metadata)
