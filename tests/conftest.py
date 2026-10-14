import pytest
from fastapi.testclient import TestClient

from main import create_app
from meal_budget.core.config import PROJECT_ROOT, Settings


@pytest.fixture(autouse=True)
def isolated_catalog(monkeypatch):
    # Обычные тесты не зависят от обновляемого веб-каталога.
    monkeypatch.setenv("CATALOG_PATH", str(PROJECT_ROOT / "tests/fixtures/products.json"))


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    settings = Settings(_env_file=None, deepseek_api_key="")
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def generation_payload():
    return {
        "days": 7,
        "budget": "2500.50",
        "daily_targets": {
            "calories_kcal": 2000,
            "protein_g": 100,
            "fat_g": 70,
            "carbs_g": 250,
        },
        "excluded_product_ids": ["oats"],
        "disliked_product_ids": [],
        "max_cooking_minutes": 30,
        "meals_per_day": 3,
        "price_region": "Москва",
        "wishes": "Простые блюда",
    }
