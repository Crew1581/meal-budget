FROM ghcr.io/astral-sh/uv:0.12.5 AS uv
FROM python:3.13.15-slim
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    APP_ENVIRONMENT=production
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-cache && \
    groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home app && \
    mkdir generated && chown 10001:10001 generated && chmod 700 generated
COPY src/ ./src/
COPY data/ ./data/
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "meal_budget.main:app", "--app-dir", "src", "--host", "0.0.0.0", "--port", "8000"]
