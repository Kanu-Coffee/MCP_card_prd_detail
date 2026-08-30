"""Offline-first producers for the historical and page-window gold lanes.

The production MCP process never imports this module.  It reads immutable source
artifacts, creates only caller-selected evidence paths, and deliberately has no
generation-pointer or WebDAV integration.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import math
import os
import sqlite3
import stat
import struct
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, Self, cast

import httpx
import numpy as np
import numpy.typing as npt
from cardrag_core import (
    QUERY_EMBEDDING_PREFIX,
    QWEN3_EMBEDDING_DIMENSION,
    QWEN3_EMBEDDING_MODEL,
    QWEN3_QUERY_POLICY,
    GenerationManifest,
    canonical_json_bytes,
    canonical_sha256,
    format_qwen3_document,
    format_qwen3_query,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from cardrag_mcp.embeddings import _provider_matches
from cardrag_mcp.evaluation import (
    V109_BASELINE_COMMIT,
    EvaluatedAnswer,
    GoldDataset,
    QueryRunResult,
    RetrievedContract,
    RetrievedSpan,
    V109BaselineObservation,
    load_gold_jsonl,
)
from cardrag_mcp.gold_capture import (
    ArtifactBinding,
    CorpusInventoryManifest,
    CorpusInventoryRow,
    ExternalLexicalRanks,
    ExternalObservationManifest,
    ExternalQueryObservation,
    GoldCaptureError,
    PageGenerationManifest,
    _CanonicalJsonlReader,
    _hash_regular,
    _jsonl_bytes,
    _load_answers,
    _load_generation_manifest,
    _publish_immutable,
    _read_regular,
    _read_secret,
    _sqlite_readonly,
    _validated_openrouter_base_url,
    _validated_source_commit,
)

Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Identifier = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,511}$"),
]
SourceCommit = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{40}([0-9a-f]{24})?$"),
]
EmbeddingLane = Literal["v109_baseline", "qwen_page"]
EmbeddingKind = Literal["document", "query"]

EMBEDDING_REPLAY_SCHEMA: Literal["cardrag.gold-embedding-replay.v1"] = (
    "cardrag.gold-embedding-replay.v1"
)
EMBEDDING_REPLAY_ROW_SCHEMA: Literal["cardrag.gold-embedding-replay-row.v1"] = (
    "cardrag.gold-embedding-replay-row.v1"
)
PAGE_CHUNKING_POLICY: Literal["cardrag.page-window-1600.v1"] = "cardrag.page-window-1600.v1"
PAGE_MAXIMUM_CHARS: Literal[1600] = 1600
PAGE_OVERLAP_CHARS: Literal[160] = 160
PAGE_SOURCE_TEXT_CONTRACT: Literal["cardrag.page-source-text-range.v1"] = (
    "cardrag.page-source-text-range.v1"
)
PAGE_COLUMN_CONTRACT: Literal["cardrag.evaluation-page-columns.v1"] = (
    "cardrag.evaluation-page-columns.v1"
)
PAGE_CHUNK_SOURCE_PATH = "apps/cardrag-worker/src/cardrag_worker/pipeline.py::chunk_pages"
PAGE_CHUNK_SOURCE_COMMIT = V109_BASELINE_COMMIT

_MAX_MANIFEST_BYTES = 32 * 1024 * 1024
_MAX_DATABASE_BYTES = 4 * 1024 * 1024 * 1024
_MAX_EMBEDDING_REPLAY_BYTES = 64 * 1024 * 1024 * 1024
_MAX_VECTOR_BYTES = 64 * 1024 * 1024 * 1024
_NORM_TOLERANCE = 2e-5
_MIN_PROVIDER_RESPONSE_BYTES = 1024
_MAX_PROVIDER_RESPONSE_BYTES = 64 * 1024 * 1024
_MAX_PROVIDER_TIMEOUT_SECONDS = 3600.0
_MAX_QWEN_BATCH_SIZE = 128
# Deterministic producer working-set budget for an operator host with at least
# 8 GiB RAM.  The documented 5,799-row Page corpus has ample room, while a
# malformed local input cannot finish paid calls before discovering that its
# replay is too large for the supported single-process workflow.
_QWEN_REPLAY_WORKING_SET_LIMIT_BYTES = 1536 * 1024 * 1024
_QWEN_REPLAY_FIXED_HEADROOM_BYTES = 256 * 1024 * 1024
_QWEN_INPUT_RESIDENT_MULTIPLIER = 2
_QWEN_RESPONSE_RESIDENT_MULTIPLIER = 6
_MAX_IDENTIFIER_CHARACTERS = 512
_PAGE_CHUNK_OBJECT_HEADROOM_BYTES = 2048
_SOURCE_PAGE_ROW_HEADROOM_BYTES = 2048
_SOURCE_REVISION_HEADROOM_BYTES = 512
_SOURCE_PAGE_TOTAL_TEXT_LIMIT_BYTES = (
    _QWEN_REPLAY_WORKING_SET_LIMIT_BYTES - _QWEN_REPLAY_FIXED_HEADROOM_BYTES
) // 4
_SOURCE_PAGE_SINGLE_TEXT_LIMIT_BYTES = (
    _QWEN_REPLAY_WORKING_SET_LIMIT_BYTES - _QWEN_REPLAY_FIXED_HEADROOM_BYTES
) // 16
MAX_GIT_EVIDENCE_FILE_BYTES = 95_000_000
_OFFICIAL_OPENROUTER_API_BASE_URL: Literal["https://openrouter.ai/api/v1"] = (
    "https://openrouter.ai/api/v1"
)
V109_PRESERVED_RUN_ID = "2208f0c6076649c4be915be182422b6a"
V109_PRESERVED_GENERATION_ID = "g-2208f0c6076649c4be915be1-d11f80f9af71"
V109_PRESERVED_PUBLISH_SHA256 = "83ff730f7972ccc8cafb2be4bf8b82d7c65236c531244b722ccba2a5d5225ffa"
V109_PRESERVED_PUBLISH_SIZE_BYTES = 958_668
V109_PRESERVED_MANIFEST_SHA256 = "dd12487e4f92a2d84362322f04d027421540c6bda27659e46cf6af553e216002"
V109_PRESERVED_MANIFEST_SIZE_BYTES = 542_209
V109_PRESERVED_DATABASE_SHA256 = "d25be45bc5d39af6561e587635b08312913107b6f6416500da39ab9eb757d38f"
V109_PRESERVED_DATABASE_SIZE_BYTES = 58_466_304
QWEN_TOKENIZER_REVISION: Literal["1d8ad4ca9b3dd8059ad90a75d4983776a23d44af"] = (
    "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af"
)
QWEN_TOKENIZER_SHA256: Literal[
    "83cdf8c3a34f68862319cb1810ee7b1e2c0a44e0864ae930194ddb76bb7feb8d"
] = "83cdf8c3a34f68862319cb1810ee7b1e2c0a44e0864ae930194ddb76bb7feb8d"
QWEN_TOKENIZER_SIZE_BYTES = 11_422_947


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self)


class EmbeddingReplayManifest(_StrictModel):
    """Identity of sealed provider output replayed without network access."""

    schema_version: Literal["cardrag.gold-embedding-replay.v1"]
    lane: EmbeddingLane
    input_kind: EmbeddingKind
    synthetic: Literal[False]
    source_commit: SourceCommit
    embedding_model: Literal[
        "openai/text-embedding-3-small",
        "qwen/qwen3-embedding-8b",
    ]
    embedding_dimension: Literal[1536, 4096]
    embedding_profile_id: Identifier
    query_policy: Literal["cardrag.embedding-input.v1", "cardrag.qwen3-query.v1"] | None
    document_policy: Literal["cardrag.page-window-1600.v1"] | None
    provider_receipt: ArtifactBinding | None = None
    record_count: int = Field(ge=1)

    @model_validator(mode="after")
    def profile_matches_lane_and_kind(self) -> Self:
        if self.lane == "v109_baseline":
            valid = (
                self.input_kind == "query"
                and self.source_commit == V109_BASELINE_COMMIT
                and self.embedding_model == "openai/text-embedding-3-small"
                and self.embedding_dimension == 1536
                and self.embedding_profile_id == "cardrag.embedding.v109-small.v1"
                and self.query_policy == "cardrag.embedding-input.v1"
                and self.document_policy is None
                and self.provider_receipt is not None
            )
        elif self.input_kind == "query":
            valid = (
                self.embedding_model == QWEN3_EMBEDDING_MODEL
                and self.embedding_dimension == QWEN3_EMBEDDING_DIMENSION
                and self.query_policy == QWEN3_QUERY_POLICY
                and self.document_policy is None
                and self.provider_receipt is not None
            )
        else:
            valid = (
                self.embedding_model == QWEN3_EMBEDDING_MODEL
                and self.embedding_dimension == QWEN3_EMBEDDING_DIMENSION
                and self.query_policy is None
                and self.document_policy == PAGE_CHUNKING_POLICY
                and self.provider_receipt is not None
            )
        if not valid:
            raise ValueError("embedding replay profile does not match its lane and input kind")
        return self


class EmbeddingReplayRow(_StrictModel):
    schema_version: Literal["cardrag.gold-embedding-replay-row.v1"]
    ordinal: int = Field(ge=0)
    input_id: Identifier
    formatted_input_sha256: Sha256Hex
    vector_f32_sha256: Sha256Hex
    vector_f32_base64: str = Field(min_length=4, max_length=64 * 1024)


class QwenEmbeddingInputManifest(_StrictModel):
    schema_version: Literal["cardrag.gold-qwen-embedding-input.v1"]
    lane: Literal["qwen_page"]
    input_kind: EmbeddingKind
    source_commit: SourceCommit
    embedding_model: Literal["qwen/qwen3-embedding-8b"]
    embedding_dimension: Literal[4096]
    embedding_profile_id: Identifier
    provider_id: Literal["deepinfra", "nebius"]
    maximum_tokens: int = Field(ge=1)
    tokenizer_revision: Literal["1d8ad4ca9b3dd8059ad90a75d4983776a23d44af"]
    tokenizer_sha256: Literal["83cdf8c3a34f68862319cb1810ee7b1e2c0a44e0864ae930194ddb76bb7feb8d"]
    record_count: int = Field(ge=1)

    @model_validator(mode="after")
    def profile_id_is_exact(self) -> Self:
        from cardrag_core import qwen3_embedding_profile_id

        if self.embedding_profile_id != qwen3_embedding_profile_id(
            self.provider_id,
            maximum_tokens=self.maximum_tokens,
        ):
            raise ValueError("Qwen input profile ID is invalid")
        return self


class QwenEmbeddingInputRow(_StrictModel):
    schema_version: Literal["cardrag.gold-qwen-embedding-input-row.v1"]
    ordinal: int = Field(ge=0)
    input_id: Identifier
    formatted_input_sha256: Sha256Hex
    formatted_input: str = Field(min_length=1, max_length=65_536)
    token_count: int = Field(ge=1)

    @model_validator(mode="after")
    def hash_is_exact(self) -> Self:
        if (
            self.formatted_input != self.formatted_input.strip()
            or hashlib.sha256(self.formatted_input.encode("utf-8")).hexdigest()
            != self.formatted_input_sha256
        ):
            raise ValueError("Qwen formatted input is not canonical and hash-bound")
        return self


class ProviderResponseArtifact(_StrictModel):
    file_name: Identifier
    artifact: ArtifactBinding

    @model_validator(mode="after")
    def file_name_is_one_safe_component(self) -> Self:
        if Path(self.file_name).name != self.file_name or self.file_name in {".", ".."}:
            raise ValueError("provider response file name must be one safe component")
        return self


class ProviderRequestRecord(_StrictModel):
    ordinal: int = Field(ge=0)
    input_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=128)
    request_body_sha256: Sha256Hex
    response_file_name: Identifier
    idempotency_key: Sha256Hex

    @model_validator(mode="after")
    def identity_is_canonical(self) -> Self:
        if (
            len(set(self.input_ids)) != len(self.input_ids)
            or Path(self.response_file_name).name != self.response_file_name
            or self.response_file_name in {".", ".."}
        ):
            raise ValueError("provider request record identity is invalid")
        expected_key = canonical_sha256(
            {
                "input_ids": list(self.input_ids),
                "ordinal": self.ordinal,
                "request_body_sha256": self.request_body_sha256,
                "response_file_name": self.response_file_name,
                "schema_version": "cardrag.gold-provider-request-reservation.v1",
            }
        )
        if self.idempotency_key != expected_key:
            raise ValueError("provider request reservation identity is invalid")
        return self


class ProviderReceipt(_StrictModel):
    """Request/raw-response evidence required by release-grade replay files."""

    schema_version: Literal["cardrag.gold-provider-receipt.v1"]
    provider: Literal["openrouter"]
    base_url: Literal["https://openrouter.ai/api/v1"]
    model: Literal[
        "openai/text-embedding-3-small",
        "qwen/qwen3-embedding-8b",
    ]
    provider_id: Literal["deepinfra", "nebius"] | None
    source_generation_id: Identifier | None = None
    source_generation_manifest: ArtifactBinding | None = None
    source_serving_database: ArtifactBinding | None = None
    request_contract_sha256: Sha256Hex
    requests: tuple[ProviderRequestRecord, ...] = Field(min_length=1)
    response_artifact_sha256: Sha256Hex
    response_artifacts: tuple[ProviderResponseArtifact, ...] = Field(min_length=1)
    input_count: int = Field(ge=1)

    @model_validator(mode="after")
    def request_and_response_sets_are_canonical_and_bound(self) -> Self:
        names = tuple(item.file_name for item in self.response_artifacts)
        request_names = tuple(item.response_file_name for item in self.requests)
        source_fields = (
            self.source_generation_id,
            self.source_generation_manifest,
            self.source_serving_database,
        )
        if (
            names != tuple(sorted(set(names)))
            or request_names != names
            or tuple(item.ordinal for item in self.requests) != tuple(range(len(self.requests)))
            or sum(len(item.input_ids) for item in self.requests) != self.input_count
            or len({input_id for item in self.requests for input_id in item.input_ids})
            != self.input_count
        ):
            raise ValueError("provider request/response artifact order is invalid")
        if self.model == "openai/text-embedding-3-small":
            if self.provider_id is not None or not all(
                value is not None for value in source_fields
            ):
                raise ValueError("historical provider receipt source identity is incomplete")
        elif self.provider_id not in {"deepinfra", "nebius"} or any(
            value is not None for value in source_fields
        ):
            raise ValueError("Qwen provider receipt profile is invalid")
        expected_responses = canonical_sha256(
            {
                "artifacts": [item.model_dump(mode="json") for item in self.response_artifacts],
                "schema_version": "cardrag.gold-provider-responses.v1",
            }
        )
        expected_request = canonical_sha256(
            {
                "base_url": self.base_url,
                "input_count": self.input_count,
                "model": self.model,
                "provider": self.provider,
                "provider_id": self.provider_id,
                "requests": [item.model_dump(mode="json") for item in self.requests],
                "source_generation_id": self.source_generation_id,
                "source_generation_manifest": self.source_generation_manifest,
                "source_serving_database": self.source_serving_database,
                "schema_version": "cardrag.gold-provider-request-contract.v1",
            }
        )
        if self.response_artifact_sha256 != expected_responses:
            raise ValueError("provider response artifact set is not bound")
        if self.request_contract_sha256 != expected_request:
            raise ValueError("provider request contract is not bound")
        return self


class EmbeddingRawResponseEnvelope(_StrictModel):
    schema_version: Literal["cardrag.gold-embedding-provider-response.v1"]
    status_code: Literal[200]
    provider_header: str | None = Field(default=None, min_length=1, max_length=512)
    body_sha256: Sha256Hex
    body_size_bytes: int = Field(ge=1, le=_MAX_PROVIDER_RESPONSE_BYTES)
    body_base64: str = Field(min_length=4)

    @model_validator(mode="after")
    def body_is_canonical_base64_and_bound(self) -> Self:
        try:
            raw = base64.b64decode(self.body_base64, validate=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("Qwen response body is not canonical base64") from exc
        if (
            base64.b64encode(raw).decode("ascii") != self.body_base64
            or len(raw) != self.body_size_bytes
            or hashlib.sha256(raw).hexdigest() != self.body_sha256
        ):
            raise ValueError("Qwen response body binding is invalid")
        return self


@dataclass(frozen=True, slots=True)
class PageChunk:
    row_index: int
    chunk_id: str
    contract_revision_id: str
    document_id: str
    page: int
    source_start: int
    source_end: int
    text: str
    input_sha256: str


@dataclass(frozen=True, slots=True)
class _PageEmbeddingInputs(Sequence[tuple[str, str]]):
    chunks: Sequence[PageChunk]

    def __len__(self) -> int:
        return len(self.chunks)

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return tuple(self[position] for position in range(*index.indices(len(self))))
        chunk = self.chunks[index]
        return chunk.chunk_id, format_qwen3_document(chunk.text)


@dataclass(frozen=True, slots=True)
class QwenPageCorpus:
    database: ArtifactBinding
    vectors: ArtifactBinding
    inventory: ArtifactBinding
    generation_manifest: ArtifactBinding
    generation_id: str
    row_count: int


@dataclass(frozen=True, slots=True)
class CompactObservationArtifacts:
    observations: tuple[ExternalQueryObservation, ...]
    dense_score_matrix: ArtifactBinding
    query_vector_matrix: ArtifactBinding
    lexical_rank_artifact: ArtifactBinding | None


@dataclass(frozen=True, slots=True)
class QwenReplayResourceForecast:
    record_count: int
    matrix_size_bytes: int
    replay_size_bytes: int
    peak_working_set_bytes: int


@dataclass(frozen=True, slots=True)
class PageVectorLoadForecast:
    row_count: int
    vector_size_bytes: int
    peak_working_set_bytes: int


@dataclass(frozen=True, slots=True)
class QwenProviderReceiptForecast:
    request_count: int
    receipt_size_bytes: int


@dataclass(frozen=True, slots=True)
class SourcePageResourceForecast:
    chunk_count: int
    chunk_text_size_bytes: int
    retained_resident_size_bytes: int
    peak_working_set_bytes: int


def _validated_exact_int(
    value: object,
    *,
    minimum: int,
    maximum: int | None,
    code: str,
) -> int:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise GoldCaptureError(code)
    return value


def _validated_provider_timeout(value: object, *, code: str) -> float:
    if type(value) is int:
        valid = 0 < value <= _MAX_PROVIDER_TIMEOUT_SECONDS
    elif type(value) is float:
        valid = math.isfinite(value) and value > 0.0 and value <= _MAX_PROVIDER_TIMEOUT_SECONDS
    else:
        valid = False
    if not valid:
        raise GoldCaptureError(code)
    return float(cast(int | float, value))


def _validated_provider_response_limit(value: object, *, code: str) -> int:
    return _validated_exact_int(
        value,
        minimum=_MIN_PROVIDER_RESPONSE_BYTES,
        maximum=_MAX_PROVIDER_RESPONSE_BYTES,
        code=code,
    )


def _strict_json_object(payload: bytes, *, code: str) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise GoldCaptureError(f"{code}_duplicate_key")
            result[key] = value
        return result

    def reject_constant(_value: str) -> Any:
        raise GoldCaptureError(f"{code}_non_finite")

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except GoldCaptureError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GoldCaptureError(f"{code}_invalid") from exc
    if not isinstance(value, dict) or canonical_json_bytes(value) != payload:
        raise GoldCaptureError(f"{code}_not_canonical")
    return cast(dict[str, Any], value)


def _strict_provider_json_object(payload: bytes, *, code: str) -> Mapping[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise GoldCaptureError(f"{code}_duplicate_key")
            result[key] = value
        return result

    def reject_constant(_value: str) -> Any:
        raise GoldCaptureError(f"{code}_non_finite")

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except GoldCaptureError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GoldCaptureError(f"{code}_json_invalid") from exc
    if not isinstance(value, Mapping):
        raise GoldCaptureError(f"{code}_invalid")
    return value


def _manifest_from_source_payload(payload: bytes) -> tuple[GenerationManifest, bool]:
    root = _strict_json_object(payload, code="generation_manifest_source")
    candidate: object = root
    nested = False
    if "manifest" in root:
        nested = True
        candidate = root["manifest"]
        if not isinstance(candidate, Mapping):
            raise GoldCaptureError("worker_seal_manifest_invalid")
    try:
        manifest = GenerationManifest.model_validate_json(canonical_json_bytes(candidate))
    except Exception as exc:
        raise GoldCaptureError("generation_manifest_invalid") from exc
    manifest_payload = manifest.canonical_bytes()
    if candidate == root and payload != manifest_payload:
        raise GoldCaptureError("generation_manifest_not_canonical")
    return manifest, nested


def extract_generation_manifest(
    source_path: Path,
    *,
    output_path: Path,
) -> tuple[GenerationManifest, ArtifactBinding]:
    """Extract a canonical manifest from a standalone file or Worker publish seal."""

    payload = _read_regular(
        source_path,
        maximum_bytes=_MAX_MANIFEST_BYTES,
        code="generation_manifest_source",
    )
    manifest, _nested = _manifest_from_source_payload(payload)
    manifest_payload = manifest.canonical_bytes()
    binding = _publish_immutable(output_path, manifest_payload)
    return manifest, binding


def _binding_from_bytes(payload: bytes) -> ArtifactBinding:
    return ArtifactBinding(sha256=hashlib.sha256(payload).hexdigest(), size_bytes=len(payload))


def _qwen_replay_manifest(
    source: QwenEmbeddingInputManifest,
    *,
    provider_receipt: ArtifactBinding,
) -> EmbeddingReplayManifest:
    return EmbeddingReplayManifest(
        schema_version=EMBEDDING_REPLAY_SCHEMA,
        lane="qwen_page",
        input_kind=source.input_kind,
        synthetic=False,
        source_commit=source.source_commit,
        embedding_model=QWEN3_EMBEDDING_MODEL,
        embedding_dimension=QWEN3_EMBEDDING_DIMENSION,
        embedding_profile_id=source.embedding_profile_id,
        query_policy=QWEN3_QUERY_POLICY if source.input_kind == "query" else None,
        document_policy=PAGE_CHUNKING_POLICY if source.input_kind == "document" else None,
        provider_receipt=provider_receipt,
        record_count=source.record_count,
    )


def _embedding_replay_row_size(*, ordinal: int, input_id: str, dimension: int) -> int:
    vector_size = dimension * 4
    base64_size = 4 * ((vector_size + 2) // 3)
    without_vector = canonical_json_bytes(
        {
            "formatted_input_sha256": "0" * 64,
            "input_id": input_id,
            "ordinal": ordinal,
            "schema_version": EMBEDDING_REPLAY_ROW_SCHEMA,
            "vector_f32_base64": "",
            "vector_f32_sha256": "0" * 64,
        }
    )
    return len(without_vector) + base64_size + 1


def _qwen_streaming_replay_resident_size(
    *,
    manifest: EmbeddingReplayManifest,
    expected_inputs: Sequence[tuple[str, str]],
    retained_resident_size_bytes: int,
) -> int:
    if manifest.lane != "qwen_page" or manifest.provider_receipt is None:
        raise GoldCaptureError("embedding_replay_manifest_binding_mismatch")
    maximum_line_size = len(manifest.canonical_bytes()) + 1
    for ordinal, (input_id, _formatted) in enumerate(expected_inputs):
        maximum_line_size = max(
            maximum_line_size,
            _embedding_replay_row_size(
                ordinal=ordinal,
                input_id=input_id,
                dimension=manifest.embedding_dimension,
            ),
        )
    retained_resident_size_bytes = _validated_exact_int(
        retained_resident_size_bytes,
        minimum=0,
        maximum=_MAX_EMBEDDING_REPLAY_BYTES,
        code="qwen_provider_capture_limits_invalid",
    )
    return manifest.provider_receipt.size_bytes + maximum_line_size + retained_resident_size_bytes


def _forecast_qwen_replay_resources(
    *,
    record_count: int,
    dimension: int,
    input_resident_size_bytes: int,
    maximum_response_bytes: int,
    batch_size: int,
    manifest_line_size_bytes: int,
    input_ids: Sequence[str] | None = None,
) -> QwenReplayResourceForecast:
    """Bound replay disk size and peak resident memory before provider access."""

    code = "qwen_provider_capture_limits_invalid"
    record_count = _validated_exact_int(
        record_count,
        minimum=1,
        maximum=None,
        code=code,
    )
    dimension = _validated_exact_int(
        dimension,
        minimum=QWEN3_EMBEDDING_DIMENSION,
        maximum=QWEN3_EMBEDDING_DIMENSION,
        code=code,
    )
    input_resident_size_bytes = _validated_exact_int(
        input_resident_size_bytes,
        minimum=1,
        maximum=_MAX_EMBEDDING_REPLAY_BYTES,
        code=code,
    )
    maximum_response_bytes = _validated_provider_response_limit(
        maximum_response_bytes,
        code=code,
    )
    batch_size = _validated_exact_int(
        batch_size,
        minimum=1,
        maximum=_MAX_QWEN_BATCH_SIZE,
        code=code,
    )
    manifest_line_size_bytes = _validated_exact_int(
        manifest_line_size_bytes,
        minimum=1,
        maximum=_MAX_MANIFEST_BYTES + 1,
        code=code,
    )
    matrix_size = record_count * dimension * 4
    batch_matrix_size = min(record_count, batch_size) * dimension * 4
    peak_working_set = (
        _QWEN_REPLAY_FIXED_HEADROOM_BYTES
        + input_resident_size_bytes * _QWEN_INPUT_RESIDENT_MULTIPLIER
        + matrix_size
        + maximum_response_bytes * _QWEN_RESPONSE_RESIDENT_MULTIPLIER
        + batch_matrix_size * 3
    )
    if peak_working_set > _QWEN_REPLAY_WORKING_SET_LIMIT_BYTES:
        raise GoldCaptureError("qwen_provider_capture_resource_limit_exceeded")
    if input_ids is None:
        maximum_row_size = _embedding_replay_row_size(
            ordinal=record_count - 1,
            input_id="x" * _MAX_IDENTIFIER_CHARACTERS,
            dimension=dimension,
        )
        replay_size = manifest_line_size_bytes + record_count * maximum_row_size
    else:
        if len(input_ids) != record_count:
            raise GoldCaptureError("qwen_embedding_input_manifest_binding_mismatch")
        replay_size = manifest_line_size_bytes + sum(
            _embedding_replay_row_size(
                ordinal=ordinal,
                input_id=input_id,
                dimension=dimension,
            )
            for ordinal, input_id in enumerate(input_ids)
        )
    if replay_size > _MAX_EMBEDDING_REPLAY_BYTES:
        raise GoldCaptureError("qwen_embedding_replay_size_invalid")
    return QwenReplayResourceForecast(
        record_count=record_count,
        matrix_size_bytes=matrix_size,
        replay_size_bytes=replay_size,
        peak_working_set_bytes=peak_working_set,
    )


def _forecast_qwen_provider_receipt_size(
    *,
    input_ids: Sequence[str],
    provider_id: Literal["deepinfra", "nebius"],
    batch_size: int,
    maximum_response_bytes: int,
) -> QwenProviderReceiptForecast:
    """Conservatively bound the canonical receipt before any provider call."""

    code = "qwen_provider_capture_limits_invalid"
    batch_size = _validated_exact_int(
        batch_size,
        minimum=1,
        maximum=_MAX_QWEN_BATCH_SIZE,
        code=code,
    )
    maximum_response_bytes = _validated_provider_response_limit(
        maximum_response_bytes,
        code=code,
    )
    if provider_id not in {"deepinfra", "nebius"} or not input_ids:
        raise GoldCaptureError(code)
    if any(type(input_id) is not str or not 1 <= len(input_id) <= 512 for input_id in input_ids):
        raise GoldCaptureError(code)
    empty_receipt = canonical_json_bytes(
        {
            "base_url": _OFFICIAL_OPENROUTER_API_BASE_URL,
            "input_count": len(input_ids),
            "model": QWEN3_EMBEDDING_MODEL,
            "provider": "openrouter",
            "provider_id": provider_id,
            "request_contract_sha256": "f" * 64,
            "requests": [],
            "response_artifact_sha256": "f" * 64,
            "response_artifacts": [],
            "schema_version": "cardrag.gold-provider-receipt.v1",
            "source_generation_id": None,
            "source_generation_manifest": None,
            "source_serving_database": None,
        }
    )
    receipt_size = len(empty_receipt)
    request_count = 0
    maximum_envelope_size = maximum_response_bytes * 2
    for batch_index, start in enumerate(range(0, len(input_ids), batch_size)):
        response_name = f"qwen-response-{'f' * 24}-{batch_index:06d}.json"
        request_size = len(
            canonical_json_bytes(
                {
                    "idempotency_key": "f" * 64,
                    "input_ids": list(input_ids[start : start + batch_size]),
                    "ordinal": batch_index,
                    "request_body_sha256": "f" * 64,
                    "response_file_name": response_name,
                }
            )
        )
        artifact_size = len(
            canonical_json_bytes(
                {
                    "artifact": {
                        "sha256": "f" * 64,
                        "size_bytes": maximum_envelope_size,
                    },
                    "file_name": response_name,
                }
            )
        )
        separator_count = 0 if batch_index == 0 else 2
        receipt_size += request_size + artifact_size + separator_count
        request_count += 1
        if receipt_size > _MAX_MANIFEST_BYTES:
            raise GoldCaptureError("qwen_provider_receipt_size_invalid")
    return QwenProviderReceiptForecast(
        request_count=request_count,
        receipt_size_bytes=receipt_size,
    )


def _validate_git_evidence_size(
    size_bytes: int,
    *,
    code: str,
    expected_size_bytes: int | None = None,
) -> None:
    if (
        type(size_bytes) is not int
        or size_bytes < 1
        or size_bytes > MAX_GIT_EVIDENCE_FILE_BYTES
        or (
            expected_size_bytes is not None
            and (type(expected_size_bytes) is not int or size_bytes != expected_size_bytes)
        )
    ):
        raise GoldCaptureError(f"{code}_size_invalid")


def _predicted_f32_matrix_size(
    row_count: int,
    column_count: int,
    *,
    code: str,
) -> int:
    if (
        type(row_count) is not int
        or type(column_count) is not int
        or row_count < 1
        or column_count < 1
    ):
        raise GoldCaptureError(f"{code}_shape_invalid")
    size_bytes = row_count * column_count * 4
    _validate_git_evidence_size(
        size_bytes,
        code=code,
        expected_size_bytes=size_bytes,
    )
    return size_bytes


def _predicted_page_vector_size(row_count: int) -> int:
    if type(row_count) is not int or row_count < 1:
        raise GoldCaptureError("page_vectors_shape_invalid")
    size_bytes = row_count * QWEN3_EMBEDDING_DIMENSION * 4
    if size_bytes > _MAX_VECTOR_BYTES:
        raise GoldCaptureError("page_vectors_size_invalid")
    return size_bytes


def _forecast_page_vector_load(row_count: int) -> PageVectorLoadForecast:
    vector_size = _predicted_page_vector_size(row_count)
    block_size = min(row_count, 256) * QWEN3_EMBEDDING_DIMENSION * 4
    peak_working_set = _QWEN_REPLAY_FIXED_HEADROOM_BYTES + vector_size + block_size * 3
    if peak_working_set > _QWEN_REPLAY_WORKING_SET_LIMIT_BYTES:
        raise GoldCaptureError("page_vector_resource_limit_exceeded")
    return PageVectorLoadForecast(
        row_count=row_count,
        vector_size_bytes=vector_size,
        peak_working_set_bytes=peak_working_set,
    )


def _load_bounded_sqlite_metadata(
    connection: sqlite3.Connection,
    *,
    minimum_count: int,
    maximum_count: int,
    schema_code: str,
    mismatch_code: str,
) -> dict[str, str]:
    """Preflight metadata storage without materializing unbounded key/value cells."""

    try:
        summary = connection.execute(
            """SELECT count(*),
                      coalesce(sum(length(CAST(key AS BLOB))
                                   + length(CAST(value AS BLOB))),0),
                      coalesce(max(length(CAST(key AS BLOB))
                                   + length(CAST(value AS BLOB))),0),
                      coalesce(sum(CASE WHEN typeof(key)='text'
                                             AND typeof(value)='text'
                                        THEN 0 ELSE 1 END),0)
                 FROM metadata"""
        ).fetchone()
    except sqlite3.Error as exc:
        raise GoldCaptureError(schema_code) from exc
    if summary is None or any(type(value) is not int for value in summary):
        raise GoldCaptureError(schema_code)
    count, total_size, maximum_size, invalid_type_count = cast(tuple[int, int, int, int], summary)
    if (
        count < minimum_count
        or count > maximum_count
        or total_size < count
        or total_size > 64 * 1024
        or maximum_size < 1
        or maximum_size > 4 * 1024
        or invalid_type_count != 0
    ):
        raise GoldCaptureError(mismatch_code)
    try:
        metadata = {
            str(row[0]): str(row[1]) for row in connection.execute("SELECT key,value FROM metadata")
        }
    except sqlite3.Error as exc:
        raise GoldCaptureError(schema_code) from exc
    if len(metadata) != count:
        raise GoldCaptureError(mismatch_code)
    return metadata


def _validate_source_page_summary(
    *,
    page_count: int,
    current_revision_count: int,
    total_text_size_bytes: int,
    maximum_text_size_bytes: int,
) -> None:
    code = "page_source_resource_limit_exceeded"
    page_count = _validated_exact_int(page_count, minimum=1, maximum=None, code=code)
    current_revision_count = _validated_exact_int(
        current_revision_count,
        minimum=1,
        maximum=page_count,
        code=code,
    )
    total_text_size_bytes = _validated_exact_int(
        total_text_size_bytes,
        minimum=1,
        maximum=_SOURCE_PAGE_TOTAL_TEXT_LIMIT_BYTES,
        code=code,
    )
    maximum_text_size_bytes = _validated_exact_int(
        maximum_text_size_bytes,
        minimum=1,
        maximum=_SOURCE_PAGE_SINGLE_TEXT_LIMIT_BYTES,
        code=code,
    )
    summary_peak = (
        _QWEN_REPLAY_FIXED_HEADROOM_BYTES
        + maximum_text_size_bytes * 4
        + page_count * _SOURCE_PAGE_ROW_HEADROOM_BYTES
        + current_revision_count * _SOURCE_REVISION_HEADROOM_BYTES
    )
    if (
        maximum_text_size_bytes > total_text_size_bytes
        or page_count > total_text_size_bytes
        or summary_peak > _QWEN_REPLAY_WORKING_SET_LIMIT_BYTES
    ):
        raise GoldCaptureError(code)


def _forecast_source_page_chunks(
    *,
    chunk_count: int,
    chunk_text_size_bytes: int,
    maximum_source_page_size_bytes: int,
) -> SourcePageResourceForecast:
    code = "page_source_resource_limit_exceeded"
    chunk_count = _validated_exact_int(chunk_count, minimum=1, maximum=None, code=code)
    chunk_text_size_bytes = _validated_exact_int(
        chunk_text_size_bytes,
        minimum=1,
        maximum=_SOURCE_PAGE_TOTAL_TEXT_LIMIT_BYTES * 2,
        code=code,
    )
    maximum_source_page_size_bytes = _validated_exact_int(
        maximum_source_page_size_bytes,
        minimum=1,
        maximum=_SOURCE_PAGE_SINGLE_TEXT_LIMIT_BYTES,
        code=code,
    )
    vector_size = chunk_count * QWEN3_EMBEDDING_DIMENSION * 4
    retained_resident_size = (
        chunk_text_size_bytes * 4 + chunk_count * _PAGE_CHUNK_OBJECT_HEADROOM_BYTES
    )
    peak_working_set = (
        _QWEN_REPLAY_FIXED_HEADROOM_BYTES
        + maximum_source_page_size_bytes * 4
        + retained_resident_size
        + vector_size
    )
    if peak_working_set > _QWEN_REPLAY_WORKING_SET_LIMIT_BYTES:
        raise GoldCaptureError(code)
    return SourcePageResourceForecast(
        chunk_count=chunk_count,
        chunk_text_size_bytes=chunk_text_size_bytes,
        retained_resident_size_bytes=retained_resident_size,
        peak_working_set_bytes=peak_working_set,
    )


def _page_chunks_retained_resident_size(chunks: Sequence[PageChunk]) -> int:
    if not chunks:
        raise GoldCaptureError("page_chunk_identity_invalid")
    return sum(
        len(chunk.text.encode("utf-8")) * 4 + _PAGE_CHUNK_OBJECT_HEADROOM_BYTES for chunk in chunks
    )


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _validate_v109_release_anchor(
    *,
    release_gate: bool,
    expected_run_id: str | None,
    expected_generation_id: str | None,
    expected_manifest_sha256: str | None,
    expected_database_sha256: str | None,
    generation_id: str,
    generation_manifest: ArtifactBinding,
    serving_database: ArtifactBinding,
    expected_publish_sha256: str | None = None,
    publish_artifact: ArtifactBinding | None = None,
) -> None:
    if not release_gate:
        return
    if (
        expected_run_id != V109_PRESERVED_RUN_ID
        or expected_generation_id != V109_PRESERVED_GENERATION_ID
        or expected_manifest_sha256 != V109_PRESERVED_MANIFEST_SHA256
        or expected_database_sha256 != V109_PRESERVED_DATABASE_SHA256
        or generation_id != V109_PRESERVED_GENERATION_ID
        or generation_manifest
        != ArtifactBinding(
            sha256=V109_PRESERVED_MANIFEST_SHA256,
            size_bytes=V109_PRESERVED_MANIFEST_SIZE_BYTES,
        )
        or serving_database
        != ArtifactBinding(
            sha256=V109_PRESERVED_DATABASE_SHA256,
            size_bytes=V109_PRESERVED_DATABASE_SIZE_BYTES,
        )
    ):
        raise GoldCaptureError("v109_preserved_source_anchor_mismatch")
    if publish_artifact is not None or expected_publish_sha256 is not None:
        if (
            expected_publish_sha256 != V109_PRESERVED_PUBLISH_SHA256
            or publish_artifact
            != ArtifactBinding(
                sha256=V109_PRESERVED_PUBLISH_SHA256,
                size_bytes=V109_PRESERVED_PUBLISH_SIZE_BYTES,
            )
        ):
            raise GoldCaptureError("v109_preserved_publish_anchor_mismatch")


def _provider_request_body(
    *,
    model: Literal["openai/text-embedding-3-small", "qwen/qwen3-embedding-8b"],
    provider_id: Literal["deepinfra", "nebius"] | None,
    formatted_inputs: Sequence[str],
) -> dict[str, Any]:
    if not formatted_inputs or len(formatted_inputs) > 128:
        raise GoldCaptureError("provider_request_input_count_invalid")
    body: dict[str, Any] = {
        "model": model,
        "input": list(formatted_inputs),
        "dimensions": 1536 if model == "openai/text-embedding-3-small" else 4096,
        "encoding_format": "float",
    }
    if model == "openai/text-embedding-3-small":
        if provider_id is not None or len(formatted_inputs) != 1:
            raise GoldCaptureError("provider_request_profile_invalid")
    elif provider_id not in {"deepinfra", "nebius"}:
        raise GoldCaptureError("provider_request_profile_invalid")
    else:
        body["provider"] = {
            "order": [provider_id],
            "only": [provider_id],
            "allow_fallbacks": False,
            "require_parameters": False,
        }
    return body


def _provider_request_record(
    *,
    ordinal: int,
    input_ids: Sequence[str],
    request_body: Mapping[str, Any],
    response_file_name: str,
) -> ProviderRequestRecord:
    body_sha256 = canonical_sha256(request_body)
    reservation = {
        "input_ids": list(input_ids),
        "ordinal": ordinal,
        "request_body_sha256": body_sha256,
        "response_file_name": response_file_name,
        "schema_version": "cardrag.gold-provider-request-reservation.v1",
    }
    return ProviderRequestRecord(
        ordinal=ordinal,
        input_ids=tuple(input_ids),
        request_body_sha256=body_sha256,
        response_file_name=response_file_name,
        idempotency_key=canonical_sha256(reservation),
    )


def _provider_request_contract_sha256(
    *,
    model: Literal["openai/text-embedding-3-small", "qwen/qwen3-embedding-8b"],
    provider_id: Literal["deepinfra", "nebius"] | None,
    requests: Sequence[ProviderRequestRecord],
    input_count: int,
    source_generation_id: str | None = None,
    source_generation_manifest: ArtifactBinding | None = None,
    source_serving_database: ArtifactBinding | None = None,
) -> str:
    return canonical_sha256(
        {
            "base_url": _OFFICIAL_OPENROUTER_API_BASE_URL,
            "input_count": input_count,
            "model": model,
            "provider": "openrouter",
            "provider_id": provider_id,
            "requests": [item.model_dump(mode="json") for item in requests],
            "source_generation_id": source_generation_id,
            "source_generation_manifest": source_generation_manifest,
            "source_serving_database": source_serving_database,
            "schema_version": "cardrag.gold-provider-request-contract.v1",
        }
    )


def _load_response_envelope(
    path: Path,
    *,
    maximum_bytes: int,
    code: str,
) -> tuple[EmbeddingRawResponseEnvelope, bytes]:
    payload = _read_regular(path, maximum_bytes=maximum_bytes, code=code)
    try:
        envelope = EmbeddingRawResponseEnvelope.model_validate_json(payload)
    except ValidationError as exc:
        raise GoldCaptureError(f"{code}_invalid") from exc
    if payload != envelope.canonical_bytes():
        raise GoldCaptureError(f"{code}_not_canonical")
    return envelope, payload


def _load_provider_receipt(
    path: Path,
    expected: ArtifactBinding,
) -> ProviderReceipt:
    payload = _read_regular(path, maximum_bytes=_MAX_MANIFEST_BYTES, code="provider_receipt")
    if _binding_from_bytes(payload) != expected:
        raise GoldCaptureError("provider_receipt_binding_mismatch")
    try:
        receipt = ProviderReceipt.model_validate_json(payload)
    except ValidationError as exc:
        raise GoldCaptureError("provider_receipt_invalid") from exc
    if payload != receipt.canonical_bytes():
        raise GoldCaptureError("provider_receipt_not_canonical")
    return receipt


def _decode_vector(row: EmbeddingReplayRow, *, dimension: int) -> npt.NDArray[np.float32]:
    try:
        raw = base64.b64decode(row.vector_f32_base64, validate=True)
    except (TypeError, ValueError) as exc:
        raise GoldCaptureError("embedding_replay_vector_base64_invalid") from exc
    if (
        base64.b64encode(raw).decode("ascii") != row.vector_f32_base64
        or len(raw) != dimension * 4
        or hashlib.sha256(raw).hexdigest() != row.vector_f32_sha256
    ):
        raise GoldCaptureError("embedding_replay_vector_binding_mismatch")
    vector = np.frombuffer(raw, dtype="<f4", count=dimension)
    if not bool(np.isfinite(vector).all()):
        raise GoldCaptureError("embedding_replay_vector_non_finite")
    norm = float(np.linalg.norm(vector))
    if not math.isfinite(norm) or not math.isclose(
        norm,
        1.0,
        rel_tol=_NORM_TOLERANCE,
        abs_tol=_NORM_TOLERANCE,
    ):
        raise GoldCaptureError("embedding_replay_vector_not_normalized")
    return vector


def load_embedding_replay(
    path: Path,
    *,
    provider_receipt_path: Path,
    lane: EmbeddingLane,
    input_kind: EmbeddingKind,
    source_commit: str,
    embedding_profile_id: str,
    expected_inputs: Sequence[tuple[str, str]],
    expected_source_generation_id: str | None = None,
    expected_source_generation_manifest: ArtifactBinding | None = None,
    expected_source_serving_database: ArtifactBinding | None = None,
    retained_resident_size_bytes: int = 0,
) -> tuple[EmbeddingReplayManifest, npt.NDArray[np.float32], ArtifactBinding]:
    """Load exact ordered input/vector pairs and bind their upstream receipt."""

    retained_resident_size_bytes = _validated_exact_int(
        retained_resident_size_bytes,
        minimum=0,
        maximum=_MAX_EMBEDDING_REPLAY_BYTES,
        code="embedding_replay_retained_resident_size_invalid",
    )
    with _CanonicalJsonlReader(
        path,
        maximum_bytes=_MAX_EMBEDDING_REPLAY_BYTES,
        code="embedding_replay",
    ) as reader:
        raw_manifest = reader.next_record()
        try:
            manifest = EmbeddingReplayManifest.model_validate(raw_manifest)
        except ValidationError as exc:
            raise GoldCaptureError("embedding_replay_manifest_invalid", line=1) from exc
        if (
            raw_manifest is None
            or manifest.lane != lane
            or manifest.input_kind != input_kind
            or manifest.source_commit != source_commit
            or manifest.embedding_profile_id != embedding_profile_id
            or manifest.record_count != len(expected_inputs)
            or manifest.provider_receipt is None
        ):
            raise GoldCaptureError("embedding_replay_manifest_binding_mismatch")
        receipt = _load_provider_receipt(
            provider_receipt_path,
            manifest.provider_receipt,
        )
        if (
            receipt.model != manifest.embedding_model
            or receipt.input_count != manifest.record_count
            or (manifest.lane == "qwen_page" and receipt.provider_id not in {"deepinfra", "nebius"})
            or (manifest.lane == "v109_baseline" and receipt.provider_id is not None)
            or receipt.source_generation_id != expected_source_generation_id
            or receipt.source_generation_manifest != expected_source_generation_manifest
            or receipt.source_serving_database != expected_source_serving_database
        ):
            raise GoldCaptureError("provider_receipt_profile_mismatch")
        if manifest.lane == "qwen_page":
            listed = reader.before
            if listed is None:  # pragma: no cover - reader invariant
                raise GoldCaptureError("embedding_replay_reader_state_invalid")
            forecast = _forecast_qwen_replay_resources(
                record_count=manifest.record_count,
                dimension=manifest.embedding_dimension,
                input_resident_size_bytes=_qwen_streaming_replay_resident_size(
                    manifest=manifest,
                    expected_inputs=expected_inputs,
                    retained_resident_size_bytes=retained_resident_size_bytes,
                ),
                maximum_response_bytes=_MAX_PROVIDER_RESPONSE_BYTES,
                batch_size=max(len(request.input_ids) for request in receipt.requests),
                manifest_line_size_bytes=len(manifest.canonical_bytes()) + 1,
                input_ids=tuple(expected_id for expected_id, _formatted in expected_inputs),
            )
            if listed.st_size != forecast.replay_size_bytes:
                raise GoldCaptureError("embedding_replay_size_forecast_mismatch")
        try:
            matrix = np.empty((len(expected_inputs), manifest.embedding_dimension), dtype="<f4")
        except (MemoryError, ValueError) as exc:
            raise GoldCaptureError("embedding_replay_matrix_allocation_failed") from exc
        for ordinal, (expected_id, formatted) in enumerate(expected_inputs):
            raw = reader.next_record()
            if raw is None:
                raise GoldCaptureError("embedding_replay_manifest_binding_mismatch")
            try:
                row = EmbeddingReplayRow.model_validate(raw)
            except ValidationError as exc:
                raise GoldCaptureError("embedding_replay_row_invalid", line=ordinal + 2) from exc
            if (
                row.ordinal != ordinal
                or row.input_id != expected_id
                or row.formatted_input_sha256
                != hashlib.sha256(formatted.encode("utf-8")).hexdigest()
            ):
                raise GoldCaptureError("embedding_replay_input_binding_mismatch", line=ordinal + 2)
            matrix[ordinal] = _decode_vector(row, dimension=manifest.embedding_dimension)
        if reader.next_record() is not None:
            raise GoldCaptureError("embedding_replay_manifest_binding_mismatch")
        replay_binding = reader.binding
    _validate_provider_evidence(
        provider_receipt_path=provider_receipt_path,
        receipt=receipt,
        lane=lane,
        input_kind=input_kind,
        expected_inputs=expected_inputs,
        replay_matrix=matrix,
    )
    return manifest, matrix, replay_binding


def _load_qwen_token_counter(path: Path) -> Callable[[str], int]:
    binding = _hash_regular(
        path,
        maximum_bytes=QWEN_TOKENIZER_SIZE_BYTES,
        code="qwen_tokenizer",
    )
    if binding.sha256 != QWEN_TOKENIZER_SHA256 or binding.size_bytes != QWEN_TOKENIZER_SIZE_BYTES:
        raise GoldCaptureError("qwen_tokenizer_binding_mismatch")
    try:
        from tokenizers import Tokenizer

        tokenizer = Tokenizer.from_file(str(path))
    except Exception as exc:
        raise GoldCaptureError("qwen_tokenizer_load_failed") from exc

    def count(value: str) -> int:
        if not value.strip():
            raise GoldCaptureError("qwen_tokenizer_input_blank")
        try:
            result = len(tokenizer.encode(value, add_special_tokens=True).ids)
        except Exception as exc:
            raise GoldCaptureError("qwen_tokenizer_count_failed") from exc
        if result < 1:
            raise GoldCaptureError("qwen_tokenizer_count_invalid")
        return result

    return count


def _qwen_input_payload(
    *,
    source_commit: str,
    input_kind: EmbeddingKind,
    embedding_profile_id: str,
    provider_id: Literal["deepinfra", "nebius"],
    maximum_tokens: int,
    values: Sequence[tuple[str, str]],
    token_counter: Callable[[str], int],
) -> bytes:
    maximum_tokens = _validated_exact_int(
        maximum_tokens,
        minimum=1,
        maximum=None,
        code="qwen_embedding_input_limits_invalid",
    )
    manifest = _qwen_input_manifest(
        source_commit=source_commit,
        input_kind=input_kind,
        embedding_profile_id=embedding_profile_id,
        provider_id=provider_id,
        maximum_tokens=maximum_tokens,
        record_count=len(values),
    )
    if len({input_id for input_id, _formatted in values}) != len(values):
        raise GoldCaptureError("qwen_embedding_input_id_duplicate")
    records: list[BaseModel] = [manifest]
    for ordinal, (input_id, formatted) in enumerate(values):
        token_count = token_counter(formatted)
        if (
            isinstance(token_count, bool)
            or not isinstance(token_count, int)
            or token_count < 1
            or token_count > maximum_tokens
        ):
            raise GoldCaptureError("qwen_embedding_input_token_limit_exceeded")
        records.append(
            QwenEmbeddingInputRow(
                schema_version="cardrag.gold-qwen-embedding-input-row.v1",
                ordinal=ordinal,
                input_id=input_id,
                formatted_input_sha256=hashlib.sha256(formatted.encode("utf-8")).hexdigest(),
                formatted_input=formatted,
                token_count=token_count,
            )
        )
    return _jsonl_bytes(records)


def _qwen_input_manifest(
    *,
    source_commit: str,
    input_kind: EmbeddingKind,
    embedding_profile_id: str,
    provider_id: Literal["deepinfra", "nebius"],
    maximum_tokens: int,
    record_count: int,
) -> QwenEmbeddingInputManifest:
    return QwenEmbeddingInputManifest(
        schema_version="cardrag.gold-qwen-embedding-input.v1",
        lane="qwen_page",
        input_kind=input_kind,
        source_commit=_validated_source_commit(source_commit),
        embedding_model=QWEN3_EMBEDDING_MODEL,
        embedding_dimension=4096,
        embedding_profile_id=embedding_profile_id,
        provider_id=provider_id,
        maximum_tokens=maximum_tokens,
        tokenizer_revision=QWEN_TOKENIZER_REVISION,
        tokenizer_sha256=QWEN_TOKENIZER_SHA256,
        record_count=record_count,
    )


def _forecast_qwen_page_input_size(
    *,
    manifest: QwenEmbeddingInputManifest,
    chunks: Sequence[PageChunk],
) -> int:
    if manifest.input_kind != "document" or manifest.record_count != len(chunks):
        raise GoldCaptureError("qwen_embedding_input_manifest_binding_mismatch")
    input_size = len(manifest.canonical_bytes()) + 1
    input_ids: list[str] = []
    seen_input_ids: set[str] = set()
    for ordinal, chunk in enumerate(chunks):
        if chunk.chunk_id in seen_input_ids:
            raise GoldCaptureError("qwen_embedding_input_id_duplicate")
        seen_input_ids.add(chunk.chunk_id)
        formatted = format_qwen3_document(chunk.text)
        input_ids.append(chunk.chunk_id)
        input_size += (
            len(
                canonical_json_bytes(
                    {
                        "formatted_input": formatted,
                        "formatted_input_sha256": hashlib.sha256(
                            formatted.encode("utf-8")
                        ).hexdigest(),
                        "input_id": chunk.chunk_id,
                        "ordinal": ordinal,
                        "schema_version": "cardrag.gold-qwen-embedding-input-row.v1",
                        "token_count": manifest.maximum_tokens,
                    }
                )
            )
            + 1
        )
        if input_size > _MAX_EMBEDDING_REPLAY_BYTES:
            raise GoldCaptureError("qwen_embedding_input_size_invalid")
    dummy_receipt = ArtifactBinding(sha256="f" * 64, size_bytes=_MAX_MANIFEST_BYTES)
    replay_manifest = _qwen_replay_manifest(manifest, provider_receipt=dummy_receipt)
    _forecast_qwen_replay_resources(
        record_count=manifest.record_count,
        dimension=manifest.embedding_dimension,
        input_resident_size_bytes=input_size,
        maximum_response_bytes=_MAX_PROVIDER_RESPONSE_BYTES,
        batch_size=16,
        manifest_line_size_bytes=len(replay_manifest.canonical_bytes()) + 1,
        input_ids=input_ids,
    )
    _forecast_qwen_provider_receipt_size(
        input_ids=input_ids,
        provider_id=manifest.provider_id,
        batch_size=16,
        maximum_response_bytes=_MAX_PROVIDER_RESPONSE_BYTES,
    )
    return input_size


def _publish_qwen_page_inputs(
    *,
    manifest: QwenEmbeddingInputManifest,
    chunks: Sequence[PageChunk],
    token_counter: Callable[[str], int],
    output_path: Path,
    maximum_size_bytes: int,
) -> ArtifactBinding:
    output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".qwen-page-inputs-",
        dir=output_path.parent,
    )
    temporary = Path(temporary_name)
    written = 0
    try:
        with os.fdopen(descriptor, "wb") as output:
            manifest_line = manifest.canonical_bytes() + b"\n"
            output.write(manifest_line)
            written += len(manifest_line)
            for ordinal, chunk in enumerate(chunks):
                formatted = format_qwen3_document(chunk.text)
                token_count = token_counter(formatted)
                if (
                    isinstance(token_count, bool)
                    or not isinstance(token_count, int)
                    or token_count < 1
                    or token_count > manifest.maximum_tokens
                ):
                    raise GoldCaptureError("qwen_embedding_input_token_limit_exceeded")
                row = QwenEmbeddingInputRow(
                    schema_version="cardrag.gold-qwen-embedding-input-row.v1",
                    ordinal=ordinal,
                    input_id=chunk.chunk_id,
                    formatted_input_sha256=hashlib.sha256(formatted.encode("utf-8")).hexdigest(),
                    formatted_input=formatted,
                    token_count=token_count,
                )
                line = row.canonical_bytes() + b"\n"
                output.write(line)
                written += len(line)
                if written > maximum_size_bytes:
                    raise GoldCaptureError("qwen_embedding_input_size_forecast_mismatch")
            output.flush()
            os.fsync(output.fileno())
        return _publish_built_file(
            temporary,
            output_path,
            maximum_bytes=maximum_size_bytes,
            code="qwen_embedding_inputs",
        )
    finally:
        temporary.unlink(missing_ok=True)


def prepare_qwen_page_embedding_inputs(
    *,
    source_generation_manifest_path: Path,
    source_database_path: Path,
    source_commit: str,
    embedding_profile_id: str,
    provider_id: Literal["deepinfra", "nebius"],
    maximum_tokens: int,
    output_path: Path,
    tokenizer_path: Path | None = None,
    token_counter: Callable[[str], int] | None = None,
) -> ArtifactBinding:
    """Seal exact page-chunk document inputs before any provider request."""

    maximum_tokens = _validated_exact_int(
        maximum_tokens,
        minimum=1,
        maximum=None,
        code="qwen_embedding_input_limits_invalid",
    )
    source_manifest, _source_binding = _load_generation_manifest(source_generation_manifest_path)
    _validate_source_qwen_profile(
        source_manifest,
        embedding_profile_id=embedding_profile_id,
        provider_id=provider_id,
        maximum_tokens=maximum_tokens,
    )
    chunks = _source_page_chunks(
        generation_manifest=source_manifest,
        database_path=source_database_path,
    )
    manifest = _qwen_input_manifest(
        source_commit=source_commit,
        input_kind="document",
        embedding_profile_id=embedding_profile_id,
        provider_id=provider_id,
        maximum_tokens=maximum_tokens,
        record_count=len(chunks),
    )
    maximum_size_bytes = _forecast_qwen_page_input_size(
        manifest=manifest,
        chunks=chunks,
    )
    if token_counter is None:
        if tokenizer_path is None:
            raise GoldCaptureError("qwen_tokenizer_required")
        token_counter = _load_qwen_token_counter(tokenizer_path)
    return _publish_qwen_page_inputs(
        manifest=manifest,
        chunks=chunks,
        token_counter=token_counter,
        output_path=output_path,
        maximum_size_bytes=maximum_size_bytes,
    )


def prepare_qwen_query_embedding_inputs(
    *,
    gold_path: Path,
    expected_gold_sha256: str,
    source_commit: str,
    embedding_profile_id: str,
    provider_id: Literal["deepinfra", "nebius"],
    maximum_tokens: int,
    output_path: Path,
    tokenizer_path: Path | None = None,
    token_counter: Callable[[str], int] | None = None,
    release_gate: bool = True,
) -> ArtifactBinding:
    """Seal exact instructed gold-query inputs before any provider request."""

    maximum_tokens = _validated_exact_int(
        maximum_tokens,
        minimum=1,
        maximum=None,
        code="qwen_embedding_input_limits_invalid",
    )
    if token_counter is None:
        if tokenizer_path is None:
            raise GoldCaptureError("qwen_tokenizer_required")
        token_counter = _load_qwen_token_counter(tokenizer_path)
    gold = load_gold_jsonl(gold_path, release_gate=release_gate)
    if gold.sha256 != expected_gold_sha256:
        raise GoldCaptureError("gold_sha256_mismatch")
    payload = _qwen_input_payload(
        source_commit=source_commit,
        input_kind="query",
        embedding_profile_id=embedding_profile_id,
        provider_id=provider_id,
        maximum_tokens=maximum_tokens,
        values=tuple(
            (query.query_id, format_qwen3_query(query.question)) for query in gold.queries
        ),
        token_counter=token_counter,
    )
    return _publish_immutable(output_path, payload)


def _load_qwen_inputs(
    path: Path,
    *,
    batch_size: int,
    maximum_response_bytes: int,
) -> tuple[
    QwenEmbeddingInputManifest,
    tuple[QwenEmbeddingInputRow, ...],
    ArtifactBinding,
]:
    with _CanonicalJsonlReader(
        path,
        maximum_bytes=_MAX_EMBEDDING_REPLAY_BYTES,
        code="qwen_embedding_inputs",
    ) as reader:
        raw_manifest = reader.next_record()
        try:
            manifest = QwenEmbeddingInputManifest.model_validate(raw_manifest)
        except ValidationError as exc:
            raise GoldCaptureError("qwen_embedding_input_manifest_invalid") from exc
        if raw_manifest is None or canonical_json_bytes(raw_manifest) != manifest.canonical_bytes():
            raise GoldCaptureError("qwen_embedding_input_manifest_binding_mismatch")
        listed = reader.before
        if listed is None:  # pragma: no cover - reader invariant
            raise GoldCaptureError("qwen_embedding_input_reader_state_invalid")
        forecast_manifest = _qwen_replay_manifest(
            manifest,
            provider_receipt=ArtifactBinding(
                sha256="f" * 64,
                size_bytes=_MAX_MANIFEST_BYTES,
            ),
        )
        _forecast_qwen_replay_resources(
            record_count=manifest.record_count,
            dimension=manifest.embedding_dimension,
            input_resident_size_bytes=listed.st_size,
            maximum_response_bytes=maximum_response_bytes,
            batch_size=batch_size,
            manifest_line_size_bytes=len(forecast_manifest.canonical_bytes()) + 1,
        )
        rows: list[QwenEmbeddingInputRow] = []
        for ordinal in range(manifest.record_count):
            raw = reader.next_record()
            if raw is None:
                raise GoldCaptureError("qwen_embedding_input_manifest_binding_mismatch")
            try:
                row = QwenEmbeddingInputRow.model_validate(raw)
            except ValidationError as exc:
                raise GoldCaptureError(
                    "qwen_embedding_input_row_invalid",
                    line=ordinal + 2,
                ) from exc
            if row.ordinal != ordinal:
                raise GoldCaptureError(
                    "qwen_embedding_input_row_binding_mismatch",
                    line=ordinal + 2,
                )
            rows.append(row)
        if reader.next_record() is not None:
            raise GoldCaptureError("qwen_embedding_input_manifest_binding_mismatch")
        binding = reader.binding
    if len({row.input_id for row in rows}) != len(rows):
        raise GoldCaptureError("qwen_embedding_input_id_duplicate")
    return manifest, tuple(rows), binding


async def _bounded_provider_body(
    response: httpx.Response,
    *,
    maximum_bytes: int,
) -> bytes:
    maximum_bytes = _validated_provider_response_limit(
        maximum_bytes,
        code="provider_response_limit_invalid",
    )
    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            declared_size = int(declared) if declared.isascii() and declared.isdigit() else -1
        except ValueError:
            declared_size = -1
        if declared_size < 0 or declared_size > maximum_bytes:
            raise GoldCaptureError("qwen_provider_content_length_invalid")
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(chunk) > maximum_bytes - len(body):
            raise GoldCaptureError("qwen_provider_response_too_large")
        body.extend(chunk)
    if not body:
        raise GoldCaptureError("qwen_provider_response_empty")
    return bytes(body)


def _provider_name(payload: Mapping[str, Any], header: str | None) -> str | None:
    for candidate in (payload.get("provider_id"), payload.get("provider")):
        if isinstance(candidate, str) and candidate:
            return candidate
    metadata = payload.get("openrouter_metadata")
    if isinstance(metadata, Mapping):
        for key in ("provider_slug", "provider_id", "provider_name", "provider"):
            candidate = metadata.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
    return header


def _validated_external_openrouter_base_url(
    raw_value: str,
) -> Literal["https://openrouter.ai/api/v1"]:
    """Allow bearer credentials only to the exact official API origin/path."""

    validated = _validated_openrouter_base_url(raw_value)
    if validated != _OFFICIAL_OPENROUTER_API_BASE_URL:
        raise GoldCaptureError("openrouter_base_url_invalid")
    return _OFFICIAL_OPENROUTER_API_BASE_URL


def _validate_injected_openrouter_client(client: httpx.AsyncClient) -> None:
    try:
        base_url = str(client.base_url).rstrip("/")
        follows_redirects = bool(client.follow_redirects)
    except Exception as exc:
        raise GoldCaptureError("openrouter_injected_client_invalid") from exc
    if base_url != _OFFICIAL_OPENROUTER_API_BASE_URL or follows_redirects:
        raise GoldCaptureError("openrouter_injected_client_invalid")


def _provider_response_contains_secret(
    *,
    body: bytes,
    header_values: Sequence[str],
    secret: str,
) -> bool:
    return secret.encode("utf-8") in body or any(secret in value for value in header_values)


def _envelope_contains_secret(envelope: EmbeddingRawResponseEnvelope, secret: str) -> bool:
    return _provider_response_contains_secret(
        body=base64.b64decode(envelope.body_base64, validate=True),
        header_values=() if envelope.provider_header is None else (envelope.provider_header,),
        secret=secret,
    )


def _normalize_qwen_query_vector(values: object) -> npt.NDArray[np.float32]:
    """Match the current MCP query embedder's one-pass float32 semantics."""

    try:
        vector = np.asarray(values, dtype=np.float32)
    except (TypeError, ValueError) as exc:
        raise GoldCaptureError("qwen_provider_response_vector_invalid") from exc
    if vector.shape != (QWEN3_EMBEDDING_DIMENSION,) or not bool(np.isfinite(vector).all()):
        raise GoldCaptureError("qwen_provider_response_vector_invalid")
    norm = np.linalg.norm(vector)
    if not bool(np.isfinite(norm)) or float(norm) <= 1e-12:
        raise GoldCaptureError("qwen_provider_response_vector_invalid")
    return np.asarray(vector / norm, dtype=np.float32)


def _normalize_qwen_document_vector(values: object) -> npt.NDArray[np.float32]:
    """Match Worker provider normalization plus its float32 cache encoding."""

    if (
        not isinstance(values, Sequence)
        or isinstance(values, (str, bytes, bytearray))
        or len(values) != QWEN3_EMBEDDING_DIMENSION
        or any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values)
    ):
        raise GoldCaptureError("qwen_provider_response_vector_invalid")
    converted = tuple(float(value) for value in values)
    if not all(math.isfinite(value) for value in converted):
        raise GoldCaptureError("qwen_provider_response_vector_invalid")
    provider_norm = math.sqrt(sum(value * value for value in converted))
    if not math.isfinite(provider_norm) or provider_norm <= 1e-12:
        raise GoldCaptureError("qwen_provider_response_vector_invalid")
    provider_normalized = tuple(value / provider_norm for value in converted)
    if not math.isclose(
        sum(value * value for value in provider_normalized),
        1.0,
        rel_tol=_NORM_TOLERANCE,
        abs_tol=_NORM_TOLERANCE,
    ):
        raise GoldCaptureError("qwen_provider_response_vector_invalid")
    cache_norm = math.sqrt(sum(value * value for value in provider_normalized))
    if not math.isfinite(cache_norm) or cache_norm <= 1e-12:
        raise GoldCaptureError("qwen_provider_response_vector_invalid")
    packed = struct.pack(
        f"<{QWEN3_EMBEDDING_DIMENSION}f",
        *(value / cache_norm for value in provider_normalized),
    )
    return np.frombuffer(packed, dtype="<f4").copy()


def _parse_qwen_response(
    envelope: EmbeddingRawResponseEnvelope,
    *,
    provider_id: Literal["deepinfra", "nebius"],
    input_kind: EmbeddingKind,
    expected_count: int,
) -> npt.NDArray[np.float32]:
    raw = base64.b64decode(envelope.body_base64, validate=True)
    payload = _strict_provider_json_object(raw, code="qwen_provider_response")
    actual_model = payload.get("model")
    actual_provider = _provider_name(payload, envelope.provider_header)
    data = payload.get("data")
    if (
        not isinstance(actual_model, str)
        or actual_model.casefold() != QWEN3_EMBEDDING_MODEL.casefold()
        or actual_provider is None
        or not _provider_matches(actual_provider, provider_id)
        or not isinstance(data, list)
        or len(data) != expected_count
    ):
        raise GoldCaptureError("qwen_provider_response_profile_mismatch")
    indexed: dict[int, npt.NDArray[np.float32]] = {}
    for value in data:
        if not isinstance(value, Mapping):
            raise GoldCaptureError("qwen_provider_response_row_invalid")
        index = value.get("index")
        embedding = value.get("embedding")
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or index in indexed
            or not isinstance(embedding, Sequence)
            or isinstance(embedding, (str, bytes, bytearray))
            or len(embedding) != QWEN3_EMBEDDING_DIMENSION
        ):
            raise GoldCaptureError("qwen_provider_response_row_invalid")
        indexed[index] = (
            _normalize_qwen_query_vector(embedding)
            if input_kind == "query"
            else _normalize_qwen_document_vector(embedding)
        )
    if set(indexed) != set(range(expected_count)):
        raise GoldCaptureError("qwen_provider_response_indices_invalid")
    return np.stack([indexed[index] for index in range(expected_count)]).astype("<f4")


def _publish_qwen_replay_from_responses(
    *,
    source: QwenEmbeddingInputManifest,
    rows: Sequence[QwenEmbeddingInputRow],
    receipt: ProviderReceipt,
    receipt_path: Path,
    receipt_binding: ArtifactBinding,
    replay_output_path: Path,
    forecast: QwenReplayResourceForecast,
) -> ArtifactBinding:
    replay_manifest = _qwen_replay_manifest(source, provider_receipt=receipt_binding)
    replay_output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".qwen-replay-",
        dir=replay_output_path.parent,
    )
    temporary = Path(temporary_name)
    written = 0
    offset = 0
    try:
        with os.fdopen(descriptor, "wb") as output:
            manifest_line = replay_manifest.canonical_bytes() + b"\n"
            output.write(manifest_line)
            written += len(manifest_line)
            for request, response_artifact in zip(
                receipt.requests,
                receipt.response_artifacts,
                strict=True,
            ):
                envelope, envelope_payload = _load_response_envelope(
                    receipt_path.parent / response_artifact.file_name,
                    maximum_bytes=_MAX_PROVIDER_RESPONSE_BYTES * 2,
                    code="qwen_provider_response_envelope",
                )
                if _binding_from_bytes(envelope_payload) != response_artifact.artifact:
                    raise GoldCaptureError("provider_response_artifact_binding_mismatch")
                count = len(request.input_ids)
                selected_rows = rows[offset : offset + count]
                if (
                    len(selected_rows) != count
                    or tuple(row.input_id for row in selected_rows) != request.input_ids
                ):
                    raise GoldCaptureError("provider_request_input_binding_mismatch")
                parsed = _parse_qwen_response(
                    envelope,
                    provider_id=source.provider_id,
                    input_kind=source.input_kind,
                    expected_count=count,
                )
                for row, vector in zip(selected_rows, parsed, strict=True):
                    raw = vector.astype("<f4", copy=False).tobytes()
                    replay_row = EmbeddingReplayRow(
                        schema_version=EMBEDDING_REPLAY_ROW_SCHEMA,
                        ordinal=offset,
                        input_id=row.input_id,
                        formatted_input_sha256=row.formatted_input_sha256,
                        vector_f32_sha256=hashlib.sha256(raw).hexdigest(),
                        vector_f32_base64=base64.b64encode(raw).decode("ascii"),
                    )
                    line = replay_row.canonical_bytes() + b"\n"
                    output.write(line)
                    written += len(line)
                    if written > forecast.replay_size_bytes:
                        raise GoldCaptureError("qwen_embedding_replay_size_forecast_mismatch")
                    offset += 1
            if offset != len(rows) or written != forecast.replay_size_bytes:
                raise GoldCaptureError("qwen_embedding_replay_size_forecast_mismatch")
            output.flush()
            os.fsync(output.fileno())
        return _publish_built_file(
            temporary,
            replay_output_path,
            maximum_bytes=forecast.replay_size_bytes,
            code="qwen_embedding_replay",
        )
    finally:
        temporary.unlink(missing_ok=True)


async def capture_qwen_embedding_replay(
    *,
    input_path: Path,
    openrouter_base_url: str,
    openrouter_api_key_file: Path,
    provider_receipt_output_path: Path,
    replay_output_path: Path,
    state_directory: Path,
    tokenizer_path: Path | None = None,
    batch_size: int = 16,
    timeout_seconds: float = 120.0,
    maximum_response_bytes: int = _MAX_PROVIDER_RESPONSE_BYTES,
    client: httpx.AsyncClient | None = None,
    token_counter: Callable[[str], int] | None = None,
) -> tuple[ArtifactBinding, ArtifactBinding]:
    """Capture pinned-provider Qwen vectors with raw-response, crash-safe replay."""

    base_url = _validated_external_openrouter_base_url(openrouter_base_url)
    code = "qwen_provider_capture_limits_invalid"
    batch_size = _validated_exact_int(
        batch_size,
        minimum=1,
        maximum=_MAX_QWEN_BATCH_SIZE,
        code=code,
    )
    timeout_seconds = _validated_provider_timeout(timeout_seconds, code=code)
    maximum_response_bytes = _validated_provider_response_limit(
        maximum_response_bytes,
        code=code,
    )
    manifest, rows, input_binding = _load_qwen_inputs(
        input_path,
        batch_size=batch_size,
        maximum_response_bytes=maximum_response_bytes,
    )
    forecast_manifest = _qwen_replay_manifest(
        manifest,
        provider_receipt=ArtifactBinding(
            sha256="f" * 64,
            size_bytes=_MAX_MANIFEST_BYTES,
        ),
    )
    _forecast_qwen_replay_resources(
        record_count=manifest.record_count,
        dimension=manifest.embedding_dimension,
        input_resident_size_bytes=input_binding.size_bytes,
        maximum_response_bytes=maximum_response_bytes,
        batch_size=batch_size,
        manifest_line_size_bytes=len(forecast_manifest.canonical_bytes()) + 1,
        input_ids=tuple(row.input_id for row in rows),
    )
    receipt_forecast = _forecast_qwen_provider_receipt_size(
        input_ids=tuple(row.input_id for row in rows),
        provider_id=manifest.provider_id,
        batch_size=batch_size,
        maximum_response_bytes=maximum_response_bytes,
    )
    if token_counter is None:
        if tokenizer_path is None:
            raise GoldCaptureError("qwen_tokenizer_required")
        token_counter = _load_qwen_token_counter(tokenizer_path)
    for row in rows:
        actual_count = token_counter(row.formatted_input)
        if actual_count != row.token_count or actual_count > manifest.maximum_tokens:
            raise GoldCaptureError("qwen_embedding_input_token_binding_mismatch")
    if client is not None:
        _validate_injected_openrouter_client(client)
    key = _read_secret(openrouter_api_key_file)
    capture_id = canonical_sha256(
        {
            "base_url": base_url,
            "batch_size": batch_size,
            "input_artifact": input_binding.model_dump(mode="json"),
            "profile_id": manifest.embedding_profile_id,
            "schema_version": "cardrag.gold-qwen-provider-request.v1",
        }
    )
    state_root = _safe_private_state_directory(state_directory)
    state = _safe_private_state_directory(state_root / capture_id)
    identity_path = state / "identity.json"
    identity_payload = canonical_json_bytes(
        {
            "capture_id": capture_id,
            "input_artifact": input_binding.model_dump(mode="json"),
            "schema_version": "cardrag.gold-qwen-provider-state.v1",
        }
    )
    _publish_immutable(identity_path, identity_payload)
    reservations = _safe_private_state_directory(state / "reservations")
    state_responses = _safe_private_state_directory(state / "responses")
    owner = client is None
    active_client = client or httpx.AsyncClient(
        base_url=base_url.rstrip("/") + "/",
        timeout=httpx.Timeout(timeout_seconds),
        follow_redirects=False,
    )
    requests: list[ProviderRequestRecord] = []
    response_artifacts: list[ProviderResponseArtifact] = []
    try:
        for batch_index, start in enumerate(range(0, len(rows), batch_size)):
            batch = rows[start : start + batch_size]
            response_name = f"qwen-response-{capture_id[:24]}-{batch_index:06d}.json"
            request_body = _provider_request_body(
                model=QWEN3_EMBEDDING_MODEL,
                provider_id=manifest.provider_id,
                formatted_inputs=tuple(row.formatted_input for row in batch),
            )
            request_record = _provider_request_record(
                ordinal=batch_index,
                input_ids=tuple(row.input_id for row in batch),
                request_body=request_body,
                response_file_name=response_name,
            )
            _publish_immutable(
                reservations / f"request-{batch_index:06d}.json",
                request_record.canonical_bytes(),
            )
            requests.append(request_record)
            response_path = provider_receipt_output_path.parent / response_name
            state_response_path = state_responses / response_name
            if state_response_path.exists():
                envelope, envelope_payload = _load_response_envelope(
                    state_response_path,
                    maximum_bytes=maximum_response_bytes * 2,
                    code="qwen_provider_response_envelope",
                )
            elif response_path.exists():
                envelope, envelope_payload = _load_response_envelope(
                    response_path,
                    maximum_bytes=maximum_response_bytes * 2,
                    code="qwen_provider_response_envelope",
                )
                _parse_qwen_response(
                    envelope,
                    provider_id=manifest.provider_id,
                    input_kind=manifest.input_kind,
                    expected_count=len(batch),
                )
                _publish_immutable(state_response_path, envelope_payload)
            else:
                try:
                    async with active_client.stream(
                        "POST",
                        "embeddings",
                        headers={
                            "Authorization": f"Bearer {key}",
                            "Content-Type": "application/json",
                        },
                        json=request_body,
                    ) as response:
                        response.raise_for_status()
                        response_body = await _bounded_provider_body(
                            response,
                            maximum_bytes=maximum_response_bytes,
                        )
                        provider_header = response.headers.get("x-openrouter-provider")
                        response_headers = tuple(response.headers.values())
                except httpx.HTTPError:
                    raise GoldCaptureError("qwen_provider_request_failed") from None
                if _provider_response_contains_secret(
                    body=response_body,
                    header_values=response_headers,
                    secret=key,
                ):
                    raise GoldCaptureError("qwen_provider_response_contains_api_key")
                envelope = EmbeddingRawResponseEnvelope(
                    schema_version="cardrag.gold-embedding-provider-response.v1",
                    status_code=200,
                    provider_header=provider_header,
                    body_sha256=hashlib.sha256(response_body).hexdigest(),
                    body_size_bytes=len(response_body),
                    body_base64=base64.b64encode(response_body).decode("ascii"),
                )
                _parse_qwen_response(
                    envelope,
                    provider_id=manifest.provider_id,
                    input_kind=manifest.input_kind,
                    expected_count=len(batch),
                )
                envelope_payload = envelope.canonical_bytes()
                _publish_immutable(state_response_path, envelope_payload)
            if _envelope_contains_secret(envelope, key):
                raise GoldCaptureError("qwen_provider_response_contains_api_key")
            _parse_qwen_response(
                envelope,
                provider_id=manifest.provider_id,
                input_kind=manifest.input_kind,
                expected_count=len(batch),
            )
            _publish_immutable(response_path, envelope_payload)
            response_artifacts.append(
                ProviderResponseArtifact(
                    file_name=response_name,
                    artifact=_binding_from_bytes(envelope_payload),
                )
            )
    finally:
        if owner:
            await active_client.aclose()
    request_contract_sha256 = _provider_request_contract_sha256(
        model=QWEN3_EMBEDDING_MODEL,
        provider_id=manifest.provider_id,
        requests=requests,
        input_count=len(rows),
    )
    response_set_sha256 = canonical_sha256(
        {
            "artifacts": [item.model_dump(mode="json") for item in response_artifacts],
            "schema_version": "cardrag.gold-provider-responses.v1",
        }
    )
    receipt = ProviderReceipt(
        schema_version="cardrag.gold-provider-receipt.v1",
        provider="openrouter",
        base_url=base_url,
        model=QWEN3_EMBEDDING_MODEL,
        provider_id=manifest.provider_id,
        request_contract_sha256=request_contract_sha256,
        requests=tuple(requests),
        response_artifact_sha256=response_set_sha256,
        response_artifacts=tuple(response_artifacts),
        input_count=len(rows),
    )
    receipt_payload = receipt.canonical_bytes()
    if len(receipt_payload) > receipt_forecast.receipt_size_bytes:
        raise GoldCaptureError("qwen_provider_receipt_size_forecast_mismatch")
    receipt_binding = _publish_immutable(provider_receipt_output_path, receipt_payload)
    replay_manifest = _qwen_replay_manifest(manifest, provider_receipt=receipt_binding)
    forecast = _forecast_qwen_replay_resources(
        record_count=manifest.record_count,
        dimension=manifest.embedding_dimension,
        input_resident_size_bytes=input_binding.size_bytes,
        maximum_response_bytes=maximum_response_bytes,
        batch_size=batch_size,
        manifest_line_size_bytes=len(replay_manifest.canonical_bytes()) + 1,
        input_ids=tuple(row.input_id for row in rows),
    )
    replay_binding = _publish_qwen_replay_from_responses(
        source=manifest,
        rows=rows,
        receipt=receipt,
        receipt_path=provider_receipt_output_path,
        receipt_binding=receipt_binding,
        replay_output_path=replay_output_path,
        forecast=forecast,
    )
    return receipt_binding, replay_binding


def _parse_v109_response(
    envelope: EmbeddingRawResponseEnvelope,
) -> npt.NDArray[np.float32]:
    """Apply the exact response semantics from the fixed v1.0.9 embedder."""

    raw = base64.b64decode(envelope.body_base64, validate=True)
    try:
        payload = _strict_provider_json_object(raw, code="v109_provider_response")
        data = payload["data"]
        if not isinstance(data, list) or len(data) != 1:
            raise ValueError
        item = data[0]
        if not isinstance(item, dict) or item.get("index", 0) != 0:
            raise ValueError
        vector = np.asarray(item["embedding"], dtype=np.float32)
    except GoldCaptureError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise GoldCaptureError("v109_provider_response_invalid") from exc
    if vector.shape != (1536,) or not bool(np.isfinite(vector).all()):
        raise GoldCaptureError("v109_provider_response_vector_invalid")
    norm = np.linalg.norm(vector)
    if not bool(np.isfinite(norm)) or float(norm) <= 0:
        raise GoldCaptureError("v109_provider_response_vector_invalid")
    return np.asarray(vector / norm, dtype=np.float32)


def _validate_provider_evidence(
    *,
    provider_receipt_path: Path,
    receipt: ProviderReceipt,
    lane: EmbeddingLane,
    input_kind: EmbeddingKind,
    expected_inputs: Sequence[tuple[str, str]],
    replay_matrix: npt.NDArray[np.float32],
) -> None:
    """Recompute every request and vector from the raw provider evidence."""

    if len(receipt.response_artifacts) != len(receipt.requests):
        raise GoldCaptureError("provider_response_request_count_mismatch")
    offset = 0
    for request, response_artifact in zip(
        receipt.requests,
        receipt.response_artifacts,
        strict=True,
    ):
        envelope, envelope_payload = _load_response_envelope(
            provider_receipt_path.parent / response_artifact.file_name,
            maximum_bytes=_MAX_PROVIDER_RESPONSE_BYTES * 2,
            code="provider_response_artifact",
        )
        if _binding_from_bytes(envelope_payload) != response_artifact.artifact:
            raise GoldCaptureError("provider_response_artifact_binding_mismatch")
        count = len(request.input_ids)
        selected = expected_inputs[offset : offset + count]
        if len(selected) != count or tuple(row[0] for row in selected) != request.input_ids:
            raise GoldCaptureError("provider_request_input_binding_mismatch")
        request_body = _provider_request_body(
            model=receipt.model,
            provider_id=receipt.provider_id,
            formatted_inputs=tuple(row[1] for row in selected),
        )
        expected_request = _provider_request_record(
            ordinal=request.ordinal,
            input_ids=request.input_ids,
            request_body=request_body,
            response_file_name=request.response_file_name,
        )
        if request != expected_request:
            raise GoldCaptureError("provider_request_contract_mismatch")
        if lane == "qwen_page":
            if receipt.provider_id not in {"deepinfra", "nebius"}:
                raise GoldCaptureError("provider_receipt_profile_mismatch")
            derived = _parse_qwen_response(
                envelope,
                provider_id=receipt.provider_id,
                input_kind=input_kind,
                expected_count=count,
            )
        else:
            if input_kind != "query" or count != 1:
                raise GoldCaptureError("v109_provider_request_contract_mismatch")
            derived = _parse_v109_response(envelope).reshape(1, 1536)
        actual_batch = replay_matrix[offset : offset + count]
        if derived.shape != actual_batch.shape:
            raise GoldCaptureError("provider_replay_vector_count_mismatch")
        for expected, actual in zip(derived, actual_batch, strict=True):
            if (
                expected.astype("<f4", copy=False).tobytes()
                != actual.astype("<f4", copy=False).tobytes()
            ):
                raise GoldCaptureError("provider_replay_vector_mismatch")
        offset += count
    if offset != len(expected_inputs):
        raise GoldCaptureError("provider_request_input_coverage_mismatch")


async def capture_v109_query_embedding_replay(
    *,
    gold_path: Path,
    expected_gold_sha256: str,
    generation_manifest_path: Path,
    database_path: Path,
    openrouter_base_url: str,
    openrouter_api_key_file: Path,
    provider_receipt_output_path: Path,
    replay_output_path: Path,
    state_directory: Path,
    timeout_seconds: float = 120.0,
    maximum_response_bytes: int = _MAX_PROVIDER_RESPONSE_BYTES,
    release_gate: bool = True,
    expected_run_id: str | None = None,
    expected_generation_id: str | None = None,
    expected_manifest_sha256: str | None = None,
    expected_database_sha256: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> tuple[ArtifactBinding, ArtifactBinding]:
    """Replay the exact fixed v1.0.9 query-embedding HTTP contract.

    Unlike the Qwen path this intentionally performs one request per query,
    does not provider-pin, and accepts index omission as zero.  Those details
    are historical v1.0.9 semantics rather than current defaults.
    """

    base_url = _validated_external_openrouter_base_url(openrouter_base_url)
    code = "v109_provider_capture_limits_invalid"
    timeout_seconds = _validated_provider_timeout(timeout_seconds, code=code)
    maximum_response_bytes = _validated_provider_response_limit(
        maximum_response_bytes,
        code=code,
    )
    gold = load_gold_jsonl(gold_path, release_gate=release_gate)
    if gold.sha256 != expected_gold_sha256:
        raise GoldCaptureError("gold_sha256_mismatch")
    manifest, manifest_binding = _load_generation_manifest(generation_manifest_path)
    if manifest.schema_version != "cardrag.generation.v4":
        raise GoldCaptureError("v109_source_requires_generation_v4")
    database_binding = ArtifactBinding(
        sha256=manifest.serving_database.sha256,
        size_bytes=manifest.serving_database.size_bytes,
    )
    _validate_v109_release_anchor(
        release_gate=release_gate,
        expected_run_id=expected_run_id,
        expected_generation_id=expected_generation_id,
        expected_manifest_sha256=expected_manifest_sha256,
        expected_database_sha256=expected_database_sha256,
        generation_id=manifest.generation_id,
        generation_manifest=manifest_binding,
        serving_database=database_binding,
    )
    with _sqlite_readonly(database_path, expected_binding=database_binding) as connection:
        try:
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 64 * 1024)
        except (AttributeError, sqlite3.Error) as exc:
            raise GoldCaptureError("v109_source_database_schema_invalid") from exc
        metadata = _load_bounded_sqlite_metadata(
            connection,
            minimum_count=6,
            maximum_count=64,
            schema_code="v109_source_database_schema_invalid",
            mismatch_code="v109_source_database_profile_mismatch",
        )
    if (
        metadata.get("schema_id") != "cardrag.serving-db.v4"
        or metadata.get("generation_id") != manifest.generation_id
        or metadata.get("embedding_provider") != "openrouter"
        or metadata.get("embedding_model") != "openai/text-embedding-3-small"
        or metadata.get("embedding_dimension") != "1536"
        or metadata.get("embedding_input_policy_version") != "cardrag.embedding-input.v1"
    ):
        raise GoldCaptureError("v109_source_database_profile_mismatch")
    if client is not None:
        _validate_injected_openrouter_client(client)
    key = _read_secret(openrouter_api_key_file)
    capture_id = canonical_sha256(
        {
            "base_url": base_url,
            "database": database_binding.model_dump(mode="json"),
            "generation_manifest": manifest_binding.model_dump(mode="json"),
            "gold_sha256": gold.sha256,
            "http_semantics_source_commit": V109_BASELINE_COMMIT,
            "schema_version": "cardrag.gold-v109-provider-request.v1",
        }
    )
    state_root = _safe_private_state_directory(state_directory)
    state = _safe_private_state_directory(state_root / capture_id)
    _publish_immutable(
        state / "identity.json",
        canonical_json_bytes(
            {
                "capture_id": capture_id,
                "database": database_binding.model_dump(mode="json"),
                "generation_manifest": manifest_binding.model_dump(mode="json"),
                "gold_sha256": gold.sha256,
                "schema_version": "cardrag.gold-v109-provider-state.v1",
            }
        ),
    )
    reservations = _safe_private_state_directory(state / "reservations")
    state_responses = _safe_private_state_directory(state / "responses")
    owner = client is None
    active_client = client or httpx.AsyncClient(
        base_url=base_url.rstrip("/") + "/",
        timeout=httpx.Timeout(timeout_seconds),
        follow_redirects=False,
    )
    requests: list[ProviderRequestRecord] = []
    response_artifacts: list[ProviderResponseArtifact] = []
    vectors: list[npt.NDArray[np.float32]] = []
    try:
        for index, query in enumerate(gold.queries):
            response_name = f"v109-response-{capture_id[:24]}-{index:06d}.json"
            formatted_input = QUERY_EMBEDDING_PREFIX + query.question
            request_body = _provider_request_body(
                model="openai/text-embedding-3-small",
                provider_id=None,
                formatted_inputs=(formatted_input,),
            )
            request_record = _provider_request_record(
                ordinal=index,
                input_ids=(query.query_id,),
                request_body=request_body,
                response_file_name=response_name,
            )
            _publish_immutable(
                reservations / f"request-{index:06d}.json",
                request_record.canonical_bytes(),
            )
            requests.append(request_record)
            response_path = provider_receipt_output_path.parent / response_name
            state_response_path = state_responses / response_name
            if state_response_path.exists():
                envelope, envelope_payload = _load_response_envelope(
                    state_response_path,
                    maximum_bytes=maximum_response_bytes * 2,
                    code="v109_provider_response_envelope",
                )
            elif response_path.exists():
                envelope, envelope_payload = _load_response_envelope(
                    response_path,
                    maximum_bytes=maximum_response_bytes * 2,
                    code="v109_provider_response_envelope",
                )
                _parse_v109_response(envelope)
                _publish_immutable(state_response_path, envelope_payload)
            else:
                try:
                    async with active_client.stream(
                        "POST",
                        "embeddings",
                        headers={
                            "Authorization": f"Bearer {key}",
                            "Content-Type": "application/json",
                        },
                        json=request_body,
                    ) as response:
                        response.raise_for_status()
                        response_body = await _bounded_provider_body(
                            response,
                            maximum_bytes=maximum_response_bytes,
                        )
                        provider_header = response.headers.get("x-openrouter-provider")
                        response_headers = tuple(response.headers.values())
                except httpx.HTTPError:
                    raise GoldCaptureError("v109_provider_request_failed") from None
                if _provider_response_contains_secret(
                    body=response_body,
                    header_values=response_headers,
                    secret=key,
                ):
                    raise GoldCaptureError("v109_provider_response_contains_api_key")
                envelope = EmbeddingRawResponseEnvelope(
                    schema_version="cardrag.gold-embedding-provider-response.v1",
                    status_code=200,
                    provider_header=provider_header,
                    body_sha256=hashlib.sha256(response_body).hexdigest(),
                    body_size_bytes=len(response_body),
                    body_base64=base64.b64encode(response_body).decode("ascii"),
                )
                # Parse before immutable publication so a malformed transient
                # response cannot poison a resumable capture identity.
                _parse_v109_response(envelope)
                envelope_payload = envelope.canonical_bytes()
                _publish_immutable(state_response_path, envelope_payload)
            if _envelope_contains_secret(envelope, key):
                raise GoldCaptureError("v109_provider_response_contains_api_key")
            vector = _parse_v109_response(envelope)
            _publish_immutable(response_path, envelope_payload)
            vectors.append(vector)
            response_artifacts.append(
                ProviderResponseArtifact(
                    file_name=response_name,
                    artifact=_binding_from_bytes(envelope_payload),
                )
            )
    finally:
        if owner:
            await active_client.aclose()
    request_contract_sha256 = _provider_request_contract_sha256(
        model="openai/text-embedding-3-small",
        provider_id=None,
        requests=requests,
        input_count=len(gold.queries),
        source_generation_id=manifest.generation_id,
        source_generation_manifest=manifest_binding,
        source_serving_database=database_binding,
    )
    response_set_sha256 = canonical_sha256(
        {
            "artifacts": [item.model_dump(mode="json") for item in response_artifacts],
            "schema_version": "cardrag.gold-provider-responses.v1",
        }
    )
    receipt = ProviderReceipt(
        schema_version="cardrag.gold-provider-receipt.v1",
        provider="openrouter",
        base_url=base_url,
        model="openai/text-embedding-3-small",
        provider_id=None,
        source_generation_id=manifest.generation_id,
        source_generation_manifest=manifest_binding,
        source_serving_database=database_binding,
        request_contract_sha256=request_contract_sha256,
        requests=tuple(requests),
        response_artifact_sha256=response_set_sha256,
        response_artifacts=tuple(response_artifacts),
        input_count=len(gold.queries),
    )
    receipt_binding = _publish_immutable(
        provider_receipt_output_path,
        receipt.canonical_bytes(),
    )
    replay_manifest = EmbeddingReplayManifest(
        schema_version=EMBEDDING_REPLAY_SCHEMA,
        lane="v109_baseline",
        input_kind="query",
        synthetic=False,
        source_commit=V109_BASELINE_COMMIT,
        embedding_model="openai/text-embedding-3-small",
        embedding_dimension=1536,
        embedding_profile_id="cardrag.embedding.v109-small.v1",
        query_policy="cardrag.embedding-input.v1",
        document_policy=None,
        provider_receipt=receipt_binding,
        record_count=len(gold.queries),
    )
    replay_rows: list[BaseModel] = [replay_manifest]
    for ordinal, (query, vector) in enumerate(zip(gold.queries, vectors, strict=True)):
        formatted = QUERY_EMBEDDING_PREFIX + query.question
        raw = vector.astype("<f4", copy=False).tobytes()
        replay_rows.append(
            EmbeddingReplayRow(
                schema_version=EMBEDDING_REPLAY_ROW_SCHEMA,
                ordinal=ordinal,
                input_id=query.query_id,
                formatted_input_sha256=hashlib.sha256(formatted.encode("utf-8")).hexdigest(),
                vector_f32_sha256=hashlib.sha256(raw).hexdigest(),
                vector_f32_base64=base64.b64encode(raw).decode("ascii"),
            )
        )
    replay_binding = _publish_immutable(replay_output_path, _jsonl_bytes(replay_rows))
    return receipt_binding, replay_binding


def _page_chunks(
    *,
    contract_revision_id: str,
    document_id: str,
    page: int,
    text: str,
    first_row_index: int,
) -> tuple[PageChunk, ...]:
    """Exact v1.0.9 ``chunk_pages`` algorithm, fixed at its historical commit."""

    chunks: list[PageChunk] = []
    length = len(text)
    start = 0
    while start < length:
        limit = min(length, start + PAGE_MAXIMUM_CHARS)
        end = limit
        if limit < length:
            boundary = max(text.rfind("\n", start + 1, limit), text.rfind(" ", start + 1, limit))
            if boundary > start + PAGE_MAXIMUM_CHARS // 2:
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
        chunks.append(
            PageChunk(
                row_index=first_row_index + len(chunks),
                chunk_id=chunk_id,
                contract_revision_id=contract_revision_id,
                document_id=document_id,
                page=page,
                source_start=start,
                source_end=end,
                text=chunk_text,
                input_sha256=input_sha256,
            )
        )
        if end >= length:
            break
        start = max(start + 1, end - PAGE_OVERLAP_CHARS)
    return tuple(chunks)


def _source_page_chunks(
    *,
    generation_manifest: GenerationManifest,
    database_path: Path,
) -> tuple[PageChunk, ...]:
    if generation_manifest.schema_version != "cardrag.generation.v5":
        raise GoldCaptureError("page_source_requires_generation_v5")
    chunks: list[PageChunk] = []
    seen_identities: set[tuple[str, str]] = set()
    seen_chunk_ids: set[str] = set()
    last_pages: dict[str, int] = {}
    declared_page_counts: dict[str, int] = {}
    source_row_count = 0
    source_text_size = 0
    maximum_source_page_size = 0
    chunk_text_size = 0
    with _sqlite_readonly(
        database_path,
        expected_binding=ArtifactBinding(
            sha256=generation_manifest.serving_database.sha256,
            size_bytes=generation_manifest.serving_database.size_bytes,
        ),
    ) as connection:
        try:
            connection.setlimit(
                sqlite3.SQLITE_LIMIT_LENGTH,
                _SOURCE_PAGE_SINGLE_TEXT_LIMIT_BYTES + 64 * 1024,
            )
        except (AttributeError, sqlite3.Error) as exc:
            raise GoldCaptureError("page_source_database_schema_invalid") from exc
        metadata = _load_bounded_sqlite_metadata(
            connection,
            minimum_count=3,
            maximum_count=64,
            schema_code="page_source_database_schema_invalid",
            mismatch_code="page_source_database_profile_mismatch",
        )
        try:
            current_count_row = connection.execute(
                "SELECT count(*) FROM contract_revisions WHERE temporal_status='current'"
            ).fetchone()
            summary = connection.execute(
                """SELECT count(*),
                          coalesce(sum(length(CAST(p.text AS BLOB))),0),
                          coalesce(max(length(CAST(p.text AS BLOB))),0),
                          coalesce(sum(CASE
                            WHEN typeof(r.contract_revision_id)='text'
                             AND typeof(r.document_id)='text'
                             AND typeof(r.page_count)='integer'
                             AND typeof(p.page)='integer'
                             AND typeof(p.text)='text'
                             AND typeof(p.text_sha256)='text'
                            THEN 0 ELSE 1 END),0),
                          coalesce(min(length(CAST(r.contract_revision_id AS BLOB))),0),
                          coalesce(max(length(CAST(r.contract_revision_id AS BLOB))),0),
                          coalesce(min(length(CAST(r.document_id AS BLOB))),0),
                          coalesce(max(length(CAST(r.document_id AS BLOB))),0),
                          coalesce(min(length(CAST(p.text_sha256 AS BLOB))),0),
                          coalesce(max(length(CAST(p.text_sha256 AS BLOB))),0),
                          coalesce(sum(length(CAST(r.contract_revision_id AS BLOB))
                                     + length(CAST(r.document_id AS BLOB))
                                     + length(CAST(p.text_sha256 AS BLOB))),0),
                          coalesce(min(r.page_count),0),
                          coalesce(max(r.page_count),0),
                          coalesce(min(p.page),0),
                          coalesce(max(p.page),0)
                     FROM contract_revisions AS r
                     JOIN document_pages AS p
                       ON p.contract_revision_id=r.contract_revision_id
                    WHERE r.temporal_status='current'"""
            ).fetchone()
            if (
                current_count_row is None
                or len(current_count_row) != 1
                or type(current_count_row[0]) is not int
                or summary is None
                or any(type(value) is not int for value in summary)
            ):
                raise GoldCaptureError("page_source_database_profile_mismatch")
            current_count = current_count_row[0]
            (
                summary_page_count,
                summary_text_size,
                summary_maximum_text_size,
                invalid_storage_count,
                minimum_revision_size,
                maximum_revision_size,
                minimum_document_size,
                maximum_document_size,
                minimum_sha256_size,
                maximum_sha256_size,
                auxiliary_text_size,
                minimum_declared_page_count,
                maximum_declared_page_count,
                minimum_page,
                maximum_page,
            ) = cast(
                tuple[int, int, int, int, int, int, int, int, int, int, int, int, int, int, int],
                summary,
            )
            if (
                metadata.get("schema_id") != "cardrag.serving-db.v5"
                or metadata.get("generation_id") != generation_manifest.generation_id
                or metadata.get("current_revision_count") != str(current_count)
                or summary_page_count < 1
                or invalid_storage_count != 0
                or minimum_revision_size < 1
                or maximum_revision_size > _MAX_IDENTIFIER_CHARACTERS
                or minimum_document_size < 1
                or maximum_document_size > _MAX_IDENTIFIER_CHARACTERS
                or minimum_sha256_size != 64
                or maximum_sha256_size != 64
                or auxiliary_text_size < summary_page_count * 66
                or auxiliary_text_size > summary_page_count * (_MAX_IDENTIFIER_CHARACTERS * 2 + 64)
                or minimum_declared_page_count < 1
                or maximum_declared_page_count > summary_page_count
                or minimum_page < 1
                or maximum_page > summary_page_count
            ):
                raise GoldCaptureError("page_source_database_profile_mismatch")
            _validate_source_page_summary(
                page_count=summary_page_count,
                current_revision_count=current_count,
                total_text_size_bytes=summary_text_size,
                maximum_text_size_bytes=summary_maximum_text_size,
            )
            source_cursor = connection.execute(
                """SELECT r.contract_revision_id,r.document_id,r.page_count,
                          p.page,p.text,p.text_sha256
                     FROM contract_revisions AS r
                     JOIN document_pages AS p
                       ON p.contract_revision_id=r.contract_revision_id
                    WHERE r.temporal_status='current'
                    ORDER BY r.contract_revision_id,p.page"""
            )
            for revision_id, document_id, page_count, page, text, declared_sha256 in source_cursor:
                revision = str(revision_id)
                page_number = int(page)
                declared_page_count = int(page_count)
                page_text = str(text)
                page_text_bytes = page_text.encode("utf-8")
                page_text_size = len(page_text_bytes)
                source_row_count += 1
                source_text_size += page_text_size
                maximum_source_page_size = max(maximum_source_page_size, page_text_size)
                if hashlib.sha256(page_text_bytes).hexdigest() != str(declared_sha256):
                    raise GoldCaptureError("page_source_text_sha256_mismatch")
                prior_count = declared_page_counts.setdefault(revision, declared_page_count)
                expected_page = last_pages.get(revision, 0) + 1
                if (
                    prior_count != declared_page_count
                    or page_number != expected_page
                    or page_number > declared_page_count
                ):
                    raise GoldCaptureError("page_source_page_count_mismatch")
                last_pages[revision] = page_number
                page_chunks = _page_chunks(
                    contract_revision_id=revision,
                    document_id=str(document_id),
                    page=page_number,
                    text=page_text,
                    first_row_index=len(chunks),
                )
                if not page_chunks:
                    raise GoldCaptureError("page_chunk_identity_invalid")
                for chunk in page_chunks:
                    identity = (chunk.chunk_id, chunk.contract_revision_id)
                    if identity in seen_identities or chunk.chunk_id in seen_chunk_ids:
                        raise GoldCaptureError("page_chunk_identity_invalid")
                    seen_identities.add(identity)
                    seen_chunk_ids.add(chunk.chunk_id)
                    chunk_text_size += len(chunk.text.encode("utf-8"))
                    chunks.append(chunk)
                if chunks:
                    _forecast_source_page_chunks(
                        chunk_count=len(chunks),
                        chunk_text_size_bytes=chunk_text_size,
                        maximum_source_page_size_bytes=summary_maximum_text_size,
                    )
        except sqlite3.Error as exc:
            raise GoldCaptureError("page_source_database_schema_invalid") from exc
    if (
        source_row_count != summary_page_count
        or source_text_size != summary_text_size
        or maximum_source_page_size != summary_maximum_text_size
        or len(last_pages) != current_count
        or any(last_pages[revision] != declared_page_counts[revision] for revision in last_pages)
    ):
        raise GoldCaptureError("page_source_page_coverage_mismatch")
    if not chunks or len(seen_identities) != len(chunks) or len(seen_chunk_ids) != len(chunks):
        raise GoldCaptureError("page_chunk_identity_invalid")
    return tuple(chunks)


def _validate_source_qwen_profile(
    manifest: GenerationManifest,
    *,
    embedding_profile_id: str,
    provider_id: Literal["deepinfra", "nebius"] | None = None,
    maximum_tokens: int | None = None,
) -> None:
    """Bind page-lane evidence to the candidate's exact primary Qwen profile."""

    profile = next(
        (
            candidate
            for candidate in manifest.embedding_profiles
            if candidate.profile_id == embedding_profile_id
        ),
        None,
    )
    if (
        manifest.schema_version != "cardrag.generation.v5"
        or manifest.primary_embedding_profile_id != embedding_profile_id
        or profile is None
        or profile.provider != "openrouter"
        or profile.model != QWEN3_EMBEDDING_MODEL
        or profile.dimension != QWEN3_EMBEDDING_DIMENSION
        or profile.provider_fallback != "forbidden"
        or profile.dtype != "float32"
        or profile.normalization != "l2"
        or profile.document_instruction is not None
        or profile.query_policy != QWEN3_QUERY_POLICY
        or profile.truncation != "error"
        or (provider_id is not None and profile.provider_id != provider_id)
        or (maximum_tokens is not None and profile.maximum_tokens != maximum_tokens)
    ):
        raise GoldCaptureError("page_source_embedding_profile_mismatch")


_PAGE_DATABASE_DDL = """
CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) STRICT, WITHOUT ROWID;
CREATE TABLE evaluation_chunks(
  row_index INTEGER PRIMARY KEY CHECK(row_index >= 0),
  chunk_id TEXT NOT NULL UNIQUE,
  contract_revision_id TEXT NOT NULL,
  span_id TEXT NOT NULL UNIQUE,
  document_id TEXT NOT NULL,
  page INTEGER NOT NULL CHECK(page > 0),
  source_start INTEGER NOT NULL CHECK(source_start >= 0),
  source_end INTEGER NOT NULL CHECK(source_end > source_start),
  text TEXT NOT NULL CHECK(length(text) > 0),
  input_sha256 TEXT NOT NULL CHECK(length(input_sha256)=64)
) STRICT;
"""


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _safe_private_state_directory(path: Path) -> Path:
    """Create/traverse state with no symlink component and a private leaf."""

    absolute = Path(os.path.abspath(path))
    flags = (
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open("/", flags)
    try:
        for component in absolute.parts[1:]:
            try:
                os.mkdir(component, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except OSError as exc:
                raise GoldCaptureError("provider_state_ancestor_invalid") from exc
            os.close(descriptor)
            descriptor = child
        status = os.fstat(descriptor)
        if status.st_uid != os.geteuid() or stat.S_IMODE(status.st_mode) & 0o077:
            raise GoldCaptureError("provider_state_directory_not_private")
    finally:
        os.close(descriptor)
    return absolute


def _publish_built_file(
    temporary: Path,
    destination: Path,
    *,
    maximum_bytes: int,
    code: str,
) -> ArtifactBinding:
    source_binding = _hash_regular(temporary, maximum_bytes=maximum_bytes, code=f"{code}_working")
    if destination.is_symlink():
        raise GoldCaptureError(f"{code}_output_symlink")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(temporary, 0o400)
    try:
        os.link(temporary, destination, follow_symlinks=False)
    except FileExistsError:
        existing = _hash_regular(destination, maximum_bytes=maximum_bytes, code=f"{code}_output")
        if existing != source_binding:
            raise GoldCaptureError(f"{code}_output_already_differs") from None
    _fsync_directory(destination.parent)
    temporary.unlink(missing_ok=True)
    return source_binding


def _build_page_database(
    destination: Path,
    *,
    generation_id: str,
    source_commit: str,
    source_generation_id: str,
    source_generation_manifest: ArtifactBinding,
    source_serving_database: ArtifactBinding,
    embedding_profile_id: str,
    chunks: Sequence[PageChunk],
) -> ArtifactBinding:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".page-eval-", dir=destination.parent)
    os.close(descriptor)
    working = Path(name)
    working.unlink()
    connection = sqlite3.connect(working)
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.executescript(_PAGE_DATABASE_DDL)
        metadata = {
            "schema_id": "cardrag.evaluation-page.v1",
            "generation_id": generation_id,
            "source_commit": source_commit,
            "source_generation_id": source_generation_id,
            "source_generation_manifest_sha256": source_generation_manifest.sha256,
            "source_generation_manifest_size_bytes": str(source_generation_manifest.size_bytes),
            "source_serving_database_sha256": source_serving_database.sha256,
            "source_serving_database_size_bytes": str(source_serving_database.size_bytes),
            "embedding_model": QWEN3_EMBEDDING_MODEL,
            "embedding_dimension": str(QWEN3_EMBEDDING_DIMENSION),
            "embedding_profile_id": embedding_profile_id,
            "chunking_policy": PAGE_CHUNKING_POLICY,
            "maximum_chars": str(PAGE_MAXIMUM_CHARS),
            "overlap_chars": str(PAGE_OVERLAP_CHARS),
            "source_text_contract": PAGE_SOURCE_TEXT_CONTRACT,
            "column_contract": PAGE_COLUMN_CONTRACT,
            "row_count": str(len(chunks)),
        }
        connection.executemany("INSERT INTO metadata VALUES(?,?)", sorted(metadata.items()))
        connection.executemany(
            "INSERT INTO evaluation_chunks VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                (
                    row.row_index,
                    row.chunk_id,
                    row.contract_revision_id,
                    row.chunk_id,
                    row.document_id,
                    row.page,
                    row.source_start,
                    row.source_end,
                    row.text,
                    row.input_sha256,
                )
                for row in chunks
            ),
        )
        connection.commit()
        check = connection.execute("PRAGMA integrity_check").fetchone()
        if check is None or str(check[0]) != "ok":
            raise GoldCaptureError("page_database_integrity_failed")
    except Exception:
        connection.close()
        working.unlink(missing_ok=True)
        raise
    connection.close()
    return _publish_built_file(
        working,
        destination,
        maximum_bytes=_MAX_DATABASE_BYTES,
        code="page_database",
    )


def _write_vectors(
    destination: Path,
    matrix: npt.NDArray[np.float32],
) -> ArtifactBinding:
    if matrix.ndim != 2 or matrix.shape[1] != QWEN3_EMBEDDING_DIMENSION:
        raise GoldCaptureError("page_vector_shape_invalid")
    predicted_size = _predicted_page_vector_size(matrix.shape[0])
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".page-vectors-", dir=destination.parent)
    working = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            for start in range(0, matrix.shape[0], 256):
                block = matrix[start : start + 256]
                if not bool(np.isfinite(block).all()) or not bool(
                    np.allclose(
                        np.linalg.norm(block, axis=1),
                        1.0,
                        rtol=_NORM_TOLERANCE,
                        atol=_NORM_TOLERANCE,
                    )
                ):
                    raise GoldCaptureError("page_vectors_invalid")
                output.write(block.astype("<f4", copy=False).tobytes(order="C"))
            output.flush()
            os.fsync(output.fileno())
        return _publish_built_file(
            working,
            destination,
            maximum_bytes=predicted_size,
            code="page_vectors",
        )
    finally:
        working.unlink(missing_ok=True)


def _inventory_bytes(
    *,
    lane: EmbeddingLane,
    generation_id: str,
    database: ArtifactBinding,
    vectors: ArtifactBinding | None,
    rows: Sequence[tuple[str, str, str, str, str]],
    dimension: Literal[1536, 4096],
) -> bytes:
    manifest = CorpusInventoryManifest(
        schema_version="cardrag.gold-corpus-inventory.v1",
        lane=lane,
        generation_id=generation_id,
        serving_database_sha256=database.sha256,
        vector_artifact_sha256=None if vectors is None else vectors.sha256,
        embedding_dimension=dimension,
        row_count=len(rows),
    )
    records: list[BaseModel] = [manifest]
    records.extend(
        CorpusInventoryRow(
            schema_version="cardrag.gold-corpus-row.v1",
            row_index=index,
            evidence_id=evidence_id,
            contract_revision_id=contract_id,
            span_id=span_id,
            input_sha256=input_sha256,
            embedding_f32_sha256=embedding_sha256,
        )
        for index, (evidence_id, contract_id, span_id, input_sha256, embedding_sha256) in enumerate(
            rows
        )
    )
    payload = _jsonl_bytes(records)
    _validate_git_evidence_size(len(payload), code="corpus_inventory")
    return payload


def build_qwen_page_corpus(
    *,
    source_generation_manifest_path: Path,
    source_database_path: Path,
    source_commit: str,
    embedding_profile_id: str,
    document_embedding_replay_path: Path,
    provider_receipt_path: Path,
    database_output_path: Path,
    vector_output_path: Path,
    inventory_output_path: Path,
    generation_manifest_output_path: Path,
) -> QwenPageCorpus:
    """Build the non-production page/1,600 corpus from one immutable v5 generation."""

    commit = _validated_source_commit(source_commit)
    source_manifest, source_binding = _load_generation_manifest(source_generation_manifest_path)
    _validate_source_qwen_profile(
        source_manifest,
        embedding_profile_id=embedding_profile_id,
    )
    chunks = _source_page_chunks(
        generation_manifest=source_manifest,
        database_path=source_database_path,
    )
    generation_id = (
        "page-"
        + canonical_sha256(
            {
                "chunking_policy": PAGE_CHUNKING_POLICY,
                "embedding_profile_id": embedding_profile_id,
                "source_commit": commit,
                "source_generation_id": source_manifest.generation_id,
                "source_generation_manifest_sha256": source_binding.sha256,
            }
        )[:48]
    )
    replay_manifest, matrix, _replay_binding = load_embedding_replay(
        document_embedding_replay_path,
        provider_receipt_path=provider_receipt_path,
        lane="qwen_page",
        input_kind="document",
        source_commit=commit,
        embedding_profile_id=embedding_profile_id,
        expected_inputs=_PageEmbeddingInputs(chunks),
        retained_resident_size_bytes=_page_chunks_retained_resident_size(chunks),
    )
    database_binding = _build_page_database(
        database_output_path,
        generation_id=generation_id,
        source_commit=commit,
        source_generation_id=source_manifest.generation_id,
        source_generation_manifest=source_binding,
        source_serving_database=ArtifactBinding(
            sha256=source_manifest.serving_database.sha256,
            size_bytes=source_manifest.serving_database.size_bytes,
        ),
        embedding_profile_id=embedding_profile_id,
        chunks=chunks,
    )
    vector_binding = _write_vectors(vector_output_path, matrix)
    inventory_payload = _inventory_bytes(
        lane="qwen_page",
        generation_id=generation_id,
        database=database_binding,
        vectors=vector_binding,
        dimension=4096,
        rows=tuple(
            (
                chunk.chunk_id,
                chunk.contract_revision_id,
                chunk.chunk_id,
                chunk.input_sha256,
                hashlib.sha256(matrix[index].astype("<f4", copy=False).tobytes()).hexdigest(),
            )
            for index, chunk in enumerate(chunks)
        ),
    )
    inventory_binding = _publish_immutable(inventory_output_path, inventory_payload)
    page_manifest = PageGenerationManifest(
        schema_version="cardrag.evaluation-page-generation.v2",
        source_commit=commit,
        source_generation_id=source_manifest.generation_id,
        source_generation_manifest=source_binding,
        source_serving_database=ArtifactBinding(
            sha256=source_manifest.serving_database.sha256,
            size_bytes=source_manifest.serving_database.size_bytes,
        ),
        generation_id=generation_id,
        serving_schema="cardrag.evaluation-page.v1",
        serving_database=database_binding,
        vector_artifact=vector_binding,
        embedding_model=QWEN3_EMBEDDING_MODEL,
        embedding_dimension=4096,
        embedding_profile_id=replay_manifest.embedding_profile_id,
        chunking_policy=PAGE_CHUNKING_POLICY,
        maximum_chars=PAGE_MAXIMUM_CHARS,
        overlap_chars=PAGE_OVERLAP_CHARS,
        source_text_contract=PAGE_SOURCE_TEXT_CONTRACT,
        column_contract=PAGE_COLUMN_CONTRACT,
        row_count=len(chunks),
        corpus_inventory_sha256=inventory_binding.sha256,
    )
    page_manifest_payload = page_manifest.canonical_bytes()
    _validate_git_evidence_size(len(page_manifest_payload), code="page_manifest")
    manifest_binding = _publish_immutable(
        generation_manifest_output_path,
        page_manifest_payload,
    )
    return QwenPageCorpus(
        database=database_binding,
        vectors=vector_binding,
        inventory=inventory_binding,
        generation_manifest=manifest_binding,
        generation_id=generation_id,
        row_count=len(chunks),
    )


def build_v109_inventory(
    *,
    generation_manifest_source_path: Path,
    generation_manifest_output_path: Path,
    database_path: Path,
    inventory_output_path: Path,
    release_gate: bool = True,
    expected_run_id: str | None = None,
    expected_generation_id: str | None = None,
    expected_publish_sha256: str | None = None,
    expected_manifest_sha256: str | None = None,
    expected_database_sha256: str | None = None,
) -> tuple[GenerationManifest, ArtifactBinding, ArtifactBinding]:
    """Extract and inventory the exact preserved v1.0.9 generation."""

    source_payload = _read_regular(
        generation_manifest_source_path,
        maximum_bytes=_MAX_MANIFEST_BYTES,
        code="v109_publish_source",
    )
    publish_binding = _binding_from_bytes(source_payload)
    manifest, nested = _manifest_from_source_payload(source_payload)
    manifest_payload = manifest.canonical_bytes()
    manifest_binding = _binding_from_bytes(manifest_payload)
    if manifest.schema_version != "cardrag.generation.v4":
        raise GoldCaptureError("v109_source_requires_generation_v4")
    database_binding = ArtifactBinding(
        sha256=manifest.serving_database.sha256,
        size_bytes=manifest.serving_database.size_bytes,
    )
    if release_gate and (not nested or expected_publish_sha256 is None):
        raise GoldCaptureError("v109_preserved_publish_anchor_mismatch")
    _validate_v109_release_anchor(
        release_gate=release_gate,
        expected_run_id=expected_run_id,
        expected_generation_id=expected_generation_id,
        expected_publish_sha256=expected_publish_sha256,
        expected_manifest_sha256=expected_manifest_sha256,
        expected_database_sha256=expected_database_sha256,
        generation_id=manifest.generation_id,
        generation_manifest=manifest_binding,
        serving_database=database_binding,
        publish_artifact=publish_binding if nested else None,
    )
    rows: list[tuple[str, str, str, str, str]] = []
    with _sqlite_readonly(database_path, expected_binding=database_binding) as connection:
        try:
            metadata = {
                str(row[0]): str(row[1])
                for row in connection.execute("SELECT key,value FROM metadata")
            }
            source_rows = connection.execute(
                "SELECT evidence_id,document_id,text,embedding FROM evidence ORDER BY evidence_id"
            ).fetchall()
        except sqlite3.Error as exc:
            raise GoldCaptureError("v109_source_database_schema_invalid") from exc
    if (
        metadata.get("schema_id") != "cardrag.serving-db.v4"
        or metadata.get("generation_id") != manifest.generation_id
        or metadata.get("embedding_model") != "openai/text-embedding-3-small"
        or metadata.get("embedding_dimension") != "1536"
        or len(source_rows) != manifest.counts.chunks
    ):
        raise GoldCaptureError("v109_source_database_profile_mismatch")
    for evidence_id, document_id, text, embedding in source_rows:
        if not isinstance(embedding, bytes) or len(embedding) != 1536 * 4:
            raise GoldCaptureError("v109_source_embedding_invalid")
        vector = np.frombuffer(embedding, dtype="<f4")
        if not bool(np.isfinite(vector).all()) or not math.isclose(
            float(np.linalg.norm(vector)),
            1.0,
            rel_tol=_NORM_TOLERANCE,
            abs_tol=_NORM_TOLERANCE,
        ):
            raise GoldCaptureError("v109_source_embedding_invalid")
        evidence = str(evidence_id)
        rows.append(
            (
                evidence,
                str(document_id),
                evidence,
                hashlib.sha256(str(text).encode("utf-8")).hexdigest(),
                hashlib.sha256(embedding).hexdigest(),
            )
        )
    published_manifest_binding = _publish_immutable(
        generation_manifest_output_path,
        manifest_payload,
    )
    if published_manifest_binding != manifest_binding:
        raise GoldCaptureError("v109_manifest_publish_binding_mismatch")
    inventory_payload = _inventory_bytes(
        lane="v109_baseline",
        generation_id=manifest.generation_id,
        database=database_binding,
        vectors=None,
        dimension=1536,
        rows=tuple(rows),
    )
    inventory_binding = _publish_immutable(inventory_output_path, inventory_payload)
    return manifest, manifest_binding, inventory_binding


def _contracts_from_ranked_rows(
    ranked: Sequence[tuple[int, float]],
    *,
    inventory: Sequence[CorpusInventoryRow],
    maximum: int = 100,
) -> tuple[RetrievedContract, ...]:
    result: list[RetrievedContract] = []
    seen: set[str] = set()
    for row_index, score in ranked:
        contract_revision_id = inventory[row_index].contract_revision_id
        if contract_revision_id in seen:
            continue
        seen.add(contract_revision_id)
        result.append(
            RetrievedContract(
                contract_revision_id=contract_revision_id,
                rank=len(result) + 1,
                score=score,
            )
        )
        if len(result) == maximum:
            break
    return tuple(result)


def _ranked_result(
    *,
    lane: EmbeddingLane,
    query_id: str,
    answer: EvaluatedAnswer,
    inventory: Sequence[CorpusInventoryRow],
    scores: npt.NDArray[np.float32],
    dense_order: Sequence[int],
    lexical_ranks: Mapping[int, int],
) -> QueryRunResult:
    dense_ranked = tuple((index, float(scores[index])) for index in dense_order)
    dense_limit = 250 if lane == "v109_baseline" else 100
    dense_spans = tuple(
        RetrievedSpan(
            span_id=inventory[row_index].span_id,
            contract_revision_id=inventory[row_index].contract_revision_id,
            rank=rank,
            score=score,
        )
        for rank, (row_index, score) in enumerate(dense_ranked[:dense_limit], start=1)
    )
    baseline: V109BaselineObservation | None = None
    if lane == "v109_baseline":
        dense_contracts = _contracts_from_ranked_rows(
            dense_ranked[:250],
            inventory=inventory,
        )
        dense_ranks = {row_index: rank for rank, row_index in enumerate(dense_order, start=1)}
        fused = sorted(
            (
                (
                    (
                        0.0
                        if row_index not in lexical_ranks
                        else 1.0 / (60 + lexical_ranks[row_index])
                    )
                    + (
                        0.0 if dense_ranks[row_index] > 250 else 1.0 / (60 + dense_ranks[row_index])
                    ),
                    row_index,
                )
                for row_index in range(len(inventory))
                if row_index in lexical_ranks or dense_ranks[row_index] <= 250
            ),
            key=lambda item: (-item[0], inventory[item[1]].evidence_id),
        )
        primary_ranked = tuple((row_index, score) for score, row_index in fused)
        primary_spans = tuple(
            RetrievedSpan(
                span_id=inventory[row_index].span_id,
                contract_revision_id=inventory[row_index].contract_revision_id,
                rank=rank,
                score=score,
            )
            for rank, (row_index, score) in enumerate(primary_ranked[:100], start=1)
        )
        baseline = V109BaselineObservation(
            kind="v109_small_rrf",
            rrf_k=60,
            dense_contracts=dense_contracts,
            dense_spans=dense_spans,
        )
    else:
        primary_ranked = dense_ranked
        primary_spans = dense_spans
    return QueryRunResult(
        schema_version="cardrag.gold-run-result.v1",
        query_id=query_id,
        lane=lane,
        contracts=_contracts_from_ranked_rows(primary_ranked, inventory=inventory),
        spans=primary_spans,
        answer=answer,
        v109_baseline=baseline,
    )


def _fts_ranks(connection: sqlite3.Connection, question: str) -> dict[str, int]:
    tokens = [part for part in question.split() if part]
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
                LIMIT 250""",
            (expression,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise GoldCaptureError("v109_lexical_capture_failed") from exc
    return {str(row[0]): rank for rank, row in enumerate(rows, start=1)}


def _compact_query_observations(
    *,
    lane: EmbeddingLane,
    gold: GoldDataset,
    answers: Mapping[str, EvaluatedAnswer],
    query_vectors: npt.NDArray[np.float32],
    corpus: npt.NDArray[np.float32],
    inventory: Sequence[CorpusInventoryRow],
    source_database: sqlite3.Connection,
    dense_score_matrix_path: Path,
    query_vector_matrix_path: Path,
    lexical_rank_path: Path | None,
) -> CompactObservationArtifacts:
    query_count = len(gold.queries)
    row_count = len(inventory)
    dimension: Literal[1536, 4096] = 1536 if lane == "v109_baseline" else 4096
    if (
        query_vectors.shape != (query_count, dimension)
        or corpus.shape != (row_count, dimension)
        or (lane == "v109_baseline") != (lexical_rank_path is not None)
    ):
        raise GoldCaptureError("external_compact_evidence_shape_invalid")
    absolute_outputs = {
        os.path.abspath(dense_score_matrix_path),
        os.path.abspath(query_vector_matrix_path),
        *(() if lexical_rank_path is None else (os.path.abspath(lexical_rank_path),)),
    }
    if len(absolute_outputs) != (2 if lexical_rank_path is None else 3):
        raise GoldCaptureError("external_compact_evidence_output_collision")

    dense_size = _predicted_f32_matrix_size(
        query_count,
        row_count,
        code="external_dense_score_matrix",
    )
    vector_size = _predicted_f32_matrix_size(
        query_count,
        dimension,
        code="external_query_vector_matrix",
    )
    dense_matrix = np.empty((query_count, row_count), dtype="<f4")
    vector_payload = query_vectors.astype("<f4", copy=False).tobytes(order="C")
    _validate_git_evidence_size(
        len(vector_payload),
        code="external_query_vector_matrix",
        expected_size_bytes=vector_size,
    )

    observations: list[ExternalQueryObservation] = []
    lexical_payload = bytearray()
    corpus_norms = np.linalg.norm(corpus, axis=1)
    if not bool(np.isfinite(corpus_norms).all()) or bool(np.any(corpus_norms <= 0.0)):
        raise GoldCaptureError("external_corpus_vector_invalid")
    for query_index, query in enumerate(gold.queries):
        query_vector = query_vectors[query_index]
        query_raw = query_vector.astype("<f4", copy=False).tobytes(order="C")
        query_norm = float(np.linalg.norm(query_vector))
        if not math.isfinite(query_norm) or query_norm <= 0.0:
            raise GoldCaptureError("external_query_vector_invalid")
        scores = np.asarray(
            (corpus @ query_vector) / (corpus_norms * query_norm),
            dtype="<f4",
        )
        if not bool(np.isfinite(scores).all()) or bool(
            np.any(scores < -1.0) or np.any(scores > 1.0)
        ):
            raise GoldCaptureError("external_dense_score_invalid")
        dense_matrix[query_index] = scores
        dense_order = sorted(
            range(len(inventory)),
            key=lambda index: (-float(scores[index]), inventory[index].evidence_id),
        )
        lexical_by_evidence = (
            _fts_ranks(source_database, query.question) if lane == "v109_baseline" else {}
        )
        row_index_by_evidence = {row.evidence_id: row.row_index for row in inventory}
        if not set(lexical_by_evidence).issubset(row_index_by_evidence):
            raise GoldCaptureError("external_lexical_inventory_mismatch")
        lexical_by_row = {
            row_index_by_evidence[evidence_id]: rank
            for evidence_id, rank in lexical_by_evidence.items()
        }

        dense_offset = query_index * row_count * 4
        vector_offset = query_index * dimension * 4
        dense_raw = scores.tobytes(order="C")
        lexical_offset: int | None = None
        lexical_size: int | None = None
        lexical_count: int | None = None
        lexical_sha256: str | None = None
        if lane == "v109_baseline":
            lexical_record = ExternalLexicalRanks(
                schema_version="cardrag.gold-lexical-ranks.v1",
                ordinal=query_index,
                query_id=query.query_id,
                ranks=tuple(
                    sorted(
                        lexical_by_row.items(),
                        key=lambda item: item[1],
                    )
                ),
            )
            lexical_raw = lexical_record.canonical_bytes() + b"\n"
            lexical_offset = len(lexical_payload)
            lexical_size = len(lexical_raw)
            lexical_count = len(lexical_by_row)
            lexical_sha256 = hashlib.sha256(lexical_raw).hexdigest()
            lexical_payload.extend(lexical_raw)
        observations.append(
            ExternalQueryObservation(
                schema_version="cardrag.gold-external-query-observation.v2",
                ordinal=query_index,
                lane=lane,
                query_id=query.query_id,
                query_sha256=hashlib.sha256(query.question.encode("utf-8")).hexdigest(),
                dense_offset_bytes=dense_offset,
                dense_size_bytes=len(dense_raw),
                dense_count=row_count,
                dense_sha256=hashlib.sha256(dense_raw).hexdigest(),
                vector_offset_bytes=vector_offset,
                vector_size_bytes=len(query_raw),
                vector_count=dimension,
                vector_sha256=hashlib.sha256(query_raw).hexdigest(),
                lexical_offset_bytes=lexical_offset,
                lexical_size_bytes=lexical_size,
                lexical_count=lexical_count,
                lexical_sha256=lexical_sha256,
                result=_ranked_result(
                    lane=lane,
                    query_id=query.query_id,
                    answer=answers[query.query_id],
                    inventory=inventory,
                    scores=scores,
                    dense_order=dense_order,
                    lexical_ranks=lexical_by_row,
                ),
            )
        )
    dense_payload = dense_matrix.tobytes(order="C")
    _validate_git_evidence_size(
        len(dense_payload),
        code="external_dense_score_matrix",
        expected_size_bytes=dense_size,
    )
    dense_binding = _publish_immutable(dense_score_matrix_path, dense_payload)
    vector_binding = _publish_immutable(query_vector_matrix_path, vector_payload)
    lexical_binding: ArtifactBinding | None = None
    if lexical_rank_path is not None:
        _validate_git_evidence_size(
            len(lexical_payload),
            code="external_lexical_rank_artifact",
        )
        lexical_binding = _publish_immutable(lexical_rank_path, bytes(lexical_payload))
    return CompactObservationArtifacts(
        observations=tuple(observations),
        dense_score_matrix=dense_binding,
        query_vector_matrix=vector_binding,
        lexical_rank_artifact=lexical_binding,
    )


def _load_inventory_rows(
    path: Path,
) -> tuple[CorpusInventoryManifest, tuple[CorpusInventoryRow, ...], ArtifactBinding]:
    with _CanonicalJsonlReader(
        path,
        maximum_bytes=MAX_GIT_EVIDENCE_FILE_BYTES,
        code="producer_inventory",
    ) as reader:
        raw_manifest = reader.next_record()
        try:
            manifest = CorpusInventoryManifest.model_validate(raw_manifest)
        except ValidationError as exc:
            raise GoldCaptureError("producer_inventory_manifest_invalid") from exc
        if raw_manifest is None or canonical_json_bytes(raw_manifest) != manifest.canonical_bytes():
            raise GoldCaptureError("producer_inventory_binding_invalid")
        rows: list[CorpusInventoryRow] = []
        for index in range(manifest.row_count):
            raw = reader.next_record()
            if raw is None:
                raise GoldCaptureError("producer_inventory_binding_invalid")
            try:
                row = CorpusInventoryRow.model_validate(raw)
            except ValidationError as exc:
                raise GoldCaptureError(
                    "producer_inventory_row_invalid",
                    line=index + 2,
                ) from exc
            if canonical_json_bytes(raw) != row.canonical_bytes() or row.row_index != index:
                raise GoldCaptureError(
                    "producer_inventory_row_binding_invalid",
                    line=index + 2,
                )
            rows.append(row)
        if reader.next_record() is not None:
            raise GoldCaptureError("producer_inventory_binding_invalid")
        binding = reader.binding
    return manifest, tuple(rows), binding


def _load_page_vectors(
    path: Path,
    *,
    expected_binding: ArtifactBinding,
    inventory: Sequence[CorpusInventoryRow],
) -> npt.NDArray[np.float32]:
    forecast = _forecast_page_vector_load(len(inventory))
    if expected_binding.size_bytes != forecast.vector_size_bytes:
        raise GoldCaptureError("page_vector_binding_mismatch")
    absolute = Path(os.path.abspath(path))
    try:
        listed = absolute.lstat()
    except FileNotFoundError:
        raise GoldCaptureError("page_vectors_missing") from None
    if stat.S_ISLNK(listed.st_mode) or not stat.S_ISREG(listed.st_mode):
        raise GoldCaptureError("page_vectors_not_regular")
    if listed.st_size != forecast.vector_size_bytes:
        raise GoldCaptureError("page_vector_binding_mismatch")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(absolute, flags)
    except OSError as exc:
        raise GoldCaptureError("page_vectors_open_failed") from exc
    byte_view: memoryview | None = None
    try:
        before = os.fstat(descriptor)
        if _file_identity(listed) != _file_identity(before):
            raise GoldCaptureError("page_vectors_changed_during_read")
        try:
            matrix = np.empty((len(inventory), QWEN3_EMBEDDING_DIMENSION), dtype="<f4")
        except (MemoryError, ValueError) as exc:
            raise GoldCaptureError("page_vector_matrix_allocation_failed") from exc
        byte_view = memoryview(matrix).cast("B")
        digest = hashlib.sha256()
        offset = 0
        for row_start in range(0, len(inventory), 256):
            row_end = min(len(inventory), row_start + 256)
            block_end = row_end * QWEN3_EMBEDDING_DIMENSION * 4
            while offset < block_end:
                try:
                    count = os.readv(descriptor, [byte_view[offset:block_end]])
                except OSError as exc:
                    raise GoldCaptureError("page_vectors_read_failed") from exc
                if count <= 0:
                    raise GoldCaptureError("page_vectors_changed_during_read")
                digest.update(byte_view[offset : offset + count])
                offset += count
        if offset != forecast.vector_size_bytes or os.read(descriptor, 1):
            raise GoldCaptureError("page_vectors_changed_during_read")
        after = os.fstat(descriptor)
        try:
            current = absolute.lstat()
        except FileNotFoundError:
            raise GoldCaptureError("page_vectors_changed_during_read") from None
        if _file_identity(before) != _file_identity(after) or _file_identity(
            before
        ) != _file_identity(current):
            raise GoldCaptureError("page_vectors_changed_during_read")
        binding = ArtifactBinding(
            sha256=digest.hexdigest(),
            size_bytes=offset,
        )
        if binding != expected_binding:
            raise GoldCaptureError("page_vector_binding_mismatch")
        for row_start in range(0, len(inventory), 256):
            row_end = min(len(inventory), row_start + 256)
            block = matrix[row_start:row_end]
            if not bool(np.isfinite(block).all()) or not bool(
                np.allclose(
                    np.linalg.norm(block, axis=1),
                    1.0,
                    rtol=_NORM_TOLERANCE,
                    atol=_NORM_TOLERANCE,
                )
            ):
                raise GoldCaptureError("producer_page_vectors_invalid")
            for index in range(row_start, row_end):
                raw = matrix[index].astype("<f4", copy=False).tobytes()
                if hashlib.sha256(raw).hexdigest() != inventory[index].embedding_f32_sha256:
                    raise GoldCaptureError("producer_page_vector_inventory_mismatch")
    finally:
        if byte_view is not None:
            byte_view.release()
        os.close(descriptor)
    return matrix


def _expected_page_database_metadata(
    page_manifest: PageGenerationManifest,
) -> dict[str, str]:
    return {
        "column_contract": page_manifest.column_contract,
        "chunking_policy": page_manifest.chunking_policy,
        "embedding_dimension": str(page_manifest.embedding_dimension),
        "embedding_model": page_manifest.embedding_model,
        "embedding_profile_id": page_manifest.embedding_profile_id,
        "generation_id": page_manifest.generation_id,
        "maximum_chars": str(page_manifest.maximum_chars),
        "overlap_chars": str(page_manifest.overlap_chars),
        "row_count": str(page_manifest.row_count),
        "schema_id": page_manifest.serving_schema,
        "source_commit": page_manifest.source_commit,
        "source_generation_id": page_manifest.source_generation_id,
        "source_generation_manifest_sha256": page_manifest.source_generation_manifest.sha256,
        "source_generation_manifest_size_bytes": str(
            page_manifest.source_generation_manifest.size_bytes
        ),
        "source_serving_database_sha256": page_manifest.source_serving_database.sha256,
        "source_serving_database_size_bytes": str(page_manifest.source_serving_database.size_bytes),
        "source_text_contract": page_manifest.source_text_contract,
    }


def _load_qwen_observation_corpus(
    connection: sqlite3.Connection,
    *,
    page_manifest: PageGenerationManifest,
    vector_path: Path,
    vectors_binding: ArtifactBinding,
    inventory: Sequence[CorpusInventoryRow],
) -> npt.NDArray[np.float32]:
    """Validate page rows with bounded cursors before allocating their vector matrix."""

    expected_row_count = page_manifest.row_count
    if expected_row_count != len(inventory):
        raise GoldCaptureError("producer_page_inventory_mismatch")
    vector_forecast = _forecast_page_vector_load(expected_row_count)
    if vectors_binding.size_bytes != vector_forecast.vector_size_bytes:
        raise GoldCaptureError("page_vector_binding_mismatch")
    expected_metadata = _expected_page_database_metadata(page_manifest)
    try:
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 64 * 1024)
    except (AttributeError, sqlite3.Error) as exc:
        raise GoldCaptureError("page_database_schema_invalid") from exc
    metadata = _load_bounded_sqlite_metadata(
        connection,
        minimum_count=len(expected_metadata),
        maximum_count=len(expected_metadata),
        schema_code="page_database_schema_invalid",
        mismatch_code="page_database_metadata_mismatch",
    )
    if metadata != expected_metadata:
        raise GoldCaptureError("page_database_metadata_mismatch")
    try:
        page_summary = connection.execute(
            """SELECT count(*),
                      coalesce(sum(length(CAST(text AS BLOB))),0),
                      coalesce(max(length(CAST(text AS BLOB))),0),
                      coalesce(sum(CASE
                        WHEN typeof(row_index)='integer'
                         AND typeof(chunk_id)='text'
                         AND typeof(contract_revision_id)='text'
                         AND typeof(span_id)='text'
                         AND typeof(document_id)='text'
                         AND typeof(page)='integer'
                         AND typeof(source_start)='integer'
                         AND typeof(source_end)='integer'
                         AND typeof(text)='text'
                         AND typeof(input_sha256)='text'
                        THEN 0 ELSE 1 END),0),
                      coalesce(min(length(CAST(chunk_id AS BLOB))),0),
                      coalesce(max(length(CAST(chunk_id AS BLOB))),0),
                      coalesce(min(length(CAST(contract_revision_id AS BLOB))),0),
                      coalesce(max(length(CAST(contract_revision_id AS BLOB))),0),
                      coalesce(min(length(CAST(span_id AS BLOB))),0),
                      coalesce(max(length(CAST(span_id AS BLOB))),0),
                      coalesce(min(length(CAST(document_id AS BLOB))),0),
                      coalesce(max(length(CAST(document_id AS BLOB))),0),
                      coalesce(min(length(CAST(input_sha256 AS BLOB))),0),
                      coalesce(max(length(CAST(input_sha256 AS BLOB))),0),
                      coalesce(sum(length(CAST(chunk_id AS BLOB))
                                   + length(CAST(contract_revision_id AS BLOB))
                                   + length(CAST(span_id AS BLOB))
                                   + length(CAST(document_id AS BLOB))
                                   + length(CAST(input_sha256 AS BLOB))),0),
                      coalesce(min(row_index),0),
                      coalesce(max(row_index),0),
                      coalesce(min(page),0),
                      coalesce(max(page),0),
                      coalesce(min(source_start),0),
                      coalesce(max(source_start),0),
                      coalesce(min(source_end),0),
                      coalesce(max(source_end),0)
                 FROM evaluation_chunks"""
        ).fetchone()
        if page_summary is None:
            raise GoldCaptureError("page_database_schema_invalid")
        if any(type(value) is not int for value in page_summary):
            raise GoldCaptureError("page_database_schema_invalid")
        (
            actual_row_count,
            total_text_size,
            maximum_text_size,
            invalid_storage_count,
            minimum_chunk_id_size,
            maximum_chunk_id_size,
            minimum_revision_size,
            maximum_revision_size,
            minimum_span_id_size,
            maximum_span_id_size,
            minimum_document_size,
            maximum_document_size,
            minimum_sha256_size,
            maximum_sha256_size,
            auxiliary_text_size,
            minimum_row_index,
            maximum_row_index,
            minimum_page,
            maximum_page,
            minimum_source_start,
            maximum_source_start,
            minimum_source_end,
            maximum_source_end,
        ) = cast(
            tuple[
                int,
                int,
                int,
                int,
                int,
                int,
                int,
                int,
                int,
                int,
                int,
                int,
                int,
                int,
                int,
                int,
                int,
                int,
                int,
                int,
                int,
                int,
                int,
            ],
            page_summary,
        )
        maximum_canonical_text_size = PAGE_MAXIMUM_CHARS * 4
        if (
            actual_row_count != expected_row_count
            or total_text_size < actual_row_count
            or maximum_text_size < 1
            or maximum_text_size > maximum_canonical_text_size
            or total_text_size > actual_row_count * maximum_canonical_text_size
            or invalid_storage_count != 0
            or minimum_chunk_id_size != 73
            or maximum_chunk_id_size != 73
            or minimum_revision_size < 1
            or maximum_revision_size > _MAX_IDENTIFIER_CHARACTERS
            or minimum_span_id_size != 73
            or maximum_span_id_size != 73
            or minimum_document_size < 1
            or maximum_document_size > _MAX_IDENTIFIER_CHARACTERS
            or minimum_sha256_size != 64
            or maximum_sha256_size != 64
            or auxiliary_text_size < actual_row_count * 212
            or auxiliary_text_size
            > actual_row_count * (73 * 2 + _MAX_IDENTIFIER_CHARACTERS * 2 + 64)
            or minimum_row_index != 0
            or maximum_row_index != actual_row_count - 1
            or minimum_page < 1
            or maximum_page > actual_row_count
            or minimum_source_start < 0
            or maximum_source_start > _SOURCE_PAGE_SINGLE_TEXT_LIMIT_BYTES
            or minimum_source_end < 1
            or maximum_source_end > _SOURCE_PAGE_SINGLE_TEXT_LIMIT_BYTES
        ):
            raise GoldCaptureError("producer_page_inventory_mismatch")
        source_cursor = connection.execute(
            """SELECT row_index,chunk_id,contract_revision_id,span_id,document_id,
                      page,source_start,source_end,text,input_sha256
                 FROM evaluation_chunks ORDER BY row_index"""
        )
        parsed_row_count = 0
        parsed_text_size = 0
        parsed_maximum_text_size = 0
        for index, source in enumerate(source_cursor):
            if index >= expected_row_count:
                raise GoldCaptureError("producer_page_inventory_mismatch")
            expected = inventory[index]
            source_text = str(source[8])
            source_text_bytes = source_text.encode("utf-8")
            source_text_size = len(source_text_bytes)
            source_start = int(source[6])
            source_end = int(source[7])
            input_sha256 = hashlib.sha256(source_text_bytes).hexdigest()
            expected_chunk_id = "evidence_" + canonical_sha256(
                {
                    "document_id": str(source[4]),
                    "page": int(source[5]),
                    "source_end": source_end,
                    "source_start": source_start,
                    "text_sha256": input_sha256,
                }
            )
            if (
                int(source[0]) != index
                or str(source[1]) != expected.evidence_id
                or str(source[2]) != expected.contract_revision_id
                or str(source[3]) != expected.span_id
                or str(source[1]) != str(source[3])
                or str(source[1]) != expected_chunk_id
                or str(source[9]) != expected.input_sha256
                or input_sha256 != expected.input_sha256
                or source_start < 0
                or source_end <= source_start
                or source_end - source_start != len(source_text)
                or source_text != source_text.strip()
                or len(source_text) > PAGE_MAXIMUM_CHARS
            ):
                raise GoldCaptureError("producer_page_inventory_mismatch")
            parsed_row_count += 1
            parsed_text_size += source_text_size
            parsed_maximum_text_size = max(parsed_maximum_text_size, source_text_size)
        if (
            parsed_row_count != actual_row_count
            or parsed_text_size != total_text_size
            or parsed_maximum_text_size != maximum_text_size
        ):
            raise GoldCaptureError("producer_page_inventory_mismatch")
    except sqlite3.Error as exc:
        raise GoldCaptureError("page_database_schema_invalid") from exc
    return _load_page_vectors(
        vector_path,
        expected_binding=vectors_binding,
        inventory=inventory,
    )


def produce_external_observation(
    *,
    lane: EmbeddingLane,
    gold_path: Path,
    expected_gold_sha256: str,
    answer_artifact_path: Path,
    expected_answer_artifact_sha256: str,
    query_embedding_replay_path: Path,
    provider_receipt_path: Path,
    generation_manifest_path: Path,
    database_path: Path,
    vector_path: Path | None,
    inventory_path: Path,
    source_commit: str,
    output_path: Path,
    dense_score_matrix_path: Path,
    query_vector_matrix_path: Path,
    lexical_rank_path: Path | None,
    release_gate: bool = True,
    expected_v109_run_id: str | None = None,
    expected_v109_generation_id: str | None = None,
    expected_v109_manifest_sha256: str | None = None,
    expected_v109_database_sha256: str | None = None,
) -> ArtifactBinding:
    """Produce an observation accepted by :func:`seal_external_observation`."""

    evidence_outputs = (
        output_path,
        dense_score_matrix_path,
        query_vector_matrix_path,
        *((lexical_rank_path,) if lexical_rank_path is not None else ()),
    )
    if (lane == "v109_baseline") != (lexical_rank_path is not None) or len(
        {os.path.abspath(path) for path in evidence_outputs}
    ) != len(evidence_outputs):
        raise GoldCaptureError("external_compact_evidence_output_invalid")
    commit = _validated_source_commit(source_commit)
    gold = load_gold_jsonl(gold_path, release_gate=release_gate)
    if gold.sha256 != expected_gold_sha256:
        raise GoldCaptureError("gold_sha256_mismatch")
    if lane == "v109_baseline":
        source_manifest, generation_binding = _load_generation_manifest(generation_manifest_path)
        if (
            source_manifest.schema_version != "cardrag.generation.v4"
            or commit != V109_BASELINE_COMMIT
        ):
            raise GoldCaptureError("v109_source_identity_mismatch")
        generation_id = source_manifest.generation_id
        serving_database = ArtifactBinding(
            sha256=source_manifest.serving_database.sha256,
            size_bytes=source_manifest.serving_database.size_bytes,
        )
        _validate_v109_release_anchor(
            release_gate=release_gate,
            expected_run_id=expected_v109_run_id,
            expected_generation_id=expected_v109_generation_id,
            expected_manifest_sha256=expected_v109_manifest_sha256,
            expected_database_sha256=expected_v109_database_sha256,
            generation_id=source_manifest.generation_id,
            generation_manifest=generation_binding,
            serving_database=serving_database,
        )
        vectors_binding = None
        profile_id = "cardrag.embedding.v109-small.v1"
        source_version: Literal["v1.0.9", "v1.0.10-candidate"] = "v1.0.9"
        serving_schema: Literal["cardrag.serving-db.v4", "cardrag.evaluation-page.v1"] = (
            "cardrag.serving-db.v4"
        )
        model: Literal["openai/text-embedding-3-small", "qwen/qwen3-embedding-8b"] = (
            "openai/text-embedding-3-small"
        )
        dimension: Literal[1536, 4096] = 1536
        query_inputs = tuple(
            (query.query_id, QUERY_EMBEDDING_PREFIX + query.question) for query in gold.queries
        )
    else:
        payload = _read_regular(
            generation_manifest_path,
            maximum_bytes=_MAX_MANIFEST_BYTES,
            code="page_manifest",
        )
        try:
            page_manifest = PageGenerationManifest.model_validate_json(payload)
        except ValidationError as exc:
            raise GoldCaptureError("page_manifest_invalid") from exc
        if payload != page_manifest.canonical_bytes() or page_manifest.source_commit != commit:
            raise GoldCaptureError("page_manifest_binding_mismatch")
        generation_binding = _binding_from_bytes(payload)
        generation_id = page_manifest.generation_id
        serving_database = page_manifest.serving_database
        vectors_binding = page_manifest.vector_artifact
        profile_id = page_manifest.embedding_profile_id
        source_version = "v1.0.10-candidate"
        serving_schema = "cardrag.evaluation-page.v1"
        model = QWEN3_EMBEDDING_MODEL
        dimension = 4096
        query_inputs = tuple(
            (query.query_id, format_qwen3_query(query.question)) for query in gold.queries
        )
    _answer_manifest, answers, answer_binding = _load_answers(
        answer_artifact_path,
        gold=gold,
        lane=lane,
        generation_id=generation_id,
        generation_manifest_sha256=generation_binding.sha256,
    )
    if answer_binding.sha256 != expected_answer_artifact_sha256:
        raise GoldCaptureError("answer_artifact_sha256_mismatch")
    _replay_manifest, query_vectors, _query_replay_binding = load_embedding_replay(
        query_embedding_replay_path,
        provider_receipt_path=provider_receipt_path,
        lane=lane,
        input_kind="query",
        source_commit=commit,
        embedding_profile_id=profile_id,
        expected_inputs=query_inputs,
        expected_source_generation_id=generation_id if lane == "v109_baseline" else None,
        expected_source_generation_manifest=(
            generation_binding if lane == "v109_baseline" else None
        ),
        expected_source_serving_database=(serving_database if lane == "v109_baseline" else None),
    )
    inventory_manifest, inventory, inventory_binding = _load_inventory_rows(inventory_path)
    if (
        inventory_manifest.lane != lane
        or inventory_manifest.generation_id != generation_id
        or inventory_manifest.serving_database_sha256 != serving_database.sha256
        or inventory_manifest.vector_artifact_sha256
        != (None if vectors_binding is None else vectors_binding.sha256)
        or inventory_manifest.embedding_dimension != dimension
    ):
        raise GoldCaptureError("producer_inventory_source_mismatch")
    with _sqlite_readonly(database_path, expected_binding=serving_database) as connection:
        if lane == "v109_baseline":
            source_rows = connection.execute(
                "SELECT evidence_id,document_id,text,embedding FROM evidence ORDER BY evidence_id"
            ).fetchall()
            corpus = np.empty((len(source_rows), 1536), dtype="<f4")
            for index, (source, expected) in enumerate(zip(source_rows, inventory, strict=True)):
                embedding = source[3]
                if (
                    not isinstance(embedding, bytes)
                    or str(source[0]) != expected.evidence_id
                    or str(source[1]) != expected.contract_revision_id
                    or hashlib.sha256(str(source[2]).encode("utf-8")).hexdigest()
                    != expected.input_sha256
                    or hashlib.sha256(embedding).hexdigest() != expected.embedding_f32_sha256
                ):
                    raise GoldCaptureError("producer_v109_inventory_mismatch")
                corpus[index] = np.frombuffer(embedding, dtype="<f4")
        else:
            if vector_path is None or vectors_binding is None:
                raise GoldCaptureError("page_vector_artifact_required")
            corpus = _load_qwen_observation_corpus(
                connection,
                page_manifest=page_manifest,
                vector_path=vector_path,
                vectors_binding=vectors_binding,
                inventory=inventory,
            )
        compact = _compact_query_observations(
            lane=lane,
            gold=gold,
            answers=answers,
            query_vectors=query_vectors,
            corpus=corpus,
            inventory=inventory,
            source_database=connection,
            dense_score_matrix_path=dense_score_matrix_path,
            query_vector_matrix_path=query_vector_matrix_path,
            lexical_rank_path=lexical_rank_path,
        )
    manifest = ExternalObservationManifest(
        schema_version="cardrag.gold-external-observation-artifact.v2",
        lane=lane,
        capture_mode="external_reproducible",
        synthetic=False,
        gold_sha256=gold.sha256,
        query_count=len(gold.queries),
        source_version=source_version,
        source_commit=commit,
        generation_id=generation_id,
        generation_manifest=generation_binding,
        serving_schema=serving_schema,
        serving_database=serving_database,
        vector_artifact=vectors_binding,
        embedding_model=model,
        embedding_dimension=dimension,
        embedding_profile_id=profile_id,
        retrieval_policy="small_rrf" if lane == "v109_baseline" else "qwen_page_window",
        maximum_candidates=250 if lane == "v109_baseline" else None,
        scoring_contract=(
            "cardrag.v109-small-dense-rrf-capture.v1"
            if lane == "v109_baseline"
            else "cardrag.qwen-page-exact-capture.v1"
        ),
        row_count=len(inventory),
        corpus_inventory_sha256=inventory_binding.sha256,
        dense_score_matrix=compact.dense_score_matrix,
        query_vector_matrix=compact.query_vector_matrix,
        lexical_rank_artifact=compact.lexical_rank_artifact,
        byte_order="little-endian",
        scalar_type="float32",
        matrix_order="row-major",
        maximum_result_contracts=100,
        maximum_result_spans=100,
        maximum_dense_trace_contracts=100,
        maximum_dense_trace_spans=250,
        approximate=False,
    )
    observation_payload = _jsonl_bytes((manifest, *compact.observations))
    _validate_git_evidence_size(
        len(observation_payload),
        code="external_observation",
    )
    return _publish_immutable(output_path, observation_payload)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract = subparsers.add_parser("extract-manifest")
    extract.add_argument("--source", type=Path, required=True)
    extract.add_argument("--output", type=Path, required=True)

    v109_inventory = subparsers.add_parser("v109-inventory")
    v109_inventory.add_argument("--generation-manifest-source", type=Path, required=True)
    v109_inventory.add_argument("--generation-manifest-output", type=Path, required=True)
    v109_inventory.add_argument("--database", type=Path, required=True)
    v109_inventory.add_argument("--inventory-output", type=Path, required=True)
    v109_inventory.add_argument("--expected-run-id")
    v109_inventory.add_argument("--expected-generation-id")
    v109_inventory.add_argument("--expected-publish-sha256")
    v109_inventory.add_argument("--expected-manifest-sha256")
    v109_inventory.add_argument("--expected-database-sha256")
    v109_inventory.add_argument("--fixture-mode", action="store_true")

    v109_live = subparsers.add_parser("v109-live-replay")
    v109_live.add_argument("--gold", type=Path, required=True)
    v109_live.add_argument("--expected-gold-sha256", required=True)
    v109_live.add_argument("--generation-manifest", type=Path, required=True)
    v109_live.add_argument("--database", type=Path, required=True)
    v109_live.add_argument(
        "--openrouter-base-url",
        default="https://openrouter.ai/api/v1",
    )
    v109_live.add_argument("--openrouter-api-key-file", type=Path, required=True)
    v109_live.add_argument("--provider-receipt-output", type=Path, required=True)
    v109_live.add_argument("--replay-output", type=Path, required=True)
    v109_live.add_argument("--state-dir", type=Path, required=True)
    v109_live.add_argument("--expected-run-id")
    v109_live.add_argument("--expected-generation-id")
    v109_live.add_argument("--expected-manifest-sha256")
    v109_live.add_argument("--expected-database-sha256")
    v109_live.add_argument("--timeout-seconds", type=float, default=120.0)
    v109_live.add_argument(
        "--maximum-response-bytes",
        type=int,
        default=_MAX_PROVIDER_RESPONSE_BYTES,
    )
    v109_live.add_argument("--fixture-mode", action="store_true")

    page = subparsers.add_parser("qwen-page-corpus")
    page.add_argument("--source-generation-manifest", type=Path, required=True)
    page.add_argument("--source-database", type=Path, required=True)
    page.add_argument("--source-commit", required=True)
    page.add_argument("--embedding-profile-id", required=True)
    page.add_argument("--document-embedding-replay", type=Path, required=True)
    page.add_argument("--provider-receipt", type=Path, required=True)
    page.add_argument("--database-output", type=Path, required=True)
    page.add_argument("--vectors-output", type=Path, required=True)
    page.add_argument("--inventory-output", type=Path, required=True)
    page.add_argument("--generation-manifest-output", type=Path, required=True)

    page_inputs = subparsers.add_parser("qwen-page-inputs")
    page_inputs.add_argument("--source-generation-manifest", type=Path, required=True)
    page_inputs.add_argument("--source-database", type=Path, required=True)
    page_inputs.add_argument("--source-commit", required=True)
    page_inputs.add_argument("--embedding-profile-id", required=True)
    page_inputs.add_argument("--provider-id", choices=("deepinfra", "nebius"), required=True)
    page_inputs.add_argument("--maximum-tokens", type=int, required=True)
    page_inputs.add_argument("--tokenizer", type=Path, required=True)
    page_inputs.add_argument("--output", type=Path, required=True)

    query_inputs = subparsers.add_parser("qwen-query-inputs")
    query_inputs.add_argument("--gold", type=Path, required=True)
    query_inputs.add_argument("--expected-gold-sha256", required=True)
    query_inputs.add_argument("--source-commit", required=True)
    query_inputs.add_argument("--embedding-profile-id", required=True)
    query_inputs.add_argument("--provider-id", choices=("deepinfra", "nebius"), required=True)
    query_inputs.add_argument("--maximum-tokens", type=int, required=True)
    query_inputs.add_argument("--tokenizer", type=Path, required=True)
    query_inputs.add_argument("--output", type=Path, required=True)
    query_inputs.add_argument("--fixture-mode", action="store_true")

    live = subparsers.add_parser("qwen-live-replay")
    live.add_argument("--input", type=Path, required=True)
    live.add_argument(
        "--openrouter-base-url",
        default="https://openrouter.ai/api/v1",
    )
    live.add_argument("--openrouter-api-key-file", type=Path, required=True)
    live.add_argument("--provider-receipt-output", type=Path, required=True)
    live.add_argument("--replay-output", type=Path, required=True)
    live.add_argument("--state-dir", type=Path, required=True)
    live.add_argument("--tokenizer", type=Path, required=True)
    live.add_argument("--batch-size", type=int, default=16)
    live.add_argument("--timeout-seconds", type=float, default=120.0)
    live.add_argument(
        "--maximum-response-bytes",
        type=int,
        default=_MAX_PROVIDER_RESPONSE_BYTES,
    )

    observe = subparsers.add_parser("observe")
    observe.add_argument("--lane", choices=("v109_baseline", "qwen_page"), required=True)
    observe.add_argument("--gold", type=Path, required=True)
    observe.add_argument("--expected-gold-sha256", required=True)
    observe.add_argument("--answer-artifact", type=Path, required=True)
    observe.add_argument("--expected-answer-artifact-sha256", required=True)
    observe.add_argument("--query-embedding-replay", type=Path, required=True)
    observe.add_argument("--provider-receipt", type=Path, required=True)
    observe.add_argument("--generation-manifest", type=Path, required=True)
    observe.add_argument("--database", type=Path, required=True)
    observe.add_argument("--vectors", type=Path)
    observe.add_argument("--inventory", type=Path, required=True)
    observe.add_argument("--source-commit", required=True)
    observe.add_argument("--output", type=Path, required=True)
    observe.add_argument("--score-matrix", type=Path, required=True)
    observe.add_argument("--query-vector-matrix", type=Path, required=True)
    observe.add_argument("--lexical-ranks", type=Path)
    observe.add_argument("--expected-v109-run-id")
    observe.add_argument("--expected-v109-generation-id")
    observe.add_argument("--expected-v109-manifest-sha256")
    observe.add_argument("--expected-v109-database-sha256")
    observe.add_argument("--fixture-mode", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "extract-manifest":
            manifest, binding = extract_generation_manifest(
                cast(Path, arguments.source),
                output_path=cast(Path, arguments.output),
            )
            result: object = {
                "generation_id": manifest.generation_id,
                "manifest": binding.model_dump(mode="json"),
            }
        elif arguments.command == "v109-inventory":
            manifest, manifest_binding, inventory_binding = build_v109_inventory(
                generation_manifest_source_path=cast(Path, arguments.generation_manifest_source),
                generation_manifest_output_path=cast(Path, arguments.generation_manifest_output),
                database_path=cast(Path, arguments.database),
                inventory_output_path=cast(Path, arguments.inventory_output),
                release_gate=not bool(arguments.fixture_mode),
                expected_run_id=cast(str | None, arguments.expected_run_id),
                expected_generation_id=cast(str | None, arguments.expected_generation_id),
                expected_publish_sha256=cast(str | None, arguments.expected_publish_sha256),
                expected_manifest_sha256=cast(str | None, arguments.expected_manifest_sha256),
                expected_database_sha256=cast(str | None, arguments.expected_database_sha256),
            )
            result = {
                "generation_id": manifest.generation_id,
                "generation_manifest": manifest_binding.model_dump(mode="json"),
                "inventory": inventory_binding.model_dump(mode="json"),
            }
        elif arguments.command == "v109-live-replay":
            receipt_binding, replay_binding = asyncio.run(
                capture_v109_query_embedding_replay(
                    gold_path=cast(Path, arguments.gold),
                    expected_gold_sha256=str(arguments.expected_gold_sha256),
                    generation_manifest_path=cast(Path, arguments.generation_manifest),
                    database_path=cast(Path, arguments.database),
                    openrouter_base_url=str(arguments.openrouter_base_url),
                    openrouter_api_key_file=cast(Path, arguments.openrouter_api_key_file),
                    provider_receipt_output_path=cast(
                        Path,
                        arguments.provider_receipt_output,
                    ),
                    replay_output_path=cast(Path, arguments.replay_output),
                    state_directory=cast(Path, arguments.state_dir),
                    timeout_seconds=float(arguments.timeout_seconds),
                    maximum_response_bytes=int(arguments.maximum_response_bytes),
                    release_gate=not bool(arguments.fixture_mode),
                    expected_run_id=cast(str | None, arguments.expected_run_id),
                    expected_generation_id=cast(
                        str | None,
                        arguments.expected_generation_id,
                    ),
                    expected_manifest_sha256=cast(
                        str | None,
                        arguments.expected_manifest_sha256,
                    ),
                    expected_database_sha256=cast(
                        str | None,
                        arguments.expected_database_sha256,
                    ),
                )
            )
            result = {
                "provider_receipt": receipt_binding.model_dump(mode="json"),
                "replay": replay_binding.model_dump(mode="json"),
            }
        elif arguments.command == "qwen-page-corpus":
            corpus = build_qwen_page_corpus(
                source_generation_manifest_path=cast(Path, arguments.source_generation_manifest),
                source_database_path=cast(Path, arguments.source_database),
                source_commit=str(arguments.source_commit),
                embedding_profile_id=str(arguments.embedding_profile_id),
                document_embedding_replay_path=cast(Path, arguments.document_embedding_replay),
                provider_receipt_path=cast(Path, arguments.provider_receipt),
                database_output_path=cast(Path, arguments.database_output),
                vector_output_path=cast(Path, arguments.vectors_output),
                inventory_output_path=cast(Path, arguments.inventory_output),
                generation_manifest_output_path=cast(Path, arguments.generation_manifest_output),
            )
            result = {
                "database": corpus.database.model_dump(mode="json"),
                "generation_id": corpus.generation_id,
                "generation_manifest": corpus.generation_manifest.model_dump(mode="json"),
                "inventory": corpus.inventory.model_dump(mode="json"),
                "row_count": corpus.row_count,
                "vectors": corpus.vectors.model_dump(mode="json"),
            }
        elif arguments.command == "qwen-page-inputs":
            binding = prepare_qwen_page_embedding_inputs(
                source_generation_manifest_path=cast(
                    Path,
                    arguments.source_generation_manifest,
                ),
                source_database_path=cast(Path, arguments.source_database),
                source_commit=str(arguments.source_commit),
                embedding_profile_id=str(arguments.embedding_profile_id),
                provider_id=cast(Literal["deepinfra", "nebius"], arguments.provider_id),
                maximum_tokens=int(arguments.maximum_tokens),
                tokenizer_path=cast(Path, arguments.tokenizer),
                output_path=cast(Path, arguments.output),
            )
            result = {"qwen_embedding_inputs": binding.model_dump(mode="json")}
        elif arguments.command == "qwen-query-inputs":
            binding = prepare_qwen_query_embedding_inputs(
                gold_path=cast(Path, arguments.gold),
                expected_gold_sha256=str(arguments.expected_gold_sha256),
                source_commit=str(arguments.source_commit),
                embedding_profile_id=str(arguments.embedding_profile_id),
                provider_id=cast(Literal["deepinfra", "nebius"], arguments.provider_id),
                maximum_tokens=int(arguments.maximum_tokens),
                tokenizer_path=cast(Path, arguments.tokenizer),
                output_path=cast(Path, arguments.output),
                release_gate=not bool(arguments.fixture_mode),
            )
            result = {"qwen_embedding_inputs": binding.model_dump(mode="json")}
        elif arguments.command == "qwen-live-replay":
            receipt_binding, replay_binding = asyncio.run(
                capture_qwen_embedding_replay(
                    input_path=cast(Path, arguments.input),
                    openrouter_base_url=str(arguments.openrouter_base_url),
                    openrouter_api_key_file=cast(Path, arguments.openrouter_api_key_file),
                    provider_receipt_output_path=cast(
                        Path,
                        arguments.provider_receipt_output,
                    ),
                    replay_output_path=cast(Path, arguments.replay_output),
                    state_directory=cast(Path, arguments.state_dir),
                    tokenizer_path=cast(Path, arguments.tokenizer),
                    batch_size=int(arguments.batch_size),
                    timeout_seconds=float(arguments.timeout_seconds),
                    maximum_response_bytes=int(arguments.maximum_response_bytes),
                )
            )
            result = {
                "provider_receipt": receipt_binding.model_dump(mode="json"),
                "replay": replay_binding.model_dump(mode="json"),
            }
        else:
            binding = produce_external_observation(
                lane=cast(EmbeddingLane, arguments.lane),
                gold_path=cast(Path, arguments.gold),
                expected_gold_sha256=str(arguments.expected_gold_sha256),
                answer_artifact_path=cast(Path, arguments.answer_artifact),
                expected_answer_artifact_sha256=str(arguments.expected_answer_artifact_sha256),
                query_embedding_replay_path=cast(Path, arguments.query_embedding_replay),
                provider_receipt_path=cast(Path, arguments.provider_receipt),
                generation_manifest_path=cast(Path, arguments.generation_manifest),
                database_path=cast(Path, arguments.database),
                vector_path=cast(Path | None, arguments.vectors),
                inventory_path=cast(Path, arguments.inventory),
                source_commit=str(arguments.source_commit),
                output_path=cast(Path, arguments.output),
                dense_score_matrix_path=cast(Path, arguments.score_matrix),
                query_vector_matrix_path=cast(Path, arguments.query_vector_matrix),
                lexical_rank_path=cast(Path | None, arguments.lexical_ranks),
                release_gate=not bool(arguments.fixture_mode),
                expected_v109_run_id=cast(str | None, arguments.expected_v109_run_id),
                expected_v109_generation_id=cast(
                    str | None,
                    arguments.expected_v109_generation_id,
                ),
                expected_v109_manifest_sha256=cast(
                    str | None,
                    arguments.expected_v109_manifest_sha256,
                ),
                expected_v109_database_sha256=cast(
                    str | None,
                    arguments.expected_v109_database_sha256,
                ),
            )
            result = {"observation": binding.model_dump(mode="json")}
    except GoldCaptureError as exc:
        print(json.dumps({"error": exc.code, "line": exc.line}, sort_keys=True))
        return 2
    print(canonical_json_bytes(result).decode("utf-8"))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
