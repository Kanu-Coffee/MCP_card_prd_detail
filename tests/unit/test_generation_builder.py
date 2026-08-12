from __future__ import annotations

from datetime import UTC, datetime
from importlib.resources import files as resource_files

import pytest
from pydantic import ValidationError

import cardrag.generation_builder as generation_builder_module
from cardrag.generation_builder import (
    FIXTURE_GATE_GOLD_SHA256,
    FIXTURE_GATE_REPORT_SHA256,
    GenerationBuilder,
    QualityGateReport,
    RetrievalGateReport,
)


def _quality(**updates: object) -> QualityGateReport:
    values: dict[str, object] = {
        "generation_id": "gen-20260812T000000Z-aaaaaaaaaaaa",
        "evaluated_at": datetime(2026, 8, 12, tzinfo=UTC),
        "gold_set_id": "bound-gold",
        "gold_set_sha256": FIXTURE_GATE_GOLD_SHA256,
        "document_count": 3,
        "page_count": 6,
        "page_coverage": 1.0,
        "critical_numeric_errors": 0,
        "critical_negation_errors": 0,
        "structure_span_accuracy": 1.0,
        "taxonomy_recall": 0.95,
        "critical_token_recall": 1.0,
        "regression_schema_version": "cardrag-quality-evaluation.v1",
        "regression_fixture_set": "synthetic-three-issuer-v1",
        "regression_report_sha256": FIXTURE_GATE_REPORT_SHA256,
        "regression_character_accuracy": 0.995,
        "regression_page_coverage": 1.0,
        "regression_page_order_exact": True,
        "regression_critical_token_recall": 1.0,
        "regression_source_span_accuracy": 1.0,
        "regression_taxonomy_recall": 0.95,
        "regression_critical_error_count": 0,
        "status": "passed",
    }
    values.update(updates)
    return QualityGateReport.model_validate(values)


def _retrieval(**updates: object) -> RetrievalGateReport:
    values: dict[str, object] = {
        "generation_id": "gen-20260812T000000Z-aaaaaaaaaaaa",
        "evaluated_at": datetime(2026, 8, 12, tzinfo=UTC),
        "query_set_id": "gold-queries",
        "query_set_sha256": FIXTURE_GATE_GOLD_SHA256,
        "query_count": 6,
        "recall_at_10": 0.95,
        "critical_recall_at_10": 1.0,
        "mean_reciprocal_rank": 0.90,
        "ndcg_at_10": 0.90,
        "filter_accuracy": 1.0,
        "issuer_collisions": 0,
        "latency_p95_ms": 30_000.0,
        "resource_peak_bytes": 1,
        "regression_schema_version": "cardrag-quality-evaluation.v1",
        "regression_fixture_set": "synthetic-three-issuer-v1",
        "regression_report_sha256": FIXTURE_GATE_REPORT_SHA256,
        "candidate_probe_sha256": "a" * 64,
        "candidate_probe_recall_at_10": 1.0,
        "candidate_probe_filter_accuracy": 1.0,
        "status": "passed",
    }
    values.update(updates)
    return RetrievalGateReport.model_validate(values)


def test_quality_report_enforces_adr_thresholds_and_fixture_provenance() -> None:
    assert _quality().status == "passed"

    with pytest.raises(ValidationError, match="status differs"):
        _quality(structure_span_accuracy=0.999)
    with pytest.raises(ValidationError, match="status differs"):
        _quality(regression_report_sha256="0" * 64)
    with pytest.raises(ValidationError, match="status differs"):
        _quality(critical_negation_errors=1)


def test_retrieval_report_enforces_recall_mrr_ndcg_filter_and_sanity() -> None:
    assert _retrieval().status == "passed"

    for field, value in (
        ("recall_at_10", 0.949),
        ("critical_recall_at_10", 0.999),
        ("mean_reciprocal_rank", 0.899),
        ("ndcg_at_10", 0.899),
        ("filter_accuracy", 0.999),
        ("candidate_probe_recall_at_10", 0.9),
    ):
        with pytest.raises(ValidationError, match="status differs"):
            _retrieval(**{field: value})


def test_packaged_fixture_admission_missing_or_tampered_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = resource_files("cardrag.resources").joinpath("fixture-gate.json").read_bytes()

    class Resource:
        def __init__(self, body: bytes | None) -> None:
            self.body = body

        def joinpath(self, _: str) -> Resource:
            return self

        def read_bytes(self) -> bytes:
            if self.body is None:
                raise FileNotFoundError
            return self.body

    monkeypatch.setattr(generation_builder_module, "files", lambda _: Resource(None))
    with pytest.raises(ValueError, match="missing"):
        GenerationBuilder._load_fixture_gate()

    monkeypatch.setattr(generation_builder_module, "files", lambda _: Resource(original + b"\n"))
    with pytest.raises(ValueError, match="modified"):
        GenerationBuilder._load_fixture_gate()
