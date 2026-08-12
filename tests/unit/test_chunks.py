from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from cardrag.domain import Issuer
from cardrag.pipeline.chunks import build_chunks, estimate_tokens
from cardrag.pipeline.ocr import PAGE_MARKER, split_pages
from cardrag.pipeline.structure import (
    ExtractionMethod,
    SectionType,
    SourceSpan,
    StructuredDocument,
    StructuredFact,
)


def _fact(
    ocr_text: str,
    value: str,
    *,
    fact_id: str,
    section_type: SectionType,
    parent_fact_id: str | None = None,
) -> StructuredFact:
    start = ocr_text.index(value)
    return StructuredFact(
        fact_id=fact_id,
        section_type=section_type,
        value=value,
        span=SourceSpan(
            page=1,
            start=start,
            end=start + len(value),
            quote=value,
            quote_sha256=hashlib.sha256(value.encode()).hexdigest(),
        ),
        extraction_method=ExtractionMethod.RULE,
        confidence=1.0,
        parent_fact_id=parent_fact_id,
        relation="context_of" if parent_fact_id else None,
    )


def _structured_context() -> tuple[StructuredDocument, str, tuple[str, ...]]:
    heading = "# 대중교통 혜택"
    children = (
        "조건 A: 전월 이용실적 30만원 이상인 경우에만 제공",
        "조건 B: 월 2회 및 월 최대 5,000원까지만 제공",
        "제외 C: 세금 및 상품권 결제는 실적에 포함하지 않음",
    )
    ocr_text = "## Page 1\n" + "\n".join((heading, *children)) + "\n"
    root = _fact(ocr_text, heading, fact_id="root", section_type=SectionType.BENEFIT)
    facts = (root,) + tuple(
        _fact(
            ocr_text,
            child,
            fact_id=f"child-{index}",
            section_type=SectionType.CONDITION,
            parent_fact_id=root.fact_id,
        )
        for index, child in enumerate(children, 1)
    )
    return (
        StructuredDocument(
            document_id="document-context",
            ocr_sha256=hashlib.sha256(ocr_text.encode()).hexdigest(),
            facts=facts,
            validation_status="passed",
        ),
        heading,
        children,
    )


def test_context_chunks_repeat_parent_and_never_exceed_token_cap() -> None:
    structured, heading, children = _structured_context()
    max_tokens = max(estimate_tokens(f"{heading}\n{child}") for child in children)
    assert estimate_tokens(f"{heading}\n{children[0]}\n{children[1]}") > max_tokens

    chunks = build_chunks(
        structured,
        issuer=Issuer.WOORI,
        product_code="CARD-1",
        product_name="합성 카드",
        document_version="v1",
        effective_date="2026-08-12",
        max_tokens=max_tokens,
    )

    assert len(chunks) == len(children)
    assert {chunk.text.splitlines()[1] for chunk in chunks} == set(children)
    assert all(chunk.text.splitlines()[0] == heading for chunk in chunks)
    assert all(chunk.estimated_tokens <= max_tokens for chunk in chunks)
    assert all(chunk.issuer is Issuer.WOORI for chunk in chunks)
    assert all(chunk.document_id == structured.document_id for chunk in chunks)
    assert all(chunk.text_sha256 == hashlib.sha256(chunk.text.encode()).hexdigest() for chunk in chunks)
    assert all(len(chunk.source_spans) == 2 for chunk in chunks)
    assert all(chunk.source_spans[0].quote_sha256 == hashlib.sha256(heading.encode()).hexdigest() for chunk in chunks)
    assert [span.quote_sha256 for span in chunks[-1].source_spans] == [
        hashlib.sha256(heading.encode()).hexdigest(),
        hashlib.sha256(children[-1].encode()).hexdigest(),
    ]

    repeated = build_chunks(
        structured,
        issuer=Issuer.WOORI,
        product_code="CARD-1",
        product_name="합성 카드",
        document_version="v1",
        effective_date="2026-08-12",
        max_tokens=max_tokens,
    )
    assert [chunk.evidence_id for chunk in repeated] == [chunk.evidence_id for chunk in chunks]


def test_unsplittable_context_unit_fails_instead_of_exceeding_cap() -> None:
    structured, heading, _ = _structured_context()

    with pytest.raises(ValueError, match="exceeds the token limit"):
        build_chunks(
            structured,
            issuer=Issuer.KB,
            product_code="CARD-2",
            product_name="합성 카드",
            document_version="v1",
            effective_date="2026-08-12",
            max_tokens=estimate_tokens(heading),
        )


def test_extracted_cross_page_relation_yields_exact_multispan_context() -> None:
    from cardrag.pipeline.structure import extract_structure

    ocr_text = (
        "## Page 1\n\n# 혜택\n대중교통 10% 할인, 월 최대 5,000원\n"
        "전월 이용실적 30만원 이상\n\n"
        "## Page 2\n\n# 실적 제외\n세금 및 상품권은 실적에 포함하지 않습니다.\n\n"
        "# 필수 안내\n[1] 할인 한도는 매월 말일까지 적용됩니다.\n"
    )
    structured = extract_structure("cross-page", ocr_text)
    chunks = build_chunks(
        structured,
        issuer=Issuer.SHINHAN,
        product_code="SH-1",
        product_name="합성 카드",
        document_version="v1",
        effective_date="2026-08-12",
        max_tokens=800,
        ocr_text=ocr_text,
    )

    exclusion = next(chunk for chunk in chunks if "상품권은 실적" in chunk.text)
    footnote = next(chunk for chunk in chunks if "매월 말일까지" in chunk.text)
    for chunk in (exclusion, footnote):
        assert "대중교통 10% 할인" in chunk.text
        assert chunk.page_start == 1 and chunk.page_end == 2
        assert len(chunk.source_spans) >= 4
        assert [(span.page, span.start, span.end) for span in chunk.source_spans] == sorted(
            (span.page, span.start, span.end) for span in chunk.source_spans
        )
    assert "# 실적 제외" in exclusion.text
    assert "# 필수 안내" in footnote.text
    assert exclusion.section_type == SectionType.PERFORMANCE_EXCLUSION.value
    assert footnote.section_type == SectionType.MANDATORY_NOTICE.value
    assert exclusion.parent_fact_id is not None
    assert footnote.parent_fact_id is not None


@pytest.mark.parametrize("document_index", range(4))
def test_gold_documents_build_real_related_multispan_chunks(document_index: int) -> None:
    from cardrag.pipeline.structure import extract_structure

    gold_path = Path(__file__).parents[1] / "fixtures/gold/gold_set.v1.json"
    document = json.loads(gold_path.read_text(encoding="utf-8"))["documents"][document_index]
    structured = extract_structure(document["key"], document["ocr"])
    chunks = build_chunks(
        structured,
        issuer=Issuer(document["issuer"]),
        product_code=document["product_code"],
        product_name=document["product_name"],
        document_version=document["source_version"],
        effective_date=document["effective_date"],
        max_tokens=800,
        ocr_text=document["ocr"],
    )

    required_context_types = {
        SectionType.PERFORMANCE_REQUIREMENT,
        SectionType.PERFORMANCE_EXCLUSION,
    }
    facts_by_type = {
        kind: [fact for fact in structured.facts if fact.section_type is kind]
        for kind in required_context_types
    }
    assert all(facts_by_type.values())
    assert all(fact.parent_fact_id and fact.relation for facts in facts_by_type.values() for fact in facts)
    assert all(
        any(chunk.section_type == kind.value and len(chunk.source_spans) > 1 for chunk in chunks)
        for kind in required_context_types
    )
    assert any(
        chunk.section_type == SectionType.ANNUAL_FEE.value
        and any(token in chunk.text for token in ("0원", "12,000원", "15,000원", "18,000원"))
        for chunk in chunks
    )
    page_map = {
        int(marker.group(1)): page
        for page in split_pages(document["ocr"])
        if (marker := PAGE_MARKER.match(page)) is not None
    }
    for chunk in chunks:
        for span in chunk.source_spans:
            quote = page_map[span.page][span.start : span.end]
            assert hashlib.sha256(quote.encode()).hexdigest() == span.quote_sha256
