from __future__ import annotations

import hashlib
import struct
from pathlib import Path

import pytest
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

from cardrag_worker.capacity_v5 import (
    DATABASE_EXPORT_PEAK_MULTIPLIER,
    build_v5_database_ledger,
    predict_serving_database_bytes,
)
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
    ViewSourceSpanInput,
)
from cardrag_worker.structure import DerivedView, NodeSpan


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _bounded_fixture(
    *,
    count: int,
    text_characters: int,
    index_key_characters: int,
) -> dict[str, object]:
    profile_id = qwen3_embedding_profile_id(provider_id="deepinfra", maximum_tokens=32768)
    profile = EmbeddingProfileInput(
        profile_id=profile_id,
        provider=QWEN3_EMBEDDING_PROVIDER,
        model=QWEN3_EMBEDDING_MODEL,
        provider_id="deepinfra",
        dimension=QWEN3_EMBEDDING_DIMENSION,
        dtype=QWEN3_EMBEDDING_DTYPE,
        normalization=QWEN3_EMBEDDING_NORMALIZATION,
        document_policy=QWEN3_DOCUMENT_POLICY,
        query_policy=QWEN3_QUERY_POLICY,
        maximum_tokens=32768,
    )
    vector = struct.pack("<4096f", 1.0, *([0.0] * 4095))
    lineages: list[ProductLineageInput] = []
    revisions: list[ContractRevisionInput] = []
    pages: list[DocumentPageInput] = []
    nodes: list[StructureNodeInput] = []
    node_spans: list[NodeSpanInput] = []
    derived_views: list[DerivedView] = []
    embedding_views: list[EmbeddingViewInput] = []

    for index in range(count):
        identity = _sha256(f"row-{index}")
        lineage_id = "lineage_" + _sha256(f"lineage-{index}")
        revision_id = "revision_" + _sha256(f"revision-{index}")
        root_id = "node_" + _sha256(f"root-{index}")
        paragraph_id = "node_" + _sha256(f"paragraph-{index}")
        if index_key_characters:
            product_code = identity + "p" * (index_key_characters - len(identity))
            document_type = "d" * index_key_characters
            name = "n" * index_key_characters
            text = f"benefit-{identity}"
        else:
            product_code = f"card-{index}"
            document_type = "product_description"
            tokens: list[str] = []
            token_index = 0
            while len(" ".join(tokens)) < text_characters:
                tokens.append(_sha256(f"token-{index}-{token_index}")[:10])
                token_index += 1
            text = " ".join(tokens)[:text_characters].rstrip()
            name = f"Card {index}"
        text_sha256 = _sha256(text)
        lineages.append(
            ProductLineageInput(
                product_lineage_id=lineage_id,
                issuer="test",
                product_code=product_code,
                document_type=document_type,
                name=name,
            )
        )
        revisions.append(
            ContractRevisionInput(
                contract_revision_id=revision_id,
                product_lineage_id=lineage_id,
                document_id="doc_" + _sha256(f"document-{index}"),
                source_id="source_" + _sha256(f"source-{index}"),
                source_version="1",
                source_url=f"https://example.test/{index}.pdf",
                effective_date="2026-08-01",
                pdf_sha256=_sha256(f"pdf-{index}"),
                pdf_size_bytes=1,
                page_count=1,
                temporal_status="current",
            )
        )
        pages.append(
            DocumentPageInput(
                contract_revision_id=revision_id,
                page=1,
                text=text,
                text_sha256=text_sha256,
            )
        )
        nodes.append(
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
                table_headers=(),
                table_cells=(),
                table_role=None,
            )
        )
        nodes.append(
            StructureNodeInput(
                node_id=paragraph_id,
                contract_revision_id=revision_id,
                parent_id=root_id,
                parent_contract_revision_id=revision_id,
                node_type="PARAGRAPH",
                major_class="BENEFIT",
                raw_heading=None,
                ordinal=1,
                display_text=text,
                table_headers=(),
                table_cells=(),
                table_role=None,
            )
        )
        node_spans.append(
            NodeSpanInput(
                node_id=paragraph_id,
                contract_revision_id=revision_id,
                page=1,
                source_start=0,
                source_end=len(text),
                text_sha256=text_sha256,
                span_ordinal=0,
                is_canonical=True,
            )
        )
        span = NodeSpan(
            page=1,
            source_start=0,
            source_end=len(text),
            text_sha256=text_sha256,
            span_ordinal=0,
            is_canonical=True,
        )
        derived_views.append(
            DerivedView(
                view_id="view_" + _sha256(f"view-{index}"),
                contract_revision_id=revision_id,
                node_id=paragraph_id,
                parent_item_id=None,
                view_type="TITLE",
                ordinal=0,
                display_text=text,
                embedding_input=text,
                spans=(span,),
                context=(),
                input_sha256=text_sha256,
            )
        )
        embedding_views.append(
            EmbeddingViewInput(
                row_index=index,
                node_id=paragraph_id,
                contract_revision_id=revision_id,
                view_type="TITLE",
                embedding_input=text,
                input_sha256=text_sha256,
                profile_id=profile_id,
                display_text=text,
                source_spans=(
                    ViewSourceSpanInput(
                        page=1,
                        source_start=0,
                        source_end=len(text),
                        text_sha256=text_sha256,
                    ),
                ),
                vector=vector,
            )
        )

    return {
        "issuers": (IssuerInput(code="test", display_name="Test", sort_order=1),),
        "product_lineages": tuple(lineages),
        "contract_revisions": tuple(revisions),
        "document_pages": tuple(pages),
        "structure_nodes": tuple(nodes),
        "node_spans": tuple(node_spans),
        "node_links": (),
        "embedding_profiles": (profile,),
        "derived_views": tuple(derived_views),
        "embedding_views": tuple(embedding_views),
        "primary_embedding_profile_id": profile_id,
    }


@pytest.mark.parametrize(
    ("count", "text_characters", "index_key_characters"),
    ((1000, 2000, 0), (1000, 80, 475), (500, 80, 4096)),
    ids=("fts-page-slack", "overflow-key-crossover", "long-secondary-index"),
)
def test_database_prediction_bounds_exporter_sealed_and_build_peak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    count: int,
    text_characters: int,
    index_key_characters: int,
) -> None:
    rows = _bounded_fixture(
        count=count,
        text_characters=text_characters,
        index_key_characters=index_key_characters,
    )
    ledger = build_v5_database_ledger(
        issuers=rows["issuers"],
        product_lineages=rows["product_lineages"],
        unsupported_products=(),
        ocr_failed_products=(),
        contract_revisions=rows["contract_revisions"],
        document_pages=rows["document_pages"],
        structure_nodes=rows["structure_nodes"],
        node_spans=rows["node_spans"],
        node_links=(),
        embedding_profiles=rows["embedding_profiles"],
        derived_views=rows["derived_views"],
        primary_embedding_profile_id=rows["primary_embedding_profile_id"],
        extra_metadata={},
        sealed_profile=False,
    )
    prediction = predict_serving_database_bytes(
        payload_bytes=ledger.payload_bytes,
        row_count=ledger.row_count,
        fts_indexed_text_bytes=ledger.fts_indexed_text_bytes,
        secondary_index_text_bytes=ledger.secondary_index_text_bytes,
    )
    original_unlink = Path.unlink

    def preserve_database_build(path: Path, *args: object, **kwargs: object) -> None:
        if (
            path.parent == tmp_path
            and path.name.startswith(".index.sqlite3.")
            and path.name.endswith(".build")
        ):
            return
        original_unlink(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "unlink", preserve_database_build)
    result = ServingDatabaseExporterV5().export(
        tmp_path / "index.sqlite3",
        tmp_path / "vectors.f32",
        generation_id="generation-capacity-regression",
        corpus_sha256="a" * 64,
        contract_sha256="b" * 64,
        primary_embedding_profile_id=rows["primary_embedding_profile_id"],
        issuers=rows["issuers"],
        product_lineages=rows["product_lineages"],
        contract_revisions=rows["contract_revisions"],
        document_pages=rows["document_pages"],
        structure_nodes=rows["structure_nodes"],
        node_spans=rows["node_spans"],
        node_links=(),
        embedding_profiles=rows["embedding_profiles"],
        embedding_views=rows["embedding_views"],
        predicted_serving_database_bytes=prediction,
        reserved_free_space_bytes=0,
    )
    working = tuple(tmp_path.glob(".index.sqlite3.*.build"))

    assert len(working) == 1
    assert result.database_size_bytes <= prediction
    assert working[0].stat().st_size + result.database_size_bytes <= (
        DATABASE_EXPORT_PEAK_MULTIPLIER * prediction
    )
    original_unlink(working[0])
