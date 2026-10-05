"""End-to-end behavior tests for the gold-free release-readiness verifier."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import cast

import pytest
from pydantic import BaseModel, ValidationError
from test_candidate_acceptance import (
    IMAGE_REPOSITORY,
    ROWS,
    SOURCE_COMMIT,
    _file_binding,
    _manifest,
    _smoke_call,
)

from cardrag_core.candidate_acceptance import (
    BASELINE_IDENTITY_ASSETS,
    CANDIDATE_ISSUERS,
    MCP_TOOLS,
    BaselineAssetIdentity,
    BaselineIdentityEvidence,
    CandidateAcceptanceError,
    CandidateEvidenceBindings,
    CandidateImageIdentity,
    EvidenceFile,
    GenerationCASEvidence,
    IssuerRunMetrics,
    MCPSmokeEvidence,
    NativeCacheAuditEvidence,
    NativeCacheObject,
    NativeCacheSnapshot,
    RollbackLedgerEvidence,
    RollbackStep,
    WorkerMetricsEvidence,
)
from cardrag_core.canonical import canonical_json_bytes, canonical_sha256, sha256_bytes
from cardrag_core.manifests import GenerationManifest, GenerationPointer, GenerationReady
from cardrag_core.release_readiness import (
    RECEIPT_SCHEMA,
    ReleaseReadinessEffectiveConfig,
    ReleaseReadinessReceipt,
    main,
    verify_release_readiness,
)

RECEIPT_NAME = "release-readiness-receipt.json"
_NO_LF_FIELDS = frozenset({"generation_manifest", "generation_ready", "candidate_pointer"})


def _candidate_v6_manifest() -> GenerationManifest:
    return cast(
        GenerationManifest,
        _manifest().model_copy(
            update={
                "schema_version": "cardrag.generation.v6",
                "serving_schema": "cardrag.serving-db.v6",
                "document_aggregation_profile": None,
                "document_aggregation_policy": None,
                "sealed_profile_sha256": None,
                "exact_row_corpus_sha256": None,
            }
        ),
    )


def _image_identity(role: str, digests: tuple[str, str, str, str]) -> CandidateImageIdentity:
    digest, platform_manifest, platform_config, attestation = digests
    return CandidateImageIdentity(
        role=role,  # type: ignore[arg-type]
        repository=IMAGE_REPOSITORY,
        digest=f"sha256:{digest}",
        compose_image_reference=f"{IMAGE_REPOSITORY}@sha256:{digest}",
        index_media_type="application/vnd.oci.image.index.v1+json",
        index_manifest_count=2,
        platform_manifest_digest=f"sha256:{platform_manifest}",
        platform_config_digest=f"sha256:{platform_config}",
        platform_manifest_media_type="application/vnd.oci.image.manifest.v1+json",
        platform_os="linux",
        platform_architecture="amd64",
        attestation_manifest_digest=f"sha256:{attestation}",
        attestation_manifest_media_type="application/vnd.oci.image.manifest.v1+json",
        attestation_os="unknown",
        attestation_architecture="unknown",
        attestation_reference_type="attestation-manifest",
        attestation_subject_digest=f"sha256:{platform_manifest}",
        revision=SOURCE_COMMIT,
        version="1.0.32",
        platform="linux/amd64",
        entrypoint=f"cardrag-{role}",
        user="10001:10001",
    )


def _config(manifest: GenerationManifest) -> ReleaseReadinessEffectiveConfig:
    primary = next(
        profile
        for profile in manifest.embedding_profiles
        if profile.profile_id == manifest.primary_embedding_profile_id
    )
    return ReleaseReadinessEffectiveConfig(
        schema_version="cardrag.release-readiness-effective-config.v1",
        source_commit=SOURCE_COMMIT,
        release_version="1.0.32",
        compose_project="cardrag-v122-candidate",
        channel="candidate-v1.0.11",
        worker_volume="cardrag-worker-v122-candidate-state",
        worker_state_mount_path="/var/lib/cardrag-worker",
        worker_codex_home_volume="cardrag-worker-v122-candidate-codex-home",
        worker_codex_home_mount_path="/var/lib/cardrag-codex-home",
        worker_codex_auth_root="/var/lib/cardrag-codex-home",
        worker_home="/var/lib/cardrag-codex-home/home",
        mcp_volume="cardrag-mcp-v122-candidate-state",
        mcp_host="127.0.0.1",
        mcp_port=18022,
        rootfs_read_only=True,
        cap_drop_all=True,
        no_new_privileges=True,
        worker_seccomp_unconfined=True,
        worker_apparmor_unconfined=True,
        worker_systempaths_unconfined=False,
        worker_privileged=False,
        worker_cap_add_count=0,
        baseline_volume_rw_mounts=0,
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
        worker_image=_image_identity("worker", ("a" * 64, "c" * 64, "1" * 64, "d" * 64)),
        mcp_image=_image_identity("mcp", ("b" * 64, "e" * 64, "2" * 64, "f" * 64)),
        candidate_webdav_namespace_sha256=sha256_bytes(b"candidate namespace"),
        stable_channel_used=False,
        stable_publication_approved=False,
        baseline_seed_access="read-only",
        ocr_cache_mode="read-only",
        ocr_cache_publication_approved=False,
        remote_gc_approved=False,
        collect_remote_garbage=False,
        experimental_map_reduce_enabled=False,
        embedding_model="qwen/qwen3-embedding-8b",
        embedding_dimension=4096,
        embedding_dtype="float32",
        embedding_normalization="l2",
        embedding_provider_id=primary.provider_id,
        embedding_maximum_tokens=primary.maximum_tokens,
        embedding_request_max_attempts=12,
        embedding_retry_base_seconds=1,
        embedding_retry_cap_seconds=60,
        retrieval_mode="exact-all-active-rows.v1",
        candidate_prefilter="none",
        approximate=False,
    )


def _write_readiness_bundle(root: Path) -> dict[str, BaseModel]:
    manifest = _candidate_v6_manifest()
    ready = GenerationReady(
        generation_id=manifest.generation_id,
        manifest_sha256=manifest.manifest_sha256,
        serving_database_sha256=manifest.serving_database.sha256,
        serving_database_size_bytes=manifest.serving_database.size_bytes,
        vector_sidecar_sha256=manifest.vector_sidecar.artifact.sha256,
        vector_sidecar_size_bytes=manifest.vector_sidecar.artifact.size_bytes,
    )
    pointer = GenerationPointer(
        generation_id=manifest.generation_id,
        manifest_sha256=manifest.manifest_sha256,
        ready_sha256=canonical_sha256(ready),
    )
    config = _config(manifest)
    generation_objects_by_path = {
        artifact.path: artifact
        for document in manifest.documents
        for artifact in (document.pdf, document.ocr)
        if artifact is not None
    }
    generation_objects = tuple(
        generation_objects_by_path[path] for path in sorted(generation_objects_by_path)
    )
    publication_calls = len(generation_objects) + 5
    terminal_result = {
        "run_id": "readiness-run",
        "status": "succeeded",
        "generation_id": manifest.generation_id,
        "documents": manifest.counts.documents,
        "corpus_sha256": manifest.corpus_sha256,
        "contract_sha256": manifest.contract_sha256,
    }
    worker = WorkerMetricsEvidence(
        schema_version="cardrag.candidate-worker-metrics.v4",
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
        terminal_exit_code=0,
        terminal_result=terminal_result,
        terminal_result_sha256=canonical_sha256(terminal_result),
        issuer_metrics=tuple(
            IssuerRunMetrics(
                issuer=row.issuer, acquired=row.acquired, succeeded=row.succeeded, failed=row.failed
            )
            for row in manifest.issuer_ocr_counts
        ),
        documents=manifest.counts.documents,
        chunks=manifest.counts.chunks,
        embedding_rows=manifest.vector_sidecar.row_count,
        vector_sidecar_size_bytes=manifest.vector_sidecar.artifact.size_bytes,
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
        generation_publication_calls=publication_calls,
    )
    mcp = MCPSmokeEvidence(
        schema_version="cardrag.candidate-mcp-smoke.v3",
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
        serving_schema="cardrag.serving-db.v6",
        embedding_dimension=4096,
        retrieval_mode="exact",
        approximate=False,
        expected_active_contracts=len(CANDIDATE_ISSUERS),
        scored_contracts=len(CANDIDATE_ISSUERS),
        expected_embedding_rows=ROWS,
        scored_embedding_rows=ROWS,
        exact_blocks=2,
        cross_contract_node_count=0,
        discovered_tools=MCP_TOOLS,
        tool_results=tuple(_smoke_call(tool, manifest.generation_id) for tool in MCP_TOOLS),
        bundle_source_spans_verified=True,
        revision_history_verified=True,
        legacy_adapter_verified=True,
        pdf_range_status=206,
        pdf_magic_prefix="%PDF-",
        pdf_content_range_verified=True,
    )
    hit = sha256_bytes(b"native cache hit reuse key")
    miss = sha256_bytes(b"native cache miss reuse key")
    entries = tuple(
        sorted(
            (
                NativeCacheObject(
                    path=f"v1/ocr-cache/native/{hit[:2]}/{hit}/manifest.json",
                    status=200,
                    sha256=sha256_bytes(b"native hit manifest"),
                    size_bytes=len(b"native hit manifest"),
                ),
                NativeCacheObject(
                    path=f"v1/ocr-cache/native/{hit[:2]}/{hit}/READY.json",
                    status=200,
                    sha256=sha256_bytes(b"native hit ready"),
                    size_bytes=len(b"native hit ready"),
                ),
                NativeCacheObject(path=f"v1/ocr-cache/native/{miss[:2]}/{miss}/manifest.json", status=404),
                NativeCacheObject(path=f"v1/ocr-cache/native/{miss[:2]}/{miss}/READY.json", status=404),
            ),
            key=lambda entry: entry.path,
        )
    )
    inventory_sha256 = canonical_sha256(
        {"entries": entries, "schema_version": "cardrag.native-cache-control-inventory.v1"}
    )
    before = NativeCacheSnapshot(
        schema_version="cardrag.native-cache-control-snapshot.v1",
        source_commit=SOURCE_COMMIT,
        phase="before",
        namespace="v1/ocr-cache/native",
        entries=entries,
        inventory_sha256=inventory_sha256,
    )
    after = NativeCacheSnapshot(
        schema_version="cardrag.native-cache-control-snapshot.v1",
        source_commit=SOURCE_COMMIT,
        phase="after",
        namespace="v1/ocr-cache/native",
        entries=entries,
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
        vector_sidecar=manifest.vector_sidecar.artifact,
        objects=generation_objects,
        object_publish_calls=len(generation_objects),
        object_create_writes=0,
        database_puts=1,
        vector_puts=1,
        manifest_puts=1,
        ready_puts=1,
        pointer_cas_attempts=1,
        pointer_cas_successes=1,
        logical_publication_calls=publication_calls,
        total_generation_write_requests=17,
        native_cache_write_requests=0,
        stable_channel_write_requests=0,
    )

    def step(ordinal: int, action: str, schema: str, generation_id: str, note: bytes) -> RollbackStep:
        exact = schema != "cardrag.serving-db.v4"
        return RollbackStep(
            ordinal=ordinal,
            action=action,  # type: ignore[arg-type]
            serving_schema=schema,  # type: ignore[arg-type]
            generation_id=generation_id,
            runtime_instance_sha256=sha256_bytes(note),
            health_ready=True,
            tool_discovery_passed=True,
            search_mode="exact" if exact else "legacy-hybrid",  # type: ignore[arg-type]
            search_contracts_outcome="exact-passed" if exact else "unsupported-rejected",  # type: ignore[arg-type]
        )

    candidate = manifest.generation_id
    rollback = RollbackLedgerEvidence(
        schema_version="cardrag.candidate-rollback-ledger.v2",
        source_commit=SOURCE_COMMIT,
        channel="candidate-v1.0.11",
        steps=(
            step(1, "activate", "cardrag.serving-db.v5", "baseline-v5", b"baseline before"),
            step(2, "activate", "cardrag.serving-db.v6", candidate, b"candidate before restart"),
            step(3, "restart", "cardrag.serving-db.v6", candidate, b"candidate after restart"),
            step(4, "activate", "cardrag.serving-db.v5", "baseline-v5", b"baseline restored"),
            step(5, "activate", "cardrag.serving-db.v6", candidate, b"candidate final"),
        ),
        rollback_verified=True,
        stable_channel_write_requests=0,
    )
    baseline = BaselineIdentityEvidence(
        schema_version="cardrag.baseline-before-after-identity.v1",
        source_commit=SOURCE_COMMIT,
        assets=tuple(
            BaselineAssetIdentity(
                asset=asset,
                before_sha256=sha256_bytes(f"baseline {asset}".encode()),
                after_sha256=sha256_bytes(f"baseline {asset}".encode()),
                equal=True,
            )
            for asset in BASELINE_IDENTITY_ASSETS
        ),
        candidate_rw_mounts_of_baseline_volumes=0,
        candidate_stable_channel_requests=0,
        candidate_librechat_switch_requests=0,
        destructive_cleanup_commands=0,
        baseline_restart_commands=0,
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
        "baseline_identity": baseline,
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
        "baseline_identity": "baseline-identity.json",
    }
    root.mkdir(parents=True, exist_ok=True)
    bindings: dict[str, EvidenceFile] = {}
    for field, model in models.items():
        raw = canonical_json_bytes(model) + (b"" if field in _NO_LF_FIELDS else b"\n")
        (root / names[field]).write_bytes(raw)
        bindings[field] = _file_binding(names[field], raw)
    receipt = ReleaseReadinessReceipt(
        schema_version=RECEIPT_SCHEMA,
        release_version="1.0.32",
        source_commit=SOURCE_COMMIT,
        compose_project="cardrag-v122-candidate",
        channel="candidate-v1.0.11",
        generation_id=manifest.generation_id,
        issuers=CANDIDATE_ISSUERS,
        release_eligible=True,
        evidence=CandidateEvidenceBindings(**bindings),
    )
    (root / RECEIPT_NAME).write_bytes(receipt.canonical_bytes())
    models["__receipt__"] = receipt
    return models


def _receipt_sha(root: Path) -> str:
    return hashlib.sha256((root / RECEIPT_NAME).read_bytes()).hexdigest()


def _verify(root: Path, **overrides: object):
    kwargs: dict[str, object] = {
        "expected_receipt_sha256": _receipt_sha(root),
        "expected_source_commit": SOURCE_COMMIT,
        "expected_image_repository": IMAGE_REPOSITORY,
    }
    kwargs.update(overrides)
    return verify_release_readiness(
        root / RECEIPT_NAME,
        root,
        expected_receipt_sha256=cast(str, kwargs["expected_receipt_sha256"]),
        expected_source_commit=cast(str, kwargs["expected_source_commit"]),
        expected_image_repository=cast(str, kwargs["expected_image_repository"]),
    )


def test_readiness_verifier_binds_the_complete_operational_evidence_without_gold(tmp_path: Path) -> None:
    root = tmp_path / "release-evidence-v1.0.32"
    _write_readiness_bundle(root)
    validation = _verify(root)
    assert validation.status == "validated"
    assert validation.receipt_sha256 == _receipt_sha(root)
    assert validation.source_commit == SOURCE_COMMIT
    assert validation.worker_image.version == "1.0.32"
    assert validation.mcp_image.digest == "sha256:" + "b" * 64


def test_readiness_cli_is_canonical_and_fails_without_leaking(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "bundle"
    _write_readiness_bundle(root)
    arguments = [
        "--receipt",
        str(root / RECEIPT_NAME),
        "--evidence-root",
        str(root),
        "--expected-receipt-sha256",
        _receipt_sha(root),
        "--expected-source-commit",
        SOURCE_COMMIT,
        "--expected-image-repository",
        IMAGE_REPOSITORY,
    ]
    assert main(arguments) == 0
    printed = capfd.readouterr()
    assert printed.out.endswith("\n")
    assert '"status":"validated"' in printed.out
    assert printed.err == ""

    broken = [*arguments[:5], "0" * 64, *arguments[6:]]
    assert main(broken) == 1
    failed = capfd.readouterr()
    assert failed.out == ""
    assert failed.err == "release readiness validation failed\n"


def test_readiness_effective_config_rejects_profile_fields(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    models = _write_readiness_bundle(root)
    config = models["effective_config"]
    assert isinstance(config, ReleaseReadinessEffectiveConfig)
    payload = config.model_dump(mode="json")
    payload["document_aggregation_profile_sha256"] = "a" * 64
    with pytest.raises(ValidationError):
        ReleaseReadinessEffectiveConfig.model_validate(payload)


def test_readiness_verify_rejects_source_image_and_binding_divergence(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    _write_readiness_bundle(root)
    with pytest.raises(CandidateAcceptanceError, match="expected_source_commit_invalid"):
        _verify(root, expected_source_commit="z" * 40)
    with pytest.raises(CandidateAcceptanceError, match="receipt_sha256_mismatch"):
        _verify(root, expected_receipt_sha256="f" * 64)
    with pytest.raises(CandidateAcceptanceError, match="image_repository_mismatch"):
        _verify(root, expected_image_repository="ghcr.io/kanu-coffee/other-candidate")
    receipt_path = root / RECEIPT_NAME
    original = receipt_path.read_bytes()
    tampered = original.replace(b'"release_version":"1.0.32"', b'"release_version":"1.0.21"')
    assert tampered != original
    receipt_path.write_bytes(tampered)
    with pytest.raises(CandidateAcceptanceError):
        _verify(root, expected_receipt_sha256=hashlib.sha256(tampered).hexdigest())


def test_readiness_verify_rejects_tampered_evidence_bytes(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    _write_readiness_bundle(root)
    manifest_path = root / "serving-generation-manifest.json"
    original = manifest_path.read_bytes()
    tampered = original.replace(b'"candidate-generation"', b'"candidate-generation2"')
    assert tampered != original
    manifest_path.write_bytes(tampered)
    with pytest.raises(CandidateAcceptanceError):
        _verify(root)


def test_readiness_worker_metrics_reject_failed_terminal_and_failed_issuer() -> None:
    with pytest.raises(ValidationError, match="terminal"):
        WorkerMetricsEvidence.model_validate(
            {
                "schema_version": "cardrag.candidate-worker-metrics.v4",
                "source_commit": SOURCE_COMMIT,
                "generation_id": "candidate-generation",
                "generation_manifest_sha256": "a" * 64,
                "effective_config_sha256": "b" * 64,
            }
        )
    with pytest.raises(ValidationError):
        IssuerRunMetrics(issuer="bc", acquired=2, succeeded=1, failed=1)
