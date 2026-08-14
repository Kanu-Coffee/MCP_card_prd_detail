"""Immutable generation sealing, verification, atomic publication and rollback."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

GENERATION_SCHEMA_VERSION = "cardrag-generation.v1"
GENERATION_PATTERN = re.compile(r"^gen-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}$")


class GenerationFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    sha256: str
    size: int = Field(ge=0)


class GenerationManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = GENERATION_SCHEMA_VERSION
    generation_id: str
    created_at: datetime
    source_snapshot_ids: tuple[str, ...]
    document_count: int = Field(ge=0)
    latest_document_count: int = Field(ge=0)
    latest_pdf_count: int = Field(ge=0)
    latest_ocr_count: int = Field(ge=0)
    latest_structure_count: int = Field(ge=0)
    latest_embedding_count: int = Field(ge=0)
    latest_index_count: int = Field(ge=0)
    historical_quarantine_count: int = Field(ge=0)
    embedding_provider: str
    embedding_model: str
    embedding_dimension: int = Field(gt=0)
    chunk_policy: str
    taxonomy_version: str
    files: tuple[GenerationFile, ...]
    quality_report_sha256: str
    retrieval_report_sha256: str

    @model_validator(mode="after")
    def latest_coverage_is_complete(self) -> Self:
        counts = (
            self.latest_pdf_count,
            self.latest_ocr_count,
            self.latest_structure_count,
            self.latest_embedding_count,
            self.latest_index_count,
        )
        if any(value != self.latest_document_count for value in counts):
            raise ValueError("latest document coverage must be 100% at every stage")
        if not GENERATION_PATTERN.fullmatch(self.generation_id):
            raise ValueError("invalid generation ID")
        return self

    def canonical_bytes(self) -> bytes:
        return (
            json.dumps(
                self.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            + "\n"
        ).encode()

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class CurrentPointer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "cardrag-current-pointer.v1"
    generation_id: str
    manifest_sha256: str
    published_at: datetime
    previous_generation_id: str | None = None


class GenerationVerificationError(RuntimeError):
    pass


def new_generation_id(now: datetime | None = None, entropy: str | None = None) -> str:
    timestamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    suffix = entropy or uuid.uuid4().hex[:12]
    if not re.fullmatch(r"[0-9a-f]{12}", suffix):
        raise ValueError("generation entropy must be 12 lowercase hex characters")
    return f"gen-{timestamp}-{suffix}"


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _atomic_json(path: Path, model: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (
        json.dumps(model.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode()
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temp.open("wb") as output:
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temp.unlink(missing_ok=True)


class GenerationStore:
    def __init__(self, root: Path, build_root: Path) -> None:
        self.root = root.resolve()
        self.build_root = build_root.resolve()
        self.generations = self.root / "generations"
        self.current_path = self.root / "current.json"
        self.history_path = self.root / "publication-history.jsonl"
        self.lock_path = self.root / ".publish.lock"
        self.generations.mkdir(parents=True, exist_ok=True)
        self.build_root.mkdir(parents=True, exist_ok=True)

    def candidate_path(self, generation_id: str) -> Path:
        self._validate_id(generation_id)
        candidate = self.build_root / generation_id
        candidate.mkdir(mode=0o750, parents=True, exist_ok=False)
        return candidate

    @staticmethod
    def _validate_id(generation_id: str) -> None:
        if not GENERATION_PATTERN.fullmatch(generation_id):
            raise ValueError("invalid generation ID")

    def build_file_inventory(self, candidate: Path) -> tuple[GenerationFile, ...]:
        candidate = candidate.resolve()
        if not candidate.is_relative_to(self.build_root):
            raise ValueError("candidate is outside build root")
        files: list[GenerationFile] = []
        for path in sorted(candidate.rglob("*")):
            if not path.is_file() or path.name in {"manifest.json", "READY"}:
                continue
            resolved = path.resolve()
            if not resolved.is_relative_to(candidate):
                raise ValueError("candidate contains a symlink escape")
            digest, size = _hash_file(path)
            files.append(
                GenerationFile(path=path.relative_to(candidate).as_posix(), sha256=digest, size=size)
            )
        return tuple(files)

    def seal(self, candidate: Path, manifest: GenerationManifest) -> Path:
        candidate = candidate.resolve()
        expected = self.build_file_inventory(candidate)
        if expected != manifest.files:
            raise GenerationVerificationError("manifest file inventory differs from candidate bytes")
        manifest_path = candidate / "manifest.json"
        _atomic_json(manifest_path, manifest)
        self.verify_path(candidate, expected_generation_id=manifest.generation_id, require_ready=False)
        ready_body = json.dumps(
            {"generation_id": manifest.generation_id, "manifest_sha256": manifest.sha256},
            sort_keys=True,
        ).encode()
        ready = candidate / "READY"
        with ready.open("xb") as output:
            output.write(ready_body)
            output.flush()
            os.fsync(output.fileno())
        # Seal files and all child directories before publication. Runtime
        # writers build a new candidate rather than editing a sealed snapshot.
        for path in sorted(candidate.rglob("*"), reverse=True):
            if path.is_file():
                os.chmod(path, 0o440)
            elif path.is_dir():
                os.chmod(path, 0o550)  # noqa: S103 - sealed read/execute directory
        destination = self.generations / manifest.generation_id
        if destination.exists():
            raise FileExistsError("generation already exists")
        os.replace(candidate, destination)
        os.chmod(destination, 0o550)  # noqa: S103 - sealed read/execute directory
        return destination

    def verify_path(
        self,
        generation_path: Path,
        *,
        expected_generation_id: str | None = None,
        require_ready: bool = True,
    ) -> GenerationManifest:
        generation_path = generation_path.resolve()
        allowed_roots = (self.generations, self.build_root)
        if not any(generation_path.is_relative_to(root) for root in allowed_roots):
            raise GenerationVerificationError("generation path escaped configured roots")
        try:
            manifest = GenerationManifest.model_validate_json(
                (generation_path / "manifest.json").read_bytes()
            )
        except Exception as exc:
            raise GenerationVerificationError("generation manifest is invalid") from exc
        if expected_generation_id and manifest.generation_id != expected_generation_id:
            raise GenerationVerificationError("generation ID mismatch")
        for entry in manifest.files:
            target = (generation_path / entry.path).resolve()
            if not target.is_relative_to(generation_path) or not target.is_file():
                raise GenerationVerificationError(f"missing or escaped generation file: {entry.path}")
            digest, size = _hash_file(target)
            if digest != entry.sha256 or size != entry.size:
                raise GenerationVerificationError(f"generation checksum mismatch: {entry.path}")
        if require_ready:
            try:
                ready = json.loads((generation_path / "READY").read_text(encoding="utf-8"))
            except Exception as exc:
                raise GenerationVerificationError("generation has no valid READY seal") from exc
            if ready != {"generation_id": manifest.generation_id, "manifest_sha256": manifest.sha256}:
                raise GenerationVerificationError("READY seal does not match manifest")
        return manifest

    @contextmanager
    def publication_lock(self) -> Iterator[None]:
        """Serialize DB/FS publication coordinators on this generation root."""

        self.root.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def current(self) -> CurrentPointer:
        pointer = CurrentPointer.model_validate_json(self.current_path.read_bytes())
        manifest = self.verify_path(
            self.generations / pointer.generation_id,
            expected_generation_id=pointer.generation_id,
        )
        if manifest.sha256 != pointer.manifest_sha256:
            raise GenerationVerificationError("current pointer manifest checksum mismatch")
        return pointer

    def publish(self, generation_id: str) -> CurrentPointer:
        self._validate_id(generation_id)
        with self.publication_lock():
            return self.publish_locked(generation_id)

    def publish_locked(self, generation_id: str) -> CurrentPointer:
        """Publish while the caller holds :meth:`publication_lock`."""

        self._validate_id(generation_id)
        manifest = self.verify_path(self.generations / generation_id, expected_generation_id=generation_id)
        previous: CurrentPointer | None = None
        if self.current_path.exists():
            previous = CurrentPointer.model_validate_json(self.current_path.read_bytes())
        pointer = CurrentPointer(
            generation_id=generation_id,
            manifest_sha256=manifest.sha256,
            published_at=datetime.now(UTC),
            previous_generation_id=previous.generation_id if previous else None,
        )
        _atomic_json(self.current_path, pointer)
        history_size: int | None = None
        try:
            with self.history_path.open("a+b") as history:
                history.seek(0, os.SEEK_END)
                history_size = history.tell()
                history.write(json.dumps(pointer.model_dump(mode="json"), sort_keys=True).encode() + b"\n")
                history.flush()
                os.fsync(history.fileno())
        except Exception as history_error:
            try:
                if history_size is not None:
                    with self.history_path.open("r+b") as history:
                        history.truncate(history_size)
                        history.flush()
                        os.fsync(history.fileno())
                if previous is None:
                    self.current_path.unlink(missing_ok=True)
                    directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                else:
                    _atomic_json(self.current_path, previous)
            except Exception as restore_error:
                raise GenerationVerificationError(
                    "publication history failed and current pointer restoration failed"
                ) from restore_error
            raise GenerationVerificationError(
                "publication history failed; current pointer was restored"
            ) from history_error
        return pointer

    def deactivate_locked(self, expected_generation_id: str) -> CurrentPointer:
        """Remove the serving pointer while the caller holds publication authority.

        This is the fail-closed rollback for a first-ever publication, where no
        previous generation exists.  The expected ID prevents an operator from
        deactivating a generation that advanced after they inspected it.
        """

        self._validate_id(expected_generation_id)
        pointer = self.current()
        if pointer.generation_id != expected_generation_id:
            raise GenerationVerificationError("current generation changed before deactivation")
        self.current_path.unlink()
        directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return pointer

    def restore_pointer_locked(self, pointer: CurrentPointer) -> None:
        """Restore a previously verified pointer during DB compensation."""

        self.verify_path(
            self.generations / pointer.generation_id,
            expected_generation_id=pointer.generation_id,
        )
        _atomic_json(self.current_path, pointer)

    def rollback(self, generation_id: str | None = None) -> CurrentPointer:
        current = self.current()
        target = generation_id or current.previous_generation_id
        if not target:
            raise GenerationVerificationError("no previous generation is recorded")
        if target == current.generation_id:
            raise ValueError("rollback target is already current")
        return self.publish(target)

    @contextmanager
    def open_current(self) -> Iterator[tuple[CurrentPointer, Path]]:
        # Pin the pointer value once; a concurrent publish cannot mix this request.
        pointer = self.current()
        path = self.generations / pointer.generation_id
        yield pointer, path

    def prune(
        self,
        *,
        pinned: set[str] | None = None,
        database_generation_ids: set[str] | None = None,
        keep_successful: int = 3,
        failed_older_than: timedelta = timedelta(days=7),
        now: datetime | None = None,
    ) -> list[str]:
        """Remove expired trees using either FS-only or DB-authoritative retention.

        ``database_generation_ids`` is supplied by the DB-backed coordinator after
        it has applied the canonical created-at/pin policy.  In that mode the
        filesystem must not independently retain its newest READY directories by
        mtime: every sealed directory absent from the database set is an orphan.
        The mtime-based latest-three policy remains available to standalone store
        users that have no database authority.
        """

        pinned = pinned or set()
        current_id = self.current().generation_id if self.current_path.exists() else None
        candidates: list[tuple[datetime, Path, bool]] = []
        for path in self.generations.iterdir():
            if not path.is_dir() or not GENERATION_PATTERN.fullmatch(path.name):
                continue
            created = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            candidates.append((created, path, (path / "READY").exists()))
        if database_generation_ids is None:
            successful = sorted((item for item in candidates if item[2]), reverse=True)
            retain = {item[1].name for item in successful[:keep_successful]}
        else:
            retain = set(database_generation_ids)
        retain |= pinned | ({current_id} if current_id else set())
        removed: list[str] = []
        cutoff = (now or datetime.now(UTC)) - failed_older_than
        for created, path, ready in candidates:
            if path.name in retain:
                continue
            if ready or created < cutoff:
                self._remove_generation_tree(path)
                removed.append(path.name)
        candidate_retain = retain if database_generation_ids is not None else pinned
        for path in self.build_root.iterdir():
            if (
                not path.is_dir()
                or not GENERATION_PATTERN.fullmatch(path.name)
                or path.name in candidate_retain
            ):
                continue
            created = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            if created < cutoff:
                self._remove_generation_tree(path)
                removed.append(path.name)
        return sorted(removed)

    def remove_generation_trees(self, generation_id: str) -> bool:
        """Remove the exact published and candidate trees for a DB-deleted ID.

        The caller coordinates retention locking.  All deletion still flows
        through :meth:`_remove_generation_tree`, which validates containment and
        restores owner permissions on immutable sealed files before removal.
        """

        self._validate_id(generation_id)
        removed = False
        for root in (self.generations, self.build_root):
            target = root / generation_id
            if target.is_symlink():
                raise ValueError("retention target must not be a symlink")
            if not target.exists():
                continue
            self._remove_generation_tree(target)
            removed = True
        return removed

    def _remove_generation_tree(self, path: Path) -> None:
        """Operator retention deletion for an explicitly validated generation."""
        if path.is_symlink():
            raise ValueError("retention target must not be a symlink")
        resolved = path.resolve(strict=True)
        allowed_parents = {self.generations.resolve(), self.build_root.resolve()}
        if resolved.parent not in allowed_parents or not GENERATION_PATTERN.fullmatch(resolved.name):
            raise ValueError("retention target is not a generation directory")
        # Sealed trees intentionally lack write bits. Restore only owner bits on
        # this exact validated tree before the deliberate retention deletion.
        for child in resolved.rglob("*"):
            if child.is_dir() and not child.is_symlink():
                os.chmod(child, 0o700)
            elif child.is_file() and not child.is_symlink():
                os.chmod(child, 0o600)
        os.chmod(resolved, 0o700)
        shutil.rmtree(resolved)
