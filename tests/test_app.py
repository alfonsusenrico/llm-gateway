from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from llm_gateway.api.app import create_app
from llm_gateway.api.problems import PROBLEM_CONTENT_TYPE, GatewayError
from tests.conftest import make_settings


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app(make_settings())

    @app.get("/test/domain-error")
    async def domain_error() -> None:
        raise GatewayError(
            "budget_exhausted", status=429, title="Budget exhausted", retry_after_seconds=60
        )

    @app.get("/test/crash")
    async def crash() -> None:
        raise RuntimeError("provider said: secret detail")

    @app.get("/test/items/{item_id}")
    async def item(item_id: int) -> dict[str, int]:
        return {"item_id": item_id}

    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def test_liveness_reports_the_revision(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "revision": "test-revision"}


def test_every_response_carries_a_request_id(client: TestClient) -> None:
    response = client.get("/health")

    assert len(response.headers["X-Request-ID"]) == 32


def test_a_safe_caller_request_id_is_kept(client: TestClient) -> None:
    response = client.get("/health", headers={"X-Request-ID": "caller-req-0001"})

    assert response.headers["X-Request-ID"] == "caller-req-0001"


def test_an_unsafe_caller_request_id_is_replaced(client: TestClient) -> None:
    response = client.get("/health", headers={"X-Request-ID": "bad id\twith spaces"})

    assert response.headers["X-Request-ID"] != "bad id\twith spaces"


def test_readiness_fails_with_retry_after_when_the_database_is_down(client: TestClient) -> None:
    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.headers["content-type"] == PROBLEM_CONTENT_TYPE
    assert response.headers["Retry-After"] == "5"
    assert response.json()["code"] == "dependency_unavailable"


def test_domain_error_is_a_problem_with_its_code(client: TestClient) -> None:
    response = client.get("/test/domain-error")

    body = response.json()
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"
    assert body["code"] == "budget_exhausted"
    assert body["type"] == "/problems/budget-exhausted"
    assert body["request_id"] == response.headers["X-Request-ID"]


def test_unknown_route_is_a_problem(client: TestClient) -> None:
    response = client.get("/missing")

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_validation_error_lists_fields_without_values(client: TestClient) -> None:
    response = client.get("/test/items/not-a-number-secret")

    body = response.json()
    assert response.status_code == 422
    assert body["code"] == "request_invalid"
    assert body["errors"][0]["field"] == "path.item_id"
    assert "not-a-number-secret" not in response.text


def test_unhandled_error_hides_its_message_and_keeps_the_request_id(client: TestClient) -> None:
    response = client.get("/test/crash", headers={"X-Request-ID": "crash-request-01"})

    body = response.json()
    assert response.status_code == 500
    assert body["code"] == "internal_error"
    assert body["request_id"] == "crash-request-01"
    assert "secret detail" not in response.text


@pytest.mark.db
def test_readiness_succeeds_with_a_database(test_database_url: str) -> None:
    app = create_app(make_settings(test_database_url))

    with TestClient(app) as test_client:
        response = test_client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "revision": "test-revision"}
