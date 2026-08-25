"""Fail-closed environment and Docker-secret resolution."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_MAX_SECRET_BYTES = 64 * 1024


class SecretResolutionError(ValueError):
    """A secret source was ambiguous, absent, or unsafe."""


def _validate_secret_text(value: str, *, label: str) -> str:
    if not value:
        raise SecretResolutionError(f"{label} is empty")
    if "\x00" in value or "\r" in value or "\n" in value:
        raise SecretResolutionError(f"{label} must be a single non-empty line")
    return value


def resolve_env_secret(
    name: str,
    *,
    environ: Mapping[str, str] | None = None,
    required: bool = True,
) -> str | None:
    """Resolve exactly one of ``NAME`` and ``NAME_FILE`` without leaking it.

    Secret files must be explicit absolute regular files. One terminal LF or
    CRLF, as commonly added by secret managers, is removed; other line breaks
    are rejected.
    """

    if not _ENV_NAME.fullmatch(name):
        raise ValueError("secret environment name must be uppercase snake case")
    source = os.environ if environ is None else environ
    file_name = f"{name}_FILE"
    has_value = name in source
    has_file = file_name in source
    if has_value and has_file:
        raise SecretResolutionError(f"set only one of {name} and {file_name}")
    if not has_value and not has_file:
        if required:
            raise SecretResolutionError(f"one of {name} or {file_name} is required")
        return None
    if has_value:
        return _validate_secret_text(source[name], label=name)

    secret_path = Path(source[file_name])
    if not secret_path.is_absolute():
        raise SecretResolutionError(f"{file_name} must be an absolute path")
    if secret_path.is_symlink() or not secret_path.is_file():
        raise SecretResolutionError(f"{file_name} must name a regular non-symlink file")
    if secret_path.stat().st_size > _MAX_SECRET_BYTES:
        raise SecretResolutionError(f"{file_name} exceeds the secret size limit")
    try:
        raw = secret_path.read_bytes()
        if raw.endswith(b"\r\n"):
            raw = raw[:-2]
        elif raw.endswith(b"\n"):
            raw = raw[:-1]
        value = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SecretResolutionError(f"{file_name} cannot be read as UTF-8") from exc
    return _validate_secret_text(value, label=file_name)
