from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Request, Security
from fastapi.security import HTTPAuthorizationCredentials

from meal_budget.api.generations import GUARDED_REQUEST_KEY, JOBS_PATH, bearer, error_response
from meal_budget.schemas.generation import GenerationHTTPErrorResponse, GenerationRequest
from meal_budget.schemas.jobs import JobAccepted, JobStatus
from meal_budget.services.generation_jobs import WorkerUnavailable

router = APIRouter()
Access = Annotated[HTTPAuthorizationCredentials | None, Security(bearer)]
ERRORS = {
    status: {"model": GenerationHTTPErrorResponse} for status in (401, 404, 413, 422, 429, 503)
}


def unauthorized(request: Request) -> bool:
    return request.scope.get(GUARDED_REQUEST_KEY) is not request.scope


def storage_unavailable():
    return error_response(503, "job_storage_unavailable", "Хранилище заданий недоступно.")


def worker_unavailable():
    return error_response(503, "worker_unavailable", "Обработчик заданий недоступен.")


@router.post(
    JOBS_PATH, status_code=202, response_model=JobAccepted, tags=["menu"], responses=ERRORS
)
async def create_job(menu: GenerationRequest, request: Request, access: Access):
    if unauthorized(request):
        return error_response(401, "unauthorized", "Требуется токен доступа к генерации.")
    try:
        job_id = await request.app.state.generation_jobs.submit(menu)
    except WorkerUnavailable:
        return worker_unavailable()
    except Exception:
        return storage_unavailable()
    if job_id is None:
        return error_response(429, "generation_queue_full", "Очередь генерации заполнена.")
    return JobAccepted(id=job_id)


@router.get(JOBS_PATH + "/{job_id}", response_model=JobStatus, tags=["menu"], responses=ERRORS)
async def get_job(job_id: UUID, request: Request, access: Access):
    if unauthorized(request):
        return error_response(401, "unauthorized", "Требуется токен доступа к генерации.")
    try:
        job = await request.app.state.generation_jobs.get(str(job_id))
    except WorkerUnavailable:
        return worker_unavailable()
    except Exception:
        return storage_unavailable()
    if job is None:
        return error_response(404, "job_not_found", "Задание не найдено или срок хранения истек.")
    return job
