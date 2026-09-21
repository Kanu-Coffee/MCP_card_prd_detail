"""Compatibility facade for source-backed card launch-date derivation."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from cardrag_core import LAUNCH_DATE_PARSER_VERSION, LaunchDateStatus, resolve_launch_date_texts

PARSER_VERSION = LAUNCH_DATE_PARSER_VERSION

__all__ = [
    "PARSER_VERSION",
    "LaunchDateResolution",
    "LaunchDateStatus",
    "parse_launch_date",
    "resolve_launch_date",
    "resolve_launch_date_details",
]


@dataclass(frozen=True)
class LaunchDateResolution:
    launch_date: date | None
    status: LaunchDateStatus
    evidence: tuple[str, ...] = ()


def resolve_launch_date_details(texts: Iterable[str]) -> LaunchDateResolution:
    """Return the shared parser result in the stable MCP response shape."""

    resolution = resolve_launch_date_texts(texts)
    return LaunchDateResolution(
        resolution.launch_date,
        resolution.status,
        tuple(item.excerpt for item in resolution.evidence),
    )


def resolve_launch_date(texts: Iterable[str]) -> date | None:
    return resolve_launch_date_details(texts).launch_date


def parse_launch_date(text: str) -> date | None:
    return resolve_launch_date((text,))
