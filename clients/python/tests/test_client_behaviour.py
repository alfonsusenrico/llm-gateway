from __future__ import annotations

import json
from typing import Any

import anyio
import httpx2
import pytest
from llm_gateway_client import (
    AuthenticationError,
    BudgetExhaustedError,
    ExecutionUnknownError,
    GatewayClient,
    GatewayError,
    GatewayUnavailableError,
    InvalidRequestError,
    RetryableError,
    RetryPolicy,
    SyncGatewayClient,
    __version__,
)
from llm_gateway_client.errors import error_from_problem

pytestmark = pytest.mark.anyio
OK_BODY = {
    "provider_call_id": "call-1",
    "status": "completed",
    "provider": "stub",
    "model": "stub-echo",
    "model_alias": "stub-echo",
    "definition_digest": "a" * 64,
    "output_text": "hi",
    "usage": {"input_tokens": 3, "output_tokens": 1},
    "cost_usd": "0.00",
    "replayed": False,
}


def problem(status: int, code: str, retry_after: str | None = None) -> httpx2.Response:
    headers = {"Retry-After": retry_after} if retry_after else {}
    return httpx2.Response(status, json={"code": code, "title": code}, headers=headers)


def ok(concurrency_limit: int = 2) -> httpx2.Response:
    return httpx2.Response(
        200, json=OK_BODY, headers={"X-Concurrency-Limit": str(concurrency_limit)}
    )


class Script:
    """Answers each request with the next scripted response or exception."""

    def __init__(self, *answers: httpx2.Response | Exception) -> None:
        self.answers = list(answers)
        self.bodies: list[dict[str, Any]] = []
        self.headers: list[httpx2.Headers] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.bodies.append(json.loads(request.content))
        self.headers.append(request.headers)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def client(script: Script, sleeps: list[float], **policy: Any) -> GatewayClient:
    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    return GatewayClient(
        "http://gateway",
        "lgw.dev.key",
        retry_policy=RetryPolicy(**policy),
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(script)),
        sleep=record_sleep,
    )


async def call(gateway: GatewayClient):
    return await gateway.responses.create(
        provider_call_id="call-1", operation="demo", input="hello", profile="chat"
    )


@pytest.mark.parametrize(
    ("code", "kind"),
    [
        ("provider_unavailable", RetryableError),
        ("concurrency_exhausted", RetryableError),
        ("execution_unknown", ExecutionUnknownError),
        ("budget_exhausted", BudgetExhaustedError),
        ("idempotency_conflict", InvalidRequestError),
        ("cost_ceiling_exceeded", InvalidRequestError),
        ("key_invalid", AuthenticationError),
        ("provider_account_blocked", GatewayUnavailableError),
        ("call_result_expired", GatewayUnavailableError),
    ],
)
def test_codes_map_to_typed_errors(code: str, kind: type[GatewayError]) -> None:
    error = error_from_problem(429, {"code": code, "request_id": "r1"}, 3.0)

    assert type(error) is kind
    assert (error.code, error.request_id, error.retry_after_seconds) == (code, "r1", 3.0)


def test_unknown_code_fails_closed() -> None:
    error = error_from_problem(503, {"code": "brand_new_code"}, 1.0)

    assert type(error) is GatewayError


async def test_retryable_refusal_is_resubmitted_with_the_same_call_id() -> None:
    script = Script(problem(503, "provider_unavailable", "2"), ok())
    sleeps: list[float] = []

    result = await call(client(script, sleeps))

    assert result.output_text == "hi"
    assert [body["provider_call_id"] for body in script.bodies] == ["call-1", "call-1"]
    assert len(sleeps) == 1 and 2.0 <= sleeps[0] <= 2.4  # Retry-After plus up to 20% jitter


async def test_transport_failure_is_resubmitted() -> None:
    script = Script(httpx2.ReadError("connection dropped"), ok())

    result = await call(client(script, []))

    assert result.status == "completed"
    assert len(script.bodies) == 2


async def test_gateway_5xx_without_a_problem_body_is_retried() -> None:
    script = Script(httpx2.Response(502, text="bad gateway"), ok())

    assert (await call(client(script, []))).status == "completed"


async def test_execution_unknown_is_never_retried() -> None:
    script = Script(problem(502, "execution_unknown"))
    sleeps: list[float] = []

    with pytest.raises(ExecutionUnknownError):
        await call(client(script, sleeps))

    assert len(script.bodies) == 1 and sleeps == []


async def test_budget_exhaustion_raises_with_the_reset_time() -> None:
    script = Script(problem(429, "budget_exhausted", "86400"))

    with pytest.raises(BudgetExhaustedError) as raised:
        await call(client(script, []))

    assert raised.value.retry_after_seconds == 86400


async def test_a_wait_beyond_the_retry_budget_raises_instead_of_sleeping() -> None:
    script = Script(problem(429, "rate_limited", "120"))
    sleeps: list[float] = []

    with pytest.raises(RetryableError) as raised:
        await call(client(script, sleeps, max_total_seconds=90))

    assert raised.value.retry_after_seconds == 120
    assert sleeps == []


async def test_attempts_are_bounded() -> None:
    script = Script(*[problem(503, "provider_unavailable", "1")] * 4)

    with pytest.raises(RetryableError):
        await call(client(script, [], max_attempts=4))

    assert len(script.bodies) == 4


async def test_requests_identify_the_client() -> None:
    script = Script(ok())

    await call(client(script, []))

    assert script.headers[0]["User-Agent"] == f"llm-gateway-client/{__version__}"
    assert script.headers[0]["Authorization"] == "Bearer lgw.dev.key"


async def test_concurrency_follows_the_reported_limit() -> None:
    in_flight = 0
    peak = 0

    async def handler(_request: httpx2.Request) -> httpx2.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await anyio.sleep(0.05)
        in_flight -= 1
        return ok(concurrency_limit=2)

    gateway = GatewayClient(
        "http://gateway",
        "k",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    await call(gateway)  # learns the limit
    async with anyio.create_task_group() as group:
        for _ in range(5):
            group.start_soon(call, gateway)

    assert gateway.concurrency_limit == 2
    assert peak == 2


def test_sync_client_follows_the_same_rules() -> None:
    script = Script(problem(503, "provider_unavailable", "1"), ok())
    sleeps: list[float] = []
    gateway = SyncGatewayClient(
        "http://gateway",
        "k",
        http_client=httpx2.Client(transport=httpx2.MockTransport(script)),
        sleep=sleeps.append,
    )

    result = gateway.responses.create(
        provider_call_id="call-1", operation="demo", input="hi", profile="chat"
    )

    assert result.output_text == "hi"
    assert len(script.bodies) == 2 and len(sleeps) == 1


@pytest.mark.parametrize(
    "status,code",
    [
        (503, "provider_credentials_missing"),
        (503, "provider_credentials_unreadable"),
        (503, "contract_upgrade_required"),
        (503, "model_registry_unavailable"),
        (409, "credential_changed"),
        (409, "call_configuration_changed"),
    ],
)
async def test_configuration_failures_never_retry(status: int, code: str) -> None:
    script = Script(problem(status, code), ok())
    sleeps: list[float] = []
    gateway = client(script, sleeps)
    with pytest.raises(GatewayError) as raised:
        await call(gateway)
    assert raised.value.code == code
    assert sleeps == [] and len(script.bodies) == 1


def test_sync_model_discovery_uses_typed_metadata_and_task_query() -> None:
    summary = dict(
        alias="stub-echo",
        task="llm",
        provider="stub",
        model="stub-echo",
        definition_digest="a" * 64,
        execution="stub",
        billing="zero",
        limits={},
        capabilities={},
        prices={},
        availability={"configured": True},
    )
    paths = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        paths.append(
            request.url.path + ("?" + request.url.query.decode() if request.url.query else "")
        )
        return httpx2.Response(
            200,
            json={"models": [summary]}
            if request.url.path == "/v1/models"
            else summary | {"request_schema": {"type": "object"}},
        )

    with SyncGatewayClient(
        "http://gateway",
        "synthetic-access-key",
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    ) as gateway:
        assert gateway.models.list(task="llm")[0].alias == "stub-echo"
        assert gateway.models.get("stub-echo").request_schema == {"type": "object"}
    assert paths == ["/v1/models?task=llm", "/v1/models/stub-echo"]
