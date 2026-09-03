from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from cardrag_core import (
    ArtifactRef,
    EmbeddingContract,
    GenerationCounts,
    GenerationDocument,
    GenerationManifest,
    GenerationOCRFailure,
    IssuerOCRCounts,
    canonical_json_bytes,
    generation_database_path,
    generation_manifest_path,
    sha256_file,
)

from cardrag_worker.contracts import DocumentRecord, EvidenceRecord, IssuerSpec, PageRecord
from cardrag_worker.exporter import ServingDatabaseExporter
from cardrag_worker.pipeline import (
    PipelineResult,
    SealedPublicationResumer,
    WorkerPipeline,
    WorkerUnexpectedFailureError,
    resume_sealed_publication,
)
from cardrag_worker.state import AlreadyRunning, WorkerState, worker_lock
from cardrag_worker.webdav import PublishedBundle, RemoteGenerationIdentity

PUBLICATION_RUN_ID = "a" * 32


class DummyOCR:
    contract = {"schema_version": "ocr.test.v1"}
    adoption_policy_version = "cardrag.legacy-ocr-adoption.v1"


class DummyEmbedding:
    provider = "openrouter"
    model = "embed"
    dimension = 1536

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return []


class DummyAdapter:
    parser_version = "adapter.test.v1"
    spec = IssuerSpec(
        code="kb",
        display_name="KB국민카드",
        sort_order=1,
        allowed_hosts=frozenset({"kb.example"}),
        categories=("credit",),
        maximum_retries=1,
    )

    async def discover_current(self, client: Any) -> None:
        del client
        raise NotImplementedError("fresh discovery was attempted")


class NoWriteWebDAV:
    def __init__(self, current: RemoteGenerationIdentity | None = None) -> None:
        self.current = current
        self.put_calls = 0
        self.pointer_path = "v1/stable.json"
        self.channel = "stable"

    async def validated_current_generation(self) -> RemoteGenerationIdentity | None:
        return self.current

    async def get_bytes(self, path: object, *, max_bytes: int | None = None) -> bytes | None:
        return b"stable" if self.current else None

    async def put_cas_file(
        self,
        path: Path,
        *,
        media_type: str,
        expected_sha256: str | None = None,
        expected_size_bytes: int | None = None,
    ) -> tuple[str, str]:
        del path, media_type, expected_sha256, expected_size_bytes
        self.put_calls += 1
        raise AssertionError("invalid seal must not write")


class CancelReconciliationWebDAV(NoWriteWebDAV):
    def __init__(self) -> None:
        super().__init__()
        self.remote_manifest: bytes | None = None

    async def get_bytes(self, path: object, *, max_bytes: int | None = None) -> bytes | None:
        del max_bytes
        if self.current is not None and path == generation_manifest_path(self.current.generation_id):
            return self.remote_manifest
        return b"stable" if self.current else None


def build_seal(root: Path, run_id: str = "run-sealed") -> dict[str, Any]:
    run_root = root / "runs" / run_id
    sealed_root = run_root / "sealed"
    sealed_root.mkdir(parents=True)
    pdf_path = run_root / "downloads" / "source.pdf"
    ocr_path = run_root / "documents" / "doc_kb" / "ocr" / "ocr.md"
    pdf_path.parent.mkdir(parents=True)
    ocr_path.parent.mkdir(parents=True)
    pdf_path.write_bytes(b"%PDF-sealed-test")
    page_text = "카드 상품의 혜택과 제외 조건을 정확히 설명하는 페이지 본문입니다."
    ocr_path.write_text(f"## Page 1\n\n{page_text}\n", encoding="utf-8")
    pdf_sha, pdf_size = sha256_file(pdf_path)
    ocr_sha, ocr_size = sha256_file(ocr_path)
    document = DocumentRecord(
        document_id="doc_kb",
        issuer="kb",
        product_code="p1",
        product_name="테스트 카드",
        title="테스트 카드",
        pdf_sha256=pdf_sha,
        pdf_size_bytes=pdf_size,
        page_count=1,
        pages=(PageRecord(document_id="doc_kb", page=1, text=page_text),),
    )
    evidence = EvidenceRecord(
        evidence_id="evidence_1",
        document_id="doc_kb",
        page_start=1,
        page_end=1,
        section_type="body",
        text=page_text,
        source_start=0,
        source_end=len(page_text),
        embedding=[1.0] + [0.0] * 1535,
    )
    corpus_sha = "c" * 64
    contract_sha = "d" * 64
    generation_id = (
        f"g-{run_id[:24]}-{corpus_sha[:12]}"
        if len(run_id) == 32 and all(character in "0123456789abcdef" for character in run_id)
        else "g-sealed"
    )
    database_path = sealed_root / "index.sqlite3"
    export = ServingDatabaseExporter().export(
        database_path,
        generation_id=generation_id,
        corpus_sha256=corpus_sha,
        contract_sha256=contract_sha,
        embedding_provider="openrouter",
        embedding_model="embed",
        issuers=[DummyAdapter.spec],
        documents=[document],
        evidence=[evidence],
    )
    manifest = GenerationManifest(
        schema_version="cardrag.generation.v4",
        generation_id=generation_id,
        created_at=datetime(2026, 8, 25, tzinfo=UTC),
        serving_schema="cardrag.serving-db.v4",
        serving_database=ArtifactRef(
            sha256=export.sha256,
            size_bytes=export.size_bytes,
            media_type="application/vnd.sqlite3",
            path=generation_database_path(generation_id).as_posix(),
        ),
        corpus_sha256=corpus_sha,
        contract_sha256=contract_sha,
        embedding_contract=EmbeddingContract(provider="openrouter", model="embed", dimension=1536, count=1),
        issuer_codes=("kb",),
        counts=GenerationCounts(documents=1, pdf_objects=1, ocr_objects=1, chunks=1),
        documents=(
            GenerationDocument(
                document_id="doc_kb",
                issuer="kb",
                pdf=ArtifactRef.for_cas(sha256=pdf_sha, size_bytes=pdf_size, media_type="application/pdf"),
                ocr=ArtifactRef.for_cas(
                    sha256=ocr_sha,
                    size_bytes=ocr_size,
                    media_type="text/markdown; charset=utf-8",
                ),
                page_count=1,
                availability="available",
            ),
        ),
        issuer_ocr_counts=(IssuerOCRCounts(issuer="kb", acquired=1, succeeded=1, failed=0),),
    )
    seal = {
        "schema_version": "cardrag.worker-seal.v1",
        "run_id": run_id,
        "generation_id": generation_id,
        "corpus_sha256": corpus_sha,
        "contract_sha256": contract_sha,
        "database_path": str(database_path),
        "database_sha256": export.sha256,
        "database_size_bytes": export.size_bytes,
        "manifest": manifest.model_dump(mode="json"),
        "objects": [
            {
                "path": str(pdf_path),
                "sha256": pdf_sha,
                "size_bytes": pdf_size,
                "media_type": "application/pdf",
            },
            {
                "path": str(ocr_path),
                "sha256": ocr_sha,
                "size_bytes": ocr_size,
                "media_type": "text/markdown; charset=utf-8",
            },
        ],
    }
    (sealed_root / "publish.json").write_bytes(canonical_json_bytes(seal))
    return seal


def pipeline(root: Path, state: WorkerState, webdav: NoWriteWebDAV) -> WorkerPipeline:
    return WorkerPipeline(
        state=state,
        state_dir=root,
        adapters=[DummyAdapter()],  # type: ignore[arg-type]
        ocr=DummyOCR(),  # type: ignore[arg-type]
        embeddings=DummyEmbedding(),
        webdav=webdav,  # type: ignore[arg-type]
        collect_remote_garbage=False,
    )


@pytest.mark.asyncio
async def test_seal_exact_set_is_validated_before_any_remote_write(tmp_path: Path) -> None:
    seal = build_seal(tmp_path)
    seal["objects"] = seal["objects"][:-1]
    webdav = NoWriteWebDAV()
    with WorkerState(tmp_path / "state.sqlite3") as state:
        worker = pipeline(tmp_path, state, webdav)
        with pytest.raises(RuntimeError, match="exactly match"):
            await worker._publish_remote_only(seal)
    assert webdav.put_calls == 0


@pytest.mark.asyncio
async def test_stale_seal_cannot_be_rechained_over_a_newer_head(tmp_path: Path) -> None:
    seal = build_seal(tmp_path)
    webdav = NoWriteWebDAV()
    with WorkerState(tmp_path / "state.sqlite3") as state:
        worker = pipeline(tmp_path, state, webdav)
        validated = await worker._validate_local_seal(seal)
        with pytest.raises(RuntimeError, match="superseded"):
            await worker._align_seal_to_current(
                seal,
                validated=validated,
                current_generation_id="g-new-head",
            )


@pytest.mark.asyncio
async def test_explicit_resume_refreshes_discovery_before_using_a_seal(tmp_path: Path) -> None:
    build_seal(tmp_path)
    webdav = NoWriteWebDAV()
    with WorkerState(tmp_path / "state.sqlite3") as state:
        state.start_run(run_id="run-sealed")
        state.ensure_stage("run-sealed", "issuer-kb", "discovery", max_attempts=1)
        state.stage_started("run-sealed", "issuer-kb", "discovery")
        state.stage_succeeded("run-sealed", "issuer-kb", "discovery")
        state.record_snapshot(
            run_id="run-sealed",
            snapshot_id="a" * 64,
            issuer="kb",
            source_sha256="a" * 64,
            record_count=1,
            payload={"stale": True},
        )
        state.finish_run("run-sealed", "failed", error="publish interrupted")
        worker = pipeline(tmp_path, state, webdav)
        with pytest.raises(WorkerUnexpectedFailureError) as captured:
            await worker.run(resume_run_id="run-sealed")
        assert captured.value.failure.error_class_category == "runtime"
        assert captured.value.report_path.is_file()
    assert webdav.put_calls == 0


@pytest.mark.asyncio
async def test_resume_after_stable_activation_recovers_publish_row_idempotently(
    tmp_path: Path,
) -> None:
    seal = build_seal(tmp_path)
    seal["ocr_cache_publication_deferred"] = 1
    seal_path = tmp_path / "runs" / "run-sealed" / "sealed" / "publish.json"
    seal_path.write_bytes(canonical_json_bytes(seal))
    resumed_seal = json.loads(seal_path.read_bytes())
    manifest = GenerationManifest.model_validate_json(canonical_json_bytes(seal["manifest"]))
    webdav = CancelReconciliationWebDAV()
    webdav.current = RemoteGenerationIdentity(
        generation_id=manifest.generation_id,
        corpus_sha256=manifest.corpus_sha256,
        contract_sha256=manifest.contract_sha256,
    )
    webdav.remote_manifest = manifest.canonical_bytes()
    with WorkerState(tmp_path / "state.sqlite3") as state:
        state.start_run(run_id="run-sealed")
        worker = pipeline(tmp_path, state, webdav)
        first = await worker._publish_sealed("run-sealed", resumed_seal)
        second = await worker._publish_sealed("run-sealed", resumed_seal)
        assert state.ready_publish(manifest.corpus_sha256, manifest.contract_sha256) is not None
    assert first.status == second.status == "succeeded"
    assert first.ocr_cache_publication_deferred == second.ocr_cache_publication_deferred == 1


@pytest.mark.asyncio
async def test_same_generation_resume_requires_exact_manifest_and_failure_count(
    tmp_path: Path,
) -> None:
    seal = build_seal(tmp_path)
    manifest = GenerationManifest.model_validate_json(canonical_json_bytes(seal["manifest"]))
    webdav = CancelReconciliationWebDAV()
    webdav.current = RemoteGenerationIdentity(
        generation_id=manifest.generation_id,
        corpus_sha256=manifest.corpus_sha256,
        contract_sha256=manifest.contract_sha256,
        ocr_failed_document_count=1,
    )
    webdav.remote_manifest = manifest.canonical_bytes()

    with WorkerState(tmp_path / "state.sqlite3") as state:
        state.start_run(run_id="run-sealed")
        worker = pipeline(tmp_path, state, webdav)
        with pytest.raises(RuntimeError, match="does not match"):
            await worker._publish_sealed("run-sealed", seal)

        assert state.ready_publish(manifest.corpus_sha256, manifest.contract_sha256) is None


@pytest.mark.parametrize(
    ("remote_manifest_matches", "expected_status", "expect_publish"),
    [(True, "succeeded", True), (False, "interrupted", False)],
)
@pytest.mark.asyncio
async def test_run_cancellation_reconciles_only_an_exact_committed_manifest(
    tmp_path: Path,
    remote_manifest_matches: bool,
    expected_status: str,
    expect_publish: bool,
) -> None:
    seal = build_seal(tmp_path)
    manifest = GenerationManifest.model_validate_json(canonical_json_bytes(seal["manifest"]))
    webdav = CancelReconciliationWebDAV()
    with WorkerState(tmp_path / "state.sqlite3") as state:
        state.start_run(run_id="run-sealed")
        state.finish_run("run-sealed", "failed", error="prior publication attempt was interrupted")
        worker = pipeline(tmp_path, state, webdav)

        async def commit_then_cancel(run_id: str, *, refresh_sources: bool = False) -> Any:
            assert (run_id, refresh_sources) == ("run-sealed", True)
            webdav.current = RemoteGenerationIdentity(
                generation_id=manifest.generation_id,
                corpus_sha256=manifest.corpus_sha256,
                contract_sha256=manifest.contract_sha256,
            )
            webdav.remote_manifest = (
                manifest.canonical_bytes() if remote_manifest_matches else b'{"different":true}\n'
            )
            raise asyncio.CancelledError

        worker._run_locked = commit_then_cancel  # type: ignore[method-assign]
        with pytest.raises(asyncio.CancelledError):
            await worker.run(resume_run_id="run-sealed")

        row = state.connection.execute(
            "SELECT status,error,corpus_sha256,contract_sha256 FROM run WHERE run_id='run-sealed'"
        ).fetchone()
        assert row is not None
        assert row["status"] == expected_status
        publish = state.ready_publish(manifest.corpus_sha256, manifest.contract_sha256)
        assert (publish is not None) is expect_publish
        if expect_publish:
            assert tuple(row)[1:] == (None, manifest.corpus_sha256, manifest.contract_sha256)
            assert publish is not None
            assert publish["generation_id"] == manifest.generation_id
        else:
            assert row["error"] == "worker_cancelled: Pipeline execution was interrupted."


@pytest.mark.asyncio
async def test_publish_sealed_reconciles_exact_commit_after_pointer_readback_failure(
    tmp_path: Path,
) -> None:
    raw_sentinel = "RAW_STABLE_POINTER_READBACK_SECRET"
    seal = build_seal(tmp_path)
    manifest = GenerationManifest.model_validate_json(canonical_json_bytes(seal["manifest"]))
    webdav = CancelReconciliationWebDAV()
    with WorkerState(tmp_path / "state.sqlite3") as state:
        state.start_run(run_id="run-sealed")
        worker = pipeline(tmp_path, state, webdav)

        async def commit_then_fail(
            _sealed: object,
            *,
            validated: object | None = None,
        ) -> Any:
            assert validated is not None
            webdav.current = RemoteGenerationIdentity(
                generation_id=manifest.generation_id,
                corpus_sha256=manifest.corpus_sha256,
                contract_sha256=manifest.contract_sha256,
            )
            webdav.remote_manifest = manifest.canonical_bytes()
            raise RuntimeError(raw_sentinel)

        worker._publish_remote_only = commit_then_fail  # type: ignore[method-assign]
        result = await worker._publish_sealed("run-sealed", seal)

        row = state.connection.execute(
            "SELECT status,error,corpus_sha256,contract_sha256 FROM run WHERE run_id='run-sealed'"
        ).fetchone()
        assert row is not None
        assert tuple(row) == ("succeeded", None, manifest.corpus_sha256, manifest.contract_sha256)
        publish = state.ready_publish(manifest.corpus_sha256, manifest.contract_sha256)
        assert publish is not None
        assert publish["generation_id"] == manifest.generation_id

    assert result.status == "succeeded"
    assert result.generation_id == manifest.generation_id
    assert raw_sentinel not in json.dumps(dict(row))


@pytest.mark.asyncio
async def test_legacy_seal_without_deferred_count_defaults_to_zero(tmp_path: Path) -> None:
    seal = build_seal(tmp_path)
    webdav = NoWriteWebDAV()
    with WorkerState(tmp_path / "state.sqlite3") as state:
        worker = pipeline(tmp_path, state, webdav)
        validated = await worker._validate_local_seal(seal)
    assert validated.ocr_cache_publication_deferred == 0


@pytest.mark.parametrize("invalid", [True, -1, 2, "1", 1.0])
@pytest.mark.asyncio
async def test_seal_rejects_invalid_deferred_count_before_remote_write(
    tmp_path: Path,
    invalid: object,
) -> None:
    seal = build_seal(tmp_path)
    seal["ocr_cache_publication_deferred"] = invalid
    webdav = NoWriteWebDAV()
    with WorkerState(tmp_path / "state.sqlite3") as state:
        worker = pipeline(tmp_path, state, webdav)
        with pytest.raises(RuntimeError, match="deferred count is invalid"):
            await worker._publish_remote_only(seal)
    assert webdav.put_calls == 0


@pytest.mark.asyncio
async def test_seal_deferred_count_cannot_include_ocr_failed_documents(tmp_path: Path) -> None:
    seal = build_seal(tmp_path)
    manifest = GenerationManifest.model_validate_json(canonical_json_bytes(seal["manifest"]))
    available = manifest.documents[0]
    available_documents = tuple(
        available.model_copy(update={"document_id": f"doc_kb_{index:02d}"}) for index in range(19)
    )
    failed = GenerationDocument(
        document_id="doc_kb_19",
        issuer="kb",
        pdf=available.pdf,
        page_count=available.page_count,
        availability="ocr_failed",
        ocr_failure=GenerationOCRFailure(
            reason_code="provider_document_rejected",
            reason="The OCR provider could not process this document.",
            attempts=1,
        ),
    )
    expanded = GenerationManifest.model_validate_json(
        canonical_json_bytes(
            manifest.model_copy(
                update={
                    "counts": manifest.counts.model_copy(update={"documents": 20}),
                    "documents": tuple(
                        sorted((*available_documents, failed), key=lambda row: row.document_id)
                    ),
                    "issuer_ocr_counts": (IssuerOCRCounts(issuer="kb", acquired=20, succeeded=19, failed=1),),
                }
            )
        )
    )
    seal["manifest"] = expanded.model_dump(mode="json")
    seal["ocr_cache_publication_deferred"] = 20
    webdav = NoWriteWebDAV()

    with WorkerState(tmp_path / "state.sqlite3") as state:
        worker = pipeline(tmp_path, state, webdav)
        with pytest.raises(RuntimeError, match="deferred count is invalid"):
            await worker._publish_remote_only(seal)

    assert webdav.put_calls == 0


@pytest.mark.asyncio
async def test_partial_current_generation_does_not_suppress_replacement_seal(tmp_path: Path) -> None:
    seal = build_seal(tmp_path)
    manifest = GenerationManifest.model_validate_json(canonical_json_bytes(seal["manifest"]))
    manifest = manifest.model_copy(update={"previous_generation_id": "g-partial"})
    seal["manifest"] = manifest.model_dump(mode="json")
    webdav = NoWriteWebDAV(
        RemoteGenerationIdentity(
            generation_id="g-partial",
            corpus_sha256=manifest.corpus_sha256,
            contract_sha256=manifest.contract_sha256,
            generation_schema="cardrag.generation.v4",
            serving_schema="cardrag.serving-db.v4",
            ocr_failed_document_count=1,
        )
    )
    with WorkerState(tmp_path / "state.sqlite3") as state:
        state.start_run(run_id="run-sealed")
        worker = pipeline(tmp_path, state, webdav)
        publish_calls = 0

        async def publish_replacement(
            current_seal: dict[str, Any],
            *,
            validated: Any | None = None,
        ) -> tuple[PublishedBundle, Any]:
            nonlocal publish_calls
            publish_calls += 1
            assert validated is not None
            return (
                PublishedBundle(
                    generation_id=validated.manifest.generation_id,
                    index_sha256=validated.manifest.serving_database.sha256,
                    manifest_sha256=validated.manifest.manifest_sha256,
                ),
                validated,
            )

        worker._publish_remote_only = publish_replacement  # type: ignore[method-assign]
        result = await worker._publish_sealed("run-sealed", seal)

    assert publish_calls == 1
    assert result.status == "succeeded"
    assert result.generation_id == manifest.generation_id


@pytest.mark.asyncio
async def test_publication_failure_preserves_safe_phase_and_errno_for_resume(
    tmp_path: Path,
) -> None:
    raw_sentinel = "RAW_ENOSPC_PATH_TOKEN_SECRET"
    seal = build_seal(tmp_path)
    webdav = NoWriteWebDAV()

    with WorkerState(tmp_path / "state.sqlite3") as state:
        state.start_run(run_id="run-sealed")
        state.finish_run("run-sealed", "failed", error="prior publication attempt failed")
        worker = pipeline(tmp_path, state, webdav)
        validation_calls = 0
        validate_local_seal = worker._validate_local_seal

        async def count_validation(current_seal: dict[str, Any]) -> Any:
            nonlocal validation_calls
            validation_calls += 1
            return await validate_local_seal(current_seal)

        async def fail_publication(
            _sealed: object,
            *,
            validated: object | None = None,
        ) -> Any:
            assert validated is not None
            raise OSError(28, raw_sentinel)

        async def no_remote_commit(_manifest: object) -> None:
            return None

        async def publish_from_retained_seal(
            run_id: str,
            *,
            refresh_sources: bool = False,
        ) -> Any:
            assert (run_id, refresh_sources) == ("run-sealed", True)
            validated = await worker._validate_local_seal(seal)
            return await worker._publish_sealed(run_id, seal, validated=validated)

        worker._validate_local_seal = count_validation  # type: ignore[method-assign]
        worker._publish_remote_only = fail_publication  # type: ignore[method-assign]
        worker._reconcile_remote_bundle = no_remote_commit  # type: ignore[method-assign]
        worker._run_locked = publish_from_retained_seal  # type: ignore[method-assign]

        with pytest.raises(WorkerUnexpectedFailureError) as captured:
            await worker.run(resume_run_id="run-sealed")

        assert validation_calls == 1
        assert captured.value.failure.error_class_category == "local_io"
        assert captured.value.failure.phase == "remote_publication"
        assert captured.value.failure.errno == 28
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None
        report = captured.value.report_path.read_text(encoding="utf-8")
        run_error = state.connection.execute("SELECT error FROM run WHERE run_id='run-sealed'").fetchone()[0]
        assert state.ready_publish("c" * 64, "d" * 64) is None
        assert raw_sentinel not in report
        assert raw_sentinel not in str(run_error)
        assert (tmp_path / "runs" / "run-sealed" / "sealed" / "publish.json").is_file()


def publication_resumer(
    root: Path,
    state: WorkerState,
    webdav: NoWriteWebDAV,
) -> SealedPublicationResumer:
    webdav.channel = "candidate-v1.0.11"
    webdav.pointer_path = "v1/channels/candidate-v1.0.11.json"
    return SealedPublicationResumer(
        state=state,
        state_dir=root,
        webdav=webdav,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["excluded-null", "implicit-default", "offset-time"])
async def test_publication_only_rejects_manifest_that_reencodes_differently(
    tmp_path: Path,
    mutation: str,
) -> None:
    seal = build_seal(tmp_path, PUBLICATION_RUN_ID)
    manifest = seal["manifest"]
    assert isinstance(manifest, dict)
    if mutation == "excluded-null":
        manifest["vector_sidecar"] = None
    elif mutation == "implicit-default":
        del manifest["previous_generation_id"]
    else:
        manifest["created_at"] = "2026-08-25T09:00:00+09:00"
    seal_path = tmp_path / "runs" / PUBLICATION_RUN_ID / "sealed" / "publish.json"
    seal_path.write_bytes(canonical_json_bytes(seal))
    (tmp_path / "pdf-cache" / "objects" / "sha256").mkdir(parents=True)
    webdav = NoWriteWebDAV()
    with WorkerState(tmp_path / "state.sqlite3") as state:
        state.start_run(run_id=PUBLICATION_RUN_ID)
        state.finish_run(PUBLICATION_RUN_ID, "failed", error="prior publication failed")
        worker = publication_resumer(tmp_path, state, webdav)
        with (
            pytest.raises(WorkerUnexpectedFailureError) as captured,
            worker_lock(tmp_path / "worker.lock"),
        ):
            await worker._resume_publication_locked(PUBLICATION_RUN_ID)

    assert captured.value.failure.phase == "local_seal_validation"
    assert captured.value.failure.error_class_category == "runtime"
    assert webdav.put_calls == 0


def test_publication_only_seal_fifo_is_rejected_without_blocking(tmp_path: Path) -> None:
    build_seal(tmp_path, PUBLICATION_RUN_ID)
    seal_path = tmp_path / "runs" / PUBLICATION_RUN_ID / "sealed" / "publish.json"
    seal_path.unlink()
    os.mkfifo(seal_path, mode=0o600)
    webdav = NoWriteWebDAV()
    with WorkerState(tmp_path / "state.sqlite3") as state:
        worker = publication_resumer(tmp_path, state, webdav)
        with pytest.raises(RuntimeError, match="bounded regular file"):
            worker._load_seal(PUBLICATION_RUN_ID)


@pytest.mark.asyncio
async def test_seal_artifacts_are_bound_to_the_declared_run_directory(tmp_path: Path) -> None:
    seal = build_seal(tmp_path, PUBLICATION_RUN_ID)
    original_database = Path(str(seal["database_path"]))
    other_database = tmp_path / "runs" / ("b" * 32) / "sealed" / "index.sqlite3"
    other_database.parent.mkdir(parents=True)
    other_database.write_bytes(original_database.read_bytes())
    seal["database_path"] = str(other_database)
    pipeline = object.__new__(WorkerPipeline)
    pipeline.state_dir = tmp_path
    pipeline.pdf_cache = SimpleNamespace(objects_root=tmp_path / "pdf-cache" / "objects" / "sha256")
    pipeline.document_aggregation = None
    pipeline.pdf_cache.objects_root.mkdir(parents=True)

    with pytest.raises(RuntimeError, match="escapes approved worker storage"):
        await pipeline._validate_local_seal(seal)


@pytest.mark.asyncio
async def test_publication_api_holds_lock_while_existing_state_is_opened(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with WorkerState(tmp_path / "worker-state.sqlite3"):
        pass
    webdav = NoWriteWebDAV()
    webdav.channel = "candidate-v1.0.11"
    webdav.pointer_path = "v1/channels/candidate-v1.0.11.json"

    async def observe_lock(
        self: SealedPublicationResumer,
        run_id: str,
    ) -> PipelineResult:
        assert run_id == PUBLICATION_RUN_ID
        with pytest.raises(AlreadyRunning), worker_lock(tmp_path / "worker.lock"):
            pass
        return PipelineResult(
            run_id=run_id,
            status="succeeded",
            corpus_sha256="c" * 64,
            contract_sha256="d" * 64,
            generation_id="g-sealed",
            document_count=1,
            evidence_count=1,
        )

    monkeypatch.setattr(SealedPublicationResumer, "_resume_publication_locked", observe_lock)
    result = await resume_sealed_publication(
        run_id=PUBLICATION_RUN_ID,
        state_dir=tmp_path,
        webdav=webdav,  # type: ignore[arg-type]
    )

    assert result.status == "succeeded"


@pytest.mark.asyncio
async def test_publication_only_resume_uses_one_seal_validation_and_no_providers(
    tmp_path: Path,
) -> None:
    seal = build_seal(tmp_path, PUBLICATION_RUN_ID)
    (tmp_path / "pdf-cache" / "objects" / "sha256").mkdir(parents=True)
    manifest = GenerationManifest.model_validate_json(canonical_json_bytes(seal["manifest"]))
    webdav = NoWriteWebDAV()
    with WorkerState(tmp_path / "state.sqlite3") as state:
        state.start_run(run_id=PUBLICATION_RUN_ID)
        state.finish_run(PUBLICATION_RUN_ID, "failed", error="prior remote publication failed")
        worker = publication_resumer(tmp_path, state, webdav)
        validation_calls = 0
        validate = worker._validate_local_seal

        async def count_validation(current_seal: dict[str, Any]) -> Any:
            nonlocal validation_calls
            validation_calls += 1
            return await validate(current_seal)

        async def publish_exact(
            _sealed: object,
            *,
            validated: Any | None = None,
        ) -> tuple[PublishedBundle, Any]:
            assert validated is not None
            return (
                PublishedBundle(
                    generation_id=manifest.generation_id,
                    index_sha256=manifest.serving_database.sha256,
                    manifest_sha256=manifest.manifest_sha256,
                ),
                validated,
            )

        worker._validate_local_seal = count_validation  # type: ignore[method-assign]
        worker._publish_remote_only = publish_exact  # type: ignore[method-assign]
        with worker_lock(tmp_path / "worker.lock"):
            result = await worker._resume_publication_locked(PUBLICATION_RUN_ID)
        row = state.connection.execute(
            "SELECT status,error,corpus_sha256,contract_sha256 FROM run WHERE run_id=?",
            (PUBLICATION_RUN_ID,),
        ).fetchone()

    assert validation_calls == 1
    assert result.status == "succeeded"
    assert tuple(row) == ("succeeded", None, manifest.corpus_sha256, manifest.contract_sha256)
    assert not hasattr(worker, "adapters")
    assert not hasattr(worker, "ocr")
    assert not hasattr(worker, "embeddings")


@pytest.mark.asyncio
async def test_publication_only_resume_reconciles_exact_commit_without_revalidation(
    tmp_path: Path,
) -> None:
    seal = build_seal(tmp_path, PUBLICATION_RUN_ID)
    (tmp_path / "pdf-cache" / "objects" / "sha256").mkdir(parents=True)
    manifest = GenerationManifest.model_validate_json(canonical_json_bytes(seal["manifest"]))
    webdav = CancelReconciliationWebDAV()
    with WorkerState(tmp_path / "state.sqlite3") as state:
        state.start_run(run_id=PUBLICATION_RUN_ID)
        state.finish_run(PUBLICATION_RUN_ID, "failed", error="prior remote publication failed")
        worker = publication_resumer(tmp_path, state, webdav)
        validation_calls = 0
        validate = worker._validate_local_seal

        async def count_validation(current_seal: dict[str, Any]) -> Any:
            nonlocal validation_calls
            validation_calls += 1
            return await validate(current_seal)

        async def commit_then_fail(
            _sealed: object,
            *,
            validated: Any | None = None,
        ) -> Any:
            assert validated is not None
            webdav.current = RemoteGenerationIdentity(
                generation_id=manifest.generation_id,
                corpus_sha256=manifest.corpus_sha256,
                contract_sha256=manifest.contract_sha256,
            )
            webdav.remote_manifest = manifest.canonical_bytes()
            raise RuntimeError("untrusted pointer readback detail")

        worker._validate_local_seal = count_validation  # type: ignore[method-assign]
        worker._publish_remote_only = commit_then_fail  # type: ignore[method-assign]
        with worker_lock(tmp_path / "worker.lock"):
            result = await worker._resume_publication_locked(PUBLICATION_RUN_ID)

        assert validation_calls == 1
        assert result.status == "succeeded"
        assert state.ready_publish(manifest.corpus_sha256, manifest.contract_sha256) is not None


@pytest.mark.asyncio
async def test_publication_only_resume_recovers_running_row_after_pointer_commit(
    tmp_path: Path,
) -> None:
    """A hard kill after remote commit must not strand the sealed run forever."""

    seal = build_seal(tmp_path, PUBLICATION_RUN_ID)
    (tmp_path / "pdf-cache" / "objects" / "sha256").mkdir(parents=True)
    manifest = GenerationManifest.model_validate_json(canonical_json_bytes(seal["manifest"]))
    webdav = CancelReconciliationWebDAV()
    webdav.current = RemoteGenerationIdentity(
        generation_id=manifest.generation_id,
        corpus_sha256=manifest.corpus_sha256,
        contract_sha256=manifest.contract_sha256,
    )
    webdav.remote_manifest = manifest.canonical_bytes()
    with WorkerState(tmp_path / "state.sqlite3") as state:
        state.start_run(run_id=PUBLICATION_RUN_ID)
        # The row remains running exactly as it would after SIGKILL between a
        # successful pointer MOVE and local terminal bookkeeping.
        worker = publication_resumer(tmp_path, state, webdav)
        with worker_lock(tmp_path / "worker.lock"):
            result = await worker._resume_publication_locked(PUBLICATION_RUN_ID)

        row = state.connection.execute(
            "SELECT status,error FROM run WHERE run_id=?",
            (PUBLICATION_RUN_ID,),
        ).fetchone()

    assert result.status == "succeeded"
    assert result.generation_id == manifest.generation_id
    assert tuple(row) == ("succeeded", None)


@pytest.mark.asyncio
async def test_publication_only_failure_is_terminal_and_secret_safe(tmp_path: Path) -> None:
    raw_sentinel = "RAW_PUBLICATION_ONLY_PATH_TOKEN_SECRET"
    build_seal(tmp_path, PUBLICATION_RUN_ID)
    (tmp_path / "pdf-cache" / "objects" / "sha256").mkdir(parents=True)
    webdav = NoWriteWebDAV()
    with WorkerState(tmp_path / "state.sqlite3") as state:
        state.start_run(run_id=PUBLICATION_RUN_ID)
        state.finish_run(PUBLICATION_RUN_ID, "failed", error="prior remote publication failed")
        worker = publication_resumer(tmp_path, state, webdav)
        validation_calls = 0
        validate = worker._validate_local_seal

        async def count_validation(current_seal: dict[str, Any]) -> Any:
            nonlocal validation_calls
            validation_calls += 1
            return await validate(current_seal)

        async def fail_publication(
            _sealed: object,
            *,
            validated: Any | None = None,
        ) -> Any:
            assert validated is not None
            raise OSError(28, raw_sentinel)

        async def not_committed(_manifest: object) -> None:
            return None

        worker._validate_local_seal = count_validation  # type: ignore[method-assign]
        worker._publish_remote_only = fail_publication  # type: ignore[method-assign]
        worker._reconcile_remote_bundle = not_committed  # type: ignore[method-assign]

        with (
            pytest.raises(WorkerUnexpectedFailureError) as captured,
            worker_lock(tmp_path / "worker.lock"),
        ):
            await worker._resume_publication_locked(PUBLICATION_RUN_ID)

        row = state.connection.execute(
            "SELECT status,error FROM run WHERE run_id=?",
            (PUBLICATION_RUN_ID,),
        ).fetchone()
        report = captured.value.report_path.read_text(encoding="utf-8")

    assert validation_calls == 1
    assert captured.value.failure.phase == "remote_publication"
    assert captured.value.failure.error_class_category == "local_io"
    assert captured.value.failure.errno == 28
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert tuple(row)[0] == "failed"
    assert raw_sentinel not in str(tuple(row)[1])
    assert raw_sentinel not in report
