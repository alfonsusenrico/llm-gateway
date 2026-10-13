"""The provider boundary: what an adapter returns, and what each failure means."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, Protocol

from llm_gateway.calls.records import CallState
from llm_gateway.catalogue.pricing import TokenUsage
from llm_gateway.llm.request import ResponseRequest
from llm_gateway.llm.resolve import ResolvedCall

FailureCategory = Literal[
    "connection_not_established",
    "temporary_throttle",
    "temporary_overload",
    "account_blocked",
    "authentication",
    "invalid_request",
    "execution_unknown",
    "provider_failed",
]
_DEFAULT_RETRY_SECONDS = 5


@dataclass(frozen=True, slots=True)
class ProviderSuccess:
    status: Literal["completed", "incomplete"]
    output_text: str
    refusal: str | None
    incomplete_reason: str | None
    usage: TokenUsage
    reasoning_tokens: int | None
    service_tier: str | None
    provider_response_id: str | None
    provider_request_id: str | None


@dataclass(frozen=True, slots=True)
class ProviderFailure:
    category: FailureCategory
    # A safe provider code such as server_is_overloaded, never provider prose.
    provider_code: str | None = None
    retry_after_seconds: int | None = None
    usage: TokenUsage | None = None
    provider_request_id: str | None = None


@dataclass(frozen=True, slots=True)
class StreamStarted:
    """The provider accepted the call; from here a failure may follow execution."""

    provider_request_id: str | None = None


@dataclass(frozen=True, slots=True)
class StreamText:
    delta: str


@dataclass(frozen=True, slots=True)
class StreamRefusal:
    delta: str


StreamEvent = StreamStarted | StreamText | StreamRefusal | ProviderSuccess | ProviderFailure


class ProviderAdapter(Protocol):
    async def execute(
        self, resolved: ResolvedCall, request: ResponseRequest
    ) -> ProviderSuccess | ProviderFailure: ...

    def execute_stream(
        self, resolved: ResolvedCall, request: ResponseRequest
    ) -> AsyncIterator[StreamEvent]:
        """Yield events and end with exactly one ProviderSuccess or ProviderFailure."""
        ...


@dataclass(frozen=True, slots=True)
class FailurePolicy:
    problem_code: str
    settled_state: CallState
    retry_after_seconds: int | None
    alert: bool


def failure_policy(failure: ProviderFailure) -> FailurePolicy:
    """What the gateway does with a provider failure; see the design's failure table."""
    retry = failure.retry_after_seconds or _DEFAULT_RETRY_SECONDS
    match failure.category:
        case "connection_not_established" | "temporary_overload":
            return FailurePolicy("provider_unavailable", "retryable_failure", retry, False)
        case "temporary_throttle":
            return FailurePolicy("provider_rate_limited", "retryable_failure", retry, False)
        case "account_blocked" | "authentication":
            return FailurePolicy("provider_account_blocked", "failed", None, True)
        case "invalid_request":
            return FailurePolicy("provider_invalid_request", "failed", None, False)
        case "provider_failed":
            return FailurePolicy("provider_failed", "failed", None, False)
        case "execution_unknown":
            return FailurePolicy("execution_unknown", "execution_unknown", None, False)
