"""Delivery channels for owner alerts. Telegram is the first; Discord can follow."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC
from typing import Literal, Protocol

import httpx2

from llm_gateway.notifier.events import AlertEvent

TELEGRAM_API_BASE = "https://api.telegram.org"
_TELEGRAM_TIMEOUT = httpx2.Timeout(10.0)
# Telegram answers these when the chat, bot, or request itself is wrong; retrying
# cannot succeed.
_PERMANENT_STATUSES = frozenset({400, 401, 403, 404})
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    outcome: Literal["delivered", "retry", "rejected"]
    # A safe code for the event record, for example http_429 or ConnectError.
    code: str | None = None
    retry_after_seconds: int | None = None


class Channel(Protocol):
    name: str

    async def send(self, text: str) -> DeliveryResult: ...


def render_message(event: AlertEvent) -> str:
    when = event.first_seen_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"[{event.severity.upper()}] {event.event_type}", event.summary]
    if event.occurrences > 1:
        last = event.last_seen_at.astimezone(UTC).strftime("%H:%M UTC")
        lines.append(f"{event.occurrences} times, {when} to {last}")
    else:
        lines.append(when)
    return "\n".join(lines)


class TelegramChannel:
    name = "telegram"

    def __init__(self, *, bot_token: str, chat_id: str, client: httpx2.AsyncClient) -> None:
        self._url = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"
        self._chat_id = chat_id
        self._client = client

    async def send(self, text: str) -> DeliveryResult:
        # The token is in the URL, so only the error class and status are ever logged.
        try:
            response = await self._client.post(
                self._url,
                json={"chat_id": self._chat_id, "text": text, "disable_web_page_preview": True},
                timeout=_TELEGRAM_TIMEOUT,
            )
        except httpx2.TransportError as exc:
            code = type(exc).__name__
            logger.warning("Telegram delivery failed: %s", code)
            return DeliveryResult("retry", code=code)
        status = response.status_code
        if status == 200:
            return DeliveryResult("delivered")
        code = f"http_{status}"
        logger.warning("Telegram delivery failed: %s", code)
        if status == 429:
            return DeliveryResult("retry", code=code, retry_after_seconds=_retry_after(response))
        if status in _PERMANENT_STATUSES:
            return DeliveryResult("rejected", code=code)
        return DeliveryResult("retry", code=code)


def _retry_after(response: httpx2.Response) -> int | None:
    try:
        value = response.json().get("parameters", {}).get("retry_after")
    except (ValueError, AttributeError):
        return None
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None
