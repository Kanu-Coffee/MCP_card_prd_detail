from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from cardrag.service.auth import KeycloakJWTVerifier, protected_resource_metadata_url


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _integer(value: int) -> str:
    return _b64(value.to_bytes((value.bit_length() + 7) // 8, "big"))


def _jwk(private_key: rsa.RSAPrivateKey, kid: str = "key-1") -> dict[str, str]:
    public = private_key.public_key().public_numbers()
    return {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": kid,
        "n": _integer(public.n),
        "e": _integer(public.e),
    }


def _jwt(
    private_key: rsa.RSAPrivateKey,
    claims: dict[str, Any],
    *,
    kid: str = "key-1",
    algorithm: str = "RS256",
) -> str:
    header = _b64(
        json.dumps(
            {"alg": algorithm, "kid": kid, "typ": "JWT"},
            separators=(",", ":"),
        ).encode()
    )
    payload = _b64(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode("ascii")
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{_b64(signature)}"


@pytest.fixture(scope="module")
def private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.mark.asyncio
async def test_keycloak_verifier_accepts_valid_scoped_token_and_caches_jwks(
    private_key: rsa.RSAPrivateKey,
) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"keys": [_jwk(private_key)]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        verifier = KeycloakJWTVerifier(
            issuer="https://id.example/realms/cardrag",
            audience="cardrag-mcp",
            cache_seconds=300,
            client=client,
            clock=lambda: 1_000,
        )
        claims = {
            "iss": "https://id.example/realms/cardrag",
            "aud": ["account", "cardrag-mcp"],
            "azp": "codex-client",
            "sub": "user-123",
            "scope": "openid search source_pdf",
            "iat": 900,
            "nbf": 900,
            "exp": 1_100,
        }
        first = await verifier.verify_token(_jwt(private_key, claims))
        second = await verifier.verify_token(_jwt(private_key, claims))

    assert first is not None
    assert first.client_id == "codex-client"
    assert first.scopes == ["openid", "search", "source_pdf"]
    assert second is not None
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("claim_change", "algorithm"),
    [
        ({"aud": "another-service"}, "RS256"),
        ({"iss": "https://evil.example/realms/cardrag"}, "RS256"),
        ({"exp": 900}, "RS256"),
        ({"nbf": 1_100}, "RS256"),
        ({}, "HS256"),
    ],
)
async def test_keycloak_verifier_rejects_invalid_claims_and_algorithm(
    private_key: rsa.RSAPrivateKey,
    claim_change: dict[str, Any],
    algorithm: str,
) -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"keys": [_jwk(private_key)]})
    )
    async with httpx.AsyncClient(transport=transport) as client:
        verifier = KeycloakJWTVerifier(
            issuer="https://id.example/realms/cardrag",
            audience="cardrag-mcp",
            client=client,
            clock=lambda: 1_000,
        )
        claims = {
            "iss": "https://id.example/realms/cardrag",
            "aud": "cardrag-mcp",
            "client_id": "client",
            "scope": "search",
            "iat": 900,
            "exp": 1_100,
            **claim_change,
        }
        assert await verifier.verify_token(
            _jwt(private_key, claims, algorithm=algorithm)
        ) is None


@pytest.mark.asyncio
async def test_unknown_kid_is_rejected_after_rotation_refresh(
    private_key: rsa.RSAPrivateKey,
) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"keys": [_jwk(private_key)]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        verifier = KeycloakJWTVerifier(
            issuer="https://id.example/realms/cardrag",
            audience="cardrag-mcp",
            client=client,
            clock=lambda: 1_000,
        )
        claims = {
            "iss": "https://id.example/realms/cardrag",
            "aud": "cardrag-mcp",
            "client_id": "client",
            "scope": "search",
            "exp": 1_100,
        }
        assert await verifier.verify_token(_jwt(private_key, claims, kid="unknown")) is None
    assert calls == 1


def test_protected_resource_metadata_url_keeps_resource_path() -> None:
    assert protected_resource_metadata_url("https://mcp.example/mcp") == (
        "https://mcp.example/.well-known/oauth-protected-resource/mcp"
    )
