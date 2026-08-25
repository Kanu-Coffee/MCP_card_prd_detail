from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from helpers import pdf_bytes

import cardrag_worker.pipeline as pipeline_module
from cardrag_worker.contracts import (
    DownloadRequest,
    IssuerSpec,
    SourceRecord,
    snapshot_from_records,
)
from cardrag_worker.downloader import SecurePDFDownloader as RealDownloader
from cardrag_worker.downloader import validate_pdf
from cardrag_worker.pipeline import WorkerPipeline
from cardrag_worker.state import WorkerState
from cardrag_worker.webdav import RemoteGenerationIdentity


class StopAfterNoChangeCheck(RuntimeError):
    pass


class FakeOCR:
    contract = {"schema_version": "test-ocr.v1"}
    adoption_policy_version = "cardrag.legacy-ocr-adoption.v1"

    def __init__(self) -> None:
        self.calls = 0

    async def resolve(self, **kwargs: Any) -> Any:
        self.calls += 1
        raise StopAfterNoChangeCheck


class FakeEmbeddings:
    provider = "openrouter"
    model = "test-embedding"
    dimension = 1536

    def __init__(self) -> None:
        self.calls = 0

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [[1.0] + [0.0] * 1535 for _ in texts]


class FakeWebDAV:
    def __init__(self, current: RemoteGenerationIdentity | None) -> None:
        self.current = current

    async def validated_current_generation(self) -> RemoteGenerationIdentity | None:
        return self.current

    async def get_bytes(self, path: object, *, max_bytes: int | None = None) -> bytes | None:
        return b"stable" if self.current is not None else None


class Adapter:
    parser_version = "test-adapter.v1"

    def __init__(self, records: tuple[SourceRecord, ...]) -> None:
        self.records = records
        self.discovery_calls = 0
        self.prepare_calls = 0
        self.spec = IssuerSpec(
            code="testbank",
            display_name="테스트카드",
            sort_order=1,
            allowed_hosts=frozenset({"cards.example"}),
            categories=("credit",),
            minimum_interval_seconds=0,
            retry_base_seconds=0.001,
            maximum_retries=2,
        )

    async def discover_current(self, client: httpx.AsyncClient) -> Any:
        self.discovery_calls += 1
        return snapshot_from_records(
            issuer=self.spec.code,
            source_url="https://cards.example/list",
            parser_version=self.parser_version,
            records=self.records,
            started_at=datetime(2026, 8, 25, tzinfo=UTC),
        )

    async def prepare_download(self, client: httpx.AsyncClient, source: SourceRecord) -> DownloadRequest:
        self.prepare_calls += 1
        return DownloadRequest(url=source.source_url)


def source() -> SourceRecord:
    return SourceRecord(
        issuer="testbank",
        product_code="p1",
        product_name="테스트 카드",
        effective_date=date(2026, 8, 1),
        source_version="1",
        source_url="https://cards.example/current.pdf",
        source_post_id="post-1",
        file_name="current.pdf",
        category="credit",
        discovered_at=datetime(2026, 8, 25, tzinfo=UTC),
    )


def install_http(monkeypatch: pytest.MonkeyPatch, payload: bytes, requests: list[str]) -> None:
    real_async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            200,
            headers={"content-type": "application/pdf"},
            content=payload,
            request=request,
        )

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return real_async_client(transport=httpx.MockTransport(handler), timeout=kwargs.get("timeout"))

    monkeypatch.setattr(pipeline_module.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(
        pipeline_module,
        "SecurePDFDownloader",
        lambda policy: RealDownloader(policy, resolver=lambda _host: ("93.184.216.34",)),
    )


def corpus_for(payload: bytes, record: SourceRecord, tmp_path: Path) -> str:
    path = tmp_path / "identity.pdf"
    path.write_bytes(payload)
    digest, size, pages = validate_pdf(path)
    return pipeline_module.canonical_sha256(
        {
            "schema_version": "cardrag.current-corpus.v1",
            "documents": [
                {
                    "source": record.discovery_payload,
                    "pdf_sha256": digest,
                    "pdf_size_bytes": size,
                    "page_count": pages,
                }
            ],
        }
    )


@pytest.mark.asyncio
async def test_remote_exact_match_is_no_change_without_local_publish_row_or_provider_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = pdf_bytes()
    record = source()
    requests: list[str] = []
    install_http(monkeypatch, payload, requests)
    ocr = FakeOCR()
    embeddings = FakeEmbeddings()
    adapter = Adapter((record,))
    with WorkerState(tmp_path / "state.sqlite3") as state:
        webdav = FakeWebDAV(None)
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[adapter],
            ocr=ocr,  # type: ignore[arg-type]
            embeddings=embeddings,
            webdav=webdav,  # type: ignore[arg-type]
            collect_remote_garbage=False,
        )
        webdav.current = RemoteGenerationIdentity(
            generation_id="g-current",
            corpus_sha256=corpus_for(payload, record, tmp_path),
            contract_sha256=pipeline.contract_sha256,
        )
        result = await pipeline.run()
    assert result.status == "no_change"
    assert len(requests) == 1
    assert adapter.prepare_calls == 1
    assert ocr.calls == embeddings.calls == 0


@pytest.mark.asyncio
async def test_same_url_with_changed_pdf_bytes_does_not_false_no_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_payload = pdf_bytes(width=612)
    new_payload = pdf_bytes(width=613)
    record = source()
    requests: list[str] = []
    install_http(monkeypatch, new_payload, requests)
    ocr = FakeOCR()
    adapter = Adapter((record,))
    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[adapter],
            ocr=ocr,  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(  # type: ignore[arg-type]
                RemoteGenerationIdentity(
                    generation_id="g-old",
                    corpus_sha256=corpus_for(old_payload, record, tmp_path),
                    contract_sha256="0" * 64,
                )
            ),
            collect_remote_garbage=False,
            maximum_attempts=1,
        )
        pipeline.webdav.current = RemoteGenerationIdentity(  # type: ignore[attr-defined]
            generation_id="g-old",
            corpus_sha256=corpus_for(old_payload, record, tmp_path),
            contract_sha256=pipeline.contract_sha256,
        )
        with pytest.raises(StopAfterNoChangeCheck):
            await pipeline.run()
    assert len(requests) == 1
    assert ocr.calls == 1


@pytest.mark.asyncio
async def test_discovery_drop_fails_before_download_and_pointer_update(tmp_path: Path) -> None:
    record = source()
    adapter = Adapter((record,))
    with WorkerState(tmp_path / "state.sqlite3") as state:
        baseline = state.start_run(run_id="baseline")
        state.record_snapshot(
            run_id=baseline,
            snapshot_id="a" * 64,
            issuer="testbank",
            source_sha256="a" * 64,
            record_count=4,
            payload={"records": []},
        )
        state.finish_run(baseline, "succeeded")
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[adapter],
            ocr=FakeOCR(),  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
        )
        with pytest.raises(RuntimeError, match="fell below"):
            await pipeline.run()
    assert adapter.prepare_calls == 0


def test_serving_affecting_issuer_catalog_fields_change_worker_contract(tmp_path: Path) -> None:
    adapter = Adapter((source(),))
    with WorkerState(tmp_path / "state.sqlite3") as state:
        first = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[adapter],
            ocr=FakeOCR(),  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
        ).contract_sha256
        adapter.spec = replace(adapter.spec, display_name="변경된 표시명")
        second = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[adapter],
            ocr=FakeOCR(),  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
        ).contract_sha256
    assert first != second
