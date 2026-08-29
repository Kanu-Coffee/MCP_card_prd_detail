"""Read-only, reproducible audit of legacy v4 chunk-boundary defects.

This module never opens WebDAV and never writes the source database.  It is
also an independent CLI::

    python -m cardrag_worker.legacy_v4_audit --database /absolute/index.sqlite3
"""

from __future__ import annotations

import argparse
import ast
import base64
import binascii
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Final, cast

from cardrag_core import canonical_json_bytes, canonical_sha256

AUDIT_SCHEMA: Final = "cardrag.v109-sealed-v4-structure-reaudit.v1"
HISTORICAL_SCHEMA: Final = "cardrag.v109-historical-structure-observation.v1"
HISTORICAL_SOURCE_SCHEMA: Final = "cardrag.v109-historical-structure-audit-execution.v1"
ALGORITHM_VERSION: Final = "cardrag.v109-sealed-v4-structure-audit.v1"
HISTORICAL_ALGORITHM_VERSION: Final = "cardrag.v109-worker-run-structure-audit.v1"
CANONICAL_HEADING_PATTERN: Final = r"(?m)^#{1,6}[ \t]+\S"
_HEADING = re.compile(CANONICAL_HEADING_PATTERN)
_TABLE_SEPARATOR_CELL = re.compile(r"^:?-{3,}:?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GENERATION_ID = re.compile(r"^g-[A-Za-z0-9._-]{1,126}$")
_MAX_DATABASE_BYTES: Final = 2 * 1024 * 1024 * 1024
_MAX_ARTIFACT_BYTES: Final = 2 * 1024 * 1024
_HISTORICAL_SESSION_BASENAME: Final = "rollout-2026-08-29T13-25-25-01a04bc3-eace-7960-aefd-3c7a8f7b7b84.jsonl"
_HISTORICAL_SESSION_SHA256: Final = "59652d065795fea554863b34c41c2a4e3330fde7948287f142a1857718c7754e"
_HISTORICAL_RECORD_SHA256: Final = "b253ffb58eacf8e38d59575cf7d23acee462701a32176ecd5ffdd16c3ce45095"
_HISTORICAL_COMMAND_SHA256: Final = "a39bf5c05028ddd7ecde1915fa84736747a9e2573e87590654adbbf45a8a327a"
_HISTORICAL_STDOUT_SHA256: Final = "ef683584c5699139fa81dba888ac1bd9b84cd02af5353ced421fc0aa582c8ff1"
_HISTORICAL_RECORD_SIZE: Final = 10_164
_HISTORICAL_RECORD_LINE: Final = 207

METRIC_NAMES: Final = (
    "continuation_chunks",
    "mid_line_continuations",
    "titled_body_chunks",
    "titleless_continuations",
    "fragmented_markdown_tables",
)

HISTORICAL_KB_METRICS: Final = {
    "continuation_chunks": (1379, 4175),
    "mid_line_continuations": (1293, 1379),
    "titled_body_chunks": (1467, 3710),
    "titleless_continuations": (389, 1379),
    # Historical count projected onto the sealed algorithm's canonical-table denominator.
    # The historical artifact separately preserves its original 3,065-block denominator.
    "fragmented_markdown_tables": (45, 2779),
}
SEALED_RELEASE_METRICS: Final = {
    "continuation_chunks": (1379, 4175),
    "mid_line_continuations": (1293, 1379),
    "titled_body_chunks": (1467, 3710),
    "titleless_continuations": (388, 1379),
    "fragmented_markdown_tables": (45, 2779),
}
RELEASE_SOURCE_DATABASE: Final = {
    "contract_sha256": "65b4f44212114f34641f38c30221acfbd903701b3e4097883c9dc6017940dece",
    "corpus_sha256": "d11f80f9af71b98f675510529d8660da41786dedb220917180379120ab9170ab",
    "generation_id": "g-2208f0c6076649c4be915be1-d11f80f9af71",
    "schema_id": "cardrag.serving-db.v4",
    "sha256": "d25be45bc5d39af6561e587635b08312913107b6f6416500da39ab9eb757d38f",
    "size_bytes": 58_466_304,
}


class LegacyV4AuditError(RuntimeError):
    """A bounded failure that never includes a source path or database text."""


def sealed_algorithm_payload() -> dict[str, Any]:
    return {
        "canonical_heading_regex": CANONICAL_HEADING_PATTERN,
        "fragmented_markdown_table": (
            "maximal consecutive table-line block with a non-separator header, separator as row 2, "
            "and >=1 non-separator body row; fragmented iff no page-local evidence half-open span "
            "encloses the complete block"
        ),
        "line_offsets": "str.splitlines(keepends=True), page-local half-open character offsets",
        "mid_line_continuation": ("source_start>0 and page_text[source_start-1] is neither CR nor LF"),
        "table_line": "line.strip().count('|')>=2",
        "table_separator_cell_regex": _TABLE_SEPARATOR_CELL.pattern,
        "titled_body": ("section_type=='body' and a canonical heading begins in [source_start,source_end)"),
        "titleless_continuation": (
            "section_type=='body', source_start>0, a canonical heading begins before source_start, "
            "and no canonical heading begins in [source_start,source_end)"
        ),
        "version": ALGORITHM_VERSION,
    }


def historical_algorithm_payload() -> dict[str, Any]:
    return {
        "heading_regex": r"(?m)^#{1,6}\s",
        "line_offsets": "page-local half-open character offsets",
        "table_block": ("maximal consecutive lines whose trailing-LF-stripped text starts and ends with '|'"),
        "version": HISTORICAL_ALGORITHM_VERSION,
    }


def _percent(numerator: int, denominator: int) -> str:
    if denominator < 1 or not 0 <= numerator <= denominator:
        raise LegacyV4AuditError("audit metric numerator or denominator is invalid")
    value = (Decimal(numerator) * Decimal(100) / Decimal(denominator)).quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_UP
    )
    return format(value, ".4f")


def _measurement(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "denominator": denominator,
        "numerator": numerator,
        "percent_4dp": _percent(numerator, denominator),
    }


def _hash_fd(fd: int, size: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while offset < size:
        block = os.pread(fd, min(1024 * 1024, size - offset), offset)
        if not block:
            raise LegacyV4AuditError("source database ended before its declared size")
        digest.update(block)
        offset += len(block)
    if os.pread(fd, 1, size):
        raise LegacyV4AuditError("source database grew during hashing")
    return digest.hexdigest()


def _open_nofollow(path: Path) -> int:
    raw_path = os.fspath(path)
    if not os.path.isabs(raw_path) or "\x00" in raw_path:
        raise LegacyV4AuditError("audit input path must be absolute")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise LegacyV4AuditError("this platform cannot enforce O_NOFOLLOW")
    try:
        descriptor = os.open(raw_path, os.O_RDONLY | os.O_CLOEXEC | nofollow)
    except OSError:
        raise LegacyV4AuditError("audit input cannot be opened read-only without following links") from None
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        os.close(descriptor)
        raise LegacyV4AuditError("audit input is not a regular file")
    if not 1 <= info.st_size <= _MAX_DATABASE_BYTES:
        os.close(descriptor)
        raise LegacyV4AuditError("audit input size is outside the bounded range")
    return descriptor


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _table_columns(connection: sqlite3.Connection, table: str) -> frozenset[str]:
    return frozenset(str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")'))


def _validate_schema(connection: sqlite3.Connection) -> dict[str, str]:
    required = {
        "metadata": {"key", "value"},
        "documents": {"document_id"},
        "pages": {"document_id", "page", "text", "text_sha256"},
        "evidence": {
            "evidence_pk",
            "evidence_id",
            "document_id",
            "page_start",
            "page_end",
            "section_type",
            "text",
            "source_start",
            "source_end",
        },
    }
    for table, columns in required.items():
        if not columns <= _table_columns(connection, table):
            raise LegacyV4AuditError("source database has an incompatible v4 schema")
    metadata = {str(row[0]): str(row[1]) for row in connection.execute("SELECT key,value FROM metadata")}
    for key in ("schema_id", "generation_id", "corpus_sha256", "contract_sha256"):
        if key not in metadata:
            raise LegacyV4AuditError("source database metadata is incomplete")
    if metadata["schema_id"] != "cardrag.serving-db.v4":
        raise LegacyV4AuditError("source database is not serving schema v4")
    if _GENERATION_ID.fullmatch(metadata["generation_id"]) is None:
        raise LegacyV4AuditError("source database generation identity is invalid")
    if any(_SHA256.fullmatch(metadata[key]) is None for key in ("corpus_sha256", "contract_sha256")):
        raise LegacyV4AuditError("source database content identity is invalid")
    return metadata


def _source_lines(text: str) -> tuple[tuple[int, int, str], ...]:
    rows: list[tuple[int, int, str]] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        end = offset + len(line)
        rows.append((offset, end, line))
        offset = end
    if offset < len(text):
        rows.append((offset, len(text), text[offset:]))
    return tuple(rows)


def _is_table_line(line: str) -> bool:
    return line.strip().count("|") >= 2


def _is_separator(line: str) -> bool:
    cells = tuple(cell.strip() for cell in line.strip().strip("|").split("|"))
    return bool(cells) and all(_TABLE_SEPARATOR_CELL.fullmatch(cell) for cell in cells)


def _table_blocks(text: str) -> tuple[tuple[int, int], ...]:
    blocks: list[tuple[int, int]] = []
    pending: list[tuple[int, int, str]] = []
    for start, end, line in (*_source_lines(text), (len(text), len(text), "")):
        if _is_table_line(line):
            pending.append((start, end, line))
            continue
        if (
            len(pending) >= 3
            and not _is_separator(pending[0][2])
            and _is_separator(pending[1][2])
            and any(not _is_separator(row[2]) for row in pending[2:])
        ):
            blocks.append((pending[0][0], pending[-1][1]))
        pending = []
    return tuple(blocks)


def _collect(connection: sqlite3.Connection) -> tuple[dict[str, int], dict[str, tuple[int, int]]]:
    pages: dict[tuple[str, int], str] = {}
    for row in connection.execute(
        "SELECT document_id,page,text,text_sha256 FROM pages ORDER BY document_id,page"
    ):
        document_id, page, text, text_sha256 = str(row[0]), int(row[1]), str(row[2]), str(row[3])
        if hashlib.sha256(text.encode()).hexdigest() != text_sha256:
            raise LegacyV4AuditError("source database page hash is invalid")
        pages[(document_id, page)] = text

    spans_by_page: defaultdict[tuple[str, int], list[tuple[int, int]]] = defaultdict(list)
    continuation = mid_line = titled_body = titleless = body_count = heading_count = 0
    evidence_count = 0
    for row in connection.execute(
        """SELECT evidence_id,document_id,page_start,page_end,section_type,text,source_start,source_end
             FROM evidence ORDER BY document_id,page_start,source_start,evidence_pk"""
    ):
        evidence_count += 1
        document_id = str(row[1])
        page_start, page_end = int(row[2]), int(row[3])
        section_type, text = str(row[4]), str(row[5])
        source_start, source_end = int(row[6]), int(row[7])
        page_text = pages.get((document_id, page_start))
        if (
            page_text is None
            or page_start != page_end
            or section_type not in {"body", "heading"}
            or not 0 <= source_start < source_end <= len(page_text)
            or page_text[source_start:source_end] != text
        ):
            raise LegacyV4AuditError("source database evidence span is invalid")
        spans_by_page[(document_id, page_start)].append((source_start, source_end))
        body_count += int(section_type == "body")
        heading_count += int(section_type == "heading")
        heading_offsets = tuple(match.start() for match in _HEADING.finditer(page_text))
        headings_inside = any(source_start <= offset < source_end for offset in heading_offsets)
        if section_type == "body" and headings_inside:
            titled_body += 1
        if source_start > 0:
            continuation += 1
            mid_line += int(page_text[source_start - 1] not in "\r\n")
            titleless += int(
                section_type == "body"
                and any(offset < source_start for offset in heading_offsets)
                and not headings_inside
            )

    canonical_tables = fragmented_tables = 0
    for page_identity, page_text in pages.items():
        for table_start, table_end in _table_blocks(page_text):
            canonical_tables += 1
            fragmented_tables += int(
                not any(
                    source_start <= table_start and source_end >= table_end
                    for source_start, source_end in spans_by_page[page_identity]
                )
            )
    counts = {
        "body_chunks": body_count,
        "canonical_markdown_tables": canonical_tables,
        "documents": int(connection.execute("SELECT count(*) FROM documents").fetchone()[0]),
        "evidence_chunks": evidence_count,
        "heading_chunks": heading_count,
        "pages": len(pages),
    }
    metrics = {
        "continuation_chunks": (continuation, evidence_count),
        "mid_line_continuations": (mid_line, continuation),
        "titled_body_chunks": (titled_body, body_count),
        "titleless_continuations": (titleless, continuation),
        "fragmented_markdown_tables": (fragmented_tables, canonical_tables),
    }
    return counts, metrics


def audit_database(path: Path) -> dict[str, Any]:
    descriptor = _open_nofollow(path)
    try:
        before = os.fstat(descriptor)
        before_hash = _hash_fd(descriptor, before.st_size)
        try:
            connection = sqlite3.connect(
                f"file:/proc/self/fd/{descriptor}?mode=ro&immutable=1",
                uri=True,
            )
        except sqlite3.Error:
            raise LegacyV4AuditError("source database cannot be opened as immutable SQLite") from None
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            if connection.execute("PRAGMA query_only").fetchone() != (1,):
                raise LegacyV4AuditError("source database did not enter query-only mode")
            if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise LegacyV4AuditError("source database integrity check failed")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise LegacyV4AuditError("source database foreign key check failed")
            metadata = _validate_schema(connection)
            counts, metrics = _collect(connection)
        except sqlite3.Error:
            raise LegacyV4AuditError("source database read-only audit query failed") from None
        finally:
            connection.close()
        after = os.fstat(descriptor)
        after_hash = _hash_fd(descriptor, after.st_size)
        if _identity(before) != _identity(after) or before_hash != after_hash:
            raise LegacyV4AuditError("source database changed during the read-only audit")
    finally:
        os.close(descriptor)

    observed = {name: _measurement(*metrics[name]) for name in METRIC_NAMES}
    expected = {name: _measurement(*HISTORICAL_KB_METRICS[name]) for name in METRIC_NAMES}
    mismatches = [name for name in METRIC_NAMES if observed[name] != expected[name]]
    comparison = {
        "expected": expected,
        "match": not mismatches,
        "mismatched_metrics": mismatches,
        "observed": observed,
        "release_disposition": "non_blocking_distinct_provenance_and_algorithm",
    }
    unsigned = {
        "algorithm": sealed_algorithm_payload(),
        "algorithm_version": ALGORITHM_VERSION,
        "comparison_to_historical_run": comparison,
        "corpus_counts": counts,
        "schema_version": AUDIT_SCHEMA,
        "sensitive_material_included": False,
        "source_database": {
            "contract_sha256": metadata["contract_sha256"],
            "corpus_sha256": metadata["corpus_sha256"],
            "generation_id": metadata["generation_id"],
            "schema_id": metadata["schema_id"],
            "sha256": before_hash,
            "size_bytes": before.st_size,
        },
    }
    return {**unsigned, "evidence_sha256": canonical_sha256(unsigned)}


def _validate_measurement(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"denominator", "numerator", "percent_4dp"}:
        raise LegacyV4AuditError("audit measurement contract is invalid")
    numerator, denominator = value.get("numerator"), value.get("denominator")
    if type(numerator) is not int or type(denominator) is not int:
        raise LegacyV4AuditError("audit measurement counts are invalid")
    if value.get("percent_4dp") != _percent(numerator, denominator):
        raise LegacyV4AuditError("audit measurement percentage is not derived from its counts")
    return dict(value)


def validate_audit_artifact(
    payload: object,
    *,
    require_release_binding: bool = False,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) != {
        "algorithm",
        "algorithm_version",
        "comparison_to_historical_run",
        "corpus_counts",
        "evidence_sha256",
        "schema_version",
        "sensitive_material_included",
        "source_database",
    }:
        raise LegacyV4AuditError("audit artifact has an unknown contract")
    normalized = cast(dict[str, Any], json.loads(canonical_json_bytes(payload)))
    if normalized["schema_version"] != AUDIT_SCHEMA or normalized["algorithm_version"] != ALGORITHM_VERSION:
        raise LegacyV4AuditError("audit artifact schema or algorithm version is invalid")
    if normalized["algorithm"] != sealed_algorithm_payload():
        raise LegacyV4AuditError("audit artifact algorithm is not canonical")
    claimed = normalized.pop("evidence_sha256")
    if not isinstance(claimed, str) or claimed != canonical_sha256(normalized):
        raise LegacyV4AuditError("audit artifact self-hash is invalid")
    normalized["evidence_sha256"] = claimed
    if normalized["sensitive_material_included"] is not False:
        raise LegacyV4AuditError("audit artifact contains sensitive material")
    source = normalized["source_database"]
    if not isinstance(source, dict) or set(source) != set(RELEASE_SOURCE_DATABASE):
        raise LegacyV4AuditError("audit artifact source database binding is invalid")
    if (
        not isinstance(source["size_bytes"], int)
        or _SHA256.fullmatch(str(source["sha256"])) is None
        or _SHA256.fullmatch(str(source["corpus_sha256"])) is None
        or _SHA256.fullmatch(str(source["contract_sha256"])) is None
        or _GENERATION_ID.fullmatch(str(source["generation_id"])) is None
        or source["schema_id"] != "cardrag.serving-db.v4"
    ):
        raise LegacyV4AuditError("audit artifact source database identity is invalid")
    comparison = normalized["comparison_to_historical_run"]
    if not isinstance(comparison, dict) or set(comparison) != {
        "expected",
        "match",
        "mismatched_metrics",
        "observed",
        "release_disposition",
    }:
        raise LegacyV4AuditError("audit artifact comparison contract is invalid")
    expected, observed = comparison["expected"], comparison["observed"]
    if not isinstance(expected, dict) or not isinstance(observed, dict):
        raise LegacyV4AuditError("audit artifact comparison measurements are invalid")
    if set(expected) != set(METRIC_NAMES) or set(observed) != set(METRIC_NAMES):
        raise LegacyV4AuditError("audit artifact metric key set is invalid")
    for name in METRIC_NAMES:
        expected_row = _validate_measurement(expected[name])
        _validate_measurement(observed[name])
        if expected_row != _measurement(*HISTORICAL_KB_METRICS[name]):
            raise LegacyV4AuditError("audit artifact historical expectation is invalid")
    mismatches = [name for name in METRIC_NAMES if expected[name] != observed[name]]
    if (
        comparison["match"] is not (not mismatches)
        or comparison["mismatched_metrics"] != mismatches
        or comparison["release_disposition"] != "non_blocking_distinct_provenance_and_algorithm"
    ):
        raise LegacyV4AuditError("audit artifact comparison result is inconsistent")
    counts = normalized["corpus_counts"]
    if (
        not isinstance(counts, dict)
        or set(counts)
        != {
            "body_chunks",
            "canonical_markdown_tables",
            "documents",
            "evidence_chunks",
            "heading_chunks",
            "pages",
        }
        or any(type(value) is not int or value < 0 for value in counts.values())
    ):
        raise LegacyV4AuditError("audit artifact corpus counts are invalid")
    if (
        observed["continuation_chunks"]["denominator"] != counts["evidence_chunks"]
        or observed["titled_body_chunks"]["denominator"] != counts["body_chunks"]
        or observed["fragmented_markdown_tables"]["denominator"] != counts["canonical_markdown_tables"]
        or observed["mid_line_continuations"]["denominator"] != observed["continuation_chunks"]["numerator"]
        or observed["titleless_continuations"]["denominator"] != observed["continuation_chunks"]["numerator"]
    ):
        raise LegacyV4AuditError("audit artifact denominators are not bound to corpus counts")
    if require_release_binding:
        if source != RELEASE_SOURCE_DATABASE:
            raise LegacyV4AuditError("audit artifact is bound to another release source database")
        actual_metrics = {
            name: (observed[name]["numerator"], observed[name]["denominator"]) for name in METRIC_NAMES
        }
        if actual_metrics != SEALED_RELEASE_METRICS:
            raise LegacyV4AuditError("audit artifact exact release observations differ")
    return normalized


def _json_object(raw: bytes) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValueError(value)

    try:
        payload = json.loads(raw, parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise LegacyV4AuditError("audit artifact is not strict JSON") from None
    if not isinstance(payload, dict):
        raise LegacyV4AuditError("audit artifact is not a JSON object")
    return cast(dict[str, Any], payload)


def load_audit_artifact(path: Path) -> dict[str, Any]:
    descriptor = _open_nofollow(path)
    try:
        info = os.fstat(descriptor)
        if info.st_size > _MAX_ARTIFACT_BYTES:
            raise LegacyV4AuditError("audit artifact exceeds its read bound")
        raw = os.pread(descriptor, info.st_size, 0)
        if len(raw) != info.st_size or os.fstat(descriptor).st_size != info.st_size:
            raise LegacyV4AuditError("audit artifact changed while being read")
    finally:
        os.close(descriptor)
    payload = _json_object(raw)
    if raw != canonical_json_bytes(payload) + b"\n":
        raise LegacyV4AuditError("audit artifact bytes are not canonical")
    return payload


def _historical_observations_from_stdout(stdout: str) -> dict[str, Any]:
    lines = stdout.splitlines()
    if (
        len(lines) != 4
        or lines[1] != " lengths median/p75/p90/max 1177 1595 1598 1600"
        or lines[3] != " lengths median/p75/p90/max 742 1176 1497 1599"
    ):
        raise LegacyV4AuditError("historical source stdout layout is invalid")
    raw_counts: dict[str, dict[str, int]] = {}
    for issuer, line in (("kb", lines[0]), ("samsung", lines[2])):
        prefix = f"{issuer} "
        if not line.startswith(prefix):
            raise LegacyV4AuditError("historical source stdout issuer is invalid")
        try:
            parsed = ast.literal_eval(line[len(prefix) :])
        except (SyntaxError, ValueError):
            raise LegacyV4AuditError("historical source stdout counts are invalid") from None
        if (
            not isinstance(parsed, dict)
            or any(not isinstance(key, str) for key in parsed)
            or any(isinstance(value, bool) or not isinstance(value, int) for value in parsed.values())
        ):
            raise LegacyV4AuditError("historical source stdout counts are invalid")
        raw_counts[issuer] = cast(dict[str, int], parsed)
    if raw_counts != {
        "kb": {
            "chunks": 4175,
            "contains_heading_labeled_body": 1467,
            "continuation_chunks": 1379,
            "docs": 747,
            "ends_mid_line": 1326,
            "headings": 6078,
            "long_pages": 1278,
            "no_heading_context": 389,
            "pages": 2796,
            "starts_mid_line": 1293,
            "table_blocks": 3065,
            "table_blocks_not_whole_in_one_chunk": 45,
            "table_pages": 1887,
        },
        "samsung": {
            "chunks": 806,
            "contains_heading_labeled_body": 395,
            "continuation_chunks": 74,
            "docs": 65,
            "ends_mid_line": 66,
            "headings": 1953,
            "long_pages": 73,
            "no_heading_context": 35,
            "pages": 732,
            "starts_mid_line": 69,
            "table_blocks": 421,
            "table_blocks_not_whole_in_one_chunk": 12,
            "table_pages": 332,
        },
    }:
        raise LegacyV4AuditError("historical source stdout exact counts differ")
    return {
        issuer: {
            "chunks": counts["chunks"],
            "continuation_chunks": counts["continuation_chunks"],
            "documents": counts["docs"],
            "ends_mid_line": counts["ends_mid_line"],
            "fragmented_tables": counts["table_blocks_not_whole_in_one_chunk"],
            "mid_line_continuations": counts["starts_mid_line"],
            "pages": counts["pages"],
            "table_blocks": counts["table_blocks"],
            "titled_body_chunks": counts["contains_heading_labeled_body"],
            "titleless_continuations": counts["no_heading_context"],
        }
        for issuer, counts in raw_counts.items()
    }


def _validated_historical_source(
    payload: object,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate and decode the exact contemporaneous read-only execution record."""

    if not isinstance(payload, Mapping) or set(payload) != {
        "evidence_scope",
        "evidence_sha256",
        "raw_record_base64",
        "schema_version",
        "source_session",
    }:
        raise LegacyV4AuditError("historical source artifact has an unknown contract")
    normalized = cast(dict[str, Any], json.loads(canonical_json_bytes(payload)))
    claimed = normalized.pop("evidence_sha256")
    if not isinstance(claimed, str) or claimed != canonical_sha256(normalized):
        raise LegacyV4AuditError("historical source artifact self-hash is invalid")
    normalized["evidence_sha256"] = claimed
    if normalized["schema_version"] != HISTORICAL_SOURCE_SCHEMA:
        raise LegacyV4AuditError("historical source artifact identity is invalid")
    if normalized["evidence_scope"] != {
        "authentication_material_included": False,
        "card_document_text_included": False,
        "external_timestamp_attestation": False,
        "full_session_included": False,
        "independent_session_inclusion_proof": False,
        "missing_inputs_fail_closed": False,
        "operational_identifiers_and_local_paths_included": True,
        "run_completion_attested": False,
        "session_reference_locally_verified": True,
        "stable_snapshot_attested": False,
        "trust_root": "repository_review",
        "underlying_run_artifacts_hash_bound": False,
    }:
        raise LegacyV4AuditError("historical source evidence scope is invalid")
    source_session = normalized["source_session"]
    if source_session != {
        "file_name": _HISTORICAL_SESSION_BASENAME,
        "record_line": _HISTORICAL_RECORD_LINE,
        "record_sha256": _HISTORICAL_RECORD_SHA256,
        "session_sha256": _HISTORICAL_SESSION_SHA256,
    }:
        raise LegacyV4AuditError("historical source session binding is invalid")
    encoded = normalized["raw_record_base64"]
    if not isinstance(encoded, str) or len(encoded) > 16_384:
        raise LegacyV4AuditError("historical source record encoding is invalid")
    try:
        raw_record = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise LegacyV4AuditError("historical source record encoding is invalid") from None
    if (
        len(raw_record) != _HISTORICAL_RECORD_SIZE
        or not raw_record.endswith(b"\n")
        or hashlib.sha256(raw_record).hexdigest() != _HISTORICAL_RECORD_SHA256
        or base64.b64encode(raw_record).decode("ascii") != encoded
    ):
        raise LegacyV4AuditError("historical source record bytes are invalid")
    lowered = raw_record.lower()
    if any(
        forbidden in lowered
        for forbidden in (
            b"authorization:",
            b"bearer ",
            b"api-key",
            b"api_key",
            b"credential",
            b"openrouter_api_key",
            b"password",
            b"secret",
            b"token",
            b"webdav_password",
            b"webdav_username",
        )
    ):
        raise LegacyV4AuditError("historical source record contains sensitive material")
    record = _json_object(raw_record)
    if (
        record.get("timestamp") != "2026-08-29T04:31:04.015Z"
        or record.get("ordinal") != 206
        or record.get("type") != "event_msg"
    ):
        raise LegacyV4AuditError("historical source record envelope is invalid")
    record_payload = record.get("payload")
    if not isinstance(record_payload, dict) or record_payload.get("type") != "item_completed":
        raise LegacyV4AuditError("historical source record payload is invalid")
    item = record_payload.get("item")
    if not isinstance(item, dict) or item.get("type") != "CommandExecution":
        raise LegacyV4AuditError("historical source command record is invalid")
    command = item.get("command")
    stdout = item.get("stdout")
    if (
        not isinstance(command, list)
        or len(command) != 3
        or command[:2] != ["/bin/bash", "-lc"]
        or not isinstance(command[2], str)
        or hashlib.sha256(command[2].encode()).hexdigest() != _HISTORICAL_COMMAND_SHA256
        or not isinstance(stdout, str)
        or hashlib.sha256(stdout.encode()).hexdigest() != _HISTORICAL_STDOUT_SHA256
        or item.get("aggregated_output") != stdout
        or item.get("formatted_output") != stdout
        or item.get("stderr") != ""
        or item.get("exit_code") != 0
        or item.get("status") != "completed"
        or item.get("source") != "unified_exec_startup"
        or item.get("cwd") != "file:///home/lee/projects/MCP_card_prd_detail"
        or item.get("duration") != {"nanos": 897_307_195, "secs": 0}
    ):
        raise LegacyV4AuditError("historical source command result is invalid")
    _historical_observations_from_stdout(stdout)
    return normalized, item


def validate_historical_source_artifact(payload: object) -> dict[str, Any]:
    """Validate the exact, contemporaneous read-only audit execution record.

    The embedded JSONL record is the original Codex command-execution event,
    not a later transcription of its counters. Its record hash, command hash,
    stdout hash, parsed observations, session identity, exit status, and
    read-only audit script are fixed so resealing a different claim cannot pass.
    """

    normalized, _ = _validated_historical_source(payload)
    return normalized


def validate_historical_artifact(
    payload: object,
    *,
    require_source_binding: bool = False,
    source_artifact: object | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) != {
        "algorithm",
        "evidence_sha256",
        "observations",
        "provenance",
        "schema_version",
        "sensitive_material_included",
    }:
        raise LegacyV4AuditError("historical observation artifact has an unknown contract")
    normalized = cast(dict[str, Any], json.loads(canonical_json_bytes(payload)))
    claimed = normalized.pop("evidence_sha256")
    if claimed != canonical_sha256(normalized):
        raise LegacyV4AuditError("historical observation self-hash is invalid")
    normalized["evidence_sha256"] = claimed
    if (
        normalized["schema_version"] != HISTORICAL_SCHEMA
        or normalized["algorithm"] != historical_algorithm_payload()
        or normalized["sensitive_material_included"] is not False
    ):
        raise LegacyV4AuditError("historical observation identity is invalid")
    provenance = normalized["provenance"]
    if not isinstance(provenance, dict) or set(provenance) != {
        "binding",
        "run_id",
        "source_artifact_sha256",
        "source_kind",
    }:
        raise LegacyV4AuditError("historical observation provenance is invalid")
    source_hash = provenance["source_artifact_sha256"]
    if (
        provenance["run_id"] != "e63725b579b5405fb03c6dc7e3d2b061"
        or (
            source_hash is None
            and (
                provenance["binding"] != "observation_only"
                or provenance["source_kind"] != "worker_run_artifacts"
            )
        )
        or (
            source_hash is not None
            and (
                not isinstance(source_hash, str)
                or _SHA256.fullmatch(source_hash) is None
                or provenance["binding"] != "execution_record_hash_bound"
                or provenance["source_kind"] != "codex_command_execution"
            )
        )
    ):
        raise LegacyV4AuditError("historical observation provenance binding is invalid")
    if source_artifact is not None:
        validated_source, source_item = _validated_historical_source(source_artifact)
        actual_source_hash = hashlib.sha256(canonical_json_bytes(validated_source) + b"\n").hexdigest()
        if source_hash != actual_source_hash:
            raise LegacyV4AuditError("historical source artifact does not match its observation")
        if normalized["observations"] != _historical_observations_from_stdout(
            cast(str, source_item["stdout"])
        ):
            raise LegacyV4AuditError("historical observations differ from source stdout")
    if require_source_binding and (source_hash is None or source_artifact is None):
        raise LegacyV4AuditError("historical source artifact is required for release")
    expected_observations = {
        "kb": {
            "chunks": 4175,
            "continuation_chunks": 1379,
            "documents": 747,
            "ends_mid_line": 1326,
            "fragmented_tables": 45,
            "mid_line_continuations": 1293,
            "pages": 2796,
            "table_blocks": 3065,
            "titled_body_chunks": 1467,
            "titleless_continuations": 389,
        },
        "samsung": {
            "chunks": 806,
            "continuation_chunks": 74,
            "documents": 65,
            "ends_mid_line": 66,
            "fragmented_tables": 12,
            "mid_line_continuations": 69,
            "pages": 732,
            "table_blocks": 421,
            "titled_body_chunks": 395,
            "titleless_continuations": 35,
        },
    }
    if normalized["observations"] != expected_observations:
        raise LegacyV4AuditError("historical observation exact counts differ")
    return normalized


def validate_release_evidence(
    sealed_payload: object,
    historical_payload: object,
    historical_source_payload: object,
) -> None:
    """Cross-bind the two deliberately distinct legacy provenance domains."""

    sealed = validate_audit_artifact(sealed_payload, require_release_binding=True)
    historical = validate_historical_artifact(
        historical_payload,
        require_source_binding=True,
        source_artifact=historical_source_payload,
    )
    counts = sealed["corpus_counts"]
    historical_kb = historical["observations"]["kb"]
    expected = {
        "continuation_chunks": _measurement(historical_kb["continuation_chunks"], historical_kb["chunks"]),
        "fragmented_markdown_tables": _measurement(
            historical_kb["fragmented_tables"], counts["canonical_markdown_tables"]
        ),
        "mid_line_continuations": _measurement(
            historical_kb["mid_line_continuations"], historical_kb["continuation_chunks"]
        ),
        "titled_body_chunks": _measurement(historical_kb["titled_body_chunks"], counts["body_chunks"]),
        "titleless_continuations": _measurement(
            historical_kb["titleless_continuations"], historical_kb["continuation_chunks"]
        ),
    }
    if sealed["comparison_to_historical_run"]["expected"] != expected:
        raise LegacyV4AuditError("sealed and historical audit evidence are not cross-bound")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit a local legacy v4 CardRAG database read-only")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--database", type=Path)
    mode.add_argument("--validate-artifact", type=Path)
    mode.add_argument("--validate-release-artifact", type=Path)
    parser.add_argument("--historical-artifact", type=Path)
    parser.add_argument("--historical-source-artifact", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.database is not None:
            artifact = audit_database(arguments.database)
            sys.stdout.buffer.write(canonical_json_bytes(artifact) + b"\n")
            return 0
        artifact = load_audit_artifact(
            cast(Path, arguments.validate_release_artifact or arguments.validate_artifact)
        )
        validate_audit_artifact(
            artifact,
            require_release_binding=arguments.validate_release_artifact is not None,
        )
        if arguments.validate_release_artifact is not None:
            if arguments.historical_artifact is None or arguments.historical_source_artifact is None:
                raise LegacyV4AuditError("release validation requires both historical artifacts")
            historical = load_audit_artifact(arguments.historical_artifact)
            historical_source = load_audit_artifact(arguments.historical_source_artifact)
            validate_release_evidence(
                artifact,
                historical,
                historical_source,
            )
        elif arguments.historical_artifact is not None or arguments.historical_source_artifact is not None:
            raise LegacyV4AuditError("historical artifacts are only valid for release validation")
        return 0
    except LegacyV4AuditError:
        print("legacy v4 audit validation failed", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "ALGORITHM_VERSION",
    "AUDIT_SCHEMA",
    "HISTORICAL_ALGORITHM_VERSION",
    "HISTORICAL_SCHEMA",
    "HISTORICAL_SOURCE_SCHEMA",
    "LegacyV4AuditError",
    "audit_database",
    "historical_algorithm_payload",
    "load_audit_artifact",
    "main",
    "sealed_algorithm_payload",
    "validate_audit_artifact",
    "validate_historical_artifact",
    "validate_historical_source_artifact",
    "validate_release_evidence",
]
