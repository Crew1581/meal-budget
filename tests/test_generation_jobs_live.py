import asyncio
import json
import socket
import threading
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi import FastAPI, Request
from test_generation_http import ACCESS_TOKEN, HEADERS, envelope, example_request
from test_generation_http_live import live_settings, local_server

from main import create_app
from meal_budget.api.generations import JOBS_PATH


@pytest.mark.parametrize("root_path", ["", "/api", "/gateway/api"])
def test_real_socket_disconnect_after_acceptance(root_path):
    provider_app = FastAPI()
    entered, completed = threading.Event(), threading.Event()
    gate = {}
    calls = []

    @provider_app.post("/chat/completions")
    async def completion(request: Request):
        calls.append(await request.json())
        gate["loop"] = asyncio.get_running_loop()
        gate["release"] = asyncio.Event()
        entered.set()
        await gate["release"].wait()
        return envelope()

    with local_server(provider_app) as provider_url:
        app = create_app(live_settings(provider_url))
        lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def observed_lifespan(application):
            async with lifespan(application):
                store = application.state.generation_jobs.store
                original = store.complete

                async def observed(*args):
                    await original(*args)
                    completed.set()

                store.complete = observed
                yield

        app.router.lifespan_context = observed_lifespan
        with local_server(app, root_path=root_path) as api_url:
            url = urlsplit(api_url)
            body = json.dumps(example_request()).encode()
            with socket.create_connection((url.hostname, url.port), timeout=5) as connection:
                connection.sendall(
                    (
                        f"POST {JOBS_PATH} HTTP/1.1\r\nHost: localhost\r\n"
                        f"Authorization: Bearer {ACCESS_TOKEN}\r\n"
                        f"Content-Length: {len(body)}\r\nContent-Type: application/json\r\n\r\n"
                    ).encode()
                    + body
                )
                received = bytearray()
                while b"\r\n\r\n" not in received:
                    chunk = connection.recv(4096)
                    assert chunk
                    received.extend(chunk)
                headers, payload = received.split(b"\r\n\r\n", 1)
                assert b"202 Accepted" in headers
                length = next(
                    int(line.split(b":", 1)[1])
                    for line in headers.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                )
                while len(payload) < length:
                    chunk = connection.recv(4096)
                    assert chunk
                    payload += chunk
                accepted = json.loads(payload)
                assert accepted["state"] == "queued"
                connection.shutdown(socket.SHUT_RDWR)
            # Соединение закрыто, провайдер явно заблокирован до release.
            assert entered.wait(5)
            try:
                with httpx.Client(base_url=api_url, trust_env=False) as client:
                    path = JOBS_PATH + "/" + accepted["id"]
                    assert client.get(path).status_code == 401
                    assert client.get(path, headers=HEADERS).json()["state"] == "running"
                    assert len(calls) == 1
                    gate["loop"].call_soon_threadsafe(gate["release"].set)
                    assert completed.wait(5)
                    result = client.get(path, headers=HEADERS)
                    assert result.json()["state"] == "completed"
                    assert result.json()["result"]["calculation"]["total_cost"] == 89.9
            finally:
                gate["loop"].call_soon_threadsafe(gate["release"].set)
