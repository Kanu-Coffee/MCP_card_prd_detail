from __future__ import annotations

import asyncio
import hashlib
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from cardrag_core import ArtifactRef, EmbeddingContract, GenerationCounts, GenerationManifest

import cardrag_worker.webdav as webdav_module
from cardrag_worker.webdav import (
    WebDAVBundlePublisher,
    WebDAVClient,
    WebDAVError,
    _guard_v5_stable_publication,
)


def test_v5_stable_publisher_primitive_requires_explicit_capability() -> None:
    denied = type("Client", (), {"channel": "stable", "stable_publication_approved": False})()
    with pytest.raises(ValueError, match="stable v1.0.14 publication requires explicit approval"):
        _guard_v5_stable_publication(denied, schema_version="cardrag.generation.v5")

    approved = type("Client", (), {"channel": "stable", "stable_publication_approved": True})()
    candidate = type("Client", (), {"channel": "candidate-v1.0.11"})()
    _guard_v5_stable_publication(approved, schema_version="cardrag.generation.v5")
    _guard_v5_stable_publication(candidate, schema_version="cardrag.generation.v5")
    _guard_v5_stable_publication(denied, schema_version="cardrag.generation.v4")


async def _wait_until_set(event: threading.Event) -> None:
    for _ in range(1_000):
        if event.is_set():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("blocking WebDAV operation did not start")


@pytest.mark.asyncio
async def test_validated_current_generation_stream_verifies_v5_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf = ArtifactRef.for_cas(
        sha256="a" * 64,
        size_bytes=11,
        media_type="application/pdf",
    )
    ocr = ArtifactRef.for_cas(
        sha256="b" * 64,
        size_bytes=12,
        media_type="text/markdown; charset=utf-8",
    )
    manifest = SimpleNamespace(
        schema_version="cardrag.generation.v5",
        serving_schema="cardrag.serving-db.v5",
        generation_id="generation-stream-verified",
        corpus_sha256="c" * 64,
        contract_sha256="d" * 64,
        documents=(
            SimpleNamespace(pdf=pdf, ocr=ocr, availability="available"),
            SimpleNamespace(pdf=pdf, ocr=None, availability="ocr_failed"),
        ),
    )
    current = SimpleNamespace(manifest=manifest)
    capability = object()
    calls: list[tuple[str, object]] = []

    class Core:
        def read_only(self) -> object:
            return capability

    class Reader:
        def __init__(self, reader: object, *, channel: str) -> None:
            assert reader is capability
            assert channel == "candidate-v1.0.11"

        def read_current_generation(self) -> object:
            calls.append(("current", None))
            return current

        def verify_serving_database(self, *, current: object) -> None:
            calls.append(("database", current))

        def verify_vector_sidecar(self, *, current: object) -> None:
            calls.append(("vector", current))

        def verify_object(self, reference: ArtifactRef) -> None:
            calls.append(("object", reference))

    def reject_temporary_directory(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("current generation verification must not allocate temporary storage")

    monkeypatch.setattr(webdav_module, "MCPArtifactReader", Reader)
    monkeypatch.setattr(tempfile, "TemporaryDirectory", reject_temporary_directory)
    client = WebDAVClient(Core(), channel="candidate-v1.0.11")  # type: ignore[arg-type]

    identity = await client.validated_current_generation()

    assert identity is not None
    assert (
        identity.generation_id,
        identity.corpus_sha256,
        identity.contract_sha256,
        identity.generation_schema,
        identity.serving_schema,
        identity.ocr_failed_document_count,
    ) == (
        manifest.generation_id,
        manifest.corpus_sha256,
        manifest.contract_sha256,
        manifest.schema_version,
        manifest.serving_schema,
        1,
    )
    assert calls == [
        ("current", None),
        ("database", current),
        ("vector", current),
        ("object", pdf),
        ("object", ocr),
    ]


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
        def publish_file(
            self,
            path: object,
            source: Path,
            *,
            media_type: str,
            expected_sha256: str,
            expected_size_bytes: int,
        ) -> ArtifactRef:
            assert hashlib.sha256(source.read_bytes()).hexdigest() == expected_sha256
            assert source.stat().st_size == expected_size_bytes
            return ArtifactRef(
                sha256=expected_sha256,
                size_bytes=expected_size_bytes,
                media_type=media_type,
                path=str(path),
            )

        def publish_bytes(
            self,
            path: object,
            body: bytes,
            *,
            media_type: str,
        ) -> ArtifactRef:
            return ArtifactRef(
                sha256=hashlib.sha256(body).hexdigest(),
                size_bytes=len(body),
                media_type=media_type,
                path=str(path),
            )

    class Stable:
        def atomic_replace_bytes(self, body: bytes) -> ArtifactRef:
            pointer_started.set()
            if not pointer_release.wait(timeout=5):
                raise AssertionError("test did not release stable pointer MOVE")
            pointer_committed.set()
            return ArtifactRef(
                sha256=hashlib.sha256(body).hexdigest(),
                size_bytes=len(body),
                media_type="application/json",
                path="v1/channels/stable.json",
            )

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


@pytest.mark.asyncio
async def test_bundle_rejects_mismatched_member_reference_before_control_objects(
    tmp_path: Path,
) -> None:
    database = tmp_path / "index.sqlite3"
    database.write_bytes(b"sealed database")
    database_body = database.read_bytes()
    generation_id = "g-returned-identity-mismatch"
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
    control_calls: list[str] = []

    class Immutable:
        def publish_file(
            self,
            path: object,
            _source: Path,
            *,
            media_type: str,
            expected_sha256: str,
            expected_size_bytes: int,
        ) -> ArtifactRef:
            assert expected_sha256 == manifest.serving_database.sha256
            assert expected_size_bytes == manifest.serving_database.size_bytes
            return ArtifactRef(
                sha256="f" * 64,
                size_bytes=expected_size_bytes,
                media_type=media_type,
                path=str(path),
            )

        def publish_bytes(self, *_args: Any, **_kwargs: Any) -> ArtifactRef:
            control_calls.append("immutable control")
            raise AssertionError("control object must not be published")

    class Stable:
        def atomic_replace_bytes(self, _body: bytes) -> ArtifactRef:
            control_calls.append("pointer")
            raise AssertionError("pointer must not be published")

    client = type("Client", (), {"immutable": Immutable(), "stable": Stable()})()
    with pytest.raises(WebDAVError, match="serving database publisher returned"):
        await WebDAVBundlePublisher(client).publish(  # type: ignore[arg-type]
            generation_id=generation_id,
            database=database,
            manifest=manifest.model_dump(mode="json"),
        )

    assert control_calls == []
