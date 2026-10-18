"""Сборка FastAPI-приложения и владение ресурсами через lifespan."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException

from meal_budget.api.app_jobs import router as app_router
from meal_budget.api.generation_guard import GenerationGuard
from meal_budget.api.generations import generation_http_exception
from meal_budget.api.generations import router as generation_router
from meal_budget.api.jobs import router as jobs_router
from meal_budget.api.routes import calculation_validation_error, router
from meal_budget.catalog import load_catalog
from meal_budget.core.config import Settings
from meal_budget.services.deepseek import DeepSeekProvider
from meal_budget.services.generation_jobs import GenerationJobs
from meal_budget.services.generation_slot import GenerationSlot
from meal_budget.services.job_store import JobStore
from meal_budget.services.menu_generation import MenuGenerationService


def create_app(settings: Settings | None = None) -> FastAPI:
    """Создает приложение; каталог и клиент провайдера открываются только в lifespan."""
    app_settings = settings if settings is not None else Settings()
    token = app_settings.generation_access_token.get_secret_value()
    slot = GenerationSlot()
    if app_settings.generation_app_enabled and not app_settings.generation_http_enabled:
        raise RuntimeError("GENERATION_APP_ENABLED требует GENERATION_HTTP_ENABLED=true.")
    if app_settings.generation_http_enabled and (
        not token or any(not 33 <= ord(character) <= 126 for character in token)
    ):
        raise RuntimeError(
            "GENERATION_HTTP_ENABLED требует непустой GENERATION_ACCESS_TOKEN "
            "из печатных ASCII-символов без пробелов."
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.catalog = load_catalog(app_settings.catalog_path)
        app.state.generation_service = None
        app.state.generation_jobs = None
        try:
            if app_settings.generation_http_enabled:
                try:
                    store = await JobStore.open(app_settings)
                except Exception:
                    raise RuntimeError(
                        "PostgreSQL заданий недоступен или схема отсутствует. "
                        "Проверьте подключение и выполните миграции перед запуском API."
                    ) from None
                try:
                    async with DeepSeekProvider(app_settings) as provider:
                        app.state.generation_service = MenuGenerationService(provider, app_settings)
                        jobs = GenerationJobs(
                            store,
                            app.state.generation_service,
                            app.state.catalog,
                            slot,
                            app_settings,
                        )
                        app.state.generation_jobs = jobs
                        try:
                            await jobs.start()
                        except Exception:
                            raise RuntimeError(
                                "Не удалось восстановить задания PostgreSQL."
                            ) from None
                        try:
                            yield
                        finally:
                            await jobs.stop()
                finally:
                    await store.close()
            else:
                yield
        finally:
            app.state.generation_service = None
            app.state.generation_jobs = None
            app.state.catalog = None

    application = FastAPI(title="Meal Budget API", version="0.1.0", lifespan=lifespan)
    application.state.settings = app_settings
    application.include_router(router)
    if app_settings.generation_http_enabled:
        application.include_router(generation_router)
        application.include_router(jobs_router)
        if app_settings.generation_app_enabled:
            application.include_router(app_router)
        application.add_middleware(
            GenerationGuard, token=token, slot=slot, app_enabled=app_settings.generation_app_enabled
        )
        application.add_exception_handler(HTTPException, generation_http_exception)
    application.add_exception_handler(RequestValidationError, calculation_validation_error)
    return application
