"""Common transient-origin classification and bounded retry behavior."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from helpers import pdf_bytes
from test_pipeline import (
    Adapter,
    FakeEmbeddings,
    FakeOCR,
    FakeWebDAV,
    install_concurrent_pdf_http,
    source,
)

from cardrag_worker.downloader import DownloadPolicy, DownloadRequest, SecurePDFDownloader
from cardrag_worker.issuer_collection import (
    IssuerCollectionError,
    OriginServiceError,
    classify_html_service_error,
    origin_failure,
)
from cardrag_worker.issuer_metrics import IssuerHTTPMetrics
from cardrag_worker.pipeline import (
    OCRSystemicFailureError,
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


@pytest.mark.asyncio
async def test_downloader_classifies_http200_html_service_page_without_writing_pdf(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=UTF-8"},
            content=SERVICE_HTML,
            request=request,
        )

    destination = tmp_path / "result.pdf"
    downloader = SecurePDFDownloader(
        DownloadPolicy(allowed_hosts=frozenset({"cards.example"})), resolver=_public_ip
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OriginServiceError) as captured:
            await downloader.download(
                client,
                DownloadRequest(url="https://cards.example/download", method="POST"),
                destination,
            )
    assert captured.value.reason_code == "issuer_origin_service_error"
    assert captured.value.status_code == 200
    assert captured.value.mime_type == "text/html"
    assert captured.value.service_signal == "visible_service_error_page"
    assert not destination.exists()
    assert "서비스가 원활하지 않습니다" not in str(captured.value)


@pytest.mark.asyncio
async def test_rate_limited_client_classifies_html_catalog_service_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=UTF-8"},
            content=SERVICE_HTML,
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        limited = RateLimitedClient(client, IssuerRateLimiter(0), issuer="woori", purpose="catalog")
        with pytest.raises(IssuerCollectionError) as captured:
            await limited.get("https://cards.example/list")
    assert captured.value.reason_code == "issuer_origin_service_error"
    assert captured.value.status_code == 200
    assert captured.value.mime_type == "text/html"
    assert captured.value.service_signal == "visible_service_error_page"


def test_origin_failure_preserves_bounded_service_error_fields() -> None:
    original = OriginServiceError(
        status_code=200,
        mime_type="text/html",
        service_signal="visible_service_error_page",
    )
    failure = origin_failure(original)
    assert failure.reason_code == "issuer_origin_service_error"
    assert failure.exception_class == "OriginServiceError"
    assert (failure.status_code, failure.mime_type, failure.service_signal) == (
        200,
        "text/html",
        "visible_service_error_page",
    )


def test_classifier_ignores_script_comment_hidden_and_normal_product_text() -> None:
    normal = (
        b"<html><head><title>Card catalog</title>"
        b'<script>const errors={temporary:"Service unavailable"};</script>'
        b"<!-- Service unavailable in a hidden comment -->"
        b"</head><body><h1>Credit cards</h1>"
        b'<div data-card="a">Normal product listing; one benefit is currently unavailable.</div>'
        b'<div style="display:none">Service unavailable hidden template</div>'
        b"<div hidden>Service unavailable hidden attribute</div>"
        b"<template>Service unavailable template</template>"
        b"</body></html>"
    )
    assert classify_html_service_error(normal, "text/html; charset=UTF-8") is None

    normal_with_visible_product = (
        b"<html><body><h1>Credit cards</h1>"
        b"<p>Some optional benefits are temporarily unavailable.</p></body></html>"
    )
    assert classify_html_service_error(normal_with_visible_product, "text/html") is None


@pytest.mark.parametrize("encoding", ["utf-8", "cp949"])
def test_classifier_accepts_clear_visible_service_error_pages(encoding: str) -> None:
    html = (
        "<html><head><title>우리카드</title></head><body>"
        "<div class='pageTit'><h1>Error</h1></div>"
        "<section class='finishBox fail'><p>현재 홈페이지 서비스가 원활하지 않습니다.</p></section>"
        "</body></html>"
    ).encode(encoding)
    assert classify_html_service_error(html, "text/html; charset=UTF-8") == "visible_service_error_page"


def test_classifier_accepts_clear_generic_503_style_html() -> None:
    html = (
        b"<html><head><title>503 Service Unavailable</title></head>"
        b"<body><h1>Service Unavailable</h1></body></html>"
    )
    assert classify_html_service_error(html, "text/html") == "visible_service_error_page"


def test_classifier_ignores_empty_error_region_and_unrelated_product_availability() -> None:
    html = (
        b"<html><head><title>Card catalog</title></head><body>"
        b"<h1>Credit cards</h1><div class='error-message'></div>"
        b"<ul><li data-card='A'>Card A</li>"
        b"<li data-card='B' class='error-container'>Card B - airport pickup currently unavailable</li>"
        b"</ul>"
        b"</body></html>"
    )
    assert classify_html_service_error(html, "text/html") is None


def test_classifier_accepts_service_error_after_hidden_void_elements() -> None:
    html = (
        b"<html><head><title>Bank</title></head><body>"
        b"<input hidden name='csrf'><br><img src='x'>"
        b"<h1>Error</h1><p>Service unavailable</p>"
        b"</body></html>"
    )
    assert classify_html_service_error(html, "text/html") == "visible_service_error_page"


async def _normal_catalog_client(issuer: str) -> httpx.Response:
    html = (
        b"<html><head><title>Card catalog</title></head><body>"
        b"<h1>Credit cards</h1><div class='error-message'></div>"
        b"<ul><li>Card A</li><li>Card B - airport pickup currently unavailable</li></ul>"
        b"</body></html>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/html; charset=UTF-8"}, content=html, request=request
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        limited = RateLimitedClient(client, IssuerRateLimiter(0), issuer=issuer, purpose="catalog")
        return await limited.get("https://cards.example/list")


@pytest.mark.asyncio
async def test_rate_limited_client_keeps_catalog_with_empty_error_slot() -> None:
    assert (await _normal_catalog_client("kb")).status_code == 200
    assert (await _normal_catalog_client("woori")).status_code == 200


@pytest.mark.asyncio
async def test_rate_limited_client_keeps_normal_catalog_for_second_issuer() -> None:
    html = (
        b"<html><head><title>Card catalog</title>"
        b'<script>const errors={temporary:"Service unavailable"};</script>'
        b"</head><body><h1>Credit cards</h1><div>Normal product listing</div></body></html>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/html; charset=UTF-8"}, content=html, request=request
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        limited = RateLimitedClient(client, IssuerRateLimiter(0), issuer="kb", purpose="catalog")
        response = await limited.get("https://cards.example/list")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_rate_limited_client_blocks_clear_service_error_for_second_issuer() -> None:
    html = SERVICE_HTML + b"RAW_HTML_SECRET_SENTINEL"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/html; charset=UTF-8"}, content=html, request=request
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        limited = RateLimitedClient(client, IssuerRateLimiter(0), issuer="shinhan", purpose="catalog")
        with pytest.raises(IssuerCollectionError) as captured:
            await limited.get("https://cards.example/list")
    assert captured.value.reason_code == "issuer_origin_service_error"
    assert captured.value.service_signal == "visible_service_error_page"
    rendered = str(captured.value) + repr(captured.value.args)
    assert "서비스가 원활하지 않습니다" not in rendered
    assert "RAW_HTML_SECRET_SENTINEL" not in rendered


@pytest.mark.asyncio
async def test_pdf_origin_service_error_retries_then_uses_normal_pdf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = pdf_bytes()
    requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if len(requests) == 1:
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=UTF-8"},
                content=SERVICE_HTML,
                request=request,
            )
        return httpx.Response(
            200,
            headers={"content-type": "application/pdf"},
            content=payload,
            request=request,
        )

    install_concurrent_pdf_http(monkeypatch, handler)
    record = source()
    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[Adapter((record,))],
            ocr=FakeOCR(),  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
            retry_cap_seconds=0,
        )
        run_id = state.start_run()
        with pytest.raises(OCRSystemicFailureError):
            await pipeline._run_locked(run_id)  # noqa: SLF001

        assert requests == ["/current.pdf", "/current.pdf"]
        stage = state.get_stage(run_id, record.source_id, "download")
        assert stage is not None
        assert (stage.status, stage.attempt_count) == ("succeeded", 2)


@pytest.mark.asyncio
async def test_persistent_discovery_service_error_reports_typed_worker_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[str] = []

    class HtmlDiscoveryAdapter(Adapter):
        async def discover_current(self, client: Any) -> Any:
            await client.get("https://cards.example/list")
            raise AssertionError("service HTML should stop discovery")

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=UTF-8"},
            content=SERVICE_HTML,
            request=request,
        )

    install_concurrent_pdf_http(monkeypatch, handler)
    adapter = HtmlDiscoveryAdapter(())
    adapter.spec = replace(adapter.spec, maximum_retries=2)
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
        with pytest.raises(WorkerUnexpectedFailureError) as captured:
            await pipeline.run()

        failure = captured.value.failure
        assert failure.error_class_category == "issuer_origin"
        assert failure.reason_code == "issuer_origin_service_error"
        assert failure.stage == "discovery"
        assert failure.document_id is None
        assert (failure.status_code, failure.mime_type) == (200, "text/html")
        assert len(requests) == 2
        report = json.loads(captured.value.report_path.read_bytes())
        assert report["stage"] == "discovery"
        run_id = state.connection.execute("SELECT run_id FROM run").fetchone()[0]
        collection_report = json.loads(
            (tmp_path / "runs" / str(run_id) / "reports" / "issuer-collection.json").read_bytes()
        )
        outcome = next(row for row in collection_report["issuers"] if row["issuer"] == "testbank")
        assert outcome["reason_code"] == "issuer_origin_service_error"
        assert outcome["origin_status_code"] == 200
        assert outcome["origin_mime_type"] == "text/html"
        assert outcome["source_id"] is None


@pytest.mark.asyncio
async def test_persistent_pdf_service_error_reports_typed_worker_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=UTF-8"},
            content=SERVICE_HTML,
            request=request,
        )

    install_concurrent_pdf_http(monkeypatch, handler)
    record = source()
    adapter = Adapter((record,))
    adapter.spec = replace(adapter.spec, maximum_retries=2)
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
        with pytest.raises(WorkerUnexpectedFailureError) as captured:
            await pipeline.run()

        failure = captured.value.failure
        assert failure.error_class_category == "issuer_origin"
        assert failure.reason_code == "issuer_origin_service_error"
        assert failure.stage == "download"
        assert failure.document_id == record.source_id
        assert (failure.status_code, failure.mime_type) == (200, "text/html")
        assert len(requests) == 2
        report = json.loads(captured.value.report_path.read_bytes())
        assert report["mime_type"] == "text/html"
        run_id = state.connection.execute("SELECT run_id FROM run").fetchone()[0]
        collection_report = json.loads(
            (tmp_path / "runs" / str(run_id) / "reports" / "issuer-collection.json").read_bytes()
        )
        outcome = next(row for row in collection_report["issuers"] if row["issuer"] == "testbank")
        assert outcome["origin_status_code"] == 200
        assert outcome["origin_mime_type"] == "text/html"
        assert outcome["origin_service_signal"] == "visible_service_error_page"
        assert outcome["source_id"] == record.source_id


@pytest.mark.asyncio
async def test_503_retry_after_sets_persistent_cooldown(tmp_path: Path) -> None:
    with WorkerState(tmp_path / "state.sqlite3") as state:
        metrics = IssuerHTTPMetrics(state, "run", request_limit=100, byte_limit=100000)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    503,
                    headers={"retry-after": "60"},
                    content=b"temporarily unavailable",
                    request=request,
                )
            ),
            event_hooks={"request": [metrics.before], "response": [metrics.after]},
        ) as client:
            limited = RateLimitedClient(client, IssuerRateLimiter(0), issuer="woori", purpose="pdf")
            response = await limited.get("https://cards.example/pdf")
            assert response.status_code == 503
            with pytest.raises(IssuerCollectionError, match="cooldown"):
                await limited.get("https://cards.example/pdf")


@pytest.mark.asyncio
async def test_503_without_retry_after_does_not_create_cooldown(tmp_path: Path) -> None:
    with WorkerState(tmp_path / "state.sqlite3") as state:
        metrics = IssuerHTTPMetrics(state, "run", request_limit=100, byte_limit=100000)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(503, content=b"unavailable", request=request)
            ),
            event_hooks={"request": [metrics.before], "response": [metrics.after]},
        ) as client:
            limited = RateLimitedClient(client, IssuerRateLimiter(0), issuer="woori", purpose="pdf")
            assert (await limited.get("https://cards.example/pdf")).status_code == 503
            assert (await limited.get("https://cards.example/pdf")).status_code == 503
