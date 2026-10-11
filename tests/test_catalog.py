import json
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from meal_budget.catalog import load_catalog
from meal_budget.core.config import PROJECT_ROOT, Settings
from meal_budget.main import create_app


@pytest.mark.parametrize("contents", ["{", "{}", '{"products": []}', "null"])
def test_invalid_catalog_prevents_startup(tmp_path, contents):
    path = tmp_path / "invalid.json"
    path.write_text(contents, encoding="utf-8")
    with (
        pytest.raises(RuntimeError, match="каталог"),
        TestClient(create_app(Settings(_env_file=None, catalog_path=path))),
    ):
        pytest.fail("Invalid catalog must prevent startup")


def test_missing_catalog_prevents_startup(tmp_path):
    with (
        pytest.raises(RuntimeError, match="Не удалось прочитать каталог"),
        TestClient(create_app(Settings(_env_file=None, catalog_path=tmp_path / "missing.json"))),
    ):
        pytest.fail("Missing catalog must prevent startup")


@pytest.mark.parametrize("problem", ["duplicate", "invalid_price", "nonfinite"])
def test_catalog_validation(tmp_path, problem):
    raw = json.loads((PROJECT_ROOT / "data/products.json").read_text(encoding="utf-8"))
    if problem == "duplicate":
        raw["products"].append(raw["products"][0])
    elif problem == "invalid_price":
        raw["products"][0]["price"] = "89.901"
    else:
        raw["products"][0]["price"] = "NaN"
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(RuntimeError, match="Некорректный каталог"):
        load_catalog(path)


def test_paths_do_not_depend_on_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for settings in (
        Settings(_env_file=None),
        Settings(_env_file=None, catalog_path="data/products.json"),
    ):
        with TestClient(create_app(settings)) as client:
            assert client.get("/v1/products").status_code == 200
    catalog = load_catalog(settings.catalog_path)
    assert isinstance(catalog.products[0].price, Decimal)
    assert catalog.products[0].price == Decimal("89.90")


def test_api_key_is_redacted():
    settings = Settings(_env_file=None, deepseek_api_key="test-only-secret")
    assert "test-only-secret" not in repr(settings)
