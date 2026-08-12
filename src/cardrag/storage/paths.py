"""Portable path validation and same-filesystem atomic writes."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path, PurePosixPath

_SCHEME_OR_DRIVE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")


class UnsafePathError(ValueError):
    """Raised when a portable path can escape or vary by host platform."""


def portable_relative_path(value: str | PurePosixPath) -> PurePosixPath:
    """Validate and normalize a portable, non-empty relative POSIX path."""

    raw = str(value)
    if not raw or raw.startswith(("/", "//")):
        raise UnsafePathError("path must be non-empty and relative")
    if "\\" in raw:
        raise UnsafePathError("portable paths must use forward slashes")
    if _SCHEME_OR_DRIVE.match(raw):
        raise UnsafePathError("URL schemes and drive-qualified paths are not allowed")
    if _CONTROL_CHARACTER.search(raw):
        raise UnsafePathError("control characters are not allowed in paths")

    raw_parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise UnsafePathError("empty, current, and parent path segments are not allowed")
    if any(":" in part or part.endswith((" ", ".")) for part in raw_parts):
        raise UnsafePathError("path contains a non-portable segment")
    return PurePosixPath(*raw_parts)


def _absolute_root(root: str | Path) -> Path:
    root_path = Path(root)
    if not root_path.is_absolute():
        raise UnsafePathError("storage root must be an explicit absolute path")
    return root_path.resolve(strict=False)


def resolve_within_root(root: str | Path, relative: str | PurePosixPath) -> Path:
    """Resolve *relative* beneath *root*, including existing symlink targets."""

    root_resolved = _absolute_root(root)
    portable = portable_relative_path(relative)
    candidate = root_resolved.joinpath(*portable.parts)
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise UnsafePathError("resolved path escapes the configured storage root") from exc
    return resolved


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_bytes(
    target: str | Path,
    payload: bytes | bytearray | memoryview,
    *,
    overwrite: bool = False,
    mode: int = 0o644,
) -> Path:
    """Durably publish bytes using a temporary file in the target directory.

    With ``overwrite=False`` the publish is an atomic create and never replaces an
    existing inode. With ``overwrite=True`` an atomic ``os.replace`` is used.
    """

    target_path = Path(target)
    if not target_path.is_absolute():
        raise UnsafePathError("atomic write target must be absolute")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target_path.parent,
        prefix=f".{target_path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(bytes(payload))
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), mode)
        if overwrite:
            os.replace(temporary, target_path)
        else:
            os.link(temporary, target_path)
            temporary.unlink()
        _fsync_directory(target_path.parent)
    finally:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()
    return target_path


def atomic_write_within_root(
    root: str | Path,
    relative: str | PurePosixPath,
    payload: bytes | bytearray | memoryview,
    *,
    overwrite: bool = False,
    mode: int = 0o644,
) -> Path:
    """Contain and atomically write a portable path under an explicit root."""

    root_resolved = _absolute_root(root)
    root_resolved.mkdir(parents=True, exist_ok=True)
    target = resolve_within_root(root_resolved, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Re-resolve after mkdir so a pre-existing parent symlink cannot be missed.
    target = resolve_within_root(root_resolved, relative)
    return atomic_write_bytes(target, payload, overwrite=overwrite, mode=mode)
