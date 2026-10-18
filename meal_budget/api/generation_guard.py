import secrets

from starlette._utils import get_route_path
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from meal_budget.api.generations import (
    GENERATION_PATH,
    GUARDED_REQUEST_KEY,
    bearer,
    error_response,
    is_generation_path,
)
from meal_budget.services.app_admission import (
    INSTALLATION_SCOPE_KEY,
    INSTALLATION_TOKEN,
    INSTALLATIONS_PATH,
    is_app_path,
    token_hash,
)
from meal_budget.services.generation_slot import GenerationSlot

MAX_REQUEST_BYTES = 65536


class GenerationGuard:
    """Доступ/тело до JSON; синхронный маршрут разделяет слот с worker."""

    def __init__(
        self, app: ASGIApp, *, token: str, slot: GenerationSlot, app_enabled: bool = False
    ):
        self.app = app
        self.token = token.encode("ascii")
        self.slot = slot
        self.app_enabled = app_enabled

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] not in ("POST", "GET")
            or not (
                is_generation_path(get_route_path(scope))
                or (self.app_enabled and is_app_path(get_route_path(scope)))
            )
        ):
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        app_path = is_app_path(get_route_path(scope))
        registration = app_path and get_route_path(scope).rstrip("/") == INSTALLATIONS_PATH
        if app_path:
            original_send = send

            async def uncached_send(message):
                if message["type"] == "http.response.start":
                    message["headers"] = [*message["headers"], (b"cache-control", b"no-store")]
                await original_send(message)

            send = uncached_send
        credentials = None if registration else await bearer(request)
        valid = registration
        if credentials is not None:
            value = credentials.credentials
            if app_path:
                if INSTALLATION_TOKEN.fullmatch(value) and not secrets.compare_digest(
                    value.encode("ascii"), self.token
                ):
                    try:
                        store = scope["app"].state.generation_jobs.store
                        owner = await store.authenticate_installation(token_hash(value))
                    except Exception:
                        await error_response(
                            503, "job_storage_unavailable", "Хранилище заданий недоступно."
                        )(scope, receive, send)
                        return
                    if owner is not None:
                        scope[INSTALLATION_SCOPE_KEY] = owner
                        valid = True
            else:
                valid = secrets.compare_digest(value.encode("utf-8"), self.token)
        if not valid:
            await error_response(401, "unauthorized", "Требуется токен доступа к генерации.")(
                scope, receive, send
            )
            return
        synchronous = (
            scope["method"] == "POST" and get_route_path(scope).rstrip("/") == GENERATION_PATH
        )
        # Только прежний синхронный POST захватывает слот до чтения тела.
        if synchronous and not self.slot.try_acquire():
            await error_response(429, "generation_busy", "Генерация уже выполняется.")(
                scope, receive, send
            )
            return
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
            scope.pop(INSTALLATION_SCOPE_KEY, None)
            if synchronous:
                self.slot.release()

    @staticmethod
    async def too_large(scope: Scope, receive: Receive, send: Send) -> None:
        await error_response(413, "request_too_large", "Тело запроса превышает 65536 байт.")(
            scope, receive, send
        )
