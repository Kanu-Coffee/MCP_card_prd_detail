from __future__ import annotations

import hashlib
import sqlite3
from datetime import date
from pathlib import Path

import numpy as np
import pytest
from conftest import FakeEmbedder
from v5_fixtures import V5Fixture, build_v5_fixture, install_v5_fixture

import cardrag_mcp.exact as exact_module
from cardrag_mcp.compact import clause_group, encoded_response_bytes, seal_compact_page
from cardrag_mcp.launch_date import PARSER_VERSION
from cardrag_mcp.models import (
    ContractEvidenceBundle,
    ContractSearchRequest,
    ScoredEmbeddingView,
    ScoredStructureNode,
    SourceSpan,
    StructureNode,
    StructureNodeLink,
)
from cardrag_mcp.repository import ServingRepository
from cardrag_mcp.store import GenerationStore, load_generation_handle


@pytest.fixture
def runtime(
    tmp_path: Path,
) -> tuple[GenerationStore, ServingRepository, FakeEmbedder, V5Fixture]:
    store = GenerationStore(tmp_path / "state", maximum_vector_bytes=2 * 1024 * 1024)
    fixture, _ = install_v5_fixture(store)
    embedder = FakeEmbedder(np.eye(1, 4096, dtype=np.float32)[0])
    repository = ServingRepository(store, embedder, cursor_secret=b"compact-fixture-secret")
    return store, repository, embedder, fixture


def node(
    node_id: str, kind: str, parent: str | None, ordinal: int, text: str = ""
) -> StructureNode:
    table = {"table_cells": ("항목", "조건"), "table_role": "BODY"} if kind == "TABLE_ROW" else {}
    spans = (
        (
            SourceSpan(
                node_id=node_id,
                page=1,
                source_start=0,
                source_end=len(text),
                text_sha256=hashlib.sha256(text.encode()).hexdigest(),
                span_ordinal=0,
                text=text,
            ),
        )
        if text
        else ()
    )
    return StructureNode(
        node_id=node_id,
        contract_revision_id="revision-fixture",
        parent_id=parent,
        node_type=kind,
        major_class="NOTICE" if "notice" in node_id or kind == "FOOTNOTE" else "BENEFIT",
        ordinal=ordinal,
        display_text=text,
        spans=spans,
        **table,
    )


def link(source: StructureNode, target: str, kind: str) -> StructureNode:
    return source.model_copy(
        update={
            "links": (
                *source.links,
                StructureNodeLink(from_node_id=source.node_id, to_node_id=target, link_type=kind),
            )
        }
    )


def test_compact_group_preserves_table_transitive_footnotes_and_scoped_conditions() -> None:
    graph = (
        node("root", "ROOT", None, 0),
        node("section", "MAJOR_SECTION", "root", 1),
        node("item", "ITEM", "section", 2),
        node("table", "TABLE", "item", 3),
        node("row1", "TABLE_ROW", "table", 4, "월 최대 3회"),
        node("row2", "TABLE_ROW", "table", 5, "건당 10만원"),
        link(node("footnote1", "FOOTNOTE", "root", 6, "세금 제외"), "row1", "FOOTNOTE_OF"),
        link(
            node("footnote2", "FOOTNOTE", "root", 7, "제외조건 연속"),
            "footnote1",
            "CONTINUATION_OF",
        ),
        link(node("notice", "PARAGRAPH", "root", 8, "전월 30만원 이상"), "item", "APPLIES_TO"),
        link(node("unrelated", "PARAGRAPH", "section", 9, "다른 혜택"), "row1", "NEXT"),
        link(
            node("notice-global", "PARAGRAPH", "root", 10, "가족카드 합산"), "section", "APPLIES_TO"
        ),
    )
    result = clause_group(graph, "row1")
    ids = {item.node_id for item in result}
    assert ids == {item.node_id for item in graph} - {"unrelated"}
    assert {span.text for item in result for span in item.spans} == {
        "월 최대 3회",
        "건당 10만원",
        "세금 제외",
        "제외조건 연속",
        "전월 30만원 이상",
        "가족카드 합산",
    }
    assert all(
        edge.from_node_id in ids and edge.to_node_id in ids
        for item in result
        for edge in item.links
    )
    assert not clause_group(graph, "root")
    assert not clause_group(graph, "section")


@pytest.mark.asyncio
async def test_compact_real_search_and_legacy_wire_shape(runtime) -> None:
    _, repository, _, fixture = runtime
    legacy = await repository.search_contracts(ContractSearchRequest(query="혜택", issuer="kb"))
    compact = await repository.search_contracts(
        ContractSearchRequest(query="혜택", issuer="kb", response_mode="compact")
    )
    assert "compact" not in legacy.model_dump(mode="json")
    assert "catalog_resolution_hint" not in legacy.coverage.model_dump(mode="json")
    assert compact.compact is not None
    assert compact.compact.response_bytes == encoded_response_bytes(compact) <= 65_536
    assert len(compact.model_dump_json().encode()) <= compact.compact.response_bytes
    assert compact.coverage.scored_contracts == legacy.coverage.scored_contracts == 2
    assert compact.compact.full_contract_complete is False
    assert (
        compact.compact.candidate_count_semantics
        == "ranked_candidates_not_verified_condition_matches"
    )
    assert {bundle.contract.contract_revision_id for bundle in compact.bundles} == {
        fixture.current_revision_id,
        fixture.ambiguous_revision_id,
    }
    current = next(
        bundle
        for bundle in compact.bundles
        if bundle.contract.contract_revision_id == fixture.current_revision_id
    )
    assert any("전월 실적" in item.display_text for item in current.nodes)
    assert any(edge.link_type == "APPLIES_TO" for item in current.nodes for edge in item.links)
    limited = await repository.search_contracts(
        ContractSearchRequest(query="혜택", response_mode="compact", limit=1)
    )
    assert limited.compact.omitted_contracts == 1
    assert limited.compact.omitted_group_sample_complete is False


@pytest.mark.asyncio
async def test_compact_budget_keeps_oversized_group_atomic_and_tries_next_group(runtime) -> None:
    _, repository, _, _ = runtime
    page = await repository.search_contracts(
        ContractSearchRequest(query="혜택", response_mode="compact")
    )
    template = page.bundles[0]

    def make_group(name: str, text: str) -> ContractEvidenceBundle:
        value = node(name, "PARAGRAPH", None, 0, text)
        match = ScoredStructureNode(
            node=value,
            score=0.75,
            matched_view_types=("RAW_ITEM",),
            matched_views=(
                ScoredEmbeddingView(
                    row_index=0,
                    view_type="RAW_ITEM",
                    score=0.75,
                    display_text=text,
                    spans=value.spans,
                ),
            ),
        )
        return ContractEvidenceBundle(
            contract=template.contract,
            matches=(match,),
            nodes=(value,),
            linked_notice_count=0,
            parent_expansion_count=0,
        )

    oversized = make_group("large", "중요 제외조건 🧾" * 12_000)
    small = make_group("small", "전월 30만원 이상이며 세금은 제외됩니다.")
    sealed = seal_compact_page(
        page,
        [oversized, small],
        ranked_candidate_contracts=1,
        coarse_count=0,
        candidate_limit_count=0,
    )
    assert encoded_response_bytes(sealed) == sealed.compact.response_bytes <= 65_536
    assert [item.node_id for item in sealed.bundles[0].nodes] == ["small"]
    assert sealed.bundles[0].nodes[0].display_text == small.nodes[0].display_text
    assert sealed.compact.returned_clause_groups == 1
    assert sealed.compact.omitted_clause_groups == 1
    assert sealed.compact.omission_reasons == ("response_byte_limit",)
    assert sealed.compact.omitted_group_sample[0].endswith("/large")
    assert sealed.coverage.response_truncated is True
    empty = seal_compact_page(
        page,
        [oversized],
        ranked_candidate_contracts=1,
        coarse_count=0,
        candidate_limit_count=0,
    )
    assert empty.bundles == ()
    assert empty.compact.omitted_contracts == 1
    assert empty.compact.omitted_clause_groups == 1
    assert encoded_response_bytes(empty) <= 65_536
    # Exercise the actual UTF-8/JSON-escaping boundary, not only a large overflow.
    lower, upper = 1, 10_000
    while lower < upper:
        middle = (lower + upper + 1) // 2
        trial = seal_compact_page(
            page,
            [make_group("escaped", '가\\\n"' * middle)],
            ranked_candidate_contracts=1,
            coarse_count=0,
            candidate_limit_count=0,
        )
        if trial.bundles:
            lower = middle
        else:
            upper = middle - 1
    boundary = seal_compact_page(
        page,
        [make_group("escaped", '가\\\n"' * lower)],
        ranked_candidate_contracts=1,
        coarse_count=0,
        candidate_limit_count=0,
    )
    overflow = seal_compact_page(
        page,
        [make_group("escaped", '가\\\n"' * (lower + 1))],
        ranked_candidate_contracts=1,
        coarse_count=0,
        candidate_limit_count=0,
    )
    assert 63_000 < encoded_response_bytes(boundary) <= 65_536
    assert overflow.bundles == ()


@pytest.mark.asyncio
async def test_multiple_unrelated_product_names_remain_ambiguous(tmp_path: Path) -> None:
    store = GenerationStore(tmp_path / "state", maximum_vector_bytes=2 * 1024 * 1024)
    fixture = build_v5_fixture(store.generations / "gen-v5-exact")
    with sqlite3.connect(fixture.database) as connection:
        connection.execute(
            "UPDATE product_lineages SET name='Alpha Card' WHERE product_code='ALPHA'"
        )
        connection.execute(
            "UPDATE product_lineages SET name='Other Premium Business Card' "
            "WHERE product_code='BETA'"
        )
    store.activate(
        load_generation_handle(
            fixture.database.parent, store.objects, maximum_vector_bytes=store.maximum_vector_bytes
        )
    )
    repository = ServingRepository(
        store,
        FakeEmbedder(np.eye(1, 4096, dtype=np.float32)[0]),
        cursor_secret=b"ambiguous-product-fixture",
    )
    result = await repository.search_contracts(
        ContractSearchRequest(query="Alpha Card와 Other Premium Business Card 비교")
    )
    assert result.coverage.catalog_resolution_status == "ambiguous"
    assert result.coverage.catalog_candidate_count == 2
    assert result.coverage.catalog_resolved_product_lineage_id is None
    assert "product_lineage_ids" in result.coverage.catalog_resolution_hint
    assert len(result.bundles) == 2
    ids = tuple(bundle.contract.product_lineage_id for bundle in result.bundles)
    scoped = await repository.search_contracts(
        ContractSearchRequest(query="사업자 혜택", product_lineage_ids=ids)
    )
    assert scoped.coverage.catalog_resolution_status == "explicit_multiple"
    assert scoped.coverage.scored_contracts == 2
    assert {bundle.contract.product_lineage_id for bundle in scoped.bundles} == set(ids)


@pytest.mark.asyncio
async def test_launch_filter_and_generation_check_precede_embedding_and_scoring(runtime) -> None:
    store, repository, embedder, fixture = runtime
    with store.pin() as handle:
        revisions = repository.exact._active_revisions(handle, ContractSearchRequest(query="혜택"))
        for revision in revisions:
            repository.exact.metadata_cache.set(
                (
                    handle.generation_id,
                    revision.contract_revision_id,
                    revision.pdf_sha256,
                    "launch_date",
                    PARSER_VERSION,
                ),
                {
                    "launch_date": "2026-08-12"
                    if revision.contract_revision_id == fixture.current_revision_id
                    else None,
                    "status": "confirmed"
                    if revision.contract_revision_id == fixture.current_revision_id
                    else "missing",
                    "evidence": [],
                },
            )
        generation_id = handle.generation_id
    result = await repository.search_contracts(
        ContractSearchRequest(
            query="혜택",
            launch_start_date=date(2026, 8, 12),
            launch_end_date=date(2026, 8, 12),
            expected_generation_id=generation_id,
        )
    )
    assert result.coverage.expected_active_contracts == result.coverage.scored_contracts == 1
    assert result.coverage.expected_embedding_rows == result.coverage.scored_embedding_rows == 3
    assert [bundle.contract.contract_revision_id for bundle in result.bundles] == [
        fixture.current_revision_id
    ]
    # The other current revision's effective_date is in 2023; it cannot establish a launch.
    unknown = await repository.search_contracts(
        ContractSearchRequest(
            query="혜택",
            launch_start_date=date(2023, 1, 1),
            launch_end_date=date(2023, 12, 31),
        )
    )
    assert unknown.bundles == ()
    assert unknown.coverage.scored_embedding_rows == 0
    before = len(embedder.calls)
    with pytest.raises(ValueError, match="generation changed"):
        await repository.search_contracts(
            ContractSearchRequest(query="혜택", expected_generation_id="other-generation")
        )
    with pytest.raises(ValueError, match="do not exist"):
        await repository.search_contracts(
            ContractSearchRequest(query="혜택", product_lineage_ids=("missing",))
        )
    with pytest.raises(ValueError, match="future"):
        await repository.search_contracts(
            ContractSearchRequest(
                query="혜택", launch_start_date=date(2026, 8, 12), launch_end_date=date(2050, 8, 12)
            )
        )
    assert len(embedder.calls) == before


@pytest.mark.parametrize(
    ("texts", "included"),
    [
        (("출시일: 2026.08.12", "개정일: 2026.09.01"), True),
        (("출시일: 2026.08.99", "개정일: 2026.08.12"), False),
        (("출시일: 2026.08.12", "출시일: 2026.08.13"), False),
        (("출시일: 2050.08.12", "개정일: 2026.08.12"), False),
        (("출시일 미기재", "개정일: 2026.08.12"), False),
    ],
)
def test_launch_filter_resolves_source_then_reuses_negative_or_positive_cache(
    runtime,
    monkeypatch: pytest.MonkeyPatch,
    texts: tuple[str, str],
    included: bool,
) -> None:
    store, repository, _, fixture = runtime
    # Only this disposable metadata fixture is modified; production generations are immutable.
    with sqlite3.connect(fixture.database) as connection:
        for ordinal, text in zip((3, 5), texts, strict=True):
            connection.execute(
                "UPDATE structure_nodes SET display_text=? "
                "WHERE contract_revision_id=? AND ordinal=?",
                (text, fixture.current_revision_id, ordinal),
            )
    request = ContractSearchRequest(
        query="혜택", launch_start_date=date(2026, 8, 12), launch_end_date=date(2026, 9, 9)
    )
    with store.pin() as handle:
        revisions = repository.exact._active_revisions(handle, request)
        first = repository.exact._filter_launch_dates(handle, revisions, request)
        assert (
            fixture.current_revision_id in {item.contract_revision_id for item in first}
        ) == included

        def unexpected_reparse(_texts):
            raise AssertionError("unchanged launch metadata must be served from cache")

        monkeypatch.setattr(exact_module, "resolve_launch_date_details", unexpected_reparse)
        assert repository.exact._filter_launch_dates(handle, revisions, request) == first


@pytest.mark.parametrize(
    ("query", "expected"),
    [("my card premium 혜택", True), ("my card와 my card premium 비교", False)],
)
def test_nested_product_names_require_every_short_mention_to_be_contained(
    query: str, expected: bool
) -> None:
    assert exact_module._name_is_only_nested(query, "my card", ["my card premium"]) is expected


@pytest.mark.parametrize(
    "extra",
    [
        {"product_lineage_id": "a", "product_lineage_ids": ("b",)},
        {"product_lineage_ids": ()},
        {"product_lineage_ids": ("a", "a")},
        {"product_lineage_ids": tuple(f"id-{index}" for index in range(101))},
        {"launch_start_date": date(2026, 1, 1)},
        {"launch_start_date": date(2026, 2, 1), "launch_end_date": date(2026, 1, 1)},
        {
            "launch_start_date": date(2026, 1, 1),
            "launch_end_date": date(2026, 2, 1),
            "include_history": True,
        },
        {"response_mode": "typo"},
    ],
)
def test_new_request_contracts_fail_explicitly(extra: dict) -> None:
    with pytest.raises(ValueError):
        ContractSearchRequest(query="혜택", **extra)
