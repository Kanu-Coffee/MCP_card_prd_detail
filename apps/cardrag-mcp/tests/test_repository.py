from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pytest
from conftest import FakeEmbedder, create_database, unit_vector

from cardrag_mcp.embeddings import EmbeddingUnavailable
from cardrag_mcp.models import Product, SearchFilters, SearchRequest, UnsupportedProduct
from cardrag_mcp.repository import ServingRepository
from cardrag_mcp.schema import ServingDatabaseError
from cardrag_mcp.store import GenerationStore, load_generation_handle


def test_vectors_are_sorted_and_promotion_limit_is_checked_before_load(tmp_path: Path) -> None:
    store = GenerationStore(tmp_path / "state", maximum_vector_bytes=1024 * 1024)
    directory = store.generations / "gen-ordered"
    create_database(directory / "index.sqlite3", "gen-ordered")

    handle = load_generation_handle(
        directory,
        store.objects,
        maximum_vector_bytes=1024 * 1024,
    )
    assert handle.vectors.evidence_ids == ("ev-a", "ev-b", "ev-c")
    assert handle.vectors.matrix.dtype == np.float32
    assert handle.vectors.matrix.shape == (3, 1536)

    with pytest.raises(ServingDatabaseError, match="promotion limit"):
        load_generation_handle(
            directory,
            store.objects,
            maximum_vector_bytes=3 * 1536 * 4 - 1,
        )


@pytest.mark.parametrize("tamper", ["page_hash", "source_span"])
def test_promotion_rejects_tampered_page_hash_or_exact_evidence_span(
    tmp_path: Path,
    tamper: str,
) -> None:
    directory = tmp_path / f"gen-{tamper}"
    database = directory / "index.sqlite3"
    create_database(database, directory.name)
    with sqlite3.connect(database) as connection:
        if tamper == "page_hash":
            connection.execute(
                "UPDATE pages SET text_sha256=? WHERE document_id='doc-a' AND page=1",
                ("0" * 64,),
            )
        else:
            connection.execute("UPDATE evidence SET source_start=1 WHERE evidence_id='ev-a'")
        connection.commit()

    with pytest.raises(ServingDatabaseError, match="page text hash|exact page span"):
        load_generation_handle(
            directory,
            tmp_path / "objects",
            maximum_vector_bytes=1024 * 1024,
        )


def test_promotion_rejects_incompatible_query_embedding_policy(tmp_path: Path) -> None:
    directory = tmp_path / "gen-policy"
    database = directory / "index.sqlite3"
    create_database(database, directory.name)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE metadata SET value='wrong-prefix' WHERE key='embedding_query_prefix'"
        )
        connection.commit()

    with pytest.raises(ServingDatabaseError, match="input policy"):
        load_generation_handle(
            directory,
            tmp_path / "objects",
            maximum_vector_bytes=1024 * 1024,
        )


@pytest.mark.parametrize(
    ("schema_id", "protected_magic"),
    (
        ("cardrag.serving-db.v2", "SCDSA004"),
        ("cardrag.serving-db.v3", "FASOO_DRMONE"),
    ),
)
def test_loader_accepts_last_good_v2_and_current_v3(
    tmp_path: Path,
    schema_id: str,
    protected_magic: str,
) -> None:
    directory = tmp_path / f"gen-{schema_id.rsplit('.', 1)[-1]}"
    fixture = create_database(
        directory / "index.sqlite3",
        directory.name,
        schema_id=schema_id,
        protected_magic=protected_magic,
    )

    handle = load_generation_handle(
        directory,
        tmp_path / "objects",
        maximum_vector_bytes=1024 * 1024,
    )

    assert handle.metadata.schema_id == schema_id
    assert handle.metadata.contract_sha256 == fixture.contract_sha256


@pytest.mark.parametrize("schema_id", ("cardrag.serving-db.v2", "cardrag.serving-db.v3"))
def test_loader_rejects_missing_contract_hash(tmp_path: Path, schema_id: str) -> None:
    directory = tmp_path / f"gen-missing-contract-{schema_id.rsplit('.', 1)[-1]}"
    database = directory / "index.sqlite3"
    create_database(database, directory.name, schema_id=schema_id)
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM metadata WHERE key='contract_sha256'")

    with pytest.raises(ServingDatabaseError, match="required serving metadata is missing"):
        load_generation_handle(
            directory,
            tmp_path / "objects",
            maximum_vector_bytes=1024 * 1024,
        )


@pytest.mark.parametrize(
    ("schema_id", "protected_magic", "expected_error"),
    (
        ("cardrag.serving-db.v2", "FASOO_DRMONE", "invalid bounded value"),
        ("cardrag.serving-db.v4", "SCDSA002", "schema version is incompatible"),
    ),
)
def test_loader_rejects_cross_version_magic_and_unknown_schema(
    tmp_path: Path,
    schema_id: str,
    protected_magic: str,
    expected_error: str,
) -> None:
    directory = tmp_path / "gen-incompatible"
    create_database(
        directory / "index.sqlite3",
        directory.name,
        schema_id=schema_id,
        protected_magic=protected_magic,
    )

    with pytest.raises(ServingDatabaseError, match=expected_error):
        load_generation_handle(
            directory,
            tmp_path / "objects",
            maximum_vector_bytes=1024 * 1024,
        )


@pytest.mark.asyncio
async def test_v3_fasoo_product_remains_explicitly_visible(tmp_path: Path) -> None:
    store = GenerationStore(tmp_path / "state", maximum_vector_bytes=1024 * 1024)
    directory = store.generations / "gen-fasoo"
    create_database(
        directory / "index.sqlite3",
        directory.name,
        schema_id="cardrag.serving-db.v3",
        protected_magic="FASOO_DRMONE",
    )
    handle = load_generation_handle(
        directory,
        store.objects,
        maximum_vector_bytes=store.maximum_vector_bytes,
    )
    store.activate(handle)
    repository = ServingRepository(
        store,
        FakeEmbedder(unit_vector(0)),
        cursor_secret=b"test-cursor-secret-value",
        maximum_candidates=20,
    )

    product = await repository.get_product("woori", "P-DRM")

    assert isinstance(product, UnsupportedProduct)
    assert product.availability == "unsupported_drm"
    assert product.protected_magic == "FASOO_DRMONE"


@pytest.mark.parametrize(
    ("tamper", "expected_error"),
    (
        ("count", "count differs"),
        ("hash", "hash differs"),
        ("noncanonical_source", "not canonical JSON"),
        ("source_id", "source_id does not bind"),
        ("source_version", "columns do not bind"),
        ("overlap", "both available and unsupported"),
        ("promotion_limit", "promotion limit"),
    ),
)
def test_promotion_rejects_tampered_unsupported_product_contract(
    tmp_path: Path,
    tamper: str,
    expected_error: str,
) -> None:
    directory = tmp_path / f"gen-unsupported-{tamper}"
    database = directory / "index.sqlite3"
    create_database(database, directory.name)
    with sqlite3.connect(database) as connection:
        if tamper == "count":
            connection.execute(
                "UPDATE metadata SET value='0' WHERE key='unsupported_document_count'"
            )
        elif tamper == "hash":
            connection.execute(
                "UPDATE metadata SET value=? WHERE key='unsupported_documents_sha256'",
                ("0" * 64,),
            )
        elif tamper == "noncanonical_source":
            connection.execute(
                "UPDATE unsupported_products SET source_payload_json=source_payload_json || ' '"
            )
        elif tamper == "source_id":
            connection.execute(
                "UPDATE unsupported_products SET source_id=?",
                ("source_" + "0" * 64,),
            )
        elif tamper == "source_version":
            connection.execute("UPDATE unsupported_products SET source_version='changed'")
        elif tamper == "overlap":
            connection.execute("UPDATE unsupported_products SET product_code='P1'")
        else:
            connection.execute(
                "UPDATE metadata SET value='101' WHERE key='unsupported_document_count'"
            )
        connection.commit()

    with pytest.raises(ServingDatabaseError, match=expected_error):
        load_generation_handle(
            directory,
            tmp_path / "objects",
            maximum_vector_bytes=1024 * 1024,
        )


@pytest.mark.asyncio
async def test_hybrid_filter_rrf_and_bound_cursor(active_runtime) -> None:
    _, repository, embedder, _ = active_runtime
    first = await repository.search(
        SearchRequest(
            query="airport lounge",
            filters=SearchFilters(issuer="woori", product_code="P1"),
            limit=1,
        )
    )
    assert first.retrieval_mode == "hybrid"
    assert first.degraded is False
    assert [item.evidence_id for item in first.items] == ["ev-a"]
    assert first.items[0].lexical_rank == 1
    assert first.items[0].vector_rank == 1
    assert first.next_cursor is not None
    assert embedder.calls == [("airport lounge", "openrouter", "openai/text-embedding-3-small")]

    second = await repository.search(
        SearchRequest(
            query="airport lounge",
            filters=SearchFilters(issuer="woori", product_code="P1"),
            limit=1,
            cursor=first.next_cursor,
        )
    )
    assert [item.evidence_id for item in second.items] == ["ev-b"]
    assert second.next_cursor is None

    with pytest.raises(ValueError, match="cursor"):
        await repository.search(SearchRequest(query="different", limit=1, cursor=first.next_cursor))


@pytest.mark.asyncio
async def test_embedding_failure_requires_explicit_degraded_mode(active_runtime) -> None:
    _, repository, _, _ = active_runtime
    repository.embedder = FakeEmbedder(unit_vector(0), fail=True)
    request = SearchRequest(query="airport", limit=10)
    with pytest.raises(EmbeddingUnavailable):
        await repository.search(request)

    degraded = await repository.search(request.model_copy(update={"allow_degraded": True}))
    assert degraded.retrieval_mode == "lexical_only"
    assert degraded.degraded is True
    assert {item.evidence_id for item in degraded.items} == {"ev-a", "ev-b"}


@pytest.mark.asyncio
async def test_evidence_adjacency_product_and_page_queries(active_runtime) -> None:
    _, repository, _, _ = active_runtime
    assert await repository.get_evidence("missing") is None

    first = await repository.get_evidence("ev-a", limit=1)
    assert first is not None
    assert first.document_id == "doc-a"
    assert [item.evidence_id for item in first.items] == ["ev-a"]
    assert first.next_cursor is not None
    second = await repository.get_evidence("ev-a", cursor=first.next_cursor, limit=1)
    assert second is not None
    assert [item.evidence_id for item in second.items] == ["ev-b"]
    assert second.next_cursor is None

    product = await repository.get_product("woori", "P1")
    assert isinstance(product, Product)
    assert product.availability == "available"
    assert product.document.document_id == "doc-a"
    unsupported = await repository.get_product("woori", "P-DRM")
    assert isinstance(unsupported, UnsupportedProduct)
    assert unsupported.model_dump(mode="json") == {
        "issuer": "woori",
        "product_code": "P-DRM",
        "name": "Protected Card",
        "availability": "unsupported_drm",
        "source_id": unsupported.source_id,
        "source_version": "20260826",
        "source_url": "https://example.com/protected.pdf",
        "protected_magic": "SCDSA002",
        "protected_source_sha256": unsupported.protected_source_sha256,
        "protected_source_size_bytes": len(b"SCDSA002fixture-protected-source"),
    }
    page = await repository.get_source_page("doc-a", 1)
    assert page is not None
    assert page.page_count == 1
    assert [item.page for item in await repository.list_pages("doc-a")] == [1]
    assert await repository.list_pages("missing") == ()
    assert len(await repository.list_issuers()) == 2
    products = await repository.list_products()
    assert len(products) == 3
    assert {item.availability for item in products} == {"available", "unsupported_drm"}
    assert [item.product_code for item in await repository.list_products("woori")] == [
        "P-DRM",
        "P1",
    ]
    assert len(await repository.list_documents()) == 2
