"""Source-bound summary candidates independent of the coarse structure class."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

SUMMARY_CLASSIFIER_VERSION = "cardrag.product-summary.v3"
_NOTICE = re.compile(
    r"부가\s*서비스\s*(?:변경|축소|폐지)|상품\s*출시일|금융소비자|휴업|파산|수익성|법적\s*고지"
)
_REFERENCE = re.compile(r"(?:참고|참조|확인)[.!]?\s*$")
_BENEFIT = re.compile(r"할인|적립|캐시백|포인트|마일리지|무료|면제|라운지|보험|바우처|혜택|무이자")
_CONDITION = re.compile(
    r"전월|실적|한도|횟수|제외|이상|미만|이하|초과|까지|건당|\d+\s*회|월\s*\d|일\s*\d"
)
_EXEMPTION = re.compile(
    r"조건\s*[,·및 ]*\s*한도\s*없이|(?:전월\s*)?(?:실적|조건|한도)"
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


def bounded_source_text(text: str, limit: int) -> str | None:
    """Keep whole source clauses; never chop off a trailing limit or negation."""
    if len(text) <= limit:
        return text
    # Long indivisible sentences are omitted rather than misrepresented.
    return None


def summary_candidates(nodes: Sequence[Mapping[str, Any]]) -> list[SummaryCandidate]:
    by_id = {str(n["node_id"]): n for n in nodes}
    candidates: list[SummaryCandidate] = []
    for node in nodes:
        node_id = str(node["node_id"])
        text = " ".join(str(node["display_text"]).split())
        kind = str(node["node_type"])
        if kind not in _TYPES or not text or not re.search(r"[가-힣A-Za-z0-9]", text):
            continue
        if kind == "TABLE_ROW" and re.search(r"혜택률|할인율|적립률|구분.{0,30}대상", text):
            continue
        if node.get("table_role") == "header" or (
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
        if _NOTICE.search(text) or _NOTICE.search(context) or _REFERENCE.search(text):
            continue
        heading = re.sub(r"^[#>*\s]+|[*\s]+$", "", str(node.get("raw_heading") or ""))
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
                r"\d+(?:\.\d+)?\s*%|무료|면제|무이자|라운지|단체보험|보험\s*(?:가입|보장|제공)"
                r"|바우처.{0,20}(?:선택|제공|할인|증정|이용)",
                text,
            )
        ) or bool(
            re.search(r"\d[\d,]*\s*(?:만\s*)?원\s*(?:까지\s*)?(?:할인|적립|캐시백|포인트)", text)
        )
        restriction_only = bool(
            re.search(
                r"(?:할인|적립|캐시백|서비스)\s*(?:대상에서\s*|대상\s*)?(?:제외|불가|미적용)"
                r"|실적\s*제외|(?:월|통합|일)\s*(?:할인|적립|캐시백|혜택)?\s*한도",
                text,
            )
        ) and not re.search(r"\d+(?:\.\d+)?\s*%|무료\s*(?:제공|이용)", text)
        exclusion_context = bool(
            re.search(r"(?:실적|할인|적립|캐시백|서비스).{0,15}제외|제외\s*(?:대상|항목)", context)
        )
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
            and bool(re.search(r"(?:할인|적립|캐시백|혜택|서비스)$", heading))
            and bool(_BENEFIT.search(heading))
            and not re.search(r"조건|한도|제외|안내|방법|유의|지급|접수", heading)
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
        # A notice label is context, not a concrete offer or condition.
        label_only = bool(
            re.fullmatch(r"[*#\s]*(?:할인 제공 시 )?(?:유의사항|이용안내)[*\s]*", text)
        )
        if label_only:
            continue
        if benefit:
            if (
                heading
                and kind in {"MAJOR_SECTION", "ITEM"}
                and not re.search(r"유의사항|이용\s*안내|공통\s*안내|기타\s*안내|^\(|▶", heading)
            ):
                candidates.append(SummaryCandidate(node_id, heading, "benefit", heading=True))
            if kind not in {"TABLE", "MAJOR_SECTION"}:
                candidates.append(SummaryCandidate(node_id, text, "benefit"))
        if restricted or (exempt and not benefit):
            if kind not in {"TABLE", "MAJOR_SECTION"}:
                candidates.append(SummaryCandidate(node_id, text, "condition"))
    return candidates
