"""Environment settings shared by finite worker commands."""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from cardrag_core import (
    QWEN3_EMBEDDING_DIMENSION,
    QWEN3_EMBEDDING_MODEL,
    QWEN3_EMBEDDING_PROVIDER_IDS,
    Qwen3EmbeddingProviderId,
    channel_pointer_path,
    resolve_env_secret,
)

from .tokenizer_v5 import QWEN_TOKENIZER_SHA256

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
MIB = 1024 * 1024
MAX_PROVIDER_RESPONSE_BYTES = 64 * MIB


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
    raw = os.environ.get(name, str(default))
    if re.fullmatch(r"[0-9]+", raw) is None:
        raise ValueError(f"{name} must be a canonical non-negative decimal integer")
    value = int(raw)
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _positive_float(name: str, default: float) -> float:
    value = float(os.environ.get(name, str(default)))
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _boolean(name: str, default: bool) -> bool:
    value = os.environ.get(name, "true" if default else "false").strip().casefold()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value == "true"


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
    channel: str
    stable_publication_approved: bool
    remote_gc_approved: bool
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
    embedding_provider_id: Qwen3EmbeddingProviderId
    embedding_maximum_tokens: int
    embedding_tokenizer_path: Path
    embedding_timeout_seconds: float
    embedding_max_response_bytes: int
    embedding_metadata_max_response_bytes: int
    document_aggregation_profile_path: Path | None
    document_aggregation_profile_artifact_sha256: str | None
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
    pdf_cache_refresh_hours: float
    retain_generations: int
    retained_incomplete_runs: int
    garbage_grace_days: int
    collect_remote_garbage: bool

    @classmethod
    def from_env(cls, *, require_providers: bool = False, require_webdav: bool = False) -> WorkerSettings:
        dimension = _positive_int("CARDRAG_EMBEDDING_DIMENSION", QWEN3_EMBEDDING_DIMENSION)
        if dimension != QWEN3_EMBEDDING_DIMENSION:
            raise ValueError(f"CARDRAG_EMBEDDING_DIMENSION must be {QWEN3_EMBEDDING_DIMENSION}")
        embedding_model = os.environ.get("CARDRAG_EMBEDDING_MODEL", QWEN3_EMBEDDING_MODEL).strip()
        if embedding_model != QWEN3_EMBEDDING_MODEL:
            raise ValueError(f"CARDRAG_EMBEDDING_MODEL must be {QWEN3_EMBEDDING_MODEL}")
        raw_provider_id = os.environ.get("CARDRAG_EMBEDDING_PROVIDER_ID", "deepinfra").strip().casefold()
        if raw_provider_id not in QWEN3_EMBEDDING_PROVIDER_IDS:
            raise ValueError("CARDRAG_EMBEDDING_PROVIDER_ID must be deepinfra or nebius")
        provider_id = cast(Qwen3EmbeddingProviderId, raw_provider_id)
        maximum_tokens = _bounded_int(
            "CARDRAG_EMBEDDING_MAXIMUM_TOKENS",
            32_768 if provider_id == "deepinfra" else 32_000,
            minimum=1,
            maximum=32_768,
        )
        state_dir = Path(os.environ.get("CARDRAG_WORKER_STATE_DIR", "./data/cardrag-worker")).resolve()
        tokenizer_path_raw = os.environ.get("CARDRAG_QWEN_TOKENIZER_PATH")
        tokenizer_path = (
            Path(tokenizer_path_raw).resolve()
            if tokenizer_path_raw
            else state_dir / "contracts" / f"qwen3-embedding-8b-tokenizer-{QWEN_TOKENIZER_SHA256}.json"
        )
        aggregation_path_raw = os.environ.get("CARDRAG_DOCUMENT_AGGREGATION_PROFILE_FILE")
        aggregation_sha256_raw = os.environ.get("CARDRAG_DOCUMENT_AGGREGATION_PROFILE_ARTIFACT_SHA256")
        if (aggregation_path_raw is None) != (aggregation_sha256_raw is None):
            raise ValueError(
                "CARDRAG_DOCUMENT_AGGREGATION_PROFILE_FILE and "
                "CARDRAG_DOCUMENT_AGGREGATION_PROFILE_ARTIFACT_SHA256 are all-or-nothing"
            )
        aggregation_path: Path | None = None
        aggregation_sha256: str | None = None
        if aggregation_path_raw is not None and aggregation_sha256_raw is not None:
            if (
                not aggregation_path_raw
                or _CONTROL.search(aggregation_path_raw)
                or not Path(aggregation_path_raw).is_absolute()
            ):
                raise ValueError("CARDRAG_DOCUMENT_AGGREGATION_PROFILE_FILE must be an absolute path")
            aggregation_sha256 = aggregation_sha256_raw.strip()
            if re.fullmatch(r"[0-9a-f]{64}", aggregation_sha256) is None:
                raise ValueError(
                    "CARDRAG_DOCUMENT_AGGREGATION_PROFILE_ARTIFACT_SHA256 must be lowercase SHA-256"
                )
            aggregation_path = Path(os.path.abspath(aggregation_path_raw))
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
        channel = os.environ.get("CARDRAG_CHANNEL", "stable")
        channel_pointer_path(channel)
        stable_publication_approved = _boolean("CARDRAG_STABLE_PUBLICATION_APPROVED", False)
        remote_gc_approved = _boolean("CARDRAG_REMOTE_GC_APPROVED", False)
        collect_remote_garbage = _boolean("CARDRAG_COLLECT_REMOTE_GARBAGE", False)
        if collect_remote_garbage and (
            channel != "stable" or not stable_publication_approved or not remote_gc_approved
        ):
            raise ValueError(
                "CARDRAG_COLLECT_REMOTE_GARBAGE=true requires stable channel, "
                "CARDRAG_STABLE_PUBLICATION_APPROVED=true, and CARDRAG_REMOTE_GC_APPROVED=true"
            )
        return cls(
            state_dir=state_dir,
            channel=channel,
            stable_publication_approved=stable_publication_approved,
            remote_gc_approved=remote_gc_approved,
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
            embedding_model=embedding_model,
            embedding_dimension=dimension,
            embedding_provider_id=provider_id,
            embedding_maximum_tokens=maximum_tokens,
            embedding_tokenizer_path=tokenizer_path,
            embedding_timeout_seconds=_positive_float("CARDRAG_EMBEDDING_TIMEOUT_SECONDS", 120),
            embedding_max_response_bytes=_bounded_int(
                "CARDRAG_EMBEDDING_MAX_RESPONSE_BYTES",
                32 * MIB,
                minimum=1024,
                maximum=MAX_PROVIDER_RESPONSE_BYTES,
            ),
            embedding_metadata_max_response_bytes=_bounded_int(
                "CARDRAG_EMBEDDING_METADATA_MAX_RESPONSE_BYTES",
                2 * MIB,
                minimum=1024,
                maximum=16 * MIB,
            ),
            document_aggregation_profile_path=aggregation_path,
            document_aggregation_profile_artifact_sha256=aggregation_sha256,
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
            pdf_cache_refresh_hours=_positive_float("CARDRAG_PDF_CACHE_REFRESH_HOURS", 168),
            retain_generations=_bounded_int("CARDRAG_RETAIN_GENERATIONS", 2, minimum=2, maximum=20),
            retained_incomplete_runs=_bounded_int("CARDRAG_RETAIN_INCOMPLETE_RUNS", 2, minimum=1, maximum=20),
            garbage_grace_days=_bounded_int("CARDRAG_GARBAGE_GRACE_DAYS", 30, minimum=1, maximum=365),
            collect_remote_garbage=collect_remote_garbage,
        )

    @property
    def state_database(self) -> Path:
        return self.state_dir / "worker-state.sqlite3"

    @property
    def lock_file(self) -> Path:
        return self.state_dir / "worker.lock"
