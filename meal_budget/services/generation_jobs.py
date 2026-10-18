import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from meal_budget.core.config import Settings
from meal_budget.schemas.generation import GenerationRequest
from meal_budget.schemas.jobs import JobStatus
from meal_budget.schemas.products import ProductCatalog
from meal_budget.services.deepseek import GenerationError
from meal_budget.services.generation_slot import GenerationSlot
from meal_budget.services.job_store import JobStore
from meal_budget.services.menu_generation import MenuGenerationService

logger = logging.getLogger(__name__)


class WorkerUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("Generation worker unavailable")


class GenerationJobs:
    def __init__(
        self,
        store: JobStore,
        generator: MenuGenerationService,
        catalog: ProductCatalog,
        slot: GenerationSlot,
        settings: Settings,
    ):
        self.store, self.generator, self.catalog = store, generator, catalog
        self.slot, self.settings = slot, settings
        self.wake = asyncio.Event()
        self.stopping = False
        self.available = True
        self.task: asyncio.Task | None = None
        self.admission = asyncio.Lock()

    async def start(self) -> None:
        await self.store.recover(self.catalog.catalog_version, datetime.now(UTC))
        await self.cleanup()
        self.task = asyncio.create_task(self.run(), name="generation-worker")

    async def cleanup(self) -> None:
        await self.store.cleanup(datetime.now(UTC), self.settings.generation_jobs_retention_seconds)

    async def submit(self, request: GenerationRequest) -> str | None:
        if not self.available or self.stopping:
            raise WorkerUnavailable
        await self.cleanup()
        # COMMIT приема и переход в unavailable упорядочены одним lock.
        # Cleanup вне него: остановка worker не должна ждать такой POST.
        async with self.admission:
            if not self.available or self.stopping:
                raise WorkerUnavailable
            job_id = await self.store.create(
                request,
                self.catalog.catalog_version,
                self.settings.generation_jobs_queue_limit,
                datetime.now(UTC),
            )
            if job_id is not None:
                self.wake.set()
            return job_id

    async def get(self, job_id: str) -> JobStatus | None:
        await self.cleanup()
        job = await self.store.get(job_id)
        # Между async cleanup и SELECT могла завершиться запись уже истекшего result.
        if (
            job is not None
            and job.finished_at is not None
            and job.finished_at
            <= datetime.now(UTC)
            - timedelta(seconds=self.settings.generation_jobs_retention_seconds)
        ):
            await self.cleanup()
            return None
        if (
            job is not None
            and job.state in ("queued", "running")
            and (not self.available or self.stopping)
        ):
            raise WorkerUnavailable
        return job

    async def stop(self) -> None:
        async with self.admission:
            self.stopping = True
            self.available = False
        # Не удерживаем admission при отмене/ожидании worker: его finally нужен lock.
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        # Отмена могла прийти после COMMIT claim, но до получения RETURNING.
        # При недоступной БД startup-recovery завершит этот переход позднее.
        try:
            await self.store.interrupt_running(datetime.now(UTC))
        except Exception:
            logger.error("worker_shutdown_storage_unavailable")

    async def run(self) -> None:
        try:
            while not self.stopping:
                # Сбрасываем ДО async SQL: иначе POST во время SELECT потеряет wakeup.
                self.wake.clear()
                await self.cleanup()
                if not await self.store.has_queued():
                    with suppress(TimeoutError):
                        await asyncio.wait_for(
                            self.wake.wait(), self.settings.generation_jobs_cleanup_interval
                        )
                    continue
                await self.slot.acquire()
                row = None
                try:
                    row = await self.store.claim_next(datetime.now(UTC))
                    if row is None:
                        continue
                    if row["catalog_version"] != self.catalog.catalog_version:
                        await self.store.fail(
                            row["id"], "catalog_version_mismatch", datetime.now(UTC)
                        )
                        continue
                    # Claim уже закоммичен, соединение и транзакция освобождены.
                    request = GenerationRequest.model_validate(row["request_json"])
                    result = await self.generator.generate(request, self.catalog)
                    if result.catalog_version != row["catalog_version"]:
                        await self.store.fail(
                            row["id"], "catalog_version_mismatch", datetime.now(UTC)
                        )
                    else:
                        await self.store.complete(row["id"], result, datetime.now(UTC))
                except asyncio.CancelledError:
                    if row is not None:
                        try:
                            await self.store.fail(row["id"], "interrupted", datetime.now(UTC))
                        except Exception:
                            # Сбой записи не должен подменить исходную отмену.
                            logger.error("worker_interruption_storage_unavailable")
                    raise
                except GenerationError as exc:
                    await self.store.fail(row["id"], exc.code, datetime.now(UTC))
                except Exception:
                    if row is None:
                        raise
                    await self.store.fail(row["id"], "internal_error", datetime.now(UTC))
                finally:
                    self.slot.release()
        except Exception:
            # Отказ сообщается в finally, без текста SQL-исключения и автоповторов.
            pass
        finally:
            async with self.admission:
                self.available = False
                if not self.stopping:
                    logger.error("worker_unavailable")
