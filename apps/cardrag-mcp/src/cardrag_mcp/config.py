"""Typed runtime configuration with file-backed secrets."""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, BeforeValidator, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from cardrag_mcp.quota import (
    DEFAULT_EXHAUSTIVE_AUDIT_MAX_ARTIFACT_BYTES,
    DEFAULT_EXHAUSTIVE_AUDIT_MAX_JOBS,
    DEFAULT_EXHAUSTIVE_AUDIT_MAX_TOTAL_BYTES,
    DEFAULT_MAX_STATE_BYTES,
    DEFAULT_RERANKER_AUDIT_MAX_ARTIFACT_BYTES,
    DEFAULT_RERANKER_AUDIT_MAX_JOBS,
    DEFAULT_RERANKER_AUDIT_MAX_TOTAL_BYTES,
    DEFAULT_RESERVED_FREE_SPACE_BYTES,
)

MIB = 1024 * 1024
GIB = 1024 * MIB
DEFAULT_MAX_VECTOR_SIDECAR_BYTES = 16 * GIB
MAX_VECTOR_SIDECAR_BYTES = 64 * GIB
DEFAULT_MAX_SERVING_DATABASE_BYTES = 4 * GIB
DEFAULT_MAX_GENERATION_DOWNLOAD_BYTES = 32 * GIB
MAX_GENERATION_DOWNLOAD_BYTES = 256 * GIB
MAX_STATE_BYTES = 512 * GIB
MAX_RESERVED_FREE_SPACE_BYTES = 128 * GIB
DEFAULT_EMBEDDING_MAX_RESPONSE_BYTES = MIB
DEFAULT_RERANKER_MAX_RESPONSE_BYTES = MIB
MAX_PROVIDER_RESPONSE_BYTES = 64 * MIB
MINIMUM_BEARER_TOKEN_LENGTH = 32


def _parse_bounded_integer(value: object) -> object:
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        return int(value)
    raise ValueError("bounded integer settings require canonical non-negative decimal integers")


BoundedInteger = Annotated[int, BeforeValidator(_parse_bounded_integer)]


def _read_secret(path: Path | None, *, label: str) -> str | None:
    if path is None:
        return None
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError(f"{label} file is not readable") from exc
    if not value:
        raise ValueError(f"{label} file is empty")
    return value


def _validate_bearer_token(value: str) -> str:
    if len(value) < MINIMUM_BEARER_TOKEN_LENGTH:
        raise ValueError(
            f"MCP bearer token must contain at least {MINIMUM_BEARER_TOKEN_LENGTH} characters"
        )
    if any(character.isspace() for character in value):
        raise ValueError("MCP bearer token must not contain whitespace")
    return value


def _is_loopback(hostname: str | None) -> bool:
    if hostname is None:
        return False
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


class Settings(BaseSettings):
    """Settings shared by the HTTP edge and background updater."""

    model_config = SettingsConfigDict(
        env_prefix="CARDRAG_",
        env_file=None,
        case_sensitive=False,
        extra="forbid",
    )

    environment: Literal["development", "test", "production"] = "production"
    channel: str = Field(default="stable", pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    mcp_host: str = "127.0.0.1"
    mcp_port: BoundedInteger = Field(default=8000, ge=1, le=65535)
    mcp_public_base_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:8000")
    mcp_state_dir: Path = Path("/var/lib/cardrag-serving")

    mcp_bearer_token: SecretStr | None = None
    mcp_bearer_token_file: Path | None = None

    openrouter_base_url: AnyHttpUrl = AnyHttpUrl("https://openrouter.ai/api/v1")
    openrouter_api_key: SecretStr | None = None
    openrouter_api_key_file: Path | None = None
    embedding_timeout_seconds: float = Field(default=60.0, ge=1, le=300)
    reranker_shadow_enabled: bool = False
    reranker_shadow_model: Literal["qwen/qwen3-reranker-8b"] = "qwen/qwen3-reranker-8b"
    reranker_shadow_provider_id: Literal["fireworks"] = "fireworks"
    reranker_shadow_max_candidates: BoundedInteger = Field(default=64, ge=1, le=256)
    reranker_shadow_timeout_seconds: float = Field(default=60.0, ge=1, le=300)
    reranker_shadow_max_response_bytes: BoundedInteger = Field(
        default=DEFAULT_RERANKER_MAX_RESPONSE_BYTES,
        ge=1024,
        le=DEFAULT_RERANKER_AUDIT_MAX_ARTIFACT_BYTES,
    )
    experimental_map_reduce_enabled: bool = False
    experimental_map_reduce_model: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}$",
    )
    experimental_map_reduce_provider_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,511}$",
    )
    experimental_map_reduce_evaluation_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    experimental_map_reduce_max_input_characters: BoundedInteger = Field(
        default=262_144,
        ge=16_384,
        le=2_000_000,
    )
    experimental_map_reduce_max_completion_tokens: BoundedInteger = Field(
        default=4_096,
        ge=16,
        le=16_384,
    )
    experimental_map_reduce_max_job_provider_calls: BoundedInteger = Field(
        default=4_096,
        ge=1,
        le=1_000_000,
    )
    experimental_map_reduce_max_job_input_characters: BoundedInteger = Field(
        default=268_435_456,
        ge=16_384,
        le=2_000_000_000,
    )
    experimental_map_reduce_max_job_output_tokens: BoundedInteger = Field(
        default=16_777_216,
        ge=16,
        le=2_000_000_000,
    )
    experimental_map_reduce_max_concurrent_provider_calls: BoundedInteger = Field(
        default=1,
        ge=1,
        le=32,
    )
    experimental_map_reduce_timeout_seconds: float = Field(
        default=120.0,
        ge=1,
        le=300,
    )
    experimental_map_reduce_max_response_bytes: BoundedInteger = Field(
        default=DEFAULT_RERANKER_MAX_RESPONSE_BYTES,
        ge=1_024,
        le=DEFAULT_RERANKER_AUDIT_MAX_ARTIFACT_BYTES,
    )

    webdav_base_url: AnyHttpUrl | None = None
    webdav_username: str | None = Field(default=None, max_length=512)
    webdav_username_file: Path | None = None
    webdav_password: SecretStr | None = None
    webdav_password_file: Path | None = None
    webdav_ca_file: Path | None = None
    webdav_connect_timeout_seconds: float = Field(default=10.0, ge=1, le=300)
    webdav_transfer_timeout_seconds: float = Field(default=600.0, ge=5, le=3_600)

    maximum_candidate_count: BoundedInteger = Field(default=250, ge=10, le=2_000)
    # Backward-compatible v1-v4 inline matrix cap and fallback resident cap.
    mcp_max_vector_bytes: BoundedInteger = Field(default=GIB, ge=MIB, le=GIB)
    mcp_max_vector_sidecar_bytes: BoundedInteger = Field(
        default=DEFAULT_MAX_VECTOR_SIDECAR_BYTES,
        ge=MIB,
        le=MAX_VECTOR_SIDECAR_BYTES,
    )
    mcp_max_resident_vector_bytes: BoundedInteger | None = Field(
        default=None,
        ge=MIB,
        le=16 * GIB,
    )
    mcp_max_serving_database_bytes: BoundedInteger = Field(
        default=DEFAULT_MAX_SERVING_DATABASE_BYTES,
        ge=MIB,
        le=64 * GIB,
    )
    mcp_max_generation_download_bytes: BoundedInteger = Field(
        default=DEFAULT_MAX_GENERATION_DOWNLOAD_BYTES,
        ge=MIB,
        le=MAX_GENERATION_DOWNLOAD_BYTES,
    )
    mcp_max_state_bytes: BoundedInteger = Field(
        default=DEFAULT_MAX_STATE_BYTES,
        ge=MIB,
        le=MAX_STATE_BYTES,
    )
    mcp_reserved_free_space_bytes: BoundedInteger = Field(
        default=DEFAULT_RESERVED_FREE_SPACE_BYTES,
        ge=0,
        le=MAX_RESERVED_FREE_SPACE_BYTES,
    )
    mcp_exhaustive_audit_max_jobs: BoundedInteger = Field(
        default=DEFAULT_EXHAUSTIVE_AUDIT_MAX_JOBS,
        ge=1,
        le=100_000,
    )
    mcp_exhaustive_audit_max_total_bytes: BoundedInteger = Field(
        default=DEFAULT_EXHAUSTIVE_AUDIT_MAX_TOTAL_BYTES,
        ge=MIB,
        le=64 * GIB,
    )
    mcp_exhaustive_audit_max_artifact_bytes: BoundedInteger = Field(
        default=DEFAULT_EXHAUSTIVE_AUDIT_MAX_ARTIFACT_BYTES,
        ge=MIB,
        le=DEFAULT_EXHAUSTIVE_AUDIT_MAX_ARTIFACT_BYTES,
    )
    mcp_reranker_audit_max_jobs: BoundedInteger = Field(
        default=DEFAULT_RERANKER_AUDIT_MAX_JOBS,
        ge=1,
        le=100_000,
    )
    mcp_reranker_audit_max_total_bytes: BoundedInteger = Field(
        default=DEFAULT_RERANKER_AUDIT_MAX_TOTAL_BYTES,
        ge=MIB,
        le=64 * GIB,
    )
    mcp_reranker_audit_max_artifact_bytes: BoundedInteger = Field(
        default=DEFAULT_RERANKER_AUDIT_MAX_ARTIFACT_BYTES,
        ge=1024,
        le=DEFAULT_RERANKER_AUDIT_MAX_ARTIFACT_BYTES,
    )
    maximum_pdf_bytes: BoundedInteger = Field(default=100 * MIB, ge=MIB, le=100 * MIB)
    embedding_max_response_bytes: BoundedInteger = Field(
        default=DEFAULT_EMBEDDING_MAX_RESPONSE_BYTES,
        ge=1024,
        le=MAX_PROVIDER_RESPONSE_BYTES,
    )
    mcp_update_interval_seconds: BoundedInteger = Field(default=300, ge=5, le=86_400)
    mcp_retain_generations: BoundedInteger = Field(default=2, ge=2, le=20)

    @model_validator(mode="after")
    def validate_secrets_and_urls(self) -> Settings:
        if self.mcp_bearer_token is not None and self.mcp_bearer_token_file is not None:
            raise ValueError("configure only one MCP bearer token source")
        if self.mcp_bearer_token is None and self.mcp_bearer_token_file is None:
            raise ValueError("mcp_bearer_token or mcp_bearer_token_file is required")
        direct_bearer = (
            self.mcp_bearer_token.get_secret_value()
            if self.mcp_bearer_token is not None
            else _read_secret(self.mcp_bearer_token_file, label="bearer token")
        )
        if direct_bearer is None:  # guarded above
            raise ValueError("MCP bearer token is unavailable")
        _validate_bearer_token(direct_bearer)
        if self.openrouter_api_key is not None and self.openrouter_api_key_file is not None:
            raise ValueError("configure only one OpenRouter API key source")
        if self.reranker_shadow_enabled:
            if self.channel != "candidate-v1.0.10":
                raise ValueError("reranker shadow is restricted to the candidate-v1.0.10 channel")
            if not self.openrouter_api_key_value():
                raise ValueError("reranker shadow requires an OpenRouter API key")
        if self.experimental_map_reduce_enabled:
            if self.channel != "candidate-v1.0.10":
                raise ValueError(
                    "experimental map-reduce is restricted to the candidate-v1.0.10 channel"
                )
            if not self.openrouter_api_key_value():
                raise ValueError("experimental map-reduce requires an OpenRouter API key")
            if (
                self.experimental_map_reduce_model is None
                or self.experimental_map_reduce_provider_id is None
                or self.experimental_map_reduce_evaluation_sha256 is None
            ):
                raise ValueError(
                    "experimental map-reduce requires a sealed model, provider, and evaluation hash"
                )
            if (
                self.experimental_map_reduce_max_job_output_tokens
                < self.experimental_map_reduce_max_completion_tokens
            ):
                raise ValueError(
                    "experimental map-reduce job output budget is smaller than one call"
                )
        if self.webdav_username is not None and self.webdav_username_file is not None:
            raise ValueError("configure only one WebDAV username source")
        if self.webdav_password is not None and self.webdav_password_file is not None:
            raise ValueError("configure only one WebDAV password source")
        for label, value in (
            ("mcp_public_base_url", self.mcp_public_base_url),
            ("openrouter_base_url", self.openrouter_base_url),
            ("webdav_base_url", self.webdav_base_url),
        ):
            if value is None:
                continue
            parsed = urlsplit(str(value))
            if parsed.username is not None:
                raise ValueError(f"{label} must not contain credentials")
            if label == "mcp_public_base_url":
                if parsed.query or parsed.fragment:
                    raise ValueError("mcp_public_base_url must not contain a query or fragment")
                if (
                    self.environment == "production"
                    and parsed.scheme != "https"
                    and not _is_loopback(parsed.hostname)
                ):
                    raise ValueError("production public MCP URLs require HTTPS off loopback")
            if label == "openrouter_base_url":
                if parsed.query or parsed.fragment:
                    raise ValueError("openrouter_base_url must not contain a query or fragment")
                if self.environment == "production" and parsed.scheme != "https":
                    raise ValueError("production OpenRouter URLs require HTTPS")
            if label == "webdav_base_url":
                if parsed.query or parsed.fragment:
                    raise ValueError("webdav_base_url must not contain a query or fragment")
                if self.environment == "production" and parsed.scheme != "https":
                    raise ValueError("production WebDAV requires HTTPS")
        if self.webdav_base_url is not None:
            if not self.webdav_username_value() or not self.webdav_password_value():
                raise ValueError("WebDAV username and password are required with webdav_base_url")
        if self.mcp_max_serving_database_bytes > self.mcp_max_generation_download_bytes:
            raise ValueError("serving database cap exceeds the generation download quota")
        if self.mcp_max_vector_sidecar_bytes > self.mcp_max_generation_download_bytes:
            raise ValueError("vector sidecar cap exceeds the generation download quota")
        if self.maximum_pdf_bytes > self.mcp_max_generation_download_bytes:
            raise ValueError("PDF cap exceeds the generation download quota")
        if self.mcp_max_generation_download_bytes > self.mcp_max_state_bytes:
            raise ValueError("generation download quota exceeds the MCP state quota")
        if (
            self.mcp_exhaustive_audit_max_artifact_bytes > self.mcp_exhaustive_audit_max_total_bytes
            or self.mcp_exhaustive_audit_max_total_bytes > self.mcp_max_state_bytes
        ):
            raise ValueError("exhaustive audit quotas exceed their containing state quota")
        if (
            self.mcp_reranker_audit_max_artifact_bytes > self.mcp_reranker_audit_max_total_bytes
            or self.mcp_reranker_audit_max_total_bytes > self.mcp_max_state_bytes
        ):
            raise ValueError("reranker audit quotas exceed their containing state quota")
        return self

    def bearer_token_value(self) -> str:
        value = (
            self.mcp_bearer_token.get_secret_value()
            if self.mcp_bearer_token is not None
            else _read_secret(self.mcp_bearer_token_file, label="bearer token")
        )
        if value is None:  # guarded by model validation
            raise RuntimeError("MCP bearer token is unavailable")
        return _validate_bearer_token(value)

    def resident_vector_limit_bytes(self) -> int:
        """Keep the legacy environment variable as the resident-cap fallback."""

        return (
            self.mcp_max_vector_bytes
            if self.mcp_max_resident_vector_bytes is None
            else self.mcp_max_resident_vector_bytes
        )

    def openrouter_api_key_value(self) -> str | None:
        if self.openrouter_api_key is not None:
            return self.openrouter_api_key.get_secret_value()
        return _read_secret(self.openrouter_api_key_file, label="OpenRouter API key")

    def webdav_username_value(self) -> str | None:
        return self.webdav_username or _read_secret(
            self.webdav_username_file, label="WebDAV username"
        )

    def webdav_password_value(self) -> str | None:
        if self.webdav_password is not None:
            return self.webdav_password.get_secret_value()
        return _read_secret(self.webdav_password_file, label="WebDAV password")
