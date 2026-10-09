import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from cardrag_core.ocr import OCRInput, content_addressed_ocr_reuse_key
from test_ocr import PDF_SHA, FakeProvider, fake_render

from cardrag_worker.backup import BackupLedger
from cardrag_worker.ocr import OCRResolver, PriorLocalNativeSource
from cardrag_worker.settings import WorkerSettings
from cardrag_worker.state import WorkerState


@pytest.mark.asyncio
async def test_sealed_prior_content_without_native_manifest_resolves_provider_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FIX_06: Prior content/adopted document without native-manifest resolves via local cache with 0 provider calls."""
    monkeypatch.setattr("cardrag_worker.ocr.render_pdf", fake_render)
    runs_root = tmp_path / "runs"
    prior_run_id = "025ce35739944d8facfdda4c99961f0c"
    doc_id = "doc_test_content_001"
    prior_doc_ocr = runs_root / prior_run_id / "documents" / doc_id / "ocr"
    prior_doc_ocr.mkdir(parents=True)

    ocr_text = "## Page 1\n\n카드 상품 혜택 안내입니다.\n"
    ocr_bytes = ocr_text.encode("utf-8")
    ocr_sha = hashlib.sha256(ocr_bytes).hexdigest()
    ocr_size = len(ocr_bytes)
    (prior_doc_ocr / "ocr.md").write_bytes(ocr_bytes)
    # Crucially, native-manifest.json DOES NOT exist on disk!

    source = OCRInput(pdf_sha256=PDF_SHA, pdf_size_bytes=1000, page_count=1)
    reuse_key = content_addressed_ocr_reuse_key(source, cache_epoch=0)
    variant_id = "variant_12345"

    # Create sealed publish.json for prior run
    sealed_dir = runs_root / prior_run_id / "sealed"
    sealed_dir.mkdir(parents=True)
    manifest_data = {
        "generation_id": prior_run_id,
        "corpus_sha256": "a" * 64,
        "contract_sha256": "b" * 64,
        "created_at": "2026-10-08T18:00:00+00:00",
        "documents": [
            {
                "document_id": doc_id,
                "issuer": "shinhan",
                "availability": "available",
                "page_count": 1,
                "pdf": {
                    "sha256": PDF_SHA,
                    "size_bytes": 1000,
                    "media_type": "application/pdf",
                    "path": f"v1/objects/sha256/{PDF_SHA[:2]}/{PDF_SHA}",
                },
                "ocr": {
                    "sha256": ocr_sha,
                    "size_bytes": ocr_size,
                    "media_type": "text/markdown; charset=utf-8",
                    "path": f"v1/objects/sha256/{ocr_sha[:2]}/{ocr_sha}",
                },
                "ocr_cache_kind": "content",
                "ocr_reuse_key": reuse_key,
                "ocr_variant_id": variant_id,
            }
        ],
    }
    publish_data = {
        "schema_version": "cardrag.publish-seal.v1",
        "run_id": prior_run_id,
        "generation_id": prior_run_id,
        "manifest": manifest_data,
        "objects": [],
    }
    (sealed_dir / "publish.json").write_text(json.dumps(publish_data), encoding="utf-8")

    # In local mode (webdav=None)
    state = WorkerState(tmp_path / "state.sqlite3")
    provider = FakeProvider()
    resolver = OCRResolver(
        provider=provider,
        state=state,
        webdav=None,
        chunk_pages=1,
    )  # type: ignore[arg-type]

    state.start_run(run_id="run-2")
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_text("1", encoding="utf-8")


    output_dir = runs_root / "run-2" / "documents" / doc_id / "ocr"
    prior_source = PriorLocalNativeSource(
        runs_root=runs_root,
        run_id=prior_run_id,
        generation_id=prior_run_id,
        corpus_sha256="a" * 64,
        contract_sha256="b" * 64,
        document_id=doc_id,
        pdf_sha256=PDF_SHA,
        pdf_size_bytes=1000,
        page_count=1,
        ocr_sha256=ocr_sha,
        ocr_size_bytes=ocr_size,
        cache_kind="content",
        reuse_key=reuse_key,
        variant_id=variant_id,
    )

    result = await resolver.resolve(
        run_id="run-2",
        document_id=doc_id,
        pdf_path=pdf_path,
        pdf_sha256=PDF_SHA,
        pdf_size_bytes=1000,
        page_count=1,
        output_dir=output_dir,
        prior_local_native=prior_source,
    )

    assert result.cache_reused is True
    assert result.provider_called is False
    assert provider.calls == []  # Zero provider calls!
    assert result.ocr_sha256 == ocr_sha
    assert result.ocr_bytes == ocr_bytes
    assert result.cache_kind == "content"
    assert result.cache_variant_id is not None
    assert (output_dir / "ocr.md").read_bytes() == ocr_bytes


@pytest.mark.asyncio
async def test_corrupt_prior_ocr_fails_and_calls_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FIX_06: Corrupt prior OCR hash/bytes is rejected and falls back to provider."""
    monkeypatch.setattr("cardrag_worker.ocr.render_pdf", fake_render)
    runs_root = tmp_path / "runs"
    prior_run_id = "prior_run"
    doc_id = "doc_corrupt_001"
    prior_doc_ocr = runs_root / prior_run_id / "documents" / doc_id / "ocr"
    prior_doc_ocr.mkdir(parents=True)

    good_text = "## Page 1\n\n정상 내용입니다.\n"
    good_bytes = good_text.encode("utf-8")
    good_sha = hashlib.sha256(good_bytes).hexdigest()

    # Tampered on disk
    tampered_bytes = b"## Page 1\n\nTAMPERED\n"
    (prior_doc_ocr / "ocr.md").write_bytes(tampered_bytes)

    source = OCRInput(pdf_sha256=PDF_SHA, pdf_size_bytes=1000, page_count=1)
    reuse_key = content_addressed_ocr_reuse_key(source, cache_epoch=0)

    # Sealed manifest records good_sha
    sealed_dir = runs_root / prior_run_id / "sealed"
    sealed_dir.mkdir(parents=True)
    manifest_data = {
        "generation_id": prior_run_id,
        "corpus_sha256": "a" * 64,
        "contract_sha256": "b" * 64,
        "created_at": "2026-10-08T18:00:00+00:00",
        "documents": [
            {
                "document_id": doc_id,
                "issuer": "shinhan",
                "availability": "available",
                "page_count": 1,
                "pdf": {"sha256": PDF_SHA, "size_bytes": 1000, "media_type": "application/pdf", "path": "p"},
                "ocr": {"sha256": good_sha, "size_bytes": len(good_bytes), "media_type": "text/markdown; charset=utf-8", "path": "o"},
                "ocr_cache_kind": "content",
                "ocr_reuse_key": reuse_key,
                "ocr_variant_id": "v1",
            }
        ],
    }
    publish_data = {
        "schema_version": "cardrag.publish-seal.v1",
        "run_id": prior_run_id,
        "generation_id": prior_run_id,
        "manifest": manifest_data,
        "objects": [],
    }
    (sealed_dir / "publish.json").write_text(json.dumps(publish_data), encoding="utf-8")

    state = WorkerState(tmp_path / "state.sqlite3")
    provider = FakeProvider()
    resolver = OCRResolver(
        provider=provider,
        state=state,
        webdav=None,
        chunk_pages=1,
    )  # type: ignore[arg-type]

    state.start_run(run_id="run-2")
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_text("1", encoding="utf-8")

    output_dir = runs_root / "run-2" / "documents" / doc_id / "ocr"
    prior_source = PriorLocalNativeSource(
        runs_root=runs_root,
        run_id=prior_run_id,
        generation_id=prior_run_id,
        corpus_sha256="a" * 64,
        contract_sha256="b" * 64,
        document_id=doc_id,
        pdf_sha256=PDF_SHA,
        pdf_size_bytes=1000,
        page_count=1,
        ocr_sha256=good_sha,
        ocr_size_bytes=len(good_bytes),
        cache_kind="content",
        reuse_key=reuse_key,
        variant_id="v1",
    )

    result = await resolver.resolve(
        run_id="run-2",
        document_id=doc_id,
        pdf_path=pdf_path,
        pdf_sha256=PDF_SHA,
        pdf_size_bytes=1000,
        page_count=1,
        output_dir=output_dir,
        prior_local_native=prior_source,
    )

    # Provider MUST be called because prior OCR was corrupt!
    assert result.provider_called is True
    assert provider.calls == [1]


@pytest.mark.asyncio
async def test_backup_budget_reserve_commits_partial_batch_and_retry_zero_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FIX_05: Data budget timeout reserves time for index commit, and subsequent retry skips verified receipts."""
    from dataclasses import replace

    db_path = tmp_path / "backup-ledger.sqlite3"
    ledger = BackupLedger(db_path)
    spool_dir = tmp_path / "backup" / "spool"
    spool_dir.mkdir(parents=True)

    # Seed 4 items into backup_pending
    now = "2026-10-09T00:00:00+00:00"
    items = []
    with ledger._get_connection() as conn:
        for i in range(4):
            data = f"content_{i}".encode()
            h = hashlib.sha256(data).hexdigest()
            p = tmp_path / f"file_{i}.txt"
            p.write_bytes(data)
            items.append((f"item_{i}", h, len(data), str(p), f"v1/files/{h}.txt"))
            conn.execute(
                """
                INSERT INTO backup_pending
                (item_id, item_type, sha256, size_bytes, local_path, remote_path, media_type, created_at, status)
                VALUES (?, 'ocr', ?, ?, ?, ?, 'text/plain', ?, 'pending')
                """,
                (f"item_{i}", h, len(data), str(p), f"v1/files/{h}.txt", now),
            )

    settings = WorkerSettings.from_env(require_providers=False, require_webdav=False)
    settings = replace(
        settings,
        state_dir=tmp_path,
        webdav_base_url="https://webdav.example.com",
        webdav_username="user",
        webdav_password="pw",  # noqa: S106
    )

    call_counts = {"put_bytes": 0, "get_bytes": 0}

    class SlowMockWebDAV:
        def __init__(self) -> None:
            self.storage: dict[str, bytes] = {}

        async def put_bytes(self, path: str, body: bytes, *, content_type: str = "application/octet-stream") -> None:
            call_counts["put_bytes"] += 1
            self.storage[path] = body
            # Simulate slow upload on second item to consume data budget
            if "item_1" in path or call_counts["put_bytes"] == 2:
                await asyncio.sleep(0.05)

        async def get_bytes(self, path: str, *, max_bytes: int | None = None) -> bytes | None:
            call_counts["get_bytes"] += 1
            return self.storage.get(path)

        async def atomic_replace_bytes(self, path: str, body: bytes, *, content_type: str = "application/json") -> None:
            self.storage[path] = body

        async def close(self) -> None:
            pass

    mock_client = SlowMockWebDAV()

    # Flush 1: Give a small timeout_seconds = 0.08s
    # COMMIT_RESERVE_SECONDS logic gives max(5.0, 0.08 * 0.5) = 5.0 in test formula or tight data budget
    res1 = await ledger.flush(settings, mock_client, timeout_seconds=0.03, force=True)  # type: ignore[arg-type]
    # Check that partial index commit was executed and succeeded or degraded
    assert res1["status"] in {"degraded", "succeeded"}
    assert res1["flushed_count"] > 0
    flushed_first_round = res1["flushed_count"]

    # Verify remaining pending is reduced by flushed_first_round
    with ledger._get_connection() as conn:
        rem_pending = conn.execute("SELECT COUNT(*) FROM backup_pending WHERE status = 'pending'").fetchone()[0]
    assert rem_pending == 4 - flushed_first_round

    # Flush 2: Retry with remaining items
    puts_before = call_counts["put_bytes"]
    res2 = await ledger.flush(settings, mock_client, timeout_seconds=60.0, force=True)  # type: ignore[arg-type]
    assert res2["status"] == "succeeded"
    assert res2["remaining_pending"] == 0
    # Items already committed in round 1 were not re-uploaded!
    assert res2["flushed_count"] == 4 - flushed_first_round
    assert call_counts["put_bytes"] == puts_before + (4 - flushed_first_round)
