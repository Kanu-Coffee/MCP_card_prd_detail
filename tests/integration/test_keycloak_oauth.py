from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
import pytest
from bs4 import BeautifulSoup


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} is not configured")
    return value.rstrip("/") if name.endswith("URL") else value


def _expect(response: httpx.Response, status: int | set[int], label: str) -> None:
    allowed = {status} if isinstance(status, int) else status
    assert response.status_code in allowed, f"{label} returned HTTP {response.status_code}"


def _claims(token: str) -> dict[str, Any]:
    parts = token.split(".")
    assert len(parts) == 3
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    parsed = json.loads(base64.urlsafe_b64decode(payload))
    assert isinstance(parsed, dict)
    return parsed


def _allow_insecure_loopback_session_cookies(client: httpx.Client, base_url: str) -> int:
    """Model browser localhost secure-context handling for an HTTP-only fixture.

    Keycloak deliberately marks its authorization-session cookies ``Secure``.
    Browsers treat localhost origins as trustworthy, while HTTPX correctly
    follows the stricter cookie transport rule and withholds them over plain
    HTTP.  This override is therefore limited to the isolated loopback fixture
    and must never make a remote HTTP Keycloak deployment appear valid.
    """

    parsed = urlparse(base_url)
    host = (parsed.hostname or "").lower()
    loopback = host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".localhost")
    if parsed.scheme != "http" or not loopback:
        raise ValueError("insecure Keycloak session cookies are allowed only for HTTP loopback fixtures")

    changed = 0
    for cookie in client.cookies.jar:
        if cookie.secure:
            cookie.secure = False
            changed += 1
    return changed


@pytest.mark.integration
def test_loopback_cookie_transport_override_rejects_non_loopback() -> None:
    client = httpx.Client()
    try:
        with pytest.raises(ValueError, match="only for HTTP loopback"):
            _allow_insecure_loopback_session_cookies(client, "http://keycloak.example.com")
        with pytest.raises(ValueError, match="only for HTTP loopback"):
            _allow_insecure_loopback_session_cookies(client, "https://127.0.0.1")
    finally:
        client.close()


@pytest.mark.integration
def test_keycloak_service_pkce_refresh_rotation_revoke_and_idle_contract() -> None:
    base_url = _required_environment("CARDRAG_TEST_KEYCLOAK_URL")
    admin_username = _required_environment("CARDRAG_TEST_KEYCLOAK_ADMIN_USERNAME")
    admin_password = _required_environment("CARDRAG_TEST_KEYCLOAK_ADMIN_PASSWORD")
    suffix = secrets.token_hex(6)
    service_client_id = f"cardrag-ci-service-{suffix}"
    browser_client_id = f"cardrag-ci-browser-{suffix}"
    user_name = f"cardrag-ci-user-{suffix}"
    service_secret = secrets.token_urlsafe(32)
    user_password = secrets.token_urlsafe(32)
    redirect_uri = "http://127.0.0.1/cardrag-fixture-callback"
    created_clients: list[str] = []
    created_user: str | None = None

    with httpx.Client(timeout=20.0, follow_redirects=False) as client:
        admin_token_response = client.post(
            f"{base_url}/realms/master/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": admin_username,
                "password": admin_password,
            },
        )
        _expect(admin_token_response, 200, "admin token")
        admin_headers = {"Authorization": f"Bearer {admin_token_response.json()['access_token']}"}
        realm_admin = f"{base_url}/admin/realms/cardrag"

        def create_client(payload: dict[str, Any]) -> str:
            response = client.post(f"{realm_admin}/clients", headers=admin_headers, json=payload)
            _expect(response, 201, "client creation")
            identifier = response.headers["location"].rsplit("/", 1)[-1]
            created_clients.append(identifier)
            return identifier

        try:
            create_client(
                {
                    "clientId": service_client_id,
                    "enabled": True,
                    "protocol": "openid-connect",
                    "publicClient": False,
                    "secret": service_secret,
                    "serviceAccountsEnabled": True,
                    "standardFlowEnabled": False,
                    "directAccessGrantsEnabled": False,
                    "fullScopeAllowed": False,
                    "optionalClientScopes": ["search", "source_pdf"],
                }
            )
            create_client(
                {
                    "clientId": browser_client_id,
                    "enabled": True,
                    "protocol": "openid-connect",
                    "publicClient": True,
                    "serviceAccountsEnabled": False,
                    "standardFlowEnabled": True,
                    "directAccessGrantsEnabled": False,
                    "fullScopeAllowed": False,
                    "redirectUris": [redirect_uri],
                    "webOrigins": [],
                    "optionalClientScopes": ["search", "offline_access"],
                    "attributes": {"pkce.code.challenge.method": "S256"},
                }
            )
            user_response = client.post(
                f"{realm_admin}/users",
                headers=admin_headers,
                json={
                    "username": user_name,
                    "enabled": True,
                    "firstName": "CardRAG",
                    "lastName": "Fixture",
                    "email": f"{user_name}@example.invalid",
                    "emailVerified": True,
                    "credentials": [
                        {"type": "password", "value": user_password, "temporary": False}
                    ],
                },
            )
            _expect(user_response, 201, "user creation")
            created_user = user_response.headers["location"].rsplit("/", 1)[-1]

            service_token_response = client.post(
                f"{base_url}/realms/cardrag/protocol/openid-connect/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": service_client_id,
                    "client_secret": service_secret,
                    "scope": "search source_pdf",
                },
            )
            _expect(service_token_response, 200, "service token")
            service_claims = _claims(service_token_response.json()["access_token"])
            assert service_claims["iss"] == f"{base_url}/realms/cardrag"
            audience = service_claims["aud"]
            assert "cardrag-mcp" in ([audience] if isinstance(audience, str) else audience)
            assert {"search", "source_pdf"}.issubset(set(service_claims["scope"].split()))

            verifier = secrets.token_urlsafe(64)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            browser = httpx.Client(timeout=20.0, follow_redirects=False)
            try:
                authorization = browser.get(
                    f"{base_url}/realms/cardrag/protocol/openid-connect/auth",
                    params={
                        "client_id": browser_client_id,
                        "redirect_uri": redirect_uri,
                        "response_type": "code",
                        "scope": "openid search offline_access",
                        "code_challenge": challenge,
                        "code_challenge_method": "S256",
                        "state": suffix,
                    },
                )
                _expect(authorization, 200, "authorization form")
                form = BeautifulSoup(authorization.text, "html.parser").find("form", id="kc-form-login")
                assert form is not None and form.get("action")
                if urlparse(base_url).scheme == "http":
                    assert _allow_insecure_loopback_session_cookies(browser, base_url) > 0
                login = browser.post(
                    urljoin(str(authorization.url), str(form["action"])),
                    data={"username": user_name, "password": user_password, "credentialId": ""},
                )
                _expect(login, 302, "PKCE login")
                location = login.headers["location"]
                assert location.startswith(redirect_uri)
                query = parse_qs(urlparse(location).query)
                assert query["state"] == [suffix]
                code = query["code"][0]

                exchange = browser.post(
                    f"{base_url}/realms/cardrag/protocol/openid-connect/token",
                    data={
                        "grant_type": "authorization_code",
                        "client_id": browser_client_id,
                        "redirect_uri": redirect_uri,
                        "code": code,
                        "code_verifier": verifier,
                    },
                )
                _expect(exchange, 200, "PKCE token exchange")
                first_refresh = exchange.json()["refresh_token"]
                browser_claims = _claims(exchange.json()["id_token"])
                assert browser_claims["sub"] == created_user
                assert {"search", "offline_access"}.issubset(exchange.json()["scope"].split())
                assert _claims(first_refresh)["typ"] == "Offline"

                rotated = browser.post(
                    f"{base_url}/realms/cardrag/protocol/openid-connect/token",
                    data={
                        "grant_type": "refresh_token",
                        "client_id": browser_client_id,
                        "refresh_token": first_refresh,
                    },
                )
                _expect(rotated, 200, "refresh rotation")
                second_refresh = rotated.json()["refresh_token"]
                assert second_refresh != first_refresh

                reused = browser.post(
                    f"{base_url}/realms/cardrag/protocol/openid-connect/token",
                    data={
                        "grant_type": "refresh_token",
                        "client_id": browser_client_id,
                        "refresh_token": first_refresh,
                    },
                )
                _expect(reused, 400, "rotated refresh reuse")

                revoked = browser.post(
                    f"{base_url}/realms/cardrag/protocol/openid-connect/revoke",
                    data={"client_id": browser_client_id, "token": second_refresh},
                )
                _expect(revoked, 200, "refresh revocation")
                after_revoke = browser.post(
                    f"{base_url}/realms/cardrag/protocol/openid-connect/token",
                    data={
                        "grant_type": "refresh_token",
                        "client_id": browser_client_id,
                        "refresh_token": second_refresh,
                    },
                )
                _expect(after_revoke, 400, "revoked refresh")
            finally:
                browser.close()

            realm = client.get(realm_admin, headers=admin_headers)
            _expect(realm, 200, "realm configuration")
            assert realm.json()["revokeRefreshToken"] is True
            assert realm.json()["refreshTokenMaxReuse"] == 0
            assert realm.json()["ssoSessionIdleTimeout"] == 90 * 24 * 60 * 60
            assert realm.json()["offlineSessionIdleTimeout"] == 90 * 24 * 60 * 60
            assert realm.json()["offlineSessionMaxLifespanEnabled"] is False
            assert int(service_claims["exp"]) > int(time.time())
        finally:
            if created_user is not None:
                client.delete(f"{realm_admin}/users/{created_user}", headers=admin_headers)
            for identifier in reversed(created_clients):
                client.delete(f"{realm_admin}/clients/{identifier}", headers=admin_headers)
