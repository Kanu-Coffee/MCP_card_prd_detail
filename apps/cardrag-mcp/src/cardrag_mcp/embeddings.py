"""Bounded OpenRouter query embedding client."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

import httpx
import numpy as np
from cardrag_core import (
    EMBEDDING_DIMENSION,
    EMBEDDING_POLICY_VERSION,
    QUERY_EMBEDDING_PREFIX,
    QWEN3_EMBEDDING_DIMENSION,
    QWEN3_EMBEDDING_MODEL,
    QWEN3_EMBEDDING_PROVIDER_IDS,
    QWEN3_QUERY_POLICY,
    format_qwen3_query,
)
from numpy.typing import NDArray

from cardrag_mcp.quota import validate_byte_limit

EMBEDDING_INPUT_POLICY_VERSION = EMBEDDING_POLICY_VERSION

_QWEN_PROVIDER_NAMES = {
    "deepinfra": "deepinfra",
    "nebius": "nebius",
    "nebiusai": "nebius",
}
MAX_EMBEDDING_RESPONSE_BYTES = 64 * 1024 * 1024
DEFAULT_EMBEDDING_RESPONSE_BYTES = 1024 * 1024


def _response_provider(payload: Mapping[str, Any], response: httpx.Response) -> str | None:
    for candidate in (payload.get("provider_id"), payload.get("provider")):
        if isinstance(candidate, str) and candidate:
            return candidate
    metadata = payload.get("openrouter_metadata")
    if isinstance(metadata, Mapping):
        for key in ("provider_slug", "provider_id", "provider_name", "provider"):
            candidate = metadata.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
    header = response.headers.get("x-openrouter-provider")
    return header if isinstance(header, str) and header else None


def _provider_matches(actual: str, expected: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", actual.casefold())
    return _QWEN_PROVIDER_NAMES.get(normalized) == expected


class EmbeddingUnavailable(RuntimeError):
    """The vector branch cannot run for this request."""


async def _bounded_response_bytes(response: httpx.Response, maximum_bytes: int) -> bytes:
    raw_length = response.headers.get("content-length")
    if raw_length is not None and (not raw_length.isdigit() or int(raw_length) > maximum_bytes):
        raise ValueError("embedding response Content-Length is invalid")
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(chunk) > maximum_bytes - len(body):
            raise ValueError("embedding response exceeds its byte cap")
        body.extend(chunk)
    return bytes(body)


class OpenRouterEmbedder:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        timeout_seconds: float,
        maximum_response_bytes: int = DEFAULT_EMBEDDING_RESPONSE_BYTES,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        validate_byte_limit(maximum_response_bytes, label="maximum embedding response bytes")
        if maximum_response_bytes > MAX_EMBEDDING_RESPONSE_BYTES:
            raise ValueError("maximum embedding response bytes exceeds the hard safety bound")
        self._api_key = api_key
        self._maximum_response_bytes = maximum_response_bytes
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def embed(
        self,
        query: str,
        *,
        provider: str,
        model: str,
        dimension: int = EMBEDDING_DIMENSION,
        query_policy: str = EMBEDDING_POLICY_VERSION,
        provider_id: str | None = None,
    ) -> NDArray[np.float32]:
        if provider.lower() != "openrouter":
            raise EmbeddingUnavailable(
                "active generation requires an unsupported embedding provider"
            )
        if not self._api_key:
            raise EmbeddingUnavailable("OpenRouter API key is not configured")
        if query_policy == EMBEDDING_POLICY_VERSION:
            if dimension != EMBEDDING_DIMENSION or provider_id is not None:
                raise EmbeddingUnavailable("legacy embedding profile is inconsistent")
            formatted_query = QUERY_EMBEDDING_PREFIX + query
        elif query_policy == QWEN3_QUERY_POLICY:
            if (
                dimension != QWEN3_EMBEDDING_DIMENSION
                or model != QWEN3_EMBEDDING_MODEL
                or provider_id not in QWEN3_EMBEDDING_PROVIDER_IDS
            ):
                raise EmbeddingUnavailable("Qwen embedding profile is inconsistent")
            formatted_query = format_qwen3_query(query)
        else:
            raise EmbeddingUnavailable("active generation uses an unsupported query policy")
        request_body: dict[str, Any] = {
            "model": model,
            "input": [formatted_query],
            "dimensions": dimension,
            "encoding_format": "float",
        }
        if provider_id is not None:
            request_body["provider"] = {
                "order": [provider_id],
                "only": [provider_id],
                "allow_fallbacks": False,
                # Live DeepInfra/Nebius endpoint metadata does not advertise
                # the dimensions parameter even though both return the sealed
                # 4,096D result. The response contract below is authoritative.
                "require_parameters": False,
            }
        try:
            async with self._client.stream(
                "POST",
                "embeddings",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=request_body,
            ) as response:
                response.raise_for_status()
                response_body = await _bounded_response_bytes(
                    response,
                    self._maximum_response_bytes,
                )
            payload: Any = json.loads(response_body)
            if not isinstance(payload, dict):
                raise ValueError
            if provider_id is not None:
                actual_model = payload.get("model")
                if not isinstance(actual_model, str) or actual_model.casefold() != model.casefold():
                    raise ValueError
                actual_provider = _response_provider(payload, response)
                if actual_provider is None or not _provider_matches(actual_provider, provider_id):
                    raise ValueError
            data = payload["data"]
            if not isinstance(data, list) or len(data) != 1:
                raise ValueError
            item = data[0]
            if not isinstance(item, dict) or item.get("index", 0) != 0:
                raise ValueError
            vector = np.asarray(item["embedding"], dtype=np.float32)
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise EmbeddingUnavailable("OpenRouter embedding request failed") from exc
        if vector.shape != (dimension,) or not bool(np.isfinite(vector).all()):
            raise EmbeddingUnavailable("OpenRouter returned an invalid embedding")
        if float(np.linalg.norm(vector)) <= 0:
            raise EmbeddingUnavailable("OpenRouter returned a zero embedding")
        return np.asarray(vector / np.linalg.norm(vector), dtype=np.float32)
