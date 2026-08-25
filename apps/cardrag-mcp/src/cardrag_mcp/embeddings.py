"""Bounded OpenRouter query embedding client."""

from __future__ import annotations

from typing import Any

import httpx
import numpy as np
from cardrag_core import (
    EMBEDDING_DIMENSION,
    EMBEDDING_POLICY_VERSION,
    QUERY_EMBEDDING_PREFIX,
)
from numpy.typing import NDArray

EMBEDDING_INPUT_POLICY_VERSION = EMBEDDING_POLICY_VERSION


class EmbeddingUnavailable(RuntimeError):
    """The vector branch cannot run for this request."""


class OpenRouterEmbedder:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        timeout_seconds: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def embed(self, query: str, *, provider: str, model: str) -> NDArray[np.float32]:
        if provider.lower() != "openrouter":
            raise EmbeddingUnavailable(
                "active generation requires an unsupported embedding provider"
            )
        if not self._api_key:
            raise EmbeddingUnavailable("OpenRouter API key is not configured")
        try:
            response = await self._client.post(
                "embeddings",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "input": [QUERY_EMBEDDING_PREFIX + query],
                    "dimensions": EMBEDDING_DIMENSION,
                    "encoding_format": "float",
                },
            )
            response.raise_for_status()
            payload: Any = response.json()
            data = payload["data"]
            if not isinstance(data, list) or len(data) != 1:
                raise ValueError
            item = data[0]
            if not isinstance(item, dict) or item.get("index", 0) != 0:
                raise ValueError
            vector = np.asarray(item["embedding"], dtype=np.float32)
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise EmbeddingUnavailable("OpenRouter embedding request failed") from exc
        if vector.shape != (EMBEDDING_DIMENSION,) or not bool(np.isfinite(vector).all()):
            raise EmbeddingUnavailable("OpenRouter returned an invalid embedding")
        if float(np.linalg.norm(vector)) <= 0:
            raise EmbeddingUnavailable("OpenRouter returned a zero embedding")
        return np.asarray(vector / np.linalg.norm(vector), dtype=np.float32)
