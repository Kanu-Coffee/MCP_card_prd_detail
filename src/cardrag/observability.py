"""Privacy-preserving logs, metrics, correlation and retention helpers.

The module deliberately does not accept request bodies, search queries or HTTP
headers as observability dimensions.  Every Prometheus label is normalized to a
small, predefined vocabulary and application logs only emit allow-listed fields.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import ipaddress
import json
import logging
import math
import re
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Final

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from prometheus_client.exposition import CONTENT_TYPE_LATEST
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_REQUEST_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar("cardrag_request_id", default=None)
_RUN_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar("cardrag_run_id", default=None)
_JOB_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar("cardrag_job_id", default=None)
_GENERATION_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "cardrag_generation_id", default=None
)

_REQUEST_ID_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_EVENT_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_BEARER_PATTERN: Final = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_URL_SECRET_PATTERN: Final = re.compile(
    r"(?i)([?&](?:access_token|api_key|apikey|authorization|password|secret|token)=)[^&#\s]+"
)
_SECRET_FIELD_PARTS: Final = (
    "authorization",
    "api_key",
    "apikey",
    "password",
    "refresh_token",
    "access_token",
    "secret",
    "query",
    "body",
    "prompt",
    "signed_url",
)
_SAFE_LOG_FIELDS: Final = frozenset(
    {
        "attempt",
        "audit_rows_removed",
        "cache_items_removed",
        "dead_letter",
        "document_id_hash",
        "duration_seconds",
        "error_code",
        "fencing_token",
        "generation_id",
        "issuer",
        "job_id",
        "method",
        "metric_rows_removed",
        "model",
        "operation",
        "outcome",
        "output_characters",
        "page_count",
        "provider",
        "request_id",
        "retryable",
        "route",
        "run_id",
        "stage",
        "status_code",
        "worker_id",
    }
)
_ISSUERS: Final = frozenset({"woori", "kb", "shinhan"})
_STAGES: Final = frozenset(
    {"discover", "download", "ocr", "structure", "index", "materialize", "generation", "publish"}
)
_JOB_OUTCOMES: Final = frozenset(
    {
        "started",
        "succeeded",
        "retry_wait",
        "dead_letter",
        "cancelled",
        "lost_lease",
        "lease_reclaimed",
    }
)
_QUEUE_STATES: Final = frozenset({"queued", "running", "retry_wait", "dead_letter", "cancelled"})
_METHODS: Final = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
_MCP_OPERATIONS: Final = frozenset(
    {
        "search_evidence",
        "get_evidence",
        "get_product_versions",
        "get_source_pdf",
        "get_source_page",
        "issuer_catalog",
        "index_status",
        "product_resource",
        "document_resource",
        "evidence_resource",
        "source_ocr_resource",
        "source_ocr_page_resource",
        "mcp_transport_auth",
        "source_pdf",
        "source_page_png",
    }
)
_MCP_OUTCOMES: Final = frozenset(
    {"success", "no_result", "degraded", "denied", "not_found", "timeout", "error"}
)


def new_request_id(candidate: str | None = None) -> str:
    """Derive an opaque caller correlation ID or generate a fresh one.

    Even a syntactically valid caller value is hashed because a client can
    accidentally place a bearer token in ``X-Request-ID``.
    """

    if candidate is not None and _REQUEST_ID_PATTERN.fullmatch(candidate):
        digest = hashlib.sha256(candidate.encode("ascii")).hexdigest()[:24]
        return f"req_ext_{digest}"
    return f"req_{uuid.uuid4().hex}"


def current_request_id() -> str | None:
    return _REQUEST_ID.get()


def hash_identifier(value: str | None) -> str | None:
    """Make document identifiers correlation-friendly without logging the value."""

    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:24]


def redact_text(value: str, *, maximum_length: int = 512) -> str:
    """Remove common credentials from bounded diagnostic text."""

    redacted = _BEARER_PATTERN.sub("Bearer [REDACTED]", value)
    redacted = _URL_SECRET_PATTERN.sub(r"\1[REDACTED]", redacted)
    if len(redacted) > maximum_length:
        return f"{redacted[:maximum_length]}…"
    return redacted


def safe_log_fields(fields: Mapping[str, object]) -> dict[str, object]:
    """Return only bounded, non-secret fields suitable for the JSON formatter."""

    cleaned: dict[str, object] = {}
    for key, value in fields.items():
        lowered = key.lower()
        if key not in _SAFE_LOG_FIELDS or any(part in lowered for part in _SECRET_FIELD_PARTS):
            continue
        if value is None or isinstance(value, (bool, int, float)):
            cleaned[key] = value
        elif isinstance(value, str):
            cleaned[key] = redact_text(value, maximum_length=256)
        else:
            cleaned[key] = redact_text(str(value), maximum_length=256)
    return cleaned


@contextmanager
def bind_context(
    *,
    request_id: str | None = None,
    run_id: str | None = None,
    job_id: str | None = None,
    generation_id: str | None = None,
) -> Iterator[None]:
    """Bind correlation values for nested async tasks and structured log records."""

    updates = (
        (_REQUEST_ID, request_id),
        (_RUN_ID, run_id),
        (_JOB_ID, job_id),
        (_GENERATION_ID, generation_id),
    )
    tokens: list[tuple[contextvars.ContextVar[str | None], contextvars.Token[str | None]]] = []
    try:
        for variable, value in updates:
            if value is not None:
                tokens.append((variable, variable.set(redact_text(value, maximum_length=128))))
        yield
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


class CardRAGJsonFormatter(logging.Formatter):
    """Small JSON formatter that never serializes arbitrary ``LogRecord`` data."""

    def __init__(
        self,
        *,
        service: str,
        environment: str,
        application_version: str = "dev",
        image_revision: str = "unknown",
    ) -> None:
        super().__init__()
        self.service = redact_text(service, maximum_length=64)
        self.environment = redact_text(environment, maximum_length=32)
        self.application_version = redact_text(application_version, maximum_length=64)
        self.image_revision = redact_text(image_revision, maximum_length=64)

    def format(self, record: logging.LogRecord) -> str:
        candidate = getattr(record, "event", None)
        event = (
            candidate
            if isinstance(candidate, str) and _EVENT_PATTERN.fullmatch(candidate)
            else "application.log"
        )
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname.lower(),
            "service": self.service,
            "environment": self.environment,
            "application_version": self.application_version,
            "image_revision": self.image_revision,
            "event": redact_text(str(event), maximum_length=128),
        }
        context = {
            "request_id": _REQUEST_ID.get(),
            "run_id": _RUN_ID.get(),
            "job_id": _JOB_ID.get(),
            "generation_id": _GENERATION_ID.get(),
        }
        payload.update(
            safe_log_fields({key: getattr(record, key) for key in _SAFE_LOG_FIELDS if hasattr(record, key)})
        )
        # Trusted context variables take precedence over arbitrary LogRecord
        # extras and missing context values do not erase explicit internal IDs.
        payload.update(safe_log_fields({key: value for key, value in context.items() if value is not None}))
        # ``Logger._log`` may preserve an explicit ``exc_info=False`` on the
        # record.  Treat only a truthy exception tuple as exception metadata.
        if record.exc_info and "error_code" not in payload:
            exception_type = record.exc_info[0]
            payload["error_code"] = exception_type.__name__ if exception_type is not None else "Exception"
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def configure_logging(
    *,
    service: str,
    environment: str,
    application_version: str = "dev",
    image_revision: str = "unknown",
    level: int = logging.INFO,
    force: bool = True,
) -> None:
    """Configure one machine-readable stdout/stderr stream for a process."""

    handler = logging.StreamHandler()
    handler.setFormatter(
        CardRAGJsonFormatter(
            service=service,
            environment=environment,
            application_version=application_version,
            image_revision=image_revision,
        )
    )
    logging.basicConfig(level=level, handlers=[handler], force=force)


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    **fields: object,
) -> None:
    """Emit an allow-listed event; unsafe fields such as query/body are discarded."""

    safe_event = event if _EVENT_PATTERN.fullmatch(event) else "application.event"
    logger.log(
        level,
        safe_event,
        extra={"event": safe_event, **safe_log_fields(fields)},
    )


def _issuer(value: str) -> str:
    return value if value in _ISSUERS else "unknown"


def _stage(value: str) -> str:
    return value if value in _STAGES else "unknown"


def _job_outcome(value: str) -> str:
    return value if value in _JOB_OUTCOMES else "unknown"


def _queue_state(value: str) -> str:
    return value if value in _QUEUE_STATES else "unknown"


def normalized_mcp_operation(value: str) -> str:
    """Collapse an operation to the same fixed vocabulary used by Prometheus."""

    return value if value in _MCP_OPERATIONS else "unknown"


def normalized_mcp_outcome(value: str) -> str:
    """Collapse an outcome to the same fixed vocabulary used by Prometheus."""

    return value if value in _MCP_OUTCOMES else "error"


def _as_int(value: object) -> int:
    if isinstance(value, (int, str, bytes, bytearray)):
        return int(value)
    raise TypeError("metric value is not an integer")


def _as_float(value: object) -> float:
    if isinstance(value, (int, float, str, bytes, bytearray)):
        return float(value)
    raise TypeError("metric value is not numeric")


class CardRAGMetrics:
    """Prometheus instruments with strictly bounded label cardinality."""

    def __init__(self, *, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry(auto_describe=True)
        self.http_requests = Counter(
            "cardrag_http_requests_total",
            "Completed CardRAG HTTP requests.",
            ("route", "method", "status_class"),
            registry=self.registry,
        )
        self.http_duration = Histogram(
            "cardrag_http_request_duration_seconds",
            "CardRAG HTTP request duration without query or body dimensions.",
            ("route", "method"),
            buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 45, 90),
            registry=self.registry,
        )
        self.mcp_operations = Counter(
            "cardrag_mcp_operations_total",
            "Completed MCP tool and resource operations.",
            ("operation", "outcome"),
            registry=self.registry,
        )
        self.mcp_duration = Histogram(
            "cardrag_mcp_operation_duration_seconds",
            "MCP tool and resource operation duration without arguments.",
            ("operation", "outcome"),
            buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 45, 90),
            registry=self.registry,
        )
        self.jobs = Counter(
            "cardrag_jobs_total",
            "Offline job lifecycle events.",
            ("issuer", "stage", "outcome"),
            registry=self.registry,
        )
        self.job_duration = Histogram(
            "cardrag_job_duration_seconds",
            "Offline job stage duration.",
            ("issuer", "stage", "outcome"),
            buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800, 3600),
            registry=self.registry,
        )
        self.jobs_in_progress = Gauge(
            "cardrag_jobs_in_progress",
            "Currently executing offline jobs.",
            ("issuer", "stage"),
            registry=self.registry,
        )
        self.job_progress = Gauge(
            "cardrag_job_stage_progress_ratio",
            "Best-known current stage progress (0 while running, 1 after completion).",
            ("issuer", "stage"),
            registry=self.registry,
        )
        self.queue_depth = Gauge(
            "cardrag_job_queue_depth",
            "Durable jobs by bounded state.",
            ("issuer", "stage", "state"),
            registry=self.registry,
        )
        self.queue_oldest_age = Gauge(
            "cardrag_job_queue_oldest_age_seconds",
            "Age of the oldest durable job by bounded state.",
            ("issuer", "stage", "state"),
            registry=self.registry,
        )
        self.job_eta = Gauge(
            "cardrag_job_stage_eta_seconds",
            "Estimated seconds to drain queued work using local observed stage duration.",
            ("issuer", "stage"),
            registry=self.registry,
        )
        self.retention_deleted = Counter(
            "cardrag_retention_deleted_total",
            "Rows or cache entries removed by retention policy.",
            ("kind",),
            registry=self.registry,
        )
        self._duration_ema: dict[tuple[str, str], float] = {}

    def observe_http(self, *, route: str, method: str, status_code: int, duration: float) -> None:
        safe_route = normalized_route(route)
        safe_method = method if method in _METHODS else "OTHER"
        status_class = f"{min(5, max(1, status_code // 100))}xx"
        self.http_requests.labels(safe_route, safe_method, status_class).inc()
        self.http_duration.labels(safe_route, safe_method).observe(max(0.0, duration))

    def job_started(self, *, issuer: str, stage: str) -> None:
        labels = (_issuer(issuer), _stage(stage))
        self.jobs.labels(*labels, "started").inc()
        self.jobs_in_progress.labels(*labels).inc()
        self.job_progress.labels(*labels).set(0)

    def observe_mcp(self, *, operation: str, outcome: str, duration: float) -> None:
        safe_operation = normalized_mcp_operation(operation)
        safe_outcome = normalized_mcp_outcome(outcome)
        self.mcp_operations.labels(safe_operation, safe_outcome).inc()
        self.mcp_duration.labels(safe_operation, safe_outcome).observe(max(0.0, duration))

    def set_job_progress(self, *, issuer: str, stage: str, completed: int, total: int) -> None:
        if total <= 0:
            return
        ratio = min(1.0, max(0.0, completed / total))
        self.job_progress.labels(_issuer(issuer), _stage(stage)).set(ratio)

    def job_finished(self, *, issuer: str, stage: str, outcome: str, duration: float) -> None:
        labels = (_issuer(issuer), _stage(stage))
        safe_outcome = _job_outcome(outcome)
        elapsed = max(0.0, duration)
        self.jobs.labels(*labels, safe_outcome).inc()
        self.job_duration.labels(*labels, safe_outcome).observe(elapsed)
        self.jobs_in_progress.labels(*labels).dec()
        self.job_progress.labels(*labels).set(1 if safe_outcome == "succeeded" else 0)
        previous = self._duration_ema.get(labels)
        self._duration_ema[labels] = elapsed if previous is None else (0.2 * elapsed + 0.8 * previous)

    def lease_reclaimed(self, count: int) -> None:
        if count > 0:
            self.jobs.labels("unknown", "unknown", "lease_reclaimed").inc(count)

    def replace_queue_snapshot(self, rows: list[Mapping[str, object]]) -> None:
        self.queue_depth.clear()
        self.queue_oldest_age.clear()
        self.job_eta.clear()
        queued_by_stage: dict[tuple[str, str], int] = {}
        for row in rows:
            issuer = _issuer(str(row["issuer"]))
            stage = _stage(str(row["stage"]))
            state = _queue_state(str(row["state"]))
            depth = max(0, _as_int(row["depth"]))
            age = max(0.0, _as_float(row["oldest_age_seconds"]))
            self.queue_depth.labels(issuer, stage, state).set(depth)
            self.queue_oldest_age.labels(issuer, stage, state).set(age)
            if state in {"queued", "retry_wait"}:
                queued_by_stage[(issuer, stage)] = queued_by_stage.get((issuer, stage), 0) + depth
        for labels, depth in queued_by_stage.items():
            self.job_eta.labels(*labels).set(depth * self._duration_ema.get(labels, 0.0))

    def retention(self, *, kind: str, count: int) -> None:
        safe_kind = kind if kind in {"audit", "metric_rollup", "page_cache"} else "unknown"
        if count > 0:
            self.retention_deleted.labels(safe_kind).inc(count)

    def render(self) -> bytes:
        return generate_latest(self.registry)


class Observability:
    def __init__(self, *, service: str, environment: str, registry: CollectorRegistry | None = None) -> None:
        self.service = service
        self.environment = environment
        self.metrics = CardRAGMetrics(registry=registry)


_OBSERVABILITY: dict[tuple[str, str], Observability] = {}
_OBSERVABILITY_LOCK = threading.Lock()


def get_observability(*, service: str, environment: str) -> Observability:
    key = (service, environment)
    with _OBSERVABILITY_LOCK:
        instance = _OBSERVABILITY.get(key)
        if instance is None:
            instance = Observability(service=service, environment=environment)
            _OBSERVABILITY[key] = instance
        return instance


def normalized_route(path: str) -> str:
    """Map request paths to a fixed vocabulary, never a user-controlled label."""

    if path == "/mcp" or path.startswith("/mcp/"):
        return "/mcp"
    if path == "/health/live":
        return "/health/live"
    if path == "/health/ready":
        return "/health/ready"
    if path == "/metrics":
        return "/metrics"
    if path.startswith("/.well-known/"):
        return "/.well-known/*"
    if re.fullmatch(r"/sources/[^/]+/pdf", path):
        return "/sources/{document_id}/pdf"
    if re.fullmatch(r"/sources/[^/]+/pages/\d+\.png", path):
        return "/sources/{document_id}/pages/{page}.png"
    return "other"


class ObservabilityMiddleware:
    """ASGI HTTP middleware for request correlation, JSON events and metrics."""

    def __init__(self, app: ASGIApp, *, observability: Observability) -> None:
        self.app = app
        self.observability = observability
        self.logger = logging.getLogger("cardrag.http")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_request_id = headers.get(b"x-request-id", b"").decode("ascii", errors="ignore")
        request_id = new_request_id(raw_request_id or None)
        method = str(scope.get("method", "OTHER")).upper()
        route = normalized_route(str(scope.get("path", "")))
        started = time.perf_counter()
        status_code = 500

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                response_headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != b"x-request-id"
                ]
                response_headers.append((b"x-request-id", request_id.encode("ascii")))
                message["headers"] = response_headers
            await send(message)

        with bind_context(request_id=request_id):
            try:
                await self.app(scope, receive, send_with_request_id)
            except asyncio.CancelledError:
                duration = time.perf_counter() - started
                self.observability.metrics.observe_http(
                    route=route,
                    method=method,
                    status_code=499,
                    duration=duration,
                )
                log_event(
                    self.logger,
                    "http.request.cancelled",
                    level=logging.WARNING,
                    route=route,
                    method=method,
                    status_code=499,
                    duration_seconds=round(duration, 6),
                    error_code="CancelledError",
                    outcome="cancelled",
                )
                raise
            except Exception as exc:
                duration = time.perf_counter() - started
                self.observability.metrics.observe_http(
                    route=route,
                    method=method,
                    status_code=500,
                    duration=duration,
                )
                log_event(
                    self.logger,
                    "http.request.failed",
                    level=logging.ERROR,
                    route=route,
                    method=method,
                    status_code=500,
                    duration_seconds=round(duration, 6),
                    error_code=type(exc).__name__,
                )
                raise
            duration = time.perf_counter() - started
            self.observability.metrics.observe_http(
                route=route,
                method=method,
                status_code=status_code,
                duration=duration,
            )
            log_event(
                self.logger,
                "http.request.completed",
                route=route,
                method=method,
                status_code=status_code,
                duration_seconds=round(duration, 6),
                outcome="success" if status_code < 400 else "error",
            )


class AuthenticationAuditMiddleware:
    """Record MCP transport authentication denials without reading credentials."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        audit_denial: Callable[[str], Awaitable[None]],
        metric_denial: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.app = app
        self.audit_denial = audit_denial
        self.metric_denial = metric_denial
        self.logger = logging.getLogger("cardrag.auth")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or normalized_route(str(scope.get("path", ""))) != "/mcp":
            await self.app(scope, receive, send)
            return
        audited = False
        started = time.perf_counter()

        async def send_with_audit(message: Message) -> None:
            nonlocal audited
            if (
                not audited
                and message["type"] == "http.response.start"
                and int(message["status"]) in {401, 403}
            ):
                audited = True
                try:
                    await self.audit_denial(current_request_id() or new_request_id())
                    if self.metric_denial is not None:
                        await self.metric_denial(time.perf_counter() - started)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Preserve the authentication denial if the audit sink is
                    # unavailable; readiness/structured events expose the fault.
                    log_event(
                        self.logger,
                        "auth.audit.failed",
                        level=logging.ERROR,
                        error_code=type(exc).__name__,
                        outcome="error",
                    )
            await send(message)

        await self.app(scope, receive, send_with_audit)


_LOOPBACK_NETWORKS: Final = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
)


def is_loopback_metrics_request(request: Request) -> bool:
    """Authorize metrics from the actual ASGI peer, never a forwarded header."""

    if request.client is None:
        return False
    try:
        address = ipaddress.ip_address(request.client.host)
    except ValueError:
        return False
    return any(address in network for network in _LOOPBACK_NETWORKS)


def metrics_response(request: Request, observability: Observability) -> Response:
    if not is_loopback_metrics_request(request):
        # Deliberately conceal the operational endpoint from remote callers.
        return Response(status_code=404, headers={"Cache-Control": "no-store"})
    return Response(
        observability.metrics.render(),
        media_type=CONTENT_TYPE_LATEST,
        headers={"Cache-Control": "no-store"},
    )


def record_mcp_rollup(
    database: Any,
    *,
    operation: str,
    outcome: str,
    duration: float,
) -> None:
    """Persist one bounded, anonymous hourly MCP operation aggregate.

    The database function repeats the allow-list normalization and is the only
    mutation privilege granted to the online role.
    """

    safe_operation = normalized_mcp_operation(operation)
    safe_outcome = normalized_mcp_outcome(outcome)
    safe_duration = min(duration, 600.0) if math.isfinite(duration) and duration >= 0 else 0.0
    with database.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT record_mcp_metric_rollup(%s, %s, %s)",
            (safe_operation, safe_outcome, safe_duration),
        )
        connection.commit()


class PostgresMetricRollupWriter:
    """Persist anonymous hourly counters and enforce their retention window."""

    def __init__(self, database: Any, metrics: CardRAGMetrics) -> None:
        self.database = database
        self.metrics = metrics

    def record_job(self, *, issuer: str, stage: str, outcome: str, duration: float) -> None:
        dimensions = {
            "issuer": _issuer(issuer),
            "stage": _stage(stage),
            "outcome": _job_outcome(outcome),
        }
        bucket = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO metric_rollups(bucket_start, metric_name, dimensions, count, sum)
                VALUES (%s, 'job_duration_seconds', %s::jsonb, 1, %s)
                ON CONFLICT (bucket_start, metric_name, dimensions) DO UPDATE SET
                    count = metric_rollups.count + 1,
                    sum = metric_rollups.sum + EXCLUDED.sum
                """,
                (bucket, json.dumps(dimensions, separators=(",", ":"), sort_keys=True), max(0.0, duration)),
            )
            connection.commit()

    def record_mcp(self, *, operation: str, outcome: str, duration: float) -> None:
        """Upsert one anonymous MCP request into its UTC hourly aggregate.

        The only persisted dimensions are bounded operation and outcome
        vocabularies.  No request ID, subject, token, query, arguments or body
        crosses this interface.  ``count`` is request count and ``sum`` is the
        accumulated duration in seconds; retention is enforced by
        :func:`prune_database_retention` at one year.
        """

        record_mcp_rollup(
            self.database,
            operation=operation,
            outcome=outcome,
            duration=duration,
        )

    def refresh_queue(self) -> None:
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT issuer, stage, state, count(*)::int AS depth,
                       coalesce(max(extract(epoch FROM now() - created_at)), 0)::float8
                           AS oldest_age_seconds
                FROM jobs
                WHERE state IN ('queued', 'running', 'retry_wait', 'dead_letter', 'cancelled')
                GROUP BY issuer, stage, state
                """
            )
            rows: list[Mapping[str, object]] = []
            for row in cursor.fetchall():
                rows.append(dict(row))
        self.metrics.replace_queue_snapshot(rows)

    def prune_retention(self) -> tuple[int, int]:
        """Run the owner-only retention operation.

        Runtime workers deliberately never call this method; the local admin
        command owns deletion of access-audit records.
        """

        audits, rollups = prune_database_retention(self.database)
        self.metrics.retention(kind="audit", count=audits)
        self.metrics.retention(kind="metric_rollup", count=rollups)
        return audits, rollups


def prune_database_retention(database: Any) -> tuple[int, int]:
    """Delete expired audit and anonymous metric rows with admin authority."""

    with database.connection() as connection, connection.cursor() as cursor:
        cursor.execute("DELETE FROM audit_events WHERE occurred_at < now() - interval '90 days'")
        audits = max(0, cursor.rowcount)
        cursor.execute("DELETE FROM metric_rollups WHERE bucket_start < now() - interval '1 year'")
        rollups = max(0, cursor.rowcount)
        connection.commit()
    return audits, rollups


class WorkerMaintenance:
    """Cheap cadence guard for read-only queue snapshots."""

    def __init__(
        self,
        writer: PostgresMetricRollupWriter,
        *,
        queue_interval_seconds: float = 30.0,
    ) -> None:
        self.writer = writer
        self.queue_interval_seconds = queue_interval_seconds
        self._next_queue = 0.0

    def tick(self, *, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        if current >= self._next_queue:
            self.writer.refresh_queue()
            self._next_queue = current + self.queue_interval_seconds
