from __future__ import annotations

from dataclasses import replace

import pytest
from cardrag_core import canonical_sha256, sha256_bytes

from cardrag_worker.contracts import PageRecord
from cardrag_worker.structure import (
    CANONICAL_LEAF_TYPES,
    DerivedView,
    StructureArtifact,
    StructureValidationError,
    build_derived_views,
    build_unclassified_fallback_artifact,
    contextual_item_policy_payload,
    make_contract_revision_id,
    make_product_lineage_id,
    parse_structure_artifact,
    unclassified_fallback_policy_payload,
    validate_derived_views,
    validate_structure_artifact,
)

ISSUER_PAGES = {
    "kb": (
        """KB국민카드 상품설명서
판독 불확실 원문 ???
## 주요 혜택
### 대중교통 할인
- 전월 실적 충족 시 제공
| 구분 | 혜택 |
| --- | --- |
| 버스 | 10% |
- 1 -
""",
        """KB국민카드 상품설명서
| 지하철 | 10% |
| 택시 | 5% |
※ 할인 한도는 월 1만원입니다.
## 이용 전 확인사항
### 제외 대상
상품권 구매는 전월 실적에서 제외됩니다.
- 2 -
""",
    ),
    "samsung": (
        """삼성카드 상품설명서
## 주요 혜택
혜택 1. 생활 할인
- 커피전문점 10% 할인
혜택 2. 포인트 적립
- 국내 가맹점 1% 적립
1 / 2
""",
        """삼성카드 상품설명서
### 혜택 2 상세
전월 실적 조건을 충족한 경우에만 포인트가 적립됩니다.
## 공통 확인사항
서비스 제외 대상은 상품권 구매입니다.
2 / 2
""",
    ),
    "shinhan": (
        """신한카드 상품설명서
## 할인 서비스
### 온라인 쇼핑
| 대상 | 할인율 |
| --- | --- |
| 쇼핑몰 | 5% |
※ 월 할인 한도는 5천원입니다.
- 1 -
""",
        """신한카드 상품설명서
## 공통 유의사항
- 해외 이용 시 환율과 수수료를 확인하십시오.
- 전월 실적 제외 대상은 세금입니다.
- 2 -
""",
    ),
    "woori": (
        """우리카드 상품설명서
## 장문 혜택 안내
### 마일리지 적립
국내 가맹점 이용금액에 대해 마일리지를 적립합니다.
전월 실적을 충족한 회원에게 서비스를 제공합니다.
1 / 2
""",
        """우리카드 상품설명서
다음 페이지에서도 동일한 마일리지 서비스 설명이 계속됩니다.
## 이용 전 확인사항
### 전월 실적 제외
세금과 상품권 구매금액은 전월 실적에서 제외됩니다.
2 / 2
""",
    ),
}

ISSUER_SOURCE_METADATA = {
    "kb": {
        "product_name": "KB국민 테스트 카드",
        "source_version": "20260829",
        "effective_date": "2026-08-29",
    },
    "samsung": {
        "product_name": "삼성 테스트 카드",
        "source_version": "2026.08.29.1",
        "effective_date": "2026-08-29",
    },
    "shinhan": {
        "product_name": "신한 테스트 카드",
        "source_version": "202608290001",
        "effective_date": "2026-08-29",
    },
    "woori": {
        "product_name": "우리 테스트 카드",
        "source_version": "11",
        "effective_date": "2026-08-29",
    },
}


def _artifact(
    issuer: str,
    texts: tuple[str, ...] | None = None,
    *,
    product_code: str | None = None,
    source_version: str | None = None,
    source_seed: str = "source",
    pdf_seed: str = "pdf",
) -> StructureArtifact:
    selected = texts or ISSUER_PAGES[issuer]
    source_metadata = ISSUER_SOURCE_METADATA[issuer]
    resolved_source_version = source_version or source_metadata["source_version"]
    document_id = f"doc_{issuer}_{product_code or 'card'}"
    pages = tuple(
        PageRecord(document_id=document_id, page=page, text=text) for page, text in enumerate(selected, 1)
    )
    return parse_structure_artifact(
        pages,
        issuer=issuer,
        product_code=product_code or f"{issuer}-card-001",
        product_name=source_metadata["product_name"],
        source_version=resolved_source_version,
        effective_date=source_metadata["effective_date"],
        document_type="product_description",
        source_id="source_"
        + canonical_sha256(
            {
                "effective_date": source_metadata["effective_date"],
                "issuer": issuer,
                "product_name": source_metadata["product_name"],
                "seed": source_seed,
                "source_version": resolved_source_version,
            }
        ),
        pdf_sha256=sha256_bytes(f"{issuer}:{pdf_seed}".encode()),
    )


def _leaf_reconstruction(artifact: StructureArtifact, page: int) -> str:
    source = next(source for source in artifact.pages if source.page == page)
    spans = sorted(
        (
            span
            for node in artifact.nodes
            if node.node_type in CANONICAL_LEAF_TYPES
            for span in node.spans
            if span.page == page
        ),
        key=lambda span: span.source_start,
    )
    return "".join(source.text[span.source_start : span.source_end] for span in spans)


def test_unclassified_fallback_is_lossless_neutral_and_view_partitionable() -> None:
    texts = (
        "원문 첫 줄\n원문 둘째 줄\n",
        "표처럼 보이는 | 원문 | 그대로\n마지막 줄\n",
    )
    pages = tuple(
        PageRecord(document_id="doc_fallback", page=index, text=text)
        for index, text in enumerate(texts, start=1)
    )
    artifact = build_unclassified_fallback_artifact(
        pages,
        issuer="kb",
        product_code="fallback-001",
        product_name="메타데이터 전용 카드명",
        source_version="metadata-version-only",
        effective_date=None,
        document_type="product_description",
        source_id="source_" + "a" * 64,
        pdf_sha256="b" * 64,
    )
    coverage = validate_structure_artifact(artifact)

    assert coverage.coverage_percent == 100.0
    assert coverage.covered_characters == sum(map(len, texts))
    assert {_node.node_type for _node in artifact.nodes} == {"ROOT", "ITEM", "UNCLASSIFIED"}
    assert all(node.major_class == "UNKNOWN" and node.raw_heading is None for node in artifact.nodes)
    assert all(_leaf_reconstruction(artifact, page.page) == page.text for page in artifact.pages)
    assert all(
        node.display_text
        == "".join(
            next(page.text for page in artifact.pages if page.page == span.page)[
                span.source_start : span.source_end
            ]
            for span in node.spans
        )
        for node in artifact.nodes
    )
    assert all("메타데이터 전용 카드명" not in node.display_text for node in artifact.nodes)
    assert len(artifact.links) == 2 * (len(texts[0].splitlines()) + len(texts[1].splitlines()) - 1)

    views = build_derived_views(artifact, maximum_chars=512)
    validate_derived_views(artifact, views, maximum_chars=512)
    assert views
    assert all(view.contract_revision_id == artifact.contract_revision_id for view in views)
    assert all(
        view.display_text
        == "".join(
            next(page.text for page in artifact.pages if page.page == span.page)[
                span.source_start : span.source_end
            ]
            for span in view.spans
        )
        for view in views
    )
    contextual = [view for view in views if view.view_type in {"CONTEXTUAL_ITEM", "DETAIL"}]
    assert contextual
    assert all("product_name: 메타데이터 전용 카드명" in view.embedding_input for view in contextual)
    assert all("메타데이터 전용 카드명" not in view.display_text for view in views)

    assert unclassified_fallback_policy_payload() == {
        "canonical_leaf_type": "UNCLASSIFIED",
        "container_type": "ITEM",
        "contract_scope": "one-contract-revision",
        "major_class": "UNKNOWN",
        "partition_boundary": "complete-source-line",
        "schema_version": "cardrag.structure-unclassified-fallback.v1",
    }


def test_unclassified_fallback_fails_closed_when_one_source_line_exceeds_view_limit() -> None:
    pages = (PageRecord(document_id="doc_fallback", page=1, text="가" * 256),)
    artifact = build_unclassified_fallback_artifact(
        pages,
        issuer="kb",
        product_code="fallback-001",
        product_name="fallback card",
        source_version="1",
        effective_date="2026-08-29",
        document_type="product_description",
        source_id="source_" + "a" * 64,
        pdf_sha256="b" * 64,
    )

    with pytest.raises(StructureValidationError, match="one structural leaf exceeds"):
        build_derived_views(artifact, maximum_chars=128)


@pytest.mark.parametrize("issuer", ["kb", "samsung", "shinhan", "woori"])
def test_issuer_profiles_are_deterministic_and_lossless(issuer: str) -> None:
    artifact = _artifact(issuer)
    repeated = _artifact(issuer)
    coverage = validate_structure_artifact(artifact)

    assert artifact.schema_version == "cardrag.structure.v2"
    assert artifact.issuer_profile_id == f"cardrag.issuer-profile.{issuer}.v1"
    assert len(artifact.issuer_profile_sha256) == 64
    assert artifact.canonical_bytes == repeated.canonical_bytes
    assert artifact.artifact_sha256 == repeated.artifact_sha256
    assert coverage.coverage_percent == 100.0
    assert coverage.covered_non_whitespace_characters == sum(
        not character.isspace() for text in ISSUER_PAGES[issuer] for character in text
    )
    assert all(_leaf_reconstruction(artifact, page.page) == page.text for page in artifact.pages)
    assert any(node.node_type == "ROOT" for node in artifact.nodes)
    assert any(node.node_type == "MAJOR_SECTION" for node in artifact.nodes)
    assert any(node.node_type == "ITEM" for node in artifact.nodes)
    assert any(node.node_type == "BOILERPLATE" for node in artifact.nodes)
    assert all(
        span.is_canonical
        for node in artifact.nodes
        if node.node_type in CANONICAL_LEAF_TYPES
        for span in node.spans
    )
    assert all(
        not span.is_canonical
        for node in artifact.nodes
        if node.node_type in {"ROOT", "MAJOR_SECTION", "ITEM", "TABLE"}
        for span in node.spans
    )


def test_kb_multpage_table_footnote_continuation_and_header_relationships() -> None:
    artifact = _artifact("kb")
    tables = [node for node in artifact.nodes if node.node_type == "TABLE"]
    rows = [node for node in artifact.nodes if node.node_type == "TABLE_ROW"]
    link_types = {link.link_type for link in artifact.links}

    assert len(tables) == 2
    assert tables[0].table_headers == ("구분", "혜택")
    assert tables[1].table_headers == tables[0].table_headers
    assert rows
    assert all(row.table_headers == ("구분", "혜택") for row in rows)
    assert {row.table_role for row in rows} >= {"HEADER", "SEPARATOR", "BODY"}
    assert link_types == {
        "CONTINUATION_OF",
        "FOOTNOTE_OF",
        "APPLIES_TO",
        "PREVIOUS",
        "NEXT",
    }
    assert any(
        link.link_type == "CONTINUATION_OF"
        and link.from_node_id == tables[1].node_id
        and link.to_node_id == tables[0].node_id
        for link in artifact.links
    )
    footnote = next(node for node in artifact.nodes if node.node_type == "FOOTNOTE")
    assert any(
        link.from_node_id == footnote.node_id and link.link_type == "FOOTNOTE_OF" for link in artifact.links
    )


def test_common_notice_container_applies_to_every_prior_benefit_item() -> None:
    artifact = _artifact(
        "kb",
        (
            """## 주요 혜택
### 교통 할인
버스 이용 시 10%를 할인합니다.
※ 월 할인 한도는 1만원입니다.
### 카페 할인
커피 이용 시 5%를 할인합니다.
""",
            """## 공통 유의사항
### 제외 대상
상품권 구매는 전월 실적에서 제외됩니다.
""",
        ),
    )
    by_id = {node.node_id: node for node in artifact.nodes}
    benefit_items = {
        node.node_id for node in artifact.nodes if node.node_type == "ITEM" and node.major_class == "BENEFIT"
    }
    notice_major = next(
        node
        for node in artifact.nodes
        if node.node_type == "MAJOR_SECTION" and node.raw_heading == "## 공통 유의사항"
    )
    notice_heading_leaf = next(
        node
        for node in artifact.nodes
        if node.parent_id == notice_major.node_id and node.display_text == "## 공통 유의사항\n"
    )
    common_notice_links = {
        link.to_node_id
        for link in artifact.links
        if link.link_type == "APPLIES_TO" and link.from_node_id == notice_major.node_id
    }

    assert len(benefit_items) == 2
    assert common_notice_links == benefit_items
    assert all(by_id[target].node_type == "ITEM" for target in common_notice_links)
    assert not any(
        link.link_type == "APPLIES_TO" and link.from_node_id == notice_heading_leaf.node_id
        for link in artifact.links
    )
    footnote = next(node for node in artifact.nodes if node.node_type == "FOOTNOTE")
    footnote_links = {link.link_type for link in artifact.links if link.from_node_id == footnote.node_id}
    assert {"FOOTNOTE_OF", "APPLIES_TO"} <= footnote_links


def test_page_leading_table_with_own_header_is_not_false_continuation() -> None:
    artifact = _artifact(
        "kb",
        (
            """## 주요 혜택
| 기존 | 표 |
| --- | --- |
| A | B |
""",
            """| 신규 | 표 |
| --- | --- |
| C | D |
""",
        ),
    )
    tables = [node for node in artifact.nodes if node.node_type == "TABLE"]
    assert [table.table_headers for table in tables] == [("기존", "표"), ("신규", "표")]

    second_table = tables[1]
    second_rows = [node for node in artifact.nodes if node.parent_id == second_table.node_id]
    assert [row.table_role for row in second_rows] == ["HEADER", "SEPARATOR", "BODY"]
    second_node_ids = {second_table.node_id, *(row.node_id for row in second_rows)}
    assert not any(
        link.link_type == "CONTINUATION_OF" and link.from_node_id in second_node_ids
        for link in artifact.links
    )


def test_markdown_bullets_are_not_promoted_to_headings_and_strong_footnotes_survive() -> None:
    artifact = _artifact(
        "kb",
        (
            """## 기타
- 전월 실적 제외 항목
+ 할인 대상 가맹점
* 일반 목록 항목
※ 전월 실적 산정 유의사항
[1] 할인 제외 조건
주) 적립 조건
*주1 별표 각주
""",
        ),
    )
    typed_text = {node.display_text: node.node_type for node in artifact.nodes if node.display_text.strip()}
    assert typed_text["- 전월 실적 제외 항목\n"] == "LIST_ITEM"
    assert typed_text["+ 할인 대상 가맹점\n"] == "LIST_ITEM"
    assert typed_text["* 일반 목록 항목\n"] == "LIST_ITEM"
    assert typed_text["※ 전월 실적 산정 유의사항\n"] == "FOOTNOTE"
    assert typed_text["[1] 할인 제외 조건\n"] == "FOOTNOTE"
    assert typed_text["주) 적립 조건\n"] == "FOOTNOTE"
    assert typed_text["*주1 별표 각주\n"] == "FOOTNOTE"


def test_major_class_preserves_benefit_notice_mixed_and_unknown() -> None:
    artifact = _artifact(
        "kb",
        (
            """## 할인 혜택
### 기본 서비스
혜택 원문입니다.
## 이용 전 확인사항
제외 대상 원문입니다.
## 할인 한도 조건
혜택과 제한이 함께 적힌 원문입니다.
## 기타
판독 불확실 ???
""",
        ),
    )
    classes = {
        node.major_class
        for node in artifact.nodes
        if node.node_type in {"MAJOR_SECTION", "ITEM", "PARAGRAPH", "UNCLASSIFIED"}
    }
    assert classes == {"BENEFIT", "NOTICE", "MIXED", "UNKNOWN"}
    unknown_text = [
        node.display_text
        for node in artifact.nodes
        if node.node_type == "UNCLASSIFIED" and node.display_text.strip()
    ]
    assert "판독 불확실 ???\n" in unknown_text


def test_lineage_and_revision_ids_bind_the_exact_canonical_identity() -> None:
    lineage = make_product_lineage_id(
        issuer="kb", product_code="stable-001", document_type="product_description"
    )
    assert lineage == "lineage_" + canonical_sha256(
        {
            "document_type": "product_description",
            "issuer": "kb",
            "product_code": "stable-001",
        }
    )
    revision = make_contract_revision_id(
        product_lineage_id=lineage,
        source_id="source_" + "a" * 64,
        pdf_sha256="b" * 64,
    )
    assert revision == "revision_" + canonical_sha256(
        {
            "pdf_sha256": "b" * 64,
            "product_lineage_id": lineage,
            "source_id": "source_" + "a" * 64,
        }
    )

    first = _artifact("kb", product_code="stable-001")
    new_source = _artifact("kb", product_code="stable-001", source_seed="new")
    new_pdf = _artifact("kb", product_code="stable-001", pdf_seed="new")
    sibling = _artifact("kb", product_code="stable-002")
    assert first.product_lineage_id == new_source.product_lineage_id == new_pdf.product_lineage_id
    assert (
        len({first.contract_revision_id, new_source.contract_revision_id, new_pdf.contract_revision_id}) == 3
    )
    assert sibling.product_lineage_id != first.product_lineage_id


def test_validator_rejects_span_hash_page_overlap_coverage_and_midline_split() -> None:
    artifact = _artifact("shinhan")
    leaf_index = next(
        index
        for index, node in enumerate(artifact.nodes)
        if node.node_type in CANONICAL_LEAF_TYPES and node.display_text.strip()
    )
    leaf = artifact.nodes[leaf_index]
    span = leaf.spans[0]

    bad_hash_span = replace(span, text_sha256="0" * 64)
    bad_hash_node = replace(leaf, spans=(bad_hash_span,))
    with pytest.raises(StructureValidationError):
        validate_structure_artifact(
            replace(
                artifact,
                nodes=artifact.nodes[:leaf_index] + (bad_hash_node,) + artifact.nodes[leaf_index + 1 :],
            )
        )

    missing_page_span = replace(span, page=999)
    missing_page_node = replace(leaf, spans=(missing_page_span,))
    with pytest.raises(StructureValidationError):
        validate_structure_artifact(
            replace(
                artifact,
                nodes=artifact.nodes[:leaf_index] + (missing_page_node,) + artifact.nodes[leaf_index + 1 :],
            )
        )

    duplicate = replace(leaf, node_id="node_" + "f" * 64, ordinal=len(artifact.nodes))
    with pytest.raises(StructureValidationError, match="overlap"):
        validate_structure_artifact(replace(artifact, nodes=(*artifact.nodes, duplicate)))

    missing_span_node = replace(leaf, spans=(), display_text="")
    with pytest.raises(StructureValidationError, match="source span|reconstruct|coverage"):
        validate_structure_artifact(
            replace(
                artifact,
                nodes=artifact.nodes[:leaf_index] + (missing_span_node,) + artifact.nodes[leaf_index + 1 :],
            )
        )

    page = next(page for page in artifact.pages if page.page == span.page)
    split_start = span.source_start + 1
    split_text = page.text[split_start : span.source_end]
    midline_span = replace(
        span,
        source_start=split_start,
        text_sha256=sha256_bytes(split_text.encode()),
    )
    midline_node = replace(leaf, spans=(midline_span,), display_text=split_text)
    with pytest.raises(StructureValidationError, match="OCR line"):
        validate_structure_artifact(
            replace(
                artifact,
                nodes=artifact.nodes[:leaf_index] + (midline_node,) + artifact.nodes[leaf_index + 1 :],
            )
        )


def test_validator_fails_closed_for_cross_contract_parent_and_link() -> None:
    first = _artifact("kb", product_code="shared-title-a")
    second = _artifact("kb", product_code="shared-title-b")
    child_index = next(index for index, node in enumerate(first.nodes) if node.parent_id is not None)
    foreign_child = replace(first.nodes[child_index], contract_revision_id=second.contract_revision_id)
    with pytest.raises(StructureValidationError, match="contract revision"):
        validate_structure_artifact(
            replace(
                first, nodes=first.nodes[:child_index] + (foreign_child,) + first.nodes[child_index + 1 :]
            )
        )

    foreign_link = replace(first.links[0], to_node_id=second.nodes[-1].node_id)
    with pytest.raises(StructureValidationError, match="outside this artifact"):
        validate_structure_artifact(replace(first, links=(foreign_link, *first.links[1:])))


def test_derived_views_quote_only_source_and_never_mix_contracts() -> None:
    first = _artifact("samsung", product_code="same-heading-a")
    second = _artifact("samsung", product_code="same-heading-b")
    first_views = build_derived_views(first, maximum_chars=2_000)
    second_views = build_derived_views(second, maximum_chars=2_000)

    assert {view.view_type for view in first_views} >= {
        "TITLE",
        "RAW_ITEM",
        "CONTEXTUAL_ITEM",
        "MAJOR_SECTION",
        "CONTRACT",
    }
    assert {view.contract_revision_id for view in first_views} == {first.contract_revision_id}
    assert {view.contract_revision_id for view in second_views} == {second.contract_revision_id}
    for artifact, views in ((first, first_views), (second, second_views)):
        pages = {page.page: page.text for page in artifact.pages}
        for view in views:
            exact = "".join(pages[span.page][span.source_start : span.source_end] for span in view.spans)
            assert view.display_text == exact
            assert view.input_sha256 == sha256_bytes(view.embedding_input.encode())
            assert not view.embedding_input.startswith("Instruct:")

    tampered = replace(first_views[0], contract_revision_id=second.contract_revision_id)
    with pytest.raises(StructureValidationError, match="crosses a contract"):
        validate_derived_views(
            first,
            (tampered, *first_views[1:]),
            maximum_chars=2_000,
        )


def test_contextual_item_metadata_policy_snapshot_is_lossless_and_source_clean() -> None:
    artifact = _artifact("samsung")
    views = build_derived_views(artifact, maximum_chars=2_000)
    contextual = next(view for view in views if view.view_type == "CONTEXTUAL_ITEM")
    expected_context = (
        "issuer: samsung",
        "product_name: 삼성 테스트 카드",
        "product_code: samsung-card-001",
        "source_version: 2026.08.29.1",
        "effective_date: 2026-08-29",
        f"contract_revision_id: {artifact.contract_revision_id}",
        "major_class: BENEFIT",
        "heading: ## 주요 혜택",
        "heading: 혜택 1. 생활 할인",
    )

    assert contextual_item_policy_payload() == {
        "effective_date_null": "null",
        "field_order": [
            "issuer",
            "product_name",
            "product_code",
            "source_version",
            "effective_date",
            "contract_revision_id",
            "major_class",
        ],
        "heading_label": "heading",
        "line_format": "{label}: {value}",
        "schema_version": "cardrag.contextual-item-context.v1",
        "view_types": ["CONTEXTUAL_ITEM", "DETAIL"],
    }
    assert contextual.context == expected_context
    assert contextual.embedding_input == "\n".join((*expected_context, contextual.display_text))
    assert artifact.product_name not in contextual.display_text
    assert artifact.source_version not in contextual.display_text
    pages = {page.page: page.text for page in artifact.pages}
    assert contextual.display_text == "".join(
        pages[span.page][span.source_start : span.source_end] for span in contextual.spans
    )

    tampered_context = tuple(
        "product_name: 다른 카드" if row.startswith("product_name: ") else row for row in contextual.context
    )
    tampered_input = "\n".join((*tampered_context, contextual.display_text))
    tampered_view = replace(
        contextual,
        context=tampered_context,
        embedding_input=tampered_input,
        input_sha256=sha256_bytes(tampered_input.encode()),
    )
    with pytest.raises(StructureValidationError, match="contextual metadata"):
        validate_derived_views(
            artifact,
            tuple(tampered_view if view is contextual else view for view in views),
            maximum_chars=2_000,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("product_name", " 삼성 테스트 카드", "trimmed"),
        ("product_name", "삼성\n테스트 카드", "control characters"),
        ("source_version", "2026\t08", "control characters"),
        ("effective_date", "20260829", "canonical ISO"),
        ("effective_date", "2026-02-29", "canonical ISO"),
    ],
)
def test_structure_metadata_validation_fails_closed(
    field: str,
    value: str,
    message: str,
) -> None:
    artifact = _artifact("samsung")
    with pytest.raises(StructureValidationError, match=message):
        validate_structure_artifact(replace(artifact, **{field: value}))


def test_contextual_item_effective_date_null_is_explicit() -> None:
    artifact = replace(_artifact("kb"), effective_date=None)
    validate_structure_artifact(artifact)
    contextual = next(
        view
        for view in build_derived_views(artifact, maximum_chars=2_000)
        if view.view_type == "CONTEXTUAL_ITEM"
    )

    assert artifact.payload["effective_date"] is None
    assert "effective_date: null" in contextual.context


def test_metadata_only_source_version_changes_revision_and_context_cache_identity() -> None:
    first = _artifact("kb", source_version="20260829")
    observed = _artifact("kb", source_version="20260829-2")
    first_views = build_derived_views(first, maximum_chars=2_000)
    observed_views = build_derived_views(observed, maximum_chars=2_000)
    first_contextual = next(view for view in first_views if view.view_type == "CONTEXTUAL_ITEM")
    observed_contextual = next(view for view in observed_views if view.view_type == "CONTEXTUAL_ITEM")
    first_raw = next(view for view in first_views if view.view_type == "RAW_ITEM")
    observed_raw = next(view for view in observed_views if view.view_type == "RAW_ITEM")

    assert first.pages == observed.pages
    assert first.pdf_sha256 == observed.pdf_sha256
    assert first.source_sha256 == observed.source_sha256
    assert first.source_id != observed.source_id
    assert first.contract_revision_id != observed.contract_revision_id
    assert first.artifact_sha256 != observed.artifact_sha256
    assert first_contextual.display_text == observed_contextual.display_text
    assert first_contextual.embedding_input != observed_contextual.embedding_input
    assert first_contextual.input_sha256 != observed_contextual.input_sha256
    assert "source_version: 20260829-2" in observed_contextual.context
    assert first_raw.embedding_input == observed_raw.embedding_input
    assert first_raw.input_sha256 == observed_raw.input_sha256


def test_long_item_splits_only_at_structure_boundaries_without_truncation() -> None:
    long_lines = tuple(f"- 상세 조건 {index}: " + "가" * 75 + "\n" for index in range(1, 7))
    artifact = _artifact(
        "woori",
        ("## 주요 혜택\n### 장문 서비스\n" + "".join(long_lines),),
    )
    views = build_derived_views(artifact, maximum_chars=430)
    details = [view for view in views if view.view_type == "DETAIL"]

    assert len(details) >= 2
    assert not any(view.view_type == "CONTRACT" for view in views)
    assert all(len(view.embedding_input) <= 430 for view in details)
    assert all(view.display_text.endswith("\n") for view in details)
    joined = "".join(view.display_text for view in details)
    item = next(
        node for node in artifact.nodes if node.node_type == "ITEM" and node.raw_heading == "### 장문 서비스"
    )
    raw_item = "".join(
        node.display_text
        for node in artifact.nodes
        if node.parent_id == item.node_id and node.node_type in CANONICAL_LEAF_TYPES
    )
    assert joined == raw_item
    assert all(line in joined for line in long_lines)


def test_single_leaf_char_or_token_overflow_fails_instead_of_truncating() -> None:
    artifact = _artifact(
        "woori",
        ("판독 불확실 " + "가" * 400 + "\n",),
    )
    with pytest.raises(StructureValidationError, match="automatic truncation is forbidden"):
        build_derived_views(artifact, maximum_chars=100)

    token_artifact = _artifact(
        "woori",
        ("하나 둘 셋 넷 다섯 여섯 일곱 여덟 아홉 열\n",),
    )
    with pytest.raises(StructureValidationError, match="automatic truncation is forbidden"):
        build_derived_views(
            token_artifact,
            maximum_chars=1_000,
            maximum_tokens=5,
            token_counter=lambda text: len(text.split()),
        )


def test_input_validation_rejects_mixed_documents_and_mismatched_page_marker() -> None:
    with pytest.raises(ValueError, match="one non-empty document_id"):
        parse_structure_artifact(
            (PageRecord("doc-a", 1, "원문\n"), PageRecord("doc-b", 2, "원문\n")),
            issuer="kb",
            product_code="p1",
            product_name="KB국민 테스트 카드",
            source_version="20260829",
            effective_date="2026-08-29",
            document_type="product_description",
            source_id="source_" + "a" * 64,
            pdf_sha256="b" * 64,
        )
    with pytest.raises(StructureValidationError, match="page marker"):
        parse_structure_artifact(
            (PageRecord("doc-a", 1, "## Page 2\n원문\n"),),
            issuer="kb",
            product_code="p1",
            product_name="KB국민 테스트 카드",
            source_version="20260829",
            effective_date="2026-08-29",
            document_type="product_description",
            source_id="source_" + "a" * 64,
            pdf_sha256="b" * 64,
        )


def test_view_contract_has_profile_agnostic_fields_for_export() -> None:
    artifact = _artifact("kb")
    views = build_derived_views(artifact, maximum_chars=2_000)
    view: DerivedView = views[0]
    assert view.node_id
    assert view.view_type
    assert view.display_text
    assert view.embedding_input
    assert len(view.input_sha256) == 64
    assert artifact.spans == tuple(span for node in artifact.nodes for span in node.spans)
