"""Deterministic, evidence-preserving metadata derivation from OCR structure text."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal

LAUNCH_DATE_PARSER_VERSION = "cardrag.launch-date.v2"

LaunchDateStatus = Literal["confirmed", "missing", "invalid", "conflicting"]
LaunchDateMatchKind = Literal["explicit_label", "date_before_launch"]


@dataclass(frozen=True, slots=True)
class DerivedTextSegment:
    """One source-backed unit in canonical document order."""

    text: str
    node_id: str | None = None
    page: int | None = None
    source_start: int | None = None
    source_end: int | None = None
    text_sha256: str | None = None
    group_id: str | None = None
    continuation_from_previous: bool = False


@dataclass(frozen=True, slots=True)
class LaunchDateEvidence:
    candidate_ordinal: int
    match_kind: LaunchDateMatchKind
    normalized_value: str | None
    excerpt: str
    segments: tuple[DerivedTextSegment, ...]


@dataclass(frozen=True, slots=True)
class LaunchDateResolution:
    launch_date: date | None
    status: LaunchDateStatus
    evidence: tuple[LaunchDateEvidence, ...] = ()


_DATE = (
    r"(?P<year>[0-9]{4})\s*"
    r"(?:년\s*|[.\-/]\s*)"
    r"(?P<month>[0-9]{1,3})\s*"
    r"(?:월\s*|[.\-/]\s*)"
    r"(?P<day>[0-9]{1,3})(?:\s*일)?"
    r"(?![0-9A-Za-z가-힣]|\.[0-9])"
)
_LABEL = r"(?:상품\s*)?(?:출시\s*일자?|발매\s*일자?|판매\s*개시\s*일자?|신규\s*출시)"
_LABEL_PATTERN = re.compile(_LABEL + r"[\s:：()\[\]{}\-–—•·|]*" + _DATE)
_BEFORE_PATTERN = re.compile(r"(?<![0-9A-Za-z가-힣])" + _DATE + r"\s*(?:신규\s*)?출시")
_NEGATIVE_DATE_LABEL = re.compile(r"(?:개정|시행|효력|약관\s*변경|서비스\s*시작)\s*일자?\s*[:：]?\s*$")
_TWO_DIGIT_EXPLICIT = re.compile(_LABEL + r"[\s:：()\[\]{}\-–—•·|]*[’'`]?[0-9]{2}[.\-/]")


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", value).replace("\u200b", "")


def _candidate(match: re.Match[str]) -> date | None:
    year, month, day = match.group("year"), match.group("month"), match.group("day")
    try:
        parsed = date(int(year), int(month), int(day))
    except ValueError:
        return None
    if len(month) > 2 or len(day) > 2 or not 1900 <= parsed.year <= 2099:
        return None
    return parsed


def _can_join(left: DerivedTextSegment, right: DerivedTextSegment) -> bool:
    if left.group_id is not None and left.group_id == right.group_id:
        return True
    return right.continuation_from_previous


def _windows(segments: Sequence[DerivedTextSegment]) -> Iterable[tuple[DerivedTextSegment, ...]]:
    for index, segment in enumerate(segments):
        yield (segment,)
        if index + 1 < len(segments) and _can_join(segment, segments[index + 1]):
            yield (segment, segments[index + 1])


def _excerpt(text: str, start: int, end: int) -> str:
    return " ".join(text[max(0, start - 40) : end + 80].split())[:240]


def resolve_launch_date_segments(
    segments: Sequence[DerivedTextSegment],
) -> LaunchDateResolution:
    """Resolve one revision launch date without promoting unrelated document dates."""

    evidence: list[LaunchDateEvidence] = []
    seen: set[tuple[str, str | None, tuple[str | None, ...]]] = set()
    valid_dates: set[date] = set()
    invalid = False

    for window in _windows(segments):
        text = " ".join(_normalized(segment.text).strip() for segment in window).strip()
        if not text or not any(token in text for token in ("출시", "발매", "판매")):
            continue
        for kind, pattern in (
            ("explicit_label", _LABEL_PATTERN),
            ("date_before_launch", _BEFORE_PATTERN),
        ):
            for match in pattern.finditer(text):
                prefix = text[max(0, match.start() - 24) : match.start()]
                if _NEGATIVE_DATE_LABEL.search(prefix):
                    continue
                parsed = _candidate(match)
                normalized_value = None if parsed is None else parsed.isoformat()
                key = (kind, normalized_value, tuple(segment.node_id for segment in window))
                if key in seen:
                    continue
                seen.add(key)
                evidence.append(
                    LaunchDateEvidence(
                        candidate_ordinal=len(evidence),
                        match_kind=kind,  # type: ignore[arg-type]
                        normalized_value=normalized_value,
                        excerpt=_excerpt(text, match.start(), match.end()),
                        segments=window,
                    )
                )
                if parsed is None:
                    invalid = True
                else:
                    valid_dates.add(parsed)
        if _TWO_DIGIT_EXPLICIT.search(text):
            invalid = True
            key = ("explicit_label", None, tuple(segment.node_id for segment in window))
            if key not in seen:
                seen.add(key)
                evidence.append(
                    LaunchDateEvidence(
                        candidate_ordinal=len(evidence),
                        match_kind="explicit_label",
                        normalized_value=None,
                        excerpt=" ".join(text.split())[:240],
                        segments=window,
                    )
                )

    if len(valid_dates) > 1:
        return LaunchDateResolution(None, "conflicting", tuple(evidence[:8]))
    if valid_dates:
        return LaunchDateResolution(next(iter(valid_dates)), "confirmed", tuple(evidence[:8]))
    if invalid:
        return LaunchDateResolution(None, "invalid", tuple(evidence[:8]))
    return LaunchDateResolution(None, "missing")


def resolve_launch_date_texts(texts: Iterable[str]) -> LaunchDateResolution:
    """Compatibility adapter for unstructured v5 readers."""

    return resolve_launch_date_segments(
        tuple(DerivedTextSegment(text=text, group_id=f"legacy-{index}") for index, text in enumerate(texts))
    )


def resolve_lineage_launch_date(
    revisions: Iterable[LaunchDateResolution],
) -> LaunchDateResolution:
    """Roll revision evidence up without letting a newer omission erase history."""

    rows = tuple(revisions)
    values = {row.launch_date for row in rows if row.launch_date is not None}
    evidence = tuple(item for row in rows for item in row.evidence)[:8]
    if len(values) > 1 or any(row.status == "conflicting" for row in rows):
        return LaunchDateResolution(None, "conflicting", evidence)
    if values:
        return LaunchDateResolution(next(iter(values)), "confirmed", evidence)
    if any(row.status == "invalid" for row in rows):
        return LaunchDateResolution(None, "invalid", evidence)
    return LaunchDateResolution(None, "missing", evidence)
