"""Validated WebDAV connection settings with explicit secret loading."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, Self, cast
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from pydantic import Field, SecretStr, field_validator, model_validator

from .domain import NonEmptyText, StrictFrozenModel
from .secrets import resolve_env_secret

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _normalize_base_url(
    value: str,
    *,
    environment: str,
    allow_insecure_http: bool,
) -> str:
    if _CONTROL.search(value):
        raise ValueError("WebDAV URL must not contain control characters")
    parsed = urlsplit(value)
    scheme = parsed.scheme.casefold()
    insecure_allowed = environment != "production" and allow_insecure_http
    if scheme != "https" and not (scheme == "http" and insecure_allowed):
        raise ValueError("WebDAV URL must use HTTPS; HTTP is restricted to explicit non-production tests")
    if not parsed.hostname:
        raise ValueError("WebDAV URL must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("credentials are forbidden in the WebDAV URL")
    if parsed.query or parsed.fragment:
        raise ValueError("WebDAV URL must not contain a query or fragment")
    try:
        _port = parsed.port
    except ValueError as exc:
        raise ValueError("WebDAV URL port is invalid") from exc

    decoded = unquote(parsed.path or "/")
    if "\\" in decoded or "\x00" in decoded:
        raise ValueError("WebDAV base path is unsafe")
    segments = [segment for segment in decoded.split("/") if segment]
    if any(segment in {".", ".."} for segment in segments):
        raise ValueError("WebDAV base path must not contain traversal segments")
    encoded_path = "/" + "/".join(quote(segment, safe="-._~") for segment in segments)
    if not encoded_path.endswith("/"):
        encoded_path += "/"
    host = parsed.hostname.casefold()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host
    if _port is not None:
        netloc += f":{_port}"
    return urlunsplit((scheme, netloc, encoded_path, "", ""))


class WebDAVSettings(StrictFrozenModel):
    """Connection settings safe to share without serializing the password."""

    environment: Literal["development", "test", "production"] = "production"
    base_url: NonEmptyText
    username: NonEmptyText
    password: SecretStr
    connect_timeout_seconds: float = Field(default=10.0, strict=True, gt=0, le=120)
    transfer_timeout_seconds: float = Field(default=600.0, strict=True, gt=0, le=3600)
    ca_file: Path | None = None
    allow_insecure_http: bool = False

    @field_validator("ca_file")
    @classmethod
    def validate_ca_file(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        if not value.is_absolute():
            raise ValueError("WebDAV CA bundle path must be absolute")
        if value.is_symlink() or not value.is_file():
            raise ValueError("WebDAV CA bundle must be a regular non-symlink file")
        return value

    @model_validator(mode="after")
    def normalize_and_validate_url(self) -> Self:
        normalized = _normalize_base_url(
            self.base_url,
            environment=self.environment,
            allow_insecure_http=self.allow_insecure_http,
        )
        object.__setattr__(self, "base_url", normalized)
        return self

    @property
    def httpx_verify(self) -> bool | str:
        return str(self.ca_file) if self.ca_file is not None else True

    @classmethod
    def from_env(
        cls,
        *,
        prefix: str = "CARDRAG_WEBDAV_",
        environ: Mapping[str, str] | None = None,
        allow_insecure_http: bool = False,
    ) -> WebDAVSettings:
        source = {} if environ is None else environ
        if environ is None:
            import os

            source = os.environ
        base_url_name = f"{prefix}BASE_URL"
        connect_name = f"{prefix}CONNECT_TIMEOUT_SECONDS"
        transfer_name = f"{prefix}TRANSFER_TIMEOUT_SECONDS"
        ca_name = f"{prefix}CA_FILE"
        environment = cast(
            Literal["development", "test", "production"],
            source.get("CARDRAG_ENVIRONMENT", "production"),
        )
        if base_url_name not in source:
            raise ValueError(f"{base_url_name} is required")
        return cls(
            environment=environment,
            base_url=source[base_url_name],
            username=resolve_env_secret(f"{prefix}USERNAME", environ=source) or "",
            password=SecretStr(resolve_env_secret(f"{prefix}PASSWORD", environ=source) or ""),
            connect_timeout_seconds=float(source.get(connect_name, "10")),
            transfer_timeout_seconds=float(source.get(transfer_name, "600")),
            allow_insecure_http=allow_insecure_http,
            ca_file=Path(source[ca_name]) if ca_name in source else None,
        )
