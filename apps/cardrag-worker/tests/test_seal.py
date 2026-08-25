from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from cardrag_core import (
    ArtifactRef,
    EmbeddingContract,
    GenerationCounts,
    GenerationDocument,
    GenerationManifest,
    canonical_json_bytes,
    generation_database_path,
    sha256_file,
)

from cardrag_worker.contracts import DocumentRecord, EvidenceRecord, IssuerSpec, PageRecord
from cardrag_worker.exporter import ServingDatabaseExporter
from cardrag_worker.pipeline import WorkerPipeline
from cardrag_worker.state import WorkerState
from cardrag_worker.webdav import RemoteGenerationIdentity


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

    async def validated_current_generation(self) -> RemoteGenerationIdentity | None:
        return self.current

    async def get_bytes(self, path: object, *, max_bytes: int | None = None) -> bytes | None:
        return b"stable" if self.current else None

    async def put_cas_file(self, path: Path, *, media_type: str) -> tuple[str, str]:
        self.put_calls += 1
        raise AssertionError("invalid seal must not write")


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
    generation_id = "g-sealed"
    corpus_sha = "c" * 64
    contract_sha = "d" * 64
    database_path = sealed_root / "index.sqlite3"
    export = ServingDatabaseExporter().export(
        database_path,
        generation_id=generation_id,
        corpus_sha256=corpus_sha,
        embedding_provider="openrouter",
        embedding_model="embed",
        issuers=[DummyAdapter.spec],
        documents=[document],
        evidence=[evidence],
        extra_metadata={"contract_sha256": contract_sha},
    )
    manifest = GenerationManifest(
        generation_id=generation_id,
        created_at=datetime(2026, 8, 25, tzinfo=UTC),
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
            ),
        ),
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
        with pytest.raises(NotImplementedError):
            await worker.run(resume_run_id="run-sealed")
    assert webdav.put_calls == 0


@pytest.mark.asyncio
async def test_resume_after_stable_activation_recovers_publish_row_idempotently(tmp_path: Path) -> None:
    seal = build_seal(tmp_path)
    manifest = GenerationManifest.model_validate_json(canonical_json_bytes(seal["manifest"]))
    webdav = NoWriteWebDAV(
        RemoteGenerationIdentity(
            generation_id=manifest.generation_id,
            corpus_sha256=manifest.corpus_sha256,
            contract_sha256=manifest.contract_sha256,
        )
    )
    with WorkerState(tmp_path / "state.sqlite3") as state:
        state.start_run(run_id="run-sealed")
        worker = pipeline(tmp_path, state, webdav)
        first = await worker._publish_sealed("run-sealed", seal)
        second = await worker._publish_sealed("run-sealed", seal)
        assert state.ready_publish(manifest.corpus_sha256, manifest.contract_sha256) is not None
    assert first.status == second.status == "succeeded"
