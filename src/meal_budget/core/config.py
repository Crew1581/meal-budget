from pathlib import Path
from typing import Literal

from pydantic import HttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_environment: Literal["local", "test", "staging", "production"] = "local"
    catalog_path: Path = PROJECT_ROOT / "data/products.json"
    deepseek_api_key: SecretStr = SecretStr("")
    deepseek_base_url: HttpUrl = HttpUrl("https://api.deepseek.com")
    deepseek_model: str = ""

    @field_validator("catalog_path")
    @classmethod
    def resolve_catalog_path(cls, value: Path) -> Path:
        return value if value.is_absolute() else PROJECT_ROOT / value
