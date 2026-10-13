from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql

from llm_gateway.settings import Settings

# Nothing listens on port 1, so a connection fails immediately.
UNREACHABLE_DATABASE_URL = "postgresql://gateway:unused@127.0.0.1:1/gateway"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("TEST_DATABASE_URL"):
        return
    if os.environ.get("REQUIRE_DB") == "1":
        raise pytest.UsageError("REQUIRE_DB=1 but TEST_DATABASE_URL is not set")
    skip_db = pytest.mark.skip(reason="TEST_DATABASE_URL not set")
    for item in items:
        if "db" in item.keywords:
            item.add_marker(skip_db)


def make_settings(database_url: str = UNREACHABLE_DATABASE_URL, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "development",
        "app_revision": "test-revision",
        "database_url": database_url,
    }
    values.update(overrides)
    return Settings.model_validate(values)


@pytest.fixture
def test_database_url() -> str:
    return os.environ["TEST_DATABASE_URL"]


@pytest.fixture
def isolated_schema(test_database_url: str) -> Iterator[str]:
    """A throwaway schema, so each test starts from an empty database."""
    schema = f"test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(test_database_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        yield schema
    finally:
        with psycopg.connect(test_database_url, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
