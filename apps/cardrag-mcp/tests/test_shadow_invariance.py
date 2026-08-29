from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from conftest import FakeEmbedder
from v5_fixtures import install_v5_fixture

from cardrag_mcp.models import ContractSearchPage, ContractSearchRequest
from cardrag_mcp.repository import ServingRepository
from cardrag_mcp.store import GenerationStore


def _primary_result(
    page: ContractSearchPage,
) -> list[tuple[str, list[tuple[str, float, tuple[str, ...]]]]]:
    bundles = page.bundles
    return [
        (
            bundle.contract.contract_revision_id,
            [
                (
                    match.node.node_id,
                    match.score,
                    tuple(match.matched_view_types),
                )
                for match in bundle.matches
            ],
        )
        for bundle in bundles
    ]


@pytest.mark.asyncio
async def test_lexical_shadow_cannot_change_dense_contract_or_evidence_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = GenerationStore(tmp_path / "state", maximum_vector_bytes=2 * 1024 * 1024)
    fixture, _ = install_v5_fixture(store)
    query = np.zeros((4096,), dtype=np.float32)
    query[0] = 1.0
    repository = ServingRepository(
        store,
        FakeEmbedder(query),
        cursor_secret=b"lexical-shadow-invariance-secret",
        maximum_candidates=20,
    )
    request = ContractSearchRequest(query="lexical shadow invariant", limit=10)

    monkeypatch.setattr(
        repository.exact,
        "_lexical_additions",
        lambda _handle, _query, _active, _selected: {},
    )
    dense_only = await repository.search_contracts(request)

    monkeypatch.setattr(
        repository.exact,
        "_lexical_additions",
        lambda _handle, _query, _active, _selected: {
            fixture.current_revision_id: {f"{fixture.current_revision_id}-root"}
        },
    )
    lexical_shadow = await repository.search_contracts(request)

    assert _primary_result(lexical_shadow) == _primary_result(dense_only)
    assert dense_only.coverage.lexical_additional_evidence_count == 0
    assert lexical_shadow.coverage.lexical_additional_evidence_count == 1
    assert lexical_shadow.coverage.approximate is False
    assert lexical_shadow.coverage.lexical_influenced_ranking is False
    assert lexical_shadow.coverage.reranker_influenced_ranking is False
    assert lexical_shadow.coverage.expected_embedding_rows == 4
    assert lexical_shadow.coverage.scored_embedding_rows == 4
