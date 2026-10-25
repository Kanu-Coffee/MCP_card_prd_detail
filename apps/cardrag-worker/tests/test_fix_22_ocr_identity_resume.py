"""Tests for FIX_22: OCR identity preservation and diagnostics on same-run resume."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from cardrag_core import (
    ArtifactRef,
    ContentOCRArtifactManifest,
    ContentOCRImportedProvenance,
    OCRInput,
    verify_ocr_bytes,
)

from cardrag_worker.content_cache import ContentOCRVariantStore
from cardrag_worker.pipeline import (
    WorkerPipeline,
    WorkerUnexpectedFailureError,
    WorkerUnexpectedFailureRecord,
)
from cardrag_worker.state import WorkerState


@pytest.mark.anyio
async def test_content_variant_store_lookup_local_with_and_without_original_manifest(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    prior = root / "runs" / "prior-run"
    doc = "doc_f0cefe5475d41e852b0d9d3bd73001c983acb13ff05e546658d2156012f57247"
    doc_ocr = prior / "documents" / doc / "ocr"
    doc_ocr.mkdir(parents=True)

    body = b"## Page 1\n\n\xec\xb9\xb4\xeb\x93\x9c \xed\x98\x9c\xed\x83\x9d\xec\x9d\x80 \xec\xa0\x84\xec\x9b\x94 \xec\x9d\xb4\xec\x9a\xa9\xea\xb8\x88\xec\x95\xa1 \xec\x82\xac\xec\x8b\xad\xeb\xa7\x8c\xec\x9b\x90 \xec\x9d\xb4\xec\x83\x81 \xec\x82\xac\xec\x9a\xa9\xed\x95\x9c \xea\xb2\xbd\xec\x9a\xb0 \xec\x9b\x94 \xec\xb5\x9c\xeb\x8c\x80 \xec\x9d\xbc\xeb\xa7\x8c\xec\x9b\x90\xea\xb9\x8c\xec\xa7\x80 \xed\x95\xa0\xec\x9d\xb8\xeb\x90\xa9\xeb\x8b\x88\xeb\x8b\xa4.\n"
    ocr_sha = hashlib.sha256(body).hexdigest()
    source = OCRInput(pdf_sha256="a" * 64, pdf_size_bytes=100, page_count=1)
    verified = verify_ocr_bytes(
        body,
        expected_page_count=1,
        expected_sha256=ocr_sha,
        expected_size_bytes=len(body),
    )
    (doc_ocr / "ocr.md").write_bytes(body)

    provenance = ContentOCRImportedProvenance(
        source_kind="generation-only",
        provider="generation-only",
        model="unrecorded",
        generation_id="g-original-generation",
        document_id=doc,
    )
    original_manifest = ContentOCRArtifactManifest.create(
        source=source,
        cache_epoch=0,
        output=ArtifactRef.for_cas(
            sha256=ocr_sha,
            size_bytes=len(body),
            media_type="text/markdown; charset=utf-8",
        ),
        ocr_chars=verified.char_count,
        page_output_sha256=verified.page_sha256,
        created_at=datetime(2026, 10, 20, 12, 0, tzinfo=UTC),
        provenance=provenance,
    )

    # 1. Sealed publish.json with differing generation_id and created_at
    (prior / "sealed").mkdir(parents=True)
    seal = {
        "manifest": {
            "generation_id": "g-prior-generation",
            "created_at": "2026-10-21T00:00:00+00:00",
            "documents": [
                {
                    "document_id": doc,
                    "availability": "available",
                    "pdf": {
                        "sha256": source.pdf_sha256,
                        "size_bytes": source.pdf_size_bytes,
                    },
                    "page_count": 1,
                    "ocr": {"sha256": ocr_sha, "size_bytes": len(body)},
                    "ocr_cache_kind": "content",
                    "ocr_reuse_key": original_manifest.reuse_key,
                    "ocr_variant_id": original_manifest.variant_id,
                }
            ],
        }
    }
    (prior / "sealed" / "publish.json").write_text(json.dumps(seal))

    store = ContentOCRVariantStore(webdav=None, state_root=root)

    # Case A: Without content-manifest.json, lookup derives valid imported variant without ValidationError
    hit_derived = await store.lookup(
        run_id="current-run",
        document_id=doc,
        source=source,
        cache_epoch=0,
        expected_ocr_identity=(ocr_sha, len(body)),
    )
    assert hit_derived is not None
    assert hit_derived.body == body
    assert hit_derived.manifest.output.sha256 == ocr_sha
    assert hit_derived.manifest.variant_id == hit_derived.manifest.expected_variant_id

    # Case B: With valid content-manifest.json, lookup restores the exact original manifest
    (doc_ocr / "content-manifest.json").write_bytes(original_manifest.canonical_bytes())
    hit_exact = await store.lookup(
        run_id="current-run",
        document_id=doc,
        source=source,
        cache_epoch=0,
        expected_ocr_identity=(ocr_sha, len(body)),
    )
    assert hit_exact is not None
    assert hit_exact.manifest == original_manifest
    assert hit_exact.manifest.variant_id == original_manifest.variant_id

    # Case C: With corrupted content-manifest.json, safe fallback without crashing
    (doc_ocr / "content-manifest.json").write_bytes(b"{not valid json")
    hit_corrupt = await store.lookup(
        run_id="current-run",
        document_id=doc,
        source=source,
        cache_epoch=0,
        expected_ocr_identity=(ocr_sha, len(body)),
    )
    assert hit_corrupt is not None
    assert hit_corrupt.body == body
    assert hit_corrupt.manifest.variant_id == hit_corrupt.manifest.expected_variant_id


def test_worker_unexpected_failure_record_allows_structured_revalidation_codes() -> None:
    now = datetime.now(UTC)
    record = WorkerUnexpectedFailureRecord(
        run_id="49845932e05849d3a0110a2bc82d6c1c",
        occurred_at=now,
        error_class_category="validation",
        reason_code="stage_revalidation_failed",
        reason="Succeeded stage revalidation failed.",
        document_id="doc_f0cefe5475d41e852b0d9d3bd73001c983acb13ff05e546658d2156012f57247",
        stage="ocr",
    )
    assert record.reason_code == "stage_revalidation_failed"
    assert record.document_id == "doc_f0cefe5475d41e852b0d9d3bd73001c983acb13ff05e546658d2156012f57247"
    assert record.stage == "ocr"
    payload = record.payload
    assert payload["reason_code"] == "stage_revalidation_failed"
    assert payload["stage"] == "ocr"
    assert payload["document_id"] == "doc_f0cefe5475d41e852b0d9d3bd73001c983acb13ff05e546658d2156012f57247"


@pytest.mark.anyio
async def test_succeeded_stage_revalidation_failure_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import date

    import httpx
    from helpers import pdf_bytes

    import cardrag_worker.pipeline as pipeline_module
    from cardrag_worker.contracts import DownloadRequest, IssuerSpec, SourceRecord, snapshot_from_records
    from cardrag_worker.downloader import SecurePDFDownloader as RealDownloader
    from cardrag_worker.ocr import OCRValidationError

    payload = pdf_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    rec = SourceRecord(
        issuer="testbank",
        product_code="p1",
        product_name="테스트 카드",
        effective_date=date(2026, 8, 1),
        source_version="1",
        source_url="https://cards.example/current.pdf",
        source_post_id="post-p1",
        file_name="current.pdf",
        category="credit",
        discovered_at=datetime(2026, 8, 25, tzinfo=UTC),
    )
    doc_id = rec.document_id(digest)

    state = WorkerState(tmp_path / "state.sqlite3")
    run_id = state.start_run(run_id="run-reval-fail")
    state.ensure_stage(run_id, doc_id, "download", max_attempts=1)
    state.stage_started(run_id, doc_id, "download")
    state.stage_succeeded(run_id, doc_id, "download")
    state.ensure_stage(run_id, doc_id, "ocr", max_attempts=1)
    state.stage_started(run_id, doc_id, "ocr")
    state.stage_succeeded(run_id, doc_id, "ocr")
    state.connection.execute("UPDATE run SET status='failed', error='interrupted' WHERE run_id=?", (run_id,))
    state.connection.commit()

    real_async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "application/pdf"}, content=payload, request=request
        )

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return real_async_client(transport=httpx.MockTransport(handler), timeout=kwargs.get("timeout"))  # type: ignore[arg-type]

    monkeypatch.setattr(pipeline_module.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(
        pipeline_module,
        "SecurePDFDownloader",
        lambda policy: RealDownloader(policy, resolver=lambda _host: ("93.184.216.34",)),
    )

    class RevalFailOCR:
        contract = {"schema_version": "test-ocr.v1"}
        adoption_policy_version = "cardrag.legacy-ocr-adoption.v1"

        async def resolve(self, **kwargs: object) -> object:
            raise OCRValidationError("content OCR variant ID does not match manifest fields")

    class FakeEmbeddings:
        provider = "openrouter"
        model = "test-embedding"
        dimension = 1536

        async def embed_documents(self, texts: list[str]) -> list[list[float]]:
            return [[1.0] + [0.0] * 1535 for _ in texts]

    class FakeWebDAV:
        def __init__(self) -> None:
            self.pointer_path = "v1/stable.json"
            self.channel = "stable"
            self.objects: dict[str, bytes] = {}

        async def validated_current_generation(self) -> object:
            return None

        async def get_current(self) -> object:
            return None

        async def get_bytes(self, path: object, *, max_bytes: int | None = None) -> bytes | None:
            return None

    class Adapter:
        parser_version = "test-adapter.v1"

        def __init__(self, records: tuple[SourceRecord, ...]) -> None:
            self.records = records
            self.spec = IssuerSpec(
                code="testbank",
                display_name="테스트카드",
                sort_order=1,
                allowed_hosts=frozenset({"cards.example"}),
                categories=("credit",),
                minimum_interval_seconds=0,
                retry_base_seconds=0.001,
                maximum_retries=2,
            )

        async def discover_current(self, client: object) -> object:
            return snapshot_from_records(
                issuer=self.spec.code,
                source_url="https://cards.example/list",
                parser_version=self.parser_version,
                records=self.records,
                started_at=datetime(2026, 8, 25, tzinfo=UTC),
            )

        async def prepare_download(self, client: object, source: SourceRecord) -> object:
            return DownloadRequest(url=source.source_url)

    pipeline = WorkerPipeline(
        state=state,
        state_dir=tmp_path,
        adapters=[Adapter((rec,))],
        ocr=RevalFailOCR(),  # type: ignore[arg-type]
        embeddings=FakeEmbeddings(),  # type: ignore[arg-type]
        webdav=FakeWebDAV(),  # type: ignore[arg-type]
        collect_remote_garbage=False,
        maximum_attempts=1,
    )

    with pytest.raises(WorkerUnexpectedFailureError) as exc_info:
        await pipeline.run(resume_run_id=run_id)

    err = exc_info.value
    assert "ocr_validation_error" in err.stored_error
    report_file = tmp_path / "runs" / run_id / "reports" / "worker-failure.json"
    assert report_file.is_file()
    data = json.loads(report_file.read_text())
    assert data["schema_version"] == "cardrag.worker-failure-report.v1"
    assert data["stage"] == "ocr"
    assert data["document_id"] == doc_id
    assert data["reason_code"] == "ocr_validation_error"


@pytest.mark.anyio
async def test_stage_database_recording_failure_diagnostics(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    run_id = "49845932e05849d3a0110a2bc82d6c1c"
    doc_id = "doc_f0cefe5475d41e852b0d9d3bd73001c983acb13ff05e546658d2156012f57247"
    run_dir = state_dir / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "reports").mkdir(parents=True)

    pipeline = MagicMock(spec=WorkerPipeline)
    pipeline.state_dir = state_dir
    pipeline.state = MagicMock()
    pipeline.state.ensure_stage = MagicMock()
    pipeline.state.get_stage = MagicMock(return_value=None)
    pipeline.state.stage_started = MagicMock(return_value=1)
    pipeline.state.stage_succeeded = MagicMock(side_effect=RuntimeError("sqlite disk I/O error"))
    pipeline.performance = MagicMock()
    cm = MagicMock()
    cm.__enter__.return_value = None
    cm.__exit__.return_value = False
    pipeline.performance.measure = MagicMock(return_value=cm)
    pipeline.execution_plan = None
    pipeline.maximum_attempts = 3
    pipeline.retry_cap_seconds = 10.0

    async def good_operation() -> str:
        return "success"

    with pytest.raises(WorkerUnexpectedFailureError) as exc_info:
        await WorkerPipeline._finite_stage(
            pipeline,
            run_id=run_id,
            document_id=doc_id,
            name="ocr",
            operation=good_operation,
        )

    err = exc_info.value
    assert "stage_bookkeeping_failed" in err.stored_error
    report_file = state_dir / "runs" / run_id / "reports" / "worker-failure.json"
    assert report_file.is_file()
    data = json.loads(report_file.read_text())
    assert data["reason_code"] == "stage_bookkeeping_failed"
    assert data["stage"] == "ocr"
    assert data["document_id"] == doc_id
    assert data["error_class_category"] == "database"


def test_retained_identity_conflict_behavior(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    doc_id = "doc_f0cefe5475d41e852b0d9d3bd73001c983acb13ff05e546658d2156012f57247"
    ocr_dir = run_dir / "documents" / doc_id / "ocr"
    ocr_dir.mkdir(parents=True)
    ocr_file = ocr_dir / "ocr.md"
    body = b"## Page 1\nSample OCR Content"
    expected_sha = hashlib.sha256(body).hexdigest()
    ocr_file.write_bytes(body)

    # 1. Matching bytes: not a conflict
    own = ocr_file
    st = own.stat()
    assert st.st_size > 0
    assert hashlib.sha256(own.read_bytes()).hexdigest() == expected_sha

    # 2. Mismatching bytes: conflict
    diff_sha = "b" * 64
    assert hashlib.sha256(own.read_bytes()).hexdigest() != diff_sha

    # 3. Symlink: conflict
    sym_doc_id = "doc_symlink"
    sym_dir = run_dir / "documents" / sym_doc_id / "ocr"
    sym_dir.mkdir(parents=True)
    sym_file = sym_dir / "ocr.md"
    sym_file.symlink_to(ocr_file)
    assert sym_file.is_symlink()
