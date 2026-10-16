"""Typed errors for every gateway problem code; unknown codes fail closed."""

from __future__ import annotations

from typing import Any

# Waiting and resubmitting the same call ID can succeed.
RETRYABLE_CODES = frozenset(
    {
        "provider_rate_limited",
        "provider_unavailable",
        "rate_limited",
        "concurrency_exhausted",
        "call_in_progress",
        "allocation_pending",
    }
)
INVALID_REQUEST_CODES = frozenset(
    {
        "cost_ceiling_exceeded",
        "request_invalid",
        "request_too_large",
        "profile_not_found",
        "model_not_in_catalogue",
        "invalid_model_settings",
        "input_not_supported",
        "continuation_mismatch",
        "provider_invalid_request",
        "idempotency_conflict",
        "stub_not_allowed",
        "not_found",
        "method_not_allowed",
        "model_not_found",
        "model_task_mismatch",
        "credential_changed",
        "call_configuration_changed",
    }
)
AUTHENTICATION_CODES = frozenset(
    {"key_missing", "key_malformed", "key_invalid", "project_disabled", "auth_throttled"}
)
UNAVAILABLE_CODES = frozenset(
    {
        "provider_account_blocked",
        "provider_not_configured",
        "provider_failed",
        "project_quarantined",
        "call_result_expired",
        "price_unavailable",
        "dependency_unavailable",
        "internal_error",
        "model_registry_unavailable",
        "provider_credentials_missing",
        "provider_credentials_unreadable",
        "contract_upgrade_required",
    }
)
KNOWN_CODES = (
    RETRYABLE_CODES
    | INVALID_REQUEST_CODES
    | AUTHENTICATION_CODES
    | UNAVAILABLE_CODES
    | {"execution_unknown", "budget_exhausted"}
)


class GatewayError(Exception):
    """A gateway problem. Branch on `code`, never on the message."""

    def __init__(
        self,
        code: str,
        *,
        status: int | None = None,
        title: str | None = None,
        detail: str | None = None,
        request_id: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(f"{code}: {detail or title or 'gateway error'}")
        self.code = code
        self.status = status
        self.title = title
        self.detail = detail
        self.request_id = request_id
        self.retry_after_seconds = retry_after_seconds


class RetryableError(GatewayError):
    """Waiting `retry_after_seconds` and resubmitting the same call ID can succeed."""


class ExecutionUnknownError(GatewayError):
    """The provider may have run the call. Never resubmit this call ID; use a new one."""


class BudgetExhaustedError(GatewayError):
    """The monthly budget is spent; `retry_after_seconds` reaches the next month."""


class InvalidRequestError(GatewayError):
    """The request cannot succeed as sent."""


class AuthenticationError(GatewayError):
    """The key is missing, invalid, throttled, or its project is disabled."""


class GatewayUnavailableError(GatewayError):
    """The gateway or provider cannot serve this call, and waiting will not help."""


def error_from_problem(
    status: int, problem: dict[str, Any], retry_after: float | None
) -> GatewayError:
    code = str(problem.get("code") or "unknown")
    fields: dict[str, Any] = {
        "status": status,
        "title": problem.get("title"),
        "detail": problem.get("detail"),
        "request_id": problem.get("request_id"),
        "retry_after_seconds": retry_after,
    }
    if code in RETRYABLE_CODES:
        return RetryableError(code, **fields)
    if code == "execution_unknown":
        return ExecutionUnknownError(code, **fields)
    if code == "budget_exhausted":
        return BudgetExhaustedError(code, **fields)
    if code in INVALID_REQUEST_CODES:
        return InvalidRequestError(code, **fields)
    if code in AUTHENTICATION_CODES:
        return AuthenticationError(code, **fields)
    if code in UNAVAILABLE_CODES:
        return GatewayUnavailableError(code, **fields)
    # A code this version does not know is never retried.
    return GatewayError(code, **fields)
