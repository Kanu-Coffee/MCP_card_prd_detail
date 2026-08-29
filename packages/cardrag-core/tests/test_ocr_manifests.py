from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from cardrag_core import (
    LEGACY_OCR_APPROVED_PREFIX,
    LEGACY_OCR_APPROVED_PREFIX_SHA256,
    AdoptedOCRArtifactManifest,
    ArtifactRef,
    EmbeddingContract,
    GenerationCounts,
    GenerationDocument,
    GenerationManifest,
    GenerationOCRFailure,
    IssuerOCRCounts,
    LegacyAdoptionReceipt,
    LegacyAdoptionReceiptV2,
    LegacyAdoptionValidation,
    LegacyAdoptionValidationV2,
    NativeOCRContract,
    OCRArtifactManifest,
    OCRInput,
    OCRVerificationError,
    adopted_ocr_reuse_key,
    canonical_sha256,
    generation_database_path,
    native_ocr_reuse_key,
    sha256_bytes,
    verify_ocr_bytes,
)

NOW = datetime(2026, 8, 25, 1, 2, 3, tzinfo=UTC)


def _contract(**updates: object) -> NativeOCRContract:
    values: dict[str, object] = {
        "processor_version": "cardrag-worker/1.0.4",
        "prompt_version": "cardrag-ocr.ko.v2",
        "prompt_sha256": sha256_bytes(b"prompt"),
        "renderer_id": "pdfium-5.12.1-rgb-v1",
        "render_scale_milli": 6000,
        "provider": "codex-exec",
        "model": "gpt-5.6-sol",
        "reasoning_effort": "high",
        "segmentation_strategy_id": "cardrag.ocr.windowed-continuity.v1",
        "whole_document_max_pages": 4,
        "target_pages_per_call": 2,
        "context_pages_before": 1,
        "context_pages_after": 1,
        "output_policy": "target-pages-only",
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
    assert baseline != native_ocr_reuse_key(_contract(render_scale_milli=5000), source)
    assert baseline != native_ocr_reuse_key(_contract(model="gpt-5.4"), source)
    assert baseline != native_ocr_reuse_key(_contract(whole_document_max_pages=3), source)
    assert baseline != native_ocr_reuse_key(_contract(target_pages_per_call=1), source)
    assert baseline != native_ocr_reuse_key(_contract(context_pages_before=0), source)
    assert baseline != native_ocr_reuse_key(_contract(context_pages_after=0), source)
    assert baseline != native_ocr_reuse_key(
        _contract(segmentation_strategy_id="cardrag.ocr.windowed-continuity.v2"), source
    )


def test_legacy_v1_contract_parses_and_hashes_without_becoming_v2() -> None:
    legacy_payload = {
        "schema_version": "cardrag.ocr-contract.v1",
        "processor_version": "cardrag-worker/1.0.0",
        "output_profile": "cardrag.ocr-markdown.v1",
        "cache_epoch": 0,
        "prompt_version": "cardrag-ocr.ko.v1",
        "prompt_sha256": sha256_bytes(b"old-prompt"),
        "renderer_id": "pypdfium2/5.12.1",
        "render_scale_milli": 3000,
        "provider": "codex-exec",
        "model": "gpt-5.4",
        "reasoning_effort": "high",
        "chunk_pages": 2,
    }
    legacy = NativeOCRContract.model_validate(legacy_payload)
    current = _contract()

    assert legacy.model_dump(mode="json") == legacy_payload
    assert legacy.contract_sha256 == canonical_sha256(legacy_payload)
    assert native_ocr_reuse_key(legacy, _source()) != native_ocr_reuse_key(current, _source())


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


def test_v2_adopted_manifest_binds_original_and_normalized_ocr_lineage() -> None:
    assert len(LEGACY_OCR_APPROVED_PREFIX) == 24
    assert sha256_bytes(LEGACY_OCR_APPROVED_PREFIX) == LEGACY_OCR_APPROVED_PREFIX_SHA256
    source = _source()
    normalized = _ocr_payload()
    original = LEGACY_OCR_APPROVED_PREFIX + normalized
    verified = verify_ocr_bytes(normalized, expected_page_count=2)
    output = ArtifactRef.for_cas(
        sha256=verified.sha256,
        size_bytes=verified.size_bytes,
        media_type="text/markdown; charset=utf-8",
    )
    document_id = "doc_" + "d" * 64
    receipt = LegacyAdoptionReceiptV2(
        source_bundle_id="bundle-v2",
        source_bundle_sha256=sha256_bytes(b"bundle-v2"),
        source_database_id="legacy-data-kit",
        source_document_id=document_id,
        pdf_sha256=source.pdf_sha256,
        source_ocr_sha256=sha256_bytes(original),
        source_ocr_size_bytes=len(original),
        normalized_ocr_sha256=verified.sha256,
        normalized_ocr_size_bytes=verified.size_bytes,
        normalization_profile="strip-exact-generated-prefix-v1",
        prefix_sha256=LEGACY_OCR_APPROVED_PREFIX_SHA256,
        removed_bytes=24,
        validation=LegacyAdoptionValidationV2(
            source_hash_verified=True,
            normalized_hash_verified=True,
            transformation_verified=True,
            page_coverage_verified=True,
            utf8_verified=True,
            ledger_bound=True,
        ),
    )
    reuse_key = adopted_ocr_reuse_key(
        adoption_policy_version=receipt.adoption_policy_version,
        source_document_id=document_id,
        pdf_sha256=source.pdf_sha256,
    )
    manifest = AdoptedOCRArtifactManifest(
        schema_version="cardrag.ocr-artifact.v2",
        validation_profile="cardrag.legacy-ocr-adoption.v2",
        reuse_key=reuse_key,
        source=source,
        receipt=receipt,
        output=output,
        ocr_chars=verified.char_count,
        page_output_sha256=verified.page_sha256,
        created_at=NOW,
    )
    reparsed = AdoptedOCRArtifactManifest.model_validate_json(manifest.canonical_bytes())
    assert isinstance(reparsed.receipt, LegacyAdoptionReceiptV2)
    assert reparsed.receipt.source_ocr_sha256 == sha256_bytes(original)
    assert reparsed.receipt.normalized_ocr_sha256 == verified.sha256
    assert reparsed.output.sha256 == reparsed.receipt.normalized_ocr_sha256


def test_v2_adoption_rejects_arbitrary_or_mixed_normalization_contracts() -> None:
    normalized = _ocr_payload()
    valid = {
        "source_bundle_id": "bundle-v2",
        "source_bundle_sha256": sha256_bytes(b"bundle-v2"),
        "source_database_id": "legacy-data-kit",
        "source_document_id": "doc_" + "d" * 64,
        "pdf_sha256": sha256_bytes(b"pdf"),
        "source_ocr_sha256": sha256_bytes(LEGACY_OCR_APPROVED_PREFIX + normalized),
        "source_ocr_size_bytes": len(LEGACY_OCR_APPROVED_PREFIX + normalized),
        "normalized_ocr_sha256": sha256_bytes(normalized),
        "normalized_ocr_size_bytes": len(normalized),
        "normalization_profile": "strip-exact-generated-prefix-v1",
        "prefix_sha256": LEGACY_OCR_APPROVED_PREFIX_SHA256,
        "removed_bytes": 24,
        "validation": {
            "source_hash_verified": True,
            "normalized_hash_verified": True,
            "transformation_verified": True,
            "page_coverage_verified": True,
            "utf8_verified": True,
            "ledger_bound": True,
        },
    }
    with pytest.raises(ValidationError, match="approved prefix hash"):
        LegacyAdoptionReceiptV2.model_validate({**valid, "prefix_sha256": "0" * 64})
    with pytest.raises(ValidationError, match="exact adoption requires identical"):
        LegacyAdoptionReceiptV2.model_validate(
            {
                **valid,
                "normalization_profile": "exact",
                "prefix_sha256": None,
                "removed_bytes": 0,
            }
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
        issuer_codes=("kb", "lotte", "woori"),
        counts=GenerationCounts(documents=2, pdf_objects=1, ocr_objects=1, chunks=5),
        documents=documents,
    )
    assert manifest.schema_version == "cardrag.generation.v3"
    assert manifest.serving_schema == "cardrag.serving-db.v3"
    assert manifest.issuer_codes == ("kb", "lotte", "woori")
    assert manifest.documents[0].ocr_cache_kind == "native"
    assert manifest.documents[0].ocr_reuse_key == reuse_key
    assert manifest.documents[1].ocr_cache_kind is None
    assert manifest.documents[1].ocr_reuse_key is None
    with pytest.raises(ValidationError, match="undeclared issuer"):
        GenerationManifest.model_validate(
            {
                **manifest.model_dump(mode="python"),
                "issuer_codes": ("kb",),
            }
        )
    compatible_pairs = (
        ("cardrag.generation.v1", "cardrag.serving-db.v1"),
        ("cardrag.generation.v2", "cardrag.serving-db.v2"),
        ("cardrag.generation.v3", "cardrag.serving-db.v3"),
    )
    for generation_schema, serving_schema in compatible_pairs:
        compatible = GenerationManifest.model_validate(
            {
                **manifest.model_dump(mode="python"),
                "schema_version": generation_schema,
                "serving_schema": serving_schema,
            }
        )
        assert (compatible.schema_version, compatible.serving_schema) == (
            generation_schema,
            serving_schema,
        )
    for generation_schema, expected_serving_schema in compatible_pairs:
        for _, serving_schema in compatible_pairs:
            if serving_schema == expected_serving_schema:
                continue
            with pytest.raises(ValidationError, match="schema versions must match"):
                GenerationManifest.model_validate(
                    {
                        **manifest.model_dump(mode="python"),
                        "schema_version": generation_schema,
                        "serving_schema": serving_schema,
                    }
                )
    with pytest.raises(ValidationError, match="document count"):
        GenerationManifest.model_validate(
            {
                **manifest.model_dump(mode="python"),
                "counts": GenerationCounts(documents=3, pdf_objects=1, ocr_objects=1, chunks=5),
            }
        )


def test_generation_v4_records_exactly_thresholded_partial_ocr_publication() -> None:
    pdf = ArtifactRef.for_cas(sha256=sha256_bytes(b"pdf-v4"), size_bytes=6, media_type="application/pdf")
    ocr = ArtifactRef.for_cas(
        sha256=sha256_bytes(b"ocr-v4"),
        size_bytes=6,
        media_type="text/markdown; charset=utf-8",
    )
    documents = tuple(
        GenerationDocument(
            document_id=f"doc_{index:02d}",
            issuer="kb",
            pdf=pdf,
            ocr=ocr,
            page_count=1,
            availability="available",
        )
        for index in range(19)
    ) + (
        GenerationDocument(
            document_id="doc_19",
            issuer="kb",
            pdf=pdf,
            page_count=1,
            availability="ocr_failed",
            ocr_failure=GenerationOCRFailure(
                reason_code="provider_timeout",
                reason="The OCR provider timed out.",
                attempts=4,
            ),
        ),
    )
    generation_id = "gen-v4-partial"
    manifest = GenerationManifest(
        schema_version="cardrag.generation.v4",
        generation_id=generation_id,
        created_at=NOW,
        serving_schema="cardrag.serving-db.v4",
        serving_database=ArtifactRef(
            sha256=sha256_bytes(b"sqlite-v4"),
            size_bytes=9,
            media_type="application/vnd.sqlite3",
            path=generation_database_path(generation_id).as_posix(),
        ),
        corpus_sha256=sha256_bytes(b"corpus-v4"),
        contract_sha256=sha256_bytes(b"contract-v4"),
        embedding_contract=EmbeddingContract(
            provider="openrouter",
            model="openai/text-embedding-3-small",
            dimension=1536,
            count=19,
        ),
        issuer_codes=("kb",),
        counts=GenerationCounts(documents=20, pdf_objects=1, ocr_objects=1, chunks=19),
        documents=tuple(sorted(documents, key=lambda row: row.document_id)),
        issuer_ocr_counts=(IssuerOCRCounts(issuer="kb", acquired=20, succeeded=19, failed=1),),
    )

    restored = GenerationManifest.model_validate_json(manifest.canonical_bytes())
    assert restored == manifest
    assert restored.documents[-1].availability == "ocr_failed"
    assert restored.documents[-1].ocr is None

    with pytest.raises(ValidationError, match="counts differ from document availability"):
        GenerationManifest.model_validate(
            {
                **manifest.model_dump(mode="python"),
                "issuer_ocr_counts": (IssuerOCRCounts(issuer="kb", acquired=20, succeeded=18, failed=2),),
            }
        )


def test_generation_v3_canonical_bytes_do_not_gain_v4_fields() -> None:
    pdf = ArtifactRef.for_cas(
        sha256=sha256_bytes(b"legacy-pdf"),
        size_bytes=10,
        media_type="application/pdf",
    )
    ocr = ArtifactRef.for_cas(
        sha256=sha256_bytes(b"legacy-ocr"),
        size_bytes=10,
        media_type="text/markdown; charset=utf-8",
    )
    generation_id = "gen-v3-canonical"
    manifest = GenerationManifest(
        generation_id=generation_id,
        created_at=NOW,
        serving_database=ArtifactRef(
            sha256=sha256_bytes(b"legacy-db"),
            size_bytes=9,
            media_type="application/vnd.sqlite3",
            path=generation_database_path(generation_id).as_posix(),
        ),
        corpus_sha256=sha256_bytes(b"legacy-corpus"),
        contract_sha256=sha256_bytes(b"legacy-contract"),
        embedding_contract=EmbeddingContract(provider="openrouter", model="model", dimension=1536, count=1),
        issuer_codes=("kb",),
        counts=GenerationCounts(documents=1, pdf_objects=1, ocr_objects=1, chunks=1),
        documents=(
            GenerationDocument(
                document_id="doc_legacy",
                issuer="kb",
                pdf=pdf,
                ocr=ocr,
                page_count=1,
            ),
        ),
    )
    body = manifest.canonical_bytes()
    assert b"availability" not in body
    assert b"ocr_failure" not in body
    assert b"issuer_ocr_counts" not in body
    assert GenerationManifest.model_validate_json(body).canonical_bytes() == body


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
