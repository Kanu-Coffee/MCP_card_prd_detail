"""FastAPI edge with twelve default MCP tools and one optional experimental tool."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from cardrag_mcp.config import Settings
from cardrag_mcp.experimental_map_reduce import ExperimentalMapReduceLane
from cardrag_mcp.models import (
    ContractSearchRequest,
    SearchFilters,
    SearchRequest,
    SourcePdfDescriptor,
)
from cardrag_mcp.observability import Metrics, log_event
from cardrag_mcp.repository import ServingRepository
from cardrag_mcp.store import GenerationStore
from cardrag_mcp.updater import WebDAVUpdater

logger = logging.getLogger("cardrag_mcp.http")
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


class RangeNotSatisfiable(ValueError):
    pass


def parse_range(value: str | None, size: int) -> tuple[int, int, bool]:
    """Return inclusive start/end and whether a Range header was supplied."""

    if value is None:
        return 0, size - 1, False
    if "," in value:
        raise RangeNotSatisfiable("multiple ranges are not supported")
    match = _RANGE.fullmatch(value.strip())
    if match is None:
        raise RangeNotSatisfiable("invalid byte range")
    raw_start, raw_end = match.groups()
    if not raw_start and not raw_end:
        raise RangeNotSatisfiable("empty byte range")
    if not raw_start:
        suffix = int(raw_end)
        if suffix <= 0:
            raise RangeNotSatisfiable("invalid suffix range")
        start = max(0, size - suffix)
        return start, size - 1, True
    start = int(raw_start)
    if start >= size:
        raise RangeNotSatisfiable("range begins after the resource")
    end = int(raw_end) if raw_end else size - 1
    if end < start:
        raise RangeNotSatisfiable("range end precedes its start")
    return start, min(end, size - 1), True


async def _file_range(
    path: Path,
    start: int,
    length: int,
    *,
    release: Callable[[], None] | None = None,
) -> AsyncIterator[bytes]:
    try:
        with path.open("rb") as source:
            source.seek(start)
            remaining = length
            while remaining:
                chunk = await asyncio.to_thread(source.read, min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
    finally:
        if release is not None:
            release()


def _model_json(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, tuple):
        return [_model_json(item) for item in value]
    return value


def build_mcp_server(
    repository: ServingRepository,
    store: GenerationStore,
    settings: Settings,
    experimental_map_reduce: ExperimentalMapReduceLane | None = None,
) -> MCPServer:
    server = MCPServer(
        "CardRAG",
        instructions=(
            "Read-only search over the currently active card-product snapshot. "
            "A v5 generation exposes exact, structure-preserving contract search, "
            "revision history, "
            "and linked source bundles; v4 generations retain the legacy evidence tools. "
            "Products whose current official source is protected DRM are explicitly reported "
            "with availability=unsupported_drm and have no document or PDF. "
            "Products whose validated PDF failed isolated OCR are reported with "
            "availability=ocr_failed and have a PDF but no OCR pages or evidence. "
            "There is no remote-PDF fetch or page-image interface."
        ),
    )

    @server.tool()
    async def search_contracts(
        query: str,
        issuer: str | None = None,
        product_lineage_id: str | None = None,
        as_of: str | None = None,
        include_history: bool = False,
        mode: str = "exact",
        limit: int = 10,
    ) -> dict[str, Any]:
        """Search v5 views exactly, or poll a durable bounded exhaustive audit."""

        parsed_as_of = None if as_of is None else date.fromisoformat(as_of)
        result = await repository.search_contracts(
            ContractSearchRequest(
                query=query,
                issuer=issuer,
                product_lineage_id=product_lineage_id,
                as_of=parsed_as_of,
                include_history=include_history,
                mode=mode,  # type: ignore[arg-type]
                limit=limit,
            )
        )
        return result.model_dump(mode="json")

    @server.tool()
    async def get_contract_bundle(
        contract_revision_id: str,
        scope: str = "full",
        include_links: bool = True,
    ) -> dict[str, Any]:
        """Return one v5 contract's original-order structure and linked notices."""

        value = await repository.get_contract_bundle(
            contract_revision_id,
            scope=scope,
            include_links=include_links,
        )
        if value is None:
            raise ValueError("contract revision not found")
        return value.model_dump(mode="json")

    @server.tool()
    async def list_product_revisions(
        issuer: str,
        product_lineage_id: str,
    ) -> dict[str, Any]:
        """Return current, superseded, and ambiguous revisions for one product lineage."""

        value = await repository.list_product_revisions(issuer, product_lineage_id)
        return value.model_dump(mode="json")

    @server.tool()
    async def search_evidence(
        query: str,
        issuer: str | None = None,
        product_code: str | None = None,
        section_type: str | None = None,
        limit: int = 10,
        cursor: str | None = None,
        allow_degraded: bool = False,
    ) -> dict[str, Any]:
        """Search active evidence through the v5 exact adapter or legacy v4 RRF."""

        result = await repository.search(
            SearchRequest(
                query=query,
                filters=SearchFilters(
                    issuer=issuer,
                    product_code=product_code,
                    section_type=section_type,
                ),
                limit=limit,
                cursor=cursor,
                allow_degraded=allow_degraded,
            )
        )
        return result.model_dump(mode="json")

    @server.tool()
    async def get_evidence(
        evidence_id: str,
        cursor: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Return active evidence and page through following evidence in its document."""

        value = await repository.get_evidence(evidence_id, cursor=cursor, limit=limit)
        if value is None:
            raise ValueError("evidence not found")
        return value.model_dump(mode="json")

    @server.tool()
    async def get_product(issuer: str, product_code: str) -> dict[str, Any]:
        """Return the product, including explicit unsupported_drm or ocr_failed availability."""

        value = await repository.get_product(issuer, product_code)
        if value is None:
            raise ValueError("product not found")
        return value.model_dump(mode="json")

    @server.tool()
    async def get_source_pdf(document_id: str) -> dict[str, Any]:
        """Return a protected local-PDF download descriptor."""

        document = await repository.get_document(document_id)
        if document is None:
            raise ValueError("document not found")
        value = SourcePdfDescriptor(
            document_id=document.document_id,
            url=(
                str(settings.mcp_public_base_url).rstrip("/")
                + f"/sources/{document.document_id}/pdf"
            ),
            sha256=document.pdf_sha256,
            size_bytes=document.pdf_size_bytes,
        )
        return value.model_dump(mode="json")

    @server.tool()
    async def get_source_page(document_id: str, page: int) -> dict[str, Any]:
        """Return stored OCR text for one 1-based page; no PNG is generated."""

        value = await repository.get_source_page(document_id, page)
        if value is None:
            raise ValueError("source page not found")
        return value.model_dump(mode="json")

    @server.tool()
    async def list_recent_products(
        months: int = 3,
        issuer: str | None = None,
    ) -> dict[str, Any]:
        """List card products launched or revised within the specified number of months.

        Returns both the official launch date (parsed from disclosure text) and the contract
        effective date. Results are sorted newest-first. Use this tool for time-based queries
        like 'recently released cards' or 'cards launched in the last 3 months'.
        """

        result = await repository.list_recent_products(months=months, issuer=issuer)
        return result.model_dump(mode="json")

    @server.tool()
    async def find_products(
        keyword: str,
        issuer: str | None = None,
    ) -> dict[str, Any]:
        """Search card products by partial name keyword (fuzzy).

        Matches are case-insensitive and width-normalized (NFKC). Use this instead of
        get_product when you only know a partial name (e.g. '원더라이프', 'SUPER', '가온')
        rather than a 6-digit product code.
        """

        result = await repository.find_products(keyword=keyword, issuer=issuer)
        return result.model_dump(mode="json")

    @server.tool()
    async def find_cards_by_merchant(
        merchant_name: str,
        issuer: str | None = None,
    ) -> dict[str, Any]:
        """Find every card product whose BENEFIT nodes mention the given merchant or brand.

        Performs an exhaustive text scan over benefit clauses (e.g. '스타벅스', '티몬', '넷플릭스',
        '배달의민족'), returning matching benefit excerpts per card without vector search omissions.
        """

        result = await repository.find_cards_by_merchant(merchant_name=merchant_name, issuer=issuer)
        return result.model_dump(mode="json")

    @server.tool()
    async def get_product_summary(
        issuer: str,
        identifier: str,
    ) -> dict[str, Any]:
        """Return a compact summary of one card product: name, dates, annual fee, and top benefits.

        'identifier' can be a 6-digit product_code OR a product name substring.
        Produces a lightweight 1-2KB summary instead of the massive full contract bundle.
        """

        result = await repository.get_product_summary(issuer=issuer, identifier=identifier)
        if result is None:
            raise ValueError("product not found")
        return result.model_dump(mode="json")

    if experimental_map_reduce is not None:

        @server.tool()
        async def experimental_long_context_audit(
            query: str,
            action: Literal["start", "poll", "cancel"] = "start",
            job_id: str | None = None,
        ) -> dict[str, Any]:
            """Start, poll, or cancel a default-off audit-only LLM job."""

            result = await experimental_map_reduce.run(query, action=action, job_id=job_id)
            return result.model_dump(mode="json")

    @server.resource("cardrag://issuers", mime_type="application/json")
    async def issuers_resource() -> str:
        import json

        return json.dumps(_model_json(await repository.list_issuers()), ensure_ascii=False)

    @server.resource("cardrag://products", mime_type="application/json")
    async def products_resource() -> str:
        import json

        return json.dumps(_model_json(await repository.list_products()), ensure_ascii=False)

    @server.resource("cardrag://products/{issuer}/{product_code}", mime_type="application/json")
    async def product_resource(issuer: str, product_code: str) -> str:
        import json

        value = await repository.get_product(issuer, product_code)
        if value is None:
            raise ValueError("product not found")
        return json.dumps(_model_json(value), ensure_ascii=False)

    @server.resource("cardrag://documents/{document_id}", mime_type="application/json")
    async def document_resource(document_id: str) -> str:
        import json

        value = await repository.get_document(document_id)
        if value is None:
            raise ValueError("document not found")
        return json.dumps(_model_json(value), ensure_ascii=False)

    @server.resource("cardrag://documents/{document_id}/pages/{page}", mime_type="application/json")
    async def page_resource(document_id: str, page: int) -> str:
        import json

        value = await repository.get_source_page(document_id, page)
        if value is None:
            raise ValueError("source page not found")
        return json.dumps(_model_json(value), ensure_ascii=False)

    @server.resource("cardrag://evidence/{evidence_id}", mime_type="application/json")
    async def evidence_resource(evidence_id: str) -> str:
        import json

        value = await repository.get_evidence(evidence_id)
        if value is None:
            raise ValueError("evidence not found")
        return json.dumps(_model_json(value), ensure_ascii=False)

    return server


def build_app(
    repository: ServingRepository,
    store: GenerationStore,
    settings: Settings,
    *,
    updater: WebDAVUpdater | None = None,
    metrics: Metrics | None = None,
    experimental_map_reduce: ExperimentalMapReduceLane | None = None,
) -> FastAPI:
    """Build the complete ASGI app around already-created runtime dependencies."""

    metrics = metrics or Metrics.create()
    server = build_mcp_server(
        repository,
        store,
        settings,
        experimental_map_reduce,
    )
    public = urlsplit(str(settings.mcp_public_base_url))
    public_origin = urlunsplit((public.scheme, public.netloc, "", "", ""))
    mcp_app = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        host=settings.mcp_host,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(
                dict.fromkeys(
                    (
                        public.netloc,
                        f"{public.hostname}:*",
                        f"{settings.mcp_host}:*",
                        "127.0.0.1:*",
                        "localhost:*",
                        "[::1]:*",
                        "testserver",
                    )
                )
            ),
            allowed_origins=[
                public_origin,
                "http://127.0.0.1:*",
                "http://localhost:*",
                "http://[::1]:*",
            ],
        ),
    )
    stop = asyncio.Event()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task: asyncio.Task[None] | None = None
        async with mcp_app.router.lifespan_context(mcp_app):
            if updater is not None:
                task = asyncio.create_task(updater.run_forever(stop), name="cardrag-webdav-updater")
            metrics.ready.set(1 if repository.ready else 0)
            try:
                yield
            finally:
                stop.set()
                if task is not None:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                if updater is not None:
                    await updater.close()
                try:
                    if repository.reranker_shadow is not None:
                        await repository.reranker_shadow.close()
                finally:
                    try:
                        if experimental_map_reduce is not None:
                            await experimental_map_reduce.close()
                    finally:
                        await repository.embedder.close()

    app = FastAPI(
        title="CardRAG MCP",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    expected_token = settings.bearer_token_value()

    @app.middleware("http")
    async def protect_and_observe(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.url.path not in {"/health/live", "/health/ready"}:
            authorization = request.headers.get("authorization", "")
            scheme, separator, supplied = authorization.partition(" ")
            if (
                not separator
                or scheme.casefold() != "bearer"
                or not supplied
                or not secrets.compare_digest(supplied, expected_token)
            ):
                metrics.operations.labels(operation="authentication", outcome="denied").inc()
                return JSONResponse(
                    {"error": "unauthorized"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
        operation = _operation_name(request.url.path)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            metrics.operations.labels(operation=operation, outcome="error").inc()
            metrics.operation_seconds.labels(operation=operation).observe(
                time.perf_counter() - started
            )
            log_event(logger, "http.completed", operation=operation, outcome="error")
            raise
        outcome = "success" if response.status_code < 400 else "failure"
        metrics.operations.labels(operation=operation, outcome=outcome).inc()
        metrics.operation_seconds.labels(operation=operation).observe(time.perf_counter() - started)
        log_event(
            logger,
            "http.completed",
            operation=operation,
            outcome=outcome,
            status_code=response.status_code,
        )
        return response

    @app.get("/health/live", include_in_schema=False)
    async def live() -> dict[str, bool]:
        return {"live": True}

    @app.get("/health/ready", include_in_schema=False)
    async def ready() -> Response:
        is_ready = repository.ready
        metrics.ready.set(1 if is_ready else 0)
        return JSONResponse({"ready": is_ready}, status_code=200 if is_ready else 503)

    @app.get("/metrics", include_in_schema=False)
    async def prometheus() -> Response:
        return Response(metrics.body(), media_type="text/plain; version=0.0.4; charset=utf-8")

    @app.get("/resources/issuers", include_in_schema=False)
    async def issuers() -> Any:
        return _model_json(await repository.list_issuers())

    @app.get("/resources/products", include_in_schema=False)
    async def products(issuer: str | None = None) -> Any:
        return _model_json(await repository.list_products(issuer))

    @app.get("/resources/products/{issuer}/{product_code}", include_in_schema=False)
    async def product(issuer: str, product_code: str) -> Any:
        value = await repository.get_product(issuer, product_code)
        if value is None:
            raise HTTPException(status_code=404, detail="not found")
        return _model_json(value)

    @app.get("/resources/documents/{document_id}", include_in_schema=False)
    async def document(document_id: str) -> Any:
        value = await repository.get_document(document_id)
        if value is None:
            raise HTTPException(status_code=404, detail="not found")
        return _model_json(value)

    @app.get("/resources/documents/{document_id}/pages/{page}", include_in_schema=False)
    async def page(document_id: str, page: int) -> Any:
        value = await repository.get_source_page(document_id, page)
        if value is None:
            raise HTTPException(status_code=404, detail="not found")
        return _model_json(value)

    @app.get("/resources/evidence/{evidence_id}", include_in_schema=False)
    async def evidence(evidence_id: str) -> Any:
        value = await repository.get_evidence(evidence_id)
        if value is None:
            raise HTTPException(status_code=404, detail="not found")
        return _model_json(value)

    @app.api_route("/sources/{document_id}/pdf", methods=["GET", "HEAD"], include_in_schema=False)
    async def source_pdf(document_id: str, request: Request) -> Response:
        lease = store.pin()
        handle = lease.__enter__()
        released = False

        def release() -> None:
            nonlocal released
            if not released:
                released = True
                lease.__exit__(None, None, None)

        try:
            document = await asyncio.to_thread(repository._get_document, handle, document_id)
        except BaseException:
            release()
            raise
        if document is None:
            release()
            raise HTTPException(status_code=404, detail="not found")
        document_size = document.pdf_size_bytes
        try:
            path = await asyncio.to_thread(
                store.verify_pdf_for_handle,
                handle,
                document,
                maximum_bytes=settings.maximum_pdf_bytes,
            )
            start, end, partial = parse_range(request.headers.get("range"), document_size)
        except RangeNotSatisfiable:
            release()
            return Response(
                status_code=416,
                headers={"Content-Range": f"bytes */{document_size}"},
            )
        except RuntimeError:
            release()
            return JSONResponse({"error": "source unavailable"}, status_code=503)
        except BaseException:
            release()
            raise
        length = end - start + 1
        safe_name = f"cardrag-{document.pdf_sha256}.pdf"
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Disposition": f'attachment; filename="{safe_name}"',
            "Content-Length": str(length),
            "ETag": f'"{document.pdf_sha256}"',
        }
        if partial:
            headers["Content-Range"] = f"bytes {start}-{end}/{document_size}"
        if request.method == "HEAD":
            release()
            return Response(
                status_code=206 if partial else 200,
                headers=headers,
                media_type="application/pdf",
            )
        return StreamingResponse(
            _file_range(path, start, length, release=release),
            status_code=206 if partial else 200,
            headers=headers,
            media_type="application/pdf",
        )

    app.mount("/", mcp_app)
    app.state.mcp_server = server
    app.state.metrics = metrics
    app.state.experimental_map_reduce = experimental_map_reduce
    return app


def _operation_name(path: str) -> str:
    if path == "/mcp" or path.startswith("/mcp/"):
        return "mcp"
    if path.startswith("/sources/"):
        return "source_pdf"
    if path.startswith("/resources/"):
        return "resource"
    if path == "/metrics":
        return "metrics"
    if path.startswith("/health/"):
        return "health"
    return "unknown"
