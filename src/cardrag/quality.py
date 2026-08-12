"""Deterministic quality gates for OCR, structure and retrieval fixtures.

The production pipeline deliberately keeps measurement separate from model
execution.  This module therefore accepts expected and observed artefacts and
returns a versioned, serialisable result that can be used both by fast CI
fixtures and by an operator-supplied live corpus.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cardrag.domain import Issuer
from cardrag.pipeline.ocr import PAGE_MARKER
from cardrag.pipeline.structure import SectionType, StructuredDocument, validate_facts

QUALITY_REPORT_SCHEMA = "cardrag-quality-evaluation.v1"
RETRIEVAL_REPORT_SCHEMA = "cardrag-retrieval-evaluation.v1"

# Numbers are evaluated together with units and boundary/negation terms.  A
# missing or additional token is a critical error rather than an averageable
# character-level difference.
CRITICAL_PATTERN = re.compile(
    r"(?:\d[\d,]*(?:\.\d+)?\s*(?:원|만원|천원|%|개월|일|회|건|명|포인트|마일)|"
    r"이상|초과|이하|미만|제외|미포함|포함하지\s*않(?:음|습니다)|제공하지\s*않(?:음|습니다))"
)


class QualityThresholds(BaseModel):
    """Provisional v1 gates selected in ADR-0003."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    character_accuracy: float = Field(default=0.995, ge=0, le=1)
    page_coverage: float = Field(default=1.0, ge=0, le=1)
    critical_token_recall: float = Field(default=1.0, ge=0, le=1)
    taxonomy_recall: float = Field(default=0.95, ge=0, le=1)
    source_span_accuracy: float = Field(default=1.0, ge=0, le=1)
    retrieval_recall_at_k: float = Field(default=0.95, ge=0, le=1)
    critical_recall_at_k: float = Field(default=1.0, ge=0, le=1)
    mean_reciprocal_rank: float = Field(default=0.90, ge=0, le=1)
    ndcg_at_k: float = Field(default=0.90, ge=0, le=1)
    filter_accuracy: float = Field(default=1.0, ge=0, le=1)


class OCRQualityResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    character_accuracy: float = Field(ge=0, le=1)
    page_coverage: float = Field(ge=0, le=1)
    page_order_exact: bool
    critical_token_recall: float = Field(ge=0, le=1)
    missing_critical_tokens: tuple[str, ...]
    unexpected_critical_tokens: tuple[str, ...]
    critical_error_count: int = Field(ge=0)
    status: str


class StructureQualityResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    taxonomy_recall: float = Field(ge=0, le=1)
    source_span_accuracy: float = Field(ge=0, le=1)
    critical_token_recall: float = Field(ge=0, le=1)
    missing_section_types: tuple[str, ...]
    critical_error_count: int = Field(ge=0)
    status: str


class RankedEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str
    issuer: Issuer
    product_code: str
    source_version: str
    section_type: str


class GoldQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query_id: str
    query: str
    relevant_evidence_ids: tuple[str, ...] = Field(min_length=1)
    issuer: Issuer | None = None
    product_code: str | None = None
    source_version: str | None = None
    section_type: str | None = None
    critical: bool = False


class RetrievalQualityResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query_count: int = Field(gt=0)
    k: int = Field(gt=0)
    recall_at_k: float = Field(ge=0, le=1)
    critical_recall_at_k: float = Field(ge=0, le=1)
    mean_reciprocal_rank: float = Field(ge=0, le=1)
    ndcg_at_k: float = Field(ge=0, le=1)
    filter_accuracy: float = Field(ge=0, le=1)
    issuer_collision_count: int = Field(ge=0)
    failed_query_ids: tuple[str, ...]
    status: str


class FixtureQualityReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = QUALITY_REPORT_SCHEMA
    generated_at: datetime
    fixture_set: str
    fixture_sha256: str
    scope: str
    thresholds: QualityThresholds
    ocr: Mapping[str, OCRQualityResult]
    structure: Mapping[str, StructureQualityResult]
    retrieval: RetrievalQualityResult
    status: str
    limitations: tuple[str, ...]

    @model_validator(mode="after")
    def status_matches_children(self) -> Self:
        passed = (
            all(result.status == "passed" for result in self.ocr.values())
            and all(result.status == "passed" for result in self.structure.values())
            and self.retrieval.status == "passed"
        )
        if (self.status == "passed") != passed:
            raise ValueError("aggregate quality status does not match child gates")
        return self


def critical_tokens(text: str) -> tuple[str, ...]:
    return tuple(re.sub(r"\s+", " ", match.group(0)).strip() for match in CRITICAL_PATTERN.finditer(text))


def _multiset_difference(left: Sequence[str], right: Sequence[str]) -> tuple[str, ...]:
    difference = Counter(left) - Counter(right)
    return tuple(sorted(token for token, count in difference.items() for _ in range(count)))


def _levenshtein(left: str, right: str) -> int:
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for left_index, left_char in enumerate(left, 1):
        current = [left_index]
        for right_index, right_char in enumerate(right, 1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left_char != right_char),
                )
            )
        previous = current
    return previous[-1]


def evaluate_ocr(
    expected: str,
    observed: str,
    *,
    thresholds: QualityThresholds | None = None,
) -> OCRQualityResult:
    thresholds = thresholds or QualityThresholds()
    denominator = max(len(expected), len(observed), 1)
    character_accuracy = max(0.0, 1.0 - _levenshtein(expected, observed) / denominator)
    expected_pages = tuple(int(page) for page in PAGE_MARKER.findall(expected))
    observed_pages = tuple(int(page) for page in PAGE_MARKER.findall(observed))
    expected_page_set = set(expected_pages)
    page_coverage = (
        len(expected_page_set.intersection(observed_pages)) / len(expected_page_set)
        if expected_page_set
        else float(not observed_pages)
    )
    expected_critical = critical_tokens(expected)
    observed_critical = critical_tokens(observed)
    missing = _multiset_difference(expected_critical, observed_critical)
    unexpected = _multiset_difference(observed_critical, expected_critical)
    critical_recall = 1.0 - len(missing) / max(len(expected_critical), 1)
    critical_errors = len(missing) + len(unexpected)
    passed = (
        character_accuracy >= thresholds.character_accuracy
        and page_coverage >= thresholds.page_coverage
        and expected_pages == observed_pages
        and critical_recall >= thresholds.critical_token_recall
        and critical_errors == 0
    )
    return OCRQualityResult(
        character_accuracy=character_accuracy,
        page_coverage=page_coverage,
        page_order_exact=expected_pages == observed_pages,
        critical_token_recall=critical_recall,
        missing_critical_tokens=missing,
        unexpected_critical_tokens=unexpected,
        critical_error_count=critical_errors,
        status="passed" if passed else "failed",
    )


def evaluate_structure(
    expected_ocr: str,
    observed: StructuredDocument,
    *,
    expected_section_types: Sequence[SectionType],
    thresholds: QualityThresholds | None = None,
) -> StructureQualityResult:
    thresholds = thresholds or QualityThresholds()
    span_errors = 0
    try:
        validate_facts(expected_ocr, observed.facts)
    except ValueError:
        span_errors = 1
    observed_sections = {fact.section_type for fact in observed.facts}
    expected_sections = set(expected_section_types)
    missing_sections = tuple(sorted(section.value for section in expected_sections - observed_sections))
    taxonomy_recall = len(expected_sections & observed_sections) / max(len(expected_sections), 1)
    structured_text = "\n".join(fact.value for fact in observed.facts)
    expected_critical = critical_tokens(expected_ocr)
    observed_critical = critical_tokens(structured_text)
    missing_critical = _multiset_difference(expected_critical, observed_critical)
    critical_recall = 1.0 - len(missing_critical) / max(len(expected_critical), 1)
    source_span_accuracy = 1.0 if not span_errors else 0.0
    critical_errors = span_errors + len(missing_critical)
    passed = (
        taxonomy_recall >= thresholds.taxonomy_recall
        and source_span_accuracy >= thresholds.source_span_accuracy
        and critical_recall >= thresholds.critical_token_recall
        and critical_errors == 0
    )
    return StructureQualityResult(
        taxonomy_recall=taxonomy_recall,
        source_span_accuracy=source_span_accuracy,
        critical_token_recall=critical_recall,
        missing_section_types=missing_sections,
        critical_error_count=critical_errors,
        status="passed" if passed else "failed",
    )


def _dcg(relevance: Sequence[int]) -> float:
    return sum(value / math.log2(index + 2) for index, value in enumerate(relevance))


def evaluate_retrieval(
    gold_queries: Sequence[GoldQuery],
    rankings: Mapping[str, Sequence[RankedEvidence]],
    *,
    k: int = 10,
    thresholds: QualityThresholds | None = None,
) -> RetrievalQualityResult:
    if not gold_queries:
        raise ValueError("at least one gold query is required")
    if k < 1:
        raise ValueError("k must be positive")
    thresholds = thresholds or QualityThresholds()
    recalls: list[float] = []
    critical_recalls: list[float] = []
    reciprocal_ranks: list[float] = []
    ndcgs: list[float] = []
    filter_checks = 0
    filter_passes = 0
    collisions = 0
    failed: list[str] = []
    for gold in gold_queries:
        ranked = list(rankings.get(gold.query_id, ()))[:k]
        relevant = set(gold.relevant_evidence_ids)
        returned_ids = [item.evidence_id for item in ranked]
        hits = [index for index, evidence_id in enumerate(returned_ids, 1) if evidence_id in relevant]
        recall = len(relevant.intersection(returned_ids)) / len(relevant)
        recalls.append(recall)
        if gold.critical:
            critical_recalls.append(recall)
        reciprocal_ranks.append(1.0 / hits[0] if hits else 0.0)
        relevance = [int(evidence_id in relevant) for evidence_id in returned_ids]
        ideal = [1] * min(len(relevant), k) + [0] * max(0, len(relevance) - len(relevant))
        ideal_score = _dcg(ideal[: len(relevance)])
        ndcgs.append(_dcg(relevance) / ideal_score if ideal_score else 0.0)
        for item in ranked:
            checks = (
                ("issuer", gold.issuer, item.issuer),
                ("product_code", gold.product_code, item.product_code),
                ("source_version", gold.source_version, item.source_version),
                ("section_type", gold.section_type, item.section_type),
            )
            for field_name, expected, actual in checks:
                if expected is not None:
                    filter_checks += 1
                    if str(expected) == str(actual):
                        filter_passes += 1
                    elif field_name == "issuer":
                        collisions += 1
        if recall < (thresholds.critical_recall_at_k if gold.critical else thresholds.retrieval_recall_at_k):
            failed.append(gold.query_id)
    recall_at_k = sum(recalls) / len(recalls)
    critical_recall = min(critical_recalls, default=1.0)
    mrr = sum(reciprocal_ranks) / len(reciprocal_ranks)
    ndcg = sum(ndcgs) / len(ndcgs)
    filter_accuracy = filter_passes / filter_checks if filter_checks else 1.0
    passed = (
        recall_at_k >= thresholds.retrieval_recall_at_k
        and critical_recall >= thresholds.critical_recall_at_k
        and mrr >= thresholds.mean_reciprocal_rank
        and ndcg >= thresholds.ndcg_at_k
        and filter_accuracy >= thresholds.filter_accuracy
        and collisions == 0
        and not failed
    )
    return RetrievalQualityResult(
        query_count=len(gold_queries),
        k=k,
        recall_at_k=recall_at_k,
        critical_recall_at_k=critical_recall,
        mean_reciprocal_rank=mrr,
        ndcg_at_k=ndcg,
        filter_accuracy=filter_accuracy,
        issuer_collision_count=collisions,
        failed_query_ids=tuple(failed),
        status="passed" if passed else "failed",
    )


def new_fixture_report(
    *,
    fixture_set: str,
    fixture_sha256: str,
    ocr: Mapping[str, OCRQualityResult],
    structure: Mapping[str, StructureQualityResult],
    retrieval: RetrievalQualityResult,
    thresholds: QualityThresholds | None = None,
) -> FixtureQualityReport:
    thresholds = thresholds or QualityThresholds()
    passed = (
        all(result.status == "passed" for result in ocr.values())
        and all(result.status == "passed" for result in structure.values())
        and retrieval.status == "passed"
    )
    return FixtureQualityReport(
        generated_at=datetime.now(UTC),
        fixture_set=fixture_set,
        fixture_sha256=fixture_sha256,
        scope="synthetic three-issuer development gate; no live provider claim",
        thresholds=thresholds,
        ocr=ocr,
        structure=structure,
        retrieval=retrieval,
        status="passed" if passed else "failed",
        limitations=(
            "Synthetic fixtures validate contracts and regressions, not live issuer layout coverage.",
            "Deterministic fake OCR and embeddings exercise the real pipeline but do not establish external model quality or production latency.",
        ),
    )
