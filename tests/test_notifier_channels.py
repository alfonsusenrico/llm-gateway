from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import httpx2
import pytest

from llm_gateway.logging_setup import limit_library_logging
from llm_gateway.notifier.channels import TelegramChannel, render_message
from llm_gateway.notifier.events import AlertEvent

pytestmark = pytest.mark.anyio
TOKEN = "123456:SECRET-bot-token"


def _channel(handler: httpx2.MockTransport) -> TelegramChannel:
    return TelegramChannel(
        bot_token=TOKEN, chat_id="-1001", client=httpx2.AsyncClient(transport=handler)
    )


def _answer(status: int, body: dict[str, object] | None = None) -> httpx2.MockTransport:
    return httpx2.MockTransport(lambda _request: httpx2.Response(status, json=body or {}))


async def test_success_posts_the_text_to_the_chat() -> None:
    captured: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured.append(request)
        return httpx2.Response(200, json={"ok": True})

    result = await _channel(httpx2.MockTransport(handler)).send("hello")

    assert result.outcome == "delivered"
    assert captured[0].url.path == f"/bot{TOKEN}/sendMessage"
    assert json.loads(captured[0].content) == {
        "chat_id": "-1001",
        "text": "hello",
        "disable_web_page_preview": True,
    }


async def test_rate_limit_uses_the_retry_after_parameter() -> None:
    body = {"ok": False, "parameters": {"retry_after": 17}}

    result = await _channel(_answer(429, body)).send("hello")

    assert (result.outcome, result.code, result.retry_after_seconds) == ("retry", "http_429", 17)


async def test_server_error_is_retried() -> None:
    result = await _channel(_answer(502)).send("hello")

    assert (result.outcome, result.code, result.retry_after_seconds) == ("retry", "http_502", None)


@pytest.mark.parametrize("status", [400, 401, 403, 404])
async def test_wrong_chat_or_token_is_rejected(status: int) -> None:
    result = await _channel(_answer(status)).send("hello")

    assert (result.outcome, result.code) == ("rejected", f"http_{status}")


async def test_transport_error_is_retried_and_never_logs_the_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError(f"cannot reach {request.url}", request=request)

    limit_library_logging()
    with caplog.at_level(logging.DEBUG):
        result = await _channel(httpx2.MockTransport(handler)).send("hello")

    assert (result.outcome, result.code) == ("retry", "ConnectError")
    assert "SECRET" not in caplog.text
    assert "ConnectError" in caplog.text


async def test_error_response_never_logs_the_token(caplog: pytest.LogCaptureFixture) -> None:
    limit_library_logging()
    with caplog.at_level(logging.DEBUG):
        await _channel(_answer(401, {"description": "Unauthorized"})).send("hello")

    assert "SECRET" not in caplog.text
    assert "Unauthorized" not in caplog.text


def _event(occurrences: int) -> AlertEvent:
    first = datetime(2026, 10, 13, 17, 0, tzinfo=timezone(timedelta(hours=7)))
    return AlertEvent(
        event_id=1,
        event_type="project_key_rejected",
        severity="warning",
        summary="A revoked key is still in use.",
        occurrences=occurrences,
        first_seen_at=first,
        last_seen_at=first + timedelta(minutes=5),
        status="pending",
        attempts=0,
    )


def test_single_event_message() -> None:
    assert render_message(_event(1)) == (
        "[WARNING] project_key_rejected\nA revoked key is still in use.\n2026-10-13 10:00 UTC"
    )


def test_repeated_event_message_reports_count_and_span_in_utc() -> None:
    # The event times are UTC+7; the message shows them in UTC.
    assert (
        render_message(_event(3)).splitlines()[-1] == "3 times, 2026-10-13 10:00 UTC to 10:05 UTC"
    )
