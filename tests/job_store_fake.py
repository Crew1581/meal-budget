"""Только автономные тесты worker/HTTP; не SQL-адаптер и не production fallback."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from meal_budget.schemas.generation import GenerationResult
from meal_budget.schemas.jobs import JobStatus
from meal_budget.services.app_admission import AdmissionError, request_hash
from meal_budget.services.job_store import SAFE_JOB_ERRORS, encode


class MemoryJobStore:
    rows = None

    def __init__(self, rows):
        self.rows = rows
        self.closed = False
        self.installations = {}
        self.usage = {}

    async def register_installation(self, digest, settings):
        if len(self.installations) >= settings.installations_total_limit:
            raise AdmissionError("installation_capacity_reached", 86400)
        if len(self.installations) >= settings.installations_daily_limit:
            raise AdmissionError("installation_daily_quota_exceeded", 60)
        installation_id = str(uuid4())
        self.installations[installation_id] = digest
        return installation_id

    async def authenticate_installation(self, digest):
        return next((key for key, value in self.installations.items() if value == digest), None)

    async def create_app(self, request, version, installation_id, key, settings, available):
        await self.cleanup(datetime.now(UTC), settings.generation_jobs_retention_seconds)
        digest = request_hash(request)
        previous = next(
            (
                row
                for row in self.rows.values()
                if row.get("installation_id") == installation_id
                and row.get("idempotency_key") == key
            ),
            None,
        )
        if previous:
            if previous["request_hash"] != digest:
                raise AdmissionError("idempotency_conflict")
            return previous["id"], True
        if not available:
            raise AdmissionError("worker_unavailable")
        if sum(self.usage.values()) >= settings.generation_app_daily_global_limit:
            raise AdmissionError("app_daily_quota_exceeded", 60)
        if self.usage.get(installation_id, 0) >= settings.generation_app_daily_installation_limit:
            raise AdmissionError("installation_daily_job_quota_exceeded", 60)
        job_id = await self.create(
            request, version, settings.generation_jobs_queue_limit, datetime.now(UTC)
        )
        if job_id is None:
            raise AdmissionError("generation_queue_full", 1)
        self.rows[job_id].update(
            installation_id=installation_id, idempotency_key=key, request_hash=digest
        )
        self.usage[installation_id] = self.usage.get(installation_id, 0) + 1
        return job_id, False

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

    async def get(self, job_id, installation_id=None):
        row = self.rows.get(str(job_id))
        if row is None or (
            installation_id is not None and row.get("installation_id") != installation_id
        ):
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
