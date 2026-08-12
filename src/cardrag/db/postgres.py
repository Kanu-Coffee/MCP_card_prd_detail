"""Small psycopg repository with checksum-verified SQL migrations."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path
from typing import Any

from psycopg import Connection, sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


class MigrationDriftError(RuntimeError):
    pass


class Postgres:
    def __init__(
        self,
        database_url: str,
        *,
        min_size: int = 1,
        max_size: int = 8,
        statement_timeout_seconds: float | None = None,
        lock_timeout_seconds: float | None = None,
    ) -> None:
        options = _session_timeout_options(
            statement_timeout_seconds=statement_timeout_seconds,
            lock_timeout_seconds=lock_timeout_seconds,
        )
        kwargs: dict[str, Any] = {"autocommit": False, "row_factory": dict_row}
        if options:
            kwargs["options"] = options
        self._pool = ConnectionPool(
            conninfo=database_url,
            min_size=min_size,
            max_size=max_size,
            kwargs=kwargs,
            open=False,
        )

    def open(self) -> None:
        self._pool.open(wait=True)

    def close(self) -> None:
        self._pool.close()

    @contextmanager
    def connection(self) -> Iterator[Connection[Any]]:
        with self._pool.connection() as connection:
            yield connection

    def ping(self) -> bool:
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT 1 AS ok")
            row = cursor.fetchone()
            return bool(row is not None and row["ok"] == 1)

    def migrate(self) -> list[int]:
        migration_root = files("cardrag.db.migrations")
        migrations = sorted(
            (item for item in migration_root.iterdir() if item.name.endswith(".sql")),
            key=lambda item: item.name,
        )
        applied: list[int] = []
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(hashtext('cardrag-schema-migration'))")
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version integer PRIMARY KEY,
                        name text NOT NULL,
                        checksum text NOT NULL,
                        applied_at timestamptz NOT NULL DEFAULT now()
                    )
                    """
                )
                cursor.execute("SELECT version, checksum FROM schema_migrations")
                known = {int(row["version"]): row["checksum"] for row in cursor.fetchall()}
                for resource in migrations:
                    version = int(resource.name.split("_", 1)[0])
                    body = resource.read_text(encoding="utf-8")
                    digest = hashlib.sha256(body.encode()).hexdigest()
                    if version in known:
                        if known[version] != digest:
                            raise MigrationDriftError(f"migration {version} checksum changed")
                        continue
                    cursor.execute(body)
                    cursor.execute(
                        "INSERT INTO schema_migrations(version, name, checksum) VALUES (%s, %s, %s)",
                        (version, resource.name, digest),
                    )
                    applied.append(version)
            connection.commit()
        return applied

    def execute_file(self, path: Path) -> None:
        """Execute a trusted operator-selected SQL file; never exposed over MCP."""
        body = path.read_text(encoding="utf-8")
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(sql.SQL(body))
            connection.commit()


def _session_timeout_options(
    *,
    statement_timeout_seconds: float | None,
    lock_timeout_seconds: float | None,
) -> str:
    values = {
        "statement_timeout": statement_timeout_seconds,
        "lock_timeout": lock_timeout_seconds,
    }
    options: list[str] = []
    for name, seconds in values.items():
        if seconds is None:
            continue
        if not 0 < seconds <= 300:
            raise ValueError(f"{name} must be between 0 and 300 seconds")
        milliseconds = max(1, int(seconds * 1000))
        options.extend(("-c", f"{name}={milliseconds}ms"))
    return " ".join(options)
