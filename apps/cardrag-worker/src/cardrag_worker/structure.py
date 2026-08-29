"""Deterministic, lossless structure extraction from verified OCR pages.

The parser in this module deliberately uses no language model.  Its canonical
leaves partition the OCR source at complete line boundaries, while container
nodes and links add conservative structure without becoming a second source of
truth.  Ambiguous text is retained as ``UNCLASSIFIED``.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Final, Literal

from cardrag_core import canonical_json_bytes, canonical_sha256, issuer_code, sha256_bytes

from .contracts import PageRecord

STRUCTURE_SCHEMA: Final = "cardrag.structure.v2"
STRUCTURE_PROFILE_SCHEMA: Final = "cardrag.issuer-structure-profile.v1"
STRUCTURE_COVERAGE_SCHEMA: Final = "cardrag.structure-coverage.v1"
STRUCTURE_SOURCE_SCHEMA: Final = "cardrag.structure-source.v1"
STRUCTURE_VIEW_SCHEMA: Final = "cardrag.structure-views.v1"
CONTEXTUAL_ITEM_CONTEXT_POLICY: Final = "cardrag.contextual-item-context.v1"
UNCLASSIFIED_FALLBACK_POLICY: Final = "cardrag.structure-unclassified-fallback.v1"

_CONTEXT_FIELD_ORDER: Final = (
    "issuer",
    "product_name",
    "product_code",
    "source_version",
    "effective_date",
    "contract_revision_id",
    "major_class",
)
_CONTEXT_EFFECTIVE_DATE_NULL: Final = "null"
_CONTEXT_HEADING_LABEL: Final = "heading"

MajorClass = Literal["BENEFIT", "NOTICE", "MIXED", "UNKNOWN"]
NodeType = Literal[
    "ROOT",
    "MAJOR_SECTION",
    "ITEM",
    "PARAGRAPH",
    "LIST_ITEM",
    "TABLE",
    "TABLE_ROW",
    "FOOTNOTE",
    "BOILERPLATE",
    "UNCLASSIFIED",
]
LinkType = Literal["CONTINUATION_OF", "FOOTNOTE_OF", "APPLIES_TO", "PREVIOUS", "NEXT"]
TableRole = Literal["HEADER", "SEPARATOR", "BODY"]
ViewType = Literal[
    "TITLE",
    "RAW_ITEM",
    "CONTEXTUAL_ITEM",
    "DETAIL",
    "MAJOR_SECTION",
    "CONTRACT",
]

CANONICAL_LEAF_TYPES: Final[frozenset[str]] = frozenset(
    {"PARAGRAPH", "LIST_ITEM", "TABLE_ROW", "FOOTNOTE", "BOILERPLATE", "UNCLASSIFIED"}
)
_CONTAINER_TYPES: Final[frozenset[str]] = frozenset({"ROOT", "MAJOR_SECTION", "ITEM", "TABLE"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_ID = re.compile(r"^source_[0-9a-f]{64}$")
_PAGE_MARKER = re.compile(r"^## Page ([1-9][0-9]*)\s*$")
_MARKDOWN_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)\s*$")
_LIST_ITEM = re.compile(r"^\s*(?:[-+*]|\d+[.)]|[가-힣][.)])\s+\S")
_FOOTNOTE = re.compile(r"^\s*(?:※|\*{1,3}(?![\s*])|\[\d+]|주\s*[):.]|[¹²³⁴⁵⁶⁷⁸⁹])\s*\S")
_MARKDOWN_BULLET = re.compile(r"^\s*[-+*]\s+\S")
_TABLE_SEPARATOR_CELL = re.compile(r"^:?-{3,}:?$")
_PAGE_COUNTER = re.compile(
    r"^\s*-?\s*(?:(?:page|페이지)\s*)?\d+\s*(?:(?:/|of)\s*\d+)?\s*-?\s*$",
    re.IGNORECASE,
)

_BENEFIT_TERMS: Final = (
    "혜택",
    "서비스",
    "할인",
    "적립",
    "캐시백",
    "포인트",
    "마일리지",
    "무료",
)
_NOTICE_TERMS: Final = (
    "이용 전 확인",
    "적용 안내",
    "제외 대상",
    "제외",
    "전월 실적",
    "공통 확인",
    "유의사항",
    "주의사항",
    "연회비",
    "해외 이용",
    "의무표시",
    "조건",
    "한도",
)


class StructureValidationError(ValueError):
    """Raised when a structure artifact cannot prove its source contract."""


@dataclass(frozen=True, slots=True)
class IssuerParserProfile:
    issuer: str
    profile_id: str
    issuer_markers: tuple[str, ...]
    major_prefixes: tuple[str, ...]
    item_prefixes: tuple[str, ...]
    benefit_terms: tuple[str, ...] = _BENEFIT_TERMS
    notice_terms: tuple[str, ...] = _NOTICE_TERMS
    edge_line_window: int = 2

    @property
    def payload(self) -> dict[str, object]:
        return {
            "benefit_terms": list(self.benefit_terms),
            "edge_line_window": self.edge_line_window,
            "issuer": self.issuer,
            "issuer_markers": list(self.issuer_markers),
            "item_prefixes": list(self.item_prefixes),
            "major_prefixes": list(self.major_prefixes),
            "profile_id": self.profile_id,
            "schema_version": STRUCTURE_PROFILE_SCHEMA,
        }

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.payload)


_PROFILES: Final[dict[str, IssuerParserProfile]] = {
    "kb": IssuerParserProfile(
        issuer="kb",
        profile_id="cardrag.issuer-profile.kb.v1",
        issuer_markers=("kb국민카드", "국민카드"),
        major_prefixes=("■", "▣", "◆"),
        item_prefixes=("□", "▶", "◇", "●"),
    ),
    "samsung": IssuerParserProfile(
        issuer="samsung",
        profile_id="cardrag.issuer-profile.samsung.v1",
        issuer_markers=("삼성카드",),
        major_prefixes=("■", "▣", "◆"),
        item_prefixes=("혜택 ", "서비스 ", "□", "▶"),
    ),
    "shinhan": IssuerParserProfile(
        issuer="shinhan",
        profile_id="cardrag.issuer-profile.shinhan.v1",
        issuer_markers=("신한카드",),
        major_prefixes=("■", "▣", "◎"),
        item_prefixes=("□", "▶", "○", "●"),
    ),
    "woori": IssuerParserProfile(
        issuer="woori",
        profile_id="cardrag.issuer-profile.woori.v1",
        issuer_markers=("우리카드",),
        major_prefixes=("■", "▣", "Ⅰ", "Ⅱ", "Ⅲ", "Ⅳ"),
        item_prefixes=("□", "▶", "1.", "2.", "3.", "4."),
    ),
}


def issuer_parser_profile(issuer: str) -> IssuerParserProfile:
    """Return the sealed issuer profile, with a lossless generic fallback."""

    issuer_code(issuer)
    profile = _PROFILES.get(issuer)
    if profile is not None:
        return profile
    return IssuerParserProfile(
        issuer=issuer,
        profile_id=f"cardrag.issuer-profile.{issuer}.generic.v1",
        issuer_markers=(issuer,),
        major_prefixes=("■", "▣", "◆"),
        item_prefixes=("□", "▶", "◇", "●"),
    )


def make_product_lineage_id(*, issuer: str, product_code: str, document_type: str) -> str:
    issuer_code(issuer)
    _require_trimmed(product_code, "product_code")
    _require_trimmed(document_type, "document_type")
    return "lineage_" + canonical_sha256(
        {
            "document_type": document_type,
            "issuer": issuer,
            "product_code": product_code,
        }
    )


def make_contract_revision_id(*, product_lineage_id: str, source_id: str, pdf_sha256: str) -> str:
    if not product_lineage_id.startswith("lineage_") or len(product_lineage_id) != 72:
        raise ValueError("invalid product_lineage_id")
    if not _SOURCE_ID.fullmatch(source_id):
        raise ValueError("invalid source_id")
    if not _SHA256.fullmatch(pdf_sha256):
        raise ValueError("invalid pdf_sha256")
    return "revision_" + canonical_sha256(
        {
            "pdf_sha256": pdf_sha256,
            "product_lineage_id": product_lineage_id,
            "source_id": source_id,
        }
    )


@dataclass(frozen=True, slots=True)
class StructurePage:
    page: int
    text: str
    text_sha256: str

    @property
    def payload(self) -> dict[str, object]:
        return {"page": self.page, "text": self.text, "text_sha256": self.text_sha256}


@dataclass(frozen=True, slots=True)
class NodeSpan:
    page: int
    source_start: int
    source_end: int
    text_sha256: str
    span_ordinal: int
    is_canonical: bool

    @property
    def payload(self) -> dict[str, object]:
        return {
            "page": self.page,
            "is_canonical": self.is_canonical,
            "source_end": self.source_end,
            "source_start": self.source_start,
            "span_ordinal": self.span_ordinal,
            "text_sha256": self.text_sha256,
        }


@dataclass(frozen=True, slots=True)
class StructureNode:
    node_id: str
    contract_revision_id: str
    parent_id: str | None
    node_type: NodeType
    major_class: MajorClass
    raw_heading: str | None
    ordinal: int
    display_text: str
    spans: tuple[NodeSpan, ...] = ()
    table_headers: tuple[str, ...] = ()
    table_cells: tuple[str, ...] = ()
    table_role: TableRole | None = None

    @property
    def identity_payload(self) -> dict[str, object]:
        return {
            "contract_revision_id": self.contract_revision_id,
            "major_class": self.major_class,
            "ordinal": self.ordinal,
            "parent_id": self.parent_id,
            "raw_heading": self.raw_heading,
            "display_text": self.display_text,
            "spans": [span.payload for span in self.spans],
            "table_cells": list(self.table_cells),
            "table_headers": list(self.table_headers),
            "table_role": self.table_role,
            "node_type": self.node_type,
        }

    @property
    def payload(self) -> dict[str, object]:
        return {"node_id": self.node_id, **self.identity_payload}


@dataclass(frozen=True, slots=True)
class NodeLink:
    from_node_id: str
    to_node_id: str
    link_type: LinkType
    ordinal: int

    @property
    def payload(self) -> dict[str, object]:
        return {
            "from_node_id": self.from_node_id,
            "link_type": self.link_type,
            "ordinal": self.ordinal,
            "to_node_id": self.to_node_id,
        }


@dataclass(frozen=True, slots=True)
class StructureCoverage:
    source_characters: int
    covered_characters: int
    source_non_whitespace_characters: int
    covered_non_whitespace_characters: int
    source_sha256: str
    coverage_sha256: str

    @property
    def coverage_percent(self) -> float:
        if self.source_non_whitespace_characters == 0:
            return 100.0
        return 100.0 * (self.covered_non_whitespace_characters / self.source_non_whitespace_characters)


@dataclass(frozen=True, slots=True)
class StructureArtifact:
    schema_version: Literal["cardrag.structure.v2"]
    document_id: str
    issuer: str
    issuer_profile: IssuerParserProfile
    product_code: str
    product_name: str
    source_version: str
    effective_date: str | None
    document_type: str
    product_lineage_id: str
    contract_revision_id: str
    source_id: str
    pdf_sha256: str
    source_sha256: str
    source_non_whitespace_characters: int
    source_coverage_sha256: str
    pages: tuple[StructurePage, ...]
    nodes: tuple[StructureNode, ...]
    links: tuple[NodeLink, ...]

    @property
    def issuer_profile_id(self) -> str:
        return self.issuer_profile.profile_id

    @property
    def issuer_profile_sha256(self) -> str:
        return self.issuer_profile.sha256

    @property
    def spans(self) -> tuple[NodeSpan, ...]:
        """Flatten node spans in node/source order for serving exporters."""

        return tuple(span for node in self.nodes for span in node.spans)

    @property
    def payload(self) -> dict[str, object]:
        return {
            "contract_revision_id": self.contract_revision_id,
            "document_id": self.document_id,
            "document_type": self.document_type,
            "issuer": self.issuer,
            "issuer_profile": self.issuer_profile.payload,
            "issuer_profile_id": self.issuer_profile_id,
            "issuer_profile_sha256": self.issuer_profile_sha256,
            "links": [link.payload for link in self.links],
            "nodes": [node.payload for node in self.nodes],
            "pages": [page.payload for page in self.pages],
            "pdf_sha256": self.pdf_sha256,
            "product_code": self.product_code,
            "product_name": self.product_name,
            "product_lineage_id": self.product_lineage_id,
            "schema_version": self.schema_version,
            "source_coverage_sha256": self.source_coverage_sha256,
            "source_id": self.source_id,
            "source_non_whitespace_characters": self.source_non_whitespace_characters,
            "source_sha256": self.source_sha256,
            "source_version": self.source_version,
            "effective_date": self.effective_date,
        }

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.payload)

    @property
    def artifact_sha256(self) -> str:
        return sha256_bytes(self.canonical_bytes)


@dataclass(frozen=True, slots=True)
class DerivedView:
    view_id: str
    contract_revision_id: str
    node_id: str
    parent_item_id: str | None
    view_type: ViewType
    ordinal: int
    display_text: str
    embedding_input: str
    spans: tuple[NodeSpan, ...]
    context: tuple[str, ...]
    input_sha256: str

    @property
    def identity_payload(self) -> dict[str, object]:
        return {
            "context": list(self.context),
            "contract_revision_id": self.contract_revision_id,
            "display_text": self.display_text,
            "embedding_input": self.embedding_input,
            "input_sha256": self.input_sha256,
            "node_id": self.node_id,
            "ordinal": self.ordinal,
            "parent_item_id": self.parent_item_id,
            "spans": [span.payload for span in self.spans],
            "view_type": self.view_type,
        }

    @property
    def payload(self) -> dict[str, object]:
        return {"view_id": self.view_id, **self.identity_payload}


@dataclass(slots=True)
class _NodeDraft:
    parent_index: int | None
    node_type: NodeType
    major_class: MajorClass
    raw_heading: str | None
    spans: list[NodeSpan] = field(default_factory=list)
    table_headers: tuple[str, ...] = ()
    table_cells: tuple[str, ...] = ()
    table_role: TableRole | None = None


@dataclass(frozen=True, slots=True)
class _LinkDraft:
    from_index: int
    to_index: int
    link_type: LinkType


@dataclass(frozen=True, slots=True)
class _SourceLine:
    page: int
    source_start: int
    source_end: int
    text: str

    @property
    def visible(self) -> str:
        return self.text.rstrip("\r\n")

    @property
    def span(self) -> NodeSpan:
        return NodeSpan(
            page=self.page,
            source_start=self.source_start,
            source_end=self.source_end,
            text_sha256=sha256_bytes(self.text.encode("utf-8")),
            span_ordinal=0,
            is_canonical=True,
        )


def _require_trimmed(value: str, label: str) -> None:
    if not value or value != value.strip():
        raise ValueError(f"{label} must be non-empty and trimmed")


def _require_context_metadata(value: object, label: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be a non-empty trimmed string")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValueError(f"{label} must not contain control characters")


def _require_canonical_effective_date(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("effective_date must be canonical ISO text or null")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError("effective_date must be canonical ISO text or null") from None
    if parsed.isoformat() != value:
        raise ValueError("effective_date must be canonical ISO text or null")


def contextual_item_policy_payload() -> dict[str, object]:
    """Return the immutable-by-construction contextual document input policy."""

    return {
        "effective_date_null": _CONTEXT_EFFECTIVE_DATE_NULL,
        "field_order": list(_CONTEXT_FIELD_ORDER),
        "heading_label": _CONTEXT_HEADING_LABEL,
        "line_format": "{label}: {value}",
        "schema_version": CONTEXTUAL_ITEM_CONTEXT_POLICY,
        "view_types": ["CONTEXTUAL_ITEM", "DETAIL"],
    }


def unclassified_fallback_policy_payload() -> dict[str, object]:
    """Return the sealed lossless fallback policy used after parser failure."""

    return {
        "canonical_leaf_type": "UNCLASSIFIED",
        "contract_scope": "one-contract-revision",
        "container_type": "ITEM",
        "major_class": "UNKNOWN",
        "partition_boundary": "complete-source-line",
        "schema_version": UNCLASSIFIED_FALLBACK_POLICY,
    }


def _source_lines(page: PageRecord) -> tuple[_SourceLine, ...]:
    lines: list[_SourceLine] = []
    offset = 0
    for text in page.text.splitlines(keepends=True):
        end = offset + len(text)
        lines.append(_SourceLine(page.page, offset, end, text))
        offset = end
    if offset < len(page.text):
        text = page.text[offset:]
        lines.append(_SourceLine(page.page, offset, len(page.text), text))
    return tuple(lines)


def _major_class(text: str, profile: IssuerParserProfile) -> MajorClass:
    normalized = text.casefold()
    benefit = any(term.casefold() in normalized for term in profile.benefit_terms)
    notice = any(term.casefold() in normalized for term in profile.notice_terms)
    if benefit and notice:
        return "MIXED"
    if benefit:
        return "BENEFIT"
    if notice:
        return "NOTICE"
    return "UNKNOWN"


def _inherited_class(text: str, profile: IssuerParserProfile, inherited: MajorClass) -> MajorClass:
    classified = _major_class(text, profile)
    return inherited if classified == "UNKNOWN" else classified


def _heading_kind(text: str, profile: IssuerParserProfile) -> Literal["major", "item"] | None:
    stripped = text.strip()
    markdown = _MARKDOWN_HEADING.fullmatch(stripped)
    if markdown is not None:
        return "major" if len(markdown.group(1)) <= 2 else "item"
    if not stripped or len(stripped) > 120 or "|" in stripped:
        return None
    # A short benefit/notice bullet is still a list item, and an explicit
    # footnote marker is still a footnote.  Protect both before the semantic
    # heading heuristic below; otherwise common lines such as
    # ``- 전월 실적 제외`` become false MAJOR_SECTION nodes.
    if _MARKDOWN_BULLET.match(text) or _FOOTNOTE.match(text):
        return None
    if any(stripped.startswith(prefix) for prefix in profile.major_prefixes):
        return "major"
    if any(stripped.startswith(prefix) for prefix in profile.item_prefixes):
        return "item"
    if re.match(r"^(?:혜택|서비스)\s*\d+\s*[.)-]?", stripped):
        return "item"
    if stripped.startswith(("**", "[")) and stripped.endswith(("**", "]")):
        return "item"
    semantic = _major_class(stripped, profile)
    if semantic != "UNKNOWN" and len(stripped) <= 45 and not stripped.endswith(("다.", "요.")):
        return "major"
    return None


def _table_cells(text: str) -> tuple[str, ...]:
    stripped = text.strip().strip("|")
    return tuple(cell.strip() for cell in stripped.split("|"))


def _is_table_line(text: str) -> bool:
    stripped = text.strip()
    return bool(stripped) and stripped.count("|") >= 2


def _is_table_separator(cells: tuple[str, ...]) -> bool:
    return bool(cells) and all(_TABLE_SEPARATOR_CELL.fullmatch(cell) for cell in cells)


def _edge_key(text: str) -> str:
    stripped = " ".join(text.strip().casefold().split())
    if _PAGE_COUNTER.fullmatch(stripped):
        return "<page-counter>"
    return stripped


def _looks_like_boilerplate(text: str, profile: IssuerParserProfile) -> bool:
    normalized = _edge_key(text)
    if not normalized or len(normalized) > 120:
        return False
    if normalized == "<page-counter>" or _PAGE_MARKER.fullmatch(text.strip()):
        return True
    if any(marker.casefold() in normalized for marker in profile.issuer_markers):
        return True
    return "상품설명서" in normalized or "상품 안내" in normalized


def _repeated_edge_lines(
    page_lines: Sequence[tuple[_SourceLine, ...]], profile: IssuerParserProfile
) -> frozenset[tuple[int, int, int]]:
    occurrences: dict[tuple[str, str], list[_SourceLine]] = defaultdict(list)
    page_members: dict[tuple[str, str], set[int]] = defaultdict(set)
    for lines in page_lines:
        non_blank = [line for line in lines if line.visible.strip()]
        header = non_blank[: profile.edge_line_window]
        footer = non_blank[-profile.edge_line_window :]
        for edge, candidates in (("header", header), ("footer", footer)):
            for line in candidates:
                key = _edge_key(line.visible)
                if key and _looks_like_boilerplate(line.visible, profile):
                    occurrences[(edge, key)].append(line)
                    page_members[(edge, key)].add(line.page)
    repeated: set[tuple[int, int, int]] = set()
    for occurrence_key, matched_lines in occurrences.items():
        if len(page_members[occurrence_key]) < 2:
            continue
        repeated.update((line.page, line.source_start, line.source_end) for line in matched_lines)
    return frozenset(repeated)


def _next_table_line_is_separator(
    lines: Sequence[_SourceLine],
    current_index: int,
    boilerplate_lines: frozenset[tuple[int, int, int]],
) -> bool:
    """Return whether a page-leading table row declares its own Markdown header."""

    for candidate in lines[current_index + 1 :]:
        key = (candidate.page, candidate.source_start, candidate.source_end)
        stripped = candidate.visible.strip()
        if not stripped or key in boilerplate_lines or _PAGE_MARKER.fullmatch(stripped):
            continue
        return _is_table_line(candidate.visible) and _is_table_separator(_table_cells(candidate.visible))
    return False


def _source_digest(pages: Sequence[StructurePage]) -> str:
    return canonical_sha256(
        {
            "pages": [{"page": page.page, "text_sha256": page.text_sha256} for page in pages],
            "schema_version": STRUCTURE_SOURCE_SCHEMA,
        }
    )


def _coverage_digest(pages: Sequence[StructurePage]) -> str:
    return canonical_sha256(
        {
            "pages": [
                {
                    "non_whitespace_characters": sum(not char.isspace() for char in page.text),
                    "non_whitespace_sha256": sha256_bytes(
                        "".join(char for char in page.text if not char.isspace()).encode("utf-8")
                    ),
                    "page": page.page,
                    "text_sha256": page.text_sha256,
                }
                for page in pages
            ],
            "schema_version": STRUCTURE_COVERAGE_SCHEMA,
        }
    )


def _span_with_ordinal(span: NodeSpan, ordinal: int, *, is_canonical: bool | None = None) -> NodeSpan:
    return NodeSpan(
        page=span.page,
        source_start=span.source_start,
        source_end=span.source_end,
        text_sha256=span.text_sha256,
        span_ordinal=ordinal,
        is_canonical=span.is_canonical if is_canonical is None else is_canonical,
    )


def _node_id(node: StructureNode) -> str:
    return "node_" + canonical_sha256(node.identity_payload)


def _view_id(view: DerivedView) -> str:
    return "view_" + canonical_sha256(view.identity_payload)


def parse_structure_artifact(
    pages: Sequence[PageRecord],
    *,
    issuer: str,
    product_code: str,
    product_name: str,
    source_version: str,
    effective_date: str | None,
    document_type: str,
    source_id: str,
    pdf_sha256: str,
    profile: IssuerParserProfile | None = None,
) -> StructureArtifact:
    """Parse verified page text into one deterministic ``cardrag.structure.v2`` artifact."""

    if not pages:
        raise ValueError("structure input requires at least one page")
    issuer_code(issuer)
    _require_trimmed(product_code, "product_code")
    _require_context_metadata(product_name, "product_name")
    _require_context_metadata(source_version, "source_version")
    _require_canonical_effective_date(effective_date)
    _require_trimmed(document_type, "document_type")
    if not _SOURCE_ID.fullmatch(source_id):
        raise ValueError("invalid source_id")
    if not _SHA256.fullmatch(pdf_sha256):
        raise ValueError("invalid pdf_sha256")
    document_ids = {page.document_id for page in pages}
    if len(document_ids) != 1 or not next(iter(document_ids)):
        raise ValueError("structure pages must belong to one non-empty document_id")
    page_numbers = [page.page for page in pages]
    if page_numbers != sorted(set(page_numbers)) or any(page < 1 for page in page_numbers):
        raise ValueError("structure pages must be positive, sorted, and unique")

    selected_profile = profile or issuer_parser_profile(issuer)
    if selected_profile.issuer != issuer:
        raise ValueError("issuer parser profile does not match the document issuer")
    lineage_id = make_product_lineage_id(
        issuer=issuer,
        product_code=product_code,
        document_type=document_type,
    )
    revision_id = make_contract_revision_id(
        product_lineage_id=lineage_id,
        source_id=source_id,
        pdf_sha256=pdf_sha256,
    )
    structure_pages = tuple(
        StructurePage(page=page.page, text=page.text, text_sha256=page.text_sha256) for page in pages
    )
    source_lines = tuple(_source_lines(page) for page in pages)
    boilerplate_lines = _repeated_edge_lines(source_lines, selected_profile)

    drafts: list[_NodeDraft] = [
        _NodeDraft(parent_index=None, node_type="ROOT", major_class="UNKNOWN", raw_heading=None)
    ]
    link_drafts: list[_LinkDraft] = []
    leaf_indices: list[int] = []
    current_major: int | None = None
    current_item: int | None = None
    previous_page_last_semantic: int | None = None
    previous_page_last_table: int | None = None
    previous_reference_leaf: int | None = None
    benefit_item_indices: list[int] = []

    def add_node(
        *,
        parent_index: int | None,
        node_type: NodeType,
        major_class: MajorClass,
        raw_heading: str | None = None,
        spans: Sequence[NodeSpan] = (),
        table_headers: tuple[str, ...] = (),
        table_cells: tuple[str, ...] = (),
        table_role: TableRole | None = None,
    ) -> int:
        index = len(drafts)
        drafts.append(
            _NodeDraft(
                parent_index=parent_index,
                node_type=node_type,
                major_class=major_class,
                raw_heading=raw_heading,
                spans=list(spans),
                table_headers=table_headers,
                table_cells=table_cells,
                table_role=table_role,
            )
        )
        if node_type in CANONICAL_LEAF_TYPES:
            leaf_indices.append(index)
        return index

    def ensure_item() -> int:
        nonlocal current_item
        if current_item is None:
            parent = current_major if current_major is not None else 0
            current_item = add_node(
                parent_index=parent,
                node_type="ITEM",
                major_class=drafts[parent].major_class,
            )
            if drafts[current_item].major_class in {"BENEFIT", "MIXED"}:
                benefit_item_indices.append(current_item)
        return current_item

    for lines in source_lines:
        page_first_semantic = True
        page_last_semantic: int | None = None
        page_last_table: int | None = None
        current_table: int | None = None
        for line_index, line in enumerate(lines):
            line_key = (line.page, line.source_start, line.source_end)
            visible = line.visible
            stripped = visible.strip()
            marker = _PAGE_MARKER.fullmatch(stripped)
            if marker is not None and int(marker.group(1)) != line.page:
                raise StructureValidationError("embedded OCR page marker does not match PageRecord")

            if line_key in boilerplate_lines or marker is not None:
                current_table = None
                add_node(
                    parent_index=0,
                    node_type="BOILERPLATE",
                    major_class="UNKNOWN",
                    spans=(line.span,),
                )
                continue

            if not stripped:
                current_table = None
                parent = current_item if current_item is not None else current_major or 0
                add_node(
                    parent_index=parent,
                    node_type="UNCLASSIFIED",
                    major_class=drafts[parent].major_class,
                    spans=(line.span,),
                )
                continue

            if _is_table_line(visible):
                continued_table = False
                if current_table is None:
                    parent = ensure_item()
                    inherited_headers: tuple[str, ...] = ()
                    declares_local_header = page_first_semantic and _next_table_line_is_separator(
                        lines,
                        line_index,
                        boilerplate_lines,
                    )
                    if (
                        page_first_semantic
                        and previous_page_last_table is not None
                        and not declares_local_header
                    ):
                        inherited_headers = drafts[previous_page_last_table].table_headers
                        continued_table = True
                    current_table = add_node(
                        parent_index=parent,
                        node_type="TABLE",
                        major_class=drafts[parent].major_class,
                        table_headers=inherited_headers,
                    )
                    if continued_table and previous_page_last_table is not None:
                        link_drafts.append(
                            _LinkDraft(current_table, previous_page_last_table, "CONTINUATION_OF")
                        )
                table = drafts[current_table]
                table.spans.append(
                    _span_with_ordinal(
                        line.span,
                        len(table.spans),
                        is_canonical=False,
                    )
                )
                cells = _table_cells(visible)
                separator = _is_table_separator(cells)
                if not table.table_headers and not separator:
                    table.table_headers = cells
                    role: TableRole = "HEADER"
                elif separator:
                    role = "SEPARATOR"
                elif cells == table.table_headers:
                    role = "HEADER"
                else:
                    role = "BODY"
                row = add_node(
                    parent_index=current_table,
                    node_type="TABLE_ROW",
                    major_class=table.major_class,
                    spans=(line.span,),
                    table_headers=table.table_headers,
                    table_cells=cells,
                    table_role=role,
                )
                if page_first_semantic and continued_table and previous_page_last_semantic is not None:
                    link_drafts.append(_LinkDraft(row, previous_page_last_semantic, "CONTINUATION_OF"))
                page_first_semantic = False
                page_last_semantic = row
                page_last_table = current_table
                previous_reference_leaf = row
                continue

            current_table = None
            heading_kind = _heading_kind(visible, selected_profile)
            own_class = _major_class(visible, selected_profile)
            is_heading = heading_kind is not None
            if heading_kind == "major":
                current_major = add_node(
                    parent_index=0,
                    node_type="MAJOR_SECTION",
                    major_class=own_class,
                    raw_heading=visible,
                    spans=(_span_with_ordinal(line.span, 0, is_canonical=False),),
                )
                current_item = None
                if own_class in {"NOTICE", "MIXED"}:
                    link_drafts.extend(
                        _LinkDraft(current_major, benefit_item, "APPLIES_TO")
                        for benefit_item in benefit_item_indices
                    )
                parent = current_major
                node_type: NodeType = "PARAGRAPH"
                node_class = own_class
            elif heading_kind == "item":
                parent_for_item = current_major if current_major is not None else 0
                item_class = _inherited_class(
                    visible,
                    selected_profile,
                    drafts[parent_for_item].major_class,
                )
                current_item = add_node(
                    parent_index=parent_for_item,
                    node_type="ITEM",
                    major_class=item_class,
                    raw_heading=visible,
                    spans=(_span_with_ordinal(line.span, 0, is_canonical=False),),
                )
                prior_benefit_item = benefit_item_indices[-1] if benefit_item_indices else None
                if (
                    item_class in {"NOTICE", "MIXED"}
                    and drafts[parent_for_item].major_class not in {"NOTICE", "MIXED"}
                    and prior_benefit_item is not None
                ):
                    link_drafts.append(_LinkDraft(current_item, prior_benefit_item, "APPLIES_TO"))
                if item_class in {"BENEFIT", "MIXED"}:
                    benefit_item_indices.append(current_item)
                parent = current_item
                node_type = "PARAGRAPH"
                node_class = item_class
            else:
                parent = ensure_item()
                inherited = drafts[parent].major_class
                node_class = _inherited_class(visible, selected_profile, inherited)
                if _FOOTNOTE.match(visible):
                    node_type = "FOOTNOTE"
                elif _LIST_ITEM.match(visible):
                    node_type = "LIST_ITEM"
                elif inherited != "UNKNOWN" or own_class != "UNKNOWN":
                    node_type = "PARAGRAPH"
                else:
                    node_type = "UNCLASSIFIED"

            leaf = add_node(
                parent_index=parent,
                node_type=node_type,
                major_class=node_class,
                spans=(line.span,),
            )
            if page_first_semantic and previous_page_last_semantic is not None and not is_heading:
                link_drafts.append(_LinkDraft(leaf, previous_page_last_semantic, "CONTINUATION_OF"))
            if node_type == "FOOTNOTE" and previous_reference_leaf is not None:
                link_drafts.extend(
                    (
                        _LinkDraft(leaf, previous_reference_leaf, "FOOTNOTE_OF"),
                        _LinkDraft(leaf, previous_reference_leaf, "APPLIES_TO"),
                    )
                )
            elif (
                not is_heading
                and node_class in {"NOTICE", "MIXED"}
                and previous_reference_leaf is not None
                and drafts[previous_reference_leaf].major_class in {"BENEFIT", "MIXED"}
            ):
                link_drafts.append(_LinkDraft(leaf, previous_reference_leaf, "APPLIES_TO"))
            page_first_semantic = False
            page_last_semantic = leaf
            page_last_table = None
            if node_type != "FOOTNOTE":
                previous_reference_leaf = leaf

        if page_last_semantic is not None:
            previous_page_last_semantic = page_last_semantic
            previous_page_last_table = page_last_table

    for previous, current in zip(leaf_indices, leaf_indices[1:], strict=False):
        link_drafts.extend(
            (
                _LinkDraft(previous, current, "NEXT"),
                _LinkDraft(current, previous, "PREVIOUS"),
            )
        )

    source_text_by_page = {page.page: page.text for page in structure_pages}
    nodes: list[StructureNode] = []
    for ordinal, draft in enumerate(drafts):
        parent_id = None if draft.parent_index is None else nodes[draft.parent_index].node_id
        display_text = "".join(
            source_text_by_page[span.page][span.source_start : span.source_end] for span in draft.spans
        )
        provisional = StructureNode(
            node_id="",
            contract_revision_id=revision_id,
            parent_id=parent_id,
            node_type=draft.node_type,
            major_class=draft.major_class,
            raw_heading=draft.raw_heading,
            ordinal=ordinal,
            display_text=display_text,
            spans=tuple(draft.spans),
            table_headers=draft.table_headers,
            table_cells=draft.table_cells,
            table_role=draft.table_role,
        )
        nodes.append(
            StructureNode(
                node_id=_node_id(provisional),
                contract_revision_id=provisional.contract_revision_id,
                parent_id=provisional.parent_id,
                node_type=provisional.node_type,
                major_class=provisional.major_class,
                raw_heading=provisional.raw_heading,
                ordinal=provisional.ordinal,
                display_text=provisional.display_text,
                spans=provisional.spans,
                table_headers=provisional.table_headers,
                table_cells=provisional.table_cells,
                table_role=provisional.table_role,
            )
        )

    link_order = {"CONTINUATION_OF": 0, "FOOTNOTE_OF": 1, "APPLIES_TO": 2, "PREVIOUS": 3, "NEXT": 4}
    unique_link_drafts = {(link.from_index, link.to_index, link.link_type): link for link in link_drafts}
    ordered_link_drafts = sorted(
        unique_link_drafts.values(),
        key=lambda link: (
            nodes[link.from_index].ordinal,
            link_order[link.link_type],
            nodes[link.to_index].ordinal,
        ),
    )
    links = tuple(
        NodeLink(
            from_node_id=nodes[link.from_index].node_id,
            to_node_id=nodes[link.to_index].node_id,
            link_type=link.link_type,
            ordinal=ordinal,
        )
        for ordinal, link in enumerate(ordered_link_drafts)
    )
    source_sha256 = _source_digest(structure_pages)
    coverage_sha256 = _coverage_digest(structure_pages)
    artifact = StructureArtifact(
        schema_version=STRUCTURE_SCHEMA,
        document_id=next(iter(document_ids)),
        issuer=issuer,
        issuer_profile=selected_profile,
        product_code=product_code,
        product_name=product_name,
        source_version=source_version,
        effective_date=effective_date,
        document_type=document_type,
        product_lineage_id=lineage_id,
        contract_revision_id=revision_id,
        source_id=source_id,
        pdf_sha256=pdf_sha256,
        source_sha256=source_sha256,
        source_non_whitespace_characters=sum(
            not char.isspace() for page in structure_pages for char in page.text
        ),
        source_coverage_sha256=coverage_sha256,
        pages=structure_pages,
        nodes=tuple(nodes),
        links=links,
    )
    validate_structure_artifact(artifact)
    return artifact


build_structure_artifact = parse_structure_artifact


def build_unclassified_fallback_artifact(
    pages: Sequence[PageRecord],
    *,
    issuer: str,
    product_code: str,
    product_name: str,
    source_version: str,
    effective_date: str | None,
    document_type: str,
    source_id: str,
    pdf_sha256: str,
    profile: IssuerParserProfile | None = None,
) -> StructureArtifact:
    """Build one lossless, line-bounded fallback without interpreting OCR text.

    The neutral ``ITEM`` container exists only so the ordinary derived-view
    policy can partition an arbitrarily long contract at canonical leaf
    boundaries. Every source character belongs to exactly one
    ``UNCLASSIFIED`` leaf, and no metadata is inserted into source spans or
    display text.
    """

    if not pages:
        raise ValueError("structure input requires at least one page")
    issuer_code(issuer)
    _require_trimmed(product_code, "product_code")
    _require_context_metadata(product_name, "product_name")
    _require_context_metadata(source_version, "source_version")
    _require_canonical_effective_date(effective_date)
    _require_trimmed(document_type, "document_type")
    if not _SOURCE_ID.fullmatch(source_id):
        raise ValueError("invalid source_id")
    if not _SHA256.fullmatch(pdf_sha256):
        raise ValueError("invalid pdf_sha256")
    document_ids = {page.document_id for page in pages}
    if len(document_ids) != 1 or not next(iter(document_ids)):
        raise ValueError("structure pages must belong to one non-empty document_id")
    page_numbers = [page.page for page in pages]
    if page_numbers != sorted(set(page_numbers)) or any(page < 1 for page in page_numbers):
        raise ValueError("structure pages must be positive, sorted, and unique")

    selected_profile = profile or issuer_parser_profile(issuer)
    if selected_profile.issuer != issuer:
        raise ValueError("issuer parser profile does not match the document issuer")
    lineage_id = make_product_lineage_id(
        issuer=issuer,
        product_code=product_code,
        document_type=document_type,
    )
    revision_id = make_contract_revision_id(
        product_lineage_id=lineage_id,
        source_id=source_id,
        pdf_sha256=pdf_sha256,
    )
    structure_pages = tuple(
        StructurePage(page=page.page, text=page.text, text_sha256=page.text_sha256) for page in pages
    )

    nodes: list[StructureNode] = []

    def append_node(
        *,
        parent_id: str | None,
        node_type: NodeType,
        spans: Sequence[NodeSpan] = (),
    ) -> StructureNode:
        display_text = "".join(
            structure_page.text[span.source_start : span.source_end]
            for span in spans
            for structure_page in structure_pages
            if structure_page.page == span.page
        )
        provisional = StructureNode(
            node_id="",
            contract_revision_id=revision_id,
            parent_id=parent_id,
            node_type=node_type,
            major_class="UNKNOWN",
            raw_heading=None,
            ordinal=len(nodes),
            display_text=display_text,
            spans=tuple(spans),
        )
        node = StructureNode(
            node_id=_node_id(provisional),
            contract_revision_id=provisional.contract_revision_id,
            parent_id=provisional.parent_id,
            node_type=provisional.node_type,
            major_class=provisional.major_class,
            raw_heading=provisional.raw_heading,
            ordinal=provisional.ordinal,
            display_text=provisional.display_text,
            spans=provisional.spans,
        )
        nodes.append(node)
        return node

    root = append_node(parent_id=None, node_type="ROOT")
    item = append_node(parent_id=root.node_id, node_type="ITEM")
    leaves = [
        append_node(parent_id=item.node_id, node_type="UNCLASSIFIED", spans=(line.span,))
        for page in pages
        for line in _source_lines(page)
    ]
    links_list: list[NodeLink] = []
    for previous, current in zip(leaves, leaves[1:], strict=False):
        links_list.extend(
            (
                NodeLink(
                    from_node_id=previous.node_id,
                    to_node_id=current.node_id,
                    link_type="NEXT",
                    ordinal=len(links_list),
                ),
                NodeLink(
                    from_node_id=current.node_id,
                    to_node_id=previous.node_id,
                    link_type="PREVIOUS",
                    ordinal=len(links_list) + 1,
                ),
            )
        )
    links = tuple(links_list)
    artifact = StructureArtifact(
        schema_version=STRUCTURE_SCHEMA,
        document_id=next(iter(document_ids)),
        issuer=issuer,
        issuer_profile=selected_profile,
        product_code=product_code,
        product_name=product_name,
        source_version=source_version,
        effective_date=effective_date,
        document_type=document_type,
        product_lineage_id=lineage_id,
        contract_revision_id=revision_id,
        source_id=source_id,
        pdf_sha256=pdf_sha256,
        source_sha256=_source_digest(structure_pages),
        source_non_whitespace_characters=sum(
            not character.isspace() for page in structure_pages for character in page.text
        ),
        source_coverage_sha256=_coverage_digest(structure_pages),
        pages=structure_pages,
        nodes=tuple(nodes),
        links=links,
    )
    validate_structure_artifact(artifact)
    return artifact


def _line_boundary_sets(text: str) -> tuple[frozenset[int], frozenset[int]]:
    starts = {0}
    ends = {0}
    offset = 0
    for line in text.splitlines(keepends=True):
        starts.add(offset)
        offset += len(line)
        ends.add(offset)
        starts.add(offset)
    ends.add(len(text))
    return frozenset(starts), frozenset(ends)


def _validate_span(
    span: NodeSpan,
    pages_by_number: dict[int, StructurePage],
    boundaries: dict[int, tuple[frozenset[int], frozenset[int]]],
) -> str:
    page = pages_by_number.get(span.page)
    if page is None:
        raise StructureValidationError("node span references a missing source page")
    if not 0 <= span.source_start < span.source_end <= len(page.text):
        raise StructureValidationError("node span offsets exceed the source page")
    starts, ends = boundaries[span.page]
    if span.source_start not in starts or span.source_end not in ends:
        raise StructureValidationError("node span splits an OCR line at an arbitrary boundary")
    text = page.text[span.source_start : span.source_end]
    if sha256_bytes(text.encode("utf-8")) != span.text_sha256:
        raise StructureValidationError("node span hash does not match the exact source text")
    return text


def validate_structure_artifact(artifact: StructureArtifact) -> StructureCoverage:
    """Fail closed unless structure, links, hashes, and lossless coverage all agree."""

    if artifact.schema_version != STRUCTURE_SCHEMA:
        raise StructureValidationError("unsupported structure artifact schema")
    issuer_code(artifact.issuer)
    try:
        _require_context_metadata(artifact.product_name, "product_name")
        _require_context_metadata(artifact.source_version, "source_version")
        _require_canonical_effective_date(artifact.effective_date)
    except ValueError as exc:
        raise StructureValidationError(str(exc)) from None
    if artifact.issuer_profile.issuer != artifact.issuer:
        raise StructureValidationError("issuer profile crosses the document issuer")
    expected_lineage = make_product_lineage_id(
        issuer=artifact.issuer,
        product_code=artifact.product_code,
        document_type=artifact.document_type,
    )
    if artifact.product_lineage_id != expected_lineage:
        raise StructureValidationError("product lineage identity does not match canonical input")
    expected_revision = make_contract_revision_id(
        product_lineage_id=artifact.product_lineage_id,
        source_id=artifact.source_id,
        pdf_sha256=artifact.pdf_sha256,
    )
    if artifact.contract_revision_id != expected_revision:
        raise StructureValidationError("contract revision identity does not match canonical input")
    if not artifact.pages:
        raise StructureValidationError("structure artifact has no source pages")
    page_numbers = [page.page for page in artifact.pages]
    if page_numbers != sorted(set(page_numbers)) or any(page < 1 for page in page_numbers):
        raise StructureValidationError("source pages are not positive, sorted, and unique")
    for page in artifact.pages:
        if sha256_bytes(page.text.encode("utf-8")) != page.text_sha256:
            raise StructureValidationError("source page hash does not match its OCR text")
    if artifact.source_sha256 != _source_digest(artifact.pages):
        raise StructureValidationError("structure source hash does not match its pages")
    if artifact.source_coverage_sha256 != _coverage_digest(artifact.pages):
        raise StructureValidationError("structure coverage hash does not match its pages")
    source_non_whitespace = sum(not char.isspace() for page in artifact.pages for char in page.text)
    if artifact.source_non_whitespace_characters != source_non_whitespace:
        raise StructureValidationError("structure source character count is inconsistent")

    if not artifact.nodes:
        raise StructureValidationError("structure artifact has no ROOT node")
    identifiers = [node.node_id for node in artifact.nodes]
    if len(identifiers) != len(set(identifiers)):
        raise StructureValidationError("structure node ids are not unique")
    nodes_by_id = {node.node_id: node for node in artifact.nodes}
    roots = [node for node in artifact.nodes if node.node_type == "ROOT"]
    if len(roots) != 1 or roots[0].parent_id is not None:
        raise StructureValidationError("structure artifact requires exactly one parentless ROOT")
    if roots[0].ordinal != 0:
        raise StructureValidationError("ROOT must be the first structure node")
    pages_by_number = {page.page: page for page in artifact.pages}
    boundaries = {page.page: _line_boundary_sets(page.text) for page in artifact.pages}
    children: dict[str, list[StructureNode]] = defaultdict(list)
    for expected_ordinal, node in enumerate(artifact.nodes):
        if node.ordinal != expected_ordinal:
            raise StructureValidationError("structure node ordinals are not canonical")
        if node.contract_revision_id != artifact.contract_revision_id:
            raise StructureValidationError("structure node crosses its contract revision")
        if node.node_type not in CANONICAL_LEAF_TYPES | _CONTAINER_TYPES:
            raise StructureValidationError("structure node has an unsupported node_type")
        if node.major_class not in {"BENEFIT", "NOTICE", "MIXED", "UNKNOWN"}:
            raise StructureValidationError("structure node has an unsupported major_class")
        if node.node_type != "ROOT":
            if node.parent_id is None or node.parent_id not in nodes_by_id:
                raise StructureValidationError("structure node parent is missing")
            parent = nodes_by_id[node.parent_id]
            if parent.contract_revision_id != node.contract_revision_id:
                raise StructureValidationError("structure parent crosses a contract revision")
            if parent.ordinal >= node.ordinal:
                raise StructureValidationError("structure parent must precede its child")
            children[parent.node_id].append(node)
        if [span.span_ordinal for span in node.spans] != list(range(len(node.spans))):
            raise StructureValidationError("node span ordinals are not canonical")
        positions = [(span.page, span.source_start, span.source_end) for span in node.spans]
        if positions != sorted(positions):
            raise StructureValidationError("node spans do not follow source page order")
        for span in node.spans:
            _validate_span(span, pages_by_number, boundaries)
        exact_display = "".join(
            pages_by_number[span.page].text[span.source_start : span.source_end] for span in node.spans
        )
        if node.display_text != exact_display:
            raise StructureValidationError("structure node display_text is not its exact source spans")
        if node.node_type in CANONICAL_LEAF_TYPES and any(not span.is_canonical for span in node.spans):
            raise StructureValidationError("canonical leaf contains a non-canonical source span")
        if node.node_type in _CONTAINER_TYPES and any(span.is_canonical for span in node.spans):
            raise StructureValidationError("container span cannot claim canonical source coverage")
        if node.node_type == "TABLE_ROW":
            if node.parent_id is None or nodes_by_id[node.parent_id].node_type != "TABLE":
                raise StructureValidationError("TABLE_ROW must have a TABLE parent")
            table = nodes_by_id[node.parent_id]
            if node.table_headers != table.table_headers:
                raise StructureValidationError("table row lost its original header relationship")
            if node.table_role is None or not node.table_cells:
                raise StructureValidationError("table row lacks role or original cells")
        elif node.node_type == "TABLE":
            if node.table_cells or node.table_role is not None:
                raise StructureValidationError("TABLE container cannot masquerade as a row")
        elif node.table_headers or node.table_cells or node.table_role is not None:
            raise StructureValidationError("non-table node contains table metadata")
    for table in (node for node in artifact.nodes if node.node_type == "TABLE"):
        rows = children.get(table.node_id, [])
        if not rows or any(row.node_type != "TABLE_ROW" for row in rows):
            raise StructureValidationError("TABLE must contain only one or more TABLE_ROW children")

    for expected_ordinal, link in enumerate(artifact.links):
        if link.ordinal != expected_ordinal:
            raise StructureValidationError("node link ordinals are not canonical")
        source = nodes_by_id.get(link.from_node_id)
        target = nodes_by_id.get(link.to_node_id)
        if source is None or target is None:
            raise StructureValidationError("node link references a node outside this artifact")
        if (
            source.contract_revision_id != artifact.contract_revision_id
            or target.contract_revision_id != artifact.contract_revision_id
        ):
            raise StructureValidationError("node link crosses a contract revision")
        if source.node_id == target.node_id:
            raise StructureValidationError("node link cannot target itself")

    coverage: dict[int, list[bool]] = {page.page: [False] * len(page.text) for page in artifact.pages}
    covered_non_whitespace = 0
    covered_characters = 0
    leaf_spans: dict[int, list[NodeSpan]] = defaultdict(list)
    for node in artifact.nodes:
        if node.node_type not in CANONICAL_LEAF_TYPES:
            continue
        if not node.spans:
            raise StructureValidationError("canonical leaf has no exact source span")
        for span in node.spans:
            page = pages_by_number[span.page]
            for offset in range(span.source_start, span.source_end):
                if coverage[span.page][offset]:
                    raise StructureValidationError("canonical leaf spans overlap")
                coverage[span.page][offset] = True
                covered_characters += 1
                if not page.text[offset].isspace():
                    covered_non_whitespace += 1
            leaf_spans[span.page].append(span)
    for page in artifact.pages:
        if any(not covered for covered in coverage[page.page]):
            raise StructureValidationError("canonical leaves do not losslessly reconstruct the OCR page")
        ordered = sorted(leaf_spans[page.page], key=lambda span: span.source_start)
        reconstructed = "".join(page.text[span.source_start : span.source_end] for span in ordered)
        if reconstructed != page.text:
            raise StructureValidationError("canonical leaf reconstruction differs from OCR source")
    if covered_non_whitespace != source_non_whitespace:
        raise StructureValidationError("non-whitespace OCR source coverage is not 100 percent")
    for node in artifact.nodes:
        provisional = StructureNode(
            node_id="",
            contract_revision_id=node.contract_revision_id,
            parent_id=node.parent_id,
            node_type=node.node_type,
            major_class=node.major_class,
            raw_heading=node.raw_heading,
            ordinal=node.ordinal,
            display_text=node.display_text,
            spans=node.spans,
            table_headers=node.table_headers,
            table_cells=node.table_cells,
            table_role=node.table_role,
        )
        if node.node_id != _node_id(provisional):
            raise StructureValidationError("structure node id does not match its canonical payload")
    return StructureCoverage(
        source_characters=sum(len(page.text) for page in artifact.pages),
        covered_characters=covered_characters,
        source_non_whitespace_characters=source_non_whitespace,
        covered_non_whitespace_characters=covered_non_whitespace,
        source_sha256=artifact.source_sha256,
        coverage_sha256=artifact.source_coverage_sha256,
    )


def _ordered_unique_spans(spans: Iterable[NodeSpan]) -> tuple[NodeSpan, ...]:
    unique = {(span.page, span.source_start, span.source_end, span.text_sha256): span for span in spans}
    return tuple(
        _span_with_ordinal(span, ordinal)
        for ordinal, span in enumerate(
            sorted(unique.values(), key=lambda span: (span.page, span.source_start, span.source_end))
        )
    )


def _span_text(artifact: StructureArtifact, spans: Sequence[NodeSpan]) -> str:
    pages = {page.page: page for page in artifact.pages}
    return "".join(pages[span.page].text[span.source_start : span.source_end] for span in spans)


def _within_limits(
    text: str,
    *,
    maximum_chars: int,
    maximum_tokens: int | None,
    token_counter: Callable[[str], int] | None,
) -> bool:
    if len(text) > maximum_chars:
        return False
    if maximum_tokens is None:
        return True
    if token_counter is None:
        raise ValueError("maximum_tokens requires an exact token_counter")
    count = token_counter(text)
    if count < 0:
        raise ValueError("token_counter returned a negative count")
    return count <= maximum_tokens


def _embedding_input(
    view_type: ViewType,
    display_text: str,
    context: Sequence[str],
) -> str:
    if view_type not in {"CONTEXTUAL_ITEM", "DETAIL"} or not context:
        return display_text
    return "\n".join((*context, display_text))


def _context_line(label: str, value: str) -> str:
    return f"{label}: {value}"


def _canonical_item_context(
    artifact: StructureArtifact,
    item: StructureNode,
    nodes_by_id: dict[str, StructureNode],
) -> tuple[str, ...]:
    effective_date = artifact.effective_date or _CONTEXT_EFFECTIVE_DATE_NULL
    values = (
        artifact.issuer,
        artifact.product_name,
        artifact.product_code,
        artifact.source_version,
        effective_date,
        artifact.contract_revision_id,
        item.major_class,
    )
    context = tuple(
        _context_line(label, value) for label, value in zip(_CONTEXT_FIELD_ORDER, values, strict=True)
    )
    headings = tuple(
        _context_line(_CONTEXT_HEADING_LABEL, node.raw_heading)
        for node in _ancestor_headings(item, nodes_by_id)
        if node.raw_heading is not None
    )
    return (*context, *headings)


def _partition_spans(
    artifact: StructureArtifact,
    span_units: Sequence[tuple[NodeSpan, ...]],
    *,
    maximum_chars: int,
    maximum_tokens: int | None,
    token_counter: Callable[[str], int] | None,
    context: Sequence[str] = (),
) -> tuple[tuple[NodeSpan, ...], ...]:
    groups: list[tuple[NodeSpan, ...]] = []
    pending: list[NodeSpan] = []
    for unit in span_units:
        normalized_unit = _ordered_unique_spans(unit)
        unit_text = _embedding_input("DETAIL", _span_text(artifact, normalized_unit), context)
        if not _within_limits(
            unit_text,
            maximum_chars=maximum_chars,
            maximum_tokens=maximum_tokens,
            token_counter=token_counter,
        ):
            raise StructureValidationError(
                "one structural leaf exceeds the view limit; automatic truncation is forbidden"
            )
        candidate = _ordered_unique_spans((*pending, *normalized_unit))
        candidate_text = _embedding_input("DETAIL", _span_text(artifact, candidate), context)
        if pending and not _within_limits(
            candidate_text,
            maximum_chars=maximum_chars,
            maximum_tokens=maximum_tokens,
            token_counter=token_counter,
        ):
            groups.append(_ordered_unique_spans(pending))
            pending = list(normalized_unit)
        else:
            pending.extend(normalized_unit)
    if pending:
        groups.append(_ordered_unique_spans(pending))
    return tuple(groups)


def build_derived_views(
    artifact: StructureArtifact,
    *,
    maximum_chars: int,
    maximum_tokens: int | None = None,
    token_counter: Callable[[str], int] | None = None,
) -> tuple[DerivedView, ...]:
    """Build raw-only views, splitting exclusively at canonical leaf boundaries."""

    if maximum_chars < 1 or maximum_tokens is not None and maximum_tokens < 1:
        raise ValueError("derived view limits must be positive")
    if maximum_tokens is not None and token_counter is None:
        raise ValueError("maximum_tokens requires an exact token_counter")
    validate_structure_artifact(artifact)
    nodes_by_id = {node.node_id: node for node in artifact.nodes}
    children: dict[str, list[StructureNode]] = defaultdict(list)
    for node in artifact.nodes:
        if node.parent_id is not None:
            children[node.parent_id].append(node)

    def descendants(node: StructureNode) -> tuple[StructureNode, ...]:
        result: list[StructureNode] = []
        stack = list(reversed(children.get(node.node_id, [])))
        while stack:
            child = stack.pop()
            result.append(child)
            stack.extend(reversed(children.get(child.node_id, [])))
        return tuple(result)

    def leaf_nodes(node: StructureNode) -> tuple[StructureNode, ...]:
        return tuple(child for child in descendants(node) if child.node_type in CANONICAL_LEAF_TYPES)

    def heading_spans(node: StructureNode) -> tuple[NodeSpan, ...]:
        headings: list[NodeSpan] = []
        lineage: list[StructureNode] = []
        current: StructureNode | None = node
        while current is not None:
            if current.raw_heading is not None:
                lineage.append(current)
            current = nodes_by_id.get(current.parent_id) if current.parent_id is not None else None
        for heading in reversed(lineage):
            headings.extend(heading.spans)
        return _ordered_unique_spans(headings)

    views: list[DerivedView] = []

    def append_view(
        *,
        node: StructureNode,
        view_type: ViewType,
        spans: Sequence[NodeSpan],
        context: Sequence[str] = (),
        parent_item_id: str | None = None,
    ) -> None:
        normalized_spans = _ordered_unique_spans(spans)
        display_text = _span_text(artifact, normalized_spans)
        if not display_text:
            return
        embedding_input = _embedding_input(view_type, display_text, context)
        if not _within_limits(
            embedding_input,
            maximum_chars=maximum_chars,
            maximum_tokens=maximum_tokens,
            token_counter=token_counter,
        ):
            raise StructureValidationError(
                f"{view_type} exceeds its sealed limit; automatic truncation is forbidden"
            )
        ordinal = len(views)
        provisional = DerivedView(
            view_id="",
            contract_revision_id=artifact.contract_revision_id,
            node_id=node.node_id,
            parent_item_id=parent_item_id,
            view_type=view_type,
            ordinal=ordinal,
            display_text=display_text,
            embedding_input=embedding_input,
            spans=normalized_spans,
            context=tuple(context),
            input_sha256=sha256_bytes(embedding_input.encode("utf-8")),
        )
        views.append(
            DerivedView(
                view_id=_view_id(provisional),
                contract_revision_id=provisional.contract_revision_id,
                node_id=provisional.node_id,
                parent_item_id=provisional.parent_item_id,
                view_type=provisional.view_type,
                ordinal=provisional.ordinal,
                display_text=provisional.display_text,
                embedding_input=provisional.embedding_input,
                spans=provisional.spans,
                context=provisional.context,
                input_sha256=provisional.input_sha256,
            )
        )

    title_nodes = [
        node
        for node in artifact.nodes
        if node.node_type in {"MAJOR_SECTION", "ITEM"} and node.raw_heading is not None
    ]
    for node in title_nodes:
        spans = heading_spans(node)
        text = _span_text(artifact, spans)
        if not _within_limits(
            text,
            maximum_chars=maximum_chars,
            maximum_tokens=maximum_tokens,
            token_counter=token_counter,
        ):
            raise StructureValidationError("TITLE exceeds its sealed limit; headings cannot be truncated")
        append_view(node=node, view_type="TITLE", spans=spans)

    for item in (node for node in artifact.nodes if node.node_type == "ITEM"):
        leaves = leaf_nodes(item)
        raw_spans = _ordered_unique_spans(span for leaf in leaves for span in leaf.spans)
        if not raw_spans:
            continue
        raw_text = _span_text(artifact, raw_spans)
        context = _canonical_item_context(artifact, item, nodes_by_id)
        contextual_spans = _ordered_unique_spans((*heading_spans(item), *raw_spans))
        raw_fits = _within_limits(
            raw_text,
            maximum_chars=maximum_chars,
            maximum_tokens=maximum_tokens,
            token_counter=token_counter,
        )
        contextual_text = _embedding_input(
            "CONTEXTUAL_ITEM",
            _span_text(artifact, contextual_spans),
            context,
        )
        contextual_fits = _within_limits(
            contextual_text,
            maximum_chars=maximum_chars,
            maximum_tokens=maximum_tokens,
            token_counter=token_counter,
        )
        if raw_fits:
            append_view(node=item, view_type="RAW_ITEM", spans=raw_spans)
        if contextual_fits:
            append_view(
                node=item,
                view_type="CONTEXTUAL_ITEM",
                spans=contextual_spans,
                context=context,
            )
        if not raw_fits or not contextual_fits:
            groups = _partition_spans(
                artifact,
                tuple(tuple(leaf.spans) for leaf in leaves),
                maximum_chars=maximum_chars,
                maximum_tokens=maximum_tokens,
                token_counter=token_counter,
                context=context,
            )
            for group in groups:
                append_view(
                    node=item,
                    parent_item_id=item.node_id,
                    view_type="DETAIL",
                    spans=group,
                    context=context,
                )

    for section in (
        node
        for node in artifact.nodes
        if node.node_type == "MAJOR_SECTION" and node.major_class in {"BENEFIT", "NOTICE"}
    ):
        spans = _ordered_unique_spans(span for leaf in leaf_nodes(section) for span in leaf.spans)
        text = _span_text(artifact, spans)
        if spans and _within_limits(
            text,
            maximum_chars=maximum_chars,
            maximum_tokens=maximum_tokens,
            token_counter=token_counter,
        ):
            append_view(node=section, view_type="MAJOR_SECTION", spans=spans)

    root = next(node for node in artifact.nodes if node.node_type == "ROOT")
    contract_spans = _ordered_unique_spans(
        span for node in artifact.nodes if node.node_type in CANONICAL_LEAF_TYPES for span in node.spans
    )
    contract_text = _span_text(artifact, contract_spans)
    if _within_limits(
        contract_text,
        maximum_chars=maximum_chars,
        maximum_tokens=maximum_tokens,
        token_counter=token_counter,
    ):
        append_view(node=root, view_type="CONTRACT", spans=contract_spans)

    result = tuple(views)
    validate_derived_views(
        artifact,
        result,
        maximum_chars=maximum_chars,
        maximum_tokens=maximum_tokens,
        token_counter=token_counter,
    )
    return result


def _ancestor_headings(
    node: StructureNode, nodes_by_id: dict[str, StructureNode]
) -> tuple[StructureNode, ...]:
    headings: list[StructureNode] = []
    current: StructureNode | None = node
    while current is not None:
        if current.raw_heading is not None:
            headings.append(current)
        current = nodes_by_id.get(current.parent_id) if current.parent_id is not None else None
    return tuple(reversed(headings))


def validate_derived_views(
    artifact: StructureArtifact,
    views: Sequence[DerivedView],
    *,
    maximum_chars: int,
    maximum_tokens: int | None = None,
    token_counter: Callable[[str], int] | None = None,
) -> None:
    """Verify that views quote only exact spans from one contract and were never truncated."""

    validate_structure_artifact(artifact)
    nodes = {node.node_id: node for node in artifact.nodes}
    pages = {page.page: page for page in artifact.pages}
    boundaries = {page.page: _line_boundary_sets(page.text) for page in artifact.pages}
    identifiers = [view.view_id for view in views]
    if len(identifiers) != len(set(identifiers)):
        raise StructureValidationError("derived view ids are not unique")
    for expected_ordinal, view in enumerate(views):
        if view.ordinal != expected_ordinal:
            raise StructureValidationError("derived view ordinals are not canonical")
        node = nodes.get(view.node_id)
        if node is None:
            raise StructureValidationError("derived view references a node outside its artifact")
        if (
            view.contract_revision_id != artifact.contract_revision_id
            or node.contract_revision_id != view.contract_revision_id
        ):
            raise StructureValidationError("derived view crosses a contract revision")
        if view.view_type in {"CONTEXTUAL_ITEM", "DETAIL"}:
            if node.node_type != "ITEM":
                raise StructureValidationError("contextual derived view must reference an ITEM")
            expected_context = _canonical_item_context(artifact, node, nodes)
            if view.context != expected_context:
                raise StructureValidationError(
                    "derived contextual metadata does not match its structure artifact"
                )
        elif view.context:
            raise StructureValidationError("non-contextual derived view contains metadata context")
        if view.parent_item_id is not None:
            parent_item = nodes.get(view.parent_item_id)
            if parent_item is None or parent_item.node_type != "ITEM":
                raise StructureValidationError("DETAIL view lost its parent ITEM")
            if parent_item.contract_revision_id != view.contract_revision_id:
                raise StructureValidationError("derived parent item crosses a contract revision")
        if view.view_type == "DETAIL" and view.parent_item_id != view.node_id:
            raise StructureValidationError("DETAIL view lost its canonical parent ITEM")
        if view.view_type != "DETAIL" and view.parent_item_id is not None:
            raise StructureValidationError("non-DETAIL view cannot reference a parent ITEM")
        if not view.spans:
            raise StructureValidationError("derived view has no source spans")
        for span in view.spans:
            _validate_span(span, pages, boundaries)
        source_text = _span_text(artifact, view.spans)
        if source_text != view.display_text:
            raise StructureValidationError("derived display_text is not the exact source spans")
        expected_embedding_input = _embedding_input(
            view.view_type,
            view.display_text,
            view.context,
        )
        if view.embedding_input != expected_embedding_input:
            raise StructureValidationError("derived embedding_input does not match its view policy")
        if sha256_bytes(view.embedding_input.encode("utf-8")) != view.input_sha256:
            raise StructureValidationError("derived view input hash does not match embedding_input")
        if not _within_limits(
            view.embedding_input,
            maximum_chars=maximum_chars,
            maximum_tokens=maximum_tokens,
            token_counter=token_counter,
        ):
            raise StructureValidationError("derived view exceeds its sealed limit")
        provisional = DerivedView(
            view_id="",
            contract_revision_id=view.contract_revision_id,
            node_id=view.node_id,
            parent_item_id=view.parent_item_id,
            view_type=view.view_type,
            ordinal=view.ordinal,
            display_text=view.display_text,
            embedding_input=view.embedding_input,
            spans=view.spans,
            context=view.context,
            input_sha256=view.input_sha256,
        )
        if view.view_id != _view_id(provisional):
            raise StructureValidationError("derived view id does not match its canonical payload")


__all__ = [
    "CANONICAL_LEAF_TYPES",
    "CONTEXTUAL_ITEM_CONTEXT_POLICY",
    "STRUCTURE_SCHEMA",
    "UNCLASSIFIED_FALLBACK_POLICY",
    "DerivedView",
    "IssuerParserProfile",
    "MajorClass",
    "NodeLink",
    "NodeSpan",
    "NodeType",
    "StructureArtifact",
    "StructureCoverage",
    "StructureNode",
    "StructurePage",
    "StructureValidationError",
    "ViewType",
    "build_derived_views",
    "build_structure_artifact",
    "build_unclassified_fallback_artifact",
    "contextual_item_policy_payload",
    "issuer_parser_profile",
    "make_contract_revision_id",
    "make_product_lineage_id",
    "parse_structure_artifact",
    "unclassified_fallback_policy_payload",
    "validate_derived_views",
    "validate_structure_artifact",
]
