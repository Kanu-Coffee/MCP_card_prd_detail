from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

import pytest

from cardrag.legacy import (
    BundleIntegrityError,
    LegacyBundlePreparer,
    legacy_adoption_manifest,
    load_bundle_documents,
    ocr_manifest_is_reusable,
    verify_bundle,
)
from cardrag.pdf import PDF_RENDERER_ID
from tests.support_pdf import synthetic_text_pdf_bytes

PDF = synthetic_text_pdf_bytes(["deterministic synthetic fixture"])


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _fixture(tmp_path: Path, *, invalid_second_ocr: bool = True) -> tuple[Path, Path]:
    source = tmp_path / "legacy"
    (source / "raw").mkdir(parents=True)
    (source / "ocr/one").mkdir(parents=True)
    (source / "ocr/two").mkdir(parents=True)
    (source / "raw/card.pdf").write_bytes(PDF)
    one = "## Page 1\n첫 번째 자료\n"
    two = "## Page 1\n두 번째 자료\n" if invalid_second_ocr else "## Page 1\n두 번째 자료\n"
    (source / "ocr/one/ocr.md").write_text(one, encoding="utf-8")
    (source / "ocr/two/ocr.md").write_text(two, encoding="utf-8")
    metadata_one = {
        "schema_version": "ocr_result_manifest.v2",
        "raw_pdf_rel_path": "raw/card.pdf",
        "ocr_md_rel_path": "ocr/one/ocr.md",
        "ocr_md_sha256": f"sha256:{_sha(one.encode())}",
        "ocr_md_chars": len(one),
        "page_count": 1,
    }
    metadata_two = {
        "schema_version": "ocr_result_manifest.v2",
        "raw_pdf_rel_path": "missing/card.pdf",
        "ocr_md_rel_path": "ocr/two/ocr.md",
        "ocr_md_sha256": "sha256:" + ("0" * 64 if invalid_second_ocr else _sha(two.encode())),
        "ocr_md_chars": len(two),
        "page_count": 1,
    }
    (source / "ocr/one/metadata.json").write_text(json.dumps(metadata_one), encoding="utf-8")
    (source / "ocr/two/metadata.json").write_text(json.dumps(metadata_two), encoding="utf-8")
    entries = [
        {
            "status": "done",
            "cardCompany": "wooricard",
            "doc_version_id": "wooricard:100000:product_description:2025-01-01:v1",
            "productCode": "100000",
            "productName": "슬래시/콜론: 카드",
            "docType": "product_description",
            "beginDt": "2025-01-01",
            "gdccVer": "1",
            "fileNm": "카드.pdf",
            "sourceUrl": "https://example.invalid/card.pdf",
            "sourcePostId": "one",
            "pdf_sha256": _sha(PDF),
            "ocr_remote_rel": "ocr/one/ocr.md",
            "metadata_remote_rel": "ocr/one/metadata.json",
            "pages": 1,
            "ocr_chars": len(one),
            "completed_at": "2026-01-01T00:00:00Z",
        },
        {
            "status": "done",
            "cardCompany": "wooricard",
            "doc_version_id": "wooricard:100000:product_description:2024-01-01:v0/unsafe",
            "productCode": "100000",
            "productName": "옛 카드",
            "docType": "product_description",
            "beginDt": "2024-01-01",
            "gdccVer": "0/unsafe",
            "fileNm": "옛카드.pdf",
            "sourceUrl": "https://example.invalid/old.pdf",
            "sourcePostId": "two",
            "pdf_sha256": f"sha256:{_sha(PDF)}",
            "ocr_remote_rel": "ocr/two/ocr.md",
            "metadata_remote_rel": "ocr/two/metadata.json",
            "pages": 1,
            "ocr_chars": len(two),
            "completed_at": "2025-01-01T00:00:00Z",
        },
    ]
    manifest = source / "master.json"
    manifest.write_text(
        json.dumps({"schema_version": "cardrag_master_manifest.v2", "entries": entries}),
        encoding="utf-8",
    )
    return source, manifest


def test_prepare_is_deterministic_read_only_deduplicated_and_ready_last(tmp_path: Path) -> None:
    source, manifest = _fixture(tmp_path)
    before = {
        path.relative_to(source).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in source.rglob("*")
        if path.is_file()
    }
    for path in source.rglob("*"):
        if path.is_file():
            path.chmod(0o440)

    first = LegacyBundlePreparer(source).prepare(manifest, tmp_path / "bundles-one")
    second = LegacyBundlePreparer(source).prepare(manifest, tmp_path / "bundles-two")

    assert first.manifest.content_sha256 == second.manifest.content_sha256
    assert first.manifest.bundle_id == second.manifest.bundle_id
    assert first.manifest.document_count == 2
    assert first.manifest.unique_pdf_objects == 1
    assert first.manifest.unique_ocr_objects == 2
    assert first.manifest.adopted_count == 1
    assert first.manifest.reocr_count == 1
    bundle = Path(first.bundle_path or "")
    assert (bundle / "READY").is_file()
    verified = verify_bundle(bundle)
    assert verified == first.manifest
    documents = load_bundle_documents(bundle, manifest=verified)
    assert sum(document.is_latest for document in documents) == 1
    assert {document.mapping_method for document in documents} == {
        "direct_path_and_hash",
        "hash_lookup",
    }
    record_paths = [path.as_posix() for path in (bundle / "records").rglob("record.json")]
    assert all("슬래시" not in path and "콜론" not in path for path in record_paths)
    assert any("id-" in path for path in record_paths)
    after = {
        path.relative_to(source).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in source.rglob("*")
        if path.is_file()
    }
    assert before == after


def test_prepare_dry_run_writes_nothing_and_output_inside_source_is_rejected(tmp_path: Path) -> None:
    source, manifest = _fixture(tmp_path)
    output = tmp_path / "dry-run-output"
    result = LegacyBundlePreparer(source).prepare(manifest, output, dry_run=True)
    assert result.dry_run
    assert result.bundle_path is None
    assert not output.exists()

    with pytest.raises(BundleIntegrityError, match="outside"):
        LegacyBundlePreparer(source).prepare(manifest, source / "bundle")


def test_prepare_requires_declared_ocr_hash_for_adoption(tmp_path: Path) -> None:
    source, manifest = _fixture(tmp_path, invalid_second_ocr=False)
    metadata_path = source / "ocr/one/metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.pop("ocr_md_sha256")
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    result = LegacyBundlePreparer(source).prepare(manifest, tmp_path / "bundles")
    documents = load_bundle_documents(Path(result.bundle_path or ""), manifest=result.manifest)
    missing_hash = next(item for item in documents if item.source_post_id == "one")

    assert missing_hash.adoption_status == "reocr"
    assert missing_hash.adoption_reason == "ocr_hash_missing"


def test_prepare_uses_actual_pdf_page_count_for_adoption(tmp_path: Path) -> None:
    source, manifest = _fixture(tmp_path, invalid_second_ocr=False)
    two_page_pdf = synthetic_text_pdf_bytes(["page one", "page two"])
    (source / "raw/card.pdf").write_bytes(two_page_pdf)
    master = json.loads(manifest.read_text(encoding="utf-8"))
    for entry in master["entries"]:
        entry["pdf_sha256"] = _sha(two_page_pdf)
    manifest.write_text(json.dumps(master), encoding="utf-8")

    result = LegacyBundlePreparer(source).prepare(manifest, tmp_path / "bundles")
    documents = load_bundle_documents(Path(result.bundle_path or ""), manifest=result.manifest)

    assert {document.pdf_page_count for document in documents} == {2}
    assert {document.adoption_status for document in documents} == {"reocr"}
    assert {document.adoption_reason for document in documents} == {
        "pdf_page_count_mismatch"
    }


def test_verify_rejects_tampered_object(tmp_path: Path) -> None:
    source, manifest = _fixture(tmp_path)
    result = LegacyBundlePreparer(source).prepare(manifest, tmp_path / "bundles")
    bundle = Path(result.bundle_path or "")
    target = next((bundle / "objects/pdf").rglob("*.pdf"))
    target.chmod(0o640)
    target.write_bytes(PDF + b"tamper")

    with pytest.raises(BundleIntegrityError, match="checksum mismatch"):
        verify_bundle(bundle)


def test_verify_reports_file_and_byte_progress(tmp_path: Path) -> None:
    source, manifest = _fixture(tmp_path)
    result = LegacyBundlePreparer(source).prepare(manifest, tmp_path / "bundles")
    events: list[tuple[int, int, int, int]] = []

    verify_bundle(Path(result.bundle_path or ""), progress=lambda *event: events.append(event))

    assert events
    files, total_files, checked_bytes, total_bytes = events[-1]
    assert files == total_files > 0
    assert checked_bytes == total_bytes > 0
    assert all(0 <= event[2] <= event[3] for event in events)


def test_ready_checksum_rewrite_cannot_hide_tampered_record(tmp_path: Path) -> None:
    source, manifest = _fixture(tmp_path)
    result = LegacyBundlePreparer(source).prepare(manifest, tmp_path / "bundles")
    bundle = Path(result.bundle_path or "")
    record = next((bundle / "records").rglob("record.json"))
    record.chmod(0o640)
    body = json.loads(record.read_text(encoding="utf-8"))
    body["product_name"] = "위조된 이름"
    record.write_text(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    checksum_rows = []
    for path in sorted(bundle.rglob("*")):
        if path.is_file() and path.name not in {"checksums.sha256", "READY"}:
            checksum_rows.append(
                f"{_sha(path.read_bytes())}  {path.relative_to(bundle).as_posix()}\n"
            )
    checksums = bundle / "checksums.sha256"
    checksums.chmod(0o640)
    checksums.write_text("".join(checksum_rows), encoding="utf-8")
    ready_path = bundle / "READY"
    ready_path.chmod(0o640)
    ready = json.loads(ready_path.read_text(encoding="utf-8"))
    ready["checksums_sha256"] = _sha(checksums.read_bytes())
    ready_path.write_text(
        json.dumps(ready, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(BundleIntegrityError, match="record"):
        verify_bundle(bundle)


def test_verify_rejects_unlisted_symlink_directory(tmp_path: Path) -> None:
    source, manifest = _fixture(tmp_path)
    result = LegacyBundlePreparer(source).prepare(manifest, tmp_path / "bundles")
    bundle = Path(result.bundle_path or "")
    bundle.chmod(0o750)
    os.symlink(tmp_path, bundle / "unlisted-link")

    with pytest.raises(BundleIntegrityError, match="symlink"):
        verify_bundle(bundle)


def test_adoption_manifest_is_explicit_and_bound_to_pdf_and_ocr(tmp_path: Path) -> None:
    source, manifest = _fixture(tmp_path, invalid_second_ocr=False)
    result = LegacyBundlePreparer(source).prepare(manifest, tmp_path / "bundles")
    bundle = Path(result.bundle_path or "")
    document = load_bundle_documents(bundle, manifest=result.manifest)[0]
    adoption = legacy_adoption_manifest(result.manifest, document, import_id=uuid.UUID(int=1))

    assert adoption["schema_version"] == "cardrag.legacy-ocr-adoption.v1"
    assert adoption["import_id"] == str(uuid.UUID(int=1))
    assert adoption["attempt"] == {
        "provider": "legacy-import",
        "model": "legacy-unreported",
        "renderer": "legacy-unreported",
        "prompt_version": "legacy-unreported",
        "reasoning_effort": None,
        "render_scale": None,
        "chunk_pages": None,
    }
    # A JSON manifest alone is never sufficient to grant legacy adoption;
    # production also requires a matching PostgreSQL import ledger row.
    assert not ocr_manifest_is_reusable(
        adoption,
        pdf_sha256=document.pdf_sha256,
        ocr_sha256=document.ocr_sha256 or "",
        renderer=PDF_RENDERER_ID,
        reasoning_effort="high",
        render_scale=3.0,
        chunk_pages=2,
        codex_model="gpt-current",
        fallback_model="fallback-current",
    )
    assert not ocr_manifest_is_reusable(
        adoption,
        pdf_sha256="f" * 64,
        ocr_sha256=document.ocr_sha256 or "",
        renderer=PDF_RENDERER_ID,
        reasoning_effort="high",
        render_scale=3.0,
        chunk_pages=2,
        codex_model="gpt-current",
        fallback_model="fallback-current",
    )


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    source, manifest = _fixture(tmp_path)
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(PDF)
    os.symlink(outside, source / "escaped.pdf")

    with pytest.raises(BundleIntegrityError, match="symlink escape"):
        LegacyBundlePreparer(source).prepare(manifest, tmp_path / "bundles", dry_run=True)


def test_internal_legacy_source_symlink_is_also_rejected(tmp_path: Path) -> None:
    source, manifest = _fixture(tmp_path)
    os.symlink(source / "card.pdf", source / "card-alias.pdf")

    with pytest.raises(BundleIntegrityError, match="symlink"):
        LegacyBundlePreparer(source).prepare(manifest, tmp_path / "bundles", dry_run=True)


def test_symlink_source_and_bundle_roots_are_rejected_before_resolve(tmp_path: Path) -> None:
    source, manifest = _fixture(tmp_path)
    source_link = tmp_path / "legacy-link"
    os.symlink(source, source_link)
    with pytest.raises(BundleIntegrityError, match="root.*symlink"):
        LegacyBundlePreparer(source_link)

    result = LegacyBundlePreparer(source).prepare(manifest, tmp_path / "bundles")
    bundle = Path(result.bundle_path or "")
    bundle_link = tmp_path / "bundle-link"
    os.symlink(bundle, bundle_link)
    with pytest.raises(BundleIntegrityError, match="root.*symlink"):
        verify_bundle(bundle_link)


def test_prepare_refuses_output_symlink_into_source(tmp_path: Path) -> None:
    source, manifest = _fixture(tmp_path)
    output_link = tmp_path / "bundle-output-link"
    os.symlink(source, output_link)

    with pytest.raises(BundleIntegrityError, match="output.*symlink"):
        LegacyBundlePreparer(source).prepare(manifest, output_link, dry_run=False)

    assert not any(path.name.startswith("bundle-") for path in source.iterdir())
