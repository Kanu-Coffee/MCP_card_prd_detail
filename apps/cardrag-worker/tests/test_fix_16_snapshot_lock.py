"""Maintenance snapshot fences and WAL-aware SQLite recovery evidence."""

from __future__ import annotations

import fcntl
import hashlib
import json
import runpy
import selectors
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "deploy/worker/snapshot-lock.py"


def test_snapshot_captures_committed_wal_and_evidence(tmp_path: Path) -> None:
    serving = tmp_path / "serving/v1/channels"
    serving.mkdir(parents=True)
    (serving / "stable.json").write_text(json.dumps({"generation_id": "generation-test"}))
    conns = []
    try:
        for name in ("worker-state.sqlite3", "backup-ledger.sqlite3"):
            conn = sqlite3.connect(tmp_path / name)
            conns.append(conn)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE run(run_id TEXT,status TEXT,started_at INT)")
            conn.execute("INSERT INTO run VALUES ('latest','succeeded',1)")
            conn.commit()
        result = runpy.run_path(str(SCRIPT))["snapshot"](tmp_path, tmp_path / "serving", "fixture")
        assert result["latest_run"] == ("latest", "succeeded")
        assert result["stable"]["generation_id"] == "generation-test"
        for name, info in result["databases"].items():
            output = tmp_path / "deployment-backups/fixture" / name
            assert hashlib.sha256(output.read_bytes()).hexdigest() == info["sha256"]
            with sqlite3.connect(output) as snapshot:
                assert snapshot.execute("SELECT run_id FROM run").fetchone()[0] == "latest"
        with pytest.raises(FileExistsError):
            runpy.run_path(str(SCRIPT))["snapshot"](tmp_path, tmp_path / "serving", "fixture")
    finally:
        for conn in conns:
            conn.close()


@pytest.mark.parametrize("lock", ["worker.lock", "backup.lock"])
def test_snapshot_holder_rejects_busy_fence(tmp_path: Path, lock: str) -> None:
    with (tmp_path / lock).open("a+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(  # noqa: S603 -- repository-owned script, argv only, no shell
            [
                sys.executable,
                str(SCRIPT),
                str(tmp_path),
                str(tmp_path),
                "test",
                "--hold-only",
                "--ready-dir",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode != 0
        assert not (tmp_path / "ready").exists()


def test_snapshot_holder_retains_fences_until_terminated(tmp_path: Path) -> None:
    proc = subprocess.Popen(  # noqa: S603 -- repository-owned script, argv only, no shell
        [
            sys.executable,
            str(SCRIPT),
            str(tmp_path),
            str(tmp_path),
            "test",
            "--hold-only",
            "--ready-dir",
            str(tmp_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=5)
        assert proc.stdout is not None and proc.stdout.readline().strip() == "READY"
        for lock in ("worker.lock", "backup.lock"):
            with (tmp_path / lock).open("a+b") as stream, pytest.raises(BlockingIOError):
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        proc.terminate()
        proc.communicate(timeout=5)
    for lock in ("worker.lock", "backup.lock"):
        with (tmp_path / lock).open("a+b") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
