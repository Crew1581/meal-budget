import asyncio
import os
import select
import socket
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from test_generation_http import (
    ACCESS_TOKEN,
    GENERATION_PATH,
    HEADERS,
    PROVIDER_KEY,
    async_client,
    envelope,
    example_request,
)

from main import create_app
from meal_budget.core.config import PROJECT_ROOT, Settings


@contextmanager
def local_server(app, *, root_path=""):
    ready = threading.Event()

    class TestServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            ready.set()

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = TestServer(
        uvicorn.Config(app, log_level="error", access_log=False, workers=1, root_path=root_path)
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        assert ready.wait(5), "Local Uvicorn startup timed out"
        assert server.started
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(5)
        sock.close()
        assert not thread.is_alive(), "Local Uvicorn did not shut down"


def live_settings(provider_url):
    return Settings(
        _env_file=None,
        generation_http_enabled=True,
        generation_access_token=ACCESS_TOKEN,
        deepseek_api_key=PROVIDER_KEY,
        deepseek_model="test-model",
        deepseek_base_url=provider_url,
    )


@pytest.mark.parametrize("root_path", ["", "/api", "/gateway/api"])
def test_live_http_with_local_provider(tmp_path, root_path):
    provider_app = FastAPI()
    calls = []

    @provider_app.post("/chat/completions")
    async def completion(request: Request):
        assert request.headers["authorization"] == f"Bearer {PROVIDER_KEY}"
        calls.append(await request.json())
        return envelope()

    with local_server(provider_app) as provider_url:
        app = create_app(live_settings(provider_url))
        with (
            local_server(app, root_path=root_path) as api_url,
            httpx.Client(base_url=api_url, trust_env=False) as client,
        ):
            assert client.get("/health").status_code == 200
            assert client.get("/v1/products").status_code == 200
            assert client.post(GENERATION_PATH, json=example_request()).status_code == 401
            for authorization in ("Bearer wrong", b"Bearer \xff"):
                invalid_token = client.post(
                    GENERATION_PATH,
                    headers={"Authorization": authorization},
                    json=example_request(),
                )
                assert invalid_token.status_code == 401
                assert invalid_token.json()["detail"]["code"] == "unauthorized"
            assert client.post(GENERATION_PATH + "/", json=example_request()).status_code == 401
            assert not calls
            redirect = client.post(GENERATION_PATH + "/", headers=HEADERS, json=example_request())
            assert redirect.status_code == 307
            assert httpx.URL(redirect.headers["location"]).path == root_path + GENERATION_PATH
            assert not calls
            bad = example_request()
            bad["budget"] = "private-invalid-budget"
            invalid = client.post(GENERATION_PATH, headers=HEADERS, json=bad)
            assert invalid.status_code == 422
            assert invalid.json()["detail"]["code"] == "invalid_request"
            assert "private-invalid-budget" not in invalid.text
            assert "input" not in invalid.text
            assert not calls
            provider_client = app.state.generation_service.provider._client
            result = client.post(GENERATION_PATH, headers=HEADERS, json=example_request())
            assert result.status_code == 200
            assert result.json()["calculation"]["total_cost"] == 89.9

            def chunks():
                for _ in range(4):
                    yield b"{" * 32768

            oversized = client.post(GENERATION_PATH, headers=HEADERS, content=chunks())
            assert oversized.status_code == 413
            assert len(calls) == 1
            assert (
                client.post(
                    GENERATION_PATH + "/", headers=HEADERS, content=b"{" * 65537
                ).status_code
                == 413
            )
            assert len(calls) == 1

            # Документированный curl без retries; токен в stdin, а не argv.
            output = tmp_path / "generated-menu.json"
            subprocess.run(
                [
                    "curl",
                    "--disable",
                    "--noproxy",
                    "*",
                    "--silent",
                    "--show-error",
                    "--fail",
                    "--header",
                    "@-",
                    "--header",
                    "Content-Type: application/json",
                    "--data-binary",
                    f"@{PROJECT_ROOT / 'examples/generation-request.json'}",
                    "--output",
                    str(output),
                    api_url + GENERATION_PATH,
                ],
                input=f"Authorization: Bearer {ACCESS_TOKEN}\n",
                text=True,
                check=True,
                capture_output=True,
                timeout=10,
            )
            assert '"demo_unverified"' in output.read_text()
            assert len(calls) == 2
            assert not provider_client.is_closed
        assert provider_client.is_closed


def create_cli_app():
    """Фабрика только для subprocess-теста настоящего Uvicorn CLI."""
    app = create_app(live_settings(os.environ["MEAL_TEST_PROVIDER_URL"]))
    lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def ready_lifespan(application):
        async with lifespan(application):
            fd = int(os.environ["MEAL_TEST_READY_FD"])
            os.write(fd, b"R")
            os.close(fd)
            yield

    app.router.lifespan_context = ready_lifespan
    return app


@contextmanager
def cli_server(provider_url):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    read_fd, write_fd = os.pipe()
    env = {
        **os.environ,
        "PYTHONPATH": str(PROJECT_ROOT),
        "GENERATION_HTTP_ENABLED": "false",
        "DEEPSEEK_API_KEY": PROVIDER_KEY,
        "MEAL_TEST_PROVIDER_URL": provider_url,
        "MEAL_TEST_READY_FD": str(write_fd),
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "test_generation_http_live:create_cli_app",
            "--factory",
            "--app-dir",
            str(PROJECT_ROOT / "tests"),
            "--root-path",
            "/api",
            "--workers",
            "1",
            "--fd",
            str(sock.fileno()),
            "--log-level",
            "error",
            "--no-access-log",
        ],
        env=env,
        pass_fds=(sock.fileno(), write_fd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    os.close(write_fd)
    try:
        assert select.select([read_fd], [], [], 10)[0], "Uvicorn CLI startup timed out"
        assert os.read(read_fd, 1) == b"R", "Uvicorn CLI startup failed"
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        process.terminate()
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)
            raise
        finally:
            os.close(read_fd)
            sock.close()


def test_uvicorn_cli_root_path_with_local_provider():
    provider_app = FastAPI()
    calls = []
    entered = threading.Event()
    gate = {}

    @provider_app.post("/chat/completions")
    async def completion(request: Request):
        calls.append(await request.json())
        if len(calls) == 1:
            gate["loop"] = asyncio.get_running_loop()
            gate["release"] = asyncio.Event()
            entered.set()
            await gate["release"].wait()
        return envelope()

    with (
        local_server(provider_app) as provider_url,
        cli_server(provider_url) as api_url,
        httpx.Client(base_url=api_url, trust_env=False, timeout=10) as client,
    ):
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": b"Bearer \xff"}):
            assert (
                client.post(GENERATION_PATH, headers=headers, json=example_request()).status_code
                == 401
            )
        assert (
            client.post(GENERATION_PATH, headers=HEADERS, content=b"{" * 65537).status_code == 413
        )
        assert not calls
        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(
                client.post, GENERATION_PATH, headers=HEADERS, json=example_request()
            )
            try:
                assert entered.wait(5), "Local provider did not receive first request"
                for suffix in ("", "/"):
                    assert (
                        client.post(
                            GENERATION_PATH + suffix, headers=HEADERS, json=example_request()
                        ).status_code
                        == 429
                    )
                assert len(calls) == 1
            finally:
                if entered.is_set():
                    gate["loop"].call_soon_threadsafe(gate["release"].set)
            assert first.result(timeout=5).status_code == 200
        assert (
            client.post(GENERATION_PATH, headers=HEADERS, json=example_request()).status_code == 200
        )
        assert len(calls) == 2


@pytest.mark.parametrize("root_path", ["", "/api", "/gateway/api"])
def test_live_disconnect_during_body_releases_slot(root_path):
    provider_app = FastAPI()
    calls = []
    partial, disconnected, finished = threading.Event(), threading.Event(), threading.Event()

    @provider_app.post("/chat/completions")
    async def completion(request: Request):
        calls.append(await request.json())
        return envelope()

    class ObservedApp:
        def __init__(self, app):
            self.app = app
            self.first = True

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http" or not self.first:
                return await self.app(scope, receive, send)
            self.first = False

            async def observed_receive():
                message = await receive()
                if message["type"] == "http.request" and message.get("more_body"):
                    partial.set()
                elif message["type"] == "http.disconnect":
                    disconnected.set()
                return message

            try:
                await self.app(scope, observed_receive, send)
            finally:
                finished.set()

    with local_server(provider_app) as provider_url:
        app = create_app(live_settings(provider_url))
        with local_server(ObservedApp(app), root_path=root_path) as api_url:
            url = httpx.URL(api_url)
            with socket.create_connection((url.host, url.port), timeout=5) as connection:
                connection.sendall(
                    (
                        f"POST {GENERATION_PATH} HTTP/1.1\r\nHost: localhost\r\n"
                        f"Authorization: Bearer {ACCESS_TOKEN}\r\n"
                        'Content-Length: 1000\r\nContent-Type: application/json\r\n\r\n{"partial":'
                    ).encode("ascii")
                )
                assert partial.wait(5), "Guard did not receive partial body"
                assert not calls
                connection.shutdown(socket.SHUT_RDWR)
            assert finished.wait(5), "Disconnected request did not finish"
            assert disconnected.is_set()
            assert not calls
            with httpx.Client(base_url=api_url, trust_env=False) as client:
                result = client.post(GENERATION_PATH, headers=HEADERS, json=example_request())
                assert result.status_code == 200
                assert len(calls) == 1


@pytest.mark.parametrize("root_path", ["", "/api", "/gateway/api"])
def test_cancel_with_real_outgoing_transport_keeps_shared_client(root_path):
    provider_app = FastAPI()
    calls = []
    upstream_closed = threading.Event()
    gate = {}

    @provider_app.post("/chat/completions")
    async def completion(request: Request):
        calls.append(await request.json())
        if len(calls) != 1:
            return envelope()
        gate["loop"] = asyncio.get_running_loop()
        gate["release"] = asyncio.Event()

        async def blocked_body():
            try:
                yield b'{"unfinished":'
                await gate["release"].wait()
            finally:
                upstream_closed.set()

        return StreamingResponse(blocked_body(), media_type="application/json")

    with local_server(provider_app) as provider_url:
        app = create_app(live_settings(provider_url))

        async def scenario():
            received_headers = asyncio.Event()
            responses = []
            path = root_path + GENERATION_PATH
            async with async_client(app, root_path=root_path) as client:
                provider_client = app.state.generation_service.provider._client

                async def observe(response):
                    responses.append(response)
                    received_headers.set()

                provider_client.event_hooks["response"] = [observe]
                first = asyncio.create_task(
                    client.post(path, headers=HEADERS, json=example_request())
                )
                try:
                    await asyncio.wait_for(received_headers.wait(), 5)
                    assert (
                        await client.post(path, headers=HEADERS, json=example_request())
                    ).status_code == 429
                    assert len(calls) == 1
                    first.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await first
                    assert responses[0].is_closed
                    assert not provider_client.is_closed
                    assert await asyncio.to_thread(upstream_closed.wait, 5)
                    assert (
                        await client.post(path, headers=HEADERS, json=example_request())
                    ).status_code == 200
                    assert len(calls) == 2
                    assert not provider_client.is_closed
                finally:
                    first.cancel()
                    await asyncio.gather(first, return_exceptions=True)
                    if gate:
                        gate["loop"].call_soon_threadsafe(gate["release"].set)
            assert provider_client.is_closed

        asyncio.run(scenario())
