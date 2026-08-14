"""Explicit OCR adoption provenance and compatibility policy."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from cardrag.pipeline.ocr import OCR_PROMPT_VERSION

from .bundle import ADOPTION_POLICY_VERSION, LegacyBundleDocument, LegacyBundleManifest


def legacy_adoption_manifest(
    bundle: LegacyBundleManifest,
    document: LegacyBundleDocument,
    *,
    import_id: UUID,
) -> dict[str, object]:
    """Describe validated legacy bytes without fabricating native OCR provenance."""

    if document.adoption_status != "adopted" or document.ocr_sha256 is None:
        raise ValueError("only validated legacy OCR may receive adoption provenance")
    return {
        "schema_version": ADOPTION_POLICY_VERSION,
        "adoption_policy": ADOPTION_POLICY_VERSION,
        "status": "adopted",
        "bundle_id": bundle.bundle_id,
        "bundle_sha256": bundle.content_sha256,
        "import_id": str(import_id),
        "document_key": document.document_key,
        "pdf_sha256": document.pdf_sha256,
        "ocr_sha256": document.ocr_sha256,
        "page_count": document.pdf_page_count,
        "legacy_metadata_schema": document.metadata_schema,
        "validation": {
            "hash_verified": True,
            "page_coverage_verified": True,
            "utf8_verified": True,
        },
        "attempt": {
            "provider": "legacy-import",
            "model": "legacy-unreported",
            "renderer": "legacy-unreported",
            "prompt_version": "legacy-unreported",
            "reasoning_effort": None,
            "render_scale": None,
            "chunk_pages": None,
        },
    }


def ocr_manifest_is_reusable(
    manifest: dict[str, Any] | None,
    *,
    pdf_sha256: str,
    ocr_sha256: str,
    renderer: str,
    reasoning_effort: str,
    render_scale: float,
    chunk_pages: int,
    codex_model: str,
    fallback_model: str,
    prompt_version: str = OCR_PROMPT_VERSION,
) -> bool:
    """Check native OCR compatibility without granting legacy adoption.

    Legacy adoption is deliberately a database-bound decision: the manifest
    must also match a successful import/document ledger row.  A pure Python
    helper cannot prove that condition and therefore fails closed for legacy
    manifests.
    """

    if not manifest:
        return False
    if manifest.get("schema_version") == ADOPTION_POLICY_VERSION:
        return False
    attempt = manifest.get("attempt")
    if not isinstance(attempt, dict):
        return False
    provider = attempt.get("provider")
    model_ok = (provider == "codex-exec" and attempt.get("model") == codex_model) or (
        provider == "openrouter" and attempt.get("model") == fallback_model
    )
    return bool(
        attempt.get("prompt_version") == prompt_version
        and attempt.get("renderer") == renderer
        and (provider != "codex-exec" or attempt.get("reasoning_effort") == reasoning_effort)
        and attempt.get("render_scale") == render_scale
        and attempt.get("chunk_pages") == chunk_pages
        and model_ok
    )
