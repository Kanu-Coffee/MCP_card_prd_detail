from __future__ import annotations

import json
from pathlib import Path

import httpx
import numpy as np
import pytest

from cardrag_mcp.config import Settings
from cardrag_mcp.embeddings import QUERY_EMBEDDING_PREFIX, OpenRouterEmbedder

AUTH_VALUE = "test-static-bearer-token-000000000000"


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
    assert settings.mcp_retain_generations == 4


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
