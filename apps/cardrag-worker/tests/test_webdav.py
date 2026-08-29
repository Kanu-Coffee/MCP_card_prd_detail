from __future__ import annotations

import asyncio
import hashlib
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from cardrag_core import ArtifactRef, EmbeddingContract, GenerationCounts, GenerationManifest

from cardrag_worker.webdav import WebDAVBundlePublisher


async def _wait_until_set(event: threading.Event) -> None:
    for _ in range(1_000):
        if event.is_set():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("blocking WebDAV operation did not start")


@pytest.mark.asyncio
async def test_bundle_pointer_move_finishes_before_cancellation_propagates(tmp_path: Path) -> None:
    database = tmp_path / "index.sqlite3"
    database.write_bytes(b"sealed database")
    database_body = database.read_bytes()
    generation_id = "g-cancellation-fence"
    manifest = GenerationManifest(
        generation_id=generation_id,
        created_at=datetime(2026, 8, 29, tzinfo=UTC),
        serving_database=ArtifactRef(
            sha256=hashlib.sha256(database_body).hexdigest(),
            size_bytes=len(database_body),
            media_type="application/vnd.sqlite3",
            path=f"v1/generations/{generation_id}/index.sqlite3",
        ),
        corpus_sha256="a" * 64,
        contract_sha256="b" * 64,
        embedding_contract=EmbeddingContract(
            provider="test",
            model="test",
            dimension=1536,
            count=0,
        ),
        issuer_codes=("kb",),
        counts=GenerationCounts(documents=0, pdf_objects=0, ocr_objects=0, chunks=0),
    )
    pointer_started = threading.Event()
    pointer_release = threading.Event()
    pointer_committed = threading.Event()

    class Immutable:
        def publish_file(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def publish_bytes(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    class Stable:
        def atomic_replace_bytes(self, _body: bytes) -> None:
            pointer_started.set()
            if not pointer_release.wait(timeout=5):
                raise AssertionError("test did not release stable pointer MOVE")
            pointer_committed.set()

    client = type("Client", (), {"immutable": Immutable(), "stable": Stable()})()
    task = asyncio.create_task(
        WebDAVBundlePublisher(client).publish(  # type: ignore[arg-type]
            generation_id=generation_id,
            database=database,
            manifest=manifest.model_dump(mode="json"),
        )
    )
    await _wait_until_set(pointer_started)
    task.cancel()
    await asyncio.sleep(0)

    assert not task.done()
    assert not pointer_committed.is_set()
    pointer_release.set()
    with pytest.raises(asyncio.CancelledError) as captured:
        await task

    assert pointer_committed.is_set()
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
