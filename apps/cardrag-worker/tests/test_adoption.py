from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from cardrag_core import (
    LEGACY_OCR_APPROVED_PREFIX,
    LEGACY_OCR_APPROVED_PREFIX_SHA256,
    LegacyAdoptionReceiptV2,
    canonical_json_bytes,
    canonical_sha256,
    sha256_bytes,
)
from helpers import pdf_bytes

import cardrag_worker.cli as cli_module
from cardrag_worker.adoption import (
    AdoptionError,
    audit_published_adoptions,
    guard_adoption_publication,
    load_inventory,
    load_legacy_prepare_bundle,
    publish_adoptions,
    reconcile_inventories,
    validate_candidate,
    validate_inventory,
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


def v2_inventory_row(root: Path, *, strip_prefix: bool = True) -> dict[str, Any]:
    row = inventory_row(root, issuer="woori", product_code="w1", suffix="v2")
    normalized_path = Path(row["ocr_path"])
    normalized_body = normalized_path.read_bytes()
    if strip_prefix:
        source_path = root / "v2-source.md"
        source_body = LEGACY_OCR_APPROVED_PREFIX + normalized_body
        source_path.write_bytes(source_body)
        profile = "strip-exact-generated-prefix-v1"
        prefix_sha256: str | None = LEGACY_OCR_APPROVED_PREFIX_SHA256
        removed_bytes = len(LEGACY_OCR_APPROVED_PREFIX)
    else:
        source_path = normalized_path
        source_body = normalized_body
        profile = "exact"
        prefix_sha256 = None
        removed_bytes = 0
    pdf_path = Path(row["pdf_path"])
    inventory_sha256 = "2" * 64
    return {
        "schema_version": "cardrag.data-kit-adoption-inventory.v2",
        "policy_version": "cardrag.legacy-ocr-adoption.v2",
        "issuer": row["issuer"],
        "product_code": row["product_code"],
        "source_database_id": f"data-kit-sqlite-v2-sha256:{inventory_sha256}",
        "source_data_pack_manifest_sha256": "1" * 64,
        "source_inventory_sha256": inventory_sha256,
        "source_master_manifest_sha256": "3" * 64,
        "source_ocr_inventory_sha256": inventory_sha256,
        "source_bundle_id": row["source_bundle_id"],
        "source_bundle_sha256": row["source_bundle_sha256"],
        "source_document_id": row["source_document_id"],
        "legacy_source_document_id": row["source_document_id"],
        "document_type": row["document_type"],
        "effective_date": row["effective_date"],
        "source_version": row["source_version"],
        "pdf_path": str(pdf_path.resolve()),
        "source_ocr_path": str(source_path.resolve()),
        "ocr_path": str(normalized_path.resolve()),
        "ledger_pdf_sha256": sha256_bytes(pdf_path.read_bytes()),
        "pdf_size_bytes": pdf_path.stat().st_size,
        "page_count": 1,
        "ledger_ocr_sha256": sha256_bytes(source_body),
        "source_ocr_sha256": sha256_bytes(source_body),
        "source_ocr_size_bytes": len(source_body),
        "normalized_ocr_sha256": sha256_bytes(normalized_body),
        "normalized_ocr_size_bytes": len(normalized_body),
        "normalization_profile": profile,
        "prefix_sha256": prefix_sha256,
        "removed_bytes": removed_bytes,
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
        async def exists(self, _path: object) -> bool:
            return False

        async def close(self) -> None:
            return None

    async def publish(accepted: Any, client: Any) -> int:
        assert len(accepted.receipts) == 1
        return 1

    monkeypatch.setattr(
        cli_module.WorkerSettings,
        "from_env",
        lambda **kwargs: type("Settings", (), {"ocr_cache_mode": "read-write"})(),
    )
    monkeypatch.setattr(cli_module.WebDAVClient, "from_env", lambda: Client())
    monkeypatch.setattr(cli_module, "publish_adoptions", publish)
    assert await cli_module._publish_if_requested(result, True) == 1


@pytest.mark.asyncio
async def test_read_only_cache_mode_rejects_adoption_before_webdav_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = validate_inventory([inventory_row(tmp_path)], source_kind="current")
    monkeypatch.setattr(
        cli_module.WorkerSettings,
        "from_env",
        lambda **kwargs: type("Settings", (), {"ocr_cache_mode": "read-only"})(),
    )

    def webdav_must_not_be_constructed() -> None:
        raise AssertionError("read-only adoption must fail before WebDAV construction")

    monkeypatch.setattr(cli_module.WebDAVClient, "from_env", webdav_must_not_be_constructed)
    with pytest.raises(ValueError, match="OCR_CACHE_MODE=read-write"):
        await cli_module._publish_if_requested(result, True)


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
        async def exists(self, _path: object) -> bool:
            return False

        async def close(self) -> None:
            return None

    async def publish(accepted: Any, client: Any) -> int:
        assert len(accepted.receipts) == 1
        assert len(accepted.errors) == 1
        return 1

    monkeypatch.setattr(
        cli_module.WorkerSettings,
        "from_env",
        lambda **kwargs: type("Settings", (), {"ocr_cache_mode": "read-write"})(),
    )
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


def test_v2_prefix_strip_keeps_source_and_normalized_lineage_separate(tmp_path: Path) -> None:
    row = v2_inventory_row(tmp_path)
    accepted = validate_candidate(row, source_kind="legacy")
    receipt = LegacyAdoptionReceiptV2.model_validate(accepted["receipt"])
    assert receipt.source_ocr_sha256 == row["source_ocr_sha256"]
    assert receipt.normalized_ocr_sha256 == row["normalized_ocr_sha256"]
    assert receipt.source_ocr_sha256 != receipt.normalized_ocr_sha256
    assert receipt.prefix_sha256 == LEGACY_OCR_APPROVED_PREFIX_SHA256
    assert accepted["manifest"]["schema_version"] == "cardrag.ocr-artifact.v2"
    assert accepted["manifest"]["output"]["sha256"] == row["normalized_ocr_sha256"]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("ledger_ocr_sha256", "0" * 64, "original ledger hash"),
        ("normalized_ocr_sha256", "0" * 64, "normalized OCR bytes"),
        ("prefix_sha256", "0" * 64, "approved exact 24-byte prefix"),
        ("removed_bytes", 23, "approved exact 24-byte prefix"),
        ("normalization_profile", "arbitrary-rewrite", "unsupported normalization"),
    ),
)
def test_v2_adoption_rejects_hash_and_transformation_tamper(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    row = v2_inventory_row(tmp_path)
    row[field] = value
    with pytest.raises(AdoptionError, match=message):
        validate_candidate(row, source_kind="legacy")


def test_v2_adoption_rejects_non_exact_source_bytes(tmp_path: Path) -> None:
    row = v2_inventory_row(tmp_path)
    source_path = Path(row["source_ocr_path"])
    forged = b"X" * len(LEGACY_OCR_APPROVED_PREFIX) + Path(row["ocr_path"]).read_bytes()
    source_path.write_bytes(forged)
    row["ledger_ocr_sha256"] = sha256_bytes(forged)
    row["source_ocr_sha256"] = sha256_bytes(forged)
    with pytest.raises(AdoptionError, match="exactly approved-prefix"):
        validate_candidate(row, source_kind="legacy")


@pytest.mark.asyncio
async def test_v2_source_tamper_after_validation_fails_before_webdav_write(tmp_path: Path) -> None:
    row = v2_inventory_row(tmp_path)
    result = validate_inventory([row], source_kind="legacy")
    assert len(result.receipts) == 1
    Path(row["source_ocr_path"]).write_bytes(b"tampered after validation")

    class NoWriteClient:
        async def put_cas(self, *_args: Any, **_kwargs: Any) -> tuple[str, int]:
            raise AssertionError("WebDAV must not be called after source-lineage tamper")

        async def put_json(self, *_args: Any, **_kwargs: Any) -> bytes:
            raise AssertionError("WebDAV must not be called after source-lineage tamper")

    with pytest.raises(AdoptionError, match="original source OCR bytes"):
        await publish_adoptions(result, NoWriteClient())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_v2_later_source_tamper_preflights_all_rows_before_webdav_write(tmp_path: Path) -> None:
    exact_root = tmp_path / "exact"
    strip_root = tmp_path / "strip"
    exact_root.mkdir()
    strip_root.mkdir()
    exact = v2_inventory_row(exact_root, strip_prefix=False)
    strip = v2_inventory_row(strip_root)
    strip["product_code"] = "w2"
    result = validate_inventory([exact, strip], source_kind="legacy")
    assert len(result.receipts) == 2
    Path(result.receipts[-1]["source_ocr_path"]).write_bytes(b"tampered after validation")

    class NoWriteClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def put_cas(self, *_args: Any, **_kwargs: Any) -> tuple[str, int]:
            self.calls.append("put_cas")
            return "", 0

        async def put_json(self, *_args: Any, **_kwargs: Any) -> bytes:
            self.calls.append("put_json")
            return b""

    client = NoWriteClient()
    with pytest.raises(AdoptionError, match="original source OCR bytes"):
        await publish_adoptions(result, client)  # type: ignore[arg-type]
    assert client.calls == []


@pytest.mark.asyncio
async def test_v2_exact_and_prefix_strip_publish_canonical_controls(tmp_path: Path) -> None:
    exact_root = tmp_path / "exact"
    strip_root = tmp_path / "strip"
    exact_root.mkdir()
    strip_root.mkdir()
    exact = v2_inventory_row(exact_root, strip_prefix=False)
    strip = v2_inventory_row(strip_root)
    strip["product_code"] = "w2"
    result = validate_inventory(
        [exact, strip],
        source_kind="legacy",
    )
    assert len(result.receipts) == 2

    class Client:
        def __init__(self) -> None:
            self.controls: list[bytes] = []

        async def exists(self, _path: object) -> bool:
            return False

        async def put_cas(self, body: bytes, *, media_type: str) -> tuple[str, str]:
            assert media_type == "text/markdown; charset=utf-8"
            digest = sha256_bytes(body)
            return digest, f"v1/objects/sha256/{digest[:2]}/{digest}"

        async def put_json(
            self,
            _path: object,
            payload: dict[str, Any],
            *,
            immutable: bool,
        ) -> bytes:
            assert immutable is True
            body = canonical_json_bytes(payload)
            self.controls.append(body)
            return body

    client = Client()
    assert await publish_adoptions(result, client) == 2  # type: ignore[arg-type]
    manifests = [json.loads(body) for body in client.controls if b'"cardrag.ocr-artifact.v2"' in body]
    assert len(manifests) == 2
    assert {manifest["receipt"]["normalization_profile"] for manifest in manifests} == {
        "exact",
        "strip-exact-generated-prefix-v1",
    }


@pytest.mark.asyncio
async def test_v2_candidate_error_blocks_partial_cli_publication(tmp_path: Path) -> None:
    valid = v2_inventory_row(tmp_path)
    invalid = dict(valid)
    invalid["normalized_ocr_sha256"] = "0" * 64
    result = validate_inventory([valid, invalid], source_kind="legacy")
    assert len(result.receipts) == 1
    assert result.errors[0]["policy_version"] == "cardrag.legacy-ocr-adoption.v2"
    with pytest.raises(ValueError, match="partial publication"):
        await cli_module._publish_if_requested(result, True)


def test_bare_v2_jsonl_is_rejected_without_its_sealed_export(tmp_path: Path) -> None:
    row = v2_inventory_row(tmp_path)
    inventory = tmp_path / "bare-v2.jsonl"
    inventory.write_bytes(canonical_json_bytes(row) + b"\n")
    with pytest.raises(AdoptionError, match="sealed export directory"):
        load_inventory(inventory)


@pytest.mark.asyncio
async def test_adoption_guard_uses_existence_only_and_blocks_existing_stable_pointer() -> None:
    class ExistenceOnlyClient:
        def __init__(self) -> None:
            self.checked: list[object] = []

        async def exists(self, path: object) -> bool:
            self.checked.append(path)
            return True

        async def get_bytes(self, *_args: Any, **_kwargs: Any) -> bytes | None:
            raise AssertionError("the stable pointer body must not be read")

        async def put_cas(self, *_args: Any, **_kwargs: Any) -> tuple[str, str]:
            raise AssertionError("the guard must not write")

        async def put_json(self, *_args: Any, **_kwargs: Any) -> bytes:
            raise AssertionError("the guard must not write")

    client = ExistenceOnlyClient()
    with pytest.raises(AdoptionError, match="stable generation pointer already exists"):
        await guard_adoption_publication(client)  # type: ignore[arg-type]
    assert [str(path) for path in client.checked] == ["v1/channels/stable.json"]


@pytest.mark.asyncio
async def test_cli_publication_guard_runs_before_any_adoption_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = validate_inventory([v2_inventory_row(tmp_path)], source_kind="legacy")

    class Client:
        closed = False

        async def exists(self, _path: object) -> bool:
            return True

        async def close(self) -> None:
            self.closed = True

    client = Client()

    async def unexpected_publish(*_args: Any, **_kwargs: Any) -> int:
        raise AssertionError("publication must not start when stable.json exists")

    monkeypatch.setattr(
        cli_module.WorkerSettings,
        "from_env",
        lambda **kwargs: type("Settings", (), {"ocr_cache_mode": "read-write"})(),
    )
    monkeypatch.setattr(cli_module.WebDAVClient, "from_env", lambda: client)
    monkeypatch.setattr(cli_module, "publish_adoptions", unexpected_publish)
    with pytest.raises(AdoptionError, match="stable generation pointer already exists"):
        await cli_module._publish_if_requested(result, True)
    assert client.closed is True


@pytest.mark.asyncio
async def test_publication_rechecks_stable_pointer_after_full_preflight_before_first_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = validate_inventory([v2_inventory_row(tmp_path)], source_kind="legacy")

    class FlipClient:
        def __init__(self) -> None:
            self.existence_checks = 0
            self.writes: list[str] = []

        async def exists(self, _path: object) -> bool:
            self.existence_checks += 1
            return self.existence_checks == 2

        async def put_cas(self, *_args: Any, **_kwargs: Any) -> tuple[str, str]:
            self.writes.append("put_cas")
            return "", ""

        async def put_json(self, *_args: Any, **_kwargs: Any) -> bytes:
            self.writes.append("put_json")
            return b""

        async def close(self) -> None:
            return None

    client = FlipClient()
    monkeypatch.setattr(
        cli_module.WorkerSettings,
        "from_env",
        lambda **kwargs: type("Settings", (), {"ocr_cache_mode": "read-write"})(),
    )
    monkeypatch.setattr(cli_module.WebDAVClient, "from_env", lambda: client)

    with pytest.raises(AdoptionError, match="stable generation pointer already exists"):
        await cli_module._publish_if_requested(result, True)
    assert client.existence_checks == 2
    assert client.writes == []


class _MemoryAdoptionWebDAV:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.reads: list[tuple[str, int | None]] = []
        self.write_count = 0

    async def exists(self, _path: object) -> bool:
        return False

    async def put_cas(self, body: bytes, *, media_type: str) -> tuple[str, str]:
        assert media_type == "text/markdown; charset=utf-8"
        digest = sha256_bytes(body)
        path = f"v1/objects/sha256/{digest[:2]}/{digest}"
        self.objects[path] = body
        self.write_count += 1
        return digest, path

    async def put_json(
        self,
        path: object,
        payload: dict[str, Any],
        *,
        immutable: bool,
    ) -> bytes:
        assert immutable is True
        body = canonical_json_bytes(payload)
        self.objects[str(path)] = body
        self.write_count += 1
        return body

    async def get_bytes(
        self,
        path: str,
        *,
        max_bytes: int | None = None,
    ) -> bytes | None:
        self.reads.append((str(path), max_bytes))
        return self.objects.get(str(path))


@pytest.mark.asyncio
async def test_v2_post_publish_audit_reads_every_exact_control_and_cas_object(tmp_path: Path) -> None:
    result = validate_inventory([v2_inventory_row(tmp_path)], source_kind="legacy")
    client = _MemoryAdoptionWebDAV()
    assert await publish_adoptions(result, client) == 1  # type: ignore[arg-type]
    writes_before_audit = client.write_count

    assert await audit_published_adoptions(result, client) == 1  # type: ignore[arg-type]
    assert client.write_count == writes_before_audit
    assert len(client.reads) == 3
    assert {path.rsplit("/", 1)[-1] for path, _limit in client.reads} == {
        "manifest.json",
        "READY.json",
        next(path.rsplit("/", 1)[-1] for path in client.objects if "/objects/sha256/" in path),
    }
    for path, limit in client.reads:
        assert limit == len(client.objects[path])


@pytest.mark.parametrize("tampered_suffix", ("manifest.json", "READY.json", "cas"))
@pytest.mark.asyncio
async def test_v2_post_publish_audit_rejects_any_remote_byte_difference(
    tmp_path: Path,
    tampered_suffix: str,
) -> None:
    result = validate_inventory([v2_inventory_row(tmp_path)], source_kind="legacy")
    client = _MemoryAdoptionWebDAV()
    assert await publish_adoptions(result, client) == 1  # type: ignore[arg-type]
    if tampered_suffix == "cas":
        path = next(path for path in client.objects if "/objects/sha256/" in path)
    else:
        path = next(path for path in client.objects if path.endswith(tampered_suffix))
    client.objects[path] += b" "
    writes_before_audit = client.write_count

    with pytest.raises(AdoptionError, match="bytes, SHA-256, or size differ"):
        await audit_published_adoptions(result, client)  # type: ignore[arg-type]
    assert client.write_count == writes_before_audit
