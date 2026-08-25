from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from cardrag_core import SecretResolutionError
from typer.testing import CliRunner

import cardrag_worker.cli as cli_module
from cardrag_worker.providers import CodexOCRProvider
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


@pytest.mark.asyncio
async def test_codex_ocr_subprocess_is_explicitly_read_only_and_env_filtered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = tmp_path / "page.png"
    image.write_bytes(b"png")
    captured: dict[str, Any] = {}

    class Process:
        returncode = 0

        async def communicate(self, body: bytes) -> tuple[bytes, bytes]:
            captured["stdin"] = body
            return "## Page 1\n\n충분히 긴 카드 상품설명 본문을 반환합니다.\n".encode(), b""

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
    result = await CodexOCRProvider(executable="codex", model="gpt-5.4").recognize(
        (image,), first_page=1, prompt="transcribe"
    )
    arguments = captured["args"]
    sandbox_index = arguments.index("--sandbox")
    assert arguments[sandbox_index + 1] == "read-only"
    assert "SHOULD_NOT_LEAK" not in captured["env"]
    assert result.startswith("## Page 1")
