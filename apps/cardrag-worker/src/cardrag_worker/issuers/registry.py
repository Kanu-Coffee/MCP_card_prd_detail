"""Closed, reviewable issuer registry and environment activation."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from cardrag_worker.contracts import IssuerAdapter, IssuerSpec

from .bc import BCAdapter
from .hana import HanaAdapter
from .hyundai import HyundaiAdapter
from .kb import KBAdapter
from .lotte import LotteAdapter
from .samsung import SamsungAdapter
from .shinhan import ShinhanAdapter
from .woori import WooriAdapter


@dataclass(frozen=True, slots=True)
class Registration:
    spec: IssuerSpec
    factory: Callable[[], IssuerAdapter]


_REGISTRATIONS = tuple(
    sorted(
        (
            Registration(WooriAdapter.spec, WooriAdapter),
            Registration(KBAdapter.spec, KBAdapter),
            Registration(ShinhanAdapter.spec, ShinhanAdapter),
            Registration(SamsungAdapter.spec, SamsungAdapter),
            Registration(HyundaiAdapter.spec, HyundaiAdapter),
            Registration(HanaAdapter.spec, HanaAdapter),
            Registration(LotteAdapter.spec, LotteAdapter),
            Registration(BCAdapter.spec, BCAdapter),
        ),
        key=lambda item: item.spec.sort_order,
    )
)

REGISTERED_ISSUERS: Mapping[str, Registration] = {row.spec.code: row for row in _REGISTRATIONS}
# Keep the fail-safe CLI default aligned with the production Compose default.
# Adding a future registration must not silently expand this release default.
DEFAULT_ENABLED_ISSUERS = ("woori", "kb", "shinhan", "samsung", "hyundai", "hana", "lotte", "bc")


def enabled_issuer_codes(
    raw: str | None = None,
    *,
    registry: Mapping[str, Registration] = REGISTERED_ISSUERS,
) -> tuple[str, ...]:
    value = os.environ.get("CARDRAG_ENABLED_ISSUERS") if raw is None else raw
    requested = (
        tuple(part.strip().casefold() for part in value.split(","))
        if value is not None
        else DEFAULT_ENABLED_ISSUERS
    )
    if not requested or any(not code for code in requested):
        raise ValueError("CARDRAG_ENABLED_ISSUERS must contain at least one issuer")
    if len(set(requested)) != len(requested):
        raise ValueError("CARDRAG_ENABLED_ISSUERS contains a duplicate issuer")
    unknown = sorted(set(requested).difference(registry))
    if unknown:
        raise ValueError("unknown enabled issuers: " + ", ".join(unknown))
    requested_set = set(requested)
    return tuple(
        code
        for code, registration in sorted(registry.items(), key=lambda item: item[1].spec.sort_order)
        if code in requested_set
    )


def enabled_adapters(
    raw: str | None = None,
    *,
    registry: Mapping[str, Registration] = REGISTERED_ISSUERS,
) -> tuple[IssuerAdapter, ...]:
    return tuple(registry[code].factory() for code in enabled_issuer_codes(raw, registry=registry))
