"""Seal the statistically selected v1.0.10 document aggregation policy.

The online exact-search implementation is deliberately not imported here.  This
offline evaluator consumes every row score from hash-bound compact v2 artifacts,
reconstructs all three planned contract aggregation policies, and emits a
canonical profile artifact.  Release mode refuses to seal a profile unless a
unique policy wins the paired 95% bootstrap comparison and is non-regressive
against ``max_child`` overall and on every retrieval-bearing release slice.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import mmap
import os
import re
import stat
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal, Self, cast

import numpy as np
import numpy.typing as npt
from cardrag_core import DocumentAggregationProfile, GenerationManifest
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from cardrag_mcp.evaluation import (
    MAX_BOOTSTRAP_SAMPLES,
    MIN_BOOTSTRAP_SAMPLES,
    MIN_RELEASE_BOOTSTRAP_SAMPLES,
    REQUIRED_RELEASE_SLICES,
    EvaluationError,
    GoldDataset,
    GoldQuery,
    load_gold_jsonl,
)

SCORE_ARTIFACT_SCHEMA_VERSION = "cardrag.document-aggregation-score-artifact.v2"
QUERY_COVERAGE_SCHEMA_VERSION = "cardrag.document-aggregation-query-coverage.v2"
CORPUS_INVENTORY_SCHEMA_VERSION = "cardrag.document-aggregation-corpus-inventory.v1"
CORPUS_ROW_SCHEMA_VERSION = "cardrag.document-aggregation-corpus-row.v1"
PROFILE_ARTIFACT_SCHEMA_VERSION = "cardrag.document-aggregation-profile-artifact.v1"
SEALED_PROFILE_SCHEMA_VERSION = "cardrag.document-aggregation-profile.v1"
VALIDATION_RECEIPT_SCHEMA_VERSION = "cardrag.document-aggregation-validation-receipt.v1"

MAX_PORTABLE_ARTIFACT_BYTES = 95_000_000
MAX_SCORE_ARTIFACT_BYTES = MAX_PORTABLE_ARTIFACT_BYTES
MAX_SCORE_LINE_BYTES = 64 * 1024
MAX_SCORE_COUNT = 20_000_000
MAX_PROFILE_BYTES = 32 * 1024 * 1024

MAX_CHILD: Literal["max_child"] = "max_child"
TOP3_MEAN: Literal["top3_mean"] = "top3_mean"
CONTRACT_PLUS_CHILD: Literal["contract_plus_child"] = "contract_plus_child"
POLICIES = (MAX_CHILD, TOP3_MEAN, CONTRACT_PLUS_CHILD)
type AggregationPolicy = Literal["max_child", "top3_mean", "contract_plus_child"]

METRICS = ("contract_recall_at_10", "ndcg_at_10", "mrr_at_10")
SELECTION_OBJECTIVE = "ndcg_at_10"
NON_RETRIEVAL_SLICES = frozenset({"no_answer"})
REQUIRED_AGGREGATION_SLICES = REQUIRED_RELEASE_SLICES - NON_RETRIEVAL_SLICES

Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Identifier = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,511}$"),
]
EmbeddingProfileId = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"),
]
ViewType = Literal[
    "TITLE",
    "RAW_ITEM",
    "CONTEXTUAL_ITEM",
    "DETAIL",
    "MAJOR_SECTION",
    "CONTRACT",
]

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_COMMIT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


class AggregationProfileError(RuntimeError):
    """A bounded, machine-readable profile evaluation failure."""

    def __init__(self, code: str, *, line: int | None = None) -> None:
        self.code = code
        self.line = line
        super().__init__(code)


def _validated_expected_source_commit(
    value: str | None,
    *,
    release_gate: bool,
) -> str | None:
    if value is None:
        if release_gate:
            raise AggregationProfileError("expected_source_commit_required")
        return None
    if _SOURCE_COMMIT.fullmatch(value) is None:
        raise AggregationProfileError("expected_source_commit_invalid")
    return value


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class ArtifactBinding(_StrictModel):
    sha256: Sha256Hex
    size_bytes: int = Field(strict=True, gt=0, le=MAX_PORTABLE_ARTIFACT_BYTES)


class ScoreArtifactManifest(_StrictModel):
    schema_version: Literal["cardrag.document-aggregation-score-artifact.v2"]
    gold_sha256: Sha256Hex
    query_count: int = Field(strict=True, ge=1, le=500)
    corpus_row_count: int = Field(strict=True, ge=1, le=MAX_SCORE_COUNT)
    score_count: int = Field(strict=True, ge=1, le=MAX_SCORE_COUNT)
    source_commit: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}([0-9a-f]{24})?$")]
    generation_id: Identifier
    generation_manifest_sha256: Sha256Hex
    serving_database_sha256: Sha256Hex
    vector_sidecar_sha256: Sha256Hex
    exact_row_corpus_sha256: Sha256Hex
    embedding_profile_id: EmbeddingProfileId
    embedding_model: Literal["qwen/qwen3-embedding-8b"]
    embedding_dimension: Literal[4096]
    exact: Literal[True]
    approximate: Literal[False]
    scoring_contract: Literal["cardrag.v5-exact-row-score.v1"]
    temporal_scope_policy: Literal["gold-query.v1"]
    runtime_document_aggregation_status: Literal["candidate_default", "sealed"]
    runtime_document_aggregation_policy: AggregationPolicy
    runtime_sealed_profile_sha256: Sha256Hex | None = None
    corpus_inventory: ArtifactBinding
    score_matrix: ArtifactBinding
    query_vector_matrix: ArtifactBinding
    byte_order: Literal["little-endian"]
    scalar_type: Literal["float32"]
    matrix_order: Literal["row-major"]
    validation_profile: Literal["release_grade", "fixture_only"]

    @model_validator(mode="after")
    def runtime_aggregation_identity_is_complete(self) -> ScoreArtifactManifest:
        if self.runtime_document_aggregation_status == "sealed":
            if self.runtime_sealed_profile_sha256 is None:
                raise ValueError("sealed runtime aggregation requires its profile SHA-256")
        elif (
            self.runtime_document_aggregation_policy != MAX_CHILD
            or self.runtime_sealed_profile_sha256 is not None
        ):
            raise ValueError("candidate-default runtime aggregation must be unsealed max_child")
        if (
            self.score_count != self.query_count * self.corpus_row_count
            or self.score_matrix.size_bytes != self.score_count * 4
            or self.query_vector_matrix.size_bytes != self.query_count * 4096 * 4
        ):
            raise ValueError("aggregation sidecar sizes do not match their declared shapes")
        return self


class QueryScoreCoverage(_StrictModel):
    schema_version: Literal["cardrag.document-aggregation-query-coverage.v2"]
    ordinal: int = Field(strict=True, ge=0, le=499)
    query_id: Identifier
    query_sha256: Sha256Hex
    expected_rows: int = Field(strict=True, ge=1, le=MAX_SCORE_COUNT)
    scored_rows: int = Field(strict=True, ge=1, le=MAX_SCORE_COUNT)
    active_contracts: int = Field(strict=True, ge=1, le=100_000)
    score_offset_bytes: int = Field(strict=True, ge=0, le=MAX_PORTABLE_ARTIFACT_BYTES)
    score_size_bytes: int = Field(strict=True, gt=0, le=MAX_PORTABLE_ARTIFACT_BYTES)
    score_count: int = Field(strict=True, ge=1, le=MAX_SCORE_COUNT)
    score_sha256: Sha256Hex
    query_vector_offset_bytes: int = Field(strict=True, ge=0, le=MAX_PORTABLE_ARTIFACT_BYTES)
    query_vector_size_bytes: int = Field(strict=True, gt=0, le=4096 * 4)
    query_vector_count: Literal[4096]
    query_vector_sha256: Sha256Hex

    @model_validator(mode="after")
    def segments_match_counts(self) -> Self:
        if (
            self.expected_rows != self.scored_rows
            or self.scored_rows != self.score_count
            or self.score_size_bytes != self.score_count * 4
            or self.query_vector_size_bytes != self.query_vector_count * 4
        ):
            raise ValueError("aggregation query coverage is incomplete")
        return self


class CorpusInventoryManifest(_StrictModel):
    schema_version: Literal["cardrag.document-aggregation-corpus-inventory.v1"]
    generation_id: Identifier
    serving_database_sha256: Sha256Hex
    vector_sidecar_sha256: Sha256Hex
    exact_row_corpus_sha256: Sha256Hex
    embedding_profile_id: EmbeddingProfileId
    corpus_row_count: int = Field(strict=True, ge=1, le=MAX_SCORE_COUNT)


class CorpusInventoryRow(_StrictModel):
    schema_version: Literal["cardrag.document-aggregation-corpus-row.v1"]
    ordinal: int = Field(strict=True, ge=0, le=MAX_SCORE_COUNT - 1)
    row_index: int = Field(strict=True, ge=0)
    contract_revision_id: Identifier
    node_id: Identifier
    view_type: ViewType
    input_sha256: Sha256Hex
    embedding_profile_id: EmbeddingProfileId


@dataclass(frozen=True, slots=True)
class AggregationProfileArtifact:
    payload: dict[str, Any]
    canonical_bytes: bytes
    sha256: str


@dataclass(frozen=True, slots=True)
class ParsedScoreArtifact:
    manifest: ScoreArtifactManifest
    manifest_sha256: str
    artifact_sha256: str
    size_bytes: int
    query_metrics: dict[AggregationPolicy, dict[str, dict[str, float | None]]]
    query_contract_counts: dict[str, int]


@dataclass(slots=True)
class _ContractAccumulator:
    contract_score: float | None = None
    child_scores: list[float] = field(default_factory=list)

    def add(self, row: CorpusInventoryRow, score: float) -> None:
        if row.view_type == "CONTRACT":
            if self.contract_score is not None:
                raise AggregationProfileError("duplicate_contract_view")
            self.contract_score = score
        else:
            self.child_scores.append(score)
            self.child_scores.sort(reverse=True)
            del self.child_scores[3:]

    def scores(self) -> dict[AggregationPolicy, float]:
        if self.contract_score is None:
            raise AggregationProfileError("contract_view_missing")
        if not self.child_scores:
            raise AggregationProfileError("child_view_missing")
        ordered = sorted(self.child_scores, reverse=True)
        best_child = ordered[0]
        top = ordered[:3]
        return {
            MAX_CHILD: best_child,
            TOP3_MEAN: math.fsum(top) / len(top),
            CONTRACT_PLUS_CHILD: 0.5 * self.contract_score + 0.5 * best_child,
        }


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _absolute_without_resolving(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


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


def _read_regular(path: Path, *, maximum_bytes: int, error_prefix: str) -> bytes:
    absolute = _absolute_without_resolving(path)
    try:
        listed = absolute.lstat()
    except FileNotFoundError:
        raise AggregationProfileError(f"{error_prefix}_missing") from None
    if stat.S_ISLNK(listed.st_mode) or not stat.S_ISREG(listed.st_mode):
        raise AggregationProfileError(f"{error_prefix}_not_regular")
    if listed.st_size <= 0 or listed.st_size > maximum_bytes:
        raise AggregationProfileError(f"{error_prefix}_size_invalid")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(absolute, flags)
    except OSError as exc:
        raise AggregationProfileError(f"{error_prefix}_open_failed") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise AggregationProfileError(f"{error_prefix}_not_regular")
        if before.st_size <= 0 or before.st_size > maximum_bytes:
            raise AggregationProfileError(f"{error_prefix}_size_invalid")
        if _identity(listed) != _identity(before):
            raise AggregationProfileError(f"{error_prefix}_changed_during_read")
        chunks: list[bytes] = []
        remaining = before.st_size
        size = 0
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise AggregationProfileError(f"{error_prefix}_changed_during_read")
            if len(chunk) > maximum_bytes - size:
                raise AggregationProfileError(f"{error_prefix}_size_invalid")
            chunks.append(chunk)
            size += len(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise AggregationProfileError(f"{error_prefix}_changed_during_read")
        after = os.fstat(descriptor)
        try:
            current = absolute.lstat()
        except OSError:
            raise AggregationProfileError(f"{error_prefix}_changed_during_read") from None
        identity = _identity(before)
        if size != before.st_size or identity != _identity(after) or identity != _identity(current):
            raise AggregationProfileError(f"{error_prefix}_changed_during_read")
    finally:
        os.close(descriptor)
    return b"".join(chunks)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AggregationProfileError("score_json_duplicate_key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> Any:
    raise AggregationProfileError("score_json_non_finite_number")


def _decode_canonical_line(raw: bytes, line: int) -> dict[str, Any]:
    if not raw.endswith(b"\n") or raw == b"\n" or len(raw) > MAX_SCORE_LINE_BYTES:
        raise AggregationProfileError("score_line_invalid", line=line)
    body = raw[:-1]
    try:
        value = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except AggregationProfileError as exc:
        raise AggregationProfileError(exc.code, line=line) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AggregationProfileError("score_line_invalid", line=line) from exc
    if not isinstance(value, dict):
        raise AggregationProfileError("score_record_not_object", line=line)
    record = cast(dict[str, Any], value)
    if body != _canonical_json_bytes(record):
        raise AggregationProfileError("score_not_canonical_bytes", line=line)
    return record


class _ScoreLineReader:
    def __init__(self, path: Path, *, code: str = "score_artifact") -> None:
        self._path = _absolute_without_resolving(path)
        self._code = code
        self._descriptor: int | None = None
        self._stream: Any = None
        self._listed: os.stat_result | None = None
        self._before: os.stat_result | None = None
        self._digest = hashlib.sha256()
        self.line = 0
        self.size_bytes = 0

    def __enter__(self) -> _ScoreLineReader:
        try:
            listed = self._path.lstat()
        except FileNotFoundError:
            raise AggregationProfileError(f"{self._code}_missing") from None
        if stat.S_ISLNK(listed.st_mode) or not stat.S_ISREG(listed.st_mode):
            raise AggregationProfileError(f"{self._code}_not_regular")
        if listed.st_size <= 0 or listed.st_size > MAX_SCORE_ARTIFACT_BYTES:
            raise AggregationProfileError(f"{self._code}_size_invalid")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            descriptor = os.open(self._path, flags)
        except OSError as exc:
            raise AggregationProfileError(f"{self._code}_open_failed") from exc
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise AggregationProfileError(f"{self._code}_not_regular")
            if before.st_size <= 0 or before.st_size > MAX_SCORE_ARTIFACT_BYTES:
                raise AggregationProfileError(f"{self._code}_size_invalid")
            if _identity(listed) != _identity(before):
                raise AggregationProfileError(f"{self._code}_changed_during_read")
            stream = os.fdopen(descriptor, "rb", closefd=False)
        except Exception:
            os.close(descriptor)
            raise
        self._descriptor = descriptor
        self._listed = listed
        self._before = before
        self._stream = stream
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        descriptor = self._descriptor
        stream = self._stream
        try:
            if stream is not None:
                stream.close()
            if descriptor is not None:
                after = os.fstat(descriptor)
                listed = self._listed
                before = self._before
                if listed is not None and before is not None:
                    try:
                        current_path = self._path.lstat()
                    except OSError:
                        raise AggregationProfileError(f"{self._code}_changed_during_read") from None
                    identity = _identity(before)
                    if (
                        identity != _identity(listed)
                        or identity != _identity(after)
                        or identity != _identity(current_path)
                        or (exc is None and self.size_bytes != before.st_size)
                    ):
                        raise AggregationProfileError(f"{self._code}_changed_during_read")
        finally:
            if descriptor is not None:
                os.close(descriptor)
            self._descriptor = None
            self._stream = None

    def next_record(self) -> dict[str, Any] | None:
        if self._stream is None:
            raise RuntimeError("score reader is not open")
        raw = cast(bytes, self._stream.readline(MAX_SCORE_LINE_BYTES + 1))
        if not raw:
            return None
        if len(raw) > MAX_SCORE_ARTIFACT_BYTES - self.size_bytes:
            raise AggregationProfileError(f"{self._code}_size_invalid", line=self.line + 1)
        self.line += 1
        self.size_bytes += len(raw)
        self._digest.update(raw)
        return _decode_canonical_line(raw, self.line)

    @property
    def sha256(self) -> str:
        return self._digest.hexdigest()


class _MappedArtifact:
    """Hash-pin one bounded regular file and expose read-only zero-copy segments."""

    def __init__(self, path: Path, binding: ArtifactBinding, *, code: str) -> None:
        self.path = _absolute_without_resolving(path)
        self.binding = binding
        self.code = code
        self._descriptor: int | None = None
        self._mapping: mmap.mmap | None = None
        self._listed: os.stat_result | None = None
        self._before: os.stat_result | None = None

    def __enter__(self) -> _MappedArtifact:
        try:
            listed = self.path.lstat()
        except FileNotFoundError:
            raise AggregationProfileError(f"{self.code}_missing") from None
        if stat.S_ISLNK(listed.st_mode) or not stat.S_ISREG(listed.st_mode):
            raise AggregationProfileError(f"{self.code}_not_regular")
        if listed.st_size != self.binding.size_bytes:
            raise AggregationProfileError(f"{self.code}_size_mismatch")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            descriptor = os.open(self.path, flags)
        except OSError as exc:
            raise AggregationProfileError(f"{self.code}_open_failed") from exc
        mapping: mmap.mmap | None = None
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_size != self.binding.size_bytes
                or _identity(listed) != _identity(before)
            ):
                raise AggregationProfileError(f"{self.code}_changed_during_open")
            digest = hashlib.sha256()
            position = 0
            while position < before.st_size:
                block = os.pread(
                    descriptor,
                    min(1024 * 1024, before.st_size - position),
                    position,
                )
                if not block:
                    raise AggregationProfileError(f"{self.code}_changed_during_read")
                digest.update(block)
                position += len(block)
            after_hash = os.fstat(descriptor)
            try:
                current_path = self.path.lstat()
            except OSError:
                raise AggregationProfileError(f"{self.code}_changed_during_read") from None
            if _identity(before) != _identity(after_hash) or _identity(before) != _identity(
                current_path
            ):
                raise AggregationProfileError(f"{self.code}_changed_during_read")
            if digest.hexdigest() != self.binding.sha256:
                raise AggregationProfileError(f"{self.code}_sha256_mismatch")
            mapping = mmap.mmap(descriptor, before.st_size, access=mmap.ACCESS_READ)
        except BaseException:
            self._mapping = None
            self._descriptor = None
            self._listed = None
            self._before = None
            try:
                if mapping is not None:
                    mapping.close()
            finally:
                os.close(descriptor)
            raise
        self._descriptor = descriptor
        self._listed = listed
        self._before = before
        self._mapping = mapping
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        mapping = self._mapping
        descriptor = self._descriptor
        self._mapping = None
        self._descriptor = None
        try:
            if mapping is not None:
                mapping.close()
            if descriptor is not None:
                before = self._before
                listed = self._listed
                if before is not None and listed is not None:
                    try:
                        current_path = self.path.lstat()
                    except OSError:
                        raise AggregationProfileError(f"{self.code}_changed_during_read") from None
                    identity = _identity(before)
                    if (
                        identity != _identity(listed)
                        or identity != _identity(os.fstat(descriptor))
                        or identity != _identity(current_path)
                    ):
                        raise AggregationProfileError(f"{self.code}_changed_during_read")
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def array(self, *, offset: int, count: int, sha256: str) -> npt.NDArray[np.float32]:
        mapping = self._mapping
        if mapping is None:
            raise RuntimeError("mapped aggregation artifact is not open")
        size = count * 4
        if offset < 0 or size < 1 or offset + size > self.binding.size_bytes:
            raise AggregationProfileError(f"{self.code}_segment_range_invalid")
        view = memoryview(mapping)[offset : offset + size]
        try:
            if hashlib.sha256(view).hexdigest() != sha256:
                raise AggregationProfileError(f"{self.code}_segment_sha256_mismatch")
        finally:
            view.release()
        result = np.frombuffer(mapping, dtype="<f4", count=count, offset=offset)
        result.flags.writeable = False
        return result


@dataclass(frozen=True, slots=True)
class OpenedScoreArtifact:
    """A verified compact score artifact whose arrays live for the open context."""

    manifest: ScoreArtifactManifest
    score_artifact_binding: ArtifactBinding
    corpus_inventory_binding: ArtifactBinding
    score_matrix_binding: ArtifactBinding
    query_vector_matrix_binding: ArtifactBinding
    inventory_manifest: CorpusInventoryManifest
    inventory: tuple[CorpusInventoryRow, ...]
    coverages: tuple[QueryScoreCoverage, ...]
    _score_matrix: _MappedArtifact
    _query_vector_matrix: _MappedArtifact

    def scores_for(self, ordinal: int) -> npt.NDArray[np.float32]:
        if type(ordinal) is not int:
            raise AggregationProfileError("score_query_ordinal_invalid")
        try:
            coverage = self.coverages[ordinal]
        except IndexError as exc:
            raise AggregationProfileError("score_query_ordinal_invalid") from exc
        if ordinal < 0 or coverage.ordinal != ordinal:
            raise AggregationProfileError("score_query_ordinal_invalid")
        return self._score_matrix.array(
            offset=coverage.score_offset_bytes,
            count=coverage.score_count,
            sha256=coverage.score_sha256,
        )

    def query_vector_for(self, ordinal: int) -> npt.NDArray[np.float32]:
        if type(ordinal) is not int:
            raise AggregationProfileError("score_query_ordinal_invalid")
        try:
            coverage = self.coverages[ordinal]
        except IndexError as exc:
            raise AggregationProfileError("score_query_ordinal_invalid") from exc
        if ordinal < 0 or coverage.ordinal != ordinal:
            raise AggregationProfileError("score_query_ordinal_invalid")
        return self._query_vector_matrix.array(
            offset=coverage.query_vector_offset_bytes,
            count=coverage.query_vector_count,
            sha256=coverage.query_vector_sha256,
        )


def _artifact_binding(reader: _ScoreLineReader) -> ArtifactBinding:
    return ArtifactBinding(sha256=reader.sha256, size_bytes=reader.size_bytes)


def _load_inventory(
    reader: _ScoreLineReader,
    *,
    expected: ArtifactBinding,
    score_manifest: ScoreArtifactManifest,
) -> tuple[CorpusInventoryManifest, tuple[CorpusInventoryRow, ...], ArtifactBinding]:
    raw_manifest = reader.next_record()
    if raw_manifest is None:
        raise AggregationProfileError("corpus_inventory_empty")
    try:
        manifest = CorpusInventoryManifest.model_validate(raw_manifest)
    except ValidationError as exc:
        raise AggregationProfileError("corpus_inventory_manifest_invalid", line=1) from exc
    rows: list[CorpusInventoryRow] = []
    previous_row_index = -1
    seen_views: set[tuple[str, str, ViewType]] = set()
    for ordinal in range(manifest.corpus_row_count):
        raw_row = reader.next_record()
        if raw_row is None:
            raise AggregationProfileError("corpus_inventory_row_missing", line=reader.line + 1)
        try:
            row = CorpusInventoryRow.model_validate(raw_row)
        except ValidationError as exc:
            raise AggregationProfileError("corpus_inventory_row_invalid", line=reader.line) from exc
        view_key = (row.contract_revision_id, row.node_id, row.view_type)
        if (
            row.ordinal != ordinal
            or row.row_index <= previous_row_index
            or row.embedding_profile_id != score_manifest.embedding_profile_id
            or view_key in seen_views
        ):
            raise AggregationProfileError("corpus_inventory_row_order_invalid", line=reader.line)
        rows.append(row)
        previous_row_index = row.row_index
        seen_views.add(view_key)
    if reader.next_record() is not None:
        raise AggregationProfileError("corpus_inventory_trailing_record", line=reader.line)
    binding = _artifact_binding(reader)
    if binding != expected:
        raise AggregationProfileError("corpus_inventory_binding_mismatch")
    if (
        manifest.generation_id != score_manifest.generation_id
        or manifest.serving_database_sha256 != score_manifest.serving_database_sha256
        or manifest.vector_sidecar_sha256 != score_manifest.vector_sidecar_sha256
        or manifest.exact_row_corpus_sha256 != score_manifest.exact_row_corpus_sha256
        or manifest.embedding_profile_id != score_manifest.embedding_profile_id
        or manifest.corpus_row_count != score_manifest.corpus_row_count
    ):
        raise AggregationProfileError("corpus_inventory_manifest_binding_mismatch")
    return manifest, tuple(rows), binding


@contextmanager
def open_score_artifact(
    score_artifact_path: Path,
    corpus_inventory_path: Path,
    score_matrix_path: Path,
    query_vector_matrix_path: Path,
    expected_score_artifact_sha256: str | None = None,
) -> Iterator[OpenedScoreArtifact]:
    """Pin and verify compact v2 evidence for the lifetime of this context.

    Arrays returned by ``scores_for`` and ``query_vector_for`` are zero-copy
    read-only mmap views and must not outlive the context.
    """

    if (
        expected_score_artifact_sha256 is not None
        and _SHA256.fullmatch(expected_score_artifact_sha256) is None
    ):
        raise AggregationProfileError("expected_score_artifact_sha256_invalid")
    with ExitStack() as stack:
        reader = stack.enter_context(_ScoreLineReader(score_artifact_path))
        raw_manifest = reader.next_record()
        if raw_manifest is None:
            raise AggregationProfileError("score_artifact_empty")
        try:
            manifest = ScoreArtifactManifest.model_validate(raw_manifest)
        except ValidationError as exc:
            raise AggregationProfileError("score_manifest_invalid", line=1) from exc
        coverages: list[QueryScoreCoverage] = []
        score_offset = 0
        vector_offset = 0
        for ordinal in range(manifest.query_count):
            raw_coverage = reader.next_record()
            if raw_coverage is None:
                raise AggregationProfileError("score_query_coverage_missing", line=reader.line + 1)
            try:
                coverage = QueryScoreCoverage.model_validate(raw_coverage)
            except ValidationError as exc:
                raise AggregationProfileError(
                    "score_query_coverage_invalid", line=reader.line
                ) from exc
            if (
                coverage.ordinal != ordinal
                or coverage.expected_rows != manifest.corpus_row_count
                or coverage.score_offset_bytes != score_offset
                or coverage.query_vector_offset_bytes != vector_offset
            ):
                raise AggregationProfileError(
                    "score_query_coverage_order_invalid", line=reader.line
                )
            coverages.append(coverage)
            score_offset += coverage.score_size_bytes
            vector_offset += coverage.query_vector_size_bytes
        if reader.next_record() is not None:
            raise AggregationProfileError("score_artifact_trailing_record", line=reader.line)
        score_artifact_binding = _artifact_binding(reader)
        query_ids = tuple(item.query_id for item in coverages)
        if len(query_ids) != len(set(query_ids)):
            raise AggregationProfileError("score_query_id_duplicate")
        if (
            expected_score_artifact_sha256 is not None
            and score_artifact_binding.sha256 != expected_score_artifact_sha256
        ):
            raise AggregationProfileError("score_artifact_sha256_mismatch")
        if (
            score_offset != manifest.score_matrix.size_bytes
            or vector_offset != manifest.query_vector_matrix.size_bytes
            or sum(item.score_count for item in coverages) != manifest.score_count
        ):
            raise AggregationProfileError("score_sidecar_coverage_incomplete")
        inventory_reader = stack.enter_context(
            _ScoreLineReader(corpus_inventory_path, code="corpus_inventory")
        )
        inventory_manifest, inventory, inventory_binding = _load_inventory(
            inventory_reader,
            expected=manifest.corpus_inventory,
            score_manifest=manifest,
        )
        score_matrix = stack.enter_context(
            _MappedArtifact(score_matrix_path, manifest.score_matrix, code="score_matrix")
        )
        query_vector_matrix = stack.enter_context(
            _MappedArtifact(
                query_vector_matrix_path,
                manifest.query_vector_matrix,
                code="query_vector_matrix",
            )
        )
        opened = OpenedScoreArtifact(
            manifest=manifest,
            score_artifact_binding=score_artifact_binding,
            corpus_inventory_binding=inventory_binding,
            score_matrix_binding=manifest.score_matrix,
            query_vector_matrix_binding=manifest.query_vector_matrix,
            inventory_manifest=inventory_manifest,
            inventory=inventory,
            coverages=tuple(coverages),
            _score_matrix=score_matrix,
            _query_vector_matrix=query_vector_matrix,
        )
        for ordinal in range(manifest.query_count):
            scores = opened.scores_for(ordinal)
            if any(
                not math.isfinite(float(value)) or not -1.0 <= float(value) <= 1.0
                for value in scores
            ):
                del scores
                raise AggregationProfileError("score_matrix_value_invalid")
            del scores
            vector = opened.query_vector_for(ordinal)
            if any(not math.isfinite(float(value)) for value in vector):
                del vector
                raise AggregationProfileError("query_vector_value_invalid")
            norm = float(np.linalg.norm(vector))
            del vector
            if not math.isfinite(norm) or not math.isclose(norm, 1.0, rel_tol=1e-5, abs_tol=1e-5):
                raise AggregationProfileError("query_vector_norm_invalid")
        yield opened


def _query_sha256(query: GoldQuery) -> str:
    return hashlib.sha256(query.question.encode("utf-8")).hexdigest()


def _ranking_metrics(
    query: GoldQuery,
    ranked_contracts: Sequence[str],
) -> dict[str, float | None]:
    if not query.contracts:
        return {metric: None for metric in METRICS}
    relevance = {item.contract_revision_id: item.relevance for item in query.contracts}
    expected = set(relevance)
    top10 = ranked_contracts[:10]
    recall = len(set(top10).intersection(expected)) / len(expected)
    dcg = math.fsum(
        (2 ** relevance.get(contract_id, 0) - 1) / math.log2(rank + 1)
        for rank, contract_id in enumerate(top10, start=1)
    )
    ideal = sorted(relevance.values(), reverse=True)[:10]
    idcg = math.fsum(
        (2**grade - 1) / math.log2(rank + 1) for rank, grade in enumerate(ideal, start=1)
    )
    first_relevant = next(
        (rank for rank, contract_id in enumerate(top10, start=1) if contract_id in expected),
        None,
    )
    return {
        "contract_recall_at_10": recall,
        "ndcg_at_10": dcg / idcg,
        "mrr_at_10": 0.0 if first_relevant is None else 1.0 / first_relevant,
    }


def _parse_scores(
    path: Path,
    corpus_inventory_path: Path,
    score_matrix_path: Path,
    query_vector_matrix_path: Path,
    gold: GoldDataset,
    *,
    release_gate: bool,
) -> ParsedScoreArtifact:
    query_metrics: dict[AggregationPolicy, dict[str, dict[str, float | None]]] = {
        policy: {} for policy in POLICIES
    }
    query_contract_counts: dict[str, int] = {}
    with open_score_artifact(
        path,
        corpus_inventory_path,
        score_matrix_path,
        query_vector_matrix_path,
    ) as opened:
        manifest = opened.manifest
        if manifest.gold_sha256 != gold.sha256:
            raise AggregationProfileError("score_gold_sha256_mismatch", line=1)
        if manifest.query_count != len(gold.queries):
            raise AggregationProfileError("score_query_count_mismatch", line=1)
        if release_gate and manifest.validation_profile != "release_grade":
            raise AggregationProfileError("score_validation_profile_not_release_grade", line=1)
        manifest_sha256 = hashlib.sha256(
            _canonical_json_bytes(manifest.model_dump(mode="json"))
        ).hexdigest()

        for query_index, query in enumerate(gold.queries):
            coverage = opened.coverages[query_index]
            if coverage.query_id != query.query_id or coverage.query_sha256 != _query_sha256(query):
                raise AggregationProfileError("score_query_binding_mismatch", line=query_index + 2)

            contracts: dict[str, _ContractAccumulator] = {}
            scores = opened.scores_for(query_index)
            try:
                for row, raw_score in zip(opened.inventory, scores, strict=True):
                    contracts.setdefault(row.contract_revision_id, _ContractAccumulator()).add(
                        row, float(raw_score)
                    )
            finally:
                del scores

            if len(contracts) != coverage.active_contracts:
                raise AggregationProfileError(
                    "score_active_contract_count_mismatch", line=query_index + 2
                )
            policy_scores: dict[AggregationPolicy, dict[str, float]] = {
                policy: {} for policy in POLICIES
            }
            for contract_revision_id, accumulator in contracts.items():
                for policy, score in accumulator.scores().items():
                    policy_scores[policy][contract_revision_id] = score
            for policy in POLICIES:
                ranked = tuple(
                    sorted(
                        policy_scores[policy],
                        key=lambda contract_revision_id: (
                            -policy_scores[policy][contract_revision_id],
                            contract_revision_id,
                        ),
                    )
                )
                query_metrics[policy][query.query_id] = _ranking_metrics(query, ranked)
            query_contract_counts[query.query_id] = len(contracts)
        artifact_sha256 = opened.score_artifact_binding.sha256
        size_bytes = opened.score_artifact_binding.size_bytes

    return ParsedScoreArtifact(
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        artifact_sha256=artifact_sha256,
        size_bytes=size_bytes,
        query_metrics=query_metrics,
        query_contract_counts=query_contract_counts,
    )


def _derived_seed(seed: int, context: str) -> int:
    digest = hashlib.sha256(f"{seed}:{context}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def _rounded(value: float) -> float:
    return round(float(value), 12)


def _bootstrap_indexes(
    query_count: int,
    *,
    bootstrap_samples: int,
    seed: int,
    context: str,
) -> npt.NDArray[np.int64]:
    generator = np.random.Generator(np.random.PCG64(_derived_seed(seed, context)))
    return generator.integers(
        0,
        query_count,
        size=(bootstrap_samples, query_count),
        dtype=np.int64,
    )


def _summary(
    values: Sequence[float | None],
    *,
    bootstrap_samples: int,
    seed: int,
    context: str,
) -> dict[str, Any] | None:
    eligible_values = np.array([value for value in values if value is not None], dtype=np.float64)
    if not len(eligible_values):
        return None
    indexes = _bootstrap_indexes(
        len(eligible_values),
        bootstrap_samples=bootstrap_samples,
        seed=seed,
        context=context,
    )
    replicates = np.mean(eligible_values[indexes], axis=1)
    low, high = np.quantile(replicates, [0.025, 0.975], method="linear")
    return {
        "ci95": {"high": _rounded(float(high)), "low": _rounded(float(low))},
        "eligible_queries": len(eligible_values),
        "value": _rounded(float(np.mean(eligible_values))),
    }


def _metric_summaries(
    query_ids: Sequence[str],
    contributions: Mapping[str, Mapping[str, float | None]],
    *,
    bootstrap_samples: int,
    seed: int,
    context: str,
) -> dict[str, Any]:
    return {
        metric: _summary(
            [contributions[query_id][metric] for query_id in query_ids],
            bootstrap_samples=bootstrap_samples,
            seed=seed,
            context=f"{context}:{metric}",
        )
        for metric in METRICS
    }


def _comparison_summaries(
    query_ids: Sequence[str],
    candidate: Mapping[str, Mapping[str, float | None]],
    reference: Mapping[str, Mapping[str, float | None]],
    *,
    bootstrap_samples: int,
    seed: int,
    context: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for metric in METRICS:
        deltas: list[float | None] = []
        for query_id in query_ids:
            candidate_value = candidate[query_id][metric]
            reference_value = reference[query_id][metric]
            deltas.append(
                None
                if candidate_value is None or reference_value is None
                else candidate_value - reference_value
            )
        result[metric] = _summary(
            deltas,
            bootstrap_samples=bootstrap_samples,
            seed=seed,
            context=f"{context}:{metric}",
        )
    return result


def _slice_query_ids(queries: Sequence[GoldQuery]) -> dict[str, tuple[str, ...]]:
    names = sorted({name for query in queries for name in query.slices})
    return {
        name: tuple(query.query_id for query in queries if name in query.slices) for name in names
    }


def _definitions() -> dict[str, Any]:
    return {
        MAX_CHILD: {
            "child_view_types": [
                "CONTEXTUAL_ITEM",
                "DETAIL",
                "MAJOR_SECTION",
                "RAW_ITEM",
                "TITLE",
            ],
            "formula": "max(non-CONTRACT row score)",
        },
        TOP3_MEAN: {
            "child_count": 3,
            "formula": "mean(highest min(3, available) non-CONTRACT row scores)",
        },
        CONTRACT_PLUS_CHILD: {
            "child_policy": MAX_CHILD,
            "child_weight": 0.5,
            "contract_view_policy": "single CONTRACT row score",
            "contract_weight": 0.5,
            "formula": "0.5*CONTRACT + 0.5*max_child",
        },
    }


def _select_winner(comparisons: Mapping[str, Mapping[str, Any]]) -> AggregationPolicy | None:
    winners: list[AggregationPolicy] = []
    for candidate in POLICIES:
        significantly_better = True
        for reference in POLICIES:
            if candidate == reference:
                continue
            summary = comparisons[f"{candidate}_vs_{reference}"]["overall"][SELECTION_OBJECTIVE]
            if summary is None or float(summary["ci95"]["low"]) <= 0:
                significantly_better = False
                break
        if significantly_better:
            winners.append(candidate)
    return winners[0] if len(winners) == 1 else None


def _release_gate(
    winner: AggregationPolicy | None,
    comparisons: Mapping[str, Mapping[str, Any]],
    policies: Mapping[str, Mapping[str, Any]],
    slices: Mapping[str, Sequence[str]],
    *,
    release_gate: bool,
) -> dict[str, Any]:
    if not release_gate:
        return {"evaluated": False, "failure_reasons": [], "status": "not_evaluated"}
    reasons: list[str] = []
    if winner is None:
        reasons.append("no_unique_significant_winner")
    missing_slices = sorted(REQUIRED_AGGREGATION_SLICES - set(slices))
    reasons.extend(f"aggregation_slice_missing:{name}" for name in missing_slices)
    max_child_slices = cast(Mapping[str, Mapping[str, Any]], policies[MAX_CHILD]["slices"])
    for slice_name in sorted(REQUIRED_AGGREGATION_SLICES.intersection(slices)):
        slice_metrics = max_child_slices[slice_name]
        for metric in METRICS:
            if slice_metrics.get(metric) is None:
                reasons.append(f"slice_has_no_retrieval_labels:{slice_name}:{metric}")
    if winner is not None and winner != MAX_CHILD:
        comparison = comparisons[f"{winner}_vs_{MAX_CHILD}"]
        for metric in METRICS:
            summary = comparison["overall"].get(metric)
            if summary is None or float(summary["ci95"]["low"]) < 0:
                reasons.append(f"overall_regression_not_excluded:{metric}")
        for slice_name in sorted(REQUIRED_AGGREGATION_SLICES):
            slice_metrics = comparison["slices"].get(slice_name)
            if slice_metrics is None:
                continue
            for metric in METRICS:
                summary = slice_metrics.get(metric)
                if summary is None:
                    reasons.append(f"slice_metric_missing:{slice_name}:{metric}")
                elif float(summary["ci95"]["low"]) < 0:
                    reasons.append(f"slice_regression_not_excluded:{slice_name}:{metric}")
    return {
        "evaluated": True,
        "failure_reasons": reasons,
        "status": "passed" if not reasons else "failed",
    }


def build_aggregation_profile(
    gold_path: Path,
    score_artifact_path: Path,
    corpus_inventory_path: Path,
    score_matrix_path: Path,
    query_vector_matrix_path: Path,
    *,
    release_gate: bool = True,
    expected_gold_sha256: str | None = None,
    expected_source_commit: str | None = None,
    bootstrap_samples: int = 2_000,
    bootstrap_seed: int = 1010,
) -> AggregationProfileArtifact:
    """Recompute the three policies and return a canonical, hash-bound artifact."""

    if bootstrap_samples < MIN_BOOTSTRAP_SAMPLES:
        raise AggregationProfileError("bootstrap_samples_too_small")
    if bootstrap_samples > MAX_BOOTSTRAP_SAMPLES:
        raise AggregationProfileError("bootstrap_samples_too_large")
    if release_gate and bootstrap_samples < MIN_RELEASE_BOOTSTRAP_SAMPLES:
        raise AggregationProfileError("release_bootstrap_samples_too_small")
    try:
        gold = load_gold_jsonl(gold_path, release_gate=release_gate)
    except EvaluationError as exc:
        raise AggregationProfileError(f"gold_{exc.code}", line=exc.line) from exc
    if expected_gold_sha256 is not None:
        if not _SHA256.fullmatch(expected_gold_sha256) or expected_gold_sha256 != gold.sha256:
            raise AggregationProfileError("gold_sha256_mismatch")
    elif release_gate:
        raise AggregationProfileError("sealed_gold_sha256_required")
    candidate_source_commit = _validated_expected_source_commit(
        expected_source_commit,
        release_gate=release_gate,
    )

    scores = _parse_scores(
        score_artifact_path,
        corpus_inventory_path,
        score_matrix_path,
        query_vector_matrix_path,
        gold,
        release_gate=release_gate,
    )
    if (
        candidate_source_commit is not None
        and scores.manifest.source_commit != candidate_source_commit
    ):
        raise AggregationProfileError("candidate_source_commit_mismatch")
    query_ids = tuple(query.query_id for query in gold.queries)
    slice_ids = _slice_query_ids(gold.queries)
    policy_payloads: dict[str, Any] = {}
    for policy in POLICIES:
        policy_payloads[policy] = {
            "overall": _metric_summaries(
                query_ids,
                scores.query_metrics[policy],
                bootstrap_samples=bootstrap_samples,
                seed=bootstrap_seed,
                context=f"policy:{policy}:overall",
            ),
            "slices": {
                slice_name: _metric_summaries(
                    ids,
                    scores.query_metrics[policy],
                    bootstrap_samples=bootstrap_samples,
                    seed=bootstrap_seed,
                    context=f"policy:{policy}:slice:{slice_name}",
                )
                for slice_name, ids in slice_ids.items()
            },
        }

    comparisons: dict[str, Any] = {}
    for candidate in POLICIES:
        for reference in POLICIES:
            if candidate == reference:
                continue
            name = f"{candidate}_vs_{reference}"
            comparisons[name] = {
                "candidate_policy": candidate,
                "overall": _comparison_summaries(
                    query_ids,
                    scores.query_metrics[candidate],
                    scores.query_metrics[reference],
                    bootstrap_samples=bootstrap_samples,
                    seed=bootstrap_seed,
                    context=f"comparison:{name}:overall",
                ),
                "reference_policy": reference,
                "slices": {
                    slice_name: _comparison_summaries(
                        ids,
                        scores.query_metrics[candidate],
                        scores.query_metrics[reference],
                        bootstrap_samples=bootstrap_samples,
                        seed=bootstrap_seed,
                        context=f"comparison:{name}:slice:{slice_name}",
                    )
                    for slice_name, ids in slice_ids.items()
                },
            }

    winner = _select_winner(comparisons)
    gate = _release_gate(
        winner,
        comparisons,
        policy_payloads,
        slice_ids,
        release_gate=release_gate,
    )
    sealed_profile: dict[str, Any] | None = None
    sealed_profile_sha256: str | None = None
    if winner is not None and gate["status"] == "passed":
        sealed_profile_payload = {
            "aggregation_definition": _definitions()[winner],
            "aggregation_policy": winner,
            "bootstrap": {
                "ci": 0.95,
                "method": "paired-query-percentile-pcg64",
                "samples": bootstrap_samples,
                "seed": bootstrap_seed,
            },
            "embedding_profile_id": scores.manifest.embedding_profile_id,
            "exact_row_corpus_sha256": scores.manifest.exact_row_corpus_sha256,
            "generation_id": scores.manifest.generation_id,
            "generation_manifest_sha256": scores.manifest.generation_manifest_sha256,
            "gold_sha256": gold.sha256,
            "profile_id": f"cardrag.document-aggregation.{winner.replace('_', '-')}.v1",
            "schema_version": SEALED_PROFILE_SCHEMA_VERSION,
            "score_artifact_sha256": scores.artifact_sha256,
            "selection_objective": SELECTION_OBJECTIVE,
        }
        try:
            validated_profile = DocumentAggregationProfile.model_validate(sealed_profile_payload)
        except ValidationError as exc:  # pragma: no cover - definitions are module constants
            raise AggregationProfileError("sealed_profile_contract_invalid") from exc
        sealed_profile = validated_profile.model_dump(mode="json")
        sealed_profile_sha256 = validated_profile.profile_sha256

    manifest_payload = scores.manifest.model_dump(mode="json")
    payload: dict[str, Any] = {
        "artifact_bindings": {
            "corpus_inventory_sha256": scores.manifest.corpus_inventory.sha256,
            "corpus_inventory_size_bytes": scores.manifest.corpus_inventory.size_bytes,
            "generation_manifest_sha256": scores.manifest.generation_manifest_sha256,
            "gold_sha256": gold.sha256,
            "query_vector_matrix_sha256": scores.manifest.query_vector_matrix.sha256,
            "query_vector_matrix_size_bytes": scores.manifest.query_vector_matrix.size_bytes,
            "score_artifact_manifest_sha256": scores.manifest_sha256,
            "score_artifact_sha256": scores.artifact_sha256,
            "score_artifact_size_bytes": scores.size_bytes,
            "score_matrix_sha256": scores.manifest.score_matrix.sha256,
            "score_matrix_size_bytes": scores.manifest.score_matrix.size_bytes,
        },
        "bootstrap": {
            "ci": 0.95,
            "method": "paired-query-percentile-pcg64",
            "samples": bootstrap_samples,
            "seed": bootstrap_seed,
        },
        "comparisons": comparisons,
        "coverage": {
            "all_queries_exact": True,
            "approximate": False,
            "maximum_active_contracts": max(scores.query_contract_counts.values()),
            "minimum_active_contracts": min(scores.query_contract_counts.values()),
            "query_count": len(query_ids),
            "corpus_row_count": scores.manifest.corpus_row_count,
            "score_count": scores.manifest.score_count,
        },
        "definitions": _definitions(),
        "excluded_nonretrieval_slices": sorted(NON_RETRIEVAL_SLICES),
        "policies": policy_payloads,
        "release_gate": gate,
        "schema_version": PROFILE_ARTIFACT_SCHEMA_VERSION,
        "score_artifact_manifest": manifest_payload,
        "sealed_profile": sealed_profile,
        "sealed_profile_sha256": sealed_profile_sha256,
        "selection": {
            "objective": SELECTION_OBJECTIVE,
            "rule": "unique policy with paired CI95 lower bound > 0 against every alternative",
            "winner": winner,
        },
    }
    canonical = _canonical_json_bytes(payload)
    return AggregationProfileArtifact(payload, canonical, hashlib.sha256(canonical).hexdigest())


def _read_canonical_profile(path: Path) -> tuple[dict[str, Any], bytes, str]:
    data = _read_regular(path, maximum_bytes=MAX_PROFILE_BYTES, error_prefix="profile_artifact")
    if not data.endswith(b"\n") or data.endswith(b"\n\n"):
        raise AggregationProfileError("profile_not_canonical_bytes")
    body = data[:-1]
    try:
        value = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (AggregationProfileError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AggregationProfileError("profile_not_canonical_bytes") from exc
    if not isinstance(value, dict):
        raise AggregationProfileError("profile_not_canonical_bytes")
    payload = cast(dict[str, Any], value)
    if body != _canonical_json_bytes(payload):
        raise AggregationProfileError("profile_not_canonical_bytes")
    return payload, data, hashlib.sha256(data).hexdigest()


def _validate_generation_manifest(expected_sha256: str, directory: Path) -> None:
    absolute = _absolute_without_resolving(directory)
    try:
        listed = absolute.lstat()
    except FileNotFoundError:
        raise AggregationProfileError("generation_manifest_directory_missing") from None
    if stat.S_ISLNK(listed.st_mode) or not stat.S_ISDIR(listed.st_mode):
        raise AggregationProfileError("generation_manifest_directory_invalid")
    data = _read_regular(
        absolute / f"{expected_sha256}.json",
        maximum_bytes=MAX_PROFILE_BYTES,
        error_prefix="generation_manifest",
    )
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise AggregationProfileError("generation_manifest_sha256_mismatch")


def _validate_serving_generation_manifest(
    path: Path,
    *,
    sealed_profile: dict[str, Any],
    sealed_profile_sha256: str,
) -> None:
    data = _read_regular(
        path,
        maximum_bytes=MAX_PROFILE_BYTES,
        error_prefix="serving_generation_manifest",
    )
    try:
        manifest = GenerationManifest.model_validate_json(data)
    except ValidationError as exc:
        raise AggregationProfileError("serving_generation_manifest_invalid") from exc
    if data != manifest.canonical_bytes():
        raise AggregationProfileError("serving_generation_manifest_not_canonical")
    if manifest.schema_version != "cardrag.generation.v5":
        raise AggregationProfileError("serving_generation_manifest_not_v5")
    try:
        expected_profile = DocumentAggregationProfile.model_validate(sealed_profile)
    except ValidationError as exc:  # already validated while rebuilding
        raise AggregationProfileError("sealed_profile_contract_invalid") from exc
    if (
        manifest.generation_id == expected_profile.generation_id
        or manifest.document_aggregation_profile != expected_profile
        or manifest.document_aggregation_policy != expected_profile.aggregation_policy
        or manifest.sealed_profile_sha256 != sealed_profile_sha256
        or manifest.exact_row_corpus_sha256 != expected_profile.exact_row_corpus_sha256
    ):
        raise AggregationProfileError("serving_generation_aggregation_binding_mismatch")


def validate_aggregation_profile(
    profile_path: Path,
    gold_path: Path,
    score_artifact_path: Path,
    corpus_inventory_path: Path,
    score_matrix_path: Path,
    query_vector_matrix_path: Path,
    generation_manifest_directory: Path,
    *,
    expected_profile_sha256: str,
    expected_source_commit: str | None = None,
    serving_generation_manifest_path: Path | None = None,
    bootstrap_samples: int = 2_000,
    bootstrap_seed: int = 1010,
) -> AggregationProfileArtifact:
    """Recompute and byte-compare a release profile and all immutable inputs."""

    if not _SHA256.fullmatch(expected_profile_sha256):
        raise AggregationProfileError("profile_sha256_invalid")
    _payload, profile_bytes, profile_sha256 = _read_canonical_profile(profile_path)
    if profile_sha256 != expected_profile_sha256:
        raise AggregationProfileError("profile_sha256_mismatch")
    gold_bytes = _read_regular(gold_path, maximum_bytes=256 * 1024 * 1024, error_prefix="gold")
    expected = build_aggregation_profile(
        gold_path,
        score_artifact_path,
        corpus_inventory_path,
        score_matrix_path,
        query_vector_matrix_path,
        release_gate=True,
        expected_gold_sha256=hashlib.sha256(gold_bytes).hexdigest(),
        expected_source_commit=expected_source_commit,
        bootstrap_samples=bootstrap_samples,
        bootstrap_seed=bootstrap_seed,
    )
    if profile_bytes != expected.canonical_bytes + b"\n":
        raise AggregationProfileError("profile_recomputation_mismatch")
    if expected.payload["release_gate"]["status"] != "passed":
        raise AggregationProfileError("aggregation_release_gate_not_passed")
    sealed_profile = expected.payload["sealed_profile"]
    sealed_sha256 = expected.payload["sealed_profile_sha256"]
    if not isinstance(sealed_profile, dict) or not isinstance(sealed_sha256, str):
        raise AggregationProfileError("sealed_profile_missing")
    if hashlib.sha256(_canonical_json_bytes(sealed_profile)).hexdigest() != sealed_sha256:
        raise AggregationProfileError("sealed_profile_sha256_mismatch")
    generation_sha256 = cast(
        str, expected.payload["artifact_bindings"]["generation_manifest_sha256"]
    )
    _validate_generation_manifest(generation_sha256, generation_manifest_directory)
    if serving_generation_manifest_path is not None:
        _validate_serving_generation_manifest(
            serving_generation_manifest_path,
            sealed_profile=sealed_profile,
            sealed_profile_sha256=sealed_sha256,
        )
    return expected


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Select and seal CardRAG document aggregation")
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--scores", type=Path, required=True)
    parser.add_argument("--corpus-inventory", type=Path, required=True)
    parser.add_argument("--score-matrix", type=Path, required=True)
    parser.add_argument("--query-vector-matrix", type=Path, required=True)
    parser.add_argument("--expected-gold-sha256")
    parser.add_argument("--expected-source-commit")
    parser.add_argument("--validate-profile", type=Path)
    parser.add_argument("--expected-profile-sha256")
    parser.add_argument("--generation-manifest-dir", type=Path)
    parser.add_argument("--serving-generation-manifest", type=Path)
    parser.add_argument("--fixture-mode", action="store_true")
    parser.add_argument("--bootstrap-samples", type=int, default=2_000)
    parser.add_argument("--bootstrap-seed", type=int, default=1010)
    arguments = parser.parse_args(argv)
    try:
        validate_profile = cast(Path | None, arguments.validate_profile)
        generation_directory = cast(Path | None, arguments.generation_manifest_dir)
        expected_profile_sha256 = cast(str | None, arguments.expected_profile_sha256)
        expected_source_commit = cast(str | None, arguments.expected_source_commit)
        serving_generation_manifest = cast(
            Path | None,
            arguments.serving_generation_manifest,
        )
        fixture_mode = cast(bool, arguments.fixture_mode)
        if validate_profile is not None:
            if (
                fixture_mode
                or generation_directory is None
                or serving_generation_manifest is None
                or expected_profile_sha256 is None
                or expected_source_commit is None
                or cast(str | None, arguments.expected_gold_sha256) is not None
            ):
                raise AggregationProfileError("invalid_profile_validation_arguments")
            artifact = validate_aggregation_profile(
                validate_profile,
                cast(Path, arguments.gold),
                cast(Path, arguments.scores),
                cast(Path, arguments.corpus_inventory),
                cast(Path, arguments.score_matrix),
                cast(Path, arguments.query_vector_matrix),
                generation_directory,
                expected_profile_sha256=expected_profile_sha256,
                expected_source_commit=expected_source_commit,
                serving_generation_manifest_path=serving_generation_manifest,
                bootstrap_samples=cast(int, arguments.bootstrap_samples),
                bootstrap_seed=cast(int, arguments.bootstrap_seed),
            )
            receipt = {
                "profile_sha256": hashlib.sha256(artifact.canonical_bytes + b"\n").hexdigest(),
                "schema_version": VALIDATION_RECEIPT_SCHEMA_VERSION,
                "sealed_profile_sha256": artifact.payload["sealed_profile_sha256"],
                "status": "validated",
            }
            sys.stdout.buffer.write(_canonical_json_bytes(receipt) + b"\n")
            return 0
        if expected_profile_sha256 is not None or serving_generation_manifest is not None:
            raise AggregationProfileError("invalid_profile_arguments")
        if not fixture_mode and generation_directory is None:
            raise AggregationProfileError("generation_manifest_directory_required")
        artifact = build_aggregation_profile(
            cast(Path, arguments.gold),
            cast(Path, arguments.scores),
            cast(Path, arguments.corpus_inventory),
            cast(Path, arguments.score_matrix),
            cast(Path, arguments.query_vector_matrix),
            release_gate=not fixture_mode,
            expected_gold_sha256=cast(str | None, arguments.expected_gold_sha256),
            expected_source_commit=expected_source_commit,
            bootstrap_samples=cast(int, arguments.bootstrap_samples),
            bootstrap_seed=cast(int, arguments.bootstrap_seed),
        )
        if generation_directory is not None:
            generation_sha256 = cast(
                str, artifact.payload["artifact_bindings"]["generation_manifest_sha256"]
            )
            _validate_generation_manifest(generation_sha256, generation_directory)
    except AggregationProfileError as exc:
        error = {"line": exc.line, "reason_code": exc.code, "status": "failed"}
        print(json.dumps(error, sort_keys=True, separators=(",", ":")), file=sys.stderr)
        return 2
    sys.stdout.buffer.write(artifact.canonical_bytes + b"\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through ``main``
    raise SystemExit(main())
