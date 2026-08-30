"""Capture every actual v5 exact-row score into compact, resumable v2 artifacts."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import struct
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, BinaryIO, Literal, Self, cast

import numpy as np
from cardrag_core import GenerationManifest, canonical_json_bytes, canonical_sha256
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from cardrag_mcp.aggregation_profile import (
    MAX_PORTABLE_ARTIFACT_BYTES,
    MAX_SCORE_COUNT,
    ArtifactBinding,
    CorpusInventoryManifest,
    CorpusInventoryRow,
    QueryScoreCoverage,
    ScoreArtifactManifest,
    open_score_artifact,
)
from cardrag_mcp.embeddings import OpenRouterEmbedder
from cardrag_mcp.evaluation import GoldDataset, load_gold_jsonl
from cardrag_mcp.exact import VECTOR_BLOCK_ROWS, ExactCapturedRow, V5ExactRepository
from cardrag_mcp.models import ContractSearchRequest, ServingMetadata, ViewType
from cardrag_mcp.store import GenerationHandle, GenerationStore, load_generation_handle

_SOURCE_COMMIT = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_MAX_MANIFEST_BYTES = 32 * 1024 * 1024
_IDENTITY_FILE = "identity.json"
_INVENTORY_FILE = "corpus-inventory.jsonl"
_PROGRESS_FILE = "progress.json"
_LOCK_FILE = ".lock"
_QUERY_VECTOR_COUNT: Literal[4096] = 4096
_F32_BYTES = 4
_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class AggregationCaptureError(RuntimeError):
    """The score capture cannot prove a complete immutable artifact set."""


class _CaptureModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self)


class _CaptureQuery(_CaptureModel):
    query_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,511}$")
    query_sha256: Sha256Hex


class _ManifestIdentity(_CaptureModel):
    gold_sha256: Sha256Hex
    query_count: int = Field(ge=1, le=500)
    corpus_row_count: int = Field(ge=1, le=MAX_SCORE_COUNT)
    score_count: int = Field(ge=1, le=MAX_SCORE_COUNT)
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}([0-9a-f]{24})?$")
    generation_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,511}$")
    generation_manifest_sha256: Sha256Hex
    serving_database_sha256: Sha256Hex
    vector_sidecar_sha256: Sha256Hex
    exact_row_corpus_sha256: Sha256Hex
    embedding_profile_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    runtime_document_aggregation_status: Literal["candidate_default", "sealed"]
    runtime_document_aggregation_policy: Literal["max_child", "top3_mean", "contract_plus_child"]
    runtime_sealed_profile_sha256: Sha256Hex | None = None
    corpus_inventory: ArtifactBinding
    score_matrix_size_bytes: int = Field(gt=0, le=MAX_PORTABLE_ARTIFACT_BYTES)
    query_vector_matrix_size_bytes: int = Field(gt=0, le=MAX_PORTABLE_ARTIFACT_BYTES)
    validation_profile: Literal["release_grade", "fixture_only"]


class _CaptureIdentity(_CaptureModel):
    schema_version: Literal["cardrag.document-aggregation-capture-identity.v2"] = (
        "cardrag.document-aggregation-capture-identity.v2"
    )
    manifest: _ManifestIdentity
    queries: tuple[_CaptureQuery, ...] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def counts_match(self) -> Self:
        if len(self.queries) != self.manifest.query_count:
            raise ValueError("capture identity query count differs")
        return self


class _CompletedQuery(_CaptureModel):
    query_index: int = Field(ge=0, le=499)
    coverage: QueryScoreCoverage
    score_shard: ArtifactBinding
    query_vector_shard: ArtifactBinding

    @model_validator(mode="after")
    def shards_match_coverage(self) -> Self:
        if (
            self.query_index != self.coverage.ordinal
            or self.score_shard.sha256 != self.coverage.score_sha256
            or self.score_shard.size_bytes != self.coverage.score_size_bytes
            or self.query_vector_shard.sha256 != self.coverage.query_vector_sha256
            or self.query_vector_shard.size_bytes != self.coverage.query_vector_size_bytes
        ):
            raise ValueError("completed query shards differ from coverage")
        return self


class _CaptureProgress(_CaptureModel):
    schema_version: Literal["cardrag.document-aggregation-capture-progress.v2"] = (
        "cardrag.document-aggregation-capture-progress.v2"
    )
    identity_sha256: Sha256Hex
    completed_queries: tuple[_CompletedQuery, ...] = ()
    completed_chain_sha256: Sha256Hex

    @model_validator(mode="after")
    def completed_queries_are_an_ordered_hash_chain(self) -> Self:
        if tuple(item.query_index for item in self.completed_queries) != tuple(
            range(len(self.completed_queries))
        ):
            raise ValueError("capture queries must form an ordered prefix")
        expected = canonical_sha256(
            {
                "completed_queries": [
                    item.model_dump(mode="json") for item in self.completed_queries
                ],
                "identity_sha256": self.identity_sha256,
                "schema_version": "cardrag.document-aggregation-capture-chain.v2",
            }
        )
        if self.completed_chain_sha256 != expected:
            raise ValueError("capture progress hash chain is invalid")
        return self


@dataclass(frozen=True, slots=True)
class CaptureReceipt:
    output_path: Path
    corpus_inventory_output_path: Path
    score_matrix_output_path: Path
    query_vector_matrix_output_path: Path
    artifact_sha256: str
    artifact_size_bytes: int
    corpus_inventory: ArtifactBinding
    score_matrix: ArtifactBinding
    query_vector_matrix: ArtifactBinding
    query_count: int
    corpus_row_count: int
    score_count: int
    resumed_queries: int


@dataclass(frozen=True, slots=True)
class _CaptureStore:
    root: Path


@dataclass(frozen=True, slots=True)
class _FileCheckpoint:
    path: Path
    sha256: str
    size_bytes: int
    identity: tuple[int, int, int, int, int, int, int, int, int]


@dataclass(slots=True)
class _ScoreShardWriter:
    inventory: Sequence[CorpusInventoryRow]
    output: BinaryIO
    ordinal: int = 0

    def __call__(self, actual: ExactCapturedRow) -> None:
        if self.ordinal >= len(self.inventory):
            raise AggregationCaptureError("exact score stream exceeds corpus inventory")
        expected = self.inventory[self.ordinal]
        packed_score = struct.pack("<f", actual.score)
        if (
            actual.row_index != expected.row_index
            or actual.contract_revision_id != expected.contract_revision_id
            or actual.node_id != expected.node_id
            or actual.view_type != expected.view_type
            or actual.input_sha256 != expected.input_sha256
            or actual.embedding_profile_id != expected.embedding_profile_id
            or not math.isfinite(actual.score)
            or not -1.0 <= actual.score <= 1.0
            or struct.unpack("<f", packed_score)[0] != actual.score
        ):
            raise AggregationCaptureError("exact score row differs from corpus inventory")
        if self.output.write(packed_score) != len(packed_score):
            raise AggregationCaptureError("exact score shard write is incomplete")
        self.ordinal += 1


def _predicted_shape(query_count: int, corpus_row_count: int) -> tuple[int, int, int]:
    """Validate compact matrix shapes using integer arithmetic only."""

    if query_count < 1 or query_count > 500 or corpus_row_count < 1:
        raise AggregationCaptureError("compact score artifact shape is invalid")
    score_count = query_count * corpus_row_count
    score_matrix_size = score_count * _F32_BYTES
    query_vector_matrix_size = query_count * _QUERY_VECTOR_COUNT * _F32_BYTES
    if (
        score_count > MAX_SCORE_COUNT
        or score_matrix_size > MAX_PORTABLE_ARTIFACT_BYTES
        or query_vector_matrix_size > MAX_PORTABLE_ARTIFACT_BYTES
    ):
        raise AggregationCaptureError("compact score artifact prediction exceeds its hard cap")
    return score_count, score_matrix_size, query_vector_matrix_size


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


def _consume_regular_file(
    path: Path,
    *,
    maximum_bytes: int,
    collect_payload: bool,
) -> tuple[str, int, bytes | None]:
    absolute = path.absolute()
    try:
        listed = absolute.lstat()
    except FileNotFoundError:
        raise AggregationCaptureError("capture input is not a regular file") from None
    if stat.S_ISLNK(listed.st_mode) or not stat.S_ISREG(listed.st_mode):
        raise AggregationCaptureError("capture input is not a regular file")
    if listed.st_size > maximum_bytes:
        raise AggregationCaptureError("capture input exceeds its byte limit")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(absolute, flags)
    except OSError as exc:
        raise AggregationCaptureError("capture input cannot be opened safely") from exc
    digest = hashlib.sha256()
    size = 0
    payload = bytearray() if collect_payload else None
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size > maximum_bytes
            or _stat_identity(listed) != _stat_identity(before)
        ):
            raise AggregationCaptureError("capture input changed during open")
        remaining = before.st_size
        while remaining:
            block = os.read(descriptor, min(1024 * 1024, remaining))
            if not block:
                raise AggregationCaptureError("capture input changed during read")
            if len(block) > remaining or len(block) > maximum_bytes - size:
                raise AggregationCaptureError("capture input exceeds its byte limit")
            digest.update(block)
            if payload is not None:
                payload.extend(block)
            size += len(block)
            remaining -= len(block)
        if os.read(descriptor, 1):
            raise AggregationCaptureError("capture input changed during read")
        after = os.fstat(descriptor)
        try:
            current_path = absolute.lstat()
        except OSError:
            raise AggregationCaptureError("capture input changed during read") from None
        identity = _stat_identity(before)
        if (
            size != before.st_size
            or identity != _stat_identity(after)
            or identity != _stat_identity(current_path)
        ):
            raise AggregationCaptureError("capture input changed during read")
    finally:
        os.close(descriptor)
    return digest.hexdigest(), size, None if payload is None else bytes(payload)


def _sha256_file(path: Path, *, maximum_bytes: int) -> tuple[str, int]:
    digest, size, _payload = _consume_regular_file(
        path,
        maximum_bytes=maximum_bytes,
        collect_payload=False,
    )
    return digest, size


def _checkpoint_file(path: Path, *, maximum_bytes: int) -> _FileCheckpoint:
    absolute = path.absolute()
    try:
        before = absolute.lstat()
    except FileNotFoundError:
        raise AggregationCaptureError("generation artifact is missing") from None
    digest, size = _sha256_file(absolute, maximum_bytes=maximum_bytes)
    try:
        after = absolute.lstat()
    except FileNotFoundError:
        raise AggregationCaptureError("generation artifact changed during checkpoint") from None
    if _stat_identity(before) != _stat_identity(after) or size != after.st_size:
        raise AggregationCaptureError("generation artifact changed during checkpoint")
    return _FileCheckpoint(
        path=absolute,
        sha256=digest,
        size_bytes=size,
        identity=_stat_identity(after),
    )


def _verify_checkpoint_identity(checkpoint: _FileCheckpoint) -> None:
    try:
        current = checkpoint.path.lstat()
    except FileNotFoundError:
        raise AggregationCaptureError("generation artifact identity changed") from None
    if _stat_identity(current) != checkpoint.identity:
        raise AggregationCaptureError("generation artifact identity changed")


def _verify_checkpoint(checkpoint: _FileCheckpoint, *, maximum_bytes: int) -> None:
    if _checkpoint_file(checkpoint.path, maximum_bytes=maximum_bytes) != checkpoint:
        raise AggregationCaptureError("generation artifact checkpoint changed")


def _read_regular_bytes(path: Path, *, maximum_bytes: int) -> bytes:
    _digest, _size, payload = _consume_regular_file(
        path,
        maximum_bytes=maximum_bytes,
        collect_payload=True,
    )
    if payload is None:  # pragma: no cover - collect_payload=True invariant
        raise RuntimeError("capture input payload was not collected")
    return payload


def _binding(path: Path, *, maximum_bytes: int = MAX_PORTABLE_ARTIFACT_BYTES) -> ArtifactBinding:
    digest, size = _sha256_file(path, maximum_bytes=maximum_bytes)
    if size < 1:
        raise AggregationCaptureError("capture artifact is empty")
    return ArtifactBinding(sha256=digest, size_bytes=size)


def _validate_float32_file(
    path: Path,
    *,
    expected: ArtifactBinding,
    count: int,
    bounded_score: bool,
    unit_norm: bool,
) -> None:
    if expected.size_bytes != count * _F32_BYTES:
        raise AggregationCaptureError("float32 capture shard size differs from its count")
    absolute = path.absolute()
    try:
        listed = absolute.lstat()
    except FileNotFoundError:
        raise AggregationCaptureError("float32 capture shard is missing") from None
    if (
        stat.S_ISLNK(listed.st_mode)
        or not stat.S_ISREG(listed.st_mode)
        or listed.st_size != expected.size_bytes
    ):
        raise AggregationCaptureError("float32 capture shard is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(absolute, flags)
    digest = hashlib.sha256()
    square_sum = 0.0
    seen = 0
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size != expected.size_bytes
            or _stat_identity(listed) != _stat_identity(before)
        ):
            raise AggregationCaptureError("float32 capture shard changed during open")
        remaining = before.st_size
        size = 0
        while remaining:
            block = os.read(descriptor, min(1024 * 1024, remaining))
            if not block:
                raise AggregationCaptureError("float32 capture shard changed during read")
            if len(block) > remaining or len(block) > expected.size_bytes - size:
                raise AggregationCaptureError("float32 capture shard changed during read")
            if len(block) % _F32_BYTES:
                raise AggregationCaptureError("float32 capture shard is truncated")
            digest.update(block)
            values = np.frombuffer(block, dtype="<f4")
            for raw_value in values:
                value = float(raw_value)
                if not math.isfinite(value) or (bounded_score and not -1.0 <= value <= 1.0):
                    raise AggregationCaptureError("float32 capture shard has an invalid value")
                if unit_norm:
                    square_sum += value * value
            seen += len(values)
            size += len(block)
            remaining -= len(block)
        if os.read(descriptor, 1):
            raise AggregationCaptureError("float32 capture shard changed during read")
        after = os.fstat(descriptor)
        try:
            current_path = absolute.lstat()
        except OSError:
            raise AggregationCaptureError("float32 capture shard changed during read") from None
        identity = _stat_identity(before)
        if (
            seen != count
            or size != before.st_size
            or digest.hexdigest() != expected.sha256
            or identity != _stat_identity(after)
            or identity != _stat_identity(current_path)
        ):
            raise AggregationCaptureError("float32 capture shard binding is invalid")
    finally:
        os.close(descriptor)
    if unit_norm and not math.isclose(math.sqrt(square_sum), 1.0, rel_tol=1e-5, abs_tol=1e-5):
        raise AggregationCaptureError("float32 query vector is not normalized")


def _read_canonical_model[T: _CaptureModel](
    path: Path,
    model: type[T],
    *,
    maximum_bytes: int,
) -> T:
    payload = _read_regular_bytes(path, maximum_bytes=maximum_bytes)
    if not payload:
        raise AggregationCaptureError("capture state file is empty")
    try:
        value = model.model_validate_json(payload)
    except Exception as exc:
        raise AggregationCaptureError("capture state model is invalid") from exc
    if payload != value.canonical_bytes():
        raise AggregationCaptureError("capture state bytes are not canonical")
    return value


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_immutable(path: Path, payload: bytes) -> ArtifactBinding:
    if not payload or len(payload) > MAX_PORTABLE_ARTIFACT_BYTES:
        raise AggregationCaptureError("immutable capture payload has an invalid size")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise AggregationCaptureError("immutable capture output must not be a symlink")
    descriptor, name = tempfile.mkstemp(prefix=".capture-publish-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            existing = _binding(path)
            expected = ArtifactBinding(
                sha256=hashlib.sha256(payload).hexdigest(), size_bytes=len(payload)
            )
            if existing != expected:
                raise AggregationCaptureError("immutable capture file already differs") from None
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return ArtifactBinding(sha256=hashlib.sha256(payload).hexdigest(), size_bytes=len(payload))


def _replace_progress(path: Path, progress: _CaptureProgress) -> None:
    if path.is_symlink():
        raise AggregationCaptureError("capture progress must not be a symlink")
    descriptor, name = tempfile.mkstemp(prefix=".capture-progress-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(progress.canonical_bytes())
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _progress(identity_sha256: str, queries: Sequence[_CompletedQuery]) -> _CaptureProgress:
    chain_sha256 = canonical_sha256(
        {
            "completed_queries": [item.model_dump(mode="json") for item in queries],
            "identity_sha256": identity_sha256,
            "schema_version": "cardrag.document-aggregation-capture-chain.v2",
        }
    )
    return _CaptureProgress(
        identity_sha256=identity_sha256,
        completed_queries=tuple(queries),
        completed_chain_sha256=chain_sha256,
    )


def _state_directory(path: Path) -> Path:
    if path.is_symlink():
        raise AggregationCaptureError("capture state directory must not be a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    resolved = path.resolve(strict=True)
    mode = resolved.stat().st_mode
    if not stat.S_ISDIR(mode) or stat.S_IMODE(mode) & 0o077:
        raise AggregationCaptureError("capture state directory must be private")
    return resolved


@contextmanager
def _capture_lock(state: Path) -> Iterator[None]:
    path = state / _LOCK_FILE
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise AggregationCaptureError("capture lock is not a regular file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AggregationCaptureError("capture state is already in use") from exc
        yield
    finally:
        os.close(descriptor)


def _load_generation_manifest(path: Path) -> tuple[GenerationManifest, str]:
    payload = _read_regular_bytes(path, maximum_bytes=_MAX_MANIFEST_BYTES)
    if not payload:
        raise AggregationCaptureError("generation manifest is empty")
    digest = hashlib.sha256(payload).hexdigest()
    try:
        manifest = GenerationManifest.model_validate_json(payload)
    except Exception as exc:
        raise AggregationCaptureError("generation manifest is invalid") from exc
    if manifest.schema_version != "cardrag.generation.v5":
        raise AggregationCaptureError("score capture requires generation v5")
    if payload != manifest.canonical_bytes():
        raise AggregationCaptureError("generation manifest is not canonical")
    return manifest, digest


def _verify_generation_files(
    manifest: GenerationManifest,
    generation_directory: Path,
) -> tuple[_FileCheckpoint, _FileCheckpoint]:
    database_path = generation_directory / "index.sqlite3"
    vector_path = generation_directory / "vectors.f32"
    database = _checkpoint_file(
        database_path,
        maximum_bytes=4 * 1024 * 1024 * 1024,
    )
    vector = _checkpoint_file(
        vector_path,
        maximum_bytes=64 * 1024 * 1024 * 1024,
    )
    sidecar = manifest.vector_sidecar
    if sidecar is None:
        raise AggregationCaptureError("generation manifest has no v5 vector sidecar")
    if (
        manifest.serving_database.sha256 != database.sha256
        or manifest.serving_database.size_bytes != database.size_bytes
        or sidecar.artifact.sha256 != vector.sha256
        or sidecar.artifact.size_bytes != vector.size_bytes
    ):
        raise AggregationCaptureError("generation files differ from their manifest")
    return database, vector


def _verify_runtime_binding(manifest: GenerationManifest, metadata: ServingMetadata) -> None:
    profile = manifest.document_aggregation_profile
    if profile is None:
        if (
            metadata.document_aggregation_status != "candidate_default"
            or metadata.document_aggregation_policy != "max_child"
            or metadata.sealed_profile_sha256 is not None
        ):
            raise AggregationCaptureError("candidate runtime aggregation identity is inconsistent")
        return
    if (
        metadata.document_aggregation_status != "sealed"
        or metadata.document_aggregation_policy != profile.aggregation_policy
        or metadata.sealed_profile_sha256 != manifest.sealed_profile_sha256
        or metadata.exact_row_corpus_sha256 != manifest.exact_row_corpus_sha256
    ):
        raise AggregationCaptureError("sealed runtime aggregation identity is inconsistent")


def _query_paths(state: Path, query_index: int) -> tuple[Path, Path, Path]:
    stem = f"query-{query_index:03d}"
    return (
        state / f"{stem}.coverage.json",
        state / f"{stem}.scores.f32",
        state / f"{stem}.query-vector.f32",
    )


def _validate_state_entries(state: Path, query_count: int) -> None:
    allowed = {_IDENTITY_FILE, _INVENTORY_FILE, _PROGRESS_FILE, _LOCK_FILE}
    for index in range(query_count):
        allowed.update(path.name for path in _query_paths(state, index))
    for entry in state.iterdir():
        if entry.name not in allowed or entry.is_symlink():
            raise AggregationCaptureError("capture state contains an unsafe entry")
        if not entry.is_file():
            raise AggregationCaptureError("capture state entry is not a regular file")


def _inventory_bytes(
    *,
    handle: GenerationHandle,
    active_ids: set[str],
    generation_id: str,
    database_sha256: str,
    vector_sha256: str,
    exact_row_corpus_sha256: str,
    embedding_profile_id: str,
) -> tuple[bytes, tuple[CorpusInventoryRow, ...]]:
    vectors = V5ExactRepository._vectors(handle)
    selected = {
        index
        for index, revision_id in enumerate(vectors.contract_revision_ids)
        if revision_id in active_ids
    }
    with handle.connect() as connection:
        database_rows = {
            int(row[0]): (
                str(row[1]),
                str(row[2]),
                cast(ViewType, str(row[3])),
                str(row[4]),
                str(row[5]),
            )
            for row in connection.execute(
                """SELECT row_index,contract_revision_id,node_id,view_type,
                          input_sha256,profile_id
                     FROM embedding_views ORDER BY row_index"""
            )
            if int(row[0]) in selected
        }
    rows: list[CorpusInventoryRow] = []
    for row_index in sorted(selected):
        database = database_rows.get(row_index)
        if (
            vectors.row_indices[row_index] != row_index
            or database is None
            or database[:3]
            != (
                vectors.contract_revision_ids[row_index],
                vectors.node_ids[row_index],
                vectors.view_types[row_index],
            )
            or database[4] != vectors.profile_ids[row_index]
            or database[4] != embedding_profile_id
        ):
            raise AggregationCaptureError("exact corpus inventory provenance differs")
        rows.append(
            CorpusInventoryRow(
                schema_version="cardrag.document-aggregation-corpus-row.v1",
                ordinal=len(rows),
                row_index=row_index,
                contract_revision_id=database[0],
                node_id=database[1],
                view_type=database[2],
                input_sha256=database[3],
                embedding_profile_id=database[4],
            )
        )
    if not rows or {row.contract_revision_id for row in rows} != active_ids:
        raise AggregationCaptureError("exact corpus inventory coverage is incomplete")
    manifest = CorpusInventoryManifest(
        schema_version="cardrag.document-aggregation-corpus-inventory.v1",
        generation_id=generation_id,
        serving_database_sha256=database_sha256,
        vector_sidecar_sha256=vector_sha256,
        exact_row_corpus_sha256=exact_row_corpus_sha256,
        embedding_profile_id=embedding_profile_id,
        corpus_row_count=len(rows),
    )
    payload = bytearray(canonical_json_bytes(manifest) + b"\n")
    for row in rows:
        encoded = canonical_json_bytes(row) + b"\n"
        if len(encoded) > MAX_PORTABLE_ARTIFACT_BYTES - len(payload):
            raise AggregationCaptureError("corpus inventory exceeds its portable byte cap")
        payload.extend(encoded)
    return bytes(payload), tuple(rows)


def _parse_completed_query(
    state: Path,
    *,
    query_index: int,
    query: _CaptureQuery,
    corpus_row_count: int,
    active_contracts: int,
) -> _CompletedQuery | None:
    coverage_path, score_path, vector_path = _query_paths(state, query_index)
    present = (coverage_path.exists(), score_path.exists(), vector_path.exists())
    if not any(present):
        return None
    if not all(present):
        raise AggregationCaptureError("capture query trio is partial or orphaned")
    coverage_payload = _read_regular_bytes(coverage_path, maximum_bytes=_MAX_MANIFEST_BYTES)
    try:
        coverage = QueryScoreCoverage.model_validate_json(coverage_payload)
    except Exception as exc:
        raise AggregationCaptureError("capture query coverage is invalid") from exc
    if coverage_payload != canonical_json_bytes(coverage) + b"\n":
        raise AggregationCaptureError("capture query coverage is not canonical")
    score_binding = ArtifactBinding(
        sha256=coverage.score_sha256,
        size_bytes=coverage.score_size_bytes,
    )
    vector_binding = ArtifactBinding(
        sha256=coverage.query_vector_sha256,
        size_bytes=coverage.query_vector_size_bytes,
    )
    if (
        coverage.ordinal != query_index
        or coverage.query_id != query.query_id
        or coverage.query_sha256 != query.query_sha256
        or coverage.expected_rows != corpus_row_count
        or coverage.active_contracts != active_contracts
        or coverage.score_offset_bytes != query_index * corpus_row_count * _F32_BYTES
        or coverage.query_vector_offset_bytes != query_index * _QUERY_VECTOR_COUNT * _F32_BYTES
    ):
        raise AggregationCaptureError("capture query trio binding is invalid")
    _validate_float32_file(
        score_path,
        expected=score_binding,
        count=corpus_row_count,
        bounded_score=True,
        unit_norm=False,
    )
    _validate_float32_file(
        vector_path,
        expected=vector_binding,
        count=_QUERY_VECTOR_COUNT,
        bounded_score=False,
        unit_norm=True,
    )
    return _CompletedQuery(
        query_index=query_index,
        coverage=coverage,
        score_shard=score_binding,
        query_vector_shard=vector_binding,
    )


def _publish_combined(
    output_path: Path,
    shard_paths: Sequence[Path],
    *,
    expected_size: int,
) -> ArtifactBinding:
    if expected_size < 1 or expected_size > MAX_PORTABLE_ARTIFACT_BYTES:
        raise AggregationCaptureError("combined artifact predicted size exceeds its cap")
    if output_path.is_symlink():
        raise AggregationCaptureError("combined artifact output must not be a symlink")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".aggregation-combine-", dir=output_path.parent)
    temporary = Path(name)
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(descriptor, "wb") as output:
            for shard in shard_paths:
                with shard.open("rb") as source:
                    while block := source.read(1024 * 1024):
                        if len(block) > expected_size - size:
                            raise AggregationCaptureError(
                                "combined artifact exceeds predicted size"
                            )
                        digest.update(block)
                        size += len(block)
                        output.write(block)
            if size != expected_size:
                raise AggregationCaptureError("combined artifact is truncated")
            output.flush()
            os.fsync(output.fileno())
        expected = ArtifactBinding(sha256=digest.hexdigest(), size_bytes=size)
        if output_path.exists():
            if _binding(output_path) != expected:
                raise AggregationCaptureError("existing combined artifact differs")
        else:
            os.link(temporary, output_path, follow_symlinks=False)
            _fsync_directory(output_path.parent)
        return expected
    finally:
        temporary.unlink(missing_ok=True)


def _publish_immutable_file(
    source_path: Path,
    output_path: Path,
    *,
    expected: ArtifactBinding,
) -> ArtifactBinding:
    if _binding(source_path) != expected:
        raise AggregationCaptureError("immutable capture source binding differs")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.is_symlink():
        raise AggregationCaptureError("immutable capture output must not be a symlink")
    try:
        os.link(source_path, output_path, follow_symlinks=False)
    except FileExistsError:
        if _binding(output_path) != expected:
            raise AggregationCaptureError("immutable capture file already differs") from None
    _fsync_directory(output_path.parent)
    return expected


def _manifest_from_identity(
    identity: _ManifestIdentity,
    *,
    score_matrix: ArtifactBinding,
    query_vector_matrix: ArtifactBinding,
) -> ScoreArtifactManifest:
    return ScoreArtifactManifest(
        schema_version="cardrag.document-aggregation-score-artifact.v2",
        gold_sha256=identity.gold_sha256,
        query_count=identity.query_count,
        corpus_row_count=identity.corpus_row_count,
        score_count=identity.score_count,
        source_commit=identity.source_commit,
        generation_id=identity.generation_id,
        generation_manifest_sha256=identity.generation_manifest_sha256,
        serving_database_sha256=identity.serving_database_sha256,
        vector_sidecar_sha256=identity.vector_sidecar_sha256,
        exact_row_corpus_sha256=identity.exact_row_corpus_sha256,
        embedding_profile_id=identity.embedding_profile_id,
        embedding_model="qwen/qwen3-embedding-8b",
        embedding_dimension=4096,
        exact=True,
        approximate=False,
        scoring_contract="cardrag.v5-exact-row-score.v1",
        temporal_scope_policy="gold-query.v1",
        runtime_document_aggregation_status=identity.runtime_document_aggregation_status,
        runtime_document_aggregation_policy=identity.runtime_document_aggregation_policy,
        runtime_sealed_profile_sha256=identity.runtime_sealed_profile_sha256,
        corpus_inventory=identity.corpus_inventory,
        score_matrix=score_matrix,
        query_vector_matrix=query_vector_matrix,
        byte_order="little-endian",
        scalar_type="float32",
        matrix_order="row-major",
        validation_profile=identity.validation_profile,
    )


async def capture_score_artifact(
    *,
    gold_path: Path,
    generation_manifest_path: Path,
    generation_directory: Path,
    object_root: Path,
    output_path: Path,
    corpus_inventory_output_path: Path,
    score_matrix_output_path: Path,
    query_vector_matrix_output_path: Path,
    state_directory: Path,
    source_commit: str,
    embedder: OpenRouterEmbedder,
    release_gate: bool = True,
) -> CaptureReceipt:
    """Capture or resume compact v2 score evidence without recalling completed queries."""

    if _SOURCE_COMMIT.fullmatch(source_commit) is None:
        raise AggregationCaptureError("source commit is invalid")
    outputs = tuple(
        path.absolute()
        for path in (
            output_path,
            corpus_inventory_output_path,
            score_matrix_output_path,
            query_vector_matrix_output_path,
        )
    )
    if len(set(outputs)) != len(outputs):
        raise AggregationCaptureError("capture output paths must be distinct")
    gold: GoldDataset = load_gold_jsonl(gold_path, release_gate=release_gate)
    generation_manifest, generation_manifest_sha256 = _load_generation_manifest(
        generation_manifest_path
    )
    generation_directory = await asyncio.to_thread(generation_directory.resolve, strict=True)
    if generation_directory.name != generation_manifest.generation_id:
        raise AggregationCaptureError("generation directory differs from its manifest")
    database_checkpoint, vector_checkpoint = _verify_generation_files(
        generation_manifest, generation_directory
    )
    database_sha256 = database_checkpoint.sha256
    vector_sha256 = vector_checkpoint.sha256
    handle = load_generation_handle(
        generation_directory,
        object_root,
        maximum_vector_bytes=1024 * 1024 * 1024,
        maximum_vector_sidecar_bytes=64 * 1024 * 1024 * 1024,
        maximum_resident_vector_bytes=1024 * 1024 * 1024,
        maximum_database_bytes=4 * 1024 * 1024 * 1024,
        expected_generation_id=generation_manifest.generation_id,
        expected_embedding_model=generation_manifest.embedding_contract.model,
        expected_embedding_count=generation_manifest.embedding_contract.count,
    )
    await asyncio.to_thread(_verify_checkpoint_identity, database_checkpoint)
    await asyncio.to_thread(_verify_checkpoint_identity, vector_checkpoint)
    _verify_runtime_binding(generation_manifest, handle.metadata)
    vectors = V5ExactRepository._vectors(handle)
    exact_row_corpus_sha256 = handle.metadata.exact_row_corpus_sha256
    embedding_profile_id = handle.metadata.primary_embedding_profile_id
    if exact_row_corpus_sha256 is None or embedding_profile_id is None:
        raise AggregationCaptureError("v5 exact-row identity is absent")
    active_revisions = V5ExactRepository._active_revisions(
        handle, ContractSearchRequest(query=gold.queries[0].question)
    )
    active_ids = {item.contract_revision_id for item in active_revisions}
    query_count = len(gold.queries)
    expected_corpus_rows = sum(
        revision_id in active_ids for revision_id in vectors.contract_revision_ids
    )
    score_count, score_matrix_size, query_vector_matrix_size = _predicted_shape(
        query_count, expected_corpus_rows
    )
    inventory_payload, inventory_rows = _inventory_bytes(
        handle=handle,
        active_ids=active_ids,
        generation_id=generation_manifest.generation_id,
        database_sha256=database_sha256,
        vector_sha256=vector_sha256,
        exact_row_corpus_sha256=exact_row_corpus_sha256,
        embedding_profile_id=embedding_profile_id,
    )
    await asyncio.to_thread(_verify_checkpoint_identity, database_checkpoint)
    await asyncio.to_thread(_verify_checkpoint_identity, vector_checkpoint)
    corpus_row_count = len(inventory_rows)
    if corpus_row_count != expected_corpus_rows:
        raise AggregationCaptureError("corpus inventory differs from predicted row coverage")

    state = _state_directory(state_directory)
    resolved_outputs = await asyncio.gather(
        *(asyncio.to_thread(path.resolve, strict=False) for path in outputs)
    )
    if len(set(resolved_outputs)) != len(resolved_outputs):
        raise AggregationCaptureError("capture output paths resolve to the same target")
    if any(path == state or state in path.parents for path in resolved_outputs):
        raise AggregationCaptureError("final outputs must be outside capture state")
    with _capture_lock(state):
        inventory_state_path = state / _INVENTORY_FILE
        inventory_binding = _publish_immutable(inventory_state_path, inventory_payload)
        manifest_identity = _ManifestIdentity(
            gold_sha256=gold.sha256,
            query_count=query_count,
            corpus_row_count=corpus_row_count,
            score_count=score_count,
            source_commit=source_commit,
            generation_id=generation_manifest.generation_id,
            generation_manifest_sha256=generation_manifest_sha256,
            serving_database_sha256=database_sha256,
            vector_sidecar_sha256=vector_sha256,
            exact_row_corpus_sha256=exact_row_corpus_sha256,
            embedding_profile_id=embedding_profile_id,
            runtime_document_aggregation_status=handle.metadata.document_aggregation_status,
            runtime_document_aggregation_policy=handle.metadata.document_aggregation_policy,
            runtime_sealed_profile_sha256=handle.metadata.sealed_profile_sha256,
            corpus_inventory=inventory_binding,
            score_matrix_size_bytes=score_matrix_size,
            query_vector_matrix_size_bytes=query_vector_matrix_size,
            validation_profile="release_grade" if release_gate else "fixture_only",
        )
        identity = _CaptureIdentity(
            manifest=manifest_identity,
            queries=tuple(
                _CaptureQuery(
                    query_id=query.query_id,
                    query_sha256=hashlib.sha256(query.question.encode("utf-8")).hexdigest(),
                )
                for query in gold.queries
            ),
        )
        identity_sha256 = canonical_sha256(identity)
        identity_path = state / _IDENTITY_FILE
        _publish_immutable(identity_path, identity.canonical_bytes())
        persisted_identity = _read_canonical_model(
            identity_path, _CaptureIdentity, maximum_bytes=_MAX_MANIFEST_BYTES
        )
        if persisted_identity != identity:
            raise AggregationCaptureError("capture resume identity differs")
        if _binding(inventory_state_path) != inventory_binding:
            raise AggregationCaptureError("capture inventory changed")
        _validate_state_entries(state, query_count)

        completed: list[_CompletedQuery] = []
        gap_seen = False
        for index, identity_query in enumerate(identity.queries):
            item = _parse_completed_query(
                state,
                query_index=index,
                query=identity_query,
                corpus_row_count=corpus_row_count,
                active_contracts=len(active_ids),
            )
            if item is None:
                gap_seen = True
            elif gap_seen:
                raise AggregationCaptureError("capture query trios skip an ordinal")
            else:
                completed.append(item)

        progress_path = state / _PROGRESS_FILE
        persisted_progress = (
            None
            if not progress_path.exists()
            else _read_canonical_model(
                progress_path, _CaptureProgress, maximum_bytes=_MAX_MANIFEST_BYTES
            )
        )
        if persisted_progress is not None:
            if persisted_progress.identity_sha256 != identity_sha256:
                raise AggregationCaptureError("capture progress belongs to another identity")
            progress_count = len(persisted_progress.completed_queries)
            if tuple(completed[:progress_count]) != persisted_progress.completed_queries:
                raise AggregationCaptureError("capture progress differs from immutable trios")
        if persisted_progress is None or len(persisted_progress.completed_queries) < len(completed):
            _replace_progress(progress_path, _progress(identity_sha256, completed))
        resumed_queries = len(completed)
        repository = V5ExactRepository(cast(GenerationStore, _CaptureStore(state)), embedder)

        for index, gold_query in enumerate(gold.queries[len(completed) :], start=len(completed)):
            await asyncio.to_thread(_verify_checkpoint_identity, database_checkpoint)
            await asyncio.to_thread(_verify_checkpoint_identity, vector_checkpoint)
            coverage_path, score_path, vector_path = _query_paths(state, index)
            descriptor, temporary_name = tempfile.mkstemp(prefix=".capture-score-shard-", dir=state)
            temporary_score = Path(temporary_name)
            score_ordinal = 0
            try:
                with os.fdopen(descriptor, "wb") as score_output:
                    writer = _ScoreShardWriter(inventory_rows, score_output)
                    captured = await repository.capture_unscoped_current_score_stream(
                        gold_query.question,
                        handle,
                        score_sink=writer,
                        block_rows=VECTOR_BLOCK_ROWS,
                    )
                    score_ordinal = writer.ordinal
                    score_output.flush()
                    os.fsync(score_output.fileno())
                score_binding = _binding(temporary_score)
                _publish_immutable_file(
                    temporary_score,
                    score_path,
                    expected=score_binding,
                )
            finally:
                await asyncio.to_thread(temporary_score.unlink, missing_ok=True)
            if (
                captured.query_sha256 != identity.queries[index].query_sha256
                or captured.expected_active_contracts != len(active_ids)
                or captured.expected_rows != corpus_row_count
                or captured.scored_rows != corpus_row_count
                or score_ordinal != corpus_row_count
            ):
                raise AggregationCaptureError("exact score capture coverage is incomplete")
            await asyncio.to_thread(_verify_checkpoint_identity, database_checkpoint)
            await asyncio.to_thread(_verify_checkpoint_identity, vector_checkpoint)
            vector_payload = captured.query_vector_f32
            if (
                score_binding.size_bytes != corpus_row_count * _F32_BYTES
                or len(vector_payload) != _QUERY_VECTOR_COUNT * _F32_BYTES
                or hashlib.sha256(vector_payload).hexdigest() != captured.query_vector_sha256
            ):
                raise AggregationCaptureError("exact capture bytes are invalid")
            coverage = QueryScoreCoverage(
                schema_version="cardrag.document-aggregation-query-coverage.v2",
                ordinal=index,
                query_id=gold_query.query_id,
                query_sha256=captured.query_sha256,
                expected_rows=captured.expected_rows,
                scored_rows=captured.scored_rows,
                active_contracts=captured.expected_active_contracts,
                score_offset_bytes=index * corpus_row_count * _F32_BYTES,
                score_size_bytes=score_binding.size_bytes,
                score_count=captured.scored_rows,
                score_sha256=score_binding.sha256,
                query_vector_offset_bytes=index * _QUERY_VECTOR_COUNT * _F32_BYTES,
                query_vector_size_bytes=len(vector_payload),
                query_vector_count=_QUERY_VECTOR_COUNT,
                query_vector_sha256=captured.query_vector_sha256,
            )
            vector_binding = _publish_immutable(vector_path, vector_payload)
            _publish_immutable(coverage_path, canonical_json_bytes(coverage) + b"\n")
            item = _parse_completed_query(
                state,
                query_index=index,
                query=identity.queries[index],
                corpus_row_count=corpus_row_count,
                active_contracts=len(active_ids),
            )
            if item is None:  # pragma: no cover - all three files were just published
                raise AggregationCaptureError("published capture query trio disappeared")
            if item.score_shard != score_binding or item.query_vector_shard != vector_binding:
                raise AggregationCaptureError("published capture query trio differs")
            completed.append(item)
            _replace_progress(progress_path, _progress(identity_sha256, completed))

        score_matrix_binding = _publish_combined(
            score_matrix_output_path,
            tuple(_query_paths(state, index)[1] for index in range(query_count)),
            expected_size=score_matrix_size,
        )
        query_vector_matrix_binding = _publish_combined(
            query_vector_matrix_output_path,
            tuple(_query_paths(state, index)[2] for index in range(query_count)),
            expected_size=query_vector_matrix_size,
        )
        published_inventory = _publish_immutable(corpus_inventory_output_path, inventory_payload)
        if published_inventory != inventory_binding:
            raise AggregationCaptureError("published inventory differs from capture state")
        score_manifest = _manifest_from_identity(
            manifest_identity,
            score_matrix=score_matrix_binding,
            query_vector_matrix=query_vector_matrix_binding,
        )
        artifact_payload = (
            canonical_json_bytes(score_manifest)
            + b"\n"
            + b"".join(canonical_json_bytes(item.coverage) + b"\n" for item in completed)
        )
        artifact_binding = _publish_immutable(output_path, artifact_payload)
        with open_score_artifact(
            output_path,
            corpus_inventory_output_path,
            score_matrix_output_path,
            query_vector_matrix_output_path,
            artifact_binding.sha256,
        ) as verified:
            if (
                verified.manifest != score_manifest
                or verified.corpus_inventory_binding != inventory_binding
                or verified.score_matrix_binding != score_matrix_binding
                or verified.query_vector_matrix_binding != query_vector_matrix_binding
                or verified.coverages != tuple(item.coverage for item in completed)
            ):
                raise AggregationCaptureError("published compact evidence differs after validation")
        await asyncio.to_thread(
            _verify_checkpoint,
            database_checkpoint,
            maximum_bytes=4 * 1024 * 1024 * 1024,
        )
        await asyncio.to_thread(
            _verify_checkpoint,
            vector_checkpoint,
            maximum_bytes=64 * 1024 * 1024 * 1024,
        )

    return CaptureReceipt(
        output_path=await asyncio.to_thread(output_path.resolve, strict=True),
        corpus_inventory_output_path=await asyncio.to_thread(
            corpus_inventory_output_path.resolve, strict=True
        ),
        score_matrix_output_path=await asyncio.to_thread(
            score_matrix_output_path.resolve, strict=True
        ),
        query_vector_matrix_output_path=await asyncio.to_thread(
            query_vector_matrix_output_path.resolve, strict=True
        ),
        artifact_sha256=artifact_binding.sha256,
        artifact_size_bytes=artifact_binding.size_bytes,
        corpus_inventory=inventory_binding,
        score_matrix=score_matrix_binding,
        query_vector_matrix=query_vector_matrix_binding,
        query_count=query_count,
        corpus_row_count=corpus_row_count,
        score_count=score_count,
        resumed_queries=resumed_queries,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--generation-manifest", type=Path, required=True)
    parser.add_argument("--generation-dir", type=Path, required=True)
    parser.add_argument("--object-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--corpus-inventory-output", type=Path, required=True)
    parser.add_argument("--score-matrix-output", type=Path, required=True)
    parser.add_argument("--query-vector-matrix-output", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--openrouter-base-url", default="https://openrouter.ai/api/v1")
    parser.add_argument("--openrouter-api-key-file", type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument("--fixture-mode", action="store_true")
    return parser


async def _async_main(arguments: argparse.Namespace) -> CaptureReceipt:
    base_url = _validated_openrouter_base_url(str(arguments.openrouter_base_url))
    api_key: str | None = os.environ.get("OPENROUTER_API_KEY")
    if arguments.openrouter_api_key_file is not None:
        api_key = await asyncio.to_thread(
            _read_api_key_file, cast(Path, arguments.openrouter_api_key_file)
        )
    embedder = OpenRouterEmbedder(
        base_url=base_url,
        api_key=api_key,
        timeout_seconds=float(arguments.timeout_seconds),
    )
    try:
        return await capture_score_artifact(
            gold_path=cast(Path, arguments.gold),
            generation_manifest_path=cast(Path, arguments.generation_manifest),
            generation_directory=cast(Path, arguments.generation_dir),
            object_root=cast(Path, arguments.object_root),
            output_path=cast(Path, arguments.output),
            corpus_inventory_output_path=cast(Path, arguments.corpus_inventory_output),
            score_matrix_output_path=cast(Path, arguments.score_matrix_output),
            query_vector_matrix_output_path=cast(Path, arguments.query_vector_matrix_output),
            state_directory=cast(Path, arguments.state_dir),
            source_commit=cast(str, arguments.source_commit),
            embedder=embedder,
            release_gate=not cast(bool, arguments.fixture_mode),
        )
    finally:
        await embedder.close()


def _read_api_key_file(path: Path) -> str:
    payload = _read_regular_bytes(path, maximum_bytes=16 * 1024)
    try:
        value = payload.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise AggregationCaptureError("OpenRouter key file is invalid") from exc
    if not value or any(character.isspace() for character in value):
        raise AggregationCaptureError("OpenRouter key file is empty")
    return value


def _validated_openrouter_base_url(value: str) -> str:
    if value not in {_OPENROUTER_BASE_URL, f"{_OPENROUTER_BASE_URL}/"}:
        raise AggregationCaptureError("OpenRouter base URL must be the official API endpoint")
    return _OPENROUTER_BASE_URL


def main() -> None:
    try:
        receipt = asyncio.run(_async_main(_parser().parse_args()))
    except Exception as exc:
        raise SystemExit(f"aggregation score capture failed: {exc}") from exc
    print(
        json.dumps(
            {
                "artifact_sha256": receipt.artifact_sha256,
                "artifact_size_bytes": receipt.artifact_size_bytes,
                "corpus_inventory": receipt.corpus_inventory.model_dump(mode="json"),
                "corpus_inventory_output_path": str(receipt.corpus_inventory_output_path),
                "output_path": str(receipt.output_path),
                "query_count": receipt.query_count,
                "query_vector_matrix": receipt.query_vector_matrix.model_dump(mode="json"),
                "query_vector_matrix_output_path": str(receipt.query_vector_matrix_output_path),
                "resumed_queries": receipt.resumed_queries,
                "corpus_row_count": receipt.corpus_row_count,
                "score_count": receipt.score_count,
                "score_matrix": receipt.score_matrix.model_dump(mode="json"),
                "score_matrix_output_path": str(receipt.score_matrix_output_path),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
