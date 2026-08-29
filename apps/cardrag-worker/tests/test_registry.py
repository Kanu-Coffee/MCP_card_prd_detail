from __future__ import annotations

from dataclasses import dataclass

import httpx
import pytest

from cardrag_worker.contracts import IssuerSpec, SourceRecord, SourceSnapshot
from cardrag_worker.issuers.registry import (
    DEFAULT_ENABLED_ISSUERS,
    REGISTERED_ISSUERS,
    Registration,
    enabled_adapters,
    enabled_issuer_codes,
)


@dataclass
class LotteAdapter:
    spec = IssuerSpec(
        code="lotte",
        display_name="롯데카드",
        sort_order=40,
        allowed_hosts=frozenset({"lotte.example"}),
        categories=("credit",),
    )
    parser_version = "lotte.v1"

    async def discover_current(self, client: httpx.AsyncClient) -> SourceSnapshot:
        raise NotImplementedError

    async def prepare_download(self, client: httpx.AsyncClient, source: SourceRecord) -> object:
        raise NotImplementedError


def test_default_activation_is_fixed_and_does_not_expand_with_registry() -> None:
    registry = {
        **REGISTERED_ISSUERS,
        "lotte": Registration(LotteAdapter.spec, LotteAdapter),
    }
    assert DEFAULT_ENABLED_ISSUERS == ("woori", "kb", "shinhan", "samsung")
    assert enabled_issuer_codes(None, registry=registry) == DEFAULT_ENABLED_ISSUERS
    assert [adapter.spec.code for adapter in enabled_adapters("lotte", registry=registry)] == ["lotte"]


def test_samsung_is_registered_and_default_enabled_after_shinhan() -> None:
    registration = REGISTERED_ISSUERS["samsung"]

    assert registration.spec.display_name == "삼성카드"
    assert registration.factory().spec is registration.spec
    assert enabled_issuer_codes("samsung,woori") == ("woori", "samsung")


def test_production_registry_has_a_nontrivial_first_run_discovery_floor() -> None:
    assert all(row.spec.minimum_records >= 25 for row in REGISTERED_ISSUERS.values())


def test_registry_rejects_unknown_and_duplicate_activation() -> None:
    with pytest.raises(ValueError, match="unknown"):
        enabled_issuer_codes("woori,unknown")
    with pytest.raises(ValueError, match="duplicate"):
        enabled_issuer_codes("kb,kb")


def test_issuer_spec_validates_open_code_and_finite_retry_defaults() -> None:
    with pytest.raises(ValueError):
        IssuerSpec(
            code="INVALID CODE",
            display_name="bad",
            sort_order=1,
            allowed_hosts=frozenset({"example.com"}),
            categories=("credit",),
        )
    with pytest.raises(ValueError):
        IssuerSpec(
            code="lotte",
            display_name="bad",
            sort_order=1,
            allowed_hosts=frozenset({"example.com"}),
            categories=("credit",),
            retry_base_seconds=float("inf"),
        )
