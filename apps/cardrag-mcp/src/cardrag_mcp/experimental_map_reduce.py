"""Default-off, resumable long-context map-reduce quality-audit lane.

This module is deliberately independent from :mod:`cardrag_mcp.exact`.  It reads
the same immutable v5 generation, but its provider output can only be persisted
as an experimental audit artifact; it is never merged into primary ranking or
evidence bundles.
"""

from __future__ import annotations

import asyncio
import base64
import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import tempfile
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol, Self, TypeVar
from urllib.parse import urlsplit

import httpx
from cardrag_core import canonical_json_bytes, canonical_sha256
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from cardrag_mcp.quota import (
    StorageQuotaError,
    safe_shared_exhaustive_audit_usage,
    state_quota_guard,
    state_quota_policy,
    state_quota_transaction,
    validate_byte_limit,
    validate_count_limit,
)
from cardrag_mcp.store import GenerationHandle, GenerationStore

PROFILE_SCHEMA = "cardrag.experimental-map-reduce-profile.v1"
IDENTITY_SCHEMA = "cardrag.experimental-map-reduce-identity.v1"
CORPUS_PLAN_SCHEMA = "cardrag.experimental-map-reduce-corpus-plan.v1"
LEDGER_SCHEMA = "cardrag.experimental-map-reduce-ledger.v1"
PROMPT_POLICY = "cardrag.experimental-map-reduce-prompts.v1"
RECEIPT_SCHEMA = "cardrag.experimental-map-reduce-call-receipt.v1"

MAX_QUERY_CHARACTERS = 2_000
MAX_MAP_UNITS = 100_000
MAX_SOURCE_REFS_PER_UNIT = 100_000
MAX_PROVIDER_SPANS = 64
MAX_EVIDENCE_SPAN_CHARACTERS = 1_024
MAX_PROVIDER_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_LEDGER_BYTES = 256 * 1024 * 1024
MAX_REDUCE_ROUNDS = 32
MAX_REDUCE_BATCHES = 2 * MAX_PROVIDER_SPANS * MAX_MAP_UNITS
MAX_JOB_PROVIDER_CALLS = MAX_REDUCE_BATCHES + MAX_MAP_UNITS
MAX_JOB_INPUT_CHARACTERS = 2_000_000_000
MAX_JOB_OUTPUT_TOKENS = 2_000_000_000

Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Identifier = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,511}$"),
]
ProviderIdentifier = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}$"),
]

_JOB_ID = re.compile(r"^map-reduce-[0-9a-f]{64}$")
_RECEIPT_FILE = re.compile(r"^receipt-([0-9]{8})\.json$")
_INITIAL_PROGRESS_TEMP = re.compile(r"^\.progress\.json\.[A-Za-z0-9_-]{1,128}$")
_OWNED_JOB_TEMP = re.compile(
    r"^\.(progress\.json|receipt-[0-9]{8}\.json|artifact-[0-9a-f]{64}\.json|"
    r"COMPLETE\.json|CANCELLED\.json)\.[A-Za-z0-9_-]{1,128}$"
)
_PROVIDER_POLICY_TEMP = re.compile(r"^\.policy\.json\.[A-Za-z0-9_-]{1,128}$")
_MAXIMUM_INITIAL_PROGRESS_TEMPS = 32
_MAXIMUM_OWNED_JOB_TEMPS = 64
_LOCAL_JOB_LOCK_STRIPES = 1024


class ExperimentalMapReduceError(RuntimeError):
    """A provider, corpus, checkpoint, or quota contract failed closed."""


async def _cancellation_fenced_to_thread[**P, T](
    function: Callable[P, T],
    *args: P.args,
    **kwargs: P.kwargs,
) -> T:
    """Keep storage/generation leases until a blocking operation really ends."""

    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        if task.done() and not task.cancelled():
            task.exception()
        raise


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self)


class _ProviderConcurrencyPolicy(_StrictModel):
    schema_version: Literal["cardrag.experimental-map-reduce-concurrency.v1"] = (
        "cardrag.experimental-map-reduce-concurrency.v1"
    )
    maximum_concurrent_provider_calls: int = Field(ge=1, le=32)


class ExperimentalMapReduceProfile(_StrictModel):
    """Sealed reasoning-model and prompt contract selected by gold evaluation."""

    schema_version: Literal["cardrag.experimental-map-reduce-profile.v1"] = (
        "cardrag.experimental-map-reduce-profile.v1"
    )
    profile_id: Identifier
    model: ProviderIdentifier
    provider_id: Identifier
    evaluation_artifact_sha256: Sha256Hex
    prompt_policy: Literal["cardrag.experimental-map-reduce-prompts.v1"] = (
        "cardrag.experimental-map-reduce-prompts.v1"
    )
    maximum_input_characters: int = Field(ge=16_384, le=2_000_000)
    maximum_completion_tokens: int = Field(ge=16, le=16_384)
    maximum_response_bytes: int = Field(ge=1_024, le=MAX_PROVIDER_RESPONSE_BYTES)
    maximum_job_provider_calls: int = Field(ge=1, le=MAX_JOB_PROVIDER_CALLS)
    maximum_job_input_characters: int = Field(ge=16_384, le=MAX_JOB_INPUT_CHARACTERS)
    maximum_job_output_tokens: int = Field(ge=16, le=MAX_JOB_OUTPUT_TOKENS)

    @classmethod
    def seal(
        cls,
        *,
        model: str,
        provider_id: str,
        evaluation_artifact_sha256: str,
        maximum_input_characters: int,
        maximum_completion_tokens: int,
        maximum_response_bytes: int,
        maximum_job_provider_calls: int,
        maximum_job_input_characters: int,
        maximum_job_output_tokens: int,
    ) -> ExperimentalMapReduceProfile:
        payload = {
            "schema_version": PROFILE_SCHEMA,
            "model": model,
            "provider_id": provider_id,
            "evaluation_artifact_sha256": evaluation_artifact_sha256,
            "prompt_policy": PROMPT_POLICY,
            "maximum_input_characters": maximum_input_characters,
            "maximum_completion_tokens": maximum_completion_tokens,
            "maximum_response_bytes": maximum_response_bytes,
            "maximum_job_provider_calls": maximum_job_provider_calls,
            "maximum_job_input_characters": maximum_job_input_characters,
            "maximum_job_output_tokens": maximum_job_output_tokens,
        }
        return cls(
            profile_id="cardrag.experimental-map-reduce." + canonical_sha256(payload),
            model=model,
            provider_id=provider_id,
            evaluation_artifact_sha256=evaluation_artifact_sha256,
            maximum_input_characters=maximum_input_characters,
            maximum_completion_tokens=maximum_completion_tokens,
            maximum_response_bytes=maximum_response_bytes,
            maximum_job_provider_calls=maximum_job_provider_calls,
            maximum_job_input_characters=maximum_job_input_characters,
            maximum_job_output_tokens=maximum_job_output_tokens,
        )

    @model_validator(mode="after")
    def profile_id_binds_every_setting(self) -> Self:
        payload = self.model_dump(mode="python", exclude={"profile_id"})
        if self.profile_id != "cardrag.experimental-map-reduce." + canonical_sha256(payload):
            raise ValueError("experimental map-reduce profile ID is stale")
        return self


class MapSourceRef(_StrictModel):
    page: int = Field(ge=1)
    source_start: int = Field(ge=0)
    source_end: int = Field(gt=0)
    text_sha256: Sha256Hex

    @model_validator(mode="after")
    def source_range_is_nonempty(self) -> Self:
        if self.source_end <= self.source_start:
            raise ValueError("map source range is empty")
        return self


class MapUnitPlan(_StrictModel):
    unit_id: Identifier
    contract_revision_id: Identifier
    ordinal: int = Field(ge=0, lt=MAX_MAP_UNITS)
    scope: Literal["contract", "major_section", "unclassified"]
    major_section_node_id: Identifier | None = None
    source_refs: tuple[MapSourceRef, ...] = Field(
        min_length=1,
        max_length=MAX_SOURCE_REFS_PER_UNIT,
    )
    source_character_count: int = Field(gt=0)
    input_sha256: Sha256Hex

    @model_validator(mode="after")
    def scope_and_source_are_coherent(self) -> Self:
        if (self.scope == "major_section") != (self.major_section_node_id is not None):
            raise ValueError("major-section map unit has incoherent node identity")
        refs = tuple((item.page, item.source_start, item.source_end) for item in self.source_refs)
        if refs != tuple(sorted(set(refs))):
            raise ValueError("map unit source refs must be sorted and unique")
        if self.source_character_count != sum(
            item.source_end - item.source_start for item in self.source_refs
        ):
            raise ValueError("map unit source character count is stale")
        return self


class ExpectedContractMap(_StrictModel):
    contract_revision_id: Identifier
    unit_ids: tuple[Identifier, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unit_ids_are_unique(self) -> Self:
        if len(self.unit_ids) != len(set(self.unit_ids)):
            raise ValueError("contract map unit IDs must be unique")
        return self


class MapReduceIdentity(_StrictModel):
    schema_version: Literal["cardrag.experimental-map-reduce-identity.v1"] = (
        "cardrag.experimental-map-reduce-identity.v1"
    )
    job_id: Identifier
    generation_id: Identifier
    query_sha256: Sha256Hex
    profile_id: Identifier

    @model_validator(mode="after")
    def job_id_binds_required_immutable_identity(self) -> Self:
        expected = "map-reduce-" + canonical_sha256(
            {
                "schema_version": IDENTITY_SCHEMA,
                "generation_id": self.generation_id,
                "query_sha256": self.query_sha256,
                "profile_id": self.profile_id,
            }
        )
        if self.job_id != expected or _JOB_ID.fullmatch(self.job_id) is None:
            raise ValueError("experimental map-reduce job ID is stale")
        return self


class ExactEvidenceSpan(_StrictModel):
    contract_revision_id: Identifier
    page: int = Field(ge=1)
    source_start: int = Field(ge=0)
    source_end: int = Field(gt=0)
    text_sha256: Sha256Hex
    text: str = Field(min_length=1, max_length=MAX_EVIDENCE_SPAN_CHARACTERS)

    @model_validator(mode="after")
    def text_is_exactly_hash_and_offset_bound(self) -> Self:
        if self.source_end <= self.source_start:
            raise ValueError("experimental evidence range is empty")
        if self.source_end - self.source_start != len(self.text):
            raise ValueError("experimental evidence offsets differ from text length")
        if hashlib.sha256(self.text.encode("utf-8")).hexdigest() != self.text_sha256:
            raise ValueError("experimental evidence text hash is stale")
        return self


class MapUnitResult(_StrictModel):
    unit_id: Identifier
    contract_revision_id: Identifier
    provider_relevant: bool
    relevant: bool
    spans: tuple[ExactEvidenceSpan, ...] = Field(max_length=MAX_PROVIDER_SPANS)
    rejected_span_count: int = Field(ge=0, le=MAX_PROVIDER_SPANS)
    provider_response_sha256: Sha256Hex
    receipt_sha256: Sha256Hex

    @model_validator(mode="after")
    def relevance_is_derived_only_from_accepted_exact_spans(self) -> Self:
        if self.relevant != (self.provider_relevant and bool(self.spans)):
            raise ValueError("map relevance is not derived from accepted exact spans")
        keys = tuple(_span_key(item) for item in self.spans)
        if len(keys) != len(set(keys)):
            raise ValueError("map evidence spans must be unique")
        return self


class ReduceBatchResult(_StrictModel):
    round_index: int = Field(ge=0, lt=MAX_REDUCE_ROUNDS)
    batch_index: int = Field(ge=0, lt=MAX_REDUCE_BATCHES)
    input_span_sha256: Sha256Hex
    input_span_count: int = Field(gt=0)
    provider_relevant: bool
    relevant: bool
    spans: tuple[ExactEvidenceSpan, ...] = Field(max_length=MAX_PROVIDER_SPANS)
    rejected_span_count: int = Field(ge=0, le=MAX_PROVIDER_SPANS)
    provider_response_sha256: Sha256Hex
    receipt_sha256: Sha256Hex

    @model_validator(mode="after")
    def relevance_is_derived_from_exact_subset(self) -> Self:
        if self.relevant != (self.provider_relevant and bool(self.spans)):
            raise ValueError("reduce batch relevance differs from its exact spans")
        keys = tuple(_span_key(item) for item in self.spans)
        if len(keys) != len(set(keys)):
            raise ValueError("reduce batch spans must be unique")
        return self


class ReduceResult(_StrictModel):
    mode: Literal["provider", "deterministic_empty"]
    provider_relevant: bool
    relevant: bool
    spans: tuple[ExactEvidenceSpan, ...] = Field(max_length=MAX_PROVIDER_SPANS)
    rejected_span_count: int = Field(ge=0, le=MAX_PROVIDER_SPANS)
    provider_response_sha256: Sha256Hex | None = None

    @model_validator(mode="after")
    def reduce_fields_are_coherent(self) -> Self:
        if self.relevant != (self.provider_relevant and bool(self.spans)):
            raise ValueError("reduce relevance is not derived from accepted exact spans")
        if self.mode == "deterministic_empty":
            if (
                self.provider_relevant
                or self.relevant
                or self.spans
                or self.rejected_span_count
                or self.provider_response_sha256 is not None
            ):
                raise ValueError("deterministic empty reduce has provider fields")
        elif self.provider_response_sha256 is None:
            raise ValueError("provider reduce is missing its response hash")
        keys = tuple(_span_key(item) for item in self.spans)
        if len(keys) != len(set(keys)):
            raise ValueError("reduce evidence spans must be unique")
        return self


class PendingProviderCall(_StrictModel):
    sequence: int = Field(ge=0, lt=MAX_JOB_PROVIDER_CALLS)
    phase: Literal["map", "reduce"]
    unit_id: Identifier | None = None
    round_index: int | None = Field(default=None, ge=0, lt=MAX_REDUCE_ROUNDS)
    batch_index: int | None = Field(default=None, ge=0, lt=MAX_REDUCE_BATCHES)
    request_sha256: Sha256Hex
    input_characters: int = Field(gt=0, le=2_000_000)
    maximum_completion_tokens: int = Field(ge=16, le=16_384)

    @model_validator(mode="after")
    def coordinates_match_phase(self) -> Self:
        if self.phase == "map":
            if self.unit_id is None or self.round_index is not None or self.batch_index is not None:
                raise ValueError("pending map call has invalid coordinates")
        elif self.unit_id is not None or self.round_index is None or self.batch_index is None:
            raise ValueError("pending reduce call has invalid coordinates")
        return self


class MapReduceLedger(_StrictModel):
    schema_version: Literal["cardrag.experimental-map-reduce-ledger.v1"] = (
        "cardrag.experimental-map-reduce-ledger.v1"
    )
    status: Literal["progress", "complete", "cancelled"]
    identity: MapReduceIdentity
    profile: ExperimentalMapReduceProfile
    corpus_plan_sha256: Sha256Hex
    expected_contracts: tuple[ExpectedContractMap, ...] = Field(min_length=1)
    map_units: tuple[MapUnitPlan, ...] = Field(min_length=1, max_length=MAX_MAP_UNITS)
    completed_maps: tuple[MapUnitResult, ...] = Field(max_length=MAX_MAP_UNITS)
    completed_reductions: tuple[ReduceBatchResult, ...] = Field(
        default=(),
        max_length=MAX_REDUCE_BATCHES,
    )
    receipt_sha256s: tuple[Sha256Hex, ...] = Field(
        default=(),
        max_length=MAX_JOB_PROVIDER_CALLS,
    )
    provider_call_count: int = Field(default=0, ge=0, le=MAX_JOB_PROVIDER_CALLS)
    provider_input_characters: int = Field(default=0, ge=0, le=MAX_JOB_INPUT_CHARACTERS)
    advertised_prompt_tokens: int = Field(default=0, ge=0)
    advertised_completion_tokens: int = Field(default=0, ge=0)
    advertised_total_tokens: int = Field(default=0, ge=0)
    accounted_output_tokens: int = Field(default=0, ge=0, le=MAX_JOB_OUTPUT_TOKENS)
    pending_call: PendingProviderCall | None = None
    reduce_result: ReduceResult | None = None

    @model_validator(mode="after")
    def ledger_is_an_ordered_complete_prefix(self) -> Self:
        if self.identity.profile_id != self.profile.profile_id:
            raise ValueError("map-reduce identity differs from its sealed profile")
        contract_ids = tuple(item.contract_revision_id for item in self.expected_contracts)
        if contract_ids != tuple(sorted(set(contract_ids))):
            raise ValueError("expected map-reduce contracts must be sorted and unique")
        if tuple(item.ordinal for item in self.map_units) != tuple(range(len(self.map_units))):
            raise ValueError("map-reduce unit ordinals must be contiguous")
        unit_ids = tuple(item.unit_id for item in self.map_units)
        if len(unit_ids) != len(set(unit_ids)):
            raise ValueError("map-reduce unit IDs must be unique")
        declared_units = tuple(
            unit_id for expected in self.expected_contracts for unit_id in expected.unit_ids
        )
        if declared_units != unit_ids:
            raise ValueError("map-reduce contract/unit plan is stale")
        unit_offset = 0
        for expected in self.expected_contracts:
            expected_length = len(expected.unit_ids)
            contract_units = self.map_units[unit_offset : unit_offset + expected_length]
            if any(
                unit.contract_revision_id != expected.contract_revision_id
                for unit in contract_units
            ):
                raise ValueError("map-reduce contract/unit plan crosses contracts")
            unit_offset += expected_length
        plan_sha256 = canonical_sha256(
            {
                "schema_version": CORPUS_PLAN_SCHEMA,
                "expected_contracts": self.expected_contracts,
                "map_units": self.map_units,
            }
        )
        if self.corpus_plan_sha256 != plan_sha256:
            raise ValueError("map-reduce corpus plan hash is stale")
        completed_ids = tuple(item.unit_id for item in self.completed_maps)
        if completed_ids != unit_ids[: len(completed_ids)]:
            raise ValueError("completed maps must be an ordered unit prefix")
        units_by_id = {item.unit_id: item for item in self.map_units}
        for result in self.completed_maps:
            unit = units_by_id[result.unit_id]
            if result.contract_revision_id != unit.contract_revision_id:
                raise ValueError("map result belongs to another contract")
            if any(
                span.contract_revision_id != result.contract_revision_id for span in result.spans
            ):
                raise ValueError("map result contains a cross-contract span")
        if self.completed_reductions and len(self.completed_maps) != len(self.map_units):
            raise ValueError("reduce began before every contract map completed")
        result_receipts = tuple(item.receipt_sha256 for item in self.completed_maps) + tuple(
            item.receipt_sha256 for item in self.completed_reductions
        )
        if result_receipts != self.receipt_sha256s:
            raise ValueError("map-reduce results differ from their ordered receipt chain")
        expected_call_count = len(self.receipt_sha256s) + (self.pending_call is not None)
        if self.provider_call_count != expected_call_count:
            raise ValueError("provider call counter differs from receipt/reservation count")
        if self.provider_call_count > self.profile.maximum_job_provider_calls:
            raise ValueError("provider call budget is exceeded")
        pending_input = 0 if self.pending_call is None else self.pending_call.input_characters
        if self.provider_input_characters < pending_input:
            raise ValueError("provider input counter is smaller than its pending reservation")
        if self.provider_input_characters > self.profile.maximum_job_input_characters:
            raise ValueError("provider input-character budget is exceeded")
        if self.accounted_output_tokens > self.profile.maximum_job_output_tokens:
            raise ValueError("provider output-token budget is exceeded")
        if self.accounted_output_tokens != (
            self.provider_call_count * self.profile.maximum_completion_tokens
        ):
            raise ValueError("provider output-token charges differ from sealed reservations")
        if self.pending_call is not None:
            if self.pending_call.sequence != len(self.receipt_sha256s):
                raise ValueError("pending provider call sequence is stale")
            if self.pending_call.maximum_completion_tokens != (
                self.profile.maximum_completion_tokens
            ):
                raise ValueError("pending provider completion cap differs from profile")
        reduction_coordinates = tuple(
            (item.round_index, item.batch_index) for item in self.completed_reductions
        )
        if reduction_coordinates != tuple(sorted(set(reduction_coordinates))):
            raise ValueError("reduce batch checkpoints must be sorted and unique")
        if reduction_coordinates:
            rounds = sorted({item[0] for item in reduction_coordinates})
            if rounds != list(range(rounds[-1] + 1)):
                raise ValueError("reduce rounds must be contiguous")
            for round_index in rounds[:-1]:
                indices = [
                    batch
                    for candidate_round, batch in reduction_coordinates
                    if candidate_round == round_index
                ]
                if indices != list(range(indices[-1] + 1)):
                    raise ValueError("completed reduce rounds must have contiguous batches")
            last_indices = [
                batch
                for candidate_round, batch in reduction_coordinates
                if candidate_round == rounds[-1]
            ]
            if last_indices != list(range(len(last_indices))):
                raise ValueError("active reduce round must be an ordered batch prefix")
        mapped_span_keys = {
            _span_key(span) for result in self.completed_maps for span in result.spans
        }
        if any(
            _span_key(span) not in mapped_span_keys
            for batch in self.completed_reductions
            for span in batch.spans
        ):
            raise ValueError("reduce checkpoint introduced evidence absent from maps")
        if self.status == "progress":
            if self.reduce_result is not None:
                raise ValueError("progress ledger cannot contain reduce output")
        elif self.status == "complete":
            if (
                len(self.completed_maps) != len(self.map_units)
                or self.reduce_result is None
                or self.pending_call is not None
            ):
                raise ValueError("complete map-reduce ledger is incomplete")
            accepted = mapped_span_keys
            if any(_span_key(span) not in accepted for span in self.reduce_result.spans):
                raise ValueError("reduce introduced evidence absent from contract maps")
        elif self.reduce_result is not None:
            raise ValueError("cancelled map-reduce ledger cannot contain reduce output")
        return self


class _ProgressEnvelope(_StrictModel):
    schema_version: Literal["cardrag.experimental-map-reduce-progress.v1"] = (
        "cardrag.experimental-map-reduce-progress.v1"
    )
    ledger_sha256: Sha256Hex
    ledger: MapReduceLedger

    @model_validator(mode="after")
    def progress_hash_is_current(self) -> Self:
        if self.ledger.status != "progress":
            raise ValueError("progress envelope contains a terminal ledger")
        if self.ledger_sha256 != canonical_sha256(self.ledger):
            raise ValueError("map-reduce progress hash is stale")
        return self


class _CompleteMarker(_StrictModel):
    schema_version: Literal["cardrag.experimental-map-reduce-complete.v1"] = (
        "cardrag.experimental-map-reduce-complete.v1"
    )
    job_id: Identifier
    artifact_sha256: Sha256Hex
    artifact_size_bytes: int = Field(gt=0)


class _CancelledMarker(_StrictModel):
    schema_version: Literal["cardrag.experimental-map-reduce-cancelled.v1"] = (
        "cardrag.experimental-map-reduce-cancelled.v1"
    )
    job_id: Identifier
    artifact_sha256: Sha256Hex
    artifact_size_bytes: int = Field(gt=0)


class _ProviderSpan(_StrictModel):
    contract_revision_id: Identifier
    page: int = Field(ge=1)
    source_start: int = Field(ge=0)
    source_end: int = Field(gt=0)
    quote: str = Field(min_length=1, max_length=MAX_EVIDENCE_SPAN_CHARACTERS)

    @model_validator(mode="after")
    def range_matches_quote_length(self) -> Self:
        if self.source_end <= self.source_start:
            raise ValueError("provider source range is empty")
        return self


class ProviderDecision(_StrictModel):
    relevant: bool
    spans: tuple[_ProviderSpan, ...] = Field(max_length=MAX_PROVIDER_SPANS)


class ProviderUsageReceipt(_StrictModel):
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    advertised_output_tokens: int = Field(ge=0)
    advertised_usage_sha256: Sha256Hex
    advertised_usage_json: str = Field(min_length=2, max_length=64 * 1024)

    @model_validator(mode="after")
    def usage_is_canonical_and_conservative(self) -> Self:
        try:
            encoded = self.advertised_usage_json.encode("utf-8")
            parsed = _strict_json_value(encoded)
        except (RecursionError, UnicodeError, ValueError) as exc:
            raise ValueError("advertised provider usage is not strict JSON") from exc
        if not isinstance(parsed, dict) or canonical_json_bytes(parsed) != encoded:
            raise ValueError("advertised provider usage is not canonical JSON")
        if canonical_sha256(parsed) != self.advertised_usage_sha256:
            raise ValueError("advertised provider usage hash is stale")
        if (
            parsed.get("prompt_tokens") != self.prompt_tokens
            or parsed.get("completion_tokens") != self.completion_tokens
            or parsed.get("total_tokens") != self.total_tokens
        ):
            raise ValueError("advertised provider usage counters are stale")
        parsed_reasoning_tokens = 0
        direct_reasoning = parsed.get("reasoning_tokens")
        if direct_reasoning is not None:
            if type(direct_reasoning) is not int or direct_reasoning < 0:
                raise ValueError("advertised provider reasoning tokens are invalid")
            parsed_reasoning_tokens = direct_reasoning
        details = parsed.get("completion_tokens_details")
        if details is not None:
            if not isinstance(details, dict):
                raise ValueError("advertised provider completion details are invalid")
            detailed_reasoning = details.get("reasoning_tokens")
            if detailed_reasoning is not None:
                if type(detailed_reasoning) is not int or detailed_reasoning < 0:
                    raise ValueError("advertised detailed reasoning tokens are invalid")
                parsed_reasoning_tokens = max(
                    parsed_reasoning_tokens,
                    detailed_reasoning,
                )
        if self.reasoning_tokens != parsed_reasoning_tokens:
            raise ValueError("advertised provider reasoning-token counter is stale")
        if self.total_tokens < self.prompt_tokens + self.completion_tokens:
            raise ValueError("advertised provider total tokens undercounts its components")
        if self.advertised_output_tokens != max(
            self.completion_tokens,
            self.reasoning_tokens,
        ):
            raise ValueError("provider output-token accounting is not conservative")
        return self


class ProviderEnvelopeReceipt(_StrictModel):
    response_body_sha256: Sha256Hex
    response_body_size_bytes: int = Field(gt=0, le=MAX_PROVIDER_RESPONSE_BYTES)
    response_model: ProviderIdentifier
    response_provider: str = Field(min_length=1, max_length=512)
    finish_reason: Literal["stop"] = "stop"
    usage: ProviderUsageReceipt


class ProviderCallReceipt(_StrictModel):
    schema_version: Literal["cardrag.experimental-map-reduce-call-receipt.v1"] = (
        "cardrag.experimental-map-reduce-call-receipt.v1"
    )
    job_id: Identifier
    sequence: int = Field(ge=0, lt=MAX_JOB_PROVIDER_CALLS)
    phase: Literal["map", "reduce"]
    unit_id: Identifier | None = None
    round_index: int | None = Field(default=None, ge=0, lt=MAX_REDUCE_ROUNDS)
    batch_index: int | None = Field(default=None, ge=0, lt=MAX_REDUCE_BATCHES)
    request_sha256: Sha256Hex
    input_characters: int = Field(gt=0, le=2_000_000)
    maximum_completion_tokens: int = Field(ge=16, le=16_384)
    model: ProviderIdentifier
    provider_id: Identifier
    profile_id: Identifier
    raw_content_sha256: Sha256Hex
    raw_content_base64: str = Field(min_length=4, max_length=12 * 1024 * 1024)
    parsed_decision_sha256: Sha256Hex
    decision: ProviderDecision
    envelope: ProviderEnvelopeReceipt

    @model_validator(mode="after")
    def receipt_binds_coordinates_and_decision(self) -> Self:
        try:
            raw_content = base64.b64decode(
                self.raw_content_base64.encode("ascii"),
                validate=True,
            )
            parsed = _strict_json_value(raw_content)
            if not isinstance(parsed, dict) or not isinstance(parsed.get("spans"), list):
                raise ValueError
            strict_payload = dict(parsed)
            strict_payload["spans"] = tuple(parsed["spans"])
            reparsed_decision = ProviderDecision.model_validate(strict_payload)
        except (RecursionError, UnicodeError, ValueError) as exc:
            raise ValueError("provider receipt raw content is not strict decision JSON") from exc
        if hashlib.sha256(raw_content).hexdigest() != self.raw_content_sha256:
            raise ValueError("provider receipt raw content hash is stale")
        if reparsed_decision != self.decision:
            raise ValueError("provider receipt decision differs from raw content")
        if self.parsed_decision_sha256 != canonical_sha256(self.decision):
            raise ValueError("provider receipt decision hash is stale")
        if self.phase == "map":
            if self.unit_id is None or self.round_index is not None or self.batch_index is not None:
                raise ValueError("map receipt has invalid coordinates")
        elif self.unit_id is not None or self.round_index is None or self.batch_index is None:
            raise ValueError("reduce receipt has invalid coordinates")
        return self


@dataclass(frozen=True, slots=True)
class _RuntimeUnit:
    plan: MapUnitPlan
    sources: tuple[tuple[MapSourceRef, str], ...]


@dataclass(frozen=True, slots=True)
class PreparedProviderCall:
    phase: Literal["map", "reduce"]
    system_prompt: str
    user_prompt: str
    request_body: Mapping[str, object]
    request_sha256: str
    input_characters: int
    maximum_completion_tokens: int


@dataclass(frozen=True, slots=True)
class ProviderCompletion:
    content: bytes
    envelope: ProviderEnvelopeReceipt


@dataclass(frozen=True, slots=True)
class _CorpusSnapshot:
    generation_id: str
    expected_contracts: tuple[ExpectedContractMap, ...]
    units: tuple[_RuntimeUnit, ...]
    corpus_plan_sha256: str
    pages: Mapping[tuple[str, int], str]


@dataclass(frozen=True, slots=True)
class LoadedMapReduceJob:
    ledger: MapReduceLedger
    resumed: bool
    cancelled: bool = False
    artifact_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class _PendingReduction:
    round_index: int
    batch_index: int
    spans: tuple[ExactEvidenceSpan, ...]
    final_batch: bool


@dataclass(frozen=True, slots=True)
class _TerminalReduction:
    result: ReduceResult


class ExperimentalMapReduceStatus(_StrictModel):
    generation_id: Identifier
    query_sha256: Sha256Hex
    profile_id: Identifier
    job_id: Identifier
    status: Literal["mapping", "reducing", "complete", "cancelled"]
    mapped_units: int = Field(ge=0)
    total_units: int = Field(gt=0)
    mapped_contracts: int = Field(ge=0)
    total_contracts: int = Field(gt=0)
    resumed: bool
    rejected_span_count: int = Field(ge=0)
    provider_call_count: int = Field(ge=0)
    provider_input_characters: int = Field(ge=0)
    advertised_prompt_tokens: int = Field(ge=0)
    advertised_completion_tokens: int = Field(ge=0)
    advertised_total_tokens: int = Field(ge=0)
    accounted_output_tokens: int = Field(ge=0)
    pending_provider_call: bool
    evidence_spans: tuple[ExactEvidenceSpan, ...] = Field(max_length=MAX_PROVIDER_SPANS)
    artifact_sha256: Sha256Hex | None = None
    experimental: Literal[True] = True
    primary_exact_influenced: Literal[False] = False

    @model_validator(mode="after")
    def terminal_fields_are_coherent(self) -> Self:
        if self.mapped_units > self.total_units or self.mapped_contracts > self.total_contracts:
            raise ValueError("map-reduce progress exceeds its corpus")
        if self.status == "complete":
            if (
                self.mapped_units != self.total_units
                or self.mapped_contracts != self.total_contracts
                or self.artifact_sha256 is None
            ):
                raise ValueError("complete map-reduce status is incomplete")
        elif self.status == "cancelled":
            if self.artifact_sha256 is None or self.evidence_spans:
                raise ValueError("cancelled map-reduce status lacks its terminal artifact")
        elif self.artifact_sha256 is not None or self.evidence_spans:
            raise ValueError("non-complete map-reduce status exposed final evidence")
        return self


class ExperimentalReasoningProvider(Protocol):
    async def complete(self, call: PreparedProviderCall) -> ProviderCompletion: ...

    async def close(self) -> None: ...


def _span_key(span: ExactEvidenceSpan) -> tuple[str, int, int, int, str, str]:
    return (
        span.contract_revision_id,
        span.page,
        span.source_start,
        span.source_end,
        span.text_sha256,
        span.text,
    )


def _provider_json_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["relevant", "spans"],
        "properties": {
            "relevant": {"type": "boolean"},
            "spans": {
                "type": "array",
                "maxItems": MAX_PROVIDER_SPANS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "contract_revision_id",
                        "page",
                        "source_start",
                        "source_end",
                        "quote",
                    ],
                    "properties": {
                        "contract_revision_id": {"type": "string", "minLength": 1},
                        "page": {"type": "integer", "minimum": 1},
                        "source_start": {"type": "integer", "minimum": 0},
                        "source_end": {"type": "integer", "minimum": 1},
                        "quote": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": MAX_EVIDENCE_SPAN_CHARACTERS,
                        },
                    },
                },
            },
        },
    }


def _response_provider(payload: Mapping[str, Any], response: httpx.Response) -> str | None:
    for candidate in (payload.get("provider_id"), payload.get("provider")):
        if isinstance(candidate, str) and candidate:
            return candidate
    metadata = payload.get("openrouter_metadata")
    if isinstance(metadata, Mapping):
        for key in ("provider_slug", "provider_id", "provider_name", "provider"):
            candidate = metadata.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
    header = response.headers.get("x-openrouter-provider")
    return header if isinstance(header, str) and header else None


def _normalized_provider(value: str) -> str:
    # Provider aliases are identifiers, not display labels.  Case is the only
    # tolerated presentation difference; punctuation remains identity-bearing.
    return value.casefold()


async def _bounded_response_bytes(response: httpx.Response, maximum_bytes: int) -> bytes:
    raw_length = response.headers.get("content-length")
    if raw_length is not None and (not raw_length.isdigit() or int(raw_length) > maximum_bytes):
        raise ExperimentalMapReduceError("reasoning provider response length is invalid")
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(chunk) > maximum_bytes - len(body):
            raise ExperimentalMapReduceError("reasoning provider response exceeds its cap")
        body.extend(chunk)
    return bytes(body)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _strict_json_value(payload: bytes) -> object:
    return json.loads(
        payload.decode("utf-8"),
        object_pairs_hook=_unique_json_object,
        parse_constant=_reject_json_constant,
    )


def _validated_reasoning_base_url(value: str) -> str:
    if (
        not value
        or len(value) > 4_096
        or value != value.strip()
        or "\\" in value
        or "?" in value
        or "#" in value
        or any(character.isspace() or not character.isprintable() for character in value)
    ):
        raise ValueError("experimental reasoning provider URL must be credential-free HTTPS")
    try:
        parsed = urlsplit(value)
        parsed_port = parsed.port
    except ValueError as exc:
        raise ValueError(
            "experimental reasoning provider URL must be credential-free HTTPS"
        ) from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (parsed_port is not None and not 1 <= parsed_port <= 65_535)
    ):
        raise ValueError("experimental reasoning provider URL must be credential-free HTTPS")
    return value.rstrip("/") + "/"


def _provider_usage_receipt(raw_usage: object) -> ProviderUsageReceipt:
    if not isinstance(raw_usage, dict):
        raise ValueError("provider usage is missing")

    def required_integer(key: str) -> int:
        value = raw_usage.get(key)
        if type(value) is not int or value < 0:
            raise ValueError(f"provider usage {key} is invalid")
        return value

    prompt_tokens = required_integer("prompt_tokens")
    completion_tokens = required_integer("completion_tokens")
    total_tokens = required_integer("total_tokens")
    reasoning_tokens = 0
    direct_reasoning = raw_usage.get("reasoning_tokens")
    if direct_reasoning is not None:
        if type(direct_reasoning) is not int or direct_reasoning < 0:
            raise ValueError("provider usage reasoning tokens are invalid")
        reasoning_tokens = direct_reasoning
    details = raw_usage.get("completion_tokens_details")
    if details is not None:
        if not isinstance(details, dict):
            raise ValueError("provider completion token details are invalid")
        detailed_reasoning = details.get("reasoning_tokens")
        if detailed_reasoning is not None:
            if type(detailed_reasoning) is not int or detailed_reasoning < 0:
                raise ValueError("provider detailed reasoning tokens are invalid")
            reasoning_tokens = max(reasoning_tokens, detailed_reasoning)
    advertised_usage_json = canonical_json_bytes(raw_usage).decode("utf-8")
    return ProviderUsageReceipt(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        reasoning_tokens=reasoning_tokens,
        advertised_output_tokens=max(completion_tokens, reasoning_tokens),
        advertised_usage_sha256=canonical_sha256(raw_usage),
        advertised_usage_json=advertised_usage_json,
    )


class OpenRouterExperimentalReasoner:
    """Strict OpenRouter JSON-schema client for the sealed experimental profile."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float,
        profile: ExperimentalMapReduceProfile,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        validated_base_url = _validated_reasoning_base_url(base_url)
        if client is not None:
            injected_base_url = _validated_reasoning_base_url(str(client.base_url))
            if injected_base_url != validated_base_url or client.follow_redirects:
                raise ValueError(
                    "experimental reasoning client must match its HTTPS base URL without redirects"
                )
        if not api_key:
            raise ValueError("experimental reasoning API key is empty")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("experimental reasoning timeout must be positive and finite")
        self.profile = profile
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=validated_base_url,
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def map(self, *, query: str, unit: _RuntimeUnit) -> ProviderCompletion:
        return await self.complete(_prepare_map_call(self.profile, query, unit))

    async def reduce(
        self,
        *,
        query: str,
        spans: Sequence[ExactEvidenceSpan],
        maximum_output_spans: int,
    ) -> ProviderCompletion:
        return await self.complete(
            _prepare_reduce_call(
                self.profile,
                query,
                spans,
                maximum_output_spans,
            )
        )

    async def complete(self, call: PreparedProviderCall) -> ProviderCompletion:
        if (
            call.input_characters > self.profile.maximum_input_characters
            or call.input_characters != len(call.system_prompt) + len(call.user_prompt)
            or call.maximum_completion_tokens != self.profile.maximum_completion_tokens
            or call.request_sha256 != canonical_sha256(call.request_body)
            or call.request_body.get("model") != self.profile.model
            or call.request_body.get("max_completion_tokens")
            != self.profile.maximum_completion_tokens
        ):
            raise ExperimentalMapReduceError("prepared provider request differs from its profile")
        try:
            async with asyncio.timeout(self._timeout_seconds):
                async with self._client.stream(
                    "POST",
                    "chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=call.request_body,
                ) as response:
                    response.raise_for_status()
                    response_body = await _bounded_response_bytes(
                        response,
                        self.profile.maximum_response_bytes,
                    )
        except (httpx.HTTPError, TimeoutError) as exc:
            raise ExperimentalMapReduceError("reasoning provider request failed") from exc
        try:
            payload: Any = _strict_json_value(response_body)
            if not isinstance(payload, Mapping) or payload.get("model") != self.profile.model:
                raise ValueError
            actual_provider = _response_provider(payload, response)
            if actual_provider is None or _normalized_provider(
                actual_provider
            ) != _normalized_provider(self.profile.provider_id):
                raise ValueError
            choices = payload.get("choices")
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError
            choice = choices[0]
            if not isinstance(choice, Mapping) or choice.get("finish_reason") != "stop":
                raise ValueError
            message = choice.get("message")
            if not isinstance(message, Mapping):
                raise ValueError
            content = message.get("content")
            if not isinstance(content, str):
                raise ValueError
            encoded = content.encode("utf-8")
            if not encoded or len(encoded) > self.profile.maximum_response_bytes:
                raise ValueError
            usage = _provider_usage_receipt(payload.get("usage"))
            if usage.advertised_output_tokens > call.maximum_completion_tokens:
                raise ValueError
            return ProviderCompletion(
                content=encoded,
                envelope=ProviderEnvelopeReceipt(
                    response_body_sha256=hashlib.sha256(response_body).hexdigest(),
                    response_body_size_bytes=len(response_body),
                    response_model=self.profile.model,
                    response_provider=actual_provider,
                    usage=usage,
                ),
            )
        except (RecursionError, TypeError, UnicodeError, ValueError) as exc:
            raise ExperimentalMapReduceError("reasoning provider contract is invalid") from exc


_MAP_SYSTEM_PROMPT = """You are CardRAG's experimental evidence mapper.
Return only JSON matching the supplied schema. Decide whether this one contract
unit is relevant to the query. Every quote must be copied byte-for-byte as
Unicode text from exactly one labelled OCR range, and page/source_start/
source_end plus contract_revision_id must identify that exact quote. Never
rewrite, normalize, summarize, infer, or quote outside this unit. Return at
most 64 short source spans."""

_REDUCE_SYSTEM_PROMPT = """You are CardRAG's experimental evidence reducer.
Return only JSON matching the supplied schema. Select only evidence spans that
directly answer the query. You may only copy complete spans from the supplied
validated map results with exactly identical page, offsets, and quote. Never
change contract_revision_id or create, combine, rewrite, normalize, or infer a
quote. Return at most 64 spans."""


def _map_user_prompt(query: str, unit: _RuntimeUnit) -> str:
    parts = [
        "QUERY:\n" + query,
        "CONTRACT_REVISION_ID: " + unit.plan.contract_revision_id,
        "MAP_UNIT_ID: " + unit.plan.unit_id,
        "SOURCE_RANGES:",
    ]
    for ref, text in unit.sources:
        parts.append(
            f"<<<PAGE={ref.page} START={ref.source_start} END={ref.source_end}>>>\n"
            + text
            + "\n<<<END_RANGE>>>"
        )
    return "\n".join(parts)


def _reduce_user_prompt(
    query: str,
    spans: Sequence[ExactEvidenceSpan],
    maximum_output_spans: int,
) -> str:
    evidence = [span.model_dump(mode="json") for span in spans]
    return (
        "QUERY:\n"
        + query
        + f"\nMAXIMUM_OUTPUT_SPANS: {maximum_output_spans}"
        + "\nVALIDATED_MAP_SPANS:\n"
        + json.dumps(
            evidence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _prepare_provider_call(
    profile: ExperimentalMapReduceProfile,
    *,
    phase: Literal["map", "reduce"],
    system_prompt: str,
    user_prompt: str,
) -> PreparedProviderCall:
    input_characters = len(system_prompt) + len(user_prompt)
    if input_characters > profile.maximum_input_characters:
        raise ExperimentalMapReduceError(f"{phase} prompt exceeds sealed profile input")
    request_body: dict[str, object] = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "model": profile.model,
        "provider": {
            "order": [profile.provider_id],
            "only": [profile.provider_id],
            "allow_fallbacks": False,
            "require_parameters": True,
        },
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": f"cardrag_experimental_{phase}_v1",
                "strict": True,
                "schema": _provider_json_schema(),
            },
        },
        "max_completion_tokens": profile.maximum_completion_tokens,
        "temperature": 0,
    }
    return PreparedProviderCall(
        phase=phase,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        request_body=request_body,
        request_sha256=canonical_sha256(request_body),
        input_characters=input_characters,
        maximum_completion_tokens=profile.maximum_completion_tokens,
    )


def _prepare_map_call(
    profile: ExperimentalMapReduceProfile,
    query: str,
    unit: _RuntimeUnit,
) -> PreparedProviderCall:
    return _prepare_provider_call(
        profile,
        phase="map",
        system_prompt=_MAP_SYSTEM_PROMPT,
        user_prompt=_map_user_prompt(query, unit),
    )


def _prepare_reduce_call(
    profile: ExperimentalMapReduceProfile,
    query: str,
    spans: Sequence[ExactEvidenceSpan],
    maximum_output_spans: int,
) -> PreparedProviderCall:
    return _prepare_provider_call(
        profile,
        phase="reduce",
        system_prompt=_REDUCE_SYSTEM_PROMPT,
        user_prompt=_reduce_user_prompt(query, spans, maximum_output_spans),
    )


def _reduce_fixed_characters() -> int:
    return (
        len(_REDUCE_SYSTEM_PROMPT)
        + len("QUERY:\n")
        + MAX_QUERY_CHARACTERS
        + len("\nMAXIMUM_OUTPUT_SPANS: ")
        + len(str(MAX_PROVIDER_SPANS))
        + len("\nVALIDATED_MAP_SPANS:\n")
        + 2  # JSON array brackets
    )


def _reduce_span_characters(span: ExactEvidenceSpan) -> int:
    return len(
        json.dumps(
            span.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _span_fits_hierarchical_reduce(
    span: ExactEvidenceSpan,
    profile: ExperimentalMapReduceProfile,
) -> bool:
    available = profile.maximum_input_characters - _reduce_fixed_characters()
    size = _reduce_span_characters(span)
    # Reserve room for at least two spans. Thus a frontier larger than one
    # always has a multi-span batch, and the sealed per-batch output caps make
    # every non-final round globally shrink.
    return available > 0 and size * 2 + 1 <= available


def _deduplicated_spans(
    spans: Sequence[ExactEvidenceSpan],
) -> tuple[ExactEvidenceSpan, ...]:
    by_key: dict[tuple[str, int, int, int, str, str], ExactEvidenceSpan] = {}
    for span in spans:
        by_key.setdefault(_span_key(span), span)
    return tuple(by_key.values())


def _partition_reduce_spans(
    spans: Sequence[ExactEvidenceSpan],
    profile: ExperimentalMapReduceProfile,
) -> tuple[tuple[ExactEvidenceSpan, ...], ...]:
    if not spans:
        return ()
    available = profile.maximum_input_characters - _reduce_fixed_characters()
    batches: list[list[ExactEvidenceSpan]] = []
    pending: list[ExactEvidenceSpan] = []
    pending_size = 0
    for span in spans:
        span_size = _reduce_span_characters(span)
        if span_size * 2 + 1 > available:
            raise ExperimentalMapReduceError(
                "validated span cannot enter bounded hierarchical reduce"
            )
        combined = pending_size + span_size + (1 if pending else 0)
        if pending and combined > available:
            batches.append(pending)
            pending = [span]
            pending_size = span_size
        else:
            pending.append(span)
            pending_size = combined
    if pending:
        batches.append(pending)
    return tuple(tuple(batch) for batch in batches)


def _reduce_input_sha256(spans: Sequence[ExactEvidenceSpan]) -> str:
    return canonical_sha256(
        {
            "schema_version": "cardrag.experimental-map-reduce-batch-input.v1",
            "spans": tuple(spans),
        }
    )


def _aggregate_reduce_response_sha256(results: Sequence[ReduceBatchResult]) -> str:
    return canonical_sha256(
        {
            "schema_version": "cardrag.experimental-map-reduce-response-chain.v1",
            "response_sha256s": tuple(item.provider_response_sha256 for item in results),
        }
    )


def _reduction_state(
    ledger: MapReduceLedger,
) -> _PendingReduction | _TerminalReduction:
    if len(ledger.completed_maps) != len(ledger.map_units):
        raise ExperimentalMapReduceError("reduce requested before all maps completed")
    frontier = _deduplicated_spans(
        tuple(span for result in ledger.completed_maps for span in result.spans)
    )
    if not frontier:
        if ledger.completed_reductions:
            raise ExperimentalMapReduceError("empty map frontier has reduce checkpoints")
        return _TerminalReduction(
            ReduceResult(
                mode="deterministic_empty",
                provider_relevant=False,
                relevant=False,
                spans=(),
                rejected_span_count=0,
            )
        )
    result_offset = 0
    for round_index in range(MAX_REDUCE_ROUNDS):
        batches = _partition_reduce_spans(frontier, ledger.profile)
        next_frontier: list[ExactEvidenceSpan] = []
        for batch_index, batch in enumerate(batches):
            if result_offset == len(ledger.completed_reductions):
                return _PendingReduction(
                    round_index=round_index,
                    batch_index=batch_index,
                    spans=batch,
                    final_batch=len(batches) == 1,
                )
            result = ledger.completed_reductions[result_offset]
            if (
                result.round_index != round_index
                or result.batch_index != batch_index
                or result.input_span_count != len(batch)
                or result.input_span_sha256 != _reduce_input_sha256(batch)
            ):
                raise ExperimentalMapReduceError("reduce checkpoint input identity is stale")
            allowed = {_span_key(span) for span in batch}
            if any(_span_key(span) not in allowed for span in result.spans):
                raise ExperimentalMapReduceError("reduce checkpoint introduced a new source span")
            if len(batches) > 1 and len(result.spans) > max(1, len(batch) // 2):
                raise ExperimentalMapReduceError(
                    "non-final reduce batch did not satisfy its sealed shrink bound"
                )
            next_frontier.extend(result.spans)
            result_offset += 1
        if len(batches) == 1:
            if result_offset != len(ledger.completed_reductions):
                raise ExperimentalMapReduceError("reduce ledger has checkpoints after final batch")
            final = ledger.completed_reductions[result_offset - 1]
            return _TerminalReduction(
                ReduceResult(
                    mode="provider",
                    provider_relevant=final.provider_relevant,
                    relevant=final.relevant,
                    spans=final.spans,
                    rejected_span_count=final.rejected_span_count,
                    provider_response_sha256=_aggregate_reduce_response_sha256(
                        ledger.completed_reductions
                    ),
                )
            )
        next_unique = _deduplicated_spans(next_frontier)
        if len(next_unique) >= len(frontier):
            raise ExperimentalMapReduceError("non-final hierarchical reduce round did not shrink")
        frontier = next_unique
        if not frontier:
            if result_offset != len(ledger.completed_reductions):
                raise ExperimentalMapReduceError("reduce ledger continued after an empty frontier")
            return _TerminalReduction(
                ReduceResult(
                    mode="provider",
                    provider_relevant=False,
                    relevant=False,
                    spans=(),
                    rejected_span_count=0,
                    provider_response_sha256=_aggregate_reduce_response_sha256(
                        ledger.completed_reductions
                    ),
                )
            )
    raise ExperimentalMapReduceError("hierarchical reduce exceeded its sealed round bound")


def _query(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > MAX_QUERY_CHARACTERS:
        raise ExperimentalMapReduceError("experimental query is blank or too long")
    return normalized


def _identity(
    generation_id: str,
    query_sha256: str,
    profile_id: str,
) -> MapReduceIdentity:
    job_id = "map-reduce-" + canonical_sha256(
        {
            "schema_version": IDENTITY_SCHEMA,
            "generation_id": generation_id,
            "query_sha256": query_sha256,
            "profile_id": profile_id,
        }
    )
    return MapReduceIdentity(
        job_id=job_id,
        generation_id=generation_id,
        query_sha256=query_sha256,
        profile_id=profile_id,
    )


def _source_payload_sha256(
    contract_revision_id: str,
    scope: str,
    major_section_node_id: str | None,
    sources: Sequence[tuple[MapSourceRef, str]],
) -> str:
    return canonical_sha256(
        {
            "schema_version": "cardrag.experimental-map-reduce-unit-input.v1",
            "contract_revision_id": contract_revision_id,
            "scope": scope,
            "major_section_node_id": major_section_node_id,
            "sources": [
                {"ref": ref, "text_sha256": hashlib.sha256(text.encode()).hexdigest()}
                for ref, text in sources
            ],
        }
    )


def _runtime_unit(
    *,
    ordinal: int,
    contract_revision_id: str,
    scope: Literal["contract", "major_section", "unclassified"],
    major_section_node_id: str | None,
    sources: Sequence[tuple[MapSourceRef, str]],
) -> _RuntimeUnit:
    input_sha256 = _source_payload_sha256(
        contract_revision_id,
        scope,
        major_section_node_id,
        sources,
    )
    unit_id = "map_unit_" + canonical_sha256(
        {
            "schema_version": "cardrag.experimental-map-reduce-unit-id.v1",
            "contract_revision_id": contract_revision_id,
            "scope": scope,
            "major_section_node_id": major_section_node_id,
            "input_sha256": input_sha256,
        }
    )
    plan = MapUnitPlan(
        unit_id=unit_id,
        contract_revision_id=contract_revision_id,
        ordinal=ordinal,
        scope=scope,
        major_section_node_id=major_section_node_id,
        source_refs=tuple(ref for ref, _ in sources),
        source_character_count=sum(len(text) for _, text in sources),
        input_sha256=input_sha256,
    )
    return _RuntimeUnit(plan=plan, sources=tuple(sources))


def _nearest_major_section(
    node_id: str,
    nodes: Mapping[str, tuple[str | None, str]],
) -> str | None:
    current: str | None = node_id
    visited: set[str] = set()
    while current is not None:
        if current in visited:
            raise ExperimentalMapReduceError("structure parent cycle reached map-reduce lane")
        visited.add(current)
        value = nodes.get(current)
        if value is None:
            raise ExperimentalMapReduceError("structure parent disappeared from map-reduce lane")
        parent_id, node_type = value
        if node_type == "MAJOR_SECTION":
            return current
        current = parent_id
    return None


def _major_section_sources(
    connection: sqlite3.Connection,
    contract_revision_id: str,
    pages: Mapping[int, str],
) -> list[
    tuple[
        Literal["major_section", "unclassified"],
        str | None,
        list[tuple[MapSourceRef, str]],
    ]
]:
    node_rows = connection.execute(
        """SELECT node_id,parent_id,node_type
             FROM structure_nodes
            WHERE contract_revision_id=?""",
        (contract_revision_id,),
    ).fetchall()
    nodes = {
        str(row[0]): (None if row[1] is None else str(row[1]), str(row[2])) for row in node_rows
    }
    span_rows = connection.execute(
        """SELECT node_id,page,source_start,source_end,text_sha256
             FROM node_spans
            WHERE contract_revision_id=? AND is_canonical=1
            ORDER BY page,source_start,source_end,node_id""",
        (contract_revision_id,),
    ).fetchall()
    grouped: defaultdict[str | None, list[tuple[MapSourceRef, str]]] = defaultdict(list)
    page_intervals: defaultdict[int, list[tuple[int, int]]] = defaultdict(list)
    for row in span_rows:
        node_id, page = str(row[0]), int(row[1])
        start, end = int(row[2]), int(row[3])
        page_text = pages.get(page)
        if page_text is None or start < 0 or end <= start or end > len(page_text):
            raise ExperimentalMapReduceError("canonical map source range is invalid")
        text = page_text[start:end]
        digest = hashlib.sha256(text.encode()).hexdigest()
        if digest != str(row[4]):
            raise ExperimentalMapReduceError("canonical map source text hash is invalid")
        group = _nearest_major_section(node_id, nodes)
        grouped[group].append(
            (
                MapSourceRef(
                    page=page,
                    source_start=start,
                    source_end=end,
                    text_sha256=digest,
                ),
                text,
            )
        )
        page_intervals[page].append((start, end))
    for page, page_text in pages.items():
        cursor = 0
        for start, end in sorted(page_intervals.get(page, [])):
            if start < cursor:
                raise ExperimentalMapReduceError("canonical map source spans overlap")
            if any(not character.isspace() for character in page_text[cursor:start]):
                raise ExperimentalMapReduceError("canonical map source has an uncovered OCR gap")
            cursor = end
        if any(not character.isspace() for character in page_text[cursor:]):
            raise ExperimentalMapReduceError("canonical map source has an uncovered OCR suffix")
    result: list[
        tuple[Literal["major_section", "unclassified"], str | None, list[tuple[MapSourceRef, str]]]
    ] = []
    for major_id, values in sorted(
        grouped.items(),
        key=lambda item: (
            item[1][0][0].page,
            item[1][0][0].source_start,
            "" if item[0] is None else item[0],
        ),
    ):
        ordered = sorted(
            values,
            key=lambda item: (item[0].page, item[0].source_start, item[0].source_end),
        )
        result.append(
            (
                "unclassified" if major_id is None else "major_section",
                major_id,
                ordered,
            )
        )
    if not result:
        raise ExperimentalMapReduceError("active contract has no canonical OCR map source")
    return result


def _pack_section_units(
    *,
    first_ordinal: int,
    contract_revision_id: str,
    scope: Literal["major_section", "unclassified"],
    major_section_node_id: str | None,
    sources: Sequence[tuple[MapSourceRef, str]],
    profile: ExperimentalMapReduceProfile,
    query: str,
) -> list[_RuntimeUnit]:
    """Pack exact leaves, deterministically subdividing only an oversized leaf."""

    if first_ordinal >= MAX_MAP_UNITS:
        raise ExperimentalMapReduceError("experimental map-reduce unit count exceeds its cap")
    packed: list[_RuntimeUnit] = []
    pending: list[tuple[MapSourceRef, str]] = []

    def candidate(values: Sequence[tuple[MapSourceRef, str]]) -> _RuntimeUnit:
        if first_ordinal + len(packed) >= MAX_MAP_UNITS:
            raise ExperimentalMapReduceError("experimental map-reduce unit count exceeds its cap")
        return _runtime_unit(
            ordinal=first_ordinal + len(packed),
            contract_revision_id=contract_revision_id,
            scope=scope,
            major_section_node_id=major_section_node_id,
            sources=values,
        )

    def fits(unit: _RuntimeUnit) -> bool:
        return len(_MAP_SYSTEM_PROMPT) + len(_map_user_prompt(query, unit)) <= (
            profile.maximum_input_characters
        )

    def fragment(
        source: tuple[MapSourceRef, str],
        relative_start: int,
        relative_end: int,
    ) -> tuple[MapSourceRef, str]:
        ref, text = source
        fragment_text = text[relative_start:relative_end]
        return (
            MapSourceRef(
                page=ref.page,
                source_start=ref.source_start + relative_start,
                source_end=ref.source_start + relative_end,
                text_sha256=hashlib.sha256(fragment_text.encode()).hexdigest(),
            ),
            fragment_text,
        )

    def single_fits(source: tuple[MapSourceRef, str]) -> bool:
        return fits(
            _runtime_unit(
                ordinal=first_ordinal,
                contract_revision_id=contract_revision_id,
                scope=scope,
                major_section_node_id=major_section_node_id,
                sources=(source,),
            )
        )

    def bounded_fragments(
        source: tuple[MapSourceRef, str],
    ) -> tuple[tuple[MapSourceRef, str], ...]:
        if single_fits(source):
            return (source,)
        _, text = source
        paragraph_ends = tuple(
            match.end() for match in re.finditer(r"(?:\r?\n)[ \t]*(?:\r?\n)+", text)
        )
        fragments: list[tuple[MapSourceRef, str]] = []
        cursor = 0
        while cursor < len(text):
            low = cursor + 1
            high = len(text)
            maximum_end = cursor
            while low <= high:
                midpoint = (low + high) // 2
                if single_fits(fragment(source, cursor, midpoint)):
                    maximum_end = midpoint
                    low = midpoint + 1
                else:
                    high = midpoint - 1
            if maximum_end == cursor:
                raise ExperimentalMapReduceError(
                    "canonical leaf map prompt overhead exceeds sealed profile input"
                )
            boundary_index = bisect_right(paragraph_ends, maximum_end) - 1
            selected_end = (
                paragraph_ends[boundary_index]
                if boundary_index >= 0 and paragraph_ends[boundary_index] > cursor
                else maximum_end
            )
            if first_ordinal + len(fragments) >= MAX_MAP_UNITS:
                raise ExperimentalMapReduceError(
                    "oversized canonical leaf exceeds the map-unit cap"
                )
            fragments.append(fragment(source, cursor, selected_end))
            cursor = selected_end
        return tuple(fragments)

    expanded_sources = tuple(bounded for source in sources for bounded in bounded_fragments(source))
    for source in expanded_sources:
        combined = candidate((*pending, source))
        if fits(combined):
            pending.append(source)
            continue
        if pending:
            packed.append(candidate(pending))
            pending = []
        single = candidate((source,))
        if not fits(single):
            raise ExperimentalMapReduceError("bounded map fragment exceeds sealed profile input")
        pending.append(source)
    if pending:
        packed.append(candidate(pending))
    if not packed:
        raise ExperimentalMapReduceError("major section has no canonical source refs")
    return packed


def _build_corpus_snapshot(
    handle: GenerationHandle,
    profile: ExperimentalMapReduceProfile,
    query: str,
) -> _CorpusSnapshot:
    if handle.metadata.schema_id != "cardrag.serving-db.v5":
        raise ExperimentalMapReduceError("experimental map-reduce requires an active v5 generation")
    with handle.connect() as connection:
        revision_rows = connection.execute(
            """SELECT contract_revision_id
                 FROM contract_revisions
                WHERE temporal_status IN ('current','ambiguous')
                ORDER BY contract_revision_id"""
        ).fetchall()
        if not revision_rows:
            raise ExperimentalMapReduceError("active v5 generation has no current contracts")
        runtime_units: list[_RuntimeUnit] = []
        expected: list[ExpectedContractMap] = []
        all_pages: dict[tuple[str, int], str] = {}
        for revision_row in revision_rows:
            revision_id = str(revision_row[0])
            page_rows = connection.execute(
                """SELECT page,text,text_sha256
                     FROM document_pages
                    WHERE contract_revision_id=? ORDER BY page""",
                (revision_id,),
            ).fetchall()
            pages: dict[int, str] = {}
            full_sources: list[tuple[MapSourceRef, str]] = []
            for page_row in page_rows:
                page, text, declared_sha = int(page_row[0]), str(page_row[1]), str(page_row[2])
                digest = hashlib.sha256(text.encode()).hexdigest()
                if digest != declared_sha:
                    raise ExperimentalMapReduceError("document page hash is invalid")
                pages[page] = text
                all_pages[(revision_id, page)] = text
                if text:
                    full_sources.append(
                        (
                            MapSourceRef(
                                page=page,
                                source_start=0,
                                source_end=len(text),
                                text_sha256=digest,
                            ),
                            text,
                        )
                    )
            if not full_sources:
                raise ExperimentalMapReduceError("active contract has no OCR text")
            tentative = _runtime_unit(
                ordinal=len(runtime_units),
                contract_revision_id=revision_id,
                scope="contract",
                major_section_node_id=None,
                sources=full_sources,
            )
            if len(_MAP_SYSTEM_PROMPT) + len(_map_user_prompt(query, tentative)) <= (
                profile.maximum_input_characters
            ):
                contract_units = [tentative]
            else:
                contract_units = []
                for scope, major_id, sources in _major_section_sources(
                    connection,
                    revision_id,
                    pages,
                ):
                    section_units = _pack_section_units(
                        first_ordinal=len(runtime_units) + len(contract_units),
                        contract_revision_id=revision_id,
                        scope=scope,
                        major_section_node_id=major_id,
                        sources=sources,
                        profile=profile,
                        query=query,
                    )
                    contract_units.extend(section_units)
            runtime_units.extend(contract_units)
            expected.append(
                ExpectedContractMap(
                    contract_revision_id=revision_id,
                    unit_ids=tuple(item.plan.unit_id for item in contract_units),
                )
            )
    if len(runtime_units) > MAX_MAP_UNITS:
        raise ExperimentalMapReduceError("experimental map-reduce unit count exceeds its cap")
    expected_contracts = tuple(expected)
    units = tuple(runtime_units)
    plan_sha256 = canonical_sha256(
        {
            "schema_version": CORPUS_PLAN_SCHEMA,
            "expected_contracts": expected_contracts,
            "map_units": tuple(item.plan for item in units),
        }
    )
    return _CorpusSnapshot(
        generation_id=handle.generation_id,
        expected_contracts=expected_contracts,
        units=units,
        corpus_plan_sha256=plan_sha256,
        pages=all_pages,
    )


def _parse_provider_decision(payload: bytes) -> ProviderDecision:
    try:
        parsed = _strict_json_value(payload)
        if not isinstance(parsed, dict) or not isinstance(parsed.get("spans"), list):
            raise ValueError("provider decision shape is invalid")
        strict_payload = dict(parsed)
        strict_payload["spans"] = tuple(parsed["spans"])
        value = ProviderDecision.model_validate(strict_payload)
    except (RecursionError, UnicodeError, ValueError) as exc:
        raise ExperimentalMapReduceError("reasoning provider returned invalid strict JSON") from exc
    return value


def _span_is_inside_unit(span: _ProviderSpan, unit: _RuntimeUnit) -> bool:
    return any(
        ref.page == span.page
        and ref.source_start <= span.source_start
        and span.source_end <= ref.source_end
        for ref, _ in unit.sources
    )


def _validated_spans(
    decision: ProviderDecision,
    *,
    contract_revision_id: str | None,
    pages: Mapping[tuple[str, int], str],
    profile: ExperimentalMapReduceProfile,
    unit: _RuntimeUnit | None = None,
    allowed: set[tuple[str, int, int, int, str, str]] | None = None,
) -> tuple[tuple[ExactEvidenceSpan, ...], int]:
    accepted: list[ExactEvidenceSpan] = []
    rejected = 0
    seen: set[tuple[str, int, int, int, str, str]] = set()
    if not decision.relevant:
        return (), len(decision.spans)
    for proposed in decision.spans:
        candidate_contract = (
            unit.plan.contract_revision_id if unit is not None else contract_revision_id
        )
        if candidate_contract is None:
            rejected += 1
            continue
        page_text = pages.get((candidate_contract, proposed.page))
        if (
            page_text is None
            or proposed.source_end > len(page_text)
            or proposed.source_end - proposed.source_start != len(proposed.quote)
            or not proposed.quote.strip()
            or page_text[proposed.source_start : proposed.source_end] != proposed.quote
            or proposed.contract_revision_id != candidate_contract
            or (unit is not None and not _span_is_inside_unit(proposed, unit))
        ):
            rejected += 1
            continue
        span = ExactEvidenceSpan(
            contract_revision_id=candidate_contract,
            page=proposed.page,
            source_start=proposed.source_start,
            source_end=proposed.source_end,
            text_sha256=hashlib.sha256(proposed.quote.encode()).hexdigest(),
            text=proposed.quote,
        )
        key = _span_key(span)
        if (
            key in seen
            or (allowed is not None and key not in allowed)
            or not _span_fits_hierarchical_reduce(span, profile)
        ):
            rejected += 1
            continue
        seen.add(key)
        accepted.append(span)
    return tuple(accepted), rejected


def _receipt_matches_pending(
    receipt: ProviderCallReceipt,
    pending: PendingProviderCall,
    identity: MapReduceIdentity,
    profile: ExperimentalMapReduceProfile,
) -> bool:
    return (
        receipt.job_id == identity.job_id
        and receipt.sequence == pending.sequence
        and receipt.phase == pending.phase
        and receipt.unit_id == pending.unit_id
        and receipt.round_index == pending.round_index
        and receipt.batch_index == pending.batch_index
        and receipt.request_sha256 == pending.request_sha256
        and receipt.input_characters == pending.input_characters
        and receipt.maximum_completion_tokens == pending.maximum_completion_tokens
        and receipt.model == profile.model
        and receipt.provider_id == profile.provider_id
        and receipt.profile_id == profile.profile_id
        and receipt.envelope.response_model == profile.model
        and _normalized_provider(receipt.envelope.response_provider)
        == _normalized_provider(profile.provider_id)
        and receipt.envelope.usage.advertised_output_tokens <= pending.maximum_completion_tokens
    )


def _map_result_from_receipt(
    receipt: ProviderCallReceipt,
    *,
    unit: _RuntimeUnit,
    snapshot: _CorpusSnapshot,
    profile: ExperimentalMapReduceProfile,
    receipt_sha256: str,
) -> MapUnitResult:
    spans, rejected = _validated_spans(
        receipt.decision,
        contract_revision_id=unit.plan.contract_revision_id,
        pages=snapshot.pages,
        profile=profile,
        unit=unit,
    )
    return MapUnitResult(
        unit_id=unit.plan.unit_id,
        contract_revision_id=unit.plan.contract_revision_id,
        provider_relevant=receipt.decision.relevant,
        relevant=receipt.decision.relevant and bool(spans),
        spans=spans,
        rejected_span_count=rejected,
        provider_response_sha256=receipt.raw_content_sha256,
        receipt_sha256=receipt_sha256,
    )


def _reduce_result_from_receipt(
    receipt: ProviderCallReceipt,
    *,
    pending: _PendingReduction,
    receipt_sha256: str,
) -> ReduceBatchResult:
    maximum_outputs = (
        MAX_PROVIDER_SPANS
        if pending.final_batch
        else min(MAX_PROVIDER_SPANS, max(1, len(pending.spans) // 2))
    )
    candidates: list[ExactEvidenceSpan] = []
    rejected = 0
    if receipt.decision.relevant:
        for proposed in receipt.decision.spans:
            matches = [
                span
                for span in pending.spans
                if span.contract_revision_id == proposed.contract_revision_id
                and span.page == proposed.page
                and span.source_start == proposed.source_start
                and span.source_end == proposed.source_end
                and span.text == proposed.quote
            ]
            if len(matches) != 1:
                rejected += 1
                continue
            key = _span_key(matches[0])
            if key in {_span_key(item) for item in candidates}:
                rejected += 1
                continue
            if len(candidates) >= maximum_outputs:
                rejected += 1
                continue
            candidates.append(matches[0])
    else:
        rejected = len(receipt.decision.spans)
    return ReduceBatchResult(
        round_index=pending.round_index,
        batch_index=pending.batch_index,
        input_span_sha256=_reduce_input_sha256(pending.spans),
        input_span_count=len(pending.spans),
        provider_relevant=receipt.decision.relevant,
        relevant=receipt.decision.relevant and bool(candidates),
        spans=tuple(candidates),
        rejected_span_count=rejected,
        provider_response_sha256=receipt.raw_content_sha256,
        receipt_sha256=receipt_sha256,
    )


class ExperimentalMapReduceStore:
    """Traversal-safe checkpoint store with immutable cancel/completion markers."""

    def __init__(
        self,
        state_root: Path,
        *,
        maximum_jobs: int | None = None,
        maximum_total_bytes: int | None = None,
        maximum_artifact_bytes: int | None = None,
    ) -> None:
        self._state_root = state_root.resolve()
        self._root = self._state_root / "experimental-map-reduce-jobs"
        policy = state_quota_policy(self._state_root)
        self.maximum_jobs = validate_count_limit(
            policy.exhaustive_audit_max_jobs if maximum_jobs is None else maximum_jobs,
            label="maximum experimental map-reduce jobs",
        )
        self.maximum_total_bytes = validate_byte_limit(
            (
                policy.exhaustive_audit_max_total_bytes
                if maximum_total_bytes is None
                else maximum_total_bytes
            ),
            label="maximum experimental map-reduce total bytes",
        )
        self.maximum_artifact_bytes = validate_byte_limit(
            (
                policy.exhaustive_audit_max_artifact_bytes
                if maximum_artifact_bytes is None
                else maximum_artifact_bytes
            ),
            label="maximum experimental map-reduce artifact bytes",
        )
        if self.maximum_artifact_bytes > self.maximum_total_bytes:
            raise ValueError("experimental map-reduce artifact cap exceeds total quota")

    def begin(
        self,
        identity: MapReduceIdentity,
        profile: ExperimentalMapReduceProfile,
        snapshot: _CorpusSnapshot,
    ) -> MapReduceLedger:
        ledger = MapReduceLedger(
            status="progress",
            identity=identity,
            profile=profile,
            corpus_plan_sha256=snapshot.corpus_plan_sha256,
            expected_contracts=snapshot.expected_contracts,
            map_units=tuple(item.plan for item in snapshot.units),
            completed_maps=(),
        )
        envelope = _ProgressEnvelope(
            ledger_sha256=canonical_sha256(ledger),
            ledger=ledger,
        )
        payload = envelope.canonical_bytes()
        self._validate_artifact(payload)
        existing = self._job_directory(identity, create=False)
        new_job = existing is None
        growth, peak = (
            (len(payload), len(payload))
            if existing is None
            else self._replacement_growth(existing / "progress.json", payload)
        )
        with self._write_quota_guard(
            growth,
            peak_growth_bytes=peak,
            new_job_id=identity.job_id if new_job else None,
        ):
            directory = self._job_directory(identity, create=True)
            if directory is None:  # pragma: no cover
                raise ExperimentalMapReduceError("map-reduce job directory was not created")
            self._reconcile_initial_progress_temps(directory)
            self._atomic_write(directory / "progress.json", payload)
        return ledger

    def inspect(self, job_id: str) -> LoadedMapReduceJob | None:
        """Load canonical identity/terminal state without reading a generation."""

        directory = self._job_directory_by_id(job_id, create=False)
        if directory is None:
            return None
        complete_path = directory / "COMPLETE.json"
        cancelled_path = directory / "CANCELLED.json"
        if (complete_path.exists() or complete_path.is_symlink()) and (
            cancelled_path.exists() or cancelled_path.is_symlink()
        ):
            raise ExperimentalMapReduceError("map-reduce job has conflicting terminal markers")
        if complete_path.exists() or complete_path.is_symlink():
            marker = self._read_model(complete_path, _CompleteMarker, "completion marker")
            if marker.job_id != job_id:
                raise ExperimentalMapReduceError("completion marker belongs to another job")
            artifact_path = directory / f"artifact-{marker.artifact_sha256}.json"
            payload = self._read_bytes(artifact_path, "completion artifact")
            if (
                len(payload) != marker.artifact_size_bytes
                or hashlib.sha256(payload).hexdigest() != marker.artifact_sha256
            ):
                raise ExperimentalMapReduceError("completion artifact hash or size is invalid")
            ledger = self._parse_model(payload, MapReduceLedger, "completion artifact")
            if ledger.status != "complete" or ledger.identity.job_id != job_id:
                raise ExperimentalMapReduceError("completion artifact is not terminal")
            self._verify_receipts_basic(directory, ledger)
            return LoadedMapReduceJob(
                ledger=ledger,
                resumed=True,
                artifact_sha256=marker.artifact_sha256,
            )
        if cancelled_path.exists() or cancelled_path.is_symlink():
            cancel_marker = self._read_model(cancelled_path, _CancelledMarker, "cancel marker")
            if cancel_marker.job_id != job_id:
                raise ExperimentalMapReduceError("cancel marker belongs to another job")
            artifact_path = directory / f"artifact-{cancel_marker.artifact_sha256}.json"
            payload = self._read_bytes(artifact_path, "cancel artifact")
            if (
                len(payload) != cancel_marker.artifact_size_bytes
                or hashlib.sha256(payload).hexdigest() != cancel_marker.artifact_sha256
            ):
                raise ExperimentalMapReduceError("cancel artifact hash or size is invalid")
            ledger = self._parse_model(payload, MapReduceLedger, "cancel artifact")
            if ledger.status != "cancelled" or ledger.identity.job_id != job_id:
                raise ExperimentalMapReduceError("cancel artifact is not terminal")
            self._verify_receipts_basic(directory, ledger)
            return LoadedMapReduceJob(
                ledger=ledger,
                resumed=True,
                cancelled=True,
                artifact_sha256=cancel_marker.artifact_sha256,
            )
        progress_path = directory / "progress.json"
        if not progress_path.exists() and not progress_path.is_symlink():
            self._validate_recoverable_initial_directory(directory)
            return None
        envelope = self._read_model(
            progress_path,
            _ProgressEnvelope,
            "map-reduce progress",
        )
        if envelope.ledger.identity.job_id != job_id:
            raise ExperimentalMapReduceError("progress belongs to another job")
        self._verify_receipts_basic(directory, envelope.ledger)
        return LoadedMapReduceJob(
            ledger=envelope.ledger,
            resumed=bool(envelope.ledger.provider_call_count),
        )

    def reconcile_owned_temporaries(self, job_id: str) -> None:
        """Remove only bounded writer-owned crash temporaries under a job lock."""

        directory = self._job_directory_by_id(job_id, create=False)
        if directory is None:
            return
        try:
            with state_quota_transaction(self._state_root):
                candidates: list[tuple[Path, str]] = []
                for path in sorted(directory.iterdir(), key=lambda item: item.name):
                    match = _OWNED_JOB_TEMP.fullmatch(path.name)
                    if match is not None:
                        candidates.append((path, match.group(1)))
                if len(candidates) > _MAXIMUM_OWNED_JOB_TEMPS:
                    raise ExperimentalMapReduceError(
                        "map-reduce owned temporary count exceeds its cap"
                    )
                removed = False
                for path, target_name in candidates:
                    if path.is_symlink() or not path.is_file():
                        raise ExperimentalMapReduceError("map-reduce owned temporary is unsafe")
                    metadata = path.stat(follow_symlinks=False)
                    if (
                        not stat.S_ISREG(metadata.st_mode)
                        or metadata.st_size > self.maximum_artifact_bytes
                        or metadata.st_nlink not in {1, 2}
                    ):
                        raise ExperimentalMapReduceError("map-reduce owned temporary is unsafe")
                    if metadata.st_nlink == 2:
                        target = directory / target_name
                        if target.is_symlink() or not target.is_file():
                            raise ExperimentalMapReduceError(
                                "map-reduce owned temporary target is missing or unsafe"
                            )
                        target_metadata = target.stat(follow_symlinks=False)
                        if (
                            target_metadata.st_dev,
                            target_metadata.st_ino,
                        ) != (metadata.st_dev, metadata.st_ino):
                            raise ExperimentalMapReduceError(
                                "map-reduce owned temporary target identity is stale"
                            )
                    path.unlink()
                    removed = True
                if removed:
                    self._fsync_directory(directory)
        except StorageQuotaError:
            raise ExperimentalMapReduceError(
                "MCP state quota coordination rejected temporary recovery"
            ) from None

    def discard_unstarted_job(self, job_id: str) -> bool:
        """Remove only an empty/owned-temp job that never published progress."""

        directory = self._job_directory_by_id(job_id, create=False)
        if directory is None:
            return True
        try:
            with state_quota_transaction(self._state_root):
                self._reconcile_initial_progress_temps(directory)
                if any(directory.iterdir()):
                    return False
                directory.rmdir()
                self._fsync_directory(self._root)
                return True
        except StorageQuotaError:
            raise ExperimentalMapReduceError(
                "MCP state quota coordination rejected unstarted job cleanup"
            ) from None

    def _validate_recoverable_initial_directory(self, directory: Path) -> None:
        """Accept only an empty dir or bounded owned initial-write temporaries."""

        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise ExperimentalMapReduceError(
                "initial map-reduce job directory is unreadable"
            ) from exc
        if len(entries) > _MAXIMUM_INITIAL_PROGRESS_TEMPS:
            raise ExperimentalMapReduceError("initial map-reduce temporary count exceeds its cap")
        for path in entries:
            if _INITIAL_PROGRESS_TEMP.fullmatch(path.name) is None or path.is_symlink():
                raise ExperimentalMapReduceError(
                    "map-reduce job without progress contains an unknown entry"
                )
            try:
                metadata = path.stat(follow_symlinks=False)
            except OSError as exc:
                raise ExperimentalMapReduceError(
                    "initial map-reduce temporary is unreadable"
                ) from exc
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_size > self.maximum_artifact_bytes
            ):
                raise ExperimentalMapReduceError("initial map-reduce temporary is unsafe")

    def _reconcile_initial_progress_temps(self, directory: Path) -> None:
        progress_path = directory / "progress.json"
        if progress_path.exists() or progress_path.is_symlink():
            return
        self._validate_recoverable_initial_directory(directory)
        removed = False
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            path.unlink()
            removed = True
        if removed:
            self._fsync_directory(directory)

    def load(
        self,
        identity: MapReduceIdentity,
        profile: ExperimentalMapReduceProfile,
        snapshot: _CorpusSnapshot,
        query: str,
    ) -> LoadedMapReduceJob | None:
        loaded = self.inspect(identity.job_id)
        if loaded is None:
            return None
        if loaded.ledger.identity != identity or loaded.ledger.profile != profile:
            raise ExperimentalMapReduceError("map-reduce identity or profile is stale")
        if not loaded.cancelled and loaded.artifact_sha256 is None:
            self._verify(loaded.ledger, identity, profile, snapshot, query)
        return loaded

    def reserve_call(
        self,
        identity: MapReduceIdentity,
        ledger: MapReduceLedger,
        pending: PendingProviderCall,
    ) -> MapReduceLedger:
        self._verify_identity_only(ledger, identity)
        if ledger.status != "progress" or ledger.pending_call is not None:
            raise ExperimentalMapReduceError("provider call reservation is not available")
        if pending.sequence != len(ledger.receipt_sha256s):
            raise ExperimentalMapReduceError("provider call reservation sequence is stale")
        if ledger.provider_call_count + 1 > ledger.profile.maximum_job_provider_calls:
            raise ExperimentalMapReduceError("provider call budget exhausted before client call")
        if (
            ledger.provider_input_characters + pending.input_characters
            > ledger.profile.maximum_job_input_characters
        ):
            raise ExperimentalMapReduceError(
                "provider input-character budget exhausted before client call"
            )
        if (
            ledger.accounted_output_tokens + pending.maximum_completion_tokens
            > ledger.profile.maximum_job_output_tokens
        ):
            raise ExperimentalMapReduceError(
                "provider output-token budget exhausted before client call"
            )
        next_ledger = MapReduceLedger.model_validate(
            {
                **ledger.model_dump(mode="python"),
                "provider_call_count": ledger.provider_call_count + 1,
                "provider_input_characters": (
                    ledger.provider_input_characters + pending.input_characters
                ),
                "accounted_output_tokens": (
                    ledger.accounted_output_tokens + pending.maximum_completion_tokens
                ),
                "pending_call": pending,
            }
        )
        self._write_progress(self._required_job_directory(identity), next_ledger)
        return next_ledger

    def publish_receipt(
        self,
        identity: MapReduceIdentity,
        ledger: MapReduceLedger,
        receipt: ProviderCallReceipt,
    ) -> str:
        pending = ledger.pending_call
        if pending is None or not _receipt_matches_pending(
            receipt,
            pending,
            identity,
            ledger.profile,
        ):
            raise ExperimentalMapReduceError("provider receipt differs from its reservation")
        payload = receipt.canonical_bytes()
        receipt_sha256 = hashlib.sha256(payload).hexdigest()
        directory = self._required_job_directory(identity)
        path = directory / f"receipt-{receipt.sequence:08d}.json"
        growth = self._immutable_growth(path, payload)
        with self._write_quota_guard(growth, peak_growth_bytes=2 * growth):
            self._publish_immutable(path, payload)
        return receipt_sha256

    def pending_receipt(
        self,
        identity: MapReduceIdentity,
        ledger: MapReduceLedger,
    ) -> tuple[ProviderCallReceipt, str] | None:
        pending = ledger.pending_call
        if pending is None:
            return None
        path = self._required_job_directory(identity) / f"receipt-{pending.sequence:08d}.json"
        if not path.exists() and not path.is_symlink():
            return None
        payload = self._read_bytes(path, "pending provider receipt")
        receipt = self._parse_model(payload, ProviderCallReceipt, "pending provider receipt")
        if not _receipt_matches_pending(receipt, pending, identity, ledger.profile):
            raise ExperimentalMapReduceError("pending provider receipt identity is stale")
        return receipt, hashlib.sha256(payload).hexdigest()

    def checkpoint_map(
        self,
        identity: MapReduceIdentity,
        ledger: MapReduceLedger,
        result: MapUnitResult,
        receipt: ProviderCallReceipt,
    ) -> MapReduceLedger:
        self._verify_identity_only(ledger, identity)
        pending = ledger.pending_call
        if (
            pending is None
            or result.receipt_sha256 != hashlib.sha256(receipt.canonical_bytes()).hexdigest()
        ):
            raise ExperimentalMapReduceError("map checkpoint lacks its provider receipt")
        if not _receipt_matches_pending(receipt, pending, identity, ledger.profile):
            raise ExperimentalMapReduceError("map receipt differs from its reservation")
        if len(ledger.completed_maps) >= len(ledger.map_units):
            raise ExperimentalMapReduceError("all map units are already complete")
        if result.unit_id != ledger.map_units[len(ledger.completed_maps)].unit_id:
            raise ExperimentalMapReduceError("map checkpoint is out of order")
        next_ledger = MapReduceLedger.model_validate(
            {
                **ledger.model_dump(mode="python"),
                "completed_maps": (*ledger.completed_maps, result),
                "receipt_sha256s": (*ledger.receipt_sha256s, result.receipt_sha256),
                "advertised_prompt_tokens": (
                    ledger.advertised_prompt_tokens + receipt.envelope.usage.prompt_tokens
                ),
                "advertised_completion_tokens": (
                    ledger.advertised_completion_tokens + receipt.envelope.usage.completion_tokens
                ),
                "advertised_total_tokens": (
                    ledger.advertised_total_tokens + receipt.envelope.usage.total_tokens
                ),
                "pending_call": None,
            }
        )
        self._write_progress(self._required_job_directory(identity), next_ledger)
        return next_ledger

    def checkpoint_reduce(
        self,
        identity: MapReduceIdentity,
        ledger: MapReduceLedger,
        result: ReduceBatchResult,
        receipt: ProviderCallReceipt,
    ) -> MapReduceLedger:
        self._verify_identity_only(ledger, identity)
        call = ledger.pending_call
        if (
            call is None
            or result.receipt_sha256 != hashlib.sha256(receipt.canonical_bytes()).hexdigest()
        ):
            raise ExperimentalMapReduceError("reduce checkpoint lacks its provider receipt")
        if not _receipt_matches_pending(receipt, call, identity, ledger.profile):
            raise ExperimentalMapReduceError("reduce receipt differs from its reservation")
        pending = _reduction_state(ledger)
        if not isinstance(pending, _PendingReduction):
            raise ExperimentalMapReduceError("hierarchical reduce is already terminal")
        if (
            result.round_index != pending.round_index
            or result.batch_index != pending.batch_index
            or result.input_span_count != len(pending.spans)
            or result.input_span_sha256 != _reduce_input_sha256(pending.spans)
        ):
            raise ExperimentalMapReduceError("reduce checkpoint is out of order")
        next_ledger = MapReduceLedger.model_validate(
            {
                **ledger.model_dump(mode="python"),
                "completed_reductions": (*ledger.completed_reductions, result),
                "receipt_sha256s": (*ledger.receipt_sha256s, result.receipt_sha256),
                "advertised_prompt_tokens": (
                    ledger.advertised_prompt_tokens + receipt.envelope.usage.prompt_tokens
                ),
                "advertised_completion_tokens": (
                    ledger.advertised_completion_tokens + receipt.envelope.usage.completion_tokens
                ),
                "advertised_total_tokens": (
                    ledger.advertised_total_tokens + receipt.envelope.usage.total_tokens
                ),
                "pending_call": None,
            }
        )
        # Reconstructing the frontier verifies exact-subset and shrink bounds
        # before any checkpoint bytes become durable.
        _reduction_state(next_ledger)
        self._write_progress(self._required_job_directory(identity), next_ledger)
        return next_ledger

    def complete(
        self,
        identity: MapReduceIdentity,
        ledger: MapReduceLedger,
        reduce_result: ReduceResult,
    ) -> LoadedMapReduceJob:
        directory = self._required_job_directory(identity)
        self._verify_identity_only(ledger, identity)
        terminal = _reduction_state(ledger)
        if not isinstance(terminal, _TerminalReduction) or terminal.result != reduce_result:
            raise ExperimentalMapReduceError("final reduce result differs from its checkpoints")
        complete = MapReduceLedger.model_validate(
            {
                **ledger.model_dump(mode="python"),
                "status": "complete",
                "reduce_result": reduce_result,
            }
        )
        artifact_bytes = complete.canonical_bytes()
        artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
        artifact_path = directory / f"artifact-{artifact_sha256}.json"
        marker = _CompleteMarker(
            job_id=identity.job_id,
            artifact_sha256=artifact_sha256,
            artifact_size_bytes=len(artifact_bytes),
        )
        marker_bytes = marker.canonical_bytes()
        growth = self._immutable_growth(artifact_path, artifact_bytes) + self._immutable_growth(
            directory / "COMPLETE.json", marker_bytes
        )
        with self._write_quota_guard(growth, peak_growth_bytes=2 * growth):
            self._publish_immutable(artifact_path, artifact_bytes)
            self._publish_immutable(directory / "COMPLETE.json", marker_bytes)
        return LoadedMapReduceJob(
            ledger=complete,
            resumed=True,
            artifact_sha256=artifact_sha256,
        )

    def cancel(
        self,
        identity: MapReduceIdentity,
        ledger: MapReduceLedger,
    ) -> LoadedMapReduceJob:
        directory = self._required_job_directory(identity)
        self._verify_identity_only(ledger, identity)
        cancelled = MapReduceLedger.model_validate(
            {
                **ledger.model_dump(mode="python"),
                "status": "cancelled",
            }
        )
        artifact_bytes = cancelled.canonical_bytes()
        artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
        artifact_path = directory / f"artifact-{artifact_sha256}.json"
        marker = _CancelledMarker(
            job_id=identity.job_id,
            artifact_sha256=artifact_sha256,
            artifact_size_bytes=len(artifact_bytes),
        )
        marker_bytes = marker.canonical_bytes()
        growth = self._immutable_growth(
            artifact_path,
            artifact_bytes,
        ) + self._immutable_growth(directory / "CANCELLED.json", marker_bytes)
        with self._write_quota_guard(growth, peak_growth_bytes=2 * growth):
            self._publish_immutable(artifact_path, artifact_bytes)
            self._publish_immutable(directory / "CANCELLED.json", marker_bytes)
        return LoadedMapReduceJob(
            ledger=cancelled,
            resumed=True,
            cancelled=True,
            artifact_sha256=artifact_sha256,
        )

    @staticmethod
    def _verify_identity_only(ledger: MapReduceLedger, identity: MapReduceIdentity) -> None:
        if ledger.identity != identity:
            raise ExperimentalMapReduceError("map-reduce ledger identity is stale")

    def _verify_receipts_basic(
        self,
        directory: Path,
        ledger: MapReduceLedger,
    ) -> tuple[tuple[ProviderCallReceipt, str], ...]:
        receipts: list[tuple[ProviderCallReceipt, str]] = []
        expected_receipt_files: set[str] = set()
        prompt_tokens = 0
        completion_tokens = 0
        total_tokens = 0
        input_characters = 0
        results: tuple[MapUnitResult | ReduceBatchResult, ...] = (
            *ledger.completed_maps,
            *ledger.completed_reductions,
        )
        for sequence, (declared_sha256, result) in enumerate(
            zip(ledger.receipt_sha256s, results, strict=True)
        ):
            filename = f"receipt-{sequence:08d}.json"
            expected_receipt_files.add(filename)
            payload = self._read_bytes(directory / filename, "provider receipt")
            actual_sha256 = hashlib.sha256(payload).hexdigest()
            if actual_sha256 != declared_sha256 or result.receipt_sha256 != actual_sha256:
                raise ExperimentalMapReduceError("provider receipt hash chain is stale")
            receipt = self._parse_model(payload, ProviderCallReceipt, "provider receipt")
            if (
                receipt.sequence != sequence
                or receipt.job_id != ledger.identity.job_id
                or receipt.profile_id != ledger.profile.profile_id
                or receipt.model != ledger.profile.model
                or receipt.provider_id != ledger.profile.provider_id
                or receipt.maximum_completion_tokens != ledger.profile.maximum_completion_tokens
                or receipt.envelope.response_model != ledger.profile.model
                or _normalized_provider(receipt.envelope.response_provider)
                != _normalized_provider(ledger.profile.provider_id)
                or receipt.envelope.usage.advertised_output_tokens
                > receipt.maximum_completion_tokens
                or result.provider_response_sha256 != receipt.raw_content_sha256
                or result.provider_relevant != receipt.decision.relevant
            ):
                raise ExperimentalMapReduceError("provider receipt contract is stale")
            if isinstance(result, MapUnitResult):
                if receipt.phase != "map" or receipt.unit_id != result.unit_id:
                    raise ExperimentalMapReduceError("map result receipt coordinate is stale")
                self._verify_map_result_decision_binding(result, receipt, ledger.profile)
            elif (
                receipt.phase != "reduce"
                or receipt.round_index != result.round_index
                or receipt.batch_index != result.batch_index
            ):
                raise ExperimentalMapReduceError("reduce result receipt coordinate is stale")
            input_characters += receipt.input_characters
            prompt_tokens += receipt.envelope.usage.prompt_tokens
            completion_tokens += receipt.envelope.usage.completion_tokens
            total_tokens += receipt.envelope.usage.total_tokens
            receipts.append((receipt, actual_sha256))
        pending_input = 0
        if ledger.pending_call is not None:
            pending_input = ledger.pending_call.input_characters
            filename = f"receipt-{ledger.pending_call.sequence:08d}.json"
            path = directory / filename
            if path.exists() or path.is_symlink():
                expected_receipt_files.add(filename)
                payload = self._read_bytes(path, "pending provider receipt")
                receipt = self._parse_model(
                    payload,
                    ProviderCallReceipt,
                    "pending provider receipt",
                )
                if not _receipt_matches_pending(
                    receipt,
                    ledger.pending_call,
                    ledger.identity,
                    ledger.profile,
                ):
                    raise ExperimentalMapReduceError("pending provider receipt is stale")
        for path in directory.iterdir():
            if _RECEIPT_FILE.fullmatch(path.name) and path.name not in expected_receipt_files:
                raise ExperimentalMapReduceError("job contains an unordered provider receipt")
        if (
            ledger.provider_input_characters != input_characters + pending_input
            or ledger.advertised_prompt_tokens != prompt_tokens
            or ledger.advertised_completion_tokens != completion_tokens
            or ledger.advertised_total_tokens != total_tokens
        ):
            raise ExperimentalMapReduceError("provider receipt usage counters are stale")
        shadow = ledger.model_copy(
            update={
                "status": "progress",
                "completed_reductions": (),
                "receipt_sha256s": tuple(result.receipt_sha256 for result in ledger.completed_maps),
                "pending_call": None,
                "reduce_result": None,
            }
        )
        for offset, result in enumerate(ledger.completed_reductions):
            state = _reduction_state(shadow)
            if not isinstance(state, _PendingReduction):
                raise ExperimentalMapReduceError(
                    "reduce receipt follows a terminal exact-span frontier"
                )
            receipt, receipt_sha256 = receipts[len(ledger.completed_maps) + offset]
            expected = _reduce_result_from_receipt(
                receipt,
                pending=state,
                receipt_sha256=receipt_sha256,
            )
            if result != expected:
                raise ExperimentalMapReduceError(
                    "completed reduce differs from its raw provider receipt"
                )
            shadow = shadow.model_copy(
                update={
                    "completed_reductions": (*shadow.completed_reductions, expected),
                    "receipt_sha256s": (*shadow.receipt_sha256s, receipt_sha256),
                }
            )
        if ledger.status == "complete":
            terminal = _reduction_state(shadow)
            if (
                not isinstance(terminal, _TerminalReduction)
                or terminal.result != ledger.reduce_result
            ):
                raise ExperimentalMapReduceError(
                    "terminal result differs from its raw provider receipt chain"
                )
        return tuple(receipts)

    @staticmethod
    def _verify_map_result_decision_binding(
        result: MapUnitResult,
        receipt: ProviderCallReceipt,
        profile: ExperimentalMapReduceProfile,
    ) -> None:
        if result.rejected_span_count != len(receipt.decision.spans) - len(result.spans):
            raise ExperimentalMapReduceError(
                "map result rejection count differs from raw provider receipt"
            )
        proposed = {
            (
                span.contract_revision_id,
                span.page,
                span.source_start,
                span.source_end,
                span.quote,
            )
            for span in receipt.decision.spans
        }
        for span in result.spans:
            if (
                (
                    span.contract_revision_id,
                    span.page,
                    span.source_start,
                    span.source_end,
                    span.text,
                )
                not in proposed
                or span.contract_revision_id != result.contract_revision_id
                or not span.text.strip()
                or not _span_fits_hierarchical_reduce(span, profile)
            ):
                raise ExperimentalMapReduceError(
                    "completed map contains evidence absent from its raw provider receipt"
                )

    def _verify(
        self,
        ledger: MapReduceLedger,
        identity: MapReduceIdentity,
        profile: ExperimentalMapReduceProfile,
        snapshot: _CorpusSnapshot,
        query: str,
    ) -> None:
        if (
            ledger.identity != identity
            or ledger.profile != profile
            or ledger.corpus_plan_sha256 != snapshot.corpus_plan_sha256
            or ledger.expected_contracts != snapshot.expected_contracts
            or ledger.map_units != tuple(item.plan for item in snapshot.units)
        ):
            raise ExperimentalMapReduceError("map-reduce identity or corpus plan is stale")
        directory = self._required_job_directory(identity)
        receipts = self._verify_receipts_basic(directory, ledger)
        for index, result in enumerate(ledger.completed_maps):
            receipt, receipt_sha256 = receipts[index]
            unit = snapshot.units[index]
            prepared = _prepare_map_call(profile, query, unit)
            if (
                receipt.request_sha256 != prepared.request_sha256
                or receipt.input_characters != prepared.input_characters
            ):
                raise ExperimentalMapReduceError("map receipt request hash is stale")
            expected = _map_result_from_receipt(
                receipt,
                unit=unit,
                snapshot=snapshot,
                profile=profile,
                receipt_sha256=receipt_sha256,
            )
            if result != expected:
                raise ExperimentalMapReduceError(
                    "completed map differs from its exact provider receipt"
                )
        shadow = ledger.model_copy(
            update={
                "status": "progress",
                "completed_reductions": (),
                "receipt_sha256s": tuple(result.receipt_sha256 for result in ledger.completed_maps),
                "pending_call": None,
                "reduce_result": None,
            }
        )
        receipt_offset = len(ledger.completed_maps)
        for reduce_result in ledger.completed_reductions:
            state = _reduction_state(shadow)
            if not isinstance(state, _PendingReduction):
                raise ExperimentalMapReduceError("reduce receipt follows a terminal frontier")
            maximum_outputs = (
                MAX_PROVIDER_SPANS
                if state.final_batch
                else min(MAX_PROVIDER_SPANS, max(1, len(state.spans) // 2))
            )
            receipt, receipt_sha256 = receipts[receipt_offset]
            prepared = _prepare_reduce_call(
                profile,
                query,
                state.spans,
                maximum_outputs,
            )
            if (
                receipt.request_sha256 != prepared.request_sha256
                or receipt.input_characters != prepared.input_characters
            ):
                raise ExperimentalMapReduceError("reduce receipt request hash is stale")
            expected_reduce = _reduce_result_from_receipt(
                receipt,
                pending=state,
                receipt_sha256=receipt_sha256,
            )
            if reduce_result != expected_reduce:
                raise ExperimentalMapReduceError(
                    "completed reduce differs from its exact provider receipt"
                )
            shadow = shadow.model_copy(
                update={
                    "completed_reductions": (*shadow.completed_reductions, expected_reduce),
                    "receipt_sha256s": (*shadow.receipt_sha256s, receipt_sha256),
                }
            )
            receipt_offset += 1
        pending = ledger.pending_call
        if pending is not None:
            expected_coordinates: tuple[str, str | None, int | None, int | None]
            if len(ledger.completed_maps) < len(snapshot.units):
                unit = snapshot.units[len(ledger.completed_maps)]
                prepared = _prepare_map_call(profile, query, unit)
                expected_coordinates = ("map", unit.plan.unit_id, None, None)
            else:
                state = _reduction_state(shadow)
                if not isinstance(state, _PendingReduction):
                    raise ExperimentalMapReduceError("pending call follows terminal reduce")
                maximum_outputs = (
                    MAX_PROVIDER_SPANS
                    if state.final_batch
                    else min(MAX_PROVIDER_SPANS, max(1, len(state.spans) // 2))
                )
                prepared = _prepare_reduce_call(
                    profile,
                    query,
                    state.spans,
                    maximum_outputs,
                )
                expected_coordinates = (
                    "reduce",
                    None,
                    state.round_index,
                    state.batch_index,
                )
            if (
                (
                    pending.phase,
                    pending.unit_id,
                    pending.round_index,
                    pending.batch_index,
                )
                != expected_coordinates
                or pending.request_sha256 != prepared.request_sha256
                or pending.input_characters != prepared.input_characters
            ):
                raise ExperimentalMapReduceError("pending provider request is stale")
        if len(ledger.completed_maps) == len(ledger.map_units):
            reduction = _reduction_state(ledger)
            if ledger.status == "complete" and (
                not isinstance(reduction, _TerminalReduction)
                or reduction.result != ledger.reduce_result
            ):
                raise ExperimentalMapReduceError(
                    "final reduce result differs from the resumable reduction chain"
                )

    def _write_progress(self, directory: Path, ledger: MapReduceLedger) -> None:
        envelope = _ProgressEnvelope(
            ledger_sha256=canonical_sha256(ledger),
            ledger=ledger,
        )
        payload = envelope.canonical_bytes()
        growth, peak = self._replacement_growth(directory / "progress.json", payload)
        with self._write_quota_guard(growth, peak_growth_bytes=peak):
            self._atomic_write(directory / "progress.json", payload)

    def _ensure_root(self) -> Path:
        if self._state_root.is_symlink() or not self._state_root.is_dir():
            raise ExperimentalMapReduceError("MCP state root is unsafe")
        if self._root.is_symlink():
            raise ExperimentalMapReduceError("map-reduce root must not be a symlink")
        created = not self._root.exists()
        self._root.mkdir(mode=0o700, parents=False, exist_ok=True)
        if created:
            self._fsync_directory(self._state_root)
        if not self._root.is_dir() or self._root.resolve(strict=True).parent != self._state_root:
            raise ExperimentalMapReduceError("map-reduce root escaped MCP state")
        return self._root.resolve(strict=True)

    def _job_directory(
        self,
        identity: MapReduceIdentity,
        *,
        create: bool,
    ) -> Path | None:
        return self._job_directory_by_id(identity.job_id, create=create)

    def _job_directory_by_id(
        self,
        job_id: str,
        *,
        create: bool,
    ) -> Path | None:
        if _JOB_ID.fullmatch(job_id) is None:
            raise ExperimentalMapReduceError("map-reduce job ID is unsafe")
        if not self._root.exists() and not self._root.is_symlink():
            if not create:
                return None
        root = self._ensure_root()
        candidate = root / job_id
        if candidate.is_symlink():
            raise ExperimentalMapReduceError("map-reduce job directory must not be a symlink")
        if create:
            created = not candidate.exists()
            candidate.mkdir(mode=0o700, exist_ok=True)
            if created:
                self._fsync_directory(root)
        elif not candidate.exists():
            return None
        if not candidate.is_dir() or candidate.resolve(strict=True).parent != root:
            raise ExperimentalMapReduceError("map-reduce job directory escaped its root")
        return candidate.resolve(strict=True)

    def _required_job_directory(self, identity: MapReduceIdentity) -> Path:
        directory = self._job_directory(identity, create=False)
        if directory is None:
            raise ExperimentalMapReduceError("map-reduce job does not exist")
        return directory

    @contextmanager
    def _write_quota_guard(
        self,
        logical_growth_bytes: int,
        *,
        peak_growth_bytes: int | None = None,
        new_job_id: str | None = None,
    ) -> Iterator[None]:
        peak = logical_growth_bytes if peak_growth_bytes is None else peak_growth_bytes
        try:
            with state_quota_guard(
                self._state_root,
                logical_growth_bytes,
                peak_growth_bytes=peak,
            ):
                total, jobs = safe_shared_exhaustive_audit_usage(
                    self._state_root,
                    prospective_map_job_id=new_job_id,
                )
                if (
                    total > self.maximum_total_bytes
                    or logical_growth_bytes > self.maximum_total_bytes - total
                    or peak > self.maximum_total_bytes - total
                ):
                    raise ExperimentalMapReduceError(
                        "experimental map-reduce total quota rejected this write"
                    )
                if jobs > self.maximum_jobs:
                    raise ExperimentalMapReduceError(
                        "experimental map-reduce job quota rejected this query"
                    )
                yield
        except StorageQuotaError:
            raise ExperimentalMapReduceError(
                "MCP state quota rejected experimental map-reduce write"
            ) from None

    def _validate_artifact(self, payload: bytes) -> None:
        if len(payload) < 1 or len(payload) > self.maximum_artifact_bytes:
            raise ExperimentalMapReduceError(
                "experimental map-reduce artifact exceeds its configured cap"
            )

    def _immutable_growth(self, path: Path, payload: bytes) -> int:
        self._validate_artifact(payload)
        if path.is_symlink():
            raise ExperimentalMapReduceError("immutable map-reduce file must not be a symlink")
        if not path.exists():
            return len(payload)
        existing = self._read_bytes(path, "immutable map-reduce file")
        if existing != payload:
            raise ExperimentalMapReduceError(
                "immutable map-reduce file already exists with other bytes"
            )
        return 0

    def _replacement_growth(self, path: Path, payload: bytes) -> tuple[int, int]:
        self._validate_artifact(payload)
        if path.is_symlink():
            raise ExperimentalMapReduceError("map-reduce progress must not be a symlink")
        if not path.exists():
            return len(payload), len(payload)
        if not path.is_file():
            raise ExperimentalMapReduceError("map-reduce progress is not a regular file")
        size = path.stat().st_size
        return max(0, len(payload) - size), len(payload)

    @staticmethod
    def _atomic_write(path: Path, payload: bytes) -> None:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(payload)
                output.flush()
                os.fchmod(output.fileno(), 0o600)
                os.fsync(output.fileno())
            os.replace(temporary, path)
            ExperimentalMapReduceStore._fsync_directory(path.parent)
        finally:
            if temporary.exists() or temporary.is_symlink():
                temporary.unlink()
                ExperimentalMapReduceStore._fsync_directory(path.parent)

    @staticmethod
    def _publish_immutable(path: Path, payload: bytes) -> None:
        if path.exists() or path.is_symlink():
            existing = ExperimentalMapReduceStore._read_bytes(
                path,
                "immutable map-reduce file",
            )
            if existing != payload:
                raise ExperimentalMapReduceError(
                    "immutable map-reduce file already exists with other bytes"
                )
            return
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            dir=path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(payload)
                output.flush()
                os.fchmod(output.fileno(), 0o400)
                os.fsync(output.fileno())
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError:
                existing = ExperimentalMapReduceStore._read_bytes(
                    path,
                    "immutable map-reduce file",
                )
                if existing != payload:
                    raise ExperimentalMapReduceError(
                        "immutable map-reduce file already exists with other bytes"
                    ) from None
            else:
                ExperimentalMapReduceStore._fsync_directory(path.parent)
        finally:
            if temporary.exists() or temporary.is_symlink():
                temporary.unlink()
                ExperimentalMapReduceStore._fsync_directory(path.parent)

    @staticmethod
    def _read_bytes(path: Path, label: str) -> bytes:
        if path.is_symlink() or not path.is_file():
            raise ExperimentalMapReduceError(f"{label} is missing or unsafe")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise ExperimentalMapReduceError(f"{label} is unreadable") from exc
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_size < 1
                or before.st_size > (MAX_LEDGER_BYTES)
            ):
                raise ExperimentalMapReduceError(f"{label} has an invalid size or type")
            payload = os.read(descriptor, before.st_size + 1)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if len(payload) != before.st_size or (
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ExperimentalMapReduceError(f"{label} changed while being read")
        return payload

    @staticmethod
    def _parse_model(payload: bytes, model: type[ModelT], label: str) -> ModelT:
        try:
            value = model.model_validate_json(payload)
        except Exception as exc:
            raise ExperimentalMapReduceError(f"{label} is not strict JSON") from exc
        if value.canonical_bytes() != payload:
            raise ExperimentalMapReduceError(f"{label} is not canonical JSON")
        return value

    @staticmethod
    def _read_model(path: Path, model: type[ModelT], label: str) -> ModelT:
        return ExperimentalMapReduceStore._parse_model(
            ExperimentalMapReduceStore._read_bytes(path, label),
            model,
            label,
        )

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


ModelT = TypeVar("ModelT", bound=_StrictModel)


@dataclass(slots=True)
class _ProviderCoordinationLease:
    descriptor: int
    released: bool = False

    def release(self) -> None:
        if self.released:
            return
        try:
            fcntl.flock(self.descriptor, fcntl.LOCK_UN)
        finally:
            os.close(self.descriptor)
            self.released = True


class _ProviderCoordination:
    """Cross-process job serialization and bounded provider-call leases."""

    _JOB_LOCK_SLOTS = 1_024

    def __init__(self, state_root: Path, maximum_provider_calls: int) -> None:
        self._state_root = state_root.resolve(strict=True)
        self._root = self._state_root / "experimental-map-reduce-coordination"
        self._maximum_provider_calls = maximum_provider_calls
        self._policy = _ProviderConcurrencyPolicy(
            maximum_concurrent_provider_calls=maximum_provider_calls,
        )
        self._initialize()

    def _initialize(self) -> None:
        root = self._ensure_root()
        self._reconcile_policy_temporaries(root)
        policy_path = root / "policy.json"
        payload = self._policy.canonical_bytes()
        if policy_path.exists() or policy_path.is_symlink():
            self._verify_policy()
            return
        try:
            with state_quota_guard(
                self._state_root,
                len(payload),
                peak_growth_bytes=2 * len(payload),
            ):
                quota_policy = state_quota_policy(self._state_root)
                shared_total, _ = safe_shared_exhaustive_audit_usage(self._state_root)
                if (
                    shared_total > quota_policy.exhaustive_audit_max_total_bytes
                    or 2 * len(payload)
                    > quota_policy.exhaustive_audit_max_total_bytes - shared_total
                ):
                    raise ExperimentalMapReduceError(
                        "shared exhaustive audit total quota rejected provider policy"
                    )
                try:
                    ExperimentalMapReduceStore._publish_immutable(policy_path, payload)
                except ExperimentalMapReduceError:
                    # Another process may have won the exclusive creation race.
                    self._verify_policy()
                    return
        except StorageQuotaError:
            raise ExperimentalMapReduceError(
                "MCP state quota rejected provider coordination policy"
            ) from None
        self._verify_policy()

    def _reconcile_policy_temporaries(self, root: Path) -> None:
        try:
            with state_quota_transaction(self._state_root):
                candidates = tuple(
                    path
                    for path in sorted(root.iterdir(), key=lambda item: item.name)
                    if _PROVIDER_POLICY_TEMP.fullmatch(path.name)
                )
                if len(candidates) > 8:
                    raise ExperimentalMapReduceError(
                        "provider coordination temporary count exceeds its cap"
                    )
                removed = False
                for path in candidates:
                    if path.is_symlink() or not path.is_file():
                        raise ExperimentalMapReduceError(
                            "provider coordination temporary is unsafe"
                        )
                    metadata = path.stat(follow_symlinks=False)
                    if (
                        not stat.S_ISREG(metadata.st_mode)
                        or metadata.st_size > len(self._policy.canonical_bytes())
                        or metadata.st_nlink not in {1, 2}
                    ):
                        raise ExperimentalMapReduceError(
                            "provider coordination temporary is unsafe"
                        )
                    if metadata.st_nlink == 2:
                        target = root / "policy.json"
                        if target.is_symlink() or not target.is_file():
                            raise ExperimentalMapReduceError(
                                "provider coordination temporary target is unsafe"
                            )
                        target_metadata = target.stat(follow_symlinks=False)
                        if (
                            target_metadata.st_dev,
                            target_metadata.st_ino,
                        ) != (metadata.st_dev, metadata.st_ino):
                            raise ExperimentalMapReduceError(
                                "provider coordination temporary target is stale"
                            )
                        policy = ExperimentalMapReduceStore._read_model(
                            target,
                            _ProviderConcurrencyPolicy,
                            "provider coordination policy",
                        )
                        if policy != self._policy:
                            raise ExperimentalMapReduceError(
                                "provider concurrency differs from durable policy"
                            )
                    path.unlink()
                    removed = True
                if removed:
                    ExperimentalMapReduceStore._fsync_directory(root)
        except StorageQuotaError:
            raise ExperimentalMapReduceError(
                "MCP state quota coordination rejected provider temp recovery"
            ) from None

    def _ensure_root(self) -> Path:
        if self._state_root.is_symlink() or not self._state_root.is_dir():
            raise ExperimentalMapReduceError("MCP state root is unsafe")
        if self._root.is_symlink():
            raise ExperimentalMapReduceError("provider coordination root is unsafe")
        created = not self._root.exists()
        self._root.mkdir(mode=0o700, parents=False, exist_ok=True)
        if created:
            ExperimentalMapReduceStore._fsync_directory(self._state_root)
        if not self._root.is_dir() or self._root.resolve(strict=True).parent != self._state_root:
            raise ExperimentalMapReduceError("provider coordination root escaped MCP state")
        return self._root.resolve(strict=True)

    def _verify_policy(self) -> None:
        policy = ExperimentalMapReduceStore._read_model(
            self._ensure_root() / "policy.json",
            _ProviderConcurrencyPolicy,
            "provider coordination policy",
        )
        if policy != self._policy:
            raise ExperimentalMapReduceError(
                "provider concurrency differs from the durable global policy"
            )

    def _try_slot(self, name: str) -> _ProviderCoordinationLease | None:
        self._verify_policy()
        root = self._ensure_root()
        path = root / name
        if path.is_symlink():
            raise ExperimentalMapReduceError("provider coordination slot is unsafe")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
        except OSError as exc:
            raise ExperimentalMapReduceError("provider coordination slot is unreadable") from exc
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_size != 0
                or opened.st_nlink != 1
                or opened.st_uid != os.geteuid()
                or opened.st_mode & 0o077
            ):
                raise ExperimentalMapReduceError("provider coordination slot is invalid")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(descriptor)
                return None
            visible = path.stat(follow_symlinks=False)
            if not stat.S_ISREG(visible.st_mode) or (visible.st_dev, visible.st_ino) != (
                opened.st_dev,
                opened.st_ino,
            ):
                raise ExperimentalMapReduceError(
                    "provider coordination slot changed during acquisition"
                )
            return _ProviderCoordinationLease(descriptor=descriptor)
        except BaseException:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
            raise

    async def acquire_provider(self) -> _ProviderCoordinationLease:
        while True:
            for index in range(self._maximum_provider_calls):
                lease = self._try_slot(f"provider-{index:02d}.lock")
                if lease is not None:
                    return lease
            await asyncio.sleep(0.01)

    async def acquire_job(self, job_id: str) -> _ProviderCoordinationLease:
        if _JOB_ID.fullmatch(job_id) is None:
            raise ExperimentalMapReduceError("map-reduce job ID is unsafe")
        index = int(hashlib.sha256(job_id.encode()).hexdigest()[:8], 16) % (self._JOB_LOCK_SLOTS)
        while True:
            lease = self._try_slot(f"job-{index:04d}.lock")
            if lease is not None:
                return lease
            await asyncio.sleep(0.01)


class ExperimentalMapReduceLane:
    """Start/poll one durable job with at most one bounded provider call."""

    def __init__(
        self,
        store: GenerationStore,
        provider: ExperimentalReasoningProvider,
        profile: ExperimentalMapReduceProfile,
        *,
        maximum_jobs: int | None = None,
        maximum_total_bytes: int | None = None,
        maximum_artifact_bytes: int | None = None,
        maximum_concurrent_provider_calls: int = 1,
    ) -> None:
        if (
            type(maximum_concurrent_provider_calls) is not int
            or not 1 <= maximum_concurrent_provider_calls <= 32
        ):
            raise ValueError("experimental provider concurrency must be between 1 and 32")
        self.generation_store = store
        self.provider = provider
        self.profile = profile
        self.job_store = ExperimentalMapReduceStore(
            store.root,
            maximum_jobs=maximum_jobs,
            maximum_total_bytes=maximum_total_bytes,
            maximum_artifact_bytes=maximum_artifact_bytes,
        )
        self._locks: tuple[asyncio.Lock, ...] = tuple(
            asyncio.Lock() for _ in range(_LOCAL_JOB_LOCK_STRIPES)
        )
        self._provider_semaphore = asyncio.Semaphore(maximum_concurrent_provider_calls)
        self._coordination = _ProviderCoordination(
            store.root,
            maximum_concurrent_provider_calls,
        )

    def _job_lock(self, job_id: str) -> asyncio.Lock:
        index = int(hashlib.sha256(job_id.encode()).hexdigest()[:8], 16) % len(self._locks)
        return self._locks[index]

    async def close(self) -> None:
        await self.provider.close()

    async def run(
        self,
        query: str,
        *,
        action: Literal["start", "poll", "cancel"] = "start",
        job_id: str | None = None,
    ) -> ExperimentalMapReduceStatus:
        if action not in {"start", "poll", "cancel"}:
            raise ExperimentalMapReduceError("experimental map-reduce action is invalid")
        if action == "start":
            if job_id is not None:
                raise ExperimentalMapReduceError("start must not include a map-reduce job ID")
        elif job_id is None or _JOB_ID.fullmatch(job_id) is None:
            raise ExperimentalMapReduceError("poll/cancel requires a valid map-reduce job ID")
        normalized_query = _query(query)
        query_sha256 = hashlib.sha256(normalized_query.encode()).hexdigest()
        if action == "start":
            return await self._start(normalized_query, query_sha256)
        if job_id is None:  # guarded above; keeps the runtime boundary explicit
            raise ExperimentalMapReduceError("poll/cancel requires a map-reduce job ID")
        return await self._resume(job_id, normalized_query, query_sha256, action)

    async def _start(
        self,
        query: str,
        query_sha256: str,
    ) -> ExperimentalMapReduceStatus:
        def owner_for_generation(generation_id: str) -> str:
            return _identity(
                generation_id,
                query_sha256,
                self.profile.profile_id,
            ).job_id

        try:
            job_id, generation_id, _ = await _cancellation_fenced_to_thread(
                self.generation_store.claim_current_generation_gc_root,
                owner_for_generation,
            )
        except RuntimeError as exc:
            raise ExperimentalMapReduceError(str(exc)) from exc
        identity = _identity(generation_id, query_sha256, self.profile.profile_id)
        if identity.job_id != job_id:  # pragma: no cover - store validates callback identity
            raise ExperimentalMapReduceError("claimed map-reduce identity is inconsistent")
        async with self._job_lock(identity.job_id):
            lease = await self._coordination.acquire_job(identity.job_id)
            try:
                try:
                    with self.generation_store.pin_generation(generation_id) as handle:
                        return await self._start_locked(handle, identity, query)
                except BaseException:
                    await _cancellation_fenced_to_thread(
                        self._release_unstarted_claim,
                        identity,
                    )
                    raise
            finally:
                lease.release()

    def _release_unstarted_claim(self, identity: MapReduceIdentity) -> None:
        inspected = self.job_store.inspect(identity.job_id)
        if inspected is not None:
            return
        if not self.job_store.discard_unstarted_job(identity.job_id):
            return
        self.generation_store.release_generation_gc_root(
            identity.job_id,
            identity.generation_id,
        )

    async def _start_locked(
        self,
        handle: GenerationHandle,
        identity: MapReduceIdentity,
        query: str,
    ) -> ExperimentalMapReduceStatus:
        await _cancellation_fenced_to_thread(
            self.job_store.reconcile_owned_temporaries,
            identity.job_id,
        )
        inspected = await _cancellation_fenced_to_thread(
            self.job_store.inspect,
            identity.job_id,
        )
        self._verify_requested_identity(inspected, identity)
        if inspected is not None and (inspected.cancelled or inspected.ledger.status == "complete"):
            await _cancellation_fenced_to_thread(
                self.generation_store.release_generation_gc_root,
                identity.job_id,
                identity.generation_id,
            )
            return self._status(inspected)
        snapshot = await _cancellation_fenced_to_thread(
            _build_corpus_snapshot,
            handle,
            self.profile,
            query,
        )
        if inspected is None:
            self._verify_minimum_job_budget(snapshot, query)
            ledger = await _cancellation_fenced_to_thread(
                self._begin_with_generation_root,
                identity,
                snapshot,
            )
            loaded = LoadedMapReduceJob(ledger=ledger, resumed=False)
        else:
            await _cancellation_fenced_to_thread(
                self.generation_store.verify_generation_gc_root,
                identity.job_id,
                identity.generation_id,
            )
            maybe_loaded = await _cancellation_fenced_to_thread(
                self.job_store.load,
                identity,
                self.profile,
                snapshot,
                query,
            )
            if maybe_loaded is None:  # pragma: no cover - inspected above
                raise ExperimentalMapReduceError("map-reduce job disappeared")
            loaded = maybe_loaded
        return await self._advance(identity, loaded, snapshot, query)

    def _begin_with_generation_root(
        self,
        identity: MapReduceIdentity,
        snapshot: _CorpusSnapshot,
    ) -> MapReduceLedger:
        existing_generation = self.generation_store.generation_for_gc_root(identity.job_id)
        if existing_generation != identity.generation_id:
            raise ExperimentalMapReduceError("map-reduce generation GC root identity is stale")
        return self.job_store.begin(identity, self.profile, snapshot)

    async def _resume(
        self,
        job_id: str,
        query: str,
        query_sha256: str,
        action: Literal["poll", "cancel"],
    ) -> ExperimentalMapReduceStatus:
        async with self._job_lock(job_id):
            lease = await self._coordination.acquire_job(job_id)
            try:
                return await self._resume_locked(job_id, query, query_sha256, action)
            finally:
                lease.release()

    async def _resume_locked(
        self,
        job_id: str,
        query: str,
        query_sha256: str,
        action: Literal["poll", "cancel"],
    ) -> ExperimentalMapReduceStatus:
        await _cancellation_fenced_to_thread(
            self.job_store.reconcile_owned_temporaries,
            job_id,
        )
        inspected = await _cancellation_fenced_to_thread(self.job_store.inspect, job_id)
        if inspected is None:
            generation_id = await _cancellation_fenced_to_thread(
                self.generation_store.generation_for_gc_root,
                job_id,
            )
            if generation_id is None:
                raise ExperimentalMapReduceError("map-reduce job does not exist")
            identity = _identity(
                generation_id,
                query_sha256,
                self.profile.profile_id,
            )
            if identity.job_id != job_id:
                raise ExperimentalMapReduceError("job ID, query, generation, or profile mismatch")
        else:
            identity = inspected.ledger.identity
            expected = _identity(
                identity.generation_id,
                query_sha256,
                self.profile.profile_id,
            )
            self._verify_requested_identity(inspected, expected)
            if inspected.cancelled or inspected.ledger.status == "complete":
                await _cancellation_fenced_to_thread(
                    self.generation_store.release_generation_gc_root,
                    identity.job_id,
                    identity.generation_id,
                )
                return self._status(inspected)
        try:
            pin = self.generation_store.pin_generation(identity.generation_id)
            with pin as handle:
                await _cancellation_fenced_to_thread(
                    self.generation_store.verify_generation_gc_root,
                    identity.job_id,
                    identity.generation_id,
                )
                snapshot = await _cancellation_fenced_to_thread(
                    _build_corpus_snapshot,
                    handle,
                    self.profile,
                    query,
                )
                if inspected is None:
                    self._verify_minimum_job_budget(snapshot, query)
                    ledger = await _cancellation_fenced_to_thread(
                        self.job_store.begin,
                        identity,
                        self.profile,
                        snapshot,
                    )
                    loaded = LoadedMapReduceJob(ledger=ledger, resumed=True)
                else:
                    maybe_loaded = await _cancellation_fenced_to_thread(
                        self.job_store.load,
                        identity,
                        self.profile,
                        snapshot,
                        query,
                    )
                    if maybe_loaded is None:  # pragma: no cover - inspected above
                        raise ExperimentalMapReduceError("map-reduce job disappeared")
                    loaded = maybe_loaded
                if action == "cancel":
                    loaded = await self._recover_pending_receipt(
                        identity,
                        loaded,
                        snapshot,
                    )
                    cancelled = await _cancellation_fenced_to_thread(
                        self.job_store.cancel,
                        identity,
                        loaded.ledger,
                    )
                    await _cancellation_fenced_to_thread(
                        self.generation_store.release_generation_gc_root,
                        identity.job_id,
                        identity.generation_id,
                    )
                    return self._status(cancelled)
                return await self._advance(identity, loaded, snapshot, query)
        except ExperimentalMapReduceError:
            raise
        except RuntimeError as exc:
            raise ExperimentalMapReduceError(
                "bound map-reduce generation is missing or unsafe"
            ) from exc

    @staticmethod
    def _verify_requested_identity(
        loaded: LoadedMapReduceJob | None,
        expected: MapReduceIdentity,
    ) -> None:
        if loaded is not None and (
            loaded.ledger.identity != expected
            or loaded.ledger.profile.profile_id != expected.profile_id
        ):
            raise ExperimentalMapReduceError("job ID, query, generation, or profile mismatch")

    def _verify_minimum_job_budget(self, snapshot: _CorpusSnapshot, query: str) -> None:
        calls = len(snapshot.units)
        input_characters = sum(
            _prepare_map_call(self.profile, query, unit).input_characters for unit in snapshot.units
        )
        if calls > self.profile.maximum_job_provider_calls:
            raise ExperimentalMapReduceError("map plan exceeds sealed provider-call budget")
        if input_characters > self.profile.maximum_job_input_characters:
            raise ExperimentalMapReduceError("map plan exceeds sealed input-character budget")
        if calls * self.profile.maximum_completion_tokens > self.profile.maximum_job_output_tokens:
            raise ExperimentalMapReduceError("map plan exceeds sealed output-token budget")

    def _next_call(
        self,
        ledger: MapReduceLedger,
        snapshot: _CorpusSnapshot,
        query: str,
    ) -> tuple[PreparedProviderCall, PendingProviderCall] | None:
        sequence = len(ledger.receipt_sha256s)
        if len(ledger.completed_maps) < len(snapshot.units):
            unit = snapshot.units[len(ledger.completed_maps)]
            prepared = _prepare_map_call(self.profile, query, unit)
            pending = PendingProviderCall(
                sequence=sequence,
                phase="map",
                unit_id=unit.plan.unit_id,
                request_sha256=prepared.request_sha256,
                input_characters=prepared.input_characters,
                maximum_completion_tokens=prepared.maximum_completion_tokens,
            )
            return prepared, pending
        reduction = _reduction_state(ledger)
        if isinstance(reduction, _TerminalReduction):
            return None
        maximum_outputs = (
            MAX_PROVIDER_SPANS
            if reduction.final_batch
            else min(MAX_PROVIDER_SPANS, max(1, len(reduction.spans) // 2))
        )
        prepared = _prepare_reduce_call(
            self.profile,
            query,
            reduction.spans,
            maximum_outputs,
        )
        pending = PendingProviderCall(
            sequence=sequence,
            phase="reduce",
            round_index=reduction.round_index,
            batch_index=reduction.batch_index,
            request_sha256=prepared.request_sha256,
            input_characters=prepared.input_characters,
            maximum_completion_tokens=prepared.maximum_completion_tokens,
        )
        return prepared, pending

    async def _advance(
        self,
        identity: MapReduceIdentity,
        loaded: LoadedMapReduceJob,
        snapshot: _CorpusSnapshot,
        query: str,
    ) -> ExperimentalMapReduceStatus:
        recovered = await self._recover_pending_receipt(identity, loaded, snapshot)
        ledger = recovered.ledger
        if loaded.ledger.pending_call is not None and ledger.pending_call is None:
            return await self._finalize_or_status(identity, ledger, resumed=True)
        if ledger.pending_call is not None:
            # The full completion-token reservation remains permanently charged.
            # Returning the durable pending identity makes the only safe next
            # action (cancel) available without ever repeating the provider call.
            return self._status(recovered)
        next_call = self._next_call(ledger, snapshot, query)
        if next_call is None:
            return await self._complete_and_release(identity, ledger)
        prepared, pending = next_call
        async with self._provider_semaphore:
            provider_lease = await self._coordination.acquire_provider()
            try:
                ledger = await _cancellation_fenced_to_thread(
                    self.job_store.reserve_call,
                    identity,
                    ledger,
                    pending,
                )
                try:
                    completion = await self.provider.complete(prepared)
                except Exception:
                    # The durable reservation makes every provider exception an
                    # ambiguous, permanently charged attempt.  Returning the
                    # public identity lets the caller cancel without a retry.
                    return self._status(LoadedMapReduceJob(ledger=ledger, resumed=True))
            finally:
                provider_lease.release()
        try:
            decision = _parse_provider_decision(completion.content)
            receipt = ProviderCallReceipt(
                job_id=identity.job_id,
                sequence=pending.sequence,
                phase=pending.phase,
                unit_id=pending.unit_id,
                round_index=pending.round_index,
                batch_index=pending.batch_index,
                request_sha256=pending.request_sha256,
                input_characters=pending.input_characters,
                maximum_completion_tokens=pending.maximum_completion_tokens,
                model=self.profile.model,
                provider_id=self.profile.provider_id,
                profile_id=self.profile.profile_id,
                raw_content_sha256=hashlib.sha256(completion.content).hexdigest(),
                raw_content_base64=base64.b64encode(completion.content).decode("ascii"),
                parsed_decision_sha256=canonical_sha256(decision),
                decision=decision,
                envelope=completion.envelope,
            )
            receipt_sha256 = await _cancellation_fenced_to_thread(
                self.job_store.publish_receipt,
                identity,
                ledger,
                receipt,
            )
            ledger = await self._commit_receipt(
                identity,
                ledger,
                snapshot,
                receipt,
                receipt_sha256,
            )
        except Exception:
            # No post-reservation failure may strand the caller without its
            # cancel capability.  A durable receipt, if publication won the
            # crash race, is recovered and committed by the next poll.
            return self._status(LoadedMapReduceJob(ledger=ledger, resumed=True))
        return await self._finalize_or_status(identity, ledger, recovered.resumed)

    async def _recover_pending_receipt(
        self,
        identity: MapReduceIdentity,
        loaded: LoadedMapReduceJob,
        snapshot: _CorpusSnapshot,
    ) -> LoadedMapReduceJob:
        if loaded.ledger.pending_call is None:
            return loaded
        pending = await _cancellation_fenced_to_thread(
            self.job_store.pending_receipt,
            identity,
            loaded.ledger,
        )
        if pending is None:
            return loaded
        receipt, receipt_sha256 = pending
        ledger = await self._commit_receipt(
            identity,
            loaded.ledger,
            snapshot,
            receipt,
            receipt_sha256,
        )
        return LoadedMapReduceJob(ledger=ledger, resumed=True)

    async def _commit_receipt(
        self,
        identity: MapReduceIdentity,
        ledger: MapReduceLedger,
        snapshot: _CorpusSnapshot,
        receipt: ProviderCallReceipt,
        receipt_sha256: str,
    ) -> MapReduceLedger:
        pending = ledger.pending_call
        if pending is None:
            raise ExperimentalMapReduceError("provider receipt has no durable reservation")
        if pending.phase == "map":
            unit = snapshot.units[len(ledger.completed_maps)]
            map_result = _map_result_from_receipt(
                receipt,
                unit=unit,
                snapshot=snapshot,
                profile=self.profile,
                receipt_sha256=receipt_sha256,
            )
            return await _cancellation_fenced_to_thread(
                self.job_store.checkpoint_map,
                identity,
                ledger,
                map_result,
                receipt,
            )
        reduction = _reduction_state(ledger)
        if not isinstance(reduction, _PendingReduction):
            raise ExperimentalMapReduceError("reduce receipt follows terminal state")
        reduce_result = _reduce_result_from_receipt(
            receipt,
            pending=reduction,
            receipt_sha256=receipt_sha256,
        )
        return await _cancellation_fenced_to_thread(
            self.job_store.checkpoint_reduce,
            identity,
            ledger,
            reduce_result,
            receipt,
        )

    async def _finalize_or_status(
        self,
        identity: MapReduceIdentity,
        ledger: MapReduceLedger,
        resumed: bool,
    ) -> ExperimentalMapReduceStatus:
        if len(ledger.completed_maps) == len(ledger.map_units):
            reduction = _reduction_state(ledger)
            if isinstance(reduction, _TerminalReduction):
                return await self._complete_and_release(identity, ledger)
        return self._status(LoadedMapReduceJob(ledger=ledger, resumed=resumed))

    async def _complete_and_release(
        self,
        identity: MapReduceIdentity,
        ledger: MapReduceLedger,
    ) -> ExperimentalMapReduceStatus:
        reduction = _reduction_state(ledger)
        if not isinstance(reduction, _TerminalReduction):
            raise ExperimentalMapReduceError("map-reduce result is not terminal")
        completed = await _cancellation_fenced_to_thread(
            self.job_store.complete,
            identity,
            ledger,
            reduction.result,
        )
        await _cancellation_fenced_to_thread(
            self.generation_store.release_generation_gc_root,
            identity.job_id,
            identity.generation_id,
        )
        return self._status(completed)

    @staticmethod
    def _status(loaded: LoadedMapReduceJob) -> ExperimentalMapReduceStatus:
        ledger = loaded.ledger
        completed_ids = {item.unit_id for item in ledger.completed_maps}
        mapped_contracts = sum(
            all(unit_id in completed_ids for unit_id in expected.unit_ids)
            for expected in ledger.expected_contracts
        )
        if loaded.cancelled or ledger.status == "cancelled":
            status: Literal["mapping", "reducing", "complete", "cancelled"] = "cancelled"
        elif loaded.artifact_sha256 is not None:
            status = "complete"
        elif len(ledger.completed_maps) == len(ledger.map_units):
            status = "reducing"
        else:
            status = "mapping"
        final_spans = (
            ledger.reduce_result.spans
            if status == "complete" and ledger.reduce_result is not None
            else ()
        )
        return ExperimentalMapReduceStatus(
            generation_id=ledger.identity.generation_id,
            query_sha256=ledger.identity.query_sha256,
            profile_id=ledger.identity.profile_id,
            job_id=ledger.identity.job_id,
            status=status,
            mapped_units=len(ledger.completed_maps),
            total_units=len(ledger.map_units),
            mapped_contracts=mapped_contracts,
            total_contracts=len(ledger.expected_contracts),
            resumed=loaded.resumed,
            rejected_span_count=sum(item.rejected_span_count for item in ledger.completed_maps)
            + sum(item.rejected_span_count for item in ledger.completed_reductions),
            provider_call_count=ledger.provider_call_count,
            provider_input_characters=ledger.provider_input_characters,
            advertised_prompt_tokens=ledger.advertised_prompt_tokens,
            advertised_completion_tokens=ledger.advertised_completion_tokens,
            advertised_total_tokens=ledger.advertised_total_tokens,
            accounted_output_tokens=ledger.accounted_output_tokens,
            pending_provider_call=ledger.pending_call is not None,
            evidence_spans=final_spans,
            artifact_sha256=loaded.artifact_sha256,
        )


__all__ = [
    "ExactEvidenceSpan",
    "ExperimentalMapReduceError",
    "ExperimentalMapReduceLane",
    "ExperimentalMapReduceProfile",
    "ExperimentalMapReduceStatus",
    "ExperimentalMapReduceStore",
    "ExperimentalReasoningProvider",
    "MapReduceIdentity",
    "MapReduceLedger",
    "MapUnitResult",
    "OpenRouterExperimentalReasoner",
    "ProviderDecision",
]
