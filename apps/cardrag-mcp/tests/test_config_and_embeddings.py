from __future__ import annotations

import json
from pathlib import Path

import httpx
import numpy as np
import pytest
from cardrag_core import QWEN3_EMBEDDING_MODEL, QWEN3_QUERY_POLICY, format_qwen3_query
from fastapi import FastAPI

import cardrag_mcp.main as main_module
from cardrag_mcp.config import Settings
from cardrag_mcp.embeddings import QUERY_EMBEDDING_PREFIX, EmbeddingUnavailable, OpenRouterEmbedder
from cardrag_mcp.repository import ServingRepository

AUTH_VALUE = "test-static-bearer-token-000000000000"


class _ChunkedResponse(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'{"provider_secret":"'
        yield b"x" * 128
        yield b'"}'


def test_exact_deployment_env_names_and_file_backed_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text(AUTH_VALUE + "\n", encoding="utf-8")
    monkeypatch.setenv("CARDRAG_MCP_BEARER_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("CARDRAG_MCP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CARDRAG_MCP_HOST", "127.0.0.2")
    monkeypatch.setenv("CARDRAG_MCP_PORT", "8123")
    monkeypatch.setenv("CARDRAG_MCP_UPDATE_INTERVAL_SECONDS", "301")
    monkeypatch.setenv("CARDRAG_MCP_MAX_VECTOR_BYTES", str(2 * 1024 * 1024))
    monkeypatch.setenv("CARDRAG_MCP_RETAIN_GENERATIONS", "4")
    settings = Settings(environment="test")
    assert settings.bearer_token_value() == AUTH_VALUE
    assert settings.mcp_host == "127.0.0.2"
    assert settings.mcp_port == 8123
    assert settings.mcp_update_interval_seconds == 301
    assert settings.mcp_max_vector_bytes == 2 * 1024 * 1024
    assert settings.resident_vector_limit_bytes() == 2 * 1024 * 1024
    assert settings.mcp_max_vector_sidecar_bytes == 16 * 1024**3
    assert settings.mcp_retain_generations == 4


def test_v5_sidecar_and_resident_vector_caps_have_independent_env_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CARDRAG_MCP_MAX_VECTOR_BYTES", str(2 * 1024 * 1024))
    monkeypatch.setenv("CARDRAG_MCP_MAX_VECTOR_SIDECAR_BYTES", str(8 * 1024 * 1024))
    monkeypatch.setenv("CARDRAG_MCP_MAX_RESIDENT_VECTOR_BYTES", str(3 * 1024 * 1024))

    settings = Settings(environment="test", mcp_bearer_token=AUTH_VALUE)

    assert settings.mcp_max_vector_bytes == 2 * 1024 * 1024
    assert settings.mcp_max_vector_sidecar_bytes == 8 * 1024 * 1024
    assert settings.mcp_max_resident_vector_bytes == 3 * 1024 * 1024
    assert settings.resident_vector_limit_bytes() == 3 * 1024 * 1024


def test_storage_audit_and_response_cap_defaults_are_independent() -> None:
    settings = Settings(environment="test", mcp_bearer_token=AUTH_VALUE)

    assert settings.mcp_max_serving_database_bytes == 4 * 1024**3
    assert settings.mcp_max_generation_download_bytes == 32 * 1024**3
    assert settings.mcp_max_state_bytes == 64 * 1024**3
    assert settings.mcp_reserved_free_space_bytes == 2 * 1024**3
    assert settings.mcp_exhaustive_audit_max_jobs == 32
    assert settings.mcp_exhaustive_audit_max_total_bytes == 2 * 1024**3
    assert settings.mcp_exhaustive_audit_max_artifact_bytes == 256 * 1024**2
    assert settings.mcp_reranker_audit_max_jobs == 1024
    assert settings.mcp_reranker_audit_max_total_bytes == 512 * 1024**2
    assert settings.mcp_reranker_audit_max_artifact_bytes == 8 * 1024**2
    assert settings.embedding_max_response_bytes == 1024**2
    assert settings.reranker_shadow_max_response_bytes == 1024**2


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("mcp_max_state_bytes", True),
        ("mcp_reserved_free_space_bytes", -1),
        ("mcp_max_generation_download_bytes", 1 << 80),
        ("mcp_exhaustive_audit_max_jobs", False),
        ("mcp_reranker_audit_max_total_bytes", -1),
        ("embedding_max_response_bytes", True),
        ("reranker_shadow_max_response_bytes", 1 << 80),
    ),
)
def test_byte_and_count_caps_reject_bool_negative_and_overflow(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValueError):
        Settings(
            environment="test",
            mcp_bearer_token=AUTH_VALUE,
            **{field: value},
        )


def test_channel_and_retention_defaults_are_safe() -> None:
    settings = Settings(environment="test", mcp_bearer_token=AUTH_VALUE)
    assert settings.channel == "stable"
    assert settings.mcp_retain_generations == 2

    candidate = Settings(
        environment="test",
        mcp_bearer_token=AUTH_VALUE,
        channel="candidate-v1.0.9",
    )
    assert candidate.channel == "candidate-v1.0.9"
    with pytest.raises(ValueError):
        Settings(
            environment="test",
            mcp_bearer_token=AUTH_VALUE,
            channel="../stable",
        )


def test_reranker_shadow_is_disabled_and_candidate_only() -> None:
    stable = Settings(environment="test", mcp_bearer_token=AUTH_VALUE)
    assert stable.reranker_shadow_enabled is False
    assert stable.reranker_shadow_model == "qwen/qwen3-reranker-8b"
    assert stable.reranker_shadow_provider_id == "fireworks"
    assert stable.reranker_shadow_max_candidates == 64

    with pytest.raises(ValueError, match="candidate-v1.0.11"):
        Settings(
            environment="test",
            mcp_bearer_token=AUTH_VALUE,
            openrouter_api_key="secret",
            reranker_shadow_enabled=True,
        )
    with pytest.raises(ValueError, match="OpenRouter API key"):
        Settings(
            environment="test",
            channel="candidate-v1.0.11",
            mcp_bearer_token=AUTH_VALUE,
            reranker_shadow_enabled=True,
        )

    candidate = Settings(
        environment="test",
        channel="candidate-v1.0.11",
        mcp_bearer_token=AUTH_VALUE,
        openrouter_api_key="secret",
        reranker_shadow_enabled=True,
        reranker_shadow_max_candidates=32,
    )
    assert candidate.reranker_shadow_enabled is True
    assert candidate.reranker_shadow_max_candidates == 32


@pytest.mark.asyncio
async def test_main_wires_candidate_reranker_shadow_and_owned_clients(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, ServingRepository] = {}

    def capture_app(
        repository: ServingRepository,
        *_args: object,
        **_kwargs: object,
    ) -> FastAPI:
        captured["repository"] = repository
        return FastAPI()

    monkeypatch.setattr(main_module, "build_app", capture_app)
    settings = Settings(
        environment="test",
        channel="candidate-v1.0.11",
        mcp_bearer_token=AUTH_VALUE,
        mcp_state_dir=tmp_path / "state",
        openrouter_api_key="secret",
        reranker_shadow_enabled=True,
        reranker_shadow_max_candidates=17,
    )

    app = main_module.create_app(settings)
    repository = captured["repository"]
    assert isinstance(app, FastAPI)
    assert repository.reranker_shadow is not None
    assert repository.exact.reranker_shadow is repository.reranker_shadow
    assert repository.reranker_shadow.maximum_candidates == 17
    await repository.reranker_shadow.close()
    await repository.embedder.close()


def test_production_webdav_rejects_http_query_and_embedded_credentials() -> None:
    base = {
        "mcp_bearer_token": AUTH_VALUE,
        "webdav_username": "reader",
        "webdav_password": "password",
    }
    for url in (
        "http://dav.example/cardrag",
        "https://dav.example/cardrag?secret=x",
        "https://reader:password@dav.example/cardrag",
    ):
        with pytest.raises(ValueError):
            Settings(**base, webdav_base_url=url)


def test_bearer_token_strength_and_production_public_url_policy() -> None:
    for token in ("", " " * 32, "too-short", "x" * 31, "x" * 31 + " "):
        with pytest.raises(ValueError):
            Settings(environment="test", mcp_bearer_token=token)

    assert Settings(mcp_bearer_token=AUTH_VALUE).mcp_public_base_url.host == "127.0.0.1"
    with pytest.raises(ValueError, match="HTTPS"):
        Settings(
            mcp_bearer_token=AUTH_VALUE,
            mcp_public_base_url="http://mcp.example.test",
        )
    secure = Settings(
        mcp_bearer_token=AUTH_VALUE,
        mcp_public_base_url="https://mcp.example.test",
    )
    assert secure.mcp_public_base_url.scheme == "https"


def test_production_openrouter_url_cannot_expose_api_credentials() -> None:
    for url in (
        "http://openrouter.example/api/v1",
        "https://user:secret@openrouter.example/api/v1",
        "https://openrouter.example/api/v1?token=secret",
        "https://openrouter.example/api/v1#fragment",
    ):
        with pytest.raises(ValueError):
            Settings(mcp_bearer_token=AUTH_VALUE, openrouter_base_url=url)

    settings = Settings(
        mcp_bearer_token=AUTH_VALUE,
        openrouter_base_url="https://openrouter.example/api/v1",
    )
    assert settings.openrouter_base_url.scheme == "https"


@pytest.mark.asyncio
async def test_openrouter_uses_fixed_query_policy_dimension_and_normalizes() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers["authorization"]
        captured["payload"] = json.loads(request.content)
        vector = [0.0] * 1536
        vector[0] = 2.0
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": vector}]})

    client = httpx.AsyncClient(
        base_url="https://openrouter.example/api/v1/",
        transport=httpx.MockTransport(handler),
    )
    embedder = OpenRouterEmbedder(
        base_url="https://openrouter.example/api/v1",
        api_key="secret-key",
        timeout_seconds=10,
        client=client,
    )
    result = await embedder.embed(
        "airport lounge",
        provider="openrouter",
        model="openai/text-embedding-3-small",
    )
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload == {
        "model": "openai/text-embedding-3-small",
        "input": [QUERY_EMBEDDING_PREFIX + "airport lounge"],
        "dimensions": 1536,
        "encoding_format": "float",
    }
    assert captured["authorization"] == "Bearer secret-key"
    assert result.dtype == np.float32
    assert np.linalg.norm(result) == pytest.approx(1.0)
    await client.aclose()


@pytest.mark.asyncio
async def test_qwen_query_is_4096d_instruction_formatted_and_single_provider() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        vector = [0.0] * 4096
        vector[7] = 3.0
        return httpx.Response(
            200,
            json={
                # Live OpenRouter canonicalizes both values this way.
                "model": "Qwen/Qwen3-Embedding-8B",
                "provider": "DeepInfra",
                "data": [{"index": 0, "embedding": vector}],
            },
        )

    client = httpx.AsyncClient(
        base_url="https://openrouter.example/api/v1/",
        transport=httpx.MockTransport(handler),
    )
    embedder = OpenRouterEmbedder(
        base_url="https://openrouter.example/api/v1",
        api_key="secret-key",
        timeout_seconds=10,
        client=client,
    )
    result = await embedder.embed(
        "전월 실적 제외 대상",
        provider="openrouter",
        model=QWEN3_EMBEDDING_MODEL,
        dimension=4096,
        query_policy=QWEN3_QUERY_POLICY,
        provider_id="deepinfra",
    )

    assert captured["payload"] == {
        "model": QWEN3_EMBEDDING_MODEL,
        "input": [format_qwen3_query("전월 실적 제외 대상")],
        "dimensions": 4096,
        "encoding_format": "float",
        "provider": {
            "order": ["deepinfra"],
            "only": ["deepinfra"],
            "allow_fallbacks": False,
            "require_parameters": False,
        },
    }
    assert result.shape == (4096,)
    assert np.linalg.norm(result) == pytest.approx(1.0)
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_model", "response_provider"),
    (
        ("other/model", "DeepInfra"),
        ("Qwen/Qwen3-Embedding-8B", "Nebius"),
        ("Qwen/Qwen3-Embedding-8B", None),
    ),
)
async def test_qwen_query_rejects_unbound_model_or_provider(
    response_model: str,
    response_provider: str | None,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        vector = [0.0] * 4096
        vector[0] = 1.0
        payload: dict[str, object] = {
            "model": response_model,
            "data": [{"index": 0, "embedding": vector}],
        }
        if response_provider is not None:
            payload["provider"] = response_provider
        return httpx.Response(200, json=payload)

    client = httpx.AsyncClient(
        base_url="https://openrouter.example/api/v1/",
        transport=httpx.MockTransport(handler),
    )
    embedder = OpenRouterEmbedder(
        base_url="https://openrouter.example/api/v1",
        api_key="secret-key",
        timeout_seconds=10,
        client=client,
    )
    with pytest.raises(EmbeddingUnavailable, match="OpenRouter embedding request failed"):
        await embedder.embed(
            "전월 실적 제외 대상",
            provider="openrouter",
            model=QWEN3_EMBEDDING_MODEL,
            dimension=4096,
            query_policy=QWEN3_QUERY_POLICY,
            provider_id="deepinfra",
        )
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("use_content_length", (True, False))
async def test_embedding_response_cap_stops_before_json_parsing(
    use_content_length: bool,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        if use_content_length:
            return httpx.Response(200, content=b"x" * 256)
        return httpx.Response(200, stream=_ChunkedResponse())

    client = httpx.AsyncClient(
        base_url="https://openrouter.example/api/v1/",
        transport=httpx.MockTransport(handler),
    )
    embedder = OpenRouterEmbedder(
        base_url="https://openrouter.example/api/v1",
        api_key="secret-key",
        timeout_seconds=10,
        maximum_response_bytes=64,
        client=client,
    )

    with pytest.raises(
        EmbeddingUnavailable, match="OpenRouter embedding request failed"
    ) as captured:
        await embedder.embed(
            "전월 실적 제외 대상",
            provider="openrouter",
            model=QWEN3_EMBEDDING_MODEL,
            dimension=4096,
            query_policy=QWEN3_QUERY_POLICY,
            provider_id="deepinfra",
        )

    assert "provider_secret" not in str(captured.value)
    await client.aclose()
