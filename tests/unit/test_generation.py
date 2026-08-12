from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from cardrag.generation import (
    CurrentPointer,
    GenerationManifest,
    GenerationStore,
    GenerationVerificationError,
    new_generation_id,
)
from cardrag.search.generation_store import ActiveGenerationMismatch, GenerationPinnedPostgresStore

NOW = datetime(2026, 8, 12, 12, tzinfo=UTC)


def _generation_id(index: int) -> str:
    return new_generation_id(
        NOW + timedelta(seconds=index),
        entropy=f"{index:012x}",
    )


def _manifest(
    store: GenerationStore,
    candidate: Path,
    generation_id: str,
    *,
    latest_document_count: int = 1,
    latest_ocr_count: int | None = None,
) -> GenerationManifest:
    return GenerationManifest(
        generation_id=generation_id,
        created_at=NOW,
        source_snapshot_ids=("snapshot-fixture",),
        document_count=latest_document_count,
        latest_document_count=latest_document_count,
        latest_pdf_count=latest_document_count,
        latest_ocr_count=latest_document_count if latest_ocr_count is None else latest_ocr_count,
        latest_structure_count=latest_document_count,
        latest_embedding_count=latest_document_count,
        latest_index_count=latest_document_count,
        historical_quarantine_count=0,
        embedding_provider="fixture",
        embedding_model="fixture-embedding-v1",
        embedding_dimension=8,
        chunk_policy="fixture-chunk-v1",
        taxonomy_version="fixture-taxonomy-v1",
        files=store.build_file_inventory(candidate),
        quality_report_sha256=hashlib.sha256(b"quality").hexdigest(),
        retrieval_report_sha256=hashlib.sha256(b"retrieval").hexdigest(),
    )


def _sealed_generation(store: GenerationStore, index: int, body: bytes | None = None) -> tuple[str, Path]:
    generation_id = _generation_id(index)
    candidate = store.candidate_path(generation_id)
    (candidate / "catalog.json").write_bytes(body or f'{{"generation":{index}}}'.encode())
    manifest = _manifest(store, candidate, generation_id)
    return generation_id, store.seal(candidate, manifest)


def test_generation_requires_complete_latest_coverage(tmp_path: Path) -> None:
    store = GenerationStore(tmp_path / "published", tmp_path / "build")
    generation_id = _generation_id(1)
    candidate = store.candidate_path(generation_id)
    (candidate / "catalog.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValidationError, match="coverage must be 100%"):
        _manifest(
            store,
            candidate,
            generation_id,
            latest_document_count=2,
            latest_ocr_count=1,
        )


def test_seal_rejects_changed_inventory_and_checksum_tampering(tmp_path: Path) -> None:
    store = GenerationStore(tmp_path / "published", tmp_path / "build")
    generation_id = _generation_id(1)
    candidate = store.candidate_path(generation_id)
    artifact = candidate / "catalog.json"
    artifact.write_text('{"state":"original"}', encoding="utf-8")
    stale_manifest = _manifest(store, candidate, generation_id)
    artifact.write_text('{"state":"changed"}', encoding="utf-8")

    with pytest.raises(GenerationVerificationError, match="inventory differs"):
        store.seal(candidate, stale_manifest)

    valid_id, sealed = _sealed_generation(store, 2)
    sealed_artifact = sealed / "catalog.json"
    # Simulate privileged/offline storage corruption despite the normal read-only seal.
    os.chmod(sealed_artifact, 0o640)
    sealed_artifact.write_text('{"state":"tampered"}', encoding="utf-8")
    with pytest.raises(GenerationVerificationError, match="checksum mismatch"):
        store.verify_path(sealed, expected_generation_id=valid_id)


def test_seal_removes_write_bits_from_every_published_artifact(tmp_path: Path) -> None:
    store = GenerationStore(tmp_path / "published", tmp_path / "build")
    _, sealed = _sealed_generation(store, 1)

    assert sealed.stat().st_mode & 0o222 == 0
    assert (sealed / "catalog.json").stat().st_mode & 0o222 == 0
    assert (sealed / "manifest.json").stat().st_mode & 0o222 == 0
    assert (sealed / "READY").stat().st_mode & 0o222 == 0


def test_publish_pins_each_request_and_rollback_is_atomic(tmp_path: Path) -> None:
    store = GenerationStore(tmp_path / "published", tmp_path / "build")
    first_id, _ = _sealed_generation(store, 1)
    second_id, _ = _sealed_generation(store, 2)
    first_pointer = store.publish(first_id)

    with store.open_current() as (pinned_pointer, pinned_path):
        second_pointer = store.publish(second_id)
        assert pinned_pointer == first_pointer
        assert pinned_path.name == first_id
        assert json.loads((pinned_path / "catalog.json").read_text())["generation"] == 1
        assert second_pointer.previous_generation_id == first_id

    assert store.current().generation_id == second_id
    rollback = store.rollback()
    assert rollback.generation_id == first_id
    assert rollback.previous_generation_id == second_id
    assert not list(store.root.glob(".current.json.*.tmp"))


def test_publish_restores_pointer_when_history_append_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = GenerationStore(tmp_path / "published", tmp_path / "build")
    first_id, _ = _sealed_generation(store, 1)
    second_id, _ = _sealed_generation(store, 2)
    first_pointer = store.publish(first_id)
    original_open = Path.open

    def fail_history(path: Path, mode: str = "r", *args: object, **kwargs: object) -> object:
        if path == store.history_path and mode == "a+b":
            raise OSError("injected history outage")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_history)
    with pytest.raises(GenerationVerificationError, match="pointer was restored"):
        store.publish(second_id)

    assert store.current() == first_pointer


def test_current_rejects_pointer_manifest_checksum_mismatch(tmp_path: Path) -> None:
    store = GenerationStore(tmp_path / "published", tmp_path / "build")
    generation_id, _ = _sealed_generation(store, 1)
    pointer = store.publish(generation_id)
    corrupted = CurrentPointer(
        generation_id=pointer.generation_id,
        manifest_sha256="0" * 64,
        published_at=pointer.published_at,
        previous_generation_id=pointer.previous_generation_id,
    )
    store.current_path.write_text(corrupted.model_dump_json(), encoding="utf-8")

    with pytest.raises(GenerationVerificationError, match="manifest checksum"):
        store.current()


def test_retention_keeps_latest_three_and_any_pinned_generation(tmp_path: Path) -> None:
    store = GenerationStore(tmp_path / "published", tmp_path / "build")
    generations: list[str] = []
    for index in range(1, 6):
        generation_id, path = _sealed_generation(store, index)
        modified = (NOW - timedelta(days=6 - index)).timestamp()
        os.utime(path, (modified, modified))
        generations.append(generation_id)
    store.publish(generations[-1])

    removed = store.prune(pinned={generations[0]}, keep_successful=3, now=NOW)

    assert removed == [generations[1]]
    assert (store.generations / generations[0]).exists()
    assert all((store.generations / item).exists() for item in generations[2:])


def test_failed_candidate_is_retained_for_seven_days_then_removed(tmp_path: Path) -> None:
    store = GenerationStore(tmp_path / "published", tmp_path / "build")
    failed_id = _generation_id(1)
    failed = store.candidate_path(failed_id)
    (failed / "diagnostic.json").write_text('{"status":"failed"}', encoding="utf-8")

    six_days_old = (NOW - timedelta(days=6)).timestamp()
    os.utime(failed, (six_days_old, six_days_old))
    assert store.prune(now=NOW) == []
    assert failed.exists()

    eight_days_old = (NOW - timedelta(days=8)).timestamp()
    os.utime(failed, (eight_days_old, eight_days_old))
    assert store.prune(now=NOW) == [failed_id]
    assert not failed.exists()


class _GenerationCursor:
    def __init__(self, row: dict[str, object]) -> None:
        self.row = row

    def __enter__(self) -> _GenerationCursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, _: str) -> None:
        return None

    def fetchone(self) -> dict[str, object]:
        return self.row


class _GenerationConnection:
    def __init__(self, row: dict[str, object]) -> None:
        self.row = row

    def __enter__(self) -> _GenerationConnection:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def cursor(self) -> _GenerationCursor:
        return _GenerationCursor(self.row)


class _GenerationDatabase:
    def __init__(self, row: dict[str, object]) -> None:
        self.row = row

    def connection(self) -> _GenerationConnection:
        return _GenerationConnection(self.row)


def _active_row(store: GenerationStore, generation_id: str) -> dict[str, object]:
    pointer = store.current()
    manifest = store.verify_path(store.generations / generation_id)
    return {
        "generation_id": generation_id,
        "state": "published",
        "manifest_sha256": pointer.manifest_sha256,
        "schema_version": manifest.schema_version,
        "embedding_provider": manifest.embedding_provider,
        "embedding_model": manifest.embedding_model,
        "embedding_dimension": manifest.embedding_dimension,
        "latest_document_count": manifest.latest_document_count,
        "latest_covered_count": manifest.latest_index_count,
        "document_snapshot_count": manifest.document_count,
        "latest_snapshot_count": manifest.latest_document_count,
        "incompatible_snapshot_count": 0,
        "uncovered_latest_snapshot_count": 0,
    }


@pytest.mark.asyncio
async def test_online_generation_requires_file_db_manifest_and_runtime_embedding_agreement(
    tmp_path: Path,
) -> None:
    store = GenerationStore(tmp_path / "published", tmp_path / "build")
    generation_id, _ = _sealed_generation(store, 1)
    store.publish(generation_id)
    row = _active_row(store, generation_id)
    online = GenerationPinnedPostgresStore(
        _GenerationDatabase(row),  # type: ignore[arg-type]
        generation_store=store,
        embedding_provider="fixture",
        embedding_model="fixture-embedding-v1",
        embedding_dimension=8,
    )

    assert await online.active_generation_id() == generation_id

    mismatched = GenerationPinnedPostgresStore(
        _GenerationDatabase({**row, "generation_id": _generation_id(2)}),  # type: ignore[arg-type]
        generation_store=store,
        embedding_provider="fixture",
        embedding_model="fixture-embedding-v1",
        embedding_dimension=8,
    )
    with pytest.raises(ActiveGenerationMismatch, match="seal cannot be verified"):
        await mismatched.active_generation_id()

    wrong_model = GenerationPinnedPostgresStore(
        _GenerationDatabase(row),  # type: ignore[arg-type]
        generation_store=store,
        embedding_provider="fixture",
        embedding_model="different-model",
        embedding_dimension=8,
    )
    with pytest.raises(ActiveGenerationMismatch, match="embedding configuration"):
        await wrong_model.active_generation_id()

    incomplete_snapshot = GenerationPinnedPostgresStore(
        _GenerationDatabase({**row, "uncovered_latest_snapshot_count": 1}),  # type: ignore[arg-type]
        generation_store=store,
        embedding_provider="fixture",
        embedding_model="fixture-embedding-v1",
        embedding_dimension=8,
    )
    with pytest.raises(ActiveGenerationMismatch, match="coverage is incomplete"):
        await incomplete_snapshot.active_generation_id()
