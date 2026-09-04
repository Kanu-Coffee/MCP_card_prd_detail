from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import shutil
from pathlib import Path

import httpx
import numpy as np
import pytest
from cardrag_core import canonical_json_bytes, canonical_sha256
from conftest import FakeEmbedder, install_generation
from fastapi import FastAPI
from v5_fixtures import install_v5_fixture

import cardrag_mcp.experimental_map_reduce as map_reduce_module
import cardrag_mcp.main as main_module
from cardrag_mcp.app import build_mcp_server
from cardrag_mcp.config import Settings
from cardrag_mcp.experimental_map_reduce import (
    CORPUS_PLAN_SCHEMA,
    ExactEvidenceSpan,
    ExpectedContractMap,
    ExperimentalMapReduceError,
    ExperimentalMapReduceLane,
    ExperimentalMapReduceProfile,
    MapReduceIdentity,
    MapReduceLedger,
    MapSourceRef,
    MapUnitPlan,
    MapUnitResult,
    OpenRouterExperimentalReasoner,
    PreparedProviderCall,
    ProviderCompletion,
    ProviderEnvelopeReceipt,
    ReduceBatchResult,
    _identity,
    _pack_section_units,
    _parse_provider_decision,
    _PendingReduction,
    _provider_usage_receipt,
    _reduce_input_sha256,
    _reduction_state,
    _RuntimeUnit,
    _span_fits_hierarchical_reduce,
    _span_key,
    _TerminalReduction,
)
from cardrag_mcp.repository import ServingRepository
from cardrag_mcp.store import GenerationHandle, GenerationStore, load_generation_handle


def _profile(
    *,
    maximum_input_characters: int = 16_384,
    maximum_completion_tokens: int = 4_096,
    maximum_job_provider_calls: int = 4_096,
    maximum_job_input_characters: int = 268_435_456,
    maximum_job_output_tokens: int = 16_777_216,
) -> ExperimentalMapReduceProfile:
    return ExperimentalMapReduceProfile.seal(
        model="openai/reasoning-test",
        provider_id="test-provider",
        evaluation_artifact_sha256="a" * 64,
        maximum_input_characters=maximum_input_characters,
        maximum_completion_tokens=maximum_completion_tokens,
        maximum_response_bytes=1024 * 1024,
        maximum_job_provider_calls=maximum_job_provider_calls,
        maximum_job_input_characters=maximum_job_input_characters,
        maximum_job_output_tokens=maximum_job_output_tokens,
    )


def _provider_span(span: ExactEvidenceSpan) -> dict[str, object]:
    return {
        "contract_revision_id": span.contract_revision_id,
        "page": span.page,
        "source_start": span.source_start,
        "source_end": span.source_end,
        "quote": span.text,
    }


def _http_test_unit() -> _RuntimeUnit:
    text = "exact OCR"
    ref = MapSourceRef(
        page=1,
        source_start=0,
        source_end=len(text),
        text_sha256=hashlib.sha256(text.encode()).hexdigest(),
    )
    return _RuntimeUnit(
        plan=MapUnitPlan(
            unit_id="map-unit-http",
            contract_revision_id="contract-http",
            ordinal=0,
            scope="contract",
            source_refs=(ref,),
            source_character_count=len(text),
            input_sha256="f" * 64,
        ),
        sources=((ref, text),),
    )


class _FakeReasoner:
    def __init__(self, *, include_rewritten_span: bool = False) -> None:
        self.include_rewritten_span = include_rewritten_span
        self.map_calls: list[str] = []
        self.reduce_calls: list[tuple[int, int]] = []
        self.closed = False

    @staticmethod
    def _completion(raw: bytes, call: PreparedProviderCall) -> ProviderCompletion:
        usage_payload = {
            "prompt_tokens": max(1, call.input_characters // 4),
            "completion_tokens": 4,
            "total_tokens": max(1, call.input_characters // 4) + 4,
        }
        envelope_payload = canonical_json_bytes(
            {
                "model": "openai/reasoning-test",
                "provider": "test-provider",
                "usage": usage_payload,
                "content_sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
        return ProviderCompletion(
            content=raw,
            envelope=ProviderEnvelopeReceipt(
                response_body_sha256=hashlib.sha256(envelope_payload).hexdigest(),
                response_body_size_bytes=len(envelope_payload),
                response_model="openai/reasoning-test",
                response_provider="test-provider",
                usage=_provider_usage_receipt(usage_payload),
            ),
        )

    async def complete(self, call: PreparedProviderCall) -> ProviderCompletion:
        if call.phase == "reduce":
            maximum_output_spans = int(
                call.user_prompt.split("MAXIMUM_OUTPUT_SPANS: ", 1)[1].splitlines()[0]
            )
            spans = json.loads(call.user_prompt.split("VALIDATED_MAP_SPANS:\n", 1)[1])
            self.reduce_calls.append((len(spans), maximum_output_spans))
            selected = spans[:maximum_output_spans]
            return self._completion(
                canonical_json_bytes(
                    {
                        "relevant": bool(selected),
                        "spans": [
                            {
                                "contract_revision_id": span["contract_revision_id"],
                                "page": span["page"],
                                "source_start": span["source_start"],
                                "source_end": span["source_end"],
                                "quote": span["text"],
                            }
                            for span in selected
                        ],
                    }
                ),
                call,
            )
        unit_id = call.user_prompt.split("MAP_UNIT_ID: ", 1)[1].splitlines()[0]
        contract_revision_id = call.user_prompt.split("CONTRACT_REVISION_ID: ", 1)[1].splitlines()[
            0
        ]
        self.map_calls.append(unit_id)
        match = re.search(
            r"<<<PAGE=(\d+) START=(\d+) END=(\d+)>>>\n(.*?)\n<<<END_RANGE>>>",
            call.user_prompt,
            flags=re.DOTALL,
        )
        if match is None:  # pragma: no cover - sealed prompt invariant
            raise AssertionError("map prompt lacks a source range")
        page, source_start = int(match.group(1)), int(match.group(2))
        text = match.group(4)
        relative_start = next(
            index for index, character in enumerate(text) if not character.isspace()
        )
        quote = text[relative_start : relative_start + min(4, len(text) - relative_start)]
        start = source_start + relative_start
        spans: list[dict[str, object]] = []
        if self.include_rewritten_span and len(self.map_calls) == 1:
            spans.append(
                {
                    "contract_revision_id": contract_revision_id,
                    "page": page,
                    "source_start": start,
                    "source_end": start + len(quote),
                    "quote": "X" * len(quote),
                }
            )
        spans.append(
            {
                "contract_revision_id": contract_revision_id,
                "page": page,
                "source_start": start,
                "source_end": start + len(quote),
                "quote": quote,
            }
        )
        return self._completion(
            canonical_json_bytes({"relevant": True, "spans": spans}),
            call,
        )

    async def close(self) -> None:
        self.closed = True

    @property
    def call_count(self) -> int:
        return len(self.map_calls) + len(self.reduce_calls)


class _FailAfterReservationReasoner:
    def __init__(self) -> None:
        self.call_count = 0

    async def complete(self, _call: PreparedProviderCall) -> ProviderCompletion:
        self.call_count += 1
        raise ExperimentalMapReduceError("simulated provider crash window")

    async def close(self) -> None:
        return None


class _InvalidDecisionReasoner(_FakeReasoner):
    async def complete(self, call: PreparedProviderCall) -> ProviderCompletion:
        self.map_calls.append("invalid-decision")
        return self._completion(b'{"relevant":NaN,"spans":[]}', call)


class _ConcurrencyReasoner(_FakeReasoner):
    def __init__(self) -> None:
        super().__init__()
        self.active_calls = 0
        self.maximum_active_calls = 0

    async def complete(self, call: PreparedProviderCall) -> ProviderCompletion:
        self.active_calls += 1
        self.maximum_active_calls = max(self.maximum_active_calls, self.active_calls)
        try:
            await asyncio.sleep(0.03)
            return await super().complete(call)
        finally:
            self.active_calls -= 1


def _store(tmp_path: Path, *, retention: int = 3) -> GenerationStore:
    store = GenerationStore(
        tmp_path / "state",
        maximum_vector_bytes=2 * 1024 * 1024,
        retention=retention,
    )
    install_v5_fixture(store)
    return store


def _install_inactive_candidate(
    store: GenerationStore,
    generation_id: str,
) -> GenerationHandle:
    install_generation(store, generation_id, activate=False)
    return load_generation_handle(
        store.generations / generation_id,
        store.objects,
        maximum_vector_bytes=store.maximum_vector_bytes,
        maximum_vector_sidecar_bytes=store.maximum_vector_sidecar_bytes,
        maximum_resident_vector_bytes=store.maximum_resident_vector_bytes,
        expected_generation_id=generation_id,
    )


@pytest.mark.asyncio
async def test_lane_maps_every_contract_before_reduce_resumes_and_publishes_exact_only(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    first_provider = _FakeReasoner(include_rewritten_span=True)
    lane = ExperimentalMapReduceLane(store, first_provider, _profile())
    query = "전체 카드 혜택을 비교해 줘"

    first = await lane.run(query)
    assert first.status == "mapping"
    assert first.mapped_units == first.mapped_contracts == 1
    assert first.total_units == first.total_contracts == 2
    assert first.evidence_spans == ()
    assert first.primary_exact_influenced is False
    assert first.rejected_span_count == 1
    assert first_provider.reduce_calls == []

    resumed_provider = _FakeReasoner()
    resumed_lane = ExperimentalMapReduceLane(store, resumed_provider, _profile())
    mapped = await resumed_lane.run(query, action="poll", job_id=first.job_id)
    assert mapped.status == "reducing"
    assert mapped.mapped_units == mapped.total_units == 2
    assert mapped.mapped_contracts == mapped.total_contracts == 2
    assert mapped.resumed is True
    assert resumed_provider.reduce_calls == []

    complete = await resumed_lane.run(query, action="poll", job_id=first.job_id)
    assert complete.status == "complete"
    assert complete.artifact_sha256 is not None
    assert complete.evidence_spans
    assert resumed_provider.reduce_calls == [(2, 64)]
    assert all(span.text != "XXXX" for span in complete.evidence_spans)

    job = store.root / "experimental-map-reduce-jobs" / complete.job_id
    artifact = job / f"artifact-{complete.artifact_sha256}.json"
    assert artifact.is_file()
    assert artifact.stat().st_mode & 0o222 == 0
    assert query.encode() not in artifact.read_bytes()
    calls = (len(resumed_provider.map_calls), len(resumed_provider.reduce_calls))
    reused = await resumed_lane.run(query, action="poll", job_id=first.job_id)
    assert reused.artifact_sha256 == complete.artifact_sha256
    assert (len(resumed_provider.map_calls), len(resumed_provider.reduce_calls)) == calls


@pytest.mark.asyncio
async def test_job_pins_original_inactive_generation_across_restart_and_retention(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, retention=2)
    query = "generation A exhaustive audit"
    first_provider = _FakeReasoner()
    first = await ExperimentalMapReduceLane(store, first_provider, _profile()).run(query)
    assert first.generation_id == "gen-v5-exact"
    assert first_provider.call_count == 1

    for generation_id in ("gen-v5-B", "gen-v5-C", "gen-v5-D"):
        install_v5_fixture(store, generation_id=generation_id)
    assert store.active_generation_id == "gen-v5-D"
    assert (store.generations / "gen-v5-exact").is_dir()

    restarted_store = GenerationStore(
        store.root,
        maximum_vector_bytes=2 * 1024 * 1024,
        retention=2,
    )
    assert restarted_store.load_current() is True
    resumed_provider = _FakeReasoner()
    restarted_lane = ExperimentalMapReduceLane(
        restarted_store,
        resumed_provider,
        _profile(),
    )
    mapped = await restarted_lane.run(query, action="poll", job_id=first.job_id)
    assert mapped.generation_id == "gen-v5-exact"
    assert mapped.status == "reducing"
    assert resumed_provider.call_count == 1
    complete = await restarted_lane.run(query, action="poll", job_id=first.job_id)
    assert complete.status == "complete"
    assert resumed_provider.call_count == 2
    assert not (restarted_store.generation_gc_roots / f"{first.job_id}.json").exists()
    assert not (restarted_store.generations / "gen-v5-exact").exists()

    calls = resumed_provider.call_count
    terminal = await restarted_lane.run(query, action="poll", job_id=first.job_id)
    assert terminal.artifact_sha256 == complete.artifact_sha256
    assert terminal.evidence_spans == complete.evidence_spans
    assert resumed_provider.call_count == calls


@pytest.mark.asyncio
async def test_cancel_uses_original_generation_after_new_activation(tmp_path: Path) -> None:
    store = _store(tmp_path, retention=2)
    query = "cancel generation A"
    provider = _FakeReasoner()
    lane = ExperimentalMapReduceLane(store, provider, _profile())
    first = await lane.run(query)
    install_v5_fixture(store, generation_id="gen-v5-B")
    calls = provider.call_count
    cancelled = await lane.run(query, action="cancel", job_id=first.job_id)
    assert cancelled.status == "cancelled"
    assert cancelled.generation_id == "gen-v5-exact"
    assert provider.call_count == calls
    assert not (store.generation_gc_roots / f"{first.job_id}.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ("malformed", "symlink", "unreadable"))
async def test_corrupt_generation_root_aborts_activation_before_pointer_mutation(
    tmp_path: Path,
    damage: str,
) -> None:
    store = _store(tmp_path / damage, retention=2)
    running = await ExperimentalMapReduceLane(store, _FakeReasoner(), _profile()).run(
        "retain generation before activation"
    )
    marker = store.generation_gc_roots / f"{running.job_id}.json"
    candidate = _install_inactive_candidate(store, f"candidate-{damage}")
    pointer_before = store.current_path.read_bytes()
    if damage == "malformed":
        os.chmod(marker, 0o600)
        marker.write_bytes(b"{}\n")
    elif damage == "symlink":
        marker.unlink()
        target = tmp_path / f"{damage}-target.json"
        target.write_bytes(b"{}\n")
        marker.symlink_to(target)
    else:
        os.chmod(marker, 0)
    with pytest.raises(RuntimeError, match="generation GC root"):
        store.activate(candidate)
    assert store.current_path.read_bytes() == pointer_before
    assert store.active_generation_id == "gen-v5-exact"


@pytest.mark.asyncio
async def test_missing_bound_nonterminal_generation_fails_without_provider_call(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, retention=2)
    query = "missing generation must fail closed"
    first = await ExperimentalMapReduceLane(store, _FakeReasoner(), _profile()).run(query)
    install_v5_fixture(store, generation_id="gen-v5-B")
    shutil.rmtree(store.generations / "gen-v5-exact")
    provider = _FakeReasoner()
    lane = ExperimentalMapReduceLane(store, provider, _profile())
    with pytest.raises(ExperimentalMapReduceError, match="generation is missing"):
        await lane.run(query, action="poll", job_id=first.job_id)
    assert provider.call_count == 0


@pytest.mark.asyncio
async def test_cancel_is_terminal_and_marker_tamper_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    provider = _FakeReasoner()
    lane = ExperimentalMapReduceLane(store, provider, _profile())
    query = "취소할 전수 감사"

    running = await lane.run(query)
    cancelled = await lane.run(query, action="cancel", job_id=running.job_id)
    assert cancelled.status == "cancelled"
    assert cancelled.job_id == running.job_id
    calls = len(provider.map_calls)
    assert (await lane.run(query, action="poll", job_id=running.job_id)).status == "cancelled"
    assert len(provider.map_calls) == calls

    marker = store.root / "experimental-map-reduce-jobs" / running.job_id / "CANCELLED.json"
    os.chmod(marker, 0o600)
    payload = json.loads(marker.read_bytes())
    payload["artifact_sha256"] = "0" * 64
    marker.write_bytes(canonical_json_bytes(payload))
    with pytest.raises(ExperimentalMapReduceError, match="cancel"):
        await lane.run(query, action="poll", job_id=running.job_id)


@pytest.mark.asyncio
async def test_terminal_marker_symlink_fails_closed_without_provider_call(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    query = "terminal marker symlink"
    provider = _FakeReasoner()
    lane = ExperimentalMapReduceLane(store, provider, _profile())
    running = await lane.run(query)
    cancelled = await lane.run(query, action="cancel", job_id=running.job_id)
    marker = store.root / "experimental-map-reduce-jobs" / running.job_id / "CANCELLED.json"
    target = tmp_path / "copied-cancel-marker.json"
    target.write_bytes(marker.read_bytes())
    marker.unlink()
    marker.symlink_to(target)
    calls = provider.call_count
    with pytest.raises(ExperimentalMapReduceError, match="marker is missing or unsafe"):
        await lane.run(query, action="poll", job_id=running.job_id)
    assert cancelled.status == "cancelled"
    assert provider.call_count == calls


@pytest.mark.asyncio
async def test_pending_without_receipt_is_permanently_charged_and_never_recalled(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    query = "ambiguous provider crash"
    crashing = _FailAfterReservationReasoner()
    lane = ExperimentalMapReduceLane(store, crashing, _profile())
    first_pending = await lane.run(query)
    assert first_pending.pending_provider_call is True
    assert crashing.call_count == 1
    jobs = tuple((store.root / "experimental-map-reduce-jobs").iterdir())
    assert len(jobs) == 1
    job_id = jobs[0].name
    assert first_pending.job_id == job_id

    install_v5_fixture(store, generation_id="gen-v5-after-provider-crash")
    assert store.active_generation_id == "gen-v5-after-provider-crash"

    restarted_provider = _FakeReasoner()
    restarted = ExperimentalMapReduceLane(store, restarted_provider, _profile())
    pending = await restarted.run(query)
    assert pending.job_id == job_id
    assert pending.pending_provider_call is True
    assert pending.resumed is True
    assert restarted_provider.call_count == 0
    inspected = restarted.job_store.inspect(job_id)
    assert inspected is not None
    assert inspected.ledger.pending_call is not None
    assert inspected.ledger.provider_call_count == 1
    assert inspected.ledger.accounted_output_tokens == _profile().maximum_completion_tokens

    cancelled = await restarted.run(query, action="cancel", job_id=job_id)
    assert cancelled.status == "cancelled"
    assert cancelled.pending_provider_call is True
    assert cancelled.accounted_output_tokens == _profile().maximum_completion_tokens
    assert restarted_provider.call_count == 0


@pytest.mark.asyncio
async def test_invalid_provider_decision_is_intentionally_ambiguous_and_cancel_only(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    query = "invalid decision boundary"
    provider = _InvalidDecisionReasoner()
    lane = ExperimentalMapReduceLane(store, provider, _profile())
    first_pending = await lane.run(query)
    assert first_pending.pending_provider_call is True
    job_directory = next((store.root / "experimental-map-reduce-jobs").iterdir())
    assert not tuple(job_directory.glob("receipt-*.json"))
    loaded = lane.job_store.inspect(job_directory.name)
    assert loaded is not None
    assert loaded.ledger.pending_call is not None
    assert loaded.ledger.provider_call_count == 1
    assert loaded.ledger.accounted_output_tokens == _profile().maximum_completion_tokens

    calls = provider.call_count
    pending = await lane.run(query, action="poll", job_id=job_directory.name)
    assert pending.job_id == job_directory.name
    assert pending.pending_provider_call is True
    assert provider.call_count == calls
    cancelled = await lane.run(query, action="cancel", job_id=job_directory.name)
    assert cancelled.status == "cancelled"
    assert cancelled.pending_provider_call is True


@pytest.mark.asyncio
async def test_restart_recovers_generation_root_created_before_job_ledger(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    profile = _profile()
    query = "crash between generation root and ledger"
    identity = _identity(
        "gen-v5-exact",
        hashlib.sha256(query.encode()).hexdigest(),
        profile.profile_id,
    )
    store.acquire_generation_gc_root(identity.job_id, identity.generation_id)
    job_root = store.root / "experimental-map-reduce-jobs"
    assert not job_root.exists()

    provider = _FakeReasoner()
    started = await ExperimentalMapReduceLane(store, provider, profile).run(query)
    assert started.job_id == identity.job_id
    assert provider.call_count == 1
    assert (job_root / identity.job_id / "progress.json").is_file()
    store.verify_generation_gc_root(identity.job_id, identity.generation_id)


@pytest.mark.asyncio
async def test_receipt_durable_before_ledger_recovers_with_zero_provider_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    query = "receipt before progress checkpoint"
    first_provider = _FakeReasoner()
    lane = ExperimentalMapReduceLane(store, first_provider, _profile())

    def crash_checkpoint(*_args: object, **_kwargs: object) -> MapReduceLedger:
        raise RuntimeError("simulated receipt-ledger crash window")

    monkeypatch.setattr(lane.job_store, "checkpoint_map", crash_checkpoint)
    pending = await lane.run(query)
    assert pending.status == "mapping"
    assert pending.pending_provider_call is True
    assert first_provider.call_count == 1
    job_directory = next((store.root / "experimental-map-reduce-jobs").iterdir())
    assert (job_directory / "receipt-00000000.json").is_file()

    resumed_provider = _FakeReasoner()
    resumed_lane = ExperimentalMapReduceLane(store, resumed_provider, _profile())
    recovered = await resumed_lane.run(
        query,
        action="poll",
        job_id=job_directory.name,
    )
    assert recovered.mapped_units == 1
    assert recovered.provider_call_count == 1
    assert recovered.pending_provider_call is False
    assert recovered.accounted_output_tokens == _profile().maximum_completion_tokens
    assert resumed_provider.call_count == 0


@pytest.mark.asyncio
async def test_query_and_profile_mismatch_reject_before_provider_or_generation_pin(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    query = "identity-bound query"
    first = await ExperimentalMapReduceLane(store, _FakeReasoner(), _profile()).run(query)
    provider = _FakeReasoner()
    lane = ExperimentalMapReduceLane(store, provider, _profile())
    with pytest.raises(ExperimentalMapReduceError, match="mismatch"):
        await lane.run("different query", action="poll", job_id=first.job_id)
    different_profile = _profile(maximum_job_provider_calls=4_095)
    with pytest.raises(ExperimentalMapReduceError, match="mismatch"):
        await ExperimentalMapReduceLane(store, provider, different_profile).run(
            query,
            action="poll",
            job_id=first.job_id,
        )
    with pytest.raises(ExperimentalMapReduceError, match="requires"):
        await lane.run(query, action="poll")
    with pytest.raises(ExperimentalMapReduceError, match="must not include"):
        await lane.run(query, action="start", job_id=first.job_id)
    assert provider.call_count == 0


@pytest.mark.asyncio
async def test_job_budget_exhaustion_precedes_client_and_survives_restart(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    profile = _profile(
        maximum_job_provider_calls=2,
        maximum_job_output_tokens=2 * 4_096,
    )
    query = "bounded map and reduce budget"
    provider = _FakeReasoner()
    lane = ExperimentalMapReduceLane(store, provider, profile)
    first = await lane.run(query)
    mapped = await lane.run(query, action="poll", job_id=first.job_id)
    assert mapped.status == "reducing"
    assert mapped.provider_call_count == 2
    assert mapped.accounted_output_tokens == 2 * profile.maximum_completion_tokens
    assert mapped.advertised_completion_tokens == 8
    calls = provider.call_count
    with pytest.raises(ExperimentalMapReduceError, match="budget exhausted before client"):
        await lane.run(query, action="poll", job_id=first.job_id)
    assert provider.call_count == calls == 2

    restarted_provider = _FakeReasoner()
    restarted = ExperimentalMapReduceLane(store, restarted_provider, profile)
    with pytest.raises(ExperimentalMapReduceError, match="budget exhausted before client"):
        await restarted.run(query, action="poll", job_id=first.job_id)
    assert restarted_provider.call_count == 0
    inspected = restarted.job_store.inspect(first.job_id)
    assert inspected is not None
    assert inspected.ledger.provider_call_count == 2
    assert inspected.ledger.accounted_output_tokens == 8_192


@pytest.mark.asyncio
async def test_provider_concurrency_is_global_across_lane_instances_and_policy_restart(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    provider = _ConcurrencyReasoner()
    first_lane = ExperimentalMapReduceLane(
        store,
        provider,
        _profile(),
        maximum_concurrent_provider_calls=1,
    )
    second_lane = ExperimentalMapReduceLane(
        store,
        provider,
        _profile(),
        maximum_concurrent_provider_calls=1,
    )
    first, second = await asyncio.gather(
        first_lane.run("global concurrency query one"),
        second_lane.run("global concurrency query two"),
    )
    assert first.job_id != second.job_id
    assert provider.call_count == 2
    assert provider.maximum_active_calls == 1
    coordination = store.root / "experimental-map-reduce-coordination"
    slots = tuple(coordination.glob("*.lock"))
    assert slots
    assert len(slots) <= 1_024 + 32
    assert all(
        path.is_file() and not path.is_symlink() and path.stat().st_size == 0 for path in slots
    )

    with pytest.raises(ExperimentalMapReduceError, match="durable global policy"):
        ExperimentalMapReduceLane(
            store,
            _FakeReasoner(),
            _profile(),
            maximum_concurrent_provider_calls=2,
        )


@pytest.mark.asyncio
async def test_resealed_progress_cannot_rewrite_ocr_and_job_quota_preserves_existing(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    lane = ExperimentalMapReduceLane(
        store,
        _FakeReasoner(),
        _profile(),
        maximum_jobs=1,
        maximum_total_bytes=10 * 1024 * 1024,
        maximum_artifact_bytes=1024 * 1024,
    )
    first = await lane.run("tamper target")
    job_root = store.root / "experimental-map-reduce-jobs"
    progress = job_root / first.job_id / "progress.json"
    payload = json.loads(progress.read_bytes())
    span = payload["ledger"]["completed_maps"][0]["spans"][0]
    replacement = "Z" * len(span["text"])
    span["text"] = replacement
    span["text_sha256"] = hashlib.sha256(replacement.encode()).hexdigest()
    payload["ledger_sha256"] = canonical_sha256(payload["ledger"])
    progress.write_bytes(canonical_json_bytes(payload))

    with pytest.raises(ExperimentalMapReduceError, match="provider receipt"):
        await lane.run("tamper target", action="poll", job_id=first.job_id)
    with pytest.raises(ExperimentalMapReduceError, match="job quota"):
        await lane.run("second immutable identity")
    assert len(tuple(job_root.iterdir())) == 1


def _reseal_first_receipt_chain(job_directory: Path, receipt: dict[str, object]) -> None:
    receipt_path = job_directory / "receipt-00000000.json"
    receipt_bytes = canonical_json_bytes(receipt)
    os.chmod(receipt_path, 0o600)
    receipt_path.write_bytes(receipt_bytes)
    receipt_sha256 = hashlib.sha256(receipt_bytes).hexdigest()
    progress_path = job_directory / "progress.json"
    progress = json.loads(progress_path.read_bytes())
    ledger = progress["ledger"]
    ledger["receipt_sha256s"][0] = receipt_sha256
    ledger["completed_maps"][0]["receipt_sha256"] = receipt_sha256
    progress["ledger_sha256"] = canonical_sha256(ledger)
    progress_path.write_bytes(canonical_json_bytes(progress))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tamper",
    ("chain", "request", "model", "usage", "decision", "raw-duplicate"),
)
async def test_receipt_chain_and_semantic_tamper_fail_before_provider_call(
    tmp_path: Path,
    tamper: str,
) -> None:
    store = _store(tmp_path / tamper)
    query = "tamper every receipt boundary"
    provider = _FakeReasoner()
    lane = ExperimentalMapReduceLane(store, provider, _profile())
    first = await lane.run(query)
    job_directory = store.root / "experimental-map-reduce-jobs" / first.job_id
    receipt_path = job_directory / "receipt-00000000.json"
    receipt = json.loads(receipt_path.read_bytes())
    raw = base64.b64decode(receipt["raw_content_base64"], validate=True)
    assert hashlib.sha256(raw).hexdigest() == receipt["raw_content_sha256"]
    assert json.loads(raw)["relevant"] is True

    if tamper == "chain":
        os.chmod(receipt_path, 0o600)
        receipt["envelope"]["response_body_sha256"] = "0" * 64
        receipt_path.write_bytes(canonical_json_bytes(receipt))
    else:
        if tamper == "request":
            receipt["request_sha256"] = "0" * 64
        elif tamper == "model":
            receipt["model"] = "other/model"
        elif tamper == "usage":
            receipt["envelope"]["usage"]["prompt_tokens"] += 1
        elif tamper == "decision":
            receipt["decision"]["relevant"] = False
        else:
            duplicate = b'{"relevant":true,"relevant":false,"spans":[]}'
            receipt["raw_content_base64"] = base64.b64encode(duplicate).decode("ascii")
            receipt["raw_content_sha256"] = hashlib.sha256(duplicate).hexdigest()
        _reseal_first_receipt_chain(job_directory, receipt)

    calls = provider.call_count
    with pytest.raises(ExperimentalMapReduceError, match="receipt|request"):
        await lane.run(query, action="poll", job_id=first.job_id)
    assert provider.call_count == calls


def test_provider_json_rejects_duplicate_keys_and_nonstandard_constants() -> None:
    with pytest.raises(ExperimentalMapReduceError, match="invalid strict JSON"):
        _parse_provider_decision(b'{"relevant":true,"relevant":false,"spans":[]}')
    with pytest.raises(ExperimentalMapReduceError, match="invalid strict JSON"):
        _parse_provider_decision(b'{"relevant":NaN,"spans":[]}')
    with pytest.raises(ExperimentalMapReduceError, match="invalid strict JSON"):
        _parse_provider_decision(
            b'{"relevant":true,"spans":[{"contract_revision_id":"c",'
            b'"page":1,"page":2,"source_start":0,"source_end":1,"quote":"x"}]}'
        )


def test_oversized_major_section_packs_only_on_whole_canonical_leaf_boundaries() -> None:
    profile = _profile()
    sources: list[tuple[MapSourceRef, str]] = []
    offset = 0
    for index in range(8):
        text = chr(65 + index) * 3_000
        sources.append(
            (
                MapSourceRef(
                    page=1,
                    source_start=offset,
                    source_end=offset + len(text),
                    text_sha256=hashlib.sha256(text.encode()).hexdigest(),
                ),
                text,
            )
        )
        offset += len(text)
    units = _pack_section_units(
        first_ordinal=0,
        contract_revision_id="contract-one",
        scope="major_section",
        major_section_node_id="major-one",
        sources=sources,
        profile=profile,
        query="broad query",
    )
    assert len(units) > 1
    assert [item for unit in units for item in unit.sources] == sources
    assert [unit.plan.ordinal for unit in units] == list(range(len(units)))


def test_single_oversized_leaf_splits_deterministically_without_rewriting() -> None:
    profile = _profile()
    text = "".join(f"paragraph-{index}-" + chr(65 + index) * 6_000 + "\n\n" for index in range(8))
    source = (
        MapSourceRef(
            page=3,
            source_start=100,
            source_end=100 + len(text),
            text_sha256=hashlib.sha256(text.encode()).hexdigest(),
        ),
        text,
    )
    units = _pack_section_units(
        first_ordinal=0,
        contract_revision_id="contract-oversized-leaf",
        scope="major_section",
        major_section_node_id="major-oversized-leaf",
        sources=(source,),
        profile=profile,
        query="broad query",
    )
    fragments = [item for unit in units for item in unit.sources]
    assert len(units) > 1
    assert "".join(fragment_text for _, fragment_text in fragments) == text
    assert fragments[0][0].source_start == source[0].source_start
    assert fragments[-1][0].source_end == source[0].source_end
    assert all(
        left.source_end == right.source_start
        for (left, _), (right, _) in zip(fragments, fragments[1:], strict=False)
    )
    assert all(
        map_reduce_module._prepare_map_call(profile, "broad query", unit).input_characters
        <= profile.maximum_input_characters
        for unit in units
    )
    repeated = _pack_section_units(
        first_ordinal=0,
        contract_revision_id="contract-oversized-leaf",
        scope="major_section",
        major_section_node_id="major-oversized-leaf",
        sources=(source,),
        profile=profile,
        query="broad query",
    )
    assert repeated == units


def _synthetic_span(index: int) -> ExactEvidenceSpan:
    text = chr(65 + index) * 900
    start = index * 1_000
    return ExactEvidenceSpan(
        contract_revision_id="contract-one",
        page=1,
        source_start=start,
        source_end=start + len(text),
        text_sha256=hashlib.sha256(text.encode()).hexdigest(),
        text=text,
    )


def _hierarchical_ledger() -> MapReduceLedger:
    profile = _profile()
    spans = tuple(_synthetic_span(index) for index in range(24))
    unit = MapUnitPlan(
        unit_id="map-unit-one",
        contract_revision_id="contract-one",
        ordinal=0,
        scope="contract",
        source_refs=(
            MapSourceRef(
                page=1,
                source_start=0,
                source_end=24_000,
                text_sha256="b" * 64,
            ),
        ),
        source_character_count=24_000,
        input_sha256="c" * 64,
    )
    expected = (
        ExpectedContractMap(
            contract_revision_id="contract-one",
            unit_ids=(unit.unit_id,),
        ),
    )
    plan_hash = canonical_sha256(
        {
            "schema_version": CORPUS_PLAN_SCHEMA,
            "expected_contracts": expected,
            "map_units": (unit,),
        }
    )
    query_sha = "d" * 64
    identity_payload = {
        "schema_version": "cardrag.experimental-map-reduce-identity.v1",
        "generation_id": "generation-one",
        "query_sha256": query_sha,
        "profile_id": profile.profile_id,
    }
    identity = MapReduceIdentity(
        job_id="map-reduce-" + canonical_sha256(identity_payload),
        generation_id="generation-one",
        query_sha256=query_sha,
        profile_id=profile.profile_id,
    )
    return MapReduceLedger(
        status="progress",
        identity=identity,
        profile=profile,
        corpus_plan_sha256=plan_hash,
        expected_contracts=expected,
        map_units=(unit,),
        completed_maps=(
            MapUnitResult(
                unit_id=unit.unit_id,
                contract_revision_id="contract-one",
                provider_relevant=True,
                relevant=True,
                spans=spans,
                rejected_span_count=0,
                provider_response_sha256="e" * 64,
                receipt_sha256="f" * 64,
            ),
        ),
        receipt_sha256s=("f" * 64,),
        provider_call_count=1,
        provider_input_characters=1,
        accounted_output_tokens=profile.maximum_completion_tokens,
    )


def test_hierarchical_reduce_is_bounded_resumable_and_exact_subset_only() -> None:
    ledger = _hierarchical_ledger()
    initial_keys = {_span_key(span) for result in ledger.completed_maps for span in result.spans}
    provider_calls = 0
    while True:
        state = _reduction_state(ledger)
        if isinstance(state, _TerminalReduction):
            break
        assert isinstance(state, _PendingReduction)
        provider_calls += 1
        output_limit = 64 if state.final_batch else min(64, max(1, len(state.spans) // 2))
        output = state.spans[:output_limit]
        receipt_sha256 = hashlib.sha256(f"receipt-{provider_calls}".encode()).hexdigest()
        result = ReduceBatchResult(
            round_index=state.round_index,
            batch_index=state.batch_index,
            input_span_sha256=_reduce_input_sha256(state.spans),
            input_span_count=len(state.spans),
            provider_relevant=True,
            relevant=bool(output),
            spans=output,
            rejected_span_count=0,
            provider_response_sha256=hashlib.sha256(str(provider_calls).encode()).hexdigest(),
            receipt_sha256=receipt_sha256,
        )
        ledger = MapReduceLedger.model_validate(
            {
                **ledger.model_dump(mode="python"),
                "completed_reductions": (*ledger.completed_reductions, result),
                "receipt_sha256s": (*ledger.receipt_sha256s, receipt_sha256),
                "provider_call_count": ledger.provider_call_count + 1,
                "provider_input_characters": ledger.provider_input_characters + 1,
                "accounted_output_tokens": (
                    ledger.accounted_output_tokens + ledger.profile.maximum_completion_tokens
                ),
            }
        )
    assert provider_calls > 1
    assert state.result.spans
    assert {_span_key(span) for span in state.result.spans} <= initial_keys
    assert max(item.round_index for item in ledger.completed_reductions) < 32


def test_minimum_profile_can_pair_two_maximum_serialized_exact_spans() -> None:
    text = "\x00" * 1_024
    span = ExactEvidenceSpan(
        contract_revision_id="c" * 512,
        page=2**31,
        source_start=2**40,
        source_end=2**40 + len(text),
        text_sha256=hashlib.sha256(text.encode()).hexdigest(),
        text=text,
    )
    assert _span_fits_hierarchical_reduce(span, _profile()) is True


def test_artifact_cap_rejects_before_job_directory_creation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    lane = ExperimentalMapReduceLane(
        store,
        _FakeReasoner(),
        _profile(),
        maximum_jobs=1,
        maximum_total_bytes=10_000,
        maximum_artifact_bytes=100,
    )
    with pytest.raises(ExperimentalMapReduceError, match="artifact exceeds"):
        import asyncio

        asyncio.run(lane.run("artifact cap"))
    root = store.root / "experimental-map-reduce-jobs"
    assert not root.exists() or not tuple(root.iterdir())


def test_settings_are_default_off_candidate_only_and_require_a_sealed_profile() -> None:
    bearer = "test-static-bearer-token-000000000000"
    stable = Settings(environment="test", mcp_bearer_token=bearer)
    assert stable.experimental_map_reduce_enabled is False
    assert stable.experimental_map_reduce_model is None
    assert stable.experimental_map_reduce_provider_id is None
    assert stable.experimental_map_reduce_evaluation_sha256 is None

    with pytest.raises(ValueError, match="candidate-v1.0.11"):
        Settings(
            environment="test",
            mcp_bearer_token=bearer,
            openrouter_api_key="secret",
            experimental_map_reduce_enabled=True,
            experimental_map_reduce_model="openai/reasoning-test",
            experimental_map_reduce_provider_id="test-provider",
            experimental_map_reduce_evaluation_sha256="a" * 64,
        )
    with pytest.raises(ValueError, match="sealed model"):
        Settings(
            environment="test",
            channel="candidate-v1.0.11",
            mcp_bearer_token=bearer,
            openrouter_api_key="secret",
            experimental_map_reduce_enabled=True,
        )
    enabled = Settings(
        environment="test",
        channel="candidate-v1.0.11",
        mcp_bearer_token=bearer,
        openrouter_api_key="secret",
        experimental_map_reduce_enabled=True,
        experimental_map_reduce_model="openai/reasoning-test",
        experimental_map_reduce_provider_id="test-provider",
        experimental_map_reduce_evaluation_sha256="a" * 64,
    )
    assert enabled.experimental_map_reduce_max_input_characters == 262_144
    assert enabled.experimental_map_reduce_max_completion_tokens == 4_096
    assert enabled.experimental_map_reduce_max_job_provider_calls == 4_096
    assert enabled.experimental_map_reduce_max_job_input_characters == 268_435_456
    assert enabled.experimental_map_reduce_max_job_output_tokens == 16_777_216
    assert enabled.experimental_map_reduce_max_concurrent_provider_calls == 1
    assert _profile(maximum_completion_tokens=2_048).profile_id != _profile().profile_id
    assert _profile(maximum_job_provider_calls=4_095).profile_id != _profile().profile_id
    assert _profile(maximum_job_input_characters=268_435_455).profile_id != _profile().profile_id
    assert _profile(maximum_job_output_tokens=16_777_215).profile_id != _profile().profile_id
    with pytest.raises(ValueError):
        _profile(maximum_completion_tokens=16_385)


@pytest.mark.asyncio
async def test_default_runtime_constructs_no_reasoner_and_enabled_tool_is_separate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def forbidden_reasoner(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("default-off runtime constructed a reasoning provider")

    def capture_app(
        repository: ServingRepository,
        *_args: object,
        **kwargs: object,
    ) -> FastAPI:
        captured["repository"] = repository
        captured["lane"] = kwargs.get("experimental_map_reduce")
        return FastAPI()

    monkeypatch.setattr(main_module, "OpenRouterExperimentalReasoner", forbidden_reasoner)
    monkeypatch.setattr(main_module, "build_app", capture_app)
    main_module.create_app(
        Settings(
            environment="test",
            mcp_bearer_token="test-static-bearer-token-000000000000",
            mcp_state_dir=tmp_path / "disabled-state",
        )
    )
    assert captured["lane"] is None
    repository = captured["repository"]
    assert isinstance(repository, ServingRepository)
    await repository.embedder.close()

    store = _store(tmp_path / "enabled")
    vector = np.zeros((4096,), dtype=np.float32)
    vector[0] = 1.0
    serving = ServingRepository(
        store,
        FakeEmbedder(vector),
        cursor_secret=b"experimental-map-reduce-tool-secret",
    )
    disabled_settings = Settings(
        environment="test",
        mcp_bearer_token="test-static-bearer-token-000000000000",
    )
    disabled_tools = await build_mcp_server(
        serving,
        store,
        disabled_settings,
    ).list_tools()
    assert len(disabled_tools) == 12
    assert all(tool.name != "experimental_long_context_audit" for tool in disabled_tools)
    lane = ExperimentalMapReduceLane(store, _FakeReasoner(), _profile())
    settings = Settings(
        environment="test",
        channel="candidate-v1.0.11",
        mcp_bearer_token="test-static-bearer-token-000000000000",
        openrouter_api_key="secret",
        experimental_map_reduce_enabled=True,
        experimental_map_reduce_model="openai/reasoning-test",
        experimental_map_reduce_provider_id="test-provider",
        experimental_map_reduce_evaluation_sha256="a" * 64,
    )
    tools = await build_mcp_server(serving, store, settings, lane).list_tools()
    names = {tool.name for tool in tools}
    assert "experimental_long_context_audit" in names
    assert len(names) == 13
    schema = next(
        tool.input_schema for tool in tools if tool.name == "experimental_long_context_audit"
    )
    assert schema["properties"]["action"]["enum"] == ["start", "poll", "cancel"]
    assert "job_id" in schema["properties"]


@pytest.mark.asyncio
async def test_enabled_main_wires_exactly_the_sealed_reasoning_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class ConstructedReasoner(_FakeReasoner):
        def __init__(self, **kwargs: object) -> None:
            super().__init__()
            captured["reasoner_kwargs"] = kwargs

    def capture_app(
        repository: ServingRepository,
        *_args: object,
        **kwargs: object,
    ) -> FastAPI:
        captured["repository"] = repository
        captured["lane"] = kwargs["experimental_map_reduce"]
        return FastAPI()

    monkeypatch.setattr(main_module, "OpenRouterExperimentalReasoner", ConstructedReasoner)
    monkeypatch.setattr(main_module, "build_app", capture_app)
    main_module.create_app(
        Settings(
            environment="test",
            channel="candidate-v1.0.11",
            mcp_bearer_token="test-static-bearer-token-000000000000",
            mcp_state_dir=tmp_path / "enabled-main-state",
            openrouter_api_key="provider-secret",
            experimental_map_reduce_enabled=True,
            experimental_map_reduce_model="openai/reasoning-test",
            experimental_map_reduce_provider_id="test-provider",
            experimental_map_reduce_evaluation_sha256="a" * 64,
            experimental_map_reduce_max_completion_tokens=2_048,
            experimental_map_reduce_max_job_provider_calls=100,
            experimental_map_reduce_max_job_input_characters=1_000_000,
            experimental_map_reduce_max_job_output_tokens=204_800,
            experimental_map_reduce_max_concurrent_provider_calls=2,
        )
    )
    lane = captured["lane"]
    assert isinstance(lane, ExperimentalMapReduceLane)
    assert lane.profile.model == "openai/reasoning-test"
    assert lane.profile.provider_id == "test-provider"
    assert lane.profile.evaluation_artifact_sha256 == "a" * 64
    assert lane.profile.maximum_completion_tokens == 2_048
    assert lane.profile.maximum_job_provider_calls == 100
    assert lane.profile.maximum_job_input_characters == 1_000_000
    assert lane.profile.maximum_job_output_tokens == 204_800
    policy = json.loads(
        (
            tmp_path / "enabled-main-state" / "experimental-map-reduce-coordination" / "policy.json"
        ).read_bytes()
    )
    assert policy["maximum_concurrent_provider_calls"] == 2
    reasoner_kwargs = captured["reasoner_kwargs"]
    assert isinstance(reasoner_kwargs, dict)
    assert reasoner_kwargs["profile"] == lane.profile
    assert isinstance(lane.provider, ConstructedReasoner)
    assert lane.provider.map_calls == []
    assert lane.provider.reduce_calls == []
    await lane.close()
    repository = captured["repository"]
    assert isinstance(repository, ServingRepository)
    await repository.embedder.close()


@pytest.mark.asyncio
async def test_openrouter_reasoner_pins_provider_model_and_strict_json_schema() -> None:
    captured: list[dict[str, object]] = []
    decision = canonical_json_bytes({"relevant": False, "spans": []}).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer provider-secret"
        body = json.loads(request.content)
        captured.append(body)
        return httpx.Response(
            200,
            json={
                "model": "openai/reasoning-test",
                "provider_id": "test-provider",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": decision},
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 3,
                    "total_tokens": 13,
                },
            },
        )

    client = httpx.AsyncClient(
        base_url="https://openrouter.example/api/v1/",
        transport=httpx.MockTransport(handler),
    )
    profile = _profile()
    reasoner = OpenRouterExperimentalReasoner(
        base_url="https://openrouter.example/api/v1",
        api_key="provider-secret",
        timeout_seconds=10,
        profile=profile,
        client=client,
    )
    completion = await reasoner.map(query="query", unit=_http_test_unit())
    assert _parse_provider_decision(completion.content).spans == ()
    assert completion.envelope.usage.completion_tokens == 3
    request_body = captured[0]
    assert request_body["model"] == profile.model
    assert request_body["max_completion_tokens"] == profile.maximum_completion_tokens
    assert "max_tokens" not in request_body
    assert request_body["provider"] == {
        "order": [profile.provider_id],
        "only": [profile.provider_id],
        "allow_fallbacks": False,
        "require_parameters": True,
    }
    response_format = request_body["response_format"]
    assert isinstance(response_format, dict)
    assert response_format["json_schema"]["strict"] is True
    await client.aclose()


@pytest.mark.parametrize(
    "value",
    (
        "",
        "http://openrouter.example/api/v1",
        "https://user:secret@openrouter.example/api/v1",
        "https://openrouter.example/api/v1?redirect=1",
        "https://openrouter.example/api/v1?",
        "https://openrouter.example/api/v1#fragment",
        "https://openrouter.example/api/v1#",
        "https://openrouter.example:notaport/api/v1",
        "https://openrouter.example:0/api/v1",
        "https://openrouter.example:65536/api/v1",
        "//openrouter.example/api/v1",
        " https://openrouter.example/api/v1",
        "https://openrouter.example/api/v1\n",
        "https://openrouter.example/api/\x00v1",
        "https://openrouter.example/api/\x01v1",
        "https://openrouter.example/api/\x1fv1",
        "https://openrouter.example/api/\x7fv1",
        "https://openrouter.example/api/\x85v1",
        "https://openrouter.example/api/\u200bv1",
        "https:\\evil.example\\api",
    ),
)
def test_openrouter_reasoner_rejects_unsafe_url_before_secret_or_client(
    value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def forbidden_client(*_args: object, **_kwargs: object) -> object:
        calls.append("client")
        raise AssertionError("invalid URL must not construct a provider client")

    monkeypatch.setattr(map_reduce_module.httpx, "AsyncClient", forbidden_client)
    reasoner = OpenRouterExperimentalReasoner.__new__(OpenRouterExperimentalReasoner)
    with pytest.raises(ValueError, match="credential-free HTTPS"):
        reasoner.__init__(
            base_url=value,
            api_key="provider-secret",
            timeout_seconds=10,
            profile=_profile(),
        )
    assert calls == []
    assert not hasattr(reasoner, "_api_key")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("client_base_url", "follow_redirects"),
    (
        ("http://openrouter.example/api/v1/", False),
        ("https://other.example/api/v1/", False),
        ("https://openrouter.example/api/v1/", True),
    ),
)
async def test_openrouter_reasoner_rejects_unsafe_injected_client_without_a_request(
    client_base_url: str,
    follow_redirects: bool,
) -> None:
    request_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        raise AssertionError("unsafe injected provider client must not make a request")

    client = httpx.AsyncClient(
        base_url=client_base_url,
        follow_redirects=follow_redirects,
        transport=httpx.MockTransport(handler),
    )
    reasoner = OpenRouterExperimentalReasoner.__new__(OpenRouterExperimentalReasoner)
    with pytest.raises(ValueError):
        reasoner.__init__(
            base_url="https://openrouter.example/api/v1",
            api_key="provider-secret",
            timeout_seconds=10,
            profile=_profile(),
            client=client,
        )
    assert request_count == 0
    assert not hasattr(reasoner, "_api_key")
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response_body",
    (
        b'{"model":"openai/reasoning-test","model":"openai/reasoning-test",'
        b'"provider_id":"test-provider","choices":[]}',
        b'{"model":"openai/reasoning-test","provider_id":"test-provider","usage":NaN,"choices":[]}',
    ),
    ids=("duplicate-key", "non-finite-number"),
)
async def test_openrouter_reasoner_rejects_non_strict_response_envelope(
    response_body: bytes,
) -> None:
    request_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            200,
            content=response_body,
            headers={"content-type": "application/json"},
        )

    client = httpx.AsyncClient(
        base_url="https://openrouter.example/api/v1/",
        transport=httpx.MockTransport(handler),
    )
    reasoner = OpenRouterExperimentalReasoner(
        base_url="https://openrouter.example/api/v1",
        api_key="provider-secret",
        timeout_seconds=10,
        profile=_profile(),
        client=client,
    )
    with pytest.raises(ExperimentalMapReduceError, match="provider contract is invalid"):
        await reasoner.map(query="query", unit=_http_test_unit())
    assert request_count == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_openrouter_reasoner_rejects_missing_usage_fail_closed() -> None:
    request_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            200,
            json={
                "model": "openai/reasoning-test",
                "provider_id": "test-provider",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": canonical_json_bytes(
                                {"relevant": False, "spans": []}
                            ).decode(),
                        },
                    }
                ],
            },
        )

    client = httpx.AsyncClient(
        base_url="https://openrouter.example/api/v1/",
        transport=httpx.MockTransport(handler),
    )
    reasoner = OpenRouterExperimentalReasoner(
        base_url="https://openrouter.example/api/v1",
        api_key="provider-secret",
        timeout_seconds=10,
        profile=_profile(),
        client=client,
    )
    with pytest.raises(ExperimentalMapReduceError, match="provider contract is invalid"):
        await reasoner.map(query="query", unit=_http_test_unit())
    assert request_count == 1
    await client.aclose()
