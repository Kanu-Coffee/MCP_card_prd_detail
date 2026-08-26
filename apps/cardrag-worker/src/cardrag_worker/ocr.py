"""OCR cache priority and restartable local page-chunk processing."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import warnings
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx
import pypdfium2 as pdfium  # type: ignore[import-untyped]
from cardrag_core import OCRCacheKind
from cardrag_core.canonical import canonical_sha256, sha256_bytes
from cardrag_core.domain import ArtifactRef
from cardrag_core.manifests import (
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
from .providers import DEFAULT_OCR_PROMPT, OCRProvider, ProviderError
from .state import WorkerState
from .webdav import WebDAVClient

PAGE_MARKER = re.compile(r"^## Page (\d+)\s*$", re.MULTILINE)


class OCRValidationError(RuntimeError):
    pass


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


def render_pdf(pdf_path: Path, output_dir: Path, *, scale: float = 3.0) -> tuple[Path, ...]:
    output_dir.mkdir(parents=True, exist_ok=True)
    document = pdfium.PdfDocument(str(pdf_path))
    paths: list[Path] = []
    try:
        if len(document) < 1:
            raise OCRValidationError("OCR input has no pages")
        for index in range(len(document)):
            destination = output_dir / f"page-{index + 1:04d}.png"
            if not destination.exists():
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


@dataclass(frozen=True, slots=True)
class OCRResult:
    pages: tuple[str, ...]
    ocr_sha256: str
    size_bytes: int
    provenance: str
    provider: str
    model: str
    reuse_key: str
    cache_kind: OCRCacheKind | None = None
    cache_reuse_key: str | None = None

    def __post_init__(self) -> None:
        if (self.cache_kind is None) != (self.cache_reuse_key is None):
            raise ValueError("OCR cache kind and reuse key must be present together")


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
        render_scale_milli: int = 3000,
        adoption_policy_version: str = "cardrag.legacy-ocr-adoption.v1",
        prompt_version: str = "cardrag-ocr.ko.v1",
    ) -> None:
        if chunk_pages < 1:
            raise ValueError("chunk_pages must be positive")
        self.provider = provider
        self.state = state
        self.webdav = webdav
        self.chunk_pages = chunk_pages
        self.prompt = prompt
        self.render_scale_milli = render_scale_milli
        self.adoption_policy_version = adoption_policy_version
        self.contract = NativeOCRContract(
            # Adapter-only patch releases keep the OCR processing contract stable
            # so verified native cache entries remain reusable.
            processor_version="cardrag-worker/1.0.0",
            cache_epoch=cache_epoch,
            prompt_version=prompt_version,
            prompt_sha256=sha256_bytes(prompt.encode()),
            renderer_id="pypdfium2/5.12.1",
            render_scale_milli=render_scale_milli,
            provider=provider.provider,
            model=provider.model,
            reasoning_effort=getattr(provider, "reasoning_effort", None),
            chunk_pages=chunk_pages,
        )

    async def _lookup_cache(
        self,
        *,
        kind: Literal["native", "adopted"],
        reuse_key: str,
        source: OCRInput,
        source_document_id: str,
    ) -> OCRResult | None:
        if self.webdav is None:
            return None
        ready_body = await self.webdav.get_bytes(ocr_ready_path(reuse_key, kind=kind))
        manifest_body = await self.webdav.get_bytes(ocr_manifest_path(reuse_key, kind=kind))
        if ready_body is None and manifest_body is None:
            return None
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
                or adopted.receipt.adoption_policy_version != self.adoption_policy_version
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
        return OCRResult(
            pages=tuple(_page_body(page) for page in verified.pages),
            ocr_sha256=verified.sha256,
            size_bytes=verified.size_bytes,
            provenance=kind,
            provider=provider,
            model=model,
            reuse_key=reuse_key,
            cache_kind=kind,
            cache_reuse_key=reuse_key,
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
            manifest = OCRArtifactManifest.model_validate_json(manifest_path.read_bytes())
        except Exception as exc:
            raise OCRValidationError("local native OCR manifest is invalid") from exc
        if manifest.reuse_key != reuse_key or manifest.source != source or manifest.contract != self.contract:
            raise OCRValidationError("local native OCR manifest source/contract mismatch")
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
                ocr_sha256=verified.sha256,
                size_bytes=verified.size_bytes,
                provenance="native-local",
                provider=self.provider.provider,
                model=self.provider.model,
                reuse_key=reuse_key,
            ),
            manifest,
            body,
        )

    async def _commit_local_native(
        self,
        *,
        result: OCRResult,
        manifest: OCRArtifactManifest,
        body: bytes,
        native_cache_invalid: bool,
    ) -> OCRResult:
        if self.webdav is None:
            return result
        try:
            await self._publish_native_cache(manifest=manifest, body=body)
        except Exception as exc:
            if native_cache_invalid:
                # An immutable cache key cannot repair conflicting remote control
                # bytes. Treat the corrupt entry as the cache miss it already was:
                # keep the verified local seal and let the generation reference
                # the OCR CAS object without claiming a cache control binding.
                warnings.warn(
                    "native OCR was produced and locally sealed, but its corrupt immutable "
                    f"cache entry could not be replaced ({exc}); publishing generation-only OCR",
                    RuntimeWarning,
                    stacklevel=2,
                )
                return result
            raise
        return OCRResult(
            pages=result.pages,
            ocr_sha256=result.ocr_sha256,
            size_bytes=result.size_bytes,
            provenance=result.provenance,
            provider=result.provider,
            model=result.model,
            reuse_key=result.reuse_key,
            cache_kind="native",
            cache_reuse_key=result.reuse_key,
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
    ) -> OCRResult:
        source = OCRInput(
            pdf_sha256=pdf_sha256,
            pdf_size_bytes=pdf_size_bytes,
            page_count=page_count,
        )
        native_key = native_ocr_reuse_key(self.contract, source)
        adopted_key = adopted_ocr_reuse_key(
            adoption_policy_version=self.adoption_policy_version,
            source_document_id=document_id,
            pdf_sha256=pdf_sha256,
        )
        native_cache_invalid = False
        cache_candidates: tuple[tuple[Literal["native", "adopted"], str], ...] = (
            ("native", native_key),
            ("adopted", adopted_key),
        )
        for kind, lookup_key in cache_candidates:
            try:
                found = await self._lookup_cache(
                    kind=kind,
                    reuse_key=lookup_key,
                    source=source,
                    source_document_id=document_id,
                )
            except OCRValidationError as exc:
                if kind == "native":
                    native_cache_invalid = True
                warnings.warn(
                    f"ignoring invalid {kind} OCR cache entry {lookup_key}: {exc}",
                    RuntimeWarning,
                    stacklevel=2,
                )
                found = None
            if found is not None:
                return found
        local = self._load_local_native(output_dir=output_dir, source=source, reuse_key=native_key)
        if local is not None:
            local_result, local_manifest, local_body = local
            committed = await self._commit_local_native(
                result=local_result,
                manifest=local_manifest,
                body=local_body,
                native_cache_invalid=native_cache_invalid,
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
        page_text: dict[int, str] = {}
        for chunk_index, start in enumerate(range(0, page_count, self.chunk_pages)):
            selected = images[start : start + self.chunk_pages]
            input_sha = canonical_sha256(
                {
                    "contract_sha256": self.contract.contract_sha256,
                    "image_sha256": [file_sha256(path) for path in selected],
                    "model": self.provider.model,
                    "provider": self.provider.provider,
                    "start_page": start + 1,
                }
            )
            checkpoint = self.state.checkpoint(run_id, document_id, "ocr", chunk_index)
            if checkpoint is not None and checkpoint["input_sha256"] == input_sha:
                checkpoint_path = Path(checkpoint["artifact_path"])
                if checkpoint_path.exists() and file_sha256(checkpoint_path) == checkpoint["output_sha256"]:
                    chunk = checkpoint_path.read_text(encoding="utf-8")
                    pages = split_ocr_pages(chunk, expected_count=len(selected), first_page=start + 1)
                    for offset, value in enumerate(pages):
                        page_text[start + offset + 1] = value
                    continue
            raw = await self.provider.recognize(selected, first_page=start + 1, prompt=self.prompt)
            # Normalize chunk-local absolute markers to a strict persisted form.
            matches = list(PAGE_MARKER.finditer(raw))
            numbers = [int(match.group(1)) for match in matches]
            expected = list(range(start + 1, start + len(selected) + 1))
            if numbers != expected:
                raise OCRValidationError(f"OCR chunk page markers {numbers} != {expected}")
            values = tuple(
                raw[
                    match.end() : matches[index + 1].start() if index + 1 < len(matches) else len(raw)
                ].strip()
                for index, match in enumerate(matches)
            )
            if any(not value for value in values):
                raise OCRValidationError("OCR provider returned an empty page")
            normalized = (
                "\n\n".join(f"## Page {start + index + 1}\n\n{value}" for index, value in enumerate(values))
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
            for offset, value in enumerate(values):
                page_text[start + offset + 1] = value
        pages = tuple(page_text[index] for index in range(1, page_count + 1))
        combined = "\n\n".join(f"## Page {index}\n\n{value}" for index, value in enumerate(pages, 1)) + "\n"
        body = combined.encode()
        ocr_path = output_dir / "ocr.md"
        atomic_write(ocr_path, body)
        result = OCRResult(
            pages=pages,
            ocr_sha256=hashlib.sha256(body).hexdigest(),
            size_bytes=len(body),
            provenance="native",
            provider=self.provider.provider,
            model=self.provider.model,
            reuse_key=native_key,
        )
        # Verify exactly the bytes that will be cached and referenced by generations.
        verified = verify_ocr_bytes(body, expected_page_count=page_count)
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
            native_cache_invalid=native_cache_invalid,
        )
        shutil.rmtree(output_dir / "rendered", ignore_errors=True)
        return result

    async def _publish_native_cache(
        self,
        *,
        manifest: OCRArtifactManifest,
        body: bytes,
    ) -> None:
        assert self.webdav is not None
        artifact_sha, _ = await self.webdav.put_cas(body, media_type="text/markdown; charset=utf-8")
        if artifact_sha != manifest.output.sha256:
            raise OCRValidationError("native OCR changed between local seal and CAS publication")
        payload: Mapping[str, Any] = manifest.model_dump(mode="json")
        manifest_body = await self.webdav.put_json(
            ocr_manifest_path(manifest.reuse_key, kind="native"), payload, immutable=True
        )
        await self.webdav.put_json(
            ocr_ready_path(manifest.reuse_key, kind="native"),
            OCRReady(
                reuse_key=manifest.reuse_key,
                manifest_sha256=hashlib.sha256(manifest_body).hexdigest(),
                ocr_sha256=artifact_sha,
            ).model_dump(mode="json"),
            immutable=True,
        )


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
        self.primary = primary
        self.fallback = fallback
        self.adoption_policy_version = primary.adoption_policy_version
        self.contract = {
            "schema_version": "cardrag.ocr-failover.v1",
            "primary": primary.contract,
            "fallback": fallback.contract,
        }

    async def resolve(self, **kwargs: Any) -> OCRResult:
        output_dir = Path(kwargs.pop("output_dir"))
        try:
            return await self.primary.resolve(**kwargs, output_dir=output_dir / "primary")
        except (ProviderError, OCRValidationError, httpx.HTTPError, TimeoutError) as exc:
            warnings.warn(
                f"primary OCR failed; starting fresh whole-document fallback: {exc}",
                RuntimeWarning,
                stacklevel=2,
            )
            result = await self.fallback.resolve(**kwargs, output_dir=output_dir / "fallback")
            shutil.rmtree(output_dir / "primary" / "rendered", ignore_errors=True)
            return result
