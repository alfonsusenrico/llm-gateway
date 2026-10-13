from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx2
import pytest
from psycopg_pool import AsyncConnectionPool

from llm_gateway.api.app import create_app
from llm_gateway.api.auth import Authenticator, FailureThrottle, ProjectDirectory
from llm_gateway.notifier.events import wait_for_background_alerts
from llm_gateway.projects import service
from llm_gateway.projects.contract import parse_contract
from llm_gateway.projects.keys import IssuedKey, decode_pepper
from tests.conftest import TEST_PEPPER, make_settings
from tests.test_contract import contract_data

pytestmark = [pytest.mark.anyio, pytest.mark.db]
PEPPER = decode_pepper(TEST_PEPPER)
SELF = "/v1/projects/self"


class Gateway:
    """The app wired to a migrated test schema, without running its lifespan."""

    def __init__(self, pool: AsyncConnectionPool, **settings: Any) -> None:
        self.pool = pool
        self.settings = make_settings(**settings)
        self.app = create_app(self.settings)
        self.app.state.db_pool = pool
        self.authenticator = Authenticator(
            self.settings,
            pool,
            directory=ProjectDirectory(pool, dedupe_window_seconds=900, ttl_seconds=0),
            throttle=FailureThrottle(self.settings.auth_failure_limit_per_minute),
        )
        self.app.state.authenticator = self.authenticator
        self.client = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=self.app), base_url="http://gateway"
        )

    async def get(self, key: str | None = None, **headers: str) -> httpx2.Response:
        if key is not None:
            headers["Authorization"] = f"Bearer {key}"
        return await self.client.get(SELF, headers=headers)

    async def register(self, project_id: str = "personal_agent") -> dict[str, IssuedKey]:
        contract = parse_contract(contract_data(project_id=project_id))
        keys = await service.register_project(self.pool, contract, actor="t", pepper=PEPPER)
        return {key.environment: key for key in keys}

    async def alerts(self) -> list[tuple[Any, ...]]:
        await wait_for_background_alerts()
        async with self.pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT event_type, occurrences, summary FROM notifier_events ORDER BY event_id"
            )
            return await cursor.fetchall()


@pytest.fixture
async def gateway(migrated_pool: AsyncConnectionPool) -> AsyncIterator[Gateway]:
    gateway = Gateway(migrated_pool)
    async with gateway.client:
        yield gateway


def _without_request_id(response: httpx2.Response) -> dict[str, Any]:
    body = response.json()
    body.pop("request_id")
    return body


async def test_valid_key_identifies_project_and_environment(gateway: Gateway) -> None:
    keys = await gateway.register()

    production = await gateway.get(keys["production"].raw)
    development = await gateway.get(keys["development"].raw)

    assert production.status_code == 200
    assert production.json() == {
        "project_id": "personal_agent",
        "display_name": "Personal Agent",
        "environment": "production",
        "contract_version": 1,
    }
    assert development.json()["environment"] == "development"


async def test_missing_header_is_key_missing_with_a_challenge(gateway: Gateway) -> None:
    response = await gateway.get()

    assert response.status_code == 401
    assert response.json()["code"] == "key_missing"
    assert response.headers["WWW-Authenticate"].startswith("Bearer")


@pytest.mark.parametrize("raw", ["sk-abc123", "lgw.prod.short.secret"])
async def test_malformed_key_is_refused_before_lookup(gateway: Gateway, raw: str) -> None:
    response = await gateway.get(raw)

    assert (response.status_code, response.json()["code"]) == (401, "key_malformed")


async def test_unknown_and_wrong_secret_keys_get_identical_responses(gateway: Gateway) -> None:
    keys = await gateway.register()
    known_id = keys["production"].key_id

    wrong_secret = await gateway.get(f"lgw.prod.{known_id}.{'A' * 43}")
    unknown = await gateway.get(f"lgw.prod.aaaaaaaaaaaa.{'A' * 43}")

    assert wrong_secret.status_code == unknown.status_code == 401
    assert _without_request_id(wrong_secret) == _without_request_id(unknown)
    assert wrong_secret.headers["WWW-Authenticate"] == unknown.headers["WWW-Authenticate"]


async def test_key_with_the_wrong_environment_prefix_is_invalid(gateway: Gateway) -> None:
    keys = await gateway.register()
    development = keys["development"].raw

    response = await gateway.get(development.replace("lgw.dev.", "lgw.prod.", 1))

    assert response.json()["code"] == "key_invalid"


async def test_revoked_key_is_refused_and_alerts_without_key_material(gateway: Gateway) -> None:
    keys = await gateway.register()
    await service.revoke_key(gateway.pool, keys["production"].key_id)

    response = await gateway.get(keys["production"].raw)

    assert response.json()["code"] == "key_invalid"
    alerts = await gateway.alerts()
    assert [alert[0] for alert in alerts] == ["project_key_rejected"]
    assert "personal_agent (production)" in alerts[0][2]
    assert keys["production"].raw.rsplit(".", 1)[1] not in alerts[0][2]


async def test_rotation_with_grace_keeps_both_keys_working(gateway: Gateway) -> None:
    keys = await gateway.register()

    new_key = await service.rotate_key(
        gateway.pool, "personal_agent", "production", grace_minutes=30, pepper=PEPPER
    )

    assert (await gateway.get(keys["production"].raw)).status_code == 200
    assert (await gateway.get(new_key.raw)).status_code == 200


async def test_rotation_without_grace_stops_the_old_key(gateway: Gateway) -> None:
    keys = await gateway.register()

    new_key = await service.rotate_key(
        gateway.pool, "personal_agent", "production", grace_minutes=0, pepper=PEPPER
    )

    assert (await gateway.get(keys["production"].raw)).json()["code"] == "key_invalid"
    assert (await gateway.get(new_key.raw)).status_code == 200


async def test_disabled_project_is_refused(gateway: Gateway) -> None:
    keys = await gateway.register()

    def disable(contract: dict[str, Any]) -> dict[str, Any]:
        contract["status"] = "disabled"
        return contract

    await service.update_contract(gateway.pool, "personal_agent", disable, actor="t", note="d")

    response = await gateway.get(keys["production"].raw)
    assert (response.status_code, response.json()["code"]) == (403, "project_disabled")


async def test_corrupted_contract_quarantines_only_its_project(gateway: Gateway) -> None:
    broken = await gateway.register("broken_project")
    healthy = await gateway.register("healthy_project")
    async with gateway.pool.connection() as connection:
        await connection.execute(
            "UPDATE contract_versions SET contract = contract || '{\"surprise\": 1}' "
            "WHERE project_id = 'broken_project'"
        )

    refused = await gateway.get(broken["production"].raw)
    served = await gateway.get(healthy["production"].raw)

    assert (refused.status_code, refused.json()["code"]) == (503, "project_quarantined")
    assert served.status_code == 200
    assert [alert[0] for alert in await gateway.alerts()] == ["project_quarantined"]


async def test_repeated_failures_throttle_the_source_even_with_a_valid_key(
    gateway: Gateway,
) -> None:
    keys = await gateway.register()
    for _ in range(11):
        await gateway.get(f"lgw.prod.aaaaaaaaaaaa.{'A' * 43}")

    response = await gateway.get(keys["production"].raw)

    assert (response.status_code, response.json()["code"]) == (429, "auth_throttled")
    assert response.headers["Retry-After"] == "60"
    assert [alert[:2] for alert in await gateway.alerts()] == [("auth_failures_throttled", 1)]


async def test_trusted_header_counts_clients_separately(migrated_pool: AsyncConnectionPool) -> None:
    gateway = Gateway(migrated_pool, trusted_client_ip_header="CF-Connecting-IP")
    async with gateway.client:
        keys = await gateway.register()
        bad = f"lgw.prod.aaaaaaaaaaaa.{'A' * 43}"
        for _ in range(11):
            await gateway.get(bad, **{"CF-Connecting-IP": "203.0.113.7"})

        other_client = await gateway.get(
            keys["production"].raw, **{"CF-Connecting-IP": "203.0.113.8"}
        )
        throttled = await gateway.get(keys["production"].raw, **{"CF-Connecting-IP": "203.0.113.7"})

    assert other_client.status_code == 200
    assert throttled.status_code == 429


async def test_header_is_ignored_unless_trusted(gateway: Gateway) -> None:
    keys = await gateway.register()
    for index in range(11):
        await gateway.get(
            f"lgw.prod.aaaaaaaaaaaa.{'A' * 43}", **{"CF-Connecting-IP": f"203.0.113.{index}"}
        )

    response = await gateway.get(keys["production"].raw)

    assert response.status_code == 429


def test_failure_throttle_window_and_crossing() -> None:
    now = [0.0]
    throttle = FailureThrottle(2, window_seconds=60, clock=lambda: now[0])

    crossings = [throttle.record_failure("a") for _ in range(4)]
    blocked_now = throttle.blocked("a")
    now[0] = 61.0

    assert crossings == [False, False, True, False]
    assert blocked_now is True
    assert throttle.blocked("a") is False
    assert throttle.blocked("b") is False


async def test_catalogue_hides_development_models_from_production_keys(gateway: Gateway) -> None:
    keys = await gateway.register()

    production = await gateway.client.get(
        "/v1/catalogue", headers={"Authorization": f"Bearer {keys['production'].raw}"}
    )
    development = await gateway.client.get(
        "/v1/catalogue", headers={"Authorization": f"Bearer {keys['development'].raw}"}
    )

    assert [m["model"] for m in production.json()["models"]] == ["gpt-6-luna"]
    assert [m["model"] for m in development.json()["models"]] == ["gpt-6-luna", "stub-echo"]
    luna = production.json()["models"][0]
    assert luna["prices"]["standard"]["input"] == "0.10"
    assert luna["prices"]["flex"]["output"] == "0.25"
    stub = development.json()["models"][1]
    assert stub["prices"]["standard"]["output"] == "0"
    assert production.headers["Cache-Control"] == "private, max-age=300"
