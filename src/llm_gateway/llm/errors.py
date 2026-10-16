"""Call problems: one stable code per outcome, with its HTTP status and retry hint."""

from __future__ import annotations

# The HTTP status of every call problem code, so a replayed failure answers exactly
# as the original did.
PROBLEM_STATUS: dict[str, int] = {
    "request_invalid": 422,
    "request_too_large": 413,
    "profile_not_found": 422,
    "model_not_in_catalogue": 422,
    "invalid_model_settings": 422,
    "input_not_supported": 422,
    "continuation_mismatch": 422,
    "stub_not_allowed": 403,
    "provider_not_configured": 503,
    "provider_configuration_mismatch": 503,
    "provider_unavailable": 503,
    "provider_rate_limited": 429,
    "provider_account_blocked": 503,
    "provider_invalid_request": 422,
    "provider_failed": 502,
    "execution_unknown": 502,
    "call_result_expired": 410,
}
PROBLEM_STATUS.update(
    {
        "idempotency_conflict": 409,
        "project_disabled": 403,
        "project_quarantined": 503,
        "model_task_mismatch": 422,
        "model_registry_unavailable": 503,
        "provider_credentials_missing": 503,
        "provider_credentials_unreadable": 503,
        "contract_upgrade_required": 503,
        "credential_changed": 409,
        "call_configuration_changed": 409,
    }
)
PROBLEM_TITLES: dict[str, str] = {
    "request_invalid": "Request is invalid",
    "request_too_large": "Request is too large",
    "profile_not_found": "Profile not found",
    "model_not_in_catalogue": "Model is not in the catalogue",
    "invalid_model_settings": "Settings are not valid for this model",
    "input_not_supported": "Input type is not supported by this model",
    "continuation_mismatch": "Continuation comes from another provider or model",
    "stub_not_allowed": "The stub model is for development keys only",
    "provider_not_configured": "Provider is not configured",
    "provider_configuration_mismatch": "Provider configuration does not match the registered model",
    "provider_unavailable": "Provider is temporarily unavailable",
    "provider_rate_limited": "Provider is rate limiting",
    "provider_account_blocked": "Provider account cannot serve calls",
    "provider_invalid_request": "Provider rejected the request",
    "provider_failed": "Provider failed the call",
    "execution_unknown": "Outcome unknown; use a new call ID to try again",
    "call_result_expired": "The stored result has expired",
}

PROBLEM_TITLES.update(
    {
        "idempotency_conflict": "Call ID was used for another request",
        "project_disabled": "Project is disabled",
        "project_quarantined": "Project contract is invalid",
        "model_task_mismatch": "Model belongs to another inference task",
        "model_registry_unavailable": "Model registration requires synchronization",
        "provider_credentials_missing": "Project provider credentials are missing",
        "provider_credentials_unreadable": "Project provider credentials are unreadable",
        "contract_upgrade_required": "Project contract requires upgrade",
        "credential_changed": "Provider credentials changed; start a new call",
        "call_configuration_changed": "Call configuration changed; start a new call",
    }
)


class CallRejectedError(Exception):
    def __init__(
        self,
        code: str,
        *,
        detail: str | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.status = PROBLEM_STATUS[code]
        self.title = PROBLEM_TITLES[code]
        self.detail = detail
        self.retry_after_seconds = retry_after_seconds
