"""Resumable database/CAS import of a verified legacy bundle."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cardrag.db import Postgres
from cardrag.domain import (
    ArtifactManifest,
    ArtifactType,
    Issuer,
    Lineage,
    ManifestAttribute,
    SourceRecord,
    canonical_sha256,
)
from cardrag.jobs import JobRepository
from cardrag.pipeline.ocr import split_pages
from cardrag.scheduler import DailyScheduler
from cardrag.storage import ContentAddressedObjectStore

from .adoption import legacy_adoption_manifest
from .bundle import (
    LegacyBundleDocument,
    LegacyBundleManifest,
    inspect_bundle_control,
    load_bundle_documents,
    verify_bundle,
)


class LegacyImportStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    import_id: uuid.UUID
    bundle_id: str
    bundle_sha256: str
    run_id: uuid.UUID
    generation_id: str
    state: Literal[
        "preparing",
        "processing",
        "ready_to_finalize",
        "finalizing",
        "succeeded",
        "failed",
        "cancelled",
    ]
    phase: str
    last_error_code: str | None
    no_publish: bool
    total: int
    adopted: int
    reocr: int
    pending: int
    queued: int
    processing: int
    succeeded: int
    failed: int
    processed_bytes: int
    verification_files: int = Field(default=0, ge=0)
    verification_total_files: int = Field(default=0, ge=0)
    verification_bytes: int = Field(default=0, ge=0)
    verification_total_bytes: int = Field(default=0, ge=0)
    verification_bytes_per_second: float = Field(default=0.0, ge=0)
    documents_per_second: float
    eta_seconds: float | None
    active_jobs: int
    terminal_job_failures: int


Finalizer = Callable[[uuid.UUID, str], tuple[str, str]]


class LegacyImportService:
    """Seed legacy bytes and current processing jobs behind a publish boundary."""

    def __init__(
        self,
        database: Postgres,
        jobs: JobRepository,
        objects: ContentAddressedObjectStore,
        scheduler: DailyScheduler,
        *,
        embedding_model: str,
        embedding_dimension: int,
        ocr_model: str,
        ocr_reasoning_effort: str,
        ocr_fallback_model: str,
        render_scale: float,
        ocr_chunk_pages: int,
        max_job_attempts: int = 5,
    ) -> None:
        self.database = database
        self.jobs = jobs
        self.objects = objects
        self.scheduler = scheduler
        self.embedding_model = embedding_model
        self.embedding_dimension = embedding_dimension
        self.ocr_model = ocr_model
        self.ocr_reasoning_effort = ocr_reasoning_effort
        self.ocr_fallback_model = ocr_fallback_model
        self.render_scale = render_scale
        self.ocr_chunk_pages = ocr_chunk_pages
        self.max_job_attempts = max_job_attempts

    def start(
        self,
        bundle_root: Path,
        *,
        no_publish: bool = True,
        created: Callable[[LegacyImportStatus], None] | None = None,
        progress: Callable[[LegacyImportStatus], None] | None = None,
    ) -> LegacyImportStatus:
        # Control files are small and READY-bound.  Reserve the run/import IDs
        # from that immutable identity first so Portainer receives identifiers
        # before the potentially long full payload hash verification.
        bundle_identity = inspect_bundle_control(bundle_root)
        import_id: uuid.UUID
        run_id: uuid.UUID
        generation_id: str
        with self.database.connection() as connection, connection.cursor() as cursor:
            lock_name = f"cardrag-legacy-import:{bundle_identity.content_sha256}"
            cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lock_name,))
            try:
                cursor.execute(
                    """
                    SELECT import_id, run_id, generation_id FROM legacy_imports
                    WHERE bundle_sha256=%s
                      AND state IN ('preparing','processing','ready_to_finalize','finalizing')
                    FOR UPDATE
                    """,
                    (bundle_identity.content_sha256,),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    import_id = uuid.UUID(str(existing["import_id"]))
                    run_id = uuid.UUID(str(existing["run_id"]))
                    generation_id = str(existing["generation_id"])
                else:
                    run_id, generation_id = self.scheduler.create_run(
                        run_type="legacy_import",
                        bulk=True,
                        embedding_provider="openrouter",
                        embedding_model=self.embedding_model,
                        embedding_dimension=self.embedding_dimension,
                        ocr_model=self.ocr_model,
                        ocr_reasoning_effort=self.ocr_reasoning_effort,
                        ocr_fallback_model=self.ocr_fallback_model,
                        render_scale=self.render_scale,
                        ocr_chunk_pages=self.ocr_chunk_pages,
                        transaction_connection=connection,
                    )
                    import_id = uuid.uuid4()
                    cursor.execute(
                        """
                        INSERT INTO legacy_imports(
                            import_id, bundle_id, bundle_sha256, run_id, generation_id,
                            state, phase, no_publish, report
                        ) VALUES (%s,%s,%s,%s,%s,'preparing','verify_bundle',%s,%s::jsonb)
                        """,
                        (
                            import_id,
                            bundle_identity.bundle_id,
                            bundle_identity.content_sha256,
                            run_id,
                            generation_id,
                            no_publish,
                            json.dumps(
                                {
                                    "adopted": bundle_identity.adopted_count,
                                    "documents": bundle_identity.document_count,
                                    "payload_bytes": bundle_identity.payload_bytes,
                                    "reocr": bundle_identity.reocr_count,
                                    "seeded_bytes": 0,
                                }
                            ),
                        ),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        if created is not None:
            created(self.status(import_id))
        try:
            bundle = verify_bundle(
                bundle_root,
                progress=self._bundle_verification_progress(import_id, progress),
            )
            if bundle.content_sha256 != bundle_identity.content_sha256:
                raise ValueError("bundle identity changed during verification")
            with self._advisory_lock(f"cardrag-legacy-resume:{import_id}"):
                self._resume_locked(
                    import_id,
                    bundle_root,
                    progress=progress,
                    verified_bundle=bundle,
                )
        except Exception as exc:
            self._mark_failed(import_id, type(exc).__name__)
            raise
        return self.status(import_id)

    def resume(
        self,
        import_id: uuid.UUID,
        bundle_root: Path,
        *,
        progress: Callable[[LegacyImportStatus], None] | None = None,
    ) -> LegacyImportStatus:
        with self._advisory_lock(f"cardrag-legacy-resume:{import_id}"):
            return self._resume_locked(import_id, bundle_root, progress=progress)

    def _resume_locked(
        self,
        import_id: uuid.UUID,
        bundle_root: Path,
        *,
        progress: Callable[[LegacyImportStatus], None] | None,
        verified_bundle: LegacyBundleManifest | None = None,
    ) -> LegacyImportStatus:
        bundle = verified_bundle or verify_bundle(
            bundle_root,
            progress=self._bundle_verification_progress(import_id, progress),
        )
        documents = load_bundle_documents(bundle_root, manifest=bundle)
        import_row = self._load_import(import_id)
        if import_row["bundle_sha256"] != bundle.content_sha256:
            raise ValueError("selected bundle does not match the import ledger")
        if str(import_row["state"]) in {"succeeded", "cancelled"}:
            return self.status(import_id)
        if str(import_row["state"]) == "failed":
            with self.database.connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE legacy_imports SET state='preparing', phase='resume',
                        attempt=attempt+1, last_error_code=NULL, finished_at=NULL, updated_at=now()
                    WHERE import_id=%s
                    """,
                    (import_id,),
                )
                cursor.execute(
                    """
                    UPDATE pipeline_runs SET state='running', cancel_requested=false,
                        pause_requested=false, finished_at=NULL WHERE run_id=%s AND state='failed'
                    """,
                    (import_row["run_id"],),
                )
                cursor.execute(
                    """
                    UPDATE generations SET state='building'
                    WHERE generation_id=%s AND state='failed'
                    """,
                    (import_row["generation_id"],),
                )
                connection.commit()
            self._redrive_failed_chains(import_id)
        if self._cancel_requested(import_id):
            return self.status(import_id)
        self._ensure_legacy_snapshots(bundle, documents)
        self._set_phase(import_id, "seed_documents", state="processing")
        for index, document in enumerate(documents, 1):
            if self._cancel_requested(import_id):
                return self.status(import_id)
            if self._document_has_live_or_successful_chain(import_id, document.document_id):
                continue
            self._seed_document(import_id, bundle_root, bundle, document)
            if progress is not None and index % 100 == 0:
                progress(self.status(import_id))
        if self._cancel_requested(import_id):
            return self.status(import_id)
        self._set_phase(import_id, "live_reconciliation", state="processing")
        self._enqueue_reconciliation(import_id)
        return self.refresh(import_id)

    def refresh(self, import_id: uuid.UUID) -> LegacyImportStatus:
        row = self._load_import(import_id)
        if str(row["state"]) not in {"processing", "preparing"}:
            return self.status(import_id)
        run_id = uuid.UUID(str(row["run_id"]))
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE legacy_import_documents d SET
                    state=CASE
                        WHEN EXISTS (
                            SELECT 1 FROM jobs failed
                            WHERE failed.payload->>'legacy_import_id'=%s
                              AND failed.document_id=d.document_id
                              AND failed.state IN ('dead_letter','cancelled')
                        ) THEN 'failed'
                        WHEN EXISTS (
                            SELECT 1 FROM jobs terminal
                            WHERE terminal.payload->>'legacy_import_id'=%s
                              AND terminal.document_id=d.document_id
                              AND terminal.stage='index' AND terminal.state='succeeded'
                        ) THEN 'succeeded'
                        WHEN EXISTS (
                            SELECT 1 FROM jobs active
                            WHERE active.payload->>'legacy_import_id'=%s
                              AND active.document_id=d.document_id
                              AND active.state IN ('running','retry_wait')
                        ) THEN 'processing'
                        ELSE d.state
                    END,
                    error_code=CASE WHEN EXISTS (
                        SELECT 1 FROM jobs failed
                        WHERE failed.payload->>'legacy_import_id'=%s
                          AND failed.document_id=d.document_id
                          AND failed.state IN ('dead_letter','cancelled')
                    ) THEN 'terminal_job_failure' ELSE d.error_code END,
                    updated_at=now()
                WHERE d.import_id=%s
                """,
                (str(import_id), str(import_id), str(import_id), str(import_id), import_id),
            )
            cursor.execute(
                """
                SELECT count(*)::int AS total,
                       count(*) FILTER (WHERE state IN ('queued','running','retry_wait'))::int AS active,
                       count(*) FILTER (WHERE state IN ('dead_letter','cancelled'))::int AS failed
                FROM jobs WHERE payload->>'run_id'=%s
                """,
                (str(run_id),),
            )
            totals = cursor.fetchone()
            connection.commit()
            if totals is None or int(totals["total"]) == 0 or int(totals["active"]) > 0:
                return self.status(import_id)
            for issuer in (Issuer.WOORI, Issuer.KB, Issuer.SHINHAN):
                cursor.execute(
                    """
                    SELECT count(*) FILTER (WHERE state IN ('dead_letter','cancelled'))::int AS failed,
                           count(DISTINCT document_id) FILTER (
                               WHERE stage='download' AND document_id IS NOT NULL
                           )::int AS discovered,
                           count(DISTINCT document_id) FILTER (
                               WHERE stage IN ('index','materialize') AND state='succeeded'
                                 AND document_id IS NOT NULL
                           )::int AS succeeded
                    FROM jobs WHERE issuer=%s AND payload->>'run_id'=%s
                    """,
                    (issuer.value, str(run_id)),
                )
                issuer_row = cursor.fetchone()
                issuer_failed = int(issuer_row["failed"]) if issuer_row is not None else 1
                cursor.execute(
                    """
                    UPDATE run_issuer_status SET state=%s, finished_at=now(),
                        failed_count=%s, discovered_count=%s, succeeded_count=%s
                    WHERE run_id=%s AND issuer=%s
                    """,
                    (
                        "failed" if issuer_failed else "succeeded",
                        issuer_failed,
                        int(issuer_row["discovered"]) if issuer_row is not None else 0,
                        int(issuer_row["succeeded"]) if issuer_row is not None else 0,
                        run_id,
                        issuer.value,
                    ),
                )
            connection.commit()
        run_state = self.scheduler.finish_run(run_id)
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE legacy_imports SET state=%s, phase=%s, updated_at=now(),
                    last_error_code=%s, finished_at=CASE WHEN %s='failed' THEN now() ELSE NULL END
                WHERE import_id=%s
                """,
                (
                    "ready_to_finalize" if run_state == "succeeded" else "failed",
                    "awaiting_finalize" if run_state == "succeeded" else "processing_failed",
                    None if run_state == "succeeded" else "terminal_job_failure",
                    run_state,
                    import_id,
                ),
            )
            connection.commit()
        return self.status(import_id)

    def wait(
        self,
        import_id: uuid.UUID,
        *,
        poll_seconds: float = 5.0,
        progress: Callable[[LegacyImportStatus], None] | None = None,
    ) -> LegacyImportStatus:
        while True:
            status = self.refresh(import_id)
            if progress is not None:
                progress(status)
            if status.state in {"ready_to_finalize", "succeeded", "failed", "cancelled"}:
                return status
            time.sleep(poll_seconds)

    def finalize(self, import_id: uuid.UUID, finalizer: Finalizer) -> LegacyImportStatus:
        with self._advisory_lock(f"cardrag-legacy-finalize:{import_id}"):
            status = self.refresh(import_id)
            if status.state == "succeeded":
                return status
            if status.state not in {"ready_to_finalize", "finalizing"}:
                raise ValueError("legacy import is not ready to finalize")
            if status.state == "ready_to_finalize":
                self._set_phase(import_id, "quality_and_publish", state="finalizing")
            try:
                run_state, publication = finalizer(status.run_id, status.generation_id)
            except Exception as exc:
                self._mark_failed(import_id, type(exc).__name__)
                raise
            succeeded = run_state == "succeeded" and publication in {
                "published",
                "already_published",
                "no_change",
            }
            success_phase = "no_change" if publication == "no_change" else "published"
            with self.database.connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE legacy_imports SET state=%s, phase=%s, last_error_code=%s,
                        report=report || jsonb_build_object('publication', %s::text),
                        updated_at=now(), finished_at=now() WHERE import_id=%s
                    """,
                    (
                        "succeeded" if succeeded else "failed",
                        success_phase if succeeded else "publication_failed",
                        None if succeeded else publication,
                        publication,
                        import_id,
                    ),
                )
                connection.commit()
            return self.status(import_id)

    def cancel(self, import_id: uuid.UUID) -> LegacyImportStatus:
        row = self._load_import(import_id)
        if str(row["state"]) in {"succeeded", "failed", "cancelled"}:
            return self.status(import_id)
        run_id = uuid.UUID(str(row["run_id"]))
        # Set the durable run fence before waiting for an in-flight seed loop.
        self.scheduler.set_run_control(run_id, "cancel")
        with self._advisory_lock(f"cardrag-legacy-resume:{import_id}"):
            current = self._load_import(import_id)
            if str(current["state"]) in {"succeeded", "failed", "cancelled"}:
                return self.status(import_id)
            with self.database.connection() as connection, connection.cursor() as cursor:
                # Close the check/enqueue race for any job created immediately
                # before the seed loop observed the first cancellation fence.
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
                    UPDATE legacy_imports SET state='cancelled', phase='cancelled',
                        updated_at=now(), finished_at=now() WHERE import_id=%s
                    """,
                    (import_id,),
                )
                connection.commit()
        return self.status(import_id)

    def status(self, import_id: uuid.UUID) -> LegacyImportStatus:
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT i.*,
                       GREATEST(count(d.*)::int,
                           COALESCE((i.report->>'documents')::int, 0)) AS total,
                       GREATEST(count(*) FILTER (WHERE d.disposition='adopted')::int,
                           COALESCE((i.report->>'adopted')::int, 0)) AS adopted,
                       GREATEST(count(*) FILTER (WHERE d.disposition='reocr')::int,
                           COALESCE((i.report->>'reocr')::int, 0)) AS reocr,
                       count(*) FILTER (WHERE d.state='pending')::int
                           + GREATEST(COALESCE((i.report->>'documents')::int, 0)
                               - count(d.*)::int, 0) AS pending,
                       count(*) FILTER (WHERE d.state='queued')::int AS queued,
                       count(*) FILTER (WHERE d.state='processing')::int AS processing,
                       count(*) FILTER (WHERE d.state='succeeded')::int AS succeeded,
                       count(*) FILTER (WHERE d.state='failed')::int AS failed,
                       COALESCE(sum(CASE WHEN d.state IN ('queued','processing','succeeded')
                           THEN octet_length(d.pdf_sha256) ELSE 0 END), 0)::bigint AS progress_units
                FROM legacy_imports i
                LEFT JOIN legacy_import_documents d USING (import_id)
                WHERE i.import_id=%s GROUP BY i.import_id
                """,
                (import_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError("legacy import does not exist")
            cursor.execute(
                """
                SELECT count(*) FILTER (WHERE state IN ('queued','running','retry_wait'))::int AS active,
                       count(*) FILTER (WHERE state IN ('dead_letter','cancelled'))::int AS failed
                FROM jobs WHERE payload->>'run_id'=%s
                """,
                (str(row["run_id"]),),
            )
            jobs = cursor.fetchone()
        return LegacyImportStatus(
            import_id=uuid.UUID(str(row["import_id"])),
            bundle_id=str(row["bundle_id"]),
            bundle_sha256=str(row["bundle_sha256"]),
            run_id=uuid.UUID(str(row["run_id"])),
            generation_id=str(row["generation_id"]),
            state=str(row["state"]),  # type: ignore[arg-type]
            phase=str(row["phase"]),
            last_error_code=(
                str(row["last_error_code"]) if row["last_error_code"] is not None else None
            ),
            no_publish=bool(row["no_publish"]),
            total=int(row["total"]),
            adopted=int(row["adopted"]),
            reocr=int(row["reocr"]),
            pending=int(row["pending"]),
            queued=int(row["queued"]),
            processing=int(row["processing"]),
            succeeded=int(row["succeeded"]),
            failed=int(row["failed"]),
            processed_bytes=int(row["report"].get("seeded_bytes", 0)),
            verification_files=int(row["report"].get("verification_files", 0)),
            verification_total_files=int(row["report"].get("verification_total_files", 0)),
            verification_bytes=int(row["report"].get("verification_bytes", 0)),
            verification_total_bytes=int(row["report"].get("verification_total_bytes", 0)),
            verification_bytes_per_second=float(
                row["report"].get("verification_bytes_per_second", 0.0)
            ),
            documents_per_second=self._documents_per_second(row),
            eta_seconds=self._eta_seconds(row),
            active_jobs=int(jobs["active"]) if jobs is not None else 0,
            terminal_job_failures=int(jobs["failed"]) if jobs is not None else 0,
        )

    def _ensure_legacy_snapshots(
        self,
        bundle: LegacyBundleManifest,
        documents: tuple[LegacyBundleDocument, ...],
    ) -> None:
        for issuer in (Issuer.WOORI, Issuer.KB):
            selected = [item for item in documents if item.issuer == issuer]
            if not selected:
                continue
            snapshot_id = self._snapshot_id(bundle, issuer)
            started = min(item.discovered_at for item in selected)
            finished = max(item.discovered_at for item in selected)
            with self.database.connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO source_snapshots(
                        snapshot_id, issuer, discovery_mode, parser_version, source_url,
                        observed_count, payload_sha256, created_at, completed_at
                    ) VALUES (%s,%s,'history',%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (snapshot_id) DO UPDATE SET
                        observed_count=EXCLUDED.observed_count,
                        payload_sha256=EXCLUDED.payload_sha256
                    """,
                    (
                        snapshot_id,
                        issuer.value,
                        "legacy-bundle-import.v1",
                        f"https://legacy.invalid/{bundle.bundle_id}/{issuer.value}",
                        len(selected),
                        bundle.content_sha256,
                        started,
                        finished,
                    ),
                )
                connection.commit()

    def _seed_document(
        self,
        import_id: uuid.UUID,
        bundle_root: Path,
        bundle: LegacyBundleManifest,
        document: LegacyBundleDocument,
    ) -> None:
        row = self._load_import(import_id)
        generation_id = str(row["generation_id"])
        run_id = uuid.UUID(str(row["run_id"]))
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO legacy_import_documents(
                    import_id, document_id, document_key, issuer, pdf_sha256,
                    ocr_sha256, disposition, state
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,'pending')
                ON CONFLICT (import_id, document_id) DO UPDATE SET updated_at=now()
                """,
                (
                    import_id,
                    document.document_id,
                    document.document_key,
                    document.issuer.value,
                    document.pdf_sha256,
                    document.ocr_sha256,
                    document.adoption_status,
                ),
            )
            connection.commit()

        pdf_source = self._bundle_object(bundle_root, document.pdf_object_path)
        pdf_object = self.objects.put_file(pdf_source)
        if pdf_object.sha256 != document.pdf_sha256:
            raise RuntimeError("legacy PDF hash changed while entering production CAS")
        ocr_object_key: str | None = None
        ocr_pages: list[str] | None = None
        adoption: dict[str, object] | None = None
        ocr_artifact: ArtifactManifest | None = None
        page_map_artifact: ArtifactManifest | None = None
        source = self._source_record(document)
        identity = source.document_identity_for(document.pdf_sha256)
        snapshot_id = self._snapshot_id(bundle, document.issuer)
        pdf_artifact = ArtifactManifest(
            artifact_type=ArtifactType.SOURCE_PDF,
            content_sha256=pdf_object.sha256,
            size_bytes=pdf_object.size_bytes,
            media_type="application/pdf",
            created_at=document.discovered_at,
            lineage=Lineage(
                processor="legacy-bundle-import",
                processor_version=bundle.schema_version,
                config_sha256=canonical_sha256({"bundle_sha256": bundle.content_sha256}),
                input_sha256=(document.pdf_sha256,),
                source_snapshot_id=snapshot_id,
            ),
            document=identity,
            page_count=document.pdf_page_count,
            attributes=(ManifestAttribute(name="bundle_id", value=bundle.bundle_id),),
        )
        if document.adoption_status == "adopted":
            if document.ocr_object_path is None or document.ocr_sha256 is None:
                raise RuntimeError("validated adoption has no OCR object")
            ocr_source = self._bundle_object(bundle_root, document.ocr_object_path)
            stored_ocr = self.objects.put_file(ocr_source)
            if stored_ocr.sha256 != document.ocr_sha256:
                raise RuntimeError("legacy OCR hash changed while entering production CAS")
            ocr_object_key = stored_ocr.relative_path.as_posix()
            ocr_pages = split_pages(ocr_source.read_text(encoding="utf-8"))
            if len(ocr_pages) != document.pdf_page_count:
                raise RuntimeError("validated legacy OCR page coverage changed")
            adoption = legacy_adoption_manifest(bundle, document, import_id=import_id)
            lineage = Lineage(
                processor="legacy-ocr-adoption",
                processor_version=bundle.adoption_policy,
                config_sha256=canonical_sha256(
                    {"adoption_policy": bundle.adoption_policy, "bundle": bundle.content_sha256}
                ),
                input_sha256=(document.pdf_sha256,),
                source_snapshot_id=snapshot_id,
                provider="legacy-import",
                model="legacy-unreported",
            )
            ocr_artifact = ArtifactManifest(
                artifact_type=ArtifactType.OCR_MARKDOWN,
                content_sha256=stored_ocr.sha256,
                size_bytes=stored_ocr.size_bytes,
                media_type="text/markdown; charset=utf-8",
                created_at=document.discovered_at,
                lineage=lineage,
                document=identity,
                page_count=len(ocr_pages),
                attributes=(ManifestAttribute(name="bundle_id", value=bundle.bundle_id),),
            )
            page_map = (json.dumps(ocr_pages, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
            page_map_object = self.objects.put_bytes(page_map)
            page_map_artifact = ArtifactManifest(
                artifact_type=ArtifactType.OCR_PAGE_MAP,
                content_sha256=page_map_object.sha256,
                size_bytes=page_map_object.size_bytes,
                media_type="application/json",
                created_at=document.discovered_at,
                lineage=Lineage(
                    processor="ocr-page-map",
                    processor_version="ocr-page-map.v1",
                    config_sha256=canonical_sha256({"page_marker": "## Page N"}),
                    input_sha256=(stored_ocr.sha256,),
                    source_snapshot_id=snapshot_id,
                ),
                document=identity,
                page_count=len(ocr_pages),
            )

        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO source_documents(
                    document_id, discovery_id, issuer, product_code, product_name, document_type,
                    effective_date, source_version, version_sort_key, source_snapshot_id, source_url,
                    pdf_sha256, raw_object_key, last_seen_at, tombstoned_at, metadata
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,NULL,%s::jsonb)
                ON CONFLICT (document_id) DO UPDATE SET
                    product_name=EXCLUDED.product_name,
                    last_seen_at=GREATEST(source_documents.last_seen_at, EXCLUDED.last_seen_at),
                    metadata=source_documents.metadata || EXCLUDED.metadata
                """,
                (
                    document.document_id,
                    source.document_identity.stable_id,
                    document.issuer.value,
                    document.product_code,
                    document.product_name,
                    document.document_type,
                    document.effective_date,
                    document.source_version,
                    json.dumps(document.version_sort_key),
                    snapshot_id,
                    document.source_url,
                    document.pdf_sha256,
                    pdf_object.relative_path.as_posix(),
                    document.discovered_at,
                    json.dumps(
                        {
                            "legacy_adoption": adoption,
                            "legacy_bundle_id": bundle.bundle_id,
                            "source": source.model_dump(mode="json"),
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            cursor.execute(
                "DELETE FROM evidence WHERE generation_id=%s AND document_id=%s",
                (generation_id, document.document_id),
            )
            cursor.execute(
                "DELETE FROM generation_artifacts WHERE generation_id=%s AND document_id=%s",
                (generation_id, document.document_id),
            )
            cursor.execute(
                """
                INSERT INTO generation_documents(
                    generation_id, document_id, issuer, product_code, product_name, document_type,
                    effective_date, source_version, version_sort_key, source_snapshot_id, source_url,
                    discovered_at, pdf_sha256, raw_object_key, pdf_size_bytes, pdf_page_count,
                    ocr_sha256, ocr_object_key, ocr_pages, ocr_manifest, is_latest
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s)
                ON CONFLICT (generation_id, document_id) DO UPDATE SET
                    product_name=EXCLUDED.product_name,
                    source_snapshot_id=EXCLUDED.source_snapshot_id,
                    source_url=EXCLUDED.source_url,
                    discovered_at=EXCLUDED.discovered_at,
                    raw_object_key=EXCLUDED.raw_object_key,
                    pdf_size_bytes=EXCLUDED.pdf_size_bytes,
                    pdf_page_count=EXCLUDED.pdf_page_count,
                    ocr_sha256=EXCLUDED.ocr_sha256,
                    ocr_object_key=EXCLUDED.ocr_object_key,
                    ocr_pages=EXCLUDED.ocr_pages,
                    ocr_manifest=EXCLUDED.ocr_manifest,
                    structured_sha256=NULL, structured_object_key=NULL,
                    structure_schema_version=NULL, embedding_provider=NULL,
                    embedding_model=NULL, embedding_dimension=NULL, chunk_policy=NULL,
                    chunk_count=NULL, embedding_count=NULL, index_count=NULL,
                    is_latest=EXCLUDED.is_latest, materialized_from_generation_id=NULL,
                    updated_at=now()
                WHERE generation_documents.pdf_sha256=EXCLUDED.pdf_sha256
                RETURNING document_id
                """,
                (
                    generation_id,
                    document.document_id,
                    document.issuer.value,
                    document.product_code,
                    document.product_name,
                    document.document_type,
                    document.effective_date,
                    document.source_version,
                    json.dumps(document.version_sort_key),
                    snapshot_id,
                    document.source_url,
                    document.discovered_at,
                    document.pdf_sha256,
                    pdf_object.relative_path.as_posix(),
                    document.pdf_size_bytes,
                    document.pdf_page_count,
                    document.ocr_sha256 if adoption else None,
                    ocr_object_key,
                    json.dumps(ocr_pages, ensure_ascii=False) if ocr_pages is not None else None,
                    json.dumps(adoption, ensure_ascii=False) if adoption is not None else None,
                    document.is_latest,
                ),
            )
            if cursor.fetchone() is None:
                raise RuntimeError("candidate contains conflicting bytes for legacy document")
            self._record_artifact(cursor, generation_id, document.document_id, pdf_artifact)
            if ocr_artifact is not None and page_map_artifact is not None:
                self._record_artifact(cursor, generation_id, document.document_id, ocr_artifact)
                self._record_artifact(cursor, generation_id, document.document_id, page_map_artifact)
            cursor.execute(
                """
                UPDATE legacy_import_documents SET state='seeded', updated_at=now(), error_code=NULL
                WHERE import_id=%s AND document_id=%s
                """,
                (import_id, document.document_id),
            )
            cursor.execute(
                """
                UPDATE legacy_imports SET report=jsonb_set(
                    report, '{seeded_bytes}',
                    to_jsonb(LEAST(
                        COALESCE((report->>'payload_bytes')::bigint, 0),
                        COALESCE((report->>'seeded_bytes')::bigint, 0) + %s::bigint
                    ))
                ), updated_at=now() WHERE import_id=%s
                """,
                (
                    document.pdf_size_bytes
                    + ((document.ocr_size_bytes or 0) if adoption is not None else 0),
                    import_id,
                ),
            )
            connection.commit()

        stage = "structure" if adoption is not None else "ocr"
        payload: dict[str, Any] = {
            "run_id": str(run_id),
            "generation_id": generation_id,
            "source_snapshot_id": snapshot_id,
            "source": source.model_dump(mode="json"),
            "pdf_sha256": document.pdf_sha256,
            "raw_object_key": pdf_object.relative_path.as_posix(),
            "bulk": True,
            "legacy_import_id": str(import_id),
            "legacy_bundle_sha256": bundle.content_sha256,
        }
        if adoption is not None:
            payload.update({"ocr_sha256": document.ocr_sha256, "ocr_object_key": ocr_object_key})
        job_id, _ = self.jobs.enqueue(
            issuer=document.issuer.value,
            stage=stage,
            document_id=document.document_id,
            idempotency_key=(
                f"legacy:{import_id}:{stage}:{document.document_id}:"
                f"{document.ocr_sha256 if adoption else document.pdf_sha256}"
            ),
            payload=payload,
            max_attempts=self.max_job_attempts,
        )
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE legacy_import_documents SET state='queued', job_id=%s, updated_at=now()
                WHERE import_id=%s AND document_id=%s
                """,
                (job_id, import_id, document.document_id),
            )
            connection.commit()

    def _enqueue_reconciliation(self, import_id: uuid.UUID) -> None:
        row = self._load_import(import_id)
        run_id = uuid.UUID(str(row["run_id"]))
        generation_id = str(row["generation_id"])
        for issuer in (Issuer.WOORI, Issuer.KB, Issuer.SHINHAN):
            history = issuer == Issuer.SHINHAN
            self.jobs.enqueue(
                issuer=issuer.value,
                stage="discover",
                idempotency_key=f"legacy-reconcile:{import_id}:{issuer.value}",
                payload={
                    "run_id": str(run_id),
                    "generation_id": generation_id,
                    "mode": "history" if history else "current",
                    "bulk": history,
                    "categories": ["credit", "check"] if issuer == Issuer.SHINHAN else None,
                    "legacy_import_id": str(import_id),
                },
                max_attempts=self.max_job_attempts,
            )
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE run_issuer_status SET state='running', started_at=COALESCE(started_at, now())
                WHERE run_id=%s AND state IN ('queued','running')
                """,
                (run_id,),
            )
            connection.commit()

    def _record_artifact(
        self, cursor: Any, generation_id: str, document_id: str, manifest: ArtifactManifest
    ) -> None:
        stored = self.objects.put_bytes(manifest.canonical_bytes())
        cursor.execute(
            """
            INSERT INTO generation_artifacts(
                generation_id, manifest_id, artifact_id, document_id, artifact_type,
                content_sha256, size_bytes, media_type, manifest_object_key, manifest, created_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
            ON CONFLICT (generation_id, manifest_id) DO NOTHING
            """,
            (
                generation_id,
                manifest.manifest_id,
                manifest.artifact_id,
                document_id,
                manifest.artifact_type.value,
                manifest.content_sha256,
                manifest.size_bytes,
                manifest.media_type,
                stored.relative_path.as_posix(),
                manifest.canonical_bytes().decode(),
                manifest.created_at,
            ),
        )

    @staticmethod
    def _source_record(document: LegacyBundleDocument) -> SourceRecord:
        file_name = document.file_name if document.file_name.casefold().endswith(".pdf") else "legacy.pdf"
        return SourceRecord.model_validate(
            {
                "issuer": document.issuer.value,
                "product_code": document.product_code,
                "product_name": document.product_name,
                "document_type": document.document_type,
                "effective_date": document.effective_date,
                "source_version": document.source_version,
                "source_url": document.source_url,
                "source_post_id": document.source_post_id,
                "file_name": file_name,
                "category": "legacy",
                "is_current": document.is_latest,
                "discovered_at": document.discovered_at,
                "metadata": {"legacy_document_key": document.document_key},
            }
        )

    @staticmethod
    def _snapshot_id(bundle: LegacyBundleManifest, issuer: Issuer) -> str:
        return f"legacy_{issuer.value}_{bundle.content_sha256[:32]}"

    @staticmethod
    def _bundle_object(bundle_root: Path, relative: str) -> Path:
        root = bundle_root.resolve(strict=True)
        target = (root / relative).resolve(strict=True)
        if not target.is_relative_to(root) or not target.is_file() or target.is_symlink():
            raise ValueError("bundle object escaped its sealed root")
        return target

    def _load_import(self, import_id: uuid.UUID) -> dict[str, Any]:
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM legacy_imports WHERE import_id=%s", (import_id,))
            row = cursor.fetchone()
        if row is None:
            raise ValueError("legacy import does not exist")
        return dict(row)

    @contextmanager
    def _advisory_lock(self, name: str) -> Iterator[None]:
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(hashtext(%s))", (name,))
            connection.commit()
            try:
                yield
            finally:
                cursor.execute("SELECT pg_advisory_unlock(hashtext(%s))", (name,))
                connection.commit()

    def _document_has_live_or_successful_chain(self, import_id: uuid.UUID, document_id: str) -> bool:
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT d.state,
                       EXISTS (
                           SELECT 1 FROM jobs j
                           WHERE j.payload->>'legacy_import_id'=%s
                             AND j.document_id=d.document_id
                             AND j.state IN ('queued','running','retry_wait')
                       ) AS active,
                       EXISTS (
                           SELECT 1 FROM jobs j
                           WHERE j.payload->>'legacy_import_id'=%s
                             AND j.document_id=d.document_id
                             AND j.stage='index' AND j.state='succeeded'
                       ) AS succeeded
                FROM legacy_import_documents d
                WHERE d.import_id=%s AND d.document_id=%s
                """,
                (str(import_id), str(import_id), import_id, document_id),
            )
            row = cursor.fetchone()
        return bool(
            row is not None and (bool(row["active"]) or bool(row["succeeded"]))
        )

    def _redrive_failed_chains(self, import_id: uuid.UUID) -> None:
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id FROM jobs
                WHERE payload->>'legacy_import_id'=%s
                  AND state IN ('dead_letter','cancelled')
                ORDER BY created_at, id
                """,
                (str(import_id),),
            )
            job_ids = [uuid.UUID(str(row["id"])) for row in cursor.fetchall()]
            connection.commit()
        for job_id in job_ids:
            self.jobs.redrive(job_id, max_attempts=self.max_job_attempts)
        if job_ids:
            with self.database.connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE legacy_import_documents SET state='processing', error_code=NULL,
                        updated_at=now() WHERE import_id=%s AND state='failed'
                    """,
                    (import_id,),
                )
                connection.commit()

    @staticmethod
    def _documents_per_second(row: dict[str, Any]) -> float:
        started_at = row["started_at"]
        if not isinstance(started_at, datetime):
            raise RuntimeError("legacy import start timestamp is invalid")
        elapsed = max(0.001, (datetime.now(UTC) - started_at).total_seconds())
        completed = int(row["succeeded"]) + int(row["failed"])
        return float(completed) / elapsed

    @classmethod
    def _eta_seconds(cls, row: dict[str, Any]) -> float | None:
        rate = cls._documents_per_second(row)
        if rate <= 0:
            return None
        remaining = int(row["total"]) - int(row["succeeded"]) - int(row["failed"])
        return max(0.0, remaining / rate)

    def _set_phase(self, import_id: uuid.UUID, phase: str, *, state: str) -> None:
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE legacy_imports SET state=%s, phase=%s, updated_at=now()
                WHERE import_id=%s AND state NOT IN ('cancelled','succeeded')
                """,
                (state, phase, import_id),
            )
            connection.commit()

    def _cancel_requested(self, import_id: uuid.UUID) -> bool:
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT i.state='cancelled' OR r.cancel_requested AS cancelled
                FROM legacy_imports i
                JOIN pipeline_runs r USING (run_id)
                WHERE i.import_id=%s
                """,
                (import_id,),
            )
            row = cursor.fetchone()
        if row is None:
            raise ValueError("legacy import does not exist")
        return bool(row["cancelled"])

    def _bundle_verification_progress(
        self,
        import_id: uuid.UUID,
        progress: Callable[[LegacyImportStatus], None] | None,
    ) -> Callable[[int, int, int, int], None] | None:
        if progress is None:
            return None
        started = time.monotonic()
        last_emitted = started
        last_emitted_files = 0

        def emit(completed: int, total: int, checked_bytes: int, total_bytes: int) -> None:
            nonlocal last_emitted, last_emitted_files
            now = time.monotonic()
            if (
                completed == total
                or completed - last_emitted_files >= 100
                or now - last_emitted >= 30
            ):
                elapsed = max(0.001, now - started)
                with self.database.connection() as connection, connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE legacy_imports
                        SET phase='verify_bundle', report=report || jsonb_build_object(
                            'verification_files', %s::int,
                            'verification_total_files', %s::int,
                            'verification_bytes', %s::bigint,
                            'verification_total_bytes', %s::bigint,
                            'verification_bytes_per_second', %s::double precision
                        ), updated_at=now()
                        WHERE import_id=%s
                        """,
                        (completed, total, checked_bytes, total_bytes, checked_bytes / elapsed, import_id),
                    )
                    connection.commit()
                progress(self.status(import_id))
                last_emitted = now
                last_emitted_files = completed

        return emit

    def _mark_failed(self, import_id: uuid.UUID, error_code: str) -> None:
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE legacy_imports SET state='failed', phase='failed', last_error_code=%s,
                    updated_at=now(), finished_at=now() WHERE import_id=%s
                """,
                (error_code, import_id),
            )
            cursor.execute(
                """
                UPDATE legacy_import_documents SET state='failed', error_code=%s, updated_at=now()
                WHERE import_id=%s AND state IN ('pending','seeded','queued','processing')
                """,
                (error_code, import_id),
            )
            # A failed import is a durable run fence, not merely a reporting
            # state.  Cancel every already-enqueued chain in the same
            # transaction so workers cannot keep mutating a failed candidate.
            cursor.execute(
                """
                WITH target_run AS (
                    SELECT run_id FROM legacy_imports WHERE import_id=%s
                ), cancelled AS (
                    UPDATE jobs j SET cancel_requested=true, state='cancelled'::job_state,
                        lease_owner=NULL, lease_until=NULL, updated_at=now()
                    FROM target_run r
                    WHERE j.payload->>'run_id'=r.run_id::text
                      AND j.state NOT IN ('succeeded','dead_letter','cancelled')
                    RETURNING j.id, j.attempt_count, j.fencing_token
                )
                UPDATE job_attempts a SET finished_at=now(), outcome='cancelled',
                    error_code='legacy_import_failed'
                FROM cancelled c
                WHERE a.job_id=c.id AND a.attempt_no=c.attempt_count
                  AND a.fencing_token=c.fencing_token AND a.finished_at IS NULL
                """,
                (import_id,),
            )
            cursor.execute(
                """
                UPDATE generations g SET state='failed'
                FROM legacy_imports i WHERE i.import_id=%s AND g.generation_id=i.generation_id
                  AND g.state IN ('building','validating')
                """,
                (import_id,),
            )
            cursor.execute(
                """
                UPDATE pipeline_runs r SET state='failed', cancel_requested=true,
                    pause_requested=false, finished_at=now(),
                    report=r.report || jsonb_build_object('legacy_import_error', %s::text)
                FROM legacy_imports i WHERE i.import_id=%s AND r.run_id=i.run_id
                  AND r.state IN ('running','paused','queued')
                """,
                (error_code, import_id),
            )
            connection.commit()
