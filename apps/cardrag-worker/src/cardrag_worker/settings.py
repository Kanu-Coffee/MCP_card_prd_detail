"""Environment settings shared by finite worker commands."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit

from cardrag_core import (
    QWEN3_EMBEDDING_DIMENSION,
    QWEN3_EMBEDDING_MODEL,
    QWEN3_EMBEDDING_PROVIDER_IDS,
    Qwen3EmbeddingProviderId,
    channel_pointer_path,
    resolve_env_secret,
)

from .capacity_v5 import (
    DEFAULT_MAX_SERVING_DATABASE_BYTES,
    DEFAULT_MAX_STATE_BYTES,
    DEFAULT_MAX_VECTOR_SIDECAR_BYTES,
    DEFAULT_MINIMUM_START_FREE_BYTES,
    DEFAULT_RESERVED_FREE_SPACE_BYTES,
    MAX_SAFE_BYTES,
)
from .embedding_v5 import (
    DEFAULT_EMBEDDING_REQUEST_MAX_ATTEMPTS,
    DEFAULT_EMBEDDING_RETRY_BASE_SECONDS,
    DEFAULT_EMBEDDING_RETRY_CAP_SECONDS,
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


def _worker_state_dir_from_env() -> Path:
    # Keep the unresolved absolute spelling so the startup capacity gate can
    # descriptor-walk and reject every symlinked ancestor.
    return Path(os.path.abspath(os.environ.get("CARDRAG_WORKER_STATE_DIR", "./data/cardrag-worker")))


def _aggregation_profile_from_env() -> tuple[Path | None, str | None]:
    path_raw = os.environ.get("CARDRAG_DOCUMENT_AGGREGATION_PROFILE_FILE")
    sha256_raw = os.environ.get("CARDRAG_DOCUMENT_AGGREGATION_PROFILE_ARTIFACT_SHA256")
    if (path_raw is None) != (sha256_raw is None):
        raise ValueError(
            "CARDRAG_DOCUMENT_AGGREGATION_PROFILE_FILE and "
            "CARDRAG_DOCUMENT_AGGREGATION_PROFILE_ARTIFACT_SHA256 are all-or-nothing"
        )
    if path_raw is None or sha256_raw is None:
        return None, None
    if not path_raw or _CONTROL.search(path_raw) or not Path(path_raw).is_absolute():
        raise ValueError("CARDRAG_DOCUMENT_AGGREGATION_PROFILE_FILE must be an absolute path")
    artifact_sha256 = sha256_raw.strip()
    if re.fullmatch(r"[0-9a-f]{64}", artifact_sha256) is None:
        raise ValueError("CARDRAG_DOCUMENT_AGGREGATION_PROFILE_ARTIFACT_SHA256 must be lowercase SHA-256")
    return Path(os.path.abspath(path_raw)), artifact_sha256


@dataclass(frozen=True, slots=True)
class WebDAVVerificationSettings:
    mode: Literal["periodic", "strict"] = "periodic"
    every_runs: int = 14
    max_age_days: int = 7
    new_cas_gib: int = 10
    generation_readback: Literal["final", "double"] = "final"
    force_full: bool = False

    def __post_init__(self) -> None:
        if self.mode not in {"periodic", "strict"} or self.generation_readback not in {"final", "double"}:
            raise ValueError("invalid WebDAV verification mode")
        for value in (self.every_runs, self.max_age_days, self.new_cas_gib):
            if type(value) is not int or not 1 <= value <= 1_000_000:
                raise ValueError("WebDAV verification thresholds must be positive bounded integers")
        if type(self.force_full) is not bool:
            raise ValueError("WebDAV force-full verification must be boolean")

    @classmethod
    def from_env(cls) -> WebDAVVerificationSettings:
        mode = os.environ.get("CARDRAG_WEBDAV_VERIFICATION_MODE", "periodic")
        readback = os.environ.get("CARDRAG_WEBDAV_GENERATION_READBACK_MODE", "final")
        if mode not in {"periodic", "strict"}:
            raise ValueError("CARDRAG_WEBDAV_VERIFICATION_MODE must be periodic or strict")
        if readback not in {"final", "double"}:
            raise ValueError("CARDRAG_WEBDAV_GENERATION_READBACK_MODE must be final or double")
        return cls(
            mode=cast(Literal["periodic", "strict"], mode),
            every_runs=_bounded_int(
                "CARDRAG_WEBDAV_FULL_VERIFY_EVERY_RUNS", 14, minimum=1, maximum=1_000_000
            ),
            max_age_days=_bounded_int(
                "CARDRAG_WEBDAV_FULL_VERIFY_MAX_AGE_DAYS", 7, minimum=1, maximum=1_000_000
            ),
            new_cas_gib=_bounded_int(
                "CARDRAG_WEBDAV_FULL_VERIFY_NEW_CAS_GIB", 10, minimum=1, maximum=1_000_000
            ),
            generation_readback=cast(Literal["final", "double"], readback),
            force_full=_boolean("CARDRAG_WEBDAV_FORCE_FULL_VERIFY", False),
        )


@dataclass(frozen=True, slots=True)
class PublicationResumeSettings:
    """Only the local/publication controls needed to resume an exact seal."""

    state_dir: Path
    minimum_start_free_bytes: int
    channel: str
    stable_publication_approved: bool
    document_aggregation_profile_path: Path | None
    document_aggregation_profile_artifact_sha256: str | None
    sqlite_cache_mib: int
    sqlite_mmap_mib: int
    webdav_upload_chunk_mib: int
    webdav_verification: WebDAVVerificationSettings = WebDAVVerificationSettings()
    publication_transport: str = "local"
    serving_dir: Path = Path("/var/lib/cardrag-serving")

    @classmethod
    def from_env(cls) -> PublicationResumeSettings:
        channel = os.environ.get("CARDRAG_CHANNEL", "stable")
        channel_pointer_path(channel)
        aggregation_path, aggregation_sha256 = _aggregation_profile_from_env()
        publication_transport = os.environ.get("CARDRAG_PUBLICATION_TRANSPORT", "local").strip().lower()
        serving_dir_str = os.environ.get("CARDRAG_SERVING_DIR", "/var/lib/cardrag-serving").strip()
        return cls(
            state_dir=_worker_state_dir_from_env(),
            minimum_start_free_bytes=_bounded_int(
                "CARDRAG_WORKER_MINIMUM_START_FREE_BYTES",
                DEFAULT_MINIMUM_START_FREE_BYTES,
                minimum=0,
                maximum=MAX_SAFE_BYTES,
            ),
            channel=channel,
            stable_publication_approved=_boolean("CARDRAG_STABLE_PUBLICATION_APPROVED", False),
            document_aggregation_profile_path=aggregation_path,
            document_aggregation_profile_artifact_sha256=aggregation_sha256,
            sqlite_cache_mib=_bounded_int("CARDRAG_STATE_SQLITE_CACHE_MIB", 256, minimum=1, maximum=1024),
            sqlite_mmap_mib=_bounded_int("CARDRAG_STATE_SQLITE_MMAP_MIB", 2048, minimum=0, maximum=4096),
            webdav_upload_chunk_mib=_bounded_int("CARDRAG_WEBDAV_UPLOAD_CHUNK_MIB", 8, minimum=1, maximum=16),
            webdav_verification=WebDAVVerificationSettings.from_env(),
            publication_transport=publication_transport,
            serving_dir=Path(serving_dir_str),
        )

    @property
    def state_database(self) -> Path:
        return self.state_dir / "worker-state.sqlite3"


_UNSAFE_TOML_VALUE_CHARACTERS = frozenset((chr(34), chr(39), chr(92), chr(9), chr(10), chr(32)))


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    state_dir: Path
    maximum_state_bytes: int
    reserved_free_space_bytes: int
    maximum_vector_sidecar_bytes: int
    maximum_serving_database_bytes: int
    minimum_start_free_bytes: int
    channel: str
    stable_publication_approved: bool
    ocr_cache_publication_approved: bool
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
    embedding_request_max_attempts: int
    embedding_retry_base_seconds: float
    embedding_retry_cap_seconds: float
    document_aggregation_profile_path: Path | None
    document_aggregation_profile_artifact_sha256: str | None
    ocr_provider: str
    ocr_model: str
    ocr_fallback_provider: str | None
    ocr_fallback_model: str | None
    openrouter_ocr_model: str
    openrouter_ocr_fallback_model: str | None
    compatible_ocr_models: tuple[str, ...]
    ocr_reasoning_effort: str
    ocr_provider_timeout_seconds: float
    ocr_cache_mode: Literal["read-only", "read-write"]
    ocr_cache_require_hit: bool
    ocr_cache_epoch: int
    ocr_prompt_version: str
    codex_executable: str
    codex_auth_root: Path | None
    codex_model_provider: str
    codex_model_provider_base_url: str
    codex_model_provider_env_key: str
    codex_model_provider_wire_api: str
    codex_model_catalog_json: str
    paddleocr_pipeline_version: str
    paddleocr_cache_dir: Path
    paddleocr_pdf_dpi: int
    paddleocr_cpu_threads: int
    paddleocr_timeout_seconds: float
    opencode_executable: str
    opencode_config: Path | None
    opencode_agent: str
    opencode_api_key_env_var: str
    opencode_ocr_reasoning_effort: str
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
    retirement_grace_runs: int
    retirement_grace_days: int
    retirement_max_per_run: int
    garbage_grace_days: int
    collect_remote_garbage: bool
    pdf_concurrency: int
    pdf_concurrency_per_issuer: int
    local_processing_workers: int
    sqlite_cache_mib: int
    sqlite_mmap_mib: int
    webdav_upload_chunk_mib: int
    issuer_discovery_concurrency: int = 4
    issuer_discovery_timeout_seconds: float = 300
    webdav_verification: WebDAVVerificationSettings = WebDAVVerificationSettings()
    external_ocr_allowed: bool = False
    pdf_cache_force_revalidate: bool = False
    publication_transport: Literal["local", "webdav"] = "local"
    serving_dir: Path = Path("/var/lib/cardrag-serving")
    backup_mode: Literal["disabled", "immediate", "hybrid", "manual"] = "disabled"
    backup_every_runs: int = 7
    backup_new_ocr_count: int = 30
    backup_new_bytes: int = 1073741824
    backup_max_pending_age_hours: int = 168
    backup_inline_budget_seconds: float = 300.0
    backup_derived_snapshot_enabled: bool = False

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
        state_dir = _worker_state_dir_from_env()
        tokenizer_path_raw = os.environ.get("CARDRAG_QWEN_TOKENIZER_PATH")
        tokenizer_path = (
            Path(tokenizer_path_raw).resolve()
            if tokenizer_path_raw
            else state_dir / "contracts" / f"qwen3-embedding-8b-tokenizer-{QWEN_TOKENIZER_SHA256}.json"
        )
        aggregation_path, aggregation_sha256 = _aggregation_profile_from_env()
        raw_transport = os.environ.get("CARDRAG_PUBLICATION_TRANSPORT", "local").strip().casefold()
        if raw_transport not in {"local", "webdav"}:
            raise ValueError("CARDRAG_PUBLICATION_TRANSPORT must be local or webdav")
        publication_transport = cast(Literal["local", "webdav"], raw_transport)
        serving_dir_raw = os.environ.get("CARDRAG_SERVING_DIR", "/var/lib/cardrag-serving").strip()
        serving_dir = Path(os.path.abspath(serving_dir_raw))

        raw_backup_mode = os.environ.get("CARDRAG_BACKUP_MODE", "disabled").strip().casefold()
        if raw_backup_mode not in {"disabled", "immediate", "hybrid", "manual"}:
            raise ValueError("CARDRAG_BACKUP_MODE must be disabled, immediate, hybrid, or manual")
        backup_mode = cast(Literal["disabled", "immediate", "hybrid", "manual"], raw_backup_mode)

        backup_every_runs = _positive_int("CARDRAG_BACKUP_EVERY_RUNS", 7)
        backup_new_ocr_count = _positive_int("CARDRAG_BACKUP_NEW_OCR_COUNT", 30)
        backup_new_bytes = _positive_int("CARDRAG_BACKUP_NEW_BYTES", 1073741824)
        backup_max_pending_age_hours = _positive_int("CARDRAG_BACKUP_MAX_PENDING_AGE_HOURS", 168)
        backup_inline_budget_seconds = _positive_float("CARDRAG_BACKUP_INLINE_BUDGET_SECONDS", 300.0)
        backup_derived_snapshot_enabled = _boolean("CARDRAG_BACKUP_DERIVED_SNAPSHOT_ENABLED", False)

        raw_webdav_base = os.environ.get("CARDRAG_WEBDAV_BASE_URL", "").strip()
        webdav_base = raw_webdav_base if raw_webdav_base else None
        actual_require_webdav = require_webdav and (publication_transport == "webdav")
        if actual_require_webdav and not webdav_base:
            raise ValueError("CARDRAG_WEBDAV_BASE_URL is required")
        ocr_provider = os.environ.get("CARDRAG_OCR_PROVIDER", "local-paddleocr").strip().casefold()
        if ocr_provider in {"paddleocr", "paddleocr-vl"}:
            ocr_provider = "local-paddleocr"
        fallback_provider = os.environ.get("CARDRAG_OCR_FALLBACK_PROVIDER")
        external_ocr_allowed = _boolean("CARDRAG_EXTERNAL_OCR_ALLOWED", False)
        for prov_label, prov_name in (
            ("primary", ocr_provider),
            ("fallback", fallback_provider.strip().casefold() if fallback_provider else None),
        ):
            if prov_name in {"codex-exec", "openrouter", "opencode"} and not external_ocr_allowed:
                raise ValueError(
                    f"External OCR provider '{prov_name}' ({prov_label}) is not allowed "
                    "when CARDRAG_EXTERNAL_OCR_ALLOWED=false"
                )
        is_openrouter_ocr = ocr_provider == "openrouter" or (
            fallback_provider is not None and fallback_provider.strip().casefold() == "openrouter"
        )
        api_key = _read_secret("CARDRAG_OPENROUTER_API_KEY", required=require_providers and is_openrouter_ocr)
        if require_providers and not api_key:
            raise ValueError("OpenRouter API key is required for embeddings")
        auth_root = os.environ.get("CARDRAG_CODEX_AUTH_ROOT")
        resolved_auth_root = Path(auth_root).resolve() if auth_root else None
        if resolved_auth_root is not None:
            resolved_state_dir = state_dir.resolve()
            if (
                resolved_auth_root == resolved_state_dir
                or resolved_state_dir in resolved_auth_root.parents
                or resolved_auth_root in resolved_state_dir.parents
            ):
                raise ValueError("CARDRAG_CODEX_AUTH_ROOT must not overlap CARDRAG_WORKER_STATE_DIR")
        codex_model_provider = os.environ.get("CARDRAG_CODEX_MODEL_PROVIDER", "").strip()
        codex_provider_base_url = os.environ.get("CARDRAG_CODEX_MODEL_PROVIDER_BASE_URL", "").strip()
        codex_provider_env_key = os.environ.get("CARDRAG_CODEX_MODEL_PROVIDER_ENV_KEY", "").strip()
        codex_provider_wire_api = (
            os.environ.get("CARDRAG_CODEX_MODEL_PROVIDER_WIRE_API", "responses").strip().casefold()
            or "responses"
        )
        codex_model_catalog_json = os.environ.get("CARDRAG_CODEX_MODEL_CATALOG_JSON", "").strip()
        if codex_model_provider:
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", codex_model_provider):
                raise ValueError("CARDRAG_CODEX_MODEL_PROVIDER must be a lowercase bare provider identifier")
            if not codex_provider_base_url.startswith("https://") or any(
                character in codex_provider_base_url for character in _UNSAFE_TOML_VALUE_CHARACTERS
            ):
                raise ValueError("CARDRAG_CODEX_MODEL_PROVIDER_BASE_URL must be a quoted-safe https URL")
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", codex_provider_env_key):
                raise ValueError(
                    "CARDRAG_CODEX_MODEL_PROVIDER_ENV_KEY must name an uppercase environment variable"
                )
            if codex_provider_wire_api not in {"chat", "responses"}:
                raise ValueError("CARDRAG_CODEX_MODEL_PROVIDER_WIRE_API must be chat or responses")
            if codex_model_catalog_json and not (
                codex_model_catalog_json.startswith("/")
                and not any(
                    character in codex_model_catalog_json for character in _UNSAFE_TOML_VALUE_CHARACTERS
                )
            ):
                raise ValueError("CARDRAG_CODEX_MODEL_CATALOG_JSON must be an absolute quoted-safe path")
            if (
                require_providers
                and (
                    ocr_provider == "codex-exec"
                    or (
                        fallback_provider is not None
                        and fallback_provider.strip().casefold() in {"codex", "codex-exec"}
                    )
                )
                and not os.environ.get(codex_provider_env_key, "").strip()
            ):
                raise ValueError(
                    f"{codex_provider_env_key} must be set when CARDRAG_CODEX_MODEL_PROVIDER is configured"
                )
        opencode_executable = os.environ.get("CARDRAG_OPENCODE_EXECUTABLE", "opencode").strip() or "opencode"
        opencode_config = (
            Path(os.environ["CARDRAG_OPENCODE_CONFIG"]).resolve()
            if os.environ.get("CARDRAG_OPENCODE_CONFIG")
            else None
        )
        opencode_agent = os.environ.get("CARDRAG_OPENCODE_AGENT", "ocr").strip() or "ocr"
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", opencode_agent):
            raise ValueError(f"CARDRAG_OPENCODE_AGENT must be a valid identifier: {opencode_agent!r}")
        opencode_api_key_env_var = (
            os.environ.get("CARDRAG_OPENCODE_API_KEY_ENV_KEY", "ALIBABA_TOKEN_PLAN_API_KEY").strip()
            or "ALIBABA_TOKEN_PLAN_API_KEY"
        )
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", opencode_api_key_env_var):
            raise ValueError("CARDRAG_OPENCODE_API_KEY_ENV_KEY must name an uppercase environment variable")
        opencode_effort_env = os.environ.get("CARDRAG_OPENCODE_OCR_REASONING_EFFORT", "").strip()
        opencode_ocr_reasoning_effort = (
            opencode_effort_env
            if opencode_effort_env
            else (
                os.environ.get("CARDRAG_OCR_REASONING_EFFORT", "medium").strip()
                if ocr_provider == "opencode"
                else "medium"
            )
        )
        is_opencode_ocr = ocr_provider == "opencode" or (
            fallback_provider is not None and fallback_provider.strip().casefold() == "opencode"
        )
        if require_providers and is_opencode_ocr:
            opencode_key = _read_secret(opencode_api_key_env_var, required=False)
            if not opencode_key:
                raise ValueError(
                    f"OpenCode API key ({opencode_api_key_env_var}) is required when OpenCode OCR is configured"
                )
            resolved_exe = shutil.which(opencode_executable)
            if not resolved_exe:
                raise ValueError(f"OpenCode executable '{opencode_executable}' not found in PATH")
            if not os.access(resolved_exe, os.X_OK):
                raise ValueError(f"OpenCode executable '{resolved_exe}' is not executable")
            if opencode_config is not None:
                if not opencode_config.is_file():
                    raise ValueError(f"OpenCode config file '{opencode_config}' not found")
                try:
                    cfg_data = json.loads(opencode_config.read_text(encoding="utf-8"))
                except Exception as err:
                    raise ValueError(f"OpenCode config file is not valid JSON: {err}") from None
                if not isinstance(cfg_data, dict):
                    raise ValueError("OpenCode config root must be a JSON object")
                tools = cfg_data.get("tools")
                if not isinstance(tools, dict) or tools.get("*") is not False:
                    raise ValueError("OpenCode config must strictly set 'tools': {'*': False}")
                perms = cfg_data.get("permission")
                if not isinstance(perms, dict) or perms.get("*") != "deny":
                    raise ValueError("OpenCode config must strictly set 'permission': {'*': 'deny'}")
                agents = cfg_data.get("agent")
                if isinstance(agents, dict) and opencode_agent in agents:
                    ag_cfg = agents[opencode_agent]
                    if isinstance(ag_cfg, dict):
                        ag_tools = ag_cfg.get("tools")
                        if not isinstance(ag_tools, dict) or ag_tools.get("*") is not False:
                            raise ValueError(
                                f"OpenCode agent '{opencode_agent}' must set 'tools': {'*': False}"
                            )
                        ag_perms = ag_cfg.get("permission")
                        if not isinstance(ag_perms, dict) or ag_perms.get("*") != "deny":
                            raise ValueError(
                                f"OpenCode agent '{opencode_agent}' must set 'permission': {'*': 'deny'}"
                            )
        ca_file = os.environ.get("CARDRAG_WEBDAV_CA_FILE")
        channel = os.environ.get("CARDRAG_CHANNEL", "stable")
        channel_pointer_path(channel)
        stable_publication_approved = _boolean("CARDRAG_STABLE_PUBLICATION_APPROVED", False)
        ocr_cache_publication_approved = _boolean("CARDRAG_OCR_CACHE_PUBLICATION_APPROVED", False)
        remote_gc_approved = _boolean("CARDRAG_REMOTE_GC_APPROVED", False)
        raw_ocr_cache_mode = os.environ.get("CARDRAG_OCR_CACHE_MODE", "read-only").strip().casefold()
        if raw_ocr_cache_mode not in {"read-only", "read-write"}:
            raise ValueError("CARDRAG_OCR_CACHE_MODE must be read-only or read-write")
        ocr_cache_mode = cast(Literal["read-only", "read-write"], raw_ocr_cache_mode)
        ocr_cache_require_hit = _boolean("CARDRAG_OCR_CACHE_REQUIRE_HIT", False)
        if ocr_cache_require_hit and ocr_cache_mode != "read-only":
            raise ValueError("CARDRAG_OCR_CACHE_REQUIRE_HIT=true requires CARDRAG_OCR_CACHE_MODE=read-only")
        if ocr_cache_mode == "read-write" and (channel != "stable" or not ocr_cache_publication_approved):
            raise ValueError(
                "CARDRAG_OCR_CACHE_MODE=read-write requires stable channel and separate "
                "CARDRAG_OCR_CACHE_PUBLICATION_APPROVED=true approval"
            )
        collect_remote_garbage = _boolean("CARDRAG_COLLECT_REMOTE_GARBAGE", False)
        if collect_remote_garbage:
            if channel == "candidate-v1.0.11":
                if not remote_gc_approved:
                    raise ValueError(
                        "CARDRAG_COLLECT_REMOTE_GARBAGE=true requires CARDRAG_REMOTE_GC_APPROVED=true"
                    )
            elif channel != "stable" or not stable_publication_approved or not remote_gc_approved:
                raise ValueError(
                    "CARDRAG_COLLECT_REMOTE_GARBAGE=true requires stable channel, "
                    "CARDRAG_STABLE_PUBLICATION_APPROVED=true, and CARDRAG_REMOTE_GC_APPROVED=true"
                )
        return cls(
            state_dir=state_dir,
            maximum_state_bytes=_bounded_int(
                "CARDRAG_WORKER_MAX_STATE_BYTES",
                DEFAULT_MAX_STATE_BYTES,
                minimum=1,
                maximum=MAX_SAFE_BYTES,
            ),
            reserved_free_space_bytes=_bounded_int(
                "CARDRAG_WORKER_RESERVED_FREE_SPACE_BYTES",
                DEFAULT_RESERVED_FREE_SPACE_BYTES,
                minimum=0,
                maximum=MAX_SAFE_BYTES,
            ),
            maximum_vector_sidecar_bytes=_bounded_int(
                "CARDRAG_WORKER_MAX_VECTOR_SIDECAR_BYTES",
                DEFAULT_MAX_VECTOR_SIDECAR_BYTES,
                minimum=1,
                maximum=MAX_SAFE_BYTES,
            ),
            maximum_serving_database_bytes=_bounded_int(
                "CARDRAG_WORKER_MAX_SERVING_DATABASE_BYTES",
                DEFAULT_MAX_SERVING_DATABASE_BYTES,
                minimum=1,
                maximum=MAX_SAFE_BYTES,
            ),
            minimum_start_free_bytes=_bounded_int(
                "CARDRAG_WORKER_MINIMUM_START_FREE_BYTES",
                DEFAULT_MINIMUM_START_FREE_BYTES,
                minimum=0,
                maximum=MAX_SAFE_BYTES,
            ),
            channel=channel,
            stable_publication_approved=stable_publication_approved,
            ocr_cache_publication_approved=ocr_cache_publication_approved,
            remote_gc_approved=remote_gc_approved,
            webdav_base_url=webdav_base.rstrip("/") if webdav_base else None,
            webdav_username=(
                _read_secret("CARDRAG_WEBDAV_USERNAME", required=actual_require_webdav)
                if (
                    actual_require_webdav
                    or "CARDRAG_WEBDAV_USERNAME" in os.environ
                    or "CARDRAG_WEBDAV_USERNAME_FILE" in os.environ
                )
                else None
            ),
            webdav_password=(
                _read_secret("CARDRAG_WEBDAV_PASSWORD", required=actual_require_webdav)
                if (
                    actual_require_webdav
                    or "CARDRAG_WEBDAV_PASSWORD" in os.environ
                    or "CARDRAG_WEBDAV_PASSWORD_FILE" in os.environ
                )
                else None
            ),
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
            embedding_request_max_attempts=_bounded_int(
                "CARDRAG_EMBEDDING_REQUEST_MAX_ATTEMPTS",
                DEFAULT_EMBEDDING_REQUEST_MAX_ATTEMPTS,
                minimum=1,
                maximum=100,
            ),
            embedding_retry_base_seconds=_positive_float(
                "CARDRAG_EMBEDDING_RETRY_BASE_SECONDS",
                DEFAULT_EMBEDDING_RETRY_BASE_SECONDS,
            ),
            embedding_retry_cap_seconds=_positive_float(
                "CARDRAG_EMBEDDING_RETRY_CAP_SECONDS",
                DEFAULT_EMBEDDING_RETRY_CAP_SECONDS,
            ),
            document_aggregation_profile_path=aggregation_path,
            document_aggregation_profile_artifact_sha256=aggregation_sha256,
            ocr_provider=ocr_provider,
            ocr_model=os.environ.get(
                "CARDRAG_OCR_MODEL",
                "PaddleOCR-VL-1.6"
                if ocr_provider == "local-paddleocr"
                else "alibaba-token-plan/qwen3.8-flash"
                if ocr_provider == "opencode"
                else "gpt-5.6-sol",
            ),
            ocr_fallback_provider=fallback_provider.strip().casefold() if fallback_provider else None,
            ocr_fallback_model=os.environ.get("CARDRAG_OCR_FALLBACK_MODEL"),
            openrouter_ocr_model=os.environ.get(
                "CARDRAG_OPENROUTER_OCR_MODEL", "google/gemini-3.1-pro"
            ).strip(),
            openrouter_ocr_fallback_model=(
                os.environ.get("CARDRAG_OPENROUTER_OCR_FALLBACK_MODEL", "").strip() or None
            ),
            compatible_ocr_models=tuple(
                m.strip()
                for m in os.environ.get("CARDRAG_OCR_COMPATIBLE_MODELS", "gpt-5.6-sol,gpt-5.4").split(",")
                if m.strip()
            ),
            ocr_reasoning_effort=os.environ.get(
                "CARDRAG_OCR_REASONING_EFFORT",
                "medium" if ocr_provider == "opencode" else "high",
            ),
            ocr_provider_timeout_seconds=_positive_float("CARDRAG_OCR_PROVIDER_TIMEOUT_SECONDS", 1800),
            ocr_cache_mode=ocr_cache_mode,
            ocr_cache_require_hit=ocr_cache_require_hit,
            ocr_cache_epoch=_nonnegative_int("CARDRAG_OCR_CACHE_EPOCH", 0),
            ocr_prompt_version=os.environ.get("CARDRAG_OCR_PROMPT_VERSION", "cardrag-ocr.ko.v2"),
            codex_executable=os.environ.get("CARDRAG_CODEX_EXECUTABLE", "codex"),
            codex_auth_root=resolved_auth_root,
            codex_model_provider=codex_model_provider,
            codex_model_provider_base_url=codex_provider_base_url,
            codex_model_provider_env_key=codex_provider_env_key,
            codex_model_provider_wire_api=codex_provider_wire_api,
            codex_model_catalog_json=codex_model_catalog_json,
            paddleocr_pipeline_version=os.environ.get("CARDRAG_PADDLEOCR_PIPELINE_VERSION", "v1.6").strip(),
            paddleocr_cache_dir=Path(
                os.path.abspath(
                    os.environ.get(
                        "CARDRAG_PADDLEOCR_CACHE_DIR",
                        os.fspath(state_dir / "paddleocr-cache"),
                    )
                )
            ),
            paddleocr_pdf_dpi=_bounded_int("CARDRAG_PADDLEOCR_PDF_DPI", 300, minimum=72, maximum=576),
            paddleocr_cpu_threads=_bounded_int("CARDRAG_PADDLEOCR_CPU_THREADS", 8, minimum=1, maximum=64),
            paddleocr_timeout_seconds=_positive_float("CARDRAG_PADDLEOCR_TIMEOUT_SECONDS", 14_400),
            opencode_executable=opencode_executable,
            opencode_config=opencode_config,
            opencode_agent=opencode_agent,
            opencode_api_key_env_var=opencode_api_key_env_var,
            opencode_ocr_reasoning_effort=opencode_ocr_reasoning_effort,
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
            retirement_grace_runs=_bounded_int("CARDRAG_RETIREMENT_GRACE_RUNS", 2, minimum=2, maximum=20),
            retirement_grace_days=_bounded_int("CARDRAG_RETIREMENT_GRACE_DAYS", 3, minimum=1, maximum=365),
            retirement_max_per_run=_bounded_int("CARDRAG_RETIREMENT_MAX_PER_RUN", 25, minimum=1, maximum=500),
            garbage_grace_days=_bounded_int("CARDRAG_GARBAGE_GRACE_DAYS", 1, minimum=1, maximum=365),
            collect_remote_garbage=collect_remote_garbage,
            pdf_concurrency=_bounded_int("CARDRAG_PDF_CONCURRENCY", 8, minimum=1, maximum=32),
            pdf_concurrency_per_issuer=_bounded_int(
                "CARDRAG_PDF_CONCURRENCY_PER_ISSUER", 2, minimum=1, maximum=8
            ),
            issuer_discovery_concurrency=_bounded_int(
                "CARDRAG_ISSUER_DISCOVERY_CONCURRENCY", 4, minimum=1, maximum=8
            ),
            issuer_discovery_timeout_seconds=_positive_float("CARDRAG_ISSUER_DISCOVERY_TIMEOUT_SECONDS", 300),
            local_processing_workers=_bounded_int(
                "CARDRAG_LOCAL_PROCESSING_WORKERS", 4, minimum=1, maximum=8
            ),
            sqlite_cache_mib=_bounded_int("CARDRAG_STATE_SQLITE_CACHE_MIB", 256, minimum=1, maximum=1024),
            sqlite_mmap_mib=_bounded_int("CARDRAG_STATE_SQLITE_MMAP_MIB", 2048, minimum=0, maximum=4096),
            webdav_upload_chunk_mib=_bounded_int("CARDRAG_WEBDAV_UPLOAD_CHUNK_MIB", 8, minimum=1, maximum=16),
            webdav_verification=WebDAVVerificationSettings.from_env(),
            external_ocr_allowed=external_ocr_allowed,
            pdf_cache_force_revalidate=_boolean("CARDRAG_PDF_CACHE_FORCE_REVALIDATE", False),
            publication_transport=publication_transport,
            serving_dir=serving_dir,
            backup_mode=backup_mode,
            backup_every_runs=backup_every_runs,
            backup_new_ocr_count=backup_new_ocr_count,
            backup_new_bytes=backup_new_bytes,
            backup_max_pending_age_hours=backup_max_pending_age_hours,
            backup_inline_budget_seconds=backup_inline_budget_seconds,
            backup_derived_snapshot_enabled=backup_derived_snapshot_enabled,
        )

    @property
    def state_database(self) -> Path:
        return self.state_dir / "worker-state.sqlite3"

    @property
    def lock_file(self) -> Path:
        return self.state_dir / "worker.lock"
