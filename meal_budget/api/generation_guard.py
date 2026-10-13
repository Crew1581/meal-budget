import secrets

from starlette._utils import get_route_path
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from meal_budget.api.generations import (
    GENERATION_PATH,
    GUARDED_REQUEST_KEY,
    bearer,
    error_response,
)

MAX_REQUEST_BYTES = 65536


class GenerationGuard:
    """Один слот на процесс, без очереди; проверка доступа до чтения тела."""

    def __init__(self, app: ASGIApp, *, token: str):
        self.app = app
        self.token = token.encode("ascii")
        self.busy = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or get_route_path(scope).rstrip("/") != GENERATION_PATH
        ):
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        credentials = await bearer(request)
        if credentials is None or not secrets.compare_digest(
            credentials.credentials.encode("utf-8"), self.token
        ):
            await error_response(401, "unauthorized", "Требуется токен доступа к генерации.")(
                scope, receive, send
            )
            return
        # Проверка и захват без await: атомарно внутри одного event loop Uvicorn.
        if self.busy:
            await error_response(429, "generation_busy", "Генерация уже выполняется.")(
                scope, receive, send
            )
            return
        self.busy = True
        try:
            length = request.headers.get("content-length")
            normalized_length = (length.lstrip("0") or "0") if length is not None else None
            if length is not None and (
                not length.isascii()
                or not length.isdecimal()
                or len(normalized_length) > 5
                or int(normalized_length) > MAX_REQUEST_BYTES
            ):
                await self.too_large(scope, receive, send)
                return
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                if len(body) + len(chunk) > MAX_REQUEST_BYTES:
                    await self.too_large(scope, receive, send)
                    return
                body.extend(chunk)
                if not message.get("more_body", False):
                    break

            buffered = bytes(body)
            del body
            delivered = False

            async def bounded_receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": buffered, "more_body": False}
                return await receive()

            # Только сервер создает подтверждение для ЭТОГО scope после всех проверок.
            scope[GUARDED_REQUEST_KEY] = scope
            await self.app(scope, bounded_receive, send)
        finally:
            scope.pop(GUARDED_REQUEST_KEY, None)
            self.busy = False

    @staticmethod
    async def too_large(scope: Scope, receive: Receive, send: Send) -> None:
        await error_response(413, "request_too_large", "Тело запроса превышает 65536 байт.")(
            scope, receive, send
        )
