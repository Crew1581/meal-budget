"""Только выделенный локальный PostgreSQL; никогда не URL приложения/.env."""

import os
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from meal_budget.core.config import Settings
from meal_budget.migrate_jobs import migrate


@pytest.fixture
def postgres_url():
    url = os.environ.get("MEAL_TEST_POSTGRES_URL", "")
    try:
        params = conninfo_to_dict(url) if url else {}
        if (
            params.get("host") not in ("127.0.0.1", "localhost")
            or params.get("dbname") != "meal_budget_test"
        ):
            raise ValueError
        admin = psycopg.connect(url, autocommit=True, connect_timeout=3)
    except Exception:
        pytest.fail(
            "Нужен выделенный локальный PostgreSQL с БД meal_budget_test и "
            "MEAL_TEST_POSTGRES_URL; .env и URL приложения не используются.",
            pytrace=False,
        )
    name = "meal_test_" + uuid4().hex
    try:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        parts = urlsplit(url)
        yield urlunsplit(parts._replace(path="/" + name))
    finally:
        # Удаляется исключительно БД, созданная этой fixture с уникальным именем.
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )
        admin.close()


@pytest.fixture
def pg_settings(postgres_url, monkeypatch):
    migrate(postgres_url)
    monkeypatch.setenv("GENERATION_JOBS_DATABASE_URL", postgres_url)
    return Settings(
        _env_file=None,
        generation_jobs_database_url=postgres_url,
        generation_jobs_db_timeout=1,
        deepseek_api_key="provider-test-key",
        deepseek_model="test-model",
        deepseek_base_url="http://provider.test",
    )
