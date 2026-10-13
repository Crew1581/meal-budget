import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette._utils import get_route_path
from starlette.routing import Match, Route
from test_generation_http import (
    HEADERS,
    RecordingTransport,
    async_client,
    envelope,
    example_request,
)
from test_generation_http import app_factory as app_factory

from main import create_app
from meal_budget.api.generation_guard import MAX_REQUEST_BYTES, GenerationGuard
from meal_budget.api.generations import GENERATION_PATH, GUARDED_REQUEST_KEY
from meal_budget.core.config import PROJECT_ROOT, Settings

ROOT_PATHS = ["", "/api", "/gateway/api"]


@pytest.fixture(params=ROOT_PATHS)
def root_path(request):
    return request.param


@pytest.mark.parametrize(
    "root,path,expected",
    [
        ("", GENERATION_PATH, GENERATION_PATH),
        ("/api", "/api" + GENERATION_PATH, GENERATION_PATH),
        ("/gateway/api", "/gateway/api" + GENERATION_PATH, GENERATION_PATH),
        ("/api", GENERATION_PATH, GENERATION_PATH),
        ("/api", "/apix" + GENERATION_PATH, "/apix" + GENERATION_PATH),
        ("/gateway/api", "/gateway/apix" + GENERATION_PATH, "/gateway/apix" + GENERATION_PATH),
        ("/api", "/other/api" + GENERATION_PATH, "/other/api" + GENERATION_PATH),
        ("/api", "/api/api" + GENERATION_PATH, "/api" + GENERATION_PATH),
        ("/api", "/api", ""),
        ("/api", "/api/", "/"),
    ],
)
def test_effective_path_matches_starlette_routing(root, path, expected):
    scope = {"type": "http", "method": "POST", "path": path, "root_path": root}
    assert get_route_path(scope) == expected
    route = Route(GENERATION_PATH, endpoint=lambda request: None, methods=["POST"])
    assert (route.matches(scope)[0] == Match.FULL) == (expected == GENERATION_PATH)


@pytest.mark.parametrize("slash", ["", "/"])
@pytest.mark.parametrize("authorization", [None, "Bearer wrong", b"Bearer \xff"])
def test_root_path_auth_before_body(app_factory, root_path, slash, authorization):
    app, transport, _ = app_factory()
    headers = {} if authorization is None else {"Authorization": authorization}
    with TestClient(app, root_path=root_path) as client:
        result = client.post(
            root_path + GENERATION_PATH + slash,
            headers=headers,
            content=b"{" * (MAX_REQUEST_BYTES + 1),
            follow_redirects=False,
        )
    assert result.status_code == 401
    assert result.json()["detail"]["code"] == "unauthorized"
    assert result.headers["www-authenticate"] == "Bearer"
    assert not transport.requests


@pytest.mark.parametrize("slash", ["", "/"])
@pytest.mark.parametrize("chunked", [False, True])
def test_root_path_body_limit(app_factory, root_path, slash, chunked):
    async def scenario():
        app, transport, _ = app_factory()

        async def chunks():
            for _ in range(3):
                yield b"{" * 32768

        async with async_client(app, root_path=root_path) as client:
            result = await client.post(
                root_path + GENERATION_PATH + slash,
                headers=HEADERS,
                content=chunks() if chunked else b"{" * (MAX_REQUEST_BYTES + 1),
                follow_redirects=False,
            )
            assert result.status_code == 413
            assert result.json()["detail"]["code"] == "request_too_large"
            assert not transport.requests
            assert (
                await client.post(
                    root_path + GENERATION_PATH, headers=HEADERS, json=example_request()
                )
            ).status_code == 200

    asyncio.run(scenario())


def test_root_path_success_redirect_and_safe_errors(app_factory, root_path):
    app, transport, _ = app_factory()
    path = root_path + GENERATION_PATH
    with TestClient(app, root_path=root_path) as client:
        redirect = client.post(
            path + "/", headers=HEADERS, json=example_request(), follow_redirects=False
        )
        assert redirect.status_code == 307
        assert httpx.URL(redirect.headers["location"]).path == path
        assert not transport.requests
        # Редирект не переносит подтверждение guard: канонический POST защищается заново.
        assert client.post(redirect.headers["location"], json=example_request()).status_code == 401
        success = client.post(redirect.headers["location"], headers=HEADERS, json=example_request())
        assert success.status_code == 200
        assert success.json()["calculation"]["data_status"] == "demo_unverified"
        assert success.json()["calculation"]["total_cost"] == 89.9
        assert len(transport.requests) == 1

        bad = example_request()
        bad["budget"] = "private-invalid-budget"
        for content in (
            json.dumps(bad).encode(),
            b"private-invalid-json",
            b"[" * 10000 + b"]" * 10000,
        ):
            invalid = client.post(
                path, headers={**HEADERS, "Content-Type": "application/json"}, content=content
            )
            assert invalid.status_code == 422
            assert invalid.json()["detail"]["code"] == "invalid_request"
            assert "private-" not in invalid.text
            assert "input" not in invalid.text
            assert len(transport.requests) == 1
        assert client.post(path, headers=HEADERS, json=example_request()).status_code == 200


@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
def test_root_path_busy_and_slot_release(app_factory, root_path, outcome):
    async def scenario():
        entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class BlockingStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                entered.set()
                await release.wait()
                if outcome == "error":
                    raise httpx.ReadTimeout("private-provider-timeout")
                yield json.dumps(envelope()).encode()

            async def aclose(self):
                closed.set()

        async def handler(request):
            if len(transport.requests) == 1:
                return httpx.Response(200, stream=BlockingStream())
            return httpx.Response(200, json=envelope())

        transport = RecordingTransport(handler)
        app, _, providers = app_factory(transport)
        path = root_path + GENERATION_PATH
        async with async_client(app, root_path=root_path) as client:
            first = asyncio.create_task(client.post(path, headers=HEADERS, json=example_request()))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                for suffix in ("", "/"):
                    busy = await client.post(path + suffix, headers=HEADERS, json=example_request())
                    assert busy.status_code == 429
                    assert busy.json()["detail"]["code"] == "generation_busy"
                assert len(transport.requests) == 1
                if outcome == "cancel":
                    first.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await first
                else:
                    release.set()
                    result = await first
                    assert result.status_code == (200 if outcome == "success" else 504)
                    assert "private-" not in result.text
                assert closed.is_set()
                assert not providers[0]._client.is_closed
                assert (
                    await client.post(path, headers=HEADERS, json=example_request())
                ).status_code == 200
                assert len(transport.requests) == 2
            finally:
                release.set()
                first.cancel()
                await asyncio.gather(first, return_exceptions=True)
        assert providers[0]._client.is_closed

    asyncio.run(scenario())


@pytest.mark.parametrize("proof", ["absent", "boolean", "other_scope"])
def test_endpoint_fails_closed_without_guard(app_factory, monkeypatch, root_path, proof):
    async def bypass(self, scope, receive, send):
        if scope["type"] == "http":
            if proof == "boolean":
                scope[GUARDED_REQUEST_KEY] = True
            elif proof == "other_scope":
                scope[GUARDED_REQUEST_KEY] = dict(scope)
        await self.app(scope, receive, send)

    monkeypatch.setattr(GenerationGuard, "__call__", bypass)
    app, transport, _ = app_factory()
    with TestClient(app, root_path=root_path) as client:
        for headers in ({}, HEADERS):
            result = client.post(
                root_path + GENERATION_PATH, headers=headers, json=example_request()
            )
            assert result.status_code == 401
            assert result.json()["detail"]["code"] == "unauthorized"
    assert not transport.requests


def test_root_path_old_routes_without_keys(root_path):
    app = create_app(Settings(_env_file=None, deepseek_api_key="", generation_http_enabled=False))
    with TestClient(app, root_path=root_path) as client:
        assert client.get(root_path + "/health").status_code == 200
        assert client.get(root_path + "/v1/products").status_code == 200
        assert client.get(root_path + "/openapi.json").status_code == 200
        menu = json.loads((PROJECT_ROOT / "examples/menu-calculation.json").read_text())
        assert client.post(root_path + "/v1/menu-calculations", json=menu).status_code == 200
        menu["budget"] = "private-invalid-budget"
        invalid = client.post(root_path + "/v1/menu-calculations", json=menu)
        assert invalid.status_code == 422
        assert "private-invalid-budget" not in invalid.text
        assert "input" not in invalid.text
        assert client.post(root_path + GENERATION_PATH, json=example_request()).status_code == 404


def test_nested_mount_uses_accumulated_root_path(app_factory):
    async def scenario():
        app, transport, _ = app_factory()
        inner, outer = FastAPI(), FastAPI()
        inner.mount("/inner", app)
        outer.mount("/outer", inner)
        async with (
            app.router.lifespan_context(app),
            async_client(outer, root_path="/api") as client,
        ):
            path = "/api/outer/inner" + GENERATION_PATH
            assert (await client.post(path, json=example_request())).status_code == 401
            assert not transport.requests
            assert (
                await client.post(path, headers=HEADERS, json=example_request())
            ).status_code == 200
            assert len(transport.requests) == 1
            invalid = await client.post(path, headers=HEADERS, json={"private": "input"})
            assert invalid.status_code == 422
            assert invalid.json()["detail"]["code"] == "invalid_request"
            assert "private" not in invalid.text

    asyncio.run(scenario())
