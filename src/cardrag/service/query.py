"""Bounded orchestration for CardRAG online reads."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TypeVar

from psycopg.errors import QueryCanceled

from cardrag.service.models import (
    EvidenceLookupRequest,
    EvidencePage,
    Issuer,
    ProductVersions,
    ReadinessStatus,
    SearchPage,
    SearchRequest,
    SourcePage,
    SourcePdf,
)
from cardrag.service.repository import CardRAGRepository

T = TypeVar("T")


@dataclass(slots=True)
class _BudgetLease:
    owner: QueryService
    active: bool = True


_ACTIVE_BUDGET: ContextVar[_BudgetLease | None] = ContextVar(
    "cardrag_active_request_budget",
    default=None,
)


class ServiceUnavailableError(RuntimeError):
    """A stable public error that never includes queries or backend details."""


class ServiceTimeoutError(ServiceUnavailableError):
    """A bounded online operation exceeded its configured deadline."""


class NotFoundError(RuntimeError):
    pass


class QueryService:
    def __init__(
        self,
        repository: CardRAGRepository,
        *,
        max_concurrent_requests: int,
        request_timeout_seconds: float,
    ) -> None:
        self.repository = repository
        self._semaphore = asyncio.Semaphore(max_concurrent_requests)
        self._auxiliary_semaphore = asyncio.Semaphore(max_concurrent_requests)
        self._timeout = request_timeout_seconds
        self._budget_tasks: set[asyncio.Task[object]] = set()

    async def run_with_budget(
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        label: str,
        timeout_seconds: float | None = None,
    ) -> T:
        """Run one logical request under the shared admission and deadline budget.

        Calls made by the same task while a lease is active are re-entrant.  A
        composite source request can therefore call ``source_pdf()`` and
        ``source_page()`` without acquiring the semaphore twice (which would
        deadlock when the configured concurrency is one).  The timeout includes
        time waiting for admission.  A copied context cannot retain a stale
        exemption because the shared lease is marked inactive on exit.
        """

        active = _ACTIVE_BUDGET.get()
        if active is not None and active.owner is self and active.active:
            return await operation()

        task: asyncio.Task[T] | None = None
        acquired = False
        try:
            async with asyncio.timeout(self._timeout if timeout_seconds is None else timeout_seconds):
                await self._semaphore.acquire()
                acquired = True
                lease = _BudgetLease(owner=self)

                async def invoke() -> T:
                    return await operation()

                token = _ACTIVE_BUDGET.set(lease)
                try:
                    task = asyncio.create_task(invoke())
                finally:
                    _ACTIVE_BUDGET.reset(token)
                self._budget_tasks.add(task)

                def release_capacity(done: asyncio.Task[T]) -> None:
                    lease.active = False
                    self._semaphore.release()
                    self._budget_tasks.discard(done)
                    # A timed-out/cancelled caller no longer retrieves the
                    # detached task's exception.  Reading it here prevents an
                    # unhandled-task warning without exposing backend details.
                    with contextlib.suppress(asyncio.CancelledError):
                        done.exception()

                task.add_done_callback(release_capacity)
                # The client deadline cancels only this wait.  Shielding keeps
                # repository thread work alive under the still-held capacity
                # lease until it actually finishes.
                return await asyncio.shield(task)
        except asyncio.CancelledError:
            if acquired and task is None:
                self._semaphore.release()
            raise
        except TimeoutError:
            if acquired and task is None:
                self._semaphore.release()
            raise ServiceTimeoutError(f"{label} timed out") from None
        except BaseException:
            if acquired and task is None:
                self._semaphore.release()
            raise

    async def _bounded(self, operation: Callable[[], Awaitable[T]], *, label: str) -> T:
        async def normalized_operation() -> T:
            try:
                return await operation()
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                raise
            except QueryCanceled:
                raise ServiceTimeoutError(f"{label} timed out") from None
            except (NotFoundError, ServiceUnavailableError):
                raise
            except Exception:
                # Deliberately discard backend exception text: it can contain SQL,
                # filesystem paths, remote payloads, or the user's query.
                raise ServiceUnavailableError(f"{label} is temporarily unavailable") from None

        return await self.run_with_budget(normalized_operation, label=label)

    async def search(self, request: SearchRequest) -> SearchPage:
        page = await self._bounded(
            lambda: self.repository.search_evidence(request),
            label="search",
        )
        if page.degraded and not request.allow_degraded:
            raise ServiceUnavailableError("hybrid search is temporarily unavailable")
        return page

    async def evidence(
        self,
        evidence_id: str,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> EvidencePage:
        request = EvidenceLookupRequest(
            evidence_id=evidence_id,
            cursor=cursor,
            limit=limit,
        )
        result = await self._bounded(
            lambda: self.repository.get_evidence(
                request.evidence_id,
                cursor=request.cursor,
                limit=request.limit,
            ),
            label="evidence lookup",
        )
        if result is None:
            raise NotFoundError("evidence was not found")
        return result

    async def versions(
        self,
        issuer: Issuer,
        product_code: str,
        *,
        as_of: date | None = None,
    ) -> ProductVersions:
        return await self._bounded(
            lambda: self.repository.get_product_versions(issuer, product_code, as_of=as_of),
            label="version lookup",
        )

    async def source_pdf(self, document_id: str) -> SourcePdf:
        result = await self._bounded(
            lambda: self.repository.get_source_pdf(document_id),
            label="source lookup",
        )
        if result is None:
            raise NotFoundError("source was not found")
        return result

    async def source_page(self, document_id: str, page: int) -> SourcePage:
        result = await self._bounded(
            lambda: self.repository.get_source_page(document_id, page),
            label="source page lookup",
        )
        if result is None:
            raise NotFoundError("source page was not found")
        return result

    async def readiness(self) -> ReadinessStatus:
        async def check() -> ReadinessStatus:
            return await self.repository.readiness()

        try:
            status = await self.run_with_budget(
                check,
                label="readiness",
                timeout_seconds=min(self._timeout, 5.0),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return ReadinessStatus(
                ready=False,
                checks={"repository": False, "generation": False, "indexes": False},
            )
        return status

    async def record_auxiliary(
        self,
        operation: Callable[[], Awaitable[None]],
        *,
        label: str,
    ) -> None:
        """Bound audit/metric DB work without queuing behind a timed-out read.

        Auxiliary writes use a separate capacity pool of the same configured
        size.  This prevents audit/metric thread growth while allowing timeout
        classification to be persisted immediately even when the read lease is
        deliberately retained until abandoned backend work finishes.
        """

        task: asyncio.Task[None] | None = None
        acquired = False
        try:
            async with asyncio.timeout(self._timeout):
                await self._auxiliary_semaphore.acquire()
                acquired = True

                async def invoke() -> None:
                    await operation()

                task = asyncio.create_task(invoke())

                def release_capacity(done: asyncio.Task[None]) -> None:
                    self._auxiliary_semaphore.release()
                    self._budget_tasks.discard(done)
                    with contextlib.suppress(asyncio.CancelledError):
                        done.exception()

                self._budget_tasks.add(task)
                task.add_done_callback(release_capacity)
                await asyncio.shield(task)
        except asyncio.CancelledError:
            if acquired and task is None:
                self._auxiliary_semaphore.release()
            raise
        except TimeoutError:
            if acquired and task is None:
                self._auxiliary_semaphore.release()
            raise ServiceTimeoutError(f"{label} timed out") from None


def utc_now() -> datetime:
    return datetime.now(UTC)
