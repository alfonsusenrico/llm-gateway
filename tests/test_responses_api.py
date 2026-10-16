from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import anyio
import pytest
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

from llm_gateway.allocation.store import recompute
from llm_gateway.catalogue.model import get_catalogue
from llm_gateway.catalogue.pricing import TokenUsage
from llm_gateway.llm.providers import (
    ProviderFailure,
    ProviderSuccess,
    StreamStarted,
    StreamText,
)
from llm_gateway.llm.results import ResultStore, delete_expired_results
from llm_gateway.llm.service import CallDeps
from llm_gateway.llm.stub_adapter import StubAdapter
from llm_gateway.notifier.events import wait_for_background_alerts
from llm_gateway.projects.contract import ContractV2
from llm_gateway.projects.credentials import CredentialStore
from tests.conftest import TEST_RESULT_KEY
from tests.test_auth_api import Gateway

pytestmark = [pytest.mark.anyio, pytest.mark.db]
SECRET_PROMPT = "a-private-prompt-text"


class FakeOpenAI:
    """Answers like the OpenAI adapter would, from a script, optionally after a delay."""

    def __init__(self, result: ProviderSuccess | ProviderFailure, delay: float = 0.0) -> None:
        self.result = result
        self.delay = delay
        self.calls = 0

    async def execute(self, resolved: Any, request: Any) -> ProviderSuccess | ProviderFailure:
        self.calls += 1
        if self.delay:
            await anyio.sleep(self.delay)
        return self.result

    async def execute_stream(self, resolved: Any, request: Any) -> AsyncGenerator[Any]:
        self.calls += 1
        if isinstance(self.result, ProviderFailure):
            yield self.result
            return
        yield StreamStarted("req_stream")
        for word in self.result.output_text.split(" "):
            if self.delay:
                await anyio.sleep(self.delay)
            yield StreamText(word + " ")
        yield self.result


def _luna_success() -> ProviderSuccess:
    return ProviderSuccess(
        status="completed",
        output_text="Hello there.",
        refusal=None,
        incomplete_reason=None,
        usage=TokenUsage(2000, 500, cached_input_tokens=1024),
        reasoning_tokens=120,
        service_tier="default",
        provider_response_id="resp_1",
        provider_request_id="req_1",
    )


class Calls:
    def __init__(self, gateway: Gateway) -> None:
        self.gateway = gateway
        self.keys: dict[str, Any] = {}
        self.openai = FakeOpenAI(_luna_success())

    async def setup(self, **settings: Any) -> None:
        self.keys = await self.gateway.register("alpha")
        await recompute(self.gateway.pool, self.gateway.settings)
        adapters = {
            "stub": lambda _credential: StubAdapter(),
            "openai-responses": lambda _credential: self.openai,
        }
        await CredentialStore(
            self.gateway.pool, self.gateway.settings.provider_credential_encryption_key
        ).set("alpha", "openai", SecretStr("synthetic-provider-key"), actor="test")
        deps = CallDeps(
            pool=self.gateway.pool,
            settings=self.gateway.settings.model_copy(update=settings),
            catalogue=get_catalogue(),
            results=ResultStore(self.gateway.pool, TEST_RESULT_KEY, 86_400),
            adapters=adapters,
            actor="api:test",
        )
        self.gateway.app.state.call_deps = deps

    async def post(self, env: str = "development", **body: Any):
        payload: dict[str, Any] = {
            "provider_call_id": "call-1",
            "operation": "summarise",
            "model": "stub-echo",
            "input": SECRET_PROMPT,
        }
        payload.update(body)
        payload = {k: v for k, v in payload.items() if v is not None}
        return await self.gateway.client.post(
            "/v1/responses", json=payload, headers={"Authorization": f"Bearer {self.keys[env].raw}"}
        )

    async def ledger(self) -> list[tuple[Any, ...]]:
        async with self.gateway.pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT provider_call_id, state, failure_code, cost_micro_usd, charged_micro_usd "
                "FROM calls ORDER BY call_key"
            )
            return await cursor.fetchall()


@asynccontextmanager
async def calls_context(pool: AsyncConnectionPool) -> AsyncIterator[Calls]:
    """A gateway on the migrated test schema, with project alpha registered and granted."""
    gateway = Gateway(pool)
    async with gateway.client:
        calls = Calls(gateway)
        await calls.setup()
        yield calls


@pytest.fixture
async def calls(migrated_pool: AsyncConnectionPool) -> AsyncIterator[Calls]:
    async with calls_context(migrated_pool) as context:
        yield context


async def test_stub_call_returns_answer_usage_cost_and_allowance(calls: Calls) -> None:
    response = await calls.post()

    body = response.json()
    assert response.status_code == 200
    assert body["output_text"] == f"stub: {SECRET_PROMPT}"
    assert (body["status"], body["model"], body["replayed"], body["cost_usd"]) == (
        "completed",
        "stub-echo",
        False,
        "0.00",
    )
    assert body["usage"]["input_tokens"] > 0
    assert response.headers["X-RateLimit-Limit"] == "10"
    assert response.headers["X-RateLimit-Remaining"] == "9"
    assert response.headers["X-Concurrency-Limit"] == "2"
    assert "X-Budget-Remaining-USD" in response.headers
    assert await calls.ledger() == [("call-1", "completed", None, 0, 0)]


async def test_json_schema_reply_is_parsed(calls: Calls) -> None:
    schema = {"type": "json_schema", "name": "out", "schema": {"type": "object"}, "strict": True}

    ok = await calls.post(text_format=schema, stub={"reply": {"answer": 42}})
    broken = await calls.post(
        provider_call_id="call-2", text_format=schema, stub={"scenario": "invalid_json"}
    )

    assert ok.json()["output_json"] == {"answer": 42}
    assert ok.json()["structured_output_error"] is None
    assert broken.json()["structured_output_error"] == "invalid_json"


@pytest.mark.parametrize(
    ("scenario", "status", "code", "state", "retry_after"),
    [
        ("rate_limited", 429, "provider_rate_limited", "retryable_failure", "1"),
        ("overloaded", 503, "provider_unavailable", "retryable_failure", "1"),
        ("connection_failed", 503, "provider_unavailable", "retryable_failure", "1"),
        ("timeout_after_send", 502, "execution_unknown", "execution_unknown", None),
        ("invalid_request", 422, "provider_invalid_request", "failed", None),
    ],
)
async def test_stub_scenarios_map_to_their_outcomes(
    calls: Calls, scenario: str, status: int, code: str, state: str, retry_after: str | None
) -> None:
    response = await calls.post(stub={"scenario": scenario})

    assert (response.status_code, response.json()["code"]) == (status, code)
    assert response.headers.get("Retry-After") == retry_after
    assert (await calls.ledger())[0][1:3] == (state, code)


async def test_incomplete_scenario_completes_with_its_reason(calls: Calls) -> None:
    body = (await calls.post(stub={"scenario": "incomplete"})).json()

    assert (body["status"], body["incomplete_reason"]) == ("incomplete", "max_output_tokens")


async def test_resubmitted_call_is_replayed_without_a_second_charge(calls: Calls) -> None:
    calls.openai.result = _luna_success()
    first = await calls.post("production", model="openai-gpt-6-luna", max_output_tokens=600)
    again = await calls.post("production", model="openai-gpt-6-luna", max_output_tokens=600)

    assert first.json()["cost_usd"] == "0.000358"
    assert again.status_code == 200
    assert again.json()["replayed"] is True
    assert again.json()["output_text"] == first.json()["output_text"]
    assert "X-RateLimit-Limit" not in again.headers
    assert calls.openai.calls == 1
    assert await calls.ledger() == [("call-1", "completed", None, 358, 358)]


async def test_retryable_failure_is_executed_again_on_resubmission(calls: Calls) -> None:
    calls.openai.result = ProviderFailure("temporary_overload", "server_is_overloaded", 3)
    first = await calls.post("production", model="openai-gpt-6-luna")
    calls.openai.result = _luna_success()
    second = await calls.post("production", model="openai-gpt-6-luna")

    assert (first.status_code, first.headers["Retry-After"]) == (503, "3")
    assert second.status_code == 200
    assert calls.openai.calls == 2


async def test_unknown_execution_is_never_run_again(calls: Calls) -> None:
    calls.openai.result = ProviderFailure("execution_unknown", "ReadTimeout")
    first = await calls.post("production", model="openai-gpt-6-luna")
    again = await calls.post("production", model="openai-gpt-6-luna")

    assert first.json()["code"] == again.json()["code"] == "execution_unknown"
    assert calls.openai.calls == 1


async def test_different_request_with_the_same_id_conflicts(calls: Calls) -> None:
    await calls.post()

    response = await calls.post(input="something else")

    assert (response.status_code, response.json()["code"]) == (409, "idempotency_conflict")


async def test_expired_result_is_gone(calls: Calls) -> None:
    await calls.post()
    async with calls.gateway.pool.connection() as connection:
        await connection.execute("UPDATE call_results SET expires_at = now() - interval '1 second'")

    response = await calls.post()
    deleted = await delete_expired_results(calls.gateway.pool)

    assert (response.status_code, response.json()["code"]) == (410, "call_result_expired")
    assert deleted == 1


async def test_unreadable_result_counts_as_expired(calls: Calls) -> None:
    await calls.post()
    calls.gateway.app.state.call_deps = dataclasses.replace(
        calls.gateway.app.state.call_deps,
        results=ResultStore(
            calls.gateway.pool, "b3RoZXIta2V5LW90aGVyLWtleS1vdGhlci1rZXktMDA=", 86_400
        ),
    )

    response = await calls.post()

    assert response.json()["code"] == "call_result_expired"


async def test_stored_results_are_encrypted(calls: Calls) -> None:
    await calls.post()

    async with calls.gateway.pool.connection() as connection:
        cursor = await connection.execute("SELECT encrypted FROM call_results")
        row = await cursor.fetchone()

    assert row is not None and SECRET_PROMPT.encode() not in bytes(row[0])


async def test_production_key_cannot_use_the_stub(calls: Calls) -> None:
    response = await calls.post("production")

    assert (response.status_code, response.json()["code"]) == (403, "stub_not_allowed")
    assert await calls.ledger() == []


async def test_missing_provider_is_not_configured(calls: Calls) -> None:
    del calls.gateway.app.state.call_deps.adapters["openai-responses"]

    response = await calls.post("production", model="openai-gpt-6-luna")

    assert (response.status_code, response.json()["code"]) == (503, "provider_not_configured")


async def test_oversized_and_invalid_bodies_are_refused(calls: Calls) -> None:
    small = calls.gateway.settings.model_copy(update={"request_max_bytes": 10_000})
    calls.gateway.app.state.call_deps = dataclasses.replace(
        calls.gateway.app.state.call_deps, settings=small
    )

    too_large = await calls.post(input="x" * 20_000)
    invalid = await calls.post(operation="Bad Operation")

    assert (too_large.status_code, too_large.json()["code"]) == (413, "request_too_large")
    assert (invalid.status_code, invalid.json()["code"]) == (422, "request_invalid")
    assert "Bad Operation" not in invalid.text


async def test_admission_refusals_reach_the_client(calls: Calls) -> None:
    await calls.post(provider_call_id="a")
    await calls.post(provider_call_id="b", stub={"scenario": "overloaded"})
    # Development grant: 10 requests per minute; two used.
    for index in range(8):
        await calls.post(provider_call_id=f"c{index}")

    response = await calls.post(provider_call_id="over")

    assert (response.status_code, response.json()["code"]) == (429, "rate_limited")
    assert int(response.headers["Retry-After"]) >= 1


async def test_account_problems_alert_the_owner(calls: Calls) -> None:
    calls.openai.result = ProviderFailure("account_blocked", "credit_balance_exhausted")

    response = await calls.post("production", model="openai-gpt-6-luna")
    await wait_for_background_alerts()

    assert (response.status_code, response.json()["code"]) == (503, "provider_account_blocked")
    async with calls.gateway.pool.connection() as connection:
        cursor = await connection.execute("SELECT event_type, severity FROM notifier_events")
        assert ("provider_account_blocked", "critical") in await cursor.fetchall()


async def test_a_disconnected_client_still_settles_the_call(calls: Calls) -> None:
    from llm_gateway.llm.request import ResponseRequest
    from llm_gateway.llm.service import run_call

    slow = FakeOpenAI(_luna_success(), delay=0.3)
    only_slow = {"openai-responses": lambda _: slow}
    deps = dataclasses.replace(calls.gateway.app.state.call_deps, adapters=only_slow)
    request = ResponseRequest.model_validate(
        {
            "provider_call_id": "dropped",
            "operation": "summarise",
            "model": "openai-gpt-6-luna",
            "input": "Hello",
        }
    )
    identity = await calls.gateway.authenticator.directory.load("alpha")
    assert identity is not None and isinstance(identity.contract, ContractV2)
    task = asyncio.get_running_loop().create_task(
        run_call(
            deps,
            project_id="alpha",
            environment="production",
            contract=identity.contract,
            request=request,
            request_bytes=200,
        )
    )
    await anyio.sleep(0.1)
    task.cancel()  # the client went away while the provider was working
    with pytest.raises(asyncio.CancelledError):
        await task
    await anyio.sleep(0.5)

    assert ("dropped", "completed") in [row[:2] for row in await calls.ledger()]


async def test_call_logs_hold_no_content(calls: Calls, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        await calls.post()
        await calls.post(provider_call_id="call-2", stub={"scenario": "invalid_request"})

    assert "outcome=completed" in caplog.text
    assert "outcome=provider_invalid_request" in caplog.text
    assert SECRET_PROMPT not in caplog.text
