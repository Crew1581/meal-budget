"""Автономная сводка одного сценария; сетевых вызовов нет."""

import argparse
import os
import stat
import sys
from pathlib import Path

from meal_budget.catalog import load_catalog
from meal_budget.core.config import PROJECT_ROOT
from meal_budget.evaluation import load_scenarios
from meal_budget.generate_menu import output_directory, save_result
from meal_budget.schemas.evaluation import RunSummary
from meal_budget.services.deepseek import parse_json

MAX_REPORT_BYTES = 65536


def read_report(path: Path) -> RunSummary:
    # Та же защита generated/: relative path, dir_fd, O_NOFOLLOW, обычный файл.
    with output_directory(path) as (directory, name):
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as file:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                raise ValueError("report must be a regular file")
            raw = file.read(MAX_REPORT_BYTES + 1)
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError("report size limit")
    return RunSummary.model_validate(parse_json(raw))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Сводка одного сценария без сети и DeepSeek.")
    parser.add_argument("--scenario", default="ordinary")
    parser.add_argument("--result", type=Path, help="Сохраненный --report внутри generated/")
    parser.add_argument("--output", required=True, type=Path, help="Путь внутри generated/")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    try:
        scenario = next(item for item in load_scenarios() if item.id == args.scenario)
        if args.result:
            summary = read_report(args.result)
            if summary.scenario != scenario.id:
                raise ValueError("scenario mismatch")
            if summary.outcome == "success" and scenario.status != "ready":
                raise ValueError("blocked scenario cannot have a success summary")
        else:
            catalog = load_catalog(PROJECT_ROOT / "data/5ka-products.json")
            summary = RunSummary(
                scenario=scenario.id,
                catalog_version=catalog.catalog_version,
                outcome="blocked" if scenario.status == "blocked" else "not_run",
            )
        save_result(args.output, summary.model_dump_json(indent=2), overwrite=args.overwrite)
    except (OSError, ValueError, RuntimeError, StopIteration, RecursionError):
        print("Некорректный сценарий, отчет или путь внутри generated/.", file=sys.stderr)
        return 2
    print("Автономная сводка сохранена; ручная оценка обязательна.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
