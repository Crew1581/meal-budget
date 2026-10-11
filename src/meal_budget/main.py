from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from meal_budget.api.routes import calculation_validation_error, router
from meal_budget.catalog import load_catalog
from meal_budget.core.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.catalog = load_catalog(app_settings.catalog_path)
        yield
        app.state.catalog = None

    application = FastAPI(title="Meal Budget API", version="0.1.0", lifespan=lifespan)
    application.include_router(router)
    application.add_exception_handler(RequestValidationError, calculation_validation_error)
    return application


app = create_app()
