"""Produce source-grounded answer artifacts for CardRAG gold captures.

The producer deliberately separates retrieval from answer selection.  It reads
sealed ranked retrieval inputs, resolves every evidence item against the pinned
generation database, and lets a deterministic strategy or a sealed decision
artifact select evidence.  Decision providers never receive gold labels.

Answer prose is not accepted from any provider.  The emitted text is assembled
from exact source substrings, so a provider cannot paraphrase evidence or invent
a citation.  The output JSONL is the ``cardrag.gold-answer-artifact.v1`` contract
consumed by :mod:`cardrag_mcp.gold_capture`.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import sys
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol, Self, cast

from cardrag_core import GenerationManifest, canonical_json_bytes, canonical_sha256
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from cardrag_mcp.evaluation import (
    MAX_JSONL_BYTES,
    MAX_RELEASE_QUERIES,
    V109_BASELINE_COMMIT,
    EvaluatedAnswer,
    EvaluationError,
    GoldDataset,
    QueryRunResult,
    RetrievedContract,
    RunArtifactManifest,
    load_gold_jsonl,
)
from cardrag_mcp.gold_capture import (
    AnswerArtifactManifest,
    AnswerRecord,
    ArtifactBinding,
    LaneCaptureReceipt,
    PageGenerationManifest,
)
from cardrag_mcp.schema import validate_schema
from cardrag_mcp.schema_v5 import validate_schema_v5

Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Identifier = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,511}$"),
]
SourceCommit = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{40}([0-9a-f]{24})?$"),
]
AnswerLane = Literal["v109_baseline", "qwen_page", "qwen_structure_exact"]
ServingSchema = Literal[
    "cardrag.serving-db.v4",
    "cardrag.evaluation-page.v1",
    "cardrag.serving-db.v5",
]
DecisionMode = Literal["deterministic_extractive", "sealed_decisions", "provider"]
RetrievalContract = Literal[
    "fixture-unbound.v1",
    "cardrag.gold-run-ranking-projection.v1",
]
RetrievalCapturePhase = Literal["bootstrap_retrieval"]

INPUT_MANIFEST_SCHEMA: Literal["cardrag.gold-answer-input-artifact.v1"] = (
    "cardrag.gold-answer-input-artifact.v1"
)
INPUT_QUERY_SCHEMA: Literal["cardrag.gold-answer-input-query.v1"] = (
    "cardrag.gold-answer-input-query.v1"
)
DECISION_MANIFEST_SCHEMA: Literal["cardrag.gold-answer-decision-artifact.v1"] = (
    "cardrag.gold-answer-decision-artifact.v1"
)
DECISION_SCHEMA: Literal["cardrag.gold-answer-decision.v1"] = "cardrag.gold-answer-decision.v1"
STATE_IDENTITY_SCHEMA: Literal["cardrag.gold-answer-producer-state.v1"] = (
    "cardrag.gold-answer-producer-state.v1"
)
STATE_SHARD_SCHEMA: Literal["cardrag.gold-answer-producer-shard.v1"] = (
    "cardrag.gold-answer-producer-shard.v1"
)
CALL_RESERVATION_SCHEMA: Literal["cardrag.gold-answer-call-reservation.v1"] = (
    "cardrag.gold-answer-call-reservation.v1"
)
CALL_LEDGER_SCHEMA: Literal["cardrag.gold-answer-call-ledger.v1"] = (
    "cardrag.gold-answer-call-ledger.v1"
)
CALL_LEDGER_ENTRY_SCHEMA: Literal["cardrag.gold-answer-call-ledger-entry.v1"] = (
    "cardrag.gold-answer-call-ledger-entry.v1"
)
STATE_BUNDLE_SCHEMA: Literal["cardrag.gold-answer-state-bundle.v1"] = (
    "cardrag.gold-answer-state-bundle.v1"
)
STATE_BUNDLE_QUERY_SCHEMA: Literal["cardrag.gold-answer-state-bundle-query.v1"] = (
    "cardrag.gold-answer-state-bundle-query.v1"
)
RECEIPT_SCHEMA: Literal["cardrag.gold-answer-producer-receipt.v1"] = (
    "cardrag.gold-answer-producer-receipt.v1"
)
REQUEST_SCHEMA: Literal["cardrag.gold-answer-request.v1"] = "cardrag.gold-answer-request.v1"
RANKING_PROJECTION_SCHEMA: Literal["cardrag.gold-run-ranking-projection.v1"] = (
    "cardrag.gold-run-ranking-projection.v1"
)
NO_ANSWER_TEXT = "제공된 검색 근거에서 답을 확인할 수 없습니다."

_MAX_LINE_BYTES = 2 * 1024 * 1024
_MAX_MANIFEST_BYTES = 32 * 1024 * 1024
_MAX_DATABASE_BYTES = 4 * 1024 * 1024 * 1024
_MAX_SOURCE_TEXT_CHARACTERS = 131_072
_MAX_REQUEST_BYTES = 2 * 1024 * 1024
_MAX_PROVIDER_EVIDENCE = 64
_MAX_ANSWER_CHARACTERS = 65_536
_MAX_EXTERNAL_ARTIFACT_BYTES = 95_000_000
_MAX_PORTABLE_ARTIFACT_BYTES = 95_000_000
_PAGE_OVERLAP_CHARACTERS = 160
_PAGE_METADATA_KEYS = frozenset(
    {
        "schema_id",
        "generation_id",
        "source_commit",
        "source_generation_id",
        "source_generation_manifest_sha256",
        "source_generation_manifest_size_bytes",
        "source_serving_database_sha256",
        "source_serving_database_size_bytes",
        "embedding_model",
        "embedding_dimension",
        "embedding_profile_id",
        "chunking_policy",
        "maximum_chars",
        "overlap_chars",
        "source_text_contract",
        "column_contract",
        "row_count",
    }
)
_SOURCE_COMMIT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_NUMERIC_FACT = re.compile(
    r"(?<![0-9A-Za-z])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*"
    r"(?:만원|천원|억원|원|퍼센트|포인트|마일|개월|시간|회|건|일|년|%|P)(?![0-9A-Za-z])"
)
_CREDENTIAL_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{16,}", re.IGNORECASE),
    re.compile(
        r"\b(?:api[_ -]?key|client[_ -]?(?:id|secret)|access[_ -]?token|"
        r"refresh[_ -]?token|authorization)\b\s*[:=]\s*['\"]?[^\s'\"]{8,}",
        re.IGNORECASE,
    ),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
)


class GoldAnswerProducerError(RuntimeError):
    """A bounded, machine-readable answer producer failure."""

    def __init__(self, code: str, *, line: int | None = None) -> None:
        self.code = code
        self.line = line
        super().__init__(code)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self)


class AnswerEvidence(_StrictModel):
    span_id: Identifier
    contract_revision_id: Identifier
    rank: int = Field(ge=1, le=1_000)
    score: float
    source_text: str = Field(min_length=1, max_length=_MAX_SOURCE_TEXT_CHARACTERS)
    source_text_sha256: Sha256Hex

    @model_validator(mode="after")
    def source_is_exact_and_safe(self) -> Self:
        if hashlib.sha256(self.source_text.encode("utf-8")).hexdigest() != self.source_text_sha256:
            raise ValueError("answer evidence text hash is stale")
        if not self.source_text.strip():
            raise ValueError("answer evidence text cannot be whitespace-only")
        _reject_unsafe_text(
            self.source_text,
            code="answer_evidence_text_invalid",
            multiline=True,
            trimmed=False,
        )
        _reject_credentials(self.source_text, code="credential_material_detected")
        return self


class AnswerInputManifest(_StrictModel):
    schema_version: Literal["cardrag.gold-answer-input-artifact.v1"]
    lane: AnswerLane
    gold_sha256: Sha256Hex
    query_count: int = Field(ge=1, le=MAX_RELEASE_QUERIES)
    source_commit: SourceCommit
    generation_id: Identifier
    generation_manifest_sha256: Sha256Hex
    serving_schema: ServingSchema
    serving_database: ArtifactBinding
    answer_profile_id: Identifier
    maximum_answer_evidence_spans: int = Field(ge=1, le=_MAX_PROVIDER_EVIDENCE)
    rendering_contract: Literal["cardrag.extractive-source-blocks.v1"]
    retrieval_contract: RetrievalContract = "fixture-unbound.v1"
    retrieval_capture_phase: RetrievalCapturePhase | None = None
    retrieval_run: ArtifactBinding | None = None
    retrieval_capture_receipt: ArtifactBinding | None = None
    retrieval_attestation_artifact: ArtifactBinding | None = None
    retrieval_raw_score_artifact: ArtifactBinding | None = None
    retrieval_corpus_inventory: ArtifactBinding | None = None
    retrieval_dense_score_matrix: ArtifactBinding | None = None
    retrieval_query_vector_matrix: ArtifactBinding | None = None
    retrieval_lexical_rank_artifact: ArtifactBinding | None = None
    synthetic: Literal[False]

    @model_validator(mode="after")
    def lane_contract_is_exact(self) -> Self:
        expected_schema = {
            "v109_baseline": "cardrag.serving-db.v4",
            "qwen_page": "cardrag.evaluation-page.v1",
            "qwen_structure_exact": "cardrag.serving-db.v5",
        }[self.lane]
        if self.serving_schema != expected_schema:
            raise ValueError("answer input lane and serving schema differ")
        if self.lane == "v109_baseline" and self.source_commit != V109_BASELINE_COMMIT:
            raise ValueError("v1.0.9 answer input is not pinned to the historical commit")
        retrieval_bindings = (
            self.retrieval_run,
            self.retrieval_capture_receipt,
            self.retrieval_attestation_artifact,
            self.retrieval_raw_score_artifact,
            self.retrieval_corpus_inventory,
            self.retrieval_dense_score_matrix,
            self.retrieval_query_vector_matrix,
            self.retrieval_lexical_rank_artifact,
        )
        if self.retrieval_contract == "fixture-unbound.v1":
            if self.retrieval_capture_phase is not None or any(
                binding is not None for binding in retrieval_bindings
            ):
                raise ValueError("fixture-unbound input cannot claim retrieval bindings")
        else:
            core = retrieval_bindings[:4]
            corpus, dense, vector, lexical = retrieval_bindings[4:]
            if self.retrieval_capture_phase != "bootstrap_retrieval" or any(
                binding is None for binding in core
            ):
                raise ValueError("sealed retrieval input requires the bootstrap capture chain")
            if corpus is None or dense is None or vector is None:
                raise ValueError("sealed retrieval requires compact corpus and score sidecars")
            if (self.lane == "v109_baseline") != (lexical is not None):
                raise ValueError("lexical score sidecar differs from lane contract")
        return self


class AnswerInputQuery(_StrictModel):
    schema_version: Literal["cardrag.gold-answer-input-query.v1"]
    query_id: Identifier
    query_sha256: Sha256Hex
    contracts: tuple[RetrievedContract, ...] = Field(default=(), max_length=1_000)
    evidence: tuple[AnswerEvidence, ...] = Field(default=(), max_length=1_000)
    retrieval_ranking_sha256: Sha256Hex | None = None

    @model_validator(mode="after")
    def rankings_are_exact(self) -> Self:
        contract_ids = tuple(row.contract_revision_id for row in self.contracts)
        span_ids = tuple(row.span_id for row in self.evidence)
        if len(contract_ids) != len(set(contract_ids)) or len(span_ids) != len(set(span_ids)):
            raise ValueError("answer input identities must be unique")
        if tuple(row.rank for row in self.contracts) != tuple(range(1, len(self.contracts) + 1)):
            raise ValueError("answer input contract ranks must be contiguous")
        if tuple(row.rank for row in self.evidence) != tuple(range(1, len(self.evidence) + 1)):
            raise ValueError("answer input evidence ranks must be contiguous")
        if not {row.contract_revision_id for row in self.evidence}.issubset(set(contract_ids)):
            raise ValueError("answer evidence must belong to a retrieved contract")
        return self


class ProviderEvidence(_StrictModel):
    span_id: Identifier
    contract_revision_id: Identifier
    rank: int = Field(ge=1, le=_MAX_PROVIDER_EVIDENCE)
    source_text: str = Field(min_length=1, max_length=_MAX_SOURCE_TEXT_CHARACTERS)
    source_text_sha256: Sha256Hex


class AnswerRequest(_StrictModel):
    schema_version: Literal["cardrag.gold-answer-request.v1"]
    idempotency_key: Identifier
    lane: AnswerLane
    source_commit: SourceCommit
    generation_id: Identifier
    generation_manifest_sha256: Sha256Hex
    answer_profile_id: Identifier
    query_id: Identifier
    query_sha256: Sha256Hex
    question: str = Field(min_length=1, max_length=4_096)
    evidence: tuple[ProviderEvidence, ...] = Field(max_length=_MAX_PROVIDER_EVIDENCE)


class AnswerDecision(_StrictModel):
    schema_version: Literal["cardrag.gold-answer-decision.v1"]
    query_id: Identifier
    idempotency_key: Identifier
    no_answer: bool
    citation_span_ids: tuple[Identifier, ...] = Field(default=(), max_length=_MAX_PROVIDER_EVIDENCE)
    numeric_facts: tuple[str, ...] = Field(default=(), max_length=64)
    selected_revision_ids: tuple[Identifier, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def values_are_unique_and_safe(self) -> Self:
        for values in (self.citation_span_ids, self.numeric_facts, self.selected_revision_ids):
            if len(values) != len(set(values)):
                raise ValueError("answer decision values must be unique")
        for fact in self.numeric_facts:
            if (
                not fact
                or fact != fact.strip()
                or len(fact) > 256
                or not any(character.isdecimal() for character in fact)
                or _NUMERIC_FACT.search(fact) is None
            ):
                raise ValueError("answer numeric fact must be trimmed, bounded, and numeric")
            _reject_unsafe_text(fact, code="answer_numeric_fact_invalid", multiline=False)
            _reject_credentials(fact, code="credential_material_detected")
        if self.no_answer and (
            self.citation_span_ids or self.numeric_facts or self.selected_revision_ids
        ):
            raise ValueError("no-answer decision cannot claim evidence")
        if not self.no_answer and not self.citation_span_ids:
            raise ValueError("answerable decision must cite source evidence")
        return self


class DecisionArtifactManifest(_StrictModel):
    schema_version: Literal["cardrag.gold-answer-decision-artifact.v1"]
    capture_input_sha256: Sha256Hex
    gold_sha256: Sha256Hex
    query_count: int = Field(ge=1, le=MAX_RELEASE_QUERIES)
    source_commit: SourceCommit
    generation_id: Identifier
    generation_manifest_sha256: Sha256Hex
    answer_profile_id: Identifier
    decision_authority: Literal["sealed_human", "sealed_provider"]
    release_eligible: bool
    synthetic: Literal[False]


class DecisionArtifactRecord(_StrictModel):
    schema_version: Literal["cardrag.gold-answer-decision-record.v1"]
    query_id: Identifier
    request_sha256: Sha256Hex
    decision: AnswerDecision


class ProducerStateIdentity(_StrictModel):
    schema_version: Literal["cardrag.gold-answer-producer-state.v1"]
    decision_mode: DecisionMode
    decision_artifact: ArtifactBinding | None
    provider_id: Identifier
    decision_release_eligible: bool
    maximum_provider_calls: int = Field(ge=0, le=MAX_RELEASE_QUERIES)
    source_commit: SourceCommit
    gold_sha256: Sha256Hex
    capture_input_sha256: Sha256Hex
    generation_id: Identifier
    generation_manifest_sha256: Sha256Hex
    serving_database: ArtifactBinding
    retrieval_contract: RetrievalContract
    retrieval_capture_phase: RetrievalCapturePhase | None
    retrieval_run: ArtifactBinding | None
    retrieval_capture_receipt: ArtifactBinding | None
    retrieval_attestation_artifact: ArtifactBinding | None
    retrieval_raw_score_artifact: ArtifactBinding | None
    retrieval_corpus_inventory: ArtifactBinding | None
    retrieval_dense_score_matrix: ArtifactBinding | None
    retrieval_query_vector_matrix: ArtifactBinding | None
    retrieval_lexical_rank_artifact: ArtifactBinding | None
    answer_profile_id: Identifier
    query_count: int = Field(ge=1, le=MAX_RELEASE_QUERIES)


class CallReservation(_StrictModel):
    schema_version: Literal["cardrag.gold-answer-call-reservation.v1"]
    logical_call_index: int = Field(ge=1, le=MAX_RELEASE_QUERIES)
    query_id: Identifier
    request_sha256: Sha256Hex
    idempotency_key: Identifier
    provider_id: Identifier


class ProducerShard(_StrictModel):
    schema_version: Literal["cardrag.gold-answer-producer-shard.v1"]
    query_index: int = Field(ge=0, lt=MAX_RELEASE_QUERIES)
    query_id: Identifier
    request_sha256: Sha256Hex
    decision_mode: DecisionMode
    logical_call_index: int | None = Field(default=None, ge=1, le=MAX_RELEASE_QUERIES)
    decision_sha256: Sha256Hex
    decision: AnswerDecision
    record: AnswerRecord


class CallLedgerManifest(_StrictModel):
    schema_version: Literal["cardrag.gold-answer-call-ledger.v1"]
    source_commit: SourceCommit
    gold_sha256: Sha256Hex
    capture_input_sha256: Sha256Hex
    generation_id: Identifier
    generation_manifest_sha256: Sha256Hex
    answer_profile_id: Identifier
    provider_id: Identifier
    query_count: int = Field(ge=1, le=MAX_RELEASE_QUERIES)
    maximum_provider_calls: int = Field(ge=0, le=MAX_RELEASE_QUERIES)
    logical_provider_call_count: int = Field(ge=0, le=MAX_RELEASE_QUERIES)


class CallLedgerEntry(_StrictModel):
    schema_version: Literal["cardrag.gold-answer-call-ledger-entry.v1"]
    logical_call_index: int = Field(ge=1, le=MAX_RELEASE_QUERIES)
    query_id: Identifier
    request_sha256: Sha256Hex
    idempotency_key: Identifier
    provider_id: Identifier
    decision_sha256: Sha256Hex


class StateBundleManifest(_StrictModel):
    schema_version: Literal["cardrag.gold-answer-state-bundle.v1"]
    state_identity: ArtifactBinding
    decision_mode: DecisionMode
    retrieval_corpus_inventory: ArtifactBinding | None
    query_count: int = Field(ge=1, le=MAX_RELEASE_QUERIES)
    reservation_count: int = Field(ge=0, le=MAX_RELEASE_QUERIES)


class StateBundleQuery(_StrictModel):
    schema_version: Literal["cardrag.gold-answer-state-bundle-query.v1"]
    query_index: int = Field(ge=0, lt=MAX_RELEASE_QUERIES)
    query_id: Identifier
    reservation: CallReservation | None
    shard: ProducerShard

    @model_validator(mode="after")
    def state_parts_are_locally_consistent(self) -> Self:
        if self.shard.query_index != self.query_index or self.shard.query_id != self.query_id:
            raise ValueError("state bundle shard identity differs")
        if self.shard.decision_mode == "provider":
            if (
                self.reservation is None
                or self.shard.logical_call_index != self.reservation.logical_call_index
                or self.reservation.query_id != self.query_id
            ):
                raise ValueError("provider state bundle reservation differs")
        elif self.reservation is not None or self.shard.logical_call_index is not None:
            raise ValueError("non-provider state bundle cannot contain a reservation")
        return self


class AnswerProducerReceipt(_StrictModel):
    schema_version: Literal["cardrag.gold-answer-producer-receipt.v1"]
    lane: AnswerLane
    capture_mode: DecisionMode
    release_eligible: bool
    synthetic: Literal[False]
    source_commit: SourceCommit
    gold_sha256: Sha256Hex
    query_count: int = Field(ge=1, le=MAX_RELEASE_QUERIES)
    generation_id: Identifier
    generation_manifest: ArtifactBinding
    serving_database: ArtifactBinding
    capture_input: ArtifactBinding
    retrieval_contract: RetrievalContract
    retrieval_capture_phase: RetrievalCapturePhase | None
    retrieval_run: ArtifactBinding | None
    retrieval_capture_receipt: ArtifactBinding | None
    retrieval_attestation_artifact: ArtifactBinding | None
    retrieval_raw_score_artifact: ArtifactBinding | None
    retrieval_corpus_inventory: ArtifactBinding | None
    retrieval_dense_score_matrix: ArtifactBinding | None
    retrieval_query_vector_matrix: ArtifactBinding | None
    retrieval_lexical_rank_artifact: ArtifactBinding | None
    answer_profile_id: Identifier
    decision_artifact: ArtifactBinding | None
    provider_id: Identifier
    maximum_provider_calls: int = Field(ge=0, le=MAX_RELEASE_QUERIES)
    state_identity: ArtifactBinding
    state_bundle: ArtifactBinding
    answer_artifact: ArtifactBinding
    call_ledger: ArtifactBinding
    logical_provider_call_count: int = Field(ge=0, le=MAX_RELEASE_QUERIES)


class AnswerDecisionProvider(Protocol):
    """Provider boundary; implementations must honor ``idempotency_key``."""

    @property
    def provider_id(self) -> str: ...

    @property
    def answer_profile_id(self) -> str: ...

    def decide(self, request: AnswerRequest) -> AnswerDecision: ...


@dataclass(frozen=True, slots=True)
class QueryIdentity:
    """The only gold fields exposed to answer selection."""

    query_id: str
    question: str

    @property
    def query_sha256(self) -> str:
        return hashlib.sha256(self.question.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AnswerInputDataset:
    manifest: AnswerInputManifest
    queries: tuple[AnswerInputQuery, ...]
    binding: ArtifactBinding


@dataclass(frozen=True, slots=True)
class DecisionDataset:
    manifest: DecisionArtifactManifest
    records: Mapping[str, DecisionArtifactRecord]
    binding: ArtifactBinding


@dataclass(frozen=True, slots=True)
class RetrievalDataset:
    manifest: RunArtifactManifest
    results: tuple[QueryRunResult, ...]
    run_binding: ArtifactBinding
    receipt: LaneCaptureReceipt
    receipt_binding: ArtifactBinding
    attestation_binding: ArtifactBinding
    raw_score_binding: ArtifactBinding
    corpus_inventory_binding: ArtifactBinding | None
    dense_score_matrix_binding: ArtifactBinding | None
    query_vector_matrix_binding: ArtifactBinding | None
    lexical_rank_binding: ArtifactBinding | None


@dataclass(frozen=True, slots=True)
class SourceManifestBinding:
    generation_id: str
    serving_schema: str
    database: ArtifactBinding
    binding: ArtifactBinding
    source_commit: str | None
    row_count: int | None
    embedding_profile_id: str | None
    vector_sha256: str | None
    source_generation_id: str | None
    source_generation_manifest: ArtifactBinding | None
    source_serving_database: ArtifactBinding | None


@dataclass(frozen=True, slots=True)
class AnswerProducerResult:
    receipt: AnswerProducerReceipt
    answer_path: Path
    ledger_path: Path
    state_bundle_path: Path
    receipt_path: Path
    resumed_queries: int
    provider_calls_this_process: int


def _reject_unsafe_text(
    text: str,
    *,
    code: str,
    multiline: bool,
    trimmed: bool = True,
) -> None:
    if trimmed and text != text.strip():
        raise GoldAnswerProducerError(code)
    for character in text:
        category = unicodedata.category(character)
        if category in {"Cc", "Cf"} and not (multiline and character in {"\n", "\t"}):
            raise GoldAnswerProducerError(code)
        if not multiline and character in {"\n", "\r", "\t"}:
            raise GoldAnswerProducerError(code)


def _reject_credentials(text: str, *, code: str) -> None:
    if any(pattern.search(text) is not None for pattern in _CREDENTIAL_PATTERNS):
        raise GoldAnswerProducerError(code)


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_gid,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _open_directory_chain(
    path: Path,
    *,
    code: str,
    create: bool,
    require_owned_leaf: bool,
    require_private_leaf: bool,
) -> int:
    """Open every path component without following symlinks.

    The returned descriptor pins the leaf directory.  Only the caller-created
    state tree must be private; read-only input mounts may be owned by another
    uid and are therefore checked for symlinks but not ownership.
    """

    absolute = _absolute(path)
    flags = (
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open("/", flags)
    try:
        parts = absolute.parts[1:]
        if not parts:
            raise GoldAnswerProducerError(f"{code}_root_forbidden")
        for part in parts:
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise GoldAnswerProducerError(f"{code}_missing") from None
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                    child = os.open(part, flags, dir_fd=descriptor)
                except OSError as exc:
                    raise GoldAnswerProducerError(f"{code}_create_failed") from exc
            except OSError as exc:
                suffix = "symlink" if exc.errno in {errno.ELOOP, errno.ENOTDIR} else "open_failed"
                raise GoldAnswerProducerError(f"{code}_{suffix}") from exc
            os.close(descriptor)
            descriptor = child
        leaf = os.fstat(descriptor)
        if not stat.S_ISDIR(leaf.st_mode):  # pragma: no cover - O_DIRECTORY invariant
            raise GoldAnswerProducerError(f"{code}_not_directory")
        if require_owned_leaf and leaf.st_uid != os.geteuid():
            raise GoldAnswerProducerError(f"{code}_owner_invalid")
        permissions = stat.S_IMODE(leaf.st_mode)
        if require_owned_leaf and permissions & 0o022:
            raise GoldAnswerProducerError(f"{code}_permissions_invalid")
        if require_private_leaf and permissions & 0o077:
            raise GoldAnswerProducerError(f"{code}_not_private")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _read_regular_at(
    parent_descriptor: int,
    name: str,
    *,
    maximum_bytes: int,
    code: str,
) -> bytes:
    try:
        listed = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        raise GoldAnswerProducerError(f"{code}_missing") from None
    if stat.S_ISLNK(listed.st_mode) or not stat.S_ISREG(listed.st_mode):
        raise GoldAnswerProducerError(f"{code}_not_regular")
    if listed.st_size <= 0 or listed.st_size > maximum_bytes:
        raise GoldAnswerProducerError(f"{code}_size_invalid")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError as exc:
        raise GoldAnswerProducerError(f"{code}_open_failed") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise GoldAnswerProducerError(f"{code}_not_regular")
        if before.st_size <= 0 or before.st_size > maximum_bytes:
            raise GoldAnswerProducerError(f"{code}_size_invalid")
        if _stat_identity(listed) != _stat_identity(before):
            raise GoldAnswerProducerError(f"{code}_changed_during_read")
        chunks: list[bytes] = []
        remaining = before.st_size
        size = 0
        while remaining:
            block = os.read(descriptor, min(1024 * 1024, remaining))
            if not block:
                raise GoldAnswerProducerError(f"{code}_changed_during_read")
            if len(block) > maximum_bytes - size:
                raise GoldAnswerProducerError(f"{code}_size_invalid")
            chunks.append(block)
            size += len(block)
            remaining -= len(block)
        if os.read(descriptor, 1):
            raise GoldAnswerProducerError(f"{code}_changed_during_read")
        after = os.fstat(descriptor)
        try:
            current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        except OSError:
            raise GoldAnswerProducerError(f"{code}_changed_during_read") from None
        identity = _stat_identity(before)
        if (
            size != before.st_size
            or identity != _stat_identity(after)
            or identity != _stat_identity(current)
        ):
            raise GoldAnswerProducerError(f"{code}_changed_during_read")
    finally:
        os.close(descriptor)
    return b"".join(chunks)


def _read_regular(path: Path, *, maximum_bytes: int, code: str) -> bytes:
    absolute = _absolute(path)
    parent = _open_directory_chain(
        absolute.parent,
        code=f"{code}_parent",
        create=False,
        require_owned_leaf=False,
        require_private_leaf=False,
    )
    try:
        return _read_regular_at(
            parent,
            absolute.name,
            maximum_bytes=maximum_bytes,
            code=code,
        )
    finally:
        os.close(parent)


def _hash_regular(path: Path, *, maximum_bytes: int, code: str) -> ArtifactBinding:
    absolute = _absolute(path)
    parent = _open_directory_chain(
        absolute.parent,
        code=f"{code}_parent",
        create=False,
        require_owned_leaf=False,
        require_private_leaf=False,
    )
    try:
        listed = os.stat(absolute.name, dir_fd=parent, follow_symlinks=False)
        if stat.S_ISLNK(listed.st_mode) or not stat.S_ISREG(listed.st_mode):
            raise GoldAnswerProducerError(f"{code}_not_regular")
        if listed.st_size <= 0 or listed.st_size > maximum_bytes:
            raise GoldAnswerProducerError(f"{code}_size_invalid")
        descriptor = os.open(
            absolute.name,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent,
        )
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise GoldAnswerProducerError(f"{code}_not_regular")
            if before.st_size <= 0 or before.st_size > maximum_bytes:
                raise GoldAnswerProducerError(f"{code}_size_invalid")
            if _stat_identity(listed) != _stat_identity(before):
                raise GoldAnswerProducerError(f"{code}_changed_during_read")
            digest = hashlib.sha256()
            size = 0
            while block := os.read(descriptor, 1024 * 1024):
                if len(block) > maximum_bytes - size:
                    raise GoldAnswerProducerError(f"{code}_size_invalid")
                digest.update(block)
                size += len(block)
            after = os.fstat(descriptor)
            try:
                current = os.stat(absolute.name, dir_fd=parent, follow_symlinks=False)
            except OSError:
                raise GoldAnswerProducerError(f"{code}_changed_during_read") from None
        finally:
            os.close(descriptor)
        identity = _stat_identity(before)
        if (
            size != before.st_size
            or identity != _stat_identity(after)
            or identity != _stat_identity(current)
        ):
            raise GoldAnswerProducerError(f"{code}_changed_during_read")
        return ArtifactBinding(sha256=digest.hexdigest(), size_bytes=size)
    except OSError as exc:
        raise GoldAnswerProducerError(f"{code}_open_failed") from exc
    finally:
        os.close(parent)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GoldAnswerProducerError("json_duplicate_key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> Any:
    raise GoldAnswerProducerError("json_non_finite_number")


def _canonical_jsonl(payload: bytes, *, code: str) -> tuple[dict[str, Any], ...]:
    if not payload.endswith(b"\n") or payload.startswith(b"\xef\xbb\xbf"):
        raise GoldAnswerProducerError(f"{code}_not_canonical_lines")
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(payload.splitlines(), start=1):
        if not line or len(line) > _MAX_LINE_BYTES:
            raise GoldAnswerProducerError(f"{code}_line_invalid", line=line_number)
        try:
            value = json.loads(
                line.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_constant,
            )
        except GoldAnswerProducerError as exc:
            raise GoldAnswerProducerError(exc.code, line=line_number) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GoldAnswerProducerError(f"{code}_line_invalid", line=line_number) from exc
        if not isinstance(value, dict):
            raise GoldAnswerProducerError(f"{code}_record_not_object", line=line_number)
        try:
            canonical = canonical_json_bytes(value)
        except (TypeError, ValueError, UnicodeError) as exc:
            raise GoldAnswerProducerError(f"{code}_line_invalid", line=line_number) from exc
        if line != canonical:
            raise GoldAnswerProducerError(f"{code}_not_canonical_bytes", line=line_number)
        records.append(cast(dict[str, Any], value))
    if not records:
        raise GoldAnswerProducerError(f"{code}_empty")
    return tuple(records)


def _model_from_record[T: BaseModel](model: type[T], record: Mapping[str, Any], *, code: str) -> T:
    try:
        return model.model_validate_json(canonical_json_bytes(record))
    except (ValidationError, ValueError, TypeError, UnicodeError) as exc:
        if isinstance(exc, GoldAnswerProducerError):
            raise
        raise GoldAnswerProducerError(code) from exc


def _binding(payload: bytes) -> ArtifactBinding:
    return ArtifactBinding(sha256=hashlib.sha256(payload).hexdigest(), size_bytes=len(payload))


def _require_portable_payload_size(payload: bytes, *, code: str) -> None:
    if not payload or len(payload) > _MAX_PORTABLE_ARTIFACT_BYTES:
        raise GoldAnswerProducerError(f"{code}_size_invalid")


def _load_named_sidecar(
    *,
    path: Path | None,
    expected_sha256: str | None,
    expected: ArtifactBinding | None,
    code: str,
) -> ArtifactBinding | None:
    if expected is None:
        if path is not None or expected_sha256 is not None:
            raise GoldAnswerProducerError("retrieval_capture_artifact_binding_mismatch")
        return None
    if path is None or expected_sha256 is None:
        raise GoldAnswerProducerError("retrieval_capture_paths_required")
    binding = _hash_regular(
        path,
        maximum_bytes=_MAX_EXTERNAL_ARTIFACT_BYTES,
        code=code,
    )
    if binding != expected or binding.sha256 != expected_sha256:
        raise GoldAnswerProducerError("retrieval_capture_artifact_binding_mismatch")
    return binding


def _jsonl_bytes(records: Sequence[BaseModel]) -> bytes:
    return b"".join(canonical_json_bytes(record) + b"\n" for record in records)


def _ensure_private_directory(path: Path, *, code: str) -> None:
    descriptor = _open_directory_chain(
        path,
        code=code,
        create=True,
        require_owned_leaf=True,
        require_private_leaf=True,
    )
    os.close(descriptor)


def _entry_exists(path: Path, *, code: str) -> bool:
    absolute = _absolute(path)
    parent = _open_directory_chain(
        absolute.parent,
        code=f"{code}_parent",
        create=False,
        require_owned_leaf=False,
        require_private_leaf=False,
    )
    try:
        try:
            os.stat(absolute.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True
    finally:
        os.close(parent)


def _publish_immutable(path: Path, payload: bytes, *, code: str) -> ArtifactBinding:
    absolute = _absolute(path)
    parent = _open_directory_chain(
        absolute.parent,
        code=f"{code}_parent",
        create=True,
        require_owned_leaf=True,
        require_private_leaf=False,
    )
    temporary_name = f".gold-answer-{secrets.token_hex(16)}"
    try:
        try:
            listed = os.stat(absolute.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            listed = None
        if listed is not None and stat.S_ISLNK(listed.st_mode):
            raise GoldAnswerProducerError(f"{code}_symlink")
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent,
        )
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fchmod(output.fileno(), 0o400)
            os.fsync(output.fileno())
        try:
            os.link(
                temporary_name,
                absolute.name,
                src_dir_fd=parent,
                dst_dir_fd=parent,
                follow_symlinks=False,
            )
        except FileExistsError:
            existing = _read_regular_at(
                parent,
                absolute.name,
                maximum_bytes=max(1, len(payload)),
                code=code,
            )
            if existing != payload:
                raise GoldAnswerProducerError(f"{code}_already_differs") from None
        os.fsync(parent)
    finally:
        try:
            os.unlink(temporary_name, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)
    return _binding(payload)


def _load_source_manifest(path: Path, *, lane: AnswerLane) -> SourceManifestBinding:
    payload = _read_regular(path, maximum_bytes=_MAX_MANIFEST_BYTES, code="generation_manifest")
    binding = _binding(payload)
    try:
        if lane == "qwen_page":
            manifest = PageGenerationManifest.model_validate_json(payload)
            canonical = manifest.canonical_bytes()
            result = SourceManifestBinding(
                generation_id=manifest.generation_id,
                serving_schema=manifest.serving_schema,
                database=manifest.serving_database,
                binding=binding,
                source_commit=manifest.source_commit,
                row_count=manifest.row_count,
                embedding_profile_id=manifest.embedding_profile_id,
                vector_sha256=manifest.vector_artifact.sha256,
                source_generation_id=manifest.source_generation_id,
                source_generation_manifest=manifest.source_generation_manifest,
                source_serving_database=manifest.source_serving_database,
            )
        else:
            source = GenerationManifest.model_validate_json(payload)
            canonical = source.canonical_bytes()
            result = SourceManifestBinding(
                generation_id=source.generation_id,
                serving_schema=source.serving_schema,
                database=ArtifactBinding(
                    sha256=source.serving_database.sha256,
                    size_bytes=source.serving_database.size_bytes,
                ),
                binding=binding,
                source_commit=None,
                row_count=None,
                embedding_profile_id=None,
                vector_sha256=(
                    source.vector_sidecar.artifact.sha256
                    if source.schema_version == "cardrag.generation.v5"
                    and source.vector_sidecar is not None
                    else None
                ),
                source_generation_id=None,
                source_generation_manifest=None,
                source_serving_database=None,
            )
    except Exception as exc:
        raise GoldAnswerProducerError("generation_manifest_invalid") from exc
    if payload != canonical:
        raise GoldAnswerProducerError("generation_manifest_not_canonical")
    expected = {
        "v109_baseline": "cardrag.serving-db.v4",
        "qwen_page": "cardrag.evaluation-page.v1",
        "qwen_structure_exact": "cardrag.serving-db.v5",
    }[lane]
    if result.serving_schema != expected:
        raise GoldAnswerProducerError("generation_manifest_lane_mismatch")
    return result


@contextmanager
def _sqlite_readonly(
    path: Path,
    *,
    expected: ArtifactBinding,
) -> Iterator[sqlite3.Connection]:
    absolute = _absolute(path)
    parent = _open_directory_chain(
        absolute.parent,
        code="serving_database_parent",
        create=False,
        require_owned_leaf=False,
        require_private_leaf=False,
    )
    try:
        listed = os.stat(absolute.name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        os.close(parent)
        raise GoldAnswerProducerError("serving_database_missing") from None
    if stat.S_ISLNK(listed.st_mode) or not stat.S_ISREG(listed.st_mode):
        os.close(parent)
        raise GoldAnswerProducerError("serving_database_not_regular")
    if listed.st_size <= 0 or listed.st_size > _MAX_DATABASE_BYTES:
        os.close(parent)
        raise GoldAnswerProducerError("serving_database_size_invalid")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(absolute.name, flags, dir_fd=parent)
    except OSError as exc:
        os.close(parent)
        raise GoldAnswerProducerError("serving_database_open_failed") from exc
    connection: sqlite3.Connection | None = None
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise GoldAnswerProducerError("serving_database_not_regular")
        if before.st_size <= 0 or before.st_size > _MAX_DATABASE_BYTES:
            raise GoldAnswerProducerError("serving_database_size_invalid")
        if _stat_identity(listed) != _stat_identity(before):
            raise GoldAnswerProducerError("serving_database_changed_during_read")
        digest = hashlib.sha256()
        size = 0
        while block := os.read(descriptor, 1024 * 1024):
            if len(block) > _MAX_DATABASE_BYTES - size:
                raise GoldAnswerProducerError("serving_database_size_invalid")
            digest.update(block)
            size += len(block)
        after_hash = os.fstat(descriptor)
        try:
            current = os.stat(absolute.name, dir_fd=parent, follow_symlinks=False)
        except OSError:
            raise GoldAnswerProducerError("serving_database_changed_during_read") from None
        identity = _stat_identity(before)
        actual = ArtifactBinding(sha256=digest.hexdigest(), size_bytes=size)
        if (
            size != before.st_size
            or identity != _stat_identity(after_hash)
            or identity != _stat_identity(current)
            or actual != expected
        ):
            raise GoldAnswerProducerError("serving_database_binding_mismatch")
        connection = sqlite3.connect(
            f"file:/proc/self/fd/{descriptor}?mode=ro&immutable=1",
            uri=True,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        try:
            yield connection
        finally:
            try:
                current = os.stat(absolute.name, dir_fd=parent, follow_symlinks=False)
            except OSError:
                raise GoldAnswerProducerError("serving_database_changed_during_read") from None
            if identity != _stat_identity(os.fstat(descriptor)) or identity != _stat_identity(
                current
            ):
                raise GoldAnswerProducerError("serving_database_changed_during_read")
    except GoldAnswerProducerError:
        raise
    except sqlite3.Error as exc:
        raise GoldAnswerProducerError("serving_database_open_failed") from exc
    finally:
        if connection is not None:
            connection.close()
        os.close(descriptor)
        os.close(parent)


def _load_answer_inputs(
    path: Path,
    *,
    expected_sha256: str,
    identities: Sequence[QueryIdentity],
) -> AnswerInputDataset:
    payload = _read_regular(
        path,
        maximum_bytes=_MAX_PORTABLE_ARTIFACT_BYTES,
        code="answer_input",
    )
    binding = _binding(payload)
    if binding.sha256 != expected_sha256:
        raise GoldAnswerProducerError("answer_input_sha256_mismatch")
    records = _canonical_jsonl(payload, code="answer_input")
    manifest = _model_from_record(
        AnswerInputManifest,
        records[0],
        code="answer_input_manifest_invalid",
    )
    if manifest.query_count != len(identities) or len(records) != len(identities) + 1:
        raise GoldAnswerProducerError("answer_input_query_count_mismatch")
    queries: list[AnswerInputQuery] = []
    for line, (identity, raw) in enumerate(zip(identities, records[1:], strict=True), start=2):
        try:
            query = _model_from_record(
                AnswerInputQuery,
                raw,
                code="answer_input_query_invalid",
            )
        except GoldAnswerProducerError as exc:
            raise GoldAnswerProducerError(exc.code, line=line) from exc
        if query.query_id != identity.query_id or query.query_sha256 != identity.query_sha256:
            raise GoldAnswerProducerError("answer_input_query_binding_mismatch", line=line)
        queries.append(query)
    return AnswerInputDataset(manifest=manifest, queries=tuple(queries), binding=binding)


def _ranking_projection_sha256(result: QueryRunResult) -> str:
    return canonical_sha256(
        {
            "contracts": [row.model_dump(mode="json") for row in result.contracts],
            "query_id": result.query_id,
            "schema_version": RANKING_PROJECTION_SCHEMA,
            "spans": [row.model_dump(mode="json") for row in result.spans],
        }
    )


def _load_run_artifact(
    path: Path,
    *,
    expected_sha256: str,
    lane: AnswerLane,
) -> tuple[RunArtifactManifest, tuple[QueryRunResult, ...], ArtifactBinding]:
    payload = _read_regular(path, maximum_bytes=MAX_JSONL_BYTES, code="retrieval_run")
    binding = _binding(payload)
    if binding.sha256 != expected_sha256:
        raise GoldAnswerProducerError("retrieval_run_sha256_mismatch")
    records = _canonical_jsonl(payload, code="retrieval_run")
    manifest = _model_from_record(
        RunArtifactManifest,
        records[0],
        code="retrieval_run_manifest_invalid",
    )
    if manifest.lane != lane:
        raise GoldAnswerProducerError("retrieval_run_lane_mismatch")
    if manifest.query_count != len(records) - 1:
        raise GoldAnswerProducerError("retrieval_run_query_count_mismatch")
    results: list[QueryRunResult] = []
    for line, raw in enumerate(records[1:], start=2):
        try:
            result = _model_from_record(
                QueryRunResult,
                raw,
                code="retrieval_run_result_invalid",
            )
        except GoldAnswerProducerError as exc:
            raise GoldAnswerProducerError(exc.code, line=line) from exc
        if result.lane != lane:
            raise GoldAnswerProducerError("retrieval_run_lane_mismatch", line=line)
        results.append(result)
    return manifest, tuple(results), binding


def _load_capture_receipt(
    path: Path,
    *,
    expected_sha256: str,
) -> tuple[LaneCaptureReceipt, ArtifactBinding]:
    payload = _read_regular(
        path,
        maximum_bytes=_MAX_MANIFEST_BYTES,
        code="retrieval_capture_receipt",
    )
    binding = _binding(payload)
    if binding.sha256 != expected_sha256:
        raise GoldAnswerProducerError("retrieval_capture_receipt_sha256_mismatch")
    try:
        receipt = LaneCaptureReceipt.model_validate_json(payload)
    except (ValidationError, ValueError, TypeError) as exc:
        raise GoldAnswerProducerError("retrieval_capture_receipt_invalid") from exc
    if payload != receipt.canonical_bytes():
        raise GoldAnswerProducerError("retrieval_capture_receipt_not_canonical")
    return receipt, binding


def _validate_retrieval_projection(
    inputs: AnswerInputDataset,
    retrieval: RetrievalDataset,
    identities: Sequence[QueryIdentity],
) -> None:
    _validate_authoritative_rankings(inputs, retrieval.results, identities)


def _validate_authoritative_rankings(
    inputs: AnswerInputDataset,
    results: Sequence[QueryRunResult],
    identities: Sequence[QueryIdentity],
) -> None:
    if len(results) != len(identities):
        raise GoldAnswerProducerError("retrieval_query_count_mismatch")
    maximum = inputs.manifest.maximum_answer_evidence_spans
    for line, (identity, query, result) in enumerate(
        zip(identities, inputs.queries, results, strict=True),
        start=2,
    ):
        expected_spans = result.spans[:maximum]
        evidence_projection = tuple(
            (row.span_id, row.contract_revision_id, row.rank, row.score) for row in query.evidence
        )
        expected_projection = tuple(
            (row.span_id, row.contract_revision_id, row.rank, row.score) for row in expected_spans
        )
        if (
            result.query_id != identity.query_id
            or query.query_id != identity.query_id
            or query.contracts != result.contracts
            or evidence_projection != expected_projection
            or query.retrieval_ranking_sha256 != _ranking_projection_sha256(result)
        ):
            raise GoldAnswerProducerError("retrieval_ranking_projection_mismatch", line=line)


def verify_answer_input_ranking(
    *,
    input_path: Path,
    expected_input_sha256: str,
    gold_path: Path,
    expected_gold_sha256: str,
    authoritative_results: Sequence[QueryRunResult],
    expected_lane: AnswerLane,
    release_gate: bool = True,
) -> AnswerInputDataset:
    """Compare an answer input with freshly computed final capture rankings.

    This helper intentionally accepts typed results from the caller's in-memory
    score validation rather than trusting the bootstrap run bound by the input.
    """

    gold = _load_gold_dataset(
        gold_path,
        expected_sha256=expected_gold_sha256,
        release_gate=release_gate,
    )
    identities = _query_identities(gold)
    inputs = _load_answer_inputs(
        input_path,
        expected_sha256=expected_input_sha256,
        identities=identities,
    )
    if inputs.manifest.lane != expected_lane:
        raise GoldAnswerProducerError("answer_input_lane_mismatch")
    if release_gate and inputs.manifest.retrieval_contract != RANKING_PROJECTION_SCHEMA:
        raise GoldAnswerProducerError("release_retrieval_capture_required")
    if any(result.lane != expected_lane for result in authoritative_results):
        raise GoldAnswerProducerError("retrieval_run_lane_mismatch")
    _validate_authoritative_rankings(inputs, authoritative_results, identities)
    return inputs


def _load_bound_retrieval(
    *,
    inputs: AnswerInputDataset,
    identities: Sequence[QueryIdentity],
    source: SourceManifestBinding,
    retrieval_run_path: Path | None,
    expected_retrieval_run_sha256: str | None,
    retrieval_capture_receipt_path: Path | None,
    expected_retrieval_capture_receipt_sha256: str | None,
    retrieval_attestation_path: Path | None,
    expected_retrieval_attestation_sha256: str | None,
    retrieval_raw_score_path: Path | None,
    expected_retrieval_raw_score_sha256: str | None,
    retrieval_corpus_inventory_path: Path | None,
    expected_retrieval_corpus_inventory_sha256: str | None,
    retrieval_dense_score_matrix_path: Path | None,
    expected_retrieval_dense_score_matrix_sha256: str | None,
    retrieval_query_vector_matrix_path: Path | None,
    expected_retrieval_query_vector_matrix_sha256: str | None,
    retrieval_lexical_rank_path: Path | None,
    expected_retrieval_lexical_rank_sha256: str | None,
    release_gate: bool,
) -> RetrievalDataset | None:
    manifest = inputs.manifest
    supplied = (
        retrieval_run_path,
        expected_retrieval_run_sha256,
        retrieval_capture_receipt_path,
        expected_retrieval_capture_receipt_sha256,
        retrieval_attestation_path,
        expected_retrieval_attestation_sha256,
        retrieval_raw_score_path,
        expected_retrieval_raw_score_sha256,
        retrieval_corpus_inventory_path,
        expected_retrieval_corpus_inventory_sha256,
        retrieval_dense_score_matrix_path,
        expected_retrieval_dense_score_matrix_sha256,
        retrieval_query_vector_matrix_path,
        expected_retrieval_query_vector_matrix_sha256,
        retrieval_lexical_rank_path,
        expected_retrieval_lexical_rank_sha256,
    )
    if manifest.retrieval_contract == "fixture-unbound.v1":
        if release_gate:
            raise GoldAnswerProducerError("release_retrieval_capture_required")
        if any(value is not None for value in supplied):
            raise GoldAnswerProducerError("unexpected_retrieval_capture")
        if any(query.retrieval_ranking_sha256 is not None for query in inputs.queries):
            raise GoldAnswerProducerError("fixture_retrieval_ranking_hash_forbidden")
        return None
    if any(value is None for value in supplied[:8]):
        raise GoldAnswerProducerError("retrieval_capture_paths_required")
    if (
        manifest.retrieval_run is None
        or manifest.retrieval_capture_receipt is None
        or manifest.retrieval_attestation_artifact is None
        or manifest.retrieval_raw_score_artifact is None
    ):  # pragma: no cover - model validator invariant
        raise GoldAnswerProducerError("retrieval_capture_manifest_incomplete")
    run_manifest, results, run_binding = _load_run_artifact(
        cast(Path, retrieval_run_path),
        expected_sha256=cast(str, expected_retrieval_run_sha256),
        lane=manifest.lane,
    )
    receipt, receipt_binding = _load_capture_receipt(
        cast(Path, retrieval_capture_receipt_path),
        expected_sha256=cast(str, expected_retrieval_capture_receipt_sha256),
    )
    attestation_binding = _hash_regular(
        cast(Path, retrieval_attestation_path),
        maximum_bytes=(
            MAX_JSONL_BYTES
            if manifest.lane == "qwen_structure_exact"
            else _MAX_EXTERNAL_ARTIFACT_BYTES
        ),
        code="retrieval_attestation",
    )
    raw_score_binding = _hash_regular(
        cast(Path, retrieval_raw_score_path),
        maximum_bytes=(
            _MAX_DATABASE_BYTES
            if manifest.lane == "qwen_structure_exact"
            else _MAX_EXTERNAL_ARTIFACT_BYTES
        ),
        code="retrieval_raw_score",
    )

    corpus_inventory_binding = _load_named_sidecar(
        path=retrieval_corpus_inventory_path,
        expected_sha256=expected_retrieval_corpus_inventory_sha256,
        expected=manifest.retrieval_corpus_inventory,
        code="retrieval_corpus_inventory",
    )
    dense_score_matrix_binding = _load_named_sidecar(
        path=retrieval_dense_score_matrix_path,
        expected_sha256=expected_retrieval_dense_score_matrix_sha256,
        expected=manifest.retrieval_dense_score_matrix,
        code="retrieval_dense_score_matrix",
    )
    query_vector_matrix_binding = _load_named_sidecar(
        path=retrieval_query_vector_matrix_path,
        expected_sha256=expected_retrieval_query_vector_matrix_sha256,
        expected=manifest.retrieval_query_vector_matrix,
        code="retrieval_query_vector_matrix",
    )
    lexical_rank_binding = _load_named_sidecar(
        path=retrieval_lexical_rank_path,
        expected_sha256=expected_retrieval_lexical_rank_sha256,
        expected=manifest.retrieval_lexical_rank_artifact,
        code="retrieval_lexical_rank_artifact",
    )
    if (
        cast(str, expected_retrieval_attestation_sha256) != attestation_binding.sha256
        or cast(str, expected_retrieval_raw_score_sha256) != raw_score_binding.sha256
        or manifest.retrieval_run != run_binding
        or manifest.retrieval_capture_receipt != receipt_binding
        or manifest.retrieval_attestation_artifact != attestation_binding
        or manifest.retrieval_raw_score_artifact != raw_score_binding
    ):
        raise GoldAnswerProducerError("retrieval_capture_artifact_binding_mismatch")
    expected_capture_mode = (
        "native_v5" if manifest.lane == "qwen_structure_exact" else "external_reproducible"
    )
    if (
        run_manifest.gold_sha256 != manifest.gold_sha256
        or run_manifest.query_count != manifest.query_count
        or run_manifest.source_commit != manifest.source_commit
        or run_manifest.generation_id != manifest.generation_id
        or run_manifest.generation_manifest_sha256 != manifest.generation_manifest_sha256
        or run_manifest.serving_schema != manifest.serving_schema
        or receipt.lane != manifest.lane
        or receipt.capture_mode != expected_capture_mode
        or receipt.validation_profile != "release_grade"
        or receipt.gold_sha256 != manifest.gold_sha256
        or receipt.query_count != manifest.query_count
        or receipt.run_artifact != run_binding
        or receipt.attestation_artifact != attestation_binding
        or receipt.source_generation_id != manifest.generation_id
        or receipt.source_generation_manifest_sha256 != manifest.generation_manifest_sha256
        or receipt.source_database_sha256 != manifest.serving_database.sha256
        or receipt.source_vector_sha256 != source.vector_sha256
        or receipt.raw_score_artifact_sha256 != raw_score_binding.sha256
        or receipt.corpus_inventory != corpus_inventory_binding
        or receipt.dense_score_matrix != dense_score_matrix_binding
        or receipt.query_vector_matrix != query_vector_matrix_binding
        or receipt.lexical_rank_artifact != lexical_rank_binding
        or receipt.capture_phase != "bootstrap_retrieval"
        or receipt.release_eligible
        or receipt.answer_evidence is not None
    ):
        raise GoldAnswerProducerError("retrieval_capture_contract_mismatch")
    retrieval = RetrievalDataset(
        manifest=run_manifest,
        results=results,
        run_binding=run_binding,
        receipt=receipt,
        receipt_binding=receipt_binding,
        attestation_binding=attestation_binding,
        raw_score_binding=raw_score_binding,
        corpus_inventory_binding=corpus_inventory_binding,
        dense_score_matrix_binding=dense_score_matrix_binding,
        query_vector_matrix_binding=query_vector_matrix_binding,
        lexical_rank_binding=lexical_rank_binding,
    )
    _validate_retrieval_projection(inputs, retrieval, identities)
    return retrieval


def _load_decisions(
    path: Path,
    *,
    expected_sha256: str,
    inputs: AnswerInputDataset,
) -> DecisionDataset:
    payload = _read_regular(
        path,
        maximum_bytes=_MAX_PORTABLE_ARTIFACT_BYTES,
        code="answer_decisions",
    )
    binding = _binding(payload)
    if binding.sha256 != expected_sha256:
        raise GoldAnswerProducerError("answer_decisions_sha256_mismatch")
    records = _canonical_jsonl(payload, code="answer_decisions")
    manifest = _model_from_record(
        DecisionArtifactManifest,
        records[0],
        code="answer_decisions_manifest_invalid",
    )
    source = inputs.manifest
    if (
        manifest.capture_input_sha256 != inputs.binding.sha256
        or manifest.gold_sha256 != source.gold_sha256
        or manifest.query_count != source.query_count
        or manifest.source_commit != source.source_commit
        or manifest.generation_id != source.generation_id
        or manifest.generation_manifest_sha256 != source.generation_manifest_sha256
        or manifest.answer_profile_id != source.answer_profile_id
        or len(records) != source.query_count + 1
    ):
        raise GoldAnswerProducerError("answer_decisions_manifest_binding_mismatch")
    result: dict[str, DecisionArtifactRecord] = {}
    for line, (expected_query, raw) in enumerate(
        zip(inputs.queries, records[1:], strict=True),
        start=2,
    ):
        try:
            record = _model_from_record(
                DecisionArtifactRecord,
                raw,
                code="answer_decisions_record_invalid",
            )
        except GoldAnswerProducerError as exc:
            raise GoldAnswerProducerError(exc.code, line=line) from exc
        if (
            record.query_id != expected_query.query_id
            or record.query_id in result
            or record.decision.query_id != record.query_id
        ):
            raise GoldAnswerProducerError("answer_decisions_query_order_invalid", line=line)
        result[record.query_id] = record
    return DecisionDataset(manifest=manifest, records=result, binding=binding)


def _verify_source_database(
    connection: sqlite3.Connection,
    *,
    manifest: AnswerInputManifest,
    inputs: Sequence[AnswerInputQuery],
    expected_row_count: int | None,
    expected_page_embedding_profile_id: str | None,
    source_manifest: SourceManifestBinding,
) -> None:
    try:
        metadata = {
            str(row[0]): str(row[1]) for row in connection.execute("SELECT key,value FROM metadata")
        }
        if (
            metadata.get("schema_id") != manifest.serving_schema
            or metadata.get("generation_id") != manifest.generation_id
        ):
            raise GoldAnswerProducerError("serving_database_profile_mismatch")
        if manifest.lane == "qwen_structure_exact":
            validate_schema_v5(connection)
            _verify_v5_evidence(connection, inputs)
        elif manifest.lane == "v109_baseline":
            validate_schema(connection)
            _verify_v4_evidence(connection, inputs)
        else:
            _verify_page_evidence(
                connection,
                inputs,
                metadata=metadata,
                expected_row_count=expected_row_count,
                expected_embedding_profile_id=expected_page_embedding_profile_id,
                expected_source_commit=source_manifest.source_commit,
                expected_source_generation_id=source_manifest.source_generation_id,
                expected_source_generation_manifest=source_manifest.source_generation_manifest,
                expected_source_serving_database=source_manifest.source_serving_database,
            )
    except GoldAnswerProducerError:
        raise
    except Exception as exc:
        raise GoldAnswerProducerError("serving_database_source_validation_failed") from exc


def _verify_v5_evidence(
    connection: sqlite3.Connection,
    inputs: Sequence[AnswerInputQuery],
) -> None:
    cache: dict[tuple[str, str], str] = {}
    for query in inputs:
        for evidence in query.evidence:
            key = (evidence.span_id, evidence.contract_revision_id)
            source = cache.get(key)
            if source is None:
                rows = connection.execute(
                    """SELECT p.text,s.source_start,s.source_end,s.text_sha256,s.span_ordinal,
                              n.display_text
                         FROM node_spans AS s
                         JOIN document_pages AS p
                           ON p.contract_revision_id=s.contract_revision_id AND p.page=s.page
                         JOIN structure_nodes AS n
                           ON n.node_id=s.node_id
                          AND n.contract_revision_id=s.contract_revision_id
                        WHERE s.node_id=? AND s.contract_revision_id=?
                        ORDER BY s.span_ordinal""",
                    key,
                ).fetchall()
                if not rows:
                    raise GoldAnswerProducerError("answer_evidence_not_in_generation")
                fragments: list[str] = []
                display = str(rows[0][5])
                for ordinal, row in enumerate(rows):
                    page_text, start, end, declared, actual_ordinal = (
                        str(row[0]),
                        int(row[1]),
                        int(row[2]),
                        str(row[3]),
                        int(row[4]),
                    )
                    if (
                        actual_ordinal != ordinal
                        or start < 0
                        or end <= start
                        or end > len(page_text)
                    ):
                        raise GoldAnswerProducerError("answer_evidence_source_range_invalid")
                    fragment = page_text[start:end]
                    if hashlib.sha256(fragment.encode("utf-8")).hexdigest() != declared:
                        raise GoldAnswerProducerError("answer_evidence_source_hash_invalid")
                    fragments.append(fragment)
                source = "".join(fragments)
                if source != display:
                    raise GoldAnswerProducerError("answer_evidence_display_text_mismatch")
                cache[key] = source
            if source != evidence.source_text:
                raise GoldAnswerProducerError("answer_evidence_generation_mismatch")


def _verify_v4_evidence(
    connection: sqlite3.Connection,
    inputs: Sequence[AnswerInputQuery],
) -> None:
    cache: dict[tuple[str, str], str] = {}
    for query in inputs:
        for evidence in query.evidence:
            key = (evidence.span_id, evidence.contract_revision_id)
            source = cache.get(key)
            if source is None:
                row = connection.execute(
                    "SELECT text FROM evidence WHERE evidence_id=? AND document_id=?",
                    key,
                ).fetchone()
                if row is None:
                    raise GoldAnswerProducerError("answer_evidence_not_in_generation")
                source = str(row[0])
                cache[key] = source
            if source != evidence.source_text:
                raise GoldAnswerProducerError("answer_evidence_generation_mismatch")


def _verify_page_evidence(
    connection: sqlite3.Connection,
    inputs: Sequence[AnswerInputQuery],
    *,
    metadata: Mapping[str, str],
    expected_row_count: int | None,
    expected_embedding_profile_id: str | None,
    expected_source_commit: str | None,
    expected_source_generation_id: str | None,
    expected_source_generation_manifest: ArtifactBinding | None,
    expected_source_serving_database: ArtifactBinding | None,
) -> None:
    columns = tuple(
        str(row[1]) for row in connection.execute("PRAGMA table_info(evaluation_chunks)")
    )
    if columns != (
        "row_index",
        "chunk_id",
        "contract_revision_id",
        "span_id",
        "document_id",
        "page",
        "source_start",
        "source_end",
        "text",
        "input_sha256",
    ):
        raise GoldAnswerProducerError("page_database_schema_invalid")
    if (
        set(metadata) != _PAGE_METADATA_KEYS
        or connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
        or metadata.get("embedding_model") != "qwen/qwen3-embedding-8b"
        or metadata.get("embedding_dimension") != "4096"
        or expected_embedding_profile_id is None
        or metadata.get("embedding_profile_id") != expected_embedding_profile_id
        or metadata.get("chunking_policy") != "cardrag.page-window-1600.v1"
        or metadata.get("maximum_chars") != "1600"
        or metadata.get("overlap_chars") != str(_PAGE_OVERLAP_CHARACTERS)
        or expected_source_commit is None
        or metadata.get("source_commit") != expected_source_commit
        or expected_source_generation_id is None
        or metadata.get("source_generation_id") != expected_source_generation_id
        or expected_source_generation_manifest is None
        or metadata.get("source_generation_manifest_sha256")
        != expected_source_generation_manifest.sha256
        or metadata.get("source_generation_manifest_size_bytes")
        != str(expected_source_generation_manifest.size_bytes)
        or expected_source_serving_database is None
        or metadata.get("source_serving_database_sha256") != expected_source_serving_database.sha256
        or metadata.get("source_serving_database_size_bytes")
        != str(expected_source_serving_database.size_bytes)
        or metadata.get("source_text_contract") != "cardrag.page-source-text-range.v1"
        or metadata.get("column_contract") != "cardrag.evaluation-page-columns.v1"
        or expected_row_count is None
        or metadata.get("row_count") != str(expected_row_count)
        or int(connection.execute("SELECT count(*) FROM evaluation_chunks").fetchone()[0])
        != expected_row_count
    ):
        raise GoldAnswerProducerError("page_database_profile_mismatch")
    cache: dict[tuple[str, str], str] = {}
    rows = connection.execute(
        """SELECT row_index,chunk_id,contract_revision_id,span_id,document_id,page,
                  source_start,source_end,text,input_sha256
             FROM evaluation_chunks ORDER BY row_index"""
    ).fetchall()
    for expected_index, row in enumerate(rows):
        (
            row_index,
            chunk_id,
            contract_revision_id,
            span_id,
            document_id,
            page,
            source_start,
            source_end,
            text,
            input_sha256,
        ) = (
            int(row[0]),
            str(row[1]),
            str(row[2]),
            str(row[3]),
            str(row[4]),
            int(row[5]),
            int(row[6]),
            int(row[7]),
            str(row[8]),
            str(row[9]),
        )
        expected_chunk_id = "evidence_" + canonical_sha256(
            {
                "document_id": document_id,
                "page": page,
                "source_end": source_end,
                "source_start": source_start,
                "text_sha256": input_sha256,
            }
        )
        if (
            row_index != expected_index
            or chunk_id != span_id
            or chunk_id != expected_chunk_id
            or not contract_revision_id
            or not document_id
            or page < 1
            or source_start < 0
            or source_end <= source_start
            or source_end - source_start != len(text)
            or not text
            or text != text.strip()
            or len(text) > 1600
            or hashlib.sha256(text.encode("utf-8")).hexdigest() != input_sha256
        ):
            raise GoldAnswerProducerError("page_database_source_contract_invalid")
        key = (span_id, contract_revision_id)
        if key in cache:
            raise GoldAnswerProducerError("page_database_source_contract_invalid")
        cache[key] = text
    for query in inputs:
        for evidence in query.evidence:
            key = (evidence.span_id, evidence.contract_revision_id)
            source_text = cache.get(key)
            if source_text is None:
                raise GoldAnswerProducerError("answer_evidence_not_in_generation")
            if (
                source_text != evidence.source_text
                or hashlib.sha256(source_text.encode("utf-8")).hexdigest()
                != evidence.source_text_sha256
            ):
                raise GoldAnswerProducerError("answer_evidence_generation_mismatch")


def _source_text_for_span(
    connection: sqlite3.Connection,
    *,
    lane: AnswerLane,
    span_id: str,
    contract_revision_id: str,
) -> str:
    if lane == "v109_baseline":
        row = connection.execute(
            "SELECT text FROM evidence WHERE evidence_id=? AND document_id=?",
            (span_id, contract_revision_id),
        ).fetchone()
        if row is None:
            raise GoldAnswerProducerError("answer_evidence_not_in_generation")
        return str(row[0])
    if lane == "qwen_page":
        row = connection.execute(
            """SELECT text FROM evaluation_chunks
                 WHERE span_id=? AND contract_revision_id=?""",
            (span_id, contract_revision_id),
        ).fetchone()
        if row is None:
            raise GoldAnswerProducerError("answer_evidence_not_in_generation")
        return str(row[0])
    rows = connection.execute(
        """SELECT p.text,s.source_start,s.source_end,s.text_sha256,s.span_ordinal,
                  n.display_text
             FROM node_spans AS s
             JOIN document_pages AS p
               ON p.contract_revision_id=s.contract_revision_id AND p.page=s.page
             JOIN structure_nodes AS n
               ON n.node_id=s.node_id AND n.contract_revision_id=s.contract_revision_id
            WHERE s.node_id=? AND s.contract_revision_id=?
            ORDER BY s.span_ordinal""",
        (span_id, contract_revision_id),
    ).fetchall()
    if not rows:
        raise GoldAnswerProducerError("answer_evidence_not_in_generation")
    fragments: list[str] = []
    for ordinal, row in enumerate(rows):
        page_text, start, end, declared, actual_ordinal = (
            str(row[0]),
            int(row[1]),
            int(row[2]),
            str(row[3]),
            int(row[4]),
        )
        if actual_ordinal != ordinal or start < 0 or end <= start or end > len(page_text):
            raise GoldAnswerProducerError("answer_evidence_source_range_invalid")
        fragment = page_text[start:end]
        if hashlib.sha256(fragment.encode("utf-8")).hexdigest() != declared:
            raise GoldAnswerProducerError("answer_evidence_source_hash_invalid")
        fragments.append(fragment)
    source = "".join(fragments)
    if source != str(rows[0][5]):
        raise GoldAnswerProducerError("answer_evidence_display_text_mismatch")
    return source


def build_answer_input_artifact(
    *,
    lane: AnswerLane,
    gold_path: Path,
    expected_gold_sha256: str,
    generation_manifest_path: Path,
    database_path: Path,
    retrieval_run_path: Path,
    expected_retrieval_run_sha256: str,
    retrieval_capture_receipt_path: Path,
    expected_retrieval_capture_receipt_sha256: str,
    retrieval_attestation_path: Path,
    expected_retrieval_attestation_sha256: str,
    retrieval_raw_score_path: Path,
    expected_retrieval_raw_score_sha256: str,
    retrieval_corpus_inventory_path: Path,
    expected_retrieval_corpus_inventory_sha256: str,
    output_path: Path,
    expected_source_commit: str,
    answer_profile_id: str,
    maximum_answer_evidence_spans: int = 8,
    release_gate: bool = True,
    retrieval_dense_score_matrix_path: Path | None = None,
    expected_retrieval_dense_score_matrix_sha256: str | None = None,
    retrieval_query_vector_matrix_path: Path | None = None,
    expected_retrieval_query_vector_matrix_sha256: str | None = None,
    retrieval_lexical_rank_path: Path | None = None,
    expected_retrieval_lexical_rank_sha256: str | None = None,
) -> ArtifactBinding:
    """Derive an answer input only from a sealed lane capture and source DB.

    Gold labels are loaded solely to bind the dataset and project query ID/text;
    rankings come byte-for-byte from the capture run and source text comes from
    the pinned generation database.
    """

    commit = _validate_expected_source_commit(expected_source_commit, release_gate=True)
    if commit is None:  # pragma: no cover - release=True invariant
        raise GoldAnswerProducerError("expected_source_commit_required")
    gold = _load_gold_dataset(
        gold_path,
        expected_sha256=expected_gold_sha256,
        release_gate=release_gate,
    )
    identities = _query_identities(gold)
    generation = _load_source_manifest(generation_manifest_path, lane=lane)
    run_manifest, run_results, run_binding = _load_run_artifact(
        retrieval_run_path,
        expected_sha256=expected_retrieval_run_sha256,
        lane=lane,
    )
    capture_receipt, receipt_binding = _load_capture_receipt(
        retrieval_capture_receipt_path,
        expected_sha256=expected_retrieval_capture_receipt_sha256,
    )
    attestation_binding = _hash_regular(
        retrieval_attestation_path,
        maximum_bytes=(
            MAX_JSONL_BYTES if lane == "qwen_structure_exact" else _MAX_EXTERNAL_ARTIFACT_BYTES
        ),
        code="retrieval_attestation",
    )
    raw_score_binding = _hash_regular(
        retrieval_raw_score_path,
        maximum_bytes=(
            _MAX_DATABASE_BYTES if lane == "qwen_structure_exact" else _MAX_EXTERNAL_ARTIFACT_BYTES
        ),
        code="retrieval_raw_score",
    )
    corpus_inventory_binding = _load_named_sidecar(
        path=retrieval_corpus_inventory_path,
        expected_sha256=expected_retrieval_corpus_inventory_sha256,
        expected=capture_receipt.corpus_inventory,
        code="retrieval_corpus_inventory",
    )
    dense_score_matrix_binding = _load_named_sidecar(
        path=retrieval_dense_score_matrix_path,
        expected_sha256=expected_retrieval_dense_score_matrix_sha256,
        expected=capture_receipt.dense_score_matrix,
        code="retrieval_dense_score_matrix",
    )
    query_vector_matrix_binding = _load_named_sidecar(
        path=retrieval_query_vector_matrix_path,
        expected_sha256=expected_retrieval_query_vector_matrix_sha256,
        expected=capture_receipt.query_vector_matrix,
        code="retrieval_query_vector_matrix",
    )
    lexical_rank_binding = _load_named_sidecar(
        path=retrieval_lexical_rank_path,
        expected_sha256=expected_retrieval_lexical_rank_sha256,
        expected=capture_receipt.lexical_rank_artifact,
        code="retrieval_lexical_rank_artifact",
    )
    if (
        attestation_binding.sha256 != expected_retrieval_attestation_sha256
        or raw_score_binding.sha256 != expected_retrieval_raw_score_sha256
        or run_manifest.gold_sha256 != gold.sha256
        or run_manifest.query_count != len(identities)
        or run_manifest.source_commit != commit
        or run_manifest.generation_id != generation.generation_id
        or run_manifest.generation_manifest_sha256 != generation.binding.sha256
        or run_manifest.serving_schema != generation.serving_schema
        or (generation.source_commit is not None and generation.source_commit != commit)
    ):
        raise GoldAnswerProducerError("retrieval_input_source_binding_mismatch")
    manifest = AnswerInputManifest(
        schema_version=INPUT_MANIFEST_SCHEMA,
        lane=lane,
        gold_sha256=gold.sha256,
        query_count=len(identities),
        source_commit=commit,
        generation_id=generation.generation_id,
        generation_manifest_sha256=generation.binding.sha256,
        serving_schema=generation.serving_schema,
        serving_database=generation.database,
        answer_profile_id=answer_profile_id,
        maximum_answer_evidence_spans=maximum_answer_evidence_spans,
        rendering_contract="cardrag.extractive-source-blocks.v1",
        retrieval_contract=RANKING_PROJECTION_SCHEMA,
        retrieval_capture_phase="bootstrap_retrieval",
        retrieval_run=run_binding,
        retrieval_capture_receipt=receipt_binding,
        retrieval_attestation_artifact=attestation_binding,
        retrieval_raw_score_artifact=raw_score_binding,
        retrieval_corpus_inventory=corpus_inventory_binding,
        retrieval_dense_score_matrix=dense_score_matrix_binding,
        retrieval_query_vector_matrix=query_vector_matrix_binding,
        retrieval_lexical_rank_artifact=lexical_rank_binding,
        synthetic=False,
    )
    queries: list[AnswerInputQuery] = []
    with _sqlite_readonly(database_path, expected=generation.database) as connection:
        for identity, result in zip(identities, run_results, strict=True):
            if result.query_id != identity.query_id:
                raise GoldAnswerProducerError("retrieval_run_query_order_mismatch")
            evidence = tuple(
                AnswerEvidence(
                    span_id=span.span_id,
                    contract_revision_id=span.contract_revision_id,
                    rank=span.rank,
                    score=span.score,
                    source_text=(
                        source_text := _source_text_for_span(
                            connection,
                            lane=lane,
                            span_id=span.span_id,
                            contract_revision_id=span.contract_revision_id,
                        )
                    ),
                    source_text_sha256=hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
                )
                for span in result.spans[:maximum_answer_evidence_spans]
            )
            queries.append(
                AnswerInputQuery(
                    schema_version=INPUT_QUERY_SCHEMA,
                    query_id=identity.query_id,
                    query_sha256=identity.query_sha256,
                    contracts=result.contracts,
                    evidence=evidence,
                    retrieval_ranking_sha256=_ranking_projection_sha256(result),
                )
            )
        provisional_payload = _jsonl_bytes((manifest, *queries))
        _require_portable_payload_size(provisional_payload, code="answer_input_artifact")
        inputs = AnswerInputDataset(
            manifest=manifest,
            queries=tuple(queries),
            binding=_binding(provisional_payload),
        )
        retrieval = _load_bound_retrieval(
            inputs=inputs,
            identities=identities,
            source=generation,
            retrieval_run_path=retrieval_run_path,
            expected_retrieval_run_sha256=expected_retrieval_run_sha256,
            retrieval_capture_receipt_path=retrieval_capture_receipt_path,
            expected_retrieval_capture_receipt_sha256=(expected_retrieval_capture_receipt_sha256),
            retrieval_attestation_path=retrieval_attestation_path,
            expected_retrieval_attestation_sha256=expected_retrieval_attestation_sha256,
            retrieval_raw_score_path=retrieval_raw_score_path,
            expected_retrieval_raw_score_sha256=expected_retrieval_raw_score_sha256,
            retrieval_corpus_inventory_path=retrieval_corpus_inventory_path,
            expected_retrieval_corpus_inventory_sha256=(expected_retrieval_corpus_inventory_sha256),
            retrieval_dense_score_matrix_path=retrieval_dense_score_matrix_path,
            expected_retrieval_dense_score_matrix_sha256=(
                expected_retrieval_dense_score_matrix_sha256
            ),
            retrieval_query_vector_matrix_path=retrieval_query_vector_matrix_path,
            expected_retrieval_query_vector_matrix_sha256=(
                expected_retrieval_query_vector_matrix_sha256
            ),
            retrieval_lexical_rank_path=retrieval_lexical_rank_path,
            expected_retrieval_lexical_rank_sha256=(expected_retrieval_lexical_rank_sha256),
            release_gate=release_gate,
        )
        if retrieval is None:  # pragma: no cover - sealed manifest invariant
            raise GoldAnswerProducerError("retrieval_capture_paths_required")
        _verify_source_database(
            connection,
            manifest=manifest,
            inputs=queries,
            expected_row_count=generation.row_count,
            expected_page_embedding_profile_id=generation.embedding_profile_id,
            source_manifest=generation,
        )
    return _publish_immutable(output_path, provisional_payload, code="answer_input_artifact")


def build_answer_request(
    identity: QueryIdentity,
    query_input: AnswerInputQuery,
    manifest: AnswerInputManifest,
) -> AnswerRequest:
    """Build a provider request without exposing gold answer labels."""

    _reject_unsafe_text(identity.question, code="answer_question_invalid", multiline=False)
    _reject_credentials(identity.question, code="credential_material_detected")
    selected = query_input.evidence[: manifest.maximum_answer_evidence_spans]
    evidence = tuple(
        ProviderEvidence(
            span_id=row.span_id,
            contract_revision_id=row.contract_revision_id,
            rank=index,
            source_text=row.source_text,
            source_text_sha256=row.source_text_sha256,
        )
        for index, row in enumerate(selected, start=1)
    )
    base = {
        "answer_profile_id": manifest.answer_profile_id,
        "evidence": [row.model_dump(mode="json") for row in evidence],
        "generation_id": manifest.generation_id,
        "generation_manifest_sha256": manifest.generation_manifest_sha256,
        "lane": manifest.lane,
        "query_id": identity.query_id,
        "query_sha256": identity.query_sha256,
        "question": identity.question,
        "schema_version": REQUEST_SCHEMA,
        "source_commit": manifest.source_commit,
    }
    idempotency_key = "answer-" + hashlib.sha256(canonical_json_bytes(base)).hexdigest()
    request = AnswerRequest(
        schema_version=REQUEST_SCHEMA,
        idempotency_key=idempotency_key,
        lane=manifest.lane,
        source_commit=manifest.source_commit,
        generation_id=manifest.generation_id,
        generation_manifest_sha256=manifest.generation_manifest_sha256,
        answer_profile_id=manifest.answer_profile_id,
        query_id=identity.query_id,
        query_sha256=identity.query_sha256,
        question=identity.question,
        evidence=evidence,
    )
    request_bytes = request.canonical_bytes()
    if len(request_bytes) > _MAX_REQUEST_BYTES:
        raise GoldAnswerProducerError("answer_request_size_invalid")
    _reject_credentials(request_bytes.decode("utf-8"), code="credential_material_detected")
    return request


def _deterministic_decision(request: AnswerRequest) -> AnswerDecision:
    if not request.evidence:
        return AnswerDecision(
            schema_version=DECISION_SCHEMA,
            query_id=request.query_id,
            idempotency_key=request.idempotency_key,
            no_answer=True,
        )
    selected: list[ProviderEvidence] = []
    current_length = 0
    for evidence in request.evidence:
        rendered = evidence.source_text.strip()
        if not rendered:
            continue
        separator = 2 if selected else 0
        if (
            len(selected) == 8
            or current_length + separator + len(rendered) > _MAX_ANSWER_CHARACTERS
        ):
            break
        selected.append(evidence)
        current_length += separator + len(rendered)
    if not selected:
        return AnswerDecision(
            schema_version=DECISION_SCHEMA,
            query_id=request.query_id,
            idempotency_key=request.idempotency_key,
            no_answer=True,
        )
    rendered_text = "\n\n".join(row.source_text.strip() for row in selected)
    facts: list[str] = []
    for match in _NUMERIC_FACT.finditer(rendered_text):
        fact = match.group(0).strip()
        if fact not in facts:
            facts.append(fact)
        if len(facts) == 64:
            break
    revisions = tuple(dict.fromkeys(row.contract_revision_id for row in selected))
    return AnswerDecision(
        schema_version=DECISION_SCHEMA,
        query_id=request.query_id,
        idempotency_key=request.idempotency_key,
        no_answer=False,
        citation_span_ids=tuple(row.span_id for row in selected),
        numeric_facts=tuple(facts),
        selected_revision_ids=revisions,
    )


def _answer_from_decision(request: AnswerRequest, decision: AnswerDecision) -> EvaluatedAnswer:
    if decision.query_id != request.query_id or decision.idempotency_key != request.idempotency_key:
        raise GoldAnswerProducerError("answer_decision_request_binding_mismatch")
    if decision.no_answer:
        return EvaluatedAnswer(
            text=NO_ANSWER_TEXT,
            no_answer=True,
            citation_span_ids=(),
            numeric_facts=(),
            selected_revision_ids=(),
        )
    by_span = {row.span_id: row for row in request.evidence}
    try:
        selected = tuple(by_span[span_id] for span_id in decision.citation_span_ids)
    except KeyError:
        raise GoldAnswerProducerError("answer_citation_not_retrieved") from None
    expected_order = tuple(row.span_id for row in request.evidence if row.span_id in by_span)
    selected_order = tuple(row.span_id for row in selected)
    if selected_order != tuple(span_id for span_id in expected_order if span_id in selected_order):
        raise GoldAnswerProducerError("answer_citation_order_invalid")
    revisions = tuple(dict.fromkeys(row.contract_revision_id for row in selected))
    if decision.selected_revision_ids != revisions:
        raise GoldAnswerProducerError("answer_revision_binding_mismatch")
    text = "\n\n".join(row.source_text.strip() for row in selected)
    if not text or len(text) > _MAX_ANSWER_CHARACTERS:
        raise GoldAnswerProducerError("answer_text_size_invalid")
    _reject_unsafe_text(text, code="answer_text_invalid", multiline=True)
    _reject_credentials(text, code="credential_material_detected")
    if any(fact not in text for fact in decision.numeric_facts):
        raise GoldAnswerProducerError("answer_numeric_fact_not_in_cited_source")
    fact_order = tuple(sorted(decision.numeric_facts, key=lambda fact: (text.find(fact), fact)))
    if decision.numeric_facts != fact_order:
        raise GoldAnswerProducerError("answer_numeric_fact_order_invalid")
    return EvaluatedAnswer(
        text=text,
        no_answer=False,
        citation_span_ids=decision.citation_span_ids,
        numeric_facts=decision.numeric_facts,
        selected_revision_ids=decision.selected_revision_ids,
    )


def _read_model_file[T: BaseModel](path: Path, model: type[T], *, code: str) -> T:
    payload = _read_regular(path, maximum_bytes=_MAX_LINE_BYTES, code=code)
    if payload.endswith(b"\n"):
        body = payload[:-1]
    else:
        raise GoldAnswerProducerError(f"{code}_not_canonical")
    try:
        value = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (GoldAnswerProducerError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GoldAnswerProducerError(f"{code}_invalid") from exc
    if not isinstance(value, dict) or body != canonical_json_bytes(value):
        raise GoldAnswerProducerError(f"{code}_not_canonical")
    return _model_from_record(model, cast(dict[str, Any], value), code=f"{code}_invalid")


def _state_paths(state_directory: Path, query_index: int) -> tuple[Path, Path]:
    return (
        state_directory / "calls" / f"query-{query_index:03d}.json",
        state_directory / "shards" / f"query-{query_index:03d}.json",
    )


def _validate_expected_source_commit(value: str | None, *, release_gate: bool) -> str | None:
    if value is None:
        if release_gate:
            raise GoldAnswerProducerError("expected_source_commit_required")
        return None
    if _SOURCE_COMMIT.fullmatch(value) is None:
        raise GoldAnswerProducerError("expected_source_commit_invalid")
    return value


def _load_gold_dataset(
    path: Path,
    *,
    expected_sha256: str,
    release_gate: bool,
) -> GoldDataset:
    pinned = _binding(
        _read_regular(
            path,
            maximum_bytes=_MAX_PORTABLE_ARTIFACT_BYTES,
            code="gold",
        )
    )
    try:
        gold = load_gold_jsonl(path, release_gate=release_gate)
    except EvaluationError as exc:
        raise GoldAnswerProducerError(f"gold_{exc.code}", line=exc.line) from exc
    if gold.sha256 != pinned.sha256:
        raise GoldAnswerProducerError("gold_changed_during_read")
    if gold.sha256 != expected_sha256:
        raise GoldAnswerProducerError("gold_sha256_mismatch")
    return gold


def _query_identities(gold: GoldDataset) -> tuple[QueryIdentity, ...]:
    # Do not add contracts/spans/facts/revisions to this projection.  This is the
    # label-leakage boundary used by deterministic and provider selection.
    return tuple(QueryIdentity(query.query_id, query.question) for query in gold.queries)


def produce_answer_artifact(
    *,
    gold_path: Path,
    expected_gold_sha256: str,
    input_path: Path,
    expected_input_sha256: str,
    generation_manifest_path: Path,
    database_path: Path,
    state_directory: Path,
    answer_path: Path,
    ledger_path: Path,
    receipt_path: Path,
    expected_source_commit: str | None,
    expected_answer_profile_id: str,
    retrieval_run_path: Path | None = None,
    expected_retrieval_run_sha256: str | None = None,
    retrieval_capture_receipt_path: Path | None = None,
    expected_retrieval_capture_receipt_sha256: str | None = None,
    retrieval_attestation_path: Path | None = None,
    expected_retrieval_attestation_sha256: str | None = None,
    retrieval_raw_score_path: Path | None = None,
    expected_retrieval_raw_score_sha256: str | None = None,
    retrieval_corpus_inventory_path: Path | None = None,
    expected_retrieval_corpus_inventory_sha256: str | None = None,
    retrieval_dense_score_matrix_path: Path | None = None,
    expected_retrieval_dense_score_matrix_sha256: str | None = None,
    retrieval_query_vector_matrix_path: Path | None = None,
    expected_retrieval_query_vector_matrix_sha256: str | None = None,
    retrieval_lexical_rank_path: Path | None = None,
    expected_retrieval_lexical_rank_sha256: str | None = None,
    deterministic: bool = False,
    decision_path: Path | None = None,
    expected_decision_sha256: str | None = None,
    provider: AnswerDecisionProvider | None = None,
    provider_release_eligible: bool = False,
    maximum_provider_calls: int = MAX_RELEASE_QUERIES,
    release_gate: bool = True,
) -> AnswerProducerResult:
    """Create or byte-revalidate an immutable answer artifact and audit receipt."""

    modes = int(deterministic) + int(decision_path is not None) + int(provider is not None)
    if modes != 1:
        raise GoldAnswerProducerError("answer_decision_mode_invalid")
    if (
        type(maximum_provider_calls) is not int
        or not 0 <= maximum_provider_calls <= MAX_RELEASE_QUERIES
    ):
        raise GoldAnswerProducerError("maximum_provider_calls_invalid")
    expected_commit = _validate_expected_source_commit(
        expected_source_commit,
        release_gate=release_gate,
    )
    gold = _load_gold_dataset(
        gold_path,
        expected_sha256=expected_gold_sha256,
        release_gate=release_gate,
    )
    identities = _query_identities(gold)
    inputs = _load_answer_inputs(
        input_path,
        expected_sha256=expected_input_sha256,
        identities=identities,
    )
    source = inputs.manifest
    if source.gold_sha256 != gold.sha256:
        raise GoldAnswerProducerError("answer_input_gold_binding_mismatch")
    if expected_commit is not None and source.source_commit != expected_commit:
        raise GoldAnswerProducerError("candidate_source_commit_mismatch")
    if source.answer_profile_id != expected_answer_profile_id:
        raise GoldAnswerProducerError("answer_profile_id_mismatch")
    generation = _load_source_manifest(generation_manifest_path, lane=source.lane)
    if (
        generation.generation_id != source.generation_id
        or generation.serving_schema != source.serving_schema
        or generation.database != source.serving_database
        or generation.binding.sha256 != source.generation_manifest_sha256
        or (
            generation.source_commit is not None
            and generation.source_commit != source.source_commit
        )
    ):
        raise GoldAnswerProducerError("generation_manifest_binding_mismatch")
    retrieval = _load_bound_retrieval(
        inputs=inputs,
        identities=identities,
        source=generation,
        retrieval_run_path=retrieval_run_path,
        expected_retrieval_run_sha256=expected_retrieval_run_sha256,
        retrieval_capture_receipt_path=retrieval_capture_receipt_path,
        expected_retrieval_capture_receipt_sha256=expected_retrieval_capture_receipt_sha256,
        retrieval_attestation_path=retrieval_attestation_path,
        expected_retrieval_attestation_sha256=expected_retrieval_attestation_sha256,
        retrieval_raw_score_path=retrieval_raw_score_path,
        expected_retrieval_raw_score_sha256=expected_retrieval_raw_score_sha256,
        retrieval_corpus_inventory_path=retrieval_corpus_inventory_path,
        expected_retrieval_corpus_inventory_sha256=(expected_retrieval_corpus_inventory_sha256),
        retrieval_dense_score_matrix_path=retrieval_dense_score_matrix_path,
        expected_retrieval_dense_score_matrix_sha256=(expected_retrieval_dense_score_matrix_sha256),
        retrieval_query_vector_matrix_path=retrieval_query_vector_matrix_path,
        expected_retrieval_query_vector_matrix_sha256=(
            expected_retrieval_query_vector_matrix_sha256
        ),
        retrieval_lexical_rank_path=retrieval_lexical_rank_path,
        expected_retrieval_lexical_rank_sha256=expected_retrieval_lexical_rank_sha256,
        release_gate=release_gate,
    )
    with _sqlite_readonly(database_path, expected=source.serving_database) as connection:
        _verify_source_database(
            connection,
            manifest=source,
            inputs=inputs.queries,
            expected_row_count=generation.row_count,
            expected_page_embedding_profile_id=generation.embedding_profile_id,
            source_manifest=generation,
        )

    decisions: DecisionDataset | None = None
    decision_binding: ArtifactBinding | None = None
    if decision_path is not None:
        if expected_decision_sha256 is None:
            raise GoldAnswerProducerError("expected_decision_sha256_required")
        decisions = _load_decisions(
            decision_path,
            expected_sha256=expected_decision_sha256,
            inputs=inputs,
        )
        decision_binding = decisions.binding
        mode: DecisionMode = "sealed_decisions"
        provider_id = f"{decisions.manifest.decision_authority}:{decisions.binding.sha256}"
        eligible = decisions.manifest.release_eligible
    elif deterministic:
        if expected_decision_sha256 is not None:
            raise GoldAnswerProducerError("unexpected_decision_sha256")
        mode = "deterministic_extractive"
        provider_id = "builtin-deterministic-extractive-v1"
        eligible = True
    else:
        if expected_decision_sha256 is not None:
            raise GoldAnswerProducerError("unexpected_decision_sha256")
        if provider is None:  # pragma: no cover - modes invariant
            raise GoldAnswerProducerError("answer_provider_missing")
        if provider.answer_profile_id != source.answer_profile_id:
            raise GoldAnswerProducerError("answer_provider_profile_mismatch")
        try:
            provider_id = provider.provider_id
            _StrictProviderIdentity(provider_id=provider_id)
        except ValidationError as exc:
            raise GoldAnswerProducerError("answer_provider_id_invalid") from exc
        mode = "provider"
        eligible = provider_release_eligible
        if maximum_provider_calls < len(identities):
            raise GoldAnswerProducerError("maximum_provider_calls_insufficient")
    if release_gate and not eligible:
        raise GoldAnswerProducerError("answer_mode_not_release_eligible")

    state_bundle_path = state_directory / "state-bundle.jsonl"
    roots = {
        _absolute(answer_path),
        _absolute(ledger_path),
        _absolute(receipt_path),
        _absolute(state_directory / "identity.json"),
        _absolute(state_bundle_path),
    }
    if len(roots) != 5:
        raise GoldAnswerProducerError("answer_output_paths_overlap")
    _ensure_private_directory(state_directory, code="answer_state")
    _ensure_private_directory(state_directory / "calls", code="answer_state_calls")
    _ensure_private_directory(state_directory / "shards", code="answer_state_shards")
    identity_model = ProducerStateIdentity(
        schema_version=STATE_IDENTITY_SCHEMA,
        decision_mode=mode,
        decision_artifact=decision_binding,
        provider_id=provider_id,
        decision_release_eligible=eligible,
        maximum_provider_calls=maximum_provider_calls,
        source_commit=source.source_commit,
        gold_sha256=gold.sha256,
        capture_input_sha256=inputs.binding.sha256,
        generation_id=source.generation_id,
        generation_manifest_sha256=source.generation_manifest_sha256,
        serving_database=source.serving_database,
        retrieval_contract=source.retrieval_contract,
        retrieval_capture_phase=source.retrieval_capture_phase,
        retrieval_run=None if retrieval is None else retrieval.run_binding,
        retrieval_capture_receipt=None if retrieval is None else retrieval.receipt_binding,
        retrieval_attestation_artifact=None if retrieval is None else retrieval.attestation_binding,
        retrieval_raw_score_artifact=None if retrieval is None else retrieval.raw_score_binding,
        retrieval_corpus_inventory=(
            None if retrieval is None else retrieval.corpus_inventory_binding
        ),
        retrieval_dense_score_matrix=(
            None if retrieval is None else retrieval.dense_score_matrix_binding
        ),
        retrieval_query_vector_matrix=(
            None if retrieval is None else retrieval.query_vector_matrix_binding
        ),
        retrieval_lexical_rank_artifact=(
            None if retrieval is None else retrieval.lexical_rank_binding
        ),
        answer_profile_id=source.answer_profile_id,
        query_count=len(identities),
    )
    _reject_credentials(
        identity_model.canonical_bytes().decode("utf-8"),
        code="credential_material_detected",
    )
    state_identity_binding = _publish_immutable(
        state_directory / "identity.json",
        identity_model.canonical_bytes() + b"\n",
        code="answer_state_identity",
    )

    shards: list[ProducerShard] = []
    reservations: list[CallReservation] = []
    resumed = 0
    provider_calls = 0
    for query_index, (identity, query_input) in enumerate(
        zip(identities, inputs.queries, strict=True)
    ):
        request = build_answer_request(identity, query_input, source)
        request_sha256 = hashlib.sha256(request.canonical_bytes()).hexdigest()
        reservation_path, shard_path = _state_paths(state_directory, query_index)
        if _entry_exists(shard_path, code="answer_state_shard"):
            shard = _read_model_file(
                shard_path,
                ProducerShard,
                code="answer_state_shard",
            )
            if (
                shard.query_index != query_index
                or shard.query_id != identity.query_id
                or shard.request_sha256 != request_sha256
                or shard.decision_mode != mode
                or shard.record.query_id != identity.query_id
                or shard.record.query_sha256 != identity.query_sha256
                or shard.decision_sha256
                != hashlib.sha256(shard.decision.canonical_bytes()).hexdigest()
                or shard.record.answer != _answer_from_decision(request, shard.decision)
            ):
                raise GoldAnswerProducerError("answer_state_shard_binding_mismatch")
            if mode == "provider":
                if shard.logical_call_index is None:
                    raise GoldAnswerProducerError("answer_state_provider_call_missing")
                reservation = _read_model_file(
                    reservation_path,
                    CallReservation,
                    code="answer_call_reservation",
                )
                if (
                    reservation.logical_call_index != shard.logical_call_index
                    or reservation.query_id != identity.query_id
                    or reservation.request_sha256 != request_sha256
                    or reservation.idempotency_key != request.idempotency_key
                    or reservation.provider_id != provider_id
                ):
                    raise GoldAnswerProducerError("answer_call_reservation_binding_mismatch")
                reservations.append(reservation)
            else:
                if shard.logical_call_index is not None or _entry_exists(
                    reservation_path,
                    code="answer_call_reservation",
                ):
                    raise GoldAnswerProducerError("unexpected_answer_call_reservation")
                if mode == "deterministic_extractive":
                    expected_resumed_decision = _deterministic_decision(request)
                else:
                    if decisions is None:  # pragma: no cover - mode invariant
                        raise GoldAnswerProducerError("answer_decisions_missing")
                    sealed = decisions.records.get(identity.query_id)
                    if sealed is None or sealed.request_sha256 != request_sha256:
                        raise GoldAnswerProducerError("answer_decisions_query_binding_mismatch")
                    expected_resumed_decision = sealed.decision
                if shard.decision != expected_resumed_decision:
                    raise GoldAnswerProducerError("answer_state_decision_provenance_mismatch")
            shards.append(shard)
            resumed += 1
            continue

        logical_call_index: int | None = None
        if mode == "deterministic_extractive":
            if _entry_exists(reservation_path, code="answer_call_reservation"):
                raise GoldAnswerProducerError("unexpected_answer_call_reservation")
            decision = _deterministic_decision(request)
        elif mode == "sealed_decisions":
            if _entry_exists(reservation_path, code="answer_call_reservation"):
                raise GoldAnswerProducerError("unexpected_answer_call_reservation")
            if decisions is None:  # pragma: no cover - mode invariant
                raise GoldAnswerProducerError("answer_decisions_missing")
            sealed = decisions.records.get(identity.query_id)
            if sealed is None or sealed.request_sha256 != request_sha256:
                raise GoldAnswerProducerError("answer_decisions_query_binding_mismatch")
            decision = sealed.decision
        else:
            if provider is None:  # pragma: no cover - mode invariant
                raise GoldAnswerProducerError("answer_provider_missing")
            logical_call_index = len(reservations) + 1
            if logical_call_index > maximum_provider_calls:
                raise GoldAnswerProducerError("answer_provider_call_limit_exceeded")
            reservation = CallReservation(
                schema_version=CALL_RESERVATION_SCHEMA,
                logical_call_index=logical_call_index,
                query_id=identity.query_id,
                request_sha256=request_sha256,
                idempotency_key=request.idempotency_key,
                provider_id=provider_id,
            )
            _publish_immutable(
                reservation_path,
                reservation.canonical_bytes() + b"\n",
                code="answer_call_reservation",
            )
            reservations.append(reservation)
            decision = provider.decide(request)
            provider_calls += 1
        answer = _answer_from_decision(request, decision)
        _reject_credentials(
            decision.canonical_bytes().decode("utf-8"),
            code="credential_material_detected",
        )
        record = AnswerRecord(
            schema_version="cardrag.gold-answer.v1",
            query_id=identity.query_id,
            query_sha256=identity.query_sha256,
            answer=answer,
        )
        decision_sha256 = hashlib.sha256(decision.canonical_bytes()).hexdigest()
        shard = ProducerShard(
            schema_version=STATE_SHARD_SCHEMA,
            query_index=query_index,
            query_id=identity.query_id,
            request_sha256=request_sha256,
            decision_mode=mode,
            logical_call_index=logical_call_index,
            decision_sha256=decision_sha256,
            decision=decision,
            record=record,
        )
        _publish_immutable(
            shard_path,
            shard.canonical_bytes() + b"\n",
            code="answer_state_shard",
        )
        shards.append(shard)

    if mode == "sealed_decisions" and decisions is not None:
        if set(decisions.records) != {identity.query_id for identity in identities}:
            raise GoldAnswerProducerError("answer_decisions_query_coverage_mismatch")
    reservations.sort(key=lambda row: row.logical_call_index)
    if tuple(row.logical_call_index for row in reservations) != tuple(
        range(1, len(reservations) + 1)
    ):
        raise GoldAnswerProducerError("answer_call_ledger_not_contiguous")
    if len(reservations) > maximum_provider_calls:
        raise GoldAnswerProducerError("answer_provider_call_limit_exceeded")

    reservations_by_query = {reservation.query_id: reservation for reservation in reservations}
    state_bundle_manifest = StateBundleManifest(
        schema_version=STATE_BUNDLE_SCHEMA,
        state_identity=state_identity_binding,
        decision_mode=mode,
        retrieval_corpus_inventory=(
            None if retrieval is None else retrieval.corpus_inventory_binding
        ),
        query_count=len(shards),
        reservation_count=len(reservations),
    )
    state_bundle_queries = tuple(
        StateBundleQuery(
            schema_version=STATE_BUNDLE_QUERY_SCHEMA,
            query_index=query_index,
            query_id=shard.query_id,
            reservation=reservations_by_query.get(shard.query_id),
            shard=shard,
        )
        for query_index, shard in enumerate(shards)
    )
    state_bundle_payload = _jsonl_bytes((state_bundle_manifest, *state_bundle_queries))
    _require_portable_payload_size(state_bundle_payload, code="answer_state_bundle")
    _reject_credentials(
        state_bundle_payload.decode("utf-8"),
        code="credential_material_detected",
    )
    state_bundle_binding = _publish_immutable(
        state_bundle_path,
        state_bundle_payload,
        code="answer_state_bundle",
    )

    answer_manifest = AnswerArtifactManifest(
        schema_version="cardrag.gold-answer-artifact.v1",
        lane=source.lane,
        gold_sha256=gold.sha256,
        query_count=len(identities),
        generation_id=source.generation_id,
        generation_manifest_sha256=source.generation_manifest_sha256,
        answer_profile_id=source.answer_profile_id,
        synthetic=False,
    )
    answer_payload = _jsonl_bytes((answer_manifest, *(shard.record for shard in shards)))
    _require_portable_payload_size(answer_payload, code="answer_artifact")
    _reject_credentials(answer_payload.decode("utf-8"), code="credential_material_detected")
    answer_binding = _publish_immutable(answer_path, answer_payload, code="answer_artifact")
    ledger_manifest = CallLedgerManifest(
        schema_version=CALL_LEDGER_SCHEMA,
        source_commit=source.source_commit,
        gold_sha256=gold.sha256,
        capture_input_sha256=inputs.binding.sha256,
        generation_id=source.generation_id,
        generation_manifest_sha256=source.generation_manifest_sha256,
        answer_profile_id=source.answer_profile_id,
        provider_id=provider_id,
        query_count=len(identities),
        maximum_provider_calls=maximum_provider_calls,
        logical_provider_call_count=len(reservations),
    )
    ledger_entries = tuple(
        CallLedgerEntry(
            schema_version=CALL_LEDGER_ENTRY_SCHEMA,
            logical_call_index=reservation.logical_call_index,
            query_id=reservation.query_id,
            request_sha256=reservation.request_sha256,
            idempotency_key=reservation.idempotency_key,
            provider_id=reservation.provider_id,
            decision_sha256=shards[
                next(
                    index
                    for index, shard in enumerate(shards)
                    if shard.query_id == reservation.query_id
                )
            ].decision_sha256,
        )
        for reservation in reservations
    )
    ledger_payload = _jsonl_bytes((ledger_manifest, *ledger_entries))
    _require_portable_payload_size(ledger_payload, code="answer_call_ledger")
    _reject_credentials(ledger_payload.decode("utf-8"), code="credential_material_detected")
    ledger_binding = _publish_immutable(
        ledger_path,
        ledger_payload,
        code="answer_call_ledger",
    )
    receipt = AnswerProducerReceipt(
        schema_version=RECEIPT_SCHEMA,
        lane=source.lane,
        capture_mode=mode,
        release_eligible=release_gate and eligible,
        synthetic=False,
        source_commit=source.source_commit,
        gold_sha256=gold.sha256,
        query_count=len(identities),
        generation_id=source.generation_id,
        generation_manifest=generation.binding,
        serving_database=source.serving_database,
        capture_input=inputs.binding,
        retrieval_contract=source.retrieval_contract,
        retrieval_capture_phase=source.retrieval_capture_phase,
        retrieval_run=None if retrieval is None else retrieval.run_binding,
        retrieval_capture_receipt=None if retrieval is None else retrieval.receipt_binding,
        retrieval_attestation_artifact=None if retrieval is None else retrieval.attestation_binding,
        retrieval_raw_score_artifact=None if retrieval is None else retrieval.raw_score_binding,
        retrieval_corpus_inventory=(
            None if retrieval is None else retrieval.corpus_inventory_binding
        ),
        retrieval_dense_score_matrix=(
            None if retrieval is None else retrieval.dense_score_matrix_binding
        ),
        retrieval_query_vector_matrix=(
            None if retrieval is None else retrieval.query_vector_matrix_binding
        ),
        retrieval_lexical_rank_artifact=(
            None if retrieval is None else retrieval.lexical_rank_binding
        ),
        answer_profile_id=source.answer_profile_id,
        decision_artifact=decision_binding,
        provider_id=provider_id,
        maximum_provider_calls=maximum_provider_calls,
        state_identity=state_identity_binding,
        state_bundle=state_bundle_binding,
        answer_artifact=answer_binding,
        call_ledger=ledger_binding,
        logical_provider_call_count=len(reservations),
    )
    receipt_payload = receipt.canonical_bytes() + b"\n"
    _require_portable_payload_size(receipt_payload, code="answer_receipt")
    _reject_credentials(receipt_payload.decode("utf-8"), code="credential_material_detected")
    _publish_immutable(
        receipt_path,
        receipt_payload,
        code="answer_receipt",
    )
    return AnswerProducerResult(
        receipt=receipt,
        answer_path=answer_path,
        ledger_path=ledger_path,
        state_bundle_path=state_bundle_path,
        receipt_path=receipt_path,
        resumed_queries=resumed,
        provider_calls_this_process=provider_calls,
    )


def load_answer_producer_receipt(
    path: Path,
    *,
    expected_sha256: str,
) -> AnswerProducerReceipt:
    """Read one canonical producer receipt with an external SHA-256 pin."""

    payload = _read_regular(path, maximum_bytes=_MAX_MANIFEST_BYTES, code="answer_receipt")
    binding = _binding(payload)
    if binding.sha256 != expected_sha256:
        raise GoldAnswerProducerError("answer_receipt_sha256_mismatch")
    try:
        receipt = AnswerProducerReceipt.model_validate_json(payload)
    except (ValidationError, ValueError, TypeError) as exc:
        raise GoldAnswerProducerError("answer_receipt_invalid") from exc
    if payload != receipt.canonical_bytes() + b"\n":
        raise GoldAnswerProducerError("answer_receipt_not_canonical")
    return receipt


def _load_state_bundle(
    path: Path,
    *,
    expected: ArtifactBinding,
) -> tuple[StateBundleManifest, tuple[StateBundleQuery, ...], ArtifactBinding]:
    payload = _read_regular(
        path,
        maximum_bytes=_MAX_PORTABLE_ARTIFACT_BYTES,
        code="answer_state_bundle",
    )
    binding = _binding(payload)
    if binding != expected:
        raise GoldAnswerProducerError("answer_state_bundle_binding_mismatch")
    records = _canonical_jsonl(payload, code="answer_state_bundle")
    manifest = _model_from_record(
        StateBundleManifest,
        records[0],
        code="answer_state_bundle_manifest_invalid",
    )
    queries = tuple(
        _model_from_record(
            StateBundleQuery,
            raw,
            code="answer_state_bundle_query_invalid",
        )
        for raw in records[1:]
    )
    if manifest.query_count != len(queries):
        raise GoldAnswerProducerError("answer_state_bundle_query_count_mismatch")
    return manifest, queries, binding


def _verify_semantic_state_chain(
    *,
    receipt: AnswerProducerReceipt,
    identity_model: ProducerStateIdentity,
    identity_binding: ArtifactBinding,
    state_bundle_manifest: StateBundleManifest,
    state_bundle_queries: Sequence[StateBundleQuery],
    identities: Sequence[QueryIdentity],
    inputs: AnswerInputDataset,
    decisions: DecisionDataset | None,
    answer_records: Sequence[AnswerRecord],
    ledger_entries: Sequence[CallLedgerEntry],
) -> None:
    if (
        state_bundle_manifest.state_identity != identity_binding
        or state_bundle_manifest.decision_mode != receipt.capture_mode
        or state_bundle_manifest.retrieval_corpus_inventory != receipt.retrieval_corpus_inventory
        or state_bundle_manifest.query_count != len(identities)
        or len(state_bundle_queries) != len(identities)
    ):
        raise GoldAnswerProducerError("answer_state_bundle_contract_mismatch")

    if receipt.capture_mode == "deterministic_extractive":
        if (
            decisions is not None
            or receipt.decision_artifact is not None
            or receipt.provider_id != "builtin-deterministic-extractive-v1"
            or not identity_model.decision_release_eligible
        ):
            raise GoldAnswerProducerError("answer_state_decision_provenance_mismatch")
    elif receipt.capture_mode == "sealed_decisions":
        if decisions is None:
            raise GoldAnswerProducerError("answer_decisions_missing")
        expected_provider_id = f"{decisions.manifest.decision_authority}:{decisions.binding.sha256}"
        if (
            receipt.decision_artifact != decisions.binding
            or receipt.provider_id != expected_provider_id
            or identity_model.decision_release_eligible != decisions.manifest.release_eligible
            or set(decisions.records) != {identity.query_id for identity in identities}
        ):
            raise GoldAnswerProducerError("answer_state_decision_provenance_mismatch")
    elif decisions is not None or receipt.decision_artifact is not None:
        raise GoldAnswerProducerError("answer_state_decision_provenance_mismatch")

    expected_ledger: list[CallLedgerEntry] = []
    for query_index, (identity, query_input, bundled, final_record) in enumerate(
        zip(
            identities,
            inputs.queries,
            state_bundle_queries,
            answer_records,
            strict=True,
        )
    ):
        request = build_answer_request(identity, query_input, inputs.manifest)
        request_sha256 = hashlib.sha256(request.canonical_bytes()).hexdigest()
        shard = bundled.shard
        decision_sha256 = hashlib.sha256(shard.decision.canonical_bytes()).hexdigest()
        if (
            bundled.query_index != query_index
            or bundled.query_id != identity.query_id
            or shard.query_index != query_index
            or shard.query_id != identity.query_id
            or shard.request_sha256 != request_sha256
            or shard.decision_mode != receipt.capture_mode
            or shard.decision_sha256 != decision_sha256
            or shard.record.query_id != identity.query_id
            or shard.record.query_sha256 != identity.query_sha256
        ):
            raise GoldAnswerProducerError(
                "answer_state_shard_binding_mismatch",
                line=query_index + 2,
            )

        if receipt.capture_mode == "deterministic_extractive":
            expected_decision = _deterministic_decision(request)
            if bundled.reservation is not None or shard.logical_call_index is not None:
                raise GoldAnswerProducerError(
                    "unexpected_answer_call_reservation",
                    line=query_index + 2,
                )
        elif receipt.capture_mode == "sealed_decisions":
            if decisions is None:  # pragma: no cover - checked above
                raise GoldAnswerProducerError("answer_decisions_missing")
            sealed = decisions.records.get(identity.query_id)
            if sealed is None or sealed.request_sha256 != request_sha256:
                raise GoldAnswerProducerError(
                    "answer_decisions_query_binding_mismatch",
                    line=query_index + 2,
                )
            expected_decision = sealed.decision
            if bundled.reservation is not None or shard.logical_call_index is not None:
                raise GoldAnswerProducerError(
                    "unexpected_answer_call_reservation",
                    line=query_index + 2,
                )
        else:
            reservation = bundled.reservation
            if (
                reservation is None
                or shard.logical_call_index != reservation.logical_call_index
                or reservation.logical_call_index != len(expected_ledger) + 1
                or reservation.query_id != identity.query_id
                or reservation.request_sha256 != request_sha256
                or reservation.idempotency_key != request.idempotency_key
                or reservation.provider_id != receipt.provider_id
            ):
                raise GoldAnswerProducerError(
                    "answer_call_reservation_binding_mismatch",
                    line=query_index + 2,
                )
            expected_decision = shard.decision
            expected_ledger.append(
                CallLedgerEntry(
                    schema_version=CALL_LEDGER_ENTRY_SCHEMA,
                    logical_call_index=reservation.logical_call_index,
                    query_id=identity.query_id,
                    request_sha256=request_sha256,
                    idempotency_key=request.idempotency_key,
                    provider_id=receipt.provider_id,
                    decision_sha256=decision_sha256,
                )
            )

        expected_answer = _answer_from_decision(request, expected_decision)
        expected_record = AnswerRecord(
            schema_version="cardrag.gold-answer.v1",
            query_id=identity.query_id,
            query_sha256=identity.query_sha256,
            answer=expected_answer,
        )
        if (
            shard.decision != expected_decision
            or shard.record != expected_record
            or final_record != expected_record
        ):
            raise GoldAnswerProducerError(
                "answer_state_decision_provenance_mismatch",
                line=query_index + 2,
            )

    if (
        state_bundle_manifest.reservation_count != len(expected_ledger)
        or tuple(ledger_entries) != tuple(expected_ledger)
        or receipt.logical_provider_call_count != len(expected_ledger)
    ):
        raise GoldAnswerProducerError("answer_call_ledger_semantic_mismatch")


def verify_answer_producer_receipt_portable(
    *,
    receipt_path: Path,
    expected_receipt_sha256: str,
    gold_path: Path,
    expected_gold_sha256: str,
    input_path: Path,
    expected_input_sha256: str,
    answer_path: Path,
    expected_answer_sha256: str,
    ledger_path: Path,
    state_identity_path: Path,
    state_bundle_path: Path,
    expected_lane: AnswerLane,
    expected_source_commit: str,
    expected_generation_id: str,
    expected_generation_manifest_sha256: str,
    expected_answer_profile_id: str,
    retrieval_corpus_inventory_path: Path | None = None,
    expected_retrieval_corpus_inventory_sha256: str | None = None,
    decision_path: Path | None = None,
    expected_decision_sha256: str | None = None,
    release_gate: bool = True,
) -> AnswerProducerReceipt:
    """Replay a receipt from compact, repository-portable answer evidence only.

    Generation databases, vector sidecars, and retrieval score matrices are
    deliberately not opened here. The compact corpus inventory is independently
    pinned; remaining immutable bindings were established by
    :func:`verify_answer_producer_receipt` before receipt issuance and are
    checked transitively across the answer input, state identity, and receipt.
    """

    receipt = load_answer_producer_receipt(
        receipt_path,
        expected_sha256=expected_receipt_sha256,
    )
    gold = _load_gold_dataset(
        gold_path,
        expected_sha256=expected_gold_sha256,
        release_gate=release_gate,
    )
    identities = _query_identities(gold)
    inputs = _load_answer_inputs(
        input_path,
        expected_sha256=expected_input_sha256,
        identities=identities,
    )

    answer_payload = _read_regular(
        answer_path,
        maximum_bytes=_MAX_PORTABLE_ARTIFACT_BYTES,
        code="answer_artifact",
    )
    answer_binding = _binding(answer_payload)
    if answer_binding.sha256 != expected_answer_sha256:
        raise GoldAnswerProducerError("answer_artifact_sha256_mismatch")
    raw_answer_records = _canonical_jsonl(answer_payload, code="answer_artifact")
    answer_manifest = _model_from_record(
        AnswerArtifactManifest,
        raw_answer_records[0],
        code="answer_artifact_manifest_invalid",
    )
    if len(raw_answer_records) != len(identities) + 1:
        raise GoldAnswerProducerError("answer_artifact_query_count_mismatch")
    answer_records: list[AnswerRecord] = []
    for line, (identity, raw) in enumerate(
        zip(identities, raw_answer_records[1:], strict=True),
        start=2,
    ):
        record = _model_from_record(
            AnswerRecord,
            raw,
            code="answer_artifact_record_invalid",
        )
        if record.query_id != identity.query_id or record.query_sha256 != identity.query_sha256:
            raise GoldAnswerProducerError("answer_artifact_query_binding_mismatch", line=line)
        answer_records.append(record)

    ledger_payload = _read_regular(
        ledger_path,
        maximum_bytes=_MAX_PORTABLE_ARTIFACT_BYTES,
        code="answer_call_ledger",
    )
    ledger_binding = _binding(ledger_payload)
    raw_ledger_records = _canonical_jsonl(ledger_payload, code="answer_call_ledger")
    ledger_manifest = _model_from_record(
        CallLedgerManifest,
        raw_ledger_records[0],
        code="answer_call_ledger_manifest_invalid",
    )
    ledger_entries = tuple(
        _model_from_record(
            CallLedgerEntry,
            raw,
            code="answer_call_ledger_entry_invalid",
        )
        for raw in raw_ledger_records[1:]
    )

    identity_payload = _read_regular(
        state_identity_path,
        maximum_bytes=_MAX_LINE_BYTES,
        code="answer_state_identity",
    )
    try:
        identity_model = ProducerStateIdentity.model_validate_json(identity_payload)
    except (ValidationError, ValueError, TypeError) as exc:
        raise GoldAnswerProducerError("answer_state_identity_invalid") from exc
    if identity_payload != identity_model.canonical_bytes() + b"\n":
        raise GoldAnswerProducerError("answer_state_identity_not_canonical")
    identity_binding = _binding(identity_payload)
    state_bundle_manifest, state_bundle_queries, state_bundle_binding = _load_state_bundle(
        state_bundle_path,
        expected=receipt.state_bundle,
    )

    decisions: DecisionDataset | None = None
    if decision_path is None:
        if expected_decision_sha256 is not None or receipt.decision_artifact is not None:
            raise GoldAnswerProducerError("answer_decision_artifact_binding_mismatch")
    else:
        if expected_decision_sha256 is None:
            raise GoldAnswerProducerError("expected_decision_sha256_required")
        decisions = _load_decisions(
            decision_path,
            expected_sha256=expected_decision_sha256,
            inputs=inputs,
        )
        if decisions.binding != receipt.decision_artifact:
            raise GoldAnswerProducerError("answer_decision_artifact_binding_mismatch")

    source = inputs.manifest
    corpus_inventory_binding = _load_named_sidecar(
        path=retrieval_corpus_inventory_path,
        expected_sha256=expected_retrieval_corpus_inventory_sha256,
        expected=source.retrieval_corpus_inventory,
        code="retrieval_corpus_inventory",
    )
    if (
        receipt.lane != expected_lane
        or source.lane != expected_lane
        or receipt.release_eligible != release_gate
        or receipt.source_commit != expected_source_commit
        or source.source_commit != expected_source_commit
        or receipt.gold_sha256 != gold.sha256
        or source.gold_sha256 != gold.sha256
        or receipt.query_count != len(identities)
        or source.query_count != len(identities)
        or receipt.generation_id != expected_generation_id
        or source.generation_id != expected_generation_id
        or receipt.generation_manifest.sha256 != expected_generation_manifest_sha256
        or source.generation_manifest_sha256 != expected_generation_manifest_sha256
        or receipt.serving_database != source.serving_database
        or receipt.capture_input != inputs.binding
        or receipt.answer_profile_id != expected_answer_profile_id
        or source.answer_profile_id != expected_answer_profile_id
        or receipt.answer_artifact != answer_binding
        or receipt.call_ledger != ledger_binding
        or receipt.state_identity != identity_binding
        or receipt.state_bundle != state_bundle_binding
        or receipt.retrieval_contract != source.retrieval_contract
        or receipt.retrieval_capture_phase != source.retrieval_capture_phase
        or receipt.retrieval_run != source.retrieval_run
        or receipt.retrieval_capture_receipt != source.retrieval_capture_receipt
        or receipt.retrieval_attestation_artifact != source.retrieval_attestation_artifact
        or receipt.retrieval_raw_score_artifact != source.retrieval_raw_score_artifact
        or receipt.retrieval_corpus_inventory != corpus_inventory_binding
        or receipt.retrieval_dense_score_matrix != source.retrieval_dense_score_matrix
        or receipt.retrieval_query_vector_matrix != source.retrieval_query_vector_matrix
        or receipt.retrieval_lexical_rank_artifact != source.retrieval_lexical_rank_artifact
        or (release_gate and source.retrieval_contract != RANKING_PROJECTION_SCHEMA)
        or answer_manifest.lane != expected_lane
        or answer_manifest.gold_sha256 != gold.sha256
        or answer_manifest.query_count != len(identities)
        or answer_manifest.generation_id != expected_generation_id
        or answer_manifest.generation_manifest_sha256 != expected_generation_manifest_sha256
        or answer_manifest.answer_profile_id != expected_answer_profile_id
        or ledger_manifest.source_commit != receipt.source_commit
        or ledger_manifest.gold_sha256 != receipt.gold_sha256
        or ledger_manifest.capture_input_sha256 != receipt.capture_input.sha256
        or ledger_manifest.generation_id != receipt.generation_id
        or ledger_manifest.generation_manifest_sha256 != receipt.generation_manifest.sha256
        or ledger_manifest.answer_profile_id != receipt.answer_profile_id
        or ledger_manifest.provider_id != receipt.provider_id
        or ledger_manifest.query_count != receipt.query_count
        or ledger_manifest.maximum_provider_calls != receipt.maximum_provider_calls
        or ledger_manifest.logical_provider_call_count != receipt.logical_provider_call_count
        or len(ledger_entries) != receipt.logical_provider_call_count
        or tuple(entry.logical_call_index for entry in ledger_entries)
        != tuple(range(1, len(ledger_entries) + 1))
        or identity_model.decision_mode != receipt.capture_mode
        or identity_model.decision_artifact != receipt.decision_artifact
        or identity_model.provider_id != receipt.provider_id
        or identity_model.maximum_provider_calls != receipt.maximum_provider_calls
        or identity_model.source_commit != receipt.source_commit
        or identity_model.gold_sha256 != receipt.gold_sha256
        or identity_model.capture_input_sha256 != receipt.capture_input.sha256
        or identity_model.generation_id != receipt.generation_id
        or identity_model.generation_manifest_sha256 != receipt.generation_manifest.sha256
        or identity_model.serving_database != receipt.serving_database
        or identity_model.retrieval_contract != receipt.retrieval_contract
        or identity_model.retrieval_capture_phase != receipt.retrieval_capture_phase
        or identity_model.retrieval_run != receipt.retrieval_run
        or identity_model.retrieval_capture_receipt != receipt.retrieval_capture_receipt
        or identity_model.retrieval_attestation_artifact != receipt.retrieval_attestation_artifact
        or identity_model.retrieval_raw_score_artifact != receipt.retrieval_raw_score_artifact
        or identity_model.retrieval_corpus_inventory != receipt.retrieval_corpus_inventory
        or identity_model.retrieval_dense_score_matrix != receipt.retrieval_dense_score_matrix
        or identity_model.retrieval_query_vector_matrix != receipt.retrieval_query_vector_matrix
        or identity_model.retrieval_lexical_rank_artifact != receipt.retrieval_lexical_rank_artifact
        or identity_model.answer_profile_id != receipt.answer_profile_id
        or identity_model.query_count != receipt.query_count
        or (receipt.capture_mode != "provider" and receipt.logical_provider_call_count != 0)
        or (release_gate and not identity_model.decision_release_eligible)
    ):
        raise GoldAnswerProducerError("answer_receipt_contract_mismatch")

    _verify_semantic_state_chain(
        receipt=receipt,
        identity_model=identity_model,
        identity_binding=identity_binding,
        state_bundle_manifest=state_bundle_manifest,
        state_bundle_queries=state_bundle_queries,
        identities=identities,
        inputs=inputs,
        decisions=decisions,
        answer_records=answer_records,
        ledger_entries=ledger_entries,
    )
    return receipt


def verify_answer_producer_receipt(
    *,
    receipt_path: Path,
    expected_receipt_sha256: str,
    gold_path: Path,
    expected_gold_sha256: str,
    input_path: Path,
    expected_input_sha256: str,
    generation_manifest_path: Path,
    database_path: Path,
    answer_path: Path,
    expected_answer_sha256: str,
    ledger_path: Path,
    state_identity_path: Path,
    state_bundle_path: Path,
    expected_lane: AnswerLane,
    expected_source_commit: str,
    expected_generation_id: str,
    expected_generation_manifest_sha256: str,
    expected_answer_profile_id: str,
    retrieval_run_path: Path | None = None,
    expected_retrieval_run_sha256: str | None = None,
    retrieval_capture_receipt_path: Path | None = None,
    expected_retrieval_capture_receipt_sha256: str | None = None,
    retrieval_attestation_path: Path | None = None,
    expected_retrieval_attestation_sha256: str | None = None,
    retrieval_raw_score_path: Path | None = None,
    expected_retrieval_raw_score_sha256: str | None = None,
    retrieval_corpus_inventory_path: Path | None = None,
    expected_retrieval_corpus_inventory_sha256: str | None = None,
    retrieval_dense_score_matrix_path: Path | None = None,
    expected_retrieval_dense_score_matrix_sha256: str | None = None,
    retrieval_query_vector_matrix_path: Path | None = None,
    expected_retrieval_query_vector_matrix_sha256: str | None = None,
    retrieval_lexical_rank_path: Path | None = None,
    expected_retrieval_lexical_rank_sha256: str | None = None,
    decision_path: Path | None = None,
    expected_decision_sha256: str | None = None,
    release_gate: bool = True,
) -> AnswerProducerReceipt:
    """Fail closed over the complete answer/retrieval/decision provenance chain."""

    receipt = load_answer_producer_receipt(
        receipt_path,
        expected_sha256=expected_receipt_sha256,
    )
    gold = _load_gold_dataset(
        gold_path,
        expected_sha256=expected_gold_sha256,
        release_gate=release_gate,
    )
    identities = _query_identities(gold)
    inputs = _load_answer_inputs(
        input_path,
        expected_sha256=expected_input_sha256,
        identities=identities,
    )
    generation = _load_source_manifest(generation_manifest_path, lane=expected_lane)
    retrieval = _load_bound_retrieval(
        inputs=inputs,
        identities=identities,
        source=generation,
        retrieval_run_path=retrieval_run_path,
        expected_retrieval_run_sha256=expected_retrieval_run_sha256,
        retrieval_capture_receipt_path=retrieval_capture_receipt_path,
        expected_retrieval_capture_receipt_sha256=(expected_retrieval_capture_receipt_sha256),
        retrieval_attestation_path=retrieval_attestation_path,
        expected_retrieval_attestation_sha256=expected_retrieval_attestation_sha256,
        retrieval_raw_score_path=retrieval_raw_score_path,
        expected_retrieval_raw_score_sha256=expected_retrieval_raw_score_sha256,
        retrieval_corpus_inventory_path=retrieval_corpus_inventory_path,
        expected_retrieval_corpus_inventory_sha256=(expected_retrieval_corpus_inventory_sha256),
        retrieval_dense_score_matrix_path=retrieval_dense_score_matrix_path,
        expected_retrieval_dense_score_matrix_sha256=(expected_retrieval_dense_score_matrix_sha256),
        retrieval_query_vector_matrix_path=retrieval_query_vector_matrix_path,
        expected_retrieval_query_vector_matrix_sha256=(
            expected_retrieval_query_vector_matrix_sha256
        ),
        retrieval_lexical_rank_path=retrieval_lexical_rank_path,
        expected_retrieval_lexical_rank_sha256=expected_retrieval_lexical_rank_sha256,
        release_gate=release_gate,
    )
    with _sqlite_readonly(database_path, expected=inputs.manifest.serving_database) as connection:
        _verify_source_database(
            connection,
            manifest=inputs.manifest,
            inputs=inputs.queries,
            expected_row_count=generation.row_count,
            expected_page_embedding_profile_id=generation.embedding_profile_id,
            source_manifest=generation,
        )
    answer_payload = _read_regular(
        answer_path,
        maximum_bytes=_MAX_PORTABLE_ARTIFACT_BYTES,
        code="answer_artifact",
    )
    answer_binding = _binding(answer_payload)
    if answer_binding.sha256 != expected_answer_sha256:
        raise GoldAnswerProducerError("answer_artifact_sha256_mismatch")
    answer_records = _canonical_jsonl(answer_payload, code="answer_artifact")
    answer_manifest = _model_from_record(
        AnswerArtifactManifest,
        answer_records[0],
        code="answer_artifact_manifest_invalid",
    )
    if len(answer_records) != len(identities) + 1:
        raise GoldAnswerProducerError("answer_artifact_query_count_mismatch")
    parsed_answer_records: list[AnswerRecord] = []
    for line, (identity, raw) in enumerate(
        zip(identities, answer_records[1:], strict=True),
        start=2,
    ):
        record = _model_from_record(AnswerRecord, raw, code="answer_artifact_record_invalid")
        if record.query_id != identity.query_id or record.query_sha256 != identity.query_sha256:
            raise GoldAnswerProducerError("answer_artifact_query_binding_mismatch", line=line)
        parsed_answer_records.append(record)
    ledger_payload = _read_regular(
        ledger_path,
        maximum_bytes=_MAX_PORTABLE_ARTIFACT_BYTES,
        code="answer_call_ledger",
    )
    ledger_binding = _binding(ledger_payload)
    ledger_records = _canonical_jsonl(ledger_payload, code="answer_call_ledger")
    ledger_manifest = _model_from_record(
        CallLedgerManifest,
        ledger_records[0],
        code="answer_call_ledger_manifest_invalid",
    )
    ledger_entries = tuple(
        _model_from_record(
            CallLedgerEntry,
            raw,
            code="answer_call_ledger_entry_invalid",
        )
        for raw in ledger_records[1:]
    )
    identity_payload = _read_regular(
        state_identity_path,
        maximum_bytes=_MAX_LINE_BYTES,
        code="answer_state_identity",
    )
    try:
        identity_model = ProducerStateIdentity.model_validate_json(identity_payload)
    except (ValidationError, ValueError, TypeError) as exc:
        raise GoldAnswerProducerError("answer_state_identity_invalid") from exc
    if identity_payload != identity_model.canonical_bytes() + b"\n":
        raise GoldAnswerProducerError("answer_state_identity_not_canonical")
    identity_binding = _binding(identity_payload)
    state_bundle_manifest, state_bundle_queries, state_bundle_binding = _load_state_bundle(
        state_bundle_path,
        expected=receipt.state_bundle,
    )
    decisions: DecisionDataset | None = None
    if decision_path is None:
        if expected_decision_sha256 is not None or receipt.decision_artifact is not None:
            raise GoldAnswerProducerError("answer_decision_artifact_binding_mismatch")
    else:
        if expected_decision_sha256 is None:
            raise GoldAnswerProducerError("expected_decision_sha256_required")
        decisions = _load_decisions(
            decision_path,
            expected_sha256=expected_decision_sha256,
            inputs=inputs,
        )
        if decisions.binding != receipt.decision_artifact:
            raise GoldAnswerProducerError("answer_decision_artifact_binding_mismatch")
    expected_run = None if retrieval is None else retrieval.run_binding
    expected_capture = None if retrieval is None else retrieval.receipt_binding
    expected_attestation = None if retrieval is None else retrieval.attestation_binding
    expected_raw = None if retrieval is None else retrieval.raw_score_binding
    expected_inventory = None if retrieval is None else retrieval.corpus_inventory_binding
    expected_dense = None if retrieval is None else retrieval.dense_score_matrix_binding
    expected_vectors = None if retrieval is None else retrieval.query_vector_matrix_binding
    expected_lexical = None if retrieval is None else retrieval.lexical_rank_binding
    if (
        receipt.lane != expected_lane
        or receipt.release_eligible != release_gate
        or receipt.source_commit != expected_source_commit
        or receipt.gold_sha256 != gold.sha256
        or receipt.query_count != len(identities)
        or receipt.generation_id != expected_generation_id
        or receipt.generation_manifest != generation.binding
        or generation.binding.sha256 != expected_generation_manifest_sha256
        or receipt.serving_database != inputs.manifest.serving_database
        or receipt.capture_input != inputs.binding
        or receipt.answer_profile_id != expected_answer_profile_id
        or receipt.answer_artifact != answer_binding
        or receipt.call_ledger != ledger_binding
        or receipt.state_identity != identity_binding
        or receipt.state_bundle != state_bundle_binding
        or receipt.retrieval_contract != inputs.manifest.retrieval_contract
        or receipt.retrieval_capture_phase != inputs.manifest.retrieval_capture_phase
        or receipt.retrieval_run != expected_run
        or receipt.retrieval_capture_receipt != expected_capture
        or receipt.retrieval_attestation_artifact != expected_attestation
        or receipt.retrieval_raw_score_artifact != expected_raw
        or receipt.retrieval_corpus_inventory != expected_inventory
        or receipt.retrieval_dense_score_matrix != expected_dense
        or receipt.retrieval_query_vector_matrix != expected_vectors
        or receipt.retrieval_lexical_rank_artifact != expected_lexical
        or answer_manifest.lane != expected_lane
        or answer_manifest.gold_sha256 != gold.sha256
        or answer_manifest.query_count != len(identities)
        or answer_manifest.generation_id != expected_generation_id
        or answer_manifest.generation_manifest_sha256 != expected_generation_manifest_sha256
        or answer_manifest.answer_profile_id != expected_answer_profile_id
        or ledger_manifest.source_commit != receipt.source_commit
        or ledger_manifest.gold_sha256 != receipt.gold_sha256
        or ledger_manifest.capture_input_sha256 != receipt.capture_input.sha256
        or ledger_manifest.generation_id != receipt.generation_id
        or ledger_manifest.generation_manifest_sha256 != receipt.generation_manifest.sha256
        or ledger_manifest.answer_profile_id != receipt.answer_profile_id
        or ledger_manifest.provider_id != receipt.provider_id
        or ledger_manifest.query_count != receipt.query_count
        or ledger_manifest.maximum_provider_calls != receipt.maximum_provider_calls
        or ledger_manifest.logical_provider_call_count != receipt.logical_provider_call_count
        or len(ledger_entries) != receipt.logical_provider_call_count
        or tuple(entry.logical_call_index for entry in ledger_entries)
        != tuple(range(1, len(ledger_entries) + 1))
        or identity_model.decision_mode != receipt.capture_mode
        or identity_model.decision_artifact != receipt.decision_artifact
        or identity_model.provider_id != receipt.provider_id
        or identity_model.maximum_provider_calls != receipt.maximum_provider_calls
        or identity_model.source_commit != receipt.source_commit
        or identity_model.gold_sha256 != receipt.gold_sha256
        or identity_model.capture_input_sha256 != receipt.capture_input.sha256
        or identity_model.generation_id != receipt.generation_id
        or identity_model.generation_manifest_sha256 != receipt.generation_manifest.sha256
        or identity_model.serving_database != receipt.serving_database
        or identity_model.retrieval_contract != receipt.retrieval_contract
        or identity_model.retrieval_capture_phase != receipt.retrieval_capture_phase
        or identity_model.retrieval_run != receipt.retrieval_run
        or identity_model.retrieval_capture_receipt != receipt.retrieval_capture_receipt
        or identity_model.retrieval_attestation_artifact != receipt.retrieval_attestation_artifact
        or identity_model.retrieval_raw_score_artifact != receipt.retrieval_raw_score_artifact
        or identity_model.retrieval_corpus_inventory != receipt.retrieval_corpus_inventory
        or identity_model.retrieval_dense_score_matrix != receipt.retrieval_dense_score_matrix
        or identity_model.retrieval_query_vector_matrix != receipt.retrieval_query_vector_matrix
        or identity_model.retrieval_lexical_rank_artifact != receipt.retrieval_lexical_rank_artifact
        or identity_model.answer_profile_id != receipt.answer_profile_id
        or identity_model.query_count != receipt.query_count
        or (receipt.capture_mode != "provider" and receipt.logical_provider_call_count != 0)
        or (release_gate and not identity_model.decision_release_eligible)
    ):
        raise GoldAnswerProducerError("answer_receipt_contract_mismatch")
    _verify_semantic_state_chain(
        receipt=receipt,
        identity_model=identity_model,
        identity_binding=identity_binding,
        state_bundle_manifest=state_bundle_manifest,
        state_bundle_queries=state_bundle_queries,
        identities=identities,
        inputs=inputs,
        decisions=decisions,
        answer_records=parsed_answer_records,
        ledger_entries=ledger_entries,
    )
    return receipt


class _StrictProviderIdentity(_StrictModel):
    provider_id: Identifier


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Produce a sealed CardRAG gold answer artifact")
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--expected-gold-sha256", required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--expected-input-sha256", required=True)
    parser.add_argument("--generation-manifest", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--retrieval-run", type=Path)
    parser.add_argument("--expected-retrieval-run-sha256")
    parser.add_argument("--retrieval-capture-receipt", type=Path)
    parser.add_argument("--expected-retrieval-capture-receipt-sha256")
    parser.add_argument("--retrieval-attestation", type=Path)
    parser.add_argument("--expected-retrieval-attestation-sha256")
    parser.add_argument("--retrieval-raw-score", type=Path)
    parser.add_argument("--expected-retrieval-raw-score-sha256")
    parser.add_argument("--retrieval-corpus-inventory", type=Path)
    parser.add_argument("--expected-retrieval-corpus-inventory-sha256")
    parser.add_argument("--retrieval-dense-score-matrix", type=Path)
    parser.add_argument("--expected-retrieval-dense-score-matrix-sha256")
    parser.add_argument("--retrieval-query-vector-matrix", type=Path)
    parser.add_argument("--expected-retrieval-query-vector-matrix-sha256")
    parser.add_argument("--retrieval-lexical-ranks", type=Path)
    parser.add_argument("--expected-retrieval-lexical-ranks-sha256")
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--expected-source-commit")
    parser.add_argument("--expected-answer-profile-id", required=True)
    parser.add_argument("--maximum-provider-calls", type=int, default=MAX_RELEASE_QUERIES)
    parser.add_argument("--fixture-mode", action="store_true")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--deterministic-extractive", action="store_true")
    mode.add_argument("--decisions", type=Path)
    parser.add_argument("--expected-decisions-sha256")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    arguments = _parser().parse_args(argv)
    try:
        result = produce_answer_artifact(
            gold_path=arguments.gold,
            expected_gold_sha256=str(arguments.expected_gold_sha256),
            input_path=arguments.input,
            expected_input_sha256=str(arguments.expected_input_sha256),
            generation_manifest_path=arguments.generation_manifest,
            database_path=arguments.database,
            state_directory=arguments.state_dir,
            answer_path=arguments.output,
            ledger_path=arguments.ledger,
            receipt_path=arguments.receipt,
            expected_source_commit=arguments.expected_source_commit,
            expected_answer_profile_id=str(arguments.expected_answer_profile_id),
            retrieval_run_path=arguments.retrieval_run,
            expected_retrieval_run_sha256=arguments.expected_retrieval_run_sha256,
            retrieval_capture_receipt_path=arguments.retrieval_capture_receipt,
            expected_retrieval_capture_receipt_sha256=(
                arguments.expected_retrieval_capture_receipt_sha256
            ),
            retrieval_attestation_path=arguments.retrieval_attestation,
            expected_retrieval_attestation_sha256=(arguments.expected_retrieval_attestation_sha256),
            retrieval_raw_score_path=arguments.retrieval_raw_score,
            expected_retrieval_raw_score_sha256=(arguments.expected_retrieval_raw_score_sha256),
            retrieval_corpus_inventory_path=arguments.retrieval_corpus_inventory,
            expected_retrieval_corpus_inventory_sha256=(
                arguments.expected_retrieval_corpus_inventory_sha256
            ),
            retrieval_dense_score_matrix_path=(arguments.retrieval_dense_score_matrix),
            expected_retrieval_dense_score_matrix_sha256=(
                arguments.expected_retrieval_dense_score_matrix_sha256
            ),
            retrieval_query_vector_matrix_path=(arguments.retrieval_query_vector_matrix),
            expected_retrieval_query_vector_matrix_sha256=(
                arguments.expected_retrieval_query_vector_matrix_sha256
            ),
            retrieval_lexical_rank_path=arguments.retrieval_lexical_ranks,
            expected_retrieval_lexical_rank_sha256=(
                arguments.expected_retrieval_lexical_ranks_sha256
            ),
            deterministic=bool(arguments.deterministic_extractive),
            decision_path=arguments.decisions,
            expected_decision_sha256=arguments.expected_decisions_sha256,
            maximum_provider_calls=int(arguments.maximum_provider_calls),
            release_gate=not bool(arguments.fixture_mode),
        )
    except GoldAnswerProducerError as exc:
        suffix = "" if exc.line is None else f":line={exc.line}"
        print(f"gold-answer-producer:{exc.code}{suffix}", file=sys.stderr)
        raise SystemExit(2) from None
    print(result.receipt.canonical_bytes().decode("utf-8"))


if __name__ == "__main__":  # pragma: no cover
    main()
