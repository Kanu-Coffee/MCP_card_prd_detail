from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
import traceback
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from cardrag_core import (
    ArtifactRef,
    OCRArtifactManifest,
    OCRReady,
    WebDAVError,
    WebDAVHTTPError,
    WebDAVIntegrityError,
    canonical_json_bytes,
    generation_manifest_path,
    object_path,
    verify_ocr_bytes,
)
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
from cardrag_worker.gc import GCPartialFailure
from cardrag_worker.ocr import (
    FailoverOCRResolver,
    OCRCachePublicationError,
    OCRResolver,
    OCRResult,
    OCRValidationError,
)
from cardrag_worker.pipeline import (
    OCRDocumentFailuresError,
    OCRFailureBookkeepingError,
    OCRSystemicFailureError,
    WorkerPipeline,
    WorkerUnexpectedFailureError,
    classify_ocr_failure,
    is_isolatable_document_ocr_failure,
)
from cardrag_worker.providers import ProviderDocumentError, ProviderError, ProviderSystemicError
from cardrag_worker.state import AlreadyRunning, WorkerState
from cardrag_worker.webdav import PublishedBundle, RemoteGenerationIdentity


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


class SelectiveOCR:
    contract = {"schema_version": "test-ocr.v1"}
    adoption_policy_version = "cardrag.legacy-ocr-adoption.v1"

    def __init__(
        self,
        failing_document_ids: set[str],
        raw_sentinel: str,
        *,
        cache_publication_deferred: bool = False,
    ) -> None:
        self.failing_document_ids = failing_document_ids
        self.raw_sentinel = raw_sentinel
        self.cache_publication_deferred = cache_publication_deferred
        self.calls: list[str] = []

    async def resolve(self, **kwargs: Any) -> OCRResult:
        document_id = str(kwargs["document_id"])
        self.calls.append(document_id)
        if document_id in self.failing_document_ids:
            raise ProviderDocumentError() from None
        result = successful_ocr_result()
        if self.cache_publication_deferred:
            return replace(
                result,
                cache_publication_deferred=True,
                cache_publication_reason_code="ocr_cache_publication_ready_network",
            )
        return result


class SingleFailureOCR:
    contract = {"schema_version": "test-ocr.v1"}
    adoption_policy_version = "cardrag.legacy-ocr-adoption.v1"

    def __init__(
        self,
        failing_document_id: str,
        failure_factory: Callable[[], Exception],
    ) -> None:
        self.failing_document_id = failing_document_id
        self.failure_factory = failure_factory
        self.calls: list[str] = []

    async def resolve(self, **kwargs: Any) -> OCRResult:
        document_id = str(kwargs["document_id"])
        self.calls.append(document_id)
        if document_id == self.failing_document_id:
            raise self.failure_factory()
        return successful_ocr_result()


class CancelledOCR:
    contract = {"schema_version": "test-ocr.v1"}
    adoption_policy_version = "cardrag.legacy-ocr-adoption.v1"

    def __init__(self) -> None:
        self.calls = 0

    async def resolve(self, **_kwargs: Any) -> OCRResult:
        self.calls += 1
        raise asyncio.CancelledError


class CachePublicationFailureOCR:
    contract = {"schema_version": "test-ocr.v1"}
    adoption_policy_version = "cardrag.legacy-ocr-adoption.v1"

    def __init__(self, raw_sentinel: str) -> None:
        self.raw_sentinel = raw_sentinel
        self.calls = 0

    async def resolve(self, **_kwargs: Any) -> OCRResult:
        self.calls += 1
        try:
            raise RuntimeError(self.raw_sentinel)
        except RuntimeError:
            # Exercise pipeline hardening even if a future caller forgets the
            # typed boundary's required ``from None``.
            error = OCRCachePublicationError(
                phase="ready",
                error_kind="http",
                status_code=403,
                retryable=False,
                attempts=3,
            )
            error.reason = self.raw_sentinel
            error.reason_code = self.raw_sentinel
            error.args = (self.raw_sentinel,)
            error.add_note(self.raw_sentinel)
            raise error  # noqa: B904


def successful_ocr_result() -> OCRResult:
    page = "정상적으로 처리된 OCR 문서의 충분히 긴 본문입니다."
    body = f"## Page 1\n\n{page}\n".encode()
    return OCRResult(
        pages=(page,),
        ocr_bytes=body,
        ocr_text=body.decode(),
        ocr_sha256=hashlib.sha256(body).hexdigest(),
        size_bytes=len(body),
        provenance="native",
        provider="test",
        model="test",
        reuse_key="a" * 64,
    )


class FakeWebDAV:
    def __init__(self, current: RemoteGenerationIdentity | None) -> None:
        self.current = current
        self.pointer_path = "v1/stable.json"
        self.channel = "stable"

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


def install_http(
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
    requests: list[str],
    *,
    response_headers: dict[str, str] | None = None,
) -> None:
    real_async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        headers = {"content-type": "application/pdf"}
        headers.update(response_headers or {})
        return httpx.Response(
            200,
            headers=headers,
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


def expire_pdf_cache_binding(state: WorkerState, source_id: str) -> str:
    expired = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    state.connection.execute(
        "UPDATE pdf_cache_source SET last_observed_at=? WHERE source_id=?",
        (expired, source_id),
    )
    state.connection.execute(
        """UPDATE pdf_cache_source_revision SET last_observed_at=?
           WHERE source_id=? AND superseded_at IS NULL""",
        (expired, source_id),
    )
    return expired


def http_status_error(status_code: int, *, raw_detail: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://private.example/RAW_PRIVATE_URL/token")
    return httpx.HTTPStatusError(
        raw_detail,
        request=request,
        response=httpx.Response(status_code, request=request),
    )


def webdav_error_with_cause(cause: Exception, *, raw_detail: str) -> WebDAVError:
    error = WebDAVError(raw_detail)
    error.__cause__ = cause
    return error


@pytest.mark.asyncio
async def test_ocr_failures_are_reported_then_later_documents_continue_without_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = pdf_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    records = tuple(
        source(
            product_code=f"p{index}",
            source_url=f"https://cards.example/current-{index}.pdf",
        )
        for index in range(1, 4)
    )
    failing_ids = {records[0].document_id(digest), records[2].document_id(digest)}
    raw_sentinel = "RAW_PROVIDER_STDERR_SECRET_SENTINEL"
    ocr = SelectiveOCR(failing_ids, raw_sentinel)
    embeddings = FakeEmbeddings()
    requests: list[str] = []
    install_http(monkeypatch, payload, requests)

    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[Adapter(records)],
            ocr=ocr,  # type: ignore[arg-type]
            embeddings=embeddings,
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
            maximum_attempts=2,
            retry_cap_seconds=0,
        )
        with pytest.raises(OCRDocumentFailuresError) as captured:
            await pipeline.run()

        error = captured.value
        assert [item.product_code for item in error.failures] == ["p1", "p3"]
        assert ocr.calls == [
            records[0].document_id(digest),
            records[0].document_id(digest),
            records[1].document_id(digest),
            records[2].document_id(digest),
            records[2].document_id(digest),
        ]
        assert embeddings.calls == 0
        assert state.connection.execute("SELECT count(*) FROM publish").fetchone()[0] == 0
        run_row = state.connection.execute("SELECT status,error FROM run").fetchone()
        assert tuple(run_row) == (
            "failed",
            f"2 OCR document(s) failed; report={error.report}",
        )
        for record, expected_status, expected_attempts in (
            (records[0], "failed", 2),
            (records[1], "succeeded", 1),
            (records[2], "failed", 2),
        ):
            stage = state.get_stage(error.run_id, record.document_id(digest), "ocr")
            assert stage is not None
            assert (stage.status, stage.attempt_count) == (expected_status, expected_attempts)
            if expected_status == "failed":
                assert stage.last_error == (
                    "provider_document_rejected: The OCR provider could not process this document."
                )
        successful_document_id = records[1].document_id(digest)
        for stage_name in ("structure", "chunk"):
            stage = state.get_stage(error.run_id, successful_document_id, stage_name)
            assert stage is not None
            assert (stage.status, stage.attempt_count) == ("succeeded", 1)
        successful_root = tmp_path / "runs" / error.run_id / "documents" / successful_document_id
        assert (successful_root / "ocr" / "ocr.md").is_file()
        assert (successful_root / "structure" / "pages.json").is_file()
        assert (successful_root / "chunks" / "chunks.json").is_file()

        report_body = error.report_path.read_bytes()
        report = json.loads(report_body)
        assert report_body == pipeline_module.canonical_json_bytes(report)
        assert report["schema_version"] == "cardrag.ocr-failure-report.v1"
        assert report["run_id"] == error.run_id
        assert report["failure_count"] == 2
        assert [item["product_code"] for item in report["failures"]] == ["p1", "p3"]
        assert all(item["attempts"] == 2 for item in report["failures"])
        assert all(item["reason_code"] == "provider_document_rejected" for item in report["failures"])
        persisted = json.dumps(report, ensure_ascii=False) + str(
            state.connection.execute("SELECT error FROM run").fetchone()[0]
        )
        persisted += "".join(
            str(row[0] or "") for row in state.connection.execute("SELECT last_error FROM stage").fetchall()
        )
        assert raw_sentinel not in persisted

    assert len(requests) == 3
    assert not (tmp_path / "runs" / error.run_id / "sealed").exists()
    assert raw_sentinel not in caplog.text
    assert caplog.text.count("; continuing") == 2


@pytest.mark.asyncio
async def test_one_of_twenty_ocr_failures_publishes_only_successful_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = pdf_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    records = tuple(
        source(
            product_code=f"p{index:02d}",
            source_url=f"https://cards.example/current-{index:02d}.pdf",
        )
        for index in range(20)
    )
    failed_document_id = records[0].document_id(digest)
    ocr = SelectiveOCR(
        {failed_document_id},
        "RAW_PROVIDER_SECRET",
        cache_publication_deferred=True,
    )
    embeddings = FakeEmbeddings()
    assert successful_ocr_result().cache_kind is None
    requests: list[str] = []
    install_http(monkeypatch, payload, requests)

    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[Adapter(records)],
            ocr=ocr,  # type: ignore[arg-type]
            embeddings=embeddings,
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
            maximum_attempts=1,
            retry_cap_seconds=0,
        )
        captured: dict[str, Any] = {}

        async def capture_publish(run_id: str, sealed: dict[str, Any]) -> Any:
            validated = await pipeline._validate_local_seal(sealed)
            captured["sealed"] = sealed
            captured["manifest"] = validated.manifest
            state.finish_run(
                run_id,
                "succeeded",
                corpus_sha256=validated.manifest.corpus_sha256,
                contract_sha256=validated.manifest.contract_sha256,
            )
            return pipeline_module.PipelineResult(
                run_id=run_id,
                status="succeeded",
                corpus_sha256=validated.manifest.corpus_sha256,
                contract_sha256=validated.manifest.contract_sha256,
                generation_id=validated.manifest.generation_id,
                document_count=validated.manifest.counts.documents,
                evidence_count=validated.manifest.counts.chunks,
            )

        pipeline._publish_sealed = capture_publish  # type: ignore[method-assign]
        result = await pipeline.run()

    manifest = captured["manifest"]
    assert result.status == "succeeded"
    assert result.document_count == 20
    assert result.evidence_count == 19
    assert embeddings.calls == 1
    assert result.ocr_cache_publication_deferred == 19
    assert captured["sealed"]["ocr_cache_publication_deferred"] == 19
    assert not (tmp_path / "runs" / result.run_id / "reports" / "ocr-systemic-failure.json").exists()
    assert manifest.schema_version == "cardrag.generation.v4"
    assert manifest.serving_schema == "cardrag.serving-db.v4"
    assert manifest.issuer_ocr_counts[0].model_dump() == {
        "issuer": "testbank",
        "acquired": 20,
        "succeeded": 19,
        "failed": 1,
    }
    failed_manifest = next(item for item in manifest.documents if item.document_id == failed_document_id)
    assert failed_manifest.availability == "ocr_failed"
    assert failed_manifest.ocr is None
    assert failed_manifest.ocr_failure is not None
    assert failed_manifest.ocr_failure.reason_code == "provider_document_rejected"
    assert failed_manifest.ocr_failure.attempts == 1

    database_path = Path(captured["sealed"]["database_path"])
    with sqlite3.connect(f"{database_path.as_uri()}?mode=ro&immutable=1", uri=True) as connection:
        failed = connection.execute(
            """SELECT document_id,pdf_sha256,page_count,reason_code,reason,attempts
                 FROM ocr_failed_products"""
        ).fetchone()
        assert failed == (
            failed_document_id,
            digest,
            1,
            "provider_document_rejected",
            "The OCR provider could not process this document.",
            1,
        )
        assert connection.execute("SELECT count(*) FROM documents").fetchone()[0] == 19
        assert connection.execute("SELECT count(*) FROM pages").fetchone()[0] == 19
        assert connection.execute("SELECT count(*) FROM evidence").fetchone()[0] == 19
        assert (
            connection.execute(
                "SELECT count(*) FROM evidence WHERE document_id=?", (failed_document_id,)
            ).fetchone()[0]
            == 0
        )
    assert len(requests) == 20


@pytest.mark.asyncio
async def test_missing_stable_repair_preserves_sealed_cache_publication_deferred_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = pdf_bytes()
    record = source()
    requests: list[str] = []
    install_http(monkeypatch, payload, requests)

    with WorkerState(tmp_path / "state.sqlite3") as state:
        webdav = FakeWebDAV(None)
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[Adapter((record,))],
            ocr=SelectiveOCR(set(), "unused", cache_publication_deferred=True),  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=webdav,  # type: ignore[arg-type]
            collect_remote_garbage=False,
        )
        published_seal: dict[str, Any] = {}

        async def publish_first(run_id: str, sealed: dict[str, Any]) -> Any:
            validated = await pipeline._validate_local_seal(sealed)
            manifest = validated.manifest
            published_seal.update(sealed)
            state.record_publish(
                generation_id=manifest.generation_id,
                run_id=run_id,
                corpus_sha256=manifest.corpus_sha256,
                contract_sha256=manifest.contract_sha256,
                serving_sha256=manifest.serving_database.sha256,
                status="ready",
                details={"manifest_sha256": manifest.manifest_sha256},
            )
            state.finish_run(
                run_id,
                "succeeded",
                corpus_sha256=manifest.corpus_sha256,
                contract_sha256=manifest.contract_sha256,
            )
            webdav.current = RemoteGenerationIdentity(
                generation_id=manifest.generation_id,
                corpus_sha256=manifest.corpus_sha256,
                contract_sha256=manifest.contract_sha256,
                generation_schema=manifest.schema_version,
                serving_schema=manifest.serving_schema,
            )
            return pipeline_module.PipelineResult(
                run_id=run_id,
                status="succeeded",
                corpus_sha256=manifest.corpus_sha256,
                contract_sha256=manifest.contract_sha256,
                generation_id=manifest.generation_id,
                document_count=manifest.counts.documents,
                evidence_count=manifest.counts.chunks,
                ocr_cache_publication_deferred=validated.ocr_cache_publication_deferred,
            )

        pipeline._publish_sealed = publish_first  # type: ignore[method-assign]
        first = await pipeline.run()
        assert first.ocr_cache_publication_deferred == 1

        webdav.current = None
        repaired_counts: list[int] = []

        async def repair_missing_pointer(
            sealed: Mapping[str, Any],
        ) -> tuple[PublishedBundle, Any]:
            validated = await pipeline._validate_local_seal(sealed)
            repaired_counts.append(validated.ocr_cache_publication_deferred)
            manifest = validated.manifest
            return (
                PublishedBundle(
                    manifest.generation_id,
                    manifest.serving_database.sha256,
                    manifest.manifest_sha256,
                ),
                validated,
            )

        pipeline._publish_remote_only = repair_missing_pointer  # type: ignore[method-assign]
        second = await pipeline.run()

        assert published_seal["ocr_cache_publication_deferred"] == 1
        assert repaired_counts == [1]
        assert second.status == "no_change"
        assert second.generation_id == first.generation_id
        assert second.ocr_cache_publication_deferred == 1


@pytest.mark.parametrize(
    "publication_phase",
    [
        "cas",
        "manifest",
        "ready",
        "remote-conflict",
        "generation-manifest-conflict",
        "resume-generation-manifest-conflict",
    ],
)
@pytest.mark.parametrize("resolver_branch", ["single", "primary", "fallback"])
@pytest.mark.asyncio
async def test_exact_generation_with_deferred_seal_repairs_native_cache_without_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    publication_phase: str,
    resolver_branch: str,
) -> None:
    failed_publication_phase = "cas" if publication_phase.endswith("conflict") else publication_phase
    payload = pdf_bytes()
    record = source()
    requests: list[str] = []
    install_http(monkeypatch, payload, requests)
    monkeypatch.setattr("cardrag_worker.ocr.OCR_CACHE_PUBLICATION_RETRY_DELAYS_SECONDS", (0.0, 0.0))

    def render_one_page(_pdf_path: Path, output_dir: Path, *, scale: float) -> tuple[Path, ...]:
        output_dir.mkdir(parents=True, exist_ok=True)
        page = output_dir / "page-0001.png"
        page.write_bytes(f"one-page-{scale}".encode())
        return (page,)

    monkeypatch.setattr("cardrag_worker.ocr.render_pdf", render_one_page)

    class Provider:
        provider = "codex-exec"
        model = "test-ocr-model"
        reasoning_effort = "high"

        def __init__(self, model: str = "test-ocr-model") -> None:
            self.calls = 0
            self.model = model

        async def recognize(
            self,
            images: tuple[Path, ...],
            *,
            page_numbers: tuple[int, ...],
            target_page_numbers: tuple[int, ...],
            total_pages: int,
            prompt: str,
        ) -> str:
            del images, page_numbers, target_page_numbers, total_pages, prompt
            self.calls += 1
            return "## Page 1\n\n카드 상품의 혜택 조건과 제외 사항을 충분히 설명하는 본문입니다.\n"

    class RejectingProvider(Provider):
        async def recognize(
            self,
            images: tuple[Path, ...],
            *,
            page_numbers: tuple[int, ...],
            target_page_numbers: tuple[int, ...],
            total_pages: int,
            prompt: str,
        ) -> str:
            del images, page_numbers, target_page_numbers, total_pages, prompt
            self.calls += 1
            raise ProviderDocumentError() from None

    class CacheHealingWebDAV(FakeWebDAV):
        def __init__(self) -> None:
            super().__init__(None)
            self.objects: dict[str, bytes] = {}
            self.failures_remaining = 3
            self.publish_attempts = {"cas": 0, "manifest": 0, "ready": 0}

        async def get_bytes(self, path: object, *, max_bytes: int | None = None) -> bytes | None:
            key = str(path)
            if key == str(self.pointer_path):
                return b"stable" if self.current is not None else None
            body = self.objects.get(key)
            if body is not None and max_bytes is not None and len(body) > max_bytes:
                raise RuntimeError("test object exceeded its read cap")
            return body

        async def put_cas(self, body: bytes, *, media_type: str) -> tuple[str, str]:
            del media_type
            self.publish_attempts["cas"] += 1
            if failed_publication_phase == "cas" and self.failures_remaining:
                self.failures_remaining -= 1
                raise WebDAVHTTPError("PUT", PurePosixPath("v1/objects"), 503)
            digest = hashlib.sha256(body).hexdigest()
            path = object_path(digest).as_posix()
            self.objects[path] = body
            return digest, path

        async def put_json(
            self,
            path: str | PurePosixPath,
            payload: Mapping[str, Any],
            *,
            immutable: bool,
        ) -> bytes:
            assert immutable
            key = str(path)
            phase = "ready" if key.endswith("/READY.json") else "manifest"
            self.publish_attempts[phase] += 1
            if failed_publication_phase == phase and self.failures_remaining:
                self.failures_remaining -= 1
                raise WebDAVHTTPError("PUT", PurePosixPath(key), 503)
            body = canonical_json_bytes(dict(payload))
            existing = self.objects.get(key)
            if existing is not None and existing != body:
                raise RuntimeError("immutable test object conflict")
            self.objects[key] = body
            return body

    webdav = CacheHealingWebDAV()
    embeddings = FakeEmbeddings()
    published_manifests: list[Any] = []

    with WorkerState(tmp_path / "state.sqlite3") as state:
        primary_provider: Provider
        fallback_provider: Provider | None = None
        if resolver_branch == "fallback":
            primary_provider = RejectingProvider("rejecting-primary-model")
        else:
            primary_provider = Provider("successful-primary-model")
        primary_resolver = OCRResolver(
            provider=primary_provider,  # type: ignore[arg-type]
            state=state,
            webdav=webdav,  # type: ignore[arg-type]
            chunk_pages=1,
        )
        if resolver_branch == "single":
            resolver: OCRResolver | FailoverOCRResolver = primary_resolver
        else:
            fallback_provider = Provider("successful-fallback-model")
            fallback_resolver = OCRResolver(
                provider=fallback_provider,  # type: ignore[arg-type]
                state=state,
                webdav=webdav,  # type: ignore[arg-type]
                chunk_pages=1,
            )
            resolver = FailoverOCRResolver(primary_resolver, fallback_resolver)
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[Adapter((record,))],
            ocr=resolver,
            embeddings=embeddings,
            webdav=webdav,  # type: ignore[arg-type]
            collect_remote_garbage=False,
        )

        async def publish_first(run_id: str, sealed: dict[str, Any]) -> Any:
            assert not published_manifests
            validated = await pipeline._validate_local_seal(sealed)
            manifest = validated.manifest
            published_manifests.append(manifest)
            webdav.objects[generation_manifest_path(manifest.generation_id).as_posix()] = (
                manifest.canonical_bytes()
            )
            state.record_publish(
                generation_id=manifest.generation_id,
                run_id=run_id,
                corpus_sha256=manifest.corpus_sha256,
                contract_sha256=manifest.contract_sha256,
                serving_sha256=manifest.serving_database.sha256,
                status="ready",
                details={"manifest_sha256": manifest.manifest_sha256},
            )
            state.finish_run(
                run_id,
                "succeeded",
                corpus_sha256=manifest.corpus_sha256,
                contract_sha256=manifest.contract_sha256,
            )
            webdav.current = RemoteGenerationIdentity(
                generation_id=manifest.generation_id,
                corpus_sha256=manifest.corpus_sha256,
                contract_sha256=manifest.contract_sha256,
                generation_schema=manifest.schema_version,
                serving_schema=manifest.serving_schema,
            )
            return pipeline_module.PipelineResult(
                run_id=run_id,
                status="succeeded",
                corpus_sha256=manifest.corpus_sha256,
                contract_sha256=manifest.contract_sha256,
                generation_id=manifest.generation_id,
                document_count=manifest.counts.documents,
                evidence_count=manifest.counts.chunks,
                ocr_cache_publication_deferred=validated.ocr_cache_publication_deferred,
            )

        pipeline._publish_sealed = publish_first  # type: ignore[method-assign]
        with pytest.warns(RuntimeWarning, match="publishing generation-only OCR"):
            first = await pipeline.run()
        provider_calls_after_first = (
            primary_provider.calls,
            fallback_provider.calls if fallback_provider is not None else None,
        )
        document_id = record.document_id(hashlib.sha256(payload).hexdigest())
        first_ocr_bytes = (
            tmp_path / "runs" / first.run_id / "documents" / document_id / "ocr" / "ocr.md"
        ).read_bytes()
        publish_row_before = tuple(
            state.connection.execute("SELECT generation_id,run_id,details_json FROM publish").fetchone()
        )

        if publication_phase in {
            "generation-manifest-conflict",
            "resume-generation-manifest-conflict",
        }:
            webdav.objects[generation_manifest_path(first.generation_id).as_posix()] = (
                b'{"different_remote_manifest":true}'
            )
            resume_run_id: str | None = None
            if publication_phase == "resume-generation-manifest-conflict":
                state.connection.execute(
                    "UPDATE run SET status='failed',error='test interrupted' WHERE run_id=?",
                    (first.run_id,),
                )
                state.connection.commit()
                resume_run_id = first.run_id
            with pytest.raises(WorkerUnexpectedFailureError):
                await pipeline.run(resume_run_id=resume_run_id)
            assert (
                primary_provider.calls,
                fallback_provider.calls if fallback_provider is not None else None,
            ) == provider_calls_after_first
            unchanged_seal = json.loads(
                (tmp_path / "runs" / first.run_id / "sealed" / "publish.json").read_bytes()
            )
            assert unchanged_seal["ocr_cache_publication_deferred"] == 1
            return

        if publication_phase == "remote-conflict":
            branch = (
                "fallback"
                if resolver_branch == "fallback"
                else ("primary" if resolver_branch == "primary" else None)
            )
            native_root = tmp_path / "runs" / first.run_id / "documents" / document_id / "ocr"
            if branch is not None:
                native_root /= branch
            prior_native = OCRArtifactManifest.model_validate_json(
                (native_root / "native-manifest.json").read_bytes()
            )
            conflicting_body = (
                "## Page 1\n\n동일 입력 키이지만 기존 세대와 다른 원격 OCR 본문입니다.\n"
            ).encode()
            conflicting_verified = verify_ocr_bytes(conflicting_body, expected_page_count=1)
            conflicting_manifest = OCRArtifactManifest(
                reuse_key=prior_native.reuse_key,
                source=prior_native.source,
                contract=prior_native.contract,
                output=ArtifactRef.for_cas(
                    sha256=conflicting_verified.sha256,
                    size_bytes=conflicting_verified.size_bytes,
                    media_type="text/markdown; charset=utf-8",
                ),
                ocr_chars=conflicting_verified.char_count,
                page_output_sha256=conflicting_verified.page_sha256,
                created_at=prior_native.created_at,
            )
            conflicting_manifest_body = conflicting_manifest.canonical_bytes()
            cache_root = f"v1/ocr-cache/native/{prior_native.reuse_key[:2]}/{prior_native.reuse_key}"
            webdav.objects[object_path(conflicting_verified.sha256).as_posix()] = conflicting_body
            webdav.objects[f"{cache_root}/manifest.json"] = conflicting_manifest_body
            webdav.objects[f"{cache_root}/READY.json"] = OCRReady(
                reuse_key=prior_native.reuse_key,
                manifest_sha256=hashlib.sha256(conflicting_manifest_body).hexdigest(),
                ocr_sha256=conflicting_verified.sha256,
            ).canonical_bytes()

            with pytest.raises(OCRSystemicFailureError) as mismatch:
                await pipeline.run()
            assert mismatch.value.failure.reason_code == "ocr_cache_healing_identity_mismatch"
            assert (
                primary_provider.calls,
                fallback_provider.calls if fallback_provider is not None else None,
            ) == provider_calls_after_first
            unchanged_seal = json.loads(
                (tmp_path / "runs" / first.run_id / "sealed" / "publish.json").read_bytes()
            )
            assert unchanged_seal["ocr_cache_publication_deferred"] == 1
            return

        second = await pipeline.run()
        assert (
            primary_provider.calls,
            fallback_provider.calls if fallback_provider is not None else None,
        ) == provider_calls_after_first
        publish_row_after = tuple(
            state.connection.execute("SELECT generation_id,run_id,details_json FROM publish").fetchone()
        )
        healed_seal = json.loads((tmp_path / "runs" / first.run_id / "sealed" / "publish.json").read_bytes())

        third = await pipeline.run()
        publish_row_after_third = tuple(
            state.connection.execute("SELECT generation_id,run_id,details_json FROM publish").fetchone()
        )

        second_run = state.connection.execute(
            "SELECT status FROM run WHERE run_id=?", (second.run_id,)
        ).fetchone()
        assert second_run is not None
        assert second_run["status"] == "no_change"
        assert (
            state.get_stage(
                third.run_id,
                document_id,
                "ocr",
            )
            is None
        )

    assert first.status == "succeeded"
    assert first.ocr_cache_publication_deferred == 1
    assert second.status == "no_change"
    assert second.generation_id == first.generation_id
    assert second.ocr_cache_publication_deferred == 0
    assert healed_seal["ocr_cache_publication_deferred"] == 0
    assert third.status == "no_change"
    assert third.generation_id == first.generation_id
    assert third.ocr_cache_publication_deferred == 0
    assert (
        provider_calls_after_first
        == {
            "single": (1, None),
            "primary": (1, 0),
            "fallback": (1, 1),
        }[resolver_branch]
    )
    assert embeddings.calls == 1
    expected_attempts = {
        "cas": {"cas": 4, "manifest": 1, "ready": 1},
        "manifest": {"cas": 4, "manifest": 4, "ready": 1},
        "ready": {"cas": 3, "manifest": 3, "ready": 4},
    }[publication_phase]
    assert webdav.publish_attempts == expected_attempts
    assert any(path.endswith("/READY.json") for path in webdav.objects)
    assert webdav.objects[object_path(hashlib.sha256(first_ocr_bytes).hexdigest()).as_posix()] == (
        first_ocr_bytes
    )
    assert len(published_manifests) == 1
    assert published_manifests[0].documents[0].ocr_cache_kind is None
    assert publish_row_after == publish_row_before
    assert publish_row_after_third == publish_row_before


@pytest.mark.asyncio
async def test_partial_generation_heals_to_full_v4_when_failed_ocr_succeeds_next_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = pdf_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    records = tuple(
        source(
            product_code=f"p{index:02d}",
            source_url=f"https://cards.example/current-{index:02d}.pdf",
        )
        for index in range(20)
    )
    failed_document_id = records[0].document_id(digest)
    ocr = SelectiveOCR({failed_document_id}, "RAW_PROVIDER_SECRET")
    embeddings = FakeEmbeddings()
    requests: list[str] = []
    install_http(monkeypatch, payload, requests)

    with WorkerState(tmp_path / "state.sqlite3") as state:
        webdav = FakeWebDAV(None)
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[Adapter(records)],
            ocr=ocr,  # type: ignore[arg-type]
            embeddings=embeddings,
            webdav=webdav,  # type: ignore[arg-type]
            collect_remote_garbage=False,
            maximum_attempts=1,
            retry_cap_seconds=0,
        )
        published: list[tuple[Any, Path]] = []

        async def publish_and_advance(run_id: str, sealed: dict[str, Any]) -> Any:
            validated = await pipeline._validate_local_seal(sealed)
            manifest = validated.manifest
            state.record_publish(
                generation_id=manifest.generation_id,
                run_id=run_id,
                corpus_sha256=manifest.corpus_sha256,
                contract_sha256=manifest.contract_sha256,
                serving_sha256=manifest.serving_database.sha256,
                status="ready",
                details={"manifest_sha256": manifest.manifest_sha256},
            )
            state.finish_run(
                run_id,
                "succeeded",
                corpus_sha256=manifest.corpus_sha256,
                contract_sha256=manifest.contract_sha256,
            )
            webdav.current = RemoteGenerationIdentity(
                generation_id=manifest.generation_id,
                corpus_sha256=manifest.corpus_sha256,
                contract_sha256=manifest.contract_sha256,
                generation_schema=manifest.schema_version,
                serving_schema=manifest.serving_schema,
                ocr_failed_document_count=sum(
                    document.availability == "ocr_failed" for document in manifest.documents
                ),
            )
            published.append((manifest, Path(sealed["database_path"])))
            return pipeline_module.PipelineResult(
                run_id=run_id,
                status="succeeded",
                corpus_sha256=manifest.corpus_sha256,
                contract_sha256=manifest.contract_sha256,
                generation_id=manifest.generation_id,
                document_count=manifest.counts.documents,
                evidence_count=manifest.counts.chunks,
            )

        pipeline._publish_sealed = publish_and_advance  # type: ignore[method-assign]

        first = await pipeline.run()
        first_manifest, first_database = published[0]
        assert first.status == "succeeded"
        assert first_manifest.schema_version == "cardrag.generation.v4"
        assert webdav.current is not None
        assert webdav.current.ocr_failed_document_count == 1
        with sqlite3.connect(
            f"{first_database.as_uri()}?mode=ro&immutable=1",
            uri=True,
        ) as connection:
            assert connection.execute("SELECT document_id FROM ocr_failed_products").fetchall() == [
                (failed_document_id,)
            ]

        ocr.failing_document_ids.clear()
        second = await pipeline.run()
        second_manifest, second_database = published[1]

        assert second.status == "succeeded"
        assert second.generation_id != first.generation_id
        assert second_manifest.previous_generation_id == first.generation_id
        assert all(document.availability == "available" for document in second_manifest.documents)
        assert all(row.failed == 0 for row in second_manifest.issuer_ocr_counts)
        assert webdav.current is not None
        assert webdav.current.generation_id == second.generation_id
        assert webdav.current.ocr_failed_document_count == 0
        with sqlite3.connect(
            f"{second_database.as_uri()}?mode=ro&immutable=1",
            uri=True,
        ) as connection:
            assert connection.execute("SELECT count(*) FROM ocr_failed_products").fetchone()[0] == 0
            assert connection.execute("SELECT count(*) FROM documents").fetchone()[0] == 20
            assert connection.execute("SELECT count(*) FROM evidence").fetchone()[0] == 20

    assert len(requests) == 20
    # Identical OCR text reuses the content-addressed embedding cached by the
    # first run; the healed document still appears as the twentieth evidence row.
    assert embeddings.calls == 1
    assert ocr.calls.count(failed_document_id) == 2


@pytest.mark.asyncio
async def test_one_issuer_below_ocr_threshold_blocks_mixed_issuer_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = pdf_bytes()
    digest = hashlib.sha256(payload).hexdigest()

    def issuer_records(issuer: str) -> tuple[SourceRecord, ...]:
        return tuple(
            replace(
                source(
                    product_code=f"p{index:02d}",
                    source_url=f"https://cards.example/{issuer}-{index:02d}.pdf",
                ),
                issuer=issuer,
            )
            for index in range(20)
        )

    strong_records = issuer_records("strongbank")
    weak_records = issuer_records("weakbank")
    strong = Adapter(strong_records)
    strong.spec = replace(strong.spec, code="strongbank", display_name="강한카드")
    weak = Adapter(weak_records)
    weak.spec = replace(weak.spec, code="weakbank", display_name="약한카드", sort_order=2)
    failed_document_ids = {
        weak_records[0].document_id(digest),
        weak_records[1].document_id(digest),
    }
    ocr = SelectiveOCR(failed_document_ids, "RAW_PROVIDER_SECRET")
    embeddings = FakeEmbeddings()
    requests: list[str] = []
    install_http(monkeypatch, payload, requests)

    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[strong, weak],
            ocr=ocr,  # type: ignore[arg-type]
            embeddings=embeddings,
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
            maximum_attempts=1,
            retry_cap_seconds=0,
        )

        with pytest.raises(OCRDocumentFailuresError) as captured:
            await pipeline.run()

        assert {(failure.issuer, failure.document_id) for failure in captured.value.failures} == {
            ("weakbank", document_id) for document_id in failed_document_ids
        }
        assert embeddings.calls == 0
        assert state.connection.execute("SELECT count(*) FROM publish").fetchone()[0] == 0
        assert (
            state.connection.execute(
                "SELECT status FROM run WHERE run_id=?",
                (captured.value.run_id,),
            ).fetchone()[0]
            == "failed"
        )
        assert not (tmp_path / "runs" / captured.value.run_id / "sealed").exists()

    assert len(requests) == 40
    assert len(ocr.calls) == 40


@pytest.mark.parametrize(
    ("failure_factory", "expected_reason_code", "raw_markers"),
    [
        pytest.param(
            lambda: OCRValidationError("RAW_VALIDATION_DETAIL"),
            "generic_validation_error",
            ("RAW_VALIDATION_DETAIL",),
            id="validation",
        ),
        pytest.param(
            lambda: TimeoutError("RAW_TIMEOUT_DETAIL"),
            "provider_timeout",
            ("RAW_TIMEOUT_DETAIL",),
            id="timeout",
        ),
        pytest.param(
            lambda: http_status_error(503, raw_detail="RAW_HTTP_DETAIL"),
            "provider_http_503",
            ("RAW_HTTP_DETAIL", "RAW_PRIVATE_URL", "private.example"),
            id="transient-http",
        ),
    ],
)
@pytest.mark.asyncio
async def test_document_scoped_ocr_errors_exhaust_then_later_document_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure_factory: Callable[[], Exception],
    expected_reason_code: str,
    raw_markers: tuple[str, ...],
) -> None:
    payload = pdf_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    records = (
        source(product_code="p1", source_url="https://cards.example/current-1.pdf"),
        source(product_code="p2", source_url="https://cards.example/current-2.pdf"),
    )
    failing_document_id = records[0].document_id(digest)
    successful_document_id = records[1].document_id(digest)
    ocr = SingleFailureOCR(failing_document_id, failure_factory)
    requests: list[str] = []
    install_http(monkeypatch, payload, requests)

    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[Adapter(records)],
            ocr=ocr,  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
            maximum_attempts=2,
            retry_cap_seconds=0,
        )
        with pytest.raises(OCRDocumentFailuresError) as captured:
            await pipeline.run()

        error = captured.value
        assert ocr.calls == [failing_document_id, failing_document_id, successful_document_id]
        assert len(error.failures) == 1
        assert error.failures[0].reason_code == expected_reason_code
        failed_stage = state.get_stage(error.run_id, failing_document_id, "ocr")
        assert failed_stage is not None
        assert (failed_stage.status, failed_stage.attempt_count) == ("failed", 2)
        successful_stage = state.get_stage(error.run_id, successful_document_id, "ocr")
        assert successful_stage is not None
        assert (successful_stage.status, successful_stage.attempt_count) == ("succeeded", 1)
        for stage_name in ("structure", "chunk"):
            stage = state.get_stage(error.run_id, successful_document_id, stage_name)
            assert stage is not None and stage.status == "succeeded"
        persisted = error.report_path.read_text(encoding="utf-8")
        persisted += str(state.connection.execute("SELECT error FROM run").fetchone()[0])
        persisted += "".join(
            str(row[0] or "") for row in state.connection.execute("SELECT last_error FROM stage").fetchall()
        )
        for marker in raw_markers:
            assert marker not in persisted

    assert len(requests) == 2
    for marker in raw_markers:
        assert marker not in caplog.text


@pytest.mark.parametrize(
    (
        "failure_factory",
        "raw_markers",
        "reason_code",
        "error_class_category",
        "status_code",
        "error_kind",
    ),
    [
        pytest.param(
            lambda: RuntimeError("RAW_RUNTIME_DETAIL"),
            ("RAW_RUNTIME_DETAIL",),
            "ocr_unexpected_error",
            "unexpected",
            None,
            None,
            id="runtime",
        ),
        pytest.param(
            lambda: http_status_error(401, raw_detail="RAW_HTTP_AUTH_DETAIL"),
            ("RAW_HTTP_AUTH_DETAIL", "RAW_PRIVATE_URL", "private.example"),
            "ocr_http_401",
            "http_status",
            401,
            None,
            id="http-401",
        ),
        pytest.param(
            lambda: httpx.ConnectError(
                "RAW_CONNECT_DETAIL",
                request=httpx.Request("POST", "https://private.example/RAW_PRIVATE_URL/token"),
            ),
            ("RAW_CONNECT_DETAIL", "RAW_PRIVATE_URL", "private.example"),
            "ocr_network_error",
            "network",
            None,
            None,
            id="connect-error",
        ),
        pytest.param(
            lambda: WebDAVHTTPError(
                "GET",
                PurePosixPath("ocr/RAW_PRIVATE_CACHE_PATH/ready.json"),
                401,
            ),
            ("RAW_PRIVATE_CACHE_PATH",),
            "ocr_cache_http_401",
            "ocr_cache_webdav",
            401,
            "http",
            id="webdav-http-401",
        ),
        pytest.param(
            lambda: WebDAVIntegrityError("RAW_PRIVATE_CACHE_PATH hash mismatch"),
            ("RAW_PRIVATE_CACHE_PATH",),
            "ocr_cache_integrity_error",
            "ocr_cache_webdav",
            None,
            "integrity",
            id="webdav-integrity",
        ),
        pytest.param(
            lambda: webdav_error_with_cause(
                httpx.ReadTimeout(
                    "RAW_TIMEOUT_DETAIL",
                    request=httpx.Request("GET", "https://private.example/RAW_PRIVATE_CACHE_PATH"),
                ),
                raw_detail="RAW_WEBDAV_DETAIL",
            ),
            (
                "RAW_TIMEOUT_DETAIL",
                "RAW_WEBDAV_DETAIL",
                "RAW_PRIVATE_CACHE_PATH",
                "private.example",
            ),
            "ocr_cache_timeout",
            "ocr_cache_webdav",
            None,
            "timeout",
            id="webdav-timeout-cause",
        ),
    ],
)
@pytest.mark.asyncio
async def test_systemic_ocr_error_writes_safe_report_and_stops_before_later_ocr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure_factory: Callable[[], Exception],
    raw_markers: tuple[str, ...],
    reason_code: str,
    error_class_category: str,
    status_code: int | None,
    error_kind: str | None,
) -> None:
    payload = pdf_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    records = (
        source(product_code="p1", source_url="https://cards.example/current-1.pdf"),
        source(product_code="p2", source_url="https://cards.example/current-2.pdf"),
    )
    failing_document_id = records[0].document_id(digest)
    later_document_id = records[1].document_id(digest)
    ocr = SingleFailureOCR(failing_document_id, failure_factory)
    requests: list[str] = []
    install_http(monkeypatch, payload, requests)

    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[Adapter(records)],
            ocr=ocr,  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
            maximum_attempts=4,
            retry_cap_seconds=0,
        )
        with pytest.raises(OCRSystemicFailureError) as captured:
            await pipeline.run()

        assert ocr.calls == [failing_document_id]
        run_row = state.connection.execute("SELECT run_id,status,error FROM run").fetchone()
        assert run_row is not None
        run_id = str(run_row["run_id"])
        report_path = tmp_path / "runs" / run_id / "reports" / "ocr-systemic-failure.json"
        report_body = report_path.read_bytes()
        report = json.loads(report_body)
        assert report_body == pipeline_module.canonical_json_bytes(report)
        assert report == {
            "attempt": 1,
            "document_id": failing_document_id,
            "error_class_category": error_class_category,
            "issuer": "testbank",
            "occurred_at": report["occurred_at"],
            "pdf_sha256": digest,
            "product_code": "p1",
            "reason": captured.value.failure.reason,
            "reason_code": reason_code,
            "run_id": run_id,
            "schema_version": "cardrag.ocr-systemic-failure-report.v1",
            "source_id": records[0].source_id,
            **({"status_code": status_code} if status_code is not None else {}),
            **({"error_kind": error_kind} if error_kind is not None else {}),
        }
        occurred_at = datetime.fromisoformat(report["occurred_at"])
        assert occurred_at.tzinfo is not None
        assert occurred_at.utcoffset() == timedelta(0)
        assert captured.value.report_path == report_path
        assert captured.value.__cause__ is None
        assert (run_row["status"], run_row["error"]) == ("failed", captured.value.stored_error)
        failed_stage = state.get_stage(run_id, failing_document_id, "ocr")
        assert failed_stage is not None
        assert (failed_stage.status, failed_stage.attempt_count, failed_stage.last_error) == (
            "failed",
            1,
            captured.value.stored_error,
        )
        assert reason_code in captured.value.stored_error
        assert f"report=runs/{run_id}/reports/ocr-systemic-failure.json" in captured.value.stored_error
        assert state.get_stage(run_id, later_document_id, "ocr") is None
        assert not (tmp_path / "runs" / run_id / "reports" / "ocr-failures.json").exists()
        rendered = "".join(
            traceback.format_exception(
                type(captured.value),
                captured.value,
                captured.value.__traceback__,
            )
        )
        persisted = (
            rendered
            + report_body.decode()
            + str(run_row["error"])
            + str(failed_stage.last_error)
            + caplog.text
        )
        assert "source_url" not in report
        for marker in raw_markers:
            assert marker not in persisted

    assert len(requests) == 2


@pytest.mark.asyncio
async def test_typed_cache_publication_failure_preserves_only_safe_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = pdf_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    record = source()
    raw_sentinel = "RAW_CACHE_PUBLICATION_CAUSE_SECRET"
    ocr = CachePublicationFailureOCR(raw_sentinel)
    requests: list[str] = []
    install_http(monkeypatch, payload, requests)

    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[Adapter((record,))],
            ocr=ocr,  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
            maximum_attempts=4,
            retry_cap_seconds=0,
        )
        with pytest.raises(OCRSystemicFailureError) as captured:
            await pipeline.run()

        error = captured.value
        assert error.__cause__ is None
        assert error.__context__ is None
        report = json.loads(error.report_path.read_bytes())
        assert report == {
            "attempt": 1,
            "document_id": record.document_id(digest),
            "error_class_category": "ocr_cache_publication",
            "error_kind": "http",
            "issuer": "testbank",
            "occurred_at": report["occurred_at"],
            "pdf_sha256": digest,
            "phase": "ready",
            "product_code": "p1",
            "publication_attempts": 3,
            "reason": "OCR cache publication received an HTTP failure status",
            "reason_code": "ocr_cache_publication_ready_http",
            "retryable": False,
            "run_id": error.run_id,
            "schema_version": "cardrag.ocr-systemic-failure-report.v1",
            "source_id": record.source_id,
            "status_code": 403,
        }
        stage = state.get_stage(error.run_id, record.document_id(digest), "ocr")
        assert stage is not None
        run_error = state.connection.execute(
            "SELECT error FROM run WHERE run_id=?", (error.run_id,)
        ).fetchone()[0]
        rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        persisted = rendered + error.report_path.read_text() + str(stage.last_error) + str(run_error)
        assert raw_sentinel not in persisted
        assert raw_sentinel not in caplog.text

    assert ocr.calls == 1
    assert requests == [record.source_url]


@pytest.mark.asyncio
async def test_provider_process_exit_is_systemic_safe_and_stops_later_ocr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = pdf_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    records = (
        source(product_code="p1", source_url="https://cards.example/current-1.pdf"),
        source(product_code="p2", source_url="https://cards.example/current-2.pdf"),
    )
    raw_sentinel = "RAW_CODEX_STDERR_URL_TOKEN_SECRET"

    def provider_failure() -> ProviderSystemicError:
        error = ProviderSystemicError("provider_process_exit", exit_code=17)
        error.__cause__ = ProviderError(raw_sentinel)
        error.reason = raw_sentinel
        error.error_kind = raw_sentinel  # type: ignore[assignment]
        error.retryable = raw_sentinel  # type: ignore[assignment]
        error.args = (raw_sentinel,)
        error.add_note(raw_sentinel)
        return error

    ocr = SingleFailureOCR(records[0].document_id(digest), provider_failure)
    requests: list[str] = []
    install_http(monkeypatch, payload, requests)

    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[Adapter(records)],
            ocr=ocr,  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
            maximum_attempts=4,
            retry_cap_seconds=0,
        )
        with pytest.raises(OCRSystemicFailureError) as captured:
            await pipeline.run()

        error = captured.value
        assert error.__cause__ is None
        assert error.__context__ is None
        report = json.loads(error.report_path.read_bytes())
        assert report["reason_code"] == "provider_process_exit"
        assert report["reason"] == "The OCR provider process exited unsuccessfully."
        assert report["error_class_category"] == "ocr_provider_systemic"
        assert report["error_kind"] == "process_exit"
        assert report["retryable"] is False
        assert report["exit_code"] == 17
        assert report["attempt"] == 1
        assert ocr.calls == [records[0].document_id(digest)]
        stage = state.get_stage(error.run_id, records[0].document_id(digest), "ocr")
        assert stage is not None
        run_error = state.connection.execute(
            "SELECT error FROM run WHERE run_id=?", (error.run_id,)
        ).fetchone()[0]
        rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        persisted = rendered + error.report_path.read_text() + str(stage.last_error) + str(run_error)
        assert raw_sentinel not in persisted

    assert requests == [record.source_url for record in records]


@pytest.mark.asyncio
async def test_transient_provider_process_failure_retries_within_stage_budget_then_stops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = pdf_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    records = (
        source(product_code="p1", source_url="https://cards.example/current-1.pdf"),
        source(product_code="p2", source_url="https://cards.example/current-2.pdf"),
    )
    raw_sentinel = "RAW_TRANSIENT_CODEX_STDERR_SECRET"
    stderr = f"stream disconnected; {raw_sentinel}".encode()
    stderr_sha256 = hashlib.sha256(stderr).hexdigest()

    def provider_failure() -> ProviderSystemicError:
        error = ProviderSystemicError(
            "provider_process_network_error",
            exit_code=1,
            stderr_size_bytes=len(stderr),
            stderr_sha256=stderr_sha256,
        )
        error.__cause__ = ProviderError(raw_sentinel)
        error.add_note(raw_sentinel)
        return error

    failing_document_id = records[0].document_id(digest)
    ocr = SingleFailureOCR(failing_document_id, provider_failure)
    requests: list[str] = []
    install_http(monkeypatch, payload, requests)

    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[Adapter(records)],
            ocr=ocr,  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
            maximum_attempts=3,
            retry_cap_seconds=0,
        )
        with pytest.raises(OCRSystemicFailureError) as captured:
            await pipeline.run()

        error = captured.value
        report = json.loads(error.report_path.read_bytes())
        assert report["reason_code"] == "provider_process_network_error"
        assert report["error_kind"] == "network"
        assert report["retryable"] is True
        assert report["attempt"] == 3
        assert report["exit_code"] == 1
        assert report["stderr_size_bytes"] == len(stderr)
        assert report["stderr_sha256"] == stderr_sha256
        assert ocr.calls == [failing_document_id] * 3
        stage = state.get_stage(error.run_id, failing_document_id, "ocr")
        assert stage is not None
        assert (stage.status, stage.attempt_count) == ("failed", 3)
        assert state.get_stage(error.run_id, records[1].document_id(digest), "ocr") is None
        run_error = state.connection.execute(
            "SELECT error FROM run WHERE run_id=?", (error.run_id,)
        ).fetchone()[0]
        persisted = "".join(
            (
                error.report_path.read_text(),
                str(stage.last_error),
                str(run_error),
                "".join(traceback.format_exception(type(error), error, error.__traceback__)),
            )
        )
        assert raw_sentinel not in persisted

    assert requests == [record.source_url for record in records]


@pytest.mark.asyncio
async def test_generic_stage_and_run_failures_persist_only_safe_diagnostics(
    tmp_path: Path,
) -> None:
    raw_sentinel = "RAW_GENERIC_STAGE_URL_TOKEN_SECRET"
    document_id = "doc_" + "a" * 64

    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[Adapter((source(),))],
            ocr=FakeOCR(),  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
            maximum_attempts=1,
        )

        async def fail_locked(run_id: str, *, refresh_sources: bool = False) -> Any:
            assert refresh_sources is False

            async def fail_stage() -> None:
                raise RuntimeError(raw_sentinel)

            return await pipeline._finite_stage(  # noqa: SLF001
                run_id=run_id,
                document_id=document_id,
                name="embedding",
                operation=fail_stage,
                maximum_attempts=1,
            )

        pipeline._run_locked = fail_locked  # type: ignore[method-assign]
        with pytest.raises(WorkerUnexpectedFailureError) as captured:
            await pipeline.run()

        error = captured.value
        report_body = error.report_path.read_bytes()
        report = json.loads(report_body)
        assert report == {
            "error_class_category": "runtime",
            "occurred_at": report["occurred_at"],
            "reason": "Worker pipeline failed unexpectedly.",
            "reason_code": "worker_unexpected_failure",
            "run_id": error.run_id,
            "schema_version": "cardrag.worker-failure-report.v1",
        }
        stage = state.get_stage(error.run_id, document_id, "embedding")
        assert stage is not None
        assert stage.last_error == ("worker_stage_failure: Worker pipeline stage failed (category=runtime).")
        run_row = state.connection.execute(
            "SELECT status,error FROM run WHERE run_id=?", (error.run_id,)
        ).fetchone()
        assert run_row is not None
        assert (run_row["status"], run_row["error"]) == ("failed", error.stored_error)
        rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        persisted = rendered + report_body.decode() + str(stage.last_error) + str(run_row["error"])
        assert error.__cause__ is None
        assert error.__context__ is None
        assert raw_sentinel not in persisted


@pytest.mark.asyncio
async def test_pipeline_cancellation_durably_interrupts_run_and_is_reraised(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = pdf_bytes()
    record = source()
    ocr = CancelledOCR()
    requests: list[str] = []
    install_http(monkeypatch, payload, requests)

    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[Adapter((record,))],
            ocr=ocr,  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
            maximum_attempts=2,
            retry_cap_seconds=0,
        )
        with pytest.raises(asyncio.CancelledError):
            await pipeline.run()

        run_row = state.connection.execute("SELECT run_id,status,error FROM run").fetchone()
        assert run_row is not None
        assert (run_row["status"], run_row["error"]) == (
            "interrupted",
            "worker_cancelled: Pipeline execution was interrupted.",
        )
        assert not (
            tmp_path / "runs" / str(run_row["run_id"]) / "reports" / "ocr-systemic-failure.json"
        ).exists()

    assert ocr.calls == 1
    assert requests == [record.source_url]


@pytest.mark.asyncio
async def test_pipeline_normalizes_raw_cancelled_error_chain_after_interrupt_bookkeeping(
    tmp_path: Path,
) -> None:
    raw_arg = "RAW_PIPELINE_CANCEL_ARG_SECRET"
    raw_cause = "RAW_PIPELINE_CANCEL_CAUSE_SECRET"
    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[Adapter((source(),))],
            ocr=FakeOCR(),  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
        )

        async def cancel_with_raw_chain(run_id: str, *, refresh_sources: bool = False) -> Any:
            assert run_id
            assert refresh_sources is False
            raise asyncio.CancelledError(raw_arg) from RuntimeError(raw_cause)

        pipeline._run_locked = cancel_with_raw_chain  # type: ignore[method-assign]
        with pytest.raises(asyncio.CancelledError) as captured:
            await pipeline.run()

        row = state.connection.execute("SELECT status,error FROM run").fetchone()
        assert row is not None
        assert tuple(row) == (
            "interrupted",
            "worker_cancelled: Pipeline execution was interrupted.",
        )

    rendered = "".join(
        traceback.format_exception(
            type(captured.value),
            captured.value,
            captured.value.__traceback__,
        )
    )
    assert captured.value.args == ()
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert raw_arg not in rendered
    assert raw_cause not in rendered


@pytest.mark.asyncio
async def test_cancellation_after_terminal_publication_preserves_success_truth(tmp_path: Path) -> None:
    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[Adapter((source(),))],
            ocr=FakeOCR(),  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
        )
        captured_run_id = ""

        async def finish_then_cancel(run_id: str, *, refresh_sources: bool = False) -> Any:
            nonlocal captured_run_id
            captured_run_id = run_id
            assert refresh_sources is False
            state.record_publish(
                generation_id="g-terminal-before-cancel",
                run_id=run_id,
                corpus_sha256="a" * 64,
                contract_sha256="b" * 64,
                serving_sha256="c" * 64,
                status="ready",
                details={},
            )
            state.finish_run(
                run_id,
                "succeeded",
                corpus_sha256="a" * 64,
                contract_sha256="b" * 64,
            )
            raise asyncio.CancelledError

        pipeline._run_locked = finish_then_cancel  # type: ignore[method-assign]
        with pytest.raises(asyncio.CancelledError):
            await pipeline.run()

        run_row = state.connection.execute(
            "SELECT status,error,corpus_sha256,contract_sha256 FROM run WHERE run_id=?",
            (captured_run_id,),
        ).fetchone()
        assert tuple(run_row) == ("succeeded", None, "a" * 64, "b" * 64)


@pytest.mark.asyncio
async def test_local_run_cleanup_failure_is_safe_and_does_not_flip_success(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw_sentinel = "RAW_LOCAL_CLEANUP_PATH_SECRET"
    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[Adapter((source(),))],
            ocr=FakeOCR(),  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
        )

        def fail_cleanup(*, exclude_run_id: str) -> None:
            assert exclude_run_id
            raise OSError(raw_sentinel)

        async def publish_success(run_id: str, *, refresh_sources: bool = False) -> Any:
            assert refresh_sources is False
            state.finish_run(
                run_id,
                "succeeded",
                corpus_sha256="a" * 64,
                contract_sha256="b" * 64,
            )
            return pipeline_module.PipelineResult(
                run_id=run_id,
                status="succeeded",
                corpus_sha256="a" * 64,
                contract_sha256="b" * 64,
                generation_id="g-cleanup-safe",
                document_count=1,
                evidence_count=1,
            )

        pipeline._cleanup_local_runs = fail_cleanup  # type: ignore[method-assign]
        pipeline._run_locked = publish_success  # type: ignore[method-assign]
        result = await pipeline.run()

        status = state.connection.execute(
            "SELECT status FROM run WHERE run_id=?", (result.run_id,)
        ).fetchone()[0]
        assert (result.status, status) == ("succeeded", "succeeded")
        assert caplog.text.count("reason_code=local_run_cleanup_failed") == 2
        assert raw_sentinel not in caplog.text


@pytest.mark.asyncio
async def test_post_publication_gc_failure_returns_fixed_safe_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw_sentinel = "RAW_GC_REMOTE_SECRET"

    async def fail_gc(**_kwargs: Any) -> Any:
        raise RuntimeError(raw_sentinel)

    monkeypatch.setattr("cardrag_worker.gc.collect_garbage", fail_gc)
    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[Adapter((source(),))],
            ocr=FakeOCR(),  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=True,
            stable_publication_approved=True,
            remote_gc_approved=True,
        )

        async def publish_success(run_id: str, *, refresh_sources: bool = False) -> Any:
            assert refresh_sources is False
            state.finish_run(
                run_id,
                "succeeded",
                corpus_sha256="a" * 64,
                contract_sha256="b" * 64,
            )
            return pipeline_module.PipelineResult(
                run_id=run_id,
                status="succeeded",
                corpus_sha256="a" * 64,
                contract_sha256="b" * 64,
                generation_id="g-gc-safe",
                document_count=1,
                evidence_count=1,
            )

        pipeline._run_locked = publish_success  # type: ignore[method-assign]
        result = await pipeline.run()

    assert result.status == "succeeded"
    assert result.gc_status == "failed"
    assert result.gc_error == pipeline_module.REMOTE_GC_ERROR
    assert raw_sentinel not in result.gc_error
    assert raw_sentinel not in caplog.text


def test_pipeline_remote_gc_requires_both_stable_approvals(tmp_path: Path) -> None:
    with (
        WorkerState(tmp_path / "state.sqlite3") as state,
        pytest.raises(ValueError, match="publication and remote-GC approvals"),
    ):
        WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[Adapter((source(),))],
            ocr=FakeOCR(),  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=True,
            stable_publication_approved=True,
            remote_gc_approved=False,
        )


@pytest.mark.asyncio
async def test_post_publication_gc_partial_failure_preserves_known_delete_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_gc(**_kwargs: Any) -> Any:
        raise GCPartialFailure(deleted_count=2) from None

    monkeypatch.setattr("cardrag_worker.gc.collect_garbage", fail_gc)
    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[Adapter((source(),))],
            ocr=FakeOCR(),  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=True,
            stable_publication_approved=True,
            remote_gc_approved=True,
        )

        async def publish_success(run_id: str, *, refresh_sources: bool = False) -> Any:
            assert refresh_sources is False
            state.finish_run(
                run_id,
                "succeeded",
                corpus_sha256="a" * 64,
                contract_sha256="b" * 64,
            )
            return pipeline_module.PipelineResult(
                run_id=run_id,
                status="succeeded",
                corpus_sha256="a" * 64,
                contract_sha256="b" * 64,
                generation_id="g-gc-partial",
                document_count=1,
                evidence_count=1,
            )

        pipeline._run_locked = publish_success  # type: ignore[method-assign]
        result = await pipeline.run()

    assert result.status == "succeeded"
    assert result.gc_status == "failed"
    assert result.gc_deleted == 2
    assert result.gc_error == pipeline_module.REMOTE_GC_PARTIAL_ERROR


@pytest.mark.parametrize(
    "exc",
    [
        OCRValidationError("private validation detail"),
        ProviderDocumentError(),
        TimeoutError("private timeout detail"),
        httpx.ReadTimeout(
            "private timeout detail",
            request=httpx.Request("POST", "https://private.example/token"),
        ),
        *(http_status_error(status, raw_detail="private detail") for status in (408, 413, 422, 425, 429)),
        http_status_error(500, raw_detail="private detail"),
        http_status_error(599, raw_detail="private detail"),
    ],
)
def test_document_scoped_ocr_failure_allowlist(exc: Exception) -> None:
    assert is_isolatable_document_ocr_failure(exc)


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("private runtime detail"),
        ValueError("private value detail"),
        ProviderError("private provider detail"),
        ProviderSystemicError("provider_process_exit", exit_code=17),
        OSError("private local path"),
        sqlite3.OperationalError("private database detail"),
        httpx.ConnectError(
            "private connect detail",
            request=httpx.Request("POST", "https://private.example/token"),
        ),
        httpx.RequestError(
            "private request detail",
            request=httpx.Request("POST", "https://private.example/token"),
        ),
        *(http_status_error(status, raw_detail="private detail") for status in (400, 401, 402, 403, 404)),
    ],
)
def test_systemic_ocr_failure_is_not_in_document_allowlist(exc: Exception) -> None:
    assert not is_isolatable_document_ocr_failure(exc)


@pytest.mark.parametrize(
    ("exc", "reason_code"),
    [
        (OCRValidationError("OCR sparse-page wrapper is invalid"), "sparse_page_wrapper_invalid"),
        (OCRValidationError("OCR page markers [2] do not match [1]"), "page_marker_mismatch"),
        (
            OCRValidationError("OCR provider output must begin with the first Page marker"),
            "output_not_marker_first",
        ),
        (OCRValidationError("OCR provider returned an empty page"), "empty_page"),
        (OCRValidationError("OCR blank-page sentinel must be exact"), "blank_sentinel_invalid"),
        (
            OCRValidationError("OCR provider returned an implausibly short page"),
            "implausibly_short",
        ),
        (OCRValidationError("invalid native OCR cache JSON"), "cache_validation_error"),
        (OCRValidationError("private validation detail"), "generic_validation_error"),
        (ProviderDocumentError(), "provider_document_rejected"),
        (ProviderError("opaque provider detail"), "provider_error"),
        (TimeoutError("private timeout detail"), "provider_timeout"),
        (
            httpx.HTTPStatusError(
                "private HTTP detail",
                request=httpx.Request("POST", "https://private.example/token"),
                response=httpx.Response(
                    503,
                    request=httpx.Request("POST", "https://private.example/token"),
                ),
            ),
            "provider_http_503",
        ),
        (
            httpx.ConnectError(
                "private network detail",
                request=httpx.Request("POST", "https://private.example/token"),
            ),
            "provider_network_error",
        ),
        (OSError("/private/path"), "local_io_error"),
        (RuntimeError("private unexpected detail"), "unexpected_error"),
    ],
)
def test_ocr_failure_classification_is_allowlisted(exc: Exception, reason_code: str) -> None:
    failure = classify_ocr_failure(exc)
    assert failure.reason_code == reason_code
    if isinstance(exc, ProviderDocumentError):
        assert failure.stored_error == str(exc)
    else:
        assert str(exc) not in failure.stored_error


def test_mutated_document_provider_error_is_recanonicalized_without_secret() -> None:
    raw_sentinel = "RAW_DOCUMENT_PROVIDER_TOKEN_SECRET"
    error = ProviderDocumentError()
    error.reason = raw_sentinel
    error.reason_code = raw_sentinel
    error.args = (raw_sentinel,)

    failure = classify_ocr_failure(error)

    assert failure.reason_code == "provider_document_rejected"
    assert failure.reason == "The OCR provider could not process this document."
    assert raw_sentinel not in failure.stored_error


@pytest.mark.asyncio
async def test_stage_failure_bookkeeping_error_suppresses_original_exception_context(
    tmp_path: Path,
) -> None:
    raw_sentinel = "RAW_PROVIDER_STDERR_SECRET_SENTINEL"
    with WorkerState(tmp_path / "state.sqlite3") as state:
        run_id = state.start_run(run_id="run-safe-bookkeeping")
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[Adapter((source(),))],
            ocr=FakeOCR(),  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
            maximum_attempts=1,
        )

        async def fail() -> None:
            raise ProviderError(raw_sentinel)

        def broken_formatter(_exc: Exception) -> str:
            raise RuntimeError("private formatter failure")

        with pytest.raises(RuntimeError, match="stage failure bookkeeping failed") as captured:
            await pipeline._finite_stage(  # noqa: SLF001
                run_id=run_id,
                document_id="doc_" + "a" * 64,
                name="ocr",
                operation=fail,
                error_formatter=broken_formatter,
            )
        rendered = "".join(
            traceback.format_exception(
                type(captured.value),
                captured.value,
                captured.value.__traceback__,
            )
        )
        assert raw_sentinel not in rendered
        assert "private formatter failure" not in rendered


@pytest.mark.asyncio
async def test_retry_backoff_cancellation_has_no_provider_exception_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_sentinel = "RAW_PROVIDER_BACKOFF_STDERR_SENTINEL"
    sleep_started = asyncio.Event()

    async def blocked_sleep(_delay: float) -> None:
        sleep_started.set()
        await asyncio.Future()

    monkeypatch.setattr(pipeline_module.asyncio, "sleep", blocked_sleep)
    with WorkerState(tmp_path / "state.sqlite3") as state:
        run_id = state.start_run(run_id="run-safe-backoff-cancel")
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[Adapter((source(),))],
            ocr=FakeOCR(),  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
            maximum_attempts=2,
        )

        async def fail() -> None:
            raise ProviderError(raw_sentinel)

        task = asyncio.create_task(
            pipeline._finite_stage(  # noqa: SLF001
                run_id=run_id,
                document_id="doc_" + "a" * 64,
                name="ocr",
                operation=fail,
                error_formatter=lambda exc: classify_ocr_failure(exc).stored_error,
            )
        )
        await sleep_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as captured:
            await task

        rendered = "".join(
            traceback.format_exception(
                type(captured.value),
                captured.value,
                captured.value.__traceback__,
            )
        )
        assert captured.value.__context__ is None
        assert raw_sentinel not in rendered
        stage = state.get_stage(run_id, "doc_" + "a" * 64, "ocr")
        assert stage is not None
        assert (stage.status, stage.attempt_count, stage.last_error) == (
            "retry",
            1,
            "provider_error: The OCR provider failed.",
        )


@pytest.mark.asyncio
async def test_exhausted_ocr_state_lookup_failure_suppresses_provider_and_state_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = pdf_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    record = source()
    document_id = record.document_id(digest)
    provider_sentinel = "RAW_PROVIDER_STATE_LOOKUP_SENTINEL"
    state_sentinel = "RAW_STATE_LOOKUP_SENTINEL"
    ocr = SelectiveOCR({document_id}, provider_sentinel)
    requests: list[str] = []
    install_http(monkeypatch, payload, requests)

    with WorkerState(tmp_path / "state.sqlite3") as state:
        real_get_stage = state.get_stage
        ocr_get_calls = 0

        def fail_second_ocr_lookup(
            run_id: str,
            current_document_id: str,
            stage_name: str,
        ) -> Any:
            nonlocal ocr_get_calls
            if current_document_id == document_id and stage_name == "ocr":
                ocr_get_calls += 1
                if ocr_get_calls == 2:
                    raise sqlite3.OperationalError(state_sentinel)
            return real_get_stage(run_id, current_document_id, stage_name)

        monkeypatch.setattr(state, "get_stage", fail_second_ocr_lookup)
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[Adapter((record,))],
            ocr=ocr,  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
            maximum_attempts=1,
        )
        with pytest.raises(OCRFailureBookkeepingError) as captured:
            await pipeline.run()

        rendered = "".join(
            traceback.format_exception(
                type(captured.value),
                captured.value,
                captured.value.__traceback__,
            )
        )
        run_row = state.connection.execute("SELECT run_id,status,error FROM run").fetchone()
        assert run_row is not None
        run_id = str(run_row["run_id"])
        stage_row = state.connection.execute(
            """SELECT status,attempt_count,last_error FROM stage
               WHERE run_id=? AND document_id=? AND stage_name='ocr'""",
            (run_id, document_id),
        ).fetchone()
        assert stage_row is not None
        assert tuple(stage_row) == (
            "failed",
            1,
            "provider_document_rejected: The OCR provider could not process this document.",
        )
        assert (run_row["status"], run_row["error"]) == (
            "failed",
            "OCR failure isolation bookkeeping failed",
        )
        assert not (tmp_path / "runs" / run_id / "reports" / "ocr-failures.json").exists()
        leaked = rendered + str(run_row["error"]) + str(stage_row["last_error"]) + caplog.text
        assert provider_sentinel not in leaked
        assert state_sentinel not in leaked

    assert ocr.calls == [document_id]
    assert len(requests) == 1


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
async def test_exact_remote_with_ocr_failure_is_retried_instead_of_no_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = pdf_bytes()
    record = source()
    requests: list[str] = []
    install_http(monkeypatch, payload, requests)
    ocr = FakeOCR()

    with WorkerState(tmp_path / "state.sqlite3") as state:
        adapter = Adapter((record,))
        webdav = FakeWebDAV(None)
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[adapter],
            ocr=ocr,  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=webdav,  # type: ignore[arg-type]
            collect_remote_garbage=False,
            maximum_attempts=1,
        )
        webdav.current = RemoteGenerationIdentity(
            generation_id="g-partial",
            corpus_sha256=corpus_for(payload, record, tmp_path),
            contract_sha256=pipeline.contract_sha256,
            generation_schema="cardrag.generation.v4",
            serving_schema="cardrag.serving-db.v4",
        )
        first = await pipeline.run()
        assert first.status == "no_change"
        assert (first.pdf_cache_misses, first.pdf_downloads) == (1, 1)
        assert ocr.calls == 0

        # The next validated pointer reports a partial v4 generation with the
        # same corpus and contract. Cached PDF bytes remain reusable, but OCR
        # must run again instead of returning no_change forever.
        webdav.current = replace(webdav.current, ocr_failed_document_count=1)

        with pytest.raises(OCRSystemicFailureError):
            await pipeline.run()

    assert requests == [record.source_url]
    assert adapter.prepare_calls == 1
    assert ocr.calls == 1


@pytest.mark.asyncio
async def test_pipeline_reuses_pdf_cas_across_runs_without_run_scoped_pdf_copies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = pdf_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    record = source()
    requests: list[str] = []
    install_http(
        monkeypatch,
        payload,
        requests,
        response_headers={
            "etag": '"guide-v1"',
            "last-modified": "Wed, 26 Aug 2026 00:00:00 GMT",
        },
    )
    adapter = Adapter((record,))

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
            corpus_sha256=corpus_for(payload, record, tmp_path),
            contract_sha256=pipeline.contract_sha256,
        )

        first = await pipeline.run()
        second = await pipeline.run()

        assert (first.pdf_cache_hits, first.pdf_cache_misses, first.pdf_downloads) == (0, 1, 1)
        assert (second.pdf_cache_hits, second.pdf_cache_misses, second.pdf_downloads) == (1, 0, 0)
        assert first.pdf_revisions == second.pdf_revisions == 0
        binding = state.pdf_cache_source_binding(record.source_id)
        assert binding is not None
        assert (binding.pdf_sha256, binding.etag, binding.last_modified) == (
            digest,
            '"guide-v1"',
            "Wed, 26 Aug 2026 00:00:00 GMT",
        )
        assert len(state.pdf_cache_source_history(record.source_id)) == 1
        assert pipeline.pdf_cache.object_path(digest).is_file()

    assert requests == [record.source_url]
    assert adapter.prepare_calls == 1
    runs_root = tmp_path / "runs"
    assert not runs_root.exists() or not list(runs_root.rglob("*.pdf"))


@pytest.mark.asyncio
async def test_expired_pdf_cache_uses_validators_and_304_refreshes_origin_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = pdf_bytes()
    record = source()
    request_headers: list[httpx.Headers] = []
    real_async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        request_headers.append(request.headers)
        if len(request_headers) == 1:
            return httpx.Response(
                200,
                headers={
                    "content-type": "application/pdf",
                    "etag": '"guide-v1"',
                    "last-modified": "Wed, 26 Aug 2026 00:00:00 GMT",
                },
                content=payload,
                request=request,
            )
        return httpx.Response(304, headers={"etag": '"guide-v1"'}, request=request)

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return real_async_client(transport=httpx.MockTransport(handler), timeout=kwargs.get("timeout"))

    monkeypatch.setattr(pipeline_module.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(
        pipeline_module,
        "SecurePDFDownloader",
        lambda policy: RealDownloader(policy, resolver=lambda _host: ("93.184.216.34",)),
    )

    with WorkerState(tmp_path / "state.sqlite3") as state:
        adapter = Adapter((record,))
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
            corpus_sha256=corpus_for(payload, record, tmp_path),
            contract_sha256=pipeline.contract_sha256,
        )

        await pipeline.run()
        expired = expire_pdf_cache_binding(state, record.source_id)
        second = await pipeline.run()

        assert second.status == "no_change"
        assert second.pdf_cache_hits == 1
        assert second.pdf_cache_misses == 0
        assert second.pdf_cache_revalidations == 1
        assert second.pdf_cache_not_modified == 1
        assert second.pdf_downloads == 0
        assert len(state.pdf_cache_source_history(record.source_id)) == 1
        refreshed = state.pdf_cache_source_binding(record.source_id)
        assert refreshed is not None
        assert refreshed.revision_last_observed_at > expired

    assert len(request_headers) == 2
    assert request_headers[1]["if-none-match"] == '"guide-v1"'
    assert request_headers[1]["if-modified-since"] == "Wed, 26 Aug 2026 00:00:00 GMT"
    assert adapter.prepare_calls == 2


@pytest.mark.asyncio
async def test_expired_validatorless_pdf_cache_downloads_same_bytes_without_new_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = pdf_bytes()
    record = source()
    requests: list[str] = []
    install_http(monkeypatch, payload, requests)

    with WorkerState(tmp_path / "state.sqlite3") as state:
        adapter = Adapter((record,))
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
            corpus_sha256=corpus_for(payload, record, tmp_path),
            contract_sha256=pipeline.contract_sha256,
        )

        await pipeline.run()
        expire_pdf_cache_binding(state, record.source_id)
        second = await pipeline.run()

        assert second.pdf_cache_hits == 1
        assert second.pdf_cache_revalidations == 1
        assert second.pdf_cache_not_modified == 0
        assert second.pdf_downloads == 1
        assert second.pdf_revisions == 0
        assert len(state.pdf_cache_source_history(record.source_id)) == 1

    assert requests == [record.source_url, record.source_url]
    assert adapter.prepare_calls == 2


@pytest.mark.asyncio
async def test_expired_pdf_cache_200_changed_bytes_creates_revision_and_prunes_old_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_payload = pdf_bytes(width=612)
    new_payload = pdf_bytes(width=613)
    old_digest = hashlib.sha256(old_payload).hexdigest()
    new_digest = hashlib.sha256(new_payload).hexdigest()
    bodies = [old_payload, new_payload]
    seen_headers: list[httpx.Headers] = []
    record = source()
    real_async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        index = len(seen_headers)
        seen_headers.append(request.headers)
        return httpx.Response(
            200,
            headers={"content-type": "application/pdf", "etag": f'"guide-v{index + 1}"'},
            content=bodies[index],
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

    with WorkerState(tmp_path / "state.sqlite3") as state:
        adapter = Adapter((record,))
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
            generation_id="g-old",
            corpus_sha256=corpus_for(old_payload, record, tmp_path),
            contract_sha256=pipeline.contract_sha256,
        )

        await pipeline.run()
        expire_pdf_cache_binding(state, record.source_id)
        webdav.current = RemoteGenerationIdentity(
            generation_id="g-new",
            corpus_sha256=corpus_for(new_payload, record, tmp_path),
            contract_sha256=pipeline.contract_sha256,
        )
        second = await pipeline.run()

        assert second.pdf_cache_revalidations == 1
        assert second.pdf_downloads == 1
        assert second.pdf_revisions == 1
        assert second.pdf_cache_prune_status == "succeeded"
        assert second.pdf_cache_pruned_objects == 1
        assert second.pdf_cache_pruned_bytes > 0
        history = state.pdf_cache_source_history(record.source_id)
        assert [row.pdf_sha256 for row in history] == [old_digest, new_digest]
        assert not pipeline.pdf_cache.object_path(old_digest).exists()
        assert pipeline.pdf_cache.object_path(new_digest).is_file()

    assert seen_headers[1]["if-none-match"] == '"guide-v1"'


@pytest.mark.asyncio
async def test_successful_run_prune_protects_current_and_two_retained_seal_pdfs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_payload = pdf_bytes(width=612)
    record = source()
    requests: list[str] = []
    install_http(monkeypatch, current_payload, requests)

    with WorkerState(tmp_path / "state.sqlite3") as state:
        adapter = Adapter((record,))
        webdav = FakeWebDAV(None)
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[adapter],
            ocr=FakeOCR(),  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=webdav,  # type: ignore[arg-type]
            collect_remote_garbage=False,
            retained_generations=2,
        )
        retained_digests: list[str] = []
        for index, width in enumerate((620, 621, 622), start=1):
            candidate = tmp_path / f"retained-{index}.pdf"
            candidate.write_bytes(pdf_bytes(width=width))
            retained_digests.append(pipeline.pdf_cache.ingest(candidate).sha256)
            seal_path = tmp_path / "runs" / f"retained-{index}" / "sealed" / "publish.json"
            seal_path.parent.mkdir(parents=True)
            seal_path.write_text(
                json.dumps(
                    {
                        "run_id": f"retained-{index}",
                        "pdf_sha256": retained_digests[-1],
                    }
                ),
                encoding="utf-8",
            )
            retained_run_id = f"retained-{index}"
            state.start_run(run_id=retained_run_id)
            state.finish_run(retained_run_id, "succeeded")
            state.record_publish(
                generation_id=f"generation-{index}",
                run_id=retained_run_id,
                corpus_sha256=f"{index}" * 64,
                contract_sha256="c" * 64,
                serving_sha256="d" * 64,
                status="ready",
                details={},
            )
            state.connection.execute(
                "UPDATE publish SET published_at=? WHERE generation_id=?",
                (f"2026-08-{20 + index:02d}T00:00:00+00:00", f"generation-{index}"),
            )

        async def validate_retained(sealed: dict[str, Any]) -> Any:
            document = SimpleNamespace(pdf=SimpleNamespace(sha256=sealed["pdf_sha256"]))
            return SimpleNamespace(manifest=SimpleNamespace(documents=(document,)))

        monkeypatch.setattr(pipeline, "_validate_local_seal", validate_retained)
        webdav.current = RemoteGenerationIdentity(
            generation_id="g-current",
            corpus_sha256=corpus_for(current_payload, record, tmp_path),
            contract_sha256=pipeline.contract_sha256,
        )

        result = await pipeline.run()

        assert result.pdf_cache_prune_status == "succeeded"
        assert result.pdf_cache_pruned_objects == 1
        assert state.retained_publication_run_ids(limit=2) == ("retained-3", "retained-2")
        assert not pipeline.pdf_cache.object_path(retained_digests[0]).exists()
        assert all(pipeline.pdf_cache.object_path(digest).is_file() for digest in retained_digests[1:])
        assert pipeline.pdf_cache.lookup(record.source_id) is not None


@pytest.mark.parametrize("seal_kind", ["missing", "symlink"])
@pytest.mark.asyncio
async def test_missing_or_unsafe_retained_seal_fails_prune_without_deleting_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seal_kind: str,
) -> None:
    payload = pdf_bytes(width=612)
    record = source()
    requests: list[str] = []
    install_http(monkeypatch, payload, requests)

    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[Adapter((record,))],
            ocr=FakeOCR(),  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
        )
        stale_path = tmp_path / "stale.pdf"
        stale_path.write_bytes(pdf_bytes(width=620))
        stale = pipeline.pdf_cache.ingest(stale_path)
        retained_run_id = "retained-unsafe"
        state.start_run(run_id=retained_run_id)
        state.finish_run(retained_run_id, "succeeded")
        state.record_publish(
            generation_id="generation-retained-unsafe",
            run_id=retained_run_id,
            corpus_sha256="a" * 64,
            contract_sha256="b" * 64,
            serving_sha256="c" * 64,
            status="ready",
            details={},
        )
        if seal_kind == "symlink":
            outside = tmp_path / "outside-seal.json"
            outside.write_text("{}", encoding="utf-8")
            seal_path = tmp_path / "runs" / retained_run_id / "sealed" / "publish.json"
            seal_path.parent.mkdir(parents=True)
            seal_path.symlink_to(outside)
        webdav = pipeline.webdav
        webdav.current = RemoteGenerationIdentity(  # type: ignore[attr-defined]
            generation_id="g-current",
            corpus_sha256=corpus_for(payload, record, tmp_path),
            contract_sha256=pipeline.contract_sha256,
        )

        result = await pipeline.run()

        assert result.pdf_cache_prune_status == "failed"
        assert result.pdf_cache_pruned_objects == 0
        assert result.pdf_cache_prune_error == pipeline_module.PDF_CACHE_PRUNE_ERROR
        assert stale.path.is_file()
        binding = state.pdf_cache_source_binding(record.source_id)
        assert binding is not None
        assert pipeline.pdf_cache.object_path(binding.pdf_sha256).is_file()


@pytest.mark.asyncio
async def test_post_publication_prune_failure_returns_fixed_safe_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = pdf_bytes(width=612)
    record = source()
    raw_sentinel = "RAW_PDF_CACHE_PATH_SECRET"
    requests: list[str] = []
    install_http(monkeypatch, payload, requests)

    def fail_prune(_cache: object, _protected: set[str]) -> object:
        raise OSError(raw_sentinel)

    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[Adapter((record,))],
            ocr=FakeOCR(),  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
        )
        monkeypatch.setattr(type(pipeline.pdf_cache), "prune", fail_prune)
        pipeline.webdav.current = RemoteGenerationIdentity(  # type: ignore[attr-defined]
            generation_id="g-current",
            corpus_sha256=corpus_for(payload, record, tmp_path),
            contract_sha256=pipeline.contract_sha256,
        )

        result = await pipeline.run()

        status = state.connection.execute(
            "SELECT status FROM run WHERE run_id=?", (result.run_id,)
        ).fetchone()[0]
        assert (result.status, status) == ("no_change", "no_change")
        assert result.pdf_cache_prune_status == "failed"
        assert result.pdf_cache_prune_error == pipeline_module.PDF_CACHE_PRUNE_ERROR
        assert raw_sentinel not in result.pdf_cache_prune_error
        assert raw_sentinel not in caplog.text


@pytest.mark.asyncio
async def test_post_publication_prune_partial_failure_preserves_known_delete_metrics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = pdf_bytes(width=612)
    record = source()
    requests: list[str] = []
    install_http(monkeypatch, payload, requests)

    def fail_prune(_cache: object, _protected: set[str]) -> object:
        raise pipeline_module.PDFCachePruneError(deleted_objects=2, deleted_bytes=1234) from None

    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[Adapter((record,))],
            ocr=FakeOCR(),  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
        )
        monkeypatch.setattr(type(pipeline.pdf_cache), "prune", fail_prune)
        pipeline.webdav.current = RemoteGenerationIdentity(  # type: ignore[attr-defined]
            generation_id="g-current",
            corpus_sha256=corpus_for(payload, record, tmp_path),
            contract_sha256=pipeline.contract_sha256,
        )

        result = await pipeline.run()

        assert result.status == "no_change"
        assert result.pdf_cache_prune_status == "failed"
        assert result.pdf_cache_pruned_objects == 2
        assert result.pdf_cache_pruned_bytes == 1234
        assert result.pdf_cache_prune_error == pipeline_module.PDF_CACHE_PRUNE_ERROR


@pytest.mark.asyncio
async def test_prune_cancellation_keeps_worker_lock_until_blocking_delete_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = pdf_bytes(width=612)
    record = source()
    requests: list[str] = []
    install_http(monkeypatch, payload, requests)
    prune_started = threading.Event()
    prune_release = threading.Event()
    prune_finished = threading.Event()
    real_prune = pipeline_module.PDFCache.prune

    def blocking_prune(cache: object, protected: set[str]) -> object:
        prune_started.set()
        if not prune_release.wait(timeout=5):
            raise AssertionError("test did not release PDF cache prune")
        result = real_prune(cache, protected)  # type: ignore[arg-type]
        prune_finished.set()
        return result

    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[Adapter((record,))],
            ocr=FakeOCR(),  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
        )
        pipeline.webdav.current = RemoteGenerationIdentity(  # type: ignore[attr-defined]
            generation_id="g-current",
            corpus_sha256=corpus_for(payload, record, tmp_path),
            contract_sha256=pipeline.contract_sha256,
        )
        monkeypatch.setattr(pipeline_module.PDFCache, "prune", blocking_prune)

        task = asyncio.create_task(pipeline.run())
        for _ in range(1_000):
            if prune_started.is_set():
                break
            await asyncio.sleep(0.001)
        else:
            raise AssertionError("PDF cache prune did not start")
        task.cancel()
        await asyncio.sleep(0)

        contender = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[Adapter((record,))],
            ocr=FakeOCR(),  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
        )
        with pytest.raises(AlreadyRunning):
            await contender.run()
        assert not task.done()
        assert not prune_finished.is_set()

        prune_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert prune_finished.is_set()
        row = state.connection.execute("SELECT status,error FROM run").fetchone()
        assert tuple(row) == ("no_change", None)


@pytest.mark.asyncio
async def test_failed_run_never_prunes_pdf_cache_objects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = pdf_bytes(width=612)
    record = source()
    requests: list[str] = []
    install_http(monkeypatch, payload, requests)

    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[Adapter((record,))],
            ocr=FakeOCR(),  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
            maximum_attempts=1,
        )
        stale_path = tmp_path / "stale.pdf"
        stale_path.write_bytes(pdf_bytes(width=620))
        stale = pipeline.pdf_cache.ingest(stale_path)

        with pytest.raises(OCRSystemicFailureError):
            await pipeline.run()

        assert stale.path.is_file()
        run_status = state.connection.execute("SELECT status FROM run").fetchone()[0]
        assert run_status == "failed"


@pytest.mark.asyncio
async def test_pipeline_cache_miss_records_same_source_byte_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    old_payload = pdf_bytes(width=612)
    new_payload = pdf_bytes(width=613)
    old_digest = hashlib.sha256(old_payload).hexdigest()
    new_digest = hashlib.sha256(new_payload).hexdigest()
    record = source()
    requests: list[str] = []
    response_bodies = [old_payload, new_payload]
    real_async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        index = len(requests)
        requests.append(str(request.url))
        return httpx.Response(
            200,
            headers={"content-type": "application/pdf", "etag": f'"guide-{index + 1}"'},
            content=response_bodies[index],
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
    caplog.set_level("INFO", logger="cardrag_worker.pipeline")

    with WorkerState(tmp_path / "state.sqlite3") as state:
        adapter = Adapter((record,))
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
            generation_id="g-old",
            corpus_sha256=corpus_for(old_payload, record, tmp_path),
            contract_sha256=pipeline.contract_sha256,
        )
        first = await pipeline.run()
        assert first.pdf_downloads == 1

        # A wrong regular file at the old digest path must be a safe cache miss;
        # the fresh issuer response then becomes a new revision for the same URL.
        pipeline.pdf_cache.object_path(old_digest).write_bytes(new_payload)
        webdav.current = RemoteGenerationIdentity(
            generation_id="g-new",
            corpus_sha256=corpus_for(new_payload, record, tmp_path),
            contract_sha256=pipeline.contract_sha256,
        )
        second = await pipeline.run()

        assert (second.pdf_cache_hits, second.pdf_cache_misses) == (0, 1)
        assert (second.pdf_downloads, second.pdf_revisions) == (1, 1)
        history = state.pdf_cache_source_history(record.source_id)
        assert [row.pdf_sha256 for row in history] == [old_digest, new_digest]
        assert history[0].superseded_at is not None
        assert history[1].previous_revision_id == history[0].revision_id
        assert history[1].etag == '"guide-2"'
        assert pipeline.pdf_cache.object_path(new_digest).is_file()

    assert requests == [record.source_url, record.source_url]
    assert "PDF revision detected" in caplog.text
    runs_root = tmp_path / "runs"
    assert not runs_root.exists() or not list(runs_root.rglob("*.pdf"))


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
        with pytest.raises(OCRSystemicFailureError) as captured:
            await pipeline.run()
        run_row = state.connection.execute("SELECT run_id,status,error FROM run").fetchone()
        assert run_row is not None
        run_id = str(run_row["run_id"])
        document_id = record.document_id(hashlib.sha256(new_payload).hexdigest())
        stage = state.get_stage(run_id, document_id, "ocr")
        assert stage is not None
        assert (stage.status, stage.attempt_count, stage.last_error) == (
            "failed",
            1,
            captured.value.stored_error,
        )
        assert captured.value.report_path.is_file()
        assert captured.value.failure.reason_code == "ocr_unexpected_error"
        assert not (tmp_path / "runs" / run_id / "reports" / "ocr-failures.json").exists()
    assert len(requests) == 1
    assert ocr.calls == 1


@pytest.mark.asyncio
async def test_run_marks_preexisting_running_state_interrupted_under_worker_lock(tmp_path: Path) -> None:
    with WorkerState(tmp_path / "state.sqlite3") as state:
        state.start_run(run_id="stale-run")
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[Adapter(())],
            ocr=FakeOCR(),  # type: ignore[arg-type]
            embeddings=FakeEmbeddings(),
            webdav=FakeWebDAV(None),  # type: ignore[arg-type]
            collect_remote_garbage=False,
        )
        with pytest.raises(WorkerUnexpectedFailureError) as captured:
            await pipeline.run()

        rows = {
            str(row["run_id"]): (str(row["status"]), row["finished_at"], row["error"])
            for row in state.connection.execute(
                "SELECT run_id,status,finished_at,error FROM run ORDER BY started_at"
            )
        }
        assert rows["stale-run"][0] == "interrupted"
        assert rows["stale-run"][1] is not None
        assert "terminal state" in str(rows["stale-run"][2])
        current = next(value for run_id, value in rows.items() if run_id != "stale-run")
        assert current[0] == "failed"
        assert current[2] == captured.value.stored_error
        assert captured.value.failure.error_class_category == "runtime"


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
        with pytest.raises(WorkerUnexpectedFailureError) as captured:
            await pipeline.run()
        assert captured.value.failure.error_class_category == "runtime"
        assert captured.value.report_path.is_file()
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
