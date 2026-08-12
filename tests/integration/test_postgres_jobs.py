from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest

from cardrag.db import Postgres
from cardrag.jobs import JobRepository, JobState, LostLeaseError
from cardrag.scheduler import DailyScheduler

pytestmark = pytest.mark.integration


def test_concurrent_workers_claim_each_job_exactly_once(clean_database: Postgres) -> None:
    repository = JobRepository(clean_database)
    expected: set[UUID] = set()
    for index in range(40):
        job_id, created = repository.enqueue(
            issuer="woori",
            stage="fixture",
            idempotency_key=f"fixture:{index}",
            payload={"index": index},
        )
        assert created
        expected.add(job_id)

    def drain(worker: int) -> list[UUID]:
        claimed: list[UUID] = []
        while claim := repository.claim(worker_id=f"worker-{worker}", lease_seconds=30):
            claimed.append(claim.id)
            repository.finish(claim, worker_id=f"worker-{worker}")
        return claimed

    with ThreadPoolExecutor(max_workers=8) as pool:
        groups = list(pool.map(drain, range(8)))
    all_claims = [job_id for group in groups for job_id in group]
    assert set(all_claims) == expected
    assert len(all_claims) == len(expected)
    assert repository.progress() == {"succeeded": 40}


def test_lease_reclaim_rejects_late_worker_fencing_token(clean_database: Postgres) -> None:
    repository = JobRepository(clean_database)
    repository.enqueue(
        issuer="kb",
        stage="fixture",
        idempotency_key="fixture:lease",
        payload={},
        max_attempts=3,
    )
    first = repository.claim(worker_id="old", lease_seconds=30)
    assert first is not None
    with clean_database.connection() as connection, connection.cursor() as cursor:
        cursor.execute("UPDATE jobs SET lease_until = now() - interval '1 second' WHERE id = %s", (first.id,))
        connection.commit()
    assert repository.reclaim_expired() == 1
    with clean_database.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT finished_at IS NOT NULL AS finished, outcome, error_code "
            "FROM job_attempts WHERE job_id=%s AND attempt_no=1",
            (first.id,),
        )
        expired_attempt = cursor.fetchone()
    assert expired_attempt == {
        "finished": True,
        "outcome": "lease_expired",
        "error_code": "lease_expired",
    }
    second = repository.claim(worker_id="new", lease_seconds=30)
    assert second is not None
    assert second.id == first.id
    assert second.fencing_token > first.fencing_token
    with pytest.raises(LostLeaseError, match="fencing"):
        repository.finish(first, worker_id="old")
    repository.finish(second, worker_id="new")


def test_idempotency_cancel_dead_letter_and_redrive(clean_database: Postgres) -> None:
    repository = JobRepository(clean_database)
    job_id, created = repository.enqueue(
        issuer="shinhan",
        stage="fixture",
        idempotency_key="same-result",
        payload={"safe": True},
        max_attempts=1,
    )
    duplicate_id, duplicate_created = repository.enqueue(
        issuer="shinhan",
        stage="fixture",
        idempotency_key="same-result",
        payload={"different": "ignored"},
        max_attempts=10,
    )
    assert duplicate_id == job_id
    assert not duplicate_created
    claim = repository.claim(worker_id="one", lease_seconds=30)
    assert claim is not None
    assert repository.fail(
        claim,
        worker_id="one",
        error_code="fixture_permanent",
        retryable=True,
    ) == JobState.DEAD_LETTER
    repository.redrive(job_id, max_attempts=2)
    redriven = repository.claim(worker_id="two", lease_seconds=30)
    assert redriven is not None
    repository.finish(redriven, worker_id="two")


def test_checkpoint_is_idempotent_and_requires_current_fence(clean_database: Postgres) -> None:
    repository = JobRepository(clean_database)
    repository.enqueue(
        issuer="woori",
        stage="ocr",
        idempotency_key="ocr:checkpoint",
        payload={},
    )
    claim = repository.claim(worker_id="ocr", lease_seconds=30)
    assert claim is not None
    assert repository.save_checkpoint(
        claim,
        unit_key="page:1",
        input_hash="a" * 64,
        output_hash="b" * 64,
        artifact_uri="sha256/bb/" + "b" * 64,
    )
    assert not repository.save_checkpoint(
        claim,
        unit_key="page:1",
        input_hash="a" * 64,
        output_hash="b" * 64,
        artifact_uri="sha256/bb/" + "b" * 64,
    )
    repository.finish(claim, worker_id="ocr")
    with pytest.raises(LostLeaseError):
        repository.save_checkpoint(
            claim,
            unit_key="page:2",
            input_hash="a" * 64,
            output_hash="c" * 64,
            artifact_uri="sha256/cc/" + "c" * 64,
        )


def test_heartbeat_loses_authority_immediately_after_cancellation(clean_database: Postgres) -> None:
    repository = JobRepository(clean_database)
    job_id, _ = repository.enqueue(
        issuer="woori",
        stage="ocr",
        idempotency_key="ocr:cancel-heartbeat",
        payload={},
    )
    claim = repository.claim(worker_id="ocr", lease_seconds=30)
    assert claim is not None
    assert repository.cancel(job_id) == JobState.CANCELLED

    with pytest.raises(LostLeaseError, match="lease was lost"):
        repository.heartbeat(claim, worker_id="ocr", lease_seconds=30)
    with clean_database.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT state::text, lease_owner, lease_until FROM jobs WHERE id=%s",
            (job_id,),
        )
        job = cursor.fetchone()
        cursor.execute(
            "SELECT outcome, error_code, finished_at IS NOT NULL AS finished "
            "FROM job_attempts WHERE job_id=%s AND attempt_no=1",
            (job_id,),
        )
        attempt = cursor.fetchone()
    assert job == {"state": "cancelled", "lease_owner": None, "lease_until": None}
    assert attempt == {
        "outcome": "cancelled",
        "error_code": "cancel_requested",
        "finished": True,
    }


def test_scheduler_lease_renews_releases_and_rejects_stale_fence(clean_database: Postgres) -> None:
    jobs = JobRepository(clean_database)
    scheduler = DailyScheduler(clean_database, jobs)
    first = scheduler.acquire("scheduler-a", lease_seconds=60)
    assert first is not None
    assert scheduler.acquire("scheduler-b", lease_seconds=60) is None
    assert scheduler.renew("scheduler-a", first, lease_seconds=60)
    assert not scheduler.renew("scheduler-a", first + 1, lease_seconds=60)
    assert scheduler.release("scheduler-a", first)
    second = scheduler.acquire("scheduler-b", lease_seconds=60)
    assert second is not None and second > first


def test_paused_run_jobs_are_not_claimed_then_resume_and_cancel(clean_database: Postgres) -> None:
    jobs = JobRepository(clean_database)
    scheduler = DailyScheduler(clean_database, jobs)
    run_id, generation_id = scheduler.create_run(
        run_type="bulk",
        bulk=True,
        embedding_provider="openrouter",
        embedding_model="fixture-embedding-v1",
        embedding_dimension=1536,
    )
    job_id, _ = jobs.enqueue(
        issuer="woori",
        stage="fixture",
        idempotency_key=f"fixture:{run_id}",
        payload={"run_id": str(run_id)},
    )
    assert scheduler.set_run_control(run_id, "pause") == "paused"
    assert jobs.claim(worker_id="worker", lease_seconds=30) is None
    assert scheduler.set_run_control(run_id, "resume") == "running"
    claim = jobs.claim(worker_id="worker", lease_seconds=30)
    assert claim is not None and claim.id == job_id
    jobs.fail(claim, worker_id="worker", error_code="fixture", retryable=True, base_delay_seconds=0)
    assert scheduler.set_run_control(run_id, "cancel") == "cancelled"
    assert jobs.claim(worker_id="worker", lease_seconds=30) is None
    with clean_database.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT state FROM generations WHERE generation_id=%s",
            (generation_id,),
        )
        assert cursor.fetchone()["state"] == "failed"
