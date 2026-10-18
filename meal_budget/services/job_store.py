"""PostgreSQL заданий: короткие транзакции, без HTTP и автоматических миграций."""

import json
from datetime import datetime, timedelta
from math import ceil
from uuid import UUID, uuid4

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from meal_budget.core.config import Settings
from meal_budget.schemas.generation import GenerationRequest, GenerationResult
from meal_budget.schemas.jobs import JobStatus
from meal_budget.services.deepseek import ERROR_MESSAGES

SAFE_JOB_ERRORS = frozenset(ERROR_MESSAGES) | {
    "interrupted",
    "catalog_version_mismatch",
    "internal_error",
}
# Только admission: одинаковый transaction-scoped lock для count + INSERT.
QUEUE_LOCK = 0x4D45414C38


def database_parameters(url: str) -> dict[str, str]:
    if not url.startswith(("postgresql://", "postgres://")):
        raise ValueError("PostgreSQL URL required")
    return conninfo_to_dict(url)


def encode(model: GenerationRequest | GenerationResult) -> dict:
    # JSONB содержит точные Decimal-строки; транспортный float здесь не применяется.
    return json.loads(json.dumps(model.model_dump(mode="python"), ensure_ascii=False, default=str))


class SafeConnection(psycopg.AsyncConnection):
    """Фоновые сообщения пула не должны содержать сырой libpq error/DSN."""

    @classmethod
    async def connect(cls, *args, **kwargs):
        try:
            return await super().connect(*args, **kwargs)
        except Exception:
            raise psycopg.OperationalError("PostgreSQL connection unavailable") from None

    def __repr__(self):
        return "<JobDatabaseConnection>"


class JobStore:
    def __init__(self, pool: AsyncConnectionPool):
        self.pool = pool

    @classmethod
    async def open(cls, settings: Settings) -> "JobStore":
        params = database_parameters(settings.generation_jobs_database_url.get_secret_value())
        timeout = settings.generation_jobs_db_timeout
        params.update(
            connect_timeout=ceil(timeout),
            options=f"-c timezone=UTC -c statement_timeout={ceil(timeout * 1000)} "
            f"-c lock_timeout={ceil(timeout * 1000)}",
        )
        pool = AsyncConnectionPool(
            kwargs={**params, "autocommit": True, "row_factory": dict_row},
            connection_class=SafeConnection,
            min_size=1,
            max_size=settings.generation_jobs_pool_size,
            max_waiting=16,
            timeout=timeout,
            reconnect_timeout=timeout,
            num_workers=1,
            name="generation-jobs",
            check=AsyncConnectionPool.check_connection,
            open=False,
        )
        try:
            await pool.open(wait=True, timeout=timeout)
            async with pool.connection() as connection:
                await connection.execute(
                    "SELECT id, state, created_at, started_at, finished_at, request_json, "
                    "catalog_version, result_json, error_code FROM generation_jobs LIMIT 0"
                )
        except BaseException:
            await pool.close()
            raise
        return cls(pool)

    async def close(self) -> None:
        await self.pool.close()

    async def create(
        self, request: GenerationRequest, version: str, limit: int, now: datetime
    ) -> str | None:
        async with self.pool.connection() as connection, connection.transaction():
            await connection.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            await connection.execute("SELECT pg_advisory_xact_lock(%s)", (QUEUE_LOCK,))
            cursor = await connection.execute(
                "SELECT count(*) AS count FROM generation_jobs WHERE state = 'queued'"
            )
            if (await cursor.fetchone())["count"] >= limit:
                return None
            job_id = uuid4()
            await connection.execute(
                "INSERT INTO generation_jobs "
                "(id, state, created_at, request_json, catalog_version) "
                "VALUES (%s, 'queued', %s, %s, %s)",
                (job_id, now, Jsonb(encode(request)), version),
            )
        return str(job_id)

    async def has_queued(self) -> bool:
        async with self.pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT EXISTS (SELECT 1 FROM generation_jobs WHERE state = 'queued') AS queued"
            )
            return (await cursor.fetchone())["queued"]

    async def claim_next(self, now: datetime) -> dict | None:
        async with self.pool.connection() as connection, connection.transaction():
            cursor = await connection.execute(
                "WITH candidate AS ("
                "SELECT id FROM generation_jobs WHERE state = 'queued' "
                "ORDER BY created_at, id FOR UPDATE SKIP LOCKED LIMIT 1"
                ") UPDATE generation_jobs AS job SET state = 'running', started_at = %s "
                "FROM candidate WHERE job.id = candidate.id AND job.state = 'queued' "
                "RETURNING job.*",
                (now,),
            )
            return await cursor.fetchone()

    async def complete(self, job_id: str | UUID, result: GenerationResult, now: datetime) -> None:
        async with self.pool.connection() as connection, connection.transaction():
            await connection.execute(
                "UPDATE generation_jobs SET state = 'completed', "
                "result_json = %s, finished_at = %s "
                "WHERE id = %s AND state = 'running'",
                (Jsonb(encode(result)), now, UUID(str(job_id))),
            )

    async def fail(self, job_id: str | UUID, code: str, now: datetime) -> None:
        async with self.pool.connection() as connection, connection.transaction():
            await connection.execute(
                "UPDATE generation_jobs SET state = 'failed', error_code = %s, finished_at = %s "
                "WHERE id = %s AND state IN ('queued', 'running')",
                (code if code in SAFE_JOB_ERRORS else "internal_error", now, UUID(str(job_id))),
            )

    async def interrupt_running(self, now: datetime) -> None:
        async with self.pool.connection() as connection, connection.transaction():
            await connection.execute(
                "UPDATE generation_jobs SET state = 'failed', error_code = 'interrupted', "
                "finished_at = %s WHERE state = 'running'",
                (now,),
            )

    async def recover(self, version: str, now: datetime) -> None:
        async with self.pool.connection() as connection, connection.transaction():
            await connection.execute(
                "UPDATE generation_jobs SET state = 'failed', error_code = 'interrupted', "
                "finished_at = %s WHERE state = 'running'",
                (now,),
            )
            await connection.execute(
                "UPDATE generation_jobs SET state = 'failed', "
                "error_code = 'catalog_version_mismatch', finished_at = %s "
                "WHERE state = 'queued' AND catalog_version != %s",
                (now, version),
            )

    async def cleanup(self, now: datetime, retention: float) -> None:
        async with self.pool.connection() as connection, connection.transaction():
            await connection.execute(
                "DELETE FROM generation_jobs WHERE state IN ('completed', 'failed') "
                "AND finished_at <= %s",
                (now - timedelta(seconds=retention),),
            )

    async def get(self, job_id: str) -> JobStatus | None:
        async with self.pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM generation_jobs WHERE id = %s", (UUID(job_id),)
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return JobStatus(
            id=str(row["id"]),
            state=row["state"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            catalog_version=row["catalog_version"],
            result=GenerationResult.model_validate(row["result_json"])
            if row["state"] == "completed"
            else None,
            error_code=row["error_code"] if row["state"] == "failed" else None,
        )
