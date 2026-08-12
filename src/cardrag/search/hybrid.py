"""PostgreSQL FTS + pgvector HNSW fusion on one stable evidence ID."""

from __future__ import annotations

import math
from datetime import date
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cardrag.domain import Issuer
from cardrag.search.embeddings import EmbeddingProvider


class SearchFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    issuer: Issuer | None = None
    product_code: str | None = Field(default=None, max_length=128)
    section_type: str | None = Field(default=None, max_length=80)
    version: str | None = Field(default=None, max_length=128)
    as_of: date | None = None

    @model_validator(mode="after")
    def version_and_as_of_are_exclusive(self) -> SearchFilters:
        if self.version and self.as_of:
            raise ValueError("version and as_of cannot be combined")
        return self


class ExactSourceSpan(BaseModel):
    """One exact, page-local source fragment used to assemble evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    page: int = Field(ge=1)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def ordered(self) -> ExactSourceSpan:
        if self.end <= self.start:
            raise ValueError("exact source span end must be greater than start")
        return self


class SearchHit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    generation_id: str
    evidence_id: str
    issuer: Issuer
    product_code: str
    product_name: str
    document_id: str
    document_type: str
    effective_date: date
    source_version: str
    section_type: str
    page_start: int
    page_end: int
    span_start: int
    span_end: int
    source_spans: tuple[ExactSourceSpan, ...] = Field(min_length=1)
    text: str
    text_sha256: str
    pdf_sha256: str
    confidence: float
    score: float
    lexical_rank: int | None = None
    vector_rank: int | None = None

    @model_validator(mode="after")
    def exact_spans_match_envelope(self) -> SearchHit:
        ordered = tuple(sorted(self.source_spans, key=lambda item: (item.page, item.start, item.end)))
        if self.source_spans != ordered or len(set(self.source_spans)) != len(self.source_spans):
            raise ValueError("exact source spans must be unique and ordered")
        if (
            self.page_start != min(item.page for item in ordered)
            or self.page_end != max(item.page for item in ordered)
            or self.span_start != min(item.start for item in ordered)
            or self.span_end != max(item.end for item in ordered)
        ):
            raise ValueError("source span envelope does not match exact source spans")
        return self


class SearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    generation_id: str
    hits: tuple[SearchHit, ...]
    retrieval_mode: str
    degraded: bool
    failed_branch: str | None = None
    has_more: bool = False
    low_confidence: bool = False
    conflicting_versions: bool = False


class SearchStore(Protocol):
    async def active_generation_id(self) -> str: ...

    async def lexical_candidates(
        self, generation_id: str, query: str, filters: SearchFilters, limit: int
    ) -> list[dict[str, Any]]: ...

    async def vector_candidates(
        self, generation_id: str, vector: list[float], filters: SearchFilters, limit: int
    ) -> list[dict[str, Any]]: ...


def reciprocal_rank_fusion(
    lexical: list[dict[str, Any]],
    vector: list[dict[str, Any]],
    *,
    k: int = 60,
    lexical_weight: float = 1.0,
    vector_weight: float = 1.0,
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    scores: dict[str, float] = {}
    ranks: dict[str, tuple[int | None, int | None]] = {}
    for rank, row in enumerate(lexical, 1):
        evidence_id = str(row["evidence_id"])
        merged[evidence_id] = row
        scores[evidence_id] = scores.get(evidence_id, 0.0) + lexical_weight / (k + rank)
        ranks[evidence_id] = (rank, ranks.get(evidence_id, (None, None))[1])
    for rank, row in enumerate(vector, 1):
        evidence_id = str(row["evidence_id"])
        merged.setdefault(evidence_id, row)
        scores[evidence_id] = scores.get(evidence_id, 0.0) + vector_weight / (k + rank)
        ranks[evidence_id] = (ranks.get(evidence_id, (None, None))[0], rank)
    ordered = sorted(merged, key=lambda key: (-scores[key], key))
    result: list[dict[str, Any]] = []
    for evidence_id in ordered:
        row = dict(merged[evidence_id])
        row["score"] = scores[evidence_id]
        row["lexical_rank"], row["vector_rank"] = ranks[evidence_id]
        result.append(row)
    return result


class HybridSearchEngine:
    def __init__(
        self,
        store: SearchStore,
        embedder: EmbeddingProvider,
        *,
        maximum_candidates: int = 250,
    ) -> None:
        if maximum_candidates < 1:
            raise ValueError("maximum_candidates must be positive")
        self.store = store
        self.embedder = embedder
        self.maximum_candidates = maximum_candidates

    async def search(
        self,
        query: str,
        *,
        filters: SearchFilters | None = None,
        limit: int = 10,
        offset: int = 0,
        expected_generation_id: str | None = None,
        allow_degraded: bool = False,
    ) -> SearchResponse:
        query = query.strip()
        if not query or len(query) > 2000:
            raise ValueError("query must contain 1..2000 characters")
        if limit < 1 or limit > 100:
            raise ValueError("limit must be 1..100")
        if offset < 0 or offset >= self.maximum_candidates * 2:
            raise ValueError("search offset is outside the bounded candidate window")
        filters = filters or SearchFilters()
        generation_id = await self.store.active_generation_id()  # pin once for entire request
        if expected_generation_id is not None and generation_id != expected_generation_id:
            raise ValueError("search cursor belongs to a stale generation")
        # Rank one fixed candidate window on every page. Growing the branch
        # limits with ``offset`` would change RRF ranks between requests and
        # could duplicate or skip evidence behind an otherwise valid cursor.
        candidate_limit = self.maximum_candidates
        lexical = await self.store.lexical_candidates(generation_id, query, filters, candidate_limit)
        try:
            # Exactly one embedding call; the resulting vector is reused by the vector branch.
            query_vector = await self.embedder.embed_query(query)
            vector = await self.store.vector_candidates(generation_id, query_vector, filters, candidate_limit)
        except Exception:
            if not allow_degraded:
                raise
            fused = reciprocal_rank_fusion(lexical, [])
            page = fused[offset : offset + limit]
            low_confidence, conflicting_versions = self._search_states(fused)
            return SearchResponse(
                generation_id=generation_id,
                hits=tuple(self._hit(generation_id, row) for row in page),
                retrieval_mode="lexical_only",
                degraded=True,
                failed_branch="vector",
                has_more=len(fused) > offset + limit,
                low_confidence=low_confidence,
                conflicting_versions=conflicting_versions,
            )
        fused = reciprocal_rank_fusion(lexical, vector)
        page = fused[offset : offset + limit]
        low_confidence, conflicting_versions = self._search_states(fused)
        return SearchResponse(
            generation_id=generation_id,
            hits=tuple(self._hit(generation_id, row) for row in page),
            retrieval_mode="hybrid",
            degraded=False,
            has_more=len(fused) > offset + limit,
            low_confidence=low_confidence,
            conflicting_versions=conflicting_versions,
        )

    @staticmethod
    def _hit(generation_id: str, row: dict[str, Any]) -> SearchHit:
        payload = {key: value for key, value in row.items() if key in SearchHit.model_fields}
        payload["generation_id"] = generation_id
        return SearchHit.model_validate(payload)

    @staticmethod
    def _search_states(rows: list[dict[str, Any]]) -> tuple[bool, bool]:
        low_confidence = any(float(row["confidence"]) < 0.7 for row in rows)
        versions: dict[tuple[str, str, str], set[tuple[str, str]]] = {}
        for row in rows:
            issuer = row["issuer"]
            issuer_value = issuer.value if isinstance(issuer, Issuer) else str(issuer)
            key = (
                issuer_value,
                str(row["product_code"]),
                str(row["document_type"]),
            )
            versions.setdefault(key, set()).add((str(row["document_id"]), str(row["source_version"])))
        return low_confidence, any(len(values) > 1 for values in versions.values())


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise ValueError("dimension mismatch")
    denominator = math.sqrt(sum(v * v for v in left)) * math.sqrt(sum(v * v for v in right))
    return sum(a * b for a, b in zip(left, right, strict=True)) / denominator if denominator else 0.0
