"""Canonical, resumable local ledgers for v5 exhaustive quality audits."""

from __future__ import annotations

import hashlib
import math
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Self, TypeVar

import numpy as np
from cardrag_core import canonical_json_bytes, canonical_sha256
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from cardrag_mcp.aggregation import aggregate_document_view_scores, exhaustive_profile_id
from cardrag_mcp.models import DocumentAggregationPolicy, ViewType
from cardrag_mcp.quota import (
    StorageQuotaError,
    safe_shared_exhaustive_audit_usage,
    state_quota_guard,
    state_quota_policy,
    validate_byte_limit,
    validate_count_limit,
)

ExhaustiveProfileId = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,511}$"),
]
EXHAUSTIVE_PROFILE_ID: ExhaustiveProfileId = exhaustive_profile_id(
    policy="max_child",
    sealed_profile_sha256=None,
)
_JOB_ID = re.compile(r"^audit-[0-9a-f]{64}$")
_MAX_LEDGER_BYTES = 256 * 1024 * 1024
_QUERY_VECTOR_FILE = "query-vector.f32"
_QUERY_VECTOR_BYTES = 4096 * 4
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,511}$")]


class ExhaustiveAuditError(RuntimeError):
    """An audit checkpoint or completion artifact failed closed validation."""


class _AuditModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self)


class ExpectedContract(_AuditModel):
    contract_revision_id: Identifier
    embedding_rows: int = Field(gt=0)


class AuditViewScore(_AuditModel):
    row_index: int = Field(ge=0)
    view_type: ViewType
    score: float

    @model_validator(mode="after")
    def score_is_finite(self) -> Self:
        if not math.isfinite(self.score):
            raise ValueError("audit view score must be finite")
        return self


class AuditNodeScore(_AuditModel):
    node_id: Identifier
    score: float
    matched_view_types: tuple[ViewType, ...] = Field(min_length=1)
    views: tuple[AuditViewScore, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def score_and_lanes_are_canonical(self) -> Self:
        if not math.isfinite(self.score):
            raise ValueError("audit node score must be finite")
        if len(self.matched_view_types) != len(set(self.matched_view_types)):
            raise ValueError("audit node view types must be unique")
        if len({view.row_index for view in self.views}) != len(self.views):
            raise ValueError("audit node view rows must be unique")
        if len({view.view_type for view in self.views}) != len(self.views):
            raise ValueError("audit node view lanes must be unique")
        best = max(view.score for view in self.views)
        if not math.isclose(self.score, best, abs_tol=1e-7):
            raise ValueError("audit node score must equal its best view score")
        best_types = tuple(
            view.view_type for view in self.views if math.isclose(view.score, best, abs_tol=1e-7)
        )
        if self.matched_view_types != best_types:
            raise ValueError("audit matched view types differ from its best lane scores")
        return self


class AuditContractScore(_AuditModel):
    contract_revision_id: Identifier
    aggregation_policy: DocumentAggregationPolicy
    score: float
    scored_embedding_rows: int = Field(gt=0)
    exact_blocks: int = Field(gt=0)
    nodes: tuple[AuditNodeScore, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def contract_score_is_bound_to_sorted_nodes(self) -> Self:
        node_ids = tuple(node.node_id for node in self.nodes)
        if node_ids != tuple(sorted(set(node_ids))):
            raise ValueError("audit contract nodes must be sorted and unique")
        view_scores = tuple(
            (view.view_type, view.score) for node in self.nodes for view in node.views
        )
        if len(view_scores) != self.scored_embedding_rows:
            raise ValueError("audit contract row count differs from its view scores")
        expected = aggregate_document_view_scores(view_scores, self.aggregation_policy)
        if not math.isfinite(self.score) or not math.isclose(
            self.score,
            expected,
            abs_tol=1e-7,
        ):
            raise ValueError("audit contract score differs from its aggregation policy")
        return self


class ExhaustiveAuditLedger(_AuditModel):
    schema_version: Literal["cardrag.exhaustive-audit-ledger.v2"] = (
        "cardrag.exhaustive-audit-ledger.v2"
    )
    status: Literal["progress", "complete"]
    job_id: Identifier
    generation_id: Identifier
    query_sha256: Sha256Hex
    exhaustive_profile_id: ExhaustiveProfileId
    document_aggregation_policy: DocumentAggregationPolicy = "max_child"
    sealed_profile_sha256: Sha256Hex | None = None
    query_vector_sha256: Sha256Hex
    expected_contracts: tuple[ExpectedContract, ...]
    expected_embedding_rows: int = Field(ge=0)
    completed_contracts: tuple[AuditContractScore, ...] = ()
    scored_embedding_rows: int = Field(ge=0)
    exact_blocks: int = Field(ge=0)

    @model_validator(mode="after")
    def progress_is_a_complete_ordered_prefix(self) -> Self:
        expected_profile_id = exhaustive_profile_id(
            policy=self.document_aggregation_policy,
            sealed_profile_sha256=self.sealed_profile_sha256,
        )
        if self.exhaustive_profile_id != expected_profile_id:
            raise ValueError("audit profile ID differs from its aggregation identity")
        expected_ids = tuple(item.contract_revision_id for item in self.expected_contracts)
        if expected_ids != tuple(sorted(set(expected_ids))):
            raise ValueError("audit expected contracts must be sorted and unique")
        if self.expected_embedding_rows != sum(
            item.embedding_rows for item in self.expected_contracts
        ):
            raise ValueError("audit expected row count differs from its contracts")
        completed_ids = tuple(item.contract_revision_id for item in self.completed_contracts)
        if completed_ids != expected_ids[: len(completed_ids)]:
            raise ValueError("audit completed contracts must be an ordered prefix")
        expected_by_id = {
            item.contract_revision_id: item.embedding_rows for item in self.expected_contracts
        }
        if any(
            item.scored_embedding_rows != expected_by_id[item.contract_revision_id]
            for item in self.completed_contracts
        ):
            raise ValueError("audit contract row count differs from its expectation")
        if any(
            item.aggregation_policy != self.document_aggregation_policy
            for item in self.completed_contracts
        ):
            raise ValueError("audit contracts use another aggregation policy")
        if self.scored_embedding_rows != sum(
            item.scored_embedding_rows for item in self.completed_contracts
        ):
            raise ValueError("audit scored row count differs from completed contracts")
        if self.exact_blocks != sum(item.exact_blocks for item in self.completed_contracts):
            raise ValueError("audit block count differs from completed contracts")
        if self.status == "complete" and len(completed_ids) != len(expected_ids):
            raise ValueError("complete audit ledger is missing contracts")
        return self


class _ProgressEnvelope(_AuditModel):
    schema_version: Literal["cardrag.exhaustive-audit-progress.v2"] = (
        "cardrag.exhaustive-audit-progress.v2"
    )
    ledger_sha256: Sha256Hex
    ledger: ExhaustiveAuditLedger

    @model_validator(mode="after")
    def hash_binds_ledger(self) -> Self:
        if self.ledger.status != "progress":
            raise ValueError("progress envelope requires a progress ledger")
        if self.ledger_sha256 != canonical_sha256(self.ledger):
            raise ValueError("progress hash does not bind its ledger")
        return self


class _CompleteMarker(_AuditModel):
    schema_version: Literal["cardrag.exhaustive-audit-complete.v2"] = (
        "cardrag.exhaustive-audit-complete.v2"
    )
    job_id: Identifier
    artifact_sha256: Sha256Hex
    artifact_size_bytes: int = Field(gt=0)


@dataclass(frozen=True, slots=True)
class AuditIdentity:
    job_id: str
    generation_id: str
    query_sha256: str
    exhaustive_profile_id: ExhaustiveProfileId
    document_aggregation_policy: DocumentAggregationPolicy
    sealed_profile_sha256: str | None


@dataclass(frozen=True, slots=True)
class LoadedAudit:
    ledger: ExhaustiveAuditLedger
    query_vector: NDArray[np.float32]
    resumed: bool
    artifact_sha256: str | None = None


ModelT = TypeVar("ModelT", bound=_AuditModel)


def _canonical_query_vector(vector: object) -> NDArray[np.float32]:
    values = np.asarray(vector, dtype="<f4")
    if values.shape != (4096,) or not bool(np.isfinite(values).all()):
        raise ExhaustiveAuditError("exhaustive query vector is invalid")
    values = np.ascontiguousarray(values, dtype="<f4")
    norm = float(np.linalg.norm(values))
    if not math.isfinite(norm) or not math.isclose(norm, 1.0, rel_tol=1e-4, abs_tol=1e-4):
        raise ExhaustiveAuditError("exhaustive query vector is not L2-normalized")
    return values


def query_vector_sha256(vector: object) -> str:
    """Bind the exact little-endian float32 query used across resumed batches."""

    values = _canonical_query_vector(vector)
    return hashlib.sha256(values.tobytes(order="C")).hexdigest()


class ExhaustiveAuditStore:
    """Own traversal-safe progress files and immutable completion artifacts."""

    def __init__(
        self,
        state_root: Path,
        *,
        maximum_jobs: int | None = None,
        maximum_total_bytes: int | None = None,
        maximum_artifact_bytes: int | None = None,
    ) -> None:
        self._state_root = state_root.resolve()
        self._root = self._state_root / "audit-jobs"
        policy = state_quota_policy(self._state_root)
        self.maximum_jobs = validate_count_limit(
            policy.exhaustive_audit_max_jobs if maximum_jobs is None else maximum_jobs,
            label="maximum exhaustive audit jobs",
        )
        self.maximum_total_bytes = validate_byte_limit(
            (
                policy.exhaustive_audit_max_total_bytes
                if maximum_total_bytes is None
                else maximum_total_bytes
            ),
            label="maximum exhaustive audit total bytes",
        )
        self.maximum_artifact_bytes = validate_byte_limit(
            (
                policy.exhaustive_audit_max_artifact_bytes
                if maximum_artifact_bytes is None
                else maximum_artifact_bytes
            ),
            label="maximum exhaustive audit artifact bytes",
        )
        if self.maximum_artifact_bytes > self.maximum_total_bytes:
            raise ValueError("exhaustive audit artifact cap exceeds its total quota")

    @staticmethod
    def identity(
        generation_id: str,
        query_sha256: str,
        requested_profile_id: ExhaustiveProfileId | None = None,
        *,
        document_aggregation_policy: DocumentAggregationPolicy = "max_child",
        sealed_profile_sha256: str | None = None,
    ) -> AuditIdentity:
        expected_profile_id = exhaustive_profile_id(
            policy=document_aggregation_policy,
            sealed_profile_sha256=sealed_profile_sha256,
        )
        selected_profile_id = requested_profile_id or expected_profile_id
        if selected_profile_id != expected_profile_id:
            raise ExhaustiveAuditError(
                "exhaustive profile ID differs from its aggregation identity"
            )
        job_id = "audit-" + canonical_sha256(
            {
                "document_aggregation_policy": document_aggregation_policy,
                "sealed_profile_sha256": sealed_profile_sha256,
                "exhaustive_profile_id": selected_profile_id,
                "generation_id": generation_id,
                "query_sha256": query_sha256,
                "schema_version": "cardrag.exhaustive-audit-identity.v2",
            }
        )
        if _JOB_ID.fullmatch(job_id) is None:
            raise ExhaustiveAuditError("derived exhaustive audit job ID is unsafe")
        return AuditIdentity(
            job_id=job_id,
            generation_id=generation_id,
            query_sha256=query_sha256,
            exhaustive_profile_id=selected_profile_id,
            document_aggregation_policy=document_aggregation_policy,
            sealed_profile_sha256=sealed_profile_sha256,
        )

    def load(
        self,
        identity: AuditIdentity,
        expected_contracts: tuple[ExpectedContract, ...],
    ) -> LoadedAudit | None:
        directory = self._job_directory(identity, create=False)
        if directory is None:
            return None
        query_vector = self._read_query_vector(directory)
        marker_path = directory / "COMPLETE.json"
        if marker_path.exists() or marker_path.is_symlink():
            marker = self._read_model(marker_path, _CompleteMarker, label="completion marker")
            if marker.job_id != identity.job_id:
                raise ExhaustiveAuditError("completion marker belongs to another audit job")
            artifact_path = directory / f"artifact-{marker.artifact_sha256}.json"
            artifact_bytes = self._read_bytes(artifact_path, label="completion artifact")
            if (
                len(artifact_bytes) != marker.artifact_size_bytes
                or hashlib.sha256(artifact_bytes).hexdigest() != marker.artifact_sha256
            ):
                raise ExhaustiveAuditError("completion artifact hash or size is invalid")
            ledger = self._parse_model(
                artifact_bytes,
                ExhaustiveAuditLedger,
                label="completion artifact",
            )
            self._verify_identity(ledger, identity, expected_contracts)
            self._verify_query_vector(ledger, query_vector)
            if ledger.status != "complete":
                raise ExhaustiveAuditError("completion artifact does not contain a complete ledger")
            return LoadedAudit(
                ledger=ledger,
                query_vector=query_vector,
                resumed=True,
                artifact_sha256=marker.artifact_sha256,
            )
        progress_path = directory / "progress.json"
        if not progress_path.exists() and not progress_path.is_symlink():
            return None
        envelope = self._read_model(progress_path, _ProgressEnvelope, label="audit progress")
        self._verify_identity(envelope.ledger, identity, expected_contracts)
        self._verify_query_vector(envelope.ledger, query_vector)
        return LoadedAudit(
            ledger=envelope.ledger,
            query_vector=query_vector,
            resumed=bool(envelope.ledger.completed_contracts),
        )

    def load_query_vector(self, identity: AuditIdentity) -> NDArray[np.float32] | None:
        """Recover a vector published just before an interrupted initial ledger write."""

        directory = self._job_directory(identity, create=False)
        if directory is None:
            return None
        path = directory / _QUERY_VECTOR_FILE
        if not path.exists() and not path.is_symlink():
            return None
        return self._read_query_vector(directory)

    def begin(
        self,
        identity: AuditIdentity,
        expected_contracts: tuple[ExpectedContract, ...],
        *,
        query_vector: object,
    ) -> ExhaustiveAuditLedger:
        canonical_vector = _canonical_query_vector(query_vector)
        vector_bytes = canonical_vector.tobytes(order="C")
        vector_sha256 = hashlib.sha256(vector_bytes).hexdigest()
        ledger = ExhaustiveAuditLedger(
            status="progress",
            job_id=identity.job_id,
            generation_id=identity.generation_id,
            query_sha256=identity.query_sha256,
            exhaustive_profile_id=identity.exhaustive_profile_id,
            document_aggregation_policy=identity.document_aggregation_policy,
            sealed_profile_sha256=identity.sealed_profile_sha256,
            query_vector_sha256=vector_sha256,
            expected_contracts=expected_contracts,
            expected_embedding_rows=sum(item.embedding_rows for item in expected_contracts),
            completed_contracts=(),
            scored_embedding_rows=0,
            exact_blocks=0,
        )
        envelope = _ProgressEnvelope(
            ledger_sha256=canonical_sha256(ledger),
            ledger=ledger,
        )
        progress_bytes = envelope.canonical_bytes()
        self._validate_new_artifact(vector_bytes)
        self._validate_new_artifact(progress_bytes)
        existing_directory = self._job_directory(identity, create=False)
        new_job = existing_directory is None
        vector_growth = (
            len(vector_bytes)
            if existing_directory is None
            else self._immutable_growth(existing_directory / _QUERY_VECTOR_FILE, vector_bytes)
        )
        progress_growth, progress_peak = (
            (len(progress_bytes), len(progress_bytes))
            if existing_directory is None
            else self._replacement_growth(existing_directory / "progress.json", progress_bytes)
        )
        logical_growth = vector_growth + progress_growth
        peak_growth = vector_growth + progress_peak
        with self._write_quota_guard(
            logical_growth,
            peak_growth_bytes=peak_growth,
            new_job_id=identity.job_id if new_job else None,
        ):
            directory = self._job_directory(identity, create=True)
            if directory is None:  # pragma: no cover - create=True always returns a path
                raise ExhaustiveAuditError("audit directory was not created")
            self._publish_immutable(directory / _QUERY_VECTOR_FILE, vector_bytes)
            self._atomic_write(directory / "progress.json", progress_bytes)
        return ledger

    def checkpoint(
        self,
        identity: AuditIdentity,
        ledger: ExhaustiveAuditLedger,
        contract: AuditContractScore,
    ) -> ExhaustiveAuditLedger:
        directory = self._required_job_directory(identity)
        self._verify_identity(ledger, identity, ledger.expected_contracts)
        payload = ledger.model_dump(mode="python")
        payload.update(
            {
                "completed_contracts": (*ledger.completed_contracts, contract),
                "scored_embedding_rows": (
                    ledger.scored_embedding_rows + contract.scored_embedding_rows
                ),
                "exact_blocks": ledger.exact_blocks + contract.exact_blocks,
            }
        )
        next_ledger = ExhaustiveAuditLedger.model_validate(payload)
        self._write_progress(directory, next_ledger)
        return next_ledger

    def complete(
        self,
        identity: AuditIdentity,
        ledger: ExhaustiveAuditLedger,
    ) -> LoadedAudit:
        directory = self._required_job_directory(identity)
        payload = ledger.model_dump(mode="python")
        payload["status"] = "complete"
        complete = ExhaustiveAuditLedger.model_validate(payload)
        self._verify_identity(complete, identity, complete.expected_contracts)
        artifact_bytes = complete.canonical_bytes()
        artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
        artifact_path = directory / f"artifact-{artifact_sha256}.json"
        marker = _CompleteMarker(
            job_id=identity.job_id,
            artifact_sha256=artifact_sha256,
            artifact_size_bytes=len(artifact_bytes),
        )
        marker_bytes = marker.canonical_bytes()
        artifact_growth = self._immutable_growth(artifact_path, artifact_bytes)
        marker_path = directory / "COMPLETE.json"
        marker_growth = self._immutable_growth(marker_path, marker_bytes)
        with self._write_quota_guard(artifact_growth + marker_growth):
            self._publish_immutable(artifact_path, artifact_bytes)
            self._publish_immutable(marker_path, marker_bytes)
        query_vector = self._read_query_vector(directory)
        self._verify_query_vector(complete, query_vector)
        return LoadedAudit(
            ledger=complete,
            query_vector=query_vector,
            resumed=bool(ledger.completed_contracts),
            artifact_sha256=artifact_sha256,
        )

    @staticmethod
    def _read_query_vector(directory: Path) -> NDArray[np.float32]:
        payload = ExhaustiveAuditStore._read_bytes(
            directory / _QUERY_VECTOR_FILE,
            label="audit query vector",
        )
        if len(payload) != _QUERY_VECTOR_BYTES:
            raise ExhaustiveAuditError("audit query vector has an invalid byte size")
        return _canonical_query_vector(np.frombuffer(payload, dtype="<f4").copy())

    @staticmethod
    def _verify_query_vector(
        ledger: ExhaustiveAuditLedger,
        query_vector: NDArray[np.float32],
    ) -> None:
        if query_vector_sha256(query_vector) != ledger.query_vector_sha256:
            raise ExhaustiveAuditError("audit query vector hash differs from its ledger")

    def _verify_identity(
        self,
        ledger: ExhaustiveAuditLedger,
        identity: AuditIdentity,
        expected_contracts: tuple[ExpectedContract, ...],
    ) -> None:
        if (
            ledger.job_id != identity.job_id
            or ledger.generation_id != identity.generation_id
            or ledger.query_sha256 != identity.query_sha256
            or ledger.exhaustive_profile_id != identity.exhaustive_profile_id
            or ledger.document_aggregation_policy != identity.document_aggregation_policy
            or ledger.sealed_profile_sha256 != identity.sealed_profile_sha256
            or ledger.expected_contracts != expected_contracts
        ):
            raise ExhaustiveAuditError("audit ledger identity or corpus expectation is stale")

    def _ensure_root(self) -> Path:
        if self._root.is_symlink():
            raise ExhaustiveAuditError("audit-jobs root must not be a symlink")
        created = not self._root.exists()
        self._root.mkdir(mode=0o700, parents=False, exist_ok=True)
        if created:
            self._fsync_directory(self._state_root)
        resolved = self._root.resolve(strict=True)
        if resolved.parent != self._state_root:
            raise ExhaustiveAuditError("audit-jobs root escaped local MCP state")
        return resolved

    def _job_directory(
        self,
        identity: AuditIdentity,
        *,
        create: bool,
    ) -> Path | None:
        if _JOB_ID.fullmatch(identity.job_id) is None:
            raise ExhaustiveAuditError("audit job ID is unsafe")
        if not self._root.exists() and not self._root.is_symlink():
            if not create:
                return None
            root = self._ensure_root()
        else:
            root = self._ensure_root()
        candidate = root / identity.job_id
        if candidate.is_symlink():
            raise ExhaustiveAuditError("audit job directory must not be a symlink")
        if create:
            created = not candidate.exists()
            candidate.mkdir(mode=0o700, exist_ok=True)
            if created:
                self._fsync_directory(root)
        elif not candidate.exists():
            return None
        if not candidate.is_dir():
            raise ExhaustiveAuditError("audit job path is not a directory")
        resolved = candidate.resolve(strict=True)
        if resolved.parent != root:
            raise ExhaustiveAuditError("audit job directory escaped audit-jobs root")
        return resolved

    def _required_job_directory(self, identity: AuditIdentity) -> Path:
        directory = self._job_directory(identity, create=False)
        if directory is None:
            raise ExhaustiveAuditError("audit job directory is absent")
        return directory

    def _write_progress(self, directory: Path, ledger: ExhaustiveAuditLedger) -> None:
        envelope = _ProgressEnvelope(
            ledger_sha256=canonical_sha256(ledger),
            ledger=ledger,
        )
        payload = envelope.canonical_bytes()
        path = directory / "progress.json"
        logical_growth, peak_growth = self._replacement_growth(path, payload)
        with self._write_quota_guard(
            logical_growth,
            peak_growth_bytes=peak_growth,
        ):
            self._atomic_write(path, payload)

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
                    prospective_audit_job_id=new_job_id,
                )
                if (
                    total > self.maximum_total_bytes
                    or logical_growth_bytes > self.maximum_total_bytes - total
                    or peak > self.maximum_total_bytes - total
                ):
                    raise ExhaustiveAuditError("exhaustive audit total quota rejected this write")
                if jobs > self.maximum_jobs:
                    raise ExhaustiveAuditError("exhaustive audit job quota rejected this query")
                yield
        except StorageQuotaError:
            raise ExhaustiveAuditError("MCP state quota rejected exhaustive audit write") from None

    def _validate_new_artifact(self, payload: bytes) -> None:
        if len(payload) < 1 or len(payload) > self.maximum_artifact_bytes:
            raise ExhaustiveAuditError("exhaustive audit artifact exceeds its configured cap")

    def _immutable_growth(self, path: Path, payload: bytes) -> int:
        self._validate_new_artifact(payload)
        if path.is_symlink():
            raise ExhaustiveAuditError("immutable audit file must not be a symlink")
        if not path.exists():
            return len(payload)
        existing = self._read_bytes(path, label="immutable audit file")
        if existing != payload:
            raise ExhaustiveAuditError("immutable audit file already exists with other bytes")
        return 0

    def _replacement_growth(self, path: Path, payload: bytes) -> tuple[int, int]:
        self._validate_new_artifact(payload)
        if path.is_symlink():
            raise ExhaustiveAuditError("audit progress must not be a symlink")
        if not path.exists():
            return len(payload), len(payload)
        if not path.is_file():
            raise ExhaustiveAuditError("audit progress is not a regular file")
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
            ExhaustiveAuditStore._fsync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _publish_immutable(path: Path, payload: bytes) -> None:
        if path.exists() or path.is_symlink():
            existing = ExhaustiveAuditStore._read_bytes(path, label="immutable audit file")
            if existing != payload:
                raise ExhaustiveAuditError("immutable audit file already exists with other bytes")
            return
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o400)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as output:
                output.write(payload)
                output.flush()
                os.fchmod(output.fileno(), 0o400)
                os.fsync(output.fileno())
        finally:
            os.close(descriptor)
        ExhaustiveAuditStore._fsync_directory(path.parent)

    @staticmethod
    def _read_bytes(path: Path, *, label: str) -> bytes:
        if path.is_symlink() or not path.is_file():
            raise ExhaustiveAuditError(f"{label} is missing or unsafe")
        size = path.stat().st_size
        if size < 1 or size > _MAX_LEDGER_BYTES:
            raise ExhaustiveAuditError(f"{label} exceeds its safe size bound")
        payload = path.read_bytes()
        if len(payload) != size:
            raise ExhaustiveAuditError(f"{label} changed while being read")
        return payload

    @staticmethod
    def _parse_model(payload: bytes, model: type[ModelT], *, label: str) -> ModelT:
        try:
            value = model.model_validate_json(payload)
        except Exception as exc:
            raise ExhaustiveAuditError(f"{label} is not a strict audit document") from exc
        if value.canonical_bytes() != payload:
            raise ExhaustiveAuditError(f"{label} is not canonical JSON")
        return value

    @staticmethod
    def _read_model(path: Path, model: type[ModelT], *, label: str) -> ModelT:
        return ExhaustiveAuditStore._parse_model(
            ExhaustiveAuditStore._read_bytes(path, label=label),
            model,
            label=label,
        )

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


__all__ = [
    "AuditContractScore",
    "AuditIdentity",
    "AuditNodeScore",
    "AuditViewScore",
    "EXHAUSTIVE_PROFILE_ID",
    "ExpectedContract",
    "ExhaustiveAuditError",
    "ExhaustiveAuditLedger",
    "ExhaustiveAuditStore",
    "LoadedAudit",
    "query_vector_sha256",
]
