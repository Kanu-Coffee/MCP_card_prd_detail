from __future__ import annotations

import hashlib
from datetime import date
from typing import Any

import pytest

from cardrag.app import UnavailableEmbeddingProvider
from cardrag.domain import Issuer
from cardrag.search.embeddings import EmbeddingError, FakeEmbeddingProvider
from cardrag.search.hybrid import HybridSearchEngine, SearchFilters, reciprocal_rank_fusion


def _row(evidence_id: str, *, issuer: Issuer = Issuer.WOORI) -> dict[str, Any]:
    text = f"{evidence_id} 원문 근거"
    return {
        "evidence_id": evidence_id,
        "issuer": issuer,
        "product_code": "CARD-1",
        "product_name": "합성 카드",
        "document_id": "document-1",
        "document_type": "product_description",
        "effective_date": date(2026, 8, 12),
        "source_version": "v1",
        "section_type": "benefit",
        "page_start": 1,
        "page_end": 1,
        "span_start": 10,
        "span_end": 30,
        "source_spans": [
            {
                "page": 1,
                "start": 10,
                "end": 30,
                "quote_sha256": hashlib.sha256(text.encode()).hexdigest(),
            }
        ],
        "text": text,
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "pdf_sha256": "b" * 64,
        "confidence": 1.0,
    }


class _RecordingStore:
    def __init__(
        self,
        *,
        lexical: list[dict[str, Any]],
        vector: list[dict[str, Any]],
        vector_error: Exception | None = None,
    ) -> None:
        self.lexical = lexical
        self.vector = vector
        self.vector_error = vector_error
        self.active_calls = 0
        self.lexical_calls: list[tuple[str, str, SearchFilters, int]] = []
        self.vector_calls: list[tuple[str, list[float], SearchFilters, int]] = []

    async def active_generation_id(self) -> str:
        self.active_calls += 1
        return "generation-fixture"

    async def lexical_candidates(
        self, generation_id: str, query: str, filters: SearchFilters, limit: int
    ) -> list[dict[str, Any]]:
        self.lexical_calls.append((generation_id, query, filters, limit))
        return self.lexical

    async def vector_candidates(
        self, generation_id: str, vector: list[float], filters: SearchFilters, limit: int
    ) -> list[dict[str, Any]]:
        self.vector_calls.append((generation_id, vector, filters, limit))
        if self.vector_error is not None:
            raise self.vector_error
        return self.vector


def test_rrf_fuses_only_on_common_evidence_id() -> None:
    lexical = [_row("lexical-first"), _row("shared")]
    vector = [_row("shared"), _row("vector-only")]

    fused = reciprocal_rank_fusion(lexical, vector)

    assert [row["evidence_id"] for row in fused] == ["shared", "lexical-first", "vector-only"]
    assert fused[0]["lexical_rank"] == 2
    assert fused[0]["vector_rank"] == 1
    assert len({row["evidence_id"] for row in fused}) == 3


async def test_missing_embedding_secret_preserves_generation_contract_for_explicit_degradation() -> None:
    embedder = UnavailableEmbeddingProvider(model="configured-model", dimension=1536)
    assert (embedder.provider, embedder.model, embedder.dimension) == (
        "openrouter",
        "configured-model",
        1536,
    )
    with pytest.raises(EmbeddingError, match="not configured"):
        await embedder.embed_query("query")


async def test_hybrid_pins_generation_embeds_query_once_and_passes_identical_filters() -> None:
    store = _RecordingStore(
        lexical=[_row("lexical-first"), _row("shared")],
        vector=[_row("shared"), _row("vector-only")],
    )
    embedder = FakeEmbeddingProvider(dimension=8)
    engine = HybridSearchEngine(store, embedder)
    filters = SearchFilters(issuer=Issuer.WOORI, section_type="benefit", version="v1")

    response = await engine.search("대중교통 할인", filters=filters, limit=3)

    assert response.generation_id == "generation-fixture"
    assert response.hits[0].evidence_id == "shared"
    assert response.hits[0].lexical_rank == 2
    assert response.hits[0].vector_rank == 1
    assert response.retrieval_mode == "hybrid"
    assert response.degraded is False
    assert store.active_calls == 1
    assert embedder.query_calls == 1
    assert store.lexical_calls == [("generation-fixture", "대중교통 할인", filters, 250)]
    assert len(store.vector_calls) == 1
    assert store.vector_calls[0][0] == "generation-fixture"
    assert store.vector_calls[0][2:] == (filters, 250)


async def test_hybrid_pages_inside_one_pinned_generation_without_truncating_before_offset() -> None:
    rows = [_row(f"evidence-{index}") for index in range(6)]
    store = _RecordingStore(lexical=rows, vector=rows)
    embedder = FakeEmbeddingProvider(dimension=8)
    engine = HybridSearchEngine(store, embedder)

    first = await engine.search("혜택", limit=2)
    second = await engine.search(
        "혜택",
        limit=2,
        offset=2,
        expected_generation_id=first.generation_id,
    )

    assert [hit.evidence_id for hit in first.hits] == ["evidence-0", "evidence-1"]
    assert [hit.evidence_id for hit in second.hits] == ["evidence-2", "evidence-3"]
    assert first.has_more is True
    assert second.has_more is True
    assert embedder.query_calls == 2  # exactly once for each stateless page request
    assert store.active_calls == 2
    assert [call[3] for call in store.lexical_calls] == [250, 250]
    assert [call[3] for call in store.vector_calls] == [250, 250]


async def test_hybrid_states_cover_the_whole_ranked_window_not_only_the_current_page() -> None:
    rows = [
        _row("new"),
        {
            **_row("old"),
            "document_id": "document-old",
            "source_version": "v0",
            "confidence": 0.6,
        },
    ]
    store = _RecordingStore(lexical=rows, vector=rows)
    engine = HybridSearchEngine(store, FakeEmbeddingProvider(dimension=8))

    first = await engine.search("혜택", limit=1)

    assert [hit.evidence_id for hit in first.hits] == ["new"]
    assert first.low_confidence is True
    assert first.conflicting_versions is True


async def test_hybrid_rejects_cursor_generation_after_publish_switch_before_searching() -> None:
    store = _RecordingStore(lexical=[_row("evidence")], vector=[_row("evidence")])

    async def changed_generation() -> str:
        store.active_calls += 1
        return "new-generation"

    store.active_generation_id = changed_generation  # type: ignore[method-assign]
    embedder = FakeEmbeddingProvider(dimension=8)
    engine = HybridSearchEngine(store, embedder)

    with pytest.raises(ValueError, match="stale generation"):
        await engine.search("혜택", expected_generation_id="old-generation")

    assert store.lexical_calls == []
    assert store.vector_calls == []
    assert embedder.query_calls == 0


async def test_query_embedding_degradation_requires_explicit_opt_in() -> None:
    store = _RecordingStore(lexical=[_row("lexical")], vector=[])
    embedder = FakeEmbeddingProvider(dimension=8, fail_queries=True)
    engine = HybridSearchEngine(store, embedder)

    with pytest.raises(EmbeddingError, match="injected"):
        await engine.search("전월실적")

    response = await engine.search("전월실적", allow_degraded=True)
    assert response.degraded is True
    assert response.retrieval_mode == "lexical_only"
    assert response.failed_branch == "vector"
    assert [hit.evidence_id for hit in response.hits] == ["lexical"]


async def test_vector_store_degradation_requires_explicit_opt_in() -> None:
    store = _RecordingStore(
        lexical=[_row("lexical")],
        vector=[],
        vector_error=RuntimeError("injected vector search outage"),
    )
    engine = HybridSearchEngine(store, FakeEmbeddingProvider(dimension=8))

    with pytest.raises(RuntimeError, match="vector search outage"):
        await engine.search("전월실적")

    response = await engine.search("전월실적", allow_degraded=True)
    assert response.degraded is True
    assert response.retrieval_mode == "lexical_only"
    assert response.failed_branch == "vector"
    assert [hit.evidence_id for hit in response.hits] == ["lexical"]
