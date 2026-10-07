"""Read-only M0 inventory of legacy OCR controls and current served text."""

from __future__ import annotations

import asyncio
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath

from cardrag_core import (
    STABLE_POINTER_PATH,
    AdoptedOCRArtifactManifest,
    ArtifactRef,
    OCRArtifactManifest,
    OCRInput,
    OCRReady,
    WebDAVHTTPError,
    content_addressed_ocr_reuse_key,
    ocr_manifest_path,
    ocr_ready_path,
    verify_ocr_bytes,
)

from .gc import _generation_chain
from .providers import ProviderSystemicError, reject_credential_bearing_ocr
from .webdav import CONTROL_OBJECT_MAX_BYTES, WebDAVClient

_HEX_PREFIX = re.compile(r"^[0-9a-f]{2}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ContentInventoryError(RuntimeError):
    """Remote inventory could not establish a complete read-only view."""


@dataclass(frozen=True, slots=True)
class LegacyOCRCandidate:
    kind: str
    reuse_key: str
    source: OCRInput
    output: ArtifactRef
    created_at: datetime
    manifest_sha256: str
    provider: str
    manifest: OCRArtifactManifest | AdoptedOCRArtifactManifest


@dataclass(frozen=True, slots=True)
class InventoryExclusion:
    kind: str
    reuse_key: str
    reason: str


@dataclass(frozen=True, slots=True)
class ContentInventory:
    stable_generation_id: str
    legacy_found: int
    legacy_valid: int
    by_kind: dict[str, int]
    by_provider: dict[str, int]
    multi_variant_pdfs: int
    stable_ocr_documents: int
    stable_generation_only: int
    stable_latest_would_change_text: int
    stable_unverified: int
    exclusions: tuple[InventoryExclusion, ...]

    def summary(self) -> dict[str, object]:
        return {
            "stable_generation_id": self.stable_generation_id,
            "legacy_found": self.legacy_found,
            "legacy_valid": self.legacy_valid,
            "by_kind": self.by_kind,
            "by_provider": self.by_provider,
            "multi_variant_pdfs": self.multi_variant_pdfs,
            "stable_ocr_documents": self.stable_ocr_documents,
            "stable_generation_only": self.stable_generation_only,
            "stable_latest_would_change_text": self.stable_latest_would_change_text,
            "stable_unverified": self.stable_unverified,
            "exclusion_count": len(self.exclusions),
            "exclusions": [
                {"kind": row.kind, "reuse_key": row.reuse_key, "reason": row.reason}
                for row in self.exclusions
            ],
            "read_only": True,
            "migration_applied": False,
        }


async def _optional_children(webdav: WebDAVClient, path: PurePosixPath) -> tuple[PurePosixPath, ...]:
    try:
        return await webdav.list_children(path)
    except WebDAVHTTPError as exc:
        if exc.status_code == 404:
            return ()
        raise


async def _legacy_roots(webdav: WebDAVClient) -> tuple[tuple[str, str], ...]:
    found: list[tuple[str, str]] = []
    semaphore = asyncio.Semaphore(16)

    async def list_prefix(kind: str, prefix: PurePosixPath) -> tuple[tuple[str, str], ...]:
        async with semaphore:
            roots = await webdav.list_children(prefix)
        if len(roots) != len(set(roots)):
            raise ContentInventoryError("duplicate legacy OCR reuse directory")
        rows: list[tuple[str, str]] = []
        for root in roots:
            key = root.name
            if root.parent != prefix or _SHA256.fullmatch(key) is None or key[:2] != prefix.name:
                raise ContentInventoryError("unsafe legacy OCR reuse directory")
            rows.append((kind, key))
        return tuple(rows)

    for kind in ("native", "adopted"):
        kind_root = PurePosixPath("v1/ocr-cache") / kind
        prefixes = await _optional_children(webdav, kind_root)
        if len(prefixes) != len(set(prefixes)):
            raise ContentInventoryError("duplicate legacy OCR prefix")
        for prefix in prefixes:
            if prefix.parent != kind_root or _HEX_PREFIX.fullmatch(prefix.name) is None:
                raise ContentInventoryError("unsafe legacy OCR prefix")
        groups = await asyncio.gather(*(list_prefix(kind, prefix) for prefix in prefixes))
        for group in groups:
            found.extend(group)
    return tuple(found)


async def _inspect_legacy(
    webdav: WebDAVClient, *, kind: str, key: str
) -> LegacyOCRCandidate | InventoryExclusion:
    manifest_path = ocr_manifest_path(key, kind=kind)
    ready_path = ocr_ready_path(key, kind=kind)
    manifest_body = await webdav.get_bytes(manifest_path, max_bytes=CONTROL_OBJECT_MAX_BYTES)
    ready_body = await webdav.get_bytes(ready_path, max_bytes=CONTROL_OBJECT_MAX_BYTES)
    if manifest_body is None or ready_body is None:
        return InventoryExclusion(kind, key, "control_missing")
    try:
        manifest = (
            OCRArtifactManifest.model_validate_json(manifest_body)
            if kind == "native"
            else AdoptedOCRArtifactManifest.model_validate_json(manifest_body)
        )
        ready = OCRReady.model_validate_json(ready_body)
    except ValueError:
        return InventoryExclusion(kind, key, "control_invalid")
    if (
        manifest.canonical_bytes() != manifest_body
        or ready.canonical_bytes() != ready_body
        or manifest.reuse_key != key
        or ready.reuse_key != key
        or ready.manifest_sha256 != manifest.manifest_sha256
        or ready.ocr_sha256 != manifest.output.sha256
    ):
        return InventoryExclusion(kind, key, "control_binding_invalid")
    body = await webdav.get_bytes(manifest.output.path, max_bytes=manifest.output.size_bytes)
    if body is None:
        return InventoryExclusion(kind, key, "ocr_object_missing")
    try:
        verify_ocr_bytes(
            body,
            expected_page_count=manifest.source.page_count,
            expected_sha256=manifest.output.sha256,
            expected_size_bytes=manifest.output.size_bytes,
            expected_char_count=manifest.ocr_chars,
            expected_page_sha256=manifest.page_output_sha256,
        )
        reject_credential_bearing_ocr(body)
    except ProviderSystemicError:
        return InventoryExclusion(kind, key, "ocr_credential_pattern")
    except ValueError:
        return InventoryExclusion(kind, key, "ocr_object_invalid")
    return LegacyOCRCandidate(
        kind=kind,
        reuse_key=key,
        source=manifest.source,
        output=manifest.output,
        created_at=manifest.created_at,
        manifest_sha256=manifest.manifest_sha256,
        provider=manifest.contract.provider
        if isinstance(manifest, OCRArtifactManifest)
        else "legacy-adoption",
        manifest=manifest,
    )


async def inspect_all_legacy(
    webdav: WebDAVClient,
) -> tuple[tuple[LegacyOCRCandidate, ...], tuple[InventoryExclusion, ...], int]:
    roots = await _legacy_roots(webdav)
    semaphore = asyncio.Semaphore(16)

    async def inspect(kind: str, key: str) -> LegacyOCRCandidate | InventoryExclusion:
        async with semaphore:
            return await _inspect_legacy(webdav, kind=kind, key=key)

    inspected = await asyncio.gather(*(inspect(kind, key) for kind, key in roots))
    valid = tuple(row for row in inspected if isinstance(row, LegacyOCRCandidate))
    exclusions = tuple(row for row in inspected if isinstance(row, InventoryExclusion))
    return valid, exclusions, len(roots)


async def inventory_content_migration(webdav: WebDAVClient) -> ContentInventory:
    """Inspect controls and OCR bytes without writing local or remote state."""

    valid, exclusions, found_count = await inspect_all_legacy(webdav)
    _pointer, (stable,) = await _generation_chain(webdav, retain=1, pointer_path=STABLE_POINTER_PATH)
    by_source: dict[str, list[LegacyOCRCandidate]] = defaultdict(list)
    for row in valid:
        by_source[content_addressed_ocr_reuse_key(row.source)].append(row)
    stable_ocr_documents = 0
    stable_generation_only = 0
    stable_latest_would_change_text = 0
    stable_sem = asyncio.Semaphore(16)
    stable_verifications: list[asyncio.Task[bool]] = []

    async def verify_served_ocr(path: str, size: int, sha256: str, pages: int) -> bool:
        async with stable_sem:
            body = await webdav.get_bytes(path, max_bytes=size)
        if body is None:
            return False
        try:
            verify_ocr_bytes(
                body,
                expected_page_count=pages,
                expected_sha256=sha256,
                expected_size_bytes=size,
            )
            reject_credential_bearing_ocr(body)
        except ProviderSystemicError:
            return False
        except ValueError:
            return False
        return True

    for document in stable.documents:
        if document.ocr is None:
            continue
        stable_ocr_documents += 1
        source = OCRInput(
            pdf_sha256=document.pdf.sha256,
            pdf_size_bytes=document.pdf.size_bytes,
            page_count=document.page_count,
        )
        rows = by_source.get(content_addressed_ocr_reuse_key(source), [])
        matching = [
            row
            for row in rows
            if (row.output.sha256, row.output.size_bytes) == (document.ocr.sha256, document.ocr.size_bytes)
        ]
        if not matching:
            stable_generation_only += 1
        if rows:
            latest = max(rows, key=lambda row: (row.created_at, row.manifest_sha256))
            if latest.output.sha256 != document.ocr.sha256:
                stable_latest_would_change_text += 1
        if not matching:
            stable_verifications.append(
                asyncio.create_task(
                    verify_served_ocr(
                        document.ocr.path,
                        document.ocr.size_bytes,
                        document.ocr.sha256,
                        document.page_count,
                    )
                )
            )
    stable_unverified = sum(not valid for valid in await asyncio.gather(*stable_verifications))
    return ContentInventory(
        stable_generation_id=stable.generation_id,
        legacy_found=found_count,
        legacy_valid=len(valid),
        by_kind=dict(sorted(Counter(row.kind for row in valid).items())),
        by_provider=dict(sorted(Counter(row.provider for row in valid).items())),
        multi_variant_pdfs=sum(len(rows) > 1 for rows in by_source.values()),
        stable_ocr_documents=stable_ocr_documents,
        stable_generation_only=stable_generation_only,
        stable_latest_would_change_text=stable_latest_would_change_text,
        stable_unverified=stable_unverified,
        exclusions=exclusions,
    )
