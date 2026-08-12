"""Shared pure parsing and snapshot helpers."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urljoin, urlparse

from pydantic import AnyHttpUrl

from cardrag.domain import Issuer

from .base import DiscoveryMode, SourceRecord, SourceSnapshot


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
    if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
        raise ValueError("source URL is outside the issuer HTTPS allowlist")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("source URL contains forbidden components")
    return value


def canonical_snapshot(
    *,
    issuer: Issuer,
    mode: DiscoveryMode,
    source_url: str,
    parser_version: str,
    records: Iterable[SourceRecord],
    started_at: datetime,
    warnings: Iterable[str] = (),
) -> SourceSnapshot:
    deduped: dict[tuple[str, str, date, str, str], SourceRecord] = {}
    for record in records:
        key = (
            record.product_code,
            record.document_type,
            record.effective_date,
            record.source_version,
            record.source_post_id,
        )
        deduped[key] = record
    ordered = tuple(
        sorted(
            deduped.values(),
            key=lambda item: (
                item.product_code,
                item.effective_date,
                natural_version_key(item.source_version),
                item.source_post_id,
            ),
        )
    )
    body = {
        "contract_version": "source-snapshot.v1",
        "issuer": issuer.value,
        "mode": mode.value,
        "source_url": source_url,
        "parser_version": parser_version,
        "records": [item.model_dump(mode="json", exclude={"discovered_at"}) for item in ordered],
    }
    snapshot_id = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    finished_at = datetime.now(UTC)
    return SourceSnapshot(
        issuer=issuer,
        mode=mode,
        snapshot_id=snapshot_id,
        source_url=AnyHttpUrl(source_url),
        started_at=started_at,
        finished_at=finished_at,
        records=ordered,
        observed_count=len(ordered),
        parser_version=parser_version,
        warnings=tuple(warnings),
    )


def natural_version_key(value: str) -> tuple[tuple[int, int | str], ...]:
    parts: list[tuple[int, int | str]] = []
    for part in re.split(r"(\d+)", value.casefold()):
        if not part:
            continue
        parts.append((0, int(part)) if part.isdigit() else (1, part))
    return tuple(parts)


def require_nonempty(records: list[Any], *, label: str, expected_minimum: int = 1) -> None:
    if len(records) < expected_minimum:
        from .base import IssuerMarkupChanged

        raise IssuerMarkupChanged(
            f"{label} yielded {len(records)} records; expected at least {expected_minimum}"
        )
