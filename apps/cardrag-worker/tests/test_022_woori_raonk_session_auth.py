"""Acceptance and regression tests for Woori RAONK session authentication."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from helpers import pdf_bytes
from test_pipeline import (
    FakeEmbeddings,
    FakeOCR,
    FakeWebDAV,
    install_concurrent_pdf_http,
)

from cardrag_worker.contracts import SourceRecord
from cardrag_worker.detail_inventory import DetailInventory
from cardrag_worker.downloader import (
    DownloadPolicy,
    DownloadRequest,
    SecurePDFDownloader,
)
from cardrag_worker.issuers.common import IssuerMarkupChanged
from cardrag_worker.issuers.woori import (
    BASE_URL,
    DETAIL_PATH,
    MAIN_PATH,
    NOTICE_PATH,
    RAONK_PATH,
    WooriAdapter,
    extract_raonk_auth_tokens,
)
from cardrag_worker.pipeline import (
    WorkerPipeline,
    WorkerUnexpectedFailureError,
)
from cardrag_worker.rate_limit import IssuerRateLimiter, RateLimitedClient
from cardrag_worker.state import WorkerState

SERVICE_HTML = (
    "<!doctype html><html><head><title>우리카드</title></head>"
    "<body><h1>Error</h1><p>현재 홈페이지 서비스가 원활하지 않습니다.</p></body></html>"
).encode()


def _public_ip(_host: str) -> tuple[str, ...]:
    return ("93.184.216.34",)


def _make_source(
    code: str = "100637",
    name: str = "배달의민족 비장의카드 V.2",
    path: str = "/upload/cardClause/2023/10/13/08455444-775a-4e0c-a180-f43e872b3871.pdf",
    file_name: str = "1.(100637)배달의민족 비장의카드 V2_202310.pdf",
) -> SourceRecord:
    return SourceRecord(
        issuer="woori",
        product_code=code,
        product_name=name,
        effective_date=date(2023, 10, 13),
        source_version="2",
        source_url=f"https://pc.wooricard.com{path}",
        source_post_id="08455444-775a-4e0c-a180-f43e872b3871",
        file_name=file_name,
        category="personal",
        discovered_at=datetime.now(UTC),
        metadata={"raonk_file_path": path},
    )


# 1. Landing cookie and auth headers required; test via SecurePDFDownloader + RateLimitedClient
@pytest.mark.asyncio
async def test_woori_download_with_cookies_and_auth_headers_via_downloader(tmp_path: Path) -> None:
    expected_cookie = "WOORI_SESSION=s_abc123"
    expected_page_id = "H1CCT211S09"
    expected_auth_key = "key_20261030_12345"
    valid_pdf = pdf_bytes(pages=2)

    def handler(request: httpx.Request) -> httpx.Response:
        url_path = request.url.path
        if url_path == NOTICE_PATH:
            return httpx.Response(
                200,
                headers={
                    "content-type": "text/html; charset=UTF-8",
                    "set-cookie": "WOORI_SESSION=s_abc123; Path=/; Domain=pc.wooricard.com",
                },
                text=(
                    "<html><body>"
                    f'<input type="hidden" id="raonkAuthKey" value="{expected_auth_key}" />'
                    f'<input type="hidden" id="raonkAuthPageId" value="{expected_page_id}" />'
                    "</body></html>"
                ),
                request=request,
            )
        if url_path == RAONK_PATH:
            cookie_header = request.headers.get("cookie", "")
            req_page_id = request.headers.get("raonk_auth_page_id")
            req_auth_key = request.headers.get("raonk_auth_key")
            referer = request.headers.get("referer")

            # Check all security conditions
            if (
                expected_cookie in cookie_header
                and req_page_id == expected_page_id
                and req_auth_key == expected_auth_key
                and referer == BASE_URL + NOTICE_PATH
                and request.method == "POST"
            ):
                return httpx.Response(
                    200,
                    headers={"content-type": "application/pdf"},
                    content=valid_pdf,
                    request=request,
                )
            # Origin returns 200 HTML error if headers or session are missing
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=UTF-8"},
                content=SERVICE_HTML,
                request=request,
            )
        return httpx.Response(404, request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as raw_client:
        rate_client = RateLimitedClient(raw_client, IssuerRateLimiter(0), issuer="woori", purpose="pdf")
        adapter = WooriAdapter()
        source = _make_source()

        # Prepare download
        download_req = await adapter.prepare_download(rate_client, source)
        assert download_req.method == "POST"
        assert download_req.headers.get("RAONK_AUTH_PAGE_ID") == expected_page_id
        assert download_req.headers.get("RAONK_AUTH_KEY") == expected_auth_key

        # Download through SecurePDFDownloader
        dest = tmp_path / "downloaded.pdf"
        downloader = SecurePDFDownloader(
            DownloadPolicy(
                allowed_hosts=frozenset({"pc.wooricard.com"}),
                allow_private_networks_for_tests=True,
            ),
            resolver=_public_ip,
        )
        downloaded = await downloader.download(rate_client, download_req, dest)
        assert downloaded.page_count == 2
        assert downloaded.size_bytes == len(valid_pdf)
        assert dest.exists()


# 2. Client isolation: discovery client A -> download client B, prepare_download alone, detail inventory reuse
@pytest.mark.asyncio
async def test_woori_client_isolation_and_detail_inventory_reuse() -> None:
    adapter = WooriAdapter(minimum_records=1)

    # Client A: discovery client with session A
    landing_calls = {"client_a": 0, "client_b": 0, "client_c": 0}

    def make_handler(client_id: str, auth_key: str):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == NOTICE_PATH:
                landing_calls[client_id] += 1
                return httpx.Response(
                    200,
                    headers={"set-cookie": f"SESSION={client_id}; Path=/; Domain=pc.wooricard.com"},
                    text=(
                        f'<html><input type="hidden" id="raonkAuthKey" value="{auth_key}" />'
                        '<input type="hidden" id="raonkAuthPageId" value="H1CCT211S09" /></html>'
                    ),
                    request=request,
                )
            if request.url.path == MAIN_PATH:
                return httpx.Response(
                    200,
                    json={
                        "elHeader": {"resSuc": True},
                        "mainDataList": {
                            "cct11PrdntcAgrmMainVo": [
                                {"goodsDescAt": "Y", "code": "100637", "codeName": "Test Card"}
                            ]
                        },
                    },
                    request=request,
                )
            if request.url.path == DETAIL_PATH:
                return httpx.Response(
                    200,
                    json={
                        "elHeader": {"resSuc": True},
                        "rows": [
                            {
                                "filePath": "/upload/cardClause/test.pdf",
                                "fileNm": "test.pdf",
                                "beginDt": "20261001",
                                "gdccVer": "1",
                            }
                        ],
                    },
                    request=request,
                )
            return httpx.Response(404, request=request)

        return handler

    # 1. Discover with Client A
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(make_handler("client_a", "KEY_A")),
        base_url=BASE_URL,
    ) as client_a:
        snapshot = await adapter.discover_current(client_a)
        assert len(snapshot.records) == 1
        assert landing_calls["client_a"] == 1

    # 2. Download with Client B (must acquire Client B's fresh session, not Client A's)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(make_handler("client_b", "KEY_B")),
        base_url=BASE_URL,
    ) as client_b:
        req_b = await adapter.prepare_download(client_b, snapshot.records[0])
        assert req_b.headers["RAONK_AUTH_KEY"] == "KEY_B"
        assert landing_calls["client_b"] == 1
        # Check Client B's cookie jar was populated
        assert client_b.cookies.get("SESSION") == "client_b"

    # 3. Resume / separate invocation with Client C on same adapter
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(make_handler("client_c", "KEY_C")),
        base_url=BASE_URL,
    ) as client_c:
        req_c = await adapter.prepare_download(client_c, snapshot.records[0])
        assert req_c.headers["RAONK_AUTH_KEY"] == "KEY_C"
        assert landing_calls["client_c"] == 1
        assert client_c.cookies.get("SESSION") == "client_c"


# 3. Single-flight concurrent downloads on same client; separate client isolation
@pytest.mark.asyncio
async def test_woori_concurrent_downloads_single_flight() -> None:
    landing_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal landing_count
        if request.url.path == NOTICE_PATH:
            landing_count += 1
            return httpx.Response(
                200,
                headers={"set-cookie": "WOORI_SESSION=concurrent; Path=/; Domain=pc.wooricard.com"},
                text=(
                    '<html><input type="hidden" id="raonkAuthKey" value="CONCURRENT_KEY" />'
                    '<input type="hidden" id="raonkAuthPageId" value="H1CCT211S09" /></html>'
                ),
                request=request,
            )
        return httpx.Response(404, request=request)

    adapter = WooriAdapter()
    source = _make_source()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=BASE_URL) as client:
        # Run 5 prepare_download calls concurrently on the same client
        requests = await asyncio.gather(*[adapter.prepare_download(client, source) for _ in range(5)])

        # Single flight: exactly 1 landing GET occurred
        assert landing_count == 1
        for req in requests:
            assert req.headers["RAONK_AUTH_KEY"] == "CONCURRENT_KEY"
            assert req.headers["RAONK_AUTH_PAGE_ID"] == "H1CCT211S09"
        # Client cookies intact
        assert client.cookies.get("WOORI_SESSION") == "concurrent"


# 4. Fail-closed on missing/empty/conflicting/unsafe tokens; no sentinel secret leakage
def test_woori_token_extraction_fail_closed_and_sentinel_leakage() -> None:
    sentinel = "SECRET_SENTINEL_RAONK_TOKEN_999"

    # Missing key
    with pytest.raises(IssuerMarkupChanged, match="missing RAONK auth tokens") as exc:
        extract_raonk_auth_tokens('<html><input id="raonkAuthPageId" value="P1" /></html>')
    assert sentinel not in str(exc.value)

    # Missing page ID
    with pytest.raises(IssuerMarkupChanged, match="missing RAONK auth tokens") as exc:
        extract_raonk_auth_tokens(f'<html><input id="raonkAuthKey" value="{sentinel}" /></html>')
    assert sentinel not in str(exc.value)

    # Empty token
    with pytest.raises(IssuerMarkupChanged, match="empty RAONK auth tokens") as exc:
        extract_raonk_auth_tokens(
            '<html><input id="raonkAuthKey" value="" /><input id="raonkAuthPageId" value="P1" /></html>'
        )

    # Conflicting multiple values
    with pytest.raises(IssuerMarkupChanged, match="conflicting RAONK auth tokens") as exc:
        extract_raonk_auth_tokens(
            f'<html><input id="raonkAuthKey" value="KEY1" />'
            f'<input id="raonkAuthKey" value="{sentinel}" />'
            '<input id="raonkAuthPageId" value="P1" /></html>'
        )
    assert sentinel not in str(exc.value)

    # Unsafe / header injection characters (CRLF, spaces, semicolons, quotes, etc.)
    for bad_token in [
        f"bad\r\nInjected-Header: {sentinel}",
        "token with space",
        "token;semi",
        "token'quote",
        "token@at",
        "token/slash",
        "token?question",
    ]:
        with pytest.raises(IssuerMarkupChanged, match="invalid RAONK auth token format") as exc:
            extract_raonk_auth_tokens(
                f'<html><input id="raonkAuthKey" value="{bad_token}" />'
                '<input id="raonkAuthPageId" value="P1" /></html>'
            )
        assert sentinel not in str(exc.value)


# 5. Retry budget: Token 1 HTML -> Token 2 success; persistent HTML -> bounded attempts
@pytest.mark.asyncio
async def test_woori_retry_recovers_after_token_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    landing_requests: list[str] = []
    download_requests: list[dict[str, Any]] = []
    token_counter = 0
    valid_pdf = pdf_bytes(pages=2)

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_counter
        if request.url.path == NOTICE_PATH:
            token_counter += 1
            key = f"TOKEN_{token_counter}"
            landing_requests.append(key)
            return httpx.Response(
                200,
                headers={"set-cookie": f"WOORI_SESS=s_{token_counter}; Path=/; Domain=pc.wooricard.com"},
                text=(
                    f'<html><input type="hidden" id="raonkAuthKey" value="{key}" />'
                    '<input type="hidden" id="raonkAuthPageId" value="H1CCT211S09" /></html>'
                ),
                request=request,
            )
        if request.url.path == RAONK_PATH:
            key = request.headers.get("raonk_auth_key", "")
            download_requests.append({"key": key, "cookie": request.headers.get("cookie")})
            # TOKEN_1 returns origin service error (HTML)
            if key == "TOKEN_1":
                return httpx.Response(
                    200,
                    headers={"content-type": "text/html; charset=UTF-8"},
                    content=SERVICE_HTML,
                    request=request,
                )
            # TOKEN_2 succeeds with PDF!
            if key == "TOKEN_2":
                return httpx.Response(
                    200,
                    headers={"content-type": "application/pdf"},
                    content=valid_pdf,
                    request=request,
                )
        return httpx.Response(404, request=request)

    class StopAfterAcquisition(RuntimeError):
        pass

    class BarrierWebDAV(FakeWebDAV):
        async def validated_current_generation(self) -> Any:
            raise StopAfterAcquisition

    install_concurrent_pdf_http(monkeypatch, handler)
    source = _make_source()
    adapter = WooriAdapter(minimum_records=1)
    adapter.spec = replace(adapter.spec, minimum_records=1, maximum_retries=2)

    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[adapter],
            ocr=FakeOCR(),  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=BarrierWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
            retry_cap_seconds=0,
        )

        async def mock_discover(client: httpx.AsyncClient):
            from cardrag_worker.contracts import snapshot_from_records

            return snapshot_from_records(
                issuer="woori",
                source_url=BASE_URL + NOTICE_PATH,
                parser_version="woori.current.v1",
                records=[source],
                started_at=datetime.now(UTC),
            )

        adapter.discover_current = mock_discover  # type: ignore[assignment]

        run_id = state.start_run()
        with pytest.raises(StopAfterAcquisition):
            await pipeline._run_locked(run_id)

        assert landing_requests == ["TOKEN_1", "TOKEN_2"]
        assert len(download_requests) == 2
        assert download_requests[0]["key"] == "TOKEN_1"
        assert download_requests[1]["key"] == "TOKEN_2"


@pytest.mark.asyncio
async def test_woori_persistent_service_error_fails_bounded_by_maximum_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    landing_calls = 0
    download_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal landing_calls, download_calls
        if request.url.path == NOTICE_PATH:
            landing_calls += 1
            return httpx.Response(
                200,
                headers={"set-cookie": "WOORI_SESS=s1; Path=/; Domain=pc.wooricard.com"},
                text=(
                    f'<html><input type="hidden" id="raonkAuthKey" value="KEY_{landing_calls}" />'
                    '<input type="hidden" id="raonkAuthPageId" value="H1CCT211S09" /></html>'
                ),
                request=request,
            )
        if request.url.path == RAONK_PATH:
            download_calls += 1
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=UTF-8"},
                content=SERVICE_HTML,
                request=request,
            )
        return httpx.Response(404, request=request)

    install_concurrent_pdf_http(monkeypatch, handler)
    source = _make_source()
    adapter = WooriAdapter(minimum_records=1)
    adapter.spec = replace(adapter.spec, minimum_records=1, maximum_retries=2)

    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[adapter],
            ocr=FakeOCR(),  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
            retry_cap_seconds=0,
        )

        # Mock discover_current so pipeline attempts downloading source
        async def mock_discover(client: httpx.AsyncClient):
            from cardrag_worker.contracts import snapshot_from_records

            return snapshot_from_records(
                issuer="woori",
                source_url=BASE_URL + NOTICE_PATH,
                parser_version="woori.current.v1",
                records=[source],
                started_at=datetime.now(UTC),
            )

        adapter.discover_current = mock_discover  # type: ignore[assignment]

        with pytest.raises(WorkerUnexpectedFailureError) as captured:
            await pipeline.run()

        failure = captured.value.failure
        assert failure.reason_code == "issuer_origin_service_error"
        assert failure.stage == "download"
        # Download calls and landing calls were bounded by maximum_retries (2 attempts, no infinite loop)
        assert download_calls == 2
        assert landing_calls == 2


# 6. Cross-host redirect strips RAONK auth headers in SecurePDFDownloader
@pytest.mark.asyncio
async def test_woori_downloader_cross_host_redirect_strips_auth_headers(tmp_path: Path) -> None:
    seen_headers_by_host: dict[str, dict[str, str]] = {}
    valid_pdf = pdf_bytes(pages=1)

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        seen_headers_by_host[host] = dict(request.headers)
        if host == "pc.wooricard.com":
            return httpx.Response(
                302,
                headers={"Location": "https://cdn.wooricard.com/file.pdf"},
                request=request,
            )
        if host == "cdn.wooricard.com":
            return httpx.Response(
                200,
                headers={"content-type": "application/pdf"},
                content=valid_pdf,
                request=request,
            )
        return httpx.Response(404, request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://pc.wooricard.com",
    ) as client:
        req = DownloadRequest(
            url="https://pc.wooricard.com/dcpc/kupload/handler/raonkhandler.jsp",
            method="POST",
            form={"k01": "test"},
            headers={
                "RAONK_AUTH_PAGE_ID": "H1CCT211S09",
                "RAONK_AUTH_KEY": "SECRET_KEY_123",
                "Referer": "https://pc.wooricard.com/notice",
            },
        )
        dest = tmp_path / "redirected.pdf"
        downloader = SecurePDFDownloader(
            DownloadPolicy(
                allowed_hosts=frozenset({"pc.wooricard.com", "cdn.wooricard.com"}),
                allow_private_networks_for_tests=True,
            ),
            resolver=_public_ip,
        )
        downloaded = await downloader.download(client, req, dest)
        assert downloaded.page_count == 1

        # Origin host received the auth headers
        assert seen_headers_by_host["pc.wooricard.com"].get("raonk_auth_key") == "SECRET_KEY_123"
        # Cross-host redirect stripped the auth headers
        assert "raonk_auth_key" not in seen_headers_by_host["cdn.wooricard.com"]
        assert "raonk_auth_page_id" not in seen_headers_by_host["cdn.wooricard.com"]


# 7. Sequential documents: cookie attributes (Secure, expiry) preserved, server rotation respected, expiry refreshes
@pytest.mark.asyncio
async def test_woori_sequential_documents_preserve_cookie_attributes_and_handle_rotation_expiry(
    tmp_path: Path,
) -> None:
    landing_calls = 0
    sid_value = "old_sid"
    expiry_offset = 3600
    valid_pdf = pdf_bytes(pages=1)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal landing_calls
        if request.url.path == NOTICE_PATH:
            landing_calls += 1
            return httpx.Response(
                200,
                headers=[
                    (
                        "Set-Cookie",
                        f"SID={sid_value}; Path=/; Secure; Max-Age={expiry_offset}; HttpOnly",
                    ),
                    ("Set-Cookie", "SESS=static_sess; Path=/; HttpOnly"),
                ],
                text=(
                    f'<html><input type="hidden" id="raonkAuthKey" value="KEY_{landing_calls}" />'
                    '<input type="hidden" id="raonkAuthPageId" value="H1CCT211S09" /></html>'
                ),
                request=request,
            )
        if request.url.path == RAONK_PATH:
            # During doc 1 download, simulate server rotating SID cookie
            return httpx.Response(
                200,
                headers=[
                    ("content-type", "application/pdf"),
                    ("Set-Cookie", "SID=rotated_sid; Path=/; Secure; Max-Age=3600; HttpOnly"),
                ],
                content=valid_pdf,
                request=request,
            )
        return httpx.Response(404, request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as raw_client:
        rate_client = RateLimitedClient(raw_client, IssuerRateLimiter(0), issuer="woori", purpose="pdf")
        adapter = WooriAdapter()
        source1 = _make_source(code="100001", path="/upload/1.pdf", file_name="1.pdf")
        source2 = _make_source(code="100002", path="/upload/2.pdf", file_name="2.pdf")

        # 1. Prepare Doc 1 -> fetches landing
        req1 = await adapter.prepare_download(rate_client, source1)
        assert req1.headers["RAONK_AUTH_KEY"] == "KEY_1"
        assert landing_calls == 1

        # Check initial cookie attributes
        cookies_by_name = {c.name: c for c in raw_client.cookies.jar}
        assert cookies_by_name["SID"].value == "old_sid"
        assert cookies_by_name["SID"].secure is True
        assert cookies_by_name["SID"].domain_specified is False
        assert cookies_by_name["SID"].expires is not None

        # Download Doc 1 -> server rotates cookie to 'rotated_sid'
        downloader = SecurePDFDownloader(
            DownloadPolicy(
                allowed_hosts=frozenset({"pc.wooricard.com"}),
                allow_private_networks_for_tests=True,
            ),
            resolver=_public_ip,
        )
        dest1 = tmp_path / "1.pdf"
        await downloader.download(rate_client, req1, dest1)

        # Confirm client jar received the rotated cookie
        assert dict(raw_client.cookies)["SID"] == "rotated_sid"

        # 2. Pipeline clears client cookies before Doc 2 (simulating pipeline.py line 4352)
        raw_client.cookies.clear()
        assert len(list(raw_client.cookies.jar)) == 0

        # Prepare Doc 2 on the same client -> must restore rotated cookie and keep attributes!
        req2 = await adapter.prepare_download(rate_client, source2)
        assert landing_calls == 1  # No landing GET!
        assert req2.headers["RAONK_AUTH_KEY"] == "KEY_1"

        # Check restored cookies: rotated value preserved, NOT reverted to 'old_sid'
        cookies_after_restore = {c.name: c for c in raw_client.cookies.jar}
        assert cookies_after_restore["SID"].value == "rotated_sid"
        assert cookies_after_restore["SID"].secure is True
        assert cookies_after_restore["SID"].domain_specified is False
        assert cookies_after_restore["SID"].expires is not None

        # 3. Simulate cookie expiration: set cookie expiry in the past
        target = getattr(rate_client, "client", rate_client)
        session = adapter._sessions[target]
        for c in session.cookies.values():
            c.expires = time.time() - 100

        # Pipeline clears cookies before Doc 3
        raw_client.cookies.clear()

        # Prepare Doc 3 -> session detects expired cookie and fetches fresh landing!
        source3 = _make_source(code="100003", path="/upload/3.pdf", file_name="3.pdf")
        req3 = await adapter.prepare_download(rate_client, source3)
        assert landing_calls == 2  # Fresh landing GET called!
        assert req3.headers["RAONK_AUTH_KEY"] == "KEY_2"


# 8. Authentication fail-closed on None client and unsupported weakref client
@pytest.mark.asyncio
async def test_woori_prepare_download_rejects_none_and_unsupported_clients() -> None:
    adapter = WooriAdapter()
    source = _make_source()

    # None client rejected
    with pytest.raises(ValueError, match="client is required for Woori download session authentication"):
        await adapter.prepare_download(None, source)

    # Unsupported weakref client rejected
    with pytest.raises(TypeError, match="does not support weak references"):
        await adapter.prepare_download("unsupported-string-client", source)

    with pytest.raises(TypeError, match="does not support weak references"):
        await adapter.prepare_download(12345, source)

    # Test wrapper reuse: different wrappers over the same AsyncClient share identical session
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text='<html><input id="raonkAuthKey" value="KEY_SHARED" /><input id="raonkAuthPageId" value="H1CCT211S09" /></html>',
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=BASE_URL) as client1:
        wrapper1 = RateLimitedClient(client1, IssuerRateLimiter(0), issuer="woori", purpose="pdf")
        wrapper2 = RateLimitedClient(client1, IssuerRateLimiter(0), issuer="woori", purpose="pdf")

        r1 = await adapter.prepare_download(wrapper1, source)
        r2 = await adapter.prepare_download(wrapper2, source)
        assert r1.headers["RAONK_AUTH_KEY"] == "KEY_SHARED"
        assert r2.headers["RAONK_AUTH_KEY"] == "KEY_SHARED"
        assert adapter._resolve_target(wrapper1) is adapter._resolve_target(wrapper2)


# 9. DetailInventory hit: skipping detail API still ensures fresh download session
@pytest.mark.asyncio
async def test_woori_detail_inventory_hit_ensures_fresh_session_for_download(tmp_path: Path) -> None:
    detail_calls = 0
    landing_calls = {"client_a": 0, "client_b": 0, "client_c": 0}

    def make_handler(client_id: str):
        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal detail_calls
            if request.url.path == NOTICE_PATH:
                landing_calls[client_id] += 1
                return httpx.Response(
                    200,
                    headers={"set-cookie": f"SESS={client_id}; Path=/; Domain=pc.wooricard.com"},
                    text=(
                        f'<html><input id="raonkAuthKey" value="KEY_{client_id}" />'
                        '<input id="raonkAuthPageId" value="H1CCT211S09" /></html>'
                    ),
                    request=request,
                )
            if request.url.path == MAIN_PATH:
                return httpx.Response(
                    200,
                    json={
                        "elHeader": {"resSuc": True},
                        "mainDataList": {
                            "cct11PrdntcAgrmMainVo": [
                                {"goodsDescAt": "Y", "code": "P_HIT", "codeName": "Hit Card"}
                            ]
                        },
                    },
                    request=request,
                )
            if request.url.path == DETAIL_PATH:
                detail_calls += 1
                return httpx.Response(
                    200,
                    json={
                        "elHeader": {"resSuc": True},
                        "rows": [
                            {
                                "filePath": "/upload/hit.pdf",
                                "fileNm": "hit.pdf",
                                "beginDt": "20261001",
                                "gdccVer": "1",
                            }
                        ],
                    },
                    request=request,
                )
            return httpx.Response(404, request=request)

        return handler

    with WorkerState(tmp_path / "state.sqlite3") as state:
        inventory = DetailInventory(state)
        adapter = WooriAdapter(minimum_records=1)
        adapter.detail_inventory = inventory

        # 1. Run discovery with Client A -> populates DetailInventory
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(make_handler("client_a")), base_url=BASE_URL
        ) as client_a:
            snap1 = await adapter.discover_current(client_a)
            assert len(snap1.records) == 1
            assert detail_calls == 1

        # 2. Run discovery with Client B -> DetailInventory lookup hits, detail API skipped!
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(make_handler("client_b")), base_url=BASE_URL
        ) as client_b:
            snap2 = await adapter.discover_current(client_b)
            assert len(snap2.records) == 1
            assert detail_calls == 1  # Not incremented! Detail API skipped via inventory hit.

        # 3. Now download retained source using Client C (which has never run discovery)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(make_handler("client_c")), base_url=BASE_URL
        ) as client_c:
            req_c = await adapter.prepare_download(client_c, snap2.records[0])
            # Client C obtains its own fresh session and tokens
            assert req_c.headers["RAONK_AUTH_KEY"] == "KEY_client_c"
            assert landing_calls["client_c"] == 1
            assert client_c.cookies.get("SESS") == "client_c"


# 10. Async suspension in landing GET: single-flight waiting, cancellation lock recovery
@pytest.mark.asyncio
async def test_woori_single_flight_suspension_and_cancellation_recovery() -> None:
    gate = asyncio.Event()
    landing_starts = 0
    landing_finishes = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal landing_starts, landing_finishes
        if request.url.path == NOTICE_PATH:
            landing_starts += 1
            await gate.wait()
            landing_finishes += 1
            return httpx.Response(
                200,
                headers={"set-cookie": "SESS=suspended; Path=/; Domain=pc.wooricard.com"},
                text='<html><input id="raonkAuthKey" value="KEY_SUSPENDED" /><input id="raonkAuthPageId" value="H1CCT211S09" /></html>',
                request=request,
            )
        return httpx.Response(404, request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
        adapter = WooriAdapter()
        source = _make_source()

        # Task 1 starts landing GET and suspends on gate
        task1 = asyncio.create_task(adapter.prepare_download(client, source))
        await asyncio.sleep(0.01)
        assert landing_starts == 1
        assert landing_finishes == 0

        # Task 2 on the SAME client tries to prepare download -> waits on client lock
        task2 = asyncio.create_task(adapter.prepare_download(client, source))
        await asyncio.sleep(0.01)
        assert landing_starts == 1  # Single-flight: did not start a second landing GET!

        # Now cancel Task 1
        task1.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task1

        # Release the gate
        gate.set()

        # Task 2 should acquire the released lock, complete its landing GET, and succeed
        res2 = await task2
        assert res2.headers["RAONK_AUTH_KEY"] == "KEY_SUSPENDED"

        # Subsequent task on same client reuses cached session without any new landing GET
        res3 = await adapter.prepare_download(client, source)
        assert res3.headers["RAONK_AUTH_KEY"] == "KEY_SUSPENDED"
        assert landing_starts == 2  # Task 1 was cancelled during landing; Task 2 started fresh landing once.


# 11. Landing failure / malformed markup: exactly 0 unauthenticated downloads, typed exception
@pytest.mark.asyncio
async def test_woori_landing_failure_causes_zero_downloads_and_fails_closed() -> None:
    download_calls = 0

    def fail_handler(request: httpx.Request) -> httpx.Response:
        nonlocal download_calls
        if request.url.path == NOTICE_PATH:
            return httpx.Response(500, text="Internal Server Error", request=request)
        if request.url.path == RAONK_PATH:
            download_calls += 1
            return httpx.Response(200, content=pdf_bytes(), request=request)
        return httpx.Response(404, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail_handler), base_url=BASE_URL) as client:
        adapter = WooriAdapter()
        source = _make_source()

        with pytest.raises(httpx.HTTPStatusError):
            await adapter.prepare_download(client, source)
        # Exactly 0 downloads attempted without authentication
        assert download_calls == 0

        # Discovery also fails closed
        with pytest.raises(httpx.HTTPStatusError):
            await adapter.discover_current(client)


# 12. Token sentinel never leaks into database, JSON reports, SourceRecord or logs
@pytest.mark.asyncio
async def test_woori_sentinel_never_leaks_to_db_reports_or_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    sentinel = "SENTINEL_SECRET_TOKEN_WOORI_XYZ12345"
    valid_pdf = pdf_bytes(pages=1)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == NOTICE_PATH:
            return httpx.Response(
                200,
                headers={"set-cookie": f"WOORI_SESS=s_{sentinel}; Path=/; Domain=pc.wooricard.com"},
                text=(
                    f'<html><input type="hidden" id="raonkAuthKey" value="{sentinel}" />'
                    '<input type="hidden" id="raonkAuthPageId" value="H1CCT211S09" /></html>'
                ),
                request=request,
            )
        if request.url.path == RAONK_PATH:
            return httpx.Response(
                200,
                headers={"content-type": "application/pdf"},
                content=valid_pdf,
                request=request,
            )
        return httpx.Response(404, request=request)

    class StopAfterAcquisition(RuntimeError):
        pass

    class BarrierWebDAV(FakeWebDAV):
        async def validated_current_generation(self) -> Any:
            raise StopAfterAcquisition

    install_concurrent_pdf_http(monkeypatch, handler)
    source = _make_source()
    adapter = WooriAdapter(minimum_records=1)
    adapter.spec = replace(adapter.spec, minimum_records=1)

    caplog.set_level(logging.DEBUG)

    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[adapter],
            ocr=FakeOCR(),  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=BarrierWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
            retry_cap_seconds=0,
        )

        async def mock_discover(client: httpx.AsyncClient):
            from cardrag_worker.contracts import snapshot_from_records

            return snapshot_from_records(
                issuer="woori",
                source_url=BASE_URL + NOTICE_PATH,
                parser_version="woori.current.v1",
                records=[source],
                started_at=datetime.now(UTC),
            )

        adapter.discover_current = mock_discover  # type: ignore[assignment]

        run_id = state.start_run()
        with pytest.raises(StopAfterAcquisition):
            await pipeline._run_locked(run_id)

        # 1. Verify SQLite state DB contains zero occurrences of sentinel
        cursor = state.connection.cursor()
        for table_row in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            table_name = table_row[0]
            for col_row in cursor.execute(f"PRAGMA table_info({table_name})").fetchall():
                col_name = col_row[1]
                matches = cursor.execute(
                    f'SELECT COUNT(*) FROM "{table_name}" WHERE CAST("{col_name}" AS TEXT) LIKE ?',  # noqa: S608
                    (f"%{sentinel}%",),
                ).fetchone()[0]
                assert matches == 0, f"Sentinel leaked into table {table_name} column {col_name}"

        # 2. Verify JSON report files contain zero occurrences of sentinel
        reports_dir = tmp_path / "runs" / run_id / "reports"
        if reports_dir.is_dir():
            for report_file in reports_dir.glob("*.json"):
                assert sentinel not in report_file.read_text(encoding="utf-8")

        # 3. Verify SourceRecord payloads and metadata do not contain sentinel
        assert sentinel not in str(source.metadata)
        assert sentinel not in str(source.discovery_payload)
        assert sentinel not in source.source_id

        # 4. Verify logs contain zero occurrences of sentinel
        assert sentinel not in caplog.text


# 14. Server Set-Cookie deletion on 200 PDF and 304 Not Modified responses
@pytest.mark.asyncio
async def test_woori_server_set_cookie_deletion_on_pdf_and_304_responses(tmp_path: Path) -> None:
    valid_pdf = pdf_bytes(pages=1)
    landing_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal landing_calls
        if request.url.path == NOTICE_PATH:
            landing_calls += 1
            return httpx.Response(
                200,
                headers=[
                    ("Set-Cookie", f"SID=sid_{landing_calls}; Path=/; Secure; HttpOnly"),
                    ("Set-Cookie", "PATH_COOKIE=p_val; Path=/other; HttpOnly"),
                    ("Set-Cookie", "PARENT_COOKIE=par_val; Domain=.wooricard.com; Path=/"),
                ],
                text=(
                    f'<html><input type="hidden" id="raonkAuthKey" value="KEY_{landing_calls}" />'
                    '<input type="hidden" id="raonkAuthPageId" value="H1CCT211S09" /></html>'
                ),
                request=request,
            )
        if request.url.path == RAONK_PATH:
            if request.headers.get("if-none-match") == '"etag-test"':
                return httpx.Response(
                    304,
                    headers=[("Set-Cookie", "SID=; Path=/; Max-Age=0")],
                    request=request,
                )
            return httpx.Response(
                200,
                headers=[
                    ("Content-Type", "application/pdf"),
                    ("Set-Cookie", "SID=; Path=/; Max-Age=0"),
                ],
                content=valid_pdf,
                request=request,
            )
        return httpx.Response(404, request=request)

    adapter = WooriAdapter()
    source = _make_source(code="test_del", name="Card Del")

    downloader = SecurePDFDownloader(
        DownloadPolicy(
            allowed_hosts=frozenset({"pc.wooricard.com"}),
            allow_private_networks_for_tests=True,
        ),
        resolver=_public_ip,
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        # Part 1: 200 PDF response with Set-Cookie deletion
        req1 = await adapter.prepare_download(client, source)
        assert landing_calls == 1
        assert req1.headers["RAONK_AUTH_KEY"] == "KEY_1"
        assert client.cookies.get("SID") == "sid_1"

        dest1 = tmp_path / "doc1.pdf"
        await downloader.download(client, req1, dest1)
        adapter.sync_session(client)

        assert client.cookies.get("SID") is None
        session = adapter._sessions[client]
        session_cookie_names = {c.name for c in session.cookies.values()}
        assert "SID" not in session_cookie_names
        assert "PATH_COOKIE" in session_cookie_names
        assert "PARENT_COOKIE" in session_cookie_names
        assert not session.is_valid()

        # Part 2: Next document boundary clears client jar, prepare triggers fresh landing
        client.cookies.clear()
        req2 = await adapter.prepare_download(client, source)
        assert landing_calls == 2
        assert req2.headers["RAONK_AUTH_KEY"] == "KEY_2"
        assert client.cookies.get("SID") == "sid_2"
        assert client.cookies.get("SID") != "sid_1"

        # Part 3: 304 Not Modified response with Set-Cookie deletion
        from cardrag_worker.downloader import PDFNotModified

        req3 = replace(req2, headers={**req2.headers, "If-None-Match": '"etag-test"'})
        with pytest.raises(PDFNotModified):
            await downloader.download(client, req3, tmp_path / "doc3.pdf")
        adapter.sync_session(client)

        assert client.cookies.get("SID") is None
        session3 = adapter._sessions[client]
        assert "SID" not in {c.name for c in session3.cookies.values()}
        assert not session3.is_valid()

        client.cookies.clear()
        req4 = await adapter.prepare_download(client, source)
        assert landing_calls == 3
        assert req4.headers["RAONK_AUTH_KEY"] == "KEY_3"
        assert client.cookies.get("SID") == "sid_3"


# 15. Real WorkerPipeline sequentially processing two documents in the same slot
@pytest.mark.asyncio
async def test_woori_worker_pipeline_sequential_documents_same_slot_recovers_after_server_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid_pdf = pdf_bytes(pages=1)
    landing_calls = 0
    download_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal landing_calls
        if request.url.path == NOTICE_PATH:
            landing_calls += 1
            return httpx.Response(
                200,
                headers=[
                    ("Set-Cookie", f"SID=landing_sess_{landing_calls}; Path=/; Secure; HttpOnly"),
                ],
                text=(
                    f'<html><input type="hidden" id="raonkAuthKey" value="PIPELINE_KEY_{landing_calls}" />'
                    '<input type="hidden" id="raonkAuthPageId" value="H1CCT211S09" /></html>'
                ),
                request=request,
            )
        if request.url.path == RAONK_PATH:
            download_requests.append(request)
            if len(download_requests) == 1:
                return httpx.Response(
                    200,
                    headers=[
                        ("Content-Type", "application/pdf"),
                        ("Set-Cookie", "SID=; Path=/; Max-Age=0"),
                    ],
                    content=valid_pdf,
                    request=request,
                )
            return httpx.Response(
                200,
                headers=[("Content-Type", "application/pdf")],
                content=valid_pdf,
                request=request,
            )
        return httpx.Response(404, request=request)

    install_concurrent_pdf_http(monkeypatch, handler)

    adapter = WooriAdapter()
    adapter.spec = replace(adapter.spec, minimum_records=2)

    source1 = _make_source(code="pipe_1", name="Pipe Card 1", path="/upload/cardClause/2023/10/13/p1.pdf")
    source2 = _make_source(code="pipe_2", name="Pipe Card 2", path="/upload/cardClause/2023/10/13/p2.pdf")

    async def mock_discover(client: httpx.AsyncClient):
        from cardrag_worker.contracts import snapshot_from_records

        return snapshot_from_records(
            issuer="woori",
            source_url=BASE_URL + NOTICE_PATH,
            parser_version="woori.current.v1",
            records=[source1, source2],
            started_at=datetime.now(UTC),
        )

    adapter.discover_current = mock_discover  # type: ignore[assignment]

    class StopAfterAcquisition(RuntimeError):
        pass

    class BarrierWebDAV(FakeWebDAV):
        async def validated_current_generation(self) -> Any:
            raise StopAfterAcquisition

    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[adapter],
            ocr=FakeOCR(),  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=BarrierWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
            retry_cap_seconds=0,
            pdf_concurrency=1,
            pdf_concurrency_per_issuer=1,
        )

        run_id = state.start_run()
        with pytest.raises(StopAfterAcquisition):
            await pipeline._run_locked(run_id)

        assert len(download_requests) == 2
        assert download_requests[0].headers.get("RAONK_AUTH_KEY") == "PIPELINE_KEY_1"
        assert "SID=landing_sess_1" in download_requests[0].headers.get("Cookie", "")
        assert download_requests[1].headers.get("RAONK_AUTH_KEY") == "PIPELINE_KEY_2"
        assert "SID=landing_sess_2" in download_requests[1].headers.get("Cookie", "")
        assert "landing_sess_1" not in download_requests[1].headers.get("Cookie", "")
        assert landing_calls == 2


# 16. Same-name different path/domain cookie preservation & discovery-to-download same client re-auth
@pytest.mark.asyncio
async def test_woori_same_name_different_path_cookie_deletion_and_discovery_download_shared_client(
    tmp_path: Path,
) -> None:
    landing_calls = 0
    valid_pdf = pdf_bytes(pages=1)
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal landing_calls
        if request.url.path == NOTICE_PATH:
            landing_calls += 1
            return httpx.Response(
                200,
                headers=[
                    ("Set-Cookie", f"SID=required_{landing_calls}; Path=/; Secure; HttpOnly"),
                    ("Set-Cookie", "SID=unrelated_path; Path=/other; Secure; HttpOnly"),
                    ("Set-Cookie", "PARENT=parent_val; Domain=.wooricard.com; Path=/"),
                ],
                text=(
                    f'<html><input type="hidden" id="raonkAuthKey" value="KEY_{landing_calls}" />'
                    '<input type="hidden" id="raonkAuthPageId" value="H1CCT211S09" /></html>'
                ),
                request=request,
            )
        if request.url.path == MAIN_PATH:
            return httpx.Response(
                200,
                json={
                    "elHeader": {"resSuc": True},
                    "mainDataList": {
                        "cct11PrdntcAgrmMainVo": [
                            {"goodsDescAt": "Y", "code": "P1", "codeName": "Test Card P1"}
                        ]
                    },
                },
                request=request,
            )
        if request.url.path == DETAIL_PATH:
            return httpx.Response(
                200,
                json={
                    "elHeader": {"resSuc": True},
                    "cct11PrdntcAgrmBscVo": {
                        "filePath": "/upload/cardClause/2023/10/13/p1.pdf",
                        "fileNm": "p1.pdf",
                        "beginDt": "20231013",
                        "gdccVer": "1.0",
                    },
                },
                request=request,
            )
        if request.url.path == RAONK_PATH:
            captured_requests.append(request)
            return httpx.Response(
                200,
                headers=[("Content-Type", "application/pdf")],
                content=valid_pdf,
                request=request,
            )
        return httpx.Response(404, request=request)

    adapter = WooriAdapter(minimum_records=1)
    downloader = SecurePDFDownloader(
        DownloadPolicy(
            allowed_hosts=frozenset({"pc.wooricard.com"}),
            allow_private_networks_for_tests=True,
        ),
        resolver=_public_ip,
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        # Step 1: Run real discovery_current on this client
        snapshot = await adapter.discover_current(client)
        assert len(snapshot.records) == 1
        assert landing_calls == 1

        # Check session established during discovery:
        session = adapter._sessions[client]
        assert session.auth_key == "KEY_1"
        assert len(session.landing_cookie_keys) == 3
        assert session.is_valid()

        # Step 2: Server deletes the root 'SID' cookie, but 'SID' with Path=/other remains
        req_del = httpx.Request("GET", "https://pc.wooricard.com/delete")
        resp_del = httpx.Response(200, request=req_del, headers=[("Set-Cookie", "SID=; Path=/; Max-Age=0")])
        client.cookies.extract_cookies(resp_del)

        # 'SID' on /other remains in session.cookies
        remaining_paths = {c.path for c in session.cookies.values() if c.name == "SID"}
        assert remaining_paths == {"/other"}
        # But required root cookie was deleted, so is_valid() MUST be False!
        assert not session.is_valid()

        # Step 3: Pipeline clears client jar between document boundaries
        client.cookies.clear()

        # Step 4: Now prepare and download the discovered record using the SAME client
        source = snapshot.records[0]
        req = await adapter.prepare_download(client, source)

        # Fresh landing must have been executed exactly once
        assert landing_calls == 2
        assert req.headers["RAONK_AUTH_KEY"] == "KEY_2"

        dest = tmp_path / "shared_client.pdf"
        dl = await downloader.download(client, req, dest)
        assert dl.page_count == 1

        assert len(captured_requests) == 1
        assert captured_requests[0].headers.get("RAONK_AUTH_KEY") == "KEY_2"
        cookie_header = captured_requests[0].headers.get("Cookie", "")
        assert "SID=required_2" in cookie_header
        assert "required_1" not in cookie_header
        assert "unrelated_path" not in cookie_header


# 17. Preserve distinct paths with and without trailing slash (/dcpc vs /dcpc/) across clear/restore/deletion
@pytest.mark.asyncio
async def test_woori_cookie_paths_with_and_without_trailing_slash_preserved(
    tmp_path: Path,
) -> None:
    landing_calls = 0
    valid_pdf = pdf_bytes(pages=1)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal landing_calls
        if request.url.path == NOTICE_PATH:
            landing_calls += 1
            return httpx.Response(
                200,
                headers=[
                    ("Set-Cookie", f"SID=plain_{landing_calls}; Path=/dcpc; Secure; HttpOnly"),
                    ("Set-Cookie", f"SID=slash_{landing_calls}; Path=/dcpc/; Secure; HttpOnly"),
                ],
                text=(
                    f'<html><input type="hidden" id="raonkAuthKey" value="KEY_{landing_calls}" />'
                    '<input type="hidden" id="raonkAuthPageId" value="H1CCT211S09" /></html>'
                ),
                request=request,
            )
        if request.url.path == "/delete_plain":
            return httpx.Response(
                200,
                headers=[("Set-Cookie", "SID=; Path=/dcpc; Max-Age=0")],
                request=request,
            )
        if request.url.path == RAONK_PATH:
            return httpx.Response(
                200,
                headers=[("Content-Type", "application/pdf")],
                content=valid_pdf,
                request=request,
            )
        return httpx.Response(404, request=request)

    adapter = WooriAdapter()
    source = _make_source()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        # Step 1: Initial landing capture
        await adapter._ensure_session(client)
        assert landing_calls == 1

        session = adapter._sessions[client]
        assert len(session.cookies) == 2
        paths = {c.path: c for c in session.cookies.values()}
        assert "/dcpc" in paths
        assert "/dcpc/" in paths
        assert paths["/dcpc"].value == "plain_1"
        assert paths["/dcpc/"].value == "slash_1"
        assert paths["/dcpc"].secure is True
        assert paths["/dcpc/"].secure is True

        # Step 2: Document boundary clear and restore
        client.cookies.clear()
        assert len(client.cookies) == 0

        await adapter._ensure_session(client)
        assert len(client.cookies.jar) == 2
        restored_paths = {c.path: c for c in client.cookies.jar}
        assert "/dcpc" in restored_paths
        assert "/dcpc/" in restored_paths
        assert restored_paths["/dcpc"].value == "plain_1"
        assert restored_paths["/dcpc/"].value == "slash_1"

        # Check request headers to respective endpoints
        req_plain = client.build_request("GET", "https://pc.wooricard.com/dcpc")
        assert req_plain.headers.get("Cookie") == "SID=plain_1"

        req_slash = client.build_request("GET", "https://pc.wooricard.com/dcpc/")
        cookie_slash = req_slash.headers.get("Cookie", "")
        assert "SID=slash_1" in cookie_slash
        assert "SID=plain_1" in cookie_slash

        # Step 3: Deletion of only one path (/dcpc) preserves the other (/dcpc/)
        await client.get("https://pc.wooricard.com/delete_plain")
        remaining = list(adapter._sessions[client].cookies.values())
        assert len(remaining) == 1
        assert remaining[0].path == "/dcpc/"
        assert remaining[0].value == "slash_1"
        assert not session.is_valid()

        # Step 4: Next prepare triggers fresh landing recovery
        client.cookies.clear()
        req = await adapter.prepare_download(client, source)
        assert landing_calls == 2
        assert req.headers["RAONK_AUTH_KEY"] == "KEY_2"
        refreshed_paths = {c.path: c.value for c in adapter._sessions[client].cookies.values()}
        assert refreshed_paths["/dcpc"] == "plain_2"
        assert refreshed_paths["/dcpc/"] == "slash_2"
