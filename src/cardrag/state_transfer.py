"""Portable, fail-closed export and restore of CardRAG runtime state.

The package deliberately contains only the two PostgreSQL logical dumps, the
content-addressed object store, the immutable generation store, and optionally
sealed legacy import bundles.  Build workspaces, rendered-page caches, Codex
credentials, and deployment secrets are never accepted as inputs.

Every public operation validates an archive-root sentinel.  Exports are built
in a marker-owned sibling directory and published with one rename after READY
is written.  Restores validate every byte before invoking PostgreSQL tools or
installing a filesystem tree.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess  # noqa: S404 - fixed-argument PostgreSQL client execution
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files as resource_files
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol, Self
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from cardrag.db import Postgres
from cardrag.domain.canonical import canonical_json_bytes
from cardrag.generation import CurrentPointer, GenerationManifest
from cardrag.storage.paths import portable_relative_path

STATE_PACKAGE_SCHEMA: Literal["cardrag-portable-state.v1"] = "cardrag-portable-state.v1"
STATE_READY_SCHEMA: Literal["cardrag-portable-state-ready.v1"] = "cardrag-portable-state-ready.v1"
REFERENCE_REPORT_SCHEMA: Literal["cardrag-state-reference-check.v1"] = "cardrag-state-reference-check.v1"
VERIFICATION_REPORT_SCHEMA: Literal["cardrag-state-export-verification.v1"] = (
    "cardrag-state-export-verification.v1"
)
RESTORE_REPORT_SCHEMA: Literal["cardrag-state-restore-verification.v1"] = (
    "cardrag-state-restore-verification.v1"
)
ARCHIVE_SENTINEL_NAME = ".cardrag-archive-root"
ARCHIVE_SENTINEL_CONTENT = "cardrag-archive-v1"
ARCHIVE_SOURCE_NAME = ".cardrag-archive-mount-source"
STAGING_MARKER_NAME = "reports/export-operation.json"
RESTORE_STAGING_MARKER_SUFFIX = "restore-owner.json"
POSTGRES_MAJOR = 17

_COPY_CHUNK_SIZE = 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_OBJECT_KEY_RE = re.compile(r"^sha256/([0-9a-f]{2})/([0-9a-f]{64})$")
_EXPORT_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_PACKAGE_NAME_RE = re.compile(r"^cardrag-state-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}$")
_SAFE_DATABASE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,62}$")
RuntimeRole = Literal["cardrag", "cardrag_worker", "cardrag_mcp", "keycloak"]
_RUNTIME_ROLES: tuple[RuntimeRole, ...] = (
    "cardrag",
    "cardrag_worker",
    "cardrag_mcp",
    "keycloak",
)
_ROLE_ALLOWLIST = frozenset(_RUNTIME_ROLES)
_DEPLOYMENT_PAYLOAD_FILES = frozenset(
    {"stack-redacted.yaml", "image-digests.json", "release-manifest.json"}
)
_DEPLOYMENT_FILES = _DEPLOYMENT_PAYLOAD_FILES | {"deployment-set.json"}
_IMAGE_REFERENCE_RE = re.compile(r"^\S+@sha256:[0-9a-f]{64}$")
_POSSIBLE_SECRET_ENTRY = re.compile(
    r"(?i)^\s*([^#:\n]*(?:password|api[_-]?key|access[_-]?token|client[_-]?secret|"
    r"database[_-]?url|authorization|private[_-]?key)[^:\n]*)"
    r"\s*:\s*(.*?)\s*$"
)
_YAML_VALUE_ENTRY = re.compile(r"^\s*[^#: \n][^:\n]*\s*:\s*(.*?)\s*$")
_ENV_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_SAFE_ENV_REFERENCE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*(?::\?[^}]*)?\}$")
_SENSITIVE_NAME = re.compile(
    r"(?i)(password|secret|token|api[_-]?key|database[_-]?url|authorization|"
    r"private[_-]?key|dsn)"
)

Sha256Hex = StringConstraints(pattern=r"^[0-9a-f]{64}$")


class StateTransferError(RuntimeError):
    """Base exception for a refused or failed state operation."""


class ArchiveSentinelError(StateTransferError):
    """The configured archive root is not the expected mounted repository."""


class StateIntegrityError(StateTransferError):
    """An input tree or portable package failed integrity validation."""


class StateQuiescenceError(StateTransferError):
    """A writer, active job, or changing database epoch made export unsafe."""


class PostgresToolError(StateTransferError):
    """A PostgreSQL client or server is incompatible, or a tool failed."""


class StateFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    sha256: str
    size_bytes: int = Field(ge=0)
    category: Literal["database", "object", "generation", "import", "deployment", "report"]

    @model_validator(mode="after")
    def validate_portable_file(self) -> Self:
        portable_relative_path(self.path)
        if not _SHA256_RE.fullmatch(self.sha256):
            raise ValueError("invalid file SHA-256")
        return self


class DatabaseDump(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    database: Literal["cardrag", "keycloak"]
    source_database_name: str
    path: str
    sha256: str
    size_bytes: int = Field(gt=0)
    format: Literal["postgresql-custom"] = "postgresql-custom"
    postgres_major: Literal[17] = 17

    @model_validator(mode="after")
    def validate_dump(self) -> Self:
        portable_relative_path(self.path)
        if not _SHA256_RE.fullmatch(self.sha256):
            raise ValueError("invalid dump SHA-256")
        return self


class DatabaseStateSnapshot(BaseModel):
    """Portable database facts which bind a dump to filesystem state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    active_generation_id: str | None = None
    active_manifest_sha256: str | None = None
    active_root_key: str | None = None
    previous_generation_id: str | None = None
    previous_manifest_sha256: str | None = None
    pinned_generation_ids: tuple[str, ...] = ()
    schema_migrations: tuple[tuple[int, str], ...] = ()
    pgvector_version: str | None = None
    object_keys: tuple[str, ...] = ()

    @model_validator(mode="after")
    def active_fields_are_atomic(self) -> Self:
        active = (
            self.active_generation_id,
            self.active_manifest_sha256,
            self.active_root_key,
        )
        if any(value is None for value in active) and any(value is not None for value in active):
            raise ValueError("active generation snapshot is incomplete")
        if self.active_manifest_sha256 is not None and not _SHA256_RE.fullmatch(self.active_manifest_sha256):
            raise ValueError("active manifest SHA-256 is invalid")
        if self.active_root_key is not None:
            portable_relative_path(self.active_root_key)
        previous = (self.previous_generation_id, self.previous_manifest_sha256)
        if any(value is None for value in previous) and any(value is not None for value in previous):
            raise ValueError("previous generation snapshot is incomplete")
        if self.previous_manifest_sha256 is not None and not _SHA256_RE.fullmatch(
            self.previous_manifest_sha256
        ):
            raise ValueError("previous manifest SHA-256 is invalid")
        if tuple(sorted(set(self.object_keys))) != self.object_keys:
            raise ValueError("object keys must be sorted and unique")
        if tuple(sorted(set(self.pinned_generation_ids))) != self.pinned_generation_ids:
            raise ValueError("generation pins must be sorted and unique")
        return self

    @property
    def epoch_sha256(self) -> str:
        return _sha256_bytes(canonical_json_bytes(self))


class QuiescenceReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pipeline_runs: int = Field(ge=0)
    jobs: int = Field(ge=0)
    legacy_imports: int = Field(ge=0)
    other_database_sessions: int = Field(ge=0)
    status: Literal["passed", "failed"]

    @model_validator(mode="after")
    def status_matches_counts(self) -> Self:
        passed = not any((self.pipeline_runs, self.jobs, self.legacy_imports, self.other_database_sessions))
        if (self.status == "passed") is not passed:
            raise ValueError("quiescence status differs from measured counts")
        return self


class ReferenceCheckReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["cardrag-state-reference-check.v1"] = REFERENCE_REPORT_SCHEMA
    database_epoch_sha256: str
    active_generation_id: str | None
    active_manifest_sha256: str | None
    filesystem_generation_id: str | None
    filesystem_manifest_sha256: str | None
    referenced_object_count: int = Field(ge=0)
    present_object_count: int = Field(ge=0)
    missing_object_keys: tuple[str, ...] = ()
    invalid_object_keys: tuple[str, ...] = ()
    status: Literal["passed", "failed"]

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        if not _SHA256_RE.fullmatch(self.database_epoch_sha256):
            raise ValueError("invalid database epoch SHA-256")
        passed = (
            not self.missing_object_keys
            and not self.invalid_object_keys
            and self.active_generation_id == self.filesystem_generation_id
            and self.active_manifest_sha256 == self.filesystem_manifest_sha256
            and self.referenced_object_count == self.present_object_count
        )
        if (self.status == "passed") is not passed:
            raise ValueError("reference status differs from measured results")
        return self


class ExportVerificationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["cardrag-state-export-verification.v1"] = VERIFICATION_REPORT_SCHEMA
    source_regular_files: int = Field(ge=0)
    object_files: int = Field(ge=0)
    generation_files: int = Field(ge=0)
    import_files: int = Field(ge=0)
    database_dumps: int = Field(ge=0)
    symlinks: Literal[0] = 0
    special_files: Literal[0] = 0
    excluded_paths: tuple[str, ...]
    status: Literal["passed"] = "passed"


class RuntimeCompatibility(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    application_version: str
    image_revision: str
    embedding_provider: str | None = None
    embedding_model: str | None = None
    embedding_dimension: int | None = Field(default=None, gt=0)
    image_digests: dict[str, str] = Field(default_factory=dict)


class StateManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["cardrag-portable-state.v1"] = STATE_PACKAGE_SCHEMA
    export_id: str
    created_at: AwareDatetime
    postgres_major: Literal[17] = 17
    includes_imports: bool
    database_epoch_sha256: str
    database_state: DatabaseStateSnapshot
    database_dumps: tuple[DatabaseDump, ...]
    compatibility: RuntimeCompatibility
    runtime_fingerprint_sha256: str
    legacy_bundle_ids: tuple[str, ...] = ()
    files: tuple[StateFile, ...]
    object_count: int = Field(ge=0)
    object_bytes: int = Field(ge=0)
    object_inventory_sha256: str
    exclusions: tuple[str, ...] = (
        "build_workspace",
        "page_cache",
        "codex_auth",
        "secrets",
    )

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        if not _EXPORT_ID_RE.fullmatch(self.export_id):
            raise ValueError("invalid export ID")
        if self.database_epoch_sha256 != self.database_state.epoch_sha256:
            raise ValueError("database epoch digest mismatch")
        if self.runtime_fingerprint_sha256 != _sha256_bytes(canonical_json_bytes(self.compatibility)):
            raise ValueError("runtime compatibility fingerprint mismatch")
        if tuple(sorted(set(self.legacy_bundle_ids))) != self.legacy_bundle_ids:
            raise ValueError("legacy bundle IDs must be sorted and unique")
        if not _SHA256_RE.fullmatch(self.object_inventory_sha256):
            raise ValueError("invalid object inventory SHA-256")
        paths = tuple(item.path for item in self.files)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("state files must be sorted and unique")
        if {item.database for item in self.database_dumps} != {"cardrag", "keycloak"}:
            raise ValueError("both cardrag and keycloak dumps are required")
        file_by_path = {item.path: item for item in self.files}
        for dump in self.database_dumps:
            entry = file_by_path.get(dump.path)
            if entry is None or (entry.sha256, entry.size_bytes) != (
                dump.sha256,
                dump.size_bytes,
            ):
                raise ValueError("database dump is absent from file inventory")
        objects = [item for item in self.files if item.category == "object"]
        if self.object_count != len(objects) or self.object_bytes != sum(item.size_bytes for item in objects):
            raise ValueError("object inventory counters are inconsistent")
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self) + b"\n"


class StateReady(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["cardrag-portable-state-ready.v1"] = STATE_READY_SCHEMA
    export_id: str
    state_manifest_sha256: str
    checksums_sha256: str

    @model_validator(mode="after")
    def validate_ready(self) -> Self:
        if not _EXPORT_ID_RE.fullmatch(self.export_id):
            raise ValueError("invalid export ID")
        if not _SHA256_RE.fullmatch(self.state_manifest_sha256) or not _SHA256_RE.fullmatch(
            self.checksums_sha256
        ):
            raise ValueError("invalid READY digest")
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self) + b"\n"


class PackageVerification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    package_path: Path
    manifest: StateManifest
    checked_files: int = Field(ge=0)
    checked_bytes: int = Field(ge=0)
    status: Literal["passed"] = "passed"


class RestoreVerificationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["cardrag-state-restore-verification.v1"] = RESTORE_REPORT_SCHEMA
    export_id: str
    database_epoch_sha256: str
    object_files: int = Field(ge=0)
    generation_files: int = Field(ge=0)
    database_state_matches: bool
    status: Literal["passed", "failed"]

    @model_validator(mode="after")
    def validate_status(self) -> Self:
        passed = self.database_state_matches
        if (self.status == "passed") is not passed:
            raise ValueError("restore status differs from verification")
        return self


class StateProgress(BaseModel):
    """Path-free progress payload suitable for one-line operator logs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: Literal["export", "restore", "verify"]
    operation_id: str
    phase: str
    files_completed: int = Field(ge=0)
    bytes_completed: int = Field(ge=0)
    elapsed_seconds: float = Field(ge=0)
    bytes_per_second: float = Field(ge=0)
    total_files: int | None = Field(default=None, ge=0)
    total_bytes: int | None = Field(default=None, ge=0)
    eta_seconds: float | None = Field(default=None, ge=0)


ProgressCallback = Callable[[StateProgress], None]


class _OperationProgress:
    """Rate-limited, path-free progress for byte-oriented filesystem work."""

    _FILE_INTERVAL = 100
    _TIME_INTERVAL_SECONDS = 30.0

    def __init__(
        self,
        operation: Literal["export", "restore", "verify"],
        operation_id: str,
        callback: ProgressCallback | None,
        clock: Callable[[], float],
        started_at: float,
    ) -> None:
        self.operation = operation
        self.operation_id = operation_id
        self.callback = callback
        self.clock = clock
        self.started_at = started_at
        self.phase = ""
        self.phase_started_at = started_at
        self.last_emitted_at = started_at
        self.last_emitted_files = 0
        self.last_emitted_bytes = 0
        self.files_completed = 0
        self.bytes_completed = 0
        self.total_files: int | None = None
        self.total_bytes: int | None = None
        self.phase_has_emitted = False

    def lifecycle(
        self,
        phase: str,
        *,
        entries: Sequence[StateFile] = (),
    ) -> None:
        if self.callback is None:
            return
        now = self.clock()
        elapsed = max(0.0, now - self.started_at)
        bytes_completed = sum(item.size_bytes for item in entries)
        total_files = len(entries) if entries else None
        total_bytes = bytes_completed if entries else None
        self.callback(
            StateProgress(
                operation=self.operation,
                operation_id=self.operation_id,
                phase=phase,
                files_completed=len(entries),
                bytes_completed=bytes_completed,
                elapsed_seconds=elapsed,
                bytes_per_second=bytes_completed / elapsed if elapsed else 0.0,
                total_files=total_files,
                total_bytes=total_bytes,
                eta_seconds=0.0 if entries else None,
            )
        )

    def begin_phase(
        self,
        phase: str,
        *,
        total_files: int | None,
        total_bytes: int | None,
    ) -> None:
        now = self.clock()
        self.phase = phase
        self.phase_started_at = now
        self.last_emitted_at = now
        self.last_emitted_files = 0
        self.last_emitted_bytes = 0
        self.files_completed = 0
        self.bytes_completed = 0
        self.total_files = total_files
        self.total_bytes = total_bytes
        self.phase_has_emitted = False

    def set_phase_totals(self, *, total_files: int, total_bytes: int) -> None:
        self.total_files = total_files
        self.total_bytes = total_bytes

    def advance(self, *, files: int = 0, bytes_count: int = 0) -> None:
        if files < 0 or bytes_count < 0:
            raise ValueError("state progress counters cannot move backwards")
        self.files_completed += files
        self.bytes_completed += bytes_count
        if self.callback is None:
            return
        now = self.clock()
        if (
            self.files_completed - self.last_emitted_files >= self._FILE_INTERVAL
            or now - self.last_emitted_at >= self._TIME_INTERVAL_SECONDS
        ):
            self._emit(now)

    def finish_phase(self) -> None:
        if self.callback is None:
            return
        now = self.clock()
        if (
            not self.phase_has_emitted
            or self.files_completed != self.last_emitted_files
            or self.bytes_completed != self.last_emitted_bytes
        ):
            self._emit(now)

    def _emit(self, now: float) -> None:
        elapsed = max(0.0, now - self.phase_started_at)
        rate = self.bytes_completed / elapsed if elapsed else 0.0
        eta: float | None = None
        if self.total_bytes is not None:
            remaining = max(0, self.total_bytes - self.bytes_completed)
            if remaining == 0:
                eta = 0.0
            elif rate > 0:
                eta = remaining / rate
        if self.callback is not None:
            self.callback(
                StateProgress(
                    operation=self.operation,
                    operation_id=self.operation_id,
                    phase=self.phase,
                    files_completed=self.files_completed,
                    bytes_completed=self.bytes_completed,
                    elapsed_seconds=max(0.0, now - self.started_at),
                    bytes_per_second=rate,
                    total_files=self.total_files,
                    total_bytes=self.total_bytes,
                    eta_seconds=eta,
                )
            )
        self.last_emitted_at = now
        self.last_emitted_files = self.files_completed
        self.last_emitted_bytes = self.bytes_completed
        self.phase_has_emitted = True


@dataclass(frozen=True, slots=True)
class PostgresToolConfig:
    """Secret-file based connection details for PostgreSQL maintenance tools."""

    host: str
    port: int
    user: str
    password_file: Path
    cardrag_database: str = "cardrag"
    keycloak_database: str = "keycloak"
    maintenance_database: str = "postgres"
    pg_dump_bin: str = "pg_dump"
    pg_restore_bin: str = "pg_restore"
    psql_bin: str = "psql"

    def __post_init__(self) -> None:
        if not self.host or "\x00" in self.host:
            raise ValueError("invalid PostgreSQL host")
        if not 1 <= self.port <= 65535:
            raise ValueError("invalid PostgreSQL port")
        if not self.user or "\x00" in self.user:
            raise ValueError("invalid PostgreSQL user")
        for name in (
            self.cardrag_database,
            self.keycloak_database,
            self.maintenance_database,
        ):
            if not _SAFE_DATABASE_NAME_RE.fullmatch(name):
                raise ValueError("invalid PostgreSQL database name")
        for command in (self.pg_dump_bin, self.pg_restore_bin, self.psql_bin):
            if not command or "\x00" in command:
                raise ValueError("invalid PostgreSQL client command")

    def database_name(self, logical_name: Literal["cardrag", "keycloak"]) -> str:
        return self.cardrag_database if logical_name == "cardrag" else self.keycloak_database


@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class ProcessExecutor(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        input_bytes: bytes | None = None,
    ) -> ProcessResult: ...


def _default_process_executor(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    input_bytes: bytes | None = None,
) -> ProcessResult:
    completed = subprocess.run(  # noqa: S603 - argv comes from validated fixed fields
        list(argv),
        env=dict(env),
        input=input_bytes,
        capture_output=True,
        check=False,
    )
    return ProcessResult(
        completed.returncode,
        completed.stdout.decode("utf-8", errors="replace"),
        completed.stderr.decode("utf-8", errors="replace"),
    )


@dataclass(frozen=True, slots=True)
class RolePasswordSecret:
    role: RuntimeRole
    password_file: Path


class PortableDatabaseRestorer(Protocol):
    def preflight(
        self,
        *,
        package: Path,
        manifest: StateManifest,
        role_password_secrets: Sequence[RolePasswordSecret],
    ) -> object: ...

    def execute(self, preflight: object) -> DatabaseStateSnapshot: ...


@dataclass(frozen=True, slots=True, repr=False)
class _ValidatedRolePasswords:
    """In-memory secret snapshot deliberately excluded from repr/log output."""

    values: tuple[tuple[RuntimeRole, str], ...]


class PostgresToolRunner:
    """Fixed-argument PostgreSQL 17 subprocess contract.

    The password is read immediately before a child process is started and is
    passed only as ``PGPASSWORD``.  It is never put in argv or an exception.
    """

    def __init__(
        self,
        config: PostgresToolConfig,
        *,
        executor: ProcessExecutor = _default_process_executor,
    ) -> None:
        self.config = config
        self._execute = executor

    def validate(self) -> None:
        for command in (
            self.config.pg_dump_bin,
            self.config.pg_restore_bin,
            self.config.psql_bin,
        ):
            result = self._run((command, "--version"), needs_password=False)
            version_text = f"{result.stdout}\n{result.stderr}"
            match = re.search(r"\b(\d+)(?:\.\d+)?\b", version_text)
            if match is None or int(match.group(1)) != POSTGRES_MAJOR:
                raise PostgresToolError(f"PostgreSQL client must be major {POSTGRES_MAJOR}: {command}")
        version = self.query_scalar(self.config.maintenance_database, "SHOW server_version_num")
        try:
            major = int(version) // 10_000
        except ValueError as exc:
            raise PostgresToolError("PostgreSQL server returned an invalid version") from exc
        if major != POSTGRES_MAJOR:
            raise PostgresToolError(f"PostgreSQL server must be major {POSTGRES_MAJOR}")

    def validate_export_sources(self) -> None:
        """Require both source databases before a state export starts."""

        for database in (self.config.cardrag_database, self.config.keycloak_database):
            if not self.database_exists(database):
                raise PostgresToolError(f"state export source database is missing: {database}")

    def dump(self, logical_name: Literal["cardrag", "keycloak"], destination: Path) -> None:
        destination = _absolute_path(destination, "database dump destination")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() or destination.is_symlink():
            raise FileExistsError("database dump destination already exists")
        database = self.config.database_name(logical_name)
        command = (
            self.config.pg_dump_bin,
            "--host",
            self.config.host,
            "--port",
            str(self.config.port),
            "--username",
            self.config.user,
            "--dbname",
            database,
            "--format=custom",
            "--no-password",
            "--file",
            str(destination),
        )
        try:
            self._run(command)
            if destination.is_symlink() or not destination.is_file():
                raise PostgresToolError(f"pg_dump did not create the {logical_name} dump")
            if destination.stat().st_size == 0:
                raise PostgresToolError(f"pg_dump created an empty {logical_name} dump")
            _fsync_regular_file(destination)
            _fsync_directory(destination.parent)
        except Exception:
            destination.unlink(missing_ok=True)
            raise

    def restore(
        self,
        logical_name: Literal["cardrag", "keycloak"],
        source: Path,
        *,
        target_database: str | None = None,
        require_empty: bool = True,
    ) -> None:
        source = _regular_file(source, "database dump")
        database = target_database or self.config.database_name(logical_name)
        if not _SAFE_DATABASE_NAME_RE.fullmatch(database):
            raise ValueError("invalid PostgreSQL restore database")
        if require_empty and not self.database_is_empty(database):
            raise PostgresToolError(f"restore target database is not empty: {logical_name}")
        command = (
            self.config.pg_restore_bin,
            "--host",
            self.config.host,
            "--port",
            str(self.config.port),
            "--username",
            self.config.user,
            "--dbname",
            database,
            "--no-password",
            "--exit-on-error",
            "--single-transaction",
            str(source),
        )
        self._run(command)

    def database_is_empty(self, database: str) -> bool:
        value = self.query_scalar(
            database,
            """
            SELECT count(*)
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
            WHERE c.relkind IN ('r','p','v','m','S','f')
              AND n.nspname NOT IN ('pg_catalog','information_schema')
              AND n.nspname !~ '^pg_toast'
            """,
        )
        try:
            return int(value) == 0
        except ValueError as exc:
            raise PostgresToolError("PostgreSQL returned an invalid emptiness count") from exc

    def query_scalar(self, database: str, statement: str) -> str:
        if not _SAFE_DATABASE_NAME_RE.fullmatch(database):
            raise ValueError("invalid PostgreSQL database name")
        command = (
            self.config.psql_bin,
            "--host",
            self.config.host,
            "--port",
            str(self.config.port),
            "--username",
            self.config.user,
            "--dbname",
            database,
            "--no-password",
            "-X",
            "--set=ON_ERROR_STOP=1",
            "--tuples-only",
            "--no-align",
            "--command",
            statement.strip(),
        )
        result = self._run(command)
        values = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if len(values) != 1:
            raise PostgresToolError("PostgreSQL scalar query returned an unexpected result")
        return values[0]

    def execute_sql(self, database: str, statement: str) -> None:
        """Execute trusted, secret-free maintenance SQL passed over stdin."""

        if not _SAFE_DATABASE_NAME_RE.fullmatch(database):
            raise ValueError("invalid PostgreSQL database name")
        command = (
            self.config.psql_bin,
            "--host",
            self.config.host,
            "--port",
            str(self.config.port),
            "--username",
            self.config.user,
            "--dbname",
            database,
            "--no-password",
            "-X",
            "--set=ON_ERROR_STOP=1",
            "--file=-",
        )
        self._run(command, input_bytes=statement.encode("utf-8"))

    def validate_role_password_secrets(
        self,
        secrets: Sequence[RolePasswordSecret],
    ) -> _ValidatedRolePasswords:
        """Read and validate every role secret before restore mutates PostgreSQL."""

        by_role = {item.role: item for item in secrets}
        if set(by_role) != _ROLE_ALLOWLIST or len(by_role) != len(secrets):
            raise PostgresToolError("restore requires one password secret for every runtime role")
        values: list[tuple[RuntimeRole, str]] = []
        for role in _RUNTIME_ROLES:
            password_path = _regular_file(by_role[role].password_file, f"{role} password secret")
            mode = stat.S_IMODE(password_path.stat().st_mode)
            if mode & 0o022:
                raise PostgresToolError("runtime role password secret has unsafe permissions")
            try:
                password = password_path.read_text(encoding="utf-8").rstrip("\r\n")
            except UnicodeError as exc:
                raise PostgresToolError("runtime role password secret is not UTF-8") from exc
            if not password or "\x00" in password:
                raise PostgresToolError("runtime role password secret is empty or invalid")
            values.append((role, password))
        return _ValidatedRolePasswords(tuple(values))

    def rotate_validated_role_passwords(self, secrets: _ValidatedRolePasswords) -> None:
        """Rotate a preflight-captured secret set without reopening secret files."""

        statements = ["SET standard_conforming_strings = on;"]
        for role, password in secrets.values:
            escaped = password.replace("'", "''")
            statements.append(f"ALTER ROLE \"{role}\" PASSWORD '{escaped}';")
        # The SQL containing passwords exists only as child stdin.  It is never
        # included in argv, the environment, output, reports, or exceptions.
        command = (
            self.config.psql_bin,
            "--host",
            self.config.host,
            "--port",
            str(self.config.port),
            "--username",
            self.config.user,
            "--dbname",
            self.config.maintenance_database,
            "--no-password",
            "-X",
            "--set=ON_ERROR_STOP=1",
            "--file=-",
        )
        self._run(command, input_bytes=("\n".join(statements) + "\n").encode("utf-8"))

    def rotate_role_passwords(self, secrets: Sequence[RolePasswordSecret]) -> None:
        """Validate and rotate the four fixed runtime roles in one call."""

        self.rotate_validated_role_passwords(self.validate_role_password_secrets(secrets))

    def database_exists(self, database: str) -> bool:
        value = self.query_scalar(
            self.config.maintenance_database,
            f"SELECT count(*) FROM pg_database WHERE datname={_sql_literal(database)}",  # noqa: S608 - validated and quoted database identifier value
        )
        return value == "1"

    def database_comment(self, database: str) -> str | None:
        statement = (
            "SELECT coalesce(shobj_description(oid, 'pg_database'),'') "  # noqa: S608 - database value is validated and SQL-quoted
            f"FROM pg_database WHERE datname={_sql_literal(database)}"
        )
        value = self.query_scalar(
            self.config.maintenance_database,
            statement,
        )
        return value or None

    def create_restore_database(
        self,
        database: str,
        *,
        owner: Literal["cardrag", "keycloak"],
        owner_comment: str,
    ) -> None:
        quoted = _sql_identifier(database)
        quoted_owner = _sql_identifier(owner)
        self.execute_sql(
            self.config.maintenance_database,
            f"CREATE DATABASE {quoted} WITH TEMPLATE template0 OWNER {quoted_owner};\n"
            f"COMMENT ON DATABASE {quoted} IS {_sql_literal(owner_comment)};\n",
        )

    def drop_database(self, database: str) -> None:
        self.execute_sql(
            self.config.maintenance_database,
            f"DROP DATABASE {_sql_identifier(database)};\n",
        )

    def rename_database(self, source: str, destination: str) -> None:
        self.execute_sql(
            self.config.maintenance_database,
            f"ALTER DATABASE {_sql_identifier(source)} RENAME TO {_sql_identifier(destination)};\n",
        )

    def restore_provenance(self, database: str) -> tuple[str, str] | None:
        present = self.query_scalar(
            database,
            "SELECT (to_regclass('cardrag_maintenance.restore_provenance') IS NOT NULL)::int",
        )
        if present != "1":
            return None
        value = self.query_scalar(
            database,
            "SELECT export_id || ':' || dump_sha256 "
            "FROM cardrag_maintenance.restore_provenance WHERE singleton=true",
        )
        export_id, separator, dump_sha256 = value.partition(":")
        if not separator or not _EXPORT_ID_RE.fullmatch(export_id) or not _SHA256_RE.fullmatch(dump_sha256):
            raise PostgresToolError("restore provenance is invalid")
        return export_id, dump_sha256

    def stamp_restore_provenance(self, database: str, *, export_id: str, dump_sha256: str) -> None:
        if not _EXPORT_ID_RE.fullmatch(export_id) or not _SHA256_RE.fullmatch(dump_sha256):
            raise ValueError("invalid restore provenance")
        self.execute_sql(
            database,
            "CREATE SCHEMA IF NOT EXISTS cardrag_maintenance;\n"
            "CREATE TABLE IF NOT EXISTS cardrag_maintenance.restore_provenance ("
            "singleton boolean PRIMARY KEY CHECK (singleton), export_id text NOT NULL, "
            "dump_sha256 text NOT NULL, restored_at timestamptz NOT NULL DEFAULT now());\n"
            "INSERT INTO cardrag_maintenance.restore_provenance"
            f"(singleton, export_id, dump_sha256) VALUES (true, '{export_id}', '{dump_sha256}') "
            "ON CONFLICT (singleton) DO UPDATE SET "
            "export_id=EXCLUDED.export_id, dump_sha256=EXCLUDED.dump_sha256, "
            "restored_at=now();\n",
        )

    def schema_migrations(self, database: str) -> tuple[tuple[int, str], ...]:
        value = self.query_scalar(
            database,
            "SELECT coalesce(json_agg(json_build_array(version, checksum) ORDER BY version)::text,'[]') "
            "FROM schema_migrations",
        )
        try:
            loaded = json.loads(value)
            result = tuple((int(item[0]), str(item[1])) for item in loaded)
        except (TypeError, ValueError, IndexError, json.JSONDecodeError) as exc:
            raise PostgresToolError("restored schema migration inventory is invalid") from exc
        if any(not _SHA256_RE.fullmatch(checksum) for _, checksum in result):
            raise PostgresToolError("restored schema migration checksum is invalid")
        return result

    def pgvector_version(self, database: str) -> str:
        return self.query_scalar(
            database,
            "SELECT extversion FROM pg_extension WHERE extname='vector'",
        )

    def _run(
        self,
        command: Sequence[str],
        *,
        needs_password: bool = True,
        input_bytes: bytes | None = None,
    ) -> ProcessResult:
        environment = dict(os.environ)
        if needs_password:
            environment["PGPASSWORD"] = self._password()
        result = self._execute(command, env=environment, input_bytes=input_bytes)
        if result.returncode:
            # Client diagnostics can contain host/user/database names, but argv
            # and stderr are deliberately not reproduced because extensions or
            # wrappers may echo their environment.
            raise PostgresToolError(f"PostgreSQL maintenance command failed: {Path(command[0]).name}")
        return result

    def _password(self) -> str:
        password_file = _regular_file(self.config.password_file, "PostgreSQL password secret")
        if stat.S_IMODE(password_file.stat().st_mode) & 0o022:
            raise PostgresToolError("PostgreSQL password secret has unsafe permissions")
        password = password_file.read_text(encoding="utf-8").rstrip("\r\n")
        if not password:
            raise PostgresToolError("PostgreSQL password secret is empty")
        return password


class StateDatabaseInspector(Protocol):
    def quiescence(self, *, database_names: Sequence[str]) -> QuiescenceReport: ...

    def snapshot(self) -> DatabaseStateSnapshot: ...


class PostgresStateInspector:
    """Read-only state and quiescence queries against the CardRAG database."""

    def __init__(self, database: Postgres) -> None:
        self.database = database

    def quiescence(self, *, database_names: Sequence[str]) -> QuiescenceReport:
        with self.database.connection() as connection, connection.cursor() as cursor:
            pipeline_runs = self._nonterminal_count(
                cursor,
                "pipeline_runs",
                "state IN ('queued','running','paused')",
            )
            jobs = self._nonterminal_count(
                cursor,
                "jobs",
                "state IN ('queued','running','retry_wait')",
            )
            legacy_imports = self._nonterminal_count(
                cursor,
                "legacy_imports",
                "state NOT IN ('succeeded','failed','cancelled')",
            )
            cursor.execute(
                """
                SELECT count(*) AS count
                FROM pg_catalog.pg_stat_activity
                WHERE datname = ANY(%s)
                  AND backend_type='client backend'
                  AND pid <> pg_backend_pid()
                """,
                (list(database_names),),
            )
            row = cursor.fetchone()
            other_sessions = int(row["count"] if row is not None else 0)
        values = (pipeline_runs, jobs, legacy_imports, other_sessions)
        return QuiescenceReport(
            pipeline_runs=pipeline_runs,
            jobs=jobs,
            legacy_imports=legacy_imports,
            other_database_sessions=other_sessions,
            status="passed" if not any(values) else "failed",
        )

    @staticmethod
    def _nonterminal_count(cursor: Any, table: str, predicate: str) -> int:
        # Table and predicate are fixed module constants, never user input.
        cursor.execute("SELECT to_regclass(%s) AS relation", (f"public.{table}",))
        relation = cursor.fetchone()
        if relation is None or relation["relation"] is None:
            return 0
        cursor.execute(f"SELECT count(*) AS count FROM {table} WHERE {predicate}")  # noqa: S608
        row = cursor.fetchone()
        return int(row["count"] if row is not None else 0)

    def snapshot(self) -> DatabaseStateSnapshot:
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT a.generation_id, g.manifest_sha256, g.root_key
                FROM active_generation a
                JOIN generations g USING (generation_id)
                WHERE a.singleton=true
                """
            )
            active = cursor.fetchone()
            previous: dict[str, Any] | None = None
            if active is not None:
                cursor.execute(
                    """
                    SELECT generation_id, manifest_sha256
                    FROM generations
                    WHERE state IN ('ready','published','retired') AND generation_id <> %s
                    ORDER BY published_at DESC NULLS LAST, created_at DESC
                    LIMIT 1
                    """,
                    (active["generation_id"],),
                )
                previous = cursor.fetchone()
            cursor.execute(
                """
                SELECT DISTINCT generation_id FROM (
                    SELECT generation_id FROM generation_pins
                    UNION ALL
                    SELECT generation_id FROM generations WHERE pinned
                ) pins ORDER BY generation_id
                """
            )
            pins = tuple(str(row["generation_id"]) for row in cursor.fetchall())
            cursor.execute("SELECT version, checksum FROM schema_migrations ORDER BY version")
            migrations = tuple((int(row["version"]), str(row["checksum"])) for row in cursor.fetchall())
            cursor.execute("SELECT extversion FROM pg_extension WHERE extname='vector'")
            vector_row = cursor.fetchone()
            pgvector_version = str(vector_row["extversion"]) if vector_row else None
            keys: set[str] = set()
            object_columns = (
                ("source_documents", "raw_object_key"),
                ("generation_documents", "raw_object_key"),
                ("generation_documents", "ocr_object_key"),
                ("generation_documents", "structured_object_key"),
                ("generation_artifacts", "manifest_object_key"),
                ("stage_checkpoints", "artifact_uri"),
            )
            for table, column in object_columns:
                cursor.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema='public' AND table_name=%s AND column_name=%s
                    ) AS present
                    """,
                    (table, column),
                )
                present = cursor.fetchone()
                if present is None or not present["present"]:
                    continue
                cursor.execute(
                    f"SELECT DISTINCT {column} AS object_key FROM {table} "  # noqa: S608
                    f"WHERE {column} IS NOT NULL"
                )
                keys.update(str(row["object_key"]) for row in cursor.fetchall())
        return DatabaseStateSnapshot(
            active_generation_id=str(active["generation_id"]) if active else None,
            active_manifest_sha256=str(active["manifest_sha256"]) if active else None,
            active_root_key=str(active["root_key"]) if active else None,
            previous_generation_id=str(previous["generation_id"]) if previous else None,
            previous_manifest_sha256=str(previous["manifest_sha256"]) if previous else None,
            pinned_generation_ids=tuple(sorted(set(pins))),
            schema_migrations=migrations,
            pgvector_version=pgvector_version,
            object_keys=tuple(sorted(keys)),
        )


@dataclass(frozen=True, slots=True)
class ExportRequest:
    archive_root: Path
    object_root: Path
    generation_root: Path
    compatibility: RuntimeCompatibility
    deployment_root: Path
    imports_root: Path | None = None
    include_imports: bool = False
    export_id: str | None = None
    now: datetime | None = None


@dataclass(frozen=True, slots=True)
class RestoreRequest:
    archive_root: Path
    package_path: Path
    object_root: Path
    generation_root: Path
    expected_compatibility: RuntimeCompatibility
    role_password_secrets: tuple[RolePasswordSecret, ...]


@dataclass(frozen=True, slots=True)
class _DatabaseRestoreAction:
    logical_name: Literal["cardrag", "keycloak"]
    target_database: str
    target_state: Literal["absent", "empty", "already_restored"]
    staging_database: str
    staging_state: Literal["absent", "owned"]
    ownership_comment: str
    dump: DatabaseDump


@dataclass(frozen=True, slots=True, repr=False)
class _PostgresRestorePreflight:
    package: Path
    manifest: StateManifest
    actions: tuple[_DatabaseRestoreAction, ...]
    role_passwords: _ValidatedRolePasswords


class PostgresPortableDatabaseRestorer:
    """Restart-safe two-database restore using owned staging databases.

    A staging database is identified by both a deterministic name and a
    database COMMENT bound to the export/dump hashes.  If interrupted during
    pg_restore, a retry may drop and rebuild only that owned staging database.
    Once activated, an in-database provenance row lets a retry recognize the
    exact already-restored target and skip it.  Both staging databases are
    completed before either target is activated, with CardRAG staged first so
    a CardRAG restore failure cannot partially activate Keycloak.
    """

    def __init__(self, tools: PostgresToolRunner) -> None:
        self.tools = tools

    def preflight(
        self,
        *,
        package: Path,
        manifest: StateManifest,
        role_password_secrets: Sequence[RolePasswordSecret],
    ) -> _PostgresRestorePreflight:
        package = _absolute_path(package, "state package")
        role_passwords = self.tools.validate_role_password_secrets(role_password_secrets)
        dumps = {dump.database: dump for dump in manifest.database_dumps}
        actions: list[_DatabaseRestoreAction] = []
        # Classify every target and every deterministic staging name before any
        # CREATE/DROP/ALTER.  A collision in CardRAG must therefore be found
        # before Keycloak can be staged or activated (and vice versa).
        for logical_name in ("cardrag", "keycloak"):
            dump = dumps[logical_name]
            target = self.tools.config.database_name(logical_name)
            if not self.tools.database_exists(target):
                target_state: Literal["absent", "empty", "already_restored"] = "absent"
            else:
                provenance = self.tools.restore_provenance(target)
                if provenance == (manifest.export_id, dump.sha256):
                    target_state = "already_restored"
                elif self.tools.database_is_empty(target):
                    target_state = "empty"
                else:
                    raise PostgresToolError(f"restore target database is not empty: {logical_name}")
            staging = _restore_database_name(target, manifest.export_id)
            ownership = _restore_database_comment(manifest.export_id, dump.sha256)
            if self.tools.database_exists(staging):
                existing_comment = self.tools.database_comment(staging)
                if existing_comment != ownership:
                    raise PostgresToolError("restore staging database is not owned by this export")
                staging_state: Literal["absent", "owned"] = "owned"
            else:
                staging_state = "absent"
            actions.append(
                _DatabaseRestoreAction(
                    logical_name=logical_name,
                    target_database=target,
                    target_state=target_state,
                    staging_database=staging,
                    staging_state=staging_state,
                    ownership_comment=ownership,
                    dump=dump,
                )
            )
        return _PostgresRestorePreflight(
            package=package,
            manifest=manifest,
            actions=tuple(actions),
            role_passwords=role_passwords,
        )

    def execute(self, preflight: object) -> DatabaseStateSnapshot:
        if not isinstance(preflight, _PostgresRestorePreflight):
            raise TypeError("invalid PostgreSQL restore preflight")
        self._require_preflight_unchanged(preflight)

        # Build and validate both staging databases before either target is
        # activated.  A deterministic, comment-owned staging database may be
        # reset on retry; every other collision was rejected by preflight.
        for action in preflight.actions:
            if action.target_state == "already_restored":
                if action.staging_state == "owned":
                    self.tools.drop_database(action.staging_database)
                continue
            if action.staging_state == "owned":
                self.tools.drop_database(action.staging_database)
            self.tools.create_restore_database(
                action.staging_database,
                owner=action.logical_name,
                owner_comment=action.ownership_comment,
            )
            self.tools.restore(
                action.logical_name,
                preflight.package / action.dump.path,
                target_database=action.staging_database,
                require_empty=False,
            )
            if action.logical_name == "cardrag":
                expected_state = preflight.manifest.database_state
                if self.tools.schema_migrations(action.staging_database) != expected_state.schema_migrations:
                    raise PostgresToolError("restored schema migrations differ from state manifest")
                if self.tools.pgvector_version(action.staging_database) != expected_state.pgvector_version:
                    raise PostgresToolError("restored pgvector version differs from state manifest")
            self.tools.stamp_restore_provenance(
                action.staging_database,
                export_id=preflight.manifest.export_id,
                dump_sha256=action.dump.sha256,
            )

        self._require_activation_targets_unchanged(preflight)
        for action in preflight.actions:
            if action.target_state == "already_restored":
                continue
            if action.target_state == "empty":
                self.tools.drop_database(action.target_database)
            # If a previous attempt was interrupted after DROP DATABASE, the
            # target is simply absent and this rename finishes the activation.
            self.tools.rename_database(action.staging_database, action.target_database)

        self.tools.rotate_validated_role_passwords(preflight.role_passwords)
        return preflight.manifest.database_state

    def _require_preflight_unchanged(self, preflight: _PostgresRestorePreflight) -> None:
        """Recheck every classified database before the first SQL mutation."""

        for action in preflight.actions:
            target_exists = self.tools.database_exists(action.target_database)
            if action.target_state == "absent":
                target_matches = not target_exists
            elif not target_exists:
                target_matches = False
            else:
                provenance = self.tools.restore_provenance(action.target_database)
                target_matches = (
                    provenance == (preflight.manifest.export_id, action.dump.sha256)
                    if action.target_state == "already_restored"
                    else provenance != (preflight.manifest.export_id, action.dump.sha256)
                    and self.tools.database_is_empty(action.target_database)
                )
            staging_exists = self.tools.database_exists(action.staging_database)
            staging_matches = (
                not staging_exists
                if action.staging_state == "absent"
                else staging_exists
                and self.tools.database_comment(action.staging_database) == action.ownership_comment
            )
            if not target_matches or not staging_matches:
                raise PostgresToolError("restore database state changed after preflight")

    def _require_activation_targets_unchanged(
        self,
        preflight: _PostgresRestorePreflight,
    ) -> None:
        """Protect empty targets again after long-running staging restores."""

        for action in preflight.actions:
            target_exists = self.tools.database_exists(action.target_database)
            if action.target_state == "absent":
                target_matches = not target_exists
            elif not target_exists:
                target_matches = False
            elif action.target_state == "already_restored":
                target_matches = self.tools.restore_provenance(action.target_database) == (
                    preflight.manifest.export_id,
                    action.dump.sha256,
                )
            else:
                target_matches = self.tools.database_is_empty(action.target_database)
            if not target_matches:
                raise PostgresToolError("restore target database changed before activation")

            if action.target_state == "already_restored":
                if self.tools.database_exists(action.staging_database):
                    raise PostgresToolError("restore staging database remained after preflight cleanup")
                continue
            if (
                not self.tools.database_exists(action.staging_database)
                or self.tools.database_comment(action.staging_database) != action.ownership_comment
                or self.tools.restore_provenance(action.staging_database)
                != (preflight.manifest.export_id, action.dump.sha256)
            ):
                raise PostgresToolError("restore staging database changed before activation")

    def restore(
        self,
        *,
        package: Path,
        manifest: StateManifest,
        role_password_secrets: Sequence[RolePasswordSecret],
    ) -> DatabaseStateSnapshot:
        """Convenience entry point retaining the standalone restorer API."""

        return self.execute(
            self.preflight(
                package=package,
                manifest=manifest,
                role_password_secrets=role_password_secrets,
            )
        )


class PortableStateService:
    """Coordinator used by the owner-only CLI and maintenance container."""

    def __init__(
        self,
        inspector: StateDatabaseInspector | None,
        postgres_tools: PostgresToolRunner,
        *,
        database_restorer: PortableDatabaseRestorer | None = None,
        progress: ProgressCallback | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.inspector = inspector
        self.postgres_tools = postgres_tools
        self.database_restorer = database_restorer or PostgresPortableDatabaseRestorer(postgres_tools)
        self.progress = progress
        self.clock = clock

    def _emit(
        self,
        operation: Literal["export", "restore"],
        operation_id: str,
        phase: str,
        *,
        started_at: float,
        entries: Sequence[StateFile] = (),
    ) -> None:
        if self.progress is None:
            return
        elapsed = max(0.0, self.clock() - started_at)
        total_bytes = sum(item.size_bytes for item in entries)
        self.progress(
            StateProgress(
                operation=operation,
                operation_id=operation_id,
                phase=phase,
                files_completed=len(entries),
                bytes_completed=total_bytes,
                elapsed_seconds=elapsed,
                bytes_per_second=total_bytes / elapsed if elapsed else 0.0,
            )
        )

    def export(self, request: ExportRequest) -> PackageVerification:
        if self.inspector is None:
            raise StateTransferError("state export requires a CardRAG database inspector")
        archive_root = _validated_archive_root(request.archive_root)
        object_root = _validated_source_root(request.object_root, "object store")
        generation_root = _validated_source_root(request.generation_root, "generation store")
        deployment_root = _validated_deployment_root(request.deployment_root)
        imports_root: Path | None = None
        if request.include_imports:
            if request.imports_root is None:
                raise ValueError("imports_root is required for a self-contained export")
            imports_root = _validated_source_root(request.imports_root, "legacy imports")
        _require_disjoint_roots(
            archive_root,
            object_root,
            generation_root,
            imports_root,
            deployment_root,
        )
        _validate_incoming_is_empty(object_root)

        now = (request.now or datetime.now(UTC)).astimezone(UTC)
        export_id = request.export_id or uuid.uuid4().hex[:12]
        if not _EXPORT_ID_RE.fullmatch(export_id):
            raise ValueError("export ID must be 12 lowercase hexadecimal characters")
        final, staging, now = _resolve_export_paths(archive_root, export_id, now)
        started_at = self.clock()
        operation_progress = _OperationProgress(
            "export",
            export_id,
            self.progress,
            self.clock,
            started_at,
        )
        operation_progress.lifecycle("started")

        if final.exists() or final.is_symlink():
            verification = _verify_state_package(
                archive_root,
                final,
                progress=operation_progress,
            )
            if verification.manifest.export_id != export_id:
                raise StateIntegrityError("existing package belongs to another export")
            operation_progress.lifecycle(
                "completed",
                entries=verification.manifest.files,
            )
            return verification
        _prepare_staging(staging, export_id)

        database_names = (
            self.postgres_tools.config.cardrag_database,
            self.postgres_tools.config.keycloak_database,
        )
        first_quiescence = self.inspector.quiescence(database_names=database_names)
        _require_quiescent(first_quiescence)
        first_database_state = self.inspector.snapshot()
        self.postgres_tools.validate()
        self.postgres_tools.validate_export_sources()

        try:
            (staging / "database").mkdir(mode=0o750)
            with _progress_heartbeat(operation_progress, "databases_dumping"):
                self.postgres_tools.dump("cardrag", staging / "database/cardrag.dump")
                self.postgres_tools.dump("keycloak", staging / "database/keycloak.dump")
            self._emit("export", export_id, "databases_dumped", started_at=started_at)

            object_files = _copy_tree(
                object_root,
                staging / "objects",
                package_prefix="objects",
                category="object",
                excluded_top_level={".incoming"},
                validate_objects=True,
                progress=operation_progress,
                progress_phase="objects_copying",
            )
            self._emit("export", export_id, "objects_copied", started_at=started_at, entries=object_files)
            generation_files = _copy_tree(
                generation_root,
                staging / "generations",
                package_prefix="generations",
                category="generation",
                excluded_top_level={".publish.lock"},
                progress=operation_progress,
                progress_phase="generations_copying",
            )
            self._emit(
                "export",
                export_id,
                "generations_copied",
                started_at=started_at,
                entries=object_files + generation_files,
            )
            deployment_files = _copy_tree(
                deployment_root,
                staging / "deployment",
                package_prefix="deployment",
                category="deployment",
                progress=operation_progress,
                progress_phase="deployment_copying",
            )
            import_files: tuple[StateFile, ...] = ()
            legacy_bundle_ids: tuple[str, ...] = ()
            if imports_root is not None:
                legacy_bundle_ids = _validate_legacy_imports_root(imports_root)
                import_files = _copy_tree(
                    imports_root,
                    staging / "imports",
                    package_prefix="imports",
                    category="import",
                    progress=operation_progress,
                    progress_phase="imports_copying",
                )

            reference_report = _reference_check(
                first_database_state,
                object_root=object_root,
                generation_root=generation_root,
                progress=operation_progress,
                progress_phase="source_references_verifying",
            )
            if reference_report.status != "passed":
                raise StateIntegrityError("database and filesystem references do not reconcile")
            _write_canonical_model(
                staging / "reports/reference-check.json",
                reference_report,
            )
            verification_report = ExportVerificationReport(
                source_regular_files=(
                    len(object_files) + len(generation_files) + len(deployment_files) + len(import_files)
                ),
                object_files=len(object_files),
                generation_files=len(generation_files),
                import_files=len(import_files),
                database_dumps=2,
                excluded_paths=(
                    "objects/.incoming",
                    "generations/.publish.lock",
                    "build_workspace",
                    "page_cache",
                    "codex_auth",
                    "secrets",
                ),
            )
            _write_canonical_model(
                staging / "reports/verification.json",
                verification_report,
            )

            second_quiescence = self.inspector.quiescence(database_names=database_names)
            _require_quiescent(second_quiescence)
            second_database_state = self.inspector.snapshot()
            if second_database_state != first_database_state:
                raise StateQuiescenceError("database state changed during export")
            _assert_source_tree_matches_entries(
                object_root,
                object_files,
                package_prefix="objects",
                excluded_top_level={".incoming"},
                progress=operation_progress,
                progress_phase="objects_source_verifying",
            )
            _assert_source_tree_matches_entries(
                generation_root,
                generation_files,
                package_prefix="generations",
                excluded_top_level={".publish.lock"},
                progress=operation_progress,
                progress_phase="generations_source_verifying",
            )
            _assert_source_tree_matches_entries(
                deployment_root,
                deployment_files,
                package_prefix="deployment",
                progress=operation_progress,
                progress_phase="deployment_source_verifying",
            )
            if imports_root is not None:
                _assert_source_tree_matches_entries(
                    imports_root,
                    import_files,
                    package_prefix="imports",
                    progress=operation_progress,
                    progress_phase="imports_source_verifying",
                )

            files = _inventory_package_payload(staging, progress=operation_progress)
            dump_entries: list[DatabaseDump] = []
            file_by_path = {item.path: item for item in files}
            for logical_name, source_name in (
                ("cardrag", self.postgres_tools.config.cardrag_database),
                ("keycloak", self.postgres_tools.config.keycloak_database),
            ):
                entry = file_by_path[f"database/{logical_name}.dump"]
                dump_entries.append(
                    DatabaseDump(
                        database=logical_name,  # type: ignore[arg-type]
                        source_database_name=source_name,
                        path=entry.path,
                        sha256=entry.sha256,
                        size_bytes=entry.size_bytes,
                    )
                )
            object_entries = tuple(item for item in files if item.category == "object")
            object_inventory_sha = _sha256_bytes(
                canonical_json_bytes([(item.path, item.sha256, item.size_bytes) for item in object_entries])
            )
            manifest = StateManifest(
                export_id=export_id,
                created_at=now,
                includes_imports=request.include_imports,
                database_epoch_sha256=first_database_state.epoch_sha256,
                database_state=first_database_state,
                database_dumps=tuple(dump_entries),
                compatibility=request.compatibility,
                runtime_fingerprint_sha256=_sha256_bytes(canonical_json_bytes(request.compatibility)),
                legacy_bundle_ids=legacy_bundle_ids,
                files=files,
                object_count=len(object_entries),
                object_bytes=sum(item.size_bytes for item in object_entries),
                object_inventory_sha256=object_inventory_sha,
            )
            _write_bytes(staging / "state-manifest.json", manifest.canonical_bytes())
            checksum_entries = files + (_file_entry(staging / "state-manifest.json", staging, "report"),)
            checksum_body = _checksum_body(checksum_entries)
            _write_bytes(staging / "checksums.sha256", checksum_body)
            ready = StateReady(
                export_id=export_id,
                state_manifest_sha256=_sha256_file(staging / "state-manifest.json")[0],
                checksums_sha256=_sha256_bytes(checksum_body),
            )
            # READY is intentionally and observably the final file created.
            _write_bytes(staging / "READY", ready.canonical_bytes())
            _fsync_tree(staging)
            _seal_tree(staging)
            # Persist the immutable permission bits as part of the READY-last
            # commit, not only the file contents written before sealing.
            _fsync_tree(staging)
            os.replace(staging, final)
            _fsync_directory(archive_root)
        except BaseException:
            # Leave the marker-owned staging tree for inspection.  A retry with
            # the same export ID resets only that exact, validated staging tree.
            raise
        verification = _verify_state_package(
            archive_root,
            final,
            progress=operation_progress,
        )
        operation_progress.lifecycle(
            "completed",
            entries=verification.manifest.files,
        )
        return verification

    def verify(self, archive_root: Path, package_path: Path) -> PackageVerification:
        return verify_state_package(archive_root, package_path)

    def restore(self, request: RestoreRequest) -> RestoreVerificationReport:
        started_at = self.clock()
        operation_id = _package_operation_id_hint(request.package_path)
        operation_progress = _OperationProgress(
            "restore",
            operation_id,
            self.progress,
            self.clock,
            started_at,
        )
        # Operators get an identifier before the first full-package checksum pass.
        operation_progress.lifecycle("started")
        verification = _verify_state_package(
            request.archive_root,
            request.package_path,
            progress=operation_progress,
        )
        manifest = verification.manifest
        if manifest.export_id != operation_id:
            raise StateIntegrityError("state package identity changed during restore verification")
        _require_compatible_runtime(manifest.compatibility, request.expected_compatibility)
        if manifest.database_state.schema_migrations != current_schema_migrations():
            raise StateIntegrityError("state package schema migrations differ from this release")
        object_root = _absolute_path(request.object_root, "object restore target")
        generation_root = _absolute_path(request.generation_root, "generation restore target")
        _require_disjoint_roots(
            _validated_archive_root(request.archive_root),
            object_root,
            generation_root,
            None,
        )

        object_entries = tuple(item for item in manifest.files if item.category == "object")
        generation_entries = tuple(item for item in manifest.files if item.category == "generation")
        object_target_state = _preflight_restore_target(
            object_root,
            object_entries,
            package_prefix="objects",
            progress=operation_progress,
            progress_phase="existing_objects_verifying",
        )
        generation_target_state = _preflight_restore_target(
            generation_root,
            generation_entries,
            package_prefix="generations",
            progress=operation_progress,
            progress_phase="existing_generations_verifying",
        )

        package = verification.package_path
        # PostgreSQL binaries/server, all four runtime-role secrets, both
        # targets, and both deterministic staging names must pass read-only
        # preflight before creating a filesystem staging tree or changing any
        # runtime permission.
        self.postgres_tools.validate()
        database_preflight = self.database_restorer.preflight(
            package=package,
            manifest=manifest,
            role_password_secrets=request.role_password_secrets,
        )

        object_staging: Path | None = None
        generation_staging: Path | None = None
        try:
            _activate_restore_target_preflight(
                object_root,
                object_entries,
                state=object_target_state,
                package_prefix="objects",
                export_id=manifest.export_id,
            )
            _activate_restore_target_preflight(
                generation_root,
                generation_entries,
                state=generation_target_state,
                package_prefix="generations",
                export_id=manifest.export_id,
            )
            if object_target_state != "exact":
                object_staging = _prepare_restore_staging(
                    object_root,
                    manifest.export_id,
                    package / "objects",
                    object_entries,
                    package_prefix="objects",
                    progress=operation_progress,
                    progress_phase="objects_restoring",
                )
            if generation_target_state != "exact":
                generation_staging = _prepare_restore_staging(
                    generation_root,
                    manifest.export_id,
                    package / "generations",
                    generation_entries,
                    package_prefix="generations",
                    progress=operation_progress,
                    progress_phase="generations_restoring",
                )

            if object_staging is not None:
                _install_restore_staging(object_staging, object_root, manifest.export_id)
                object_staging = None
            if generation_staging is not None:
                _install_restore_staging(generation_staging, generation_root, manifest.export_id)
                generation_staging = None
            database_state = self.database_restorer.execute(database_preflight)
            self._emit(
                "restore",
                manifest.export_id,
                "databases_restored",
                started_at=started_at,
                entries=object_entries + generation_entries,
            )
        except BaseException:
            # Filesystem and database staging remain marker-owned.  The
            # database restorer recognizes an exact already-activated first DB
            # and can resume the other database without overwriting it.
            raise
        report = self._restored_report(
            request,
            manifest,
            database_state,
            progress=operation_progress,
        )
        operation_progress.lifecycle(
            "completed",
            entries=object_entries + generation_entries,
        )
        return report

    def verify_restored(self, request: RestoreRequest) -> RestoreVerificationReport:
        if self.inspector is None:
            raise StateTransferError("restored-state verification requires a CardRAG database inspector")
        started_at = self.clock()
        operation_id = _package_operation_id_hint(request.package_path)
        operation_progress = _OperationProgress(
            "restore",
            operation_id,
            self.progress,
            self.clock,
            started_at,
        )
        operation_progress.lifecycle("verify_restored_started")
        verification = _verify_state_package(
            request.archive_root,
            request.package_path,
            progress=operation_progress,
        )
        manifest = verification.manifest
        if manifest.export_id != operation_id:
            raise StateIntegrityError("state package identity changed during restored verification")
        _require_compatible_runtime(manifest.compatibility, request.expected_compatibility)
        if manifest.database_state.schema_migrations != current_schema_migrations():
            raise StateIntegrityError("state package schema migrations differ from this release")
        object_entries = tuple(item for item in manifest.files if item.category == "object")
        generation_entries = tuple(item for item in manifest.files if item.category == "generation")
        operation_progress.begin_phase(
            "restored_objects_verifying",
            total_files=2 * len(object_entries),
            total_bytes=sum(item.size_bytes for item in object_entries),
        )
        if not _tree_matches_entries(
            request.object_root,
            object_entries,
            package_prefix="objects",
            progress=operation_progress,
        ):
            raise StateIntegrityError("restored object store differs from package")
        operation_progress.finish_phase()
        operation_progress.begin_phase(
            "restored_generations_verifying",
            total_files=2 * len(generation_entries),
            total_bytes=sum(item.size_bytes for item in generation_entries),
        )
        if not _tree_matches_entries(
            request.generation_root,
            generation_entries,
            package_prefix="generations",
            progress=operation_progress,
        ):
            raise StateIntegrityError("restored generation store differs from package")
        operation_progress.finish_phase()
        # Permission drift does not change hashes, so explicitly repair and then
        # validate the runtime write boundary during every restored-state check.
        _apply_runtime_restore_modes(request.object_root, package_prefix="objects")
        _require_runtime_restore_modes(request.object_root, package_prefix="objects")
        _apply_runtime_restore_modes(request.generation_root, package_prefix="generations")
        _require_runtime_restore_modes(request.generation_root, package_prefix="generations")
        database_state = self.inspector.snapshot()
        report = self._restored_report(
            request,
            manifest,
            database_state,
            progress=operation_progress,
        )
        operation_progress.lifecycle(
            "verify_restored_completed",
            entries=object_entries + generation_entries,
        )
        return report

    def _restored_report(
        self,
        request: RestoreRequest,
        manifest: StateManifest,
        database_state: DatabaseStateSnapshot,
        progress: _OperationProgress | None = None,
    ) -> RestoreVerificationReport:
        object_entries = tuple(item for item in manifest.files if item.category == "object")
        generation_entries = tuple(item for item in manifest.files if item.category == "generation")
        matches = database_state == manifest.database_state
        report = RestoreVerificationReport(
            export_id=manifest.export_id,
            database_epoch_sha256=database_state.epoch_sha256,
            object_files=len(object_entries),
            generation_files=len(generation_entries),
            database_state_matches=matches,
            status="passed" if matches else "failed",
        )
        if report.status != "passed":
            raise StateIntegrityError("restored database epoch differs from package")
        reference = _reference_check(
            database_state,
            object_root=_absolute_path(request.object_root, "object restore target"),
            generation_root=_absolute_path(request.generation_root, "generation restore target"),
            progress=progress,
            progress_phase="restored_references_verifying",
        )
        if reference.status != "passed":
            raise StateIntegrityError("restored database and filesystem references do not reconcile")
        return report


def verify_state_package(archive_root: Path, package_path: Path) -> PackageVerification:
    """Verify a sealed package without opening either database."""

    return _verify_state_package(archive_root, package_path)


def verify_state_package_with_progress(
    archive_root: Path,
    package_path: Path,
    progress: Callable[[StateProgress], None],
) -> PackageVerification:
    """Verify a package while emitting bounded, path-free progress events."""

    started_at = time.monotonic()
    operation = _OperationProgress(
        operation="verify",
        operation_id=_package_operation_id_hint(package_path),
        callback=progress,
        clock=time.monotonic,
        started_at=started_at,
    )
    operation.lifecycle("started")
    verification = _verify_state_package(archive_root, package_path, progress=operation)
    operation.lifecycle("completed", entries=verification.manifest.files)
    return verification


def _verify_state_package(
    archive_root: Path,
    package_path: Path,
    *,
    progress: _OperationProgress | None = None,
) -> PackageVerification:
    """Internal verifier with optional path-free, bounded progress reporting."""

    archive = _validated_archive_root(archive_root)
    package = _absolute_path(package_path, "state package")
    if package.is_symlink() or not package.is_dir():
        raise StateIntegrityError("state package must be a regular directory")
    if package.parent.resolve(strict=True) != archive:
        raise StateIntegrityError("state package must be an immediate child of archive root")
    if not _PACKAGE_NAME_RE.fullmatch(package.name):
        raise StateIntegrityError("invalid state package directory name")

    if progress is not None:
        progress.begin_phase(
            "package_scanning",
            total_files=None,
            total_bytes=None,
        )
    regular_files = _scan_regular_files(
        package,
        discovered=(
            (lambda size: progress.advance(files=1, bytes_count=size)) if progress is not None else None
        ),
    )
    if progress is not None:
        progress.set_phase_totals(
            total_files=len(regular_files),
            total_bytes=sum(item.stat().st_size for item in regular_files),
        )
        progress.finish_phase()
    relative_files = {path.relative_to(package).as_posix(): path for path in regular_files}
    required = {"state-manifest.json", "checksums.sha256", "READY"}
    if not required.issubset(relative_files):
        raise StateIntegrityError("state package is incomplete")
    allowed_top_level = {
        "state-manifest.json",
        "database",
        "objects",
        "generations",
        "imports",
        "deployment",
        "reports",
        "checksums.sha256",
        "READY",
    }
    if any(PurePosixPath(name).parts[0] not in allowed_top_level for name in relative_files):
        raise StateIntegrityError("state package contains an unsupported top-level path")

    try:
        manifest = StateManifest.model_validate_json(relative_files["state-manifest.json"].read_bytes())
        ready = StateReady.model_validate_json(relative_files["READY"].read_bytes())
    except Exception as exc:
        raise StateIntegrityError("state package metadata is invalid") from exc
    if relative_files["state-manifest.json"].read_bytes() != manifest.canonical_bytes():
        raise StateIntegrityError("state manifest is not canonical")
    if relative_files["READY"].read_bytes() != ready.canonical_bytes():
        raise StateIntegrityError("READY is not canonical")
    if ready.export_id != manifest.export_id or not package.name.endswith(f"-{manifest.export_id}"):
        raise StateIntegrityError("package/export identity mismatch")
    state_manifest_hash = _sha256_file(relative_files["state-manifest.json"])[0]
    checksums_payload = relative_files["checksums.sha256"].read_bytes()
    if ready.state_manifest_sha256 != state_manifest_hash or ready.checksums_sha256 != _sha256_bytes(
        checksums_payload
    ):
        raise StateIntegrityError("READY does not match package metadata")

    checksum_rows = _parse_checksums(checksums_payload)
    expected_actual_paths = set(checksum_rows) | {"checksums.sha256", "READY"}
    if set(relative_files) != expected_actual_paths:
        raise StateIntegrityError("checksums do not cover the exact package file set")
    checked_bytes = 0
    if progress is not None:
        progress.begin_phase(
            "package_checksums_verifying",
            total_files=len(checksum_rows),
            total_bytes=sum(relative_files[path].stat().st_size for path in checksum_rows),
        )
    for relative, expected_hash in checksum_rows.items():
        actual_hash, size = _sha256_file(
            relative_files[relative],
            progress=((lambda size: progress.advance(bytes_count=size)) if progress is not None else None),
        )
        checked_bytes += size
        if actual_hash != expected_hash:
            raise StateIntegrityError(f"package checksum mismatch: {relative}")
        if progress is not None:
            progress.advance(files=1)
    if progress is not None:
        progress.finish_phase()

    manifest_entries = {item.path: item for item in manifest.files}
    expected_manifest_paths = set(checksum_rows) - {"state-manifest.json"}
    if set(manifest_entries) != expected_manifest_paths:
        raise StateIntegrityError("manifest does not cover the exact payload file set")
    if progress is not None:
        progress.begin_phase(
            "package_manifest_verifying",
            total_files=len(manifest_entries),
            total_bytes=sum(item.size_bytes for item in manifest_entries.values()),
        )
    for path, entry in manifest_entries.items():
        actual = relative_files[path]
        digest, size = _sha256_file(
            actual,
            progress=((lambda size: progress.advance(bytes_count=size)) if progress is not None else None),
        )
        if (digest, size) != (entry.sha256, entry.size_bytes):
            raise StateIntegrityError(f"manifest file metadata mismatch: {path}")
        if progress is not None:
            progress.advance(files=1)
    if progress is not None:
        progress.finish_phase()

    for dump in manifest.database_dumps:
        dump_path = relative_files[dump.path]
        if dump_path.read_bytes()[:5] != b"PGDMP":
            raise StateIntegrityError(f"database dump is not PostgreSQL custom format: {dump.database}")
    object_entries = tuple(item for item in manifest.files if item.category == "object")
    _validate_object_entries(object_entries)
    inventory_hash = _sha256_bytes(
        canonical_json_bytes([(item.path, item.sha256, item.size_bytes) for item in object_entries])
    )
    if inventory_hash != manifest.object_inventory_sha256:
        raise StateIntegrityError("object inventory digest mismatch")

    reference_path = relative_files.get("reports/reference-check.json")
    export_verification_path = relative_files.get("reports/verification.json")
    if reference_path is None or export_verification_path is None:
        raise StateIntegrityError("required state reports are missing")
    try:
        reference = ReferenceCheckReport.model_validate_json(reference_path.read_bytes())
        export_verification = ExportVerificationReport.model_validate_json(
            export_verification_path.read_bytes()
        )
    except Exception as exc:
        raise StateIntegrityError("state report is invalid") from exc
    if reference.status != "passed" or export_verification.status != "passed":
        raise StateIntegrityError("state report did not pass")
    if reference.database_epoch_sha256 != manifest.database_epoch_sha256:
        raise StateIntegrityError("reference report belongs to another database epoch")
    _verify_generation_copy(
        package / "generations",
        manifest.database_state,
        progress=progress,
        progress_phase="package_generation_seals_verifying",
    )
    _validated_deployment_root(package / "deployment")
    if manifest.includes_imports:
        bundle_ids = _validate_legacy_imports_root(package / "imports")
        if bundle_ids != manifest.legacy_bundle_ids:
            raise StateIntegrityError("legacy bundle inventory differs from state manifest")
    elif manifest.legacy_bundle_ids or (package / "imports").exists():
        raise StateIntegrityError("unexpected legacy import payload")

    return PackageVerification(
        package_path=package,
        manifest=manifest,
        checked_files=len(checksum_rows),
        checked_bytes=checked_bytes,
    )


def current_schema_migrations() -> tuple[tuple[int, str], ...]:
    """Return the release's checksum-bound CardRAG schema inventory."""

    migrations: list[tuple[int, str]] = []
    root = resource_files("cardrag.db.migrations")
    for resource in sorted(
        (item for item in root.iterdir() if item.name.endswith(".sql")),
        key=lambda item: item.name,
    ):
        version = int(resource.name.split("_", 1)[0])
        checksum = _sha256_bytes(resource.read_text(encoding="utf-8").encode("utf-8"))
        migrations.append((version, checksum))
    return tuple(migrations)


def _require_compatible_runtime(
    archived: RuntimeCompatibility,
    expected: RuntimeCompatibility,
) -> None:
    """Require the same immutable execution contract, allowing host relocation."""

    if archived != expected:
        raise StateIntegrityError("state package is incompatible with the selected runtime release")


def _validated_deployment_root(path: Path) -> Path:
    root = _validated_source_root(path, "deployment metadata")
    files = _scan_regular_files(root)
    names = {item.relative_to(root).as_posix() for item in files}
    if names != _DEPLOYMENT_FILES:
        raise StateIntegrityError("deployment metadata must contain the exact portable file set")
    loaded_json: dict[str, dict[str, object]] = {}
    for name in ("image-digests.json", "release-manifest.json", "deployment-set.json"):
        try:
            value = json.loads((root / name).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise StateIntegrityError(f"deployment metadata is invalid: {name}") from exc
        if not isinstance(value, dict):
            raise StateIntegrityError(f"deployment metadata must be a JSON object: {name}")
        if _json_contains_unredacted_secret(value):
            raise StateIntegrityError(f"deployment metadata appears to contain a secret: {name}")
        loaded_json[name] = value

    set_manifest = loaded_json["deployment-set.json"]
    expected_hashes = set_manifest.get("files")
    if (
        set_manifest.get("schema_version") != "cardrag-deployment-set.v1"
        or not isinstance(expected_hashes, dict)
        or set(expected_hashes) != _DEPLOYMENT_PAYLOAD_FILES
    ):
        raise StateIntegrityError("deployment metadata set manifest is invalid")
    for name in _DEPLOYMENT_PAYLOAD_FILES:
        expected = expected_hashes.get(name)
        if not isinstance(expected, str) or not _SHA256_RE.fullmatch(expected):
            raise StateIntegrityError("deployment metadata set contains an invalid digest")
        if _sha256_file(root / name)[0] != expected:
            raise StateIntegrityError("deployment metadata files belong to different install sets")

    image_manifest = loaded_json["image-digests.json"]
    images = image_manifest.get("images")
    if (
        image_manifest.get("schema_version") != "cardrag-image-digests.v1"
        or not isinstance(images, dict)
        or set(images) != {"admin", "worker", "mcp"}
        or not all(isinstance(item, str) and _IMAGE_REFERENCE_RE.fullmatch(item) for item in images.values())
    ):
        raise StateIntegrityError("deployment image digest manifest is invalid")
    release = loaded_json["release-manifest.json"]
    roles = release.get("roles")
    release_version = release.get("version")
    release_revision = release.get("git_sha")
    if (
        release.get("schema") != "cardrag.container-release.v3"
        or not isinstance(release_version, str)
        or not release_version
        or not isinstance(release_revision, str)
        or _GIT_SHA_RE.fullmatch(release_revision) is None
        or not isinstance(roles, dict)
        or set(roles) != {"admin", "worker", "mcp"}
    ):
        raise StateIntegrityError("deployment release manifest is invalid")
    for role, reference in images.items():
        part = roles.get(role)
        if not isinstance(part, dict):
            raise StateIntegrityError("deployment release role is invalid")
        expected_reference = f"{part.get('image')}@{part.get('digest')}"
        if (
            part.get("schema") != "cardrag.container-release-part.v3"
            or part.get("role") != role
            or part.get("version") != release_version
            or part.get("git_sha") != release_revision
            or reference != expected_reference
        ):
            raise StateIntegrityError("deployment image digests differ from release evidence")
    try:
        stack = (root / "stack-redacted.yaml").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise StateIntegrityError("redacted Stack metadata is unreadable") from exc
    for line in stack.splitlines():
        assignment = _environment_assignment(line)
        if (
            assignment is not None
            and _SENSITIVE_NAME.search(assignment.group(1))
            and not _secret_scalar_is_redacted(assignment.group(2))
        ):
            raise StateIntegrityError(
                "redacted Stack metadata appears to contain a secret environment value"
            )
        value_match = _YAML_VALUE_ENTRY.match(line)
        if value_match is not None:
            raw_value = value_match.group(1).strip().strip("'\"")
            if _uri_contains_credentials(raw_value):
                raise StateIntegrityError(
                    "redacted Stack metadata appears to contain a credential-bearing URI"
                )
        match = _POSSIBLE_SECRET_ENTRY.match(line)
        if match is None:
            continue
        key = match.group(1).strip().casefold()
        value = match.group(2).strip().strip("'\"")
        safely_redacted = (
            key.endswith("_file")
            or not value
            or _SAFE_ENV_REFERENCE.fullmatch(value) is not None
            or value.casefold() in {"redacted", "<redacted>"}
            or set(value) <= {"*"}
            or value.startswith("/run/secrets/")
        )
        if not safely_redacted:
            raise StateIntegrityError("redacted Stack metadata appears to contain a secret value")
    if not all(str(reference) in stack for reference in images.values()):
        raise StateIntegrityError("redacted Stack images differ from release evidence")
    return root


def deployment_release_contract(path: Path) -> tuple[dict[str, str], str, str]:
    """Return the checksum-bound images, version, and revision for a deployment set."""

    root = _validated_deployment_root(path)
    image_payload = json.loads((root / "image-digests.json").read_text(encoding="utf-8"))
    release = json.loads((root / "release-manifest.json").read_text(encoding="utf-8"))
    return (
        {str(role): str(reference) for role, reference in image_payload["images"].items()},
        str(release["version"]),
        str(release["git_sha"]),
    )


def _json_contains_unredacted_secret(value: object) -> bool:
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key).casefold()
            sensitive = _SENSITIVE_NAME.search(key) is not None
            if sensitive and not key.endswith("_file") and not _secret_scalar_is_redacted(child):
                return True
            if isinstance(child, str) and _uri_contains_credentials(child):
                return True
            if _json_contains_unredacted_secret(child):
                return True
        return False
    if isinstance(value, list):
        return any(_json_contains_unredacted_secret(item) for item in value)
    if isinstance(value, str):
        assignment = _environment_assignment(value)
        if assignment is not None and _SENSITIVE_NAME.search(assignment.group(1)):
            return not _secret_scalar_is_redacted(assignment.group(2))
        return _uri_contains_credentials(value)
    return False


def _environment_assignment(raw: str) -> re.Match[str] | None:
    """Parse plain or quoted Compose list-form ``NAME=value`` safely."""

    scalar = raw.strip()
    if scalar.startswith("-"):
        scalar = scalar[1:].lstrip()
    if scalar[:1] in {"'", '"'}:
        quote = scalar[0]
        closing = scalar.rfind(quote)
        if closing <= 0:
            return None
        trailing = scalar[closing + 1 :].strip()
        if trailing and not trailing.startswith("#"):
            return None
        scalar = scalar[1:closing]
    return _ENV_ASSIGNMENT.fullmatch(scalar)


def _secret_scalar_is_redacted(value: object) -> bool:
    text = str(value).strip().strip("'\"")
    return bool(
        not text
        or text.casefold() in {"redacted", "<redacted>"}
        or set(text) <= {"*"}
        or text.startswith("/run/secrets/")
        or _SAFE_ENV_REFERENCE.fullmatch(text) is not None
    )


def _uri_contains_credentials(value: str) -> bool:
    text = value.strip()
    if "://" not in text:
        return False
    try:
        parsed = urlsplit(text)
    except ValueError:
        return True
    return parsed.username is not None or parsed.password is not None


def _validate_legacy_imports_root(path: Path) -> tuple[str, ...]:
    root = _validated_source_root(path, "legacy imports")
    from cardrag.legacy import verify_bundle

    entries = sorted(root.iterdir(), key=lambda item: item.name)
    if not entries:
        raise StateIntegrityError("self-contained export requires at least one sealed legacy bundle")
    bundle_ids: list[str] = []
    for entry in entries:
        if entry.is_symlink() or not entry.is_dir() or not entry.name.startswith("bundle-"):
            raise StateIntegrityError("legacy imports root may contain only sealed bundle directories")
        try:
            manifest = verify_bundle(entry)
        except Exception as exc:
            raise StateIntegrityError(f"legacy import bundle is invalid: {entry.name}") from exc
        if manifest.bundle_id != entry.name:
            raise StateIntegrityError("legacy bundle directory identity mismatch")
        bundle_ids.append(manifest.bundle_id)
    return tuple(bundle_ids)


def _assert_source_tree_matches_entries(
    root: Path,
    entries: Sequence[StateFile],
    *,
    package_prefix: str,
    excluded_top_level: set[str] | None = None,
    progress: _OperationProgress | None = None,
    progress_phase: str = "source_verifying",
) -> None:
    if progress is not None:
        progress.begin_phase(
            progress_phase,
            total_files=2 * len(entries),
            total_bytes=sum(item.size_bytes for item in entries),
        )
    files = _scan_regular_files(
        root,
        excluded_top_level=excluded_top_level,
        discovered=((lambda _size: progress.advance(files=1)) if progress is not None else None),
    )
    expected: dict[str, StateFile] = {}
    prefix = f"{package_prefix}/"
    for entry in entries:
        if not entry.path.startswith(prefix):
            raise StateIntegrityError("copied source inventory uses the wrong package prefix")
        expected[entry.path.removeprefix(prefix)] = entry
    actual = {item.relative_to(root).as_posix(): item for item in files}
    if set(actual) != set(expected):
        raise StateQuiescenceError("source file inventory changed during export")
    for relative, item in actual.items():
        digest, size = _sha256_file(
            item,
            progress=(
                (lambda block_size: progress.advance(bytes_count=block_size))
                if progress is not None
                else None
            ),
        )
        expected_item = expected[relative]
        if (digest, size) != (expected_item.sha256, expected_item.size_bytes):
            raise StateQuiescenceError("source file changed during export")
        if progress is not None:
            progress.advance(files=1)
    if progress is not None:
        progress.finish_phase()


def _reference_check(
    database_state: DatabaseStateSnapshot,
    *,
    object_root: Path,
    generation_root: Path,
    progress: _OperationProgress | None = None,
    progress_phase: str = "references_verifying",
) -> ReferenceCheckReport:
    invalid: list[str] = []
    missing: list[str] = []
    present = 0
    if progress is not None:
        referenced_bytes = 0
        progress.begin_phase(
            f"{progress_phase}_inventory",
            total_files=None,
            total_bytes=None,
        )
        for key in database_state.object_keys:
            match = _OBJECT_KEY_RE.fullmatch(key)
            if match is None or match.group(1) != match.group(2)[:2]:
                progress.advance(files=1)
                continue
            target = object_root.joinpath(*PurePosixPath(key).parts)
            if not target.is_symlink() and target.is_file():
                size = target.stat().st_size
                referenced_bytes += size
                progress.advance(files=1, bytes_count=size)
            else:
                progress.advance(files=1)
        progress.set_phase_totals(
            total_files=len(database_state.object_keys),
            total_bytes=referenced_bytes,
        )
        progress.finish_phase()
        progress.begin_phase(
            progress_phase,
            total_files=len(database_state.object_keys),
            total_bytes=referenced_bytes,
        )
    for key in database_state.object_keys:
        match = _OBJECT_KEY_RE.fullmatch(key)
        if match is None or match.group(1) != match.group(2)[:2]:
            invalid.append(key)
            if progress is not None:
                progress.advance(files=1)
            continue
        target = object_root.joinpath(*PurePosixPath(key).parts)
        if target.is_symlink() or not target.is_file():
            missing.append(key)
            if progress is not None:
                progress.advance(files=1)
            continue
        digest, _ = _sha256_file(
            target,
            progress=(
                (lambda block_size: progress.advance(bytes_count=block_size))
                if progress is not None
                else None
            ),
        )
        if digest != match.group(2):
            missing.append(key)
            if progress is not None:
                progress.advance(files=1)
            continue
        present += 1
        if progress is not None:
            progress.advance(files=1)

    if progress is not None:
        progress.finish_phase()

    filesystem_id: str | None = None
    filesystem_hash: str | None = None
    try:
        pointer, manifest = _verify_generation_copy(
            generation_root,
            database_state,
            progress=progress,
            progress_phase=f"{progress_phase}_generation_seals",
        )
        if pointer is not None and manifest is not None:
            filesystem_id = pointer.generation_id
            filesystem_hash = manifest.sha256
    except StateIntegrityError:
        # The detailed verifier is still used by the caller after this report;
        # make this report safely fail rather than obscuring it as a success.
        filesystem_id = None
        filesystem_hash = None
    passed = (
        not invalid
        and not missing
        and database_state.active_generation_id == filesystem_id
        and database_state.active_manifest_sha256 == filesystem_hash
        and len(database_state.object_keys) == present
    )
    return ReferenceCheckReport(
        database_epoch_sha256=database_state.epoch_sha256,
        active_generation_id=database_state.active_generation_id,
        active_manifest_sha256=database_state.active_manifest_sha256,
        filesystem_generation_id=filesystem_id,
        filesystem_manifest_sha256=filesystem_hash,
        referenced_object_count=len(database_state.object_keys),
        present_object_count=present,
        missing_object_keys=tuple(sorted(missing)),
        invalid_object_keys=tuple(sorted(invalid)),
        status="passed" if passed else "failed",
    )


def _sql_identifier(value: str) -> str:
    if not _SAFE_DATABASE_NAME_RE.fullmatch(value):
        raise ValueError("invalid PostgreSQL identifier")
    return '"' + value.replace('"', '""') + '"'


def _sql_literal(value: str) -> str:
    if "\x00" in value:
        raise ValueError("invalid PostgreSQL literal")
    return "'" + value.replace("'", "''") + "'"


def _restore_database_name(target: str, export_id: str) -> str:
    if not _SAFE_DATABASE_NAME_RE.fullmatch(target) or not _EXPORT_ID_RE.fullmatch(export_id):
        raise ValueError("invalid restore database identity")
    suffix = f"_restore_{export_id}"
    return f"{target[: 63 - len(suffix)]}{suffix}"


def _restore_database_comment(export_id: str, dump_sha256: str) -> str:
    if not _EXPORT_ID_RE.fullmatch(export_id) or not _SHA256_RE.fullmatch(dump_sha256):
        raise ValueError("invalid restore database ownership")
    return f"{STATE_PACKAGE_SCHEMA}:{export_id}:{dump_sha256}"


def _verify_generation_copy(
    generation_root: Path,
    database_state: DatabaseStateSnapshot,
    *,
    progress: _OperationProgress | None = None,
    progress_phase: str = "generation_seals_verifying",
) -> tuple[CurrentPointer | None, GenerationManifest | None]:
    generation_root = _validated_source_root(generation_root, "generation store")
    manifests = _verify_all_generation_seals(
        generation_root,
        progress=progress,
        progress_phase=progress_phase,
    )
    current_path = generation_root / "current.json"
    if database_state.active_generation_id is None:
        if current_path.exists() or current_path.is_symlink():
            raise StateIntegrityError("filesystem has current generation but database does not")
        return None, None
    if current_path.is_symlink() or not current_path.is_file():
        raise StateIntegrityError("current generation pointer is missing")
    try:
        pointer = CurrentPointer.model_validate_json(current_path.read_bytes())
    except Exception as exc:
        raise StateIntegrityError("current generation pointer is invalid") from exc
    manifest = manifests.get(pointer.generation_id)
    if manifest is None:
        raise StateIntegrityError("active generation directory is missing")
    if pointer.generation_id != database_state.active_generation_id:
        raise StateIntegrityError("database and filesystem active generation differ")
    if manifest.generation_id != pointer.generation_id:
        raise StateIntegrityError("generation manifest identity differs from pointer")
    if pointer.manifest_sha256 != manifest.sha256:
        raise StateIntegrityError("generation pointer manifest checksum mismatch")
    if database_state.active_manifest_sha256 != manifest.sha256:
        raise StateIntegrityError("database generation manifest checksum mismatch")
    expected_root_key = f"generations/{manifest.generation_id}"
    if database_state.active_root_key != expected_root_key:
        raise StateIntegrityError("database generation root_key is not portable or canonical")
    if pointer.previous_generation_id != database_state.previous_generation_id:
        raise StateIntegrityError("filesystem and database previous generation differ")
    if database_state.previous_generation_id is not None:
        previous = manifests.get(database_state.previous_generation_id)
        if previous is None or previous.sha256 != database_state.previous_manifest_sha256:
            raise StateIntegrityError("previous generation manifest differs from database")
    missing_pins = set(database_state.pinned_generation_ids) - set(manifests)
    if missing_pins:
        raise StateIntegrityError("one or more pinned generation directories are missing")
    return pointer, manifest


def _verify_all_generation_seals(
    generation_root: Path,
    *,
    progress: _OperationProgress | None = None,
    progress_phase: str = "generation_seals_verifying",
) -> dict[str, GenerationManifest]:
    generations_root = generation_root / "generations"
    if generations_root.is_symlink() or not generations_root.is_dir():
        raise StateIntegrityError("generation directory root is missing")
    prepared: list[tuple[Path, GenerationManifest]] = []
    for generation_path in sorted(generations_root.iterdir(), key=lambda item: item.name):
        if generation_path.is_symlink() or not generation_path.is_dir():
            raise StateIntegrityError("generation root contains an unsupported entry")
        manifest_path = generation_path / "manifest.json"
        ready_path = generation_path / "READY"
        if manifest_path.is_symlink() or ready_path.is_symlink():
            raise StateIntegrityError("generation seals must not be symlinks")
        try:
            manifest = GenerationManifest.model_validate_json(manifest_path.read_bytes())
            ready = json.loads(ready_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise StateIntegrityError("generation seal is invalid") from exc
        if manifest.generation_id != generation_path.name:
            raise StateIntegrityError("generation directory identity differs from manifest")
        if ready != {"generation_id": manifest.generation_id, "manifest_sha256": manifest.sha256}:
            raise StateIntegrityError("generation READY seal does not match manifest")
        prepared.append((generation_path, manifest))
    if progress is not None:
        progress.begin_phase(
            progress_phase,
            total_files=sum(2 * len(manifest.files) + 2 for _, manifest in prepared),
            total_bytes=sum(entry.size for _, manifest in prepared for entry in manifest.files),
        )
    manifests: dict[str, GenerationManifest] = {}
    for generation_path, manifest in prepared:
        expected_paths = {entry.path for entry in manifest.files} | {"manifest.json", "READY"}
        actual_paths = {
            item.relative_to(generation_path).as_posix()
            for item in _scan_regular_files(
                generation_path,
                discovered=((lambda _size: progress.advance(files=1)) if progress is not None else None),
            )
        }
        if actual_paths != expected_paths:
            raise StateIntegrityError("generation manifest does not cover its exact file set")
        for entry in manifest.files:
            relative = portable_relative_path(entry.path)
            target = generation_path.joinpath(*relative.parts)
            digest, size = _sha256_file(
                target,
                progress=(
                    (lambda block_size: progress.advance(bytes_count=block_size))
                    if progress is not None
                    else None
                ),
            )
            if (digest, size) != (entry.sha256, entry.size):
                raise StateIntegrityError(f"generation file checksum mismatch: {entry.path}")
            if progress is not None:
                progress.advance(files=1)
        manifests[manifest.generation_id] = manifest
    if progress is not None:
        progress.finish_phase()
    return manifests


def _validated_archive_root(path: Path) -> Path:
    root = _absolute_path(path, "archive root")
    if root.is_symlink() or not root.is_dir():
        raise ArchiveSentinelError("archive root must be an existing, non-symlink directory")
    sentinel = root / ARCHIVE_SENTINEL_NAME
    try:
        sentinel = _regular_file(sentinel, "archive sentinel")
        content = sentinel.read_text(encoding="utf-8").rstrip("\r\n")
    except (OSError, UnicodeError, StateIntegrityError) as exc:
        raise ArchiveSentinelError("archive sentinel is missing or unreadable") from exc
    if content != ARCHIVE_SENTINEL_CONTENT:
        raise ArchiveSentinelError("archive sentinel content does not match")
    return root.resolve(strict=True)


def validate_archive_mount_identity(path: Path, expected_source: str) -> Path:
    """Bind a CLI archive operation to the host-verified mount identity record."""

    if not expected_source or "\n" in expected_source or "\r" in expected_source:
        raise ArchiveSentinelError("archive expected source must be one non-empty line")
    root = _validated_archive_root(path)
    source_record = root / ARCHIVE_SOURCE_NAME
    try:
        source_record = _regular_file(source_record, "archive mount identity record")
        if stat.S_IMODE(source_record.stat().st_mode) != 0o440:
            raise ArchiveSentinelError("archive mount identity record must have mode 0440")
        content = source_record.read_bytes()
    except (OSError, StateIntegrityError) as exc:
        raise ArchiveSentinelError("archive mount identity record is missing or unsafe") from exc
    if content != expected_source.encode("utf-8") + b"\n":
        raise ArchiveSentinelError("archive mount identity record does not match")
    return root


def _package_operation_id_hint(package_path: Path) -> str:
    """Return a path-free identifier before package bytes are trusted."""

    name = package_path.name
    if _PACKAGE_NAME_RE.fullmatch(name):
        return name.rsplit("-", 1)[1]
    return uuid.uuid4().hex[:12]


def _resolve_export_paths(
    archive_root: Path,
    export_id: str,
    requested_now: datetime,
) -> tuple[Path, Path, datetime]:
    final_matches = sorted(archive_root.glob(f"cardrag-state-*-{export_id}"))
    staging_matches = sorted(archive_root.glob(f".cardrag-state-*-{export_id}.incoming"))
    if len(final_matches) > 1 or len(staging_matches) > 1 or (final_matches and staging_matches):
        raise StateIntegrityError("archive contains ambiguous state operations for export ID")
    if final_matches:
        final = final_matches[0]
    elif staging_matches:
        staging_name = staging_matches[0].name
        final = archive_root / staging_name.removeprefix(".").removesuffix(".incoming")
    else:
        final = archive_root / (f"cardrag-state-{requested_now.strftime('%Y%m%dT%H%M%SZ')}-{export_id}")
    if not _PACKAGE_NAME_RE.fullmatch(final.name):
        raise StateIntegrityError("state operation path has an invalid package name")
    timestamp = final.name.removeprefix("cardrag-state-").removesuffix(f"-{export_id}")
    try:
        operation_time = datetime.strptime(timestamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise StateIntegrityError("state operation timestamp is invalid") from exc
    staging = archive_root / f".{final.name}.incoming"
    return final, staging, operation_time


def _validated_source_root(path: Path, label: str) -> Path:
    root = _absolute_path(path, label)
    if root.is_symlink() or not root.is_dir():
        raise StateIntegrityError(f"{label} must be an existing, non-symlink directory")
    return root.resolve(strict=True)


def _absolute_path(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise StateIntegrityError(f"{label} must be an explicit absolute path")
    return path


def _regular_file(path: Path, label: str) -> Path:
    if path.is_symlink():
        raise StateIntegrityError(f"{label} must not be a symlink")
    try:
        metadata = path.stat()
    except FileNotFoundError as exc:
        raise StateIntegrityError(f"{label} is missing") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise StateIntegrityError(f"{label} must be a regular file")
    return path


def _require_disjoint_roots(
    archive_root: Path,
    object_root: Path,
    generation_root: Path,
    imports_root: Path | None,
    deployment_root: Path | None = None,
) -> None:
    roots = [
        archive_root.resolve(strict=False),
        object_root.resolve(strict=False),
        generation_root.resolve(strict=False),
    ]
    if imports_root is not None:
        roots.append(imports_root.resolve(strict=False))
    if deployment_root is not None:
        roots.append(deployment_root.resolve(strict=False))
    for index, first in enumerate(roots):
        for second in roots[index + 1 :]:
            if first == second or first.is_relative_to(second) or second.is_relative_to(first):
                raise StateIntegrityError("state source, target, and archive roots must be disjoint")


def _validate_incoming_is_empty(object_root: Path) -> None:
    incoming = object_root / ".incoming"
    if not incoming.exists() and not incoming.is_symlink():
        return
    if incoming.is_symlink() or not incoming.is_dir():
        raise StateIntegrityError("object .incoming must be a regular directory")
    if any(incoming.iterdir()):
        raise StateQuiescenceError("object .incoming is not empty")


def _require_quiescent(report: QuiescenceReport) -> None:
    if report.status != "passed":
        raise StateQuiescenceError(
            "state export requires no active runs, jobs, imports, or other database sessions"
        )


def _prepare_staging(staging: Path, export_id: str) -> None:
    if staging.exists() or staging.is_symlink():
        _remove_marker_owned_staging(staging, export_id)
    staging.mkdir(mode=0o750)
    marker = {
        "schema_version": "cardrag-state-staging.v1",
        "export_id": export_id,
    }
    _write_bytes(staging / STAGING_MARKER_NAME, canonical_json_bytes(marker) + b"\n")


def _remove_marker_owned_staging(staging: Path, export_id: str) -> None:
    if staging.is_symlink() or not staging.is_dir():
        raise StateIntegrityError("state staging path is not a marker-owned directory")
    marker = staging / STAGING_MARKER_NAME
    try:
        payload = json.loads(_regular_file(marker, "state staging marker").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, StateIntegrityError) as exc:
        raise StateIntegrityError("refusing to reset unowned state staging directory") from exc
    if payload != {"schema_version": "cardrag-state-staging.v1", "export_id": export_id}:
        raise StateIntegrityError("state staging marker belongs to another operation")
    for path in _scan_regular_files(staging):
        os.chmod(path, 0o600)
    for path in sorted((item for item in staging.rglob("*") if item.is_dir()), reverse=True):
        if path.is_symlink():
            raise StateIntegrityError("state staging contains a symlink")
        os.chmod(path, 0o700)
    os.chmod(staging, 0o700)
    shutil.rmtree(staging)


def _scan_regular_files(
    root: Path,
    *,
    excluded_top_level: set[str] | None = None,
    discovered: Callable[[int], None] | None = None,
) -> tuple[Path, ...]:
    root = _validated_source_root(root, "file tree")
    excluded = excluded_top_level or set()
    files: list[Path] = []
    for current, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        relative_current = current_path.relative_to(root)
        if relative_current == Path("."):
            directory_names[:] = sorted(name for name in directory_names if name not in excluded)
            file_names = [name for name in file_names if name not in excluded]
        else:
            directory_names.sort()
        for name in tuple(directory_names):
            child = current_path / name
            if child.is_symlink():
                raise StateIntegrityError("source tree contains a symlink")
            metadata = child.stat(follow_symlinks=False)
            if not stat.S_ISDIR(metadata.st_mode):
                raise StateIntegrityError("source tree contains a special directory entry")
        for name in sorted(file_names):
            child = current_path / name
            metadata = child.stat(follow_symlinks=False)
            if child.is_symlink() or not stat.S_ISREG(metadata.st_mode):
                raise StateIntegrityError("source tree contains a symlink or special file")
            files.append(child)
            if discovered is not None:
                discovered(metadata.st_size)
    return tuple(sorted(files, key=lambda item: item.relative_to(root).as_posix()))


def _copy_tree(
    source: Path,
    destination: Path,
    *,
    package_prefix: str,
    category: Literal["object", "generation", "import", "deployment"],
    excluded_top_level: set[str] | None = None,
    validate_objects: bool = False,
    progress: _OperationProgress | None = None,
    progress_phase: str = "files_copying",
) -> tuple[StateFile, ...]:
    if progress is not None:
        progress.begin_phase(
            f"{progress_phase}_inventory",
            total_files=None,
            total_bytes=None,
        )
    source_files = _scan_regular_files(
        source,
        excluded_top_level=excluded_top_level,
        discovered=(
            (lambda size: progress.advance(files=1, bytes_count=size)) if progress is not None else None
        ),
    )
    if progress is not None:
        progress.set_phase_totals(
            total_files=len(source_files),
            total_bytes=sum(item.stat().st_size for item in source_files),
        )
        progress.finish_phase()
    if progress is not None:
        # Each source byte is read for the copy and once more to detect mutation.
        progress.begin_phase(
            progress_phase,
            total_files=len(source_files),
            total_bytes=2 * sum(item.stat().st_size for item in source_files),
        )
    destination.mkdir(mode=0o750, parents=True, exist_ok=False)
    for current, directory_names, _ in os.walk(source, topdown=True, followlinks=False):
        current_path = Path(current)
        if current_path == source:
            directory_names[:] = sorted(
                name for name in directory_names if name not in (excluded_top_level or set())
            )
        else:
            directory_names.sort()
        for name in directory_names:
            source_directory = current_path / name
            relative_directory = source_directory.relative_to(source)
            destination.joinpath(*relative_directory.parts).mkdir(mode=0o750, parents=True, exist_ok=True)
    entries: list[StateFile] = []
    for source_file in source_files:
        relative = source_file.relative_to(source).as_posix()
        if validate_objects:
            match = _OBJECT_KEY_RE.fullmatch(relative)
            if match is None or match.group(1) != match.group(2)[:2]:
                raise StateIntegrityError(f"object store contains a non-CAS file: {relative}")
        target = destination.joinpath(*PurePosixPath(relative).parts)
        target.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        source_hash, source_size = _copy_regular_file(
            source_file,
            target,
            progress=(
                (lambda block_size: progress.advance(bytes_count=block_size))
                if progress is not None
                else None
            ),
        )
        if validate_objects and source_hash != PurePosixPath(relative).name:
            raise StateIntegrityError(f"content-addressed object checksum mismatch: {relative}")
        entries.append(
            StateFile(
                path=f"{package_prefix}/{relative}",
                sha256=source_hash,
                size_bytes=source_size,
                category=category,
            )
        )
        if progress is not None:
            progress.advance(files=1)
    if progress is not None:
        progress.finish_phase()
    return tuple(entries)


def _copy_regular_file(
    source: Path,
    destination: Path,
    *,
    progress: Callable[[int], None] | None = None,
) -> tuple[str, int]:
    source = _regular_file(source, "copy source")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("copy destination already exists")
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as input_stream, destination.open("xb") as output_stream:
        for block in iter(lambda: input_stream.read(_COPY_CHUNK_SIZE), b""):
            output_stream.write(block)
            digest.update(block)
            size += len(block)
            if progress is not None:
                progress(len(block))
        output_stream.flush()
        os.fsync(output_stream.fileno())
    # Detect replacement or mutation while copying.
    after_digest, after_size = _sha256_file(source, progress=progress)
    if (digest.hexdigest(), size) != (after_digest, after_size):
        destination.unlink(missing_ok=True)
        raise StateQuiescenceError("source file changed during export")
    return digest.hexdigest(), size


def _inventory_package_payload(
    staging: Path,
    *,
    progress: _OperationProgress | None = None,
) -> tuple[StateFile, ...]:
    categories: dict[
        str,
        Literal["database", "object", "generation", "import", "deployment", "report"],
    ] = {
        "database": "database",
        "objects": "object",
        "generations": "generation",
        "imports": "import",
        "deployment": "deployment",
        "reports": "report",
    }
    if progress is not None:
        progress.begin_phase(
            "export_payload_scanning",
            total_files=None,
            total_bytes=None,
        )
    file_paths = _scan_regular_files(
        staging,
        discovered=(
            (lambda size: progress.advance(files=1, bytes_count=size)) if progress is not None else None
        ),
    )
    if progress is not None:
        progress.set_phase_totals(
            total_files=len(file_paths),
            total_bytes=sum(item.stat().st_size for item in file_paths),
        )
        progress.finish_phase()
    if progress is not None:
        progress.begin_phase(
            "export_payload_inventorying",
            total_files=len(file_paths),
            total_bytes=sum(item.stat().st_size for item in file_paths),
        )
    entries: list[StateFile] = []
    for file_path in file_paths:
        relative = file_path.relative_to(staging).as_posix()
        top = PurePosixPath(relative).parts[0]
        category = categories.get(top)
        if category is None:
            raise StateIntegrityError(f"unexpected file before state seal: {relative}")
        entries.append(
            _file_entry(
                file_path,
                staging,
                category,
                progress=(
                    (lambda block_size: progress.advance(bytes_count=block_size))
                    if progress is not None
                    else None
                ),
            )
        )
        if progress is not None:
            progress.advance(files=1)
    if progress is not None:
        progress.finish_phase()
    return tuple(sorted(entries, key=lambda item: item.path))


def _file_entry(
    path: Path,
    root: Path,
    category: Literal["database", "object", "generation", "import", "deployment", "report"],
    *,
    progress: Callable[[int], None] | None = None,
) -> StateFile:
    digest, size = _sha256_file(_regular_file(path, "state file"), progress=progress)
    return StateFile(
        path=path.relative_to(root).as_posix(),
        sha256=digest,
        size_bytes=size,
        category=category,
    )


def _validate_object_entries(entries: Sequence[StateFile]) -> None:
    for entry in entries:
        if not entry.path.startswith("objects/"):
            raise StateIntegrityError("object inventory path is outside objects")
        key = entry.path.removeprefix("objects/")
        match = _OBJECT_KEY_RE.fullmatch(key)
        if match is None or match.group(1) != match.group(2)[:2] or match.group(2) != entry.sha256:
            raise StateIntegrityError(f"invalid content-addressed object entry: {entry.path}")


def _checksum_body(entries: Iterable[StateFile]) -> bytes:
    ordered = sorted(entries, key=lambda item: item.path)
    if len({entry.path for entry in ordered}) != len(ordered):
        raise StateIntegrityError("duplicate checksum path")
    return "".join(f"{entry.sha256}  {entry.path}\n" for entry in ordered).encode("utf-8")


def _parse_checksums(payload: bytes) -> dict[str, str]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StateIntegrityError("checksums are not UTF-8") from exc
    if not text.endswith("\n"):
        raise StateIntegrityError("checksums are not canonically newline terminated")
    result: dict[str, str] = {}
    previous = ""
    for line in text.splitlines():
        if not re.fullmatch(r"[0-9a-f]{64}  [^\r\n]+", line):
            raise StateIntegrityError("invalid checksum line")
        digest, relative = line.split("  ", 1)
        portable_relative_path(relative)
        if relative in result or (previous and relative <= previous):
            raise StateIntegrityError("checksums must be sorted and unique")
        result[relative] = digest
        previous = relative
    return result


def _write_canonical_model(path: Path, model: BaseModel) -> None:
    _write_bytes(path, canonical_json_bytes(model) + b"\n")


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise FileExistsError("state package file already exists")
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(
    path: Path,
    *,
    progress: Callable[[int], None] | None = None,
) -> tuple[str, int]:
    path = _regular_file(path, "hash source")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(_COPY_CHUNK_SIZE), b""):
            digest.update(block)
            size += len(block)
            if progress is not None:
                progress(len(block))
    return digest.hexdigest(), size


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_regular_file(path: Path) -> None:
    path = _regular_file(path, "durable state file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise StateIntegrityError("durable state file changed before fsync")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    for path in sorted((item for item in root.rglob("*") if item.is_dir()), reverse=True):
        _fsync_directory(path)
    _fsync_directory(root)


@contextlib.contextmanager
def _progress_heartbeat(
    progress: _OperationProgress,
    phase: str,
) -> Iterator[None]:
    """Keep Portainer logs alive while a PostgreSQL client owns the process."""

    progress.begin_phase(phase, total_files=None, total_bytes=None)
    stopped = threading.Event()

    def heartbeat() -> None:
        while not stopped.wait(_OperationProgress._TIME_INTERVAL_SECONDS):
            progress.advance()

    thread = threading.Thread(target=heartbeat, name="cardrag-state-progress", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=1)
        progress.finish_phase()


def _seal_tree(root: Path) -> None:
    for path in _scan_regular_files(root):
        os.chmod(path, 0o440)
    for path in sorted((item for item in root.rglob("*") if item.is_dir()), reverse=True):
        os.chmod(path, 0o550)  # noqa: S103 - immutable archive directory
    os.chmod(root, 0o550)  # noqa: S103 - immutable archive directory


RestoreTargetState = Literal["missing", "empty", "exact"]


def _preflight_restore_target(
    target: Path,
    entries: Sequence[StateFile],
    *,
    package_prefix: str,
    progress: _OperationProgress | None = None,
    progress_phase: str = "existing_target_verifying",
) -> RestoreTargetState:
    """Classify a restore target without creating or chmod-ing anything."""

    target = _absolute_path(target, "restore target")
    if target.is_symlink():
        raise StateIntegrityError("restore target must not be a symlink")
    if not target.exists():
        parent = target.parent
        if parent.is_symlink() or not parent.is_dir():
            raise StateIntegrityError("restore target parent must be an existing directory")
        return "missing"
    if not target.is_dir():
        raise StateIntegrityError("restore target must be a directory")
    if not any(target.iterdir()):
        return "empty"
    if progress is not None:
        progress.begin_phase(
            progress_phase,
            total_files=2 * len(entries),
            total_bytes=sum(item.size_bytes for item in entries),
        )
    if _tree_matches_entries(
        target,
        entries,
        package_prefix=package_prefix,
        progress=progress,
    ):
        if progress is not None:
            progress.finish_phase()
        return "exact"
    raise StateIntegrityError("restore target must be empty (or an exact completed retry)")


def _activate_restore_target_preflight(
    target: Path,
    entries: Sequence[StateFile],
    *,
    state: RestoreTargetState,
    package_prefix: str,
    export_id: str,
) -> None:
    """Recheck a classified target immediately before its first mutation."""

    if state == "missing":
        if target.exists() or target.is_symlink():
            raise StateIntegrityError("restore target changed after preflight")
        target.mkdir(mode=0o750, parents=False)
        return
    if target.is_symlink() or not target.is_dir():
        raise StateIntegrityError("restore target changed after preflight")
    if state == "empty":
        if any(target.iterdir()):
            raise StateIntegrityError("restore target changed after preflight")
        return
    if not _tree_matches_entries(target, entries, package_prefix=package_prefix):
        raise StateIntegrityError("completed restore target changed after preflight")
    _apply_runtime_restore_modes(target, package_prefix=package_prefix)
    _require_runtime_restore_modes(target, package_prefix=package_prefix)
    _cleanup_completed_restore_staging(target, export_id)


def _prepare_restore_staging(
    target: Path,
    export_id: str,
    package_source: Path,
    entries: Sequence[StateFile],
    *,
    package_prefix: str,
    progress: _OperationProgress | None = None,
    progress_phase: str = "files_restoring",
) -> Path:
    staging = target.parent / f".{target.name}.{export_id}.restore-incoming"
    ownership_marker = _restore_staging_ownership_marker(target, export_id)
    if staging.exists() or staging.is_symlink():
        _remove_restore_marker_owned_staging(staging, ownership_marker, export_id)
    elif ownership_marker.exists() or ownership_marker.is_symlink():
        _validate_restore_staging_ownership_marker(ownership_marker, export_id)
    else:
        marker_payload = {
            "schema_version": "cardrag-state-restore-owner.v1",
            "export_id": export_id,
            "target": target.name,
        }
        _write_bytes(ownership_marker, canonical_json_bytes(marker_payload) + b"\n")
        os.chmod(ownership_marker, 0o600)
        _fsync_regular_file(ownership_marker)
        _fsync_directory(target.parent)
    staging.mkdir(mode=0o750)
    if package_prefix == "generations":
        (staging / "generations").mkdir(mode=0o750, exist_ok=True)
    if progress is not None:
        # One copy pass, one source-stability pass, and one staging checksum pass.
        progress.begin_phase(
            progress_phase,
            total_files=3 * len(entries),
            total_bytes=3 * sum(item.size_bytes for item in entries),
        )
    for entry in entries:
        relative = entry.path.removeprefix(f"{package_prefix}/")
        source = package_source.joinpath(*PurePosixPath(relative).parts)
        destination = staging.joinpath(*PurePosixPath(relative).parts)
        destination.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        digest, size = _copy_regular_file(
            source,
            destination,
            progress=(
                (lambda block_size: progress.advance(bytes_count=block_size))
                if progress is not None
                else None
            ),
        )
        if (digest, size) != (entry.sha256, entry.size_bytes):
            raise StateIntegrityError("restore staging copy differs from package")
        if progress is not None:
            progress.advance(files=1)
    if not _tree_matches_entries(
        staging,
        entries,
        package_prefix=package_prefix,
        progress=progress,
    ):
        raise StateIntegrityError("restore staging verification failed")
    if progress is not None:
        progress.finish_phase()
    _apply_runtime_restore_modes(staging, package_prefix=package_prefix)
    _require_runtime_restore_modes(staging, package_prefix=package_prefix)
    _fsync_tree(staging)
    return staging


def _restore_staging_ownership_marker(target: Path, export_id: str) -> Path:
    return target.parent / f".{target.name}.{export_id}.{RESTORE_STAGING_MARKER_SUFFIX}"


def _validate_restore_staging_ownership_marker(marker: Path, export_id: str) -> None:
    try:
        metadata = marker.stat(follow_symlinks=False)
        payload = json.loads(_regular_file(marker, "restore ownership marker").read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, StateIntegrityError) as exc:
        raise StateIntegrityError("refusing to reset unowned restore staging directory") from exc
    expected_target = marker.name.removeprefix(".").removesuffix(
        f".{export_id}.{RESTORE_STAGING_MARKER_SUFFIX}"
    )
    if payload != {
        "schema_version": "cardrag-state-restore-owner.v1",
        "export_id": export_id,
        "target": expected_target,
    }:
        raise StateIntegrityError("restore ownership marker belongs to another operation")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise StateIntegrityError("restore ownership marker has unsafe permissions")
    if (metadata.st_uid, metadata.st_gid) != (os.geteuid(), os.getegid()):
        raise StateIntegrityError("restore ownership marker has unsafe ownership")


def _remove_restore_marker_owned_staging(
    staging: Path,
    ownership_marker: Path,
    export_id: str,
) -> None:
    _validate_restore_staging_ownership_marker(ownership_marker, export_id)
    if staging.is_symlink() or not staging.is_dir():
        raise StateIntegrityError("restore staging path is not a marker-owned directory")
    for path in _scan_regular_files(staging):
        os.chmod(path, 0o600)
    for path in sorted((item for item in staging.rglob("*") if item.is_dir()), reverse=True):
        if path.is_symlink():
            raise StateIntegrityError("restore staging contains a symlink")
        os.chmod(path, 0o700)
    os.chmod(staging, 0o700)
    shutil.rmtree(staging)
    _fsync_directory(staging.parent)


def _apply_runtime_restore_modes(root: Path, *, package_prefix: str) -> None:
    """Restore the runtime write boundary without making sealed bytes mutable."""

    if package_prefix == "objects":
        for path in _scan_regular_files(root):
            os.chmod(path, 0o444)
        for path in sorted((item for item in root.rglob("*") if item.is_dir()), reverse=True):
            os.chmod(path, 0o750)  # noqa: S103 - runtime CAS directory boundary
        os.chmod(root, 0o750)  # noqa: S103 - runtime CAS root boundary
        return
    if package_prefix != "generations":
        raise StateIntegrityError("unsupported restore permission policy")
    generations = root / "generations"
    if generations.is_symlink() or not generations.is_dir():
        raise StateIntegrityError("restored generation tree is missing")
    allowed_root_files = {"current.json", "publication-history.jsonl"}
    for path in root.iterdir():
        if path == generations:
            continue
        if path.is_symlink() or not path.is_file() or path.name not in allowed_root_files:
            raise StateIntegrityError("restored generation root contains an unsafe entry")
    for generation in generations.iterdir():
        if generation.is_symlink() or not generation.is_dir():
            raise StateIntegrityError("restored generation tree contains an unsafe entry")
        for path in _scan_regular_files(generation):
            os.chmod(path, 0o440)
        for path in sorted((item for item in generation.rglob("*") if item.is_dir()), reverse=True):
            os.chmod(path, 0o550)  # noqa: S103 - sealed generation directory
        os.chmod(generation, 0o550)  # noqa: S103 - sealed generation directory
    for path in _scan_regular_files(root, excluded_top_level={"generations"}):
        os.chmod(path, 0o640)
    os.chmod(generations, 0o750)  # noqa: S103 - writable publication parent
    os.chmod(root, 0o750)  # noqa: S103 - writable generation-store root


def _require_runtime_restore_modes(root: Path, *, package_prefix: str) -> None:
    """Fail closed unless the restored tree has the exact runtime modes."""

    root = _validated_source_root(root, "restored runtime tree")

    def require(path: Path, expected: int) -> None:
        metadata = path.stat(follow_symlinks=False)
        actual = stat.S_IMODE(metadata.st_mode)
        if actual != expected:
            raise StateIntegrityError("restored runtime permission policy was not applied")
        if (metadata.st_uid, metadata.st_gid) != (os.geteuid(), os.getegid()):
            raise StateIntegrityError("restored runtime ownership policy was not applied")

    if package_prefix == "objects":
        require(root, 0o750)
        for path in _scan_regular_files(root):
            require(path, 0o444)
        for path in (item for item in root.rglob("*") if item.is_dir()):
            require(path, 0o750)
        return
    if package_prefix != "generations":
        raise StateIntegrityError("unsupported restore permission policy")
    generations = root / "generations"
    if generations.is_symlink() or not generations.is_dir():
        raise StateIntegrityError("restored generation tree is missing")
    allowed_root_files = {"current.json", "publication-history.jsonl"}
    for path in root.iterdir():
        if path == generations:
            continue
        if path.is_symlink() or not path.is_file() or path.name not in allowed_root_files:
            raise StateIntegrityError("restored generation root contains an unsafe entry")
    require(root, 0o750)
    require(generations, 0o750)
    for generation in generations.iterdir():
        if generation.is_symlink() or not generation.is_dir():
            raise StateIntegrityError("restored generation tree contains an unsafe entry")
        require(generation, 0o550)
        for path in _scan_regular_files(generation):
            require(path, 0o440)
        for path in (item for item in generation.rglob("*") if item.is_dir()):
            require(path, 0o550)
    for path in _scan_regular_files(root, excluded_top_level={"generations"}):
        require(path, 0o640)


def _cleanup_completed_restore_staging(target: Path, export_id: str) -> None:
    staging = target.parent / f".{target.name}.{export_id}.restore-incoming"
    marker = _restore_staging_ownership_marker(target, export_id)
    if staging.exists() or staging.is_symlink():
        _remove_restore_marker_owned_staging(staging, marker, export_id)
    if marker.exists() or marker.is_symlink():
        _validate_restore_staging_ownership_marker(marker, export_id)
        marker.unlink()
        _fsync_directory(target.parent)


def _install_restore_staging(staging: Path, target: Path, export_id: str) -> None:
    ownership_marker = _restore_staging_ownership_marker(target, export_id)
    _validate_restore_staging_ownership_marker(ownership_marker, export_id)
    if target.is_symlink() or not target.is_dir() or any(target.iterdir()):
        raise StateIntegrityError("restore target changed before atomic installation")
    # Linux/POSIX rename atomically replaces an existing empty directory.  Do
    # not remove it first: that would create a crash window where the runtime
    # target is absent even though the marker-owned staging tree is complete.
    os.replace(staging, target)
    _fsync_directory(target.parent)
    ownership_marker.unlink()
    _fsync_directory(target.parent)


def _tree_matches_entries(
    root: Path,
    entries: Sequence[StateFile],
    *,
    package_prefix: str,
    progress: _OperationProgress | None = None,
) -> bool:
    try:
        files = _scan_regular_files(
            _absolute_path(root, "restored tree"),
            discovered=((lambda _size: progress.advance(files=1)) if progress is not None else None),
        )
    except (OSError, StateIntegrityError):
        return False
    actual_relatives = {path.relative_to(root).as_posix(): path for path in files}
    expected: dict[str, StateFile] = {}
    for entry in entries:
        prefix = f"{package_prefix}/"
        if not entry.path.startswith(prefix):
            return False
        expected[entry.path.removeprefix(prefix)] = entry
    if set(actual_relatives) != set(expected):
        return False
    for relative, entry in expected.items():
        digest, size = _sha256_file(
            actual_relatives[relative],
            progress=(
                (lambda block_size: progress.advance(bytes_count=block_size))
                if progress is not None
                else None
            ),
        )
        if (digest, size) != (entry.sha256, entry.size_bytes):
            return False
        if progress is not None:
            progress.advance(files=1)
    return True
