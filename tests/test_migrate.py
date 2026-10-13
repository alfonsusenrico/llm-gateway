from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from llm_gateway.db.migrate import MIGRATIONS_DIR, MigrationError, apply_migrations, discover


def _write(directory: Path, name: str, sql: str) -> None:
    (directory / name).write_text(sql, encoding="utf-8")


def _connect(database_url: str, schema: str) -> psycopg.Connection:
    return psycopg.connect(database_url, options=f"-c search_path={schema}")


def test_packaged_migrations_are_valid() -> None:
    discover(MIGRATIONS_DIR)


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
