from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from cardrag_core import (
    EMBEDDING_VIEW_TYPES,
    ArtifactRef,
    DocumentAggregationBootstrap,
    DocumentAggregationProfile,
    EmbeddingContract,
    EmbeddingProfile,
    EmbeddingVectorSidecar,
    EmbeddingViewCount,
    GenerationCounts,
    GenerationDocument,
    GenerationManifest,
    GenerationReady,
    IssuerOCRCounts,
    IssuerParserProfile,
    MaxChildAggregationDefinition,
    StructureContract,
    StructureMajorClassCounts,
    StructureNodeCounts,
    StructureRevisionCounts,
    StructureSourceCoverage,
    canonical_sha256,
    expected_generation_files,
    generation_database_path,
    generation_vectors_path,
    qwen3_embedding_cache_namespace,
    qwen3_embedding_profile_id,
    sealed_v5_retrieval_policy,
    sha256_bytes,
)

NOW = datetime(2026, 8, 29, 1, 2, 3, tzinfo=UTC)


def _v5_manifest(*, rows: int = 6) -> GenerationManifest:
    generation_id = "gen-v5-contract"
    pdf = ArtifactRef.for_cas(
        sha256=sha256_bytes(b"v5-pdf"),
        size_bytes=6,
        media_type="application/pdf",
    )
    ocr = ArtifactRef.for_cas(
        sha256=sha256_bytes(b"v5-ocr"),
        size_bytes=6,
        media_type="text/markdown; charset=utf-8",
    )
    profile = EmbeddingProfile.qwen3(provider_id="deepinfra", maximum_tokens=8192)
    source_hash = sha256_bytes(b"all non-whitespace OCR characters")
    view_counts = tuple(
        EmbeddingViewCount(view_type=view_type, count=rows if index == 0 else 0)
        for index, view_type in enumerate(EMBEDDING_VIEW_TYPES)
    )
    return GenerationManifest(
        schema_version="cardrag.generation.v5",
        generation_id=generation_id,
        created_at=NOW,
        serving_schema="cardrag.serving-db.v5",
        serving_database=ArtifactRef(
            sha256=sha256_bytes(b"v5-sqlite"),
            size_bytes=9,
            media_type="application/vnd.sqlite3",
            path=generation_database_path(generation_id).as_posix(),
        ),
        corpus_sha256=sha256_bytes(b"v5-corpus"),
        contract_sha256=sha256_bytes(b"v5-contract"),
        embedding_contract=EmbeddingContract(
            provider="openrouter",
            model="qwen/qwen3-embedding-8b",
            dimension=4096,
            count=rows,
        ),
        issuer_codes=("kb",),
        counts=GenerationCounts(documents=1, pdf_objects=1, ocr_objects=1, chunks=rows),
        documents=(
            GenerationDocument(
                document_id="doc_v5",
                issuer="kb",
                pdf=pdf,
                ocr=ocr,
                page_count=1,
                availability="available",
            ),
        ),
        issuer_ocr_counts=(IssuerOCRCounts(issuer="kb", acquired=1, succeeded=1, failed=0),),
        structure_contract=StructureContract(
            schema_version="cardrag.structure.v2",
            parser_profiles=(
                IssuerParserProfile(
                    issuer="kb",
                    profile_id="cardrag.parser.kb.v1",
                    profile_sha256=sha256_bytes(b"kb-parser-policy"),
                ),
            ),
            node_counts=StructureNodeCounts(
                total=4,
                root=1,
                major_section=1,
                item=1,
                paragraph=1,
                list_item=0,
                table=0,
                table_row=0,
                footnote=0,
                boilerplate=0,
                unclassified=0,
            ),
            major_class_counts=StructureMajorClassCounts(
                total=1,
                benefit=1,
                notice=0,
                mixed=0,
                unknown=0,
            ),
            source_coverage=StructureSourceCoverage(
                source_non_whitespace_characters=31,
                covered_non_whitespace_characters=31,
                source_non_whitespace_sha256=source_hash,
                covered_non_whitespace_sha256=source_hash,
            ),
            revision_counts=StructureRevisionCounts(
                total=1,
                current=1,
                superseded=0,
                ambiguous=0,
            ),
            cross_contract_parent_count=0,
            cross_contract_link_count=0,
            lineages_with_multiple_current_revisions=0,
        ),
        embedding_profiles=(profile,),
        primary_embedding_profile_id=profile.profile_id,
        embedding_view_counts=view_counts,
        vector_sidecar=EmbeddingVectorSidecar(
            artifact=ArtifactRef(
                sha256=sha256_bytes(b"v5-vectors"),
                size_bytes=rows * 4096 * 4,
                media_type="application/octet-stream",
                path=generation_vectors_path(generation_id).as_posix(),
            ),
            profile_id=profile.profile_id,
            row_count=rows,
            dimension=4096,
            dtype="float32",
            byte_order="little-endian",
            layout="row-major",
            normalization="l2",
        ),
        parser_policy_sha256=sha256_bytes(b"parser-policy"),
        embedding_policy_sha256=sha256_bytes(b"embedding-policy"),
        retrieval_policy_sha256=sha256_bytes(b"retrieval-policy"),
    )


def test_valid_v5_manifest_and_ready_bind_the_full_fp32_sidecar() -> None:
    manifest = _v5_manifest()
    restored = GenerationManifest.model_validate_json(manifest.canonical_bytes())

    assert restored == manifest
    assert restored.schema_version == "cardrag.generation.v5"
    assert restored.serving_schema == "cardrag.serving-db.v5"
    assert restored.embedding_contract.dimension == 4096
    assert restored.vector_sidecar is not None
    assert restored.vector_sidecar.artifact.size_bytes == 6 * 4096 * 4

    ready = GenerationReady(
        generation_id=manifest.generation_id,
        manifest_sha256=manifest.manifest_sha256,
        serving_database_sha256=manifest.serving_database.sha256,
        serving_database_size_bytes=manifest.serving_database.size_bytes,
        vector_sidecar_sha256=restored.vector_sidecar.artifact.sha256,
        vector_sidecar_size_bytes=restored.vector_sidecar.artifact.size_bytes,
    )
    assert b"vector_sidecar_sha256" in ready.canonical_bytes()
    assert GenerationReady.model_validate_json(ready.canonical_bytes()) == ready


def test_v5_selected_aggregation_profile_is_all_or_none_and_retrieval_bound() -> None:
    base = _v5_manifest()
    primary_profile_id = base.primary_embedding_profile_id
    assert primary_profile_id is not None
    exact_row_corpus_sha256 = sha256_bytes(b"exact-row-corpus")
    profile = DocumentAggregationProfile(
        schema_version="cardrag.document-aggregation-profile.v1",
        profile_id="cardrag.document-aggregation.max-child.v1",
        aggregation_policy="max_child",
        aggregation_definition=MaxChildAggregationDefinition(
            child_view_types=(
                "CONTEXTUAL_ITEM",
                "DETAIL",
                "MAJOR_SECTION",
                "RAW_ITEM",
                "TITLE",
            ),
            formula="max(non-CONTRACT row score)",
        ),
        bootstrap=DocumentAggregationBootstrap(
            ci=0.95,
            method="paired-query-percentile-pcg64",
            samples=2_000,
            seed=1010,
        ),
        embedding_profile_id=primary_profile_id,
        exact_row_corpus_sha256=exact_row_corpus_sha256,
        generation_id="evaluation-generation",
        generation_manifest_sha256=sha256_bytes(b"evaluation-manifest-m0"),
        gold_sha256=sha256_bytes(b"gold"),
        score_artifact_sha256=sha256_bytes(b"all-exact-row-scores"),
        selection_objective="ndcg_at_10",
    )
    payload = base.model_dump(mode="python")
    payload.update(
        {
            "document_aggregation_profile": profile.model_dump(mode="python"),
            "document_aggregation_policy": profile.aggregation_policy,
            "sealed_profile_sha256": profile.profile_sha256,
            "exact_row_corpus_sha256": exact_row_corpus_sha256,
            "retrieval_policy_sha256": canonical_sha256(
                sealed_v5_retrieval_policy(profile, profile.profile_sha256)
            ),
        }
    )

    selected = GenerationManifest.model_validate(payload)

    assert selected.document_aggregation_profile == profile
    assert selected.document_aggregation_policy == profile.aggregation_policy
    assert selected.sealed_profile_sha256 == profile.profile_sha256
    for missing_field in (
        "document_aggregation_profile",
        "document_aggregation_policy",
        "sealed_profile_sha256",
        "exact_row_corpus_sha256",
    ):
        incomplete = dict(payload)
        incomplete.pop(missing_field)
        with pytest.raises(ValidationError, match="all-or-nothing"):
            GenerationManifest.model_validate(incomplete)

    wrong_retrieval = dict(payload)
    wrong_retrieval["retrieval_policy_sha256"] = sha256_bytes(b"wrong-retrieval")
    with pytest.raises(ValidationError, match="does not bind document aggregation"):
        GenerationManifest.model_validate(wrong_retrieval)


def test_legacy_generation_and_ready_canonical_roundtrips_exclude_v5_fields() -> None:
    for generation_schema, serving_schema in (
        ("cardrag.generation.v1", "cardrag.serving-db.v1"),
        ("cardrag.generation.v2", "cardrag.serving-db.v2"),
        ("cardrag.generation.v3", "cardrag.serving-db.v3"),
        ("cardrag.generation.v4", "cardrag.serving-db.v4"),
    ):
        payload = _v5_manifest().model_dump(mode="python")
        for field in (
            "structure_contract",
            "embedding_profiles",
            "primary_embedding_profile_id",
            "embedding_view_counts",
            "vector_sidecar",
            "parser_policy_sha256",
            "embedding_policy_sha256",
            "retrieval_policy_sha256",
        ):
            payload.pop(field)
        payload["schema_version"] = generation_schema
        payload["serving_schema"] = serving_schema
        payload["embedding_contract"] = {
            "provider": "openrouter",
            "model": "openai/text-embedding-3-small",
            "dimension": 1536,
            "count": 6,
        }
        if generation_schema != "cardrag.generation.v4":
            payload["issuer_ocr_counts"] = ()
            payload["documents"][0]["availability"] = None
        legacy = GenerationManifest.model_validate(payload)
        body = legacy.canonical_bytes()
        assert b"structure_contract" not in body
        assert b"vector_sidecar" not in body
        assert b"embedding_profiles" not in body
        assert GenerationManifest.model_validate_json(body).canonical_bytes() == body

    legacy_ready = GenerationReady(
        generation_id="legacy-ready",
        manifest_sha256=sha256_bytes(b"manifest"),
        serving_database_sha256=sha256_bytes(b"database"),
        serving_database_size_bytes=8,
    )
    ready_body = legacy_ready.canonical_bytes()
    assert b"vector_sidecar" not in ready_body
    assert GenerationReady.model_validate_json(ready_body).canonical_bytes() == ready_body


def test_v5_rejects_legacy_dimension_and_legacy_rejects_qwen_dimension() -> None:
    manifest = _v5_manifest()
    payload = manifest.model_dump(mode="python")
    payload["embedding_contract"]["dimension"] = 1536
    with pytest.raises(ValidationError, match="v5 embedding dimension must be 4096"):
        GenerationManifest.model_validate(payload)

    payload = manifest.model_dump(mode="python")
    payload["schema_version"] = "cardrag.generation.v4"
    payload["serving_schema"] = "cardrag.serving-db.v4"
    for field in (
        "structure_contract",
        "embedding_profiles",
        "primary_embedding_profile_id",
        "embedding_view_counts",
        "vector_sidecar",
        "parser_policy_sha256",
        "embedding_policy_sha256",
        "retrieval_policy_sha256",
    ):
        payload.pop(field)
    with pytest.raises(ValidationError, match="v1-v4 embedding dimension must be 1536"):
        GenerationManifest.model_validate(payload)


def test_vector_sidecar_rejects_wrong_size_and_manifest_rejects_row_or_view_counts() -> None:
    profile = EmbeddingProfile.qwen3(provider_id="deepinfra", maximum_tokens=8192)
    with pytest.raises(ValidationError, match="size must equal"):
        EmbeddingVectorSidecar(
            artifact=ArtifactRef(
                sha256=sha256_bytes(b"bad-size"),
                size_bytes=4096 * 4 - 1,
                media_type="application/octet-stream",
                path=generation_vectors_path("bad-size").as_posix(),
            ),
            profile_id=profile.profile_id,
            row_count=1,
            dimension=4096,
            dtype="float32",
            byte_order="little-endian",
            layout="row-major",
            normalization="l2",
        )

    manifest = _v5_manifest()
    payload = manifest.model_dump(mode="python")
    payload["counts"]["chunks"] = 5
    payload["embedding_contract"]["count"] = 5
    with pytest.raises(ValidationError, match="row count must equal generation chunk count"):
        GenerationManifest.model_validate(payload)

    payload = manifest.model_dump(mode="python")
    payload["embedding_view_counts"][0]["count"] = 5
    with pytest.raises(ValidationError, match="view counts must equal"):
        GenerationManifest.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("provider_id", "unapproved"),
        ("provider_fallback", "allowed"),
        ("dimension", 1536),
        ("dtype", "float16"),
        ("normalization", "none"),
        ("truncation", "truncate"),
    ),
)
def test_qwen_profile_rejects_provider_fallback_or_contract_drift(field: str, value: object) -> None:
    payload = EmbeddingProfile.qwen3(
        provider_id="deepinfra",
        maximum_tokens=8192,
    ).model_dump(mode="python")
    payload[field] = value
    with pytest.raises(ValidationError):
        EmbeddingProfile.model_validate(payload)


def test_profile_and_cache_namespace_are_provider_specific_and_fallback_free() -> None:
    deepinfra = EmbeddingProfile.qwen3(provider_id="deepinfra", maximum_tokens=8192)
    nebius = EmbeddingProfile.qwen3(provider_id="nebius", maximum_tokens=8192)

    assert deepinfra.profile_id == qwen3_embedding_profile_id("deepinfra", maximum_tokens=8192)
    assert deepinfra.cache_namespace == qwen3_embedding_cache_namespace("deepinfra", maximum_tokens=8192)
    assert nebius.profile_id != deepinfra.profile_id
    assert nebius.cache_namespace != deepinfra.cache_namespace
    assert deepinfra.provider_fallback == "forbidden"


def test_generation_file_layout_is_legacy_compatible_and_v5_opt_in() -> None:
    legacy = expected_generation_files("bundle")
    v5 = expected_generation_files("bundle", include_vectors=True)

    assert legacy == (
        "v1/generations/bundle/index.sqlite3",
        "v1/generations/bundle/manifest.json",
        "v1/generations/bundle/READY.json",
    )
    assert v5[:3] == legacy
    assert v5[3] == "v1/generations/bundle/vectors.f32"


def test_ready_sidecar_identity_is_all_or_nothing() -> None:
    with pytest.raises(ValidationError, match="provided together"):
        GenerationReady(
            generation_id="incomplete-ready",
            manifest_sha256=sha256_bytes(b"manifest"),
            serving_database_sha256=sha256_bytes(b"database"),
            serving_database_size_bytes=8,
            vector_sidecar_sha256=sha256_bytes(b"vectors"),
        )
