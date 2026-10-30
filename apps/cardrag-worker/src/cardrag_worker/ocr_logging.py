"""Bounded, invocation-scoped OCR diagnostics without provider payloads."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, TypeVar

import httpx

LOGGER = logging.getLogger("cardrag_worker.ocr")
T = TypeVar("T")
_SECRET = re.compile(
    r"(?i)(secret|bearer|authorization|password|api[_ -]?key|token|cookie|https?://|sk-[a-z0-9])"
)
_SAFE_TEXT = re.compile(r"^[\w가-힣 .,:+()\-/&]+$", re.UNICODE)


@dataclass
class Invocation:
    invocation_id: str = field(default_factory=lambda: secrets.token_hex(12))
    run_id: str | None = None
    counts: Counter[str] = field(default_factory=Counter)
    documents: set[str] = field(default_factory=set)
    document_calls: Counter[str] = field(default_factory=Counter)


@dataclass
class Document:
    fields: dict[str, Any]
    last_call: dict[str, Any] = field(default_factory=dict)
    last_failure: dict[str, Any] = field(default_factory=dict)


_invocation: ContextVar[Invocation | None] = ContextVar("ocr_invocation", default=None)
_document: ContextVar[Document | None] = ContextVar("ocr_document", default=None)
_phase: ContextVar[str] = ContextVar("ocr_phase", default="primary")
_call: ContextVar[dict[str, Any] | None] = ContextVar("ocr_call", default=None)


def _safe_value(value: Any, limit: int = 256) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if 0 <= value < 1e12 else None
    if (
        not isinstance(value, str)
        or len(value) > limit
        or not _SAFE_TEXT.fullmatch(value)
        or _SECRET.search(value)
    ):
        return None
    try:
        from .providers import reject_credential_bearing_ocr

        reject_credential_bearing_ocr(value)
    except Exception:
        return None
    return value


def emit(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """Diagnostic failure cannot replace an OCR failure or result."""
    try:
        inv = _invocation.get()
        doc = _document.get()
        payload: dict[str, Any] = {"schema_version": "cardrag.ocr-event.v1", "event": event}
        if inv:
            payload.update(run_id=inv.run_id, invocation_id=inv.invocation_id)
        if doc:
            payload.update(doc.fields)
        payload.update(fields)
        safe = {
            key: _safe_value(value, 160 if key == "product_name" else 256) for key, value in payload.items()
        }
        for key in ("product_name", "product_code", "model", "requested_model"):
            if payload.get(key) is not None and safe.get(key) is None:
                safe[f"{key}_omitted_reason"] = "unsafe_or_too_long"
        message = json.dumps(safe, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        if len(message) > 7800:
            message = json.dumps(
                {"schema_version": "cardrag.ocr-event.v1", "event": event, "truncated": True}
            )
        LOGGER.log(level, "OCR_EVENT %s", message)
    except Exception:  # noqa: S110
        pass


def begin_invocation() -> Any:
    return _invocation.set(Invocation())


def set_run_id(run_id: str) -> None:
    inv = _invocation.get()
    if inv:
        inv.run_id = run_id


def set_stage_attempt(attempt: int) -> None:
    doc = _document.get()
    if doc:
        doc.fields["stage_attempt"] = attempt


def attempt_count() -> int | None:
    inv, doc = _invocation.get(), _document.get()
    doc_id = doc.fields.get("document_id") if doc else None
    return inv.document_calls.get(doc_id, 0) if inv and isinstance(doc_id, str) else None


def end_invocation(token: Any, *, partial: bool) -> None:
    inv = _invocation.get()
    if inv:
        emit(
            "ocr_invocation_summary",
            partial=partial,
            provider_attempts_total=inv.counts["attempts"],
            returned_total=inv.counts["returned"],
            failed_total=inv.counts["failed"],
            cancelled_total=inv.counts["cancelled"],
            provider_documents_total=len(inv.documents),
            fallback_total=inv.counts["fallback"],
            corrective_total=inv.counts["corrective"],
        )
    _invocation.reset(token)


@contextmanager
def document_scope(**fields: Any) -> Iterator[Document]:
    doc = Document(fields)
    token = _document.set(doc)
    try:
        yield doc
    finally:
        _document.reset(token)


@contextmanager
def phase_scope(phase: str) -> Iterator[None]:
    token = _phase.set(phase)
    try:
        yield
    finally:
        _phase.reset(token)


def diagnostic(exc: BaseException) -> dict[str, Any]:
    try:
        return _diagnostic(exc)
    except Exception:
        return {"reason_code": "unknown", "error_kind": "unknown"}


def _diagnostic(exc: BaseException) -> dict[str, Any]:
    """Extract only bounded typed metadata; never serialize exception text."""
    from .providers import ProviderDocumentError, ProviderSystemicError

    if isinstance(exc, ProviderSystemicError):
        canonical = ProviderSystemicError(exc.reason_code, exit_code=exc.exit_code)
        with suppress(ValueError, TypeError):
            canonical = ProviderSystemicError(
                exc.reason_code,
                exit_code=exc.exit_code,
                stderr_size_bytes=exc.stderr_size_bytes,
                stderr_sha256=exc.stderr_sha256,
                stdout_size_bytes=exc.stdout_size_bytes,
                stdout_sha256=exc.stdout_sha256,
            )
        basis = exc.classification_basis
        if basis not in {
            "stderr_allowlist",
            "stderr_allowlist_no_match",
            "stdout_error_event_no_match",
            "stdout_error_event_allowlist",
            "fixed_exit_policy",
        }:
            basis = None
        return {
            key: basis if key == "classification_basis" else getattr(canonical, key, None)
            for key in (
                "reason_code",
                "error_kind",
                "retryable",
                "classification_basis",
                "exit_code",
                "stderr_size_bytes",
                "stderr_sha256",
                "stdout_size_bytes",
                "stdout_sha256",
            )
        }
    if isinstance(exc, ProviderDocumentError):
        return {
            "reason_code": "provider_document_rejected",
            "error_kind": "document",
            "retryable": True,
            "exit_code": 65 if exc.exit_code == 65 else None,
        }
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return {"reason_code": "provider_http_error", "status_code": status, "error_kind": "http"}
    if isinstance(exc, httpx.TimeoutException):
        return {"reason_code": "provider_timeout", "error_kind": type(exc).__name__}
    if isinstance(exc, TimeoutError):
        return {"reason_code": "provider_timeout", "error_kind": "timeout"}
    if isinstance(exc, httpx.RequestError):
        return {"reason_code": "provider_network_error", "error_kind": "network"}
    code = getattr(exc, "ocr_diagnostic_code", None)
    if code in {
        "generic_validation_error",
        "output_not_marker_first",
        "page_marker_mismatch",
        "empty_page",
        "page_count_mismatch",
        "sparse_page_wrapper_invalid",
        "blank_sentinel_invalid",
        "implausibly_short",
    }:
        return {"reason_code": code, "error_kind": "validation"}
    try:
        from cardrag_core.ocr import OCRVerificationError
    except Exception:
        OCRVerificationError = None  # type: ignore[assignment,misc]
    if OCRVerificationError is not None and isinstance(exc, OCRVerificationError):
        return {"reason_code": "final_ocr_verification_failed", "error_kind": "validation"}
    return {"reason_code": "unknown", "error_kind": "unknown"}


async def provider_call(  # noqa: UP047
    provider: Any,
    operation: Callable[[], Awaitable[T]],
    *,
    call_kind: str,
    chunk_index: int | None,
    page_start: int,
    page_end: int,
    input_page_count: int,
) -> T:
    inv = _invocation.get()
    doc = _document.get()
    call_id = secrets.token_hex(12)
    ordinal: int | None = None
    if inv:
        inv.counts["attempts"] += 1
        ordinal = inv.counts["attempts"]
        if call_kind == "corrective":
            inv.counts["corrective"] += 1
        if doc and isinstance(doc.fields.get("document_id"), str):
            doc_id = doc.fields["document_id"]
            inv.documents.add(doc_id)
            inv.document_calls[doc_id] += 1
    fields: dict[str, Any] = {
        "call_id": call_id,
        "call_ordinal": ordinal,
        "phase": _phase.get(),
        "call_kind": call_kind,
        "provider": getattr(provider, "provider", None),
        "model": getattr(provider, "model", None),
        "model_source": "requested",
        "reasoning_effort": getattr(provider, "reasoning_effort", None),
        "timeout_seconds": getattr(provider, "timeout_seconds", None),
        "chunk_index": chunk_index,
        "page_start": page_start,
        "page_end": page_end,
        "input_page_count": input_page_count,
        "provider_attempts_total": ordinal,
        "provider_documents_total": len(inv.documents) if inv else None,
    }
    if doc:
        doc.last_call = fields
        doc.last_failure = {}
    emit("ocr_provider_call_started", **fields)
    started = time.monotonic()
    token = _call.set(fields)
    try:
        result = await operation()
    except asyncio.CancelledError:
        if inv:
            inv.counts["cancelled"] += 1
        emit("ocr_provider_call_cancelled", **fields, elapsed_seconds=round(time.monotonic() - started, 3))
        raise
    except Exception as exc:
        info = diagnostic(exc)
        if doc:
            doc.last_failure = info
        if inv:
            inv.counts["failed"] += 1
        emit(
            "ocr_provider_call_failed",
            level=logging.WARNING,
            **fields,
            **info,
            elapsed_seconds=round(time.monotonic() - started, 3),
        )
        raise
    else:
        if inv:
            inv.counts["returned"] += 1
        emit("ocr_provider_call_returned", **fields, elapsed_seconds=round(time.monotonic() - started, 3))
        return result
    finally:
        _call.reset(token)


def validation_failed(
    exc: BaseException,
    *,
    validation_phase: str | None = None,
    validation_scope: str | None = None,
    page_count: int | None = None,
) -> None:
    doc = _document.get()
    info = diagnostic(exc)
    if doc:
        doc.last_failure = info
    extra: dict[str, Any] = {}
    if validation_phase is not None:
        extra["validation_phase"] = validation_phase

    is_document_scope = validation_scope == "document" or validation_phase == "final_chunk_combine"
    if is_document_scope:
        extra["validation_scope"] = "document"
        base_call = dict(doc.last_call) if doc else {}
        last_call_id = base_call.get("call_id")
        if last_call_id:
            extra["last_call_id"] = last_call_id
        call_context = {
            k: v
            for k, v in base_call.items()
            if k
            not in (
                "call_id",
                "call_ordinal",
                "call_kind",
                "chunk_index",
                "page_start",
                "page_end",
                "input_page_count",
            )
        }
        call_context["call_id"] = None
        call_context["chunk_index"] = None
        eff_pages = page_count or (doc.fields.get("page_count") if doc else None)
        if isinstance(eff_pages, int) and eff_pages >= 1:
            extra["page_start"] = 1
            extra["page_end"] = eff_pages
            extra["input_page_count"] = eff_pages
    else:
        call_context = dict(doc.last_call) if doc else {}

    emit(
        "ocr_output_validation_failed",
        level=logging.WARNING,
        **call_context,
        **info,
        **extra,
    )


def fallback_started(info: dict[str, Any], primary: Any, fallback: Any) -> None:
    inv, doc = _invocation.get(), _document.get()
    if inv:
        inv.counts["fallback"] += 1
    emit(
        "ocr_fallback_started",
        level=logging.WARNING,
        **(doc.last_call if doc else {}),
        **info,
        fallback_provider=getattr(fallback, "provider", None),
        fallback_model=getattr(fallback, "model", None),
        primary_provider=getattr(primary, "provider", None),
        primary_model=getattr(primary, "model", None),
    )


def document_resolved(result: Any) -> None:
    inv, doc = _invocation.get(), _document.get()
    doc_id = doc.fields.get("document_id") if doc else None
    count = inv.document_calls.get(doc_id, 0) if inv and isinstance(doc_id, str) else 0
    if count:
        emit(
            "ocr_document_resolved",
            provider=getattr(result, "provider", None),
            model=getattr(result, "model", None),
            model_source="artifact_contract",
            provenance=getattr(result, "provenance", None),
            cache_reused=getattr(result, "cache_reused", None),
            document_provider_attempts=count,
            size_bytes=getattr(result, "size_bytes", None),
        )


async def wire_call(model: str, ordinal: int, operation: Callable[[], Awaitable[T]]) -> T:  # noqa: UP047
    call = _call.get()
    if call is None:
        return await operation()
    fields = {
        "parent_call_id": call["call_id"],
        "wire_ordinal": ordinal,
        "requested_model": model,
        "actual_model": None,
        "phase": "provider_internal",
    }
    emit("ocr_wire_started", **fields)
    try:
        result = await operation()
    except asyncio.CancelledError:
        emit("ocr_wire_cancelled", **fields)
        raise
    except Exception as exc:
        emit("ocr_wire_failed", level=logging.WARNING, **fields, **diagnostic(exc))
        raise
    emit("ocr_wire_returned", **fields)
    return result
