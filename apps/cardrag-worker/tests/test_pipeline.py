from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import traceback
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from helpers import pdf_bytes

import cardrag_worker.pipeline as pipeline_module
from cardrag_worker.contracts import (
    DownloadRequest,
    IssuerSpec,
    ProtectedSourceAllowance,
    SourceRecord,
    snapshot_from_records,
)
from cardrag_worker.downloader import ProtectedDocumentError, validate_pdf
from cardrag_worker.downloader import SecurePDFDownloader as RealDownloader
from cardrag_worker.ocr import OCRResult, OCRValidationError
from cardrag_worker.pipeline import (
    OCRDocumentFailuresError,
    OCRFailureBookkeepingError,
    OCRSystemicFailureError,
    WorkerPipeline,
    classify_ocr_failure,
    is_isolatable_document_ocr_failure,
)
from cardrag_worker.providers import ProviderError
from cardrag_worker.state import WorkerState
from cardrag_worker.webdav import RemoteGenerationIdentity


class StopAfterNoChangeCheck(RuntimeError):
    pass


def test_remote_generation_identity_rejects_cross_schema_pair() -> None:
    with pytest.raises(ValueError, match="schema versions must match"):
        RemoteGenerationIdentity(
            generation_id="g-cross",
            corpus_sha256="a" * 64,
            contract_sha256="b" * 64,
            generation_schema="cardrag.generation.v2",
            serving_schema="cardrag.serving-db.v3",
        )


class FakeOCR:
    contract = {"schema_version": "test-ocr.v1"}
    adoption_policy_version = "cardrag.legacy-ocr-adoption.v1"

    def __init__(self) -> None:
        self.calls = 0

    async def resolve(self, **kwargs: Any) -> Any:
        self.calls += 1
        raise StopAfterNoChangeCheck


class FakeEmbeddings:
    provider = "openrouter"
    model = "test-embedding"
    dimension = 1536

    def __init__(self) -> None:
        self.calls = 0

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [[1.0] + [0.0] * 1535 for _ in texts]


class SelectiveOCR:
    contract = {"schema_version": "test-ocr.v1"}
    adoption_policy_version = "cardrag.legacy-ocr-adoption.v1"

    def __init__(self, failing_document_ids: set[str], raw_sentinel: str) -> None:
        self.failing_document_ids = failing_document_ids
        self.raw_sentinel = raw_sentinel
        self.calls: list[str] = []

    async def resolve(self, **kwargs: Any) -> OCRResult:
        document_id = str(kwargs["document_id"])
        self.calls.append(document_id)
        if document_id in self.failing_document_ids:
            raise ProviderError(f"Codex OCR exited 17: {self.raw_sentinel}")
        return successful_ocr_result()


class SingleFailureOCR:
    contract = {"schema_version": "test-ocr.v1"}
    adoption_policy_version = "cardrag.legacy-ocr-adoption.v1"

    def __init__(
        self,
        failing_document_id: str,
        failure_factory: Callable[[], Exception],
    ) -> None:
        self.failing_document_id = failing_document_id
        self.failure_factory = failure_factory
        self.calls: list[str] = []

    async def resolve(self, **kwargs: Any) -> OCRResult:
        document_id = str(kwargs["document_id"])
        self.calls.append(document_id)
        if document_id == self.failing_document_id:
            raise self.failure_factory()
        return successful_ocr_result()


def successful_ocr_result() -> OCRResult:
    page = "정상적으로 처리된 OCR 문서의 충분히 긴 본문입니다."
    body = f"## Page 1\n\n{page}\n".encode()
    return OCRResult(
        pages=(page,),
        ocr_bytes=body,
        ocr_text=body.decode(),
        ocr_sha256=hashlib.sha256(body).hexdigest(),
        size_bytes=len(body),
        provenance="native",
        provider="test",
        model="test",
        reuse_key="a" * 64,
    )


class FakeWebDAV:
    def __init__(self, current: RemoteGenerationIdentity | None) -> None:
        self.current = current

    async def validated_current_generation(self) -> RemoteGenerationIdentity | None:
        return self.current

    async def get_bytes(self, path: object, *, max_bytes: int | None = None) -> bytes | None:
        return b"stable" if self.current is not None else None


class Adapter:
    parser_version = "test-adapter.v1"

    def __init__(self, records: tuple[SourceRecord, ...]) -> None:
        self.records = records
        self.discovery_calls = 0
        self.prepare_calls = 0
        self.spec = IssuerSpec(
            code="testbank",
            display_name="테스트카드",
            sort_order=1,
            allowed_hosts=frozenset({"cards.example"}),
            categories=("credit",),
            minimum_interval_seconds=0,
            retry_base_seconds=0.001,
            maximum_retries=2,
        )

    async def discover_current(self, client: httpx.AsyncClient) -> Any:
        self.discovery_calls += 1
        return snapshot_from_records(
            issuer=self.spec.code,
            source_url="https://cards.example/list",
            parser_version=self.parser_version,
            records=self.records,
            started_at=datetime(2026, 8, 25, tzinfo=UTC),
        )

    async def prepare_download(self, client: httpx.AsyncClient, source: SourceRecord) -> DownloadRequest:
        self.prepare_calls += 1
        return DownloadRequest(url=source.source_url)


def source(
    *,
    product_code: str = "p1",
    source_url: str = "https://cards.example/current.pdf",
) -> SourceRecord:
    return SourceRecord(
        issuer="testbank",
        product_code=product_code,
        product_name="테스트 카드",
        effective_date=date(2026, 8, 1),
        source_version="1",
        source_url=source_url,
        source_post_id=f"post-{product_code}",
        file_name=source_url.rsplit("/", 1)[-1],
        category="credit",
        discovered_at=datetime(2026, 8, 25, tzinfo=UTC),
    )


def install_http(monkeypatch: pytest.MonkeyPatch, payload: bytes, requests: list[str]) -> None:
    real_async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            200,
            headers={"content-type": "application/pdf"},
            content=payload,
            request=request,
        )

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return real_async_client(transport=httpx.MockTransport(handler), timeout=kwargs.get("timeout"))

    monkeypatch.setattr(pipeline_module.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(
        pipeline_module,
        "SecurePDFDownloader",
        lambda policy: RealDownloader(policy, resolver=lambda _host: ("93.184.216.34",)),
    )


def http_status_error(status_code: int, *, raw_detail: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://private.example/RAW_PRIVATE_URL/token")
    return httpx.HTTPStatusError(
        raw_detail,
        request=request,
        response=httpx.Response(status_code, request=request),
    )


@pytest.mark.asyncio
async def test_ocr_failures_are_reported_then_later_documents_continue_without_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = pdf_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    records = tuple(
        source(
            product_code=f"p{index}",
            source_url=f"https://cards.example/current-{index}.pdf",
        )
        for index in range(1, 4)
    )
    failing_ids = {records[0].document_id(digest), records[2].document_id(digest)}
    raw_sentinel = "RAW_PROVIDER_STDERR_SECRET_SENTINEL"
    ocr = SelectiveOCR(failing_ids, raw_sentinel)
    embeddings = FakeEmbeddings()
    requests: list[str] = []
    install_http(monkeypatch, payload, requests)

    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[Adapter(records)],
            ocr=ocr,  # type: ignore[arg-type]
            embeddings=embeddings,
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
            maximum_attempts=2,
            retry_cap_seconds=0,
        )
        with pytest.raises(OCRDocumentFailuresError) as captured:
            await pipeline.run()

        error = captured.value
        assert [item.product_code for item in error.failures] == ["p1", "p3"]
        assert ocr.calls == [
            records[0].document_id(digest),
            records[0].document_id(digest),
            records[1].document_id(digest),
            records[2].document_id(digest),
            records[2].document_id(digest),
        ]
        assert embeddings.calls == 0
        assert state.connection.execute("SELECT count(*) FROM publish").fetchone()[0] == 0
        run_row = state.connection.execute("SELECT status,error FROM run").fetchone()
        assert tuple(run_row) == (
            "failed",
            f"2 OCR document(s) failed; report={error.report}",
        )
        for record, expected_status, expected_attempts in (
            (records[0], "failed", 2),
            (records[1], "succeeded", 1),
            (records[2], "failed", 2),
        ):
            stage = state.get_stage(error.run_id, record.document_id(digest), "ocr")
            assert stage is not None
            assert (stage.status, stage.attempt_count) == (expected_status, expected_attempts)
            if expected_status == "failed":
                assert stage.last_error == ("provider_exit_17: The OCR provider process exited with code 17.")
        successful_document_id = records[1].document_id(digest)
        for stage_name in ("structure", "chunk"):
            stage = state.get_stage(error.run_id, successful_document_id, stage_name)
            assert stage is not None
            assert (stage.status, stage.attempt_count) == ("succeeded", 1)
        successful_root = tmp_path / "runs" / error.run_id / "documents" / successful_document_id
        assert (successful_root / "ocr" / "ocr.md").is_file()
        assert (successful_root / "structure" / "pages.json").is_file()
        assert (successful_root / "chunks" / "chunks.json").is_file()

        report_body = error.report_path.read_bytes()
        report = json.loads(report_body)
        assert report_body == pipeline_module.canonical_json_bytes(report)
        assert report["schema_version"] == "cardrag.ocr-failure-report.v1"
        assert report["run_id"] == error.run_id
        assert report["failure_count"] == 2
        assert [item["product_code"] for item in report["failures"]] == ["p1", "p3"]
        assert all(item["attempts"] == 2 for item in report["failures"])
        assert all(item["reason_code"] == "provider_exit_17" for item in report["failures"])
        persisted = json.dumps(report, ensure_ascii=False) + str(
            state.connection.execute("SELECT error FROM run").fetchone()[0]
        )
        persisted += "".join(
            str(row[0] or "") for row in state.connection.execute("SELECT last_error FROM stage").fetchall()
        )
        assert raw_sentinel not in persisted

    assert len(requests) == 3
    assert not (tmp_path / "runs" / error.run_id / "sealed").exists()
    assert raw_sentinel not in caplog.text
    assert caplog.text.count("; continuing") == 2


@pytest.mark.parametrize(
    ("failure_factory", "expected_reason_code", "raw_markers"),
    [
        pytest.param(
            lambda: OCRValidationError("RAW_VALIDATION_DETAIL"),
            "generic_validation_error",
            ("RAW_VALIDATION_DETAIL",),
            id="validation",
        ),
        pytest.param(
            lambda: TimeoutError("RAW_TIMEOUT_DETAIL"),
            "provider_timeout",
            ("RAW_TIMEOUT_DETAIL",),
            id="timeout",
        ),
        pytest.param(
            lambda: http_status_error(503, raw_detail="RAW_HTTP_DETAIL"),
            "provider_http_503",
            ("RAW_HTTP_DETAIL", "RAW_PRIVATE_URL", "private.example"),
            id="transient-http",
        ),
    ],
)
@pytest.mark.asyncio
async def test_document_scoped_ocr_errors_exhaust_then_later_document_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure_factory: Callable[[], Exception],
    expected_reason_code: str,
    raw_markers: tuple[str, ...],
) -> None:
    payload = pdf_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    records = (
        source(product_code="p1", source_url="https://cards.example/current-1.pdf"),
        source(product_code="p2", source_url="https://cards.example/current-2.pdf"),
    )
    failing_document_id = records[0].document_id(digest)
    successful_document_id = records[1].document_id(digest)
    ocr = SingleFailureOCR(failing_document_id, failure_factory)
    requests: list[str] = []
    install_http(monkeypatch, payload, requests)

    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[Adapter(records)],
            ocr=ocr,  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
            maximum_attempts=2,
            retry_cap_seconds=0,
        )
        with pytest.raises(OCRDocumentFailuresError) as captured:
            await pipeline.run()

        error = captured.value
        assert ocr.calls == [failing_document_id, failing_document_id, successful_document_id]
        assert len(error.failures) == 1
        assert error.failures[0].reason_code == expected_reason_code
        failed_stage = state.get_stage(error.run_id, failing_document_id, "ocr")
        assert failed_stage is not None
        assert (failed_stage.status, failed_stage.attempt_count) == ("failed", 2)
        successful_stage = state.get_stage(error.run_id, successful_document_id, "ocr")
        assert successful_stage is not None
        assert (successful_stage.status, successful_stage.attempt_count) == ("succeeded", 1)
        for stage_name in ("structure", "chunk"):
            stage = state.get_stage(error.run_id, successful_document_id, stage_name)
            assert stage is not None and stage.status == "succeeded"
        persisted = error.report_path.read_text(encoding="utf-8")
        persisted += str(state.connection.execute("SELECT error FROM run").fetchone()[0])
        persisted += "".join(
            str(row[0] or "") for row in state.connection.execute("SELECT last_error FROM stage").fetchall()
        )
        for marker in raw_markers:
            assert marker not in persisted

    assert len(requests) == 2
    for marker in raw_markers:
        assert marker not in caplog.text


@pytest.mark.parametrize(
    ("failure_factory", "raw_markers"),
    [
        pytest.param(
            lambda: RuntimeError("RAW_RUNTIME_DETAIL"),
            ("RAW_RUNTIME_DETAIL",),
            id="runtime",
        ),
        pytest.param(
            lambda: http_status_error(401, raw_detail="RAW_HTTP_AUTH_DETAIL"),
            ("RAW_HTTP_AUTH_DETAIL", "RAW_PRIVATE_URL", "private.example"),
            id="http-401",
        ),
        pytest.param(
            lambda: httpx.ConnectError(
                "RAW_CONNECT_DETAIL",
                request=httpx.Request("POST", "https://private.example/RAW_PRIVATE_URL/token"),
            ),
            ("RAW_CONNECT_DETAIL", "RAW_PRIVATE_URL", "private.example"),
            id="connect-error",
        ),
    ],
)
@pytest.mark.asyncio
async def test_systemic_ocr_error_fails_first_attempt_without_report_or_later_ocr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure_factory: Callable[[], Exception],
    raw_markers: tuple[str, ...],
) -> None:
    payload = pdf_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    records = (
        source(product_code="p1", source_url="https://cards.example/current-1.pdf"),
        source(product_code="p2", source_url="https://cards.example/current-2.pdf"),
    )
    failing_document_id = records[0].document_id(digest)
    later_document_id = records[1].document_id(digest)
    ocr = SingleFailureOCR(failing_document_id, failure_factory)
    requests: list[str] = []
    install_http(monkeypatch, payload, requests)

    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[Adapter(records)],
            ocr=ocr,  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
            maximum_attempts=4,
            retry_cap_seconds=0,
        )
        with pytest.raises(OCRSystemicFailureError) as captured:
            await pipeline.run()

        assert ocr.calls == [failing_document_id]
        run_row = state.connection.execute("SELECT run_id,status,error FROM run").fetchone()
        assert run_row is not None
        run_id = str(run_row["run_id"])
        assert (run_row["status"], run_row["error"]) == (
            "failed",
            "OCR failed with a non-document-scoped error",
        )
        failed_stage = state.get_stage(run_id, failing_document_id, "ocr")
        assert failed_stage is not None
        assert (failed_stage.status, failed_stage.attempt_count, failed_stage.last_error) == (
            "failed",
            1,
            OCRSystemicFailureError.stored_error,
        )
        assert state.get_stage(run_id, later_document_id, "ocr") is None
        assert not (tmp_path / "runs" / run_id / "reports" / "ocr-failures.json").exists()
        rendered = "".join(
            traceback.format_exception(
                type(captured.value),
                captured.value,
                captured.value.__traceback__,
            )
        )
        persisted = rendered + str(run_row["error"]) + str(failed_stage.last_error) + caplog.text
        for marker in raw_markers:
            assert marker not in persisted

    assert len(requests) == 2


@pytest.mark.parametrize(
    "exc",
    [
        OCRValidationError("private validation detail"),
        ProviderError("private provider detail"),
        TimeoutError("private timeout detail"),
        httpx.ReadTimeout(
            "private timeout detail",
            request=httpx.Request("POST", "https://private.example/token"),
        ),
        *(http_status_error(status, raw_detail="private detail") for status in (408, 413, 422, 425, 429)),
        http_status_error(500, raw_detail="private detail"),
        http_status_error(599, raw_detail="private detail"),
    ],
)
def test_document_scoped_ocr_failure_allowlist(exc: Exception) -> None:
    assert is_isolatable_document_ocr_failure(exc)


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("private runtime detail"),
        ValueError("private value detail"),
        OSError("private local path"),
        sqlite3.OperationalError("private database detail"),
        httpx.ConnectError(
            "private connect detail",
            request=httpx.Request("POST", "https://private.example/token"),
        ),
        httpx.RequestError(
            "private request detail",
            request=httpx.Request("POST", "https://private.example/token"),
        ),
        *(http_status_error(status, raw_detail="private detail") for status in (400, 401, 402, 403, 404)),
    ],
)
def test_systemic_ocr_failure_is_not_in_document_allowlist(exc: Exception) -> None:
    assert not is_isolatable_document_ocr_failure(exc)


@pytest.mark.parametrize(
    ("exc", "reason_code"),
    [
        (OCRValidationError("OCR sparse-page wrapper is invalid"), "sparse_page_wrapper_invalid"),
        (OCRValidationError("OCR page markers [2] do not match [1]"), "page_marker_mismatch"),
        (
            OCRValidationError("OCR provider output must begin with the first Page marker"),
            "output_not_marker_first",
        ),
        (OCRValidationError("OCR provider returned an empty page"), "empty_page"),
        (OCRValidationError("OCR blank-page sentinel must be exact"), "blank_sentinel_invalid"),
        (
            OCRValidationError("OCR provider returned an implausibly short page"),
            "implausibly_short",
        ),
        (OCRValidationError("invalid native OCR cache JSON"), "cache_validation_error"),
        (OCRValidationError("private validation detail"), "generic_validation_error"),
        (ProviderError("opaque provider detail"), "provider_error"),
        (ProviderError("Codex OCR exited -9: private stderr"), "provider_exit_negative_9"),
        (TimeoutError("private timeout detail"), "provider_timeout"),
        (
            httpx.HTTPStatusError(
                "private HTTP detail",
                request=httpx.Request("POST", "https://private.example/token"),
                response=httpx.Response(
                    503,
                    request=httpx.Request("POST", "https://private.example/token"),
                ),
            ),
            "provider_http_503",
        ),
        (
            httpx.ConnectError(
                "private network detail",
                request=httpx.Request("POST", "https://private.example/token"),
            ),
            "provider_network_error",
        ),
        (OSError("/private/path"), "local_io_error"),
        (RuntimeError("private unexpected detail"), "unexpected_error"),
    ],
)
def test_ocr_failure_classification_is_allowlisted(exc: Exception, reason_code: str) -> None:
    failure = classify_ocr_failure(exc)
    assert failure.reason_code == reason_code
    assert str(exc) not in failure.stored_error


@pytest.mark.asyncio
async def test_stage_failure_bookkeeping_error_suppresses_original_exception_context(
    tmp_path: Path,
) -> None:
    raw_sentinel = "RAW_PROVIDER_STDERR_SECRET_SENTINEL"
    with WorkerState(tmp_path / "state.sqlite3") as state:
        run_id = state.start_run(run_id="run-safe-bookkeeping")
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[Adapter((source(),))],
            ocr=FakeOCR(),  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
            maximum_attempts=1,
        )

        async def fail() -> None:
            raise ProviderError(raw_sentinel)

        def broken_formatter(_exc: Exception) -> str:
            raise RuntimeError("private formatter failure")

        with pytest.raises(RuntimeError, match="stage failure bookkeeping failed") as captured:
            await pipeline._finite_stage(  # noqa: SLF001
                run_id=run_id,
                document_id="doc_" + "a" * 64,
                name="ocr",
                operation=fail,
                error_formatter=broken_formatter,
            )
        rendered = "".join(
            traceback.format_exception(
                type(captured.value),
                captured.value,
                captured.value.__traceback__,
            )
        )
        assert raw_sentinel not in rendered
        assert "private formatter failure" not in rendered


@pytest.mark.asyncio
async def test_retry_backoff_cancellation_has_no_provider_exception_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_sentinel = "RAW_PROVIDER_BACKOFF_STDERR_SENTINEL"
    sleep_started = asyncio.Event()

    async def blocked_sleep(_delay: float) -> None:
        sleep_started.set()
        await asyncio.Future()

    monkeypatch.setattr(pipeline_module.asyncio, "sleep", blocked_sleep)
    with WorkerState(tmp_path / "state.sqlite3") as state:
        run_id = state.start_run(run_id="run-safe-backoff-cancel")
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[Adapter((source(),))],
            ocr=FakeOCR(),  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
            maximum_attempts=2,
        )

        async def fail() -> None:
            raise ProviderError(raw_sentinel)

        task = asyncio.create_task(
            pipeline._finite_stage(  # noqa: SLF001
                run_id=run_id,
                document_id="doc_" + "a" * 64,
                name="ocr",
                operation=fail,
                error_formatter=lambda exc: classify_ocr_failure(exc).stored_error,
            )
        )
        await sleep_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as captured:
            await task

        rendered = "".join(
            traceback.format_exception(
                type(captured.value),
                captured.value,
                captured.value.__traceback__,
            )
        )
        assert captured.value.__context__ is None
        assert raw_sentinel not in rendered
        stage = state.get_stage(run_id, "doc_" + "a" * 64, "ocr")
        assert stage is not None
        assert (stage.status, stage.attempt_count, stage.last_error) == (
            "retry",
            1,
            "provider_error: The OCR provider failed.",
        )


@pytest.mark.asyncio
async def test_exhausted_ocr_state_lookup_failure_suppresses_provider_and_state_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = pdf_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    record = source()
    document_id = record.document_id(digest)
    provider_sentinel = "RAW_PROVIDER_STATE_LOOKUP_SENTINEL"
    state_sentinel = "RAW_STATE_LOOKUP_SENTINEL"
    ocr = SelectiveOCR({document_id}, provider_sentinel)
    requests: list[str] = []
    install_http(monkeypatch, payload, requests)

    with WorkerState(tmp_path / "state.sqlite3") as state:
        real_get_stage = state.get_stage
        ocr_get_calls = 0

        def fail_second_ocr_lookup(
            run_id: str,
            current_document_id: str,
            stage_name: str,
        ) -> Any:
            nonlocal ocr_get_calls
            if current_document_id == document_id and stage_name == "ocr":
                ocr_get_calls += 1
                if ocr_get_calls == 2:
                    raise sqlite3.OperationalError(state_sentinel)
            return real_get_stage(run_id, current_document_id, stage_name)

        monkeypatch.setattr(state, "get_stage", fail_second_ocr_lookup)
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[Adapter((record,))],
            ocr=ocr,  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
            maximum_attempts=1,
        )
        with pytest.raises(OCRFailureBookkeepingError) as captured:
            await pipeline.run()

        rendered = "".join(
            traceback.format_exception(
                type(captured.value),
                captured.value,
                captured.value.__traceback__,
            )
        )
        run_row = state.connection.execute("SELECT run_id,status,error FROM run").fetchone()
        assert run_row is not None
        run_id = str(run_row["run_id"])
        stage_row = state.connection.execute(
            """SELECT status,attempt_count,last_error FROM stage
               WHERE run_id=? AND document_id=? AND stage_name='ocr'""",
            (run_id, document_id),
        ).fetchone()
        assert stage_row is not None
        assert tuple(stage_row) == (
            "failed",
            1,
            "provider_exit_17: The OCR provider process exited with code 17.",
        )
        assert (run_row["status"], run_row["error"]) == (
            "failed",
            "OCR failure isolation bookkeeping failed",
        )
        assert not (tmp_path / "runs" / run_id / "reports" / "ocr-failures.json").exists()
        leaked = rendered + str(run_row["error"]) + str(stage_row["last_error"]) + caplog.text
        assert provider_sentinel not in leaked
        assert state_sentinel not in leaked

    assert ocr.calls == [document_id]
    assert len(requests) == 1


def corpus_for(
    payload: bytes,
    record: SourceRecord,
    tmp_path: Path,
    *,
    unsupported: tuple[tuple[SourceRecord, bytes], ...] = (),
) -> str:
    protected_magic = {
        b"SCDSA002": "SCDSA002",
        b"SCDSA004": "SCDSA004",
        b"\x9b DRMONE": "FASOO_DRMONE",
    }
    path = tmp_path / "identity.pdf"
    path.write_bytes(payload)
    digest, size, pages = validate_pdf(path)
    return pipeline_module.canonical_sha256(
        {
            "schema_version": "cardrag.current-corpus.v2",
            "documents": [
                {
                    "source": record.discovery_payload,
                    "pdf_sha256": digest,
                    "pdf_size_bytes": size,
                    "page_count": pages,
                }
            ],
            "unsupported_documents": sorted(
                (
                    {
                        "disposition": "unsupported_drm",
                        "protected_magic": protected_magic[body[:8]],
                        "protected_sha256": hashlib.sha256(body).hexdigest(),
                        "protected_size_bytes": len(body),
                        "source": row.discovery_payload,
                        "source_id": row.source_id,
                    }
                    for row, body in unsupported
                ),
                key=pipeline_module.canonical_json_bytes,
            ),
        }
    )


@pytest.mark.asyncio
async def test_explicit_protected_product_is_audited_once_and_part_of_corpus_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protected = source(product_code="p1", source_url="https://cards.example/protected.pdf")
    valid = source(product_code="p2", source_url="https://cards.example/current.pdf")
    payload = pdf_bytes()
    protected_payload = b"\x9b DRMONE" + b"\x00" * 64
    requests: list[str] = []
    real_async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        content = protected_payload if request.url.path.endswith("protected.pdf") else payload
        return httpx.Response(
            200,
            headers={"content-type": "application/pdf"},
            content=content,
            request=request,
        )

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return real_async_client(transport=httpx.MockTransport(handler), timeout=kwargs.get("timeout"))

    monkeypatch.setattr(pipeline_module.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(
        pipeline_module,
        "SecurePDFDownloader",
        lambda policy: RealDownloader(policy, resolver=lambda _host: ("93.184.216.34",)),
    )
    adapter = Adapter((protected, valid))
    adapter.spec = replace(
        adapter.spec,
        protected_source_allowances=(
            ProtectedSourceAllowance(
                source_id=protected.source_id,
                product_code=protected.product_code,
                source_version=protected.source_version,
                source_url=protected.source_url,
                sha256=hashlib.sha256(protected_payload).hexdigest(),
                size_bytes=len(protected_payload),
                magic="FASOO_DRMONE",
            ),
        ),
    )
    with WorkerState(tmp_path / "state.sqlite3") as state:
        webdav = FakeWebDAV(None)
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[adapter],
            ocr=FakeOCR(),  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=webdav,  # type: ignore[arg-type]
            collect_remote_garbage=False,
        )
        webdav.current = RemoteGenerationIdentity(
            generation_id="g-current",
            corpus_sha256=corpus_for(
                payload,
                valid,
                tmp_path,
                unsupported=((protected, protected_payload),),
            ),
            contract_sha256=pipeline.contract_sha256,
        )
        result = await pipeline.run()
        skipped = state.get_stage(result.run_id, protected.source_id, "download")
        assert skipped is not None
        assert (skipped.status, skipped.attempt_count) == ("skipped", 1)
        assert skipped.last_error is not None and "unsupported_drm" in skipped.last_error
    assert result.status == "no_change"
    assert result.document_count == 1
    assert result.unsupported_document_count == 1
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_unlisted_protected_product_remains_a_terminal_download_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = source(source_url="https://cards.example/protected.pdf")
    requests: list[str] = []
    install_http(monkeypatch, b"SCDSA002" + b"\x00" * 64, requests)
    adapter = Adapter((record,))
    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[adapter],
            ocr=FakeOCR(),  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
        )
        with pytest.raises(ProtectedDocumentError):
            await pipeline.run()
        run_id = str(state.connection.execute("SELECT run_id FROM run").fetchone()[0])
        stage = state.get_stage(run_id, record.source_id, "download")
        assert stage is not None and stage.status == "failed"
    assert len(requests) == adapter.spec.maximum_retries


@pytest.mark.asyncio
async def test_changed_protected_bytes_do_not_match_an_approved_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = source(source_url="https://cards.example/protected.pdf")
    protected_payload = b"\x9b DRMONE" + b"changed"
    requests: list[str] = []
    install_http(monkeypatch, protected_payload, requests)
    adapter = Adapter((record,))
    adapter.spec = replace(
        adapter.spec,
        protected_source_allowances=(
            ProtectedSourceAllowance(
                source_id=record.source_id,
                product_code=record.product_code,
                source_version=record.source_version,
                source_url=record.source_url,
                sha256="f" * 64,
                size_bytes=len(protected_payload),
                magic="FASOO_DRMONE",
            ),
        ),
    )
    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[adapter],
            ocr=FakeOCR(),  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
        )
        with pytest.raises(ProtectedDocumentError):
            await pipeline.run()
        run_id = str(state.connection.execute("SELECT run_id FROM run").fetchone()[0])
        stage = state.get_stage(run_id, record.source_id, "download")
        assert stage is not None and stage.status == "failed"
        assert state.stage_status_count(run_id, "download", "skipped") == 0
    assert len(requests) == adapter.spec.maximum_retries


@pytest.mark.asyncio
async def test_remote_exact_match_is_no_change_without_local_publish_row_or_provider_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = pdf_bytes()
    record = source()
    requests: list[str] = []
    install_http(monkeypatch, payload, requests)
    ocr = FakeOCR()
    embeddings = FakeEmbeddings()
    adapter = Adapter((record,))
    with WorkerState(tmp_path / "state.sqlite3") as state:
        webdav = FakeWebDAV(None)
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[adapter],
            ocr=ocr,  # type: ignore[arg-type]
            embeddings=embeddings,
            webdav=webdav,  # type: ignore[arg-type]
            collect_remote_garbage=False,
        )
        webdav.current = RemoteGenerationIdentity(
            generation_id="g-current",
            corpus_sha256=corpus_for(payload, record, tmp_path),
            contract_sha256=pipeline.contract_sha256,
            generation_schema="cardrag.generation.v2",
            serving_schema="cardrag.serving-db.v2",
        )
        result = await pipeline.run()
    assert result.status == "no_change"
    assert webdav.current.generation_schema == "cardrag.generation.v2"
    assert webdav.current.serving_schema == "cardrag.serving-db.v2"
    assert len(requests) == 1
    assert adapter.prepare_calls == 1
    assert ocr.calls == embeddings.calls == 0


@pytest.mark.asyncio
async def test_same_url_with_changed_pdf_bytes_does_not_false_no_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_payload = pdf_bytes(width=612)
    new_payload = pdf_bytes(width=613)
    record = source()
    requests: list[str] = []
    install_http(monkeypatch, new_payload, requests)
    ocr = FakeOCR()
    adapter = Adapter((record,))
    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[adapter],
            ocr=ocr,  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(  # type: ignore[arg-type]
                RemoteGenerationIdentity(
                    generation_id="g-old",
                    corpus_sha256=corpus_for(old_payload, record, tmp_path),
                    contract_sha256="0" * 64,
                )
            ),
            collect_remote_garbage=False,
            maximum_attempts=1,
        )
        pipeline.webdav.current = RemoteGenerationIdentity(  # type: ignore[attr-defined]
            generation_id="g-old",
            corpus_sha256=corpus_for(old_payload, record, tmp_path),
            contract_sha256=pipeline.contract_sha256,
        )
        with pytest.raises(OCRSystemicFailureError):
            await pipeline.run()
        run_row = state.connection.execute("SELECT run_id,status,error FROM run").fetchone()
        assert run_row is not None
        run_id = str(run_row["run_id"])
        document_id = record.document_id(hashlib.sha256(new_payload).hexdigest())
        stage = state.get_stage(run_id, document_id, "ocr")
        assert stage is not None
        assert (stage.status, stage.attempt_count, stage.last_error) == (
            "failed",
            1,
            OCRSystemicFailureError.stored_error,
        )
        assert not (tmp_path / "runs" / run_id / "reports" / "ocr-failures.json").exists()
    assert len(requests) == 1
    assert ocr.calls == 1


@pytest.mark.asyncio
async def test_discovery_drop_fails_before_download_and_pointer_update(tmp_path: Path) -> None:
    record = source()
    adapter = Adapter((record,))
    with WorkerState(tmp_path / "state.sqlite3") as state:
        baseline = state.start_run(run_id="baseline")
        state.record_snapshot(
            run_id=baseline,
            snapshot_id="a" * 64,
            issuer="testbank",
            source_sha256="a" * 64,
            record_count=4,
            payload={"records": []},
        )
        state.finish_run(baseline, "succeeded")
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[adapter],
            ocr=FakeOCR(),  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
        )
        with pytest.raises(RuntimeError, match="fell below"):
            await pipeline.run()
    assert adapter.prepare_calls == 0


def test_serving_affecting_issuer_catalog_fields_change_worker_contract(tmp_path: Path) -> None:
    adapter = Adapter((source(),))
    with WorkerState(tmp_path / "state.sqlite3") as state:
        first = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[adapter],
            ocr=FakeOCR(),  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
        ).contract_sha256
        adapter.spec = replace(adapter.spec, display_name="변경된 표시명")
        second = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[adapter],
            ocr=FakeOCR(),  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
        ).contract_sha256
    assert first != second
