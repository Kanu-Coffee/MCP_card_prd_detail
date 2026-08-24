from __future__ import annotations

import asyncio
import os
import time
from types import SimpleNamespace

import psycopg
import pytest

from cardrag.db import Postgres
from cardrag.service.query import QueryService, ServiceTimeoutError


def _runtime_url(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} is not configured")
    return value


@pytest.mark.integration
def test_schema_15_requires_pgvector_086_and_preserves_runtime_schema_boundary(
    migrated_database: Postgres,
) -> None:
    with migrated_database.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        )
        assert cursor.fetchone() == {"extversion": "0.8.6"}
        cursor.execute(
            "SELECT name FROM schema_migrations WHERE version = 15"
        )
        assert cursor.fetchone() == {"name": "015_pgvector_086.sql"}
        cursor.execute(
            """
            SELECT
                has_schema_privilege('cardrag_worker', 'public', 'USAGE') AS worker_usage,
                has_schema_privilege('cardrag_worker', 'public', 'CREATE') AS worker_create,
                has_schema_privilege('cardrag_mcp', 'public', 'USAGE') AS mcp_usage,
                has_schema_privilege('cardrag_mcp', 'public', 'CREATE') AS mcp_create
            """
        )
        assert cursor.fetchone() == {
            "worker_usage": True,
            "worker_create": False,
            "mcp_usage": True,
            "mcp_create": False,
        }


@pytest.mark.integration
def test_legacy_policy_functions_are_not_publicly_executable(
    migrated_database: Postgres,
) -> None:
    signatures = (
        "set_generation_root_key()",
        "cardrag_ocr_manifest_reusable(jsonb,text,text,text,text,text,double precision,integer,text,text)",
        "cardrag_legacy_adoption_bound(jsonb,text,text,text,text[])",
    )
    with migrated_database.connection() as connection, connection.cursor() as cursor:
        for signature in signatures:
            cursor.execute(
                "SELECT has_function_privilege('public', %s, 'EXECUTE') AS allowed",
                (signature,),
            )
            assert cursor.fetchone() == {"allowed": False}


@pytest.mark.integration
def test_mcp_role_is_read_only_except_audit_and_anonymous_metric_upsert(
    migrated_database: object,
) -> None:
    del migrated_database  # guarantees owner migrations have completed
    with psycopg.connect(_runtime_url("CARDRAG_TEST_MCP_DATABASE_URL")) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM generations")
            assert cursor.fetchone() is not None
            cursor.execute(
                """
                INSERT INTO audit_events(event_type, request_id, subject_hash, outcome)
                VALUES ('fixture', 'request-fixture', repeat('a', 64), 'success')
                """
            )
            cursor.execute("SELECT record_mcp_metric_rollup('search_evidence', 'success', 0.5)")
        connection.commit()

    with (
        psycopg.connect(_runtime_url("CARDRAG_TEST_MCP_DATABASE_URL")) as connection,
        connection.cursor() as cursor,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        cursor.execute(
            "INSERT INTO generations(generation_id, state, manifest_sha256, root_uri, schema_version) "
            "VALUES ('forbidden', 'building', repeat('0',64), '', 'forbidden')"
        )

    with (
        psycopg.connect(_runtime_url("CARDRAG_TEST_MCP_DATABASE_URL")) as connection,
        connection.cursor() as cursor,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        cursor.execute("DELETE FROM audit_events")

    with (
        psycopg.connect(_runtime_url("CARDRAG_TEST_MCP_DATABASE_URL")) as connection,
        connection.cursor() as cursor,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        cursor.execute("DELETE FROM metric_rollups")

    for forbidden_statement in (
        "INSERT INTO metric_rollups(bucket_start, metric_name, dimensions, count, sum) "
        "VALUES (date_trunc('hour', now()), 'job_duration_seconds', '{}'::jsonb, 1, 1)",
        "UPDATE metric_rollups SET count=999",
    ):
        with (
            psycopg.connect(_runtime_url("CARDRAG_TEST_MCP_DATABASE_URL")) as connection,
            connection.cursor() as cursor,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            cursor.execute(forbidden_statement)


@pytest.mark.integration
def test_worker_role_has_dml_but_no_ddl(migrated_database: object) -> None:
    del migrated_database
    with psycopg.connect(_runtime_url("CARDRAG_TEST_WORKER_DATABASE_URL")) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO metric_rollups(bucket_start, metric_name, dimensions, count, sum)
                VALUES (date_trunc('hour', now()), 'role_fixture', '{}'::jsonb, 1, 1)
                ON CONFLICT (bucket_start, metric_name, dimensions)
                DO UPDATE SET count=metric_rollups.count+1
                """
            )
        connection.commit()

    for forbidden_statement in (
        "INSERT INTO generations(generation_id, state, manifest_sha256, root_uri, schema_version) "
        "VALUES ('worker-forbidden', 'building', repeat('0',64), '', 'forbidden')",
        "UPDATE active_generation SET generation_id=generation_id WHERE singleton=true",
        "INSERT INTO audit_events(event_type, request_id, subject_hash, outcome) "
        "VALUES ('fixture', 'worker-forbidden', repeat('b',64), 'success')",
        "DELETE FROM audit_events WHERE false",
        "DELETE FROM metric_rollups WHERE false",
        "UPDATE pipeline_runs SET pause_requested=true WHERE false",
    ):
        with (
            psycopg.connect(_runtime_url("CARDRAG_TEST_WORKER_DATABASE_URL")) as connection,
            connection.cursor() as cursor,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            cursor.execute(forbidden_statement)

    with (
        psycopg.connect(_runtime_url("CARDRAG_TEST_WORKER_DATABASE_URL")) as connection,
        connection.cursor() as cursor,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        cursor.execute("CREATE TABLE forbidden_worker_ddl(id integer)")


@pytest.mark.integration
def test_mcp_pool_enforces_server_side_statement_and_lock_timeouts(
    migrated_database: Postgres,
) -> None:
    online = Postgres(
        _runtime_url("CARDRAG_TEST_MCP_DATABASE_URL"),
        min_size=1,
        max_size=1,
        statement_timeout_seconds=0.25,
        lock_timeout_seconds=0.1,
    )
    online.open()
    try:
        with online.connection() as connection, connection.cursor() as cursor:
            cursor.execute("SHOW statement_timeout")
            assert cursor.fetchone()["statement_timeout"] == "250ms"
            cursor.execute("SHOW lock_timeout")
            assert cursor.fetchone()["lock_timeout"] == "100ms"

        started = time.perf_counter()
        with (
            pytest.raises(psycopg.errors.QueryCanceled),
            online.connection() as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute("SELECT pg_sleep(2)")
        assert time.perf_counter() - started < 1

        def timed_query() -> None:
            with online.connection() as connection, connection.cursor() as cursor:
                cursor.execute("SELECT pg_sleep(2)")

        service = QueryService(
            SimpleNamespace(),  # type: ignore[arg-type]
            max_concurrent_requests=1,
            request_timeout_seconds=1,
        )
        with pytest.raises(ServiceTimeoutError, match="database fixture timed out"):
            asyncio.run(
                service._bounded(  # noqa: SLF001 - exercise the shared normalization boundary
                    lambda: asyncio.to_thread(timed_query),
                    label="database fixture",
                )
            )

        with migrated_database.connection() as owner, owner.cursor() as owner_cursor:
            owner_cursor.execute("LOCK TABLE generations IN ACCESS EXCLUSIVE MODE")
            started = time.perf_counter()
            with (
                pytest.raises(psycopg.errors.LockNotAvailable),
                online.connection() as connection,
                connection.cursor() as cursor,
            ):
                cursor.execute("SELECT count(*) FROM generations")
            assert time.perf_counter() - started < 1
            owner.rollback()
    finally:
        online.close()
