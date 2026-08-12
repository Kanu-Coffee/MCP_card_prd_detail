"""Configurable OpenRouter embeddings with strict response validation."""

from __future__ import annotations

import asyncio
import hashlib
import math
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import httpx


class EmbeddingError(RuntimeError):
    pass


class EmbeddingProvider(Protocol):
    provider: str
    model: str
    dimension: int

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def embed_query(self, query: str) -> list[float]: ...


@dataclass(frozen=True, slots=True)
class EmbeddingPolicy:
    version: str = "embedding-input.v1"
    document_prefix: str = "Represent this Korean card-disclosure evidence for retrieval: "
    query_prefix: str = "Retrieve Korean card-disclosure evidence that answers: "


def validate_vectors(vectors: Sequence[Sequence[float]], *, expected_count: int, dimension: int) -> None:
    if len(vectors) != expected_count:
        raise EmbeddingError(f"embedding count {len(vectors)} != {expected_count}")
    for index, vector in enumerate(vectors):
        if len(vector) != dimension:
            raise EmbeddingError(f"embedding {index} dimension {len(vector)} != {dimension}")
        if not all(math.isfinite(float(value)) for value in vector):
            raise EmbeddingError(f"embedding {index} contains a non-finite value")


class OpenRouterEmbeddingProvider:
    provider = "openrouter"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        dimension: int,
        base_url: str = "https://openrouter.ai/api/v1",
        timeout_seconds: float = 60,
        policy: EmbeddingPolicy | None = None,
        maximum_retries: int = 4,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.dimension = dimension
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.policy = policy or EmbeddingPolicy()
        self.maximum_retries = maximum_retries
        self._circuit_open_until = 0.0
        self._failures = 0

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return await self._embed([self.policy.document_prefix + text for text in texts])

    async def embed_query(self, query: str) -> list[float]:
        vectors = await self._embed([self.policy.query_prefix + query])
        return vectors[0]

    async def _embed(self, inputs: Sequence[str]) -> list[list[float]]:
        if time.monotonic() < self._circuit_open_until:
            raise EmbeddingError("embedding circuit is open")
        last_error: Exception | None = None
        for attempt in range(self.maximum_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                    response = await client.post(
                        self.base_url + "/embeddings",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        json={"model": self.model, "input": list(inputs), "dimensions": self.dimension},
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    retry_after = min(float(response.headers.get("retry-after", 0) or 0), 30.0)
                    raise _RetryableEmbeddingError(retry_after, response.status_code)
                response.raise_for_status()
                payload = response.json()
                rows = sorted(payload.get("data") or [], key=lambda row: int(row.get("index", -1)))
                vectors = [list(map(float, row["embedding"])) for row in rows]
                validate_vectors(vectors, expected_count=len(inputs), dimension=self.dimension)
                self._failures = 0
                return vectors
            except (_RetryableEmbeddingError, httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = exc
                self._failures += 1
                if attempt >= self.maximum_retries:
                    break
                delay = exc.retry_after if isinstance(exc, _RetryableEmbeddingError) else 0.0
                await asyncio.sleep(max(delay, min(8.0, 0.5 * 2**attempt)))
            except (KeyError, TypeError, ValueError, httpx.HTTPStatusError) as exc:
                raise EmbeddingError("embedding provider returned an invalid response") from exc
        if self._failures >= 5:
            self._circuit_open_until = time.monotonic() + 30.0
        raise EmbeddingError("embedding provider unavailable after bounded retries") from last_error


class _RetryableEmbeddingError(RuntimeError):
    def __init__(self, retry_after: float, status_code: int) -> None:
        super().__init__(f"retryable embedding status {status_code}")
        self.retry_after = retry_after


class FakeEmbeddingProvider:
    provider = "fake"

    def __init__(
        self, *, model: str = "fake-embedding-v1", dimension: int = 32, fail_queries: bool = False
    ) -> None:
        self.model = model
        self.dimension = dimension
        self.fail_queries = fail_queries
        self.query_calls = 0
        self.document_calls = 0

    def _vector(self, text: str) -> list[float]:
        digest = hashlib.shake_256(text.encode()).digest(self.dimension * 4)
        values = [
            int.from_bytes(digest[index : index + 4], "big") / (2**32) - 0.5
            for index in range(0, len(digest), 4)
        ]
        norm = math.sqrt(sum(value * value for value in values)) or 1
        return [value / norm for value in values]

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.document_calls += 1
        return [self._vector("document:" + text) for text in texts]

    async def embed_query(self, query: str) -> list[float]:
        self.query_calls += 1
        if self.fail_queries:
            raise EmbeddingError("injected query embedding failure")
        return self._vector("query:" + query)
