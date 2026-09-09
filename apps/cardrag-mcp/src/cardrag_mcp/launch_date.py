"""Parse card official launch dates from disclosure AST display text."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date

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


def resolve_launch_date(texts: Iterable[str]) -> date | None:
    """Return one unambiguous, calendar-valid launch date across disclosure nodes.

    Missing dates, conflicting dates and invalid explicit date candidates remain
    unknown. Repeated mentions of the same valid launch date are accepted.
    """
    dates: set[date] = set()
    for text in texts:
        if "출시" not in text:
            continue
        for pattern in _LAUNCH_PATTERNS:
            for match in pattern.finditer(text):
                year, month, day = match.groups()
                if len(month) > 2 or len(day) > 2:
                    return None
                try:
                    candidate = date(int(year), int(month), int(day))
                except ValueError:
                    return None
                if not 1900 <= candidate.year <= 2099:
                    return None
                dates.add(candidate)
                if len(dates) > 1:
                    return None
    return next(iter(dates)) if dates else None


def parse_launch_date(text: str) -> date | None:
    """Return an unambiguous official launch date from a disclosure text node."""
    return resolve_launch_date((text,))
