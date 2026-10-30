from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_ocr import PDF_SHA, FakeProvider, fake_render

from cardrag_worker import ocr_logging
from cardrag_worker.ocr import FailoverOCRResolver, OCRResolver
from cardrag_worker.providers import OpenRouterOCRProvider, ProviderDocumentError, ProviderSystemicError
from cardrag_worker.state import WorkerState


def events(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [
        json.loads(r.message.split("OCR_EVENT ", 1)[1]) for r in caplog.records if "OCR_EVENT " in r.message
    ]


@pytest.mark.asyncio
async def test_validation_fallback_and_cache_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("cardrag_worker.ocr.render_pdf", fake_render)
    caplog.set_level(logging.INFO, logger="cardrag_worker.ocr")

    class BadProvider(FakeProvider):
        async def recognize(self, *args: Any, **kwargs: Any) -> str:
            return "## Page 99\n\nwrong page but no sensitive raw text"

    state = WorkerState(tmp_path / "state.sqlite3")
    token = ocr_logging.begin_invocation()
    ocr_logging.set_run_id("run")
    try:
        state.start_run(run_id="run")
        pdf = tmp_path / "pdf.txt"
        pdf.write_text("1")
        primary = OCRResolver(provider=BadProvider(), state=state, webdav=None, chunk_pages=1)  # type: ignore[arg-type]
        fallback_provider = FakeProvider()
        fallback_provider.provider = "local-fallback"
        fallback = OCRResolver(provider=fallback_provider, state=state, webdav=None, chunk_pages=1)  # type: ignore[arg-type]
        resolver = FailoverOCRResolver(primary, fallback)
        with ocr_logging.document_scope(
            document_id="doc_a", product_code="A123", product_name="테스트 카드", issuer="lotte"
        ):
            with pytest.warns(RuntimeWarning):
                result = await resolver.resolve(
                    run_id="run",
                    document_id="doc_a",
                    pdf_path=pdf,
                    pdf_sha256=PDF_SHA,
                    pdf_size_bytes=3,
                    page_count=1,
                    output_dir=tmp_path / "ocr",
                )
            ocr_logging.document_resolved(result)
        rows = events(caplog)
        starts = [x for x in rows if x["event"] == "ocr_provider_call_started"]
        assert [x["call_ordinal"] for x in starts] == [1, 2]
        assert [x["phase"] for x in starts] == ["primary", "fallback"]
        assert starts[0]["product_name"] == "테스트 카드" and starts[0]["product_code"] == "A123"
        invalid = next(x for x in rows if x["event"] == "ocr_output_validation_failed")
        failover = next(x for x in rows if x["event"] == "ocr_fallback_started")
        assert invalid["reason_code"] == failover["reason_code"] == "page_marker_mismatch"
        assert invalid["call_id"] == failover["call_id"] == starts[0]["call_id"]
        assert next(x for x in rows if x["event"] == "ocr_fallback_resolved")["provider_called"] is True
        assert (
            next(x for x in rows if x["event"] == "ocr_document_resolved")["document_provider_attempts"] == 2
        )
        caplog.clear()
        with ocr_logging.document_scope(document_id="doc_a"):
            await fallback.resolve(
                run_id="run",
                document_id="doc_a",
                pdf_path=pdf,
                pdf_sha256=PDF_SHA,
                pdf_size_bytes=3,
                page_count=1,
                output_dir=tmp_path / "ocr" / "fallback",
            )
        assert not [x for x in events(caplog) if x["event"] == "ocr_provider_call_started"]
    finally:
        ocr_logging.end_invocation(token, partial=False)
        state.close()
    summary = events(caplog)[-1]
    assert summary["provider_attempts_total"] == 2 and summary["provider_documents_total"] == 1


@pytest.mark.asyncio
async def test_concurrent_context_and_resume_counters(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="cardrag_worker.ocr")
    provider = FakeProvider()

    async def work(doc: str) -> None:
        with ocr_logging.document_scope(document_id=doc, product_code=doc):

            async def action() -> str:
                await asyncio.sleep(0)
                return "ok"

            await ocr_logging.provider_call(
                provider,
                action,
                call_kind="chunk",
                chunk_index=0,
                page_start=1,
                page_end=2,
                input_page_count=2,
            )

    token = ocr_logging.begin_invocation()
    ocr_logging.set_run_id("run")
    await asyncio.gather(work("A"), work("B"))
    ocr_logging.end_invocation(token, partial=False)
    token = ocr_logging.begin_invocation()
    ocr_logging.set_run_id("run")
    await work("C")
    ocr_logging.end_invocation(token, partial=False)
    rows = events(caplog)
    starts = [x for x in rows if x["event"] == "ocr_provider_call_started"]
    assert [x["call_ordinal"] for x in starts] == [1, 2, 1]
    assert starts[0]["invocation_id"] == starts[1]["invocation_id"] != starts[2]["invocation_id"]
    for start in starts:
        end = next(
            x for x in rows if x["event"] == "ocr_provider_call_returned" and x["call_id"] == start["call_id"]
        )
        assert end["document_id"] == end["product_code"] == start["document_id"]


@pytest.mark.asyncio
async def test_failure_cancel_and_sensitive_fields(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="cardrag_worker.ocr")
    token = ocr_logging.begin_invocation()
    ocr_logging.set_run_id("run")
    provider = FakeProvider()
    provider.model = "SECRET_MODEL\nforged"
    with ocr_logging.document_scope(document_id="doc", product_name="SECRET_VALUE", product_code="x\nforged"):

        async def fail() -> str:
            raise ProviderSystemicError(
                "provider_process_rate_limited",
                exit_code=1,
                stderr_size_bytes=7,
                stderr_sha256="a" * 64,
                classification_basis="stderr_allowlist",
            )

        with pytest.raises(ProviderSystemicError):
            await ocr_logging.provider_call(
                provider, fail, call_kind="chunk", chunk_index=0, page_start=1, page_end=1, input_page_count=1
            )

        async def cancel() -> str:
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await ocr_logging.provider_call(
                provider,
                cancel,
                call_kind="chunk",
                chunk_index=1,
                page_start=2,
                page_end=2,
                input_page_count=1,
            )
    ocr_logging.end_invocation(token, partial=True)
    rows = events(caplog)
    fail_row = next(x for x in rows if x["event"] == "ocr_provider_call_failed")
    assert fail_row["reason_code"] == "provider_process_rate_limited"
    assert fail_row["model"] is None and fail_row["product_name"] is None and fail_row["product_code"] is None
    assert "SECRET" not in caplog.text and "forged" not in caplog.text
    summary = rows[-1]
    assert summary["partial"] is True and summary["failed_total"] == summary["cancelled_total"] == 1
    assert all("\n" not in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_openrouter_internal_fallback_wire_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="cardrag_worker.ocr")
    provider = OpenRouterOCRProvider(
        api_key="private", model="primary-model", fallback_model="fallback-model"
    )
    image = tmp_path / "image.png"
    image.write_bytes(b"png")

    async def request(client: Any, model: str, content: Any, models: Any = None) -> str:
        if model == "primary-model":
            raise httpx.HTTPStatusError(
                "SECRET_BODY", request=httpx.Request("POST", "https://hidden"), response=httpx.Response(429)
            )
        return "successful response"

    monkeypatch.setattr(provider, "_post_chat_completion", request)
    token = ocr_logging.begin_invocation()
    with ocr_logging.document_scope(document_id="doc"):
        await ocr_logging.provider_call(
            provider,
            lambda: provider.recognize(
                (image,), page_numbers=(1,), target_page_numbers=(1,), total_pages=1, prompt="SECRET_PROMPT"
            ),
            call_kind="chunk",
            chunk_index=0,
            page_start=1,
            page_end=1,
            input_page_count=1,
        )
    ocr_logging.end_invocation(token, partial=False)
    rows = events(caplog)
    starts = [x for x in rows if x["event"] == "ocr_wire_started"]
    assert [x["requested_model"] for x in starts] == ["primary-model", "fallback-model"]
    assert [x["wire_ordinal"] for x in starts] == [1, 2]
    assert starts[0]["parent_call_id"] == starts[1]["parent_call_id"]
    assert all(x["actual_model"] is None for x in starts)
    assert next(x for x in rows if x["event"] == "ocr_wire_failed")["status_code"] == 429
    assert "SECRET" not in caplog.text and "hidden" not in caplog.text


@pytest.mark.asyncio
async def test_pipeline_supplies_real_product_scope_and_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from helpers import pdf_bytes
    from test_pipeline import Adapter, FakeEmbeddings, FakeWebDAV, install_http, source, successful_ocr_result

    from cardrag_worker.pipeline import WorkerPipeline, WorkerUnexpectedFailureError

    caplog.set_level(logging.INFO, logger="cardrag_worker.ocr")
    record = source(product_code="product-123")
    install_http(monkeypatch, pdf_bytes(), [])

    class LoggingOCR:
        contract = {"schema_version": "test-ocr.v1"}
        adoption_policy_version = "cardrag.legacy-ocr-adoption.v1"

        async def resolve(self, **kwargs: Any) -> Any:
            async def action() -> str:
                return "ok"

            await ocr_logging.provider_call(
                FakeProvider(),
                action,
                call_kind="chunk",
                chunk_index=0,
                page_start=1,
                page_end=1,
                input_page_count=1,
            )
            return successful_ocr_result()

    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[Adapter((record,))],
            ocr=LoggingOCR(),
            embeddings=FakeEmbeddings(),  # type: ignore[arg-type]
            webdav=FakeWebDAV(None),
            collect_remote_garbage=False,
        )  # type: ignore[arg-type]
        with pytest.raises(WorkerUnexpectedFailureError):
            await pipeline.run()
    rows = events(caplog)
    start = next(row for row in rows if row["event"] == "ocr_provider_call_started")
    assert start["run_id"] == rows[-1]["run_id"]
    assert start["issuer"] == "testbank" and start["product_code"] == "product-123"
    assert start["product_name"] == "테스트 카드" and start["stage_attempt"] == 1
    assert rows[-1]["event"] == "ocr_invocation_summary" and rows[-1]["partial"] is True


@pytest.mark.asyncio
async def test_diagnostic_logger_failure_does_not_change_provider_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("logging failed")

    monkeypatch.setattr(ocr_logging.LOGGER, "log", broken)
    token = ocr_logging.begin_invocation()
    try:
        with ocr_logging.document_scope(document_id="doc"):

            async def success() -> str:
                return "actual result"

            assert (
                await ocr_logging.provider_call(
                    FakeProvider(),
                    success,
                    call_kind="chunk",
                    chunk_index=0,
                    page_start=1,
                    page_end=1,
                    input_page_count=1,
                )
                == "actual result"
            )

            async def failure() -> str:
                raise ProviderSystemicError("provider_process_exit_unknown", exit_code=7)

            with pytest.raises(ProviderSystemicError, match="provider_process_exit_unknown"):
                await ocr_logging.provider_call(
                    FakeProvider(),
                    failure,
                    call_kind="chunk",
                    chunk_index=0,
                    page_start=1,
                    page_end=1,
                    input_page_count=1,
                )
    finally:
        ocr_logging.end_invocation(token, partial=True)


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (
            ProviderSystemicError("provider_process_authentication_failed", exit_code=1),
            "provider_process_authentication_failed",
        ),
        (
            ProviderSystemicError("provider_process_configuration_failed", exit_code=1),
            "provider_process_configuration_failed",
        ),
        (
            ProviderSystemicError("provider_process_network_error", exit_code=1),
            "provider_process_network_error",
        ),
        (
            ProviderSystemicError("provider_process_exit_unknown", exit_code=1),
            "provider_process_exit_unknown",
        ),
        (TimeoutError("SECRET_TIMEOUT"), "provider_timeout"),
        (httpx.ReadTimeout("SECRET_TIMEOUT"), "provider_timeout"),
        (ProviderDocumentError(exit_code=65), "provider_document_rejected"),
    ],
)
def test_diagnostic_classification_does_not_copy_error_text(error: Exception, reason: str) -> None:
    payload = ocr_logging.diagnostic(error)
    assert payload["reason_code"] == reason
    assert "SECRET" not in json.dumps(payload)
    if isinstance(error, ProviderDocumentError):
        assert payload["exit_code"] == 65


def test_actual_stderr_formatter_and_serialization_failure(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from cardrag_worker.cli import _configure_worker_logging

    logger = logging.getLogger("cardrag_worker")
    old_handlers, old_level, old_propagate = logger.handlers[:], logger.level, logger.propagate
    logger.handlers = []
    try:
        _configure_worker_logging()
        with ocr_logging.document_scope(product_name="ghp_" + "a" * 30, product_code="unsafe\nlog"):
            ocr_logging.emit("ocr_test", reason_code="provider_timeout")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert len(captured.err.splitlines()) == 1
        event = json.loads(captured.err.split("OCR_EVENT ", 1)[1])
        assert event["product_name"] is None and event["product_code"] is None
        assert "ghp_" not in captured.err and "unsafe\\nlog" not in captured.err
        assert event["product_name_omitted_reason"] == "unsafe_or_too_long"
        monkeypatch.setattr(
            ocr_logging.json, "dumps", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError())
        )
        ocr_logging.emit("ocr_test", reason_code="unknown")
    finally:
        logger.handlers = old_handlers
        logger.setLevel(old_level)
        logger.propagate = old_propagate


@pytest.mark.asyncio
async def test_pipeline_cancellation_emits_partial_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from test_pipeline import Adapter, FakeEmbeddings, FakeOCR, FakeWebDAV, source

    from cardrag_worker.pipeline import WorkerPipeline

    caplog.set_level(logging.INFO, logger="cardrag_worker.ocr")
    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[Adapter((source(),))],
            ocr=FakeOCR(),  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
        )

        async def interrupted(*, resume_run_id: str | None = None) -> Any:
            del resume_run_id
            ocr_logging.set_run_id("run")
            with ocr_logging.document_scope(document_id="doc"):

                async def cancel() -> str:
                    raise asyncio.CancelledError()

                await ocr_logging.provider_call(
                    FakeProvider(),
                    cancel,
                    call_kind="whole_document",
                    chunk_index=None,
                    page_start=1,
                    page_end=1,
                    input_page_count=1,
                )

        monkeypatch.setattr(pipeline, "_run_measured", interrupted)
        with pytest.raises(asyncio.CancelledError):
            await pipeline.run()
    summary = events(caplog)[-1]
    assert summary["event"] == "ocr_invocation_summary" and summary["partial"] is True
    assert summary["provider_attempts_total"] == 1 and summary["cancelled_total"] == 1


@pytest.mark.asyncio
async def test_checkpoint_reuse_counts_only_actual_provider_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from cardrag_worker.providers import ProviderError

    caplog.set_level(logging.INFO, logger="cardrag_worker.ocr")
    monkeypatch.setattr("cardrag_worker.ocr.render_pdf", fake_render)
    provider = FakeProvider(fail_calls={2})
    pdf = tmp_path / "pdf.txt"
    pdf.write_text("3")
    token = ocr_logging.begin_invocation()
    ocr_logging.set_run_id("run")
    try:
        with WorkerState(tmp_path / "state.sqlite3") as state:
            state.start_run(run_id="run")
            resolver = OCRResolver(
                provider=provider, state=state, webdav=None, chunk_pages=1, whole_document_max_pages=1
            )  # type: ignore[arg-type]
            args = dict(
                run_id="run",
                document_id="doc",
                pdf_path=pdf,
                pdf_sha256=PDF_SHA,
                pdf_size_bytes=3,
                page_count=3,
                output_dir=tmp_path / "ocr",
            )
            with ocr_logging.document_scope(document_id="doc"):
                with pytest.raises(ProviderError):
                    await resolver.resolve(**args)
                provider.fail_calls.clear()
                result = await resolver.resolve(**args)
            assert result.cache_reused and result.provider_called
            starts = [x for x in events(caplog) if x["event"] == "ocr_provider_call_started"]
            assert [x["chunk_index"] for x in starts] == [0, 1, 1, 2]
            assert [x["call_ordinal"] for x in starts] == [1, 2, 3, 4]
    finally:
        ocr_logging.end_invocation(token, partial=False)


# ── FIX_01 regression tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fix01_whole_document_final_verify_emits_validation_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """whole-document provider가 ## Page 99 마커를 본문에 포함할 때
    page-value 정규화는 통과하지만 verify_ocr_bytes에서 OCRVerificationError가 발생한다.
    이때 ocr_output_validation_failed 이벤트가 반환 이벤트 이후에 발생해야 하며
    원인 reason_code=final_ocr_verification_failed, validation_phase=final_whole_document,
    관련 call_id가 포함돼야 한다. 최종 예외는 OCRVerificationError여야 한다.
    """
    from cardrag_core.ocr import OCRVerificationError
    from test_ocr import FakeDocumentProvider

    monkeypatch.setattr("cardrag_worker.ocr.render_pdf", fake_render)
    caplog.set_level(logging.INFO, logger="cardrag_worker.ocr")

    class BadDocumentProvider(FakeDocumentProvider):
        """Returns a page body containing an extra ## Page 99 marker, which passes
        per-page normalization but causes verify_ocr_bytes to fail on final check."""

        async def recognize_document(
            self,
            pdf_path: Path,
            *,
            expected_page_count: int,
            output_dir: Path,
        ) -> tuple[str, ...]:
            # Body text embeds a spurious page marker; normalization will wrap it as a
            # regular page value, but the combined body will have markers 1 and 99.
            return ("## Page 99\n\nThis is a sufficiently long OCR page body.",)

    provider = BadDocumentProvider()
    pdf = tmp_path / "pdf.txt"
    pdf.write_text("1")

    token = ocr_logging.begin_invocation()
    ocr_logging.set_run_id("run-fix01-whole")
    try:
        with WorkerState(tmp_path / "state.sqlite3") as state:
            state.start_run(run_id="run-fix01-whole")
            resolver = OCRResolver(
                provider=provider,  # type: ignore[arg-type]
                state=state,
                webdav=None,
                chunk_pages=1,
                whole_document_max_pages=1,
            )
            with (
                ocr_logging.document_scope(
                    document_id="doc-fix01-whole",
                    product_code="FIX01",
                    product_name="테스트 픽스카드",
                    issuer="hana",
                ),
                pytest.raises(OCRVerificationError),
            ):
                await resolver.resolve(
                    run_id="run-fix01-whole",
                    document_id="doc-fix01-whole",
                    pdf_path=pdf,
                    pdf_sha256=PDF_SHA,
                    pdf_size_bytes=3,
                    page_count=1,
                    output_dir=tmp_path / "ocr",
                )
    finally:
        ocr_logging.end_invocation(token, partial=True)

    rows = events(caplog)
    started = [x for x in rows if x["event"] == "ocr_provider_call_started"]
    returned = [x for x in rows if x["event"] == "ocr_provider_call_returned"]
    failed_validation = [x for x in rows if x["event"] == "ocr_output_validation_failed"]

    assert len(started) == 1, f"Expected 1 start event, got {len(started)}"
    assert len(returned) == 1, "Expected 1 returned event (provider responded)"
    assert len(failed_validation) == 1, "Expected 1 validation_failed event"

    ev = failed_validation[0]
    assert ev["reason_code"] == "final_ocr_verification_failed"
    assert ev["validation_phase"] == "final_whole_document"
    assert ev.get("call_id") == started[0]["call_id"], "call_id must link to the started event"

    # The validation_failed event must appear after the returned event in log order
    returned_idx = rows.index(returned[0])
    failed_idx = rows.index(failed_validation[0])
    assert failed_idx > returned_idx, "validation_failed must follow call_returned"

    # Summary must reflect 1 attempt, 0 returned-success (attempt counted, not verified)
    summary = rows[-1]
    assert summary["event"] == "ocr_invocation_summary"
    assert summary["provider_attempts_total"] == 1
    # provider returned but final verification failed; returned count reflects call_returned
    assert summary["returned_total"] == 1


@pytest.mark.asyncio
async def test_fix01_chunk_combine_final_verify_emits_validation_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """chunk 결합 후 verify_ocr_bytes에서 NUL 바이트로 인해 OCRVerificationError가 발생할 때
    ocr_output_validation_failed 이벤트가 reason_code=final_ocr_verification_failed,
    validation_phase=final_chunk_combine으로 기록돼야 한다.
    원문 OCR 본문이나 sentinel은 이벤트나 caplog 텍스트에 없어야 한다.
    """
    from cardrag_core.ocr import OCRVerificationError

    monkeypatch.setattr("cardrag_worker.ocr.render_pdf", fake_render)
    caplog.set_level(logging.INFO, logger="cardrag_worker.ocr")

    # Use a page count ≥ 2 so chunked path is taken; each chunk returns a NUL-injected page
    class NulChunkProvider(FakeProvider):
        async def recognize(
            self,
            images: Any,
            *,
            page_numbers: Any,
            target_page_numbers: tuple[int, ...],
            total_pages: int,
            prompt: str,
        ) -> str:
            # Return proper marker but inject a NUL byte — this passes per-chunk validation
            # (NUL appears after page body is assembled) only if injected in the combined step.
            # We inject via the page body: NUL inside text so verify_ocr_bytes fails at combine.
            pages = []
            for pn in target_page_numbers:
                pages.append(f"## Page {pn}\n\n카드 혜택 내용\x00 forbidden.")
            return "\n\n".join(pages) + "\n"

    provider = NulChunkProvider()
    pdf = tmp_path / "pdf.txt"
    pdf.write_text("2")  # fake_render renders page-count based on file content int

    token = ocr_logging.begin_invocation()
    ocr_logging.set_run_id("run-fix01-chunk")
    try:
        with WorkerState(tmp_path / "state.sqlite3") as state:
            state.start_run(run_id="run-fix01-chunk")
            resolver = OCRResolver(
                provider=provider,  # type: ignore[arg-type]
                state=state,
                webdav=None,
                chunk_pages=1,  # force 1-page chunks so chunked path is used
                whole_document_max_pages=1,
            )
            with (
                ocr_logging.document_scope(
                    document_id="doc-fix01-chunk",
                    product_code="FIX01C",
                ),
                pytest.raises(OCRVerificationError),
            ):
                await resolver.resolve(
                    run_id="run-fix01-chunk",
                    document_id="doc-fix01-chunk",
                    pdf_path=pdf,
                    pdf_sha256=PDF_SHA,
                    pdf_size_bytes=3,
                    page_count=2,
                    output_dir=tmp_path / "ocr",
                )
    finally:
        ocr_logging.end_invocation(token, partial=True)

    rows = events(caplog)
    failed_validation = [x for x in rows if x["event"] == "ocr_output_validation_failed"]
    assert len(failed_validation) >= 1, "Expected at least 1 validation_failed event"

    # Find the final_chunk_combine one
    chunk_ev = next(
        (x for x in failed_validation if x.get("validation_phase") == "final_chunk_combine"),
        None,
    )
    assert chunk_ev is not None, "Expected validation_failed with validation_phase=final_chunk_combine"
    assert chunk_ev["reason_code"] == "final_ocr_verification_failed"
    assert chunk_ev.get("call_id") is None
    assert chunk_ev.get("chunk_index") is None
    assert chunk_ev.get("validation_scope") == "document"
    assert chunk_ev.get("page_start") == 1
    assert chunk_ev.get("page_end") == 2

    # Verify no raw OCR body, NUL, or forbidden sentinel in any log record
    sentinel = "\x00"
    for record in caplog.records:
        assert sentinel not in record.message, "Raw NUL byte must not appear in any log message"


@pytest.mark.asyncio
async def test_fix02_first_chunk_error_does_not_attribute_to_last_chunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """FIX_02 Reviewer 재현:
    2페이지 문서에서 1번째 청크 본문에만 NUL이 있고 2번째 청크는 정상일 때,
    결합 후 verify_ocr_bytes 검증 실패 이벤트는 정상인 2번째 청크의 call_id/chunk_index를
    실패 원인으로 복사하지 않아야 하며, call_id=None, chunk_index=None,
    validation_scope=document, page_start=1, page_end=2,
    last_call_id=정상청크호출ID 로 남겨야 한다.
    """
    from cardrag_core.ocr import OCRVerificationError

    monkeypatch.setattr("cardrag_worker.ocr.render_pdf", fake_render)
    caplog.set_level(logging.INFO, logger="cardrag_worker.ocr")

    class FirstChunkPoisonedProvider(FakeProvider):
        async def recognize(
            self,
            images: Any,
            *,
            page_numbers: Any,
            target_page_numbers: tuple[int, ...],
            total_pages: int,
            prompt: str,
        ) -> str:
            pages = []
            for pn in target_page_numbers:
                if pn == 1:
                    pages.append(f"## Page {pn}\n\n1페이지 카드 혜택\x00 NUL 포함 본문.")
                else:
                    pages.append(f"## Page {pn}\n\n2페이지 정상적인 카드 혜택 본문 내용입니다.")
            return "\n\n".join(pages) + "\n"

    provider = FirstChunkPoisonedProvider()
    pdf = tmp_path / "pdf.txt"
    pdf.write_text("2")

    token = ocr_logging.begin_invocation()
    ocr_logging.set_run_id("run-fix02-attribution")
    try:
        with WorkerState(tmp_path / "state.sqlite3") as state:
            state.start_run(run_id="run-fix02-attribution")
            resolver = OCRResolver(
                provider=provider,  # type: ignore[arg-type]
                state=state,
                webdav=None,
                chunk_pages=1,
                whole_document_max_pages=1,
            )
            with (
                ocr_logging.document_scope(
                    document_id="doc-fix02-attr",
                    product_code="FIX02",
                ),
                pytest.raises(OCRVerificationError),
            ):
                await resolver.resolve(
                    run_id="run-fix02-attribution",
                    document_id="doc-fix02-attr",
                    pdf_path=pdf,
                    pdf_sha256=PDF_SHA,
                    pdf_size_bytes=3,
                    page_count=2,
                    output_dir=tmp_path / "ocr",
                )
    finally:
        ocr_logging.end_invocation(token, partial=True)

    rows = events(caplog)
    starts = [x for x in rows if x["event"] == "ocr_provider_call_started"]
    assert len(starts) == 2, f"Expected 2 chunk calls, got {len(starts)}"
    call_a = starts[0]
    call_b = starts[1]
    assert call_a["chunk_index"] == 0 and call_a["page_start"] == 1
    assert call_b["chunk_index"] == 1 and call_b["page_start"] == 2

    failed = [x for x in rows if x["event"] == "ocr_output_validation_failed"]
    assert len(failed) == 1, "Expected 1 validation_failed event for combined verification"
    ev = failed[0]

    assert ev["validation_phase"] == "final_chunk_combine"
    assert ev["validation_scope"] == "document"
    assert ev["reason_code"] == "final_ocr_verification_failed"

    # 정상인 두 번째 청크(B)의 call_id 및 chunk_index를 검증 실패 원인으로 복사하지 않는다.
    assert ev.get("call_id") is None, "Combined validation failure must not attribute to call_id"
    assert ev.get("chunk_index") is None, "Combined validation failure must not attribute to chunk_index"
    # 직전 호출 참고 정보는 last_call_id에 남긴다.
    assert ev.get("last_call_id") == call_b["call_id"]
    # 범위는 마지막 청크(2..2)가 아닌 전체 문서 범위(1..2)여야 한다.
    assert ev["page_start"] == 1
    assert ev["page_end"] == 2
    assert ev["input_page_count"] == 2


@pytest.mark.asyncio
async def test_fix02_checkpoint_only_combine_failure_has_no_call_attribution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """모든 청크가 체크포인트에서 재사용되었을 때 최종 결합 검증이 실패하더라도,
    신규 provider attempt를 만들지 않으며 call_id와 last_call_id가 모두 없어야 한다.
    """
    from cardrag_core.ocr import OCRVerificationError

    from cardrag_worker.ocr import file_sha256

    monkeypatch.setattr("cardrag_worker.ocr.render_pdf", fake_render)
    caplog.set_level(logging.INFO, logger="cardrag_worker.ocr")

    provider = FakeProvider()
    pdf = tmp_path / "pdf.txt"
    pdf.write_text("2")

    # 1회차: 정상 실행하여 체크포인트 생성
    token1 = ocr_logging.begin_invocation()
    try:
        with WorkerState(tmp_path / "state.sqlite3") as state:
            state.start_run(run_id="run-chk-poison")
            resolver = OCRResolver(
                provider=provider,  # type: ignore[arg-type]
                state=state,
                webdav=None,
                chunk_pages=1,
                whole_document_max_pages=1,
            )
            with ocr_logging.document_scope(document_id="doc-chk", product_code="CHK"):
                await resolver.resolve(
                    run_id="run-chk-poison",
                    document_id="doc-chk",
                    pdf_path=pdf,
                    pdf_sha256=PDF_SHA,
                    pdf_size_bytes=3,
                    page_count=2,
                    output_dir=tmp_path / "ocr1",
                )
    finally:
        ocr_logging.end_invocation(token1, partial=False)

    # 체크포인트 0번 파일에 NUL 주입 후 state 메타데이터 갱신
    with WorkerState(tmp_path / "state.sqlite3") as state:
        chk0 = state.checkpoint("run-chk-poison", "doc-chk", "ocr", 0)
        assert chk0 is not None
        p0 = Path(chk0["artifact_path"])
        p0.write_text("## Page 1\n\n페이지 1 카드 혜택\x00 NUL 포함 본문입니다.\n", encoding="utf-8")
        state.save_checkpoint(
            run_id="run-chk-poison",
            document_id="doc-chk",
            stage_name="ocr",
            chunk_index=0,
            input_sha256=chk0["input_sha256"],
            output_sha256=file_sha256(p0),
            artifact_path=p0,
        )

    # 2회차: 새 invocation에서 실행. provider 호출 없이 체크포인트 재사용 후 결합 시 실패
    caplog.clear()
    provider.calls.clear()
    token2 = ocr_logging.begin_invocation()
    try:
        with WorkerState(tmp_path / "state.sqlite3") as state:
            resolver = OCRResolver(
                provider=provider,  # type: ignore[arg-type]
                state=state,
                webdav=None,
                chunk_pages=1,
                whole_document_max_pages=1,
            )
            with (
                ocr_logging.document_scope(document_id="doc-chk", product_code="CHK"),
                pytest.raises(OCRVerificationError),
            ):
                await resolver.resolve(
                    run_id="run-chk-poison",
                    document_id="doc-chk",
                    pdf_path=pdf,
                    pdf_sha256=PDF_SHA,
                    pdf_size_bytes=3,
                    page_count=2,
                    output_dir=tmp_path / "ocr2",
                )
    finally:
        ocr_logging.end_invocation(token2, partial=True)

    rows = events(caplog)
    starts = [x for x in rows if x["event"] == "ocr_provider_call_started"]
    assert len(starts) == 0, "No provider calls should be made when reusing checkpoints"

    failed = [x for x in rows if x["event"] == "ocr_output_validation_failed"]
    assert len(failed) == 1
    ev = failed[0]

    assert ev["validation_phase"] == "final_chunk_combine"
    assert ev["validation_scope"] == "document"
    assert ev.get("call_id") is None
    assert ev.get("chunk_index") is None
    assert ev.get("last_call_id") is None
    assert ev["page_start"] == 1
    assert ev["page_end"] == 2

    summary = rows[-1]
    assert summary["event"] == "ocr_invocation_summary"
    assert summary["provider_attempts_total"] == 0
