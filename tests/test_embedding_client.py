from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest
from llm_gateway_client import GatewayClient, SyncGatewayClient
from llm_gateway_client.errors import ExecutionUnknownError

from llm_gateway.embeddings.models import EmbeddingResult

pytestmark = pytest.mark.anyio


async def no_wait(_seconds: float) -> None:
    pass


@pytest.mark.parametrize("sync", [False, True])
@pytest.mark.parametrize("unknown", [False, True])
async def test_embedding_clients_share_safe_retry_and_result_contract(
    sync: bool, unknown: bool
) -> None:
    requests = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        assert request.url.path == "/v1/embeddings"
        if unknown:
            return httpx2.Response(502, json={"code": "execution_unknown"})
        if len(requests) == 1:
            raise httpx2.ReadError("synthetic dropped response")
        return httpx2.Response(
            200,
            json={
                "provider_call_id": "client-1",
                "status": "completed",
                "provider": "openai",
                "model": "text-embedding-3-small",
                "model_alias": "openai-text-embedding-3-small",
                "definition_digest": "a" * 64,
                "dimensions": 1,
                "normalized": True,
                "input_type": None,
                "data": [{"index": 0, "embedding": [1.0]}],
                "usage": {"input_tokens": 1, "total_tokens": 1},
                "cost_usd": "0.000001",
                "replayed": True,
            },
        )

    options: dict[str, Any] = dict(
        provider_call_id="client-1",
        operation="demo",
        input="Synthetic",
        model="openai-text-embedding-3-small",
        dimensions=1,
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
                    client.embeddings.create(**options)
            else:
                result = client.embeddings.create(**options)
                assert EmbeddingResult.model_validate(result.model_dump()).replayed
    else:
        async with GatewayClient(
            "http://gateway",
            "synthetic-project",
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
            sleep=no_wait,
        ) as client:
            if unknown:
                with pytest.raises(ExecutionUnknownError):
                    await client.embeddings.create(**options)
            else:
                result = await client.embeddings.create(**options)
                assert EmbeddingResult.model_validate(result.model_dump()).replayed
    assert len(requests) == (1 if unknown else 2)
    assert all(json.loads(r.content)["provider_call_id"] == "client-1" for r in requests)
    assert all(json.loads(r.content)["encoding_format"] == "float" for r in requests)
