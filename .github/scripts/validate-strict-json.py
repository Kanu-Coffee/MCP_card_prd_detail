#!/usr/bin/env python3
"""Reject ambiguous or unbounded JSON before jq evaluates release evidence."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

MAX_DOCUMENT_BYTES = 128 * 1024 * 1024


def _reject_constant(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON number: {value}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def validate(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("JSON input is not a regular file")
        if metadata.st_size <= 0 or metadata.st_size > MAX_DOCUMENT_BYTES:
            raise ValueError("JSON input size is outside the release bound")
        with os.fdopen(os.dup(descriptor), "r", encoding="utf-8", newline="") as stream:
            raw = stream.read(MAX_DOCUMENT_BYTES + 1)
        if len(raw.encode("utf-8")) != metadata.st_size:
            raise ValueError("JSON input changed while being read")
        json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        if _identity(os.fstat(descriptor)) != _identity(metadata):
            raise ValueError("JSON input identity changed while being read")
    finally:
        os.close(descriptor)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    arguments = parser.parse_args(argv)
    try:
        for path in arguments.paths:
            validate(path)
    except (OSError, UnicodeError, ValueError, RecursionError):
        print("strict JSON validation failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
