"""Импорт предоставленного снимка без сети и повышения достоверности."""

import argparse
from pathlib import Path

from meal_budget.core.config import PROJECT_ROOT
from meal_budget.schemas.products import ProductCatalog
from meal_budget.services.deepseek import parse_json

VERSION = "5ka-2026-10-14-observed-v3"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--products", required=True, type=Path)
    parser.add_argument("--candidates", required=True, type=Path)
    args = parser.parse_args(argv)
    raw = parse_json(args.products.read_bytes())
    inventory_bytes = args.candidates.read_bytes()
    inventory = parse_json(inventory_bytes)
    original = ProductCatalog.model_validate(raw)
    if original.catalog_version != "5ka-2026-10-14-web-v2":
        raise ValueError("unexpected source version")
    candidates = {item["id"] for item in inventory["candidates"]}
    active = {item.id for item in original.products}
    if active & candidates or active & {"oats", "buckwheat", "chicken"}:
        raise ValueError("candidate/demo IDs must not enter the imported catalog")
    observations = {item["id"]: item for item in inventory["source_observations"]}
    for product in raw["products"]:
        provenance = product["provenance"]
        price = provenance["price"]
        if provenance["kind"] != "web" or price["region_confirmed"] or price["store"]:
            raise ValueError("unexpected source provenance")
        observation = observations.get(product["id"])
        if observation and observation["url"] != provenance["product_url"]:
            raise ValueError("observation must reference the same product")
        for source in (provenance["nutrition"], price):
            source["observation_method"] = "web_tool"
            source["web_cache_age"] = observation["web_cache_age"] if observation else None
        price["price_type"] = "displayed"
        price["single_package_without_card"] = None
        product["state_basis"] = "as_sold"
    raw["catalog_version"] = VERSION
    imported = ProductCatalog.model_validate(raw)
    # Только provenance/семантика изменены; численные факты не исправляем догадками.
    for before, after in zip(original.products, imported.products, strict=True):
        for field in ("id", "name", "state", "nutrition_per_100g", "package_size_g", "price"):
            if getattr(before, field) != getattr(after, field):
                raise ValueError("numerical/product facts must remain unchanged")
    (PROJECT_ROOT / "data/5ka-products.json").write_text(
        imported.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    # Отдельный справочник сохраняется буквально; load_catalog его не принимает.
    (PROJECT_ROOT / "data/5ka-candidates.json").write_bytes(inventory_bytes)
    print(f"Imported {len(imported.products)} products; candidates excluded: {len(candidates)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
