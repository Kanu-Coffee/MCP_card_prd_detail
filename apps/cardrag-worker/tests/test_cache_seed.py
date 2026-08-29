from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from helpers import pdf_bytes
from typer.testing import CliRunner

import cardrag_worker.cache_seed as cache_seed_module
import cardrag_worker.cli as cli_module
from cardrag_worker.cache_seed import (
    CacheSeedError,
    apply_cache_seed,
    build_cache_seed_plan,
)
from cardrag_worker.contracts import SourceRecord, canonical_json_bytes, canonical_sha256
from cardrag_worker.pdf_cache import PDFCache, PDFSourceIdentity
from cardrag_worker.state import WorkerState

LEGACY_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE run (
  run_id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed','no_change')),
  corpus_sha256 TEXT,
  contract_sha256 TEXT,
  error TEXT
) STRICT;
CREATE TABLE snapshot (
  snapshot_id TEXT NOT NULL,
  run_id TEXT NOT NULL REFERENCES run(run_id),
  issuer TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  source_sha256 TEXT NOT NULL,
  record_count INTEGER NOT NULL CHECK(record_count >= 0),
  payload_json TEXT NOT NULL,
  PRIMARY KEY (run_id, snapshot_id)
) STRICT;
"""


def source_record(
    *,
    product_code: str = "CARD-1",
    source_url: str = "https://cards.example/card-1.pdf",
    source_version: str = "1",
    source_post_id: str = "post-1",
    discovered_at: datetime | None = None,
) -> SourceRecord:
    return SourceRecord(
        issuer="kb",
        product_code=product_code,
        product_name=f"테스트 {product_code}",
        effective_date=datetime(2026, 1, 1, tzinfo=UTC).date(),
        source_version=source_version,
        source_url=source_url,
        source_post_id=source_post_id,
        file_name=f"{product_code}.pdf",
        category="credit",
        discovered_at=discovered_at or datetime(2026, 1, 1, tzinfo=UTC),
        metadata={"fixture": product_code},
    )


def initialize_legacy_root(root: Path) -> sqlite3.Connection:
    root.mkdir(parents=True)
    connection = sqlite3.connect(root / "worker-state.sqlite3")
    connection.executescript(LEGACY_SCHEMA)
    return connection


def insert_run(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    observed_at: datetime,
    status: str = "succeeded",
) -> None:
    connection.execute(
        "INSERT INTO run(run_id,started_at,finished_at,status) VALUES(?,?,?,?)",
        (
            run_id,
            (observed_at - timedelta(minutes=1)).isoformat(),
            observed_at.isoformat() if status != "running" else None,
            status,
        ),
    )


def insert_snapshot(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    observed_at: datetime,
    sources: tuple[SourceRecord, ...],
) -> str:
    records = sorted((source.discovery_payload for source in sources), key=canonical_json_bytes)
    payload = {
        "contract_version": "cardrag.source-snapshot.v1",
        "issuer": "kb",
        "parser_version": "kb.current.v2",
        "records": records,
        "source_url": "https://card.kbcard.com/disclosure",
    }
    snapshot_id = canonical_sha256(payload)
    connection.execute(
        """INSERT INTO snapshot
           (snapshot_id,run_id,issuer,observed_at,source_sha256,record_count,payload_json)
           VALUES(?,?,?,?,?,?,?)""",
        (
            snapshot_id,
            run_id,
            "kb",
            observed_at.isoformat(),
            snapshot_id,
            len(records),
            canonical_json_bytes(payload).decode("utf-8"),
        ),
    )
    return snapshot_id


def finish_legacy(connection: sqlite3.Connection) -> None:
    connection.commit()
    connection.close()


def write_run_pdf(root: Path, run_id: str, directory: str, source: SourceRecord, body: bytes) -> Path:
    target = root / "runs" / run_id / directory / f"{source.source_id}.pdf"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    return target


def one_candidate_fixture(root: Path) -> tuple[SourceRecord, Path, datetime]:
    observed = datetime(2026, 1, 2, tzinfo=UTC)
    source = source_record(discovered_at=observed)
    missing = source_record(
        product_code="CARD-MISSING",
        source_url="https://cards.example/missing.pdf",
        source_post_id="post-missing",
        discovered_at=observed,
    )
    connection = initialize_legacy_root(root)
    insert_run(connection, run_id="run-1", observed_at=observed)
    insert_snapshot(
        connection,
        run_id="run-1",
        observed_at=observed,
        sources=(source, missing),
    )
    finish_legacy(connection)
    path = write_run_pdf(root, "run-1", "downloads", source, pdf_bytes(pages=2))
    return source, path, observed


def test_cache_seed_dry_run_is_read_only_deterministic_and_reports_missing_sources(
    tmp_path: Path,
) -> None:
    root = tmp_path / "legacy"
    source, pdf_path, observed = one_candidate_fixture(root)
    database_path = root / "worker-state.sqlite3"
    before_database = (
        database_path.stat().st_mtime_ns,
        hashlib.sha256(database_path.read_bytes()).hexdigest(),
    )
    before_pdf = (pdf_path.stat().st_mtime_ns, hashlib.sha256(pdf_path.read_bytes()).hexdigest())

    first = build_cache_seed_plan(root)
    second = build_cache_seed_plan(root)

    assert first.report(applied=False) == second.report(applied=False)
    report = first.report(applied=False)
    assert report["status"] == "verified"
    assert report["dry_run"] is True
    assert report["candidate_files"] == 1
    assert report["applied_candidates"] == 0
    assert report["missing_source_files"] == 1
    assert report["missing_sample"] == [
        {
            "issuer": "kb",
            "product_code": "CARD-MISSING",
            "reason": "no_exact_legacy_pdf",
            "run_id": "run-1",
            "source_id": first.missing_sources[0].source.source_id,
        }
    ]
    assert report["missing_sample_truncated"] == 0
    assert report["source_occurrence_count"] == 2
    assert report["ledger_path"] is None
    assert report["ledger_accepted_candidates"] == 1
    assert report["ledger_missing_sources"] == 1
    assert report["ledger_skipped_stale_runs"] == 0
    assert report["ledger_unique_pdf_hashes"] == 1
    assert report["ledger_size_bytes"] == len(first.ledger_bytes)
    assert report["ledger_sha256"] == hashlib.sha256(first.ledger_bytes).hexdigest()
    assert report["legacy_database_sha256"] == before_database[1]
    assert report["sample"] == [
        {
            "directory": "downloads",
            "issuer": "kb",
            "observed_at": observed.isoformat(),
            "pdf_sha256": hashlib.sha256(pdf_path.read_bytes()).hexdigest(),
            "product_code": "CARD-1",
            "run_id": "run-1",
            "size_bytes": len(pdf_path.read_bytes()),
            "source_id": source.source_id,
        }
    ]
    ledger = json.loads(first.ledger_bytes)
    assert canonical_json_bytes(ledger) == first.ledger_bytes
    assert ledger["schema_version"] == "cardrag.cache-seed-ledger.v1"
    assert ledger["legacy_database"] == {
        "path": "worker-state.sqlite3",
        "sha256": before_database[1],
        "size_bytes": database_path.stat().st_size,
    }
    assert ledger["accepted_candidates"] == [
        {
            "directory": "downloads",
            "issuer": "kb",
            "legacy_path": f"runs/run-1/downloads/{source.source_id}.pdf",
            "observed_at": observed.isoformat(),
            "page_count": 2,
            "pdf_sha256": hashlib.sha256(pdf_path.read_bytes()).hexdigest(),
            "product_code": "CARD-1",
            "product_name": "테스트 CARD-1",
            "run_id": "run-1",
            "size_bytes": len(pdf_path.read_bytes()),
            "source_id": source.source_id,
        }
    ]
    assert ledger["unique_accepted_pdf_sha256"] == [hashlib.sha256(pdf_path.read_bytes()).hexdigest()]
    assert ledger["missing_sources"] == [
        {
            "issuer": "kb",
            "observed_at": observed.isoformat(),
            "product_code": "CARD-MISSING",
            "product_name": "테스트 CARD-MISSING",
            "reason": "no_exact_legacy_pdf",
            "run_id": "run-1",
            "source_id": first.missing_sources[0].source.source_id,
        }
    ]
    assert ledger["skipped_stale_run_ids"] == []
    assert not tuple(root.glob("worker-state.sqlite3-*"))
    assert before_database == (
        database_path.stat().st_mtime_ns,
        hashlib.sha256(database_path.read_bytes()).hexdigest(),
    )
    assert before_pdf == (pdf_path.stat().st_mtime_ns, hashlib.sha256(pdf_path.read_bytes()).hexdigest())


def test_cache_seed_apply_uses_public_cache_api_and_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "legacy"
    destination = tmp_path / "candidate"
    source, _, observed = one_candidate_fixture(root)
    plan = build_cache_seed_plan(root)

    with WorkerState(destination / "worker-state.sqlite3") as state:
        cache = PDFCache(destination, state)
        first = apply_cache_seed(plan, cache)
        ledger_path = destination / first["ledger_path"]
        ledger_before = (
            ledger_path.stat().st_ino,
            ledger_path.stat().st_mtime_ns,
            ledger_path.read_bytes(),
        )
        second = apply_cache_seed(plan, cache)
        ledger_after = (
            ledger_path.stat().st_ino,
            ledger_path.stat().st_mtime_ns,
            ledger_path.read_bytes(),
        )
        identity = PDFSourceIdentity.from_source_record(source)
        hit = cache.lookup(identity)

        assert first["status"] == "applied"
        assert first["applied_candidates"] == 1
        assert first["reused_candidates"] == 0
        assert first["created_pdf_objects"] == 1
        assert first["created_revisions"] == 1
        assert first["ledger_path"] == f"audit-reports/cache-seed/{plan.ledger_sha256}.json"
        assert first["ledger_sha256"] == plan.ledger_sha256
        assert first["ledger_size_bytes"] == len(plan.ledger_bytes)
        assert ledger_before[2] == plan.ledger_bytes
        assert hashlib.sha256(ledger_before[2]).hexdigest() == plan.ledger_sha256
        assert second["status"] == "applied"
        assert second["applied_candidates"] == 0
        assert second["reused_candidates"] == 1
        assert second["created_pdf_objects"] == 0
        assert second["created_revisions"] == 0
        assert second["ledger_path"] == first["ledger_path"]
        assert second["ledger_sha256"] == first["ledger_sha256"]
        assert ledger_after == ledger_before
        assert not tuple(ledger_path.parent.glob(".*.tmp"))
        assert hit is not None
        assert hit.page_count == 2
        history = state.pdf_cache_source_history(source.source_id)
        assert len(history) == 1
        assert history[0].revision_first_observed_at == observed.isoformat()


def test_cache_seed_ledger_persistence_fsyncs_file_and_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "legacy"
    destination = tmp_path / "candidate"
    one_candidate_fixture(root)
    plan = build_cache_seed_plan(root)
    destination.mkdir()
    real_fsync = os.fsync
    fsynced_modes: list[int] = []

    def tracked_fsync(descriptor: int) -> None:
        fsynced_modes.append(os.fstat(descriptor).st_mode)
        real_fsync(descriptor)

    monkeypatch.setattr(cache_seed_module.os, "fsync", tracked_fsync)
    relative = cache_seed_module._persist_ledger(plan, destination)

    target = destination / relative
    assert target.read_bytes() == plan.ledger_bytes
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert any(stat.S_ISREG(mode) for mode in fsynced_modes)
    assert any(stat.S_ISDIR(mode) for mode in fsynced_modes)


def test_cache_seed_applies_byte_and_source_revisions_in_observation_order(tmp_path: Path) -> None:
    root = tmp_path / "legacy"
    destination = tmp_path / "candidate"
    old = source_record()
    renewed = source_record(
        source_url="https://cards.example/card-1-v2.pdf",
        source_version="2",
        source_post_id="post-2",
    )
    first_time = datetime(2026, 1, 1, tzinfo=UTC)
    second_time = first_time + timedelta(days=1)
    third_time = second_time + timedelta(days=1)
    connection = initialize_legacy_root(root)
    for run_id, observed, source in (
        ("run-3", third_time, renewed),
        ("run-1", first_time, old),
        ("run-2", second_time, old),
    ):
        insert_run(connection, run_id=run_id, observed_at=observed)
        insert_snapshot(connection, run_id=run_id, observed_at=observed, sources=(source,))
    finish_legacy(connection)
    first_body = pdf_bytes(width=600)
    second_body = pdf_bytes(width=601)
    write_run_pdf(root, "run-1", "downloads", old, first_body)
    write_run_pdf(root, "run-2", "downloads", old, second_body)
    write_run_pdf(root, "run-3", "downloads", renewed, second_body)
    plan = build_cache_seed_plan(root)

    assert [candidate.run_id for candidate in plan.candidates] == ["run-1", "run-2", "run-3"]
    ledger = json.loads(plan.ledger_bytes)
    assert ledger["counts"]["accepted_candidates"] == 3
    assert ledger["counts"]["unique_accepted_pdf_hashes"] == 2
    assert ledger["unique_accepted_pdf_sha256"] == sorted(
        {
            hashlib.sha256(first_body).hexdigest(),
            hashlib.sha256(second_body).hexdigest(),
        }
    )
    with WorkerState(destination / "worker-state.sqlite3") as state:
        cache = PDFCache(destination, state)
        first = apply_cache_seed(plan, cache)

        old_history = state.pdf_cache_source_history(old.source_id)
        assert [row.pdf_sha256 for row in old_history] == [
            hashlib.sha256(first_body).hexdigest(),
            hashlib.sha256(second_body).hexdigest(),
        ]
        old_binding = state.pdf_cache_source_binding(old.source_id)
        renewed_binding = state.pdf_cache_source_binding(renewed.source_id)
        assert old_binding is not None and renewed_binding is not None
        assert old_binding.superseded_by_source_id == renewed.source_id
        assert renewed_binding.superseded_by_source_id is None

        history_before = tuple(
            (row.source_id, row.pdf_sha256, row.revision_id)
            for source_id in (old.source_id, renewed.source_id)
            for row in state.pdf_cache_source_history(source_id)
        )
        second = apply_cache_seed(plan, cache)
        history_after = tuple(
            (row.source_id, row.pdf_sha256, row.revision_id)
            for source_id in (old.source_id, renewed.source_id)
            for row in state.pdf_cache_source_history(source_id)
        )
        assert first["created_revisions"] == 3
        assert second["applied_candidates"] == 0
        assert second["reused_candidates"] == 3
        assert second["created_revisions"] == 0
        assert history_after == history_before


def test_cache_seed_orders_original_before_resume_download_for_the_same_snapshot(tmp_path: Path) -> None:
    root = tmp_path / "legacy"
    destination = tmp_path / "candidate"
    observed = datetime(2026, 1, 1, tzinfo=UTC)
    source = source_record()
    connection = initialize_legacy_root(root)
    insert_run(connection, run_id="run-1", observed_at=observed)
    insert_snapshot(connection, run_id="run-1", observed_at=observed, sources=(source,))
    finish_legacy(connection)
    first_body = pdf_bytes(width=600)
    resumed_body = pdf_bytes(width=601)
    write_run_pdf(root, "run-1", "downloads", source, first_body)
    write_run_pdf(root, "run-1", "resume-downloads", source, resumed_body)
    plan = build_cache_seed_plan(root)

    assert [candidate.directory for candidate in plan.candidates] == ["downloads", "resume-downloads"]
    with WorkerState(destination / "worker-state.sqlite3") as state:
        apply_cache_seed(plan, PDFCache(destination, state))
        assert [row.pdf_sha256 for row in state.pdf_cache_source_history(source.source_id)] == [
            hashlib.sha256(first_body).hexdigest(),
            hashlib.sha256(resumed_body).hexdigest(),
        ]


def test_cache_seed_rejects_incompatible_destination_history_before_mutation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "legacy"
    destination = tmp_path / "candidate"
    source, _, observed = one_candidate_fixture(root)
    plan = build_cache_seed_plan(root)
    incompatible_path = tmp_path / "incompatible.pdf"
    incompatible_body = pdf_bytes(width=777)
    incompatible_path.write_bytes(incompatible_body)
    incompatible_digest = hashlib.sha256(incompatible_body).hexdigest()

    with WorkerState(destination / "worker-state.sqlite3") as state:
        cache = PDFCache(destination, state)
        cache.ingest_and_bind(
            PDFSourceIdentity.from_source_record(source),
            incompatible_path,
            final_url=source.source_url,
            expected_sha256=incompatible_digest,
            observed_at=observed,
            verified_at=observed,
        )

        with pytest.raises(CacheSeedError) as captured:
            apply_cache_seed(plan, cache)

        assert captured.value.code == "destination_history_conflict"
        assert state.pdf_cache_object(plan.candidates[0].pdf_sha256) is None
        assert [row.pdf_sha256 for row in state.pdf_cache_source_history(source.source_id)] == [
            incompatible_digest
        ]


@pytest.mark.parametrize("suffix", ("-wal", "-shm", "-journal"))
def test_cache_seed_rejects_every_sqlite_sidecar(tmp_path: Path, suffix: str) -> None:
    root = tmp_path / "legacy"
    one_candidate_fixture(root)
    Path(str(root / "worker-state.sqlite3") + suffix).write_bytes(b"sidecar")

    with pytest.raises(CacheSeedError) as captured:
        build_cache_seed_plan(root)
    assert captured.value.code == "sqlite_sidecar_present"


def test_cache_seed_rejects_active_legacy_run(tmp_path: Path) -> None:
    root = tmp_path / "legacy"
    observed = datetime(2026, 1, 1, tzinfo=UTC)
    connection = initialize_legacy_root(root)
    insert_run(connection, run_id="run-active", observed_at=observed, status="running")
    finish_legacy(connection)

    with pytest.raises(CacheSeedError) as captured:
        build_cache_seed_plan(root)
    assert captured.value.code == "legacy_run_active"


def test_cache_seed_skips_proven_stale_running_run_and_its_untrusted_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "legacy"
    stale_time = datetime(2026, 1, 1, tzinfo=UTC)
    terminal_time = stale_time + timedelta(days=1)
    stale_source = source_record(
        product_code="STALE",
        source_url="https://cards.example/stale.pdf",
        source_post_id="stale-post",
    )
    terminal_source = source_record(discovered_at=terminal_time)
    connection = initialize_legacy_root(root)
    insert_run(connection, run_id="run-stale", observed_at=stale_time, status="running")
    insert_snapshot(
        connection,
        run_id="run-stale",
        observed_at=stale_time,
        sources=(stale_source,),
    )
    insert_run(connection, run_id="run-terminal", observed_at=terminal_time)
    insert_snapshot(
        connection,
        run_id="run-terminal",
        observed_at=terminal_time,
        sources=(terminal_source,),
    )
    finish_legacy(connection)
    stale_downloads = root / "runs" / "run-stale" / "downloads"
    stale_downloads.mkdir(parents=True)
    (stale_downloads / "untrusted-name").symlink_to(tmp_path / "outside")
    write_run_pdf(root, "run-terminal", "downloads", terminal_source, pdf_bytes())

    plan = build_cache_seed_plan(root)
    report = plan.report(applied=False)

    assert report["status"] == "verified"
    assert report["run_count"] == 1
    assert report["snapshot_count"] == 1
    assert report["source_occurrence_count"] == 1
    assert report["candidate_files"] == 1
    assert report["skipped_stale_runs"] == 1
    assert report["sample"][0]["run_id"] == "run-terminal"
    ledger = json.loads(plan.ledger_bytes)
    assert ledger["skipped_stale_run_ids"] == ["run-stale"]
    assert {item["run_id"] for item in ledger["accepted_candidates"]} == {"run-terminal"}
    assert all(item["run_id"] != "run-stale" for item in ledger["missing_sources"])


def test_cache_seed_blocks_latest_running_run_even_after_terminal_history(tmp_path: Path) -> None:
    root = tmp_path / "legacy"
    terminal_time = datetime(2026, 1, 1, tzinfo=UTC)
    active_time = terminal_time + timedelta(days=1)
    connection = initialize_legacy_root(root)
    insert_run(connection, run_id="run-terminal", observed_at=terminal_time)
    insert_run(connection, run_id="run-active", observed_at=active_time, status="running")
    finish_legacy(connection)

    with pytest.raises(CacheSeedError) as captured:
        build_cache_seed_plan(root)
    assert captured.value.code == "legacy_run_active"


def test_cache_seed_rejects_run_id_path_escape(tmp_path: Path) -> None:
    root = tmp_path / "legacy"
    observed = datetime(2026, 1, 1, tzinfo=UTC)
    connection = initialize_legacy_root(root)
    insert_run(connection, run_id="../escape", observed_at=observed)
    finish_legacy(connection)

    with pytest.raises(CacheSeedError) as captured:
        build_cache_seed_plan(root)
    assert captured.value.code == "invalid_legacy_database"


def test_cache_seed_requires_an_explicit_absolute_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "legacy"
    one_candidate_fixture(root)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(CacheSeedError) as captured:
        build_cache_seed_plan(Path("legacy"))
    assert captured.value.code == "invalid_legacy_root"


def test_cache_seed_rejects_snapshot_hash_or_count_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "legacy"
    one_candidate_fixture(root)
    connection = sqlite3.connect(root / "worker-state.sqlite3")
    connection.execute("UPDATE snapshot SET source_sha256=?", ("f" * 64,))
    connection.commit()
    connection.close()

    with pytest.raises(CacheSeedError) as captured:
        build_cache_seed_plan(root)
    assert captured.value.code == "invalid_snapshot"


@pytest.mark.parametrize("field", ("finished_at", "observed_at"))
def test_cache_seed_rejects_run_snapshot_time_mismatch(tmp_path: Path, field: str) -> None:
    root = tmp_path / "legacy"
    one_candidate_fixture(root)
    connection = sqlite3.connect(root / "worker-state.sqlite3")
    if field == "finished_at":
        connection.execute("UPDATE run SET finished_at=NULL")
        expected = "invalid_legacy_database"
    else:
        connection.execute(
            "UPDATE snapshot SET observed_at=?",
            (datetime(2030, 1, 1, tzinfo=UTC).isoformat(),),
        )
        expected = "invalid_snapshot"
    connection.commit()
    connection.close()

    with pytest.raises(CacheSeedError) as captured:
        build_cache_seed_plan(root)
    assert captured.value.code == expected


def test_cache_seed_rejects_unbound_or_invalid_download_before_apply(tmp_path: Path) -> None:
    root = tmp_path / "legacy"
    destination = tmp_path / "candidate"
    one_candidate_fixture(root)
    unbound = root / "runs" / "run-1" / "downloads" / f"source_{'f' * 64}.pdf"
    unbound.write_bytes(b"not a PDF")

    with pytest.raises(CacheSeedError) as captured:
        build_cache_seed_plan(root)
    assert captured.value.code == "unbound_legacy_download"
    assert not destination.exists()


@pytest.mark.parametrize("node_kind", ("symlink", "fifo"))
def test_cache_seed_rejects_symlink_or_special_pdf_node(tmp_path: Path, node_kind: str) -> None:
    root = tmp_path / "legacy"
    source, pdf_path, _ = one_candidate_fixture(root)
    body = pdf_path.read_bytes()
    pdf_path.unlink()
    if node_kind == "symlink":
        outside = tmp_path / "outside.pdf"
        outside.write_bytes(body)
        pdf_path.symlink_to(outside)
    else:
        os.mkfifo(pdf_path)

    with pytest.raises(CacheSeedError) as captured:
        build_cache_seed_plan(root)
    assert captured.value.code == "unsafe_legacy_pdf"
    assert source.source_id in pdf_path.name


def test_cache_seed_rejects_symlinked_root_without_following_it(tmp_path: Path) -> None:
    real_root = tmp_path / "real"
    one_candidate_fixture(real_root)
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(CacheSeedError) as captured:
        build_cache_seed_plan(linked_root)
    assert captured.value.code == "unsafe_legacy_path"


def test_cache_seed_report_sample_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "legacy"
    first = source_record(product_code="CARD-1", source_post_id="post-1")
    second = source_record(
        product_code="CARD-2",
        source_url="https://cards.example/card-2.pdf",
        source_post_id="post-2",
    )
    third = source_record(
        product_code="CARD-3",
        source_url="https://cards.example/card-3.pdf",
        source_post_id="post-3",
    )
    fourth = source_record(
        product_code="CARD-4",
        source_url="https://cards.example/card-4.pdf",
        source_post_id="post-4",
    )
    observed = datetime(2026, 1, 1, tzinfo=UTC)
    connection = initialize_legacy_root(root)
    insert_run(connection, run_id="run-1", observed_at=observed)
    insert_snapshot(
        connection,
        run_id="run-1",
        observed_at=observed,
        sources=(first, second, third, fourth),
    )
    finish_legacy(connection)
    write_run_pdf(root, "run-1", "downloads", first, pdf_bytes(width=600))
    write_run_pdf(root, "run-1", "downloads", second, pdf_bytes(width=601))
    monkeypatch.setattr(cache_seed_module, "REPORT_SAMPLE_LIMIT", 1)
    monkeypatch.setattr(cache_seed_module, "MISSING_SAMPLE_LIMIT", 1)

    report = build_cache_seed_plan(root).report(applied=False)

    assert len(report["sample"]) == 1
    assert report["sample_truncated"] == 1
    assert len(report["missing_sample"]) == 1
    assert report["missing_sample_truncated"] == 1
    assert report["ledger_missing_sources"] == 2
    assert len(json.dumps(report, ensure_ascii=False)) < 10_000


def test_cache_seed_rejects_ledger_over_size_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "legacy"
    one_candidate_fixture(root)
    monkeypatch.setattr(cache_seed_module, "MAX_LEDGER_BYTES", 1)

    with pytest.raises(CacheSeedError) as captured:
        build_cache_seed_plan(root)
    assert captured.value.code == "ledger_limit_exceeded"


def test_cache_seed_rejects_tampered_in_memory_ledger_identity_before_mutation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "legacy"
    destination = tmp_path / "candidate"
    source, _, _ = one_candidate_fixture(root)
    plan = build_cache_seed_plan(root)
    tampered = replace(plan, ledger_bytes=plan.ledger_bytes + b" ")

    with WorkerState(destination / "worker-state.sqlite3") as state:
        cache = PDFCache(destination, state)
        with pytest.raises(CacheSeedError) as captured:
            apply_cache_seed(tampered, cache)

        assert captured.value.code == "invalid_ledger_identity"
        assert state.pdf_cache_source_binding(source.source_id) is None


@pytest.mark.parametrize(
    ("node_kind", "expected_code"),
    (
        ("parent_symlink", "unsafe_audit_path"),
        ("ledger_symlink", "unsafe_audit_ledger"),
        ("ledger_fifo", "unsafe_audit_ledger"),
        ("ledger_hardlink", "unsafe_audit_ledger"),
        ("ledger_conflict", "audit_ledger_conflict"),
    ),
)
def test_cache_seed_audit_ledger_path_is_fail_closed_before_cache_mutation(
    tmp_path: Path,
    node_kind: str,
    expected_code: str,
) -> None:
    root = tmp_path / "legacy"
    destination = tmp_path / "candidate"
    source, _, _ = one_candidate_fixture(root)
    plan = build_cache_seed_plan(root)

    with WorkerState(destination / "worker-state.sqlite3") as state:
        cache = PDFCache(destination, state)
        audit_directory = destination / "audit-reports" / "cache-seed"
        if node_kind == "parent_symlink":
            outside = tmp_path / "outside-audit"
            outside.mkdir()
            (destination / "audit-reports").symlink_to(outside, target_is_directory=True)
        else:
            audit_directory.mkdir(parents=True)
            target = audit_directory / f"{plan.ledger_sha256}.json"
            if node_kind == "ledger_symlink":
                target.symlink_to(tmp_path / "outside-ledger")
            elif node_kind == "ledger_fifo":
                os.mkfifo(target)
            elif node_kind == "ledger_hardlink":
                outside = tmp_path / "outside-ledger"
                outside.write_bytes(plan.ledger_bytes)
                os.link(outside, target)
            else:
                target.write_bytes(b"conflicting ledger bytes")

        with pytest.raises(CacheSeedError) as captured:
            apply_cache_seed(plan, cache)

        assert captured.value.code == expected_code
        assert state.pdf_cache_source_binding(source.source_id) is None


def test_cache_seed_cli_defaults_to_dry_run_and_apply_requires_candidate_channel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "legacy"
    destination = tmp_path / "candidate"
    source, _, _ = one_candidate_fixture(root)
    monkeypatch.setenv("CARDRAG_WORKER_STATE_DIR", str(destination))
    runner = CliRunner()

    dry_run = runner.invoke(cli_module.app, ["cache-seed", str(root)])
    assert dry_run.exit_code == 0
    dry_report = json.loads(dry_run.stdout)
    assert dry_report["status"] == "verified"
    assert dry_report["ledger_path"] is None
    assert dry_report["ledger_accepted_candidates"] == 1
    assert dry_report["ledger_missing_sources"] == 1
    assert len(dry_report["ledger_sha256"]) == 64
    assert "product_name" not in dry_run.stdout
    assert '\n  "accepted_candidates":' not in dry_run.stdout
    assert not destination.exists()

    monkeypatch.setenv("CARDRAG_CHANNEL", "stable")
    blocked = runner.invoke(cli_module.app, ["cache-seed", str(root), "--apply"])
    assert blocked.exit_code == 1
    assert json.loads(blocked.stdout)["reason_code"] == "stable_destination_forbidden"
    assert not destination.exists()

    monkeypatch.setenv("CARDRAG_CHANNEL", "development")
    wrong_channel = runner.invoke(cli_module.app, ["cache-seed", str(root), "--apply"])
    assert wrong_channel.exit_code == 1
    assert json.loads(wrong_channel.stdout)["reason_code"] == "candidate_destination_required"
    assert not destination.exists()

    monkeypatch.setenv("CARDRAG_CHANNEL", "candidate-v1.0.9")
    applied = runner.invoke(cli_module.app, ["cache-seed", str(root), "--apply"])
    assert applied.exit_code == 0
    applied_report = json.loads(applied.stdout)
    assert applied_report["status"] == "applied"
    assert applied_report["applied_candidates"] == 1
    assert applied_report["reused_candidates"] == 0
    ledger_path = destination / applied_report["ledger_path"]
    ledger_before = ledger_path.read_bytes()
    assert len(ledger_before) == applied_report["ledger_size_bytes"]
    assert hashlib.sha256(ledger_before).hexdigest() == applied_report["ledger_sha256"]

    reapplied = runner.invoke(cli_module.app, ["cache-seed", str(root), "--apply"])
    assert reapplied.exit_code == 0
    reapplied_report = json.loads(reapplied.stdout)
    assert reapplied_report["status"] == "applied"
    assert reapplied_report["applied_candidates"] == 0
    assert reapplied_report["reused_candidates"] == 1
    assert reapplied_report["created_revisions"] == 0
    assert reapplied_report["ledger_path"] == applied_report["ledger_path"]
    assert reapplied_report["ledger_sha256"] == applied_report["ledger_sha256"]
    assert ledger_path.read_bytes() == ledger_before
    with WorkerState(destination / "worker-state.sqlite3") as state:
        assert state.pdf_cache_source_binding(source.source_id) is not None


def test_cache_seed_cli_failure_is_bounded_and_does_not_create_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "legacy"
    destination = tmp_path / "candidate"
    _, pdf_path, _ = one_candidate_fixture(root)
    pdf_path.write_bytes(b"SECRET_INVALID_LEGACY_PDF")
    monkeypatch.setenv("CARDRAG_CHANNEL", "candidate-v1.0.9")
    monkeypatch.setenv("CARDRAG_WORKER_STATE_DIR", str(destination))

    result = CliRunner().invoke(cli_module.app, ["cache-seed", str(root), "--apply"])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "applied_candidates": 0,
        "created_pdf_objects": 0,
        "created_revisions": 0,
        "dry_run": False,
        "ledger_path": None,
        "ledger_sha256": None,
        "ledger_size_bytes": 0,
        "reason_code": "invalid_legacy_pdf",
        "reused_candidates": 0,
        "schema_version": "cardrag.cache-seed-report.v1",
        "skipped_stale_runs": 0,
        "status": "blocked",
    }
    assert "SECRET_INVALID_LEGACY_PDF" not in result.stdout
    assert not destination.exists()


def test_cache_seed_cli_rejects_destination_overlap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "legacy"
    one_candidate_fixture(root)
    monkeypatch.setenv("CARDRAG_CHANNEL", "candidate-v1.0.9")
    monkeypatch.setenv("CARDRAG_WORKER_STATE_DIR", str(root / "candidate"))

    result = CliRunner().invoke(cli_module.app, ["cache-seed", str(root), "--apply"])

    assert result.exit_code == 1
    assert json.loads(result.stdout)["reason_code"] == "destination_overlaps_legacy_root"
    assert not (root / "candidate").exists()


def test_cache_seed_source_identity_mismatch_is_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "legacy"
    source, pdf_path, _ = one_candidate_fixture(root)
    changed = replace(source, product_name="changed")
    mismatched = pdf_path.with_name(f"{changed.source_id}.pdf")
    pdf_path.rename(mismatched)

    with pytest.raises(CacheSeedError) as captured:
        build_cache_seed_plan(root)
    assert captured.value.code == "unbound_legacy_download"


def test_cache_seed_rejects_database_change_between_plan_and_apply(tmp_path: Path) -> None:
    root = tmp_path / "legacy"
    destination = tmp_path / "candidate"
    one_candidate_fixture(root)
    plan = build_cache_seed_plan(root)
    connection = sqlite3.connect(root / "worker-state.sqlite3")
    connection.execute("UPDATE run SET error='changed after preflight'")
    connection.commit()
    connection.close()

    with WorkerState(destination / "worker-state.sqlite3") as state:
        cache = PDFCache(destination, state)
        with pytest.raises(CacheSeedError) as captured:
            apply_cache_seed(plan, cache)

        assert captured.value.code == "legacy_database_changed"
        assert state.pdf_cache_source_binding(plan.candidates[0].source.source_id) is None
