from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from cardrag_core import (
    ArtifactRef,
    EmbeddingContract,
    GenerationCounts,
    GenerationDocument,
    GenerationManifest,
    GenerationPointer,
    GenerationReady,
    sha256_bytes,
)
from cardrag_mcp.transport import LocalArtifactReader

from cardrag_worker.backup import BackupLedger
from cardrag_worker.local_publisher import LocalServingTransport
from cardrag_worker.settings import WorkerSettings


def create_sample_generation(
    gen_id: str,
) -> tuple[GenerationManifest, GenerationReady, GenerationPointer, bytes, bytes]:
    db_bytes = f"db content for {gen_id}".encode()
    db_sha = sha256_bytes(db_bytes)
    pdf_bytes = f"%PDF content for {gen_id}".encode()
    pdf_sha = sha256_bytes(pdf_bytes)

    doc = GenerationDocument(
        document_id=f"doc-{gen_id}",
        issuer="kb",
        pdf=ArtifactRef(
            sha256=pdf_sha,
            size_bytes=len(pdf_bytes),
            media_type="application/pdf",
            path=f"v1/objects/sha256/{pdf_sha[:2]}/{pdf_sha}",
        ),
        page_count=1,
    )

    manifest = GenerationManifest(
        generation_id=gen_id,
        created_at=datetime.now(UTC),
        serving_database=ArtifactRef(
            sha256=db_sha,
            size_bytes=len(db_bytes),
            media_type="application/vnd.sqlite3",
            path=f"v1/generations/{gen_id}/index.sqlite3",
        ),
        corpus_sha256="c" * 64,
        contract_sha256="d" * 64,
        embedding_contract=EmbeddingContract(
            provider="test",
            model="test",
            dimension=1536,
            count=1,
        ),
        issuer_codes=("kb",),
        counts=GenerationCounts(documents=1, pdf_objects=1, ocr_objects=0, chunks=1),
        documents=(doc,),
    )

    ready = GenerationReady(
        generation_id=gen_id,
        manifest_sha256=manifest.manifest_sha256,
        serving_database_sha256=db_sha,
        serving_database_size_bytes=len(db_bytes),
    )

    pointer = GenerationPointer(
        generation_id=gen_id,
        manifest_sha256=manifest.manifest_sha256,
        ready_sha256=sha256_bytes(ready.canonical_bytes()),
    )

    return manifest, ready, pointer, db_bytes, pdf_bytes


@pytest.mark.asyncio
async def test_local_serving_transport_publish(tmp_path: Path) -> None:
    serving_dir = tmp_path / "serving"
    transport = LocalServingTransport(serving_dir, channel="stable")

    assert await transport.validated_current_generation() is None
    assert await transport.observed_pointer_bytes() is None

    manifest1, ready1, pointer1, db1, pdf1 = create_sample_generation("gen-001")
    pdf_sha1 = manifest1.documents[0].pdf.sha256

    db_file = tmp_path / "index.sqlite3"
    db_file.write_bytes(db1)
    pdf_file = tmp_path / "doc.pdf"
    pdf_file.write_bytes(pdf1)

    unique_objects = [
        (pdf_file, "application/pdf", pdf_sha1, len(pdf1)),
    ]

    # Successful publication of gen-001
    bundle = await transport.publish(
        generation_id="gen-001",
        database=db_file,
        manifest=manifest1.model_dump(mode="json"),
        vectors=None,
        unique_objects=unique_objects,
    )
    assert bundle.generation_id == "gen-001"

    cur1 = await transport.validated_current_generation()
    assert cur1 is not None
    assert cur1.generation_id == "gen-001"
    obs_bytes = await transport.observed_pointer_bytes()
    assert obs_bytes is not None

    # Verify get_bytes and get_json
    raw_manifest = await transport.get_json("v1/generations/gen-001/manifest.json")
    assert raw_manifest["generation_id"] == "gen-001"

    # Verify MCP LocalArtifactReader reads from this volume directly
    reader = LocalArtifactReader(serving_dir, channel="stable")
    mcp_remote = await reader.read_stable_generation()
    assert mcp_remote is not None
    assert mcp_remote.generation_id == "gen-001"

    dest_db = tmp_path / "mcp_db.sqlite3"
    await reader.download_database(mcp_remote, dest_db)
    assert dest_db.read_bytes() == db1


def test_worker_settings_local_mode_without_webdav(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("CARDRAG_STATE_DIR", str(state_dir))
    monkeypatch.setenv("CARDRAG_PUBLICATION_TRANSPORT", "local")
    monkeypatch.delenv("CARDRAG_WEBDAV_BASE_URL", raising=False)
    monkeypatch.delenv("CARDRAG_WEBDAV_USERNAME", raising=False)
    monkeypatch.delenv("CARDRAG_WEBDAV_PASSWORD", raising=False)

    # WorkerSettings should load cleanly with publication_transport="local" and no WebDAV settings
    settings = WorkerSettings.from_env(require_providers=False, require_webdav=False)
    assert settings.publication_transport == "local"
    assert settings.backup_mode == "disabled"
    assert settings.serving_dir == Path("/var/lib/cardrag-serving")


@pytest.mark.asyncio
async def test_backup_ledger_triggers_and_flush(tmp_path: Path) -> None:
    db_path = tmp_path / "backup-ledger.sqlite3"
    ledger = BackupLedger(db_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    ocr_cache_dir = state_dir / "cache" / "ocr"
    ocr_cache_dir.mkdir(parents=True)

    # Create dummy settings
    settings = MagicMock()
    settings.state_dir = state_dir
    settings.backup_mode = "hybrid"
    settings.backup_every_runs = 7
    settings.backup_new_ocr_count = 30
    settings.backup_new_bytes = 1024 * 1024 * 1024  # 1GiB
    settings.backup_max_pending_age_hours = 168
    settings.backup_derived_snapshot_enabled = False
    settings.backup_inline_budget_seconds = 300.0
    settings.webdav_base_url = "https://webdav.example.com"
    settings.webdav_username = "test"
    settings.webdav_password = "password"  # noqa: S105
    settings.webdav_upload_chunk_mib = 8

    # Initial state: 0 pending, should not trigger
    status = ledger.get_status(settings)
    assert status["pending_count"] == 0
    assert not status["should_trigger"]

    # Write 5 dummy OCR cache items
    for i in range(5):
        key = f"key_{i}"
        item_dir = ocr_cache_dir / key
        item_dir.mkdir(parents=True)
        (item_dir / "ocr.json").write_text('{"text": "ocr content"}')

    ledger.record_run_success("run-1", state_dir, settings)
    status = ledger.get_status(settings)
    assert status["pending_count"] == 5
    assert status["runs_since_last_backup"] == 1
    assert not status["should_trigger"]

    # Hybrid trigger 1: runs >= 7
    with ledger._get_connection() as conn:
        conn.execute("UPDATE backup_meta SET value = '7' WHERE key = 'runs_since_backup'")
    status = ledger.get_status(settings)
    assert status["runs_since_last_backup"] == 7
    assert status["should_trigger"] is True
    assert any("runs_threshold_met" in r for r in status["trigger_reasons"])

    # Reset runs
    with ledger._get_connection() as conn:
        conn.execute("UPDATE backup_meta SET value = '1' WHERE key = 'runs_since_backup'")

    # Hybrid trigger 2: count >= 30
    settings.backup_new_ocr_count = 5
    status = ledger.get_status(settings)
    assert status["should_trigger"] is True
    assert any("ocr_count_threshold_met" in r for r in status["trigger_reasons"])
    settings.backup_new_ocr_count = 30

    # Hybrid trigger 3: age >= 168 hours
    old_time = time.time() - (200 * 3600)
    with ledger._get_connection() as conn:
        conn.execute("UPDATE backup_pending SET created_at = ?", (old_time,))
    status = ledger.get_status(settings)
    assert status["should_trigger"] is True
    assert any("age_threshold_met" in r for r in status["trigger_reasons"])

    # Reset age
    with ledger._get_connection() as conn:
        conn.execute("UPDATE backup_pending SET created_at = ?", (time.time(),))

    # Test flush with mocked WebDAV client
    uploaded_paths: list[str] = []

    class MockWebDAVClient:
        def __init__(self) -> None:
            self.storage: dict[str, bytes] = {}

        async def put(self, path: str, content: bytes | Any) -> None:
            uploaded_paths.append(path)
            self.storage[path] = content if isinstance(content, bytes) else str(content).encode()

        async def get(self, path: str) -> bytes:
            return self.storage.get(path, b"")

        async def close(self) -> None:
            pass

    mock_client = MockWebDAVClient()
    ledger._make_webdav_client = MagicMock(return_value=mock_client)  # type: ignore

    flush_result = await ledger.flush(settings, force=True)
    assert flush_result["status"] == "succeeded"
    assert flush_result["flushed_count"] == 5
    assert len(uploaded_paths) == 5

    # After flush, pending_count should be 0 and runs_since_last_backup reset to 0
    status_after = ledger.get_status(settings)
    assert status_after["pending_count"] == 0
    assert status_after["runs_since_last_backup"] == 0
    assert status_after["last_backup_at"] is not None

    # Audit check
    audit_res = await ledger.audit(settings)
    assert audit_res["total_receipts"] == 5
    assert audit_res["verified_receipts"] == 5
    assert audit_res["missing_receipts"] == 0

    # Restore check to a target dir
    restore_target = tmp_path / "restored_ocr"
    restore_res = await ledger.restore(settings, target_dir=restore_target)
    assert restore_res["restored_count"] == 5
    assert (restore_target / "v1" / "caches" / "ocr" / "key_0" / "ocr.json").exists()


def test_backup_cli_commands(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from cardrag_worker.cli import app

    state_dir = tmp_path / "cli_state"
    state_dir.mkdir()
    monkeypatch.setenv("CARDRAG_STATE_DIR", str(state_dir))
    monkeypatch.setenv("CARDRAG_PUBLICATION_TRANSPORT", "local")
    monkeypatch.delenv("CARDRAG_WEBDAV_BASE_URL", raising=False)
    monkeypatch.delenv("CARDRAG_WEBDAV_USERNAME", raising=False)
    monkeypatch.delenv("CARDRAG_WEBDAV_PASSWORD", raising=False)

    runner = CliRunner()

    # 1. status command
    res_status = runner.invoke(app, ["backup", "status"])
    assert res_status.exit_code == 0
    assert "pending_count" in res_status.stdout

    # 2. flush command
    res_flush = runner.invoke(app, ["backup", "flush"])
    assert res_flush.exit_code == 0

    # 3. audit command
    res_audit = runner.invoke(app, ["backup", "audit"])
    assert res_audit.exit_code == 0

    # 4. restore command
    restore_dest = tmp_path / "cli_restore"
    res_restore = runner.invoke(app, ["backup", "restore", "--target-dir", str(restore_dest)])
    assert res_restore.exit_code == 0
