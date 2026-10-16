"""Typed process settings, validated once at startup."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from pydantic import Field, PostgresDsn, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from llm_gateway.projects.keys import decode_pepper


class SettingsError(RuntimeError):
    """Invalid configuration. The message names variables, never their values."""


UsdSetting = Annotated[Decimal, Field(ge=0, le=1000, decimal_places=6)]


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

    # Keyed hash secret for project API keys. Changing it invalidates every key.
    project_key_pepper: SecretStr
    auth_failure_limit_per_minute: int = Field(default=10, ge=1, le=1_000)
    # Set only behind a trusted proxy (CF-Connecting-IP behind the Cloudflare
    # tunnel); otherwise the TCP peer address identifies the client.
    trusted_client_ip_header: str | None = Field(default=None, pattern=r"^[A-Za-z0-9-]{1,64}$")

    # A provider call's deadline; an admitted call's lease adds a minute to it.
    llm_call_timeout_seconds: int = Field(default=180, ge=10, le=900)
    ollama_base_url: str | None = Field(default=None, max_length=2048)
    embeddings_base_url: str | None = Field(default=None, max_length=2048)
    provider_credential_encryption_key: SecretStr
    # Fernet key for stored call results; changing it makes stored results expire.
    result_encryption_key: SecretStr
    result_retention_seconds: int = Field(default=86_400, ge=3_600, le=604_800)
    request_max_bytes: int = Field(default=20_000_000, ge=10_000, le=50_000_000)
    default_max_output_tokens: int = Field(default=4_096, ge=1, le=128_000)

    # Shared capacity that grants divide, and the guaranteed minimum per grant.
    allocation_capacity_requests_per_minute: int = Field(default=120, ge=1, le=100_000)
    allocation_capacity_concurrency: int = Field(default=8, ge=1, le=1_000)
    global_monthly_budget_usd: UsdSetting = Decimal("5.00")
    allocation_min_requests_per_minute: int = Field(default=5, ge=0, le=100_000)
    allocation_min_concurrency: int = Field(default=1, ge=0, le=1_000)
    allocation_min_monthly_budget_usd: UsdSetting = Decimal("0.10")

    # Owner notifier. Delivery is disabled until both Telegram values are set.
    notifier_telegram_bot_token: SecretStr | None = None
    # A numeric chat ID; negative for groups and channels.
    notifier_telegram_chat_id: str | None = Field(default=None, pattern=r"^-?\d{1,20}$")
    notifier_dedupe_window_seconds: int = Field(default=900, ge=60, le=86_400)
    notifier_max_event_age_seconds: int = Field(default=86_400, ge=3_600, le=604_800)

    @field_validator("result_encryption_key", "provider_credential_encryption_key")
    @classmethod
    def _encryption_key_is_fernet(cls, value: SecretStr) -> SecretStr:
        Fernet(value.get_secret_value().encode("ascii"))
        return value

    @field_validator("ollama_base_url", "embeddings_base_url")
    @classmethod
    def _local_url_is_backend_destination(cls, value: str | None) -> str | None:
        if value is None:
            return None
        url = urlsplit(value)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or any(character.isspace() or ord(character) < 32 for character in value)
        ):
            raise ValueError("must be an HTTP URL without credentials, query or fragment")
        _ = url.port  # Validate malformed/out-of-range ports without exposing the URL.
        return value.rstrip("/")

    @field_validator("project_key_pepper")
    @classmethod
    def _pepper_is_strong(cls, value: SecretStr) -> SecretStr:
        decode_pepper(value.get_secret_value())
        return value

    @property
    def project_key_pepper_bytes(self) -> bytes:
        return decode_pepper(self.project_key_pepper.get_secret_value())

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
    if settings.provider_credential_encryption_key == settings.result_encryption_key:
        raise SettingsError(
            "PROVIDER_CREDENTIAL_ENCRYPTION_KEY must differ from RESULT_ENCRYPTION_KEY"
        )
    if settings.db_pool_min_size > settings.db_pool_max_size:
        raise SettingsError("Invalid settings: DB_POOL_MIN_SIZE exceeds DB_POOL_MAX_SIZE")
    if (settings.notifier_telegram_bot_token is None) != (
        settings.notifier_telegram_chat_id is None
    ):
        raise SettingsError(
            "Invalid settings: set both NOTIFIER_TELEGRAM_BOT_TOKEN and "
            "NOTIFIER_TELEGRAM_CHAT_ID, or neither"
        )
    minimums = (
        ("ALLOCATION_MIN_REQUESTS_PER_MINUTE", settings.allocation_min_requests_per_minute,
         settings.allocation_capacity_requests_per_minute),
        ("ALLOCATION_MIN_CONCURRENCY", settings.allocation_min_concurrency,
         settings.allocation_capacity_concurrency),
        ("ALLOCATION_MIN_MONTHLY_BUDGET_USD", settings.allocation_min_monthly_budget_usd,
         settings.global_monthly_budget_usd),
    )  # fmt: skip
    for name, minimum, capacity in minimums:
        if minimum > capacity:
            raise SettingsError(f"Invalid settings: {name} exceeds its capacity")
    return settings
