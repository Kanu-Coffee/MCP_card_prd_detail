"""Low-cardinality metrics and structured logs without request arguments."""

from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import dataclass
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest


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

    @classmethod
    def create(cls) -> Metrics:
        registry = CollectorRegistry(auto_describe=True)
        return cls(
            registry=registry,
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
