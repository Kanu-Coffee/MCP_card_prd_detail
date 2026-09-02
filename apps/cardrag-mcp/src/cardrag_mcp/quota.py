"""Fail-closed local state and audit storage quota primitives.

Lock order is deliberately one-way: callers may hold the generation lifecycle
lock and a ``GenerationStore`` local lock before entering this module, but a
quota transaction never calls back into either of those locks.  Synchronous
writes hold the cross-process quota lock from their capacity check through the
write.  Asynchronous writes instead publish a durable reservation while holding
the lock and release it before network I/O.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import secrets
import shutil
import stat
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

MIB = 1024 * 1024
GIB = 1024 * MIB
MAX_SAFE_BYTES = (1 << 63) - 1

DEFAULT_MAX_SERVING_DATABASE_BYTES = 32 * GIB
DEFAULT_MAX_GENERATION_DOWNLOAD_BYTES = 64 * GIB
DEFAULT_MAX_STATE_BYTES = 128 * GIB
DEFAULT_RESERVED_FREE_SPACE_BYTES = 2 * GIB
DEFAULT_EXHAUSTIVE_AUDIT_MAX_JOBS = 32
DEFAULT_EXHAUSTIVE_AUDIT_MAX_TOTAL_BYTES = 2 * GIB
DEFAULT_EXHAUSTIVE_AUDIT_MAX_ARTIFACT_BYTES = 256 * MIB
DEFAULT_RERANKER_AUDIT_MAX_JOBS = 1024
DEFAULT_RERANKER_AUDIT_MAX_TOTAL_BYTES = 512 * MIB
DEFAULT_RERANKER_AUDIT_MAX_ARTIFACT_BYTES = 8 * MIB

_QUOTA_PARENT_DIRECTORY = "audit-reports"
_QUOTA_DIRECTORY = ".state-quota"
_QUOTA_LOCK = "state.lock"
_QUOTA_POLICY = "policy.json"
_POLICY_SCHEMA = "cardrag.mcp-state-quota-policy.v1"
_RESERVATION_SCHEMA = "cardrag.mcp-state-quota-reservation.v2"
_RESERVATION_NAME = re.compile(r"^reservation-([0-9a-f]{64})\.json$")
_EXHAUSTIVE_JOB_DIRECTORIES = (
    ("audit-jobs", re.compile(r"^audit-[0-9a-f]{64}$")),
    ("experimental-map-reduce-jobs", re.compile(r"^map-reduce-[0-9a-f]{64}$")),
)
_OWNED_TEMP_NAME = re.compile(r"^\.quota-owned-[A-Za-z0-9_-]{1,128}$")
_MAXIMUM_COORDINATION_ENTRIES = 8192
_MAXIMUM_RESERVATIONS = 4096
_MAXIMUM_COORDINATION_FILE_BYTES = 16 * 1024
_POLICY_FIELDS = (
    "maximum_state_bytes",
    "reserved_free_space_bytes",
    "exhaustive_audit_max_jobs",
    "exhaustive_audit_max_total_bytes",
    "exhaustive_audit_max_artifact_bytes",
    "reranker_audit_max_jobs",
    "reranker_audit_max_total_bytes",
    "reranker_audit_max_artifact_bytes",
)


class StorageQuotaError(RuntimeError):
    """A local write would exceed a configured or physical storage boundary."""


def validate_byte_limit(value: int, *, label: str, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if type(value) is not int or value < minimum or value > MAX_SAFE_BYTES:
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{label} must be a {qualifier} bounded integer")
    return value


def validate_count_limit(value: int, *, label: str) -> int:
    if type(value) is not int or value < 1 or value > MAX_SAFE_BYTES:
        raise ValueError(f"{label} must be a positive bounded integer")
    return value


def checked_add(left: int, right: int, *, label: str) -> int:
    validate_byte_limit(left, label=label, allow_zero=True)
    validate_byte_limit(right, label=label, allow_zero=True)
    if right > MAX_SAFE_BYTES - left:
        raise StorageQuotaError(f"{label} exceeds the supported byte range")
    return left + right


@dataclass(frozen=True, slots=True)
class StateQuotaPolicy:
    maximum_state_bytes: int = DEFAULT_MAX_STATE_BYTES
    reserved_free_space_bytes: int = DEFAULT_RESERVED_FREE_SPACE_BYTES
    exhaustive_audit_max_jobs: int = DEFAULT_EXHAUSTIVE_AUDIT_MAX_JOBS
    exhaustive_audit_max_total_bytes: int = DEFAULT_EXHAUSTIVE_AUDIT_MAX_TOTAL_BYTES
    exhaustive_audit_max_artifact_bytes: int = DEFAULT_EXHAUSTIVE_AUDIT_MAX_ARTIFACT_BYTES
    reranker_audit_max_jobs: int = DEFAULT_RERANKER_AUDIT_MAX_JOBS
    reranker_audit_max_total_bytes: int = DEFAULT_RERANKER_AUDIT_MAX_TOTAL_BYTES
    reranker_audit_max_artifact_bytes: int = DEFAULT_RERANKER_AUDIT_MAX_ARTIFACT_BYTES

    def __post_init__(self) -> None:
        validate_byte_limit(self.maximum_state_bytes, label="maximum state bytes")
        validate_byte_limit(
            self.reserved_free_space_bytes,
            label="reserved free-space bytes",
            allow_zero=True,
        )
        validate_count_limit(
            self.exhaustive_audit_max_jobs,
            label="maximum exhaustive audit jobs",
        )
        validate_byte_limit(
            self.exhaustive_audit_max_total_bytes,
            label="maximum exhaustive audit total bytes",
        )
        validate_byte_limit(
            self.exhaustive_audit_max_artifact_bytes,
            label="maximum exhaustive audit artifact bytes",
        )
        validate_count_limit(
            self.reranker_audit_max_jobs,
            label="maximum reranker audit jobs",
        )
        validate_byte_limit(
            self.reranker_audit_max_total_bytes,
            label="maximum reranker audit total bytes",
        )
        validate_byte_limit(
            self.reranker_audit_max_artifact_bytes,
            label="maximum reranker audit artifact bytes",
        )
        if self.exhaustive_audit_max_artifact_bytes > self.exhaustive_audit_max_total_bytes:
            raise ValueError("exhaustive audit artifact cap exceeds its total quota")
        if self.exhaustive_audit_max_total_bytes > self.maximum_state_bytes:
            raise ValueError("exhaustive audit total quota exceeds the MCP state quota")
        if self.reranker_audit_max_artifact_bytes > self.reranker_audit_max_total_bytes:
            raise ValueError("reranker audit artifact cap exceeds its total quota")
        if self.reranker_audit_max_total_bytes > self.maximum_state_bytes:
            raise ValueError("reranker audit total quota exceeds the MCP state quota")


_REGISTRY_LOCK = threading.Lock()
_POLICIES: dict[Path, StateQuotaPolicy] = {}
_STATE_LOCKS: dict[Path, threading.RLock] = {}
_QUOTA_DEPTH = threading.local()


def _reset_after_fork() -> None:
    # A child must never inherit a mutex or a reentrancy marker owned by a
    # vanished parent thread.
    global _REGISTRY_LOCK, _QUOTA_DEPTH
    _REGISTRY_LOCK = threading.Lock()
    _STATE_LOCKS.clear()
    _POLICIES.clear()
    _QUOTA_DEPTH = threading.local()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)


def _canonical_root(root: Path) -> Path:
    if root.is_symlink():
        raise StorageQuotaError("storage quota root must not be a symlink")
    try:
        resolved = root.resolve(strict=True)
    except OSError:
        raise StorageQuotaError("storage quota root is unavailable") from None
    if not resolved.is_dir():
        raise StorageQuotaError("storage quota root is not a directory")
    return resolved


def _state_lock(root: Path) -> tuple[Path, threading.RLock]:
    resolved = _canonical_root(root)
    with _REGISTRY_LOCK:
        lock = _STATE_LOCKS.setdefault(resolved, threading.RLock())
    return resolved, lock


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _coordination_directory(root: Path) -> Path:
    parent = root / _QUOTA_PARENT_DIRECTORY
    if parent.is_symlink():
        raise StorageQuotaError("state quota coordination parent must not be a symlink")
    try:
        parent.mkdir(mode=0o700, exist_ok=True)
    except OSError as exc:
        raise StorageQuotaError("state quota coordination parent is unavailable") from exc
    try:
        resolved_parent = parent.resolve(strict=True)
    except OSError as exc:
        raise StorageQuotaError("state quota coordination parent is unreadable") from exc
    if not resolved_parent.is_dir() or resolved_parent.parent != root:
        raise StorageQuotaError("state quota coordination parent escaped its root")
    path = resolved_parent / _QUOTA_DIRECTORY
    if path.is_symlink():
        raise StorageQuotaError("state quota coordination directory must not be a symlink")
    try:
        path.mkdir(mode=0o700, exist_ok=True)
    except OSError as exc:
        raise StorageQuotaError("state quota coordination directory is unavailable") from exc
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise StorageQuotaError("state quota coordination directory is unreadable") from exc
    if not resolved.is_dir() or resolved.parent != resolved_parent:
        raise StorageQuotaError("state quota coordination directory escaped its parent")
    return resolved


def _existing_coordination_directory(root: Path) -> Path | None:
    """Return a pre-existing safe coordination directory without creating it."""

    parent = root / _QUOTA_PARENT_DIRECTORY
    if not parent.exists() and not parent.is_symlink():
        return None
    # Policy lookup is intentionally non-mutating for standalone reranker and
    # capture paths.  A hostile parent is rejected when a write is attempted.
    if parent.is_symlink() or not parent.is_dir():
        return None
    try:
        resolved_parent = parent.resolve(strict=True)
    except OSError:
        return None
    if resolved_parent.parent != root:
        return None
    path = resolved_parent / _QUOTA_DIRECTORY
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_dir():
        raise StorageQuotaError("state quota coordination directory is unsafe")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise StorageQuotaError("state quota coordination directory is unreadable") from exc
    if resolved.parent != resolved_parent:
        raise StorageQuotaError("state quota coordination directory escaped its parent")
    return resolved


def _open_lock_file(directory: Path) -> int:
    path = directory / _QUOTA_LOCK
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
        metadata = os.fstat(descriptor)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise StorageQuotaError("state quota lock is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size != 0:
        os.close(descriptor)
        raise StorageQuotaError("state quota lock is unsafe")
    return descriptor


@contextmanager
def _quota_transaction(root: Path) -> Iterator[Path]:
    """Hold the process-local mutex and one cross-process quota flock."""

    resolved, local_lock = _state_lock(root)
    with local_lock:
        depths: dict[Path, int] = getattr(_QUOTA_DEPTH, "values", {})
        if resolved in depths:
            depths[resolved] += 1
            try:
                yield resolved
            finally:
                depths[resolved] -= 1
            return
        directory = _coordination_directory(resolved)
        descriptor = _open_lock_file(directory)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            depths[resolved] = 1
            _QUOTA_DEPTH.values = depths
            try:
                _reconcile_coordination_locked(directory)
                yield resolved
            finally:
                depths.pop(resolved, None)
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _canonical_json(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _strict_json_file(
    path: Path,
    *,
    allowed_links: tuple[int, ...] = (1,),
) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file():
        raise StorageQuotaError("state quota coordination file is unsafe")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink not in allowed_links
            or before.st_size < 1
            or before.st_size > _MAXIMUM_COORDINATION_FILE_BYTES
        ):
            raise StorageQuotaError("state quota coordination file has an invalid size")
        encoded = os.read(descriptor, before.st_size + 1)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise StorageQuotaError("state quota coordination file is unreadable") from exc
    finally:
        if "descriptor" in locals():
            os.close(descriptor)
    if len(encoded) != before.st_size or (
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise StorageQuotaError("state quota coordination file changed while being read")

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        parsed = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeError, ValueError) as exc:
        raise StorageQuotaError("state quota coordination file is not strict JSON") from exc
    if not isinstance(parsed, dict) or encoded != _canonical_json(parsed):
        raise StorageQuotaError("state quota coordination file is not canonical JSON")
    return parsed, encoded


def _owned_atomic_json(directory: Path, target: Path, value: dict[str, Any]) -> None:
    encoded = _canonical_json(value)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".quota-owned-", dir=directory)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fchmod(output.fileno(), 0o400)
            os.fsync(output.fileno())
        try:
            os.link(temporary, target, follow_symlinks=False)
        except FileExistsError:
            raise StorageQuotaError("state quota coordination identity already exists") from None
        _fsync_directory(directory)
    finally:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()
            _fsync_directory(directory)


def _owned_atomic_json_with_lease(
    directory: Path,
    target: Path,
    value: dict[str, Any],
) -> int:
    """Publish JSON while retaining an exclusive flock on its exact inode."""

    encoded = _canonical_json(value)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".quota-owned-", dir=directory)
    temporary = Path(temporary_name)
    linked = False
    returned = False
    try:
        offset = 0
        while offset < len(encoded):
            offset += os.write(descriptor, encoded[offset:])
        os.fchmod(descriptor, 0o400)
        os.fsync(descriptor)
        # The creator owns this lease before the inode becomes discoverable.
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            os.link(temporary, target, follow_symlinks=False)
        except FileExistsError:
            raise StorageQuotaError("state quota coordination identity already exists") from None
        linked = True
        _fsync_directory(directory)
        temporary.unlink()
        _fsync_directory(directory)
        returned = True
        return descriptor
    finally:
        if not returned:
            if not linked and (temporary.exists() or temporary.is_symlink()):
                try:
                    temporary.unlink()
                    _fsync_directory(directory)
                except OSError:
                    # A durable target may already exist.  The normal bounded
                    # reconciliation path validates its shared inode before
                    # removing the surviving owned name.
                    pass
            os.close(descriptor)


def _reconcile_coordination_locked(directory: Path) -> None:
    try:
        entries = sorted(directory.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise StorageQuotaError("state quota coordination directory is unreadable") from exc
    if len(entries) > _MAXIMUM_COORDINATION_ENTRIES:
        raise StorageQuotaError("state quota coordination entry cap is exceeded")
    removed = False
    for path in entries:
        if _OWNED_TEMP_NAME.fullmatch(path.name):
            if path.is_symlink() or not path.is_file():
                raise StorageQuotaError("state quota owned temporary is unsafe")
            metadata = path.stat(follow_symlinks=False)
            if metadata.st_size > _MAXIMUM_COORDINATION_FILE_BYTES:
                raise StorageQuotaError("state quota owned temporary is unsafe")
            if metadata.st_nlink == 2:
                companions = []
                for candidate in entries:
                    if candidate == path or (
                        candidate.name != _QUOTA_POLICY
                        and _RESERVATION_NAME.fullmatch(candidate.name) is None
                    ):
                        continue
                    candidate_metadata = candidate.stat(follow_symlinks=False)
                    if (
                        candidate_metadata.st_dev,
                        candidate_metadata.st_ino,
                    ) == (metadata.st_dev, metadata.st_ino):
                        companions.append(candidate)
                if len(companions) != 1:
                    raise StorageQuotaError("state quota owned temporary link is ambiguous")
                companion = companions[0]
                parsed, _ = _strict_json_file(companion, allowed_links=(2,))
                if companion.name == _QUOTA_POLICY:
                    _parse_policy_payload(parsed)
                else:
                    _parse_reservation_payload(companion, parsed)
            elif metadata.st_nlink != 1:
                raise StorageQuotaError("state quota owned temporary is unsafe")
            path.unlink()
            removed = True
            continue
        if path.name == _QUOTA_LOCK:
            metadata = path.stat(follow_symlinks=False)
            if (
                path.is_symlink()
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_size != 0
            ):
                raise StorageQuotaError("state quota lock is unsafe")
            continue
        if path.name == _QUOTA_POLICY or _RESERVATION_NAME.fullmatch(path.name):
            if path.is_symlink() or not path.is_file():
                raise StorageQuotaError("state quota coordination entry is unsafe")
            continue
        raise StorageQuotaError("state quota coordination directory contains an unknown entry")
    if removed:
        _fsync_directory(directory)


def _policy_payload(policy: StateQuotaPolicy) -> dict[str, Any]:
    values = asdict(policy)
    return {
        "schema_version": _POLICY_SCHEMA,
        "values": [values[field] for field in _POLICY_FIELDS],
    }


def _parse_policy_payload(parsed: dict[str, Any]) -> StateQuotaPolicy:
    if set(parsed) != {"schema_version", "values"}:
        raise StorageQuotaError("state quota policy fields are invalid")
    values = parsed.get("values")
    if (
        parsed.get("schema_version") != _POLICY_SCHEMA
        or not isinstance(values, list)
        or len(values) != len(_POLICY_FIELDS)
    ):
        raise StorageQuotaError("state quota policy schema is invalid")
    try:
        durable = StateQuotaPolicy(**dict(zip(_POLICY_FIELDS, values, strict=True)))
    except (TypeError, ValueError) as exc:
        raise StorageQuotaError("state quota policy is invalid") from exc
    if parsed != _policy_payload(durable):
        raise StorageQuotaError("state quota policy fields are invalid")
    return durable


def _parse_reservation_payload(
    path: Path,
    parsed: dict[str, Any],
) -> tuple[str, int, int]:
    match = _RESERVATION_NAME.fullmatch(path.name)
    if match is None or set(parsed) != {
        "schema_version",
        "token",
        "lease",
        "logical",
        "peak",
    }:
        raise StorageQuotaError("state quota reservation fields are invalid")
    token = parsed["token"]
    if (
        parsed["schema_version"] != _RESERVATION_SCHEMA
        or not isinstance(token, str)
        or token != match.group(1)
        or parsed["lease"] != "exclusive-flock-v1"
    ):
        raise StorageQuotaError("state quota reservation identity is invalid")
    try:
        logical = validate_byte_limit(
            parsed["logical"],
            label="reserved logical state growth",
            allow_zero=True,
        )
        peak = validate_byte_limit(
            parsed["peak"],
            label="reserved peak state growth",
            allow_zero=True,
        )
    except ValueError as exc:
        raise StorageQuotaError("state quota reservation values are invalid") from exc
    if peak < logical:
        raise StorageQuotaError("state quota reservation peak is invalid")
    return token, logical, peak


def _ensure_policy_bootstrap_capacity(root: Path, policy: StateQuotaPolicy, size: int) -> None:
    usage = safe_tree_usage(root)
    if usage > policy.maximum_state_bytes or size > policy.maximum_state_bytes - usage:
        raise StorageQuotaError("MCP state quota cannot contain its durable policy")
    try:
        free_bytes = shutil.disk_usage(root).free
    except OSError:
        raise StorageQuotaError("filesystem free space is unavailable") from None
    peak = checked_add(size, size, label="state quota policy publication growth")
    if peak > free_bytes or policy.reserved_free_space_bytes > free_bytes - peak:
        raise StorageQuotaError("MCP reserved free-space gate rejected its durable policy")


def _durable_policy_locked(
    root: Path,
    requested: StateQuotaPolicy | None = None,
) -> StateQuotaPolicy:
    directory = _coordination_directory(root)
    path = directory / _QUOTA_POLICY
    expected = StateQuotaPolicy() if requested is None else requested
    if not path.exists() and not path.is_symlink():
        payload = _policy_payload(expected)
        _ensure_policy_bootstrap_capacity(root, expected, len(_canonical_json(payload)))
        if any(_RESERVATION_NAME.fullmatch(item.name) for item in directory.iterdir()):
            raise StorageQuotaError("state quota reservations exist without a durable policy")
        _owned_atomic_json(directory, path, payload)
        durable = expected
    else:
        parsed, _ = _strict_json_file(path)
        durable = _parse_policy_payload(parsed)
    if requested is not None and durable != requested:
        raise StorageQuotaError("state quota policy differs from the durable policy")
    with _REGISTRY_LOCK:
        _POLICIES[root] = durable
    return durable


def configure_state_quota(root: Path, policy: StateQuotaPolicy) -> None:
    with _quota_transaction(root) as resolved:
        _durable_policy_locked(resolved, policy)


def state_quota_policy(root: Path) -> StateQuotaPolicy:
    resolved = _canonical_root(root)
    with _REGISTRY_LOCK:
        policy = _POLICIES.get(resolved)
    if policy is not None:
        return policy
    if _existing_coordination_directory(resolved) is None:
        # Merely constructing an optional audit/reranker component must not
        # mutate an otherwise empty capture repository.  The first actual
        # quota-guarded write publishes the default policy atomically.
        return StateQuotaPolicy()
    with _quota_transaction(resolved):
        return _durable_policy_locked(resolved)


def _directory_usage(path: Path) -> int:
    total = 0
    try:
        with os.scandir(path) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name)
    except OSError:
        raise StorageQuotaError("storage quota tree is unreadable") from None
    for entry in entries:
        try:
            metadata = entry.stat(follow_symlinks=False)
        except OSError:
            raise StorageQuotaError("storage quota tree changed during traversal") from None
        mode = metadata.st_mode
        if stat.S_ISLNK(mode):
            raise StorageQuotaError("storage quota tree contains a symlink")
        if stat.S_ISDIR(mode):
            amount = _directory_usage(Path(entry.path))
        elif stat.S_ISREG(mode):
            amount = metadata.st_size
        else:
            raise StorageQuotaError("storage quota tree contains a non-regular entry")
        total = checked_add(total, amount, label="storage quota usage")
    return total


def safe_tree_usage(root: Path) -> int:
    """Count logical application-file bytes without following any symlink."""

    resolved = _canonical_root(root)
    return _directory_usage(resolved)


def safe_subtree_usage(root: Path, subtree: Path) -> int:
    resolved_root = _canonical_root(root)
    if subtree.is_symlink():
        raise StorageQuotaError("storage quota subtree must not be a symlink")
    if not subtree.exists():
        return 0
    try:
        resolved_subtree = subtree.resolve(strict=True)
    except OSError:
        raise StorageQuotaError("storage quota subtree is unavailable") from None
    if not resolved_subtree.is_dir() or not resolved_subtree.is_relative_to(resolved_root):
        raise StorageQuotaError("storage quota subtree escaped its root")
    return _directory_usage(resolved_subtree)


def safe_shared_exhaustive_audit_usage(
    root: Path,
    *,
    prospective_audit_job_id: str | None = None,
    prospective_map_job_id: str | None = None,
) -> tuple[int, int]:
    """Return canonical shared bytes and unique audit/map-reduce job count.

    The exhaustive audit and experimental map-reduce lane consume one sealed
    quota.  A nonterminal map-reduce generation root counts as its job even
    before the first progress ledger is published; a later job directory with
    the same ID is therefore not double-counted.
    """

    resolved = _canonical_root(root)
    audit_ids: set[str] = set()
    map_ids: set[str] = set()
    total = 0
    for directory_name, identity_pattern in _EXHAUSTIVE_JOB_DIRECTORIES:
        directory = resolved / directory_name
        if directory.is_symlink():
            raise StorageQuotaError("shared exhaustive audit subtree is a symlink")
        if not directory.exists():
            continue
        try:
            resolved_directory = directory.resolve(strict=True)
        except OSError:
            raise StorageQuotaError("shared exhaustive audit subtree is unreadable") from None
        if not resolved_directory.is_dir() or resolved_directory.parent != resolved:
            raise StorageQuotaError("shared exhaustive audit subtree escaped its root")
        for entry in sorted(resolved_directory.iterdir(), key=lambda item: item.name):
            if (
                entry.is_symlink()
                or not entry.is_dir()
                or identity_pattern.fullmatch(entry.name) is None
            ):
                raise StorageQuotaError("shared exhaustive audit subtree has an unsafe entry")
            (audit_ids if directory_name == "audit-jobs" else map_ids).add(entry.name)
        total = checked_add(
            total,
            _directory_usage(resolved_directory),
            label="shared exhaustive audit usage",
        )

    generation_roots = resolved / "generation-gc-roots"
    if generation_roots.is_symlink():
        raise StorageQuotaError("map-reduce generation roots must not be a symlink")
    if generation_roots.exists():
        try:
            resolved_roots = generation_roots.resolve(strict=True)
        except OSError:
            raise StorageQuotaError("map-reduce generation roots are unreadable") from None
        if not resolved_roots.is_dir() or resolved_roots.parent != resolved:
            raise StorageQuotaError("map-reduce generation roots escaped state")
        for path in sorted(resolved_roots.iterdir(), key=lambda item: item.name):
            parsed, encoded = _strict_json_file(path)
            if set(parsed) != {"schema_version", "owner_id", "generation_id"}:
                raise StorageQuotaError("map-reduce generation root fields are invalid")
            owner_id = parsed["owner_id"]
            generation_id = parsed["generation_id"]
            if (
                parsed["schema_version"] != "cardrag.mcp-generation-gc-root.v1"
                or not isinstance(owner_id, str)
                or re.fullmatch(r"map-reduce-[0-9a-f]{64}", owner_id) is None
                or not isinstance(generation_id, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", generation_id) is None
                or path.name != f"{owner_id}.json"
                or encoded != _canonical_json(parsed)
            ):
                raise StorageQuotaError("map-reduce generation root identity is invalid")
            map_ids.add(owner_id)
            total = checked_add(
                total,
                len(encoded),
                label="shared exhaustive audit usage",
            )

    provider_coordination = resolved / "experimental-map-reduce-coordination"
    total = checked_add(
        total,
        safe_subtree_usage(resolved, provider_coordination),
        label="shared exhaustive audit usage",
    )

    if prospective_audit_job_id is not None:
        if re.fullmatch(r"audit-[0-9a-f]{64}", prospective_audit_job_id) is None:
            raise StorageQuotaError("prospective exhaustive audit job ID is invalid")
        audit_ids.add(prospective_audit_job_id)
    if prospective_map_job_id is not None:
        if re.fullmatch(r"map-reduce-[0-9a-f]{64}", prospective_map_job_id) is None:
            raise StorageQuotaError("prospective map-reduce job ID is invalid")
        map_ids.add(prospective_map_job_id)
    return total, len(audit_ids) + len(map_ids)


def _durable_reservations_locked(root: Path) -> dict[str, tuple[int, int]]:
    directory = _coordination_directory(root)
    reservations: dict[str, tuple[int, int]] = {}
    entries = sorted(directory.iterdir(), key=lambda item: item.name)
    if len(entries) > _MAXIMUM_COORDINATION_ENTRIES:
        raise StorageQuotaError("state quota coordination entry cap is exceeded")
    for path in entries:
        match = _RESERVATION_NAME.fullmatch(path.name)
        if match is None:
            continue
        if len(reservations) >= _MAXIMUM_RESERVATIONS:
            raise StorageQuotaError("state quota reservation count is exhausted")
        parsed, _ = _strict_json_file(path)
        token, logical, peak = _parse_reservation_payload(path, parsed)
        reservations[token] = (logical, peak)
    return reservations


def reconcile_abandoned_state_reservations(root: Path) -> tuple[str, ...]:
    """Explicitly remove only reservation records whose creator lease is gone.

    This is intentionally never called by ordinary request or startup paths.
    A live writer holds an exclusive flock on the exact published inode for the
    entire reservation lifetime; reconciliation uses a non-blocking exclusive
    flock and leaves every live record untouched.  Bytes already materialized
    in the state tree remain charged by :func:`safe_tree_usage`.
    """

    removed: list[str] = []
    with _quota_transaction(root) as resolved:
        _durable_policy_locked(resolved)
        reservations = _durable_reservations_locked(resolved)
        directory = _coordination_directory(resolved)
        for token in sorted(reservations):
            path = directory / f"reservation-{token}.json"
            flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0)
            )
            descriptor: int | None = None
            try:
                descriptor = os.open(path, flags)
                metadata = os.fstat(descriptor)
            except OSError as exc:
                if descriptor is not None:
                    os.close(descriptor)
                raise StorageQuotaError("state quota reservation lease is unreadable") from exc
            if descriptor is None:  # pragma: no cover - os.open either returns or raises
                raise StorageQuotaError("state quota reservation lease is unavailable")
            lease_acquired = False
            try:
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or metadata.st_size < 1
                    or metadata.st_size > _MAXIMUM_COORDINATION_FILE_BYTES
                ):
                    raise StorageQuotaError("state quota reservation lease is unsafe")
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    if exc.errno in {errno.EACCES, errno.EAGAIN}:
                        continue
                    raise StorageQuotaError("state quota reservation lease check failed") from exc
                lease_acquired = True
                current = path.stat(follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
                    raise StorageQuotaError("state quota reservation changed during reconciliation")
                parsed, _ = _strict_json_file(path)
                parsed_token, _, _ = _parse_reservation_payload(path, parsed)
                if parsed_token != token:
                    raise StorageQuotaError("state quota reservation identity is stale")
                # Validate that every materialized partial/complete byte is
                # representable by the ordinary fail-closed tree accounting
                # before removing the conservative future-growth charge.
                safe_tree_usage(resolved)
                path.unlink()
                removed.append(token)
            except OSError as exc:
                raise StorageQuotaError("state quota reservation could not be reconciled") from exc
            finally:
                try:
                    if lease_acquired:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
        if removed:
            _fsync_directory(directory)
    return tuple(removed)


def _ensure_global_state_growth_locked(
    root: Path,
    *,
    logical_growth_bytes: int,
    peak_growth_bytes: int,
) -> None:
    logical_growth = validate_byte_limit(
        logical_growth_bytes,
        label="logical state growth",
        allow_zero=True,
    )
    peak_growth = validate_byte_limit(
        peak_growth_bytes,
        label="peak state growth",
        allow_zero=True,
    )
    if peak_growth < logical_growth:
        raise ValueError("peak state growth cannot be smaller than logical growth")
    policy = _durable_policy_locked(root)
    usage = safe_tree_usage(root)
    reserved_logical = 0
    reserved_peak = 0
    for logical_bytes, peak_bytes in _durable_reservations_locked(root).values():
        reserved_logical = checked_add(
            reserved_logical,
            logical_bytes,
            label="reserved logical state growth",
        )
        reserved_peak = checked_add(
            reserved_peak,
            peak_bytes,
            label="reserved peak state growth",
        )
    accounted_usage = checked_add(usage, reserved_logical, label="accounted state usage")
    if (
        accounted_usage > policy.maximum_state_bytes
        or logical_growth > policy.maximum_state_bytes - accounted_usage
    ):
        raise StorageQuotaError("MCP state quota has insufficient capacity for this write")
    try:
        free_bytes = shutil.disk_usage(root).free
    except OSError:
        raise StorageQuotaError("filesystem free space is unavailable") from None
    required_peak = checked_add(
        reserved_peak,
        peak_growth,
        label="reserved filesystem growth",
    )
    if required_peak > free_bytes or policy.reserved_free_space_bytes > free_bytes - required_peak:
        raise StorageQuotaError("MCP reserved free-space gate rejected this write")


class StateQuotaReservation:
    """Account one asynchronous write until its bytes are committed or removed."""

    __slots__ = ("_descriptor", "_pid", "_released", "_root", "_token")

    def __init__(self, root: Path, token: str, descriptor: int) -> None:
        self._root = root
        self._token = token
        self._descriptor = descriptor
        self._pid = os.getpid()
        self._released = False

    @property
    def token(self) -> str:
        """Return the durable reservation identity for diagnostics and tests."""

        return self._token

    def release(self) -> None:
        if self._released:
            return
        if self._pid != os.getpid():
            raise StorageQuotaError("state quota reservation cannot be released after fork")
        descriptor_to_close: int | None = None
        with _quota_transaction(self._root) as resolved:
            if self._released:
                return
            path = _coordination_directory(resolved) / f"reservation-{self._token}.json"
            reservations = _durable_reservations_locked(resolved)
            if self._token not in reservations or not path.exists():
                raise StorageQuotaError("state quota reservation is unavailable")
            try:
                lease_metadata = os.fstat(self._descriptor)
                path_metadata = path.stat(follow_symlinks=False)
            except OSError as exc:
                raise StorageQuotaError("state quota reservation lease is unavailable") from exc
            if (
                not stat.S_ISREG(lease_metadata.st_mode)
                or lease_metadata.st_nlink != 1
                or (lease_metadata.st_dev, lease_metadata.st_ino)
                != (path_metadata.st_dev, path_metadata.st_ino)
            ):
                raise StorageQuotaError("state quota reservation lease identity is stale")
            try:
                path.unlink()
                _fsync_directory(path.parent)
            except OSError as exc:
                raise StorageQuotaError("state quota reservation could not be released") from exc
            self._released = True
            descriptor_to_close = self._descriptor
            self._descriptor = -1
        if descriptor_to_close is not None:
            fcntl.flock(descriptor_to_close, fcntl.LOCK_UN)
            os.close(descriptor_to_close)

    def __enter__(self) -> StateQuotaReservation:
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


def reserve_global_state_growth(
    root: Path,
    logical_growth_bytes: int,
    *,
    peak_growth_bytes: int | None = None,
) -> StateQuotaReservation:
    """Publish a durable reservation without holding a lock during async I/O."""

    peak = logical_growth_bytes if peak_growth_bytes is None else peak_growth_bytes
    with _quota_transaction(root) as resolved:
        logical = validate_byte_limit(
            logical_growth_bytes,
            label="logical state growth",
            allow_zero=True,
        )
        peak_value = validate_byte_limit(
            peak,
            label="peak state growth",
            allow_zero=True,
        )
        if peak_value < logical:
            raise ValueError("peak state growth cannot be smaller than logical growth")
        reservations = _durable_reservations_locked(resolved)
        if len(reservations) >= _MAXIMUM_RESERVATIONS:
            raise StorageQuotaError("state quota reservation count is exhausted")
        while True:
            token = secrets.token_hex(32)
            if token not in reservations:
                break
        value = {
            "schema_version": _RESERVATION_SCHEMA,
            "token": token,
            "lease": "exclusive-flock-v1",
            "logical": logical,
            "peak": peak_value,
        }
        record_bytes = len(_canonical_json(value))
        _ensure_global_state_growth_locked(
            resolved,
            logical_growth_bytes=checked_add(
                logical,
                record_bytes,
                label="reserved logical state and receipt growth",
            ),
            peak_growth_bytes=checked_add(
                peak_value,
                checked_add(
                    record_bytes,
                    record_bytes,
                    label="reservation publication peak growth",
                ),
                label="reserved filesystem and receipt growth",
            ),
        )
        directory = _coordination_directory(resolved)
        descriptor = _owned_atomic_json_with_lease(
            directory,
            directory / f"reservation-{token}.json",
            value,
        )
    return StateQuotaReservation(resolved, token, descriptor)


def ensure_global_state_growth(
    root: Path,
    logical_growth_bytes: int,
    *,
    peak_growth_bytes: int | None = None,
) -> None:
    peak = logical_growth_bytes if peak_growth_bytes is None else peak_growth_bytes
    with _quota_transaction(root) as resolved:
        _ensure_global_state_growth_locked(
            resolved,
            logical_growth_bytes=logical_growth_bytes,
            peak_growth_bytes=peak,
        )


@contextmanager
def state_quota_transaction(root: Path) -> Iterator[Path]:
    """Serialize a bounded state mutation that cannot increase logical bytes."""

    with _quota_transaction(root) as resolved:
        _durable_policy_locked(resolved)
        _durable_reservations_locked(resolved)
        yield resolved


@contextmanager
def state_quota_guard(
    root: Path,
    logical_growth_bytes: int,
    *,
    peak_growth_bytes: int | None = None,
) -> Iterator[Path]:
    """Serialize a quota check and write across threads and processes."""

    peak = logical_growth_bytes if peak_growth_bytes is None else peak_growth_bytes
    with _quota_transaction(root) as resolved:
        _ensure_global_state_growth_locked(
            resolved,
            logical_growth_bytes=logical_growth_bytes,
            peak_growth_bytes=peak,
        )
        yield resolved


__all__ = [
    "DEFAULT_EXHAUSTIVE_AUDIT_MAX_ARTIFACT_BYTES",
    "DEFAULT_EXHAUSTIVE_AUDIT_MAX_JOBS",
    "DEFAULT_EXHAUSTIVE_AUDIT_MAX_TOTAL_BYTES",
    "DEFAULT_MAX_GENERATION_DOWNLOAD_BYTES",
    "DEFAULT_MAX_SERVING_DATABASE_BYTES",
    "DEFAULT_MAX_STATE_BYTES",
    "DEFAULT_RERANKER_AUDIT_MAX_ARTIFACT_BYTES",
    "DEFAULT_RERANKER_AUDIT_MAX_JOBS",
    "DEFAULT_RERANKER_AUDIT_MAX_TOTAL_BYTES",
    "DEFAULT_RESERVED_FREE_SPACE_BYTES",
    "MAX_SAFE_BYTES",
    "StateQuotaPolicy",
    "StateQuotaReservation",
    "StorageQuotaError",
    "checked_add",
    "configure_state_quota",
    "ensure_global_state_growth",
    "reconcile_abandoned_state_reservations",
    "reserve_global_state_growth",
    "safe_shared_exhaustive_audit_usage",
    "safe_subtree_usage",
    "safe_tree_usage",
    "state_quota_guard",
    "state_quota_transaction",
    "validate_byte_limit",
    "validate_count_limit",
]
