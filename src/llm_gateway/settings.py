"""Typed process settings, validated once at startup."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, PostgresDsn, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class SettingsError(RuntimeError):
    """Invalid configuration. The message names variables, never their values."""


class Settings(BaseSettings):
    # Field names map to upper-case environment variables (APP_ENV, DATABASE_URL, ...).
    # Empty variables count as unset, so compose `${NAME:-}` passthrough stays optional.
    model_config = SettingsConfigDict(
        case_sensitive=False, extra="ignore", frozen=True, env_ignore_empty=True
    )

    # Production is the safe default; development behaviour is opt-in.
    app_env: Literal["development", "production"] = "production"
    app_revision: str = Field(default="unknown", max_length=64)
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    database_url: PostgresDsn
    db_pool_min_size: int = Field(default=1, ge=1, le=20)
    db_pool_max_size: int = Field(default=5, ge=1, le=50)

    # Owner notifier. Delivery is disabled until both Telegram values are set.
    notifier_telegram_bot_token: SecretStr | None = None
    # A numeric chat ID; negative for groups and channels.
    notifier_telegram_chat_id: str | None = Field(default=None, pattern=r"^-?\d{1,20}$")
    notifier_dedupe_window_seconds: int = Field(default=900, ge=60, le=86_400)
    notifier_max_event_age_seconds: int = Field(default=86_400, ge=3_600, le=604_800)

    @property
    def telegram_configured(self) -> bool:
        return self.notifier_telegram_bot_token is not None and bool(self.notifier_telegram_chat_id)


def load_settings() -> Settings:
    """Load settings from the environment or stop with the names of invalid variables."""
    try:
        settings = Settings()  # pyright: ignore[reportCallIssue]  # values come from the environment
    except ValidationError as exc:
        names = sorted({str(error["loc"][0]).upper() for error in exc.errors() if error["loc"]})
        raise SettingsError(f"Invalid or missing settings: {', '.join(names)}") from None
    if settings.db_pool_min_size > settings.db_pool_max_size:
        raise SettingsError("Invalid settings: DB_POOL_MIN_SIZE exceeds DB_POOL_MAX_SIZE")
    if (settings.notifier_telegram_bot_token is None) != (
        settings.notifier_telegram_chat_id is None
    ):
        raise SettingsError(
            "Invalid settings: set both NOTIFIER_TELEGRAM_BOT_TOKEN and "
            "NOTIFIER_TELEGRAM_CHAT_ID, or neither"
        )
    return settings
