"""Token-bounded evidence chunks that retain parent/condition context."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cardrag.domain import EvidenceSourceSpan, Issuer
from cardrag.pipeline.ocr import PAGE_MARKER, split_pages
from cardrag.pipeline.structure import StructuredDocument, StructuredFact

CHUNK_POLICY_VERSION = "semantic-context.v3"


class EvidenceChunk(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str
    issuer: Issuer
    product_code: str
    product_name: str
    document_id: str
    document_version: str
    effective_date: str
    section_type: str
    page_start: int = Field(gt=0)
    page_end: int = Field(gt=0)
    span_start: int = Field(ge=0)
    span_end: int = Field(gt=0)
    source_spans: tuple[EvidenceSourceSpan, ...] = Field(min_length=1)
    text: str = Field(min_length=1)
    text_sha256: str
    parent_fact_id: str | None
    chunk_policy: str = CHUNK_POLICY_VERSION
    estimated_tokens: int = Field(gt=0)

    @model_validator(mode="after")
    def verify_hash_and_range(self) -> Self:
        if self.page_end < self.page_start or self.span_end <= self.span_start:
            raise ValueError("invalid chunk source range")
        if hashlib.sha256(self.text.encode()).hexdigest() != self.text_sha256:
            raise ValueError("chunk text hash mismatch")
        ordered = tuple(sorted(self.source_spans, key=lambda span: (span.page, span.start, span.end)))
        if self.source_spans != ordered or len(set(self.source_spans)) != len(self.source_spans):
            raise ValueError("chunk source spans must be unique and in document order")
        for previous, current in zip(ordered, ordered[1:], strict=False):
            if previous.page == current.page and current.start < previous.end:
                raise ValueError("chunk source spans must not overlap")
        if (
            self.page_start != min(span.page for span in ordered)
            or self.page_end != max(span.page for span in ordered)
            or self.span_start != min(span.start for span in ordered)
            or self.span_end != max(span.end for span in ordered)
        ):
            raise ValueError("chunk envelope differs from its source fragments")
        return self


def estimate_tokens(text: str) -> int:
    # Conservative language-agnostic upper estimate; provider adapters enforce their exact limit too.
    return max(1, (len(text.encode("utf-8")) + 2) // 3)


def build_chunks(
    structured: StructuredDocument,
    *,
    issuer: Issuer,
    product_code: str,
    product_name: str,
    document_version: str,
    effective_date: str,
    max_tokens: int = 800,
    ocr_text: str | None = None,
) -> tuple[EvidenceChunk, ...]:
    by_id = {fact.fact_id: fact for fact in structured.facts}
    children: dict[str, list[StructuredFact]] = defaultdict(list)
    for fact in structured.facts:
        if fact.parent_fact_id:
            children[fact.parent_fact_id].append(fact)

    def ancestry(fact: StructuredFact) -> list[StructuredFact]:
        path = [fact]
        seen = {fact.fact_id}
        parent_id = fact.parent_fact_id
        while parent_id is not None:
            if parent_id in seen or parent_id not in by_id:
                raise ValueError("structured relationship graph is invalid")
            parent = by_id[parent_id]
            path.append(parent)
            seen.add(parent_id)
            parent_id = parent.parent_fact_id
        return list(reversed(path))

    # Emit one exact context path for every non-root fact.  This keeps a
    # concrete benefit beside its condition/exclusion/footnote even after a
    # page boundary, and makes every citation the ordered union of the actual
    # source fragments. A childless root still remains searchable by itself.
    groups = [
        ancestry(fact)
        for fact in structured.facts
        if fact.parent_fact_id is not None or not children.get(fact.fact_id)
    ]

    chunks: list[EvidenceChunk] = []
    for group in groups:
        chunks.append(
            _chunk_from_facts(
                group,
                issuer=issuer,
                product_code=product_code,
                product_name=product_name,
                document_id=structured.document_id,
                document_version=document_version,
                effective_date=effective_date,
                max_tokens=max_tokens,
                ocr_text=ocr_text,
            )
        )
    return tuple(chunks)


def _chunk_from_facts(
    facts: list[StructuredFact],
    *,
    issuer: Issuer,
    product_code: str,
    product_name: str,
    document_id: str,
    document_version: str,
    effective_date: str,
    max_tokens: int,
    ocr_text: str | None,
) -> EvidenceChunk:
    unique: list[StructuredFact] = list(dict.fromkeys(facts))
    for fact in unique:
        if fact.value not in fact.span.quote:
            raise ValueError("chunk fact is not backed by its source quote")
        if ocr_text is not None:
            page_text = next(
                (
                    value
                    for value in split_pages(ocr_text)
                    if (match := PAGE_MARKER.match(value)) is not None
                    and int(match.group(1)) == fact.span.page
                ),
                None,
            )
            if (
                page_text is None
                or not 0 <= fact.span.start < fact.span.end <= len(page_text)
                or page_text[fact.span.start : fact.span.end] != fact.span.quote
            ):
                raise ValueError("chunk source fragment is not exactly backed by canonical OCR")
    text = "\n".join(fact.value for fact in unique)
    tokens = estimate_tokens(text)
    if tokens > max_tokens:
        raise ValueError("a single evidence unit exceeds the token limit; manual/table-aware split required")
    text_hash = hashlib.sha256(text.encode()).hexdigest()
    pages = [fact.span.page for fact in unique]
    starts = [fact.span.start for fact in unique]
    ends = [fact.span.end for fact in unique]
    source_spans = tuple(
        EvidenceSourceSpan(
            page=fact.span.page,
            start=fact.span.start,
            end=fact.span.end,
            quote_sha256=fact.span.quote_sha256,
        )
        for fact in sorted(unique, key=lambda item: (item.span.page, item.span.start, item.span.end))
    )
    span_identity = json.dumps(
        [span.model_dump(mode="json") for span in source_spans],
        sort_keys=True,
        separators=(",", ":"),
    )
    evidence_id = hashlib.sha256(
        f"{issuer.value}\0{document_id}\0{span_identity}\0{text_hash}".encode()
    ).hexdigest()
    return EvidenceChunk(
        evidence_id=evidence_id,
        issuer=issuer,
        product_code=product_code,
        product_name=product_name,
        document_id=document_id,
        document_version=document_version,
        effective_date=effective_date,
        # The path supplies retrieval context, while its leaf is the fact the
        # chunk represents. Filters must therefore see ``condition`` or
        # ``*_exclusion`` rather than the ancestor benefit heading.
        section_type=unique[-1].section_type.value,
        page_start=min(pages),
        page_end=max(pages),
        span_start=min(starts),
        span_end=max(ends),
        source_spans=source_spans,
        text=text,
        text_sha256=text_hash,
        parent_fact_id=unique[-1].parent_fact_id,
        estimated_tokens=tokens,
    )
