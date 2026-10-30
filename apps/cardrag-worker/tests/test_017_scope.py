from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from test_pipeline_v5 import _Adapter

from cardrag_worker.contracts import SourceRecord
from cardrag_worker.issuer_collection import IssuerCollectionError
from cardrag_worker.issuer_http import PROXY_HOSTS, create_issuer_client, validate_issuer_proxy_url
from cardrag_worker.issuer_metrics import IssuerHTTPMetrics
from cardrag_worker.rate_limit import IssuerRateLimiter, RateLimitedClient
from cardrag_worker.rotation import Rotation
from cardrag_worker.state import WorkerState

pytest_plugins = ["test_partial_execution"]


async def test_proxy_exact_origins_and_no_ambient_proxy(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://ambient.invalid:1234")
    async with create_issuer_client(proxy_url="http://proxy.invalid:1234", hyundai_legacy_tls=True) as client:
        for host in PROXY_HOSTS:
            route = client._transport_for_url(httpx.URL(f"https://{host}/"))
            assert route is not client._transport
            for wrong in (f"http://{host}/", f"https://sub.{host}/", f"https://{host}.example/"):
                assert client._transport_for_url(httpx.URL(wrong)) is client._transport
            with pytest.raises(httpx.InvalidURL, match="restricted"):
                await client.get(f"https://{host}:8443/")
        for host in ("m.lottecard.co.kr", "openrouter.ai", "webdav.example", "www.kbcard.com"):
            assert client._transport_for_url(httpx.URL(f"https://{host}/")) is client._transport
    for value in (
        "socks5://secret:password@proxy.invalid",
        "http://proxy.invalid/path",
        "http://proxy.invalid:bad",
    ):
        with pytest.raises(ValueError) as exc:
            validate_issuer_proxy_url(value)
        assert "secret" not in str(exc.value) and "password" not in str(exc.value)


def test_rotation_kst_retries_failures_and_roster(tmp_path):
    with WorkerState(tmp_path / "state.sqlite3") as state:
        rotation = Rotation(state, channel="stable")
        start = datetime(2026, 10, 18, 15, tzinfo=UTC)  # KST October19
        assert rotation.claim(("a", "b", "c"), now=start).scope.selected == ("a",)
        assert rotation.claim(("a", "b", "c"), now=start + timedelta(hours=3)).scope.selected == ("a",)
        assert rotation.claim(("a", "b", "c", "d"), now=start + timedelta(days=1)).scope.selected == ("b",)
        # Missed timer dates consume no extra slots, new members enter next cycle.
        assert rotation.claim(("a", "b", "c", "d"), now=start + timedelta(days=10)).scope.selected == ("c",)
        assert rotation.claim(("a", "b", "c", "d"), now=start + timedelta(days=11)).scope.selected == ("a",)


async def test_http_stream_bytes_status_budget_and_persistent_cooldown(tmp_path):
    with WorkerState(tmp_path / "state.sqlite3") as state:
        metrics = IssuerHTTPMetrics(state, "run", request_limit=2, byte_limit=100)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    429, headers={"retry-after": "3600"}, content=b"protected-body"
                )
            ),
            event_hooks={"request": [metrics.before], "response": [metrics.after]},
        ) as client:
            limited = RateLimitedClient(client, IssuerRateLimiter(0), issuer="lotte", purpose="pdf")
            async with limited.stream("GET", "https://example.com/pdf") as response:
                assert await response.aread() == b"protected-body"
            with pytest.raises(IssuerCollectionError, match="cooldown"):
                await limited.get("https://example.com/pdf")
        values = {r["metric"]: r["value"] for r in metrics.report()}
        assert values == {"requests": 1, "status_429": 1, "body_bytes": 14}
        restarted = IssuerHTTPMetrics(state, "run")
        assert restarted.report() == metrics.report()


@pytest.mark.parametrize("issuer_count", [2, 8])
async def test_selected_issuer_preserves_prior_rows_without_processing(corpus, monkeypatch, issuer_count):  # noqa: F811
    factory, prior, state, pdf_calls, embed_calls, ocr, dav = corpus
    from cardrag_worker.webdav import RemoteGenerationIdentity

    def activate(source):
        m = source.manifest
        dav.current = RemoteGenerationIdentity(
            m.generation_id, m.corpus_sha256, m.contract_sha256, m.schema_version, m.serving_schema
        )

    activate(prior)
    runner = factory()
    base = runner.adapters[0].record
    second = _Adapter(
        replace(
            base,
            issuer="otherbank",
            product_code="other",
            source_post_id="other",
            source_url="https://cards.example/other.pdf",
        )
    )
    second.spec = replace(second.spec, code="otherbank", sort_order=2)
    others = [second]
    for number in range(2, issuer_count):
        extra = _Adapter(
            replace(
                base,
                issuer=f"bank{number}",
                product_code=f"other{number}",
                source_post_id=f"other{number}",
                source_url=f"https://cards.example/other{number}.pdf",
            )
        )
        extra.spec = replace(extra.spec, code=f"bank{number}", sort_order=number + 1)
        others.append(extra)
    from cardrag_worker.pipeline import WorkerPipeline

    def build(selected):
        return WorkerPipeline(
            state=state,
            state_dir=prior.root,
            adapters=[factory().adapters[0], *others],
            ocr=ocr,
            embeddings=runner.embeddings,
            webdav=dav,
            selected_issuers=selected,
            maximum_attempts=1,
            retry_cap_seconds=0,
        )

    runner = build(("testbank", *(adapter.spec.code for adapter in others)))
    full = await runner.run()
    from cardrag_worker.partial_execution import ReuseSource

    baseline = ReuseSource(prior.root, full.run_id, state.path)
    assert len({d.issuer for d in baseline.manifest.documents}) == issuer_count
    activate(baseline)
    runner = build(("testbank",))
    # Force a new selected source so the full-corpus no-change gate cannot hide work.
    runner.adapters[0].record = replace(base, source_version="2", source_post_id="new")

    async def forbidden(*args, **kwargs):
        raise AssertionError("nonselected adapter was invoked")

    for adapter in others:
        adapter.discover_current = forbidden
        adapter.prepare_download = forbidden
    original_resolve = ocr.resolve
    attempted = []

    async def resolve(**kwargs):
        attempted.append(kwargs["document_id"])
        return await original_resolve(**kwargs)

    monkeypatch.setattr(ocr, "resolve", resolve)
    before = len(pdf_calls)
    result = await runner.run()
    assert result.status == "succeeded"
    output = ReuseSource(prior.root, result.run_id, state.path)
    old_other = [d for d in baseline.manifest.documents if d.issuer != "testbank"]
    new_other = [d for d in output.manifest.documents if d.issuer != "testbank"]
    assert old_other == new_other
    assert not {d.document_id for d in old_other} & set(attempted)
    assert all("other.pdf" not in url for url in pdf_calls[before:])


async def test_collect_then_process_has_separate_side_effects(corpus):  # noqa: F811
    from cardrag_worker.acquisition import AcquisitionSource
    from cardrag_worker.partial_execution import ExecutionPlan, ReuseSource
    from cardrag_worker.webdav import RemoteGenerationIdentity

    factory, baseline, state, pdf_calls, embed_calls, ocr, dav = corpus
    m = baseline.manifest
    dav.current = RemoteGenerationIdentity(
        m.generation_id, m.corpus_sha256, m.contract_sha256, m.schema_version, m.serving_schema
    )
    before = (ocr.calls, len(embed_calls), dict(dav.objects))
    collected = await factory(ExecutionPlan(phase="collect")).run()
    assert collected.status == "collected" and collected.published is False
    assert (ocr.calls, len(embed_calls), dav.objects) == before
    source = AcquisitionSource(baseline.root, collected.run_id, state.path)
    source.preflight(ExecutionPlan(frozenset({"pdf"}), collected.run_id, phase="process"))
    before_pdf = len(pdf_calls)
    result = await factory(ExecutionPlan(frozenset({"pdf"}), collected.run_id, phase="process"), source).run()
    assert result.status == "succeeded"
    assert len(pdf_calls) == before_pdf
    published = ReuseSource(baseline.root, result.run_id, state.path)
    assert {d.document_id for d in published.manifest.documents} == set(baseline.documents)


def test_audit_migration_is_durable_spread_and_never_refreshes_origin(tmp_path):
    from types import SimpleNamespace

    from cardrag_worker.origin_policy import OriginPolicy

    now = datetime(2026, 10, 19, tzinfo=UTC)
    with WorkerState(tmp_path / "state.sqlite3") as state:
        policy = OriginPolicy(state, interval=timedelta(days=90), visit_days=8)
        deadlines = []
        for i in range(100):
            identity = SimpleNamespace(source_id=f"source-{i}")
            cached = SimpleNamespace(origin_checked_at=now - timedelta(days=7))
            policy.due(identity, cached, now=now)
            row = state.connection.execute(
                "SELECT * FROM origin_audit WHERE source_id=?", (identity.source_id,)
            ).fetchone()
            assert row[1] == cached.origin_checked_at.isoformat()
            due = datetime.fromisoformat(row[2])
            assert now <= due <= cached.origin_checked_at + timedelta(days=90)
            policy.due(identity, cached, now=now + timedelta(days=1))
            assert (
                state.connection.execute(
                    "SELECT next_audit_at FROM origin_audit WHERE source_id=?", (identity.source_id,)
                ).fetchone()[0]
                == row[2]
            )
            deadlines.append(due)
        assert (max(deadlines) - min(deadlines)).days >= 75
        policy.remember_negative(
            "drm", {"magic": "SCDSA002", "sha256": "a" * 64, "size_bytes": 10}, now=now, known=True
        )
        assert policy.negative("drm", now=now + timedelta(days=89)) is not None
        assert policy.negative("changed", now=now) is None
        assert policy.negative("drm", now=now + timedelta(days=91)) is None
        assert policy.report(now=now)["overdue"] == 0


def test_display_rename_reuses_exact_attachment_without_resetting_origin(tmp_path):
    from test_pipeline import pdf_bytes

    from cardrag_worker.pdf_cache import PDFCache, PDFSourceIdentity

    now = datetime(2026, 10, 1, tzinfo=UTC)
    record = SourceRecord(
        "testbank",
        "one",
        "Old",
        date(2026, 1, 1),
        "v1",
        "https://example.com/one.pdf",
        "post",
        "one.pdf",
        "credit",
        now,
    )
    path = tmp_path / "input.pdf"
    path.write_bytes(pdf_bytes())
    with WorkerState(tmp_path / "state.sqlite3") as state:
        cache = PDFCache(tmp_path, state)
        original = cache.ingest_and_bind(
            PDFSourceIdentity.from_source_record(record), path, final_url=record.source_url, observed_at=now
        )
        renamed = cache.reuse_display_update(replace(record, product_name="New"), record)
        assert renamed.sha256 == original.sha256 and renamed.origin_checked_at == now
        assert cache.reuse_display_update(replace(record, source_version="v2"), record) is None


async def test_rotation_repeats_same_issuer_and_manual_run_does_not_consume_slot(tmp_path):
    from cardrag_worker.rotation import scope_session

    with WorkerState(tmp_path / "state.sqlite3") as state:
        options = {"selected": None, "mode": "round_robin", "channel": "stable", "resume": None}
        pool = tuple(f"issuer-{i}" for i in range(8))
        async with scope_session(state, tmp_path, pool, **options) as (assignment, bind):
            first = assignment.scope.selected
            bind(state.start_run())
            state.finish_run(assignment.run_id, "succeeded")
            assignment.result = {"status": "succeeded", "run_id": assignment.run_id, "evidence": "saved"}
        async with scope_session(state, tmp_path, pool, **options) as (assignment, bind):
            assert assignment.scope.selected == first
            assert assignment.result is None
            assert assignment.attempt == 2
            history = state.connection.execute(
                "SELECT result_json FROM issuer_rotation_attempt WHERE attempt=1"
            ).fetchone()
            assert json.loads(history[0])["evidence"] == "saved"
        before = [tuple(row) for row in state.connection.execute("SELECT * FROM issuer_rotation")]
        async with scope_session(
            state, tmp_path, pool, selected=(pool[4],), mode="selected", channel="stable", resume=None
        ) as (assignment, bind):
            assert bind is None and assignment.scope.selected == (pool[4],)
        assert before == [tuple(row) for row in state.connection.execute("SELECT * FROM issuer_rotation")]


async def test_rotation_interruption_keeps_original_run_and_scope(tmp_path):
    import asyncio

    from cardrag_worker.rotation import scope_session

    with WorkerState(tmp_path / "state.sqlite3") as state:
        options = {"selected": None, "mode": "round_robin", "channel": "stable", "resume": None}
        with pytest.raises(asyncio.CancelledError):
            async with scope_session(state, tmp_path, ("a", "b"), **options) as (assignment, bind):
                run_id = state.start_run()
                bind(run_id)
                state.ensure_stage(run_id, "doc", "download", max_attempts=4)
                state.stage_started(run_id, "doc", "download")
                raise asyncio.CancelledError()
        async with scope_session(state, tmp_path, ("a", "b"), **options) as (assignment, bind):
            assert assignment.run_id == run_id and assignment.scope.selected == ("a",)
            assert state.get_stage(run_id, "doc", "download").attempt_count == 1


async def test_hyundai_inventory_reuses_details_and_discovers_rolling_revision(tmp_path):
    from test_issuers_hyundai_hana import HYUNDAI_HTML, attachment, hyundai_payload

    from cardrag_worker.detail_inventory import DetailInventory
    from cardrag_worker.issuers.hyundai import HyundaiAdapter

    calls = []

    def handler(request):
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, text=HYUNDAI_HTML)
        revised = calls.count("POST") > 1
        return httpx.Response(
            200, json=hyundai_payload(attachment("new.pdf", "20261001") if revised else attachment())
        )

    with WorkerState(tmp_path / "state.sqlite3") as state:
        adapter = HyundaiAdapter(minimum_records=1)
        adapter.detail_inventory = DetailInventory(state)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            first = await adapter.discover_current(client)
            second = await adapter.discover_current(client)
            assert first.snapshot_id == second.snapshot_id
            assert calls == ["GET", "POST", "GET"]
            state.connection.execute(
                "UPDATE issuer_detail_inventory SET next_detail_at='2000-01-01T00:00:00+00:00'"
            )
            third = await adapter.discover_current(client)
            assert third.records[0].source_id != first.records[0].source_id
            assert calls == ["GET", "POST", "GET", "GET", "POST"]


async def test_shinhan_literal_prefix_handles_plus_name_without_full_scan():
    from test_issuers import Response, shinhan_mobile_payload

    from cardrag_worker.issuers import shinhan

    name = "제주은행 신한카드 Discount Plan+"
    record = SourceRecord(
        "shinhan",
        "P1",
        name,
        date(2026, 8, 25),
        "20260825",
        shinhan.BASE_URL + shinhan.DOWNLOAD_PATH,
        "credit:P1",
        name + ".pdf",
        "credit",
        datetime.now(UTC),
        {shinhan.DOWNLOAD_NAME_METADATA_KEY: name},
    )
    searches = []

    class Client:
        async def get(self, *args, **kwargs):
            return Response(text="landing")

        async def post(self, url, **kwargs):
            search = kwargs["data"]["crdPdGuiNm"]
            searches.append(search)
            return Response(
                payload=shinhan_mobile_payload(
                    product_name=name, query=search, tokens=("CURRENT-TOKEN",) if search == "제주은행" else ()
                )
            )

    original = record.source_id
    request = await shinhan.ShinhanAdapter().prepare_download(Client(), record)
    assert searches == [name, "제주은행"]
    assert record.source_id == original and "CURRENT-TOKEN" in request.url


def test_scoped_ocr_queue_partitions_parent_without_consuming_other_issuer(tmp_path):
    from test_ocr_requests import _stable_manifest

    from cardrag_worker.ocr_requests import (
        complete_reprocess_request,
        load_next_reprocess_request,
        partition_reprocess_requests,
        plan_reprocess_requests,
        queue_reprocess_requests,
    )

    manifest = _stable_manifest()
    available = next(doc for doc in manifest.documents if doc.ocr is not None)
    manifest = manifest.model_copy(
        update={
            "documents": (
                available,
                available.model_copy(update={"document_id": "doc_other", "issuer": "shinhan"}),
            )
        }
    )
    request = plan_reprocess_requests(manifest, all_documents=True, confirm_all=True, max_documents=100)[0]
    queue_reprocess_requests(tmp_path, (request,))
    issuers = {doc.document_id: doc.issuer for doc in manifest.documents}
    partition_reprocess_requests(tmp_path, issuers)
    selected = manifest.documents[0].issuer
    eligible = frozenset(doc_id for doc_id, issuer in issuers.items() if issuer == selected)
    child = load_next_reprocess_request(tmp_path, eligible_document_ids=eligible)
    assert child is not None and child.request_id != request.request_id
    assert {t.document_id for t in child.targets} <= eligible
    assert (
        tmp_path / "ocr-requests" / f"{request.request_id}.json"
    ).read_bytes() == request.canonical_bytes()
    complete_reprocess_request(tmp_path, child, run_id="run-one", generation_id="generation-one")
    assert load_next_reprocess_request(tmp_path, eligible_document_ids=eligible) is None
    assert not (tmp_path / "ocr-requests/delegated-completed" / f"{request.request_id}.json").exists()
    other = load_next_reprocess_request(tmp_path)
    assert other is not None and other.request_id != child.request_id
    complete_reprocess_request(tmp_path, other, run_id="run-two", generation_id="generation-two")
    assert load_next_reprocess_request(tmp_path) is None
    assert (tmp_path / "ocr-requests/delegated-completed" / f"{request.request_id}.json").is_file()


async def test_rotation_reconciles_committed_seal_without_provider_calls(corpus):  # noqa: F811
    from cardrag_worker.rotation import scope_session
    from cardrag_worker.webdav import RemoteGenerationIdentity

    factory, source, state, pdf_calls, embed_calls, ocr, dav = corpus
    manifest = source.manifest
    dav.current = RemoteGenerationIdentity(
        manifest.generation_id,
        manifest.corpus_sha256,
        manifest.contract_sha256,
        manifest.schema_version,
        manifest.serving_schema,
    )
    rotation = Rotation(state, channel=dav.channel)
    assignment = rotation.claim(("testbank",))
    rotation.bind(assignment, source.run_id)
    state.connection.execute(
        "UPDATE run SET status='running',finished_at=NULL WHERE run_id=?", (source.run_id,)
    )
    before = (len(pdf_calls), len(embed_calls), ocr.calls, dict(dav.objects))
    async with scope_session(
        state,
        source.root,
        ("testbank",),
        selected=None,
        mode="round_robin",
        channel=dav.channel,
        resume=None,
        transport=dav,
    ) as (recovered, bind):
        assert recovered.result["status"] == "succeeded" and recovered.result["rotation_reconciled"]
        assert recovered.run_id == source.run_id
    assert (len(pdf_calls), len(embed_calls), ocr.calls, dav.objects) == before
    assert state.run_status(source.run_id) == "succeeded"


async def test_rotation_never_overwrites_newer_manual_pointer(corpus):  # noqa: F811
    from cardrag_worker.pipeline import WorkerUnexpectedFailureError
    from cardrag_worker.rotation import scope_session

    factory, source, state, pdf_calls, embed_calls, ocr, dav = corpus
    rotation = Rotation(state, channel=dav.channel)
    assignment = rotation.claim(("testbank",))
    rotation.bind(assignment, source.run_id)
    state.connection.execute(
        "UPDATE run SET status='running',finished_at=NULL WHERE run_id=?", (source.run_id,)
    )
    dav.install_other_candidate_head()
    pointer = dav.objects[str(dav.pointer_path)]
    before = (len(pdf_calls), len(embed_calls), ocr.calls)
    with pytest.raises(WorkerUnexpectedFailureError):
        async with scope_session(
            state,
            source.root,
            ("testbank",),
            selected=None,
            mode="round_robin",
            channel=dav.channel,
            resume=None,
            transport=dav,
        ):
            raise AssertionError("conflicting publication should be blocked before providers")
    assert dav.objects[str(dav.pointer_path)] == pointer
    assert (len(pdf_calls), len(embed_calls), ocr.calls) == before


async def test_selected_backup_ignores_global_cache_and_preserves_unrelated_pending(tmp_path):
    import hashlib
    from types import SimpleNamespace

    from cardrag_core import object_path

    from cardrag_worker.backup import BackupLedger

    settings = SimpleNamespace(backup_mode="hybrid", state_dir=tmp_path, webdav_base_url=None)
    objects, docs = [], []
    for issuer in ("shinhan", "lotte"):
        body = issuer.encode()
        digest = hashlib.sha256(body).hexdigest()
        path = tmp_path / f"{issuer}.pdf"
        path.write_bytes(body)
        objects.append(
            {"sha256": digest, "size_bytes": len(body), "media_type": "application/pdf", "path": str(path)}
        )
        docs.append(
            {"issuer": issuer, "document_id": f"doc_{issuer}", "pdf": {"sha256": digest}, "ocr": None}
        )
    seal = tmp_path / "runs/run/sealed/publish.json"
    seal.parent.mkdir(parents=True)
    seal.write_text(json.dumps({"manifest": {"documents": docs}, "objects": objects}))
    unrelated = tmp_path / "cache/ocr/key/unrelated.json"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text('{"unrelated":true}')
    ledger = BackupLedger(tmp_path / "backup.sqlite3")
    ledger.record_run_success("run", tmp_path, settings, selected_issuers=("shinhan",))
    with ledger._get_connection() as connection:
        pending = [dict(row) for row in connection.execute("SELECT * FROM backup_pending")]
    assert {row["sha256"] for row in pending} == {objects[0]["sha256"]}
    allowed = ledger.scope_remote_paths("run", tmp_path, ("shinhan",))
    assert object_path(objects[0]["sha256"]).as_posix() in allowed
    assert object_path(objects[1]["sha256"]).as_posix() not in allowed
    result = await ledger.flush(settings, force=True, allowed_remote_paths=frozenset())
    assert result["requests"] == 0
    with ledger._get_connection() as connection:
        assert connection.execute("SELECT count(*) FROM backup_pending").fetchone()[0] == 1


async def test_budget_query_isolates_issuers_on_shared_host(tmp_path):
    with WorkerState(tmp_path / "state.sqlite3") as state:
        metrics = IssuerHTTPMetrics(state, "run", request_limit=10000, byte_limit=100000)
        # Issuer A makes 5000 requests to Host 1
        metrics.increment(("issuer_a", "host1", "catalog"), "requests", 5000)
        # Issuer B makes 3000 requests to Host 1
        metrics.increment(("issuer_b", "host1", "catalog"), "requests", 3000)
        # Issuer A makes 4000 requests to Host 2
        metrics.increment(("issuer_a", "host2", "pdf"), "requests", 4000)

        # Enforce max(by_issuer, by_host) independently:
        # Issuer A on Host 1: issuer_a total = 9000, host1 total = 8000 -> max is 9000
        assert metrics.used("issuer_a", "host1", "requests") == 9000
        # Issuer B on Host 1: issuer_b total = 3000, host1 total = 8000 -> max is 8000
        assert metrics.used("issuer_b", "host1", "requests") == 8000


async def test_transient_transport_error_does_not_abort_retries(tmp_path):
    with WorkerState(tmp_path / "state.sqlite3") as state:
        metrics = IssuerHTTPMetrics(state, "run")
        attempts = 0

        def handler(request):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise httpx.ConnectTimeout("temporary timeout", request=request)
            return httpx.Response(200, content=b"recovered")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            event_hooks={"request": [metrics.before], "response": [metrics.after]},
        ) as client:
            limited = RateLimitedClient(client, IssuerRateLimiter(0), issuer="shinhan", purpose="pdf")
            # First attempt fails with ConnectTimeout
            with pytest.raises(httpx.ConnectTimeout):
                await limited.get("https://example.com/item")
            # Second attempt should NOT be blocked by cooldown, and succeeds
            response = await limited.get("https://example.com/item")
            assert response.status_code == 200
            assert response.content == b"recovered"

        values = {r["metric"]: r["value"] for r in metrics.report()}
        assert values["timeouts"] == 1
        assert values["status_200"] == 1


def test_origin_policy_inherit_commits_durably(tmp_path):
    from cardrag_worker.origin_policy import OriginPolicy

    db_path = tmp_path / "state.sqlite3"
    with WorkerState(db_path) as state:
        policy = OriginPolicy(state, interval=timedelta(days=90))
        now = datetime(2026, 10, 19, tzinfo=UTC)
        policy.verified("source_prior", now, result="body")
        policy.inherit("source_new", "source_prior")

    # Re-open with fresh connection to verify durable commit
    with WorkerState(db_path) as re_state:
        row = re_state.connection.execute(
            "SELECT origin_checked_at,result FROM origin_audit WHERE source_id=?", ("source_new",)
        ).fetchone()
        assert row is not None
        assert row[0] == now.isoformat()
        assert row[1] == "body"


def test_origin_policy_overdue_and_verified_304(tmp_path):
    from cardrag_worker.origin_policy import OriginPolicy

    with WorkerState(tmp_path / "state.sqlite3") as state:
        policy = OriginPolicy(state, interval=timedelta(days=90))
        now = datetime(2026, 10, 19, tzinfo=UTC)
        old_time = now - timedelta(days=95)
        state.connection.execute(
            "INSERT INTO origin_audit VALUES (?,?,?,?)",
            ("source_old", old_time.isoformat(), (old_time + timedelta(days=90)).isoformat(), "body"),
        )
        state.connection.commit()
        report = policy.report(now=now)
        assert report["scheduled"] == 1
        assert report["overdue"] == 1

        policy.verified("source_old", now, result="not_modified")
        updated_report = policy.report(now=now)
        assert updated_report["overdue"] == 0
        row = state.connection.execute(
            "SELECT result FROM origin_audit WHERE source_id='source_old'"
        ).fetchone()
        assert row[0] == "not_modified"


async def test_woori_1000_limit_raises_markup_changed():
    from cardrag_worker.issuers.common import IssuerMarkupChanged
    from cardrag_worker.issuers.woori import WooriAdapter

    adapter = WooriAdapter()
    thousand_products = [{"cardId": f"C{i:04d}", "cardNm": f"Card {i}"} for i in range(1000)]

    def handler(request):
        if "main" in str(request.url) or request.method == "POST":
            return httpx.Response(200, json={"mainDataList": {"cct11PrdntcAgrmMainVo": thousand_products}})
        return httpx.Response(
            200,
            text=(
                '<html><input type="hidden" id="raonkAuthKey" value="SYNTHETIC_AUTH_KEY_FIXTURE_017" />'
                '<input type="hidden" id="raonkAuthPageId" value="H1CCT211S09" /></html>'
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(IssuerMarkupChanged, match="Woori main listing requires pagination"):
            await adapter.discover_current(client)


def test_origin_policy_negative_non_drm_payload(tmp_path):
    from cardrag_worker.origin_policy import OriginPolicy

    with WorkerState(tmp_path / "state.sqlite3") as state:
        policy = OriginPolicy(state, interval=timedelta(days=90))
        now = datetime(2026, 10, 19, tzinfo=UTC)
        policy.remember_negative("doc_404", {"status": 404}, now=now, known=True)
        retrieved = policy.negative("doc_404", now=now)
        assert retrieved == {"status": 404}
        assert "magic" not in retrieved


async def test_automatic_resume_keeps_original_roster(tmp_path):
    from cardrag_worker.issuer_scope import freeze_scope
    from cardrag_worker.rotation import scope_session

    with WorkerState(tmp_path / "state.sqlite3") as state:
        opts = dict(selected=None, mode="round_robin", channel="stable", resume=None)
        async with scope_session(state, tmp_path, ("shinhan", "lotte"), **opts) as (assignment, bind):
            run_id = state.start_run()
            bind(run_id)
            freeze_scope(tmp_path, run_id, assignment.scope)
        async with scope_session(state, tmp_path, ("shinhan", "lotte", "kb"), **opts) as (assignment, bind):
            assert assignment.run_id == run_id
            freeze_scope(tmp_path, run_id, assignment.scope)


@pytest.mark.parametrize("registered", [False, True])
async def test_process_reduced_pool_resume_after_seal(corpus, monkeypatch, registered):  # noqa: F811
    from cardrag_worker.acquisition import AcquisitionSource
    from cardrag_worker.partial_execution import ExecutionPlan, ReuseSource
    from cardrag_worker.pipeline import WorkerPipeline, WorkerUnexpectedFailureError
    from cardrag_worker.webdav import RemoteGenerationIdentity

    factory, prior, state, pdf_calls, embed_calls, ocr, dav = corpus
    m = prior.manifest
    dav.current = RemoteGenerationIdentity(
        m.generation_id, m.corpus_sha256, m.contract_sha256, m.schema_version, m.serving_schema
    )
    initial = factory()
    base = initial.adapters[0].record
    other = _Adapter(
        replace(
            base,
            issuer="otherbank",
            product_code="other",
            source_post_id="other",
            source_url="https://cards.example/other.pdf",
        )
    )
    other.spec = replace(other.spec, code="otherbank", sort_order=2)
    if registered:
        from cardrag_worker.issuers.registry import REGISTERED_ISSUERS, Registration

        monkeypatch.setitem(REGISTERED_ISSUERS, "otherbank", Registration(other.spec, lambda: other))

    def runner(adapters, plan=None, source=None, selected=("testbank",)):
        return WorkerPipeline(
            state=state,
            state_dir=prior.root,
            adapters=adapters,
            selected_issuers=selected,
            ocr=ocr,
            embeddings=initial.embeddings,
            webdav=dav,
            maximum_attempts=1,
            retry_cap_seconds=0,
            execution_plan=plan,
            reuse_source=source,
        )

    full_runner = runner([initial.adapters[0], other], selected=("testbank", "otherbank"))
    full = await full_runner.run()
    baseline = ReuseSource(prior.root, full.run_id, state.path)
    dav.current = RemoteGenerationIdentity(
        baseline.manifest.generation_id,
        baseline.manifest.corpus_sha256,
        baseline.manifest.contract_sha256,
        baseline.manifest.schema_version,
        baseline.manifest.serving_schema,
    )
    collected = await runner(
        [initial.adapters[0], other], ExecutionPlan(phase="collect", selected_issuers=("testbank",))
    ).run()
    source = AcquisitionSource(prior.root, collected.run_id, state.path)
    plan = ExecutionPlan(
        frozenset({"pdf"}), collected.run_id, phase="process", selected_issuers=("testbank",)
    )
    dav.fail_pointer_once = True
    with pytest.raises(WorkerUnexpectedFailureError):
        await runner([initial.adapters[0]], plan, source).run()
    run_id = state.connection.execute("SELECT run_id FROM run ORDER BY started_at DESC LIMIT 1").fetchone()[0]
    assert (prior.root / "runs" / run_id / "sealed/publish.json").is_file()
    before = (len(pdf_calls), ocr.calls, len(embed_calls))
    result = await runner([initial.adapters[0]], plan, source).run(resume_run_id=run_id)
    assert (len(pdf_calls), ocr.calls, len(embed_calls)) == before
    output = ReuseSource(prior.root, result.run_id, state.path)
    assert {d.issuer for d in output.manifest.documents} == {"testbank", "otherbank"}


@pytest.mark.parametrize("command_mode", ["original", "bare", "matching_issuer"])
async def test_process_explicit_resume_restores_inferred_scope(corpus, monkeypatch, command_mode):  # noqa: F811
    import asyncio
    from dataclasses import fields
    from types import SimpleNamespace

    from typer.testing import CliRunner

    from cardrag_worker import cli, issuers, settings
    from cardrag_worker.acquisition import AcquisitionSource
    from cardrag_worker.issuers.registry import REGISTERED_ISSUERS, Registration
    from cardrag_worker.partial_execution import ExecutionPlan, ReuseSource
    from cardrag_worker.pipeline import WorkerPipeline, WorkerUnexpectedFailureError
    from cardrag_worker.state import WorkerState
    from cardrag_worker.webdav import RemoteGenerationIdentity, channel_pointer_path

    factory, prior, state, pdf_calls, embed_calls, ocr, dav = corpus
    m = prior.manifest
    dav.current = RemoteGenerationIdentity(
        m.generation_id, m.corpus_sha256, m.contract_sha256, m.schema_version, m.serving_schema
    )
    initial = factory()
    base = initial.adapters[0].record
    other = _Adapter(
        replace(
            base,
            issuer="otherbank",
            product_code="other",
            source_post_id="other",
            source_url="https://cards.example/other.pdf",
        )
    )
    other.spec = replace(other.spec, code="otherbank", sort_order=2)
    monkeypatch.setitem(
        REGISTERED_ISSUERS, "testbank", Registration(initial.adapters[0].spec, lambda: initial.adapters[0])
    )
    monkeypatch.setitem(REGISTERED_ISSUERS, "otherbank", Registration(other.spec, lambda: other))

    def runner(adapters, plan=None, source=None, selected=("testbank",)):
        return WorkerPipeline(
            state=state,
            state_dir=prior.root,
            adapters=adapters,
            selected_issuers=selected,
            ocr=ocr,
            embeddings=initial.embeddings,
            webdav=dav,
            maximum_attempts=1,
            retry_cap_seconds=0,
            execution_plan=plan,
            reuse_source=source,
        )

    full_runner = runner([initial.adapters[0], other], selected=("testbank", "otherbank"))
    full = await full_runner.run()
    baseline = ReuseSource(prior.root, full.run_id, state.path)
    dav.current = RemoteGenerationIdentity(
        baseline.manifest.generation_id,
        baseline.manifest.corpus_sha256,
        baseline.manifest.contract_sha256,
        baseline.manifest.schema_version,
        baseline.manifest.serving_schema,
    )

    collected = await runner(
        [initial.adapters[0], other],
        ExecutionPlan(phase="collect", selected_issuers=("testbank",), channel="stable"),
    ).run()
    collect_run_id = collected.run_id

    source = AcquisitionSource(prior.root, collect_run_id, state.path)
    plan = ExecutionPlan(
        frozenset({"pdf"}), collect_run_id, phase="process", selected_issuers=("testbank",), channel="stable"
    )
    dav.fail_pointer_once = True
    with pytest.raises(WorkerUnexpectedFailureError):
        await runner([initial.adapters[0]], plan, source).run()
    process_run_id = state.connection.execute(
        "SELECT run_id FROM run ORDER BY started_at DESC LIMIT 1"
    ).fetchone()[0]
    assert (prior.root / "runs" / process_run_id / "sealed/publish.json").is_file()

    configured = settings.WorkerSettings.from_env(require_providers=False, require_webdav=False)
    values = {f.name: getattr(configured, f.name) for f in fields(configured)}
    values.update(
        state_dir=prior.root,
        state_database=state.path,
        lock_file=prior.root / "worker.lock",
        embedding_provider_id=prior.profile["provider_id"],
        embedding_model=prior.profile["model"],
        embedding_dimension=prior.profile["dimension"],
        embedding_maximum_tokens=prior.profile["maximum_tokens"],
        minimum_start_free_bytes=0,
        reserved_free_space_bytes=0,
        channel="stable",
        stable_publication_approved=True,
        publication_transport="webdav",
    )
    test_settings = SimpleNamespace(**values)
    monkeypatch.setattr(settings.WorkerSettings, "from_env", lambda **_: test_settings)
    monkeypatch.setattr(cli.WorkerSettings, "from_env", lambda **_: test_settings)

    from cardrag_worker import webdav as webdav_mod

    dav.channel = "stable"
    dav.pointer_path = channel_pointer_path("stable")
    dav.stable_publication_approved = True

    class FakeWebDAVClient:
        channel = "stable"

        @classmethod
        def from_env(cls, **kw):
            return SimpleNamespace(core="dummy_core")

        def __new__(cls, *args, **kwargs):
            return dav

    monkeypatch.setattr(webdav_mod, "WebDAVClient", FakeWebDAVClient)
    monkeypatch.setattr(cli, "_ocr_resolver", lambda *args, **kwargs: ocr)

    async def fake_embed(*args, **kwargs):
        return initial.embeddings

    monkeypatch.setattr(cli, "_qwen_embedding_provider", fake_embed)
    monkeypatch.setattr(issuers, "enabled_adapters", lambda: [initial.adapters[0]])
    monkeypatch.setenv("CARDRAG_ENABLED_ISSUERS", "testbank,otherbank")

    async def fake_shutdown(operation, **kwargs):
        return await operation()

    monkeypatch.setattr(cli, "_operation_with_signal_shutdown", fake_shutdown)

    state.close()
    runner_cli = CliRunner()

    if command_mode == "original":
        res_diff_issuer = await asyncio.to_thread(
            runner_cli.invoke,
            cli.app,
            [
                "run",
                "--phase",
                "process",
                "--reuse-from-run",
                collect_run_id,
                "--resume",
                process_run_id,
                "--issuer",
                "otherbank",
            ],
        )
        assert res_diff_issuer.exit_code != 0
        res_diff_source = await asyncio.to_thread(
            runner_cli.invoke,
            cli.app,
            [
                "run",
                "--phase",
                "process",
                "--reuse-from-run",
                "wrong-source",
                "--resume",
                process_run_id,
            ],
        )
        assert res_diff_source.exit_code != 0
        res_diff_phase = await asyncio.to_thread(
            runner_cli.invoke,
            cli.app,
            ["run", "--phase", "collect", "--resume", process_run_id],
        )
        assert res_diff_phase.exit_code != 0
        cmd = [
            "run",
            "--phase",
            "process",
            "--reuse-from-run",
            collect_run_id,
            "--resume",
            process_run_id,
        ]
    elif command_mode == "bare":
        cmd = ["run", "--resume", process_run_id]
    elif command_mode == "matching_issuer":
        cmd = [
            "run",
            "--phase",
            "process",
            "--reuse-from-run",
            collect_run_id,
            "--resume",
            process_run_id,
            "--issuer",
            "testbank",
        ]

    res_success = await asyncio.to_thread(runner_cli.invoke, cli.app, cmd)
    assert res_success.exit_code == 0, res_success.output

    with WorkerState(test_settings.state_database) as post_state:
        assert post_state.run_status(process_run_id) == "succeeded"
        resumed = ReuseSource(prior.root, process_run_id, post_state.path)
        assert {d.issuer for d in resumed.manifest.documents} == {"testbank", "otherbank"}


async def test_process_preserves_issuer_removed_from_pool(corpus):  # noqa: F811
    from cardrag_worker.acquisition import AcquisitionSource
    from cardrag_worker.partial_execution import ExecutionPlan, ReuseSource
    from cardrag_worker.pipeline import WorkerPipeline
    from cardrag_worker.webdav import RemoteGenerationIdentity

    factory, prior, state, pdf_calls, embed_calls, ocr, dav = corpus
    m = prior.manifest
    dav.current = RemoteGenerationIdentity(
        m.generation_id, m.corpus_sha256, m.contract_sha256, m.schema_version, m.serving_schema
    )
    initial = factory()
    base = initial.adapters[0].record
    other = _Adapter(
        replace(
            base,
            issuer="otherbank",
            product_code="other",
            source_post_id="other",
            source_url="https://cards.example/other.pdf",
        )
    )
    other.spec = replace(other.spec, code="otherbank", sort_order=2)

    def runner(adapters, plan=None, source=None, selected=("testbank",)):
        return WorkerPipeline(
            state=state,
            state_dir=prior.root,
            adapters=adapters,
            selected_issuers=selected,
            ocr=ocr,
            embeddings=initial.embeddings,
            webdav=dav,
            maximum_attempts=1,
            retry_cap_seconds=0,
            execution_plan=plan,
            reuse_source=source,
        )

    full_runner = runner([initial.adapters[0], other], selected=("testbank", "otherbank"))
    full = await full_runner.run()
    baseline = ReuseSource(prior.root, full.run_id, state.path)
    dav.current = RemoteGenerationIdentity(
        baseline.manifest.generation_id,
        baseline.manifest.corpus_sha256,
        baseline.manifest.contract_sha256,
        baseline.manifest.schema_version,
        baseline.manifest.serving_schema,
    )
    collected = await runner(
        [initial.adapters[0], other], ExecutionPlan(phase="collect", selected_issuers=("testbank",))
    ).run()
    source = AcquisitionSource(prior.root, collected.run_id, state.path)
    plan = ExecutionPlan(
        frozenset({"pdf"}), collected.run_id, phase="process", selected_issuers=("testbank",)
    )
    result = await runner([initial.adapters[0]], plan, source).run()
    output = ReuseSource(prior.root, result.run_id, state.path)
    assert {d.issuer for d in output.manifest.documents} == {"testbank", "otherbank"}


async def test_ocr_queue_excludes_retained_issuer_outside_configured_pool(corpus, monkeypatch):  # noqa: F811
    from cardrag_worker.ocr_requests import OCRReprocessRequest, OCRRequestTarget, queue_reprocess_requests
    from cardrag_worker.partial_execution import PartialExecutionError
    from cardrag_worker.pipeline import WorkerPipeline

    factory, baseline, state, _, _, _, _ = corpus
    runner = factory()
    old = next(iter(baseline.documents.values()))
    request = OCRReprocessRequest(
        request_id="ocr-foreign",
        source_generation_id=baseline.manifest.generation_id,
        created_at=datetime.now(UTC),
        targets=(
            OCRRequestTarget(
                document_id=old.document_id,
                pdf_sha256=old.pdf.sha256,
                pdf_size_bytes=old.pdf.size_bytes,
                page_count=old.page_count,
            ),
        ),
    )
    queue_reprocess_requests(baseline.root, (request,))
    new_adapter = _Adapter(replace(runner.adapters[0].record, issuer="otherbank"))
    new_adapter.spec = replace(new_adapter.spec, code="otherbank")
    runner = WorkerPipeline(
        state=state,
        state_dir=baseline.root,
        adapters=[new_adapter],
        ocr=runner.ocr,
        embeddings=runner.embeddings,
        webdav=runner.webdav,
        maximum_attempts=1,
    )
    captured = []

    async def inspect_selection(*args, **kwargs):
        captured.append(runner._active_ocr_request)
        raise PartialExecutionError("review_selection_observed", "ocr")

    monkeypatch.setattr(runner, "_run_locked", inspect_selection)
    with pytest.raises(PartialExecutionError, match="review_selection_observed"):
        await runner.run()
    assert captured == [None], "nonselected OCR request was bound before collection"


@pytest.mark.parametrize("registered", [False, True])
async def test_process_reduced_pool_resume_after_commit(corpus, monkeypatch, registered):  # noqa: F811
    from cardrag_worker.acquisition import AcquisitionSource
    from cardrag_worker.partial_execution import ExecutionPlan, ReuseSource
    from cardrag_worker.pipeline import WorkerPipeline
    from cardrag_worker.webdav import RemoteGenerationIdentity

    factory, prior, state, pdf_calls, embed_calls, ocr, dav = corpus

    def activate(dav, source):
        m = source.manifest
        dav.current = RemoteGenerationIdentity(
            m.generation_id,
            m.corpus_sha256,
            m.contract_sha256,
            m.schema_version,
            m.serving_schema,
        )

    activate(dav, prior)
    initial = factory()
    base = initial.adapters[0].record
    other = _Adapter(
        replace(
            base,
            issuer="otherbank",
            product_code="other",
            source_post_id="other",
            source_url="https://cards.example/other.pdf",
        )
    )
    other.spec = replace(other.spec, code="otherbank", sort_order=2)
    if registered:
        from cardrag_worker.issuers.registry import REGISTERED_ISSUERS, Registration

        monkeypatch.setitem(REGISTERED_ISSUERS, "otherbank", Registration(other.spec, lambda: other))

    def runner(adapters, plan=None, source=None, selected=("testbank",)):
        return WorkerPipeline(
            state=state,
            state_dir=prior.root,
            adapters=adapters,
            selected_issuers=selected,
            ocr=ocr,
            embeddings=initial.embeddings,
            webdav=dav,
            maximum_attempts=1,
            retry_cap_seconds=0,
            execution_plan=plan,
            reuse_source=source,
        )

    full_runner = runner([initial.adapters[0], other], selected=("testbank", "otherbank"))
    full = await full_runner.run()
    baseline = ReuseSource(prior.root, full.run_id, state.path)
    activate(dav, baseline)
    collected = await runner(
        [initial.adapters[0], other], ExecutionPlan(phase="collect", selected_issuers=("testbank",))
    ).run()
    source = AcquisitionSource(prior.root, collected.run_id, state.path)
    plan = ExecutionPlan(
        frozenset({"pdf"}), collected.run_id, phase="process", selected_issuers=("testbank",)
    )
    committed = await runner([initial.adapters[0]], plan, source).run()
    run_id = committed.run_id
    committed_source = ReuseSource(prior.root, run_id, state.path)
    activate(dav, committed_source)
    state.finish_run(run_id, "interrupted", error="review: pointer committed before local acknowledgement")
    before = (len(pdf_calls), ocr.calls, len(embed_calls))
    result = await runner([initial.adapters[0]], plan, source).run(resume_run_id=run_id)
    assert (len(pdf_calls), ocr.calls, len(embed_calls)) == before
    output = ReuseSource(prior.root, result.run_id, state.path)
    assert {d.issuer for d in output.manifest.documents} == {"testbank", "otherbank"}


# ---------------------------------------------------------------------------
# FIX_06 Tests: ZIP Attachment Isolation, No-Unzip Spy, and Prior Preservation
# ---------------------------------------------------------------------------


def _make_snapshot(adapter, records):
    from cardrag_worker.contracts import snapshot_from_records

    return snapshot_from_records(
        issuer=adapter.spec.code,
        source_url="https://cards.example/list",
        parser_version=adapter.parser_version,
        records=records,
        started_at=datetime(2026, 8, 29, tzinfo=UTC),
    )


def _activate_remote(dav, source):
    from cardrag_worker.webdav import RemoteGenerationIdentity

    m = source.manifest
    dav.current = RemoteGenerationIdentity(
        m.generation_id,
        m.corpus_sha256,
        m.contract_sha256,
        m.schema_version,
        m.serving_schema,
    )


@pytest.mark.parametrize(
    ("issuer", "product_code", "post_id", "file_name"),
    [
        ("lotte", "9901", "9901", "terms_2026.zip"),
        ("lotte", "9902", "9902", "GUIDE_NEW.ZIP"),
        ("shinhan", "SH001", "post_sh", "disclosure.zip"),
        ("kbcard", "KB999", "post_kb", "PRODUCT_MANUAL.ZIP"),
    ],
)
def test_zip_attachment_multiple_fixtures_no_unzip_spy(
    tmp_path, monkeypatch, issuer, product_code, post_id, file_name
):
    import zipfile

    from cardrag_worker.downloader import UnsupportedAttachmentError, validate_pdf
    from cardrag_worker.pipeline import _is_explicit_zip

    def forbidden_zipfile(*args, **kwargs):
        raise AssertionError("zipfile was invoked! ZIP archive must NEVER be opened or unzipped.")

    monkeypatch.setattr(zipfile, "ZipFile", forbidden_zipfile)

    now = datetime(2026, 10, 19, tzinfo=UTC)
    record = SourceRecord(
        issuer=issuer,
        product_code=product_code,
        product_name=f"Test {product_code}",
        effective_date=date(2026, 10, 1),
        source_version="20261001",
        source_url=f"https://cards.example/{file_name}",
        source_post_id=post_id,
        file_name=file_name,
        category="credit",
        discovered_at=now,
        metadata={"attachment_type": "zip"},
    )
    assert record.is_zip is True
    assert record.is_pdf is False
    assert _is_explicit_zip(record) is True

    # Validate that validate_pdf rejects ZIP signatures without calling zipfile
    zip_signature = b"PK\x03\x04\x14\x00\x00\x00\x08\x00fakezipcontent"
    zip_path = tmp_path / "test.zip"
    zip_path.write_bytes(zip_signature)
    with pytest.raises(UnsupportedAttachmentError, match="zip archive"):
        validate_pdf(zip_path)


@pytest.mark.asyncio
async def test_mixed_pdf_and_zip_pipeline_execution(corpus, monkeypatch):
    import zipfile

    from cardrag_worker.pipeline import WorkerPipeline

    def forbidden_zipfile(*args, **kwargs):
        raise AssertionError("zipfile was invoked! ZIP archive must NEVER be unzipped.")

    monkeypatch.setattr(zipfile, "ZipFile", forbidden_zipfile)

    factory, prior, state, pdf_calls, embed_calls, ocr, dav = corpus
    _activate_remote(dav, prior)
    runner = factory()
    base_adapter = runner.adapters[0]
    base_rec = base_adapter.record

    # Card 1: Normal PDF
    card1 = replace(
        base_rec,
        product_code="CARD_NORMAL",
        source_post_id="post_normal",
        file_name="normal.pdf",
        source_url="https://cards.example/normal.pdf",
    )
    # Card 2: Explicit ZIP
    card2 = replace(
        base_rec,
        product_code="CARD_ZIP",
        source_post_id="post_zip",
        file_name="manual.zip",
        source_url="https://cards.example/manual.zip",
        metadata={"attachment_type": "zip"},
    )

    mixed_adapter = _Adapter(card1)
    snapshot_records = [card1, card2]

    async def mock_discover(client):
        return _make_snapshot(mixed_adapter, snapshot_records)

    monkeypatch.setattr(mixed_adapter, "discover_current", mock_discover)

    pipe = WorkerPipeline(
        state=state,
        state_dir=prior.root,
        adapters=[mixed_adapter],
        ocr=runner.ocr,
        embeddings=runner.embeddings,
        webdav=runner.webdav,
        maximum_attempts=1,
    )

    pdf_requests_before = len(pdf_calls)

    result = await pipe.run()

    # Verify Card 2 (ZIP) had 0 body GETs, 0 OCR, 0 embeddings
    zip_gets = [url for url in pdf_calls[pdf_requests_before:] if "manual.zip" in url]
    assert len(zip_gets) == 0, "Explicit ZIP must skip body download"
    assert result.unsupported_attachment_count == 1
    assert result.collection_status == "degraded"
    assert result.status in {"succeeded", "no_change"}

    # Card 1 (PDF) succeeded and is published in manifest
    current_remote = await dav.validated_current_generation()
    assert current_remote is not None
    assert result.document_count == 1


@pytest.mark.asyncio
async def test_sniffed_zip_signature_isolation_and_sibling_continuation(corpus, monkeypatch):
    import zipfile

    from test_pipeline import pdf_bytes

    import cardrag_worker.pipeline as pipeline_mod
    from cardrag_worker.pipeline import WorkerPipeline

    def forbidden_zipfile(*args, **kwargs):
        raise AssertionError("zipfile was invoked! ZIP archive must NEVER be unzipped.")

    monkeypatch.setattr(zipfile, "ZipFile", forbidden_zipfile)

    factory, prior, state, pdf_calls, embed_calls, ocr, dav = corpus
    _activate_remote(dav, prior)
    runner = factory()
    base_adapter = runner.adapters[0]
    base_rec = base_adapter.record

    # Card A: file_name is .pdf but server serves ZIP bytes
    card_a = replace(
        base_rec,
        product_code="CARD_SNEAKY_ZIP",
        source_post_id="post_sneaky",
        file_name="sneaky.pdf",
        source_url="https://cards.example/sneaky.pdf",
    )
    # Card B: file_name is .pdf and server serves normal PDF
    card_b = replace(
        base_rec,
        product_code="CARD_HONEST_PDF",
        source_post_id="post_honest",
        file_name="honest.pdf",
        source_url="https://cards.example/honest.pdf",
    )

    adapter = _Adapter(card_a)

    async def mock_discover(client):
        return _make_snapshot(adapter, [card_a, card_b])

    monkeypatch.setattr(adapter, "discover_current", mock_discover)

    valid_pdf_content = pdf_bytes()
    zip_bytes_content = b"PK\x03\x04\x14\x00\x00\x00\x08\x00" + b"\x00" * 100

    def mock_pdf_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        pdf_calls.append(url)
        if "sneaky.pdf" in url:
            return httpx.Response(
                200,
                headers={"content-type": "application/zip"},
                content=zip_bytes_content,
                request=request,
            )
        return httpx.Response(
            200,
            headers={"content-type": "application/pdf"},
            content=valid_pdf_content,
            request=request,
        )

    real_async_client = httpx.AsyncClient
    mock_transport = httpx.MockTransport(mock_pdf_handler)

    def client_factory(**kwargs):
        if kwargs.get("transport") is None:
            kwargs["transport"] = mock_transport
        return real_async_client(**kwargs)

    monkeypatch.setattr(pipeline_mod.httpx, "AsyncClient", client_factory)

    pipe = WorkerPipeline(
        state=state,
        state_dir=prior.root,
        adapters=[adapter],
        ocr=runner.ocr,
        embeddings=runner.embeddings,
        webdav=runner.webdav,
        maximum_attempts=1,
    )

    result = await pipe.run()

    # Sibling (card_b) must succeed and not be cancelled
    assert result.status in {"succeeded", "no_change"}
    assert result.unsupported_attachment_count == 1
    assert result.collection_status == "degraded"
    assert result.document_count == 1


@pytest.mark.asyncio
async def test_prior_document_preservation_and_retirement_freeze(corpus, monkeypatch):
    import zipfile

    from cardrag_worker.pipeline import WorkerPipeline

    def forbidden_zipfile(*args, **kwargs):
        raise AssertionError("zipfile was invoked!")

    monkeypatch.setattr(zipfile, "ZipFile", forbidden_zipfile)

    factory, prior, state, pdf_calls, embed_calls, ocr, dav = corpus
    _activate_remote(dav, prior)
    runner = factory()
    base_adapter = runner.adapters[0]
    base_rec = base_adapter.record

    # Card 1 had a verified document in baseline
    card1_prior = base_rec
    # Card 1 now has attachment changed to .zip
    card1_zip = replace(
        card1_prior,
        file_name="updated_terms.zip",
        source_url="https://cards.example/updated_terms.zip",
        metadata={"attachment_type": "zip"},
    )
    # Card 2 is newly introduced with .zip (no prior baseline)
    card2_zip = replace(
        card1_prior,
        product_code="NEW_PRODUCT_ZIP",
        source_post_id="post_new_zip",
        file_name="new_product.zip",
        source_url="https://cards.example/new_product.zip",
        metadata={"attachment_type": "zip"},
    )

    adapter = _Adapter(card1_zip)

    async def mock_discover(client):
        return _make_snapshot(adapter, [card1_zip, card2_zip])

    monkeypatch.setattr(adapter, "discover_current", mock_discover)

    pipe = WorkerPipeline(
        state=state,
        state_dir=prior.root,
        adapters=[adapter],
        ocr=runner.ocr,
        embeddings=runner.embeddings,
        webdav=runner.webdav,
        maximum_attempts=1,
    )

    result = await pipe.run()

    # Prior document for card1 is preserved as current!
    # Card 2 is reported as unsupported attachment.
    # Retirement gate: card1 was in discovery, so not retired.
    assert result.unsupported_attachment_count >= 1
    assert result.collection_status == "degraded"
    assert result.document_count == 1  # card1's prior doc preserved in serving
    assert result.retired_count == 0
    assert result.retirement_candidate_count == 0


@pytest.mark.asyncio
async def test_recheck_and_recovery_to_pdf(corpus, monkeypatch):
    from cardrag_worker.pipeline import WorkerPipeline

    factory, prior, state, pdf_calls, embed_calls, ocr, dav = corpus
    _activate_remote(dav, prior)
    runner = factory()
    base_adapter = runner.adapters[0]
    base_rec = base_adapter.record

    # Card 0: Normal card to satisfy OCR gate
    card_normal = base_rec
    # Card 1: Listing has card with .zip
    card_zip = replace(
        base_rec,
        product_code="RECHECK_CARD",
        source_post_id="post_recheck",
        file_name="terms.zip",
        source_url="https://cards.example/terms.zip",
        metadata={"attachment_type": "zip"},
    )

    adapter = _Adapter(card_normal)

    async def mock_discover_zip(client):
        return _make_snapshot(adapter, [card_normal, card_zip])

    monkeypatch.setattr(adapter, "discover_current", mock_discover_zip)

    pipe1 = WorkerPipeline(
        state=state,
        state_dir=prior.root,
        adapters=[adapter],
        ocr=runner.ocr,
        embeddings=runner.embeddings,
        webdav=runner.webdav,
        maximum_attempts=1,
    )
    res1 = await pipe1.run()
    assert res1.unsupported_attachment_count == 1
    assert res1.collection_status == "degraded"

    # Batch 2: Next run with same ZIP -> 0 body downloads for ZIP
    calls_before = len(pdf_calls)
    pipe2 = WorkerPipeline(
        state=state,
        state_dir=prior.root,
        adapters=[adapter],
        ocr=runner.ocr,
        embeddings=runner.embeddings,
        webdav=runner.webdav,
        maximum_attempts=1,
    )
    await pipe2.run()
    zip_calls = [url for url in pdf_calls[calls_before:] if "terms.zip" in url]
    assert len(zip_calls) == 0, "Same ZIP on next batch must not download body"

    # Batch 3: Card metadata changes to .pdf -> reacquired strictly
    card_pdf = replace(
        card_zip,
        file_name="terms.pdf",
        source_url="https://cards.example/terms.pdf",
        metadata={},
    )

    async def mock_discover_pdf(client):
        return _make_snapshot(adapter, [card_normal, card_pdf])

    monkeypatch.setattr(adapter, "discover_current", mock_discover_pdf)
    calls_before_batch3 = len(pdf_calls)
    pipe3 = WorkerPipeline(
        state=state,
        state_dir=prior.root,
        adapters=[adapter],
        ocr=runner.ocr,
        embeddings=runner.embeddings,
        webdav=runner.webdav,
        maximum_attempts=1,
    )
    res3 = await pipe3.run()
    assert res3.unsupported_attachment_count == 0
    assert res3.collection_status == "complete"
    assert res3.document_count == 2
    assert any("terms.pdf" in url for url in pdf_calls[calls_before_batch3:]), (
        "Recovered PDF must be acquired strictly"
    )


@pytest.mark.asyncio
async def test_collect_and_process_phase_handoff_with_unsupported_attachment(corpus, monkeypatch):
    from cardrag_worker.acquisition import AcquisitionSource
    from cardrag_worker.partial_execution import ExecutionPlan
    from cardrag_worker.pipeline import WorkerPipeline

    factory, prior, state, pdf_calls, embed_calls, ocr, dav = corpus
    _activate_remote(dav, prior)
    runner = factory()
    base_adapter = runner.adapters[0]
    base_rec = base_adapter.record

    card_pdf = replace(
        base_rec,
        product_code="CARD_COLLECT_PDF",
        source_post_id="post_col_pdf",
        file_name="doc.pdf",
        source_url="https://cards.example/doc.pdf",
    )
    card_zip = replace(
        base_rec,
        product_code="CARD_COLLECT_ZIP",
        source_post_id="post_col_zip",
        file_name="doc.zip",
        source_url="https://cards.example/doc.zip",
        metadata={"attachment_type": "zip"},
    )

    adapter = _Adapter(card_pdf)

    async def mock_discover(client):
        return _make_snapshot(adapter, [card_pdf, card_zip])

    monkeypatch.setattr(adapter, "discover_current", mock_discover)

    # Phase 1: Collect
    collect_pipe = WorkerPipeline(
        state=state,
        state_dir=prior.root,
        adapters=[adapter],
        ocr=runner.ocr,
        embeddings=runner.embeddings,
        webdav=runner.webdav,
        maximum_attempts=1,
        execution_plan=ExecutionPlan(phase="collect"),
    )
    collect_res = await collect_pipe.run()
    assert collect_res.status == "collected"
    assert collect_res.unsupported_attachment_count == 1
    assert collect_res.collection_status == "degraded"

    # Verify acquisition.json
    acq_path = prior.root / "runs" / collect_res.run_id / "acquisition.json"
    assert acq_path.exists()
    acq_data = json.loads(acq_path.read_text())
    assert len(acq_data["unsupported_attachments"]) == 1
    assert acq_data["unsupported_attachments"][0]["product_code"] == "CARD_COLLECT_ZIP"
    assert acq_data["unsupported_attachments"][0]["format"] == "zip"

    # Phase 2: Process using AcquisitionSource
    acq_source = AcquisitionSource(prior.root, collect_res.run_id, state.path)
    assert len(acq_source.unsupported_attachments) == 1

    process_pipe = WorkerPipeline(
        state=state,
        state_dir=prior.root,
        adapters=[adapter],
        ocr=runner.ocr,
        embeddings=runner.embeddings,
        webdav=runner.webdav,
        maximum_attempts=1,
        execution_plan=ExecutionPlan(frozenset({"pdf"}), collect_res.run_id, phase="process"),
        reuse_source=acq_source,
    )
    process_res = await process_pipe.run()
    assert process_res.status == "succeeded"
    assert process_res.document_count == 1


def test_lotte_adapter_discovers_zip_records():
    from cardrag_worker.issuers.lotte import _pdf_url, parse_listing_page

    now = datetime(2026, 10, 19, tzinfo=UTC)
    payload = {
        "Status": {"code": 0},
        "Content": json.dumps(
            {
                "result": {
                    "collection": [
                        {
                            "id": "disclosure",
                            "totalcount": "2",
                            "count": "2",
                            "docs": [
                                {
                                    "DOCID": "1001",
                                    "VT_CD_KND_NM": "Normal Card",
                                    "OCY_FILE_NM": "normal.pdf",
                                    "BULT_SDT": "20261019",
                                    "ISU_E_YN": "N",
                                    "VT_CD_KND_C": "",
                                },
                                {
                                    "DOCID": "1002",
                                    "VT_CD_KND_NM": "Zip Card",
                                    "OCY_FILE_NM": "guide.zip",
                                    "BULT_SDT": "20261019",
                                    "ISU_E_YN": "N",
                                    "VT_CD_KND_C": "",
                                },
                            ],
                        }
                    ],
                    "parameter": {"collection": "disclosure", "query": "", "startcount": "0"},
                    "totalcount": "2",
                }
            }
        ),
    }

    total, records = parse_listing_page(payload, offset=0, page_size=100, discovered_at=now)
    assert total == 2
    assert len(records) == 2
    assert records[0].is_pdf is True and records[0].is_zip is False
    assert "attachment_type" not in records[0].metadata
    assert records[1].is_pdf is False and records[1].is_zip is True
    assert records[1].metadata.get("attachment_type") == "zip"
    assert _pdf_url("guide.zip").endswith("guide.zip")


@pytest.mark.asyncio
async def test_process_retains_zip_error_status(corpus, monkeypatch):
    from cardrag_worker.acquisition import AcquisitionSource
    from cardrag_worker.partial_execution import ExecutionPlan
    from cardrag_worker.pipeline import WorkerPipeline

    factory, prior, state, pdf_calls, embed_calls, ocr, dav = corpus
    _activate_remote(dav, prior)
    runner = factory()
    base_adapter = runner.adapters[0]
    base_rec = base_adapter.record

    card_pdf = replace(
        base_rec,
        product_code="CARD_COLLECT_PDF",
        source_post_id="post_col_pdf",
        file_name="doc.pdf",
        source_url="https://cards.example/doc.pdf",
    )
    card_zip = replace(
        base_rec,
        product_code="CARD_COLLECT_ZIP",
        source_post_id="post_col_zip",
        file_name="doc.zip",
        source_url="https://cards.example/doc.zip",
        metadata={"attachment_type": "zip"},
    )

    adapter = _Adapter(card_pdf)

    async def mock_discover(client):
        return _make_snapshot(adapter, [card_pdf, card_zip])

    monkeypatch.setattr(adapter, "discover_current", mock_discover)

    # Phase 1: Collect
    collect_pipe = WorkerPipeline(
        state=state,
        state_dir=prior.root,
        adapters=[adapter],
        ocr=runner.ocr,
        embeddings=runner.embeddings,
        webdav=runner.webdav,
        maximum_attempts=1,
        execution_plan=ExecutionPlan(phase="collect"),
    )
    collect_res = await collect_pipe.run()
    assert collect_res.status == "collected"
    assert collect_res.unsupported_attachment_count == 1
    assert collect_res.collection_status == "degraded"

    # Verify acquisition.json
    acq_path = prior.root / "runs" / collect_res.run_id / "acquisition.json"
    assert acq_path.exists()
    acq_data = json.loads(acq_path.read_text())
    assert len(acq_data["unsupported_attachments"]) == 1
    assert acq_data["unsupported_attachments"][0]["product_code"] == "CARD_COLLECT_ZIP"
    assert acq_data["unsupported_attachments"][0]["format"] == "zip"

    # Phase 2: Process using AcquisitionSource
    acq_source = AcquisitionSource(prior.root, collect_res.run_id, state.path)
    assert len(acq_source.unsupported_attachments) == 1

    process_pipe = WorkerPipeline(
        state=state,
        state_dir=prior.root,
        adapters=[adapter],
        ocr=runner.ocr,
        embeddings=runner.embeddings,
        webdav=runner.webdav,
        maximum_attempts=1,
        execution_plan=ExecutionPlan(frozenset({"pdf"}), collect_res.run_id, phase="process"),
        reuse_source=acq_source,
    )
    process_res = await process_pipe.run()
    assert process_res.status == "succeeded"
    assert process_res.document_count == 1
    assert (process_res.unsupported_attachment_count, process_res.collection_status) == (1, "degraded")


@pytest.mark.asyncio
async def test_explicit_zip_rechecked_after_audit_window(corpus, monkeypatch):
    import cardrag_worker.pipeline as module
    from cardrag_worker.pipeline import WorkerPipeline

    factory, prior, state, pdf_calls, embed_calls, ocr, dav = corpus
    _activate_remote(dav, prior)
    initial = factory()
    base = initial.adapters[0].record
    zipped = replace(
        base,
        product_code="zip-later-pdf",
        source_post_id="zip-later-pdf",
        file_name="same.zip",
        source_url="https://cards.example/same.zip",
        metadata={"attachment_type": "zip"},
    )
    adapter = _Adapter(base)

    async def discover(client):
        return _make_snapshot(adapter, [base, zipped])

    monkeypatch.setattr(adapter, "discover_current", discover)

    def runner():
        return WorkerPipeline(
            state=state,
            state_dir=prior.root,
            adapters=[adapter],
            ocr=ocr,
            embeddings=initial.embeddings,
            webdav=dav,
            maximum_attempts=1,
        )

    await runner().run()
    before = len(pdf_calls)

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(days=100)

    monkeypatch.setattr(module, "datetime", Later)
    # Origin mock now serves a valid PDF even at the unchanged .zip URL.
    await runner().run()
    assert any("same.zip" in url for url in pdf_calls[before:]), (
        "No bounded origin recheck even after 100 days"
    )


def test_real_hyundai_zip_reaches_common_policy():
    from test_issuers_hyundai_hana import NOW, PRODUCT, attachment, hyundai_payload

    from cardrag_worker.issuers.hyundai import parse_detail

    result = parse_detail(hyundai_payload(attachment("changed.zip")), product=PRODUCT, discovered_at=NOW)
    assert result.is_zip


def test_real_woori_zip_reaches_common_policy():
    from cardrag_worker.issuers.woori import parse_detail_records

    result = parse_detail_records(
        {"filePath": "/upload/changed.zip", "fileNm": "changed.zip", "beginDt": "20261001", "gdccVer": "v1"},
        product_code="P1",
        product_name="Card",
        discovered_at=datetime.now(UTC),
    )
    assert len(result) == 1 and result[0].is_zip


def test_real_kb_zip_reaches_common_policy():
    from cardrag_worker.issuers.kb import parse_listing

    html = """
    <tr>
      <td><a href="javascript:goDetail('05000', 'KB Zip Card')">KB Zip Card</a></td>
      <td><a href="/obj/card/download/05000__prdctOpmn_20261019.zip">다운로드</a></td>
    </tr>
    """
    records = parse_listing(html, category_code="0", discovered_at=datetime.now(UTC))
    assert len(records) == 1
    assert records[0].is_zip
    assert records[0].file_name.endswith(".zip")


def test_real_shinhan_zip_reaches_common_policy():
    from cardrag_worker.issuers.shinhan import parse_listing

    html = """
    <table>
      <tr name="pdPbnRow">
        <td>신한 Zip 카드</td>
        <td>2026.10.19</td>
        <td onclick="fnRetrieveFile('token123','shinhan_guide.zip')">PDF</td>
        <td onclick="openSvPifPop('P1','신한 Zip 카드')">이력</td>
      </tr>
    </table>
    <!-- DONE -->
    """
    records, _, done = parse_listing(html, category="credit", discovered_at=datetime.now(UTC))
    assert len(records) == 1
    assert records[0].is_zip
    assert records[0].file_name.endswith(".zip")


def test_real_hana_zip_reaches_common_policy():
    from test_issuers_hyundai_hana import hana_payload, hana_record

    from cardrag_worker.issuers.hana import parse_listing

    payload = hana_payload([hana_record(APN_FILE_NM="hana_guide.zip")])
    page = parse_listing(payload, discovered_at=datetime.now(UTC))
    assert len(page.records) == 1
    assert page.records[0].is_zip
    assert page.records[0].file_name.endswith(".zip")


def test_real_samsung_zip_reaches_common_policy():
    from cardrag_worker.issuers.samsung import parse_listing_page

    payload = {
        "common": {"procsRsDvC": "0"},
        "message": {"msgMarkDvC": "00"},
        "totInqrCt": "1",
        "blbdInqrRsList": [
            {
                "itgBlbdChnlDvC": "01",
                "itgBlbdTpDvC": "19",
                "bgdAlncPdC": "AAP100",
                "bltnbmTitNm": "삼성 테스트 카드",
                "itgBlbdSn": "100",
                "bltnStrtdt": "20260825",
                "bltnbmWrteDt": "20260820",
                "sysFstRgTs": "20260820123456789012",
                "sysLtChTs": "20260825150000000001",
                "uploadFileList": [
                    {
                        "apnFileNm": "삼성 테스트 카드 이용안내장.zip",
                        "apnFileGrpNo": "000000000000000000000100",
                        "apnFileGrpNoE": "TOKEN100%2B%2F%3D",
                        "apnFileSn": "1",
                    }
                ],
            }
        ],
    }
    page = parse_listing_page(payload, discovered_at=datetime.now(UTC), page_size=10)
    assert len(page.attachments) == 1
    assert page.attachments[0].file_name.endswith(".zip")


def test_real_bc_zip_reaches_common_policy():
    from test_issuers_lotte_bc import bc_html, bc_row

    from cardrag_worker.issuers.bc import parse_listing as parse_bc_listing

    html = bc_html(bc_row(name="BC Zip Card", guides=(("안내장 받기", "bc_guide.zip"),)))
    listing = parse_bc_listing(html, discovered_at=datetime.now(UTC))
    records = listing.records
    assert len(records) == 1
    assert records[0].is_zip
    assert records[0].file_name.endswith(".zip")


def test_cli_payload_exposes_unsupported_attachment_count():
    from cardrag_worker.cli import _pipeline_result_payload
    from cardrag_worker.pipeline import PipelineResult

    res = PipelineResult(
        run_id="run123",
        status="succeeded",
        corpus_sha256="csha",
        contract_sha256="ksha",
        generation_id="gen123",
        document_count=5,
        evidence_count=10,
        unsupported_attachment_count=2,
        collection_status="degraded",
    )
    payload = _pipeline_result_payload(res)
    assert payload["unsupported_attachment_count"] == 2
    assert payload["collection_status"] == "degraded"


async def test_zip_error_survives_own_seal_resume(corpus, monkeypatch):
    from cardrag_worker.acquisition import AcquisitionSource
    from cardrag_worker.partial_execution import ExecutionPlan, ReuseSource
    from cardrag_worker.pipeline import SealedPublicationResumer, WorkerPipeline

    factory, prior, state, pdf_calls, embed_calls, ocr, dav = corpus
    _activate_remote(dav, prior)
    runner = factory()
    base_adapter = runner.adapters[0]
    base_rec = base_adapter.record

    card_pdf = replace(
        base_rec,
        product_code="CARD_COLLECT_PDF",
        source_post_id="post_col_pdf",
        file_name="doc.pdf",
        source_url="https://cards.example/doc.pdf",
    )
    card_zip = replace(
        base_rec,
        product_code="CARD_COLLECT_ZIP",
        source_post_id="post_col_zip",
        file_name="doc.zip",
        source_url="https://cards.example/doc.zip",
        metadata={"attachment_type": "zip"},
    )

    adapter = _Adapter(card_pdf)

    async def mock_discover(client):
        return _make_snapshot(adapter, [card_pdf, card_zip])

    monkeypatch.setattr(adapter, "discover_current", mock_discover)

    # Phase 1: Collect
    collect_pipe = WorkerPipeline(
        state=state,
        state_dir=prior.root,
        adapters=[adapter],
        ocr=runner.ocr,
        embeddings=runner.embeddings,
        webdav=runner.webdav,
        maximum_attempts=1,
        execution_plan=ExecutionPlan(phase="collect"),
    )
    collect_res = await collect_pipe.run()
    assert collect_res.status == "collected"
    assert collect_res.unsupported_attachment_count == 1
    assert collect_res.collection_status == "degraded"

    # Verify acquisition.json
    acq_path = prior.root / "runs" / collect_res.run_id / "acquisition.json"
    assert acq_path.exists()
    acq_data = json.loads(acq_path.read_text())
    assert len(acq_data["unsupported_attachments"]) == 1
    assert acq_data["unsupported_attachments"][0]["product_code"] == "CARD_COLLECT_ZIP"
    assert acq_data["unsupported_attachments"][0]["format"] == "zip"

    # Phase 2: Process using AcquisitionSource
    acq_source = AcquisitionSource(prior.root, collect_res.run_id, state.path)
    assert len(acq_source.unsupported_attachments) == 1

    process_pipe = WorkerPipeline(
        state=state,
        state_dir=prior.root,
        adapters=[adapter],
        ocr=runner.ocr,
        embeddings=runner.embeddings,
        webdav=runner.webdav,
        maximum_attempts=1,
        execution_plan=ExecutionPlan(frozenset({"pdf"}), collect_res.run_id, phase="process"),
        reuse_source=acq_source,
    )
    process_res = await process_pipe.run()
    assert process_res.status == "succeeded"
    assert process_res.document_count == 1
    assert (process_res.unsupported_attachment_count, process_res.collection_status) == (1, "degraded")

    published = ReuseSource(prior.root, process_res.run_id, state.path)
    _activate_remote(dav, published)
    state.finish_run(process_res.run_id, "interrupted", error="review: committed before local completion")
    resumed = WorkerPipeline(
        state=state,
        state_dir=prior.root,
        adapters=[adapter],
        ocr=runner.ocr,
        embeddings=runner.embeddings,
        webdav=dav,
        maximum_attempts=1,
        execution_plan=ExecutionPlan(frozenset({"pdf"}), collect_res.run_id, phase="process"),
        reuse_source=acq_source,
    )
    result = await resumed.run(resume_run_id=process_res.run_id)
    assert (result.unsupported_attachment_count, result.collection_status) == (1, "degraded")

    # SealedPublicationResumer also restores unsupported attachment count and status
    state.finish_run(process_res.run_id, "interrupted", error="review: test resumer")
    sealed_resumer = SealedPublicationResumer(
        state=state,
        state_dir=prior.root,
        webdav=dav,
    )
    resumer_result = await sealed_resumer._resume_publication_locked(process_res.run_id)
    assert (resumer_result.unsupported_attachment_count, resumer_result.collection_status) == (1, "degraded")


async def test_recovered_pdf_does_not_revert_to_zip_error(corpus, monkeypatch):
    import cardrag_worker.pipeline as module
    from cardrag_worker.partial_execution import ReuseSource
    from cardrag_worker.pipeline import WorkerPipeline

    factory, prior, state, pdf_calls, embed_calls, ocr, dav = corpus
    _activate_remote(dav, prior)
    initial = factory()
    base = initial.adapters[0].record
    zipped = replace(
        base,
        product_code="zip-later-pdf",
        source_post_id="zip-later-pdf",
        file_name="same.zip",
        source_url="https://cards.example/same.zip",
        metadata={"attachment_type": "zip"},
    )
    adapter = _Adapter(base)

    async def discover(client):
        return _make_snapshot(adapter, [base, zipped])

    monkeypatch.setattr(adapter, "discover_current", discover)

    def runner():
        return WorkerPipeline(
            state=state,
            state_dir=prior.root,
            adapters=[adapter],
            ocr=ocr,
            embeddings=initial.embeddings,
            webdav=dav,
            maximum_attempts=1,
        )

    recovered = await runner().run()
    assert (recovered.unsupported_attachment_count, recovered.collection_status) == (1, "degraded")
    before = len(pdf_calls)

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(days=100)

    monkeypatch.setattr(module, "datetime", Later)
    # Origin mock now serves a valid PDF even at the unchanged .zip URL.
    recovered = await runner().run()
    assert any("same.zip" in url for url in pdf_calls[before:]), (
        "No bounded origin recheck even after 100 days"
    )
    assert (recovered.unsupported_attachment_count, recovered.collection_status) == (0, "complete")

    published = ReuseSource(prior.root, recovered.run_id, state.path)
    _activate_remote(dav, published)
    call_count_before = len(pdf_calls)
    again = await runner().run()
    assert (again.unsupported_attachment_count, again.collection_status) == (0, "complete")
    assert len(pdf_calls) == call_count_before


async def test_woori_zip_recheck_download_preparation():
    import pytest
    from test_issuers import WooriClient

    from cardrag_worker.issuers.woori import WooriAdapter, parse_detail_records, raonk_plaintext

    client = WooriClient()

    # 1. .zip URL generates valid POST DownloadRequest
    zip_rec = parse_detail_records(
        {
            "filePath": "/upload/changed.zip",
            "fileNm": "changed.zip",
            "beginDt": "20261001",
            "gdccVer": "v1",
        },
        product_code="P1",
        product_name="Card",
        discovered_at=datetime.now(UTC),
    )[0]
    zip_req = await WooriAdapter().prepare_download(client, zip_rec)
    assert zip_req.method == "POST"

    # 2. Normal sibling .pdf is unaffected and valid
    pdf_rec = parse_detail_records(
        {
            "filePath": "/upload/normal.pdf",
            "fileNm": "normal.pdf",
            "beginDt": "20261001",
            "gdccVer": "v1",
        },
        product_code="P2",
        product_name="Card2",
        discovered_at=datetime.now(UTC),
    )[0]
    pdf_req = await WooriAdapter().prepare_download(client, pdf_rec)
    assert pdf_req.method == "POST"

    # 3. Disallowed extension (e.g. .exe) is rejected by raonk_plaintext guard
    with pytest.raises(ValueError, match="invalid RAONK file name"):
        raonk_plaintext("/upload/bad.exe", "bad.exe")


async def test_publication_only_resume_before_execution_result_written(corpus, monkeypatch):
    import json
    from types import SimpleNamespace

    import pytest
    from cardrag_core import GenerationManifest

    from cardrag_worker.acquisition import AcquisitionSource
    from cardrag_worker.partial_execution import ExecutionPlan
    from cardrag_worker.pipeline import SealedPublicationResumer, WorkerPipeline

    factory, prior, state, pdf_calls, embed_calls, ocr, dav = corpus
    _activate_remote(dav, prior)
    runner = factory()
    base_adapter = runner.adapters[0]
    base_rec = base_adapter.record

    card_pdf = replace(
        base_rec,
        product_code="CARD_COLLECT_PDF",
        source_post_id="post_col_pdf",
        file_name="doc.pdf",
        source_url="https://cards.example/doc.pdf",
    )
    card_zip = replace(
        base_rec,
        product_code="CARD_COLLECT_ZIP",
        source_post_id="post_col_zip",
        file_name="doc.zip",
        source_url="https://cards.example/doc.zip",
        metadata={"attachment_type": "zip"},
    )

    adapter = _Adapter(card_pdf)

    async def mock_discover(client):
        return _make_snapshot(adapter, [card_pdf, card_zip])

    monkeypatch.setattr(adapter, "discover_current", mock_discover)

    # Phase 1: Collect
    collect_pipe = WorkerPipeline(
        state=state,
        state_dir=prior.root,
        adapters=[adapter],
        ocr=runner.ocr,
        embeddings=runner.embeddings,
        webdav=runner.webdav,
        maximum_attempts=1,
        execution_plan=ExecutionPlan(phase="collect"),
    )
    collect_res = await collect_pipe.run()
    assert collect_res.status == "collected"
    assert collect_res.unsupported_attachment_count == 1
    assert collect_res.collection_status == "degraded"

    # Verify acquisition.json
    acq_path = prior.root / "runs" / collect_res.run_id / "acquisition.json"
    assert acq_path.exists()
    acq_data = json.loads(acq_path.read_text())
    assert len(acq_data["unsupported_attachments"]) == 1
    assert acq_data["unsupported_attachments"][0]["product_code"] == "CARD_COLLECT_ZIP"
    assert acq_data["unsupported_attachments"][0]["format"] == "zip"

    # Phase 2: Process using AcquisitionSource
    acq_source = AcquisitionSource(prior.root, collect_res.run_id, state.path)
    assert len(acq_source.unsupported_attachments) == 1

    process_pipe = WorkerPipeline(
        state=state,
        state_dir=prior.root,
        adapters=[adapter],
        ocr=runner.ocr,
        embeddings=runner.embeddings,
        webdav=runner.webdav,
        maximum_attempts=1,
        execution_plan=ExecutionPlan(frozenset({"pdf"}), collect_res.run_id, phase="process"),
        reuse_source=acq_source,
    )

    class ProcessExit(BaseException):
        pass

    original_finish = state.finish_run

    def interrupt_finish(run_id, status, **kwargs):
        if status == "succeeded":
            raise ProcessExit()
        return original_finish(run_id, status, **kwargs)

    monkeypatch.setattr(state, "finish_run", interrupt_finish)
    with pytest.raises(ProcessExit):
        await process_pipe.run()
    monkeypatch.setattr(state, "finish_run", original_finish)
    run_id = state.connection.execute("SELECT run_id FROM run ORDER BY started_at DESC LIMIT 1").fetchone()[0]
    assert state.run_status(run_id) == "running"
    assert not (prior.root / "runs" / run_id / "execution-result.json").exists()
    seal = json.loads((prior.root / "runs" / run_id / "sealed/publish.json").read_text())
    manifest = GenerationManifest.model_validate_json(json.dumps(seal["manifest"]))
    assert json.loads(dav.objects[str(dav.pointer_path)])["generation_id"] == manifest.generation_id
    _activate_remote(dav, SimpleNamespace(manifest=manifest))
    resumer = SealedPublicationResumer(state=state, state_dir=prior.root, webdav=dav)
    result = await resumer._resume_publication_locked(run_id)
    assert (result.unsupported_attachment_count, result.collection_status) == (1, "degraded")


async def test_normal_run_publication_resume_keeps_zip_error(corpus, monkeypatch):
    import zipfile

    from cardrag_worker.partial_execution import ReuseSource
    from cardrag_worker.pipeline import SealedPublicationResumer, WorkerPipeline

    def forbidden_zipfile(*args, **kwargs):
        raise AssertionError("zipfile was invoked! ZIP archive must NEVER be unzipped.")

    monkeypatch.setattr(zipfile, "ZipFile", forbidden_zipfile)

    factory, prior, state, pdf_calls, embed_calls, ocr, dav = corpus
    _activate_remote(dav, prior)
    runner = factory()
    base_adapter = runner.adapters[0]
    base_rec = base_adapter.record

    # Card 1: Normal PDF
    card1 = replace(
        base_rec,
        product_code="CARD_NORMAL",
        source_post_id="post_normal",
        file_name="normal.pdf",
        source_url="https://cards.example/normal.pdf",
    )
    # Card 2: Explicit ZIP
    card2 = replace(
        base_rec,
        product_code="CARD_ZIP",
        source_post_id="post_zip",
        file_name="manual.zip",
        source_url="https://cards.example/manual.zip",
        metadata={"attachment_type": "zip"},
    )

    mixed_adapter = _Adapter(card1)
    snapshot_records = [card1, card2]

    async def mock_discover(client):
        return _make_snapshot(mixed_adapter, snapshot_records)

    monkeypatch.setattr(mixed_adapter, "discover_current", mock_discover)

    pipe = WorkerPipeline(
        state=state,
        state_dir=prior.root,
        adapters=[mixed_adapter],
        ocr=runner.ocr,
        embeddings=runner.embeddings,
        webdav=runner.webdav,
        maximum_attempts=1,
    )

    pdf_requests_before = len(pdf_calls)

    result = await pipe.run()

    # Verify Card 2 (ZIP) had 0 body GETs, 0 OCR, 0 embeddings
    zip_gets = [url for url in pdf_calls[pdf_requests_before:] if "manual.zip" in url]
    assert len(zip_gets) == 0, "Explicit ZIP must skip body download"
    assert result.unsupported_attachment_count == 1
    assert result.collection_status == "degraded"
    assert result.status in {"succeeded", "no_change"}

    # Card 1 (PDF) succeeded and is published in manifest
    current_remote = await dav.validated_current_generation()
    assert current_remote is not None
    assert result.document_count == 1
    published = ReuseSource(prior.root, result.run_id, state.path)
    _activate_remote(dav, published)
    assert not (prior.root / "runs" / result.run_id / "execution-result.json").exists()
    state.finish_run(result.run_id, "interrupted", error="review: publication acknowledgement lost")
    resumer = SealedPublicationResumer(state=state, state_dir=prior.root, webdav=dav)
    resumed = await resumer._resume_publication_locked(result.run_id)
    assert (resumed.unsupported_attachment_count, resumed.collection_status) == (1, "degraded")
