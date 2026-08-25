"""Typed runtime configuration with file-backed secrets."""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIB = 1024 * 1024
GIB = 1024 * MIB
MINIMUM_BEARER_TOKEN_LENGTH = 32


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
    mcp_host: str = "127.0.0.1"
    mcp_port: int = Field(default=8000, ge=1, le=65535)
    mcp_public_base_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:8000")
    mcp_state_dir: Path = Path("/var/lib/cardrag-serving")

    mcp_bearer_token: SecretStr | None = None
    mcp_bearer_token_file: Path | None = None

    openrouter_base_url: AnyHttpUrl = AnyHttpUrl("https://openrouter.ai/api/v1")
    openrouter_api_key: SecretStr | None = None
    openrouter_api_key_file: Path | None = None
    embedding_timeout_seconds: float = Field(default=60.0, ge=1, le=300)

    webdav_base_url: AnyHttpUrl | None = None
    webdav_username: str | None = Field(default=None, max_length=512)
    webdav_username_file: Path | None = None
    webdav_password: SecretStr | None = None
    webdav_password_file: Path | None = None
    webdav_ca_file: Path | None = None
    webdav_connect_timeout_seconds: float = Field(default=10.0, ge=1, le=300)
    webdav_transfer_timeout_seconds: float = Field(default=600.0, ge=5, le=3_600)

    maximum_candidate_count: int = Field(default=250, ge=10, le=2_000)
    mcp_max_vector_bytes: int = Field(default=GIB, ge=MIB, le=GIB)
    maximum_pdf_bytes: int = Field(default=100 * MIB, ge=MIB, le=100 * MIB)
    mcp_update_interval_seconds: int = Field(default=300, ge=5, le=86_400)
    mcp_retain_generations: int = Field(default=3, ge=3, le=20)

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
