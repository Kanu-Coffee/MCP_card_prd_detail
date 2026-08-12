from __future__ import annotations

import json
import math
import secrets

import httpx
import pytest
import respx

from cardrag.search.embeddings import (
    EmbeddingError,
    FakeEmbeddingProvider,
    OpenRouterEmbeddingProvider,
    validate_vectors,
)


@pytest.mark.parametrize(
    ("vectors", "expected_count", "dimension", "message"),
    [
        ([[0.0, 1.0]], 2, 2, "embedding count"),
        ([[0.0]], 1, 2, "dimension"),
        ([[0.0, float("nan")]], 1, 2, "non-finite"),
        ([[0.0, float("inf")]], 1, 2, "non-finite"),
    ],
)
def test_vector_validation_rejects_count_dimension_and_non_finite_values(
    vectors: list[list[float]], expected_count: int, dimension: int, message: str
) -> None:
    with pytest.raises(EmbeddingError, match=message):
        validate_vectors(vectors, expected_count=expected_count, dimension=dimension)


async def test_fake_embeddings_have_exact_count_dimension_and_finite_values() -> None:
    provider = FakeEmbeddingProvider(dimension=8)
    texts = ["전월실적 30만원", "실적 제외 상품권"]

    first = await provider.embed_documents(texts)
    second = await provider.embed_documents(texts)
    query = await provider.embed_query("대중교통 할인")

    assert first == second
    assert len(first) == len(texts)
    assert all(len(vector) == provider.dimension for vector in first)
    assert all(math.isfinite(value) for vector in first for value in vector)
    assert len(query) == provider.dimension
    assert provider.document_calls == 2
    assert provider.query_calls == 1


@respx.mock
async def test_openrouter_retry_is_bounded_and_preserves_input_order(monkeypatch: pytest.MonkeyPatch) -> None:
    endpoint = "https://embedding.fixture/v1/embeddings"
    route = respx.post(endpoint).mock(
        side_effect=[
            httpx.Response(503, headers={"retry-after": "0"}),
            httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 1, "embedding": [0.0, 1.0, 0.0]},
                        {"index": 0, "embedding": [1.0, 0.0, 0.0]},
                    ]
                },
            ),
        ]
    )
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("cardrag.search.embeddings.asyncio.sleep", fake_sleep)
    provider = OpenRouterEmbeddingProvider(
        api_key=secrets.token_hex(16),
        model="fixture-embedding",
        dimension=3,
        base_url="https://embedding.fixture/v1",
        maximum_retries=1,
    )

    vectors = await provider.embed_documents(["첫 번째", "두 번째"])

    assert vectors == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
    assert route.call_count == 2
    assert sleeps == [0.5]
    request_payload = json.loads(route.calls.last.request.content)
    assert request_payload["dimensions"] == 3
    assert request_payload["input"] == [
        provider.policy.document_prefix + "첫 번째",
        provider.policy.document_prefix + "두 번째",
    ]
