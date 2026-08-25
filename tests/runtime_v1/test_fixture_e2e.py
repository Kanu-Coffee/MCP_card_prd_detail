from __future__ import annotations

import hashlib
import shutil
import threading
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import numpy as np
import pytest
from cardrag_core import MCPArtifactReader, WebDAVSettings
from cardrag_core import WebDAVClient as CoreWebDAVClient
from cardrag_mcp.app import build_app
from cardrag_mcp.config import Settings
from cardrag_mcp.observability import Metrics
from cardrag_mcp.repository import ServingRepository
from cardrag_mcp.store import GenerationStore
from cardrag_mcp.transport import CoreArtifactReader
from cardrag_mcp.updater import WebDAVUpdater
from cardrag_worker.contracts import (
    DownloadRequest,
    IssuerSpec,
    SourceRecord,
    SourceSnapshot,
    snapshot_from_records,
)
from cardrag_worker.downloader import DownloadedPDF, SecurePDFDownloader, validate_pdf
from cardrag_worker.ocr import OCRResolver
from cardrag_worker.pipeline import WorkerPipeline
from cardrag_worker.state import WorkerState
from cardrag_worker.webdav import WebDAVClient as WorkerWebDAVClient
from fastapi.testclient import TestClient
from pydantic import SecretStr
from pypdf import PdfWriter


class MemoryWebDAV:
    """The small RFC 4918 subset exercised by both production facades."""

    def __init__(self, base_path: str = "/cardrag") -> None:
        self.base_path = base_path.rstrip("/")
        self.objects: dict[str, bytes] = {}
        self.collections = {self.base_path}
        self._lock = threading.Lock()

    @staticmethod
    def _etag(body: bytes) -> str:
        return f'"{hashlib.sha256(body).hexdigest()}"'

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.rstrip("/") or "/"
        method = request.method
        with self._lock:
            if method == "HEAD":
                if path in self.objects:
                    body = self.objects[path]
                    return httpx.Response(
                        200,
                        headers={"Content-Length": str(len(body)), "ETag": self._etag(body)},
                    )
                return httpx.Response(200 if path in self.collections else 404)
            if method == "GET":
                body = self.objects.get(path)
                if body is None:
                    return httpx.Response(404)
                return httpx.Response(
                    200,
                    content=body,
                    headers={"Content-Length": str(len(body)), "ETag": self._etag(body)},
                )
            if method == "MKCOL":
                if path in self.collections:
                    return httpx.Response(405)
                self.collections.add(path)
                return httpx.Response(201)
            if method == "PUT":
                if request.headers.get("if-none-match") == "*" and path in self.objects:
                    return httpx.Response(412)
                self.objects[path] = request.read()
                return httpx.Response(201)
            if method == "MOVE":
                destination = urlsplit(request.headers["destination"]).path.rstrip("/")
                if path not in self.objects:
                    return httpx.Response(404)
                if request.headers.get("overwrite") == "F" and destination in self.objects:
                    return httpx.Response(412)
                self.objects[destination] = self.objects.pop(path)
                return httpx.Response(201)
            if method == "DELETE":
                existed = path in self.objects or path in self.collections
                self.objects.pop(path, None)
                self.collections.discard(path)
                return httpx.Response(204 if existed else 404)
            if method == "PROPFIND":
                if path not in self.collections:
                    return httpx.Response(404)
                body = (
                    '<?xml version="1.0" encoding="utf-8"?>'
                    '<d:multistatus xmlns:d="DAV:">'
                    f"<d:response><d:href>{path}/</d:href></d:response>"
                    "</d:multistatus>"
                ).encode()
                return httpx.Response(207, content=body)
        return httpx.Response(405)


class FixtureAdapter:
    parser_version = "fixture.current.v1"
    spec = IssuerSpec(
        code="fixture",
        display_name="Fixture Card",
        sort_order=1,
        allowed_hosts=frozenset({"issuer.test"}),
        categories=("current",),
        minimum_interval_seconds=0,
    )

    def __init__(self, record: SourceRecord) -> None:
        self.record = record

    async def discover_current(self, client: httpx.AsyncClient) -> SourceSnapshot:
        del client
        return snapshot_from_records(
            issuer=self.spec.code,
            source_url="https://issuer.test/current",
            parser_version=self.parser_version,
            records=(self.record,),
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    async def prepare_download(self, client: httpx.AsyncClient, source: SourceRecord) -> DownloadRequest:
        del client
        return DownloadRequest(source.source_url)


class FixtureOCR:
    provider = "fixture-ocr"
    model = "fixture-ocr-v1"
    reasoning_effort = None

    def __init__(self) -> None:
        self.calls = 0

    async def recognize(
        self,
        images: tuple[Path, ...] | list[Path],
        *,
        first_page: int,
        prompt: str,
    ) -> str:
        del prompt
        self.calls += 1
        return (
            "\n\n".join(
                f"## Page {first_page + index}\n\nAirport lounge benefit for premium card holders."
                for index, _ in enumerate(images)
            )
            + "\n"
        )


class FixtureEmbeddings:
    provider = "openrouter"
    model = "openai/text-embedding-3-small"
    dimension = 1536

    def __init__(self) -> None:
        self.calls = 0

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        vector = [0.0] * self.dimension
        vector[0] = 1.0
        return [vector.copy() for _ in texts]


class FixtureQueryEmbedder:
    async def embed(self, query: str, *, provider: str, model: str) -> np.ndarray:
        del query, provider, model
        vector = np.zeros(1536, dtype=np.float32)
        vector[0] = 1
        return vector

    async def close(self) -> None:
        return None


def _pdf(path: Path) -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with path.open("wb") as output:
        writer.write(output)
    return path.read_bytes()


@pytest.mark.asyncio
async def test_worker_webdav_mcp_search_and_pdf_range(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_pdf = tmp_path / "source.pdf"
    pdf_body = _pdf(source_pdf)
    record = SourceRecord(
        issuer="fixture",
        product_code="CARD-1",
        product_name="Fixture Premium",
        effective_date=date(2026, 1, 1),
        source_version="20260101",
        source_url="https://issuer.test/card.pdf",
        source_post_id="card-1",
        file_name="card.pdf",
        category="current",
        discovered_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    async def fixture_download(
        downloader: SecurePDFDownloader,
        client: httpx.AsyncClient,
        request: DownloadRequest,
        destination: Path,
        *,
        expected_sha256: str | None = None,
    ) -> DownloadedPDF:
        del downloader, client, request
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_pdf, destination)
        digest, size, pages = validate_pdf(destination, expected_sha256=expected_sha256)
        return DownloadedPDF(destination, digest, size, pages, record.source_url)

    monkeypatch.setattr(SecurePDFDownloader, "download", fixture_download)

    memory = MemoryWebDAV()
    core_client = CoreWebDAVClient(
        WebDAVSettings(
            environment="test",
            base_url="https://dav.test/cardrag",
            username="fixture",
            password=SecretStr("fixture-password"),
        ),
        transport=httpx.MockTransport(memory),
    )
    worker_webdav = WorkerWebDAVClient(core_client)
    worker_root = tmp_path / "worker"
    ocr_provider = FixtureOCR()
    embeddings = FixtureEmbeddings()
    with WorkerState(worker_root / "worker-state.sqlite3") as state:
        resolver = OCRResolver(
            provider=ocr_provider,
            state=state,
            webdav=worker_webdav,
            chunk_pages=1,
        )
        pipeline = WorkerPipeline(
            state=state,
            state_dir=worker_root,
            adapters=(FixtureAdapter(record),),
            ocr=resolver,
            embeddings=embeddings,
            webdav=worker_webdav,
            collect_remote_garbage=False,
        )
        first = await pipeline.run()
        stable_path = "/cardrag/v1/channels/stable.json"
        first_pointer = memory.objects[stable_path]
        assert first.status == "succeeded"
        assert ocr_provider.calls == 1
        assert embeddings.calls == 1

        second = await pipeline.run()
        assert second.status == "no_change"
        assert memory.objects[stable_path] == first_pointer
        assert ocr_provider.calls == 1
        assert embeddings.calls == 1

    # WebDAV is the published source of truth: losing disposable Worker state
    # must not make an identical verified corpus invoke OCR/embedding again.
    recovered_root = tmp_path / "worker-recovered"
    with WorkerState(recovered_root / "worker-state.sqlite3") as recovered_state:
        recovered_resolver = OCRResolver(
            provider=ocr_provider,
            state=recovered_state,
            webdav=worker_webdav,
            chunk_pages=1,
        )
        recovered = await WorkerPipeline(
            state=recovered_state,
            state_dir=recovered_root,
            adapters=(FixtureAdapter(record),),
            ocr=recovered_resolver,
            embeddings=embeddings,
            webdav=worker_webdav,
            collect_remote_garbage=False,
        ).run()
        assert recovered.status == "no_change"
        assert memory.objects[stable_path] == first_pointer
        assert ocr_provider.calls == 1
        assert embeddings.calls == 1

    read_only = core_client.read_only()
    reader = CoreArtifactReader(MCPArtifactReader(read_only), read_only)
    store = GenerationStore(tmp_path / "mcp", maximum_vector_bytes=1024 * 1024)
    updater = WebDAVUpdater(reader, store, Metrics.create(), poll_seconds=300)
    assert await updater.poll_once() is True

    repository = ServingRepository(
        store,
        FixtureQueryEmbedder(),  # type: ignore[arg-type]
        cursor_secret=b"fixture-cursor-secret-that-is-long-enough",
    )
    from cardrag_mcp.models import SearchRequest

    results = await repository.search(SearchRequest(query="Airport lounge"))
    assert results.items
    assert results.items[0].issuer == "fixture"

    token = hashlib.sha256(b"fixture bearer credential").hexdigest()
    settings = Settings(
        environment="test",
        mcp_bearer_token=token,
        mcp_state_dir=store.root,
        mcp_public_base_url="http://testserver",
    )
    app = build_app(repository, store, settings)
    headers = {"Authorization": f"Bearer {token}", "Range": "bytes=0-4"}
    document_id = results.items[0].document_id
    with TestClient(app) as client:
        response = client.get(f"/sources/{document_id}/pdf", headers=headers)
        assert response.status_code == 206
        assert response.content == pdf_body[:5] == b"%PDF-"
    await updater.close()
