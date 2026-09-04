"""Parse card official launch dates from disclosure AST display text."""

from __future__ import annotations

import re
from datetime import date

_LAUNCH_PATTERNS: tuple[re.Pattern[str], ...] = (
    # e.g. "상품 출시일 : 2026년 09월 01일", "카드 신규출시(2026년 06월 09일)"
    re.compile(
        r"(?:출시일|출시일자|신규\s*출시|신규출시)[\s:（(\-•·]*\s*"
        r"(19\d{2}|20\d{2})년\s*(1[0-2]|0?[1-9])월\s*([12]\d|3[01]|0?[1-9])(?:일|\b)"
    ),
    # e.g. "(1993년 10월 02일 출시)", "(2026년 6월 8일 출시)"
    re.compile(
        r"[（(\s](19\d{2}|20\d{2})년\s*(1[0-2]|0?[1-9])월\s*([12]\d|3[01]|0?[1-9])(?:일|\b)\s*출시"
    ),
    # e.g. "(2026.08.12 출시)", "(2026.06.29 출시)", "(1996.01.05 출시)"
    re.compile(
        r"[（(\s](19\d{2}|20\d{2})\.\s*(1[0-2]|0?[1-9])\.\s*([12]\d|3[01]|0?[1-9])\s*출시"
    ),
    # e.g. "출시일 : 2026.08.12", "신규출시 : 2026.06.29"
    re.compile(
        r"(?:출시일|출시일자|신규\s*출시|신규출시)[\s:（(\-•·]*\s*"
        r"(19\d{2}|20\d{2})\.\s*(1[0-2]|0?[1-9])\.\s*([12]\d|3[01]|0?[1-9])"
    ),
)


def parse_launch_date(text: str) -> date | None:
    """Extract the first valid card launch date found in the given text.

    Returns None if no release date pattern matches or if the extracted date
    is calendar-invalid (e.g. Feb 31).
    """
    for pattern in _LAUNCH_PATTERNS:
        match = pattern.search(text)
        if match is not None:
            year, month, day = (
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)),
            )
            try:
                return date(year, month, day)
            except ValueError:
                continue
    return None
