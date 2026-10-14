import json
from decimal import Decimal


def test_health_without_key(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_products_contract(client):
    response = client.get("/v1/products")
    assert response.status_code == 200
    body = response.json()
    assert body["data_status"] == "demo_unverified"
    assert body["warning"]
    assert body["catalog_version"] == "synthetic-meal-3-v1"
    product = body["products"][0]
    assert set(product) == {
        "id",
        "name",
        "state",
        "nutrition_per_100g",
        "package_size_g",
        "price",
        "provenance",
        "price_region",
        "price_source",
        "price_updated_at",
    }
    assert set(product["nutrition_per_100g"]) == {
        "calories_kcal",
        "protein_g",
        "fat_g",
        "carbs_g",
    }
    assert product["id"] == "oats"
    assert product["state"] == "dry"
    assert product["package_size_g"] == 500
    assert isinstance(product["price"], (int, float))
    assert json.loads(response.text, parse_float=Decimal)["products"][0]["price"] == Decimal(
        "89.90"
    )


def test_documentation_and_no_generation_endpoint(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 200
    assert set(client.get("/openapi.json").json()["paths"]) == {
        "/health",
        "/v1/products",
        "/v1/menu-calculations",
    }
    assert client.post("/v1/generations", json={}).status_code == 404
