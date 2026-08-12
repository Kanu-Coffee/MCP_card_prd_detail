from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from cardrag.pipeline.ocr import critical_tokens, split_pages
from cardrag.pipeline.structure import (
    SectionType,
    SourceSpan,
    StructuredFact,
    classify,
    enhance_and_validate,
    extract_structure,
    validate_span,
)

GOLD_OCR = Path(__file__).parents[1] / "fixtures/gold/card_disclosure_ocr.md"


def _gold_text() -> str:
    return GOLD_OCR.read_text(encoding="utf-8")


def test_exclusion_classification_precedes_broad_performance_and_benefit_matches() -> None:
    assert classify("전월 이용실적 제외 대상") is SectionType.PERFORMANCE_EXCLUSION
    assert classify("할인에서 제외됩니다") is SectionType.BENEFIT_EXCLUSION
    assert classify("할인을 제공하지 않습니다") is SectionType.BENEFIT_EXCLUSION


def test_rule_structure_preserves_exact_page_spans_numbers_and_negation() -> None:
    ocr_text = _gold_text()
    document = extract_structure("gold-document", ocr_text)

    assert document.validation_status == "passed"
    assert document.ocr_sha256 == hashlib.sha256(ocr_text.encode()).hexdigest()
    assert {fact.section_type for fact in document.facts} >= {
        SectionType.BENEFIT,
        SectionType.PERFORMANCE_REQUIREMENT,
        SectionType.PERFORMANCE_EXCLUSION,
        SectionType.MANDATORY_NOTICE,
    }
    pages = {int(page.splitlines()[0].removeprefix("## Page ")): page for page in split_pages(ocr_text)}
    for fact in document.facts:
        assert pages[fact.span.page][fact.span.start : fact.span.end] == fact.span.quote == fact.value
        validate_span(ocr_text, fact.span)

    structured_text = "\n".join(fact.value for fact in document.facts)
    assert critical_tokens(structured_text) == critical_tokens(ocr_text)

    benefit_heading = next(
        fact for fact in document.facts if fact.value == "# 혜택"
    )
    benefit = next(fact for fact in document.facts if "대중교통 10%" in fact.value)
    requirement = next(fact for fact in document.facts if "30만원 이상" in fact.value)
    exclusion = next(fact for fact in document.facts if "상품권은 포함하지" in fact.value)
    notice_heading = next(fact for fact in document.facts if fact.value == "# 필수 안내")
    footnote = next(fact for fact in document.facts if "해외 이용 수수료" in fact.value)

    assert (benefit.parent_fact_id, benefit.relation) == (
        benefit_heading.fact_id,
        "benefit_detail",
    )
    assert (requirement.parent_fact_id, requirement.relation) == (
        benefit.fact_id,
        "performance_requirement_of",
    )
    assert (exclusion.parent_fact_id, exclusion.relation) == (
        benefit.fact_id,
        "performance_exclusion_of",
    )
    assert (notice_heading.parent_fact_id, notice_heading.relation) == (
        benefit.fact_id,
        "footnote_for",
    )
    assert (footnote.parent_fact_id, footnote.relation) == (
        notice_heading.fact_id,
        "benefit_exclusion_of",
    )


def test_exact_span_validation_rejects_a_cross_page_quote() -> None:
    ocr_text = _gold_text()
    page = split_pages(ocr_text)[0]
    start = page.index("실적 제외")
    end = len(page) + 1
    quote = page[start:]
    span = SourceSpan(
        page=1,
        start=start,
        end=end,
        quote=quote,
        quote_sha256=hashlib.sha256(quote.encode()).hexdigest(),
    )

    with pytest.raises(ValueError, match="canonical OCR page"):
        validate_span(ocr_text, span)


def test_second_page_spans_are_page_local_and_recomputable() -> None:
    ocr_text = "## Page 1\n\n첫 페이지 연회비 10,000원\n\n## Page 2\n\n두 번째 페이지 할인 20%\n"
    document = extract_structure("two-page", ocr_text)
    page_two = split_pages(ocr_text)[1]
    fact = next(item for item in document.facts if item.span.page == 2)

    assert fact.span.start == page_two.index(fact.value)
    quote = page_two[fact.span.start : fact.span.end]
    assert quote == fact.value
    assert hashlib.sha256(quote.encode()).hexdigest() == fact.span.quote_sha256


async def test_enhancer_fabrication_is_rejected() -> None:
    ocr_text = _gold_text()
    document = extract_structure("gold-document", ocr_text)

    class FabricatingEnhancer:
        async def enhance(
            self, text: str, candidates: tuple[StructuredFact, ...]
        ) -> tuple[StructuredFact, ...]:
            del text
            fabricated = candidates[0].model_copy(update={"value": "원문에 없는 99% 캐시백"})
            return (fabricated, *candidates[1:])

    with pytest.raises(ValueError, match="fact value is not present"):
        await enhance_and_validate(document, ocr_text, FabricatingEnhancer())
