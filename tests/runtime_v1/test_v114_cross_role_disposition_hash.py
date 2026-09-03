from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from cardrag_core import canonical_json_bytes, canonical_sha256
from cardrag_core.embedding import (
    QWEN3_DOCUMENT_POLICY,
    QWEN3_EMBEDDING_DIMENSION,
    QWEN3_EMBEDDING_DTYPE,
    QWEN3_EMBEDDING_MODEL,
    QWEN3_EMBEDDING_NORMALIZATION,
    QWEN3_EMBEDDING_PROVIDER,
    QWEN3_QUERY_POLICY,
    qwen3_embedding_profile_id,
)
from cardrag_mcp.schema_v5 import validate_schema_v5
from cardrag_worker.exporter_v5 import (
    ContractRevisionInput,
    DocumentPageInput,
    EmbeddingProfileInput,
    EmbeddingViewInput,
    IssuerInput,
    NodeSpanInput,
    ProductLineageInput,
    ServingDatabaseExporterV5,
    StructureNodeInput,
    UnsupportedProductInput,
    ViewSourceSpanInput,
)


def _unsupported_product(
    product_code: str,
    name: str,
    *,
    protected_sha256: str,
) -> UnsupportedProductInput:
    source = {
        "category": "credit",
        "document_type": "product_description",
        "effective_date": "2026-08-30",
        "file_name": f"{product_code}.pdf",
        "issuer": "kb",
        "metadata": {"fixture": "v1.0.14-cross-role"},
        "product_code": product_code,
        "product_name": name,
        "source_post_id": f"post-{product_code}",
        "source_url": f"https://public.example/{product_code}.pdf",
        "source_version": "2026-08-30",
    }
    return UnsupportedProductInput(
        issuer="kb",
        product_code=product_code,
        name=name,
        disposition="unsupported_drm",
        source_id="source_" + canonical_sha256(source),
        source_version="2026-08-30",
        source_url=f"https://public.example/{product_code}.pdf",
        protected_magic="SCDSA002",
        protected_sha256=protected_sha256,
        protected_size_bytes=2048,
        source_payload_json=canonical_json_bytes(source).decode("utf-8"),
    )


def _unsupported_payload(row: UnsupportedProductInput) -> dict[str, object]:
    return {
        "disposition": row.disposition,
        "protected_magic": row.protected_magic,
        "protected_sha256": row.protected_sha256,
        "protected_size_bytes": row.protected_size_bytes,
        "source": json.loads(row.source_payload_json),
        "source_id": row.source_id,
    }


def _export_inputs(unsupported_products: tuple[UnsupportedProductInput, ...]) -> dict[str, Any]:
    lineage_id = "lineage_" + canonical_sha256(
        {
            "document_type": "product_description",
            "issuer": "kb",
            "product_code": "CARD-001",
        }
    )
    active_source_id = "source_" + "2" * 64
    active_pdf_sha256 = "a" * 64
    revision_id = "revision_" + canonical_sha256(
        {
            "pdf_sha256": active_pdf_sha256,
            "product_lineage_id": lineage_id,
            "source_id": active_source_id,
        }
    )
    root_id = "node_root"
    paragraph_id = "node_paragraph"
    page_text = "혜택 안내\n전월 실적 제외\n"
    page_sha256 = hashlib.sha256(page_text.encode("utf-8")).hexdigest()
    profile_id = qwen3_embedding_profile_id("deepinfra", maximum_tokens=8192)
    return {
        "generation_id": "generation-v114-cross-role",
        "corpus_sha256": "b" * 64,
        "contract_sha256": "c" * 64,
        "primary_embedding_profile_id": profile_id,
        "issuers": (IssuerInput(code="kb", display_name="KB국민카드", sort_order=1),),
        "product_lineages": (
            ProductLineageInput(
                product_lineage_id=lineage_id,
                issuer="kb",
                product_code="CARD-001",
                document_type="product_description",
                name="테스트 카드",
            ),
        ),
        "unsupported_products": unsupported_products,
        "contract_revisions": (
            ContractRevisionInput(
                contract_revision_id=revision_id,
                product_lineage_id=lineage_id,
                document_id="doc_current",
                source_id=active_source_id,
                source_version="2026-08-30",
                source_url="https://public.example/card.pdf",
                effective_date="2026-08-30",
                pdf_sha256=active_pdf_sha256,
                pdf_size_bytes=1024,
                page_count=1,
                temporal_status="current",
            ),
        ),
        "document_pages": (
            DocumentPageInput(
                contract_revision_id=revision_id,
                page=1,
                text=page_text,
                text_sha256=page_sha256,
            ),
        ),
        "structure_nodes": (
            StructureNodeInput(
                node_id=root_id,
                contract_revision_id=revision_id,
                parent_id=None,
                parent_contract_revision_id=None,
                node_type="ROOT",
                major_class="UNKNOWN",
                raw_heading=None,
                ordinal=0,
                display_text="",
            ),
            StructureNodeInput(
                node_id=paragraph_id,
                contract_revision_id=revision_id,
                parent_id=root_id,
                parent_contract_revision_id=revision_id,
                node_type="PARAGRAPH",
                major_class="BENEFIT",
                raw_heading=None,
                ordinal=1,
                display_text=page_text,
            ),
        ),
        "node_spans": (
            NodeSpanInput(
                node_id=paragraph_id,
                contract_revision_id=revision_id,
                page=1,
                source_start=0,
                source_end=len(page_text),
                text_sha256=page_sha256,
                span_ordinal=0,
                is_canonical=True,
            ),
        ),
        "node_links": (),
        "embedding_profiles": (
            EmbeddingProfileInput(
                profile_id=profile_id,
                provider=QWEN3_EMBEDDING_PROVIDER,
                model=QWEN3_EMBEDDING_MODEL,
                provider_id="deepinfra",
                dimension=QWEN3_EMBEDDING_DIMENSION,
                dtype=QWEN3_EMBEDDING_DTYPE,
                normalization=QWEN3_EMBEDDING_NORMALIZATION,
                document_policy=QWEN3_DOCUMENT_POLICY,
                query_policy=QWEN3_QUERY_POLICY,
                maximum_tokens=8192,
            ),
        ),
        "embedding_views": (
            EmbeddingViewInput(
                row_index=0,
                node_id=paragraph_id,
                contract_revision_id=revision_id,
                view_type="DETAIL",
                embedding_input=page_text,
                input_sha256=page_sha256,
                profile_id=profile_id,
                display_text=page_text,
                source_spans=(
                    ViewSourceSpanInput(
                        page=1,
                        source_start=0,
                        source_end=len(page_text),
                        text_sha256=page_sha256,
                    ),
                ),
                vector=[1.0] + [0.0] * (QWEN3_EMBEDDING_DIMENSION - 1),
            ),
        ),
        "extra_metadata": {
            "parser_policy_sha256": "d" * 64,
            "embedding_policy_sha256": "e" * 64,
            "retrieval_policy_sha256": "f" * 64,
            "parser_profile_id.kb": "cardrag.issuer-profile.kb.v1",
            "parser_profile_sha256.kb": "1" * 64,
        },
    }


def test_worker_v5_exported_disposition_hash_is_accepted_by_mcp_canonical_validator(
    tmp_path: Path,
) -> None:
    # Relational producer order is CARD-A then CARD-Z.  protected_sha256 is the
    # first differing canonical payload field, deliberately reversing that
    # order so this fixture cannot pass accidentally with a single document.
    unsupported_products = (
        _unsupported_product("CARD-Z", "Zulu Card", protected_sha256="0" * 64),
        _unsupported_product("CARD-A", "Alpha Card", protected_sha256="f" * 64),
    )
    producer_documents = [
        _unsupported_payload(row)
        for row in sorted(unsupported_products, key=lambda row: (row.issuer, row.product_code))
    ]
    canonical_documents = sorted(producer_documents, key=canonical_json_bytes)
    assert [row["source"]["product_code"] for row in producer_documents] == ["CARD-A", "CARD-Z"]
    assert [row["source"]["product_code"] for row in canonical_documents] == ["CARD-Z", "CARD-A"]

    schema = "cardrag.unsupported-documents.v1"
    producer_order_sha256 = canonical_sha256({"documents": producer_documents, "schema_version": schema})
    canonical_order_sha256 = canonical_sha256({"documents": canonical_documents, "schema_version": schema})
    assert producer_order_sha256 != canonical_order_sha256

    database = tmp_path / "index.sqlite3"
    vectors = tmp_path / "vectors.f32"
    ServingDatabaseExporterV5().export(
        database,
        vectors,
        **_export_inputs(unsupported_products),
    )

    connection = sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True)
    try:
        relational_order = [
            str(row[0])
            for row in connection.execute(
                "SELECT product_code FROM unsupported_products ORDER BY issuer,product_code"
            )
        ]
        metadata = dict(connection.execute("SELECT key,value FROM metadata"))
        assert relational_order == ["CARD-A", "CARD-Z"]

        # This is the actual cross-role contract: MCP must accept the exact DB
        # emitted by Worker even though its relational and hash payload orders differ.
        loaded = validate_schema_v5(connection, maximum_sidecar_bytes=vectors.stat().st_size)
    finally:
        connection.close()

    assert metadata["unsupported_document_count"] == "2"
    assert metadata["unsupported_documents_sha256"] == canonical_order_sha256
    assert loaded.unsupported_document_count == 2
    assert loaded.unsupported_documents_sha256 == canonical_order_sha256
