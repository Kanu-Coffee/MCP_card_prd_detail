"""Issuer-local origin failures, independent of shared Worker storage failures."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from typing import Any

import httpx
from cardrag_core import WebDAVError


class IssuerCollectionError(RuntimeError):
    """A safe origin/parser verdict; never retains response text or credentials."""

    def __init__(self, reason_code: str, exception_class: str) -> None:
        self.reason_code = reason_code
        self.exception_class = exception_class
        super().__init__(reason_code)


def origin_failure(exc: Exception) -> IssuerCollectionError:
    # These indicate shared/local infrastructure, even inside an adapter hook.
    if isinstance(exc, (OSError, sqlite3.Error, WebDAVError, MemoryError)):
        raise exc
    if isinstance(exc, httpx.TimeoutException):
        reason = "issuer_origin_timeout"
    elif isinstance(exc, httpx.RequestError):
        reason = "issuer_origin_network"
    elif isinstance(exc, httpx.HTTPStatusError):
        reason = "issuer_origin_http_status"
    else:
        reason = "issuer_source_invalid"
    return IssuerCollectionError(reason, type(exc).__name__)


@dataclass(slots=True)
class IssuerCollectionOutcome:
    issuer: str
    discovery_status: str = "pending"
    download_status: str = "pending"
    reason_code: str | None = None
    exception_class: str | None = None
    attempts: int = 0
    elapsed_seconds: float = 0
    discovered_count: int = 0
    acquired_count: int = 0
    adopted_count: int = 0
    carried_count: int = 0
    last_successful_collection_at: str | None = None
    origin_freshness_at: str | None = None
    carry_generation_id: str | None = None

    @property
    def failed(self) -> bool:
        return self.reason_code is not None

    def payload(self) -> dict[str, Any]:
        return asdict(self)
