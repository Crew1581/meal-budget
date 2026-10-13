import json
import os
import re
import selectors
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from time import monotonic

import httpx
import pytest

from meal_budget.core.config import PROJECT_ROOT


def process_environment(**overrides):
    # Не передаем секреты из окружения разработчика дочерним процессам.
    return {
        "APP_ENVIRONMENT": "test",
        "CATALOG_PATH": str(PROJECT_ROOT / "data/products.json"),
        "DEEPSEEK_API_KEY": "",
        "DEEPSEEK_MODEL": "",
        "GENERATION_ACCESS_TOKEN": "",
        "GENERATION_HTTP_ENABLED": "false",
        **overrides,
    }


def run_code(code, directory, **environment):
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=directory,
        env={"PYTHONPATH": str(PROJECT_ROOT), **process_environment(**environment)},
        capture_output=True,
        text=True,
        timeout=15,
    )


@pytest.mark.parametrize(
    "args,host,port,reload",
    [
        ([], "127.0.0.1", 8000, False),
        (["--host", "127.0.0.2", "--port", "8765"], "127.0.0.2", 8765, False),
        (["--port", "0"], "127.0.0.1", 0, False),
        (["--reload"], "127.0.0.1", 8000, True),
    ],
)
def test_manage_passes_safe_uvicorn_options(tmp_path, args, host, port, reload):
    result = run_code(
        f"""
import json, manage
calls = []
manage.uvicorn.run = lambda app, **kwargs: calls.append({{"app": app, **kwargs}})
manage.main({args!r})
assert len(calls) == 1
print(json.dumps(calls[0]))
""",
        tmp_path,
        WEB_CONCURRENCY="8",
    )
    assert result.returncode == 0, result.stderr
    options = json.loads(result.stdout)
    assert options == {
        "app": "main:create_app",
        "factory": True,
        "app_dir": str(PROJECT_ROOT),
        "host": host,
        "port": port,
        "workers": 1,
        "reload": reload,
        "reload_dirs": [str(PROJECT_ROOT)] if reload else None,
        "proxy_headers": False,
        "access_log": False,
    }


def test_reload_rejected_with_generation_enabled(tmp_path):
    result = run_code(
        """
import manage
def forbidden(*args, **kwargs):
    raise AssertionError("server must not start")
manage.uvicorn.run = forbidden
manage.main(["--reload"])
""",
        tmp_path,
        GENERATION_HTTP_ENABLED="true",
        GENERATION_ACCESS_TOKEN="fake-test-token",
    )
    assert result.returncode == 2
    assert "--reload недоступен" in result.stderr
    assert "fake-test-token" not in result.stdout + result.stderr
    assert "Traceback" not in result.stderr


def test_settings_error_does_not_print_values(tmp_path):
    result = run_code(
        "import manage; manage.main([])",
        tmp_path,
        GENERATION_HTTP_ENABLED="private-invalid-value",
        GENERATION_ACCESS_TOKEN="private-test-token",
    )
    assert result.returncode == 2
    assert "Некорректные настройки" in result.stderr
    assert "private-" not in result.stdout + result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("args", [["--port", "-1"], ["--port", "65536"], ["--workers", "2"]])
def test_invalid_launch_arguments(tmp_path, args):
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "manage.py"), *args],
        cwd=tmp_path,
        env=process_environment(),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


def test_help_without_pythonpath_or_valid_settings(tmp_path):
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "manage.py"), "--help"],
        cwd=tmp_path,
        env=process_environment(GENERATION_HTTP_ENABLED="private-invalid-value"),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0
    assert "--reload" in result.stdout
    assert "--host" in result.stdout and "--port" in result.stdout
    assert result.stderr == ""


def test_root_factory_owns_lifespan_without_eager_settings(tmp_path):
    result = run_code(
        """
import main
from fastapi.testclient import TestClient
from meal_budget.core.config import Settings
assert main.create_app.__module__ == "main"
assert not hasattr(main, "app")
settings = Settings(_env_file=None, generation_http_enabled=False, deepseek_api_key="")
def forbidden(*args, **kwargs):
    raise AssertionError("must not load settings or create provider")
main.Settings = forbidden
main.DeepSeekProvider = forbidden
application = main.create_app(settings)
assert not hasattr(application.state, "catalog")
with TestClient(application) as client:
    assert client.get("/health").json() == {"status": "ok"}
    assert application.state.catalog is not None
    assert application.state.generation_service is None
assert application.state.catalog is None
assert application.state.generation_service is None
""",
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_root_factory_import_does_not_validate_settings(tmp_path):
    result = run_code(
        "import main; assert callable(main.create_app); assert not hasattr(main, 'app')",
        tmp_path,
        GENERATION_HTTP_ENABLED="private-invalid-value",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_flat_package_paths_and_cli_without_pythonpath():
    assert Path(__file__).resolve().parents[1] == PROJECT_ROOT
    assert (PROJECT_ROOT / "meal_budget/core/config.py").is_file()
    result = subprocess.run(
        [sys.executable, "-m", "meal_budget.generate_menu", "--help"],
        cwd=PROJECT_ROOT,
        env=process_environment(),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0
    assert "generated/" in result.stdout
    assert result.stderr == ""


@contextmanager
def managed_server(directory, *, runner="manage"):
    # Настоящий запуск manage.py через .venv без PYTHONPATH; порт выбирает ОС.
    command = [sys.executable, str(PROJECT_ROOT / "manage.py"), "--port", "0"]
    if runner == "uvicorn":
        command = [
            sys.executable,
            "-m",
            "uvicorn",
            "main:create_app",
            "--factory",
            "--app-dir",
            str(PROJECT_ROOT),
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--workers",
            "1",
            "--no-access-log",
        ]
    process = subprocess.Popen(
        command,
        cwd=directory,
        env=process_environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stderr, selectors.EVENT_READ)
            deadline = monotonic() + 10
            received = bytearray()
            match = None
            while monotonic() < deadline:
                events = selector.select(max(0, deadline - monotonic()))
                if not events:
                    break
                chunk = os.read(process.stderr.fileno(), 65536)
                if not chunk:
                    break
                received.extend(chunk)
                # Сообщение Uvicorn появляется после bind/listen, без sleep/polling HTTP.
                match = re.search(rb"Uvicorn running on http://127\.0\.0\.1:(\d+)", received)
                if match:
                    break
            assert match is not None, "manage.py не запустил сервер за отведенное время"
        yield f"http://127.0.0.1:{int(match.group(1))}"
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)
            raise


@pytest.mark.parametrize("foreign_directory", [False, True])
@pytest.mark.parametrize("runner", ["manage", "uvicorn"])
def test_direct_launch_and_routes_without_keys(tmp_path, foreign_directory, runner):
    directory = tmp_path if foreign_directory else PROJECT_ROOT
    with (
        managed_server(directory, runner=runner) as url,
        httpx.Client(base_url=url, trust_env=False) as client,
    ):
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/v1/products").status_code == 200
        menu = json.loads((PROJECT_ROOT / "examples/menu-calculation.json").read_text())
        calculation = client.post("/v1/menu-calculations", json=menu)
        assert calculation.status_code == 200
        assert calculation.json()["total_cost"] == 89.9
        assert set(client.get("/openapi.json").json()["paths"]) == {
            "/health",
            "/v1/products",
            "/v1/menu-calculations",
        }
        assert client.post("/v1/menu-generations", json={}).status_code == 404
