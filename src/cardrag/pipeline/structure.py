"""Deterministic, evidence-bound structure extraction."""

from __future__ import annotations

import hashlib
import re
from enum import StrEnum
from typing import Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cardrag.pipeline.ocr import PAGE_MARKER, split_pages

STRUCTURE_SCHEMA_VERSION = "structured-document.v1"


class SectionType(StrEnum):
    PRODUCT = "product"
    ANNUAL_FEE = "annual_fee"
    BENEFIT = "benefit"
    CONDITION = "condition"
    PERFORMANCE_REQUIREMENT = "performance_requirement"
    PERFORMANCE_EXCLUSION = "performance_exclusion"
    BENEFIT_EXCLUSION = "benefit_exclusion"
    MANDATORY_NOTICE = "mandatory_notice"
    NOTICE = "notice"
    OTHER = "other"


class ExtractionMethod(StrEnum):
    RULE = "rule"
    LLM_ASSISTED = "llm_assisted"


class SourceSpan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    page: int = Field(gt=0)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote: str = Field(min_length=1)
    quote_sha256: str

    @model_validator(mode="after")
    def valid_span(self) -> Self:
        if self.end <= self.start:
            raise ValueError("span end must exceed start")
        if hashlib.sha256(self.quote.encode()).hexdigest() != self.quote_sha256:
            raise ValueError("quote hash mismatch")
        return self


class StructuredFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_id: str
    section_type: SectionType
    value: str = Field(min_length=1)
    span: SourceSpan
    extraction_method: ExtractionMethod
    confidence: float = Field(ge=0, le=1)
    parent_fact_id: str | None = None
    relation: str | None = None


class StructuredDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = STRUCTURE_SCHEMA_VERSION
    document_id: str
    ocr_sha256: str
    facts: tuple[StructuredFact, ...]
    validation_status: str
    warnings: tuple[str, ...] = ()


class StructureEnhancer(Protocol):
    async def enhance(
        self, text: str, candidates: tuple[StructuredFact, ...]
    ) -> tuple[StructuredFact, ...]: ...


HEADINGS: tuple[tuple[re.Pattern[str], SectionType], ...] = (
    (re.compile(r"연회비"), SectionType.ANNUAL_FEE),
    (re.compile(r"실적.*제외|제외.*실적"), SectionType.PERFORMANCE_EXCLUSION),
    (re.compile(r"전월\s*(?:이용)?실적"), SectionType.PERFORMANCE_REQUIREMENT),
    (
        re.compile(
            r"(?:혜택|할인|적립|서비스).*(?:제외|제공하지|미제공)"
            r"|(?:제외|제공하지).*(?:혜택|할인|적립|서비스)"
        ),
        SectionType.BENEFIT_EXCLUSION,
    ),
    (re.compile(r"혜택|적립|할인|서비스"), SectionType.BENEFIT),
    (re.compile(r"필수.*안내"), SectionType.MANDATORY_NOTICE),
    (re.compile(r"상품|카드"), SectionType.PRODUCT),
    # A title such as ``상품 안내`` is a product section, not a generic
    # notice.  Keep the broad notice matcher after the domain-specific title
    # matcher so heading classification is deterministic.
    (re.compile(r"유의|안내"), SectionType.NOTICE),
)


def classify(text: str) -> SectionType:
    normalized = re.sub(r"[#*|]", " ", text).strip()
    for pattern, section_type in HEADINGS:
        if pattern.search(normalized):
            return section_type
    if re.search(r"(?:이상|충족|조건|한도|횟수)", normalized):
        return SectionType.CONDITION
    return SectionType.OTHER


_CONTEXT_RELATIONS: dict[SectionType, str] = {
    SectionType.CONDITION: "condition_of",
    SectionType.PERFORMANCE_REQUIREMENT: "performance_requirement_of",
    SectionType.PERFORMANCE_EXCLUSION: "performance_exclusion_of",
    SectionType.BENEFIT_EXCLUSION: "benefit_exclusion_of",
    SectionType.MANDATORY_NOTICE: "footnote_for",
    SectionType.NOTICE: "notice_for",
}


def _is_heading(line: str, section_type: SectionType) -> bool:
    """Recognize headings without treating every short domain sentence as one.

    Canonical OCR prompts preserve Markdown headings.  A narrow bare-heading
    fallback is retained for issuer PDFs whose visual title is emitted without
    ``#``; it deliberately rejects values, punctuation and sentence endings.
    """

    if line.lstrip().startswith("#"):
        return True
    normalized = re.sub(r"[#*|]", " ", line).strip()
    return bool(
        section_type != SectionType.OTHER
        and len(normalized) <= 24
        and not re.search(r"\d|[,.:;()\[\]]|(?:다|요)\.?$", normalized)
    )


def _relation_for(section_type: SectionType, *, parent_is_heading: bool) -> str:
    if section_type == SectionType.BENEFIT:
        return "benefit_detail"
    if section_type in _CONTEXT_RELATIONS:
        return _CONTEXT_RELATIONS[section_type]
    return "detail_of" if parent_is_heading else "context_of"


def _section_in_context(
    classified: SectionType,
    *,
    current_heading: StructuredFact | None,
) -> SectionType:
    """Let an explicit exclusion/notice heading dominate broad word matches.

    For example, ``상품권`` is not a product fact under ``# 실적 제외``, and a
    sentence containing ``할인`` is still a mandatory notice under that
    heading. More specific nested headings are handled before this function.
    """

    if current_heading is None:
        return classified
    if classified == SectionType.OTHER and current_heading.section_type != SectionType.OTHER:
        # Table headers/rows and terse values rarely repeat their semantic
        # label. Preserve the explicit Markdown section (notably annual fee)
        # so section filters do not lose the actual values.
        return current_heading.section_type
    if classified in {
        SectionType.PERFORMANCE_REQUIREMENT,
        SectionType.PERFORMANCE_EXCLUSION,
        SectionType.BENEFIT_EXCLUSION,
    }:
        return classified
    contextual = current_heading.section_type
    if contextual in {
        SectionType.CONDITION,
        SectionType.PERFORMANCE_EXCLUSION,
        SectionType.BENEFIT_EXCLUSION,
        SectionType.MANDATORY_NOTICE,
        SectionType.NOTICE,
    }:
        return contextual
    return classified


def validate_span(ocr_text: str, span: SourceSpan) -> None:
    page_text = next(
        (
            value
            for value in split_pages(ocr_text)
            if (match := PAGE_MARKER.match(value)) is not None and int(match.group(1)) == span.page
        ),
        None,
    )
    if page_text is None:
        raise ValueError("span page is absent from OCR")
    if span.end > len(page_text) or page_text[span.start : span.end] != span.quote:
        raise ValueError("structured value is not exactly backed by its canonical OCR page")


def validate_facts(ocr_text: str, facts: tuple[StructuredFact, ...]) -> None:
    by_id = {fact.fact_id: fact for fact in facts}
    ids = set(by_id)
    if len(ids) != len(facts):
        raise ValueError("duplicate fact ID")
    for fact in facts:
        validate_span(ocr_text, fact.span)
        if fact.value not in fact.span.quote:
            raise ValueError("fact value is not present in its evidence quote")
        if fact.parent_fact_id and fact.parent_fact_id not in ids:
            raise ValueError("fact parent does not exist")
        if bool(fact.parent_fact_id) != bool(fact.relation):
            raise ValueError("fact parent and relation must be declared together")
        visited = {fact.fact_id}
        parent_id = fact.parent_fact_id
        while parent_id is not None:
            if parent_id in visited:
                raise ValueError("fact relationship graph contains a cycle")
            visited.add(parent_id)
            parent_id = by_id[parent_id].parent_fact_id


def extract_structure(document_id: str, ocr_text: str) -> StructuredDocument:
    if not PAGE_MARKER.search(ocr_text):
        raise ValueError("canonical OCR has no page markers")
    facts: list[StructuredFact] = []
    current_heading: StructuredFact | None = None
    last_benefit_heading: StructuredFact | None = None
    last_benefit_detail: StructuredFact | None = None
    for page_text in split_pages(ocr_text):
        marker = PAGE_MARKER.match(page_text)
        if marker is None:
            raise ValueError("canonical OCR page is missing its marker")
        current_page = int(marker.group(1))
        for line_match in re.finditer(r"^(.+?)\s*$", page_text, flags=re.MULTILINE):
            line = line_match.group(1)
            if re.fullmatch(r"## Page (\d+)", line):
                continue
            stripped = line.strip()
            if not stripped:
                continue
            start = line_match.start(1) + len(line) - len(line.lstrip())
            end = start + len(stripped)
            kind = classify(stripped)
            quote_hash = hashlib.sha256(stripped.encode()).hexdigest()
            fact_id = hashlib.sha256(
                f"{document_id}\0{current_page}\0{start}\0{end}\0{quote_hash}".encode()
            ).hexdigest()
            is_heading = _is_heading(stripped, kind)
            if not is_heading:
                kind = _section_in_context(kind, current_heading=current_heading)
            parent_fact: StructuredFact | None
            if is_heading:
                # Conditions/exclusions/notices following a benefit remain
                # explicitly attached to the latest concrete benefit.  This
                # relationship may cross a page boundary in one disclosure.
                parent_fact = (
                    last_benefit_detail or last_benefit_heading if kind in _CONTEXT_RELATIONS else None
                )
            elif kind == SectionType.BENEFIT or (
                kind in _CONTEXT_RELATIONS
                and current_heading is not None
                and current_heading.section_type in _CONTEXT_RELATIONS
            ):
                parent_fact = current_heading
            elif kind in _CONTEXT_RELATIONS and last_benefit_detail is not None:
                parent_fact = last_benefit_detail
            else:
                parent_fact = current_heading
            relation = (
                _relation_for(
                    kind,
                    parent_is_heading=bool(
                        parent_fact and _is_heading(parent_fact.value, parent_fact.section_type)
                    ),
                )
                if parent_fact is not None
                else None
            )
            fact = StructuredFact(
                fact_id=fact_id,
                section_type=kind,
                value=stripped,
                span=SourceSpan(
                    page=current_page,
                    start=start,
                    end=end,
                    quote=stripped,
                    quote_sha256=quote_hash,
                ),
                extraction_method=ExtractionMethod.RULE,
                confidence=1.0,
                parent_fact_id=parent_fact.fact_id if parent_fact else None,
                relation=relation,
            )
            facts.append(fact)
            if is_heading:
                current_heading = fact
                if kind == SectionType.BENEFIT:
                    last_benefit_heading = fact
                    last_benefit_detail = None
            elif kind == SectionType.BENEFIT and current_heading is not None:
                last_benefit_detail = fact
    result = tuple(facts)
    validate_facts(ocr_text, result)
    return StructuredDocument(
        document_id=document_id,
        ocr_sha256=hashlib.sha256(ocr_text.encode()).hexdigest(),
        facts=result,
        validation_status="passed",
    )


async def enhance_and_validate(
    document: StructuredDocument,
    ocr_text: str,
    enhancer: StructureEnhancer | None,
) -> StructuredDocument:
    if enhancer is None:
        return document
    candidates = await enhancer.enhance(ocr_text, document.facts)
    validate_facts(ocr_text, candidates)
    return document.model_copy(update={"facts": candidates})
