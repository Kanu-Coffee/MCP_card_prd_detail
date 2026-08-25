from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from cardrag_core import canonical_json_bytes, canonical_sha256, sha256_bytes
from helpers import pdf_bytes

import cardrag_worker.cli as cli_module
from cardrag_worker.adoption import (
    AdoptionError,
    load_legacy_prepare_bundle,
    reconcile_inventories,
    validate_candidate,
)

OCR_ONE = "## Page 1\n\n카드 혜택의 전월 실적 조건과 제외 항목을 충분히 설명합니다.\n".encode()


def inventory_row(
    root: Path,
    *,
    issuer: str = "kb",
    product_code: str = "p1",
    width: float = 612,
    suffix: str = "one",
) -> dict[str, Any]:
    pdf = root / f"{suffix}.pdf"
    ocr = root / f"{suffix}.md"
    pdf.write_bytes(pdf_bytes(width=width))
    ocr.write_bytes(OCR_ONE.replace("혜택".encode(), f"혜택-{suffix}".encode()))
    return {
        "issuer": issuer,
        "product_code": product_code,
        "document_type": "product_description",
        "effective_date": date(2026, 8, 1).isoformat(),
        "source_version": suffix,
        "source_bundle_id": f"bundle-{suffix}",
        "source_bundle_sha256": sha256_bytes(f"bundle-{suffix}".encode()),
        "source_database_id": f"db-{suffix}",
        "source_document_id": f"legacy-{suffix}",
        "pdf_path": str(pdf),
        "ocr_path": str(ocr),
        "ledger_pdf_sha256": sha256_bytes(pdf.read_bytes()),
        "ledger_ocr_sha256": sha256_bytes(ocr.read_bytes()),
    }


def write_legacy_bundle(root: Path) -> tuple[Path, Path, dict[str, Any]]:
    for directory in (
        "manifests",
        "objects/pdf/sha256",
        "objects/ocr/sha256",
        "records",
        "reports",
    ):
        (root / directory).mkdir(parents=True, exist_ok=True)
    pdf_body = pdf_bytes()
    pdf_sha = sha256_bytes(pdf_body)
    ocr_sha = sha256_bytes(OCR_ONE)
    pdf_relative = f"objects/pdf/sha256/{pdf_sha[:2]}/{pdf_sha}.pdf"
    ocr_relative = f"objects/ocr/sha256/{ocr_sha[:2]}/{ocr_sha}.md"
    (root / pdf_relative).parent.mkdir(parents=True, exist_ok=True)
    (root / ocr_relative).parent.mkdir(parents=True, exist_ok=True)
    (root / pdf_relative).write_bytes(pdf_body)
    (root / ocr_relative).write_bytes(OCR_ONE)
    document = {
        "document_id": "legacy-doc-1",
        "document_key": "kb:p1:product_description:2026-08-01:1",
        "issuer": "kb",
        "product_code": "p1",
        "product_name": "KB 테스트 카드",
        "document_type": "product_description",
        "effective_date": "2026-08-01",
        "source_version": "1",
        "version_sort_key": [[0, 1]],
        "source_url": "https://card.kbcard.com/legacy.pdf",
        "source_post_id": "legacy-post-1",
        "file_name": "legacy.pdf",
        "discovered_at": "2026-08-01T00:00:00Z",
        "is_latest": True,
        "adoption_status": "adopted",
        "pdf_sha256": pdf_sha,
        "pdf_size_bytes": len(pdf_body),
        "pdf_page_count": 1,
        "pdf_object_path": pdf_relative,
        "ocr_sha256": ocr_sha,
        "ocr_size_bytes": len(OCR_ONE),
        "ocr_object_path": ocr_relative,
        "metadata_sha256": None,
        "metadata_object_path": None,
        "metadata_schema": "cardrag.legacy-metadata.v1",
        "adoption_reason": "strictly verified legacy OCR",
        "mapping_method": "direct_path_and_hash",
        "source_pdf_path": None,
        "source_ocr_path": None,
        "source_metadata_path": None,
        "warnings": [],
    }
    documents_path = root / "manifests/documents.jsonl"
    documents_path.write_bytes(canonical_json_bytes(document) + b"\n")
    record_path = root / "records/kb/p1/product_description/2026-08-01/1/record.json"
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_bytes(canonical_json_bytes(document) + b"\n")
    (root / "manifests/source-files.jsonl").write_bytes(b"")
    (root / "manifests/exceptions.jsonl").write_bytes(b"")
    source_manifest_sha = "f" * 64
    content_spec = {
        "adoption_policy": "cardrag.legacy-ocr-adoption.v1",
        "documents_sha256": sha256_bytes(documents_path.read_bytes()),
        "exceptions_sha256": sha256_bytes(b""),
        "objects": [
            {"kind": "ocr", "sha256": ocr_sha, "size_bytes": len(OCR_ONE)},
            {"kind": "pdf", "sha256": pdf_sha, "size_bytes": len(pdf_body)},
        ],
        "schema_version": "cardrag.legacy-bundle.v1",
        "source_files_sha256": sha256_bytes(b""),
        "source_manifest_sha256": source_manifest_sha,
    }
    content_sha = canonical_sha256(content_spec)
    manifest = {
        "schema_version": "cardrag.legacy-bundle.v1",
        "bundle_id": f"bundle-{content_sha[:12]}",
        "content_sha256": content_sha,
        "source_manifest_sha256": source_manifest_sha,
        "document_count": 1,
        "adopted_count": 1,
        "reocr_count": 0,
        "unique_pdf_objects": 1,
        "unique_ocr_objects": 1,
        "unique_metadata_objects": 0,
        "payload_bytes": len(pdf_body) + len(OCR_ONE),
        "documents_manifest": "manifests/documents.jsonl",
        "source_files_manifest": "manifests/source-files.jsonl",
        "exceptions_manifest": "manifests/exceptions.jsonl",
        "adoption_policy": "cardrag.legacy-ocr-adoption.v1",
    }
    (root / "bundle-manifest.json").write_bytes(canonical_json_bytes(manifest) + b"\n")
    reseal_legacy_bundle(root, manifest)
    ledger = {
        "schema_version": "cardrag.legacy-adoption-ledger.v1",
        "source_database_id": "v0.2.1-postgres:" + "a" * 64,
        "import_id": "00000000-0000-0000-0000-000000000001",
        "bundle_id": manifest["bundle_id"],
        "bundle_sha256": content_sha,
        "generation_id": "legacy-generation",
        "source_document_id": document["document_id"],
        "document_key": document["document_key"],
        "issuer": "kb",
        "pdf_sha256": pdf_sha,
        "ocr_sha256": ocr_sha,
        "disposition": "adopted",
        "status": "succeeded",
    }
    ledger_path = root.parent / "ledger.jsonl"
    ledger_path.write_bytes(canonical_json_bytes(ledger) + b"\n")
    return root, ledger_path, document


def reseal_legacy_bundle(root: Path, manifest: dict[str, Any]) -> None:
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name not in {"checksums.sha256", "READY"}:
            rows.append(f"{sha256_bytes(path.read_bytes())}  {path.relative_to(root).as_posix()}\n")
    (root / "checksums.sha256").write_text("".join(rows), encoding="utf-8")
    ready = {
        "schema_version": "cardrag.legacy-bundle-ready.v1",
        "bundle_id": manifest["bundle_id"],
        "content_sha256": manifest["content_sha256"],
        "bundle_manifest_sha256": sha256_bytes((root / "bundle-manifest.json").read_bytes()),
        "checksums_sha256": sha256_bytes((root / "checksums.sha256").read_bytes()),
    }
    (root / "READY").write_bytes(canonical_json_bytes(ready) + b"\n")


def test_current_wins_legacy_conflict_without_blocking_publication(tmp_path: Path) -> None:
    current = inventory_row(tmp_path, suffix="current")
    legacy = inventory_row(tmp_path, width=613, suffix="legacy")
    result = reconcile_inventories([current], [legacy])
    assert len(result.receipts) == 1
    assert result.receipts[0]["source_kind"] == "current"
    assert len(result.conflicts) == 1
    assert result.conflicts[0].kind == "current_over_legacy"
    assert result.conflicts[0].blocking is False


@pytest.mark.asyncio
async def test_nonblocking_current_override_can_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = reconcile_inventories(
        [inventory_row(tmp_path, suffix="current")],
        [inventory_row(tmp_path, width=613, suffix="legacy")],
    )

    class Client:
        async def close(self) -> None:
            return None

    async def publish(accepted: Any, client: Any) -> int:
        assert len(accepted.receipts) == 1
        return 1

    monkeypatch.setattr(cli_module.WorkerSettings, "from_env", lambda **kwargs: object())
    monkeypatch.setattr(cli_module.WebDAVClient, "from_env", lambda: Client())
    monkeypatch.setattr(cli_module, "publish_adoptions", publish)
    assert await cli_module._publish_if_requested(result, True) == 1


@pytest.mark.asyncio
async def test_invalid_candidate_is_reported_while_valid_candidate_can_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    valid = inventory_row(tmp_path, issuer="kb", product_code="valid", suffix="valid")
    invalid = inventory_row(tmp_path, issuer="woori", product_code="invalid", suffix="invalid")
    Path(invalid["ocr_path"]).write_text("not canonical OCR", encoding="utf-8")
    result = reconcile_inventories([valid, invalid], [])
    assert len(result.receipts) == 1
    assert len(result.errors) == 1

    class Client:
        async def close(self) -> None:
            return None

    async def publish(accepted: Any, client: Any) -> int:
        assert len(accepted.receipts) == 1
        assert len(accepted.errors) == 1
        return 1

    monkeypatch.setattr(cli_module.WorkerSettings, "from_env", lambda **kwargs: object())
    monkeypatch.setattr(cli_module.WebDAVClient, "from_env", lambda: Client())
    monkeypatch.setattr(cli_module, "publish_adoptions", publish)
    assert await cli_module._publish_if_requested(result, True) == 1


def test_multiple_current_candidates_for_one_product_are_blocking(tmp_path: Path) -> None:
    result = reconcile_inventories(
        [
            inventory_row(tmp_path, suffix="first"),
            inventory_row(tmp_path, width=613, suffix="second"),
        ],
        [],
    )
    assert result.receipts == ()
    assert any(conflict.blocking for conflict in result.conflicts)


def test_adoption_manifest_is_deterministic_and_symlinks_are_rejected(tmp_path: Path) -> None:
    row = inventory_row(tmp_path, suffix="stable")
    first = validate_candidate(row, source_kind="current")
    second = validate_candidate(row, source_kind="current")
    assert canonical_json_bytes(first["manifest"]) == canonical_json_bytes(second["manifest"])
    link = tmp_path / "linked.pdf"
    link.symlink_to(Path(row["pdf_path"]))
    row["pdf_path"] = str(link)
    with pytest.raises(AdoptionError, match="symlink"):
        validate_candidate(row, source_kind="current")


def test_legacy_bundle_loader_binds_full_content_and_future_identity(tmp_path: Path) -> None:
    bundle, ledger, _ = write_legacy_bundle(tmp_path / "bundle")
    rows = load_legacy_prepare_bundle(bundle, ledger)
    assert len(rows) == 1
    assert rows[0]["source_document_id"] == "legacy-doc-1"
    assert rows[0]["product_code"] == "p1"


def test_legacy_identity_field_tamper_fails_even_when_checksums_are_resealed(tmp_path: Path) -> None:
    bundle, ledger, document = write_legacy_bundle(tmp_path / "bundle")
    document["product_code"] = "attacker-controlled"
    (bundle / "manifests/documents.jsonl").write_bytes(canonical_json_bytes(document) + b"\n")
    manifest = json.loads((bundle / "bundle-manifest.json").read_bytes())
    reseal_legacy_bundle(bundle, manifest)
    with pytest.raises(AdoptionError, match="content identity"):
        load_legacy_prepare_bundle(bundle, ledger)


def test_legacy_ledger_duplicate_is_rejected(tmp_path: Path) -> None:
    bundle, ledger, _ = write_legacy_bundle(tmp_path / "bundle")
    ledger.write_bytes(ledger.read_bytes() * 2)
    with pytest.raises(AdoptionError, match="duplicate"):
        load_legacy_prepare_bundle(bundle, ledger)
