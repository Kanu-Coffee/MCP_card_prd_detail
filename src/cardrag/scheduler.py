"""03:00 Asia/Seoul one-shot scheduler contract with issuer failure isolation."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from cardrag.db import Postgres
from cardrag.domain import Issuer
from cardrag.generation import new_generation_id
from cardrag.jobs import JobRepository
from cardrag.pipeline.chunks import CHUNK_POLICY_VERSION
from cardrag.pipeline.ocr import OCR_PROMPT_VERSION
from cardrag.pipeline.structure import STRUCTURE_SCHEMA_VERSION

SEOUL = ZoneInfo("Asia/Seoul")
ISSUER_ORDER = (Issuer.WOORI, Issuer.KB, Issuer.SHINHAN)


class _SqlCursor(Protocol):
    def execute(self, query: str, params: tuple[object, ...]) -> object: ...


def next_daily_run(now: datetime, *, at: time = time(3, 0)) -> datetime:
    local = now.astimezone(SEOUL)
    target = datetime.combine(local.date(), at, tzinfo=SEOUL)
    if target <= local:
        target += timedelta(days=1)
    return target


class DailyScheduler:
    def __init__(self, database: Postgres, jobs: JobRepository) -> None:
        self.database = database
        self.jobs = jobs

    def acquire(self, owner_id: str, *, lease_seconds: int = 3600) -> int | None:
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO scheduler_locks(schedule_name, owner_id, lease_until, fencing_token)
                VALUES ('daily', %s, now() + make_interval(secs => %s), 1)
                ON CONFLICT (schedule_name) DO UPDATE SET
                    owner_id = EXCLUDED.owner_id,
                    lease_until = EXCLUDED.lease_until,
                    fencing_token = scheduler_locks.fencing_token + 1,
                    updated_at = now()
                WHERE scheduler_locks.lease_until <= now() OR scheduler_locks.owner_id = EXCLUDED.owner_id
                RETURNING fencing_token
                """,
                (owner_id, lease_seconds),
            )
            row = cursor.fetchone()
            connection.commit()
            return int(row["fencing_token"]) if row else None

    def renew(self, owner_id: str, fencing_token: int, *, lease_seconds: int = 3600) -> bool:
        """Extend only the exact lease originally acquired by this scheduler."""
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE scheduler_locks
                SET lease_until = now() + make_interval(secs => %s), updated_at = now()
                WHERE schedule_name = 'daily' AND owner_id = %s
                  AND fencing_token = %s AND lease_until > now()
                RETURNING fencing_token
                """,
                (lease_seconds, owner_id, fencing_token),
            )
            renewed = cursor.fetchone() is not None
            connection.commit()
            return renewed

    def release(self, owner_id: str, fencing_token: int) -> bool:
        """Expire the lease without deleting its monotonically increasing fence."""
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE scheduler_locks SET lease_until = now(), updated_at = now()
                WHERE schedule_name = 'daily' AND owner_id = %s AND fencing_token = %s
                RETURNING fencing_token
                """,
                (owner_id, fencing_token),
            )
            released = cursor.fetchone() is not None
            connection.commit()
            return released

    def finish_run(self, run_id: uuid.UUID) -> str:
        """Finalize a supervised run from its issuer outcomes."""
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT state FROM pipeline_runs WHERE run_id=%s FOR UPDATE",
                (run_id,),
            )
            run = cursor.fetchone()
            if run is None:
                raise ValueError("pipeline run does not exist")
            if str(run["state"]) == "cancelled":
                self._fail_candidate(cursor, run_id, reason="run_cancelled")
                connection.commit()
                return "cancelled"
            cursor.execute(
                """
                SELECT count(*) FILTER (WHERE state='failed')::int AS failed,
                       count(*) FILTER (WHERE state='succeeded')::int AS succeeded
                FROM run_issuer_status WHERE run_id=%s
                """,
                (run_id,),
            )
            row = cursor.fetchone()
            if row is None or int(row["failed"]) + int(row["succeeded"]) != len(ISSUER_ORDER):
                raise RuntimeError("run cannot be finalized before every issuer is terminal")
            state = "failed" if row["failed"] else "succeeded"
            cursor.execute(
                """
                UPDATE pipeline_runs SET state=%s, finished_at=now(),
                    report=jsonb_build_object('issuer_succeeded', %s, 'issuer_failed', %s)
                WHERE run_id=%s AND state='running'
                RETURNING state
                """,
                (state, row["succeeded"], row["failed"], run_id),
            )
            if cursor.fetchone() is None:
                connection.rollback()
                raise RuntimeError("run is no longer active")
            if state == "failed":
                self._fail_candidate(cursor, run_id, reason="issuer_failure")
            connection.commit()
            return state

    def set_run_control(self, run_id: uuid.UUID, action: str) -> str:
        """Pause, resume, or cancel a run from the local operator CLI."""
        if action not in {"pause", "resume", "cancel"}:
            raise ValueError("invalid run control action")
        if action == "pause":
            assignment = "state='paused', pause_requested=true"
            allowed = "state='running'"
        elif action == "resume":
            assignment = "state='running', pause_requested=false"
            allowed = "state='paused'"
        else:
            assignment = "state='cancelled', cancel_requested=true, finished_at=now()"
            allowed = "state IN ('running','paused','queued')"
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE pipeline_runs SET {assignment} WHERE run_id=%s AND {allowed} RETURNING state",  # noqa: S608 - fixed internal fragments
                (run_id,),
            )
            row = cursor.fetchone()
            if row is None:
                connection.rollback()
                raise ValueError(f"run cannot transition via {action}")
            if action == "cancel":
                cursor.execute(
                    """
                    WITH cancelled AS (
                        UPDATE jobs SET cancel_requested=true, state='cancelled'::job_state,
                            lease_owner=NULL, lease_until=NULL, updated_at=now()
                        WHERE payload->>'run_id'=%s
                          AND state NOT IN ('succeeded','dead_letter','cancelled')
                        RETURNING id, attempt_count, fencing_token
                    )
                    UPDATE job_attempts a SET finished_at=now(), outcome='cancelled',
                        error_code='run_cancelled'
                    FROM cancelled c
                    WHERE a.job_id=c.id AND a.attempt_no=c.attempt_count
                      AND a.fencing_token=c.fencing_token AND a.finished_at IS NULL
                    """,
                    (str(run_id),),
                )
                cursor.execute(
                    """
                    UPDATE run_issuer_status SET state='cancelled', finished_at=COALESCE(finished_at, now())
                    WHERE run_id=%s AND state IN ('queued','running')
                    """,
                    (run_id,),
                )
                self._fail_candidate(cursor, run_id, reason="run_cancelled")
            connection.commit()
            return str(row["state"])

    @staticmethod
    def _fail_candidate(cursor: _SqlCursor, run_id: uuid.UUID, *, reason: str) -> None:
        """Move an abandoned candidate into the bounded failed-retention class."""

        cursor.execute(
            """
            UPDATE generations g SET state='failed'
            FROM pipeline_runs r
            WHERE r.run_id=%s AND g.generation_id=r.generation_id
              AND g.state IN ('building','validating')
            """,
            (run_id,),
        )
        cursor.execute(
            """
            UPDATE pipeline_runs SET report=report || jsonb_build_object(
                'generation_validation', 'failed', 'generation_reason', %s::text
            ) WHERE run_id=%s
            """,
            (reason, run_id),
        )

    def create_run(
        self,
        *,
        run_type: str = "daily",
        bulk: bool = False,
        embedding_provider: str,
        embedding_model: str,
        embedding_dimension: int,
        ocr_model: str = "gpt-5.4",
        ocr_reasoning_effort: str = "high",
        ocr_fallback_model: str = "google/gemini-2.5-pro",
        render_scale: float = 3.0,
        ocr_chunk_pages: int = 2,
        structure_schema_version: str = STRUCTURE_SCHEMA_VERSION,
        chunk_policy: str = CHUNK_POLICY_VERSION,
    ) -> tuple[uuid.UUID, str]:
        if not embedding_provider.strip() or not embedding_model.strip():
            raise ValueError("embedding provider and model must be explicit")
        if embedding_dimension <= 0:
            raise ValueError("embedding dimension must be positive")
        run_id = uuid.uuid4()
        generation_id = new_generation_id()
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO pipeline_runs(run_id, run_type, state, generation_id, started_at)
                VALUES (%s, %s, 'running', %s, now())
                """,
                (run_id, run_type, generation_id),
            )
            cursor.execute(
                """
                INSERT INTO generations(generation_id, state, manifest_sha256, root_uri, schema_version,
                                        embedding_provider, embedding_model, embedding_dimension)
                VALUES (%s, 'building', repeat('0', 64), '', 'cardrag-generation.v1',
                        %s, %s, %s)
                """,
                (generation_id, embedding_provider, embedding_model, embedding_dimension),
            )
            # Start every candidate as a complete, standalone materialization of
            # the active immutable generation. Fresh issuer snapshots are *not*
            # copied; sealing still requires a successful discovery per issuer.
            cursor.execute(
                """
                INSERT INTO generation_documents(
                    generation_id, document_id, issuer, product_code, product_name, document_type,
                    effective_date, source_version, version_sort_key, source_snapshot_id, source_url,
                    discovered_at, pdf_sha256, raw_object_key, pdf_size_bytes, pdf_page_count,
                    ocr_sha256, ocr_object_key, ocr_pages, ocr_manifest,
                    structured_sha256, structured_object_key, structure_schema_version,
                    embedding_provider, embedding_model, embedding_dimension, chunk_policy,
                    chunk_count, embedding_count, index_count, is_latest,
                    materialized_from_generation_id
                )
                SELECT %s, d.document_id, d.issuer, d.product_code, d.product_name, d.document_type,
                       d.effective_date, d.source_version, d.version_sort_key, d.source_snapshot_id,
                       d.source_url, d.discovered_at, d.pdf_sha256, d.raw_object_key,
                       d.pdf_size_bytes, d.pdf_page_count, d.ocr_sha256, d.ocr_object_key,
                       d.ocr_pages, d.ocr_manifest, d.structured_sha256, d.structured_object_key,
                       d.structure_schema_version, d.embedding_provider, d.embedding_model,
                       d.embedding_dimension, d.chunk_policy, d.chunk_count, d.embedding_count,
                       d.index_count, d.is_latest, a.generation_id
                FROM active_generation a
                JOIN generation_documents d ON d.generation_id=a.generation_id
                JOIN generations active ON active.generation_id=a.generation_id
                WHERE active.embedding_provider=%s AND active.embedding_model=%s
                  AND active.embedding_dimension=%s
                  AND d.structure_schema_version=%s AND d.chunk_policy=%s
                  AND d.ocr_manifest->'attempt'->>'prompt_version'=%s
                  AND (
                      d.ocr_manifest->'attempt'->>'provider'<>'codex-exec'
                      OR d.ocr_manifest->'attempt'->>'reasoning_effort'=%s
                  )
                  AND (d.ocr_manifest->'attempt'->>'render_scale')::double precision=%s
                  AND (d.ocr_manifest->'attempt'->>'chunk_pages')::integer=%s
                  AND (
                      (d.ocr_manifest->'attempt'->>'provider'='codex-exec'
                       AND d.ocr_manifest->'attempt'->>'model'=%s)
                      OR
                      (d.ocr_manifest->'attempt'->>'provider'='openrouter'
                       AND d.ocr_manifest->'attempt'->>'model'=%s)
                  )
                """,
                (
                    generation_id,
                    embedding_provider,
                    embedding_model,
                    embedding_dimension,
                    structure_schema_version,
                    chunk_policy,
                    OCR_PROMPT_VERSION,
                    ocr_reasoning_effort,
                    render_scale,
                    ocr_chunk_pages,
                    ocr_model,
                    ocr_fallback_model,
                ),
            )
            copied_documents = cursor.rowcount
            cursor.execute(
                """
                SELECT count(*)::int AS document_count
                FROM active_generation a
                JOIN generation_documents d ON d.generation_id=a.generation_id
                """
            )
            active_count_row = cursor.fetchone()
            active_documents = int(active_count_row["document_count"]) if active_count_row is not None else 0
            if not bulk and copied_documents != active_documents:
                connection.rollback()
                raise ValueError(
                    "processing contract changed; a BULK rebuild is required to preserve all versions"
                )
            cursor.execute(
                """
                INSERT INTO evidence(
                    generation_id, evidence_id, document_id, issuer, product_code, product_name,
                    document_type, effective_date, source_version, section_type, page_start,
                    page_end, span_start, span_end, source_spans, text, text_sha256,
                    confidence, is_latest, embedding
                )
                SELECT %s, e.evidence_id, e.document_id, e.issuer, e.product_code, e.product_name,
                       e.document_type, e.effective_date, e.source_version, e.section_type,
                       e.page_start, e.page_end, e.span_start, e.span_end, e.source_spans,
                       e.text, e.text_sha256,
                       e.confidence, e.is_latest, e.embedding
                FROM active_generation a
                JOIN evidence e ON e.generation_id=a.generation_id
                JOIN generation_documents copied
                  ON copied.generation_id=%s AND copied.document_id=e.document_id
                """,
                (generation_id, generation_id),
            )
            cursor.execute(
                """
                INSERT INTO generation_artifacts(
                    generation_id, manifest_id, artifact_id, document_id, artifact_type,
                    content_sha256, size_bytes, media_type, manifest_object_key, manifest, created_at
                )
                SELECT %s, x.manifest_id, x.artifact_id, x.document_id, x.artifact_type,
                       x.content_sha256, x.size_bytes, x.media_type, x.manifest_object_key,
                       x.manifest, x.created_at
                FROM active_generation a
                JOIN generation_artifacts x ON x.generation_id=a.generation_id
                JOIN generation_documents copied
                  ON copied.generation_id=%s AND copied.document_id=x.document_id
                """,
                (generation_id, generation_id),
            )
            for sequence, issuer in enumerate(ISSUER_ORDER, 1):
                cursor.execute(
                    "INSERT INTO run_issuer_status(run_id, issuer, sequence_no) VALUES (%s, %s, %s)",
                    (run_id, issuer.value, sequence),
                )
            connection.commit()
        return run_id, generation_id

    async def enqueue_sequence(
        self,
        run_id: uuid.UUID,
        generation_id: str,
        *,
        bulk: bool,
        inter_issuer_seconds: float = 600.0,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        wait_for_completion: Callable[[uuid.UUID, Issuer], Awaitable[None]] | None = None,
    ) -> list[uuid.UUID]:
        job_ids: list[uuid.UUID] = []
        for index, issuer in enumerate(ISSUER_ORDER):
            if self._run_is_cancelled(run_id):
                self._mark_remaining_cancelled(run_id)
                break
            job_id = self._enqueue_issuer(run_id, generation_id, issuer, bulk=bulk)
            job_ids.append(job_id)
            if wait_for_completion is None:
                await self._wait_issuer(run_id, issuer)
            else:
                await wait_for_completion(run_id, issuer)
            if self._run_is_cancelled(run_id):
                self._mark_remaining_cancelled(run_id)
                break
            if index < len(ISSUER_ORDER) - 1:
                await sleeper(inter_issuer_seconds)
        return job_ids

    def _enqueue_issuer(
        self,
        run_id: uuid.UUID,
        generation_id: str,
        issuer: Issuer,
        *,
        bulk: bool,
    ) -> uuid.UUID:
        """Idempotently enqueue one issuer and make its supervision state durable."""

        categories = ["credit", "check"] if issuer == Issuer.SHINHAN else None
        job_id, _ = self.jobs.enqueue(
            issuer=issuer.value,
            stage="discover",
            idempotency_key=f"discover:{run_id}:{issuer.value}",
            payload={
                "run_id": str(run_id),
                "generation_id": generation_id,
                "mode": "history" if bulk else "current",
                "bulk": bulk,
                "categories": categories,
            },
        )
        # Close the check/enqueue cancellation race. If cancellation committed
        # between them, fence the just-created (or idempotently found) job.
        if self._run_is_cancelled(run_id):
            with contextlib.suppress(ValueError):
                self.jobs.cancel(job_id)
            self._mark_remaining_cancelled(run_id)
            return job_id
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE run_issuer_status
                SET state='running', started_at=COALESCE(started_at, now())
                WHERE run_id=%s AND issuer=%s AND state IN ('queued','running')
                """,
                (run_id, issuer.value),
            )
            connection.commit()
        return job_id

    async def _wait_issuer(self, run_id: uuid.UUID, issuer: Issuer) -> None:
        """Wait until the issuer's entire downstream job graph is terminal."""
        while True:
            if self._run_is_cancelled(run_id):
                self._mark_remaining_cancelled(run_id)
                return
            with self.database.connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT count(*) FILTER (
                               WHERE state IN ('queued','running','retry_wait')
                           )::int AS active,
                           count(*)::int AS total,
                           count(*) FILTER (
                               WHERE state IN ('dead_letter','cancelled')
                           )::int AS failed,
                           count(DISTINCT document_id) FILTER (
                               WHERE stage='download' AND document_id IS NOT NULL
                           )::int AS discovered,
                           count(DISTINCT document_id) FILTER (
                               WHERE stage IN ('index','materialize')
                                 AND state='succeeded' AND document_id IS NOT NULL
                           )::int AS succeeded
                    FROM jobs
                    WHERE issuer=%s AND payload->>'run_id'=%s
                    """,
                    (issuer.value, str(run_id)),
                )
                row = cursor.fetchone()
            if row is None:
                raise RuntimeError("issuer run status query returned no row")
            if int(row["total"]) == 0:
                raise RuntimeError("issuer cannot complete before its discovery job is enqueued")
            active = row["active"]
            if active == 0:
                with self.database.connection() as connection, connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE run_issuer_status
                        SET state=%s, finished_at=now(), failed_count=%s,
                            discovered_count=%s, succeeded_count=%s
                        WHERE run_id=%s AND issuer=%s
                        """,
                        (
                            "failed" if row["failed"] else "succeeded",
                            row["failed"],
                            row["discovered"],
                            row["succeeded"],
                            run_id,
                            issuer.value,
                        ),
                    )
                    connection.commit()
                return
            await asyncio.sleep(5.0)

    async def wait_existing_run(
        self,
        run_id: uuid.UUID,
        *,
        inter_issuer_seconds: float = 600.0,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Resume both enqueueing and supervision of a partially started run."""

        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT state, generation_id, run_type FROM pipeline_runs WHERE run_id=%s",
                (run_id,),
            )
            run = cursor.fetchone()
        if run is None:
            raise ValueError("pipeline run does not exist")
        generation_id = str(run["generation_id"] or "")
        if not generation_id:
            raise ValueError("pipeline run has no candidate generation")
        bulk = str(run["run_type"]) == "bulk"

        for index, issuer in enumerate(ISSUER_ORDER):
            if self._run_is_cancelled(run_id):
                self._mark_remaining_cancelled(run_id)
                return
            with self.database.connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT s.state, EXISTS (
                        SELECT 1 FROM jobs j
                        WHERE j.issuer=%s AND j.stage='discover'
                          AND j.payload->>'run_id'=%s
                    ) AS discovery_enqueued
                    FROM run_issuer_status s
                    WHERE s.run_id=%s AND s.issuer=%s
                    """,
                    (issuer.value, str(run_id), run_id, issuer.value),
                )
                row = cursor.fetchone()
            if row is None:
                raise ValueError("pipeline run issuer status is missing")
            if str(row["state"]) in {"succeeded", "failed", "cancelled"}:
                continue
            if not bool(row["discovery_enqueued"]):
                # A production wait-mode supervisor sleeps after each completed
                # issuer. On recovery, conservatively replay the full delay
                # before a missing later issuer; an extra delay is safe, while
                # shortening it would violate the issuer politeness contract.
                if index:
                    await sleeper(inter_issuer_seconds)
                if self._run_is_cancelled(run_id):
                    self._mark_remaining_cancelled(run_id)
                    return
                self._enqueue_issuer(run_id, generation_id, issuer, bulk=bulk)
            elif str(row["state"]) == "queued":
                # The process may have died after the idempotent job insert but
                # before the status update. Replaying the helper closes that
                # narrow transaction boundary without duplicating the job.
                self._enqueue_issuer(run_id, generation_id, issuer, bulk=bulk)
            await self._wait_issuer(run_id, issuer)

    def _run_is_cancelled(self, run_id: uuid.UUID) -> bool:
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT state FROM pipeline_runs WHERE run_id=%s", (run_id,))
            row = cursor.fetchone()
        if row is None:
            raise ValueError("pipeline run does not exist")
        return str(row["state"]) == "cancelled"

    def _mark_remaining_cancelled(self, run_id: uuid.UUID) -> None:
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE run_issuer_status SET state='cancelled', finished_at=COALESCE(finished_at, now())
                WHERE run_id=%s AND state IN ('queued','running')
                """,
                (run_id,),
            )
            connection.commit()
