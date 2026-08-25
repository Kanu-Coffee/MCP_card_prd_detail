from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from cardrag_core import (
    ArtifactRef,
    CurrentGeneration,
    EmbeddingContract,
    GenerationCounts,
    GenerationDocument,
    GenerationManifest,
    GenerationPointer,
    GenerationReady,
)

from cardrag_mcp.transport import CoreArtifactReader


def current_generation() -> CurrentGeneration:
    generation_id = "gen-001"
    database = ArtifactRef(
        sha256="a" * 64,
        size_bytes=100,
        media_type="application/vnd.sqlite3",
        path=f"v1/generations/{generation_id}/index.sqlite3",
    )
    pdf = ArtifactRef(
        sha256="b" * 64,
        size_bytes=20,
        media_type="application/pdf",
        path=f"v1/objects/sha256/bb/{'b' * 64}",
    )
    manifest = GenerationManifest(
        generation_id=generation_id,
        created_at=datetime.now(UTC),
        serving_database=database,
        corpus_sha256="c" * 64,
        contract_sha256="d" * 64,
        embedding_contract=EmbeddingContract(
            provider="openrouter",
            model="openai/text-embedding-3-small",
            dimension=1536,
            count=1,
        ),
        issuer_codes=("woori",),
        counts=GenerationCounts(documents=1, pdf_objects=1, ocr_objects=0, chunks=1),
        documents=(
            GenerationDocument(
                document_id="doc-1",
                issuer="woori",
                pdf=pdf,
                page_count=1,
            ),
        ),
    )
    ready = GenerationReady(
        generation_id=generation_id,
        manifest_sha256=manifest.manifest_sha256,
        serving_database_sha256=database.sha256,
        serving_database_size_bytes=database.size_bytes,
    )
    pointer = GenerationPointer(
        generation_id=generation_id,
        manifest_sha256=manifest.manifest_sha256,
        ready_sha256="e" * 64,
    )
    return CurrentGeneration(pointer=pointer, ready=ready, manifest=manifest)


class Facade:
    def __init__(self) -> None:
        self.calls = 0
        self.value = current_generation()

    def read_current_generation(self) -> CurrentGeneration:
        self.calls += 1
        return self.value


class Client:
    def __init__(self) -> None:
        self.etag = '"etag-1"'
        self.closed = False

    def head(self, _path):
        return SimpleNamespace(etag=self.etag)

    def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_stable_etag_skips_control_files_but_not_first_verified_read() -> None:
    facade = Facade()
    client = Client()
    reader = CoreArtifactReader(facade, client)

    first = await reader.read_stable_generation()
    second = await reader.read_stable_generation()
    assert first == second
    assert facade.calls == 1

    client.etag = '"etag-2"'
    await reader.read_stable_generation()
    assert facade.calls == 2
    await reader.close()
    assert client.closed is True
