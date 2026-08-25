from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cardrag.cli import app
from cardrag.legacy import (
    DATA_KIT_INVENTORY_SCHEMA,
    DATA_KIT_REJECTION_SCHEMA,
    DataKitInventoryError,
    export_data_kit_inventory,
    write_data_kit_inventory,
)
from tests.support_pdf import synthetic_text_pdf_bytes


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _fixture(tmp_path: Path, *, preamble: bool = False) -> tuple[Path, Path]:
    source = tmp_path / "data-kit" / "cardrag-conveyor-data"
    for relative in ("data/db", "artifacts/manifests", "reports", "logs", "outputs"):
        (source / relative).mkdir(parents=True)
    document_id = "wooricard:100000:product_description:2025-01-01:v1"
    output_base = "artifacts/ocr/wooricard/products/100000__fixture/product_description/2025-01-01__v1"
    raw_relative = (
        "artifacts/raw-pdfs/wooricard/products/100000__fixture/"
        "product_description/2025-01-01__v1__fixture.pdf"
    )
    ocr_relative = f"{output_base}/ocr.md"
    metadata_relative = f"{output_base}/metadata.json"
    pdf = synthetic_text_pdf_bytes(["data kit fixture"])
    ocr = (
        "# OCR complete\n\n" if preamble else ""
    ) + "## Page 1\n충분히 긴 테스트 OCR 본문입니다. 카드 혜택 원문입니다.\n"
    (source / raw_relative).parent.mkdir(parents=True)
    (source / raw_relative).write_bytes(pdf)
    (source / ocr_relative).parent.mkdir(parents=True)
    (source / ocr_relative).write_text(ocr, encoding="utf-8")
    metadata = {
        "schema_version": "cardrag_imported_ocr_asset.v1",
        "status": "success",
        "doc_version_id": document_id,
        "primary_text_artifact": "ocr.md",
        "ocr_md_rel_path": ocr_relative,
        "raw_pdf_rel_path": raw_relative,
        "metadata_rel_path": metadata_relative,
        "ocr_md_sha256": f"sha256:{_sha(ocr.encode())}",
        "raw_pdf_sha256": f"sha256:{_sha(pdf)}",
        "ocr_md_chars": len(ocr),
        "pages": 1,
    }
    (source / metadata_relative).write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    master_entry = {
        "status": "done",
        "cardCompany": "wooricard",
        "doc_version_id": document_id,
        "productCode": "100000",
        "productName": "fixture",
        "docType": "product_description",
        "beginDt": "2025-01-01",
        "gdccVer": "1",
        "fileNm": "fixture.pdf",
        "fileSize": len(pdf),
        "sourceUrl": "https://example.invalid/fixture.pdf",
        "sourcePostId": "fixture",
        "raw_pdf_rel_path": raw_relative,
        "pdf_sha256": f"sha256:{_sha(pdf)}",
        "output_base_rel": output_base,
        "guide_remote_rel": None,
        "ocr_remote_rel": ocr_relative,
        "metadata_remote_rel": metadata_relative,
        "pages": 1,
        "guide_chars": None,
        "ocr_chars": len(ocr),
        "completed_at": "2026-01-01T00:00:00+00:00",
        "error": "",
    }
    (source / "artifacts/manifests/cardrag_master_manifest.json").write_text(
        json.dumps(
            {"schema_version": "cardrag_master_manifest.v2", "entries": [master_entry]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    data_pack = {
        "name": "cardrag-conveyor data-kit",
        "release_name": "cardrag-conveyor-hatch-fixture",
        "created_kst": "2026-01-01T09:00:00+09:00",
        "created_utc": "2026-01-01T00:00:00+00:00",
        "source_hatch_root": "/read-only/source",
        "data_kit_path": "data-kit/cardrag-conveyor-data",
        "included_roots": ["data/", "artifacts/", "reports/", "logs/", "outputs/"],
        "sqlite_dbs": [
            "data/db/inventory.sqlite3",
            "data/db/evidence_inventory.sqlite3",
            "data/db/structured_sections.sqlite3",
            "data/db/api_embeddings_structured.sqlite3",
        ],
        "key_counts": {
            "manifest_entries": 1,
            "inventory_docs": 1,
            "inventory_by_issuer": [["wooricard", 1]],
        },
        "excluded_transient": [],
        "notes": "fixture",
    }
    (source / "DATA_PACK_MANIFEST.json").write_text(
        json.dumps(data_pack, ensure_ascii=False), encoding="utf-8"
    )

    inventory = source / "data/db/inventory.sqlite3"
    connection = sqlite3.connect(inventory)
    connection.executescript(
        """
        CREATE TABLE documents (
          doc_id text primary key,
          card_company text not null default 'unknown',
          product_code text not null,
          product_name text not null,
          doc_type text not null,
          effective_date text not null,
          version text not null,
          file_name text not null,
          status text not null,
          output_base_rel text,
          guide_remote_rel text,
          ocr_remote_rel text,
          metadata_remote_rel text,
          pages integer,
          guide_chars integer,
          ocr_chars integer,
          completed_at text,
          error text not null default '',
          is_latest integer not null default 0
        );
        CREATE TABLE index_state (
          doc_id text primary key references documents(doc_id) on delete cascade,
          state text not null,
          last_error text not null default '',
          updated_at text default current_timestamp
        );
        CREATE INDEX idx_documents_latest on documents(card_company, is_latest, doc_type);
        CREATE INDEX idx_documents_product on documents(card_company, product_code, doc_type, effective_date);
        CREATE INDEX idx_index_state_state on index_state(state);
        """
    )
    connection.execute(
        "INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            document_id,
            "wooricard",
            "100000",
            "fixture",
            "product_description",
            "2025-01-01",
            "1",
            "fixture.pdf",
            "done",
            output_base,
            None,
            ocr_relative,
            metadata_relative,
            1,
            None,
            len(ocr),
            "2026-01-01T00:00:00+00:00",
            "",
            1,
        ),
    )
    connection.execute(
        "INSERT INTO index_state(doc_id, state, last_error) VALUES (?, 'needs_reindex', '')",
        (document_id,),
    )
    connection.commit()
    connection.close()
    shutil.copyfile(inventory, source / "data/db/ocr_inventory.sqlite3")
    return source, Path(raw_relative)


def _source_fingerprint(source: Path) -> dict[str, tuple[int, int, int]]:
    return {
        path.relative_to(source).as_posix(): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
            path.stat().st_mode,
        )
        for path in source.rglob("*")
        if path.is_file()
    }


def test_export_is_read_only_canonical_and_worker_compatible(tmp_path: Path) -> None:
    source, _ = _fixture(tmp_path)
    before = _source_fingerprint(source)

    result = export_data_kit_inventory(source.resolve())

    assert result.selected_documents == 1
    assert result.rejected == ()
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row["schema_version"] == DATA_KIT_INVENTORY_SCHEMA
    assert row["issuer"] == "woori"
    assert row["source_database_id"].startswith("data-kit-sqlite-sha256:")
    assert len(row["source_data_pack_manifest_sha256"]) == 64
    assert len(row["source_master_manifest_sha256"]) == 64
    assert Path(row["pdf_path"]).is_absolute()
    assert Path(row["ocr_path"]).is_absolute()
    assert before == _source_fingerprint(source)

    output = tmp_path / "export" / "legacy.jsonl"
    rejected = tmp_path / "export" / "rejected.jsonl"
    output.parent.mkdir()
    written, rejected_written = write_data_kit_inventory(
        result,
        output,
        source_root=source,
        rejected_output=rejected,
    )
    assert json.loads(written.read_text(encoding="utf-8")) == row
    assert rejected_written.read_bytes() == b""
    assert stat_mode(written) == 0o600


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_noncanonical_candidate_is_rejected_without_blocking_valid_export(tmp_path: Path) -> None:
    source, _ = _fixture(tmp_path, preamble=True)

    result = export_data_kit_inventory(source.resolve())

    assert result.rows == ()
    assert len(result.rejected) == 1
    assert result.rejected[0]["schema_version"] == DATA_KIT_REJECTION_SCHEMA
    assert result.rejected[0]["reason"] == "ocr_noncanonical"


def test_database_master_mismatch_fails_closed(tmp_path: Path) -> None:
    source, _ = _fixture(tmp_path)
    manifest_path = source / "artifacts/manifests/cardrag_master_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["entries"][0]["productName"] = "tampered"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(DataKitInventoryError, match="disagree"):
        export_data_kit_inventory(source.resolve())


def test_pdf_hash_mismatch_is_explicitly_rejected(tmp_path: Path) -> None:
    source, relative = _fixture(tmp_path)
    (source / relative).write_bytes(synthetic_text_pdf_bytes(["different bytes"]))

    result = export_data_kit_inventory(source.resolve())

    assert result.rows == ()
    assert result.rejected[0]["reason"] == "pdf_hash_mismatch"


def test_missing_declared_pdf_path_uses_one_pass_hash_lookup(tmp_path: Path) -> None:
    source, relative = _fixture(tmp_path)
    relocated = source / "artifacts/raw-pdfs/unmapped/relocated.pdf"
    relocated.parent.mkdir(parents=True)
    (source / relative).replace(relocated)
    manifest_path = source / "artifacts/manifests/cardrag_master_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["entries"][0].pop("raw_pdf_rel_path")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    metadata_path = source / manifest["entries"][0]["metadata_remote_rel"]
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.pop("raw_pdf_rel_path")
    metadata.pop("raw_pdf_sha256")
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")

    result = export_data_kit_inventory(source.resolve())

    assert len(result.rows) == 1
    assert result.rows[0]["pdf_path"] == str(relocated.resolve())


def test_sqlite_twins_must_be_byte_identical(tmp_path: Path) -> None:
    source, _ = _fixture(tmp_path)
    with (source / "data/db/ocr_inventory.sqlite3").open("ab") as stream:
        stream.write(b"tamper")

    with pytest.raises(DataKitInventoryError, match="not identical"):
        export_data_kit_inventory(source.resolve())


def test_source_symlink_and_existing_output_are_rejected(tmp_path: Path) -> None:
    source, _ = _fixture(tmp_path)
    os.symlink(tmp_path, source / "reports/unsafe-link")
    with pytest.raises(DataKitInventoryError, match="symlink"):
        export_data_kit_inventory(source.resolve())
    (source / "reports/unsafe-link").unlink()

    result = export_data_kit_inventory(source.resolve())
    output = tmp_path / "existing.jsonl"
    output.write_text("preserve", encoding="utf-8")
    with pytest.raises(DataKitInventoryError, match="already exists"):
        write_data_kit_inventory(result, output, source_root=source)
    assert output.read_text(encoding="utf-8") == "preserve"


def test_cli_exposes_raw_data_kit_export() -> None:
    result = CliRunner().invoke(app, ["legacy", "export-data-kit-inventory", "--help"])

    assert result.exit_code == 0
    assert "--source" in result.stdout
    assert "--rejected-output" in result.stdout
