from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cardrag_worker.state import AlreadyRunning, WorkerState, worker_lock


def test_stage_terminal_failure_does_not_require_exhausted_attempts(tmp_path: Path) -> None:
    with WorkerState(tmp_path / "state.sqlite3") as state:
        run_id = state.start_run(run_id="run-terminal")
        state.ensure_stage(run_id, "doc", "ocr", max_attempts=4)
        assert state.stage_started(run_id, "doc", "ocr") == 1
        state.stage_terminal_failed(run_id, "doc", "ocr", "safe terminal error")
        stage = state.get_stage(run_id, "doc", "ocr")
        assert stage is not None
        assert (stage.status, stage.attempt_count, stage.max_attempts, stage.last_error) == (
            "failed",
            1,
            4,
            "safe terminal error",
        )


def test_state_uses_wal_and_resume_resets_attempts_and_refreshes_discovery(tmp_path: Path) -> None:
    with WorkerState(tmp_path / "state.sqlite3") as state:
        assert state.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        run_id = state.start_run(run_id="run-1")
        state.ensure_stage(run_id, "doc", "ocr", max_attempts=1)
        state.ensure_stage(run_id, "protected", "download", max_attempts=1)
        state.ensure_stage(run_id, "issuer-kb", "discovery", max_attempts=1)
        assert state.stage_started(run_id, "issuer-kb", "discovery") == 1
        state.stage_succeeded(run_id, "issuer-kb", "discovery")
        assert state.stage_started(run_id, "doc", "ocr") == 1
        assert state.stage_failed(run_id, "doc", "ocr", RuntimeError("boom"), delay_seconds=0) == "failed"
        assert state.stage_started(run_id, "protected", "download") == 1
        state.stage_skipped(run_id, "protected", "download", "unsupported_drm kb/04130")
        assert state.stage_status_count(run_id, "download", "skipped") == 1
        artifact = tmp_path / "chunk.md"
        artifact.write_text("checkpoint", encoding="utf-8")
        state.save_checkpoint(
            run_id=run_id,
            document_id="doc",
            stage_name="ocr",
            chunk_index=0,
            input_sha256="1" * 64,
            output_sha256="2" * 64,
            artifact_path=artifact,
        )
        state.finish_run(run_id, "failed", error="boom")
        state.assert_resumable(run_id)
        resumed = state.get_stage(run_id, "doc", "ocr")
        assert resumed is not None
        assert (resumed.status, resumed.attempt_count) == ("retry", 0)
        discovery = state.get_stage(run_id, "issuer-kb", "discovery")
        assert discovery is not None
        assert (discovery.status, discovery.attempt_count) == ("retry", 0)
        protected = state.get_stage(run_id, "protected", "download")
        assert protected is not None
        assert (protected.status, protected.attempt_count) == ("retry", 0)
        assert state.checkpoint(run_id, "doc", "ocr", 0) is not None
        assert state.stage_started(run_id, "doc", "ocr") == 1


def test_worker_lock_is_nonblocking_and_recoverable(tmp_path: Path) -> None:
    lock = tmp_path / "worker.lock"
    with worker_lock(lock), pytest.raises(AlreadyRunning), worker_lock(lock):
        pass
    with worker_lock(lock):
        pass


def test_discovery_baseline_ignores_failed_runs(tmp_path: Path) -> None:
    with WorkerState(tmp_path / "state.sqlite3") as state:
        failed = state.start_run(run_id="failed")
        state.record_snapshot(
            run_id=failed,
            snapshot_id="a" * 64,
            issuer="kb",
            source_sha256="a" * 64,
            record_count=100,
            payload={"records": []},
        )
        state.finish_run(failed, "failed")
        assert state.last_successful_snapshot_count("kb") is None

        good = state.start_run(run_id="good")
        state.record_snapshot(
            run_id=good,
            snapshot_id="b" * 64,
            issuer="kb",
            source_sha256="b" * 64,
            record_count=20,
            payload={"records": []},
        )
        state.finish_run(good, "no_change")
        assert state.last_successful_snapshot_count("kb") == 20


def test_incomplete_run_retention_is_bounded(tmp_path: Path) -> None:
    with WorkerState(tmp_path / "state.sqlite3") as state:
        for index in range(12):
            run_id = state.start_run(run_id=f"failed-{index:02d}")
            state.finish_run(run_id, "failed")
        prunable = state.prunable_incomplete_run_ids(keep=10)
        assert set(prunable) == {"failed-00", "failed-01"}


def test_stale_running_runs_are_atomically_interrupted_with_exclusion(tmp_path: Path) -> None:
    with WorkerState(tmp_path / "state.sqlite3") as state:
        stale = state.start_run(run_id="stale")
        current = state.start_run(run_id="current")
        assert state.mark_stale_running_runs_interrupted(exclude_run_id=current) == (stale,)
        assert state.mark_stale_running_runs_interrupted(exclude_run_id=current) == ()

        stale_row = state.connection.execute("SELECT * FROM run WHERE run_id=?", (stale,)).fetchone()
        current_row = state.connection.execute("SELECT * FROM run WHERE run_id=?", (current,)).fetchone()
        assert stale_row["status"] == "interrupted"
        assert stale_row["finished_at"] is not None
        assert 0 < len(stale_row["error"]) <= 4000
        assert current_row["status"] == "running"
        assert current_row["finished_at"] is None
        assert state.prunable_incomplete_run_ids(keep=1) == (stale,)

        state.assert_resumable(stale)
        assert (
            state.connection.execute("SELECT status FROM run WHERE run_id=?", (stale,)).fetchone()[0]
            == "running"
        )


def test_existing_v108_run_status_constraint_is_migrated_without_losing_foreign_keys(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE run (
          run_id TEXT PRIMARY KEY,
          started_at TEXT NOT NULL,
          finished_at TEXT,
          status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed','no_change')),
          corpus_sha256 TEXT,
          contract_sha256 TEXT,
          error TEXT
        ) STRICT;
        CREATE TABLE legacy_child (
          child_id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL REFERENCES run(run_id)
        ) STRICT;
        INSERT INTO run(run_id,started_at,status) VALUES('old-running','2026-01-01T00:00:00+00:00','running');
        INSERT INTO legacy_child(child_id,run_id) VALUES('child','old-running');
        """
    )
    connection.close()

    with WorkerState(database) as state:
        assert state.connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert state.mark_stale_running_runs_interrupted() == ("old-running",)
        assert (
            state.connection.execute("SELECT status FROM run WHERE run_id='old-running'").fetchone()[0]
            == "interrupted"
        )
        assert state.connection.execute("SELECT run_id FROM legacy_child").fetchone()[0] == "old-running"
