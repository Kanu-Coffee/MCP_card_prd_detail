from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from cardrag_core import (
    EMBEDDING_VIEW_TYPES,
    ArtifactRef,
    CurrentGeneration,
    EmbeddingContract,
    EmbeddingProfile,
    EmbeddingVectorSidecar,
    EmbeddingViewCount,
    GenerationCounts,
    GenerationDocument,
    GenerationManifest,
    GenerationPointer,
    GenerationReady,
    IssuerParserProfile,
    StructureContract,
    StructureMajorClassCounts,
    StructureNodeCounts,
    StructureRevisionCounts,
    StructureSourceCoverage,
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


def current_v5_generation() -> CurrentGeneration:
    current = current_generation()
    profile = EmbeddingProfile.qwen3(provider_id="deepinfra", maximum_tokens=8192)
    vector = ArtifactRef(
        sha256="f" * 64,
        size_bytes=4096 * 4,
        media_type="application/octet-stream",
        path=f"v1/generations/{current.manifest.generation_id}/vectors.f32",
    )
    manifest = current.manifest.model_copy(
        update={
            "schema_version": "cardrag.generation.v5",
            "serving_schema": "cardrag.serving-db.v5",
            "embedding_contract": EmbeddingContract(
                provider="openrouter",
                model="qwen/qwen3-embedding-8b",
                dimension=4096,
                count=1,
            ),
            "vector_sidecar": EmbeddingVectorSidecar(
                artifact=vector,
                profile_id=profile.profile_id,
                row_count=1,
                dimension=4096,
                dtype="float32",
                byte_order="little-endian",
                layout="row-major",
                normalization="l2",
            ),
            "structure_contract": StructureContract(
                schema_version="cardrag.structure.v2",
                parser_profiles=(
                    IssuerParserProfile(
                        issuer="woori",
                        profile_id="cardrag.issuer-profile.woori.v1",
                        profile_sha256="1" * 64,
                    ),
                ),
                node_counts=StructureNodeCounts(
                    total=1,
                    root=1,
                    major_section=0,
                    item=0,
                    paragraph=0,
                    list_item=0,
                    table=0,
                    table_row=0,
                    footnote=0,
                    boilerplate=0,
                    unclassified=0,
                ),
                major_class_counts=StructureMajorClassCounts(
                    total=0,
                    benefit=0,
                    notice=0,
                    mixed=0,
                    unknown=0,
                ),
                source_coverage=StructureSourceCoverage(
                    source_non_whitespace_characters=1,
                    covered_non_whitespace_characters=1,
                    source_non_whitespace_sha256="2" * 64,
                    covered_non_whitespace_sha256="2" * 64,
                ),
                revision_counts=StructureRevisionCounts(
                    total=1,
                    current=1,
                    superseded=0,
                    ambiguous=0,
                ),
                cross_contract_parent_count=0,
                cross_contract_link_count=0,
                lineages_with_multiple_current_revisions=0,
            ),
            "embedding_profiles": (profile,),
            "primary_embedding_profile_id": profile.profile_id,
            "embedding_view_counts": tuple(
                EmbeddingViewCount(
                    view_type=view_type,
                    count=1 if view_type == "TITLE" else 0,
                )
                for view_type in EMBEDDING_VIEW_TYPES
            ),
            "parser_policy_sha256": "3" * 64,
            "embedding_policy_sha256": "4" * 64,
            "retrieval_policy_sha256": "5" * 64,
        }
    )
    ready = current.ready.model_copy(
        update={
            "manifest_sha256": manifest.manifest_sha256,
            "vector_sidecar_sha256": vector.sha256,
            "vector_sidecar_size_bytes": vector.size_bytes,
        }
    )
    return CurrentGeneration(pointer=current.pointer, ready=ready, manifest=manifest)


class Facade:
    def __init__(self) -> None:
        self.calls = 0
        self.value = current_generation()
        self.vector_download_current: CurrentGeneration | None = None

    def read_current_generation(self) -> CurrentGeneration:
        self.calls += 1
        return self.value

    def download_vector_sidecar(
        self,
        destination: Path,
        *,
        current: CurrentGeneration,
    ) -> None:
        self.vector_download_current = current
        destination.write_bytes(b"vector-sidecar")


class Client:
    def __init__(self) -> None:
        self.etag = '"etag-1"'
        self.closed = False

    def head(self, _path):
        return SimpleNamespace(etag=self.etag)

    def close(self) -> None:
        self.closed = True


class BlockingDownloadFacade(Facade):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def download_object(
        self,
        _reference: ArtifactRef,
        destination: Path,
    ) -> None:
        self.started.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("test download was not released")
        destination.write_bytes(b"bounded-download")


@pytest.mark.asyncio
async def test_stable_etag_skips_control_files_but_not_first_verified_read() -> None:
    facade = Facade()
    client = Client()
    reader = CoreArtifactReader(facade, client)

    first = await reader.read_stable_generation()
    second = await reader.read_stable_generation()
    assert first == second
    assert first.serving_schema == "cardrag.serving-db.v3"
    assert first.corpus_sha256 == "c" * 64
    assert first.contract_sha256 == "d" * 64
    assert first.issuer_codes == ("woori",)
    assert first.document_count == 1
    assert first.documents[0].issuer == "woori"
    assert first.documents[0].page_count == 1
    assert facade.calls == 1

    client.etag = '"etag-2"'
    await reader.read_stable_generation()
    assert facade.calls == 2
    await reader.close()
    assert client.closed is True


@pytest.mark.asyncio
async def test_v5_sidecar_mapping_and_download_remain_bound_to_verified_current(
    tmp_path: Path,
) -> None:
    facade = Facade()
    facade.value = current_v5_generation()
    reader = CoreArtifactReader(facade, Client())

    remote = await reader.read_stable_generation()
    destination = tmp_path / "vectors.f32"
    await reader.download_vector_sidecar(remote, destination)

    assert remote.serving_schema == "cardrag.serving-db.v5"
    assert remote.embedding_dimension == 4096
    assert remote.vector_sidecar is not None
    assert remote.vector_sidecar.path.endswith("/vectors.f32")
    assert remote.vector_sidecar.size_bytes == 4096 * 4
    assert remote.structure_contract == facade.value.manifest.structure_contract
    assert remote.embedding_profiles == facade.value.manifest.embedding_profiles
    assert remote.primary_embedding_profile_id == (
        facade.value.manifest.primary_embedding_profile_id
    )
    assert remote.vector_sidecar_contract == facade.value.manifest.vector_sidecar
    assert remote.parser_policy_sha256 == "3" * 64
    assert destination.read_bytes() == b"vector-sidecar"
    assert facade.vector_download_current is facade.value

    changed = replace(remote, generation_id="different-generation")
    with pytest.raises(RuntimeError, match="stable generation changed"):
        await reader.download_vector_sidecar(changed, destination)


@pytest.mark.asyncio
async def test_download_cancellation_waits_for_blocking_writer_to_finish(tmp_path: Path) -> None:
    facade = BlockingDownloadFacade()
    reader = CoreArtifactReader(facade, Client())
    remote = await reader.read_stable_generation()
    destination = tmp_path / "object.pdf"
    task = asyncio.create_task(reader.download_object(remote.documents[0].pdf, destination))
    assert await asyncio.to_thread(facade.started.wait, 1)

    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    facade.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert destination.read_bytes() == b"bounded-download"


@pytest.mark.asyncio
async def test_local_artifact_reader_reads_generation_and_downloads_files(tmp_path: Path) -> None:
    from cardrag_core import sha256_bytes

    from cardrag_mcp.transport import LocalArtifactReader, RemoteArtifact

    serving_dir = tmp_path / "serving"
    serving_dir.mkdir(parents=True)
    base = current_v5_generation()
    gen_id = base.manifest.generation_id

    db_bytes = b"sqlite3 database test content"
    db_sha = sha256_bytes(db_bytes)
    vec_bytes = b"v" * (4096 * 4)
    vec_sha = sha256_bytes(vec_bytes)
    pdf_bytes = b"%PDF-test-pdf-bytes"
    pdf_sha = sha256_bytes(pdf_bytes)

    assert base.manifest.vector_sidecar is not None
    new_doc = base.manifest.documents[0].model_copy(
        update={
            "availability": "available",
            "ocr": ArtifactRef(
                sha256="c" * 64,
                size_bytes=10,
                media_type="text/markdown; charset=utf-8",
                path=f"v1/objects/sha256/{'c' * 2}/{'c' * 64}",
            ),
            "pdf": ArtifactRef(
                sha256=pdf_sha,
                size_bytes=len(pdf_bytes),
                media_type="application/pdf",
                path=f"v1/objects/sha256/{pdf_sha[:2]}/{pdf_sha}",
            ),
        }
    )
    new_sidecar = base.manifest.vector_sidecar.model_copy(
        update={
            "artifact": ArtifactRef(
                sha256=vec_sha,
                size_bytes=len(vec_bytes),
                media_type="application/octet-stream",
                path=f"v1/generations/{gen_id}/vectors.f32",
            ),
        }
    )
    from cardrag_core import IssuerOCRCounts

    manifest_data = base.manifest.model_copy(
        update={
            "serving_database": ArtifactRef(
                sha256=db_sha,
                size_bytes=len(db_bytes),
                media_type="application/vnd.sqlite3",
                path=f"v1/generations/{gen_id}/index.sqlite3",
            ),
            "vector_sidecar": new_sidecar,
            "counts": GenerationCounts(documents=1, pdf_objects=1, ocr_objects=1, chunks=1),
            "issuer_ocr_counts": (
                IssuerOCRCounts(issuer="woori", acquired=1, succeeded=1, failed=0),
            ),
            "documents": (new_doc,),
        }
    )
    manifest_bytes = manifest_data.canonical_bytes()
    manifest_sha = manifest_data.manifest_sha256

    ready_data = GenerationReady(
        generation_id=gen_id,
        manifest_sha256=manifest_sha,
        serving_database_sha256=db_sha,
        serving_database_size_bytes=len(db_bytes),
        vector_sidecar_sha256=vec_sha,
        vector_sidecar_size_bytes=len(vec_bytes),
    )
    ready_bytes = ready_data.canonical_bytes()
    ready_sha = sha256_bytes(ready_bytes)

    pointer_data = GenerationPointer(
        generation_id=gen_id,
        manifest_sha256=manifest_sha,
        ready_sha256=ready_sha,
    )
    pointer_bytes = pointer_data.canonical_bytes()

    # Write files to serving_dir
    gen_dir = serving_dir / "v1" / "generations" / gen_id
    gen_dir.mkdir(parents=True)
    (gen_dir / "manifest.json").write_bytes(manifest_bytes)
    (gen_dir / "READY.json").write_bytes(ready_bytes)
    (gen_dir / "index.sqlite3").write_bytes(db_bytes)
    (gen_dir / "vectors.f32").write_bytes(vec_bytes)

    obj_dir = serving_dir / "v1" / "objects" / "sha256" / pdf_sha[:2]
    obj_dir.mkdir(parents=True)
    (obj_dir / pdf_sha).write_bytes(pdf_bytes)

    channel_dir = serving_dir / "v1" / "channels"
    channel_dir.mkdir(parents=True)
    (channel_dir / "stable.json").write_bytes(pointer_bytes)

    reader = LocalArtifactReader(serving_dir, channel="stable")
    remote = await reader.read_stable_generation()
    assert remote is not None
    assert remote.generation_id == gen_id
    assert remote.serving_schema == "cardrag.serving-db.v5"

    dest_db = tmp_path / "downloaded_db.sqlite3"
    await reader.download_database(remote, dest_db)
    assert dest_db.read_bytes() == db_bytes

    dest_vec = tmp_path / "downloaded_vec.f32"
    await reader.download_vector_sidecar(remote, dest_vec)
    assert dest_vec.read_bytes() == vec_bytes

    dest_pdf = tmp_path / "downloaded_doc.pdf"
    remote_art = RemoteArtifact(
        sha256=pdf_sha,
        size_bytes=len(pdf_bytes),
        path=f"v1/objects/sha256/{pdf_sha[:2]}/{pdf_sha}",
        media_type="application/pdf",
    )
    await reader.download_object(remote_art, dest_pdf)
    assert dest_pdf.read_bytes() == pdf_bytes


@pytest.mark.asyncio
async def test_local_artifact_reader_missing_channel_returns_none(tmp_path: Path) -> None:
    from cardrag_mcp.transport import LocalArtifactReader

    serving_dir = tmp_path / "empty_serving"
    serving_dir.mkdir()
    reader = LocalArtifactReader(serving_dir, channel="stable")
    assert await reader.read_stable_generation() is None


@pytest.mark.asyncio
async def test_local_artifact_reader_rejects_path_traversal(tmp_path: Path) -> None:
    from cardrag_mcp.transport import LocalArtifactReader, RemoteArtifact

    serving_dir = tmp_path / "serving"
    serving_dir.mkdir()
    reader = LocalArtifactReader(serving_dir, channel="stable")
    escaped_art = RemoteArtifact(
        sha256="a" * 64,
        size_bytes=10,
        path="../../etc/passwd",
        media_type="application/octet-stream",
    )
    dest = tmp_path / "escaped.out"
    with pytest.raises((RuntimeError, ValueError)):
        await reader.download_object(escaped_art, dest)
