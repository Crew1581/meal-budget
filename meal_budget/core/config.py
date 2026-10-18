from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field, HttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_environment: Literal["local", "test", "staging", "production"] = "local"
    catalog_path: Path = PROJECT_ROOT / "data/products.json"
    deepseek_api_key: SecretStr = SecretStr("")
    deepseek_base_url: HttpUrl = HttpUrl("https://api.deepseek.com")
    deepseek_model: str = ""
    # omit позволяет использовать модель без поддержки параметра thinking.
    deepseek_thinking: Literal["disabled", "enabled", "omit"] = "disabled"
    deepseek_reasoning_effort: Literal["low", "high", "max"] = "low"
    deepseek_connect_timeout: float = Field(default=10, gt=0, le=60, allow_inf_nan=False)
    deepseek_read_timeout: float = Field(default=60, gt=0, le=300, allow_inf_nan=False)
    deepseek_write_timeout: float = Field(default=10, gt=0, le=60, allow_inf_nan=False)
    deepseek_pool_timeout: float = Field(default=10, gt=0, le=60, allow_inf_nan=False)
    deepseek_max_output_tokens: int = Field(default=8192, ge=1, le=32768)
    deepseek_max_response_bytes: int = Field(default=262144, ge=1024, le=1048576)
    generation_http_enabled: bool = False
    generation_app_enabled: bool = False
    generation_app_daily_installation_limit: int = Field(default=3, ge=1, le=1000)
    generation_app_daily_global_limit: int = Field(default=30, ge=1, le=10000)
    installations_daily_limit: int = Field(default=20, ge=1, le=10000)
    installations_total_limit: int = Field(default=200, ge=1, le=100000)
    generation_access_token: SecretStr = SecretStr("")
    generation_jobs_database_url: SecretStr = SecretStr("")
    generation_jobs_pool_size: int = Field(default=4, ge=1, le=10)
    generation_jobs_db_timeout: float = Field(default=5, gt=0, le=60, allow_inf_nan=False)
    generation_jobs_queue_limit: int = Field(default=4, ge=1, le=100)
    generation_jobs_retention_seconds: float = Field(
        default=86400, gt=0, le=604800, allow_inf_nan=False
    )
    generation_jobs_cleanup_interval: float = Field(default=60, gt=0, le=3600, allow_inf_nan=False)
    generation_total_timeout: float = Field(default=180, gt=0, le=600, allow_inf_nan=False)
    generation_nutrition_tolerance: Decimal = Field(
        default=Decimal("0.10"), ge=0, le=1, allow_inf_nan=False
    )

    @field_validator("catalog_path")
    @classmethod
    def resolve_catalog_path(cls, value: Path) -> Path:
        return value if value.is_absolute() else PROJECT_ROOT / value
