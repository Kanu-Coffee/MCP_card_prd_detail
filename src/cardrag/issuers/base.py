"""Issuer adapter v1: site interpretation only, no durable side effects."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol, Self

import httpx
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, model_validator

from cardrag.domain import Issuer
from cardrag.domain import SourceRecord as SourceRecord

ADAPTER_CONTRACT_VERSION = "issuer-adapter.v1"


class DiscoveryMode(StrEnum):
    CURRENT = "current"
    HISTORY = "history"


class SourceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: str = ADAPTER_CONTRACT_VERSION
    issuer: Issuer
    mode: DiscoveryMode
    snapshot_id: str = Field(min_length=16)
    source_url: AnyHttpUrl
    started_at: datetime
    finished_at: datetime
    records: tuple[SourceRecord, ...]
    observed_count: int = Field(ge=0)
    parser_version: str
    warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def count_matches(self) -> Self:
        if self.observed_count != len(self.records):
            raise ValueError("observed_count does not match records")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at precedes started_at")
        return self


class IssuerAdapter(Protocol):
    issuer: Issuer
    allowed_hosts: frozenset[str]
    parser_version: str

    async def discover(
        self,
        client: httpx.AsyncClient,
        *,
        mode: DiscoveryMode,
        categories: frozenset[str] | None = None,
    ) -> SourceSnapshot: ...


class IssuerMarkupChanged(RuntimeError):
    """The endpoint responded, but its shape no longer satisfies the contract."""


class UnsupportedCategory(ValueError):
    pass
