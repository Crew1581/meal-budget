"""Регрессия утечки через реальный HTTPX → httpcore → h11, без сетевых сокетов."""

import asyncio
import logging

import httpcore
import httpx
import pytest

from meal_budget.core.config import Settings
from meal_budget.services.deepseek import DeepSeekProvider, GenerationError

FAKE_KEY = "fake-header-secret"


class RecordingNetworkStream(httpcore.AsyncMockStream):
    def __init__(self):
        body = (
            b'{"model":"test-model","choices":'
            b'[{"finish_reason":"stop","message":{"content":"{}"}}]}'
        )
        super().__init__(
            [
                b"HTTP/1.1 200 OK\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body,
            ]
        )
        self.writes = []

    async def write(self, buffer, timeout=None):
        self.writes.append(buffer)


class RecordingNetworkBackend(httpcore.AsyncMockBackend):
    def __init__(self):
        super().__init__([])
        self.connections = []
        self.transport_calls = 0

    async def connect_tcp(self, **kwargs):
        stream = RecordingNetworkStream()
        self.connections.append(stream)
        return stream

    async def connect_unix_socket(self, **kwargs):
        raise AssertionError("unexpected unix socket")


def complete(key, backend):
    settings = Settings(
        _env_file=None,
        deepseek_api_key=key,
        deepseek_model="test-model",
        deepseek_base_url="https://provider.test",
    )
    # HTTPX 0.28.1 не предоставляет network_backend; заменяем только backend его pool.
    transport = httpx.AsyncHTTPTransport(retries=0, trust_env=False)
    transport._pool._network_backend = backend
    original_handle = transport.handle_async_request

    async def handle(request):
        backend.transport_calls += 1
        return await original_handle(request)

    transport.handle_async_request = handle

    async def scenario():
        async with DeepSeekProvider(settings, transport=transport) as provider:
            return await provider.complete([])

    return asyncio.run(scenario())


def test_key_trimmed_through_real_h11_without_debug_leak(caplog):
    caplog.set_level(logging.DEBUG, logger="httpcore")
    backend = RecordingNetworkBackend()
    result = complete("  " + FAKE_KEY + "  ", backend)
    assert result.content == "{}"
    assert len(backend.connections) == 1
    assert backend.transport_calls == 1
    stream = backend.connections[0]
    wire = b"".join(stream.writes)
    assert b"Authorization: Bearer " + FAKE_KEY.encode() + b"\r\n" in wire
    assert b"Accept-Encoding: identity\r\n" in wire
    assert stream._closed
    # Доказываем, что DEBUG действительно включен и HTTP/1.1 сериализация выполнялась.
    assert any(
        record.name == "httpcore.http11" and "send_request_headers.complete" in record.message
        for record in caplog.records
    )
    assert FAKE_KEY not in caplog.text


@pytest.mark.parametrize(
    "key",
    [
        "",
        "   ",
        "\t" + FAKE_KEY,
        FAKE_KEY + "\n",
        FAKE_KEY + "\r\nx: injected",
        FAKE_KEY + "\x00",
        FAKE_KEY + "\x1f",
        FAKE_KEY + "\x7f",
        FAKE_KEY + "é",
        FAKE_KEY + "\u00a0",
    ],
)
def test_invalid_keys_rejected_before_real_transport_without_debug_leak(key, caplog):
    caplog.set_level(logging.DEBUG, logger="httpcore")
    backend = RecordingNetworkBackend()
    with pytest.raises(GenerationError) as exc:
        complete(key, backend)
    assert exc.value.code == "configuration_missing"
    assert backend.connections == []
    assert backend.transport_calls == 0
    assert FAKE_KEY not in str(exc.value) + caplog.text
    assert not any(record.name.startswith("httpcore") for record in caplog.records)
