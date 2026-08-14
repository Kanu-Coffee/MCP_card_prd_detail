"""Durable PostgreSQL job lifecycle with atomic claims and fencing tokens."""

from __future__ import annotations

import json
import random
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from cardrag.db import Postgres


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    DEAD_LETTER = "dead_letter"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    id: uuid.UUID
    issuer: str
    stage: str
    document_id: str | None
    payload: dict[str, Any]
    attempt_no: int
    max_attempts: int
    fencing_token: int
    lease_until: datetime
    lease_owner: str
    generation_id: str | None = None


class LostLeaseError(RuntimeError):
    pass


class JobRepository:
    def __init__(self, database: Postgres) -> None:
        self.database = database

    def enqueue(
        self,
        *,
        issuer: str,
        stage: str,
        idempotency_key: str,
        payload: dict[str, Any],
        document_id: str | None = None,
        max_attempts: int = 5,
    ) -> tuple[uuid.UUID, bool]:
        job_id = uuid.uuid4()
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO jobs(id, issuer, stage, document_id, idempotency_key, payload, max_attempts)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING id
                """,
                (job_id, issuer, stage, document_id, idempotency_key, json.dumps(payload), max_attempts),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute("SELECT id FROM jobs WHERE idempotency_key = %s", (idempotency_key,))
                row = cursor.fetchone()
                created = False
            else:
                created = True
            connection.commit()
            if row is None:
                raise RuntimeError("idempotent job lookup returned no row")
            return row["id"], created

    def claim(self, *, worker_id: str, lease_seconds: int, issuer: str | None = None) -> ClaimedJob | None:
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                WITH candidate AS (
                    SELECT id
                    FROM jobs
                    WHERE state IN ('queued', 'retry_wait')
                      AND available_at <= now()
                      AND cancel_requested = false
                      AND NOT EXISTS (
                          SELECT 1 FROM pipeline_runs r
                          WHERE r.run_id::text = jobs.payload->>'run_id'
                            AND (
                                r.pause_requested OR r.cancel_requested
                                OR r.state NOT IN ('queued','running')
                            )
                      )
                      AND (%(issuer)s::text IS NULL OR issuer = %(issuer)s::text)
                    ORDER BY available_at, created_at, id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                ), claimed AS (
                    UPDATE jobs j
                    SET state = 'running',
                        attempt_count = attempt_count + 1,
                        lease_owner = %(worker_id)s,
                        lease_until = now() + make_interval(secs => %(lease_seconds)s),
                        fencing_token = fencing_token + 1,
                        updated_at = now()
                    FROM candidate c
                    WHERE j.id = c.id
                    RETURNING j.*
                )
                INSERT INTO job_attempts(job_id, attempt_no, fencing_token, worker_id)
                SELECT id, attempt_count, fencing_token, %(worker_id)s FROM claimed
                RETURNING job_id
                """,
                {"worker_id": worker_id, "lease_seconds": lease_seconds, "issuer": issuer},
            )
            inserted = cursor.fetchone()
            if inserted is None:
                connection.commit()
                return None
            cursor.execute("SELECT * FROM jobs WHERE id = %s", (inserted["job_id"],))
            row = cursor.fetchone()
            connection.commit()
        if row is None:
            raise RuntimeError("claimed job disappeared before it could be loaded")
        return ClaimedJob(
            id=row["id"],
            issuer=row["issuer"],
            stage=row["stage"],
            document_id=row["document_id"],
            payload=dict(row["payload"]),
            attempt_no=row["attempt_count"],
            max_attempts=row["max_attempts"],
            fencing_token=row["fencing_token"],
            lease_until=row["lease_until"],
            lease_owner=row["lease_owner"],
            generation_id=str(row["payload"].get("generation_id"))
            if row["payload"].get("generation_id")
            else None,
        )

    def heartbeat(self, claim: ClaimedJob, *, worker_id: str, lease_seconds: int) -> datetime:
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE jobs
                SET lease_until = now() + make_interval(secs => %s), updated_at = now()
                WHERE id = %s AND state = 'running' AND lease_owner = %s
                  AND fencing_token = %s AND lease_until > now()
                  AND cancel_requested = false
                RETURNING lease_until
                """,
                (lease_seconds, claim.id, worker_id, claim.fencing_token),
            )
            row = cursor.fetchone()
            connection.commit()
        if row is None:
            raise LostLeaseError("job lease was lost")
        lease_until: datetime = row["lease_until"]
        return lease_until

    def assert_current(self, claim: ClaimedJob, *, worker_id: str) -> None:
        """Fence all durable stage side effects, not only the final state change."""
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1 FROM jobs
                WHERE id=%s AND state='running' AND lease_owner=%s
                  AND fencing_token=%s AND lease_until > now() AND cancel_requested=false
                """,
                (claim.id, worker_id, claim.fencing_token),
            )
            if cursor.fetchone() is None:
                raise LostLeaseError("job lease was lost before a stage side effect")

    def enqueue_child(
        self,
        claim: ClaimedJob,
        *,
        stage: str,
        idempotency_key: str,
        payload: dict[str, Any],
        document_id: str | None,
        max_attempts: int,
    ) -> tuple[uuid.UUID, bool]:
        """Fence validation and child creation in one transaction."""
        child_id = uuid.uuid4()
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1 FROM jobs
                WHERE id=%s AND state='running' AND lease_owner=%s
                  AND fencing_token=%s AND lease_until > now() AND cancel_requested=false
                FOR UPDATE
                """,
                (claim.id, claim.lease_owner, claim.fencing_token),
            )
            if cursor.fetchone() is None:
                connection.rollback()
                raise LostLeaseError("job lease was lost before child enqueue")
            cursor.execute(
                """
                INSERT INTO jobs(id, issuer, stage, document_id, idempotency_key, payload, max_attempts)
                VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s)
                ON CONFLICT (idempotency_key) DO NOTHING RETURNING id
                """,
                (
                    child_id,
                    claim.issuer,
                    stage,
                    document_id,
                    idempotency_key,
                    json.dumps(payload),
                    max_attempts,
                ),
            )
            row = cursor.fetchone()
            created = row is not None
            if row is None:
                cursor.execute("SELECT id FROM jobs WHERE idempotency_key=%s", (idempotency_key,))
                row = cursor.fetchone()
            connection.commit()
        if row is None:
            raise RuntimeError("idempotent child job lookup returned no row")
        return row["id"], created

    def finish(
        self,
        claim: ClaimedJob,
        *,
        worker_id: str,
        provider: str | None = None,
        model: str | None = None,
        config_hash: str | None = None,
    ) -> None:
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE jobs SET state = 'succeeded', lease_owner = NULL, lease_until = NULL,
                                updated_at = now()
                WHERE id = %s AND state = 'running' AND lease_owner = %s AND fencing_token = %s
                  AND cancel_requested = false
                RETURNING id
                """,
                (claim.id, worker_id, claim.fencing_token),
            )
            if cursor.fetchone() is None:
                connection.rollback()
                raise LostLeaseError("late completion rejected by fencing token")
            cursor.execute(
                """
                UPDATE job_attempts
                SET finished_at = now(), outcome = 'succeeded', provider = %s, model = %s, config_hash = %s
                WHERE job_id = %s AND attempt_no = %s AND fencing_token = %s
                """,
                (provider, model, config_hash, claim.id, claim.attempt_no, claim.fencing_token),
            )
            connection.commit()

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
        exhausted = claim.attempt_no >= claim.max_attempts
        next_state = JobState.RETRY_WAIT if retryable and not exhausted else JobState.DEAD_LETTER
        delay = min(3600.0, base_delay_seconds * 2 ** max(0, claim.attempt_no - 1))
        delay *= random.uniform(0.75, 1.25)  # noqa: S311 - retry jitter, not security
        delay = max(delay, minimum_delay_seconds)
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE jobs
                SET state = %s, available_at = now() + make_interval(secs => %s),
                    lease_owner = NULL, lease_until = NULL, last_error_code = %s, updated_at = now()
                WHERE id = %s AND state = 'running' AND lease_owner = %s AND fencing_token = %s
                  AND cancel_requested = false
                RETURNING id
                """,
                (next_state.value, delay, error_code, claim.id, worker_id, claim.fencing_token),
            )
            if cursor.fetchone() is None:
                connection.rollback()
                raise LostLeaseError("late failure rejected by fencing token")
            cursor.execute(
                """
                UPDATE job_attempts SET finished_at = now(), outcome = %s, error_code = %s
                WHERE job_id = %s AND attempt_no = %s AND fencing_token = %s
                """,
                (next_state.value, error_code, claim.id, claim.attempt_no, claim.fencing_token),
            )
            connection.commit()
        return next_state

    def reclaim_expired(self) -> int:
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                WITH expired AS (
                    UPDATE jobs
                    SET state = CASE WHEN cancel_requested THEN 'cancelled'::job_state
                                     WHEN attempt_count >= max_attempts THEN 'dead_letter'::job_state
                                     ELSE 'retry_wait'::job_state END,
                        available_at = now(), lease_owner = NULL, lease_until = NULL,
                        last_error_code = 'lease_expired', updated_at = now()
                    WHERE state = 'running' AND lease_until <= now()
                    RETURNING id, attempt_count, fencing_token, cancel_requested
                ), closed AS (
                    UPDATE job_attempts a SET finished_at=now(),
                        outcome=CASE WHEN x.cancel_requested THEN 'cancelled' ELSE 'lease_expired' END,
                        error_code=CASE WHEN x.cancel_requested THEN 'cancel_requested' ELSE 'lease_expired' END
                    FROM expired x
                    WHERE a.job_id=x.id AND a.attempt_no=x.attempt_count
                      AND a.fencing_token=x.fencing_token AND a.finished_at IS NULL
                    RETURNING a.job_id
                )
                SELECT id FROM expired
                """
            )
            count = len(cursor.fetchall())
            connection.commit()
            return count

    def cancel(self, job_id: uuid.UUID) -> JobState:
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                WITH cancelled AS (
                    UPDATE jobs SET cancel_requested = true, state='cancelled'::job_state,
                        lease_owner=NULL, lease_until=NULL, updated_at=now()
                    WHERE id = %s AND state NOT IN ('succeeded', 'dead_letter', 'cancelled')
                    RETURNING id, attempt_count, fencing_token
                ), closed AS (
                    UPDATE job_attempts a SET finished_at=now(), outcome='cancelled',
                        error_code='cancel_requested'
                    FROM cancelled c
                    WHERE a.job_id=c.id AND a.attempt_no=c.attempt_count
                      AND a.fencing_token=c.fencing_token AND a.finished_at IS NULL
                )
                SELECT 'cancelled'::text AS state FROM cancelled
                """,
                (job_id,),
            )
            row = cursor.fetchone()
            connection.commit()
        if row is None:
            raise ValueError("job cannot be cancelled")
        return JobState(row["state"])

    def redrive(self, job_id: uuid.UUID, *, max_attempts: int | None = None) -> None:
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE jobs SET state = 'queued',
                    max_attempts = attempt_count + COALESCE(%s, max_attempts), available_at = now(),
                    lease_owner = NULL, lease_until = NULL, last_error_code = NULL,
                    cancel_requested = false, updated_at = now()
                WHERE id = %s AND state IN ('dead_letter', 'cancelled')
                RETURNING id
                """,
                (max_attempts, job_id),
            )
            if cursor.fetchone() is None:
                connection.rollback()
                raise ValueError("only dead-letter/cancelled jobs can be redriven")
            connection.commit()

    def save_checkpoint(
        self,
        claim: ClaimedJob,
        *,
        unit_key: str,
        input_hash: str,
        output_hash: str,
        artifact_uri: str,
    ) -> bool:
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1 FROM jobs
                WHERE id = %s AND state = 'running' AND lease_owner = %s
                  AND fencing_token = %s AND lease_until > now() AND cancel_requested = false
                FOR UPDATE
                """,
                (claim.id, claim.lease_owner, claim.fencing_token),
            )
            if cursor.fetchone() is None:
                connection.rollback()
                raise LostLeaseError("cannot checkpoint without current lease")
            cursor.execute(
                """
                INSERT INTO stage_checkpoints(job_id, attempt_no, unit_key, input_hash, output_hash, artifact_uri)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (job_id, attempt_no, unit_key) DO NOTHING
                RETURNING unit_key
                """,
                (claim.id, claim.attempt_no, unit_key, input_hash, output_hash, artifact_uri),
            )
            created = cursor.fetchone() is not None
            connection.commit()
            return created

    def progress(self) -> dict[str, int]:
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT state::text, count(*)::int AS count FROM jobs GROUP BY state")
            return {row["state"]: row["count"] for row in cursor.fetchall()}


def retry_after(attempt: int, *, base: float = 2.0, cap: float = 3600.0) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=min(cap, base * 2 ** max(0, attempt - 1)))
