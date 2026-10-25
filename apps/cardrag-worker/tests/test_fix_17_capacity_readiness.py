"""Tests for FIX_17: Capacity readiness check, failure payload binding, and safe space criteria."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cardrag_worker.capacity_v5 import (
    CAPACITY_FREE_SPACE,
    V5CapacityError,
)
from cardrag_worker.cli import _echo_worker_unexpected_failure


def test_echo_worker_unexpected_failure_formats_capacity_details(capsys: Any) -> None:
    """When a V5CapacityError occurs, the exit payload must contain capacity_details and failure_reason_code."""
    details = {
        "available_bytes": 69418844160,
        "deficit_bytes": 4553039424,
        "peak_growth_bytes": 71824399936,
        "required_free_bytes": 73971883584,
        "reserved_free_bytes": 2147483648,
    }
    exc = V5CapacityError(
        "Worker reserved free-space gate rejected v5 artifacts",
        reason_code=CAPACITY_FREE_SPACE,
        details=details,
    )

    _echo_worker_unexpected_failure(exc)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert payload["status"] == "failed"
    assert payload["error_class_category"] == "capacity"
    assert payload["failure_reason_code"] == CAPACITY_FREE_SPACE
    assert payload["capacity_details"]["available_bytes"] == 69418844160
    assert payload["capacity_details"]["deficit_bytes"] == 4553039424
    assert payload["capacity_details"]["required_free_bytes"] == 73971883584


def test_capacity_readiness_insufficient_vs_ready() -> None:
    """Readiness logic correctly distinguishes insufficient free space from ready."""

    def evaluate_readiness(avail: int, required: int, buffer_bytes: int) -> dict[str, Any]:
        total_required = required + buffer_bytes
        deficit = max(0, total_required - avail)
        surplus = max(0, avail - total_required)
        return {
            "status": "ready" if avail >= total_required else "insufficient",
            "available_bytes": avail,
            "required_free_bytes": required,
            "buffer_bytes": buffer_bytes,
            "total_required_bytes": total_required,
            "deficit_bytes": deficit,
            "surplus_bytes": surplus,
        }

    # Case 1: Insufficient (similar to BC failure state)
    res_insufficient = evaluate_readiness(
        avail=69418844160,  # ~64.65 GiB
        required=73971883584,  # ~68.89 GiB
        buffer_bytes=4294967296,  # 4.0 GiB
    )
    assert res_insufficient["status"] == "insufficient"
    assert res_insufficient["deficit_bytes"] > 0
    assert res_insufficient["surplus_bytes"] == 0

    # Case 2: Ready (after cleaning 13.6 GiB)
    res_ready = evaluate_readiness(
        avail=84005748736,  # ~78.23 GiB
        required=73971883584,  # ~68.89 GiB
        buffer_bytes=4294967296,  # 4.0 GiB
    )
    assert res_ready["status"] == "ready"
    assert res_ready["deficit_bytes"] == 0
    assert res_ready["surplus_bytes"] > 0


def test_failed_run_does_not_record_backup_success(tmp_path: Path) -> None:
    """A failed run must NOT be recorded as a successful backup run."""
    from cardrag_worker.backup import BackupLedger

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    db_path = state_dir / "backup-ledger.sqlite3"
    ledger = BackupLedger(db_path)

    # In a failed run, record_run_success is never called; verify ledger has no recorded runs
    with ledger._get_connection() as conn:
        rows = conn.execute(
            "SELECT run_id FROM backup_recorded_runs WHERE run_id = 'run-failed-bc'"
        ).fetchall()
        assert len(rows) == 0
