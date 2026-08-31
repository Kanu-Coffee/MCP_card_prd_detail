"""Fail-closed loading for a release-validated v1.0.11 aggregation profile.

The statistical evaluator lives in ``cardrag-mcp``.  The finite Worker does
not repeat that expensive evaluation, but it only accepts the evaluator's
canonical artifact when an operator also supplies its independently verified
file SHA-256.  The selected core profile is then carried to the exporter and
generation manifest without trusting an untyped JSON fragment.  Compact score
evidence is represented by a canonical manifest plus three separately bound
portable artifacts; the Worker validates those bindings without reopening the
large offline score matrices at runtime.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, cast

from cardrag_core import DocumentAggregationProfile, canonical_json_bytes, canonical_sha256
from pydantic import ValidationError

PROFILE_ARTIFACT_SCHEMA_VERSION = "cardrag.document-aggregation-profile-artifact.v1"
MAX_PROFILE_ARTIFACT_BYTES = 32 * 1024 * 1024
MAX_PORTABLE_ARTIFACT_BYTES = 95_000_000
MAX_SCORE_COUNT = 20_000_000

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_COMMIT = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_TOP_LEVEL_KEYS = frozenset(
    {
        "artifact_bindings",
        "bootstrap",
        "comparisons",
        "coverage",
        "definitions",
        "excluded_nonretrieval_slices",
        "policies",
        "release_gate",
        "schema_version",
        "score_artifact_manifest",
        "sealed_profile",
        "sealed_profile_sha256",
        "selection",
    }
)
_BINDING_KEYS = frozenset(
    {
        "corpus_inventory_sha256",
        "corpus_inventory_size_bytes",
        "generation_manifest_sha256",
        "gold_sha256",
        "query_vector_matrix_sha256",
        "query_vector_matrix_size_bytes",
        "score_artifact_manifest_sha256",
        "score_artifact_sha256",
        "score_artifact_size_bytes",
        "score_matrix_sha256",
        "score_matrix_size_bytes",
    }
)
_SCORE_MANIFEST_KEYS = frozenset(
    {
        "approximate",
        "byte_order",
        "corpus_inventory",
        "corpus_row_count",
        "embedding_dimension",
        "embedding_model",
        "embedding_profile_id",
        "exact",
        "exact_row_corpus_sha256",
        "generation_id",
        "generation_manifest_sha256",
        "gold_sha256",
        "matrix_order",
        "query_count",
        "query_vector_matrix",
        "runtime_document_aggregation_policy",
        "runtime_document_aggregation_status",
        "runtime_sealed_profile_sha256",
        "scalar_type",
        "schema_version",
        "score_count",
        "score_matrix",
        "scoring_contract",
        "serving_database_sha256",
        "source_commit",
        "temporal_scope_policy",
        "validation_profile",
        "vector_sidecar_sha256",
    }
)
_ARTIFACT_BINDING_KEYS = frozenset({"sha256", "size_bytes"})


class AggregationProfileV5Error(RuntimeError):
    """A bounded reason code for unsafe or inconsistent profile input."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class VerifiedAggregationProfileV5:
    """The selected core profile plus both of its immutable hash identities."""

    profile: DocumentAggregationProfile
    profile_sha256: str
    artifact_sha256: str

    def __post_init__(self) -> None:
        if self.profile.profile_sha256 != self.profile_sha256:
            raise ValueError("sealed aggregation profile SHA-256 is inconsistent")
        if _SHA256.fullmatch(self.artifact_sha256) is None:
            raise ValueError("aggregation profile artifact SHA-256 is invalid")


def _identity(value: os.stat_result) -> tuple[int, int, int, int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_gid,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _read_regular(path: Path) -> bytes:
    if not path.is_absolute():
        raise AggregationProfileV5Error("profile_path_not_absolute")
    try:
        listed = path.lstat()
    except FileNotFoundError:
        raise AggregationProfileV5Error("profile_artifact_missing") from None
    if stat.S_ISLNK(listed.st_mode) or not stat.S_ISREG(listed.st_mode):
        raise AggregationProfileV5Error("profile_artifact_not_regular")
    if listed.st_size <= 0 or listed.st_size > MAX_PROFILE_ARTIFACT_BYTES:
        raise AggregationProfileV5Error("profile_artifact_size_invalid")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AggregationProfileV5Error("profile_artifact_open_failed") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise AggregationProfileV5Error("profile_artifact_not_regular")
        if before.st_size <= 0 or before.st_size > MAX_PROFILE_ARTIFACT_BYTES:
            raise AggregationProfileV5Error("profile_artifact_size_invalid")
        if _identity(listed) != _identity(before):
            raise AggregationProfileV5Error("profile_artifact_changed_during_read")
        remaining = before.st_size
        chunks: list[bytes] = []
        size = 0
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise AggregationProfileV5Error("profile_artifact_changed_during_read")
            if len(chunk) > MAX_PROFILE_ARTIFACT_BYTES - size:
                raise AggregationProfileV5Error("profile_artifact_size_invalid")
            chunks.append(chunk)
            size += len(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise AggregationProfileV5Error("profile_artifact_changed_during_read")
        after = os.fstat(descriptor)
        try:
            current = path.lstat()
        except OSError:
            raise AggregationProfileV5Error("profile_artifact_changed_during_read") from None
        identity = _identity(before)
        if size != before.st_size or identity != _identity(after) or identity != _identity(current):
            raise AggregationProfileV5Error("profile_artifact_changed_during_read")
    finally:
        os.close(descriptor)
    return b"".join(chunks)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AggregationProfileV5Error("profile_artifact_not_canonical")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> NoReturn:
    raise AggregationProfileV5Error("profile_artifact_not_canonical")


def _mapping(value: object, *, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AggregationProfileV5Error(code)
    return cast(Mapping[str, Any], value)


def _sha256(value: object, *, code: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise AggregationProfileV5Error(code)
    return value


def _positive_int(value: object, *, code: str) -> int:
    if type(value) is not int or value < 1:
        raise AggregationProfileV5Error(code)
    return value


def _artifact_binding(
    value: object,
    *,
    code: str,
) -> tuple[str, int]:
    binding = _mapping(value, code=code)
    if set(binding) != _ARTIFACT_BINDING_KEYS:
        raise AggregationProfileV5Error(code)
    digest = _sha256(binding.get("sha256"), code=code)
    size = _positive_int(binding.get("size_bytes"), code=code)
    if size > MAX_PORTABLE_ARTIFACT_BYTES:
        raise AggregationProfileV5Error(code)
    return digest, size


def load_verified_aggregation_profile_v5(
    path: Path,
    *,
    expected_artifact_sha256: str,
) -> VerifiedAggregationProfileV5:
    """Load one canonical, passed M0 profile artifact by its full file hash."""

    if _SHA256.fullmatch(expected_artifact_sha256) is None:
        raise AggregationProfileV5Error("profile_artifact_sha256_invalid")
    data = _read_regular(path)
    if hashlib.sha256(data).hexdigest() != expected_artifact_sha256:
        raise AggregationProfileV5Error("profile_artifact_sha256_mismatch")
    if not data.endswith(b"\n") or data.endswith(b"\n\n"):
        raise AggregationProfileV5Error("profile_artifact_not_canonical")
    body = data[:-1]
    try:
        decoded = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (AggregationProfileV5Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AggregationProfileV5Error("profile_artifact_not_canonical") from exc
    if not isinstance(decoded, dict) or body != canonical_json_bytes(decoded):
        raise AggregationProfileV5Error("profile_artifact_not_canonical")
    payload = cast(dict[str, Any], decoded)
    if set(payload) != _TOP_LEVEL_KEYS or payload.get("schema_version") != PROFILE_ARTIFACT_SCHEMA_VERSION:
        raise AggregationProfileV5Error("profile_artifact_contract_invalid")

    gate = _mapping(payload["release_gate"], code="profile_release_gate_invalid")
    if (
        set(gate) != {"evaluated", "failure_reasons", "status"}
        or gate.get("evaluated") is not True
        or gate.get("failure_reasons") != []
        or gate.get("status") != "passed"
    ):
        raise AggregationProfileV5Error("profile_release_gate_not_passed")

    sealed_payload = _mapping(payload["sealed_profile"], code="sealed_profile_missing")
    try:
        # JSON arrays are the canonical encoding for tuple-valued definitions;
        # validate in JSON mode so strict core models accept that wire form.
        profile = DocumentAggregationProfile.model_validate_json(canonical_json_bytes(sealed_payload))
    except ValidationError as exc:
        raise AggregationProfileV5Error("sealed_profile_contract_invalid") from exc
    profile_sha256 = _sha256(
        payload["sealed_profile_sha256"],
        code="sealed_profile_sha256_invalid",
    )
    if profile.profile_sha256 != profile_sha256:
        raise AggregationProfileV5Error("sealed_profile_sha256_mismatch")

    selection = _mapping(payload["selection"], code="profile_selection_invalid")
    if (
        set(selection) != {"objective", "rule", "winner"}
        or selection.get("objective") != profile.selection_objective
        or selection.get("winner") != profile.aggregation_policy
        or not isinstance(selection.get("rule"), str)
        or not selection.get("rule")
    ):
        raise AggregationProfileV5Error("profile_selection_invalid")

    bootstrap = _mapping(payload["bootstrap"], code="profile_bootstrap_invalid")
    if dict(bootstrap) != profile.bootstrap.model_dump(mode="json"):
        raise AggregationProfileV5Error("profile_bootstrap_invalid")
    definitions = _mapping(payload["definitions"], code="profile_definitions_invalid")
    if definitions.get(profile.aggregation_policy) != profile.aggregation_definition.model_dump(mode="json"):
        raise AggregationProfileV5Error("profile_definitions_invalid")
    if payload["excluded_nonretrieval_slices"] != ["no_answer"]:
        raise AggregationProfileV5Error("profile_excluded_slices_invalid")
    if not isinstance(payload["policies"], dict) or not isinstance(payload["comparisons"], dict):
        raise AggregationProfileV5Error("profile_statistics_missing")

    bindings = _mapping(payload["artifact_bindings"], code="profile_bindings_invalid")
    if set(bindings) != _BINDING_KEYS:
        raise AggregationProfileV5Error("profile_bindings_invalid")
    generation_manifest_sha256 = _sha256(
        bindings.get("generation_manifest_sha256"),
        code="profile_bindings_invalid",
    )
    gold_sha256 = _sha256(bindings.get("gold_sha256"), code="profile_bindings_invalid")
    score_manifest_sha256 = _sha256(
        bindings.get("score_artifact_manifest_sha256"),
        code="profile_bindings_invalid",
    )
    score_artifact_sha256 = _sha256(
        bindings.get("score_artifact_sha256"),
        code="profile_bindings_invalid",
    )
    score_artifact_size_bytes = _positive_int(
        bindings.get("score_artifact_size_bytes"),
        code="profile_bindings_invalid",
    )
    corpus_inventory_binding = (
        _sha256(
            bindings.get("corpus_inventory_sha256"),
            code="profile_bindings_invalid",
        ),
        _positive_int(
            bindings.get("corpus_inventory_size_bytes"),
            code="profile_bindings_invalid",
        ),
    )
    score_matrix_binding = (
        _sha256(bindings.get("score_matrix_sha256"), code="profile_bindings_invalid"),
        _positive_int(
            bindings.get("score_matrix_size_bytes"),
            code="profile_bindings_invalid",
        ),
    )
    query_vector_matrix_binding = (
        _sha256(
            bindings.get("query_vector_matrix_sha256"),
            code="profile_bindings_invalid",
        ),
        _positive_int(
            bindings.get("query_vector_matrix_size_bytes"),
            code="profile_bindings_invalid",
        ),
    )
    if score_artifact_size_bytes > MAX_PORTABLE_ARTIFACT_BYTES or any(
        size > MAX_PORTABLE_ARTIFACT_BYTES
        for _digest, size in (
            corpus_inventory_binding,
            score_matrix_binding,
            query_vector_matrix_binding,
        )
    ):
        raise AggregationProfileV5Error("profile_bindings_invalid")
    if (
        generation_manifest_sha256 != profile.generation_manifest_sha256
        or gold_sha256 != profile.gold_sha256
        or score_artifact_sha256 != profile.score_artifact_sha256
    ):
        raise AggregationProfileV5Error("profile_bindings_mismatch")

    score_manifest = _mapping(
        payload["score_artifact_manifest"],
        code="profile_score_manifest_invalid",
    )
    if set(score_manifest) != _SCORE_MANIFEST_KEYS:
        raise AggregationProfileV5Error("profile_score_manifest_invalid")
    if canonical_sha256(score_manifest) != score_manifest_sha256:
        raise AggregationProfileV5Error("profile_score_manifest_sha256_mismatch")
    for field in (
        "gold_sha256",
        "generation_manifest_sha256",
        "serving_database_sha256",
        "vector_sidecar_sha256",
        "exact_row_corpus_sha256",
    ):
        _sha256(score_manifest.get(field), code="profile_score_manifest_invalid")
    source_commit = score_manifest.get("source_commit")
    if not isinstance(source_commit, str) or _SOURCE_COMMIT.fullmatch(source_commit) is None:
        raise AggregationProfileV5Error("profile_score_manifest_invalid")
    query_count = _positive_int(
        score_manifest.get("query_count"),
        code="profile_score_manifest_invalid",
    )
    corpus_row_count = _positive_int(
        score_manifest.get("corpus_row_count"),
        code="profile_score_manifest_invalid",
    )
    score_count = _positive_int(
        score_manifest.get("score_count"),
        code="profile_score_manifest_invalid",
    )
    if (
        not 300 <= query_count <= 500
        or score_count > MAX_SCORE_COUNT
        or score_count != query_count * corpus_row_count
    ):
        raise AggregationProfileV5Error("profile_score_manifest_invalid")
    manifest_corpus_inventory = _artifact_binding(
        score_manifest.get("corpus_inventory"),
        code="profile_score_manifest_invalid",
    )
    manifest_score_matrix = _artifact_binding(
        score_manifest.get("score_matrix"),
        code="profile_score_manifest_invalid",
    )
    manifest_query_vector_matrix = _artifact_binding(
        score_manifest.get("query_vector_matrix"),
        code="profile_score_manifest_invalid",
    )
    if (
        score_manifest.get("schema_version") != "cardrag.document-aggregation-score-artifact.v2"
        or score_manifest.get("generation_id") != profile.generation_id
        or score_manifest.get("generation_manifest_sha256") != profile.generation_manifest_sha256
        or score_manifest.get("gold_sha256") != profile.gold_sha256
        or score_manifest.get("exact_row_corpus_sha256") != profile.exact_row_corpus_sha256
        or score_manifest.get("embedding_profile_id") != profile.embedding_profile_id
        or score_manifest.get("embedding_model") != "qwen/qwen3-embedding-8b"
        or type(score_manifest.get("embedding_dimension")) is not int
        or score_manifest.get("embedding_dimension") != 4096
        or score_manifest.get("exact") is not True
        or score_manifest.get("approximate") is not False
        or score_manifest.get("scoring_contract") != "cardrag.v5-exact-row-score.v1"
        or score_manifest.get("temporal_scope_policy") != "gold-query.v1"
        or score_manifest.get("runtime_document_aggregation_status") != "candidate_default"
        or score_manifest.get("runtime_document_aggregation_policy") != "max_child"
        or score_manifest.get("runtime_sealed_profile_sha256") is not None
        or score_manifest.get("byte_order") != "little-endian"
        or score_manifest.get("scalar_type") != "float32"
        or score_manifest.get("matrix_order") != "row-major"
        or score_manifest.get("validation_profile") != "release_grade"
        or manifest_corpus_inventory != corpus_inventory_binding
        or manifest_score_matrix != score_matrix_binding
        or manifest_query_vector_matrix != query_vector_matrix_binding
        or manifest_score_matrix[1] != score_count * 4
        or manifest_query_vector_matrix[1] != query_count * 4096 * 4
    ):
        raise AggregationProfileV5Error("profile_score_manifest_mismatch")

    coverage = _mapping(payload["coverage"], code="profile_coverage_invalid")
    if (
        set(coverage)
        != {
            "all_queries_exact",
            "approximate",
            "corpus_row_count",
            "maximum_active_contracts",
            "minimum_active_contracts",
            "query_count",
            "score_count",
        }
        or coverage.get("all_queries_exact") is not True
        or coverage.get("approximate") is not False
        or coverage.get("query_count") != query_count
        or coverage.get("corpus_row_count") != corpus_row_count
        or coverage.get("score_count") != score_count
    ):
        raise AggregationProfileV5Error("profile_coverage_invalid")
    maximum_contracts = _positive_int(
        coverage.get("maximum_active_contracts"),
        code="profile_coverage_invalid",
    )
    minimum_contracts = _positive_int(
        coverage.get("minimum_active_contracts"),
        code="profile_coverage_invalid",
    )
    if minimum_contracts > maximum_contracts:
        raise AggregationProfileV5Error("profile_coverage_invalid")

    return VerifiedAggregationProfileV5(
        profile=profile,
        profile_sha256=profile_sha256,
        artifact_sha256=expected_artifact_sha256,
    )


__all__ = [
    "AggregationProfileV5Error",
    "MAX_PROFILE_ARTIFACT_BYTES",
    "MAX_PORTABLE_ARTIFACT_BYTES",
    "PROFILE_ARTIFACT_SCHEMA_VERSION",
    "VerifiedAggregationProfileV5",
    "load_verified_aggregation_profile_v5",
]
