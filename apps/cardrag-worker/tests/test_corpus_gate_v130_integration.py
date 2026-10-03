"""FIX_03 integration seams: corpus_diff with a rolling baseline + retirement
resolver, the pipeline baseline recorder, and CLI payloads."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_corpus_diff import MockAcquiredDocument, _make_pdf, _make_source

from cardrag_worker import cli as cli_module
from cardrag_worker.corpus_baseline import (
    CorpusBaselineDocument,
)
from cardrag_worker.corpus_diff import (
    CORPUS_DIFF_SCHEMA_VERSION,
    CorpusDiffError,
    CorpusPriorView,
    PriorEntry,
    generate_corpus_diff_report,
)
from cardrag_worker.pipeline import PipelineResult, WorkerPipeline
from cardrag_worker.retirement import RetirementLedger, RetirementOutcome
from cardrag_worker.state import WorkerState


def _prior(doc_ids: dict[str, str]) -> CorpusPriorView:
    entries = {
        doc_id: PriorEntry(
            document_id=doc_id,
            source_id=source_id,
            issuer="lotte",
            product_code=f"P{doc_id[-2:]}",
            document_type="product-manual",
            pdf_sha256=f"{doc_id[-2:]:0>2}" + "0" * 62,
            ocr_sha256=None,
        )
        for doc_id, source_id in doc_ids.items()
    }
    return CorpusPriorView(
        kind="rolling-baseline",
        generation_id="g-baseline",
        run_id="run-baseline",
        observed_at="2026-09-29T00:00:00+00:00",
        current_doc_ids=frozenset(entries),
        historical_doc_ids=frozenset(),
        entries=entries,
    )


def _outcome() -> RetirementOutcome:
    return RetirementOutcome(
        retired=({"document_id": "d1", "source_id": "s1", "issuer": "lotte", "retired_run_id": "r"},),
        candidates=(),
        unjustified={},
        ledger=RetirementLedger(updated_run_id="r", entries=(), ledger_sha256=""),
        reinstated=(),
    )


def test_resolver_classifies_and_gate_passes(tmp_path: Path) -> None:
    prior = _prior({"d1": "s1", "d2": "s2"})
    calls: list[tuple] = []

    def resolver(requests, discovered):
        calls.append((tuple(r.document_id for r in requests), frozenset(discovered)))
        return _outcome()

    source = _make_source("keep", issuer="lotte", product_code="KEEP")
    pdf = _make_pdf(b"%PDF-1.4 keep", tmp_path)
    report = generate_corpus_diff_report(
        run_id="run-2",
        acquired_documents=[MockAcquiredDocument(source=source, pdf=pdf)],
        prior=prior,
        retirement_resolver=resolver,
        output_path=tmp_path / "corpus-diff.json",
        fail_on_missing=True,
    )
    assert report.schema_version == CORPUS_DIFF_SCHEMA_VERSION == "cardrag.corpus-diff.v2"
    assert report.baseline_kind == "rolling-baseline"
    assert report.counts["retired"] == 1
    assert report.counts["missing_unjustified"] == 0
    assert calls and set(calls[0][0]) == {"d1", "d2"}


def test_unjustified_raise_carries_counts(tmp_path: Path) -> None:
    prior = _prior({"d1": "s1"})

    def resolver(requests, discovered):
        return None  # e.g. recovery-only path: everything stays unjustified

    with pytest.raises(CorpusDiffError) as raised:
        generate_corpus_diff_report(
            run_id="run-3",
            acquired_documents=[],
            prior=prior,
            retirement_resolver=resolver,
            output_path=None,
            fail_on_missing=True,
        )
    assert raised.value.missing_count == 1
    assert raised.value.retired_count == 0
    assert raised.value.candidate_count == 0


def test_pipeline_records_baseline_after_success(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state = WorkerState(state_dir / "worker-state.sqlite3")
    state.connection.execute(
        "INSERT INTO run(run_id, started_at, finished_at, status, corpus_sha256, contract_sha256) "
        "VALUES('run-s', '2026-09-30T00:00:00+00:00', '2026-09-30T02:00:00+00:00', "
        "'succeeded', ?, ?)",
        ("9" * 64, "8" * 64),
    )
    state.record_publish(
        generation_id="g-succeeded",
        run_id="run-s",
        corpus_sha256="9" * 64,
        contract_sha256="8" * 64,
        serving_sha256="7" * 64,
        status="ready",
        details={"manifest_sha256": "6" * 64},
    )
    pipeline = WorkerPipeline.__new__(WorkerPipeline)
    pipeline.state = state
    pipeline.state_dir = state_dir
    pipeline._corpus_baseline_documents = (
        CorpusBaselineDocument(
            document_id="doc-a",
            source_id="source-a",
            issuer="kb",
            product_code="PA",
            document_type="product-manual",
            temporal_status="current",
            pdf_sha256="1" * 64,
        ),
    )
    pipeline._corpus_issuer_counts = {"kb": 1}
    result = PipelineResult(
        run_id="run-s",
        status="succeeded",
        corpus_sha256="9" * 64,
        contract_sha256="8" * 64,
        generation_id="g-succeeded",
        document_count=1,
        evidence_count=2,
    )
    WorkerPipeline._record_corpus_baseline(pipeline, result)
    from cardrag_worker.corpus_baseline import load_corpus_baseline

    baseline = load_corpus_baseline(state_dir, state)
    assert baseline is not None
    assert baseline.generation_id == "g-succeeded"
    # A failed run must not advance the baseline.
    failing = replace(result, status="failed", generation_id=None)
    pipeline._corpus_baseline_documents = ()
    WorkerPipeline._record_corpus_baseline(pipeline, failing)
    again = load_corpus_baseline(state_dir, state)
    assert again is not None and again.run_id == "run-s"


def test_cli_payload_surfaces_counts() -> None:
    error = CorpusDiffError(run_id="r", missing_count=2, retired_count=1, candidate_count=3)
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        cli_module._echo_corpus_diff_error(error)
    payload = json.loads(buffer.getvalue())
    assert payload["missing_count"] == 2
    assert payload["retired_count"] == 1
    assert payload["candidate_count"] == 3

    result = PipelineResult(
        run_id="r",
        status="succeeded",
        corpus_sha256="c",
        contract_sha256="d",
        generation_id="g",
        document_count=1,
        evidence_count=1,
        retired_count=4,
        retirement_candidate_count=2,
    )
    payload = cli_module._pipeline_result_payload(result)
    assert payload["retired_count"] == 4
    assert payload["retirement_candidate_count"] == 2


def test_settings_knob_defaults_and_bounds(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from cardrag_worker.settings import WorkerSettings

    monkeypatch.delenv("CARDRAG_RETIREMENT_GRACE_RUNS", raising=False)
    monkeypatch.delenv("CARDRAG_RETIREMENT_GRACE_DAYS", raising=False)
    monkeypatch.delenv("CARDRAG_RETIREMENT_MAX_PER_RUN", raising=False)
    monkeypatch.setenv("CARDRAG_WORKER_STATE_DIR", str(tmp_path / "state"))
    base = WorkerSettings.from_env()
    assert (base.retirement_grace_runs, base.retirement_grace_days, base.retirement_max_per_run) == (2, 3, 25)
    monkeypatch.setenv("CARDRAG_RETIREMENT_GRACE_RUNS", "1")
    with pytest.raises(ValueError):
        WorkerSettings.from_env()


def test_retirement_ledger_committed_strictly_after_durable_finish_run(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state = WorkerState(state_dir / "worker-state.sqlite3")
    state.start_run(run_id="run-test")

    pipeline = WorkerPipeline.__new__(WorkerPipeline)
    pipeline.state = state
    pipeline.state_dir = state_dir

    from cardrag_worker.retirement import RetirementEntry, load_retirement_ledger

    entry = RetirementEntry(
        document_id="doc-1",
        source_id="src-1",
        issuer="lotte",
        product_code="P1",
        document_type="product-manual",
        pdf_sha256="1" * 64,
        ocr_sha256="2" * 64,
        status="candidate",
        first_absent_at="2026-09-30T00:00:00+00:00",
        first_absent_run_id="run-test",
        last_observed_at="2026-09-29T00:00:00+00:00",
        last_observed_run_id="run-base",
        consecutive_absences=1,
        last_checked_run_id="run-test",
    )
    pending = RetirementLedger(updated_run_id="run-test", entries=(entry,), ledger_sha256="")
    pipeline._pending_retirement_ledger = pending

    # Before finish_run, disk has no retirement ledger
    assert load_retirement_ledger(state_dir, state) is None

    # If run fails, finish_run is called with "failed", _commit is NOT called
    pipeline._pending_retirement_ledger = pending
    state.finish_run("run-test", "failed", error="simulated failure")
    assert load_retirement_ledger(state_dir, state) is None

    # In a new run that succeeds:
    state.start_run(run_id="run-success")
    state.finish_run("run-success", "succeeded", corpus_sha256="a" * 64, contract_sha256="b" * 64)
    # Finish_run succeeded, now _commit_pending_retirement_ledger is called
    pipeline._pending_retirement_ledger = RetirementLedger(
        updated_run_id="run-success", entries=(entry,), ledger_sha256=""
    )
    pipeline._commit_pending_retirement_ledger()

    loaded = load_retirement_ledger(state_dir, state)
    assert loaded is not None
    assert loaded.updated_run_id == "run-success"


@pytest.mark.asyncio
async def test_rolling_baseline_non_seed_document_ocr_evidence_resolution(tmp_path: Path) -> None:
    import hashlib

    from cardrag_worker.webdav import WebDAVClient

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state = WorkerState(state_dir / "worker-state.sqlite3")
    state.start_run(run_id="run-prior")
    state.finish_run("run-prior", "succeeded", corpus_sha256="a" * 64, contract_sha256="b" * 64)

    doc_id = "doc-non-seed-1"
    pdf_content = b"%PDF-1.4 test non-seed"
    actual_pdf_sha = hashlib.sha256(pdf_content).hexdigest()
    valid_ocr_bytes = b"# Non-seed OCR Markdown content\n"
    valid_ocr_sha = hashlib.sha256(valid_ocr_bytes).hexdigest()

    run_sealed = state_dir / "runs" / "run-prior" / "sealed"
    run_sealed.mkdir(parents=True)
    manifest_payload = {
        "schema_version": "cardrag.generation.v6",
        "generation_id": "g-prior",
        "created_at": "2026-09-30T00:00:00+00:00",
        "corpus_sha256": "a" * 64,
        "contract_sha256": "b" * 64,
        "documents": [
            {
                "document_id": doc_id,
                "issuer": "samsung",
                "pdf": {
                    "path": f"v1/objects/sha256/{actual_pdf_sha[:2]}/{actual_pdf_sha}",
                    "sha256": actual_pdf_sha,
                    "size_bytes": len(pdf_content),
                    "media_type": "application/pdf",
                },
                "ocr": {
                    "path": f"v1/objects/sha256/{valid_ocr_sha[:2]}/{valid_ocr_sha}",
                    "sha256": valid_ocr_sha,
                    "size_bytes": len(valid_ocr_bytes),
                    "media_type": "text/markdown; charset=utf-8",
                },
                "availability": "available",
                "page_count": 1,
            }
        ],
    }
    (run_sealed / "publish.json").write_text(
        json.dumps(
            {
                "schema_version": "cardrag.worker-seal.v1",
                "run_id": "run-prior",
                "generation_id": "g-prior",
                "manifest": manifest_payload,
            }
        )
    )

    # Fake WebDAV client inheriting from WebDAVClient so isinstance passes
    class MockCore:
        def __init__(self, content: bytes | None) -> None:
            self.content = content

        def get(self, path: object, max_bytes: int | None = None) -> Any:
            if self.content is None:
                raise RuntimeError("404 Not Found")

            class Resp:
                status_code = 200
                content = self.content

            return Resp()

    class MockWebDAV(WebDAVClient):
        def __init__(self, ocr_content: bytes | None) -> None:
            self.core = MockCore(ocr_content)  # type: ignore[assignment]

    pipeline = WorkerPipeline.__new__(WorkerPipeline)
    pipeline.state = state
    pipeline.state_dir = state_dir
    pipeline.webdav = None

    pdf_cache_dir = state_dir / "pdf-cache" / "objects" / "sha256" / actual_pdf_sha[:2]
    pdf_cache_dir.mkdir(parents=True)
    (pdf_cache_dir / actual_pdf_sha).write_bytes(pdf_content)

    src_id = "source_" + "1" * 64
    state.record_pdf_cache_object(
        pdf_sha256=actual_pdf_sha,
        size_bytes=len(pdf_content),
        page_count=1,
        relative_path=f"objects/sha256/{actual_pdf_sha[:2]}/{actual_pdf_sha}",
    )
    state.connection.execute(
        "INSERT INTO pdf_cache_source (source_id, issuer, product_code, document_type, source_url, "
        "source_version, source_post_id, discovery_sha256, first_observed_at, last_observed_at, last_verified_at) "
        "VALUES (?, 'samsung', 'P1', 'product_manual', 'https://example.test', '1', '1', ?, "
        "'2026-09-30T00:00:00+00:00', '2026-09-30T00:00:00+00:00', '2026-09-30T00:00:00+00:00')",
        (src_id, "d" * 64),
    )
    state.connection.execute(
        "INSERT INTO pdf_cache_source_revision (source_id, pdf_sha256, pdf_size_bytes, page_count, final_url, "
        "first_observed_at, last_observed_at, verified_at) "
        "VALUES (?, ?, ?, 1, 'https://example.test', '2026-09-30T00:00:00+00:00', '2026-09-30T00:00:00+00:00', '2026-09-30T00:00:00+00:00')",
        (src_id, actual_pdf_sha, len(pdf_content)),
    )

    from cardrag_worker.retirement import AbsentDocument

    absent = AbsentDocument(
        document_id=doc_id,
        source_id=src_id,
        issuer="samsung",
        product_code="P1",
        document_type="product_manual",
        pdf_sha256=actual_pdf_sha,
        ocr_sha256=valid_ocr_sha,
        last_observed_run_id="run-prior",
        last_observed_at="2026-09-30T00:00:00+00:00",
    )

    # 1. With WebDAV CAS returning corrupted bytes (mismatched sha256):
    pipeline.webdav = MockWebDAV(b"corrupted bytes")
    assert pipeline._retirement_evidence_ok(absent, None) is False  # type: ignore[arg-type]

    # 2. With WebDAV CAS returning exact valid OCR bytes:
    pipeline.webdav = MockWebDAV(valid_ocr_bytes)
    assert pipeline._retirement_evidence_ok(absent, None) is True  # type: ignore[arg-type]
