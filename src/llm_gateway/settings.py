"""Typed process settings, validated once at startup."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, PostgresDsn, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class SettingsError(RuntimeError):
    """Invalid configuration. The message names variables, never their values."""


class Settings(BaseSettings):
    # Field names map to upper-case environment variables (APP_ENV, DATABASE_URL, ...).
    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore", frozen=True)

    # Production is the safe default; development behaviour is opt-in.
    app_env: Literal["development", "production"] = "production"
    app_revision: str = Field(default="unknown", max_length=64)
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    database_url: PostgresDsn
    db_pool_min_size: int = Field(default=1, ge=1, le=20)
    db_pool_max_size: int = Field(default=5, ge=1, le=50)


def load_settings() -> Settings:
    """Load settings from the environment or stop with the names of invalid variables."""
    try:
        settings = Settings()  # pyright: ignore[reportCallIssue]  # values come from the environment
    except ValidationError as exc:
        names = sorted({str(error["loc"][0]).upper() for error in exc.errors() if error["loc"]})
        raise SettingsError(f"Invalid or missing settings: {', '.join(names)}") from None
    if settings.db_pool_min_size > settings.db_pool_max_size:
        raise SettingsError("Invalid settings: DB_POOL_MIN_SIZE exceeds DB_POOL_MAX_SIZE")
    return settings
