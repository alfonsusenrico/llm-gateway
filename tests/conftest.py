from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator

import psycopg
import pytest
from psycopg import sql
from psycopg_pool import AsyncConnectionPool

from llm_gateway.db.migrate import MIGRATIONS_DIR, apply_migrations, discover
from llm_gateway.settings import Settings

# Nothing listens on port 1, so a connection fails immediately.
UNREACHABLE_DATABASE_URL = "postgresql://gateway:unused@127.0.0.1:1/gateway"
# A fixed 32-byte pepper (base64url) so tests can compute expected hashes.
TEST_PEPPER = "dGVzdC1wZXBwZXItdGVzdC1wZXBwZXItdGVzdC1wZXBwZXI"


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
        "project_key_pepper": TEST_PEPPER,
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


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def schema_options(schema: str) -> str:
    return f"-c search_path={schema}"


@pytest.fixture
async def migrated_pool(
    test_database_url: str, isolated_schema: str
) -> AsyncIterator[AsyncConnectionPool]:
    """A pool on an empty schema with every packaged migration applied."""
    with psycopg.connect(test_database_url, options=schema_options(isolated_schema)) as connection:
        apply_migrations(connection, discover(MIGRATIONS_DIR))
    pool: AsyncConnectionPool = AsyncConnectionPool(
        test_database_url,
        kwargs={"options": schema_options(isolated_schema)},
        min_size=1,
        max_size=12,
        open=False,
    )
    await pool.open(wait=True, timeout=10)
    try:
        yield pool
    finally:
        await pool.close()


def schema_database_url(database_url: str, schema: str) -> str:
    """The database URL with the schema on the search path, for code that builds its own pool."""
    separator = "&" if "?" in database_url else "?"
    return f"{database_url}{separator}options=-c%20search_path%3D{schema}"


@pytest.fixture
def migrated_schema_url(test_database_url: str, isolated_schema: str) -> str:
    """A database URL whose search path is a fresh, fully migrated schema."""
    with psycopg.connect(test_database_url, options=schema_options(isolated_schema)) as connection:
        apply_migrations(connection, discover(MIGRATIONS_DIR))
    return schema_database_url(test_database_url, isolated_schema)
