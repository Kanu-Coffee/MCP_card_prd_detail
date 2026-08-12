from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from cardrag.domain import (
    DocumentIdentity,
    EvidenceIdentity,
    EvidenceSourceSpan,
    Issuer,
    SourceRecord,
    natural_version_key,
)


def _document(issuer: Issuer = Issuer.WOORI, version: str = "9") -> DocumentIdentity:
    return DocumentIdentity(
        issuer=issuer,
        product_code="SAME-CODE",
        document_type="product_description",
        effective_date="2026-08-12",
        version=version,
    )


def test_document_identity_is_strict_frozen_and_issuer_scoped() -> None:
    woori = _document(Issuer.WOORI)
    kb = _document(Issuer.KB)

    assert woori.stable_id.startswith("doc_")
    assert len(woori.stable_id) == len("doc_") + 64
    assert woori.stable_id != kb.stable_id
    assert woori.doc_version_id == woori.stable_id

    with pytest.raises(ValidationError):
        DocumentIdentity(
            issuer="woori",  # type: ignore[arg-type]
            product_code="SAME-CODE",
            document_type="product_description",
            effective_date="2026-08-12",
            version="1",
        )
    with pytest.raises(ValidationError):
        woori.version = "10"  # type: ignore[misc]


def test_document_identity_requires_valid_components() -> None:
    with pytest.raises(ValidationError):
        DocumentIdentity(
            issuer=Issuer.WOORI,
            product_code="",
            document_type="Product Description",
            effective_date="2026-02-30",
            version="1",
            unexpected="rejected",  # type: ignore[call-arg]
        )


def test_natural_version_ordering_is_numeric_not_lexical() -> None:
    assert natural_version_key("v9") < natural_version_key("v10")
    assert natural_version_key("1.2") < natural_version_key("1.10")
    assert _document(version="9").chronological_sort_key < _document(version="10").chronological_sort_key


def test_source_hash_change_creates_a_distinct_immutable_document_version() -> None:
    base = _document()
    first = base.model_copy(update={"source_sha256": "1" * 64})
    second = base.model_copy(update={"source_sha256": "2" * 64})

    assert first.stable_id != second.stable_id


def test_evidence_identity_is_stable_and_rank_independent() -> None:
    document = _document()
    first = EvidenceIdentity.from_text(
        document=document,
        page=3,
        start=10,
        end=28,
        text="전월실적 제외 조건",
    )
    repeated = EvidenceIdentity.from_text(
        document=document,
        page=3,
        start=10,
        end=28,
        text="전월실적 제외 조건",
    )

    assert first.stable_id == repeated.stable_id
    assert first.evidence_id == first.stable_id
    assert _document(Issuer.KB).stable_id not in first.stable_id

    with pytest.raises(ValidationError):
        EvidenceIdentity(
            document=document,
            source_spans=(
                EvidenceSourceSpan(
                    page=1,
                    start=8,
                    end=8,
                    quote_sha256="0" * 64,
                ),
            ),
            text_sha256="0" * 64,
        )


def test_multi_span_evidence_identity_uses_the_ordered_exact_union() -> None:
    first_hash = __import__("hashlib").sha256("혜택 본문".encode()).hexdigest()
    second_hash = __import__("hashlib").sha256("제외 조건".encode()).hexdigest()
    spans = (
        EvidenceSourceSpan(page=1, start=10, end=15, quote_sha256=first_hash),
        EvidenceSourceSpan(page=2, start=3, end=8, quote_sha256=second_hash),
    )
    identity = EvidenceIdentity(
        document=_document(),
        source_spans=spans,
        text_sha256=__import__("hashlib").sha256("혜택 본문\n제외 조건".encode()).hexdigest(),
    )
    changed = EvidenceIdentity(
        document=identity.document,
        source_spans=spans[:1],
        text_sha256=identity.text_sha256,
    )

    assert identity.stable_id != changed.stable_id
    with pytest.raises(ValidationError, match="document order"):
        EvidenceIdentity(
            document=_document(),
            source_spans=tuple(reversed(spans)),
            text_sha256=identity.text_sha256,
        )


def test_source_record_derives_the_same_document_contract() -> None:
    record = SourceRecord(
        issuer=Issuer.SHINHAN,
        product_code="SH-100",
        product_name="신한 테스트 카드",
        effective_date=date(2026, 8, 1),
        source_version="2",
        source_url="https://example.shinhancard.test/disclosures/100",
        source_post_id="post-100",
        file_name="guide.pdf",
        category="credit",
        is_current=True,
        discovered_at=datetime(2026, 8, 12, tzinfo=UTC),
    )

    assert record.document_identity.issuer is Issuer.SHINHAN
    assert record.document_identity.product_code == "SH-100"

    with pytest.raises(ValidationError):
        SourceRecord(
            issuer=Issuer.SHINHAN,
            product_code="SH-100",
            product_name="신한 테스트 카드",
            effective_date=date(2026, 8, 1),
            source_version="2",
            source_url="file:///etc/passwd",
            source_post_id="post-100",
            file_name="../guide.pdf",
            category="credit",
            is_current=True,
            discovered_at=datetime(2026, 8, 12, tzinfo=UTC),
        )


def test_issuer_boundary_reexports_the_single_source_record_contract() -> None:
    from cardrag.issuers.base import SourceRecord as IssuerSourceRecord

    assert IssuerSourceRecord is SourceRecord
    assert IssuerSourceRecord.model_json_schema() == SourceRecord.model_json_schema()
