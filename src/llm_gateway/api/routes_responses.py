"""`POST /v1/responses`: one model call through the gateway."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from llm_gateway.api.auth import ProjectIdentity, require_project
from llm_gateway.api.problems import GatewayError, problem_response
from llm_gateway.api.routes_projects import AUTH_RESPONSES
from llm_gateway.calls.records import AdmissionRefusedError, Allowance
from llm_gateway.llm.errors import CallRejectedError
from llm_gateway.llm.request import ResponseRequest
from llm_gateway.llm.service import CallDeps, run_call
from llm_gateway.money import format_usd

router = APIRouter(prefix="/v1", tags=["responses"])

_ADMISSION_TITLES = {
    "allocation_pending": "Grant not computed yet",
    "price_unavailable": "No price for this model and tier",
    "cost_ceiling_exceeded": "Worst-case cost exceeds the per-call ceiling",
    "concurrency_exhausted": "Granted concurrency is in use",
    "rate_limited": "Granted request rate reached",
    "budget_exhausted": "Monthly budget exhausted",
    "idempotency_conflict": "Call ID was used for a different request",
    "call_in_progress": "This call ID is still running",
}
_RESPONSES: dict[int | str, dict[str, Any]] = {
    **AUTH_RESPONSES,
    403: {"description": "`project_disabled`, or `stub_not_allowed` for a production key."},
    409: {
        "description": "`idempotency_conflict`, or `call_in_progress` (retry after `Retry-After`)."
    },
    410: {"description": "`call_result_expired`: a completed call older than the retention."},
    413: {"description": "`request_too_large`."},
    422: {
        "description": "`request_invalid`, `profile_not_found`, `model_not_in_catalogue`, "
        "`invalid_model_settings`, `input_not_supported`, `cost_ceiling_exceeded`, "
        "`provider_invalid_request`."
    },
    429: {
        "description": "`concurrency_exhausted`, `rate_limited`, `budget_exhausted`, "
        "`provider_rate_limited`, `auth_throttled`; with `Retry-After`."
    },
    502: {"description": "`execution_unknown` (never retry this call ID) or `provider_failed`."},
    503: {
        "description": "`allocation_pending`, `price_unavailable`, `provider_not_configured`, "
        "`provider_unavailable` (retry with `Retry-After`), `provider_account_blocked`, "
        "`project_quarantined`."
    },
}


def allowance_headers(allowance: Allowance) -> dict[str, str]:
    return {
        "X-RateLimit-Limit": str(allowance.requests_per_minute_limit),
        "X-RateLimit-Remaining": str(allowance.requests_per_minute_remaining),
        "X-RateLimit-Reset": str(allowance.requests_per_minute_reset_seconds),
        "X-Concurrency-Limit": str(allowance.concurrency_limit),
        "X-Concurrency-Remaining": str(allowance.concurrency_remaining),
        "X-Budget-Remaining-USD": format_usd(allowance.budget_remaining_micro_usd),
    }


@router.post("/responses", responses=_RESPONSES)
async def create_response(
    request: Request, identity: Annotated[ProjectIdentity, Depends(require_project)]
) -> JSONResponse:
    """Run one model call. Resubmitting the same call ID never runs it twice."""
    deps: CallDeps = request.app.state.call_deps
    raw = await request.body()
    if len(raw) > deps.settings.request_max_bytes:
        raise GatewayError("request_too_large", status=413, title="Request is too large")
    try:
        call = ResponseRequest.model_validate_json(raw)
    except ValidationError as exc:
        errors = [
            {
                "field": ".".join(str(part) for part in error.get("loc", ())),
                "code": str(error.get("type", "invalid")),
            }
            for error in exc.errors()
        ]
        return problem_response(
            code="request_invalid", status=422, title="Request is invalid", errors=errors
        )
    try:
        result = await run_call(
            deps,
            project_id=identity.project_id,
            environment=identity.environment,
            contract=identity.contract,
            request=call,
            request_bytes=len(raw),
        )
    except CallRejectedError as exc:
        raise GatewayError(
            exc.code,
            status=exc.status,
            title=exc.title,
            detail=exc.detail,
            retry_after_seconds=exc.retry_after_seconds,
        ) from None
    except AdmissionRefusedError as exc:
        raise GatewayError(
            exc.code,
            status=exc.status,
            title=_ADMISSION_TITLES.get(exc.code, "Call refused"),
            retry_after_seconds=exc.retry_after_seconds,
        ) from None
    headers = allowance_headers(result.allowance) if result.allowance else {}
    return JSONResponse(result.body, headers=headers)
