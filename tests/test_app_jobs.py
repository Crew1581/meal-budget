import asyncio
import logging
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_generation_http import HEADERS, async_client, example_request
from test_generation_http import app_factory as app_factory
from test_generation_jobs import completion_event, finished

from main import create_app
from meal_budget.api import app_jobs
from meal_budget.api.generation_guard import GenerationGuard
from meal_budget.api.generations import GENERATION_PATH, JOBS_PATH
from meal_budget.core.config import Settings
from meal_budget.schemas.generation import GenerationRequest
from meal_budget.services.app_admission import (
    APP_JOBS_PATH,
    INSTALLATIONS_PATH,
    request_hash,
    token_hash,
)


def test_token_uses_32_random_bytes(app_factory, monkeypatch):
    calls = []
    generate = app_jobs.secrets.token_urlsafe

    def observed(nbytes):
        calls.append(nbytes)
        return generate(nbytes)

    monkeypatch.setattr(app_jobs.secrets, "token_urlsafe", observed)
    app, _, _ = app_factory(generation_app_enabled=True)
    with TestClient(app) as client:
        assert client.post(INSTALLATIONS_PATH).status_code == 201
    assert calls == [32]


async def installation(client, root=""):
    response = await client.post(root + INSTALLATIONS_PATH)
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    UUID(body["installation_id"])
    assert body["token_type"] == "Bearer" and len(body["token"]) == 47
    return body, {"Authorization": "Bearer " + body["token"], "Idempotency-Key": "test-1"}


def test_canonical_hash():
    original = example_request()
    changed = dict(reversed(list(original.items())))
    changed["budget"] = "100.00"
    original["budget"] = 100
    changed["wishes"] = "  test  "
    original["wishes"] = "test"
    original["daily_targets"]["calories_kcal"] = Decimal("2E+3")
    changed["daily_targets"] = {**original["daily_targets"], "calories_kcal": "2000.00"}
    original["excluded_product_ids"] = ["oats", "chicken", "oats"]
    changed["excluded_product_ids"] = ["chicken", "oats"]
    first = request_hash(GenerationRequest.model_validate(original))
    assert first == request_hash(GenerationRequest.model_validate(changed))
    changed["wishes"] = "other"
    assert first != request_hash(GenerationRequest.model_validate(changed))
    assert len(first) == 32
    # Малый Decimal exponent не должен разворачиваться в гигантскую строку/float.
    changed["daily_targets"]["protein_g"] = "1e-1000000"
    assert len(request_hash(GenerationRequest.model_validate(changed))) == 32


def test_disabled_settings_and_openapi(app_factory):
    with pytest.raises(RuntimeError, match="GENERATION_HTTP_ENABLED"):
        create_app(Settings(_env_file=None, generation_app_enabled=True))
    for enabled in (False, True):
        app, _, _ = app_factory(generation_app_enabled=enabled)
        with TestClient(app) as client:
            schema = client.get("/openapi.json").json()
            if enabled:
                assert schema["paths"][APP_JOBS_PATH]["post"]["security"] == [
                    {"InstallationToken": []}
                ]
                assert "security" not in schema["paths"][INSTALLATIONS_PATH]["post"]
            else:
                for path in (INSTALLATIONS_PATH, APP_JOBS_PATH):
                    assert path not in schema["paths"]
                    assert client.post(path).status_code == 404
                assert (
                    client.post(JOBS_PATH, headers=HEADERS, json=example_request()).status_code
                    == 202
                )


def test_tokens_ownership_replay_and_no_secrets(app_factory, monkeypatch, caplog):
    async def scenario():
        app, transport, _ = app_factory(generation_app_enabled=True)
        async with async_client(app) as client:
            event = completion_event(app, monkeypatch)
            first, headers = await installation(client)
            second, other = await installation(client)
            assert first["token"] != second["token"]
            store = app.state.generation_jobs.store
            assert store.installations[first["installation_id"]] == token_hash(first["token"])
            for bad in (
                {},
                HEADERS,
                {"Authorization": "Bearer wrong"},
                {"Authorization": b"Bearer \xff"},
            ):
                response = await client.post(APP_JOBS_PATH, headers=bad, content=b"{" * 65537)
                assert response.status_code == 401
                assert response.headers["www-authenticate"] == "Bearer"
                assert first["token"] not in response.text
            for path in (GENERATION_PATH, JOBS_PATH):
                assert (
                    await client.post(path, headers=headers, json=example_request())
                ).status_code == 401
            accepted = await client.post(APP_JOBS_PATH, headers=headers, json=example_request())
            assert accepted.status_code == 202
            job_id = accepted.json()["id"]
            await finished(app, job_id, event)
            for auth in (headers, other):
                for foreign in (job_id if auth is other else str(uuid4()), str(uuid4())):
                    response = await client.get(APP_JOBS_PATH + "/" + foreign, headers=auth)
                    assert response.status_code == 404
            response = await client.get(APP_JOBS_PATH + "/" + job_id, headers=headers)
            assert response.json()["state"] == "completed" and first["token"] not in response.text
            assert (await client.get(JOBS_PATH + "/" + job_id, headers=headers)).status_code == 401
            again = await client.post(APP_JOBS_PATH, headers=headers, json=example_request())
            assert again.json() == accepted.json()
            changed = {**example_request(), "wishes": "private-wishes"}
            conflict = await client.post(APP_JOBS_PATH, headers=headers, json=changed)
            assert conflict.status_code == 409 and "private-wishes" not in conflict.text
            assert store.usage[first["installation_id"]] == 1
            assert len(transport.requests) == 1
            assert first["token"] not in caplog.text and second["token"] not in caplog.text

    with caplog.at_level(logging.DEBUG):
        asyncio.run(scenario())


@pytest.mark.parametrize("key", [None, "", "a" * 65, "bad key", "private/key", "bad\nkey"])
def test_key_validation_without_leaks(app_factory, key):
    async def scenario():
        app, transport, _ = app_factory(generation_app_enabled=True)
        async with async_client(app) as client:
            _, headers = await installation(client)
            headers.pop("Idempotency-Key")
            if key is not None:
                headers["Idempotency-Key"] = key
            response = await client.post(APP_JOBS_PATH, headers=headers, json=example_request())
            assert response.status_code == 422
            assert not app.state.generation_jobs.store.rows and not transport.requests
            if key and "private" in key:
                assert key not in response.text

    asyncio.run(scenario())


@pytest.mark.parametrize("root", ["", "/api", "/gateway/api"])
@pytest.mark.parametrize("slash", ["", "/"])
def test_root_body_limits_and_safe_errors(app_factory, root, slash):
    async def scenario():
        app, transport, _ = app_factory(generation_app_enabled=True)
        async with async_client(app, root_path=root) as client:
            _, headers = await installation(client, root)

            async def chunks():
                yield b"{" * 32768
                yield b"{" * 32769

            for path, auth in ((INSTALLATIONS_PATH, {}), (APP_JOBS_PATH, headers)):
                for content in (b"{" * 65537, chunks()):
                    response = await client.post(root + path + slash, headers=auth, content=content)
                    assert response.status_code == 413
                    assert response.headers["cache-control"] == "no-store"
            invalid = await client.post(
                root + APP_JOBS_PATH, headers=headers, content=b"private-invalid"
            )
            assert invalid.status_code == 422 and "private-invalid" not in invalid.text
            invalid_id = await client.get(root + APP_JOBS_PATH + "/private-id", headers=headers)
            assert invalid_id.status_code == 422 and "private-id" not in invalid_id.text
            duplicate = [
                ("Authorization", headers["Authorization"]),
                ("Idempotency-Key", "a"),
                ("Idempotency-Key", "b"),
            ]
            assert (
                await client.post(root + APP_JOBS_PATH, headers=duplicate, json=example_request())
            ).status_code == 422
            assert not transport.requests
            redirect = await client.post(
                root + APP_JOBS_PATH + "/", headers=headers, json=example_request()
            )
            assert redirect.status_code == 307
            assert httpx.URL(redirect.headers["location"]).path == root + APP_JOBS_PATH

    asyncio.run(scenario())


def test_nested_mount(app_factory):
    async def scenario():
        app, _, _ = app_factory(generation_app_enabled=True)
        inner, outer = FastAPI(), FastAPI()
        inner.mount("/inner", app)
        outer.mount("/outer", inner)
        async with (
            app.router.lifespan_context(app),
            async_client(outer, root_path="/api") as client,
        ):
            root = "/api/outer/inner"
            _, headers = await installation(client, root)
            assert (
                await client.post(root + APP_JOBS_PATH, headers=headers, json=example_request())
            ).status_code == 202

    asyncio.run(scenario())


def test_app_fails_closed_without_guard(app_factory, monkeypatch):
    async def bypass(self, scope, receive, send):
        await self.app(scope, receive, send)

    monkeypatch.setattr(GenerationGuard, "__call__", bypass)
    app, transport, _ = app_factory(generation_app_enabled=True)
    with TestClient(app) as client:
        assert client.post(INSTALLATIONS_PATH).status_code == 401
        assert (
            client.post(
                APP_JOBS_PATH, headers={"Idempotency-Key": "a"}, json=example_request()
            ).status_code
            == 401
        )
        assert client.get(APP_JOBS_PATH + "/" + str(uuid4())).status_code == 401
    assert not transport.requests


@pytest.mark.parametrize("global_limit", [False, True])
def test_quota_and_registration_api_codes(app_factory, monkeypatch, global_limit):
    async def scenario():
        app, _, _ = app_factory(
            generation_app_enabled=True,
            generation_app_daily_installation_limit=1,
            generation_app_daily_global_limit=1 if global_limit else 10,
            installations_daily_limit=2,
        )
        async with async_client(app) as client:
            event = completion_event(app, monkeypatch)
            _, headers = await installation(client)
            await installation(client)
            limited = await client.post(INSTALLATIONS_PATH)
            assert limited.status_code == 429
            assert limited.json()["detail"]["code"] == "installation_daily_quota_exceeded"
            assert int(limited.headers["retry-after"]) > 0
            accepted = await client.post(APP_JOBS_PATH, headers=headers, json=example_request())
            await finished(app, accepted.json()["id"], event)
            rejected = await client.post(
                APP_JOBS_PATH, headers={**headers, "Idempotency-Key": "new"}, json=example_request()
            )
            assert rejected.status_code == 429 and int(rejected.headers["retry-after"]) > 0
            expected = (
                "app_daily_quota_exceeded"
                if global_limit
                else "installation_daily_job_quota_exceeded"
            )
            assert rejected.json()["detail"]["code"] == expected

    asyncio.run(scenario())


def test_storage_errors_are_secret_free(app_factory, monkeypatch, caplog):
    async def scenario():
        app, _, _ = app_factory(generation_app_enabled=True)
        async with async_client(app) as client:
            issued, headers = await installation(client)
            store = app.state.generation_jobs.store

            async def broken(*args, **kwargs):
                raise RuntimeError(issued["token"] + " private-data")

            for method, path, body in (
                ("register_installation", INSTALLATIONS_PATH, {}),
                ("create_app", APP_JOBS_PATH, example_request()),
                ("get", APP_JOBS_PATH + "/" + str(uuid4()), None),
                ("authenticate_installation", APP_JOBS_PATH, example_request()),
            ):
                with monkeypatch.context() as patch:
                    patch.setattr(store, method, broken)
                    response = await client.request(
                        "GET" if body is None else "POST", path, headers=headers, json=body
                    )
                    assert response.status_code == 503
                    assert issued["token"] not in response.text + caplog.text
                    assert "private-data" not in response.text + caplog.text

    with caplog.at_level(logging.DEBUG):
        asyncio.run(scenario())
