from __future__ import annotations

import uuid
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self

from cardrag.domain import Issuer
from cardrag.scheduler import DailyScheduler, next_daily_run


class _Cursor(AbstractContextManager["_Cursor"]):
    def __init__(self, database: _Database) -> None:
        self.database = database
        self.row: dict[str, object] | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback

    def execute(self, query: str, params: tuple[object, ...]) -> None:
        normalized = " ".join(query.split())
        self.database.queries.append(normalized)
        if normalized.startswith("SELECT state FROM pipeline_runs"):
            self.row = {"state": self.database.run_state}
            return
        if normalized.startswith("SELECT count"):
            issuer = str(params[0])
            self.row = (
                {"active": 0, "total": 2, "failed": 1, "discovered": 2, "succeeded": 1}
                if issuer == Issuer.WOORI.value
                else {"active": 0, "total": 2, "failed": 0, "discovered": 2, "succeeded": 2}
            )
            return
        if normalized.startswith("UPDATE run_issuer_status"):
            if "SET state='running'" in normalized:
                _run_id, issuer = params
                self.database.status_updates.append((str(issuer), "running", 0))
            elif "SET state='cancelled'" in normalized:
                self.database.status_updates.append(("remaining", "cancelled", 0))
            else:
                state, failed_count, discovered, succeeded, _run_id, issuer = params
                self.database.status_updates.append((str(issuer), str(state), int(failed_count)))
                self.database.accounting_updates.append((str(issuer), int(discovered), int(succeeded)))
            return
        raise AssertionError(f"unexpected scheduler SQL: {normalized}")

    def fetchone(self) -> dict[str, object] | None:
        return self.row


class _Connection(AbstractContextManager["_Connection"]):
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
        self.status_updates: list[tuple[str, str, int]] = []
        self.accounting_updates: list[tuple[str, int, int]] = []
        self.queries: list[str] = []
        self.commits = 0
        self.run_state = "running"

    def connection(self) -> _Connection:
        return _Connection(self)


class _Jobs:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def enqueue(self, **kwargs: Any) -> tuple[uuid.UUID, bool]:
        self.calls.append(kwargs)
        return uuid.uuid5(uuid.NAMESPACE_URL, str(kwargs["idempotency_key"])), True

    def cancel(self, job_id: uuid.UUID) -> object:
        del job_id
        return None


def test_next_daily_run_uses_kst_and_a_supplied_fake_clock() -> None:
    before = datetime(2026, 8, 11, 17, 59, tzinfo=UTC)  # 02:59 KST
    exactly = datetime(2026, 8, 11, 18, 0, tzinfo=UTC)  # 03:00 KST

    first = next_daily_run(before)
    second = next_daily_run(exactly)

    assert first.isoformat() == "2026-08-12T03:00:00+09:00"
    assert second.isoformat() == "2026-08-13T03:00:00+09:00"


async def test_sequence_is_woori_kb_shinhan_with_ten_minute_gaps_and_failure_isolation() -> None:
    database = _Database()
    jobs = _Jobs()
    scheduler = DailyScheduler(database, jobs)  # type: ignore[arg-type]
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    run_id = uuid.uuid4()
    job_ids = await scheduler.enqueue_sequence(
        run_id,
        "generation-fixture",
        bulk=True,
        inter_issuer_seconds=600,
        sleeper=fake_sleep,
    )

    assert len(job_ids) == 3
    assert [call["issuer"] for call in jobs.calls] == ["woori", "kb", "shinhan"]
    assert [call["payload"]["mode"] for call in jobs.calls] == ["history", "history", "history"]
    assert [call["payload"]["categories"] for call in jobs.calls] == [None, None, ["credit", "check"]]
    assert sleeps == [600, 600]
    assert database.status_updates == [
        ("woori", "running", 0),
        ("woori", "failed", 1),
        ("kb", "running", 0),
        ("kb", "succeeded", 0),
        ("shinhan", "running", 0),
        ("shinhan", "succeeded", 0),
    ]
    # Each issuer records a running transition and one terminal transition.
    assert database.commits == 6
    assert database.accounting_updates == [
        ("woori", 2, 1),
        ("kb", 2, 2),
        ("shinhan", 2, 2),
    ]
    assert any("state IN ('dead_letter','cancelled')" in query for query in database.queries)


async def test_sequence_stops_before_next_issuer_when_run_is_cancelled() -> None:
    database = _Database()
    jobs = _Jobs()
    scheduler = DailyScheduler(database, jobs)  # type: ignore[arg-type]

    async def cancel_after_first(_: uuid.UUID, __: Issuer) -> None:
        database.run_state = "cancelled"

    job_ids = await scheduler.enqueue_sequence(
        uuid.uuid4(),
        "generation-fixture",
        bulk=False,
        inter_issuer_seconds=600,
        wait_for_completion=cancel_after_first,
    )

    assert len(job_ids) == 1
    assert [call["issuer"] for call in jobs.calls] == ["woori"]
    assert ("remaining", "cancelled", 0) in database.status_updates
