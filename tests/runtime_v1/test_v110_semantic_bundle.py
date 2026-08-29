from __future__ import annotations

import struct
from pathlib import Path

from cardrag_core import sha256_bytes
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
from cardrag_mcp.exact import V5ExactRepository
from cardrag_mcp.store import load_generation_handle
from cardrag_worker.contracts import PageRecord
from cardrag_worker.exporter_v5 import (
    ContractRevisionInput,
    DocumentPageInput,
    EmbeddingProfileInput,
    EmbeddingViewInput,
    IssuerInput,
    NodeLinkInput,
    NodeSpanInput,
    ProductLineageInput,
    ServingDatabaseExporterV5,
    StructureNodeInput,
    ViewSourceSpanInput,
)
from cardrag_worker.structure import build_derived_views, parse_structure_artifact


def test_parser_export_exact_preserves_common_notice_descendants_without_graph_closure(
    tmp_path: Path,
) -> None:
    pages = (
        PageRecord(
            document_id="doc-semantic-bundle",
            page=1,
            text=(
                "## 주요 혜택\n"
                "### 교통 할인\n"
                "버스 이용 시 10%를 할인합니다.\n"
                "※ 월 할인 한도는 1만원입니다.\n"
                "## 추가 혜택\n"
                "### 카페 할인\n"
                "커피 이용 시 5%를 할인합니다.\n"
            ),
        ),
        PageRecord(
            document_id="doc-semantic-bundle",
            page=2,
            text=(
                "## 공통 유의사항\n"
                "### 제외 대상\n"
                "상품권 구매는 전월 실적에서 제외됩니다.\n"
                "## 기타\n"
                "이 문장은 검색 문맥과 무관합니다.\n"
            ),
        ),
    )
    artifact = parse_structure_artifact(
        pages,
        issuer="kb",
        product_code="SEMANTIC-001",
        product_name="의미 연결 테스트 카드",
        source_version="2026-08-30",
        effective_date="2026-08-30",
        document_type="product_description",
        source_id="source_" + sha256_bytes(b"semantic-bundle-source"),
        pdf_sha256=sha256_bytes(b"semantic-bundle-pdf"),
    )
    views = build_derived_views(artifact, maximum_chars=131_072)
    profile_id = qwen3_embedding_profile_id("deepinfra", maximum_tokens=8192)
    unit_vector = struct.pack("<f", 1.0) + bytes((QWEN3_EMBEDDING_DIMENSION - 1) * 4)
    generation = tmp_path / "gen-v5-semantic-bundle"
    ServingDatabaseExporterV5().export(
        generation / "index.sqlite3",
        generation / "vectors.f32",
        generation_id=generation.name,
        corpus_sha256=sha256_bytes(b"semantic-bundle-corpus"),
        contract_sha256=sha256_bytes(b"semantic-bundle-contract"),
        primary_embedding_profile_id=profile_id,
        issuers=(IssuerInput(code="kb", display_name="KB국민카드", sort_order=1),),
        product_lineages=(
            ProductLineageInput(
                product_lineage_id=artifact.product_lineage_id,
                issuer=artifact.issuer,
                product_code=artifact.product_code,
                document_type=artifact.document_type,
                name=artifact.product_name,
            ),
        ),
        contract_revisions=(
            ContractRevisionInput(
                contract_revision_id=artifact.contract_revision_id,
                product_lineage_id=artifact.product_lineage_id,
                document_id=artifact.document_id,
                source_id=artifact.source_id,
                source_version=artifact.source_version,
                source_url="https://public.example/semantic-bundle.pdf",
                effective_date="2026-08-30",
                pdf_sha256=artifact.pdf_sha256,
                pdf_size_bytes=1024,
                page_count=len(artifact.pages),
                temporal_status="current",
            ),
        ),
        document_pages=tuple(
            DocumentPageInput(
                contract_revision_id=artifact.contract_revision_id,
                page=page.page,
                text=page.text,
                text_sha256=page.text_sha256,
            )
            for page in artifact.pages
        ),
        structure_nodes=tuple(
            StructureNodeInput(
                node_id=node.node_id,
                contract_revision_id=node.contract_revision_id,
                parent_id=node.parent_id,
                parent_contract_revision_id=(None if node.parent_id is None else node.contract_revision_id),
                node_type=node.node_type,
                major_class=node.major_class,
                raw_heading=node.raw_heading,
                ordinal=node.ordinal,
                display_text=node.display_text,
                table_headers=node.table_headers,
                table_cells=node.table_cells,
                table_role=node.table_role,
            )
            for node in artifact.nodes
        ),
        node_spans=tuple(
            NodeSpanInput(
                node_id=node.node_id,
                contract_revision_id=node.contract_revision_id,
                page=span.page,
                source_start=span.source_start,
                source_end=span.source_end,
                text_sha256=span.text_sha256,
                span_ordinal=span.span_ordinal,
                is_canonical=span.is_canonical,
            )
            for node in artifact.nodes
            for span in node.spans
        ),
        node_links=tuple(
            NodeLinkInput(
                from_node_id=link.from_node_id,
                from_contract_revision_id=artifact.contract_revision_id,
                to_node_id=link.to_node_id,
                to_contract_revision_id=artifact.contract_revision_id,
                link_type=link.link_type,
                ordinal=link.ordinal,
            )
            for link in artifact.links
        ),
        embedding_profiles=(
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
        embedding_views=tuple(
            EmbeddingViewInput(
                row_index=row_index,
                node_id=view.node_id,
                contract_revision_id=view.contract_revision_id,
                view_type=view.view_type,
                embedding_input=view.embedding_input,
                input_sha256=view.input_sha256,
                profile_id=profile_id,
                display_text=view.display_text,
                source_spans=tuple(
                    ViewSourceSpanInput(
                        page=span.page,
                        source_start=span.source_start,
                        source_end=span.source_end,
                        text_sha256=span.text_sha256,
                    )
                    for span in view.spans
                ),
                vector=unit_vector,
            )
            for row_index, view in enumerate(views)
        ),
        extra_metadata={
            "embedding_policy_sha256": sha256_bytes(b"semantic-bundle-embedding-policy"),
            "parser_policy_sha256": sha256_bytes(b"semantic-bundle-parser-policy"),
            "parser_profile_id.kb": artifact.issuer_profile_id,
            "parser_profile_sha256.kb": artifact.issuer_profile_sha256,
            "retrieval_policy_sha256": sha256_bytes(b"semantic-bundle-retrieval-policy"),
        },
    )
    handle = load_generation_handle(
        generation,
        tmp_path / "objects",
        maximum_vector_bytes=32 * 1024 * 1024,
    )

    benefit_items = sorted(
        (node for node in artifact.nodes if node.node_type == "ITEM" and node.major_class == "BENEFIT"),
        key=lambda node: node.ordinal,
    )
    notice_major = next(
        node
        for node in artifact.nodes
        if node.node_type == "MAJOR_SECTION" and node.raw_heading == "## 공통 유의사항"
    )
    artifact_by_id = {node.node_id: node for node in artifact.nodes}
    notice_descendants = {notice_major.node_id}
    for node in artifact.nodes:
        parent_id = node.parent_id
        while parent_id is not None:
            if parent_id == notice_major.node_id:
                notice_descendants.add(node.node_id)
                break
            parent_id = artifact_by_id[parent_id].parent_id
    exclusion = next(
        node for node in artifact.nodes if node.display_text == "상품권 구매는 전월 실적에서 제외됩니다.\n"
    )
    unrelated = next(
        node for node in artifact.nodes if node.display_text == "이 문장은 검색 문맥과 무관합니다.\n"
    )
    footnote = next(node for node in artifact.nodes if node.node_type == "FOOTNOTE")

    assert len(benefit_items) == 2
    assert {
        link.to_node_id
        for link in artifact.links
        if link.link_type == "APPLIES_TO" and link.from_node_id == notice_major.node_id
    } == {item.node_id for item in benefit_items}
    for index, benefit_item in enumerate(benefit_items):
        graph, _, linked_notice_count = V5ExactRepository._expanded_graph(
            handle,
            artifact.contract_revision_id,
            (benefit_item.node_id,),
            full=False,
            scope="full",
            include_links=True,
        )
        selected = {node.node_id for node in graph}

        assert notice_descendants <= selected
        assert exclusion.node_id in selected
        assert unrelated.node_id not in selected
        assert benefit_items[1 - index].node_id not in selected
        assert {node.contract_revision_id for node in graph} == {artifact.contract_revision_id}
        assert [node.ordinal for node in graph] == sorted(node.ordinal for node in graph)
        assert linked_notice_count == (2 if index == 0 else 1)
        if index == 0:
            assert footnote.node_id in selected
            exported_footnote = next(node for node in graph if node.node_id == footnote.node_id)
            assert {link.link_type for link in exported_footnote.links} >= {
                "APPLIES_TO",
                "FOOTNOTE_OF",
            }
        else:
            assert footnote.node_id not in selected
