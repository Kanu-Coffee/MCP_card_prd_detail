"""Keycloak bearer-token verification without token introspection.

The verifier accepts signed access JWTs only.  Refresh-token rotation belongs
to the MCP client/Keycloak OAuth flow; this resource server never receives or
stores refresh tokens.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import inspect
import json
import math
import re
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from mcp.server.auth.provider import AccessToken, TokenVerifier
from starlette.requests import Request

_JWT_SEGMENT = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_JWT_BYTES = 32 * 1024
_MAX_JWKS_BYTES = 1024 * 1024


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _decode_json_segment(segment: str) -> dict[str, Any]:
    raw = _decode_base64url(segment)
    value = json.loads(
        raw,
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
    )
    if not isinstance(value, dict):
        raise ValueError("JWT segment must be a JSON object")
    return value


def _decode_base64url(value: str) -> bytes:
    if not value or _JWT_SEGMENT.fullmatch(value) is None:
        raise ValueError("invalid base64url")
    padding_length = (-len(value)) % 4
    try:
        return base64.b64decode(
            value + ("=" * padding_length),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid base64url") from exc


def _numeric_date(claims: Mapping[str, Any], name: str, *, required: bool) -> float | None:
    value = claims.get(name)
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a numeric date")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _scopes(claims: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    scope = claims.get("scope")
    if scope is not None:
        if not isinstance(scope, str):
            raise ValueError("scope must be a string")
        values.extend(scope.split())
    scp = claims.get("scp")
    if isinstance(scp, str):
        values.extend(scp.split())
    elif isinstance(scp, list) and all(isinstance(item, str) for item in scp):
        values.extend(scp)
    elif scp is not None:
        raise ValueError("scp must be a string or string list")
    return list(dict.fromkeys(value for value in values if value))


class KeycloakJWTVerifier(TokenVerifier):
    """Validate Keycloak RS256 access JWTs against a bounded JWKS cache."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        cache_seconds: int = 300,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
        leeway_seconds: int = 30,
    ) -> None:
        issuer_url = urlsplit(issuer)
        if issuer_url.scheme not in {"http", "https"} or not issuer_url.netloc:
            raise ValueError("OIDC issuer must be an absolute HTTP(S) URL")
        if issuer_url.username is not None or issuer_url.password is not None:
            raise ValueError("OIDC issuer must not contain user information")
        if issuer_url.query or issuer_url.fragment:
            raise ValueError("OIDC issuer must not contain query or fragment")
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self.cache_seconds = cache_seconds
        self.clock = clock
        self.leeway_seconds = leeway_seconds
        self.jwks_url = f"{self.issuer}/protocol/openid-connect/certs"
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(5.0),
            follow_redirects=False,
            trust_env=False,
        )
        self._owns_client = client is None
        self._keys: dict[str, rsa.RSAPublicKey] = {}
        self._cache_deadline = 0.0
        self._last_forced_refresh = 0.0
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            if not token or len(token.encode("utf-8")) > _MAX_JWT_BYTES:
                return None
            parts = token.split(".")
            if len(parts) != 3:
                return None
            encoded_header, encoded_claims, encoded_signature = parts
            header = _decode_json_segment(encoded_header)
            claims = _decode_json_segment(encoded_claims)
            signature = _decode_base64url(encoded_signature)

            if header.get("alg") != "RS256":
                return None
            if "crit" in header:
                return None
            token_type = header.get("typ")
            if token_type is not None and token_type not in {"JWT", "at+jwt"}:
                return None
            kid = header.get("kid")
            if not isinstance(kid, str) or not kid or len(kid) > 512:
                return None

            key = await self._get_key(kid)
            if key is None:
                return None
            key.verify(
                signature,
                f"{encoded_header}.{encoded_claims}".encode("ascii"),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )

            now = self.clock()
            expires_at = _numeric_date(claims, "exp", required=True)
            not_before = _numeric_date(claims, "nbf", required=False)
            issued_at = _numeric_date(claims, "iat", required=False)
            assert expires_at is not None
            if expires_at <= now - self.leeway_seconds:
                return None
            if not_before is not None and not_before > now + self.leeway_seconds:
                return None
            if issued_at is not None and issued_at > now + self.leeway_seconds:
                return None
            if claims.get("iss") != self.issuer:
                return None

            audience = claims.get("aud")
            if isinstance(audience, str):
                audiences = [audience]
            elif isinstance(audience, list) and all(isinstance(item, str) for item in audience):
                audiences = audience
            else:
                return None
            if self.audience not in audiences:
                return None

            client_id = claims.get("client_id", claims.get("azp"))
            if not isinstance(client_id, str) or not client_id or len(client_id) > 512:
                return None
            subject = claims.get("sub")
            if subject is not None and (not isinstance(subject, str) or len(subject) > 1_024):
                return None

            return _make_access_token(
                token=token,
                client_id=client_id,
                scopes=_scopes(claims),
                expires_at=int(expires_at),
                resource=self.audience,
                subject=subject,
                claims=claims,
            )
        except (ValueError, UnicodeError, InvalidSignature, httpx.HTTPError):
            return None

    async def _get_key(self, kid: str) -> rsa.RSAPublicKey | None:
        now = time.monotonic()
        if now < self._cache_deadline and kid in self._keys:
            return self._keys[kid]
        # Unknown kids trigger one refresh even while the cache is otherwise
        # fresh, which permits normal Keycloak signing-key rotation.
        await self._refresh_keys(force=bool(self._keys) and kid not in self._keys)
        return self._keys.get(kid)

    async def _refresh_keys(self, *, force: bool) -> None:
        async with self._lock:
            now = time.monotonic()
            if force and now < self._last_forced_refresh + min(30.0, self.cache_seconds / 2):
                return
            if not force and now < self._cache_deadline:
                return
            if force:
                # Bound unknown-kid refreshes so attacker-controlled JWT headers
                # cannot turn the resource server into a Keycloak request flood.
                self._last_forced_refresh = now
            response = await self._client.get(
                self.jwks_url,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            if len(response.content) > _MAX_JWKS_BYTES:
                raise ValueError("JWKS response is too large")
            payload = response.json(
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
            if not isinstance(payload, dict) or not isinstance(payload.get("keys"), list):
                raise ValueError("invalid JWKS")
            parsed: dict[str, rsa.RSAPublicKey] = {}
            for jwk in payload["keys"]:
                parsed_key = _parse_rsa_jwk(jwk)
                if parsed_key is None:
                    continue
                kid, key = parsed_key
                if kid in parsed:
                    raise ValueError("duplicate JWKS kid")
                parsed[kid] = key
            if not parsed:
                raise ValueError("JWKS has no usable signing keys")
            self._keys = parsed
            self._cache_deadline = now + self.cache_seconds


def _parse_rsa_jwk(value: Any) -> tuple[str, rsa.RSAPublicKey] | None:
    if not isinstance(value, dict) or value.get("kty") != "RSA":
        return None
    if value.get("use", "sig") != "sig" or value.get("alg", "RS256") != "RS256":
        return None
    key_ops = value.get("key_ops")
    if key_ops is not None and (not isinstance(key_ops, list) or "verify" not in key_ops):
        return None
    kid = value.get("kid")
    modulus = value.get("n")
    exponent = value.get("e")
    if not isinstance(kid, str) or not kid:
        return None
    if not isinstance(modulus, str) or not modulus:
        return None
    if not isinstance(exponent, str) or not exponent:
        return None
    if len(kid) > 512:
        return None
    n = int.from_bytes(_decode_base64url(modulus), "big")
    e = int.from_bytes(_decode_base64url(exponent), "big")
    if n.bit_length() < 2_048 or n.bit_length() > 8_192:
        return None
    if e < 3 or e > 2**32 or e % 2 == 0:
        return None
    try:
        return kid, rsa.RSAPublicNumbers(e=e, n=n).public_key()
    except ValueError:
        return None


def _make_access_token(**values: Any) -> AccessToken:
    """Construct across the small AccessToken v1/v2 field difference."""

    try:
        supported = inspect.signature(AccessToken).parameters
        values = {key: value for key, value in values.items() if key in supported}
    except (TypeError, ValueError):
        pass
    return AccessToken(**values)


def protected_resource_metadata_url(resource_url: str) -> str:
    """Build the RFC 9728 well-known URI for a path-bearing resource URL."""

    parts = urlsplit(resource_url)
    resource_path = parts.path.rstrip("/")
    metadata_path = "/.well-known/oauth-protected-resource" + resource_path
    return urlunsplit((parts.scheme, parts.netloc, metadata_path, "", ""))


def bearer_challenge(resource_url: str, *, error: str, scope: str | None = None) -> str:
    members = [f'resource_metadata="{protected_resource_metadata_url(resource_url)}"']
    members.append(f'error="{error}"')
    if scope is not None:
        members.append(f'scope="{scope}"')
    return "Bearer " + ", ".join(members)


async def authenticate_request(
    request: Request,
    verifier: TokenVerifier,
    *,
    required_scope: str,
) -> tuple[AccessToken | None, str | None]:
    """Return an access token and an optional OAuth error code."""

    access_token: AccessToken | None = None
    try:
        user = request.user
        candidate = getattr(user, "access_token", None)
        if isinstance(candidate, AccessToken):
            access_token = candidate
    except (AssertionError, AttributeError):
        pass

    if access_token is None:
        header = request.headers.get("authorization", "")
        if len(header) > _MAX_JWT_BYTES + 16:
            return None, "invalid_token"
        scheme, separator, credentials = header.partition(" ")
        if not separator or scheme.lower() != "bearer" or not credentials or " " in credentials:
            return None, "invalid_token"
        access_token = await verifier.verify_token(credentials)
    if access_token is None:
        return None, "invalid_token"
    if required_scope not in access_token.scopes:
        return access_token, "insufficient_scope"
    return access_token, None


def access_subject(access_token: AccessToken) -> str:
    subject = getattr(access_token, "subject", None)
    if isinstance(subject, str) and subject:
        return subject
    claims = getattr(access_token, "claims", None)
    if isinstance(claims, Mapping):
        claim_subject = claims.get("sub")
        if isinstance(claim_subject, str) and claim_subject:
            return claim_subject
    return access_token.client_id
