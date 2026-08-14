"""Fail-closed filesystem capacity admission for operator mutations."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path


class StorageAdmissionError(RuntimeError):
    """A target filesystem does not have the configured safe reserve."""


@dataclass(frozen=True)
class StorageThresholds:
    minimum_free_bytes: int = 50 * 1024**3
    minimum_free_percent: int = 20
    warning_used_percent: int = 70
    maximum_used_percent: int = 85

    @classmethod
    def from_environment(cls) -> StorageThresholds:
        values = {
            "minimum_free_bytes": _integer_environment("CARDRAG_MINIMUM_FREE_GIB", 50)
            * 1024**3,
            "minimum_free_percent": _integer_environment(
                "CARDRAG_MINIMUM_FREE_PERCENT", 20
            ),
            "warning_used_percent": _integer_environment(
                "CARDRAG_WARNING_USED_PERCENT", 70
            ),
            "maximum_used_percent": _integer_environment(
                "CARDRAG_MAXIMUM_USED_PERCENT", 85
            ),
        }
        thresholds = cls(**values)
        if not 0 <= thresholds.minimum_free_percent <= 100:
            raise StorageAdmissionError("minimum free percent must be between 0 and 100")
        if not 0 <= thresholds.warning_used_percent < thresholds.maximum_used_percent <= 100:
            raise StorageAdmissionError("storage warning/blocking thresholds are invalid")
        return thresholds


@dataclass(frozen=True)
class FilesystemAdmission:
    device: int
    free_bytes: int
    free_percent: int
    inode_free_percent: int
    used_percent: int


def enforce_storage_admission(
    paths: Iterable[Path],
    *,
    phase: str,
    thresholds: StorageThresholds | None = None,
    warning: Callable[[str], None] | None = None,
) -> tuple[FilesystemAdmission, ...]:
    """Check each distinct target filesystem without logging absolute paths."""

    policy = thresholds or StorageThresholds.from_environment()
    results: list[FilesystemAdmission] = []
    seen_devices: set[int] = set()
    for supplied in paths:
        path = _existing_directory(supplied)
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise StorageAdmissionError("storage target must be an existing non-symlink directory")
        if metadata.st_dev in seen_devices:
            continue
        seen_devices.add(metadata.st_dev)
        usage = os.statvfs(path)
        total_bytes = usage.f_blocks * usage.f_frsize
        free_bytes = usage.f_bavail * usage.f_frsize
        if total_bytes <= 0 or usage.f_files <= 0:
            raise StorageAdmissionError("storage capacity cannot be measured")
        free_percent = free_bytes * 100 // total_bytes
        used_percent = 100 - free_percent
        inode_free_percent = usage.f_favail * 100 // usage.f_files
        result = FilesystemAdmission(
            device=metadata.st_dev,
            free_bytes=free_bytes,
            free_percent=free_percent,
            inode_free_percent=inode_free_percent,
            used_percent=used_percent,
        )
        preflight_blocked = (
            free_bytes < policy.minimum_free_bytes
            or used_percent >= policy.maximum_used_percent
        )
        reserve_blocked = (
            free_percent < policy.minimum_free_percent
            or inode_free_percent < policy.minimum_free_percent
        )
        if reserve_blocked or (phase != "postflight" and preflight_blocked):
            raise StorageAdmissionError(
                f"storage {phase} rejected: free={free_percent}% "
                f"inode_free={inode_free_percent}% used={used_percent}%"
            )
        if used_percent >= policy.warning_used_percent and warning is not None:
            warning(
                f"storage warning: phase={phase} used={used_percent}% "
                f"inode_free={inode_free_percent}%"
            )
        results.append(result)
    if not results:
        raise StorageAdmissionError("at least one storage target is required")
    return tuple(results)


def _existing_directory(path: Path) -> Path:
    candidate = path
    while not candidate.exists():
        if candidate.parent == candidate:
            raise StorageAdmissionError("storage target parent does not exist")
        candidate = candidate.parent
    return candidate


def _integer_environment(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise StorageAdmissionError(f"{name} must be a non-negative integer") from exc
    if value < 0:
        raise StorageAdmissionError(f"{name} must be a non-negative integer")
    return value
