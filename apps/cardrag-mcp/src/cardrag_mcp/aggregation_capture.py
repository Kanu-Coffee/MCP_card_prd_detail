"""Capture every actual v5 exact-row score into a resumable sealed JSONL artifact."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Self, cast

from cardrag_core import GenerationManifest, canonical_json_bytes, canonical_sha256
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from cardrag_mcp.aggregation_profile import (
    MAX_SCORE_ARTIFACT_BYTES,
    QueryScoreCoverage,
    RowScore,
    ScoreArtifactManifest,
)
from cardrag_mcp.embeddings import OpenRouterEmbedder
from cardrag_mcp.evaluation import GoldDataset, load_gold_jsonl
from cardrag_mcp.exact import V5ExactRepository
from cardrag_mcp.models import ContractSearchRequest, ServingMetadata
from cardrag_mcp.store import GenerationStore, load_generation_handle

_SOURCE_COMMIT = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_MANIFEST_BYTES = 32 * 1024 * 1024
_MAX_SHARD_BYTES = 4 * 1024 * 1024 * 1024
_IDENTITY_FILE = "identity.json"
_PROGRESS_FILE = "progress.json"
_LOCK_FILE = ".lock"
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class AggregationCaptureError(RuntimeError):
    """The score capture cannot prove a complete immutable artifact."""


class _CaptureModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self)


class _CaptureQuery(_CaptureModel):
    query_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,511}$")
    query_sha256: Sha256Hex


class _CaptureIdentity(_CaptureModel):
    schema_version: Literal["cardrag.document-aggregation-capture-identity.v1"] = (
        "cardrag.document-aggregation-capture-identity.v1"
    )
    score_manifest: ScoreArtifactManifest
    queries: tuple[_CaptureQuery, ...] = Field(min_length=1, max_length=500)


class _CompletedShard(_CaptureModel):
    query_index: int = Field(ge=0, le=499)
    query_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,511}$")
    query_vector_sha256: Sha256Hex
    shard_sha256: Sha256Hex
    shard_size_bytes: int = Field(gt=0, le=_MAX_SHARD_BYTES)
    scored_rows: int = Field(gt=0)


class _CaptureProgress(_CaptureModel):
    schema_version: Literal["cardrag.document-aggregation-capture-progress.v1"] = (
        "cardrag.document-aggregation-capture-progress.v1"
    )
    identity_sha256: Sha256Hex
    completed_shards: tuple[_CompletedShard, ...] = ()
    completed_chain_sha256: Sha256Hex

    @model_validator(mode="after")
    def completed_shards_are_an_ordered_hash_chain(self) -> Self:
        if tuple(item.query_index for item in self.completed_shards) != tuple(
            range(len(self.completed_shards))
        ):
            raise ValueError("capture shards must form an ordered prefix")
        expected = canonical_sha256(
            {
                "completed_shards": [
                    item.model_dump(mode="json") for item in self.completed_shards
                ],
                "identity_sha256": self.identity_sha256,
                "schema_version": "cardrag.document-aggregation-capture-chain.v1",
            }
        )
        if self.completed_chain_sha256 != expected:
            raise ValueError("capture progress hash chain is invalid")
        return self


@dataclass(frozen=True, slots=True)
class CaptureReceipt:
    output_path: Path
    artifact_sha256: str
    artifact_size_bytes: int
    query_count: int
    row_count: int
    resumed_queries: int


@dataclass(frozen=True, slots=True)
class _CaptureStore:
    root: Path


def _sha256_file(path: Path, *, maximum_bytes: int) -> tuple[str, int]:
    if path.is_symlink() or not path.is_file():
        raise AggregationCaptureError("capture input is not a regular file")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            if len(block) > maximum_bytes - size:
                raise AggregationCaptureError("capture input exceeds its byte limit")
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _read_canonical_model[T: _CaptureModel](
    path: Path,
    model: type[T],
    *,
    maximum_bytes: int,
) -> T:
    _digest, size = _sha256_file(path, maximum_bytes=maximum_bytes)
    if size < 1:
        raise AggregationCaptureError("capture state file is empty")
    payload = path.read_bytes()
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


def _publish_immutable(path: Path, payload: bytes) -> None:
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
            if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
                raise AggregationCaptureError("immutable capture file already differs") from None
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


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


def _progress(identity_sha256: str, shards: Sequence[_CompletedShard]) -> _CaptureProgress:
    chain_sha256 = canonical_sha256(
        {
            "completed_shards": [item.model_dump(mode="json") for item in shards],
            "identity_sha256": identity_sha256,
            "schema_version": "cardrag.document-aggregation-capture-chain.v1",
        }
    )
    return _CaptureProgress(
        identity_sha256=identity_sha256,
        completed_shards=tuple(shards),
        completed_chain_sha256=chain_sha256,
    )


def _state_directory(path: Path) -> Path:
    if path.is_symlink():
        raise AggregationCaptureError("capture state directory must not be a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    resolved = path.resolve(strict=True)
    if not stat.S_ISDIR(resolved.stat().st_mode):
        raise AggregationCaptureError("capture state path is not a directory")
    return resolved


def _load_generation_manifest(path: Path) -> tuple[GenerationManifest, str]:
    digest, size = _sha256_file(path, maximum_bytes=_MAX_MANIFEST_BYTES)
    if size < 1:
        raise AggregationCaptureError("generation manifest is empty")
    payload = path.read_bytes()
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
) -> tuple[str, str]:
    database_path = generation_directory / "index.sqlite3"
    vector_path = generation_directory / "vectors.f32"
    database_sha256, database_size = _sha256_file(
        database_path,
        maximum_bytes=4 * 1024 * 1024 * 1024,
    )
    vector_sha256, vector_size = _sha256_file(
        vector_path,
        maximum_bytes=64 * 1024 * 1024 * 1024,
    )
    sidecar = manifest.vector_sidecar
    if sidecar is None:
        raise AggregationCaptureError("generation manifest has no v5 vector sidecar")
    if (
        manifest.serving_database.sha256 != database_sha256
        or manifest.serving_database.size_bytes != database_size
        or sidecar.artifact.sha256 != vector_sha256
        or sidecar.artifact.size_bytes != vector_size
    ):
        raise AggregationCaptureError("generation files differ from their manifest")
    return database_sha256, vector_sha256


def _verify_runtime_binding(
    manifest: GenerationManifest,
    metadata: ServingMetadata,
) -> None:
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


def _query_shard_path(state: Path, query_index: int) -> Path:
    return state / f"query-{query_index:03d}.jsonl"


def _parse_query_shard(
    path: Path,
    *,
    query_index: int,
    query_id: str,
    query_sha256: str,
    embedding_profile_id: str,
) -> _CompletedShard:
    digest, size = _sha256_file(path, maximum_bytes=_MAX_SHARD_BYTES)
    if size < 1:
        raise AggregationCaptureError("capture query shard is empty")
    raw_lines = path.read_bytes().splitlines(keepends=True)
    if not raw_lines or any(not line.endswith(b"\n") for line in raw_lines):
        raise AggregationCaptureError("capture query shard lines are incomplete")
    try:
        coverage = QueryScoreCoverage.model_validate_json(raw_lines[0])
    except Exception as exc:
        raise AggregationCaptureError("capture query coverage is invalid") from exc
    if (
        coverage.query_id != query_id
        or coverage.query_sha256 != query_sha256
        or coverage.expected_rows != coverage.scored_rows
        or len(raw_lines) != coverage.scored_rows + 1
    ):
        raise AggregationCaptureError("capture query coverage binding is invalid")
    for ordinal, raw in enumerate(raw_lines[1:]):
        try:
            row = RowScore.model_validate_json(raw)
        except Exception as exc:
            raise AggregationCaptureError("capture row score is invalid") from exc
        if (
            row.query_id != query_id
            or row.ordinal != ordinal
            or row.embedding_profile_id != embedding_profile_id
        ):
            raise AggregationCaptureError("capture row score binding is invalid")
        if raw != canonical_json_bytes(row) + b"\n":
            raise AggregationCaptureError("capture row score is not canonical")
    if raw_lines[0] != canonical_json_bytes(coverage) + b"\n":
        raise AggregationCaptureError("capture query coverage is not canonical")
    return _CompletedShard(
        query_index=query_index,
        query_id=query_id,
        query_vector_sha256=coverage.query_vector_sha256,
        shard_sha256=digest,
        shard_size_bytes=size,
        scored_rows=coverage.scored_rows,
    )


def _validate_state_entries(state: Path, query_count: int) -> None:
    allowed = {_IDENTITY_FILE, _PROGRESS_FILE, _LOCK_FILE}
    allowed.update(f"query-{index:03d}.jsonl" for index in range(query_count))
    for entry in state.iterdir():
        if entry.name not in allowed or entry.is_symlink():
            raise AggregationCaptureError("capture state contains an unsafe entry")
        if entry.name != _LOCK_FILE and not entry.is_file():
            raise AggregationCaptureError("capture state entry is not a regular file")


def _build_shard(coverage: QueryScoreCoverage, rows: Sequence[RowScore]) -> bytes:
    payload = canonical_json_bytes(coverage) + b"\n"
    payload += b"".join(canonical_json_bytes(row) + b"\n" for row in rows)
    if len(payload) > _MAX_SHARD_BYTES:
        raise AggregationCaptureError("capture query shard exceeds its byte cap")
    return payload


def _publish_final_artifact(
    output_path: Path,
    manifest: ScoreArtifactManifest,
    shard_paths: Sequence[Path],
) -> tuple[str, int]:
    if output_path.is_symlink():
        raise AggregationCaptureError("score artifact output must not be a symlink")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".aggregation-scores-", dir=output_path.parent)
    temporary = Path(name)
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(descriptor, "wb") as output:
            for payload in (canonical_json_bytes(manifest) + b"\n",):
                digest.update(payload)
                size += len(payload)
                output.write(payload)
            for shard in shard_paths:
                with shard.open("rb") as source:
                    while block := source.read(1024 * 1024):
                        if len(block) > MAX_SCORE_ARTIFACT_BYTES - size:
                            raise AggregationCaptureError("score artifact exceeds its byte cap")
                        digest.update(block)
                        size += len(block)
                        output.write(block)
            output.flush()
            os.fsync(output.fileno())
        expected_sha256 = digest.hexdigest()
        if output_path.exists():
            actual_sha256, actual_size = _sha256_file(
                output_path,
                maximum_bytes=MAX_SCORE_ARTIFACT_BYTES,
            )
            if (actual_sha256, actual_size) != (expected_sha256, size):
                raise AggregationCaptureError("existing score artifact differs from capture state")
        else:
            os.link(temporary, output_path, follow_symlinks=False)
            _fsync_directory(output_path.parent)
        return expected_sha256, size
    finally:
        temporary.unlink(missing_ok=True)


async def capture_score_artifact(
    *,
    gold_path: Path,
    generation_manifest_path: Path,
    generation_directory: Path,
    object_root: Path,
    output_path: Path,
    state_directory: Path,
    source_commit: str,
    embedder: OpenRouterEmbedder,
    release_gate: bool = True,
) -> CaptureReceipt:
    """Capture or resume all gold-query scores and publish one canonical JSONL file."""

    if _SOURCE_COMMIT.fullmatch(source_commit) is None:
        raise AggregationCaptureError("source commit is invalid")
    gold: GoldDataset = load_gold_jsonl(gold_path, release_gate=release_gate)
    generation_manifest, generation_manifest_sha256 = _load_generation_manifest(
        generation_manifest_path
    )
    generation_directory = await asyncio.to_thread(
        generation_directory.resolve,
        strict=True,
    )
    if generation_directory.name != generation_manifest.generation_id:
        raise AggregationCaptureError("generation directory differs from its manifest")
    database_sha256, vector_sha256 = _verify_generation_files(
        generation_manifest,
        generation_directory,
    )
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
    _verify_runtime_binding(generation_manifest, handle.metadata)
    vectors = V5ExactRepository._vectors(handle)
    exact_row_corpus_sha256 = handle.metadata.exact_row_corpus_sha256
    embedding_profile_id = handle.metadata.primary_embedding_profile_id
    if exact_row_corpus_sha256 is None or embedding_profile_id is None:
        raise AggregationCaptureError("v5 exact-row identity is absent")
    active_revisions = V5ExactRepository._active_revisions(
        handle,
        # Only the default unscoped current/ambiguous gold policy is admissible.
        ContractSearchRequest(query=gold.queries[0].question),
    )
    active_ids = {item.contract_revision_id for item in active_revisions}
    expected_rows = sum(revision_id in active_ids for revision_id in vectors.contract_revision_ids)
    if expected_rows < 1 or not active_ids:
        raise AggregationCaptureError("score capture has no active exact rows")
    score_manifest = ScoreArtifactManifest(
        schema_version="cardrag.document-aggregation-score-artifact.v1",
        gold_sha256=gold.sha256,
        query_count=len(gold.queries),
        row_count=expected_rows * len(gold.queries),
        source_commit=source_commit,
        generation_id=generation_manifest.generation_id,
        generation_manifest_sha256=generation_manifest_sha256,
        serving_database_sha256=database_sha256,
        vector_sidecar_sha256=vector_sha256,
        exact_row_corpus_sha256=exact_row_corpus_sha256,
        embedding_profile_id=embedding_profile_id,
        embedding_model="qwen/qwen3-embedding-8b",
        embedding_dimension=4096,
        exact=True,
        approximate=False,
        scoring_contract="cardrag.v5-exact-row-score.v1",
        temporal_scope_policy="gold-query.v1",
        runtime_document_aggregation_status=handle.metadata.document_aggregation_status,
        runtime_document_aggregation_policy=handle.metadata.document_aggregation_policy,
        runtime_sealed_profile_sha256=handle.metadata.sealed_profile_sha256,
    )
    identity = _CaptureIdentity(
        score_manifest=score_manifest,
        queries=tuple(
            _CaptureQuery(
                query_id=query.query_id,
                query_sha256=hashlib.sha256(query.question.encode("utf-8")).hexdigest(),
            )
            for query in gold.queries
        ),
    )
    identity_sha256 = canonical_sha256(identity)
    state = _state_directory(state_directory)
    identity_path = state / _IDENTITY_FILE
    _publish_immutable(identity_path, identity.canonical_bytes())
    persisted_identity = _read_canonical_model(
        identity_path,
        _CaptureIdentity,
        maximum_bytes=_MAX_MANIFEST_BYTES,
    )
    if persisted_identity != identity:
        raise AggregationCaptureError("capture resume identity differs")
    _validate_state_entries(state, len(gold.queries))

    progress_path = state / _PROGRESS_FILE
    persisted_progress = (
        None
        if not progress_path.exists()
        else _read_canonical_model(
            progress_path,
            _CaptureProgress,
            maximum_bytes=_MAX_MANIFEST_BYTES,
        )
    )
    if persisted_progress is not None and persisted_progress.identity_sha256 != identity_sha256:
        raise AggregationCaptureError("capture progress belongs to another identity")
    completed: list[_CompletedShard] = []
    for index, query in enumerate(gold.queries):
        shard_path = _query_shard_path(state, index)
        if not shard_path.exists():
            break
        completed.append(
            _parse_query_shard(
                shard_path,
                query_index=index,
                query_id=query.query_id,
                query_sha256=identity.queries[index].query_sha256,
                embedding_profile_id=embedding_profile_id,
            )
        )
    if any(
        _query_shard_path(state, index).exists()
        for index in range(len(completed) + 1, len(gold.queries))
    ):
        raise AggregationCaptureError("capture shards do not form a complete prefix")
    if persisted_progress is not None:
        progress_count = len(persisted_progress.completed_shards)
        if tuple(completed[:progress_count]) != persisted_progress.completed_shards:
            raise AggregationCaptureError("capture progress differs from immutable shards")
        if progress_count < len(completed):
            _replace_progress(progress_path, _progress(identity_sha256, completed))
    resumed_queries = len(completed)
    repository = V5ExactRepository(cast(GenerationStore, _CaptureStore(state)), embedder)
    for index, query in enumerate(gold.queries[len(completed) :], start=len(completed)):
        capture = await repository.capture_unscoped_current_scores(
            query.question,
            handle=handle,
        )
        if (
            capture.query_sha256 != identity.queries[index].query_sha256
            or capture.expected_active_contracts != len(active_ids)
            or capture.expected_rows != expected_rows
            or capture.scored_rows != expected_rows
        ):
            raise AggregationCaptureError("exact score capture coverage is incomplete")
        coverage = QueryScoreCoverage(
            schema_version="cardrag.document-aggregation-query-coverage.v1",
            query_id=query.query_id,
            query_sha256=capture.query_sha256,
            query_vector_sha256=capture.query_vector_sha256,
            expected_rows=capture.expected_rows,
            scored_rows=capture.scored_rows,
            active_contracts=capture.expected_active_contracts,
        )
        rows = tuple(
            RowScore(
                schema_version="cardrag.document-aggregation-row-score.v1",
                query_id=query.query_id,
                ordinal=ordinal,
                row_index=row.row_index,
                contract_revision_id=row.contract_revision_id,
                node_id=row.node_id,
                view_type=row.view_type,
                input_sha256=row.input_sha256,
                embedding_profile_id=row.embedding_profile_id,
                score=row.score,
            )
            for ordinal, row in enumerate(capture.rows)
        )
        shard_payload = _build_shard(coverage, rows)
        shard_path = _query_shard_path(state, index)
        _publish_immutable(shard_path, shard_payload)
        completed.append(
            _parse_query_shard(
                shard_path,
                query_index=index,
                query_id=query.query_id,
                query_sha256=capture.query_sha256,
                embedding_profile_id=embedding_profile_id,
            )
        )
        _replace_progress(progress_path, _progress(identity_sha256, completed))
    if not progress_path.exists():
        _replace_progress(progress_path, _progress(identity_sha256, completed))
    artifact_sha256, artifact_size = _publish_final_artifact(
        output_path,
        score_manifest,
        tuple(_query_shard_path(state, index) for index in range(len(gold.queries))),
    )
    resolved_output = await asyncio.to_thread(output_path.resolve, strict=True)
    return CaptureReceipt(
        output_path=resolved_output,
        artifact_sha256=artifact_sha256,
        artifact_size_bytes=artifact_size,
        query_count=len(gold.queries),
        row_count=score_manifest.row_count,
        resumed_queries=resumed_queries,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--generation-manifest", type=Path, required=True)
    parser.add_argument("--generation-dir", type=Path, required=True)
    parser.add_argument("--object-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument(
        "--openrouter-base-url",
        default="https://openrouter.ai/api/v1",
    )
    parser.add_argument("--openrouter-api-key-file", type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument("--fixture-mode", action="store_true")
    return parser


async def _async_main(arguments: argparse.Namespace) -> CaptureReceipt:
    api_key: str | None = os.environ.get("OPENROUTER_API_KEY")
    if arguments.openrouter_api_key_file is not None:
        key_path: Path = arguments.openrouter_api_key_file
        api_key = await asyncio.to_thread(_read_api_key_file, key_path)
    embedder = OpenRouterEmbedder(
        base_url=str(arguments.openrouter_base_url),
        api_key=api_key,
        timeout_seconds=float(arguments.timeout_seconds),
    )
    try:
        return await capture_score_artifact(
            gold_path=arguments.gold,
            generation_manifest_path=arguments.generation_manifest,
            generation_directory=arguments.generation_dir,
            object_root=arguments.object_root,
            output_path=arguments.output,
            state_directory=arguments.state_dir,
            source_commit=arguments.source_commit,
            embedder=embedder,
            release_gate=not arguments.fixture_mode,
        )
    finally:
        await embedder.close()


def _read_api_key_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise AggregationCaptureError("OpenRouter key file is unsafe")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise AggregationCaptureError("OpenRouter key file is empty")
    return value


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
                "output_path": str(receipt.output_path),
                "query_count": receipt.query_count,
                "resumed_queries": receipt.resumed_queries,
                "row_count": receipt.row_count,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
