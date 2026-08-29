from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
import threading
from pathlib import Path
from typing import Literal

import pytest
from cardrag_core import canonical_json_bytes
from v5_fixtures import install_v5_fixture

from cardrag_mcp.experimental_map_reduce import (
    ExperimentalMapReduceError,
    ExperimentalMapReduceLane,
    ExperimentalMapReduceProfile,
    PreparedProviderCall,
    ProviderCompletion,
    ProviderEnvelopeReceipt,
    _identity,
    _provider_usage_receipt,
)
from cardrag_mcp.store import GenerationHandle, GenerationStore


def _profile() -> ExperimentalMapReduceProfile:
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


def _store(tmp_path: Path, *, retention: int = 3) -> GenerationStore:
    store = GenerationStore(
        tmp_path / "state",
        maximum_vector_bytes=2 * 1024 * 1024,
        retention=retention,
    )
    install_v5_fixture(store)
    return store


class _NoEvidenceReasoner:
    def __init__(self) -> None:
        self.call_count = 0

    async def complete(self, call: PreparedProviderCall) -> ProviderCompletion:
        self.call_count += 1
        content = canonical_json_bytes({"relevant": False, "spans": []})
        prompt_tokens = max(1, call.input_characters // 4)
        usage_payload = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": 4,
            "total_tokens": prompt_tokens + 4,
        }
        envelope_payload = canonical_json_bytes(
            {
                "content_sha256": hashlib.sha256(content).hexdigest(),
                "model": "openai/reasoning-test",
                "provider": "test-provider",
                "usage": usage_payload,
            }
        )
        return ProviderCompletion(
            content=content,
            envelope=ProviderEnvelopeReceipt(
                response_body_sha256=hashlib.sha256(envelope_payload).hexdigest(),
                response_body_size_bytes=len(envelope_payload),
                response_model="openai/reasoning-test",
                response_provider="test-provider",
                usage=_provider_usage_receipt(usage_payload),
            ),
        )

    async def close(self) -> None:
        return None


class _CrashAfterReservationReasoner:
    def __init__(self) -> None:
        self.call_count = 0

    async def complete(self, _call: PreparedProviderCall) -> ProviderCompletion:
        self.call_count += 1
        raise ExperimentalMapReduceError("simulated provider crash window")

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_unknown_job_polls_keep_exactly_1024_local_lock_stripes(
    tmp_path: Path,
) -> None:
    store = GenerationStore(
        tmp_path / "state",
        maximum_vector_bytes=2 * 1024 * 1024,
    )
    lane = ExperimentalMapReduceLane(store, _NoEvidenceReasoner(), _profile())

    for index in range(2_048):
        unknown_job_id = f"map-reduce-{index:064x}"
        with pytest.raises(ExperimentalMapReduceError):
            await lane.run(
                "unknown job must not allocate a per-ID lock",
                action="poll",
                job_id=unknown_job_id,
            )

    assert len(lane._locks) == 1_024
    assert len({id(lock) for lock in lane._locks}) == 1_024


@pytest.mark.asyncio
async def test_repeated_start_after_provider_crash_returns_pending_status_then_cancels(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    query = "ambiguous first provider call"
    crashing_provider = _CrashAfterReservationReasoner()

    first_pending = await ExperimentalMapReduceLane(store, crashing_provider, _profile()).run(query)
    assert first_pending.pending_provider_call is True
    assert crashing_provider.call_count == 1

    replacement_provider = _NoEvidenceReasoner()
    restarted_lane = ExperimentalMapReduceLane(store, replacement_provider, _profile())
    pending = await restarted_lane.run(query)

    assert pending.job_id.startswith("map-reduce-")
    assert pending.status == "mapping"
    assert pending.resumed is True
    assert pending.pending_provider_call is True
    assert pending.provider_call_count == 1
    assert pending.accounted_output_tokens == _profile().maximum_completion_tokens
    assert replacement_provider.call_count == 0

    cancelled = await restarted_lane.run(
        query,
        action="cancel",
        job_id=pending.job_id,
    )
    assert cancelled.job_id == pending.job_id
    assert cancelled.status == "cancelled"
    assert cancelled.pending_provider_call is True
    assert replacement_provider.call_count == 0
    assert not (store.generation_gc_roots / f"{pending.job_id}.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_shape", ("empty-directory", "orphan-progress-temp"))
async def test_initial_job_directory_crash_is_recovered(
    tmp_path: Path,
    crash_shape: str,
) -> None:
    store = _store(tmp_path / crash_shape)
    profile = _profile()
    query = f"recover {crash_shape} before initial progress publish"
    identity = _identity(
        "gen-v5-exact",
        hashlib.sha256(query.encode()).hexdigest(),
        profile.profile_id,
    )
    store.acquire_generation_gc_root(identity.job_id, identity.generation_id)
    job_directory = store.root / "experimental-map-reduce-jobs" / identity.job_id
    job_directory.mkdir(parents=True)
    orphan: Path | None = None
    if crash_shape == "orphan-progress-temp":
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".progress.json.",
            dir=job_directory,
        )
        orphan = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as output:
            output.write(b"partially written initial progress")
            output.flush()
            os.fsync(output.fileno())

    provider = _NoEvidenceReasoner()
    recovered = await ExperimentalMapReduceLane(store, provider, profile).run(
        query,
        action="poll",
        job_id=identity.job_id,
    )

    assert recovered.job_id == identity.job_id
    assert recovered.generation_id == identity.generation_id
    assert recovered.status == "mapping"
    assert recovered.provider_call_count == 1
    assert provider.call_count == 1
    assert (job_directory / "progress.json").is_file()
    if orphan is not None:
        assert not orphan.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ("poll", "cancel"))
async def test_root_only_old_generation_recovers_after_rollover(
    tmp_path: Path,
    action: Literal["poll", "cancel"],
) -> None:
    store = _store(tmp_path / action, retention=2)
    profile = _profile()
    query = f"root-only generation A recovery by {action}"
    identity = _identity(
        "gen-v5-exact",
        hashlib.sha256(query.encode()).hexdigest(),
        profile.profile_id,
    )
    store.acquire_generation_gc_root(identity.job_id, identity.generation_id)

    for generation_id in ("gen-v5-B", "gen-v5-C", "gen-v5-D"):
        install_v5_fixture(store, generation_id=generation_id)
    assert store.active_generation_id == "gen-v5-D"
    assert (store.generations / identity.generation_id).is_dir()

    restarted_store = GenerationStore(
        store.root,
        maximum_vector_bytes=2 * 1024 * 1024,
        retention=2,
    )
    assert restarted_store.load_current() is True
    provider = _NoEvidenceReasoner()
    recovered = await ExperimentalMapReduceLane(
        restarted_store,
        provider,
        profile,
    ).run(
        query,
        action=action,
        job_id=identity.job_id,
    )

    assert recovered.job_id == identity.job_id
    assert recovered.generation_id == identity.generation_id
    if action == "poll":
        assert recovered.status == "mapping"
        assert recovered.provider_call_count == 1
        assert provider.call_count == 1
        cancelled = await ExperimentalMapReduceLane(
            restarted_store,
            provider,
            profile,
        ).run(query, action="cancel", job_id=identity.job_id)
        assert cancelled.status == "cancelled"
        assert provider.call_count == 1
    else:
        assert recovered.status == "cancelled"
        assert recovered.provider_call_count == 0
        assert provider.call_count == 0
    assert not (restarted_store.generation_gc_roots / f"{identity.job_id}.json").exists()


@pytest.mark.asyncio
async def test_owned_immutable_hardlink_crash_temp_is_reconciled(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    query = "reconcile immutable publication hardlink"
    provider = _NoEvidenceReasoner()
    lane = ExperimentalMapReduceLane(store, provider, _profile())
    running = await lane.run(query)
    cancelled = await lane.run(query, action="cancel", job_id=running.job_id)
    directory = store.root / "experimental-map-reduce-jobs" / running.job_id
    target = directory / "CANCELLED.json"
    temporary = directory / ".CANCELLED.json.crashowned"
    os.link(target, temporary)
    assert target.stat().st_nlink == 2

    restarted = ExperimentalMapReduceLane(store, provider, _profile())
    recovered = await restarted.run(query, action="poll", job_id=running.job_id)

    assert recovered == cancelled
    assert not temporary.exists()
    assert target.stat().st_nlink == 1


def test_owned_generation_root_hardlink_crash_temp_is_reconciled(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    profile = _profile()
    query_sha256 = hashlib.sha256(b"generation root hardlink crash").hexdigest()
    identity = _identity("gen-v5-exact", query_sha256, profile.profile_id)
    store.acquire_generation_gc_root(identity.job_id, identity.generation_id)
    target = store.generation_gc_roots / f"{identity.job_id}.json"
    temporary = store.incoming / ".generation-gc-root.crashowned"
    os.link(target, temporary)
    assert target.stat().st_nlink == 2

    roots = store.generation_gc_roots_snapshot()

    assert roots == {identity.job_id: identity.generation_id}
    assert not temporary.exists()
    assert target.stat().st_nlink == 1


def test_owned_provider_policy_hardlink_crash_temp_is_reconciled(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    ExperimentalMapReduceLane(store, _NoEvidenceReasoner(), _profile())
    coordination = store.root / "experimental-map-reduce-coordination"
    target = coordination / "policy.json"
    temporary = coordination / ".policy.json.crashowned"
    os.link(target, temporary)
    assert target.stat().st_nlink == 2

    ExperimentalMapReduceLane(store, _NoEvidenceReasoner(), _profile())

    assert not temporary.exists()
    assert target.stat().st_nlink == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_crash_root", (False, True))
async def test_handled_preledger_failure_releases_exact_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing_crash_root: bool,
) -> None:
    store = _store(tmp_path)
    profile = _profile()
    query = f"handled over-budget claim existing={existing_crash_root}"
    identity = _identity(
        "gen-v5-exact",
        hashlib.sha256(query.encode()).hexdigest(),
        profile.profile_id,
    )
    if existing_crash_root:
        store.acquire_generation_gc_root(identity.job_id, identity.generation_id)
    provider = _NoEvidenceReasoner()
    lane = ExperimentalMapReduceLane(store, provider, profile)

    def reject_budget(*_args: object, **_kwargs: object) -> None:
        raise ExperimentalMapReduceError("deterministic pre-ledger budget rejection")

    monkeypatch.setattr(lane, "_verify_minimum_job_budget", reject_budget)
    with pytest.raises(ExperimentalMapReduceError, match="pre-ledger"):
        await lane.run(query)

    assert provider.call_count == 0
    assert store.generation_gc_roots_snapshot() == {}
    jobs = store.root / "experimental-map-reduce-jobs"
    assert not jobs.exists() or not tuple(jobs.iterdir())


def test_stale_loaded_store_claims_durable_current_not_deleted_active(
    tmp_path: Path,
) -> None:
    stale = _store(tmp_path, retention=2)
    assert stale.active_generation_id == "gen-v5-exact"
    fresh = GenerationStore(
        stale.root,
        maximum_vector_bytes=2 * 1024 * 1024,
        retention=2,
    )
    assert fresh.load_current() is True
    install_v5_fixture(fresh, generation_id="gen-v5-B")
    install_v5_fixture(fresh, generation_id="gen-v5-C")
    assert not (stale.generations / "gen-v5-exact").exists()

    query_sha256 = hashlib.sha256(b"stale store durable pointer claim").hexdigest()
    profile = _profile()

    def owner_for_generation(generation_id: str) -> str:
        return _identity(generation_id, query_sha256, profile.profile_id).job_id

    owner_id, generation_id, created = stale.claim_current_generation_gc_root(owner_for_generation)

    assert created is True
    assert generation_id == "gen-v5-C"
    assert owner_id == owner_for_generation("gen-v5-C")
    stale.release_generation_gc_root(owner_id, generation_id)


def test_root_only_claims_consume_shared_job_cap(tmp_path: Path) -> None:
    store = GenerationStore(
        tmp_path / "state",
        maximum_vector_bytes=2 * 1024 * 1024,
        exhaustive_audit_max_jobs=1,
    )
    install_v5_fixture(store)
    profile = _profile()

    def owner(query: bytes):
        query_sha256 = hashlib.sha256(query).hexdigest()
        return lambda generation_id: (
            _identity(
                generation_id,
                query_sha256,
                profile.profile_id,
            ).job_id
        )

    first_owner, first_generation, _ = store.claim_current_generation_gc_root(owner(b"first"))
    with pytest.raises(RuntimeError, match="job quota"):
        store.claim_current_generation_gc_root(owner(b"second"))

    assert store.generation_gc_roots_snapshot() == {first_owner: first_generation}
    store.release_generation_gc_root(first_owner, first_generation)
    second_owner, second_generation, created = store.claim_current_generation_gc_root(
        owner(b"second")
    )
    assert created is True
    store.release_generation_gc_root(second_owner, second_generation)


def test_generation_root_hardlink_peak_consumes_shared_total_cap(tmp_path: Path) -> None:
    calibration = _store(tmp_path / "calibration")
    profile = _profile()
    query_sha256 = hashlib.sha256(b"generation root peak").hexdigest()
    identity = _identity("gen-v5-exact", query_sha256, profile.profile_id)
    payload = {
        "schema_version": "cardrag.mcp-generation-gc-root.v1",
        "owner_id": identity.job_id,
        "generation_id": identity.generation_id,
    }
    root_bytes = len(calibration._canonical_generation_gc_root(payload))
    shared_total = 2 * root_bytes - 1
    store = GenerationStore(
        tmp_path / "target",
        maximum_vector_bytes=2 * 1024 * 1024,
        exhaustive_audit_max_jobs=2,
        exhaustive_audit_max_total_bytes=shared_total,
        exhaustive_audit_max_artifact_bytes=min(64, shared_total),
    )
    install_v5_fixture(store)

    def owner_for_generation(generation_id: str) -> str:
        return _identity(generation_id, query_sha256, profile.profile_id).job_id

    with pytest.raises(RuntimeError, match="total quota"):
        store.claim_current_generation_gc_root(owner_for_generation)

    assert store.generation_gc_roots_snapshot() == {}
    assert not tuple(store.incoming.glob(".generation-gc-root.*"))


@pytest.mark.asyncio
async def test_claim_fsync_precedes_rollover_and_same_query_resumes_one_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_a = _store(tmp_path, retention=2)
    store_b = GenerationStore(
        store_a.root,
        maximum_vector_bytes=2 * 1024 * 1024,
        retention=2,
    )
    assert store_b.load_current() is True
    profile = _profile()
    query = "same sealed query across A to C rollover"
    claim_held = threading.Event()
    allow_claim = threading.Event()
    activation_attempted = threading.Event()
    original_claim = store_a._acquire_generation_gc_root_locked

    def paused_claim(*args: object, **kwargs: object) -> None:
        claim_held.set()
        if not allow_claim.wait(10):
            raise TimeoutError("claim release timed out")
        original_claim(*args, **kwargs)

    monkeypatch.setattr(store_a, "_acquire_generation_gc_root_locked", paused_claim)
    first_provider = _CrashAfterReservationReasoner()
    first_lane = ExperimentalMapReduceLane(store_a, first_provider, profile)
    first_task = asyncio.create_task(first_lane.run(query))
    assert await asyncio.to_thread(claim_held.wait, 10)

    original_activate = store_b.activate

    def signaling_activate(handle: GenerationHandle) -> None:
        activation_attempted.set()
        original_activate(handle)

    monkeypatch.setattr(store_b, "activate", signaling_activate)

    def roll_generations() -> None:
        install_v5_fixture(store_b, generation_id="gen-v5-B")
        install_v5_fixture(store_b, generation_id="gen-v5-C")

    rollover = asyncio.create_task(asyncio.to_thread(roll_generations))
    assert await asyncio.to_thread(activation_attempted.wait, 10)
    await asyncio.sleep(0.05)
    assert not rollover.done()
    allow_claim.set()
    await rollover

    second_provider = _NoEvidenceReasoner()
    second_lane = ExperimentalMapReduceLane(store_b, second_provider, profile)
    first = await first_task
    second = await second_lane.run(query)

    assert first.job_id == second.job_id
    assert first.generation_id == second.generation_id == "gen-v5-exact"
    assert first.pending_provider_call is second.pending_provider_call is True
    assert first_provider.call_count == 1
    assert second_provider.call_count == 0
    assert len(store_a.generation_gc_roots_snapshot()) == 1
    jobs = tuple((store_a.root / "experimental-map-reduce-jobs").iterdir())
    assert len(jobs) == 1
    assert (store_a.generations / "gen-v5-exact").is_dir()
