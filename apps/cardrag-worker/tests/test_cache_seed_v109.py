from __future__ import annotations

import hashlib
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from helpers import pdf_bytes

from cardrag_worker.cache_seed_v109 import (
    V109CacheSeedError,
    apply_v109_cache_seed,
    build_v109_cache_seed_plan,
    load_v109_seed_pins,
)
from cardrag_worker.pdf_cache import PDFCache, PDFSourceIdentity
from cardrag_worker.state import WorkerState


@dataclass(frozen=True)
class V109Fixture:
    root: Path
    identity: PDFSourceIdentity
    pdf_hashes: tuple[str, ...]
    object_paths: tuple[Path, ...]


def _identity(label: str = "card-1") -> PDFSourceIdentity:
    discovery_sha256 = hashlib.sha256(label.encode()).hexdigest()
    return PDFSourceIdentity(
        source_id=f"source_{discovery_sha256}",
        issuer="kb",
        product_code="CARD-1",
        document_type="product-manual",
        source_url=f"https://cards.example/{label}",
        source_version="2026-01",
        source_post_id=f"post-{label}",
        discovery_sha256=discovery_sha256,
    )


def _checkpoint_and_close(state: WorkerState) -> None:
    state.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    state.close()


def _terminal_fixture(tmp_path: Path, *, revisions: int = 2) -> V109Fixture:
    root = tmp_path / "v109-state"
    state = WorkerState(root / "worker-state.sqlite3")
    run_id = state.start_run(run_id="v109-terminal")
    cache = PDFCache(root, state)
    identity = _identity()
    observed = datetime(2026, 1, 1, tzinfo=UTC)
    pdf_hashes: list[str] = []
    object_paths: list[Path] = []
    for index in range(revisions):
        source_path = tmp_path / f"input-{index}.pdf"
        source_path.write_bytes(pdf_bytes(pages=index + 1, width=612 + index))
        digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
        cache.ingest_and_bind(
            identity,
            source_path,
            final_url=f"https://cdn.example/card-1-revision-{index}.pdf?token=secret-{index}",
            expected_sha256=digest,
            expected_size_bytes=source_path.stat().st_size,
            expected_page_count=index + 1,
            etag=f'"revision-{index}"',
            last_modified="Thu, 01 Jan 2026 00:00:00 GMT",
            replace_validators=True,
            observed_at=observed + timedelta(days=index),
            verified_at=observed + timedelta(days=index, hours=1),
        )
        if index == 0:
            # Exercise a validator refresh of the same byte revision so the
            # seed must preserve first/last observation boundaries exactly.
            cache.ingest_and_bind(
                identity,
                source_path,
                final_url=f"https://cdn.example/card-1-revision-{index}.pdf?token=secret-{index}",
                expected_sha256=digest,
                expected_size_bytes=source_path.stat().st_size,
                expected_page_count=index + 1,
                etag=f'"revision-{index}"',
                last_modified="Thu, 01 Jan 2026 00:00:00 GMT",
                replace_validators=True,
                observed_at=observed + timedelta(hours=12),
                verified_at=observed + timedelta(hours=13),
            )
        pdf_hashes.append(digest)
        object_paths.append(cache.object_path(digest))
    state.finish_run(run_id, "succeeded")
    _checkpoint_and_close(state)
    assert not Path(f"{root / 'worker-state.sqlite3'}-wal").exists()
    assert not Path(f"{root / 'worker-state.sqlite3'}-shm").exists()
    return V109Fixture(root, identity, tuple(pdf_hashes), tuple(object_paths))


def _tree_fingerprint(root: Path) -> tuple[tuple[str, int, int, str | None], ...]:
    result: list[tuple[str, int, int, str | None]] = []
    for path in sorted(root.rglob("*"), key=lambda item: str(item.relative_to(root))):
        relative = str(path.relative_to(root))
        info = path.lstat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if stat.S_ISREG(info.st_mode) else None
        result.append((relative, stat.S_IFMT(info.st_mode), info.st_mtime_ns, digest))
    return tuple(result)


def test_plan_and_apply_are_source_read_only_deterministic_and_idempotent(tmp_path: Path) -> None:
    fixture = _terminal_fixture(tmp_path)
    before = _tree_fingerprint(fixture.root)

    first_plan = build_v109_cache_seed_plan(fixture.root)
    second_plan = build_v109_cache_seed_plan(fixture.root)

    assert first_plan.report(applied=False) == second_plan.report(applied=False)
    assert first_plan.ledger_bytes == second_plan.ledger_bytes
    assert first_plan.accepted_pdf_hashes == frozenset(fixture.pdf_hashes)
    assert len(first_plan.accepted_revisions) == 2
    assert b"token=secret" not in first_plan.ledger_bytes
    assert b"https://" not in first_plan.ledger_bytes
    assert _tree_fingerprint(fixture.root) == before

    destination = tmp_path / "v111-state"
    with WorkerState(destination / "worker-state.sqlite3") as destination_state:
        destination_cache = PDFCache(destination, destination_state)
        first = apply_v109_cache_seed(first_plan, destination_cache)
        assert first["status"] == "applied"
        assert first["imported_pdf_objects"] == 2
        assert first["imported_revisions"] == 2
        assert first["reused_pdf_objects"] == 0
        assert first["reused_revisions"] == 0
        history = destination_state.pdf_cache_source_history(fixture.identity.source_id)
        assert tuple(item.pdf_sha256 for item in history) == fixture.pdf_hashes

        second = apply_v109_cache_seed(first_plan, destination_cache)
        assert second["imported_pdf_objects"] == 0
        assert second["imported_revisions"] == 0
        assert second["reused_pdf_objects"] == 2
        assert second["reused_revisions"] == 2

    assert load_v109_seed_pins(destination) == frozenset(fixture.pdf_hashes)
    assert _tree_fingerprint(fixture.root) == before


def test_latest_running_run_blocks_the_entire_seed(tmp_path: Path) -> None:
    root = tmp_path / "v109-running"
    state = WorkerState(root / "worker-state.sqlite3")
    state.start_run(run_id="still-running")
    _checkpoint_and_close(state)

    with pytest.raises(V109CacheSeedError, match="source_run_active") as error:
        build_v109_cache_seed_plan(root)
    assert error.value.code == "source_run_active"


def test_source_identity_supersession_is_replayed_in_lineage_order(tmp_path: Path) -> None:
    root = tmp_path / "v109-state"
    state = WorkerState(root / "worker-state.sqlite3")
    run_id = state.start_run(run_id="source-lineage")
    cache = PDFCache(root, state)
    first_identity = _identity("older")
    second_identity = _identity("newer")
    observed = datetime(2026, 2, 1, tzinfo=UTC)
    for index, identity in enumerate((first_identity, second_identity)):
        source_path = tmp_path / f"lineage-{index}.pdf"
        source_path.write_bytes(pdf_bytes(pages=index + 1, width=620 + index))
        digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
        cache.ingest_and_bind(
            identity,
            source_path,
            final_url=f"https://cdn.example/lineage-{index}.pdf",
            expected_sha256=digest,
            expected_size_bytes=source_path.stat().st_size,
            expected_page_count=index + 1,
            observed_at=observed + timedelta(days=index),
            verified_at=observed + timedelta(days=index),
        )
    state.finish_run(run_id, "succeeded")
    _checkpoint_and_close(state)

    plan = build_v109_cache_seed_plan(root)
    destination = tmp_path / "v111-state"
    with WorkerState(destination / "worker-state.sqlite3") as destination_state:
        apply_v109_cache_seed(plan, PDFCache(destination, destination_state))
        older = destination_state.pdf_cache_source_history(first_identity.source_id)
        newer = destination_state.pdf_cache_source_history(second_identity.source_id)
        assert older[0].superseded_by_source_id == second_identity.source_id
        assert newer[0].superseded_by_source_id is None


def test_pin_loader_without_seed_ledgers_returns_empty(tmp_path: Path) -> None:
    state_dir = tmp_path / "empty-state"
    state_dir.mkdir()
    assert load_v109_seed_pins(state_dir) == frozenset()


def test_missing_object_is_canonical_and_blocks_partial_source_history(tmp_path: Path) -> None:
    fixture = _terminal_fixture(tmp_path)
    fixture.object_paths[0].unlink()

    plan = build_v109_cache_seed_plan(fixture.root)

    missing_objects = [item for item in plan.objects if item.status == "missing"]
    assert [(item.pdf_sha256, item.missing_reason) for item in missing_objects] == [
        (fixture.pdf_hashes[0], "cas_object_missing")
    ]
    assert plan.sources[0].status == "missing"
    assert plan.sources[0].missing_reason == "source_history_incomplete"
    assert not plan.accepted_revisions
    assert {item.missing_reason for item in plan.revisions} == {
        "cas_object_missing",
        "source_history_incomplete",
    }
    assert plan.accepted_pdf_hashes == frozenset({fixture.pdf_hashes[1]})


def test_hash_mismatch_and_cas_symlink_fail_closed(tmp_path: Path) -> None:
    mismatch = _terminal_fixture(tmp_path / "mismatch")
    mismatch.object_paths[0].write_bytes(pdf_bytes(pages=4, width=700))
    with pytest.raises(V109CacheSeedError, match="source_pdf_validation_failed"):
        build_v109_cache_seed_plan(mismatch.root)

    linked = _terminal_fixture(tmp_path / "linked")
    target = tmp_path / "external.pdf"
    target.write_bytes(pdf_bytes())
    linked.object_paths[0].unlink()
    linked.object_paths[0].symlink_to(target)
    with pytest.raises(V109CacheSeedError, match="unsafe_source_cas"):
        build_v109_cache_seed_plan(linked.root)


def test_database_symlink_and_source_destination_overlap_fail_closed(tmp_path: Path) -> None:
    fixture = _terminal_fixture(tmp_path / "overlap")
    plan = build_v109_cache_seed_plan(fixture.root)

    class OverlappingCache:
        state_dir = fixture.root

    with pytest.raises(V109CacheSeedError, match="source_destination_overlap"):
        apply_v109_cache_seed(plan, cast(PDFCache, OverlappingCache()))

    linked = _terminal_fixture(tmp_path / "database-link")
    database = linked.root / "worker-state.sqlite3"
    outside = tmp_path / "outside.sqlite3"
    database.rename(outside)
    database.symlink_to(outside)
    with pytest.raises(V109CacheSeedError, match="source_database_missing_or_unsafe"):
        build_v109_cache_seed_plan(linked.root)


@pytest.mark.parametrize("attack", ["tamper", "symlink"])
def test_pin_loader_rejects_tampered_or_symlinked_ledger(tmp_path: Path, attack: str) -> None:
    fixture = _terminal_fixture(tmp_path)
    plan = build_v109_cache_seed_plan(fixture.root)
    destination = tmp_path / "v111-state"
    with WorkerState(destination / "worker-state.sqlite3") as destination_state:
        report = apply_v109_cache_seed(plan, PDFCache(destination, destination_state))
    relative = report["ledger_path"]
    assert isinstance(relative, str)
    ledger_path = destination / relative
    if attack == "tamper":
        ledger_path.write_bytes(ledger_path.read_bytes() + b" ")
    else:
        safe_copy = tmp_path / "ledger-copy.json"
        safe_copy.write_bytes(ledger_path.read_bytes())
        ledger_path.unlink()
        ledger_path.symlink_to(safe_copy)

    with pytest.raises(V109CacheSeedError, match="invalid_seed_ledger"):
        load_v109_seed_pins(destination)
