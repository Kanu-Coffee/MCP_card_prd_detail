from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import fitz  # type: ignore[import-untyped]
import pytest

from cardrag.domain import Issuer
from cardrag.pipeline.ocr import render_pdf
from cardrag.pipeline.structure import SectionType, extract_structure
from cardrag.quality import (
    GoldQuery,
    RankedEvidence,
    evaluate_ocr,
    evaluate_retrieval,
    evaluate_structure,
)
from scripts.run_fixture_quality import run as run_fixture_quality

GOLD_SET = Path(__file__).parents[1] / "fixtures/gold/gold_set.v1.json"


def _fixture() -> dict[str, object]:
    return json.loads(GOLD_SET.read_text(encoding="utf-8"))


def test_three_issuer_gold_baseline_passes_ocr_and_structure_gates() -> None:
    fixture = _fixture()
    documents = fixture["documents"]
    assert isinstance(documents, list)
    assert {document["issuer"] for document in documents} == {"woori", "kb", "shinhan"}
    assert {document["source_version"] for document in documents if document["issuer"] == "kb"} == {
        "v9",
        "v10",
    }

    for document in documents:
        canonical = document["ocr"]
        assert isinstance(canonical, str)
        assert evaluate_ocr(canonical, canonical).status == "passed"
        structured = extract_structure(str(document["key"]), canonical)
        expected_sections = tuple(SectionType(value) for value in document["expected_section_types"])
        result = evaluate_structure(
            canonical,
            structured,
            expected_section_types=expected_sections,
        )
        assert result.status == "passed", (document["key"], result)


def test_ocr_gate_never_averages_away_numeric_or_negation_change() -> None:
    expected = "## Page 1\n\n전월 이용실적 30만원 이상이며 세금은 실적에서 제외됩니다.\n"
    changed = expected.replace("30만원", "80만원").replace("제외", "포함")

    result = evaluate_ocr(expected, changed)

    assert result.status == "failed"
    assert result.critical_error_count >= 2
    assert "30만원" in result.missing_critical_tokens
    assert "제외" in result.missing_critical_tokens


def test_retrieval_gate_measures_ranking_and_pre_filter_accuracy() -> None:
    gold = GoldQuery(
        query_id="q1",
        query="할인",
        relevant_evidence_ids=("right",),
        issuer=Issuer.WOORI,
        product_code="W-1",
        source_version="v1",
        section_type="benefit",
        critical=True,
    )
    right = RankedEvidence(
        evidence_id="right",
        issuer=Issuer.WOORI,
        product_code="W-1",
        source_version="v1",
        section_type="benefit",
    )
    passed = evaluate_retrieval((gold,), {"q1": (right,)})
    assert passed.status == "passed"
    assert passed.mean_reciprocal_rank == 1.0

    collision = right.model_copy(update={"issuer": Issuer.KB})
    failed = evaluate_retrieval((gold,), {"q1": (collision,)})
    assert failed.status == "failed"
    assert failed.filter_accuracy < 1.0
    assert failed.issuer_collision_count == 1


def test_fixture_gate_observes_real_pipeline_rankings_not_expected_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The gold file contains relevance anchors, not a precomputed observed
    # ranking. A real fake-OCR/chunk/embed/HybridSearch run must produce it.
    fixture = _fixture()
    assert "rankings" not in fixture
    output = tmp_path / "passed.json"
    assert run_fixture_quality(GOLD_SET, output) == "passed"
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["retrieval"]["query_count"] == 6
    assert report["retrieval"]["mean_reciprocal_rank"] < 1.0

    async def no_hits(*_: object, **__: object) -> SimpleNamespace:
        return SimpleNamespace(hits=())

    from scripts import run_fixture_quality as harness

    # If the actual search result is empty, replaying the expected ranking
    # would still pass. The gate must instead fail.
    monkeypatch.setattr(harness.HybridSearchEngine, "search", no_hits)
    failed_output = tmp_path / "failed.json"
    assert run_fixture_quality(GOLD_SET, failed_output) == "failed"
    failed = json.loads(failed_output.read_text(encoding="utf-8"))
    assert failed["retrieval"]["failed_query_ids"]


def _write_synthetic_pdf(path: Path, *, image_only: bool) -> None:
    source = fitz.open()
    for page_number in range(1, 3):
        page = source.new_page(width=595, height=842)
        page.insert_text(
            (48, 72),
            f"SYNTHETIC CARD DISCLOSURE PAGE {page_number}\nANNUAL FEE 12000 KRW\nLIMIT 5000 KRW",
            fontsize=12,
        )
    if not image_only:
        source.save(path)
        source.close()
        return
    target = fitz.open()
    for page in source:
        pixmap = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
        output_page = target.new_page(width=595, height=842)
        output_page.insert_image(output_page.rect, stream=pixmap.tobytes("png"))
    target.save(path)
    target.close()
    source.close()


def test_license_safe_gold_pdfs_cover_text_native_and_image_oriented_layouts(tmp_path: Path) -> None:
    for image_only in (False, True):
        pdf_path = tmp_path / f"gold-{'image' if image_only else 'text'}.pdf"
        _write_synthetic_pdf(pdf_path, image_only=image_only)
        rendered = render_pdf(pdf_path, tmp_path / f"rendered-{image_only}", scale=1.0)

        assert len(rendered.page_images) == 2
        assert all(page.stat().st_size > 0 for page in rendered.page_images)
        with fitz.open(pdf_path) as document:
            assert document.page_count == 2
            assert bool(document[0].get_text().strip()) is (not image_only)
