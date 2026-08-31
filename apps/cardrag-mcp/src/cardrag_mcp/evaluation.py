"""Deterministic offline evaluation for the CardRAG v1.0.11 gold set.

This module deliberately has no dependency on the production repository or
search path.  It consumes sealed JSONL labels, already-captured lane results,
and an anonymous pairwise answer artifact, validates complete query coverage,
and emits or strictly revalidates a canonical report.
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
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, cast

import numpy as np
import numpy.typing as npt
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

GOLD_SCHEMA_VERSION = "cardrag.gold-query.v1"
RUN_SCHEMA_VERSION = "cardrag.gold-run-result.v1"
RUN_ARTIFACT_SCHEMA_VERSION = "cardrag.gold-run-artifact.v1"
BLIND_ARTIFACT_SCHEMA_VERSION = "cardrag.blind-evaluation-artifact.v1"
BLIND_RATING_SCHEMA_VERSION = "cardrag.blind-pairwise-rating.v1"
REPORT_SCHEMA_VERSION = "cardrag.gold-evaluation-report.v1"
V109_BASELINE_COMMIT = "fee8f65a9fda7ae0c286ac92cf4c3f55c1a6f113"

MIN_RELEASE_QUERIES = 300
MAX_RELEASE_QUERIES = 500
MIN_RELEASE_BOOTSTRAP_SAMPLES = 2_000
MAX_JSONL_BYTES = 256 * 1024 * 1024
MAX_JSONL_LINE_BYTES = 2 * 1024 * 1024
MIN_BOOTSTRAP_SAMPLES = 100
MAX_BOOTSTRAP_SAMPLES = 10_000
MAX_DISTINCT_SLICES = 256
MAX_BLIND_RATINGS_PER_QUERY = 10

LANES = (
    "v109_baseline",
    "qwen_page",
    "qwen_structure_exact",
    "lexical_shadow",
    "reranker_shadow",
)
type EvaluationLane = Literal[
    "v109_baseline",
    "qwen_page",
    "qwen_structure_exact",
    "lexical_shadow",
    "reranker_shadow",
]
type PairwisePreference = Literal["left", "tie", "right"]
type PairPosition = Literal["left", "right"]
type BlindDimension = Literal["naturalness", "factual_completeness"]

BLIND_DIMENSIONS: tuple[BlindDimension, ...] = ("naturalness", "factual_completeness")

REQUIRED_RELEASE_SLICES = frozenset(
    {
        "benefit",
        "earning",
        "discount",
        "cashback",
        "performance",
        "exclusion",
        "limit",
        "frequency",
        "minimum_payment",
        "annual_fee",
        "issuance_condition",
        "foreign_fee",
        "negation",
        "exception",
        "grace_period",
        "table",
        "footnote",
        "cross_page",
        "common_notice",
        "hard_negative",
        "current_history",
        "product_specific",
        "discovery_recommendation",
        "comparison",
        "no_answer",
        "long",
        "major:benefit",
        "major:notice",
        "issuer:kb",
        "issuer:samsung",
        "issuer:shinhan",
        "issuer:woori",
    }
)

PRIMARY_METRICS = (
    "contract_recall_at_10",
    "span_recall_at_5",
    "ndcg_at_10",
    "mrr_at_10",
)
METRIC_NAMES = (
    "contract_recall_at_10",
    "contract_recall_at_50",
    "contract_recall_at_100",
    "span_recall_at_5",
    "span_recall_at_10",
    "ndcg_at_10",
    "mrr_at_10",
    "condition_coretrieval",
    "numeric_fact_precision",
    "numeric_fact_recall",
    "numeric_fact_exact_match",
    "revision_accuracy",
    "no_answer_accuracy",
    "no_answer_false_positive_rate",
    "no_answer_false_negative_rate",
    "citation_precision",
    "citation_recall",
    "span_contract_integrity",
)

Identifier = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,511}$"),
]
SliceName = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9][a-z0-9._:-]{0,63}$"),
]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
EvidenceRole = Literal[
    "benefit",
    "condition",
    "exclusion",
    "notice",
    "numeric",
    "revision",
    "citation",
    "other",
]
EvaluationProfile = Literal[
    "cardrag.eval.v109-small-rrf.v1",
    "cardrag.eval.qwen-page.v1",
    "cardrag.eval.qwen-structure-exact.v1",
    "cardrag.eval.lexical-shadow.v1",
    "cardrag.eval.reranker-shadow.v1",
]
RetrievalPolicy = Literal[
    "small_rrf",
    "qwen_page_window",
    "qwen_structure_exact",
    "qwen_structure_exact_lexical_shadow",
    "qwen_structure_exact_reranker_shadow",
]

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_COMMIT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


class EvaluationError(RuntimeError):
    """A bounded, machine-readable evaluator failure."""

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
            raise EvaluationError("expected_source_commit_required")
        return None
    if _SOURCE_COMMIT.fullmatch(value) is None:
        raise EvaluationError("expected_source_commit_invalid")
    return value


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class GoldContract(_StrictModel):
    contract_revision_id: Identifier
    relevance: int = Field(ge=1, le=3)


class GoldSpan(_StrictModel):
    span_id: Identifier
    contract_revision_id: Identifier
    page: int = Field(ge=1)
    source_start: int = Field(ge=0)
    source_end: int = Field(gt=0)
    text_sha256: Sha256Hex
    relevance: int = Field(default=1, ge=1, le=3)
    roles: tuple[EvidenceRole, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_span(self) -> GoldSpan:
        if self.source_end <= self.source_start:
            raise ValueError("source span must be non-empty")
        if len(set(self.roles)) != len(self.roles) or tuple(sorted(self.roles)) != self.roles:
            raise ValueError("span roles must be unique and sorted")
        return self


class ConditionGroup(_StrictModel):
    span_ids: tuple[Identifier, ...] = Field(min_length=2, max_length=16)
    at_k: Literal[5, 10] = 10

    @model_validator(mode="after")
    def validate_ids(self) -> ConditionGroup:
        if len(set(self.span_ids)) != len(self.span_ids):
            raise ValueError("condition group span IDs must be unique")
        return self


class GoldQuery(_StrictModel):
    schema_version: Literal["cardrag.gold-query.v1"]
    query_id: Identifier
    question: str = Field(min_length=1, max_length=4_096)
    slices: tuple[SliceName, ...] = Field(min_length=1, max_length=64)
    contracts: tuple[GoldContract, ...] = Field(default=(), max_length=100)
    spans: tuple[GoldSpan, ...] = Field(default=(), max_length=500)
    condition_groups: tuple[ConditionGroup, ...] = Field(default=(), max_length=32)
    expected_numeric_facts: tuple[str, ...] = Field(default=(), max_length=64)
    expected_revision_ids: tuple[Identifier, ...] = Field(default=(), max_length=32)
    no_answer: bool
    high_risk: bool = False

    @model_validator(mode="after")
    def validate_gold_contract(self) -> GoldQuery:
        if self.question != self.question.strip() or any(ord(char) < 32 for char in self.question):
            raise ValueError("question must be a trimmed single line")
        if len(set(self.slices)) != len(self.slices) or tuple(sorted(self.slices)) != self.slices:
            raise ValueError("slices must be unique and sorted")
        contract_ids = [item.contract_revision_id for item in self.contracts]
        span_ids = [item.span_id for item in self.spans]
        if len(set(contract_ids)) != len(contract_ids) or len(set(span_ids)) != len(span_ids):
            raise ValueError("gold contract and span IDs must be unique")
        contract_set = set(contract_ids)
        span_set = set(span_ids)
        if any(item.contract_revision_id not in contract_set for item in self.spans):
            raise ValueError("every gold span must belong to a gold contract")
        if not set(self.expected_revision_ids).issubset(contract_set):
            raise ValueError("expected revision IDs must be gold contracts")
        if len(set(self.expected_revision_ids)) != len(self.expected_revision_ids):
            raise ValueError("expected revision IDs must be unique")
        if any(
            not fact or fact != fact.strip() or len(fact) > 256 or "\x00" in fact
            for fact in self.expected_numeric_facts
        ):
            raise ValueError("numeric facts must be non-empty, trimmed, and bounded")
        if len(set(self.expected_numeric_facts)) != len(self.expected_numeric_facts):
            raise ValueError("numeric facts must be unique")
        roles_by_span = {item.span_id: set(item.roles) for item in self.spans}
        for group in self.condition_groups:
            if not set(group.span_ids).issubset(span_set):
                raise ValueError("condition groups must reference gold spans")
            roles = set().union(*(roles_by_span[span_id] for span_id in group.span_ids))
            if "benefit" not in roles or not roles.intersection(
                {"condition", "exclusion", "notice"}
            ):
                raise ValueError("condition groups need benefit and condition/notice evidence")
        if self.no_answer:
            if (
                self.contracts
                or self.spans
                or self.condition_groups
                or self.expected_numeric_facts
                or self.expected_revision_ids
                or self.high_risk
            ):
                raise ValueError("no-answer labels cannot contain positive evidence")
            if "no_answer" not in self.slices:
                raise ValueError("no-answer labels require the no_answer slice")
        else:
            if "no_answer" in self.slices:
                raise ValueError("answerable labels cannot use the no_answer slice")
            if not self.contracts or not self.spans:
                raise ValueError("answerable labels require contracts and source spans")
            span_roles = {role for span in self.spans for role in span.roles}
            if self.expected_numeric_facts and "numeric" not in span_roles:
                raise ValueError("numeric facts require numeric source evidence")
            if self.expected_revision_ids and "revision" not in span_roles:
                raise ValueError("revision labels require revision source evidence")
        return self


class RetrievedContract(_StrictModel):
    contract_revision_id: Identifier
    rank: int = Field(ge=1, le=1_000)
    score: float


class RetrievedSpan(_StrictModel):
    span_id: Identifier
    contract_revision_id: Identifier
    rank: int = Field(ge=1, le=1_000)
    score: float


def _validate_rankings(
    contracts: Sequence[RetrievedContract],
    spans: Sequence[RetrievedSpan],
) -> None:
    contract_ids = [item.contract_revision_id for item in contracts]
    span_ids = [item.span_id for item in spans]
    if len(set(contract_ids)) != len(contract_ids) or len(set(span_ids)) != len(span_ids):
        raise ValueError("retrieved identities must be unique")
    if [item.rank for item in contracts] != list(range(1, len(contracts) + 1)):
        raise ValueError("contract ranks must be contiguous and ordered")
    if [item.rank for item in spans] != list(range(1, len(spans) + 1)):
        raise ValueError("span ranks must be contiguous and ordered")
    if not {item.contract_revision_id for item in spans}.issubset(set(contract_ids)):
        raise ValueError("retrieved spans must belong to retrieved contracts")


class RunArtifactManifest(_StrictModel):
    schema_version: Literal["cardrag.gold-run-artifact.v1"]
    lane: EvaluationLane
    profile_id: EvaluationProfile
    gold_sha256: Sha256Hex
    query_count: int = Field(ge=1, le=MAX_RELEASE_QUERIES)
    source_version: Literal["v1.0.9", "v1.0.11-candidate"]
    source_commit: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}([0-9a-f]{24})?$")]
    generation_id: Identifier
    generation_manifest_sha256: Sha256Hex
    serving_schema: Literal[
        "cardrag.serving-db.v4",
        "cardrag.serving-db.v5",
        "cardrag.evaluation-page.v1",
    ]
    embedding_model: str = Field(min_length=1, max_length=512)
    embedding_dimension: Literal[1536, 4096]
    retrieval_policy: RetrievalPolicy
    rrf_k: int | None = Field(default=None, ge=1, le=1_000)
    shadow_only: bool
    primary_lane: Literal["qwen_structure_exact"] | None = None
    shadow_model: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def validate_profile(self) -> RunArtifactManifest:
        common_qwen = (
            self.source_version == "v1.0.11-candidate"
            and self.embedding_model == "qwen/qwen3-embedding-8b"
            and self.embedding_dimension == 4096
            and self.rrf_k is None
        )
        valid = False
        if self.lane == "v109_baseline":
            valid = (
                self.profile_id == "cardrag.eval.v109-small-rrf.v1"
                and self.source_version == "v1.0.9"
                and self.source_commit == V109_BASELINE_COMMIT
                and self.serving_schema == "cardrag.serving-db.v4"
                and self.embedding_model == "openai/text-embedding-3-small"
                and self.embedding_dimension == 1536
                and self.retrieval_policy == "small_rrf"
                and self.rrf_k == 60
                and not self.shadow_only
                and self.primary_lane is None
                and self.shadow_model is None
            )
        elif self.lane == "qwen_page":
            valid = (
                common_qwen
                and self.profile_id == "cardrag.eval.qwen-page.v1"
                and self.serving_schema == "cardrag.evaluation-page.v1"
                and self.retrieval_policy == "qwen_page_window"
                and not self.shadow_only
                and self.primary_lane is None
                and self.shadow_model is None
            )
        elif self.lane == "qwen_structure_exact":
            valid = (
                common_qwen
                and self.profile_id == "cardrag.eval.qwen-structure-exact.v1"
                and self.serving_schema == "cardrag.serving-db.v5"
                and self.retrieval_policy == "qwen_structure_exact"
                and not self.shadow_only
                and self.primary_lane is None
                and self.shadow_model is None
            )
        elif self.lane == "lexical_shadow":
            valid = (
                common_qwen
                and self.profile_id == "cardrag.eval.lexical-shadow.v1"
                and self.serving_schema == "cardrag.serving-db.v5"
                and self.retrieval_policy == "qwen_structure_exact_lexical_shadow"
                and self.shadow_only
                and self.primary_lane == "qwen_structure_exact"
                and self.shadow_model is None
            )
        elif self.lane == "reranker_shadow":
            valid = (
                common_qwen
                and self.profile_id == "cardrag.eval.reranker-shadow.v1"
                and self.serving_schema == "cardrag.serving-db.v5"
                and self.retrieval_policy == "qwen_structure_exact_reranker_shadow"
                and self.shadow_only
                and self.primary_lane == "qwen_structure_exact"
                and self.shadow_model == "qwen/qwen3-reranker-8b"
            )
        if not valid:
            raise ValueError("run artifact profile does not match its lane")
        return self


class ShadowObservation(_StrictModel):
    kind: Literal["lexical", "reranker"]
    influenced_primary_ordering: Literal[False] = False
    contracts: tuple[RetrievedContract, ...] = Field(default=(), max_length=1_000)
    spans: tuple[RetrievedSpan, ...] = Field(default=(), max_length=1_000)

    @model_validator(mode="after")
    def validate_shadow_ranking(self) -> ShadowObservation:
        _validate_rankings(self.contracts, self.spans)
        return self


class V109BaselineObservation(_StrictModel):
    kind: Literal["v109_small_rrf"]
    rrf_k: Literal[60]
    dense_contracts: tuple[RetrievedContract, ...] = Field(min_length=1, max_length=1_000)
    dense_spans: tuple[RetrievedSpan, ...] = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def validate_dense_raw_ranking(self) -> V109BaselineObservation:
        _validate_rankings(self.dense_contracts, self.dense_spans)
        return self


class EvaluatedAnswer(_StrictModel):
    text: str = Field(min_length=1, max_length=65_536)
    no_answer: bool
    citation_span_ids: tuple[Identifier, ...] = Field(default=(), max_length=500)
    numeric_facts: tuple[str, ...] = Field(default=(), max_length=64)
    selected_revision_ids: tuple[Identifier, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def validate_answer(self) -> EvaluatedAnswer:
        if self.text != self.text.strip() or any(
            ord(character) < 32 and character not in {"\n", "\t"} for character in self.text
        ):
            raise ValueError("answer text must be trimmed and contain no control characters")
        for values in (self.citation_span_ids, self.numeric_facts, self.selected_revision_ids):
            if len(set(values)) != len(values):
                raise ValueError("answer evidence values must be unique")
        if any(
            not fact or fact != fact.strip() or len(fact) > 256 or "\x00" in fact
            for fact in self.numeric_facts
        ):
            raise ValueError("answer numeric facts must be trimmed and bounded")
        if self.no_answer and (
            self.citation_span_ids or self.numeric_facts or self.selected_revision_ids
        ):
            raise ValueError("a no-answer result cannot claim answer evidence")
        return self


class QueryRunResult(_StrictModel):
    schema_version: Literal["cardrag.gold-run-result.v1"]
    query_id: Identifier
    lane: EvaluationLane
    contracts: tuple[RetrievedContract, ...] = Field(default=(), max_length=1_000)
    spans: tuple[RetrievedSpan, ...] = Field(default=(), max_length=1_000)
    answer: EvaluatedAnswer
    v109_baseline: V109BaselineObservation | None = None
    shadow: ShadowObservation | None = None

    @model_validator(mode="after")
    def validate_ranking(self) -> QueryRunResult:
        _validate_rankings(self.contracts, self.spans)
        contract_ids = {item.contract_revision_id for item in self.contracts}
        span_ids = {item.span_id for item in self.spans}
        if not set(self.answer.citation_span_ids).issubset(set(span_ids)):
            raise ValueError("answer citations must be retrieved spans")
        if not set(self.answer.selected_revision_ids).issubset(set(contract_ids)):
            raise ValueError("selected revisions must be retrieved contracts")
        if self.lane == "v109_baseline":
            if self.v109_baseline is None:
                raise ValueError("v1.0.9 baseline requires its exact dense raw ranking")
        elif self.v109_baseline is not None:
            raise ValueError("non-baseline lanes cannot contain a v1.0.9 baseline trace")
        if self.lane == "lexical_shadow":
            if self.shadow is None or self.shadow.kind != "lexical":
                raise ValueError("lexical shadow lane requires lexical observations")
        elif self.lane == "reranker_shadow":
            if self.shadow is None or self.shadow.kind != "reranker":
                raise ValueError("reranker shadow lane requires reranker observations")
        elif self.shadow is not None:
            raise ValueError("non-shadow lanes cannot contain shadow observations")
        return self


class BlindEvaluationManifest(_StrictModel):
    schema_version: Literal["cardrag.blind-evaluation-artifact.v1"]
    gold_sha256: Sha256Hex
    baseline_lane: Literal["v109_baseline"]
    baseline_run_sha256: Sha256Hex
    candidate_lane: Literal["qwen_structure_exact"]
    candidate_run_sha256: Sha256Hex
    query_count: int = Field(strict=True, ge=1, le=MAX_RELEASE_QUERIES)
    ratings_per_query: int = Field(strict=True, ge=1, le=MAX_BLIND_RATINGS_PER_QUERY)
    pair_count: int = Field(
        strict=True,
        ge=1,
        le=MAX_RELEASE_QUERIES * MAX_BLIND_RATINGS_PER_QUERY,
    )
    presentation_protocol: Literal["anonymous-a-b.v1"]
    rubric_id: Literal["cardrag.blind-rubric.naturalness-factual-completeness.v1"]
    lane_identity_exposed_to_raters: bool = Field(strict=True)

    @model_validator(mode="after")
    def validate_pair_count(self) -> BlindEvaluationManifest:
        if self.lane_identity_exposed_to_raters:
            raise ValueError("blind evaluation cannot expose lane identity to raters")
        if self.pair_count != self.query_count * self.ratings_per_query:
            raise ValueError("blind pair count must cover every query equally")
        return self


class BlindPairwiseRating(_StrictModel):
    schema_version: Literal["cardrag.blind-pairwise-rating.v1"]
    pair_id: Identifier
    query_id: Identifier
    rater_key: Identifier
    candidate_position: PairPosition
    left_answer_sha256: Sha256Hex
    right_answer_sha256: Sha256Hex
    naturalness_preference: PairwisePreference
    factual_completeness_preference: PairwisePreference


@dataclass(frozen=True, slots=True)
class GoldDataset:
    queries: tuple[GoldQuery, ...]
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class RunDataset:
    lane: EvaluationLane
    manifest: RunArtifactManifest
    results: tuple[QueryRunResult, ...]
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class BlindEvaluationDataset:
    manifest: BlindEvaluationManifest
    ratings: tuple[BlindPairwiseRating, ...]
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    payload: dict[str, Any]
    canonical_bytes: bytes
    sha256: str


def _absolute_without_resolving(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int, int, int, int]:
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
    absolute = _absolute_without_resolving(path)
    try:
        listed = absolute.lstat()
    except FileNotFoundError:
        raise EvaluationError("jsonl_missing") from None
    if stat.S_ISLNK(listed.st_mode) or not stat.S_ISREG(listed.st_mode):
        raise EvaluationError("jsonl_not_regular")
    if listed.st_size <= 0 or listed.st_size > MAX_JSONL_BYTES:
        raise EvaluationError("jsonl_size_invalid")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(absolute, flags)
    except OSError as exc:
        raise EvaluationError("jsonl_open_failed") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise EvaluationError("jsonl_not_regular")
        if before.st_size <= 0 or before.st_size > MAX_JSONL_BYTES:
            raise EvaluationError("jsonl_size_invalid")
        if _stat_identity(listed) != _stat_identity(before):
            raise EvaluationError("jsonl_changed_during_read")
        chunks: list[bytes] = []
        remaining = before.st_size
        size = 0
        while remaining:
            block = os.read(descriptor, min(1024 * 1024, remaining))
            if not block:
                raise EvaluationError("jsonl_changed_during_read")
            if len(block) > MAX_JSONL_BYTES - size:
                raise EvaluationError("jsonl_size_invalid")
            if len(block) > remaining:
                raise EvaluationError("jsonl_changed_during_read")
            chunks.append(block)
            size += len(block)
            remaining -= len(block)
        if os.read(descriptor, 1):
            raise EvaluationError("jsonl_changed_during_read")
        after = os.fstat(descriptor)
        try:
            current = absolute.lstat()
        except OSError:
            raise EvaluationError("jsonl_changed_during_read") from None
        identity = _stat_identity(before)
        if (
            size != before.st_size
            or identity != _stat_identity(listed)
            or identity != _stat_identity(after)
            or identity != _stat_identity(current)
        ):
            raise EvaluationError("jsonl_changed_during_read")
    finally:
        os.close(descriptor)
    return b"".join(chunks)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvaluationError("json_duplicate_key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> Any:
    raise EvaluationError("json_non_finite_number")


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _jsonl_objects(data: bytes) -> tuple[dict[str, Any], ...]:
    if data.startswith(b"\xef\xbb\xbf") or not data.endswith(b"\n"):
        raise EvaluationError("jsonl_not_canonical_lines")
    result: list[dict[str, Any]] = []
    for line_number, line in enumerate(data.splitlines(), start=1):
        if not line or len(line) > MAX_JSONL_LINE_BYTES:
            raise EvaluationError("jsonl_line_invalid", line=line_number)
        try:
            decoded = line.decode("utf-8")
            value = json.loads(
                decoded,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_json_constant,
            )
        except EvaluationError as exc:
            raise EvaluationError(exc.code, line=line_number) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EvaluationError("jsonl_line_invalid", line=line_number) from exc
        if not isinstance(value, dict):
            raise EvaluationError("jsonl_record_not_object", line=line_number)
        record = cast(dict[str, Any], value)
        if line != _canonical_json_bytes(record):
            raise EvaluationError("jsonl_not_canonical_bytes", line=line_number)
        result.append(record)
    if not result:
        raise EvaluationError("jsonl_empty")
    return tuple(result)


def load_gold_jsonl(path: Path, *, release_gate: bool = False) -> GoldDataset:
    """Load and validate gold labels; fixture mode may contain fewer than 300."""

    data = _read_regular(path)
    records = _jsonl_objects(data)
    if len(records) > MAX_RELEASE_QUERIES:
        raise EvaluationError("gold_count_out_of_release_range")
    queries: list[GoldQuery] = []
    seen: set[str] = set()
    for line, record in enumerate(records, start=1):
        try:
            query = GoldQuery.model_validate(record)
        except ValidationError as exc:
            raise EvaluationError("gold_schema_invalid", line=line) from exc
        if query.query_id in seen:
            raise EvaluationError("gold_query_duplicate", line=line)
        seen.add(query.query_id)
        queries.append(query)
    if release_gate and not MIN_RELEASE_QUERIES <= len(queries) <= MAX_RELEASE_QUERIES:
        raise EvaluationError("gold_count_out_of_release_range")
    all_slices = {item for query in queries for item in query.slices}
    if len(all_slices) > MAX_DISTINCT_SLICES:
        raise EvaluationError("gold_slice_limit_exceeded")
    if release_gate:
        if not REQUIRED_RELEASE_SLICES.issubset(all_slices):
            raise EvaluationError("gold_required_slice_missing")
        if (
            not any(query.no_answer for query in queries)
            or not any(query.expected_revision_ids for query in queries)
            or not any(query.high_risk and query.expected_numeric_facts for query in queries)
            or not any(query.high_risk and query.condition_groups for query in queries)
        ):
            raise EvaluationError("gold_high_risk_slice_missing")
    return GoldDataset(tuple(queries), hashlib.sha256(data).hexdigest(), len(data))


def load_run_jsonl(path: Path, *, lane: EvaluationLane) -> RunDataset:
    """Load one sealed lane artifact, its profile manifest, and query results."""

    data = _read_regular(path)
    records = _jsonl_objects(data)
    if len(records) > MAX_RELEASE_QUERIES + 1:
        raise EvaluationError("run_result_count_invalid")
    try:
        manifest = RunArtifactManifest.model_validate(records[0])
    except ValidationError as exc:
        raise EvaluationError("run_manifest_invalid", line=1) from exc
    if manifest.lane != lane:
        raise EvaluationError("run_lane_mismatch", line=1)
    result_records = records[1:]
    if manifest.query_count != len(result_records):
        raise EvaluationError("run_manifest_count_mismatch", line=1)
    results: list[QueryRunResult] = []
    seen: set[str] = set()
    for line, record in enumerate(result_records, start=2):
        try:
            result = QueryRunResult.model_validate(record)
        except ValidationError as exc:
            raise EvaluationError("run_schema_invalid", line=line) from exc
        if result.lane != lane:
            raise EvaluationError("run_lane_mismatch", line=line)
        if result.query_id in seen:
            raise EvaluationError("run_query_duplicate", line=line)
        seen.add(result.query_id)
        results.append(result)
    return RunDataset(
        lane,
        manifest,
        tuple(results),
        hashlib.sha256(data).hexdigest(),
        len(data),
    )


def load_blind_evaluation_jsonl(path: Path) -> BlindEvaluationDataset:
    """Load a sealed anonymous A/B rating artifact without resolving its run bindings."""

    data = _read_regular(path)
    records = _jsonl_objects(data)
    if len(records) > MAX_RELEASE_QUERIES * MAX_BLIND_RATINGS_PER_QUERY + 1:
        raise EvaluationError("blind_rating_count_invalid")
    try:
        manifest = BlindEvaluationManifest.model_validate(records[0])
    except ValidationError as exc:
        raise EvaluationError("blind_manifest_invalid", line=1) from exc
    rating_records = records[1:]
    if manifest.pair_count != len(rating_records):
        raise EvaluationError("blind_manifest_count_mismatch", line=1)

    ratings: list[BlindPairwiseRating] = []
    pair_ids: set[str] = set()
    query_raters: set[tuple[str, str]] = set()
    query_counts: dict[str, int] = {}
    positions_by_rater: dict[str, list[PairPosition]] = {}
    positions: list[PairPosition] = []
    for line, record in enumerate(rating_records, start=2):
        try:
            rating = BlindPairwiseRating.model_validate(record)
        except ValidationError as exc:
            raise EvaluationError("blind_rating_invalid", line=line) from exc
        if rating.left_answer_sha256 == rating.right_answer_sha256 and (
            rating.naturalness_preference != "tie"
            or rating.factual_completeness_preference != "tie"
        ):
            raise EvaluationError("blind_identical_answer_preference_invalid", line=line)
        query_rater = (rating.query_id, rating.rater_key)
        if rating.pair_id in pair_ids or query_rater in query_raters:
            raise EvaluationError("blind_rating_duplicate", line=line)
        pair_ids.add(rating.pair_id)
        query_raters.add(query_rater)
        query_counts[rating.query_id] = query_counts.get(rating.query_id, 0) + 1
        positions_by_rater.setdefault(rating.rater_key, []).append(rating.candidate_position)
        positions.append(rating.candidate_position)
        ratings.append(rating)

    if len(query_counts) != manifest.query_count or any(
        count != manifest.ratings_per_query for count in query_counts.values()
    ):
        raise EvaluationError("blind_query_coverage_mismatch")

    def position_imbalance(values: Sequence[PairPosition]) -> int:
        return abs(values.count("left") - values.count("right"))

    if position_imbalance(positions) > 1 or any(
        position_imbalance(rater_positions) > 1 for rater_positions in positions_by_rater.values()
    ):
        raise EvaluationError("blind_assignment_unbalanced")
    return BlindEvaluationDataset(
        manifest,
        tuple(ratings),
        hashlib.sha256(data).hexdigest(),
        len(data),
    )


def _answer_sha256(answer: EvaluatedAnswer) -> str:
    return hashlib.sha256(answer.text.encode("utf-8")).hexdigest()


def _validate_blind_bindings(
    blind: BlindEvaluationDataset,
    gold: GoldDataset,
    datasets: Mapping[str, RunDataset],
    results_by_lane: Mapping[str, Mapping[str, QueryRunResult]],
) -> None:
    manifest = blind.manifest
    if (
        manifest.gold_sha256 != gold.sha256
        or manifest.query_count != len(gold.queries)
        or manifest.baseline_run_sha256 != datasets["v109_baseline"].sha256
        or manifest.candidate_run_sha256 != datasets["qwen_structure_exact"].sha256
    ):
        raise EvaluationError("blind_artifact_binding_mismatch")
    query_ids = {query.query_id for query in gold.queries}
    if {rating.query_id for rating in blind.ratings} != query_ids:
        raise EvaluationError("blind_query_coverage_mismatch")

    baseline = results_by_lane["v109_baseline"]
    candidate = results_by_lane["qwen_structure_exact"]
    for rating in blind.ratings:
        baseline_sha256 = _answer_sha256(baseline[rating.query_id].answer)
        candidate_sha256 = _answer_sha256(candidate[rating.query_id].answer)
        expected = (
            (candidate_sha256, baseline_sha256)
            if rating.candidate_position == "left"
            else (baseline_sha256, candidate_sha256)
        )
        if (rating.left_answer_sha256, rating.right_answer_sha256) != expected:
            raise EvaluationError("blind_answer_hash_mismatch")


def _set_precision(predicted: set[str], expected: set[str]) -> float:
    if not predicted:
        return 0.0
    return len(predicted.intersection(expected)) / len(predicted)


def _set_recall(predicted: set[str], expected: set[str]) -> float:
    return len(predicted.intersection(expected)) / len(expected)


def _evaluated_rankings(
    result: QueryRunResult,
) -> tuple[Sequence[RetrievedContract], Sequence[RetrievedSpan]]:
    if result.shadow is not None:
        return result.shadow.contracts, result.shadow.spans
    return result.contracts, result.spans


def _query_metrics(gold: GoldQuery, result: QueryRunResult) -> dict[str, float | None]:
    expected_contracts = {item.contract_revision_id for item in gold.contracts}
    expected_spans = {item.span_id for item in gold.spans}
    ranked_contracts, ranked_spans = _evaluated_rankings(result)
    retrieved_contracts = [item.contract_revision_id for item in ranked_contracts]
    retrieved_spans = [item.span_id for item in ranked_spans]
    metrics: dict[str, float | None] = {name: None for name in METRIC_NAMES}

    if expected_contracts:
        for limit in (10, 50, 100):
            metrics[f"contract_recall_at_{limit}"] = _set_recall(
                set(retrieved_contracts[:limit]), expected_contracts
            )
        relevance = {item.contract_revision_id: item.relevance for item in gold.contracts}
        dcg = sum(
            (2 ** relevance.get(contract_id, 0) - 1) / math.log2(rank + 1)
            for rank, contract_id in enumerate(retrieved_contracts[:10], start=1)
        )
        ideal = sorted(relevance.values(), reverse=True)[:10]
        idcg = sum(
            (2**grade - 1) / math.log2(rank + 1) for rank, grade in enumerate(ideal, start=1)
        )
        metrics["ndcg_at_10"] = dcg / idcg
        first_relevant = next(
            (
                rank
                for rank, item in enumerate(retrieved_contracts[:10], start=1)
                if item in relevance
            ),
            None,
        )
        metrics["mrr_at_10"] = 0.0 if first_relevant is None else 1.0 / first_relevant

    if expected_spans:
        metrics["span_recall_at_5"] = _set_recall(set(retrieved_spans[:5]), expected_spans)
        metrics["span_recall_at_10"] = _set_recall(set(retrieved_spans[:10]), expected_spans)
        citations = set(result.answer.citation_span_ids)
        metrics["citation_precision"] = _set_precision(citations, expected_spans)
        metrics["citation_recall"] = _set_recall(citations, expected_spans)

    if gold.condition_groups:
        successes = [
            set(group.span_ids).issubset(set(retrieved_spans[: group.at_k]))
            for group in gold.condition_groups
        ]
        metrics["condition_coretrieval"] = sum(successes) / len(successes)

    expected_numeric = set(gold.expected_numeric_facts)
    predicted_numeric = set(result.answer.numeric_facts)
    if expected_numeric or predicted_numeric:
        metrics["numeric_fact_precision"] = _set_precision(predicted_numeric, expected_numeric)
    if expected_numeric:
        metrics["numeric_fact_recall"] = _set_recall(predicted_numeric, expected_numeric)
        metrics["numeric_fact_exact_match"] = float(predicted_numeric == expected_numeric)

    if gold.expected_revision_ids:
        metrics["revision_accuracy"] = float(
            set(result.answer.selected_revision_ids) == set(gold.expected_revision_ids)
        )

    metrics["no_answer_accuracy"] = float(result.answer.no_answer == gold.no_answer)
    if gold.no_answer:
        metrics["no_answer_false_positive_rate"] = float(not result.answer.no_answer)
    else:
        metrics["no_answer_false_negative_rate"] = float(result.answer.no_answer)

    expected_span_contract = {item.span_id: item.contract_revision_id for item in gold.spans}
    contract_mismatches = sum(
        item.span_id in expected_span_contract
        and expected_span_contract[item.span_id] != item.contract_revision_id
        for item in ranked_spans
    )
    metrics["span_contract_integrity"] = float(contract_mismatches == 0)
    return metrics


def _derived_seed(seed: int, context: str) -> int:
    digest = hashlib.sha256(f"{seed}:{context}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def _rounded(value: float) -> float:
    return round(float(value), 12)


def _metric_summary(
    values: Sequence[float | None],
    *,
    indexes: npt.NDArray[np.int64],
) -> dict[str, Any] | None:
    vector = np.array([np.nan if value is None else value for value in values], dtype=np.float64)
    eligible = int(np.count_nonzero(~np.isnan(vector)))
    if eligible == 0:
        return None
    sampled = vector[indexes]
    counts = np.count_nonzero(~np.isnan(sampled), axis=1)
    valid = counts > 0
    replicates = np.nansum(sampled[valid], axis=1) / counts[valid]
    if not len(replicates):  # pragma: no cover - at least one eligible value
        raise EvaluationError("bootstrap_failed")
    low, high = np.quantile(replicates, [0.025, 0.975], method="linear")
    return {
        "ci95": {"high": _rounded(float(high)), "low": _rounded(float(low))},
        "eligible_queries": eligible,
        "value": _rounded(float(np.nanmean(vector))),
    }


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


def _summarize(
    query_ids: Sequence[str],
    contributions: Mapping[str, Mapping[str, float | None]],
    *,
    bootstrap_samples: int,
    seed: int,
    context: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    indexes = _bootstrap_indexes(
        len(query_ids),
        bootstrap_samples=bootstrap_samples,
        seed=seed,
        context=context,
    )
    for metric in METRIC_NAMES:
        summary = _metric_summary(
            [contributions[query_id][metric] for query_id in query_ids],
            indexes=indexes,
        )
        if summary is not None:
            result[metric] = summary
    return result


def _lane_critical_counts(
    gold_by_id: Mapping[str, GoldQuery],
    results: Mapping[str, QueryRunResult],
) -> dict[str, int]:
    numeric_omissions = 0
    condition_omission_queries = 0
    revision_errors = 0
    span_contract_mismatches = 0
    no_answer_false_positives = 0
    for query_id, gold in gold_by_id.items():
        result = results[query_id]
        _, ranked_spans = _evaluated_rankings(result)
        if gold.high_risk:
            numeric_omissions += len(
                set(gold.expected_numeric_facts).difference(result.answer.numeric_facts)
            )
            if gold.condition_groups:
                retrieved = [item.span_id for item in ranked_spans]
                if any(
                    not set(group.span_ids).issubset(set(retrieved[: group.at_k]))
                    for group in gold.condition_groups
                ):
                    condition_omission_queries += 1
        if gold.expected_revision_ids and set(result.answer.selected_revision_ids) != set(
            gold.expected_revision_ids
        ):
            revision_errors += 1
        expected_span_contract = {item.span_id: item.contract_revision_id for item in gold.spans}
        span_contract_mismatches += sum(
            item.span_id in expected_span_contract
            and expected_span_contract[item.span_id] != item.contract_revision_id
            for item in ranked_spans
        )
        if gold.no_answer and not result.answer.no_answer:
            no_answer_false_positives += 1
    return {
        "critical_condition_omission_queries": condition_omission_queries,
        "critical_numeric_omissions": numeric_omissions,
        "no_answer_false_positives": no_answer_false_positives,
        "revision_errors": revision_errors,
        "span_contract_mismatches": span_contract_mismatches,
    }


def _comparison_summary(
    query_ids: Sequence[str],
    candidate: Mapping[str, Mapping[str, float | None]],
    reference: Mapping[str, Mapping[str, float | None]],
    *,
    bootstrap_samples: int,
    seed: int,
    context: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    indexes = _bootstrap_indexes(
        len(query_ids),
        bootstrap_samples=bootstrap_samples,
        seed=seed,
        context=context,
    )
    for metric in METRIC_NAMES:
        deltas: list[float | None] = []
        for query_id in query_ids:
            candidate_value = candidate[query_id][metric]
            reference_value = reference[query_id][metric]
            deltas.append(
                None
                if candidate_value is None or reference_value is None
                else candidate_value - reference_value
            )
        summary = _metric_summary(
            deltas,
            indexes=indexes,
        )
        if summary is not None:
            summary["delta"] = summary.pop("value")
            result[metric] = summary
    return result


def _slice_query_ids(queries: Sequence[GoldQuery]) -> dict[str, tuple[str, ...]]:
    slices = sorted({slice_name for query in queries for slice_name in query.slices})
    return {
        slice_name: tuple(query.query_id for query in queries if slice_name in query.slices)
        for slice_name in slices
    }


def _rating_preference(
    rating: BlindPairwiseRating,
    dimension: BlindDimension,
) -> PairwisePreference:
    if dimension == "naturalness":
        return rating.naturalness_preference
    return rating.factual_completeness_preference


def _candidate_pairwise_delta(
    preference: PairwisePreference,
    candidate_position: PairPosition,
) -> float:
    if preference == "tie":
        return 0.0
    return 1.0 if preference == candidate_position else -1.0


def _blind_dimension_summary(
    query_ids: Sequence[str],
    ratings_by_query: Mapping[str, Sequence[BlindPairwiseRating]],
    *,
    dimension: BlindDimension,
    bootstrap_samples: int,
    seed: int,
    context: str,
) -> dict[str, Any]:
    deltas_by_query = {
        query_id: [
            _candidate_pairwise_delta(
                _rating_preference(rating, dimension),
                rating.candidate_position,
            )
            for rating in ratings_by_query[query_id]
        ]
        for query_id in query_ids
    }
    query_deltas = [
        sum(deltas_by_query[query_id]) / len(deltas_by_query[query_id]) for query_id in query_ids
    ]
    indexes = _bootstrap_indexes(
        len(query_ids),
        bootstrap_samples=bootstrap_samples,
        seed=seed,
        context=context,
    )
    summary = _metric_summary(query_deltas, indexes=indexes)
    if summary is None:  # pragma: no cover - every blind query has a rating
        raise EvaluationError("blind_summary_failed")
    summary["delta"] = summary.pop("value")
    all_deltas = [delta for query_id in query_ids for delta in deltas_by_query[query_id]]
    summary.update(
        {
            "baseline_wins": all_deltas.count(-1.0),
            "candidate_wins": all_deltas.count(1.0),
            "ratings": len(all_deltas),
            "ties": all_deltas.count(0.0),
        }
    )
    return summary


def _blind_evaluation_payload(
    blind: BlindEvaluationDataset,
    query_ids: Sequence[str],
    slice_ids: Mapping[str, Sequence[str]],
    *,
    bootstrap_samples: int,
    seed: int,
) -> dict[str, Any]:
    ratings_by_query: dict[str, list[BlindPairwiseRating]] = {
        query_id: [] for query_id in query_ids
    }
    for rating in blind.ratings:
        ratings_by_query[rating.query_id].append(rating)
    manifest_payload = blind.manifest.model_dump(mode="json")
    return {
        "artifact_manifest": manifest_payload,
        "artifact_manifest_sha256": hashlib.sha256(
            _canonical_json_bytes(manifest_payload)
        ).hexdigest(),
        "artifact_sha256": blind.sha256,
        "artifact_size_bytes": blind.size_bytes,
        "evaluated": True,
        "overall": {
            dimension: _blind_dimension_summary(
                query_ids,
                ratings_by_query,
                dimension=dimension,
                bootstrap_samples=bootstrap_samples,
                seed=seed,
                context=f"blind:{dimension}:overall",
            )
            for dimension in BLIND_DIMENSIONS
        },
        "slices": {
            slice_name: {
                dimension: _blind_dimension_summary(
                    ids,
                    ratings_by_query,
                    dimension=dimension,
                    bootstrap_samples=bootstrap_samples,
                    seed=seed,
                    context=f"blind:{dimension}:slice:{slice_name}",
                )
                for dimension in BLIND_DIMENSIONS
            }
            for slice_name, ids in slice_ids.items()
        },
    }


def _blind_release_gate_reasons(blind: Mapping[str, Any]) -> list[str]:
    overall = cast(Mapping[str, Mapping[str, Any]], blind["overall"])
    naturalness = overall["naturalness"]
    completeness = overall["factual_completeness"]
    reasons: list[str] = []
    if float(cast(Mapping[str, Any], naturalness["ci95"])["low"]) < 0:
        reasons.append("blind_naturalness_regression")
    if float(cast(Mapping[str, Any], completeness["ci95"])["low"]) <= 0:
        reasons.append("blind_factual_completeness_not_improved")
    return reasons


def _release_gate_payload(
    lanes: Mapping[str, dict[str, Any]],
    comparisons: Mapping[str, dict[str, Any]],
    blind: Mapping[str, Any],
    slices: Sequence[str],
    *,
    evaluated: bool,
) -> dict[str, Any]:
    if not evaluated:
        return {"evaluated": False, "failure_reasons": [], "status": "not_evaluated"}
    reasons: list[str] = []
    primary = comparisons["qwen_structure_exact_vs_v109_baseline"]
    for metric in PRIMARY_METRICS:
        summary = primary["overall"].get(metric)
        if summary is None or float(summary["ci95"]["low"]) <= 0:
            reasons.append(f"primary_not_significantly_better:{metric}")
    for slice_name in slices:
        slice_metrics = primary["slices"].get(slice_name, {})
        for metric in PRIMARY_METRICS:
            summary = slice_metrics.get(metric)
            if summary is not None and float(summary["ci95"]["high"]) < 0:
                reasons.append(f"significant_slice_regression:{slice_name}:{metric}")
    exact = lanes["qwen_structure_exact"]
    condition = exact["overall"].get("condition_coretrieval")
    if condition is None or float(condition["value"]) < 0.95:
        reasons.append("condition_coretrieval_below_0_95")
    critical = exact["critical_counts"]
    for key in (
        "critical_condition_omission_queries",
        "critical_numeric_omissions",
        "revision_errors",
        "span_contract_mismatches",
    ):
        if int(critical[key]) != 0:
            reasons.append(f"critical_errors:{key}")
    reasons.extend(_blind_release_gate_reasons(blind))
    return {
        "evaluated": True,
        "failure_reasons": reasons,
        "status": "passed" if not reasons else "failed",
    }


def evaluate_gold_runs(
    gold_path: Path,
    run_paths: Mapping[str, Path],
    *,
    blind_evaluation_path: Path | None = None,
    release_gate: bool = True,
    expected_gold_sha256: str | None = None,
    expected_source_commit: str | None = None,
    bootstrap_samples: int = 2_000,
    bootstrap_seed: int = 1010,
) -> EvaluationReport:
    """Evaluate all five lanes and return a deterministic canonical report."""

    if bootstrap_samples < MIN_BOOTSTRAP_SAMPLES:
        raise EvaluationError("bootstrap_samples_too_small")
    if bootstrap_samples > MAX_BOOTSTRAP_SAMPLES:
        raise EvaluationError("bootstrap_samples_too_large")
    if set(run_paths) != set(LANES):
        raise EvaluationError("required_run_lane_missing")
    gold = load_gold_jsonl(gold_path, release_gate=release_gate)
    if release_gate and bootstrap_samples < MIN_RELEASE_BOOTSTRAP_SAMPLES:
        raise EvaluationError("release_bootstrap_samples_too_small")
    if expected_gold_sha256 is not None:
        if not _SHA256.fullmatch(expected_gold_sha256) or expected_gold_sha256 != gold.sha256:
            raise EvaluationError("gold_sha256_mismatch")
    elif release_gate:
        raise EvaluationError("sealed_gold_sha256_required")
    candidate_source_commit = _validated_expected_source_commit(
        expected_source_commit,
        release_gate=release_gate,
    )

    gold_by_id = {query.query_id: query for query in gold.queries}
    query_ids = tuple(query.query_id for query in gold.queries)
    slice_ids = _slice_query_ids(gold.queries)
    datasets: dict[str, RunDataset] = {}
    results_by_lane: dict[str, dict[str, QueryRunResult]] = {}
    for lane_name in LANES:
        lane = cast(EvaluationLane, lane_name)
        dataset = load_run_jsonl(run_paths[lane_name], lane=lane)
        if dataset.manifest.gold_sha256 != gold.sha256:
            raise EvaluationError("run_manifest_gold_mismatch")
        if (
            lane != "v109_baseline"
            and candidate_source_commit is not None
            and dataset.manifest.source_commit != candidate_source_commit
        ):
            raise EvaluationError("candidate_source_commit_mismatch")
        by_id = {item.query_id: item for item in dataset.results}
        if set(by_id) != set(gold_by_id):
            raise EvaluationError("run_query_coverage_mismatch")
        datasets[lane_name] = dataset
        results_by_lane[lane_name] = by_id

    exact_manifest = datasets["qwen_structure_exact"].manifest
    exact_results = results_by_lane["qwen_structure_exact"]
    for shadow_lane in ("lexical_shadow", "reranker_shadow"):
        manifest = datasets[shadow_lane].manifest
        if (
            manifest.generation_id != exact_manifest.generation_id
            or manifest.generation_manifest_sha256 != exact_manifest.generation_manifest_sha256
            or manifest.source_commit != exact_manifest.source_commit
        ):
            raise EvaluationError("shadow_primary_generation_mismatch")
        for query_id in query_ids:
            shadow_result = results_by_lane[shadow_lane][query_id]
            exact_result = exact_results[query_id]
            if (
                shadow_result.contracts != exact_result.contracts
                or shadow_result.spans != exact_result.spans
                or shadow_result.answer != exact_result.answer
            ):
                raise EvaluationError("shadow_changed_primary_result")

    blind_dataset: BlindEvaluationDataset | None = None
    if blind_evaluation_path is not None:
        blind_dataset = load_blind_evaluation_jsonl(blind_evaluation_path)
        _validate_blind_bindings(blind_dataset, gold, datasets, results_by_lane)
    elif release_gate:
        raise EvaluationError("sealed_blind_evaluation_required")

    contributions: dict[str, dict[str, dict[str, float | None]]] = {}
    lane_payloads: dict[str, dict[str, Any]] = {}
    for lane_name in LANES:
        dataset = datasets[lane_name]
        by_id = results_by_lane[lane_name]
        lane_contributions = {
            query_id: _query_metrics(gold_by_id[query_id], by_id[query_id])
            for query_id in query_ids
        }
        contributions[lane_name] = lane_contributions
        manifest_payload = dataset.manifest.model_dump(mode="json")
        manifest_bytes = _canonical_json_bytes(manifest_payload)
        lane_payloads[lane_name] = {
            "artifact_manifest": manifest_payload,
            "artifact_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "critical_counts": _lane_critical_counts(gold_by_id, by_id),
            "overall": _summarize(
                query_ids,
                lane_contributions,
                bootstrap_samples=bootstrap_samples,
                seed=bootstrap_seed,
                context=f"lane:{lane_name}:overall",
            ),
            "result_sha256": dataset.sha256,
            "result_size_bytes": dataset.size_bytes,
            "slices": {
                slice_name: _summarize(
                    ids,
                    lane_contributions,
                    bootstrap_samples=bootstrap_samples,
                    seed=bootstrap_seed,
                    context=f"lane:{lane_name}:slice:{slice_name}",
                )
                for slice_name, ids in slice_ids.items()
            },
        }
        if lane_name == "v109_baseline":
            baseline_traces = [
                result.v109_baseline
                for result in dataset.results
                if result.v109_baseline is not None
            ]
            if len(baseline_traces) != len(query_ids):  # pragma: no cover - model invariant
                raise EvaluationError("v109_dense_raw_trace_missing")
            lane_payloads[lane_name]["baseline_trace"] = {
                "dense_raw_query_count": len(baseline_traces),
                "rrf_k": 60,
            }

    comparison_specs = (
        ("qwen_page_vs_v109_baseline", "qwen_page", "v109_baseline"),
        (
            "qwen_structure_exact_vs_v109_baseline",
            "qwen_structure_exact",
            "v109_baseline",
        ),
        (
            "lexical_shadow_vs_qwen_structure_exact",
            "lexical_shadow",
            "qwen_structure_exact",
        ),
        (
            "reranker_shadow_vs_qwen_structure_exact",
            "reranker_shadow",
            "qwen_structure_exact",
        ),
    )
    comparison_payloads: dict[str, dict[str, Any]] = {}
    for name, candidate_name, reference_name in comparison_specs:
        comparison_payloads[name] = {
            "candidate_lane": candidate_name,
            "overall": _comparison_summary(
                query_ids,
                contributions[candidate_name],
                contributions[reference_name],
                bootstrap_samples=bootstrap_samples,
                seed=bootstrap_seed,
                context=f"comparison:{name}:overall",
            ),
            "reference_lane": reference_name,
            "slices": {
                slice_name: _comparison_summary(
                    ids,
                    contributions[candidate_name],
                    contributions[reference_name],
                    bootstrap_samples=bootstrap_samples,
                    seed=bootstrap_seed,
                    context=f"comparison:{name}:slice:{slice_name}",
                )
                for slice_name, ids in slice_ids.items()
            },
        }

    blind_payload: dict[str, Any]
    if blind_dataset is None:
        blind_payload = {"evaluated": False}
    else:
        blind_payload = _blind_evaluation_payload(
            blind_dataset,
            query_ids,
            slice_ids,
            bootstrap_samples=bootstrap_samples,
            seed=bootstrap_seed,
        )

    release_payload = _release_gate_payload(
        lane_payloads,
        comparison_payloads,
        blind_payload,
        tuple(slice_ids),
        evaluated=release_gate,
    )
    generation_manifest_sha256 = sorted(
        {dataset.manifest.generation_manifest_sha256 for dataset in datasets.values()}
    )
    payload: dict[str, Any] = {
        "artifact_bindings": {
            "blind_evaluation_sha256": None if blind_dataset is None else blind_dataset.sha256,
            "generation_manifest_sha256": generation_manifest_sha256,
            "gold_sha256": gold.sha256,
            "run_sha256": {lane_name: datasets[lane_name].sha256 for lane_name in LANES},
        },
        "blind_evaluation": blind_payload,
        "bootstrap": {
            "ci": 0.95,
            "method": "paired-query-percentile-pcg64",
            "samples": bootstrap_samples,
            "seed": bootstrap_seed,
        },
        "comparisons": comparison_payloads,
        "gold": {
            "query_count": len(gold.queries),
            "sha256": gold.sha256,
            "size_bytes": gold.size_bytes,
            "slice_counts": {slice_name: len(ids) for slice_name, ids in slice_ids.items()},
        },
        "lanes": lane_payloads,
        "release_gate": release_payload,
        "schema_version": REPORT_SCHEMA_VERSION,
    }
    canonical = _canonical_json_bytes(payload)
    return EvaluationReport(payload, canonical, hashlib.sha256(canonical).hexdigest())


def _parse_run_arguments(values: Sequence[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        lane, separator, path = value.partition("=")
        if not separator or lane not in LANES or not path or lane in result:
            raise EvaluationError("invalid_run_argument")
        result[lane] = Path(path)
    return result


def _read_canonical_report(path: Path) -> tuple[dict[str, Any], bytes, str]:
    data = _read_regular(path)
    if not data.endswith(b"\n") or data.endswith(b"\n\n"):
        raise EvaluationError("report_not_canonical_bytes")
    body = data[:-1]
    try:
        value = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except EvaluationError as exc:
        raise EvaluationError("report_not_canonical_bytes") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvaluationError("report_not_canonical_bytes") from exc
    if not isinstance(value, dict):
        raise EvaluationError("report_not_canonical_bytes")
    payload = cast(dict[str, Any], value)
    if body != _canonical_json_bytes(payload):
        raise EvaluationError("report_not_canonical_bytes")
    return payload, data, hashlib.sha256(data).hexdigest()


def _validate_generation_manifest_artifacts(
    expected_sha256: Sequence[str],
    directory: Path,
) -> None:
    absolute = _absolute_without_resolving(directory)
    try:
        listed = absolute.lstat()
    except FileNotFoundError:
        raise EvaluationError("generation_manifest_directory_missing") from None
    if stat.S_ISLNK(listed.st_mode) or not stat.S_ISDIR(listed.st_mode):
        raise EvaluationError("generation_manifest_directory_invalid")
    for expected in expected_sha256:
        artifact = absolute / f"{expected}.json"
        data = _read_regular(artifact)
        if hashlib.sha256(data).hexdigest() != expected:
            raise EvaluationError("generation_manifest_sha256_mismatch")


def validate_evaluation_report(
    report_path: Path,
    gold_path: Path,
    run_paths: Mapping[str, Path],
    blind_evaluation_path: Path,
    generation_manifest_directory: Path,
    *,
    expected_report_sha256: str,
    expected_source_commit: str | None = None,
    release_gate: bool = True,
    bootstrap_samples: int = 2_000,
    bootstrap_seed: int = 1010,
) -> EvaluationReport:
    """Recompute and byte-compare a sealed report and every input artifact offline."""

    if not _SHA256.fullmatch(expected_report_sha256):
        raise EvaluationError("report_sha256_invalid")
    _payload, report_bytes, report_sha256 = _read_canonical_report(report_path)
    if report_sha256 != expected_report_sha256:
        raise EvaluationError("report_sha256_mismatch")
    gold_sha256 = hashlib.sha256(_read_regular(gold_path)).hexdigest()
    expected = evaluate_gold_runs(
        gold_path,
        run_paths,
        blind_evaluation_path=blind_evaluation_path,
        release_gate=release_gate,
        expected_gold_sha256=gold_sha256,
        expected_source_commit=expected_source_commit,
        bootstrap_samples=bootstrap_samples,
        bootstrap_seed=bootstrap_seed,
    )
    if report_bytes != expected.canonical_bytes + b"\n":
        raise EvaluationError("report_recomputation_mismatch")
    if release_gate and expected.payload["release_gate"]["status"] != "passed":
        raise EvaluationError("release_gate_not_passed")
    bindings = cast(dict[str, Any], expected.payload["artifact_bindings"])
    generation_sha256 = cast(list[str], bindings["generation_manifest_sha256"])
    _validate_generation_manifest_artifacts(
        generation_sha256,
        generation_manifest_directory,
    )
    return expected


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate sealed CardRAG gold JSONL runs")
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--run", action="append", default=[], metavar="LANE=PATH")
    parser.add_argument("--blind-evaluation", type=Path)
    parser.add_argument("--expected-gold-sha256")
    parser.add_argument("--expected-source-commit")
    parser.add_argument("--validate-report", type=Path)
    parser.add_argument("--expected-report-sha256")
    parser.add_argument("--generation-manifest-dir", type=Path)
    parser.add_argument("--fixture-mode", action="store_true")
    parser.add_argument("--bootstrap-samples", type=int, default=2_000)
    parser.add_argument("--bootstrap-seed", type=int, default=1010)
    arguments = parser.parse_args(argv)
    try:
        runs = _parse_run_arguments(cast(list[str], arguments.run))
        validate_report = cast(Path | None, arguments.validate_report)
        blind_evaluation = cast(Path | None, arguments.blind_evaluation)
        generation_manifest_dir = cast(Path | None, arguments.generation_manifest_dir)
        expected_report_sha256 = cast(str | None, arguments.expected_report_sha256)
        expected_source_commit = cast(str | None, arguments.expected_source_commit)
        if validate_report is not None:
            if (
                cast(bool, arguments.fixture_mode)
                or blind_evaluation is None
                or generation_manifest_dir is None
                or expected_report_sha256 is None
                or expected_source_commit is None
                or cast(str | None, arguments.expected_gold_sha256) is not None
            ):
                raise EvaluationError("invalid_report_validation_arguments")
            report = validate_evaluation_report(
                validate_report,
                cast(Path, arguments.gold),
                runs,
                blind_evaluation,
                generation_manifest_dir,
                expected_report_sha256=expected_report_sha256,
                expected_source_commit=expected_source_commit,
                bootstrap_samples=cast(int, arguments.bootstrap_samples),
                bootstrap_seed=cast(int, arguments.bootstrap_seed),
            )
            receipt = {
                "report_sha256": hashlib.sha256(report.canonical_bytes + b"\n").hexdigest(),
                "schema_version": "cardrag.gold-evaluation-validation-receipt.v1",
                "status": "validated",
            }
            sys.stdout.buffer.write(_canonical_json_bytes(receipt) + b"\n")
            return 0
        if expected_report_sha256 is not None or generation_manifest_dir is not None:
            raise EvaluationError("invalid_evaluation_arguments")
        report = evaluate_gold_runs(
            cast(Path, arguments.gold),
            runs,
            blind_evaluation_path=blind_evaluation,
            release_gate=not cast(bool, arguments.fixture_mode),
            expected_gold_sha256=cast(str | None, arguments.expected_gold_sha256),
            expected_source_commit=expected_source_commit,
            bootstrap_samples=cast(int, arguments.bootstrap_samples),
            bootstrap_seed=cast(int, arguments.bootstrap_seed),
        )
    except EvaluationError as exc:
        error = {"line": exc.line, "reason_code": exc.code, "status": "failed"}
        print(json.dumps(error, sort_keys=True, separators=(",", ":")), file=sys.stderr)
        return 2
    sys.stdout.buffer.write(report.canonical_bytes + b"\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through ``main``
    raise SystemExit(main())
