import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from job_store_fake import MemoryJobStore
from pydantic import ValidationError
from test_generation_http import (
    ACCESS_TOKEN,
    HEADERS,
    PROVIDER_KEY,
    RecordingTransport,
    async_client,
    envelope,
    example_request,
)
from test_generation_http import app_factory as app_factory

from main import create_app
from meal_budget.api.generation_guard import MAX_REQUEST_BYTES, GenerationGuard
from meal_budget.api.generations import GENERATION_PATH, GUARDED_REQUEST_KEY, JOBS_PATH
from meal_budget.catalog import load_catalog
from meal_budget.core.config import Settings
from meal_budget.schemas.generation import GenerationRequest
from meal_budget.services import generation_jobs as jobs_module


def completion_event(app, monkeypatch):
    event = asyncio.Event()
    store = app.state.generation_jobs.store
    for name in ("complete", "fail"):
        original = getattr(store, name)

        async def observed(*args, original=original):
            await original(*args)
            event.set()

        monkeypatch.setattr(store, name, observed)
    return event


async def finished(app, job_id, event):
    async with asyncio.timeout(5):
        while True:
            event.clear()
            job = await app.state.generation_jobs.get(job_id)
            if job.state in ("completed", "failed"):
                return job
            await event.wait()


def test_returns_before_provider_finishes_and_survives_client_close(app_factory, monkeypatch):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def handler(request):
            entered.set()
            await release.wait()
            return httpx.Response(200, json=envelope())

        app, transport, providers = app_factory(RecordingTransport(handler))
        async with app.router.lifespan_context(app):
            event = completion_event(app, monkeypatch)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test"
            ) as client:
                # Provider не может закончить до release: 202 не ждет его завершения.
                response = await asyncio.wait_for(
                    client.post(JOBS_PATH, headers=HEADERS, json=example_request()), 5
                )
                assert response.status_code == 202
                assert set(response.json()) == {"id", "state"}
                assert response.json()["state"] == "queued"
                job_id = response.json()["id"]
                await asyncio.wait_for(entered.wait(), 5)
                running = await client.get(f"{JOBS_PATH}/{job_id}", headers=HEADERS)
                assert running.json()["state"] == "running"
                assert running.json()["result"] is None
                assert running.json()["started_at"] is not None
                assert not providers[0]._client.is_closed
            # Принявший задание клиент уже закрыт; worker принадлежит lifespan.
            release.set()
            await finished(app, job_id, event)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test"
            ) as client:
                response = await client.get(f"{JOBS_PATH}/{job_id}", headers=HEADERS)
                body = response.json()
                assert body["state"] == "completed"
                assert body["result"]["calculation"]["total_cost"] == 89.9
                assert body["result"]["calculation"]["within_budget"] is True
                assert body["catalog_version"] == body["result"]["catalog_version"]
                assert body["finished_at"] is not None and body["error_code"] is None
                assert "private-reasoning" not in response.text
                assert "wishes" not in body
        assert len(transport.requests) == 1 and transport.closed
        assert providers[0]._client.is_closed

    asyncio.run(scenario())


def test_queue_and_shared_sync_slot(app_factory, monkeypatch):
    async def scenario():
        entered = [asyncio.Event(), asyncio.Event()]
        release = [asyncio.Event(), asyncio.Event()]

        async def handler(request):
            index = len(transport.requests) - 1
            if index < 2:
                entered[index].set()
                await release[index].wait()
            return httpx.Response(200, json=envelope())

        transport = RecordingTransport(handler)
        app, _, _ = app_factory(transport, generation_jobs_queue_limit=1)
        async with async_client(app) as client:
            event = completion_event(app, monkeypatch)
            sync = asyncio.create_task(
                client.post(GENERATION_PATH, headers=HEADERS, json=example_request())
            )
            try:
                await asyncio.wait_for(entered[0].wait(), 5)
                accepted = await client.post(JOBS_PATH, headers=HEADERS, json=example_request())
                job_id = accepted.json()["id"]
                assert accepted.status_code == 202
                queued = await client.get(f"{JOBS_PATH}/{job_id}", headers=HEADERS)
                assert queued.json()["state"] == "queued"
                assert queued.json()["started_at"] is None
                full = await client.post(JOBS_PATH, headers=HEADERS, json=example_request())
                assert full.status_code == 429
                assert full.json()["detail"]["code"] == "generation_queue_full"
                assert len(transport.requests) == 1
                release[0].set()
                assert (await sync).status_code == 200
                await asyncio.wait_for(entered[1].wait(), 5)
                # В обратную сторону: worker не позволяет синхронному POST обойти слот.
                assert (
                    await client.post(GENERATION_PATH, headers=HEADERS, json=example_request())
                ).status_code == 429
                second = await client.post(JOBS_PATH, headers=HEADERS, json=example_request())
                assert second.status_code == 202
                assert (
                    await client.post(JOBS_PATH, headers=HEADERS, json=example_request())
                ).status_code == 429
                assert len(transport.requests) == 2
                release[1].set()
                assert (await finished(app, job_id, event)).state == "completed"
                assert (await finished(app, second.json()["id"], event)).state == "completed"
                assert len(transport.requests) == 3
            finally:
                for gate in release:
                    gate.set()
                sync.cancel()
                await asyncio.gather(sync, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "kind,code,calls",
    [
        ("auth", "provider_authorization", 1),
        ("rate", "provider_rate_limit", 1),
        ("network", "provider_unavailable", 1),
        ("timeout", "provider_timeout", 1),
        ("deadline", "generation_timeout", 1),
        ("invalid", "invalid_response", 3),
        ("constraints", "attempts_exhausted", 3),
        ("configuration", "configuration_missing", 0),
        ("request", "invalid_request", 0),
    ],
)
def test_failures_are_safe_and_bounded(app_factory, monkeypatch, caplog, kind, code, calls):
    async def scenario():
        async def handler(request):
            if kind == "network":
                raise httpx.ConnectError("private-network-error")
            if kind == "timeout":
                raise httpx.ReadTimeout("private-timeout-error")
            if kind == "deadline":
                await asyncio.Event().wait()
            if kind in ("auth", "rate"):
                return httpx.Response(401 if kind == "auth" else 429, text="private-error")
            if kind == "invalid":
                return httpx.Response(200, text="private-invalid")
            plan = envelope()
            if kind == "constraints":
                value = json.loads(envelope()["choices"][0]["message"]["content"])
                value["recipes"][0]["ingredients"][0]["grams"] = 1
                plan = envelope(value)
            return httpx.Response(200, json=plan)

        app, transport, _ = app_factory(
            RecordingTransport(handler),
            deepseek_api_key="" if kind == "configuration" else PROVIDER_KEY,
            generation_total_timeout=0.05 if kind == "deadline" else 180,
        )
        async with async_client(app) as client:
            event = completion_event(app, monkeypatch)
            payload = example_request()
            payload["wishes"] = "private-wishes"
            if kind == "request":
                payload["disliked_product_ids"] = ["missing"]
            accepted = await client.post(JOBS_PATH, headers=HEADERS, json=payload)
            assert accepted.status_code == 202
            job_id = accepted.json()["id"]
            await finished(app, job_id, event)
            result = await client.get(f"{JOBS_PATH}/{job_id}", headers=HEADERS)
            assert result.status_code == 200
            assert result.json()["state"] == "failed"
            assert result.json()["error_code"] == code
            assert result.json()["result"] is None
            assert "private-" not in result.text
            assert len(transport.requests) == calls
        assert transport.closed

    with caplog.at_level(logging.DEBUG):
        asyncio.run(scenario())
    assert "private-" not in caplog.text
    assert ACCESS_TOKEN not in caplog.text and PROVIDER_KEY not in caplog.text


@pytest.mark.parametrize("root", ["", "/api", "/gateway/api"])
@pytest.mark.parametrize("slash", ["", "/"])
def test_auth_body_root_path_and_safe_validation(app_factory, root, slash):
    async def scenario():
        app, transport, _ = app_factory()
        path = root + JOBS_PATH
        async with async_client(app, root_path=root) as client:
            for headers in (
                {},
                {"Authorization": "Bearer wrong"},
                {"Authorization": b"Bearer \xff"},
            ):
                assert (
                    await client.post(path + slash, headers=headers, content=b"{" * 65537)
                ).status_code == 401
                assert (
                    await client.get(f"{path}/{uuid4()}{slash}", headers=headers)
                ).status_code == 401

            async def chunks():
                for _ in range(3):
                    yield b"{" * 32768

            for content in (b"{" * (MAX_REQUEST_BYTES + 1), chunks()):
                response = await client.post(path + slash, headers=HEADERS, content=content)
                assert response.status_code == 413
            for content in (b"private-invalid", b"[" * 10000 + b"]" * 10000):
                response = await client.post(
                    path, headers={**HEADERS, "Content-Type": "application/json"}, content=content
                )
                assert response.status_code == 422
                assert response.json()["detail"]["code"] == "invalid_request"
                assert "private" not in response.text and "input" not in response.text
            invalid_id = await client.get(path + "/private-invalid-id", headers=HEADERS)
            assert invalid_id.status_code == 422 and "private" not in invalid_id.text
            missing = await client.get(path + f"/{uuid4()}", headers=HEADERS)
            assert missing.status_code == 404
            assert missing.json()["detail"]["code"] == "job_not_found"
            assert not transport.requests
            redirect = await client.post(path + "/", headers=HEADERS, json=example_request())
            assert redirect.status_code == 307
            assert httpx.URL(redirect.headers["location"]).path == path
            assert not transport.requests

    asyncio.run(scenario())


@pytest.mark.parametrize("proof", ["absent", "boolean", "other_scope"])
def test_jobs_fail_closed_without_guard(app_factory, monkeypatch, proof):
    async def bypass(self, scope, receive, send):
        if scope["type"] == "http":
            if proof == "boolean":
                scope[GUARDED_REQUEST_KEY] = True
            if proof == "other_scope":
                scope[GUARDED_REQUEST_KEY] = dict(scope)
        await self.app(scope, receive, send)

    monkeypatch.setattr(GenerationGuard, "__call__", bypass)
    app, transport, _ = app_factory()
    with TestClient(app) as client:
        assert client.post(JOBS_PATH, headers=HEADERS, json=example_request()).status_code == 401
        assert client.get(f"{JOBS_PATH}/{uuid4()}", headers=HEADERS).status_code == 401
    assert not transport.requests


def test_disabled_and_openapi(app_factory):
    with TestClient(create_app(Settings(_env_file=None, generation_http_enabled=False))) as client:
        assert client.post(JOBS_PATH, json=example_request()).status_code == 404
        assert client.get(f"{JOBS_PATH}/{uuid4()}").status_code == 404
        assert JOBS_PATH not in client.get("/openapi.json").json()["paths"]
    app, _, _ = app_factory()
    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()
        for path, method in ((JOBS_PATH, "post"), (JOBS_PATH + "/{job_id}", "get")):
            assert schema["paths"][path][method]["security"] == [{"GenerationAccessToken": []}]
        assert "202" in schema["paths"][JOBS_PATH]["post"]["responses"]


def test_recovery_version_binding_and_persisted_result(app_factory, monkeypatch, job_rows):
    async def scenario():
        settings = Settings(_env_file=None)
        catalog = load_catalog(settings.catalog_path)
        store = MemoryJobStore(job_rows)
        now = datetime.now(UTC)
        payload = GenerationRequest.model_validate(example_request())
        queued = await store.create(payload, catalog.catalog_version, 10, now)
        running = await store.create(payload, catalog.catalog_version, 10, now)
        mismatch = await store.create(payload, "another-version", 10, now)
        job_rows[running].update(state="running", started_at=now)
        await store.close()
        app, transport, _ = app_factory()
        async with async_client(app) as client:
            event = completion_event(app, monkeypatch)
            result = await finished(app, queued, event)
            assert result.state == "completed"
            for job_id, code in ((running, "interrupted"), (mismatch, "catalog_version_mismatch")):
                body = (await client.get(f"{JOBS_PATH}/{job_id}", headers=HEADERS)).json()
                assert body["state"] == "failed" and body["error_code"] == code
                assert body["result"] is None
            assert len(transport.requests) == 1
        restarted, second_transport, _ = app_factory()
        async with async_client(restarted) as client:
            body = (await client.get(f"{JOBS_PATH}/{queued}", headers=HEADERS)).json()
            assert body["state"] == "completed"
            assert body["result"]["calculation"]["total_cost"] == 89.9
            assert not second_transport.requests

    asyncio.run(scenario())


def test_shutdown_interrupts_running_and_preserves_queue(app_factory, monkeypatch):
    async def scenario():
        entered = asyncio.Event()

        async def handler(request):
            entered.set()
            await asyncio.Event().wait()

        app, transport, providers = app_factory(RecordingTransport(handler))
        async with async_client(app) as client:
            running = (
                await client.post(JOBS_PATH, headers=HEADERS, json=example_request())
            ).json()["id"]
            await asyncio.wait_for(entered.wait(), 5)
            queued = (await client.post(JOBS_PATH, headers=HEADERS, json=example_request())).json()[
                "id"
            ]
            worker = app.state.generation_jobs
            store = worker.store
        assert worker.task.done() and transport.closed and providers[0]._client.is_closed
        assert store.closed
        restarted, transport, _ = app_factory()
        async with async_client(restarted) as client:
            event = completion_event(restarted, monkeypatch)
            body = (await client.get(f"{JOBS_PATH}/{running}", headers=HEADERS)).json()
            assert body["error_code"] == "interrupted"
            assert (await finished(restarted, queued, event)).state == "completed"
            assert len(transport.requests) == 1

    asyncio.run(scenario())


def test_cleanup_terminal_only_and_get_after_deletion(app_factory, monkeypatch):
    async def scenario():
        app, _, _ = app_factory(generation_jobs_retention_seconds=60)
        async with async_client(app) as client:
            event = completion_event(app, monkeypatch)
            accepted = await client.post(JOBS_PATH, headers=HEADERS, json=example_request())
            job_id = accepted.json()["id"]
            await finished(app, job_id, event)
            store = app.state.generation_jobs.store
            now = datetime.now(UTC)
            # При TTL=60 ровно на границе удаляем и результат, и анкету.
            store.rows[job_id]["finished_at"] = now - timedelta(seconds=61)
            payload = GenerationRequest.model_validate(example_request())
            queued = await store.create(payload, "old-catalog", 10, now - timedelta(days=10))
            running = await store.create(payload, "old-catalog", 10, now - timedelta(days=10))
            store.rows[running].update(state="running", started_at=now - timedelta(days=10))
            response = await client.get(f"{JOBS_PATH}/{job_id}", headers=HEADERS)
            assert response.status_code == 404
            assert await store.get(job_id) is None
            assert (await store.get(queued)).state == "queued"
            assert (await store.get(running)).state == "running"

    asyncio.run(scenario())


def test_jobs_under_nested_mount(app_factory):
    async def scenario():
        app, _, _ = app_factory()
        inner, outer = FastAPI(), FastAPI()
        inner.mount("/inner", app)
        outer.mount("/outer", inner)
        async with (
            app.router.lifespan_context(app),
            async_client(outer, root_path="/api") as client,
        ):
            path = "/api/outer/inner" + JOBS_PATH
            assert (await client.post(path, json=example_request())).status_code == 401
            assert (await client.get(path + f"/{uuid4()}")).status_code == 401
            response = await client.post(path, headers=HEADERS, json=example_request())
            assert response.status_code == 202
            assert (
                await client.get(path + "/" + response.json()["id"], headers=HEADERS)
            ).status_code == 200

    asyncio.run(scenario())


def test_terminal_retention_and_exact_decimal_serialization(job_rows):
    async def scenario():
        store = MemoryJobStore(job_rows)
        now = datetime.now(UTC)
        payload = GenerationRequest.model_validate(example_request())
        payload.budget = Decimal("89.90")
        job_id = await store.create(payload, "version", 4, now)
        row = await store.claim_next(now)
        assert row["request_json"]["budget"] == "89.90"
        restored = GenerationRequest.model_validate(row["request_json"])
        assert restored.budget.as_tuple() == payload.budget.as_tuple()
        await store.fail(job_id, "private-unrecognized-error", now)
        assert (await store.get(job_id)).error_code == "internal_error"
        await store.cleanup(now + timedelta(seconds=59), 60)
        assert (await store.get(job_id)).state == "failed"
        await store.cleanup(now + timedelta(seconds=60), 60)
        assert await store.get(job_id) is None
        await store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "field,value",
    [
        ("generation_jobs_queue_limit", 0),
        ("generation_jobs_queue_limit", 101),
        ("generation_jobs_retention_seconds", 0),
        ("generation_jobs_retention_seconds", float("inf")),
        ("generation_jobs_retention_seconds", 604801),
        ("generation_jobs_cleanup_interval", 0),
        ("generation_jobs_cleanup_interval", 3601),
        ("generation_jobs_pool_size", 0),
        ("generation_jobs_pool_size", 11),
        ("generation_jobs_db_timeout", 0),
    ],
)
def test_job_settings_limits(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_storage_errors_are_safe_and_never_retry(app_factory, monkeypatch, caplog):
    async def scenario():
        app, transport, _ = app_factory()
        async with async_client(app) as client:
            worker = app.state.generation_jobs
            complete = worker.store.complete

            async def broken(*args):
                raise RuntimeError("private-storage-error")

            # Неуспешная запись результата превращается в failed, не новую генерацию.
            event = completion_event(app, monkeypatch)
            monkeypatch.setattr(worker.store, "complete", broken)
            response = await client.post(JOBS_PATH, headers=HEADERS, json=example_request())
            job_id = response.json()["id"]
            result = await finished(app, job_id, event)
            assert result.state == "failed" and result.error_code == "internal_error"
            assert len(transport.requests) == 1
            monkeypatch.setattr(worker.store, "complete", complete)
            monkeypatch.setattr(worker.store, "get", broken)
            response = await client.get(JOBS_PATH + "/" + job_id, headers=HEADERS)
            assert response.status_code == 503 and "private" not in response.text
            monkeypatch.setattr(worker.store, "create", broken)
            response = await client.post(JOBS_PATH, headers=HEADERS, json=example_request())
            assert response.status_code == 503 and "private" not in response.text
            assert len(transport.requests) == 1

    with caplog.at_level(logging.DEBUG):
        asyncio.run(scenario())
    assert "private-storage-error" not in caplog.text


def test_full_queue_with_concurrent_posts_is_bounded(app_factory, monkeypatch):
    async def scenario():
        app, transport, _ = app_factory(generation_jobs_queue_limit=2)
        async with async_client(app) as client:
            worker = app.state.generation_jobs
            # Держим общий слот явно: все задания остаются queued.
            assert worker.slot.try_acquire()
            try:
                responses = await asyncio.gather(
                    *[
                        client.post(JOBS_PATH, headers=HEADERS, json=example_request())
                        for _ in range(10)
                    ]
                )
                assert sum(response.status_code == 202 for response in responses) == 2
                assert sum(response.status_code == 429 for response in responses) == 8
                ids = [
                    response.json()["id"] for response in responses if response.status_code == 202
                ]
                assert len(set(ids)) == 2  # Одинаковая анкета не дедуплицируется.
                assert not transport.requests
            finally:
                worker.slot.release()
            event = completion_event(app, monkeypatch)
            for job_id in ids:
                assert (await finished(app, job_id, event)).state == "completed"
            assert len(transport.requests) == 2

    asyncio.run(scenario())


def test_idle_worker_cleans_without_http_requests(app_factory, monkeypatch, job_rows):
    async def scenario():
        now = datetime.now(UTC)
        idle, tick, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()
        real_wait_for = asyncio.wait_for

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return now

        async def timer(awaitable, timeout):
            if timeout != 60:
                return await real_wait_for(awaitable, timeout)
            try:
                idle.set()
                await tick.wait()
                tick.clear()
                raise TimeoutError
            finally:
                awaitable.close()

        store = MemoryJobStore(job_rows)
        job_id = await store.create(
            GenerationRequest.model_validate(example_request()), "version", 4, now
        )
        await store.fail(job_id, "internal_error", now)
        await store.close()
        monkeypatch.setattr(jobs_module, "datetime", Clock)
        monkeypatch.setattr(jobs_module.asyncio, "wait_for", timer)
        app, transport, _ = app_factory(
            generation_jobs_retention_seconds=60, generation_jobs_cleanup_interval=60
        )
        async with app.router.lifespan_context(app):
            store = app.state.generation_jobs.store
            cleanup = store.cleanup

            async def observed(*args):
                await cleanup(*args)
                if await store.get(job_id) is None:
                    cleaned.set()

            monkeypatch.setattr(store, "cleanup", observed)
            await real_wait_for(idle.wait(), 5)
            assert (await store.get(job_id)).state == "failed"
            now += timedelta(seconds=61)
            tick.set()  # Явно наступивший tick, без реальной задержки/гонки часов.
            await real_wait_for(cleaned.wait(), 5)
            assert await store.get(job_id) is None and not transport.requests

    asyncio.run(scenario())


def test_post_during_async_empty_query_does_not_lose_wakeup(app_factory, monkeypatch):
    async def scenario():
        queried, release_query, entered, release_provider = (asyncio.Event() for _ in range(4))

        async def handler(request):
            entered.set()
            await release_provider.wait()
            return httpx.Response(200, json=envelope())

        app, _, _ = app_factory(RecordingTransport(handler))
        async with async_client(app) as client:
            worker = app.state.generation_jobs
            has_queued = worker.store.has_queued
            first = True

            async def stale_query():
                nonlocal first
                if first:
                    first = False
                    queried.set()
                    await release_query.wait()
                    return False  # Снимок SELECT до COMMIT конкурирующего POST.
                return await has_queued()

            monkeypatch.setattr(worker.store, "has_queued", stale_query)
            event = completion_event(app, monkeypatch)
            await asyncio.wait_for(queried.wait(), 5)
            accepted = await client.post(JOBS_PATH, headers=HEADERS, json=example_request())
            assert accepted.status_code == 202
            release_query.set()
            await asyncio.wait_for(entered.wait(), 5)
            release_provider.set()
            assert (await finished(app, accepted.json()["id"], event)).state == "completed"

    asyncio.run(scenario())


def test_terminal_commit_between_cleanup_and_select_does_not_leak_expired_data(
    app_factory, monkeypatch
):
    async def scenario():
        app, _, _ = app_factory(generation_jobs_retention_seconds=60)
        async with async_client(app) as client:
            event = completion_event(app, monkeypatch)
            accepted = await client.post(JOBS_PATH, headers=HEADERS, json=example_request())
            job_id = accepted.json()["id"]
            await finished(app, job_id, event)
            worker = app.state.generation_jobs
            get = worker.store.get

            async def just_committed(current_id):
                # Cleanup уже закончен, но SELECT видит более поздний COMMIT.
                worker.store.rows[job_id]["finished_at"] = datetime.now(UTC) - timedelta(seconds=61)
                return await get(current_id)

            monkeypatch.setattr(worker.store, "get", just_committed)
            response = await client.get(f"{JOBS_PATH}/{job_id}", headers=HEADERS)
            assert response.status_code == 404
            assert job_id not in worker.store.rows

    asyncio.run(scenario())


def test_get_rechecks_worker_after_await(app_factory, monkeypatch, caplog):
    async def scenario():
        entered, crash, selected, resume = (asyncio.Event() for _ in range(4))
        app, transport, _ = app_factory()
        async with async_client(app) as client:
            worker = app.state.generation_jobs

            async def broken_query():
                entered.set()
                await crash.wait()
                raise RuntimeError("private-sql-error")

            monkeypatch.setattr(worker.store, "has_queued", broken_query)
            accepted = await client.post(JOBS_PATH, headers=HEADERS, json=example_request())
            job_id = accepted.json()["id"]
            await asyncio.wait_for(entered.wait(), 5)
            get = worker.store.get

            async def paused_get(current_id):
                job = await get(current_id)
                selected.set()
                await resume.wait()
                return job

            monkeypatch.setattr(worker.store, "get", paused_get)
            polling = asyncio.create_task(client.get(f"{JOBS_PATH}/{job_id}", headers=HEADERS))
            try:
                await asyncio.wait_for(selected.wait(), 5)
                crash.set()
                await asyncio.wait_for(worker.task, 5)
                resume.set()
                response = await asyncio.wait_for(polling, 5)
                assert response.status_code == 503
                assert response.json()["detail"]["code"] == "worker_unavailable"
                assert (await get(job_id)).state == "queued" and not transport.requests
            finally:
                resume.set()
                crash.set()
                polling.cancel()
                await asyncio.gather(polling, return_exceptions=True)

    asyncio.run(scenario())
    assert "worker_unavailable" in caplog.text and "private-sql-error" not in caplog.text


@pytest.mark.parametrize("shutdown", [False, True])
@pytest.mark.parametrize("write_failure", [False, True])
def test_worker_cancellation_and_shutdown_release_resources(
    app_factory, monkeypatch, caplog, shutdown, write_failure
):
    async def scenario():
        entered, closed = asyncio.Event(), asyncio.Event()

        class WaitingStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                entered.set()
                await asyncio.Event().wait()
                yield b""

            async def aclose(self):
                closed.set()

        async def handler(request):
            return httpx.Response(200, stream=WaitingStream())

        app, transport, providers = app_factory(RecordingTransport(handler))
        async with async_client(app) as client:
            worker = app.state.generation_jobs
            running = (
                await client.post(JOBS_PATH, headers=HEADERS, json=example_request())
            ).json()["id"]
            await asyncio.wait_for(entered.wait(), 5)
            queued = (await client.post(JOBS_PATH, headers=HEADERS, json=example_request())).json()[
                "id"
            ]
            if write_failure:

                async def broken_fail(*args):
                    raise RuntimeError("private-interruption-write-error")

                monkeypatch.setattr(worker.store, "fail", broken_fail)
            if shutdown:
                await asyncio.wait_for(worker.stop(), 5)
            else:
                worker.task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(worker.task, 5)
            assert not worker.available and not worker.slot.busy and not worker.admission.locked()
            assert closed.is_set() and not providers[0]._client.is_closed
            persisted = await worker.store.get(running)
            if write_failure:
                assert persisted.state == "running" and persisted.error_code is None
            else:
                assert persisted.error_code == "interrupted"
            assert (await worker.store.get(queued)).state == "queued"
            response = await client.get(f"{JOBS_PATH}/{queued}", headers=HEADERS)
            assert response.status_code == 503
            assert response.json()["detail"]["code"] == "worker_unavailable"
            store = worker.store
        assert store.closed and transport.closed and providers[0]._client.is_closed
        assert len(transport.requests) == 1

    asyncio.run(scenario())
    events = [
        record.getMessage() for record in caplog.records if record.name.endswith("generation_jobs")
    ]
    expected = ["worker_interruption_storage_unavailable"] if write_failure else []
    if shutdown and write_failure:
        expected.extend(["worker_shutdown_storage_unavailable"] * 2)
    elif not shutdown:
        expected.append("worker_unavailable")
        if write_failure:
            expected.append("worker_shutdown_storage_unavailable")
    assert events == expected
    assert "private-interruption-write-error" not in caplog.text


def test_cancelled_admission_does_not_deadlock_shutdown(app_factory, monkeypatch):
    async def scenario():
        entered = asyncio.Event()
        app, transport, _ = app_factory()
        async with async_client(app) as client:
            worker = app.state.generation_jobs

            async def blocked_create(*args):
                entered.set()
                await asyncio.Event().wait()

            monkeypatch.setattr(worker.store, "create", blocked_create)
            post = asyncio.create_task(
                client.post(JOBS_PATH, headers=HEADERS, json=example_request())
            )
            stop = None
            try:
                await asyncio.wait_for(entered.wait(), 5)
                stop = asyncio.create_task(worker.stop())
                post.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await post
                await asyncio.wait_for(stop, 5)
                assert not worker.available and not worker.admission.locked()
                assert worker.task.done() and not worker.slot.busy and not transport.requests
            finally:
                post.cancel()
                if stop is not None:
                    await asyncio.gather(stop, return_exceptions=True)
                await asyncio.gather(post, return_exceptions=True)

    asyncio.run(scenario())
