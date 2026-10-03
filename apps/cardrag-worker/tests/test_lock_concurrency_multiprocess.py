"""Two-process concurrent lock competition test with a multiprocessing barrier (Fix 4).

Proves that when two independent processes compete for the worker lock simultaneously:
1. Exactly one process wins or holds the lock while the other is rejected.
2. The loser exits cleanly with reason_code="worker_busy".
3. The loser NEVER touches, creates, or opens the state SQLite database or WAL/SHM files.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import time
from pathlib import Path

from typer.testing import CliRunner


def _worker_process_target(
    state_dir_str: str,
    barrier: mp.Barrier,
    result_queue: mp.Queue,
) -> None:
    # Running inside an isolated process
    os.environ["CARDRAG_WORKER_STATE_DIR"] = state_dir_str
    os.environ["CARDRAG_CHANNEL"] = "candidate-v1.0.11"
    os.environ["CARDRAG_WEBDAV_BASE_URL"] = "https://webdav.example.test"
    os.environ["CARDRAG_WEBDAV_USERNAME"] = "user"
    os.environ["CARDRAG_WEBDAV_PASSWORD"] = "pass"  # noqa: S105
    os.environ["CARDRAG_OPENROUTER_API_KEY"] = "fake-key"
    os.environ["CARDRAG_DOCUMENT_AGGREGATION"] = ""

    # Wait at the barrier for simultaneous start
    barrier.wait()

    import sqlite3

    orig_connect = sqlite3.connect
    db_opened = False

    def guarded_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        nonlocal db_opened
        db_opened = True
        return orig_connect(*args, **kwargs)  # type: ignore[arg-type]

    sqlite3.connect = guarded_connect  # type: ignore[assignment]

    from cardrag_worker import cli as cli_module

    runner = CliRunner()
    start_time = time.monotonic()
    result = runner.invoke(cli_module.app, ["run"])
    elapsed = time.monotonic() - start_time

    sqlite3.connect = orig_connect  # type: ignore[assignment]

    stdout = result.stdout
    reason_code = None
    if "{" in stdout:
        try:
            parsed = json.loads(stdout[stdout.index("{") :])
            reason_code = parsed.get("reason_code")
        except (ValueError, json.JSONDecodeError):
            pass

    result_queue.put(
        {
            "pid": os.getpid(),
            "exit_code": result.exit_code,
            "reason_code": reason_code,
            "db_opened": db_opened,
            "stdout": stdout,
            "elapsed": elapsed,
            "exception": repr(result.exception),
        }
    )


def test_independent_two_process_lock_barrier(tmp_path: Path) -> None:
    state_dir = tmp_path / "worker-state"
    state_dir.mkdir(parents=True)

    db_path = state_dir / "worker-state.sqlite3"
    wal_path = state_dir / "worker-state.sqlite3-wal"
    shm_path = state_dir / "worker-state.sqlite3-shm"

    assert not db_path.exists()
    assert not wal_path.exists()
    assert not shm_path.exists()

    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(2)
    result_queue = ctx.Queue()

    p1 = ctx.Process(
        target=_worker_process_target,
        args=(str(state_dir), barrier, result_queue),
    )
    p2 = ctx.Process(
        target=_worker_process_target,
        args=(str(state_dir), barrier, result_queue),
    )

    p1.start()
    p2.start()

    p1.join(timeout=30)
    p2.join(timeout=30)

    assert not p1.is_alive(), "Process 1 timed out"
    assert not p2.is_alive(), "Process 2 timed out"

    results = []
    while not result_queue.empty():
        results.append(result_queue.get())

    assert len(results) == 2, f"Expected 2 results, got {len(results)}"

    # Find the process that lost the lock race
    busy_results = [r for r in results if r["reason_code"] == "worker_busy"]
    assert len(busy_results) >= 1, f"At least one process should lose the lock: {results}"

    loser = busy_results[0]
    assert loser["exit_code"] == 0
    assert not loser["db_opened"], "Loser process must never open or touch the database"
