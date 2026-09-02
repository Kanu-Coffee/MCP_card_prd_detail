from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import httpx
import pytest
from cardrag_core import (
    ArtifactRef,
    DocumentAggregationBootstrap,
    DocumentAggregationProfile,
    GenerationManifest,
    GenerationReady,
    MaxChildAggregationDefinition,
    canonical_sha256,
    channel_pointer_path,
    generation_database_path,
    generation_manifest_path,
    generation_ready_path,
    generation_vectors_path,
    object_path,
)
from helpers import pdf_bytes

import cardrag_worker.pipeline as pipeline_module
from cardrag_worker.aggregation_profile_v5 import VerifiedAggregationProfileV5
from cardrag_worker.capacity_v5 import V5CapacityError
from cardrag_worker.contracts import (
    DownloadRequest,
    IssuerSpec,
    SourceRecord,
    snapshot_from_records,
)
from cardrag_worker.downloader import DownloadedPDF
from cardrag_worker.downloader import SecurePDFDownloader as RealDownloader
from cardrag_worker.embedding_v5 import (
    OpenRouterEndpointMetadata,
    OpenRouterQwenEmbeddingProviderV5,
    QwenEmbeddingProfileV5,
)
from cardrag_worker.exporter_v5 import LazyEmbeddingVector
from cardrag_worker.ocr import OCRResult
from cardrag_worker.pipeline import (
    StructureDocumentFailuresError,
    WorkerPipeline,
    WorkerUnexpectedFailureError,
)
from cardrag_worker.revision_history_v5 import (
    REVISION_HISTORY_POLICY_VERSION,
    TemporalStatusV5,
    UnresolvedRevisionIdentityV5,
    canonical_unresolved_revision_ledger_v5,
    unresolved_revision_ledger_sha256_v5,
)
from cardrag_worker.state import WorkerState
from cardrag_worker.structure import StructureValidationError
from cardrag_worker.tokenizer_v5 import QWEN_TOKENIZER_REVISION, QWEN_TOKENIZER_SHA256
from cardrag_worker.webdav import RemoteGenerationIdentity


class _PinnedFakeTokenCounter:
    asset_sha256 = QWEN_TOKENIZER_SHA256
    revision = QWEN_TOKENIZER_REVISION

    def __call__(self, text: str) -> int:
        # This integration test injects a deterministic counter while binding
        # the same immutable tokenizer identity enforced by WorkerPipeline.
        return max(1, len(text))


def test_v5_corpus_identity_binds_temporal_supersession_and_unresolved_truth(
    tmp_path: Path,
) -> None:
    old = SourceRecord(
        issuer="testbank",
        product_code="test-001",
        product_name="테스트 카드",
        effective_date=date(2025, 8, 1),
        source_version="old",
        source_url="https://cards.example/old.pdf",
        source_post_id="post-old",
        file_name="old.pdf",
        category="credit",
        discovered_at=datetime(2026, 8, 29, tzinfo=UTC),
    )
    current = SourceRecord(
        issuer="testbank",
        product_code="test-001",
        product_name="테스트 카드",
        effective_date=date(2026, 8, 1),
        source_version="current",
        source_url="https://cards.example/current.pdf",
        source_post_id="post-current",
        file_name="current.pdf",
        category="credit",
        discovered_at=datetime(2026, 8, 29, tzinfo=UTC),
    )

    def acquired(
        source: SourceRecord,
        digest: str,
        *,
        temporal_status: TemporalStatusV5,
        supersedes_document_id: str | None = None,
    ) -> Any:
        return pipeline_module._AcquiredDocument(  # noqa: SLF001
            source=source,
            pdf=DownloadedPDF(
                path=tmp_path / f"{digest}.pdf",
                sha256=digest,
                size_bytes=100,
                page_count=1,
                final_url=source.source_url,
            ),
            temporal_status=temporal_status,
            supersedes_document_id=supersedes_document_id,
        )

    empty_ledger = canonical_unresolved_revision_ledger_v5(())
    empty_sha = unresolved_revision_ledger_sha256_v5(empty_ledger)
    old_document_id = old.document_id("a" * 64)

    def digest(rows: tuple[Any, ...], ledger: tuple[Any, ...] = empty_ledger) -> str:
        return canonical_sha256(
            pipeline_module._v5_corpus_identity_payload(  # noqa: SLF001
                acquired=rows,
                unsupported_documents=(),
                unresolved_revisions=ledger,
                unresolved_revision_sha256=unresolved_revision_ledger_sha256_v5(ledger),
            )
        )

    metadata_only_observation = replace(current, source_version="current-metadata-2")
    current_acquired = acquired(current, "b" * 64, temporal_status="current")
    metadata_acquired = acquired(
        metadata_only_observation,
        "b" * 64,
        temporal_status="current",
    )
    changed_discovery_fields = {
        key
        for key, value in current.discovery_payload.items()
        if metadata_only_observation.discovery_payload[key] != value
    }
    assert changed_discovery_fields == {"source_version"}
    assert current_acquired.pdf.sha256 == metadata_acquired.pdf.sha256
    assert current.source_id != metadata_only_observation.source_id
    assert current.document_id("b" * 64) != metadata_only_observation.document_id("b" * 64)
    assert digest((current_acquired,)) != digest((metadata_acquired,))

    ambiguous_rows = (
        acquired(old, "a" * 64, temporal_status="ambiguous"),
        acquired(
            current,
            "b" * 64,
            temporal_status="current",
            supersedes_document_id=old_document_id,
        ),
    )
    superseded_rows = (
        acquired(old, "a" * 64, temporal_status="superseded"),
        ambiguous_rows[1],
    )
    no_supersession_rows = (
        superseded_rows[0],
        acquired(current, "b" * 64, temporal_status="current"),
    )
    unresolved_ledger = canonical_unresolved_revision_ledger_v5(
        (
            UnresolvedRevisionIdentityV5(
                source_id=old.source_id,
                pdf_sha256="c" * 64,
                reason_code="source_metadata_unresolved",
            ),
        )
    )

    assert digest(ambiguous_rows) != digest(superseded_rows)
    assert digest(superseded_rows) != digest(no_supersession_rows)
    assert digest(superseded_rows) != digest(superseded_rows, unresolved_ledger)
    payload = pipeline_module._v5_corpus_identity_payload(  # noqa: SLF001
        acquired=superseded_rows,
        unsupported_documents=(),
        unresolved_revisions=empty_ledger,
        unresolved_revision_sha256=empty_sha,
    )
    assert payload["schema_version"] == "cardrag.current-corpus.v3"
    assert payload["revision_history"]["policy_version"] == REVISION_HISTORY_POLICY_VERSION


class _OCR:
    contract = {"schema_version": "test-ocr.v1"}
    adoption_policy_version = "cardrag.legacy-ocr-adoption.v1"
    cache_mode = "read-only"

    def __init__(self) -> None:
        self.calls = 0

    async def resolve(self, **_kwargs: Any) -> OCRResult:
        self.calls += 1
        page = """테스트카드 상품설명서
## 주요 혜택
### 대중교통 할인
- 전월 이용금액 30만원 이상이면 대중교통 이용금액의 10%를 할인합니다.
## 이용 전 확인사항
상품권 구매금액은 전월 이용실적에서 제외됩니다.
""".strip()
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


class _Adapter:
    parser_version = "test-adapter.v1"

    def __init__(self, record: SourceRecord) -> None:
        self.record = record
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

    async def discover_current(self, _client: httpx.AsyncClient) -> Any:
        return snapshot_from_records(
            issuer=self.spec.code,
            source_url="https://cards.example/list",
            parser_version=self.parser_version,
            records=(self.record,),
            started_at=datetime(2026, 8, 29, tzinfo=UTC),
        )

    async def prepare_download(
        self,
        _client: httpx.AsyncClient,
        source: SourceRecord,
    ) -> DownloadRequest:
        return DownloadRequest(url=source.source_url)


class _MultiAdapter(_Adapter):
    def __init__(self, records: tuple[SourceRecord, ...]) -> None:
        super().__init__(records[0])
        self.records = records

    async def discover_current(self, _client: httpx.AsyncClient) -> Any:
        return snapshot_from_records(
            issuer=self.spec.code,
            source_url="https://cards.example/list",
            parser_version=self.parser_version,
            records=self.records,
            started_at=datetime(2026, 8, 29, tzinfo=UTC),
        )


class _ImmutableStore:
    def __init__(self, owner: _FakeCandidateWebDAV) -> None:
        self.owner = owner

    def publish_file(
        self,
        path: str | PurePosixPath,
        source: Path,
        *,
        media_type: str,
        expected_sha256: str,
        expected_size_bytes: int,
    ) -> ArtifactRef:
        body = source.read_bytes()
        assert hashlib.sha256(body).hexdigest() == expected_sha256
        assert len(body) == expected_size_bytes
        return self.publish_bytes(path, body, media_type=media_type)

    def publish_bytes(
        self,
        path: str | PurePosixPath,
        body: bytes,
        *,
        media_type: str,
    ) -> ArtifactRef:
        key = str(path)
        existing = self.owner.objects.get(key)
        if existing is not None and existing != body:
            raise RuntimeError("immutable fake WebDAV object conflict")
        self.owner.objects[key] = body
        return ArtifactRef(
            sha256=hashlib.sha256(body).hexdigest(),
            size_bytes=len(body),
            media_type=media_type,
            path=key,
        )


class _StableStore:
    def __init__(self, owner: _FakeCandidateWebDAV) -> None:
        self.owner = owner

    def atomic_replace_bytes(self, body: bytes) -> ArtifactRef:
        if self.owner.fail_pointer_once:
            self.owner.fail_pointer_once = False
            raise RuntimeError("injected candidate pointer failure")
        path = str(self.owner.pointer_path)
        self.owner.objects[path] = body
        return ArtifactRef(
            sha256=hashlib.sha256(body).hexdigest(),
            size_bytes=len(body),
            media_type="application/json",
            path=path,
        )


class _FakeCandidateWebDAV:
    channel = "candidate-v1.0.11"

    def __init__(self) -> None:
        self.pointer_path = channel_pointer_path(self.channel)
        self.objects: dict[str, bytes] = {}
        self.fail_pointer_once = True
        self.current: RemoteGenerationIdentity | None = None
        self.immutable = _ImmutableStore(self)
        self.stable = _StableStore(self)

    async def validated_current_generation(self) -> RemoteGenerationIdentity | None:
        return self.current

    async def get_bytes(
        self,
        path: str | PurePosixPath,
        *,
        max_bytes: int | None = None,
    ) -> bytes | None:
        body = self.objects.get(str(path))
        if body is not None and max_bytes is not None and len(body) > max_bytes:
            raise RuntimeError("fake WebDAV object exceeded its read cap")
        return body

    async def put_cas_file(
        self,
        path: Path,
        *,
        media_type: str = "application/octet-stream",
        expected_sha256: str | None = None,
        expected_size_bytes: int | None = None,
    ) -> tuple[str, str]:
        del media_type
        body = path.read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        assert expected_sha256 is None or digest == expected_sha256
        assert expected_size_bytes is None or len(body) == expected_size_bytes
        remote_path = object_path(digest).as_posix()
        existing = self.objects.get(remote_path)
        if existing is not None and existing != body:
            raise RuntimeError("fake CAS conflict")
        self.objects[remote_path] = body
        return digest, remote_path

    def install_other_candidate_head(self) -> RemoteGenerationIdentity:
        current = RemoteGenerationIdentity(
            generation_id="g-other-candidate-head",
            corpus_sha256="c" * 64,
            contract_sha256="d" * 64,
            generation_schema="cardrag.generation.v5",
            serving_schema="cardrag.serving-db.v5",
        )
        self.current = current
        self.objects[str(self.pointer_path)] = b'{"generation_id":"g-other-candidate-head"}'
        return current


def test_embedding_miss_batches_bind_count_and_exact_aggregate_token_caps() -> None:
    assert pipeline_module._embedding_miss_batches(  # noqa: SLF001
        (0, 1, 2, 3, 4),
        (4, 4, 3, 2, 1),
        maximum_tokens=8,
        maximum_batch_size=2,
    ) == ((0, 1), (2, 3), (4,))
    with pytest.raises(ValueError, match="exact batch token cap"):
        pipeline_module._embedding_miss_batches(  # noqa: SLF001
            (0,),
            (9,),
            maximum_tokens=8,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_mode", "expected_reason"),
    (("parser", "parser_failed"), ("views", "derived_view_failed")),
)
async def test_v5_structure_failure_uses_lossless_fallback_and_seals_its_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
    expected_reason: str,
) -> None:
    payload = [pdf_bytes()]
    pdf_requests: list[str] = []
    _install_pdf_http(monkeypatch, payload, pdf_requests)
    embedding_requests: list[dict[str, Any]] = []
    embeddings = _test_qwen_embeddings(embedding_requests)
    first = _test_source("test-001")
    second = _test_source("test-002")
    ocr = _OCR()
    webdav = _FakeCandidateWebDAV()

    real_parser = pipeline_module.parse_structure_artifact
    real_views = pipeline_module.build_derived_views
    if failure_mode == "parser":

        def injected_parser(*args: Any, **kwargs: Any) -> Any:
            if kwargs["product_code"] == first.product_code:
                raise StructureValidationError("injected parser failure")
            return real_parser(*args, **kwargs)

        monkeypatch.setattr(pipeline_module, "parse_structure_artifact", injected_parser)
    else:

        def injected_views(artifact: Any, **kwargs: Any) -> Any:
            is_neutral_fallback = all(
                node.node_type in {"ROOT", "ITEM", "UNCLASSIFIED"} for node in artifact.nodes
            )
            if artifact.product_code == first.product_code and not is_neutral_fallback:
                raise StructureValidationError(
                    "one structural leaf exceeds the view limit; injected overflow"
                )
            return real_views(artifact, **kwargs)

        monkeypatch.setattr(pipeline_module, "build_derived_views", injected_views)

    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[_MultiAdapter((first, second))],
            ocr=ocr,  # type: ignore[arg-type]
            embeddings=embeddings,
            webdav=webdav,  # type: ignore[arg-type]
            collect_remote_garbage=False,
            maximum_attempts=1,
            retry_cap_seconds=0,
        )
        with pytest.raises(WorkerUnexpectedFailureError) as failed_publish:
            await pipeline.run()
        run_id = failed_publish.value.run_id
        assert ocr.calls == 2
        assert len(embedding_requests) == 1

        first_document_id = first.document_id(hashlib.sha256(payload[0]).hexdigest())
        second_document_id = second.document_id(hashlib.sha256(payload[0]).hexdigest())
        structure_root = tmp_path / "runs" / run_id / "documents"
        first_structure_path = structure_root / first_document_id / "structure" / "structure.v2.json"
        second_structure_path = structure_root / second_document_id / "structure" / "structure.v2.json"
        first_structure = json.loads(first_structure_path.read_bytes())
        second_structure = json.loads(second_structure_path.read_bytes())
        assert {node["node_type"] for node in first_structure["nodes"]} == {
            "ROOT",
            "ITEM",
            "UNCLASSIFIED",
        }
        assert "MAJOR_SECTION" in {node["node_type"] for node in second_structure["nodes"]}
        pages_by_number = {page["page"]: page["text"] for page in first_structure["pages"]}
        leaf_spans = sorted(
            (
                span
                for node in first_structure["nodes"]
                if node["node_type"] == "UNCLASSIFIED"
                for span in node["spans"]
            ),
            key=lambda span: (span["page"], span["source_start"]),
        )
        reconstructed = "".join(
            pages_by_number[span["page"]][span["source_start"] : span["source_end"]] for span in leaf_spans
        )
        assert reconstructed == "".join(page["text"] for page in first_structure["pages"])
        assert all(span["is_canonical"] for span in leaf_spans)

        sealed_path = tmp_path / "runs" / run_id / "sealed" / "publish.json"
        sealed = json.loads(sealed_path.read_text(encoding="utf-8"))
        metrics = sealed["v5_metrics"]
        assert metrics["source_coverage_percent"] == 100.0
        assert metrics["structure_fallback_policy_version"] == ("cardrag.structure-unclassified-fallback.v1")
        assert metrics["structure_fallback_document_count"] == 1
        assert metrics["structure_fallback_documents"] == [
            {
                "contract_revision_id": first_structure["contract_revision_id"],
                "document_id": first_document_id,
                "reason_code": expected_reason,
                "structure_artifact_sha256": hashlib.sha256(first_structure_path.read_bytes()).hexdigest(),
            }
        ]
        assert metrics["structure_failed_document_count"] == 0
        assert metrics["structure_failed_documents_sha256"] == (
            pipeline_module._structure_failure_ledger_sha256(())  # noqa: SLF001
        )

        database_path = tmp_path / "runs" / run_id / "sealed" / "index.sqlite3"
        with sqlite3.connect(f"{database_path.as_uri()}?mode=ro&immutable=1", uri=True) as connection:
            metadata = dict(connection.execute("SELECT key,value FROM metadata"))
        assert metadata["structure_fallback_document_count"] == "1"
        assert (
            metadata["structure_fallback_documents_sha256"] == metrics["structure_fallback_documents_sha256"]
        )
        assert metadata["structure_failed_document_count"] == "0"
        assert metadata["structure_failed_documents_sha256"] == metrics["structure_failed_documents_sha256"]

        calls_before_resume = (ocr.calls, len(embedding_requests), len(pdf_requests))
        resumed = await pipeline.run(resume_run_id=run_id)
        assert resumed.status == "succeeded"
        assert (ocr.calls, len(embedding_requests), len(pdf_requests)) == calls_before_resume

        tampered = json.loads(sealed_path.read_text(encoding="utf-8"))
        tampered["v5_metrics"]["structure_failed_document_count"] = 1
        with pytest.raises(RuntimeError, match="structure_failed disposition"):
            await pipeline._validate_local_seal(tampered)  # noqa: SLF001


@pytest.mark.asyncio
async def test_v5_unavailable_fallback_continues_documents_then_blocks_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = [pdf_bytes()]
    pdf_requests: list[str] = []
    _install_pdf_http(monkeypatch, payload, pdf_requests)
    embedding_requests: list[dict[str, Any]] = []
    embeddings = _test_qwen_embeddings(embedding_requests)
    first = _test_source("test-001")
    second = _test_source("test-002")
    ocr = _OCR()
    webdav = _FakeCandidateWebDAV()
    real_parser = pipeline_module.parse_structure_artifact
    real_fallback = pipeline_module.build_unclassified_fallback_artifact

    def injected_parser(*args: Any, **kwargs: Any) -> Any:
        if kwargs["product_code"] == first.product_code:
            raise StructureValidationError("injected parser failure")
        return real_parser(*args, **kwargs)

    def injected_fallback(*args: Any, **kwargs: Any) -> Any:
        if kwargs["product_code"] == first.product_code:
            raise StructureValidationError("injected fallback failure")
        return real_fallback(*args, **kwargs)

    monkeypatch.setattr(pipeline_module, "parse_structure_artifact", injected_parser)
    monkeypatch.setattr(
        pipeline_module,
        "build_unclassified_fallback_artifact",
        injected_fallback,
    )

    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[_MultiAdapter((first, second))],
            ocr=ocr,  # type: ignore[arg-type]
            embeddings=embeddings,
            webdav=webdav,  # type: ignore[arg-type]
            collect_remote_garbage=False,
            maximum_attempts=1,
            retry_cap_seconds=0,
        )
        with pytest.raises(StructureDocumentFailuresError) as captured:
            await pipeline.run()
        error = captured.value
        assert len(error.failures) == 1
        assert error.failures[0].failure_stage == "parser"
        assert ocr.calls == 2
        assert embedding_requests == []
        assert len(pdf_requests) == 2

        first_document_id = first.document_id(hashlib.sha256(payload[0]).hexdigest())
        second_document_id = second.document_id(hashlib.sha256(payload[0]).hexdigest())
        run_root = tmp_path / "runs" / error.run_id
        assert (run_root / "documents" / first_document_id / "ocr" / "ocr.md").is_file()
        assert (run_root / "documents" / second_document_id / "structure" / "views.v1.json").is_file()
        assert not (run_root / "sealed" / "publish.json").exists()
        assert webdav.objects == {}

        report = json.loads(error.report_path.read_text(encoding="utf-8"))
        assert report["structure_failed_count"] == 1
        assert report["ledger_sha256"] == error.ledger_sha256
        failure_payload = report["ledger"]["documents"][0]
        assert failure_payload["disposition"] == "structure_failed"
        assert failure_payload["failure_code"] == "structure_fallback_failed"
        assert failure_payload["ocr_sha256"] == error.failures[0].ocr_sha256
        assert failure_payload["source_pages_sha256"] == error.failures[0].source_pages_sha256
        run_status = state.connection.execute(
            "SELECT status,error FROM run WHERE run_id=?", (error.run_id,)
        ).fetchone()
        assert tuple(run_status) == ("failed", error.stored_error)
        assert f"count=1; ledger_sha256={error.ledger_sha256}" in run_status[1]
        assert state.get_stage(error.run_id, first_document_id, "ocr").status == "succeeded"
        assert state.get_stage(error.run_id, first_document_id, "structure").status == "failed"
        assert state.get_stage(error.run_id, second_document_id, "views").status == "succeeded"


@pytest.mark.asyncio
async def test_v5_capacity_rejection_is_non_retryable_and_explicit_resume_restarts_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = [pdf_bytes()]
    pdf_requests: list[str] = []
    _install_pdf_http(monkeypatch, payload, pdf_requests)
    embedding_requests: list[dict[str, Any]] = []
    embeddings = _test_qwen_embeddings(embedding_requests)
    source = _test_source("capacity-resume")
    ocr = _OCR()
    webdav = _FakeCandidateWebDAV()
    webdav.fail_pointer_once = False
    real_preflight = pipeline_module.preflight_v5_capacity
    allow_capacity = False
    preflight_calls = 0
    export_calls = 0

    def gated_preflight(*args: Any, **kwargs: Any) -> Any:
        nonlocal preflight_calls
        preflight_calls += 1
        if not allow_capacity:
            raise V5CapacityError("injected capacity shortfall")
        return real_preflight(*args, **kwargs)

    monkeypatch.setattr(pipeline_module, "preflight_v5_capacity", gated_preflight)

    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[_Adapter(source)],
            ocr=ocr,  # type: ignore[arg-type]
            embeddings=embeddings,
            webdav=webdav,  # type: ignore[arg-type]
            collect_remote_garbage=False,
            maximum_attempts=3,
            retry_cap_seconds=0,
        )
        real_export = pipeline.exporter_v5.export

        def counted_export(*args: Any, **kwargs: Any) -> Any:
            nonlocal export_calls
            export_calls += 1
            return real_export(*args, **kwargs)

        monkeypatch.setattr(pipeline.exporter_v5, "export", counted_export)

        with pytest.raises(V5CapacityError, match="injected capacity shortfall"):
            await pipeline.run()

        run_id = str(state.connection.execute("SELECT run_id FROM run").fetchone()[0])
        stage = state.get_stage(run_id, "corpus-v5", "embedding-v5")
        assert stage is not None
        assert (stage.status, stage.attempt_count, stage.max_attempts) == ("failed", 1, 3)
        assert stage.last_error is not None
        assert "v5_capacity_preflight_failed" in stage.last_error
        assert embedding_requests == []
        assert export_calls == 0
        assert webdav.objects == {}
        assert state.connection.execute("SELECT count(*) FROM embedding_cache_v5").fetchone()[0] == 0
        assert not (tmp_path / "runs" / run_id / "sealed").exists()

        allow_capacity = True
        resumed = await pipeline.run(resume_run_id=run_id)

        assert resumed.status == "succeeded"
        assert resumed.v5_metrics is not None
        assert resumed.v5_metrics["embedding_provider_call_count"] == len(embedding_requests) == 1
        assert export_calls == 1
        assert preflight_calls >= 3  # rejected initial, resumed initial, resumed final.


@pytest.mark.asyncio
async def test_v5_permanent_embedding_http_status_is_non_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_pdf_http(monkeypatch, [pdf_bytes()], [])
    wire_calls = 0

    def reject(request: httpx.Request) -> httpx.Response:
        nonlocal wire_calls
        wire_calls += 1
        return httpx.Response(401, request=request)

    embeddings = _test_qwen_embeddings([])
    embeddings.transport = httpx.MockTransport(reject)
    webdav = _FakeCandidateWebDAV()
    webdav.fail_pointer_once = False

    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[_Adapter(_test_source("permanent-embedding"))],
            ocr=_OCR(),  # type: ignore[arg-type]
            embeddings=embeddings,
            webdav=webdav,  # type: ignore[arg-type]
            collect_remote_garbage=False,
            maximum_attempts=3,
            retry_cap_seconds=0,
        )

        with pytest.raises(WorkerUnexpectedFailureError):
            await pipeline.run()

        run_id = str(state.connection.execute("SELECT run_id FROM run").fetchone()[0])
        stage = state.get_stage(run_id, "corpus-v5", "embedding-v5")
        assert stage is not None
        assert (stage.status, stage.attempt_count, stage.max_attempts) == ("failed", 1, 3)
        assert stage.last_error is not None
        assert "v5_embedding_request_rejected" in stage.last_error
        assert "kind=authentication" in stage.last_error
        assert "status_code=401" in stage.last_error
        assert wire_calls == 1


@pytest.mark.asyncio
async def test_v5_pipeline_resume_overwrites_run_owned_partial_export_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = [pdf_bytes()]
    pdf_requests: list[str] = []
    _install_pdf_http(monkeypatch, payload, pdf_requests)
    embedding_requests: list[dict[str, Any]] = []
    embeddings = _test_qwen_embeddings(embedding_requests)
    source = _test_source("partial-export-resume")
    ocr = _OCR()
    webdav = _FakeCandidateWebDAV()
    webdav.fail_pointer_once = False

    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[_Adapter(source)],
            ocr=ocr,  # type: ignore[arg-type]
            embeddings=embeddings,
            webdav=webdav,  # type: ignore[arg-type]
            collect_remote_garbage=False,
            maximum_attempts=1,
            retry_cap_seconds=0,
        )
        real_export = pipeline.exporter_v5.export
        failed_once = False

        def fail_after_sidecar_install(
            database_target: Path,
            vectors_target: Path,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            nonlocal failed_once
            assert kwargs["replace_incomplete_owned_targets"] is True
            if not failed_once:
                failed_once = True
                vectors_target.parent.mkdir(parents=True, exist_ok=True)
                vectors_target.write_bytes(b"simulated-partial-sidecar")
                raise RuntimeError("injected process-death export window")
            return real_export(database_target, vectors_target, *args, **kwargs)

        monkeypatch.setattr(pipeline.exporter_v5, "export", fail_after_sidecar_install)

        with pytest.raises(WorkerUnexpectedFailureError) as failed:
            await pipeline.run()

        run_id = failed.value.run_id
        sealed = tmp_path / "runs" / run_id / "sealed"
        assert (sealed / "vectors.f32").read_bytes() == b"simulated-partial-sidecar"
        assert not (sealed / "index.sqlite3").exists()
        provider_calls = len(embedding_requests)

        resumed = await pipeline.run(resume_run_id=run_id)

        assert resumed.status == "succeeded"
        assert (sealed / "index.sqlite3").is_file()
        assert (sealed / "vectors.f32").stat().st_size == resumed.evidence_count * 4096 * 4
        assert len(embedding_requests) == provider_calls


def _install_pdf_http(
    monkeypatch: pytest.MonkeyPatch,
    payload: list[bytes],
    requests: list[str],
) -> None:
    real_async_client = httpx.AsyncClient

    def pdf_handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            200,
            headers={"content-type": "application/pdf"},
            content=payload[0],
            request=request,
        )

    pdf_transport = httpx.MockTransport(pdf_handler)

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        if kwargs.get("transport") is None:
            kwargs["transport"] = pdf_transport
        return real_async_client(**kwargs)

    monkeypatch.setattr(pipeline_module.httpx, "AsyncClient", client_factory)  # type: ignore[attr-defined]
    monkeypatch.setattr(
        pipeline_module,
        "SecurePDFDownloader",
        lambda policy: RealDownloader(policy, resolver=lambda _host: ("93.184.216.34",)),
    )


def _test_qwen_embeddings(
    requests: list[dict[str, Any]],
) -> OpenRouterQwenEmbeddingProviderV5:
    def embedding_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert isinstance(body, dict)
        requests.append(body)
        inputs = body["input"]
        vector = [1.0] + [0.0] * 4095
        return httpx.Response(
            200,
            json={
                "model": "qwen/qwen3-embedding-8b",
                "provider": "deepinfra",
                "data": [{"index": index, "embedding": vector} for index, _value in enumerate(inputs)],
            },
            request=request,
        )

    profile = QwenEmbeddingProfileV5.from_endpoint(
        OpenRouterEndpointMetadata(
            model="qwen/qwen3-embedding-8b",
            provider_id="deepinfra",
            provider_name="DeepInfra",
            endpoint_name="DeepInfra/Qwen3-Embedding-8B",
            quantization="BF16",
            maximum_tokens=32768,
            supported_parameters=("encoding_format",),
            metadata_sha256="b" * 64,
        )
    )
    return OpenRouterQwenEmbeddingProviderV5(
        api_key="test-only-key",
        profile=profile,
        token_counter=_PinnedFakeTokenCounter(),
        transport=httpx.MockTransport(embedding_handler),
    )


def test_v5_candidate_pipeline_rejects_remote_ocr_cache_writes(tmp_path: Path) -> None:
    ocr = _OCR()
    ocr.cache_mode = "read-write"
    with (
        WorkerState(tmp_path / "state.sqlite3") as state,
        pytest.raises(ValueError, match="requires read-only remote OCR cache"),
    ):
        WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[_Adapter(_test_source("test-001"))],
            ocr=ocr,  # type: ignore[arg-type]
            embeddings=_test_qwen_embeddings([]),
            webdav=_FakeCandidateWebDAV(),  # type: ignore[arg-type]
        )


def _test_source(product_code: str) -> SourceRecord:
    return SourceRecord(
        issuer="testbank",
        product_code=product_code,
        product_name=f"테스트 카드 {product_code}",
        effective_date=date(2026, 8, 1),
        source_version="1",
        source_url=f"https://cards.example/{product_code}.pdf",
        source_post_id=f"post-{product_code}",
        file_name=f"{product_code}.pdf",
        category="credit",
        discovered_at=datetime(2026, 8, 29, tzinfo=UTC),
    )


def _verified_aggregation_profile(
    *,
    embedding_profile_id: str,
    exact_row_corpus_sha256: str,
    generation_id: str,
    generation_manifest_sha256: str,
) -> VerifiedAggregationProfileV5:
    profile = DocumentAggregationProfile(
        schema_version="cardrag.document-aggregation-profile.v1",
        profile_id="cardrag.document-aggregation.max-child.v1",
        aggregation_policy="max_child",
        aggregation_definition=MaxChildAggregationDefinition(
            child_view_types=(
                "CONTEXTUAL_ITEM",
                "DETAIL",
                "MAJOR_SECTION",
                "RAW_ITEM",
                "TITLE",
            ),
            formula="max(non-CONTRACT row score)",
        ),
        bootstrap=DocumentAggregationBootstrap(
            ci=0.95,
            method="paired-query-percentile-pcg64",
            samples=2_000,
            seed=1010,
        ),
        embedding_profile_id=embedding_profile_id,
        exact_row_corpus_sha256=exact_row_corpus_sha256,
        generation_id=generation_id,
        generation_manifest_sha256=generation_manifest_sha256,
        gold_sha256="a" * 64,
        score_artifact_sha256="b" * 64,
        selection_objective="ndcg_at_10",
    )
    return VerifiedAggregationProfileV5(
        profile=profile,
        profile_sha256=profile.profile_sha256,
        artifact_sha256="c" * 64,
    )


@pytest.mark.asyncio
async def test_v5_pipeline_seals_publishes_resumes_and_reuses_profile_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = [pdf_bytes()]
    pdf_requests: list[str] = []
    _install_pdf_http(monkeypatch, payload, pdf_requests)
    embedding_requests: list[dict[str, Any]] = []

    def embedding_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert isinstance(body, dict)
        embedding_requests.append(body)
        inputs = body["input"]
        assert isinstance(inputs, list)
        assert len(inputs) == 1
        if len(embedding_requests) == 1:
            return httpx.Response(529, request=request)
        vector = [1.0] + [0.0] * 4095
        return httpx.Response(
            200,
            json={
                "model": "qwen/qwen3-embedding-8b",
                "provider": "deepinfra",
                "data": [{"index": index, "embedding": vector} for index, _value in enumerate(inputs)],
            },
            request=request,
        )

    profile = QwenEmbeddingProfileV5.from_endpoint(
        OpenRouterEndpointMetadata(
            model="qwen/qwen3-embedding-8b",
            provider_id="deepinfra",
            provider_name="DeepInfra",
            endpoint_name="DeepInfra/Qwen3-Embedding-8B",
            quantization="BF16",
            maximum_tokens=32768,
            supported_parameters=("encoding_format",),
            metadata_sha256="b" * 64,
        )
    )
    embeddings = OpenRouterQwenEmbeddingProviderV5(
        api_key="test-only-key",
        profile=profile,
        token_counter=_PinnedFakeTokenCounter(),
        retry_base_seconds=0,
        retry_cap_seconds=0,
        transport=httpx.MockTransport(embedding_handler),
    )
    source = SourceRecord(
        issuer="testbank",
        product_code="test-001",
        product_name="테스트 카드",
        effective_date=date(2026, 8, 1),
        source_version="1",
        source_url="https://cards.example/current.pdf",
        source_post_id="post-test-001",
        file_name="current.pdf",
        category="credit",
        discovered_at=datetime(2026, 8, 29, tzinfo=UTC),
    )
    second_source = replace(
        source,
        product_code="test-002",
        source_url="https://cards.example/second.pdf",
        source_post_id="post-test-002",
        file_name="second.pdf",
    )
    ocr = _OCR()
    webdav = _FakeCandidateWebDAV()

    original_build_derived_views = pipeline_module.build_derived_views

    def build_two_duplicate_inputs(*args: Any, **kwargs: Any) -> tuple[Any, ...]:
        generated = original_build_derived_views(*args, **kwargs)
        selected = next(view for view in generated if view.view_type == "TITLE")
        provisional = replace(selected, view_id="", ordinal=0)
        return (
            replace(
                provisional,
                view_id="view_" + canonical_sha256(provisional.identity_payload),
            ),
        )

    monkeypatch.setattr(pipeline_module, "build_derived_views", build_two_duplicate_inputs)

    with WorkerState(tmp_path / "state.sqlite3") as state:
        pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[_MultiAdapter((source, second_source))],
            ocr=ocr,  # type: ignore[arg-type]
            embeddings=embeddings,
            webdav=webdav,  # type: ignore[arg-type]
            collect_remote_garbage=False,
            maximum_attempts=1,
            retry_cap_seconds=0,
        )
        original_contract_sha256 = pipeline.contract_sha256
        ocr.cache_mode = "read-write"
        assert pipeline.contract_sha256 != original_contract_sha256
        ocr.cache_mode = "read-only"
        assert pipeline.contract_sha256 == original_contract_sha256
        original_context_policy = pipeline_module.contextual_item_policy_payload
        with monkeypatch.context() as context_policy_patch:
            context_policy_patch.setattr(
                pipeline_module,
                "contextual_item_policy_payload",
                lambda: {
                    **original_context_policy(),
                    "schema_version": "cardrag.contextual-item-context.changed-for-test",
                },
            )
            assert pipeline.contract_sha256 != original_contract_sha256
        original_fallback_policy = pipeline_module.unclassified_fallback_policy_payload
        with monkeypatch.context() as fallback_policy_patch:
            fallback_policy_patch.setattr(
                pipeline_module,
                "unclassified_fallback_policy_payload",
                lambda: {
                    **original_fallback_policy(),
                    "schema_version": "cardrag.structure-unclassified-fallback.changed-for-test",
                },
            )
            assert pipeline.contract_sha256 != original_contract_sha256
        with monkeypatch.context() as contract_patch:
            contract_patch.setattr(
                pipeline_module,
                "REVISION_HISTORY_POLICY_VERSION",
                "cardrag.revision-history.changed-for-test",
            )
            assert pipeline.contract_sha256 != original_contract_sha256
        vector_representations: list[tuple[type[object], ...]] = []
        real_export = pipeline.exporter_v5.export

        def record_export(*args: Any, **kwargs: Any) -> Any:
            views = kwargs["embedding_views"]
            vector_representations.append(tuple(type(view.vector) for view in views))
            assert len(views) in {2, 4}
            assert all(isinstance(view.vector, LazyEmbeddingVector) for view in views)
            return real_export(*args, **kwargs)

        monkeypatch.setattr(pipeline.exporter_v5, "export", record_export)

        with pytest.raises(WorkerUnexpectedFailureError) as failed:
            await pipeline.run()
        run_id = failed.value.run_id
        api_calls_after_seal = len(embedding_requests)
        assert api_calls_after_seal == 2
        assert vector_representations and all(
            vector_type is LazyEmbeddingVector
            for invocation in vector_representations
            for vector_type in invocation
        )
        assert ocr.calls == 2
        assert (tmp_path / "runs" / run_id / "sealed" / "publish.json").is_file()
        document_id = source.document_id(hashlib.sha256(payload[0]).hexdigest())
        structure_checkpoint = (
            tmp_path / "runs" / run_id / "documents" / document_id / "structure" / "structure.v2.json"
        )
        views_checkpoint = structure_checkpoint.with_name("views.v1.json")
        structure_payload = json.loads(structure_checkpoint.read_bytes())
        views_payload = json.loads(views_checkpoint.read_bytes())
        assert structure_payload["product_name"] == source.product_name
        assert structure_payload["source_version"] == source.source_version
        assert structure_payload["effective_date"] == source.effective_date.isoformat()
        assert len(views_payload["views"]) == 1
        assert views_payload["views"][0]["view_type"] == "TITLE"
        assert views_payload["views"][0]["context"] == []

        resumed = await pipeline.run(resume_run_id=run_id)
        assert resumed.status == "succeeded"
        assert resumed.generation_id is not None
        assert resumed.evidence_count == 2
        assert resumed.v5_metrics is not None
        metrics = resumed.v5_metrics
        assert metrics["schema_version"] == "cardrag.worker-v5-metrics.v3"
        assert metrics["source_coverage_percent"] == 100.0
        assert metrics["contract_revision_count"] == 2
        assert metrics["current_revision_count"] == 2
        assert metrics["superseded_revision_count"] == 0
        assert metrics["historical_revision_unresolved_count"] == 0
        assert metrics["historical_revision_unresolved_identities"] == []
        assert metrics["revision_history_policy_version"] == REVISION_HISTORY_POLICY_VERSION
        assert metrics["historical_revision_unresolved_sha256"] == (unresolved_revision_ledger_sha256_v5(()))
        assert metrics["embedding_provider_call_count"] == 2
        assert metrics["embedding_dimension"] == 4096
        assert sum(row["downloads"] for row in metrics["embedding_view_counts"].values()) == 1
        assert len(embedding_requests) == api_calls_after_seal
        assert ocr.calls == 2
        assert resumed.pdf_cache_hits == 2
        assert resumed.pdf_downloads == 0

        sealed_path = tmp_path / "runs" / run_id / "sealed" / "publish.json"
        tampered_seal = json.loads(sealed_path.read_text(encoding="utf-8"))
        tampered_seal["v5_metrics"]["historical_revision_unresolved_sha256"] = "f" * 64
        with pytest.raises(RuntimeError, match="unresolved revision ledger is inconsistent"):
            await pipeline._validate_local_seal(tampered_seal)  # noqa: SLF001

        for request_body in embedding_requests:
            assert request_body["model"] == "qwen/qwen3-embedding-8b"
            assert request_body["dimensions"] == 4096
            assert request_body["encoding_format"] == "float"
            assert request_body["provider"] == {
                "order": ["deepinfra"],
                "only": ["deepinfra"],
                "allow_fallbacks": False,
                "require_parameters": False,
            }
            assert "truncate" not in request_body

        generation_id = resumed.generation_id
        manifest_path = generation_manifest_path(generation_id).as_posix()
        ready_path = generation_ready_path(generation_id).as_posix()
        database_path = generation_database_path(generation_id).as_posix()
        vectors_path = generation_vectors_path(generation_id).as_posix()
        assert {manifest_path, ready_path, database_path, vectors_path} <= webdav.objects.keys()
        manifest = GenerationManifest.model_validate_json(webdav.objects[manifest_path])
        ready = GenerationReady.model_validate_json(webdav.objects[ready_path])
        assert manifest.schema_version == "cardrag.generation.v5"
        assert manifest.serving_schema == "cardrag.serving-db.v5"
        assert manifest.embedding_contract.dimension == 4096
        assert manifest.vector_sidecar is not None
        assert manifest.vector_sidecar.row_count == resumed.evidence_count
        assert manifest.vector_sidecar.artifact.size_bytes == resumed.evidence_count * 4096 * 4
        assert hashlib.sha256(webdav.objects[vectors_path]).hexdigest() == (
            manifest.vector_sidecar.artifact.sha256
        )
        assert ready.vector_sidecar_sha256 == manifest.vector_sidecar.artifact.sha256
        assert ready.vector_sidecar_size_bytes == manifest.vector_sidecar.artifact.size_bytes
        assert all(not path.startswith("v1/ocr-cache/") for path in webdav.objects)
        assert all(
            document.ocr is None or document.ocr.path in webdav.objects for document in manifest.documents
        )
        assert state.connection.execute("SELECT count(*) FROM embedding_cache").fetchone()[0] == 0
        assert state.connection.execute("SELECT count(*) FROM embedding_cache_v5").fetchone()[0] == (1)

        local_database = tmp_path / "runs" / run_id / "sealed" / "index.sqlite3"
        with sqlite3.connect(f"{local_database.as_uri()}?mode=ro&immutable=1", uri=True) as connection:
            metadata = dict(connection.execute("SELECT key,value FROM metadata"))
            assert metadata["schema_id"] == "cardrag.serving-db.v5"
            assert metadata["embedding_dimension"] == "4096"
            assert metadata["vector_sidecar_sha256"] == manifest.vector_sidecar.artifact.sha256
            assert metadata["revision_history_policy_version"] == REVISION_HISTORY_POLICY_VERSION
            assert metadata["historical_revision_unresolved_count"] == "0"
            assert metadata["historical_revision_unresolved_sha256"] == (
                unresolved_revision_ledger_sha256_v5(())
            )
            assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert connection.execute("PRAGMA foreign_key_check").fetchone() is None

        # A different valid candidate head requires a newly chained seal, but
        # the same profile/input must come exclusively from cache_v5.
        other_head = webdav.install_other_candidate_head()
        fresh = await pipeline.run()
        assert fresh.status == "succeeded"
        assert fresh.generation_id is not None and fresh.generation_id != generation_id
        assert fresh.pdf_cache_hits == 2
        assert fresh.pdf_downloads == 0
        assert ocr.calls == 4
        assert len(embedding_requests) == api_calls_after_seal
        assert fresh.v5_metrics is not None
        assert fresh.v5_metrics["embedding_provider_call_count"] == 0
        assert (
            sum(row["hits"] for row in fresh.v5_metrics["embedding_view_counts"].values())
            == fresh.evidence_count
        )
        fresh_manifest = GenerationManifest.model_validate_json(
            webdav.objects[generation_manifest_path(fresh.generation_id).as_posix()]
        )
        assert fresh_manifest.previous_generation_id == other_head.generation_id

        # The same stable source URL now serves different PDF bytes. Expire
        # only its cache freshness proof, then require the v5 build to retain
        # the old immutable revision and link the newly observed current one.
        payload[0] = pdf_bytes(width=613)
        expired = datetime(2020, 1, 1, tzinfo=UTC).isoformat()
        state.connection.execute(
            """UPDATE pdf_cache_source_revision
               SET last_observed_at=?,verified_at=? WHERE superseded_at IS NULL""",
            (expired, expired),
        )
        revised = await pipeline.run()
        assert revised.status == "succeeded"
        assert revised.generation_id is not None
        assert revised.v5_metrics is not None
        assert revised.v5_metrics["contract_revision_count"] == 4
        assert revised.v5_metrics["current_revision_count"] == 2
        assert revised.v5_metrics["superseded_revision_count"] == 2
        assert revised.v5_metrics["ambiguous_revision_count"] == 0
        assert revised.v5_metrics["historical_pdf_cache_hits"] == 2
        assert revised.v5_metrics["historical_revision_unresolved_count"] == 0
        published_run = state.connection.execute(
            "SELECT run_id FROM publish WHERE generation_id=?",
            (revised.generation_id,),
        ).fetchone()
        assert published_run is not None
        revised_database = tmp_path / "runs" / str(published_run[0]) / "sealed" / "index.sqlite3"
        with sqlite3.connect(revised_database) as connection:
            revisions = connection.execute(
                """SELECT temporal_status,supersedes_revision_id
                   FROM contract_revisions ORDER BY temporal_status"""
            ).fetchall()
        assert sorted(row[0] for row in revisions) == [
            "current",
            "current",
            "superseded",
            "superseded",
        ]
        assert sum(row[1] is not None for row in revisions) == 2

    assert pdf_requests == [
        source.source_url,
        second_source.source_url,
        source.source_url,
        second_source.source_url,
    ]


@pytest.mark.asyncio
async def test_v5_pipeline_promotes_verified_m0_profile_into_sealed_m1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = [pdf_bytes()]
    pdf_requests: list[str] = []
    _install_pdf_http(monkeypatch, payload, pdf_requests)
    embedding_requests: list[dict[str, Any]] = []
    embeddings = _test_qwen_embeddings(embedding_requests)
    source = _test_source("test-m1")
    ocr = _OCR()
    webdav = _FakeCandidateWebDAV()
    webdav.fail_pointer_once = False

    with WorkerState(tmp_path / "state.sqlite3") as state:
        m0_pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[_Adapter(source)],
            ocr=ocr,  # type: ignore[arg-type]
            embeddings=embeddings,
            webdav=webdav,  # type: ignore[arg-type]
            collect_remote_garbage=False,
            maximum_attempts=1,
            retry_cap_seconds=0,
        )
        m0_contract_sha256 = m0_pipeline.contract_sha256
        m0 = await m0_pipeline.run()
        assert m0.status == "succeeded"
        assert m0.generation_id is not None
        m0_manifest = GenerationManifest.model_validate_json(
            webdav.objects[generation_manifest_path(m0.generation_id).as_posix()]
        )
        assert m0_manifest.document_aggregation_profile is None
        assert m0_manifest.document_aggregation_policy is None
        assert m0_manifest.sealed_profile_sha256 is None
        assert m0_manifest.exact_row_corpus_sha256 is None
        m0_run = state.connection.execute(
            "SELECT run_id FROM publish WHERE generation_id=?",
            (m0.generation_id,),
        ).fetchone()
        assert m0_run is not None
        m0_database = tmp_path / "runs" / str(m0_run[0]) / "sealed" / "index.sqlite3"
        with sqlite3.connect(m0_database) as connection:
            m0_metadata = dict(connection.execute("SELECT key,value FROM metadata"))
        assert m0_metadata["document_aggregation_status"] == "candidate_default"
        assert m0_metadata["document_aggregation_policy"] == "max_child"
        assert "sealed_profile_sha256" not in m0_metadata
        exact_row_corpus_sha256 = m0_metadata["exact_row_corpus_sha256"]

        selected = _verified_aggregation_profile(
            embedding_profile_id=embeddings.profile.profile_id,
            exact_row_corpus_sha256=exact_row_corpus_sha256,
            generation_id=m0_manifest.generation_id,
            generation_manifest_sha256=m0_manifest.manifest_sha256,
        )
        profile = selected.profile
        provider_calls_after_m0 = len(embedding_requests)
        with pytest.raises(RuntimeError, match="valid remote M0/M1 head"):
            await pipeline_module.validate_document_aggregation_head(webdav, selected)

        current_m0 = RemoteGenerationIdentity(
            generation_id=m0_manifest.generation_id,
            corpus_sha256=m0_manifest.corpus_sha256,
            contract_sha256=m0_manifest.contract_sha256,
            generation_schema="cardrag.generation.v5",
            serving_schema="cardrag.serving-db.v5",
        )
        webdav.current = current_m0
        stale = _verified_aggregation_profile(
            embedding_profile_id=embeddings.profile.profile_id,
            exact_row_corpus_sha256=exact_row_corpus_sha256,
            generation_id="g-stale-evaluation",
            generation_manifest_sha256="d" * 64,
        )
        with pytest.raises(RuntimeError, match="evaluated M0"):
            await pipeline_module.validate_document_aggregation_head(webdav, stale)
        webdav.current = replace(current_m0, corpus_sha256="e" * 64)
        with pytest.raises(RuntimeError, match="head identity is inconsistent"):
            await pipeline_module.validate_document_aggregation_head(webdav, selected)
        webdav.current = current_m0
        assert len(embedding_requests) == provider_calls_after_m0

        m1_pipeline = WorkerPipeline(
            state=state,
            state_dir=tmp_path,
            adapters=[_Adapter(source)],
            ocr=ocr,  # type: ignore[arg-type]
            embeddings=embeddings,
            webdav=webdav,  # type: ignore[arg-type]
            collect_remote_garbage=False,
            maximum_attempts=1,
            retry_cap_seconds=0,
            document_aggregation=selected,
        )
        assert m1_pipeline.contract_sha256 != m0_contract_sha256

        m1 = await m1_pipeline.run()

        assert m1.status == "succeeded"
        assert m1.generation_id is not None and m1.generation_id != m0.generation_id
        assert len(embedding_requests) == provider_calls_after_m0
        m1_manifest = GenerationManifest.model_validate_json(
            webdav.objects[generation_manifest_path(m1.generation_id).as_posix()]
        )
        assert m1_manifest.previous_generation_id == m0.generation_id
        assert m1_manifest.document_aggregation_profile == profile
        assert m1_manifest.document_aggregation_policy == "max_child"
        assert m1_manifest.sealed_profile_sha256 == profile.profile_sha256
        assert m1_manifest.exact_row_corpus_sha256 == exact_row_corpus_sha256
        assert m1_manifest.retrieval_policy_sha256 == canonical_sha256(m1_pipeline.v5_retrieval_policy)
        m1_run = state.connection.execute(
            "SELECT run_id FROM publish WHERE generation_id=?",
            (m1.generation_id,),
        ).fetchone()
        assert m1_run is not None
        m1_database = tmp_path / "runs" / str(m1_run[0]) / "sealed" / "index.sqlite3"
        with sqlite3.connect(m1_database) as connection:
            m1_metadata = dict(connection.execute("SELECT key,value FROM metadata"))
        assert m1_metadata["document_aggregation_status"] == "sealed"
        assert m1_metadata["document_aggregation_policy"] == "max_child"
        assert m1_metadata["sealed_profile_sha256"] == profile.profile_sha256
        assert m1_metadata["exact_row_corpus_sha256"] == exact_row_corpus_sha256
        assert m1_metadata["aggregation_profile_artifact_sha256"] == "c" * 64
        webdav.current = RemoteGenerationIdentity(
            generation_id=m1_manifest.generation_id,
            corpus_sha256=m1_manifest.corpus_sha256,
            contract_sha256=m1_manifest.contract_sha256,
            generation_schema="cardrag.generation.v5",
            serving_schema="cardrag.serving-db.v5",
        )
        assert await m1_pipeline._validated_document_aggregation_head() == m1_manifest  # noqa: SLF001

    assert pdf_requests == [source.source_url]
