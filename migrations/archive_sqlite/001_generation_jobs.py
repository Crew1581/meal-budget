"""Временные серверные задания; не мобильная SQLite."""

from yoyo import step

__depends__ = set()

steps = [
    step(
        """
        CREATE TABLE generation_jobs (
            id TEXT PRIMARY KEY,
            state TEXT NOT NULL CHECK (state IN ('queued', 'running', 'completed', 'failed')),
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            request_json TEXT NOT NULL,
            catalog_version TEXT NOT NULL,
            result_json TEXT,
            error_code TEXT,
            CHECK (
                (state IN ('queued', 'running') AND finished_at IS NULL
                    AND result_json IS NULL AND error_code IS NULL)
                OR (state = 'completed' AND finished_at IS NOT NULL
                    AND result_json IS NOT NULL AND error_code IS NULL)
                OR (state = 'failed' AND finished_at IS NOT NULL
                    AND result_json IS NULL AND error_code IS NOT NULL)
            )
        )
        """,
        "DROP TABLE generation_jobs",
    ),
    step(
        "CREATE INDEX generation_jobs_queue ON generation_jobs (state, created_at, id)",
        "DROP INDEX generation_jobs_queue",
    ),
    step(
        "CREATE INDEX generation_jobs_expiry ON generation_jobs (finished_at)",
        "DROP INDEX generation_jobs_expiry",
    ),
]
