"""Best-effort Qwen reranker shadow lane with immutable local evidence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import stat
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, Self, cast

import httpx
from cardrag_core import canonical_json_bytes, canonical_sha256
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from cardrag_mcp.quota import (
    StorageQuotaError,
    safe_subtree_usage,
    state_quota_guard,
    state_quota_policy,
    validate_byte_limit,
    validate_count_limit,
)

RERANKER_MODEL: Literal["qwen/qwen3-reranker-8b"] = "qwen/qwen3-reranker-8b"
RERANKER_CANONICAL_RESPONSE_MODEL = "accounts/fireworks/models/qwen3-reranker-8b"
RERANKER_PROVIDER_ID: Literal["fireworks"] = "fireworks"
RERANKER_ARTIFACT_SCHEMA = "cardrag.reranker-shadow-artifact.v1"
RERANKER_IDENTITY_SCHEMA = "cardrag.reranker-shadow-identity.v1"
RERANKER_CANDIDATES_SCHEMA = "cardrag.reranker-shadow-candidates.v1"

MAX_RERANKER_CANDIDATES = 256
MAX_RERANKER_DOCUMENT_CHARACTERS = 100_000
MAX_RERANKER_TOTAL_CHARACTERS = 1_000_000
MAX_RERANKER_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_RERANKER_RESPONSE_BYTES = 8 * 1024 * 1024

Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,511}$")]
RerankerFailureReason = Literal[
    "provider_request_failed",
    "provider_contract_invalid",
    "candidate_input_invalid",
    "artifact_store_failed",
    "shadow_internal_error",
]

_ARTIFACT_NAME = re.compile(r"^reranker-shadow-[0-9a-f]{64}\.json$")


class RerankerShadowError(RuntimeError):
    """A bounded reranker failure that must never expose provider response text."""

    def __init__(self, reason_code: RerankerFailureReason) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class RerankerArtifactError(RuntimeError):
    """The local immutable shadow artifact failed strict validation."""


@dataclass(frozen=True, slots=True)
class RerankerCandidate:
    candidate_id: str
    contract_revision_id: str
    node_id: str
    display_text: str
    dense_rank: int
    dense_score: float
    matched_view_types: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RerankerScore:
    index: int
    relevance_score: float


@dataclass(frozen=True, slots=True)
class RerankerShadowDiagnostics:
    status: Literal["succeeded", "failed"]
    candidate_count: int
    rank_change_count: int | None
    artifact_sha256: str | None
    failure_reason: RerankerFailureReason | None


class _ShadowModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self)


class RerankerCandidateBinding(_ShadowModel):
    candidate_id: Identifier
    contract_revision_id: Identifier
    node_id: Identifier
    dense_rank: int = Field(ge=1, le=MAX_RERANKER_CANDIDATES)
    dense_score: float
    matched_view_types: tuple[Identifier, ...] = Field(min_length=1, max_length=6)
    text_sha256: Sha256Hex

    @model_validator(mode="after")
    def dense_input_is_finite_and_unique(self) -> Self:
        if not math.isfinite(self.dense_score):
            raise ValueError("reranker dense score must be finite")
        if len(self.matched_view_types) != len(set(self.matched_view_types)):
            raise ValueError("reranker matched view types must be unique")
        return self


class RerankerShadowIdentity(_ShadowModel):
    schema_version: Literal["cardrag.reranker-shadow-identity.v1"] = (
        "cardrag.reranker-shadow-identity.v1"
    )
    generation_id: Identifier
    query_sha256: Sha256Hex
    model: Literal["qwen/qwen3-reranker-8b"]
    provider_id: Literal["fireworks"]
    candidate_sha256: Sha256Hex


class RerankerShadowScore(_ShadowModel):
    candidate_id: Identifier
    original_index: int = Field(ge=0, lt=MAX_RERANKER_CANDIDATES)
    dense_rank: int = Field(ge=1, le=MAX_RERANKER_CANDIDATES)
    shadow_rank: int = Field(ge=1, le=MAX_RERANKER_CANDIDATES)
    relevance_score: float

    @model_validator(mode="after")
    def score_is_finite(self) -> Self:
        if not math.isfinite(self.relevance_score):
            raise ValueError("reranker relevance score must be finite")
        return self


class RerankerShadowArtifact(_ShadowModel):
    schema_version: Literal["cardrag.reranker-shadow-artifact.v1"] = (
        "cardrag.reranker-shadow-artifact.v1"
    )
    artifact_sha256: Sha256Hex
    identity: RerankerShadowIdentity
    status: Literal["succeeded", "failed"]
    candidates: tuple[RerankerCandidateBinding, ...] = Field(max_length=MAX_RERANKER_CANDIDATES)
    results: tuple[RerankerShadowScore, ...] = Field(max_length=MAX_RERANKER_CANDIDATES)
    rank_change_count: int | None = Field(default=None, ge=0, le=MAX_RERANKER_CANDIDATES)
    failure_reason: (
        Literal[
            "provider_request_failed",
            "provider_contract_invalid",
            "candidate_input_invalid",
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def artifact_is_self_bound_and_complete(self) -> Self:
        candidate_ids = tuple(item.candidate_id for item in self.candidates)
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("reranker candidates must be unique")
        if tuple(item.dense_rank for item in self.candidates) != tuple(
            range(1, len(self.candidates) + 1)
        ):
            raise ValueError("reranker dense ranks must be contiguous")
        expected_candidate_sha256 = canonical_sha256(
            {
                "candidates": self.candidates,
                "schema_version": RERANKER_CANDIDATES_SCHEMA,
            }
        )
        if self.identity.candidate_sha256 != expected_candidate_sha256:
            raise ValueError("reranker candidate hash is stale")
        if self.status == "succeeded":
            if self.failure_reason is not None or self.rank_change_count is None:
                raise ValueError("successful reranker artifact has failure fields")
            if len(self.results) != len(self.candidates):
                raise ValueError("successful reranker artifact has incomplete results")
            if tuple(item.shadow_rank for item in self.results) != tuple(
                range(1, len(self.results) + 1)
            ):
                raise ValueError("reranker shadow ranks must be contiguous")
            if {item.original_index for item in self.results} != set(range(len(self.candidates))):
                raise ValueError("reranker result indices are incomplete")
            for result in self.results:
                candidate = self.candidates[result.original_index]
                if (
                    result.candidate_id != candidate.candidate_id
                    or result.dense_rank != candidate.dense_rank
                ):
                    raise ValueError("reranker result is not bound to its dense candidate")
            expected_changes = sum(item.dense_rank != item.shadow_rank for item in self.results)
            if self.rank_change_count != expected_changes:
                raise ValueError("reranker rank-change count is invalid")
        else:
            if self.failure_reason is None or self.rank_change_count is not None or self.results:
                raise ValueError("failed reranker artifact has successful result fields")
        payload = self.model_dump(mode="python", exclude={"artifact_sha256"})
        if self.artifact_sha256 != canonical_sha256(payload):
            raise ValueError("reranker artifact hash does not bind its canonical payload")
        return self


def _candidate_bindings(
    candidates: Sequence[RerankerCandidate],
) -> tuple[RerankerCandidateBinding, ...]:
    bindings: list[RerankerCandidateBinding] = []
    total_characters = 0
    for expected_rank, candidate in enumerate(candidates, start=1):
        if (
            not candidate.candidate_id
            or not candidate.contract_revision_id
            or not candidate.node_id
            or not candidate.display_text
            or len(candidate.display_text) > MAX_RERANKER_DOCUMENT_CHARACTERS
            or candidate.dense_rank != expected_rank
            or not math.isfinite(candidate.dense_score)
            or not candidate.matched_view_types
        ):
            raise RerankerShadowError("candidate_input_invalid")
        total_characters += len(candidate.display_text)
        if total_characters > MAX_RERANKER_TOTAL_CHARACTERS:
            raise RerankerShadowError("candidate_input_invalid")
        try:
            bindings.append(
                RerankerCandidateBinding(
                    candidate_id=candidate.candidate_id,
                    contract_revision_id=candidate.contract_revision_id,
                    node_id=candidate.node_id,
                    dense_rank=candidate.dense_rank,
                    dense_score=candidate.dense_score,
                    matched_view_types=candidate.matched_view_types,
                    text_sha256=hashlib.sha256(candidate.display_text.encode()).hexdigest(),
                )
            )
        except ValueError as exc:
            raise RerankerShadowError("candidate_input_invalid") from exc
    return tuple(bindings)


def _identity(
    generation_id: str,
    query: str,
    bindings: tuple[RerankerCandidateBinding, ...],
) -> RerankerShadowIdentity:
    try:
        return RerankerShadowIdentity(
            generation_id=generation_id,
            query_sha256=hashlib.sha256(query.encode()).hexdigest(),
            model=RERANKER_MODEL,
            provider_id=RERANKER_PROVIDER_ID,
            candidate_sha256=canonical_sha256(
                {
                    "candidates": bindings,
                    "schema_version": RERANKER_CANDIDATES_SCHEMA,
                }
            ),
        )
    except ValueError as exc:
        raise RerankerShadowError("candidate_input_invalid") from exc


def _artifact(
    identity: RerankerShadowIdentity,
    candidates: tuple[RerankerCandidateBinding, ...],
    *,
    status: Literal["succeeded", "failed"],
    results: tuple[RerankerShadowScore, ...] = (),
    rank_change_count: int | None = None,
    failure_reason: Literal[
        "provider_request_failed",
        "provider_contract_invalid",
        "candidate_input_invalid",
    ]
    | None = None,
) -> RerankerShadowArtifact:
    payload: dict[str, Any] = {
        "schema_version": RERANKER_ARTIFACT_SCHEMA,
        "identity": identity,
        "status": status,
        "candidates": candidates,
        "results": results,
        "rank_change_count": rank_change_count,
        "failure_reason": failure_reason,
    }
    return RerankerShadowArtifact(
        artifact_sha256=canonical_sha256(payload),
        **payload,
    )


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


def _provider_matches(actual: str) -> bool:
    return re.sub(r"[^a-z0-9]", "", actual.casefold()) == RERANKER_PROVIDER_ID


async def _bounded_response_bytes(response: httpx.Response, maximum_bytes: int) -> bytes:
    raw_length = response.headers.get("content-length")
    if raw_length is not None and (not raw_length.isdigit() or int(raw_length) > maximum_bytes):
        raise ValueError("reranker response Content-Length is invalid")
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(chunk) > maximum_bytes - len(body):
            raise ValueError("reranker response exceeds its byte cap")
        body.extend(chunk)
    return bytes(body)


class OpenRouterReranker:
    """Strict client for OpenRouter's text rerank endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float,
        maximum_response_bytes: int = MAX_RERANKER_RESPONSE_BYTES,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("OpenRouter API key is empty")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("reranker timeout must be positive and finite")
        validate_byte_limit(maximum_response_bytes, label="maximum reranker response bytes")
        if maximum_response_bytes > MAX_RERANKER_RESPONSE_BYTES:
            raise ValueError("maximum reranker response bytes exceeds the hard safety bound")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._maximum_response_bytes = maximum_response_bytes
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def rerank(self, query: str, documents: Sequence[str]) -> tuple[RerankerScore, ...]:
        if (
            not query
            or len(documents) < 1
            or len(documents) > MAX_RERANKER_CANDIDATES
            or any(
                not document or len(document) > MAX_RERANKER_DOCUMENT_CHARACTERS
                for document in documents
            )
            or sum(map(len, documents)) > MAX_RERANKER_TOTAL_CHARACTERS
        ):
            raise RerankerShadowError("candidate_input_invalid")
        request_body = {
            "documents": list(documents),
            "model": RERANKER_MODEL,
            "provider": {
                "order": [RERANKER_PROVIDER_ID],
                "only": [RERANKER_PROVIDER_ID],
                "allow_fallbacks": False,
                "require_parameters": False,
            },
            "query": query,
            "top_n": len(documents),
        }
        try:
            async with asyncio.timeout(self._timeout_seconds):
                async with self._client.stream(
                    "POST",
                    "rerank",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=request_body,
                ) as response:
                    response.raise_for_status()
                    response_body = await _bounded_response_bytes(
                        response,
                        self._maximum_response_bytes,
                    )
        except (httpx.HTTPError, TimeoutError) as exc:
            raise RerankerShadowError("provider_request_failed") from exc
        except ValueError as exc:
            raise RerankerShadowError("provider_contract_invalid") from exc
        try:
            payload: Any = json.loads(response_body)
            if not isinstance(payload, Mapping):
                raise ValueError
            actual_model = payload.get("model")
            if not isinstance(actual_model, str) or actual_model not in {
                RERANKER_MODEL,
                RERANKER_CANONICAL_RESPONSE_MODEL,
            }:
                raise ValueError
            actual_provider = _response_provider(payload, response)
            if actual_provider is None or not _provider_matches(actual_provider):
                raise ValueError
            raw_results = payload.get("results")
            if not isinstance(raw_results, list) or len(raw_results) != len(documents):
                raise ValueError
            results: list[RerankerScore] = []
            seen: set[int] = set()
            previous_score = math.inf
            for raw in raw_results:
                if not isinstance(raw, Mapping):
                    raise ValueError
                index = raw.get("index")
                score = raw.get("relevance_score")
                if (
                    isinstance(index, bool)
                    or not isinstance(index, int)
                    or index < 0
                    or index >= len(documents)
                    or index in seen
                    or isinstance(score, bool)
                    or not isinstance(score, (int, float))
                ):
                    raise ValueError
                normalized_score = float(score)
                if not math.isfinite(normalized_score) or normalized_score > previous_score:
                    raise ValueError
                previous_score = normalized_score
                seen.add(index)
                results.append(RerankerScore(index=index, relevance_score=normalized_score))
            if seen != set(range(len(documents))):
                raise ValueError
            return tuple(results)
        except (TypeError, ValueError) as exc:
            raise RerankerShadowError("provider_contract_invalid") from exc


class RerankerShadowStore:
    """Publish and reload one immutable canonical artifact per shadow identity."""

    def __init__(
        self,
        state_root: Path,
        *,
        maximum_jobs: int | None = None,
        maximum_total_bytes: int | None = None,
        maximum_artifact_bytes: int | None = None,
    ) -> None:
        self._state_root = state_root.resolve()
        self._audit_root = self._state_root / "audit-reports"
        self._root = self._audit_root / "reranker-shadow"
        policy = state_quota_policy(self._state_root)
        self.maximum_jobs = validate_count_limit(
            policy.reranker_audit_max_jobs if maximum_jobs is None else maximum_jobs,
            label="maximum reranker audit jobs",
        )
        self.maximum_total_bytes = validate_byte_limit(
            (
                policy.reranker_audit_max_total_bytes
                if maximum_total_bytes is None
                else maximum_total_bytes
            ),
            label="maximum reranker audit total bytes",
        )
        self.maximum_artifact_bytes = validate_byte_limit(
            (
                policy.reranker_audit_max_artifact_bytes
                if maximum_artifact_bytes is None
                else maximum_artifact_bytes
            ),
            label="maximum reranker audit artifact bytes",
        )
        if (
            self.maximum_artifact_bytes > MAX_RERANKER_ARTIFACT_BYTES
            or self.maximum_artifact_bytes > self.maximum_total_bytes
        ):
            raise ValueError("reranker artifact cap exceeds its containing quota")

    @staticmethod
    def artifact_name(identity: RerankerShadowIdentity) -> str:
        return "reranker-shadow-" + canonical_sha256(identity) + ".json"

    def load(
        self,
        identity: RerankerShadowIdentity,
        candidates: tuple[RerankerCandidateBinding, ...],
    ) -> RerankerShadowArtifact | None:
        name = self.artifact_name(identity)
        if _ARTIFACT_NAME.fullmatch(name) is None:
            raise RerankerArtifactError("reranker artifact name is unsafe")
        root = self._ensure_root()
        path = root / name
        if not path.exists() and not path.is_symlink():
            return None
        artifact = self._read_artifact(path)
        if artifact.identity != identity or artifact.candidates != candidates:
            raise RerankerArtifactError("reranker artifact identity or candidates are stale")
        return artifact

    def publish(self, artifact: RerankerShadowArtifact) -> RerankerShadowArtifact:
        root = self._ensure_root()
        path = root / self.artifact_name(artifact.identity)
        existing = self.load(artifact.identity, artifact.candidates)
        if existing is not None:
            return existing
        payload = artifact.canonical_bytes()
        if len(payload) > self.maximum_artifact_bytes:
            raise RerankerArtifactError("reranker artifact exceeds its size bound")
        with self._write_quota_guard(len(payload)):
            existing = self.load(artifact.identity, artifact.candidates)
            if existing is not None:
                return existing
            descriptor, temporary_name = tempfile.mkstemp(prefix=".reranker-shadow.", dir=root)
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
                    existing = self.load(artifact.identity, artifact.candidates)
                    if existing is None:  # pragma: no cover - concurrent removal
                        raise RerankerArtifactError("reranker artifact publication raced") from None
                    return existing
                self._fsync_directory(root)
            finally:
                temporary.unlink(missing_ok=True)
                self._fsync_directory(root)
        return artifact

    def _artifact_count(self) -> int:
        if not self._root.exists() and not self._root.is_symlink():
            return 0
        root = self._ensure_root()
        count = 0
        for path in sorted(root.iterdir(), key=lambda item: item.name):
            if (
                path.is_symlink()
                or not path.is_file()
                or _ARTIFACT_NAME.fullmatch(path.name) is None
            ):
                raise RerankerArtifactError("reranker artifact directory contains an unsafe entry")
            count += 1
        return count

    @contextmanager
    def _write_quota_guard(self, artifact_bytes: int) -> Iterator[None]:
        try:
            with state_quota_guard(self._state_root, artifact_bytes):
                total = safe_subtree_usage(self._state_root, self._root)
                if (
                    total > self.maximum_total_bytes
                    or artifact_bytes > self.maximum_total_bytes - total
                ):
                    raise RerankerArtifactError("reranker audit total quota rejected this write")
                if self._artifact_count() >= self.maximum_jobs:
                    raise RerankerArtifactError("reranker audit job quota rejected this query")
                yield
        except StorageQuotaError:
            raise RerankerArtifactError("MCP state quota rejected reranker audit write") from None

    def _ensure_root(self) -> Path:
        if self._state_root.is_symlink() or not self._state_root.is_dir():
            raise RerankerArtifactError("MCP state root is unsafe")
        for parent, path in (
            (self._state_root, self._audit_root),
            (self._audit_root, self._root),
        ):
            if path.is_symlink():
                raise RerankerArtifactError("reranker artifact directory must not be a symlink")
            created = not path.exists()
            path.mkdir(mode=0o700, parents=False, exist_ok=True)
            if created:
                self._fsync_directory(parent)
            if not path.is_dir() or path.resolve(strict=True).parent != parent.resolve(strict=True):
                raise RerankerArtifactError("reranker artifact directory escaped MCP state")
        return self._root.resolve(strict=True)

    @staticmethod
    def _read_artifact(path: Path) -> RerankerShadowArtifact:
        if path.is_symlink():
            raise RerankerArtifactError("reranker artifact must not be a symlink")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise RerankerArtifactError("reranker artifact is unreadable") from exc
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_mode & 0o222:
                raise RerankerArtifactError("reranker artifact must be an immutable regular file")
            if before.st_size < 1 or before.st_size > MAX_RERANKER_ARTIFACT_BYTES:
                raise RerankerArtifactError("reranker artifact has an invalid size")
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
            raise RerankerArtifactError("reranker artifact changed during read")
        try:
            artifact = RerankerShadowArtifact.model_validate_json(payload)
        except Exception as exc:
            raise RerankerArtifactError("reranker artifact schema is invalid") from exc
        if artifact.canonical_bytes() != payload:
            raise RerankerArtifactError("reranker artifact is not canonical JSON")
        return artifact

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class RerankerShadowLane:
    """Observe dense candidates without ever mutating primary response objects."""

    def __init__(
        self,
        client: OpenRouterReranker,
        store: RerankerShadowStore,
        *,
        maximum_candidates: int,
    ) -> None:
        if maximum_candidates < 1 or maximum_candidates > MAX_RERANKER_CANDIDATES:
            raise ValueError("reranker shadow candidate limit is invalid")
        self.client = client
        self.store = store
        self.maximum_candidates = maximum_candidates

    async def close(self) -> None:
        await self.client.close()

    async def observe(
        self,
        *,
        generation_id: str,
        query: str,
        candidates: Sequence[RerankerCandidate],
    ) -> RerankerShadowDiagnostics:
        candidate_count = min(len(candidates), self.maximum_candidates)
        try:
            selected = tuple(candidates[: self.maximum_candidates])
            bindings = _candidate_bindings(selected)
            identity = _identity(generation_id, query, bindings)
            try:
                existing = await asyncio.to_thread(self.store.load, identity, bindings)
            except RerankerArtifactError:
                return RerankerShadowDiagnostics(
                    status="failed",
                    candidate_count=candidate_count,
                    rank_change_count=None,
                    artifact_sha256=None,
                    failure_reason="artifact_store_failed",
                )
            if existing is not None:
                return self._diagnostics(existing)
            try:
                if selected:
                    raw_scores = await self.client.rerank(
                        query,
                        [candidate.display_text for candidate in selected],
                    )
                    results = tuple(
                        RerankerShadowScore(
                            candidate_id=bindings[score.index].candidate_id,
                            original_index=score.index,
                            dense_rank=bindings[score.index].dense_rank,
                            shadow_rank=shadow_rank,
                            relevance_score=score.relevance_score,
                        )
                        for shadow_rank, score in enumerate(raw_scores, start=1)
                    )
                    rank_changes = sum(item.dense_rank != item.shadow_rank for item in results)
                else:
                    results = ()
                    rank_changes = 0
                artifact = _artifact(
                    identity,
                    bindings,
                    status="succeeded",
                    results=results,
                    rank_change_count=rank_changes,
                )
            except RerankerShadowError as exc:
                failure_reason = cast(
                    Literal[
                        "provider_request_failed",
                        "provider_contract_invalid",
                        "candidate_input_invalid",
                    ],
                    exc.reason_code,
                )
                artifact = _artifact(
                    identity,
                    bindings,
                    status="failed",
                    failure_reason=failure_reason,
                )
            try:
                stored = await asyncio.to_thread(self.store.publish, artifact)
            except RerankerArtifactError:
                return RerankerShadowDiagnostics(
                    status="failed",
                    candidate_count=candidate_count,
                    rank_change_count=None,
                    artifact_sha256=None,
                    failure_reason="artifact_store_failed",
                )
            return self._diagnostics(stored)
        except RerankerShadowError as exc:
            return RerankerShadowDiagnostics(
                status="failed",
                candidate_count=candidate_count,
                rank_change_count=None,
                artifact_sha256=None,
                failure_reason=exc.reason_code,
            )
        except Exception:
            return RerankerShadowDiagnostics(
                status="failed",
                candidate_count=candidate_count,
                rank_change_count=None,
                artifact_sha256=None,
                failure_reason="shadow_internal_error",
            )

    @staticmethod
    def _diagnostics(artifact: RerankerShadowArtifact) -> RerankerShadowDiagnostics:
        return RerankerShadowDiagnostics(
            status=artifact.status,
            candidate_count=len(artifact.candidates),
            rank_change_count=artifact.rank_change_count,
            artifact_sha256=artifact.artifact_sha256,
            failure_reason=artifact.failure_reason,
        )


def reranker_candidate_id(contract_revision_id: str, node_id: str) -> str:
    return "rerank_candidate_" + canonical_sha256(
        {
            "contract_revision_id": contract_revision_id,
            "node_id": node_id,
            "schema_version": "cardrag.reranker-shadow-candidate-id.v1",
        }
    )


__all__ = [
    "MAX_RERANKER_CANDIDATES",
    "OpenRouterReranker",
    "RERANKER_CANONICAL_RESPONSE_MODEL",
    "RERANKER_MODEL",
    "RERANKER_PROVIDER_ID",
    "RerankerArtifactError",
    "RerankerCandidate",
    "RerankerShadowArtifact",
    "RerankerShadowDiagnostics",
    "RerankerShadowError",
    "RerankerShadowIdentity",
    "RerankerShadowLane",
    "RerankerShadowStore",
    "reranker_candidate_id",
]
