from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest
from conftest import create_database, install_generation

from cardrag_mcp.observability import Metrics
from cardrag_mcp.store import GenerationStore, cas_path
from cardrag_mcp.updater import (
    RemoteArtifact,
    RemoteDocument,
    RemoteGeneration,
    WebDAVUpdater,
)


def remote_generation(fixture) -> RemoteGeneration:
    database_body = fixture.database.read_bytes()
    return RemoteGeneration(
        generation_id=fixture.generation_id,
        database=RemoteArtifact(
            path=f"v1/generations/{fixture.generation_id}/index.sqlite3",
            sha256=hashlib.sha256(database_body).hexdigest(),
            size_bytes=len(database_body),
            media_type="application/vnd.sqlite3",
        ),
        documents=tuple(
            RemoteDocument(
                document_id=document_id,
                pdf=RemoteArtifact(
                    path=f"v1/objects/sha256/{digest[:2]}/{digest}",
                    sha256=digest,
                    size_bytes=size,
                    media_type="application/pdf",
                ),
            )
            for document_id, digest, size, _ in fixture.documents
        ),
        embedding_provider="openrouter",
        embedding_model="openai/text-embedding-3-small",
        embedding_dimension=1536,
        embedding_count=3 if len(fixture.documents) == 2 else 2,
    )


class FakeReader:
    def __init__(self, generation: RemoteGeneration, database: Path, objects: dict[str, bytes]):
        self.generation = generation
        self.database = database
        self.objects = objects
        self.corrupt_object = False
        self.closed = False

    async def read_stable_generation(self) -> RemoteGeneration:
        return self.generation

    async def download_database(self, generation: RemoteGeneration, destination: Path) -> None:
        assert generation.generation_id == self.generation.generation_id
        shutil.copyfile(self.database, destination)

    async def download_object(self, artifact: RemoteArtifact, destination: Path) -> None:
        body = self.objects[artifact.sha256]
        destination.write_bytes(b"corrupt" if self.corrupt_object else body)

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_updater_activates_only_after_database_and_all_pdfs_verify(tmp_path: Path) -> None:
    remote_root = tmp_path / "remote"
    first_fixture = create_database(remote_root / "g1.sqlite3", "gen-001")
    first = remote_generation(first_fixture)
    objects = {digest: body for _, digest, _, body in first_fixture.documents}
    reader = FakeReader(first, first_fixture.database, objects)
    store = GenerationStore(tmp_path / "state", maximum_vector_bytes=1024 * 1024)
    updater = WebDAVUpdater(reader, store, Metrics.create(), poll_seconds=300)

    assert await updater.poll_once() is True
    assert store.active_generation_id == "gen-001"
    assert await updater.poll_once() is False

    second_fixture = create_database(
        remote_root / "g2.sqlite3",
        "gen-002",
        suffix="-2",
    )
    reader.generation = remote_generation(second_fixture)
    reader.database = second_fixture.database
    reader.objects.update({digest: body for _, digest, _, body in second_fixture.documents})
    reader.corrupt_object = True
    with pytest.raises(RuntimeError, match="hash or size"):
        await updater.poll_once()
    assert store.active_generation_id == "gen-001"
    assert not (store.generations / "gen-002").exists()

    await updater.close()
    assert reader.closed is True


@pytest.mark.asyncio
async def test_promotion_budget_includes_active_and_pinned_vector_memory(tmp_path: Path) -> None:
    remote_root = tmp_path / "remote"
    store = GenerationStore(tmp_path / "state", maximum_vector_bytes=1024 * 1024)
    install_generation(store, "gen-001", suffix="-1", two_documents=False)
    second = create_database(remote_root / "g2.sqlite3", "gen-002", suffix="-2")
    remote = remote_generation(second)
    required = remote.embedding_count * (1536 * 4 + 4)
    store.maximum_vector_bytes = store.resident_vector_bytes + required - 1
    reader = FakeReader(
        remote,
        second.database,
        {digest: body for _, digest, _, body in second.documents},
    )
    updater = WebDAVUpdater(reader, store, Metrics.create(), poll_seconds=300)

    with pytest.raises(RuntimeError, match="resident/pinned vector memory"):
        await updater.poll_once()
    assert store.active_generation_id == "gen-001"
    assert not (store.generations / "gen-002").exists()


@pytest.mark.asyncio
async def test_updater_activates_verified_orphan_final_after_crash(tmp_path: Path) -> None:
    store = GenerationStore(tmp_path / "state", maximum_vector_bytes=1024 * 1024)
    fixture = install_generation(store, "gen-orphan", activate=False)
    reader = FakeReader(
        remote_generation(fixture),
        fixture.database,
        {digest: body for _, digest, _, body in fixture.documents},
    )
    updater = WebDAVUpdater(reader, store, Metrics.create(), poll_seconds=300)

    assert store.active_generation_id is None
    assert await updater.poll_once() is True
    assert store.active_generation_id == "gen-orphan"
    assert store.current_path.exists()


@pytest.mark.asyncio
async def test_updater_restores_missing_pdf_before_activating_orphan_final(
    tmp_path: Path,
) -> None:
    store = GenerationStore(tmp_path / "state", maximum_vector_bytes=1024 * 1024)
    fixture = install_generation(store, "gen-orphan", activate=False)
    missing_digest = fixture.documents[0][1]
    cas_path(store.objects, missing_digest).unlink()
    reader = FakeReader(
        remote_generation(fixture),
        fixture.database,
        {digest: body for _, digest, _, body in fixture.documents},
    )
    updater = WebDAVUpdater(reader, store, Metrics.create(), poll_seconds=300)

    assert await updater.poll_once() is True
    assert store.active_generation_id == "gen-orphan"
    assert cas_path(store.objects, missing_digest).is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt", [False, True])
async def test_unchanged_poll_repairs_missing_or_corrupt_active_pdf(
    tmp_path: Path,
    *,
    corrupt: bool,
) -> None:
    store = GenerationStore(tmp_path / "state", maximum_vector_bytes=1024 * 1024)
    fixture = install_generation(store, "gen-001")
    digest = fixture.documents[0][1]
    destination = cas_path(store.objects, digest)
    if corrupt:
        destination.write_bytes(b"%PDF-corrupt")
    else:
        destination.unlink()
    reader = FakeReader(
        remote_generation(fixture),
        fixture.database,
        {object_digest: body for _, object_digest, _, body in fixture.documents},
    )
    updater = WebDAVUpdater(reader, store, Metrics.create(), poll_seconds=300)

    assert await updater.poll_once() is False
    assert hashlib.sha256(destination.read_bytes()).hexdigest() == digest
    assert store.active_generation_id == "gen-001"


def test_restart_refuses_current_when_a_referenced_pdf_is_missing(tmp_path: Path) -> None:
    state = tmp_path / "state"
    store = GenerationStore(state, maximum_vector_bytes=1024 * 1024)
    fixture = install_generation(store, "gen-001")
    digest = fixture.documents[0][1]

    restarted = GenerationStore(state, maximum_vector_bytes=1024 * 1024)
    assert restarted.load_current() is True
    cas_path(store.objects, digest).unlink()
    broken_restart = GenerationStore(state, maximum_vector_bytes=1024 * 1024)
    assert broken_restart.load_current() is False
    assert broken_restart.active_generation_id is None


def test_retention_and_cas_gc_wait_for_generation_pin(tmp_path: Path) -> None:
    store = GenerationStore(
        tmp_path / "state",
        maximum_vector_bytes=1024 * 1024,
        retention=3,
    )
    first = install_generation(
        store,
        "gen-001",
        suffix="-1",
        two_documents=False,
    )
    first_object = cas_path(store.objects, first.documents[0][1])
    lease = store.pin()
    pinned = lease.__enter__()
    assert pinned.generation_id == "gen-001"
    try:
        for number in (2, 3, 4):
            install_generation(
                store,
                f"gen-00{number}",
                suffix=f"-{number}",
                two_documents=False,
            )
        assert (store.generations / "gen-001").exists()
        assert first_object.exists()
    finally:
        lease.__exit__(None, None, None)

    assert not (store.generations / "gen-001").exists()
    assert not first_object.exists()
    assert len([path for path in store.generations.iterdir() if path.is_dir()]) == 3


def test_disk_retention_does_not_keep_inactive_vector_matrices_resident(tmp_path: Path) -> None:
    store = GenerationStore(
        tmp_path / "state",
        maximum_vector_bytes=1024 * 1024,
        retention=3,
    )
    for number in (1, 2, 3):
        install_generation(
            store,
            f"gen-00{number}",
            suffix=f"-{number}",
            two_documents=False,
        )

    assert len([path for path in store.generations.iterdir() if path.is_dir()]) == 3
    assert tuple(store._entries) == ("gen-003",)


def test_pinned_previous_matrix_is_released_after_request_finishes(tmp_path: Path) -> None:
    store = GenerationStore(
        tmp_path / "state",
        maximum_vector_bytes=1024 * 1024,
        retention=3,
    )
    install_generation(store, "gen-001", suffix="-1", two_documents=False)
    lease = store.pin()
    lease.__enter__()
    install_generation(store, "gen-002", suffix="-2", two_documents=False)
    assert set(store._entries) == {"gen-001", "gen-002"}

    lease.__exit__(None, None, None)
    assert tuple(store._entries) == ("gen-002",)
