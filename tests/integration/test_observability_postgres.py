from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from prometheus_client import CollectorRegistry

from cardrag.db import Postgres
from cardrag.jobs import JobRepository
from cardrag.observability import CardRAGMetrics, PostgresMetricRollupWriter
from cardrag.service.postgres_repository import PostgresCardRAGRepository

pytestmark = pytest.mark.integration


def test_rollup_queue_snapshot_and_retention_use_real_postgres(clean_database: Postgres) -> None:
    metrics = CardRAGMetrics(registry=CollectorRegistry())
    writer = PostgresMetricRollupWriter(clean_database, metrics)
    jobs = JobRepository(clean_database)
    jobs.enqueue(
        issuer="shinhan",
        stage="ocr",
        document_id="fixture-document",
        idempotency_key="observability:fixture",
        payload={"query": "must not reach metrics"},
    )
    with clean_database.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO audit_events(event_type, request_id, subject_hash, outcome, occurred_at)
            VALUES
              ('source_pdf', 'old-audit', %s, 'allowed', now() - interval '91 days'),
              ('source_pdf', 'current-audit', %s, 'allowed', now())
            """,
            ("a" * 64, "b" * 64),
        )
        cursor.execute(
            """
            INSERT INTO metric_rollups(bucket_start, metric_name, dimensions, count, sum)
            VALUES
              (now() - interval '367 days', 'fixture_old', '{}'::jsonb, 1, 1),
              (date_trunc('hour', now()), 'fixture_current', '{}'::jsonb, 1, 1)
            """
        )
        connection.commit()

    writer.record_job(issuer="shinhan", stage="ocr", outcome="succeeded", duration=4.5)
    writer.record_mcp(operation="search_evidence", outcome="success", duration=0.75)
    writer.record_mcp(operation="search_evidence", outcome="success", duration=1.25)
    writer.refresh_queue()
    assert writer.prune_retention() == (1, 1)

    with clean_database.connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT request_id FROM audit_events ORDER BY request_id")
        assert [row["request_id"] for row in cursor.fetchall()] == ["current-audit"]
        cursor.execute("SELECT metric_name, dimensions, count, sum FROM metric_rollups ORDER BY metric_name")
        rows = cursor.fetchall()

    assert [row["metric_name"] for row in rows] == [
        "fixture_current",
        "job_duration_seconds",
        "mcp_operation_duration_seconds",
    ]
    job_row = rows[1]
    assert job_row["dimensions"] == {
        "issuer": "shinhan",
        "stage": "ocr",
        "outcome": "succeeded",
    }
    assert job_row["count"] == 1
    assert job_row["sum"] == 4.5
    mcp_row = rows[2]
    assert mcp_row["dimensions"] == {
        "operation": "search_evidence",
        "outcome": "success",
    }
    assert mcp_row["count"] == 2
    assert mcp_row["sum"] == 2.0
    rendered = metrics.render().decode()
    assert 'cardrag_job_queue_depth{issuer="shinhan",stage="ocr",state="queued"} 1.0' in rendered
    assert "fixture-document" not in rendered
    assert "must not reach metrics" not in rendered


@pytest.mark.asyncio
async def test_online_repository_persists_anonymous_mcp_hourly_rollup(
    clean_database: Postgres,
    tmp_path: Path,
) -> None:
    repository = PostgresCardRAGRepository(
        clean_database,
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        tmp_path,
    )

    await repository.record_mcp_metric(
        operation="get_evidence",
        outcome="not_found",
        duration=0.2,
    )
    await repository.record_mcp_metric(
        operation="get_evidence",
        outcome="not_found",
        duration=0.3,
    )
    await repository.record_mcp_metric(
        operation="mcp_transport_auth",
        outcome="denied",
        duration=0.1,
    )
    with clean_database.connection() as connection, connection.cursor() as cursor:
        for duration in ("-1", "'NaN'::float8", "'Infinity'::float8", "1e308"):
            cursor.execute(
                f"SELECT record_mcp_metric_rollup('source_pdf', 'error', {duration})"  # noqa: S608
            )
        connection.commit()

    with clean_database.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT date_trunc('hour', bucket_start) = bucket_start AS hourly,
                   metric_name, dimensions, count, sum
            FROM metric_rollups
            """
        )
        rows = cursor.fetchall()

    assert len(rows) == 3
    row = next(row for row in rows if row["dimensions"]["operation"] == "get_evidence")
    assert row["hourly"] is True
    assert row["metric_name"] == "mcp_operation_duration_seconds"
    assert row["dimensions"] == {"operation": "get_evidence", "outcome": "not_found"}
    assert row["count"] == 2
    assert row["sum"] == pytest.approx(0.5)
    assert set(row["dimensions"]) == {"operation", "outcome"}
    transport_row = next(row for row in rows if row["dimensions"]["operation"] == "mcp_transport_auth")
    assert transport_row["dimensions"] == {
        "operation": "mcp_transport_auth",
        "outcome": "denied",
    }
    bounded_row = next(row for row in rows if row["dimensions"]["operation"] == "source_pdf")
    assert bounded_row["dimensions"] == {"operation": "source_pdf", "outcome": "error"}
    assert bounded_row["count"] == 4
    assert bounded_row["sum"] == 600.0
