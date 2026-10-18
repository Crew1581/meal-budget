import secrets
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from meal_budget.api.generations import GUARDED_REQUEST_KEY, error_response
from meal_budget.api.jobs import storage_unavailable, worker_unavailable
from meal_budget.schemas.generation import GenerationHTTPErrorResponse, GenerationRequest
from meal_budget.schemas.installations import InstallationCreated
from meal_budget.schemas.jobs import JobAccepted, JobStatus
from meal_budget.services.app_admission import (
    APP_JOBS_PATH,
    IDEMPOTENCY_KEY,
    INSTALLATION_SCOPE_KEY,
    INSTALLATIONS_PATH,
    AdmissionError,
    token_hash,
)
from meal_budget.services.generation_jobs import WorkerUnavailable

router = APIRouter()
installation_bearer = HTTPBearer(auto_error=False, scheme_name="InstallationToken")
Access = Annotated[HTTPAuthorizationCredentials | None, Security(installation_bearer)]
ERRORS = {
    status: {"model": GenerationHTTPErrorResponse} for status in (401, 404, 409, 413, 422, 429, 503)
}
MESSAGES = {
    "idempotency_conflict": "Ключ уже использован с другой анкетой.",
    "generation_queue_full": "Очередь генерации заполнена.",
    "app_daily_quota_exceeded": "Общая суточная квота заданий исчерпана.",
    "installation_daily_job_quota_exceeded": "Суточная квота заданий установки исчерпана.",
    "installation_daily_quota_exceeded": "Суточная квота создания установок исчерпана.",
    "installation_capacity_reached": "Регистрация установок временно недоступна.",
}


def admission_error(exc: AdmissionError):
    if exc.code == "worker_unavailable":
        return worker_unavailable()
    response = error_response(
        409 if exc.code == "idempotency_conflict" else 429, exc.code, MESSAGES[exc.code]
    )
    if exc.retry_after is not None:
        response.headers["Retry-After"] = str(exc.retry_after)
    return response


def authorized(request: Request) -> bool:
    return request.scope.get(GUARDED_REQUEST_KEY) is request.scope


@router.post(
    INSTALLATIONS_PATH,
    status_code=201,
    response_model=InstallationCreated,
    tags=["app"],
    responses=ERRORS,
)
async def register_installation(request: Request):
    if not authorized(request):
        return error_response(401, "unauthorized", "Запрос не прошел проверку доступа.")
    token = "mbi_" + secrets.token_urlsafe(32)
    # Даже случайное совпадение не должно смешивать виды авторизации.
    while secrets.compare_digest(
        token, request.app.state.settings.generation_access_token.get_secret_value()
    ):
        token = "mbi_" + secrets.token_urlsafe(32)
    try:
        installation_id = await request.app.state.generation_jobs.store.register_installation(
            token_hash(token), request.app.state.settings
        )
    except AdmissionError as exc:
        return admission_error(exc)
    except Exception:
        return storage_unavailable()
    return InstallationCreated(installation_id=installation_id, token=token)


@router.post(
    APP_JOBS_PATH, status_code=202, response_model=JobAccepted, tags=["app"], responses=ERRORS
)
async def create_job(
    menu: GenerationRequest,
    request: Request,
    access: Access,
    idempotency_key: Annotated[str, Header(description="1–64 ASCII: A–Z, a–z, 0–9, _ и -.")],
):
    installation_id = request.scope.get(INSTALLATION_SCOPE_KEY)
    if not authorized(request) or installation_id is None:
        return error_response(401, "unauthorized", "Требуется токен установки.")
    if (
        IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None
        or len(request.headers.getlist("idempotency-key")) != 1
    ):
        return error_response(
            422,
            "invalid_idempotency_key",
            "Требуется один Idempotency-Key: 1–64 ASCII-буквы, цифры, _ или -.",
        )
    try:
        job_id = await request.app.state.generation_jobs.submit_app(
            menu, installation_id, idempotency_key
        )
    except AdmissionError as exc:
        return admission_error(exc)
    except Exception:
        return storage_unavailable()
    return JobAccepted(id=job_id)


@router.get(APP_JOBS_PATH + "/{job_id}", response_model=JobStatus, tags=["app"], responses=ERRORS)
async def get_job(job_id: UUID, request: Request, access: Access):
    installation_id = request.scope.get(INSTALLATION_SCOPE_KEY)
    if not authorized(request) or installation_id is None:
        return error_response(401, "unauthorized", "Требуется токен установки.")
    try:
        job = await request.app.state.generation_jobs.get_app(str(job_id), installation_id)
    except WorkerUnavailable:
        return worker_unavailable()
    except Exception:
        return storage_unavailable()
    if job is None:
        return error_response(404, "job_not_found", "Задание не найдено или срок хранения истек.")
    return job
