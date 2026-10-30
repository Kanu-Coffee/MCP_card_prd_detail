"""Paddle aliases and explicit legacy recovery."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from cardrag_worker.capacity_v5 import V5CapacityError, preflight_worker_start_capacity
from cardrag_worker.paddle_cleanup import PaddleCleanupError, recover_paddle_aliases
from cardrag_worker.paddleocr_runner import _paddle_input_path
from cardrag_worker.providers import PaddleOCRVLProvider, ProviderSystemicError
from cardrag_worker.state import AlreadyRunning, worker_lock

RUN = "a" * 32


def _legacy(root: Path, branch: str = "fallback", target: Path | None = None) -> Path:
    base = root / "runs" / RUN / "documents" / ("doc_" + "a" * 64) / "ocr"
    if branch:
        base = base / branch
    base = base / "checkpoints"
    base.mkdir(parents=True, exist_ok=True)
    alias = base / ".paddle-input-1083.pdf"
    alias.symlink_to(target or root / "absent")
    return alias


def test_input_alias_is_outside_state_and_does_not_touch_legacy(tmp_path: Path) -> None:
    obj = tmp_path / "cas"
    obj.write_bytes(b"%PDF")
    stale = _legacy(tmp_path)
    with _paddle_input_path(obj, output_path=stale.parent / "result") as alias:
        assert alias.is_symlink() and alias.read_bytes() == b"%PDF"
        assert not alias.is_relative_to(tmp_path)
        assert stale.is_symlink()
    assert not os.path.lexists(alias)
    assert stale.is_symlink()


def test_explicit_cleanup_fixes_capacity_without_touching_target(tmp_path: Path) -> None:
    (tmp_path / "worker.lock").touch()
    obj = tmp_path / "pdf"
    obj.write_bytes(b"%PDF")
    aliases = [
        _legacy(tmp_path, branch, obj if branch == "fallback" else None)
        for branch in ("", "primary", "fallback")
    ]
    keep = aliases[-1].parent / ".paddle-input-123.pdf"
    keep.write_bytes(b"regular")
    unrelated = aliases[-1].parent / ".paddle-input-note.pdf"
    unrelated.symlink_to(obj)
    with pytest.raises(V5CapacityError, match="symlink"):
        preflight_worker_start_capacity(tmp_path, minimum_free_bytes=0)
    preview = recover_paddle_aliases(tmp_path, RUN)
    assert len(preview["entries"]) == 3
    assert all(os.path.lexists(p) for p in aliases)
    with worker_lock(tmp_path / "worker.lock"), pytest.raises(AlreadyRunning):
        recover_paddle_aliases(tmp_path, RUN, apply=True)
    assert all(os.path.lexists(p) for p in aliases)
    result = recover_paddle_aliases(tmp_path, RUN, apply=True)
    assert len(result["entries"]) == 3
    assert all(not os.path.lexists(p) for p in aliases)
    assert obj.read_bytes() == b"%PDF" and keep.read_bytes() == b"regular" and unrelated.is_symlink()
    with pytest.raises(V5CapacityError, match="symlink"):
        preflight_worker_start_capacity(tmp_path, minimum_free_bytes=0)
    unrelated.unlink()
    preflight_worker_start_capacity(tmp_path, minimum_free_bytes=0)
    assert recover_paddle_aliases(tmp_path, RUN, apply=True)["entries"] == []


def test_cleanup_refuses_symlink_parent_and_preserves_external_file(tmp_path: Path) -> None:
    (tmp_path / "worker.lock").touch()
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / ".paddle-input-1.pdf"
    sentinel.symlink_to(external / "target")
    ocr = tmp_path / "runs" / RUN / "documents" / ("doc_" + "a" * 64) / "ocr"
    ocr.mkdir(parents=True)
    (ocr / "fallback").symlink_to(external, target_is_directory=True)
    with pytest.raises(PaddleCleanupError, match="unsafe_directory"):
        recover_paddle_aliases(tmp_path, RUN, apply=True)
    assert sentinel.is_symlink()


def test_cleanup_rejects_bad_run_and_symlink_root(tmp_path: Path) -> None:
    (tmp_path / "worker.lock").touch()
    with pytest.raises(PaddleCleanupError, match="invalid_run_id"):
        recover_paddle_aliases(tmp_path, "run-9", apply=True)
    linked = tmp_path / "linked"
    linked.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(PaddleCleanupError):
        recover_paddle_aliases(linked, RUN, apply=True)


def _fake_child(tmp_path: Path) -> Path:
    child = tmp_path / "fake-paddle"
    child.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys, time
args = sys.argv
input_path = pathlib.Path(args[args.index("--input") + 1])
output_path = pathlib.Path(args[args.index("--output") + 1])
pathlib.Path(os.environ["TEST_PADDLE_RECORD"]).write_text(str(input_path))
pathlib.Path(os.environ["TEST_PADDLE_RECORD"] + ".pid").write_text(str(os.getpid()))
if os.environ.get("TEST_PADDLE_EXIT"):
    sys.exit(int(os.environ["TEST_PADDLE_EXIT"]))
assert input_path.suffix == ".pdf" and input_path.read_bytes() == b"%PDF"
if os.environ.get("TEST_PADDLE_WAIT"):
    time.sleep(30)
output_path.write_text(json.dumps({"pages": ["ok"]}))
""")
    child.chmod(0o755)
    return child


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["success", "timeout", "cancel", "spawn", "document_error", "config_error"])
async def test_provider_reaps_child_and_cleans_parent_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    record = tmp_path / "record"
    monkeypatch.setenv("TEST_PADDLE_RECORD", str(record))
    child = _fake_child(tmp_path)
    pdf = tmp_path / "cas-object"
    pdf.write_bytes(b"%PDF")
    output = tmp_path / "state" / "ocr" / "fallback"
    output.mkdir(parents=True)
    if mode in {"timeout", "cancel"}:
        monkeypatch.setenv("TEST_PADDLE_WAIT", "1")
    if mode in {"document_error", "config_error"}:
        monkeypatch.setenv("TEST_PADDLE_EXIT", "65" if mode == "document_error" else "70")
    provider = PaddleOCRVLProvider(
        cache_dir=tmp_path / "models",
        executable=str(child if mode != "spawn" else child / "missing"),
        timeout_seconds=0.5 if mode == "timeout" else 20,
    )
    task = asyncio.create_task(provider.recognize_document(pdf, expected_page_count=1, output_dir=output))
    if mode == "cancel":
        for _ in range(200):
            if record.exists():
                break
            await asyncio.sleep(0.01)
        assert record.exists()
        task.cancel()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif mode == "timeout":
        with pytest.raises(TimeoutError):
            await task
    elif mode == "spawn":
        with pytest.raises(ProviderSystemicError, match="started"):
            await task
    elif mode == "document_error":
        from cardrag_worker.providers import ProviderDocumentError

        with pytest.raises(ProviderDocumentError):
            await task
    elif mode == "config_error":
        with pytest.raises(ProviderSystemicError):
            await task
    else:
        assert await task == ("ok",)
    if record.exists():
        alias = Path(record.read_text())
        assert not alias.is_relative_to(tmp_path / "state")
        assert not os.path.lexists(alias)
        pid = int(record.with_suffix(".pid").read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    assert not list((tmp_path / "state").rglob(".paddle-input-*"))
    assert pdf.read_bytes() == b"%PDF"


def test_cleanup_refuses_directory_replacement_before_unlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "worker.lock").touch()
    original = _legacy(tmp_path)
    original_parent = original.parent
    replacement_parent = original_parent.with_name("original-checkpoints")
    real_readlink = os.readlink
    swapped = False

    def replace_after_read(path: object, *args: object, **kwargs: object) -> str:
        nonlocal swapped
        result = real_readlink(path, *args, **kwargs)
        if not swapped:
            swapped = True
            original_parent.rename(replacement_parent)
            original_parent.mkdir()
            (original_parent / original.name).symlink_to(tmp_path / "new-target")
        return result

    monkeypatch.setattr(os, "readlink", replace_after_read)
    with pytest.raises(PaddleCleanupError, match="path_changed"):
        recover_paddle_aliases(tmp_path, RUN, apply=True)
    assert (replacement_parent / original.name).is_symlink()
    assert original.is_symlink()


def test_capacity_diagnostics_only_expose_known_safe_alias_path(tmp_path: Path) -> None:
    path = tmp_path / "runs" / RUN / "documents" / ("doc_" + "f" * 64) / "ocr" / "fallback" / "checkpoints"
    path.mkdir(parents=True)
    alias = path / ".paddle-input-123.pdf"
    alias.symlink_to(tmp_path / "SECRET")
    with pytest.raises(V5CapacityError) as exc_info:
        preflight_worker_start_capacity(tmp_path, minimum_free_bytes=0)
    assert exc_info.value.details["path_kind"] == 1
    assert exc_info.value.safe_relative_path == str(alias.relative_to(tmp_path))
    alias.unlink()
    secret_name = tmp_path / "SECRET_TOKEN"
    secret_name.symlink_to(tmp_path / "missing")
    with pytest.raises(V5CapacityError) as exc_info:
        preflight_worker_start_capacity(tmp_path, minimum_free_bytes=0)
    assert exc_info.value.safe_relative_path is None
    assert "SECRET" not in str(exc_info.value)


def test_recovery_cli_dry_run_apply_and_busy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from typer.testing import CliRunner

    from cardrag_worker.cli import app

    (tmp_path / "worker.lock").touch()
    alias = _legacy(tmp_path)
    monkeypatch.setenv("CARDRAG_WORKER_STATE_DIR", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(app, ["paddle-alias-recovery", RUN])
    assert result.exit_code == 0 and json.loads(result.stdout)["mode"] == "dry_run"
    assert alias.is_symlink()
    with worker_lock(tmp_path / "worker.lock"):
        result = runner.invoke(app, ["paddle-alias-recovery", RUN, "--apply"])
    assert result.exit_code != 0 and json.loads(result.stdout)["reason_code"] == "worker_busy"
    assert alias.is_symlink()
    result = runner.invoke(app, ["paddle-alias-recovery", RUN, "--apply"])
    assert result.exit_code == 0 and json.loads(result.stdout)["mode"] == "apply"
    assert not os.path.lexists(alias)


def test_cleanup_missing_run_reports_error(tmp_path: Path) -> None:
    (tmp_path / "worker.lock").touch()
    with pytest.raises(PaddleCleanupError, match="run_unavailable"):
        recover_paddle_aliases(tmp_path, RUN)


@pytest.mark.asyncio
async def test_timeout_then_success_leaves_state_capacity_usable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = tmp_path / "record"
    monkeypatch.setenv("TEST_PADDLE_RECORD", str(record))
    monkeypatch.setenv("TEST_PADDLE_WAIT", "1")
    pdf = tmp_path / "cas"
    pdf.write_bytes(b"%PDF")
    state = tmp_path / "state"
    output = state / "ocr" / "fallback"
    output.mkdir(parents=True)
    provider = PaddleOCRVLProvider(
        cache_dir=tmp_path / "models", executable=str(_fake_child(tmp_path)), timeout_seconds=0.5
    )
    with pytest.raises(TimeoutError):
        await provider.recognize_document(pdf, expected_page_count=1, output_dir=output)
    monkeypatch.delenv("TEST_PADDLE_WAIT")
    assert await provider.recognize_document(pdf, expected_page_count=1, output_dir=output) == ("ok",)
    preflight_worker_start_capacity(state, minimum_free_bytes=0)


@pytest.mark.asyncio
async def test_repeated_cancel_waits_for_child_reap_before_alias_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = asyncio.Event()
    reaping = asyncio.Event()
    allow_reap = asyncio.Event()
    captured: dict[str, Path] = {}

    class Process:
        returncode = None

        async def communicate(self) -> tuple[bytes, bytes]:
            started.set()
            await asyncio.Event().wait()
            return b"", b""

        def kill(self) -> None:
            pass

        async def wait(self) -> int:
            reaping.set()
            await allow_reap.wait()
            return -9

    async def create(*args: str, **kwargs: object) -> Process:
        captured["alias"] = Path(args[args.index("--input") + 1])
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    pdf = tmp_path / "cas"
    pdf.write_bytes(b"%PDF")
    output = tmp_path / "state" / "fallback"
    output.mkdir(parents=True)
    provider = PaddleOCRVLProvider(cache_dir=tmp_path / "models")
    task = asyncio.create_task(provider.recognize_document(pdf, expected_page_count=1, output_dir=output))
    await started.wait()
    task.cancel()
    await reaping.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert captured["alias"].is_symlink()
    assert not task.done()
    allow_reap.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not os.path.lexists(captured["alias"])


@pytest.mark.asyncio
async def test_timeout_then_cancel_during_reap_propagates_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reaping = asyncio.Event()
    allow_reap = asyncio.Event()
    captured: dict[str, Path] = {}

    class SlowProcess:
        returncode = None

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.sleep(10)
            return b"", b""

        def kill(self) -> None:
            pass

        async def wait(self) -> int:
            reaping.set()
            await allow_reap.wait()
            return -9

    async def create(*args: str, **kwargs: object) -> SlowProcess:
        captured["alias"] = Path(args[args.index("--input") + 1])
        return SlowProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    pdf = tmp_path / "cas"
    pdf.write_bytes(b"%PDF")
    output = tmp_path / "state" / "fallback"
    output.mkdir(parents=True)
    provider = PaddleOCRVLProvider(cache_dir=tmp_path / "models", timeout_seconds=0.01)
    task = asyncio.create_task(provider.recognize_document(pdf, expected_page_count=1, output_dir=output))

    await reaping.wait()
    assert captured["alias"].is_symlink()
    assert not task.done()

    # First cancel during reap
    task.cancel()
    # Repeated cancel during reap
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    assert captured["alias"].is_symlink()

    allow_reap.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.cancelled()
    assert not os.path.lexists(captured["alias"])


@pytest.mark.asyncio
async def test_timeout_without_cancellation_preserves_timeout_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reaping = asyncio.Event()
    allow_reap = asyncio.Event()
    captured: dict[str, Path] = {}

    class SlowProcess:
        returncode = None

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.sleep(10)
            return b"", b""

        def kill(self) -> None:
            pass

        async def wait(self) -> int:
            reaping.set()
            await allow_reap.wait()
            return -9

    async def create(*args: str, **kwargs: object) -> SlowProcess:
        captured["alias"] = Path(args[args.index("--input") + 1])
        return SlowProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    pdf = tmp_path / "cas"
    pdf.write_bytes(b"%PDF")
    output = tmp_path / "state" / "fallback"
    output.mkdir(parents=True)
    provider = PaddleOCRVLProvider(cache_dir=tmp_path / "models", timeout_seconds=0.01)
    task = asyncio.create_task(provider.recognize_document(pdf, expected_page_count=1, output_dir=output))

    await reaping.wait()
    assert captured["alias"].is_symlink()
    assert not task.done()

    allow_reap.set()
    with pytest.raises(TimeoutError):
        await task

    assert not task.cancelled()
    assert not os.path.lexists(captured["alias"])


@pytest.mark.asyncio
async def test_signal_wrapper_handles_timeout_reap_cancellation_without_external_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import signal

    from cardrag_worker.cli import WorkerSignalShutdown, _operation_with_signal_shutdown

    loop = asyncio.get_running_loop()
    reaping = asyncio.Event()
    allow_reap = asyncio.Event()
    captured_callbacks: dict[int, Any] = {}

    def mock_add_signal_handler(sig: int, callback: Any, *args: Any) -> None:
        captured_callbacks[sig] = (callback, args)

    monkeypatch.setattr(loop, "add_signal_handler", mock_add_signal_handler)
    monkeypatch.setattr(loop, "remove_signal_handler", lambda _sig: True)

    class SlowProcess:
        returncode = None

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.sleep(10)
            return b"", b""

        def kill(self) -> None:
            pass

        async def wait(self) -> int:
            reaping.set()
            await allow_reap.wait()
            return -9

    async def create(*args: str, **kwargs: object) -> SlowProcess:
        return SlowProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    pdf = tmp_path / "cas"
    pdf.write_bytes(b"%PDF")
    output = tmp_path / "state" / "fallback"
    output.mkdir(parents=True)
    provider = PaddleOCRVLProvider(cache_dir=tmp_path / "models", timeout_seconds=0.01)

    async def operation() -> dict[str, Any]:
        await provider.recognize_document(pdf, expected_page_count=1, output_dir=output)
        return {"status": "completed"}

    wrapper_task = asyncio.create_task(
        _operation_with_signal_shutdown(operation, task_name="test-shutdown-on-timeout")
    )
    await reaping.wait()

    # Trigger signal handler callback directly without external signal
    assert signal.SIGTERM in captured_callbacks
    handler, args = captured_callbacks[signal.SIGTERM]
    handler(*args)
    # Repeated signal should be coalesced
    handler(*args)
    await asyncio.sleep(0)

    allow_reap.set()
    with pytest.raises(WorkerSignalShutdown) as exc_info:
        await wrapper_task

    assert exc_info.value.signal_number == signal.SIGTERM


def test_fifo_lock_rejected_in_subprocess_within_deadline(tmp_path: Path) -> None:
    import stat
    import subprocess
    import sys

    fifo = tmp_path / "worker.lock"
    os.mkfifo(fifo)
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("sentinel_value")
    alias = _legacy(tmp_path)

    for apply_mode in (False, True):
        code = f"""
import sys
from pathlib import Path
from cardrag_worker.paddle_cleanup import recover_paddle_aliases, PaddleCleanupError

try:
    recover_paddle_aliases(Path({repr(str(tmp_path))}), {repr(RUN)}, apply={apply_mode})
except PaddleCleanupError as exc:
    if "unsafe_lock" in str(exc):
        sys.exit(42)
    sys.exit(1)
sys.exit(0)
"""
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            proc.communicate(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise AssertionError(
                f"recover_paddle_aliases blocked on FIFO worker.lock (apply={apply_mode})"
            ) from None
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

        assert proc.returncode == 42

    assert stat.S_ISFIFO(fifo.stat().st_mode)
    assert sentinel.read_text() == "sentinel_value"
    assert alias.is_symlink()


@pytest.mark.parametrize("apply_mode", [False, True])
def test_lock_validation_refuses_unsafe_locks_and_preserves_state(tmp_path: Path, apply_mode: bool) -> None:
    import stat

    lock_path = tmp_path / "worker.lock"
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("keep")
    alias = _legacy(tmp_path)

    # 1. Missing lock
    if lock_path.exists() or lock_path.is_symlink():
        lock_path.unlink()
    with pytest.raises(PaddleCleanupError, match="unsafe_lock"):
        recover_paddle_aliases(tmp_path, RUN, apply=apply_mode)
    assert not lock_path.exists()
    assert sentinel.read_text() == "keep"
    assert alias.is_symlink()

    # 2. Symlink lock
    target_lock = tmp_path / "other.lock"
    target_lock.touch()
    lock_path.symlink_to(target_lock)
    with pytest.raises(PaddleCleanupError, match="unsafe_lock"):
        recover_paddle_aliases(tmp_path, RUN, apply=apply_mode)
    assert lock_path.is_symlink()
    assert sentinel.read_text() == "keep"
    assert alias.is_symlink()
    lock_path.unlink()

    # 3. Directory lock
    lock_path.mkdir()
    with pytest.raises(PaddleCleanupError, match="unsafe_lock"):
        recover_paddle_aliases(tmp_path, RUN, apply=apply_mode)
    assert lock_path.is_dir()
    assert sentinel.read_text() == "keep"
    assert alias.is_symlink()
    lock_path.rmdir()

    # 4. Hardlink lock (nlink > 1)
    target_lock.touch()
    os.link(target_lock, lock_path)
    with pytest.raises(PaddleCleanupError, match="unsafe_lock"):
        recover_paddle_aliases(tmp_path, RUN, apply=apply_mode)
    assert lock_path.is_file() and os.stat(lock_path).st_nlink > 1
    assert sentinel.read_text() == "keep"
    assert alias.is_symlink()
    lock_path.unlink()
    target_lock.unlink()

    # 5. FIFO lock
    os.mkfifo(lock_path)
    with pytest.raises(PaddleCleanupError, match="unsafe_lock"):
        recover_paddle_aliases(tmp_path, RUN, apply=apply_mode)
    assert stat.S_ISFIFO(lock_path.stat().st_mode)
    assert sentinel.read_text() == "keep"
    assert alias.is_symlink()
    lock_path.unlink()

    # 6. Busy regular lock
    lock_path.touch()
    with worker_lock(lock_path), pytest.raises(AlreadyRunning):
        recover_paddle_aliases(tmp_path, RUN, apply=apply_mode)
    assert lock_path.is_file()
    assert sentinel.read_text() == "keep"
    assert alias.is_symlink()

    # 7. Idle regular lock
    result = recover_paddle_aliases(tmp_path, RUN, apply=apply_mode)
    assert len(result["entries"]) == 1
    assert lock_path.is_file()
    assert sentinel.read_text() == "keep"
    if apply_mode:
        assert not os.path.lexists(alias)
    else:
        assert alias.is_symlink()
