"""Bounded issuer HTTP telemetry; deliberately stores no paths, tokens or bodies."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, cast

import httpx

from .issuer_collection import IssuerCollectionError

if TYPE_CHECKING:
    from .state import WorkerState

MetricsKey = tuple[str, str, str]


class _CountedStream(httpx.AsyncByteStream):
    def __init__(self, wrapped: httpx.AsyncByteStream, policy: IssuerHTTPMetrics, key: MetricsKey) -> None:
        self.wrapped, self.policy, self.key = wrapped, policy, key

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self.wrapped:
                self.policy.received(self.key, len(chunk))
                yield chunk
        except BaseException as exc:
            import asyncio

            self.policy.increment(
                self.key, "cancelled" if isinstance(exc, asyncio.CancelledError) else "body_errors", 1
            )
            note_transport_failure(exc)
            raise

    async def aclose(self) -> None:
        await self.wrapped.aclose()


class IssuerHTTPMetrics:
    def __init__(
        self, state: WorkerState, run_id: str, *, request_limit: int = 10000, byte_limit: int = 4 * 1024**3
    ) -> None:
        if request_limit < 1 or byte_limit < 1:
            raise ValueError("issuer HTTP budgets must be positive")
        self.state, self.run_id = state, run_id
        self.request_limit, self.byte_limit = request_limit, byte_limit
        state.connection.executescript("""
            CREATE TABLE IF NOT EXISTS issuer_http_usage (
                run_id TEXT, issuer TEXT, host TEXT, purpose TEXT, metric TEXT, value INTEGER NOT NULL,
                PRIMARY KEY(run_id,issuer,host,purpose,metric)
            );
            CREATE TABLE IF NOT EXISTS issuer_http_cooldown (
                issuer TEXT, host TEXT, next_allowed_at TEXT NOT NULL,
                PRIMARY KEY(issuer,host)
            );
        """)

    def increment(self, key: MetricsKey, metric: str, amount: int) -> None:
        self.state.connection.execute(
            "INSERT INTO issuer_http_usage VALUES (?,?,?,?,?,?) ON CONFLICT DO UPDATE SET value=value+excluded.value",
            (self.run_id, *key, metric, amount),
        )
        self.state.connection.commit()

    def used(self, issuer: str, host: str, metric: str) -> int:
        # Enforce both the issuer total and host total independently.
        by_issuer = self.state.connection.execute(
            "SELECT coalesce(sum(value),0) FROM issuer_http_usage WHERE run_id=? AND issuer=? AND metric=?",
            (self.run_id, issuer, metric),
        ).fetchone()[0]
        by_host = self.state.connection.execute(
            "SELECT coalesce(sum(value),0) FROM issuer_http_usage WHERE run_id=? AND host=? AND metric=?",
            (self.run_id, host, metric),
        ).fetchone()[0]
        return cast(int, max(by_issuer, by_host))

    async def before(self, request: httpx.Request) -> None:
        issuer = request.extensions.get("cardrag_issuer", "unknown")
        purpose = request.extensions.get("cardrag_purpose", "unknown")
        key = (issuer, request.url.host, purpose)
        request.extensions["cardrag_metrics_key"] = key
        request.extensions["cardrag_metrics"] = self
        row = self.state.connection.execute(
            "SELECT next_allowed_at FROM issuer_http_cooldown WHERE issuer=? AND host=?", key[:2]
        ).fetchone()
        if row is not None and datetime.now(UTC) < datetime.fromisoformat(row[0]):
            raise IssuerCollectionError("issuer_origin_cooldown", "OriginCooldown")
        if (
            self.used(key[0], key[1], "requests") >= self.request_limit
            or self.used(key[0], key[1], "body_bytes") >= self.byte_limit
        ):
            raise IssuerCollectionError("issuer_origin_budget_deferred", "OriginBudget")
        self.increment(key, "requests", 1)

    def cooldown(self, key: MetricsKey, *, seconds: float) -> None:
        due = (datetime.now(UTC) + timedelta(seconds=max(1, min(seconds, 7 * 86400)))).isoformat()
        self.state.connection.execute(
            "INSERT INTO issuer_http_cooldown VALUES (?,?,?) ON CONFLICT DO UPDATE "
            "SET next_allowed_at=max(next_allowed_at,excluded.next_allowed_at)",
            (*key[:2], due),
        )
        self.state.connection.commit()

    async def after(self, response: httpx.Response) -> None:
        key = response.request.extensions["cardrag_metrics_key"]
        self.increment(key, f"status_{response.status_code}", 1)
        retry_after = response.headers.get("retry-after")
        if response.status_code in {403, 429} or (response.status_code == 503 and retry_after is not None):
            value = retry_after or ""
            try:
                seconds = float(value)
            except ValueError:
                try:
                    seconds = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    seconds = 3600
            import math

            self.cooldown(key, seconds=seconds if math.isfinite(seconds) else 3600)
        if response.is_stream_consumed:
            self.received(key, len(response.content))
        else:
            response.stream = _CountedStream(cast(httpx.AsyncByteStream, response.stream), self, key)

    def received(self, key: MetricsKey, amount: int) -> None:
        self.increment(key, "body_bytes", amount)
        if self.used(key[0], key[1], "body_bytes") > self.byte_limit:
            raise IssuerCollectionError("issuer_origin_budget_deferred", "OriginBudget")

    def report(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.state.connection.execute(
                "SELECT issuer,host,purpose,metric,value FROM issuer_http_usage WHERE run_id=? "
                "ORDER BY issuer,host,purpose,metric",
                (self.run_id,),
            )
        ]


def note_transport_failure(exc: BaseException) -> None:
    if not isinstance(exc, httpx.RequestError):
        return
    try:
        request = exc.request
    except RuntimeError:
        return
    observer = request.extensions.get("cardrag_metrics")
    key = request.extensions.get("cardrag_metrics_key")
    if observer is not None and key is not None:
        observer.increment(
            key, "timeouts" if isinstance(exc, httpx.TimeoutException) else "connection_errors", 1
        )
