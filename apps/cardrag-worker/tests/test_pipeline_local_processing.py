from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import pytest
from helpers import pdf_bytes
from test_pipeline_v5 import (
    _OCR,
    _FakeCandidateWebDAV,
    _install_pdf_http,
    _MultiAdapter,
    _test_qwen_embeddings,
    _test_source,
)

import cardrag_worker.pipeline as pipeline_module
from cardrag_worker.ocr import OCRResult
from cardrag_worker.pipeline import OCRSystemicFailureError, WorkerPipeline, WorkerUnexpectedFailureError
from cardrag_worker.providers import ProviderSystemicError
from cardrag_worker.state import WorkerState


class SerialOCR(_OCR):
    def __init__(self) -> None:
        super().__init__()
        self.active = 0
        self.maximum = 0

    async def resolve(self, **kwargs: Any) -> OCRResult:
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        try:
            await asyncio.sleep(0)
            return await super().resolve(**kwargs)
        finally:
            self.active -= 1


async def _wait(event: threading.Event) -> None:
    assert await asyncio.to_thread(event.wait, 5)


def _pipeline(
    root: Path, state: WorkerState, ocr: SerialOCR, requests: list[dict[str, Any]], *, workers: int = 4
) -> WorkerPipeline:
    webdav = _FakeCandidateWebDAV()
    webdav.fail_pointer_once = False
    return WorkerPipeline(
        state=state,
        state_dir=root,
        adapters=[_MultiAdapter(tuple(_test_source(f"local-{i}") for i in range(4)))],
        ocr=ocr,  # type: ignore[arg-type]
        embeddings=_test_qwen_embeddings(requests),
        webdav=webdav,  # type: ignore[arg-type]
        local_processing_workers=workers,
        pdf_concurrency=workers,
        maximum_attempts=1,
        retry_cap_seconds=0,
    )


async def test_local_parsing_overlaps_but_ocr_requests_and_embedding_are_fenced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_pdf_http(monkeypatch, [pdf_bytes()], [])
    real_parse = pipeline_module.parse_structure_artifact
    first_started = threading.Event()
    second_started = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    started = 0
    main_thread = threading.get_ident()

    def parse(*args: Any, **kwargs: Any) -> Any:
        nonlocal started
        assert threading.get_ident() != main_thread
        with lock:
            started += 1
            ordinal = started
        if ordinal == 1:
            first_started.set()
            assert release.wait(10)
        else:
            second_started.set()
        return real_parse(*args, **kwargs)

    monkeypatch.setattr(pipeline_module, "parse_structure_artifact", parse)
    ocr = SerialOCR()
    requests: list[dict[str, Any]] = []
    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = _pipeline(tmp_path, state, ocr, requests)
        task = asyncio.create_task(pipeline.run())
        try:
            await _wait(first_started)
            await _wait(second_started)
            assert not task.done()
            assert requests == []
            assert ocr.maximum == 1
        finally:
            release.set()
        result = await task
        assert result.status == "succeeded"
        assert ocr.calls == 4
        report = json.loads((tmp_path / "runs" / result.run_id / "reports" / "performance.json").read_bytes())
        assert report["metrics"]["ocr_expected"] == report["metrics"]["ocr_succeeded"] == 4
        assert report["metrics"]["ocr_missing"] == 0
        assert report["metrics"]["token_cache_hits"] > 0


async def test_cancellation_drains_blocking_parsing_before_worker_state_is_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_pdf_http(monkeypatch, [pdf_bytes()], [])
    real_parse = pipeline_module.parse_structure_artifact
    started = threading.Event()
    release = threading.Event()
    completed = threading.Event()

    def parse(*args: Any, **kwargs: Any) -> Any:
        started.set()
        assert release.wait(10)
        try:
            return real_parse(*args, **kwargs)
        finally:
            completed.set()

    monkeypatch.setattr(pipeline_module, "parse_structure_artifact", parse)
    requests: list[dict[str, Any]] = []
    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = _pipeline(tmp_path, state, SerialOCR(), requests)
        task = asyncio.create_task(pipeline.run())
        try:
            await _wait(started)
            task.cancel()
            await asyncio.sleep(0.01)
            task.cancel()
            assert not task.done()
            assert not completed.is_set()
            assert requests == []
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert completed.is_set()
        row = state.connection.execute("SELECT run_id,status FROM run").fetchone()
        assert row["status"] == "interrupted"
        result = await pipeline.run(resume_run_id=row["run_id"])
        assert result.status == "succeeded"
        assert pipeline.performance.snapshot()["metrics"]["ocr_missing"] == 0


async def test_missing_document_result_blocks_embedding_and_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_pdf_http(monkeypatch, [pdf_bytes()], [])
    real_map = pipeline_module.bounded_ordered_map

    async def drop_last(items: Any, operation: Any, **kwargs: Any) -> Any:
        if kwargs.get("group_key") is None:
            items = items[:-1]
        return await real_map(items, operation, **kwargs)

    monkeypatch.setattr(pipeline_module, "bounded_ordered_map", drop_last)
    requests: list[dict[str, Any]] = []
    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = _pipeline(tmp_path, state, SerialOCR(), requests)
        with pytest.raises(WorkerUnexpectedFailureError) as failure:
            await pipeline.run()
        assert requests == []
        assert pipeline.performance.snapshot()["metrics"]["ocr_missing"] == 1
        assert not (tmp_path / "runs" / failure.value.run_id / "sealed" / "publish.json").exists()


async def test_systemic_ocr_failure_closes_gate_before_waking_waiting_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_pdf_http(monkeypatch, [pdf_bytes()], [])

    class FailingOCR(SerialOCR):
        async def resolve(self, **kwargs: Any) -> OCRResult:
            self.calls += 1
            # Other admitted documents have time to queue behind the OCR lock.
            await asyncio.sleep(0.01)
            raise ProviderSystemicError("provider_process_exit", exit_code=17)

    requests: list[dict[str, Any]] = []
    ocr = FailingOCR()
    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = _pipeline(tmp_path, state, ocr, requests)
        with pytest.raises(OCRSystemicFailureError):
            await pipeline.run()
        assert ocr.calls == 1
        assert requests == []
        assert state.connection.execute("SELECT status FROM run").fetchone()[0] == "failed"


async def test_sequential_and_parallel_processing_produce_identical_serving_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_pdf_http(monkeypatch, [pdf_bytes()], [])
    artifacts = []
    contracts = []
    for workers in (1, 4):
        root = tmp_path / str(workers)
        root.mkdir()
        with WorkerState(root / "state.sqlite3") as state:
            run_id = state.start_run(run_id="same-v1018-input")
            pipeline = _pipeline(root, state, SerialOCR(), [], workers=workers)
            contracts.append(pipeline.contract_sha256)
            result = await pipeline.run(resume_run_id=run_id)
            assert result.status == "succeeded"
            sealed = root / "runs" / run_id / "sealed"
            artifacts.append(tuple((sealed / name).read_bytes() for name in ("index.sqlite3", "vectors.f32")))
    assert contracts[0] == contracts[1]
    assert artifacts[0] == artifacts[1]
