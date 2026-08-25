"""Portable, traversal-safe paths for the WebDAV artifact namespace."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9.][A-Za-z0-9._-]{0,254}$")

STABLE_POINTER_PATH = PurePosixPath("v1", "channels", "stable.json")


def validate_sha256(value: str) -> str:
    if not _SHA256.fullmatch(value):
        raise ValueError("SHA-256 values must be 64 lowercase hexadecimal characters")
    return value


def validate_identifier(value: str, *, label: str = "identifier") -> str:
    if not _SAFE_ID.fullmatch(value) or value in {".", ".."}:
        raise ValueError(f"{label} is not a safe portable identifier")
    return value


def validate_relative_path(value: str | PurePosixPath) -> PurePosixPath:
    """Return a normalized relative path, rejecting encoded or literal traversal."""

    raw = value.as_posix() if isinstance(value, PurePosixPath) else value
    if not raw or raw.startswith("/") or "\\" in raw or "%" in raw or "\x00" in raw:
        raise ValueError("artifact paths must be non-empty, unencoded POSIX-relative paths")
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise ValueError("artifact paths must not contain control characters")
    segments = raw.split("/")
    if any(
        not segment or segment in {".", ".."} or not _SAFE_SEGMENT.fullmatch(segment) for segment in segments
    ):
        raise ValueError("artifact path contains an unsafe segment")
    return PurePosixPath(*segments)


def object_path(digest: str) -> PurePosixPath:
    value = validate_sha256(digest)
    return PurePosixPath("v1", "objects", "sha256", value[:2], value)


def ocr_cache_root_path(reuse_key: str, *, kind: str = "native") -> PurePosixPath:
    value = validate_sha256(reuse_key)
    if kind not in {"native", "adopted"}:
        raise ValueError("OCR cache kind must be native or adopted")
    return PurePosixPath("v1", "ocr-cache", kind, value[:2], value)


def ocr_manifest_path(reuse_key: str, *, kind: str = "native") -> PurePosixPath:
    return ocr_cache_root_path(reuse_key, kind=kind) / "manifest.json"


def ocr_ready_path(reuse_key: str, *, kind: str = "native") -> PurePosixPath:
    return ocr_cache_root_path(reuse_key, kind=kind) / "READY.json"


def generation_root_path(generation_id: str) -> PurePosixPath:
    return PurePosixPath("v1", "generations", validate_identifier(generation_id, label="generation_id"))


def generation_manifest_path(generation_id: str) -> PurePosixPath:
    return generation_root_path(generation_id) / "manifest.json"


def generation_ready_path(generation_id: str) -> PurePosixPath:
    return generation_root_path(generation_id) / "READY.json"


def generation_database_path(generation_id: str) -> PurePosixPath:
    return generation_root_path(generation_id) / "index.sqlite3"


def temporary_object_path(digest: str, token: str | None = None) -> PurePosixPath:
    value = validate_sha256(digest)
    suffix = token or uuid.uuid4().hex
    validate_identifier(suffix, label="temporary token")
    return PurePosixPath("v1", ".incoming", "objects", value[:2], f"{value}.{suffix}.tmp")


@dataclass(frozen=True, slots=True)
class GenerationPaths:
    """All deterministic paths belonging to one immutable generation."""

    generation_id: str
    root: PurePosixPath
    manifest: PurePosixPath
    ready: PurePosixPath
    serving_database: PurePosixPath

    @classmethod
    def for_generation(cls, generation_id: str) -> GenerationPaths:
        root = generation_root_path(generation_id)
        return cls(
            generation_id=validate_identifier(generation_id, label="generation_id"),
            root=root,
            manifest=root / "manifest.json",
            ready=root / "READY.json",
            serving_database=root / "index.sqlite3",
        )
