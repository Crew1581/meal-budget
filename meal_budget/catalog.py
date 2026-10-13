import json
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError

from meal_budget.schemas.products import ProductCatalog


def load_catalog(path: Path) -> ProductCatalog:
    """Читает каталог без промежуточного binary float и проверяет контракт."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    except (OSError, UnicodeError, ValueError):
        raise RuntimeError(
            f"Не удалось прочитать каталог {path}: проверьте доступность и формат JSON"
        ) from None
    try:
        return ProductCatalog.model_validate(raw)
    except ValidationError as exc:
        # Не включаем содержимое файла в ошибку запуска.
        fields = ", ".join(".".join(map(str, error["loc"])) for error in exc.errors())
        raise RuntimeError(f"Некорректный каталог {path}: проверьте поля {fields}") from None
