from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

import pytest

from cardrag.db import Postgres
from cardrag.domain import Issuer
from cardrag.jobs import JobRepository
from cardrag.scheduler import DailyScheduler

pytestmark = pytest.mark.integration


async def test_wait_existing_run_reenqueues_missing_issuers_after_first_enqueue_crash(
    clean_database: Postgres,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs = JobRepository(clean_database)
    scheduler = DailyScheduler(clean_database, jobs)
    run_id, generation_id = scheduler.create_run(
        run_type="daily",
        bulk=False,
        embedding_provider="openrouter",
        embedding_model="fixture-embedding-v1",
        embedding_dimension=1536,
    )

    # Reproduce a wait-mode supervisor crash immediately after the first
    # issuer was durably enqueued. KB and Shinhan remain status=queued and have
    # no jobs at all.
    jobs.enqueue(
        issuer="woori",
        stage="discover",
        idempotency_key=f"discover:{run_id}:woori",
        payload={
            "run_id": str(run_id),
            "generation_id": generation_id,
            "mode": "current",
            "bulk": False,
            "categories": None,
        },
    )
    with clean_database.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE run_issuer_status SET state='running', started_at=now()
            WHERE run_id=%s AND issuer='woori'
            """,
            (run_id,),
        )
        connection.commit()

    original_wait = scheduler._wait_issuer
    supervised: list[str] = []

    async def complete_fixture_graph(target_run_id: uuid.UUID, issuer: Issuer) -> None:
        with clean_database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT count(*)::int AS n FROM jobs
                WHERE issuer=%s AND stage='discover' AND payload->>'run_id'=%s
                """,
                (issuer.value, str(target_run_id)),
            )
            assert cursor.fetchone() == {"n": 1}
            cursor.execute(
                """
                UPDATE jobs SET state='succeeded', updated_at=now()
                WHERE issuer=%s AND stage='discover' AND payload->>'run_id'=%s
                """,
                (issuer.value, str(target_run_id)),
            )
            connection.commit()
        supervised.append(issuer.value)
        await original_wait(target_run_id, issuer)

    monkeypatch.setattr(scheduler, "_wait_issuer", complete_fixture_graph)
    delays: list[float] = []

    async def record_delay(seconds: float) -> None:
        delays.append(seconds)

    await scheduler.wait_existing_run(
        run_id,
        inter_issuer_seconds=600,
        sleeper=record_delay,
    )

    assert supervised == ["woori", "kb", "shinhan"]
    assert delays == [600, 600]
    with clean_database.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT issuer, payload->>'mode' AS mode, payload->'categories' AS categories
            FROM jobs WHERE stage='discover' AND payload->>'run_id'=%s
            ORDER BY CASE issuer WHEN 'woori' THEN 1 WHEN 'kb' THEN 2 ELSE 3 END
            """,
            (str(run_id),),
        )
        discovery = cursor.fetchall()
        cursor.execute(
            "SELECT issuer, state FROM run_issuer_status WHERE run_id=%s ORDER BY sequence_no",
            (run_id,),
        )
        statuses = cursor.fetchall()
    assert discovery == [
        {"issuer": "woori", "mode": "current", "categories": None},
        {"issuer": "kb", "mode": "current", "categories": None},
        {"issuer": "shinhan", "mode": "current", "categories": ["credit", "check"]},
    ]
    assert statuses == [
        {"issuer": "woori", "state": "succeeded"},
        {"issuer": "kb", "state": "succeeded"},
        {"issuer": "shinhan", "state": "succeeded"},
    ]
    assert scheduler.finish_run(run_id) == "succeeded"

    # Finalize/recovery is idempotent: terminal issuer rows do not enqueue a
    # second discovery graph or replay the delay.
    second_delays: list[float] = []
    sleeper: Callable[[float], Awaitable[None]]

    async def record_second_delay(seconds: float) -> None:
        second_delays.append(seconds)

    sleeper = record_second_delay
    await scheduler.wait_existing_run(run_id, inter_issuer_seconds=600, sleeper=sleeper)
    assert second_delays == []
    with clean_database.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*)::int AS n FROM jobs WHERE stage='discover' AND payload->>'run_id'=%s",
            (str(run_id),),
        )
        assert cursor.fetchone() == {"n": 3}


async def test_cancelled_job_fails_issuer_run_and_candidate(clean_database: Postgres) -> None:
    jobs = JobRepository(clean_database)
    scheduler = DailyScheduler(clean_database, jobs)
    run_id, generation_id = scheduler.create_run(
        run_type="daily",
        bulk=False,
        embedding_provider="openrouter",
        embedding_model="fixture-embedding-v1",
        embedding_dimension=1536,
    )
    job_id, _ = jobs.enqueue(
        issuer="woori",
        stage="discover",
        idempotency_key=f"discover:{run_id}:woori",
        payload={
            "run_id": str(run_id),
            "generation_id": generation_id,
            "mode": "current",
            "bulk": False,
            "categories": None,
        },
    )
    with clean_database.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE run_issuer_status SET state='running', started_at=now()
            WHERE run_id=%s AND issuer='woori'
            """,
            (run_id,),
        )
        connection.commit()

    assert jobs.cancel(job_id).value == "cancelled"
    await scheduler._wait_issuer(run_id, Issuer.WOORI)

    with clean_database.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT state, failed_count FROM run_issuer_status WHERE run_id=%s AND issuer='woori'",
            (run_id,),
        )
        assert cursor.fetchone() == {"state": "failed", "failed_count": 1}
        cursor.execute(
            """
            UPDATE run_issuer_status SET state='succeeded', finished_at=now()
            WHERE run_id=%s AND issuer IN ('kb','shinhan')
            """,
            (run_id,),
        )
        connection.commit()

    assert scheduler.finish_run(run_id) == "failed"
    with clean_database.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT state FROM generations WHERE generation_id=%s",
            (generation_id,),
        )
        assert cursor.fetchone() == {"state": "failed"}
        cursor.execute("SELECT state, report FROM pipeline_runs WHERE run_id=%s", (run_id,))
        run = cursor.fetchone()
    assert run is not None
    assert run["state"] == "failed"
    assert run["report"]["generation_reason"] == "issuer_failure"
