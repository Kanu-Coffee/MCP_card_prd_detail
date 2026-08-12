from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from cardrag.legacy.migration import LegacyMigrator
from cardrag.storage import ContentAddressedObjectStore

TINY_PDF = b"%PDF-1.4\n% license-safe synthetic fixture\n1 0 obj<<>>endobj\n%%EOF\n"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_manifest(source: Path, items: list[dict[str, object]]) -> Path:
    manifest = source / "master-manifest.json"
    manifest.write_text(json.dumps({"items": items}, ensure_ascii=False), encoding="utf-8")
    return manifest


def _object_store(tmp_path: Path) -> ContentAddressedObjectStore:
    return ContentAddressedObjectStore((tmp_path / "objects").resolve())


def test_hash_lookup_counter_drift_read_only_and_idempotent_copy(tmp_path: Path) -> None:
    source = tmp_path / "legacy"
    (source / "raw").mkdir(parents=True)
    (source / "ocr").mkdir()
    (source / "raw/card.pdf").write_bytes(TINY_PDF)
    (source / "raw/duplicate.pdf").write_bytes(TINY_PDF)
    ocr_text = "## Page 1\n전월실적 30만원 이상\n"
    (source / "ocr/card.md").write_text(ocr_text, encoding="utf-8")
    manifest = _write_manifest(
        source,
        [
            {
                "doc_version_id": "direct",
                "raw_pdf_rel_path": "raw/card.pdf",
                "pdf_sha256": _sha256(TINY_PDF),
                "ocr_rel_path": "ocr/card.md",
                "ocr_chars": len(ocr_text),
            },
            {
                "doc_version_id": "hash-lookup",
                "raw_pdf_rel_path": "missing/card.pdf",
                "pdf_sha256": _sha256(TINY_PDF),
                "ocr_rel_path": "ocr/card.md",
                "ocr_chars": len(ocr_text) + 7,
            },
        ],
    )
    for path in source.rglob("*"):
        if path.is_file():
            path.chmod(0o444)
    store = _object_store(tmp_path)
    migrator = LegacyMigrator(source, store)
    before = migrator.snapshot_source_metadata()

    first = migrator.migrate_manifest(manifest)
    second = migrator.migrate_manifest(manifest)

    assert migrator.snapshot_source_metadata() == before
    assert first.source_writes == second.source_writes == 0
    assert first.selected_documents == first.migrated_documents == 2
    assert first.unique_pdf_objects == 1
    assert first.unique_ocr_objects == 1
    assert first.raw_path_missing_resolved_by_hash == 1
    assert first.master_ocr_chars_drift == 1
    assert {mapping.mapping_method for mapping in first.mappings} == {
        "direct_path_and_hash",
        "hash_lookup",
    }
    assert all(mapping.pdf_object_path.startswith("sha256/") for mapping in first.mappings)
    assert all(mapping.ocr_object_path.startswith("sha256/") for mapping in first.mappings)
    assert first.historical_embeddings_imported == 0
    assert first.quarantined == 0
    assert second.model_dump(exclude={"inventory_sha256"}) == first.model_dump(exclude={"inventory_sha256"})
    assert len(list((store.root / "sha256").glob("*/*"))) == 2
    assert store.read_bytes(_sha256(TINY_PDF)) == TINY_PDF


def test_path_escape_hash_mismatch_and_unresolved_items_are_quarantined(tmp_path: Path) -> None:
    source = tmp_path / "legacy"
    (source / "raw").mkdir(parents=True)
    (source / "raw/card.pdf").write_bytes(TINY_PDF)
    outside = tmp_path / "outside.md"
    outside.write_text("외부 OCR", encoding="utf-8")
    manifest = _write_manifest(
        source,
        [
            {
                "document_id": "wrong-hash",
                "raw_pdf_rel_path": "raw/card.pdf",
                "pdf_sha256": "0" * 64,
            },
            {
                "document_id": "unresolved",
                "raw_pdf_rel_path": "missing.pdf",
                "pdf_sha256": "1" * 64,
            },
            {
                "document_id": "escaped-ocr",
                "raw_pdf_rel_path": "raw/card.pdf",
                "pdf_sha256": _sha256(TINY_PDF),
                "ocr_rel_path": "../outside.md",
                "ocr_chars": len("외부 OCR"),
            },
        ],
    )

    report = LegacyMigrator(source, _object_store(tmp_path)).migrate_manifest(manifest)

    assert report.selected_documents == 3
    assert report.migrated_documents == 0
    assert report.quarantined == 3
    assert {exception.code for exception in report.exceptions} == {
        "pdf_hash_mismatch",
        "raw_pdf_unresolved",
        "ocr_unreadable",
    }


def test_inventory_rejects_symlink_escape(tmp_path: Path) -> None:
    source = tmp_path / "legacy"
    source.mkdir()
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(TINY_PDF)
    os.symlink(outside, source / "escaped.pdf")

    with pytest.raises(ValueError, match="symlink escape"):
        LegacyMigrator(source, _object_store(tmp_path)).inventory()


def test_declared_ocr_hash_mismatch_is_quarantined_without_silent_rewrite(tmp_path: Path) -> None:
    source = tmp_path / "legacy"
    (source / "raw").mkdir(parents=True)
    (source / "ocr").mkdir()
    (source / "raw/card.pdf").write_bytes(TINY_PDF)
    ocr_text = "## Page 1\n검증할 OCR 원문\n"
    (source / "ocr/card.md").write_text(ocr_text, encoding="utf-8")
    manifest = _write_manifest(
        source,
        [
            {
                "document_id": "ocr-hash-mismatch",
                "raw_pdf_rel_path": "raw/card.pdf",
                "pdf_sha256": _sha256(TINY_PDF),
                "ocr_rel_path": "ocr/card.md",
                "ocr_md_sha256": "0" * 64,
                "ocr_chars": len(ocr_text),
            }
        ],
    )

    report = LegacyMigrator(source, _object_store(tmp_path)).migrate_manifest(manifest)

    assert report.migrated_documents == 0
    assert report.quarantined == 1
    assert [exception.code for exception in report.exceptions] == ["ocr_hash_mismatch"]


def test_pilot_staging_rollback_is_marker_and_root_bound(tmp_path: Path) -> None:
    build_root = tmp_path / "build"
    build_root.mkdir()
    pilot = LegacyMigrator.create_pilot_root(build_root, "pilot-012345abcdef")
    (pilot / "disposable.txt").write_text("fixture", encoding="utf-8")

    assert LegacyMigrator.rollback_pilot(build_root, pilot) == "pilot-012345abcdef"
    assert not pilot.exists()

    outside = tmp_path / "pilot-012345abcdef"
    outside.mkdir()
    (outside / ".cardrag-legacy-pilot.json").write_text(
        json.dumps(
            {
                "schema_version": "cardrag-legacy-pilot.v1",
                "pilot_id": outside.name,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="escaped"):
        LegacyMigrator.rollback_pilot(build_root, outside)
