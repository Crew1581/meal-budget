"""Первая независимая PostgreSQL-история; не перенос данных из SQLite."""

from yoyo import step

__depends__ = set()

steps = [
    step(
        """
        CREATE TABLE generation_jobs (
            id UUID PRIMARY KEY,
            state TEXT NOT NULL CHECK (state IN ('queued', 'running', 'completed', 'failed')),
            created_at TIMESTAMPTZ NOT NULL,
            started_at TIMESTAMPTZ,
            finished_at TIMESTAMPTZ,
            request_json JSONB NOT NULL CHECK (jsonb_typeof(request_json) = 'object'),
            catalog_version TEXT NOT NULL,
            result_json JSONB,
            error_code TEXT,
            CHECK (
                (state = 'queued' AND started_at IS NULL AND finished_at IS NULL
                    AND result_json IS NULL AND error_code IS NULL)
                OR (state = 'running' AND started_at IS NOT NULL AND finished_at IS NULL
                    AND result_json IS NULL AND error_code IS NULL)
                OR (state = 'completed' AND started_at IS NOT NULL AND finished_at IS NOT NULL
                    AND result_json IS NOT NULL AND jsonb_typeof(result_json) = 'object'
                    AND error_code IS NULL)
                OR (state = 'failed' AND finished_at IS NOT NULL
                    AND result_json IS NULL AND error_code IS NOT NULL)
            )
        )
        """,
        "DROP TABLE generation_jobs",
    ),
    step(
        "CREATE INDEX generation_jobs_queue ON generation_jobs (created_at, id) "
        "WHERE state = 'queued'",
        "DROP INDEX generation_jobs_queue",
    ),
    step(
        "CREATE INDEX generation_jobs_expiry ON generation_jobs (finished_at) "
        "WHERE state IN ('completed', 'failed')",
        "DROP INDEX generation_jobs_expiry",
    ),
]
