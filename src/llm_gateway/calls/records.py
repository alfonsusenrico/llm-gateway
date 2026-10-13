"""Call records, the call state machine, and admission refusals."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, LiteralString

CallState = Literal["admitted", "completed", "failed", "retryable_failure", "execution_unknown"]
FINISHED_STATES: frozenset[CallState] = frozenset(
    {"completed", "failed", "retryable_failure", "execution_unknown"}
)
# The only allowed transitions; everything else is rejected in code.
_ALLOWED: dict[CallState, frozenset[CallState]] = {
    "admitted": FINISHED_STATES,
    "retryable_failure": frozenset({"admitted"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "execution_unknown": frozenset(),
}


class IllegalTransitionError(RuntimeError):
    def __init__(self, from_state: str, to_state: str) -> None:
        super().__init__(f"Call state cannot change from {from_state} to {to_state}.")


def check_transition(from_state: CallState, to_state: CallState) -> None:
    if to_state not in _ALLOWED[from_state]:
        raise IllegalTransitionError(from_state, to_state)


class AdmissionRefusedError(Exception):
    """A call refused before reaching a provider, with a stable code for the API layer."""

    def __init__(self, code: str, *, status: int, retry_after_seconds: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.retry_after_seconds = retry_after_seconds

    @property
    def retryable(self) -> bool:
        return self.retry_after_seconds is not None


@dataclass(frozen=True, slots=True)
class CallRecord:
    call_key: int
    project_id: str
    environment: str
    provider_call_id: str
    request_fingerprint: str
    operation: str
    provider: str
    model: str
    service_tier_requested: str
    service_tier_used: str | None
    state: CallState
    reserved_micro_usd: int
    cost_micro_usd: int | None
    charged_micro_usd: int
    failure_code: str | None
    admitted_at: datetime
    finished_at: datetime | None


CALL_COLUMNS: LiteralString = (
    "call_key, project_id, environment, provider_call_id, request_fingerprint, operation, "
    "provider, model, service_tier_requested, service_tier_used, state, reserved_micro_usd, "
    "cost_micro_usd, charged_micro_usd, failure_code, admitted_at, finished_at"
)


@dataclass(frozen=True, slots=True)
class Allowance:
    """What remains of the grant after this call, for response headers."""

    requests_per_minute_limit: int
    requests_per_minute_remaining: int
    requests_per_minute_reset_seconds: int
    concurrency_limit: int
    concurrency_remaining: int
    budget_remaining_micro_usd: int


@dataclass(frozen=True, slots=True)
class CallRequest:
    """What admission needs to know about a call; never its content."""

    project_id: str
    environment: str
    provider_call_id: str
    request_fingerprint: str
    operation: str
    provider: str
    model: str
    service_tier: Literal["standard", "flex"]
    request_bytes: int
    max_output_tokens: int


@dataclass(frozen=True, slots=True)
class Admitted:
    call_key: int
    reserved_micro_usd: int
    allowance: Allowance


@dataclass(frozen=True, slots=True)
class AlreadyFinished:
    """The call ID already ended; the API replays its recorded outcome."""

    call: CallRecord
