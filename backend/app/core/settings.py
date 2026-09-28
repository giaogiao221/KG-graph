from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EXTRACTION_")

    environment: str = "development"
    jwt_secret: SecretStr = Field(min_length=32)
    storage_root: Path = Path("storage")
    max_upload_bytes: int = Field(default=25 * 1024 * 1024, gt=0, le=1024**3)
    max_export_rows: int = Field(default=100_000, gt=0, le=1_000_000)
    max_export_bytes: int = Field(default=100 * 1024 * 1024, gt=0, le=2 * 1024**3)

    @field_validator("jwt_secret", mode="before")
    @classmethod
    def strip_jwt_secret(cls, value: str | SecretStr) -> SecretStr:
        raw_value = value.get_secret_value() if isinstance(value, SecretStr) else value
        return SecretStr(raw_value.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
