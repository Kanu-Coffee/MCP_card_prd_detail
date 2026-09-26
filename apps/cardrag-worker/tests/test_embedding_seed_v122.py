"""Tests for the fail-closed v1.0.28 embedding-cache seed path (FIX_02 blocking item 1)."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import struct
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cardrag_worker.cli import app
from cardrag_worker.embedding_seed_v122 import (
    EmbeddingSeedPlan,
    StateSeedError,
    apply_embedding_cache_seed_v122,
    build_embedding_cache_seed_v122_plan,
    load_embedding_seed_ledger_v122,
)
from cardrag_worker.state import WorkerState

runner = CliRunner()

_DIMENSION = 8


def _unit_values(label: str) -> list[float]:
    digest = hashlib.sha256(label.encode()).hexdigest()
    raw = [int(digest[i : i + 2], 16) / 255.0 + 0.01 for i in range(0, 32, 4)][: _DIMENSION]
    norm = math.sqrt(sum(value * value for value in raw))
    return [value / norm for value in raw]


def _embedding_row(label: str) -> tuple[str, str, str]:
    formatted = f"instruction for {label}"
    input_sha256 = hashlib.sha256(formatted.encode()).hexdigest()
    cache_key = hashlib.sha256(f"cache::{label}".encode()).hexdigest()
    profile_id = f"cardrag.qwen3-embedding-8b.deepinfra.{label[0]}{'c' * 55}"
    return cache_key, profile_id, input_sha256


def _checkpoint_and_close(state: WorkerState) -> None:
    state.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    state.close()


def _seed_source(state: WorkerState, *, labels: tuple[str, ...]) -> dict[str, list[float]]:
    vectors: dict[str, list[float]] = {}
    for label in labels:
        cache_key, profile_id, input_sha256 = _embedding_row(label)
        values = _unit_values(label)
        vectors[cache_key] = values
        state.put_embedding_v5(
            cache_key=cache_key,
            profile_id=profile_id,
            input_sha256=input_sha256,
            dimension=_DIMENSION,
            values=values,
        )
    return vectors


def _create_source(tmp_path: Path, *, labels: tuple[str, ...] = ("a1", "a2", "a3", "b1")) -> Path:
    root = tmp_path / "v1028-embedding-source"
    root.mkdir(parents=True, exist_ok=True)
    state = WorkerState(root / "worker-state.sqlite3")
    _seed_source(state, labels=labels)
    _checkpoint_and_close(state)
    return root


def _open_state(root: Path) -> WorkerState:
    return WorkerState(root / "worker-state.sqlite3")


def _rows(state: WorkerState) -> dict[str, tuple[str, str, int, str, str, bytes, str]]:
    result: dict[str, tuple[str, str, int, str, str, bytes, str]] = {}
    for raw in state.connection.execute(
        """SELECT cache_key,profile_id,input_sha256,dimension,dtype,normalization,embedding,created_at
           FROM embedding_cache_v5"""
    ):
        result[str(raw[0])] = (str(raw[1]), str(raw[2]), int(raw[3]), str(raw[4]), str(raw[5]), bytes(raw[6]), str(raw[7]))
    return result


def test_plan_validates_and_seals_rows(tmp_path: Path) -> None:
    root = _create_source(tmp_path)
    plan = build_embedding_cache_seed_v122_plan(root)
    assert all(row.dtype == "float32" and row.normalization == "l2" for row in plan.rows)
    assert len(plan.rows) == 4
    assert [row.cache_key for row in plan.rows] == sorted(row.cache_key for row in plan.rows)
    report = plan.report(applied=False)
    assert report["dry_run"] is True
    assert report["applied"] is False
    assert report["schema_version"] == "cardrag.embedding-seed-report.v1"
    rollup = report["profile_rollup"]
    assert rollup[f"cardrag.qwen3-embedding-8b.deepinfra.a{'c' * 55}"]["row_count"] == 3
    assert rollup[f"cardrag.qwen3-embedding-8b.deepinfra.b{'c' * 55}"]["row_count"] == 1
    assert plan.ledger_sha256 == hashlib.sha256(plan.ledger_bytes).hexdigest()
    reloaded = build_embedding_cache_seed_v122_plan(root)
    assert reloaded.ledger_sha256 == plan.ledger_sha256
    assert reloaded.row_root_sha256 == plan.row_root_sha256


def test_apply_imports_verbatim_then_is_idempotent(tmp_path: Path) -> None:
    root = _create_source(tmp_path)
    plan = build_embedding_cache_seed_v122_plan(root)
    destination = tmp_path / "candidate-destination"
    destination.mkdir()
    state = _open_state(destination)
    try:
        first = apply_embedding_cache_seed_v122(plan, state, destination)
        assert first["applied"] is True
        assert first["imported_rows"] == 4
        assert first["reused_rows"] == 0
        ledger_path = destination / first["ledger_path"]
        assert ledger_path.name == f"{plan.ledger_sha256}.json"
        sealed = load_embedding_seed_ledger_v122(destination)
        assert sealed is not None
        assert sealed["row_root_sha256"] == plan.row_root_sha256
        assert sealed["source_database_sha256"] == plan.source_database_sha256

        destination_rows = _rows(state)
        for row in plan.rows:
            stored = destination_rows[row.cache_key]
            assert stored[0] == row.profile_id
            assert stored[1] == row.input_sha256
            assert stored[2] == row.dimension
            assert stored[3] == "float32"
            assert stored[4] == "l2"
            assert hashlib.sha256(stored[5]).hexdigest() == row.embedding_sha256
            assert stored[6] == row.created_at

        second = apply_embedding_cache_seed_v122(plan, state, destination)
        assert second["imported_rows"] == 0
        assert second["reused_rows"] == 4
    finally:
        state.close()


def test_apply_rejects_conflicting_destination_rows(tmp_path: Path) -> None:
    root = _create_source(tmp_path)
    plan = build_embedding_cache_seed_v122_plan(root)
    destination = tmp_path / "candidate-conflict"
    destination.mkdir()
    state = _open_state(destination)
    try:
        victim = plan.rows[0]
        tampered = [value * 0.5 for value in _unit_values("a1")]
        state.put_embedding_v5(
            cache_key=victim.cache_key,
            profile_id=victim.profile_id,
            input_sha256=victim.input_sha256,
            dimension=_DIMENSION,
            values=tampered,
        )
        with pytest.raises(StateSeedError) as raised:
            apply_embedding_cache_seed_v122(plan, state, destination)
        assert raised.value.code == "destination_row_conflict"
        ledger_dir = destination / "audit-reports" / "embedding-seed"
        assert not ledger_dir.exists() or not list(ledger_dir.glob("*.json"))
    finally:
        state.close()


def test_plan_rejects_denormalized_source_row(tmp_path: Path) -> None:
    root = _create_source(tmp_path)
    connection = sqlite3.connect(root / "worker-state.sqlite3")
    victim = hashlib.sha256(b"cache::a2").hexdigest()
    junk = struct.pack(f"<{_DIMENSION}f", *(0.5 for _ in range(_DIMENSION)))
    connection.execute("UPDATE embedding_cache_v5 SET embedding=? WHERE cache_key=?", (junk, victim))
    connection.commit()
    connection.close()
    with pytest.raises(StateSeedError) as raised:
        build_embedding_cache_seed_v122_plan(root)
    assert raised.value.code == "source_embedding_row_norm_invalid"


def test_plan_enforces_expected_rows(tmp_path: Path) -> None:
    root = _create_source(tmp_path)
    plan = build_embedding_cache_seed_v122_plan(root, expected_rows=4)
    assert isinstance(plan, EmbeddingSeedPlan)
    with pytest.raises(StateSeedError) as raised:
        build_embedding_cache_seed_v122_plan(root, expected_rows=5)
    assert raised.value.code == "expected_rows_mismatch"


def test_plan_rejects_sidecars_and_active_writers(tmp_path: Path) -> None:
    root = _create_source(tmp_path)
    (root / "worker-state.sqlite3-wal").write_bytes(b"")
    with pytest.raises(StateSeedError) as raised:
        build_embedding_cache_seed_v122_plan(root)
    assert raised.value.code == "source_has_sidecars"
    (root / "worker-state.sqlite3-wal").unlink()

    state = _open_state(root)
    state.connection.execute(
        "INSERT INTO run(run_id, started_at, status) VALUES('run-live', '2026-09-25T00:00:00+00:00', 'running')"
    )
    _checkpoint_and_close(state)
    with pytest.raises(StateSeedError) as raised:
        build_embedding_cache_seed_v122_plan(root)
    assert raised.value.code == "source_run_active"


def test_apply_rejects_same_root_destination(tmp_path: Path) -> None:
    root = _create_source(tmp_path)
    plan = build_embedding_cache_seed_v122_plan(root)
    state = _open_state(root)
    try:
        with pytest.raises(StateSeedError) as raised:
            apply_embedding_cache_seed_v122(plan, state, root)
        assert raised.value.code == "source_destination_overlap"
    finally:
        state.close()


def test_cli_seed_embedding_cache_v122(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _create_source(tmp_path, labels=("a1", "a2"))
    destination = tmp_path / "candidate-state"
    monkeypatch.setenv("CARDRAG_WORKER_STATE_DIR", str(destination))
    monkeypatch.setenv("CARDRAG_CHANNEL", "candidate-v1.0.11")

    dry = runner.invoke(app, ["seed-embedding-cache-v122", str(root)])
    assert dry.exit_code == 0, dry.output
    dry_json = json.loads(dry.output)
    assert dry_json["dry_run"] is True
    assert dry_json["row_count"] == 2

    applied = runner.invoke(app, ["seed-embedding-cache-v122", str(root), "--apply", "--expected-rows", "2"])
    assert applied.exit_code == 0, applied.output
    applied_json = json.loads(applied.output)
    assert applied_json["applied"] is True
    assert applied_json["imported_rows"] == 2
    assert applied_json["idempotence_imported_rows"] == 0
    assert applied_json["idempotence_verified"] is True

    blocked = runner.invoke(app, ["seed-embedding-cache-v122", str(root), "--expected-rows", "9"])
    assert blocked.exit_code == 1
    blocked_json = json.loads(blocked.output)
    assert blocked_json["status"] == "blocked"
    assert blocked_json["reason_code"] == "expected_rows_mismatch"
