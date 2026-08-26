from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
from io import BytesIO
from pathlib import Path
from typing import Literal

import pytest
from cardrag_core import canonical_json_bytes
from pypdf import PdfWriter

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from cardrag_worker.adoption import (  # type: ignore[import-untyped]  # noqa: E402
    AdoptionError,
    load_inventory,
)

import tools.legacy_data_kit_adoption_v2 as legacy_export  # noqa: E402
from tools.legacy_data_kit_adoption_v2 import (  # noqa: E402
    ADOPTION_POLICY_VERSION,
    EXACT_PROFILE,
    INVENTORY_SCHEMA,
    WOORI_GENERATED_PREFIX,
    WOORI_GENERATED_PREFIX_SHA256,
    WOORI_PREFIX_PROFILE,
    DataKitExportError,
    DataKitExportPlan,
    compute_source_bundle_sha256,
    export_data_kit_adoption_v2,
    plan_data_kit_adoption_v2,
)


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _pdf_bytes() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _fixture(
    tmp_path: Path,
    *,
    variant: Literal["exact", "prefix", "near-prefix", "short"] = "exact",
    issuer: Literal["wooricard", "kbcard"] = "wooricard",
) -> Path:
    source = tmp_path / "data-kit" / "cardrag-conveyor-data"
    for relative in ("data/db", "artifacts/manifests", "reports", "logs", "outputs"):
        (source / relative).mkdir(parents=True)
    document_id = f"{issuer}:100000:product_description:2025-01-01:v1"
    output_base = f"artifacts/ocr/{issuer}/products/100000__fixture/product_description/2025-01-01__v1"
    raw_relative = (
        f"artifacts/raw-pdfs/{issuer}/products/100000__fixture/"
        "product_description/2025-01-01__v1__fixture.pdf"
    )
    ocr_relative = f"{output_base}/ocr.md"
    metadata_relative = f"{output_base}/metadata.json"
    pdf = _pdf_bytes()
    canonical = "## Page 1\n\n충분히 긴 테스트 OCR 본문입니다. 카드 혜택 원문입니다.\n".encode()
    if variant == "exact":
        ocr = canonical
    elif variant == "prefix":
        ocr = WOORI_GENERATED_PREFIX + canonical
    elif variant == "near-prefix":
        ocr = "# OCR 처리 완료본 \n\n".encode() + canonical
    else:
        ocr = b"## Page 1\n\nX\n"
    (source / raw_relative).parent.mkdir(parents=True)
    (source / raw_relative).write_bytes(pdf)
    (source / ocr_relative).parent.mkdir(parents=True)
    (source / ocr_relative).write_bytes(ocr)
    metadata = {
        "schema_version": "cardrag_imported_ocr_asset.v1",
        "status": "success",
        "doc_version_id": document_id,
        "primary_text_artifact": "ocr.md",
        "ocr_md_rel_path": ocr_relative,
        "raw_pdf_rel_path": raw_relative,
        "metadata_rel_path": metadata_relative,
        "ocr_md_sha256": f"sha256:{_sha(ocr)}",
        "raw_pdf_sha256": f"sha256:{_sha(pdf)}",
        "ocr_md_chars": len(ocr.decode()),
        "pages": 1,
    }
    (source / metadata_relative).write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    master_entry = {
        "status": "done",
        "cardCompany": issuer,
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
        "ocr_chars": len(ocr.decode()),
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
            "inventory_by_issuer": [[issuer, 1]],
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
            issuer,
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
            len(ocr.decode()),
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
    return source


def _source_fingerprint(source: Path) -> dict[str, tuple[str, int, int, int]]:
    return {
        path.relative_to(source).as_posix(): (
            _sha(path.read_bytes()),
            path.stat().st_size,
            path.stat().st_mtime_ns,
            path.stat().st_mode,
        )
        for path in source.rglob("*")
        if path.is_file()
    }


def _jsonl(path: Path) -> list[dict[str, object]]:
    lines = path.read_bytes().splitlines()
    rows = [json.loads(line) for line in lines]
    assert all(line == canonical_json_bytes(row) for line, row in zip(lines, rows, strict=True))
    return rows


def test_exact_export_is_read_only_and_uses_original_ocr(tmp_path: Path) -> None:
    source = _fixture(tmp_path)
    before = _source_fingerprint(source)
    output = tmp_path / "export"

    result = export_data_kit_adoption_v2(source.resolve(), output.resolve())

    assert result.accepted_documents == 1
    assert result.exact_documents == 1
    assert result.normalized_documents == 0
    assert result.rejected_documents == 0
    row = _jsonl(result.inventory_path)[0]
    receipt = _jsonl(result.receipts_path)[0]
    assert row["schema_version"] == INVENTORY_SCHEMA
    assert row["normalization_profile"] == receipt["normalization_profile"] == EXACT_PROFILE
    assert row["ledger_ocr_sha256"] == row["source_ocr_sha256"] == row["normalized_ocr_sha256"]
    assert row["policy_version"] == ADOPTION_POLICY_VERSION
    assert row["source_ocr_path"] == row["ocr_path"]
    assert row["prefix_sha256"] is None
    assert row["removed_bytes"] == 0
    assert Path(str(row["ocr_path"])).is_relative_to(source)
    assert str(row["source_database_id"]).startswith("data-kit-sqlite-v2-sha256:")
    manifest = json.loads(result.manifest_path.read_bytes())
    assert manifest["source_root"] == str(source.resolve())
    assert before == _source_fingerprint(source)
    assert all((path.stat().st_mode & 0o777) == 0o600 for path in output.rglob("*") if path.is_file())


def test_worker_accepts_only_the_sealed_v2_export_root(tmp_path: Path) -> None:
    source = _fixture(tmp_path)
    result = export_data_kit_adoption_v2(source.resolve(), (tmp_path / "export").resolve())

    rows = load_inventory(result.output_root)

    assert len(rows) == 1
    assert rows[0]["source_document_id"] == rows[0]["legacy_source_document_id"]
    with pytest.raises(AdoptionError, match="sealed export directory"):
        load_inventory(result.inventory_path)


@pytest.mark.parametrize("suffix", ("-journal", "-wal", "-shm"))
def test_worker_sealed_loader_rejects_inventory_sqlite_sidecars_before_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
) -> None:
    source = _fixture(tmp_path)
    result = export_data_kit_adoption_v2(source.resolve(), (tmp_path / "export").resolve())
    Path(f"{source / 'data/db/inventory.sqlite3'}{suffix}").write_bytes(b"live sidecar")

    def unexpected_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        raise AssertionError("immutable SQLite query must not run when a sidecar exists")

    monkeypatch.setattr("cardrag_worker.adoption.sqlite3.connect", unexpected_connect)

    with pytest.raises(AdoptionError, match="live sidecar"):
        load_inventory(result.output_root)


def test_worker_sealed_loader_rejects_resealed_pdf_substitution(tmp_path: Path) -> None:
    source = _fixture(tmp_path)
    result = export_data_kit_adoption_v2(source.resolve(), (tmp_path / "export").resolve())
    writer = PdfWriter()
    writer.add_blank_page(width=613, height=792)
    alternate_buffer = BytesIO()
    writer.write(alternate_buffer)
    alternate_body = alternate_buffer.getvalue()
    alternate_pdf = source / "artifacts/raw-pdfs/alternate.pdf"
    alternate_pdf.parent.mkdir(parents=True, exist_ok=True)
    alternate_pdf.write_bytes(alternate_body)

    inventory = _jsonl(result.inventory_path)
    inventory[0]["pdf_path"] = str(alternate_pdf)
    inventory[0]["ledger_pdf_sha256"] = _sha(alternate_body)
    inventory[0]["pdf_size_bytes"] = len(alternate_body)
    inventory_body = b"".join(canonical_json_bytes(row) + b"\n" for row in inventory)
    result.inventory_path.write_bytes(inventory_body)
    manifest = json.loads(result.manifest_path.read_bytes())
    manifest["inventory_sha256"] = _sha(inventory_body)
    result.manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")

    with pytest.raises(AdoptionError, match="PDF hash differs from source master"):
        load_inventory(result.output_root)


def test_exact_woori_prefix_is_normalized_with_dual_lineage(tmp_path: Path) -> None:
    source = _fixture(tmp_path, variant="prefix")
    source_ocr = next((source / "artifacts/ocr").rglob("ocr.md"))
    source_payload = source_ocr.read_bytes()
    output = tmp_path / "export"

    result = export_data_kit_adoption_v2(source.resolve(), output.resolve())

    row = _jsonl(result.inventory_path)[0]
    receipt = _jsonl(result.receipts_path)[0]
    normalized_path = Path(str(row["ocr_path"]))
    normalized = normalized_path.read_bytes()
    assert result.exact_documents == 0
    assert result.normalized_documents == 1
    assert normalized == source_payload[len(WOORI_GENERATED_PREFIX) :]
    assert normalized.startswith(b"## Page 1")
    assert normalized_path.is_relative_to(output)
    assert row["ledger_ocr_sha256"] == row["source_ocr_sha256"] == _sha(source_payload)
    assert row["normalized_ocr_sha256"] == _sha(normalized)
    assert row["normalized_ocr_sha256"] != row["source_ocr_sha256"]
    assert receipt["normalization_profile"] == WOORI_PREFIX_PROFILE
    assert receipt["prefix_sha256"] == WOORI_GENERATED_PREFIX_SHA256
    assert receipt["removed_bytes"] == 24
    assert receipt["policy_version"] == ADOPTION_POLICY_VERSION
    assert row["source_ocr_path"] == str(source_ocr)


@pytest.mark.parametrize(
    ("variant", "issuer", "reason"),
    [
        ("near-prefix", "wooricard", "ocr_noncanonical"),
        ("prefix", "kbcard", "ocr_noncanonical"),
        ("short", "wooricard", "ocr_short_page"),
    ],
)
def test_unapproved_transforms_and_short_pages_are_rejected(
    tmp_path: Path,
    variant: Literal["prefix", "near-prefix", "short"],
    issuer: Literal["wooricard", "kbcard"],
    reason: str,
) -> None:
    source = _fixture(tmp_path, variant=variant, issuer=issuer)

    plan = plan_data_kit_adoption_v2(source.resolve())

    assert plan.candidates == ()
    assert len(plan.rejected) == 1
    assert plan.rejected[0]["reason"] == reason


def test_bundle_identity_is_deterministic_and_binds_lineage(tmp_path: Path) -> None:
    source = _fixture(tmp_path, variant="prefix")

    first = plan_data_kit_adoption_v2(source.resolve())
    second = plan_data_kit_adoption_v2(source.resolve())

    assert first.source_bundle_id == second.source_bundle_id
    assert first.source_bundle_sha256 == second.source_bundle_sha256
    transformations = [candidate.transformation_identity() for candidate in first.candidates]
    assert compute_source_bundle_sha256(first.controls, transformations) == first.source_bundle_sha256
    tampered = [dict(transformations[0])]
    tampered[0]["source_ocr_sha256"] = "0" * 64
    assert compute_source_bundle_sha256(first.controls, tampered) != first.source_bundle_sha256
    tampered = [dict(transformations[0])]
    tampered[0]["normalized_ocr_sha256"] = "f" * 64
    assert compute_source_bundle_sha256(first.controls, tampered) != first.source_bundle_sha256


def test_source_symlink_output_collision_and_source_nesting_fail_closed(tmp_path: Path) -> None:
    source = _fixture(tmp_path)
    source_alias = tmp_path / "source-root-link"
    os.symlink(source, source_alias)
    with pytest.raises(DataKitExportError, match="symlink"):
        export_data_kit_adoption_v2(source_alias.absolute(), (tmp_path / "link-export").absolute())
    source_alias.unlink()

    unsafe = source / "reports" / "unsafe-link"
    os.symlink(tmp_path, unsafe)
    with pytest.raises(DataKitExportError, match="symlink"):
        plan_data_kit_adoption_v2(source.resolve())
    unsafe.unlink()

    output = tmp_path / "existing"
    output.mkdir()
    sentinel = output / "preserve"
    sentinel.write_text("unchanged", encoding="utf-8")
    with pytest.raises(DataKitExportError, match="already exists"):
        export_data_kit_adoption_v2(source.resolve(), output.resolve())
    assert sentinel.read_text(encoding="utf-8") == "unchanged"

    with pytest.raises(DataKitExportError, match="outside the source"):
        export_data_kit_adoption_v2(source.resolve(), (source / "new-export").resolve())


def test_export_parent_swap_after_planning_fails_without_source_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _fixture(tmp_path, variant="prefix")
    output_parent = tmp_path / "output-parent"
    output_parent.mkdir()
    displaced_parent = tmp_path / "displaced-output-parent"
    output = output_parent / "export"
    source_before = _source_fingerprint(source)
    original_plan = plan_data_kit_adoption_v2

    def swap_parent_after_planning(source_root: Path) -> DataKitExportPlan:
        plan = original_plan(source_root)
        output_parent.rename(displaced_parent)
        os.symlink(source / "reports", output_parent, target_is_directory=True)
        return plan

    monkeypatch.setattr(
        "tools.legacy_data_kit_adoption_v2.plan_data_kit_adoption_v2",
        swap_parent_after_planning,
    )

    with pytest.raises(DataKitExportError, match="export parent changed"):
        export_data_kit_adoption_v2(source.resolve(), output.absolute())

    assert source_before == _source_fingerprint(source)
    assert not (source / "reports" / "export").exists()
    assert not (displaced_parent / "export").exists()


def test_export_in_place_output_mutation_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _fixture(tmp_path, variant="prefix")
    output = tmp_path / "export"
    original_create = legacy_export._ExportWriter.create
    mutated = False

    def create_then_mutate(
        writer: legacy_export._ExportWriter,
        relative: str,
        payload: bytes,
    ) -> None:
        nonlocal mutated
        original_create(writer, relative, payload)
        if relative == "inventory.jsonl" and not mutated:
            mutated = True
            with (output / relative).open("r+b") as stream:
                stream.write(b"X")
                stream.flush()
                os.fsync(stream.fileno())

    monkeypatch.setattr(legacy_export._ExportWriter, "create", create_then_mutate)

    with pytest.raises(DataKitExportError, match="bytes or identity changed"):
        export_data_kit_adoption_v2(source.resolve(), output.resolve())

    assert mutated is True
    assert not output.exists()


def test_source_control_replacement_during_parse_and_hash_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _fixture(tmp_path)
    control = source / "DATA_PACK_MANIFEST.json"
    replacement = source / "DATA_PACK_MANIFEST.replacement.json"
    replacement.write_bytes(control.read_bytes())
    original_finish = legacy_export._finish_stable_read
    replaced = False

    def replace_before_identity_check(
        path: Path,
        *,
        descriptor: int,
        before: os.stat_result,
        bytes_read: int,
        field: str,
    ) -> tuple[int, int, int, int, int, int, int]:
        nonlocal replaced
        if path == control and not replaced:
            replaced = True
            os.replace(replacement, control)
        return original_finish(
            path,
            descriptor=descriptor,
            before=before,
            bytes_read=bytes_read,
            field=field,
        )

    monkeypatch.setattr(legacy_export, "_finish_stable_read", replace_before_identity_check)

    with pytest.raises(DataKitExportError, match="changed while it was read"):
        plan_data_kit_adoption_v2(source.resolve())
