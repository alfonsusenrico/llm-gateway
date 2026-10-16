from __future__ import annotations

from pathlib import Path
from typing import LiteralString

import psycopg
import pytest

from llm_gateway.db.migrate import MIGRATIONS_DIR, MigrationError, apply_migrations, discover


def _write(directory: Path, name: str, sql: str) -> None:
    (directory / name).write_text(sql, encoding="utf-8")


def _connect(database_url: str, schema: str) -> psycopg.Connection:
    return psycopg.connect(database_url, options=f"-c search_path={schema}")


def test_packaged_migrations_are_valid() -> None:
    discover(MIGRATIONS_DIR)


@pytest.mark.db
def test_packaged_migrations_apply_to_empty_database_and_reapply_is_a_no_op(
    test_database_url: str, isolated_schema: str
) -> None:
    migrations = discover(MIGRATIONS_DIR)

    with _connect(test_database_url, isolated_schema) as connection:
        first = apply_migrations(connection, migrations)
        second = apply_migrations(connection, migrations)

    assert first == [migration.version for migration in migrations]
    assert second == []


def test_invalid_file_name_is_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "1_Create Things.sql", "SELECT 1;")

    with pytest.raises(MigrationError, match="name is invalid"):
        discover(tmp_path)


def test_duplicate_version_is_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "0001_first.sql", "SELECT 1;")
    _write(tmp_path, "0001_second.sql", "SELECT 1;")

    with pytest.raises(MigrationError, match="share a version"):
        discover(tmp_path)


@pytest.mark.db
def test_applies_to_empty_database_and_reapply_is_a_no_op(
    tmp_path: Path, test_database_url: str, isolated_schema: str
) -> None:
    _write(tmp_path, "0001_create_widgets.sql", "CREATE TABLE widgets (id bigint PRIMARY KEY);")
    _write(tmp_path, "0002_add_widget_name.sql", "ALTER TABLE widgets ADD COLUMN name text;")
    migrations = discover(tmp_path)

    with _connect(test_database_url, isolated_schema) as connection:
        first = apply_migrations(connection, migrations)
        second = apply_migrations(connection, migrations)
        columns = connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = 'widgets' ORDER BY ordinal_position",
            (isolated_schema,),
        ).fetchall()

    assert first == ["0001", "0002"]
    assert second == []
    assert [column for (column,) in columns] == ["id", "name"]


@pytest.mark.db
def test_changed_applied_migration_stops_the_runner(
    tmp_path: Path, test_database_url: str, isolated_schema: str
) -> None:
    _write(tmp_path, "0001_create_widgets.sql", "CREATE TABLE widgets (id bigint PRIMARY KEY);")
    with _connect(test_database_url, isolated_schema) as connection:
        apply_migrations(connection, discover(tmp_path))
    _write(tmp_path, "0001_create_widgets.sql", "CREATE TABLE widgets (id text PRIMARY KEY);")

    with (
        _connect(test_database_url, isolated_schema) as connection,
        pytest.raises(MigrationError, match="0001 has changed"),
    ):
        apply_migrations(connection, discover(tmp_path))


@pytest.mark.db
def test_applied_migration_without_a_file_stops_the_runner(
    tmp_path: Path, test_database_url: str, isolated_schema: str
) -> None:
    _write(tmp_path, "0001_create_widgets.sql", "CREATE TABLE widgets (id bigint PRIMARY KEY);")
    with _connect(test_database_url, isolated_schema) as connection:
        apply_migrations(connection, discover(tmp_path))
    (tmp_path / "0001_create_widgets.sql").unlink()

    with (
        _connect(test_database_url, isolated_schema) as connection,
        pytest.raises(MigrationError, match="no file: 0001"),
    ):
        apply_migrations(connection, discover(tmp_path))


@pytest.mark.db
def test_failed_migration_is_rolled_back_and_not_recorded(
    tmp_path: Path, test_database_url: str, isolated_schema: str
) -> None:
    _write(
        tmp_path,
        "0001_half_broken.sql",
        "CREATE TABLE widgets (id bigint PRIMARY KEY); SELECT * FROM missing_table;",
    )

    with _connect(test_database_url, isolated_schema) as connection:
        with pytest.raises(psycopg.errors.UndefinedTable):
            apply_migrations(connection, discover(tmp_path))
        tables = connection.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
            (isolated_schema,),
        ).fetchall()
        recorded = connection.execute("SELECT count(*) FROM schema_migrations").fetchone()

    assert [table for (table,) in tables] == ["schema_migrations"]
    assert recorded == (0,)


@pytest.mark.db
def test_inference_migrations_preserve_legacy_calls_results_keys_and_contracts(
    test_database_url: str, isolated_schema: str
) -> None:
    from cryptography.fernet import Fernet
    from psycopg.types.json import Jsonb

    from tests.conftest import TEST_RESULT_KEY
    from tests.test_contract import contract_data

    migrations = discover(MIGRATIONS_DIR)
    legacy = contract_data(contract_version=1, profiles={})
    encrypted = Fernet(TEST_RESULT_KEY.encode()).encrypt(b'{"synthetic":"retained-result"}')
    with _connect(test_database_url, isolated_schema) as connection:
        apply_migrations(connection, [m for m in migrations if m.version <= "0005"])
        connection.execute(
            "INSERT INTO projects (project_id, current_version, weight) VALUES ('personal_agent', 1, 3)"
        )
        connection.execute(
            "INSERT INTO contract_versions (project_id, version, contract, actor, change_note) VALUES ('personal_agent', 1, %s, 'test', 'legacy')",
            (Jsonb(legacy),),
        )
        connection.execute(
            "INSERT INTO project_keys (key_id, project_id, environment, secret_hmac) VALUES ('aaaaaaaaaaaa', 'personal_agent', 'production', %s)",
            ("a" * 64,),
        )
        row = connection.execute(
            "INSERT INTO calls (project_id, environment, provider_call_id, request_fingerprint, operation, provider, model, service_tier_requested, state, reserved_micro_usd, charged_micro_usd) VALUES ('personal_agent', 'production', 'legacy-call', %s, 'demo', 'openai', 'gpt-6-luna', 'standard', 'execution_unknown', 520, 520) RETURNING call_key",
            ("b" * 64,),
        ).fetchone()
        assert row is not None
        connection.execute(
            "INSERT INTO call_events (call_key, to_state, charged_micro_usd, actor) VALUES (%s, 'execution_unknown', 520, 'test')",
            row,
        )
        connection.execute(
            "INSERT INTO call_results (call_key, encrypted, expires_at) VALUES (%s, %s, now() + interval '24 hours')",
            (row[0], encrypted),
        )
        queries: list[LiteralString] = [
            "SELECT projects::text FROM projects",
            "SELECT contract_versions::text FROM contract_versions",
            "SELECT project_keys::text FROM project_keys",
            "SELECT call_events::text FROM call_events",
            "SELECT call_results::text FROM call_results",
            "SELECT request_fingerprint, state, reserved_micro_usd, charged_micro_usd FROM calls",
        ]
        snapshots = [connection.execute(query).fetchall() for query in queries]
        assert apply_migrations(connection, migrations) == ["0006", "0007", "0008"]
        assert [connection.execute(query).fetchall() for query in queries] == snapshots
        assert connection.execute(
            "SELECT request_schema_version, task, model_alias, definition_digest, settings_digest, credential_revision, billing_kind FROM calls"
        ).fetchone() == (1, "llm", None, None, None, None, None)
        assert apply_migrations(connection, migrations) == []
