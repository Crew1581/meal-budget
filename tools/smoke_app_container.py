"""Production smoke без pytest/DeepSeek: только свои временные Docker-ресурсы."""

import hashlib
import json
import os
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import psycopg

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "meal-budget:local"
# Фиксированный синтетический провайдер в отдельном контейнере, без внешних запросов.
PROVIDER = """
import json, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
gate = threading.Event()
gate.set()
calls = 0
plan = {
    "recipes": [{"id": "oats-demo", "name": "Synthetic smoke", "servings": 1,
        "ingredients": [{"product_id": "oats", "grams": 100}],
        "steps": ["Prepare"], "cooking_minutes": 10}],
    "days": [{"day": 1, "meals": [{"recipe_id": "oats-demo", "servings": 1}]}],
}
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        body = json.dumps({"calls": calls}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_POST(self):
        global calls
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.path == "/block": gate.clear()
        elif self.path == "/release": gate.set()
        else:
            calls += 1
            gate.wait(30)
        body = json.dumps({"model": "local-smoke", "choices": [{"finish_reason": "stop",
            "message": {"content": json.dumps(plan)}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
ThreadingHTTPServer(("0.0.0.0", 9001), Handler).serve_forever()
"""


def docker(*arguments, env=None, check=True, include_stderr=False):
    result = subprocess.run(
        ["docker", *arguments],
        text=True,
        capture_output=True,
        env=env,
        timeout=90,
        check=check,
    )
    return (result.stdout + (result.stderr if include_stderr else "")).strip()


def until(predicate):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (httpx.TransportError, psycopg.OperationalError):
            pass
        time.sleep(0.05)
    raise AssertionError("smoke deadline exceeded")


def main() -> int:
    project = "meal9-smoke-" + uuid4().hex[:12]
    db, provider, api = (project + suffix for suffix in ("-db", "-provider", "-api"))
    password = uuid4().hex
    environment = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ["HOME"],
        "POSTGRES_PASSWORD": password,
        "POSTGRES_USER": "meal_smoke",
        "POSTGRES_DB": "meal_smoke",
        "GENERATION_HTTP_ENABLED": "true",
        "GENERATION_APP_ENABLED": "true",
        "GENERATION_ACCESS_TOKEN": uuid4().hex,
        "GENERATION_JOBS_DB_TIMEOUT": "1",
        "GENERATION_JOBS_QUEUE_LIMIT": "1",
        "GENERATION_JOBS_RETENTION_SECONDS": "60",
        "GENERATION_APP_DAILY_GLOBAL_LIMIT": "3",
        "GENERATION_APP_DAILY_INSTALLATION_LIMIT": "2",
        "INSTALLATIONS_DAILY_LIMIT": "3",
        "INSTALLATIONS_TOTAL_LIMIT": "3",
        "DEEPSEEK_API_KEY": "synthetic-smoke-key",
        "DEEPSEEK_MODEL": "local-smoke",
        "DEEPSEEK_BASE_URL": f"http://{provider}:9001",
        "CATALOG_PATH": "data/products.json",
    }
    environment["GENERATION_JOBS_DATABASE_URL"] = (
        f"postgresql://meal_smoke:{password}@{db}:5432/meal_smoke"
    )
    env_flags = [
        flag for name in environment if name not in ("PATH", "HOME") for flag in ("-e", name)
    ]
    names = (api, provider, db)
    admin = None
    try:
        docker("network", "create", project)
        docker(
            "run",
            "-d",
            "--name",
            db,
            "--network",
            project,
            "-p",
            "127.0.0.1::5432",
            "-e",
            "POSTGRES_PASSWORD",
            "-e",
            "POSTGRES_USER",
            "-e",
            "POSTGRES_DB",
            "postgres:17-alpine",
            "postgres",
            "-c",
            "log_statement=none",
            "-c",
            "log_error_verbosity=terse",
            "-c",
            "log_min_error_statement=panic",
            "-c",
            "log_parameter_max_length_on_error=0",
            env=environment,
        )
        port = int(docker("port", db, "5432/tcp").rsplit(":", 1)[1])
        admin = until(
            lambda: psycopg.connect(
                host="127.0.0.1",
                port=port,
                user="meal_smoke",
                password=password,
                dbname="meal_smoke",
                autocommit=True,
                connect_timeout=1,
            )
        )
        docker(
            "run",
            "-d",
            "--name",
            provider,
            "--network",
            project,
            "-p",
            "127.0.0.1::9001",
            IMAGE,
            "python",
            "-u",
            "-c",
            PROVIDER,
        )
        provider_port = int(docker("port", provider, "9001/tcp").rsplit(":", 1)[1])
        docker(
            "run",
            "--rm",
            "--network",
            project,
            *env_flags,
            IMAGE,
            "python",
            "-m",
            "meal_budget.migrate_jobs",
            "apply",
            env=environment,
        )

        @contextmanager
        def start_api():
            docker(
                "run",
                "-d",
                "--name",
                api,
                "--network",
                project,
                "-p",
                "127.0.0.1::8000",
                *env_flags,
                IMAGE,
                "python",
                "-m",
                "uvicorn",
                "main:create_app",
                "--factory",
                "--host",
                "0.0.0.0",
                "--port",
                "8000",
                "--workers",
                "1",
                "--no-access-log",
                "--root-path",
                "/gateway/api",
                env=environment,
            )
            api_port = int(docker("port", api, "8000/tcp").rsplit(":", 1)[1])
            with httpx.Client(
                base_url=f"http://127.0.0.1:{api_port}", trust_env=False, timeout=5
            ) as client:
                until(lambda: client.get("/health").status_code == 200)
                yield client

        payload = json.loads((ROOT / "examples/generation-request.json").read_bytes())
        secrets = [
            password,
            environment["GENERATION_ACCESS_TOKEN"],
            environment["DEEPSEEK_API_KEY"],
        ]
        with httpx.Client(base_url=f"http://127.0.0.1:{provider_port}", trust_env=False) as control:
            until(lambda: control.get("/calls").status_code == 200)
            with start_api() as client:
                assert client.get("/v1/products").status_code == 200
                calculation = json.loads((ROOT / "examples/menu-calculation.json").read_bytes())
                assert client.post("/v1/menu-calculations", json=calculation).status_code == 200
                issued = client.post("/v1/installations")
                assert issued.status_code == 201 and issued.headers["cache-control"] == "no-store"
                token = issued.json()["token"]
                other = client.post("/v1/installations").json()["token"]
                secrets.extend((token, other))
                third_installation = client.post("/v1/installations")
                assert third_installation.status_code == 201
                secrets.append(third_installation.json()["token"])
                assert client.post("/v1/installations").status_code == 429
                headers = {"Authorization": "Bearer " + token, "Idempotency-Key": "smoke-1"}
                foreign = {"Authorization": "Bearer " + other, "Idempotency-Key": "smoke-1"}
                developer = {"Authorization": "Bearer " + environment["GENERATION_ACCESS_TOKEN"]}
                path = "/v1/app/generation-jobs"
                assert client.post(path, json=payload).status_code == 401
                assert client.post(path, headers=developer, json=payload).status_code == 401
                for private_path in ("/v1/menu-generations", "/v1/generation-jobs"):
                    assert (
                        client.post(private_path, headers=headers, json=payload).status_code == 401
                    )
                for body in (b"{" * 65537, iter((b"{" * 32768, b"{" * 32769))):
                    assert client.post(path, headers=headers, content=body).status_code == 413
                redirect = client.post(path + "/", headers=headers, json=payload)
                assert redirect.status_code == 307
                assert "/gateway/api/v1/app/generation-jobs" in redirect.headers["location"]
                with ThreadPoolExecutor(max_workers=8) as executor:
                    replies = list(
                        executor.map(
                            lambda _: client.post(path, headers=headers, json=payload), range(8)
                        )
                    )
                assert all(reply.status_code == 202 for reply in replies)
                assert len({reply.json()["id"] for reply in replies}) == 1
                first = replies[0].json()["id"]
                until(
                    lambda: (
                        client.get(path + "/" + first, headers=headers).json().get("state")
                        == "completed"
                    )
                )
                assert control.get("/calls").json()["calls"] == 1
                unknown = client.get(path + "/" + str(uuid4()), headers=foreign)
                assert client.get(path + "/" + first, headers=foreign).json() == unknown.json()
                assert unknown.status_code == 404
                conflict = client.post(path, headers=headers, json={**payload, "wishes": "changed"})
                assert conflict.status_code == 409
                rows = admin.execute(
                    "SELECT token_hash FROM installations WHERE id=%s",
                    (UUID(issued.json()["installation_id"]),),
                ).fetchall()
                assert rows == [(hashlib.sha256(token.encode()).digest(),)]
                assert admin.execute("SELECT accepted FROM app_daily_usage").fetchall() == [(1,)]
                print("Production: registration/auth/root_path/body/parallel replay/ownership — OK")

                control.post("/block")
                second_headers = {**headers, "Idempotency-Key": "smoke-2"}
                second = client.post(path, headers=second_headers, json=payload).json()["id"]
                until(lambda: control.get("/calls").json()["calls"] == 2)
                assert (
                    client.post("/v1/menu-generations", headers=developer, json=payload).status_code
                    == 429
                )
                queued = client.post(path, headers=foreign, json=payload).json()["id"]
                full = client.post(
                    path, headers={**foreign, "Idempotency-Key": "new"}, json=payload
                )
                assert (
                    full.status_code == 429
                    and full.json()["detail"]["code"] == "generation_queue_full"
                )
                admin.execute("""
                    CREATE FUNCTION smoke_claim() RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN PERFORM pg_advisory_xact_lock(880009); RETURN NEW; END; $$
                """)
                admin.execute("SELECT pg_advisory_lock(880009)")
                admin.execute(
                    "CREATE TRIGGER smoke_claim BEFORE UPDATE ON generation_jobs "
                    "FOR EACH ROW WHEN (NEW.state='running') EXECUTE FUNCTION smoke_claim()"
                )
                control.post("/release")
                until(lambda: client.get(path + "/" + queued, headers=foreign).status_code == 503)
                assert client.post(path, headers=foreign, json=payload).json()["id"] == queued
                assert client.get(path + "/" + queued, headers=headers).status_code == 404
                assert (
                    client.get(path + "/" + second, headers=headers).json()["state"] == "completed"
                )
                assert (
                    client.post(
                        path, headers={**headers, "Idempotency-Key": "new"}, json=payload
                    ).status_code
                    == 503
                )
                assert control.get("/calls").json()["calls"] == 2
                print("Production: общий слот/очередь/реальная авария claim/replay без worker — OK")
                logs = docker("logs", api, include_stderr=True)
                assert "worker_unavailable" in logs
                assert all(secret not in logs for secret in secrets)
            docker("stop", "--time", "10", api)
            docker("rm", api)
            admin.execute("SELECT pg_advisory_unlock(880009)")
            admin.execute("DROP TRIGGER smoke_claim ON generation_jobs")
            with start_api() as client:
                until(
                    lambda: (
                        client.get(path + "/" + queued, headers=foreign).json().get("state")
                        == "completed"
                    )
                )
                assert control.get("/calls").json()["calls"] == 3
                limited = client.post(
                    path, headers={**foreign, "Idempotency-Key": "new"}, json=payload
                )
                assert (
                    limited.status_code == 429
                    and limited.json()["detail"]["code"] == "app_daily_quota_exceeded"
                )
                assert 1 <= int(limited.headers["retry-after"]) <= 86400
                admin.execute(
                    "UPDATE generation_jobs SET "
                    "finished_at=clock_timestamp()-interval '61 seconds' "
                    "WHERE id=%s",
                    (UUID(first),),
                )
                assert client.get(path + "/" + first, headers=headers).status_code == 404
                assert client.post(path, headers=headers, json=payload).status_code == 429
                assert admin.execute("SELECT accepted FROM app_daily_usage").fetchall() == [(3,)]
                assert admin.execute("SELECT count(*) FROM generation_jobs").fetchone()[0] == 2
                print("Production: recovery/сохранённая квота/TTL без dangling key — OK")
            for name in names:
                logs = docker("logs", name, include_stderr=True)
                assert all(secret not in logs for secret in secrets)
            print("Production smoke — успешно; 3 вызова только синтетического провайдера.")
        return 0
    except Exception as exc:
        # Только класс/строка нашего сценария, без текста исключения, argv и locals.
        frames = [
            frame for frame in traceback.extract_tb(exc.__traceback__) if frame.filename == __file__
        ]
        line = frames[-1].lineno if frames else 0
        print(
            f"Production smoke не пройден: {type(exc).__name__}, строка {line}; "
            "секреты и сырые ошибки скрыты.",
            file=sys.stderr,
        )
        return 1
    finally:
        if admin is not None:
            admin.close()
        # Исключительно имена, созданные этим запуском; пользовательские ресурсы не трогаем.
        for name in names:
            docker("rm", "-f", "-v", name, check=False)
        docker("network", "rm", project, check=False)


if __name__ == "__main__":
    raise SystemExit(main())
