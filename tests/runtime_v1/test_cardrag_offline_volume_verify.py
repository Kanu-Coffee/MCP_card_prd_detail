from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools/cardrag_offline_volume_verify.py"


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - exact interpreter and repository tool
        [sys.executable, str(TOOL), *arguments],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _state_pair(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    source.mkdir(mode=0o700)
    (source / "objects").mkdir(mode=0o700)
    (source / "objects" / "payload.bin").write_bytes(b"sealed-payload")
    database = sqlite3.connect(source / "worker-state.sqlite3")
    database.execute("CREATE TABLE run (id TEXT PRIMARY KEY, status TEXT NOT NULL)")
    database.execute("INSERT INTO run VALUES ('a', 'succeeded')")
    database.commit()
    database.close()
    destination = tmp_path / "destination"
    shutil.copytree(source, destination, copy_function=shutil.copy2)
    return source, destination


def test_state_verification_seals_equal_independent_trees(tmp_path: Path) -> None:
    source, destination = _state_pair(tmp_path)

    result = _run("state", "--source", str(source), "--destination", str(destination))

    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["schema_version"] == "cardrag.offline-volume-verification.v1"
    assert receipt["status"] == "passed"
    assert receipt["sqlite_database_count"] == 1
    assert len(receipt["content_tree_sha256"]) == 64


def test_state_verification_rejects_tree_drift(tmp_path: Path) -> None:
    source, destination = _state_pair(tmp_path)
    (destination / "objects" / "payload.bin").write_bytes(b"different")

    result = _run("state", "--source", str(source), "--destination", str(destination))

    assert result.returncode == 2
    assert "state_tree_mismatch" in result.stderr


def test_state_verification_rejects_hardlinks(tmp_path: Path) -> None:
    source, destination = _state_pair(tmp_path)
    destination_payload = destination / "objects" / "payload.bin"
    destination_payload.unlink()
    os.link(source / "objects" / "payload.bin", destination_payload)

    result = _run("state", "--source", str(source), "--destination", str(destination))

    assert result.returncode == 2
    assert "hardlink_forbidden" in result.stderr


def test_state_verification_rejects_sqlite_transient_files(tmp_path: Path) -> None:
    source, destination = _state_pair(tmp_path)
    (source / "worker-state.sqlite3-wal").write_bytes(b"wal")
    (destination / "worker-state.sqlite3-wal").write_bytes(b"wal")

    result = _run("state", "--source", str(source), "--destination", str(destination))

    assert result.returncode == 2
    assert "sqlite_transient_file_present" in result.stderr


def test_state_verification_rejects_sqlite_foreign_key_violations(tmp_path: Path) -> None:
    source, _ = _state_pair(tmp_path)
    database = sqlite3.connect(source / "worker-state.sqlite3")
    database.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
    database.execute("CREATE TABLE child (parent_id INTEGER REFERENCES parent(id))")
    database.execute("INSERT INTO child VALUES (999)")
    database.commit()
    database.close()
    destination = tmp_path / "destination-with-foreign-key-violation"
    shutil.copytree(source, destination, copy_function=shutil.copy2)

    result = _run("state", "--source", str(source), "--destination", str(destination))

    assert result.returncode == 2
    assert "sqlite_integrity_check_failed" in result.stderr


def _codex_pair(tmp_path: Path, credential: bytes) -> tuple[Path, Path, list[str]]:
    source = tmp_path / "source-codex"
    destination = tmp_path / "destination-codex"
    source.mkdir(mode=0o700)
    destination.mkdir(mode=0o700)
    source_auth = source / "auth.json"
    source_auth.write_bytes(credential)
    source_auth.chmod(0o600)
    (source / "runtime-cache").mkdir()
    destination_auth = destination / "auth.json"
    destination_auth.write_bytes(credential)
    destination_auth.chmod(0o600)
    (destination / "home").mkdir(mode=0o700)
    identity_arguments = [
        "--expected-uid",
        str(os.getuid()),
        "--expected-gid",
        str(os.getgid()),
    ]
    return source, destination, identity_arguments


def test_codex_home_verification_allows_source_runtime_state_but_minimal_destination(
    tmp_path: Path,
) -> None:
    source, destination, identity_arguments = _codex_pair(tmp_path, b'{"token":"redacted"}')

    result = _run(
        "codex-home",
        "--source",
        str(source),
        "--destination",
        str(destination),
        *identity_arguments,
    )

    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["auth_content_equal"] is True
    assert receipt["destination_entry_count"] == 2
    assert "sha256" not in result.stdout


def test_codex_home_failure_does_not_disclose_credentials(tmp_path: Path) -> None:
    credential = b'{"token":"sk-secret-must-never-appear"}'
    source, destination, identity_arguments = _codex_pair(tmp_path, credential)
    (destination / "logs").mkdir()

    result = _run(
        "codex-home",
        "--source",
        str(source),
        "--destination",
        str(destination),
        *identity_arguments,
    )

    assert result.returncode == 2
    assert "destination_inventory_invalid" in result.stderr
    assert credential.decode() not in result.stdout
    assert credential.decode() not in result.stderr
