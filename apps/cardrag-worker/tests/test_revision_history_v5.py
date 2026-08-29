from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
from cardrag_core import canonical_sha256

from cardrag_worker.contracts import SourceRecord
from cardrag_worker.pdf_cache import PDFSourceIdentity
from cardrag_worker.revision_history_v5 import (
    UNRESOLVED_REVISION_LEDGER_SCHEMA,
    RevisionHistoryV5Error,
    UnresolvedRevisionIdentityV5,
    canonical_unresolved_revision_ledger_v5,
    plan_revision_history_v5,
    unresolved_revision_ledger_sha256_v5,
)
from cardrag_worker.state import PDFSourceRevisionRow


def _source(*, version: str, effective_date: date) -> SourceRecord:
    return SourceRecord(
        issuer="kb",
        product_code="CARD-001",
        product_name="테스트 카드",
        effective_date=effective_date,
        source_version=version,
        source_url=f"https://cards.example/{version}.pdf",
        source_post_id=f"post-{version}",
        file_name=f"{version}.pdf",
        category="credit",
        discovered_at=datetime(2026, 8, 29, tzinfo=UTC),
    )


def _row(
    revision_id: int,
    source: SourceRecord,
    pdf_sha256: str,
    *,
    previous_revision_id: int | None = None,
    superseded_at: str | None = None,
    superseded_by_source_id: str | None = None,
    source_superseded_at: str | None = None,
) -> PDFSourceRevisionRow:
    identity = PDFSourceIdentity.from_source_record(source)
    observed = f"2026-08-{revision_id:02d}T00:00:00+00:00"
    return PDFSourceRevisionRow(
        revision_id=revision_id,
        previous_revision_id=previous_revision_id,
        source_id=identity.source_id,
        issuer=identity.issuer,
        product_code=identity.product_code,
        document_type=identity.document_type,
        source_url=identity.source_url,
        source_version=identity.source_version,
        source_post_id=identity.source_post_id,
        discovery_sha256=identity.discovery_sha256,
        pdf_sha256=pdf_sha256,
        pdf_size_bytes=1024 + revision_id,
        page_count=1,
        relative_path=f"objects/sha256/{pdf_sha256[:2]}/{pdf_sha256}",
        final_url=source.source_url,
        etag=None,
        last_modified=None,
        source_first_observed_at=observed,
        source_last_observed_at=observed,
        revision_first_observed_at=observed,
        revision_last_observed_at=observed,
        verified_at=observed,
        superseded_at=superseded_at,
        superseded_by_source_id=superseded_by_source_id,
        source_superseded_at=source_superseded_at,
    )


def test_same_source_pdf_bytes_form_current_and_superseded_chain() -> None:
    source = _source(version="v1", effective_date=date(2026, 1, 1))
    first = _row(1, source, "a" * 64, superseded_at="2026-08-02T00:00:00+00:00")
    current = _row(2, source, "b" * 64, previous_revision_id=1)

    plan = plan_revision_history_v5(
        current_source=source,
        current_pdf_sha256=current.pdf_sha256,
        rows=(first, current),
        known_sources={source.source_id: source},
    )

    assert plan.unresolved_revision_count == 0
    assert [candidate.temporal_status for candidate in plan.candidates] == ["superseded", "current"]
    assert plan.candidates[1].supersedes_document_id == source.document_id(first.pdf_sha256)


def test_snapshot_bound_source_transition_links_distinct_contract_revisions() -> None:
    old = _source(version="v1", effective_date=date(2025, 1, 1))
    current = _source(version="v2", effective_date=date(2026, 1, 1))
    old_row = _row(
        1,
        old,
        "a" * 64,
        superseded_by_source_id=current.source_id,
        source_superseded_at="2026-08-02T00:00:00+00:00",
    )
    current_row = _row(2, current, "b" * 64)

    plan = plan_revision_history_v5(
        current_source=current,
        current_pdf_sha256=current_row.pdf_sha256,
        rows=(old_row, current_row),
        known_sources={old.source_id: old, current.source_id: current},
    )

    assert [candidate.source.source_version for candidate in plan.candidates] == ["v1", "v2"]
    assert [candidate.temporal_status for candidate in plan.candidates] == ["superseded", "current"]
    assert plan.candidates[1].supersedes_document_id == old.document_id(old_row.pdf_sha256)


def test_missing_historical_snapshot_is_counted_and_never_fabricated() -> None:
    old = _source(version="v1", effective_date=date(2025, 1, 1))
    current = _source(version="v2", effective_date=date(2026, 1, 1))
    old_row = _row(
        1,
        old,
        "a" * 64,
        superseded_by_source_id=current.source_id,
        source_superseded_at="2026-08-02T00:00:00+00:00",
    )
    current_row = _row(2, current, "b" * 64)

    plan = plan_revision_history_v5(
        current_source=current,
        current_pdf_sha256=current_row.pdf_sha256,
        rows=(old_row, current_row),
        known_sources={current.source_id: current},
    )

    assert plan.unresolved_revision_count == 1
    assert plan.unresolved_revisions == (
        UnresolvedRevisionIdentityV5(
            source_id=old.source_id,
            pdf_sha256=old_row.pdf_sha256,
            reason_code="source_metadata_unresolved",
        ),
    )
    assert [
        (candidate.source.source_version, candidate.temporal_status) for candidate in plan.candidates
    ] == [("v2", "current")]
    assert plan.candidates[0].supersedes_document_id is None

    ledger = canonical_unresolved_revision_ledger_v5(
        (
            *plan.unresolved_revisions,
            UnresolvedRevisionIdentityV5(
                source_id=old.source_id,
                pdf_sha256=old_row.pdf_sha256,
                reason_code="pdf_cache_object_unavailable",
            ),
        )
    )
    assert ledger == (
        {
            "source_id": old.source_id,
            "pdf_sha256": old_row.pdf_sha256,
            "reason_codes": ["pdf_cache_object_unavailable", "source_metadata_unresolved"],
        },
    )
    assert unresolved_revision_ledger_sha256_v5(ledger) == canonical_sha256(
        {"schema_version": UNRESOLVED_REVISION_LEDGER_SCHEMA, "identities": list(ledger)}
    )


def test_noncurrent_active_revision_is_ambiguous_not_guessed_superseded() -> None:
    stale = _source(version="v1", effective_date=date(2025, 1, 1))
    current = _source(version="v2", effective_date=date(2026, 1, 1))
    stale_row = _row(1, stale, "a" * 64)
    current_row = _row(2, current, "b" * 64)

    plan = plan_revision_history_v5(
        current_source=current,
        current_pdf_sha256=current_row.pdf_sha256,
        rows=(stale_row, current_row),
        known_sources={stale.source_id: stale, current.source_id: current},
    )

    assert [candidate.temporal_status for candidate in plan.candidates] == ["ambiguous", "current"]


def test_snapshot_payload_conflict_with_durable_source_fails_closed() -> None:
    source = _source(version="v1", effective_date=date(2026, 1, 1))
    current = _row(1, source, "a" * 64)
    conflicting = replace(source, source_url="https://cards.example/conflict.pdf")

    with pytest.raises(RevisionHistoryV5Error, match="conflicts"):
        plan_revision_history_v5(
            current_source=source,
            current_pdf_sha256=current.pdf_sha256,
            rows=(current,),
            known_sources={source.source_id: conflicting},
        )
