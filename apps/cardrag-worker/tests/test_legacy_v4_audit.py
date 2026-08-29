from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest
from cardrag_core import canonical_sha256

from cardrag_worker import legacy_v4_audit
from cardrag_worker.legacy_v4_audit import (
    ALGORITHM_VERSION,
    LegacyV4AuditError,
    audit_database,
    load_audit_artifact,
    validate_audit_artifact,
)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _create_v4_database(path: Path) -> str:
    page_text = (
        "# Alpha\n"
        "prefix body\n"
        "continued chunk\n"
        "## Later\n"
        "| A | B |\n"
        "| --- | --- |\n"
        "| one | two |\n"
        "end\r\n"
        "next"
    )
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE documents(document_id TEXT PRIMARY KEY);
        CREATE TABLE pages(
            document_id TEXT NOT NULL,
            page INTEGER NOT NULL,
            text TEXT NOT NULL,
            text_sha256 TEXT NOT NULL
        );
        CREATE TABLE evidence(
            evidence_pk INTEGER PRIMARY KEY,
            evidence_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            page_start INTEGER NOT NULL,
            page_end INTEGER NOT NULL,
            section_type TEXT NOT NULL,
            text TEXT NOT NULL,
            source_start INTEGER NOT NULL,
            source_end INTEGER NOT NULL
        );
        """
    )
    identity = "a" * 64
    connection.executemany(
        "INSERT INTO metadata(key,value) VALUES (?,?)",
        (
            ("schema_id", "cardrag.serving-db.v4"),
            ("generation_id", "g-fixture"),
            ("corpus_sha256", identity),
            ("contract_sha256", identity),
        ),
    )
    connection.execute("INSERT INTO documents(document_id) VALUES ('doc-1')")
    connection.execute(
        "INSERT INTO pages(document_id,page,text,text_sha256) VALUES ('doc-1',1,?,?)",
        (page_text, _sha256(page_text)),
    )

    prefix_start = page_text.index("prefix")
    continued_start = page_text.index("continued")
    table_start = page_text.index("| A | B |")
    separator_start = page_text.index("| --- | --- |")
    table_body_start = page_text.index("| one | two |")
    end_start = page_text.index("end")
    next_start = page_text.index("next")
    spans = (
        ("heading", 0, prefix_start),
        # Starts within a line: continuation, mid-line, and titleless.
        ("body", prefix_start + 2, continued_start),
        # Starts after LF and contains a later heading: titled-body but not titleless.
        ("body", continued_start, table_start),
        # No one evidence span encloses the complete header+separator+body table.
        ("body", table_start, separator_start),
        ("body", separator_start, table_body_start),
        ("body", table_body_start, end_start),
        # Starts after CRLF: continuation but explicitly not mid-line.
        ("body", next_start, len(page_text)),
    )
    for evidence_pk, (section_type, start, end) in enumerate(spans, start=1):
        connection.execute(
            """INSERT INTO evidence(
                   evidence_pk,evidence_id,document_id,page_start,page_end,section_type,
                   text,source_start,source_end
               ) VALUES (?,?,?,1,1,?,?,?,?)""",
            (
                evidence_pk,
                f"e-{evidence_pk}",
                "doc-1",
                section_type,
                page_text[start:end],
                start,
                end,
            ),
        )
    connection.commit()
    connection.close()
    return page_text


def _reseal(payload: dict[str, object]) -> dict[str, object]:
    unsigned = {key: value for key, value in payload.items() if key != "evidence_sha256"}
    return {**unsigned, "evidence_sha256": canonical_sha256(unsigned)}


def test_audit_seals_explicit_chunk_and_markdown_table_algorithms(tmp_path: Path) -> None:
    database = tmp_path / "index.sqlite3"
    _create_v4_database(database)

    artifact = audit_database(database)

    assert artifact["algorithm_version"] == ALGORITHM_VERSION
    assert artifact["source_database"]["sha256"] == hashlib.sha256(database.read_bytes()).hexdigest()
    assert artifact["source_database"]["size_bytes"] == database.stat().st_size
    assert artifact["corpus_counts"] == {
        "body_chunks": 6,
        "canonical_markdown_tables": 1,
        "documents": 1,
        "evidence_chunks": 7,
        "heading_chunks": 1,
        "pages": 1,
    }
    observed = artifact["comparison_to_historical_run"]["observed"]
    assert observed["continuation_chunks"] == {
        "denominator": 7,
        "numerator": 6,
        "percent_4dp": "85.7143",
    }
    assert observed["mid_line_continuations"]["numerator"] == 1
    assert observed["titled_body_chunks"]["numerator"] == 1
    assert observed["titleless_continuations"]["numerator"] == 5
    assert observed["fragmented_markdown_tables"] == {
        "denominator": 1,
        "numerator": 1,
        "percent_4dp": "100.0000",
    }
    assert validate_audit_artifact(artifact) == artifact


def test_audit_rejects_symlinks_and_database_identity_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "index.sqlite3"
    _create_v4_database(database)
    link = tmp_path / "database-link"
    link.symlink_to(database)
    with pytest.raises(LegacyV4AuditError):
        audit_database(link)

    original_collect = legacy_v4_audit._collect

    def mutate_after_collect(connection: sqlite3.Connection) -> object:
        result = original_collect(connection)
        descriptor = os.open(database, os.O_WRONLY | os.O_APPEND)
        try:
            os.write(descriptor, b"tamper")
        finally:
            os.close(descriptor)
        return result

    monkeypatch.setattr(legacy_v4_audit, "_collect", mutate_after_collect)
    with pytest.raises(LegacyV4AuditError, match="changed during"):
        audit_database(database)


def test_artifact_loader_and_validator_reject_tamper_and_resealed_claims(tmp_path: Path) -> None:
    database = tmp_path / "index.sqlite3"
    _create_v4_database(database)
    artifact = audit_database(database)
    artifact_path = tmp_path / "audit.json"
    artifact_path.write_bytes(legacy_v4_audit.canonical_json_bytes(artifact) + b"\n")

    assert load_audit_artifact(artifact_path) == artifact
    link = tmp_path / "artifact-link"
    link.symlink_to(artifact_path)
    with pytest.raises(LegacyV4AuditError):
        load_audit_artifact(link)

    tampered = json.loads(json.dumps(artifact))
    tampered["comparison_to_historical_run"]["match"] = True
    with pytest.raises(LegacyV4AuditError, match="self-hash"):
        validate_audit_artifact(tampered)

    resealed = _reseal(tampered)
    with pytest.raises(LegacyV4AuditError, match="comparison result"):
        validate_audit_artifact(resealed)

    percentage_tamper = json.loads(json.dumps(artifact))
    percentage_tamper["comparison_to_historical_run"]["observed"]["continuation_chunks"]["percent_4dp"] = (
        "0.0000"
    )
    with pytest.raises(LegacyV4AuditError, match="percentage"):
        validate_audit_artifact(_reseal(percentage_tamper))
