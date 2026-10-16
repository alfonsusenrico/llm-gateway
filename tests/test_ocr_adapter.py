from __future__ import annotations

import httpx2
import pytest

from llm_gateway.llm.providers import ProviderFailure
from llm_gateway.ocr.adapter import OcrAdapter, OcrSuccess
from llm_gateway.ocr.models import ImageInfo, ResolvedOcr
from tests.ocr_fakes import OcrService, local_model, request

pytestmark = pytest.mark.anyio


async def run(mode: str, timeout: float = 10):
    entry, metadata = local_model()
    service = OcrService(metadata)
    service.mode = mode
    if mode == "deadline":
        service.delay = 0.1
    result = await OcrAdapter(
        "http://native", timeout, transport=httpx2.MockTransport(service)
    ).execute(ResolvedOcr(entry, ImageInfo(100, 40, 100)), request())
    return service, result


@pytest.mark.parametrize("mode", ["success", "blank"])
async def test_native_wire_and_valid_output(mode) -> None:
    service, result = await run(mode)
    assert isinstance(result, OcrSuccess)
    assert len(result.blocks) == (0 if mode == "blank" else 1)
    assert service.generations == 1
    assert [r.url.path for r in service.requests] == ["/health/ready", "/v1/models", "/v1/ocr"]
    import json

    assert set(json.loads(service.requests[-1].content)) == {"model", "media_type", "data_base64"}
    assert "authorization" not in service.requests[-1].headers


@pytest.mark.parametrize(
    "mode", ["ready_cpu", "metadata_cpu", "metadata_mismatch", "metadata_hash", "redirect"]
)
async def test_gpu_identity_checked_before_content(mode) -> None:
    service, result = await run(mode)
    assert isinstance(result, ProviderFailure) and result.category == "configuration_mismatch"
    assert service.generations == 0
    assert all(r.method == "GET" for r in service.requests)


@pytest.mark.parametrize(
    "mode,category",
    [
        ("gpu_unavailable", "temporary_overload"),
        ("service_busy", "temporary_overload"),
        ("ready_503", "temporary_overload"),
        ("image_invalid", "invalid_request"),
        ("model_not_found", "configuration_mismatch"),
        ("connect", "connection_not_established"),
        ("raw_503", "execution_unknown"),
        ("inference_failed", "execution_unknown"),
        ("timeout", "execution_unknown"),
    ],
)
async def test_confirmed_refusals_and_uncertain_execution(mode, category) -> None:
    _, result = await run(mode)
    assert isinstance(result, ProviderFailure) and result.category == category
    assert "PRIVATE_PROVIDER_TEXT" not in repr(result)


@pytest.mark.parametrize(
    "mode",
    [
        "wrong_model",
        "wrong_digest",
        "wrong_width",
        "width_bool",
        "coordinates",
        "text_mismatch",
        "confidence_bool",
        "confidence_nan",
        "confidence_range",
        "box_bool",
        "box_inf",
        "box_range",
        "box_shape",
        "text_limit",
        "block_limit",
        "malformed",
        "body_limit",
    ],
)
async def test_invalid_submitted_output_is_unknown(mode) -> None:
    service, result = await run(mode)
    assert isinstance(result, ProviderFailure) and result.category == "execution_unknown"
    assert service.generations == 1


async def test_total_deadline() -> None:
    service, result = await run("deadline", 0.02)
    assert isinstance(result, ProviderFailure) and result.category == "execution_unknown"
    assert service.generations == 1
