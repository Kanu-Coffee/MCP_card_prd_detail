from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from cardrag_core.embedding import (
    QWEN3_EMBEDDING_DIMENSION,
    QWEN3_EMBEDDING_MODEL,
    format_qwen3_query,
)

from cardrag_worker.embedding_v5 import (
    KOREAN_PREFLIGHT_SAMPLES,
    EmbeddingV5Error,
    OpenRouterEndpointMetadata,
    OpenRouterQwenEmbeddingProviderV5,
    QwenEmbeddingProfileV5,
    embedding_cache_key,
    fetch_openrouter_qwen_endpoints,
    parse_openrouter_endpoint_metadata,
    preflight_openrouter_qwen_providers,
    select_quality_endpoint,
)

BASE_URL = "https://mock.openrouter.test/api/v1"


class _ChunkedResponse(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'{"provider_secret":"'
        yield b"x" * 2048
        yield b'"}'


def _endpoint(
    provider_id: str,
    *,
    quantization: str = "bf16",
    maximum_tokens: int = 8192,
    declares_dimensions: bool = True,
) -> dict[str, Any]:
    display = "DeepInfra" if provider_id == "deepinfra" else "Nebius"
    return {
        "name": f"{display}: Qwen3 Embedding",
        "provider_name": display,
        "tag": provider_id,
        "model_id": QWEN3_EMBEDDING_MODEL,
        "quantization": quantization,
        "context_length": maximum_tokens,
        "max_prompt_tokens": maximum_tokens,
        "supported_parameters": (
            ["encoding_format", "dimensions"] if declares_dimensions else ["encoding_format"]
        ),
    }


def _metadata_payload(*endpoints: dict[str, Any]) -> dict[str, Any]:
    return {"data": {"id": QWEN3_EMBEDDING_MODEL, "endpoints": list(endpoints)}}


def _profile(provider_id: str = "deepinfra", *, maximum_tokens: int = 8192) -> QwenEmbeddingProfileV5:
    endpoint = OpenRouterEndpointMetadata(
        model=QWEN3_EMBEDDING_MODEL,
        provider_id=provider_id,  # type: ignore[arg-type]
        provider_name="DeepInfra" if provider_id == "deepinfra" else "Nebius",
        endpoint_name=f"{provider_id}: Qwen3 Embedding",
        quantization="bf16",
        maximum_tokens=maximum_tokens,
        supported_parameters=("dimensions", "encoding_format"),
        metadata_sha256="a" * 64,
    )
    return QwenEmbeddingProfileV5.from_endpoint(endpoint)


def _unit_vector(provider_id: str = "deepinfra") -> list[float]:
    vector = [0.0] * QWEN3_EMBEDDING_DIMENSION
    if provider_id == "deepinfra":
        vector[0] = 1.0
    else:
        vector[0] = 0.8
        vector[1] = 0.6
    return vector


def test_metadata_parser_orders_allowlist_and_rejects_fp8_or_ambiguous_routes() -> None:
    endpoints = parse_openrouter_endpoint_metadata(
        _metadata_payload(_endpoint("nebius"), _endpoint("deepinfra"))
    )
    assert [row.provider_id for row in endpoints] == ["deepinfra", "nebius"]
    assert select_quality_endpoint(endpoints, provider_id="deepinfra").maximum_tokens == 8192

    live_shape = parse_openrouter_endpoint_metadata(
        _metadata_payload(
            _endpoint("deepinfra", declares_dimensions=False),
            _endpoint("nebius", declares_dimensions=False),
        )
    )
    assert select_quality_endpoint(live_shape, provider_id="deepinfra").provider_id == "deepinfra"

    null_prompt_limit = _endpoint("deepinfra", maximum_tokens=32768)
    null_prompt_limit["max_prompt_tokens"] = None
    parsed_null_limit = parse_openrouter_endpoint_metadata(
        _metadata_payload(null_prompt_limit, _endpoint("nebius", maximum_tokens=32000))
    )
    assert select_quality_endpoint(parsed_null_limit, provider_id="deepinfra").maximum_tokens == 32768

    fp8 = parse_openrouter_endpoint_metadata(
        _metadata_payload(_endpoint("deepinfra", quantization="fp8"), _endpoint("nebius"))
    )
    with pytest.raises(EmbeddingV5Error, match="FP8"):
        select_quality_endpoint(fp8, provider_id="deepinfra")

    ambiguous = parse_openrouter_endpoint_metadata(
        _metadata_payload(_endpoint("deepinfra"), _endpoint("deepinfra"), _endpoint("nebius"))
    )
    with pytest.raises(EmbeddingV5Error, match="ambiguous"):
        select_quality_endpoint(ambiguous, provider_id="deepinfra")


def test_profile_and_cache_identity_change_with_routed_provider() -> None:
    deepinfra = _profile("deepinfra")
    nebius = _profile("nebius")
    assert deepinfra.profile_id != nebius.profile_id
    assert deepinfra.cache_namespace != nebius.cache_namespace

    deep_key, deep_input = embedding_cache_key(
        deepinfra,
        kind="document",
        formatted_input="원문 그대로",
    )
    nebius_key, nebius_input = embedding_cache_key(
        nebius,
        kind="document",
        formatted_input="원문 그대로",
    )
    assert deep_input == nebius_input
    assert deep_key != nebius_key


@pytest.mark.asyncio
async def test_provider_sends_raw_documents_exact_queries_and_pinned_route() -> None:
    request_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        request_bodies.append(body)
        return httpx.Response(
            200,
            json={
                # This is the canonical capitalization returned by the live
                # OpenRouter Qwen endpoint for the lower-case request slug.
                "model": "Qwen/Qwen3-Embedding-8B",
                "provider": "deepinfra",
                "data": [
                    {"index": index, "embedding": _unit_vector()}
                    for index, _value in enumerate(body["input"])
                ],
            },
            request=request,
        )

    provider = OpenRouterQwenEmbeddingProviderV5(
        api_key="injected-test-credential",
        profile=_profile(),
        token_counter=lambda text: len(text),
        base_url=BASE_URL,
        transport=httpx.MockTransport(handler),
    )
    document = "전월 실적 제외 원문"
    query = "상품권은 실적에 포함되나요?"
    assert len(await provider.embed_documents([document])) == 1
    assert len(await provider.embed_queries([query])) == 1

    assert request_bodies[0]["input"] == [document]
    assert request_bodies[1]["input"] == [format_qwen3_query(query)]
    for body in request_bodies:
        assert body["dimensions"] == 4096
        assert body["encoding_format"] == "float"
        assert "truncation" not in body
        assert body["provider"] == {
            "order": ["deepinfra"],
            "only": ["deepinfra"],
            "allow_fallbacks": False,
            "require_parameters": False,
        }


@pytest.mark.asyncio
async def test_provider_rejects_oversize_before_http_and_never_truncates() -> None:
    def unexpected(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("oversize input must fail before network I/O")

    provider = OpenRouterQwenEmbeddingProviderV5(
        api_key="injected-test-credential",
        profile=_profile(maximum_tokens=2),
        token_counter=lambda text: len(text),
        base_url=BASE_URL,
        transport=httpx.MockTransport(unexpected),
    )
    with pytest.raises(EmbeddingV5Error, match="truncation is forbidden"):
        await provider.embed_documents(["세글자"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_provider", "vector", "message"),
    (
        ("nebius", _unit_vector(), "provider"),
        ("deepinfra", [1.0] * (QWEN3_EMBEDDING_DIMENSION - 1), "dimension"),
        ("deepinfra", [float("nan")] + [0.0] * (QWEN3_EMBEDDING_DIMENSION - 1), "non-finite"),
        ("deepinfra", ["1.0"] + [0.0] * (QWEN3_EMBEDDING_DIMENSION - 1), "non-numeric"),
        ("deepinfra", [0.0] * QWEN3_EMBEDDING_DIMENSION, "norm"),
    ),
)
async def test_provider_rejects_wrong_route_dimension_and_norm(
    response_provider: str,
    vector: list[object],
    message: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "model": QWEN3_EMBEDDING_MODEL,
                    "provider": response_provider,
                    "data": [{"index": 0, "embedding": vector}],
                }
            ).encode(),
            headers={"content-type": "application/json"},
            request=request,
        )

    provider = OpenRouterQwenEmbeddingProviderV5(
        api_key="injected-test-credential",
        profile=_profile(),
        token_counter=lambda _text: 1,
        base_url=BASE_URL,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(EmbeddingV5Error, match=message):
        await provider.embed_documents(["원문"])


@pytest.mark.asyncio
@pytest.mark.parametrize("use_content_length", (True, False))
async def test_worker_embedding_response_cap_is_incremental_and_secret_safe(
    use_content_length: bool,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if use_content_length:
            return httpx.Response(200, content=b"x" * 2048, request=request)
        return httpx.Response(200, stream=_ChunkedResponse(), request=request)

    provider = OpenRouterQwenEmbeddingProviderV5(
        api_key="injected-test-credential",
        profile=_profile(),
        token_counter=lambda _text: 1,
        base_url=BASE_URL,
        maximum_response_bytes=1024,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(EmbeddingV5Error, match="byte cap|Content-Length") as captured:
        await provider.embed_documents(["원문"])

    assert "provider_secret" not in str(captured.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("use_content_length", (True, False))
async def test_worker_metadata_response_cap_is_incremental_and_secret_safe(
    use_content_length: bool,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if use_content_length:
            return httpx.Response(200, content=b"x" * 2048, request=request)
        return httpx.Response(200, stream=_ChunkedResponse(), request=request)

    with pytest.raises(EmbeddingV5Error, match="byte cap|Content-Length") as captured:
        await fetch_openrouter_qwen_endpoints(
            api_key="injected-test-credential",
            base_url=BASE_URL,
            maximum_response_bytes=1024,
            transport=httpx.MockTransport(handler),
        )

    assert "provider_secret" not in str(captured.value)


@pytest.mark.asyncio
async def test_credential_injected_preflight_compares_20_plus_korean_samples() -> None:
    assert len(KOREAN_PREFLIGHT_SAMPLES) >= 20
    seen_providers: list[str] = []
    seen_authorizations: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_authorizations.append(request.headers["authorization"])
        if request.method == "GET":
            assert request.url.path.endswith("/models/qwen/qwen3-embedding-8b/endpoints")
            return httpx.Response(
                200,
                json=_metadata_payload(_endpoint("nebius"), _endpoint("deepinfra")),
                request=request,
            )
        body = json.loads(request.content)
        provider_id = body["provider"]["order"][0]
        seen_providers.append(provider_id)
        assert body["provider"]["only"] == [provider_id]
        assert body["provider"]["allow_fallbacks"] is False
        assert body["provider"]["require_parameters"] is False
        assert body["input"] == list(KOREAN_PREFLIGHT_SAMPLES)
        vector = _unit_vector(provider_id)
        return httpx.Response(
            200,
            json={
                "model": QWEN3_EMBEDDING_MODEL,
                "provider": provider_id,
                # Reversed response rows exercise exact index rebinding.
                "data": [
                    {"index": index, "embedding": vector} for index in reversed(range(len(body["input"])))
                ],
            },
            request=request,
        )

    report = await preflight_openrouter_qwen_providers(
        api_key="injected-test-credential",
        token_counter=lambda text: len(text),
        base_url=BASE_URL,
        transport=httpx.MockTransport(handler),
    )
    assert seen_providers == ["deepinfra", "deepinfra", "nebius", "nebius"]
    assert set(seen_authorizations) == {"Bearer injected-test-credential"}
    assert [row.profile.provider_id for row in report.providers] == ["deepinfra", "nebius"]
    assert all(row.sample_count == len(KOREAN_PREFLIGHT_SAMPLES) for row in report.providers)
    assert all(row.minimum_repeat_cosine == pytest.approx(1.0) for row in report.providers)
    assert report.minimum_cross_provider_cosine == pytest.approx(0.8)
