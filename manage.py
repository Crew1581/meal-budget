"""Локальный запуск API из терминала или PyCharm."""

import argparse
from collections.abc import Sequence
from pathlib import Path

import uvicorn
from pydantic import ValidationError

from meal_budget.core.config import Settings

PROJECT_DIR = Path(__file__).resolve().parent


def port_number(value: str) -> int:
    """Порт 0 позволяет операционной системе выбрать свободный порт для теста."""
    port = int(value)
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("Порт должен быть в диапазоне 0–65535.")
    return port


def main(argv: Sequence[str] | None = None) -> None:
    """Запускает один worker Uvicorn без генераций при старте."""
    parser = argparse.ArgumentParser(description="Запуск Meal Budget API")
    parser.add_argument(
        "--host", default="127.0.0.1", help="Адрес сервера (по умолчанию 127.0.0.1)"
    )
    parser.add_argument(
        "--port", type=port_number, default=8000, help="Порт сервера (по умолчанию 8000)"
    )
    parser.add_argument(
        "--reload", action="store_true", help="Перезапуск при изменении кода разработки"
    )
    args = parser.parse_args(argv)

    try:
        settings = Settings()
    except ValidationError:
        # Полный ValidationError может содержать исходные значения настроек.
        parser.error("Некорректные настройки приложения; проверьте окружение.")
    if args.reload and settings.generation_http_enabled:
        parser.error("--reload недоступен при GENERATION_HTTP_ENABLED=true.")

    uvicorn.run(
        "main:create_app",
        factory=True,
        app_dir=str(PROJECT_DIR),
        host=args.host,
        port=args.port,
        workers=1,
        reload=args.reload,
        reload_dirs=[str(PROJECT_DIR)] if args.reload else None,
        proxy_headers=False,
        access_log=False,
    )


if __name__ == "__main__":
    main()
