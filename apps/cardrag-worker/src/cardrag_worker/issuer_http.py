"""Issuer HTTP pools with a TLS compatibility exception for one exact origin."""

from __future__ import annotations

import ssl
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx

if TYPE_CHECKING:
    from .issuer_metrics import IssuerHTTPMetrics

PROXY_HOSTS = frozenset({"www.shinhancard.com", "www.lottecard.co.kr", "image.lottecard.co.kr"})


def validate_issuer_proxy_url(value: str | None) -> str | None:
    if not value or not value.strip():
        return None
    # Never include a URL (potentially containing credentials) in diagnostics.
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "http"
            or not parsed.hostname
            or parsed.port == 0
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
        httpx.URL(value)
    except (ValueError, httpx.InvalidURL):
        raise ValueError("CARDRAG_ISSUER_PROXY_URL must be an HTTP proxy origin") from None
    return value


class _IssuerProxyTransport(httpx.AsyncHTTPTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if (
            request.url.scheme != "https"
            or request.url.host not in PROXY_HOSTS
            or request.url.port not in {None, 443}
        ):
            raise httpx.InvalidURL("issuer proxy is restricted to approved HTTPS origins")
        try:
            return await super().handle_async_request(request)
        except httpx.TransportError:
            # Transport exception messages may contain the private endpoint.
            raise httpx.ConnectError("issuer proxy transport failed", request=request) from None


HYUNDAI_ORIGIN = "https://www.hyundaicard.com:443"


class _HyundaiTransport(httpx.AsyncHTTPTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # HTTPX normalizes :443 out of mount patterns, which otherwise match
        # every port on this host. Check the actual origin before connecting.
        if (
            request.url.scheme != "https"
            or request.url.host != "www.hyundaicard.com"
            or request.url.port not in {None, 443}
        ):
            raise httpx.InvalidURL("legacy TLS is restricted to the Hyundai HTTPS origin")
        return await super().handle_async_request(request)


def hyundai_ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    # Keep certificate/hostname verification and the default protocol/cipher
    # policy. This server alone needs pre-RFC 5746 initial connection support.
    context.options |= ssl.OP_LEGACY_SERVER_CONNECT
    return context


def create_issuer_client(
    *,
    hyundai_legacy_tls: bool = False,
    proxy_url: str | None = None,
    metrics: IssuerHTTPMetrics | None = None,
) -> httpx.AsyncClient:
    mounts: dict[str, httpx.AsyncBaseTransport] = {}
    proxy_url = validate_issuer_proxy_url(proxy_url)
    if proxy_url is not None:
        # Independent transports are closed by AsyncClient. Redirects select a
        # transport again on each actual hop, after downloader origin checks.
        for host in sorted(PROXY_HOSTS):
            mounts[f"https://{host}:443"] = _IssuerProxyTransport(proxy=proxy_url, trust_env=False)
    if hyundai_legacy_tls:
        mounts[HYUNDAI_ORIGIN] = _HyundaiTransport(verify=hyundai_ssl_context())
    return httpx.AsyncClient(
        mounts=mounts,
        follow_redirects=False,
        timeout=60,
        trust_env=False,
        event_hooks={"request": [metrics.before], "response": [metrics.after]}
        if metrics is not None
        else None,
    )
