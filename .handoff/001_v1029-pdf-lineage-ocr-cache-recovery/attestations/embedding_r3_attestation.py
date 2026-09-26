#!/usr/bin/env python3
"""Integrity attestation for the r3 candidate embedding_cache_v5 provenance.

Read-only over both mounted worker-state volumes. Re-derives every table CHECK
constraint plus the L2 norm contract per row, proves verbatim source->r3 row
equivalence, bounds the run-produced extra rows inside the terminal run
window, and proves that every sealed vectors.f32 row equals an embedding blob
present in the r3 cache. Emits a canonical JSON attestation on stdout.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import struct
import sys
from datetime import datetime
from typing import Any

SRC_DB = "/src/worker-state.sqlite3"
DST_DB = "/dst/worker-state.sqlite3"
RUN_ID = "0928dee8e6f04af9ae41fdb736c10df3"
VECTORS = f"/dst/runs/{RUN_ID}/sealed/vectors.f32"
VECTOR_ROW_BYTES = 4096 * 4

_SHA = re.compile(r"^[0-9a-f]{64}$")


def sha256_file(path: str, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY)
    try:
        while block := os.read(fd, chunk):
            digest.update(block)
    finally:
        os.close(fd)
    return digest.hexdigest()


def open_ro(path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    connection.execute("PRAGMA query_only=ON")
    return connection


def validate_row(cache_key: str, profile_id: str, input_sha256: str, dimension: object, dtype: str,
                 normalization: str, embedding: bytes, created_at: str) -> str | None:
    if not _SHA.fullmatch(cache_key) or len(cache_key) != 64:
        return "cache_key"
    if not profile_id or profile_id != profile_id.strip() or not (1 <= len(profile_id) <= 512) or "\x00" in profile_id:
        return "profile_id"
    if not _SHA.fullmatch(input_sha256):
        return "input_sha256"
    if isinstance(dimension, bool) or not isinstance(dimension, int) or dimension <= 0:
        return "dimension"
    if dtype != "float32":
        return "dtype"
    if normalization != "l2":
        return "normalization"
    assert isinstance(dimension, int)
    if len(embedding) != dimension * 4:
        return "blob_length"
    values = struct.unpack(f"<{dimension}f", embedding)
    if not all(math.isfinite(value) for value in values):
        return "non_finite"
    norm_squared = sum(value * value for value in values)
    if not math.isclose(norm_squared, 1.0, rel_tol=2e-5, abs_tol=2e-5):
        return "norm"
    try:
        parsed = datetime.fromisoformat(created_at)
    except ValueError:
        return "created_at"
    if parsed.tzinfo is None:
        return "created_at"
    return None


def collect(
    connection: sqlite3.Connection,
    label: str,
    stats: dict[str, Any],
    *,
    validate: bool,
) -> dict[str, tuple[str, str, int, str, str, str, str]]:
    rows: dict[str, tuple[str, str, int, str, str, str, str]] = {}
    start = time_now()
    cursor = connection.execute(
        "SELECT cache_key,profile_id,input_sha256,dimension,dtype,normalization,embedding,created_at"
        " FROM embedding_cache_v5 ORDER BY cache_key"
    )
    invalid: dict[str, int] = {}
    for cache_key, profile_id, input_sha256, dimension, dtype, normalization, embedding, created_at in cursor:
        violation = (
            validate_row(cache_key, profile_id, input_sha256, dimension, dtype, normalization, embedding, created_at)
            if validate
            else None
        )
        if violation is not None:
            invalid[violation] = invalid.get(violation, 0) + 1
            continue
        if not validate:
            if (
                len(embedding) != dimension * 4
                or dtype != "float32"
                or normalization != "l2"
                or not _SHA.fullmatch(cache_key)
                or not _SHA.fullmatch(input_sha256)
            ):
                raise SystemExit(f"{label} has structurally invalid row {cache_key}")
        assert isinstance(dimension, int)
        rows[cache_key] = (
            profile_id,
            input_sha256,
            dimension,
            dtype,
            normalization,
            hashlib.sha256(embedding).hexdigest(),
            created_at,
        )
    stats[label] = {
        "rows_scanned": len(rows) + sum(invalid.values()),
        "rows_valid": len(rows),
        "invalid_violations": invalid,
        "seconds": round(time_now() - start, 1),
    }
    if invalid:
        raise SystemExit(f"{label} contains constraint-violating rows: {invalid}")
    return rows


def time_now() -> float:
    import time

    return time.monotonic()


def main() -> None:
    stats: dict[str, Any] = {"started_at": datetime.now().astimezone().isoformat()}
    src = open_ro(SRC_DB)
    dst = open_ro(DST_DB)

    run_row = dst.execute(
        "SELECT started_at,finished_at,status FROM run WHERE run_id=?", (RUN_ID,)
    ).fetchone()
    assert run_row is not None and run_row[2] == "succeeded"
    stats["run_window"] = {"started_at": run_row[0], "finished_at": run_row[1]}

    stats["source_database_sha256"] = sha256_file(SRC_DB)
    stats["destination_database_sha256"] = sha256_file(DST_DB)

    src_rows = collect(src, "source_v114", stats, validate=False)
    dst_rows = collect(dst, "candidate_r3", stats, validate=True)

    verbatim = 0
    mismatched: list[str] = []
    for key, value in src_rows.items():
        existing = dst_rows.get(key)
        if existing == value:
            verbatim += 1
        else:
            mismatched.append(key)
    only_in_source = sorted(set(src_rows) - set(dst_rows))
    extra = {key: dst_rows[key] for key in set(dst_rows) - set(src_rows)}

    run_start = datetime.fromisoformat(str(run_row[0]))
    run_finish = datetime.fromisoformat(str(run_row[1]))
    outside_window: list[str] = []
    for key, value in extra.items():
        created = datetime.fromisoformat(value[6])
        if not (run_start <= created <= run_finish):
            outside_window.append(key)

    stats["equivalence"] = {
        "source_rows": len(src_rows),
        "destination_rows": len(dst_rows),
        "verbatim_copied_rows": verbatim,
        "verbatim_mismatch_keys": mismatched[:10],
        "only_in_source_keys": only_in_source[:10],
        "extra_rows_in_destination": len(extra),
        "extra_rows_created_outside_run_window": outside_window[:10],
        "extra_row_created_at_bounds": [
            min(value[6] for value in extra.values()),
            max(value[6] for value in extra.values()),
        ]
        if extra
        else None,
        "extra_row_profiles": sorted({value[0] for value in extra.values()}),
    }

    # vectors.f32 derivation: every 4096-dim float32 row must equal an r3 cache blob
    blob_shas = {value[5] for value in dst_rows.values()}
    with open(f"/dst/runs/{RUN_ID}/sealed/publish.json") as handle:
        seal = json.load(handle)
    expected_vectors = int(seal["manifest"]["counts"]["chunks"])
    vectors_fd = os.open(VECTORS, os.O_RDONLY)
    matched = 0
    unmatched = 0
    file_digest = hashlib.sha256()
    vector_rows = 0
    try:
        stats["vectors_file_bytes"] = os.fstat(vectors_fd).st_size
        while True:
            blob = os.read(vectors_fd, VECTOR_ROW_BYTES)
            if not blob:
                break
            if len(blob) != VECTOR_ROW_BYTES:
                raise SystemExit("vectors.f32 has a truncated final row")
            file_digest.update(blob)
            vector_rows += 1
            if hashlib.sha256(blob).hexdigest() in blob_shas:
                matched += 1
            else:
                unmatched += 1
    finally:
        os.close(vectors_fd)
    stats["vectors_derivation"] = {
        "vector_rows_checked": matched + unmatched,
        "rows_equal_to_an_r3_cache_blob": matched,
        "rows_without_cache_evidence": unmatched,
        "expected_chunk_count_from_seal": expected_vectors,
        "vectors_file_sha256": file_digest.hexdigest(),
        "vectors_file_sha256_matches_seal": file_digest.hexdigest() == str(seal["vector_sha256"]),
    }
    if matched + unmatched != expected_vectors or not stats["vectors_derivation"]["vectors_file_sha256_matches_seal"]:
        raise SystemExit("vectors.f32 does not match the seal")
    stats["completed_at"] = datetime.now().astimezone().isoformat()
    stats["schema_version"] = "cardrag.embedding-r3-attestation.v1"
    json.dump(stats, sys.stdout, indent=2, sort_keys=True)
    print()
    if mismatched or only_in_source or outside_window or unmatched:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
