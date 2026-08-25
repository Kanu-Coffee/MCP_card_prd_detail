from __future__ import annotations

import re
from datetime import date
from typing import Any
from urllib.parse import urljoin, urlparse


class IssuerMarkupChanged(RuntimeError):
    """The issuer endpoint responded but no longer satisfies its parser contract."""


class UnsupportedCategory(ValueError):
    pass


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def parse_source_date(value: str) -> date:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) != 8:
        raise ValueError(f"expected an 8 digit source date, got {value!r}")
    return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))


def absolute_https_url(base: str, candidate: str, allowed_hosts: frozenset[str]) -> str:
    value = urljoin(base, candidate.strip())
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in allowed_hosts
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError("source URL is outside the issuer HTTPS allowlist")
    return value


def first(raw: dict[str, Any], *keys: str) -> str:
    for key in keys:
        if raw.get(key) is not None and str(raw[key]).strip():
            return str(raw[key]).strip()
    return ""


def natural_version_key(value: str) -> tuple[tuple[int, int | str], ...]:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in re.split(r"(\d+)", value.casefold())
        if part
    )


def require_minimum(rows: list[Any], *, label: str, minimum: int) -> None:
    if len(rows) < minimum:
        raise IssuerMarkupChanged(f"{label} yielded {len(rows)} records; expected at least {minimum}")
