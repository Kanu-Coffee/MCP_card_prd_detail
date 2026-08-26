from __future__ import annotations

import base64
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx
import pytest
from pydantic import SecretStr

from cardrag_core import (
    STABLE_POINTER_PATH,
    ArtifactContractError,
    ArtifactRef,
    CASPublisher,
    EmbeddingContract,
    GenerationCounts,
    GenerationDocument,
    GenerationManifest,
    GenerationPointer,
    GenerationReady,
    ImmutablePublisher,
    MCPArtifactReader,
    StablePointerPublisher,
    WebDAVClient,
    WebDAVHTTPError,
    WebDAVIntegrityError,
    WebDAVSettings,
    generation_database_path,
    generation_manifest_path,
    generation_ready_path,
    object_path,
    sha256_bytes,
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


def test_immutable_publisher_accepts_verified_409_move_collision(
    webdav: tuple[_MemoryWebDAV, WebDAVClient],
) -> None:
    backend, client = webdav
    backend.inject_move_collision_status = 409
    reference = CASPublisher(client).publish_bytes(b"raced", media_type="text/plain")
    assert backend.files[reference.path] == b"raced"
    assert not any(".incoming" in path for path in backend.files)


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


def _publish_current_generation(
    client: WebDAVClient,
    *,
    legacy_v1: bool = False,
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
        schema_version="cardrag.generation.v1" if legacy_v1 else "cardrag.generation.v2",
        generation_id=generation_id,
        created_at=datetime(2026, 8, 25, tzinfo=UTC),
        serving_schema="cardrag.serving-db.v1" if legacy_v1 else "cardrag.serving-db.v2",
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


def test_mcp_facade_exposes_verified_reads_only(
    webdav: tuple[_MemoryWebDAV, WebDAVClient],
    tmp_path: Path,
) -> None:
    _, client = webdav
    manifest, pdf = _publish_current_generation(client)
    read_only = client.read_only()
    assert not hasattr(read_only, "put")
    assert not hasattr(read_only, "move")
    assert not hasattr(read_only, "delete")
    reader = MCPArtifactReader(read_only)
    current = reader.read_current_generation()
    assert current.manifest == manifest
    assert reader.read_object(pdf) == b"%PDF-test"
    destination = (tmp_path / "index.sqlite3").resolve()
    reader.download_serving_database(destination, current=current)
    assert destination.read_bytes() == b"SQLite format 3\x00test"


def test_artifact_reader_verifies_a_legacy_v1_head_for_worker_successor(
    webdav: tuple[_MemoryWebDAV, WebDAVClient],
) -> None:
    _, client = webdav
    manifest, _ = _publish_current_generation(client, legacy_v1=True)
    current = MCPArtifactReader(client.read_only()).read_current_generation()
    assert current.manifest == manifest
    assert current.manifest.schema_version == "cardrag.generation.v1"
    assert current.manifest.serving_schema == "cardrag.serving-db.v1"


def test_mcp_facade_rejects_pointer_ready_tampering(
    webdav: tuple[_MemoryWebDAV, WebDAVClient],
) -> None:
    backend, client = webdav
    _publish_current_generation(client)
    pointer = GenerationPointer.model_validate_json(backend.files[STABLE_POINTER_PATH.as_posix()])
    forged = pointer.model_copy(update={"ready_sha256": "0" * 64})
    backend.files[STABLE_POINTER_PATH.as_posix()] = forged.canonical_bytes()
    with pytest.raises(ArtifactContractError, match="does not bind"):
        MCPArtifactReader(client.read_only()).read_current_generation()


def test_move_refuses_overwrite(webdav: tuple[_MemoryWebDAV, WebDAVClient]) -> None:
    _, client = webdav
    client.ensure_collection("v1/move")
    client.put("v1/move/a", b"a")
    client.put("v1/move/b", b"b")
    with pytest.raises(WebDAVHTTPError) as failure:
        client.move("v1/move/a", "v1/move/b", overwrite=False)
    assert failure.value.status_code == 412
