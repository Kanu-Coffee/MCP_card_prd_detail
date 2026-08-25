from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from cardrag.cli import app
from cardrag.db import Postgres
from cardrag.legacy.current_inventory import (
    CurrentInventoryExportError,
    canonical_inventory_jsonl,
    export_current_inventory,
    normalize_current_inventory,
    write_current_inventory,
)

PDF_SHA = "a" * 64
OCR_SHA = "b" * 64
MANIFEST_SHA = "c" * 64


def _object_path(root: Path, digest: str, body: bytes) -> Path:
    target = root / "sha256" / digest[:2] / digest
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    return target


def _row(**changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "database_name": "cardrag",
        "generation_id": "gen-20260825T030000Z-012345abcdef",
        "generation_state": "published",
        "manifest_sha256": MANIFEST_SHA,
        "generation_schema_version": "cardrag-generation.v1",
        "generation_root_uri": "generations/gen-20260825T030000Z-012345abcdef",
        "latest_document_count": 1,
        "document_id": "doc_legacy",
        "issuer": "woori",
        "product_code": "CARD-1",
        "document_type": "product_description",
        "effective_date": date(2026, 8, 25),
        "source_version": "v1",
        "pdf_sha256": PDF_SHA,
        "raw_object_key": f"sha256/{PDF_SHA[:2]}/{PDF_SHA}",
        "pdf_size_bytes": 8,
        "pdf_page_count": 1,
        "ocr_sha256": OCR_SHA,
        "ocr_object_key": f"sha256/{OCR_SHA[:2]}/{OCR_SHA}",
        "ocr_pages_type": "array",
        "ocr_page_count": 1,
        "ocr_manifest_type": "object",
    }
    value.update(changes)
    return value


def _storage(tmp_path: Path) -> Path:
    root = tmp_path / "cas"
    root.mkdir()
    _object_path(root, PDF_SHA, b"%PDF-v1\n")
    _object_path(root, OCR_SHA, b"## Page 1\ntext\n")
    return root


class _FakeCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.statements: list[str] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: str) -> None:
        self.statements.append(statement)

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


class _FakeConnection:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.cursor_instance = _FakeCursor(rows)
        self.rollbacks = 0

    def cursor(self) -> _FakeCursor:
        return self.cursor_instance

    def rollback(self) -> None:
        self.rollbacks += 1


class _FakeDatabase:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.connection_instance = _FakeConnection(rows)

    @contextmanager
    def connection(self) -> Iterator[_FakeConnection]:
        yield self.connection_instance


def test_export_uses_read_only_snapshot_and_emits_worker_inventory(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    fake = _FakeDatabase([_row()])

    rows = export_current_inventory(cast(Postgres, fake), storage)

    statements = fake.connection_instance.cursor_instance.statements
    assert statements[0] == "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
    assert "JOIN generation_documents" in statements[1]
    assert "d.is_latest IS TRUE" in statements[1]
    assert fake.connection_instance.rollbacks == 1
    assert rows[0]["source_bundle_id"] == "gen-20260825T030000Z-012345abcdef"
    assert rows[0]["source_bundle_sha256"] == MANIFEST_SHA
    assert rows[0]["source_database_id"].startswith("v0.2.1-postgres:")
    assert rows[0]["source_document_id"] == "doc_legacy"
    assert rows[0]["v1_document_id"].startswith("doc_")
    assert rows[0]["pdf_path"] == str((storage / f"sha256/aa/{PDF_SHA}").resolve())


def test_jsonl_is_canonical_and_output_cannot_touch_cas(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    rows = normalize_current_inventory([_row()], storage)
    body = canonical_inventory_jsonl(rows)
    assert body.endswith(b"\n")
    assert json.loads(body) == rows[0]
    assert (
        body.splitlines()[0]
        == json.dumps(rows[0], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    )

    output = tmp_path / "current-published.jsonl"
    assert write_current_inventory(rows, output, protected_root=storage) == output
    assert output.read_bytes() == body
    assert output.stat().st_mode & 0o777 == 0o600

    with pytest.raises(CurrentInventoryExportError, match="outside CARDRAG_STORAGE_ROOT"):
        write_current_inventory(rows, storage / "inventory.jsonl", protected_root=storage)
    assert not (storage / "inventory.jsonl").exists()


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"ocr_sha256": None}, "invalid ocr_sha256"),
        ({"ocr_pages_type": None}, "incomplete OCR metadata"),
        ({"ocr_page_count": 2}, "page count differs"),
        ({"pdf_size_bytes": 7}, "PDF size differs"),
        ({"generation_state": "ready"}, "not published"),
    ],
)
def test_incomplete_or_inconsistent_published_rows_fail_closed(
    tmp_path: Path, changes: dict[str, Any], message: str
) -> None:
    storage = _storage(tmp_path)
    with pytest.raises(CurrentInventoryExportError, match=message):
        normalize_current_inventory([_row(**changes)], storage)


def test_duplicate_product_fails_closed(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    first = _row(latest_document_count=2)
    second = _row(latest_document_count=2, document_id="doc_other")
    with pytest.raises(CurrentInventoryExportError, match="duplicate product"):
        normalize_current_inventory([first, second], storage)


def test_non_cas_key_and_symlink_escape_fail_closed(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    with pytest.raises(CurrentInventoryExportError, match="unsafe"):
        normalize_current_inventory([_row(raw_object_key="../outside")], storage)

    escaped_sha = "d" * 64
    outside = tmp_path / "outside"
    _object_path(outside, escaped_sha, b"%PDF-v1\n")
    (storage / "sha256" / escaped_sha[:2]).symlink_to(outside / "sha256" / escaped_sha[:2])
    with pytest.raises(CurrentInventoryExportError, match="symlink"):
        normalize_current_inventory(
            [
                _row(
                    pdf_sha256=escaped_sha,
                    raw_object_key=f"sha256/{escaped_sha[:2]}/{escaped_sha}",
                )
            ],
            storage,
        )


def test_cli_exposes_current_inventory_export_command() -> None:
    result = CliRunner().invoke(app, ["legacy", "export-current-inventory", "--help"])
    assert result.exit_code == 0
    assert "--output" in result.stdout
