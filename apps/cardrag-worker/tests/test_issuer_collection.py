from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from cardrag_core import GenerationManifest, WebDAVSettings, generation_manifest_path
from cardrag_core import WebDAVClient as CoreWebDAVClient
from helpers import pdf_bytes
from pydantic import SecretStr
from test_content_webdav_rehearsal import IsolatedWebDAV
from test_pipeline import Adapter, FakeEmbeddings, FakeOCR, FakeWebDAV, install_http, source
from test_pipeline_v5 import _test_qwen_embeddings

from cardrag_worker.issuer_collection import origin_failure
from cardrag_worker.ocr import OCRResolver
from cardrag_worker.pipeline import OCRSystemicFailureError, WorkerPipeline, WorkerUnexpectedFailureError
from cardrag_worker.settings import WorkerSettings
from cardrag_worker.state import WorkerState
from cardrag_worker.webdav import WebDAVClient


class IssuerAdapterFixture(Adapter):
    def __init__(self, code: str) -> None:
        super().__init__((replace(source(), issuer=code),))
        self.spec = replace(
            self.spec, code=code, display_name=code, sort_order=1 if code == "woori" else 2, maximum_retries=1
        )
        self.fail: str | None = None
        self.entered: asyncio.Event | None = None
        self.peer: asyncio.Event | None = None
        self.client: Any = None

    async def discover_current(self, client: Any) -> Any:
        self.client = client
        if self.entered is not None:
            self.entered.set()
        if self.peer is not None:
            await self.peer.wait()
        if self.fail == "reset":
            raise httpx.ReadError("PRIVATE_TOKEN_IN_RESPONSE")
        if self.fail == "timeout":
            await asyncio.Event().wait()
        if self.fail == "parser":
            raise ValueError("PRIVATE_HTML_RESPONSE")
        return await super().discover_current(client)

    async def prepare_download(self, client: Any, record: Any) -> Any:
        if self.fail == "prepare":
            raise httpx.ReadError("PRIVATE_COOKIE")
        return await super().prepare_download(client, record)


@pytest.mark.parametrize("failure", ["reset", "timeout", "parser", "prepare"])
async def test_discovery_parallel_failure_isolation_and_collection_barrier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    good, bad = IssuerAdapterFixture("woori"), IssuerAdapterFixture("shinhan")
    good.entered, bad.entered = asyncio.Event(), asyncio.Event()
    good.peer, bad.peer = bad.entered, good.entered
    bad.fail = failure
    requests: list[str] = []
    install_http(monkeypatch, pdf_bytes(), requests)
    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[good, bad],
            ocr=FakeOCR(),
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            issuer_discovery_timeout_seconds=0.2,
            retry_cap_seconds=0,
        )
        # Stop precisely at the OCR boundary, after collection has fully drained.
        with pytest.raises(OCRSystemicFailureError):
            await asyncio.wait_for(pipeline.run(), 3)
        run_id = str(state.connection.execute("SELECT run_id FROM run").fetchone()[0])
        report = json.loads((tmp_path / "runs" / run_id / "reports" / "issuer-collection.json").read_bytes())
        assert report["collection_status"] == "degraded"
        outcomes = {o["issuer"]: o for o in report["issuers"]}
        assert outcomes["woori"]["download_status"] == "succeeded"
        assert outcomes["shinhan"]["reason_code"] is not None
        assert good.client is not bad.client
        assert (
            state.connection.execute(
                "SELECT succeeded FROM issuer_collection WHERE issuer='shinhan'"
            ).fetchone()[0]
            == 0
        )
        assert "PRIVATE" not in json.dumps(report)
        assert (tmp_path / "runs" / run_id / "checkpoints" / "acquisition.v1.json").exists()


async def test_all_failed_has_no_ocr_or_publication(tmp_path: Path) -> None:
    adapters = [IssuerAdapterFixture("woori"), IssuerAdapterFixture("shinhan")]
    for adapter in adapters:
        adapter.fail = "reset"
    ocr = FakeOCR()
    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=adapters,
            ocr=ocr,  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),
            retry_cap_seconds=0,
        )  # type: ignore[arg-type]
        with pytest.raises(WorkerUnexpectedFailureError):
            await pipeline.run()
        assert ocr.calls == 0
        assert state.connection.execute("SELECT count(*) FROM publish").fetchone()[0] == 0
        run_id = state.connection.execute("SELECT run_id FROM run").fetchone()[0]
        report = json.loads((tmp_path / "runs" / run_id / "reports" / "issuer-collection.json").read_bytes())
        assert report["collection_status"] == "failed"


def test_origin_boundary_does_not_hide_local_storage_failures() -> None:
    for exc in (OSError("disk"), sqlite3.OperationalError("database locked"), MemoryError()):
        with pytest.raises(type(exc)):
            origin_failure(exc)


def test_collection_settings_defaults_and_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = WorkerSettings.from_env()
    assert settings.issuer_discovery_concurrency == 4
    assert settings.issuer_discovery_timeout_seconds == 300
    monkeypatch.setenv("CARDRAG_ISSUER_DISCOVERY_CONCURRENCY", "9")
    with pytest.raises(ValueError):
        WorkerSettings.from_env()
    monkeypatch.setenv("CARDRAG_ISSUER_DISCOVERY_CONCURRENCY", "1")
    monkeypatch.setenv("CARDRAG_ISSUER_DISCOVERY_TIMEOUT_SECONDS", "0")
    with pytest.raises(ValueError):
        WorkerSettings.from_env()


class VisionFixture:
    provider = "codex-exec"
    model = "test-model"
    reasoning_effort = "medium"

    def __init__(self) -> None:
        self.calls = 0

    async def recognize(self, *_args: Any, **kwargs: Any) -> str:
        self.calls += 1
        return "\n\n".join(
            f"## Page {page}\n\n테스트카드 상품설명서. 전월 이용실적 30만원 이상 대중교통 10% 할인, 월 최대 5천원. 상품권 구매금액은 실적에서 제외됩니다."
            for page in kwargs["target_page_numbers"]
        )


async def test_real_webdav_v5_shinhan_reset_carries_history_and_search_rows_then_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = IsolatedWebDAV()
    core = CoreWebDAVClient(
        WebDAVSettings(
            environment="test",
            base_url="http://127.0.0.1/dav",
            username="test",
            password=SecretStr("test"),
            allow_insecure_http=True,
        ),
        transport=httpx.MockTransport(backend),
    )
    webdav = WebDAVClient(core, stable_publication_approved=True)
    good, bad = IssuerAdapterFixture("woori"), IssuerAdapterFixture("shinhan")
    old = replace(
        bad.records[0], source_url="https://cards.example/shinhan-old.pdf", file_name="shinhan-old.pdf"
    )
    bad.records = (old,)
    pdfs = {good.records[0].source_url: pdf_bytes(width=612), old.source_url: pdf_bytes(width=620)}
    embeddings = _test_qwen_embeddings([])
    from test_pipeline import RealDownloader

    import cardrag_worker.pipeline as pipeline_module

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=pdfs[str(request.url)], headers={"content-type": "application/pdf"}
        )

    real_async_client = httpx.AsyncClient

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        kwargs.setdefault("transport", httpx.MockTransport(handler))
        return real_async_client(**kwargs)

    monkeypatch.setattr(pipeline_module.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(
        pipeline_module,
        "SecurePDFDownloader",
        lambda policy: RealDownloader(policy, resolver=lambda _host: ("93.184.216.34",)),
    )
    provider = VisionFixture()
    with WorkerState(tmp_path / "state.sqlite3") as state:

        def pipeline() -> WorkerPipeline:
            return WorkerPipeline(
                state=state,
                state_dir=tmp_path,
                adapters=[good, bad],
                ocr=OCRResolver(provider=provider, state=state, webdav=webdav),  # type: ignore[arg-type]
                embeddings=embeddings,
                webdav=webdav,
                stable_publication_approved=True,
                ocr_cache_publication_approved=True,
                retry_cap_seconds=0,
            )

        first = await pipeline().run()
        assert first.status == "succeeded"
        revised = replace(
            old,
            source_version="2",
            source_post_id="revised",
            source_url="https://cards.example/shinhan-new.pdf",
            file_name="shinhan-new.pdf",
        )
        bad.records = (revised,)
        pdfs[revised.source_url] = pdf_bytes(width=621)
        second = await pipeline().run()
        before = GenerationManifest.model_validate_json(
            backend.files[str(generation_manifest_path(second.generation_id))]
        )
        before_docs = {doc.document_id: doc for doc in before.documents if doc.issuer == "shinhan"}
        assert len(before_docs) == 2  # current plus superseded
        new_good = replace(
            good.records[0],
            product_code="p2",
            source_post_id="p2",
            source_url="https://cards.example/woori-new.pdf",
            file_name="woori-new.pdf",
        )
        good.records = (*good.records, new_good)
        pdfs[new_good.source_url] = pdf_bytes(width=650)
        bad.fail = "reset"
        provider_calls_before = provider.calls
        third_pipeline = pipeline()
        third = await third_pipeline.run()
        assert (third.status, third.collection_status, third.failed_issuers) == (
            "succeeded",
            "degraded",
            ("shinhan",),
        )
        assert provider.calls - provider_calls_before == 1  # only the new Woori PDF
        after = GenerationManifest.model_validate_json(
            backend.files[str(generation_manifest_path(third.generation_id))]
        )
        after_docs = {doc.document_id: doc for doc in after.documents if doc.issuer == "shinhan"}
        assert after_docs == before_docs
        database = Path(
            json.loads((tmp_path / "runs" / third.run_id / "sealed" / "publish.json").read_bytes())[
                "database_path"
            ]
        )
        with sqlite3.connect(database) as connection:
            rows = connection.execute(
                "SELECT temporal_status FROM contract_revisions r JOIN product_lineages p USING(product_lineage_id) WHERE p.issuer='shinhan' ORDER BY temporal_status"
            ).fetchall()
            assert rows == [("current",), ("superseded",)]
            assert (
                connection.execute(
                    "SELECT count(*) FROM document_pages d JOIN contract_revisions r USING(contract_revision_id) WHERE r.document_id IN (?,?)",
                    tuple(before_docs),
                ).fetchone()[0]
                == 2
            )
            assert (
                connection.execute(
                    "SELECT count(*) FROM embedding_views v JOIN contract_revisions r USING(contract_revision_id) WHERE r.document_id IN (?,?)",
                    tuple(before_docs),
                ).fetchone()[0]
                > 0
            )
        fourth = await pipeline().run()
        assert fourth.status == "no_change"
        assert fourth.collection_status == "degraded"
        assert provider.calls == provider_calls_before + 1
        # A model/provider change cannot invalidate content OCR or force a generation.
        provider.provider, provider.model = "opencode", "alibaba-token-plan/qwen3.8-flash"
        switched = await pipeline().run()
        assert switched.status == "no_change"
        assert provider.calls == provider_calls_before + 1
        bad.fail = None
        recovered = await pipeline().run()
        assert recovered.collection_status == "complete"
        assert provider.calls == provider_calls_before + 1
        from cardrag_worker.ocr_requests import (
            load_next_reprocess_request,
            plan_reprocess_requests,
            queue_reprocess_requests,
        )

        served = GenerationManifest.model_validate_json(
            backend.files[str(generation_manifest_path(recovered.generation_id))]
        )
        requests = plan_reprocess_requests(served, issuer="shinhan")
        queue_reprocess_requests(tmp_path, requests)
        bad.fail = "reset"
        deferred = await pipeline().run()
        assert deferred.status == "no_change"
        assert load_next_reprocess_request(tmp_path) == requests[0]
        assert provider.calls == provider_calls_before + 1
        collection = json.loads(
            (tmp_path / "runs" / deferred.run_id / "reports" / "issuer-collection.json").read_bytes()
        )
        assert set(collection["deferred_reprocess_document_ids"]) == set(before_docs)
        assert not (tmp_path / "ocr-requests" / "completed" / f"{requests[0].request_id}.json").exists()

        pointer_before = backend.files[str(webdav.pointer_path)]
        old_pdf = next(iter(before_docs.values())).pdf
        pipeline_for_corruption = pipeline()
        pipeline_for_corruption.pdf_cache.object_path(old_pdf.sha256).unlink()
        backend.files[old_pdf.path] = b"CORRUPTED PDF"
        with pytest.raises(WorkerUnexpectedFailureError):
            await pipeline_for_corruption.run()
        assert backend.files[str(webdav.pointer_path)] == pointer_before
        assert provider.calls == provider_calls_before + 1
    await webdav.close()


async def test_download_failure_discards_partial_issuer_and_drains_other_issuer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_pipeline import install_concurrent_pdf_http

    good, bad = IssuerAdapterFixture("woori"), IssuerAdapterFixture("shinhan")
    good.records = (
        replace(good.records[0], source_url="https://cards.example/good.pdf", file_name="good.pdf"),
    )
    bad.records = tuple(
        replace(
            bad.records[0],
            product_code=f"p{i}",
            source_post_id=f"p{i}",
            source_url=f"https://cards.example/bad-{i}.pdf",
            file_name=f"bad-{i}.pdf",
        )
        for i in range(5)
    )
    calls: list[str] = []
    first_bad_downloaded = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/bad-1.pdf":
            await first_bad_downloaded.wait()
            raise httpx.ReadError("PRIVATE_TOKEN", request=request)
        if request.url.path == "/bad-0.pdf":
            first_bad_downloaded.set()
        return httpx.Response(200, content=pdf_bytes(), headers={"content-type": "application/pdf"})

    install_concurrent_pdf_http(monkeypatch, handler)
    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[good, bad],
            ocr=FakeOCR(),  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),
            retry_cap_seconds=0,
        )  # type: ignore[arg-type]
        with pytest.raises(OCRSystemicFailureError):
            await pipeline.run()
        run_id = state.connection.execute("SELECT run_id FROM run").fetchone()[0]
        receipt = json.loads(
            (tmp_path / "runs" / run_id / "checkpoints" / "acquisition.v1.json").read_bytes()
        )
        assert [row["source_id"] for row in receipt["documents"]] == [good.records[0].source_id]
        report = json.loads((tmp_path / "runs" / run_id / "reports" / "issuer-collection.json").read_bytes())
        bad_outcome = next(row for row in report["issuers"] if row["issuer"] == "shinhan")
        assert bad_outcome["adopted_count"] == 0
        assert bad_outcome["origin_freshness_at"] is None
        assert report["terminal"] is True
        assert len([c for c in calls if c.startswith("/bad-")]) < len(bad.records)


async def test_discovery_concurrency_bound_and_external_cancel_drain(tmp_path: Path) -> None:
    active = 0
    maximum = 0
    at_capacity = asyncio.Event()
    release = asyncio.Event()
    clients: list[Any] = []

    class BlockingAdapter(IssuerAdapterFixture):
        async def discover_current(self, client: Any) -> Any:
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            clients.append(client)
            if active == 2:
                at_capacity.set()
            try:
                await release.wait()
                return await super().discover_current(client)
            finally:
                active -= 1

    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[BlockingAdapter(code) for code in ("woori", "shinhan", "kb", "lotte")],
            ocr=FakeOCR(),
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            issuer_discovery_concurrency=2,
        )
        task = asyncio.create_task(pipeline.run())
        await asyncio.wait_for(at_capacity.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert active == 0 and maximum == 2 and len(clients) == 2
        assert state.connection.execute("SELECT status FROM run").fetchone()[0] == "interrupted"


def test_partial_run_snapshot_never_becomes_successful_origin_baseline(tmp_path: Path) -> None:
    adapter = IssuerAdapterFixture("shinhan")
    from cardrag_worker.contracts import snapshot_from_records

    with WorkerState(tmp_path / "state.sqlite3") as state:
        for count, succeeded in ((1, True), (3, False)):
            run_id = state.start_run()
            records = tuple(replace(adapter.records[0], product_code=f"p{i}") for i in range(count))
            snapshot = snapshot_from_records(
                issuer="shinhan",
                source_url="https://cards.example/list",
                parser_version=adapter.parser_version,
                records=records,
                started_at=datetime.now(UTC),
            )
            state.record_snapshot(
                run_id=run_id,
                snapshot_id=snapshot.snapshot_id,
                issuer="shinhan",
                source_sha256=snapshot.snapshot_id,
                record_count=count,
                payload=snapshot.payload,
            )
            state.record_issuer_collection(run_id, "shinhan", succeeded=succeeded)
            state.finish_run(run_id, "succeeded")
        assert state.last_successful_snapshot_count("shinhan") == 1


def test_failed_issuer_retirement_candidate_is_frozen_even_when_carried_lineage_is_present() -> None:
    from test_retirement_v130 import _absent, _evaluate, _policy

    from cardrag_worker.retirement import evaluate_retirements

    item = _absent("100", issuer="shinhan")
    first = _evaluate((item,), run_id="first", started_at=datetime(2026, 10, 1, tzinfo=UTC), ledger=None)
    frozen = evaluate_retirements(
        run_id="next",
        run_started_at=datetime(2026, 10, 7, tzinfo=UTC),
        policy=_policy(),
        baseline_total=10000,
        ledger=first.ledger,
        absent=(item,),
        durable_ok={item.document_id: True},
        lineage_absent={item.document_id: True},
        discovered_lineages={item.lineage_key},
        frozen_issuers=frozenset({"shinhan"}),
    )
    assert frozen.ledger.entries == first.ledger.entries
    assert frozen.retired == () and frozen.reinstated == ()
