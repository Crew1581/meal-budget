"""Ручной запуск из корня: uv run --frozen python -m meal_budget.generate_menu."""

import argparse
import asyncio
import os
import secrets
import stat
import sys
from contextlib import contextmanager, suppress
from pathlib import Path

from meal_budget.catalog import load_catalog
from meal_budget.core.config import PROJECT_ROOT, Settings
from meal_budget.schemas.generation import GenerationRequest, GenerationResult
from meal_budget.schemas.products import ProductCatalog
from meal_budget.services.deepseek import DeepSeekProvider, GenerationError, parse_json
from meal_budget.services.menu_generation import MenuGenerationService

MAX_REQUEST_BYTES = 65536


class UnsupportedPlatformError(RuntimeError):
    def __init__(self):
        super().__init__(
            "Файловый сценарий CLI поддерживается и проверен только на Linux "
            "с O_DIRECTORY и O_NOFOLLOW."
        )


def directory_flags() -> int:
    if sys.platform != "linux" or not all(
        hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW")
    ):
        raise UnsupportedPlatformError()
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


async def run_generation(
    request: GenerationRequest, catalog: ProductCatalog, settings: Settings
) -> GenerationResult:
    async with DeepSeekProvider(settings) as provider:
        return await MenuGenerationService(provider, settings).generate(request, catalog)


@contextmanager
def output_directory(path: Path):
    """Открывает родителя внутри generated/, не следуя символическим ссылкам."""
    flags = directory_flags()
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError("invalid output path")
    directory = os.open(PROJECT_ROOT, flags)
    try:
        for part in ("generated", *path.parts[:-1]):
            with suppress(FileExistsError):
                os.mkdir(part, mode=0o700, dir_fd=directory)
            child = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        yield directory, path.name
    finally:
        os.close(directory)


def check_output(directory: int, name: str, *, overwrite: bool) -> None:
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("output must not be a symlink or special file")
    if not overwrite:
        raise FileExistsError("output exists")


def save_result(path: Path, content: str, *, overwrite: bool) -> None:
    # dir_fd фиксирует родителя: подмена пути symlink не перенаправит запись.
    with output_directory(path) as (directory, name):
        check_output(directory, name, overwrite=overwrite)
        temporary = ".menu-" + secrets.token_hex(16)
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode=0o600,
            dir_fd=directory,
        )
        try:
            with os.fdopen(descriptor, mode="w", encoding="utf-8") as file:
                file.write(content)
            check_output(directory, name, overwrite=overwrite)
            if overwrite:
                os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            else:
                # В отличие от exists()+rename, link атомарно отказывает при гонке.
                os.link(
                    temporary,
                    name,
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                    follow_symlinks=False,
                )
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=directory)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Ручная генерация проверенного меню (платный API). "
        "Файловый сценарий поддерживается и проверен только на Linux."
    )
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Относительный путь внутри generated/, например personal-menu.json "
        "или tests/run-1.json; без .. и symlink",
    )
    parser.add_argument("--overwrite", action="store_true", help="Явно разрешить замену результата")
    args = parser.parse_args(argv)
    try:
        directory_flags()
    except UnsupportedPlatformError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        with output_directory(args.output) as (directory, name):
            check_output(directory, name, overwrite=args.overwrite)
    except FileExistsError:
        print("Результат уже существует; для замены нужен --overwrite.", file=sys.stderr)
        return 2
    except (OSError, ValueError):
        print("Недопустимый путь результата внутри generated/.", file=sys.stderr)
        return 2
    try:
        # Не загружаем .env: ключ для ручного запуска принимается только из окружения.
        settings = Settings(_env_file=None)
        with args.request.open("rb") as file:
            raw = file.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError("request size limit")
        request = GenerationRequest.model_validate(parse_json(raw))
        catalog = load_catalog(settings.catalog_path)
    except (OSError, ValueError, RuntimeError, RecursionError):
        print("Некорректная конфигурация, анкета или каталог.", file=sys.stderr)
        return 2
    try:
        result = asyncio.run(run_generation(request, catalog, settings))
        save_result(args.output, result.model_dump_json(indent=2), overwrite=args.overwrite)
    except GenerationError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError):
        print("Не удалось сохранить проверенный результат.", file=sys.stderr)
        return 2
    print("Проверенный результат сохранён. Каталог demo_unverified; см. предупреждения в файле.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
