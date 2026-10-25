"""Quota failover preserves document/chunk boundaries and terminal errors."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from cardrag_core import ContentOCRArtifactManifest, sha256_bytes
from test_content_cache import _variant
from test_ocr import PDF_SHA, FakeProvider, fake_render

from cardrag_worker.content_cache import ContentOCRVariantStore
from cardrag_worker.ocr import FailoverOCRResolver, OCRResolver
from cardrag_worker.pipeline import WorkerPipeline, WorkerUnexpectedFailureError
from cardrag_worker.providers import ProviderSystemicError
from cardrag_worker.state import WorkerState


class QuotaCodex(FakeProvider):
    async def recognize(self, *args: Any, **kwargs: Any) -> str:
        if kwargs["target_page_numbers"][0] == 2 and self.fail_calls:
            self.calls.append(2)
            error = ProviderSystemicError("provider_process_rate_limited", exit_code=1)
            error.args = ("PRIVATE_STDERR_SENTINEL",)
            error.add_note("PRIVATE_STDERR_SENTINEL")
            raise error
        return await super().recognize(*args, **kwargs)


class LocalPaddle:
    provider = "local-paddleocr"
    model = "PaddleOCR-VL-1.6"
    reasoning_effort = None
    renderer_id = "paddlex-pdfium/300dpi"
    render_scale_milli = 4167

    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    async def recognize_document(self, *args: Any, **kwargs: Any) -> tuple[str, ...]:
        self.calls += 1
        assert kwargs["expected_page_count"] == 2
        if self.fail:
            raise ProviderSystemicError("provider_systemic_failure")
        return (
            "로컬 Paddle 첫 페이지는 전월 이용금액 조건과 할인한도를 안내합니다.",
            "로컬 Paddle 둘째 페이지는 카드 혜택의 제외사항을 안내합니다.",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback_fails", [False, True])
async def test_codex_quota_uses_whole_local_document_and_preserves_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fallback_fails: bool
) -> None:
    monkeypatch.setattr("cardrag_worker.ocr.render_pdf", fake_render)
    primary_provider = QuotaCodex(fail_calls={99})
    fallback_provider = LocalPaddle(fail=fallback_fails)
    with WorkerState(tmp_path / "state.sqlite3") as state:
        run_id = state.start_run(run_id="quota-run")
        primary = OCRResolver(
            provider=primary_provider,
            state=state,
            webdav=None,
            chunk_pages=1,
            whole_document_max_pages=1,
        )
        fallback = OCRResolver(provider=fallback_provider, state=state, webdav=None)
        resolver = FailoverOCRResolver(primary, fallback)
        contract_before = dict(resolver.contract)
        pdf = tmp_path / "pdf.txt"
        pdf.write_text("2")
        arguments = dict(
            run_id=run_id,
            document_id="doc_quota",
            pdf_path=pdf,
            pdf_sha256=PDF_SHA,
            pdf_size_bytes=3,
            page_count=2,
            output_dir=tmp_path / "ocr",
        )
        with pytest.warns(RuntimeWarning, match="whole-document fallback") as warnings:
            if fallback_fails:
                with pytest.raises(ProviderSystemicError) as failure:
                    await resolver.resolve(**arguments)
                assert failure.value.reason_code == "provider_systemic_failure"
                assert failure.value.__context__ is None
            else:
                result = await resolver.resolve(**arguments)
                assert result.provider == "local-paddleocr"
                assert len(result.pages) == 2
                assert result.ocr_bytes == (tmp_path / "ocr/fallback/ocr.md").read_bytes()
                assert all("로컬 Paddle" in page for page in result.pages)
                assert "페이지 1 카드" not in result.ocr_text  # no primary/fallback text mixing
        assert "PRIVATE_STDERR_SENTINEL" not in "".join(str(w.message) for w in warnings)
        assert primary_provider.calls == [1, 2]
        assert fallback_provider.calls == 1
        checkpoint = state.checkpoint(run_id, "doc_quota", "ocr", 0)
        assert checkpoint is not None
        chunk = Path(checkpoint["artifact_path"])
        assert chunk.is_file() and sha256_bytes(chunk.read_bytes()) == checkpoint["output_sha256"]
        assert resolver.contract == contract_before
        if fallback_fails:
            primary_provider.fail_calls.clear()
            result = await resolver.resolve(**arguments)
            assert len(result.pages) == 2
            assert primary_provider.calls == [1, 2, 2]  # retained chunk 0 is not called again
            assert state.checkpoint(run_id, "doc_quota", "ocr", 0) == checkpoint
            assert fallback_provider.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "primary_name", "fallback_name"),
    [
        ("provider_process_authentication_failed", "codex-exec", "local-paddleocr"),
        ("provider_process_configuration_failed", "codex-exec", "local-paddleocr"),
        ("provider_process_exit_unknown", "codex-exec", "local-paddleocr"),
        ("provider_process_network_error", "codex-exec", "local-paddleocr"),
        ("provider_output_credential_detected", "codex-exec", "local-paddleocr"),
        ("provider_contract_invalid", "codex-exec", "local-paddleocr"),
        ("provider_process_rate_limited", "opencode", "local-paddleocr"),
        ("provider_process_rate_limited", "codex-exec", "openrouter"),
    ],
)
async def test_other_systemic_errors_and_provider_pairs_still_stop(
    tmp_path: Path, reason: Any, primary_name: str, fallback_name: str
) -> None:
    error = ProviderSystemicError(reason, exit_code=1 if reason.startswith("provider_process_") else None)

    class Primary:
        adoption_policy_version = "test"
        cache_mode = "read-write"
        contract = {"schema_version": "primary"}
        provider = SimpleNamespace(provider=primary_name)

        async def resolve(self, **kwargs: Any) -> None:
            raise error

    class Fallback(Primary):
        provider = SimpleNamespace(provider=fallback_name)

        async def resolve(self, **kwargs: Any) -> None:
            pytest.fail("non-allowlisted errors must not call fallback")

    resolver = FailoverOCRResolver(Primary(), Fallback())  # type: ignore[arg-type]
    with pytest.raises(ProviderSystemicError) as failure:
        await resolver.resolve(output_dir=tmp_path / "ocr")
    assert failure.value is error


@pytest.mark.asyncio
async def test_content_sibling_manifest_cannot_impersonate_sealed_variant(tmp_path: Path) -> None:
    first, body = _variant(
        created_at=datetime(2026, 10, 20, tzinfo=UTC), provider="codex-exec", text="카드 혜택 안내입니다."
    )
    sibling = ContentOCRArtifactManifest.create(
        source=first.source,
        cache_epoch=first.cache_epoch,
        output=first.output,
        ocr_chars=first.ocr_chars,
        page_output_sha256=first.page_output_sha256,
        created_at=first.created_at + timedelta(seconds=1),
        provenance=first.provenance,
    )
    prior = tmp_path / "runs/prior"
    ocr = prior / "documents/doc_test/ocr"
    ocr.mkdir(parents=True)
    (ocr / "ocr.md").write_bytes(body)
    (ocr / "content-manifest.json").write_bytes(sibling.canonical_bytes())
    (prior / "sealed").mkdir()
    (prior / "sealed/publish.json").write_text(
        json.dumps(
            {
                "manifest": {
                    "generation_id": "g-prior",
                    "created_at": first.created_at.isoformat(),
                    "documents": [
                        {
                            "document_id": "doc_test",
                            "availability": "available",
                            "pdf": {
                                "sha256": first.source.pdf_sha256,
                                "size_bytes": first.source.pdf_size_bytes,
                            },
                            "page_count": first.source.page_count,
                            "ocr": {"sha256": first.output.sha256, "size_bytes": len(body)},
                            "ocr_cache_kind": "content",
                            "ocr_reuse_key": first.reuse_key,
                            "ocr_variant_id": first.variant_id,
                        }
                    ],
                }
            }
        )
    )
    hit = await ContentOCRVariantStore(webdav=None, state_root=tmp_path).lookup(
        run_id="current",
        document_id="doc_test",
        source=first.source,
        cache_epoch=0,
        expected_ocr_identity=(first.output.sha256, len(body)),
    )
    assert hit is not None and hit.body == body
    assert hit.manifest.variant_id != sibling.variant_id
    assert hit.manifest.variant_id == hit.manifest.expected_variant_id


@pytest.mark.asyncio
async def test_discovery_database_error_keeps_safe_diagnostic(tmp_path: Path) -> None:
    pipeline = MagicMock(spec=WorkerPipeline)
    pipeline.state_dir = tmp_path
    pipeline.state = MagicMock()
    pipeline.performance = MagicMock()
    pipeline.state.get_stage.return_value = None
    pipeline.state.stage_started.return_value = 1
    pipeline.state.stage_succeeded.side_effect = RuntimeError("PRIVATE_DB_ERROR")
    pipeline.execution_plan = None
    pipeline.maximum_attempts = 1

    async def discover() -> str:
        return "done"

    with pytest.raises(WorkerUnexpectedFailureError) as failure:
        await WorkerPipeline._finite_stage(
            pipeline,
            run_id="run",
            document_id="issuer-lotte",
            name="discovery",
            operation=discover,
        )
    payload = failure.value.failure.payload
    assert payload["reason_code"] == "stage_bookkeeping_failed"
    assert payload["stage"] == "discovery" and "document_id" not in payload
    assert "PRIVATE_DB_ERROR" not in json.dumps(payload)
