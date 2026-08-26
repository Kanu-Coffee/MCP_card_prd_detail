from __future__ import annotations

import hashlib
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
    ProtectedSourceAllowance,
    SourceRecord,
    snapshot_from_records,
)
from cardrag_worker.downloader import ProtectedDocumentError, validate_pdf
from cardrag_worker.downloader import SecurePDFDownloader as RealDownloader
from cardrag_worker.pipeline import WorkerPipeline
from cardrag_worker.state import WorkerState
from cardrag_worker.webdav import RemoteGenerationIdentity


class StopAfterNoChangeCheck(RuntimeError):
    pass


def test_remote_generation_identity_rejects_cross_schema_pair() -> None:
    with pytest.raises(ValueError, match="schema versions must match"):
        RemoteGenerationIdentity(
            generation_id="g-cross",
            corpus_sha256="a" * 64,
            contract_sha256="b" * 64,
            generation_schema="cardrag.generation.v2",
            serving_schema="cardrag.serving-db.v3",
        )


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


def source(
    *,
    product_code: str = "p1",
    source_url: str = "https://cards.example/current.pdf",
) -> SourceRecord:
    return SourceRecord(
        issuer="testbank",
        product_code=product_code,
        product_name="테스트 카드",
        effective_date=date(2026, 8, 1),
        source_version="1",
        source_url=source_url,
        source_post_id=f"post-{product_code}",
        file_name=source_url.rsplit("/", 1)[-1],
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


def corpus_for(
    payload: bytes,
    record: SourceRecord,
    tmp_path: Path,
    *,
    unsupported: tuple[tuple[SourceRecord, bytes], ...] = (),
) -> str:
    protected_magic = {
        b"SCDSA002": "SCDSA002",
        b"SCDSA004": "SCDSA004",
        b"\x9b DRMONE": "FASOO_DRMONE",
    }
    path = tmp_path / "identity.pdf"
    path.write_bytes(payload)
    digest, size, pages = validate_pdf(path)
    return pipeline_module.canonical_sha256(
        {
            "schema_version": "cardrag.current-corpus.v2",
            "documents": [
                {
                    "source": record.discovery_payload,
                    "pdf_sha256": digest,
                    "pdf_size_bytes": size,
                    "page_count": pages,
                }
            ],
            "unsupported_documents": sorted(
                (
                    {
                        "disposition": "unsupported_drm",
                        "protected_magic": protected_magic[body[:8]],
                        "protected_sha256": hashlib.sha256(body).hexdigest(),
                        "protected_size_bytes": len(body),
                        "source": row.discovery_payload,
                        "source_id": row.source_id,
                    }
                    for row, body in unsupported
                ),
                key=pipeline_module.canonical_json_bytes,
            ),
        }
    )


@pytest.mark.asyncio
async def test_explicit_protected_product_is_audited_once_and_part_of_corpus_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protected = source(product_code="p1", source_url="https://cards.example/protected.pdf")
    valid = source(product_code="p2", source_url="https://cards.example/current.pdf")
    payload = pdf_bytes()
    protected_payload = b"\x9b DRMONE" + b"\x00" * 64
    requests: list[str] = []
    real_async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        content = protected_payload if request.url.path.endswith("protected.pdf") else payload
        return httpx.Response(
            200,
            headers={"content-type": "application/pdf"},
            content=content,
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
    adapter = Adapter((protected, valid))
    adapter.spec = replace(
        adapter.spec,
        protected_source_allowances=(
            ProtectedSourceAllowance(
                source_id=protected.source_id,
                product_code=protected.product_code,
                source_version=protected.source_version,
                source_url=protected.source_url,
                sha256=hashlib.sha256(protected_payload).hexdigest(),
                size_bytes=len(protected_payload),
                magic="FASOO_DRMONE",
            ),
        ),
    )
    with WorkerState(tmp_path / "state.sqlite3") as state:
        webdav = FakeWebDAV(None)
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[adapter],
            ocr=FakeOCR(),  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=webdav,  # type: ignore[arg-type]
            collect_remote_garbage=False,
        )
        webdav.current = RemoteGenerationIdentity(
            generation_id="g-current",
            corpus_sha256=corpus_for(
                payload,
                valid,
                tmp_path,
                unsupported=((protected, protected_payload),),
            ),
            contract_sha256=pipeline.contract_sha256,
        )
        result = await pipeline.run()
        skipped = state.get_stage(result.run_id, protected.source_id, "download")
        assert skipped is not None
        assert (skipped.status, skipped.attempt_count) == ("skipped", 1)
        assert skipped.last_error is not None and "unsupported_drm" in skipped.last_error
    assert result.status == "no_change"
    assert result.document_count == 1
    assert result.unsupported_document_count == 1
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_unlisted_protected_product_remains_a_terminal_download_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = source(source_url="https://cards.example/protected.pdf")
    requests: list[str] = []
    install_http(monkeypatch, b"SCDSA002" + b"\x00" * 64, requests)
    adapter = Adapter((record,))
    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[adapter],
            ocr=FakeOCR(),  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
        )
        with pytest.raises(ProtectedDocumentError):
            await pipeline.run()
        run_id = str(state.connection.execute("SELECT run_id FROM run").fetchone()[0])
        stage = state.get_stage(run_id, record.source_id, "download")
        assert stage is not None and stage.status == "failed"
    assert len(requests) == adapter.spec.maximum_retries


@pytest.mark.asyncio
async def test_changed_protected_bytes_do_not_match_an_approved_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = source(source_url="https://cards.example/protected.pdf")
    protected_payload = b"\x9b DRMONE" + b"changed"
    requests: list[str] = []
    install_http(monkeypatch, protected_payload, requests)
    adapter = Adapter((record,))
    adapter.spec = replace(
        adapter.spec,
        protected_source_allowances=(
            ProtectedSourceAllowance(
                source_id=record.source_id,
                product_code=record.product_code,
                source_version=record.source_version,
                source_url=record.source_url,
                sha256="f" * 64,
                size_bytes=len(protected_payload),
                magic="FASOO_DRMONE",
            ),
        ),
    )
    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[adapter],
            ocr=FakeOCR(),  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
        )
        with pytest.raises(ProtectedDocumentError):
            await pipeline.run()
        run_id = str(state.connection.execute("SELECT run_id FROM run").fetchone()[0])
        stage = state.get_stage(run_id, record.source_id, "download")
        assert stage is not None and stage.status == "failed"
        assert state.stage_status_count(run_id, "download", "skipped") == 0
    assert len(requests) == adapter.spec.maximum_retries


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
            generation_schema="cardrag.generation.v2",
            serving_schema="cardrag.serving-db.v2",
        )
        result = await pipeline.run()
    assert result.status == "no_change"
    assert webdav.current.generation_schema == "cardrag.generation.v2"
    assert webdav.current.serving_schema == "cardrag.serving-db.v2"
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
