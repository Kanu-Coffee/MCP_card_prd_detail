"""Issuer HTTP pools with a TLS compatibility exception for one exact origin."""

from __future__ import annotations

import ssl

import httpx

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


def create_issuer_client(*, hyundai_legacy_tls: bool = False) -> httpx.AsyncClient:
    mounts: dict[str, httpx.AsyncBaseTransport] = {}
    if hyundai_legacy_tls:
        mounts[HYUNDAI_ORIGIN] = _HyundaiTransport(verify=hyundai_ssl_context())
    return httpx.AsyncClient(mounts=mounts, follow_redirects=False, timeout=60)
