from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
TOOL = ROOT / "tools/cardrag_v113_recovery_copy.py"

import tools.cardrag_v113_recovery_copy as recovery_copy  # noqa: E402
from tools.cardrag_v113_recovery_copy import RecoveryCopyError  # noqa: E402


def _owner() -> tuple[int, int]:
    return os.getuid(), os.getgid()


def _open_descriptor_count() -> int:
    with os.scandir("/proc/self/fd") as entries:
        return sum(1 for _entry in entries)


def test_recovery_tool_is_readable_by_the_unprivileged_production_container_user() -> None:
    assert stat.S_IMODE(TOOL.stat().st_mode) == 0o644


def _state_source(tmp_path: Path) -> tuple[Path, Path, bytes, bytes]:
    source = tmp_path / "source-state"
    source.mkdir(mode=0o700)
    objects = source / "objects"
    objects.mkdir(mode=0o755)
    objects.chmod(0o755)
    payload = objects / "payload.bin"
    payload.write_bytes(b"sealed-payload\n")
    payload.chmod(0o600)

    database = source / "worker-state.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE run (run_id TEXT PRIMARY KEY, status TEXT NOT NULL)")
    connection.execute("INSERT INTO run VALUES ('preserved', 'running')")
    connection.commit()
    connection.close()
    database.chmod(0o644)

    wal_bytes = b"incident-wal-frame-data"
    (source / "worker-state.sqlite3-wal").write_bytes(wal_bytes)
    (source / "worker-state.sqlite3-shm").write_bytes(b"incident-shm-wal-index")
    return source, payload, database.read_bytes(), wal_bytes


def _empty_state_destination(tmp_path: Path, name: str = "destination-state") -> Path:
    destination = tmp_path / name
    destination.mkdir(mode=0o755)
    return destination


def _copy_state(source: Path, destination: Path) -> dict[str, object]:
    uid, gid = _owner()
    return recovery_copy.copy_state(
        source,
        destination,
        expected_uid=uid,
        expected_gid=gid,
        enforce_exact_incident=False,
    )


def test_state_copy_excludes_only_shm_and_seals_stream_hashes(tmp_path: Path) -> None:
    source, _payload, database_bytes, wal_bytes = _state_source(tmp_path)
    destination = _empty_state_destination(tmp_path)

    receipt = _copy_state(source, destination)

    assert receipt["schema_version"] == recovery_copy.SCHEMA_VERSION
    assert receipt["status"] == "passed"
    assert receipt["mode"] == "state"
    assert receipt["excluded_entries"] == ["worker-state.sqlite3-shm"]
    assert receipt["main_database_sha256"] == hashlib.sha256(database_bytes).hexdigest()
    assert receipt["wal_sha256"] == hashlib.sha256(wal_bytes).hexdigest()
    assert receipt["shm_excluded_sha256"] == hashlib.sha256(b"incident-shm-wal-index").hexdigest()
    assert len(str(receipt["content_tree_sha256"])) == 64
    assert (destination / "worker-state.sqlite3").read_bytes() == database_bytes
    assert (destination / "worker-state.sqlite3-wal").read_bytes() == wal_bytes
    assert not (destination / "worker-state.sqlite3-shm").exists()
    assert (destination / "objects" / "payload.bin").read_bytes() == b"sealed-payload\n"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    assert stat.S_IMODE((destination / "objects").stat().st_mode) == 0o755
    assert stat.S_IMODE((destination / "objects" / "payload.bin").stat().st_mode) == 0o600


def test_state_copy_detects_source_mutation_and_leaves_destination_unusable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, payload, _database_bytes, _wal_bytes = _state_source(tmp_path)
    destination = _empty_state_destination(tmp_path)
    original_copy_stream = recovery_copy._copy_stream
    mutated = False

    def mutate_after_stream(source_descriptor: int, destination_descriptor: int, size: int) -> str:
        nonlocal mutated
        digest = original_copy_stream(source_descriptor, destination_descriptor, size)
        if not mutated:
            payload.write_bytes(b"source-mutated-after-stream")
            mutated = True
        return digest

    monkeypatch.setattr(recovery_copy, "_copy_stream", mutate_after_stream)

    with pytest.raises(RecoveryCopyError, match="source_file_changed_during_copy"):
        _copy_state(source, destination)

    assert mutated is True
    assert any(destination.iterdir())
    with pytest.raises(RecoveryCopyError, match="destination_not_empty"):
        _copy_state(source, destination)


@pytest.mark.parametrize(
    ("unsafe_entry", "expected_error"),
    [
        (lambda source, payload: os.symlink(payload, source / "unsafe-link"), "state_symlink_forbidden"),
        (lambda source, payload: os.link(payload, source / "unsafe-hardlink"), "state_hardlink_forbidden"),
        (lambda source, _payload: os.mkfifo(source / "unsafe-fifo"), "state_special_file_forbidden"),
    ],
)
def test_state_copy_rejects_symlink_hardlink_and_special_entries(
    tmp_path: Path,
    unsafe_entry: Callable[[Path, Path], object],
    expected_error: str,
) -> None:
    source, payload, _database_bytes, _wal_bytes = _state_source(tmp_path)
    destination = _empty_state_destination(tmp_path)
    unsafe_entry(source, payload)

    with pytest.raises(RecoveryCopyError, match=expected_error):
        _copy_state(source, destination)

    assert list(destination.iterdir()) == []


def test_state_copy_rejects_nonempty_destination(tmp_path: Path) -> None:
    source, _payload, _database_bytes, _wal_bytes = _state_source(tmp_path)
    destination = _empty_state_destination(tmp_path)
    (destination / "previous-attempt").write_bytes(b"partial")

    with pytest.raises(RecoveryCopyError, match="destination_not_empty"):
        _copy_state(source, destination)


@pytest.mark.parametrize("wal_state", ["missing", "empty"])
def test_state_copy_requires_nonempty_incident_wal(tmp_path: Path, wal_state: str) -> None:
    source, _payload, _database_bytes, _wal_bytes = _state_source(tmp_path)
    destination = _empty_state_destination(tmp_path)
    wal = source / "worker-state.sqlite3-wal"
    if wal_state == "missing":
        wal.unlink()
    else:
        wal.write_bytes(b"")

    expected = "missing" if wal_state == "missing" else "incident_wal_empty"
    with pytest.raises(RecoveryCopyError, match=expected):
        _copy_state(source, destination)


def test_state_copy_requires_the_exact_root_incident_shm(tmp_path: Path) -> None:
    source, _payload, _database_bytes, _wal_bytes = _state_source(tmp_path)
    destination = _empty_state_destination(tmp_path)
    (source / "worker-state.sqlite3-shm").unlink()

    with pytest.raises(RecoveryCopyError, match="worker_state_sqlite3_shm_missing"):
        _copy_state(source, destination)


def test_state_copy_rejects_every_other_sqlite_transient(tmp_path: Path) -> None:
    source, _payload, _database_bytes, _wal_bytes = _state_source(tmp_path)
    destination = _empty_state_destination(tmp_path)
    (source / "objects" / "observer.sqlite3-shm").write_bytes(b"unexpected")

    with pytest.raises(RecoveryCopyError, match="unexpected_sqlite_transient"):
        _copy_state(source, destination)


def test_state_production_contract_rejects_nonincident_inventory(tmp_path: Path) -> None:
    source, _payload, _database_bytes, _wal_bytes = _state_source(tmp_path)
    destination = _empty_state_destination(tmp_path)
    uid, gid = _owner()

    with pytest.raises(RecoveryCopyError, match="incident_main_database_size_mismatch"):
        recovery_copy.copy_state(
            source,
            destination,
            expected_uid=uid,
            expected_gid=gid,
        )


def test_state_exact_incident_path_seals_inventory_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _payload, database_bytes, wal_bytes = _state_source(tmp_path)
    destination = _empty_state_destination(tmp_path)
    shm_bytes = (source / "worker-state.sqlite3-shm").read_bytes()
    total_bytes = sum(path.stat().st_size for path in source.rglob("*") if path.is_file())
    monkeypatch.setattr(recovery_copy, "INCIDENT_MAIN_DATABASE_BYTES", len(database_bytes))
    monkeypatch.setattr(recovery_copy, "INCIDENT_WAL_BYTES", len(wal_bytes))
    monkeypatch.setattr(recovery_copy, "INCIDENT_SHM_BYTES", len(shm_bytes))
    monkeypatch.setattr(
        recovery_copy,
        "INCIDENT_MAIN_DATABASE_SHA256",
        hashlib.sha256(database_bytes).hexdigest(),
    )
    monkeypatch.setattr(recovery_copy, "INCIDENT_WAL_SHA256", hashlib.sha256(wal_bytes).hexdigest())
    monkeypatch.setattr(recovery_copy, "INCIDENT_SHM_SHA256", hashlib.sha256(shm_bytes).hexdigest())
    monkeypatch.setattr(recovery_copy, "INCIDENT_SOURCE_FILE_COUNT", 4)
    monkeypatch.setattr(recovery_copy, "INCIDENT_SOURCE_DIRECTORY_ENTRY_COUNT", 1)
    monkeypatch.setattr(recovery_copy, "INCIDENT_SOURCE_TOTAL_FILE_BYTES", total_bytes)
    uid, gid = _owner()

    receipt = recovery_copy.copy_state(
        source,
        destination,
        expected_uid=uid,
        expected_gid=gid,
    )

    assert receipt["incident_source_file_count"] == 4
    assert receipt["incident_source_directory_entry_count"] == 1
    assert receipt["incident_source_total_file_bytes"] == total_bytes
    assert receipt["main_database_size_bytes"] == len(database_bytes)
    assert receipt["wal_size_bytes"] == len(wal_bytes)
    assert receipt["shm_excluded_size_bytes"] == len(shm_bytes)
    assert receipt["shm_excluded_sha256"] == hashlib.sha256(shm_bytes).hexdigest()


def test_state_destination_open_failure_does_not_leak_source_root_descriptor(tmp_path: Path) -> None:
    source, _payload, _database_bytes, _wal_bytes = _state_source(tmp_path)
    missing_destination = tmp_path / "missing-state-destination"
    uid, gid = _owner()
    descriptor_count = _open_descriptor_count()

    for _attempt in range(20):
        with pytest.raises(RecoveryCopyError, match="destination_directory_open_failed"):
            recovery_copy.copy_state(
                source,
                missing_destination,
                expected_uid=uid,
                expected_gid=gid,
                enforce_exact_incident=False,
            )

    assert _open_descriptor_count() == descriptor_count


def _codex_pair(tmp_path: Path, credential: bytes) -> tuple[Path, Path]:
    source = tmp_path / "source-codex"
    destination = tmp_path / "destination-codex"
    source.mkdir(mode=0o700)
    destination.mkdir(mode=0o755)
    (destination / "home").mkdir(mode=0o700)
    auth = source / "auth.json"
    auth.write_bytes(credential)
    auth.chmod(0o600)
    (source / "runtime-cache").mkdir(mode=0o700)
    return source, destination


def _copy_codex(
    source: Path, destination: Path, *, maximum_auth_bytes: int = 2 * 1024 * 1024
) -> dict[str, object]:
    uid, gid = _owner()
    return recovery_copy.copy_codex_auth(
        source,
        destination,
        expected_uid=uid,
        expected_gid=gid,
        maximum_auth_bytes=maximum_auth_bytes,
    )


def test_codex_copy_is_bounded_atomic_and_receipt_discloses_no_token_or_hash(tmp_path: Path) -> None:
    credential = b'{"token":"sk-never-log-this"}'
    source, destination = _codex_pair(tmp_path, credential)

    receipt = _copy_codex(source, destination)

    rendered = json.dumps(receipt, sort_keys=True)
    assert receipt == {
        "auth_bytes_copied": len(credential),
        "destination_entry_count": 2,
        "destination_home_empty": True,
        "mode": "codex",
        "schema_version": recovery_copy.SCHEMA_VERSION,
        "status": "passed",
    }
    assert credential.decode() not in rendered
    assert "sha" not in rendered.lower()
    assert {entry.name for entry in destination.iterdir()} == {"auth.json", "home"}
    assert (destination / "auth.json").read_bytes() == credential
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    assert stat.S_IMODE((destination / "auth.json").stat().st_mode) == 0o600
    assert list((destination / "home").iterdir()) == []


@pytest.mark.parametrize("violation", ["oversized", "wrong-mode", "nonempty-home"])
def test_codex_copy_rejects_source_and_destination_contract_violations(
    tmp_path: Path,
    violation: str,
) -> None:
    source, destination = _codex_pair(tmp_path, b"bounded-auth")
    if violation == "oversized":
        (source / "auth.json").write_bytes(b"x" * 17)
        (source / "auth.json").chmod(0o600)
    elif violation == "wrong-mode":
        (source / "auth.json").chmod(0o644)
    else:
        (destination / "home" / "stale").write_bytes(b"runtime state")

    expected = "source_auth_invalid" if violation != "nonempty-home" else "destination_home_invalid"
    with pytest.raises(RecoveryCopyError, match=expected):
        _copy_codex(source, destination, maximum_auth_bytes=16)


def test_codex_copy_rejects_auth_hardlink_without_disclosing_content(tmp_path: Path) -> None:
    credential = b"top-secret-auth-content"
    source, destination = _codex_pair(tmp_path, credential)
    os.link(source / "auth.json", source / "auth-copy.json")

    with pytest.raises(RecoveryCopyError) as captured:
        _copy_codex(source, destination)

    assert "codex_source_auth_invalid" in str(captured.value)
    assert credential.decode() not in str(captured.value)


def test_codex_destination_open_failure_does_not_leak_source_root_descriptor(tmp_path: Path) -> None:
    source, _unused_destination = _codex_pair(tmp_path, b"bounded-auth")
    missing_destination = tmp_path / "missing-codex-destination"
    uid, gid = _owner()
    descriptor_count = _open_descriptor_count()

    for _attempt in range(20):
        with pytest.raises(RecoveryCopyError, match="destination_directory_open_failed"):
            recovery_copy.copy_codex_auth(
                source,
                missing_destination,
                expected_uid=uid,
                expected_gid=gid,
            )

    assert _open_descriptor_count() == descriptor_count
