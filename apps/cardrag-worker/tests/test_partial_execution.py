from __future__ import annotations

import json
import time
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
from test_pipeline import pdf_bytes
from test_pipeline_v5 import _OCR, _Adapter, _FakeCandidateWebDAV, _install_pdf_http, _PinnedFakeTokenCounter

from cardrag_worker.contracts import SourceRecord
from cardrag_worker.embedding_v5 import (
    OpenRouterEndpointMetadata,
    OpenRouterQwenEmbeddingProviderV5,
    QwenEmbeddingProfileV5,
)
from cardrag_worker.partial_execution import ExecutionPlan, LocalWebDAV, PartialExecutionError, ReuseSource
from cardrag_worker.pipeline import WorkerPipeline
from cardrag_worker.state import WorkerState


@pytest.fixture
async def corpus(tmp_path, monkeypatch):
    pdf_calls = []
    _install_pdf_http(monkeypatch, [pdf_bytes()], pdf_calls)
    embed_calls = []

    def handler(request):
        body = json.loads(request.content)
        embed_calls.append(body)
        return httpx.Response(
            200,
            json={
                "model": "qwen/qwen3-embedding-8b",
                "provider": "deepinfra",
                "data": [{"index": i, "embedding": [1.0] + [0.0] * 4095} for i in range(len(body["input"]))],
            },
            request=request,
        )

    profile = QwenEmbeddingProfileV5.from_endpoint(
        OpenRouterEndpointMetadata(
            model="qwen/qwen3-embedding-8b",
            provider_id="deepinfra",
            provider_name="DeepInfra",
            endpoint_name="test",
            quantization="BF16",
            maximum_tokens=32768,
            supported_parameters=("encoding_format",),
            metadata_sha256="b" * 64,
        )
    )
    embeddings = OpenRouterQwenEmbeddingProviderV5(
        api_key="fixture",
        profile=profile,
        token_counter=_PinnedFakeTokenCounter(),
        transport=httpx.MockTransport(handler),
    )
    adapter = _Adapter(
        SourceRecord(
            issuer="testbank",
            product_code="test-001",
            product_name="테스트 카드",
            effective_date=date(2026, 8, 1),
            source_version="1",
            source_url="https://cards.example/current.pdf",
            source_post_id="post-test-001",
            file_name="current.pdf",
            category="credit",
            discovered_at=datetime.now(UTC),
        )
    )
    ocr = _OCR()
    dav = _FakeCandidateWebDAV()
    dav.fail_pointer_once = False
    with WorkerState(tmp_path / "state.sqlite3") as state:

        def pipeline(plan=None, source=None, local=False):
            return WorkerPipeline(
                state=state,
                state_dir=tmp_path,
                adapters=[adapter],
                ocr=ocr,
                embeddings=embeddings,
                webdav=LocalWebDAV() if local else dav,
                maximum_attempts=1,
                retry_cap_seconds=0,
                execution_plan=plan,
                reuse_source=source,
            )

        started = time.monotonic()
        original = await pipeline().run()
        baseline_seconds = time.monotonic() - started
        source = ReuseSource(tmp_path, original.run_id, state.path)
        source.baseline_seconds = baseline_seconds
        source.baseline_calls = {"pdf": len(pdf_calls), "ocr": ocr.calls, "embedding": len(embed_calls)}
        yield pipeline, source, state, pdf_calls, embed_calls, ocr, dav


@pytest.mark.parametrize(
    "skipped",
    [
        {"pdf"},
        {"ocr"},
        {"embedding"},
        {"pdf", "ocr"},
        {"pdf", "ocr", "embedding"},
        {"pdf", "ocr", "structure", "embedding"},
    ],
)
async def test_individual_and_combined_skips_execute_downstream(corpus, skipped):
    pipeline, source, state, pdf_calls, embed_calls, ocr, dav = corpus
    plan = ExecutionPlan(frozenset(skipped | {"webdav"}), source.run_id)
    before = (len(pdf_calls), len(embed_calls), ocr.calls, dict(dav.objects))
    result = await pipeline(plan, source, local=True).run()
    assert result.status == "local_only" and not result.published
    assert Path(result.local_artifacts, "publish.json").is_file()
    assert dav.objects == before[3]
    if "pdf" in skipped:
        assert len(pdf_calls) == before[0]
    if "ocr" in skipped:
        assert ocr.calls == before[2]
    if "embedding" in skipped:
        assert len(embed_calls) == before[1]
    assert state.run_status(source.run_id) == "succeeded"
    assert state.run_status(result.run_id) == "interrupted"
    assert (
        state.connection.execute("SELECT 1 FROM publish WHERE run_id=?", (result.run_id,)).fetchone() is None
    )
    assert result.execution["skipped_stages"] == [
        s for s in ("pdf", "ocr", "structure", "embedding", "export", "webdav") if s in plan.skipped
    ]


async def test_reuse_export_no_rebuild(corpus, monkeypatch):
    pipeline, source, state, *_ = corpus
    plan = ExecutionPlan(
        frozenset({"pdf", "ocr", "structure", "embedding", "export", "webdav"}), source.run_id
    )
    runner = pipeline(plan, source, local=True)
    monkeypatch.setattr(runner.exporter_v5, "export", lambda *_a, **_k: pytest.fail("export must be reused"))
    result = await runner.run()
    assert result.generation_id == source.manifest.generation_id and not result.published


async def test_corrupted_source_ocr_is_refused_without_provider_call(corpus):
    pipeline, source, state, pdf_calls, embed_calls, ocr, dav = corpus
    document = next(iter(source.documents.values()))
    source.object_path(document.ocr.sha256, "ocr").write_text("corrupt")
    source.verified.clear()
    before = ocr.calls
    with pytest.raises(PartialExecutionError, match="skip_artifact_incompatible"):
        source.preflight(ExecutionPlan(frozenset({"ocr"}), source.run_id, dry_run=True))
    assert ocr.calls == before


async def test_embedding_miss_refuses_hidden_paid_fallback(corpus):
    pipeline, source, state, pdf_calls, embed_calls, ocr, dav = corpus
    state.connection.execute("DELETE FROM embedding_cache_v5")
    before = len(embed_calls)
    with pytest.raises(PartialExecutionError):
        await pipeline(
            ExecutionPlan(frozenset({"pdf", "ocr", "embedding", "webdav"}), source.run_id), source, local=True
        ).run()
    assert len(embed_calls) == before


def test_plan_validation_and_source_path_traversal():
    with pytest.raises(ValueError, match="unknown"):
        ExecutionPlan(frozenset({"bogus"}), "source")
    with pytest.raises(ValueError, match="requires"):
        ExecutionPlan(frozenset({"pdf"}))
    with pytest.raises(ValueError, match="invalid"):
        ExecutionPlan(frozenset({"pdf"}), "../other")


async def test_local_resume_reuses_seal_without_upstream_calls(corpus):
    pipeline, source, state, pdf_calls, embed_calls, ocr, dav = corpus
    skipped = frozenset({"pdf", "ocr", "embedding", "webdav"})
    first = await pipeline(ExecutionPlan(skipped, source.run_id), source, local=True).run()
    before = (len(pdf_calls), len(embed_calls), ocr.calls, dict(dav.objects))
    resumed = await pipeline(ExecutionPlan(skipped, source.run_id), source, local=True).run(
        resume_run_id=first.run_id
    )
    assert resumed.status == "local_only" and resumed.generation_id == first.generation_id
    assert (len(pdf_calls), len(embed_calls), ocr.calls, dict(dav.objects)) == before
    assert state.run_status(source.run_id) == "succeeded"


@pytest.mark.parametrize("stage", ["structure", "export", "webdav"])
async def test_additional_individual_stages(corpus, stage, monkeypatch):
    pipeline, source, state, *_ = corpus
    plan = ExecutionPlan(frozenset({stage, "webdav"}), source.run_id)
    runner = pipeline(plan, source, local=True)
    if stage == "export":
        monkeypatch.setattr(runner.exporter_v5, "export", lambda *_a, **_k: pytest.fail("export called"))
    result = await runner.run()
    assert result.status == "local_only" and not result.published
    assert state.run_status(source.run_id) == "succeeded"


async def test_preflight_has_no_side_effects_and_checks_embedding_cache(corpus):
    pipeline, source, state, pdf_calls, embed_calls, ocr, dav = corpus
    before = (state.connection.total_changes, len(pdf_calls), len(embed_calls), ocr.calls, dict(dav.objects))
    plan = ExecutionPlan(frozenset({"pdf", "ocr", "embedding", "webdav"}), source.run_id, dry_run=True)
    payload = source.preflight(plan)
    assert set(payload["expected_external_calls"].values()) == {"zero"}
    assert (
        state.connection.total_changes,
        len(pdf_calls),
        len(embed_calls),
        ocr.calls,
        dict(dav.objects),
    ) == before
    state.connection.execute("DELETE FROM embedding_cache_v5")
    with pytest.raises(PartialExecutionError, match="skip_artifact_missing"):
        source.preflight(plan)


async def test_changed_structure_rebuilds_and_changed_views_refuse_embedding_skip(corpus, monkeypatch):
    import hashlib
    from dataclasses import replace

    import cardrag_worker.structure as structure
    from cardrag_worker.embedding_v5 import format_embedding_input

    pipeline, source, state, pdf_calls, embed_calls, ocr, dav = corpus
    original = structure.build_derived_views
    calls = []

    def changed(*args, **kwargs):
        calls.append(True)
        return tuple(
            replace(
                v,
                embedding_input=v.embedding_input + " changed",
                input_sha256=hashlib.sha256(
                    format_embedding_input("document", v.embedding_input + " changed").encode()
                ).hexdigest(),
            )
            for v in original(*args, **kwargs)
        )

    monkeypatch.setattr(structure, "build_derived_views", changed)
    before = (len(pdf_calls), len(embed_calls), ocr.calls)
    # Exact input hashes are checked before embedding or publication.
    with pytest.raises(PartialExecutionError, match="skip_artifact_missing"):
        await pipeline(
            ExecutionPlan(frozenset({"pdf", "ocr", "embedding", "webdav"}), source.run_id), source, local=True
        ).run()
    assert calls and (len(pdf_calls), len(embed_calls), ocr.calls) == before


async def test_resume_plan_change_is_refused(corpus):
    pipeline, source, state, *_ = corpus
    first = await pipeline(
        ExecutionPlan(frozenset({"pdf", "ocr", "webdav"}), source.run_id), source, local=True
    ).run()
    with pytest.raises(PartialExecutionError, match="skip_artifact_incompatible"):
        await pipeline(ExecutionPlan(frozenset({"pdf", "webdav"}), source.run_id), source, local=True).run(
            resume_run_id=first.run_id
        )
    assert state.run_status(first.run_id) == "interrupted"


async def test_reuse_semantics_and_measured_calls(corpus):
    pipeline, source, state, pdf_calls, embed_calls, ocr, dav = corpus
    before = (len(pdf_calls), ocr.calls, len(embed_calls))
    result = await pipeline(
        ExecutionPlan(frozenset({"pdf", "ocr", "embedding", "webdav"}), source.run_id), source, local=True
    ).run()
    candidate = ReuseSource(source.root, result.run_id, state.path)
    assert set(source.revisions) == set(candidate.revisions)
    for doc_id in source.documents:
        assert (
            candidate.ocr(doc_id, source.documents[doc_id].pdf.sha256).ocr_sha256
            == source.documents[doc_id].ocr.sha256
        )
        with source.connect() as a, candidate.connect() as b:
            sql = "SELECT node_id,node_type,display_text FROM structure_nodes ORDER BY node_id"
            assert [tuple(r) for r in a.execute(sql)] == [tuple(r) for r in b.execute(sql)]
            sql = "SELECT node_id,view_type,input_sha256,profile_id FROM embedding_views ORDER BY row_index"
            assert [tuple(r) for r in a.execute(sql)] == [tuple(r) for r in b.execute(sql)]
    metrics = {
        "synthetic_documents": 1,
        "baseline_seconds": source.baseline_seconds,
        "partial_seconds": result.execution["elapsed_seconds"],
        "baseline_calls": source.baseline_calls,
        "partial_calls": {
            "pdf": len(pdf_calls) - before[0],
            "ocr": ocr.calls - before[1],
            "embedding": len(embed_calls) - before[2],
        },
        "partial_execution": result.execution,
    }
    (source.root / "benchmark.json").write_text(json.dumps(metrics, indent=2))
    assert set(metrics["partial_calls"].values()) == {0}


async def test_source_retention_and_baseline_preserved(corpus, monkeypatch):
    pipeline, source, state, *_ = corpus
    root = source.root
    before = {
        str(p.relative_to(root)): p.read_bytes()
        for directory in ("corpus-baselines", "retirement-ledgers")
        for p in (root / directory).rglob("*.json")
    }
    result = await pipeline(
        ExecutionPlan(frozenset({"pdf", "ocr", "webdav"}), source.run_id), source, local=True
    ).run()
    after = {
        str(p.relative_to(root)): p.read_bytes()
        for directory in ("corpus-baselines", "retirement-ledgers")
        for p in (root / directory).rglob("*.json")
    }
    assert after == before
    normal = pipeline()
    monkeypatch.setattr(state, "retained_publication_run_ids", lambda **_: ())
    normal._cleanup_local_runs(exclude_run_id=result.run_id)
    assert source.directory.is_dir()


async def test_cli_dry_run_never_initializes_providers_or_writer(corpus, monkeypatch):
    from types import SimpleNamespace

    from cardrag_worker import cli, settings
    from cardrag_worker import state as state_module
    from cardrag_worker.partial_cli import run_partial

    _, source, state, pdf_calls, embed_calls, ocr, dav = corpus
    config = SimpleNamespace(
        lock_file=source.root / "worker.lock",
        state_dir=source.root,
        state_database=state.path,
        embedding_provider_id=source.profile["provider_id"],
        embedding_model=source.profile["model"],
        embedding_dimension=source.profile["dimension"],
        embedding_maximum_tokens=source.profile["maximum_tokens"],
    )
    monkeypatch.setattr(settings.WorkerSettings, "from_env", lambda **_: config)

    def forbidden(*args, **kwargs):
        pytest.fail("dry-run must not initialize providers or a writable state connection")

    monkeypatch.setattr(cli, "_provider", forbidden)
    monkeypatch.setattr(cli, "_qwen_embedding_provider", forbidden)
    monkeypatch.setattr(state_module, "WorkerState", forbidden)
    before = state.connection.total_changes
    result = await run_partial(
        ExecutionPlan(frozenset({"pdf", "ocr", "embedding", "webdav"}), source.run_id, dry_run=True), None
    )
    assert result["status"] == "dry_run" and not result["state_changed"]
    assert state.connection.total_changes == before


async def test_cli_local_reuse_runs_without_ocr_or_embedding_credentials(corpus, monkeypatch):
    from dataclasses import fields
    from types import SimpleNamespace

    from cardrag_worker import cli, issuers, settings, tokenizer_v5
    from cardrag_worker.partial_cli import run_partial

    pipeline, source, state, pdf_calls, embed_calls, ocr, dav = corpus
    configured = settings.WorkerSettings.from_env(require_providers=False, require_webdav=False)
    values = {f.name: getattr(configured, f.name) for f in fields(configured)}
    values.update(
        state_dir=source.root,
        state_database=state.path,
        lock_file=source.root / "worker.lock",
        embedding_provider_id=source.profile["provider_id"],
        embedding_model=source.profile["model"],
        embedding_dimension=source.profile["dimension"],
        embedding_maximum_tokens=source.profile["maximum_tokens"],
        minimum_start_free_bytes=0,
        reserved_free_space_bytes=0,
    )
    monkeypatch.setattr(settings.WorkerSettings, "from_env", lambda **_: SimpleNamespace(**values))
    adapters = pipeline().adapters
    monkeypatch.setattr(issuers, "enabled_adapters", lambda: adapters)
    monkeypatch.setattr(tokenizer_v5.QwenTokenizerV5, "from_file", lambda _: _PinnedFakeTokenCounter())

    def forbidden(*args, **kwargs):
        pytest.fail("explicit provider skip must not initialize a credentialed provider")

    monkeypatch.setattr(cli, "_provider", forbidden)
    monkeypatch.setattr(cli, "_qwen_embedding_provider", forbidden)
    state.close()
    result = await run_partial(
        ExecutionPlan(frozenset({"pdf", "ocr", "embedding", "webdav"}), source.run_id), None
    )
    assert result["status"] == "local_only" and not result["published"]


async def test_legacy_publication_resume_cannot_bypass_local_plan(corpus):
    from cardrag_worker.pipeline import resume_sealed_publication

    pipeline, source, state, _, _, _, dav = corpus
    local = await pipeline(
        ExecutionPlan(frozenset({"pdf", "ocr", "webdav"}), source.run_id), source, local=True
    ).run()
    before = dict(dav.objects)
    with pytest.raises(PartialExecutionError, match="skip_artifact_incompatible"):
        await resume_sealed_publication(run_id=local.run_id, state_dir=source.root, webdav=dav)
    assert dav.objects == before and state.run_status(local.run_id) == "interrupted"


@pytest.mark.parametrize("skip_ocr", [True, False])
async def test_pending_ocr_request_cannot_be_falsely_completed(corpus, skip_ocr):
    from cardrag_worker.ocr_requests import OCRReprocessRequest, OCRRequestTarget, queue_reprocess_requests

    pipeline, source, state, _, _, ocr, dav = corpus
    document = next(iter(source.documents.values()))
    request = OCRReprocessRequest(
        request_id="ocr-test-pending",
        source_generation_id=source.manifest.generation_id,
        created_at=datetime.now(UTC),
        targets=(
            OCRRequestTarget(
                document_id=document.document_id,
                pdf_sha256=document.pdf.sha256,
                pdf_size_bytes=document.pdf.size_bytes,
                page_count=document.page_count,
            ),
        ),
    )
    queue_reprocess_requests(source.root, (request,))
    skipped = {"pdf", "webdav"} | ({"ocr"} if skip_ocr else set())
    before = ocr.calls
    # The fake resolver has no durable content variant, so it cannot acknowledge a reprocess either.
    with pytest.raises(PartialExecutionError, match="skip_artifact_incompatible"):
        await pipeline(ExecutionPlan(frozenset(skipped), source.run_id), source, local=True).run()
    if skip_ocr:
        assert ocr.calls == before
    assert not (source.root / "ocr-requests" / "completed" / (request.request_id + ".json")).exists()


def stable_source_runner(corpus):
    from cardrag_core import channel_pointer_path

    from cardrag_worker.webdav import RemoteGenerationIdentity

    pipeline, source, _, _, _, _, dav = corpus
    runner = pipeline(
        ExecutionPlan(frozenset({"pdf", "ocr", "embedding"}), source.run_id, channel="stable"), source
    )
    dav.channel = "stable"
    dav.pointer_path = channel_pointer_path("stable")
    dav.stable_publication_approved = True
    runner.stable_publication_approved = True
    dav.current = RemoteGenerationIdentity(
        generation_id=source.manifest.generation_id,
        corpus_sha256=source.manifest.corpus_sha256,
        contract_sha256=source.manifest.contract_sha256,
        generation_schema=source.manifest.schema_version,
        serving_schema=source.manifest.serving_schema,
    )
    return runner, dav


@pytest.mark.parametrize("boundary", ["export", "upload"])
async def test_frozen_stable_source_change_is_fenced(corpus, monkeypatch, boundary):
    runner, dav = stable_source_runner(corpus)
    if boundary == "export":
        original = runner.exporter_v5.export

        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            dav.install_other_candidate_head()
            return result

        monkeypatch.setattr(runner.exporter_v5, "export", changed)
    else:
        original_upload = dav.put_cas_file

        async def changed_upload(*args, **kwargs):
            result = await original_upload(*args, **kwargs)
            dav.install_other_candidate_head()
            return result

        monkeypatch.setattr(dav, "put_cas_file", changed_upload)
    with pytest.raises(PartialExecutionError, match="skip_source_unavailable"):
        await runner.run()
    assert dav.current.generation_id == "g-other-candidate-head"
    assert json.loads(dav.objects[str(dav.pointer_path)])["generation_id"] == "g-other-candidate-head"


async def test_frozen_stable_matching_source_publishes(corpus):
    runner, dav = stable_source_runner(corpus)
    result = await runner.run()
    assert result.status == "succeeded"
    assert json.loads(dav.objects[str(dav.pointer_path)])["generation_id"] == result.generation_id


@pytest.fixture
async def failed_corpus(corpus):
    from dataclasses import replace

    from test_pipeline_v5 import _MultiAdapter

    from cardrag_worker.ocr import OCRValidationError

    pipeline, source, state, pdf_calls, embed_calls, _, dav = corpus
    original_runner = pipeline()
    base = original_runner.adapters[0].record
    records = (base,) + tuple(
        replace(
            base, product_code=f"test-{i:03}", source_post_id=f"post-{i}", product_name=f"테스트 카드 {i}"
        )
        for i in range(2, 21)
    )

    class RecoverableOCR(_OCR):
        def __init__(self):
            super().__init__()
            self.failed_ids = {records[-1].document_id(next(iter(source.documents.values())).pdf.sha256)}
            self.attempted = []
            self.systemic = False

        async def resolve(self, **kwargs):
            self.attempted.append(kwargs["document_id"])
            if kwargs["document_id"] in self.failed_ids:
                if self.systemic:
                    raise ValueError("fixture configuration failure")
                raise OCRValidationError("fixture document failure")
            return await super().resolve(**kwargs)

    ocr = RecoverableOCR()
    original_runner.adapters = [_MultiAdapter(records)]
    original_runner.ocr = ocr
    dav.objects.pop(str(dav.pointer_path), None)
    initial = await original_runner.run()
    failed_source = ReuseSource(source.root, initial.run_id, state.path)
    assert len(failed_source.failed) == 1
    assert failed_source.manifest.issuer_ocr_counts[0].succeeded == 19

    def factory(skipped):
        runner = pipeline(
            ExecutionPlan(frozenset(skipped | {"webdav"}), failed_source.run_id), failed_source, local=True
        )
        runner.ocr = ocr
        return runner

    yield factory, failed_source, ocr, pdf_calls


@pytest.mark.parametrize("skip_ocr", [False, True])
async def test_failed_frozen_document_recovers_only_when_ocr_selected(failed_corpus, skip_ocr):
    factory, source, ocr, pdf_calls = failed_corpus
    before = len(pdf_calls), len(ocr.attempted)
    ocr.failed_ids.clear()
    result = await factory({"pdf", "ocr"} if skip_ocr else {"pdf"}).run()
    assert result.status == "local_only"
    seal = json.loads(Path(result.local_artifacts, "publish.json").read_text())
    count = seal["manifest"]["issuer_ocr_counts"][0]
    assert count == {
        "issuer": "testbank",
        "acquired": 20,
        "succeeded": 19 if skip_ocr else 20,
        "failed": 1 if skip_ocr else 0,
    }
    assert len(pdf_calls) == before[0]
    if skip_ocr:
        assert len(ocr.attempted) == before[1]
    else:
        assert source.failed[0]["document_id"] in ocr.attempted[before[1] :]
        assert result.document_count == 20


async def test_failed_frozen_document_retries_and_keeps_bounded_failure(failed_corpus):
    factory, _, ocr, _ = failed_corpus
    runner = factory({"pdf"})
    runner.maximum_attempts = 2
    before = len(ocr.attempted)
    result = await runner.run()
    assert result.status == "local_only"
    failure_id = next(iter(ocr.failed_ids))
    assert ocr.attempted[before:].count(failure_id) == 2
    report = json.loads((runner.state_dir / "runs" / result.run_id / "reports/ocr-failures.json").read_text())
    assert report["failures"][0]["attempts"] == 2


async def test_failed_replay_below_95_percent_cannot_export(failed_corpus):
    from cardrag_worker.pipeline import OCRDocumentFailuresError

    factory, source, ocr, _ = failed_corpus
    ocr.failed_ids.add(next(iter(source.revisions)))
    with pytest.raises(OCRDocumentFailuresError):
        await factory({"pdf"}).run()


async def test_failed_replay_missing_identity_blocks_without_inference(failed_corpus, monkeypatch):
    from cardrag_worker import pipeline as module

    factory, source, ocr, _ = failed_corpus
    failed_id = source.failed[0]["document_id"]
    source.documents = {failed_id: source.documents[failed_id]}
    monkeypatch.setattr(module, "_known_snapshot_sources", lambda *args, **kwargs: {})
    before = len(ocr.attempted)
    with pytest.raises(PartialExecutionError, match="skip_source_unavailable"):
        await factory({"pdf"}).run()
    assert len(ocr.attempted) == before


async def test_exact_committed_partial_stable_resume_is_idempotent(corpus):
    from dataclasses import replace

    runner, dav = stable_source_runner(corpus)
    result = await runner.run()
    dav.current = replace(dav.current, generation_id=result.generation_id)
    runner.state.finish_run(result.run_id, "interrupted", error="fixture lost local receipt")
    before = dict(dav.objects)
    resumed = await runner.run(resume_run_id=result.run_id)
    assert resumed.status == "succeeded"
    assert dav.objects == before


async def test_failed_frozen_systemic_error_is_not_retried(failed_corpus):
    from cardrag_worker.pipeline import WorkerUnexpectedFailureError

    factory, _, ocr, _ = failed_corpus
    ocr.systemic = True
    before = len(ocr.attempted)
    runner = factory({"pdf"})
    runner.maximum_attempts = 3
    with pytest.raises(WorkerUnexpectedFailureError):
        await runner.run()
    assert ocr.attempted[before:].count(next(iter(ocr.failed_ids))) == 1


async def test_failed_target_reprocess_requires_durable_variant(failed_corpus):
    from cardrag_worker.ocr_requests import OCRReprocessRequest, OCRRequestTarget, queue_reprocess_requests

    factory, source, ocr, _ = failed_corpus
    document = source.documents[source.failed[0]["document_id"]]
    request = OCRReprocessRequest(
        request_id="ocr-failed-target",
        source_generation_id=source.manifest.generation_id,
        created_at=datetime.now(UTC),
        targets=(
            OCRRequestTarget(
                document_id=document.document_id,
                pdf_sha256=document.pdf.sha256,
                pdf_size_bytes=document.pdf.size_bytes,
                page_count=document.page_count,
            ),
        ),
    )
    queue_reprocess_requests(source.root, (request,))
    ocr.failed_ids.clear()
    with pytest.raises(PartialExecutionError, match="skip_artifact_incompatible"):
        await factory({"pdf"}).run()
    assert document.document_id in ocr.attempted
    assert not (source.root / "ocr-requests/completed/ocr-failed-target.json").exists()


async def test_failed_historical_replay_is_never_isolated(failed_corpus):
    from cardrag_worker.pipeline import OCRDocumentFailuresError

    factory, source, ocr, _ = failed_corpus
    ocr.failed_ids.clear()
    historical_id = next(iter(source.revisions))
    source.revisions[historical_id] = {**source.revisions[historical_id], "temporal_status": "superseded"}
    ocr.failed_ids.add(historical_id)
    with pytest.raises(OCRDocumentFailuresError):
        await factory({"pdf"}).run()
