from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from cardrag.db import Postgres
from cardrag.jobs import JobRepository
from cardrag.legacy import LegacyBundlePreparer, LegacyImportService
from cardrag.scheduler import DailyScheduler
from cardrag.storage import ContentAddressedObjectStore
from tests.support_pdf import synthetic_text_pdf_bytes

pytestmark = pytest.mark.integration

PDF = synthetic_text_pdf_bytes(["integration fixture"])


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _bundle(tmp_path: Path) -> Path:
    source = tmp_path / "legacy"
    (source / "raw").mkdir(parents=True)
    (source / "ocr").mkdir()
    (source / "raw/card.pdf").write_bytes(PDF)
    ocr = "## Page 1\n레거시 OCR\n"
    (source / "ocr/ocr.md").write_text(ocr, encoding="utf-8")
    metadata = {
        "schema_version": "ocr_result_manifest.v2",
        "raw_pdf_rel_path": "raw/card.pdf",
        "ocr_md_rel_path": "ocr/ocr.md",
        "ocr_md_sha256": _sha(ocr.encode()),
        "ocr_md_chars": len(ocr),
        "page_count": 1,
    }
    (source / "ocr/metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    entry = {
        "cardCompany": "wooricard",
        "doc_version_id": "wooricard:100001:product_description:2025-01-01:v1",
        "productCode": "100001",
        "productName": "통합 시험 카드",
        "docType": "product_description",
        "beginDt": "2025-01-01",
        "gdccVer": "1",
        "fileNm": "card.pdf",
        "sourceUrl": "https://example.invalid/card.pdf",
        "sourcePostId": "card-one",
        "pdf_sha256": _sha(PDF),
        "ocr_remote_rel": "ocr/ocr.md",
        "metadata_remote_rel": "ocr/metadata.json",
        "pages": 1,
        "ocr_chars": len(ocr),
        "completed_at": "2026-01-01T00:00:00Z",
    }
    manifest = source / "master.json"
    manifest.write_text(json.dumps({"entries": [entry]}), encoding="utf-8")
    result = LegacyBundlePreparer(source).prepare(manifest, tmp_path / "bundles")
    assert result.bundle_path
    return Path(result.bundle_path)


def _service(database: Postgres, tmp_path: Path) -> LegacyImportService:
    jobs = JobRepository(database)
    return LegacyImportService(
        database,
        jobs,
        ContentAddressedObjectStore((tmp_path / "objects").resolve()),
        DailyScheduler(database, jobs),
        embedding_model="fixture-embedding-v1",
        embedding_dimension=1536,
        ocr_model="fixture-ocr-v1",
        ocr_reasoning_effort="high",
        ocr_fallback_model="fixture-fallback-v1",
        render_scale=3.0,
        ocr_chunk_pages=2,
    )


def test_import_seeds_adopted_ocr_once_and_reconciliation_modes(
    clean_database: Postgres,
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    service = _service(clean_database, tmp_path)

    first = service.start(bundle, no_publish=True)
    second = service.start(bundle, no_publish=True)

    assert first.import_id == second.import_id
    assert first.run_id == second.run_id
    assert first.generation_id == second.generation_id
    assert first.adopted == 1
    assert first.reocr == 0
    assert first.no_publish
    with clean_database.connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT count(*)::int AS n FROM legacy_imports")
        assert cursor.fetchone() == {"n": 1}
        cursor.execute("SELECT count(*)::int AS n FROM pipeline_runs")
        assert cursor.fetchone() == {"n": 1}
        cursor.execute("SELECT count(*)::int AS n FROM generations")
        assert cursor.fetchone() == {"n": 1}
        cursor.execute(
            """
            SELECT ocr_manifest->>'schema_version' AS schema,
                   ocr_manifest->>'pdf_sha256'=pdf_sha256 AS pdf_bound,
                   ocr_manifest->>'ocr_sha256'=ocr_sha256 AS ocr_bound
            FROM generation_documents WHERE generation_id=%s
            """,
            (first.generation_id,),
        )
        assert cursor.fetchone() == {
            "schema": "cardrag.legacy-ocr-adoption.v1",
            "pdf_bound": True,
            "ocr_bound": True,
        }
        cursor.execute(
            """
            SELECT stage, issuer, payload->>'mode' AS mode
            FROM jobs WHERE payload->>'run_id'=%s
            ORDER BY stage, issuer
            """,
            (str(first.run_id),),
        )
        jobs = cursor.fetchall()
    assert {tuple(row.values()) for row in jobs} == {
        ("discover", "kb", "current"),
        ("discover", "shinhan", "history"),
        ("discover", "woori", "current"),
        ("structure", "woori", None),
    }
    assert not any(row["stage"] == "ocr" for row in jobs)


def test_cancel_keeps_candidate_data_but_fences_jobs(clean_database: Postgres, tmp_path: Path) -> None:
    service = _service(clean_database, tmp_path)
    status = service.start(_bundle(tmp_path), no_publish=True)

    cancelled = service.cancel(status.import_id)

    assert cancelled.state == "cancelled"
    with clean_database.connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT state FROM pipeline_runs WHERE run_id=%s", (status.run_id,))
        assert cursor.fetchone() == {"state": "cancelled"}
        cursor.execute(
            "SELECT count(*)::int AS n FROM generation_documents WHERE generation_id=%s",
            (status.generation_id,),
        )
        assert cursor.fetchone() == {"n": 1}


def test_concurrent_cancel_fences_jobs_created_by_an_inflight_seed(
    clean_database: Postgres,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(clean_database, tmp_path)
    bundle = _bundle(tmp_path)
    seed_entered = threading.Event()
    allow_seed = threading.Event()
    original_seed = service._seed_document

    def delayed_seed(*args: object, **kwargs: object) -> None:
        seed_entered.set()
        assert allow_seed.wait(timeout=5)
        original_seed(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(service, "_seed_document", delayed_seed)
    created = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        start_future = pool.submit(service.start, bundle, created=created.append)
        assert seed_entered.wait(timeout=5)
        assert created
        cancel_future = pool.submit(service.cancel, created[0].import_id)
        allow_seed.set()
        start_future.result(timeout=10)
        cancelled = cancel_future.result(timeout=10)

    assert cancelled.state == "cancelled"
    with clean_database.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*)::int AS active
            FROM jobs
            WHERE payload->>'run_id'=%s AND state IN ('queued','running','retry_wait')
            """,
            (str(cancelled.run_id),),
        )
        assert cursor.fetchone() == {"active": 0}
        cursor.execute("SELECT state FROM legacy_imports WHERE import_id=%s", (cancelled.import_id,))
        assert cursor.fetchone() == {"state": "cancelled"}


def test_concurrent_start_creates_exactly_one_import_run_and_generation(
    clean_database: Postgres,
    tmp_path: Path,
) -> None:
    service = _service(clean_database, tmp_path)
    bundle = _bundle(tmp_path)

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: service.start(bundle), range(2)))

    assert len({item.import_id for item in statuses}) == 1
    assert len({item.run_id for item in statuses}) == 1
    assert len({item.generation_id for item in statuses}) == 1
    with clean_database.connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT count(*)::int AS n FROM legacy_imports")
        assert cursor.fetchone() == {"n": 1}


def test_import_creation_rolls_back_run_and_generation_if_ledger_insert_fails(
    clean_database: Postgres,
    tmp_path: Path,
) -> None:
    service = _service(clean_database, tmp_path)
    bundle = _bundle(tmp_path)
    with clean_database.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            CREATE FUNCTION reject_legacy_import_fixture() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'fixture ledger failure';
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER reject_legacy_import_fixture
            BEFORE INSERT ON legacy_imports
            FOR EACH ROW EXECUTE FUNCTION reject_legacy_import_fixture();
            """
        )
        connection.commit()

    try:
        with pytest.raises(Exception, match="fixture ledger failure"):
            service.start(bundle)
    finally:
        with clean_database.connection() as connection, connection.cursor() as cursor:
            cursor.execute("DROP TRIGGER reject_legacy_import_fixture ON legacy_imports")
            cursor.execute("DROP FUNCTION reject_legacy_import_fixture()")
            connection.commit()

    with clean_database.connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT count(*)::int AS n FROM legacy_imports")
        assert cursor.fetchone() == {"n": 0}
        cursor.execute("SELECT count(*)::int AS n FROM pipeline_runs")
        assert cursor.fetchone() == {"n": 0}
        cursor.execute("SELECT count(*)::int AS n FROM generations")
        assert cursor.fetchone() == {"n": 0}


def test_import_prints_durable_ids_before_full_bundle_verification(
    clean_database: Postgres,
    tmp_path: Path,
) -> None:
    service = _service(clean_database, tmp_path)
    bundle = _bundle(tmp_path)
    pdf = next((bundle / "objects/pdf").rglob("*.pdf"))
    pdf.chmod(0o640)
    pdf.write_bytes(PDF + b"tampered-after-ready")
    created = []

    with pytest.raises(Exception, match="checksum mismatch"):
        service.start(bundle, created=created.append)

    assert len(created) == 1
    status = service.status(created[0].import_id)
    assert status.import_id == created[0].import_id
    assert status.run_id == created[0].run_id
    assert status.state == "failed"
    with clean_database.connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT state FROM pipeline_runs WHERE run_id=%s", (status.run_id,))
        assert cursor.fetchone() == {"state": "failed"}
        cursor.execute(
            "SELECT state FROM generations WHERE generation_id=%s",
            (status.generation_id,),
        )
        assert cursor.fetchone() == {"state": "failed"}
        cursor.execute("SELECT count(*)::int AS n FROM pipeline_runs")
        assert cursor.fetchone() == {"n": 1}
        cursor.execute("SELECT count(*)::int AS n FROM generations")
        assert cursor.fetchone() == {"n": 1}


def test_import_failure_fences_enqueued_jobs_and_resume_redrives_them(
    clean_database: Postgres,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(clean_database, tmp_path)
    bundle = _bundle(tmp_path)
    original_reconciliation = service._enqueue_reconciliation

    def fail_after_document_seed(import_id: object) -> None:
        raise RuntimeError("fixture failure after enqueue")

    monkeypatch.setattr(service, "_enqueue_reconciliation", fail_after_document_seed)
    with pytest.raises(RuntimeError, match="fixture failure after enqueue"):
        service.start(bundle)
    with clean_database.connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT import_id, run_id FROM legacy_imports")
        row = cursor.fetchone()
        assert row is not None
        import_id = row["import_id"]
        run_id = row["run_id"]
        cursor.execute(
            "SELECT state, cancel_requested FROM pipeline_runs WHERE run_id=%s",
            (run_id,),
        )
        assert cursor.fetchone() == {"state": "failed", "cancel_requested": True}
        cursor.execute(
            """
            SELECT count(*) FILTER (WHERE state IN ('queued','running','retry_wait'))::int AS active,
                   count(*) FILTER (WHERE state='cancelled')::int AS cancelled
            FROM jobs WHERE payload->>'run_id'=%s
            """,
            (str(run_id),),
        )
        counts = cursor.fetchone()
        assert counts is not None and counts["active"] == 0 and counts["cancelled"] >= 1
    assert service.jobs.claim(worker_id="fixture", lease_seconds=30) is None

    monkeypatch.setattr(service, "_enqueue_reconciliation", original_reconciliation)
    resumed = service.resume(import_id, bundle)

    assert resumed.state == "processing"
    with clean_database.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*)::int AS active FROM jobs "
            "WHERE payload->>'run_id'=%s AND state IN ('queued','running','retry_wait')",
            (str(run_id),),
        )
        assert cursor.fetchone()["active"] >= 1


def test_finalize_resumes_after_publish_before_ledger_commit(
    clean_database: Postgres,
    tmp_path: Path,
) -> None:
    service = _service(clean_database, tmp_path)
    status = service.start(_bundle(tmp_path), no_publish=True)
    with clean_database.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE legacy_imports
            SET state='finalizing', phase='quality_and_publish'
            WHERE import_id=%s
            """,
            (status.import_id,),
        )
        connection.commit()

    finalized = service.finalize(
        status.import_id,
        lambda run_id, generation_id: ("succeeded", "already_published"),
    )

    assert finalized.state == "succeeded"
    assert finalized.phase == "published"


def test_finalize_treats_clean_no_change_as_success(
    clean_database: Postgres,
    tmp_path: Path,
) -> None:
    service = _service(clean_database, tmp_path)
    status = service.start(_bundle(tmp_path), no_publish=True)
    with clean_database.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE legacy_imports
            SET state='ready_to_finalize', phase='awaiting_finalize'
            WHERE import_id=%s
            """,
            (status.import_id,),
        )
        cursor.execute(
            "UPDATE pipeline_runs SET state='succeeded', finished_at=now() WHERE run_id=%s",
            (status.run_id,),
        )
        connection.commit()

    finalized = service.finalize(
        status.import_id,
        lambda run_id, generation_id: ("succeeded", "no_change"),
    )

    assert finalized.state == "succeeded"
    assert finalized.phase == "no_change"
    assert finalized.last_error_code is None
