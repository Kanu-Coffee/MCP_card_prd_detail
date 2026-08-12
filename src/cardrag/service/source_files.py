"""Catalog-bound PDF streaming and ephemeral page rendering."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import os
import re
import stat
import tempfile
import time
import unicodedata
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlsplit, urlunsplit

import fitz  # type: ignore[import-untyped]
from mcp.server.auth.provider import AccessToken
from starlette.requests import Request
from starlette.responses import FileResponse, Response
from starlette.types import Receive, Scope, Send

from cardrag.service.auth import access_subject
from cardrag.service.models import (
    AuditEvent,
    DocumentDescriptor,
    SourceOcrDescriptor,
    SourcePage,
    SourcePageDescriptor,
    SourcePdf,
    SourcePdfDescriptor,
)
from cardrag.service.query import QueryService, ServiceTimeoutError, utc_now

_PDF_MAGIC = b"%PDF-"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")
_CANONICAL_DOCUMENT_ID = re.compile(r"^doc_[0-9a-f]{64}$")


class InvalidSourceError(RuntimeError):
    """The catalog record does not match a safe immutable source file."""


class BoundedFileResponse(FileResponse):
    """Apply the online request budget while Starlette transmits a file.

    Source preparation and body transfer are necessarily two budget phases:
    Starlette calls the response only after the endpoint has returned.  Both
    phases use the same ``QueryService`` semaphore and timeout.  Preparation
    has one deadline, then transfer has a fresh deadline which includes any
    wait to reacquire capacity and holds the slot until the final body (and any
    response background task) completes.  If transfer times out after headers
    have started, the ASGI stream is aborted because its status can no longer
    be replaced with a JSON error.
    """

    def __init__(
        self,
        *args: Any,
        query_service: QueryService,
        budget_label: str,
        on_complete: Callable[[str, float], Awaitable[None]] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._query_service = query_service
        self._budget_label = budget_label
        self._on_complete = on_complete

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        started = time.perf_counter()
        caller_outcome: str | None = None

        async def stream() -> None:
            outcome = "success"
            try:
                await FileResponse.__call__(self, scope, receive, send)
            except asyncio.CancelledError:
                outcome = "error"
                raise
            except Exception:
                outcome = "error"
                raise
            finally:
                if self._on_complete is not None:
                    await self._on_complete(
                        caller_outcome or outcome,
                        time.perf_counter() - started,
                    )

        try:
            await self._query_service.run_with_budget(
                stream,
                label=self._budget_label,
            )
        except ServiceTimeoutError:
            caller_outcome = "timeout"
            raise
        except asyncio.CancelledError:
            caller_outcome = "error"
            raise


class OutcomeResponse(Response):
    """Notify the source-route metric sink after a non-file response is sent."""

    def __init__(
        self,
        *args: Any,
        outcome: str,
        on_complete: Callable[[str, float], Awaitable[None]],
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._outcome = outcome
        self._on_complete = on_complete

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        started = time.perf_counter()
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._on_complete(self._outcome, time.perf_counter() - started)


def validate_document_id(document_id: str) -> str:
    if not document_id or len(document_id) > 512:
        raise ValueError("invalid document id")
    if document_id in {".", ".."} or "/" in document_id or "\\" in document_id:
        raise ValueError("invalid document id")
    if unicodedata.normalize("NFC", document_id) != document_id:
        raise ValueError("invalid document id")
    if any(ord(character) < 32 or ord(character) == 127 for character in document_id):
        raise ValueError("invalid document id")
    return document_id


def _public_origin(resource_url: str) -> str:
    parts = urlsplit(resource_url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("invalid public server URL")
    if parts.username is not None or parts.password is not None:
        raise ValueError("public server URL must not contain user information")
    if parts.query or parts.fragment:
        raise ValueError("public server URL must not contain query or fragment")
    return urlunsplit((parts.scheme, parts.netloc, "", "", "")).rstrip("/")


class SourceFileService:
    def __init__(
        self,
        query_service: QueryService,
        *,
        storage_root: Path,
        page_cache_root: Path,
        public_server_url: str,
        max_pdf_bytes: int,
        page_cache_ttl_seconds: int,
        render_scale: float,
        subject_namespace: str,
    ) -> None:
        self.query_service = query_service
        self.repository = query_service.repository
        self.storage_root = storage_root
        self.page_cache_root = page_cache_root
        self.public_origin = _public_origin(public_server_url)
        self.max_pdf_bytes = max_pdf_bytes
        self.page_cache_ttl_seconds = page_cache_ttl_seconds
        self.render_scale = render_scale
        self.subject_namespace = subject_namespace
        self._render_lock = asyncio.Lock()

    async def pdf_descriptor(self, document_id: str) -> SourcePdfDescriptor:
        return await self.query_service.run_with_budget(
            lambda: self._pdf_descriptor(document_id),
            label="source PDF descriptor",
        )

    async def _pdf_descriptor(self, document_id: str) -> SourcePdfDescriptor:
        document_id = validate_document_id(document_id)
        source = await self.query_service.source_pdf(document_id)
        await self._verify_pdf(source)
        return SourcePdfDescriptor(
            document_id=source.document_id,
            issuer=source.issuer,
            product_code=source.product_code,
            version=source.version,
            url=self.pdf_url(source.document_id),
            sha256=source.sha256,
            size_bytes=source.size_bytes,
            mime_type=source.mime_type,
        )

    async def document_descriptor(self, document_id: str) -> DocumentDescriptor:
        return await self.query_service.run_with_budget(
            lambda: self._document_descriptor(document_id),
            label="document descriptor",
        )

    async def _document_descriptor(self, document_id: str) -> DocumentDescriptor:
        document_id = validate_document_id(document_id)
        source = await self.query_service.source_pdf(document_id)
        await self._verify_pdf(source)
        encoded_id = quote(source.document_id, safe="")
        return DocumentDescriptor(
            document_id=source.document_id,
            issuer=source.issuer,
            product_code=source.product_code,
            version=source.version,
            sha256=source.sha256,
            size_bytes=source.size_bytes,
            mime_type=source.mime_type,
            ocr_resource=f"cardrag://sources/{encoded_id}/ocr",
        )

    async def ocr_descriptor(self, document_id: str) -> SourceOcrDescriptor:
        return await self.query_service.run_with_budget(
            lambda: self._ocr_descriptor(document_id),
            label="source OCR descriptor",
        )

    async def _ocr_descriptor(self, document_id: str) -> SourceOcrDescriptor:
        document_id = validate_document_id(document_id)
        first_page = await self.query_service.source_page(document_id, 1)
        encoded_id = quote(first_page.document_id, safe="")
        return SourceOcrDescriptor(
            document_id=first_page.document_id,
            issuer=first_page.issuer,
            product_code=first_page.product_code,
            version=first_page.version,
            page_count=first_page.page_count,
            pdf_sha256=first_page.pdf_sha256,
            page_resource_template=(f"cardrag://sources/{encoded_id}/ocr/pages/{{page}}"),
        )

    async def page_descriptor(
        self,
        document_id: str,
        page: int,
        *,
        include_png: bool,
    ) -> SourcePageDescriptor:
        return await self.query_service.run_with_budget(
            lambda: self._page_descriptor(document_id, page, include_png=include_png),
            label="source page descriptor",
        )

    async def _page_descriptor(
        self,
        document_id: str,
        page: int,
        *,
        include_png: bool,
    ) -> SourcePageDescriptor:
        document_id = validate_document_id(document_id)
        if page < 1:
            raise ValueError("page must be at least 1")
        source_page = await self.query_service.source_page(document_id, page)
        png_url: str | None = None
        ttl: int | None = None
        if include_png:
            source_pdf = await self.query_service.source_pdf(document_id)
            self._assert_page_matches_pdf(source_page, source_pdf)
            await self._cached_page_path(source_pdf, source_page)
            png_url = self.page_url(document_id, page)
            ttl = self.page_cache_ttl_seconds
        return SourcePageDescriptor(
            **source_page.model_dump(mode="python"),
            png_url=png_url,
            png_cache_ttl_seconds=ttl,
        )

    async def pdf_response(
        self,
        document_id: str,
        request: Request,
        access_token: AccessToken,
        *,
        request_id: str,
        on_complete: Callable[[str, float], Awaitable[None]] | None = None,
    ) -> Response:
        return await self.query_service.run_with_budget(
            lambda: self._pdf_response(
                document_id,
                request,
                access_token,
                request_id=request_id,
                on_complete=on_complete,
            ),
            label="source PDF response preparation",
        )

    async def _pdf_response(
        self,
        document_id: str,
        request: Request,
        access_token: AccessToken,
        *,
        request_id: str,
        on_complete: Callable[[str, float], Awaitable[None]] | None,
    ) -> Response:
        document_id = validate_document_id(document_id)
        source = await self.query_service.source_pdf(document_id)
        path = await self._verify_pdf(source)
        range_header = request.headers.get("range")
        if range_header is not None and not _valid_range(range_header, source.size_bytes):
            await self.audit_attempt(
                request_id=request_id,
                action="source_pdf",
                access_token=access_token,
                source=source,
                document_id=source.document_id,
                page=None,
                requested_range=range_header,
                outcome="denied",
            )
            response_type = OutcomeResponse if on_complete is not None else Response
            response_kwargs: dict[str, Any] = {}
            if on_complete is not None:
                response_kwargs = {"outcome": "denied", "on_complete": on_complete}
            return response_type(
                status_code=416,
                headers={
                    "Content-Range": f"bytes */{source.size_bytes}",
                    "Accept-Ranges": "bytes",
                    "Cache-Control": "private, no-store",
                    "X-Request-ID": request_id,
                },
                **response_kwargs,
            )
        await self.audit_attempt(
            request_id=request_id,
            action="source_pdf",
            access_token=access_token,
            source=source,
            document_id=source.document_id,
            page=None,
            requested_range=range_header,
            outcome="allowed",
        )
        return BoundedFileResponse(
            path,
            query_service=self.query_service,
            budget_label="source PDF stream",
            on_complete=on_complete,
            media_type="application/pdf",
            filename=f"{source.document_id}.pdf",
            headers={
                "Accept-Ranges": "bytes",
                "Cache-Control": "private, no-store",
                "ETag": f'"{source.sha256}"',
                "X-Content-SHA256": source.sha256,
                "X-Request-ID": request_id,
            },
        )

    async def page_response(
        self,
        document_id: str,
        page: int,
        access_token: AccessToken,
        *,
        request_id: str,
        on_complete: Callable[[str, float], Awaitable[None]] | None = None,
    ) -> Response:
        return await self.query_service.run_with_budget(
            lambda: self._page_response(
                document_id,
                page,
                access_token,
                request_id=request_id,
                on_complete=on_complete,
            ),
            label="source page response preparation",
        )

    async def _page_response(
        self,
        document_id: str,
        page: int,
        access_token: AccessToken,
        *,
        request_id: str,
        on_complete: Callable[[str, float], Awaitable[None]] | None,
    ) -> Response:
        document_id = validate_document_id(document_id)
        if page < 1:
            raise ValueError("page must be at least 1")
        source_page = await self.query_service.source_page(document_id, page)
        source_pdf = await self.query_service.source_pdf(document_id)
        self._assert_page_matches_pdf(source_page, source_pdf)
        path = await self._cached_page_path(source_pdf, source_page)
        await self.audit_attempt(
            request_id=request_id,
            action="source_page_png",
            access_token=access_token,
            source=source_pdf,
            document_id=source_pdf.document_id,
            page=page,
            requested_range=None,
            outcome="allowed",
        )
        return BoundedFileResponse(
            path,
            query_service=self.query_service,
            budget_label="source page stream",
            on_complete=on_complete,
            media_type="image/png",
            filename=f"{source_pdf.document_id}-page-{page}.png",
            headers={
                "Cache-Control": f"private, max-age={self.page_cache_ttl_seconds}",
                "ETag": f'"{source_pdf.sha256}-page-{page}"',
                "X-Source-PDF-SHA256": source_pdf.sha256,
                "X-Request-ID": request_id,
            },
        )

    async def verify_pdf(self, source: SourcePdf) -> Path:
        return await self.query_service.run_with_budget(
            lambda: self._verify_pdf(source),
            label="source PDF verification",
        )

    async def _verify_pdf(self, source: SourcePdf) -> Path:
        try:
            return await asyncio.to_thread(self._verify_pdf_sync, source)
        except asyncio.CancelledError:
            raise
        except InvalidSourceError:
            raise
        except Exception:
            raise InvalidSourceError("source failed integrity validation") from None

    def _verify_pdf_sync(self, source: SourcePdf) -> Path:
        try:
            root = self.storage_root.resolve(strict=True)
            candidate = source.path
            if not candidate.is_absolute():
                raise InvalidSourceError("source path is not absolute")
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
            if candidate != resolved:
                raise InvalidSourceError("source path is not canonical")
        except (OSError, ValueError):
            raise InvalidSourceError("source path is outside storage") from None

        # Reject a catalog path that traverses a child symlink, even when the
        # resolved target happens to remain under storage_root.
        cursor = candidate
        while cursor != self.storage_root and cursor != cursor.parent:
            if cursor.is_symlink():
                raise InvalidSourceError("source path contains a symlink")
            cursor = cursor.parent
        details = resolved.stat()
        if not stat.S_ISREG(details.st_mode):
            raise InvalidSourceError("source is not a regular file")
        if details.st_size != source.size_bytes or details.st_size > self.max_pdf_bytes:
            raise InvalidSourceError("source size does not match catalog")

        digest = hashlib.sha256()
        with resolved.open("rb") as stream:
            if stream.read(len(_PDF_MAGIC)) != _PDF_MAGIC:
                raise InvalidSourceError("source is not a PDF")
            stream.seek(0)
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != source.sha256:
            raise InvalidSourceError("source hash does not match catalog")
        return resolved

    async def cached_page_path(self, source_pdf: SourcePdf, source_page: SourcePage) -> Path:
        return await self.query_service.run_with_budget(
            lambda: self._cached_page_path(source_pdf, source_page),
            label="source page render",
        )

    async def _cached_page_path(self, source_pdf: SourcePdf, source_page: SourcePage) -> Path:
        verified_pdf = await self._verify_pdf(source_pdf)
        cache_key = hashlib.sha256(
            f"{source_pdf.sha256}:{source_page.page}:{self.render_scale}:rgb-v1".encode()
        ).hexdigest()
        target = self.page_cache_root / cache_key[:2] / f"{cache_key}.png"
        if await asyncio.to_thread(self._fresh_png, target):
            return target
        async with self._render_lock:
            if await asyncio.to_thread(self._fresh_png, target):
                return target
            await asyncio.to_thread(
                self._render_page_sync,
                verified_pdf,
                source_page,
                target,
            )
        return target

    def _fresh_png(self, target: Path) -> bool:
        try:
            details = target.stat()
            if not stat.S_ISREG(details.st_mode) or details.st_size <= len(_PNG_MAGIC):
                return False
            if time.time() - details.st_mtime > self.page_cache_ttl_seconds:
                return False
            with target.open("rb") as stream:
                return stream.read(len(_PNG_MAGIC)) == _PNG_MAGIC
        except OSError:
            return False

    def _render_page_sync(self, pdf_path: Path, source_page: SourcePage, target: Path) -> None:
        self.page_cache_root.mkdir(parents=True, exist_ok=True)
        cache_root = self.page_cache_root.resolve(strict=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.parent.resolve(strict=True).relative_to(cache_root)
        except (OSError, ValueError):
            raise InvalidSourceError("page cache path escaped cache root") from None
        if target.parent.is_symlink() or target.is_symlink():
            raise InvalidSourceError("page cache path contains a symlink")
        temporary: Path | None = None
        try:
            with fitz.open(pdf_path) as document:
                if document.page_count != source_page.page_count:
                    raise InvalidSourceError("catalog page count does not match PDF")
                if source_page.page > document.page_count:
                    raise InvalidSourceError("page is outside PDF")
                pdf_page = document.load_page(source_page.page - 1)
                pixmap = pdf_page.get_pixmap(
                    matrix=fitz.Matrix(self.render_scale, self.render_scale),
                    alpha=False,
                    colorspace=fitz.csRGB,
                )
                png = pixmap.tobytes("png")
            descriptor, name = tempfile.mkstemp(prefix=".cardrag-page-", suffix=".png", dir=target.parent)
            temporary = Path(name)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(png)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _assert_page_matches_pdf(self, page: SourcePage, pdf: SourcePdf) -> None:
        if page.document_id != pdf.document_id or page.pdf_sha256 != pdf.sha256:
            raise InvalidSourceError("page does not match source PDF")
        if page.version != pdf.version or page.issuer != pdf.issuer:
            raise InvalidSourceError("page provenance does not match source PDF")

    async def cleanup_expired(self) -> int:
        return await asyncio.to_thread(self._cleanup_expired_sync)

    def _cleanup_expired_sync(self) -> int:
        try:
            root = self.page_cache_root.resolve(strict=True)
        except OSError:
            return 0
        cutoff = time.time() - self.page_cache_ttl_seconds
        removed = 0
        for candidate in root.glob("*/*.png"):
            try:
                if candidate.is_symlink() or candidate.stat().st_mtime < cutoff:
                    candidate.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

    def pdf_url(self, document_id: str) -> str:
        return f"{self.public_origin}/sources/{quote(validate_document_id(document_id), safe='')}/pdf"

    def page_url(self, document_id: str, page: int) -> str:
        return (
            f"{self.public_origin}/sources/{quote(validate_document_id(document_id), safe='')}"
            f"/pages/{page}.png"
        )

    async def audit_attempt(
        self,
        *,
        request_id: str,
        action: Literal["source_pdf", "source_page_png"],
        access_token: AccessToken | None,
        source: SourcePdf | None,
        document_id: str,
        page: int | None,
        requested_range: str | None,
        outcome: Literal["allowed", "denied", "not_found", "invalid_source", "timeout"],
        required: bool = True,
    ) -> None:
        subject = "anonymous"
        client_id: str | None = None
        granted_scopes: tuple[str, ...] = ()
        if access_token is not None:
            subject = access_subject(access_token)
            client_id = access_token.client_id
            granted_scopes = tuple(sorted(set(access_token.scopes)))
        subject_hash = hashlib.sha256(f"{self.subject_namespace}\x00{subject}".encode()).hexdigest()
        event = AuditEvent(
            request_id=request_id,
            occurred_at=utc_now(),
            action=action,
            subject_hash=subject_hash,
            client_id=client_id,
            granted_scopes=granted_scopes,
            document_id=_audit_document_id(document_id),
            page=page,
            source_sha256=source.sha256 if source is not None else None,
            requested_range=_audit_range(requested_range),
            outcome=outcome,
        )
        recorder = getattr(self.repository, "record_audit", None)
        if recorder is None:
            if required:
                raise InvalidSourceError("source audit repository is unavailable")
            return
        try:

            async def persist() -> None:
                result = recorder(event)
                if inspect.isawaitable(result):
                    await _await_any(result)

            await self.query_service.record_auxiliary(persist, label="source access audit")
        except asyncio.CancelledError:
            raise
        except Exception:
            if required:
                raise InvalidSourceError("source audit repository is unavailable") from None

    @staticmethod
    def request_id() -> str:
        return f"req_{uuid.uuid4().hex}"


async def _await_any(value: Awaitable[Any]) -> Any:
    return await value


def _valid_range(value: str, size: int) -> bool:
    if len(value) > 200 or "," in value:
        return False
    match = _RANGE.fullmatch(value)
    if match is None:
        return False
    start_text, end_text = match.groups()
    if not start_text and not end_text:
        return False
    if start_text:
        start = int(start_text)
        if start >= size:
            return False
        if end_text and int(end_text) < start:
            return False
    elif int(end_text) <= 0:
        return False
    return True


def _audit_range(value: str | None) -> str | None:
    """Retain useful byte-range metadata without persisting arbitrary headers."""

    if value is None:
        return None
    if len(value) <= 200 and "," not in value and _RANGE.fullmatch(value) is not None:
        return value
    return "invalid"


def _audit_document_id(value: str) -> str:
    """Keep canonical IDs exact and hash all untrusted/noncanonical values."""

    if _CANONICAL_DOCUMENT_ID.fullmatch(value):
        return value
    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()
    return f"invalid_{digest}"
