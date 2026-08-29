from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import cardrag_mcp.quota as quota_module
from cardrag_mcp.audit import ExhaustiveAuditError, ExhaustiveAuditStore, ExpectedContract
from cardrag_mcp.quota import (
    StateQuotaPolicy,
    StorageQuotaError,
    configure_state_quota,
    ensure_global_state_growth,
    reserve_global_state_growth,
)


def _query_vector() -> np.ndarray:
    vector = np.zeros((4096,), dtype=np.float32)
    vector[0] = 1.0
    return vector


def _identity(store: ExhaustiveAuditStore, query: str):
    return store.identity("generation-v5", hashlib.sha256(query.encode()).hexdigest())


def _expectations() -> tuple[ExpectedContract, ...]:
    return (ExpectedContract(contract_revision_id="revision-1", embedding_rows=1),)


def test_exhaustive_job_quota_rejects_new_query_and_preserves_existing(tmp_path: Path) -> None:
    store = ExhaustiveAuditStore(
        tmp_path,
        maximum_jobs=1,
        maximum_total_bytes=1024 * 1024,
        maximum_artifact_bytes=1024 * 1024,
    )
    first = _identity(store, "first query")
    second = _identity(store, "second query")
    ledger = store.begin(first, _expectations(), query_vector=_query_vector())

    with pytest.raises(ExhaustiveAuditError, match="job quota"):
        store.begin(second, _expectations(), query_vector=_query_vector())

    assert store.load(first, _expectations()) is not None
    assert (tmp_path / "audit-jobs" / first.job_id / "progress.json").is_file()
    assert not (tmp_path / "audit-jobs" / second.job_id).exists()
    assert ledger.job_id == first.job_id


def test_exhaustive_total_quota_rejects_growth_without_deleting_jobs(tmp_path: Path) -> None:
    initial = ExhaustiveAuditStore(
        tmp_path,
        maximum_jobs=2,
        maximum_total_bytes=1024 * 1024,
        maximum_artifact_bytes=1024 * 1024,
    )
    first = _identity(initial, "first query")
    initial.begin(first, _expectations(), query_vector=_query_vector())
    usage = sum(
        path.stat().st_size for path in (tmp_path / "audit-jobs").rglob("*") if path.is_file()
    )
    bounded = ExhaustiveAuditStore(
        tmp_path,
        maximum_jobs=2,
        maximum_total_bytes=usage,
        maximum_artifact_bytes=usage,
    )
    second = _identity(bounded, "second query")

    with pytest.raises(ExhaustiveAuditError, match="total quota"):
        bounded.begin(second, _expectations(), query_vector=_query_vector())

    assert bounded.load(first, _expectations()) is not None
    assert not (tmp_path / "audit-jobs" / second.job_id).exists()


def test_exhaustive_artifact_cap_rejects_before_job_creation(tmp_path: Path) -> None:
    store = ExhaustiveAuditStore(
        tmp_path,
        maximum_jobs=1,
        maximum_total_bytes=1024 * 1024,
        maximum_artifact_bytes=1024,
    )

    with pytest.raises(ExhaustiveAuditError, match="artifact exceeds"):
        store.begin(
            _identity(store, "oversized vector"),
            _expectations(),
            query_vector=_query_vector(),
        )

    assert not (tmp_path / "audit-jobs").exists()


def test_reserved_free_space_gate_never_deletes_existing_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retained = tmp_path / "retained.bin"
    retained.write_bytes(b"retained")
    configure_state_quota(
        tmp_path,
        StateQuotaPolicy(
            maximum_state_bytes=1024,
            reserved_free_space_bytes=100,
            exhaustive_audit_max_jobs=1,
            exhaustive_audit_max_total_bytes=256,
            exhaustive_audit_max_artifact_bytes=128,
            reranker_audit_max_jobs=1,
            reranker_audit_max_total_bytes=256,
            reranker_audit_max_artifact_bytes=128,
        ),
    )
    monkeypatch.setattr(
        quota_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=1024, used=914, free=110),
    )

    with pytest.raises(StorageQuotaError, match="reserved free-space"):
        ensure_global_state_growth(tmp_path, 16)

    assert retained.read_bytes() == b"retained"


def test_inflight_reservation_blocks_competing_state_growth(tmp_path: Path) -> None:
    configure_state_quota(
        tmp_path,
        StateQuotaPolicy(
            maximum_state_bytes=1500,
            reserved_free_space_bytes=0,
            exhaustive_audit_max_jobs=1,
            exhaustive_audit_max_total_bytes=256,
            exhaustive_audit_max_artifact_bytes=128,
            reranker_audit_max_jobs=1,
            reranker_audit_max_total_bytes=256,
            reranker_audit_max_artifact_bytes=128,
        ),
    )
    reservation = reserve_global_state_growth(tmp_path, 1000)
    try:
        with pytest.raises(StorageQuotaError, match="state quota"):
            ensure_global_state_growth(tmp_path, 501)
    finally:
        reservation.release()

    ensure_global_state_growth(tmp_path, 501)
