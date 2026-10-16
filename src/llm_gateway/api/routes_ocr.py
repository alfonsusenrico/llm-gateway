"""Independent typed OCR inference endpoint."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from llm_gateway.api.auth import ProjectIdentity, require_project
from llm_gateway.api.problems import GatewayError, document_problems
from llm_gateway.api.routes_responses import _ADMISSION_TITLES, _RESPONSES, allowance_headers
from llm_gateway.calls.records import AdmissionRefusedError
from llm_gateway.llm.errors import CallRejectedError
from llm_gateway.llm.service import CallDeps
from llm_gateway.ocr.models import OcrRequest, OcrResult
from llm_gateway.ocr.service import run_ocr

router = APIRouter(prefix="/v1", tags=["ocr"])
_DOCUMENTED = {
    **_RESPONSES,
    200: {"description": "Text and quadrilateral boxes in original image pixels."},
}


@router.post("/ocr", responses=document_problems(_DOCUMENTED), response_model=OcrResult)
async def create_ocr(
    request: Request,
    call: OcrRequest,
    identity: Annotated[ProjectIdentity, Depends(require_project)],
) -> JSONResponse:
    deps: CallDeps = request.app.state.call_deps
    raw = await request.body()
    if len(raw) > deps.settings.request_max_bytes:
        raise GatewayError("request_too_large", status=413, title="Request is too large")
    try:
        result = await run_ocr(
            deps,
            project_id=identity.project_id,
            environment=identity.environment,
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
    headers["Cache-Control"] = "private, no-store"
    return JSONResponse(result.body, headers=headers)
