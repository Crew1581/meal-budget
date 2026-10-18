"""Только localhost: фиксированный demo-ответ, без DeepSeek и внешней сети."""

import json

import uvicorn
from fastapi import FastAPI

app = FastAPI(title="Local synthetic generation provider")


@app.post("/chat/completions")
async def completion():
    # Только для examples/generation-request.json и data/products.json.
    plan = {
        "recipes": [
            {
                "id": "oats-demo",
                "name": "Технический пример, не рекомендация",
                "servings": 1,
                "ingredients": [{"product_id": "oats", "grams": 100}],
                "steps": ["Приготовить"],
                "cooking_minutes": 10,
            }
        ],
        "days": [{"day": 1, "meals": [{"recipe_id": "oats-demo", "servings": 1}]}],
    }
    return {
        "model": "local-test",
        "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(plan)}}],
    }


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=9001, access_log=False)
