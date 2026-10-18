"""Безопасные ошибки admission и стабильный хеш валидированной анкеты."""

import hashlib
import json
import re
from decimal import Decimal

from meal_budget.schemas.generation import GenerationRequest

INSTALLATIONS_PATH = "/v1/installations"
APP_JOBS_PATH = "/v1/app/generation-jobs"
INSTALLATION_SCOPE_KEY = "meal_budget.installation_id"
IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9_-]{1,64}", re.ASCII)
INSTALLATION_TOKEN = re.compile(r"mbi_[A-Za-z0-9_-]{43}", re.ASCII)


def is_app_path(path: str) -> bool:
    path = path.rstrip("/")
    return path in (INSTALLATIONS_PATH, APP_JOBS_PATH) or path.startswith(APP_JOBS_PATH + "/")


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode("ascii")).digest()


def request_hash(request: GenerationRequest) -> bytes:
    # Не используем JSON-сериализаторы Money/JSONNumber: они предназначены для вывода.
    # Кортеж Decimal не зависит от контекста и не разворачивает огромный exponent.
    def canonical(value):
        if isinstance(value, Decimal):
            sign, digits, exponent = value.as_tuple()
            coefficient = "".join(map(str, digits)).rstrip("0") or "0"
            exponent += len(digits) - len(coefficient)
            return f"{'-' if sign else ''}{coefficient}e{exponent}"
        if isinstance(value, dict):
            return {key: canonical(item) for key, item in value.items()}
        if isinstance(value, list):
            return [canonical(item) for item in value]
        return value

    normalized = request.model_dump(mode="python")
    # Эти списки имеют семантику множеств в генерационном сервисе.
    for field in ("excluded_product_ids", "disliked_product_ids"):
        normalized[field] = sorted(set(normalized[field]))
    payload = json.dumps(
        canonical(normalized), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).digest()


class AdmissionError(RuntimeError):
    def __init__(self, code: str, retry_after: int | None = None):
        self.code, self.retry_after = code, retry_after
        super().__init__(code)
