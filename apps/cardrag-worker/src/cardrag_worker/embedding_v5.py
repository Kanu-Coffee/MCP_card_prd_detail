"""Qwen v5 embedding profile, OpenRouter routing, and fail-closed preflight.

This module is intentionally separate from :mod:`cardrag_worker.providers` so
the immutable v4 1,536-dimensional provider contract remains untouched.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast
from urllib.parse import quote

import httpx
from cardrag_core import canonical_sha256
from cardrag_core.embedding import (
    QWEN3_DOCUMENT_POLICY,
    QWEN3_EMBEDDING_DIMENSION,
    QWEN3_EMBEDDING_DTYPE,
    QWEN3_EMBEDDING_MODEL,
    QWEN3_EMBEDDING_NORMALIZATION,
    QWEN3_EMBEDDING_PROVIDER,
    QWEN3_EMBEDDING_PROVIDER_IDS,
    QWEN3_QUERY_POLICY,
    QWEN3_TRUNCATION_POLICY,
    Qwen3EmbeddingProviderId,
    format_qwen3_document,
    format_qwen3_query,
    qwen3_embedding_cache_namespace,
    qwen3_embedding_profile_id,
)

InputKind = Literal["document", "query"]
TokenCounter = Callable[[str], int]

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PROVIDER_NAMES: dict[str, Qwen3EmbeddingProviderId] = {
    "deepinfra": "deepinfra",
    "nebius": "nebius",
    "nebiusai": "nebius",
}
_NORM_TOLERANCE = 2e-5
MIB = 1024 * 1024
MAX_EMBEDDING_RESPONSE_BYTES = 64 * MIB
MAX_METADATA_RESPONSE_BYTES = 16 * MIB
DEFAULT_EMBEDDING_RESPONSE_BYTES = 32 * MIB
DEFAULT_METADATA_RESPONSE_BYTES = 2 * MIB
DEFAULT_EMBEDDING_REQUEST_MAX_ATTEMPTS = 12
DEFAULT_EMBEDDING_RETRY_BASE_SECONDS = 1.0
DEFAULT_EMBEDDING_RETRY_CAP_SECONDS = 60.0
_RETRYABLE_EMBEDDING_HTTP_STATUSES = frozenset({408, 425, 429})
LOGGER = logging.getLogger(__name__)


class EmbeddingV5Error(RuntimeError):
    """A fail-closed Qwen profile, routing, or response contract error."""


class EmbeddingV5RequestError(EmbeddingV5Error):
    """A secret-free OpenRouter request failure classification."""

    def __init__(
        self,
        message: str,
        *,
        kind: str,
        status_code: int | None = None,
        attempts: int = 1,
    ) -> None:
        self.kind = kind if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", kind) else "unknown"
        self.status_code = status_code if type(status_code) is int and 100 <= status_code <= 599 else None
        self.attempts = attempts if type(attempts) is int and attempts >= 1 else 1
        super().__init__(message)


class EmbeddingV5TransientError(EmbeddingV5RequestError):
    """A transient OpenRouter failure that exhausted request-local retries."""


class EmbeddingV5PermanentRequestError(EmbeddingV5RequestError):
    """A permanent OpenRouter rejection or local request configuration error."""


_TRANSIENT_TRANSPORT_ERRORS = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.ProxyError,
    httpx.RemoteProtocolError,
    httpx.DecodingError,
)
_PERMANENT_TRANSPORT_ERRORS = (httpx.LocalProtocolError, httpx.UnsupportedProtocol)
_TRANSIENT_ENVELOPE_MARKERS = (
    "rate_limit",
    "too_many_requests",
    "timeout",
    "timed_out",
    "overload",
    "temporar",
    "unavailable",
    "server_error",
    "upstream_error",
    "provider_error",
    "network_error",
)


def _request_failure_kind(status_code: int) -> str:
    return {
        400: "invalid_request",
        401: "authentication",
        402: "billing",
        403: "authorization",
        404: "not_found",
        408: "timeout",
        413: "payload_too_large",
        422: "unprocessable_request",
        425: "too_early",
        429: "rate_limit",
        502: "upstream_error",
        503: "unavailable",
        504: "timeout",
        524: "timeout",
        529: "overloaded",
    }.get(status_code, "server_error" if status_code >= 500 else "client_error")


def _is_retryable_status(status_code: int) -> bool:
    return status_code in _RETRYABLE_EMBEDDING_HTTP_STATUSES or 500 <= status_code <= 599


def _response_error_envelope(response_body: bytes) -> tuple[bool, str, int | None] | None:
    """Return ``(transient, safe_kind, status)`` for a 2xx error envelope."""

    try:
        payload = json.loads(response_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, Mapping) or payload.get("error") is None:
        return None
    error = payload["error"]
    candidates: list[object] = []
    if isinstance(error, Mapping):
        candidates.extend((error.get("code"), error.get("status"), error.get("type")))
    else:
        candidates.append(error)
    for candidate in candidates:
        status_code: int | None = None
        if type(candidate) is int:
            status_code = candidate
        elif isinstance(candidate, str) and candidate.isdecimal():
            status_code = int(candidate)
        if status_code is not None and 100 <= status_code <= 599:
            return (
                _is_retryable_status(status_code),
                _request_failure_kind(status_code),
                status_code,
            )
    normalized = "_".join(
        re.sub(r"[^a-z0-9]+", "_", candidate.casefold()).strip("_")
        for candidate in candidates
        if isinstance(candidate, str)
    )
    transient = any(marker in normalized for marker in _TRANSIENT_ENVELOPE_MARKERS)
    return transient, "provider_transient" if transient else "provider_rejected", None


def _validate_retry_policy(
    request_max_attempts: int,
    retry_base_seconds: float,
    retry_cap_seconds: float,
) -> tuple[int, float, float]:
    if type(request_max_attempts) is not int or not 1 <= request_max_attempts <= 100:
        raise ValueError("embedding request max attempts must be between 1 and 100")
    if (
        isinstance(retry_base_seconds, bool)
        or not math.isfinite(retry_base_seconds)
        or retry_base_seconds < 0
    ):
        raise ValueError("embedding retry base seconds must be finite and non-negative")
    if isinstance(retry_cap_seconds, bool) or not math.isfinite(retry_cap_seconds) or retry_cap_seconds < 0:
        raise ValueError("embedding retry cap seconds must be finite and non-negative")
    return request_max_attempts, float(retry_base_seconds), float(retry_cap_seconds)


def _retry_seconds(
    attempt: int,
    response: httpx.Response | None,
    *,
    base_seconds: float,
    cap_seconds: float,
) -> float:
    delay = float(min(cap_seconds, base_seconds * (2 ** (attempt - 1))))
    if response is None:
        return delay
    raw_retry_after = response.headers.get("retry-after")
    if raw_retry_after is None:
        return delay
    try:
        retry_after = float(raw_retry_after)
    except ValueError:
        return delay
    if not math.isfinite(retry_after) or retry_after < 0:
        return delay
    return min(cap_seconds, max(delay, retry_after))


def _validate_response_limit(value: int, *, maximum: int, label: str) -> int:
    if type(value) is not int or value < 1024 or value > maximum:
        raise ValueError(f"{label} must be a bounded integer byte count")
    return value


async def _bounded_response_bytes(response: httpx.Response, maximum_bytes: int) -> bytes:
    raw_length = response.headers.get("content-length")
    if raw_length is not None and (not raw_length.isdigit() or int(raw_length) > maximum_bytes):
        raise EmbeddingV5Error("OpenRouter response Content-Length is invalid")
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(chunk) > maximum_bytes - len(body):
            raise EmbeddingV5Error("OpenRouter response exceeds its configured byte cap")
        body.extend(chunk)
    return bytes(body)


@dataclass(frozen=True, slots=True)
class OpenRouterEndpointMetadata:
    model: str
    provider_id: Qwen3EmbeddingProviderId
    provider_name: str
    endpoint_name: str
    quantization: str
    maximum_tokens: int
    supported_parameters: tuple[str, ...]
    metadata_sha256: str

    @property
    def is_fp8(self) -> bool:
        return "fp8" in self.quantization.casefold().replace("-", "")


@dataclass(frozen=True, slots=True)
class QwenEmbeddingProfileV5:
    profile_id: str
    provider: str
    model: str
    provider_id: Qwen3EmbeddingProviderId
    dimension: int
    dtype: str
    normalization: str
    document_policy: str
    query_policy: str
    maximum_tokens: int
    endpoint_name: str
    endpoint_metadata_sha256: str

    def __post_init__(self) -> None:
        if (
            self.provider != QWEN3_EMBEDDING_PROVIDER
            or self.model != QWEN3_EMBEDDING_MODEL
            or self.provider_id not in QWEN3_EMBEDDING_PROVIDER_IDS
            or self.dimension != QWEN3_EMBEDDING_DIMENSION
            or self.dtype != QWEN3_EMBEDDING_DTYPE
            or self.normalization != QWEN3_EMBEDDING_NORMALIZATION
            or self.document_policy != QWEN3_DOCUMENT_POLICY
            or self.query_policy != QWEN3_QUERY_POLICY
            or isinstance(self.maximum_tokens, bool)
            or self.maximum_tokens <= 0
            or not self.endpoint_name
            or not _SHA256.fullmatch(self.endpoint_metadata_sha256)
        ):
            raise ValueError("invalid Qwen v5 embedding profile")
        expected = qwen3_embedding_profile_id(
            self.provider_id,
            maximum_tokens=self.maximum_tokens,
        )
        if self.profile_id != expected:
            raise ValueError("embedding profile_id does not bind its provider contract")

    @classmethod
    def from_endpoint(cls, endpoint: OpenRouterEndpointMetadata) -> QwenEmbeddingProfileV5:
        if endpoint.model != QWEN3_EMBEDDING_MODEL:
            raise EmbeddingV5Error("endpoint metadata is for a different model")
        if endpoint.is_fp8:
            raise EmbeddingV5Error("FP8 endpoints are forbidden by the quality profile")
        return cls(
            profile_id=qwen3_embedding_profile_id(
                endpoint.provider_id,
                maximum_tokens=endpoint.maximum_tokens,
            ),
            provider=QWEN3_EMBEDDING_PROVIDER,
            model=QWEN3_EMBEDDING_MODEL,
            provider_id=endpoint.provider_id,
            dimension=QWEN3_EMBEDDING_DIMENSION,
            dtype=QWEN3_EMBEDDING_DTYPE,
            normalization=QWEN3_EMBEDDING_NORMALIZATION,
            document_policy=QWEN3_DOCUMENT_POLICY,
            query_policy=QWEN3_QUERY_POLICY,
            maximum_tokens=endpoint.maximum_tokens,
            endpoint_name=endpoint.endpoint_name,
            endpoint_metadata_sha256=endpoint.metadata_sha256,
        )

    @property
    def cache_namespace(self) -> str:
        return qwen3_embedding_cache_namespace(
            self.provider_id,
            maximum_tokens=self.maximum_tokens,
        )

    @property
    def truncation_policy(self) -> str:
        return QWEN3_TRUNCATION_POLICY


@dataclass(frozen=True, slots=True)
class SampleRepeatResult:
    input_sha256: str
    repeated_cosine: float


@dataclass(frozen=True, slots=True)
class ProviderPreflightReport:
    profile: QwenEmbeddingProfileV5
    sample_count: int
    samples: tuple[SampleRepeatResult, ...]
    minimum_repeat_cosine: float
    mean_repeat_cosine: float


@dataclass(frozen=True, slots=True)
class ProviderComparisonReport:
    providers: tuple[ProviderPreflightReport, ...]
    cross_provider_cosines: tuple[float, ...]
    minimum_cross_provider_cosine: float
    mean_cross_provider_cosine: float


# These are deliberately short contract-domain samples: the preflight is a
# provider consistency check, not a synthetic benchmark or a production gold
# set.  It still covers benefits, exclusions, thresholds, dates, and table-like
# rows that commonly appear in Korean card disclosures.
KOREAN_PREFLIGHT_SAMPLES: tuple[str, ...] = (
    "전월 이용금액 30만원 이상 시 대중교통 이용금액의 10%를 할인합니다.",
    "상품권 구매 및 선불카드 충전금액은 전월 이용실적에서 제외됩니다.",
    "해외 가맹점 결제 시 결제금액의 1.5%를 포인트로 적립합니다.",
    "할인한도는 월 1만원이며 잔여 한도는 다음 달로 이월되지 않습니다.",
    "국세·지방세·공과금·아파트관리비는 적립 대상에서 제외됩니다.",
    "연회비 | 국내전용 1만원 | 해외겸용 1만2천원",
    "카페 업종에서 건당 1만원 이상 결제하면 2천원을 할인합니다.",
    "신규 발급 회원은 카드 사용등록월의 다음 달 말일까지 실적 조건이 면제됩니다.",
    "간편결제에 등록하여 결제한 경우에도 해당 가맹점 업종 기준을 적용합니다.",
    "무이자할부 이용금액은 할인 및 포인트 적립 서비스에서 제외됩니다.",
    "주유 리터당 60원 할인은 일 1회, 월 4회 제공됩니다.",
    "통신요금 자동납부 승인 건에 한하여 월 최대 5천원을 할인합니다.",
    "혜택 제공일 이전에 매입이 취소되면 할인 금액도 함께 환수됩니다.",
    "전월 실적 70만원 이상 구간의 통합 할인한도는 2만원입니다.",
    "온라인 쇼핑몰 공식 홈페이지와 앱에서 결제한 건만 서비스 대상입니다.",
    "백화점·대형마트 내 임대매장은 가맹점 등록 업종에 따라 제외될 수 있습니다.",
    "해외 이용금액은 원화 환산 후 국제브랜드 수수료와 해외서비스 수수료가 부과됩니다.",
    "서비스 적용 순서는 승인 접수 순이며 한도 소진 후 거래에는 적용되지 않습니다.",
    "교육비 할인 대상은 학원 업종으로 등록된 오프라인 가맹점에 한합니다.",
    "보험료 결제금액은 전월 이용실적에는 포함되지만 포인트 적립 대상은 아닙니다.",
    "항공권 취소 수수료와 여행사 별도 수수료는 할인 대상 금액에 포함되지 않습니다.",
    "가족카드 이용실적은 본인카드와 합산되며 할인한도도 통합 관리됩니다.",
    "결제일 할인은 청구서에서 확인할 수 있으며 현장 할인과 중복 제공되지 않습니다.",
    "서비스 변경 시 변경일 6개월 전에 홈페이지와 이용대금명세서로 안내합니다.",
)


def _required_mapping(value: object, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EmbeddingV5Error(f"OpenRouter metadata {field} must be an object")
    return cast(Mapping[str, Any], value)


def _required_sequence(value: object, *, field: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise EmbeddingV5Error(f"OpenRouter metadata {field} must be an array")
    return cast(Sequence[object], value)


def _provider_id(row: Mapping[str, Any]) -> Qwen3EmbeddingProviderId | None:
    candidates = (row.get("provider_slug"), row.get("tag"), row.get("provider_name"))
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        normalized = re.sub(r"[^a-z0-9]", "", candidate.casefold())
        provider = _PROVIDER_NAMES.get(normalized)
        if provider is not None:
            return provider
    return None


def parse_openrouter_endpoint_metadata(
    payload: object,
    *,
    model: str = QWEN3_EMBEDDING_MODEL,
) -> tuple[OpenRouterEndpointMetadata, ...]:
    """Parse the public model endpoint response without trusting loose fields."""

    root = _required_mapping(payload, field="response")
    data = _required_mapping(root.get("data"), field="data")
    if data.get("id") != model:
        raise EmbeddingV5Error("OpenRouter metadata returned a different model id")
    endpoints = _required_sequence(data.get("endpoints"), field="data.endpoints")
    parsed: list[OpenRouterEndpointMetadata] = []
    for value in endpoints:
        row = _required_mapping(value, field="data.endpoints[]")
        provider_id = _provider_id(row)
        if provider_id is None:
            continue
        provider_name = row.get("provider_name")
        endpoint_name = row.get("name")
        quantization = row.get("quantization")
        if not all(isinstance(item, str) and item for item in (provider_name, endpoint_name, quantization)):
            raise EmbeddingV5Error("allowlisted endpoint has incomplete provider/precision metadata")
        endpoint_model = row.get("model_id", model)
        if endpoint_model != model:
            raise EmbeddingV5Error("endpoint row is bound to a different model")
        # OpenRouter currently emits ``max_prompt_tokens: null`` alongside a
        # valid integer ``context_length`` for both allowlisted Qwen routes.
        # A present null must not shadow the validated context fallback.
        maximum_tokens = row.get("max_prompt_tokens")
        if maximum_tokens is None:
            maximum_tokens = row.get("context_length")
        if isinstance(maximum_tokens, bool) or not isinstance(maximum_tokens, int) or maximum_tokens <= 0:
            raise EmbeddingV5Error("endpoint maximum input tokens are missing or invalid")
        supported_raw = _required_sequence(
            row.get("supported_parameters"),
            field="endpoint.supported_parameters",
        )
        if not all(isinstance(item, str) and item for item in supported_raw):
            raise EmbeddingV5Error("endpoint supported_parameters contains an invalid value")
        supported = tuple(sorted(set(cast(Sequence[str], supported_raw))))
        parsed.append(
            OpenRouterEndpointMetadata(
                model=model,
                provider_id=provider_id,
                provider_name=cast(str, provider_name),
                endpoint_name=cast(str, endpoint_name),
                quantization=cast(str, quantization),
                maximum_tokens=maximum_tokens,
                supported_parameters=supported,
                metadata_sha256=canonical_sha256(row),
            )
        )
    if not parsed:
        raise EmbeddingV5Error("metadata contains no allowlisted Qwen embedding endpoint")
    priority = {provider: index for index, provider in enumerate(QWEN3_EMBEDDING_PROVIDER_IDS)}
    return tuple(sorted(parsed, key=lambda row: (priority[row.provider_id], row.endpoint_name)))


def select_quality_endpoint(
    endpoints: Sequence[OpenRouterEndpointMetadata],
    *,
    provider_id: Qwen3EmbeddingProviderId,
) -> OpenRouterEndpointMetadata:
    """Select one routeable endpoint and reject ambiguous or FP8 provider routes."""

    matches = tuple(row for row in endpoints if row.provider_id == provider_id)
    if not matches:
        raise EmbeddingV5Error(f"no endpoint metadata found for {provider_id}")
    if any(row.is_fp8 for row in matches):
        raise EmbeddingV5Error(f"{provider_id} advertises an FP8 endpoint")
    if len(matches) != 1:
        raise EmbeddingV5Error(f"{provider_id} endpoint route is ambiguous")
    endpoint = matches[0]
    return endpoint


def format_embedding_input(kind: InputKind, text: str) -> str:
    if kind == "document":
        return format_qwen3_document(text)
    if kind == "query":
        return format_qwen3_query(text)
    raise ValueError("embedding input kind must be document or query")


def embedding_cache_key(
    profile: QwenEmbeddingProfileV5,
    *,
    kind: InputKind,
    formatted_input: str,
) -> tuple[str, str]:
    """Return cache key and exact input hash, both profile/provider separated."""

    input_sha256 = hashlib.sha256(formatted_input.encode("utf-8")).hexdigest()
    key = canonical_sha256(
        {
            "cache_namespace": profile.cache_namespace,
            "input_kind": kind,
            "input_sha256": input_sha256,
            "schema_version": "cardrag.embedding-cache-key.v5",
        }
    )
    return key, input_sha256


def _normalize_vector(values: object, *, index: int) -> list[float]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        raise EmbeddingV5Error(f"embedding {index} is not an array")
    if len(values) != QWEN3_EMBEDDING_DIMENSION:
        raise EmbeddingV5Error(
            f"embedding {index} dimension {len(values)} is not {QWEN3_EMBEDDING_DIMENSION}"
        )
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
        raise EmbeddingV5Error(f"embedding {index} contains a non-numeric value")
    try:
        vector = [float(value) for value in values]
    except (TypeError, ValueError):
        raise EmbeddingV5Error(f"embedding {index} contains a non-numeric value") from None
    if not all(math.isfinite(value) for value in vector):
        raise EmbeddingV5Error(f"embedding {index} contains a non-finite value")
    norm = math.sqrt(sum(value * value for value in vector))
    if not math.isfinite(norm) or norm <= 1e-12:
        raise EmbeddingV5Error(f"embedding {index} has an invalid norm")
    normalized = [value / norm for value in vector]
    normalized_norm = sum(value * value for value in normalized)
    if not math.isclose(normalized_norm, 1.0, rel_tol=_NORM_TOLERANCE, abs_tol=_NORM_TOLERANCE):
        raise EmbeddingV5Error(f"embedding {index} failed L2 normalization")
    return normalized


def _response_provider(payload: Mapping[str, Any], response_provider: str | None) -> str | None:
    for candidate in (payload.get("provider_id"), payload.get("provider")):
        if isinstance(candidate, str) and candidate:
            return candidate
    metadata = payload.get("openrouter_metadata")
    if isinstance(metadata, Mapping):
        for key in ("provider_slug", "provider_id", "provider_name", "provider"):
            candidate = metadata.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
    return response_provider


def _provider_matches(actual: str, expected: Qwen3EmbeddingProviderId) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", actual.casefold())
    return _PROVIDER_NAMES.get(normalized) == expected


class OpenRouterQwenEmbeddingProviderV5:
    """A single-provider, non-fallback Qwen embedding client."""

    def __init__(
        self,
        *,
        api_key: str,
        profile: QwenEmbeddingProfileV5,
        token_counter: TokenCounter,
        base_url: str = "https://openrouter.ai/api/v1",
        timeout_seconds: float = 120,
        maximum_response_bytes: int = DEFAULT_EMBEDDING_RESPONSE_BYTES,
        request_max_attempts: int = DEFAULT_EMBEDDING_REQUEST_MAX_ATTEMPTS,
        retry_base_seconds: float = DEFAULT_EMBEDDING_RETRY_BASE_SECONDS,
        retry_cap_seconds: float = DEFAULT_EMBEDDING_RETRY_CAP_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("OpenRouter API key is empty")
        if not callable(token_counter):
            raise TypeError("a model-specific token counter is required")
        if not base_url.startswith("https://") and transport is None:
            raise ValueError("OpenRouter base URL must use HTTPS")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive and finite")
        _validate_response_limit(
            maximum_response_bytes,
            maximum=MAX_EMBEDDING_RESPONSE_BYTES,
            label="maximum embedding response bytes",
        )
        request_max_attempts, retry_base_seconds, retry_cap_seconds = _validate_retry_policy(
            request_max_attempts,
            retry_base_seconds,
            retry_cap_seconds,
        )
        if not callable(sleep):
            raise TypeError("embedding retry sleep must be callable")
        self.api_key = api_key
        self.profile = profile
        self.token_counter = token_counter
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.maximum_response_bytes = maximum_response_bytes
        self.request_max_attempts = request_max_attempts
        self.retry_base_seconds: float = float(retry_base_seconds)
        self.retry_cap_seconds: float = float(retry_cap_seconds)
        self.sleep = sleep
        self.transport = transport
        self.logical_batch_count = 0
        self.wire_attempt_count = 0

    @property
    def provider(self) -> str:
        return self.profile.provider

    @property
    def model(self) -> str:
        return self.profile.model

    @property
    def dimension(self) -> int:
        return self.profile.dimension

    def _validate_token_limits(self, values: Sequence[str]) -> None:
        for index, value in enumerate(values):
            count = self.token_counter(value)
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                raise EmbeddingV5Error("token counter returned an invalid value")
            if count > self.profile.maximum_tokens:
                raise EmbeddingV5Error(
                    f"embedding input {index} exceeds sealed maximum_tokens; truncation is forbidden"
                )

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        return _is_retryable_status(status_code)

    def _retry_seconds(self, attempt: int, response: httpx.Response | None) -> float:
        return _retry_seconds(
            attempt,
            response,
            base_seconds=self.retry_base_seconds,
            cap_seconds=self.retry_cap_seconds,
        )

    async def _request_embedding(self, body: Mapping[str, object]) -> tuple[bytes, str | None]:
        self.logical_batch_count += 1
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            transport=self.transport,
        ) as client:
            for attempt in range(1, self.request_max_attempts + 1):
                response: httpx.Response | None = None
                response_body = b""
                retry_reason: str
                retry_status: int | None = None
                permanent_failure: tuple[str, int | None] | None = None
                try:
                    self.wire_attempt_count += 1
                    async with client.stream(
                        "POST",
                        self.base_url + "/embeddings",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        json=body,
                    ) as response:
                        response.raise_for_status()
                        response_body = await _bounded_response_bytes(
                            response,
                            self.maximum_response_bytes,
                        )
                    response_failure = _response_error_envelope(response_body)
                    if response_failure is None:
                        return response_body, response.headers.get("x-openrouter-provider")
                    transient, retry_reason, retry_status = response_failure
                    if not transient:
                        permanent_failure = (retry_reason, retry_status)
                except httpx.HTTPStatusError as exc:
                    response = exc.response
                    retry_status = response.status_code
                    retry_reason = _request_failure_kind(retry_status)
                    if not self._is_retryable_status(retry_status):
                        permanent_failure = (retry_reason, retry_status)
                except _PERMANENT_TRANSPORT_ERRORS:
                    permanent_failure = ("client_protocol", None)
                    retry_reason = "client_protocol"
                except _TRANSIENT_TRANSPORT_ERRORS:
                    retry_reason = "transport"
                except httpx.RequestError:
                    permanent_failure = ("client_request", None)
                    retry_reason = "client_request"

                if permanent_failure is not None:
                    kind, status_code = permanent_failure
                    response = None
                    response_body = b""
                    raise EmbeddingV5PermanentRequestError(
                        "OpenRouter embedding request was permanently rejected",
                        kind=kind,
                        status_code=status_code,
                        attempts=attempt,
                    ) from None

                if attempt >= self.request_max_attempts:
                    LOGGER.error(
                        "OpenRouter embedding request retries exhausted reason=%s status=%s attempts=%d",
                        retry_reason,
                        retry_status if retry_status is not None else "none",
                        attempt,
                    )
                    response = None
                    response_body = b""
                    raise EmbeddingV5TransientError(
                        "OpenRouter embedding request failed after transient retries",
                        kind=retry_reason,
                        status_code=retry_status,
                        attempts=attempt,
                    ) from None
                delay = self._retry_seconds(attempt, response)
                LOGGER.warning(
                    "OpenRouter embedding request transient failure reason=%s status=%s "
                    "attempt=%d/%d retry_seconds=%.3f",
                    retry_reason,
                    retry_status if retry_status is not None else "none",
                    attempt,
                    self.request_max_attempts,
                    delay,
                )
                response = None
                response_body = b""
                await self.sleep(delay)

        raise RuntimeError("embedding request retry loop terminated unexpectedly")  # pragma: no cover

    async def _embed_formatted(self, values: Sequence[str]) -> list[list[float]]:
        if not values:
            return []
        self._validate_token_limits(values)
        body = {
            "model": self.profile.model,
            "input": list(values),
            "dimensions": self.profile.dimension,
            "encoding_format": "float",
            "provider": {
                "order": [self.profile.provider_id],
                "only": [self.profile.provider_id],
                "allow_fallbacks": False,
                # OpenRouter's live Qwen endpoint metadata currently omits
                # ``dimensions`` for DeepInfra and Nebius even though both
                # endpoints return the requested 4,096D embeddings.  Setting
                # this routing flag would filter both pinned providers before
                # the request can prove the actual response contract.
                "require_parameters": False,
            },
        }
        response_body, response_provider = await self._request_embedding(body)
        try:
            payload = json.loads(response_body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise EmbeddingV5Error("OpenRouter embedding response is not JSON") from None
        root = _required_mapping(payload, field="embedding response")
        # OpenRouter accepts the lower-case registry slug in requests but its
        # live Qwen response canonicalizes the same slug as
        # ``Qwen/Qwen3-Embedding-8B``.  Bind every path component and spelling
        # while allowing only this case normalization.
        actual_model = root.get("model")
        if not isinstance(actual_model, str) or actual_model.casefold() != self.profile.model.casefold():
            raise EmbeddingV5Error("OpenRouter response model does not match the sealed profile")
        actual_provider = _response_provider(root, response_provider)
        if actual_provider is None or not _provider_matches(actual_provider, self.profile.provider_id):
            raise EmbeddingV5Error("OpenRouter response provider does not match the pinned route")
        data = _required_sequence(root.get("data"), field="embedding response.data")
        if len(data) != len(values):
            raise EmbeddingV5Error("OpenRouter embedding response count mismatch")
        indexed: dict[int, list[float]] = {}
        for value in data:
            row = _required_mapping(value, field="embedding response.data[]")
            index = row.get("index")
            if isinstance(index, bool) or not isinstance(index, int) or index in indexed:
                raise EmbeddingV5Error("OpenRouter embedding response index is invalid")
            indexed[index] = _normalize_vector(row.get("embedding"), index=index)
        if set(indexed) != set(range(len(values))):
            raise EmbeddingV5Error("OpenRouter embedding response indices are not contiguous")
        return [indexed[index] for index in range(len(values))]

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return await self._embed_formatted([format_qwen3_document(text) for text in texts])

    async def embed_queries(self, queries: Sequence[str]) -> list[list[float]]:
        return await self._embed_formatted([format_qwen3_query(query) for query in queries])


async def fetch_openrouter_qwen_endpoints(
    *,
    api_key: str,
    base_url: str = "https://openrouter.ai/api/v1",
    timeout_seconds: float = 30,
    maximum_response_bytes: int = DEFAULT_METADATA_RESPONSE_BYTES,
    request_max_attempts: int = DEFAULT_EMBEDDING_REQUEST_MAX_ATTEMPTS,
    retry_base_seconds: float = DEFAULT_EMBEDDING_RETRY_BASE_SECONDS,
    retry_cap_seconds: float = DEFAULT_EMBEDDING_RETRY_CAP_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[OpenRouterEndpointMetadata, ...]:
    if not api_key:
        raise ValueError("OpenRouter API key is empty")
    if not base_url.startswith("https://") and transport is None:
        raise ValueError("OpenRouter base URL must use HTTPS")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive and finite")
    _validate_response_limit(
        maximum_response_bytes,
        maximum=MAX_METADATA_RESPONSE_BYTES,
        label="maximum endpoint metadata response bytes",
    )
    request_max_attempts, retry_base_seconds, retry_cap_seconds = _validate_retry_policy(
        request_max_attempts,
        retry_base_seconds,
        retry_cap_seconds,
    )
    if not callable(sleep):
        raise TypeError("embedding retry sleep must be callable")
    author, slug = QWEN3_EMBEDDING_MODEL.split("/", 1)
    url = f"{base_url.rstrip('/')}/models/{quote(author, safe='')}/{quote(slug, safe='')}/endpoints"
    response_body = b""
    async with httpx.AsyncClient(timeout=timeout_seconds, transport=transport) as client:
        for attempt in range(1, request_max_attempts + 1):
            response: httpx.Response | None = None
            retry_reason: str
            retry_status: int | None = None
            permanent_failure: tuple[str, int | None] | None = None
            try:
                async with client.stream(
                    "GET",
                    url,
                    headers={"Authorization": f"Bearer {api_key}"},
                ) as response:
                    response.raise_for_status()
                    response_body = await _bounded_response_bytes(
                        response,
                        maximum_response_bytes,
                    )
                response_failure = _response_error_envelope(response_body)
                if response_failure is None:
                    break
                transient, retry_reason, retry_status = response_failure
                if not transient:
                    permanent_failure = (retry_reason, retry_status)
            except httpx.HTTPStatusError as exc:
                response = exc.response
                retry_status = response.status_code
                retry_reason = _request_failure_kind(retry_status)
                if not _is_retryable_status(retry_status):
                    permanent_failure = (retry_reason, retry_status)
            except _PERMANENT_TRANSPORT_ERRORS:
                permanent_failure = ("client_protocol", None)
                retry_reason = "client_protocol"
            except _TRANSIENT_TRANSPORT_ERRORS:
                retry_reason = "transport"
            except httpx.RequestError:
                permanent_failure = ("client_request", None)
                retry_reason = "client_request"

            if permanent_failure is not None:
                kind, status_code = permanent_failure
                response = None
                response_body = b""
                raise EmbeddingV5PermanentRequestError(
                    "OpenRouter endpoint metadata request was permanently rejected",
                    kind=kind,
                    status_code=status_code,
                    attempts=attempt,
                ) from None
            if attempt >= request_max_attempts:
                LOGGER.error(
                    "OpenRouter endpoint metadata retries exhausted reason=%s status=%s attempts=%d",
                    retry_reason,
                    retry_status if retry_status is not None else "none",
                    attempt,
                )
                response = None
                response_body = b""
                raise EmbeddingV5TransientError(
                    "OpenRouter endpoint metadata request failed after transient retries",
                    kind=retry_reason,
                    status_code=retry_status,
                    attempts=attempt,
                ) from None
            delay = _retry_seconds(
                attempt,
                response,
                base_seconds=retry_base_seconds,
                cap_seconds=retry_cap_seconds,
            )
            LOGGER.warning(
                "OpenRouter endpoint metadata transient failure reason=%s status=%s "
                "attempt=%d/%d retry_seconds=%.3f",
                retry_reason,
                retry_status if retry_status is not None else "none",
                attempt,
                request_max_attempts,
                delay,
            )
            response = None
            response_body = b""
            await sleep(delay)
        else:  # pragma: no cover - every exhausted path raises above.
            raise RuntimeError("endpoint metadata retry loop terminated unexpectedly")
    try:
        payload = json.loads(response_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise EmbeddingV5Error("OpenRouter endpoint metadata response is not JSON") from None
    return parse_openrouter_endpoint_metadata(payload)


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        raise EmbeddingV5Error("cannot compare incompatible embedding vectors")
    score = sum(a * b for a, b in zip(left, right, strict=True))
    if not math.isfinite(score):
        raise EmbeddingV5Error("embedding cosine is non-finite")
    return max(-1.0, min(1.0, score))


async def preflight_openrouter_qwen_providers(
    *,
    api_key: str,
    token_counter: TokenCounter,
    samples: Sequence[str] = KOREAN_PREFLIGHT_SAMPLES,
    base_url: str = "https://openrouter.ai/api/v1",
    timeout_seconds: float = 120,
    transport: httpx.AsyncBaseTransport | None = None,
    minimum_repeat_cosine: float = 0.999,
    embedding_maximum_response_bytes: int = DEFAULT_EMBEDDING_RESPONSE_BYTES,
    metadata_maximum_response_bytes: int = DEFAULT_METADATA_RESPONSE_BYTES,
    request_max_attempts: int = DEFAULT_EMBEDDING_REQUEST_MAX_ATTEMPTS,
    retry_base_seconds: float = DEFAULT_EMBEDDING_RETRY_BASE_SECONDS,
    retry_cap_seconds: float = DEFAULT_EMBEDDING_RETRY_CAP_SECONDS,
) -> ProviderComparisonReport:
    """Evaluate DeepInfra then Nebius with two identical 20+ sample calls each."""

    if len(samples) < 20 or len(set(samples)) != len(samples) or any(not item.strip() for item in samples):
        raise ValueError("preflight requires at least 20 unique non-empty Korean samples")
    if not -1 <= minimum_repeat_cosine <= 1 or not math.isfinite(minimum_repeat_cosine):
        raise ValueError("minimum_repeat_cosine must be finite and within [-1,1]")
    endpoints = await fetch_openrouter_qwen_endpoints(
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        maximum_response_bytes=metadata_maximum_response_bytes,
        request_max_attempts=request_max_attempts,
        retry_base_seconds=retry_base_seconds,
        retry_cap_seconds=retry_cap_seconds,
        transport=transport,
    )
    reports: list[ProviderPreflightReport] = []
    first_runs: list[list[list[float]]] = []
    for provider_id in QWEN3_EMBEDDING_PROVIDER_IDS:
        endpoint = select_quality_endpoint(endpoints, provider_id=provider_id)
        profile = QwenEmbeddingProfileV5.from_endpoint(endpoint)
        provider = OpenRouterQwenEmbeddingProviderV5(
            api_key=api_key,
            profile=profile,
            token_counter=token_counter,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            maximum_response_bytes=embedding_maximum_response_bytes,
            request_max_attempts=request_max_attempts,
            retry_base_seconds=retry_base_seconds,
            retry_cap_seconds=retry_cap_seconds,
            transport=transport,
        )
        first = await provider.embed_documents(samples)
        repeated = await provider.embed_documents(samples)
        cosines = tuple(_cosine(left, right) for left, right in zip(first, repeated, strict=True))
        if min(cosines) < minimum_repeat_cosine:
            raise EmbeddingV5Error(f"{provider_id} repeated-input cosine is below the gate")
        sample_rows = tuple(
            SampleRepeatResult(
                input_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                repeated_cosine=cosine,
            )
            for text, cosine in zip(samples, cosines, strict=True)
        )
        reports.append(
            ProviderPreflightReport(
                profile=profile,
                sample_count=len(samples),
                samples=sample_rows,
                minimum_repeat_cosine=min(cosines),
                mean_repeat_cosine=sum(cosines) / len(cosines),
            )
        )
        first_runs.append(first)
    left_provider, right_provider = first_runs
    cross = tuple(_cosine(left, right) for left, right in zip(left_provider, right_provider, strict=True))
    return ProviderComparisonReport(
        providers=tuple(reports),
        cross_provider_cosines=cross,
        minimum_cross_provider_cosine=min(cross),
        mean_cross_provider_cosine=sum(cross) / len(cross),
    )


__all__ = [
    "DEFAULT_EMBEDDING_REQUEST_MAX_ATTEMPTS",
    "DEFAULT_EMBEDDING_RETRY_BASE_SECONDS",
    "DEFAULT_EMBEDDING_RETRY_CAP_SECONDS",
    "EmbeddingV5Error",
    "EmbeddingV5PermanentRequestError",
    "EmbeddingV5RequestError",
    "EmbeddingV5TransientError",
    "InputKind",
    "KOREAN_PREFLIGHT_SAMPLES",
    "OpenRouterEndpointMetadata",
    "OpenRouterQwenEmbeddingProviderV5",
    "ProviderComparisonReport",
    "ProviderPreflightReport",
    "QwenEmbeddingProfileV5",
    "SampleRepeatResult",
    "TokenCounter",
    "embedding_cache_key",
    "fetch_openrouter_qwen_endpoints",
    "format_embedding_input",
    "parse_openrouter_endpoint_metadata",
    "preflight_openrouter_qwen_providers",
    "select_quality_endpoint",
]
