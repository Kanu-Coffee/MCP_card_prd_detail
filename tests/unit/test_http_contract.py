from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.provider import AccessToken, TokenVerifier
from psycopg.errors import QueryCanceled
from pydantic import SecretStr

from cardrag.config import Settings
from cardrag.mcp_server import build_app
from cardrag.service.auth import _make_access_token
from cardrag.service.models import (
    AuditEvent,
    EvidencePage,
    ProductVersions,
    ReadinessStatus,
    SearchPage,
    SearchRequest,
    SourcePage,
    SourcePdf,
)
from tests.support_pdf import write_synthetic_pdf


class FakeTokenVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        scopes = {
            "search-token": ["search"],
            "source-token": ["search", "source_pdf"],
        }.get(token)
        if scopes is None:
            return None
        return _make_access_token(
            token=token,
            client_id="test-client",
            scopes=scopes,
            expires_at=2_000_000_000,
            resource="cardrag-mcp",
            subject="test-user",
            claims={"sub": "test-user"},
        )


class HttpRepository:
    def __init__(self, source: SourcePdf, source_page: SourcePage) -> None:
        self.source = source
        self.source_page = source_page
        self.audits: list[AuditEvent] = []
        self.metric_rollups: list[dict[str, object]] = []

    async def search_evidence(self, request: SearchRequest) -> SearchPage:
        return SearchPage(
            generation_id="gen-http",
            items=[],
            no_evidence=True,
            warnings=("no_evidence",),
        )

    async def get_evidence(
        self,
        evidence_id: str,
        *,
        cursor: str | None,
        limit: int,
    ) -> EvidencePage | None:
        return None

    async def get_product_versions(
        self,
        issuer: str,
        product_code: str,
        *,
        as_of: date | None,
    ) -> ProductVersions:
        return ProductVersions(
            generation_id="gen-http",
            issuer=issuer,
            product_code=product_code,
            items=[],
        )

    async def get_source_pdf(self, document_id: str) -> SourcePdf | None:
        return self.source if document_id == self.source.document_id else None

    async def get_source_page(self, document_id: str, page: int) -> SourcePage | None:
        if document_id == self.source_page.document_id and page == self.source_page.page:
            return self.source_page
        return None

    async def readiness(self) -> ReadinessStatus:
        return ReadinessStatus(
            ready=True,
            generation_id="gen-http",
            checks={"schema": True, "generation": True, "indexes": True},
        )

    async def record_audit(self, event: AuditEvent) -> None:
        self.audits.append(event)

    async def record_mcp_metric(
        self,
        *,
        operation: str,
        outcome: str,
        duration: float,
    ) -> None:
        self.metric_rollups.append({"operation": operation, "outcome": outcome, "duration": duration})


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        environment="test",
        database_url=SecretStr("postgresql://unused"),
        storage_root=tmp_path / "storage",
        generation_root=tmp_path / "generation",
        build_root=tmp_path / "build",
        page_cache_root=tmp_path / "page-cache",
        mcp_server_url="http://test/mcp",
        oidc_issuer="https://id.example/realms/cardrag",
    )


@pytest.fixture
def http_app(tmp_path: Path) -> tuple[Any, HttpRepository, bytes]:
    settings = _settings(tmp_path)
    for path in (
        settings.storage_root,
        settings.generation_root,
        settings.build_root,
        settings.page_cache_root,
    ):
        path.mkdir(parents=True)
    pdf_path = settings.storage_root / "source.pdf"
    write_synthetic_pdf(pdf_path, ["CardRAG exact immutable page"])
    content = pdf_path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    source = SourcePdf(
        document_id="woori:card-1:v1",
        issuer="woori",
        product_code="card-1",
        version="v1",
        path=pdf_path,
        sha256=digest,
        size_bytes=len(content),
    )
    source_page = SourcePage(
        document_id=source.document_id,
        issuer=source.issuer,
        product_code=source.product_code,
        version=source.version,
        page=1,
        page_count=1,
        ocr_text="CardRAG exact immutable page",
        ocr_sha256=hashlib.sha256(b"CardRAG exact immutable page").hexdigest(),
        pdf_sha256=digest,
    )
    repository = HttpRepository(source, source_page)
    app = build_app(repository, settings, token_verifier=FakeTokenVerifier())
    return app, repository, content


@pytest.mark.asyncio
async def test_health_and_readiness_are_public(http_app: tuple[Any, HttpRepository, bytes]) -> None:
    app, _, _ = http_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        live = await client.get("/health/live")
        ready = await client.get("/health/ready")
        metadata = await client.get("/.well-known/oauth-protected-resource/mcp")

    assert live.status_code == 200
    assert live.json() == {"status": "ok"}
    assert ready.status_code == 200
    assert ready.json()["generation_id"] == "gen-http"
    assert metadata.status_code == 200
    assert metadata.json()["scopes_supported"] == ["search", "source_pdf"]


@pytest.mark.asyncio
async def test_metrics_are_loopback_only_and_contain_no_query_or_token_labels(
    http_app: tuple[Any, HttpRepository, bytes],
) -> None:
    app, _, _ = http_app
    loopback_transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    remote_transport = httpx.ASGITransport(app=app, client=("203.0.113.20", 12345))
    async with httpx.AsyncClient(transport=loopback_transport, base_url="http://test") as client:
        await client.get(
            "/health/live?query=private-search",
            headers={"Authorization": "Bearer private-token"},
        )
        allowed = await client.get("/metrics")
    async with httpx.AsyncClient(transport=remote_transport, base_url="http://test") as client:
        denied = await client.get(
            "/metrics",
            headers={"X-Forwarded-For": "127.0.0.1"},
        )

    assert allowed.status_code == 200
    assert denied.status_code == 404
    assert allowed.headers["cache-control"] == "no-store"
    assert b'route="/health/live"' in allowed.content
    assert b"private-search" not in allowed.content
    assert b"private-token" not in allowed.content


@pytest.mark.asyncio
async def test_pdf_requires_source_scope_and_advertises_rfc9728_metadata(
    http_app: tuple[Any, HttpRepository, bytes],
) -> None:
    app, _, _ = http_app
    path = "/sources/woori:card-1:v1/pdf"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        missing = await client.get(path)
        insufficient = await client.get(
            path,
            headers={"Authorization": "Bearer search-token"},
        )

    assert missing.status_code == 401
    assert "oauth-protected-resource/mcp" in missing.headers["www-authenticate"]
    assert insufficient.status_code == 403
    assert 'scope="source_pdf"' in insufficient.headers["www-authenticate"]


@pytest.mark.asyncio
async def test_auth_failures_are_audited_without_bearer_or_query_data(
    http_app: tuple[Any, HttpRepository, bytes],
) -> None:
    app, repository, _ = http_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/sources/woori:card-1:v1/pdf?token=never-store-this")

    assert response.status_code == 401
    assert len(repository.audits) == 1
    event = repository.audits[0]
    assert event.outcome == "denied"
    assert event.subject_hash == hashlib.sha256(b"https://id.example/realms/cardrag\x00anonymous").hexdigest()
    assert event.source_sha256 is None
    assert "never-store-this" not in event.model_dump_json()


@pytest.mark.asyncio
async def test_pdf_supports_single_range_and_records_non_query_audit(
    http_app: tuple[Any, HttpRepository, bytes],
) -> None:
    app, repository, content = http_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/sources/woori:card-1:v1/pdf",
            headers={
                "Authorization": "Bearer source-token",
                "Range": "bytes=0-7",
            },
        )

    assert response.status_code == 206
    assert response.content == content[:8]
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["x-content-sha256"] == hashlib.sha256(content).hexdigest()
    assert len(repository.audits) == 1
    assert repository.audits[0].requested_range == "bytes=0-7"
    assert repository.audits[0].action == "source_pdf"
    assert repository.audits[0].granted_scopes == ("search", "source_pdf")
    assert repository.audits[0].request_id == response.headers["x-request-id"]
    assert repository.metric_rollups[-1]["operation"] == "source_pdf"
    assert repository.metric_rollups[-1]["outcome"] == "success"
    assert set(repository.metric_rollups[-1]) == {"operation", "outcome", "duration"}


@pytest.mark.asyncio
async def test_invalid_multi_range_is_rejected_and_audited(
    http_app: tuple[Any, HttpRepository, bytes],
) -> None:
    app, repository, _ = http_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/sources/woori:card-1:v1/pdf",
            headers={
                "Authorization": "Bearer source-token",
                "Range": "bytes=0-1,3-4 Bearer never-persist-this",
            },
        )

    assert response.status_code == 416
    assert response.headers["content-range"].startswith("bytes */")
    assert repository.audits[0].outcome == "denied"
    assert repository.audits[0].requested_range == "invalid"
    assert "never-persist-this" not in repository.audits[0].model_dump_json()
    assert repository.metric_rollups[-1]["operation"] == "source_pdf"
    assert repository.metric_rollups[-1]["outcome"] == "denied"
    assert "never-persist-this" not in repr(repository.metric_rollups)


@pytest.mark.asyncio
async def test_page_png_requires_source_scope_and_uses_ttl_cache(
    http_app: tuple[Any, HttpRepository, bytes],
) -> None:
    app, repository, _ = http_app
    path = "/sources/woori:card-1:v1/pages/1.png"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        denied = await client.get(path, headers={"Authorization": "Bearer search-token"})
        allowed = await client.get(path, headers={"Authorization": "Bearer source-token"})

    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert allowed.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert allowed.headers["cache-control"] == "private, max-age=604800"
    assert repository.audits[-1].action == "source_page_png"
    assert repository.audits[-1].page == 1
    assert [(metric["operation"], metric["outcome"]) for metric in repository.metric_rollups] == [
        ("source_page_png", "denied"),
        ("source_page_png", "success"),
    ]


@pytest.mark.asyncio
async def test_pdf_route_is_catalog_bound(http_app: tuple[Any, HttpRepository, bytes]) -> None:
    app, repository, _ = http_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        unknown = await client.get(
            "/sources/not-in-catalog/pdf",
            headers={"Authorization": "Bearer source-token"},
        )
        traversal = await client.get(
            "/sources/%2E%2E%5Csecret/pdf",
            headers={"Authorization": "Bearer source-token"},
        )
        sensitive = await client.get(
            "/sources/Bearer-never-persist-document-input/pdf",
            headers={"Authorization": "Bearer source-token"},
        )

    assert unknown.status_code == 404
    assert traversal.status_code in {400, 404}
    assert sensitive.status_code == 404
    assert "never-persist-document-input" not in "".join(
        event.model_dump_json() for event in repository.audits
    )


@pytest.mark.asyncio
async def test_mcp_endpoint_requires_bearer_token(
    http_app: tuple[Any, HttpRepository, bytes],
) -> None:
    app, repository, _ = http_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/mcp",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )

    assert response.status_code == 401
    assert "oauth-protected-resource/mcp" in response.headers["www-authenticate"]
    assert len(repository.audits) == 1
    assert repository.audits[0].action == "mcp_transport_auth"
    assert repository.audits[0].outcome == "denied"
    assert repository.metric_rollups[-1]["operation"] == "mcp_transport_auth"
    assert repository.metric_rollups[-1]["outcome"] == "denied"
    assert (
        repository.audits[0].subject_hash
        == hashlib.sha256(b"https://id.example/realms/cardrag\x00anonymous").hexdigest()
    )


@pytest.mark.asyncio
async def test_mcp_sdk_lists_tools_and_resource_templates(
    http_app: tuple[Any, HttpRepository, bytes],
) -> None:
    app, repository, _ = http_app
    # MCP 2 uses httpx2 internally and accepts a custom ASGI transport, which
    # gives this test an end-to-end protocol check without opening a socket.
    import httpx2

    http_client = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer search-token"},
    )
    async with (
        app.router.lifespan_context(app),
        http_client,
        streamable_http_client(
            "http://test/mcp",
            http_client=http_client,
            terminate_on_close=False,
        ) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        resources = await session.list_resources()
        templates = await session.list_resource_templates()
        issuer_catalog = await session.read_resource("cardrag://catalog/issuers")
        document_metadata = await session.read_resource("cardrag://documents/woori:card-1:v1")
        ocr_page = await session.read_resource("cardrag://sources/woori:card-1:v1/ocr/pages/1")
        search_result = await session.call_tool(
            "search_evidence",
            {"query": "annual fee"},
        )
        denied_source = await session.call_tool(
            "get_source_pdf",
            {"document_id": "woori:card-1:v1"},
        )

    assert {tool.name for tool in tools.tools} == {
        "search_evidence",
        "get_evidence",
        "get_product_versions",
        "get_source_pdf",
        "get_source_page",
    }
    assert {str(resource.uri) for resource in resources.resources} >= {
        "cardrag://catalog/issuers",
        "cardrag://catalog/index-status",
    }
    assert {str(template.uri_template) for template in templates.resource_templates} >= {
        "cardrag://evidence/{evidence_id}",
        "cardrag://documents/{document_id}",
        "cardrag://sources/{document_id}/ocr",
        "cardrag://sources/{document_id}/ocr/pages/{page}",
    }
    assert "woori" in issuer_catalog.contents[0].text
    assert hashlib.sha256(http_app[2]).hexdigest() in document_metadata.contents[0].text
    assert "CardRAG exact immutable page" in ocr_page.contents[0].text
    assert search_result.is_error is False
    assert denied_source.is_error is True
    assert [(event.action, event.outcome) for event in repository.audits] == [
        ("issuer_catalog", "success"),
        ("document_resource", "success"),
        ("source_ocr_page_resource", "success"),
        ("search_evidence", "no_result"),
        ("get_source_pdf", "denied"),
    ]
    assert all(event.request_id.startswith("req_") for event in repository.audits)
    assert (
        repository.audits[0].subject_hash
        == hashlib.sha256(b"https://id.example/realms/cardrag\x00test-user").hexdigest()
    )
    assert repository.audits[0].granted_scopes == ("search",)
    assert repository.audits[1].document_id == ("invalid_" + hashlib.sha256(b"woori:card-1:v1").hexdigest())
    assert "annual fee" not in "".join(event.model_dump_json() for event in repository.audits)
    assert [(str(metric["operation"]), str(metric["outcome"])) for metric in repository.metric_rollups] == [
        ("issuer_catalog", "success"),
        ("document_resource", "success"),
        ("source_ocr_page_resource", "success"),
        ("search_evidence", "no_result"),
        ("get_source_pdf", "denied"),
    ]
    assert all(set(metric) == {"operation", "outcome", "duration"} for metric in repository.metric_rollups)
    assert "annual fee" not in repr(repository.metric_rollups)
    assert "search-token" not in repr(repository.metric_rollups)
    metrics = app.state.cardrag_observability.metrics.render()
    assert b'operation="search_evidence",outcome="no_result"' in metrics
    assert b'operation="get_source_pdf",outcome="denied"' in metrics
    assert b"annual fee" not in metrics


@pytest.mark.asyncio
async def test_mcp_timeout_is_audited_and_measured_without_query(
    http_app: tuple[Any, HttpRepository, bytes],
) -> None:
    app, repository, _ = http_app
    import httpx2

    app.state.cardrag_query_service._timeout = 0.01

    async def slow_search(request: SearchRequest) -> SearchPage:
        del request
        await asyncio.sleep(1)
        return SearchPage(generation_id="gen-http", items=[])

    repository.search_evidence = slow_search  # type: ignore[method-assign]
    http_client = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer search-token"},
    )
    async with (
        app.router.lifespan_context(app),
        http_client,
        streamable_http_client(
            "http://test/mcp",
            http_client=http_client,
            terminate_on_close=False,
        ) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        started = time.perf_counter()
        result = await session.call_tool(
            "search_evidence",
            {"query": "private timeout phrase"},
        )

    assert time.perf_counter() - started < 0.25
    assert result.is_error is True
    assert repository.audits[-1].action == "search_evidence"
    assert repository.audits[-1].outcome == "timeout"
    audit_json = repository.audits[-1].model_dump_json()
    metrics = app.state.cardrag_observability.metrics.render()
    assert "private timeout phrase" not in audit_json
    assert b'operation="search_evidence",outcome="timeout"' in metrics
    assert b"private timeout phrase" not in metrics


@pytest.mark.asyncio
async def test_mcp_postgres_statement_timeout_records_timeout_outcome(
    http_app: tuple[Any, HttpRepository, bytes],
) -> None:
    app, repository, _ = http_app
    import httpx2

    async def cancelled_search(request: SearchRequest) -> SearchPage:
        del request
        raise QueryCanceled("private SQL from statement_timeout")

    repository.search_evidence = cancelled_search  # type: ignore[method-assign]
    http_client = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer search-token"},
    )
    async with (
        app.router.lifespan_context(app),
        http_client,
        streamable_http_client(
            "http://test/mcp",
            http_client=http_client,
            terminate_on_close=False,
        ) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        result = await session.call_tool("search_evidence", {"query": "private phrase"})

    assert result.is_error is True
    assert repository.audits[-1].outcome == "timeout"
    assert repository.metric_rollups[-1]["outcome"] == "timeout"
    assert "private" not in repository.audits[-1].model_dump_json()
    assert "private" not in repr(repository.metric_rollups[-1])
