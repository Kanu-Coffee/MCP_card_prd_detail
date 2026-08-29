from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from cardrag_core import STABLE_POINTER_PATH, SecretResolutionError
from typer.testing import CliRunner

import cardrag_worker.cli as cli_module
import cardrag_worker.providers as providers_module
from cardrag_worker.adoption import AdoptionError
from cardrag_worker.gc import GCPartialFailure
from cardrag_worker.pipeline import (
    OCRDocumentFailuresError,
    OCRFailureRecord,
    OCRSystemicFailureError,
    OCRSystemicFailureRecord,
    PipelineResult,
    WorkerUnexpectedFailureError,
    WorkerUnexpectedFailureRecord,
)
from cardrag_worker.providers import (
    CodexOCRProvider,
    OpenRouterOCRProvider,
    ProviderError,
    ProviderSystemicError,
)
from cardrag_worker.settings import WorkerSettings, _read_secret
from cardrag_worker.state import AlreadyRunning


@pytest.mark.asyncio
async def test_signal_requested_during_handler_install_cancels_new_pipeline_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = False
    loop = asyncio.get_running_loop()

    async def must_not_run(_resume: str | None) -> dict[str, Any]:
        nonlocal entered
        entered = True
        return {"status": "unexpected"}

    def eager_signal_handler(
        signal_number: int,
        callback: Any,
        *args: Any,
    ) -> None:
        if signal_number == signal.SIGTERM:
            callback(*args)

    monkeypatch.setattr(cli_module, "_run", must_not_run)
    monkeypatch.setattr(loop, "add_signal_handler", eager_signal_handler)
    monkeypatch.setattr(loop, "remove_signal_handler", lambda _signal_number: True)

    with pytest.raises(cli_module.WorkerSignalShutdown) as captured:
        await cli_module._run_with_signal_shutdown(None)

    assert captured.value.signal_number == signal.SIGTERM
    assert entered is False


@pytest.mark.parametrize(
    ("shutdown_signal", "expected_exit"),
    [(signal.SIGTERM, 143), (signal.SIGINT, 130)],
    ids=["sigterm", "sigint"],
)
def test_run_cli_real_signal_drains_fenced_operation_and_records_terminal_truth(
    tmp_path: Path,
    shutdown_signal: signal.Signals,
    expected_exit: int,
) -> None:
    harness = Path(__file__).parent / "fixtures" / "signal_worker_harness.py"
    environment = os.environ.copy()
    environment["CARDRAG_SIGNAL_TEST_STATE_DIR"] = str(tmp_path)
    process = subprocess.Popen(  # noqa: S603
        [sys.executable, str(harness), "run"],  # noqa: S607
        cwd=Path(__file__).resolve().parents[3],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    started = tmp_path / "blocking-operation.started"
    release = tmp_path / "blocking-operation.release"
    finished = tmp_path / "blocking-operation.finished"
    deadline = time.monotonic() + 10
    while not started.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)

    try:
        assert started.is_file(), process.communicate(timeout=1)
        os.kill(process.pid, shutdown_signal)
        time.sleep(0.1)
        os.kill(process.pid, shutdown_signal)
        time.sleep(0.1)
        assert process.poll() is None

        with (tmp_path / "worker.lock").open("a+b") as contender, pytest.raises(BlockingIOError):
            fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        release.write_text("release", encoding="utf-8")
        stdout, stderr = process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    assert process.returncode == expected_exit, stderr
    assert json.loads(stdout) == {
        "exit_code": expected_exit,
        "reason": "Worker stopped after cancellation drain and terminal-state reconciliation.",
        "reason_code": "worker_signal_shutdown",
        "signal": shutdown_signal.name,
        "status": "shutdown_complete",
    }
    assert stderr.count("cancellation drain started") == 1
    assert stderr.count("Additional worker shutdown signal ignored") == 1
    assert "Traceback" not in stderr
    assert finished.read_text(encoding="utf-8") == started.read_text(encoding="utf-8")
    with sqlite3.connect(tmp_path / "worker-state.sqlite3") as connection:
        terminal = connection.execute("SELECT status,error FROM run").fetchone()
    assert terminal == (
        "interrupted",
        "worker_cancelled: Pipeline execution was interrupted.",
    )
    with (tmp_path / "worker.lock").open("a+b") as contender:
        fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(contender.fileno(), fcntl.LOCK_UN)


def test_signal_shutdown_cli_rejects_mutated_signal_without_secret_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_sentinel = "RAW_SIGNAL_URL_TOKEN_SECRET"
    failure = cli_module.WorkerSignalShutdown(int(signal.SIGTERM))
    failure.signal_number = raw_sentinel  # type: ignore[assignment]

    async def failed(_resume: str | None) -> dict[str, Any]:
        raise failure from RuntimeError(raw_sentinel)

    monkeypatch.setattr(cli_module, "_run_with_signal_shutdown", failed)
    result = CliRunner().invoke(cli_module.app, ["run"])

    assert result.exit_code == 143
    assert json.loads(result.stdout) == {
        "exit_code": 143,
        "reason": "Worker stopped after cancellation drain and terminal-state reconciliation.",
        "reason_code": "worker_signal_shutdown",
        "signal": "SIGTERM",
        "status": "shutdown_complete",
    }
    assert raw_sentinel not in result.output
    assert result.exception is not None
    rendered = "".join(
        traceback.format_exception(
            type(result.exception),
            result.exception,
            result.exception.__traceback__,
        )
    )
    assert raw_sentinel not in rendered


def test_worker_progress_logging_is_info_and_idempotent() -> None:
    logger = logging.getLogger("cardrag_worker")
    prior_handlers = list(logger.handlers)
    prior_level = logger.level
    prior_propagate = logger.propagate
    try:
        logger.handlers.clear()
        cli_module._configure_worker_logging()
        cli_module._configure_worker_logging()
        assert logger.level == logging.INFO
        assert logger.propagate is False
        assert len(logger.handlers) == 1
    finally:
        logger.handlers[:] = prior_handlers
        logger.setLevel(prior_level)
        logger.propagate = prior_propagate


def test_cli_already_running_is_success_and_resume_passes_exact_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_sentinel = "https://user:RAW_LOCK_TOKEN@example.test/private"

    async def already_running(resume: str | None) -> dict[str, Any]:
        assert resume in {None, "run-123"}
        raise AlreadyRunning(raw_sentinel)

    monkeypatch.setattr(cli_module, "_run", already_running)
    result = CliRunner().invoke(cli_module.app, ["run"])
    assert result.exit_code == 0
    expected = {
        "reason": "Worker did not start because the worker lock is held.",
        "reason_code": "worker_busy",
        "status": "already_running",
    }
    assert json.loads(result.stdout) == expected
    resumed = CliRunner().invoke(cli_module.app, ["resume", "run-123"])
    assert resumed.exit_code == 0
    assert json.loads(resumed.stdout) == expected
    assert raw_sentinel not in result.stdout + resumed.stdout


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (
            RuntimeError("https://user:RAW_TOKEN@example.test/private"),
            {
                "reason": "Remote garbage collection failed.",
                "reason_code": "remote_gc_failed",
                "status": "failed",
            },
        ),
        (
            AlreadyRunning("RAW_LOCK_PATH_TOKEN"),
            {
                "reason": ("Remote garbage collection did not start because the worker lock is held."),
                "reason_code": "remote_gc_busy",
                "status": "failed",
            },
        ),
    ],
)
def test_gc_cli_failure_boundary_emits_only_fixed_safe_json(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected: dict[str, Any],
) -> None:
    async def failed(*, apply: bool, retain: int, grace_days: int) -> dict[str, Any]:
        del apply, retain, grace_days
        raise failure

    monkeypatch.setattr(cli_module, "_run_gc", failed)
    result = CliRunner().invoke(cli_module.app, ["gc", "--apply"])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == expected
    assert "RAW_" not in result.output
    assert "example.test" not in result.output
    assert result.exception is not None
    rendered = "".join(
        traceback.format_exception(
            type(result.exception),
            result.exception,
            result.exception.__traceback__,
        )
    )
    assert "RAW_" not in rendered
    assert "example.test" not in rendered


def test_gc_cli_partial_failure_reports_known_count_without_source_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_sentinel = "https://user:RAW_PARTIAL_TOKEN@example.test/private"

    async def failed(*, apply: bool, retain: int, grace_days: int) -> dict[str, Any]:
        del apply, retain, grace_days
        raise GCPartialFailure(deleted_count=2) from RuntimeError(raw_sentinel)

    monkeypatch.setattr(cli_module, "_run_gc", failed)
    result = CliRunner().invoke(cli_module.app, ["gc", "--apply"])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "deleted_count": 2,
        "reason": "Remote garbage collection stopped after partial deletion.",
        "reason_code": "remote_gc_partial_failure",
        "status": "failed",
    }
    assert raw_sentinel not in result.output
    assert result.exception is not None
    rendered = "".join(
        traceback.format_exception(
            type(result.exception),
            result.exception,
            result.exception.__traceback__,
        )
    )
    assert raw_sentinel not in rendered


def test_gc_cli_rejects_mutated_partial_count_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_sentinel = "https://user:RAW_MUTATED_COUNT@example.test/private"
    failure = GCPartialFailure(deleted_count=1)
    failure.deleted_count = raw_sentinel  # type: ignore[assignment]

    async def failed(*, apply: bool, retain: int, grace_days: int) -> dict[str, Any]:
        del apply, retain, grace_days
        raise failure

    monkeypatch.setattr(cli_module, "_run_gc", failed)
    result = CliRunner().invoke(cli_module.app, ["gc", "--apply"])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "reason": "Remote garbage collection failed.",
        "reason_code": "remote_gc_failed",
        "status": "failed",
    }
    assert raw_sentinel not in result.output


def test_gc_runner_close_failure_does_not_replace_partial_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_sentinel = "RAW_CLOSE_URL_TOKEN_SECRET"

    class Settings:
        state_dir = tmp_path
        state_database = tmp_path / "state.sqlite3"
        lock_file = tmp_path / "worker.lock"
        channel = "stable"
        collect_remote_garbage = True
        stable_publication_approved = True
        remote_gc_approved = True

    class Client:
        pointer_path = STABLE_POINTER_PATH

        async def close(self) -> None:
            raise RuntimeError(raw_sentinel)

    async def partial(**_kwargs: Any) -> Any:
        raise GCPartialFailure(deleted_count=3) from None

    monkeypatch.setattr(cli_module.WorkerSettings, "from_env", lambda **_kwargs: Settings())
    monkeypatch.setattr(cli_module.WebDAVClient, "from_env", lambda **_kwargs: Client())
    monkeypatch.setattr(cli_module, "collect_garbage", partial)

    with pytest.raises(GCPartialFailure) as captured:
        asyncio.run(cli_module._run_gc(apply=True, retain=2, grace_days=30))

    assert captured.value.deleted_count == 3
    rendered = "".join(
        traceback.format_exception(
            type(captured.value),
            captured.value,
            captured.value.__traceback__,
        )
    )
    assert raw_sentinel not in rendered


def test_pipeline_result_payload_exposes_pdf_cache_activity() -> None:
    v5_metrics = {"schema_version": "cardrag.worker-v5-metrics.v2"}
    payload = cli_module._pipeline_result_payload(
        PipelineResult(
            run_id="run-cache-metrics",
            status="succeeded",
            corpus_sha256="a" * 64,
            contract_sha256="b" * 64,
            generation_id="gen-cache-metrics",
            document_count=10,
            evidence_count=20,
            pdf_cache_hits=7,
            pdf_cache_misses=3,
            pdf_downloads=2,
            pdf_revisions=1,
            ocr_cache_publication_deferred=2,
            v5_metrics=v5_metrics,
        )
    )

    assert payload["pdf_cache_hits"] == 7
    assert payload["pdf_cache_misses"] == 3
    assert payload["pdf_downloads"] == 2
    assert payload["pdf_revisions"] == 1
    assert payload["ocr_cache_publication_deferred"] == 2
    assert payload["v5_metrics"] == v5_metrics


def test_run_verifies_supplied_aggregation_profile_before_state_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_path = tmp_path / "document-aggregation-profile.json"
    state_root = tmp_path / "state"
    observed: dict[str, object] = {}

    class Settings:
        channel = "candidate-v1.0.10"
        stable_publication_approved = False
        document_aggregation_profile_path = profile_path
        document_aggregation_profile_artifact_sha256 = "a" * 64
        state_dir = state_root

    def reject_profile(path: Path, *, expected_artifact_sha256: str) -> None:
        observed.update(path=path, expected_artifact_sha256=expected_artifact_sha256)
        raise RuntimeError("injected_profile_rejection")

    monkeypatch.setattr(cli_module.WorkerSettings, "from_env", lambda **_kwargs: Settings())
    monkeypatch.setattr(cli_module, "load_verified_aggregation_profile_v5", reject_profile)
    monkeypatch.setattr(cli_module, "_configure_worker_logging", lambda: None)

    with pytest.raises(RuntimeError, match="injected_profile_rejection"):
        asyncio.run(cli_module._run(None))

    assert observed == {
        "path": profile_path,
        "expected_artifact_sha256": "a" * 64,
    }
    assert not state_root.exists()


@pytest.mark.parametrize(
    "head_failure",
    (
        "remote_m0_missing",
        "remote_m0_stale",
        "remote_head_identity_mismatch",
    ),
)
def test_run_rejects_aggregation_head_before_provider_or_state_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    head_failure: str,
) -> None:
    profile_path = tmp_path / "document-aggregation-profile.json"
    state_root = tmp_path / "state"
    selected = object()
    events: list[str] = []

    class Settings:
        channel = "candidate-v1.0.10"
        stable_publication_approved = False
        document_aggregation_profile_path = profile_path
        document_aggregation_profile_artifact_sha256 = "a" * 64
        state_dir = state_root

    class Client:
        async def close(self) -> None:
            events.append("webdav_close")

    def webdav_from_env(**_kwargs: object) -> Client:
        events.append("webdav_constructed")
        return Client()

    async def reject_head(
        _webdav: object,
        supplied: object,
        *,
        expected_m1_contract_sha256: str | None = None,
    ) -> None:
        assert supplied is selected
        assert expected_m1_contract_sha256 is None
        events.append("head_get_only_validation")
        raise RuntimeError(head_failure)

    async def provider_must_not_run(_settings: object) -> None:
        events.append("qwen_provider_preflight")

    def state_must_not_open(_path: Path) -> None:
        events.append("worker_state_opened")

    monkeypatch.setattr(cli_module.WorkerSettings, "from_env", lambda **_kwargs: Settings())
    monkeypatch.setattr(
        cli_module,
        "load_verified_aggregation_profile_v5",
        lambda *_args, **_kwargs: selected,
    )
    monkeypatch.setattr(cli_module.WebDAVClient, "from_env", webdav_from_env)
    monkeypatch.setattr(cli_module, "validate_document_aggregation_head", reject_head)
    monkeypatch.setattr(cli_module, "_qwen_embedding_provider", provider_must_not_run)
    monkeypatch.setattr(cli_module, "WorkerState", state_must_not_open)
    monkeypatch.setattr(cli_module, "_configure_worker_logging", lambda: None)

    with pytest.raises(RuntimeError, match=head_failure):
        asyncio.run(cli_module._run(None))

    assert events == ["webdav_constructed", "head_get_only_validation", "webdav_close"]
    assert not state_root.exists()


def test_run_without_aggregation_profile_preserves_m0_state_then_webdav_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"

    class Settings:
        channel = "candidate-v1.0.10"
        stable_publication_approved = False
        document_aggregation_profile_path = None
        document_aggregation_profile_artifact_sha256 = None
        state_dir = state_root

    def stop_at_webdav(**_kwargs: object) -> None:
        assert state_root.is_dir()
        raise RuntimeError("m0_webdav_stop")

    monkeypatch.setattr(cli_module.WorkerSettings, "from_env", lambda **_kwargs: Settings())
    monkeypatch.setattr(cli_module.WebDAVClient, "from_env", stop_at_webdav)
    monkeypatch.setattr(cli_module, "_configure_worker_logging", lambda: None)

    with pytest.raises(RuntimeError, match="m0_webdav_stop"):
        asyncio.run(cli_module._run(None))


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


def test_cli_systemic_ocr_failure_is_structured_and_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_sentinel = "RAW_WEBDAV_URL_CREDENTIAL_BODY"
    failure = OCRSystemicFailureRecord(
        run_id="run-systemic",
        document_id="doc_" + "a" * 64,
        source_id="source_" + "b" * 64,
        issuer="shinhan",
        product_code="00870",
        pdf_sha256="c" * 64,
        attempt=1,
        occurred_at=datetime(2026, 8, 28, 13, 48, 29, tzinfo=UTC),
        reason_code="ocr_cache_publication_ready_http",
        reason="OCR cache publication received an HTTP failure status",
        error_class_category="ocr_cache_publication",
        phase="ready",
        status_code=503,
        error_kind="http",
        retryable=False,
        publication_attempts=3,
    )
    error = OCRSystemicFailureError(
        run_id="run-systemic",
        report_path=tmp_path / "ocr-systemic-failure.json",
        failure=failure,
    )

    async def failed(_resume: str | None) -> dict[str, Any]:
        raise error from RuntimeError(raw_sentinel)

    monkeypatch.setattr(cli_module, "_run", failed)
    result = CliRunner().invoke(cli_module.app, ["run"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload == {
        "document_id": "doc_" + "a" * 64,
        "error_class_category": "ocr_cache_publication",
        "error_kind": "http",
        "issuer": "shinhan",
        "phase": "ready",
        "product_code": "00870",
        "publication_attempts": 3,
        "reason": "OCR cache publication received an HTTP failure status",
        "reason_code": "ocr_cache_publication_ready_http",
        "report": "runs/run-systemic/reports/ocr-systemic-failure.json",
        "retryable": False,
        "run_id": "run-systemic",
        "status": "failed",
        "status_code": 503,
    }
    assert raw_sentinel not in result.stdout


@pytest.mark.parametrize("command", [("run",), ("resume", "run-resume")])
def test_cli_worker_failure_is_structured_without_raw_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: tuple[str, ...],
) -> None:
    raw_sentinel = "RAW_GENERIC_PIPELINE_URL_TOKEN_SECRET"
    failure = WorkerUnexpectedFailureRecord(
        run_id="run-worker-failure",
        occurred_at=datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
        error_class_category="network",
    )
    error = WorkerUnexpectedFailureError(
        run_id=failure.run_id,
        report_path=tmp_path / "worker-failure.json",
        failure=failure,
    )

    async def failed(_resume: str | None) -> dict[str, Any]:
        raise error from RuntimeError(raw_sentinel)

    monkeypatch.setattr(cli_module, "_run", failed)
    result = CliRunner().invoke(cli_module.app, list(command))
    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "error_class_category": "network",
        "reason": "Worker pipeline failed unexpectedly.",
        "reason_code": "worker_unexpected_failure",
        "report": "runs/run-worker-failure/reports/worker-failure.json",
        "run_id": "run-worker-failure",
        "status": "failed",
    }
    assert raw_sentinel not in result.stdout


def test_cli_pre_pipeline_failure_uses_fixed_safe_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_sentinel = "RAW_SETTINGS_URL_TOKEN_SECRET"

    async def failed(_resume: str | None) -> dict[str, Any]:
        raise ValueError(raw_sentinel)

    monkeypatch.setattr(cli_module, "_run", failed)
    result = CliRunner().invoke(cli_module.app, ["run"])
    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "reason": "Worker pipeline failed unexpectedly.",
        "reason_code": "worker_unexpected_failure",
        "status": "failed",
    }
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


def test_embedding_response_caps_are_bounded_canonical_integers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CARDRAG_EMBEDDING_MAX_RESPONSE_BYTES", raising=False)
    monkeypatch.delenv("CARDRAG_EMBEDDING_METADATA_MAX_RESPONSE_BYTES", raising=False)
    settings = WorkerSettings.from_env()
    assert settings.embedding_max_response_bytes == 32 * 1024**2
    assert settings.embedding_metadata_max_response_bytes == 2 * 1024**2

    for name, value in (
        ("CARDRAG_EMBEDDING_MAX_RESPONSE_BYTES", "true"),
        ("CARDRAG_EMBEDDING_MAX_RESPONSE_BYTES", "-1"),
        ("CARDRAG_EMBEDDING_MAX_RESPONSE_BYTES", str(1 << 80)),
        ("CARDRAG_EMBEDDING_METADATA_MAX_RESPONSE_BYTES", "0"),
    ):
        monkeypatch.setenv(name, value)
        with pytest.raises(ValueError, match=name):
            WorkerSettings.from_env()
        monkeypatch.delenv(name)
        monkeypatch.delenv(name, raising=False)


def test_candidate_channel_and_two_generation_retention_are_validated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "CARDRAG_CHANNEL",
        "CARDRAG_STABLE_PUBLICATION_APPROVED",
        "CARDRAG_REMOTE_GC_APPROVED",
        "CARDRAG_RETAIN_GENERATIONS",
        "CARDRAG_RETAIN_INCOMPLETE_RUNS",
        "CARDRAG_COLLECT_REMOTE_GARBAGE",
    ):
        monkeypatch.delenv(name, raising=False)
    stable = WorkerSettings.from_env()
    assert stable.channel == "stable"
    assert stable.stable_publication_approved is False
    assert stable.remote_gc_approved is False
    assert stable.retain_generations == 2
    assert stable.retained_incomplete_runs == 2
    assert stable.collect_remote_garbage is False

    monkeypatch.setenv("CARDRAG_CHANNEL", "candidate-v1.0.9")
    monkeypatch.setenv("CARDRAG_COLLECT_REMOTE_GARBAGE", "false")
    candidate = WorkerSettings.from_env()
    assert candidate.channel == "candidate-v1.0.9"
    assert candidate.collect_remote_garbage is False

    monkeypatch.setenv("CARDRAG_CHANNEL", "../stable")
    with pytest.raises(ValueError, match="channel"):
        WorkerSettings.from_env()
    monkeypatch.setenv("CARDRAG_CHANNEL", "stable")
    monkeypatch.setenv("CARDRAG_RETAIN_GENERATIONS", "1")
    with pytest.raises(ValueError, match="CARDRAG_RETAIN_GENERATIONS"):
        WorkerSettings.from_env()


def test_remote_gc_requires_stable_channel_and_two_independent_approvals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CARDRAG_CHANNEL", "stable")
    monkeypatch.setenv("CARDRAG_COLLECT_REMOTE_GARBAGE", "true")
    monkeypatch.delenv("CARDRAG_STABLE_PUBLICATION_APPROVED", raising=False)
    monkeypatch.delenv("CARDRAG_REMOTE_GC_APPROVED", raising=False)

    with pytest.raises(ValueError, match="STABLE_PUBLICATION_APPROVED=true"):
        WorkerSettings.from_env()

    monkeypatch.setenv("CARDRAG_STABLE_PUBLICATION_APPROVED", "true")
    with pytest.raises(ValueError, match="REMOTE_GC_APPROVED=true"):
        WorkerSettings.from_env()

    monkeypatch.setenv("CARDRAG_REMOTE_GC_APPROVED", "true")
    approved = WorkerSettings.from_env()
    assert approved.collect_remote_garbage is True
    assert approved.stable_publication_approved is True
    assert approved.remote_gc_approved is True
    cli_module._guard_remote_gc(approved, apply=True)

    monkeypatch.setenv("CARDRAG_CHANNEL", "candidate-v1.0.10")
    with pytest.raises(ValueError, match="requires stable channel"):
        WorkerSettings.from_env()


def test_remote_gc_apply_guard_precedes_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CARDRAG_CHANNEL", "stable")
    monkeypatch.setenv("CARDRAG_COLLECT_REMOTE_GARBAGE", "false")
    monkeypatch.delenv("CARDRAG_STABLE_PUBLICATION_APPROVED", raising=False)
    monkeypatch.delenv("CARDRAG_REMOTE_GC_APPROVED", raising=False)
    settings = WorkerSettings.from_env()

    cli_module._guard_remote_gc(settings, apply=False)
    with pytest.raises(ValueError, match="separate remote-GC approval"):
        cli_module._guard_remote_gc(settings, apply=True)


def test_v110_publication_channel_requires_explicit_stable_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CARDRAG_CHANNEL", "candidate-v1.0.10")
    monkeypatch.delenv("CARDRAG_STABLE_PUBLICATION_APPROVED", raising=False)
    cli_module._guard_v110_publication_channel(WorkerSettings.from_env())

    monkeypatch.setenv("CARDRAG_CHANNEL", "stable")
    with pytest.raises(ValueError, match="explicit.*APPROVED=true"):
        cli_module._guard_v110_publication_channel(WorkerSettings.from_env())

    monkeypatch.setenv("CARDRAG_STABLE_PUBLICATION_APPROVED", "true")
    approved = WorkerSettings.from_env()
    assert approved.stable_publication_approved is True
    cli_module._guard_v110_publication_channel(approved)

    monkeypatch.setenv("CARDRAG_CHANNEL", "development")
    with pytest.raises(ValueError, match="candidate-v1.0.10 or stable"):
        cli_module._guard_v110_publication_channel(WorkerSettings.from_env())

    monkeypatch.setenv("CARDRAG_STABLE_PUBLICATION_APPROVED", "yes")
    with pytest.raises(ValueError, match="CARDRAG_STABLE_PUBLICATION_APPROVED"):
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


@pytest.mark.asyncio
async def test_codex_nonzero_exit_is_typed_systemic_and_discards_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = tmp_path / "page-1.png"
    image.write_bytes(b"png")
    raw_sentinel = "RAW_CODEX_STDERR_URL_TOKEN_SECRET"

    class Process:
        returncode = 17

        async def communicate(self, _body: bytes) -> tuple[bytes, bytes]:
            return b"", raw_sentinel.encode()

        def kill(self) -> None:
            return None

        async def wait(self) -> None:
            return None

    async def create(*_args: str, **_kwargs: Any) -> Process:
        return Process()

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
    assert error.reason_code == "provider_process_exit"
    assert error.error_kind == "process_exit"
    assert error.scope == "systemic"
    assert error.retryable is False
    assert error.exit_code == 17
    rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    assert raw_sentinel not in str(error)
    assert raw_sentinel not in rendered


@pytest.mark.asyncio
async def test_codex_spawn_error_is_typed_systemic_without_raw_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = tmp_path / "page-1.png"
    image.write_bytes(b"png")
    raw_sentinel = "/private/RAW_CODEX_EXECUTABLE_TOKEN"

    async def create(*_args: str, **_kwargs: Any) -> Any:
        raise FileNotFoundError(raw_sentinel)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    provider = CodexOCRProvider(executable=raw_sentinel, model="gpt-5.6-sol")
    with pytest.raises(ProviderSystemicError) as captured:
        await provider.recognize(
            (image,),
            page_numbers=(1,),
            target_page_numbers=(1,),
            total_pages=1,
            prompt="transcribe",
        )

    error = captured.value
    assert error.reason_code == "provider_process_spawn_failed"
    assert error.error_kind == "process_spawn"
    assert error.exit_code is None
    assert error.__cause__ is None
    rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    assert raw_sentinel not in rendered


@pytest.mark.asyncio
async def test_openrouter_invalid_ocr_contract_is_typed_systemic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = tmp_path / "page-1.png"
    image.write_bytes(b"png")

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"RAW_PRIVATE_RESPONSE_TOKEN": "secret"}

    class Client:
        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, *_args: object, **_kwargs: object) -> Response:
            return Response()

    monkeypatch.setattr(providers_module.httpx, "AsyncClient", lambda **_kwargs: Client())
    provider = OpenRouterOCRProvider(api_key="secret", model="test-model")
    with pytest.raises(ProviderSystemicError) as captured:
        await provider.recognize(
            (image,),
            page_numbers=(1,),
            target_page_numbers=(1,),
            total_pages=1,
            prompt="transcribe",
        )

    error = captured.value
    assert error.reason_code == "provider_contract_invalid"
    assert error.error_kind == "contract"
    assert error.scope == "systemic"
    assert error.__cause__ is None
    assert "RAW_PRIVATE_RESPONSE_TOKEN" not in str(error)
