"""Установки, владельцы, идемпотентность и независимый от TTL учёт квот."""

from yoyo import step

__depends__ = {"001_generation_jobs_postgresql"}

steps = [
    step(
        """
        CREATE TABLE installations (
            id UUID PRIMARY KEY,
            token_hash BYTEA NOT NULL UNIQUE CHECK (octet_length(token_hash) = 32),
            created_at TIMESTAMPTZ NOT NULL
        )
        """,
        "DROP TABLE installations",
    ),
    step(
        "CREATE INDEX installations_created ON installations (created_at)",
        "DROP INDEX installations_created",
    ),
    step(
        """
        ALTER TABLE generation_jobs
            ADD COLUMN installation_id UUID REFERENCES installations(id),
            ADD COLUMN idempotency_key TEXT COLLATE "C",
            ADD COLUMN request_hash BYTEA,
            ADD CONSTRAINT app_job_identity CHECK (
                (installation_id IS NULL AND idempotency_key IS NULL AND request_hash IS NULL)
                OR (installation_id IS NOT NULL AND idempotency_key IS NOT NULL
                    AND request_hash IS NOT NULL AND octet_length(request_hash) = 32
                    AND octet_length(idempotency_key) BETWEEN 1 AND 64
                    AND idempotency_key !~ '[^A-Za-z0-9_-]')
            ),
            ADD CONSTRAINT app_job_idempotency UNIQUE (installation_id, idempotency_key)
        """,
        """
        DELETE FROM generation_jobs WHERE installation_id IS NOT NULL;
        ALTER TABLE generation_jobs
            DROP CONSTRAINT app_job_idempotency,
            DROP CONSTRAINT app_job_identity,
            DROP COLUMN request_hash,
            DROP COLUMN idempotency_key,
            DROP COLUMN installation_id
        """,
    ),
    step(
        """
        CREATE TABLE app_daily_usage (
            day DATE PRIMARY KEY,
            accepted INTEGER NOT NULL CHECK (accepted > 0)
        )
        """,
        "DROP TABLE app_daily_usage",
    ),
    step(
        """
        CREATE TABLE installation_daily_usage (
            installation_id UUID NOT NULL REFERENCES installations(id),
            day DATE NOT NULL,
            accepted INTEGER NOT NULL CHECK (accepted > 0),
            PRIMARY KEY (installation_id, day)
        )
        """,
        "DROP TABLE installation_daily_usage",
    ),
    step(
        "CREATE INDEX installation_usage_day ON installation_daily_usage (day)",
        "DROP INDEX installation_usage_day",
    ),
]
