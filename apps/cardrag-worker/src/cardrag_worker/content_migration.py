"""Read-only construction of a lossless content OCR migration plan."""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from cardrag_core import (
    STABLE_POINTER_PATH,
    ArtifactRef,
    ContentOCRArtifactManifest,
    ContentOCRImportedProvenance,
    ContentOCRMigrationSource,
    GenerationPointer,
    OCRArtifactManifest,
    OCRInput,
    content_addressed_ocr_reuse_key,
    verify_ocr_bytes,
)

from .content_cache import ContentOCRVariantStore
from .content_inventory import ContentInventoryError, inspect_all_legacy
from .gc import _generation_chain
from .providers import ProviderSystemicError, reject_credential_bearing_ocr
from .webdav import WebDAVClient


@dataclass(frozen=True, slots=True)
class ContentMigrationPlan:
    stable_generation_id: str
    variants: tuple[ContentOCRArtifactManifest, ...]
    stable_document_variant_ids: dict[str, str]
    legacy_variant_count: int
    generation_only_variant_count: int
    conflicting_pdf_keys: int

    def summary(self) -> dict[str, object]:
        return {
            "stable_generation_id": self.stable_generation_id,
            "legacy_variants": self.legacy_variant_count,
            "generation_only_variants": self.generation_only_variant_count,
            "total_variants": len(self.variants),
            "stable_documents_pinned": len(self.stable_document_variant_ids),
            "conflicting_pdf_keys": self.conflicting_pdf_keys,
            "read_only": True,
            "migration_applied": False,
        }


async def plan_content_migration(webdav: WebDAVClient, *, cache_epoch: int = 0) -> ContentMigrationPlan:
    """Verify every source and map each served document to its exact OCR text."""

    legacy, exclusions, _found = await inspect_all_legacy(webdav)
    if exclusions:
        raise ContentInventoryError("migration plan requires every legacy OCR artifact to validate")
    _pointer, (stable,) = await _generation_chain(webdav, retain=1, pointer_path=STABLE_POINTER_PATH)
    variants: list[ContentOCRArtifactManifest] = []
    by_source_output: dict[tuple[str, str, int], list[ContentOCRArtifactManifest]] = defaultdict(list)
    for row in legacy:
        original = row.manifest
        provenance = (
            original.contract
            if isinstance(original, OCRArtifactManifest)
            else ContentOCRImportedProvenance(
                source_kind="adopted",
                provider="legacy-adoption",
                source_manifest_sha256=original.manifest_sha256,
            )
        )
        migrated = ContentOCRArtifactManifest.create(
            source=original.source,
            cache_epoch=cache_epoch,
            output=original.output,
            ocr_chars=original.ocr_chars,
            page_output_sha256=original.page_output_sha256,
            created_at=original.created_at,
            provenance=provenance,
            migrated_from=ContentOCRMigrationSource(kind=row.kind, reuse_key=row.reuse_key),  # type: ignore[arg-type]
        )
        variants.append(migrated)
        by_source_output[(migrated.reuse_key, migrated.output.sha256, migrated.output.size_bytes)].append(
            migrated
        )

    source_outputs: dict[str, Counter[tuple[str, int]]] = defaultdict(Counter)
    missing_by_identity: dict[tuple[str, str, int], tuple[str, OCRInput, ArtifactRef]] = {}
    for document in stable.documents:
        if document.ocr is None:
            continue
        source = OCRInput(
            pdf_sha256=document.pdf.sha256,
            pdf_size_bytes=document.pdf.size_bytes,
            page_count=document.page_count,
        )
        key = content_addressed_ocr_reuse_key(source, cache_epoch=cache_epoch)
        identity = (key, document.ocr.sha256, document.ocr.size_bytes)
        source_outputs[key][(document.ocr.sha256, document.ocr.size_bytes)] += 1
        if identity not in by_source_output:
            previous = missing_by_identity.get(identity)
            if previous is None or document.document_id < previous[0]:
                missing_by_identity[identity] = (document.document_id, source, document.ocr)

    semaphore = asyncio.Semaphore(16)

    async def import_generation_only(
        identity: tuple[str, str, int], document_id: str, source: OCRInput, output: ArtifactRef
    ) -> ContentOCRArtifactManifest:
        async with semaphore:
            body = await webdav.get_bytes(output.path, max_bytes=output.size_bytes)
        if body is None:
            raise ContentInventoryError("stable generation OCR CAS is missing")
        try:
            verified = verify_ocr_bytes(
                body,
                expected_page_count=source.page_count,
                expected_sha256=output.sha256,
                expected_size_bytes=output.size_bytes,
            )
            reject_credential_bearing_ocr(body)
        except ProviderSystemicError as exc:
            raise ContentInventoryError("stable generation OCR resembles a credential") from exc
        except ValueError as exc:
            raise ContentInventoryError("stable generation OCR CAS is invalid") from exc
        manifest = ContentOCRArtifactManifest.create(
            source=source,
            cache_epoch=cache_epoch,
            output=output,
            ocr_chars=verified.char_count,
            page_output_sha256=verified.page_sha256,
            created_at=stable.created_at,
            provenance=ContentOCRImportedProvenance(
                source_kind="generation-only",
                provider="generation-only",
                generation_id=stable.generation_id,
                document_id=document_id,
            ),
        )
        if identity != (manifest.reuse_key, manifest.output.sha256, manifest.output.size_bytes):
            raise ContentInventoryError("generation-only content identity changed while planning")
        return manifest

    generated = await asyncio.gather(
        *(
            import_generation_only(identity, document_id, source, output)
            for identity, (document_id, source, output) in sorted(missing_by_identity.items())
        )
    )
    for manifest in generated:
        variants.append(manifest)
        by_source_output[(manifest.reuse_key, manifest.output.sha256, manifest.output.size_bytes)].append(
            manifest
        )

    stable_document_variant_ids: dict[str, str] = {}
    for document in stable.documents:
        if document.ocr is None:
            continue
        source = OCRInput(
            pdf_sha256=document.pdf.sha256,
            pdf_size_bytes=document.pdf.size_bytes,
            page_count=document.page_count,
        )
        identity = (
            content_addressed_ocr_reuse_key(source, cache_epoch=cache_epoch),
            document.ocr.sha256,
            document.ocr.size_bytes,
        )
        choices = by_source_output.get(identity)
        if not choices:
            raise ContentInventoryError("stable document has no migration variant")
        selected = max(choices, key=lambda item: (item.created_at, item.manifest_sha256))
        stable_document_variant_ids[document.document_id] = selected.variant_id
    return ContentMigrationPlan(
        stable_generation_id=stable.generation_id,
        variants=tuple(sorted(variants, key=lambda item: (item.reuse_key, item.variant_label))),
        stable_document_variant_ids=stable_document_variant_ids,
        legacy_variant_count=len(legacy),
        generation_only_variant_count=len(generated),
        conflicting_pdf_keys=sum(len(outputs) > 1 for outputs in source_outputs.values()),
    )


async def apply_content_migration(
    webdav: WebDAVClient,
    plan: ContentMigrationPlan,
    *,
    state_root: Path,
) -> int:
    """Append a precomputed plan; partial runs can safely retry unchanged variants."""

    pointer_body = await webdav.get_bytes(STABLE_POINTER_PATH)
    if pointer_body is None:
        raise ContentInventoryError("stable pointer is missing before migration")
    try:
        pointer = GenerationPointer.model_validate_json(pointer_body)
    except ValueError as exc:
        raise ContentInventoryError("stable pointer is invalid before migration") from exc
    if pointer.canonical_bytes() != pointer_body or pointer.generation_id != plan.stable_generation_id:
        raise ContentInventoryError("stable generation changed before migration")
    store = ContentOCRVariantStore(webdav=webdav, state_root=state_root)
    for index, manifest in enumerate(plan.variants):
        if index % 100 == 0 and await webdav.get_bytes(STABLE_POINTER_PATH) != pointer_body:
            raise ContentInventoryError("stable pointer changed during migration")
        await store.publish_existing(manifest)
    if await webdav.get_bytes(STABLE_POINTER_PATH) != pointer_body:
        raise ContentInventoryError("stable pointer changed during migration")
    return len(plan.variants)
