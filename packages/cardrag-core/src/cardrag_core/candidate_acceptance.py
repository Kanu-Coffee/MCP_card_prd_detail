"""Fail-closed verifier for the v1.0.11 real-candidate acceptance receipt.

The receipt is a canonical technical trust root.  It does not manufacture
runtime evidence or imply a separate human approval: it binds exact canonical
evidence files and replays their cross-contract invariants without opening
Docker, WebDAV, or either runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Annotated, Final, Literal, Self, cast

from pydantic import BaseModel, Field, StringConstraints, ValidationError, field_validator, model_validator

from .canonical import canonical_json_bytes, canonical_sha256
from .domain import ArtifactRef, StrictFrozenModel
from .embedding import Qwen3EmbeddingProviderId
from .manifests import GenerationManifest, GenerationPointer, GenerationReady
from .paths import validate_identifier, validate_relative_path

RECEIPT_SCHEMA: Final = "cardrag.candidate-acceptance-receipt.v1"
VALIDATION_SCHEMA: Final = "cardrag.candidate-acceptance-validation.v1"
CANDIDATE_ISSUERS: Final = ("kb", "samsung", "shinhan", "woori")
MCP_TOOLS: Final = (
    "search_contracts",
    "get_contract_bundle",
    "list_product_revisions",
    "search_evidence",
    "get_evidence",
    "get_product",
    "get_source_pdf",
    "get_source_page",
)
V109_IDENTITY_ASSETS: Final = (
    "docker_worker_runtime",
    "docker_mcp_runtime",
    "worker_image",
    "mcp_image",
    "worker_volume",
    "mcp_volume",
    "systemd_worker_units",
    "local_stable_pointer",
    "webdav_stable_channel",
)

Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
SourceCommit = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
ImageDigest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
ImageReference = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9][a-z0-9._/-]{0,254}@sha256:[0-9a-f]{64}$"),
]
PositiveStrictInt = Annotated[int, Field(strict=True, gt=0)]
NonNegativeStrictInt = Annotated[int, Field(strict=True, ge=0)]
EmbeddingMaximumTokens = Annotated[int, Field(strict=True, ge=1, le=32_768)]

_MAX_RECEIPT_BYTES: Final = 2 * 1024 * 1024
_MAX_EVIDENCE_BYTES: Final = 128 * 1024 * 1024
_READ_SIZE: Final = 1024 * 1024
_IMAGE_REPOSITORY_RE: Final = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,254}$")


class CandidateAcceptanceError(RuntimeError):
    """A bounded validation failure which never includes evidence contents."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _CanonicalModel(StrictFrozenModel):
    def canonical_bytes(self, *, trailing_lf: bool = True) -> bytes:
        payload = canonical_json_bytes(self)
        return payload + (b"\n" if trailing_lf else b"")


class EvidenceFile(_CanonicalModel):
    path: str
    sha256: Sha256Hex
    size_bytes: PositiveStrictInt = Field(le=_MAX_EVIDENCE_BYTES)

    @field_validator("path")
    @classmethod
    def path_is_safe(cls, value: str) -> str:
        return validate_relative_path(value).as_posix()


class CandidateImageIdentity(_CanonicalModel):
    role: Literal["worker", "mcp"]
    repository: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._/-]{0,254}$")]
    digest: ImageDigest
    compose_image_reference: ImageReference
    index_media_type: Literal["application/vnd.oci.image.index.v1+json"]
    index_manifest_count: Literal[2]
    platform_manifest_digest: ImageDigest
    platform_config_digest: ImageDigest
    platform_manifest_media_type: Literal["application/vnd.oci.image.manifest.v1+json"]
    platform_os: Literal["linux"]
    platform_architecture: Literal["amd64"]
    attestation_manifest_digest: ImageDigest
    attestation_manifest_media_type: Literal["application/vnd.oci.image.manifest.v1+json"]
    attestation_os: Literal["unknown"]
    attestation_architecture: Literal["unknown"]
    attestation_reference_type: Literal["attestation-manifest"]
    attestation_subject_digest: ImageDigest
    revision: SourceCommit
    version: Literal["1.0.11"]
    platform: Literal["linux/amd64"]
    entrypoint: Literal["cardrag-worker", "cardrag-mcp"]
    user: Literal["10001:10001"]

    @model_validator(mode="after")
    def entrypoint_matches_role(self) -> Self:
        if self.entrypoint != f"cardrag-{self.role}":
            raise ValueError("candidate image entrypoint does not match its role")
        if self.compose_image_reference != f"{self.repository}@{self.digest}":
            raise ValueError("candidate Compose image reference must be the sealed OCI index")
        if (
            len(
                {
                    self.digest,
                    self.platform_manifest_digest,
                    self.platform_config_digest,
                    self.attestation_manifest_digest,
                }
            )
            != 4
        ):
            raise ValueError(
                "candidate image index, platform, config, and attestation digests must be distinct"
            )
        if self.attestation_subject_digest != self.platform_manifest_digest:
            raise ValueError("candidate attestation subject must be the sealed platform manifest")
        return self


class EffectiveConfigEvidence(_CanonicalModel):
    schema_version: Literal["cardrag.candidate-effective-config.v2"]
    source_commit: SourceCommit
    release_version: Literal["1.0.11"]
    compose_project: Literal["cardrag-v111-candidate"]
    channel: Literal["candidate-v1.0.11"]
    worker_volume: Literal["cardrag-worker-v111-candidate-state"]
    worker_state_mount_path: Literal["/var/lib/cardrag-worker"]
    worker_codex_home_volume: Literal["cardrag-worker-v111-candidate-codex-home"]
    worker_codex_home_mount_path: Literal["/var/lib/cardrag-codex-home"]
    worker_codex_auth_root: Literal["/var/lib/cardrag-codex-home"]
    worker_home: Literal["/var/lib/cardrag-codex-home/home"]
    mcp_volume: Literal["cardrag-mcp-v111-candidate-state"]
    mcp_host: Literal["127.0.0.1"]
    mcp_port: Literal[18011]
    rootfs_read_only: Literal[True]
    cap_drop_all: Literal[True]
    no_new_privileges: Literal[True]
    worker_seccomp_unconfined: Literal[True]
    worker_apparmor_unconfined: Literal[True]
    worker_systempaths_unconfined: Literal[False]
    worker_privileged: Literal[False]
    worker_cap_add_count: Literal[0]
    v109_volume_rw_mounts: Literal[0]
    worker_max_state_bytes: Literal[68719476736]
    worker_reserved_free_space_bytes: Literal[2147483648]
    worker_max_vector_sidecar_bytes: Literal[17179869184]
    worker_max_serving_database_bytes: Literal[4294967296]
    worker_minimum_start_free_bytes: Literal[34359738368]
    mcp_max_vector_bytes: Literal[1073741824]
    mcp_max_resident_vector_bytes: Literal[1073741824]
    mcp_max_vector_sidecar_bytes: Literal[17179869184]
    mcp_max_serving_database_bytes: Literal[4294967296]
    mcp_max_generation_download_bytes: Literal[34359738368]
    mcp_max_state_bytes: Literal[68719476736]
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
    candidate_webdav_namespace_sha256: Sha256Hex
    stable_channel_used: Literal[False]
    stable_publication_approved: Literal[False]
    v109_seed_access: Literal["read-only"]
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
    retrieval_mode: Literal["exact-all-active-rows.v1"]
    candidate_prefilter: Literal["none"]
    approximate: Literal[False]
    document_aggregation_profile_sha256: Sha256Hex
    document_aggregation_policy: Literal["max_child", "top3_mean", "contract_plus_child"]
    retrieval_policy_sha256: Sha256Hex

    @model_validator(mode="after")
    def exact_candidate_contract(self) -> Self:
        if self.issuers != CANDIDATE_ISSUERS:
            raise ValueError("candidate config must contain exactly the four canonical issuers")
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


class IssuerRunMetrics(_CanonicalModel):
    issuer: str
    acquired: PositiveStrictInt
    succeeded: PositiveStrictInt
    failed: Literal[0]

    @field_validator("issuer")
    @classmethod
    def issuer_is_safe(cls, value: str) -> str:
        return validate_identifier(value, label="issuer")

    @model_validator(mode="after")
    def counts_match(self) -> Self:
        if self.acquired != self.succeeded + self.failed:
            raise ValueError("issuer run metrics do not balance")
        return self


class WorkerMetricsEvidence(_CanonicalModel):
    schema_version: Literal["cardrag.candidate-worker-metrics.v3"]
    source_commit: SourceCommit
    generation_id: str
    generation_manifest_sha256: Sha256Hex
    effective_config_sha256: Sha256Hex
    runtime_image_repo_digest: ImageReference
    runtime_container_image_id: ImageDigest
    runtime_image_store_identity: Literal["classic-config-id", "containerd-index-id"]
    runtime_container_config_image: ImageReference
    runtime_manifest_descriptor_digest: ImageDigest | None
    runtime_manifest_descriptor_platform: Literal["linux/amd64"] | None
    runtime_uid_gid: Literal["10001:10001"]
    rootfs_read_only_verified: Literal[True]
    cap_drop_all_verified: Literal[True]
    no_new_privileges_verified: Literal[True]
    seccomp_unconfined_verified: Literal[True]
    apparmor_unconfined_verified: Literal[True]
    systempaths_unconfined_verified: Literal[False]
    privileged_verified: Literal[False]
    cap_add_count_verified: Literal[0]
    worker_state_mount_path: Literal["/var/lib/cardrag-worker"]
    codex_home_mount_path: Literal["/var/lib/cardrag-codex-home"]
    codex_auth_root: Literal["/var/lib/cardrag-codex-home"]
    codex_home: Literal["/var/lib/cardrag-codex-home"]
    home: Literal["/var/lib/cardrag-codex-home/home"]
    codex_home_separate_volume_verified: Literal[True]
    worker_state_legacy_codex_auth_entries: Literal[0]
    codex_auth_json_mode: Literal["0600"]
    codex_auth_json_uid_gid: Literal["10001:10001"]
    codex_login_status_verified: Literal[True]
    codex_login_status_output_retained: Literal[False]
    codex_version_verified: Literal[True]
    bubblewrap_version_verified: Literal[True]
    bubblewrap_user_namespace_verified: Literal[True]
    codex_read_only_sandbox_verified: Literal[True]
    codex_general_file_read_and_exec_tools_disabled_verified: Literal[True]
    codex_shell_environment_inherit_none_verified: Literal[True]
    ocr_credential_token_rejection_verified: Literal[True]
    full_candidate_run: Literal[True]
    run_completed: Literal[True]
    issuer_metrics: tuple[IssuerRunMetrics, ...]
    documents: PositiveStrictInt
    chunks: PositiveStrictInt
    embedding_rows: PositiveStrictInt
    vector_sidecar_size_bytes: PositiveStrictInt
    embedding_dimension: Literal[4096]
    structure_source_non_whitespace_characters: PositiveStrictInt
    structure_covered_non_whitespace_characters: PositiveStrictInt
    cross_contract_parent_count: Literal[0]
    cross_contract_link_count: Literal[0]
    embedding_provider_calls: NonNegativeStrictInt
    pdf_seed_hits: PositiveStrictInt
    ocr_native_cache_hits: PositiveStrictInt
    ocr_native_cache_misses: PositiveStrictInt
    native_cache_publication_calls: Literal[0]
    generation_publication_calls: PositiveStrictInt

    @field_validator("generation_id")
    @classmethod
    def generation_id_is_safe(cls, value: str) -> str:
        return validate_identifier(value, label="generation_id")

    @model_validator(mode="after")
    def runtime_manifest_descriptor_is_complete(self) -> Self:
        if (self.runtime_manifest_descriptor_digest is None) != (
            self.runtime_manifest_descriptor_platform is None
        ):
            raise ValueError("runtime manifest descriptor digest and platform must be paired")
        return self

    @model_validator(mode="after")
    def metrics_cover_the_full_run(self) -> Self:
        if tuple(row.issuer for row in self.issuer_metrics) != CANDIDATE_ISSUERS:
            raise ValueError("Worker metrics must cover exactly four issuers")
        if self.documents != sum(row.acquired for row in self.issuer_metrics):
            raise ValueError("Worker document metrics differ from issuer totals")
        if (
            self.structure_covered_non_whitespace_characters
            != self.structure_source_non_whitespace_characters
        ):
            raise ValueError("Worker structure coverage is not 100 percent")
        if self.vector_sidecar_size_bytes != self.embedding_rows * 4096 * 4:
            raise ValueError("Worker vector sidecar is not exhaustive 4096D FP32")
        return self


class ToolSmokeResult(_CanonicalModel):
    tool: str
    passed: Literal[True]
    response_sha256: Sha256Hex


class MCPSmokeEvidence(_CanonicalModel):
    schema_version: Literal["cardrag.candidate-mcp-smoke.v2"]
    source_commit: SourceCommit
    generation_id: str
    generation_manifest_sha256: Sha256Hex
    effective_config_sha256: Sha256Hex
    runtime_image_repo_digest: ImageReference
    runtime_container_image_id: ImageDigest
    runtime_image_store_identity: Literal["classic-config-id", "containerd-index-id"]
    runtime_container_config_image: ImageReference
    runtime_manifest_descriptor_digest: ImageDigest | None
    runtime_manifest_descriptor_platform: Literal["linux/amd64"] | None
    runtime_uid_gid: Literal["10001:10001"]
    rootfs_read_only_verified: Literal[True]
    cap_drop_all_verified: Literal[True]
    no_new_privileges_verified: Literal[True]
    health_ready: Literal[True]
    serving_schema: Literal["cardrag.serving-db.v5"]
    embedding_dimension: Literal[4096]
    retrieval_mode: Literal["exact"]
    approximate: Literal[False]
    expected_active_contracts: PositiveStrictInt
    scored_contracts: PositiveStrictInt
    expected_embedding_rows: PositiveStrictInt
    scored_embedding_rows: PositiveStrictInt
    exact_blocks: PositiveStrictInt
    cross_contract_node_count: Literal[0]
    discovered_tools: tuple[str, ...]
    tool_results: tuple[ToolSmokeResult, ...]
    bundle_source_spans_verified: Literal[True]
    revision_history_verified: Literal[True]
    legacy_adapter_verified: Literal[True]
    pdf_range_status: Literal[206]
    pdf_magic_prefix: Literal["%PDF-"]
    pdf_content_range_verified: Literal[True]

    @field_validator("generation_id")
    @classmethod
    def generation_id_is_safe(cls, value: str) -> str:
        return validate_identifier(value, label="generation_id")

    @model_validator(mode="after")
    def runtime_manifest_descriptor_is_complete(self) -> Self:
        if (self.runtime_manifest_descriptor_digest is None) != (
            self.runtime_manifest_descriptor_platform is None
        ):
            raise ValueError("runtime manifest descriptor digest and platform must be paired")
        return self

    @model_validator(mode="after")
    def all_tools_and_rows_are_exact(self) -> Self:
        if self.discovered_tools != MCP_TOOLS:
            raise ValueError("MCP discovery must return exactly eight canonical tools")
        if tuple(result.tool for result in self.tool_results) != MCP_TOOLS:
            raise ValueError("MCP smoke must call every canonical tool exactly once")
        if self.scored_contracts != self.expected_active_contracts:
            raise ValueError("MCP exact smoke did not score every active contract")
        if self.scored_embedding_rows != self.expected_embedding_rows:
            raise ValueError("MCP exact smoke did not score every eligible row")
        return self


class NativeCacheObject(_CanonicalModel):
    path: str
    status: Literal[200, 404]
    sha256: Sha256Hex | None = None
    size_bytes: NonNegativeStrictInt | None = None

    @field_validator("path")
    @classmethod
    def native_path_is_safe(cls, value: str) -> str:
        path = validate_relative_path(value)
        parts = path.parts
        if len(parts) != 6 or parts[:3] != ("v1", "ocr-cache", "native"):
            raise ValueError("native cache snapshot contains a non-control path")
        prefix, reuse_key, control_name = parts[3:]
        if (
            len(prefix) != 2
            or any(character not in "0123456789abcdef" for character in prefix)
            or len(reuse_key) != 64
            or any(character not in "0123456789abcdef" for character in reuse_key)
            or prefix != reuse_key[:2]
            or control_name not in {"manifest.json", "READY.json"}
        ):
            raise ValueError("native cache control path does not bind a reuse key")
        return path.as_posix()

    @model_validator(mode="after")
    def status_has_exact_identity(self) -> Self:
        present = self.sha256 is not None and self.size_bytes is not None
        if (self.sha256 is None) != (self.size_bytes is None):
            raise ValueError("native cache object identity is partial")
        if self.status == 200 and not present:
            raise ValueError("present native cache object has no identity")
        if self.status == 404 and present:
            raise ValueError("absent native cache object has an identity")
        return self


class NativeCacheSnapshot(_CanonicalModel):
    schema_version: Literal["cardrag.native-cache-control-snapshot.v1"]
    source_commit: SourceCommit
    phase: Literal["before", "after"]
    namespace: Literal["v1/ocr-cache/native"]
    entries: tuple[NativeCacheObject, ...] = Field(min_length=4)
    inventory_sha256: Sha256Hex

    @model_validator(mode="after")
    def inventory_is_sorted_unique_and_bound(self) -> Self:
        paths = tuple(entry.path for entry in self.entries)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("native cache snapshot paths are not sorted and unique")
        pairs: dict[str, list[NativeCacheObject]] = {}
        for entry in self.entries:
            reuse_key = PurePosixPath(entry.path).parts[4]
            pairs.setdefault(reuse_key, []).append(entry)
        pair_statuses: set[int] = set()
        for controls in pairs.values():
            control_names = {PurePosixPath(control.path).name for control in controls}
            statuses = {control.status for control in controls}
            if control_names != {"manifest.json", "READY.json"} or len(controls) != 2:
                raise ValueError("native cache snapshot requires a complete control pair")
            if len(statuses) != 1:
                raise ValueError("native cache control pair has a partial status")
            pair_statuses.update(statuses)
        if pair_statuses != {200, 404}:
            raise ValueError("native cache control must include complete hit and miss pairs")
        expected = canonical_sha256(
            {
                "entries": self.entries,
                "schema_version": "cardrag.native-cache-control-inventory.v1",
            }
        )
        if self.inventory_sha256 != expected:
            raise ValueError("native cache inventory SHA-256 is not canonical")
        return self


class NativeCacheAuditEvidence(_CanonicalModel):
    schema_version: Literal["cardrag.native-cache-zero-write-audit.v1"]
    source_commit: SourceCommit
    generation_id: str
    cache_mode: Literal["read-only"]
    before_inventory_sha256: Sha256Hex
    after_inventory_sha256: Sha256Hex
    cache_hit_count: PositiveStrictInt
    cache_miss_count: PositiveStrictInt
    native_get_requests: PositiveStrictInt
    native_head_requests: Literal[0]
    native_write_requests: Literal[0]
    native_publication_calls: Literal[0]
    native_created_paths: Literal[0]
    native_modified_paths: Literal[0]
    native_deleted_paths: Literal[0]
    verified_read_only_seed: Literal[True]

    @field_validator("generation_id")
    @classmethod
    def generation_id_is_safe(cls, value: str) -> str:
        return validate_identifier(value, label="generation_id")

    @model_validator(mode="after")
    def before_and_after_are_identical(self) -> Self:
        if self.before_inventory_sha256 != self.after_inventory_sha256:
            raise ValueError("native cache before/after inventories differ")
        return self


class GenerationCASEvidence(_CanonicalModel):
    schema_version: Literal["cardrag.candidate-generation-cas-audit.v1"]
    source_commit: SourceCommit
    channel: Literal["candidate-v1.0.11"]
    generation_id: str
    manifest_sha256: Sha256Hex
    ready_sha256: Sha256Hex
    pointer_sha256: Sha256Hex
    serving_database: ArtifactRef
    vector_sidecar: ArtifactRef
    objects: tuple[ArtifactRef, ...] = Field(min_length=1)
    object_publish_calls: PositiveStrictInt
    object_create_writes: NonNegativeStrictInt
    database_puts: Literal[1]
    vector_puts: Literal[1]
    manifest_puts: Literal[1]
    ready_puts: Literal[1]
    pointer_cas_attempts: PositiveStrictInt
    pointer_cas_successes: Literal[1]
    logical_publication_calls: PositiveStrictInt
    total_generation_write_requests: PositiveStrictInt
    native_cache_write_requests: Literal[0]
    stable_channel_write_requests: Literal[0]

    @field_validator("generation_id")
    @classmethod
    def generation_id_is_safe(cls, value: str) -> str:
        return validate_identifier(value, label="generation_id")

    @model_validator(mode="after")
    def write_ledger_balances(self) -> Self:
        paths = tuple(item.path for item in self.objects)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("generation CAS objects are not sorted and unique")
        if any(PurePosixPath(item.path).parts[:3] != ("v1", "objects", "sha256") for item in self.objects):
            raise ValueError("generation CAS ledger contains a non-object artifact")
        if self.object_publish_calls != len(self.objects):
            raise ValueError("generation CAS logical object count does not match its ledger")
        if self.object_create_writes > self.object_publish_calls:
            raise ValueError("generation CAS object creates exceed logical publications")
        expected_logical = (
            self.object_publish_calls
            + self.database_puts
            + self.vector_puts
            + self.manifest_puts
            + self.ready_puts
            + self.pointer_cas_attempts
        )
        if self.logical_publication_calls != expected_logical:
            raise ValueError("generation CAS logical publication ledger does not balance")
        minimum_http_writes = (
            self.object_create_writes
            + self.database_puts
            + self.vector_puts
            + self.manifest_puts
            + self.ready_puts
            + self.pointer_cas_attempts
        )
        if self.total_generation_write_requests < minimum_http_writes:
            raise ValueError("generation CAS HTTP writes are below proven write requests")
        return self


class RollbackStep(_CanonicalModel):
    ordinal: PositiveStrictInt
    action: Literal["activate", "restart"]
    serving_schema: Literal["cardrag.serving-db.v4", "cardrag.serving-db.v5"]
    generation_id: str
    runtime_instance_sha256: Sha256Hex
    health_ready: Literal[True]
    tool_discovery_passed: Literal[True]
    search_mode: Literal["legacy-hybrid", "exact"]
    search_contracts_outcome: Literal["unsupported-rejected", "exact-passed"]

    @field_validator("generation_id")
    @classmethod
    def generation_id_is_safe(cls, value: str) -> str:
        return validate_identifier(value, label="generation_id")

    @model_validator(mode="after")
    def search_contract_matches_schema(self) -> Self:
        expected = (
            ("legacy-hybrid", "unsupported-rejected")
            if self.serving_schema == "cardrag.serving-db.v4"
            else ("exact", "exact-passed")
        )
        if (self.search_mode, self.search_contracts_outcome) != expected:
            raise ValueError("rollback smoke behavior does not match the serving schema")
        return self


class RollbackLedgerEvidence(_CanonicalModel):
    schema_version: Literal["cardrag.candidate-v4-v5-rollback-ledger.v1"]
    source_commit: SourceCommit
    channel: Literal["candidate-v1.0.11"]
    steps: tuple[RollbackStep, ...]
    rollback_verified: Literal[True]
    stable_channel_write_requests: Literal[0]

    @model_validator(mode="after")
    def sequence_is_v4_v5_restart_v4_v5(self) -> Self:
        if len(self.steps) != 5:
            raise ValueError("rollback ledger must contain exactly five steps")
        expected = (
            (1, "activate", "cardrag.serving-db.v4"),
            (2, "activate", "cardrag.serving-db.v5"),
            (3, "restart", "cardrag.serving-db.v5"),
            (4, "activate", "cardrag.serving-db.v4"),
            (5, "activate", "cardrag.serving-db.v5"),
        )
        observed = tuple((step.ordinal, step.action, step.serving_schema) for step in self.steps)
        if observed != expected:
            raise ValueError("rollback ledger sequence is not v4-v5-restart-v4-v5")
        if self.steps[0].generation_id != self.steps[3].generation_id:
            raise ValueError("rollback did not restore the original v4 generation")
        if self.steps[1].generation_id != self.steps[2].generation_id:
            raise ValueError("restart did not retain the v5 generation")
        if self.steps[1].generation_id != self.steps[4].generation_id:
            raise ValueError("final activation did not restore the tested v5 generation")
        if self.steps[0].generation_id == self.steps[1].generation_id:
            raise ValueError("rollback ledger v4 and v5 generations are not distinct")
        if self.steps[1].runtime_instance_sha256 == self.steps[2].runtime_instance_sha256:
            raise ValueError("rollback ledger does not prove a distinct restart instance")
        return self


class V109AssetIdentity(_CanonicalModel):
    asset: str
    before_sha256: Sha256Hex
    after_sha256: Sha256Hex
    equal: Literal[True]

    @model_validator(mode="after")
    def identities_match(self) -> Self:
        if self.before_sha256 != self.after_sha256:
            raise ValueError("v1.0.9 asset identity changed")
        return self


class V109IdentityEvidence(_CanonicalModel):
    schema_version: Literal["cardrag.v109-before-after-identity.v1"]
    source_commit: SourceCommit
    assets: tuple[V109AssetIdentity, ...]
    candidate_rw_mounts_of_v109_volumes: Literal[0]
    candidate_stable_channel_requests: Literal[0]
    candidate_librechat_switch_requests: Literal[0]
    destructive_cleanup_commands: Literal[0]
    v109_restart_commands: Literal[0]

    @model_validator(mode="after")
    def all_operating_assets_are_covered(self) -> Self:
        if tuple(asset.asset for asset in self.assets) != V109_IDENTITY_ASSETS:
            raise ValueError("v1.0.9 identity ledger does not cover the canonical asset set")
        return self


class CandidateEvidenceBindings(_CanonicalModel):
    effective_config: EvidenceFile
    generation_manifest: EvidenceFile
    generation_ready: EvidenceFile
    candidate_pointer: EvidenceFile
    worker_metrics: EvidenceFile
    mcp_smoke: EvidenceFile
    native_cache_before: EvidenceFile
    native_cache_after: EvidenceFile
    native_cache_audit: EvidenceFile
    generation_cas: EvidenceFile
    rollback_ledger: EvidenceFile
    v109_identity: EvidenceFile

    def files(self) -> tuple[EvidenceFile, ...]:
        return (
            self.effective_config,
            self.generation_manifest,
            self.generation_ready,
            self.candidate_pointer,
            self.worker_metrics,
            self.mcp_smoke,
            self.native_cache_before,
            self.native_cache_after,
            self.native_cache_audit,
            self.generation_cas,
            self.rollback_ledger,
            self.v109_identity,
        )

    @model_validator(mode="after")
    def evidence_paths_are_distinct(self) -> Self:
        paths = tuple(binding.path for binding in self.files())
        if len(paths) != len(set(paths)):
            raise ValueError("candidate receipt reuses an evidence path")
        return self


class CandidateAcceptanceReceipt(_CanonicalModel):
    schema_version: Literal["cardrag.candidate-acceptance-receipt.v1"]
    release_version: Literal["1.0.11"]
    source_commit: SourceCommit
    compose_project: Literal["cardrag-v111-candidate"]
    channel: Literal["candidate-v1.0.11"]
    generation_id: str
    issuers: tuple[str, ...]
    release_eligible: Literal[True]
    evidence: CandidateEvidenceBindings

    @field_validator("generation_id")
    @classmethod
    def generation_id_is_safe(cls, value: str) -> str:
        return validate_identifier(value, label="generation_id")

    @model_validator(mode="after")
    def exact_issuers_are_present(self) -> Self:
        if self.issuers != CANDIDATE_ISSUERS:
            raise ValueError("candidate receipt must contain exactly four canonical issuers")
        return self


class CandidateAcceptanceValidation(_CanonicalModel):
    schema_version: Literal["cardrag.candidate-acceptance-validation.v1"]
    status: Literal["validated"]
    receipt_sha256: Sha256Hex
    source_commit: SourceCommit
    generation_id: str
    worker_image: CandidateImageIdentity
    mcp_image: CandidateImageIdentity


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CandidateAcceptanceError("json_duplicate_key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> object:
    raise CandidateAcceptanceError("json_non_finite")


def _parse_canonical_model[ModelT: BaseModel](
    raw: bytes,
    model: type[ModelT],
    *,
    trailing_lf: bool,
) -> ModelT:
    try:
        text = raw.decode("utf-8", errors="strict")
        parsed = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
        if not isinstance(parsed, dict):
            raise CandidateAcceptanceError("json_root_not_object")
        payload = raw[:-1] if trailing_lf and raw.endswith(b"\n") else raw
        value = model.model_validate_json(payload)
    except CandidateAcceptanceError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError, TypeError):
        raise CandidateAcceptanceError("json_schema_invalid") from None
    expected = canonical_json_bytes(value) + (b"\n" if trailing_lf else b"")
    if raw != expected:
        raise CandidateAcceptanceError("json_not_canonical")
    return value


def _read_open_file(descriptor: int, *, maximum_size: int, expected_size: int | None = None) -> bytes:
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode):
        raise CandidateAcceptanceError("evidence_not_regular")
    if not 1 <= before.st_size <= maximum_size:
        raise CandidateAcceptanceError("evidence_size_out_of_range")
    if expected_size is not None and before.st_size != expected_size:
        raise CandidateAcceptanceError("evidence_size_mismatch")
    chunks: list[bytes] = []
    remaining = before.st_size
    while remaining:
        block = os.read(descriptor, min(_READ_SIZE, remaining))
        if not block:
            raise CandidateAcceptanceError("evidence_short_read")
        chunks.append(block)
        remaining -= len(block)
    if os.read(descriptor, 1):
        raise CandidateAcceptanceError("evidence_grew_during_read")
    after = os.fstat(descriptor)
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    if identity_before != identity_after:
        raise CandidateAcceptanceError("evidence_changed_during_read")
    return b"".join(chunks)


def _nofollow_flags(*, directory: bool = False) -> int:
    nofollow = cast(int | None, getattr(os, "O_NOFOLLOW", None))
    if nofollow is None:
        raise CandidateAcceptanceError("nofollow_unavailable")
    flags = os.O_RDONLY | os.O_CLOEXEC | nofollow
    if directory:
        directory_flag = cast(int | None, getattr(os, "O_DIRECTORY", None))
        if directory_flag is None:
            raise CandidateAcceptanceError("directory_open_unavailable")
        flags |= directory_flag
    return flags


def _read_absolute_file(path: Path, *, maximum_size: int) -> bytes:
    if not path.is_absolute():
        raise CandidateAcceptanceError("path_not_absolute")
    try:
        descriptor = os.open(os.fspath(path), _nofollow_flags())
    except OSError:
        raise CandidateAcceptanceError("evidence_open_failed") from None
    try:
        return _read_open_file(descriptor, maximum_size=maximum_size)
    finally:
        os.close(descriptor)


def _open_evidence_root(path: Path) -> int:
    if not path.is_absolute():
        raise CandidateAcceptanceError("evidence_root_not_absolute")
    try:
        descriptor = os.open(os.fspath(path), _nofollow_flags(directory=True))
    except OSError:
        raise CandidateAcceptanceError("evidence_root_open_failed") from None
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise CandidateAcceptanceError("evidence_root_not_directory")
    return descriptor


def _read_bound_file(root_descriptor: int, binding: EvidenceFile) -> bytes:
    parts = PurePosixPath(binding.path).parts
    directory_descriptor = os.dup(root_descriptor)
    try:
        for segment in parts[:-1]:
            try:
                next_descriptor = os.open(
                    segment,
                    _nofollow_flags(directory=True),
                    dir_fd=directory_descriptor,
                )
            except OSError:
                raise CandidateAcceptanceError("evidence_component_open_failed") from None
            os.close(directory_descriptor)
            directory_descriptor = next_descriptor
        try:
            file_descriptor = os.open(parts[-1], _nofollow_flags(), dir_fd=directory_descriptor)
        except OSError:
            raise CandidateAcceptanceError("evidence_open_failed") from None
        try:
            raw = _read_open_file(
                file_descriptor,
                maximum_size=_MAX_EVIDENCE_BYTES,
                expected_size=binding.size_bytes,
            )
        finally:
            os.close(file_descriptor)
    finally:
        os.close(directory_descriptor)
    if hashlib.sha256(raw).hexdigest() != binding.sha256:
        raise CandidateAcceptanceError("evidence_sha256_mismatch")
    return raw


def _load_bound_model[ModelT: BaseModel](
    root_descriptor: int,
    binding: EvidenceFile,
    model: type[ModelT],
    *,
    trailing_lf: bool = True,
) -> ModelT:
    return _parse_canonical_model(
        _read_bound_file(root_descriptor, binding),
        model,
        trailing_lf=trailing_lf,
    )


def _runtime_image_identity_matches(
    evidence: WorkerMetricsEvidence | MCPSmokeEvidence,
    image: CandidateImageIdentity,
) -> bool:
    """Bind Docker classic and containerd stores to one sealed OCI identity.

    Classic stores expose the platform config digest as the container image ID;
    their manifest descriptor may be absent or, when present, must be the sealed
    linux/amd64 platform manifest. Containerd stores expose the sealed OCI index
    as the container image ID and must also expose that exact platform manifest.
    """

    if (
        evidence.runtime_image_repo_digest != image.compose_image_reference
        or evidence.runtime_container_config_image != image.compose_image_reference
    ):
        return False
    descriptor_is_platform = (
        evidence.runtime_manifest_descriptor_digest == image.platform_manifest_digest
        and evidence.runtime_manifest_descriptor_platform == image.platform
    )
    if evidence.runtime_image_store_identity == "classic-config-id":
        return evidence.runtime_container_image_id == image.platform_config_digest and (
            (
                evidence.runtime_manifest_descriptor_digest is None
                and evidence.runtime_manifest_descriptor_platform is None
            )
            or descriptor_is_platform
        )
    return evidence.runtime_container_image_id == image.digest and descriptor_is_platform


def verify_candidate_acceptance(
    receipt_path: Path,
    evidence_root: Path,
    *,
    expected_receipt_sha256: str,
    expected_source_commit: str,
    expected_image_repository: str,
) -> CandidateAcceptanceValidation:
    """Validate a canonical receipt and all of its bound evidence read-only."""

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
    receipt = _parse_canonical_model(
        receipt_raw,
        CandidateAcceptanceReceipt,
        trailing_lf=True,
    )
    if receipt.source_commit != expected_source_commit:
        raise CandidateAcceptanceError("receipt_source_commit_mismatch")

    root_descriptor = _open_evidence_root(evidence_root)
    try:
        bindings = receipt.evidence
        config = _load_bound_model(root_descriptor, bindings.effective_config, EffectiveConfigEvidence)
        manifest = _load_bound_model(
            root_descriptor,
            bindings.generation_manifest,
            GenerationManifest,
            trailing_lf=False,
        )
        ready = _load_bound_model(
            root_descriptor,
            bindings.generation_ready,
            GenerationReady,
            trailing_lf=False,
        )
        pointer = _load_bound_model(
            root_descriptor,
            bindings.candidate_pointer,
            GenerationPointer,
            trailing_lf=False,
        )
        worker = _load_bound_model(root_descriptor, bindings.worker_metrics, WorkerMetricsEvidence)
        mcp = _load_bound_model(root_descriptor, bindings.mcp_smoke, MCPSmokeEvidence)
        before = _load_bound_model(root_descriptor, bindings.native_cache_before, NativeCacheSnapshot)
        after = _load_bound_model(root_descriptor, bindings.native_cache_after, NativeCacheSnapshot)
        native_audit = _load_bound_model(
            root_descriptor,
            bindings.native_cache_audit,
            NativeCacheAuditEvidence,
        )
        generation_cas = _load_bound_model(
            root_descriptor,
            bindings.generation_cas,
            GenerationCASEvidence,
        )
        rollback = _load_bound_model(
            root_descriptor,
            bindings.rollback_ledger,
            RollbackLedgerEvidence,
        )
        v109 = _load_bound_model(root_descriptor, bindings.v109_identity, V109IdentityEvidence)
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
        v109.source_commit,
    )
    if any(source != receipt.source_commit for source in evidence_sources):
        raise CandidateAcceptanceError("evidence_source_commit_mismatch")
    if manifest.schema_version != "cardrag.generation.v5" or manifest.serving_schema != (
        "cardrag.serving-db.v5"
    ):
        raise CandidateAcceptanceError("generation_not_v5")
    if manifest.generation_id != receipt.generation_id or manifest.issuer_codes != CANDIDATE_ISSUERS:
        raise CandidateAcceptanceError("generation_identity_mismatch")
    if manifest.counts.documents < 1 or manifest.counts.chunks < 1:
        raise CandidateAcceptanceError("generation_empty")
    if manifest.structure_contract is None or (
        manifest.structure_contract.source_coverage.source_non_whitespace_characters < 1
    ):
        raise CandidateAcceptanceError("generation_structure_empty")
    if manifest.document_aggregation_profile is None or manifest.sealed_profile_sha256 is None:
        raise CandidateAcceptanceError("generation_profile_unsealed")
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
        or config.document_aggregation_profile_sha256 != manifest.sealed_profile_sha256
        or config.document_aggregation_policy != manifest.document_aggregation_policy
        or config.retrieval_policy_sha256 != manifest.retrieval_policy_sha256
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

    return CandidateAcceptanceValidation(
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


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        validation = verify_candidate_acceptance(
            cast(Path, arguments.receipt),
            cast(Path, arguments.evidence_root),
            expected_receipt_sha256=cast(str, arguments.expected_receipt_sha256),
            expected_source_commit=cast(str, arguments.expected_source_commit),
            expected_image_repository=cast(str, arguments.expected_image_repository),
        )
    except CandidateAcceptanceError:
        print("candidate acceptance validation failed", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(validation.canonical_bytes())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "CANDIDATE_ISSUERS",
    "MCP_TOOLS",
    "RECEIPT_SCHEMA",
    "V109_IDENTITY_ASSETS",
    "CandidateAcceptanceError",
    "CandidateAcceptanceReceipt",
    "CandidateAcceptanceValidation",
    "CandidateEvidenceBindings",
    "CandidateImageIdentity",
    "EffectiveConfigEvidence",
    "EvidenceFile",
    "GenerationCASEvidence",
    "IssuerRunMetrics",
    "MCPSmokeEvidence",
    "NativeCacheAuditEvidence",
    "NativeCacheObject",
    "NativeCacheSnapshot",
    "RollbackLedgerEvidence",
    "RollbackStep",
    "ToolSmokeResult",
    "V109AssetIdentity",
    "V109IdentityEvidence",
    "WorkerMetricsEvidence",
    "main",
    "verify_candidate_acceptance",
]
