from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from cardrag_core.candidate_acceptance import (
    CANDIDATE_ISSUERS,
    MCP_TOOLS,
    RECEIPT_SCHEMA,
    V109_IDENTITY_ASSETS,
    CandidateAcceptanceError,
    CandidateAcceptanceReceipt,
    CandidateEvidenceBindings,
    CandidateImageIdentity,
    EffectiveConfigEvidence,
    EvidenceFile,
    GenerationCASEvidence,
    IssuerRunMetrics,
    MCPSmokeEvidence,
    NativeCacheAuditEvidence,
    NativeCacheObject,
    NativeCacheSnapshot,
    RollbackLedgerEvidence,
    RollbackStep,
    ToolSmokeResult,
    V109AssetIdentity,
    V109IdentityEvidence,
    WorkerMetricsEvidence,
    main,
    verify_candidate_acceptance,
)
from cardrag_core.canonical import canonical_json_bytes, canonical_sha256, sha256_bytes
from cardrag_core.domain import ArtifactRef
from cardrag_core.manifests import (
    EMBEDDING_VIEW_TYPES,
    DocumentAggregationBootstrap,
    DocumentAggregationProfile,
    EmbeddingContract,
    EmbeddingProfile,
    EmbeddingVectorSidecar,
    EmbeddingViewCount,
    GenerationCounts,
    GenerationDocument,
    GenerationManifest,
    GenerationPointer,
    GenerationReady,
    IssuerOCRCounts,
    IssuerParserProfile,
    MaxChildAggregationDefinition,
    StructureContract,
    StructureMajorClassCounts,
    StructureNodeCounts,
    StructureRevisionCounts,
    StructureSourceCoverage,
    sealed_v5_retrieval_policy,
)
from cardrag_core.paths import generation_database_path, generation_vectors_path

SOURCE_COMMIT = "1" * 40
OTHER_SOURCE_COMMIT = "2" * 40
NOW = datetime(2026, 8, 30, 1, 2, 3, tzinfo=UTC)
ROWS = 8
IMAGE_REPOSITORY = "ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate"


@dataclass
class AcceptanceBundle:
    root: Path
    receipt_path: Path
    receipt: CandidateAcceptanceReceipt
    models: dict[str, BaseModel]

    @property
    def receipt_sha256(self) -> str:
        return hashlib.sha256(self.receipt_path.read_bytes()).hexdigest()


def _manifest() -> GenerationManifest:
    generation_id = "candidate-generation"
    embedding_profile = EmbeddingProfile.qwen3(provider_id="deepinfra", maximum_tokens=8192)
    exact_row_corpus_sha256 = sha256_bytes(b"exact candidate row corpus")
    aggregation_profile = DocumentAggregationProfile(
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
        embedding_profile_id=embedding_profile.profile_id,
        exact_row_corpus_sha256=exact_row_corpus_sha256,
        generation_id="aggregation-evaluation-generation",
        generation_manifest_sha256=sha256_bytes(b"aggregation evaluation manifest"),
        gold_sha256=sha256_bytes(b"sealed gold"),
        score_artifact_sha256=sha256_bytes(b"sealed exact scores"),
        selection_objective="ndcg_at_10",
    )
    documents = tuple(
        GenerationDocument(
            document_id=f"doc-{issuer}",
            issuer=issuer,
            pdf=ArtifactRef.for_cas(
                sha256=sha256_bytes(f"{issuer} pdf".encode()),
                size_bytes=len(f"{issuer} pdf".encode()),
                media_type="application/pdf",
            ),
            ocr=ArtifactRef.for_cas(
                sha256=sha256_bytes(f"{issuer} ocr".encode()),
                size_bytes=len(f"{issuer} ocr".encode()),
                media_type="text/markdown; charset=utf-8",
            ),
            page_count=1,
            availability="available",
        )
        for issuer in CANDIDATE_ISSUERS
    )
    issuer_counts = tuple(
        IssuerOCRCounts(issuer=issuer, acquired=1, succeeded=1, failed=0) for issuer in CANDIDATE_ISSUERS
    )
    source_hash = sha256_bytes(b"all candidate non-whitespace OCR characters")
    profile_sha256 = aggregation_profile.profile_sha256
    return GenerationManifest(
        schema_version="cardrag.generation.v5",
        generation_id=generation_id,
        created_at=NOW,
        serving_schema="cardrag.serving-db.v5",
        serving_database=ArtifactRef(
            sha256=sha256_bytes(b"candidate sqlite"),
            size_bytes=len(b"candidate sqlite"),
            media_type="application/vnd.sqlite3",
            path=generation_database_path(generation_id).as_posix(),
        ),
        corpus_sha256=sha256_bytes(b"candidate corpus"),
        contract_sha256=sha256_bytes(b"candidate contract"),
        embedding_contract=EmbeddingContract(
            provider=embedding_profile.provider,
            model=embedding_profile.model,
            dimension=4096,
            count=ROWS,
        ),
        issuer_codes=CANDIDATE_ISSUERS,
        counts=GenerationCounts(
            documents=4,
            pdf_objects=4,
            ocr_objects=4,
            chunks=ROWS,
        ),
        documents=documents,
        issuer_ocr_counts=issuer_counts,
        structure_contract=StructureContract(
            schema_version="cardrag.structure.v2",
            parser_profiles=tuple(
                IssuerParserProfile(
                    issuer=issuer,
                    profile_id=f"cardrag.parser.{issuer}.v1",
                    profile_sha256=sha256_bytes(f"{issuer} parser".encode()),
                )
                for issuer in CANDIDATE_ISSUERS
            ),
            node_counts=StructureNodeCounts(
                total=16,
                root=4,
                major_section=4,
                item=4,
                paragraph=4,
                list_item=0,
                table=0,
                table_row=0,
                footnote=0,
                boilerplate=0,
                unclassified=0,
            ),
            major_class_counts=StructureMajorClassCounts(
                total=4,
                benefit=1,
                notice=1,
                mixed=1,
                unknown=1,
            ),
            source_coverage=StructureSourceCoverage(
                source_non_whitespace_characters=400,
                covered_non_whitespace_characters=400,
                source_non_whitespace_sha256=source_hash,
                covered_non_whitespace_sha256=source_hash,
            ),
            revision_counts=StructureRevisionCounts(
                total=4,
                current=4,
                superseded=0,
                ambiguous=0,
            ),
            cross_contract_parent_count=0,
            cross_contract_link_count=0,
            lineages_with_multiple_current_revisions=0,
        ),
        embedding_profiles=(embedding_profile,),
        primary_embedding_profile_id=embedding_profile.profile_id,
        embedding_view_counts=tuple(
            EmbeddingViewCount(view_type=view_type, count=ROWS if index == 0 else 0)
            for index, view_type in enumerate(EMBEDDING_VIEW_TYPES)
        ),
        vector_sidecar=EmbeddingVectorSidecar(
            artifact=ArtifactRef(
                sha256=sha256_bytes(b"candidate vectors"),
                size_bytes=ROWS * 4096 * 4,
                media_type="application/octet-stream",
                path=generation_vectors_path(generation_id).as_posix(),
            ),
            profile_id=embedding_profile.profile_id,
            row_count=ROWS,
            dimension=4096,
            dtype="float32",
            byte_order="little-endian",
            layout="row-major",
            normalization="l2",
        ),
        parser_policy_sha256=sha256_bytes(b"parser policy"),
        embedding_policy_sha256=sha256_bytes(b"embedding policy"),
        retrieval_policy_sha256=canonical_sha256(
            sealed_v5_retrieval_policy(aggregation_profile, profile_sha256)
        ),
        document_aggregation_profile=aggregation_profile,
        document_aggregation_policy=aggregation_profile.aggregation_policy,
        sealed_profile_sha256=profile_sha256,
        exact_row_corpus_sha256=exact_row_corpus_sha256,
    )


def _file_binding(path: str, raw: bytes) -> EvidenceFile:
    return EvidenceFile(
        path=path,
        sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
    )


def _write_valid_bundle(root: Path) -> AcceptanceBundle:
    root.mkdir(parents=True, exist_ok=True)
    manifest = _manifest()
    vector_sidecar = manifest.vector_sidecar
    aggregation_profile = manifest.document_aggregation_profile
    embedding_profile = next(
        (
            profile
            for profile in manifest.embedding_profiles
            if profile.profile_id == manifest.primary_embedding_profile_id
        ),
        None,
    )
    assert vector_sidecar is not None
    assert aggregation_profile is not None
    assert embedding_profile is not None
    ready = GenerationReady(
        generation_id=manifest.generation_id,
        manifest_sha256=manifest.manifest_sha256,
        serving_database_sha256=manifest.serving_database.sha256,
        serving_database_size_bytes=manifest.serving_database.size_bytes,
        vector_sidecar_sha256=vector_sidecar.artifact.sha256,
        vector_sidecar_size_bytes=vector_sidecar.artifact.size_bytes,
    )
    pointer = GenerationPointer(
        generation_id=manifest.generation_id,
        manifest_sha256=manifest.manifest_sha256,
        ready_sha256=canonical_sha256(ready),
    )
    generation_objects_by_path = {
        artifact.path: artifact
        for document in manifest.documents
        for artifact in (document.pdf, document.ocr)
        if artifact is not None
    }
    generation_objects = tuple(
        generation_objects_by_path[path] for path in sorted(generation_objects_by_path)
    )
    config = EffectiveConfigEvidence(
        schema_version="cardrag.candidate-effective-config.v3",
        source_commit=SOURCE_COMMIT,
        release_version="1.0.14",
        compose_project="cardrag-v114-candidate",
        channel="candidate-v1.0.11",
        worker_volume="cardrag-worker-v114-candidate-state",
        worker_state_mount_path="/var/lib/cardrag-worker",
        worker_codex_home_volume="cardrag-worker-v114-candidate-codex-home",
        worker_codex_home_mount_path="/var/lib/cardrag-codex-home",
        worker_codex_auth_root="/var/lib/cardrag-codex-home",
        worker_home="/var/lib/cardrag-codex-home/home",
        mcp_volume="cardrag-mcp-v114-candidate-state",
        mcp_host="127.0.0.1",
        mcp_port=18014,
        rootfs_read_only=True,
        cap_drop_all=True,
        no_new_privileges=True,
        worker_seccomp_unconfined=True,
        worker_apparmor_unconfined=True,
        worker_systempaths_unconfined=False,
        worker_privileged=False,
        worker_cap_add_count=0,
        v109_volume_rw_mounts=0,
        worker_max_state_bytes=137438953472,
        worker_reserved_free_space_bytes=2147483648,
        worker_max_vector_sidecar_bytes=17179869184,
        worker_max_serving_database_bytes=34359738368,
        worker_minimum_start_free_bytes=34359738368,
        mcp_max_vector_bytes=1073741824,
        mcp_max_resident_vector_bytes=1073741824,
        mcp_max_vector_sidecar_bytes=17179869184,
        mcp_max_serving_database_bytes=34359738368,
        mcp_max_generation_download_bytes=68719476736,
        mcp_max_state_bytes=137438953472,
        mcp_reserved_free_space_bytes=2147483648,
        mcp_exhaustive_audit_max_jobs=32,
        mcp_exhaustive_audit_max_total_bytes=2147483648,
        mcp_exhaustive_audit_max_artifact_bytes=268435456,
        mcp_reranker_audit_max_jobs=1024,
        mcp_reranker_audit_max_total_bytes=536870912,
        mcp_reranker_audit_max_artifact_bytes=8388608,
        issuers=CANDIDATE_ISSUERS,
        worker_image=CandidateImageIdentity(
            role="worker",
            repository=IMAGE_REPOSITORY,
            digest=f"sha256:{'a' * 64}",
            compose_image_reference=f"{IMAGE_REPOSITORY}@sha256:{'a' * 64}",
            index_media_type="application/vnd.oci.image.index.v1+json",
            index_manifest_count=2,
            platform_manifest_digest=f"sha256:{'c' * 64}",
            platform_config_digest=f"sha256:{'1' * 64}",
            platform_manifest_media_type="application/vnd.oci.image.manifest.v1+json",
            platform_os="linux",
            platform_architecture="amd64",
            attestation_manifest_digest=f"sha256:{'d' * 64}",
            attestation_manifest_media_type="application/vnd.oci.image.manifest.v1+json",
            attestation_os="unknown",
            attestation_architecture="unknown",
            attestation_reference_type="attestation-manifest",
            attestation_subject_digest=f"sha256:{'c' * 64}",
            revision=SOURCE_COMMIT,
            version="1.0.14",
            platform="linux/amd64",
            entrypoint="cardrag-worker",
            user="10001:10001",
        ),
        mcp_image=CandidateImageIdentity(
            role="mcp",
            repository=IMAGE_REPOSITORY,
            digest=f"sha256:{'b' * 64}",
            compose_image_reference=f"{IMAGE_REPOSITORY}@sha256:{'b' * 64}",
            index_media_type="application/vnd.oci.image.index.v1+json",
            index_manifest_count=2,
            platform_manifest_digest=f"sha256:{'e' * 64}",
            platform_config_digest=f"sha256:{'2' * 64}",
            platform_manifest_media_type="application/vnd.oci.image.manifest.v1+json",
            platform_os="linux",
            platform_architecture="amd64",
            attestation_manifest_digest=f"sha256:{'f' * 64}",
            attestation_manifest_media_type="application/vnd.oci.image.manifest.v1+json",
            attestation_os="unknown",
            attestation_architecture="unknown",
            attestation_reference_type="attestation-manifest",
            attestation_subject_digest=f"sha256:{'e' * 64}",
            revision=SOURCE_COMMIT,
            version="1.0.14",
            platform="linux/amd64",
            entrypoint="cardrag-mcp",
            user="10001:10001",
        ),
        candidate_webdav_namespace_sha256=sha256_bytes(b"candidate namespace"),
        stable_channel_used=False,
        stable_publication_approved=False,
        v109_seed_access="read-only",
        ocr_cache_mode="read-only",
        ocr_cache_publication_approved=False,
        remote_gc_approved=False,
        collect_remote_garbage=False,
        experimental_map_reduce_enabled=False,
        embedding_model="qwen/qwen3-embedding-8b",
        embedding_dimension=4096,
        embedding_dtype="float32",
        embedding_normalization="l2",
        embedding_provider_id=embedding_profile.provider_id,
        embedding_maximum_tokens=embedding_profile.maximum_tokens,
        embedding_request_max_attempts=12,
        embedding_retry_base_seconds=1,
        embedding_retry_cap_seconds=60,
        retrieval_mode="exact-all-active-rows.v1",
        candidate_prefilter="none",
        approximate=False,
        document_aggregation_profile_sha256=aggregation_profile.profile_sha256,
        document_aggregation_policy=aggregation_profile.aggregation_policy,
        retrieval_policy_sha256=manifest.retrieval_policy_sha256,
    )
    issuer_metrics = tuple(
        IssuerRunMetrics(
            issuer=row.issuer,
            acquired=row.acquired,
            succeeded=row.succeeded,
            failed=row.failed,
        )
        for row in manifest.issuer_ocr_counts
    )
    generation_write_requests = 13
    worker = WorkerMetricsEvidence(
        schema_version="cardrag.candidate-worker-metrics.v3",
        source_commit=SOURCE_COMMIT,
        generation_id=manifest.generation_id,
        generation_manifest_sha256=manifest.manifest_sha256,
        effective_config_sha256=canonical_sha256(config),
        runtime_image_repo_digest=config.worker_image.compose_image_reference,
        runtime_container_image_id=config.worker_image.platform_config_digest,
        runtime_image_store_identity="classic-config-id",
        runtime_container_config_image=config.worker_image.compose_image_reference,
        runtime_manifest_descriptor_digest=None,
        runtime_manifest_descriptor_platform=None,
        runtime_uid_gid="10001:10001",
        rootfs_read_only_verified=True,
        cap_drop_all_verified=True,
        no_new_privileges_verified=True,
        seccomp_unconfined_verified=True,
        apparmor_unconfined_verified=True,
        systempaths_unconfined_verified=False,
        privileged_verified=False,
        cap_add_count_verified=0,
        worker_state_mount_path="/var/lib/cardrag-worker",
        codex_home_mount_path="/var/lib/cardrag-codex-home",
        codex_auth_root="/var/lib/cardrag-codex-home",
        codex_home="/var/lib/cardrag-codex-home",
        home="/var/lib/cardrag-codex-home/home",
        codex_home_separate_volume_verified=True,
        worker_state_legacy_codex_auth_entries=0,
        codex_auth_json_mode="0600",
        codex_auth_json_uid_gid="10001:10001",
        codex_login_status_verified=True,
        codex_login_status_output_retained=False,
        codex_version_verified=True,
        bubblewrap_version_verified=True,
        bubblewrap_user_namespace_verified=True,
        codex_read_only_sandbox_verified=True,
        codex_general_file_read_and_exec_tools_disabled_verified=True,
        codex_shell_environment_inherit_none_verified=True,
        ocr_credential_token_rejection_verified=True,
        full_candidate_run=True,
        run_completed=True,
        issuer_metrics=issuer_metrics,
        documents=manifest.counts.documents,
        chunks=manifest.counts.chunks,
        embedding_rows=vector_sidecar.row_count,
        vector_sidecar_size_bytes=vector_sidecar.artifact.size_bytes,
        embedding_dimension=4096,
        structure_source_non_whitespace_characters=400,
        structure_covered_non_whitespace_characters=400,
        cross_contract_parent_count=0,
        cross_contract_link_count=0,
        embedding_provider_calls=ROWS,
        pdf_seed_hits=2,
        ocr_native_cache_hits=2,
        ocr_native_cache_misses=2,
        native_cache_publication_calls=0,
        generation_publication_calls=generation_write_requests,
    )
    mcp = MCPSmokeEvidence(
        schema_version="cardrag.candidate-mcp-smoke.v2",
        source_commit=SOURCE_COMMIT,
        generation_id=manifest.generation_id,
        generation_manifest_sha256=manifest.manifest_sha256,
        effective_config_sha256=canonical_sha256(config),
        runtime_image_repo_digest=config.mcp_image.compose_image_reference,
        runtime_container_image_id=config.mcp_image.platform_config_digest,
        runtime_image_store_identity="classic-config-id",
        runtime_container_config_image=config.mcp_image.compose_image_reference,
        runtime_manifest_descriptor_digest=None,
        runtime_manifest_descriptor_platform=None,
        runtime_uid_gid="10001:10001",
        rootfs_read_only_verified=True,
        cap_drop_all_verified=True,
        no_new_privileges_verified=True,
        health_ready=True,
        serving_schema="cardrag.serving-db.v5",
        embedding_dimension=4096,
        retrieval_mode="exact",
        approximate=False,
        expected_active_contracts=4,
        scored_contracts=4,
        expected_embedding_rows=ROWS,
        scored_embedding_rows=ROWS,
        exact_blocks=2,
        cross_contract_node_count=0,
        discovered_tools=MCP_TOOLS,
        tool_results=tuple(
            ToolSmokeResult(tool=tool, passed=True, response_sha256=sha256_bytes(tool.encode()))
            for tool in MCP_TOOLS
        ),
        bundle_source_spans_verified=True,
        revision_history_verified=True,
        legacy_adapter_verified=True,
        pdf_range_status=206,
        pdf_magic_prefix="%PDF-",
        pdf_content_range_verified=True,
    )
    hit_reuse_key = sha256_bytes(b"native cache hit reuse key")
    miss_reuse_key = sha256_bytes(b"native cache miss reuse key")
    native_entries = tuple(
        sorted(
            (
                NativeCacheObject(
                    path=(f"v1/ocr-cache/native/{hit_reuse_key[:2]}/{hit_reuse_key}/manifest.json"),
                    status=200,
                    sha256=sha256_bytes(b"native hit manifest"),
                    size_bytes=len(b"native hit manifest"),
                ),
                NativeCacheObject(
                    path=f"v1/ocr-cache/native/{hit_reuse_key[:2]}/{hit_reuse_key}/READY.json",
                    status=200,
                    sha256=sha256_bytes(b"native hit ready"),
                    size_bytes=len(b"native hit ready"),
                ),
                NativeCacheObject(
                    path=(f"v1/ocr-cache/native/{miss_reuse_key[:2]}/{miss_reuse_key}/manifest.json"),
                    status=404,
                ),
                NativeCacheObject(
                    path=(f"v1/ocr-cache/native/{miss_reuse_key[:2]}/{miss_reuse_key}/READY.json"),
                    status=404,
                ),
            ),
            key=lambda entry: entry.path,
        )
    )
    inventory_sha256 = canonical_sha256(
        {
            "entries": native_entries,
            "schema_version": "cardrag.native-cache-control-inventory.v1",
        }
    )
    before = NativeCacheSnapshot(
        schema_version="cardrag.native-cache-control-snapshot.v1",
        source_commit=SOURCE_COMMIT,
        phase="before",
        namespace="v1/ocr-cache/native",
        entries=native_entries,
        inventory_sha256=inventory_sha256,
    )
    after = NativeCacheSnapshot(
        schema_version="cardrag.native-cache-control-snapshot.v1",
        source_commit=SOURCE_COMMIT,
        phase="after",
        namespace="v1/ocr-cache/native",
        entries=native_entries,
        inventory_sha256=inventory_sha256,
    )
    native_audit = NativeCacheAuditEvidence(
        schema_version="cardrag.native-cache-zero-write-audit.v1",
        source_commit=SOURCE_COMMIT,
        generation_id=manifest.generation_id,
        cache_mode="read-only",
        before_inventory_sha256=inventory_sha256,
        after_inventory_sha256=inventory_sha256,
        cache_hit_count=1,
        cache_miss_count=1,
        native_get_requests=8,
        native_head_requests=0,
        native_write_requests=0,
        native_publication_calls=0,
        native_created_paths=0,
        native_modified_paths=0,
        native_deleted_paths=0,
        verified_read_only_seed=True,
    )
    generation_cas = GenerationCASEvidence(
        schema_version="cardrag.candidate-generation-cas-audit.v1",
        source_commit=SOURCE_COMMIT,
        channel="candidate-v1.0.11",
        generation_id=manifest.generation_id,
        manifest_sha256=manifest.manifest_sha256,
        ready_sha256=canonical_sha256(ready),
        pointer_sha256=canonical_sha256(pointer),
        serving_database=manifest.serving_database,
        vector_sidecar=vector_sidecar.artifact,
        objects=generation_objects,
        object_publish_calls=len(generation_objects),
        object_create_writes=0,
        database_puts=1,
        vector_puts=1,
        manifest_puts=1,
        ready_puts=1,
        pointer_cas_attempts=1,
        pointer_cas_successes=1,
        logical_publication_calls=generation_write_requests,
        total_generation_write_requests=17,
        native_cache_write_requests=0,
        stable_channel_write_requests=0,
    )
    rollback = RollbackLedgerEvidence(
        schema_version="cardrag.candidate-v4-v5-rollback-ledger.v1",
        source_commit=SOURCE_COMMIT,
        channel="candidate-v1.0.11",
        steps=(
            RollbackStep(
                ordinal=1,
                action="activate",
                serving_schema="cardrag.serving-db.v4",
                generation_id="legacy-v4",
                runtime_instance_sha256=sha256_bytes(b"runtime v4 before"),
                health_ready=True,
                tool_discovery_passed=True,
                search_mode="legacy-hybrid",
                search_contracts_outcome="unsupported-rejected",
            ),
            RollbackStep(
                ordinal=2,
                action="activate",
                serving_schema="cardrag.serving-db.v5",
                generation_id=manifest.generation_id,
                runtime_instance_sha256=sha256_bytes(b"runtime v5 before restart"),
                health_ready=True,
                tool_discovery_passed=True,
                search_mode="exact",
                search_contracts_outcome="exact-passed",
            ),
            RollbackStep(
                ordinal=3,
                action="restart",
                serving_schema="cardrag.serving-db.v5",
                generation_id=manifest.generation_id,
                runtime_instance_sha256=sha256_bytes(b"runtime v5 after restart"),
                health_ready=True,
                tool_discovery_passed=True,
                search_mode="exact",
                search_contracts_outcome="exact-passed",
            ),
            RollbackStep(
                ordinal=4,
                action="activate",
                serving_schema="cardrag.serving-db.v4",
                generation_id="legacy-v4",
                runtime_instance_sha256=sha256_bytes(b"runtime v4 after"),
                health_ready=True,
                tool_discovery_passed=True,
                search_mode="legacy-hybrid",
                search_contracts_outcome="unsupported-rejected",
            ),
            RollbackStep(
                ordinal=5,
                action="activate",
                serving_schema="cardrag.serving-db.v5",
                generation_id=manifest.generation_id,
                runtime_instance_sha256=sha256_bytes(b"runtime v5 final"),
                health_ready=True,
                tool_discovery_passed=True,
                search_mode="exact",
                search_contracts_outcome="exact-passed",
            ),
        ),
        rollback_verified=True,
        stable_channel_write_requests=0,
    )
    v109 = V109IdentityEvidence(
        schema_version="cardrag.v109-before-after-identity.v1",
        source_commit=SOURCE_COMMIT,
        assets=tuple(
            V109AssetIdentity(
                asset=asset,
                before_sha256=sha256_bytes(f"v109 {asset}".encode()),
                after_sha256=sha256_bytes(f"v109 {asset}".encode()),
                equal=True,
            )
            for asset in V109_IDENTITY_ASSETS
        ),
        candidate_rw_mounts_of_v109_volumes=0,
        candidate_stable_channel_requests=0,
        candidate_librechat_switch_requests=0,
        destructive_cleanup_commands=0,
        v109_restart_commands=0,
    )
    models: dict[str, BaseModel] = {
        "effective_config": config,
        "generation_manifest": manifest,
        "generation_ready": ready,
        "candidate_pointer": pointer,
        "worker_metrics": worker,
        "mcp_smoke": mcp,
        "native_cache_before": before,
        "native_cache_after": after,
        "native_cache_audit": native_audit,
        "generation_cas": generation_cas,
        "rollback_ledger": rollback,
        "v109_identity": v109,
    }
    names = {
        "effective_config": "effective-config.json",
        "generation_manifest": "serving-generation-manifest.json",
        "generation_ready": "serving-generation-READY.json",
        "candidate_pointer": "candidate-pointer.json",
        "worker_metrics": "worker-metrics.json",
        "mcp_smoke": "mcp-smoke.json",
        "native_cache_before": "native-cache-before.json",
        "native_cache_after": "native-cache-after.json",
        "native_cache_audit": "native-cache-audit.json",
        "generation_cas": "generation-cas-audit.json",
        "rollback_ledger": "rollback-ledger.json",
        "v109_identity": "v109-identity.json",
    }
    no_lf = {"generation_manifest", "generation_ready", "candidate_pointer"}
    bindings: dict[str, EvidenceFile] = {}
    for field, model in models.items():
        raw = canonical_json_bytes(model) + (b"" if field in no_lf else b"\n")
        (root / names[field]).write_bytes(raw)
        bindings[field] = _file_binding(names[field], raw)
    receipt = CandidateAcceptanceReceipt(
        schema_version=RECEIPT_SCHEMA,
        release_version="1.0.14",
        source_commit=SOURCE_COMMIT,
        compose_project="cardrag-v114-candidate",
        channel="candidate-v1.0.11",
        generation_id=manifest.generation_id,
        issuers=CANDIDATE_ISSUERS,
        release_eligible=True,
        evidence=CandidateEvidenceBindings(**bindings),
    )
    receipt_path = root / "candidate-acceptance-receipt.json"
    receipt_path.write_bytes(receipt.canonical_bytes())
    return AcceptanceBundle(root=root, receipt_path=receipt_path, receipt=receipt, models=models)


def _rewrite_bound_file(bundle: AcceptanceBundle, field: str, raw: bytes) -> None:
    old_binding = getattr(bundle.receipt.evidence, field)
    (bundle.root / old_binding.path).write_bytes(raw)
    evidence_payload = bundle.receipt.evidence.model_dump(mode="python")
    evidence_payload[field] = _file_binding(old_binding.path, raw)
    receipt_payload = bundle.receipt.model_dump(mode="python")
    receipt_payload["evidence"] = CandidateEvidenceBindings.model_validate(evidence_payload)
    bundle.receipt = CandidateAcceptanceReceipt.model_validate(receipt_payload)
    bundle.receipt_path.write_bytes(bundle.receipt.canonical_bytes())


def _verify(bundle: AcceptanceBundle) -> None:
    validation = verify_candidate_acceptance(
        bundle.receipt_path.resolve(),
        bundle.root.resolve(),
        expected_receipt_sha256=bundle.receipt_sha256,
        expected_source_commit=SOURCE_COMMIT,
        expected_image_repository=IMAGE_REPOSITORY,
    )
    assert validation.status == "validated"
    assert validation.source_commit == SOURCE_COMMIT
    assert validation.generation_id == "candidate-generation"
    assert validation.worker_image == bundle.models["effective_config"].worker_image
    assert validation.mcp_image == bundle.models["effective_config"].mcp_image


def test_acceptance_verifier_cross_binds_the_complete_candidate_evidence(tmp_path: Path) -> None:
    bundle = _write_valid_bundle(tmp_path)

    _verify(bundle)


def test_acceptance_allows_an_all_hit_embedding_run_and_independent_native_counts(
    tmp_path: Path,
) -> None:
    bundle = _write_valid_bundle(tmp_path)
    worker = bundle.models["worker_metrics"]
    payload = worker.model_dump(mode="python")
    payload["embedding_provider_calls"] = 0
    all_hit_worker = WorkerMetricsEvidence.model_validate(payload)
    _rewrite_bound_file(bundle, "worker_metrics", all_hit_worker.canonical_bytes())

    _verify(bundle)


def test_candidate_receipt_rejects_even_one_failed_issuer_document() -> None:
    with pytest.raises(ValidationError):
        IssuerRunMetrics(
            issuer="kb",
            acquired=20,
            succeeded=19,
            failed=1,
        )


def test_acceptance_cli_is_canonical_and_fails_without_leaking_evidence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle = _write_valid_bundle(tmp_path)
    arguments = [
        "--receipt",
        str(bundle.receipt_path.resolve()),
        "--evidence-root",
        str(bundle.root.resolve()),
        "--expected-receipt-sha256",
        bundle.receipt_sha256,
        "--expected-source-commit",
        SOURCE_COMMIT,
        "--expected-image-repository",
        IMAGE_REPOSITORY,
    ]

    assert main(arguments) == 0
    success = capsys.readouterr()
    assert success.err == ""
    assert success.out.endswith("\n")
    assert '"status":"validated"' in success.out
    assert f'"repository":"{IMAGE_REPOSITORY}"' in success.out
    assert f'"digest":"sha256:{"a" * 64}"' in success.out
    assert f'"digest":"sha256:{"b" * 64}"' in success.out

    arguments[-3] = OTHER_SOURCE_COMMIT
    assert main(arguments) == 1
    failure = capsys.readouterr()
    assert failure.out == ""
    assert failure.err == "candidate acceptance validation failed\n"
    assert SOURCE_COMMIT not in failure.err


@pytest.mark.parametrize(
    ("receipt_sha256", "source_commit", "code"),
    (
        ("0" * 64, SOURCE_COMMIT, "receipt_sha256_mismatch"),
        (None, OTHER_SOURCE_COMMIT, "receipt_source_commit_mismatch"),
        ("INVALID", SOURCE_COMMIT, "expected_receipt_sha256_invalid"),
        (None, "ABC", "expected_source_commit_invalid"),
    ),
)
def test_acceptance_rejects_unapproved_receipt_identity(
    tmp_path: Path,
    receipt_sha256: str | None,
    source_commit: str,
    code: str,
) -> None:
    bundle = _write_valid_bundle(tmp_path)

    with pytest.raises(CandidateAcceptanceError, match=f"^{code}$"):
        verify_candidate_acceptance(
            bundle.receipt_path.resolve(),
            bundle.root.resolve(),
            expected_receipt_sha256=receipt_sha256 or bundle.receipt_sha256,
            expected_source_commit=source_commit,
            expected_image_repository=IMAGE_REPOSITORY,
        )


def test_acceptance_rejects_an_untrusted_candidate_registry(tmp_path: Path) -> None:
    bundle = _write_valid_bundle(tmp_path)

    with pytest.raises(CandidateAcceptanceError, match="^image_repository_mismatch$"):
        verify_candidate_acceptance(
            bundle.receipt_path.resolve(),
            bundle.root.resolve(),
            expected_receipt_sha256=bundle.receipt_sha256,
            expected_source_commit=SOURCE_COMMIT,
            expected_image_repository="ghcr.io/attacker/candidate",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("index_manifest_count", 3),
        ("platform_architecture", "arm64"),
        ("attestation_os", "linux"),
        ("attestation_subject_digest", f"sha256:{'9' * 64}"),
        ("compose_image_reference", f"{IMAGE_REPOSITORY}@sha256:{'9' * 64}"),
        ("platform_config_digest", f"sha256:{'c' * 64}"),
    ),
)
def test_candidate_image_identity_rejects_non_exact_index_contract(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    bundle = _write_valid_bundle(tmp_path)
    image = bundle.models["effective_config"].worker_image
    payload = image.model_dump(mode="python")
    payload[field] = value

    with pytest.raises(ValidationError):
        CandidateImageIdentity.model_validate(payload)


def test_effective_config_rejects_a_cross_role_platform_manifest(tmp_path: Path) -> None:
    bundle = _write_valid_bundle(tmp_path)
    config = bundle.models["effective_config"]
    payload = config.model_dump(mode="python")
    worker_platform = config.worker_image.platform_manifest_digest
    payload["mcp_image"]["platform_manifest_digest"] = worker_platform
    payload["mcp_image"]["attestation_subject_digest"] = worker_platform

    with pytest.raises(ValidationError):
        EffectiveConfigEvidence.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("worker_codex_home_volume", "cardrag-worker-v114-candidate-state"),
        ("worker_codex_home_mount_path", "/var/lib/cardrag-worker/codex"),
        ("worker_codex_auth_root", "/var/lib/cardrag-worker/codex"),
        ("worker_home", "/var/lib/cardrag-worker/home"),
    ),
)
def test_effective_config_rejects_codex_home_state_overlap(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    bundle = _write_valid_bundle(tmp_path)
    payload = bundle.models["effective_config"].model_dump(mode="python")
    payload[field] = value

    with pytest.raises(ValidationError):
        EffectiveConfigEvidence.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "stable_volume"),
    (
        ("worker_volume", "cardrag-worker-v111-state"),
        ("worker_codex_home_volume", "cardrag-worker-v111-codex-home"),
        ("mcp_volume", "cardrag-mcp-v111-state"),
    ),
)
def test_effective_config_rejects_stable_volume_reuse(
    tmp_path: Path,
    field: str,
    stable_volume: str,
) -> None:
    bundle = _write_valid_bundle(tmp_path)
    payload = bundle.models["effective_config"].model_dump(mode="python")
    payload[field] = stable_volume

    with pytest.raises(ValidationError):
        EffectiveConfigEvidence.model_validate(payload)


@pytest.mark.parametrize(
    "field",
    (
        "worker_max_state_bytes",
        "worker_reserved_free_space_bytes",
        "worker_max_vector_sidecar_bytes",
        "worker_max_serving_database_bytes",
        "worker_minimum_start_free_bytes",
        "mcp_max_vector_bytes",
        "mcp_max_resident_vector_bytes",
        "mcp_max_vector_sidecar_bytes",
        "mcp_max_serving_database_bytes",
        "mcp_max_generation_download_bytes",
        "mcp_max_state_bytes",
        "mcp_reserved_free_space_bytes",
        "mcp_exhaustive_audit_max_jobs",
        "mcp_exhaustive_audit_max_total_bytes",
        "mcp_exhaustive_audit_max_artifact_bytes",
        "mcp_reranker_audit_max_jobs",
        "mcp_reranker_audit_max_total_bytes",
        "mcp_reranker_audit_max_artifact_bytes",
    ),
)
def test_effective_config_rejects_every_capacity_override(tmp_path: Path, field: str) -> None:
    bundle = _write_valid_bundle(tmp_path)
    config = bundle.models["effective_config"]
    payload = config.model_dump(mode="python")
    payload[field] = 0

    with pytest.raises(ValidationError):
        EffectiveConfigEvidence.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("embedding_request_max_attempts", 11),
        ("embedding_retry_base_seconds", 2),
        ("embedding_retry_cap_seconds", 61),
    ),
)
def test_effective_config_rejects_every_retry_override(
    tmp_path: Path,
    field: str,
    value: int,
) -> None:
    bundle = _write_valid_bundle(tmp_path)
    payload = bundle.models["effective_config"].model_dump(mode="python")
    payload[field] = value

    with pytest.raises(ValidationError):
        EffectiveConfigEvidence.model_validate(payload)


@pytest.mark.parametrize(
    "field",
    (
        "embedding_request_max_attempts",
        "embedding_retry_base_seconds",
        "embedding_retry_cap_seconds",
    ),
)
def test_effective_config_requires_every_retry_evidence_field(
    tmp_path: Path,
    field: str,
) -> None:
    bundle = _write_valid_bundle(tmp_path)
    payload = bundle.models["effective_config"].model_dump(mode="python")
    del payload[field]

    with pytest.raises(ValidationError):
        EffectiveConfigEvidence.model_validate(payload)


def test_effective_config_rejects_pre_retry_schema(tmp_path: Path) -> None:
    bundle = _write_valid_bundle(tmp_path)
    payload = bundle.models["effective_config"].model_dump(mode="python")
    payload["schema_version"] = "cardrag.candidate-effective-config.v2"

    with pytest.raises(ValidationError):
        EffectiveConfigEvidence.model_validate(payload)


def test_acceptance_requires_worker_sandbox_runtime_smoke(tmp_path: Path) -> None:
    bundle = _write_valid_bundle(tmp_path)
    worker = bundle.models["worker_metrics"]
    raw = worker.canonical_bytes().replace(
        b'"bubblewrap_user_namespace_verified":true',
        b'"bubblewrap_user_namespace_verified":false',
    )
    _rewrite_bound_file(bundle, "worker_metrics", raw)

    with pytest.raises(CandidateAcceptanceError, match="^json_schema_invalid$"):
        _verify(bundle)


@pytest.mark.parametrize(
    ("evidence_name", "field", "value", "code"),
    (
        (
            "worker_metrics",
            "runtime_image_repo_digest",
            f"{IMAGE_REPOSITORY}@sha256:{'9' * 64}",
            "worker_metrics_binding_mismatch",
        ),
        (
            "worker_metrics",
            "runtime_container_image_id",
            f"sha256:{'9' * 64}",
            "worker_metrics_binding_mismatch",
        ),
        (
            "worker_metrics",
            "runtime_container_config_image",
            f"{IMAGE_REPOSITORY}@sha256:{'9' * 64}",
            "worker_metrics_binding_mismatch",
        ),
        (
            "worker_metrics",
            "runtime_image_store_identity",
            "containerd-index-id",
            "worker_metrics_binding_mismatch",
        ),
        (
            "mcp_smoke",
            "runtime_image_repo_digest",
            f"{IMAGE_REPOSITORY}@sha256:{'9' * 64}",
            "mcp_smoke_binding_mismatch",
        ),
        (
            "mcp_smoke",
            "runtime_container_image_id",
            f"sha256:{'9' * 64}",
            "mcp_smoke_binding_mismatch",
        ),
        (
            "mcp_smoke",
            "runtime_container_config_image",
            f"{IMAGE_REPOSITORY}@sha256:{'9' * 64}",
            "mcp_smoke_binding_mismatch",
        ),
        (
            "mcp_smoke",
            "runtime_image_store_identity",
            "containerd-index-id",
            "mcp_smoke_binding_mismatch",
        ),
    ),
)
def test_acceptance_binds_runtime_containers_to_the_rendered_sealed_images(
    tmp_path: Path,
    evidence_name: str,
    field: str,
    value: str,
    code: str,
) -> None:
    bundle = _write_valid_bundle(tmp_path)
    evidence = bundle.models[evidence_name]
    payload = evidence.model_dump(mode="python")
    payload[field] = value
    mismatched = type(evidence).model_validate(payload)
    _rewrite_bound_file(bundle, evidence_name, mismatched.canonical_bytes())

    with pytest.raises(CandidateAcceptanceError, match=f"^{code}$"):
        _verify(bundle)


@pytest.mark.parametrize(
    ("evidence_name", "image_field"),
    (("worker_metrics", "worker_image"), ("mcp_smoke", "mcp_image")),
)
def test_acceptance_allows_containerd_index_identity_with_exact_platform_descriptor(
    tmp_path: Path,
    evidence_name: str,
    image_field: str,
) -> None:
    bundle = _write_valid_bundle(tmp_path)
    evidence = bundle.models[evidence_name]
    image = getattr(bundle.models["effective_config"], image_field)
    payload = evidence.model_dump(mode="python")
    payload.update(
        runtime_image_store_identity="containerd-index-id",
        runtime_container_image_id=image.digest,
        runtime_manifest_descriptor_digest=image.platform_manifest_digest,
        runtime_manifest_descriptor_platform="linux/amd64",
    )
    containerd = type(evidence).model_validate(payload)
    _rewrite_bound_file(bundle, evidence_name, containerd.canonical_bytes())

    _verify(bundle)


@pytest.mark.parametrize(
    ("evidence_name", "image_field"),
    (("worker_metrics", "worker_image"), ("mcp_smoke", "mcp_image")),
)
def test_acceptance_allows_classic_config_identity_with_exact_optional_descriptor(
    tmp_path: Path,
    evidence_name: str,
    image_field: str,
) -> None:
    bundle = _write_valid_bundle(tmp_path)
    evidence = bundle.models[evidence_name]
    image = getattr(bundle.models["effective_config"], image_field)
    payload = evidence.model_dump(mode="python")
    payload.update(
        runtime_manifest_descriptor_digest=image.platform_manifest_digest,
        runtime_manifest_descriptor_platform="linux/amd64",
    )
    classic = type(evidence).model_validate(payload)
    _rewrite_bound_file(bundle, evidence_name, classic.canonical_bytes())

    _verify(bundle)


@pytest.mark.parametrize(
    ("evidence_name", "image_field"),
    (("worker_metrics", "worker_image"), ("mcp_smoke", "mcp_image")),
)
def test_runtime_manifest_descriptor_pair_must_be_complete(
    tmp_path: Path,
    evidence_name: str,
    image_field: str,
) -> None:
    bundle = _write_valid_bundle(tmp_path)
    evidence = bundle.models[evidence_name]
    image = getattr(bundle.models["effective_config"], image_field)
    payload = evidence.model_dump(mode="python")
    payload["runtime_manifest_descriptor_digest"] = image.platform_manifest_digest

    with pytest.raises(ValidationError, match="descriptor digest and platform must be paired"):
        type(evidence).model_validate(payload)


@pytest.mark.parametrize(
    ("evidence_name", "image_field", "invalid_identity"),
    (
        ("worker_metrics", "worker_image", "platform"),
        ("worker_metrics", "worker_image", "attestation"),
        ("worker_metrics", "worker_image", "config"),
        ("worker_metrics", "worker_image", "other"),
        ("mcp_smoke", "mcp_image", "platform"),
        ("mcp_smoke", "mcp_image", "attestation"),
        ("mcp_smoke", "mcp_image", "config"),
        ("mcp_smoke", "mcp_image", "other"),
    ),
)
def test_containerd_runtime_id_rejects_platform_attestation_and_other_digests(
    tmp_path: Path,
    evidence_name: str,
    image_field: str,
    invalid_identity: str,
) -> None:
    bundle = _write_valid_bundle(tmp_path)
    evidence = bundle.models[evidence_name]
    image = getattr(bundle.models["effective_config"], image_field)
    invalid_digest = {
        "platform": image.platform_manifest_digest,
        "attestation": image.attestation_manifest_digest,
        "config": image.platform_config_digest,
        "other": f"sha256:{'9' * 64}",
    }[invalid_identity]
    payload = evidence.model_dump(mode="python")
    payload.update(
        runtime_image_store_identity="containerd-index-id",
        runtime_container_image_id=invalid_digest,
        runtime_manifest_descriptor_digest=image.platform_manifest_digest,
        runtime_manifest_descriptor_platform="linux/amd64",
    )
    mismatched = type(evidence).model_validate(payload)
    _rewrite_bound_file(bundle, evidence_name, mismatched.canonical_bytes())

    code = (
        "worker_metrics_binding_mismatch"
        if evidence_name == "worker_metrics"
        else "mcp_smoke_binding_mismatch"
    )
    with pytest.raises(CandidateAcceptanceError, match=f"^{code}$"):
        _verify(bundle)


@pytest.mark.parametrize(
    ("evidence_name", "image_field", "invalid_identity"),
    (
        ("worker_metrics", "worker_image", "index"),
        ("worker_metrics", "worker_image", "platform"),
        ("worker_metrics", "worker_image", "attestation"),
        ("worker_metrics", "worker_image", "other"),
        ("mcp_smoke", "mcp_image", "index"),
        ("mcp_smoke", "mcp_image", "platform"),
        ("mcp_smoke", "mcp_image", "attestation"),
        ("mcp_smoke", "mcp_image", "other"),
    ),
)
def test_classic_runtime_id_rejects_index_platform_attestation_and_other_digests(
    tmp_path: Path,
    evidence_name: str,
    image_field: str,
    invalid_identity: str,
) -> None:
    bundle = _write_valid_bundle(tmp_path)
    evidence = bundle.models[evidence_name]
    image = getattr(bundle.models["effective_config"], image_field)
    invalid_digest = {
        "index": image.digest,
        "platform": image.platform_manifest_digest,
        "attestation": image.attestation_manifest_digest,
        "other": f"sha256:{'9' * 64}",
    }[invalid_identity]
    payload = evidence.model_dump(mode="python")
    payload["runtime_container_image_id"] = invalid_digest
    mismatched = type(evidence).model_validate(payload)
    _rewrite_bound_file(bundle, evidence_name, mismatched.canonical_bytes())

    code = (
        "worker_metrics_binding_mismatch"
        if evidence_name == "worker_metrics"
        else "mcp_smoke_binding_mismatch"
    )
    with pytest.raises(CandidateAcceptanceError, match=f"^{code}$"):
        _verify(bundle)


@pytest.mark.parametrize(
    ("evidence_name", "image_field"),
    (("worker_metrics", "worker_image"), ("mcp_smoke", "mcp_image")),
)
def test_containerd_runtime_rejects_wrong_platform_manifest_descriptor(
    tmp_path: Path,
    evidence_name: str,
    image_field: str,
) -> None:
    bundle = _write_valid_bundle(tmp_path)
    evidence = bundle.models[evidence_name]
    image = getattr(bundle.models["effective_config"], image_field)
    payload = evidence.model_dump(mode="python")
    payload.update(
        runtime_image_store_identity="containerd-index-id",
        runtime_container_image_id=image.digest,
        runtime_manifest_descriptor_digest=image.attestation_manifest_digest,
        runtime_manifest_descriptor_platform="linux/amd64",
    )
    mismatched = type(evidence).model_validate(payload)
    _rewrite_bound_file(bundle, evidence_name, mismatched.canonical_bytes())

    code = (
        "worker_metrics_binding_mismatch"
        if evidence_name == "worker_metrics"
        else "mcp_smoke_binding_mismatch"
    )
    with pytest.raises(CandidateAcceptanceError, match=f"^{code}$"):
        _verify(bundle)


def test_acceptance_rejects_tampered_or_symlinked_evidence(tmp_path: Path) -> None:
    bundle = _write_valid_bundle(tmp_path)
    metrics_path = bundle.root / bundle.receipt.evidence.worker_metrics.path
    metrics_path.write_bytes(metrics_path.read_bytes() + b" ")

    with pytest.raises(CandidateAcceptanceError, match="^evidence_size_mismatch$"):
        _verify(bundle)

    bundle = _write_valid_bundle(tmp_path / "symlink")
    smoke_path = bundle.root / bundle.receipt.evidence.mcp_smoke.path
    replacement = bundle.root / "replacement.json"
    replacement.write_bytes(smoke_path.read_bytes())
    smoke_path.unlink()
    smoke_path.symlink_to(replacement)
    with pytest.raises(CandidateAcceptanceError, match="^evidence_open_failed$"):
        _verify(bundle)


def test_acceptance_rejects_noncanonical_and_duplicate_json(tmp_path: Path) -> None:
    bundle = _write_valid_bundle(tmp_path)
    config = bundle.models["effective_config"]
    noncanonical = b" " + canonical_json_bytes(config) + b"\n"
    _rewrite_bound_file(bundle, "effective_config", noncanonical)
    with pytest.raises(CandidateAcceptanceError, match="^json_not_canonical$"):
        _verify(bundle)

    bundle = _write_valid_bundle(tmp_path / "duplicate")
    config_raw = canonical_json_bytes(bundle.models["effective_config"])
    duplicate = (
        config_raw.replace(
            b'"approximate":false,',
            b'"approximate":false,"approximate":false,',
            1,
        )
        + b"\n"
    )
    _rewrite_bound_file(bundle, "effective_config", duplicate)
    with pytest.raises(CandidateAcceptanceError, match="^json_duplicate_key$"):
        _verify(bundle)


@pytest.mark.parametrize(
    ("field", "key", "value"),
    (
        ("effective_config", "stable_channel_used", True),
        ("effective_config", "stable_publication_approved", True),
        ("effective_config", "remote_gc_approved", True),
        ("effective_config", "experimental_map_reduce_enabled", True),
        ("effective_config", "worker_seccomp_unconfined", False),
        ("effective_config", "worker_apparmor_unconfined", False),
        ("effective_config", "worker_systempaths_unconfined", True),
        ("effective_config", "worker_privileged", True),
        ("effective_config", "worker_cap_add_count", 1),
        ("worker_metrics", "seccomp_unconfined_verified", False),
        ("worker_metrics", "apparmor_unconfined_verified", False),
        ("worker_metrics", "systempaths_unconfined_verified", True),
        ("worker_metrics", "privileged_verified", True),
        ("worker_metrics", "cap_add_count_verified", 1),
        ("worker_metrics", "codex_home_separate_volume_verified", False),
        ("worker_metrics", "worker_state_legacy_codex_auth_entries", 1),
        ("worker_metrics", "codex_auth_json_mode", "0644"),
        ("worker_metrics", "codex_auth_json_uid_gid", "0:0"),
        ("worker_metrics", "codex_login_status_verified", False),
        ("worker_metrics", "codex_login_status_output_retained", True),
        (
            "worker_metrics",
            "codex_general_file_read_and_exec_tools_disabled_verified",
            False,
        ),
        ("worker_metrics", "codex_shell_environment_inherit_none_verified", False),
        ("worker_metrics", "ocr_credential_token_rejection_verified", False),
        ("worker_metrics", "structure_covered_non_whitespace_characters", 399),
        ("mcp_smoke", "cap_drop_all_verified", False),
        ("mcp_smoke", "no_new_privileges_verified", False),
        ("mcp_smoke", "scored_embedding_rows", ROWS - 1),
        ("native_cache_audit", "native_head_requests", 1),
        ("native_cache_audit", "native_write_requests", 1),
        ("generation_cas", "stable_channel_write_requests", 1),
        ("rollback_ledger", "stable_channel_write_requests", 1),
        ("v109_identity", "candidate_rw_mounts_of_v109_volumes", 1),
    ),
)
def test_acceptance_rejects_each_fail_closed_candidate_invariant(
    tmp_path: Path,
    field: str,
    key: str,
    value: object,
) -> None:
    bundle = _write_valid_bundle(tmp_path)
    payload = bundle.models[field].model_dump(mode="json")
    payload[key] = value
    _rewrite_bound_file(bundle, field, canonical_json_bytes(payload) + b"\n")

    with pytest.raises(CandidateAcceptanceError, match="^json_schema_invalid$"):
        _verify(bundle)


def test_acceptance_rejects_valid_ledgers_that_do_not_cross_bind(tmp_path: Path) -> None:
    bundle = _write_valid_bundle(tmp_path)
    generation_cas = bundle.models["generation_cas"]
    payload = generation_cas.model_dump(mode="python")
    payload["pointer_sha256"] = sha256_bytes(b"another pointer")
    mismatched = GenerationCASEvidence.model_validate(payload)
    _rewrite_bound_file(bundle, "generation_cas", mismatched.canonical_bytes())

    with pytest.raises(CandidateAcceptanceError, match="^generation_cas_binding_mismatch$"):
        _verify(bundle)


def test_acceptance_binds_mcp_active_contracts_to_manifest_revision_scope(tmp_path: Path) -> None:
    bundle = _write_valid_bundle(tmp_path)
    mcp = bundle.models["mcp_smoke"]
    payload = mcp.model_dump(mode="python")
    payload["expected_active_contracts"] = 5
    payload["scored_contracts"] = 5
    mismatched = MCPSmokeEvidence.model_validate(payload)
    _rewrite_bound_file(bundle, "mcp_smoke", mismatched.canonical_bytes())

    with pytest.raises(CandidateAcceptanceError, match="^mcp_smoke_binding_mismatch$"):
        _verify(bundle)


def test_acceptance_rejects_changed_native_control_inventory(tmp_path: Path) -> None:
    bundle = _write_valid_bundle(tmp_path)
    after = bundle.models["native_cache_after"]
    assert isinstance(after, NativeCacheSnapshot)
    changed_entries_list = list(after.entries)
    changed_index = next(index for index, entry in enumerate(changed_entries_list) if entry.status == 200)
    changed_entries_list[changed_index] = NativeCacheObject(
        path=changed_entries_list[changed_index].path,
        status=200,
        sha256=sha256_bytes(b"changed native object"),
        size_bytes=21,
    )
    changed_entries = tuple(changed_entries_list)
    changed_inventory = canonical_sha256(
        {
            "entries": changed_entries,
            "schema_version": "cardrag.native-cache-control-inventory.v1",
        }
    )
    changed_after = NativeCacheSnapshot(
        schema_version="cardrag.native-cache-control-snapshot.v1",
        source_commit=SOURCE_COMMIT,
        phase="after",
        namespace="v1/ocr-cache/native",
        entries=changed_entries,
        inventory_sha256=changed_inventory,
    )
    _rewrite_bound_file(bundle, "native_cache_after", changed_after.canonical_bytes())

    with pytest.raises(CandidateAcceptanceError, match="^native_cache_snapshot_changed$"):
        _verify(bundle)


def test_native_control_snapshot_rejects_arbitrary_or_incomplete_paths(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-control path"):
        NativeCacheObject(
            path="v1/ocr-cache/native/aa/present.json",
            status=200,
            sha256=sha256_bytes(b"arbitrary"),
            size_bytes=9,
        )

    bundle = _write_valid_bundle(tmp_path)
    before = bundle.models["native_cache_before"]
    assert isinstance(before, NativeCacheSnapshot)
    incomplete_entries = before.entries[:-1]
    payload = before.model_dump(mode="json")
    payload["entries"] = [entry.model_dump(mode="json") for entry in incomplete_entries]
    payload["inventory_sha256"] = canonical_sha256(
        {
            "entries": incomplete_entries,
            "schema_version": "cardrag.native-cache-control-inventory.v1",
        }
    )
    _rewrite_bound_file(bundle, "native_cache_before", canonical_json_bytes(payload) + b"\n")

    with pytest.raises(CandidateAcceptanceError, match="^json_schema_invalid$"):
        _verify(bundle)


def test_generation_cas_rejects_a_substituted_document_object(tmp_path: Path) -> None:
    bundle = _write_valid_bundle(tmp_path)
    generation_cas = bundle.models["generation_cas"]
    assert isinstance(generation_cas, GenerationCASEvidence)
    substituted = ArtifactRef.for_cas(
        sha256=sha256_bytes(b"substituted generation object"),
        size_bytes=29,
        media_type="application/pdf",
    )
    objects = tuple(sorted((*generation_cas.objects[:-1], substituted), key=lambda item: item.path))
    payload = generation_cas.model_dump(mode="python")
    payload["objects"] = objects
    mismatched = GenerationCASEvidence.model_validate(payload)
    _rewrite_bound_file(bundle, "generation_cas", mismatched.canonical_bytes())

    with pytest.raises(CandidateAcceptanceError, match="^generation_cas_object_binding_mismatch$"):
        _verify(bundle)


def test_generation_cas_rejects_an_underreported_http_write_ledger(tmp_path: Path) -> None:
    bundle = _write_valid_bundle(tmp_path)
    generation_cas = bundle.models["generation_cas"]
    assert isinstance(generation_cas, GenerationCASEvidence)
    payload = generation_cas.model_dump(mode="python")
    minimum_http_writes = (
        generation_cas.object_create_writes
        + generation_cas.database_puts
        + generation_cas.vector_puts
        + generation_cas.manifest_puts
        + generation_cas.ready_puts
        + generation_cas.pointer_cas_attempts
    )
    payload["total_generation_write_requests"] = minimum_http_writes - 1

    with pytest.raises(ValidationError, match="below proven write requests"):
        GenerationCASEvidence.model_validate(payload)


def test_rollback_ledger_rejects_one_generation_disguised_as_v4_and_v5(tmp_path: Path) -> None:
    bundle = _write_valid_bundle(tmp_path)
    rollback = bundle.models["rollback_ledger"]
    assert isinstance(rollback, RollbackLedgerEvidence)
    payload = rollback.model_dump(mode="python")
    v4_generation_id = rollback.steps[0].generation_id
    payload["steps"] = tuple(
        {**step.model_dump(mode="python"), "generation_id": v4_generation_id} for step in rollback.steps
    )

    with pytest.raises(ValidationError, match="v4 and v5 generations are not distinct"):
        RollbackLedgerEvidence.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    (("embedding_provider_id", "nebius"), ("embedding_maximum_tokens", 32_768)),
)
def test_acceptance_rejects_effective_embedding_profile_drift(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    bundle = _write_valid_bundle(tmp_path)
    config = bundle.models["effective_config"]
    payload = config.model_dump(mode="python")
    payload[field] = value
    mismatched = EffectiveConfigEvidence.model_validate(payload)
    _rewrite_bound_file(bundle, "effective_config", mismatched.canonical_bytes())

    with pytest.raises(CandidateAcceptanceError, match="^effective_config_binding_mismatch$"):
        _verify(bundle)


def test_acceptance_rejects_evidence_from_another_source_commit(tmp_path: Path) -> None:
    bundle = _write_valid_bundle(tmp_path)
    worker = bundle.models["worker_metrics"]
    payload = worker.model_dump(mode="python")
    payload["source_commit"] = OTHER_SOURCE_COMMIT
    mismatched = WorkerMetricsEvidence.model_validate(payload)
    _rewrite_bound_file(bundle, "worker_metrics", mismatched.canonical_bytes())

    with pytest.raises(CandidateAcceptanceError, match="^evidence_source_commit_mismatch$"):
        _verify(bundle)
