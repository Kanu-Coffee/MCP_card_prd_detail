"""Source-bound summary candidates independent of the coarse structure class."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

SUMMARY_CLASSIFIER_VERSION = "cardrag.product-summary.v4"
_NOTICE = re.compile(
    r"부가\s*서비스.{0,50}(?:변경|축소|폐지|유지)|상품\s*출시일|금융소비자|휴업|파산|수익성|법적\s*고지"
    r"|신용평점|신용등급|개인\s*신용|고지하여|심의필|준법감시"
    r"|국제브랜드.{0,20}수수료.{0,30}산정"
)
_REFERENCE = re.compile(r"(?:참고|참조|확인)[.!]?\s*$")
_BENEFIT = re.compile(
    r"할인|적립|캐시백|포인트|마일(?:리지)?|무료|면제|라운지|보험|바우처|혜택|무이자"
)
_CONDITION = re.compile(
    r"전월|실적|한도|횟수|제외|이상|미만|이하|초과|까지|건당|\d+\s*회|월\s*\d|일\s*\d"
)
_EXEMPTION = re.compile(
    r"(?:전월\s*)?실적에?\s*관계\s*없이|조건\s*[,·및 ]*\s*한도\s*없이|"
    r"(?:전월\s*)?(?:실적|조건|한도)"
    r"(?:\s*(?:조건|제한))?\s*(?:없음|없이|무관)|무실적|무제한"
)
_NEGATED_OFFER = re.compile(
    r"(?:할인|적립|캐시백|포인트|마일리지|무료|무이자\s*할부)"
    r"\s*(?:혜택\s*)?(?:(?:서비스|이용|제공)\s*)?"
    r"(?:대상(?:에서)?\s*)?(?:제외|불가|미적용|미제공|제공하지\s*않(?:음|습니다)?)"
)
_FEE_BAD = re.compile(r"반환|산정|중도해지|제외|수수료\s*및|이자|연체|일할")
_AMOUNT = re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:만\s*)?원|면제|없음|무료")
_TYPES = {
    "MAJOR_SECTION",
    "ITEM",
    "PARAGRAPH",
    "LIST_ITEM",
    "FOOTNOTE",
    "TABLE_ROW",
    "TABLE",
    "UNCLASSIFIED",
}


@dataclass(frozen=True)
class SummaryCandidate:
    node_id: str
    text: str
    field: Literal["annual_fee", "benefit", "condition"]
    heading: bool = False
    score: int = 0
    source_excerpt: str | None = None
    label_detail: bool = False


def bounded_source_text(text: str, limit: int) -> str | None:
    """Keep whole source clauses; never chop off a trailing limit or negation."""
    if len(text) <= limit:
        return text
    # Long indivisible sentences are omitted rather than misrepresented.
    return None


_EXAMPLE = re.compile(r"^[\s*#>●·※\\\-]*(?:[\[【(]\s*)?(?:예시|계산\s*예|예)(?:\s|[:：\]】)]|$)")
_TABLE_CONDITION = re.compile(r"조건|확인사항|실적|한도|제외|유의|이용금액|횟수")
_TABLE_LABEL = re.compile(r"구분|서비스|대상|업종|가맹점")
_TABLE_OFFER = re.compile(r"내용|혜택|할인|적립|포인트|마일리지|캐시백|제공")
_QUALIFIER = re.compile(
    r"(?:전월\s*)?(?:이용\s*)?실적[^;()%]{0,45}?(?:이상|미만|이하|초과)(?:\s*이용\s*시)?"
    r"|\d[\d,.]*\s*(?:천|만)?원\s*이상\s*(?:이용|결제)\s*시"
    r"|(?:\d+\s*)?(?:일|월|연)\s*\d+\s*회"
    r"|(?:동반\s*)?\d+\s*인까지"
    r"|(?:월|일|연|건당)[^;()%]{0,25}?\d[\d,]*\s*(?:천|만)?원(?:까지|\s*한도)"
)


def _clean(text: str) -> str:
    return " ".join(re.sub(r"<br\s*/?>", " ", text, flags=re.I).split())


def _strings(value: Any) -> list[str]:
    try:
        data = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return []
    return data if isinstance(data, list) and all(isinstance(v, str) for v in data) else []


def _table_fragments(
    node: Mapping[str, Any], by_id: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Use column roles when available; leave unstructured rows to the normal classifier."""
    cells = _strings(node.get("table_cells_json"))
    parent = by_id.get(str(node.get("parent_id")), {})
    headers = _strings(node.get("table_headers_json")) or _strings(parent.get("table_headers_json"))
    if not cells or not headers or len(cells) != len(headers):
        return [dict(node)]
    fragments = []
    for index, cell in enumerate(cells):
        text = _clean(cell)
        if not text:
            continue
        header = headers[index] or next((h for h in reversed(headers[:index]) if h.strip()), "")
        role = (
            "condition"
            if _TABLE_CONDITION.search(header)
            else "offer"
            if _TABLE_OFFER.search(header)
            else "label"
            if _TABLE_LABEL.search(header)
            else "unknown"
        )
        source = " ".join(cell.split())
        fragment = dict(node, display_text=text, _column_role=role, _source_excerpt=source)
        # A separate service cell is a real heading; it also contextualizes rate-only cells.
        label = next(
            (
                c
                for h, c in reversed(list(zip(headers, cells, strict=True)))
                if _TABLE_LABEL.search(h) and c.strip()
            ),
            "",
        )
        fragment["_table_label"] = _clean(label)
        fragment["_offer_value"] = text
        fragment["_unavailable"] = any(
            re.fullmatch(r"[×xX-]|미제공|불가", c.strip())
            for h, c in zip(headers, cells, strict=True)
            if _TABLE_OFFER.search(h)
        )
        if role == "offer" and label and _clean(label) not in text:
            raw = " ".join(str(node["display_text"]).split())
            start, end = raw.find(" ".join(label.split())), raw.find(source)
            if start >= 0 and end >= 0:
                excerpt = raw[
                    min(start, end) : max(start + len(" ".join(label.split())), end + len(source))
                ]
                if len(excerpt) <= 300:
                    fragment["display_text"] = _clean(excerpt)
                    fragment["_source_excerpt"] = excerpt
        fragments.append(fragment)
    return fragments


def _condition_fragments(text: str) -> list[str]:
    """Return source qualifiers without repeating a complete positive offer."""
    fragments = [m.group() for m in _QUALIFIER.finditer(text)]
    fragments.extend(m.group() for m in _EXEMPTION.finditer(text))
    fragments.extend(
        m.group(1)
        for m in re.finditer(r"\(([^()]*)\)", text)
        if _CONDITION.search(m.group(1)) or re.search(r"한함|해당", m.group(1))
    )
    return list(dict.fromkeys(fragments))


def summary_candidates(nodes: Sequence[Mapping[str, Any]]) -> list[SummaryCandidate]:
    by_id = {str(n["node_id"]): n for n in nodes}
    candidates: list[SummaryCandidate] = []
    exclusion_scope: dict[str, bool] = {}
    prepared = []
    for original in nodes:
        node = dict(original)
        scope = str(node.get("parent_id"))
        text = _clean(str(node["display_text"]))
        if node["node_type"] == "LIST_ITEM" or (
            node["node_type"] == "PARAGRAPH" and re.match(r"^[·•-]", text)
        ):
            node["_excluded_list"] = exclusion_scope.get(scope, False)
        elif text:
            exclusion_scope[scope] = bool(
                re.search(r"제외\s*(?:대상|항목)|(?:아래|다음).*제외", text)
            )
        prepared.append(node)
    expanded = [
        fragment
        for node in prepared
        for fragment in (
            _table_fragments(node, by_id) if str(node["node_type"]) == "TABLE_ROW" else [dict(node)]
        )
    ]
    for node in expanded:
        node_id = str(node["node_id"])
        text = _clean(str(node["display_text"]))
        kind = str(node["node_type"])
        if kind not in _TYPES or not text or not re.search(r"[가-힣A-Za-z0-9]", text):
            continue
        if kind == "TABLE_ROW" and re.search(r"혜택률|할인율|적립률|구분.{0,30}대상", text):
            continue
        if str(node.get("table_role") or "").lower() == "header" or (
            kind == "TABLE_ROW"
            and not _AMOUNT.search(text)
            and "%" not in text
            and not _BENEFIT.search(text)
            and not _CONDITION.search(text)
        ):
            continue
        ancestors: list[str] = []
        parent = node.get("parent_id")
        visited = {node_id}
        while parent and str(parent) in by_id and str(parent) not in visited and len(visited) < 64:
            visited.add(str(parent))
            ancestor = by_id[str(parent)]
            ancestors.append(str(ancestor.get("raw_heading") or ""))
            if ancestor.get("node_type") == "TABLE":
                ancestors.append(str(ancestor.get("table_headers_json") or ""))
            parent = ancestor.get("parent_id")
        context = " ".join(ancestors)
        if (
            _NOTICE.search(text)
            or _NOTICE.search(context)
            or _REFERENCE.search(text)
            or _EXAMPLE.search(text)
            or _EXAMPLE.search(context)
            or re.search(r"\d[^=]{0,100}[×X+][^=]{0,100}=\s*\d", text)
        ):
            continue
        heading = re.sub(r"^[#>*\s]+|[*\s]+$", "", str(node.get("raw_heading") or ""))
        column_role = node.get("_column_role")
        source_excerpt = node.get("_source_excerpt")
        fee_context = "연회비" in text or "연회비" in context
        if fee_context:
            if _AMOUNT.search(text) and not _FEE_BAD.search(text):
                score = 10 if "본인" in text else 5
                if kind == "TABLE":
                    score -= 2
                if "가족" in text and "본인" not in text:
                    score -= 5
                candidates.append(SummaryCandidate(node_id, text, "annual_fee", score=score))
            continue
        exempt = bool(_EXEMPTION.search(text))
        remaining = _EXEMPTION.sub("", text)
        restricted = bool(_CONDITION.search(remaining)) or "조건" in remaining
        # Remove an explicitly negated offer before deciding whether a separate
        # positive offer remains. Parenthesized exceptions do not negate the main offer.
        main_text = re.sub(r"\([^()]*\)", " ", text)
        positive_text = _NEGATED_OFFER.sub("", main_text)
        negated_offer_only = bool(_NEGATED_OFFER.search(main_text)) and not _BENEFIT.search(
            positive_text
        )
        # Offer quantities and services differ from procedures, caps and exclusions.
        concrete = bool(
            re.search(
                r"\d+(?:\.\d+)?\s*%|무료|면제|무이자.{0,15}(?:제공|개월|혜택)|단체보험|보험\s*(?:가입|보장|제공)"
                r"|바우처.{0,20}(?:선택|제공|할인|증정|이용)",
                text,
            )
        ) or bool(
            re.search(
                r"\d[\d,]*\s*(?:천|만)?\s*원\s*(?:까지\s*)?(?:청구\s*)?(?:할인|적립|캐시백|포인트)"
                r"|\d[\d,]*\s*(?:마일|포인트)\s*(?:제공|적립)|\d+\s*원\s*/\s*ℓ"
                r"|\d[\d,]*\s*원당\s*\d+(?:\.\d+)?\s*(?:마일|포인트)",
                text,
            )
        )
        restriction_only = bool(
            re.search(
                r"(?:할인|적립|캐시백|서비스)\s*(?:혜택\s*)?(?:대상에서\s*|대상\s*)?(?:제외|불가|미적용)"
                r"|실적\s*제외|(?:월|통합|일)\s*(?:할인|적립|캐시백|혜택)?\s*한도",
                text,
            )
        ) and not re.search(r"\d+(?:\.\d+)?\s*%|무료\s*(?:제공|이용)", text)
        if re.search(
            r"(?:^[-*·\s]*월.{0,30}(?:이용금액|한도)|제외\s*$|적용되지\s*않)", text
        ) and not re.search(r"\d+(?:\.\d+)?\s*%|무료\s*(?:제공|이용)", text):
            restriction_only = True
            restricted = True
        if re.search(
            r"바우처.{0,30}(?:기간|신청|선택 및 변경)|"
            r"라운지.{0,20}(?:다운로드|발급 후)|사용\s*제한|보험가입.{0,20}정보",
            text,
        ):
            concrete = False
        exclusion_context = bool(
            re.search(r"(?:실적|할인|적립|캐시백|서비스).{0,15}제외|제외\s*(?:대상|항목)", context)
        )
        exclusion_context = exclusion_context or bool(node.get("_excluded_list"))
        restricted = restricted or exclusion_context
        table_offer = (
            kind == "TABLE_ROW"
            and bool(_BENEFIT.search(context))
            and not re.search(r"금리|이자|수수료|통계", context)
            and bool(_AMOUNT.search(text) or "%" in text)
        )
        offer_label = (
            kind in {"MAJOR_SECTION", "ITEM"}
            and bool(heading)
            and len(heading) <= 60
            and bool(re.search(r"(?:할인|적립|캐시백|혜택|서비스)(?:\([^)]*\))?$", heading))
            and bool(_BENEFIT.search(heading))
            and not re.search(
                r"조건|한도|제외|안내|방법|유의|지급|접수|절사|금액을|신청|계산", heading
            )
        )
        benefit = (
            ((bool(_BENEFIT.search(text)) and concrete) or table_offer or offer_label)
            and not restriction_only
            and not exclusion_context
            and not negated_offer_only
        )
        if re.search(
            r"\[예시\]|^예시|계산\s*예|연체\s*시|유이자|할부금리|연체이자"
            r"|제공하지\s*않|제공\s*불가",
            text,
        ):
            benefit = False
        if re.search(r"미적립|미제공|제공되지|제공하지|면제되지", text):
            benefit = False
            restricted = True
        if node.get("_unavailable"):
            benefit = False
        if column_role == "condition":
            benefit = False
            restricted = True
        elif column_role == "label":
            benefit = False
            restricted = False
        elif column_role == "unknown" and kind == "TABLE_ROW":
            # Blank/multi-level headers are not evidence of an offer.
            benefit = (
                (bool(_BENEFIT.search(text) and concrete) or table_offer and concrete)
                and not restriction_only
                and not negated_offer_only
            )
        elif column_role == "offer" and not node.get("_unavailable"):
            benefit = benefit or (
                bool(node.get("_table_label"))
                and concrete
                and not negated_offer_only
                and not restriction_only
                and not re.search(r"금리|이자|수수료|통계", context)
            )
        if column_role == "offer" and not re.search(
            r"\d|무료|면제|제공|적립|할인", str(node.get("_offer_value", text))
        ):
            benefit = False
        # A notice label is context, not a concrete offer or condition.
        label_only = bool(
            re.fullmatch(r"[*#\s]*(?:할인 제공 시 )?(?:유의사항|이용안내)[*\s]*", text)
        )
        if label_only:
            continue
        # Heading-shaped paragraphs merely repeat a parent label, not offer detail.
        duplicated_label = bool(
            heading and _clean(heading) == re.sub(r"^[#>*\s]+|[*\s]+$", "", text)
        ) or any(
            re.sub(r"^[#>*\s]+|[*\s]+$", "", text) == re.sub(r"^[#>*\s]+|[*\s]+$", "", a)
            for a in ancestors
            if a
        )
        if column_role == "label" and text and not re.search(r"기본|추가|구간", text):
            siblings = _strings(node.get("table_cells_json"))
            if any(
                _BENEFIT.search(c) or re.search(r"\d+(?:\.\d+)?\s*%|\d+원/ℓ", c) for c in siblings
            ):
                candidates.append(
                    SummaryCandidate(
                        node_id, text, "benefit", heading=True, source_excerpt=source_excerpt
                    )
                )
        if benefit:
            if (
                heading
                and kind in {"MAJOR_SECTION", "ITEM"}
                and not re.search(r"유의사항|이용\s*안내|공통\s*안내|기타\s*안내|^\(|▶", heading)
            ):
                candidates.append(SummaryCandidate(node_id, heading, "benefit", heading=True))
            if (
                kind != "TABLE"
                and (kind != "MAJOR_SECTION" or concrete)
                and (not duplicated_label or concrete)
            ):
                candidates.append(
                    SummaryCandidate(
                        node_id,
                        text,
                        "benefit",
                        score=10
                        if not duplicated_label and not re.fullmatch(r"[\d, .%원천만월]+", text)
                        else 2,
                        label_detail=duplicated_label,
                        source_excerpt=source_excerpt,
                    )
                )
        if restricted or (exempt and not benefit):
            if kind not in {"TABLE", "MAJOR_SECTION"} and not duplicated_label:
                for condition in _condition_fragments(text) if benefit else [text]:
                    candidates.append(
                        SummaryCandidate(
                            node_id,
                            condition,
                            "condition",
                            score=8
                            if column_role == "condition"
                            or _condition_fragments(text)
                            or re.search(r"실적|한도|횟수|제외|한함|\d+\s*회", condition)
                            or exempt
                            else 1,
                            source_excerpt=source_excerpt,
                        )
                    )
    return candidates
