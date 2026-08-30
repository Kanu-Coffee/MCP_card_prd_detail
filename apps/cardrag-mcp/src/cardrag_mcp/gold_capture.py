"""Fail-closed producers for the five CardRAG v1.0.10 gold lanes.

The offline evaluator intentionally does not call serving code.  This module is
the other half of that boundary: it captures native v5 exact, lexical, and
reranker observations, or seals a reproducible external observation for the two
historical/page lanes which have no producer in the v1.0.10 runtime.

Nothing in this module is imported by the MCP server.  It never follows a
generation pointer and it publishes only immutable, canonical evidence files.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import mmap
import os
import re
import sqlite3
import stat
import sys
import tempfile
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol, Self, cast
from urllib.parse import urlsplit

import numpy as np
import numpy.typing as npt
from cardrag_core import GenerationManifest, canonical_json_bytes, canonical_sha256
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from cardrag_mcp.aggregation_profile import (
    MAX_SCORE_ARTIFACT_BYTES,
    MAX_SCORE_COUNT,
    AggregationProfileError,
    OpenedScoreArtifact,
    QueryScoreCoverage,
    open_score_artifact,
)
from cardrag_mcp.aggregation_profile import (
    ArtifactBinding as ScoreArtifactBinding,
)
from cardrag_mcp.embeddings import OpenRouterEmbedder
from cardrag_mcp.evaluation import (
    LANES,
    MAX_JSONL_BYTES,
    MAX_RELEASE_QUERIES,
    MIN_RELEASE_QUERIES,
    V109_BASELINE_COMMIT,
    EvaluatedAnswer,
    EvaluationLane,
    GoldDataset,
    GoldQuery,
    QueryRunResult,
    RetrievedContract,
    RetrievedSpan,
    RunArtifactManifest,
    RunDataset,
    ShadowObservation,
    V109BaselineObservation,
    load_gold_jsonl,
    load_run_jsonl,
)
from cardrag_mcp.exact import VECTOR_BLOCK_ROWS, ExactCapturedRow, V5ExactRepository
from cardrag_mcp.models import ContractSearchRequest, DocumentAggregationPolicy, ViewType
from cardrag_mcp.reranker import (
    RERANKER_MODEL,
    OpenRouterReranker,
    RerankerShadowArtifact,
    RerankerShadowLane,
    RerankerShadowStore,
)
from cardrag_mcp.store import load_generation_handle

Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Identifier = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,511}$"),
]
SourceCommit = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{40}([0-9a-f]{24})?$"),
]
type NativeV5Lane = Literal["qwen_structure_exact", "lexical_shadow", "reranker_shadow"]
type ShadowLane = Literal["lexical_shadow", "reranker_shadow"]
type AnswerLane = Literal["v109_baseline", "qwen_page", "qwen_structure_exact"]
NATIVE_V5_LANES: tuple[NativeV5Lane, ...] = (
    "qwen_structure_exact",
    "lexical_shadow",
    "reranker_shadow",
)
SHADOW_LANES: tuple[ShadowLane, ...] = ("lexical_shadow", "reranker_shadow")

CAPTURE_RECEIPT_SCHEMA: Literal["cardrag.gold-lane-capture-receipt.v2"] = (
    "cardrag.gold-lane-capture-receipt.v2"
)
CAPTURE_SET_RECEIPT_SCHEMA: Literal["cardrag.gold-capture-set-receipt.v2"] = (
    "cardrag.gold-capture-set-receipt.v2"
)
EXTERNAL_MANIFEST_SCHEMA: Literal["cardrag.gold-external-observation-artifact.v2"] = (
    "cardrag.gold-external-observation-artifact.v2"
)
EXTERNAL_QUERY_SCHEMA: Literal["cardrag.gold-external-query-observation.v2"] = (
    "cardrag.gold-external-query-observation.v2"
)
NATIVE_V5_MANIFEST_SCHEMA: Literal["cardrag.gold-native-v5-attestation.v2"] = (
    "cardrag.gold-native-v5-attestation.v2"
)
NATIVE_V5_QUERY_SCHEMA: Literal["cardrag.gold-native-v5-query-attestation.v2"] = (
    "cardrag.gold-native-v5-query-attestation.v2"
)
NATIVE_QUERY_SHARD_SCHEMA: Literal["cardrag.gold-native-v5-query-shard.v2"] = (
    "cardrag.gold-native-v5-query-shard.v2"
)
ANSWER_MANIFEST_SCHEMA = "cardrag.gold-answer-artifact.v1"
ANSWER_RECORD_SCHEMA = "cardrag.gold-answer.v1"
ANSWER_EVIDENCE_BINDINGS_SCHEMA: Literal["cardrag.gold-answer-evidence-bindings.v1"] = (
    "cardrag.gold-answer-evidence-bindings.v1"
)
NATIVE_CAPTURE_IDENTITY_SCHEMA: Literal["cardrag.gold-native-v5-capture-identity.v2"] = (
    "cardrag.gold-native-v5-capture-identity.v2"
)

PAGE_GENERATION_SCHEMA: Literal["cardrag.evaluation-page-generation.v2"] = (
    "cardrag.evaluation-page-generation.v2"
)
PAGE_SOURCE_TEXT_CONTRACT: Literal["cardrag.page-source-text-range.v1"] = (
    "cardrag.page-source-text-range.v1"
)
PAGE_COLUMN_CONTRACT: Literal["cardrag.evaluation-page-columns.v1"] = (
    "cardrag.evaluation-page-columns.v1"
)

_MAX_CAPTURE_LINE_BYTES = 256 * 1024 * 1024
_MAX_MANIFEST_BYTES = 32 * 1024 * 1024
_MAX_DATABASE_BYTES = 4 * 1024 * 1024 * 1024
_MAX_SIDECAR_BYTES = 64 * 1024 * 1024 * 1024
_MAX_EXTERNAL_ROWS_PER_QUERY = 2_000_000
_MAX_EXTERNAL_SIDECAR_BYTES = 95_000_000
_MAX_EXTERNAL_ARTIFACT_BYTES = 95_000_000
_MAX_CAPTURE_STATE_FILE_BYTES = 64 * 1024 * 1024
_MAX_NATIVE_CAPTURE_CORPUS_ROWS = MAX_SCORE_COUNT // MIN_RELEASE_QUERIES
_SOURCE_COMMIT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_V109_PRESERVED_GENERATION_ID = "g-2208f0c6076649c4be915be1-d11f80f9af71"
_V109_PRESERVED_MANIFEST_SHA256 = "dd12487e4f92a2d84362322f04d027421540c6bda27659e46cf6af553e216002"
_V109_PRESERVED_MANIFEST_SIZE_BYTES = 542_209
_V109_PRESERVED_DATABASE_SHA256 = "d25be45bc5d39af6561e587635b08312913107b6f6416500da39ab9eb757d38f"
_V109_PRESERVED_DATABASE_SIZE_BYTES = 58_466_304
_PAGE_DATABASE_COLUMNS = (
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
)
_PAGE_DATABASE_METADATA_KEYS = frozenset(
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


class GoldCaptureError(RuntimeError):
    """A capture input or runtime observation cannot prove a release lane."""

    def __init__(self, code: str, *, line: int | None = None) -> None:
        self.code = code
        self.line = line
        super().__init__(code)


def _validated_expected_source_commit(
    value: str | None,
    *,
    release_gate: bool,
) -> str | None:
    if value is None:
        if release_gate:
            raise GoldCaptureError("expected_source_commit_required")
        return None
    if _SOURCE_COMMIT.fullmatch(value) is None:
        raise GoldCaptureError("expected_source_commit_invalid")
    return value


def _validated_source_commit(value: str) -> str:
    if _SOURCE_COMMIT.fullmatch(value) is None:
        raise GoldCaptureError("source_commit_invalid")
    return value


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self)


class ArtifactBinding(_StrictModel):
    sha256: Sha256Hex
    size_bytes: int = Field(gt=0)


class ExternalObservationManifest(_StrictModel):
    """Identity for a reproducible non-native lane capture."""

    schema_version: Literal["cardrag.gold-external-observation-artifact.v2"]
    lane: Literal["v109_baseline", "qwen_page"]
    capture_mode: Literal["external_reproducible"]
    synthetic: Literal[False]
    gold_sha256: Sha256Hex
    query_count: int = Field(ge=1, le=MAX_RELEASE_QUERIES)
    source_version: Literal["v1.0.9", "v1.0.10-candidate"]
    source_commit: SourceCommit
    generation_id: Identifier
    generation_manifest: ArtifactBinding
    serving_schema: Literal["cardrag.serving-db.v4", "cardrag.evaluation-page.v1"]
    serving_database: ArtifactBinding
    vector_artifact: ArtifactBinding | None = None
    embedding_model: Literal[
        "openai/text-embedding-3-small",
        "qwen/qwen3-embedding-8b",
    ]
    embedding_dimension: Literal[1536, 4096]
    embedding_profile_id: Identifier
    retrieval_policy: Literal["small_rrf", "qwen_page_window"]
    maximum_candidates: Literal[250] | None = None
    scoring_contract: Literal[
        "cardrag.v109-small-dense-rrf-capture.v1",
        "cardrag.qwen-page-exact-capture.v1",
    ]
    row_count: int = Field(ge=1)
    corpus_inventory_sha256: Sha256Hex
    dense_score_matrix: ArtifactBinding
    query_vector_matrix: ArtifactBinding
    lexical_rank_artifact: ArtifactBinding | None
    byte_order: Literal["little-endian"]
    scalar_type: Literal["float32"]
    matrix_order: Literal["row-major"]
    maximum_result_contracts: Literal[100]
    maximum_result_spans: Literal[100]
    maximum_dense_trace_contracts: Literal[100]
    maximum_dense_trace_spans: Literal[250]
    approximate: Literal[False]

    @model_validator(mode="after")
    def lane_profile_is_exact(self) -> Self:
        if self.lane == "v109_baseline":
            valid = (
                self.source_version == "v1.0.9"
                and self.source_commit == V109_BASELINE_COMMIT
                and self.serving_schema == "cardrag.serving-db.v4"
                and self.embedding_model == "openai/text-embedding-3-small"
                and self.embedding_dimension == 1536
                and self.retrieval_policy == "small_rrf"
                and self.maximum_candidates == 250
                and self.scoring_contract == "cardrag.v109-small-dense-rrf-capture.v1"
                and self.vector_artifact is None
                and self.lexical_rank_artifact is not None
            )
        else:
            valid = (
                self.source_version == "v1.0.10-candidate"
                and self.serving_schema == "cardrag.evaluation-page.v1"
                and self.embedding_model == "qwen/qwen3-embedding-8b"
                and self.embedding_dimension == 4096
                and self.retrieval_policy == "qwen_page_window"
                and self.maximum_candidates is None
                and self.scoring_contract == "cardrag.qwen-page-exact-capture.v1"
                and self.vector_artifact is not None
                and self.lexical_rank_artifact is None
            )
        if not valid:
            raise ValueError("external observation profile does not match its lane")
        if (
            self.dense_score_matrix.size_bytes != self.query_count * self.row_count * 4
            or self.query_vector_matrix.size_bytes
            != self.query_count * self.embedding_dimension * 4
            or self.dense_score_matrix.size_bytes > _MAX_EXTERNAL_SIDECAR_BYTES
            or self.query_vector_matrix.size_bytes > _MAX_EXTERNAL_SIDECAR_BYTES
            or (
                self.lexical_rank_artifact is not None
                and self.lexical_rank_artifact.size_bytes > _MAX_EXTERNAL_SIDECAR_BYTES
            )
        ):
            raise ValueError("external sidecar size does not match its declared shape")
        return self


class ExternalRawRow(_StrictModel):
    row_index: int = Field(ge=0)
    evidence_id: Identifier
    contract_revision_id: Identifier
    span_id: Identifier
    input_sha256: Sha256Hex
    dense_score: float = Field(ge=-1.0, le=1.0)
    dense_rank: int = Field(ge=1)
    lexical_rank: int | None = Field(default=None, ge=1)


class ExternalQueryObservation(_StrictModel):
    schema_version: Literal["cardrag.gold-external-query-observation.v2"]
    ordinal: int = Field(ge=0, lt=MAX_RELEASE_QUERIES)
    lane: Literal["v109_baseline", "qwen_page"]
    query_id: Identifier
    query_sha256: Sha256Hex
    dense_offset_bytes: int = Field(ge=0)
    dense_size_bytes: int = Field(gt=0, le=_MAX_EXTERNAL_SIDECAR_BYTES)
    dense_count: int = Field(ge=1, le=_MAX_EXTERNAL_ROWS_PER_QUERY)
    dense_sha256: Sha256Hex
    vector_offset_bytes: int = Field(ge=0)
    vector_size_bytes: int = Field(gt=0, le=_MAX_EXTERNAL_SIDECAR_BYTES)
    vector_count: Literal[1536, 4096]
    vector_sha256: Sha256Hex
    lexical_offset_bytes: int | None = Field(default=None, ge=0)
    lexical_size_bytes: int | None = Field(default=None, gt=0, le=_MAX_EXTERNAL_SIDECAR_BYTES)
    lexical_count: int | None = Field(default=None, ge=0, le=250)
    lexical_sha256: Sha256Hex | None = None
    result: QueryRunResult

    @model_validator(mode="after")
    def segments_and_result_are_exact(self) -> Self:
        lexical = (
            self.lexical_offset_bytes,
            self.lexical_size_bytes,
            self.lexical_count,
            self.lexical_sha256,
        )
        if any(value is None for value in lexical) and any(value is not None for value in lexical):
            raise ValueError("external lexical segment fields must be all present or all absent")
        if self.dense_size_bytes != self.dense_count * 4:
            raise ValueError("external dense segment size differs from its count")
        if self.vector_size_bytes != self.vector_count * 4:
            raise ValueError("external vector segment size differs from its count")
        if self.result.query_id != self.query_id or self.result.lane != self.lane:
            raise ValueError("external result is not bound to its query and lane")
        return self


class ExternalLexicalRanks(_StrictModel):
    schema_version: Literal["cardrag.gold-lexical-ranks.v1"]
    ordinal: int = Field(ge=0, lt=MAX_RELEASE_QUERIES)
    query_id: Identifier
    ranks: tuple[tuple[int, int], ...] = Field(max_length=250)

    @model_validator(mode="after")
    def ranks_are_unique_and_contiguous(self) -> Self:
        row_indices = tuple(item[0] for item in self.ranks)
        ranks = tuple(item[1] for item in self.ranks)
        if (
            any(index < 0 or index >= _MAX_EXTERNAL_ROWS_PER_QUERY for index in row_indices)
            or len(row_indices) != len(set(row_indices))
            or ranks != tuple(range(1, len(ranks) + 1))
        ):
            raise ValueError("lexical rank rows must be unique and ranks contiguous")
        return self


class CorpusInventoryManifest(_StrictModel):
    schema_version: Literal["cardrag.gold-corpus-inventory.v1"]
    lane: Literal["v109_baseline", "qwen_page"]
    generation_id: Identifier
    serving_database_sha256: Sha256Hex
    vector_artifact_sha256: Sha256Hex | None = None
    embedding_dimension: Literal[1536, 4096]
    row_count: int = Field(ge=1)


class CorpusInventoryRow(_StrictModel):
    schema_version: Literal["cardrag.gold-corpus-row.v1"]
    row_index: int = Field(ge=0)
    evidence_id: Identifier
    contract_revision_id: Identifier
    span_id: Identifier
    input_sha256: Sha256Hex
    embedding_f32_sha256: Sha256Hex


class PageGenerationManifest(_StrictModel):
    """Required standalone contract for the non-production 1,600-char lane."""

    schema_version: Literal["cardrag.evaluation-page-generation.v2"]
    source_commit: SourceCommit
    source_generation_id: Identifier
    source_generation_manifest: ArtifactBinding
    source_serving_database: ArtifactBinding
    generation_id: Identifier
    serving_schema: Literal["cardrag.evaluation-page.v1"]
    serving_database: ArtifactBinding
    vector_artifact: ArtifactBinding
    embedding_model: Literal["qwen/qwen3-embedding-8b"]
    embedding_dimension: Literal[4096]
    embedding_profile_id: Identifier
    chunking_policy: Literal["cardrag.page-window-1600.v1"]
    maximum_chars: Literal[1600]
    overlap_chars: Literal[160]
    source_text_contract: Literal["cardrag.page-source-text-range.v1"]
    column_contract: Literal["cardrag.evaluation-page-columns.v1"]
    row_count: int = Field(ge=1)
    corpus_inventory_sha256: Sha256Hex


class AnswerEvidenceBindings(_StrictModel):
    """Immutable answer-production chain bound to one final retrieval capture."""

    schema_version: Literal["cardrag.gold-answer-evidence-bindings.v1"]
    lane: AnswerLane
    answer_profile_id: Identifier
    capture_input: ArtifactBinding
    producer_receipt: ArtifactBinding
    answer_artifact: ArtifactBinding
    call_ledger: ArtifactBinding
    state_identity: ArtifactBinding
    answer_state_bundle: ArtifactBinding
    decision_artifact: ArtifactBinding | None
    retrieval_run: ArtifactBinding
    retrieval_capture_receipt: ArtifactBinding
    retrieval_attestation_artifact: ArtifactBinding
    retrieval_raw_score_artifact: ArtifactBinding
    retrieval_corpus_inventory: ArtifactBinding
    retrieval_dense_score_matrix: ArtifactBinding
    retrieval_query_vector_matrix: ArtifactBinding
    retrieval_lexical_rank_artifact: ArtifactBinding | None = None

    @model_validator(mode="after")
    def retrieval_sidecars_match_lane(self) -> Self:
        if self.lane == "v109_baseline" and self.retrieval_lexical_rank_artifact is None:
            raise ValueError("v1.0.9 answer evidence must bind lexical ranks")
        if self.lane != "v109_baseline" and self.retrieval_lexical_rank_artifact is not None:
            raise ValueError("non-v1.0.9 answer evidence cannot bind lexical ranks")
        return self


class AnswerArtifactManifest(_StrictModel):
    schema_version: Literal["cardrag.gold-answer-artifact.v1"]
    lane: Literal["v109_baseline", "qwen_page", "qwen_structure_exact"]
    gold_sha256: Sha256Hex
    query_count: int = Field(ge=1, le=MAX_RELEASE_QUERIES)
    generation_id: Identifier
    generation_manifest_sha256: Sha256Hex
    answer_profile_id: Identifier
    synthetic: Literal[False]


class AnswerRecord(_StrictModel):
    schema_version: Literal["cardrag.gold-answer.v1"]
    query_id: Identifier
    query_sha256: Sha256Hex
    answer: EvaluatedAnswer


class NativeV5AttestationManifest(_StrictModel):
    schema_version: Literal["cardrag.gold-native-v5-attestation.v2"]
    capture_phase: Literal["bootstrap_retrieval", "final_release"]
    validation_profile: Literal["release_grade", "fixture_only"]
    capture_mode: Literal["native_v5"]
    synthetic: Literal[False]
    gold_sha256: Sha256Hex
    query_count: int = Field(ge=1, le=MAX_RELEASE_QUERIES)
    source_commit: SourceCommit
    generation_id: Identifier
    generation_manifest: ArtifactBinding
    serving_database: ArtifactBinding
    vector_sidecar: ArtifactBinding
    exact_row_corpus_sha256: Sha256Hex
    embedding_profile_id: Identifier
    embedding_model: Literal["qwen/qwen3-embedding-8b"]
    embedding_dimension: Literal[4096]
    score_artifact: ArtifactBinding
    score_corpus_inventory: ArtifactBinding
    score_matrix: ArtifactBinding
    score_query_vector_matrix: ArtifactBinding
    answer_artifact: ArtifactBinding | None
    answer_evidence: AnswerEvidenceBindings | None = None
    raw_score_api: Literal["cardrag_mcp.exact.V5ExactRepository.capture_unscoped_current_scores"]
    exact_api: Literal["cardrag_mcp.exact.V5ExactRepository.search"]
    lexical_api: Literal["cardrag_mcp.exact.V5ExactRepository.search.lexical_shadow"]
    reranker_api: Literal["cardrag_mcp.reranker.RerankerShadowLane.observe"]
    reranker_model: Literal["qwen/qwen3-reranker-8b"]

    @model_validator(mode="after")
    def answer_phase_is_complete(self) -> Self:
        if self.capture_phase == "bootstrap_retrieval":
            if self.answer_evidence is not None or (
                self.validation_profile == "release_grade" and self.answer_artifact is not None
            ):
                raise ValueError("release-grade bootstrap cannot bind answer evidence")
        elif (
            self.validation_profile != "release_grade"
            or self.answer_evidence is None
            or self.answer_artifact is None
            or self.answer_evidence.answer_artifact != self.answer_artifact
        ):
            raise ValueError("final native attestation must bind release-grade answers")
        return self


class NativeV5QueryAttestation(_StrictModel):
    schema_version: Literal["cardrag.gold-native-v5-query-attestation.v2"]
    query_id: Identifier
    query_sha256: Sha256Hex
    query_vector_sha256: Sha256Hex
    raw_score_query_binding_sha256: Sha256Hex
    raw_expected_embedding_rows: int = Field(ge=1)
    raw_scored_embedding_rows: int = Field(ge=1)
    raw_active_contracts: int = Field(ge=1)
    expected_embedding_rows: int = Field(ge=1)
    scored_embedding_rows: int = Field(ge=1)
    expected_active_contracts: int = Field(ge=1)
    scored_contracts: int = Field(ge=1)
    exact_blocks: int = Field(ge=1)
    exact_response_sha256: Sha256Hex
    lexical_status: Literal["succeeded"]
    lexical_additional_evidence_count: int = Field(ge=0)
    reranker_artifact_sha256: Sha256Hex
    qwen_structure_exact_result_sha256: Sha256Hex
    lexical_shadow_result_sha256: Sha256Hex
    reranker_shadow_result_sha256: Sha256Hex

    @model_validator(mode="after")
    def coverage_is_complete(self) -> Self:
        if (
            self.raw_expected_embedding_rows != self.raw_scored_embedding_rows
            or self.raw_active_contracts < self.expected_active_contracts
            or self.expected_embedding_rows != self.scored_embedding_rows
            or self.expected_active_contracts != self.scored_contracts
        ):
            raise ValueError("native v5 coverage is incomplete")
        return self


class _NativeCaptureIdentity(_StrictModel):
    schema_version: Literal["cardrag.gold-native-v5-capture-identity.v2"]
    attestation_manifest: NativeV5AttestationManifest
    query_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=MAX_RELEASE_QUERIES)


class _NativeQueryShard(_StrictModel):
    schema_version: Literal["cardrag.gold-native-v5-query-shard.v2"]
    query_index: int = Field(ge=0, lt=MAX_RELEASE_QUERIES)
    attestation: NativeV5QueryAttestation
    qwen_structure_exact: QueryRunResult
    lexical_shadow: QueryRunResult
    reranker_shadow: QueryRunResult

    @model_validator(mode="after")
    def shadows_preserve_primary_result(self) -> Self:
        primary = self.qwen_structure_exact
        for shadow in (self.lexical_shadow, self.reranker_shadow):
            if (
                shadow.query_id != primary.query_id
                or shadow.contracts != primary.contracts
                or shadow.spans != primary.spans
                or shadow.answer != primary.answer
            ):
                raise ValueError("native shadow shard changed primary output")
        return self


class LaneCaptureReceipt(_StrictModel):
    schema_version: Literal["cardrag.gold-lane-capture-receipt.v2"]
    lane: EvaluationLane
    capture_mode: Literal["native_v5", "external_reproducible"]
    capture_phase: Literal["bootstrap_retrieval", "final_release"]
    validation_profile: Literal["release_grade", "fixture_only"]
    release_eligible: bool
    gold_sha256: Sha256Hex
    query_count: int = Field(ge=1, le=MAX_RELEASE_QUERIES)
    run_artifact: ArtifactBinding
    attestation_artifact: ArtifactBinding
    source_generation_id: Identifier
    source_generation_manifest_sha256: Sha256Hex
    source_database_sha256: Sha256Hex
    source_vector_sha256: Sha256Hex | None = None
    raw_score_artifact_sha256: Sha256Hex
    corpus_inventory: ArtifactBinding
    dense_score_matrix: ArtifactBinding | None = None
    query_vector_matrix: ArtifactBinding | None = None
    lexical_rank_artifact: ArtifactBinding | None = None
    answer_evidence: AnswerEvidenceBindings | None = None

    @model_validator(mode="after")
    def release_answer_evidence_is_complete(self) -> Self:
        expected_lane: AnswerLane = (
            "qwen_structure_exact"
            if self.lane in {"lexical_shadow", "reranker_shadow"}
            else cast(AnswerLane, self.lane)
        )
        if self.capture_phase == "bootstrap_retrieval":
            if self.release_eligible or self.answer_evidence is not None:
                raise ValueError("bootstrap capture cannot claim final release evidence")
        elif not self.release_eligible or self.answer_evidence is None:
            raise ValueError("final capture must be release eligible and bind answer evidence")
        if self.capture_phase == "final_release" and self.validation_profile != "release_grade":
            raise ValueError("final release capture must use release-grade validation")
        if self.answer_evidence is not None and self.answer_evidence.lane != expected_lane:
            raise ValueError("capture answer evidence lane differs")
        if self.dense_score_matrix is None or self.query_vector_matrix is None:
            raise ValueError("capture must bind both score matrices")
        if self.capture_mode == "native_v5" and self.lexical_rank_artifact is not None:
            raise ValueError("native capture cannot bind lexical ranks")
        if self.lane == "v109_baseline" and self.lexical_rank_artifact is None:
            raise ValueError("v1.0.9 capture must bind lexical ranks")
        if self.lane != "v109_baseline" and self.lexical_rank_artifact is not None:
            raise ValueError("non-v1.0.9 capture cannot bind lexical ranks")
        return self


class CaptureSetReceipt(_StrictModel):
    schema_version: Literal["cardrag.gold-capture-set-receipt.v2"]
    gold_sha256: Sha256Hex
    query_count: int = Field(ge=1, le=MAX_RELEASE_QUERIES)
    release_eligible: bool
    lanes: tuple[LaneCaptureReceipt, ...] = Field(min_length=5, max_length=5)

    @model_validator(mode="after")
    def all_five_lanes_are_present(self) -> Self:
        if tuple(item.lane for item in self.lanes) != LANES:
            raise ValueError("capture set must contain the five canonical lanes in order")
        if any(
            item.gold_sha256 != self.gold_sha256
            or item.query_count != self.query_count
            or item.release_eligible != self.release_eligible
            or item.capture_phase != self.lanes[0].capture_phase
            or item.validation_profile != self.lanes[0].validation_profile
            for item in self.lanes
        ):
            raise ValueError("capture set lane bindings differ")
        native_evidence = tuple(self.lanes[index].answer_evidence for index in range(2, 5))
        if any(value != native_evidence[0] for value in native_evidence[1:]):
            raise ValueError("native lanes must share the exact answer evidence chain")
        return self


@dataclass(frozen=True, slots=True)
class NativeV5CaptureResult:
    run_paths: Mapping[EvaluationLane, Path]
    receipt_paths: Mapping[EvaluationLane, Path]
    attestation_path: Path
    resumed_queries: int


@dataclass(frozen=True, slots=True)
class AnswerEvidenceArtifacts:
    """Paths and independent digest pins needed to verify one answer chain."""

    generation_manifest_path: Path | None
    database_path: Path | None
    input_path: Path
    expected_input_sha256: str
    producer_receipt_path: Path
    expected_producer_receipt_sha256: str
    answer_artifact_path: Path
    expected_answer_artifact_sha256: str
    call_ledger_path: Path
    state_identity_path: Path
    state_bundle_path: Path
    answer_profile_id: str
    retrieval_run_path: Path
    expected_retrieval_run_sha256: str
    retrieval_capture_receipt_path: Path
    expected_retrieval_capture_receipt_sha256: str
    retrieval_attestation_path: Path
    expected_retrieval_attestation_sha256: str
    retrieval_raw_score_path: Path
    expected_retrieval_raw_score_sha256: str
    retrieval_corpus_inventory_path: Path
    expected_retrieval_corpus_inventory_sha256: str
    retrieval_dense_score_matrix_path: Path
    expected_retrieval_dense_score_matrix_sha256: str
    retrieval_query_vector_matrix_path: Path
    expected_retrieval_query_vector_matrix_sha256: str
    retrieval_lexical_rank_path: Path | None = None
    expected_retrieval_lexical_rank_sha256: str | None = None
    decision_path: Path | None = None
    expected_decision_sha256: str | None = None


class _ExactStore(Protocol):
    root: Path


@dataclass(frozen=True, slots=True)
class _CaptureStore:
    root: Path


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


type _StatIdentity = tuple[int, int, int, int, int, int, int, int, int]


def _stat_identity(value: os.stat_result) -> _StatIdentity:
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


def _read_regular(path: Path, *, maximum_bytes: int, code: str) -> bytes:
    """Read one immutable regular file through O_NOFOLLOW and detect races."""

    absolute = _absolute(path)
    try:
        listed = absolute.lstat()
    except FileNotFoundError:
        raise GoldCaptureError(f"{code}_missing") from None
    if stat.S_ISLNK(listed.st_mode) or not stat.S_ISREG(listed.st_mode):
        raise GoldCaptureError(f"{code}_not_regular")
    if listed.st_size <= 0 or listed.st_size > maximum_bytes:
        raise GoldCaptureError(f"{code}_size_invalid")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(absolute, flags)
    except OSError as exc:
        raise GoldCaptureError(f"{code}_open_failed") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise GoldCaptureError(f"{code}_not_regular")
        if before.st_size <= 0 or before.st_size > maximum_bytes:
            raise GoldCaptureError(f"{code}_size_invalid")
        if _stat_identity(listed) != _stat_identity(before):
            raise GoldCaptureError(f"{code}_changed_during_read")
        remaining = before.st_size
        chunks: list[bytes] = []
        size = 0
        while remaining:
            block = os.read(descriptor, min(1024 * 1024, remaining))
            if not block:
                raise GoldCaptureError(f"{code}_changed_during_read")
            if len(block) > maximum_bytes - size:
                raise GoldCaptureError(f"{code}_size_invalid")
            chunks.append(block)
            size += len(block)
            remaining -= len(block)
        if os.read(descriptor, 1):
            raise GoldCaptureError(f"{code}_changed_during_read")
        after = os.fstat(descriptor)
        try:
            current = absolute.lstat()
        except OSError:
            raise GoldCaptureError(f"{code}_changed_during_read") from None
        identity = _stat_identity(before)
        if (
            size != before.st_size
            or identity != _stat_identity(after)
            or identity != _stat_identity(current)
        ):
            raise GoldCaptureError(f"{code}_changed_during_read")
    finally:
        os.close(descriptor)
    return b"".join(chunks)


def _hash_regular(path: Path, *, maximum_bytes: int, code: str) -> ArtifactBinding:
    binding, _identity_value = _hash_regular_with_identity(
        path,
        maximum_bytes=maximum_bytes,
        code=code,
    )
    return binding


def _hash_regular_with_identity(
    path: Path,
    *,
    maximum_bytes: int,
    code: str,
) -> tuple[ArtifactBinding, _StatIdentity]:
    absolute = _absolute(path)
    try:
        listed = absolute.lstat()
    except FileNotFoundError:
        raise GoldCaptureError(f"{code}_missing") from None
    if stat.S_ISLNK(listed.st_mode) or not stat.S_ISREG(listed.st_mode):
        raise GoldCaptureError(f"{code}_not_regular")
    if listed.st_size <= 0 or listed.st_size > maximum_bytes:
        raise GoldCaptureError(f"{code}_size_invalid")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(absolute, flags)
    except OSError as exc:
        raise GoldCaptureError(f"{code}_open_failed") from exc
    digest = hashlib.sha256()
    size = 0
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise GoldCaptureError(f"{code}_not_regular")
        if before.st_size <= 0 or before.st_size > maximum_bytes:
            raise GoldCaptureError(f"{code}_size_invalid")
        if _stat_identity(listed) != _stat_identity(before):
            raise GoldCaptureError(f"{code}_changed_during_read")
        while block := os.read(descriptor, 1024 * 1024):
            if len(block) > maximum_bytes - size:
                raise GoldCaptureError(f"{code}_size_invalid")
            digest.update(block)
            size += len(block)
        after = os.fstat(descriptor)
        try:
            current = absolute.lstat()
        except FileNotFoundError:
            raise GoldCaptureError(f"{code}_changed_during_read") from None
        identity = _stat_identity(before)
        if (
            size != before.st_size
            or identity != _stat_identity(after)
            or identity != _stat_identity(listed)
            or identity != _stat_identity(current)
        ):
            raise GoldCaptureError(f"{code}_changed_during_read")
    finally:
        os.close(descriptor)
    return ArtifactBinding(sha256=digest.hexdigest(), size_bytes=size), identity


@dataclass(frozen=True, slots=True)
class _RegularArtifactCheckpoint:
    path: Path
    binding: ArtifactBinding
    identity: _StatIdentity
    maximum_bytes: int
    code: str


def _regular_artifact_checkpoint(
    path: Path,
    *,
    maximum_bytes: int,
    code: str,
) -> _RegularArtifactCheckpoint:
    absolute = _absolute(path)
    binding, identity = _hash_regular_with_identity(
        absolute,
        maximum_bytes=maximum_bytes,
        code=code,
    )
    return _RegularArtifactCheckpoint(
        path=absolute,
        binding=binding,
        identity=identity,
        maximum_bytes=maximum_bytes,
        code=code,
    )


def _verify_regular_artifact_checkpoint(checkpoint: _RegularArtifactCheckpoint) -> None:
    binding, identity = _hash_regular_with_identity(
        checkpoint.path,
        maximum_bytes=checkpoint.maximum_bytes,
        code=checkpoint.code,
    )
    if binding != checkpoint.binding or identity != checkpoint.identity:
        raise GoldCaptureError(f"{checkpoint.code}_changed_after_use")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GoldCaptureError("json_duplicate_key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> Any:
    raise GoldCaptureError("json_non_finite_number")


def _canonical_jsonl(payload: bytes, *, code: str) -> tuple[dict[str, Any], ...]:
    if not payload.endswith(b"\n") or payload.startswith(b"\xef\xbb\xbf"):
        raise GoldCaptureError(f"{code}_not_canonical_lines")
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(payload.splitlines(), start=1):
        if not line or len(line) > _MAX_CAPTURE_LINE_BYTES:
            raise GoldCaptureError(f"{code}_line_invalid", line=line_number)
        try:
            value = json.loads(
                line.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_constant,
            )
        except GoldCaptureError as exc:
            raise GoldCaptureError(exc.code, line=line_number) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GoldCaptureError(f"{code}_line_invalid", line=line_number) from exc
        if not isinstance(value, dict) or line != canonical_json_bytes(value):
            raise GoldCaptureError(f"{code}_not_canonical_bytes", line=line_number)
        records.append(cast(dict[str, Any], value))
    if not records:
        raise GoldCaptureError(f"{code}_empty")
    return tuple(records)


class _CanonicalJsonlReader:
    """Streaming canonical JSONL reader which retains one O_NOFOLLOW descriptor."""

    def __init__(self, path: Path, *, maximum_bytes: int, code: str) -> None:
        self.path = _absolute(path)
        self.maximum_bytes = maximum_bytes
        self.code = code
        self.descriptor: int | None = None
        self.stream: Any = None
        self.listed: os.stat_result | None = None
        self.before: os.stat_result | None = None
        self.digest = hashlib.sha256()
        self.size_bytes = 0
        self.line = 0

    def __enter__(self) -> Self:
        try:
            listed = self.path.lstat()
        except FileNotFoundError:
            raise GoldCaptureError(f"{self.code}_missing") from None
        if stat.S_ISLNK(listed.st_mode) or not stat.S_ISREG(listed.st_mode):
            raise GoldCaptureError(f"{self.code}_not_regular")
        if listed.st_size <= 0 or listed.st_size > self.maximum_bytes:
            raise GoldCaptureError(f"{self.code}_size_invalid")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.path, flags)
        except OSError as exc:
            raise GoldCaptureError(f"{self.code}_open_failed") from exc
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise GoldCaptureError(f"{self.code}_not_regular")
            if before.st_size <= 0 or before.st_size > self.maximum_bytes:
                raise GoldCaptureError(f"{self.code}_size_invalid")
            if _stat_identity(listed) != _stat_identity(before):
                raise GoldCaptureError(f"{self.code}_changed_during_read")
            stream = os.fdopen(descriptor, "rb", closefd=False)
        except Exception:
            os.close(descriptor)
            raise
        self.descriptor = descriptor
        self.listed = listed
        self.before = before
        self.stream = stream
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        descriptor = self.descriptor
        try:
            if self.stream is not None:
                self.stream.close()
            if descriptor is not None:
                after = os.fstat(descriptor)
                before = self.before
                listed = self.listed
                if before is None or listed is None:  # pragma: no cover - internal invariant
                    raise GoldCaptureError(f"{self.code}_reader_state_invalid")
                try:
                    current = self.path.lstat()
                except OSError:
                    raise GoldCaptureError(f"{self.code}_changed_during_read") from None
                identity = _stat_identity(before)
                if (
                    identity != _stat_identity(listed)
                    or identity != _stat_identity(after)
                    or identity != _stat_identity(current)
                    or (exc is None and self.size_bytes != before.st_size)
                ):
                    raise GoldCaptureError(f"{self.code}_changed_during_read")
        finally:
            if descriptor is not None:
                os.close(descriptor)
            self.descriptor = None
            self.stream = None

    def next_record(self) -> dict[str, Any] | None:
        if self.stream is None:
            raise RuntimeError("canonical JSONL reader is not open")
        raw = cast(bytes, self.stream.readline(_MAX_CAPTURE_LINE_BYTES + 1))
        if not raw:
            return None
        if len(raw) > self.maximum_bytes - self.size_bytes:
            raise GoldCaptureError(f"{self.code}_size_invalid", line=self.line + 1)
        self.line += 1
        self.size_bytes += len(raw)
        self.digest.update(raw)
        if (
            not raw.endswith(b"\n")
            or raw == b"\n"
            or len(raw) > _MAX_CAPTURE_LINE_BYTES
            or (self.line == 1 and raw.startswith(b"\xef\xbb\xbf"))
        ):
            raise GoldCaptureError(f"{self.code}_line_invalid", line=self.line)
        body = raw[:-1]
        try:
            value = json.loads(
                body.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_constant,
            )
        except GoldCaptureError as exc:
            raise GoldCaptureError(exc.code, line=self.line) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GoldCaptureError(f"{self.code}_line_invalid", line=self.line) from exc
        if not isinstance(value, dict) or body != canonical_json_bytes(value):
            raise GoldCaptureError(f"{self.code}_not_canonical_bytes", line=self.line)
        return cast(dict[str, Any], value)

    @property
    def binding(self) -> ArtifactBinding:
        return ArtifactBinding(sha256=self.digest.hexdigest(), size_bytes=self.size_bytes)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_immutable(path: Path, payload: bytes) -> ArtifactBinding:
    if path.is_symlink():
        raise GoldCaptureError("capture_output_symlink")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".gold-capture-", dir=path.parent)
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
            existing = _read_regular(
                path,
                maximum_bytes=max(len(payload), 1),
                code="capture_output",
            )
            if existing != payload:
                raise GoldCaptureError("capture_output_already_differs") from None
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return ArtifactBinding(sha256=hashlib.sha256(payload).hexdigest(), size_bytes=len(payload))


def _jsonl_bytes(records: Sequence[BaseModel]) -> bytes:
    return b"".join(canonical_json_bytes(record) + b"\n" for record in records)


def _external_run_bytes(records: Sequence[BaseModel]) -> bytes:
    payload = _jsonl_bytes(records)
    if len(payload) > _MAX_EXTERNAL_ARTIFACT_BYTES:
        raise GoldCaptureError("external_run_artifact_too_large")
    return payload


def _model_from_json_record[T: BaseModel](model: type[T], record: Mapping[str, Any]) -> T:
    """Preserve strict JSON semantics while accepting JSON arrays for tuple fields."""

    return model.model_validate_json(canonical_json_bytes(record))


def _load_generation_manifest(path: Path) -> tuple[GenerationManifest, ArtifactBinding]:
    payload = _read_regular(path, maximum_bytes=_MAX_MANIFEST_BYTES, code="generation_manifest")
    try:
        manifest = GenerationManifest.model_validate_json(payload)
    except Exception as exc:
        raise GoldCaptureError("generation_manifest_invalid") from exc
    if payload != manifest.canonical_bytes():
        raise GoldCaptureError("generation_manifest_not_canonical")
    return manifest, ArtifactBinding(
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
    )


def _load_page_generation_manifest(
    path: Path,
) -> tuple[PageGenerationManifest, ArtifactBinding]:
    payload = _read_regular(path, maximum_bytes=_MAX_MANIFEST_BYTES, code="page_manifest")
    try:
        manifest = PageGenerationManifest.model_validate_json(payload)
    except Exception as exc:
        raise GoldCaptureError("page_manifest_invalid") from exc
    if payload != manifest.canonical_bytes():
        raise GoldCaptureError("page_manifest_not_canonical")
    return manifest, ArtifactBinding(
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
    )


def _load_answers(
    path: Path,
    *,
    gold: GoldDataset,
    lane: Literal["v109_baseline", "qwen_page", "qwen_structure_exact"],
    generation_id: str,
    generation_manifest_sha256: str,
) -> tuple[AnswerArtifactManifest, dict[str, EvaluatedAnswer], ArtifactBinding]:
    payload = _read_regular(path, maximum_bytes=MAX_JSONL_BYTES, code="answer_artifact")
    records = _canonical_jsonl(payload, code="answer_artifact")
    try:
        manifest = _model_from_json_record(AnswerArtifactManifest, records[0])
    except ValidationError as exc:
        raise GoldCaptureError("answer_manifest_invalid", line=1) from exc
    if (
        manifest.lane != lane
        or manifest.gold_sha256 != gold.sha256
        or manifest.query_count != len(gold.queries)
        or manifest.generation_id != generation_id
        or manifest.generation_manifest_sha256 != generation_manifest_sha256
        or len(records) != len(gold.queries) + 1
    ):
        raise GoldCaptureError("answer_manifest_binding_mismatch", line=1)
    answers: dict[str, EvaluatedAnswer] = {}
    for line, (query, raw) in enumerate(zip(gold.queries, records[1:], strict=True), start=2):
        try:
            record = _model_from_json_record(AnswerRecord, raw)
        except ValidationError as exc:
            raise GoldCaptureError("answer_record_invalid", line=line) from exc
        if (
            record.query_id != query.query_id
            or record.query_sha256 != hashlib.sha256(query.question.encode("utf-8")).hexdigest()
        ):
            raise GoldCaptureError("answer_query_binding_mismatch", line=line)
        answers[record.query_id] = record.answer
    return (
        manifest,
        answers,
        ArtifactBinding(sha256=hashlib.sha256(payload).hexdigest(), size_bytes=len(payload)),
    )


def _bootstrap_answers(gold: GoldDataset) -> dict[str, EvaluatedAnswer]:
    answer = EvaluatedAnswer(
        text="제공된 검색 근거에서 답을 확인할 수 없습니다.",
        no_answer=True,
        citation_span_ids=(),
        numeric_facts=(),
        selected_revision_ids=(),
    )
    return {query.query_id: answer for query in gold.queries}


def _verify_answer_evidence(
    *,
    artifacts: AnswerEvidenceArtifacts | None,
    authoritative_results: Sequence[QueryRunResult],
    expected_lane: AnswerLane,
    gold: GoldDataset,
    gold_path: Path,
    expected_gold_sha256: str,
    expected_source_commit: str,
    expected_generation_id: str,
    expected_generation_manifest_sha256: str,
    expected_serving_database_sha256: str,
    release_gate: bool,
    source_replay: bool = True,
) -> AnswerEvidenceBindings | None:
    """Verify both the producer chain and its final, freshly scored ranking input."""

    if artifacts is None:
        return None
    if not release_gate:
        raise GoldCaptureError("answer_finalization_requires_release_gate")
    sidecar_pairs = (
        (
            artifacts.retrieval_corpus_inventory_path,
            artifacts.expected_retrieval_corpus_inventory_sha256,
        ),
        (
            artifacts.retrieval_dense_score_matrix_path,
            artifacts.expected_retrieval_dense_score_matrix_sha256,
        ),
        (
            artifacts.retrieval_query_vector_matrix_path,
            artifacts.expected_retrieval_query_vector_matrix_sha256,
        ),
        (
            artifacts.retrieval_lexical_rank_path,
            artifacts.expected_retrieval_lexical_rank_sha256,
        ),
    )
    if any((path is None) != (digest is None) for path, digest in sidecar_pairs):
        raise GoldCaptureError("answer_retrieval_sidecar_arguments_incomplete")
    if expected_lane == "v109_baseline" and sidecar_pairs[3][0] is None:
        raise GoldCaptureError("v109_answer_lexical_sidecar_required")
    if expected_lane != "v109_baseline" and sidecar_pairs[3][0] is not None:
        raise GoldCaptureError("answer_lexical_sidecar_forbidden")

    # gold_answer_artifact imports this module's schemas, so keep this import
    # local and execute it only after gold_capture has finished importing.
    from cardrag_mcp import gold_answer_artifact as answer_module

    GoldAnswerProducerError = answer_module.GoldAnswerProducerError
    verify_answer_input_ranking = answer_module.verify_answer_input_ranking

    try:
        verifier: Any
        if source_replay:
            if artifacts.generation_manifest_path is None or artifacts.database_path is None:
                raise GoldCaptureError("answer_source_replay_paths_required")
            verifier = answer_module.verify_answer_producer_receipt
        else:
            verifier = getattr(
                answer_module,
                "verify_answer_producer_receipt_portable",
                None,
            )
            if verifier is None:
                raise GoldCaptureError("portable_answer_verifier_unavailable")
        receipt_arguments: dict[str, object] = {
            "receipt_path": artifacts.producer_receipt_path,
            "expected_receipt_sha256": artifacts.expected_producer_receipt_sha256,
            "gold_path": gold_path,
            "expected_gold_sha256": expected_gold_sha256,
            "input_path": artifacts.input_path,
            "expected_input_sha256": artifacts.expected_input_sha256,
            "answer_path": artifacts.answer_artifact_path,
            "expected_answer_sha256": artifacts.expected_answer_artifact_sha256,
            "ledger_path": artifacts.call_ledger_path,
            "state_identity_path": artifacts.state_identity_path,
            "state_bundle_path": artifacts.state_bundle_path,
            "retrieval_corpus_inventory_path": (artifacts.retrieval_corpus_inventory_path),
            "expected_retrieval_corpus_inventory_sha256": (
                artifacts.expected_retrieval_corpus_inventory_sha256
            ),
            "expected_lane": expected_lane,
            "expected_source_commit": expected_source_commit,
            "expected_generation_id": expected_generation_id,
            "expected_generation_manifest_sha256": expected_generation_manifest_sha256,
            "expected_answer_profile_id": artifacts.answer_profile_id,
            "decision_path": artifacts.decision_path,
            "expected_decision_sha256": artifacts.expected_decision_sha256,
            "release_gate": release_gate,
        }
        if source_replay:
            receipt_arguments.update(
                {
                    "generation_manifest_path": artifacts.generation_manifest_path,
                    "database_path": artifacts.database_path,
                    "retrieval_run_path": artifacts.retrieval_run_path,
                    "expected_retrieval_run_sha256": (artifacts.expected_retrieval_run_sha256),
                    "retrieval_capture_receipt_path": (artifacts.retrieval_capture_receipt_path),
                    "expected_retrieval_capture_receipt_sha256": (
                        artifacts.expected_retrieval_capture_receipt_sha256
                    ),
                    "retrieval_attestation_path": artifacts.retrieval_attestation_path,
                    "expected_retrieval_attestation_sha256": (
                        artifacts.expected_retrieval_attestation_sha256
                    ),
                    "retrieval_raw_score_path": artifacts.retrieval_raw_score_path,
                    "expected_retrieval_raw_score_sha256": (
                        artifacts.expected_retrieval_raw_score_sha256
                    ),
                    "retrieval_dense_score_matrix_path": (
                        artifacts.retrieval_dense_score_matrix_path
                    ),
                    "expected_retrieval_dense_score_matrix_sha256": (
                        artifacts.expected_retrieval_dense_score_matrix_sha256
                    ),
                    "retrieval_query_vector_matrix_path": (
                        artifacts.retrieval_query_vector_matrix_path
                    ),
                    "expected_retrieval_query_vector_matrix_sha256": (
                        artifacts.expected_retrieval_query_vector_matrix_sha256
                    ),
                    "retrieval_lexical_rank_path": (artifacts.retrieval_lexical_rank_path),
                    "expected_retrieval_lexical_rank_sha256": (
                        artifacts.expected_retrieval_lexical_rank_sha256
                    ),
                }
            )
        receipt = verifier(**receipt_arguments)
        inputs = verify_answer_input_ranking(
            input_path=artifacts.input_path,
            expected_input_sha256=artifacts.expected_input_sha256,
            gold_path=gold_path,
            expected_gold_sha256=expected_gold_sha256,
            authoritative_results=authoritative_results,
            expected_lane=expected_lane,
            release_gate=release_gate,
        )
    except GoldAnswerProducerError as exc:
        raise GoldCaptureError(f"answer_evidence_{exc.code}", line=exc.line) from exc

    input_binding = _hash_regular(
        artifacts.input_path,
        maximum_bytes=MAX_JSONL_BYTES,
        code="answer_capture_input",
    )
    producer_receipt_binding = _hash_regular(
        artifacts.producer_receipt_path,
        maximum_bytes=_MAX_MANIFEST_BYTES,
        code="answer_producer_receipt",
    )
    answer_binding = _hash_regular(
        artifacts.answer_artifact_path,
        maximum_bytes=MAX_JSONL_BYTES,
        code="answer_artifact",
    )
    ledger_binding = _hash_regular(
        artifacts.call_ledger_path,
        maximum_bytes=MAX_JSONL_BYTES,
        code="answer_call_ledger",
    )
    identity_binding = _hash_regular(
        artifacts.state_identity_path,
        maximum_bytes=_MAX_CAPTURE_STATE_FILE_BYTES,
        code="answer_state_identity",
    )
    state_bundle_binding = _hash_regular(
        artifacts.state_bundle_path,
        maximum_bytes=MAX_JSONL_BYTES,
        code="answer_state_bundle",
    )
    retrieval_run = _hash_regular(
        artifacts.retrieval_run_path,
        maximum_bytes=MAX_JSONL_BYTES,
        code="answer_retrieval_run",
    )
    retrieval_receipt = _hash_regular(
        artifacts.retrieval_capture_receipt_path,
        maximum_bytes=_MAX_MANIFEST_BYTES,
        code="answer_retrieval_capture_receipt",
    )
    retrieval_attestation = _hash_regular(
        artifacts.retrieval_attestation_path,
        maximum_bytes=MAX_SCORE_ARTIFACT_BYTES,
        code="answer_retrieval_attestation",
    )
    retrieval_raw_score = _hash_regular(
        artifacts.retrieval_raw_score_path,
        maximum_bytes=MAX_SCORE_ARTIFACT_BYTES,
        code="answer_retrieval_raw_score",
    )
    retrieval_corpus_inventory = _hash_regular(
        artifacts.retrieval_corpus_inventory_path,
        maximum_bytes=_MAX_EXTERNAL_SIDECAR_BYTES,
        code="answer_retrieval_corpus_inventory",
    )
    retrieval_dense_score_matrix = _hash_regular(
        artifacts.retrieval_dense_score_matrix_path,
        maximum_bytes=_MAX_EXTERNAL_SIDECAR_BYTES,
        code="answer_retrieval_dense_score_matrix",
    )
    retrieval_query_vector_matrix = _hash_regular(
        artifacts.retrieval_query_vector_matrix_path,
        maximum_bytes=_MAX_EXTERNAL_SIDECAR_BYTES,
        code="answer_retrieval_query_vector_matrix",
    )
    retrieval_lexical_rank_artifact = (
        None
        if artifacts.retrieval_lexical_rank_path is None
        else _hash_regular(
            artifacts.retrieval_lexical_rank_path,
            maximum_bytes=_MAX_EXTERNAL_SIDECAR_BYTES,
            code="answer_retrieval_lexical_rank_artifact",
        )
    )
    decision_binding = (
        None
        if artifacts.decision_path is None
        else _hash_regular(
            artifacts.decision_path,
            maximum_bytes=MAX_JSONL_BYTES,
            code="answer_decision_artifact",
        )
    )
    _answer_manifest, answers, loaded_answer_binding = _load_answers(
        artifacts.answer_artifact_path,
        gold=gold,
        lane=expected_lane,
        generation_id=expected_generation_id,
        generation_manifest_sha256=expected_generation_manifest_sha256,
    )
    if (
        input_binding != inputs.binding
        or input_binding.sha256 != artifacts.expected_input_sha256
        or producer_receipt_binding.sha256 != artifacts.expected_producer_receipt_sha256
        or answer_binding.sha256 != artifacts.expected_answer_artifact_sha256
        or loaded_answer_binding != answer_binding
        or retrieval_run.sha256 != artifacts.expected_retrieval_run_sha256
        or retrieval_receipt.sha256 != artifacts.expected_retrieval_capture_receipt_sha256
        or retrieval_attestation.sha256 != artifacts.expected_retrieval_attestation_sha256
        or retrieval_raw_score.sha256 != artifacts.expected_retrieval_raw_score_sha256
        or retrieval_corpus_inventory.sha256 != artifacts.expected_retrieval_corpus_inventory_sha256
        or retrieval_dense_score_matrix.sha256
        != artifacts.expected_retrieval_dense_score_matrix_sha256
        or retrieval_query_vector_matrix.sha256
        != artifacts.expected_retrieval_query_vector_matrix_sha256
        or (
            retrieval_lexical_rank_artifact is not None
            and retrieval_lexical_rank_artifact.sha256
            != artifacts.expected_retrieval_lexical_rank_sha256
        )
        or receipt.capture_input != input_binding
        or receipt.answer_artifact != answer_binding
        or receipt.serving_database.sha256 != expected_serving_database_sha256
        or receipt.call_ledger != ledger_binding
        or receipt.state_identity != identity_binding
        or getattr(receipt, "state_bundle", None) != state_bundle_binding
        or receipt.retrieval_run != retrieval_run
        or receipt.retrieval_capture_receipt != retrieval_receipt
        or receipt.retrieval_attestation_artifact != retrieval_attestation
        or receipt.retrieval_raw_score_artifact != retrieval_raw_score
        or getattr(receipt, "retrieval_corpus_inventory", None) != retrieval_corpus_inventory
        or getattr(receipt, "retrieval_dense_score_matrix", None) != retrieval_dense_score_matrix
        or getattr(receipt, "retrieval_query_vector_matrix", None) != retrieval_query_vector_matrix
        or getattr(receipt, "retrieval_lexical_rank_artifact", None)
        != retrieval_lexical_rank_artifact
        or receipt.decision_artifact != decision_binding
        or inputs.manifest.answer_profile_id != artifacts.answer_profile_id
        or tuple(result.query_id for result in authoritative_results)
        != tuple(query.query_id for query in gold.queries)
        or any(result.answer != answers[result.query_id] for result in authoritative_results)
    ):
        raise GoldCaptureError("answer_evidence_binding_changed_after_verification")
    return AnswerEvidenceBindings(
        schema_version=ANSWER_EVIDENCE_BINDINGS_SCHEMA,
        lane=expected_lane,
        answer_profile_id=artifacts.answer_profile_id,
        capture_input=input_binding,
        producer_receipt=producer_receipt_binding,
        answer_artifact=answer_binding,
        call_ledger=ledger_binding,
        state_identity=identity_binding,
        answer_state_bundle=state_bundle_binding,
        decision_artifact=decision_binding,
        retrieval_run=retrieval_run,
        retrieval_capture_receipt=retrieval_receipt,
        retrieval_attestation_artifact=retrieval_attestation,
        retrieval_raw_score_artifact=retrieval_raw_score,
        retrieval_corpus_inventory=retrieval_corpus_inventory,
        retrieval_dense_score_matrix=retrieval_dense_score_matrix,
        retrieval_query_vector_matrix=retrieval_query_vector_matrix,
        retrieval_lexical_rank_artifact=retrieval_lexical_rank_artifact,
    )


def _load_inventory(
    path: Path,
    *,
    expected_manifest: ExternalObservationManifest,
    expected_sha256: str,
) -> tuple[CorpusInventoryManifest, tuple[CorpusInventoryRow, ...], ArtifactBinding]:
    rows: list[CorpusInventoryRow] = []
    with _CanonicalJsonlReader(
        path,
        maximum_bytes=_MAX_EXTERNAL_ARTIFACT_BYTES,
        code="corpus_inventory",
    ) as reader:
        raw_manifest = reader.next_record()
        if raw_manifest is None:
            raise GoldCaptureError("corpus_inventory_empty")
        try:
            manifest = _model_from_json_record(CorpusInventoryManifest, raw_manifest)
        except ValidationError as exc:
            raise GoldCaptureError("corpus_inventory_manifest_invalid", line=1) from exc
        for expected_index in range(manifest.row_count):
            raw = reader.next_record()
            if raw is None:
                raise GoldCaptureError("corpus_inventory_row_missing", line=reader.line + 1)
            try:
                row = _model_from_json_record(CorpusInventoryRow, raw)
            except ValidationError as exc:
                raise GoldCaptureError("corpus_inventory_row_invalid", line=reader.line) from exc
            if row.row_index != expected_index:
                raise GoldCaptureError("corpus_inventory_row_order_invalid", line=reader.line)
            rows.append(row)
        if reader.next_record() is not None:
            raise GoldCaptureError("corpus_inventory_trailing_record", line=reader.line)
        binding = reader.binding
    if (
        binding.sha256 != expected_sha256
        or expected_sha256 != expected_manifest.corpus_inventory_sha256
        or manifest.lane != expected_manifest.lane
        or manifest.generation_id != expected_manifest.generation_id
        or manifest.serving_database_sha256 != expected_manifest.serving_database.sha256
        or manifest.vector_artifact_sha256
        != (
            None
            if expected_manifest.vector_artifact is None
            else expected_manifest.vector_artifact.sha256
        )
        or manifest.embedding_dimension != expected_manifest.embedding_dimension
        or manifest.row_count != expected_manifest.row_count
    ):
        raise GoldCaptureError("corpus_inventory_binding_mismatch")
    identities = tuple((row.evidence_id, row.span_id) for row in rows)
    if len(identities) != len(set(identities)):
        raise GoldCaptureError("corpus_inventory_identity_duplicate")
    return manifest, tuple(rows), binding


@contextmanager
def _sqlite_readonly(
    path: Path,
    *,
    expected_binding: ArtifactBinding | None = None,
) -> Iterator[sqlite3.Connection]:
    """Pin, hash, and query one SQLite inode without following the source path."""

    absolute = _absolute(path)
    try:
        listed = absolute.lstat()
    except FileNotFoundError:
        raise GoldCaptureError("serving_database_missing") from None
    if stat.S_ISLNK(listed.st_mode) or not stat.S_ISREG(listed.st_mode):
        raise GoldCaptureError("serving_database_not_regular")
    if listed.st_size <= 0 or listed.st_size > _MAX_DATABASE_BYTES:
        raise GoldCaptureError("serving_database_size_invalid")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(absolute, flags)
    except OSError as exc:
        raise GoldCaptureError("serving_database_open_failed") from exc
    connection: sqlite3.Connection | None = None
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise GoldCaptureError("serving_database_not_regular")
        if before.st_size <= 0 or before.st_size > _MAX_DATABASE_BYTES:
            raise GoldCaptureError("serving_database_size_invalid")
        if _stat_identity(listed) != _stat_identity(before):
            raise GoldCaptureError("serving_database_changed_during_read")
        digest = hashlib.sha256()
        size = 0
        while block := os.read(descriptor, 1024 * 1024):
            if len(block) > _MAX_DATABASE_BYTES - size:
                raise GoldCaptureError("serving_database_size_invalid")
            digest.update(block)
            size += len(block)
        after_hash = os.fstat(descriptor)
        try:
            current = absolute.lstat()
        except OSError:
            raise GoldCaptureError("serving_database_changed_during_read") from None
        identity = _stat_identity(before)
        if (
            size != before.st_size
            or identity != _stat_identity(after_hash)
            or identity != _stat_identity(current)
        ):
            raise GoldCaptureError("serving_database_changed_during_read")
        binding = ArtifactBinding(sha256=digest.hexdigest(), size_bytes=size)
        if expected_binding is not None and binding != expected_binding:
            raise GoldCaptureError("serving_database_binding_mismatch")
        # SQLite opens its own descriptor for this already pinned inode.  Keeping
        # our descriptor open lets the final fstat detect in-place mutation and
        # prevents a path replacement from changing what SQLite reads.
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
                current = absolute.lstat()
            except OSError:
                raise GoldCaptureError("serving_database_changed_during_read") from None
            if identity != _stat_identity(os.fstat(descriptor)) or identity != _stat_identity(
                current
            ):
                raise GoldCaptureError("serving_database_changed_during_read")
    except GoldCaptureError:
        raise
    except sqlite3.Error as exc:
        raise GoldCaptureError("serving_database_open_failed") from exc
    finally:
        if connection is not None:
            connection.close()
        os.close(descriptor)


def _page_source_rows(
    *,
    contract_revision_id: str,
    document_id: str,
    page: int,
    text: str,
    first_row_index: int,
) -> tuple[tuple[object, ...], ...]:
    """Reproduce the pinned 1,600/160 page-window source/range contract."""

    rows: list[tuple[object, ...]] = []
    length = len(text)
    start = 0
    while start < length:
        limit = min(length, start + 1600)
        end = limit
        if limit < length:
            boundary = max(text.rfind("\n", start + 1, limit), text.rfind(" ", start + 1, limit))
            if boundary > start + 800:
                end = boundary
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if end <= start:
            break
        chunk_text = text[start:end]
        input_sha256 = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()
        chunk_id = "evidence_" + canonical_sha256(
            {
                "document_id": document_id,
                "page": page,
                "source_end": end,
                "source_start": start,
                "text_sha256": input_sha256,
            }
        )
        rows.append(
            (
                first_row_index + len(rows),
                chunk_id,
                contract_revision_id,
                chunk_id,
                document_id,
                page,
                start,
                end,
                chunk_text,
                input_sha256,
            )
        )
        if end >= length:
            break
        start = max(start + 1, end - 160)
    return tuple(rows)


def _verify_page_parent_source(
    *,
    page_manifest: PageGenerationManifest,
    page_rows: Sequence[sqlite3.Row],
    source_generation_manifest_path: Path | None,
    source_database_path: Path | None,
    release_gate: bool,
) -> None:
    supplied = (source_generation_manifest_path, source_database_path)
    if any(path is None for path in supplied):
        if release_gate or any(path is not None for path in supplied):
            raise GoldCaptureError("page_parent_source_paths_required")
        return
    source_manifest, source_manifest_binding = _load_generation_manifest(
        cast(Path, source_generation_manifest_path)
    )
    if (
        source_manifest.schema_version != "cardrag.generation.v5"
        or source_manifest.generation_id != page_manifest.source_generation_id
        or source_manifest_binding != page_manifest.source_generation_manifest
        or ArtifactBinding(
            sha256=source_manifest.serving_database.sha256,
            size_bytes=source_manifest.serving_database.size_bytes,
        )
        != page_manifest.source_serving_database
    ):
        raise GoldCaptureError("page_parent_generation_binding_mismatch")
    expected_rows: list[tuple[object, ...]] = []
    with _sqlite_readonly(
        cast(Path, source_database_path),
        expected_binding=page_manifest.source_serving_database,
    ) as connection:
        try:
            metadata = {
                str(row[0]): str(row[1])
                for row in connection.execute("SELECT key,value FROM metadata")
            }
            sources = connection.execute(
                """SELECT r.contract_revision_id,r.document_id,r.page_count,
                          p.page,p.text,p.text_sha256
                     FROM contract_revisions AS r
                     JOIN document_pages AS p
                       ON p.contract_revision_id=r.contract_revision_id
                    WHERE r.temporal_status='current'
                    ORDER BY r.contract_revision_id,p.page"""
            ).fetchall()
            current_count = int(
                connection.execute(
                    "SELECT count(*) FROM contract_revisions WHERE temporal_status='current'"
                ).fetchone()[0]
            )
        except sqlite3.Error as exc:
            raise GoldCaptureError("page_parent_database_schema_invalid") from exc
    if (
        metadata.get("schema_id") != "cardrag.serving-db.v5"
        or metadata.get("generation_id") != page_manifest.source_generation_id
        or metadata.get("current_revision_count") != str(current_count)
        or not sources
    ):
        raise GoldCaptureError("page_parent_database_profile_mismatch")
    pages_by_revision: dict[str, list[int]] = {}
    page_counts: dict[str, int] = {}
    for revision_id, document_id, page_count, page, text, declared_sha256 in sources:
        revision = str(revision_id)
        page_number = int(page)
        source_text = str(text)
        declared_count = int(page_count)
        if hashlib.sha256(source_text.encode("utf-8")).hexdigest() != str(declared_sha256):
            raise GoldCaptureError("page_parent_source_text_sha256_mismatch")
        pages_by_revision.setdefault(revision, []).append(page_number)
        if page_counts.setdefault(revision, declared_count) != declared_count:
            raise GoldCaptureError("page_parent_page_count_mismatch")
        expected_rows.extend(
            _page_source_rows(
                contract_revision_id=revision,
                document_id=str(document_id),
                page=page_number,
                text=source_text,
                first_row_index=len(expected_rows),
            )
        )
    if len(pages_by_revision) != current_count or any(
        pages != list(range(1, page_counts[revision] + 1))
        for revision, pages in pages_by_revision.items()
    ):
        raise GoldCaptureError("page_parent_page_coverage_mismatch")
    actual_rows = tuple(tuple(row[column] for column in range(10)) for row in page_rows)
    if actual_rows != tuple(expected_rows):
        raise GoldCaptureError("page_parent_source_range_mismatch")


@contextmanager
def _verified_corpus_vectors(
    *,
    manifest: ExternalObservationManifest,
    inventory: Sequence[CorpusInventoryRow],
    database_path: Path,
    vector_path: Path | None,
    page_manifest: PageGenerationManifest | None = None,
    source_generation_manifest_path: Path | None = None,
    source_database_path: Path | None = None,
    release_gate: bool = True,
) -> Iterator[tuple[npt.NDArray[np.float32], sqlite3.Connection]]:
    """Open the source corpus read-only and bind every inventory vector to it."""

    dimension = manifest.embedding_dimension
    vector_mapping: mmap.mmap | None = None
    vector_descriptor: int | None = None
    matrix: npt.NDArray[np.float32]
    with (
        ExitStack() as cleanup,
        _sqlite_readonly(
            database_path,
            expected_binding=manifest.serving_database,
        ) as connection,
    ):
        if manifest.lane == "v109_baseline":
            try:
                metadata = {
                    str(row[0]): str(row[1])
                    for row in connection.execute("SELECT key,value FROM metadata")
                }
                database_rows = connection.execute(
                    """SELECT evidence_id,document_id,text,embedding
                         FROM evidence ORDER BY evidence_id"""
                ).fetchall()
            except sqlite3.Error as exc:
                raise GoldCaptureError("v109_database_schema_invalid") from exc
            if (
                metadata.get("schema_id") != "cardrag.serving-db.v4"
                or metadata.get("generation_id") != manifest.generation_id
                or metadata.get("embedding_model") != manifest.embedding_model
                or metadata.get("embedding_dimension") != str(dimension)
                or len(database_rows) != len(inventory)
            ):
                raise GoldCaptureError("v109_database_profile_mismatch")
            matrix = np.empty((len(inventory), dimension), dtype="<f4")
            for index, (source, expected) in enumerate(zip(database_rows, inventory, strict=True)):
                evidence_id = str(source[0])
                document_id = str(source[1])
                text_sha256 = hashlib.sha256(str(source[2]).encode("utf-8")).hexdigest()
                embedding = source[3]
                if (
                    evidence_id != expected.evidence_id
                    or document_id != expected.contract_revision_id
                    or evidence_id != expected.span_id
                    or text_sha256 != expected.input_sha256
                    or not isinstance(embedding, bytes)
                    or len(embedding) != dimension * 4
                    or hashlib.sha256(embedding).hexdigest() != expected.embedding_f32_sha256
                ):
                    raise GoldCaptureError("v109_inventory_database_mismatch")
                matrix[index] = np.frombuffer(embedding, dtype="<f4", count=dimension)
        else:
            if vector_path is None or manifest.vector_artifact is None or page_manifest is None:
                raise GoldCaptureError("qwen_page_vector_artifact_required")
            try:
                metadata = {
                    str(row[0]): str(row[1])
                    for row in connection.execute("SELECT key,value FROM metadata")
                }
                columns = tuple(
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(evaluation_chunks)")
                )
                source_rows = connection.execute(
                    """SELECT row_index,chunk_id,contract_revision_id,span_id,
                              document_id,page,source_start,source_end,text,input_sha256
                         FROM evaluation_chunks ORDER BY row_index"""
                ).fetchall()
            except sqlite3.Error as exc:
                raise GoldCaptureError("page_database_schema_invalid") from exc
            if (
                columns != _PAGE_DATABASE_COLUMNS
                or set(metadata) != _PAGE_DATABASE_METADATA_KEYS
                or metadata.get("schema_id") != "cardrag.evaluation-page.v1"
                or metadata.get("generation_id") != manifest.generation_id
                or metadata.get("source_commit") != page_manifest.source_commit
                or metadata.get("source_generation_id") != page_manifest.source_generation_id
                or metadata.get("source_generation_manifest_sha256")
                != page_manifest.source_generation_manifest.sha256
                or metadata.get("source_generation_manifest_size_bytes")
                != str(page_manifest.source_generation_manifest.size_bytes)
                or metadata.get("source_serving_database_sha256")
                != page_manifest.source_serving_database.sha256
                or metadata.get("source_serving_database_size_bytes")
                != str(page_manifest.source_serving_database.size_bytes)
                or metadata.get("embedding_model") != manifest.embedding_model
                or metadata.get("embedding_dimension") != str(dimension)
                or metadata.get("embedding_profile_id") != manifest.embedding_profile_id
                or metadata.get("chunking_policy") != "cardrag.page-window-1600.v1"
                or metadata.get("maximum_chars") != "1600"
                or metadata.get("overlap_chars") != "160"
                or metadata.get("source_text_contract") != PAGE_SOURCE_TEXT_CONTRACT
                or metadata.get("column_contract") != PAGE_COLUMN_CONTRACT
                or metadata.get("row_count") != str(page_manifest.row_count)
                or len(source_rows) != len(inventory)
                or len(source_rows) != page_manifest.row_count
            ):
                raise GoldCaptureError("page_database_profile_mismatch")
            for expected_index, (source, expected) in enumerate(
                zip(source_rows, inventory, strict=True)
            ):
                source_text = str(source[8])
                input_sha256 = str(source[9])
                document_id = str(source[4])
                page = int(source[5])
                source_start = int(source[6])
                source_end = int(source[7])
                expected_chunk_id = "evidence_" + canonical_sha256(
                    {
                        "document_id": document_id,
                        "page": page,
                        "source_end": source_end,
                        "source_start": source_start,
                        "text_sha256": input_sha256,
                    }
                )
                actual = (
                    int(source[0]),
                    str(source[1]),
                    str(source[2]),
                    str(source[3]),
                    input_sha256,
                )
                wanted = (
                    expected_index,
                    expected.evidence_id,
                    expected.contract_revision_id,
                    expected.span_id,
                    expected.input_sha256,
                )
                if (
                    actual != wanted
                    or str(source[1]) != str(source[3])
                    or str(source[1]) != expected_chunk_id
                    or not document_id
                    or page < 1
                    or source_start < 0
                    or source_end <= source_start
                    or source_end - source_start != len(source_text)
                    or not source_text
                    or source_text != source_text.strip()
                    or len(source_text) > 1600
                    or hashlib.sha256(source_text.encode("utf-8")).hexdigest() != input_sha256
                ):
                    raise GoldCaptureError("page_inventory_database_mismatch")
            _verify_page_parent_source(
                page_manifest=page_manifest,
                page_rows=source_rows,
                source_generation_manifest_path=source_generation_manifest_path,
                source_database_path=source_database_path,
                release_gate=release_gate,
            )
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            absolute_vector = _absolute(vector_path)
            try:
                listed_vector = absolute_vector.lstat()
            except FileNotFoundError:
                raise GoldCaptureError("page_vector_artifact_missing") from None
            if stat.S_ISLNK(listed_vector.st_mode) or not stat.S_ISREG(listed_vector.st_mode):
                raise GoldCaptureError("page_vector_artifact_not_regular")
            expected_vector_size = len(inventory) * dimension * 4
            if (
                listed_vector.st_size != expected_vector_size
                or listed_vector.st_size != manifest.vector_artifact.size_bytes
                or listed_vector.st_size > _MAX_SIDECAR_BYTES
            ):
                raise GoldCaptureError("page_vector_size_mismatch")
            try:
                vector_descriptor = os.open(absolute_vector, flags)
            except OSError as exc:
                raise GoldCaptureError("page_vector_artifact_open_failed") from exc
            cleanup.callback(os.close, vector_descriptor)
            vector_before = os.fstat(vector_descriptor)
            if not stat.S_ISREG(vector_before.st_mode):
                raise GoldCaptureError("page_vector_artifact_not_regular")
            if (
                vector_before.st_size != expected_vector_size
                or vector_before.st_size > _MAX_SIDECAR_BYTES
            ):
                raise GoldCaptureError("page_vector_size_mismatch")
            vector_identity = _stat_identity(vector_before)
            if vector_identity != _stat_identity(listed_vector):
                raise GoldCaptureError("page_vector_changed_during_validation")
            digest = hashlib.sha256()
            size = 0
            while block := os.read(vector_descriptor, 1024 * 1024):
                if len(block) > expected_vector_size - size:
                    raise GoldCaptureError("page_vector_size_mismatch")
                digest.update(block)
                size += len(block)
            vector_after_hash = os.fstat(vector_descriptor)
            try:
                current_vector = absolute_vector.lstat()
            except OSError:
                raise GoldCaptureError("page_vector_changed_during_validation") from None
            if (
                size != vector_before.st_size
                or vector_identity != _stat_identity(vector_after_hash)
                or vector_identity != _stat_identity(current_vector)
                or ArtifactBinding(sha256=digest.hexdigest(), size_bytes=size)
                != manifest.vector_artifact
            ):
                raise GoldCaptureError("page_vector_binding_mismatch")
            vector_mapping = mmap.mmap(vector_descriptor, 0, access=mmap.ACCESS_READ)
            matrix = np.frombuffer(vector_mapping, dtype="<f4").reshape(len(inventory), dimension)
            for index, row in enumerate(inventory):
                if (
                    hashlib.sha256(matrix[index].astype("<f4", copy=False).tobytes()).hexdigest()
                    != row.embedding_f32_sha256
                ):
                    raise GoldCaptureError("page_inventory_vector_mismatch")
        if not bool(np.isfinite(matrix).all()):
            raise GoldCaptureError("corpus_vector_non_finite")
        norms = np.linalg.norm(matrix, axis=1)
        if not bool(np.allclose(norms, 1.0, rtol=2e-5, atol=2e-5)):
            raise GoldCaptureError("corpus_vector_not_normalized")
        try:
            yield matrix, connection
        finally:
            if vector_descriptor is not None:
                vector_after = os.fstat(vector_descriptor)
                try:
                    current_vector = absolute_vector.lstat()
                except OSError:
                    raise GoldCaptureError("page_vector_changed_during_validation") from None
                if vector_identity != _stat_identity(
                    vector_after
                ) or vector_identity != _stat_identity(current_vector):
                    raise GoldCaptureError("page_vector_changed_during_validation")
    # The ndarray owns the mmap export until the caller releases its
    # ``with ... as corpus`` binding.  Its mmap object then closes during
    # ordinary reference teardown; ExitStack has already closed the descriptor.
    del vector_mapping


@contextmanager
def _verified_sidecar(
    path: Path,
    *,
    expected: ArtifactBinding,
    code: str,
) -> Iterator[mmap.mmap]:
    if expected.size_bytes > _MAX_EXTERNAL_SIDECAR_BYTES:
        raise GoldCaptureError(f"{code}_too_large")
    absolute = _absolute(path)
    try:
        listed = absolute.lstat()
    except FileNotFoundError:
        raise GoldCaptureError(f"{code}_missing") from None
    if stat.S_ISLNK(listed.st_mode) or not stat.S_ISREG(listed.st_mode):
        raise GoldCaptureError(f"{code}_not_regular")
    if listed.st_size != expected.size_bytes:
        raise GoldCaptureError(f"{code}_binding_mismatch")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(absolute, flags)
    except OSError as exc:
        raise GoldCaptureError(f"{code}_open_failed") from exc
    mapping: mmap.mmap | None = None
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise GoldCaptureError(f"{code}_not_regular")
        if before.st_size != expected.size_bytes or before.st_size > _MAX_EXTERNAL_SIDECAR_BYTES:
            raise GoldCaptureError(f"{code}_binding_mismatch")
        identity = _stat_identity(before)
        if identity != _stat_identity(listed):
            raise GoldCaptureError(f"{code}_changed_during_validation")
        digest = hashlib.sha256()
        size = 0
        while block := os.read(descriptor, 1024 * 1024):
            if len(block) > expected.size_bytes - size:
                raise GoldCaptureError(f"{code}_binding_mismatch")
            digest.update(block)
            size += len(block)
        after_hash = os.fstat(descriptor)
        try:
            current = absolute.lstat()
        except OSError:
            raise GoldCaptureError(f"{code}_changed_during_validation") from None
        if (
            size != before.st_size
            or digest.hexdigest() != expected.sha256
            or identity != _stat_identity(after_hash)
            or identity != _stat_identity(current)
        ):
            raise GoldCaptureError(f"{code}_binding_mismatch")
        mapping = mmap.mmap(descriptor, 0, access=mmap.ACCESS_READ)
        try:
            yield mapping
        finally:
            try:
                current = absolute.lstat()
            except OSError:
                raise GoldCaptureError(f"{code}_changed_during_validation") from None
            if identity != _stat_identity(os.fstat(descriptor)) or identity != _stat_identity(
                current
            ):
                raise GoldCaptureError(f"{code}_changed_during_validation")
    finally:
        if mapping is not None:
            mapping.close()
        os.close(descriptor)


def _decode_query_vector(
    raw: bytes,
    *,
    expected_sha256: str,
    dimension: int,
) -> npt.NDArray[np.float32]:
    if len(raw) != dimension * 4 or hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise GoldCaptureError("query_vector_binding_mismatch")
    vector = np.frombuffer(raw, dtype="<f4", count=dimension)
    if not bool(np.isfinite(vector).all()):
        raise GoldCaptureError("query_vector_non_finite")
    norm = float(np.linalg.norm(vector))
    if not math.isfinite(norm) or not math.isclose(norm, 1.0, rel_tol=2e-5, abs_tol=2e-5):
        raise GoldCaptureError("query_vector_not_normalized")
    return vector


def _contracts_from_spans(
    spans: Sequence[RetrievedSpan],
    *,
    maximum: int = 100,
) -> tuple[RetrievedContract, ...]:
    result: list[RetrievedContract] = []
    seen: set[str] = set()
    for span in spans:
        if span.contract_revision_id in seen:
            continue
        seen.add(span.contract_revision_id)
        result.append(
            RetrievedContract(
                contract_revision_id=span.contract_revision_id,
                rank=len(result) + 1,
                score=span.score,
            )
        )
        if len(result) == maximum:
            break
    return tuple(result)


def _contracts_from_scored_external_rows(
    rows: Sequence[tuple[float, ExternalRawRow]],
    *,
    maximum: int = 100,
) -> tuple[RetrievedContract, ...]:
    result: list[RetrievedContract] = []
    seen: set[str] = set()
    for score, row in rows:
        if row.contract_revision_id in seen:
            continue
        seen.add(row.contract_revision_id)
        result.append(
            RetrievedContract(
                contract_revision_id=row.contract_revision_id,
                rank=len(result) + 1,
                score=score,
            )
        )
        if len(result) == maximum:
            break
    return tuple(result)


def _sidecar_segment(
    mapping: mmap.mmap,
    *,
    offset: int,
    size: int,
    expected_sha256: str,
    code: str,
) -> bytes:
    end = offset + size
    if end < offset or end > len(mapping):
        raise GoldCaptureError(f"{code}_range_invalid")
    payload = mapping[offset:end]
    if len(payload) != size or hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise GoldCaptureError(f"{code}_sha256_mismatch")
    return payload


def _lexical_ranks_from_segment(
    payload: bytes,
    *,
    observation: ExternalQueryObservation,
    inventory_count: int,
) -> dict[int, int]:
    if not payload.endswith(b"\n") or payload.count(b"\n") != 1:
        raise GoldCaptureError("external_lexical_segment_not_one_canonical_line")
    try:
        record = ExternalLexicalRanks.model_validate_json(payload[:-1])
    except ValidationError as exc:
        raise GoldCaptureError("external_lexical_segment_invalid") from exc
    if (
        payload != record.canonical_bytes() + b"\n"
        or record.ordinal != observation.ordinal
        or record.query_id != observation.query_id
        or observation.lexical_count != len(record.ranks)
        or any(row_index >= inventory_count for row_index, _rank in record.ranks)
    ):
        raise GoldCaptureError("external_lexical_segment_binding_mismatch")
    return dict(record.ranks)


def _external_raw_rows(
    *,
    dense_payload: bytes,
    inventory: Sequence[CorpusInventoryRow],
    lexical_ranks: Mapping[int, int],
) -> tuple[ExternalRawRow, ...]:
    if len(dense_payload) != len(inventory) * 4:
        raise GoldCaptureError("external_dense_segment_count_mismatch")
    scores = np.frombuffer(dense_payload, dtype="<f4", count=len(inventory))
    if not bool(np.isfinite(scores).all()):
        raise GoldCaptureError("external_dense_score_non_finite")
    order = sorted(
        range(len(inventory)),
        key=lambda index: (-float(scores[index]), inventory[index].evidence_id),
    )
    dense_ranks = {row_index: rank for rank, row_index in enumerate(order, start=1)}
    return tuple(
        ExternalRawRow(
            row_index=index,
            evidence_id=provenance.evidence_id,
            contract_revision_id=provenance.contract_revision_id,
            span_id=provenance.span_id,
            input_sha256=provenance.input_sha256,
            dense_score=float(scores[index]),
            dense_rank=dense_ranks[index],
            lexical_rank=lexical_ranks.get(index),
        )
        for index, provenance in enumerate(inventory)
    )


def _validate_external_result(
    observation: ExternalQueryObservation,
    *,
    raw_rows: Sequence[ExternalRawRow],
    query_vector: npt.NDArray[np.float32],
    corpus: npt.NDArray[np.float32],
    inventory: Sequence[CorpusInventoryRow],
    expected_lexical_ranks: Mapping[str, int] | None,
) -> None:
    if len(raw_rows) != len(inventory):
        raise GoldCaptureError("external_raw_row_inventory_count_mismatch")
    query_norm = float(np.linalg.norm(query_vector))
    corpus_norms = np.linalg.norm(corpus, axis=1)
    recomputed = (corpus @ query_vector) / (corpus_norms * query_norm)
    by_dense_rank = sorted(raw_rows, key=lambda row: row.dense_rank)
    expected_dense_order = sorted(
        raw_rows,
        key=lambda row: (-row.dense_score, row.evidence_id),
    )
    if by_dense_rank != expected_dense_order:
        raise GoldCaptureError("external_dense_rank_score_mismatch")
    for raw, provenance in zip(raw_rows, inventory, strict=True):
        if (
            raw.row_index != provenance.row_index
            or raw.evidence_id != provenance.evidence_id
            or raw.contract_revision_id != provenance.contract_revision_id
            or raw.span_id != provenance.span_id
            or raw.input_sha256 != provenance.input_sha256
            or np.asarray(raw.dense_score, dtype="<f4").tobytes()
            != np.asarray(recomputed[raw.row_index], dtype="<f4").tobytes()
        ):
            raise GoldCaptureError("external_raw_score_provenance_mismatch")
    actual_lexical_ranks = {
        row.evidence_id: row.lexical_rank for row in raw_rows if row.lexical_rank is not None
    }
    if expected_lexical_ranks is None:
        if actual_lexical_ranks:
            raise GoldCaptureError("qwen_page_lexical_trace_forbidden")
    elif actual_lexical_ranks != expected_lexical_ranks:
        raise GoldCaptureError("v109_lexical_trace_mismatch")

    dense_limit = 250 if observation.lane == "v109_baseline" else 100
    dense_spans = tuple(
        RetrievedSpan(
            span_id=row.span_id,
            contract_revision_id=row.contract_revision_id,
            rank=rank,
            score=row.dense_score,
        )
        for rank, row in enumerate(by_dense_rank[:dense_limit], start=1)
    )
    dense_contracts = _contracts_from_spans(dense_spans, maximum=100)
    result = observation.result
    if observation.lane == "v109_baseline":
        fused = sorted(
            (
                (
                    (0.0 if row.lexical_rank is None else 1.0 / (60 + row.lexical_rank))
                    + (0.0 if row.dense_rank > 250 else 1.0 / (60 + row.dense_rank)),
                    row,
                )
                for row in raw_rows
                if row.lexical_rank is not None or row.dense_rank <= 250
            ),
            key=lambda item: (-item[0], item[1].evidence_id),
        )
        primary_spans = tuple(
            RetrievedSpan(
                span_id=row.span_id,
                contract_revision_id=row.contract_revision_id,
                rank=rank,
                score=score,
            )
            for rank, (score, row) in enumerate(fused[:100], start=1)
        )
        expected_trace = V109BaselineObservation(
            kind="v109_small_rrf",
            rrf_k=60,
            dense_contracts=dense_contracts,
            dense_spans=dense_spans,
        )
        if result.v109_baseline != expected_trace:
            raise GoldCaptureError("v109_raw_dense_trace_mismatch")
        primary_contracts = _contracts_from_scored_external_rows(fused)
    else:
        primary_spans = dense_spans
        primary_contracts = _contracts_from_scored_external_rows(
            tuple((row.dense_score, row) for row in by_dense_rank)
        )
    if result.spans != primary_spans or result.contracts != primary_contracts:
        raise GoldCaptureError("external_primary_ranking_mismatch")


def _v109_lexical_ranks_from_connection(
    connection: sqlite3.Connection,
    query: str,
    *,
    limit: int,
) -> dict[str, int]:
    tokens = [part for part in query.split() if part]
    if not tokens:
        raise GoldCaptureError("v109_lexical_query_blank")
    expression = " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)
    try:
        rows = connection.execute(
            """SELECT e.evidence_id
                 FROM evidence_fts AS f
                 JOIN evidence AS e ON e.evidence_pk=f.rowid
                 JOIN documents AS d ON d.document_id=e.document_id
                WHERE evidence_fts MATCH ?
                ORDER BY bm25(evidence_fts),e.evidence_id
                LIMIT ?""",
            (expression, limit),
        ).fetchall()
    except sqlite3.Error as exc:
        raise GoldCaptureError("v109_lexical_capture_failed") from exc
    return {str(row[0]): rank for rank, row in enumerate(rows, start=1)}


def _v109_lexical_ranks(database_path: Path, query: str, *, limit: int) -> dict[str, int]:
    with _sqlite_readonly(database_path) as connection:
        return _v109_lexical_ranks_from_connection(connection, query, limit=limit)


def seal_external_observation(
    *,
    gold_path: Path,
    expected_gold_sha256: str,
    observation_path: Path,
    expected_observation_sha256: str,
    inventory_path: Path,
    expected_inventory_sha256: str,
    generation_manifest_path: Path,
    database_path: Path,
    vector_path: Path | None,
    dense_score_matrix_path: Path,
    query_vector_matrix_path: Path,
    lexical_rank_path: Path | None,
    output_path: Path,
    receipt_path: Path,
    answer_evidence_artifacts: AnswerEvidenceArtifacts | None = None,
    source_generation_manifest_path: Path | None = None,
    source_database_path: Path | None = None,
    expected_source_commit: str | None = None,
    release_gate: bool = True,
) -> LaneCaptureReceipt:
    """Validate and seal v1.0.9 or Qwen-page external raw observations."""

    gold = load_gold_jsonl(gold_path, release_gate=release_gate)
    if gold.sha256 != expected_gold_sha256:
        raise GoldCaptureError("gold_sha256_mismatch")
    candidate_source_commit = _validated_expected_source_commit(
        expected_source_commit,
        release_gate=release_gate,
    )
    observation_binding = _hash_regular(
        observation_path,
        maximum_bytes=_MAX_EXTERNAL_ARTIFACT_BYTES,
        code="external_observation",
    )
    if observation_binding.sha256 != expected_observation_sha256:
        raise GoldCaptureError("external_observation_sha256_mismatch")

    with _CanonicalJsonlReader(
        observation_path,
        maximum_bytes=_MAX_EXTERNAL_ARTIFACT_BYTES,
        code="external_observation",
    ) as reader:
        raw_manifest = reader.next_record()
        if raw_manifest is None:
            raise GoldCaptureError("external_observation_empty")
        try:
            manifest = _model_from_json_record(ExternalObservationManifest, raw_manifest)
        except ValidationError as exc:
            raise GoldCaptureError("external_observation_manifest_invalid", line=1) from exc
        if (
            manifest.gold_sha256 != gold.sha256
            or manifest.query_count != len(gold.queries)
            or manifest.generation_manifest.sha256 == "0" * 64
        ):
            raise GoldCaptureError("external_observation_manifest_binding_mismatch", line=1)
        if (manifest.lexical_rank_artifact is None) != (lexical_rank_path is None):
            raise GoldCaptureError("external_lexical_sidecar_path_mismatch", line=1)
        if (
            manifest.lane == "qwen_page"
            and candidate_source_commit is not None
            and manifest.source_commit != candidate_source_commit
        ):
            raise GoldCaptureError("candidate_source_commit_mismatch", line=1)

        page_manifest: PageGenerationManifest | None = None
        if manifest.lane == "v109_baseline":
            source_manifest, generation_binding = _load_generation_manifest(
                generation_manifest_path
            )
            if (
                source_manifest.schema_version != "cardrag.generation.v4"
                or source_manifest.serving_schema != "cardrag.serving-db.v4"
                or source_manifest.generation_id != manifest.generation_id
                or source_manifest.serving_database.sha256 != manifest.serving_database.sha256
                or source_manifest.serving_database.size_bytes
                != manifest.serving_database.size_bytes
            ):
                raise GoldCaptureError("v109_generation_manifest_mismatch")
            if release_gate and (
                manifest.generation_id != _V109_PRESERVED_GENERATION_ID
                or manifest.generation_manifest
                != ArtifactBinding(
                    sha256=_V109_PRESERVED_MANIFEST_SHA256,
                    size_bytes=_V109_PRESERVED_MANIFEST_SIZE_BYTES,
                )
                or manifest.serving_database
                != ArtifactBinding(
                    sha256=_V109_PRESERVED_DATABASE_SHA256,
                    size_bytes=_V109_PRESERVED_DATABASE_SIZE_BYTES,
                )
                or generation_binding != manifest.generation_manifest
            ):
                raise GoldCaptureError("v109_preserved_source_anchor_mismatch")
        else:
            page_manifest, generation_binding = _load_page_generation_manifest(
                generation_manifest_path
            )
            if (
                page_manifest.source_commit != manifest.source_commit
                or page_manifest.generation_id != manifest.generation_id
                or page_manifest.serving_database != manifest.serving_database
                or page_manifest.vector_artifact != manifest.vector_artifact
                or page_manifest.row_count != manifest.row_count
                or page_manifest.corpus_inventory_sha256 != manifest.corpus_inventory_sha256
            ):
                raise GoldCaptureError("page_generation_manifest_mismatch")
        if generation_binding != manifest.generation_manifest:
            raise GoldCaptureError("generation_manifest_binding_mismatch")

        _inventory_manifest, inventory, inventory_binding = _load_inventory(
            inventory_path,
            expected_manifest=manifest,
            expected_sha256=expected_inventory_sha256,
        )
        results: list[QueryRunResult] = []
        with ExitStack() as sidecars:
            dense_scores = sidecars.enter_context(
                _verified_sidecar(
                    dense_score_matrix_path,
                    expected=manifest.dense_score_matrix,
                    code="external_dense_score_matrix",
                )
            )
            query_vectors = sidecars.enter_context(
                _verified_sidecar(
                    query_vector_matrix_path,
                    expected=manifest.query_vector_matrix,
                    code="external_query_vector_matrix",
                )
            )
            lexical_mapping = (
                None
                if lexical_rank_path is None or manifest.lexical_rank_artifact is None
                else sidecars.enter_context(
                    _verified_sidecar(
                        lexical_rank_path,
                        expected=manifest.lexical_rank_artifact,
                        code="external_lexical_rank_artifact",
                    )
                )
            )
            dense_end = 0
            vector_end = 0
            lexical_end = 0
            with _verified_corpus_vectors(
                manifest=manifest,
                inventory=inventory,
                database_path=database_path,
                vector_path=vector_path,
                page_manifest=page_manifest,
                source_generation_manifest_path=source_generation_manifest_path,
                source_database_path=source_database_path,
                release_gate=release_gate,
            ) as (corpus, source_database):
                for ordinal, query in enumerate(gold.queries):
                    raw = reader.next_record()
                    if raw is None:
                        raise GoldCaptureError("external_query_missing", line=reader.line + 1)
                    try:
                        observation = _model_from_json_record(ExternalQueryObservation, raw)
                    except ValidationError as exc:
                        raise GoldCaptureError("external_query_invalid", line=reader.line) from exc
                    if (
                        observation.ordinal != ordinal
                        or observation.lane != manifest.lane
                        or observation.query_id != query.query_id
                        or observation.query_sha256
                        != hashlib.sha256(query.question.encode("utf-8")).hexdigest()
                        or observation.dense_count != manifest.row_count
                        or observation.vector_count != manifest.embedding_dimension
                        or observation.dense_offset_bytes != dense_end
                        or observation.vector_offset_bytes != vector_end
                        or (
                            lexical_mapping is None and observation.lexical_offset_bytes is not None
                        )
                        or (
                            lexical_mapping is not None
                            and observation.lexical_offset_bytes != lexical_end
                        )
                    ):
                        raise GoldCaptureError("external_query_binding_mismatch", line=reader.line)
                    dense_payload = _sidecar_segment(
                        dense_scores,
                        offset=observation.dense_offset_bytes,
                        size=observation.dense_size_bytes,
                        expected_sha256=observation.dense_sha256,
                        code="external_dense_segment",
                    )
                    vector_payload = _sidecar_segment(
                        query_vectors,
                        offset=observation.vector_offset_bytes,
                        size=observation.vector_size_bytes,
                        expected_sha256=observation.vector_sha256,
                        code="external_vector_segment",
                    )
                    if lexical_mapping is None:
                        lexical_ranks: dict[int, int] = {}
                    else:
                        if (
                            observation.lexical_offset_bytes is None
                            or observation.lexical_size_bytes is None
                            or observation.lexical_sha256 is None
                        ):
                            raise GoldCaptureError("external_lexical_segment_missing")
                        lexical_payload = _sidecar_segment(
                            lexical_mapping,
                            offset=observation.lexical_offset_bytes,
                            size=observation.lexical_size_bytes,
                            expected_sha256=observation.lexical_sha256,
                            code="external_lexical_segment",
                        )
                        lexical_ranks = _lexical_ranks_from_segment(
                            lexical_payload,
                            observation=observation,
                            inventory_count=len(inventory),
                        )
                        lexical_end += observation.lexical_size_bytes
                    query_vector = _decode_query_vector(
                        vector_payload,
                        expected_sha256=observation.vector_sha256,
                        dimension=manifest.embedding_dimension,
                    )
                    raw_rows = _external_raw_rows(
                        dense_payload=dense_payload,
                        inventory=inventory,
                        lexical_ranks=lexical_ranks,
                    )
                    _validate_external_result(
                        observation,
                        raw_rows=raw_rows,
                        query_vector=query_vector,
                        corpus=corpus,
                        inventory=inventory,
                        expected_lexical_ranks=(
                            None
                            if manifest.lane == "qwen_page"
                            else _v109_lexical_ranks_from_connection(
                                source_database,
                                query.question,
                                limit=manifest.maximum_candidates or 0,
                            )
                        ),
                    )
                    results.append(observation.result)
                    dense_end += observation.dense_size_bytes
                    vector_end += observation.vector_size_bytes
            if (
                dense_end != len(dense_scores)
                or vector_end != len(query_vectors)
                or (lexical_mapping is not None and lexical_end != len(lexical_mapping))
            ):
                raise GoldCaptureError("external_sidecar_trailing_or_missing_bytes")
        if reader.next_record() is not None:
            raise GoldCaptureError("external_observation_trailing_record", line=reader.line)
        if reader.binding != observation_binding:
            raise GoldCaptureError("external_observation_changed_during_validation")

    answer_evidence = _verify_answer_evidence(
        artifacts=answer_evidence_artifacts,
        authoritative_results=results,
        expected_lane=cast(AnswerLane, manifest.lane),
        gold=gold,
        gold_path=gold_path,
        expected_gold_sha256=gold.sha256,
        expected_source_commit=manifest.source_commit,
        expected_generation_id=manifest.generation_id,
        expected_generation_manifest_sha256=manifest.generation_manifest.sha256,
        expected_serving_database_sha256=manifest.serving_database.sha256,
        release_gate=release_gate,
    )

    run_manifest = RunArtifactManifest(
        schema_version="cardrag.gold-run-artifact.v1",
        lane=manifest.lane,
        profile_id=(
            "cardrag.eval.v109-small-rrf.v1"
            if manifest.lane == "v109_baseline"
            else "cardrag.eval.qwen-page.v1"
        ),
        gold_sha256=gold.sha256,
        query_count=len(gold.queries),
        source_version=manifest.source_version,
        source_commit=manifest.source_commit,
        generation_id=manifest.generation_id,
        generation_manifest_sha256=manifest.generation_manifest.sha256,
        serving_schema=manifest.serving_schema,
        embedding_model=manifest.embedding_model,
        embedding_dimension=manifest.embedding_dimension,
        retrieval_policy=manifest.retrieval_policy,
        rrf_k=60 if manifest.lane == "v109_baseline" else None,
        shadow_only=False,
        primary_lane=None,
        shadow_model=None,
    )
    run_payload = _external_run_bytes((run_manifest, *results))
    run_binding = _publish_immutable(output_path, run_payload)
    receipt = LaneCaptureReceipt(
        schema_version=CAPTURE_RECEIPT_SCHEMA,
        lane=manifest.lane,
        capture_mode="external_reproducible",
        capture_phase="final_release" if answer_evidence is not None else "bootstrap_retrieval",
        validation_profile="release_grade" if release_gate else "fixture_only",
        release_eligible=answer_evidence is not None,
        gold_sha256=gold.sha256,
        query_count=len(gold.queries),
        run_artifact=run_binding,
        attestation_artifact=observation_binding,
        source_generation_id=manifest.generation_id,
        source_generation_manifest_sha256=manifest.generation_manifest.sha256,
        source_database_sha256=manifest.serving_database.sha256,
        source_vector_sha256=(
            None if manifest.vector_artifact is None else manifest.vector_artifact.sha256
        ),
        raw_score_artifact_sha256=observation_binding.sha256,
        corpus_inventory=inventory_binding,
        dense_score_matrix=manifest.dense_score_matrix,
        query_vector_matrix=manifest.query_vector_matrix,
        lexical_rank_artifact=manifest.lexical_rank_artifact,
        answer_evidence=answer_evidence,
    )
    _publish_immutable(receipt_path, receipt.canonical_bytes())
    # Reuse the evaluator's strict loader as the final serialization boundary.
    load_run_jsonl(output_path, lane=manifest.lane)
    if inventory_binding.sha256 != manifest.corpus_inventory_sha256:  # pragma: no cover
        raise GoldCaptureError("inventory_binding_lost")
    return receipt


@dataclass(frozen=True, slots=True)
class _NativeScoreSummary:
    coverage: QueryScoreCoverage
    raw_query_binding_sha256: str
    query_vector_f32: bytes
    contract_scores: Mapping[str, float]
    node_scores: Mapping[tuple[str, str], float]
    row_counts: Mapping[str, int]


@dataclass(slots=True)
class _BoundedNativeContractScores:
    """At most four floats per contract, independent of corpus row count."""

    contract_score: float | None = None
    contract_count: int = 0
    highest_child_scores: tuple[float, ...] = ()

    def add(self, view_type: ViewType, score: float) -> None:
        if view_type == "CONTRACT":
            self.contract_count += 1
            if self.contract_score is None:
                self.contract_score = score
            return
        self.highest_child_scores = tuple(
            sorted((*self.highest_child_scores, score), reverse=True)[:3]
        )

    def aggregate(self, policy: DocumentAggregationPolicy) -> float:
        if not self.highest_child_scores:
            raise GoldCaptureError("native_score_contract_child_missing")
        if policy == "max_child":
            return self.highest_child_scores[0]
        if policy == "top3_mean":
            return math.fsum(self.highest_child_scores) / len(self.highest_child_scores)
        if self.contract_count != 1 or self.contract_score is None:
            raise GoldCaptureError("native_score_contract_view_invalid")
        return math.fsum((0.5 * self.contract_score, 0.5 * self.highest_child_scores[0]))


@dataclass(frozen=True, slots=True)
class _NativeRowProvenance:
    row_index: int
    contract_revision_id: str
    node_id: str
    view_type: ViewType
    input_sha256: str
    embedding_profile_id: str


def _native_corpus_provenance(
    repository: V5ExactRepository,
    handle: Any,
) -> tuple[_NativeRowProvenance, ...]:
    vectors = repository._vectors(handle)
    active_ids = {
        revision.contract_revision_id
        for revision in repository._active_revisions(
            handle,
            ContractSearchRequest(query="gold raw provenance"),
        )
    }
    result: list[_NativeRowProvenance] = []
    with handle.connect() as connection:
        database_rows = iter(
            connection.execute(
                """SELECT row_index,contract_revision_id,node_id,view_type,
                          input_sha256,profile_id
                     FROM embedding_views ORDER BY row_index"""
            )
        )
        database = next(database_rows, None)
        for position, contract_revision_id in enumerate(vectors.contract_revision_ids):
            if contract_revision_id not in active_ids:
                continue
            while database is not None and int(database[0]) < position:
                database = next(database_rows, None)
            if (
                vectors.row_indices[position] != position
                or database is None
                or int(database[0]) != position
                or (
                    str(database[1]),
                    str(database[2]),
                    str(database[3]),
                )
                != (
                    contract_revision_id,
                    vectors.node_ids[position],
                    vectors.view_types[position],
                )
                or str(database[5]) != vectors.profile_ids[position]
            ):
                raise GoldCaptureError("native_corpus_row_provenance_mismatch")
            result.append(
                _NativeRowProvenance(
                    row_index=position,
                    contract_revision_id=contract_revision_id,
                    node_id=vectors.node_ids[position],
                    view_type=cast(ViewType, vectors.view_types[position]),
                    input_sha256=str(database[4]),
                    embedding_profile_id=vectors.profile_ids[position],
                )
            )
            database = next(database_rows, None)
    if not result or {row.contract_revision_id for row in result} != active_ids:
        raise GoldCaptureError("native_corpus_row_coverage_mismatch")
    return tuple(result)


def _score_binding(value: ScoreArtifactBinding) -> ArtifactBinding:
    return ArtifactBinding(
        sha256=value.sha256,
        size_bytes=value.size_bytes,
    )


@contextmanager
def _open_native_score_artifact(
    *,
    score_artifact_path: Path,
    score_corpus_inventory_path: Path,
    score_matrix_path: Path,
    score_query_vector_matrix_path: Path,
    expected_score_artifact_sha256: str | None = None,
) -> Iterator[OpenedScoreArtifact]:
    try:
        with open_score_artifact(
            score_artifact_path,
            score_corpus_inventory_path,
            score_matrix_path,
            score_query_vector_matrix_path,
            expected_score_artifact_sha256,
        ) as opened:
            yield opened
    except AggregationProfileError as exc:
        raise GoldCaptureError(
            f"v5_score_{exc.code}",
            line=exc.line,
        ) from exc


def _native_score_summary(
    opened: OpenedScoreArtifact,
    *,
    query_index: int,
    query_id: str,
    query_sha256: str,
    expected_provenance: Sequence[_NativeRowProvenance] | None,
) -> _NativeScoreSummary:
    if opened.manifest.corpus_row_count > _MAX_NATIVE_CAPTURE_CORPUS_ROWS:
        raise GoldCaptureError("native_score_corpus_row_memory_limit_exceeded")
    try:
        coverage = opened.coverages[query_index]
    except IndexError as exc:  # pragma: no cover - loader enforces manifest count
        raise GoldCaptureError("v5_score_query_coverage_missing") from exc
    if (
        coverage.ordinal != query_index
        or coverage.query_id != query_id
        or coverage.query_sha256 != query_sha256
        or coverage.expected_rows != coverage.scored_rows
        or coverage.scored_rows != opened.manifest.corpus_row_count
        or len(opened.inventory) != coverage.scored_rows
    ):
        raise GoldCaptureError("v5_score_query_coverage_mismatch")
    by_contract: defaultdict[str, _BoundedNativeContractScores] = defaultdict(
        _BoundedNativeContractScores
    )
    row_counts: defaultdict[str, int] = defaultdict(int)
    node_scores: dict[tuple[str, str], float] = {}
    parsed_row_count = 0
    scores = opened.scores_for(query_index)
    for ordinal, (identity, raw_score) in enumerate(zip(opened.inventory, scores, strict=True)):
        if expected_provenance is not None and (
            ordinal >= len(expected_provenance)
            or (
                identity.row_index,
                identity.contract_revision_id,
                identity.node_id,
                identity.view_type,
                identity.input_sha256,
                identity.embedding_profile_id,
            )
            != (
                expected_provenance[ordinal].row_index,
                expected_provenance[ordinal].contract_revision_id,
                expected_provenance[ordinal].node_id,
                expected_provenance[ordinal].view_type,
                expected_provenance[ordinal].input_sha256,
                expected_provenance[ordinal].embedding_profile_id,
            )
        ):
            del scores
            raise GoldCaptureError("v5_score_row_binding_mismatch")
        score = float(raw_score)
        by_contract[identity.contract_revision_id].add(identity.view_type, score)
        row_counts[identity.contract_revision_id] += 1
        node_key = (identity.contract_revision_id, identity.node_id)
        node_scores[node_key] = max(node_scores.get(node_key, -math.inf), score)
        parsed_row_count += 1
    del scores
    if (expected_provenance is not None and parsed_row_count != len(expected_provenance)) or len(
        by_contract
    ) != coverage.active_contracts:
        raise GoldCaptureError("v5_score_active_contract_count_mismatch")
    contract_scores = {
        contract_id: accumulator.aggregate(opened.manifest.runtime_document_aggregation_policy)
        for contract_id, accumulator in by_contract.items()
    }
    vector = opened.query_vector_for(query_index)
    query_vector_f32 = vector.astype("<f4", copy=False).tobytes()
    del vector
    return _NativeScoreSummary(
        coverage=coverage,
        raw_query_binding_sha256=canonical_sha256(
            {
                "corpus_inventory_sha256": opened.corpus_inventory_binding.sha256,
                "score_sha256": coverage.score_sha256,
            }
        ),
        query_vector_f32=query_vector_f32,
        contract_scores=contract_scores,
        node_scores=node_scores,
        row_counts=dict(row_counts),
    )


def _restrict_native_summary(
    summary: _NativeScoreSummary,
    active_contract_ids: set[str],
) -> _NativeScoreSummary:
    if not active_contract_ids or not active_contract_ids.issubset(summary.contract_scores):
        raise GoldCaptureError("native_runtime_scope_not_in_raw_scores")
    return _NativeScoreSummary(
        coverage=summary.coverage,
        raw_query_binding_sha256=summary.raw_query_binding_sha256,
        query_vector_f32=summary.query_vector_f32,
        contract_scores={
            contract_id: score
            for contract_id, score in summary.contract_scores.items()
            if contract_id in active_contract_ids
        },
        node_scores={
            key: score
            for key, score in summary.node_scores.items()
            if key[0] in active_contract_ids
        },
        row_counts={
            contract_id: count
            for contract_id, count in summary.row_counts.items()
            if contract_id in active_contract_ids
        },
    )


async def _validate_fresh_exact_score_stream(
    *,
    repository: V5ExactRepository,
    handle: Any,
    query: str,
    opened: OpenedScoreArtifact,
    query_index: int,
    summary: _NativeScoreSummary,
) -> None:
    expected_scores = opened.scores_for(query_index)
    cursor = 0

    def compare_row(actual: ExactCapturedRow) -> None:
        nonlocal cursor
        if cursor >= len(opened.inventory):
            raise GoldCaptureError("native_fresh_score_row_extra")
        recorded = opened.inventory[cursor]
        expected_score = expected_scores[cursor]
        if (
            recorded.row_index != actual.row_index
            or recorded.contract_revision_id != actual.contract_revision_id
            or recorded.node_id != actual.node_id
            or recorded.view_type != actual.view_type
            or recorded.input_sha256 != actual.input_sha256
            or recorded.embedding_profile_id != actual.embedding_profile_id
            or np.asarray(expected_score, dtype="<f4").tobytes()
            != np.asarray(actual.score, dtype="<f4").tobytes()
        ):
            raise GoldCaptureError("native_fresh_score_row_mismatch")
        cursor += 1

    fresh = await repository.capture_unscoped_current_score_stream(
        query,
        handle,
        score_sink=compare_row,
        block_rows=VECTOR_BLOCK_ROWS,
    )
    if (
        fresh.query_sha256 != summary.coverage.query_sha256
        or fresh.query_vector_sha256 != summary.coverage.query_vector_sha256
        or fresh.query_vector_f32 != summary.query_vector_f32
        or fresh.expected_active_contracts != summary.coverage.active_contracts
        or fresh.expected_rows != summary.coverage.expected_rows
        or fresh.scored_rows != summary.coverage.scored_rows
        or fresh.exact_blocks != math.ceil(fresh.expected_rows / VECTOR_BLOCK_ROWS)
        or cursor != summary.coverage.scored_rows
    ):
        raise GoldCaptureError("native_fresh_score_coverage_mismatch")


def _native_primary_rankings(
    summary: _NativeScoreSummary,
) -> tuple[tuple[RetrievedContract, ...], tuple[RetrievedSpan, ...]]:
    ranked_contract_ids = sorted(
        summary.contract_scores,
        key=lambda contract_id: (-summary.contract_scores[contract_id], contract_id),
    )[:100]
    contracts = tuple(
        RetrievedContract(
            contract_revision_id=contract_id,
            rank=rank,
            score=summary.contract_scores[contract_id],
        )
        for rank, contract_id in enumerate(ranked_contract_ids, start=1)
    )
    selected = set(ranked_contract_ids)
    ranked_nodes = sorted(
        (
            (contract_id, node_id, score)
            for (contract_id, node_id), score in summary.node_scores.items()
            if contract_id in selected
        ),
        key=lambda item: (-item[2], item[0], item[1]),
    )[:1_000]
    spans = tuple(
        RetrievedSpan(
            span_id=node_id,
            contract_revision_id=contract_id,
            rank=rank,
            score=score,
        )
        for rank, (contract_id, node_id, score) in enumerate(ranked_nodes, start=1)
    )
    return contracts, spans


def _shadow_rankings(
    values: Sequence[tuple[str, str, float]],
) -> tuple[tuple[RetrievedContract, ...], tuple[RetrievedSpan, ...]]:
    unique: dict[tuple[str, str], float] = {}
    for contract_id, span_id, score in values:
        key = (contract_id, span_id)
        unique[key] = max(unique.get(key, -math.inf), score)
    ordered = sorted(
        ((contract_id, span_id, score) for (contract_id, span_id), score in unique.items()),
        key=lambda item: (-item[2], item[0], item[1]),
    )[:1_000]
    spans = tuple(
        RetrievedSpan(
            span_id=span_id,
            contract_revision_id=contract_id,
            rank=rank,
            score=score,
        )
        for rank, (contract_id, span_id, score) in enumerate(ordered, start=1)
    )
    return _contracts_from_spans(spans), spans


def _shadow_rankings_with_primary(
    values: Sequence[tuple[str, str, float]],
    *,
    primary_contracts: Sequence[RetrievedContract],
    primary_spans: Sequence[RetrievedSpan],
) -> tuple[tuple[RetrievedContract, ...], tuple[RetrievedSpan, ...]]:
    """Promote shadow matches while retaining the exact lane as the fallback order."""

    promoted_contracts, promoted_spans = _shadow_rankings(values)
    seen_spans = {item.span_id for item in promoted_spans}
    combined_spans = (
        *promoted_spans,
        *(item for item in primary_spans if item.span_id not in seen_spans),
    )
    spans = tuple(
        RetrievedSpan(
            span_id=item.span_id,
            contract_revision_id=item.contract_revision_id,
            rank=rank,
            score=item.score,
        )
        for rank, item in enumerate(combined_spans[:1_000], start=1)
    )
    seen_contracts = {item.contract_revision_id for item in promoted_contracts}
    combined_contracts = (
        *promoted_contracts,
        *(item for item in primary_contracts if item.contract_revision_id not in seen_contracts),
    )
    contracts = tuple(
        RetrievedContract(
            contract_revision_id=item.contract_revision_id,
            rank=rank,
            score=item.score,
        )
        for rank, item in enumerate(combined_contracts[:1_000], start=1)
    )
    return contracts, spans


def _load_reranker_artifact_by_sha256(
    state_root: Path,
    artifact_sha256: str,
) -> RerankerShadowArtifact:
    root = state_root / "audit-reports" / "reranker-shadow"
    if root.is_symlink() or not root.is_dir():
        raise GoldCaptureError("reranker_artifact_directory_invalid")
    found: RerankerShadowArtifact | None = None
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if path.is_symlink() or not path.is_file():
            raise GoldCaptureError("reranker_artifact_entry_unsafe")
        payload = _read_regular(
            path,
            maximum_bytes=8 * 1024 * 1024,
            code="reranker_artifact",
        )
        try:
            candidate = RerankerShadowArtifact.model_validate_json(payload)
        except Exception as exc:
            raise GoldCaptureError("reranker_artifact_invalid") from exc
        if payload != candidate.canonical_bytes():
            raise GoldCaptureError("reranker_artifact_not_canonical")
        if candidate.artifact_sha256 == artifact_sha256:
            if found is not None:
                raise GoldCaptureError("reranker_artifact_identity_duplicate")
            found = candidate
    if found is None or found.status != "succeeded":
        raise GoldCaptureError("reranker_artifact_missing_or_failed")
    return found


def _native_runtime_scope(
    repository: V5ExactRepository,
    handle: Any,
    query: str,
) -> tuple[ContractSearchRequest, set[str]]:
    request = ContractSearchRequest(query=query, limit=100)
    catalog = repository._resolve_catalog(handle, request)
    effective_request = (
        request.model_copy(update={"product_lineage_id": catalog.product_lineage_id})
        if catalog.status == "resolved"
        else request
    )
    active_ids = {
        revision.contract_revision_id
        for revision in repository._active_revisions(handle, effective_request)
    }
    if not active_ids:
        raise GoldCaptureError("native_runtime_scope_empty")
    return request, active_ids


def _validate_native_query_shard(
    *,
    shard: _NativeQueryShard,
    query_index: int,
    query: GoldQuery,
    score_summary: _NativeScoreSummary,
    answer: EvaluatedAnswer,
    repository: V5ExactRepository,
    handle: Any,
    state: Path,
) -> None:
    query_sha256 = hashlib.sha256(query.question.encode("utf-8")).hexdigest()
    attestation = shard.attestation
    _request, runtime_active_ids = _native_runtime_scope(
        repository,
        handle,
        query.question,
    )
    runtime_summary = _restrict_native_summary(score_summary, runtime_active_ids)
    primary_contracts, primary_spans = _native_primary_rankings(runtime_summary)
    runtime_expected_rows = sum(runtime_summary.row_counts.values())
    primary = shard.qwen_structure_exact
    if (
        shard.query_index != query_index
        or attestation.query_id != query.query_id
        or attestation.query_sha256 != query_sha256
        or attestation.query_vector_sha256 != score_summary.coverage.query_vector_sha256
        or attestation.raw_score_query_binding_sha256 != score_summary.raw_query_binding_sha256
        or attestation.raw_expected_embedding_rows != score_summary.coverage.expected_rows
        or attestation.raw_scored_embedding_rows != score_summary.coverage.scored_rows
        or attestation.raw_active_contracts != score_summary.coverage.active_contracts
        or attestation.expected_embedding_rows != runtime_expected_rows
        or attestation.scored_embedding_rows != runtime_expected_rows
        or attestation.expected_active_contracts != len(runtime_active_ids)
        or attestation.scored_contracts != len(runtime_active_ids)
        or attestation.exact_blocks != math.ceil(runtime_expected_rows / VECTOR_BLOCK_ROWS)
        or primary.query_id != query.query_id
        or primary.contracts != primary_contracts
        or primary.spans != primary_spans
        or primary.answer != answer
        or attestation.qwen_structure_exact_result_sha256 != canonical_sha256(primary)
        or attestation.lexical_shadow_result_sha256 != canonical_sha256(shard.lexical_shadow)
        or attestation.reranker_shadow_result_sha256 != canonical_sha256(shard.reranker_shadow)
    ):
        raise GoldCaptureError("native_capture_query_shard_binding_mismatch")

    global_active_ids = set(score_summary.contract_scores)
    lexical_audit = repository._lexical_audit(handle, query.question, global_active_ids)
    if not lexical_audit.enabled or lexical_audit.status != "succeeded":
        raise GoldCaptureError("native_capture_query_shard_lexical_failed")
    lexical_values = tuple(
        (
            contract_id,
            node_id,
            score_summary.node_scores[(contract_id, node_id)],
        )
        for contract_id, node_ids in lexical_audit.nodes_by_revision.items()
        for node_id in node_ids
        if contract_id in runtime_active_ids and (contract_id, node_id) in score_summary.node_scores
    )
    lexical_contracts, lexical_spans = _shadow_rankings_with_primary(
        lexical_values,
        primary_contracts=primary_contracts,
        primary_spans=primary_spans,
    )
    lexical_shadow = shard.lexical_shadow.shadow
    dense_top_by_revision: defaultdict[str, set[str]] = defaultdict(set)
    for (revision_id, node_id), _score in sorted(
        runtime_summary.node_scores.items(),
        key=lambda item: (-item[1], item[0][0], item[0][1]),
    ):
        if len(dense_top_by_revision[revision_id]) < 8:
            dense_top_by_revision[revision_id].add(node_id)
    lexical_additional = sum(
        len(nodes - dense_top_by_revision.get(revision_id, set()))
        for revision_id, nodes in lexical_audit.nodes_by_revision.items()
        if revision_id in global_active_ids
    )
    if (
        lexical_shadow is None
        or lexical_shadow.contracts != lexical_contracts
        or lexical_shadow.spans != lexical_spans
        or attestation.lexical_additional_evidence_count != lexical_additional
    ):
        raise GoldCaptureError("native_capture_query_shard_lexical_mismatch")

    reranker_artifact = _load_reranker_artifact_by_sha256(
        state,
        attestation.reranker_artifact_sha256,
    )
    if (
        reranker_artifact.identity.generation_id != handle.generation_id
        or reranker_artifact.identity.query_sha256 != query_sha256
    ):
        raise GoldCaptureError("native_capture_query_shard_reranker_identity_mismatch")
    bindings = {item.candidate_id: item for item in reranker_artifact.candidates}
    reranker_values = tuple(
        (
            bindings[result.candidate_id].contract_revision_id,
            bindings[result.candidate_id].node_id,
            result.relevance_score,
        )
        for result in reranker_artifact.results
    )
    reranker_contracts, reranker_spans = _shadow_rankings_with_primary(
        reranker_values,
        primary_contracts=primary_contracts,
        primary_spans=primary_spans,
    )
    reranker_shadow = shard.reranker_shadow.shadow
    if (
        reranker_shadow is None
        or reranker_shadow.contracts != reranker_contracts
        or reranker_shadow.spans != reranker_spans
    ):
        raise GoldCaptureError("native_capture_query_shard_reranker_mismatch")


def _safe_state_directory(path: Path) -> Path:
    if path.is_symlink():
        raise GoldCaptureError("capture_state_symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    resolved = path.resolve(strict=True)
    if not stat.S_ISDIR(resolved.stat().st_mode):
        raise GoldCaptureError("capture_state_not_directory")
    return resolved


def _prepare_output_directory(path: Path) -> Path:
    if path.is_symlink():
        raise GoldCaptureError("capture_output_directory_invalid")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    resolved = path.resolve(strict=True)
    if not stat.S_ISDIR(resolved.stat().st_mode):
        raise GoldCaptureError("capture_output_directory_invalid")
    return resolved


def _load_state_model[T: _StrictModel](
    path: Path,
    model: type[T],
) -> T:
    payload = _read_regular(
        path,
        maximum_bytes=_MAX_CAPTURE_STATE_FILE_BYTES,
        code="capture_state_file",
    )
    try:
        value = model.model_validate_json(payload)
    except Exception as exc:
        raise GoldCaptureError("capture_state_model_invalid") from exc
    if payload != value.canonical_bytes():
        raise GoldCaptureError("capture_state_not_canonical")
    return value


def _validate_state_entries(state: Path, query_count: int) -> None:
    allowed = {"identity.json"}
    allowed.update(f"query-{index:03d}.json" for index in range(query_count))
    # RerankerShadowStore owns this bounded subtree.
    allowed.add("audit-reports")
    for entry in state.iterdir():
        if entry.name not in allowed or entry.is_symlink():
            raise GoldCaptureError("capture_state_contains_unsafe_entry")
        if entry.name == "audit-reports":
            if not entry.is_dir():
                raise GoldCaptureError("capture_state_audit_not_directory")
        elif not entry.is_file():
            raise GoldCaptureError("capture_state_entry_not_regular")


def _native_run_manifests(
    attestation: NativeV5AttestationManifest,
) -> dict[EvaluationLane, RunArtifactManifest]:
    common = {
        "schema_version": "cardrag.gold-run-artifact.v1",
        "gold_sha256": attestation.gold_sha256,
        "query_count": attestation.query_count,
        "source_version": "v1.0.10-candidate",
        "source_commit": attestation.source_commit,
        "generation_id": attestation.generation_id,
        "generation_manifest_sha256": attestation.generation_manifest.sha256,
        "serving_schema": "cardrag.serving-db.v5",
        "embedding_model": "qwen/qwen3-embedding-8b",
        "embedding_dimension": 4096,
        "rrf_k": None,
    }
    return {
        "qwen_structure_exact": RunArtifactManifest.model_validate(
            {
                **common,
                "lane": "qwen_structure_exact",
                "profile_id": "cardrag.eval.qwen-structure-exact.v1",
                "retrieval_policy": "qwen_structure_exact",
                "shadow_only": False,
                "primary_lane": None,
                "shadow_model": None,
            }
        ),
        "lexical_shadow": RunArtifactManifest.model_validate(
            {
                **common,
                "lane": "lexical_shadow",
                "profile_id": "cardrag.eval.lexical-shadow.v1",
                "retrieval_policy": "qwen_structure_exact_lexical_shadow",
                "shadow_only": True,
                "primary_lane": "qwen_structure_exact",
                "shadow_model": None,
            }
        ),
        "reranker_shadow": RunArtifactManifest.model_validate(
            {
                **common,
                "lane": "reranker_shadow",
                "profile_id": "cardrag.eval.reranker-shadow.v1",
                "retrieval_policy": "qwen_structure_exact_reranker_shadow",
                "shadow_only": True,
                "primary_lane": "qwen_structure_exact",
                "shadow_model": RERANKER_MODEL,
            }
        ),
    }


async def capture_native_v5_lanes(
    *,
    gold_path: Path,
    expected_gold_sha256: str,
    score_artifact_path: Path,
    score_corpus_inventory_path: Path,
    score_matrix_path: Path,
    score_query_vector_matrix_path: Path,
    expected_score_artifact_sha256: str,
    answer_artifact_path: Path | None,
    expected_answer_artifact_sha256: str | None,
    generation_manifest_path: Path,
    generation_directory: Path,
    object_root: Path,
    output_directory: Path,
    state_directory: Path,
    source_commit: str,
    embedder: OpenRouterEmbedder,
    reranker_lane: RerankerShadowLane,
    expected_source_commit: str | None = None,
    release_gate: bool = True,
) -> NativeV5CaptureResult:
    """Capture structure exact and both shadow lanes from the actual v5 APIs."""

    gold = load_gold_jsonl(gold_path, release_gate=release_gate)
    if gold.sha256 != expected_gold_sha256:
        raise GoldCaptureError("gold_sha256_mismatch")
    actual_source_commit = _validated_source_commit(source_commit)
    candidate_source_commit = _validated_expected_source_commit(
        expected_source_commit,
        release_gate=release_gate,
    )
    if candidate_source_commit is not None and actual_source_commit != candidate_source_commit:
        raise GoldCaptureError("candidate_source_commit_mismatch")
    generation_manifest, generation_binding = _load_generation_manifest(generation_manifest_path)
    if generation_manifest.schema_version != "cardrag.generation.v5":
        raise GoldCaptureError("native_capture_requires_generation_v5")
    resolved_generation = await asyncio.to_thread(generation_directory.resolve, strict=True)
    if resolved_generation.name != generation_manifest.generation_id:
        raise GoldCaptureError("generation_directory_manifest_mismatch")
    database_checkpoint = _regular_artifact_checkpoint(
        resolved_generation / "index.sqlite3",
        maximum_bytes=_MAX_DATABASE_BYTES,
        code="v5_serving_database",
    )
    database_binding = database_checkpoint.binding
    sidecar_binding = _hash_regular(
        resolved_generation / "vectors.f32",
        maximum_bytes=_MAX_SIDECAR_BYTES,
        code="v5_vector_sidecar",
    )
    if (
        database_binding.sha256 != generation_manifest.serving_database.sha256
        or database_binding.size_bytes != generation_manifest.serving_database.size_bytes
        or generation_manifest.vector_sidecar is None
        or sidecar_binding.sha256 != generation_manifest.vector_sidecar.artifact.sha256
        or sidecar_binding.size_bytes != generation_manifest.vector_sidecar.artifact.size_bytes
        or generation_manifest.exact_row_corpus_sha256 is None
        or generation_manifest.primary_embedding_profile_id is None
    ):
        raise GoldCaptureError("native_generation_artifact_binding_mismatch")
    with _open_native_score_artifact(
        score_artifact_path=score_artifact_path,
        score_corpus_inventory_path=score_corpus_inventory_path,
        score_matrix_path=score_matrix_path,
        score_query_vector_matrix_path=score_query_vector_matrix_path,
        expected_score_artifact_sha256=expected_score_artifact_sha256,
    ) as opened_scores:
        score_manifest = opened_scores.manifest
        score_binding = _score_binding(opened_scores.score_artifact_binding)
        score_inventory_binding = _score_binding(opened_scores.corpus_inventory_binding)
        score_matrix_binding = _score_binding(opened_scores.score_matrix_binding)
        score_query_vector_binding = _score_binding(opened_scores.query_vector_matrix_binding)
    if (
        score_manifest.gold_sha256 != gold.sha256
        or score_manifest.query_count != len(gold.queries)
        or score_manifest.validation_profile
        != ("release_grade" if release_gate else "fixture_only")
        or score_manifest.source_commit != actual_source_commit
        or score_manifest.generation_id != generation_manifest.generation_id
        or score_manifest.generation_manifest_sha256 != generation_binding.sha256
        or score_manifest.serving_database_sha256 != database_binding.sha256
        or score_manifest.vector_sidecar_sha256 != sidecar_binding.sha256
        or score_manifest.exact_row_corpus_sha256 != generation_manifest.exact_row_corpus_sha256
        or score_manifest.embedding_profile_id != generation_manifest.primary_embedding_profile_id
    ):
        raise GoldCaptureError("v5_score_manifest_binding_mismatch", line=1)
    if (answer_artifact_path is None) != (expected_answer_artifact_sha256 is None):
        raise GoldCaptureError("bootstrap_answer_artifact_arguments_incomplete")
    if release_gate and answer_artifact_path is not None:
        raise GoldCaptureError("release_bootstrap_answer_artifact_forbidden")
    answer_binding: ArtifactBinding | None
    if answer_artifact_path is None:
        answers = _bootstrap_answers(gold)
        answer_binding = None
    else:
        _answer_manifest, answers, answer_binding = _load_answers(
            answer_artifact_path,
            gold=gold,
            lane="qwen_structure_exact",
            generation_id=generation_manifest.generation_id,
            generation_manifest_sha256=generation_binding.sha256,
        )
        if answer_binding.sha256 != expected_answer_artifact_sha256:
            raise GoldCaptureError("answer_artifact_sha256_mismatch")
        if release_gate and answers != _bootstrap_answers(gold):
            raise GoldCaptureError("bootstrap_answer_artifact_must_be_label_independent")

    handle = load_generation_handle(
        resolved_generation,
        object_root,
        maximum_vector_bytes=1024 * 1024 * 1024,
        maximum_vector_sidecar_bytes=_MAX_SIDECAR_BYTES,
        maximum_resident_vector_bytes=1024 * 1024 * 1024,
        maximum_database_bytes=_MAX_DATABASE_BYTES,
        expected_generation_id=generation_manifest.generation_id,
        expected_embedding_model="qwen/qwen3-embedding-8b",
        expected_embedding_count=generation_manifest.embedding_contract.count,
    )
    if (
        handle.metadata.schema_id != "cardrag.serving-db.v5"
        or handle.metadata.primary_embedding_profile_id
        != generation_manifest.primary_embedding_profile_id
        or handle.metadata.exact_row_corpus_sha256 != generation_manifest.exact_row_corpus_sha256
    ):
        raise GoldCaptureError("native_runtime_generation_binding_mismatch")
    attestation_manifest = NativeV5AttestationManifest(
        schema_version=NATIVE_V5_MANIFEST_SCHEMA,
        capture_phase="bootstrap_retrieval",
        validation_profile="release_grade" if release_gate else "fixture_only",
        capture_mode="native_v5",
        synthetic=False,
        gold_sha256=gold.sha256,
        query_count=len(gold.queries),
        source_commit=actual_source_commit,
        generation_id=generation_manifest.generation_id,
        generation_manifest=generation_binding,
        serving_database=database_binding,
        vector_sidecar=sidecar_binding,
        exact_row_corpus_sha256=generation_manifest.exact_row_corpus_sha256,
        embedding_profile_id=generation_manifest.primary_embedding_profile_id,
        embedding_model="qwen/qwen3-embedding-8b",
        embedding_dimension=4096,
        score_artifact=score_binding,
        score_corpus_inventory=score_inventory_binding,
        score_matrix=score_matrix_binding,
        score_query_vector_matrix=score_query_vector_binding,
        answer_artifact=answer_binding,
        answer_evidence=None,
        raw_score_api="cardrag_mcp.exact.V5ExactRepository.capture_unscoped_current_scores",
        exact_api="cardrag_mcp.exact.V5ExactRepository.search",
        lexical_api="cardrag_mcp.exact.V5ExactRepository.search.lexical_shadow",
        reranker_api="cardrag_mcp.reranker.RerankerShadowLane.observe",
        reranker_model=RERANKER_MODEL,
    )
    identity = _NativeCaptureIdentity(
        schema_version=NATIVE_CAPTURE_IDENTITY_SCHEMA,
        attestation_manifest=attestation_manifest,
        query_ids=tuple(query.query_id for query in gold.queries),
    )
    state = _safe_state_directory(state_directory)
    identity_path = state / "identity.json"
    _publish_immutable(identity_path, identity.canonical_bytes())
    if _load_state_model(identity_path, _NativeCaptureIdentity) != identity:
        raise GoldCaptureError("native_capture_resume_identity_mismatch")
    _validate_state_entries(state, len(gold.queries))

    repository = V5ExactRepository(cast(Any, _CaptureStore(state)), embedder)
    native_provenance = _native_corpus_provenance(repository, handle)
    shards: list[_NativeQueryShard] = []
    resumed_queries = 0
    with _open_native_score_artifact(
        score_artifact_path=score_artifact_path,
        score_corpus_inventory_path=score_corpus_inventory_path,
        score_matrix_path=score_matrix_path,
        score_query_vector_matrix_path=score_query_vector_matrix_path,
        expected_score_artifact_sha256=expected_score_artifact_sha256,
    ) as opened_scores:
        if (
            opened_scores.manifest != score_manifest
            or _score_binding(opened_scores.score_artifact_binding) != score_binding
            or _score_binding(opened_scores.corpus_inventory_binding) != score_inventory_binding
            or _score_binding(opened_scores.score_matrix_binding) != score_matrix_binding
            or _score_binding(opened_scores.query_vector_matrix_binding)
            != score_query_vector_binding
        ):
            raise GoldCaptureError("v5_score_artifact_changed_during_capture")

        for query_index, query in enumerate(gold.queries):
            query_sha256 = hashlib.sha256(query.question.encode("utf-8")).hexdigest()
            score_summary = _native_score_summary(
                opened_scores,
                query_index=query_index,
                query_id=query.query_id,
                query_sha256=query_sha256,
                expected_provenance=native_provenance,
            )
            shard_path = state / f"query-{query_index:03d}.json"
            if shard_path.exists() or shard_path.is_symlink():
                shard = _load_state_model(shard_path, _NativeQueryShard)
                try:
                    _validate_native_query_shard(
                        shard=shard,
                        query_index=query_index,
                        query=query,
                        score_summary=score_summary,
                        answer=answers[query.query_id],
                        repository=repository,
                        handle=handle,
                        state=state,
                    )
                except GoldCaptureError as exc:
                    raise GoldCaptureError("native_capture_resume_shard_mismatch") from exc
                resumed_queries += 1
                shards.append(shard)
                continue

            await _validate_fresh_exact_score_stream(
                repository=repository,
                handle=handle,
                query=query.question,
                opened=opened_scores,
                query_index=query_index,
                summary=score_summary,
            )
            request, runtime_active_ids = _native_runtime_scope(
                repository,
                handle,
                query.question,
            )
            runtime_summary = _restrict_native_summary(score_summary, runtime_active_ids)
            runtime_expected_rows = sum(runtime_summary.row_counts.values())
            page = await repository.search(request, handle=handle)
            coverage = page.coverage
            if (
                page.generation_id != generation_manifest.generation_id
                or coverage.approximate
                or coverage.search_mode != "exact"
                or coverage.expected_embedding_rows != runtime_expected_rows
                or coverage.scored_embedding_rows != runtime_expected_rows
                or coverage.expected_active_contracts != len(runtime_active_ids)
                or coverage.scored_contracts != len(runtime_active_ids)
                or coverage.exact_blocks < 1
                or coverage.lexical_status != "succeeded"
                or coverage.lexical_influenced_ranking
                or coverage.reranker_influenced_ranking
            ):
                raise GoldCaptureError("native_exact_api_coverage_mismatch")
            primary_contracts, primary_spans = _native_primary_rankings(runtime_summary)
            api_contract_ids = tuple(
                bundle.contract.contract_revision_id for bundle in page.bundles
            )
            expected_api_prefix = tuple(
                item.contract_revision_id for item in primary_contracts[: len(api_contract_ids)]
            )
            if api_contract_ids != expected_api_prefix:
                raise GoldCaptureError("native_exact_api_ranking_mismatch")
            answer = answers[query.query_id]
            primary = QueryRunResult(
                schema_version="cardrag.gold-run-result.v1",
                query_id=query.query_id,
                lane="qwen_structure_exact",
                contracts=primary_contracts,
                spans=primary_spans,
                answer=answer,
            )

            global_active_ids = set(score_summary.contract_scores)
            lexical_audit = repository._lexical_audit(
                handle,
                query.question,
                global_active_ids,
            )
            if not lexical_audit.enabled or lexical_audit.status != "succeeded":
                raise GoldCaptureError("native_lexical_shadow_failed")
            lexical_values = tuple(
                (
                    contract_id,
                    node_id,
                    score_summary.node_scores[(contract_id, node_id)],
                )
                for contract_id, node_ids in lexical_audit.nodes_by_revision.items()
                for node_id in node_ids
                if contract_id in runtime_active_ids
                and (contract_id, node_id) in score_summary.node_scores
            )
            lexical_contracts, lexical_spans = _shadow_rankings_with_primary(
                lexical_values,
                primary_contracts=primary.contracts,
                primary_spans=primary.spans,
            )
            lexical_global_count = sum(map(len, lexical_audit.nodes_by_revision.values()))
            if coverage.lexical_global_matched_evidence_count != lexical_global_count:
                raise GoldCaptureError("native_lexical_global_coverage_mismatch")
            lexical = QueryRunResult(
                schema_version="cardrag.gold-run-result.v1",
                query_id=query.query_id,
                lane="lexical_shadow",
                contracts=primary.contracts,
                spans=primary.spans,
                answer=primary.answer,
                shadow=ShadowObservation(
                    kind="lexical",
                    influenced_primary_ordering=False,
                    contracts=lexical_contracts,
                    spans=lexical_spans,
                ),
            )

            candidates = repository._reranker_candidates(page.bundles)
            diagnostics = await reranker_lane.observe(
                generation_id=generation_manifest.generation_id,
                query=query.question,
                candidates=candidates,
            )
            if diagnostics.status != "succeeded" or diagnostics.artifact_sha256 is None:
                raise GoldCaptureError("native_reranker_shadow_failed")
            reranker_artifact = _load_reranker_artifact_by_sha256(
                state,
                diagnostics.artifact_sha256,
            )
            bindings = {item.candidate_id: item for item in reranker_artifact.candidates}
            reranker_values = tuple(
                (
                    bindings[result.candidate_id].contract_revision_id,
                    bindings[result.candidate_id].node_id,
                    result.relevance_score,
                )
                for result in reranker_artifact.results
            )
            reranker_contracts, reranker_spans = _shadow_rankings_with_primary(
                reranker_values,
                primary_contracts=primary.contracts,
                primary_spans=primary.spans,
            )
            reranker = QueryRunResult(
                schema_version="cardrag.gold-run-result.v1",
                query_id=query.query_id,
                lane="reranker_shadow",
                contracts=primary.contracts,
                spans=primary.spans,
                answer=primary.answer,
                shadow=ShadowObservation(
                    kind="reranker",
                    influenced_primary_ordering=False,
                    contracts=reranker_contracts,
                    spans=reranker_spans,
                ),
            )
            query_attestation = NativeV5QueryAttestation(
                schema_version=NATIVE_V5_QUERY_SCHEMA,
                query_id=query.query_id,
                query_sha256=query_sha256,
                query_vector_sha256=score_summary.coverage.query_vector_sha256,
                raw_score_query_binding_sha256=score_summary.raw_query_binding_sha256,
                raw_expected_embedding_rows=score_summary.coverage.expected_rows,
                raw_scored_embedding_rows=score_summary.coverage.scored_rows,
                raw_active_contracts=score_summary.coverage.active_contracts,
                expected_embedding_rows=coverage.expected_embedding_rows,
                scored_embedding_rows=coverage.scored_embedding_rows,
                expected_active_contracts=coverage.expected_active_contracts,
                scored_contracts=coverage.scored_contracts,
                exact_blocks=coverage.exact_blocks,
                exact_response_sha256=canonical_sha256(page),
                lexical_status="succeeded",
                lexical_additional_evidence_count=coverage.lexical_global_additional_evidence_count,
                reranker_artifact_sha256=reranker_artifact.artifact_sha256,
                qwen_structure_exact_result_sha256=canonical_sha256(primary),
                lexical_shadow_result_sha256=canonical_sha256(lexical),
                reranker_shadow_result_sha256=canonical_sha256(reranker),
            )
            shard = _NativeQueryShard(
                schema_version=NATIVE_QUERY_SHARD_SCHEMA,
                query_index=query_index,
                attestation=query_attestation,
                qwen_structure_exact=primary,
                lexical_shadow=lexical,
                reranker_shadow=reranker,
            )
            _validate_native_query_shard(
                shard=shard,
                query_index=query_index,
                query=query,
                score_summary=score_summary,
                answer=answer,
                repository=repository,
                handle=handle,
                state=state,
            )
            _publish_immutable(shard_path, shard.canonical_bytes())
            shards.append(_load_state_model(shard_path, _NativeQueryShard))
    if (
        sum(shard.attestation.raw_scored_embedding_rows for shard in shards)
        != score_manifest.score_count
    ):
        raise GoldCaptureError("v5_score_manifest_score_count_mismatch")
    _verify_regular_artifact_checkpoint(database_checkpoint)

    output_directory = await asyncio.to_thread(_prepare_output_directory, output_directory)
    attestation_path = output_directory / "native-v5-attestation.jsonl"
    attestation_binding = _publish_immutable(
        attestation_path,
        _jsonl_bytes(
            (
                attestation_manifest,
                *(shard.attestation for shard in shards),
            )
        ),
    )
    manifests = _native_run_manifests(attestation_manifest)
    run_paths: dict[EvaluationLane, Path] = {}
    receipt_paths: dict[EvaluationLane, Path] = {}
    result_attribute: dict[NativeV5Lane, str] = {
        "qwen_structure_exact": "qwen_structure_exact",
        "lexical_shadow": "lexical_shadow",
        "reranker_shadow": "reranker_shadow",
    }
    for lane in NATIVE_V5_LANES:
        typed_lane = lane
        run_path = output_directory / f"{lane}.jsonl"
        results = tuple(getattr(shard, result_attribute[lane]) for shard in shards)
        run_binding = _publish_immutable(
            run_path,
            _jsonl_bytes((manifests[typed_lane], *results)),
        )
        receipt = LaneCaptureReceipt(
            schema_version=CAPTURE_RECEIPT_SCHEMA,
            lane=typed_lane,
            capture_mode="native_v5",
            capture_phase="bootstrap_retrieval",
            validation_profile="release_grade" if release_gate else "fixture_only",
            release_eligible=False,
            gold_sha256=gold.sha256,
            query_count=len(gold.queries),
            run_artifact=run_binding,
            attestation_artifact=attestation_binding,
            source_generation_id=generation_manifest.generation_id,
            source_generation_manifest_sha256=generation_binding.sha256,
            source_database_sha256=database_binding.sha256,
            source_vector_sha256=sidecar_binding.sha256,
            raw_score_artifact_sha256=score_binding.sha256,
            corpus_inventory=score_inventory_binding,
            dense_score_matrix=score_matrix_binding,
            query_vector_matrix=score_query_vector_binding,
            answer_evidence=None,
        )
        receipt_path = output_directory / f"{lane}.capture-receipt.json"
        _publish_immutable(receipt_path, receipt.canonical_bytes())
        load_run_jsonl(run_path, lane=typed_lane)
        run_paths[typed_lane] = run_path
        receipt_paths[typed_lane] = receipt_path
    return NativeV5CaptureResult(
        run_paths=run_paths,
        receipt_paths=receipt_paths,
        attestation_path=attestation_path,
        resumed_queries=resumed_queries,
    )


def _load_receipt(path: Path) -> tuple[LaneCaptureReceipt, ArtifactBinding]:
    payload = _read_regular(path, maximum_bytes=_MAX_MANIFEST_BYTES, code="capture_receipt")
    try:
        receipt = LaneCaptureReceipt.model_validate_json(payload)
    except Exception as exc:
        raise GoldCaptureError("capture_receipt_invalid") from exc
    if payload != receipt.canonical_bytes():
        raise GoldCaptureError("capture_receipt_not_canonical")
    return receipt, ArtifactBinding(
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
    )


def validate_native_v5_capture(
    *,
    gold_path: Path,
    expected_gold_sha256: str,
    score_artifact_path: Path,
    score_corpus_inventory_path: Path,
    score_matrix_path: Path,
    score_query_vector_matrix_path: Path,
    answer_artifact_path: Path | None,
    generation_manifest_path: Path,
    generation_directory: Path,
    object_root: Path,
    attestation_path: Path,
    run_paths: Mapping[EvaluationLane, Path],
    receipt_paths: Mapping[EvaluationLane, Path],
    reranker_state_root: Path,
    expected_source_commit: str | None = None,
    release_gate: bool = True,
) -> dict[EvaluationLane, LaneCaptureReceipt]:
    """Recompute native evidence bindings without network or serving calls."""

    native_lanes = ("qwen_structure_exact", "lexical_shadow", "reranker_shadow")
    if set(run_paths) != set(native_lanes) or set(receipt_paths) != set(native_lanes):
        raise GoldCaptureError("native_validation_lane_set_invalid")
    gold = load_gold_jsonl(gold_path, release_gate=release_gate)
    if gold.sha256 != expected_gold_sha256:
        raise GoldCaptureError("gold_sha256_mismatch")
    candidate_source_commit = _validated_expected_source_commit(
        expected_source_commit,
        release_gate=release_gate,
    )
    generation, generation_binding = _load_generation_manifest(generation_manifest_path)
    if generation.schema_version != "cardrag.generation.v5":
        raise GoldCaptureError("native_capture_requires_generation_v5")
    database_checkpoint = _regular_artifact_checkpoint(
        generation_directory / "index.sqlite3",
        maximum_bytes=_MAX_DATABASE_BYTES,
        code="v5_serving_database",
    )
    database_binding = database_checkpoint.binding
    sidecar_binding = _hash_regular(
        generation_directory / "vectors.f32",
        maximum_bytes=_MAX_SIDECAR_BYTES,
        code="v5_vector_sidecar",
    )
    with _open_native_score_artifact(
        score_artifact_path=score_artifact_path,
        score_corpus_inventory_path=score_corpus_inventory_path,
        score_matrix_path=score_matrix_path,
        score_query_vector_matrix_path=score_query_vector_matrix_path,
    ) as initial_scores:
        score_manifest = initial_scores.manifest
        score_binding = _score_binding(initial_scores.score_artifact_binding)
        score_inventory_binding = _score_binding(initial_scores.corpus_inventory_binding)
        score_matrix_binding = _score_binding(initial_scores.score_matrix_binding)
        score_query_vector_binding = _score_binding(initial_scores.query_vector_matrix_binding)
    answer_binding: ArtifactBinding | None
    if release_gate and answer_artifact_path is not None:
        raise GoldCaptureError("release_bootstrap_answer_artifact_forbidden")
    if answer_artifact_path is None:
        answers = _bootstrap_answers(gold)
        answer_binding = None
    else:
        _answer_manifest, answers, answer_binding = _load_answers(
            answer_artifact_path,
            gold=gold,
            lane="qwen_structure_exact",
            generation_id=generation.generation_id,
            generation_manifest_sha256=generation_binding.sha256,
        )
        if release_gate and answers != _bootstrap_answers(gold):
            raise GoldCaptureError("bootstrap_answer_artifact_must_be_label_independent")
    handle = load_generation_handle(
        generation_directory,
        object_root,
        maximum_vector_bytes=1024 * 1024 * 1024,
        maximum_vector_sidecar_bytes=_MAX_SIDECAR_BYTES,
        maximum_resident_vector_bytes=1024 * 1024 * 1024,
        maximum_database_bytes=_MAX_DATABASE_BYTES,
        expected_generation_id=generation.generation_id,
        expected_embedding_model="qwen/qwen3-embedding-8b",
        expected_embedding_count=generation.embedding_contract.count,
    )
    repository = V5ExactRepository(
        cast(Any, _CaptureStore(reranker_state_root)),
        cast(Any, None),
    )
    native_provenance = _native_corpus_provenance(repository, handle)
    datasets = {
        lane: load_run_jsonl(run_paths[cast(EvaluationLane, lane)], lane=cast(EvaluationLane, lane))
        for lane in native_lanes
    }
    if candidate_source_commit is not None and any(
        dataset.manifest.source_commit != candidate_source_commit for dataset in datasets.values()
    ):
        raise GoldCaptureError("candidate_source_commit_mismatch")
    attestation_binding = _hash_regular(
        attestation_path,
        maximum_bytes=MAX_JSONL_BYTES,
        code="native_attestation",
    )
    receipts: dict[EvaluationLane, LaneCaptureReceipt] = {}
    for lane in native_lanes:
        typed_lane = cast(EvaluationLane, lane)
        receipt, _receipt_binding = _load_receipt(receipt_paths[typed_lane])
        run_binding = _hash_regular(
            run_paths[typed_lane],
            maximum_bytes=MAX_JSONL_BYTES,
            code="run_artifact",
        )
        if (
            receipt.lane != typed_lane
            or receipt.capture_mode != "native_v5"
            or receipt.capture_phase != "bootstrap_retrieval"
            or receipt.validation_profile != ("release_grade" if release_gate else "fixture_only")
            or receipt.release_eligible
            or receipt.answer_evidence is not None
            or receipt.gold_sha256 != gold.sha256
            or receipt.query_count != len(gold.queries)
            or receipt.run_artifact != run_binding
            or receipt.attestation_artifact != attestation_binding
            or receipt.source_generation_id != generation.generation_id
            or receipt.source_generation_manifest_sha256 != generation_binding.sha256
            or receipt.source_database_sha256 != database_binding.sha256
            or receipt.source_vector_sha256 != sidecar_binding.sha256
            or receipt.raw_score_artifact_sha256 != score_binding.sha256
            or receipt.corpus_inventory != score_inventory_binding
            or receipt.dense_score_matrix != score_matrix_binding
            or receipt.query_vector_matrix != score_query_vector_binding
            or receipt.lexical_rank_artifact is not None
        ):
            raise GoldCaptureError("native_capture_receipt_binding_mismatch")
        receipts[typed_lane] = receipt

    results_by_lane = {
        lane: {result.query_id: result for result in dataset.results}
        for lane, dataset in datasets.items()
    }
    with (
        _CanonicalJsonlReader(
            attestation_path,
            maximum_bytes=MAX_JSONL_BYTES,
            code="native_attestation",
        ) as attestation_reader,
        _open_native_score_artifact(
            score_artifact_path=score_artifact_path,
            score_corpus_inventory_path=score_corpus_inventory_path,
            score_matrix_path=score_matrix_path,
            score_query_vector_matrix_path=score_query_vector_matrix_path,
        ) as opened_scores,
    ):
        raw_attestation_manifest = attestation_reader.next_record()
        if raw_attestation_manifest is None:
            raise GoldCaptureError("native_validation_artifact_empty")
        try:
            attestation_manifest = _model_from_json_record(
                NativeV5AttestationManifest,
                raw_attestation_manifest,
            )
        except ValidationError as exc:
            raise GoldCaptureError("native_validation_manifest_invalid", line=1) from exc
        if (
            opened_scores.manifest != score_manifest
            or _score_binding(opened_scores.score_artifact_binding) != score_binding
            or _score_binding(opened_scores.corpus_inventory_binding) != score_inventory_binding
            or _score_binding(opened_scores.score_matrix_binding) != score_matrix_binding
            or _score_binding(opened_scores.query_vector_matrix_binding)
            != score_query_vector_binding
        ):
            raise GoldCaptureError("native_validation_score_artifact_changed")
        if (
            attestation_manifest.gold_sha256 != gold.sha256
            or attestation_manifest.capture_phase != "bootstrap_retrieval"
            or attestation_manifest.validation_profile
            != ("release_grade" if release_gate else "fixture_only")
            or attestation_manifest.answer_evidence is not None
            or attestation_manifest.query_count != len(gold.queries)
            or (
                candidate_source_commit is not None
                and (
                    attestation_manifest.source_commit != candidate_source_commit
                    or score_manifest.source_commit != candidate_source_commit
                )
            )
            or attestation_manifest.generation_id != generation.generation_id
            or attestation_manifest.generation_manifest != generation_binding
            or attestation_manifest.serving_database != database_binding
            or attestation_manifest.vector_sidecar != sidecar_binding
            or attestation_manifest.score_artifact != score_binding
            or attestation_manifest.score_corpus_inventory != score_inventory_binding
            or attestation_manifest.score_matrix != score_matrix_binding
            or attestation_manifest.score_query_vector_matrix != score_query_vector_binding
            or attestation_manifest.answer_artifact != answer_binding
            or score_manifest.gold_sha256 != gold.sha256
            or score_manifest.validation_profile
            != ("release_grade" if release_gate else "fixture_only")
            or score_manifest.generation_manifest_sha256 != generation_binding.sha256
            or score_manifest.serving_database_sha256 != database_binding.sha256
            or score_manifest.vector_sidecar_sha256 != sidecar_binding.sha256
        ):
            raise GoldCaptureError("native_validation_manifest_binding_mismatch")
        identity = _load_state_model(
            reranker_state_root / "identity.json",
            _NativeCaptureIdentity,
        )
        if identity != _NativeCaptureIdentity(
            schema_version=NATIVE_CAPTURE_IDENTITY_SCHEMA,
            attestation_manifest=attestation_manifest,
            query_ids=tuple(query.query_id for query in gold.queries),
        ):
            raise GoldCaptureError("native_validation_identity_mismatch")
        _validate_state_entries(reranker_state_root, len(gold.queries))
        total_rows = 0
        for query_index, query in enumerate(gold.queries):
            query_sha256 = hashlib.sha256(query.question.encode("utf-8")).hexdigest()
            summary = _native_score_summary(
                opened_scores,
                query_index=query_index,
                query_id=query.query_id,
                query_sha256=query_sha256,
                expected_provenance=native_provenance,
            )
            raw_attestation = attestation_reader.next_record()
            if raw_attestation is None:
                raise GoldCaptureError("native_query_attestation_missing")
            try:
                attestation = _model_from_json_record(
                    NativeV5QueryAttestation,
                    raw_attestation,
                )
            except ValidationError as exc:
                raise GoldCaptureError(
                    "native_query_attestation_invalid",
                    line=attestation_reader.line,
                ) from exc
            primary = results_by_lane["qwen_structure_exact"].get(query.query_id)
            lexical = results_by_lane["lexical_shadow"].get(query.query_id)
            reranker = results_by_lane["reranker_shadow"].get(query.query_id)
            if primary is None or lexical is None or reranker is None:
                raise GoldCaptureError("native_run_query_coverage_mismatch")
            if (
                attestation.query_id != query.query_id
                or attestation.query_sha256 != query_sha256
                or attestation.query_vector_sha256 != summary.coverage.query_vector_sha256
                or attestation.raw_score_query_binding_sha256 != summary.raw_query_binding_sha256
                or attestation.raw_expected_embedding_rows != summary.coverage.expected_rows
                or attestation.raw_scored_embedding_rows != summary.coverage.scored_rows
                or attestation.raw_active_contracts != summary.coverage.active_contracts
                or attestation.qwen_structure_exact_result_sha256 != canonical_sha256(primary)
                or attestation.lexical_shadow_result_sha256 != canonical_sha256(lexical)
                or attestation.reranker_shadow_result_sha256 != canonical_sha256(reranker)
                or lexical.contracts != primary.contracts
                or lexical.spans != primary.spans
                or lexical.answer != primary.answer
                or reranker.contracts != primary.contracts
                or reranker.spans != primary.spans
                or reranker.answer != primary.answer
            ):
                raise GoldCaptureError("native_query_attestation_binding_mismatch")
            shard = _load_state_model(
                reranker_state_root / f"query-{query_index:03d}.json",
                _NativeQueryShard,
            )
            if (
                shard.attestation != attestation
                or shard.qwen_structure_exact != primary
                or shard.lexical_shadow != lexical
                or shard.reranker_shadow != reranker
            ):
                raise GoldCaptureError("native_validation_query_shard_mismatch")
            _validate_native_query_shard(
                shard=shard,
                query_index=query_index,
                query=query,
                score_summary=summary,
                answer=answers[query.query_id],
                repository=repository,
                handle=handle,
                state=reranker_state_root,
            )
            total_rows += summary.coverage.scored_rows
        if attestation_reader.next_record() is not None:
            raise GoldCaptureError("native_validation_trailing_record")
        if (
            attestation_reader.binding != attestation_binding
            or total_rows != score_manifest.score_count
        ):
            raise GoldCaptureError("native_validation_artifact_binding_mismatch")
    _verify_regular_artifact_checkpoint(database_checkpoint)
    return receipts


def finalize_native_v5_with_answers(
    *,
    gold_path: Path,
    expected_gold_sha256: str,
    score_artifact_path: Path,
    score_corpus_inventory_path: Path,
    score_matrix_path: Path,
    score_query_vector_matrix_path: Path,
    generation_manifest_path: Path,
    generation_directory: Path,
    object_root: Path,
    bootstrap_attestation_path: Path,
    bootstrap_run_paths: Mapping[EvaluationLane, Path],
    bootstrap_receipt_paths: Mapping[EvaluationLane, Path],
    expected_bootstrap_receipt_sha256: Mapping[EvaluationLane, str],
    reranker_state_root: Path,
    answer_evidence_artifacts: AnswerEvidenceArtifacts,
    output_directory: Path,
    expected_source_commit: str,
    release_gate: bool = True,
) -> NativeV5CaptureResult:
    """Promote one fully validated bootstrap capture without any provider calls."""

    if not release_gate:
        raise GoldCaptureError("native_finalization_requires_release_gate")
    native_lanes = tuple(cast(EvaluationLane, lane) for lane in NATIVE_V5_LANES)
    if (
        set(bootstrap_run_paths) != set(native_lanes)
        or set(bootstrap_receipt_paths) != set(native_lanes)
        or set(expected_bootstrap_receipt_sha256) != set(native_lanes)
    ):
        raise GoldCaptureError("native_finalization_lane_set_invalid")
    for lane in native_lanes:
        _receipt, binding = _load_receipt(bootstrap_receipt_paths[lane])
        if binding.sha256 != expected_bootstrap_receipt_sha256[lane]:
            raise GoldCaptureError("native_bootstrap_receipt_sha256_mismatch")
    validated_receipts = validate_native_v5_capture(
        gold_path=gold_path,
        expected_gold_sha256=expected_gold_sha256,
        score_artifact_path=score_artifact_path,
        score_corpus_inventory_path=score_corpus_inventory_path,
        score_matrix_path=score_matrix_path,
        score_query_vector_matrix_path=score_query_vector_matrix_path,
        answer_artifact_path=None,
        generation_manifest_path=generation_manifest_path,
        generation_directory=generation_directory,
        object_root=object_root,
        attestation_path=bootstrap_attestation_path,
        run_paths=bootstrap_run_paths,
        receipt_paths=bootstrap_receipt_paths,
        reranker_state_root=reranker_state_root,
        expected_source_commit=expected_source_commit,
        release_gate=True,
    )
    gold = load_gold_jsonl(gold_path, release_gate=True)
    generation, generation_binding = _load_generation_manifest(generation_manifest_path)
    with _open_native_score_artifact(
        score_artifact_path=score_artifact_path,
        score_corpus_inventory_path=score_corpus_inventory_path,
        score_matrix_path=score_matrix_path,
        score_query_vector_matrix_path=score_query_vector_matrix_path,
    ) as opened_scores:
        score_binding = _score_binding(opened_scores.score_artifact_binding)
        score_inventory_binding = _score_binding(opened_scores.corpus_inventory_binding)
        score_matrix_binding = _score_binding(opened_scores.score_matrix_binding)
        score_query_vector_binding = _score_binding(opened_scores.query_vector_matrix_binding)
    datasets: dict[EvaluationLane, RunDataset] = {}
    for lane in native_lanes:
        before = _hash_regular(
            bootstrap_run_paths[lane],
            maximum_bytes=MAX_JSONL_BYTES,
            code="native_bootstrap_run",
        )
        dataset = load_run_jsonl(bootstrap_run_paths[lane], lane=lane)
        after = _hash_regular(
            bootstrap_run_paths[lane],
            maximum_bytes=MAX_JSONL_BYTES,
            code="native_bootstrap_run",
        )
        if before != after or after != validated_receipts[lane].run_artifact:
            raise GoldCaptureError("native_bootstrap_run_changed_after_validation")
        datasets[lane] = dataset
    with _CanonicalJsonlReader(
        bootstrap_attestation_path,
        maximum_bytes=MAX_JSONL_BYTES,
        code="native_bootstrap_attestation",
    ) as reader:
        raw_manifest = reader.next_record()
        if raw_manifest is None:
            raise GoldCaptureError("native_bootstrap_attestation_empty")
        try:
            bootstrap_manifest = _model_from_json_record(
                NativeV5AttestationManifest,
                raw_manifest,
            )
        except ValidationError as exc:
            raise GoldCaptureError("native_bootstrap_attestation_manifest_invalid") from exc
        bootstrap_queries: list[NativeV5QueryAttestation] = []
        for _query in gold.queries:
            raw = reader.next_record()
            if raw is None:
                raise GoldCaptureError("native_bootstrap_query_attestation_missing")
            try:
                bootstrap_queries.append(_model_from_json_record(NativeV5QueryAttestation, raw))
            except ValidationError as exc:
                raise GoldCaptureError("native_bootstrap_query_attestation_invalid") from exc
        if reader.next_record() is not None:
            raise GoldCaptureError("native_bootstrap_attestation_trailing_record")
        if reader.binding != validated_receipts["qwen_structure_exact"].attestation_artifact:
            raise GoldCaptureError("native_bootstrap_attestation_changed_after_validation")
    if (
        bootstrap_manifest.capture_phase != "bootstrap_retrieval"
        or bootstrap_manifest.validation_profile != "release_grade"
        or bootstrap_manifest.answer_evidence is not None
        or bootstrap_manifest.answer_artifact is not None
        or bootstrap_manifest.generation_manifest != generation_binding
        or bootstrap_manifest.score_artifact != score_binding
        or bootstrap_manifest.score_corpus_inventory != score_inventory_binding
        or bootstrap_manifest.score_matrix != score_matrix_binding
        or bootstrap_manifest.score_query_vector_matrix != score_query_vector_binding
        or any(
            receipt.attestation_artifact
            != validated_receipts["qwen_structure_exact"].attestation_artifact
            or receipt.raw_score_artifact_sha256 != score_binding.sha256
            for receipt in validated_receipts.values()
        )
    ):
        raise GoldCaptureError("native_bootstrap_attestation_phase_invalid")

    _answer_manifest, answers, answer_binding = _load_answers(
        answer_evidence_artifacts.answer_artifact_path,
        gold=gold,
        lane="qwen_structure_exact",
        generation_id=generation.generation_id,
        generation_manifest_sha256=generation_binding.sha256,
    )
    final_results: dict[EvaluationLane, tuple[QueryRunResult, ...]] = {}
    for lane in native_lanes:
        final_results[lane] = tuple(
            result.model_copy(update={"answer": answers[result.query_id]})
            for result in datasets[lane].results
        )
    primary_results = final_results["qwen_structure_exact"]
    for lane in ("lexical_shadow", "reranker_shadow"):
        if any(
            shadow.contracts != primary.contracts
            or shadow.spans != primary.spans
            or shadow.answer != primary.answer
            for primary, shadow in zip(primary_results, final_results[lane], strict=True)
        ):
            raise GoldCaptureError("native_finalization_shadow_changed_primary")
    answer_evidence = _verify_answer_evidence(
        artifacts=answer_evidence_artifacts,
        authoritative_results=primary_results,
        expected_lane="qwen_structure_exact",
        gold=gold,
        gold_path=gold_path,
        expected_gold_sha256=gold.sha256,
        expected_source_commit=expected_source_commit,
        expected_generation_id=generation.generation_id,
        expected_generation_manifest_sha256=generation_binding.sha256,
        expected_serving_database_sha256=bootstrap_manifest.serving_database.sha256,
        release_gate=True,
    )
    if answer_evidence is None or answer_evidence.answer_artifact != answer_binding:
        raise GoldCaptureError("native_finalization_answer_binding_mismatch")
    final_manifest = bootstrap_manifest.model_copy(
        update={
            "capture_phase": "final_release",
            "answer_artifact": answer_binding,
            "answer_evidence": answer_evidence,
        }
    )
    final_queries = tuple(
        bootstrap.model_copy(
            update={
                "qwen_structure_exact_result_sha256": canonical_sha256(primary),
                "lexical_shadow_result_sha256": canonical_sha256(lexical),
                "reranker_shadow_result_sha256": canonical_sha256(reranker),
            }
        )
        for bootstrap, primary, lexical, reranker in zip(
            bootstrap_queries,
            final_results["qwen_structure_exact"],
            final_results["lexical_shadow"],
            final_results["reranker_shadow"],
            strict=True,
        )
    )
    output = _prepare_output_directory(output_directory)
    identity = _NativeCaptureIdentity(
        schema_version=NATIVE_CAPTURE_IDENTITY_SCHEMA,
        attestation_manifest=final_manifest,
        query_ids=tuple(query.query_id for query in gold.queries),
    )
    _publish_immutable(output / "native-v5-final-identity.json", identity.canonical_bytes())
    attestation_path = output / "native-v5-attestation.jsonl"
    attestation_binding = _publish_immutable(
        attestation_path,
        _jsonl_bytes((final_manifest, *final_queries)),
    )
    run_manifests = _native_run_manifests(final_manifest)
    run_paths: dict[EvaluationLane, Path] = {}
    receipt_paths: dict[EvaluationLane, Path] = {}
    for lane in native_lanes:
        run_path = output / f"{lane}.jsonl"
        run_binding = _publish_immutable(
            run_path,
            _jsonl_bytes((run_manifests[lane], *final_results[lane])),
        )
        receipt = LaneCaptureReceipt(
            schema_version=CAPTURE_RECEIPT_SCHEMA,
            lane=lane,
            capture_mode="native_v5",
            capture_phase="final_release",
            validation_profile="release_grade",
            release_eligible=True,
            gold_sha256=gold.sha256,
            query_count=len(gold.queries),
            run_artifact=run_binding,
            attestation_artifact=attestation_binding,
            source_generation_id=generation.generation_id,
            source_generation_manifest_sha256=generation_binding.sha256,
            source_database_sha256=final_manifest.serving_database.sha256,
            source_vector_sha256=final_manifest.vector_sidecar.sha256,
            raw_score_artifact_sha256=score_binding.sha256,
            corpus_inventory=score_inventory_binding,
            dense_score_matrix=score_matrix_binding,
            query_vector_matrix=score_query_vector_binding,
            answer_evidence=answer_evidence,
        )
        receipt_path = output / f"{lane}.capture-receipt.json"
        _publish_immutable(receipt_path, receipt.canonical_bytes())
        load_run_jsonl(run_path, lane=lane)
        run_paths[lane] = run_path
        receipt_paths[lane] = receipt_path
    return NativeV5CaptureResult(
        run_paths=run_paths,
        receipt_paths=receipt_paths,
        attestation_path=attestation_path,
        resumed_queries=len(gold.queries),
    )


def _validate_external_release_attestation(
    *,
    path: Path,
    inventory_path: Path,
    dense_score_matrix_path: Path,
    query_vector_matrix_path: Path,
    lexical_rank_path: Path | None,
    receipt: LaneCaptureReceipt,
    dataset_results: Mapping[str, QueryRunResult],
    run_manifest: RunArtifactManifest,
    gold: GoldDataset,
) -> None:
    if receipt.lane not in {"v109_baseline", "qwen_page"}:
        raise GoldCaptureError("external_release_attestation_lane_invalid")
    inventory_binding = _hash_regular(
        inventory_path,
        maximum_bytes=_MAX_EXTERNAL_ARTIFACT_BYTES,
        code="external_release_corpus_inventory",
    )
    with _CanonicalJsonlReader(
        path,
        maximum_bytes=_MAX_EXTERNAL_ARTIFACT_BYTES,
        code="external_release_attestation",
    ) as reader:
        raw_manifest = reader.next_record()
        if raw_manifest is None:
            raise GoldCaptureError("external_release_attestation_empty")
        try:
            manifest = _model_from_json_record(ExternalObservationManifest, raw_manifest)
        except ValidationError as exc:
            raise GoldCaptureError("external_release_attestation_manifest_invalid", line=1) from exc
        if (
            receipt.capture_mode != "external_reproducible"
            or manifest.lane != receipt.lane
            or manifest.gold_sha256 != gold.sha256
            or manifest.query_count != len(gold.queries)
            or manifest.generation_id != receipt.source_generation_id
            or manifest.generation_manifest.sha256 != receipt.source_generation_manifest_sha256
            or manifest.serving_database.sha256 != receipt.source_database_sha256
            or (None if manifest.vector_artifact is None else manifest.vector_artifact.sha256)
            != receipt.source_vector_sha256
            or receipt.raw_score_artifact_sha256 != receipt.attestation_artifact.sha256
            or receipt.corpus_inventory != inventory_binding
            or manifest.corpus_inventory_sha256 != inventory_binding.sha256
            or receipt.dense_score_matrix != manifest.dense_score_matrix
            or receipt.query_vector_matrix != manifest.query_vector_matrix
            or receipt.lexical_rank_artifact != manifest.lexical_rank_artifact
            or run_manifest.source_commit != manifest.source_commit
            or run_manifest.source_version != manifest.source_version
            or run_manifest.serving_schema != manifest.serving_schema
            or run_manifest.embedding_model != manifest.embedding_model
            or run_manifest.embedding_dimension != manifest.embedding_dimension
            or run_manifest.retrieval_policy != manifest.retrieval_policy
        ):
            raise GoldCaptureError("external_release_attestation_binding_mismatch", line=1)
        if (manifest.lexical_rank_artifact is None) != (lexical_rank_path is None):
            raise GoldCaptureError("external_release_lexical_path_mismatch")
        with ExitStack() as sidecars:
            dense_scores = sidecars.enter_context(
                _verified_sidecar(
                    dense_score_matrix_path,
                    expected=manifest.dense_score_matrix,
                    code="external_release_dense_score_matrix",
                )
            )
            query_vectors = sidecars.enter_context(
                _verified_sidecar(
                    query_vector_matrix_path,
                    expected=manifest.query_vector_matrix,
                    code="external_release_query_vector_matrix",
                )
            )
            lexical_mapping = (
                None
                if lexical_rank_path is None or manifest.lexical_rank_artifact is None
                else sidecars.enter_context(
                    _verified_sidecar(
                        lexical_rank_path,
                        expected=manifest.lexical_rank_artifact,
                        code="external_release_lexical_rank_artifact",
                    )
                )
            )
            dense_end = 0
            vector_end = 0
            lexical_end = 0
            for ordinal, query in enumerate(gold.queries):
                raw = reader.next_record()
                if raw is None:
                    raise GoldCaptureError("external_release_attestation_query_missing")
                try:
                    observation = _model_from_json_record(ExternalQueryObservation, raw)
                except ValidationError as exc:
                    raise GoldCaptureError(
                        "external_release_attestation_query_invalid",
                        line=reader.line,
                    ) from exc
                result = dataset_results.get(query.query_id)
                if (
                    result is None
                    or observation.ordinal != ordinal
                    or observation.lane != receipt.lane
                    or observation.query_id != query.query_id
                    or observation.query_sha256
                    != hashlib.sha256(query.question.encode("utf-8")).hexdigest()
                    or observation.result != result
                    or observation.dense_count != manifest.row_count
                    or observation.vector_count != manifest.embedding_dimension
                    or observation.dense_offset_bytes != dense_end
                    or observation.vector_offset_bytes != vector_end
                    or (lexical_mapping is None and observation.lexical_offset_bytes is not None)
                    or (
                        lexical_mapping is not None
                        and observation.lexical_offset_bytes != lexical_end
                    )
                ):
                    raise GoldCaptureError(
                        "external_release_attestation_query_binding_mismatch",
                        line=reader.line,
                    )
                dense_payload = _sidecar_segment(
                    dense_scores,
                    offset=observation.dense_offset_bytes,
                    size=observation.dense_size_bytes,
                    expected_sha256=observation.dense_sha256,
                    code="external_release_dense_segment",
                )
                if not bool(np.isfinite(np.frombuffer(dense_payload, dtype="<f4")).all()):
                    raise GoldCaptureError("external_release_dense_score_non_finite")
                vector_payload = _sidecar_segment(
                    query_vectors,
                    offset=observation.vector_offset_bytes,
                    size=observation.vector_size_bytes,
                    expected_sha256=observation.vector_sha256,
                    code="external_release_vector_segment",
                )
                _decode_query_vector(
                    vector_payload,
                    expected_sha256=observation.vector_sha256,
                    dimension=manifest.embedding_dimension,
                )
                if lexical_mapping is not None:
                    if (
                        observation.lexical_offset_bytes is None
                        or observation.lexical_size_bytes is None
                        or observation.lexical_sha256 is None
                    ):
                        raise GoldCaptureError("external_release_lexical_segment_missing")
                    lexical_payload = _sidecar_segment(
                        lexical_mapping,
                        offset=observation.lexical_offset_bytes,
                        size=observation.lexical_size_bytes,
                        expected_sha256=observation.lexical_sha256,
                        code="external_release_lexical_segment",
                    )
                    _lexical_ranks_from_segment(
                        lexical_payload,
                        observation=observation,
                        inventory_count=manifest.row_count,
                    )
                    lexical_end += observation.lexical_size_bytes
                dense_end += observation.dense_size_bytes
                vector_end += observation.vector_size_bytes
            if (
                dense_end != len(dense_scores)
                or vector_end != len(query_vectors)
                or (lexical_mapping is not None and lexical_end != len(lexical_mapping))
            ):
                raise GoldCaptureError("external_release_sidecar_trailing_or_missing_bytes")
        if reader.next_record() is not None:
            raise GoldCaptureError("external_release_attestation_trailing_record")
        binding = reader.binding
    if binding != receipt.attestation_artifact:
        raise GoldCaptureError("external_release_attestation_sha256_mismatch")


def _validate_native_release_attestation(
    *,
    path: Path,
    score_artifact_path: Path,
    score_corpus_inventory_path: Path,
    score_matrix_path: Path,
    score_query_vector_matrix_path: Path,
    receipts: Mapping[NativeV5Lane, LaneCaptureReceipt],
    datasets: Mapping[NativeV5Lane, Mapping[str, QueryRunResult]],
    run_manifests: Mapping[NativeV5Lane, RunArtifactManifest],
    gold: GoldDataset,
) -> None:
    exact_receipt = receipts["qwen_structure_exact"]
    with (
        _CanonicalJsonlReader(
            path,
            maximum_bytes=MAX_JSONL_BYTES,
            code="native_release_attestation",
        ) as reader,
        _open_native_score_artifact(
            score_artifact_path=score_artifact_path,
            score_corpus_inventory_path=score_corpus_inventory_path,
            score_matrix_path=score_matrix_path,
            score_query_vector_matrix_path=score_query_vector_matrix_path,
        ) as opened_scores,
    ):
        raw_manifest = reader.next_record()
        if raw_manifest is None:
            raise GoldCaptureError("native_release_attestation_empty")
        try:
            manifest = _model_from_json_record(NativeV5AttestationManifest, raw_manifest)
        except ValidationError as exc:
            raise GoldCaptureError("native_release_attestation_manifest_invalid", line=1) from exc
        score_manifest = opened_scores.manifest
        score_binding = _score_binding(opened_scores.score_artifact_binding)
        inventory_binding = _score_binding(opened_scores.corpus_inventory_binding)
        score_matrix_binding = _score_binding(opened_scores.score_matrix_binding)
        query_vector_binding = _score_binding(opened_scores.query_vector_matrix_binding)
        for lane, receipt in receipts.items():
            run_manifest = run_manifests[lane]
            if (
                receipt.capture_mode != "native_v5"
                or receipt.capture_phase != exact_receipt.capture_phase
                or receipt.validation_profile != exact_receipt.validation_profile
                or receipt.release_eligible != exact_receipt.release_eligible
                or receipt.answer_evidence != exact_receipt.answer_evidence
                or receipt.attestation_artifact != exact_receipt.attestation_artifact
                or receipt.raw_score_artifact_sha256 != exact_receipt.raw_score_artifact_sha256
                or receipt.corpus_inventory != exact_receipt.corpus_inventory
                or receipt.dense_score_matrix != exact_receipt.dense_score_matrix
                or receipt.query_vector_matrix != exact_receipt.query_vector_matrix
                or receipt.source_generation_id != exact_receipt.source_generation_id
                or receipt.source_generation_manifest_sha256
                != exact_receipt.source_generation_manifest_sha256
                or receipt.source_database_sha256 != exact_receipt.source_database_sha256
                or receipt.source_vector_sha256 != exact_receipt.source_vector_sha256
                or receipt.lane != lane
                or run_manifest.source_commit != manifest.source_commit
                or run_manifest.source_version != "v1.0.10-candidate"
                or run_manifest.generation_id != manifest.generation_id
                or run_manifest.generation_manifest_sha256 != manifest.generation_manifest.sha256
                or run_manifest.serving_schema != "cardrag.serving-db.v5"
                or run_manifest.embedding_model != manifest.embedding_model
                or run_manifest.embedding_dimension != manifest.embedding_dimension
            ):
                raise GoldCaptureError("native_release_receipt_binding_mismatch")
        if (
            manifest.gold_sha256 != gold.sha256
            or manifest.capture_phase != exact_receipt.capture_phase
            or manifest.validation_profile != exact_receipt.validation_profile
            or manifest.answer_evidence != exact_receipt.answer_evidence
            or (
                None
                if manifest.answer_evidence is None
                else manifest.answer_evidence.answer_artifact
            )
            != manifest.answer_artifact
            or manifest.query_count != len(gold.queries)
            or manifest.generation_id != exact_receipt.source_generation_id
            or manifest.generation_manifest.sha256
            != exact_receipt.source_generation_manifest_sha256
            or manifest.serving_database.sha256 != exact_receipt.source_database_sha256
            or manifest.vector_sidecar.sha256 != exact_receipt.source_vector_sha256
            or manifest.score_artifact.sha256 != exact_receipt.raw_score_artifact_sha256
            or manifest.score_artifact != score_binding
            or manifest.score_corpus_inventory != inventory_binding
            or manifest.score_matrix != score_matrix_binding
            or manifest.score_query_vector_matrix != query_vector_binding
            or exact_receipt.corpus_inventory != inventory_binding
            or exact_receipt.dense_score_matrix != score_matrix_binding
            or exact_receipt.query_vector_matrix != query_vector_binding
            or score_manifest.gold_sha256 != gold.sha256
            or score_manifest.validation_profile != exact_receipt.validation_profile
            or score_manifest.query_count != len(gold.queries)
            or score_manifest.source_commit != manifest.source_commit
            or score_manifest.generation_id != manifest.generation_id
            or score_manifest.generation_manifest_sha256 != manifest.generation_manifest.sha256
            or score_manifest.serving_database_sha256 != manifest.serving_database.sha256
            or score_manifest.vector_sidecar_sha256 != manifest.vector_sidecar.sha256
            or score_manifest.exact_row_corpus_sha256 != manifest.exact_row_corpus_sha256
            or score_manifest.embedding_profile_id != manifest.embedding_profile_id
        ):
            raise GoldCaptureError("native_release_attestation_binding_mismatch", line=1)
        result_hash_field: Mapping[NativeV5Lane, str] = {
            "qwen_structure_exact": "qwen_structure_exact_result_sha256",
            "lexical_shadow": "lexical_shadow_result_sha256",
            "reranker_shadow": "reranker_shadow_result_sha256",
        }
        total_raw_rows = 0
        for query_index, query in enumerate(gold.queries):
            query_sha256 = hashlib.sha256(query.question.encode("utf-8")).hexdigest()
            summary = _native_score_summary(
                opened_scores,
                query_index=query_index,
                query_id=query.query_id,
                query_sha256=query_sha256,
                expected_provenance=None,
            )
            raw = reader.next_record()
            if raw is None:
                raise GoldCaptureError("native_release_attestation_query_missing")
            try:
                attestation = _model_from_json_record(NativeV5QueryAttestation, raw)
            except ValidationError as exc:
                raise GoldCaptureError(
                    "native_release_attestation_query_invalid",
                    line=reader.line,
                ) from exc
            if (
                attestation.query_id != query.query_id
                or attestation.query_sha256 != query_sha256
                or attestation.query_vector_sha256 != summary.coverage.query_vector_sha256
                or attestation.raw_score_query_binding_sha256 != summary.raw_query_binding_sha256
                or attestation.raw_expected_embedding_rows != summary.coverage.expected_rows
                or attestation.raw_scored_embedding_rows != summary.coverage.scored_rows
                or attestation.raw_active_contracts != summary.coverage.active_contracts
            ):
                raise GoldCaptureError(
                    "native_release_attestation_query_binding_mismatch",
                    line=reader.line,
                )
            for lane, result_field in result_hash_field.items():
                result = datasets[lane].get(query.query_id)
                if result is None or getattr(attestation, result_field) != canonical_sha256(result):
                    raise GoldCaptureError(
                        "native_release_attestation_result_binding_mismatch",
                        line=reader.line,
                    )
            total_raw_rows += summary.coverage.scored_rows
        if reader.next_record() is not None:
            raise GoldCaptureError("native_release_attestation_trailing_record")
        binding = reader.binding
    if binding != exact_receipt.attestation_artifact:
        raise GoldCaptureError("native_release_attestation_sha256_mismatch")
    if score_binding != manifest.score_artifact or total_raw_rows != score_manifest.score_count:
        raise GoldCaptureError("native_release_score_artifact_binding_mismatch")


def validate_capture_set(
    *,
    gold_path: Path,
    expected_gold_sha256: str,
    run_paths: Mapping[EvaluationLane, Path],
    receipt_paths: Mapping[EvaluationLane, Path],
    attestation_paths: Mapping[EvaluationLane, Path],
    native_score_artifact_path: Path,
    native_score_corpus_inventory_path: Path,
    native_score_matrix_path: Path,
    native_score_query_vector_matrix_path: Path,
    external_inventory_paths: Mapping[EvaluationLane, Path] | None = None,
    expected_receipt_sha256: Mapping[EvaluationLane, str],
    external_dense_score_matrix_paths: Mapping[EvaluationLane, Path] | None = None,
    external_query_vector_matrix_paths: Mapping[EvaluationLane, Path] | None = None,
    external_lexical_rank_paths: Mapping[EvaluationLane, Path] | None = None,
    answer_evidence_artifacts: Mapping[AnswerLane, AnswerEvidenceArtifacts] | None = None,
    output_path: Path | None = None,
    expected_source_commit: str | None = None,
    release_gate: bool = True,
) -> CaptureSetReceipt:
    """Bind five already-revalidated lanes; this does not replay their source artifacts."""

    if (
        set(run_paths) != set(LANES)
        or set(receipt_paths) != set(LANES)
        or set(attestation_paths) != set(LANES)
        or set(expected_receipt_sha256) != set(LANES)
    ):
        raise GoldCaptureError("capture_set_required_lane_missing")
    external_lanes = {"v109_baseline", "qwen_page"}
    if (
        external_inventory_paths is None
        or set(external_inventory_paths) != external_lanes
        or external_dense_score_matrix_paths is None
        or set(external_dense_score_matrix_paths) != external_lanes
        or external_query_vector_matrix_paths is None
        or set(external_query_vector_matrix_paths) != external_lanes
        or external_lexical_rank_paths is None
        or set(external_lexical_rank_paths) != {"v109_baseline"}
    ):
        raise GoldCaptureError("capture_set_external_sidecar_paths_required")
    if release_gate:
        if answer_evidence_artifacts is None or set(answer_evidence_artifacts) != {
            "v109_baseline",
            "qwen_page",
            "qwen_structure_exact",
        }:
            raise GoldCaptureError("capture_set_answer_evidence_required")
    elif answer_evidence_artifacts is not None:
        raise GoldCaptureError("capture_set_answer_evidence_unexpected")
    gold = load_gold_jsonl(gold_path, release_gate=release_gate)
    if gold.sha256 != expected_gold_sha256:
        raise GoldCaptureError("gold_sha256_mismatch")
    candidate_source_commit = _validated_expected_source_commit(
        expected_source_commit,
        release_gate=release_gate,
    )
    receipts: list[LaneCaptureReceipt] = []
    datasets_by_lane: dict[EvaluationLane, Mapping[str, QueryRunResult]] = {}
    manifests_by_lane: dict[EvaluationLane, RunArtifactManifest] = {}
    for lane_name in LANES:
        lane = cast(EvaluationLane, lane_name)
        receipt, receipt_binding = _load_receipt(receipt_paths[lane])
        dataset = load_run_jsonl(run_paths[lane], lane=lane)
        dataset_results = {result.query_id: result for result in dataset.results}
        run_binding = _hash_regular(
            run_paths[lane],
            maximum_bytes=(
                _MAX_EXTERNAL_ARTIFACT_BYTES
                if lane in {"v109_baseline", "qwen_page"}
                else MAX_JSONL_BYTES
            ),
            code="run_artifact",
        )
        if (
            lane != "v109_baseline"
            and candidate_source_commit is not None
            and dataset.manifest.source_commit != candidate_source_commit
        ):
            raise GoldCaptureError("candidate_source_commit_mismatch")
        if (
            receipt_binding.sha256 != expected_receipt_sha256[lane]
            or receipt.lane != lane
            or (release_gate and not receipt.release_eligible)
            or (release_gate and receipt.capture_phase != "final_release")
            or (not release_gate and receipt.capture_phase != "bootstrap_retrieval")
            or receipt.validation_profile != ("release_grade" if release_gate else "fixture_only")
            or receipt.gold_sha256 != gold.sha256
            or receipt.query_count != len(gold.queries)
            or receipt.run_artifact != run_binding
            or dataset.manifest.gold_sha256 != gold.sha256
            or dataset.manifest.generation_id != receipt.source_generation_id
            or dataset.manifest.generation_manifest_sha256
            != receipt.source_generation_manifest_sha256
            or tuple(result.query_id for result in dataset.results)
            != tuple(query.query_id for query in gold.queries)
        ):
            raise GoldCaptureError("capture_set_lane_binding_mismatch")
        receipts.append(receipt)
        datasets_by_lane[lane] = dataset_results
        manifests_by_lane[lane] = dataset.manifest

    if len({receipt.release_eligible for receipt in receipts}) != 1:
        raise GoldCaptureError("capture_set_release_eligibility_mismatch")

    receipt_by_lane = {receipt.lane: receipt for receipt in receipts}
    for lane in ("v109_baseline", "qwen_page"):
        _validate_external_release_attestation(
            path=attestation_paths[lane],
            inventory_path=external_inventory_paths[lane],
            dense_score_matrix_path=external_dense_score_matrix_paths[lane],
            query_vector_matrix_path=external_query_vector_matrix_paths[lane],
            lexical_rank_path=external_lexical_rank_paths.get(lane),
            receipt=receipt_by_lane[lane],
            dataset_results=datasets_by_lane[lane],
            run_manifest=manifests_by_lane[lane],
            gold=gold,
        )
    native_attestation_paths = {attestation_paths[lane] for lane in NATIVE_V5_LANES}
    if len(native_attestation_paths) != 1:
        raise GoldCaptureError("native_release_attestation_path_mismatch")
    _validate_native_release_attestation(
        path=next(iter(native_attestation_paths)),
        score_artifact_path=native_score_artifact_path,
        score_corpus_inventory_path=native_score_corpus_inventory_path,
        score_matrix_path=native_score_matrix_path,
        score_query_vector_matrix_path=native_score_query_vector_matrix_path,
        receipts={lane: receipt_by_lane[lane] for lane in NATIVE_V5_LANES},
        datasets={lane: datasets_by_lane[lane] for lane in NATIVE_V5_LANES},
        run_manifests={lane: manifests_by_lane[lane] for lane in NATIVE_V5_LANES},
        gold=gold,
    )
    if answer_evidence_artifacts is not None:
        for answer_lane in ("v109_baseline", "qwen_page", "qwen_structure_exact"):
            run_manifest = manifests_by_lane[answer_lane]
            authoritative = tuple(
                datasets_by_lane[answer_lane][query.query_id] for query in gold.queries
            )
            verified = _verify_answer_evidence(
                artifacts=answer_evidence_artifacts[answer_lane],
                authoritative_results=authoritative,
                expected_lane=answer_lane,
                gold=gold,
                gold_path=gold_path,
                expected_gold_sha256=gold.sha256,
                expected_source_commit=run_manifest.source_commit,
                expected_generation_id=run_manifest.generation_id,
                expected_generation_manifest_sha256=(run_manifest.generation_manifest_sha256),
                expected_serving_database_sha256=(
                    receipt_by_lane[answer_lane].source_database_sha256
                ),
                release_gate=True,
                source_replay=False,
            )
            if verified is None or receipt_by_lane[answer_lane].answer_evidence != verified:
                raise GoldCaptureError("capture_set_answer_evidence_binding_mismatch")
        exact_answer_evidence = receipt_by_lane["qwen_structure_exact"].answer_evidence
        if any(
            receipt_by_lane[lane].answer_evidence != exact_answer_evidence for lane in SHADOW_LANES
        ):
            raise GoldCaptureError("capture_set_shadow_answer_evidence_mismatch")
    exact = load_run_jsonl(run_paths["qwen_structure_exact"], lane="qwen_structure_exact")
    exact_by_id = {result.query_id: result for result in exact.results}
    for lane_name in SHADOW_LANES:
        shadow = load_run_jsonl(run_paths[lane_name], lane=lane_name)
        if (
            shadow.manifest.generation_id != exact.manifest.generation_id
            or shadow.manifest.generation_manifest_sha256
            != exact.manifest.generation_manifest_sha256
            or shadow.manifest.source_commit != exact.manifest.source_commit
        ):
            raise GoldCaptureError("capture_set_shadow_generation_mismatch")
        for result in shadow.results:
            primary = exact_by_id.get(result.query_id)
            if primary is None or (
                result.contracts != primary.contracts
                or result.spans != primary.spans
                or result.answer != primary.answer
            ):
                raise GoldCaptureError("capture_set_shadow_changed_primary")
    set_receipt = CaptureSetReceipt(
        schema_version=CAPTURE_SET_RECEIPT_SCHEMA,
        gold_sha256=gold.sha256,
        query_count=len(gold.queries),
        release_eligible=receipts[0].release_eligible,
        lanes=tuple(receipts),
    )
    if output_path is not None:
        _publish_immutable(output_path, set_receipt.canonical_bytes())
    return set_receipt


def _lane_paths(values: Sequence[str], *, label: str) -> dict[EvaluationLane, Path]:
    result: dict[EvaluationLane, Path] = {}
    for value in values:
        if "=" not in value:
            raise GoldCaptureError(f"{label}_argument_invalid")
        lane_name, raw_path = value.split("=", 1)
        if lane_name not in LANES or not raw_path:
            raise GoldCaptureError(f"{label}_argument_invalid")
        lane = cast(EvaluationLane, lane_name)
        if lane in result:
            raise GoldCaptureError(f"{label}_argument_duplicate")
        result[lane] = Path(raw_path)
    return result


def _lane_hashes(values: Sequence[str], *, label: str) -> dict[EvaluationLane, str]:
    result: dict[EvaluationLane, str] = {}
    for value in values:
        if "=" not in value:
            raise GoldCaptureError(f"{label}_argument_invalid")
        lane_name, digest = value.split("=", 1)
        if (
            lane_name not in LANES
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise GoldCaptureError(f"{label}_argument_invalid")
        lane = cast(EvaluationLane, lane_name)
        if lane in result:
            raise GoldCaptureError(f"{label}_argument_duplicate")
        result[lane] = digest
    return result


def _answer_lane_paths(values: Sequence[str], *, label: str) -> dict[AnswerLane, Path]:
    parsed = _lane_paths(values, label=label)
    allowed = {"v109_baseline", "qwen_page", "qwen_structure_exact"}
    if not set(parsed).issubset(allowed):
        raise GoldCaptureError(f"{label}_argument_invalid")
    return cast(dict[AnswerLane, Path], parsed)


def _answer_lane_hashes(values: Sequence[str], *, label: str) -> dict[AnswerLane, str]:
    parsed = _lane_hashes(values, label=label)
    allowed = {"v109_baseline", "qwen_page", "qwen_structure_exact"}
    if not set(parsed).issubset(allowed):
        raise GoldCaptureError(f"{label}_argument_invalid")
    return cast(dict[AnswerLane, str], parsed)


def _answer_lane_values(values: Sequence[str], *, label: str) -> dict[AnswerLane, str]:
    result: dict[AnswerLane, str] = {}
    allowed = {"v109_baseline", "qwen_page", "qwen_structure_exact"}
    for value in values:
        if "=" not in value:
            raise GoldCaptureError(f"{label}_argument_invalid")
        lane_name, item = value.split("=", 1)
        if lane_name not in allowed or not item or lane_name in result:
            raise GoldCaptureError(f"{label}_argument_invalid")
        result[cast(AnswerLane, lane_name)] = item
    return result


def _single_answer_evidence(
    arguments: argparse.Namespace,
    *,
    generation_manifest_path: Path,
    database_path: Path,
) -> AnswerEvidenceArtifacts | None:
    input_path = cast(Path | None, getattr(arguments, "answer_input", None))
    names = (
        "expected_answer_input_sha256",
        "answer_producer_receipt",
        "expected_answer_producer_receipt_sha256",
        "answer_artifact",
        "expected_answer_artifact_sha256",
        "answer_call_ledger",
        "answer_state_identity",
        "answer_profile_id",
        "answer_retrieval_run",
        "expected_answer_retrieval_run_sha256",
        "answer_retrieval_capture_receipt",
        "expected_answer_retrieval_capture_receipt_sha256",
        "answer_retrieval_attestation",
        "expected_answer_retrieval_attestation_sha256",
        "answer_retrieval_raw_score",
        "expected_answer_retrieval_raw_score_sha256",
        "answer_state_bundle",
    )
    values = tuple(getattr(arguments, name, None) for name in names)
    sidecar_names = (
        "answer_retrieval_corpus_inventory",
        "expected_answer_retrieval_corpus_inventory_sha256",
        "answer_retrieval_dense_score_matrix",
        "expected_answer_retrieval_dense_score_matrix_sha256",
        "answer_retrieval_query_vector_matrix",
        "expected_answer_retrieval_query_vector_matrix_sha256",
        "answer_retrieval_lexical_ranks",
        "expected_answer_retrieval_lexical_ranks_sha256",
    )
    sidecar_values = tuple(getattr(arguments, name, None) for name in sidecar_names)
    if input_path is None:
        if any(value is not None for value in (*values, *sidecar_values)) or any(
            getattr(arguments, name, None) is not None
            for name in ("answer_decision", "expected_answer_decision_sha256")
        ):
            raise GoldCaptureError("answer_evidence_arguments_incomplete")
        return None
    if any(value is None for value in values):
        raise GoldCaptureError("answer_evidence_arguments_incomplete")
    if any(
        (sidecar_values[index] is None) != (sidecar_values[index + 1] is None)
        for index in (0, 2, 4, 6)
    ):
        raise GoldCaptureError("answer_retrieval_sidecar_arguments_incomplete")
    if any(value is None for value in sidecar_values[:6]):
        raise GoldCaptureError("answer_retrieval_sidecar_arguments_incomplete")
    decision_path = cast(Path | None, getattr(arguments, "answer_decision", None))
    decision_sha256 = cast(
        str | None,
        getattr(arguments, "expected_answer_decision_sha256", None),
    )
    if (decision_path is None) != (decision_sha256 is None):
        raise GoldCaptureError("answer_decision_arguments_incomplete")
    return AnswerEvidenceArtifacts(
        generation_manifest_path=generation_manifest_path,
        database_path=database_path,
        input_path=input_path,
        expected_input_sha256=str(values[0]),
        producer_receipt_path=cast(Path, values[1]),
        expected_producer_receipt_sha256=str(values[2]),
        answer_artifact_path=cast(Path, values[3]),
        expected_answer_artifact_sha256=str(values[4]),
        call_ledger_path=cast(Path, values[5]),
        state_identity_path=cast(Path, values[6]),
        state_bundle_path=cast(Path, values[16]),
        answer_profile_id=str(values[7]),
        retrieval_run_path=cast(Path, values[8]),
        expected_retrieval_run_sha256=str(values[9]),
        retrieval_capture_receipt_path=cast(Path, values[10]),
        expected_retrieval_capture_receipt_sha256=str(values[11]),
        retrieval_attestation_path=cast(Path, values[12]),
        expected_retrieval_attestation_sha256=str(values[13]),
        retrieval_raw_score_path=cast(Path, values[14]),
        expected_retrieval_raw_score_sha256=str(values[15]),
        retrieval_corpus_inventory_path=cast(Path, sidecar_values[0]),
        expected_retrieval_corpus_inventory_sha256=str(sidecar_values[1]),
        retrieval_dense_score_matrix_path=cast(Path, sidecar_values[2]),
        expected_retrieval_dense_score_matrix_sha256=str(sidecar_values[3]),
        retrieval_query_vector_matrix_path=cast(Path, sidecar_values[4]),
        expected_retrieval_query_vector_matrix_sha256=str(sidecar_values[5]),
        retrieval_lexical_rank_path=cast(Path | None, sidecar_values[6]),
        expected_retrieval_lexical_rank_sha256=cast(str | None, sidecar_values[7]),
        decision_path=decision_path,
        expected_decision_sha256=decision_sha256,
    )


def _add_answer_evidence_arguments(
    parser: argparse.ArgumentParser,
    *,
    include_answer_artifact: bool = True,
) -> None:
    parser.add_argument("--answer-input", type=Path)
    parser.add_argument("--expected-answer-input-sha256")
    parser.add_argument("--answer-producer-receipt", type=Path)
    parser.add_argument("--expected-answer-producer-receipt-sha256")
    if include_answer_artifact:
        parser.add_argument("--answer-artifact", type=Path)
        parser.add_argument("--expected-answer-artifact-sha256")
    parser.add_argument("--answer-call-ledger", type=Path)
    parser.add_argument("--answer-state-identity", type=Path)
    parser.add_argument("--answer-state-bundle", type=Path)
    parser.add_argument("--answer-profile-id")
    parser.add_argument("--answer-retrieval-run", type=Path)
    parser.add_argument("--expected-answer-retrieval-run-sha256")
    parser.add_argument("--answer-retrieval-capture-receipt", type=Path)
    parser.add_argument("--expected-answer-retrieval-capture-receipt-sha256")
    parser.add_argument("--answer-retrieval-attestation", type=Path)
    parser.add_argument("--expected-answer-retrieval-attestation-sha256")
    parser.add_argument("--answer-retrieval-raw-score", type=Path)
    parser.add_argument("--expected-answer-retrieval-raw-score-sha256")
    parser.add_argument("--answer-retrieval-corpus-inventory", type=Path)
    parser.add_argument("--expected-answer-retrieval-corpus-inventory-sha256")
    parser.add_argument("--answer-retrieval-dense-score-matrix", type=Path)
    parser.add_argument("--expected-answer-retrieval-dense-score-matrix-sha256")
    parser.add_argument("--answer-retrieval-query-vector-matrix", type=Path)
    parser.add_argument("--expected-answer-retrieval-query-vector-matrix-sha256")
    parser.add_argument("--answer-retrieval-lexical-ranks", type=Path)
    parser.add_argument("--expected-answer-retrieval-lexical-ranks-sha256")
    parser.add_argument("--answer-decision", type=Path)
    parser.add_argument("--expected-answer-decision-sha256")


def _answer_evidence_by_lane(
    arguments: argparse.Namespace,
) -> dict[AnswerLane, AnswerEvidenceArtifacts] | None:
    path_names = (
        "answer_input",
        "answer_producer_receipt",
        "answer_artifact",
        "answer_call_ledger",
        "answer_state_identity",
        "answer_state_bundle",
        "answer_retrieval_run",
        "answer_retrieval_capture_receipt",
        "answer_retrieval_attestation",
        "answer_retrieval_raw_score",
    )
    hash_names = (
        "expected_answer_input_sha256",
        "expected_answer_producer_receipt_sha256",
        "expected_answer_artifact_sha256",
        "expected_answer_retrieval_run_sha256",
        "expected_answer_retrieval_capture_receipt_sha256",
        "expected_answer_retrieval_attestation_sha256",
        "expected_answer_retrieval_raw_score_sha256",
    )
    path_maps = {
        name: _answer_lane_paths(getattr(arguments, name), label=name) for name in path_names
    }
    hash_maps = {
        name: _answer_lane_hashes(getattr(arguments, name), label=name) for name in hash_names
    }
    profiles = _answer_lane_values(arguments.answer_profile_id, label="answer_profile_id")
    decision_paths = _answer_lane_paths(arguments.answer_decision, label="answer_decision")
    decision_hashes = _answer_lane_hashes(
        arguments.expected_answer_decision_sha256,
        label="expected_answer_decision_sha256",
    )
    corpus_sidecar_paths = _answer_lane_paths(
        arguments.answer_retrieval_corpus_inventory,
        label="answer_retrieval_corpus_inventory",
    )
    corpus_sidecar_hashes = _answer_lane_hashes(
        arguments.expected_answer_retrieval_corpus_inventory_sha256,
        label="expected_answer_retrieval_corpus_inventory_sha256",
    )
    dense_sidecar_paths = _answer_lane_paths(
        arguments.answer_retrieval_dense_score_matrix,
        label="answer_retrieval_dense_score_matrix",
    )
    dense_sidecar_hashes = _answer_lane_hashes(
        arguments.expected_answer_retrieval_dense_score_matrix_sha256,
        label="expected_answer_retrieval_dense_score_matrix_sha256",
    )
    vector_sidecar_paths = _answer_lane_paths(
        arguments.answer_retrieval_query_vector_matrix,
        label="answer_retrieval_query_vector_matrix",
    )
    vector_sidecar_hashes = _answer_lane_hashes(
        arguments.expected_answer_retrieval_query_vector_matrix_sha256,
        label="expected_answer_retrieval_query_vector_matrix_sha256",
    )
    lexical_sidecar_paths = _answer_lane_paths(
        arguments.answer_retrieval_lexical_ranks,
        label="answer_retrieval_lexical_ranks",
    )
    lexical_sidecar_hashes = _answer_lane_hashes(
        arguments.expected_answer_retrieval_lexical_ranks_sha256,
        label="expected_answer_retrieval_lexical_ranks_sha256",
    )
    all_maps: tuple[Mapping[AnswerLane, object], ...] = (
        *path_maps.values(),
        *hash_maps.values(),
        profiles,
        corpus_sidecar_paths,
        corpus_sidecar_hashes,
        dense_sidecar_paths,
        dense_sidecar_hashes,
        vector_sidecar_paths,
        vector_sidecar_hashes,
    )
    optional_maps = (
        decision_paths,
        decision_hashes,
        lexical_sidecar_paths,
        lexical_sidecar_hashes,
    )
    if not any(all_maps) and not any(optional_maps):
        return None
    required_lanes: set[AnswerLane] = {
        "v109_baseline",
        "qwen_page",
        "qwen_structure_exact",
    }
    if any(set(mapping) != required_lanes for mapping in all_maps):
        raise GoldCaptureError("answer_evidence_lane_arguments_incomplete")
    if set(decision_paths) != set(decision_hashes) or not set(decision_paths).issubset(
        required_lanes
    ):
        raise GoldCaptureError("answer_decision_lane_arguments_incomplete")
    if set(lexical_sidecar_paths) != {"v109_baseline"} or set(lexical_sidecar_hashes) != {
        "v109_baseline"
    }:
        raise GoldCaptureError("answer_retrieval_sidecar_lane_arguments_incomplete")
    result: dict[AnswerLane, AnswerEvidenceArtifacts] = {}
    for lane in sorted(required_lanes):
        result[lane] = AnswerEvidenceArtifacts(
            generation_manifest_path=None,
            database_path=None,
            input_path=path_maps["answer_input"][lane],
            expected_input_sha256=hash_maps["expected_answer_input_sha256"][lane],
            producer_receipt_path=path_maps["answer_producer_receipt"][lane],
            expected_producer_receipt_sha256=(
                hash_maps["expected_answer_producer_receipt_sha256"][lane]
            ),
            answer_artifact_path=path_maps["answer_artifact"][lane],
            expected_answer_artifact_sha256=(hash_maps["expected_answer_artifact_sha256"][lane]),
            call_ledger_path=path_maps["answer_call_ledger"][lane],
            state_identity_path=path_maps["answer_state_identity"][lane],
            state_bundle_path=path_maps["answer_state_bundle"][lane],
            answer_profile_id=profiles[lane],
            retrieval_run_path=path_maps["answer_retrieval_run"][lane],
            expected_retrieval_run_sha256=(hash_maps["expected_answer_retrieval_run_sha256"][lane]),
            retrieval_capture_receipt_path=(path_maps["answer_retrieval_capture_receipt"][lane]),
            expected_retrieval_capture_receipt_sha256=(
                hash_maps["expected_answer_retrieval_capture_receipt_sha256"][lane]
            ),
            retrieval_attestation_path=path_maps["answer_retrieval_attestation"][lane],
            expected_retrieval_attestation_sha256=(
                hash_maps["expected_answer_retrieval_attestation_sha256"][lane]
            ),
            retrieval_raw_score_path=path_maps["answer_retrieval_raw_score"][lane],
            expected_retrieval_raw_score_sha256=(
                hash_maps["expected_answer_retrieval_raw_score_sha256"][lane]
            ),
            retrieval_corpus_inventory_path=corpus_sidecar_paths[lane],
            expected_retrieval_corpus_inventory_sha256=(corpus_sidecar_hashes[lane]),
            retrieval_dense_score_matrix_path=dense_sidecar_paths[lane],
            expected_retrieval_dense_score_matrix_sha256=(dense_sidecar_hashes[lane]),
            retrieval_query_vector_matrix_path=vector_sidecar_paths[lane],
            expected_retrieval_query_vector_matrix_sha256=(vector_sidecar_hashes[lane]),
            retrieval_lexical_rank_path=lexical_sidecar_paths.get(lane),
            expected_retrieval_lexical_rank_sha256=lexical_sidecar_hashes.get(lane),
            decision_path=decision_paths.get(lane),
            expected_decision_sha256=decision_hashes.get(lane),
        )
    return result


def _read_secret(path: Path) -> str:
    payload = _read_regular(path, maximum_bytes=64 * 1024, code="api_key_file")
    try:
        value = payload.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise GoldCaptureError("api_key_file_invalid") from exc
    if not value or "\x00" in value or "\n" in value:
        raise GoldCaptureError("api_key_file_invalid")
    return value


def _validated_openrouter_base_url(raw_value: str) -> str:
    """Validate the authenticated provider destination before reading its key."""

    if (
        not raw_value
        or raw_value != raw_value.strip()
        or "\\" in raw_value
        or "?" in raw_value
        or "#" in raw_value
        or any(character.isspace() or not character.isprintable() for character in raw_value)
    ):
        raise GoldCaptureError("openrouter_base_url_invalid")
    try:
        parsed = urlsplit(raw_value)
        _ = parsed.port
    except ValueError as exc:
        raise GoldCaptureError("openrouter_base_url_invalid") from exc
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise GoldCaptureError("openrouter_base_url_invalid")
    return raw_value.rstrip("/")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    external = subparsers.add_parser(
        "external",
        help="validate and seal one reproducible v109_baseline or qwen_page observation",
    )
    external.add_argument("--gold", type=Path, required=True)
    external.add_argument("--expected-gold-sha256", required=True)
    external.add_argument("--expected-source-commit")
    external.add_argument("--observation", type=Path, required=True)
    external.add_argument("--expected-observation-sha256", required=True)
    external.add_argument("--inventory", type=Path, required=True)
    external.add_argument("--expected-inventory-sha256", required=True)
    external.add_argument("--generation-manifest", type=Path, required=True)
    external.add_argument("--database", type=Path, required=True)
    external.add_argument("--source-generation-manifest", type=Path)
    external.add_argument("--source-database", type=Path)
    external.add_argument("--vectors", type=Path)
    external.add_argument("--score-matrix", type=Path, required=True)
    external.add_argument("--query-vector-matrix", type=Path, required=True)
    external.add_argument("--lexical-ranks", type=Path)
    external.add_argument("--output", type=Path, required=True)
    external.add_argument("--receipt", type=Path, required=True)
    external.add_argument("--fixture-mode", action="store_true")
    _add_answer_evidence_arguments(external)

    native = subparsers.add_parser(
        "native-v5",
        help="capture exact, lexical shadow, and reranker shadow from the real v5 APIs",
    )
    native.add_argument("--gold", type=Path, required=True)
    native.add_argument("--expected-gold-sha256", required=True)
    native.add_argument("--score-artifact", type=Path, required=True)
    native.add_argument("--score-corpus-inventory", type=Path, required=True)
    native.add_argument("--score-matrix", type=Path, required=True)
    native.add_argument("--score-query-vector-matrix", type=Path, required=True)
    native.add_argument("--expected-score-artifact-sha256", required=True)
    native.add_argument("--answer-artifact", type=Path)
    native.add_argument("--expected-answer-artifact-sha256")
    native.add_argument("--generation-manifest", type=Path, required=True)
    native.add_argument("--generation-dir", type=Path, required=True)
    native.add_argument("--object-root", type=Path, required=True)
    native.add_argument("--output-dir", type=Path, required=True)
    native.add_argument("--state-dir", type=Path, required=True)
    native.add_argument("--source-commit", required=True)
    native.add_argument("--expected-source-commit")
    native.add_argument("--openrouter-api-key-file", type=Path, required=True)
    native.add_argument("--openrouter-base-url", default="https://openrouter.ai/api/v1")
    native.add_argument("--timeout-seconds", type=float, default=60.0)
    native.add_argument("--reranker-max-candidates", type=int, default=64)
    native.add_argument("--fixture-mode", action="store_true")

    finalize_native = subparsers.add_parser(
        "finalize-native-v5",
        help="offline promotion of one native bootstrap capture with sealed answers",
    )
    finalize_native.add_argument("--gold", type=Path, required=True)
    finalize_native.add_argument("--expected-gold-sha256", required=True)
    finalize_native.add_argument("--expected-source-commit", required=True)
    finalize_native.add_argument("--score-artifact", type=Path, required=True)
    finalize_native.add_argument("--score-corpus-inventory", type=Path, required=True)
    finalize_native.add_argument("--score-matrix", type=Path, required=True)
    finalize_native.add_argument("--score-query-vector-matrix", type=Path, required=True)
    finalize_native.add_argument("--generation-manifest", type=Path, required=True)
    finalize_native.add_argument("--generation-dir", type=Path, required=True)
    finalize_native.add_argument("--object-root", type=Path, required=True)
    finalize_native.add_argument("--bootstrap-attestation", type=Path, required=True)
    finalize_native.add_argument("--bootstrap-run", action="append", default=[])
    finalize_native.add_argument("--bootstrap-receipt", action="append", default=[])
    finalize_native.add_argument(
        "--expected-bootstrap-receipt-sha256",
        action="append",
        default=[],
    )
    finalize_native.add_argument("--reranker-state-root", type=Path, required=True)
    finalize_native.add_argument("--output-dir", type=Path, required=True)
    _add_answer_evidence_arguments(finalize_native)

    validate_native = subparsers.add_parser(
        "validate-native-v5",
        help="offline revalidation of a native v5 capture",
    )
    validate_native.add_argument("--gold", type=Path, required=True)
    validate_native.add_argument("--expected-gold-sha256", required=True)
    validate_native.add_argument("--expected-source-commit")
    validate_native.add_argument("--score-artifact", type=Path, required=True)
    validate_native.add_argument("--score-corpus-inventory", type=Path, required=True)
    validate_native.add_argument("--score-matrix", type=Path, required=True)
    validate_native.add_argument("--score-query-vector-matrix", type=Path, required=True)
    validate_native.add_argument("--answer-artifact", type=Path)
    validate_native.add_argument("--generation-manifest", type=Path, required=True)
    validate_native.add_argument("--generation-dir", type=Path, required=True)
    validate_native.add_argument("--object-root", type=Path, required=True)
    validate_native.add_argument("--attestation", type=Path, required=True)
    validate_native.add_argument("--run", action="append", default=[])
    validate_native.add_argument("--receipt", action="append", default=[])
    validate_native.add_argument("--reranker-state-root", type=Path, required=True)
    validate_native.add_argument("--fixture-mode", action="store_true")

    validate_set = subparsers.add_parser(
        "validate-set",
        help="bind five independently revalidated lane receipts",
    )
    validate_set.add_argument("--gold", type=Path, required=True)
    validate_set.add_argument("--expected-gold-sha256", required=True)
    validate_set.add_argument("--expected-source-commit")
    validate_set.add_argument("--run", action="append", default=[])
    validate_set.add_argument("--receipt", action="append", default=[])
    validate_set.add_argument("--attestation", action="append", default=[])
    validate_set.add_argument("--native-score-artifact", type=Path, required=True)
    validate_set.add_argument("--native-score-corpus-inventory", type=Path, required=True)
    validate_set.add_argument("--native-score-matrix", type=Path, required=True)
    validate_set.add_argument("--native-score-query-vector-matrix", type=Path, required=True)
    validate_set.add_argument("--external-inventory", action="append", default=[])
    validate_set.add_argument("--external-score-matrix", action="append", default=[])
    validate_set.add_argument("--external-query-vector-matrix", action="append", default=[])
    validate_set.add_argument("--external-lexical-ranks", action="append", default=[])
    validate_set.add_argument("--expected-receipt-sha256", action="append", default=[])
    for flag in (
        "answer-input",
        "answer-producer-receipt",
        "answer-artifact",
        "answer-call-ledger",
        "answer-state-identity",
        "answer-state-bundle",
        "answer-retrieval-run",
        "answer-retrieval-capture-receipt",
        "answer-retrieval-attestation",
        "answer-retrieval-raw-score",
        "answer-retrieval-corpus-inventory",
        "answer-retrieval-dense-score-matrix",
        "answer-retrieval-query-vector-matrix",
        "answer-retrieval-lexical-ranks",
        "expected-answer-input-sha256",
        "expected-answer-producer-receipt-sha256",
        "expected-answer-artifact-sha256",
        "expected-answer-retrieval-run-sha256",
        "expected-answer-retrieval-capture-receipt-sha256",
        "expected-answer-retrieval-attestation-sha256",
        "expected-answer-retrieval-raw-score-sha256",
        "expected-answer-retrieval-corpus-inventory-sha256",
        "expected-answer-retrieval-dense-score-matrix-sha256",
        "expected-answer-retrieval-query-vector-matrix-sha256",
        "expected-answer-retrieval-lexical-ranks-sha256",
        "answer-profile-id",
        "answer-decision",
        "expected-answer-decision-sha256",
    ):
        validate_set.add_argument(f"--{flag}", action="append", default=[])
    validate_set.add_argument("--output", type=Path, required=True)
    validate_set.add_argument("--fixture-mode", action="store_true")
    return parser


async def _run_native(arguments: argparse.Namespace) -> NativeV5CaptureResult:
    release_gate = not bool(arguments.fixture_mode)
    source_commit = _validated_source_commit(str(arguments.source_commit))
    expected_source_commit = _validated_expected_source_commit(
        cast(str | None, arguments.expected_source_commit),
        release_gate=release_gate,
    )
    if expected_source_commit is not None and source_commit != expected_source_commit:
        raise GoldCaptureError("candidate_source_commit_mismatch")
    base_url = _validated_openrouter_base_url(str(arguments.openrouter_base_url))
    api_key = _read_secret(cast(Path, arguments.openrouter_api_key_file))
    embedder = OpenRouterEmbedder(
        base_url=base_url,
        api_key=api_key,
        timeout_seconds=float(arguments.timeout_seconds),
    )
    state = _safe_state_directory(cast(Path, arguments.state_dir))
    reranker_client = OpenRouterReranker(
        base_url=base_url,
        api_key=api_key,
        timeout_seconds=float(arguments.timeout_seconds),
    )
    reranker_lane = RerankerShadowLane(
        reranker_client,
        RerankerShadowStore(state),
        maximum_candidates=int(arguments.reranker_max_candidates),
    )
    try:
        return await capture_native_v5_lanes(
            gold_path=cast(Path, arguments.gold),
            expected_gold_sha256=str(arguments.expected_gold_sha256),
            score_artifact_path=cast(Path, arguments.score_artifact),
            score_corpus_inventory_path=cast(Path, arguments.score_corpus_inventory),
            score_matrix_path=cast(Path, arguments.score_matrix),
            score_query_vector_matrix_path=cast(
                Path,
                arguments.score_query_vector_matrix,
            ),
            expected_score_artifact_sha256=str(arguments.expected_score_artifact_sha256),
            answer_artifact_path=cast(Path | None, arguments.answer_artifact),
            expected_answer_artifact_sha256=cast(
                str | None,
                arguments.expected_answer_artifact_sha256,
            ),
            generation_manifest_path=cast(Path, arguments.generation_manifest),
            generation_directory=cast(Path, arguments.generation_dir),
            object_root=cast(Path, arguments.object_root),
            output_directory=cast(Path, arguments.output_dir),
            state_directory=state,
            source_commit=source_commit,
            embedder=embedder,
            reranker_lane=reranker_lane,
            expected_source_commit=expected_source_commit,
            release_gate=release_gate,
        )
    finally:
        await reranker_lane.close()
        await embedder.close()


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "external":
            external_generation_manifest = cast(Path, arguments.generation_manifest)
            external_database = cast(Path, arguments.database)
            receipt = seal_external_observation(
                gold_path=cast(Path, arguments.gold),
                expected_gold_sha256=str(arguments.expected_gold_sha256),
                observation_path=cast(Path, arguments.observation),
                expected_observation_sha256=str(arguments.expected_observation_sha256),
                inventory_path=cast(Path, arguments.inventory),
                expected_inventory_sha256=str(arguments.expected_inventory_sha256),
                generation_manifest_path=external_generation_manifest,
                database_path=external_database,
                vector_path=cast(Path | None, arguments.vectors),
                dense_score_matrix_path=cast(Path, arguments.score_matrix),
                query_vector_matrix_path=cast(Path, arguments.query_vector_matrix),
                lexical_rank_path=cast(Path | None, arguments.lexical_ranks),
                output_path=cast(Path, arguments.output),
                receipt_path=cast(Path, arguments.receipt),
                answer_evidence_artifacts=_single_answer_evidence(
                    arguments,
                    generation_manifest_path=external_generation_manifest,
                    database_path=external_database,
                ),
                source_generation_manifest_path=cast(
                    Path | None,
                    arguments.source_generation_manifest,
                ),
                source_database_path=cast(Path | None, arguments.source_database),
                expected_source_commit=cast(str | None, arguments.expected_source_commit),
                release_gate=not bool(arguments.fixture_mode),
            )
            payload: object = receipt
        elif arguments.command == "native-v5":
            result = asyncio.run(_run_native(arguments))
            payload = {
                "attestation_path": str(result.attestation_path),
                "receipt_paths": {
                    lane: str(path) for lane, path in sorted(result.receipt_paths.items())
                },
                "resumed_queries": result.resumed_queries,
                "run_paths": {lane: str(path) for lane, path in sorted(result.run_paths.items())},
            }
        elif arguments.command == "finalize-native-v5":
            generation_manifest_path = cast(Path, arguments.generation_manifest)
            generation_directory = cast(Path, arguments.generation_dir)
            evidence = _single_answer_evidence(
                arguments,
                generation_manifest_path=generation_manifest_path,
                database_path=generation_directory / "index.sqlite3",
            )
            if evidence is None:
                raise GoldCaptureError("answer_evidence_required")
            result = finalize_native_v5_with_answers(
                gold_path=cast(Path, arguments.gold),
                expected_gold_sha256=str(arguments.expected_gold_sha256),
                score_artifact_path=cast(Path, arguments.score_artifact),
                score_corpus_inventory_path=cast(
                    Path,
                    arguments.score_corpus_inventory,
                ),
                score_matrix_path=cast(Path, arguments.score_matrix),
                score_query_vector_matrix_path=cast(
                    Path,
                    arguments.score_query_vector_matrix,
                ),
                generation_manifest_path=generation_manifest_path,
                generation_directory=generation_directory,
                object_root=cast(Path, arguments.object_root),
                bootstrap_attestation_path=cast(Path, arguments.bootstrap_attestation),
                bootstrap_run_paths=_lane_paths(arguments.bootstrap_run, label="bootstrap_run"),
                bootstrap_receipt_paths=_lane_paths(
                    arguments.bootstrap_receipt,
                    label="bootstrap_receipt",
                ),
                expected_bootstrap_receipt_sha256=_lane_hashes(
                    arguments.expected_bootstrap_receipt_sha256,
                    label="bootstrap_receipt_sha256",
                ),
                reranker_state_root=cast(Path, arguments.reranker_state_root),
                answer_evidence_artifacts=evidence,
                output_directory=cast(Path, arguments.output_dir),
                expected_source_commit=str(arguments.expected_source_commit),
            )
            payload = {
                "attestation_path": str(result.attestation_path),
                "receipt_paths": {
                    lane: str(path) for lane, path in sorted(result.receipt_paths.items())
                },
                "run_paths": {lane: str(path) for lane, path in sorted(result.run_paths.items())},
            }
        elif arguments.command == "validate-native-v5":
            receipts = validate_native_v5_capture(
                gold_path=cast(Path, arguments.gold),
                expected_gold_sha256=str(arguments.expected_gold_sha256),
                score_artifact_path=cast(Path, arguments.score_artifact),
                score_corpus_inventory_path=cast(
                    Path,
                    arguments.score_corpus_inventory,
                ),
                score_matrix_path=cast(Path, arguments.score_matrix),
                score_query_vector_matrix_path=cast(
                    Path,
                    arguments.score_query_vector_matrix,
                ),
                answer_artifact_path=cast(Path | None, arguments.answer_artifact),
                generation_manifest_path=cast(Path, arguments.generation_manifest),
                generation_directory=cast(Path, arguments.generation_dir),
                object_root=cast(Path, arguments.object_root),
                attestation_path=cast(Path, arguments.attestation),
                run_paths=_lane_paths(arguments.run, label="run"),
                receipt_paths=_lane_paths(arguments.receipt, label="receipt"),
                reranker_state_root=cast(Path, arguments.reranker_state_root),
                expected_source_commit=cast(str | None, arguments.expected_source_commit),
                release_gate=not bool(arguments.fixture_mode),
            )
            payload = {lane: receipt for lane, receipt in sorted(receipts.items())}
        else:
            set_receipt = validate_capture_set(
                gold_path=cast(Path, arguments.gold),
                expected_gold_sha256=str(arguments.expected_gold_sha256),
                run_paths=_lane_paths(arguments.run, label="run"),
                receipt_paths=_lane_paths(arguments.receipt, label="receipt"),
                attestation_paths=_lane_paths(arguments.attestation, label="attestation"),
                native_score_artifact_path=cast(Path, arguments.native_score_artifact),
                native_score_corpus_inventory_path=cast(
                    Path,
                    arguments.native_score_corpus_inventory,
                ),
                native_score_matrix_path=cast(Path, arguments.native_score_matrix),
                native_score_query_vector_matrix_path=cast(
                    Path,
                    arguments.native_score_query_vector_matrix,
                ),
                external_inventory_paths=_lane_paths(
                    arguments.external_inventory,
                    label="external_inventory",
                ),
                external_dense_score_matrix_paths=_lane_paths(
                    arguments.external_score_matrix,
                    label="external_score_matrix",
                ),
                external_query_vector_matrix_paths=_lane_paths(
                    arguments.external_query_vector_matrix,
                    label="external_query_vector_matrix",
                ),
                external_lexical_rank_paths=_lane_paths(
                    arguments.external_lexical_ranks,
                    label="external_lexical_ranks",
                ),
                expected_receipt_sha256=_lane_hashes(
                    arguments.expected_receipt_sha256,
                    label="receipt_sha256",
                ),
                answer_evidence_artifacts=_answer_evidence_by_lane(arguments),
                output_path=cast(Path, arguments.output),
                expected_source_commit=cast(str | None, arguments.expected_source_commit),
                release_gate=not bool(arguments.fixture_mode),
            )
            payload = set_receipt
    except Exception as exc:
        code = exc.code if isinstance(exc, GoldCaptureError) else str(exc)
        print(f"gold capture failed: {code}", file=sys.stderr)
        return 2
    print(canonical_json_bytes(payload).decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
