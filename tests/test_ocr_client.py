from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest
from llm_gateway_client import GatewayClient, SyncGatewayClient
from llm_gateway_client.errors import ExecutionUnknownError

from llm_gateway.ocr.models import OcrResult

pytestmark = pytest.mark.anyio


async def no_wait(_seconds: float) -> None:
    pass


@pytest.mark.parametrize("sync", [False, True])
@pytest.mark.parametrize("unknown", [False, True])
async def test_ocr_clients_share_safe_retry_and_result_contract(sync: bool, unknown: bool) -> None:
    requests = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        assert request.url.path == "/v1/ocr"
        if unknown:
            return httpx2.Response(502, json={"code": "execution_unknown"})
        if len(requests) == 1:
            raise httpx2.ReadError("synthetic dropped response")
        return httpx2.Response(
            200,
            json={
                "provider_call_id": "client-1",
                "status": "completed",
                "provider": "rapidocr",
                "model": "rapidocr-ppocrv4-v1",
                "model_alias": "local-rapidocr-ppocrv4-v1",
                "definition_digest": "a" * 64,
                "media_type": "image/png",
                "width": 100,
                "height": 40,
                "coordinate_system": "original_image_pixels",
                "exif_rotation": False,
                "text": "",
                "blocks": [],
                "cost_usd": "0.00",
                "replayed": True,
            },
        )

    options: dict[str, Any] = dict(
        provider_call_id="client-1",
        operation="demo",
        media_type="image/png",
        data_base64="AAAA",
        model="local-rapidocr-ppocrv4-v1",
    )
    if sync:
        with SyncGatewayClient(
            "http://gateway",
            "synthetic-project",
            http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
            sleep=lambda _: None,
        ) as client:
            if unknown:
                with pytest.raises(ExecutionUnknownError):
                    client.ocr.create(**options)
            else:
                result = client.ocr.create(**options)
                assert OcrResult.model_validate(result.model_dump()).replayed
    else:
        async with GatewayClient(
            "http://gateway",
            "synthetic-project",
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
            sleep=no_wait,
        ) as client:
            if unknown:
                with pytest.raises(ExecutionUnknownError):
                    await client.ocr.create(**options)
            else:
                result = await client.ocr.create(**options)
                assert OcrResult.model_validate(result.model_dump()).replayed
    assert len(requests) == (1 if unknown else 2)
    assert all(json.loads(r.content)["provider_call_id"] == "client-1" for r in requests)
    assert all("input" not in json.loads(r.content) for r in requests)


def test_ocr_image_helper_reads_bytes_and_local_path(tmp_path) -> None:
    import base64

    from llm_gateway_client import ocr_image

    raw = b"synthetic image bytes"
    path = tmp_path / "example.png"
    path.write_bytes(raw)
    assert ocr_image(raw, "image/png") == ocr_image(path, "image/png")
    assert ocr_image(raw, "image/png") == {
        "media_type": "image/png",
        "data_base64": base64.b64encode(raw).decode(),
    }
