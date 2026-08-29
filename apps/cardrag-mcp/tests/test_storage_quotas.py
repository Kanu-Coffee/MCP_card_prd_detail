from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import cardrag_mcp.quota as quota_module
from cardrag_mcp.audit import (
    AuditContractScore,
    AuditNodeScore,
    AuditViewScore,
    ExhaustiveAuditError,
    ExhaustiveAuditStore,
    ExpectedContract,
)
from cardrag_mcp.quota import (
    StateQuotaPolicy,
    StorageQuotaError,
    configure_state_quota,
    ensure_global_state_growth,
    reserve_global_state_growth,
    safe_shared_exhaustive_audit_usage,
    safe_tree_usage,
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


def test_shared_total_rejects_atomic_progress_replacement_peak(tmp_path: Path) -> None:
    revision_id = "revision-peak"
    expectations = (ExpectedContract(contract_revision_id=revision_id, embedding_rows=1),)

    def contract() -> AuditContractScore:
        view = AuditViewScore(row_index=0, view_type="TITLE", score=0.5)
        node = AuditNodeScore(
            node_id="node-peak",
            score=0.5,
            matched_view_types=("TITLE",),
            views=(view,),
        )
        return AuditContractScore(
            contract_revision_id=revision_id,
            aggregation_policy="max_child",
            score=0.5,
            scored_embedding_rows=1,
            exact_blocks=1,
            nodes=(node,),
        )

    calibration_root = tmp_path / "calibration"
    calibration_root.mkdir()
    calibration = ExhaustiveAuditStore(
        calibration_root,
        maximum_jobs=1,
        maximum_total_bytes=1024 * 1024,
        maximum_artifact_bytes=1024 * 1024,
    )
    calibration_identity = _identity(calibration, "replacement peak")
    calibration_ledger = calibration.begin(
        calibration_identity,
        expectations,
        query_vector=_query_vector(),
    )
    old_progress_size = (
        (calibration_root / "audit-jobs" / calibration_identity.job_id / "progress.json")
        .stat()
        .st_size
    )
    calibration.checkpoint(calibration_identity, calibration_ledger, contract())
    new_progress_size = (
        (calibration_root / "audit-jobs" / calibration_identity.job_id / "progress.json")
        .stat()
        .st_size
    )

    target_root = tmp_path / "target"
    target_root.mkdir()
    generous = ExhaustiveAuditStore(
        target_root,
        maximum_jobs=1,
        maximum_total_bytes=1024 * 1024,
        maximum_artifact_bytes=1024 * 1024,
    )
    identity = _identity(generous, "replacement peak")
    ledger = generous.begin(identity, expectations, query_vector=_query_vector())
    current_total, _ = safe_shared_exhaustive_audit_usage(target_root)
    final_logical_total = current_total + max(0, new_progress_size - old_progress_size)
    assert final_logical_total < current_total + new_progress_size
    bounded = ExhaustiveAuditStore(
        target_root,
        maximum_jobs=1,
        maximum_total_bytes=final_logical_total,
        maximum_artifact_bytes=final_logical_total,
    )
    progress = target_root / "audit-jobs" / identity.job_id / "progress.json"
    before = progress.read_bytes()

    with pytest.raises(ExhaustiveAuditError, match="total quota"):
        bounded.checkpoint(identity, ledger, contract())

    assert progress.read_bytes() == before


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


def _small_policy(maximum_state_bytes: int) -> StateQuotaPolicy:
    return StateQuotaPolicy(
        maximum_state_bytes=maximum_state_bytes,
        reserved_free_space_bytes=0,
        exhaustive_audit_max_jobs=1,
        exhaustive_audit_max_total_bytes=maximum_state_bytes,
        exhaustive_audit_max_artifact_bytes=maximum_state_bytes,
        reranker_audit_max_jobs=1,
        reranker_audit_max_total_bytes=maximum_state_bytes,
        reranker_audit_max_artifact_bytes=maximum_state_bytes,
    )


def test_reservation_coordination_bytes_count_toward_near_limit(tmp_path: Path) -> None:
    maximum = 1_000
    configure_state_quota(tmp_path, _small_policy(maximum))
    policy_usage = safe_tree_usage(tmp_path)
    assert 0 < policy_usage < maximum - 250
    (tmp_path / "padding.bin").write_bytes(b"x" * (maximum - 250 - policy_usage))
    assert safe_tree_usage(tmp_path) == maximum - 250

    # The requested 150 bytes alone fit with 100 bytes to spare.  Its durable
    # reservation record does not, and must not bypass the hard state quota.
    with pytest.raises(StorageQuotaError, match="state quota"):
        reserve_global_state_growth(tmp_path, 150)

    assert not tuple((tmp_path / "audit-reports" / ".state-quota").glob("reservation-*.json"))


def test_policy_hardlink_crash_is_reconciled_only_to_exact_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_unlink = Path.unlink
    crashed = False

    def crash_after_link(path: Path, *args: object, **kwargs: object) -> None:
        nonlocal crashed
        if path.name.startswith(".quota-owned-") and not crashed:
            crashed = True
            raise OSError("simulated crash after durable link")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", crash_after_link)
    with pytest.raises(OSError, match="simulated crash"):
        configure_state_quota(tmp_path, _small_policy(4_096))
    monkeypatch.setattr(Path, "unlink", original_unlink)

    coordination = tmp_path / "audit-reports" / ".state-quota"
    temporary = next(coordination.glob(".quota-owned-*"))
    target = coordination / "policy.json"
    assert temporary.stat().st_ino == target.stat().st_ino
    assert temporary.stat().st_nlink == 2

    configure_state_quota(tmp_path, _small_policy(4_096))

    assert not tuple(coordination.glob(".quota-owned-*"))
    assert target.stat().st_nlink == 1


def test_reservation_hardlink_crash_recovers_and_remains_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_state_quota(tmp_path, _small_policy(2_000))
    original_unlink = Path.unlink
    crashed = False

    def crash_after_link(path: Path, *args: object, **kwargs: object) -> None:
        nonlocal crashed
        if path.name.startswith(".quota-owned-") and not crashed:
            crashed = True
            raise OSError("simulated reservation publication crash")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", crash_after_link)
    with pytest.raises(OSError, match="publication crash"):
        reserve_global_state_growth(tmp_path, 1_000)
    monkeypatch.setattr(Path, "unlink", original_unlink)

    coordination = tmp_path / "audit-reports" / ".state-quota"
    temporary = next(coordination.glob(".quota-owned-*"))
    target = next(coordination.glob("reservation-*.json"))
    assert temporary.stat().st_ino == target.stat().st_ino
    assert temporary.stat().st_nlink == 2

    with pytest.raises(StorageQuotaError, match="state quota"):
        ensure_global_state_growth(tmp_path, 1_001)

    assert not tuple(coordination.glob(".quota-owned-*"))
    assert target.stat().st_nlink == 1
