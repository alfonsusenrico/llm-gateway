from __future__ import annotations

import pytest

from llm_gateway.settings import SettingsError, load_settings
from tests.conftest import TEST_PEPPER


@pytest.fixture(autouse=True)
def _pepper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROJECT_KEY_PEPPER", TEST_PEPPER)


def test_production_is_the_default_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://gateway:secret@db:5432/gateway")

    settings = load_settings()

    assert settings.app_env == "production"


def test_missing_required_setting_names_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(SettingsError, match="DATABASE_URL"):
        load_settings()


def test_invalid_value_is_never_echoed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "not-a-url-with-secret-token-123")

    with pytest.raises(SettingsError) as raised:
        load_settings()

    assert "secret-token-123" not in str(raised.value)
    assert "DATABASE_URL" in str(raised.value)


def test_pool_bounds_must_be_ordered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://gateway:secret@db:5432/gateway")
    monkeypatch.setenv("DB_POOL_MIN_SIZE", "6")
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "5")

    with pytest.raises(SettingsError, match="DB_POOL_MIN_SIZE"):
        load_settings()


def test_telegram_values_must_be_set_together(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://gateway:secret@db:5432/gateway")
    monkeypatch.setenv("NOTIFIER_TELEGRAM_BOT_TOKEN", "123:secret-token-value")
    monkeypatch.delenv("NOTIFIER_TELEGRAM_CHAT_ID", raising=False)

    with pytest.raises(SettingsError) as raised:
        load_settings()

    assert "NOTIFIER_TELEGRAM_CHAT_ID" in str(raised.value)
    assert "secret-token-value" not in str(raised.value)


def test_empty_telegram_values_mean_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://gateway:secret@db:5432/gateway")
    monkeypatch.setenv("NOTIFIER_TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("NOTIFIER_TELEGRAM_CHAT_ID", "")

    assert load_settings().telegram_configured is False


def test_chat_id_must_be_numeric(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://gateway:secret@db:5432/gateway")
    monkeypatch.setenv("NOTIFIER_TELEGRAM_BOT_TOKEN", "123:secret-token-value")
    monkeypatch.setenv("NOTIFIER_TELEGRAM_CHAT_ID", "@my_channel")

    with pytest.raises(SettingsError, match="NOTIFIER_TELEGRAM_CHAT_ID"):
        load_settings()


def test_token_is_hidden_in_the_settings_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://gateway:secret@db:5432/gateway")
    monkeypatch.setenv("NOTIFIER_TELEGRAM_BOT_TOKEN", "123:secret-token-value")
    monkeypatch.setenv("NOTIFIER_TELEGRAM_CHAT_ID", "-1001")

    settings = load_settings()

    assert settings.telegram_configured is True
    assert "secret-token-value" not in repr(settings)


def test_missing_pepper_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://gateway:secret@db:5432/gateway")
    monkeypatch.delenv("PROJECT_KEY_PEPPER")

    with pytest.raises(SettingsError, match="PROJECT_KEY_PEPPER"):
        load_settings()


def test_short_pepper_is_rejected_without_echo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://gateway:secret@db:5432/gateway")
    monkeypatch.setenv("PROJECT_KEY_PEPPER", "c2hvcnQtcGVwcGVy")

    with pytest.raises(SettingsError) as raised:
        load_settings()

    assert "PROJECT_KEY_PEPPER" in str(raised.value)
    assert "c2hvcnQ" not in str(raised.value)
