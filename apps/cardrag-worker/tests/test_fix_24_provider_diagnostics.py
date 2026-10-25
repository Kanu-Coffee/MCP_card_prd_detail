"""Bounded provider-process diagnostics for systemic OCR failures."""

from __future__ import annotations

import asyncio
import hashlib
import json
import traceback
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from helpers import pdf_bytes
from test_ocr import PDF_SHA, FakeProvider, fake_render
from test_pipeline import (
    Adapter,
    FakeEmbeddings,
    FakeWebDAV,
    SingleFailureOCR,
    install_http,
    source,
)

from cardrag_worker import cli as cli_module
from cardrag_worker.ocr import FailoverOCRResolver, OCRResolver
from cardrag_worker.pipeline import (
    OCRSystemicFailureError,
    OCRSystemicFailureRecord,
    WorkerPipeline,
    _classify_ocr_systemic_failure,
)
from cardrag_worker.providers import (
    CodexOCRProvider,
    OpenCodeOCRProvider,
    ProviderSystemicError,
)
from cardrag_worker.state import WorkerState


def _process(*, returncode: int, stdout: bytes, stderr: bytes) -> Any:
    class Process:
        async def communicate(self, _body: bytes | None = None) -> tuple[bytes, bytes]:
            return stdout, stderr

        def kill(self) -> None:
            return None

        async def wait(self) -> None:
            return None

    Process.returncode = returncode
    return Process()


@pytest.mark.asyncio
async def test_codex_nonzero_exit_preserves_bounded_stdout_and_stderr_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = tmp_path / "page-1.png"
    image.write_bytes(b"png")
    raw_sentinel = "RAW_FIX24_CODEX_OUTPUT_SECRET"
    stdout = f"unstructured stdout {raw_sentinel}".encode()
    stderr = f"unclassified process failure {raw_sentinel}".encode()

    async def create(*_args: str, **_kwargs: Any) -> Any:
        return _process(returncode=17, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    provider = CodexOCRProvider(executable="codex", model="gpt-5.6-sol")
    with pytest.raises(ProviderSystemicError) as captured:
        await provider.recognize(
            (image,),
            page_numbers=(1,),
            target_page_numbers=(1,),
            total_pages=1,
            prompt="transcribe",
        )

    error = captured.value
    assert error.reason_code == "provider_process_exit_unknown"
    assert error.provider == "codex-exec"
    assert error.classification_basis == "stderr_allowlist_no_match"
    assert error.stdout_size_bytes == len(stdout)
    assert error.stdout_sha256 == hashlib.sha256(stdout).hexdigest()
    assert error.stderr_size_bytes == len(stderr)
    assert error.stderr_sha256 == hashlib.sha256(stderr).hexdigest()
    rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    assert raw_sentinel not in str(error)
    assert raw_sentinel not in rendered


@pytest.mark.asyncio
async def test_codex_exit_zero_does_not_classify_misleading_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = tmp_path / "page-1.png"
    image.write_bytes(b"png")
    stdout = "## Page 1\n\n정상 OCR 본문입니다.\n".encode()

    async def create(*_args: str, **_kwargs: Any) -> Any:
        return _process(returncode=0, stdout=stdout, stderr=b"ERROR: rate limit exceeded")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    provider = CodexOCRProvider(executable="codex", model="gpt-5.6-sol")
    result = await provider.recognize(
        (image,),
        page_numbers=(1,),
        target_page_numbers=(1,),
        total_pages=1,
        prompt="transcribe",
    )
    assert result == stdout.decode()


@pytest.mark.asyncio
async def test_opencode_nonzero_ignores_text_parts_but_uses_official_error_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = tmp_path / "page-1.png"
    image.write_bytes(b"png")
    monkeypatch.setenv("ALIBABA_TOKEN_PLAN_API_KEY", "test-key")

    text_event = json.dumps(
        {
            "type": "text",
            "part": {"type": "text", "text": "quota exceeded and rate limited by ordinary OCR body"},
        }
    ).encode()

    async def create_text_only(*_args: str, **_kwargs: Any) -> Any:
        return _process(returncode=1, stdout=text_event, stderr=b"unclassified process failure")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_text_only)
    provider = OpenCodeOCRProvider()
    with pytest.raises(ProviderSystemicError) as captured:
        await provider.recognize(
            (image,),
            page_numbers=(1,),
            target_page_numbers=(1,),
            total_pages=1,
            prompt="transcribe",
        )
    assert captured.value.reason_code == "provider_process_exit_unknown"
    assert captured.value.classification_basis == "stderr_allowlist_no_match"
    assert captured.value.provider == "opencode"
    assert captured.value.stdout_size_bytes == len(text_event)

    error_event = json.dumps({"type": "error", "message": "quota exceeded"}).encode()

    async def create_error_event(*_args: str, **_kwargs: Any) -> Any:
        return _process(returncode=1, stdout=error_event, stderr=b"unclassified process failure")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_error_event)
    with pytest.raises(ProviderSystemicError) as captured:
        await provider.recognize(
            (image,),
            page_numbers=(1,),
            target_page_numbers=(1,),
            total_pages=1,
            prompt="transcribe",
        )
    assert captured.value.reason_code == "provider_process_rate_limited"
    assert captured.value.classification_basis == "stdout_error_event_allowlist"
    assert captured.value.stdout_sha256 == hashlib.sha256(error_event).hexdigest()


def test_provider_diagnostic_validation_rejects_incomplete_or_unbounded_context() -> None:
    with pytest.raises(ValueError, match="stdout diagnostics must be complete"):
        ProviderSystemicError("provider_process_exit_unknown", exit_code=1, stdout_size_bytes=3)
    with pytest.raises(ValueError, match="page range must be complete"):
        ProviderSystemicError("provider_process_exit_unknown", exit_code=1, page_start=1)
    with pytest.raises(ValueError, match="page range is invalid"):
        ProviderSystemicError(
            "provider_process_exit_unknown",
            exit_code=1,
            page_start=2,
            page_end=1,
        )
    with pytest.raises(ValueError, match="provider identity is invalid"):
        ProviderSystemicError("provider_process_exit_unknown", exit_code=1, provider="Codex Exec")
    with pytest.raises(ValueError, match="classification basis is invalid"):
        ProviderSystemicError(
            "provider_process_exit_unknown",
            exit_code=1,
            classification_basis="raw stderr",
        )

    valid = ProviderSystemicError(
        "provider_process_exit_unknown",
        exit_code=1,
        stdout_size_bytes=0,
        stdout_sha256=hashlib.sha256(b"").hexdigest(),
        provider="codex-exec",
        phase="primary",
        classification_basis="stderr_allowlist_no_match",
        chunk_index=0,
        page_start=1,
        page_end=2,
    )
    assert valid.provider == "codex-exec"
    assert valid.phase == "primary"
    assert valid.page_start == 1 and valid.page_end == 2


@pytest.mark.asyncio
async def test_ocr_resolver_attaches_provider_phase_and_chunk_page_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("cardrag_worker.ocr.render_pdf", fake_render)

    class FailingSecondChunk(FakeProvider):
        async def recognize(self, *args: Any, **kwargs: Any) -> str:
            if kwargs["target_page_numbers"][0] == 2:
                self.calls.append(2)
                raise ProviderSystemicError(
                    "provider_process_exit_unknown",
                    exit_code=1,
                    provider=self.provider,
                    classification_basis="stderr_allowlist_no_match",
                )
            return await super().recognize(*args, **kwargs)

    provider = FailingSecondChunk()
    pdf = tmp_path / "pdf.txt"
    pdf.write_text("2")
    with WorkerState(tmp_path / "state.sqlite3") as state:
        run_id = state.start_run(run_id="context-run")
        resolver = OCRResolver(
            provider=provider,
            state=state,
            webdav=None,
            chunk_pages=1,
            whole_document_max_pages=1,
        )
        with pytest.raises(ProviderSystemicError) as captured:
            await resolver.resolve(
                run_id=run_id,
                document_id="doc_context",
                pdf_path=pdf,
                pdf_sha256=PDF_SHA,
                pdf_size_bytes=3,
                page_count=2,
                output_dir=tmp_path / "ocr",
            )

    error = captured.value
    assert error.reason_code == "provider_process_exit_unknown"
    assert error.provider == "codex-exec"
    assert error.phase == "primary"
    assert error.chunk_index == 1
    assert error.page_start == 2
    assert error.page_end == 2
    classified = _classify_ocr_systemic_failure(error)
    assert (classified.provider, classified.phase) == ("codex-exec", "primary")
    assert (classified.chunk_index, classified.page_start, classified.page_end) == (1, 2, 2)


@pytest.mark.asyncio
async def test_document_provider_failure_attaches_full_page_range(
    tmp_path: Path,
) -> None:
    class FailingDocumentProvider:
        provider = "local-paddleocr"
        model = "PaddleOCR-VL-1.6"
        reasoning_effort = None
        renderer_id = "paddlex-pdfium/300dpi"
        render_scale_milli = 4167

        async def recognize_document(
            self,
            _pdf_path: Path,
            *,
            expected_page_count: int,
            output_dir: Path,
        ) -> tuple[str, ...]:
            assert expected_page_count == 2
            assert output_dir.name == "ocr"
            raise ProviderSystemicError(
                "provider_process_configuration_failed",
                exit_code=1,
                provider=self.provider,
                classification_basis="fixed_exit_policy",
            )

    pdf = tmp_path / "pdf.txt"
    pdf.write_text("2")
    with WorkerState(tmp_path / "state.sqlite3") as state:
        run_id = state.start_run(run_id="document-context-run")
        resolver = OCRResolver(provider=FailingDocumentProvider(), state=state, webdav=None)  # type: ignore[arg-type]
        with pytest.raises(ProviderSystemicError) as captured:
            await resolver.resolve(
                run_id=run_id,
                document_id="doc_document_context",
                pdf_path=pdf,
                pdf_sha256=PDF_SHA,
                pdf_size_bytes=3,
                page_count=2,
                output_dir=tmp_path / "ocr",
            )

    error = captured.value
    assert (error.provider, error.phase) == ("local-paddleocr", "primary")
    assert error.chunk_index is None
    assert (error.page_start, error.page_end) == (1, 2)


@pytest.mark.asyncio
async def test_failover_marks_fallback_phase_and_does_not_allow_unknown(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    class StubResolver:
        adoption_policy_version = "test"
        cache_mode = "read-write"
        contract: dict[str, Any] = {}

        def __init__(self, name: str, error: ProviderSystemicError | None = None) -> None:
            self.provider = SimpleNamespace(provider=name)
            self.error = error

        async def resolve(self, **_kwargs: Any) -> Any:
            calls.append(self.provider.provider)
            if self.error is not None:
                raise self.error
            return SimpleNamespace(pages=())

    primary = StubResolver(
        "codex-exec",
        ProviderSystemicError(
            "provider_process_rate_limited",
            exit_code=1,
            provider="codex-exec",
            classification_basis="stderr_allowlist",
        ),
    )
    fallback = StubResolver(
        "local-paddleocr",
        ProviderSystemicError(
            "provider_process_exit_unknown",
            exit_code=1,
            provider="local-paddleocr",
            classification_basis="fixed_exit_policy",
        ),
    )
    resolver = FailoverOCRResolver(primary, fallback)  # type: ignore[arg-type]
    with pytest.raises(ProviderSystemicError) as captured:
        await resolver.resolve(output_dir=tmp_path / "ocr")
    assert calls == ["codex-exec", "local-paddleocr"]
    assert captured.value.reason_code == "provider_process_exit_unknown"
    assert captured.value.provider == "local-paddleocr"
    assert captured.value.phase == "fallback"

    calls.clear()
    primary.error = ProviderSystemicError(
        "provider_process_exit_unknown",
        exit_code=1,
        provider="codex-exec",
        classification_basis="stderr_allowlist_no_match",
    )
    with pytest.raises(ProviderSystemicError) as captured:
        await resolver.resolve(output_dir=tmp_path / "ocr")
    assert calls == ["codex-exec"]
    assert captured.value.provider == "codex-exec"
    assert captured.value.phase == "primary"


@pytest.mark.asyncio
async def test_pipeline_report_and_cli_echo_include_provider_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = pdf_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    record = source()
    raw_sentinel = "RAW_FIX24_PIPELINE_STDOUT_SECRET"
    stdout = f"raw provider stdout {raw_sentinel}".encode()
    stderr = f"raw provider stderr {raw_sentinel}".encode()

    def provider_failure() -> ProviderSystemicError:
        return ProviderSystemicError(
            "provider_process_exit_unknown",
            exit_code=17,
            stdout_size_bytes=len(stdout),
            stdout_sha256=hashlib.sha256(stdout).hexdigest(),
            stderr_size_bytes=len(stderr),
            stderr_sha256=hashlib.sha256(stderr).hexdigest(),
            provider="codex-exec",
            phase="primary",
            classification_basis="stderr_allowlist_no_match",
            chunk_index=0,
            page_start=1,
            page_end=1,
        )

    ocr = SingleFailureOCR(record.document_id(digest), provider_failure)
    requests: list[str] = []
    install_http(monkeypatch, payload, requests)
    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[Adapter((record,))],
            ocr=ocr,  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
            maximum_attempts=4,
            retry_cap_seconds=0,
        )
        with pytest.raises(OCRSystemicFailureError) as captured:
            await pipeline.run()

        error = captured.value
        report = json.loads(error.report_path.read_bytes())
        assert report["provider"] == "codex-exec"
        assert report["phase"] == "primary"
        assert report["classification_basis"] == "stderr_allowlist_no_match"
        assert report["chunk_index"] == 0
        assert (report["page_start"], report["page_end"]) == (1, 1)
        assert report["stdout_size_bytes"] == len(stdout)
        assert report["stdout_sha256"] == hashlib.sha256(stdout).hexdigest()
        assert report["stderr_size_bytes"] == len(stderr)
        assert report["stderr_sha256"] == hashlib.sha256(stderr).hexdigest()
        persisted = error.report_path.read_text() + str(
            state.connection.execute("SELECT error FROM run").fetchone()[0]
        )
        assert raw_sentinel not in persisted


def test_invalid_optional_provider_diagnostics_do_not_erase_unknown_reason() -> None:
    error = ProviderSystemicError(
        "provider_process_exit_unknown",
        exit_code=1,
        provider="codex-exec",
        classification_basis="stderr_allowlist_no_match",
    )
    error.page_start = 2  # type: ignore[assignment]
    classified = _classify_ocr_systemic_failure(error)
    assert classified.reason_code == "provider_process_exit_unknown"
    assert classified.provider is None
    assert classified.page_start is None and classified.page_end is None


def test_legacy_systemic_record_payload_remains_optional() -> None:
    record = OCRSystemicFailureRecord(
        run_id="run-legacy",
        document_id="doc_" + "a" * 64,
        source_id="source_" + "b" * 64,
        issuer="testbank",
        product_code="p1",
        pdf_sha256="c" * 64,
        attempt=1,
        occurred_at=datetime(2026, 10, 21, tzinfo=UTC),
        reason_code="provider_process_exit_unknown",
        reason="The OCR provider process exited for an unclassified reason.",
        error_class_category="ocr_provider_systemic",
        error_kind="process_exit",
        retryable=False,
        exit_code=1,
    )
    assert "provider" not in record.payload
    assert "stdout_size_bytes" not in record.payload
    assert "chunk_index" not in record.payload
    assert "page_start" not in record.payload
    with pytest.raises(ValueError, match="stdout diagnostics must be complete"):
        replace(record, stdout_size_bytes=1)
    with pytest.raises(ValueError, match="page range must be complete"):
        replace(record, page_start=1)
    with pytest.raises(ValueError, match="page range is invalid"):
        replace(record, page_start=2, page_end=1)
    with pytest.raises(ValueError, match="classification basis is invalid"):
        replace(record, classification_basis="raw stderr")


def test_cli_systemic_failure_echoes_bounded_provider_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = OCRSystemicFailureRecord(
        run_id="run-fix24",
        document_id="doc_" + "a" * 64,
        source_id="source_" + "b" * 64,
        issuer="testbank",
        product_code="p1",
        pdf_sha256="c" * 64,
        attempt=1,
        occurred_at=datetime(2026, 10, 21, tzinfo=UTC),
        reason_code="provider_process_exit_unknown",
        reason="The OCR provider process exited for an unclassified reason.",
        error_class_category="ocr_provider_systemic",
        phase="primary",
        error_kind="process_exit",
        retryable=False,
        exit_code=17,
        stdout_size_bytes=3,
        stdout_sha256=hashlib.sha256(b"out").hexdigest(),
        provider="codex-exec",
        classification_basis="stderr_allowlist_no_match",
        chunk_index=0,
        page_start=1,
        page_end=1,
    )
    error = OCRSystemicFailureError(
        run_id="run-fix24",
        report_path=tmp_path / "ocr-systemic-failure.json",
        failure=failure,
    )

    async def failed(_resume: str | None) -> dict[str, Any]:
        raise error

    monkeypatch.setattr(cli_module, "_run", failed)
    from typer.testing import CliRunner

    result = CliRunner().invoke(cli_module.app, ["run"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["provider"] == "codex-exec"
    assert payload["phase"] == "primary"
    assert payload["classification_basis"] == "stderr_allowlist_no_match"
    assert payload["chunk_index"] == 0
    assert (payload["page_start"], payload["page_end"]) == (1, 1)
    assert payload["stdout_size_bytes"] == 3
    assert "RAW_FIX24" not in result.stdout
