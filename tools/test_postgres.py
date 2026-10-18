"""Изолированный Compose-проект для PostgreSQL-тестов; секреты/.env не читаются."""

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    project = "meal8-test-" + uuid4().hex[:12]
    password = uuid4().hex
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ["HOME"],
        "POSTGRES_DB": "meal_budget_test",
        "POSTGRES_USER": "meal_test",
        "POSTGRES_PASSWORD": password,
        "POSTGRES_PORT": "0",
    }
    compose = ["docker", "compose", "--env-file", "/dev/null", "-p", project]
    try:
        subprocess.run(
            [*compose, "up", "-d", "--wait", "db"],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
            timeout=180,
        )
        address = subprocess.check_output(
            [*compose, "port", "db", "5432"], cwd=ROOT, env=env, text=True
        ).strip()
        env["MEAL_TEST_POSTGRES_URL"] = (
            f"postgresql://meal_test:{password}@{address}/meal_budget_test"
        )
        return subprocess.call(
            [sys.executable, "-m", "pytest", "-m", "postgres", "tests/integration", *sys.argv[1:]],
            cwd=ROOT,
            env=env,
        )
    except (subprocess.SubprocessError, OSError):
        print("Не удалось запустить отдельный тестовый PostgreSQL/Compose.", file=sys.stderr)
        return 2
    finally:
        # Только уникальный проект и volume, созданные данным запуском.
        subprocess.run(
            [*compose, "down", "--volumes"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            timeout=60,
            check=False,
        )


if __name__ == "__main__":
    raise SystemExit(main())
