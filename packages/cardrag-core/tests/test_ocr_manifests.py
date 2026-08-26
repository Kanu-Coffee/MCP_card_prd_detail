from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from cardrag_core import (
    AdoptedOCRArtifactManifest,
    ArtifactRef,
    EmbeddingContract,
    GenerationCounts,
    GenerationDocument,
    GenerationManifest,
    LegacyAdoptionReceipt,
    LegacyAdoptionValidation,
    NativeOCRContract,
    OCRArtifactManifest,
    OCRInput,
    OCRVerificationError,
    adopted_ocr_reuse_key,
    generation_database_path,
    native_ocr_reuse_key,
    sha256_bytes,
    verify_ocr_bytes,
)

NOW = datetime(2026, 8, 25, 1, 2, 3, tzinfo=UTC)


def _contract(**updates: object) -> NativeOCRContract:
    values: dict[str, object] = {
        "processor_version": "ocr-processor.v1",
        "prompt_version": "cardrag-ocr.ko.v1",
        "prompt_sha256": sha256_bytes(b"prompt"),
        "renderer_id": "pdfium-5.12.1-rgb-v1",
        "render_scale_milli": 3000,
        "provider": "codex-exec",
        "model": "gpt-5.4",
        "reasoning_effort": "high",
        "chunk_pages": 2,
    }
    values.update(updates)
    return NativeOCRContract(**values)


def _source() -> OCRInput:
    return OCRInput(pdf_sha256=sha256_bytes(b"pdf"), pdf_size_bytes=3, page_count=2)


def _ocr_payload() -> bytes:
    return (
        "## Page 1\n\n첫 페이지의 전월 이용실적 조건입니다.\n\n"
        "## Page 2\n\n두 번째 페이지의 할인 제외 조건입니다.\n"
    ).encode()


def test_native_reuse_key_changes_with_processing_contract() -> None:
    source = _source()
    baseline = native_ocr_reuse_key(_contract(), source)
    assert baseline == native_ocr_reuse_key(_contract(), source)
    assert baseline != native_ocr_reuse_key(_contract(prompt_sha256=sha256_bytes(b"new")), source)
    assert baseline != native_ocr_reuse_key(_contract(provider="openrouter"), source)
    assert baseline != native_ocr_reuse_key(_contract(cache_epoch=1), source)


def test_strict_ocr_verifier_binds_bytes_pages_and_hashes() -> None:
    payload = _ocr_payload()
    initial = verify_ocr_bytes(payload, expected_page_count=2)
    verified = verify_ocr_bytes(
        payload,
        expected_page_count=2,
        expected_sha256=initial.sha256,
        expected_size_bytes=initial.size_bytes,
        expected_char_count=initial.char_count,
        expected_page_sha256=initial.page_sha256,
    )
    assert verified.pages[0].startswith("## Page 1")
    assert len(verified.page_sha256) == 2

    with pytest.raises(OCRVerificationError, match="markers"):
        verify_ocr_bytes(payload.replace(b"Page 2", b"Page 3"), expected_page_count=2)
    with pytest.raises(OCRVerificationError, match="canonical"):
        verify_ocr_bytes(b"\n" + payload, expected_page_count=2)
    with pytest.raises(OCRVerificationError, match="SHA-256"):
        verify_ocr_bytes(payload, expected_page_count=2, expected_sha256="0" * 64)
    with pytest.raises(OCRVerificationError, match="UTF-8"):
        verify_ocr_bytes(b"## Page 1\n\n" + b"\xff" * 30 + b"\n", expected_page_count=1)


def test_native_ocr_manifest_recomputes_reuse_and_page_contract() -> None:
    source = _source()
    contract = _contract()
    verified = verify_ocr_bytes(_ocr_payload(), expected_page_count=2)
    output = ArtifactRef.for_cas(
        sha256=verified.sha256,
        size_bytes=verified.size_bytes,
        media_type="text/markdown; charset=utf-8",
    )
    manifest = OCRArtifactManifest(
        reuse_key=native_ocr_reuse_key(contract, source),
        source=source,
        contract=contract,
        output=output,
        ocr_chars=verified.char_count,
        page_output_sha256=verified.page_sha256,
        created_at=NOW,
    )
    assert manifest.manifest_sha256 == sha256_bytes(manifest.canonical_bytes())
    with pytest.raises(ValidationError, match="reuse key"):
        OCRArtifactManifest(
            reuse_key="0" * 64,
            source=source,
            contract=contract,
            output=output,
            ocr_chars=verified.char_count,
            page_output_sha256=verified.page_sha256,
            created_at=NOW,
        )


def test_adopted_manifest_requires_a_hash_and_ledger_bound_receipt() -> None:
    source = _source()
    verified = verify_ocr_bytes(_ocr_payload(), expected_page_count=2)
    output = ArtifactRef.for_cas(
        sha256=verified.sha256,
        size_bytes=verified.size_bytes,
        media_type="text/markdown; charset=utf-8",
    )
    policy = "cardrag.legacy-ocr-adoption.v1"
    document_id = "doc_legacy123"
    receipt = LegacyAdoptionReceipt(
        adoption_policy_version=policy,
        source_bundle_id="bundle-abc123",
        source_bundle_sha256=sha256_bytes(b"bundle"),
        source_database_id="legacy-import-42",
        source_document_id=document_id,
        pdf_sha256=source.pdf_sha256,
        ocr_sha256=output.sha256,
        validation=LegacyAdoptionValidation(
            hash_verified=True,
            page_coverage_verified=True,
            utf8_verified=True,
            ledger_bound=True,
        ),
    )
    reuse_key = adopted_ocr_reuse_key(
        adoption_policy_version=policy,
        source_document_id=document_id,
        pdf_sha256=source.pdf_sha256,
    )
    manifest = AdoptedOCRArtifactManifest(
        reuse_key=reuse_key,
        source=source,
        receipt=receipt,
        output=output,
        ocr_chars=verified.char_count,
        page_output_sha256=verified.page_sha256,
        created_at=NOW,
    )
    assert manifest.origin == "legacy_adoption"
    with pytest.raises(ValidationError, match="OCR hash"):
        AdoptedOCRArtifactManifest(
            reuse_key=reuse_key,
            source=source,
            receipt=receipt,
            output=ArtifactRef.for_cas(
                sha256=sha256_bytes(b"forged"),
                size_bytes=6,
                media_type="text/markdown; charset=utf-8",
            ),
            ocr_chars=verified.char_count,
            page_output_sha256=verified.page_sha256,
            created_at=NOW,
        )


def test_generation_manifest_lists_pdf_cas_and_open_issuer_codes() -> None:
    pdf = ArtifactRef.for_cas(
        sha256=sha256_bytes(b"pdf"),
        size_bytes=3,
        media_type="application/pdf",
    )
    ocr = ArtifactRef.for_cas(
        sha256=sha256_bytes(b"ocr"),
        size_bytes=3,
        media_type="text/markdown; charset=utf-8",
    )
    generation_id = "gen-20260825"
    database = ArtifactRef(
        sha256=sha256_bytes(b"sqlite"),
        size_bytes=6,
        media_type="application/vnd.sqlite3",
        path=generation_database_path(generation_id).as_posix(),
    )
    reuse_key = sha256_bytes(b"native-cache-key")
    documents = (
        GenerationDocument(
            document_id="doc_kb",
            issuer="kb",
            pdf=pdf,
            ocr=ocr,
            ocr_cache_kind="native",
            ocr_reuse_key=reuse_key,
            page_count=2,
        ),
        GenerationDocument(document_id="doc_lotte", issuer="lotte", pdf=pdf, ocr=ocr, page_count=2),
    )
    manifest = GenerationManifest(
        generation_id=generation_id,
        created_at=NOW,
        serving_database=database,
        corpus_sha256=sha256_bytes(b"corpus"),
        contract_sha256=sha256_bytes(b"contract"),
        embedding_contract=EmbeddingContract(
            provider="openrouter",
            model="openai/text-embedding-3-small",
            dimension=1536,
            count=5,
        ),
        issuer_codes=("kb", "lotte"),
        counts=GenerationCounts(documents=2, pdf_objects=1, ocr_objects=1, chunks=5),
        documents=documents,
    )
    assert manifest.schema_version == "cardrag.generation.v2"
    assert manifest.serving_schema == "cardrag.serving-db.v2"
    assert manifest.issuer_codes == ("kb", "lotte")
    assert manifest.documents[0].ocr_cache_kind == "native"
    assert manifest.documents[0].ocr_reuse_key == reuse_key
    assert manifest.documents[1].ocr_cache_kind is None
    assert manifest.documents[1].ocr_reuse_key is None
    legacy = GenerationManifest.model_validate(
        {
            **manifest.model_dump(mode="python"),
            "schema_version": "cardrag.generation.v1",
            "serving_schema": "cardrag.serving-db.v1",
        }
    )
    assert legacy.schema_version == "cardrag.generation.v1"
    assert legacy.serving_schema == "cardrag.serving-db.v1"
    with pytest.raises(ValidationError, match="schema versions must match"):
        GenerationManifest.model_validate(
            {
                **manifest.model_dump(mode="python"),
                "schema_version": "cardrag.generation.v1",
                "serving_schema": "cardrag.serving-db.v2",
            }
        )
    with pytest.raises(ValidationError, match="document count"):
        GenerationManifest.model_validate(
            {
                **manifest.model_dump(mode="python"),
                "counts": GenerationCounts(documents=3, pdf_objects=1, ocr_objects=1, chunks=5),
            }
        )


def test_generation_ocr_cache_identity_is_exact_and_all_or_nothing() -> None:
    pdf = ArtifactRef.for_cas(
        sha256=sha256_bytes(b"pdf"),
        size_bytes=3,
        media_type="application/pdf",
    )
    ocr = ArtifactRef.for_cas(
        sha256=sha256_bytes(b"ocr"),
        size_bytes=3,
        media_type="text/markdown; charset=utf-8",
    )
    reuse_key = sha256_bytes(b"adopted-cache-key")

    cached = GenerationDocument(
        document_id="doc_cached",
        issuer="lotte",
        pdf=pdf,
        ocr=ocr,
        ocr_cache_kind="adopted",
        ocr_reuse_key=reuse_key,
        page_count=2,
    )
    assert cached.ocr_cache_kind == "adopted"
    assert cached.ocr_reuse_key == reuse_key

    generation_only = GenerationDocument(
        document_id="doc_generation_only",
        issuer="lotte",
        pdf=pdf,
        ocr=ocr,
        page_count=2,
    )
    assert generation_only.ocr_cache_kind is None
    assert generation_only.ocr_reuse_key is None

    with pytest.raises(ValidationError, match="provided together"):
        GenerationDocument(
            document_id="doc_missing_key",
            issuer="lotte",
            pdf=pdf,
            ocr=ocr,
            ocr_cache_kind="native",
            page_count=2,
        )
    with pytest.raises(ValidationError, match="provided together"):
        GenerationDocument(
            document_id="doc_missing_kind",
            issuer="lotte",
            pdf=pdf,
            ocr=ocr,
            ocr_reuse_key=reuse_key,
            page_count=2,
        )
    with pytest.raises(ValidationError, match="requires a generation OCR artifact"):
        GenerationDocument(
            document_id="doc_without_ocr",
            issuer="lotte",
            pdf=pdf,
            ocr_cache_kind="native",
            ocr_reuse_key=reuse_key,
            page_count=2,
        )
    with pytest.raises(ValidationError):
        GenerationDocument(
            document_id="doc_bad_kind",
            issuer="lotte",
            pdf=pdf,
            ocr=ocr,
            ocr_cache_kind="legacy",  # type: ignore[arg-type]
            ocr_reuse_key=reuse_key,
            page_count=2,
        )
    with pytest.raises(ValidationError):
        GenerationDocument(
            document_id="doc_bad_key",
            issuer="lotte",
            pdf=pdf,
            ocr=ocr,
            ocr_cache_kind="native",
            ocr_reuse_key="not-a-sha256",
            page_count=2,
        )
