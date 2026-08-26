"""Environment settings shared by finite worker commands."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from cardrag_core import EMBEDDING_DIMENSION, resolve_env_secret

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _read_secret(name: str, *, required: bool = False) -> str | None:
    return resolve_env_secret(name, required=required)


def _positive_int(name: str, default: int) -> int:
    value = int(os.environ.get(name, str(default)))
    if value < 1:
        raise ValueError(f"{name} must be positive")
    return value


def _nonnegative_int(name: str, default: int) -> int:
    value = int(os.environ.get(name, str(default)))
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _bounded_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    value = int(os.environ.get(name, str(default)))
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _positive_float(name: str, default: float) -> float:
    value = float(os.environ.get(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _provider_base_url(name: str, default: str) -> str:
    value = os.environ.get(name, default).strip().rstrip("/")
    parsed = urlsplit(value)
    environment = os.environ.get("CARDRAG_ENVIRONMENT", "production").strip().casefold()
    if environment not in {"development", "test", "production"}:
        raise ValueError("CARDRAG_ENVIRONMENT must be development, test, or production")
    if (
        _CONTROL.search(value)
        or parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{name} must be a credential-free HTTP(S) base URL")
    if environment == "production" and parsed.scheme != "https":
        raise ValueError(f"{name} must use HTTPS in production")
    return value


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    state_dir: Path
    webdav_base_url: str | None
    webdav_username: str | None
    webdav_password: str | None
    webdav_ca_file: Path | None
    webdav_connect_timeout_seconds: float
    webdav_transfer_timeout_seconds: float
    openrouter_base_url: str
    openrouter_api_key: str | None
    embedding_model: str
    embedding_dimension: int
    ocr_provider: str
    ocr_model: str
    ocr_fallback_provider: str | None
    ocr_fallback_model: str | None
    ocr_reasoning_effort: str
    ocr_provider_timeout_seconds: float
    ocr_cache_epoch: int
    ocr_prompt_version: str
    codex_executable: str
    codex_auth_root: Path | None
    ocr_chunk_pages: int
    ocr_whole_document_max_pages: int
    ocr_context_pages_before: int
    ocr_context_pages_after: int
    ocr_render_scale_milli: int
    stage_max_attempts: int
    retry_cap_seconds: float

    @classmethod
    def from_env(cls, *, require_providers: bool = False, require_webdav: bool = False) -> WorkerSettings:
        dimension = _positive_int("CARDRAG_EMBEDDING_DIMENSION", EMBEDDING_DIMENSION)
        if dimension != EMBEDDING_DIMENSION:
            raise ValueError(f"CARDRAG_EMBEDDING_DIMENSION must be {EMBEDDING_DIMENSION}")
        webdav_base = os.environ.get("CARDRAG_WEBDAV_BASE_URL")
        if require_webdav and not webdav_base:
            raise ValueError("CARDRAG_WEBDAV_BASE_URL is required")
        ocr_provider = os.environ.get("CARDRAG_OCR_PROVIDER", "codex-exec").strip().casefold()
        fallback_provider = os.environ.get("CARDRAG_OCR_FALLBACK_PROVIDER")
        api_key = _read_secret(
            "CARDRAG_OPENROUTER_API_KEY", required=require_providers and ocr_provider == "openrouter"
        )
        if require_providers and not api_key:
            raise ValueError("OpenRouter API key is required for embeddings")
        auth_root = os.environ.get("CARDRAG_CODEX_AUTH_ROOT")
        ca_file = os.environ.get("CARDRAG_WEBDAV_CA_FILE")
        return cls(
            state_dir=Path(os.environ.get("CARDRAG_WORKER_STATE_DIR", "./data/cardrag-worker")).resolve(),
            webdav_base_url=webdav_base.rstrip("/") if webdav_base else None,
            webdav_username=_read_secret("CARDRAG_WEBDAV_USERNAME", required=require_webdav),
            webdav_password=_read_secret("CARDRAG_WEBDAV_PASSWORD", required=require_webdav),
            webdav_ca_file=Path(ca_file).resolve() if ca_file else None,
            webdav_connect_timeout_seconds=_positive_float("CARDRAG_WEBDAV_CONNECT_TIMEOUT_SECONDS", 10),
            webdav_transfer_timeout_seconds=_positive_float("CARDRAG_WEBDAV_TRANSFER_TIMEOUT_SECONDS", 600),
            openrouter_base_url=_provider_base_url(
                "CARDRAG_OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
            ),
            openrouter_api_key=api_key,
            embedding_model=os.environ.get("CARDRAG_EMBEDDING_MODEL", "openai/text-embedding-3-small"),
            embedding_dimension=dimension,
            ocr_provider=ocr_provider,
            ocr_model=os.environ.get("CARDRAG_OCR_MODEL", "gpt-5.6-sol"),
            ocr_fallback_provider=fallback_provider.strip().casefold() if fallback_provider else None,
            ocr_fallback_model=os.environ.get("CARDRAG_OCR_FALLBACK_MODEL"),
            ocr_reasoning_effort=os.environ.get("CARDRAG_OCR_REASONING_EFFORT", "high"),
            ocr_provider_timeout_seconds=_positive_float("CARDRAG_OCR_PROVIDER_TIMEOUT_SECONDS", 1800),
            ocr_cache_epoch=_nonnegative_int("CARDRAG_OCR_CACHE_EPOCH", 0),
            ocr_prompt_version=os.environ.get("CARDRAG_OCR_PROMPT_VERSION", "cardrag-ocr.ko.v2"),
            codex_executable=os.environ.get("CARDRAG_CODEX_EXECUTABLE", "codex"),
            codex_auth_root=Path(auth_root).resolve() if auth_root else None,
            ocr_chunk_pages=_bounded_int("CARDRAG_OCR_CHUNK_PAGES", 2, minimum=1, maximum=100),
            ocr_whole_document_max_pages=_bounded_int(
                "CARDRAG_OCR_WHOLE_DOCUMENT_MAX_PAGES", 4, minimum=1, maximum=100
            ),
            ocr_context_pages_before=_bounded_int(
                "CARDRAG_OCR_CONTEXT_PAGES_BEFORE", 1, minimum=0, maximum=20
            ),
            ocr_context_pages_after=_bounded_int("CARDRAG_OCR_CONTEXT_PAGES_AFTER", 1, minimum=0, maximum=20),
            ocr_render_scale_milli=_bounded_int(
                "CARDRAG_OCR_RENDER_SCALE_MILLI", 6000, minimum=1000, maximum=8000
            ),
            stage_max_attempts=_positive_int("CARDRAG_STAGE_MAX_ATTEMPTS", 4),
            retry_cap_seconds=_positive_float("CARDRAG_RETRY_CAP_SECONDS", 30),
        )

    @property
    def state_database(self) -> Path:
        return self.state_dir / "worker-state.sqlite3"

    @property
    def lock_file(self) -> Path:
        return self.state_dir / "worker.lock"
