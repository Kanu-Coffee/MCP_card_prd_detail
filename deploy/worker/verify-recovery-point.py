#!/usr/bin/env python3
"""Verify the current protected recovery snapshot without restoring it."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import stat
from pathlib import Path
from typing import Any

REQUIRED_DATABASES = ("worker-state.sqlite3", "backup-ledger.sqlite3")
SIDECARS = ("-wal", "-shm", "-journal")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SNAPSHOT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} is not a JSON object")
    return payload


def file_identity(path: Path, label: str) -> tuple[int, int, int, int]:
    try:
        info = path.lstat()
    except OSError:
        raise ValueError(f"{label} is unavailable") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{label} is not a regular file")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def sidecar_identities(snapshot_dir: Path, database: str) -> dict[str, tuple[int, int, int, int] | None]:
    result: dict[str, tuple[int, int, int, int] | None] = {}
    for suffix in SIDECARS:
        path = snapshot_dir / f"{database}{suffix}"
        if not path.exists() and not path.is_symlink():
            result[suffix] = None
            continue
        identity = file_identity(path, f"{database}{suffix}")
        if suffix == "-wal" and identity[2] != 0:
            raise ValueError(f"{database} has a nonempty WAL sidecar")
        if suffix == "-journal" and identity[2] != 0:
            raise ValueError(f"{database} has a nonempty journal sidecar")
        result[suffix] = identity
    return result


def validate_evidence_databases(evidence: dict[str, Any]) -> dict[str, dict[str, Any]]:
    databases = evidence.get("databases")
    if not isinstance(databases, dict):
        raise ValueError("snapshot evidence databases are missing")
    if set(databases) != set(REQUIRED_DATABASES):
        raise ValueError(
            "snapshot evidence must contain exactly worker-state.sqlite3 and backup-ledger.sqlite3"
        )
    validated: dict[str, dict[str, Any]] = {}
    for name in REQUIRED_DATABASES:
        metadata = databases.get(name)
        if not isinstance(metadata, dict):
            raise ValueError(f"snapshot database metadata is invalid: {name}")
        size = metadata.get("size")
        sha256 = metadata.get("sha256")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError(f"snapshot database size is invalid: {name}")
        if not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None:
            raise ValueError(f"snapshot database hash is invalid: {name}")
        validated[name] = {"size": size, "sha256": sha256}
    return validated


def verify(state: Path, guidance_path: Path) -> dict[str, Any]:
    guidance = load_object(guidance_path)
    if guidance.get("schema_version") != "cardrag.recovery-point-guidance.v1":
        raise ValueError("unsupported recovery guidance schema")
    snapshot_name = guidance.get("protected_snapshot")
    if not isinstance(snapshot_name, str) or _SNAPSHOT_NAME.fullmatch(snapshot_name) is None:
        raise ValueError("protected_snapshot is invalid")
    expected_evidence_sha = guidance.get("snapshot_evidence_sha256")
    if not isinstance(expected_evidence_sha, str) or _SHA256.fullmatch(expected_evidence_sha) is None:
        raise ValueError("snapshot_evidence_sha256 is invalid")

    state = state.resolve()
    backups = (state / "deployment-backups").resolve()
    candidate = backups / snapshot_name
    if candidate.is_symlink():
        raise ValueError("protected snapshot directory is a symlink")
    snapshot_dir = candidate.resolve()
    if not snapshot_dir.is_relative_to(backups) or not snapshot_dir.is_dir():
        raise ValueError("protected snapshot directory is unavailable")
    if guidance.get("snapshot_directory") != f"/state/deployment-backups/{snapshot_name}":
        raise ValueError("guidance snapshot_directory does not match protected_snapshot")

    evidence_path = snapshot_dir / "snapshot.json"
    evidence_identity = file_identity(evidence_path, "snapshot evidence")
    evidence = load_object(evidence_path)
    expected_databases = validate_evidence_databases(evidence)
    if sha256_file(evidence_path) != expected_evidence_sha:
        raise ValueError("snapshot evidence hash does not match guidance")
    latest_evidence = evidence.get("latest_run")
    if (
        not isinstance(latest_evidence, list)
        or len(latest_evidence) != 2
        or not all(isinstance(value, str) and value for value in latest_evidence)
    ):
        raise ValueError("snapshot latest_run evidence is invalid")

    databases: dict[str, Any] = {}
    for name in REQUIRED_DATABASES:
        expected = expected_databases[name]
        path = snapshot_dir / name
        before = file_identity(path, name)
        sidecars_before = sidecar_identities(snapshot_dir, name)
        actual_size = path.stat().st_size
        actual_sha = sha256_file(path)
        if actual_size != expected["size"] or actual_sha != expected["sha256"]:
            raise ValueError(f"snapshot database identity mismatch: {name}")
        with sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True) as connection:
            check = connection.execute("PRAGMA quick_check").fetchone()
        if not check or check[0] != "ok":
            raise ValueError(f"snapshot database quick_check failed: {name}")
        if file_identity(path, name) != before:
            raise ValueError(f"snapshot database changed during verification: {name}")
        sidecars_after = sidecar_identities(snapshot_dir, name)
        if sidecars_after != sidecars_before:
            raise ValueError(f"snapshot sidecar changed during verification: {name}")
        databases[name] = {
            "size": actual_size,
            "sha256": actual_sha,
            "quick_check": "ok",
            "wal_bytes": 0 if sidecars_before["-wal"] is None else sidecars_before["-wal"][2],
        }

    if file_identity(evidence_path, "snapshot evidence") != evidence_identity:
        raise ValueError("snapshot evidence changed during verification")

    with sqlite3.connect(
        f"{(snapshot_dir / 'worker-state.sqlite3').as_uri()}?mode=ro&immutable=1", uri=True
    ) as connection:
        latest = connection.execute(
            "SELECT run_id,status FROM run ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    if latest is None or list(latest) != latest_evidence:
        raise ValueError("snapshot latest run does not match evidence")

    historical = guidance.get("historical_release")
    if not isinstance(historical, dict):
        raise ValueError("historical release guidance is missing")
    historical_snapshot = historical.get("snapshot_directory")
    if not isinstance(historical_snapshot, str) or not historical_snapshot.startswith(
        "/state/deployment-backups/"
    ):
        raise ValueError("historical snapshot path is invalid")
    historical_name = historical_snapshot.removeprefix("/state/deployment-backups/")
    if _SNAPSHOT_NAME.fullmatch(historical_name) is None:
        raise ValueError("historical snapshot name is invalid")
    historical_path = backups / historical_name
    historical_exists = historical_path.exists() or historical_path.is_symlink()
    if historical_exists:
        raise ValueError("historical snapshot unexpectedly exists; guidance needs review")

    return {
        "schema_version": "cardrag.recovery-point-verification.v1",
        "verified": True,
        "protected_snapshot": snapshot_name,
        "snapshot_directory": str(snapshot_dir),
        "snapshot_evidence_sha256": expected_evidence_sha,
        "databases": databases,
        "latest_run": list(latest),
        "stable_generation": evidence.get("stable", {}).get("generation_id"),
        "code_rollback": guidance.get("code_rollback"),
        "database_restore": guidance.get("database_restore"),
        "historical_release": {
            **historical,
            "snapshot_exists": historical_exists,
            "selected_as_current": False,
        },
        "restore_performed": False,
        "state_written": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state", type=Path, help="Worker state mount, usually /state")
    parser.add_argument("guidance", type=Path, help="recovery-point.json path")
    args = parser.parse_args()
    try:
        result = verify(args.state, args.guidance)
    except Exception as exc:
        print(json.dumps({"verified": False, "reason": str(exc)}, ensure_ascii=False))
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
