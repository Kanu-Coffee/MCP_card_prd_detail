from __future__ import annotations

import base64
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import unquote, urlsplit

import httpx
import pytest
from pydantic import SecretStr

from cardrag_core import (
    EMBEDDING_VIEW_TYPES,
    STABLE_POINTER_PATH,
    ArtifactContractError,
    ArtifactRef,
    CASPublisher,
    EmbeddingContract,
    EmbeddingProfile,
    EmbeddingVectorSidecar,
    EmbeddingViewCount,
    GenerationCounts,
    GenerationDocument,
    GenerationManifest,
    GenerationPointer,
    GenerationReady,
    ImmutablePublisher,
    IssuerOCRCounts,
    IssuerParserProfile,
    MCPArtifactReader,
    StablePointerPublisher,
    StructureContract,
    StructureMajorClassCounts,
    StructureNodeCounts,
    StructureRevisionCounts,
    StructureSourceCoverage,
    WebDAVClient,
    WebDAVHTTPError,
    WebDAVIntegrityError,
    WebDAVSettings,
    generation_database_path,
    generation_manifest_path,
    generation_ready_path,
    generation_vectors_path,
    object_path,
    sha256_bytes,
)

GenerationSchema = Literal[
    "cardrag.generation.v1",
    "cardrag.generation.v2",
    "cardrag.generation.v3",
]
ServingSchema = Literal[
    "cardrag.serving-db.v1",
    "cardrag.serving-db.v2",
    "cardrag.serving-db.v3",
]
SCHEMA_PAIRS: tuple[tuple[GenerationSchema, ServingSchema], ...] = (
    ("cardrag.generation.v1", "cardrag.serving-db.v1"),
    ("cardrag.generation.v2", "cardrag.serving-db.v2"),
    ("cardrag.generation.v3", "cardrag.serving-db.v3"),
)


class _MemoryWebDAV:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.collections: set[str] = {""}
        self.requests: list[tuple[str, str]] = []
        self.expected_auth = "Basic " + base64.b64encode(b"dav-user:dav-password").decode()
        self.inject_move_collision_status: int | None = None

    @staticmethod
    def _relative(url: httpx.URL | str) -> str:
        parsed = urlsplit(str(url))
        assert parsed.path.startswith("/dav/")
        return unquote(parsed.path[len("/dav/") :]).rstrip("/")

    @staticmethod
    def _parent(path: str) -> str:
        return path.rpartition("/")[0]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == self.expected_auth
        path = self._relative(request.url)
        self.requests.append((request.method, path))
        if request.method == "HEAD":
            if path in self.files:
                return httpx.Response(
                    200,
                    headers={"Content-Length": str(len(self.files[path])), "ETag": '"not-a-sha"'},
                )
            if path in self.collections:
                return httpx.Response(200)
            return httpx.Response(404)
        if request.method == "GET":
            if path not in self.files:
                return httpx.Response(404)
            body = self.files[path]
            return httpx.Response(200, content=body, headers={"Content-Length": str(len(body))})
        if request.method == "PROPFIND":
            if path not in self.files and path not in self.collections:
                return httpx.Response(404)
            return httpx.Response(207, content=b"<d:multistatus xmlns:d='DAV:'/>")
        if request.method == "MKCOL":
            if path in self.files or path in self.collections:
                return httpx.Response(405)
            if self._parent(path) not in self.collections:
                return httpx.Response(409)
            self.collections.add(path)
            return httpx.Response(201)
        if request.method == "PUT":
            if self._parent(path) not in self.collections:
                return httpx.Response(409)
            if request.headers.get("If-None-Match") == "*" and path in self.files:
                return httpx.Response(412)
            self.files[path] = request.read()
            return httpx.Response(201)
        if request.method == "MOVE":
            if path not in self.files:
                return httpx.Response(404)
            destination = self._relative(request.headers["Destination"])
            if self._parent(destination) not in self.collections:
                return httpx.Response(409)
            if self.inject_move_collision_status is not None:
                status = self.inject_move_collision_status
                self.inject_move_collision_status = None
                self.files[destination] = self.files[path]
                return httpx.Response(status)
            if destination in self.files and request.headers.get("Overwrite") == "F":
                return httpx.Response(412)
            self.files[destination] = self.files.pop(path)
            return httpx.Response(201)
        if request.method == "DELETE":
            if path not in self.files:
                return httpx.Response(404)
            del self.files[path]
            return httpx.Response(204)
        return httpx.Response(405)


@pytest.fixture
def webdav() -> tuple[_MemoryWebDAV, WebDAVClient]:
    backend = _MemoryWebDAV()
    settings = WebDAVSettings(
        environment="test",
        base_url="http://127.0.0.1/dav",
        username="dav-user",
        password=SecretStr("dav-password"),
        allow_insecure_http=True,
    )
    client = WebDAVClient(settings, transport=httpx.MockTransport(backend))
    try:
        yield backend, client
    finally:
        client.close()


def test_rfc4918_methods_and_basic_auth(webdav: tuple[_MemoryWebDAV, WebDAVClient]) -> None:
    backend, client = webdav
    client.ensure_collection("v1/example")
    client.put("v1/example/source.bin", b"payload", if_none_match=True)
    assert client.head("v1/example/source.bin").size_bytes == 7
    assert client.get("v1/example/source.bin").content == b"payload"
    assert client.propfind("v1/example", depth="1").status_code == 207
    client.move("v1/example/source.bin", "v1/example/moved.bin", overwrite=False)
    assert client.exists("v1/example/moved.bin")
    client.delete("v1/example/moved.bin")
    assert not client.exists("v1/example/moved.bin")
    methods = {method for method, _ in backend.requests}
    assert {"HEAD", "GET", "PROPFIND", "MKCOL", "PUT", "MOVE", "DELETE"} <= methods


def test_get_hard_cap_and_verified_atomic_download(
    webdav: tuple[_MemoryWebDAV, WebDAVClient],
    tmp_path: Path,
) -> None:
    _, client = webdav
    client.ensure_collection("v1/files")
    client.put("v1/files/large", b"x" * 100)
    with pytest.raises(WebDAVIntegrityError, match="read limit"):
        client.get("v1/files/large", max_bytes=10)
    destination = (tmp_path / "download.bin").resolve()
    destination.write_bytes(b"old")
    with pytest.raises(WebDAVIntegrityError, match="exceeds"):
        client.download("v1/files/large", destination, expected_size_bytes=10)
    assert destination.read_bytes() == b"old"
    with pytest.raises(WebDAVIntegrityError, match="SHA-256"):
        client.download("v1/files/large", destination, expected_sha256="0" * 64)
    assert destination.read_bytes() == b"old"
    verified = client.download(
        "v1/files/large",
        destination,
        expected_sha256=sha256_bytes(b"x" * 100),
        expected_size_bytes=100,
    )
    assert verified.size_bytes == 100
    assert destination.read_bytes() == b"x" * 100


def test_cas_publisher_is_create_once_and_readback_verified(
    webdav: tuple[_MemoryWebDAV, WebDAVClient],
) -> None:
    backend, client = webdav
    publisher = CASPublisher(client)
    reference = publisher.publish_bytes(b"immutable", media_type="text/plain")
    assert reference.path == object_path(reference.sha256).as_posix()
    assert backend.files[reference.path] == b"immutable"
    assert publisher.publish_bytes(b"immutable", media_type="text/plain") == reference
    assert not any(".incoming" in path for path in backend.files)

    backend.files[reference.path] = b"corrupt"
    with pytest.raises(WebDAVIntegrityError):
        publisher.publish_bytes(b"immutable", media_type="text/plain")


def test_file_publication_rejects_symlinks_before_remote_mutation(
    webdav: tuple[_MemoryWebDAV, WebDAVClient],
    tmp_path: Path,
) -> None:
    backend, client = webdav
    target = tmp_path / "target.bin"
    target.write_bytes(b"sealed")
    source = tmp_path / "source.bin"
    source.symlink_to(target.name)
    request_count = len(backend.requests)

    with pytest.raises(WebDAVIntegrityError, match="missing or unsafe"):
        CASPublisher(client).publish_file(source)

    assert len(backend.requests) == request_count
    assert not backend.files


def test_file_publication_expected_identity_fails_before_remote_mutation(
    webdav: tuple[_MemoryWebDAV, WebDAVClient],
    tmp_path: Path,
) -> None:
    backend, client = webdav
    source = tmp_path / "source.bin"
    source.write_bytes(b"actual")
    request_count = len(backend.requests)

    with pytest.raises(WebDAVIntegrityError, match="sealed identity"):
        CASPublisher(client).publish_file(
            source,
            expected_sha256=sha256_bytes(b"different"),
            expected_size_bytes=len(b"actual"),
        )

    assert len(backend.requests) == request_count
    assert not backend.files


def test_file_publication_detects_path_swap_before_remote_commit(
    webdav: tuple[_MemoryWebDAV, WebDAVClient],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, client = webdav
    original = b"sealed-A"
    replacement = b"unsealed-B"
    source = tmp_path / "source.bin"
    source.write_bytes(original)
    replacement_path = tmp_path / "replacement.bin"
    replacement_path.write_bytes(replacement)
    destination = object_path(sha256_bytes(original)).as_posix()
    original_put = client.put
    swapped = False

    def swap_before_upload(*args: object, **kwargs: object) -> object:
        nonlocal swapped
        if not swapped:
            swapped = True
            replacement_path.replace(source)
        return original_put(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(client, "put", swap_before_upload)

    with pytest.raises(WebDAVIntegrityError, match="identity changed"):
        CASPublisher(client).publish_file(
            source,
            expected_sha256=sha256_bytes(original),
            expected_size_bytes=len(original),
        )

    assert source.read_bytes() == replacement
    assert destination not in backend.files
    assert not any(".incoming" in path for path in backend.files)


def test_immutable_publisher_accepts_verified_409_move_collision(
    webdav: tuple[_MemoryWebDAV, WebDAVClient],
) -> None:
    backend, client = webdav
    backend.inject_move_collision_status = 409
    reference = CASPublisher(client).publish_bytes(b"raced", media_type="text/plain")
    assert backend.files[reference.path] == b"raced"
    assert not any(".incoming" in path for path in backend.files)


def test_immutable_verified_commit_survives_temporary_cleanup_failure(
    webdav: tuple[_MemoryWebDAV, WebDAVClient],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    backend, client = webdav
    raw_sentinel = "RAW_IMMUTABLE_DELETE_SECRET_PATH"

    def fail_delete(*_args: object, **_kwargs: object) -> None:
        raise WebDAVHTTPError("DELETE", PurePosixPath(raw_sentinel), 500)

    monkeypatch.setattr(client, "delete", fail_delete)
    reference = ImmutablePublisher(client).publish_bytes(
        "v1/objects/verified-cleanup.bin",
        b"verified",
    )

    assert backend.files[reference.path] == b"verified"
    assert "temporary cleanup failed after verified commit" in caplog.text
    assert raw_sentinel not in caplog.text


def test_immutable_publisher_cleanup_cannot_mask_primary_failure(
    webdav: tuple[_MemoryWebDAV, WebDAVClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, client = webdav

    def fail_move(*_args: object, **_kwargs: object) -> None:
        raise WebDAVHTTPError("MOVE", PurePosixPath("v1/original-failure"), 503)

    def fail_delete(*_args: object, **_kwargs: object) -> None:
        raise WebDAVHTTPError("DELETE", PurePosixPath("v1/cleanup-failure"), 500)

    monkeypatch.setattr(client, "move", fail_move)
    monkeypatch.setattr(client, "delete", fail_delete)

    with pytest.raises(WebDAVHTTPError) as captured:
        ImmutablePublisher(client).publish_bytes(
            "v1/objects/original.bin",
            b"payload",
        )

    assert (captured.value.method, captured.value.status_code) == ("MOVE", 503)


def test_stable_pointer_is_the_only_atomic_overwrite_path(
    webdav: tuple[_MemoryWebDAV, WebDAVClient],
) -> None:
    backend, client = webdav
    publisher = StablePointerPublisher(client)
    first = publisher.atomic_replace_json({"generation_id": "gen-one"})
    second = publisher.atomic_replace_json({"generation_id": "gen-two"})
    assert first.path == STABLE_POINTER_PATH.as_posix()
    assert second.path == STABLE_POINTER_PATH.as_posix()
    assert backend.files[STABLE_POINTER_PATH.as_posix()] == b'{"generation_id":"gen-two"}'
    move_requests = [request for request in backend.requests if request[0] == "MOVE"]
    assert len(move_requests) == 2


def test_stable_pointer_verified_commit_survives_temporary_cleanup_failure(
    webdav: tuple[_MemoryWebDAV, WebDAVClient],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    backend, client = webdav
    raw_sentinel = "RAW_DELETE_SECRET_PATH"

    def fail_delete(*_args: object, **_kwargs: object) -> None:
        raise WebDAVHTTPError("DELETE", PurePosixPath(raw_sentinel), 500)

    monkeypatch.setattr(client, "delete", fail_delete)
    reference = StablePointerPublisher(client).atomic_replace_json({"generation_id": "committed"})

    assert reference.path == STABLE_POINTER_PATH.as_posix()
    assert backend.files[reference.path] == b'{"generation_id":"committed"}'
    assert "temporary cleanup failed after verified replacement" in caplog.text
    assert raw_sentinel not in caplog.text


def test_candidate_channel_is_isolated_from_stable(
    webdav: tuple[_MemoryWebDAV, WebDAVClient],
) -> None:
    backend, client = webdav
    StablePointerPublisher(client).atomic_replace_json({"generation_id": "stable"})
    candidate = StablePointerPublisher(client, channel="candidate-v1.0.9")
    result = candidate.atomic_replace_json({"generation_id": "candidate"})

    assert result.path == "v1/channels/candidate-v1.0.9.json"
    assert backend.files[STABLE_POINTER_PATH.as_posix()] == b'{"generation_id":"stable"}'
    assert backend.files[result.path] == b'{"generation_id":"candidate"}'


def _publish_current_generation(
    client: WebDAVClient,
    *,
    schema_pair: tuple[GenerationSchema, ServingSchema],
) -> tuple[GenerationManifest, ArtifactRef]:
    pdf = CASPublisher(client).publish_bytes(b"%PDF-test", media_type="application/pdf")
    generation_id = "gen-20260825"
    database_bytes = b"SQLite format 3\x00test"
    database = ImmutablePublisher(client).publish_bytes(
        generation_database_path(generation_id),
        database_bytes,
        media_type="application/vnd.sqlite3",
    )
    document = GenerationDocument(
        document_id="doc_lotte",
        issuer="lotte",
        pdf=pdf,
        page_count=1,
    )
    manifest = GenerationManifest(
        schema_version=schema_pair[0],
        generation_id=generation_id,
        created_at=datetime(2026, 8, 25, tzinfo=UTC),
        serving_schema=schema_pair[1],
        serving_database=database,
        corpus_sha256=sha256_bytes(b"corpus"),
        contract_sha256=sha256_bytes(b"contract"),
        embedding_contract=EmbeddingContract(
            provider="openrouter",
            model="openai/text-embedding-3-small",
            dimension=1536,
            count=1,
        ),
        issuer_codes=("lotte",),
        counts=GenerationCounts(documents=1, pdf_objects=1, ocr_objects=0, chunks=1),
        documents=(document,),
    )
    ImmutablePublisher(client).publish_bytes(
        generation_manifest_path(generation_id),
        manifest.canonical_bytes(),
        media_type="application/json",
    )
    ready = GenerationReady(
        generation_id=generation_id,
        manifest_sha256=manifest.manifest_sha256,
        serving_database_sha256=database.sha256,
        serving_database_size_bytes=database.size_bytes,
    )
    ImmutablePublisher(client).publish_bytes(
        generation_ready_path(generation_id),
        ready.canonical_bytes(),
        media_type="application/json",
    )
    pointer = GenerationPointer(
        generation_id=generation_id,
        manifest_sha256=manifest.manifest_sha256,
        ready_sha256=sha256_bytes(ready.canonical_bytes()),
    )
    client.ensure_collection(STABLE_POINTER_PATH.parent)
    client.put(STABLE_POINTER_PATH, pointer.canonical_bytes(), content_type="application/json")
    return manifest, pdf


def _publish_current_v5_generation(
    client: WebDAVClient,
) -> tuple[GenerationManifest, bytes]:
    generation_id = "gen-20260829-v5"
    pdf = CASPublisher(client).publish_bytes(b"%PDF-v5", media_type="application/pdf")
    ocr = CASPublisher(client).publish_bytes(
        b"one",
        media_type="text/markdown; charset=utf-8",
    )
    database = ImmutablePublisher(client).publish_bytes(
        generation_database_path(generation_id),
        b"SQLite format 3\x00v5",
        media_type="application/vnd.sqlite3",
    )
    vector_bytes = b"\x00" * (4096 * 4)
    vector_artifact = ImmutablePublisher(client).publish_bytes(
        generation_vectors_path(generation_id),
        vector_bytes,
        media_type="application/octet-stream",
    )
    profile = EmbeddingProfile.qwen3(provider_id="deepinfra", maximum_tokens=8192)
    coverage_hash = sha256_bytes(b"one")
    manifest = GenerationManifest(
        schema_version="cardrag.generation.v5",
        generation_id=generation_id,
        created_at=datetime(2026, 8, 29, tzinfo=UTC),
        serving_schema="cardrag.serving-db.v5",
        serving_database=database,
        corpus_sha256=sha256_bytes(b"v5-corpus"),
        contract_sha256=sha256_bytes(b"v5-contract"),
        embedding_contract=EmbeddingContract(
            provider="openrouter",
            model="qwen/qwen3-embedding-8b",
            dimension=4096,
            count=1,
        ),
        issuer_codes=("lotte",),
        counts=GenerationCounts(documents=1, pdf_objects=1, ocr_objects=1, chunks=1),
        documents=(
            GenerationDocument(
                document_id="doc_lotte_v5",
                issuer="lotte",
                pdf=pdf,
                ocr=ocr,
                page_count=1,
                availability="available",
            ),
        ),
        issuer_ocr_counts=(IssuerOCRCounts(issuer="lotte", acquired=1, succeeded=1, failed=0),),
        structure_contract=StructureContract(
            schema_version="cardrag.structure.v2",
            parser_profiles=(
                IssuerParserProfile(
                    issuer="lotte",
                    profile_id="cardrag.parser.lotte.v1",
                    profile_sha256=sha256_bytes(b"lotte-parser"),
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
                source_non_whitespace_characters=3,
                covered_non_whitespace_characters=3,
                source_non_whitespace_sha256=coverage_hash,
                covered_non_whitespace_sha256=coverage_hash,
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
        embedding_profiles=(profile,),
        primary_embedding_profile_id=profile.profile_id,
        embedding_view_counts=tuple(
            EmbeddingViewCount(view_type=view_type, count=int(index == 0))
            for index, view_type in enumerate(EMBEDDING_VIEW_TYPES)
        ),
        vector_sidecar=EmbeddingVectorSidecar(
            artifact=vector_artifact,
            profile_id=profile.profile_id,
            row_count=1,
            dimension=4096,
            dtype="float32",
            byte_order="little-endian",
            layout="row-major",
            normalization="l2",
        ),
        parser_policy_sha256=sha256_bytes(b"parser-policy"),
        embedding_policy_sha256=sha256_bytes(b"embedding-policy"),
        retrieval_policy_sha256=sha256_bytes(b"retrieval-policy"),
    )
    ImmutablePublisher(client).publish_bytes(
        generation_manifest_path(generation_id),
        manifest.canonical_bytes(),
        media_type="application/json",
    )
    ready = GenerationReady(
        generation_id=generation_id,
        manifest_sha256=manifest.manifest_sha256,
        serving_database_sha256=database.sha256,
        serving_database_size_bytes=database.size_bytes,
        vector_sidecar_sha256=vector_artifact.sha256,
        vector_sidecar_size_bytes=vector_artifact.size_bytes,
    )
    ImmutablePublisher(client).publish_bytes(
        generation_ready_path(generation_id),
        ready.canonical_bytes(),
        media_type="application/json",
    )
    pointer = GenerationPointer(
        generation_id=generation_id,
        manifest_sha256=manifest.manifest_sha256,
        ready_sha256=sha256_bytes(ready.canonical_bytes()),
    )
    client.ensure_collection(STABLE_POINTER_PATH.parent)
    client.put(STABLE_POINTER_PATH, pointer.canonical_bytes(), content_type="application/json")
    return manifest, vector_bytes


@pytest.mark.parametrize("schema_pair", SCHEMA_PAIRS)
def test_mcp_facade_exposes_verified_supported_schema_reads_only(
    webdav: tuple[_MemoryWebDAV, WebDAVClient],
    tmp_path: Path,
    schema_pair: tuple[GenerationSchema, ServingSchema],
) -> None:
    _, client = webdav
    manifest, pdf = _publish_current_generation(client, schema_pair=schema_pair)
    read_only = client.read_only()
    assert not hasattr(read_only, "put")
    assert not hasattr(read_only, "move")
    assert not hasattr(read_only, "delete")
    reader = MCPArtifactReader(read_only)
    current = reader.read_current_generation()
    assert current.manifest == manifest
    assert (current.manifest.schema_version, current.manifest.serving_schema) == schema_pair
    assert reader.read_object(pdf) == b"%PDF-test"
    destination = (tmp_path / "index.sqlite3").resolve()
    reader.download_serving_database(destination, current=current)
    assert destination.read_bytes() == b"SQLite format 3\x00test"


def test_mcp_facade_rejects_pointer_ready_tampering(
    webdav: tuple[_MemoryWebDAV, WebDAVClient],
) -> None:
    backend, client = webdav
    _publish_current_generation(client, schema_pair=SCHEMA_PAIRS[-1])
    pointer = GenerationPointer.model_validate_json(backend.files[STABLE_POINTER_PATH.as_posix()])
    forged = pointer.model_copy(update={"ready_sha256": "0" * 64})
    backend.files[STABLE_POINTER_PATH.as_posix()] = forged.canonical_bytes()
    with pytest.raises(ArtifactContractError, match="does not bind"):
        MCPArtifactReader(client.read_only()).read_current_generation()


def test_mcp_facade_downloads_only_ready_bound_v5_vector_sidecar(
    webdav: tuple[_MemoryWebDAV, WebDAVClient],
    tmp_path: Path,
) -> None:
    backend, client = webdav
    manifest, vector_bytes = _publish_current_v5_generation(client)
    reader = MCPArtifactReader(client.read_only())
    current = reader.read_current_generation()
    destination = (tmp_path / "vectors.f32").resolve()

    verified = reader.download_vector_sidecar(destination, current=current)

    assert verified.sha256 == sha256_bytes(vector_bytes)
    assert destination.read_bytes() == vector_bytes
    assert manifest.vector_sidecar is not None
    vector_path = manifest.vector_sidecar.artifact.path
    backend.files[vector_path] = b"x" * len(vector_bytes)
    last_good = (tmp_path / "last-good.f32").resolve()
    last_good.write_bytes(b"last-good")
    with pytest.raises(WebDAVIntegrityError, match="SHA-256"):
        reader.download_vector_sidecar(last_good, current=current)
    assert last_good.read_bytes() == b"last-good"


def test_mcp_facade_rejects_v5_ready_sidecar_cross_pair(
    webdav: tuple[_MemoryWebDAV, WebDAVClient],
) -> None:
    backend, client = webdav
    manifest, _ = _publish_current_v5_generation(client)
    ready_path = generation_ready_path(manifest.generation_id).as_posix()
    ready = GenerationReady.model_validate_json(backend.files[ready_path])
    forged = ready.model_copy(update={"vector_sidecar_sha256": "0" * 64})
    backend.files[ready_path] = forged.canonical_bytes()
    pointer = GenerationPointer(
        generation_id=manifest.generation_id,
        manifest_sha256=manifest.manifest_sha256,
        ready_sha256=sha256_bytes(forged.canonical_bytes()),
    )
    backend.files[STABLE_POINTER_PATH.as_posix()] = pointer.canonical_bytes()

    with pytest.raises(ArtifactContractError, match="does not bind the vector sidecar"):
        MCPArtifactReader(client.read_only()).read_current_generation()


def test_move_refuses_overwrite(webdav: tuple[_MemoryWebDAV, WebDAVClient]) -> None:
    _, client = webdav
    client.ensure_collection("v1/move")
    client.put("v1/move/a", b"a")
    client.put("v1/move/b", b"b")
    with pytest.raises(WebDAVHTTPError) as failure:
        client.move("v1/move/a", "v1/move/b", overwrite=False)
    assert failure.value.status_code == 412
