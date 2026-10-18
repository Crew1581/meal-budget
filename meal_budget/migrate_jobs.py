"""Отдельная команда yoyo; активна только PostgreSQL-история миграций."""

import argparse
import sys
from urllib.parse import urlencode

from yoyo import get_backend, read_migrations

from meal_budget.core.config import PROJECT_ROOT, Settings
from meal_budget.services.job_store import database_parameters


def migrate(url: str, *, rollback: bool = False) -> None:
    params = database_parameters(url)
    params["connect_timeout"] = "5"
    # Один psycopg 3 для async API и sync-команды yoyo, без psycopg2.
    with get_backend("postgresql+psycopg:///?" + urlencode(params)) as backend:
        migrations = read_migrations(str(PROJECT_ROOT / "migrations/postgresql"))
        with backend.lock():
            if rollback:
                backend.rollback_migrations(backend.to_rollback(migrations))
            else:
                backend.apply_migrations(backend.to_apply(migrations))


def main() -> int:
    parser = argparse.ArgumentParser(description="Миграции PostgreSQL заданий.")
    parser.add_argument("action", choices=("apply", "rollback"))
    args = parser.parse_args()
    try:
        migrate(
            Settings().generation_jobs_database_url.get_secret_value(),
            rollback=args.action == "rollback",
        )
    except Exception:
        print("Не удалось выполнить миграции PostgreSQL заданий.", file=sys.stderr)
        return 2
    print("Миграции PostgreSQL заданий выполнены.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
