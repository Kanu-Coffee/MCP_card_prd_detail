"""Public MCP issuer input contract and alias normalization."""

from __future__ import annotations

import re
import unicodedata
from typing import Annotated, Final

from pydantic import Field

CANONICAL_ISSUER_CODES: Final = (
    "bc",
    "hana",
    "hyundai",
    "kb",
    "lotte",
    "samsung",
    "shinhan",
    "woori",
)

_ISSUER_DESCRIPTION: Final = (
    "Card issuer. Canonical values: bc, hana, hyundai, kb, lotte, samsung, "
    "shinhan, woori. Common Korean and English names such as 비씨카드, 하나카드, "
    "현대카드, KB국민카드, 롯데카드, 삼성카드, 신한카드, and 우리카드 are "
    "accepted and normalized to the canonical value."
)

IssuerInput = Annotated[
    str,
    Field(
        min_length=1,
        max_length=64,
        description=_ISSUER_DESCRIPTION,
        examples=["kb", "KB국민카드"],
    ),
]

_SEPARATOR_RE: Final = re.compile(r"[\s._-]+")
_ALIASES: Final[dict[str, str]] = {
    # BC Card
    "bc": "bc",
    "bccard": "bc",
    "bc카드": "bc",
    "비씨": "bc",
    "비씨카드": "bc",
    # Hana Card
    "hana": "hana",
    "hanacard": "hana",
    "하나": "hana",
    "하나카드": "hana",
    # Hyundai Card
    "hyundai": "hyundai",
    "hyundaicard": "hyundai",
    "현대": "hyundai",
    "현대카드": "hyundai",
    # KB Kookmin Card
    "kb": "kb",
    "kbcard": "kb",
    "kbkookmin": "kb",
    "kbkookmincard": "kb",
    "kb국민": "kb",
    "kb국민카드": "kb",
    "kookmin": "kb",
    "kookmincard": "kb",
    "국민": "kb",
    "국민카드": "kb",
    # Lotte Card
    "lotte": "lotte",
    "lottecard": "lotte",
    "롯데": "lotte",
    "롯데카드": "lotte",
    # Samsung Card
    "samsung": "samsung",
    "samsungcard": "samsung",
    "삼성": "samsung",
    "삼성카드": "samsung",
    # Shinhan Card
    "shinhan": "shinhan",
    "shinhancard": "shinhan",
    "신한": "shinhan",
    "신한카드": "shinhan",
    # Woori Card
    "woori": "woori",
    "wooricard": "woori",
    "우리": "woori",
    "우리카드": "woori",
}


def normalize_issuer(value: str) -> str:
    """Return the canonical issuer code or fail before repository lookup."""

    key = _SEPARATOR_RE.sub("", unicodedata.normalize("NFKC", value).strip().casefold())
    canonical = _ALIASES.get(key)
    if canonical is None:
        allowed = ", ".join(CANONICAL_ISSUER_CODES)
        raise ValueError(f"unknown issuer; use one of the canonical values: {allowed}")
    return canonical


def normalize_optional_issuer(value: str | None) -> str | None:
    """Normalize an optional issuer filter while preserving an omitted filter."""

    return None if value is None else normalize_issuer(value)


__all__ = [
    "CANONICAL_ISSUER_CODES",
    "IssuerInput",
    "normalize_issuer",
    "normalize_optional_issuer",
]
