"""Fail-closed verifier for the versioned release-readiness receipt.

The release-readiness receipt is the operational sibling of the candidate
acceptance receipt for releases whose publish gate is functional verification
rather than research evaluation. It binds exactly the same twelve evidence
files as ``cardrag_core.candidate_acceptance`` except that the effective
configuration carries no document-aggregation-profile or retrieval-policy
binding: those identities are research outputs and are intentionally absent
here because the deployed operational runtime runs profile-less.

Every other cross-contract invariant (source commit, OCI image identity,
generation manifest/READY/pointer, worker terminal metrics, twelve MCP tool
calls, native cache immutability, generation CAS ledger, rollback ledger, and
baseline identity) is enforced exactly as in the acceptance verifier. This
module never manufactures evidence; a missing or failing measurement is a
validation error, never a hand-written flag.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath
from typing import Annotated, Final, Literal, Self, cast

from pydantic import StringConstraints, model_validator

from .candidate_acceptance import (
    _IMAGE_REPOSITORY_RE,
    _MAX_RECEIPT_BYTES,
    CANDIDATE_ISSUERS,
    BaselineIdentityEvidence,
    CandidateAcceptanceError,
    CandidateEvidenceBindings,
    CandidateImageIdentity,
    EmbeddingMaximumTokens,
    GenerationCASEvidence,
    MCPSmokeEvidence,
    NativeCacheAuditEvidence,
    NativeCacheSnapshot,
    RollbackLedgerEvidence,
    SourceCommit,
    ToolSmokeResult,
    WorkerMetricsEvidence,
    _CanonicalModel,
    _load_bound_model,
    _open_evidence_root,
    _parse_canonical_model,
    _read_absolute_file,
    _runtime_image_identity_matches,
)
from .canonical import canonical_sha256
from .embedding import Qwen3EmbeddingProviderId
from .manifests import GenerationManifest, GenerationPointer, GenerationReady

RECEIPT_SCHEMA: Final = "cardrag.release-readiness-receipt.v1"
VALIDATION_SCHEMA: Final = "cardrag.release-readiness-validation.v1"
_SHA256_PATTERN: Final = r"^[0-9a-f]{64}$"


class ReleaseReadinessEffectiveConfig(_CanonicalModel):
    schema_version: Literal["cardrag.release-readiness-effective-config.v1"]
    source_commit: SourceCommit
    release_version: Literal[
        "1.0.22",
        "1.0.23",
        "1.0.24",
        "1.0.25",
        "1.0.26",
        "1.0.27",
        "1.0.28",
        "1.0.29",
        "1.0.30",
        "1.0.31",
        "1.0.32",
        "1.0.33",
    ]
    compose_project: Literal["cardrag-v122-candidate"]
    channel: Literal["candidate-v1.0.11"]
    worker_volume: Literal["cardrag-worker-v122-candidate-state"]
    worker_state_mount_path: Literal["/var/lib/cardrag-worker"]
    worker_codex_home_volume: Literal["cardrag-worker-v122-candidate-codex-home"]
    worker_codex_home_mount_path: Literal["/var/lib/cardrag-codex-home"]
    worker_codex_auth_root: Literal["/var/lib/cardrag-codex-home"]
    worker_home: Literal["/var/lib/cardrag-codex-home/home"]
    mcp_volume: Literal["cardrag-mcp-v122-candidate-state"]
    mcp_host: Literal["127.0.0.1"]
    mcp_port: Literal[18022]
    rootfs_read_only: Literal[True]
    cap_drop_all: Literal[True]
    no_new_privileges: Literal[True]
    worker_seccomp_unconfined: Literal[True]
    worker_apparmor_unconfined: Literal[True]
    worker_systempaths_unconfined: Literal[False]
    worker_privileged: Literal[False]
    worker_cap_add_count: Literal[0]
    baseline_volume_rw_mounts: Literal[0]
    worker_max_state_bytes: Literal[137438953472]
    worker_reserved_free_space_bytes: Literal[2147483648]
    worker_max_vector_sidecar_bytes: Literal[17179869184]
    worker_max_serving_database_bytes: Literal[34359738368]
    worker_minimum_start_free_bytes: Literal[34359738368]
    mcp_max_vector_bytes: Literal[1073741824]
    mcp_max_resident_vector_bytes: Literal[1073741824]
    mcp_max_vector_sidecar_bytes: Literal[17179869184]
    mcp_max_serving_database_bytes: Literal[34359738368]
    mcp_max_generation_download_bytes: Literal[68719476736]
    mcp_max_state_bytes: Literal[137438953472]
    mcp_reserved_free_space_bytes: Literal[2147483648]
    mcp_exhaustive_audit_max_jobs: Literal[32]
    mcp_exhaustive_audit_max_total_bytes: Literal[2147483648]
    mcp_exhaustive_audit_max_artifact_bytes: Literal[268435456]
    mcp_reranker_audit_max_jobs: Literal[1024]
    mcp_reranker_audit_max_total_bytes: Literal[536870912]
    mcp_reranker_audit_max_artifact_bytes: Literal[8388608]
    issuers: tuple[str, ...]
    worker_image: CandidateImageIdentity
    mcp_image: CandidateImageIdentity
    candidate_webdav_namespace_sha256: Annotated[str, StringConstraints(pattern=_SHA256_PATTERN)]
    stable_channel_used: Literal[False]
    stable_publication_approved: Literal[False]
    baseline_seed_access: Literal["read-only"]
    ocr_cache_mode: Literal["read-only"]
    ocr_cache_publication_approved: Literal[False]
    remote_gc_approved: Literal[False]
    collect_remote_garbage: Literal[False]
    experimental_map_reduce_enabled: Literal[False]
    embedding_model: Literal["qwen/qwen3-embedding-8b"]
    embedding_dimension: Literal[4096]
    embedding_dtype: Literal["float32"]
    embedding_normalization: Literal["l2"]
    embedding_provider_id: Qwen3EmbeddingProviderId
    embedding_maximum_tokens: EmbeddingMaximumTokens
    embedding_request_max_attempts: Literal[12]
    embedding_retry_base_seconds: Literal[1]
    embedding_retry_cap_seconds: Literal[60]
    retrieval_mode: Literal["exact-all-active-rows.v1"]
    candidate_prefilter: Literal["none"]
    approximate: Literal[False]

    @model_validator(mode="after")
    def exact_candidate_contract(self) -> Self:
        if self.issuers != CANDIDATE_ISSUERS:
            raise ValueError("candidate config must contain exactly the eight canonical issuers")
        if (self.worker_image.role, self.mcp_image.role) != ("worker", "mcp"):
            raise ValueError("candidate images do not match their roles")
        if self.worker_image.revision != self.source_commit or self.mcp_image.revision != self.source_commit:
            raise ValueError("candidate images do not bind the candidate source commit")
        if any(
            worker_digest == mcp_digest
            for worker_digest, mcp_digest in (
                (self.worker_image.digest, self.mcp_image.digest),
                (
                    self.worker_image.platform_manifest_digest,
                    self.mcp_image.platform_manifest_digest,
                ),
                (
                    self.worker_image.attestation_manifest_digest,
                    self.mcp_image.attestation_manifest_digest,
                ),
                (
                    self.worker_image.platform_config_digest,
                    self.mcp_image.platform_config_digest,
                ),
            )
        ):
            raise ValueError("candidate Worker and MCP image identities must be distinct")
        return self


class ReleaseReadinessReceipt(_CanonicalModel):
    schema_version: Literal["cardrag.release-readiness-receipt.v1"]
    release_version: Literal["1.0.32", "1.0.33"]
    source_commit: SourceCommit
    compose_project: Literal["cardrag-v122-candidate"]
    channel: Literal["candidate-v1.0.11"]
    generation_id: str
    issuers: tuple[str, ...]
    release_eligible: Literal[True]
    evidence: CandidateEvidenceBindings

    @model_validator(mode="after")
    def exact_issuers_are_present(self) -> Self:
        if self.issuers != CANDIDATE_ISSUERS:
            raise ValueError("release readiness receipt must contain exactly eight canonical issuers")
        return self


class ReleaseReadinessValidation(_CanonicalModel):
    schema_version: Literal["cardrag.release-readiness-validation.v1"]
    status: Literal["validated"]
    receipt_sha256: Annotated[str, StringConstraints(pattern=_SHA256_PATTERN)]
    source_commit: SourceCommit
    generation_id: str
    worker_image: CandidateImageIdentity
    mcp_image: CandidateImageIdentity


def verify_release_readiness(
    receipt_path: Path,
    evidence_root: Path,
    *,
    expected_receipt_sha256: str,
    expected_source_commit: str,
    expected_image_repository: str,
    mcp_response_validator: Callable[[ToolSmokeResult], None] | None = None,
) -> ReleaseReadinessValidation:
    """Validate a canonical release-readiness receipt and bound evidence read-only.

    Every invariant of ``candidate_acceptance.verify_candidate_acceptance`` is
    enforced except the document-aggregation-profile binding, which is a
    research output and is not part of this operational gate.
    """

    if len(expected_receipt_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in expected_receipt_sha256
    ):
        raise CandidateAcceptanceError("expected_receipt_sha256_invalid")
    if len(expected_source_commit) != 40 or any(
        character not in "0123456789abcdef" for character in expected_source_commit
    ):
        raise CandidateAcceptanceError("expected_source_commit_invalid")
    if _IMAGE_REPOSITORY_RE.fullmatch(expected_image_repository) is None:
        raise CandidateAcceptanceError("expected_image_repository_invalid")

    receipt_raw = _read_absolute_file(receipt_path, maximum_size=_MAX_RECEIPT_BYTES)
    receipt_sha256 = hashlib.sha256(receipt_raw).hexdigest()
    if receipt_sha256 != expected_receipt_sha256:
        raise CandidateAcceptanceError("receipt_sha256_mismatch")
    receipt = _parse_canonical_model(receipt_raw, ReleaseReadinessReceipt, trailing_lf=True)
    if receipt.source_commit != expected_source_commit:
        raise CandidateAcceptanceError("receipt_source_commit_mismatch")

    root_descriptor = _open_evidence_root(evidence_root)
    try:
        bindings = receipt.evidence
        config = _load_bound_model(
            root_descriptor, bindings.effective_config, ReleaseReadinessEffectiveConfig
        )
        manifest = _load_bound_model(
            root_descriptor, bindings.generation_manifest, GenerationManifest, trailing_lf=False
        )
        ready = _load_bound_model(
            root_descriptor, bindings.generation_ready, GenerationReady, trailing_lf=False
        )
        pointer = _load_bound_model(
            root_descriptor, bindings.candidate_pointer, GenerationPointer, trailing_lf=False
        )
        worker = _load_bound_model(root_descriptor, bindings.worker_metrics, WorkerMetricsEvidence)
        mcp = _load_bound_model(root_descriptor, bindings.mcp_smoke, MCPSmokeEvidence)
        before = _load_bound_model(root_descriptor, bindings.native_cache_before, NativeCacheSnapshot)
        after = _load_bound_model(root_descriptor, bindings.native_cache_after, NativeCacheSnapshot)
        native_audit = _load_bound_model(
            root_descriptor, bindings.native_cache_audit, NativeCacheAuditEvidence
        )
        generation_cas = _load_bound_model(root_descriptor, bindings.generation_cas, GenerationCASEvidence)
        rollback = _load_bound_model(root_descriptor, bindings.rollback_ledger, RollbackLedgerEvidence)
        baseline = _load_bound_model(root_descriptor, bindings.baseline_identity, BaselineIdentityEvidence)
    finally:
        os.close(root_descriptor)

    evidence_sources = (
        config.source_commit,
        worker.source_commit,
        mcp.source_commit,
        before.source_commit,
        after.source_commit,
        native_audit.source_commit,
        generation_cas.source_commit,
        rollback.source_commit,
        baseline.source_commit,
    )
    if any(source != receipt.source_commit for source in evidence_sources):
        raise CandidateAcceptanceError("evidence_source_commit_mismatch")
    if (
        manifest.schema_version != "cardrag.generation.v6"
        or manifest.serving_schema != "cardrag.serving-db.v6"
    ):
        raise CandidateAcceptanceError("generation_schema_mismatch")
    if manifest.generation_id != receipt.generation_id or manifest.issuer_codes != CANDIDATE_ISSUERS:
        raise CandidateAcceptanceError("generation_identity_mismatch")
    if manifest.counts.documents < 1 or manifest.counts.chunks < 1:
        raise CandidateAcceptanceError("generation_empty")
    if manifest.structure_contract is None or (
        manifest.structure_contract.source_coverage.source_non_whitespace_characters < 1
    ):
        raise CandidateAcceptanceError("generation_structure_empty")
    if manifest.vector_sidecar is None:
        raise CandidateAcceptanceError("generation_vector_sidecar_missing")
    primary_embedding_profile = next(
        (
            profile
            for profile in manifest.embedding_profiles
            if profile.profile_id == manifest.primary_embedding_profile_id
        ),
        None,
    )
    if primary_embedding_profile is None:
        raise CandidateAcceptanceError("generation_primary_embedding_profile_missing")
    if manifest.manifest_sha256 != bindings.generation_manifest.sha256:
        raise CandidateAcceptanceError("generation_manifest_binding_mismatch")
    ready_sha256 = canonical_sha256(ready)
    pointer_sha256 = canonical_sha256(pointer)
    if (
        ready.generation_id != manifest.generation_id
        or ready.manifest_sha256 != manifest.manifest_sha256
        or ready.serving_database_sha256 != manifest.serving_database.sha256
        or ready.serving_database_size_bytes != manifest.serving_database.size_bytes
        or ready.vector_sidecar_sha256 != manifest.vector_sidecar.artifact.sha256
        or ready.vector_sidecar_size_bytes != manifest.vector_sidecar.artifact.size_bytes
        or ready_sha256 != bindings.generation_ready.sha256
    ):
        raise CandidateAcceptanceError("generation_ready_binding_mismatch")
    if (
        pointer.generation_id != manifest.generation_id
        or pointer.manifest_sha256 != manifest.manifest_sha256
        or pointer.ready_sha256 != ready_sha256
        or pointer_sha256 != bindings.candidate_pointer.sha256
    ):
        raise CandidateAcceptanceError("generation_pointer_binding_mismatch")

    if (
        config.source_commit != receipt.source_commit
        or config.issuers != receipt.issuers
        or config.embedding_model != primary_embedding_profile.model
        or config.embedding_dimension != primary_embedding_profile.dimension
        or config.embedding_dtype != primary_embedding_profile.dtype
        or config.embedding_normalization != primary_embedding_profile.normalization
        or config.embedding_provider_id != primary_embedding_profile.provider_id
        or config.embedding_maximum_tokens != primary_embedding_profile.maximum_tokens
    ):
        raise CandidateAcceptanceError("effective_config_binding_mismatch")
    if (
        config.worker_image.repository != expected_image_repository
        or config.mcp_image.repository != expected_image_repository
    ):
        raise CandidateAcceptanceError("image_repository_mismatch")
    if config.release_version != receipt.release_version:
        raise CandidateAcceptanceError("effective_config_version_mismatch")

    expected_issuer_metrics = tuple(
        (row.issuer, row.acquired, row.succeeded, row.failed) for row in manifest.issuer_ocr_counts
    )
    effective_config_sha256 = canonical_sha256(config)
    observed_issuer_metrics = tuple(
        (row.issuer, row.acquired, row.succeeded, row.failed) for row in worker.issuer_metrics
    )
    if (
        worker.generation_id != manifest.generation_id
        or worker.generation_manifest_sha256 != manifest.manifest_sha256
        or worker.effective_config_sha256 != effective_config_sha256
        or not _runtime_image_identity_matches(worker, config.worker_image)
        or worker.worker_state_mount_path != config.worker_state_mount_path
        or worker.codex_home_mount_path != config.worker_codex_home_mount_path
        or worker.codex_auth_root != config.worker_codex_auth_root
        or worker.codex_home != config.worker_codex_home_mount_path
        or worker.home != config.worker_home
        or worker.documents != manifest.counts.documents
        or worker.terminal_result.get("corpus_sha256") != manifest.corpus_sha256
        or worker.terminal_result.get("contract_sha256") != manifest.contract_sha256
        or worker.chunks != manifest.counts.chunks
        or worker.embedding_rows != manifest.vector_sidecar.row_count
        or worker.vector_sidecar_size_bytes != manifest.vector_sidecar.artifact.size_bytes
        or observed_issuer_metrics != expected_issuer_metrics
        or worker.structure_source_non_whitespace_characters
        != manifest.structure_contract.source_coverage.source_non_whitespace_characters
        or worker.structure_covered_non_whitespace_characters
        != manifest.structure_contract.source_coverage.covered_non_whitespace_characters
    ):
        raise CandidateAcceptanceError("worker_metrics_binding_mismatch")
    if (
        mcp.generation_id != manifest.generation_id
        or mcp.generation_manifest_sha256 != manifest.manifest_sha256
        or mcp.effective_config_sha256 != effective_config_sha256
        or not _runtime_image_identity_matches(mcp, config.mcp_image)
        or mcp.expected_active_contracts
        != (
            manifest.structure_contract.revision_counts.current
            + manifest.structure_contract.revision_counts.ambiguous
        )
        or mcp.expected_embedding_rows != manifest.vector_sidecar.row_count
    ):
        raise CandidateAcceptanceError("mcp_smoke_binding_mismatch")
    if mcp_response_validator is not None:
        for result in mcp.tool_results:
            mcp_response_validator(result)

    if before.phase != "before" or after.phase != "after" or before.entries != after.entries:
        raise CandidateAcceptanceError("native_cache_snapshot_changed")
    native_pair_statuses = {PurePosixPath(entry.path).parts[4]: entry.status for entry in before.entries}
    native_hit_pairs = sum(status == 200 for status in native_pair_statuses.values())
    native_miss_pairs = sum(status == 404 for status in native_pair_statuses.values())
    if (
        before.inventory_sha256 != after.inventory_sha256
        or native_audit.before_inventory_sha256 != before.inventory_sha256
        or native_audit.after_inventory_sha256 != after.inventory_sha256
        or native_audit.generation_id != manifest.generation_id
        or native_audit.cache_hit_count != native_hit_pairs
        or native_audit.cache_miss_count != native_miss_pairs
        or native_audit.native_get_requests != len(before.entries) + len(after.entries)
    ):
        raise CandidateAcceptanceError("native_cache_audit_binding_mismatch")

    if (
        generation_cas.generation_id != manifest.generation_id
        or generation_cas.manifest_sha256 != manifest.manifest_sha256
        or generation_cas.ready_sha256 != ready_sha256
        or generation_cas.pointer_sha256 != pointer_sha256
        or generation_cas.serving_database != manifest.serving_database
        or generation_cas.vector_sidecar != manifest.vector_sidecar.artifact
        or generation_cas.logical_publication_calls != worker.generation_publication_calls
    ):
        raise CandidateAcceptanceError("generation_cas_binding_mismatch")
    expected_objects_by_path = {
        artifact.path: artifact
        for document in manifest.documents
        for artifact in (document.pdf, document.ocr)
        if artifact is not None
    }
    expected_objects = tuple(expected_objects_by_path[path] for path in sorted(expected_objects_by_path))
    if generation_cas.objects != expected_objects:
        raise CandidateAcceptanceError("generation_cas_object_binding_mismatch")
    if (
        rollback.steps[1].generation_id != manifest.generation_id
        or rollback.steps[2].generation_id != manifest.generation_id
        or rollback.steps[4].generation_id != manifest.generation_id
    ):
        raise CandidateAcceptanceError("rollback_generation_mismatch")

    return ReleaseReadinessValidation(
        schema_version=VALIDATION_SCHEMA,
        status="validated",
        receipt_sha256=receipt_sha256,
        source_commit=receipt.source_commit,
        generation_id=receipt.generation_id,
        worker_image=config.worker_image,
        mcp_image=config.mcp_image,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--expected-receipt-sha256", required=True)
    parser.add_argument("--expected-source-commit", required=True)
    parser.add_argument("--expected-image-repository", required=True)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    mcp_response_validator: Callable[[ToolSmokeResult], None] | None = None,
) -> int:
    arguments = _parser().parse_args(argv)
    try:
        validation = verify_release_readiness(
            cast(Path, arguments.receipt),
            cast(Path, arguments.evidence_root),
            expected_receipt_sha256=cast(str, arguments.expected_receipt_sha256),
            expected_source_commit=cast(str, arguments.expected_source_commit),
            expected_image_repository=cast(str, arguments.expected_image_repository),
            mcp_response_validator=mcp_response_validator,
        )
    except CandidateAcceptanceError:
        print("release readiness validation failed", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(validation.canonical_bytes())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
