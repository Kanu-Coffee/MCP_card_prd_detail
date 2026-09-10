"""Parse card official launch dates from disclosure AST display text."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Literal

_LAUNCH_LABEL = r"(?:출시일자?|신규\s*출시)[\s:：（(\-•·]*"
# Capture complete numeric components before checking their calendar validity.
# Restricting day alternatives in the regex can silently turn an OCR value such
# as "99" or "101" into the valid prefixes "9" or "10".
_KOREAN_DATE = r"([0-9]{4})년\s*([0-9]+)월\s*([0-9]+)(?:일|(?!\w))"
_DOTTED_DATE = r"([0-9]{4})\.\s*([0-9]+)\.\s*([0-9]+)(?!\w|\.[0-9])"

_LAUNCH_PATTERNS: tuple[re.Pattern[str], ...] = (
    # e.g. "상품 출시일 : 2026년 09월 01일", "카드 신규출시(2026년 06월 09일)"
    re.compile(_LAUNCH_LABEL + _KOREAN_DATE),
    # e.g. "(1993년 10월 02일 출시)", "(2026년 6월 8일 출시)"
    re.compile(r"(?<!\w)" + _KOREAN_DATE + r"\s*출시"),
    # e.g. "(2026.08.12 출시)", "(2026.06.29 출시)", "(1996.01.05 출시)"
    re.compile(r"(?<!\w)" + _DOTTED_DATE + r"\s*출시"),
    # e.g. "출시일 : 2026.08.12", "신규출시 : 2026.06.29"
    re.compile(_LAUNCH_LABEL + _DOTTED_DATE),
)


PARSER_VERSION = "launch-date-v1.0.22"
LaunchDateStatus = Literal["confirmed", "missing", "invalid", "conflicting"]


@dataclass(frozen=True)
class LaunchDateResolution:
    launch_date: date | None
    status: LaunchDateStatus
    evidence: tuple[str, ...] = ()


def resolve_launch_date_details(texts: Iterable[str]) -> LaunchDateResolution:
    """Resolve a date and an explicit reason, preserving short source excerpts."""
    dates: set[date] = set()
    invalid = False
    evidence: list[str] = []
    for text in texts:
        if "출시" not in text:
            continue
        matched_spans: list[tuple[int, int]] = []
        for pattern in _LAUNCH_PATTERNS:
            for match in pattern.finditer(text):
                matched_spans.append(match.span())
                excerpt = " ".join(text[max(0, match.start() - 40) : match.end() + 80].split())[
                    :240
                ]
                if excerpt not in evidence and len(evidence) < 8:
                    evidence.append(excerpt)
                year, month, day = match.groups()
                try:
                    candidate = date(int(year), int(month), int(day))
                    if len(month) > 2 or len(day) > 2 or not 1900 <= candidate.year <= 2099:
                        raise ValueError("invalid launch date")
                except ValueError:
                    invalid = True
                else:
                    dates.add(candidate)
        # A date-like value adjacent to a launch label that the strict grammar
        # cannot read is invalid; an explicit "미기재"/"미확인" remains missing.
        for date_label in re.finditer(_LAUNCH_LABEL + r"[0-9]{4,}[.년/-]", text):
            if not any(start <= date_label.start() < end for start, end in matched_spans):
                invalid = True
                if len(evidence) < 8:
                    evidence.append(" ".join(text.split())[:240])
    if len(dates) > 1:
        return LaunchDateResolution(None, "conflicting", tuple(evidence))
    if invalid:
        return LaunchDateResolution(None, "invalid", tuple(evidence))
    if dates:
        return LaunchDateResolution(next(iter(dates)), "confirmed", tuple(evidence))
    return LaunchDateResolution(None, "missing", tuple(evidence))


def resolve_launch_date(texts: Iterable[str]) -> date | None:
    """Return one unambiguous calendar-valid date, preserving the legacy API."""
    return resolve_launch_date_details(texts).launch_date


def parse_launch_date(text: str) -> date | None:
    """Return an unambiguous official launch date from a disclosure text node."""
    return resolve_launch_date((text,))
