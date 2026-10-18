"""Только автономные тесты worker/HTTP; не SQL-адаптер и не production fallback."""

from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

from meal_budget.schemas.generation import GenerationResult
from meal_budget.schemas.jobs import JobStatus
from meal_budget.services.job_store import SAFE_JOB_ERRORS, encode


class MemoryJobStore:
    rows = None

    def __init__(self, rows):
        self.rows = rows
        self.closed = False

    @classmethod
    async def open(cls, settings):
        return cls(cls.rows if cls.rows is not None else {})

    async def close(self):
        self.closed = True

    async def create(self, request, version, limit, now):
        if sum(row["state"] == "queued" for row in self.rows.values()) >= limit:
            return None
        job_id = str(uuid4())
        self.rows[job_id] = dict(
            id=job_id,
            state="queued",
            created_at=now,
            started_at=None,
            finished_at=None,
            request_json=encode(request),
            catalog_version=version,
            result_json=None,
            error_code=None,
        )
        return job_id

    async def has_queued(self):
        return any(row["state"] == "queued" for row in self.rows.values())

    async def claim_next(self, now):
        rows = sorted(
            (row for row in self.rows.values() if row["state"] == "queued"),
            key=lambda row: (row["created_at"], row["id"]),
        )
        if not rows:
            return None
        row = rows[0]
        row.update(state="running", started_at=now)
        return deepcopy(row)

    async def complete(self, job_id, result, now):
        row = self.rows[str(job_id)]
        if row["state"] == "running":
            row.update(state="completed", result_json=encode(result), finished_at=now)

    async def fail(self, job_id, code, now):
        row = self.rows[str(job_id)]
        if row["state"] in ("queued", "running"):
            row.update(
                state="failed",
                error_code=code if code in SAFE_JOB_ERRORS else "internal_error",
                finished_at=now,
            )

    async def interrupt_running(self, now):
        for row in self.rows.values():
            if row["state"] == "running":
                await self.fail(row["id"], "interrupted", now)

    async def recover(self, version, now):
        await self.interrupt_running(now)
        for row in self.rows.values():
            if row["state"] == "queued" and row["catalog_version"] != version:
                await self.fail(row["id"], "catalog_version_mismatch", now)

    async def cleanup(self, now, retention):
        for row in list(self.rows.values()):
            if row["state"] in ("completed", "failed") and row["finished_at"] <= now - timedelta(
                seconds=retention
            ):
                del self.rows[row["id"]]

    async def get(self, job_id):
        row = self.rows.get(str(job_id))
        if row is None:
            return None
        return JobStatus(
            **{
                key: row[key]
                for key in (
                    "id",
                    "state",
                    "created_at",
                    "started_at",
                    "finished_at",
                    "catalog_version",
                )
            },
            result=GenerationResult.model_validate(row["result_json"])
            if row["state"] == "completed"
            else None,
            error_code=row["error_code"],
        )
