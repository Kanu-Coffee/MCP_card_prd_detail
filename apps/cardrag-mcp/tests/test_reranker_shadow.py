from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import httpx
import numpy as np
import pytest
from conftest import FakeEmbedder, create_database, unit_vector
from v5_fixtures import install_v5_fixture

from cardrag_mcp.models import ContractSearchRequest, SearchRequest
from cardrag_mcp.repository import ServingRepository
from cardrag_mcp.reranker import (
    RERANKER_CANONICAL_RESPONSE_MODEL,
    RERANKER_MODEL,
    OpenRouterReranker,
    RerankerCandidate,
    RerankerShadowError,
    RerankerShadowLane,
    RerankerShadowStore,
)
from cardrag_mcp.store import GenerationStore, load_generation_handle


class _ChunkedResponse(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'{"provider_secret":"'
        yield b"x" * 128
        yield b'"}'


def _client(
    handler: httpx.MockTransport,
) -> tuple[OpenRouterReranker, httpx.AsyncClient]:
    http_client = httpx.AsyncClient(
        base_url="https://openrouter.example/api/v1/",
        transport=handler,
    )
    return (
        OpenRouterReranker(
            base_url="https://openrouter.example/api/v1",
            api_key="test-secret",
            timeout_seconds=10,
            client=http_client,
        ),
        http_client,
    )


def _candidates() -> tuple[RerankerCandidate, ...]:
    return (
        RerankerCandidate(
            candidate_id="candidate-a",
            contract_revision_id="revision-a",
            node_id="node-a",
            display_text="공항 라운지 월 1회",
            dense_rank=1,
            dense_score=0.9,
            matched_view_types=("RAW_ITEM",),
        ),
        RerankerCandidate(
            candidate_id="candidate-b",
            contract_revision_id="revision-b",
            node_id="node-b",
            display_text="전월 실적 제외 조건",
            dense_rank=2,
            dense_score=0.8,
            matched_view_types=("CONTEXTUAL_ITEM", "DETAIL"),
        ),
    )


@pytest.mark.asyncio
async def test_openrouter_reranker_pins_fireworks_and_accepts_live_canonical_alias() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["authorization"]
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": RERANKER_CANONICAL_RESPONSE_MODEL,
                "provider": "Fireworks",
                "results": [
                    {"index": 1, "relevance_score": 0.99},
                    {"index": 0, "relevance_score": 0.25},
                ],
            },
        )

    reranker, http_client = _client(httpx.MockTransport(handler))
    scores = await reranker.rerank("실적 조건", [item.display_text for item in _candidates()])

    assert captured == {
        "url": "https://openrouter.example/api/v1/rerank",
        "authorization": "Bearer test-secret",
        "payload": {
            "documents": [item.display_text for item in _candidates()],
            "model": RERANKER_MODEL,
            "provider": {
                "order": ["fireworks"],
                "only": ["fireworks"],
                "allow_fallbacks": False,
                "require_parameters": False,
            },
            "query": "실적 조건",
            "top_n": 2,
        },
    }
    assert [(item.index, item.relevance_score) for item in scores] == [
        (1, 0.99),
        (0, 0.25),
    ]
    await http_client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response_payload",
    (
        {
            "model": "other/model",
            "provider": "Fireworks",
            "results": [
                {"index": 0, "relevance_score": 0.9},
                {"index": 1, "relevance_score": 0.8},
            ],
        },
        {
            "model": RERANKER_MODEL,
            "provider": "Together",
            "results": [
                {"index": 0, "relevance_score": 0.9},
                {"index": 1, "relevance_score": 0.8},
            ],
        },
        {
            "model": RERANKER_MODEL,
            "provider": "Fireworks",
            "results": [{"index": 0, "relevance_score": 0.9}],
        },
        {
            "model": RERANKER_MODEL,
            "provider": "Fireworks",
            "results": [
                {"index": 0, "relevance_score": 0.9},
                {"index": 0, "relevance_score": 0.8},
            ],
        },
        {
            "model": RERANKER_MODEL,
            "provider": "Fireworks",
            "results": [
                {"index": 0, "relevance_score": 0.8},
                {"index": 1, "relevance_score": 0.9},
            ],
        },
        {
            "model": RERANKER_MODEL,
            "provider": "Fireworks",
            "results": [
                {"index": 0, "relevance_score": math.nan},
                {"index": 1, "relevance_score": 0.8},
            ],
        },
    ),
)
async def test_openrouter_reranker_rejects_unbound_or_incomplete_responses(
    response_payload: dict[str, object],
) -> None:
    reranker, http_client = _client(
        httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                content=json.dumps(response_payload).encode(),
                headers={"content-type": "application/json"},
            )
        )
    )

    with pytest.raises(RerankerShadowError) as captured:
        await reranker.rerank("질의", [item.display_text for item in _candidates()])

    assert captured.value.reason_code == "provider_contract_invalid"
    await http_client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("use_content_length", (True, False))
async def test_reranker_response_cap_stops_before_json_parsing(
    use_content_length: bool,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        if use_content_length:
            return httpx.Response(200, content=b"x" * 256)
        return httpx.Response(200, stream=_ChunkedResponse())

    http_client = httpx.AsyncClient(
        base_url="https://openrouter.example/api/v1/",
        transport=httpx.MockTransport(handler),
    )
    reranker = OpenRouterReranker(
        base_url="https://openrouter.example/api/v1",
        api_key="test-secret",
        timeout_seconds=10,
        maximum_response_bytes=64,
        client=http_client,
    )

    with pytest.raises(RerankerShadowError) as captured:
        await reranker.rerank("질의", [item.display_text for item in _candidates()])

    assert captured.value.reason_code == "provider_contract_invalid"
    assert "provider_secret" not in str(captured.value)
    await http_client.aclose()


@pytest.mark.asyncio
async def test_shadow_artifact_is_canonical_immutable_bound_and_idempotent(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "model": RERANKER_CANONICAL_RESPONSE_MODEL,
                "provider": "Fireworks",
                "results": [
                    {"index": 1, "relevance_score": 0.99},
                    {"index": 0, "relevance_score": 0.25},
                ],
            },
        )

    reranker, http_client = _client(httpx.MockTransport(handler))
    lane = RerankerShadowLane(
        reranker,
        RerankerShadowStore(tmp_path),
        maximum_candidates=2,
    )

    first = await lane.observe(
        generation_id="generation-v5",
        query="질의 원문은 artifact에 없어야 한다",
        candidates=_candidates(),
    )
    second = await lane.observe(
        generation_id="generation-v5",
        query="질의 원문은 artifact에 없어야 한다",
        candidates=_candidates(),
    )

    assert first == second
    assert first.status == "succeeded"
    assert first.candidate_count == 2
    assert first.rank_change_count == 2
    assert first.failure_reason is None
    assert calls == 1
    artifacts = list((tmp_path / "audit-reports" / "reranker-shadow").glob("*.json"))
    assert len(artifacts) == 1
    artifact = artifacts[0]
    payload = artifact.read_bytes()
    assert artifact.stat().st_mode & 0o222 == 0
    assert (
        payload
        == json.dumps(
            json.loads(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
    assert "질의 원문".encode() not in payload
    assert "공항 라운지".encode() not in payload
    parsed = json.loads(payload)
    assert parsed["identity"] == {
        "schema_version": "cardrag.reranker-shadow-identity.v1",
        "generation_id": "generation-v5",
        "query_sha256": hashlib.sha256("질의 원문은 artifact에 없어야 한다".encode()).hexdigest(),
        "model": RERANKER_MODEL,
        "provider_id": "fireworks",
        "candidate_sha256": parsed["identity"]["candidate_sha256"],
    }
    assert first.artifact_sha256 == parsed["artifact_sha256"]

    artifact.chmod(0o600)
    tampered_mode = await lane.observe(
        generation_id="generation-v5",
        query="질의 원문은 artifact에 없어야 한다",
        candidates=_candidates(),
    )
    assert tampered_mode.status == "failed"
    assert tampered_mode.failure_reason == "artifact_store_failed"
    assert tampered_mode.artifact_sha256 is None
    assert calls == 1
    await http_client.aclose()


@pytest.mark.asyncio
async def test_provider_failure_is_isolated_and_persisted_as_bounded_artifact(
    tmp_path: Path,
) -> None:
    reranker, http_client = _client(
        httpx.MockTransport(lambda _request: httpx.Response(503, text="provider secret detail"))
    )
    lane = RerankerShadowLane(
        reranker,
        RerankerShadowStore(tmp_path),
        maximum_candidates=2,
    )

    diagnostics = await lane.observe(
        generation_id="generation-v5",
        query="provider failure query",
        candidates=_candidates(),
    )

    assert diagnostics.status == "failed"
    assert diagnostics.failure_reason == "provider_request_failed"
    assert diagnostics.artifact_sha256 is not None
    artifact = next((tmp_path / "audit-reports" / "reranker-shadow").glob("*.json"))
    payload = artifact.read_text(encoding="utf-8")
    assert "provider secret detail" not in payload
    assert "provider failure query" not in payload
    assert json.loads(payload)["failure_reason"] == "provider_request_failed"
    await http_client.aclose()


@pytest.mark.asyncio
async def test_shadow_identity_separates_generation_query_and_candidate_hash(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "model": RERANKER_CANONICAL_RESPONSE_MODEL,
                "provider": "Fireworks",
                "results": [
                    {"index": 0, "relevance_score": 0.9},
                    {"index": 1, "relevance_score": 0.8},
                ],
            },
        )

    reranker, http_client = _client(httpx.MockTransport(handler))
    lane = RerankerShadowLane(
        reranker,
        RerankerShadowStore(tmp_path),
        maximum_candidates=2,
    )
    changed_candidates = list(_candidates())
    changed = changed_candidates[1]
    changed_candidates[1] = RerankerCandidate(
        candidate_id=changed.candidate_id,
        contract_revision_id=changed.contract_revision_id,
        node_id=changed.node_id,
        display_text=changed.display_text + " 변경",
        dense_rank=changed.dense_rank,
        dense_score=changed.dense_score,
        matched_view_types=changed.matched_view_types,
    )

    diagnostics = (
        await lane.observe(
            generation_id="generation-v5-a",
            query="query-a",
            candidates=_candidates(),
        ),
        await lane.observe(
            generation_id="generation-v5-a",
            query="query-b",
            candidates=_candidates(),
        ),
        await lane.observe(
            generation_id="generation-v5-b",
            query="query-a",
            candidates=_candidates(),
        ),
        await lane.observe(
            generation_id="generation-v5-a",
            query="query-a",
            candidates=changed_candidates,
        ),
    )

    assert calls == 4
    assert len({item.artifact_sha256 for item in diagnostics}) == 4
    assert len(list((tmp_path / "audit-reports" / "reranker-shadow").glob("*.json"))) == 4
    await http_client.aclose()


@pytest.mark.asyncio
async def test_reranker_store_quotas_preserve_existing_immutable_artifacts(tmp_path: Path) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "model": RERANKER_CANONICAL_RESPONSE_MODEL,
                "provider": "Fireworks",
                "results": [
                    {"index": 0, "relevance_score": 0.9},
                    {"index": 1, "relevance_score": 0.8},
                ],
            },
        )

    reranker, http_client = _client(httpx.MockTransport(handler))
    one_job_store = RerankerShadowStore(
        tmp_path,
        maximum_jobs=1,
        maximum_total_bytes=1024 * 1024,
        maximum_artifact_bytes=1024 * 1024,
    )
    lane = RerankerShadowLane(reranker, one_job_store, maximum_candidates=2)
    first = await lane.observe(
        generation_id="generation-v5",
        query="first query",
        candidates=_candidates(),
    )
    rejected_by_jobs = await lane.observe(
        generation_id="generation-v5",
        query="second query",
        candidates=_candidates(),
    )
    artifacts = list((tmp_path / "audit-reports" / "reranker-shadow").glob("*.json"))

    assert first.status == "succeeded"
    assert rejected_by_jobs.failure_reason == "artifact_store_failed"
    assert len(artifacts) == 1
    original_bytes = artifacts[0].read_bytes()

    total_bounded_store = RerankerShadowStore(
        tmp_path,
        maximum_jobs=2,
        maximum_total_bytes=len(original_bytes),
        maximum_artifact_bytes=len(original_bytes),
    )
    total_bounded_lane = RerankerShadowLane(
        reranker,
        total_bounded_store,
        maximum_candidates=2,
    )
    rejected_by_total = await total_bounded_lane.observe(
        generation_id="generation-v5",
        query="third query",
        candidates=_candidates(),
    )
    loaded_again = await total_bounded_lane.observe(
        generation_id="generation-v5",
        query="first query",
        candidates=_candidates(),
    )

    assert rejected_by_total.failure_reason == "artifact_store_failed"
    assert loaded_again.artifact_sha256 == first.artifact_sha256
    assert artifacts[0].read_bytes() == original_bytes

    artifact_bounded_root = tmp_path / "artifact-bounded"
    artifact_bounded_root.mkdir()
    artifact_bounded_store = RerankerShadowStore(
        artifact_bounded_root,
        maximum_jobs=1,
        maximum_total_bytes=1024 * 1024,
        maximum_artifact_bytes=128,
    )
    artifact_bounded_lane = RerankerShadowLane(
        reranker,
        artifact_bounded_store,
        maximum_candidates=2,
    )
    rejected_by_artifact = await artifact_bounded_lane.observe(
        generation_id="generation-v5",
        query="artifact query",
        candidates=_candidates(),
    )
    assert rejected_by_artifact.failure_reason == "artifact_store_failed"
    assert not list((tmp_path / "artifact-bounded").rglob("*.json"))
    assert calls == 4
    await http_client.aclose()


@pytest.mark.asyncio
async def test_unsafe_shadow_store_is_bounded_and_does_not_escape_state(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    (state / "audit-reports").symlink_to(outside, target_is_directory=True)
    reranker, http_client = _client(
        httpx.MockTransport(lambda _request: pytest.fail("unsafe store must fail first"))
    )
    lane = RerankerShadowLane(
        reranker,
        RerankerShadowStore(state),
        maximum_candidates=2,
    )

    diagnostics = await lane.observe(
        generation_id="generation-v5",
        query="safe query",
        candidates=_candidates(),
    )

    assert diagnostics.status == "failed"
    assert diagnostics.failure_reason == "artifact_store_failed"
    assert diagnostics.artifact_sha256 is None
    assert list(outside.iterdir()) == []
    await http_client.aclose()


@pytest.mark.asyncio
async def test_invalid_candidate_has_exact_bounded_reason_without_provider_call(
    tmp_path: Path,
) -> None:
    reranker, http_client = _client(
        httpx.MockTransport(lambda _request: pytest.fail("invalid input must not reach provider"))
    )
    lane = RerankerShadowLane(
        reranker,
        RerankerShadowStore(tmp_path),
        maximum_candidates=2,
    )
    invalid = (
        RerankerCandidate(
            candidate_id="candidate-a",
            contract_revision_id="revision-a",
            node_id="node-a",
            display_text="",
            dense_rank=1,
            dense_score=0.9,
            matched_view_types=("RAW_ITEM",),
        ),
    )

    diagnostics = await lane.observe(
        generation_id="generation-v5",
        query="safe query",
        candidates=invalid,
    )

    assert diagnostics.status == "failed"
    assert diagnostics.failure_reason == "candidate_input_invalid"
    assert diagnostics.candidate_count == 1
    assert diagnostics.artifact_sha256 is None
    assert not (tmp_path / "audit-reports").exists()
    await http_client.aclose()


@pytest.mark.asyncio
async def test_exact_shadow_changes_only_diagnostics_and_never_dense_ranking(
    tmp_path: Path,
) -> None:
    store = GenerationStore(tmp_path / "state", maximum_vector_bytes=2 * 1024 * 1024)
    install_v5_fixture(store)
    vector = np.zeros((4096,), dtype=np.float32)
    vector[0] = 1.0
    baseline = ServingRepository(
        store,
        FakeEmbedder(vector),
        cursor_secret=b"reranker-baseline-cursor-secret",
        maximum_candidates=20,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        count = len(json.loads(request.content)["documents"])
        return httpx.Response(
            200,
            json={
                "model": RERANKER_CANONICAL_RESPONSE_MODEL,
                "provider": "Fireworks",
                "results": [
                    {"index": index, "relevance_score": float(index)}
                    for index in reversed(range(count))
                ],
            },
        )

    reranker, http_client = _client(httpx.MockTransport(handler))
    shadow = ServingRepository(
        store,
        FakeEmbedder(vector),
        cursor_secret=b"reranker-shadow-cursor-secret-value",
        maximum_candidates=20,
        reranker_shadow=RerankerShadowLane(
            reranker,
            RerankerShadowStore(store.root),
            maximum_candidates=20,
        ),
    )
    request = ContractSearchRequest(query="공항 혜택", limit=10)

    baseline_result = await baseline.search_contracts(request)
    shadow_result = await shadow.search_contracts(request)

    assert shadow_result.bundles == baseline_result.bundles
    assert shadow_result.coverage.reranker_influenced_ranking is False
    assert shadow_result.coverage.reranker_shadow_status == "succeeded"
    assert shadow_result.coverage.reranker_shadow_rank_change_count == 2
    baseline_coverage = baseline_result.coverage.model_dump(
        mode="json", exclude={"exact_search_milliseconds"}
    )
    shadow_coverage = shadow_result.coverage.model_dump(
        mode="json",
        exclude={
            "exact_search_milliseconds",
            "reranker_shadow_status",
            "reranker_shadow_candidate_count",
            "reranker_shadow_rank_change_count",
            "reranker_shadow_artifact_sha256",
            "reranker_shadow_failure_reason",
        },
    )
    assert shadow_coverage == baseline_coverage
    await http_client.aclose()


@pytest.mark.asyncio
async def test_provider_failure_never_fails_primary_exact_search(tmp_path: Path) -> None:
    store = GenerationStore(tmp_path / "state", maximum_vector_bytes=2 * 1024 * 1024)
    install_v5_fixture(store)
    vector = np.zeros((4096,), dtype=np.float32)
    vector[0] = 1.0
    reranker, http_client = _client(
        httpx.MockTransport(lambda _request: httpx.Response(500, text="do not leak"))
    )
    repository = ServingRepository(
        store,
        FakeEmbedder(vector),
        cursor_secret=b"reranker-provider-failure-cursor",
        maximum_candidates=20,
        reranker_shadow=RerankerShadowLane(
            reranker,
            RerankerShadowStore(store.root),
            maximum_candidates=20,
        ),
    )

    result = await repository.search_contracts(ContractSearchRequest(query="전월 실적"))

    assert result.bundles
    assert result.coverage.reranker_influenced_ranking is False
    assert result.coverage.reranker_shadow_status == "failed"
    assert result.coverage.reranker_shadow_failure_reason == "provider_request_failed"
    assert result.coverage.reranker_shadow_artifact_sha256 is not None
    await http_client.aclose()


@pytest.mark.asyncio
async def test_exhaustive_interruption_does_not_shadow_until_resumed_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = GenerationStore(tmp_path / "state", maximum_vector_bytes=2 * 1024 * 1024)
    install_v5_fixture(store)
    vector = np.zeros((4096,), dtype=np.float32)
    vector[0] = 1.0
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        count = len(json.loads(request.content)["documents"])
        return httpx.Response(
            200,
            json={
                "model": RERANKER_CANONICAL_RESPONSE_MODEL,
                "provider": "Fireworks",
                "results": [
                    {"index": index, "relevance_score": float(count - index)}
                    for index in range(count)
                ],
            },
        )

    reranker, http_client = _client(httpx.MockTransport(handler))
    repository = ServingRepository(
        store,
        FakeEmbedder(vector),
        cursor_secret=b"reranker-exhaustive-resume-cursor",
        maximum_candidates=20,
        reranker_shadow=RerankerShadowLane(
            reranker,
            RerankerShadowStore(store.root),
            maximum_candidates=20,
        ),
    )
    request = ContractSearchRequest(query="전체 카드 비교", mode="exhaustive")
    original_checkpoint = repository.exact.audit_store.checkpoint

    def checkpoint_then_interrupt(*args: object, **kwargs: object) -> object:
        ledger = original_checkpoint(*args, **kwargs)  # type: ignore[arg-type]
        if len(ledger.completed_contracts) == 1:
            raise RuntimeError("interrupt before shadow")
        return ledger

    monkeypatch.setattr(repository.exact.audit_store, "checkpoint", checkpoint_then_interrupt)
    with pytest.raises(RuntimeError, match="interrupt before shadow"):
        await repository.search_contracts(request)
    assert calls == 0

    monkeypatch.setattr(repository.exact.audit_store, "checkpoint", original_checkpoint)
    resumed = await repository.search_contracts(request)
    completed_again = await repository.search_contracts(request)

    assert calls == 1
    assert resumed.coverage.exhaustive_resumed is True
    assert resumed.coverage.reranker_shadow_status == "succeeded"
    assert completed_again.coverage.reranker_shadow_artifact_sha256 == (
        resumed.coverage.reranker_shadow_artifact_sha256
    )
    await http_client.aclose()


@pytest.mark.asyncio
async def test_v4_compatibility_search_never_invokes_or_writes_reranker_shadow(
    tmp_path: Path,
) -> None:
    store = GenerationStore(tmp_path / "state", maximum_vector_bytes=1024 * 1024)
    directory = store.generations / "gen-v4-reranker-isolation"
    create_database(
        directory / "index.sqlite3",
        directory.name,
        schema_id="cardrag.serving-db.v4",
    )
    store.activate(
        load_generation_handle(
            directory,
            store.objects,
            maximum_vector_bytes=store.maximum_vector_bytes,
        )
    )
    reranker, http_client = _client(
        httpx.MockTransport(lambda _request: pytest.fail("v4 must not invoke reranker"))
    )
    repository = ServingRepository(
        store,
        FakeEmbedder(unit_vector(0)),
        cursor_secret=b"reranker-v4-isolation-cursor-secret",
        maximum_candidates=20,
        reranker_shadow=RerankerShadowLane(
            reranker,
            RerankerShadowStore(store.root),
            maximum_candidates=20,
        ),
    )

    result = await repository.search(SearchRequest(query="airport"))

    assert result.retrieval_mode == "hybrid"
    assert result.items
    assert not (store.root / "audit-reports" / "reranker-shadow").exists()
    await http_client.aclose()
