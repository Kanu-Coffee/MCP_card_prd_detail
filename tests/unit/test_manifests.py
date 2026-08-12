from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from cardrag.domain import (
    ArtifactManifest,
    ArtifactType,
    DocumentIdentity,
    Issuer,
    Lineage,
    ManifestAttribute,
    canonical_json_bytes,
    canonical_sha256,
    sha256_bytes,
)


def _document() -> DocumentIdentity:
    return DocumentIdentity(
        issuer=Issuer.KB,
        product_code="KB-1",
        document_type="product_description",
        effective_date="2026-08-12",
        version="10",
    )


def _lineage() -> Lineage:
    return Lineage(
        processor="fixture-ocr",
        processor_version="1.2.0",
        config_sha256=sha256_bytes(b"config"),
        input_sha256=(sha256_bytes(b"second"), sha256_bytes(b"first")),
        input_artifact_ids=("artifact_z", "artifact_a"),
        prompt_version="ocr.v1",
        provider="fixture",
        model="deterministic",
        attempt=2,
    )


def test_canonical_json_is_key_order_independent_and_rejects_nan() -> None:
    assert canonical_json_bytes({"b": 2, "a": 1}) == b'{"a":1,"b":2}'
    assert canonical_sha256({"a": 1, "b": 2}) == canonical_sha256({"b": 2, "a": 1})

    with pytest.raises(ValueError):
        canonical_json_bytes({"score": float("nan")})


def test_lineage_normalizes_set_like_references_and_is_canonical() -> None:
    lineage = _lineage()

    assert lineage.input_sha256 == tuple(sorted(lineage.input_sha256))
    assert lineage.input_artifact_ids == ("artifact_a", "artifact_z")
    assert len(lineage.canonical_sha256) == 64

    with pytest.raises(ValidationError):
        Lineage(
            processor="fixture",
            processor_version="1",
            config_sha256=sha256_bytes(b"config"),
            input_sha256=(sha256_bytes(b"same"), sha256_bytes(b"same")),
        )


def test_artifact_manifest_round_trip_and_hash_change_detection() -> None:
    payload = "# OCR\n원문".encode()
    manifest = ArtifactManifest.for_bytes(
        artifact_type=ArtifactType.OCR_MARKDOWN,
        payload=payload,
        media_type="text/markdown; charset=utf-8",
        created_at=datetime(2026, 8, 12, 3, 4, 5, tzinfo=UTC),
        lineage=_lineage(),
        document=_document(),
        page_count=1,
        attributes=(
            ManifestAttribute(name="render_scale", value=6.0),
            ManifestAttribute(name="character_count", value=8),
        ),
    )

    restored = ArtifactManifest.model_validate_json(manifest.canonical_bytes())
    assert restored == manifest
    assert manifest.content_sha256 == sha256_bytes(payload)
    assert manifest.attributes[0].name == "character_count"
    assert manifest.artifact_id.startswith("artifact_")
    assert manifest.manifest_id.startswith("manifest_")

    changed = manifest.model_copy(update={"size_bytes": manifest.size_bytes + 1})
    assert changed.canonical_sha256 != manifest.canonical_sha256


def test_manifest_rejects_invalid_hash_and_duplicate_attributes() -> None:
    with pytest.raises(ValidationError):
        ArtifactManifest(
            artifact_type=ArtifactType.SOURCE_PDF,
            content_sha256="A" * 64,
            size_bytes=1,
            media_type="application/pdf",
            created_at=datetime.now(UTC),
            lineage=_lineage(),
        )

    duplicate = ManifestAttribute(name="pages", value=1)
    with pytest.raises(ValidationError):
        ArtifactManifest.for_bytes(
            artifact_type=ArtifactType.SOURCE_PDF,
            payload=b"%PDF-fixture",
            media_type="application/pdf",
            created_at=datetime.now(UTC),
            lineage=_lineage(),
            attributes=(duplicate, duplicate),
        )
