from __future__ import annotations

import anyio

from llm_gateway.notifier.channels import DeliveryResult


class FakeChannel:
    """Records sent texts and answers with scripted results (delivered by default)."""

    name = "fake"

    def __init__(self, *results: DeliveryResult, delay_seconds: float = 0.0) -> None:
        self.sent: list[str] = []
        self._results = list(results)
        self._delay_seconds = delay_seconds

    async def send(self, text: str) -> DeliveryResult:
        if self._delay_seconds:
            await anyio.sleep(self._delay_seconds)
        self.sent.append(text)
        if self._results:
            return self._results.pop(0)
        return DeliveryResult("delivered")
