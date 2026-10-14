"""Ограниченный Chat Completions transport; без SDK и сетевых повторов."""

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated

import httpx
from pydantic import BaseModel, Field

from meal_budget.core.config import Settings
from meal_budget.schemas.generation import CallMetadata, GenerationMetadata

ERROR_MESSAGES = {
    "catalog_unavailable": "Активный каталог пуст; генерация недоступна без проверенных товаров.",
    "configuration_missing": "Не настроены ключ или модель DeepSeek.",
    "provider_unavailable": "Провайдер генерации недоступен.",
    "provider_authorization": "Ошибка авторизации у провайдера генерации.",
    "provider_rate_limit": "Достигнут лимит запросов провайдера генерации.",
    "provider_timeout": "Таймаут провайдера; результат запроса и расход токенов неизвестны.",
    "generation_timeout": "Истекло общее время генерации; расход токенов может быть неизвестен.",
    "invalid_response": "Не удалось подобрать меню с указанными условиями",
    "attempts_exhausted": "Не удалось подобрать меню с указанными условиями",
    "invalid_request": "Некорректные ссылки предпочтений на серверный каталог.",
}


class GenerationError(Exception):
    def __init__(self, code: str):
        super().__init__(ERROR_MESSAGES[code])
        self.code = code
        self.metadata: GenerationMetadata | None = None


class InvalidResponse(GenerationError):
    def __init__(self, violation: str, metadata: CallMetadata | None = None):
        super().__init__("invalid_response")
        self.violation = violation
        self.response_metadata = metadata or CallMetadata()


class ProviderUsage(BaseModel):
    prompt_tokens: int = Field(strict=True, ge=0)
    completion_tokens: int = Field(strict=True, ge=0)
    total_tokens: int = Field(strict=True, ge=0)
    prompt_cache_hit_tokens: int | None = Field(default=None, strict=True, ge=0)
    prompt_cache_miss_tokens: int | None = Field(default=None, strict=True, ge=0)


class ProviderMessage(BaseModel):
    content: str | None


class ProviderChoice(BaseModel):
    finish_reason: str
    message: ProviderMessage


class ProviderResponse(BaseModel):
    choices: list[ProviderChoice] = Field(min_length=1, max_length=1)
    model: Annotated[str, Field(max_length=120, pattern=r"^[a-zA-Z0-9._/-]+$")] | None = None
    usage: ProviderUsage | None = None


@dataclass(frozen=True)
class Completion:
    content: str
    metadata: CallMetadata


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("non-finite JSON number")


def parse_json(raw: str | bytes):
    # Не извлекаем JSON из текста; повторные ключи и NaN также не принимаются.
    return json.loads(raw, parse_float=Decimal, object_pairs_hook=_object, parse_constant=_constant)


class DeepSeekProvider:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self._client = httpx.AsyncClient(
            transport=transport or httpx.AsyncHTTPTransport(retries=0),
            timeout=httpx.Timeout(
                connect=settings.deepseek_connect_timeout,
                read=settings.deepseek_read_timeout,
                write=settings.deepseek_write_timeout,
                pool=settings.deepseek_pool_timeout,
            ),
            follow_redirects=False,
            trust_env=False,
        )

    async def __aenter__(self) -> "DeepSeekProvider":
        return self

    async def __aexit__(self, *args) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def ensure_configured(self) -> None:
        self._normalized_key()
        url = self.settings.deepseek_base_url
        if (
            not self.settings.deepseek_model.strip()
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise GenerationError("configuration_missing")

    def _normalized_key(self) -> str:
        value = self.settings.deepseek_api_key.get_secret_value()
        # Проверяем ДО HTTPX/h11: их DEBUG-ошибка может включать значение заголовка.
        # Только печатный ASCII; управляющие символы не удаляем даже на краях.
        if any(not 32 <= ord(character) <= 126 for character in value):
            raise GenerationError("configuration_missing")
        key = value.strip(" ")
        if not key:
            raise GenerationError("configuration_missing")
        return key

    async def complete(self, messages: list[dict[str, str]]) -> Completion:
        self.ensure_configured()
        key = self._normalized_key()
        settings = self.settings
        body = {
            "model": settings.deepseek_model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "stream": False,
            "max_tokens": settings.deepseek_max_output_tokens,
        }
        if settings.deepseek_thinking != "omit":
            body["thinking"] = {"type": settings.deepseek_thinking}
        if settings.deepseek_thinking == "enabled":
            body["reasoning_effort"] = settings.deepseek_reasoning_effort
        url = str(settings.deepseek_base_url).rstrip("/") + "/chat/completions"
        try:
            async with self._client.stream(
                "POST",
                url,
                json=body,
                headers={"Authorization": f"Bearer {key}", "Accept-Encoding": "identity"},
            ) as response:
                if response.status_code in (401, 403):
                    raise GenerationError("provider_authorization")
                if response.status_code == 429:
                    raise GenerationError("provider_rate_limit")
                if response.status_code != 200:
                    # Не читаем и не выдаем тело ошибки провайдера.
                    raise GenerationError("provider_unavailable")
                if (
                    response.headers.get("content-encoding", "identity").strip().lower()
                    != "identity"
                ):
                    raise InvalidResponse("unsupported_content_encoding")
                raw = bytearray()
                async for chunk in response.aiter_raw():
                    if len(raw) + len(chunk) > settings.deepseek_max_response_bytes:
                        raise InvalidResponse("response_too_large")
                    raw.extend(chunk)
        except httpx.TimeoutException:
            raise GenerationError("provider_timeout") from None
        except httpx.RequestError:
            raise GenerationError("provider_unavailable") from None
        try:
            envelope = ProviderResponse.model_validate(parse_json(bytes(raw)))
        except (ValueError, UnicodeError, RecursionError):
            raise InvalidResponse("invalid_provider_json") from None
        metadata = CallMetadata(
            model=envelope.model,
            usage=envelope.usage.model_dump(exclude_none=True) if envelope.usage else None,
        )
        choice = envelope.choices[0]
        if choice.finish_reason != "stop":
            raise InvalidResponse("incomplete_response", metadata)
        if not choice.message.content:
            raise InvalidResponse("empty_response", metadata)
        return Completion(choice.message.content, metadata)
