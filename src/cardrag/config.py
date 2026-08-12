"""Explicit, typed runtime configuration.

No setting is discovered from the current directory or a user's HOME.  A
deployment may opt into an env file by passing `_env_file` explicitly when it
constructs :class:`Settings`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CARDRAG_",
        env_file=None,
        extra="forbid",
        case_sensitive=False,
    )

    environment: Literal["development", "test", "production"] = "production"
    application_version: str = "dev"
    image_revision: str = "unknown"
    database_url: SecretStr
    storage_root: Path
    generation_root: Path
    build_root: Path
    page_cache_root: Path

    mcp_server_url: AnyHttpUrl
    oidc_issuer: AnyHttpUrl
    oidc_audience: str = "cardrag-mcp"
    oidc_jwks_cache_seconds: int = Field(default=300, ge=30, le=3600)
    required_search_scope: str = "search"
    required_source_scope: str = "source_pdf"

    host: str = "0.0.0.0"  # noqa: S104 - container listener; Compose binds loopback
    port: int = Field(default=8000, ge=1, le=65535)
    max_concurrent_requests: int = Field(default=5, ge=1, le=100)
    request_timeout_seconds: float = Field(default=45.0, ge=1, le=300)
    postgres_statement_timeout_seconds: float = Field(default=40.0, ge=1, le=295)
    postgres_lock_timeout_seconds: float = Field(default=5.0, ge=0.1, le=60)
    max_pdf_bytes: int = Field(default=100 * 1024 * 1024, ge=1024)
    page_cache_ttl_seconds: int = Field(default=7 * 24 * 3600, ge=60)

    openrouter_base_url: AnyHttpUrl = AnyHttpUrl("https://openrouter.ai/api/v1")
    openrouter_api_key_file: Path | None = None
    embedding_model: str = "openai/text-embedding-3-small"
    # Migration 001 declares pgvector ``vector(1536)``.  Accepting another
    # runtime value would record a manifest that the serving schema cannot
    # actually query, so a dimension change must ship as an explicit schema
    # migration and generation rebuild rather than an environment toggle.
    embedding_dimension: Literal[1536] = 1536
    embedding_timeout_seconds: float = Field(default=60.0, ge=1, le=300)
    codex_bin: str = "codex"
    codex_auth_root: Path | None = None
    ocr_model: str = "gpt-5.4"
    ocr_reasoning_effort: Literal["low", "medium", "high", "xhigh"] = "high"
    ocr_fallback_model: str = "google/gemini-2.5-pro"
    ocr_timeout_seconds: int = Field(default=600, ge=30, le=3600)
    render_scale: float = Field(default=3.0, ge=1.0, le=8.0)
    ocr_chunk_pages: int = Field(default=2, ge=1, le=10)

    worker_lease_seconds: int = Field(default=120, ge=15, le=3600)
    worker_heartbeat_seconds: int = Field(default=30, ge=5, le=300)
    max_job_attempts: int = Field(default=5, ge=1, le=20)
    worker_metrics_enabled: bool = True
    worker_metrics_host: str = "127.0.0.1"
    worker_metrics_port: int = Field(default=9090, ge=1, le=65535)
    issuer_request_timeout_seconds: float = Field(default=30.0, ge=1, le=120)
    issuer_max_download_bytes: int = Field(default=100 * 1024 * 1024, ge=1024)
    woori_discovery_minimum: int = Field(default=20, ge=2)
    kb_discovery_minimum: int = Field(default=20, ge=2)
    shinhan_discovery_minimum: int = Field(default=20, ge=2)
    discovery_minimum_previous_ratio: float = Field(default=0.6, gt=0.0, le=1.0)

    def issuer_discovery_minimum(self, issuer: str) -> int:
        return {
            "woori": self.woori_discovery_minimum,
            "kb": self.kb_discovery_minimum,
            "shinhan": self.shinhan_discovery_minimum,
        }[issuer]

    @field_validator("storage_root", "generation_root", "build_root", "page_cache_root")
    @classmethod
    def require_absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("runtime paths must be absolute")
        return value

    @field_validator("codex_bin")
    @classmethod
    def command_must_be_basename_or_absolute(cls, value: str) -> str:
        if not value or "\x00" in value:
            raise ValueError("invalid command")
        return value

    @field_validator("postgres_statement_timeout_seconds")
    @classmethod
    def statement_timeout_precedes_request_timeout(cls, value: float, info: ValidationInfo) -> float:
        request_timeout = info.data.get("request_timeout_seconds")
        if isinstance(request_timeout, (int, float)) and value >= request_timeout:
            raise ValueError("PostgreSQL statement timeout must be below the request timeout")
        return value

    @field_validator("postgres_lock_timeout_seconds")
    @classmethod
    def lock_timeout_precedes_statement_timeout(cls, value: float, info: ValidationInfo) -> float:
        statement_timeout = info.data.get("postgres_statement_timeout_seconds")
        if isinstance(statement_timeout, (int, float)) and value >= statement_timeout:
            raise ValueError("PostgreSQL lock timeout must be below the statement timeout")
        return value

    def secret_text_from_file(self, path: Path | None) -> str | None:
        if path is None:
            return None
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            raise ValueError(f"secret file is empty: {path.name}")
        return raw
