"""RFC 9457 problem responses: the only error shape the API returns."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from llm_gateway.logging_setup import request_id_var

PROBLEM_CONTENT_TYPE = "application/problem+json"
# A relative URI reference (RFC 9457 section 3.1.1); the stable `code` carries the meaning.
_PROBLEM_TYPE_BASE = "/problems/"
logger = logging.getLogger(__name__)


class GatewayError(Exception):
    """A domain failure with a stable machine-readable code."""

    def __init__(
        self,
        code: str,
        *,
        status: int,
        title: str,
        detail: str | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.title = title
        self.detail = detail
        self.retry_after_seconds = retry_after_seconds


def problem_response(
    *,
    code: str,
    status: int,
    title: str,
    detail: str | None = None,
    errors: list[dict[str, str]] | None = None,
    retry_after_seconds: int | None = None,
) -> JSONResponse:
    body: dict[str, object] = {
        "type": _PROBLEM_TYPE_BASE + code.replace("_", "-"),
        "title": title,
        "status": status,
        "code": code,
        "request_id": request_id_var.get(),
    }
    if detail:
        body["detail"] = detail
    if errors:
        body["errors"] = errors
    headers = {}
    if retry_after_seconds is not None and status in {429, 503}:
        headers["Retry-After"] = str(retry_after_seconds)
    return JSONResponse(body, status_code=status, media_type=PROBLEM_CONTENT_TYPE, headers=headers)


def unhandled_error_response(exc: Exception) -> JSONResponse:
    logger.error("Unhandled error: %s", type(exc).__name__, exc_info=exc)
    return problem_response(code="internal_error", status=500, title="Internal error")


def install_problem_handlers(app: FastAPI) -> None:
    @app.exception_handler(GatewayError)
    async def gateway_error(_request: Request, exc: GatewayError) -> JSONResponse:
        return problem_response(
            code=exc.code,
            status=exc.status,
            title=exc.title,
            detail=exc.detail,
            retry_after_seconds=exc.retry_after_seconds,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        codes = {404: "not_found", 405: "method_not_allowed"}
        code = codes.get(exc.status_code, "http_error")
        return problem_response(
            code=code, status=exc.status_code, title=code.replace("_", " ").capitalize()
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # Field paths and error types only; the rejected input values are never echoed.
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
