"""Stateless Streamable HTTP MCP and catalog-bound source routes."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import date
from typing import Any, Literal, TypeVar, cast
from urllib.parse import urlsplit, urlunsplit

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.routes import create_protected_resource_routes
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import TypeAdapter
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from cardrag.config import Settings
from cardrag.observability import (
    AuthenticationAuditMiddleware,
    ObservabilityMiddleware,
    bind_context,
    current_request_id,
    get_observability,
    hash_identifier,
    log_event,
    metrics_response,
)
from cardrag.service.auth import (
    KeycloakJWTVerifier,
    access_subject,
    authenticate_request,
    bearer_challenge,
)
from cardrag.service.models import (
    AuditAction,
    AuditEvent,
    AuditOutcome,
    Cursor,
    EvidencePage,
    Identifier,
    Issuer,
    PageLimit,
    ProductVersions,
    SearchPage,
    SearchRequest,
)
from cardrag.service.query import (
    NotFoundError,
    QueryService,
    ServiceTimeoutError,
    ServiceUnavailableError,
    utc_now,
)
from cardrag.service.repository import CardRAGRepository
from cardrag.service.source_files import InvalidSourceError, SourceFileService

AccessTokenGetter = Callable[[], AccessToken | None | Awaitable[AccessToken | None]]
_IDENTIFIER = TypeAdapter(Identifier)
_CANONICAL_DOCUMENT_ID = re.compile(r"^doc_[0-9a-f]{64}$")
T = TypeVar("T")
SourceAuditAction = Literal["source_pdf", "source_page_png"]
SourceAuditOutcome = Literal["allowed", "denied", "not_found", "invalid_source", "timeout"]


def build_mcp_server(
    repository: CardRAGRepository,
    settings: Settings,
    *,
    token_verifier: TokenVerifier,
    access_token_getter: AccessTokenGetter = get_access_token,
) -> tuple[MCPServer, QueryService, SourceFileService]:
    """Create the MCP server without opening a database or reading secrets."""

    query_service = QueryService(
        repository,
        max_concurrent_requests=settings.max_concurrent_requests,
        request_timeout_seconds=settings.request_timeout_seconds,
    )
    source_service = SourceFileService(
        query_service,
        storage_root=settings.storage_root,
        page_cache_root=settings.page_cache_root,
        public_server_url=str(settings.mcp_server_url),
        max_pdf_bytes=settings.max_pdf_bytes,
        page_cache_ttl_seconds=settings.page_cache_ttl_seconds,
        render_scale=settings.render_scale,
        subject_namespace=str(settings.oidc_issuer).rstrip("/"),
    )
    observability = get_observability(service="mcp", environment=settings.environment)
    operation_logger = logging.getLogger("cardrag.mcp")
    auth = AuthSettings(
        issuer_url=settings.oidc_issuer,
        resource_server_url=settings.mcp_server_url,
        # The transport requires a valid bearer token.  Individual tools and
        # resources then enforce `search` or `source_pdf`; making `search`
        # global would incorrectly require both scopes for a PDF-only client.
        required_scopes=[],
    )

    constructor_kwargs: dict[str, Any] = {
        "token_verifier": token_verifier,
        "auth": auth,
        "instructions": (
            "Read-only evidence search for Woori, KB Kookmin, and Shinhan card documents. "
            "Search defaults to the latest version. Treat evidence provenance and exact source "
            "hashes as authoritative; request historical versions explicitly."
        ),
    }
    constructor_parameters = inspect.signature(MCPServer).parameters
    if "stateless_http" in constructor_parameters:
        constructor_kwargs["stateless_http"] = True
    if "json_response" in constructor_parameters:
        constructor_kwargs["json_response"] = True
    server = MCPServer("CardRAG", **constructor_kwargs)

    async def access_token() -> AccessToken | None:
        candidate = access_token_getter()
        return await candidate if inspect.isawaitable(candidate) else candidate

    async def audit_call(
        *,
        action: AuditAction,
        token: AccessToken | None,
        outcome: AuditOutcome,
        document_id: str | None,
    ) -> None:
        recorder = getattr(repository, "record_audit", None)
        if recorder is None:
            raise ServiceUnavailableError("access audit is temporarily unavailable")
        subject = access_subject(token) if token is not None else "anonymous"
        subject_hash = hashlib.sha256(
            f"{str(settings.oidc_issuer).rstrip('/')}\x00{subject}".encode()
        ).hexdigest()
        event = AuditEvent(
            request_id=current_request_id() or source_service.request_id(),
            occurred_at=utc_now(),
            action=action,
            subject_hash=subject_hash,
            client_id=token.client_id if token is not None else None,
            granted_scopes=tuple(sorted(set(token.scopes))) if token is not None else (),
            document_id=_safe_audit_document_id(document_id) if document_id is not None else None,
            outcome=outcome,
        )
        try:

            async def persist() -> None:
                result = recorder(event)
                if inspect.isawaitable(result):
                    await result

            await query_service.record_auxiliary(persist, label="access audit")
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ServiceUnavailableError("access audit is temporarily unavailable") from None

    async def audited_call(
        *,
        action: AuditAction,
        scope: str | tuple[str, ...],
        operation: Callable[[], Awaitable[T]],
        document_id: str | None = None,
    ) -> T:
        started = time.perf_counter()

        async def observe(
            outcome: AuditOutcome,
            *,
            error_code: str | None = None,
            generation_id: str | None = None,
        ) -> None:
            duration = time.perf_counter() - started
            observability.metrics.observe_mcp(
                operation=action,
                outcome=outcome,
                duration=duration,
            )
            with bind_context(generation_id=generation_id):
                log_event(
                    operation_logger,
                    "mcp.operation.completed",
                    level=logging.INFO if outcome in {"success", "no_result"} else logging.WARNING,
                    operation=action,
                    outcome=outcome,
                    duration_seconds=round(duration, 6),
                    error_code=error_code,
                    document_id_hash=hash_identifier(document_id),
                )
            recorder = getattr(repository, "record_mcp_metric", None)
            if recorder is None:
                return
            try:

                async def persist() -> None:
                    persisted = recorder(
                        operation=action,
                        outcome=outcome,
                        duration=duration,
                    )
                    if inspect.isawaitable(persisted):
                        await persisted

                await query_service.record_auxiliary(persist, label="MCP metric rollup")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Metrics must not turn a completed user operation into a
                # failure.  The allow-listed event exposes persistence faults
                # without logging arguments, request bodies or credentials.
                log_event(
                    operation_logger,
                    "mcp.metric_rollup.failed",
                    level=logging.ERROR,
                    operation=action,
                    outcome="error",
                    error_code=type(exc).__name__,
                )

        token = await access_token()
        required_scopes = (scope,) if isinstance(scope, str) else scope
        if token is None or not set(required_scopes).issubset(token.scopes):
            try:
                await audit_call(action=action, token=token, outcome="denied", document_id=document_id)
            except Exception as exc:
                await observe("error", error_code=type(exc).__name__)
                raise
            await observe("denied")
            raise PermissionError(f"required OAuth scope is missing: {' '.join(required_scopes)}")
        try:
            result = await operation()
        except asyncio.CancelledError:
            raise
        except NotFoundError as exc:
            await audit_call(action=action, token=token, outcome="not_found", document_id=document_id)
            await observe("not_found", error_code=type(exc).__name__)
            raise
        except ServiceTimeoutError as exc:
            await audit_call(action=action, token=token, outcome="timeout", document_id=document_id)
            await observe("timeout", error_code=type(exc).__name__)
            raise
        except ServiceUnavailableError as exc:
            await audit_call(action=action, token=token, outcome="error", document_id=document_id)
            await observe("error", error_code=type(exc).__name__)
            raise
        except Exception as exc:
            await audit_call(action=action, token=token, outcome="error", document_id=document_id)
            await observe("error", error_code=type(exc).__name__)
            raise
        if bool(getattr(result, "degraded", False)):
            outcome: AuditOutcome = "degraded"
        elif action == "search_evidence" and not getattr(result, "items", [True]):
            outcome = "no_result"
        else:
            outcome = "success"
        await audit_call(action=action, token=token, outcome=outcome, document_id=document_id)
        generation_id = getattr(result, "generation_id", None)
        await observe(outcome, generation_id=generation_id if isinstance(generation_id, str) else None)
        return result

    @server.tool()
    async def search_evidence(
        query: str,
        issuer: Issuer | None = None,
        product_code: str | None = None,
        section_type: str | None = None,
        version: str | None = None,
        as_of: date | None = None,
        limit: int = 10,
        cursor: str | None = None,
        allow_degraded: bool = False,
    ) -> dict[str, Any]:
        """Hybrid evidence search with pre-candidate filters and stable pagination."""

        async def search() -> SearchPage:
            request = SearchRequest(
                query=query,
                issuer=issuer,
                product_code=product_code,
                section_type=section_type,
                version=version,
                as_of=as_of,
                limit=limit,
                cursor=cursor,
                allow_degraded=allow_degraded,
            )
            return await query_service.search(request)

        result = await audited_call(
            action="search_evidence",
            scope=settings.required_search_scope,
            operation=search,
        )
        return result.model_dump(mode="json")

    @server.tool()
    async def get_evidence(
        evidence_id: Identifier,
        cursor: Cursor | None = None,
        limit: PageLimit = 20,
    ) -> dict[str, Any]:
        """Resolve a stable evidence ID and optionally page through adjacent evidence."""

        async def evidence() -> EvidencePage:
            return await query_service.evidence(evidence_id, cursor=cursor, limit=limit)

        result = await audited_call(
            action="get_evidence",
            scope=settings.required_search_scope,
            operation=evidence,
        )
        return result.model_dump(mode="json")

    @server.tool()
    async def get_product_versions(
        issuer: Issuer,
        product_code: str,
        as_of: date | None = None,
    ) -> dict[str, Any]:
        """List cataloged versions, including exact hashes and the latest marker."""

        async def versions() -> ProductVersions:
            validated_code = _IDENTIFIER.validate_python(product_code)
            return await query_service.versions(issuer, validated_code, as_of=as_of)

        result = await audited_call(
            action="get_product_versions",
            scope=settings.required_search_scope,
            operation=versions,
        )
        return result.model_dump(mode="json")

    @server.tool()
    async def get_source_pdf(document_id: str) -> dict[str, Any]:
        """Return an authenticated streaming URL plus the exact PDF hash and size."""

        descriptor = await audited_call(
            action="get_source_pdf",
            scope=settings.required_source_scope,
            operation=lambda: source_service.pdf_descriptor(document_id),
            document_id=document_id,
        )
        return descriptor.model_dump(mode="json")

    @server.tool()
    async def get_source_page(
        document_id: str,
        page: int,
        include_png: bool = False,
    ) -> dict[str, Any]:
        """Return page OCR; optionally prepare a seven-day cached PNG URL."""

        required_scope: str | tuple[str, ...] = (
            (settings.required_search_scope, settings.required_source_scope)
            if include_png
            else settings.required_search_scope
        )
        descriptor = await audited_call(
            action="get_source_page",
            scope=required_scope,
            operation=lambda: source_service.page_descriptor(
                document_id,
                page,
                include_png=include_png,
            ),
            document_id=document_id,
        )
        return descriptor.model_dump(mode="json")

    def json_resource(value: Any) -> str:
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @server.resource(
        "cardrag://catalog/issuers",
        name="Card issuer catalog",
        description="Supported v1 issuer codes.",
        mime_type="application/json",
    )
    async def issuer_catalog() -> str:
        async def catalog() -> dict[str, Any]:
            return {
                "issuers": [
                    {"code": "woori", "name": "우리카드"},
                    {"code": "kb", "name": "KB국민카드"},
                    {"code": "shinhan", "name": "신한카드"},
                ]
            }

        result = await audited_call(
            action="issuer_catalog",
            scope=settings.required_search_scope,
            operation=catalog,
        )
        return json_resource(result)

    @server.resource(
        "cardrag://catalog/index-status",
        name="Published index status",
        description="Readiness and active published generation status.",
        mime_type="application/json",
    )
    async def index_status() -> str:
        result = await audited_call(
            action="index_status",
            scope=settings.required_search_scope,
            operation=query_service.readiness,
        )
        return json_resource(result)

    @server.resource(
        "cardrag://products/{issuer}/{product_code}",
        name="Product version catalog",
        description="Published versions for one issuer-scoped product.",
        mime_type="application/json",
    )
    async def product_resource(issuer: Issuer, product_code: str) -> str:
        async def product() -> Any:
            validated_code = _IDENTIFIER.validate_python(product_code)
            return await query_service.versions(issuer, validated_code)

        result = await audited_call(
            action="product_resource",
            scope=settings.required_search_scope,
            operation=product,
        )
        return json_resource(result)

    @server.resource(
        "cardrag://documents/{document_id}",
        name="Document metadata",
        description="Exact immutable document version, hash, size, and OCR link.",
        mime_type="application/json",
    )
    async def document_resource(document_id: str) -> str:
        result = await audited_call(
            action="document_resource",
            scope=settings.required_search_scope,
            operation=lambda: source_service.document_descriptor(document_id),
            document_id=document_id,
        )
        return json_resource(result)

    @server.resource(
        "cardrag://evidence/{evidence_id}",
        name="Stable evidence",
        description="Stable full evidence record with document and source-span provenance.",
        mime_type="application/json",
    )
    async def evidence_resource(evidence_id: str) -> str:
        async def evidence() -> Any:
            validated_id = _IDENTIFIER.validate_python(evidence_id)
            return await query_service.evidence(validated_id, limit=50)

        result = await audited_call(
            action="evidence_resource",
            scope=settings.required_search_scope,
            operation=evidence,
        )
        return json_resource(result)

    @server.resource(
        "cardrag://sources/{document_id}/ocr",
        name="Source OCR descriptor",
        description="Page count and stable page-resource template for an exact source OCR.",
        mime_type="application/json",
    )
    async def source_ocr_resource(document_id: str) -> str:
        result = await audited_call(
            action="source_ocr_resource",
            scope=settings.required_search_scope,
            operation=lambda: source_service.ocr_descriptor(document_id),
            document_id=document_id,
        )
        return json_resource(result)

    @server.resource(
        "cardrag://sources/{document_id}/ocr/pages/{page}",
        name="Source OCR page",
        description="OCR text and exact source provenance for one 1-based PDF page.",
        mime_type="application/json",
    )
    async def source_ocr_page_resource(document_id: str, page: int) -> str:
        async def source_page() -> Any:
            if page < 1:
                raise ValueError("page must be at least 1")
            return await query_service.source_page(document_id, page)

        result = await audited_call(
            action="source_ocr_page_resource",
            scope=settings.required_search_scope,
            operation=source_page,
            document_id=document_id,
        )
        return json_resource(result)

    return server, query_service, source_service


def build_app(
    repository: CardRAGRepository,
    settings: Settings,
    *,
    token_verifier: TokenVerifier | None = None,
    access_token_getter: AccessTokenGetter = get_access_token,
) -> Any:
    """Build the complete ASGI app around an injected read-only repository."""

    owns_verifier = token_verifier is None
    verifier = token_verifier or KeycloakJWTVerifier(
        issuer=str(settings.oidc_issuer),
        audience=settings.oidc_audience,
        cache_seconds=settings.oidc_jwks_cache_seconds,
    )
    server, query_service, source_service = build_mcp_server(
        repository,
        settings,
        token_verifier=verifier,
        access_token_getter=access_token_getter,
    )

    method = server.streamable_http_app
    method_parameters = inspect.signature(method).parameters
    method_kwargs: dict[str, Any] = {}
    if "streamable_http_path" in method_parameters:
        method_kwargs["streamable_http_path"] = "/mcp"
    if "stateless_http" in method_parameters:
        method_kwargs["stateless_http"] = True
    if "json_response" in method_parameters:
        method_kwargs["json_response"] = True
    if "host" in method_parameters:
        method_kwargs["host"] = settings.host
    if "transport_security" in method_parameters:
        public_url = urlsplit(str(settings.mcp_server_url))
        public_origin = urlunsplit((public_url.scheme, public_url.netloc, "", "", ""))
        method_kwargs["transport_security"] = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(
                dict.fromkeys(
                    [
                        public_url.netloc,
                        f"{public_url.hostname}:*",
                        f"{settings.host}:*",
                        "127.0.0.1:*",
                        "localhost:*",
                        "[::1]:*",
                    ]
                )
            ),
            allowed_origins=[
                public_origin,
                "http://127.0.0.1:*",
                "http://localhost:*",
                "http://[::1]:*",
            ],
        )
    app = method(**method_kwargs)
    observability = get_observability(service="mcp", environment=settings.environment)

    async def live(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def ready(_: Request) -> Response:
        status = await query_service.readiness()
        return JSONResponse(
            status.model_dump(mode="json"),
            status_code=200 if status.ready else 503,
        )

    async def metrics(request: Request) -> Response:
        return metrics_response(request, observability)

    async def source_pdf(request: Request) -> Response:
        request_id = current_request_id() or source_service.request_id()
        started = time.perf_counter()

        async def completed(outcome: str, stream_duration: float) -> None:
            del stream_duration
            await persist_metric(
                "source_pdf",
                outcome,
                time.perf_counter() - started,
            )

        access_token, error = await authenticate_request(
            request,
            verifier,
            required_scope=settings.required_source_scope,
        )
        if error is not None:
            await source_service.audit_attempt(
                request_id=request_id,
                action="source_pdf",
                access_token=access_token,
                source=None,
                document_id=_safe_audit_document_id(request.path_params["document_id"]),
                page=None,
                requested_range=request.headers.get("range"),
                outcome="denied",
                required=False,
            )
            await completed("denied", 0.0)
            return _auth_error(error, settings, request_id=request_id)
        assert access_token is not None
        try:
            return await source_service.pdf_response(
                request.path_params["document_id"],
                request,
                access_token,
                request_id=request_id,
                on_complete=completed,
            )
        except ValueError:
            await _audit_failure(
                source_service,
                request_id=request_id,
                action="source_pdf",
                access_token=access_token,
                document_id=request.path_params["document_id"],
                page=None,
                requested_range=request.headers.get("range"),
                outcome="invalid_source",
            )
            await completed("error", 0.0)
            return _error_response("invalid_document_id", 400, request_id)
        except NotFoundError:
            await _audit_failure(
                source_service,
                request_id=request_id,
                action="source_pdf",
                access_token=access_token,
                document_id=request.path_params["document_id"],
                page=None,
                requested_range=request.headers.get("range"),
                outcome="not_found",
            )
            await completed("not_found", 0.0)
            return _error_response("source_not_found", 404, request_id)
        except ServiceTimeoutError:
            await _audit_failure(
                source_service,
                request_id=request_id,
                action="source_pdf",
                access_token=access_token,
                document_id=request.path_params["document_id"],
                page=None,
                requested_range=request.headers.get("range"),
                outcome="timeout",
            )
            await completed("timeout", 0.0)
            return _error_response("source_unavailable", 503, request_id)
        except (InvalidSourceError, ServiceUnavailableError):
            await _audit_failure(
                source_service,
                request_id=request_id,
                action="source_pdf",
                access_token=access_token,
                document_id=request.path_params["document_id"],
                page=None,
                requested_range=request.headers.get("range"),
                outcome="invalid_source",
            )
            await completed("error", 0.0)
            return _error_response("source_unavailable", 503, request_id)

    async def source_page(request: Request) -> Response:
        request_id = current_request_id() or source_service.request_id()
        started = time.perf_counter()

        async def completed(outcome: str, stream_duration: float) -> None:
            del stream_duration
            await persist_metric(
                "source_page_png",
                outcome,
                time.perf_counter() - started,
            )

        access_token, error = await authenticate_request(
            request,
            verifier,
            required_scope=settings.required_source_scope,
        )
        if error is not None:
            await source_service.audit_attempt(
                request_id=request_id,
                action="source_page_png",
                access_token=access_token,
                source=None,
                document_id=_safe_audit_document_id(request.path_params["document_id"]),
                page=int(request.path_params["page"]),
                requested_range=None,
                outcome="denied",
                required=False,
            )
            await completed("denied", 0.0)
            return _auth_error(error, settings, request_id=request_id)
        assert access_token is not None
        try:
            return await source_service.page_response(
                request.path_params["document_id"],
                int(request.path_params["page"]),
                access_token,
                request_id=request_id,
                on_complete=completed,
            )
        except ValueError:
            await _audit_failure(
                source_service,
                request_id=request_id,
                action="source_page_png",
                access_token=access_token,
                document_id=request.path_params["document_id"],
                page=int(request.path_params["page"]),
                requested_range=None,
                outcome="invalid_source",
            )
            await completed("error", 0.0)
            return _error_response("invalid_source_page", 400, request_id)
        except NotFoundError:
            await _audit_failure(
                source_service,
                request_id=request_id,
                action="source_page_png",
                access_token=access_token,
                document_id=request.path_params["document_id"],
                page=int(request.path_params["page"]),
                requested_range=None,
                outcome="not_found",
            )
            await completed("not_found", 0.0)
            return _error_response("source_page_not_found", 404, request_id)
        except ServiceTimeoutError:
            await _audit_failure(
                source_service,
                request_id=request_id,
                action="source_page_png",
                access_token=access_token,
                document_id=request.path_params["document_id"],
                page=int(request.path_params["page"]),
                requested_range=None,
                outcome="timeout",
            )
            await completed("timeout", 0.0)
            return _error_response("source_unavailable", 503, request_id)
        except (InvalidSourceError, ServiceUnavailableError):
            await _audit_failure(
                source_service,
                request_id=request_id,
                action="source_page_png",
                access_token=access_token,
                document_id=request.path_params["document_id"],
                page=int(request.path_params["page"]),
                requested_range=None,
                outcome="invalid_source",
            )
            await completed("error", 0.0)
            return _error_response("source_unavailable", 503, request_id)

    # These routes are inserted into the SDK app so its Streamable HTTP session
    # manager and RFC 9728 metadata retain their own lifespan and middleware.
    protected_resource_routes = create_protected_resource_routes(
        resource_url=settings.mcp_server_url,
        authorization_servers=[settings.oidc_issuer],
        scopes_supported=[settings.required_search_scope, settings.required_source_scope],
        resource_name="CardRAG MCP",
    )
    app.router.routes[0:0] = [
        Route("/health/live", live, methods=["GET"]),
        Route("/health/ready", ready, methods=["GET"]),
        Route("/metrics", metrics, methods=["GET"]),
        Route("/sources/{document_id}/pdf", source_pdf, methods=["GET", "HEAD"]),
        Route(
            "/sources/{document_id}/pages/{page:int}.png",
            source_page,
            methods=["GET", "HEAD"],
        ),
        *protected_resource_routes,
    ]
    app.state.cardrag_mcp_server = server
    app.state.cardrag_query_service = query_service
    app.state.cardrag_source_service = source_service
    app.state.cardrag_observability = observability
    transport_audit_logger = logging.getLogger("cardrag.auth")

    async def persist_metric(operation: str, outcome: str, duration: float) -> None:
        recorder = getattr(repository, "record_mcp_metric", None)
        if recorder is None:
            return

        async def persist() -> None:
            result = recorder(operation=operation, outcome=outcome, duration=duration)
            if inspect.isawaitable(result):
                await result

        try:
            await query_service.record_auxiliary(persist, label="MCP metric rollup")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_event(
                transport_audit_logger,
                "mcp.metric_rollup.failed",
                level=logging.ERROR,
                operation=operation,
                outcome="error",
                error_code=type(exc).__name__,
            )

    async def audit_transport_denial(request_id: str) -> None:
        recorder = getattr(repository, "record_audit", None)
        if recorder is None:
            raise ServiceUnavailableError("access audit is temporarily unavailable")
        event = AuditEvent(
            request_id=request_id,
            occurred_at=utc_now(),
            action="mcp_transport_auth",
            subject_hash=hashlib.sha256(
                f"{str(settings.oidc_issuer).rstrip('/')}\x00anonymous".encode()
            ).hexdigest(),
            granted_scopes=(),
            outcome="denied",
        )

        async def persist() -> None:
            result = recorder(event)
            if inspect.isawaitable(result):
                await result

        await query_service.record_auxiliary(persist, label="transport access audit")
        log_event(
            transport_audit_logger,
            "auth.transport.denied",
            request_id=request_id,
            outcome="denied",
        )

    app.add_middleware(
        AuthenticationAuditMiddleware,
        audit_denial=audit_transport_denial,
        metric_denial=lambda duration: persist_metric("mcp_transport_auth", "denied", duration),
    )
    app.add_middleware(ObservabilityMiddleware, observability=observability)

    original_lifespan = app.router.lifespan_context
    maintenance_logger = logging.getLogger("cardrag.maintenance")

    async def cleanup_page_cache() -> None:
        interval = min(3600, max(60, settings.page_cache_ttl_seconds // 4))
        while True:
            try:
                removed = await source_service.cleanup_expired()
                observability.metrics.retention(kind="page_cache", count=removed)
                if removed:
                    log_event(
                        maintenance_logger,
                        "retention.page_cache.completed",
                        cache_items_removed=removed,
                        outcome="success",
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_event(
                    maintenance_logger,
                    "retention.page_cache.failed",
                    level=logging.ERROR,
                    error_code=type(exc).__name__,
                    outcome="error",
                )
            await asyncio.sleep(interval)

    @asynccontextmanager
    async def lifespan(application: Any) -> Any:
        async with original_lifespan(application):
            cleanup_task = asyncio.create_task(cleanup_page_cache(), name="cardrag-page-cache-retention")
            try:
                yield
            finally:
                cleanup_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cleanup_task
                if owns_verifier and hasattr(verifier, "aclose"):
                    try:
                        await cast(Any, verifier).aclose()
                    except Exception as exc:
                        log_event(
                            maintenance_logger,
                            "auth.verifier.close_failed",
                            level=logging.ERROR,
                            error_code=type(exc).__name__,
                            outcome="error",
                        )

    app.router.lifespan_context = lifespan
    return app


def _auth_error(error: str, settings: Settings, *, request_id: str) -> JSONResponse:
    insufficient = error == "insufficient_scope"
    scope = settings.required_source_scope if insufficient else None
    return JSONResponse(
        {"error": error},
        status_code=403 if insufficient else 401,
        headers={
            "WWW-Authenticate": bearer_challenge(
                str(settings.mcp_server_url),
                error=error,
                scope=scope,
            ),
            "Cache-Control": "no-store",
            "X-Request-ID": request_id,
        },
    )


def _error_response(error: str, status_code: int, request_id: str) -> JSONResponse:
    return JSONResponse(
        {"error": error},
        status_code=status_code,
        headers={"Cache-Control": "no-store", "X-Request-ID": request_id},
    )


def _safe_audit_document_id(document_id: str) -> str:
    if _CANONICAL_DOCUMENT_ID.fullmatch(document_id):
        return document_id
    digest = hashlib.sha256(document_id.encode("utf-8", errors="replace")).hexdigest()
    return f"invalid_{digest}"


async def _audit_failure(
    source_service: SourceFileService,
    *,
    request_id: str,
    action: SourceAuditAction,
    access_token: AccessToken | None,
    document_id: str,
    page: int | None,
    requested_range: str | None,
    outcome: SourceAuditOutcome,
) -> None:
    await source_service.audit_attempt(
        request_id=request_id,
        action=action,
        access_token=access_token,
        source=None,
        document_id=_safe_audit_document_id(document_id),
        page=page,
        requested_range=requested_range,
        outcome=outcome,
        required=False,
    )
