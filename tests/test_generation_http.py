import asyncio
import copy
import json
import logging
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi.testclient import TestClient

from meal_budget import main
from meal_budget.api.generation_guard import MAX_REQUEST_BYTES
from meal_budget.api.generations import GENERATION_PATH
from meal_budget.core.config import PROJECT_ROOT, Settings
from meal_budget.schemas.generation import GeneratedPlan
from meal_budget.services.deepseek import DeepSeekProvider

ACCESS_TOKEN = "developer-test-token"
PROVIDER_KEY = "provider-test-key"
HEADERS = {"Authorization": f"Bearer {ACCESS_TOKEN}"}


def example_request():
    return json.loads((PROJECT_ROOT / "examples/generation-request.json").read_text())


def example_plan():
    return {
        "recipes": [
            {
                "id": "oats-demo",
                "name": "Технический пример",
                "servings": 1,
                "ingredients": [{"product_id": "oats", "grams": 100}],
                "steps": ["Приготовить"],
                "cooking_minutes": 10,
            }
        ],
        "days": [{"day": 1, "meals": [{"recipe_id": "oats-demo", "servings": 1}]}],
    }


def envelope(plan=None):
    return {
        "model": "test-model",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps(example_plan() if plan is None else plan),
                    "reasoning_content": "private-reasoning",
                },
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    }


class RecordingTransport(httpx.MockTransport):
    def __init__(self, handler=None):
        self.requests = []
        self.closed = False
        self.reply_handler = handler
        super().__init__(self.respond)

    async def respond(self, request):
        self.requests.append(request)
        reply = (
            await self.reply_handler(request)
            if self.reply_handler
            else httpx.Response(200, json=envelope())
        )
        if reply.is_stream_consumed:
            return httpx.Response(
                reply.status_code, headers=reply.headers, stream=httpx.ByteStream(reply.content)
            )
        return reply

    async def aclose(self):
        self.closed = True
        await super().aclose()


@pytest.fixture
def app_factory(monkeypatch):
    def factory(transport=None, **overrides):
        transport = transport or RecordingTransport()
        providers = []

        def provider(settings):
            instance = DeepSeekProvider(settings, transport=transport)
            providers.append(instance)
            return instance

        monkeypatch.setattr(main, "DeepSeekProvider", provider)
        settings = Settings(
            _env_file=None,
            **{
                "generation_http_enabled": True,
                "generation_access_token": ACCESS_TOKEN,
                "deepseek_api_key": PROVIDER_KEY,
                "deepseek_model": "test-model",
                "deepseek_base_url": "http://provider.test",
                **overrides,
            },
        )
        return main.create_app(settings), transport, providers

    return factory


@asynccontextmanager
async def async_client(app, *, root_path=""):
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, root_path=root_path), base_url="http://test"
        ) as client,
    ):
        yield client


def test_disabled_by_default(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Disabled endpoint must not create a provider")

    monkeypatch.setattr(main, "DeepSeekProvider", forbidden)
    with TestClient(main.create_app(Settings(_env_file=None, deepseek_api_key=""))) as disabled:
        assert disabled.post(GENERATION_PATH, json=example_request()).status_code == 404
        assert GENERATION_PATH not in disabled.get("/openapi.json").json()["paths"]
        assert disabled.get("/health").status_code == 200
        assert disabled.get("/v1/products").status_code == 200
        menu = json.loads((PROJECT_ROOT / "examples/menu-calculation.json").read_text())
        assert disabled.post("/v1/menu-calculations", json=menu).status_code == 200


@pytest.mark.parametrize("token", ["", " ", "abc def", "\nsecret", "секрет"])
def test_enabled_requires_token(token):
    with pytest.raises(RuntimeError, match="GENERATION_ACCESS_TOKEN") as exc:
        main.create_app(
            Settings(_env_file=None, generation_http_enabled=True, generation_access_token=token)
        )
    assert "secret" not in str(exc.value)


@pytest.mark.parametrize(
    "authorization", [None, "Bearer wrong", f"Bearer {PROVIDER_KEY}", "Basic abc", "Bearer"]
)
def test_unauthorized_before_body_and_provider(app_factory, authorization, caplog):
    app, transport, _ = app_factory()
    headers = {} if authorization is None else {"Authorization": authorization}
    with caplog.at_level(logging.DEBUG), TestClient(app) as client:
        result = client.post(GENERATION_PATH, headers=headers, content=b"{" * MAX_REQUEST_BYTES)
    assert result.status_code == 401
    assert result.headers["www-authenticate"] == "Bearer"
    assert result.json()["detail"]["code"] == "unauthorized"
    assert not transport.requests
    assert ACCESS_TOKEN not in caplog.text
    assert PROVIDER_KEY not in caplog.text
    assert "Authorization" not in caplog.text


def test_success_reuse_and_lifespan(app_factory):
    app, transport, providers = app_factory()
    with TestClient(app) as client:
        for _ in range(2):
            result = client.post(GENERATION_PATH, headers=HEADERS, json=example_request())
            assert result.status_code == 200
            body = result.json()
            expected_plan = GeneratedPlan.model_validate(example_plan()).model_dump(mode="json")
            assert body["plan"] == expected_plan
            assert body["calculation"]["total_cost"] == 89.9
            assert body["calculation"]["within_budget"] is True
            assert body["calculation"]["data_status"] == "demo_unverified"
            assert body["calculation"]["warning"] in body["warnings"]
            assert body["metadata"]["calls"] == 1
            assert body["metadata"]["responses"][0]["usage"]["total_tokens"] == 3
            assert "private-reasoning" not in result.text
            assert ACCESS_TOKEN not in result.text
            assert PROVIDER_KEY not in result.text
        assert len(providers) == 1
        assert not providers[0]._client.is_closed
        assert not transport.closed
        schema = client.get("/openapi.json").json()
        assert schema["paths"][GENERATION_PATH]["post"]["security"] == [
            {"GenerationAccessToken": []}
        ]
        assert schema["components"]["securitySchemes"]["GenerationAccessToken"] == {
            "type": "http",
            "scheme": "bearer",
        }
    assert transport.closed
    assert providers[0]._client.is_closed
    assert app.state.generation_service is None


@pytest.mark.parametrize(
    "kind,status,code,calls",
    [
        ("authorization", 503, "provider_authorization", 1),
        ("forbidden", 503, "provider_authorization", 1),
        ("rate", 503, "provider_rate_limit", 1),
        ("unavailable", 503, "provider_unavailable", 1),
        ("network", 503, "provider_unavailable", 1),
        ("timeout", 504, "provider_timeout", 1),
        ("deadline", 504, "generation_timeout", 1),
        ("invalid", 502, "invalid_response", 3),
        ("constraints", 422, "attempts_exhausted", 3),
    ],
)
def test_service_errors_and_slot_release(app_factory, kind, status, code, calls):
    failed = True
    stream_closed = asyncio.Event()

    class WaitingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.Event().wait()
            yield b""

        async def aclose(self):
            stream_closed.set()

    async def handler(request):
        if not failed:
            return httpx.Response(200, json=envelope())
        if kind == "network":
            raise httpx.ConnectError("private-provider-error")
        if kind == "timeout":
            raise httpx.ReadTimeout("private-provider-error")
        if kind == "deadline":
            return httpx.Response(200, stream=WaitingStream())
        if kind == "invalid":
            return httpx.Response(200, content=b"private-invalid-response")
        if kind == "constraints":
            bad = copy.deepcopy(example_plan())
            bad["recipes"][0]["ingredients"][0]["grams"] = 1
            return httpx.Response(200, json=envelope(bad))
        provider_status = {"authorization": 401, "forbidden": 403, "rate": 429, "unavailable": 500}
        return httpx.Response(provider_status[kind], content=b"private-provider-error")

    transport = RecordingTransport(handler)
    app, _, providers = app_factory(
        transport, generation_total_timeout=0.05 if kind == "deadline" else 180
    )
    with TestClient(app) as client:
        result = client.post(GENERATION_PATH, headers=HEADERS, json=example_request())
        assert result.status_code == status
        assert result.json()["detail"]["code"] == code
        assert result.json()["detail"]["metadata"]["calls"] == calls
        assert len(transport.requests) == calls
        assert "private-" not in result.text
        assert "plan" not in result.json()["detail"]
        if kind == "deadline":
            assert stream_closed.is_set()
        failed = False
        success = client.post(GENERATION_PATH, headers=HEADERS, json=example_request())
        assert success.status_code == 200
    assert providers[0]._client.is_closed


def test_enabled_without_provider_key_and_old_routes(app_factory):
    app, transport, _ = app_factory(deepseek_api_key="")
    with TestClient(app) as client:
        result = client.post(GENERATION_PATH, headers=HEADERS, json=example_request())
        assert result.status_code == 503
        assert result.json()["detail"]["code"] == "configuration_missing"
        assert client.get("/health").status_code == 200
        assert client.get("/v1/products").status_code == 200
        menu = json.loads((PROJECT_ROOT / "examples/menu-calculation.json").read_text())
        assert client.post("/v1/menu-calculations", json=menu).status_code == 200
    assert not transport.requests


@pytest.mark.parametrize("kind", ["schema", "json", "deep_json", "unknown_product"])
def test_invalid_request_without_provider(app_factory, kind):
    app, transport, _ = app_factory()
    payload = example_request()
    if kind == "schema":
        payload["budget"] = "private-invalid-budget"
    elif kind == "unknown_product":
        payload["disliked_product_ids"] = ["missing"]
    content = json.dumps(payload).encode()
    if kind == "json":
        content = b"private-invalid-json"
    elif kind == "deep_json":
        content = b"[" * 10000 + b"0" + b"]" * 10000
    with TestClient(app) as client:
        result = client.post(
            GENERATION_PATH,
            headers={**HEADERS, "Content-Type": "application/json"},
            content=content,
        )
        assert result.status_code == 422
        assert result.json()["detail"]["code"] == "invalid_request"
        assert "private-" not in result.text
        success = client.post(GENERATION_PATH, headers=HEADERS, json=example_request())
        assert success.status_code == 200
    assert len(transport.requests) == 1


def test_repairs_use_existing_three_call_budget(app_factory):
    async def handler(request):
        return httpx.Response(200, json=envelope({}) if len(transport.requests) < 3 else envelope())

    transport = RecordingTransport(handler)
    app, _, _ = app_factory(transport)
    with TestClient(app) as client:
        result = client.post(GENERATION_PATH, headers=HEADERS, json=example_request())
    assert result.status_code == 200
    assert result.json()["metadata"]["calls"] == 3
    assert len(transport.requests) == 3


@pytest.mark.parametrize("cancel", [False, True])
def test_concurrency_and_cancellation(app_factory, cancel):
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()
        closed = asyncio.Event()

        class BlockingStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                entered.set()
                await release.wait()
                yield json.dumps(envelope()).encode()

            async def aclose(self):
                closed.set()

        async def handler(request):
            if len(transport.requests) == 1:
                return httpx.Response(200, stream=BlockingStream())
            return httpx.Response(200, json=envelope())

        transport = RecordingTransport(handler)
        app, _, providers = app_factory(transport)
        async with async_client(app) as client:
            first = asyncio.create_task(
                client.post(GENERATION_PATH, headers=HEADERS, json=example_request())
            )
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                busy = await client.post(GENERATION_PATH, headers=HEADERS, json=example_request())
                assert busy.status_code == 429
                assert busy.json()["detail"]["code"] == "generation_busy"
                assert len(transport.requests) == 1
                assert (await client.get("/health")).status_code == 200
                assert (await client.get("/v1/products")).status_code == 200
                if cancel:
                    first.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await first
                else:
                    release.set()
                    assert (await first).status_code == 200
                assert closed.is_set()
                # Отменяется поток запроса, а не общий клиент lifespan.
                assert not providers[0]._client.is_closed
                result = await client.post(GENERATION_PATH, headers=HEADERS, json=example_request())
                assert result.status_code == 200
                assert len(transport.requests) == 2
            finally:
                release.set()
                if not first.done():
                    first.cancel()
                await asyncio.gather(first, return_exceptions=True)
        assert providers[0]._client.is_closed
        assert transport.closed

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "kind", ["length", "chunked", "lying_length", "huge_length", "zero_length", "boundary"]
)
def test_body_limit_before_json_and_slot_release(app_factory, kind):
    async def scenario():
        app, transport, _ = app_factory()
        sent = 0

        async def chunks():
            nonlocal sent
            for _ in range(4):
                sent += 1
                yield b"{" * (MAX_REQUEST_BYTES // 2)

        headers = {**HEADERS, "Content-Type": "application/json"}
        content = chunks()
        if kind == "length":
            headers["Content-Length"] = str(MAX_REQUEST_BYTES + 1)
        elif kind == "huge_length":
            headers["Content-Length"] = "9" * 5000
        elif kind == "lying_length":
            headers["Content-Length"] = "1"
        elif kind == "zero_length":
            headers["Content-Length"] = "0" * 5000
        elif kind == "boundary":
            raw = json.dumps(example_request()).encode()
            content = raw + b" " * (MAX_REQUEST_BYTES - len(raw))
        async with async_client(app) as client:
            result = await client.post(GENERATION_PATH, headers=headers, content=content)
            assert result.status_code == (200 if kind == "boundary" else 413)
            if kind != "boundary":
                assert result.json()["detail"]["code"] == "request_too_large"
                assert not transport.requests
                assert sent == (0 if kind in ("length", "huge_length") else 3)
            assert (
                await client.post(GENERATION_PATH, headers=HEADERS, json=example_request())
            ).status_code == 200

    asyncio.run(scenario())


@pytest.mark.parametrize("authorization", [False, True])
def test_rejections_do_not_read_body(app_factory, authorization):
    async def scenario():
        app, transport, _ = app_factory()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow_body():
            entered.set()
            await release.wait()
            yield json.dumps(example_request()).encode()

        async def unread_body():
            pytest.fail("Rejected request body must not be read")
            yield b""

        async with async_client(app) as client:
            first = asyncio.create_task(
                client.post(GENERATION_PATH, headers=HEADERS, content=slow_body())
            )
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                result = await client.post(
                    GENERATION_PATH, headers=HEADERS if authorization else {}, content=unread_body()
                )
                assert result.status_code == (429 if authorization else 401)
                assert not transport.requests
                first.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await first
                assert (
                    await client.post(GENERATION_PATH, headers=HEADERS, json=example_request())
                ).status_code == 200
            finally:
                first.cancel()
                await asyncio.gather(first, return_exceptions=True)

    asyncio.run(scenario())
