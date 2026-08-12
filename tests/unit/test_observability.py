from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import Any, Self

import httpx
import pytest
from prometheus_client import CollectorRegistry
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from cardrag.jobs import ClaimedJob, JobState, LostLeaseError
from cardrag.observability import (
    CardRAGJsonFormatter,
    CardRAGMetrics,
    Observability,
    ObservabilityMiddleware,
    PostgresMetricRollupWriter,
    WorkerMaintenance,
    bind_context,
    log_event,
    metrics_response,
    new_request_id,
    normalized_route,
    redact_text,
    safe_log_fields,
)
from cardrag.pipeline.runtime import PermanentStageError, WorkerLoop

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_json_log_is_correlated_and_drops_query_token_and_body() -> None:
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(
        CardRAGJsonFormatter(
            service="worker",
            environment="test",
            application_version="1.2.3",
            image_revision="abc123",
        )
    )
    logger = logging.getLogger("test.observability.redaction")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    with bind_context(
        request_id="req_safe",
        run_id="run-1",
        job_id="job-1",
        generation_id="gen-1",
    ):
        log_event(
            logger,
            "provider.failed",
            query="show my private search",
            body="raw OCR body",
            authorization="Bearer never-log",
            error_code="ProviderError",
            stage="ocr",
        )

    event = json.loads(stream.getvalue())
    serialized = json.dumps(event)
    assert event["request_id"] == "req_safe"
    assert event["run_id"] == "run-1"
    assert event["job_id"] == "job-1"
    assert event["generation_id"] == "gen-1"
    assert event["application_version"] == "1.2.3"
    assert event["image_revision"] == "abc123"
    assert event["stage"] == "ocr"
    assert event["error_code"] == "ProviderError"
    assert "private search" not in serialized
    assert "OCR body" not in serialized
    assert "never-log" not in serialized
    assert event["event"] == "provider.failed"
    assert "super-secret" not in redact_text("Bearer super-secret")
    assert "also-secret" not in redact_text("https://example.test/?token=also-secret")


def test_untrusted_log_message_and_context_cannot_override_correlation() -> None:
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(CardRAGJsonFormatter(service="mcp", environment="test"))
    logger = logging.getLogger("test.observability.untrusted")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    with bind_context(request_id="trusted-request"):
        logger.info(
            "Bearer leaked-token?token=leaked-query",
            extra={"request_id": "attacker-overwrite"},
        )

    event = json.loads(stream.getvalue())
    serialized = json.dumps(event)
    assert event["event"] == "application.log"
    assert event["request_id"] == "trusted-request"
    assert "leaked-token" not in serialized
    assert "leaked-query" not in serialized
    assert "attacker-overwrite" not in serialized


def test_json_formatter_accepts_explicit_false_exc_info() -> None:
    formatter = CardRAGJsonFormatter(service="mcp", environment="test")
    record = logging.LogRecord(
        name="cardrag.readiness",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="readiness check failed",
        args=(),
        exc_info=False,  # type: ignore[arg-type]  # logging accepts this at runtime
    )

    event = json.loads(formatter.format(record))

    assert event["event"] == "application.log"
    assert "error_code" not in event


def test_field_allowlist_cannot_be_bypassed_by_secret_names() -> None:
    cleaned = safe_log_fields(
        {
            "issuer": "woori",
            "duration_seconds": 1.5,
            "query": "private",
            "body": "private",
            "access_token": "secret",
            "unexpected": "secret",
        }
    )
    assert cleaned == {"issuer": "woori", "duration_seconds": 1.5}


def test_request_ids_and_routes_are_bounded() -> None:
    assert new_request_id("client-id_1").startswith("req_ext_")
    assert "client-id_1" not in new_request_id("client-id_1")
    assert new_request_id("bad\nheader").startswith("req_")
    assert normalized_route("/sources/customer-document/pages/123.png") == (
        "/sources/{document_id}/pages/{page}.png"
    )
    assert normalized_route("/private/user-provided-value") == "other"


@pytest.mark.asyncio
async def test_http_middleware_correlates_without_exposing_path_or_query() -> None:
    async def endpoint(scope: Any, receive: Any, send: Any) -> None:
        await PlainTextResponse("ok")(scope, receive, send)

    observability = Observability(
        service="mcp",
        environment="test",
        registry=CollectorRegistry(),
    )
    app = ObservabilityMiddleware(endpoint, observability=observability)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/sources/private-document/pdf?query=never-observe",
            headers={
                "Authorization": "Bearer never-observe",
                "X-Request-ID": "client-correlation-1",
            },
        )

    assert response.status_code == 200
    assert response.headers["x-request-id"].startswith("req_ext_")
    assert response.headers["x-request-id"] != "client-correlation-1"
    rendered = observability.metrics.render().decode()
    assert 'route="/sources/{document_id}/pdf"' in rendered
    assert "private-document" not in rendered
    assert "never-observe" not in rendered


def _request_from_peer(peer: str) -> Request:
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/metrics",
            "raw_path": b"/metrics",
            "query_string": b"",
            "headers": [],
            "client": (peer, 1234),
            "server": ("cardrag", 8000),
        }
    )


def test_metrics_endpoint_uses_actual_loopback_peer_and_ignores_forwarded_headers() -> None:
    observability = Observability(
        service="mcp",
        environment="test",
        registry=CollectorRegistry(),
    )
    allowed = metrics_response(_request_from_peer("127.0.0.1"), observability)
    denied = metrics_response(_request_from_peer("203.0.113.10"), observability)

    assert allowed.status_code == 200
    assert allowed.headers["cache-control"] == "no-store"
    assert denied.status_code == 404
    assert denied.body == b""


def test_metrics_normalize_all_labels_and_track_progress_eta() -> None:
    metrics = CardRAGMetrics(registry=CollectorRegistry())
    metrics.job_started(issuer="attacker-card", stage="arbitrary-user-value")
    metrics.job_finished(
        issuer="attacker-card",
        stage="arbitrary-user-value",
        outcome="arbitrary-error-message",
        duration=12,
    )
    metrics.replace_queue_snapshot(
        [
            {
                "issuer": "attacker-card",
                "stage": "arbitrary-user-value",
                "state": "queued",
                "depth": 2,
                "oldest_age_seconds": 30,
            }
        ]
    )

    rendered = metrics.render().decode()
    assert 'issuer="unknown"' in rendered
    assert 'stage="unknown"' in rendered
    assert 'outcome="unknown"' in rendered
    assert "attacker-card" not in rendered
    assert "arbitrary-user-value" not in rendered
    assert 'cardrag_job_stage_eta_seconds{issuer="unknown",stage="unknown"} 24.0' in rendered


class _Cursor:
    def __init__(self, database: _Database) -> None:
        self.database = database
        self.rowcount = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback

    def execute(self, query: str, params: tuple[object, ...] | None = None) -> None:
        self.database.statements.append((" ".join(query.split()), params))
        if query.startswith("DELETE FROM audit_events"):
            self.rowcount = 3
        elif query.startswith("DELETE FROM metric_rollups"):
            self.rowcount = 4

    def fetchall(self) -> list[dict[str, object]]:
        return [
            {
                "issuer": "woori",
                "stage": "ocr",
                "state": "retry_wait",
                "depth": 5,
                "oldest_age_seconds": 123.0,
            }
        ]


class _Connection:
    def __init__(self, database: _Database) -> None:
        self.database = database

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback

    def cursor(self) -> _Cursor:
        return _Cursor(self.database)

    def commit(self) -> None:
        self.database.commits += 1


class _Database:
    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[object, ...] | None]] = []
        self.commits = 0

    def connection(self) -> _Connection:
        return _Connection(self)


def test_postgres_rollup_contains_only_bounded_dimensions_and_prunes_policy_windows() -> None:
    database = _Database()
    metrics = CardRAGMetrics(registry=CollectorRegistry())
    writer = PostgresMetricRollupWriter(database, metrics)

    writer.record_job(
        issuer="unknown-user-value",
        stage="unknown-user-value",
        outcome="Bearer secret",
        duration=2.5,
    )
    writer.refresh_queue()
    deleted = writer.prune_retention()

    insert_params = database.statements[0][1]
    assert insert_params is not None
    dimensions = json.loads(str(insert_params[1]))
    assert dimensions == {"issuer": "unknown", "outcome": "unknown", "stage": "unknown"}
    assert deleted == (3, 4)
    assert any("interval '90 days'" in statement for statement, _ in database.statements)
    assert any("interval '1 year'" in statement for statement, _ in database.statements)
    rendered = metrics.render().decode()
    assert 'cardrag_job_queue_depth{issuer="woori",stage="ocr",state="retry_wait"} 5.0' in rendered


def test_postgres_mcp_rollup_persists_only_bounded_operation_outcome_and_duration() -> None:
    database = _Database()
    writer = PostgresMetricRollupWriter(database, CardRAGMetrics(registry=CollectorRegistry()))

    writer.record_mcp(
        operation="search_evidence",
        outcome="success",
        duration=1.25,
    )
    writer.record_mcp(
        operation="query=private phrase",
        outcome="Bearer private-token",
        duration=float("nan"),
    )
    writer.record_mcp(
        operation="source_pdf",
        outcome="success",
        duration=1e308,
    )

    first_statement, first_params = database.statements[0]
    assert "record_mcp_metric_rollup" in first_statement
    assert first_params is not None
    assert first_params == ("search_evidence", "success", 1.25)

    _, second_params = database.statements[1]
    assert second_params is not None
    assert second_params == ("unknown", "error", 0.0)
    _, huge_params = database.statements[2]
    assert huge_params == ("source_pdf", "success", 600.0)
    serialized = repr(database.statements)
    assert "private phrase" not in serialized
    assert "private-token" not in serialized


class _Writer:
    def __init__(self) -> None:
        self.queue_calls = 0

    def refresh_queue(self) -> None:
        self.queue_calls += 1


def test_worker_maintenance_has_bounded_queue_snapshot_cadence() -> None:
    writer = _Writer()
    maintenance = WorkerMaintenance(
        writer,  # type: ignore[arg-type]
        queue_interval_seconds=30,
    )

    maintenance.tick(now=10)
    maintenance.tick(now=20)
    maintenance.tick(now=40)
    maintenance.tick(now=111)

    assert writer.queue_calls == 3


class _WorkerJobs:
    def __init__(self, claims: list[ClaimedJob]) -> None:
        self.claims = claims
        self.finished: list[uuid.UUID] = []
        self.finished_provenance: list[tuple[str | None, str | None, str | None]] = []
        self.failed: list[tuple[uuid.UUID, bool]] = []
        self.heartbeat_failure: Exception | None = None
        self.finish_failure: Exception | None = None
        self.fail_failure: Exception | None = None

    def reclaim_expired(self) -> int:
        return 0

    def claim(self, *, worker_id: str, lease_seconds: int) -> ClaimedJob | None:
        del worker_id, lease_seconds
        return self.claims.pop(0) if self.claims else None

    def heartbeat(self, claim: ClaimedJob, *, worker_id: str, lease_seconds: int) -> datetime:
        del claim, worker_id, lease_seconds
        if self.heartbeat_failure is not None:
            raise self.heartbeat_failure
        return datetime.now(UTC) + timedelta(seconds=30)

    def finish(
        self,
        claim: ClaimedJob,
        *,
        worker_id: str,
        provider: str | None = None,
        model: str | None = None,
        config_hash: str | None = None,
    ) -> None:
        del worker_id
        if self.finish_failure is not None:
            raise self.finish_failure
        self.finished.append(claim.id)
        self.finished_provenance.append((provider, model, config_hash))

    def fail(
        self,
        claim: ClaimedJob,
        *,
        worker_id: str,
        error_code: str,
        retryable: bool,
        base_delay_seconds: float = 2.0,
        minimum_delay_seconds: float = 0.0,
    ) -> JobState:
        del worker_id, error_code, base_delay_seconds, minimum_delay_seconds
        if self.fail_failure is not None:
            raise self.fail_failure
        self.failed.append((claim.id, retryable))
        return (
            JobState.RETRY_WAIT
            if retryable and claim.attempt_no < claim.max_attempts
            else JobState.DEAD_LETTER
        )


class _Pipeline:
    def __init__(
        self,
        failure: Exception | None = None,
        *,
        successful_ocr_provenance: dict[str, str] | None = None,
    ) -> None:
        self.settings = SimpleNamespace(
            environment="test",
            ocr_model="fixture-ocr",
            ocr_fallback_model="fixture-fallback",
            ocr_chunk_pages=2,
            ocr_reasoning_effort="high",
            render_scale=3.0,
            embedding_model="fixture-embedding",
            embedding_dimension=1536,
        )
        self.failure = failure
        self.successful_ocr_provenance = successful_ocr_provenance

    async def handle(self, claim: ClaimedJob) -> None:
        if self.failure is not None:
            raise self.failure
        if self.successful_ocr_provenance is not None:
            claim.payload["_successful_ocr_provenance"] = self.successful_ocr_provenance


def _claim(*, attempt: int = 1, maximum: int = 2) -> ClaimedJob:
    return ClaimedJob(
        id=uuid.uuid4(),
        issuer="shinhan",
        stage="ocr",
        document_id="never-export-this-document-id",
        payload={"run_id": "run-fixture", "generation_id": "generation-fixture", "query": "private"},
        attempt_no=attempt,
        max_attempts=maximum,
        fencing_token=4,
        lease_until=datetime.now(UTC) + timedelta(seconds=30),
        lease_owner="fixture-worker",
        generation_id="generation-fixture",
    )


@pytest.mark.asyncio
async def test_worker_loop_reports_success_retry_and_dead_letter_without_payload_labels() -> None:
    metrics = Observability(
        service="worker",
        environment="test",
        registry=CollectorRegistry(),
    )
    success_jobs = _WorkerJobs([_claim()])
    retry_jobs = _WorkerJobs([_claim()])
    dead_jobs = _WorkerJobs([_claim(attempt=2, maximum=2)])

    await WorkerLoop(
        success_jobs,  # type: ignore[arg-type]
        _Pipeline(
            successful_ocr_provenance={
                "provider": "openrouter",
                "model": "fixture-fallback",
                "config_hash": "a" * 64,
            }
        ),  # type: ignore[arg-type]
        worker_id="worker-1",
        lease_seconds=30,
        observability=metrics,
    ).run(once=True)
    await WorkerLoop(
        retry_jobs,  # type: ignore[arg-type]
        _Pipeline(RuntimeError("private provider response")),  # type: ignore[arg-type]
        worker_id="worker-1",
        lease_seconds=30,
        observability=metrics,
    ).run(once=True)
    await WorkerLoop(
        dead_jobs,  # type: ignore[arg-type]
        _Pipeline(PermanentStageError("private OCR body")),  # type: ignore[arg-type]
        worker_id="worker-1",
        lease_seconds=30,
        observability=metrics,
    ).run(once=True)

    assert len(success_jobs.finished) == 1
    assert success_jobs.finished_provenance == [("openrouter", "fixture-fallback", "a" * 64)]
    assert len(retry_jobs.failed) == 1 and retry_jobs.failed[0][1] is True
    assert len(dead_jobs.failed) == 1 and dead_jobs.failed[0][1] is False
    rendered = metrics.metrics.render().decode()
    assert 'outcome="succeeded"' in rendered
    assert 'outcome="retry_wait"' in rendered
    assert 'outcome="dead_letter"' in rendered
    assert "never-export-this-document-id" not in rendered
    assert "private" not in rendered


@pytest.mark.asyncio
async def test_heartbeat_lease_loss_cancels_running_stage() -> None:
    started = __import__("asyncio").Event()
    cancelled = __import__("asyncio").Event()

    class SlowPipeline(_Pipeline):
        async def handle(self, claim: ClaimedJob) -> None:
            del claim
            started.set()
            try:
                await __import__("asyncio").Future()
            except __import__("asyncio").CancelledError:
                cancelled.set()
                raise

    jobs = _WorkerJobs([_claim()])
    jobs.heartbeat_failure = LostLeaseError("cancel requested")
    loop = WorkerLoop(
        jobs,  # type: ignore[arg-type]
        SlowPipeline(),  # type: ignore[arg-type]
        worker_id="worker-1",
        lease_seconds=0,
    )
    task = __import__("asyncio").create_task(loop.run(once=True))
    await started.wait()
    await task

    assert cancelled.is_set()
    assert jobs.finished == []
    assert jobs.failed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["finish", "fail"])
async def test_terminal_transition_lease_race_does_not_kill_worker(transition: str) -> None:
    first = _claim()
    second = _claim()
    jobs = _WorkerJobs([first, second])
    if transition == "finish":
        jobs.finish_failure = LostLeaseError("cancel won finish race")
        pipeline = _Pipeline()
    else:
        jobs.fail_failure = LostLeaseError("cancel won fail race")
        pipeline = _Pipeline(RuntimeError("retryable stage failure"))

    loop = WorkerLoop(
        jobs,  # type: ignore[arg-type]
        pipeline,  # type: ignore[arg-type]
        worker_id="worker-1",
        lease_seconds=30,
    )
    await loop._run_claim(first)

    # The transition race is reported as lost_lease, but the long-lived loop
    # remains usable and can claim subsequent work.
    assert loop.stopping is False


def test_alert_rules_and_runbook_cover_bounded_operational_failures() -> None:
    rules = (PROJECT_ROOT / "deploy/monitoring/cardrag-alerts.yml").read_text(encoding="utf-8")
    runbook = (PROJECT_ROOT / "deploy/monitoring/RUNBOOK.md").read_text(encoding="utf-8")

    for alert in (
        "CardRAGQueueOlderThanDailyCycle",
        "CardRAGDeadLetterCreated",
        "CardRAGRetryBurst",
        "CardRAGLeaseReclaimBurst",
        "CardRAGQueueEtaExceedsOneDay",
        "CardRAGMCPServerErrorRatio",
        "CardRAGMCPDiagnosticLatencyHigh",
        "CardRAGMCPNoResultRatioHigh",
    ):
        assert f"alert: {alert}" in rules
        assert f"## {alert}" in runbook
    assert ") > 30" in rules
    assert "query=" not in rules.lower()
    assert "token=" not in rules.lower()
    assert "Authorization headers" in runbook
    assert "90 days" in runbook
    assert "one year" in runbook
