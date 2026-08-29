"""Seal the statistically selected v1.0.10 document aggregation policy.

The online exact-search implementation is deliberately not imported here.  This
offline evaluator consumes every row score from a hash-bound JSONL artifact,
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
import os
import re
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal, cast

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

SCORE_ARTIFACT_SCHEMA_VERSION = "cardrag.document-aggregation-score-artifact.v1"
QUERY_COVERAGE_SCHEMA_VERSION = "cardrag.document-aggregation-query-coverage.v1"
ROW_SCORE_SCHEMA_VERSION = "cardrag.document-aggregation-row-score.v1"
PROFILE_ARTIFACT_SCHEMA_VERSION = "cardrag.document-aggregation-profile-artifact.v1"
SEALED_PROFILE_SCHEMA_VERSION = "cardrag.document-aggregation-profile.v1"
VALIDATION_RECEIPT_SCHEMA_VERSION = "cardrag.document-aggregation-validation-receipt.v1"

MAX_SCORE_ARTIFACT_BYTES = 4 * 1024 * 1024 * 1024
MAX_SCORE_LINE_BYTES = 64 * 1024
MAX_SCORE_ROWS = 20_000_000
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


class AggregationProfileError(RuntimeError):
    """A bounded, machine-readable profile evaluation failure."""

    def __init__(self, code: str, *, line: int | None = None) -> None:
        self.code = code
        self.line = line
        super().__init__(code)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class ScoreArtifactManifest(_StrictModel):
    schema_version: Literal["cardrag.document-aggregation-score-artifact.v1"]
    gold_sha256: Sha256Hex
    query_count: int = Field(strict=True, ge=1, le=500)
    row_count: int = Field(strict=True, ge=1, le=MAX_SCORE_ROWS)
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
        return self


class QueryScoreCoverage(_StrictModel):
    schema_version: Literal["cardrag.document-aggregation-query-coverage.v1"]
    query_id: Identifier
    query_sha256: Sha256Hex
    query_vector_sha256: Sha256Hex
    expected_rows: int = Field(strict=True, ge=1, le=MAX_SCORE_ROWS)
    scored_rows: int = Field(strict=True, ge=1, le=MAX_SCORE_ROWS)
    active_contracts: int = Field(strict=True, ge=1, le=100_000)


class RowScore(_StrictModel):
    schema_version: Literal["cardrag.document-aggregation-row-score.v1"]
    query_id: Identifier
    ordinal: int = Field(strict=True, ge=0, le=MAX_SCORE_ROWS - 1)
    row_index: int = Field(strict=True, ge=0)
    contract_revision_id: Identifier
    node_id: Identifier
    view_type: ViewType
    input_sha256: Sha256Hex
    embedding_profile_id: EmbeddingProfileId
    score: float = Field(strict=True, ge=-1.0, le=1.0)


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

    def add(self, row: RowScore) -> None:
        if row.view_type == "CONTRACT":
            if self.contract_score is not None:
                raise AggregationProfileError("duplicate_contract_view")
            self.contract_score = row.score
        else:
            self.child_scores.append(row.score)

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


def _identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


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
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(absolute, flags)
    except OSError as exc:
        raise AggregationProfileError(f"{error_prefix}_open_failed") from exc
    try:
        before = os.fstat(descriptor)
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise AggregationProfileError(f"{error_prefix}_changed_during_read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise AggregationProfileError(f"{error_prefix}_changed_during_read")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if _identity(before) != _identity(after) or (listed.st_dev, listed.st_ino) != (
        before.st_dev,
        before.st_ino,
    ):
        raise AggregationProfileError(f"{error_prefix}_changed_during_read")
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
    def __init__(self, path: Path) -> None:
        self._path = _absolute_without_resolving(path)
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
            raise AggregationProfileError("score_artifact_missing") from None
        if stat.S_ISLNK(listed.st_mode) or not stat.S_ISREG(listed.st_mode):
            raise AggregationProfileError("score_artifact_not_regular")
        if listed.st_size <= 0 or listed.st_size > MAX_SCORE_ARTIFACT_BYTES:
            raise AggregationProfileError("score_artifact_size_invalid")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self._path, flags)
        except OSError as exc:
            raise AggregationProfileError("score_artifact_open_failed") from exc
        self._descriptor = descriptor
        self._listed = listed
        self._before = os.fstat(descriptor)
        self._stream = os.fdopen(descriptor, "rb", closefd=False)
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
                if (
                    exc is None
                    and listed is not None
                    and before is not None
                    and (
                        _identity(before) != _identity(after)
                        or (listed.st_dev, listed.st_ino) != (before.st_dev, before.st_ino)
                        or self.size_bytes != before.st_size
                    )
                ):
                    raise AggregationProfileError("score_artifact_changed_during_read")
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
        self.line += 1
        self.size_bytes += len(raw)
        self._digest.update(raw)
        return _decode_canonical_line(raw, self.line)

    @property
    def sha256(self) -> str:
        return self._digest.hexdigest()


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


def _parse_scores(path: Path, gold: GoldDataset) -> ParsedScoreArtifact:
    query_metrics: dict[AggregationPolicy, dict[str, dict[str, float | None]]] = {
        policy: {} for policy in POLICIES
    }
    query_contract_counts: dict[str, int] = {}
    total_rows = 0
    with _ScoreLineReader(path) as reader:
        manifest_record = reader.next_record()
        if manifest_record is None:
            raise AggregationProfileError("score_artifact_empty")
        try:
            manifest = ScoreArtifactManifest.model_validate(manifest_record)
        except ValidationError as exc:
            raise AggregationProfileError("score_manifest_invalid", line=1) from exc
        if manifest.gold_sha256 != gold.sha256:
            raise AggregationProfileError("score_gold_sha256_mismatch", line=1)
        if manifest.query_count != len(gold.queries):
            raise AggregationProfileError("score_query_count_mismatch", line=1)
        manifest_sha256 = hashlib.sha256(_canonical_json_bytes(manifest_record)).hexdigest()

        for query in gold.queries:
            coverage_record = reader.next_record()
            if coverage_record is None:
                raise AggregationProfileError("score_query_coverage_missing", line=reader.line + 1)
            try:
                coverage = QueryScoreCoverage.model_validate(coverage_record)
            except ValidationError as exc:
                raise AggregationProfileError(
                    "score_query_coverage_invalid", line=reader.line
                ) from exc
            if coverage.query_id != query.query_id or coverage.query_sha256 != _query_sha256(query):
                raise AggregationProfileError("score_query_binding_mismatch", line=reader.line)
            if coverage.expected_rows != coverage.scored_rows:
                raise AggregationProfileError("score_query_coverage_incomplete", line=reader.line)

            contracts: dict[str, _ContractAccumulator] = {}
            seen_rows: set[int] = set()
            seen_views: set[tuple[str, str, ViewType]] = set()
            previous_row_index = -1
            for ordinal in range(coverage.scored_rows):
                row_record = reader.next_record()
                if row_record is None:
                    raise AggregationProfileError("score_row_missing", line=reader.line + 1)
                try:
                    row = RowScore.model_validate(row_record)
                except ValidationError as exc:
                    raise AggregationProfileError("score_row_invalid", line=reader.line) from exc
                if row.query_id != query.query_id or row.ordinal != ordinal:
                    raise AggregationProfileError(
                        "score_row_query_order_mismatch", line=reader.line
                    )
                if row.embedding_profile_id != manifest.embedding_profile_id:
                    raise AggregationProfileError("score_row_profile_mismatch", line=reader.line)
                if row.row_index in seen_rows or row.row_index <= previous_row_index:
                    raise AggregationProfileError(
                        "score_row_index_not_unique_sorted", line=reader.line
                    )
                view_key = (row.contract_revision_id, row.node_id, row.view_type)
                if view_key in seen_views:
                    raise AggregationProfileError("score_view_duplicate", line=reader.line)
                seen_rows.add(row.row_index)
                seen_views.add(view_key)
                previous_row_index = row.row_index
                contracts.setdefault(row.contract_revision_id, _ContractAccumulator()).add(row)

            if len(contracts) != coverage.active_contracts:
                raise AggregationProfileError(
                    "score_active_contract_count_mismatch", line=reader.line
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
            total_rows += coverage.scored_rows

        if reader.next_record() is not None:
            raise AggregationProfileError("score_artifact_trailing_record", line=reader.line)
        artifact_sha256 = reader.sha256
        size_bytes = reader.size_bytes

    if total_rows != manifest.row_count:
        raise AggregationProfileError("score_manifest_row_count_mismatch", line=1)
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
    *,
    release_gate: bool = True,
    expected_gold_sha256: str | None = None,
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

    scores = _parse_scores(score_artifact_path, gold)
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
            "generation_manifest_sha256": scores.manifest.generation_manifest_sha256,
            "gold_sha256": gold.sha256,
            "score_artifact_manifest_sha256": scores.manifest_sha256,
            "score_artifact_sha256": scores.artifact_sha256,
            "score_artifact_size_bytes": scores.size_bytes,
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
            "row_count": scores.manifest.row_count,
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
    generation_manifest_directory: Path,
    *,
    expected_profile_sha256: str,
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
        release_gate=True,
        expected_gold_sha256=hashlib.sha256(gold_bytes).hexdigest(),
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
    parser.add_argument("--expected-gold-sha256")
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
                or cast(str | None, arguments.expected_gold_sha256) is not None
            ):
                raise AggregationProfileError("invalid_profile_validation_arguments")
            artifact = validate_aggregation_profile(
                validate_profile,
                cast(Path, arguments.gold),
                cast(Path, arguments.scores),
                generation_directory,
                expected_profile_sha256=expected_profile_sha256,
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
            release_gate=not fixture_mode,
            expected_gold_sha256=cast(str | None, arguments.expected_gold_sha256),
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
