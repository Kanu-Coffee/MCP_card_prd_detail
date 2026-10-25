"""Recovery-point verifier rejects incomplete DB sets and unsafe sidecars."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "deploy/worker/verify-recovery-point.py"
SNAPSHOT = "019-fix24-current-20261022"


def _load_verifier() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fix29_recovery_verifier", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


verifier = _load_verifier()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_databases(root: Path) -> Path:
    snapshot = root / "deployment-backups" / SNAPSHOT
    snapshot.mkdir(parents=True)
    worker = snapshot / "worker-state.sqlite3"
    with sqlite3.connect(worker) as connection:
        connection.execute(
            "CREATE TABLE run (run_id TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO run VALUES (?, ?, ?)",
            ("run-1", "succeeded", "2026-10-22T00:00:00+00:00"),
        )
    backup = snapshot / "backup-ledger.sqlite3"
    with sqlite3.connect(backup) as connection:
        connection.execute("CREATE TABLE receipt (value TEXT)")
    evidence: dict[str, Any] = {
        "databases": {
            "worker-state.sqlite3": {"size": worker.stat().st_size, "sha256": _sha(worker)},
            "backup-ledger.sqlite3": {"size": backup.stat().st_size, "sha256": _sha(backup)},
        },
        "latest_run": ["run-1", "succeeded"],
        "stable": {"generation_id": "g-test"},
    }
    (snapshot / "snapshot.json").write_text(json.dumps(evidence))
    return snapshot


def _guidance(snapshot: Path) -> dict[str, Any]:
    evidence = snapshot / "snapshot.json"
    return {
        "schema_version": "cardrag.recovery-point-guidance.v1",
        "protected_snapshot": SNAPSHOT,
        "snapshot_directory": f"/state/deployment-backups/{SNAPSHOT}",
        "snapshot_evidence_sha256": _sha(evidence),
        "historical_release": {
            "snapshot_directory": "/state/deployment-backups/historical-missing",
        },
        "code_rollback": {"available": True},
        "database_restore": {"authorized": False, "performed": False},
    }


def _write_guidance(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload))
    return path


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    state = tmp_path / "state"
    snapshot = _write_databases(state)
    guidance = _write_guidance(tmp_path / "recovery-point.json", _guidance(snapshot))
    return state, guidance, snapshot


def test_verifies_exact_two_database_snapshot(tmp_path: Path) -> None:
    state, guidance, _snapshot = _fixture(tmp_path)
    result = verifier.verify(state, guidance)
    assert result["verified"] is True
    assert set(result["databases"]) == {"worker-state.sqlite3", "backup-ledger.sqlite3"}
    assert result["databases"]["worker-state.sqlite3"]["wal_bytes"] == 0
    assert result["restore_performed"] is False
    assert result["state_written"] is False


@pytest.mark.parametrize("missing", ["worker-state.sqlite3", "backup-ledger.sqlite3"])
def test_rejects_missing_required_database(tmp_path: Path, missing: str) -> None:
    state, guidance, snapshot = _fixture(tmp_path)
    (snapshot / missing).unlink()
    with pytest.raises(ValueError, match="unavailable"):
        verifier.verify(state, guidance)


@pytest.mark.parametrize("key", ["worker-state.sqlite3", "backup-ledger.sqlite3"])
def test_rejects_incomplete_evidence_database_set(tmp_path: Path, key: str) -> None:
    state, guidance, snapshot = _fixture(tmp_path)
    evidence = json.loads((snapshot / "snapshot.json").read_text())
    evidence["databases"].pop(key)
    (snapshot / "snapshot.json").write_text(json.dumps(evidence))
    payload = json.loads(guidance.read_text())
    payload["snapshot_evidence_sha256"] = _sha(snapshot / "snapshot.json")
    _write_guidance(guidance, payload)
    with pytest.raises(ValueError, match="exactly worker-state"):
        verifier.verify(state, guidance)


def test_rejects_extra_or_escaping_database_entry(tmp_path: Path) -> None:
    state, guidance, snapshot = _fixture(tmp_path)
    evidence = json.loads((snapshot / "snapshot.json").read_text())
    evidence["databases"]["../outside.sqlite3"] = {"size": 1, "sha256": "a" * 64}
    (snapshot / "snapshot.json").write_text(json.dumps(evidence))
    payload = json.loads(guidance.read_text())
    payload["snapshot_evidence_sha256"] = _sha(snapshot / "snapshot.json")
    _write_guidance(guidance, payload)
    with pytest.raises(ValueError, match="exactly worker-state"):
        verifier.verify(state, guidance)


@pytest.mark.parametrize("field", ["size", "sha256"])
def test_rejects_invalid_database_metadata(tmp_path: Path, field: str) -> None:
    state, guidance, snapshot = _fixture(tmp_path)
    evidence = json.loads((snapshot / "snapshot.json").read_text())
    evidence["databases"]["worker-state.sqlite3"][field] = -1 if field == "size" else "bad"
    (snapshot / "snapshot.json").write_text(json.dumps(evidence))
    payload = json.loads(guidance.read_text())
    payload["snapshot_evidence_sha256"] = _sha(snapshot / "snapshot.json")
    _write_guidance(guidance, payload)
    with pytest.raises(ValueError, match="size|hash"):
        verifier.verify(state, guidance)


@pytest.mark.parametrize("field", ["size", "sha256"])
def test_rejects_database_identity_mismatch(tmp_path: Path, field: str) -> None:
    state, guidance, snapshot = _fixture(tmp_path)
    evidence = json.loads((snapshot / "snapshot.json").read_text())
    if field == "size":
        evidence["databases"]["backup-ledger.sqlite3"]["size"] += 1
    else:
        evidence["databases"]["backup-ledger.sqlite3"]["sha256"] = "0" * 64
    (snapshot / "snapshot.json").write_text(json.dumps(evidence))
    payload = json.loads(guidance.read_text())
    payload["snapshot_evidence_sha256"] = _sha(snapshot / "snapshot.json")
    _write_guidance(guidance, payload)
    with pytest.raises(ValueError, match="identity mismatch"):
        verifier.verify(state, guidance)


def test_rejects_symlink_database(tmp_path: Path) -> None:
    state, guidance, snapshot = _fixture(tmp_path)
    backup = snapshot / "backup-ledger.sqlite3"
    target = snapshot / "worker-state.sqlite3"
    backup.unlink()
    backup.symlink_to(target)
    with pytest.raises(ValueError, match="not a regular file"):
        verifier.verify(state, guidance)


@pytest.mark.parametrize("database", ["worker-state.sqlite3", "backup-ledger.sqlite3"])
def test_rejects_nonempty_wal_before_immutable_check(tmp_path: Path, database: str) -> None:
    state, guidance, snapshot = _fixture(tmp_path)
    (snapshot / f"{database}-wal").write_bytes(b"x" * 29)
    with pytest.raises(ValueError, match="nonempty WAL"):
        verifier.verify(state, guidance)


def test_accepts_zero_byte_wal_and_preserves_read_only_contract(tmp_path: Path) -> None:
    state, guidance, snapshot = _fixture(tmp_path)
    (snapshot / "worker-state.sqlite3-wal").write_bytes(b"")
    result = verifier.verify(state, guidance)
    assert result["verified"] is True
    assert result["databases"]["worker-state.sqlite3"]["wal_bytes"] == 0


def test_rejects_nonempty_journal_sidecar(tmp_path: Path) -> None:
    state, guidance, snapshot = _fixture(tmp_path)
    (snapshot / "backup-ledger.sqlite3-journal").write_bytes(b"pending")
    with pytest.raises(ValueError, match="nonempty journal"):
        verifier.verify(state, guidance)


def test_rejects_existing_historical_snapshot(tmp_path: Path) -> None:
    state, guidance, _snapshot = _fixture(tmp_path)
    historical = state / "deployment-backups" / "historical-missing"
    historical.mkdir()
    with pytest.raises(ValueError, match="historical snapshot unexpectedly exists"):
        verifier.verify(state, guidance)


def test_cli_reports_safe_failure_without_traceback(tmp_path: Path) -> None:
    state, guidance, snapshot = _fixture(tmp_path)
    (snapshot / "backup-ledger.sqlite3").unlink()
    result = subprocess.run(  # noqa: S603 - fixed interpreter and local fixture paths
        [sys.executable, str(SCRIPT), str(state), str(guidance)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout.startswith('{"verified": false')
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr
