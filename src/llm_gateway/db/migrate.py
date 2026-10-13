"""Forward-only SQL migration runner.

Migrations are hand-written files named ``NNNN_snake_case.sql`` in the package's
``migrations`` directory. Each runs in its own transaction and is recorded with a
SHA-256 checksum; an applied file that later changes stops the runner. It runs as
a separate step before the API starts, never at application startup.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import psycopg

from llm_gateway.logging_setup import configure_logging
from llm_gateway.settings import SettingsError, load_settings

MIGRATIONS_DIR = Path(__file__).with_name("migrations")
_FILENAME = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")
# Serializes concurrent runners; an arbitrary constant owned by this module.
_ADVISORY_LOCK_KEY = 7_402_110_001
logger = logging.getLogger(__name__)


class MigrationError(RuntimeError):
    """A migration set that must not be applied."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: str
    name: str
    sql: str
    checksum: str


def discover(directory: Path) -> list[Migration]:
    migrations: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = _FILENAME.match(path.name)
        if match is None:
            raise MigrationError(f"Migration file name is invalid: {path.name}")
        sql = path.read_text(encoding="utf-8")
        migrations.append(
            Migration(
                version=match["version"],
                name=match["name"],
                sql=sql,
                checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            )
        )
    versions = [migration.version for migration in migrations]
    if len(versions) != len(set(versions)):
        raise MigrationError("Two migration files share a version number.")
    return migrations


def apply_migrations(connection: psycopg.Connection, migrations: list[Migration]) -> list[str]:
    """Apply pending migrations in order and return the versions applied."""
    connection.autocommit = True
    connection.execute("SELECT pg_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,))
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version text PRIMARY KEY,
                name text NOT NULL,
                checksum text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        rows = connection.execute("SELECT version, checksum FROM schema_migrations").fetchall()
        applied = {str(version): str(checksum) for version, checksum in rows}
        known = {migration.version for migration in migrations}
        missing = sorted(set(applied) - known)
        if missing:
            raise MigrationError(f"Applied migrations have no file: {', '.join(missing)}")
        newly_applied: list[str] = []
        for migration in migrations:
            recorded = applied.get(migration.version)
            if recorded is not None:
                if recorded != migration.checksum:
                    raise MigrationError(f"Applied migration {migration.version} has changed.")
                continue
            with connection.transaction():
                connection.execute(migration.sql.encode("utf-8"))
                connection.execute(
                    "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                    (migration.version, migration.name, migration.checksum),
                )
            newly_applied.append(migration.version)
            logger.info("Applied migration %s_%s", migration.version, migration.name)
        return newly_applied
    finally:
        connection.execute("SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_KEY,))


def main() -> int:
    try:
        settings = load_settings()
    except SettingsError as exc:
        print(exc, file=sys.stderr)
        return 2
    configure_logging(settings)
    try:
        migrations = discover(MIGRATIONS_DIR)
        with psycopg.connect(str(settings.database_url)) as connection:
            applied = apply_migrations(connection, migrations)
    except MigrationError as exc:
        logger.error("Migration stopped: %s", exc)
        return 1
    except psycopg.Error as exc:
        logger.error("Migration failed: %s", type(exc).__name__)
        return 1
    logger.info("Migrations complete: %d applied, %d total", len(applied), len(migrations))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
