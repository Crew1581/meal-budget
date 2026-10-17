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
from meal_budget.evaluation import (
    ScenarioPreparationError,
    require_ready_scenario,
    verify_preparation,
)
from meal_budget.schemas.diagnostics import AttemptDiagnostic, ResponseMetadata
from meal_budget.schemas.evaluation import RunSummary
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
    request: GenerationRequest,
    catalog: ProductCatalog,
    settings: Settings,
    *,
    diagnostics: list[AttemptDiagnostic] | None = None,
    response_metadata: list[ResponseMetadata] | None = None,
) -> GenerationResult:
    async with DeepSeekProvider(settings) as provider:
        return await MenuGenerationService(provider, settings).generate(
            request, catalog, diagnostics=diagnostics, response_metadata=response_metadata
        )


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


def normalized_output_path(path: Path) -> Path:
    # Лексическая нормализация Path удаляет ./ и повторные /, но не следует symlink.
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError("invalid output path")
    return path


def check_existing_output(path: Path, *, overwrite: bool) -> None:
    """Проверяет существующую часть пути, ничего не создавая."""
    flags = directory_flags()
    directory = os.open(PROJECT_ROOT, flags)
    try:
        for part in ("generated", *path.parts[:-1]):
            try:
                child = os.open(part, flags, dir_fd=directory)
            except FileNotFoundError:
                # Ниже отсутствующего родителя нет существующих конфликтующих файлов.
                break
            os.close(directory)
            directory = child
        else:
            check_output(directory, path.name, overwrite=overwrite)
        if not os.access(".", os.W_OK | os.X_OK, dir_fd=directory, effective_ids=True):
            raise PermissionError("output directory is not writable")
    finally:
        os.close(directory)


def check_output_paths(
    output: Path, report: Path | None, *, overwrite: bool
) -> tuple[Path, Path | None]:
    output = normalized_output_path(output)
    if report is not None:
        report = normalized_output_path(report)
        if output == report or output in report.parents or report in output.parents:
            raise ValueError("output and report paths conflict")
    # Общая проверка обеих целей ДО mkdir: никакой частичной подготовки при конфликте.
    for path in (output, report):
        if path is not None:
            check_existing_output(path, overwrite=overwrite)
    return output, report


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
    parser.add_argument("--scenario", help="ID подготовленного ready-сценария из manifest")
    parser.add_argument("--report", type=Path, help="Сводка успеха/ошибки внутри generated/")
    args = parser.parse_args(argv)
    try:
        directory_flags()
    except UnsupportedPlatformError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        scenario = require_ready_scenario(args.scenario) if args.scenario is not None else None
    except ScenarioPreparationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        args.output, args.report = check_output_paths(
            args.output, args.report, overwrite=args.overwrite
        )
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
        if scenario is not None:
            verify_preparation(scenario, request, catalog, settings.generation_nutrition_tolerance)
        summary = RunSummary(
            scenario=args.scenario if args.scenario is not None else "unspecified",
            catalog_version=catalog.catalog_version,
            requested_model=settings.deepseek_model or None,
            thinking=settings.deepseek_thinking,
            outcome="not_run",
            attempts=[],
            response_metadata=[],
        )
    except ScenarioPreparationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError, RuntimeError, RecursionError):
        print("Некорректная конфигурация, анкета или каталог.", file=sys.stderr)
        return 2
    try:
        # Все назначения уже проверены. Ошибка создания родителей также до провайдера.
        for path in (args.output, args.report):
            if path is not None:
                with output_directory(path) as (directory, name):
                    check_output(directory, name, overwrite=args.overwrite)
    except (OSError, ValueError):
        print("Не удалось подготовить назначения внутри generated/.", file=sys.stderr)
        return 2
    try:
        result = asyncio.run(
            run_generation(
                request,
                catalog,
                settings,
                diagnostics=summary.attempts if args.report else None,
                response_metadata=summary.response_metadata if args.report else None,
            )
        )
        save_result(args.output, result.model_dump_json(indent=2), overwrite=args.overwrite)
        if args.report:
            summary.outcome = "success"
            summary.metadata = result.metadata
            summary.budget_pass = result.calculation.within_budget
            summary.nutrition_pass = True
            save_result(args.report, summary.model_dump_json(indent=2), overwrite=args.overwrite)
    except GenerationError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        if args.report:
            summary.outcome = "error"
            summary.error_code = exc.code
            summary.metadata = exc.metadata
            try:
                save_result(
                    args.report, summary.model_dump_json(indent=2), overwrite=args.overwrite
                )
            except (OSError, ValueError):
                print("Не удалось сохранить сводку ошибки.", file=sys.stderr)
                return 2
        return 1
    except (OSError, ValueError):
        print("Не удалось сохранить проверенный результат.", file=sys.stderr)
        return 2
    print("Проверенный результат сохранён. Каталог demo_unverified; см. предупреждения в файле.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
