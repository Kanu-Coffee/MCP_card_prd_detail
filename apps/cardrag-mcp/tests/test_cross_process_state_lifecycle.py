from __future__ import annotations

import hashlib
import multiprocessing
import queue as queue_module
import threading
import time
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pytest
from v5_fixtures import build_v5_fixture, install_v5_fixture

from cardrag_mcp.audit import (
    AuditContractScore,
    AuditNodeScore,
    AuditViewScore,
    ExhaustiveAuditError,
    ExhaustiveAuditStore,
    ExpectedContract,
)
from cardrag_mcp.experimental_map_reduce import (
    ExperimentalMapReduceError,
    ExperimentalMapReduceProfile,
    ExperimentalMapReduceStore,
    _build_corpus_snapshot,
)
from cardrag_mcp.experimental_map_reduce import (
    _identity as _map_identity,
)
from cardrag_mcp.quota import (
    StateQuotaPolicy,
    configure_state_quota,
    reconcile_abandoned_state_reservations,
    reserve_global_state_growth,
    safe_shared_exhaustive_audit_usage,
    safe_subtree_usage,
    safe_tree_usage,
    state_quota_guard,
    state_quota_policy,
)
from cardrag_mcp.store import GenerationStore, cas_path

_PROCESS_TIMEOUT_SECONDS = 15.0
_SYNC_TIMEOUT_SECONDS = 10.0
_VECTOR_BYTES_LIMIT = 2 * 1024 * 1024
_AUDIT_ARTIFACT_LIMIT = 1024 * 1024
_AUDIT_TOTAL_LIMIT = 8 * 1024 * 1024


def _query_vector() -> np.ndarray:
    vector = np.zeros((4096,), dtype=np.float32)
    vector[0] = 1.0
    return vector


def _expectations(revision_id: str = "revision-1") -> tuple[ExpectedContract, ...]:
    return (ExpectedContract(contract_revision_id=revision_id, embedding_rows=1),)


def _map_profile() -> ExperimentalMapReduceProfile:
    return ExperimentalMapReduceProfile.seal(
        model="openai/reasoning-test",
        provider_id="test-provider",
        evaluation_artifact_sha256="a" * 64,
        maximum_input_characters=16_384,
        maximum_completion_tokens=4_096,
        maximum_response_bytes=1024 * 1024,
        maximum_job_provider_calls=4_096,
        maximum_job_input_characters=268_435_456,
        maximum_job_output_tokens=16_777_216,
    )


def _identity(store: ExhaustiveAuditStore, query: str):
    return store.identity("generation-v5", hashlib.sha256(query.encode()).hexdigest())


def _policy(maximum_state_bytes: int) -> StateQuotaPolicy:
    subtree_limit = min(maximum_state_bytes // 2, _AUDIT_TOTAL_LIMIT)
    artifact_limit = min(subtree_limit, _AUDIT_ARTIFACT_LIMIT)
    return StateQuotaPolicy(
        maximum_state_bytes=maximum_state_bytes,
        reserved_free_space_bytes=0,
        exhaustive_audit_max_jobs=64,
        exhaustive_audit_max_total_bytes=subtree_limit,
        exhaustive_audit_max_artifact_bytes=artifact_limit,
        reranker_audit_max_jobs=64,
        reranker_audit_max_total_bytes=subtree_limit,
        reranker_audit_max_artifact_bytes=artifact_limit,
    )


def _configure_quota_root(root: Path, *, maximum_state_bytes: int = 32 * 1024 * 1024) -> None:
    root.mkdir(parents=True)
    configure_state_quota(root, _policy(maximum_state_bytes))


def _audit_begin_worker(
    root_text: str,
    worker_index: int,
    barrier: Any,
    results: Any,
    maximum_jobs: int,
    maximum_total_bytes: int,
) -> None:
    try:
        store = ExhaustiveAuditStore(
            Path(root_text),
            maximum_jobs=maximum_jobs,
            maximum_total_bytes=maximum_total_bytes,
            maximum_artifact_bytes=min(maximum_total_bytes, _AUDIT_ARTIFACT_LIMIT),
        )
        identity = _identity(store, f"cross-process audit query {worker_index}")
        barrier.wait(timeout=_SYNC_TIMEOUT_SECONDS)
        ledger = store.begin(identity, _expectations(), query_vector=_query_vector())
        results.put(("ok", worker_index, ledger.job_id))
    except Exception as exc:
        results.put(("error", worker_index, type(exc).__name__, str(exc)))


def _reservation_worker(
    root_text: str,
    amount: int,
    barrier: Any | None,
    results: Any,
) -> None:
    try:
        if barrier is not None:
            barrier.wait(timeout=_SYNC_TIMEOUT_SECONDS)
        reservation = reserve_global_state_growth(Path(root_text), amount)
        results.put(("ok", reservation.token))
        # Deliberately exit without release: an interrupted async writer must
        # remain charged conservatively after its process disappears.
    except Exception as exc:
        results.put(("error", type(exc).__name__, str(exc)))


def _holding_reservation_worker(
    root_text: str,
    amount: int,
    ready: Any,
    release: Any,
    results: Any,
) -> None:
    try:
        reservation = reserve_global_state_growth(Path(root_text), amount)
        results.put(("held", reservation.token))
        ready.set()
        if not release.wait(_SYNC_TIMEOUT_SECONDS):
            raise TimeoutError("reservation release event timed out")
        reservation.release()
        results.put(("released", reservation.token))
    except Exception as exc:
        results.put(("error", type(exc).__name__, str(exc)))


def _partial_reservation_worker(
    root_text: str,
    reserved_amount: int,
    partial_amount: int,
    results: Any,
) -> None:
    try:
        root = Path(root_text)
        reservation = reserve_global_state_growth(root, reserved_amount)
        partial = root / "abandoned-partial.bin"
        partial.write_bytes(b"p" * partial_amount)
        results.put(("ok", reservation.token, partial.stat().st_size))
        # Simulate a killed async writer: neither the partial output nor its
        # durable lease record is cleaned up before process exit.
    except Exception as exc:
        results.put(("error", type(exc).__name__, str(exc)))


def _capacity_check_worker(root_text: str, amount: int, results: Any) -> None:
    try:
        root = Path(root_text)
        durable_policy = state_quota_policy(root)
        with state_quota_guard(root, amount):
            (root / "competing-sync-write.bin").write_bytes(b"x" * amount)
        results.put(("ok", durable_policy.maximum_state_bytes))
    except Exception as exc:
        results.put(("error", type(exc).__name__, str(exc)))


def _policy_config_worker(root_text: str, maximum_state_bytes: int, results: Any) -> None:
    try:
        configure_state_quota(Path(root_text), _policy(maximum_state_bytes))
        results.put(("ok", maximum_state_bytes))
    except Exception as exc:
        results.put(("error", type(exc).__name__, str(exc)))


def _generation_store(root_text: str, *, retention: int = 2) -> GenerationStore:
    return GenerationStore(
        Path(root_text),
        maximum_vector_bytes=_VECTOR_BYTES_LIMIT,
        retention=retention,
    )


def _shared_creation_worker(
    root_text: str,
    kind: Literal["audit", "map"],
    is_first: bool,
    maximum_jobs: int,
    maximum_total_bytes: int,
    ready_barrier: Any,
    first_has_quota_lock: Any,
    start: Any,
    results: Any,
) -> None:
    try:
        root = Path(root_text)
        create_job: Callable[[], str]
        if kind == "audit":
            audit_store = ExhaustiveAuditStore(
                root,
                maximum_jobs=maximum_jobs,
                maximum_total_bytes=maximum_total_bytes,
                maximum_artifact_bytes=min(maximum_total_bytes, _AUDIT_ARTIFACT_LIMIT),
            )
            audit_identity = _identity(audit_store, "shared audit/map audit query")

            def create_audit_job() -> str:
                ledger = audit_store.begin(
                    audit_identity,
                    _expectations(),
                    query_vector=_query_vector(),
                )
                return ledger.job_id

            create_job = create_audit_job
        else:
            generation_store = _generation_store(root_text)
            if not generation_store.load_current():
                raise RuntimeError("map worker could not load the current generation")
            profile = _map_profile()
            query = "shared audit/map map query"
            with generation_store.pin() as handle:
                map_identity = _map_identity(
                    handle.generation_id,
                    hashlib.sha256(query.encode()).hexdigest(),
                    profile.profile_id,
                )
                snapshot = _build_corpus_snapshot(handle, profile, query)
            map_store = ExperimentalMapReduceStore(
                root,
                maximum_jobs=maximum_jobs,
                maximum_total_bytes=maximum_total_bytes,
                maximum_artifact_bytes=min(maximum_total_bytes, _AUDIT_ARTIFACT_LIMIT),
            )

            def create_map_job() -> str:
                ledger = map_store.begin(map_identity, profile, snapshot)
                return ledger.identity.job_id

            create_job = create_map_job

        ready_barrier.wait(timeout=_SYNC_TIMEOUT_SECONDS)
        if is_first:
            with state_quota_guard(root, 0):
                first_has_quota_lock.set()
                if not start.wait(_SYNC_TIMEOUT_SECONDS):
                    raise TimeoutError("shared creation start event timed out")
                job_id = create_job()
        else:
            if not first_has_quota_lock.wait(_SYNC_TIMEOUT_SECONDS):
                raise TimeoutError("first shared creator did not acquire quota lock")
            if not start.wait(_SYNC_TIMEOUT_SECONDS):
                raise TimeoutError("shared creation start event timed out")
            job_id = create_job()
        results.put((kind, "ok", job_id))
    except Exception as exc:
        results.put((kind, "error", type(exc).__name__, str(exc)))


def _inherited_store_worker(
    store: GenerationStore,
    expected_generation_id: str,
    results: Any,
) -> None:
    try:
        inherited_items = store._entries.items()
        inherited_references = tuple(
            sorted((generation_id, entry.references) for generation_id, entry in inherited_items)
        )
        active_before_reload = store.active_generation_id
        entries_were_cleared = not store._entries
        loaded = store.load_current()
        active_after_reload = store.active_generation_id

        def owner_for_generation(generation_id: str) -> str:
            digest = hashlib.sha256(f"fork-claim:{generation_id}".encode()).hexdigest()
            return f"map-reduce-{digest}"

        claimed = store.claim_current_generation_gc_root(owner_for_generation)
        reloaded_items = store._entries.items()
        reloaded_entries = tuple(
            sorted((generation_id, entry.references) for generation_id, entry in reloaded_items)
        )
        results.put(
            (
                "ok",
                inherited_references,
                active_before_reload,
                entries_were_cleared,
                loaded,
                active_after_reload,
                claimed,
                reloaded_entries,
                expected_generation_id,
            )
        )
    except Exception as exc:
        results.put(("error", type(exc).__name__, str(exc)))


def _root_publisher_worker(
    root_text: str,
    owner_id: str,
    generation_id: str,
    ready: Any,
    start: Any,
    lifecycle_held: Any,
    allow_publish: Any,
    published: Any,
    results: Any,
) -> None:
    try:
        store = _generation_store(root_text)
        if not store.load_current():
            raise RuntimeError("publisher could not load the current generation")
        ready.set()
        if not start.wait(_SYNC_TIMEOUT_SECONDS):
            raise TimeoutError("publisher start event timed out")
        with store._generation_lifecycle():
            lifecycle_held.set()
            if not allow_publish.wait(_SYNC_TIMEOUT_SECONDS):
                raise TimeoutError("publisher release event timed out")
            store.acquire_generation_gc_root(owner_id, generation_id)
            published.set()
        results.put(("publisher", "ok"))
    except Exception as exc:
        results.put(("publisher", "error", type(exc).__name__, str(exc)))


def _activation_worker(
    root_text: str,
    candidate_generation_id: str,
    ready: Any,
    start: Any,
    attempted: Any,
    roots_scanned: Any,
    done: Any,
    results: Any,
) -> None:
    try:
        store = _generation_store(root_text)
        if not store.load_current():
            raise RuntimeError("activator could not load the current generation")
        with store.pin_generation(candidate_generation_id) as handle:
            original_scan = store._durable_generation_roots_locked

            def observed_scan() -> dict[str, str]:
                roots_scanned.set()
                return original_scan()

            store._durable_generation_roots_locked = observed_scan  # type: ignore[method-assign]
            ready.set()
            if not start.wait(_SYNC_TIMEOUT_SECONDS):
                raise TimeoutError("activation start event timed out")
            attempted.set()
            store.activate(handle)
        results.put(("activation", "ok"))
    except Exception as exc:
        results.put(("activation", "error", type(exc).__name__, str(exc)))
    finally:
        done.set()


def _activation_loop_worker(
    root_text: str,
    generation_ids: tuple[str, str],
    barrier: Any,
    results: Any,
) -> None:
    try:
        store = _generation_store(root_text)
        if not store.load_current():
            raise RuntimeError("activation loop could not load the current generation")
        barrier.wait(timeout=_SYNC_TIMEOUT_SECONDS)
        for index in range(12):
            with store.pin_generation(generation_ids[index % len(generation_ids)]) as handle:
                store.activate(handle)
        results.put(("activation-loop", "ok"))
    except Exception as exc:
        results.put(("activation-loop", "error", type(exc).__name__, str(exc)))


def _checkpoint_loop_worker(root_text: str, barrier: Any, results: Any) -> None:
    try:
        store = ExhaustiveAuditStore(
            Path(root_text),
            maximum_jobs=64,
            maximum_total_bytes=_AUDIT_TOTAL_LIMIT,
            maximum_artifact_bytes=_AUDIT_ARTIFACT_LIMIT,
        )
        barrier.wait(timeout=_SYNC_TIMEOUT_SECONDS)
        for index in range(12):
            revision_id = f"revision-{index:02d}"
            identity = _identity(store, f"checkpoint lock-order query {index}")
            ledger = store.begin(
                identity,
                _expectations(revision_id),
                query_vector=_query_vector(),
            )
            view = AuditViewScore(row_index=0, view_type="TITLE", score=0.5)
            node = AuditNodeScore(
                node_id=f"node-{index:02d}",
                score=0.5,
                matched_view_types=("TITLE",),
                views=(view,),
            )
            contract = AuditContractScore(
                contract_revision_id=revision_id,
                aggregation_policy="max_child",
                score=0.5,
                scored_embedding_rows=1,
                exact_blocks=1,
                nodes=(node,),
            )
            store.checkpoint(identity, ledger, contract)
        results.put(("checkpoint-loop", "ok"))
    except Exception as exc:
        results.put(("checkpoint-loop", "error", type(exc).__name__, str(exc)))


def _join_or_terminate(processes: list[Any], *, timeout: float = _PROCESS_TIMEOUT_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    for process in processes:
        process.join(max(0.0, deadline - time.monotonic()))
    stuck = [process for process in processes if process.is_alive()]
    for process in stuck:
        process.terminate()
    for process in stuck:
        process.join(2.0)
    if stuck:
        pytest.fail(f"multiprocessing workers deadlocked: {[process.pid for process in stuck]}")
    failures = [(process.pid, process.exitcode) for process in processes if process.exitcode != 0]
    assert not failures, f"multiprocessing workers exited abnormally: {failures}"


def _collect(results: Any, count: int) -> list[tuple[Any, ...]]:
    return [results.get(timeout=5.0) for _ in range(count)]


def _prepare_candidate(store: GenerationStore, generation_id: str) -> None:
    fixture = build_v5_fixture(store.generations / generation_id, generation_id=generation_id)
    for digest, body in fixture.pdf_objects:
        target = cas_path(store.objects, digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            assert target.read_bytes() == body
        else:
            target.write_bytes(body)


def _calibrate_audit_job_bytes(root: Path) -> int:
    _configure_quota_root(root)
    store = ExhaustiveAuditStore(
        root,
        maximum_jobs=2,
        maximum_total_bytes=_AUDIT_TOTAL_LIMIT,
        maximum_artifact_bytes=_AUDIT_ARTIFACT_LIMIT,
    )
    store.begin(
        _identity(store, "shared audit/map audit query"),
        _expectations(),
        query_vector=_query_vector(),
    )
    total, jobs = safe_shared_exhaustive_audit_usage(root)
    assert jobs == 1
    return total


def _calibrate_map_job_bytes(root: Path) -> int:
    generation_store = GenerationStore(
        root,
        maximum_vector_bytes=_VECTOR_BYTES_LIMIT,
        retention=2,
    )
    install_v5_fixture(generation_store, generation_id="gen-shared-target")
    profile = _map_profile()
    query = "shared audit/map map query"
    with generation_store.pin() as handle:
        identity = _map_identity(
            handle.generation_id,
            hashlib.sha256(query.encode()).hexdigest(),
            profile.profile_id,
        )
        snapshot = _build_corpus_snapshot(handle, profile, query)
    ExperimentalMapReduceStore(
        root,
        maximum_jobs=2,
        maximum_total_bytes=_AUDIT_TOTAL_LIMIT,
        maximum_artifact_bytes=_AUDIT_ARTIFACT_LIMIT,
    ).begin(identity, profile, snapshot)
    total, jobs = safe_shared_exhaustive_audit_usage(root)
    assert jobs == 1
    return total


def test_cross_process_maximum_jobs_allows_exactly_one_audit_begin(tmp_path: Path) -> None:
    root = tmp_path / "state"
    _configure_quota_root(root)
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(3)
    results = context.Queue()
    processes = [
        context.Process(
            target=_audit_begin_worker,
            args=(str(root), index, barrier, results, 1, _AUDIT_TOTAL_LIMIT),
        )
        for index in range(2)
    ]
    for process in processes:
        process.start()
    barrier.wait(timeout=_SYNC_TIMEOUT_SECONDS)
    _join_or_terminate(processes)
    outcomes = _collect(results, 2)

    successes = [outcome for outcome in outcomes if outcome[0] == "ok"]
    failures = [outcome for outcome in outcomes if outcome[0] == "error"]
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0][2] == "ExhaustiveAuditError"
    assert "job quota" in failures[0][3]
    assert len(list((root / "audit-jobs").glob("audit-*"))) == 1


def test_cross_process_total_bytes_allows_exactly_one_near_limit_begin(
    tmp_path: Path,
) -> None:
    calibration_root = tmp_path / "calibration"
    _configure_quota_root(calibration_root)
    calibration = ExhaustiveAuditStore(
        calibration_root,
        maximum_jobs=1,
        maximum_total_bytes=_AUDIT_TOTAL_LIMIT,
        maximum_artifact_bytes=_AUDIT_ARTIFACT_LIMIT,
    )
    calibration.begin(
        _identity(calibration, "cross-process audit query 0"),
        _expectations(),
        query_vector=_query_vector(),
    )
    one_job_bytes = safe_subtree_usage(calibration_root, calibration_root / "audit-jobs")

    root = tmp_path / "state"
    _configure_quota_root(root)
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(3)
    results = context.Queue()
    processes = [
        context.Process(
            target=_audit_begin_worker,
            args=(str(root), index, barrier, results, 2, one_job_bytes),
        )
        for index in range(2)
    ]
    for process in processes:
        process.start()
    barrier.wait(timeout=_SYNC_TIMEOUT_SECONDS)
    _join_or_terminate(processes)
    outcomes = _collect(results, 2)

    successes = [outcome for outcome in outcomes if outcome[0] == "ok"]
    failures = [outcome for outcome in outcomes if outcome[0] == "error"]
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0][2] == "ExhaustiveAuditError"
    assert "total quota" in failures[0][3]
    assert safe_subtree_usage(root, root / "audit-jobs") == one_job_bytes


def test_exited_process_durable_reservation_blocks_restart(tmp_path: Path) -> None:
    root = tmp_path / "state"
    _configure_quota_root(root, maximum_state_bytes=1_000_000)
    context = multiprocessing.get_context("fork")
    reservation_results = context.Queue()
    reserving = context.Process(
        target=_reservation_worker,
        args=(str(root), 600_000, None, reservation_results),
    )
    reserving.start()
    _join_or_terminate([reserving])
    reserved = _collect(reservation_results, 1)[0]
    assert reserved[0] == "ok"
    assert list((root / "audit-reports" / ".state-quota").glob("reservation-*.json"))

    restart_results = context.Queue()
    restarted = context.Process(
        target=_capacity_check_worker,
        args=(str(root), 600_000, restart_results),
    )
    restarted.start()
    _join_or_terminate([restarted])
    blocked = _collect(restart_results, 1)[0]
    assert blocked[0] == "error"
    assert blocked[1] == "StorageQuotaError"
    assert "state quota" in blocked[2]
    assert not (root / "competing-sync-write.bin").exists()

    assert reconcile_abandoned_state_reservations(root) == (reserved[1],)
    assert not list((root / "audit-reports" / ".state-quota").glob("reservation-*.json"))

    restored_results = context.Queue()
    restored = context.Process(
        target=_capacity_check_worker,
        args=(str(root), 600_000, restored_results),
    )
    restored.start()
    _join_or_terminate([restored])
    restored_outcome = _collect(restored_results, 1)[0]
    assert restored_outcome == ("ok", 1_000_000)
    assert (root / "competing-sync-write.bin").stat().st_size == 600_000


def test_reconciliation_does_not_remove_a_live_reservation_lease(tmp_path: Path) -> None:
    root = tmp_path / "state"
    _configure_quota_root(root, maximum_state_bytes=1_000_000)
    context = multiprocessing.get_context("fork")
    ready = context.Event()
    release = context.Event()
    results = context.Queue()
    holder = context.Process(
        target=_holding_reservation_worker,
        args=(str(root), 600_000, ready, release, results),
    )
    held: tuple[Any, ...] | None = None
    holder.start()
    try:
        assert ready.wait(_SYNC_TIMEOUT_SECONDS)
        held = _collect(results, 1)[0]
        assert held[0] == "held"
        assert reconcile_abandoned_state_reservations(root) == ()
        records = list((root / "audit-reports" / ".state-quota").glob("reservation-*.json"))
        assert [record.name for record in records] == [f"reservation-{held[1]}.json"]
    finally:
        release.set()
    _join_or_terminate([holder])
    assert held is not None
    assert _collect(results, 1)[0] == ("released", held[1])
    assert not list((root / "audit-reports" / ".state-quota").glob("reservation-*.json"))


def test_reconciliation_keeps_abandoned_partial_bytes_charged(tmp_path: Path) -> None:
    root = tmp_path / "state"
    _configure_quota_root(root, maximum_state_bytes=1_000_000)
    context = multiprocessing.get_context("fork")
    results = context.Queue()
    writer = context.Process(
        target=_partial_reservation_worker,
        args=(str(root), 600_000, 300_000, results),
    )
    writer.start()
    _join_or_terminate([writer])
    outcome = _collect(results, 1)[0]
    assert outcome[0] == "ok"
    token = outcome[1]
    partial = root / "abandoned-partial.bin"
    assert outcome[2] == partial.stat().st_size == 300_000
    usage_with_record = safe_tree_usage(root)

    assert reconcile_abandoned_state_reservations(root) == (token,)
    usage_after_reconciliation = safe_tree_usage(root)
    assert usage_after_reconciliation < usage_with_record
    assert partial.read_bytes() == b"p" * 300_000
    assert not list((root / "audit-reports" / ".state-quota").glob("reservation-*.json"))

    capacity_results = context.Queue()
    competing = context.Process(
        target=_capacity_check_worker,
        args=(str(root), 800_000, capacity_results),
    )
    competing.start()
    _join_or_terminate([competing])
    blocked = _collect(capacity_results, 1)[0]
    assert blocked[0] == "error"
    assert blocked[1] == "StorageQuotaError"
    assert "state quota" in blocked[2]
    assert partial.stat().st_size == 300_000
    assert not (root / "competing-sync-write.bin").exists()


def test_simultaneous_durable_reservations_allow_exactly_one(tmp_path: Path) -> None:
    root = tmp_path / "state"
    _configure_quota_root(root, maximum_state_bytes=1_000_000)
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(3)
    results = context.Queue()
    processes = [
        context.Process(
            target=_reservation_worker,
            args=(str(root), 600_000, barrier, results),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    barrier.wait(timeout=_SYNC_TIMEOUT_SECONDS)
    _join_or_terminate(processes)
    outcomes = _collect(results, 2)

    successes = [outcome for outcome in outcomes if outcome[0] == "ok"]
    failures = [outcome for outcome in outcomes if outcome[0] == "error"]
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0][1] == "StorageQuotaError"
    assert "state quota" in failures[0][2]
    assert len(list((root / "audit-reports" / ".state-quota").glob("reservation-*.json"))) == 1


def test_process_restart_rejects_different_durable_state_quota_policy(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    _configure_quota_root(root, maximum_state_bytes=1_000_000)
    context = multiprocessing.get_context("fork")
    results = context.Queue()
    restarted = context.Process(
        target=_policy_config_worker,
        args=(str(root), 1_000_001, results),
    )
    restarted.start()
    _join_or_terminate([restarted])
    outcome = _collect(results, 1)[0]

    assert outcome[0] == "error"
    assert outcome[1] == "StorageQuotaError"
    assert "durable policy" in outcome[2]
    assert state_quota_policy(root).maximum_state_bytes == 1_000_000


@pytest.mark.parametrize("quota_kind", ("jobs", "total"))
@pytest.mark.parametrize("first_kind", ("audit", "map"))
def test_audit_and_map_creation_share_cross_process_quota_in_both_directions(
    tmp_path: Path,
    quota_kind: Literal["jobs", "total"],
    first_kind: Literal["audit", "map"],
) -> None:
    if quota_kind == "total":
        audit_bytes = _calibrate_audit_job_bytes(tmp_path / "audit-calibration")
        map_bytes = _calibrate_map_job_bytes(tmp_path / "map-calibration")
        maximum_jobs = 2
        maximum_total_bytes = max(audit_bytes, map_bytes)
        expected_first_bytes = audit_bytes if first_kind == "audit" else map_bytes
    else:
        maximum_jobs = 1
        maximum_total_bytes = _AUDIT_TOTAL_LIMIT
        expected_first_bytes = None

    root = tmp_path / "state"
    generation_store = GenerationStore(
        root,
        maximum_vector_bytes=_VECTOR_BYTES_LIMIT,
        retention=2,
    )
    install_v5_fixture(generation_store, generation_id="gen-shared-target")
    context = multiprocessing.get_context("fork")
    ready_barrier = context.Barrier(3)
    first_has_quota_lock = context.Event()
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_shared_creation_worker,
            args=(
                str(root),
                kind,
                kind == first_kind,
                maximum_jobs,
                maximum_total_bytes,
                ready_barrier,
                first_has_quota_lock,
                start,
                results,
            ),
        )
        for kind in ("audit", "map")
    ]
    for process in processes:
        process.start()
    try:
        ready_barrier.wait(timeout=_SYNC_TIMEOUT_SECONDS)
        assert first_has_quota_lock.wait(_SYNC_TIMEOUT_SECONDS)
        start.set()
    finally:
        start.set()
    _join_or_terminate(processes)
    outcomes = {outcome[0]: outcome for outcome in _collect(results, 2)}

    assert outcomes[first_kind][1] == "ok"
    losing_kind = "map" if first_kind == "audit" else "audit"
    failure = outcomes[losing_kind]
    assert failure[1] == "error"
    expected_error = (
        "ExperimentalMapReduceError" if losing_kind == "map" else "ExhaustiveAuditError"
    )
    assert failure[2] == expected_error
    quota_label = "job" if quota_kind == "jobs" else "total"
    assert f"{quota_label} quota" in failure[3]
    shared_bytes, shared_jobs = safe_shared_exhaustive_audit_usage(root)
    assert shared_jobs == 1
    assert shared_bytes <= maximum_total_bytes
    if expected_first_bytes is not None:
        assert shared_bytes == expected_first_bytes


@pytest.mark.parametrize("lane_kind", ("audit", "map"))
def test_shared_quota_rejects_zero_logical_growth_replacement_peak(
    tmp_path: Path,
    lane_kind: Literal["audit", "map"],
) -> None:
    root = tmp_path / "state"
    if lane_kind == "audit":
        _configure_quota_root(root)
        initial_store = ExhaustiveAuditStore(
            root,
            maximum_jobs=2,
            maximum_total_bytes=_AUDIT_TOTAL_LIMIT,
            maximum_artifact_bytes=_AUDIT_ARTIFACT_LIMIT,
        )
        identity = _identity(initial_store, "replacement peak audit query")
        initial_store.begin(identity, _expectations(), query_vector=_query_vector())
        progress = root / "audit-jobs" / identity.job_id / "progress.json"
        original_progress = progress.read_bytes()
        shared_bytes, jobs = safe_shared_exhaustive_audit_usage(root)
        constrained_store = ExhaustiveAuditStore(
            root,
            maximum_jobs=2,
            maximum_total_bytes=shared_bytes,
            maximum_artifact_bytes=min(shared_bytes, _AUDIT_ARTIFACT_LIMIT),
        )
        with pytest.raises(ExhaustiveAuditError, match="total quota"):
            constrained_store.begin(identity, _expectations(), query_vector=_query_vector())
    else:
        generation_store = GenerationStore(
            root,
            maximum_vector_bytes=_VECTOR_BYTES_LIMIT,
            retention=2,
        )
        install_v5_fixture(generation_store, generation_id="gen-peak-target")
        profile = _map_profile()
        query = "replacement peak map query"
        with generation_store.pin() as handle:
            identity = _map_identity(
                handle.generation_id,
                hashlib.sha256(query.encode()).hexdigest(),
                profile.profile_id,
            )
            snapshot = _build_corpus_snapshot(handle, profile, query)
        initial_store = ExperimentalMapReduceStore(
            root,
            maximum_jobs=2,
            maximum_total_bytes=_AUDIT_TOTAL_LIMIT,
            maximum_artifact_bytes=_AUDIT_ARTIFACT_LIMIT,
        )
        initial_store.begin(identity, profile, snapshot)
        progress = root / "experimental-map-reduce-jobs" / identity.job_id / "progress.json"
        original_progress = progress.read_bytes()
        shared_bytes, jobs = safe_shared_exhaustive_audit_usage(root)
        constrained_store = ExperimentalMapReduceStore(
            root,
            maximum_jobs=2,
            maximum_total_bytes=shared_bytes,
            maximum_artifact_bytes=min(shared_bytes, _AUDIT_ARTIFACT_LIMIT),
        )
        with pytest.raises(ExperimentalMapReduceError, match="total quota"):
            constrained_store.begin(identity, profile, snapshot)

    assert jobs == 1
    assert progress.read_bytes() == original_progress
    assert safe_shared_exhaustive_audit_usage(root) == (shared_bytes, 1)
    assert not list(progress.parent.glob(".progress.json.*"))


def test_lifecycle_flock_publishes_root_before_retention_activation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    store = GenerationStore(root, maximum_vector_bytes=_VECTOR_BYTES_LIMIT, retention=2)
    generation_a = "gen-lifecycle-A"
    generation_b = "gen-lifecycle-B"
    generation_c = "gen-lifecycle-C"
    install_v5_fixture(store, generation_id=generation_a)
    install_v5_fixture(store, generation_id=generation_b)
    _prepare_candidate(store, generation_c)
    owner_id = "map-reduce-" + "1" * 64

    context = multiprocessing.get_context("fork")
    publisher_ready = context.Event()
    activation_ready = context.Event()
    publisher_start = context.Event()
    activation_start = context.Event()
    lifecycle_held = context.Event()
    activation_attempted = context.Event()
    roots_scanned = context.Event()
    allow_publish = context.Event()
    published = context.Event()
    activation_done = context.Event()
    results = context.Queue()
    publisher = context.Process(
        target=_root_publisher_worker,
        args=(
            str(root),
            owner_id,
            generation_a,
            publisher_ready,
            publisher_start,
            lifecycle_held,
            allow_publish,
            published,
            results,
        ),
    )
    activator = context.Process(
        target=_activation_worker,
        args=(
            str(root),
            generation_c,
            activation_ready,
            activation_start,
            activation_attempted,
            roots_scanned,
            activation_done,
            results,
        ),
    )
    publisher.start()
    activator.start()
    try:
        assert publisher_ready.wait(_SYNC_TIMEOUT_SECONDS)
        assert activation_ready.wait(_SYNC_TIMEOUT_SECONDS)
        publisher_start.set()
        assert lifecycle_held.wait(_SYNC_TIMEOUT_SECONDS)
        activation_start.set()
        assert activation_attempted.wait(_SYNC_TIMEOUT_SECONDS)
        assert not roots_scanned.wait(0.5)
        assert not activation_done.is_set()
        allow_publish.set()
        assert published.wait(_SYNC_TIMEOUT_SECONDS)
    finally:
        allow_publish.set()
        publisher_start.set()
        activation_start.set()
    _join_or_terminate([publisher, activator])
    outcomes = sorted(_collect(results, 2))
    assert outcomes == [("activation", "ok"), ("publisher", "ok")]
    assert roots_scanned.is_set()

    restarted = GenerationStore(root, maximum_vector_bytes=_VECTOR_BYTES_LIMIT, retention=2)
    assert restarted.load_current() is True
    assert restarted.active_generation_id == generation_c
    assert restarted.generation_for_gc_root(owner_id) == generation_a
    assert (restarted.generations / generation_a).is_dir()


def test_activation_and_checkpoint_loops_terminate_without_deadlock(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store = GenerationStore(root, maximum_vector_bytes=_VECTOR_BYTES_LIMIT, retention=2)
    generation_a = "gen-deadlock-A"
    generation_b = "gen-deadlock-B"
    install_v5_fixture(store, generation_id=generation_a)
    install_v5_fixture(store, generation_id=generation_b)

    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(3)
    results = context.Queue()
    processes = [
        context.Process(
            target=_activation_loop_worker,
            args=(str(root), (generation_a, generation_b), barrier, results),
        ),
        context.Process(
            target=_checkpoint_loop_worker,
            args=(str(root), barrier, results),
        ),
    ]
    for process in processes:
        process.start()
    barrier.wait(timeout=_SYNC_TIMEOUT_SECONDS)
    _join_or_terminate(processes)
    outcomes = sorted(_collect(results, 2))

    assert outcomes == [("activation-loop", "ok"), ("checkpoint-loop", "ok")]
    assert len(list((root / "audit-jobs").glob("audit-*"))) == 12


def test_forked_generation_store_discards_inherited_locked_handles_and_reloads(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    store = GenerationStore(root, maximum_vector_bytes=_VECTOR_BYTES_LIMIT, retention=2)
    generation_a = "gen-fork-A"
    generation_b = "gen-fork-B"
    install_v5_fixture(store, generation_id=generation_a)
    install_v5_fixture(store, generation_id=generation_b)
    pinned = store.pin_generation(generation_a)
    pinned_handle = pinned.__enter__()
    assert pinned_handle.generation_id == generation_a

    lock_held = threading.Event()
    release_parent_lock = threading.Event()

    def hold_parent_store_lock() -> None:
        with store._lock:
            lock_held.set()
            release_parent_lock.wait(_SYNC_TIMEOUT_SECONDS)

    holder = threading.Thread(target=hold_parent_store_lock, name="hold-generation-store-lock")
    holder.start()
    assert lock_held.wait(_SYNC_TIMEOUT_SECONDS)

    context = multiprocessing.get_context("fork")
    results = context.Queue()
    child = context.Process(
        target=_inherited_store_worker,
        args=(store, generation_b, results),
    )
    outcome: tuple[Any, ...] | None = None
    child_was_stuck = False
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=".*fork.*")
            child.start()
        try:
            outcome = results.get(timeout=5.0)
        except queue_module.Empty:
            child_was_stuck = True
    finally:
        release_parent_lock.set()
        holder.join(2.0)
        child.join(2.0)
        if child.is_alive():
            child.terminate()
            child.join(2.0)
            child_was_stuck = True
        pinned.__exit__(None, None, None)

    assert not holder.is_alive()
    assert not child_was_stuck, "forked GenerationStore deadlocked on an inherited local lock"
    assert child.exitcode == 0
    assert outcome is not None
    assert outcome[0] == "ok"
    inherited_references = dict(outcome[1])
    assert inherited_references[generation_a] == 1
    assert outcome[2] is None
    assert outcome[3] is True
    assert outcome[4] is True
    assert outcome[5] == generation_b
    claimed_owner, claimed_generation, created = outcome[6]
    assert claimed_owner.startswith("map-reduce-")
    assert claimed_generation == generation_b
    assert created is True
    assert outcome[7] == ((generation_b, 0),)
    assert outcome[8] == generation_b
