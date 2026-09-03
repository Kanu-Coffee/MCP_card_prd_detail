from __future__ import annotations

import errno
import hashlib
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import cardrag_worker.state as state_module
from cardrag_worker.capacity_v5 import predict_v5_local_artifacts
from cardrag_worker.state import (
    WORKER_STATE_SQLITE_PAGE_BYTES,
    WORKER_STATE_WAL_AUTOCHECKPOINT_PAGES,
    AlreadyRunning,
    WorkerState,
    WorkerStateWALCapacityError,
    worker_lock,
)


def _state_tree_bytes(path: Path) -> int:
    return sum(
        candidate.stat().st_size
        for candidate in (path, path.with_name(f"{path.name}-wal"), path.with_name(f"{path.name}-shm"))
        if candidate.exists()
    )


def _subprocess_read_only_query(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed interpreter and inline probe
        [
            sys.executable,
            "-c",
            """
import sqlite3
import sys
from pathlib import Path

path = Path(sys.argv[1])
connection = sqlite3.connect(
    f"{path.resolve().as_uri()}?mode=ro",
    uri=True,
    timeout=0,
)
try:
    print(connection.execute("SELECT count(*) FROM run").fetchone()[0])
finally:
    connection.close()
""",
            os.fspath(path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _subprocess_observes_posix_record_lock(path: Path, offset: int) -> bool:
    probe = subprocess.run(  # noqa: S603 - fixed interpreter and inline probe
        [
            sys.executable,
            "-c",
            """
import errno
import fcntl
import os
import sys

descriptor = os.open(sys.argv[1], os.O_RDWR)
offset = int(sys.argv[2])
try:
    try:
        fcntl.lockf(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, offset, os.SEEK_SET)
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            raise SystemExit(0)
        raise
    else:
        fcntl.lockf(descriptor, fcntl.LOCK_UN, 1, offset, os.SEEK_SET)
        raise SystemExit(1)
finally:
    os.close(descriptor)
""",
            os.fspath(path),
            str(offset),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if probe.returncode not in (0, 1):
        raise AssertionError(f"record-lock probe failed: {probe.stderr}")
    return probe.returncode == 0


@pytest.mark.skipif(sys.platform != "linux", reason="regression exercises Linux POSIX locks")
def test_identity_verification_fds_do_not_release_stock_vfs_locks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "state.sqlite3"
    monkeypatch.setattr(state_module, "WORKER_STATE_USE_UNIX_EXCL", False)

    with WorkerState(path):
        # SQLite's shared database byte and WAL-index dead-man-switch byte must
        # remain locked after every post-connect identity check has completed.
        assert _subprocess_observes_posix_record_lock(path, 0x40000002)
        assert _subprocess_observes_posix_record_lock(
            path.with_name(f"{path.name}-shm"),
            128,
        )


def test_same_process_second_state_fails_before_opening_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "state.sqlite3"

    with WorkerState(path) as state:
        attempted_open = False

        def reject_open(*_args: object, **_kwargs: object) -> tuple[int, os.stat_result]:
            nonlocal attempted_open
            attempted_open = True
            raise AssertionError("second WorkerState must fail before opening an active inode")

        with monkeypatch.context() as scoped:
            scoped.setattr(state_module, "_open_nofollow_regular", reject_open)
            with pytest.raises(RuntimeError, match="already open in this process"):
                WorkerState(path)
        assert not attempted_open
        state.connection.execute("CREATE TABLE registry_probe(value INTEGER NOT NULL)")
        state.connection.execute("INSERT INTO registry_probe(value) VALUES(1)")

    with WorkerState(path) as reopened:
        assert reopened.connection.execute("SELECT value FROM registry_probe").fetchone()[0] == 1


@pytest.mark.skipif(sys.platform != "linux", reason="regression exercises the Linux unix-excl VFS")
def test_linux_unix_excl_crosses_wal_index_boundary_without_mapping_shm(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    wal_path = path.with_name(f"{path.name}-wal")
    shm_path = path.with_name(f"{path.name}-shm")
    row_count = 4064

    with WorkerState(path) as state:
        state.connection.execute("PRAGMA wal_autocheckpoint=0")
        state.connection.execute("CREATE TABLE unix_excl_probe(payload BLOB NOT NULL)")
        with state.transaction() as connection:
            connection.executemany(
                "INSERT INTO unix_excl_probe(payload) VALUES(zeroblob(?))",
                ((WORKER_STATE_SQLITE_PAGE_BYTES,) for _ in range(row_count)),
            )

        wal_frame_count = (wal_path.stat().st_size - 32) // (WORKER_STATE_SQLITE_PAGE_BYTES + 24)
        assert wal_frame_count > 4063
        assert not shm_path.exists()
        assert os.fspath(shm_path.resolve()) not in Path("/proc/self/maps").read_text()

        read_only = _subprocess_read_only_query(path)
        assert read_only.returncode != 0
        assert "database is locked" in read_only.stderr

        state.connection.execute("INSERT INTO unix_excl_probe(payload) VALUES(zeroblob(1))")
        assert state.connection.execute("SELECT count(*) FROM unix_excl_probe").fetchone()[0] == (
            row_count + 1
        )


def test_state_rejects_existing_non_4096_page_database(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA page_size=8192")
        connection.execute("VACUUM")
        connection.execute("CREATE TABLE sentinel(value TEXT)")

    with pytest.raises(RuntimeError, match="4096-byte"):
        WorkerState(path)


def test_state_and_lock_reject_symlink_leaves_without_mutating_targets(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.write_bytes(b"preserve")
    state_link = tmp_path / "state.sqlite3"
    state_link.symlink_to(victim)

    with pytest.raises(RuntimeError, match="unavailable or not a regular file"):
        WorkerState(state_link)

    lock_link = tmp_path / "worker.lock"
    lock_link.symlink_to(victim)
    with pytest.raises(RuntimeError, match="unavailable or not a regular file"), worker_lock(lock_link):
        pass

    assert victim.read_bytes() == b"preserve"


@pytest.mark.parametrize("suffix", ("-wal", "-shm"))
def test_state_rejects_existing_sqlite_sidecar_symlink_without_mutating_target(
    tmp_path: Path,
    suffix: str,
) -> None:
    victim = tmp_path / "victim"
    victim.write_bytes(b"preserve-sidecar-victim")
    path = tmp_path / "state.sqlite3"
    path.with_name(f"{path.name}{suffix}").symlink_to(victim)

    with pytest.raises(RuntimeError, match="unavailable or not a regular file"):
        WorkerState(path)

    assert victim.read_bytes() == b"preserve-sidecar-victim"


def test_state_revalidates_sqlite_created_sidecars_as_regular_files(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"

    with WorkerState(path):
        wal_path = path.with_name(f"{path.name}-wal")
        assert stat.S_ISREG(wal_path.stat(follow_symlinks=False).st_mode)
        shm_path = path.with_name(f"{path.name}-shm")
        if sys.platform == "linux":
            assert not shm_path.exists()
        else:
            assert stat.S_ISREG(shm_path.stat(follow_symlinks=False).st_mode)


@pytest.mark.skipif(sys.platform != "linux", reason="regression exercises the Linux unix-excl VFS")
def test_linux_unix_excl_tolerates_but_does_not_map_stale_regular_shm(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    shm_path = path.with_name(f"{path.name}-shm")
    stale_payload = b"stale-stock-unix-vfs-shm" * 2048
    shm_path.write_bytes(stale_payload)

    with WorkerState(path) as state:
        state.start_run(run_id="stale-shm-compatible")
        assert shm_path.read_bytes() == stale_payload
        assert os.fspath(shm_path.resolve()) not in Path("/proc/self/maps").read_text()


@pytest.mark.parametrize("suffix", ("-wal", "-shm"))
def test_state_fails_closed_when_sqlite_sidecar_is_swapped_before_postcheck(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
) -> None:
    path = tmp_path / "state.sqlite3"
    victim = tmp_path / "victim"
    victim.write_bytes(b"preserve-postcheck-victim")
    if suffix == "-shm":
        path.with_name(f"{path.name}-shm").write_bytes(b"stale-stock-unix-vfs-shm")
    original_open = state_module._open_nofollow_regular
    injected = False
    verification_descriptors: list[int] = []

    def swap_then_open(
        candidate: Path,
        *,
        create: bool,
        retain_descriptor_in: list[int] | None = None,
    ) -> tuple[int, os.stat_result]:
        nonlocal injected
        if (
            not create
            and retain_descriptor_in is not None
            and candidate.name.endswith(suffix)
            and candidate.exists()
            and not injected
        ):
            candidate.unlink()
            candidate.symlink_to(victim)
            injected = True
        result = original_open(
            candidate,
            create=create,
            retain_descriptor_in=retain_descriptor_in,
        )
        if retain_descriptor_in is not None:
            verification_descriptors.append(result[0])
        return result

    monkeypatch.setattr(state_module, "_open_nofollow_regular", swap_then_open)

    with pytest.raises(RuntimeError, match="WAL/SHM identity|SQLite files changed"):
        WorkerState(path)

    assert injected
    assert verification_descriptors
    for descriptor in verification_descriptors:
        with pytest.raises(OSError) as closed:
            os.fstat(descriptor)
        assert closed.value.errno == errno.EBADF
    assert victim.read_bytes() == b"preserve-postcheck-victim"


def test_embedding_cache_prediction_and_hard_wal_cap_cover_a_pinned_reader(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    miss_count = WORKER_STATE_WAL_AUTOCHECKPOINT_PAGES + 1
    values = (1.0, *([0.0] * 4095))
    with WorkerState(path) as state, sqlite3.connect(path, isolation_level=None) as reader:
        tree_baseline = _state_tree_bytes(path)
        wal_baseline = state.observe_embedding_cache_v5_wal()
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM embedding_cache_v5").fetchone()

        for index in range(miss_count):
            state.put_embedding_v5(
                cache_key=hashlib.sha256(f"cache-{index}".encode()).hexdigest(),
                profile_id="profile-v5",
                input_sha256=hashlib.sha256(f"input-{index}".encode()).hexdigest(),
                dimension=4096,
                values=values,
            )

        prediction = predict_v5_local_artifacts(
            derived_view_count=miss_count,
            database_payload_bytes=0,
            database_row_count=0,
            embedding_cache_miss_count=miss_count,
            embedding_cache_wal_baseline_bytes=wal_baseline.size_bytes,
        )
        actual_growth = _state_tree_bytes(path) - tree_baseline
        modeled_cache_peak = (
            prediction.embedding_cache_growth_bytes + prediction.embedding_cache_transaction_bytes
        )
        assert actual_growth <= modeled_cache_peak

        wal_capacity = state.check_embedding_cache_v5_wal_capacity(
            baseline=wal_baseline,
            maximum_wal_growth_bytes=(
                prediction.embedding_cache_transaction_bytes - prediction.embedding_cache_wal_baseline_bytes
            ),
        )
        assert wal_capacity.wal_size_bytes <= wal_capacity.maximum_wal_bytes
        too_small_growth = wal_capacity.wal_size_bytes - wal_baseline.size_bytes - 1
        assert too_small_growth > 0
        with pytest.raises(WorkerStateWALCapacityError, match="predicted hard limit"):
            state.check_embedding_cache_v5_wal_capacity(
                baseline=wal_baseline,
                maximum_wal_growth_bytes=too_small_growth,
            )

        reader.execute("ROLLBACK")
        later_baseline = state.observe_embedding_cache_v5_wal()
        one_miss = predict_v5_local_artifacts(
            derived_view_count=1,
            database_payload_bytes=0,
            database_row_count=0,
            embedding_cache_miss_count=1,
            embedding_cache_wal_baseline_bytes=later_baseline.size_bytes,
        )
        later = state.check_embedding_cache_v5_wal_capacity(
            baseline=later_baseline,
            maximum_wal_growth_bytes=(
                one_miss.embedding_cache_transaction_bytes - one_miss.embedding_cache_wal_baseline_bytes
            ),
        )
        assert later.maximum_wal_bytes == one_miss.embedding_cache_transaction_bytes
        assert later.wal_size_bytes <= later.maximum_wal_bytes

        all_hit_baseline = state.observe_embedding_cache_v5_wal()
        all_hit = predict_v5_local_artifacts(
            derived_view_count=1,
            database_payload_bytes=0,
            database_row_count=0,
            embedding_cache_miss_count=0,
            embedding_cache_wal_baseline_bytes=all_hit_baseline.size_bytes,
        )
        before_bookkeeping = _state_tree_bytes(path)
        state.start_run(run_id="all-hit-bookkeeping")
        bookkeeping_growth = _state_tree_bytes(path) - before_bookkeeping
        assert bookkeeping_growth <= all_hit.embedding_cache_transaction_bytes


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
        assert state.connection.execute("PRAGMA page_size").fetchone()[0] == (WORKER_STATE_SQLITE_PAGE_BYTES)
        assert state.connection.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == (
            WORKER_STATE_WAL_AUTOCHECKPOINT_PAGES
        )
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


def test_publication_resume_claim_does_not_reopen_any_pipeline_stage(tmp_path: Path) -> None:
    with WorkerState(tmp_path / "state.sqlite3") as state:
        run_id = state.start_run(run_id="run-publication")
        for document_id, stage_name in (
            ("issuer-kb", "discovery"),
            ("source-1", "download"),
            ("doc-1", "ocr"),
            ("doc-1", "embedding"),
        ):
            state.ensure_stage(run_id, document_id, stage_name, max_attempts=2)
            state.stage_started(run_id, document_id, stage_name)
            state.stage_terminal_failed(run_id, document_id, stage_name, "safe failure")
        before = state.connection.execute(
            """SELECT document_id,stage_name,status,attempt_count,max_attempts,
                      available_at,last_error,updated_at
               FROM stage WHERE run_id=? ORDER BY document_id,stage_name""",
            (run_id,),
        ).fetchall()
        state.finish_run(run_id, "failed", error="publication failed")
        unrelated = state.start_run(run_id="unrelated-running")

        state.assert_publication_resumable(run_id)

        # Simulate SIGKILL immediately after the first claim. Once a new
        # process owns worker.lock, the leftover running row is stale and the
        # same sealed-publication operation must be reclaimable.
        state.assert_publication_resumable(run_id)

        after = state.connection.execute(
            """SELECT document_id,stage_name,status,attempt_count,max_attempts,
                      available_at,last_error,updated_at
               FROM stage WHERE run_id=? ORDER BY document_id,stage_name""",
            (run_id,),
        ).fetchall()
        status = state.connection.execute(
            "SELECT status,error FROM run WHERE run_id=?",
            (run_id,),
        ).fetchone()
        assert [tuple(row) for row in after] == [tuple(row) for row in before]
        assert tuple(status) == ("running", None)
        assert (
            state.connection.execute(
                "SELECT status FROM run WHERE run_id=?",
                (unrelated,),
            ).fetchone()[0]
            == "running"
        )


@pytest.mark.parametrize("status", ["succeeded", "no_change"])
def test_publication_resume_claim_rejects_complete_run(
    tmp_path: Path,
    status: str,
) -> None:
    with WorkerState(tmp_path / "state.sqlite3") as state:
        run_id = state.start_run(run_id="run-publication")
        state.finish_run(run_id, status)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="not a resumable publication candidate"):
            state.assert_publication_resumable(run_id)


def test_worker_lock_is_nonblocking_and_recoverable(tmp_path: Path) -> None:
    lock = tmp_path / "worker.lock"
    with worker_lock(lock), pytest.raises(AlreadyRunning), worker_lock(lock):
        pass
    with worker_lock(lock):
        pass


def test_existing_state_open_never_creates_a_missing_database(tmp_path: Path) -> None:
    database = tmp_path / "missing.sqlite3"
    with pytest.raises(FileNotFoundError), WorkerState(database, create=False):
        pass
    assert not database.exists()


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
