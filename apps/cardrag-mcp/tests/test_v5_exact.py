from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date
from pathlib import Path

import numpy as np
import pytest
from conftest import FakeEmbedder, create_database, unit_vector
from v5_fixtures import V5Fixture, build_v5_fixture, install_v5_fixture

import cardrag_mcp.exact as exact_module
from cardrag_mcp.audit import ExhaustiveAuditError
from cardrag_mcp.embeddings import EmbeddingUnavailable
from cardrag_mcp.models import ContractSearchRequest, SearchCoverage, SearchRequest
from cardrag_mcp.repository import ServingRepository
from cardrag_mcp.schema_v5 import LoadedVectorsV5, ServingDatabaseV5Error
from cardrag_mcp.store import GenerationHandle, GenerationStore, cas_path, load_generation_handle


@pytest.fixture
def v5_runtime(
    tmp_path: Path,
) -> tuple[GenerationStore, ServingRepository, FakeEmbedder, V5Fixture]:
    store = GenerationStore(tmp_path / "state", maximum_vector_bytes=2 * 1024 * 1024)
    fixture, _ = install_v5_fixture(store)
    query = np.zeros((4096,), dtype=np.float32)
    query[0] = 1.0
    embedder = FakeEmbedder(query)
    repository = ServingRepository(
        store,
        embedder,
        cursor_secret=b"v5-exact-test-cursor-secret-value",
        maximum_candidates=20,
    )
    return store, repository, embedder, fixture


def test_exporter_compatible_v5_fixture_loads_bound_sidecar_and_full_coverage(
    tmp_path: Path,
) -> None:
    fixture = build_v5_fixture(tmp_path / "gen-v5-exact")
    handle = load_generation_handle(
        fixture.database.parent,
        tmp_path / "objects",
        maximum_vector_bytes=2 * 1024 * 1024,
    )

    assert handle.metadata.schema_id == "cardrag.serving-db.v5"
    assert handle.metadata.embedding_dimension == 4096
    assert handle.metadata.embedding_count == fixture.vector_count == 5
    assert handle.vector_sidecar_path == fixture.vectors.resolve()
    assert isinstance(handle.vectors, LoadedVectorsV5)
    assert handle.vectors.matrix.shape == (5, 4096)
    assert handle.vectors.matrix.dtype == np.float32
    assert np.allclose(handle.vectors.norms, 1.0)
    with handle.connect() as connection:
        metadata = dict(connection.execute("SELECT key,value FROM metadata"))
        assert metadata["source_non_whitespace_count"] == metadata["covered_non_whitespace_count"]
        assert (
            connection.execute(
                "SELECT count(*) FROM revision_coverage "
                "WHERE source_non_whitespace_count=covered_non_whitespace_count"
            ).fetchone()[0]
            == 3
        )


def test_v5_schema_rejects_partial_or_cardinality_invalid_sealed_aggregation(
    tmp_path: Path,
) -> None:
    fixture = build_v5_fixture(
        tmp_path / "gen-v5-aggregation-schema",
        generation_id="gen-v5-aggregation-schema",
    )
    baseline = load_generation_handle(
        fixture.database.parent,
        tmp_path / "objects",
        maximum_vector_bytes=2 * 1024 * 1024,
    )
    assert baseline.metadata.exact_row_corpus_sha256 is not None
    with sqlite3.connect(fixture.database) as connection:
        connection.executemany(
            "INSERT INTO metadata(key,value) VALUES(?,?)",
            (
                ("document_aggregation_status", "sealed"),
                ("document_aggregation_policy", "top3_mean"),
                ("sealed_profile_sha256", "d" * 64),
                (
                    "exact_row_corpus_sha256",
                    baseline.metadata.exact_row_corpus_sha256,
                ),
            ),
        )
        connection.commit()

    with pytest.raises(ServingDatabaseV5Error, match="one CONTRACT"):
        load_generation_handle(
            fixture.database.parent,
            tmp_path / "objects",
            maximum_vector_bytes=2 * 1024 * 1024,
        )

    with sqlite3.connect(fixture.database) as connection:
        connection.execute("DELETE FROM metadata WHERE key='sealed_profile_sha256'")
        connection.commit()
    with pytest.raises(ServingDatabaseV5Error, match="metadata is incomplete"):
        load_generation_handle(
            fixture.database.parent,
            tmp_path / "objects",
            maximum_vector_bytes=2 * 1024 * 1024,
        )


def test_v5_loader_separates_sidecar_file_cap_from_legacy_and_resident_caps(
    tmp_path: Path,
) -> None:
    fixture = build_v5_fixture(
        tmp_path / "gen-v5-capacity",
        generation_id="gen-v5-capacity",
    )
    sidecar_size = fixture.vectors.stat().st_size

    handle = load_generation_handle(
        fixture.database.parent,
        tmp_path / "objects",
        maximum_vector_bytes=1,
        maximum_vector_sidecar_bytes=sidecar_size,
        maximum_resident_vector_bytes=fixture.vector_count * 4,
    )

    assert isinstance(handle.vectors.matrix, np.memmap)
    assert handle.vectors.matrix.nbytes == sidecar_size
    assert handle.vectors.norms.nbytes == fixture.vector_count * 4
    with pytest.raises(ServingDatabaseV5Error, match="promotion limit"):
        load_generation_handle(
            fixture.database.parent,
            tmp_path / "objects",
            maximum_vector_bytes=sidecar_size,
            maximum_vector_sidecar_bytes=sidecar_size - 1,
            maximum_resident_vector_bytes=fixture.vector_count * 4,
        )


def test_store_resident_bytes_exclude_v5_mmap_but_include_norms(
    v5_runtime: tuple[GenerationStore, ServingRepository, FakeEmbedder, V5Fixture],
) -> None:
    store, _, _, fixture = v5_runtime
    with store.pin() as handle:
        assert isinstance(handle.vectors.matrix, np.memmap)
        assert handle.vectors.matrix.nbytes == fixture.vectors.stat().st_size
        assert store.resident_vector_bytes == handle.vectors.norms.nbytes
        assert store.resident_vector_bytes == fixture.vector_count * 4


def test_contract_search_rejects_ambiguous_temporal_scope() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        ContractSearchRequest(
            query="과거 혜택",
            as_of=date(2024, 1, 1),
            include_history=True,
        )


@pytest.mark.asyncio
async def test_v5_dispositions_are_hash_bound_and_exposed_by_legacy_catalog_api(
    tmp_path: Path,
) -> None:
    store = GenerationStore(tmp_path / "state", maximum_vector_bytes=2 * 1024 * 1024)
    fixture, handle = install_v5_fixture(store, include_dispositions=True)
    repository = ServingRepository(
        store,
        FakeEmbedder(np.eye(1, 4096, dtype=np.float32)[0]),
        cursor_secret=b"v5-disposition-test-cursor-secret",
        maximum_candidates=20,
    )

    assert handle.metadata.unsupported_document_count == 1
    assert handle.metadata.ocr_failed_document_count == 1
    unsupported = await repository.get_product("kb", fixture.unsupported_product_code)
    failed = await repository.get_product("kb", fixture.ocr_failed_product_code)
    assert unsupported is not None and unsupported.availability == "unsupported_drm"
    assert failed is not None and failed.availability == "ocr_failed"
    assert failed.document.document_id == fixture.ocr_failed_document_id
    assert await repository.get_document(fixture.ocr_failed_document_id) == failed.document
    assert await repository.get_source_page(fixture.ocr_failed_document_id, 1) is None
    products = await repository.list_products("kb")
    assert {product.availability for product in products} == {
        "available",
        "unsupported_drm",
        "ocr_failed",
    }
    documents = await repository.list_documents()
    assert fixture.ocr_failed_document_id in {document.document_id for document in documents}


def test_v5_promotion_rejects_disposition_payload_tamper(tmp_path: Path) -> None:
    fixture = build_v5_fixture(
        tmp_path / "gen-v5-disposition-tamper",
        generation_id="gen-v5-disposition-tamper",
        include_dispositions=True,
    )
    with sqlite3.connect(fixture.database) as connection:
        connection.execute(
            "UPDATE unsupported_products SET source_payload_json=source_payload_json || ' '"
        )
        connection.commit()

    with pytest.raises(ServingDatabaseV5Error, match="canonically bound"):
        load_generation_handle(
            fixture.database.parent,
            tmp_path / "objects",
            maximum_vector_bytes=2 * 1024 * 1024,
        )


@pytest.mark.parametrize(
    ("tamper", "error"),
    (
        ("schema", "unexpected metadata schema"),
        ("cross_parent", "foreign_key_check|parent contract"),
        ("cross_link", "foreign_key_check|cross-contract"),
        (
            "coverage_non_whitespace_gap",
            "structure node display text is not source-bound|coverage is below 100 percent",
        ),
        ("revision_ledger", "revision coverage ledger is not source-bound"),
    ),
)
def test_v5_promotion_rejects_schema_cross_contract_and_coverage_tamper(
    tmp_path: Path,
    tamper: str,
    error: str,
) -> None:
    fixture = build_v5_fixture(tmp_path / f"gen-v5-{tamper}", generation_id=f"gen-v5-{tamper}")
    with sqlite3.connect(fixture.database) as connection:
        if tamper == "schema":
            connection.execute("ALTER TABLE metadata ADD COLUMN extra TEXT")
        elif tamper == "cross_parent":
            connection.execute(
                """UPDATE structure_nodes
                      SET parent_contract_revision_id=?
                    WHERE node_id=?""",
                (
                    fixture.ambiguous_revision_id,
                    f"{fixture.current_revision_id}-paragraph",
                ),
            )
        elif tamper == "cross_link":
            connection.execute(
                """UPDATE node_links SET to_contract_revision_id=?
                    WHERE from_contract_revision_id=?""",
                (fixture.ambiguous_revision_id, fixture.current_revision_id),
            )
        elif tamper == "coverage_non_whitespace_gap":
            page_text = connection.execute(
                """SELECT text FROM document_pages
                    WHERE contract_revision_id=? AND page=1""",
                (fixture.current_revision_id,),
            ).fetchone()[0]
            row = connection.execute(
                """SELECT source_start,source_end FROM node_spans
                    WHERE node_id=?""",
                (f"{fixture.current_revision_id}-notice-paragraph",),
            ).fetchone()
            shortened_end = int(row[1]) - 2
            shortened = str(page_text)[int(row[0]) : shortened_end]
            connection.execute(
                """UPDATE node_spans SET source_end=?,text_sha256=?
                    WHERE node_id=?""",
                (
                    shortened_end,
                    hashlib.sha256(shortened.encode()).hexdigest(),
                    f"{fixture.current_revision_id}-notice-paragraph",
                ),
            )
        else:
            connection.execute(
                """UPDATE revision_coverage SET source_sha256=?,coverage_sha256=?
                    WHERE contract_revision_id=?""",
                ("0" * 64, "1" * 64, fixture.current_revision_id),
            )
        connection.commit()

    with pytest.raises(ServingDatabaseV5Error, match=error):
        load_generation_handle(
            fixture.database.parent,
            tmp_path / "objects",
            maximum_vector_bytes=2 * 1024 * 1024,
        )


@pytest.mark.parametrize(
    ("tamper", "error"),
    (
        ("missing_policy", "metadata parser_policy_sha256"),
        ("invalid_date", "effective date is invalid"),
        ("supersedes_cycle", "contains a cycle"),
        ("noncanonical_lineage", "lineage identity is not canonically bound"),
        ("profile_identity", "profile ID is not canonical"),
        ("table_metadata", "non-table node contains table metadata"),
        ("view_span", "embedding view source span hash is invalid"),
    ),
)
def test_v5_promotion_rejects_temporal_policy_table_and_view_provenance_tamper(
    tmp_path: Path,
    tamper: str,
    error: str,
) -> None:
    fixture = build_v5_fixture(
        tmp_path / f"gen-v5-semantic-{tamper}",
        generation_id=f"gen-v5-semantic-{tamper}",
    )
    with sqlite3.connect(fixture.database) as connection:
        if tamper == "missing_policy":
            connection.execute("DELETE FROM metadata WHERE key='parser_policy_sha256'")
        elif tamper == "invalid_date":
            connection.execute(
                """UPDATE contract_revisions SET effective_date='2025-02-30'
                     WHERE contract_revision_id=?""",
                (fixture.current_revision_id,),
            )
        elif tamper == "supersedes_cycle":
            connection.execute(
                """UPDATE contract_revisions
                      SET effective_date='2025-01-01',supersedes_revision_id=?
                    WHERE contract_revision_id=?""",
                (fixture.current_revision_id, fixture.old_revision_id),
            )
        elif tamper == "noncanonical_lineage":
            noncanonical = "lineage_" + "f" * 64
            connection.execute(
                "UPDATE product_lineages SET product_lineage_id=? WHERE product_lineage_id=?",
                (noncanonical, fixture.lineage_id),
            )
            connection.execute(
                """UPDATE contract_revisions SET product_lineage_id=?
                     WHERE product_lineage_id=?""",
                (noncanonical, fixture.lineage_id),
            )
        elif tamper == "profile_identity":
            connection.execute("UPDATE embedding_profiles SET maximum_tokens=4096")
        elif tamper == "table_metadata":
            connection.execute(
                "UPDATE structure_nodes SET table_role='BODY' WHERE node_id=?",
                (f"{fixture.current_revision_id}-paragraph",),
            )
        else:
            connection.execute(
                "UPDATE embedding_view_spans SET text_sha256=? WHERE row_index=0",
                ("0" * 64,),
            )
        connection.commit()

    with pytest.raises(ServingDatabaseV5Error, match=error):
        load_generation_handle(
            fixture.database.parent,
            tmp_path / "objects",
            maximum_vector_bytes=2 * 1024 * 1024,
        )


@pytest.mark.asyncio
async def test_exact_search_scores_every_active_view_in_blocks_without_rank_fusion(
    v5_runtime: tuple[GenerationStore, ServingRepository, FakeEmbedder, V5Fixture],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, repository, embedder, fixture = v5_runtime
    monkeypatch.setattr(exact_module, "VECTOR_BLOCK_ROWS", 2)

    result = await repository.search_contracts(ContractSearchRequest(query="공항", limit=10))

    assert [bundle.contract.contract_revision_id for bundle in result.bundles] == [
        fixture.ambiguous_revision_id,
        fixture.current_revision_id,
    ]
    assert result.bundles[0].matches[0].score == pytest.approx(0.9, abs=1e-6)
    assert result.bundles[1].matches[0].score == pytest.approx(0.8, abs=1e-6)
    current_match = result.bundles[1].matches[0]
    assert current_match.node.node_type == "ITEM"
    assert current_match.node.spans == ()
    assert [view.view_type for view in current_match.matched_views] == ["TITLE", "DETAIL"]
    assert [view.score for view in current_match.matched_views] == pytest.approx([0.7, 0.8])
    assert current_match.matched_views[1].display_text == "공항 라운지 혜택\n"
    assert "".join(span.text for span in current_match.matched_views[1].spans) == (
        current_match.matched_views[1].display_text
    )
    assert result.bundles[1].linked_notice_count == 1
    assert result.coverage.expected_active_contracts == 2
    assert result.coverage.scored_contracts == 2
    assert result.coverage.expected_embedding_rows == 4
    assert result.coverage.scored_embedding_rows == 4
    assert result.coverage.document_aggregation_status == "candidate_default"
    assert result.coverage.document_aggregation_policy == "max_child"
    assert result.coverage.sealed_profile_sha256 is None
    assert result.coverage.exact_row_corpus_sha256 is not None
    assert result.coverage.exact_blocks == 2
    assert result.coverage.approximate is False
    assert result.coverage.lexical_influenced_ranking is False
    assert result.coverage.reranker_influenced_ranking is False
    assert result.coverage.catalog_resolution_status == "unresolved"
    assert result.coverage.catalog_candidate_count == 0
    assert embedder.profile_calls == [(4096, "cardrag.qwen3-query.v1", "deepinfra")]
    incomplete = result.coverage.model_dump(mode="python")
    incomplete["scored_embedding_rows"] -= 1
    with pytest.raises(ValueError, match="score every expected contract and row"):
        SearchCoverage.model_validate(incomplete)
    missing_contract = result.coverage.model_dump(mode="python")
    missing_contract["scored_contracts"] -= 1
    with pytest.raises(ValueError, match="score every expected contract and row"):
        SearchCoverage.model_validate(missing_contract)


@pytest.mark.asyncio
async def test_exact_temporal_selection_current_as_of_and_history(
    v5_runtime: tuple[GenerationStore, ServingRepository, FakeEmbedder, V5Fixture],
) -> None:
    _, repository, _, fixture = v5_runtime

    current = await repository.search_contracts(ContractSearchRequest(query="혜택"))
    as_of = await repository.search_contracts(
        ContractSearchRequest(query="혜택", as_of=date(2024, 6, 1))
    )
    history = await repository.search_contracts(
        ContractSearchRequest(query="혜택", include_history=True)
    )

    assert {item.contract.contract_revision_id for item in current.bundles} == {
        fixture.current_revision_id,
        fixture.ambiguous_revision_id,
    }
    assert current.coverage.temporal_scope == "current"
    assert current.coverage.expected_embedding_rows == 4
    assert [item.contract.contract_revision_id for item in as_of.bundles] == [
        fixture.old_revision_id,
        fixture.ambiguous_revision_id,
    ]
    assert as_of.coverage.temporal_scope == "as_of"
    assert as_of.coverage.expected_embedding_rows == 2
    assert [item.contract.contract_revision_id for item in history.bundles] == [
        fixture.old_revision_id,
        fixture.ambiguous_revision_id,
        fixture.current_revision_id,
    ]
    assert history.coverage.temporal_scope == "history"
    assert history.coverage.expected_embedding_rows == 5


@pytest.mark.asyncio
async def test_as_of_equal_latest_revision_dates_fail_before_provider_call(
    tmp_path: Path,
) -> None:
    store = GenerationStore(tmp_path / "state", maximum_vector_bytes=2 * 1024 * 1024)
    fixture = build_v5_fixture(
        store.generations / "gen-v5-as-of-tie",
        generation_id="gen-v5-as-of-tie",
    )
    with sqlite3.connect(fixture.database) as connection:
        connection.execute(
            """UPDATE contract_revisions SET effective_date='2025-01-01'
                 WHERE contract_revision_id=?""",
            (fixture.old_revision_id,),
        )
        connection.commit()
    for digest, body in fixture.pdf_objects:
        destination = cas_path(store.objects, digest)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(body)
    handle = load_generation_handle(
        fixture.database.parent,
        store.objects,
        maximum_vector_bytes=store.maximum_vector_bytes,
        maximum_vector_sidecar_bytes=store.maximum_vector_sidecar_bytes,
        maximum_resident_vector_bytes=store.maximum_resident_vector_bytes,
        expected_generation_id=fixture.generation_id,
        expected_embedding_model="qwen/qwen3-embedding-8b",
        expected_embedding_count=fixture.vector_count,
    )
    store.verify_handle_pdfs(handle)
    store.activate(handle)
    query = np.zeros((4096,), dtype=np.float32)
    query[0] = 1.0
    embedder = FakeEmbedder(query)
    repository = ServingRepository(
        store,
        embedder,
        cursor_secret=b"as-of-tie-test-cursor-secret-value",
        maximum_candidates=20,
    )

    with pytest.raises(ValueError, match="as_of revision selection is ambiguous"):
        await repository.search_contracts(
            ContractSearchRequest(query="혜택", as_of=date(2025, 1, 1))
        )

    assert embedder.calls == []
    assert embedder.profile_calls == []


@pytest.mark.asyncio
async def test_contract_bundle_never_crosses_revision_and_preserves_source_order(
    v5_runtime: tuple[GenerationStore, ServingRepository, FakeEmbedder, V5Fixture],
) -> None:
    _, repository, _, fixture = v5_runtime
    bundle = await repository.get_contract_bundle(fixture.current_revision_id)
    assert bundle is not None
    assert {node.contract_revision_id for node in bundle.nodes} == {fixture.current_revision_id}
    node_ids = {node.node_id for node in bundle.nodes}
    assert all(
        link.from_node_id in node_ids and link.to_node_id in node_ids
        for node in bundle.nodes
        for link in node.links
    )
    source = await repository.get_source_page("doc-alpha-current", 1)
    assert source is not None
    spans = sorted(
        (span for node in bundle.nodes for span in node.spans),
        key=lambda span: (span.page, span.source_start),
    )
    assert "".join(span.text for span in spans) == source.text

    notices = await repository.get_contract_bundle(
        fixture.current_revision_id,
        scope="notices",
    )
    assert notices is not None
    assert {node.major_class for node in notices.nodes} <= {"UNKNOWN", "NOTICE"}
    revisions = await repository.list_product_revisions("kb", fixture.lineage_id)
    assert [item.temporal_status for item in revisions.revisions] == [
        "current",
        "superseded",
    ]


@pytest.mark.asyncio
async def test_contract_bundle_recovers_original_table_headers_cells_and_role(
    tmp_path: Path,
) -> None:
    store = GenerationStore(tmp_path / "state", maximum_vector_bytes=2 * 1024 * 1024)
    fixture = build_v5_fixture(
        store.generations / "gen-v5-table-bundle",
        generation_id="gen-v5-table-bundle",
    )
    table_id = f"{fixture.current_revision_id}-table"
    row_id = f"{fixture.current_revision_id}-paragraph"
    headers = ("혜택", "조건")
    with sqlite3.connect(fixture.database) as connection:
        for node_id, ordinal in (
            (f"{fixture.current_revision_id}-notice-paragraph", 6),
            (f"{fixture.current_revision_id}-notice", 5),
            (row_id, 4),
        ):
            connection.execute(
                "UPDATE structure_nodes SET ordinal=? WHERE node_id=?",
                (ordinal, node_id),
            )
        connection.execute(
            """INSERT INTO structure_nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                table_id,
                fixture.current_revision_id,
                f"{fixture.current_revision_id}-item",
                fixture.current_revision_id,
                "TABLE",
                "BENEFIT",
                None,
                3,
                "",
                json.dumps(headers, ensure_ascii=False, separators=(",", ":")),
                "[]",
                None,
            ),
        )
        connection.execute(
            """UPDATE structure_nodes
                  SET parent_id=?,node_type='TABLE_ROW',table_headers_json=?,
                      table_cells_json=?,table_role='BODY'
                WHERE node_id=?""",
            (
                table_id,
                json.dumps(headers, ensure_ascii=False, separators=(",", ":")),
                json.dumps(
                    ("공항 라운지", "전월 실적 조건"),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                row_id,
            ),
        )
        for key, delta in (
            ("structure_node_count", 1),
            ("structure_node_count.PARAGRAPH", -1),
            ("structure_node_count.TABLE", 1),
            ("structure_node_count.TABLE_ROW", 1),
        ):
            connection.execute(
                "UPDATE metadata SET value=CAST(value AS INTEGER)+? WHERE key=?",
                (delta, key),
            )
        connection.commit()
    handle = load_generation_handle(
        fixture.database.parent,
        store.objects,
        maximum_vector_bytes=store.maximum_vector_bytes,
    )
    store.activate(handle)
    repository = ServingRepository(
        store,
        FakeEmbedder(np.eye(1, 4096, dtype=np.float32)[0]),
        cursor_secret=b"table-bundle-source-contract-secret",
        maximum_candidates=20,
    )

    bundle = await repository.get_contract_bundle(fixture.current_revision_id)

    assert bundle is not None
    by_id = {node.node_id: node for node in bundle.nodes}
    assert by_id[table_id].table_headers == headers
    assert by_id[row_id].table_headers == headers
    assert by_id[row_id].table_cells == ("공항 라운지", "전월 실적 조건")
    assert by_id[row_id].table_role == "BODY"


def test_previous_next_expansion_is_one_hop_not_transitive_contract_closure(
    tmp_path: Path,
) -> None:
    fixture = build_v5_fixture(
        tmp_path / "gen-v5-bounded-links",
        generation_id="gen-v5-bounded-links",
    )
    near = f"{fixture.current_revision_id}-near-item"
    far = f"{fixture.current_revision_id}-far-item"
    paragraph = f"{fixture.current_revision_id}-paragraph"
    parent = f"{fixture.current_revision_id}-benefit"
    with sqlite3.connect(fixture.database) as connection:
        connection.executemany(
            "INSERT INTO structure_nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                (
                    near,
                    fixture.current_revision_id,
                    parent,
                    fixture.current_revision_id,
                    "ITEM",
                    "BENEFIT",
                    None,
                    6,
                    "",
                    "[]",
                    "[]",
                    None,
                ),
                (
                    far,
                    fixture.current_revision_id,
                    parent,
                    fixture.current_revision_id,
                    "ITEM",
                    "BENEFIT",
                    None,
                    7,
                    "",
                    "[]",
                    "[]",
                    None,
                ),
            ),
        )
        connection.executemany(
            "INSERT INTO node_links VALUES(?,?,?,?,?)",
            (
                (
                    paragraph,
                    fixture.current_revision_id,
                    near,
                    fixture.current_revision_id,
                    "NEXT",
                ),
                (
                    near,
                    fixture.current_revision_id,
                    far,
                    fixture.current_revision_id,
                    "NEXT",
                ),
            ),
        )
        connection.execute(
            "UPDATE metadata SET value=CAST(value AS INTEGER)+2 WHERE key='structure_node_count'"
        )
        connection.execute(
            """UPDATE metadata SET value=CAST(value AS INTEGER)+2
                 WHERE key='structure_node_count.ITEM'"""
        )
        connection.execute(
            "UPDATE metadata SET value=CAST(value AS INTEGER)+2 WHERE key='node_link_count'"
        )
        connection.commit()
    handle = load_generation_handle(
        fixture.database.parent,
        tmp_path / "objects",
        maximum_vector_bytes=2 * 1024 * 1024,
    )

    graph, _, linked_notice_count = exact_module.V5ExactRepository._expanded_graph(
        handle,
        fixture.current_revision_id,
        (paragraph,),
        full=False,
        scope="full",
        include_links=True,
    )

    selected = {node.node_id for node in graph}
    assert near in selected
    assert far not in selected
    assert linked_notice_count == 1


@pytest.mark.asyncio
async def test_v5_legacy_evidence_adapter_and_v4_legacy_search_remain_available(
    tmp_path: Path,
    v5_runtime: tuple[GenerationStore, ServingRepository, FakeEmbedder, V5Fixture],
) -> None:
    _, v5_repository, _, _ = v5_runtime
    v5_legacy = await v5_repository.search(SearchRequest(query="공항"))
    assert v5_legacy.retrieval_mode == "exact"
    assert v5_legacy.items
    assert all(item.page_start == 1 for item in v5_legacy.items)

    store = GenerationStore(tmp_path / "v4-state", maximum_vector_bytes=1024 * 1024)
    directory = store.generations / "gen-v4-adapter"
    create_database(
        directory / "index.sqlite3",
        directory.name,
        schema_id="cardrag.serving-db.v4",
    )
    handle = load_generation_handle(
        directory,
        store.objects,
        maximum_vector_bytes=store.maximum_vector_bytes,
    )
    store.activate(handle)
    v4_repository = ServingRepository(
        store,
        FakeEmbedder(unit_vector(0)),
        cursor_secret=b"v4-adapter-cursor-secret-value",
        maximum_candidates=20,
    )
    v4_legacy = await v4_repository.search(SearchRequest(query="airport"))
    assert v4_legacy.retrieval_mode == "hybrid"
    assert v4_legacy.items
    with pytest.raises(RuntimeError, match="does not provide v5 contract search"):
        await v4_repository.search_contracts(ContractSearchRequest(query="airport"))


@pytest.mark.asyncio
async def test_legacy_v5_route_and_exact_search_share_one_pinned_generation(
    tmp_path: Path,
    v5_runtime: tuple[GenerationStore, ServingRepository, FakeEmbedder, V5Fixture],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, repository, _, fixture = v5_runtime
    v4_directory = store.generations / "gen-v4-concurrent"
    create_database(
        v4_directory / "index.sqlite3",
        v4_directory.name,
        schema_id="cardrag.serving-db.v4",
    )
    v4_handle = load_generation_handle(
        v4_directory,
        store.objects,
        maximum_vector_bytes=store.maximum_vector_bytes,
    )
    original = repository.exact.search

    async def activate_between_route_and_query(
        request: ContractSearchRequest,
        *,
        handle: GenerationHandle | None = None,
    ) -> object:
        store.activate(v4_handle)
        return await original(request, handle=handle)

    monkeypatch.setattr(repository.exact, "search", activate_between_route_and_query)

    result = await repository.search(SearchRequest(query="공항"))

    assert result.generation_id == fixture.generation_id
    assert result.items
    assert store.active_generation_id == v4_handle.generation_id


@pytest.mark.asyncio
async def test_get_evidence_route_and_bundle_share_one_pinned_generation(
    v5_runtime: tuple[GenerationStore, ServingRepository, FakeEmbedder, V5Fixture],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, repository, _, fixture = v5_runtime
    v4_directory = store.generations / "gen-v4-evidence-concurrent"
    create_database(
        v4_directory / "index.sqlite3",
        v4_directory.name,
        schema_id="cardrag.serving-db.v4",
    )
    v4_handle = load_generation_handle(
        v4_directory,
        store.objects,
        maximum_vector_bytes=store.maximum_vector_bytes,
    )
    original = repository.exact.get_bundle

    def activate_between_route_and_bundle(
        contract_revision_id: str,
        *,
        scope: str,
        include_links: bool,
        handle: GenerationHandle | None = None,
    ) -> object:
        store.activate(v4_handle)
        return original(
            contract_revision_id,
            scope=scope,  # type: ignore[arg-type]
            include_links=include_links,
            handle=handle,
        )

    monkeypatch.setattr(repository.exact, "get_bundle", activate_between_route_and_bundle)

    page = await repository.get_evidence(f"{fixture.current_revision_id}-item")

    assert page is not None
    assert page.generation_id == fixture.generation_id
    assert page.items
    assert store.active_generation_id == v4_handle.generation_id


@pytest.mark.asyncio
async def test_exact_mode_never_creates_an_exhaustive_audit_job(
    v5_runtime: tuple[GenerationStore, ServingRepository, FakeEmbedder, V5Fixture],
) -> None:
    store, repository, _, _ = v5_runtime

    result = await repository.search_contracts(
        ContractSearchRequest(query="정확 검색은 로컬 상태를 쓰지 않는다")
    )

    assert result.coverage.search_mode == "exact"
    assert not (store.root / "audit-jobs").exists()
    assert result.coverage.model_dump(mode="json") == {
        key: value
        for key, value in result.coverage.model_dump(mode="json").items()
        if not key.startswith("exhaustive_")
    }


@pytest.mark.asyncio
async def test_exhaustive_audit_resumes_contract_checkpoint_and_preserves_dense_ranking(
    v5_runtime: tuple[GenerationStore, ServingRepository, FakeEmbedder, V5Fixture],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, repository, embedder, _ = v5_runtime
    request = ContractSearchRequest(query="중단 재개 비밀 질의", mode="exhaustive")
    original_checkpoint = repository.exact.audit_store.checkpoint

    def checkpoint_then_interrupt(*args: object, **kwargs: object) -> object:
        ledger = original_checkpoint(*args, **kwargs)  # type: ignore[arg-type]
        if len(ledger.completed_contracts) == 1:
            raise RuntimeError("injected exhaustive interruption")
        return ledger

    monkeypatch.setattr(
        repository.exact.audit_store,
        "checkpoint",
        checkpoint_then_interrupt,
    )
    with pytest.raises(RuntimeError, match="injected exhaustive interruption"):
        await repository.search_contracts(request)

    job_directories = list((store.root / "audit-jobs").glob("audit-*"))
    assert len(job_directories) == 1
    progress = job_directories[0] / "progress.json"
    progress_payload = progress.read_bytes()
    assert "중단 재개 비밀 질의".encode() not in progress_payload
    assert not list(job_directories[0].glob(".progress.json.*"))
    query_vector_checkpoint = job_directories[0] / "query-vector.f32"
    assert query_vector_checkpoint.stat().st_size == 4096 * 4
    assert query_vector_checkpoint.stat().st_mode & 0o222 == 0
    assert len(embedder.calls) == 1

    # A live provider can return cosine-equivalent but byte-different floats.
    # Resume must load the sealed LE-f32 query rather than call it again.
    original_vector = embedder.vector.copy()
    jittered = original_vector.copy()
    jittered[1] = np.float32(1e-4)
    jittered /= np.linalg.norm(jittered)
    embedder.vector = jittered

    monkeypatch.setattr(
        repository.exact.audit_store,
        "checkpoint",
        original_checkpoint,
    )
    resumed = await repository.search_contracts(request)
    assert len(embedder.calls) == 1
    embedder.vector = original_vector
    exact = await repository.search_contracts(ContractSearchRequest(query="중단 재개 비밀 질의"))

    coverage = resumed.coverage
    assert coverage.search_mode == "exhaustive"
    assert coverage.exhaustive_resumed is True
    assert coverage.exhaustive_completed_contracts == 2
    assert coverage.exhaustive_total_contracts == 2
    assert coverage.expected_embedding_rows == coverage.scored_embedding_rows == 4
    assert coverage.exhaustive_job_id == job_directories[0].name
    assert coverage.exhaustive_artifact_sha256 is not None
    assert coverage.approximate is False
    assert coverage.lexical_influenced_ranking is False
    assert coverage.reranker_influenced_ranking is False
    assert [item.contract.contract_revision_id for item in resumed.bundles] == [
        item.contract.contract_revision_id for item in exact.bundles
    ]
    assert [item.matches[0].score for item in resumed.bundles] == pytest.approx(
        [item.matches[0].score for item in exact.bundles]
    )

    artifact = job_directories[0] / f"artifact-{coverage.exhaustive_artifact_sha256}.json"
    marker = job_directories[0] / "COMPLETE.json"
    assert artifact.is_file() and artifact.stat().st_mode & 0o222 == 0
    assert marker.is_file() and marker.stat().st_mode & 0o222 == 0
    assert "중단 재개 비밀 질의".encode() not in artifact.read_bytes()

    calls_before_completed_reuse = len(embedder.calls)
    completed_again = await repository.search_contracts(request)
    assert len(embedder.calls) == calls_before_completed_reuse
    assert completed_again.coverage.exhaustive_resumed is True
    assert (
        completed_again.coverage.exhaustive_artifact_sha256 == coverage.exhaustive_artifact_sha256
    )


@pytest.mark.asyncio
async def test_exhaustive_progress_tamper_fails_closed(
    v5_runtime: tuple[GenerationStore, ServingRepository, FakeEmbedder, V5Fixture],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, repository, _, _ = v5_runtime
    request = ContractSearchRequest(query="progress tamper", mode="exhaustive")
    original_checkpoint = repository.exact.audit_store.checkpoint

    def checkpoint_then_interrupt(*args: object, **kwargs: object) -> object:
        original_checkpoint(*args, **kwargs)  # type: ignore[arg-type]
        raise RuntimeError("interrupt after durable checkpoint")

    monkeypatch.setattr(
        repository.exact.audit_store,
        "checkpoint",
        checkpoint_then_interrupt,
    )
    with pytest.raises(RuntimeError, match="durable checkpoint"):
        await repository.search_contracts(request)
    monkeypatch.setattr(
        repository.exact.audit_store,
        "checkpoint",
        original_checkpoint,
    )

    progress = next((store.root / "audit-jobs").glob("audit-*/progress.json"))
    payload = json.loads(progress.read_text(encoding="utf-8"))
    payload["ledger"]["completed_contracts"][0]["nodes"][0]["score"] = -0.125
    progress.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    with pytest.raises(ExhaustiveAuditError):
        await repository.search_contracts(request)
    assert not (progress.parent / "COMPLETE.json").exists()


@pytest.mark.asyncio
async def test_exhaustive_completed_artifact_tamper_fails_closed(
    v5_runtime: tuple[GenerationStore, ServingRepository, FakeEmbedder, V5Fixture],
) -> None:
    store, repository, _, _ = v5_runtime
    request = ContractSearchRequest(query="artifact tamper", mode="exhaustive")
    completed = await repository.search_contracts(request)
    job_id = completed.coverage.exhaustive_job_id
    artifact_sha256 = completed.coverage.exhaustive_artifact_sha256
    assert job_id is not None and artifact_sha256 is not None
    artifact = store.root / "audit-jobs" / job_id / f"artifact-{artifact_sha256}.json"
    artifact.chmod(0o600)
    artifact.write_bytes(artifact.read_bytes() + b" ")

    with pytest.raises(ExhaustiveAuditError, match="hash or size"):
        await repository.search_contracts(request)


@pytest.mark.asyncio
async def test_exhaustive_job_is_generation_bound_and_stale_artifact_is_not_reused(
    v5_runtime: tuple[GenerationStore, ServingRepository, FakeEmbedder, V5Fixture],
) -> None:
    store, repository, embedder, _ = v5_runtime
    request = ContractSearchRequest(query="generation binding", mode="exhaustive")
    first = await repository.search_contracts(request)
    first_job = first.coverage.exhaustive_job_id
    assert first_job is not None

    install_v5_fixture(store, generation_id="gen-v5-next")
    second = await repository.search_contracts(request)
    second_job = second.coverage.exhaustive_job_id

    assert first.generation_id == "gen-v5-exact"
    assert second.generation_id == "gen-v5-next"
    assert second_job is not None and second_job != first_job
    assert second.coverage.exhaustive_artifact_sha256 != (first.coverage.exhaustive_artifact_sha256)
    assert (store.root / "audit-jobs" / first_job).is_dir()
    assert (store.root / "audit-jobs" / second_job).is_dir()
    assert len(embedder.calls) == 2


@pytest.mark.asyncio
async def test_exhaustive_job_directory_cannot_escape_local_state(
    tmp_path: Path,
    v5_runtime: tuple[GenerationStore, ServingRepository, FakeEmbedder, V5Fixture],
) -> None:
    store, repository, _, fixture = v5_runtime
    query = "traversal safe audit"
    query_sha256 = hashlib.sha256(query.encode()).hexdigest()
    identity = repository.exact.audit_store.identity(fixture.generation_id, query_sha256)
    audit_root = store.root / "audit-jobs"
    audit_root.mkdir()
    outside = tmp_path / "outside-audit"
    outside.mkdir()
    (audit_root / identity.job_id).symlink_to(outside, target_is_directory=True)

    with pytest.raises(ExhaustiveAuditError, match="must not be a symlink"):
        await repository.search_contracts(ContractSearchRequest(query=query, mode="exhaustive"))
    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "search_request",
    (
        ContractSearchRequest(query="all", mode="exhaustive", issuer="kb"),
        ContractSearchRequest(
            query="all",
            mode="exhaustive",
            product_lineage_id="lineage-alpha",
        ),
        ContractSearchRequest(query="all", mode="exhaustive", as_of=date(2025, 1, 1)),
        ContractSearchRequest(query="all", mode="exhaustive", include_history=True),
    ),
)
async def test_exhaustive_audit_rejects_scopes_not_bound_by_its_job_identity(
    v5_runtime: tuple[GenerationStore, ServingRepository, FakeEmbedder, V5Fixture],
    search_request: ContractSearchRequest,
) -> None:
    store, repository, _, _ = v5_runtime

    with pytest.raises(ValueError, match="unscoped current corpus"):
        await repository.search_contracts(search_request)
    assert not (store.root / "audit-jobs").exists()


def test_contract_search_limit_allows_recall_at_100_but_rejects_larger_payloads() -> None:
    assert ContractSearchRequest(query="recall", limit=100).limit == 100
    with pytest.raises(ValueError, match="less than or equal to 100"):
        ContractSearchRequest(query="recall", limit=101)


@pytest.mark.asyncio
async def test_catalog_resolver_uses_nfkc_casefold_whitespace_and_longest_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = GenerationStore(tmp_path / "state", maximum_vector_bytes=2 * 1024 * 1024)
    fixture = build_v5_fixture(
        store.generations / "gen-v5-catalog",
        generation_id="gen-v5-catalog",
    )
    with sqlite3.connect(fixture.database) as connection:
        connection.execute(
            "UPDATE product_lineages SET name='ＭＹ　ＣＡＲＤ' WHERE product_code='ALPHA'"
        )
        connection.execute(
            "UPDATE product_lineages SET name='MY CARD PREMIUM' WHERE product_code='BETA'"
        )
        connection.commit()
    store.activate(
        load_generation_handle(
            fixture.database.parent,
            store.objects,
            maximum_vector_bytes=store.maximum_vector_bytes,
        )
    )
    repository = ServingRepository(
        store,
        FakeEmbedder(np.eye(1, 4096, dtype=np.float32)[0]),
        cursor_secret=b"catalog-resolution-test-secret-value",
        maximum_candidates=20,
    )

    longest = await repository.search_contracts(
        ContractSearchRequest(query="  my   card   premium 혜택  ")
    )
    assert longest.coverage.catalog_resolution_status == "resolved"
    assert longest.coverage.catalog_candidate_count == 1
    assert longest.coverage.catalog_resolved_product_name == "MY CARD PREMIUM"
    assert [bundle.contract.contract_revision_id for bundle in longest.bundles] == [
        fixture.ambiguous_revision_id
    ]

    full_contract = await repository.search_contracts(
        ContractSearchRequest(query="my card 혜택", issuer="kb")
    )
    assert full_contract.coverage.full_contract_fallback_count == 0
    assert [bundle.contract.contract_revision_id for bundle in full_contract.bundles] == [
        fixture.current_revision_id
    ]
    assert len(full_contract.bundles[0].nodes) == 6

    expanded_graph = exact_module.V5ExactRepository._expanded_graph
    full_flags: list[bool] = []

    def capture_expansion(*args: object, **kwargs: object) -> object:
        full_flags.append(bool(kwargs["full"]))
        return expanded_graph(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        exact_module.V5ExactRepository,
        "_expanded_graph",
        staticmethod(capture_expansion),
    )
    monkeypatch.setattr(exact_module, "FULL_CONTRACT_CONTEXT_NODE_LIMIT", 1)
    normalized = await repository.search_contracts(
        ContractSearchRequest(query="my card 혜택", issuer="kb")
    )
    assert normalized.coverage.catalog_resolution_status == "resolved"
    assert normalized.coverage.catalog_resolved_product_lineage_id == fixture.lineage_id
    assert normalized.coverage.full_contract_fallback_count == 1
    assert [bundle.contract.contract_revision_id for bundle in normalized.bundles] == [
        fixture.current_revision_id
    ]
    assert full_flags == [False]

    explicit = await repository.search_contracts(
        ContractSearchRequest(
            query="my card premium 혜택",
            product_lineage_id=fixture.lineage_id,
        )
    )
    assert explicit.coverage.catalog_resolution_status == "explicit"
    assert explicit.coverage.catalog_resolved_product_lineage_id == fixture.lineage_id
    assert [bundle.contract.contract_revision_id for bundle in explicit.bundles] == [
        fixture.current_revision_id
    ]


@pytest.mark.asyncio
async def test_catalog_resolver_reports_ambiguity_without_arbitrary_lineage_selection(
    tmp_path: Path,
) -> None:
    store = GenerationStore(tmp_path / "state", maximum_vector_bytes=2 * 1024 * 1024)
    fixture = build_v5_fixture(
        store.generations / "gen-v5-catalog-ambiguous",
        generation_id="gen-v5-catalog-ambiguous",
    )
    with sqlite3.connect(fixture.database) as connection:
        connection.execute("UPDATE product_lineages SET name='공통 카드'")
        connection.commit()
    store.activate(
        load_generation_handle(
            fixture.database.parent,
            store.objects,
            maximum_vector_bytes=store.maximum_vector_bytes,
        )
    )
    repository = ServingRepository(
        store,
        FakeEmbedder(np.eye(1, 4096, dtype=np.float32)[0]),
        cursor_secret=b"catalog-ambiguity-test-secret-value",
        maximum_candidates=20,
    )

    ambiguous = await repository.search_contracts(ContractSearchRequest(query="공통 카드 혜택"))
    assert ambiguous.coverage.catalog_resolution_status == "ambiguous"
    assert ambiguous.coverage.catalog_candidate_count == 2
    assert ambiguous.coverage.catalog_resolved_product_lineage_id is None
    assert {bundle.contract.contract_revision_id for bundle in ambiguous.bundles} == {
        fixture.current_revision_id,
        fixture.ambiguous_revision_id,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lineage_id", "issuer", "error"),
    (
        ("lineage-does-not-exist", None, "does not exist"),
        (None, "not-kb", "does not belong to issuer"),
    ),
)
async def test_explicit_catalog_lineage_fails_before_embedding_when_invalid(
    v5_runtime: tuple[GenerationStore, ServingRepository, FakeEmbedder, V5Fixture],
    lineage_id: str | None,
    issuer: str | None,
    error: str,
) -> None:
    _, repository, embedder, fixture = v5_runtime
    explicit_lineage = fixture.lineage_id if lineage_id is None else lineage_id

    with pytest.raises(ValueError, match=error):
        await repository.search_contracts(
            ContractSearchRequest(
                query="invalid explicit lineage",
                issuer=issuer,
                product_lineage_id=explicit_lineage,
            )
        )

    assert embedder.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("budget_name", "budget"),
    (
        ("MAX_SEARCH_RESPONSE_NODES", 4),
        ("MAX_SEARCH_RESPONSE_CHARACTERS", 1),
    ),
)
async def test_contract_search_seals_node_and_character_response_budgets(
    v5_runtime: tuple[GenerationStore, ServingRepository, FakeEmbedder, V5Fixture],
    monkeypatch: pytest.MonkeyPatch,
    budget_name: str,
    budget: int,
) -> None:
    _, repository, _, _ = v5_runtime
    monkeypatch.setattr(exact_module, budget_name, budget)

    result = await repository.search_contracts(
        ContractSearchRequest(query="payload budget", limit=100)
    )

    assert result.coverage.response_truncated is True
    if budget_name == "MAX_SEARCH_RESPONSE_NODES":
        assert result.coverage.response_node_count <= budget
    else:
        assert result.coverage.response_character_count <= budget
    assert result.coverage.response_node_count == sum(
        len(bundle.nodes) for bundle in result.bundles
    )
    assert result.coverage.response_character_count == sum(
        bundle.context_character_count for bundle in result.bundles
    )


@pytest.mark.asyncio
async def test_lexical_audit_scans_global_active_fts_and_distinguishes_failures(
    v5_runtime: tuple[GenerationStore, ServingRepository, FakeEmbedder, V5Fixture],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, repository, embedder, fixture = v5_runtime
    request = ContractSearchRequest(query="전월", limit=1)

    healthy = await repository.search_contracts(request)
    assert [bundle.contract.contract_revision_id for bundle in healthy.bundles] == [
        fixture.ambiguous_revision_id
    ]
    assert healthy.coverage.lexical_enabled is True
    assert healthy.coverage.lexical_status == "succeeded"
    assert healthy.coverage.lexical_error is None
    assert healthy.coverage.lexical_global_matched_evidence_count == 1

    def unavailable_fts(*_args: object, **_kwargs: object) -> object:
        raise sqlite3.OperationalError("no such table: embedding_views_fts")

    monkeypatch.setattr(repository.exact, "_lexical_additions", unavailable_fts)
    degraded = await repository.search_contracts(request)
    assert [bundle.contract.contract_revision_id for bundle in degraded.bundles] == [
        fixture.ambiguous_revision_id
    ]
    assert degraded.coverage.lexical_enabled is False
    assert degraded.coverage.lexical_status == "failed"
    assert degraded.coverage.lexical_error == "fts_unavailable"

    embedder.fail = True
    with pytest.raises(EmbeddingUnavailable, match="injected failure"):
        await repository.search_contracts(request)


@pytest.mark.asyncio
async def test_exhaustive_mode_polls_bounded_contract_batches_until_complete(
    v5_runtime: tuple[GenerationStore, ServingRepository, FakeEmbedder, V5Fixture],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, repository, embedder, _ = v5_runtime
    monkeypatch.setattr(exact_module, "EXHAUSTIVE_CONTRACTS_PER_CALL", 1)
    request = ContractSearchRequest(query="bounded exhaustive polling", mode="exhaustive")

    running = await repository.search_contracts(request)
    assert running.bundles == ()
    assert running.coverage.exhaustive_status == "running"
    assert running.coverage.exhaustive_completed_contracts == 1
    assert running.coverage.exhaustive_total_contracts == 2
    assert running.coverage.exhaustive_resumed is False
    assert running.coverage.exhaustive_artifact_sha256 is None
    assert running.coverage.lexical_status == "deferred"
    assert running.coverage.response_node_count == 0
    assert len(embedder.calls) == 1
    wrong_contract_prefix = running.coverage.model_dump(mode="python")
    wrong_contract_prefix["scored_contracts"] = 0
    with pytest.raises(ValueError, match="progress counters differ"):
        SearchCoverage.model_validate(wrong_contract_prefix)
    wrong_row_prefix = running.coverage.model_dump(mode="python")
    wrong_row_prefix["scored_embedding_rows"] = running.coverage.expected_embedding_rows
    with pytest.raises(ValueError, match="strict scored prefix"):
        SearchCoverage.model_validate(wrong_row_prefix)

    complete = await repository.search_contracts(request)
    assert complete.bundles
    assert complete.coverage.exhaustive_status == "complete"
    assert complete.coverage.exhaustive_completed_contracts == 2
    assert complete.coverage.exhaustive_total_contracts == 2
    assert complete.coverage.exhaustive_resumed is True
    assert complete.coverage.exhaustive_artifact_sha256 is not None
    assert complete.coverage.exhaustive_job_id == running.coverage.exhaustive_job_id
    assert len(embedder.calls) == 1
    incomplete_completion = complete.coverage.model_dump(mode="python")
    incomplete_completion["scored_embedding_rows"] -= 1
    with pytest.raises(ValueError, match="score every expected contract and row"):
        SearchCoverage.model_validate(incomplete_completion)
    stale_progress_counters = complete.coverage.model_dump(mode="python")
    stale_progress_counters["exhaustive_completed_contracts"] -= 1
    stale_progress_counters["exhaustive_total_contracts"] -= 1
    with pytest.raises(ValueError, match="progress counters differ"):
        SearchCoverage.model_validate(stale_progress_counters)

    polled = await repository.search_contracts(request)
    assert polled.coverage.exhaustive_status == "complete"
    assert (
        polled.coverage.exhaustive_artifact_sha256 == complete.coverage.exhaustive_artifact_sha256
    )
    assert len(embedder.calls) == 1
