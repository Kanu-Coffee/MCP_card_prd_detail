from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from cardrag_core import (
    STABLE_POINTER_PATH,
    ArtifactRef,
    EmbeddingContract,
    GenerationCounts,
    GenerationDocument,
    GenerationManifest,
    GenerationPointer,
    GenerationReady,
    generation_database_path,
    generation_manifest_path,
    generation_ready_path,
    object_path,
    sha256_bytes,
    verify_ocr_bytes,
)

from cardrag_worker.ocr import OCRResolver
from cardrag_worker.ocr_recovery import (
    OCRRecoveryError,
    restore_ocr_seed_from_generation,
)
from cardrag_worker.providers import OCRProvider
from cardrag_worker.state import WorkerState

NOW = datetime(2026, 9, 28, tzinfo=UTC)


class DummyThrowingProvider(OCRProvider):
    provider = "external-forbidden"
    model = "forbidden-model"

    async def recognize(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("External OCR provider called unexpectedly! Mass re-processing regression.")


class FakeWebDAV:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.channel = "stable"

    async def get_bytes(self, path: str | PurePosixPath) -> bytes | None:
        return self.objects.get(str(path))


def build_test_remote() -> tuple[FakeWebDAV, str, list[dict[str, Any]]]:
    webdav = FakeWebDAV()
    gen_id = "g-recovery-test"

    doc_specs = [
        # Normal native doc
        {
            "id": "doc_native_1",
            "issuer": "shinhan",
            "body": "## Page 1\n\n신한카드 안내장 본문 내용 1입니다. 혜택 및 연회비 안내.\n",
            "is_paddle": False,
        },
        # Another doc sharing same CAS
        {
            "id": "doc_native_2_shared_cas",
            "issuer": "shinhan",
            "body": "## Page 1\n\n신한카드 안내장 본문 내용 1입니다. 혜택 및 연회비 안내.\n",
            "is_paddle": False,
        },
        # Local PaddleOCR doc (no remote ocr_cache_kind/reuse_key)
        {
            "id": "doc_paddle_1",
            "issuer": "kb",
            "body": "## Page 1\n\nKB국민카드 패들OCR 인식 본문입니다. 전월실적 30만원 이상.\n",
            "is_paddle": True,
        },
    ]

    gen_docs: list[GenerationDocument] = []
    for spec in doc_specs:
        body_bytes = spec["body"].encode("utf-8")
        verified = verify_ocr_bytes(body_bytes, expected_page_count=1)
        ocr_sha = verified.sha256
        ocr_size = verified.size_bytes
        ocr_path = object_path(ocr_sha).as_posix()
        webdav.objects[ocr_path] = body_bytes

        pdf_body = f"pdf_content_for_{spec['id']}".encode()
        pdf_sha = hashlib.sha256(pdf_body).hexdigest()
        pdf_path = object_path(pdf_sha).as_posix()
        webdav.objects[pdf_path] = pdf_body
        pdf_ref = ArtifactRef.for_cas(sha256=pdf_sha, size_bytes=len(pdf_body), media_type="application/pdf")
        ocr_ref = ArtifactRef.for_cas(
            sha256=ocr_sha, size_bytes=ocr_size, media_type="text/markdown; charset=utf-8"
        )

        reuse_key = sha256_bytes(f"reuse_{spec['id']}".encode()) if not spec["is_paddle"] else None
        gen_doc = GenerationDocument(
            document_id=spec["id"],
            issuer=spec["issuer"],
            pdf=pdf_ref,
            ocr=ocr_ref,
            ocr_cache_kind="native" if not spec["is_paddle"] else None,
            ocr_reuse_key=reuse_key,
            page_count=1,
        )
        gen_docs.append(gen_doc)

    db_body = b"sqlite3_test_db"
    db_sha = hashlib.sha256(db_body).hexdigest()
    db_ref = ArtifactRef(
        sha256=db_sha,
        size_bytes=len(db_body),
        media_type="application/vnd.sqlite3",
        path=generation_database_path(gen_id).as_posix(),
    )
    manifest = GenerationManifest(
        generation_id=gen_id,
        created_at=NOW,
        serving_database=db_ref,
        corpus_sha256="c" * 64,
        contract_sha256="d" * 64,
        embedding_contract=EmbeddingContract(
            provider="openrouter", model="embed", dimension=1536, count=len(gen_docs)
        ),
        issuer_codes=("kb", "shinhan"),
        counts=GenerationCounts(
            documents=len(gen_docs),
            pdf_objects=len({d.pdf.sha256 for d in gen_docs}),
            ocr_objects=len({d.ocr.sha256 for d in gen_docs if d.ocr is not None}),
            chunks=len(gen_docs),
        ),
        documents=tuple(gen_docs),
    )
    manifest_bytes = manifest.canonical_bytes()
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    ready = GenerationReady(
        generation_id=gen_id,
        manifest_sha256=manifest_sha,
        serving_database_sha256=db_sha,
        serving_database_size_bytes=len(db_body),
    )
    ready_bytes = ready.canonical_bytes()
    ready_sha = hashlib.sha256(ready_bytes).hexdigest()
    pointer = GenerationPointer(
        generation_id=gen_id,
        manifest_sha256=manifest_sha,
        ready_sha256=ready_sha,
    )

    webdav.objects[STABLE_POINTER_PATH.as_posix()] = pointer.canonical_bytes()
    webdav.objects[generation_manifest_path(gen_id).as_posix()] = manifest_bytes
    webdav.objects[generation_ready_path(gen_id).as_posix()] = ready_bytes

    return webdav, gen_id, doc_specs


@pytest.mark.asyncio
async def test_restore_ocr_seed_dry_run_and_apply_idempotent(tmp_path: Path) -> None:
    webdav, gen_id, doc_specs = build_test_remote()
    dest = tmp_path / "worker-state"

    # 1. Dry run
    dry_result = await restore_ocr_seed_from_generation(
        webdav=webdav,
        destination=dest,
        dry_run=True,
    )
    assert dry_result.dry_run is True
    assert dry_result.total_documents == 3
    assert dry_result.total_ocr_documents == 3
    assert dry_result.unique_ocr_cas_objects == 2  # doc 1 and 2 share the same CAS
    assert dry_result.unbound_cache_documents == 1
    assert dry_result.ledger_path is None
    # No files written during dry run
    assert not (dest / "ocr-seed").exists()

    # 2. Apply run
    apply_result = await restore_ocr_seed_from_generation(
        webdav=webdav,
        destination=dest,
        dry_run=False,
    )
    assert apply_result.dry_run is False
    assert apply_result.imported_ocr_files == 3
    assert apply_result.reused_ocr_files == 0
    assert apply_result.ledger_path is not None
    assert (dest / apply_result.ledger_path).is_file()

    # Check that ocr.md files exist and have correct content
    for spec in doc_specs:
        ocr_file = dest / "ocr-seed" / spec["id"] / "ocr.md"
        assert ocr_file.is_file()
        assert ocr_file.read_bytes() == spec["body"].encode("utf-8")

    # 3. Idempotent re-apply
    reapply_result = await restore_ocr_seed_from_generation(
        webdav=webdav,
        destination=dest,
        dry_run=False,
    )
    assert reapply_result.imported_ocr_files == 0
    assert reapply_result.reused_ocr_files == 3
    assert reapply_result.total_bytes_transferred == 0


@pytest.mark.asyncio
async def test_restore_ocr_seed_detects_corrupted_cas(tmp_path: Path) -> None:
    webdav, gen_id, _ = build_test_remote()
    dest = tmp_path / "worker-state"

    # Corrupt one CAS object
    for k in list(webdav.objects.keys()):
        if k.startswith("v1/objects/sha256/"):
            webdav.objects[k] = b"corrupted bytes"
            break

    with pytest.raises(OCRRecoveryError) as exc_info:
        await restore_ocr_seed_from_generation(
            webdav=webdav,
            destination=dest,
            dry_run=False,
        )
    assert exc_info.value.code in ("ocr_cas_size_mismatch", "ocr_cas_hash_mismatch")


@pytest.mark.asyncio
async def test_restored_seed_enables_zero_provider_ocr_resolution(tmp_path: Path) -> None:
    webdav, gen_id, doc_specs = build_test_remote()
    dest = tmp_path / "worker-state"

    # Restore OCR seed into empty worker state
    apply_result = await restore_ocr_seed_from_generation(
        webdav=webdav,
        destination=dest,
        dry_run=False,
    )
    assert apply_result.imported_ocr_files == 3

    # Now verify OCRResolver resolves ALL documents with 0 provider calls
    db_file = dest / "worker-state.sqlite3"
    with WorkerState(db_file) as state:
        forbidden_provider = DummyThrowingProvider()
        resolver = OCRResolver(
            state=state,
            webdav=webdav,
            provider=forbidden_provider,
            cache_mode="read-only",
        )

        for spec in doc_specs:
            body_bytes = spec["body"].encode("utf-8")
            verified = verify_ocr_bytes(body_bytes, expected_page_count=1)
            pdf_sha = hashlib.sha256(f"pdf_content_for_{spec['id']}".encode()).hexdigest()
            out_dir = tmp_path / "out" / spec["id"]
            # resolve() MUST succeed via seed ledger without calling DummyThrowingProvider!
            res = await resolver.resolve(
                run_id="r-test",
                document_id=spec["id"],
                pdf_path=tmp_path / "fake.pdf",
                pdf_sha256=pdf_sha,
                pdf_size_bytes=len(f"pdf_content_for_{spec['id']}".encode()),
                page_count=1,
                output_dir=out_dir,
            )
            assert res.ocr_sha256 == verified.sha256
            assert res.ocr_bytes == body_bytes
            assert res.provider_called is False
            assert res.cache_reused is True
            if spec["is_paddle"]:
                assert res.provider == "unverified"
                assert res.model == "unverified"
            else:
                assert res.provider == "external-forbidden"
                assert res.model == "restored-native"


@pytest.mark.asyncio
async def test_restore_ocr_seed_rejects_missing_ready(tmp_path: Path) -> None:
    webdav, gen_id, _ = build_test_remote()
    del webdav.objects[generation_ready_path(gen_id).as_posix()]

    with pytest.raises(OCRRecoveryError) as exc_info:
        await restore_ocr_seed_from_generation(
            webdav=webdav,
            destination=tmp_path / "worker-state",
            dry_run=True,
        )
    assert exc_info.value.code == "ready_missing"


@pytest.mark.asyncio
async def test_restore_ocr_seed_rejects_non_canonical_control_json(tmp_path: Path) -> None:
    # 1. Non-canonical pointer
    webdav, gen_id, _ = build_test_remote()
    webdav.objects[STABLE_POINTER_PATH.as_posix()] += b"  \n"
    with pytest.raises(OCRRecoveryError) as exc_info:
        await restore_ocr_seed_from_generation(
            webdav=webdav,
            destination=tmp_path / "worker-state",
            dry_run=True,
        )
    assert exc_info.value.code == "pointer_not_canonical"

    # 2. Non-canonical manifest
    webdav, gen_id, _ = build_test_remote()
    webdav.objects[generation_manifest_path(gen_id).as_posix()] += b"  \n"
    with pytest.raises(OCRRecoveryError) as exc_info:
        await restore_ocr_seed_from_generation(
            webdav=webdav,
            destination=tmp_path / "worker-state",
            dry_run=True,
        )
    assert exc_info.value.code == "manifest_not_canonical"

    # 3. Non-canonical READY
    webdav, gen_id, _ = build_test_remote()
    webdav.objects[generation_ready_path(gen_id).as_posix()] += b"  \n"
    with pytest.raises(OCRRecoveryError) as exc_info:
        await restore_ocr_seed_from_generation(
            webdav=webdav,
            destination=tmp_path / "worker-state",
            dry_run=True,
        )
    assert exc_info.value.code == "ready_not_canonical"


@pytest.mark.asyncio
async def test_restore_ocr_seed_rejects_hash_and_identity_mismatches(tmp_path: Path) -> None:
    # 1. Manifest / READY hash mismatch
    webdav, gen_id, _ = build_test_remote()
    ready = GenerationReady.model_validate_json(webdav.objects[generation_ready_path(gen_id).as_posix()])
    tampered_ready = GenerationReady(
        generation_id=ready.generation_id,
        manifest_sha256="0" * 64,
        serving_database_sha256=ready.serving_database_sha256,
        serving_database_size_bytes=ready.serving_database_size_bytes,
    )
    webdav.objects[generation_ready_path(gen_id).as_posix()] = tampered_ready.canonical_bytes()
    with pytest.raises(OCRRecoveryError) as exc_info:
        await restore_ocr_seed_from_generation(
            webdav=webdav,
            destination=tmp_path / "worker-state",
            dry_run=True,
        )
    assert exc_info.value.code == "manifest_ready_sha_mismatch"

    # 2. Generation ID mismatch between manifest and requested
    webdav, gen_id, _ = build_test_remote()
    with pytest.raises(OCRRecoveryError) as exc_info:
        await restore_ocr_seed_from_generation(
            webdav=webdav,
            destination=tmp_path / "worker-state",
            generation_id="g-other-id",
            dry_run=True,
        )
    assert exc_info.value.code == "manifest_missing"


@pytest.mark.asyncio
async def test_ocr_recovery_ledger_does_not_abort_corpus_diff_on_unacquired_items(tmp_path: Path) -> None:
    from dataclasses import dataclass

    from cardrag_worker.contracts import SourceRecord
    from cardrag_worker.corpus_diff import generate_corpus_diff_report
    from cardrag_worker.downloader import DownloadedPDF
    from cardrag_worker.state_seed_v122 import load_state_seed_ledger

    @dataclass(frozen=True)
    class MockAcqDoc:
        source: SourceRecord
        pdf: DownloadedPDF
        is_historical: bool = False
        temporal_status: str = "current"
        supersedes_document_id: str | None = None

    webdav, gen_id, doc_specs = build_test_remote()
    dest = tmp_path / "worker-state"

    # Restore 3 documents into empty state
    await restore_ocr_seed_from_generation(webdav=webdav, destination=dest, dry_run=False)

    seed_ledger = load_state_seed_ledger(dest)
    assert seed_ledger is not None
    assert seed_ledger.is_ocr_recovery_only is True
    assert len(seed_ledger.entries_by_doc_id) == 3

    # Now simulate a crawler run that only discovers 2 current documents (1 document was historical/retired)
    acquired = []
    for spec in doc_specs[:2]:
        source = SourceRecord(
            issuer=spec["issuer"],
            product_code=spec["id"],
            product_name=spec["id"],
            document_type="product-manual",
            source_url=f"https://example.com/{spec['id']}.pdf",
            source_version="2026-01",
            effective_date=datetime(2026, 1, 1, tzinfo=UTC).date(),
            source_post_id="",
            file_name="guide.pdf",
            category="credit",
            discovered_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        pdf_body = f"pdf_content_for_{spec['id']}".encode()
        pdf_sha = hashlib.sha256(pdf_body).hexdigest()
        pdf_path = tmp_path / f"{pdf_sha}.pdf"
        pdf_path.write_bytes(pdf_body)
        pdf = DownloadedPDF(
            path=pdf_path,
            sha256=pdf_sha,
            size_bytes=len(pdf_body),
            page_count=1,
            final_url=f"https://example.com/{spec['id']}.pdf",
        )
        acquired.append(MockAcqDoc(source=source, pdf=pdf))

    out_report = tmp_path / "reports" / "corpus-diff.json"
    # This MUST NOT raise CorpusDiffError despite only 2 of 3 ledger items being acquired!
    report = generate_corpus_diff_report(
        run_id="run-recovery-corpus-test",
        acquired_documents=acquired,
        seed_ledger=seed_ledger,
        output_path=out_report,
        fail_on_missing=True,
    )
    assert report.counts["missing_unjustified"] == 0
    assert report.counts["final_current"] == 2
    assert out_report.is_file()
