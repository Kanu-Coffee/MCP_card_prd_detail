from __future__ import annotations

import hashlib
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from cardrag_core import (
    EMBEDDING_VIEW_TYPES,
    ArtifactRef,
    DocumentAggregationBootstrap,
    DocumentAggregationProfile,
    EmbeddingProfile,
    EmbeddingVectorSidecar,
    EmbeddingViewCount,
    IssuerParserProfile,
    StructureContract,
    StructureMajorClassCounts,
    StructureNodeCounts,
    StructureRevisionCounts,
    StructureSourceCoverage,
    Top3MeanAggregationDefinition,
    canonical_sha256,
    sealed_v5_retrieval_policy,
)
from conftest import create_database, install_generation
from v5_fixtures import V5Fixture, build_v5_fixture

import cardrag_mcp.quota as quota_module
import cardrag_mcp.store as store_module
import cardrag_mcp.updater as updater_module
from cardrag_mcp.models import ServingMetadata
from cardrag_mcp.observability import Metrics
from cardrag_mcp.quota import StorageQuotaError
from cardrag_mcp.schema_v5 import LoadedVectorsV5
from cardrag_mcp.store import GenerationHandle, GenerationStore, cas_path, load_generation_handle
from cardrag_mcp.updater import (
    RemoteArtifact,
    RemoteDocument,
    RemoteGeneration,
    WebDAVUpdater,
)


def remote_generation(fixture) -> RemoteGeneration:
    database_body = fixture.database.read_bytes()
    is_v4 = fixture.serving_schema == "cardrag.serving-db.v4"
    return RemoteGeneration(
        generation_id=fixture.generation_id,
        serving_schema=fixture.serving_schema,
        corpus_sha256=fixture.corpus_sha256,
        contract_sha256=fixture.contract_sha256,
        database=RemoteArtifact(
            path=f"v1/generations/{fixture.generation_id}/index.sqlite3",
            sha256=hashlib.sha256(database_body).hexdigest(),
            size_bytes=len(database_body),
            media_type="application/vnd.sqlite3",
        ),
        documents=tuple(
            RemoteDocument(
                document_id=document_id,
                issuer=issuer,
                page_count=page_count,
                pdf=RemoteArtifact(
                    path=f"v1/objects/sha256/{digest[:2]}/{digest}",
                    sha256=digest,
                    size_bytes=size,
                    media_type="application/pdf",
                ),
                ocr_sha256="e" * 64 if is_v4 else None,
            )
            for (document_id, digest, size, _), (_, issuer, page_count) in zip(
                fixture.documents,
                fixture.document_contracts,
                strict=True,
            )
        ),
        issuer_codes=fixture.issuer_codes,
        document_count=len(fixture.documents),
        pdf_object_count=len({digest for _, digest, _, _ in fixture.documents}),
        ocr_object_count=1 if is_v4 else 0,
        chunk_count=3 if len(fixture.documents) == 2 else 2,
        embedding_provider="openrouter",
        embedding_model="openai/text-embedding-3-small",
        embedding_dimension=1536,
        embedding_count=3 if len(fixture.documents) == 2 else 2,
        issuer_ocr_counts=(
            tuple(
                (
                    issuer,
                    sum(row[1] == issuer for row in fixture.document_contracts),
                    sum(row[1] == issuer for row in fixture.document_contracts),
                    0,
                )
                for issuer in fixture.issuer_codes
            )
            if is_v4
            else ()
        ),
    )


def remote_v5_generation(
    database: Path,
    vector_body: bytes,
    *,
    generation_id: str = "gen-v5",
) -> RemoteGeneration:
    database_body = database.read_bytes()
    profile = EmbeddingProfile.qwen3(provider_id="deepinfra", maximum_tokens=8192)
    vector_artifact = ArtifactRef(
        path=f"v1/generations/{generation_id}/vectors.f32",
        sha256=hashlib.sha256(vector_body).hexdigest(),
        size_bytes=len(vector_body),
        media_type="application/octet-stream",
    )
    structure_contract = StructureContract.model_construct(
        schema_version="cardrag.structure.v2",
        parser_profiles=(),
        node_counts=StructureNodeCounts(
            total=0,
            root=0,
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
            source_non_whitespace_characters=0,
            covered_non_whitespace_characters=0,
            source_non_whitespace_sha256="0" * 64,
            covered_non_whitespace_sha256="0" * 64,
        ),
        revision_counts=StructureRevisionCounts(
            total=0,
            current=0,
            superseded=0,
            ambiguous=0,
        ),
        cross_contract_parent_count=0,
        cross_contract_link_count=0,
        lineages_with_multiple_current_revisions=0,
    )
    return RemoteGeneration(
        generation_id=generation_id,
        serving_schema="cardrag.serving-db.v5",
        corpus_sha256=hashlib.sha256(f"corpus:{generation_id}".encode()).hexdigest(),
        contract_sha256=hashlib.sha256(f"contract:{generation_id}".encode()).hexdigest(),
        database=RemoteArtifact(
            path=f"v1/generations/{generation_id}/index.sqlite3",
            sha256=hashlib.sha256(database_body).hexdigest(),
            size_bytes=len(database_body),
            media_type="application/vnd.sqlite3",
        ),
        documents=(),
        issuer_codes=(),
        document_count=0,
        pdf_object_count=0,
        ocr_object_count=0,
        chunk_count=1,
        embedding_provider="openrouter",
        embedding_model="qwen/qwen3-embedding-8b",
        embedding_dimension=4096,
        embedding_count=1,
        issuer_ocr_counts=(),
        vector_sidecar=RemoteArtifact(
            path=vector_artifact.path,
            sha256=vector_artifact.sha256,
            size_bytes=vector_artifact.size_bytes,
            media_type=vector_artifact.media_type,
        ),
        structure_contract=structure_contract,
        embedding_profiles=(profile,),
        primary_embedding_profile_id=profile.profile_id,
        embedding_view_counts=tuple(
            EmbeddingViewCount(view_type=view_type, count=1 if view_type == "TITLE" else 0)
            for view_type in EMBEDDING_VIEW_TYPES
        ),
        vector_sidecar_contract=EmbeddingVectorSidecar(
            artifact=vector_artifact,
            profile_id=profile.profile_id,
            row_count=1,
            dimension=4096,
            dtype="float32",
            byte_order="little-endian",
            layout="row-major",
            normalization="l2",
        ),
        parser_policy_sha256="a" * 64,
        embedding_policy_sha256="b" * 64,
        retrieval_policy_sha256="c" * 64,
    )


def remote_generation_from_v5_fixture(fixture: V5Fixture) -> RemoteGeneration:
    database_body = fixture.database.read_bytes()
    vector_body = fixture.vectors.read_bytes()
    with sqlite3.connect(fixture.database) as connection:
        metadata = dict(connection.execute("SELECT key,value FROM metadata"))
        document_rows = connection.execute(
            """SELECT r.document_id,l.issuer,r.page_count,r.pdf_sha256,r.pdf_size_bytes
                 FROM contract_revisions AS r
                 JOIN product_lineages AS l
                   ON l.product_lineage_id=r.product_lineage_id
                ORDER BY r.document_id"""
        ).fetchall()
    profile = EmbeddingProfile.qwen3(provider_id="deepinfra", maximum_tokens=8192)
    vector_artifact = ArtifactRef(
        path=f"v1/generations/{fixture.generation_id}/vectors.f32",
        sha256=hashlib.sha256(vector_body).hexdigest(),
        size_bytes=len(vector_body),
        media_type="application/octet-stream",
    )
    documents = tuple(
        RemoteDocument(
            document_id=str(row[0]),
            issuer=str(row[1]),
            page_count=int(row[2]),
            pdf=RemoteArtifact(
                path=f"v1/objects/sha256/{str(row[3])[:2]}/{row[3]}",
                sha256=str(row[3]),
                size_bytes=int(row[4]),
                media_type="application/pdf",
            ),
            ocr_sha256=hashlib.sha256(f"ocr:{row[0]}".encode()).hexdigest(),
        )
        for row in document_rows
    )
    structure = StructureContract(
        schema_version="cardrag.structure.v2",
        parser_profiles=(
            IssuerParserProfile(
                issuer="kb",
                profile_id=metadata["parser_profile_id.kb"],
                profile_sha256=metadata["parser_profile_sha256.kb"],
            ),
        ),
        node_counts=StructureNodeCounts(
            total=int(metadata["structure_node_count"]),
            root=int(metadata["structure_node_count.ROOT"]),
            major_section=int(metadata["structure_node_count.MAJOR_SECTION"]),
            item=int(metadata["structure_node_count.ITEM"]),
            paragraph=int(metadata["structure_node_count.PARAGRAPH"]),
            list_item=int(metadata["structure_node_count.LIST_ITEM"]),
            table=int(metadata["structure_node_count.TABLE"]),
            table_row=int(metadata["structure_node_count.TABLE_ROW"]),
            footnote=int(metadata["structure_node_count.FOOTNOTE"]),
            boilerplate=int(metadata["structure_node_count.BOILERPLATE"]),
            unclassified=int(metadata["structure_node_count.UNCLASSIFIED"]),
        ),
        major_class_counts=StructureMajorClassCounts(
            total=int(metadata["structure_node_count.MAJOR_SECTION"]),
            benefit=int(metadata["structure_major_class_count.BENEFIT"]),
            notice=int(metadata["structure_major_class_count.NOTICE"]),
            mixed=int(metadata["structure_major_class_count.MIXED"]),
            unknown=int(metadata["structure_major_class_count.UNKNOWN"]),
        ),
        source_coverage=StructureSourceCoverage(
            source_non_whitespace_characters=int(metadata["source_non_whitespace_count"]),
            covered_non_whitespace_characters=int(metadata["covered_non_whitespace_count"]),
            source_non_whitespace_sha256=metadata["source_coverage_sha256"],
            covered_non_whitespace_sha256=metadata["source_coverage_sha256"],
        ),
        revision_counts=StructureRevisionCounts(
            total=int(metadata["contract_revision_count"]),
            current=int(metadata["current_revision_count"]),
            superseded=int(metadata["superseded_revision_count"]),
            ambiguous=int(metadata["ambiguous_revision_count"]),
        ),
        cross_contract_parent_count=0,
        cross_contract_link_count=0,
        lineages_with_multiple_current_revisions=0,
    )
    return RemoteGeneration(
        generation_id=fixture.generation_id,
        serving_schema="cardrag.serving-db.v5",
        corpus_sha256=metadata["corpus_sha256"],
        contract_sha256=metadata["contract_sha256"],
        database=RemoteArtifact(
            path=f"v1/generations/{fixture.generation_id}/index.sqlite3",
            sha256=hashlib.sha256(database_body).hexdigest(),
            size_bytes=len(database_body),
            media_type="application/vnd.sqlite3",
        ),
        documents=documents,
        issuer_codes=("kb",),
        document_count=len(documents),
        pdf_object_count=len({document.pdf.sha256 for document in documents}),
        ocr_object_count=len(documents),
        chunk_count=fixture.vector_count,
        embedding_provider="openrouter",
        embedding_model="qwen/qwen3-embedding-8b",
        embedding_dimension=4096,
        embedding_count=fixture.vector_count,
        issuer_ocr_counts=(("kb", len(documents), len(documents), 0),),
        vector_sidecar=RemoteArtifact(
            path=vector_artifact.path,
            sha256=vector_artifact.sha256,
            size_bytes=vector_artifact.size_bytes,
            media_type=vector_artifact.media_type,
        ),
        structure_contract=structure,
        embedding_profiles=(profile,),
        primary_embedding_profile_id=profile.profile_id,
        embedding_view_counts=tuple(
            EmbeddingViewCount(
                view_type=view_type,
                count=int(metadata[f"embedding_view_count.{view_type}"]),
            )
            for view_type in EMBEDDING_VIEW_TYPES
        ),
        vector_sidecar_contract=EmbeddingVectorSidecar(
            artifact=vector_artifact,
            profile_id=profile.profile_id,
            row_count=fixture.vector_count,
            dimension=4096,
            dtype="float32",
            byte_order="little-endian",
            layout="row-major",
            normalization="l2",
        ),
        parser_policy_sha256=metadata["parser_policy_sha256"],
        embedding_policy_sha256=metadata["embedding_policy_sha256"],
        retrieval_policy_sha256=metadata["retrieval_policy_sha256"],
    )


def fake_v5_handle(
    directory: Path,
    object_root: Path,
    generation: RemoteGeneration,
) -> GenerationHandle:
    assert generation.vector_sidecar is not None
    assert generation.primary_embedding_profile_id is not None
    matrix = np.zeros((1, 4096), dtype=np.float32)
    matrix[0, 0] = 1.0
    return GenerationHandle(
        generation_id=generation.generation_id,
        directory=directory.resolve(),
        database_path=(directory / "index.sqlite3").resolve(),
        object_root=object_root.resolve(),
        metadata=ServingMetadata(
            schema_id="cardrag.serving-db.v5",
            generation_id=generation.generation_id,
            corpus_sha256=generation.corpus_sha256,
            contract_sha256=generation.contract_sha256,
            embedding_provider=generation.embedding_provider,
            embedding_model=generation.embedding_model,
            embedding_input_policy_version="cardrag.structure-views.v1",
            embedding_dimension=4096,
            embedding_count=1,
            unsupported_document_count=0,
            unsupported_documents_sha256="0" * 64,
            ocr_failed_document_count=0,
            ocr_failed_documents_sha256="0" * 64,
            primary_embedding_profile_id=generation.primary_embedding_profile_id,
            vector_sidecar_sha256=generation.vector_sidecar.sha256,
            vector_sidecar_size_bytes=generation.vector_sidecar.size_bytes,
        ),
        vectors=LoadedVectorsV5(
            row_indices=(0,),
            node_ids=("node-1",),
            contract_revision_ids=("revision-1",),
            view_types=("TITLE",),
            profile_ids=(generation.primary_embedding_profile_id,),
            matrix=matrix,
            norms=np.ones((1,), dtype=np.float32),
        ),
        vector_sidecar_path=(directory / "vectors.f32").resolve(),
    )


class FakeReader:
    def __init__(
        self,
        generation: RemoteGeneration,
        database: Path,
        objects: dict[str, bytes],
        vector_sidecar: bytes | None = None,
    ):
        self.generation = generation
        self.database = database
        self.objects = objects
        self.vector_sidecar = vector_sidecar
        self.corrupt_object = False
        self.corrupt_vector_sidecar = False
        self.closed = False
        self.database_downloads = 0
        self.object_downloads = 0
        self.vector_downloads = 0

    async def read_stable_generation(self) -> RemoteGeneration:
        return self.generation

    async def download_database(self, generation: RemoteGeneration, destination: Path) -> None:
        assert generation.generation_id == self.generation.generation_id
        self.database_downloads += 1
        shutil.copyfile(self.database, destination)

    async def download_vector_sidecar(
        self,
        generation: RemoteGeneration,
        destination: Path,
    ) -> None:
        assert generation.generation_id == self.generation.generation_id
        assert self.vector_sidecar is not None
        self.vector_downloads += 1
        body = self.vector_sidecar
        destination.write_bytes(b"x" * len(body) if self.corrupt_vector_sidecar else body)

    async def download_object(self, artifact: RemoteArtifact, destination: Path) -> None:
        self.object_downloads += 1
        body = self.objects[artifact.sha256]
        destination.write_bytes(b"corrupt" if self.corrupt_object else body)

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("database_schema", "manifest_schema"),
    (
        ("cardrag.serving-db.v2", "cardrag.serving-db.v3"),
        ("cardrag.serving-db.v3", "cardrag.serving-db.v2"),
    ),
)
async def test_updater_rejects_manifest_database_schema_cross_pair(
    tmp_path: Path,
    database_schema: str,
    manifest_schema: str,
) -> None:
    fixture = create_database(
        tmp_path / "remote.sqlite3",
        "gen-cross-schema",
        schema_id=database_schema,
    )
    remote = replace(
        remote_generation(fixture),
        serving_schema=manifest_schema,
    )
    reader = FakeReader(
        remote,
        fixture.database,
        {digest: body for _, digest, _, body in fixture.documents},
    )
    store = GenerationStore(tmp_path / "state", maximum_vector_bytes=1024 * 1024)
    updater = WebDAVUpdater(reader, store, Metrics.create(), poll_seconds=300)

    with pytest.raises(RuntimeError, match="database contract differs"):
        await updater.poll_once()

    assert store.active_generation_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ("corpus_sha256", "contract_sha256"))
async def test_updater_rejects_manifest_database_hash_cross_pair(
    tmp_path: Path,
    field: str,
) -> None:
    fixture = create_database(tmp_path / "remote.sqlite3", "gen-cross-hash")
    remote = replace(remote_generation(fixture), **{field: "0" * 64})
    reader = FakeReader(
        remote,
        fixture.database,
        {digest: body for _, digest, _, body in fixture.documents},
    )
    store = GenerationStore(tmp_path / "state", maximum_vector_bytes=1024 * 1024)
    updater = WebDAVUpdater(reader, store, Metrics.create(), poll_seconds=300)

    with pytest.raises(RuntimeError, match="database contract differs"):
        await updater.poll_once()

    assert store.active_generation_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ("issuer", "page_count"))
async def test_updater_rejects_manifest_database_document_cross_pair(
    tmp_path: Path,
    field: str,
) -> None:
    fixture = create_database(tmp_path / "remote.sqlite3", "gen-cross-document")
    remote = remote_generation(fixture)
    documents = list(remote.documents)
    if field == "issuer":
        documents[0] = replace(documents[0], issuer=documents[1].issuer)
        documents[1] = replace(documents[1], issuer=remote.documents[0].issuer)
    else:
        documents[0] = replace(documents[0], page_count=2)
    remote = replace(remote, documents=tuple(documents))
    reader = FakeReader(
        remote,
        fixture.database,
        {digest: body for _, digest, _, body in fixture.documents},
    )
    store = GenerationStore(tmp_path / "state", maximum_vector_bytes=1024 * 1024)
    updater = WebDAVUpdater(reader, store, Metrics.create(), poll_seconds=300)

    with pytest.raises(RuntimeError, match="manifest and database PDF references differ"):
        await updater.poll_once()

    assert store.active_generation_id is None


@pytest.mark.asyncio
async def test_updater_rejects_database_corpus_count_cross_pair(tmp_path: Path) -> None:
    fixture = create_database(tmp_path / "remote.sqlite3", "gen-cross-count")
    with sqlite3.connect(fixture.database) as connection:
        connection.execute("DELETE FROM products WHERE product_code='P2'")
    remote = remote_generation(fixture)
    reader = FakeReader(
        remote,
        fixture.database,
        {digest: body for _, digest, _, body in fixture.documents},
    )
    store = GenerationStore(tmp_path / "state", maximum_vector_bytes=1024 * 1024)
    updater = WebDAVUpdater(reader, store, Metrics.create(), poll_seconds=300)

    with pytest.raises(RuntimeError, match="manifest and database corpus counts differ"):
        await updater.poll_once()

    assert store.active_generation_id is None


@pytest.mark.asyncio
async def test_updater_can_activate_last_good_v2_during_mcp_first_upgrade(tmp_path: Path) -> None:
    fixture = create_database(
        tmp_path / "remote-v2.sqlite3",
        "gen-v2",
        schema_id="cardrag.serving-db.v2",
    )
    remote = remote_generation(fixture)
    reader = FakeReader(
        remote,
        fixture.database,
        {digest: body for _, digest, _, body in fixture.documents},
    )
    store = GenerationStore(tmp_path / "state", maximum_vector_bytes=1024 * 1024)
    updater = WebDAVUpdater(reader, store, Metrics.create(), poll_seconds=300)

    assert await updater.poll_once() is True
    assert store.active_generation_id == "gen-v2"
    with store.pin() as handle:
        assert handle.metadata.schema_id == "cardrag.serving-db.v2"


@pytest.mark.asyncio
async def test_updater_activates_v4_generation(tmp_path: Path) -> None:
    fixture = create_database(
        tmp_path / "remote-v4.sqlite3",
        "gen-v4",
        schema_id="cardrag.serving-db.v4",
    )
    remote = remote_generation(fixture)
    reader = FakeReader(
        remote,
        fixture.database,
        {digest: body for _, digest, _, body in fixture.documents},
    )
    store = GenerationStore(tmp_path / "state", maximum_vector_bytes=1024 * 1024)
    updater = WebDAVUpdater(reader, store, Metrics.create(), poll_seconds=300)

    assert await updater.poll_once() is True
    assert store.active_generation_id == "gen-v4"
    with store.pin() as handle:
        assert handle.metadata.schema_id == "cardrag.serving-db.v4"
        assert handle.metadata.ocr_failed_document_count == 0


def test_updater_seals_schema_dimension_and_sidecar_presence(tmp_path: Path) -> None:
    database = tmp_path / "remote-v5.sqlite3"
    database.write_bytes(b"v5-database")
    vector_body = np.eye(1, 4096, dtype="<f4").tobytes()
    generation = remote_v5_generation(database, vector_body)

    with pytest.raises(RuntimeError, match="requires a vector sidecar"):
        WebDAVUpdater._verify_remote_manifest_contract(replace(generation, vector_sidecar=None))
    with pytest.raises(RuntimeError, match="dimension does not match"):
        WebDAVUpdater._verify_remote_manifest_contract(
            replace(generation, embedding_dimension=1536)
        )
    with pytest.raises(RuntimeError, match="must not declare a vector sidecar"):
        WebDAVUpdater._verify_remote_manifest_contract(
            replace(
                generation,
                serving_schema="cardrag.serving-db.v4",
                embedding_dimension=1536,
            )
        )
    assert generation.vector_sidecar is not None
    with pytest.raises(RuntimeError, match="vector sidecar artifact is invalid"):
        WebDAVUpdater._verify_remote_manifest_contract(
            replace(
                generation,
                vector_sidecar=replace(
                    generation.vector_sidecar,
                    size_bytes=generation.vector_sidecar.size_bytes - 1,
                ),
            )
        )


def test_v5_manifest_database_semantic_binding_rejects_cross_artifact_tamper(
    tmp_path: Path,
) -> None:
    fixture = build_v5_fixture(
        tmp_path / "gen-v5-semantic-binding",
        generation_id="gen-v5-semantic-binding",
    )
    generation = remote_generation_from_v5_fixture(fixture)
    handle = load_generation_handle(
        fixture.database.parent,
        tmp_path / "objects",
        maximum_vector_bytes=2 * 1024 * 1024,
    )
    updater = WebDAVUpdater(
        FakeReader(generation, fixture.database, {}),
        GenerationStore(tmp_path / "state", maximum_vector_bytes=2 * 1024 * 1024),
        Metrics.create(),
    )

    WebDAVUpdater._verify_remote_manifest_contract(generation)
    updater._verify_generation_binding(handle, generation)

    assert generation.structure_contract is not None
    parser = generation.structure_contract.parser_profiles[0]
    parser_tamper = generation.structure_contract.model_copy(
        update={"parser_profiles": (parser.model_copy(update={"profile_sha256": "d" * 64}),)}
    )
    counts = generation.structure_contract.node_counts
    count_values = counts.model_dump(mode="python")
    count_values["total"] += 1
    count_values["paragraph"] += 1
    count_tamper = generation.structure_contract.model_copy(
        update={"node_counts": StructureNodeCounts(**count_values)}
    )
    view_counts = tuple(
        row.model_copy(
            update={
                "count": (
                    row.count + 1
                    if row.view_type == "TITLE"
                    else row.count - 1
                    if row.view_type == "DETAIL"
                    else row.count
                )
            }
        )
        for row in generation.embedding_view_counts
    )
    alternate_profile = EmbeddingProfile.qwen3(
        provider_id="deepinfra",
        maximum_tokens=4096,
    )
    assert generation.vector_sidecar_contract is not None
    profile_sidecar = generation.vector_sidecar_contract.model_copy(
        update={"profile_id": alternate_profile.profile_id}
    )
    tampered_generations = (
        replace(generation, parser_policy_sha256="e" * 64),
        replace(generation, structure_contract=parser_tamper),
        replace(generation, structure_contract=count_tamper),
        replace(generation, embedding_view_counts=view_counts),
        replace(
            generation,
            embedding_profiles=(alternate_profile,),
            primary_embedding_profile_id=alternate_profile.profile_id,
            vector_sidecar_contract=profile_sidecar,
        ),
    )
    for tampered in tampered_generations:
        WebDAVUpdater._verify_remote_manifest_contract(tampered)
        with pytest.raises(RuntimeError, match="semantic manifest contract"):
            updater._verify_generation_binding(handle, tampered)


def test_updater_requires_selected_aggregation_manifest_and_database_to_match(
    tmp_path: Path,
) -> None:
    fixture = build_v5_fixture(
        tmp_path / "gen-v5-aggregation-binding",
        generation_id="gen-v5-aggregation-binding",
    )
    generation = remote_generation_from_v5_fixture(fixture)
    handle = load_generation_handle(
        fixture.database.parent,
        tmp_path / "objects",
        maximum_vector_bytes=2 * 1024 * 1024,
    )
    assert generation.primary_embedding_profile_id is not None
    assert handle.metadata.exact_row_corpus_sha256 is not None
    profile = DocumentAggregationProfile(
        schema_version="cardrag.document-aggregation-profile.v1",
        profile_id="cardrag.document-aggregation.top3-mean.v1",
        aggregation_policy="top3_mean",
        aggregation_definition=Top3MeanAggregationDefinition(
            child_count=3,
            formula="mean(highest min(3, available) non-CONTRACT row scores)",
        ),
        bootstrap=DocumentAggregationBootstrap(
            ci=0.95,
            method="paired-query-percentile-pcg64",
            samples=2_000,
            seed=1010,
        ),
        embedding_profile_id=generation.primary_embedding_profile_id,
        exact_row_corpus_sha256=handle.metadata.exact_row_corpus_sha256,
        generation_id="evaluation-generation",
        generation_manifest_sha256="1" * 64,
        gold_sha256="2" * 64,
        score_artifact_sha256="3" * 64,
        selection_objective="ndcg_at_10",
    )
    selected = replace(
        generation,
        document_aggregation_profile=profile,
        document_aggregation_policy=profile.aggregation_policy,
        sealed_profile_sha256=profile.profile_sha256,
        exact_row_corpus_sha256=profile.exact_row_corpus_sha256,
        retrieval_policy_sha256=canonical_sha256(
            sealed_v5_retrieval_policy(profile, profile.profile_sha256)
        ),
    )

    WebDAVUpdater._verify_remote_manifest_contract(selected)
    with pytest.raises(RuntimeError, match="differs from database aggregation identity"):
        WebDAVUpdater(
            FakeReader(selected, fixture.database, {}),
            GenerationStore(tmp_path / "state", maximum_vector_bytes=2 * 1024 * 1024),
            Metrics.create(),
        )._verify_generation_binding(handle, selected)

    with pytest.raises(RuntimeError, match="all-or-nothing"):
        WebDAVUpdater._verify_remote_manifest_contract(
            replace(selected, sealed_profile_sha256=None)
        )
    with pytest.raises(RuntimeError, match="inconsistent"):
        WebDAVUpdater._verify_remote_manifest_contract(
            replace(selected, exact_row_corpus_sha256="4" * 64)
        )


def test_legacy_generation_rejects_even_a_dangling_sidecar_symlink(tmp_path: Path) -> None:
    fixture = create_database(
        tmp_path / "gen-legacy" / "index.sqlite3",
        "gen-legacy",
    )
    (fixture.database.parent / "vectors.f32").symlink_to("missing-vectors.f32")
    generation = remote_generation(fixture)
    store = GenerationStore(tmp_path / "state", maximum_vector_bytes=1024 * 1024)
    updater = WebDAVUpdater(
        FakeReader(generation, fixture.database, {}),
        store,
        Metrics.create(),
    )

    with pytest.raises(RuntimeError, match="forbidden vector sidecar"):
        updater._validated_handle(fixture.database.parent, generation)


@pytest.mark.asyncio
async def test_v4_v5_v4_transition_stages_sidecar_durably_and_restarts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = GenerationStore(
        tmp_path / "state",
        maximum_vector_bytes=1024 * 1024,
        retention=3,
    )
    v4_fixture = create_database(
        store.generations / "gen-v4-before" / "index.sqlite3",
        "gen-v4-before",
        schema_id="cardrag.serving-db.v4",
    )
    for _, digest, _, body in v4_fixture.documents:
        target = cas_path(store.objects, digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    v4_handle = load_generation_handle(
        store.generations / "gen-v4-before",
        store.objects,
        maximum_vector_bytes=store.maximum_vector_bytes,
        expected_generation_id="gen-v4-before",
    )
    store.verify_handle_pdfs(v4_handle)
    store.activate(v4_handle)

    database = tmp_path / "remote-v5.sqlite3"
    database.write_bytes(b"v5-stage-database")
    vector_body = np.eye(1, 4096, dtype="<f4").tobytes()
    v5_generation = remote_v5_generation(
        database,
        vector_body,
        generation_id="gen-v5-transition",
    )
    reader = FakeReader(
        v5_generation,
        database,
        {digest: body for _, digest, _, body in v4_fixture.documents},
        vector_body,
    )
    updater = WebDAVUpdater(reader, store, Metrics.create(), poll_seconds=300)

    real_load = updater_module.load_generation_handle
    real_database_contract = WebDAVUpdater._database_contract
    real_verify_handle_pdfs = GenerationStore.verify_handle_pdfs

    def load_dispatch(
        directory: Path,
        object_root: Path,
        *,
        maximum_vector_bytes: int,
        maximum_database_bytes: int = 4 * 1024 * 1024 * 1024,
        maximum_vector_sidecar_bytes: int | None = None,
        maximum_resident_vector_bytes: int | None = None,
        expected_generation_id: str | None = None,
        expected_embedding_model: str | None = None,
        expected_embedding_count: int | None = None,
    ) -> GenerationHandle:
        if directory.name == v5_generation.generation_id:
            assert maximum_vector_bytes == store.maximum_vector_bytes
            assert maximum_database_bytes == store.maximum_database_bytes
            assert maximum_vector_sidecar_bytes == store.maximum_vector_sidecar_bytes
            assert maximum_resident_vector_bytes == store.maximum_resident_vector_bytes
            assert expected_generation_id == v5_generation.generation_id
            if expected_embedding_model is not None:
                assert expected_embedding_model == v5_generation.embedding_model
            if expected_embedding_count is not None:
                assert expected_embedding_count == v5_generation.embedding_count
            return fake_v5_handle(directory, object_root, v5_generation)
        return real_load(
            directory,
            object_root,
            maximum_vector_bytes=maximum_vector_bytes,
            maximum_database_bytes=maximum_database_bytes,
            maximum_vector_sidecar_bytes=maximum_vector_sidecar_bytes,
            maximum_resident_vector_bytes=maximum_resident_vector_bytes,
            expected_generation_id=expected_generation_id,
            expected_embedding_model=expected_embedding_model,
            expected_embedding_count=expected_embedding_count,
        )

    def database_contract_dispatch(handle: GenerationHandle):
        if handle.metadata.schema_id == "cardrag.serving-db.v5":
            return [], (0, 0, 0, 0, 1, 0), ()
        return real_database_contract(handle)

    def verify_handle_pdfs_dispatch(
        selected_store: GenerationStore,
        handle: GenerationHandle,
    ) -> None:
        if handle.metadata.schema_id != "cardrag.serving-db.v5":
            real_verify_handle_pdfs(selected_store, handle)

    fsync_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(updater_module, "load_generation_handle", load_dispatch)
    monkeypatch.setattr(store_module, "load_generation_handle", load_dispatch)
    monkeypatch.setattr(
        WebDAVUpdater,
        "_database_contract",
        staticmethod(database_contract_dispatch),
    )
    monkeypatch.setattr(
        WebDAVUpdater,
        "_verify_v5_semantic_binding",
        staticmethod(lambda _handle, _generation: None),
    )
    monkeypatch.setattr(
        GenerationStore,
        "verify_handle_pdfs",
        verify_handle_pdfs_dispatch,
    )
    monkeypatch.setattr(
        updater_module,
        "_fsync_file",
        lambda path: fsync_calls.append(("file", path.name)),
    )
    monkeypatch.setattr(
        updater_module,
        "_fsync_directory",
        lambda path: fsync_calls.append(("directory", path.name)),
    )

    assert await updater.poll_once() is True
    assert store.active_generation_id == "gen-v5-transition"
    assert reader.vector_downloads == 1
    assert fsync_calls == [
        ("file", "index.sqlite3"),
        ("file", "vectors.f32"),
        ("directory", "gen-v5-transition"),
        ("directory", "generations"),
    ]
    with store.pin() as v5_handle:
        assert v5_handle.directory == (store.generations / "gen-v5-transition").resolve()
        assert (
            v5_handle.vector_sidecar_path
            == (store.generations / "gen-v5-transition" / "vectors.f32").resolve()
        )
        assert v5_handle.vector_sidecar_path.read_bytes() == vector_body

    restarted = GenerationStore(
        store.root,
        maximum_vector_bytes=store.maximum_vector_bytes,
        retention=3,
    )
    assert restarted.load_current() is True
    with restarted.pin() as restarted_handle:
        assert restarted_handle.metadata.schema_id == "cardrag.serving-db.v5"
        assert (
            restarted_handle.vector_sidecar_path
            == (restarted.generations / "gen-v5-transition" / "vectors.f32").resolve()
        )

    reader.generation = remote_generation(v4_fixture)
    reader.database = v4_fixture.database
    reader.vector_sidecar = None
    assert await updater.poll_once() is True
    assert store.active_generation_id == "gen-v4-before"
    with store.pin() as rolled_back:
        assert rolled_back.metadata.schema_id == "cardrag.serving-db.v4"
        assert rolled_back.vector_sidecar_path is None


@pytest.mark.asyncio
async def test_corrupt_v5_sidecar_keeps_v4_last_good_and_cleans_staging(
    tmp_path: Path,
) -> None:
    store = GenerationStore(tmp_path / "state", maximum_vector_bytes=1024 * 1024)
    v4_fixture = create_database(
        store.generations / "gen-v4-good" / "index.sqlite3",
        "gen-v4-good",
        schema_id="cardrag.serving-db.v4",
    )
    for _, digest, _, body in v4_fixture.documents:
        target = cas_path(store.objects, digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    handle = load_generation_handle(
        store.generations / "gen-v4-good",
        store.objects,
        maximum_vector_bytes=store.maximum_vector_bytes,
    )
    store.verify_handle_pdfs(handle)
    store.activate(handle)

    database = tmp_path / "remote-v5.sqlite3"
    database.write_bytes(b"v5-corrupt-sidecar-database")
    vector_body = np.eye(1, 4096, dtype="<f4").tobytes()
    generation = remote_v5_generation(
        database,
        vector_body,
        generation_id="gen-v5-corrupt",
    )
    reader = FakeReader(generation, database, {}, vector_body)
    reader.corrupt_vector_sidecar = True
    updater = WebDAVUpdater(reader, store, Metrics.create(), poll_seconds=300)

    with pytest.raises(RuntimeError, match="hash or size"):
        await updater.poll_once()

    assert reader.vector_downloads == 1
    assert store.active_generation_id == "gen-v4-good"
    assert not (store.incoming / "gen-v5-corrupt").exists()
    assert not (store.generations / "gen-v5-corrupt").exists()


@pytest.mark.asyncio
async def test_v5_promotion_budget_counts_norms_but_not_mmap_sidecar(
    tmp_path: Path,
) -> None:
    store = GenerationStore(tmp_path / "state", maximum_vector_bytes=1024 * 1024)
    install_generation(store, "gen-last-good", two_documents=False)
    database = tmp_path / "remote-v5.sqlite3"
    database.write_bytes(b"v5-promotion-budget-database")
    vector_body = np.eye(1, 4096, dtype="<f4").tobytes()
    generation = remote_v5_generation(
        database,
        vector_body,
        generation_id="gen-v5-too-large",
    )
    reader = FakeReader(generation, database, {}, vector_body)
    updater = WebDAVUpdater(reader, store, Metrics.create(), poll_seconds=300)
    store.maximum_resident_vector_bytes = store.resident_vector_bytes + 4 - 1

    with pytest.raises(RuntimeError, match="resident/pinned vector memory"):
        await updater.poll_once()

    assert store.active_generation_id == "gen-last-good"
    assert not (store.incoming / "gen-v5-too-large").exists()


def test_v5_declared_sidecar_and_resident_capacity_are_independent(tmp_path: Path) -> None:
    database = tmp_path / "remote-v5.sqlite3"
    database.write_bytes(b"v5-large-capacity-contract")
    one_row = np.eye(1, 4096, dtype="<f4").tobytes()
    generation = remote_v5_generation(database, one_row, generation_id="gen-v5-large")
    row_count = 65_537
    sidecar_size = row_count * 4096 * 4
    assert generation.vector_sidecar is not None
    assert generation.vector_sidecar_contract is not None
    sidecar_artifact = generation.vector_sidecar_contract.artifact.model_copy(
        update={"size_bytes": sidecar_size}
    )
    generation = replace(
        generation,
        chunk_count=row_count,
        embedding_count=row_count,
        vector_sidecar=replace(generation.vector_sidecar, size_bytes=sidecar_size),
        vector_sidecar_contract=generation.vector_sidecar_contract.model_copy(
            update={"artifact": sidecar_artifact, "row_count": row_count}
        ),
        embedding_view_counts=tuple(
            row.model_copy(update={"count": row_count if row.view_type == "TITLE" else 0})
            for row in generation.embedding_view_counts
        ),
    )
    store = GenerationStore(
        tmp_path / "state",
        maximum_vector_bytes=1024**3,
        maximum_vector_sidecar_bytes=2 * 1024**3,
        maximum_resident_vector_bytes=1024**3,
    )
    updater = WebDAVUpdater(
        FakeReader(generation, database, {}, one_row),
        store,
        Metrics.create(),
    )

    WebDAVUpdater._verify_remote_manifest_contract(generation)
    updater._verify_candidate_capacity(generation)
    assert sidecar_size > store.maximum_vector_bytes
    assert row_count * 4 < store.maximum_resident_vector_bytes

    store.maximum_vector_sidecar_bytes = sidecar_size - 1
    with pytest.raises(RuntimeError, match="configured file limit"):
        updater._verify_candidate_capacity(generation)


@pytest.mark.asyncio
async def test_sidecar_file_cap_rejects_before_download_and_keeps_last_good(
    tmp_path: Path,
) -> None:
    vector_body = np.eye(1, 4096, dtype="<f4").tobytes()
    store = GenerationStore(
        tmp_path / "state",
        maximum_vector_bytes=1024 * 1024,
        maximum_vector_sidecar_bytes=len(vector_body) - 1,
    )
    install_generation(store, "gen-last-good", two_documents=False)
    database = tmp_path / "remote-v5.sqlite3"
    database.write_bytes(b"v5-sidecar-file-cap")
    generation = remote_v5_generation(
        database,
        vector_body,
        generation_id="gen-v5-over-file-cap",
    )
    reader = FakeReader(generation, database, {}, vector_body)
    updater = WebDAVUpdater(reader, store, Metrics.create())

    with pytest.raises(RuntimeError, match="configured file limit"):
        await updater.poll_once()

    assert reader.vector_downloads == 0
    assert store.active_generation_id == "gen-last-good"
    assert not (store.incoming / generation.generation_id).exists()


@pytest.mark.asyncio
async def test_database_hard_cap_rejects_before_any_artifact_download(tmp_path: Path) -> None:
    fixture = create_database(tmp_path / "remote.sqlite3", "gen-database-hard-cap")
    generation = remote_generation(fixture)
    reader = FakeReader(
        generation,
        fixture.database,
        {digest: body for _, digest, _, body in fixture.documents},
    )
    store = GenerationStore(tmp_path / "state", maximum_vector_bytes=1024 * 1024)
    updater = WebDAVUpdater(
        reader,
        store,
        Metrics.create(),
        maximum_database_bytes=generation.database.size_bytes - 1,
    )

    with pytest.raises(RuntimeError, match="database exceeds its configured hard cap"):
        await updater.poll_once()

    assert (reader.database_downloads, reader.vector_downloads, reader.object_downloads) == (
        0,
        0,
        0,
    )
    assert store.active_generation_id is None


@pytest.mark.asyncio
async def test_generation_aggregate_quota_is_checked_before_any_download(tmp_path: Path) -> None:
    fixture = create_database(tmp_path / "remote.sqlite3", "gen-aggregate-hard-cap")
    generation = remote_generation(fixture)
    unique_pdf_bytes = sum(
        {document.pdf.sha256: document.pdf.size_bytes for document in generation.documents}.values()
    )
    declared_total = generation.database.size_bytes + unique_pdf_bytes
    reader = FakeReader(
        generation,
        fixture.database,
        {digest: body for _, digest, _, body in fixture.documents},
    )
    store = GenerationStore(tmp_path / "state", maximum_vector_bytes=1024 * 1024)
    updater = WebDAVUpdater(
        reader,
        store,
        Metrics.create(),
        maximum_database_bytes=generation.database.size_bytes,
        maximum_pdf_bytes=max(document.pdf.size_bytes for document in generation.documents),
        maximum_generation_download_bytes=declared_total - 1,
    )

    with pytest.raises(RuntimeError, match="aggregate download quota"):
        await updater.poll_once()

    assert (reader.database_downloads, reader.vector_downloads, reader.object_downloads) == (
        0,
        0,
        0,
    )
    assert not (store.incoming / generation.generation_id).exists()


def test_manifest_rejects_boolean_pdf_size() -> None:
    artifact = RemoteArtifact(
        path=f"v1/objects/sha256/{'a' * 2}/{'a' * 64}",
        sha256="a" * 64,
        size_bytes=True,  # type: ignore[arg-type]
        media_type="application/pdf",
    )
    remote = RemoteGeneration(
        generation_id="gen-invalid-pdf-size",
        serving_schema="cardrag.serving-db.v3",
        corpus_sha256="b" * 64,
        contract_sha256="c" * 64,
        database=RemoteArtifact(
            path="v1/generations/gen-invalid-pdf-size/index.sqlite3",
            sha256="d" * 64,
            size_bytes=1,
            media_type="application/vnd.sqlite3",
        ),
        documents=(RemoteDocument("doc", "kb", 1, artifact),),
        issuer_codes=("kb",),
        document_count=1,
        pdf_object_count=1,
        ocr_object_count=0,
        chunk_count=1,
        embedding_provider="openrouter",
        embedding_model="openai/text-embedding-3-small",
        embedding_dimension=1536,
        embedding_count=1,
    )

    with pytest.raises(RuntimeError, match="PDF artifact is invalid"):
        WebDAVUpdater._verify_remote_manifest_contract(remote)


@pytest.mark.asyncio
async def test_cas_state_quota_rejects_before_temp_creation_without_cleanup(tmp_path: Path) -> None:
    body = b"%PDF-quota"
    digest = hashlib.sha256(body).hexdigest()
    artifact = RemoteArtifact(
        path=f"v1/objects/sha256/{digest[:2]}/{digest}",
        sha256=digest,
        size_bytes=len(body),
        media_type="application/pdf",
    )
    database = tmp_path / "unused.sqlite3"
    database.write_bytes(b"x")
    generation = RemoteGeneration(
        generation_id="gen-state-quota",
        serving_schema="cardrag.serving-db.v3",
        corpus_sha256="b" * 64,
        contract_sha256="c" * 64,
        database=RemoteArtifact(
            path="v1/generations/gen-state-quota/index.sqlite3",
            sha256=hashlib.sha256(b"x").hexdigest(),
            size_bytes=1,
            media_type="application/vnd.sqlite3",
        ),
        documents=(),
        issuer_codes=(),
        document_count=0,
        pdf_object_count=0,
        ocr_object_count=0,
        chunk_count=0,
        embedding_provider="openrouter",
        embedding_model="openai/text-embedding-3-small",
        embedding_dimension=1536,
        embedding_count=0,
    )
    reader = FakeReader(generation, database, {digest: body})
    store = GenerationStore(
        tmp_path / "state",
        maximum_vector_bytes=1,
        maximum_vector_sidecar_bytes=1,
        maximum_resident_vector_bytes=1,
        maximum_pdf_bytes=100,
        maximum_database_bytes=50,
        maximum_generation_download_bytes=100,
        maximum_state_bytes=200,
        reserved_free_space_bytes=0,
        exhaustive_audit_max_jobs=1,
        exhaustive_audit_max_total_bytes=50,
        exhaustive_audit_max_artifact_bytes=25,
        reranker_audit_max_jobs=1,
        reranker_audit_max_total_bytes=50,
        reranker_audit_max_artifact_bytes=25,
    )
    padding = store.root / "retained-state.bin"
    padding.write_bytes(b"r" * 195)
    updater = WebDAVUpdater(
        reader,
        store,
        Metrics.create(),
        maximum_pdf_bytes=100,
        maximum_database_bytes=50,
        maximum_generation_download_bytes=100,
    )

    with pytest.raises(RuntimeError, match="state quota"):
        await updater._sync_pdf(artifact)

    assert reader.object_downloads == 0
    assert padding.read_bytes() == b"r" * 195
    assert not cas_path(store.objects, digest).exists()


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
    store.maximum_resident_vector_bytes = store.resident_vector_bytes + required - 1
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


def test_restart_refuses_current_database_above_hard_cap(tmp_path: Path) -> None:
    state = tmp_path / "state"
    store = GenerationStore(state, maximum_vector_bytes=1024 * 1024)
    fixture = install_generation(store, "gen-001")

    restarted = GenerationStore(
        state,
        maximum_vector_bytes=1024 * 1024,
        maximum_database_bytes=fixture.database.stat().st_size - 1,
    )

    assert restarted.load_current() is False
    assert restarted.active_generation_id is None


def test_activation_pointer_obeys_reserved_free_space_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = GenerationStore(tmp_path / "state", maximum_vector_bytes=1024 * 1024)
    install_generation(store, "gen-001")
    install_generation(store, "gen-002", activate=False)
    candidate = load_generation_handle(
        store.generations / "gen-002",
        store.objects,
        maximum_vector_bytes=store.maximum_vector_bytes,
        maximum_database_bytes=store.maximum_database_bytes,
    )
    monkeypatch.setattr(
        quota_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=1, used=1, free=0),
    )

    with pytest.raises(StorageQuotaError, match="reserved free-space"):
        store.activate(candidate)

    assert store.active_generation_id == "gen-001"


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
    one_generation_resident_bytes = store.resident_vector_bytes
    lease = store.pin()
    lease.__enter__()
    install_generation(store, "gen-002", suffix="-2", two_documents=False)
    assert set(store._entries) == {"gen-001", "gen-002"}
    assert store.resident_vector_bytes == 2 * one_generation_resident_bytes

    lease.__exit__(None, None, None)
    assert tuple(store._entries) == ("gen-002",)
    assert store.resident_vector_bytes == one_generation_resident_bytes
