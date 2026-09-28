"""Tests for post-acquisition corpus diff report generation and fail-closed gate."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import cardrag_worker.cli as cli_module
from cardrag_worker.contracts import SourceRecord
from cardrag_worker.corpus_diff import (
    CORPUS_DIFF_SCHEMA_VERSION,
    CorpusDiffError,
    generate_corpus_diff_report,
)
from cardrag_worker.downloader import DownloadedPDF
from cardrag_worker.state_seed_v122 import StateSeedLedger, StateSeedOCREntry


@dataclass(frozen=True)
class MockAcquiredDocument:
    source: SourceRecord
    pdf: DownloadedPDF
    is_historical: bool = False
    temporal_status: str = "current"
    supersedes_document_id: str | None = None


def _make_source(
    label: str,
    issuer: str = "kb",
    product_code: str = "CARD-1",
    product_name: str = "Card One",
) -> SourceRecord:
    return SourceRecord(
        issuer=issuer,
        product_code=product_code,
        product_name=product_name,
        document_type="product-manual",
        source_url=f"https://example.com/{label}.pdf",
        source_version="2026-01",
        effective_date=datetime(2026, 1, 1, tzinfo=UTC).date(),
        source_post_id=f"post-{label}",
        file_name="guide.pdf",
        category="credit",
        discovered_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _make_pdf(content: bytes, tmp_path: Path) -> DownloadedPDF:
    sha = hashlib.sha256(content).hexdigest()
    path = tmp_path / f"{sha}.pdf"
    path.write_bytes(content)
    return DownloadedPDF(
        path=path,
        sha256=sha,
        size_bytes=len(content),
        page_count=1,
        final_url=f"https://example.com/{sha}.pdf",
    )


def test_corpus_diff_without_seed_ledger(tmp_path: Path) -> None:
    source1 = _make_source("doc1", product_code="P1")
    pdf1 = _make_pdf(b"%PDF-1.4 doc1", tmp_path)
    doc1 = MockAcquiredDocument(source=source1, pdf=pdf1)

    out_path = tmp_path / "reports" / "corpus-diff.json"
    report = generate_corpus_diff_report(
        run_id="run-1",
        acquired_documents=[doc1],
        seed_ledger=None,
        output_path=out_path,
    )

    assert report.schema_version == CORPUS_DIFF_SCHEMA_VERSION
    assert report.counts["final_current"] == 1
    assert report.counts["new_products"] == 1
    assert report.counts["prior_current"] == 0
    assert out_path.is_file()


def test_corpus_diff_full_categorization(tmp_path: Path) -> None:
    # Set up prior documents:
    # doc1: unchanged current
    # doc2: will be revised (predecessor becomes historical, new revision becomes current)
    # doc3: prior historical, stays historical
    source1 = _make_source("doc1", product_code="P1")
    pdf1 = _make_pdf(b"%PDF-1.4 doc1-bytes", tmp_path)
    doc_id1 = source1.document_id(pdf1.sha256)

    source2 = _make_source("doc2", product_code="P2")
    pdf2_old = _make_pdf(b"%PDF-1.4 doc2-old-bytes", tmp_path)
    doc_id2_old = source2.document_id(pdf2_old.sha256)

    source3 = _make_source("doc3", product_code="P3")
    pdf3 = _make_pdf(b"%PDF-1.4 doc3-bytes", tmp_path)
    doc_id3 = source3.document_id(pdf3.sha256)

    entries = {
        doc_id1: StateSeedOCREntry(
            document_id=doc_id1,
            issuer="kb",
            source_id=source1.source_id,
            pdf_sha256=pdf1.sha256,
            pdf_size_bytes=pdf1.size_bytes,
            page_count=1,
            ocr_sha256="a" * 64,
            ocr_size_bytes=100,
            kind="native",
            reuse_key="rk-1",
            source_ocr_path=tmp_path / "ocr1.md",
            source_manifest_path=None,
            manifest_bytes=None,
            ready_bytes=None,
            manifest_sha256=None,
            ready_sha256=None,
        ),
        doc_id2_old: StateSeedOCREntry(
            document_id=doc_id2_old,
            issuer="kb",
            source_id=source2.source_id,
            pdf_sha256=pdf2_old.sha256,
            pdf_size_bytes=pdf2_old.size_bytes,
            page_count=1,
            ocr_sha256="b" * 64,
            ocr_size_bytes=100,
            kind="native",
            reuse_key="rk-2",
            source_ocr_path=tmp_path / "ocr2.md",
            source_manifest_path=None,
            manifest_bytes=None,
            ready_bytes=None,
            manifest_sha256=None,
            ready_sha256=None,
        ),
        doc_id3: StateSeedOCREntry(
            document_id=doc_id3,
            issuer="kb",
            source_id=source3.source_id,
            pdf_sha256=pdf3.sha256,
            pdf_size_bytes=pdf3.size_bytes,
            page_count=1,
            ocr_sha256="c" * 64,
            ocr_size_bytes=100,
            kind="adopted",
            reuse_key="rk-3",
            source_ocr_path=tmp_path / "ocr3.md",
            source_manifest_path=None,
            manifest_bytes=None,
            ready_bytes=None,
            manifest_sha256=None,
            ready_sha256=None,
        ),
    }

    seed_ledger = StateSeedLedger(
        generation_id="gen-test",
        run_id="run-seed",
        corpus_sha256="f" * 64,
        contract_sha256="e" * 64,
        entries_by_doc_id=entries,
        seed_pdf_shas=frozenset({pdf1.sha256, pdf2_old.sha256, pdf3.sha256}),
        seed_doc_ids=frozenset({doc_id1, doc_id2_old, doc_id3}),
        prior_current_doc_ids=frozenset({doc_id1, doc_id2_old}),
        prior_historical_doc_ids=frozenset({doc_id3}),
    )

    # Now acquire:
    # 1. doc1: unchanged
    # 2. doc2_new: new bytes for source2, supersedes doc_id2_old
    # 3. doc2_old: retained as historical!
    # 4. doc3: retained as historical!
    # 5. doc4: new product!
    pdf2_new = _make_pdf(b"%PDF-1.4 doc2-NEW-bytes", tmp_path)
    doc_id2_new = source2.document_id(pdf2_new.sha256)

    source4 = _make_source("doc4", product_code="P4-NEW")
    pdf4 = _make_pdf(b"%PDF-1.4 doc4-brand-new", tmp_path)
    doc_id4 = source4.document_id(pdf4.sha256)

    acquired = [
        MockAcquiredDocument(source=source1, pdf=pdf1, is_historical=False),
        MockAcquiredDocument(
            source=source2,
            pdf=pdf2_new,
            is_historical=False,
            supersedes_document_id=doc_id2_old,
        ),
        MockAcquiredDocument(
            source=source2,
            pdf=pdf2_old,
            is_historical=True,
            temporal_status="historical",
        ),
        MockAcquiredDocument(
            source=source3,
            pdf=pdf3,
            is_historical=True,
            temporal_status="historical",
        ),
        MockAcquiredDocument(source=source4, pdf=pdf4, is_historical=False),
    ]

    report = generate_corpus_diff_report(
        run_id="run-new",
        acquired_documents=acquired,
        seed_ledger=seed_ledger,
    )

    assert report.counts["prior_current"] == 2
    assert report.counts["prior_historical"] == 1
    assert report.counts["unchanged"] == 1
    assert report.unchanged == (doc_id1,)
    assert report.counts["same_source_byte_revision"] == 1
    assert report.same_source_byte_revisions[0]["document_id"] == doc_id2_new
    assert report.same_source_byte_revisions[0]["previous_document_id"] == doc_id2_old
    assert report.counts["new_products"] == 1
    assert report.new_products[0]["document_id"] == doc_id4
    assert report.counts["replaced_predecessors"] == 1
    assert report.counts["final_current"] == 3
    assert report.counts["final_historical"] == 2
    assert report.counts["final_corpus"] == 5
    assert report.counts["missing_unjustified"] == 0


def test_corpus_diff_fails_closed_when_seed_document_missing(tmp_path: Path) -> None:
    source1 = _make_source("doc1", product_code="P1")
    pdf1 = _make_pdf(b"%PDF-1.4 doc1", tmp_path)
    doc_id1 = source1.document_id(pdf1.sha256)

    seed_ledger = StateSeedLedger(
        generation_id="gen-test",
        run_id="run-seed",
        corpus_sha256="f" * 64,
        contract_sha256="e" * 64,
        entries_by_doc_id={
            doc_id1: StateSeedOCREntry(
                document_id=doc_id1,
                issuer="kb",
                source_id=source1.source_id,
                pdf_sha256=pdf1.sha256,
                pdf_size_bytes=pdf1.size_bytes,
                page_count=1,
                ocr_sha256="a" * 64,
                ocr_size_bytes=100,
                kind="native",
                reuse_key="rk-1",
                source_ocr_path=tmp_path / "ocr1.md",
                source_manifest_path=None,
                manifest_bytes=None,
                ready_bytes=None,
                manifest_sha256=None,
                ready_sha256=None,
            )
        },
        seed_pdf_shas=frozenset({pdf1.sha256}),
        seed_doc_ids=frozenset({doc_id1}),
        prior_current_doc_ids=frozenset({doc_id1}),
        prior_historical_doc_ids=frozenset(),
    )

    # Empty acquired list: doc1 disappeared without justification!
    with pytest.raises(CorpusDiffError, match="disappeared without justification"):
        generate_corpus_diff_report(
            run_id="run-fail",
            acquired_documents=[],
            seed_ledger=seed_ledger,
            fail_on_missing=True,
        )


def test_corpus_diff_failure_persists_canonical_report_before_raising(tmp_path: Path) -> None:
    source1 = _make_source("doc1", product_code="P1")
    pdf1 = _make_pdf(b"%PDF-1.4 doc1", tmp_path)
    doc_id1 = source1.document_id(pdf1.sha256)

    seed_ledger = StateSeedLedger(
        generation_id="gen-test-persists",
        run_id="run-seed",
        corpus_sha256="f" * 64,
        contract_sha256="e" * 64,
        entries_by_doc_id={
            doc_id1: StateSeedOCREntry(
                document_id=doc_id1,
                issuer="kb",
                source_id=source1.source_id,
                pdf_sha256=pdf1.sha256,
                pdf_size_bytes=pdf1.size_bytes,
                page_count=1,
                ocr_sha256="a" * 64,
                ocr_size_bytes=100,
                kind="native",
                reuse_key="rk-1",
                source_ocr_path=tmp_path / "ocr1.md",
                source_manifest_path=None,
                manifest_bytes=None,
                ready_bytes=None,
                manifest_sha256=None,
                ready_sha256=None,
            )
        },
        seed_pdf_shas=frozenset({pdf1.sha256}),
        seed_doc_ids=frozenset({doc_id1}),
        prior_current_doc_ids=frozenset({doc_id1}),
        prior_historical_doc_ids=frozenset(),
    )

    out_path = tmp_path / "reports" / "corpus-diff.json"
    with pytest.raises(CorpusDiffError) as exc_info:
        generate_corpus_diff_report(
            run_id="run-disappear",
            acquired_documents=[],
            seed_ledger=seed_ledger,
            output_path=out_path,
            fail_on_missing=True,
        )

    assert exc_info.value.missing_count == 1
    assert exc_info.value.reason_code == "corpus_diff_missing"
    assert exc_info.value.report == "runs/run-disappear/reports/corpus-diff.json"

    # Crucial assertion: corpus-diff.json MUST exist on disk before raising!
    assert out_path.is_file()
    saved_report = json.loads(out_path.read_text())
    assert saved_report["schema_version"] == CORPUS_DIFF_SCHEMA_VERSION
    assert saved_report["counts"]["missing_unjustified"] == 1
    assert saved_report["counts"]["prior_current"] == 1
    assert doc_id1 in saved_report["missing_unjustified"]


def test_reduced_e2e_corpus_diff_fixture(tmp_path: Path) -> None:
    # 1. Unchanged current (doc1)
    source1 = _make_source("doc1", product_code="P1")
    pdf1 = _make_pdf(b"%PDF-1.4 doc1-bytes", tmp_path)
    doc_id1 = source1.document_id(pdf1.sha256)

    # 2. Prior historical maintained (doc2_hist)
    source2 = _make_source("doc2_hist", product_code="P2")
    pdf2_hist = _make_pdf(b"%PDF-1.4 doc2-hist-bytes", tmp_path)
    doc_id2_hist = source2.document_id(pdf2_hist.sha256)

    # 3. Same-source byte revision: predecessor (doc3_old) -> new revision (doc3_new)
    source3 = _make_source("doc3", product_code="P3")
    pdf3_old = _make_pdf(b"%PDF-1.4 doc3-old-bytes", tmp_path)
    doc_id3_old = source3.document_id(pdf3_old.sha256)
    pdf3_new = _make_pdf(b"%PDF-1.4 doc3-new-bytes", tmp_path)
    doc_id3_new = source3.document_id(pdf3_new.sha256)

    # 4. Successor source: predecessor (doc4_old) -> successor source (doc4_succ)
    source4_old = _make_source("doc4_old", product_code="P4-OLD")
    pdf4_old = _make_pdf(b"%PDF-1.4 doc4-old-bytes", tmp_path)
    doc_id4_old = source4_old.document_id(pdf4_old.sha256)
    source4_succ = _make_source("doc4_succ", product_code="P4-NEW")
    pdf4_succ = _make_pdf(b"%PDF-1.4 doc4-succ-bytes", tmp_path)
    doc_id4_succ = source4_succ.document_id(pdf4_succ.sha256)

    # 5. New product: doc5_new (brand new, no predecessor)
    source5 = _make_source("doc5", product_code="P5-BRAND-NEW")
    pdf5 = _make_pdf(b"%PDF-1.4 doc5-brand-new", tmp_path)
    doc_id5 = source5.document_id(pdf5.sha256)

    entries = {
        doc_id1: StateSeedOCREntry(
            document_id=doc_id1,
            issuer="kb",
            source_id=source1.source_id,
            pdf_sha256=pdf1.sha256,
            pdf_size_bytes=pdf1.size_bytes,
            page_count=1,
            ocr_sha256="1" * 64,
            ocr_size_bytes=100,
            kind="native",
            reuse_key="rk-1",
            source_ocr_path=tmp_path / "ocr1.md",
            source_manifest_path=None,
            manifest_bytes=None,
            ready_bytes=None,
            manifest_sha256=None,
            ready_sha256=None,
        ),
        doc_id2_hist: StateSeedOCREntry(
            document_id=doc_id2_hist,
            issuer="kb",
            source_id=source2.source_id,
            pdf_sha256=pdf2_hist.sha256,
            pdf_size_bytes=pdf2_hist.size_bytes,
            page_count=1,
            ocr_sha256="2" * 64,
            ocr_size_bytes=100,
            kind="adopted",
            reuse_key="rk-2",
            source_ocr_path=tmp_path / "ocr2.md",
            source_manifest_path=None,
            manifest_bytes=None,
            ready_bytes=None,
            manifest_sha256=None,
            ready_sha256=None,
        ),
        doc_id3_old: StateSeedOCREntry(
            document_id=doc_id3_old,
            issuer="kb",
            source_id=source3.source_id,
            pdf_sha256=pdf3_old.sha256,
            pdf_size_bytes=pdf3_old.size_bytes,
            page_count=1,
            ocr_sha256="3" * 64,
            ocr_size_bytes=100,
            kind="native",
            reuse_key="rk-3",
            source_ocr_path=tmp_path / "ocr3.md",
            source_manifest_path=None,
            manifest_bytes=None,
            ready_bytes=None,
            manifest_sha256=None,
            ready_sha256=None,
        ),
        doc_id4_old: StateSeedOCREntry(
            document_id=doc_id4_old,
            issuer="kb",
            source_id=source4_old.source_id,
            pdf_sha256=pdf4_old.sha256,
            pdf_size_bytes=pdf4_old.size_bytes,
            page_count=1,
            ocr_sha256="4" * 64,
            ocr_size_bytes=100,
            kind="native",
            reuse_key="rk-4",
            source_ocr_path=tmp_path / "ocr4.md",
            source_manifest_path=None,
            manifest_bytes=None,
            ready_bytes=None,
            manifest_sha256=None,
            ready_sha256=None,
        ),
    }

    seed_ledger = StateSeedLedger(
        generation_id="gen-test-e2e",
        run_id="run-seed",
        corpus_sha256="f" * 64,
        contract_sha256="e" * 64,
        entries_by_doc_id=entries,
        seed_pdf_shas=frozenset({pdf1.sha256, pdf2_hist.sha256, pdf3_old.sha256, pdf4_old.sha256}),
        seed_doc_ids=frozenset({doc_id1, doc_id2_hist, doc_id3_old, doc_id4_old}),
        prior_current_doc_ids=frozenset({doc_id1, doc_id3_old, doc_id4_old}),
        prior_historical_doc_ids=frozenset({doc_id2_hist}),
        source_records={
            source1.source_id: source1,
            source2.source_id: source2,
            source3.source_id: source3,
            source4_old.source_id: source4_old,
        },
    )

    acquired = [
        # 1. Unchanged current
        MockAcquiredDocument(source=source1, pdf=pdf1, is_historical=False),
        # 2. Prior historical maintained
        MockAcquiredDocument(source=source2, pdf=pdf2_hist, is_historical=True, temporal_status="historical"),
        # 3. Same-source byte revision
        MockAcquiredDocument(
            source=source3, pdf=pdf3_new, is_historical=False, supersedes_document_id=doc_id3_old
        ),
        MockAcquiredDocument(source=source3, pdf=pdf3_old, is_historical=True, temporal_status="historical"),
        # 4. Successor source
        MockAcquiredDocument(
            source=source4_succ, pdf=pdf4_succ, is_historical=False, supersedes_document_id=doc_id4_old
        ),
        MockAcquiredDocument(
            source=source4_old, pdf=pdf4_old, is_historical=True, temporal_status="historical"
        ),
        # 5. New product
        MockAcquiredDocument(source=source5, pdf=pdf5, is_historical=False),
    ]

    out_path = tmp_path / "reports" / "corpus-diff.json"
    report = generate_corpus_diff_report(
        run_id="run-e2e",
        acquired_documents=acquired,
        seed_ledger=seed_ledger,
        output_path=out_path,
        fail_on_missing=True,
    )

    assert report.counts["prior_current"] == 3
    assert report.counts["prior_historical"] == 1
    assert report.counts["unchanged"] == 1
    assert report.unchanged == (doc_id1,)
    assert report.counts["same_source_byte_revision"] == 1
    assert report.same_source_byte_revisions[0]["document_id"] == doc_id3_new
    assert report.same_source_byte_revisions[0]["previous_document_id"] == doc_id3_old
    assert report.counts["successor_sources"] == 1
    assert report.successor_sources[0]["document_id"] == doc_id4_succ
    assert report.successor_sources[0]["previous_document_id"] == doc_id4_old
    assert report.counts["new_products"] == 1
    assert report.new_products[0]["document_id"] == doc_id5
    assert len(report.historical_maintained) == 3
    assert doc_id2_hist in report.historical_maintained
    assert report.counts["final_current"] == 4
    assert report.counts["final_historical"] == 3
    assert report.counts["final_corpus"] == 7
    assert report.counts["missing_unjustified"] == 0
    assert out_path.is_file()


def test_cli_typed_corpus_diff_payload_and_redaction(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    secret_token = "TOP_SECRET_AUTH_KEY_XYZ123"  # noqa: S105

    async def fail_with_corpus_diff(*args: Any, **kwargs: Any) -> Any:
        raise CorpusDiffError(
            run_id="run-test-diff-err",
            missing_count=3,
            report="runs/run-test-diff-err/reports/corpus-diff.json",
            sample=("doc_111", "doc_222", "doc_333"),
        )

    monkeypatch.setattr(cli_module, "_run_with_signal_shutdown", fail_with_corpus_diff)
    runner = CliRunner()
    result = runner.invoke(cli_module.app, ["run"])
    assert result.exit_code == 1

    payload = json.loads(result.stdout)
    assert payload["status"] == "failed"
    assert payload["reason_code"] == "corpus_diff_missing"
    assert payload["run_id"] == "run-test-diff-err"
    assert payload["missing_count"] == 3
    assert payload["report"] == "runs/run-test-diff-err/reports/corpus-diff.json"
    assert payload["sample"] == ["doc_111", "doc_222", "doc_333"]
    assert secret_token not in result.stdout
    assert "https://" not in result.stdout
