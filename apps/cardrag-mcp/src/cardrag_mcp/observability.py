"""Low-cardinality metrics and structured logs without request arguments."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from dataclasses import dataclass
from typing import Any

from mcp.server import MCPServer
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

TOOL_NAMES = frozenset(
    {
        "search_contracts",
        "get_contract_bundle",
        "list_product_revisions",
        "search_evidence",
        "get_evidence",
        "get_product",
        "get_source_pdf",
        "get_source_page",
        "list_recent_products",
        "find_products",
        "find_cards_by_merchant",
        "get_product_summary",
        "experimental_long_context_audit",
    }
)


class ObservedMCPServer(MCPServer):
    """Measure validated tool dispatch without storing arguments or unbounded labels."""

    def __init__(self, name: str, *, metrics: Metrics, **kwargs: Any) -> None:
        super().__init__(name, **kwargs)
        self.metrics = metrics

    async def call_tool(self, name: str, arguments: dict[str, Any], context: Any = None) -> Any:
        label = name if name in TOOL_NAMES else "unknown"
        started = time.perf_counter()
        outcome = "error"
        try:
            result = await super().call_tool(name, arguments, context)
            outcome = "error" if getattr(result, "is_error", False) else "success"
            self.metrics.tool_response_bytes.labels(tool=label).observe(
                len(result.model_dump_json().encode("utf-8"))
            )
            return result
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            self.metrics.tool_calls.labels(tool=label, outcome=outcome).inc()
            self.metrics.tool_seconds.labels(tool=label).observe(time.perf_counter() - started)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
            "timestamp": time.time(),
        }
        safe = getattr(record, "safe_fields", None)
        if isinstance(safe, dict):
            payload.update(safe)
        if record.exc_info and record.exc_info[0] is not None:
            payload["error_type"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())


def log_event(logger: logging.Logger, message: str, **safe_fields: Any) -> None:
    """Log only fields explicitly supplied by internal call sites."""

    logger.info(message, extra={"safe_fields": safe_fields})


@dataclass(slots=True)
class Metrics:
    registry: CollectorRegistry
    operations: Counter
    operation_seconds: Histogram
    updates: Counter
    ready: Gauge
    update_age_seconds: Gauge
    tool_calls: Counter
    tool_seconds: Histogram
    tool_response_bytes: Histogram
    metadata_cache: Gauge

    @classmethod
    def create(cls) -> Metrics:
        registry = CollectorRegistry(auto_describe=True)
        return cls(
            registry=registry,
            tool_calls=Counter(
                "cardrag_mcp_tool_calls_total",
                "MCP tool dispatches including invalid calls.",
                ("tool", "outcome"),
                registry=registry,
            ),
            tool_seconds=Histogram(
                "cardrag_mcp_tool_seconds",
                "MCP tool dispatch latency.",
                ("tool",),
                registry=registry,
                buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 15, 60),
            ),
            tool_response_bytes=Histogram(
                "cardrag_mcp_tool_response_bytes",
                "Serialized MCP result bytes including envelopes.",
                ("tool",),
                registry=registry,
                buckets=(1024, 4096, 16384, 65536, 262144, 1048576, 4194304),
            ),
            metadata_cache=Gauge(
                "cardrag_mcp_metadata_cache",
                "Bounded metadata cache counters and usage.",
                ("measure",),
                registry=registry,
            ),
            operations=Counter(
                "cardrag_mcp_operations_total",
                "Completed MCP and HTTP operations.",
                ("operation", "outcome"),
                registry=registry,
            ),
            operation_seconds=Histogram(
                "cardrag_mcp_operation_seconds",
                "MCP and HTTP operation latency.",
                ("operation",),
                registry=registry,
                buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 15, 60),
            ),
            updates=Counter(
                "cardrag_mcp_updates_total",
                "Background generation update attempts.",
                ("outcome",),
                registry=registry,
            ),
            ready=Gauge(
                "cardrag_mcp_ready",
                "One when a verified local generation is active.",
                registry=registry,
            ),
            update_age_seconds=Gauge(
                "cardrag_mcp_last_success_unixtime",
                "Unix time of the last successful or no-op stable-channel poll.",
                registry=registry,
            ),
        )

    def body(self) -> bytes:
        return generate_latest(self.registry)
