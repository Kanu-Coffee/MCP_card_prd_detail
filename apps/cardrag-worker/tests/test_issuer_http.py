from __future__ import annotations

import ssl

import httpx
import pytest

from cardrag_worker import issuer_http


def test_legacy_context_keeps_certificate_hostname_and_protocol_policy() -> None:
    default = ssl.create_default_context()
    legacy = issuer_http.hyundai_ssl_context()
    assert legacy.verify_mode == ssl.CERT_REQUIRED
    assert legacy.check_hostname is True
    assert legacy.minimum_version == default.minimum_version
    assert legacy.get_ciphers() == default.get_ciphers()
    assert legacy.options == default.options | ssl.OP_LEGACY_SERVER_CONNECT


@pytest.mark.parametrize("enabled", [False, True])
async def test_tls_exception_routes_only_to_exact_hyundai_https_origin(enabled: bool) -> None:
    async with issuer_http.create_issuer_client(hyundai_legacy_tls=enabled) as client:
        special = client._transport_for_url(httpx.URL(issuer_http.HYUNDAI_ORIGIN))
        if enabled:
            assert special is not client._transport
        for url in (
            "https://www.hyundaicard.com/upload/card/guide.pdf",
            "https://www.hyundaicard.com:443/cpu/ug/CPUUG2001_04.hc",
        ):
            assert client._transport_for_url(httpx.URL(url)) is special
        if enabled:
            for url in (
                "http://www.hyundaicard.com",
                "https://m.hyundaicard.com",
                "https://sub.www.hyundaicard.com",
                "https://www.hyundaicard.com.attacker.example",
                "https://www.hanacard.co.kr",
                "https://webdav.example",
                "https://openrouter.ai",
            ):
                assert client._transport_for_url(httpx.URL(url)) is not special
            with pytest.raises(httpx.InvalidURL, match="restricted"):
                await client.get("https://www.hyundaicard.com:8443/guide.pdf")
        assert client.follow_redirects is False
        assert client.timeout == httpx.Timeout(60)
