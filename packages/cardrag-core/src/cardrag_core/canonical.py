"""Deterministic JSON and SHA-256 helpers shared across process boundaries."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import fields, is_dataclass
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

_READ_SIZE = 1024 * 1024


def sha256_bytes(payload: bytes | bytearray | memoryview) -> str:
    """Return a lowercase SHA-256 digest for an in-memory payload."""

    return hashlib.sha256(bytes(payload)).hexdigest()


def sha256_chunks(chunks: Iterable[bytes]) -> tuple[str, int]:
    """Hash a byte stream and return ``(digest, size_bytes)``."""

    digest = hashlib.sha256()
    size_bytes = 0
    for chunk in chunks:
        if not isinstance(chunk, bytes):
            raise TypeError("SHA-256 stream chunks must be bytes")
        digest.update(chunk)
        size_bytes += len(chunk)
    return digest.hexdigest(), size_bytes


def sha256_file(path: str | Path) -> tuple[str, int]:
    """Hash a regular, non-symlink file without loading it into memory."""

    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise ValueError("hash source must be a regular non-symlink file")
    with source.open("rb") as handle:
        return sha256_chunks(iter(lambda: handle.read(_READ_SIZE), b""))


def _json_ready(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _json_ready(value.model_dump(mode="json", by_alias=True, exclude_none=False))
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _json_ready(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Enum):
        return _json_ready(value.value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("canonical datetimes must be timezone-aware")
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        ready: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("canonical JSON object keys must be strings")
            ready[key] = _json_ready(item)
        return ready
    if isinstance(value, (tuple, list)):
        return [_json_ready(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical JSON does not allow non-finite floats")
        return value
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    """Encode *value* as the package's single canonical UTF-8 JSON form."""

    return json.dumps(
        _json_ready(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    """Hash the canonical JSON representation of *value*."""

    return sha256_bytes(canonical_json_bytes(value))
