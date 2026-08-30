"""OCR cache priority and restartable local page-chunk processing."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
import warnings
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx
import pypdfium2 as pdfium  # type: ignore[import-untyped]
from cardrag_core import (
    OCRCacheKind,
    WebDAVHTTPError,
    WebDAVIntegrityError,
    canonical_json_bytes,
)
from cardrag_core.canonical import canonical_sha256, sha256_bytes
from cardrag_core.domain import ArtifactRef
from cardrag_core.manifests import (
    LEGACY_ADOPTION_POLICY_V1,
    LEGACY_ADOPTION_POLICY_V2,
    AdoptedOCRArtifactManifest,
    OCRArtifactManifest,
    OCRReady,
    adopted_ocr_reuse_key,
)
from cardrag_core.ocr import (
    NativeOCRContract,
    OCRInput,
    native_ocr_reuse_key,
    verify_ocr_bytes,
)
from cardrag_core.paths import ocr_manifest_path, ocr_ready_path

from .contracts import PageRecord
from .providers import (
    DEFAULT_OCR_PROMPT,
    OCR_BLANK_PAGE_SENTINEL,
    OCR_SPARSE_PAGE_PREFIX,
    OCRProvider,
    ProviderDocumentError,
    reject_credential_bearing_ocr,
)
from .state import WorkerState
from .webdav import WebDAVClient

PAGE_MARKER = re.compile(r"^## Page ([1-9][0-9]*)$", re.MULTILINE)
OCR_SPARSE_PAGE_MAX_VISIBLE_CHARACTERS = 12
OCR_PROCESSOR_VERSION = "cardrag-worker/1.0.4"
OCR_SEGMENTATION_STRATEGY_ID = "cardrag.ocr.windowed-continuity.v1"
OCR_OUTPUT_POLICY: Literal["target-pages-only"] = "target-pages-only"
OCR_CACHE_PUBLICATION_RETRY_DELAYS_SECONDS = (0.25, 1.0)
OCR_CACHE_PUBLICATION_DIAGNOSTIC = "native-cache-publication-diagnostic.json"
OCR_CACHE_PUBLICATION_DIAGNOSTIC_MAX_BYTES = 4096

OCRCacheMode = Literal["read-only", "read-write"]
OCRCachePublicationPhase = Literal["cas", "manifest", "ready"]
OCRCachePublicationErrorKind = Literal[
    "network",
    "timeout",
    "http",
    "integrity",
    "contract",
    "unexpected",
]

# 423 is WebDAV's resource-locked response.  At an immutable publication
# boundary it can clear without any contract change, so treat it like the
# other bounded-retry statuses rather than an integrity/authentication error.
_OCR_CACHE_TRANSIENT_HTTP_STATUSES = frozenset({408, 423, 425, 429, *range(500, 600)})
_OCR_CACHE_PUBLICATION_REASONS: dict[OCRCachePublicationErrorKind, str] = {
    "network": "OCR cache publication network is temporarily unavailable",
    "timeout": "OCR cache publication timed out",
    "http": "OCR cache publication received an HTTP failure status",
    "integrity": "OCR cache publication integrity verification failed",
    "contract": "OCR cache publication contract validation failed",
    "unexpected": "OCR cache publication failed unexpectedly",
}


class OCRValidationError(RuntimeError):
    pass


class OCRCachePublicationError(RuntimeError):
    """Secret-safe, phase-aware failure raised by native cache publication."""

    def __init__(
        self,
        *,
        phase: OCRCachePublicationPhase,
        error_kind: OCRCachePublicationErrorKind,
        status_code: int | None = None,
        retryable: bool = False,
        attempts: int = 1,
    ) -> None:
        if attempts < 1:
            raise ValueError("attempts must be positive")
        self.phase = phase
        self.error_kind = error_kind
        self.status_code = status_code
        self.retryable = retryable
        self.attempts = attempts
        self.reason_code = f"ocr_cache_publication_{phase}_{error_kind}"
        self.reason = _OCR_CACHE_PUBLICATION_REASONS[error_kind]
        status = f", status_code={status_code}" if status_code is not None else ""
        super().__init__(
            f"{self.reason_code}: {self.reason} "
            f"(phase={phase}, retryable={str(retryable).lower()}, attempts={attempts}{status})"
        )


def _exception_chain(exc: BaseException) -> tuple[BaseException, ...]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(chain) < 8:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return tuple(chain)


def _cache_publication_error(
    phase: OCRCachePublicationPhase,
    exc: Exception,
    *,
    attempts: int = 1,
) -> OCRCachePublicationError:
    if isinstance(exc, OCRCachePublicationError):
        return OCRCachePublicationError(
            phase=exc.phase,
            error_kind=exc.error_kind,
            status_code=exc.status_code,
            retryable=exc.retryable,
            attempts=attempts,
        )
    chain = _exception_chain(exc)
    http_failure = next((item for item in chain if isinstance(item, WebDAVHTTPError)), None)
    if isinstance(http_failure, WebDAVHTTPError):
        status_code = http_failure.status_code
        if status_code in {409, 412}:
            return OCRCachePublicationError(
                phase=phase,
                error_kind="contract",
                status_code=status_code,
                attempts=attempts,
            )
        return OCRCachePublicationError(
            phase=phase,
            error_kind="http",
            status_code=status_code,
            retryable=status_code in _OCR_CACHE_TRANSIENT_HTTP_STATUSES,
            attempts=attempts,
        )
    if any(isinstance(item, WebDAVIntegrityError) for item in chain):
        return OCRCachePublicationError(
            phase=phase,
            error_kind="integrity",
            attempts=attempts,
        )
    if any(isinstance(item, (httpx.TimeoutException, TimeoutError)) for item in chain):
        return OCRCachePublicationError(
            phase=phase,
            error_kind="timeout",
            retryable=True,
            attempts=attempts,
        )
    if any(isinstance(item, (httpx.UnsupportedProtocol, httpx.LocalProtocolError)) for item in chain):
        # These describe the request/client configuration, not a remote outage.
        # Retrying the same immutable publication cannot repair them.
        return OCRCachePublicationError(
            phase=phase,
            error_kind="contract",
            attempts=attempts,
        )
    if any(
        isinstance(item, (httpx.NetworkError, httpx.ProxyError, ConnectionError))
        or (isinstance(item, httpx.ProtocolError) and not isinstance(item, httpx.LocalProtocolError))
        for item in chain
    ):
        # Remote protocol disconnects and proxy-connection failures are
        # RequestError siblings of NetworkError in HTTPX, but are equally
        # recoverable at this bounded publication boundary.
        return OCRCachePublicationError(
            phase=phase,
            error_kind="network",
            retryable=True,
            attempts=attempts,
        )
    if any(isinstance(item, (ValueError, TypeError, OCRValidationError)) for item in chain):
        return OCRCachePublicationError(
            phase=phase,
            error_kind="contract",
            attempts=attempts,
        )
    return OCRCachePublicationError(
        phase=phase,
        error_kind="unexpected",
        attempts=attempts,
    )


def atomic_write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _page_body(page_with_marker: str) -> str:
    first_line, separator, remainder = page_with_marker.partition("\n")
    if not separator or not PAGE_MARKER.fullmatch(first_line):
        raise OCRValidationError("verified OCR page has no canonical marker")
    return remainder.strip()


def split_ocr_pages(text: str, *, expected_count: int, first_page: int = 1) -> tuple[str, ...]:
    matches = list(PAGE_MARKER.finditer(text))
    if matches and matches[0].start() != 0:
        raise OCRValidationError("OCR provider output must begin with the first Page marker")
    numbers = [int(match.group(1)) for match in matches]
    expected = list(range(first_page, first_page + expected_count))
    if numbers != expected:
        raise OCRValidationError(f"OCR page markers {numbers} do not match {expected}")
    pages = tuple(
        text[match.end() : matches[index + 1].start() if index + 1 < len(matches) else len(text)].strip()
        for index, match in enumerate(matches)
    )
    if any(len(page) < 1 for page in pages):
        raise OCRValidationError("OCR contains an empty page")
    return pages


def render_pdf(pdf_path: Path, output_dir: Path, *, scale: float = 6.0) -> tuple[Path, ...]:
    output_dir.mkdir(parents=True, exist_ok=True)
    document = pdfium.PdfDocument(str(pdf_path))
    paths: list[Path] = []
    try:
        if len(document) < 1:
            raise OCRValidationError("OCR input has no pages")
        for index in range(len(document)):
            destination = output_dir / f"page-{index + 1:04d}.png"
            # Rendering is part of the OCR contract. A filename left by an
            # older renderer or scale is not evidence that its pixels match
            # this run, so every page is atomically rendered again before
            # checkpoint input hashes are evaluated.
            page = document[index]
            try:
                image = page.render(scale=scale).to_pil()
                temporary = destination.with_suffix(".tmp.png")
                image.save(temporary, format="PNG")
                temporary.replace(destination)
            finally:
                page.close()
            paths.append(destination)
    finally:
        document.close()
    return tuple(paths)


def _validate_and_normalize_target_page_values(
    page_numbers: tuple[int, ...],
    values: tuple[str, ...],
) -> tuple[str, ...]:
    """Apply minimum-page invariants and canonicalize sparse-page whitespace."""

    if len(page_numbers) != len(values):
        raise OCRValidationError("OCR page/value count differs")
    normalized: list[str] = []
    for page_number, value in zip(page_numbers, values, strict=True):
        if OCR_SPARSE_PAGE_PREFIX in value:
            lines = value.splitlines()
            visible_lines = tuple(line.strip() for line in lines[1:] if line.strip())
            if lines[:1] != [OCR_SPARSE_PAGE_PREFIX]:
                raise OCRValidationError("OCR sparse-page wrapper is invalid")
            if any(
                PAGE_MARKER.fullmatch(line) or line in {OCR_BLANK_PAGE_SENTINEL, OCR_SPARSE_PAGE_PREFIX}
                for line in visible_lines
            ):
                raise OCRValidationError("OCR sparse-page wrapper is invalid")
            visible_character_count = sum(len("".join(line.split())) for line in visible_lines)
            if visible_character_count > OCR_SPARSE_PAGE_MAX_VISIBLE_CHARACTERS:
                raise OCRValidationError("OCR sparse-page wrapper is invalid")
            # Markdown generators commonly insert one blank line after the
            # wrapper or between disconnected logo elements. A logo/pictogram-
            # only page can also contain zero source characters. Blank-only
            # lines carry no source information, so persist one stable
            # representation while retaining every visible transcription line
            # in source order.
            value = OCR_SPARSE_PAGE_PREFIX
            if visible_lines:
                value += f"\n{'\n'.join(visible_lines)}"
        elif value.startswith(OCR_BLANK_PAGE_SENTINEL) and value != OCR_BLANK_PAGE_SENTINEL:
            raise OCRValidationError("OCR blank-page sentinel must be exact")
        if len(f"## Page {page_number}\n\n{value}") < 20:
            raise OCRValidationError("OCR provider returned an implausibly short page")
        normalized.append(value)
    return tuple(normalized)


@dataclass(frozen=True, slots=True)
class OCRResult:
    pages: tuple[str, ...]
    ocr_bytes: bytes
    ocr_text: str
    ocr_sha256: str
    size_bytes: int
    provenance: str
    provider: str
    model: str
    reuse_key: str
    cache_kind: OCRCacheKind | None = None
    cache_reuse_key: str | None = None
    cache_publication_deferred: bool = False
    cache_publication_reason_code: str | None = None
    cache_reused: bool = False
    provider_called: bool = False

    def __post_init__(self) -> None:
        reject_credential_bearing_ocr(self.ocr_bytes)
        if type(self.cache_reused) is not bool or type(self.provider_called) is not bool:
            raise ValueError("OCR cache/provider metrics must be booleans")
        if (self.cache_kind is None) != (self.cache_reuse_key is None):
            raise ValueError("OCR cache kind and reuse key must be present together")
        if self.cache_publication_deferred != (self.cache_publication_reason_code is not None):
            raise ValueError("deferred OCR cache publication and reason code must be present together")
        if self.cache_publication_reason_code is not None and not re.fullmatch(
            r"ocr_cache_publication_(?:cas|manifest|ready)_"
            r"(?:network|timeout|http|integrity|contract|unexpected)",
            self.cache_publication_reason_code,
        ):
            raise ValueError("OCR cache publication reason code is not allowlisted")
        try:
            verified = verify_ocr_bytes(
                self.ocr_bytes,
                expected_page_count=len(self.pages),
                expected_sha256=self.ocr_sha256,
                expected_size_bytes=self.size_bytes,
            )
        except Exception as exc:
            raise OCRValidationError("OCR result bytes failed strict verification") from exc
        if verified.text != self.ocr_text:
            raise OCRValidationError("OCR result text does not match its exact bytes")
        if tuple(_page_body(page) for page in verified.pages) != self.pages:
            raise OCRValidationError("OCR result pages do not match its exact bytes")


@dataclass(frozen=True, slots=True)
class PriorLocalNativeSource:
    """A generation-seal-bound local native OCR candidate from a prior run.

    The pipeline constructs this only after validating the retained publication
    seal.  The resolver still revalidates the filesystem boundary, native OCR
    manifest, source/processor contract, reuse key, and exact generation OCR
    identity before any bytes are materialized into the new run.
    """

    runs_root: Path
    run_id: str
    generation_id: str
    corpus_sha256: str
    contract_sha256: str
    document_id: str
    pdf_sha256: str
    pdf_size_bytes: int
    page_count: int
    ocr_sha256: str
    ocr_size_bytes: int
    resolver_subdir: Literal["primary", "fallback"] | None = None


@dataclass(frozen=True, slots=True)
class OCRCall:
    """One provider call with target output pages and read-only visual context."""

    target_page_numbers: tuple[int, ...]
    input_page_numbers: tuple[int, ...]


def plan_ocr_calls(
    *,
    page_count: int,
    whole_document_max_pages: int,
    target_pages_per_call: int,
    context_pages_before: int,
    context_pages_after: int,
) -> tuple[OCRCall, ...]:
    if page_count < 1:
        raise ValueError("page_count must be positive")
    if whole_document_max_pages < 1:
        raise ValueError("whole_document_max_pages must be positive")
    if target_pages_per_call < 1:
        raise ValueError("target_pages_per_call must be positive")
    if context_pages_before < 0 or context_pages_after < 0:
        raise ValueError("OCR context page counts must be non-negative")
    all_pages = tuple(range(1, page_count + 1))
    if page_count <= whole_document_max_pages:
        return (OCRCall(target_page_numbers=all_pages, input_page_numbers=all_pages),)
    calls: list[OCRCall] = []
    for first_target in range(1, page_count + 1, target_pages_per_call):
        last_target = min(page_count, first_target + target_pages_per_call - 1)
        targets = tuple(range(first_target, last_target + 1))
        first_input = max(1, first_target - context_pages_before)
        last_input = min(page_count, last_target + context_pages_after)
        calls.append(
            OCRCall(
                target_page_numbers=targets,
                input_page_numbers=tuple(range(first_input, last_input + 1)),
            )
        )
    return tuple(calls)


class OCRResolver:
    """Resolve native WebDAV, then adopted WebDAV, then local resumable OCR."""

    def __init__(
        self,
        *,
        provider: OCRProvider,
        state: WorkerState,
        webdav: WebDAVClient | None,
        chunk_pages: int = 2,
        prompt: str = DEFAULT_OCR_PROMPT,
        cache_epoch: int = 0,
        render_scale_milli: int = 6000,
        adoption_policy_version: str = LEGACY_ADOPTION_POLICY_V2,
        prompt_version: str = "cardrag-ocr.ko.v2",
        whole_document_max_pages: int = 4,
        context_pages_before: int = 1,
        context_pages_after: int = 1,
        cache_mode: OCRCacheMode = "read-write",
    ) -> None:
        if chunk_pages < 1:
            raise ValueError("chunk_pages must be positive")
        if whole_document_max_pages < 1:
            raise ValueError("whole_document_max_pages must be positive")
        if context_pages_before < 0 or context_pages_after < 0:
            raise ValueError("OCR context page counts must be non-negative")
        if render_scale_milli < 1000 or render_scale_milli > 8000:
            raise ValueError("render_scale_milli must be between 1000 and 8000")
        if cache_mode not in {"read-only", "read-write"}:
            raise ValueError("cache_mode must be read-only or read-write")
        self.provider = provider
        self.state = state
        self.webdav = webdav
        self.chunk_pages = chunk_pages
        self.whole_document_max_pages = whole_document_max_pages
        self.context_pages_before = context_pages_before
        self.context_pages_after = context_pages_after
        self.prompt = prompt
        self.render_scale_milli = render_scale_milli
        self.adoption_policy_version = adoption_policy_version
        self._cache_mode = cache_mode
        self.contract = NativeOCRContract(
            processor_version=OCR_PROCESSOR_VERSION,
            cache_epoch=cache_epoch,
            prompt_version=prompt_version,
            prompt_sha256=sha256_bytes(prompt.encode()),
            renderer_id="pypdfium2/5.12.1",
            render_scale_milli=render_scale_milli,
            provider=provider.provider,
            model=provider.model,
            reasoning_effort=getattr(provider, "reasoning_effort", None),
            segmentation_strategy_id=OCR_SEGMENTATION_STRATEGY_ID,
            whole_document_max_pages=whole_document_max_pages,
            target_pages_per_call=chunk_pages,
            context_pages_before=context_pages_before,
            context_pages_after=context_pages_after,
            output_policy=OCR_OUTPUT_POLICY,
        )

    @property
    def cache_mode(self) -> OCRCacheMode:
        """Immutable remote-cache capability selected before provider work."""

        return self._cache_mode

    def _require_remote_cache_writable(self) -> None:
        if self.cache_mode != "read-write":
            raise OCRValidationError("remote OCR cache mutation is disabled in read-only mode")

    async def _lookup_cache(
        self,
        *,
        kind: Literal["native", "adopted"],
        reuse_key: str,
        source: OCRInput,
        source_document_id: str,
        output_dir: Path,
        adoption_policy_version: str | None = None,
    ) -> OCRResult | None:
        if self.webdav is None:
            return None
        ready_body = await self.webdav.get_bytes(ocr_ready_path(reuse_key, kind=kind))
        manifest_body = await self.webdav.get_bytes(ocr_manifest_path(reuse_key, kind=kind))
        if ready_body is None and manifest_body is None:
            return None
        if kind == "native" and ready_body is None and manifest_body is not None:
            if self.cache_mode == "read-only":
                raise OCRValidationError(
                    "native OCR cache is incomplete; READY repair is disabled in read-only mode"
                )
            return await self._repair_native_ready(
                manifest_body=manifest_body,
                reuse_key=reuse_key,
                source=source,
                output_dir=output_dir,
            )
        if ready_body is None or manifest_body is None:
            raise OCRValidationError(f"incomplete {kind} OCR cache entry")
        try:
            ready_raw = json.loads(ready_body)
            json.loads(manifest_body)
        except json.JSONDecodeError as exc:
            raise OCRValidationError(f"invalid {kind} OCR cache JSON") from exc
        if ready_raw.get("manifest_sha256") != hashlib.sha256(manifest_body).hexdigest():
            raise OCRValidationError(f"{kind} OCR READY does not match manifest")
        if kind == "native":
            try:
                ready = OCRReady.model_validate_json(ready_body)
                manifest = OCRArtifactManifest.model_validate_json(manifest_body)
            except Exception as exc:
                raise OCRValidationError("native OCR cache manifest is invalid") from exc
            if ready.canonical_bytes() != ready_body or manifest.canonical_bytes() != manifest_body:
                raise OCRValidationError("native OCR cache control JSON is not canonical")
            if ready.reuse_key != reuse_key or ready.ocr_sha256 != manifest.output.sha256:
                raise OCRValidationError("native OCR READY contract mismatch")
            if (
                manifest.reuse_key != reuse_key
                or manifest.source != source
                or manifest.contract != self.contract
            ):
                raise OCRValidationError("native OCR cache source/contract mismatch")
            artifact = manifest.output
            page_hashes = manifest.page_output_sha256
            char_count = manifest.ocr_chars
            provider = manifest.contract.provider
            model = manifest.contract.model
        else:
            try:
                ready = OCRReady.model_validate_json(ready_body)
                adopted = AdoptedOCRArtifactManifest.model_validate_json(manifest_body)
            except Exception as exc:
                raise OCRValidationError("adopted OCR cache manifest is invalid") from exc
            if ready.canonical_bytes() != ready_body or adopted.canonical_bytes() != manifest_body:
                raise OCRValidationError("adopted OCR cache control JSON is not canonical")
            if ready.reuse_key != reuse_key or ready.ocr_sha256 != adopted.output.sha256:
                raise OCRValidationError("adopted OCR READY contract mismatch")
            if (
                adopted.reuse_key != reuse_key
                or adopted.source != source
                or adopted.receipt.source_document_id != source_document_id
                or adopted.receipt.adoption_policy_version != adoption_policy_version
            ):
                raise OCRValidationError("adopted OCR cache source/receipt mismatch")
            artifact = adopted.output
            page_hashes = adopted.page_output_sha256
            char_count = adopted.ocr_chars
            provider = "legacy-adoption"
            model = adopted.receipt.source_database_id
        body = await self.webdav.get_bytes(artifact.path, max_bytes=artifact.size_bytes)
        if body is None or hashlib.sha256(body).hexdigest() != artifact.sha256:
            raise OCRValidationError("OCR cache artifact is missing or corrupt")
        reject_credential_bearing_ocr(body)
        try:
            verified = verify_ocr_bytes(
                body,
                expected_page_count=source.page_count,
                expected_sha256=artifact.sha256,
                expected_size_bytes=artifact.size_bytes,
                expected_char_count=char_count,
                expected_page_sha256=page_hashes,
            )
        except Exception as exc:
            raise OCRValidationError("OCR cache bytes failed strict verification") from exc
        if kind == "native":
            # Keep a local, canonical copy of the exact remote winner.  Besides
            # making ordinary cache hits restartable, this is the commit point
            # used when two isolated workers race to populate one immutable
            # reuse key with nondeterministic OCR output.
            atomic_write(output_dir / "ocr.md", body)
            atomic_write(output_dir / "native-manifest.json", manifest_body)
        return OCRResult(
            pages=tuple(_page_body(page) for page in verified.pages),
            ocr_bytes=body,
            ocr_text=verified.text,
            ocr_sha256=verified.sha256,
            size_bytes=verified.size_bytes,
            provenance=kind,
            provider=provider,
            model=model,
            reuse_key=reuse_key,
            cache_kind=kind,
            cache_reuse_key=reuse_key,
            cache_reused=True,
        )

    async def _repair_native_ready(
        self,
        *,
        manifest_body: bytes,
        reuse_key: str,
        source: OCRInput,
        output_dir: Path,
    ) -> OCRResult:
        """Repair the sole safe partial state: verified manifest+CAS, absent READY."""

        self._require_remote_cache_writable()
        assert self.webdav is not None
        try:
            manifest = OCRArtifactManifest.model_validate_json(manifest_body)
        except Exception as exc:
            raise OCRValidationError("partial native OCR manifest is invalid") from exc
        if manifest.canonical_bytes() != manifest_body:
            raise OCRValidationError("partial native OCR manifest is not canonical")
        if manifest.reuse_key != reuse_key or manifest.source != source or manifest.contract != self.contract:
            raise OCRValidationError("partial native OCR manifest contract mismatch")
        artifact = manifest.output
        body = await self.webdav.get_bytes(artifact.path, max_bytes=artifact.size_bytes)
        if body is None or hashlib.sha256(body).hexdigest() != artifact.sha256:
            raise OCRValidationError("partial native OCR artifact is missing or corrupt")
        reject_credential_bearing_ocr(body)
        try:
            verified = verify_ocr_bytes(
                body,
                expected_page_count=source.page_count,
                expected_sha256=artifact.sha256,
                expected_size_bytes=artifact.size_bytes,
                expected_char_count=manifest.ocr_chars,
                expected_page_sha256=manifest.page_output_sha256,
            )
        except Exception as exc:
            raise OCRValidationError("partial native OCR bytes failed strict verification") from exc
        atomic_write(output_dir / "ocr.md", body)
        atomic_write(output_dir / "native-manifest.json", manifest_body)
        result = OCRResult(
            pages=tuple(_page_body(page) for page in verified.pages),
            ocr_bytes=body,
            ocr_text=verified.text,
            ocr_sha256=verified.sha256,
            size_bytes=verified.size_bytes,
            provenance="native-partial",
            provider=manifest.contract.provider,
            model=manifest.contract.model,
            reuse_key=reuse_key,
            cache_reused=True,
        )
        try:
            await self._publish_native_ready(manifest=manifest)
        except OCRCachePublicationError as exc:
            if not exc.retryable:
                raise
            self._write_cache_publication_diagnostic(
                output_dir=output_dir,
                manifest=manifest,
                error=exc,
            )
            warnings.warn(
                "native OCR READY repair was deferred after bounded retries "
                f"(reason_code={exc.reason_code}, phase={exc.phase}); "
                "publishing generation-only OCR",
                RuntimeWarning,
                stacklevel=2,
            )
            return replace(
                result,
                cache_publication_deferred=True,
                cache_publication_reason_code=exc.reason_code,
            )
        self._cache_publication_diagnostic_path(output_dir).unlink(missing_ok=True)
        return OCRResult(
            pages=result.pages,
            ocr_bytes=result.ocr_bytes,
            ocr_text=result.ocr_text,
            ocr_sha256=result.ocr_sha256,
            size_bytes=result.size_bytes,
            provenance="native-repaired",
            provider=result.provider,
            model=result.model,
            reuse_key=result.reuse_key,
            cache_kind="native",
            cache_reuse_key=result.reuse_key,
            cache_reused=result.cache_reused,
            provider_called=result.provider_called,
        )

    def _load_local_native(
        self,
        *,
        output_dir: Path,
        source: OCRInput,
        reuse_key: str,
    ) -> tuple[OCRResult, OCRArtifactManifest, bytes] | None:
        ocr_path = output_dir / "ocr.md"
        manifest_path = output_dir / "native-manifest.json"
        if not manifest_path.exists():
            return None
        if (
            not ocr_path.is_file()
            or ocr_path.is_symlink()
            or not manifest_path.is_file()
            or manifest_path.is_symlink()
        ):
            raise OCRValidationError("local native OCR seal is incomplete or unsafe")
        try:
            manifest_body = manifest_path.read_bytes()
            manifest = OCRArtifactManifest.model_validate_json(manifest_body)
        except Exception as exc:
            raise OCRValidationError("local native OCR manifest is invalid") from exc
        if manifest.canonical_bytes() != manifest_body:
            raise OCRValidationError("local native OCR manifest is not canonical")
        if manifest.reuse_key != reuse_key or manifest.source != source or manifest.contract != self.contract:
            # A sealed artifact from an older processing contract is a clean
            # cache miss. Its checkpoints are independently input-hash bound.
            return None
        body = ocr_path.read_bytes()
        try:
            verified = verify_ocr_bytes(
                body,
                expected_page_count=source.page_count,
                expected_sha256=manifest.output.sha256,
                expected_size_bytes=manifest.output.size_bytes,
                expected_char_count=manifest.ocr_chars,
                expected_page_sha256=manifest.page_output_sha256,
            )
        except Exception as exc:
            raise OCRValidationError("local native OCR bytes failed strict verification") from exc
        return (
            OCRResult(
                pages=tuple(_page_body(page) for page in verified.pages),
                ocr_bytes=body,
                ocr_text=verified.text,
                ocr_sha256=verified.sha256,
                size_bytes=verified.size_bytes,
                provenance="native-local",
                provider=self.provider.provider,
                model=self.provider.model,
                reuse_key=reuse_key,
                cache_reused=True,
            ),
            manifest,
            body,
        )

    def _materialize_prior_local_native(
        self,
        *,
        prior: PriorLocalNativeSource,
        source: OCRInput,
        reuse_key: str,
        document_id: str,
        output_dir: Path,
    ) -> tuple[OCRResult, OCRArtifactManifest, bytes] | None:
        """Strictly verify and atomically seed a new run from a retained run."""

        if (
            prior.document_id != document_id
            or prior.pdf_sha256 != source.pdf_sha256
            or prior.pdf_size_bytes != source.pdf_size_bytes
            or prior.page_count != source.page_count
            or not re.fullmatch(r"[0-9a-f]{64}", prior.corpus_sha256)
            or not re.fullmatch(r"[0-9a-f]{64}", prior.contract_sha256)
            or not re.fullmatch(r"[0-9a-f]{64}", prior.ocr_sha256)
            or prior.ocr_size_bytes < 1
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", prior.run_id)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", prior.generation_id)
            or prior.resolver_subdir not in {None, "primary", "fallback"}
        ):
            raise OCRValidationError("prior local native OCR identity is invalid")

        run_root = prior.runs_root / prior.run_id
        document_root = run_root / "documents" / document_id
        outer_output_dir = document_root / "ocr"
        prior_output_dir = (
            outer_output_dir / prior.resolver_subdir
            if prior.resolver_subdir is not None
            else outer_output_dir
        )
        directory_chain = [
            prior.runs_root,
            run_root,
            run_root / "documents",
            document_root,
            outer_output_dir,
        ]
        if prior.resolver_subdir is not None:
            directory_chain.append(prior_output_dir)
        try:
            for directory in directory_chain:
                mode = directory.lstat().st_mode
                if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                    raise OCRValidationError("prior local native OCR directory is unsafe")
            resolved_runs_root = prior.runs_root.resolve(strict=True)
            resolved_run_root = run_root.resolve(strict=True)
            resolved_documents_root = (run_root / "documents").resolve(strict=True)
            resolved_document_root = document_root.resolve(strict=True)
            resolved_outer_output_dir = outer_output_dir.resolve(strict=True)
            resolved_output_dir = prior_output_dir.resolve(strict=True)
        except OCRValidationError:
            raise
        except (OSError, RuntimeError) as exc:
            raise OCRValidationError("prior local native OCR directory is unavailable") from exc
        if (
            resolved_run_root.parent != resolved_runs_root
            or resolved_documents_root.parent != resolved_run_root
            or resolved_document_root.parent != resolved_documents_root
            or resolved_outer_output_dir.parent != resolved_document_root
            or (prior.resolver_subdir is None and resolved_output_dir != resolved_outer_output_dir)
            or (prior.resolver_subdir is not None and resolved_output_dir.parent != resolved_outer_output_dir)
        ):
            raise OCRValidationError("prior local native OCR path escapes retained run storage")

        prior_ocr_path = prior_output_dir / "ocr.md"
        prior_manifest_path = prior_output_dir / "native-manifest.json"
        try:
            for path in (prior_ocr_path, prior_manifest_path):
                mode = path.lstat().st_mode
                if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                    raise OCRValidationError("prior local native OCR file is unsafe")
                if path.resolve(strict=True).parent != resolved_output_dir:
                    raise OCRValidationError("prior local native OCR file escapes its output directory")
        except OCRValidationError:
            raise
        except (OSError, RuntimeError) as exc:
            raise OCRValidationError("prior local native OCR file is unavailable") from exc

        local = self._load_local_native(
            output_dir=prior_output_dir,
            source=source,
            reuse_key=reuse_key,
        )
        if local is None:
            return None
        local_result, local_manifest, local_body = local
        if (
            local_result.ocr_sha256 != prior.ocr_sha256
            or local_result.size_bytes != prior.ocr_size_bytes
            or local_manifest.output.sha256 != prior.ocr_sha256
            or local_manifest.output.size_bytes != prior.ocr_size_bytes
        ):
            raise OCRValidationError("prior local native OCR differs from its generation seal")

        if output_dir != prior_output_dir:
            if output_dir.is_symlink() or (output_dir.exists() and not output_dir.is_dir()):
                raise OCRValidationError("current local OCR output directory is unsafe")
            # Write the body first and the canonical manifest last so the
            # manifest remains the local commit marker across process crashes.
            atomic_write(output_dir / "ocr.md", local_body)
            atomic_write(output_dir / "native-manifest.json", local_manifest.canonical_bytes())
            materialized = self._load_local_native(
                output_dir=output_dir,
                source=source,
                reuse_key=reuse_key,
            )
            if materialized is None:
                raise OCRValidationError("materialized prior local native OCR lost its contract")
            return materialized
        return local

    def matches_prior_local_native(
        self,
        *,
        prior: PriorLocalNativeSource,
        document_id: str,
        pdf_sha256: str,
        pdf_size_bytes: int,
        page_count: int,
    ) -> bool:
        """Return a strict, read-only match for failover branch routing."""

        try:
            source = OCRInput(
                pdf_sha256=pdf_sha256,
                pdf_size_bytes=pdf_size_bytes,
                page_count=page_count,
            )
            reuse_key = native_ocr_reuse_key(self.contract, source)
            run_root = prior.runs_root / prior.run_id / "documents" / document_id / "ocr"
            prior_output_dir = (
                run_root / prior.resolver_subdir if prior.resolver_subdir is not None else run_root
            )
            return (
                self._materialize_prior_local_native(
                    prior=prior,
                    source=source,
                    reuse_key=reuse_key,
                    document_id=document_id,
                    output_dir=prior_output_dir,
                )
                is not None
            )
        except (OCRValidationError, ValueError):
            return False

    @staticmethod
    def _cache_publication_diagnostic_path(output_dir: Path) -> Path:
        return output_dir / OCR_CACHE_PUBLICATION_DIAGNOSTIC

    def _write_cache_publication_diagnostic(
        self,
        *,
        output_dir: Path,
        manifest: OCRArtifactManifest,
        error: OCRCachePublicationError,
    ) -> None:
        payload = canonical_json_bytes(
            {
                "artifact_sha256": manifest.output.sha256,
                "attempts": error.attempts,
                "created_at": datetime.now(UTC),
                "error_kind": error.error_kind,
                "outcome": "generation-only",
                "phase": error.phase,
                "reason": error.reason,
                "reason_code": error.reason_code,
                "retryable": error.retryable,
                "reuse_key": manifest.reuse_key,
                "schema_version": "cardrag.ocr-cache-publication-diagnostic.v1",
                "status_code": error.status_code,
            }
        )
        if len(payload) > OCR_CACHE_PUBLICATION_DIAGNOSTIC_MAX_BYTES:
            raise OCRValidationError("OCR cache publication diagnostic exceeds its size bound")
        atomic_write(self._cache_publication_diagnostic_path(output_dir), payload)

    async def _commit_local_native(
        self,
        *,
        result: OCRResult,
        manifest: OCRArtifactManifest,
        body: bytes,
        output_dir: Path,
        source_document_id: str,
    ) -> OCRResult:
        if body != result.ocr_bytes:
            raise OCRValidationError("local native OCR body does not match its verified result")
        if self.webdav is None or self.cache_mode == "read-only":
            return result
        try:
            await self._publish_native_cache(manifest=manifest, body=body)
        except OCRCachePublicationError as exc:
            winner: OCRResult | None = None
            if exc.phase in {"manifest", "ready"} and exc.error_kind in {"contract", "integrity"}:
                # A create-once collision can be a legitimate concurrent
                # first-writer win.  Adopt it only after the normal native
                # READY -> manifest -> CAS -> page-hash validation succeeds.
                # Any incomplete, corrupt, or differently contracted entry
                # preserves the original fail-closed publication error.
                try:
                    winner = await self._lookup_cache(
                        kind="native",
                        reuse_key=manifest.reuse_key,
                        source=manifest.source,
                        source_document_id=source_document_id,
                        output_dir=output_dir,
                    )
                except Exception:
                    winner = None
            if winner is not None:
                self._cache_publication_diagnostic_path(output_dir).unlink(missing_ok=True)
                return replace(winner, provider_called=result.provider_called)
            if not exc.retryable:
                raise
            self._write_cache_publication_diagnostic(
                output_dir=output_dir,
                manifest=manifest,
                error=exc,
            )
            warnings.warn(
                "native OCR cache publication was deferred after bounded retries "
                f"(reason_code={exc.reason_code}, phase={exc.phase}); "
                "publishing generation-only OCR",
                RuntimeWarning,
                stacklevel=2,
            )
            return replace(
                result,
                cache_publication_deferred=True,
                cache_publication_reason_code=exc.reason_code,
            )
        self._cache_publication_diagnostic_path(output_dir).unlink(missing_ok=True)
        return OCRResult(
            pages=result.pages,
            ocr_bytes=result.ocr_bytes,
            ocr_text=result.ocr_text,
            ocr_sha256=result.ocr_sha256,
            size_bytes=result.size_bytes,
            provenance=result.provenance,
            provider=result.provider,
            model=result.model,
            reuse_key=result.reuse_key,
            cache_kind="native",
            cache_reuse_key=result.reuse_key,
            cache_reused=result.cache_reused,
            provider_called=result.provider_called,
        )

    async def resolve(
        self,
        *,
        run_id: str,
        document_id: str,
        pdf_path: Path,
        pdf_sha256: str,
        pdf_size_bytes: int,
        page_count: int,
        output_dir: Path,
        prior_local_native: PriorLocalNativeSource | None = None,
    ) -> OCRResult:
        source = OCRInput(
            pdf_sha256=pdf_sha256,
            pdf_size_bytes=pdf_size_bytes,
            page_count=page_count,
        )
        native_key = native_ocr_reuse_key(self.contract, source)
        adopted_policies = [self.adoption_policy_version]
        if LEGACY_ADOPTION_POLICY_V1 not in adopted_policies:
            adopted_policies.append(LEGACY_ADOPTION_POLICY_V1)
        cache_candidate_list: list[tuple[Literal["native", "adopted"], str, str | None]] = [
            ("native", native_key, None)
        ]
        for policy in adopted_policies:
            cache_candidate_list.append(
                (
                    "adopted",
                    adopted_ocr_reuse_key(
                        adoption_policy_version=policy,
                        source_document_id=document_id,
                        pdf_sha256=pdf_sha256,
                    ),
                    policy,
                )
            )
        cache_candidates = tuple(cache_candidate_list)
        for kind, lookup_key, candidate_policy in cache_candidates:
            try:
                found = await self._lookup_cache(
                    kind=kind,
                    reuse_key=lookup_key,
                    source=source,
                    source_document_id=document_id,
                    output_dir=output_dir,
                    adoption_policy_version=candidate_policy,
                )
            except OCRValidationError:
                warnings.warn(
                    f"ignoring invalid {kind} OCR cache entry; strict validation failed",
                    RuntimeWarning,
                    stacklevel=2,
                )
                found = None
            if found is not None:
                return found
        local = self._load_local_native(output_dir=output_dir, source=source, reuse_key=native_key)
        if local is None and prior_local_native is not None:
            try:
                local = self._materialize_prior_local_native(
                    prior=prior_local_native,
                    source=source,
                    reuse_key=native_key,
                    document_id=document_id,
                    output_dir=output_dir,
                )
            except OCRValidationError:
                # A retained local artifact is only an optimization.  Any
                # ambiguity or tampering is a strict miss: do not reuse its
                # bytes, and let the ordinary provider path rebuild the OCR.
                warnings.warn(
                    "ignoring invalid prior local native OCR source; strict validation failed",
                    RuntimeWarning,
                    stacklevel=2,
                )
                local = None
        if local is not None:
            local_result, local_manifest, local_body = local
            committed = await self._commit_local_native(
                result=local_result,
                manifest=local_manifest,
                body=local_body,
                output_dir=output_dir,
                source_document_id=document_id,
            )
            shutil.rmtree(output_dir / "rendered", ignore_errors=True)
            return committed
        images = render_pdf(
            pdf_path,
            output_dir / "rendered",
            scale=self.render_scale_milli / 1000,
        )
        if len(images) != page_count:
            raise OCRValidationError("rendered page count differs from validated PDF")
        calls = plan_ocr_calls(
            page_count=page_count,
            whole_document_max_pages=self.whole_document_max_pages,
            target_pages_per_call=self.chunk_pages,
            context_pages_before=self.context_pages_before,
            context_pages_after=self.context_pages_after,
        )
        image_hashes = {
            page_number: file_sha256(images[page_number - 1]) for page_number in range(1, page_count + 1)
        }
        page_text: dict[int, str] = {}
        cache_reused = False
        provider_called = False
        for chunk_index, call in enumerate(calls):
            selected = tuple(images[page_number - 1] for page_number in call.input_page_numbers)
            input_sha = canonical_sha256(
                {
                    "contract_sha256": self.contract.contract_sha256,
                    "input_images": [
                        {"page_number": page_number, "sha256": image_hashes[page_number]}
                        for page_number in call.input_page_numbers
                    ],
                    "model": self.provider.model,
                    "output_policy": OCR_OUTPUT_POLICY,
                    "provider": self.provider.provider,
                    "schema_version": "cardrag.ocr-checkpoint-input.v2",
                    "target_page_numbers": call.target_page_numbers,
                    "total_pages": page_count,
                }
            )
            checkpoint = self.state.checkpoint(run_id, document_id, "ocr", chunk_index)
            if checkpoint is not None and checkpoint["input_sha256"] == input_sha:
                checkpoint_path = Path(checkpoint["artifact_path"])
                if checkpoint_path.exists() and file_sha256(checkpoint_path) == checkpoint["output_sha256"]:
                    chunk = checkpoint_path.read_text(encoding="utf-8")
                    reject_credential_bearing_ocr(chunk)
                    try:
                        pages = split_ocr_pages(
                            chunk,
                            expected_count=len(call.target_page_numbers),
                            first_page=call.target_page_numbers[0],
                        )
                        pages = _validate_and_normalize_target_page_values(
                            call.target_page_numbers,
                            pages,
                        )
                    except OCRValidationError:
                        # A short or malformed provider response from an older
                        # failed attempt must not poison every retry. Re-run
                        # this exact call and atomically replace it below.
                        pass
                    else:
                        for page_number, value in zip(call.target_page_numbers, pages, strict=True):
                            page_text[page_number] = value
                        cache_reused = True
                        continue
            provider_called = True
            raw = await self.provider.recognize(
                selected,
                page_numbers=call.input_page_numbers,
                target_page_numbers=call.target_page_numbers,
                total_pages=page_count,
                prompt=self.prompt,
            )
            reject_credential_bearing_ocr(raw)
            # Context images may inform continuity but are never persisted as
            # output from this call. Exact target markers enforce that boundary.
            values = split_ocr_pages(
                raw,
                expected_count=len(call.target_page_numbers),
                first_page=call.target_page_numbers[0],
            )
            if any(not value for value in values):
                raise OCRValidationError("OCR provider returned an empty page")
            values = _validate_and_normalize_target_page_values(call.target_page_numbers, values)
            normalized = (
                "\n\n".join(
                    f"## Page {page_number}\n\n{value}"
                    for page_number, value in zip(call.target_page_numbers, values, strict=True)
                )
                + "\n"
            )
            chunk_path = output_dir / "checkpoints" / f"chunk-{chunk_index:04d}.md"
            atomic_write(chunk_path, normalized.encode())
            self.state.save_checkpoint(
                run_id=run_id,
                document_id=document_id,
                stage_name="ocr",
                chunk_index=chunk_index,
                input_sha256=input_sha,
                output_sha256=file_sha256(chunk_path),
                artifact_path=chunk_path,
            )
            for page_number, value in zip(call.target_page_numbers, values, strict=True):
                page_text[page_number] = value
        pages = tuple(page_text[index] for index in range(1, page_count + 1))
        combined = "\n\n".join(f"## Page {index}\n\n{value}" for index, value in enumerate(pages, 1)) + "\n"
        body = combined.encode()
        reject_credential_bearing_ocr(body)
        ocr_path = output_dir / "ocr.md"
        atomic_write(ocr_path, body)
        # Verify exactly the bytes that will be cached and referenced by generations.
        verified = verify_ocr_bytes(body, expected_page_count=page_count)
        result = OCRResult(
            pages=tuple(_page_body(page) for page in verified.pages),
            ocr_bytes=body,
            ocr_text=verified.text,
            ocr_sha256=verified.sha256,
            size_bytes=verified.size_bytes,
            provenance="native",
            provider=self.provider.provider,
            model=self.provider.model,
            reuse_key=native_key,
            cache_reused=cache_reused,
            provider_called=provider_called,
        )
        manifest = OCRArtifactManifest(
            reuse_key=result.reuse_key,
            source=source,
            contract=self.contract,
            output=ArtifactRef.for_cas(
                sha256=result.ocr_sha256,
                size_bytes=result.size_bytes,
                media_type="text/markdown; charset=utf-8",
            ),
            ocr_chars=verified.char_count,
            page_output_sha256=verified.page_sha256,
            created_at=datetime.now(UTC),
        )
        atomic_write(output_dir / "native-manifest.json", manifest.canonical_bytes())
        result = await self._commit_local_native(
            result=result,
            manifest=manifest,
            body=body,
            output_dir=output_dir,
            source_document_id=document_id,
        )
        shutil.rmtree(output_dir / "rendered", ignore_errors=True)
        return result

    async def _publish_native_cache(
        self,
        *,
        manifest: OCRArtifactManifest,
        body: bytes,
    ) -> None:
        self._require_remote_cache_writable()
        attempts = len(OCR_CACHE_PUBLICATION_RETRY_DELAYS_SECONDS) + 1
        for attempt in range(1, attempts + 1):
            try:
                await self._publish_native_cache_once(manifest=manifest, body=body)
            except OCRCachePublicationError as exc:
                error = _cache_publication_error(exc.phase, exc, attempts=attempt)
                if not error.retryable or attempt == attempts:
                    raise error from None
                await asyncio.sleep(OCR_CACHE_PUBLICATION_RETRY_DELAYS_SECONDS[attempt - 1])
            else:
                return
        raise AssertionError("bounded OCR cache publication loop did not terminate")

    async def _publish_native_ready(self, *, manifest: OCRArtifactManifest) -> None:
        self._require_remote_cache_writable()
        attempts = len(OCR_CACHE_PUBLICATION_RETRY_DELAYS_SECONDS) + 1
        for attempt in range(1, attempts + 1):
            try:
                await self._publish_native_ready_once(manifest=manifest)
            except OCRCachePublicationError as exc:
                error = _cache_publication_error("ready", exc, attempts=attempt)
                if not error.retryable or attempt == attempts:
                    raise error from None
                await asyncio.sleep(OCR_CACHE_PUBLICATION_RETRY_DELAYS_SECONDS[attempt - 1])
            else:
                return
        raise AssertionError("bounded OCR READY publication loop did not terminate")

    async def _publish_native_cache_once(
        self,
        *,
        manifest: OCRArtifactManifest,
        body: bytes,
    ) -> None:
        self._require_remote_cache_writable()
        assert self.webdav is not None
        try:
            artifact_sha, artifact_path = await self.webdav.put_cas(
                body,
                media_type="text/markdown; charset=utf-8",
            )
        except Exception as exc:
            raise _cache_publication_error("cas", exc) from None
        if artifact_sha != manifest.output.sha256 or artifact_path != manifest.output.path:
            raise OCRCachePublicationError(phase="cas", error_kind="contract") from None
        payload: Mapping[str, Any] = manifest.model_dump(mode="json")
        try:
            manifest_body = await self.webdav.put_json(
                ocr_manifest_path(manifest.reuse_key, kind="native"), payload, immutable=True
            )
        except Exception as exc:
            raise _cache_publication_error("manifest", exc) from None
        if manifest_body != manifest.canonical_bytes():
            raise OCRCachePublicationError(phase="manifest", error_kind="contract") from None
        await self._publish_native_ready_once(manifest=manifest)

    async def _publish_native_ready_once(self, *, manifest: OCRArtifactManifest) -> None:
        self._require_remote_cache_writable()
        assert self.webdav is not None
        manifest_body = manifest.canonical_bytes()
        ready = OCRReady(
            reuse_key=manifest.reuse_key,
            manifest_sha256=hashlib.sha256(manifest_body).hexdigest(),
            ocr_sha256=manifest.output.sha256,
        )
        try:
            ready_body = await self.webdav.put_json(
                ocr_ready_path(manifest.reuse_key, kind="native"),
                ready.model_dump(mode="json"),
                immutable=True,
            )
        except Exception as exc:
            raise _cache_publication_error("ready", exc) from None
        if ready_body != ready.canonical_bytes():
            raise OCRCachePublicationError(phase="ready", error_kind="contract") from None


def page_records(document_id: str, result: OCRResult) -> tuple[PageRecord, ...]:
    return tuple(
        PageRecord(document_id=document_id, page=index, text=text)
        for index, text in enumerate(result.pages, 1)
    )


class FailoverOCRResolver:
    """Whole-document, explicitly configured primary/fallback resolution."""

    def __init__(self, primary: OCRResolver, fallback: OCRResolver) -> None:
        if primary.adoption_policy_version != fallback.adoption_policy_version:
            raise ValueError("OCR resolvers must share one adoption policy")
        if primary.cache_mode != fallback.cache_mode:
            raise ValueError("OCR resolvers must share one cache mode")
        self.primary = primary
        self.fallback = fallback
        self.adoption_policy_version = primary.adoption_policy_version
        self.contract = {
            "schema_version": "cardrag.ocr-failover.v1",
            "primary": primary.contract,
            "fallback": fallback.contract,
        }

    @property
    def cache_mode(self) -> OCRCacheMode:
        """Return the shared child capability and reject any desynchronization."""

        if self.primary.cache_mode != self.fallback.cache_mode:
            raise ValueError("OCR resolvers no longer share one cache mode")
        return self.primary.cache_mode

    async def resolve(self, **kwargs: Any) -> OCRResult:
        output_dir = Path(kwargs.pop("output_dir"))
        raw_prior = kwargs.pop("prior_local_native", None)
        primary_prior: PriorLocalNativeSource | None = None
        fallback_prior: PriorLocalNativeSource | None = None
        if isinstance(raw_prior, PriorLocalNativeSource):
            primary_prior = replace(raw_prior, resolver_subdir="primary")
            fallback_prior = replace(raw_prior, resolver_subdir="fallback")
            document_id = str(kwargs["document_id"])
            pdf_sha256 = str(kwargs["pdf_sha256"])
            pdf_size_bytes = int(kwargs["pdf_size_bytes"])
            page_count = int(kwargs["page_count"])
            primary_matches = self.primary.matches_prior_local_native(
                prior=primary_prior,
                document_id=document_id,
                pdf_sha256=pdf_sha256,
                pdf_size_bytes=pdf_size_bytes,
                page_count=page_count,
            )
            fallback_matches = self.fallback.matches_prior_local_native(
                prior=fallback_prior,
                document_id=document_id,
                pdf_sha256=pdf_sha256,
                pdf_size_bytes=pdf_size_bytes,
                page_count=page_count,
            )
            if primary_matches != fallback_matches:
                if primary_matches:
                    return await self.primary.resolve(
                        **kwargs,
                        output_dir=output_dir / "primary",
                        prior_local_native=primary_prior,
                    )
                result = await self.fallback.resolve(
                    **kwargs,
                    output_dir=output_dir / "fallback",
                    prior_local_native=fallback_prior,
                )
                shutil.rmtree(output_dir / "primary" / "rendered", ignore_errors=True)
                return result
            # Zero matches is unavailable/invalid; two matches cannot prove
            # which resolver produced the sealed generation OCR.  Both are a
            # strict miss, so neither sibling may consume retained bytes.
            primary_prior = None
            fallback_prior = None
        try:
            return await self.primary.resolve(
                **kwargs,
                output_dir=output_dir / "primary",
                prior_local_native=primary_prior,
            )
        except (
            ProviderDocumentError,
            OCRValidationError,
            httpx.TimeoutException,
            TimeoutError,
        ):
            pass
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if status_code not in {408, 413, 422, 425, 429} and not 500 <= status_code <= 599:
                raise
        # Warning emission and fallback execution stay outside the primary
        # exception handler so cancellation cannot retain provider stderr as
        # implicit exception context.
        warnings.warn(
            "primary OCR failed; starting fresh whole-document fallback",
            RuntimeWarning,
            stacklevel=2,
        )
        result = await self.fallback.resolve(
            **kwargs,
            output_dir=output_dir / "fallback",
            prior_local_native=fallback_prior,
        )
        shutil.rmtree(output_dir / "primary" / "rendered", ignore_errors=True)
        return result
