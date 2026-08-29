"""Evidence-bound planning for v5 contract revision history.

Only source payloads retained in canonical discovery snapshots (or the exact
current ``SourceRecord``) may supply revision metadata.  PDF cache timestamps
and links prove byte-revision state, but are never used to invent an effective
date or historical product name.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypedDict

from .contracts import SourceRecord, canonical_sha256
from .pdf_cache import PDFSourceIdentity
from .state import PDFSourceRevisionRow

TemporalStatusV5 = Literal["current", "superseded", "ambiguous"]
UnresolvedRevisionReasonV5 = Literal[
    "document_identity_collision",
    "pdf_cache_object_unavailable",
    "source_metadata_unresolved",
]
REVISION_HISTORY_POLICY_VERSION = "cardrag.revision-history.v1"
UNRESOLVED_REVISION_LEDGER_SCHEMA = "cardrag.revision-history-unresolved.v1"
_SOURCE_ID = re.compile(r"source_[0-9a-f]{64}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_UNRESOLVED_REASONS = frozenset(
    {
        "document_identity_collision",
        "pdf_cache_object_unavailable",
        "source_metadata_unresolved",
    }
)


class RevisionHistoryV5Error(RuntimeError):
    """Durable history contradicts the current source or its own links."""


@dataclass(frozen=True, slots=True)
class RevisionHistoryCandidateV5:
    source: SourceRecord
    revision: PDFSourceRevisionRow
    temporal_status: TemporalStatusV5
    supersedes_document_id: str | None

    @property
    def document_id(self) -> str:
        return self.source.document_id(self.revision.pdf_sha256)


@dataclass(frozen=True, slots=True)
class UnresolvedRevisionIdentityV5:
    """One exact durable revision identity that could not be materialized."""

    source_id: str
    pdf_sha256: str
    reason_code: UnresolvedRevisionReasonV5

    def __post_init__(self) -> None:
        if (
            _SOURCE_ID.fullmatch(self.source_id) is None
            or _SHA256.fullmatch(self.pdf_sha256) is None
            or self.reason_code not in _UNRESOLVED_REASONS
        ):
            raise ValueError("unresolved revision identity is invalid")


class UnresolvedRevisionLedgerEntryV5(TypedDict):
    source_id: str
    pdf_sha256: str
    reason_codes: list[str]


def canonical_unresolved_revision_ledger_v5(
    entries: Iterable[UnresolvedRevisionIdentityV5],
) -> tuple[UnresolvedRevisionLedgerEntryV5, ...]:
    """Aggregate and order unresolved identities for hashing and metrics."""

    reasons_by_identity: dict[tuple[str, str], set[str]] = defaultdict(set)
    for entry in entries:
        reasons_by_identity[(entry.source_id, entry.pdf_sha256)].add(entry.reason_code)
    return tuple(
        {
            "source_id": source_id,
            "pdf_sha256": pdf_sha256,
            "reason_codes": sorted(reason_codes),
        }
        for (source_id, pdf_sha256), reason_codes in sorted(reasons_by_identity.items())
    )


def unresolved_revision_ledger_sha256_v5(
    ledger: Iterable[UnresolvedRevisionLedgerEntryV5],
) -> str:
    """Bind the canonical unresolved identity ledger to its schema."""

    return canonical_sha256(
        {
            "schema_version": UNRESOLVED_REVISION_LEDGER_SCHEMA,
            "identities": list(ledger),
        }
    )


@dataclass(frozen=True, slots=True)
class RevisionHistoryPlanV5:
    candidates: tuple[RevisionHistoryCandidateV5, ...]
    unresolved_revisions: tuple[UnresolvedRevisionIdentityV5, ...]

    @property
    def unresolved_revision_count(self) -> int:
        return len(self.unresolved_revisions)


def _timestamp(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise RevisionHistoryV5Error(f"historical {field} is invalid") from None
    if parsed.tzinfo is None:
        raise RevisionHistoryV5Error(f"historical {field} is timezone-naive")
    return parsed


def _source_identity(row: PDFSourceRevisionRow) -> tuple[str, str, str, str, str, str, str, str]:
    return (
        row.source_id,
        row.issuer,
        row.product_code,
        row.document_type,
        row.source_url,
        row.source_version,
        row.source_post_id,
        row.discovery_sha256,
    )


def _record_identity(source: SourceRecord) -> tuple[str, str, str, str, str, str, str, str]:
    identity = PDFSourceIdentity.from_source_record(source)
    return (
        identity.source_id,
        identity.issuer,
        identity.product_code,
        identity.document_type,
        identity.source_url,
        identity.source_version,
        identity.source_post_id,
        identity.discovery_sha256,
    )


def plan_revision_history_v5(
    *,
    current_source: SourceRecord,
    current_pdf_sha256: str,
    rows: tuple[PDFSourceRevisionRow, ...],
    known_sources: dict[str, SourceRecord],
) -> RevisionHistoryPlanV5:
    """Plan unique contract identities without guessing missing source fields."""

    if current_source.source_id not in known_sources:
        known_sources = {**known_sources, current_source.source_id: current_source}
    elif known_sources[current_source.source_id].discovery_payload != current_source.discovery_payload:
        raise RevisionHistoryV5Error("current source payload conflicts with snapshot history")
    lineage = (current_source.issuer, current_source.product_code, current_source.document_type)
    if not rows:
        raise RevisionHistoryV5Error("current lineage has no durable PDF revision history")

    row_by_id: dict[int, PDFSourceRevisionRow] = {}
    rows_by_identity: dict[tuple[str, str], list[PDFSourceRevisionRow]] = defaultdict(list)
    source_rows: dict[str, list[PDFSourceRevisionRow]] = defaultdict(list)
    source_static_identity: dict[str, tuple[str, str, str, str, str, str, str, str]] = {}
    for row in rows:
        if row.revision_id in row_by_id:
            raise RevisionHistoryV5Error("historical PDF revision_id is duplicated")
        if (row.issuer, row.product_code, row.document_type) != lineage:
            raise RevisionHistoryV5Error("historical PDF revision crosses a product lineage")
        if row.previous_revision_id is not None and row.previous_revision_id >= row.revision_id:
            raise RevisionHistoryV5Error("historical PDF revision order is not forward-only")
        for field, value in (
            ("source_first_observed_at", row.source_first_observed_at),
            ("source_last_observed_at", row.source_last_observed_at),
            ("revision_first_observed_at", row.revision_first_observed_at),
            ("revision_last_observed_at", row.revision_last_observed_at),
            ("verified_at", row.verified_at),
        ):
            _timestamp(value, field=field)
        if row.superseded_at is not None:
            _timestamp(row.superseded_at, field="superseded_at")
        if row.source_superseded_at is not None:
            _timestamp(row.source_superseded_at, field="source_superseded_at")
        static_identity = _source_identity(row)
        prior_identity = source_static_identity.setdefault(row.source_id, static_identity)
        if prior_identity != static_identity:
            raise RevisionHistoryV5Error("historical source identity changes across byte revisions")
        row_by_id[row.revision_id] = row
        rows_by_identity[(row.source_id, row.pdf_sha256)].append(row)
        source_rows[row.source_id].append(row)

    current_rows = rows_by_identity.get((current_source.source_id, current_pdf_sha256), [])
    if not current_rows or not any(
        row.superseded_at is None and row.source_superseded_at is None for row in current_rows
    ):
        raise RevisionHistoryV5Error("current discovery/PDF is not the active durable revision")

    resolved_sources: dict[str, SourceRecord] = {}
    for source_id, source in known_sources.items():
        if (source.issuer, source.product_code, source.document_type) != lineage:
            continue
        persisted = source_static_identity.get(source_id)
        if persisted is None:
            continue
        if _record_identity(source) != persisted:
            raise RevisionHistoryV5Error("snapshot source payload conflicts with PDF cache history")
        resolved_sources[source_id] = source

    included_identities = {
        contract_identity
        for contract_identity in rows_by_identity
        if contract_identity[0] in resolved_sources
    }
    unresolved_revisions = tuple(
        UnresolvedRevisionIdentityV5(
            source_id=source_id,
            pdf_sha256=pdf_sha256,
            reason_code="source_metadata_unresolved",
        )
        for source_id, pdf_sha256 in sorted(set(rows_by_identity) - included_identities)
    )
    predecessor_candidates: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    for contract_identity, identity_rows in rows_by_identity.items():
        for row in identity_rows:
            if row.previous_revision_id is None:
                continue
            previous = row_by_id.get(row.previous_revision_id)
            if previous is None or previous.source_id != row.source_id:
                raise RevisionHistoryV5Error("historical previous_revision_id is missing or cross-source")
            previous_identity = (previous.source_id, previous.pdf_sha256)
            if previous_identity != contract_identity:
                predecessor_candidates[contract_identity].add(previous_identity)

    # A source transition can prove one predecessor only when the durable
    # successor pointer is unambiguous. Multiple prior source identities are
    # retained without inventing a single supersession edge.
    predecessor_sources: dict[str, set[str]] = defaultdict(set)
    for source_id, source_revision_rows in source_rows.items():
        successor_ids = {row.superseded_by_source_id for row in source_revision_rows}
        if len(successor_ids) != 1:
            raise RevisionHistoryV5Error("historical source successor changes across byte revisions")
        successor_id = next(iter(successor_ids))
        if successor_id is not None:
            predecessor_sources[successor_id].add(source_id)
    for source_id, prior_source_ids in predecessor_sources.items():
        if len(prior_source_ids) != 1 or source_id not in source_rows:
            continue
        predecessor_source_id = next(iter(prior_source_ids))
        target_first = min(source_rows[source_id], key=lambda row: row.revision_id)
        predecessor_last = max(source_rows[predecessor_source_id], key=lambda row: row.revision_id)
        target_identity = (target_first.source_id, target_first.pdf_sha256)
        predecessor_identity = (predecessor_last.source_id, predecessor_last.pdf_sha256)
        if target_identity != predecessor_identity:
            predecessor_candidates[target_identity].add(predecessor_identity)

    planned: list[RevisionHistoryCandidateV5] = []
    for contract_identity in included_identities:
        identity_rows = rows_by_identity[contract_identity]
        representative = max(identity_rows, key=lambda row: row.revision_id)
        if contract_identity == (current_source.source_id, current_pdf_sha256):
            temporal_status: TemporalStatusV5 = "current"
        elif any(
            row.superseded_at is not None or row.source_superseded_at is not None for row in identity_rows
        ):
            temporal_status = "superseded"
        else:
            temporal_status = "ambiguous"
        resolved_predecessors = {
            predecessor
            for predecessor in predecessor_candidates.get(contract_identity, set())
            if predecessor in included_identities
        }
        supersedes_document_id: str | None = None
        if len(resolved_predecessors) == 1:
            predecessor_source_id, predecessor_pdf_sha256 = next(iter(resolved_predecessors))
            supersedes_document_id = resolved_sources[predecessor_source_id].document_id(
                predecessor_pdf_sha256
            )
        planned.append(
            RevisionHistoryCandidateV5(
                source=resolved_sources[contract_identity[0]],
                revision=representative,
                temporal_status=temporal_status,
                supersedes_document_id=supersedes_document_id,
            )
        )
    planned.sort(
        key=lambda candidate: (
            candidate.revision.revision_first_observed_at,
            candidate.revision.revision_id,
            candidate.source.source_id,
            candidate.revision.pdf_sha256,
        )
    )
    if sum(candidate.temporal_status == "current" for candidate in planned) != 1:
        raise RevisionHistoryV5Error("revision plan does not contain exactly one current contract")
    return RevisionHistoryPlanV5(tuple(planned), unresolved_revisions)


__all__ = [
    "REVISION_HISTORY_POLICY_VERSION",
    "RevisionHistoryCandidateV5",
    "RevisionHistoryPlanV5",
    "RevisionHistoryV5Error",
    "TemporalStatusV5",
    "UNRESOLVED_REVISION_LEDGER_SCHEMA",
    "UnresolvedRevisionIdentityV5",
    "UnresolvedRevisionLedgerEntryV5",
    "UnresolvedRevisionReasonV5",
    "canonical_unresolved_revision_ledger_v5",
    "plan_revision_history_v5",
    "unresolved_revision_ledger_sha256_v5",
]
