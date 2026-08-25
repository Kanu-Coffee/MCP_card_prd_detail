from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from cardrag.cli import app
from cardrag.db import Postgres
from cardrag.legacy.adoption_ledger import (
    ADOPTION_LEDGER_SCHEMA,
    AdoptionLedgerExportError,
    canonical_adoption_ledger_jsonl,
    export_adoption_ledger,
    normalize_adoption_ledger,
    write_adoption_ledger,
)

IMPORT_ID = uuid.UUID("01234567-89ab-cdef-8123-456789abcdef")
BUNDLE_SHA = "1" * 64
PDF_SHA = "2" * 64
OCR_SHA = "3" * 64


def _row(**changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "database_name": "cardrag",
        "import_id": IMPORT_ID,
        "bundle_id": f"bundle-{BUNDLE_SHA[:12]}",
        "bundle_sha256": BUNDLE_SHA,
        "generation_id": "legacy_woori_0123456789abcdef",
        "import_state": "succeeded",
        "import_phase": "published",
        "source_document_id": "legacy-document-1",
        "document_key": "woori:CARD-1:product_description:2026-08-25:v1",
        "issuer": "woori",
        "pdf_sha256": PDF_SHA,
        "ocr_sha256": OCR_SHA,
        "disposition": "adopted",
        "document_state": "succeeded",
    }
    value.update(changes)
    return value


class _FakeCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.statements: list[tuple[str, tuple[object, ...] | None]] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: str, parameters: tuple[object, ...] | None = None) -> None:
        self.statements.append((statement, parameters))

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


def test_export_uses_repeatable_read_only_snapshot_and_exact_join() -> None:
    fake = _FakeDatabase([_row()])

    rows = export_adoption_ledger(cast(Postgres, fake), import_id=IMPORT_ID)

    statements = fake.connection_instance.cursor_instance.statements
    assert statements[0] == (
        "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY",
        None,
    )
    query, parameters = statements[1]
    assert "JOIN legacy_import_documents" in query
    assert "d.disposition = 'adopted'" in query
    assert "i.state = 'succeeded'" in query
    assert "i.import_id = %s" in query
    assert parameters == (IMPORT_ID,)
    assert fake.connection_instance.rollbacks == 1
    assert rows[0]["schema_version"] == ADOPTION_LEDGER_SCHEMA
    assert rows[0]["import_id"] == str(IMPORT_ID)
    assert rows[0]["source_database_id"].startswith("v0.2.1-postgres:")
    assert rows[0]["status"] == "succeeded"
    assert rows[0]["disposition"] == "adopted"


def test_multiple_succeeded_imports_require_explicit_import_id() -> None:
    other = uuid.UUID("11234567-89ab-cdef-8123-456789abcdef")
    with pytest.raises(AdoptionLedgerExportError, match="--import-id"):
        normalize_adoption_ledger([_row(), _row(import_id=other)])


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"import_state": "processing"}, "not succeeded"),
        ({"import_phase": "awaiting_finalize"}, "published terminal"),
        ({"document_state": "failed"}, "document is not succeeded"),
        ({"disposition": "reocr"}, "document is not succeeded"),
        ({"ocr_sha256": None}, "invalid ocr_sha256"),
        ({"bundle_id": "bundle-ffffffffffff"}, "does not match"),
        ({"issuer": "WOORI"}, "invalid issuer"),
    ],
)
def test_non_terminal_or_unbound_rows_fail_closed(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(AdoptionLedgerExportError, match=message):
        normalize_adoption_ledger([_row(**changes)], expected_import_id=IMPORT_ID)


@pytest.mark.parametrize("duplicate_field", ["source_document_id", "document_key"])
def test_duplicate_document_identity_fails_closed(duplicate_field: str) -> None:
    first = _row()
    second = _row(
        source_document_id="legacy-document-2",
        document_key="woori:CARD-2:product_description:2026-08-25:v1",
        pdf_sha256="4" * 64,
        ocr_sha256="5" * 64,
    )
    second[duplicate_field] = first[duplicate_field]
    with pytest.raises(AdoptionLedgerExportError, match="duplicate"):
        normalize_adoption_ledger([first, second], expected_import_id=IMPORT_ID)


def test_jsonl_is_sorted_canonical_and_new_output_cannot_touch_cas(tmp_path: Path) -> None:
    storage = tmp_path / "cas"
    storage.mkdir()
    source_marker = storage / "source-object"
    source_marker.write_bytes(b"immutable source")
    rows = normalize_adoption_ledger(
        [
            _row(
                source_document_id="legacy-document-2",
                document_key="woori:CARD-2:product_description:2026-08-25:v1",
            ),
            _row(),
        ],
        expected_import_id=IMPORT_ID,
    )
    body = canonical_adoption_ledger_jsonl(reversed(rows))  # type: ignore[arg-type]
    decoded = [json.loads(line) for line in body.splitlines()]
    assert [row["source_document_id"] for row in decoded] == [
        "legacy-document-1",
        "legacy-document-2",
    ]
    assert (
        body.splitlines()[0]
        == json.dumps(decoded[0], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    )

    output = tmp_path / "legacy-ledger.jsonl"
    assert write_adoption_ledger(rows, output, protected_root=storage) == output
    assert output.read_bytes() == canonical_adoption_ledger_jsonl(rows)
    assert output.stat().st_mode & 0o777 == 0o600
    assert source_marker.read_bytes() == b"immutable source"
    with pytest.raises(AdoptionLedgerExportError, match="already exists"):
        write_adoption_ledger(rows, output, protected_root=storage)
    with pytest.raises(AdoptionLedgerExportError, match="outside CARDRAG_STORAGE_ROOT"):
        write_adoption_ledger(rows, storage / "ledger.jsonl", protected_root=storage)
    assert not (storage / "ledger.jsonl").exists()


def test_relative_output_and_missing_adopted_rows_fail_closed(tmp_path: Path) -> None:
    storage = tmp_path / "cas"
    storage.mkdir()
    with pytest.raises(AdoptionLedgerExportError, match="must be absolute"):
        write_adoption_ledger([], Path("legacy-ledger.jsonl"), protected_root=storage)
    with pytest.raises(AdoptionLedgerExportError, match="invalid source_document_id"):
        normalize_adoption_ledger([_row(source_document_id=None)], expected_import_id=IMPORT_ID)


def test_cli_exposes_adoption_ledger_export_command() -> None:
    result = CliRunner().invoke(app, ["legacy", "export-adoption-ledger", "--help"])
    assert result.exit_code == 0
    assert "--output" in result.stdout
    assert "--import-id" in result.stdout
