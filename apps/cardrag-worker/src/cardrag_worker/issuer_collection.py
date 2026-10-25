"""Issuer-local origin failures, independent of shared Worker storage failures."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from typing import Any

import httpx
from cardrag_core import WebDAVError

_SAFE_MIME = re.compile(r"[a-z0-9][a-z0-9.+-]{0,63}/[a-z0-9][a-z0-9.+-]{0,63}\Z")
_SAFE_SIGNAL = re.compile(r"[a-z0-9_]{1,64}\Z")
_SERVICE_ERROR_MARKERS = (
    "서비스가 원활하지 않습니다",
    "service unavailable",
    "temporarily unavailable",
    "currently unavailable",
    "internal server error",
)
_HTML_MEDIA_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_SKIP_HTML_TAGS = frozenset({"script", "style", "template", "noscript", "svg"})
_VOID_HTML_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_HTML_HEADING_TAGS = frozenset({"title", "h1", "h2", "h3"})
_PAGE_ERROR_REGION_TOKENS = (
    "service-error",
    "service_error",
    "error-page",
    "error_page",
    "error-container",
    "error_container",
    "finishbox",
    "finish-box",
)
_HIDDEN_STYLE = re.compile(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", re.IGNORECASE)
_ERROR_HEADING = re.compile(
    r"(?:^|[\s.:])(?:error|service\s+error|오류|서비스\s*오류|service\s+unavailable|"
    r"temporarily\s+unavailable|currently\s+unavailable|internal\s+server\s+error)(?:$|[\s.:])",
    re.IGNORECASE,
)


def safe_mime_type(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.split(";", 1)[0].strip().casefold()
    return normalized if _SAFE_MIME.fullmatch(normalized) is not None else None


class _VisibleHtmlSummary(HTMLParser):
    """Collect only visible text/headings; never retain or return raw HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool, bool, bool]] = []
        self.visible: list[str] = []
        self.headings: list[str] = []
        self.error_region_text: list[str] = []

    def _start(self, tag: str, attrs: list[tuple[str, str | None]], *, push: bool) -> None:
        values = {key.casefold(): (value or "") for key, value in attrs}
        tag = tag.casefold()
        parent_hidden = any(hidden for _, hidden, _, _ in self.stack)
        hidden = parent_hidden or tag in _SKIP_HTML_TAGS
        hidden = hidden or "hidden" in values or values.get("aria-hidden", "").casefold() == "true"
        hidden = hidden or _HIDDEN_STYLE.search(values.get("style", "")) is not None
        marker = f"{values.get('class', '')} {values.get('id', '')}".casefold()
        error_region = (
            not hidden
            and tag in {"body", "main", "section", "article", "div"}
            and any(token in marker for token in _PAGE_ERROR_REGION_TOKENS)
        )
        if push and tag not in _VOID_HTML_TAGS:
            self.stack.append((tag, hidden, tag in _HTML_HEADING_TAGS, error_region))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs, push=True)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs, push=False)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if not self.stack or any(hidden for _, hidden, _, _ in self.stack):
            return
        text = " ".join(data.split())
        if not text:
            return
        self.visible.append(text)
        if any(is_heading for _, _, is_heading, _ in self.stack):
            self.headings.append(text)
        if any(is_error_region for _, _, _, is_error_region in self.stack):
            self.error_region_text.append(text)


def classify_html_service_error(content: bytes, content_type: str | None) -> str | None:
    """Return a bounded signal only for clearly visible HTML service-error pages."""

    if not content:
        return None
    media_type = safe_mime_type(content_type)
    prefix = content[:65536].lstrip().lower()
    if media_type not in _HTML_MEDIA_TYPES and not prefix.startswith((b"<!doctype html", b"<html", b"<head")):
        return None
    prefix_bytes = content[:65536]
    for encoding in ("utf-8", "cp949"):
        try:
            text = prefix_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
        summary = _VisibleHtmlSummary()
        try:
            summary.feed(text)
        except Exception:  # noqa: S112 - malformed HTML must not become a service-error signal
            continue
        visible = " ".join(summary.visible).casefold()
        if not any(marker in visible for marker in _SERVICE_ERROR_MARKERS):
            continue
        headings = " ".join(summary.headings)
        error_region_text = " ".join(summary.error_region_text).casefold()
        if _ERROR_HEADING.search(headings) is not None or any(
            marker in error_region_text for marker in _SERVICE_ERROR_MARKERS
        ):
            return "visible_service_error_page"
    return None


class IssuerCollectionError(RuntimeError):
    """A safe origin/parser verdict; never retains response text or credentials."""

    def __init__(
        self,
        reason_code: str,
        exception_class: str,
        *,
        status_code: int | None = None,
        mime_type: str | None = None,
        service_signal: str | None = None,
    ) -> None:
        if status_code is not None and (isinstance(status_code, bool) or not 100 <= status_code <= 599):
            raise ValueError("issuer collection status_code is invalid")
        if mime_type is not None and (
            not isinstance(mime_type, str) or safe_mime_type(mime_type) != mime_type
        ):
            raise ValueError("issuer collection mime_type is invalid")
        if service_signal is not None and (
            not isinstance(service_signal, str) or _SAFE_SIGNAL.fullmatch(service_signal) is None
        ):
            raise ValueError("issuer collection service_signal is invalid")
        self.reason_code = reason_code
        self.exception_class = exception_class
        self.status_code = status_code
        self.mime_type = mime_type
        self.service_signal = service_signal
        super().__init__(reason_code)


class OriginServiceError(IssuerCollectionError):
    """An HTTP response was a verified service-error page, not a document."""

    def __init__(
        self,
        *,
        status_code: int,
        mime_type: str | None,
        service_signal: str,
    ) -> None:
        super().__init__(
            "issuer_origin_service_error",
            "OriginServiceError",
            status_code=status_code,
            mime_type=mime_type,
            service_signal=service_signal,
        )


class IssuerCollectionFailedError(RuntimeError):
    """The selected issuer scope had no successful discovery/download at all."""

    def __init__(
        self,
        *,
        reason_code: str,
        exception_class: str,
        stage: str,
        document_id: str | None = None,
        status_code: int | None = None,
        mime_type: str | None = None,
        service_signal: str | None = None,
    ) -> None:
        if not re.fullmatch(r"[a-z0-9_]{1,64}", reason_code):
            raise ValueError("issuer collection failure reason_code is invalid")
        if not re.fullmatch(r"[a-z0-9_-]{1,64}", stage):
            raise ValueError("issuer collection failure stage is invalid")
        if document_id is not None and re.fullmatch(r"(?:source|doc)_[0-9a-f]{64}", document_id) is None:
            raise ValueError("issuer collection failure document_id is invalid")
        if status_code is not None and (isinstance(status_code, bool) or not 100 <= status_code <= 599):
            raise ValueError("issuer collection failure status_code is invalid")
        if mime_type is not None and (
            not isinstance(mime_type, str) or safe_mime_type(mime_type) != mime_type
        ):
            raise ValueError("issuer collection failure mime_type is invalid")
        if service_signal is not None and (
            not isinstance(service_signal, str) or _SAFE_SIGNAL.fullmatch(service_signal) is None
        ):
            raise ValueError("issuer collection failure service_signal is invalid")
        self.reason_code = reason_code
        self.exception_class = exception_class
        self.stage = stage
        self.document_id = document_id
        self.status_code = status_code
        self.mime_type = mime_type
        self.service_signal = service_signal
        super().__init__("all issuer collection attempts failed")


def origin_failure(exc: Exception) -> IssuerCollectionError:
    if isinstance(exc, IssuerCollectionError):
        return exc
    # These indicate shared/local infrastructure, even inside an adapter hook.
    if isinstance(exc, (OSError, sqlite3.Error, WebDAVError, MemoryError)):
        raise exc
    if isinstance(exc, httpx.TimeoutException):
        return IssuerCollectionError("issuer_origin_timeout", type(exc).__name__)
    if isinstance(exc, httpx.RequestError):
        return IssuerCollectionError("issuer_origin_network", type(exc).__name__)
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        return IssuerCollectionError(
            "issuer_origin_http_status",
            type(exc).__name__,
            status_code=response.status_code,
            mime_type=safe_mime_type(response.headers.get("content-type")),
        )
    return IssuerCollectionError("issuer_source_invalid", type(exc).__name__)


@dataclass(slots=True)
class IssuerCollectionOutcome:
    issuer: str
    discovery_status: str = "pending"
    download_status: str = "pending"
    reason_code: str | None = None
    exception_class: str | None = None
    source_id: str | None = None
    origin_status_code: int | None = None
    origin_mime_type: str | None = None
    origin_service_signal: str | None = None
    attempts: int = 0
    elapsed_seconds: float = 0
    discovered_count: int = 0
    acquired_count: int = 0
    adopted_count: int = 0
    carried_count: int = 0
    last_successful_collection_at: str | None = None
    origin_freshness_at: str | None = None
    carry_generation_id: str | None = None
    consecutive_failed_runs: int | None = None

    @property
    def failed(self) -> bool:
        return self.reason_code is not None

    def payload(self) -> dict[str, Any]:
        return asdict(self)
