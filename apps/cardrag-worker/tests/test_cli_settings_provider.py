from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from cardrag_core import SecretResolutionError
from typer.testing import CliRunner

import cardrag_worker.cli as cli_module
from cardrag_worker.adoption import AdoptionError
from cardrag_worker.pipeline import OCRDocumentFailuresError, OCRFailureRecord
from cardrag_worker.providers import CodexOCRProvider, ProviderError
from cardrag_worker.settings import WorkerSettings, _read_secret
from cardrag_worker.state import AlreadyRunning


def test_cli_already_running_is_success_and_resume_passes_exact_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def already_running(resume: str | None) -> dict[str, Any]:
        raise AlreadyRunning(f"busy:{resume}")

    monkeypatch.setattr(cli_module, "_run", already_running)
    result = CliRunner().invoke(cli_module.app, ["run"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "already_running"
    resumed = CliRunner().invoke(cli_module.app, ["resume", "run-123"])
    assert resumed.exit_code == 0
    assert "busy:run-123" in resumed.stdout


def test_cli_ocr_failure_aggregate_is_safe_bounded_and_exits_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_sentinel = "RAW_PROVIDER_STDERR_SECRET_SENTINEL"
    untrusted_product_name = "RAW_UNTRUSTED_PRODUCT_NAME\n<markup>"
    failure = OCRFailureRecord(
        issuer="kb",
        product_code="09072",
        product_name=untrusted_product_name,
        file_name="test.pdf",
        document_id="doc_" + "a" * 64,
        pdf_sha256="b" * 64,
        page_count=49,
        attempts=4,
        reason_code="provider_exit_17",
        reason="The OCR provider process exited with code 17.",
    )
    aggregate = OCRDocumentFailuresError(
        run_id="run-safe",
        report_path=tmp_path / "ocr-failures.json",
        failures=(failure,) * 7,
    )

    async def failed(_resume: str | None) -> dict[str, Any]:
        raise aggregate from ProviderError(raw_sentinel)

    monkeypatch.setattr(cli_module, "_run", failed)
    result = CliRunner().invoke(cli_module.app, ["run"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "failed"
    assert payload["reason_code"] == "ocr_document_failures"
    assert payload["ocr_failure_count"] == 7
    assert payload["report"] == "runs/run-safe/reports/ocr-failures.json"
    assert len(payload["sample"]) == 5
    assert "product_name" not in payload["sample"][0]
    assert untrusted_product_name not in result.stdout
    assert raw_sentinel not in result.stdout


def test_adoption_guard_cli_fails_closed_without_echoing_remote_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def blocked() -> dict[str, Any]:
        raise AdoptionError("stable exists; remote-content-must-stay-secret")

    monkeypatch.setattr(cli_module, "_guard_adoption_namespace", blocked)
    result = CliRunner().invoke(cli_module.app, ["adoption-guard"])
    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "stable_pointer_absent": False,
        "status": "blocked",
    }
    assert "remote-content-must-stay-secret" not in result.stdout


def test_adoption_audit_cli_reports_verified_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def verified(inventory: Path) -> dict[str, Any]:
        assert inventory == tmp_path
        return {"expected": 1558, "audited": 1558, "status": "verified"}

    monkeypatch.setattr(cli_module, "_audit_adoption_export", verified)
    result = CliRunner().invoke(cli_module.app, ["adoption-audit", str(tmp_path)])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "expected": 1558,
        "audited": 1558,
        "status": "verified",
    }


def test_secret_files_must_be_absolute_regular_single_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = tmp_path / "secret"
    secret.write_text("token\n", encoding="utf-8")
    monkeypatch.setenv("CARDRAG_OPENROUTER_API_KEY_FILE", str(secret))
    assert _read_secret("CARDRAG_OPENROUTER_API_KEY", required=True) == "token"

    monkeypatch.setenv("CARDRAG_OPENROUTER_API_KEY_FILE", "relative-secret")
    with pytest.raises(SecretResolutionError, match="absolute"):
        _read_secret("CARDRAG_OPENROUTER_API_KEY", required=True)

    monkeypatch.setenv("CARDRAG_OPENROUTER_API_KEY_FILE", str(secret))
    monkeypatch.setenv("CARDRAG_OPENROUTER_API_KEY", "direct")
    with pytest.raises(SecretResolutionError, match="only one"):
        _read_secret("CARDRAG_OPENROUTER_API_KEY", required=True)

    monkeypatch.delenv("CARDRAG_OPENROUTER_API_KEY")
    secret.write_text("line-one\nline-two\n", encoding="utf-8")
    with pytest.raises(SecretResolutionError, match="single"):
        _read_secret("CARDRAG_OPENROUTER_API_KEY", required=True)


def test_production_openrouter_base_url_requires_https_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CARDRAG_ENVIRONMENT", "production")
    for value in (
        "http://openrouter.example/api/v1",
        "https://user:secret@openrouter.example/api/v1",
        "https://openrouter.example/api/v1?token=secret",
        "https://openrouter.example/api/v1#fragment",
    ):
        monkeypatch.setenv("CARDRAG_OPENROUTER_BASE_URL", value)
        with pytest.raises(ValueError):
            WorkerSettings.from_env()

    monkeypatch.setenv("CARDRAG_OPENROUTER_BASE_URL", "https://openrouter.example/api/v1/")
    assert WorkerSettings.from_env().openrouter_base_url == "https://openrouter.example/api/v1"


def test_ocr_quality_defaults_and_configuration_are_validated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "CARDRAG_OCR_MODEL",
        "CARDRAG_OCR_REASONING_EFFORT",
        "CARDRAG_OCR_PROMPT_VERSION",
        "CARDRAG_OCR_PROVIDER_TIMEOUT_SECONDS",
        "CARDRAG_OCR_RENDER_SCALE_MILLI",
        "CARDRAG_OCR_WHOLE_DOCUMENT_MAX_PAGES",
        "CARDRAG_OCR_CONTEXT_PAGES_BEFORE",
        "CARDRAG_OCR_CONTEXT_PAGES_AFTER",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = WorkerSettings.from_env()
    assert settings.ocr_provider == "codex-exec"
    assert settings.ocr_model == "gpt-5.6-sol"
    assert settings.ocr_reasoning_effort == "high"
    assert settings.ocr_prompt_version == "cardrag-ocr.ko.v2"
    assert settings.ocr_provider_timeout_seconds == 1800
    assert settings.ocr_render_scale_milli == 6000
    assert settings.ocr_whole_document_max_pages == 4
    assert (settings.ocr_context_pages_before, settings.ocr_context_pages_after) == (1, 1)

    monkeypatch.setenv("CARDRAG_OCR_RENDER_SCALE_MILLI", "999")
    with pytest.raises(ValueError, match="CARDRAG_OCR_RENDER_SCALE_MILLI"):
        WorkerSettings.from_env()
    monkeypatch.setenv("CARDRAG_OCR_RENDER_SCALE_MILLI", "6000")
    monkeypatch.setenv("CARDRAG_OCR_PROVIDER_TIMEOUT_SECONDS", "0")
    with pytest.raises(ValueError, match="CARDRAG_OCR_PROVIDER_TIMEOUT_SECONDS"):
        WorkerSettings.from_env()


@pytest.mark.asyncio
async def test_codex_ocr_subprocess_is_explicitly_read_only_and_env_filtered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    images = tuple(tmp_path / f"page-{page}.png" for page in range(2, 6))
    for image in images:
        image.write_bytes(b"png")
    captured: dict[str, Any] = {}

    class Process:
        returncode = 0

        async def communicate(self, body: bytes) -> tuple[bytes, bytes]:
            captured["stdin"] = body
            return (
                "## Page 3\n\n충분히 긴 카드 상품설명 본문을 반환합니다.\n\n"
                "## Page 4\n\n다음 대상 페이지도 상품 문맥을 유지해 반환합니다.\n"
            ).encode(), b""

        def kill(self) -> None:
            return None

        async def wait(self) -> None:
            return None

    async def create(*args: str, **kwargs: Any) -> Process:
        captured["args"] = args
        captured["env"] = kwargs["env"]
        return Process()

    monkeypatch.setenv("SHOULD_NOT_LEAK", "secret")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    provider = CodexOCRProvider(
        executable="codex",
        model="gpt-5.6-sol",
        timeout_seconds=1800,
        reasoning_effort="high",
    )
    result = await provider.recognize(
        images,
        page_numbers=(2, 3, 4, 5),
        target_page_numbers=(3, 4),
        total_pages=5,
        prompt="transcribe",
    )
    arguments = captured["args"]
    sandbox_index = arguments.index("--sandbox")
    assert arguments[sandbox_index + 1] == "read-only"
    assert arguments[arguments.index("--model") + 1] == "gpt-5.6-sol"
    assert 'model_reasoning_effort="high"' in arguments
    assert "SHOULD_NOT_LEAK" not in captured["env"]
    stdin = captured["stdin"].decode("utf-8")
    assert "5 ordered pages in total" in stdin
    assert "Page 2 of 5: CONTEXT BEFORE" in stdin
    assert "Page 3 of 5: TARGET" in stdin
    assert "Page 4 of 5: TARGET" in stdin
    assert "Page 5 of 5: CONTEXT AFTER" in stdin
    assert "only these TARGET markers" in stdin
    assert provider.timeout_seconds == 1800
    assert result.startswith("## Page 3")
