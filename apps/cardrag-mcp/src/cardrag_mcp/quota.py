"""Fail-closed local state and audit storage quota primitives."""

from __future__ import annotations

import os
import shutil
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

MIB = 1024 * 1024
GIB = 1024 * MIB
MAX_SAFE_BYTES = (1 << 63) - 1

DEFAULT_MAX_STATE_BYTES = 64 * GIB
DEFAULT_RESERVED_FREE_SPACE_BYTES = 2 * GIB
DEFAULT_EXHAUSTIVE_AUDIT_MAX_JOBS = 32
DEFAULT_EXHAUSTIVE_AUDIT_MAX_TOTAL_BYTES = 2 * GIB
DEFAULT_EXHAUSTIVE_AUDIT_MAX_ARTIFACT_BYTES = 256 * MIB
DEFAULT_RERANKER_AUDIT_MAX_JOBS = 1024
DEFAULT_RERANKER_AUDIT_MAX_TOTAL_BYTES = 512 * MIB
DEFAULT_RERANKER_AUDIT_MAX_ARTIFACT_BYTES = 8 * MIB


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
_RESERVATIONS: dict[Path, dict[object, tuple[int, int]]] = {}


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


def configure_state_quota(root: Path, policy: StateQuotaPolicy) -> None:
    resolved = _canonical_root(root)
    with _REGISTRY_LOCK:
        _POLICIES[resolved] = policy
        _STATE_LOCKS.setdefault(resolved, threading.RLock())
        _RESERVATIONS.setdefault(resolved, {})


def state_quota_policy(root: Path) -> StateQuotaPolicy:
    resolved = _canonical_root(root)
    with _REGISTRY_LOCK:
        return _POLICIES.get(resolved, StateQuotaPolicy())


def _state_lock(root: Path) -> tuple[Path, threading.RLock]:
    resolved = _canonical_root(root)
    with _REGISTRY_LOCK:
        lock = _STATE_LOCKS.setdefault(resolved, threading.RLock())
    return resolved, lock


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
    """Count logical regular-file bytes without following any symlink."""

    return _directory_usage(_canonical_root(root))


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
    policy = state_quota_policy(root)
    usage = safe_tree_usage(root)
    reserved_logical = 0
    reserved_peak = 0
    for logical_bytes, peak_bytes in _RESERVATIONS.get(root, {}).values():
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

    __slots__ = ("_released", "_root", "_token")

    def __init__(self, root: Path, token: object) -> None:
        self._root = root
        self._token = token
        self._released = False

    def release(self) -> None:
        resolved, lock = _state_lock(self._root)
        with lock:
            if self._released:
                return
            reservations = _RESERVATIONS.get(resolved)
            if reservations is None or reservations.pop(self._token, None) is None:
                raise StorageQuotaError("state quota reservation is unavailable")
            self._released = True

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
    """Reserve quota across an asynchronous write without holding a thread lock."""

    peak = logical_growth_bytes if peak_growth_bytes is None else peak_growth_bytes
    resolved, lock = _state_lock(root)
    with lock:
        _ensure_global_state_growth_locked(
            resolved,
            logical_growth_bytes=logical_growth_bytes,
            peak_growth_bytes=peak,
        )
        token = object()
        _RESERVATIONS.setdefault(resolved, {})[token] = (
            logical_growth_bytes,
            peak,
        )
    return StateQuotaReservation(resolved, token)


def ensure_global_state_growth(
    root: Path,
    logical_growth_bytes: int,
    *,
    peak_growth_bytes: int | None = None,
) -> None:
    peak = logical_growth_bytes if peak_growth_bytes is None else peak_growth_bytes
    resolved, lock = _state_lock(root)
    with lock:
        _ensure_global_state_growth_locked(
            resolved,
            logical_growth_bytes=logical_growth_bytes,
            peak_growth_bytes=peak,
        )


@contextmanager
def state_quota_guard(
    root: Path,
    logical_growth_bytes: int,
    *,
    peak_growth_bytes: int | None = None,
) -> Iterator[Path]:
    """Serialize one in-process quota check and its immediately following write."""

    peak = logical_growth_bytes if peak_growth_bytes is None else peak_growth_bytes
    resolved, lock = _state_lock(root)
    with lock:
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
    "reserve_global_state_growth",
    "safe_subtree_usage",
    "safe_tree_usage",
    "state_quota_guard",
    "validate_byte_limit",
    "validate_count_limit",
]
