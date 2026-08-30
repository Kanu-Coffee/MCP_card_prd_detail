from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, cast

import httpx
import numpy as np
import pytest
from cardrag_core import (
    QUERY_EMBEDDING_PREFIX,
    ArtifactRef,
    EmbeddingContract,
    GenerationCounts,
    GenerationDocument,
    GenerationManifest,
    IssuerOCRCounts,
    canonical_json_bytes,
    canonical_sha256,
    format_qwen3_query,
    generation_database_path,
    qwen3_embedding_profile_id,
    sha256_bytes,
)
from conftest import GenerationFixture, create_database

import cardrag_mcp.external_gold_producer as producer
from cardrag_mcp.evaluation import V109_BASELINE_COMMIT
from cardrag_mcp.external_gold_producer import (
    EMBEDDING_REPLAY_ROW_SCHEMA,
    EMBEDDING_REPLAY_SCHEMA,
    EmbeddingReplayManifest,
    EmbeddingReplayRow,
    ProviderReceipt,
    ProviderResponseArtifact,
    build_qwen_page_corpus,
    build_v109_inventory,
    capture_qwen_embedding_replay,
    capture_v109_query_embedding_replay,
    produce_external_observation,
)
from cardrag_mcp.gold_capture import (
    AnswerArtifactManifest,
    AnswerRecord,
    ArtifactBinding,
    CorpusInventoryManifest,
    CorpusInventoryRow,
    GoldCaptureError,
    seal_external_observation,
)


def _binding(path: Path) -> ArtifactBinding:
    body = path.read_bytes()
    return ArtifactBinding(sha256=hashlib.sha256(body).hexdigest(), size_bytes=len(body))


def _write_jsonl(path: Path, records: list[object]) -> ArtifactBinding:
    body = b"".join(canonical_json_bytes(record) + b"\n" for record in records)
    path.write_bytes(body)
    return _binding(path)


def _unit_vector(dimension: int, index: int = 0) -> np.ndarray:
    vector = np.zeros((dimension,), dtype="<f4")
    vector[index] = 1.0
    return vector


def _maximum_identifier(index: int) -> str:
    prefix = f"row-{index:06d}-"
    return prefix + "x" * (512 - len(prefix))


def _raw_embedding_envelope(
    payload: object,
    *,
    provider_header: str | None = None,
) -> producer.EmbeddingRawResponseEnvelope:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return producer.EmbeddingRawResponseEnvelope(
        schema_version="cardrag.gold-embedding-provider-response.v1",
        status_code=200,
        provider_header=provider_header,
        body_sha256=hashlib.sha256(body).hexdigest(),
        body_size_bytes=len(body),
        body_base64=base64.b64encode(body).decode("ascii"),
    )


def _provider_receipt(
    path: Path,
    *,
    model: str,
    provider_id: str | None,
    inputs: list[tuple[str, str, np.ndarray]],
    source_generation_id: str | None = None,
    source_generation_manifest: ArtifactBinding | None = None,
    source_serving_database: ArtifactBinding | None = None,
) -> ArtifactBinding:
    typed_model = cast(
        Literal["openai/text-embedding-3-small", "qwen/qwen3-embedding-8b"],
        model,
    )
    typed_provider = cast(Literal["deepinfra", "nebius"] | None, provider_id)
    batches = [[row] for row in inputs] if provider_id is None else [inputs]
    requests: list[producer.ProviderRequestRecord] = []
    response_artifacts: list[ProviderResponseArtifact] = []
    for ordinal, batch in enumerate(batches):
        raw_response = path.parent / f"{path.stem}.raw-response-{ordinal:06d}.json"
        payload: dict[str, object] = {
            "data": [
                {"embedding": vector.astype("<f4", copy=False).tolist(), "index": index}
                for index, (_input_id, _formatted, vector) in enumerate(batch)
            ]
        }
        provider_header: str | None = None
        if provider_id is not None:
            payload["model"] = "Qwen/Qwen3-Embedding-8B"
            provider_header = "DeepInfra" if provider_id == "deepinfra" else "Nebius"
        envelope = _raw_embedding_envelope(payload, provider_header=provider_header)
        raw_response.write_bytes(envelope.canonical_bytes())
        response_artifacts.append(
            ProviderResponseArtifact(file_name=raw_response.name, artifact=_binding(raw_response))
        )
        request_body = producer._provider_request_body(
            model=typed_model,
            provider_id=typed_provider,
            formatted_inputs=tuple(formatted for _input_id, formatted, _vector in batch),
        )
        requests.append(
            producer._provider_request_record(
                ordinal=ordinal,
                input_ids=tuple(input_id for input_id, _formatted, _vector in batch),
                request_body=request_body,
                response_file_name=raw_response.name,
            )
        )
    response_set_sha256 = canonical_sha256(
        {
            "artifacts": [item.model_dump(mode="json") for item in response_artifacts],
            "schema_version": "cardrag.gold-provider-responses.v1",
        }
    )
    request_contract_sha256 = producer._provider_request_contract_sha256(
        model=typed_model,
        provider_id=typed_provider,
        requests=requests,
        input_count=len(inputs),
        source_generation_id=source_generation_id,
        source_generation_manifest=source_generation_manifest,
        source_serving_database=source_serving_database,
    )
    receipt = ProviderReceipt.model_validate(
        {
            "schema_version": "cardrag.gold-provider-receipt.v1",
            "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "model": model,
            "provider_id": provider_id,
            "source_generation_id": source_generation_id,
            "source_generation_manifest": source_generation_manifest,
            "source_serving_database": source_serving_database,
            "request_contract_sha256": request_contract_sha256,
            "requests": tuple(requests),
            "response_artifact_sha256": response_set_sha256,
            "response_artifacts": tuple(response_artifacts),
            "input_count": len(inputs),
        }
    )
    path.write_bytes(receipt.canonical_bytes())
    return _binding(path)


def _embedding_replay(
    path: Path,
    *,
    lane: str,
    input_kind: str,
    source_commit: str,
    model: str,
    dimension: int,
    profile_id: str,
    query_policy: str | None,
    document_policy: str | None,
    provider_receipt: ArtifactBinding,
    inputs: list[tuple[str, str, np.ndarray]],
) -> ArtifactBinding:
    manifest = EmbeddingReplayManifest.model_validate(
        {
            "schema_version": EMBEDDING_REPLAY_SCHEMA,
            "lane": lane,
            "input_kind": input_kind,
            "synthetic": False,
            "source_commit": source_commit,
            "embedding_model": model,
            "embedding_dimension": dimension,
            "embedding_profile_id": profile_id,
            "query_policy": query_policy,
            "document_policy": document_policy,
            "provider_receipt": provider_receipt.model_dump(mode="json"),
            "record_count": len(inputs),
        }
    )
    records: list[object] = [manifest]
    for ordinal, (input_id, formatted, vector) in enumerate(inputs):
        raw = vector.astype("<f4", copy=False).tobytes()
        records.append(
            EmbeddingReplayRow(
                schema_version=EMBEDDING_REPLAY_ROW_SCHEMA,
                ordinal=ordinal,
                input_id=input_id,
                formatted_input_sha256=hashlib.sha256(formatted.encode()).hexdigest(),
                vector_f32_sha256=hashlib.sha256(raw).hexdigest(),
                vector_f32_base64=base64.b64encode(raw).decode(),
            )
        )
    return _write_jsonl(path, records)


def _rebind_replay_receipt(path: Path, receipt: ArtifactBinding) -> None:
    lines = path.read_bytes().splitlines()
    manifest = EmbeddingReplayManifest.model_validate_json(lines[0])
    rebound = EmbeddingReplayManifest.model_validate(
        {
            **manifest.model_dump(mode="python"),
            "provider_receipt": receipt,
        }
    )
    path.write_bytes(rebound.canonical_bytes() + b"\n" + b"\n".join(lines[1:]) + b"\n")


def _gold(path: Path, *, contract_id: str, span_id: str, question: str) -> ArtifactBinding:
    return _write_jsonl(
        path,
        [
            {
                "condition_groups": [],
                "contracts": [{"contract_revision_id": contract_id, "relevance": 3}],
                "expected_numeric_facts": [],
                "expected_revision_ids": [],
                "high_risk": False,
                "no_answer": False,
                "query_id": "gold-001",
                "question": question,
                "schema_version": "cardrag.gold-query.v1",
                "slices": ["benefit"],
                "spans": [
                    {
                        "contract_revision_id": contract_id,
                        "page": 1,
                        "relevance": 3,
                        "roles": ["benefit"],
                        "source_end": 1,
                        "source_start": 0,
                        "span_id": span_id,
                        "text_sha256": "c" * 64,
                    }
                ],
            }
        ],
    )


def _answers(
    path: Path,
    *,
    lane: str,
    gold_sha256: str,
    generation_id: str,
    generation_manifest_sha256: str,
    question: str,
) -> ArtifactBinding:
    manifest = AnswerArtifactManifest.model_validate(
        {
            "schema_version": "cardrag.gold-answer-artifact.v1",
            "lane": lane,
            "gold_sha256": gold_sha256,
            "query_count": 1,
            "generation_id": generation_id,
            "generation_manifest_sha256": generation_manifest_sha256,
            "answer_profile_id": "human-reviewed-fixture",
            "synthetic": False,
        }
    )
    record = AnswerRecord.model_validate(
        {
            "schema_version": "cardrag.gold-answer.v1",
            "query_id": "gold-001",
            "query_sha256": hashlib.sha256(question.encode()).hexdigest(),
            "answer": {
                "text": "검토된 답변",
                "no_answer": False,
                "citation_span_ids": [],
                "numeric_facts": [],
                "selected_revision_ids": [],
            },
        }
    )
    return _write_jsonl(path, [manifest, record])


def _v109_manifest(database: Path, fixture: GenerationFixture) -> GenerationManifest:
    database_binding = _binding(database)
    documents = []
    issuer_counts: dict[str, int] = {}
    for document_id, pdf_sha256, pdf_size, _pdf_body in fixture.documents:
        issuer = next(row[1] for row in fixture.document_contracts if row[0] == document_id)
        issuer_counts[issuer] = issuer_counts.get(issuer, 0) + 1
        ocr_body = f"ocr:{document_id}".encode()
        documents.append(
            GenerationDocument(
                document_id=document_id,
                issuer=issuer,
                pdf=ArtifactRef.for_cas(
                    sha256=pdf_sha256,
                    size_bytes=pdf_size,
                    media_type="application/pdf",
                ),
                ocr=ArtifactRef.for_cas(
                    sha256=sha256_bytes(ocr_body),
                    size_bytes=len(ocr_body),
                    media_type="text/markdown; charset=utf-8",
                ),
                page_count=1,
                availability="available",
            )
        )
    with sqlite3.connect(database) as connection:
        evidence_count = int(connection.execute("SELECT count(*) FROM evidence").fetchone()[0])
    return GenerationManifest(
        schema_version="cardrag.generation.v4",
        generation_id=fixture.generation_id,
        created_at=datetime(2026, 8, 30, tzinfo=UTC),
        serving_schema="cardrag.serving-db.v4",
        serving_database=ArtifactRef(
            sha256=database_binding.sha256,
            size_bytes=database_binding.size_bytes,
            media_type="application/vnd.sqlite3",
            path=generation_database_path(fixture.generation_id).as_posix(),
        ),
        corpus_sha256=fixture.corpus_sha256,
        contract_sha256=fixture.contract_sha256,
        embedding_contract=EmbeddingContract(
            provider="openrouter",
            model="openai/text-embedding-3-small",
            dimension=1536,
            count=evidence_count,
        ),
        issuer_codes=tuple(sorted(issuer_counts)),
        counts=GenerationCounts(
            documents=len(documents),
            pdf_objects=len(documents),
            ocr_objects=len(documents),
            chunks=evidence_count,
        ),
        documents=tuple(sorted(documents, key=lambda row: row.document_id)),
        issuer_ocr_counts=tuple(
            IssuerOCRCounts(issuer=issuer, acquired=count, succeeded=count, failed=0)
            for issuer, count in sorted(issuer_counts.items())
        ),
    )


def test_v109_producer_extracts_worker_seal_and_passes_external_validator(tmp_path: Path) -> None:
    fixture = create_database(
        tmp_path / "source" / "index.sqlite3",
        "generation-v109",
        schema_id="cardrag.serving-db.v4",
    )
    manifest = _v109_manifest(fixture.database, fixture)
    worker_seal = (
        tmp_path / "state" / "runs" / "2208f0c6076649c4be915be182422b6a" / "sealed" / "publish.json"
    )
    worker_seal.parent.mkdir(parents=True)
    worker_seal.write_bytes(
        canonical_json_bytes(
            {
                "manifest": manifest.model_dump(mode="json"),
                "schema_version": "cardrag.worker-publish-seal.fixture.v1",
            }
        )
    )
    standalone = tmp_path / "v109-manifest.json"
    inventory = tmp_path / "v109-inventory.jsonl"
    rejected_manifest = tmp_path / "rejected-v109-manifest.json"
    rejected_inventory = tmp_path / "rejected-v109-inventory.jsonl"
    with pytest.raises(GoldCaptureError) as rejected:
        build_v109_inventory(
            generation_manifest_source_path=worker_seal,
            generation_manifest_output_path=rejected_manifest,
            database_path=fixture.database,
            inventory_output_path=rejected_inventory,
            expected_run_id=producer.V109_PRESERVED_RUN_ID,
            expected_generation_id=producer.V109_PRESERVED_GENERATION_ID,
            expected_publish_sha256=producer.V109_PRESERVED_PUBLISH_SHA256,
            expected_manifest_sha256=producer.V109_PRESERVED_MANIFEST_SHA256,
            expected_database_sha256=producer.V109_PRESERVED_DATABASE_SHA256,
        )
    assert rejected.value.code in {
        "v109_preserved_source_anchor_mismatch",
        "v109_preserved_publish_anchor_mismatch",
    }
    assert not rejected_manifest.exists()
    assert not rejected_inventory.exists()
    actual, manifest_binding, inventory_binding = build_v109_inventory(
        generation_manifest_source_path=worker_seal,
        generation_manifest_output_path=standalone,
        database_path=fixture.database,
        inventory_output_path=inventory,
        release_gate=False,
    )
    assert actual == manifest
    assert standalone.read_bytes() == manifest.canonical_bytes()

    question = "airport lounge"
    gold = tmp_path / "gold.jsonl"
    gold_binding = _gold(gold, contract_id="doc-a", span_id="ev-a", question=question)
    answers = tmp_path / "answers.jsonl"
    answers_binding = _answers(
        answers,
        lane="v109_baseline",
        gold_sha256=gold_binding.sha256,
        generation_id=manifest.generation_id,
        generation_manifest_sha256=manifest_binding.sha256,
        question=question,
    )
    receipt_path = tmp_path / "v109-provider-receipt.json"
    receipt_binding = _provider_receipt(
        receipt_path,
        model="openai/text-embedding-3-small",
        provider_id=None,
        inputs=[
            (
                "gold-001",
                QUERY_EMBEDDING_PREFIX + question,
                _unit_vector(1536),
            )
        ],
        source_generation_id=manifest.generation_id,
        source_generation_manifest=manifest_binding,
        source_serving_database=_binding(fixture.database),
    )
    replay = tmp_path / "v109-query-replay.jsonl"
    _embedding_replay(
        replay,
        lane="v109_baseline",
        input_kind="query",
        source_commit=V109_BASELINE_COMMIT,
        model="openai/text-embedding-3-small",
        dimension=1536,
        profile_id="cardrag.embedding.v109-small.v1",
        query_policy="cardrag.embedding-input.v1",
        document_policy=None,
        provider_receipt=receipt_binding,
        inputs=[("gold-001", QUERY_EMBEDDING_PREFIX + question, _unit_vector(1536))],
    )
    observation = tmp_path / "v109-observation.jsonl"
    dense_scores = tmp_path / "v109-dense-scores.f32"
    query_vectors = tmp_path / "v109-query-vectors.f32"
    lexical_ranks = tmp_path / "v109-lexical-ranks.jsonl"
    observation_binding = produce_external_observation(
        lane="v109_baseline",
        gold_path=gold,
        expected_gold_sha256=gold_binding.sha256,
        answer_artifact_path=answers,
        expected_answer_artifact_sha256=answers_binding.sha256,
        query_embedding_replay_path=replay,
        provider_receipt_path=receipt_path,
        generation_manifest_path=standalone,
        database_path=fixture.database,
        vector_path=None,
        inventory_path=inventory,
        source_commit=V109_BASELINE_COMMIT,
        output_path=observation,
        dense_score_matrix_path=dense_scores,
        query_vector_matrix_path=query_vectors,
        lexical_rank_path=lexical_ranks,
        release_gate=False,
    )
    observation_records = [json.loads(line) for line in observation.read_bytes().splitlines()]
    assert observation_records[0]["schema_version"] == (
        "cardrag.gold-external-observation-artifact.v2"
    )
    assert observation_records[0]["dense_score_matrix"] == _binding(dense_scores).model_dump(
        mode="json"
    )
    assert observation_records[0]["query_vector_matrix"] == _binding(query_vectors).model_dump(
        mode="json"
    )
    assert observation_records[0]["lexical_rank_artifact"] == _binding(lexical_ranks).model_dump(
        mode="json"
    )
    assert "raw_rows" not in observation_records[1]
    assert len(dense_scores.read_bytes()) == manifest.counts.chunks * 4
    assert len(query_vectors.read_bytes()) == 1536 * 4
    assert lexical_ranks.read_bytes().endswith(b"\n")
    assert lexical_ranks.read_bytes().count(b"\n") == 1
    receipt = seal_external_observation(
        gold_path=gold,
        expected_gold_sha256=gold_binding.sha256,
        observation_path=observation,
        expected_observation_sha256=observation_binding.sha256,
        inventory_path=inventory,
        expected_inventory_sha256=inventory_binding.sha256,
        generation_manifest_path=standalone,
        database_path=fixture.database,
        vector_path=None,
        dense_score_matrix_path=dense_scores,
        query_vector_matrix_path=query_vectors,
        lexical_rank_path=lexical_ranks,
        output_path=tmp_path / "v109-run.jsonl",
        receipt_path=tmp_path / "v109-capture-receipt.json",
        release_gate=False,
    )
    assert receipt.lane == "v109_baseline"
    assert not receipt.release_eligible
    assert receipt.dense_score_matrix == _binding(dense_scores)
    assert receipt.query_vector_matrix == _binding(query_vectors)
    assert receipt.lexical_rank_artifact == _binding(lexical_ranks)


@pytest.mark.asyncio
async def test_v109_live_replay_uses_exact_historical_http_semantics_and_resumes(
    tmp_path: Path,
) -> None:
    fixture = create_database(
        tmp_path / "source" / "index.sqlite3",
        "generation-v109",
        schema_id="cardrag.serving-db.v4",
    )
    manifest = _v109_manifest(fixture.database, fixture)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_bytes(manifest.canonical_bytes())
    question = "airport lounge"
    gold = tmp_path / "gold.jsonl"
    gold_binding = _gold(gold, contract_id="doc-a", span_id="ev-a", question=question)
    key_path = tmp_path / "openrouter.key"
    key_path.write_text("fixture-secret-key", encoding="utf-8")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = json.loads(request.content)
        assert body == {
            "dimensions": 1536,
            "encoding_format": "float",
            "input": [QUERY_EMBEDDING_PREFIX + question],
            "model": "openai/text-embedding-3-small",
        }
        assert "provider" not in body
        return httpx.Response(
            200,
            json={"data": [{"embedding": _unit_vector(1536).tolist()}]},
        )

    async with httpx.AsyncClient(
        base_url="https://openrouter.ai/api/v1/",
        transport=httpx.MockTransport(handler),
    ) as client:
        captured = await capture_v109_query_embedding_replay(
            gold_path=gold,
            expected_gold_sha256=gold_binding.sha256,
            generation_manifest_path=manifest_path,
            database_path=fixture.database,
            openrouter_base_url="https://openrouter.ai/api/v1",
            openrouter_api_key_file=key_path,
            provider_receipt_output_path=tmp_path / "v109-provider-receipt.json",
            replay_output_path=tmp_path / "v109-query-replay.jsonl",
            state_directory=tmp_path / "state",
            release_gate=False,
            client=client,
        )
    assert calls == 1
    replay_manifest, matrix, replay_binding = producer.load_embedding_replay(
        tmp_path / "v109-query-replay.jsonl",
        provider_receipt_path=tmp_path / "v109-provider-receipt.json",
        lane="v109_baseline",
        input_kind="query",
        source_commit=V109_BASELINE_COMMIT,
        embedding_profile_id="cardrag.embedding.v109-small.v1",
        expected_inputs=(("gold-001", QUERY_EMBEDDING_PREFIX + question),),
        expected_source_generation_id=manifest.generation_id,
        expected_source_generation_manifest=_binding(manifest_path),
        expected_source_serving_database=_binding(fixture.database),
    )
    assert replay_manifest.source_commit == V109_BASELINE_COMMIT
    assert np.array_equal(matrix[0], _unit_vector(1536))
    assert replay_binding == captured[1]

    def forbidden(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("historical replay resume must not call the provider")

    async with httpx.AsyncClient(
        base_url="https://openrouter.ai/api/v1/",
        transport=httpx.MockTransport(forbidden),
    ) as client:
        resumed = await capture_v109_query_embedding_replay(
            gold_path=gold,
            expected_gold_sha256=gold_binding.sha256,
            generation_manifest_path=manifest_path,
            database_path=fixture.database,
            openrouter_base_url="https://openrouter.ai/api/v1",
            openrouter_api_key_file=key_path,
            provider_receipt_output_path=tmp_path / "v109-provider-receipt.json",
            replay_output_path=tmp_path / "v109-query-replay.jsonl",
            state_directory=tmp_path / "state",
            release_gate=False,
            client=client,
        )
    assert resumed == captured


def test_v109_normalization_matches_historical_one_pass_float32_bytes() -> None:
    source = np.random.default_rng(2).normal(size=1536).astype(np.float32)
    envelope = _raw_embedding_envelope({"data": [{"embedding": source.tolist(), "index": 0}]})
    actual = producer._parse_v109_response(envelope)
    expected = np.asarray(source / np.linalg.norm(source), dtype=np.float32)
    old_two_pass = np.asarray(expected / np.linalg.norm(expected), dtype=np.float32)

    assert (
        actual.astype("<f4", copy=False).tobytes() == expected.astype("<f4", copy=False).tobytes()
    )
    assert expected.tobytes() != old_two_pass.tobytes()


def test_qwen_role_normalization_matches_mcp_query_and_worker_document_bytes() -> None:
    from cardrag_worker.embedding_v5 import _normalize_vector as worker_normalize_vector
    from cardrag_worker.state import _encode_embedding_cache_v5

    source = np.random.default_rng(1).normal(size=4096).astype(np.float32)
    values = source.tolist()
    envelope = _raw_embedding_envelope(
        {
            "data": [{"embedding": values, "index": 0}],
            "model": "Qwen/Qwen3-Embedding-8B",
        },
        provider_header="DeepInfra",
    )

    query = producer._parse_qwen_response(
        envelope,
        provider_id="deepinfra",
        input_kind="query",
        expected_count=1,
    )[0]
    expected_query = np.asarray(source / np.linalg.norm(source), dtype=np.float32)
    old_two_pass = np.asarray(
        expected_query / np.linalg.norm(expected_query),
        dtype=np.float32,
    )
    assert (
        query.astype("<f4", copy=False).tobytes()
        == expected_query.astype("<f4", copy=False).tobytes()
    )
    assert expected_query.tobytes() != old_two_pass.tobytes()

    document = producer._parse_qwen_response(
        envelope,
        provider_id="deepinfra",
        input_kind="document",
        expected_count=1,
    )[0]
    worker_values = worker_normalize_vector(values, index=0)
    expected_document = _encode_embedding_cache_v5(worker_values, dimension=4096)
    assert document.astype("<f4", copy=False).tobytes() == expected_document
    assert document.tobytes() != query.tobytes()


def test_compact_git_cap_excludes_large_local_page_corpus_artifacts() -> None:
    assert (
        producer._predicted_f32_matrix_size(
            300,
            4_175,
            code="fixture_dense",
        )
        == 5_010_000
    )
    assert (
        producer._predicted_f32_matrix_size(
            300,
            1_536,
            code="fixture_query",
        )
        == 1_843_200
    )
    with pytest.raises(GoldCaptureError) as exceeded:
        producer._predicted_f32_matrix_size(
            1,
            producer.MAX_GIT_EVIDENCE_FILE_BYTES // 4 + 1,
            code="fixture_too_large",
        )
    assert exceeded.value.code == "fixture_too_large_size_invalid"
    page_vector_size = producer._predicted_page_vector_size(5_799)
    assert page_vector_size == 95_010_816
    assert page_vector_size > producer.MAX_GIT_EVIDENCE_FILE_BYTES
    assert producer._MAX_DATABASE_BYTES > producer.MAX_GIT_EVIDENCE_FILE_BYTES
    with pytest.raises(GoldCaptureError) as local_limit:
        producer._predicted_page_vector_size(producer._MAX_VECTOR_BYTES // (4096 * 4) + 1)
    assert local_limit.value.code == "page_vectors_size_invalid"


def test_page_vector_load_forecast_supports_full_corpus_and_exact_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forecast = producer._forecast_page_vector_load(5_799)

    assert forecast.vector_size_bytes == 95_010_816
    assert forecast.peak_working_set_bytes < producer._QWEN_REPLAY_WORKING_SET_LIMIT_BYTES
    monkeypatch.setattr(
        producer,
        "_QWEN_REPLAY_WORKING_SET_LIMIT_BYTES",
        forecast.peak_working_set_bytes,
    )
    assert producer._forecast_page_vector_load(5_799) == forecast
    monkeypatch.setattr(
        producer,
        "_QWEN_REPLAY_WORKING_SET_LIMIT_BYTES",
        forecast.peak_working_set_bytes - 1,
    )
    with pytest.raises(GoldCaptureError, match="page_vector_resource_limit_exceeded"):
        producer._forecast_page_vector_load(5_799)


def test_inventory_stream_loads_full_page_count(tmp_path: Path) -> None:
    inventory = tmp_path / "inventory.jsonl"
    manifest = CorpusInventoryManifest(
        schema_version="cardrag.gold-corpus-inventory.v1",
        lane="qwen_page",
        generation_id="page-generation",
        serving_database_sha256="a" * 64,
        vector_artifact_sha256="b" * 64,
        embedding_dimension=4096,
        row_count=5_799,
    )
    records: list[object] = [manifest]
    records.extend(
        CorpusInventoryRow(
            schema_version="cardrag.gold-corpus-row.v1",
            row_index=index,
            evidence_id=f"evidence-{index:06d}",
            contract_revision_id=f"revision-{index:06d}",
            span_id=f"evidence-{index:06d}",
            input_sha256="c" * 64,
            embedding_f32_sha256="d" * 64,
        )
        for index in range(5_799)
    )
    expected = _write_jsonl(inventory, records)

    loaded_manifest, loaded_rows, binding = producer._load_inventory_rows(inventory)

    assert loaded_manifest == manifest
    assert len(loaded_rows) == 5_799
    assert loaded_rows[-1].row_index == 5_798
    assert binding == expected


def test_inventory_git_cap_rejects_before_record_parse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = tmp_path / "oversized-inventory.jsonl"
    with inventory.open("wb") as stream:
        stream.seek(producer.MAX_GIT_EVIDENCE_FILE_BYTES)
        stream.write(b"\n")
    record_reads = 0

    def forbidden_record_read(_reader: object) -> None:
        nonlocal record_reads
        record_reads += 1
        raise AssertionError("inventory cap must fail before JSONL parsing")

    monkeypatch.setattr(producer._CanonicalJsonlReader, "next_record", forbidden_record_read)
    with pytest.raises(GoldCaptureError, match="producer_inventory_size_invalid"):
        producer._load_inventory_rows(inventory)
    assert record_reads == 0


def test_provider_state_rejects_intermediate_symlink(tmp_path: Path) -> None:
    target = tmp_path / "real-state"
    target.mkdir(mode=0o700)
    link = tmp_path / "linked-state"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(GoldCaptureError, match="provider_state_ancestor_invalid"):
        producer._safe_private_state_directory(link / "capture")
    assert not (target / "capture").exists()


def _page_source_database(path: Path) -> tuple[ArtifactBinding, str, str, str]:
    generation_id = "candidate-generation"
    revision_id = "revision-current"
    document_id = "document-current"
    page_text = "혜택 안내\n" + "가" * 1700 + " 조건 안내"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) STRICT, WITHOUT ROWID;
            CREATE TABLE contract_revisions(
              contract_revision_id TEXT PRIMARY KEY,
              document_id TEXT NOT NULL,
              page_count INTEGER NOT NULL,
              temporal_status TEXT NOT NULL
            ) STRICT;
            CREATE TABLE document_pages(
              contract_revision_id TEXT NOT NULL,
              page INTEGER NOT NULL,
              text TEXT NOT NULL,
              text_sha256 TEXT NOT NULL,
              PRIMARY KEY(contract_revision_id,page)
            ) STRICT, WITHOUT ROWID;
            """
        )
        connection.executemany(
            "INSERT INTO metadata VALUES(?,?)",
            (
                ("schema_id", "cardrag.serving-db.v5"),
                ("generation_id", generation_id),
                ("current_revision_count", "1"),
            ),
        )
        connection.execute(
            "INSERT INTO contract_revisions VALUES(?,?,?,?)",
            (revision_id, document_id, 1, "current"),
        )
        connection.execute(
            "INSERT INTO document_pages VALUES(?,?,?,?)",
            (revision_id, 1, page_text, hashlib.sha256(page_text.encode()).hexdigest()),
        )
        connection.commit()
    finally:
        connection.close()
    return _binding(path), generation_id, revision_id, document_id


def _multi_revision_page_source_database(
    path: Path,
    *,
    page_texts: tuple[str, ...],
    shared_document_id: bool,
) -> tuple[ArtifactBinding, str]:
    generation_id = "candidate-generation-multi"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) STRICT, WITHOUT ROWID;
            CREATE TABLE contract_revisions(
              contract_revision_id TEXT PRIMARY KEY,
              document_id TEXT NOT NULL,
              page_count INTEGER NOT NULL,
              temporal_status TEXT NOT NULL
            ) STRICT;
            CREATE TABLE document_pages(
              contract_revision_id TEXT NOT NULL,
              page INTEGER NOT NULL,
              text TEXT NOT NULL,
              text_sha256 TEXT NOT NULL,
              PRIMARY KEY(contract_revision_id,page)
            ) STRICT, WITHOUT ROWID;
            """
        )
        connection.executemany(
            "INSERT INTO metadata VALUES(?,?)",
            (
                ("schema_id", "cardrag.serving-db.v5"),
                ("generation_id", generation_id),
                ("current_revision_count", str(len(page_texts))),
            ),
        )
        for index, page_text in enumerate(page_texts):
            revision_id = f"revision-{index:06d}"
            document_id = "document-shared" if shared_document_id else f"document-{index:06d}"
            connection.execute(
                "INSERT INTO contract_revisions VALUES(?,?,?,?)",
                (revision_id, document_id, 1, "current"),
            )
            connection.execute(
                "INSERT INTO document_pages VALUES(?,?,?,?)",
                (
                    revision_id,
                    1,
                    page_text,
                    hashlib.sha256(page_text.encode()).hexdigest(),
                ),
            )
        connection.commit()
    finally:
        connection.close()
    return _binding(path), generation_id


def _page_source_manifest_stub(
    *,
    database: ArtifactBinding,
    generation_id: str,
) -> tuple[GenerationManifest, str]:
    profile_id = qwen3_embedding_profile_id("deepinfra", maximum_tokens=8192)
    return (
        cast(
            GenerationManifest,
            SimpleNamespace(
                schema_version="cardrag.generation.v5",
                generation_id=generation_id,
                serving_database=database,
                primary_embedding_profile_id=profile_id,
                embedding_profiles=(
                    SimpleNamespace(
                        profile_id=profile_id,
                        provider="openrouter",
                        provider_id="deepinfra",
                        provider_fallback="forbidden",
                        model="qwen/qwen3-embedding-8b",
                        dimension=4096,
                        dtype="float32",
                        normalization="l2",
                        document_instruction=None,
                        query_policy="cardrag.qwen3-query.v1",
                        truncation="error",
                        maximum_tokens=8192,
                    ),
                ),
            ),
        ),
        profile_id,
    )


def test_page_input_prepare_rejects_cross_revision_duplicate_chunk_id_before_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "duplicate-page-source.sqlite3"
    binding, generation_id = _multi_revision_page_source_database(
        database,
        page_texts=("동일한 현재 약관 페이지", "동일한 현재 약관 페이지"),
        shared_document_id=True,
    )
    manifest, profile_id = _page_source_manifest_stub(
        database=binding,
        generation_id=generation_id,
    )
    monkeypatch.setattr(
        producer,
        "_load_generation_manifest",
        lambda _path: (manifest, ArtifactBinding(sha256="d" * 64, size_bytes=123)),
    )
    token_counts = 0

    def forbidden_token_count(_value: str) -> int:
        nonlocal token_counts
        token_counts += 1
        raise AssertionError("duplicate chunk IDs must fail before tokenization")

    output = tmp_path / "duplicate-page-inputs.jsonl"
    with pytest.raises(GoldCaptureError, match="page_chunk_identity_invalid"):
        producer.prepare_qwen_page_embedding_inputs(
            source_generation_manifest_path=tmp_path / "source-manifest.json",
            source_database_path=database,
            source_commit="1" * 40,
            embedding_profile_id=profile_id,
            provider_id="deepinfra",
            maximum_tokens=8192,
            output_path=output,
            token_counter=forbidden_token_count,
        )
    assert token_counts == 0
    assert not output.exists()
    assert not list(tmp_path.glob(".qwen-page-inputs-*"))


def test_page_source_summary_bounds_whitespace_page_maps_before_chunking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "whitespace-page-source.sqlite3"
    binding, generation_id = _multi_revision_page_source_database(
        database,
        page_texts=(" ",) * 8,
        shared_document_id=False,
    )
    manifest, profile_id = _page_source_manifest_stub(
        database=binding,
        generation_id=generation_id,
    )
    monkeypatch.setattr(
        producer,
        "_load_generation_manifest",
        lambda _path: (manifest, ArtifactBinding(sha256="d" * 64, size_bytes=123)),
    )
    monkeypatch.setattr(
        producer,
        "_SOURCE_PAGE_ROW_HEADROOM_BYTES",
        producer._QWEN_REPLAY_WORKING_SET_LIMIT_BYTES,
    )
    chunk_calls = 0
    token_counts = 0

    def forbidden_chunks(**_kwargs: object) -> tuple[producer.PageChunk, ...]:
        nonlocal chunk_calls
        chunk_calls += 1
        raise AssertionError("summary cap must fail before page text chunking")

    def forbidden_token_count(_value: str) -> int:
        nonlocal token_counts
        token_counts += 1
        raise AssertionError("summary cap must fail before tokenization")

    monkeypatch.setattr(producer, "_page_chunks", forbidden_chunks)
    output = tmp_path / "whitespace-page-inputs.jsonl"
    with pytest.raises(GoldCaptureError, match="page_source_resource_limit_exceeded"):
        producer.prepare_qwen_page_embedding_inputs(
            source_generation_manifest_path=tmp_path / "source-manifest.json",
            source_database_path=database,
            source_commit="1" * 40,
            embedding_profile_id=profile_id,
            provider_id="deepinfra",
            maximum_tokens=8192,
            output_path=output,
            token_counter=forbidden_token_count,
        )
    assert chunk_calls == 0
    assert token_counts == 0
    assert not output.exists()


def test_page_source_summary_rejects_large_text_before_chunking_or_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "large-page-source.sqlite3"
    binding, generation_id = _multi_revision_page_source_database(
        database,
        page_texts=("두 글자",),
        shared_document_id=False,
    )
    manifest, profile_id = _page_source_manifest_stub(
        database=binding,
        generation_id=generation_id,
    )
    monkeypatch.setattr(
        producer,
        "_load_generation_manifest",
        lambda _path: (manifest, ArtifactBinding(sha256="d" * 64, size_bytes=123)),
    )
    monkeypatch.setattr(producer, "_SOURCE_PAGE_SINGLE_TEXT_LIMIT_BYTES", 1)
    chunk_calls = 0

    def forbidden_chunks(**_kwargs: object) -> tuple[producer.PageChunk, ...]:
        nonlocal chunk_calls
        chunk_calls += 1
        raise AssertionError("source text cap must fail before page text chunking")

    monkeypatch.setattr(producer, "_page_chunks", forbidden_chunks)
    output = tmp_path / "large-page-inputs.jsonl"
    with pytest.raises(GoldCaptureError, match="page_source_resource_limit_exceeded"):
        producer.prepare_qwen_page_embedding_inputs(
            source_generation_manifest_path=tmp_path / "source-manifest.json",
            source_database_path=database,
            source_commit="1" * 40,
            embedding_profile_id=profile_id,
            provider_id="deepinfra",
            maximum_tokens=8192,
            output_path=output,
            token_counter=lambda _value: 1,
        )
    assert chunk_calls == 0
    assert not output.exists()


@pytest.mark.parametrize("oversized_field", ("metadata", "contract_revision_id"))
def test_page_source_preflights_all_selected_text_before_chunking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    oversized_field: str,
) -> None:
    database = tmp_path / f"oversized-{oversized_field}.sqlite3"
    _binding_value, generation_id = _multi_revision_page_source_database(
        database,
        page_texts=("정상 페이지",),
        shared_document_id=False,
    )
    with sqlite3.connect(database) as connection:
        if oversized_field == "metadata":
            connection.execute(
                "UPDATE metadata SET value=? WHERE key='generation_id'",
                ("x" * 4097,),
            )
        else:
            oversized_revision = "r" * 513
            connection.execute(
                "UPDATE contract_revisions SET contract_revision_id=?",
                (oversized_revision,),
            )
            connection.execute(
                "UPDATE document_pages SET contract_revision_id=?",
                (oversized_revision,),
            )
        connection.commit()
    manifest, profile_id = _page_source_manifest_stub(
        database=_binding(database),
        generation_id=generation_id,
    )
    monkeypatch.setattr(
        producer,
        "_load_generation_manifest",
        lambda _path: (manifest, ArtifactBinding(sha256="d" * 64, size_bytes=123)),
    )
    chunk_calls = 0
    token_counts = 0

    def forbidden_chunks(**_kwargs: object) -> tuple[producer.PageChunk, ...]:
        nonlocal chunk_calls
        chunk_calls += 1
        raise AssertionError("column preflight must precede page row materialization")

    def forbidden_token_count(_value: str) -> int:
        nonlocal token_counts
        token_counts += 1
        raise AssertionError("column preflight must precede tokenization")

    monkeypatch.setattr(producer, "_page_chunks", forbidden_chunks)
    output = tmp_path / "page-inputs.jsonl"
    with pytest.raises(GoldCaptureError, match="page_source_database_profile_mismatch"):
        producer.prepare_qwen_page_embedding_inputs(
            source_generation_manifest_path=tmp_path / "source-manifest.json",
            source_database_path=database,
            source_commit="1" * 40,
            embedding_profile_id=profile_id,
            provider_id="deepinfra",
            maximum_tokens=8192,
            output_path=output,
            token_counter=forbidden_token_count,
        )
    assert chunk_calls == 0
    assert token_counts == 0
    assert not output.exists()


def test_qwen_page_corpus_and_observation_pass_existing_external_validator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_database = tmp_path / "source-v5.sqlite3"
    source_binding, source_generation_id, revision_id, document_id = _page_source_database(
        source_database
    )
    source_binding_manifest = ArtifactBinding(
        sha256="d" * 64,
        size_bytes=123,
    )
    commit = "1" * 40
    profile_id = qwen3_embedding_profile_id("deepinfra", maximum_tokens=8192)
    source_manifest = SimpleNamespace(
        schema_version="cardrag.generation.v5",
        generation_id=source_generation_id,
        serving_database=source_binding,
        primary_embedding_profile_id=profile_id,
        embedding_profiles=(
            SimpleNamespace(
                profile_id=profile_id,
                provider="openrouter",
                provider_id="deepinfra",
                provider_fallback="forbidden",
                model="qwen/qwen3-embedding-8b",
                dimension=4096,
                dtype="float32",
                normalization="l2",
                document_instruction=None,
                query_policy="cardrag.qwen3-query.v1",
                truncation="error",
                maximum_tokens=8192,
            ),
        ),
    )
    producer._validate_source_qwen_profile(
        cast(GenerationManifest, source_manifest),
        embedding_profile_id=profile_id,
        provider_id="deepinfra",
        maximum_tokens=8192,
    )
    mismatched_source_manifest = SimpleNamespace(
        **{
            **vars(source_manifest),
            "primary_embedding_profile_id": "cardrag.embedding.qwen3.invalid",
        }
    )
    with pytest.raises(GoldCaptureError) as mismatch:
        producer._validate_source_qwen_profile(
            cast(GenerationManifest, mismatched_source_manifest),
            embedding_profile_id=profile_id,
            provider_id="deepinfra",
            maximum_tokens=8192,
        )
    assert mismatch.value.code == "page_source_embedding_profile_mismatch"
    monkeypatch.setattr(
        producer,
        "_load_generation_manifest",
        lambda _path: (source_manifest, source_binding_manifest),
    )
    chunks = producer._source_page_chunks(
        generation_manifest=cast(GenerationManifest, source_manifest),
        database_path=source_database,
    )
    assert len(chunks) == 2
    assert chunks[0].source_end <= 1600
    assert chunks[1].source_start == chunks[0].source_end - 160
    assert all(chunk.contract_revision_id == revision_id for chunk in chunks)
    assert all(chunk.document_id == document_id for chunk in chunks)

    page_inputs = tmp_path / "page-document-inputs.jsonl"
    page_inputs_binding = producer.prepare_qwen_page_embedding_inputs(
        source_generation_manifest_path=tmp_path / "unused-source-manifest.json",
        source_database_path=source_database,
        source_commit=commit,
        embedding_profile_id=profile_id,
        provider_id="deepinfra",
        maximum_tokens=8192,
        output_path=page_inputs,
        token_counter=lambda _value: 3,
    )
    expected_page_inputs = producer._qwen_input_payload(
        source_commit=commit,
        input_kind="document",
        embedding_profile_id=profile_id,
        provider_id="deepinfra",
        maximum_tokens=8192,
        values=tuple(
            (chunk.chunk_id, producer.format_qwen3_document(chunk.text)) for chunk in chunks
        ),
        token_counter=lambda _value: 3,
    )
    assert page_inputs.read_bytes() == expected_page_inputs
    assert page_inputs_binding == _binding(page_inputs)

    document_receipt_path = tmp_path / "document-provider-receipt.json"
    document_vectors = [_unit_vector(4096, index) for index in range(len(chunks))]
    document_receipt = _provider_receipt(
        document_receipt_path,
        model="qwen/qwen3-embedding-8b",
        provider_id="deepinfra",
        inputs=[
            (
                chunk.chunk_id,
                producer.format_qwen3_document(chunk.text),
                document_vectors[index],
            )
            for index, chunk in enumerate(chunks)
        ],
    )
    document_replay = tmp_path / "document-replay.jsonl"
    _embedding_replay(
        document_replay,
        lane="qwen_page",
        input_kind="document",
        source_commit=commit,
        model="qwen/qwen3-embedding-8b",
        dimension=4096,
        profile_id=profile_id,
        query_policy=None,
        document_policy="cardrag.page-window-1600.v1",
        provider_receipt=document_receipt,
        inputs=[
            (chunk.chunk_id, chunk.text, document_vectors[index])
            for index, chunk in enumerate(chunks)
        ],
    )
    page_database = tmp_path / "page.sqlite3"
    page_vectors = tmp_path / "page-vectors.f32"
    inventory = tmp_path / "page-inventory.jsonl"
    page_manifest = tmp_path / "page-manifest.json"
    corpus = build_qwen_page_corpus(
        source_generation_manifest_path=tmp_path / "unused-source-manifest.json",
        source_database_path=source_database,
        source_commit=commit,
        embedding_profile_id=profile_id,
        document_embedding_replay_path=document_replay,
        provider_receipt_path=document_receipt_path,
        database_output_path=page_database,
        vector_output_path=page_vectors,
        inventory_output_path=inventory,
        generation_manifest_output_path=page_manifest,
    )
    resumed = build_qwen_page_corpus(
        source_generation_manifest_path=tmp_path / "unused-source-manifest.json",
        source_database_path=source_database,
        source_commit=commit,
        embedding_profile_id=profile_id,
        document_embedding_replay_path=document_replay,
        provider_receipt_path=document_receipt_path,
        database_output_path=page_database,
        vector_output_path=page_vectors,
        inventory_output_path=inventory,
        generation_manifest_output_path=page_manifest,
    )
    assert resumed == corpus
    with sqlite3.connect(page_database) as connection:
        assert tuple(
            str(row[1]) for row in connection.execute("PRAGMA table_info(evaluation_chunks)")
        ) == (
            "row_index",
            "chunk_id",
            "contract_revision_id",
            "span_id",
            "document_id",
            "page",
            "source_start",
            "source_end",
            "text",
            "input_sha256",
        )
        metadata = {
            str(row[0]): str(row[1]) for row in connection.execute("SELECT key,value FROM metadata")
        }
        assert metadata == {
            "column_contract": "cardrag.evaluation-page-columns.v1",
            "chunking_policy": "cardrag.page-window-1600.v1",
            "embedding_dimension": "4096",
            "embedding_model": "qwen/qwen3-embedding-8b",
            "embedding_profile_id": profile_id,
            "generation_id": corpus.generation_id,
            "maximum_chars": "1600",
            "overlap_chars": "160",
            "row_count": str(len(chunks)),
            "schema_id": "cardrag.evaluation-page.v1",
            "source_commit": commit,
            "source_generation_id": source_generation_id,
            "source_generation_manifest_sha256": source_binding_manifest.sha256,
            "source_generation_manifest_size_bytes": str(source_binding_manifest.size_bytes),
            "source_serving_database_sha256": source_binding.sha256,
            "source_serving_database_size_bytes": str(source_binding.size_bytes),
            "source_text_contract": "cardrag.page-source-text-range.v1",
        }
        rows = connection.execute(
            """SELECT row_index,chunk_id,contract_revision_id,span_id,document_id,
                      page,source_start,source_end,text,input_sha256
                 FROM evaluation_chunks ORDER BY row_index"""
        ).fetchall()
    assert len(rows) == len(chunks)
    for row, chunk in zip(rows, chunks, strict=True):
        assert row == (
            chunk.row_index,
            chunk.chunk_id,
            chunk.contract_revision_id,
            chunk.chunk_id,
            chunk.document_id,
            chunk.page,
            chunk.source_start,
            chunk.source_end,
            chunk.text,
            hashlib.sha256(chunk.text.encode("utf-8")).hexdigest(),
        )

    question = "혜택은?"
    gold = tmp_path / "page-gold.jsonl"
    gold_binding = _gold(
        gold,
        contract_id=revision_id,
        span_id=chunks[0].chunk_id,
        question=question,
    )
    answers = tmp_path / "page-answers.jsonl"
    answers_binding = _answers(
        answers,
        lane="qwen_page",
        gold_sha256=gold_binding.sha256,
        generation_id=corpus.generation_id,
        generation_manifest_sha256=corpus.generation_manifest.sha256,
        question=question,
    )
    query_receipt_path = tmp_path / "query-provider-receipt.json"
    query_receipt = _provider_receipt(
        query_receipt_path,
        model="qwen/qwen3-embedding-8b",
        provider_id="deepinfra",
        inputs=[
            (
                "gold-001",
                format_qwen3_query(question),
                _unit_vector(4096),
            )
        ],
    )
    query_replay = tmp_path / "query-replay.jsonl"
    _embedding_replay(
        query_replay,
        lane="qwen_page",
        input_kind="query",
        source_commit=commit,
        model="qwen/qwen3-embedding-8b",
        dimension=4096,
        profile_id=profile_id,
        query_policy="cardrag.qwen3-query.v1",
        document_policy=None,
        provider_receipt=query_receipt,
        inputs=[("gold-001", format_qwen3_query(question), _unit_vector(4096))],
    )
    observation = tmp_path / "page-observation.jsonl"
    dense_scores = tmp_path / "page-dense-scores.f32"
    query_vectors = tmp_path / "page-query-vectors.f32"
    observation_binding = produce_external_observation(
        lane="qwen_page",
        gold_path=gold,
        expected_gold_sha256=gold_binding.sha256,
        answer_artifact_path=answers,
        expected_answer_artifact_sha256=answers_binding.sha256,
        query_embedding_replay_path=query_replay,
        provider_receipt_path=query_receipt_path,
        generation_manifest_path=page_manifest,
        database_path=page_database,
        vector_path=page_vectors,
        inventory_path=inventory,
        source_commit=commit,
        output_path=observation,
        dense_score_matrix_path=dense_scores,
        query_vector_matrix_path=query_vectors,
        lexical_rank_path=None,
        release_gate=False,
    )
    observation_records = [json.loads(line) for line in observation.read_bytes().splitlines()]
    assert observation_records[0]["schema_version"] == (
        "cardrag.gold-external-observation-artifact.v2"
    )
    assert observation_records[0]["lexical_rank_artifact"] is None
    assert observation_records[0]["maximum_result_contracts"] == 100
    assert observation_records[0]["maximum_result_spans"] == 100
    assert observation_records[0]["maximum_dense_trace_contracts"] == 100
    assert observation_records[0]["maximum_dense_trace_spans"] == 250
    assert "raw_rows" not in observation_records[1]
    assert len(dense_scores.read_bytes()) == len(chunks) * 4
    assert len(query_vectors.read_bytes()) == 4096 * 4
    vector_payload = page_vectors.read_bytes()
    tampered_vectors = tmp_path / "page-vectors-tampered.f32"
    tampered_vectors.write_bytes(bytes((vector_payload[0] ^ 1,)) + vector_payload[1:])
    _inventory_manifest, inventory_rows, _inventory_binding = producer._load_inventory_rows(
        inventory
    )
    loaded_page_manifest = producer.PageGenerationManifest.model_validate_json(
        page_manifest.read_bytes()
    )
    vector_loads = 0

    def forbidden_vector_load(*_args: object, **_kwargs: object) -> np.ndarray:
        nonlocal vector_loads
        vector_loads += 1
        raise AssertionError("page DB preflight must precede vector allocation")

    with monkeypatch.context() as preflight:
        preflight.setattr(producer, "_load_page_vectors", forbidden_vector_load)
        for name, statement, parameters, expected_error in (
            (
                "sparse",
                "DELETE FROM evaluation_chunks WHERE row_index=1",
                (),
                "producer_page_inventory_mismatch",
            ),
            (
                "oversized-text",
                "UPDATE evaluation_chunks SET text=? WHERE row_index=0",
                ("x" * (producer.PAGE_MAXIMUM_CHARS * 4 + 1),),
                "producer_page_inventory_mismatch",
            ),
            (
                "oversized-identifier",
                "UPDATE evaluation_chunks SET contract_revision_id=? WHERE row_index=0",
                ("x" * 513,),
                "producer_page_inventory_mismatch",
            ),
            (
                "oversized-metadata",
                "UPDATE metadata SET value=? WHERE key='generation_id'",
                ("x" * 4097,),
                "page_database_metadata_mismatch",
            ),
        ):
            malformed = tmp_path / f"page-{name}.sqlite3"
            with sqlite3.connect(page_database) as source_connection:
                with sqlite3.connect(malformed) as destination_connection:
                    source_connection.backup(destination_connection)
            with sqlite3.connect(malformed) as mutation:
                mutation.execute(statement, parameters)
                mutation.commit()
            with sqlite3.connect(malformed) as malformed_connection:
                with pytest.raises(GoldCaptureError, match=expected_error):
                    producer._load_qwen_observation_corpus(
                        malformed_connection,
                        page_manifest=loaded_page_manifest,
                        vector_path=page_vectors,
                        vectors_binding=corpus.vectors,
                        inventory=inventory_rows,
                    )

        class OversizedInventory(Sequence[CorpusInventoryRow]):
            def __len__(self) -> int:
                return 100_000

            def __getitem__(self, _index: int | slice) -> CorpusInventoryRow:
                raise AssertionError("row-count forecast must precede inventory access")

        oversized_manifest = producer.PageGenerationManifest.model_validate(
            {
                **loaded_page_manifest.model_dump(mode="python"),
                "row_count": 100_000,
            }
        )
        with sqlite3.connect(page_database) as oversized_connection:
            with pytest.raises(GoldCaptureError, match="page_vector_resource_limit_exceeded"):
                producer._load_qwen_observation_corpus(
                    oversized_connection,
                    page_manifest=oversized_manifest,
                    vector_path=page_vectors,
                    vectors_binding=corpus.vectors,
                    inventory=OversizedInventory(),
                )
    assert vector_loads == 0
    with pytest.raises(GoldCaptureError, match="page_vector_binding_mismatch"):
        producer._load_page_vectors(
            tampered_vectors,
            expected_binding=corpus.vectors,
            inventory=inventory_rows,
        )
    with pytest.raises(GoldCaptureError, match="producer_page_vector_inventory_mismatch"):
        producer._load_page_vectors(
            tampered_vectors,
            expected_binding=_binding(tampered_vectors),
            inventory=inventory_rows,
        )
    dense_payload = dense_scores.read_bytes()
    tampered_dense = tmp_path / "page-dense-scores-tampered.f32"
    tampered_dense.write_bytes(bytes((dense_payload[0] ^ 1,)) + dense_payload[1:])
    with pytest.raises(GoldCaptureError) as tampered:
        seal_external_observation(
            gold_path=gold,
            expected_gold_sha256=gold_binding.sha256,
            observation_path=observation,
            expected_observation_sha256=observation_binding.sha256,
            inventory_path=inventory,
            expected_inventory_sha256=corpus.inventory.sha256,
            generation_manifest_path=page_manifest,
            database_path=page_database,
            vector_path=page_vectors,
            dense_score_matrix_path=tampered_dense,
            query_vector_matrix_path=query_vectors,
            lexical_rank_path=None,
            output_path=tmp_path / "tampered-page-run.jsonl",
            receipt_path=tmp_path / "tampered-page-capture-receipt.json",
            expected_source_commit=commit,
            release_gate=False,
        )
    assert tampered.value.code == "external_dense_score_matrix_binding_mismatch"
    receipt = seal_external_observation(
        gold_path=gold,
        expected_gold_sha256=gold_binding.sha256,
        observation_path=observation,
        expected_observation_sha256=observation_binding.sha256,
        inventory_path=inventory,
        expected_inventory_sha256=corpus.inventory.sha256,
        generation_manifest_path=page_manifest,
        database_path=page_database,
        vector_path=page_vectors,
        dense_score_matrix_path=dense_scores,
        query_vector_matrix_path=query_vectors,
        lexical_rank_path=None,
        output_path=tmp_path / "page-run.jsonl",
        receipt_path=tmp_path / "page-capture-receipt.json",
        expected_source_commit=commit,
        release_gate=False,
    )
    assert receipt.lane == "qwen_page"
    assert receipt.attestation_artifact == observation_binding


def test_embedding_replay_rejects_input_hash_tamper(tmp_path: Path) -> None:
    receipt_path = tmp_path / "provider-receipt.json"
    original_input = format_qwen3_query("원본")
    receipt = _provider_receipt(
        receipt_path,
        model="qwen/qwen3-embedding-8b",
        provider_id="deepinfra",
        inputs=[("gold-001", original_input, _unit_vector(4096))],
    )
    replay = tmp_path / "replay.jsonl"
    _embedding_replay(
        replay,
        lane="qwen_page",
        input_kind="query",
        source_commit="1" * 40,
        model="qwen/qwen3-embedding-8b",
        dimension=4096,
        profile_id=qwen3_embedding_profile_id("deepinfra", maximum_tokens=8192),
        query_policy="cardrag.qwen3-query.v1",
        document_policy=None,
        provider_receipt=receipt,
        inputs=[("gold-001", original_input, _unit_vector(4096))],
    )
    with pytest.raises(GoldCaptureError, match="embedding_replay_input_binding_mismatch"):
        producer.load_embedding_replay(
            replay,
            provider_receipt_path=receipt_path,
            lane="qwen_page",
            input_kind="query",
            source_commit="1" * 40,
            embedding_profile_id=qwen3_embedding_profile_id("deepinfra", maximum_tokens=8192),
            expected_inputs=(("gold-001", format_qwen3_query("변조")),),
        )


def test_embedding_replay_rejects_vector_not_derived_from_raw_response(tmp_path: Path) -> None:
    formatted = format_qwen3_query("원본")
    receipt_path = tmp_path / "provider-receipt.json"
    receipt = _provider_receipt(
        receipt_path,
        model="qwen/qwen3-embedding-8b",
        provider_id="deepinfra",
        inputs=[("gold-001", formatted, _unit_vector(4096, 0))],
    )
    replay = tmp_path / "replay.jsonl"
    _embedding_replay(
        replay,
        lane="qwen_page",
        input_kind="query",
        source_commit="1" * 40,
        model="qwen/qwen3-embedding-8b",
        dimension=4096,
        profile_id=qwen3_embedding_profile_id("deepinfra", maximum_tokens=8192),
        query_policy="cardrag.qwen3-query.v1",
        document_policy=None,
        provider_receipt=receipt,
        inputs=[("gold-001", formatted, _unit_vector(4096, 1))],
    )
    with pytest.raises(GoldCaptureError, match="provider_replay_vector_mismatch"):
        producer.load_embedding_replay(
            replay,
            provider_receipt_path=receipt_path,
            lane="qwen_page",
            input_kind="query",
            source_commit="1" * 40,
            embedding_profile_id=qwen3_embedding_profile_id(
                "deepinfra",
                maximum_tokens=8192,
            ),
            expected_inputs=(("gold-001", formatted),),
        )


def test_embedding_replay_rejects_self_asserted_malformed_raw_response(tmp_path: Path) -> None:
    formatted = format_qwen3_query("원본")
    receipt_path = tmp_path / "provider-receipt.json"
    receipt_binding = _provider_receipt(
        receipt_path,
        model="qwen/qwen3-embedding-8b",
        provider_id="deepinfra",
        inputs=[("gold-001", formatted, _unit_vector(4096))],
    )
    replay = tmp_path / "replay.jsonl"
    _embedding_replay(
        replay,
        lane="qwen_page",
        input_kind="query",
        source_commit="1" * 40,
        model="qwen/qwen3-embedding-8b",
        dimension=4096,
        profile_id=qwen3_embedding_profile_id("deepinfra", maximum_tokens=8192),
        query_policy="cardrag.qwen3-query.v1",
        document_policy=None,
        provider_receipt=receipt_binding,
        inputs=[("gold-001", formatted, _unit_vector(4096))],
    )
    receipt = ProviderReceipt.model_validate_json(receipt_path.read_bytes())
    raw_path = receipt_path.parent / receipt.response_artifacts[0].file_name
    malformed = _raw_embedding_envelope({"fixture": "self-asserted"})
    raw_path.write_bytes(malformed.canonical_bytes())
    response_artifacts = (
        ProviderResponseArtifact(file_name=raw_path.name, artifact=_binding(raw_path)),
    )
    response_set_sha256 = canonical_sha256(
        {
            "artifacts": [item.model_dump(mode="json") for item in response_artifacts],
            "schema_version": "cardrag.gold-provider-responses.v1",
        }
    )
    rebound_receipt = ProviderReceipt.model_validate(
        {
            **receipt.model_dump(mode="python"),
            "response_artifact_sha256": response_set_sha256,
            "response_artifacts": response_artifacts,
        }
    )
    receipt_path.write_bytes(rebound_receipt.canonical_bytes())
    _rebind_replay_receipt(replay, _binding(receipt_path))
    with pytest.raises(GoldCaptureError, match="qwen_provider_response"):
        producer.load_embedding_replay(
            replay,
            provider_receipt_path=receipt_path,
            lane="qwen_page",
            input_kind="query",
            source_commit="1" * 40,
            embedding_profile_id=qwen3_embedding_profile_id(
                "deepinfra",
                maximum_tokens=8192,
            ),
            expected_inputs=(("gold-001", formatted),),
        )


def test_embedding_replay_rejects_self_asserted_request_body(tmp_path: Path) -> None:
    formatted = format_qwen3_query("원본")
    receipt_path = tmp_path / "provider-receipt.json"
    receipt_binding = _provider_receipt(
        receipt_path,
        model="qwen/qwen3-embedding-8b",
        provider_id="deepinfra",
        inputs=[("gold-001", formatted, _unit_vector(4096))],
    )
    replay = tmp_path / "replay.jsonl"
    _embedding_replay(
        replay,
        lane="qwen_page",
        input_kind="query",
        source_commit="1" * 40,
        model="qwen/qwen3-embedding-8b",
        dimension=4096,
        profile_id=qwen3_embedding_profile_id("deepinfra", maximum_tokens=8192),
        query_policy="cardrag.qwen3-query.v1",
        document_policy=None,
        provider_receipt=receipt_binding,
        inputs=[("gold-001", formatted, _unit_vector(4096))],
    )
    receipt = ProviderReceipt.model_validate_json(receipt_path.read_bytes())
    wrong_body = producer._provider_request_body(
        model="qwen/qwen3-embedding-8b",
        provider_id="deepinfra",
        formatted_inputs=(format_qwen3_query("다른 요청"),),
    )
    wrong_request = producer._provider_request_record(
        ordinal=0,
        input_ids=("gold-001",),
        request_body=wrong_body,
        response_file_name=receipt.response_artifacts[0].file_name,
    )
    request_contract_sha256 = producer._provider_request_contract_sha256(
        model="qwen/qwen3-embedding-8b",
        provider_id="deepinfra",
        requests=(wrong_request,),
        input_count=1,
    )
    rebound_receipt = ProviderReceipt.model_validate(
        {
            **receipt.model_dump(mode="python"),
            "request_contract_sha256": request_contract_sha256,
            "requests": (wrong_request,),
        }
    )
    receipt_path.write_bytes(rebound_receipt.canonical_bytes())
    _rebind_replay_receipt(replay, _binding(receipt_path))
    with pytest.raises(GoldCaptureError, match="provider_request_contract_mismatch"):
        producer.load_embedding_replay(
            replay,
            provider_receipt_path=receipt_path,
            lane="qwen_page",
            input_kind="query",
            source_commit="1" * 40,
            embedding_profile_id=qwen3_embedding_profile_id(
                "deepinfra",
                maximum_tokens=8192,
            ),
            expected_inputs=(("gold-001", formatted),),
        )


def test_qwen_resource_forecast_supports_full_page_corpus_and_exact_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forecast = producer._forecast_qwen_replay_resources(
        record_count=5_799,
        dimension=4_096,
        input_resident_size_bytes=16 * 1024 * 1024,
        maximum_response_bytes=producer._MAX_PROVIDER_RESPONSE_BYTES,
        batch_size=16,
        manifest_line_size_bytes=1_024,
    )

    assert forecast.matrix_size_bytes == 95_010_816
    assert forecast.replay_size_bytes > producer.MAX_GIT_EVIDENCE_FILE_BYTES
    assert forecast.peak_working_set_bytes < producer._QWEN_REPLAY_WORKING_SET_LIMIT_BYTES
    monkeypatch.setattr(
        producer,
        "_QWEN_REPLAY_WORKING_SET_LIMIT_BYTES",
        forecast.peak_working_set_bytes,
    )
    assert (
        producer._forecast_qwen_replay_resources(
            record_count=5_799,
            dimension=4_096,
            input_resident_size_bytes=16 * 1024 * 1024,
            maximum_response_bytes=producer._MAX_PROVIDER_RESPONSE_BYTES,
            batch_size=16,
            manifest_line_size_bytes=1_024,
        ).peak_working_set_bytes
        == forecast.peak_working_set_bytes
    )
    monkeypatch.setattr(
        producer,
        "_QWEN_REPLAY_WORKING_SET_LIMIT_BYTES",
        forecast.peak_working_set_bytes - 1,
    )
    with pytest.raises(GoldCaptureError, match="qwen_provider_capture_resource_limit_exceeded"):
        producer._forecast_qwen_replay_resources(
            record_count=5_799,
            dimension=4_096,
            input_resident_size_bytes=16 * 1024 * 1024,
            maximum_response_bytes=producer._MAX_PROVIDER_RESPONSE_BYTES,
            batch_size=16,
            manifest_line_size_bytes=1_024,
        )


def test_qwen_twenty_thousand_capture_and_streaming_load_forecasts_are_consistent() -> None:
    count = 20_000
    formatted = format_qwen3_query("혜택은?")
    expected_inputs = tuple((_maximum_identifier(index), formatted) for index in range(count))
    input_ids = tuple(input_id for input_id, _formatted in expected_inputs)
    manifest = EmbeddingReplayManifest(
        schema_version=EMBEDDING_REPLAY_SCHEMA,
        lane="qwen_page",
        input_kind="query",
        synthetic=False,
        source_commit="1" * 40,
        embedding_model="qwen/qwen3-embedding-8b",
        embedding_dimension=4096,
        embedding_profile_id=qwen3_embedding_profile_id(
            "deepinfra",
            maximum_tokens=8192,
        ),
        query_policy="cardrag.qwen3-query.v1",
        document_policy=None,
        provider_receipt=ArtifactBinding(sha256="a" * 64, size_bytes=2 * 1024 * 1024),
        record_count=count,
    )
    capture = producer._forecast_qwen_replay_resources(
        record_count=count,
        dimension=4096,
        input_resident_size_bytes=32 * 1024 * 1024,
        maximum_response_bytes=producer._MAX_PROVIDER_RESPONSE_BYTES,
        batch_size=16,
        manifest_line_size_bytes=len(manifest.canonical_bytes()) + 1,
        input_ids=input_ids,
    )
    loader = producer._forecast_qwen_replay_resources(
        record_count=count,
        dimension=4096,
        input_resident_size_bytes=producer._qwen_streaming_replay_resident_size(
            manifest=manifest,
            expected_inputs=expected_inputs,
            retained_resident_size_bytes=0,
        ),
        maximum_response_bytes=producer._MAX_PROVIDER_RESPONSE_BYTES,
        batch_size=16,
        manifest_line_size_bytes=len(manifest.canonical_bytes()) + 1,
        input_ids=input_ids,
    )

    assert capture.replay_size_bytes == loader.replay_size_bytes
    assert capture.replay_size_bytes > 400 * 1024 * 1024
    assert capture.peak_working_set_bytes < producer._QWEN_REPLAY_WORKING_SET_LIMIT_BYTES
    assert loader.peak_working_set_bytes < producer._QWEN_REPLAY_WORKING_SET_LIMIT_BYTES
    assert (
        producer._forecast_qwen_provider_receipt_size(
            input_ids=input_ids[:5_799],
            provider_id="deepinfra",
            batch_size=16,
            maximum_response_bytes=producer._MAX_PROVIDER_RESPONSE_BYTES,
        ).receipt_size_bytes
        <= producer._MAX_MANIFEST_BYTES
    )
    assert (
        producer._forecast_qwen_provider_receipt_size(
            input_ids=input_ids,
            provider_id="deepinfra",
            batch_size=16,
            maximum_response_bytes=producer._MAX_PROVIDER_RESPONSE_BYTES,
        ).receipt_size_bytes
        <= producer._MAX_MANIFEST_BYTES
    )
    composite_count = 50_000
    composite_inputs = tuple((f"row-{index}", formatted) for index in range(composite_count))
    composite_ids = tuple(input_id for input_id, _formatted in composite_inputs)
    composite_manifest = manifest.model_copy(update={"record_count": composite_count})
    streaming_only_resident = producer._qwen_streaming_replay_resident_size(
        manifest=composite_manifest,
        expected_inputs=composite_inputs,
        retained_resident_size_bytes=0,
    )
    assert (
        producer._forecast_qwen_replay_resources(
            record_count=composite_count,
            dimension=4096,
            input_resident_size_bytes=streaming_only_resident,
            maximum_response_bytes=producer._MAX_PROVIDER_RESPONSE_BYTES,
            batch_size=16,
            manifest_line_size_bytes=len(composite_manifest.canonical_bytes()) + 1,
            input_ids=composite_ids,
        ).peak_working_set_bytes
        < producer._QWEN_REPLAY_WORKING_SET_LIMIT_BYTES
    )
    retained_page_chunks = composite_count * (
        producer._PAGE_CHUNK_OBJECT_HEADROOM_BYTES + producer.PAGE_MAXIMUM_CHARS * 4
    )
    with pytest.raises(GoldCaptureError, match="qwen_provider_capture_resource_limit_exceeded"):
        producer._forecast_qwen_replay_resources(
            record_count=composite_count,
            dimension=4096,
            input_resident_size_bytes=producer._qwen_streaming_replay_resident_size(
                manifest=composite_manifest,
                expected_inputs=composite_inputs,
                retained_resident_size_bytes=retained_page_chunks,
            ),
            maximum_response_bytes=producer._MAX_PROVIDER_RESPONSE_BYTES,
            batch_size=16,
            manifest_line_size_bytes=len(composite_manifest.canonical_bytes()) + 1,
            input_ids=composite_ids,
        )
    with pytest.raises(GoldCaptureError, match="qwen_provider_capture_resource_limit_exceeded"):
        producer._forecast_qwen_replay_resources(
            record_count=count,
            dimension=4096,
            input_resident_size_bytes=capture.replay_size_bytes,
            maximum_response_bytes=producer._MAX_PROVIDER_RESPONSE_BYTES,
            batch_size=16,
            manifest_line_size_bytes=len(manifest.canonical_bytes()) + 1,
            input_ids=input_ids,
        )


@pytest.mark.parametrize(
    ("field", "invalid"),
    (
        ("record_count", float("nan")),
        ("record_count", 1.5),
        ("record_count", True),
        ("record_count", 0),
        ("record_count", -1),
        ("dimension", float("inf")),
        ("dimension", 4_096.0),
        ("dimension", True),
        ("dimension", 4_095),
        ("dimension", 4_097),
        ("input_resident_size_bytes", float("-inf")),
        ("input_resident_size_bytes", 1.5),
        ("input_resident_size_bytes", False),
        ("input_resident_size_bytes", 0),
        ("input_resident_size_bytes", -1),
        ("input_resident_size_bytes", producer._MAX_EMBEDDING_REPLAY_BYTES + 1),
        ("maximum_response_bytes", float("nan")),
        ("maximum_response_bytes", float("inf")),
        ("maximum_response_bytes", float("-inf")),
        ("maximum_response_bytes", 1_024.5),
        ("maximum_response_bytes", True),
        ("maximum_response_bytes", False),
        ("maximum_response_bytes", 0),
        ("maximum_response_bytes", -1),
        ("maximum_response_bytes", producer._MAX_PROVIDER_RESPONSE_BYTES + 1),
        ("batch_size", float("nan")),
        ("batch_size", 1.5),
        ("batch_size", True),
        ("batch_size", 0),
        ("batch_size", -1),
        ("batch_size", 129),
        ("manifest_line_size_bytes", float("inf")),
        ("manifest_line_size_bytes", 1.5),
        ("manifest_line_size_bytes", False),
        ("manifest_line_size_bytes", 0),
        ("manifest_line_size_bytes", -1),
        ("manifest_line_size_bytes", producer._MAX_MANIFEST_BYTES + 2),
    ),
)
def test_qwen_resource_forecast_rejects_noncanonical_numeric_limits(
    field: str,
    invalid: object,
) -> None:
    values: dict[str, object] = {
        "record_count": 1,
        "dimension": 4_096,
        "input_resident_size_bytes": 1,
        "maximum_response_bytes": producer._MAX_PROVIDER_RESPONSE_BYTES,
        "batch_size": 1,
        "manifest_line_size_bytes": 1_024,
    }
    values[field] = invalid
    with pytest.raises(GoldCaptureError, match="qwen_provider_capture_limits_invalid"):
        producer._forecast_qwen_replay_resources(
            record_count=cast(int, values["record_count"]),
            dimension=cast(int, values["dimension"]),
            input_resident_size_bytes=cast(int, values["input_resident_size_bytes"]),
            maximum_response_bytes=cast(int, values["maximum_response_bytes"]),
            batch_size=cast(int, values["batch_size"]),
            manifest_line_size_bytes=cast(int, values["manifest_line_size_bytes"]),
        )


@pytest.mark.parametrize("lane", ("qwen", "v109"))
@pytest.mark.parametrize(
    "invalid_limit",
    (
        float("nan"),
        float("inf"),
        float("-inf"),
        1_024.5,
        True,
        False,
        0,
        -1,
        producer._MAX_PROVIDER_RESPONSE_BYTES + 1,
    ),
)
@pytest.mark.asyncio
async def test_live_capture_rejects_noncanonical_response_limit_before_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lane: str,
    invalid_limit: object,
) -> None:
    secret_reads = 0
    client_constructions = 0
    provider_calls = 0
    token_counts = 0

    def forbidden_secret(_path: Path) -> str:
        nonlocal secret_reads
        secret_reads += 1
        raise AssertionError("numeric preflight must fail before credential access")

    class ForbiddenClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            nonlocal client_constructions
            client_constructions += 1
            raise AssertionError("numeric preflight must fail before client construction")

        def stream(self, *_args: object, **_kwargs: object) -> None:
            nonlocal provider_calls
            provider_calls += 1
            raise AssertionError("numeric preflight must fail before provider calls")

    def forbidden_token_count(_value: str) -> int:
        nonlocal token_counts
        token_counts += 1
        raise AssertionError("numeric preflight must fail before tokenizer work")

    monkeypatch.setattr(producer, "_read_secret", forbidden_secret)
    monkeypatch.setattr(httpx, "AsyncClient", ForbiddenClient)
    state = tmp_path / f"{lane}-state"
    receipt = tmp_path / f"{lane}-receipt.json"
    replay = tmp_path / f"{lane}-replay.jsonl"
    expected_error = f"{lane}_provider_capture_limits_invalid"
    with pytest.raises(GoldCaptureError, match=expected_error):
        if lane == "qwen":
            await capture_qwen_embedding_replay(
                input_path=tmp_path / "missing-input",
                openrouter_base_url="https://openrouter.ai/api/v1",
                openrouter_api_key_file=tmp_path / "missing-key",
                provider_receipt_output_path=receipt,
                replay_output_path=replay,
                state_directory=state,
                maximum_response_bytes=cast(int, invalid_limit),
                token_counter=forbidden_token_count,
            )
        else:
            await capture_v109_query_embedding_replay(
                gold_path=tmp_path / "missing-gold",
                expected_gold_sha256="a" * 64,
                generation_manifest_path=tmp_path / "missing-manifest",
                database_path=tmp_path / "missing-database",
                openrouter_base_url="https://openrouter.ai/api/v1",
                openrouter_api_key_file=tmp_path / "missing-key",
                provider_receipt_output_path=receipt,
                replay_output_path=replay,
                state_directory=state,
                maximum_response_bytes=cast(int, invalid_limit),
                release_gate=False,
            )
    assert secret_reads == 0
    assert client_constructions == 0
    assert provider_calls == 0
    assert token_counts == 0
    assert not state.exists()
    assert not receipt.exists()
    assert not replay.exists()


@pytest.mark.parametrize("lane", ("qwen", "v109"))
@pytest.mark.parametrize(
    "invalid_timeout",
    (
        float("nan"),
        float("inf"),
        float("-inf"),
        True,
        False,
        0.0,
        -1.0,
        producer._MAX_PROVIDER_TIMEOUT_SECONDS + 0.5,
    ),
)
@pytest.mark.asyncio
async def test_live_capture_rejects_invalid_timeout_before_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lane: str,
    invalid_timeout: object,
) -> None:
    secret_reads = 0
    client_constructions = 0
    provider_calls = 0

    def forbidden_secret(_path: Path) -> str:
        nonlocal secret_reads
        secret_reads += 1
        raise AssertionError("numeric preflight must fail before credential access")

    class ForbiddenClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            nonlocal client_constructions
            client_constructions += 1
            raise AssertionError("numeric preflight must fail before client construction")

        def stream(self, *_args: object, **_kwargs: object) -> None:
            nonlocal provider_calls
            provider_calls += 1
            raise AssertionError("numeric preflight must fail before provider calls")

    monkeypatch.setattr(producer, "_read_secret", forbidden_secret)
    monkeypatch.setattr(httpx, "AsyncClient", ForbiddenClient)
    state = tmp_path / f"{lane}-state"
    receipt = tmp_path / f"{lane}-receipt.json"
    replay = tmp_path / f"{lane}-replay.jsonl"
    expected_error = f"{lane}_provider_capture_limits_invalid"
    with pytest.raises(GoldCaptureError, match=expected_error):
        if lane == "qwen":
            await capture_qwen_embedding_replay(
                input_path=tmp_path / "missing-input",
                openrouter_base_url="https://openrouter.ai/api/v1",
                openrouter_api_key_file=tmp_path / "missing-key",
                provider_receipt_output_path=receipt,
                replay_output_path=replay,
                state_directory=state,
                timeout_seconds=cast(float, invalid_timeout),
                token_counter=lambda _value: 1,
            )
        else:
            await capture_v109_query_embedding_replay(
                gold_path=tmp_path / "missing-gold",
                expected_gold_sha256="a" * 64,
                generation_manifest_path=tmp_path / "missing-manifest",
                database_path=tmp_path / "missing-database",
                openrouter_base_url="https://openrouter.ai/api/v1",
                openrouter_api_key_file=tmp_path / "missing-key",
                provider_receipt_output_path=receipt,
                replay_output_path=replay,
                state_directory=state,
                timeout_seconds=cast(float, invalid_timeout),
                release_gate=False,
            )
    assert secret_reads == 0
    assert client_constructions == 0
    assert provider_calls == 0
    assert not state.exists()
    assert not receipt.exists()
    assert not replay.exists()


@pytest.mark.parametrize(
    "invalid_batch_size",
    (float("nan"), float("inf"), float("-inf"), 1.5, True, False, 0, -1, 129),
)
@pytest.mark.asyncio
async def test_qwen_live_capture_rejects_invalid_batch_size_before_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_batch_size: object,
) -> None:
    secret_reads = 0
    client_constructions = 0
    provider_calls = 0

    def forbidden_secret(_path: Path) -> str:
        nonlocal secret_reads
        secret_reads += 1
        raise AssertionError("numeric preflight must fail before credential access")

    class ForbiddenClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            nonlocal client_constructions
            client_constructions += 1
            raise AssertionError("numeric preflight must fail before client construction")

        def stream(self, *_args: object, **_kwargs: object) -> None:
            nonlocal provider_calls
            provider_calls += 1
            raise AssertionError("numeric preflight must fail before provider calls")

    monkeypatch.setattr(producer, "_read_secret", forbidden_secret)
    monkeypatch.setattr(httpx, "AsyncClient", ForbiddenClient)
    state = tmp_path / "state"
    receipt = tmp_path / "receipt.json"
    replay = tmp_path / "replay.jsonl"
    with pytest.raises(GoldCaptureError, match="qwen_provider_capture_limits_invalid"):
        await capture_qwen_embedding_replay(
            input_path=tmp_path / "missing-input",
            openrouter_base_url="https://openrouter.ai/api/v1",
            openrouter_api_key_file=tmp_path / "missing-key",
            provider_receipt_output_path=receipt,
            replay_output_path=replay,
            state_directory=state,
            batch_size=cast(int, invalid_batch_size),
            token_counter=lambda _value: 1,
        )
    assert secret_reads == 0
    assert client_constructions == 0
    assert provider_calls == 0
    assert not state.exists()
    assert not receipt.exists()
    assert not replay.exists()


@pytest.mark.parametrize(
    "invalid_limit",
    (
        float("nan"),
        float("inf"),
        float("-inf"),
        1_024.5,
        True,
        False,
        0,
        -1,
        producer._MAX_PROVIDER_RESPONSE_BYTES + 1,
    ),
)
@pytest.mark.asyncio
async def test_bounded_provider_body_defensively_rejects_invalid_limit(
    invalid_limit: object,
) -> None:
    response = httpx.Response(200, content=b"bounded")
    with pytest.raises(GoldCaptureError, match="provider_response_limit_invalid"):
        await producer._bounded_provider_body(
            response,
            maximum_bytes=cast(int, invalid_limit),
        )


@pytest.mark.asyncio
async def test_bounded_provider_body_rejects_unbounded_content_length_digits() -> None:
    response = httpx.Response(
        200,
        headers={"content-length": "9" * 5_000},
        content=b"bounded",
    )
    with pytest.raises(GoldCaptureError, match="qwen_provider_content_length_invalid"):
        await producer._bounded_provider_body(
            response,
            maximum_bytes=producer._MAX_PROVIDER_RESPONSE_BYTES,
        )


@pytest.mark.asyncio
async def test_v109_release_anchor_rejects_hostile_metadata_before_database_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "hostile-v109.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO metadata VALUES(?,?)",
            ("oversized", "x" * (1024 * 1024)),
        )
        connection.commit()
    database_binding = _binding(database)
    hostile_manifest = cast(
        GenerationManifest,
        SimpleNamespace(
            schema_version="cardrag.generation.v4",
            generation_id="hostile-generation",
            serving_database=database_binding,
        ),
    )
    monkeypatch.setattr(
        producer,
        "load_gold_jsonl",
        lambda _path, **_kwargs: SimpleNamespace(sha256="a" * 64),
    )
    monkeypatch.setattr(
        producer,
        "_load_generation_manifest",
        lambda _path: (
            hostile_manifest,
            ArtifactBinding(sha256="b" * 64, size_bytes=123),
        ),
    )
    database_opens = 0
    secret_reads = 0
    client_constructions = 0
    state_accesses = 0

    def forbidden_database_open(*_args: object, **_kwargs: object) -> None:
        nonlocal database_opens
        database_opens += 1
        raise AssertionError("release anchor must precede SQLite open")

    def forbidden_secret(_path: Path) -> str:
        nonlocal secret_reads
        secret_reads += 1
        raise AssertionError("release anchor must precede credential access")

    def forbidden_client(*_args: object, **_kwargs: object) -> None:
        nonlocal client_constructions
        client_constructions += 1
        raise AssertionError("release anchor must precede client construction")

    def forbidden_state(_path: Path) -> Path:
        nonlocal state_accesses
        state_accesses += 1
        raise AssertionError("release anchor must precede state creation")

    monkeypatch.setattr(producer, "_sqlite_readonly", forbidden_database_open)
    monkeypatch.setattr(producer, "_read_secret", forbidden_secret)
    monkeypatch.setattr(producer.httpx, "AsyncClient", forbidden_client)
    monkeypatch.setattr(producer, "_safe_private_state_directory", forbidden_state)
    with pytest.raises(GoldCaptureError, match="v109_preserved_source_anchor_mismatch"):
        await capture_v109_query_embedding_replay(
            gold_path=tmp_path / "gold.jsonl",
            expected_gold_sha256="a" * 64,
            generation_manifest_path=tmp_path / "manifest.json",
            database_path=database,
            openrouter_base_url="https://openrouter.ai/api/v1",
            openrouter_api_key_file=tmp_path / "missing-key",
            provider_receipt_output_path=tmp_path / "receipt.json",
            replay_output_path=tmp_path / "replay.jsonl",
            state_directory=tmp_path / "state",
            expected_run_id=producer.V109_PRESERVED_RUN_ID,
            expected_generation_id=producer.V109_PRESERVED_GENERATION_ID,
            expected_manifest_sha256=producer.V109_PRESERVED_MANIFEST_SHA256,
            expected_database_sha256=producer.V109_PRESERVED_DATABASE_SHA256,
        )
    assert database_opens == 0
    assert secret_reads == 0
    assert client_constructions == 0
    assert state_accesses == 0


@pytest.mark.asyncio
async def test_qwen_receipt_boundary_plus_one_fails_before_all_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record_count = 34_462
    input_ids = tuple(_maximum_identifier(index) for index in range(record_count))
    assert (
        producer._forecast_qwen_provider_receipt_size(
            input_ids=input_ids[:-1],
            provider_id="deepinfra",
            batch_size=1,
            maximum_response_bytes=producer._MAX_PROVIDER_RESPONSE_BYTES,
        ).receipt_size_bytes
        <= producer._MAX_MANIFEST_BYTES
    )
    manifest = producer._qwen_input_manifest(
        source_commit="1" * 40,
        input_kind="query",
        embedding_profile_id=qwen3_embedding_profile_id(
            "deepinfra",
            maximum_tokens=8192,
        ),
        provider_id="deepinfra",
        maximum_tokens=8192,
        record_count=record_count,
    )
    rows = tuple(SimpleNamespace(input_id=input_id) for input_id in input_ids)
    input_binding = ArtifactBinding(sha256="a" * 64, size_bytes=1)
    monkeypatch.setattr(
        producer,
        "_load_qwen_inputs",
        lambda _path, **_kwargs: (manifest, rows, input_binding),
    )
    secret_reads = 0
    client_constructions = 0
    provider_calls = 0
    state_accesses = 0
    token_counts = 0

    def forbidden_secret(_path: Path) -> str:
        nonlocal secret_reads
        secret_reads += 1
        raise AssertionError("receipt preflight must fail before credential access")

    class ForbiddenClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            nonlocal client_constructions
            client_constructions += 1

        def stream(self, *_args: object, **_kwargs: object) -> None:
            nonlocal provider_calls
            provider_calls += 1
            raise AssertionError("receipt preflight must make zero provider calls")

    def forbidden_state(_path: Path) -> Path:
        nonlocal state_accesses
        state_accesses += 1
        raise AssertionError("receipt preflight must fail before state creation")

    def forbidden_token_count(_value: str) -> int:
        nonlocal token_counts
        token_counts += 1
        raise AssertionError("receipt preflight must fail before tokenization")

    monkeypatch.setattr(producer, "_read_secret", forbidden_secret)
    monkeypatch.setattr(producer.httpx, "AsyncClient", ForbiddenClient)
    monkeypatch.setattr(producer, "_safe_private_state_directory", forbidden_state)
    receipt = tmp_path / "provider-receipt.json"
    replay = tmp_path / "replay.jsonl"
    state = tmp_path / "state"
    with pytest.raises(GoldCaptureError, match="qwen_provider_receipt_size_invalid"):
        await capture_qwen_embedding_replay(
            input_path=tmp_path / "inputs.jsonl",
            openrouter_base_url="https://openrouter.ai/api/v1",
            openrouter_api_key_file=tmp_path / "missing-key",
            provider_receipt_output_path=receipt,
            replay_output_path=replay,
            state_directory=state,
            batch_size=1,
            token_counter=forbidden_token_count,
        )
    assert secret_reads == 0
    assert client_constructions == 0
    assert provider_calls == 0
    assert state_accesses == 0
    assert token_counts == 0
    assert not receipt.exists()
    assert not replay.exists()
    assert not state.exists()


@pytest.mark.asyncio
async def test_qwen_natural_resource_cap_fails_before_input_rows_or_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = producer._qwen_input_manifest(
        source_commit="1" * 40,
        input_kind="query",
        embedding_profile_id=qwen3_embedding_profile_id(
            "deepinfra",
            maximum_tokens=8192,
        ),
        provider_id="deepinfra",
        maximum_tokens=8192,
        record_count=60_000,
    )
    input_path = tmp_path / "oversized-workflow-inputs.jsonl"
    input_path.write_bytes(manifest.canonical_bytes() + b"\n")
    secret_reads = 0
    client_constructions = 0
    state_accesses = 0
    token_counts = 0

    def forbidden_secret(_path: Path) -> str:
        nonlocal secret_reads
        secret_reads += 1
        raise AssertionError("resource preflight must fail before credential access")

    def forbidden_client(*_args: object, **_kwargs: object) -> None:
        nonlocal client_constructions
        client_constructions += 1
        raise AssertionError("resource preflight must fail before client construction")

    def forbidden_state(_path: Path) -> Path:
        nonlocal state_accesses
        state_accesses += 1
        raise AssertionError("resource preflight must fail before state creation")

    def forbidden_token_count(_value: str) -> int:
        nonlocal token_counts
        token_counts += 1
        raise AssertionError("resource preflight must fail before tokenization")

    monkeypatch.setattr(producer, "_read_secret", forbidden_secret)
    monkeypatch.setattr(producer.httpx, "AsyncClient", forbidden_client)
    monkeypatch.setattr(producer, "_safe_private_state_directory", forbidden_state)
    receipt = tmp_path / "provider-receipt.json"
    replay = tmp_path / "replay.jsonl"
    state = tmp_path / "state"
    with pytest.raises(GoldCaptureError, match="qwen_provider_capture_resource_limit_exceeded"):
        await capture_qwen_embedding_replay(
            input_path=input_path,
            openrouter_base_url="https://openrouter.ai/api/v1",
            openrouter_api_key_file=tmp_path / "missing-key",
            provider_receipt_output_path=receipt,
            replay_output_path=replay,
            state_directory=state,
            token_counter=forbidden_token_count,
        )
    assert secret_reads == 0
    assert client_constructions == 0
    assert state_accesses == 0
    assert token_counts == 0
    assert not receipt.exists()
    assert not replay.exists()
    assert not state.exists()


@pytest.mark.asyncio
async def test_qwen_resource_preflight_fails_before_secret_client_or_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    formatted = format_qwen3_query("혜택은?")
    input_path = tmp_path / "qwen-inputs.jsonl"
    input_path.write_bytes(
        producer._qwen_input_payload(
            source_commit="1" * 40,
            input_kind="query",
            embedding_profile_id=qwen3_embedding_profile_id(
                "deepinfra",
                maximum_tokens=8192,
            ),
            provider_id="deepinfra",
            maximum_tokens=8192,
            values=(("gold-001", formatted),),
            token_counter=lambda _value: 3,
        )
    )
    secret_reads = 0
    client_constructions = 0
    provider_calls = 0
    token_counts = 0

    def forbidden_secret(_path: Path) -> str:
        nonlocal secret_reads
        secret_reads += 1
        raise AssertionError("resource preflight must fail before credential access")

    class ForbiddenClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            nonlocal client_constructions
            client_constructions += 1
            raise AssertionError("resource preflight must fail before client construction")

        def stream(self, *_args: object, **_kwargs: object) -> None:
            nonlocal provider_calls
            provider_calls += 1
            raise AssertionError("resource preflight must fail before provider calls")

    def forbidden_token_count(_value: str) -> int:
        nonlocal token_counts
        token_counts += 1
        raise AssertionError("resource preflight must fail before tokenizer work")

    monkeypatch.setattr(producer, "_QWEN_REPLAY_WORKING_SET_LIMIT_BYTES", 1)
    monkeypatch.setattr(producer, "_read_secret", forbidden_secret)
    monkeypatch.setattr(httpx, "AsyncClient", ForbiddenClient)
    with pytest.raises(GoldCaptureError, match="qwen_provider_capture_resource_limit_exceeded"):
        await capture_qwen_embedding_replay(
            input_path=input_path,
            openrouter_base_url="https://openrouter.ai/api/v1",
            openrouter_api_key_file=tmp_path / "missing-key",
            provider_receipt_output_path=tmp_path / "receipt.json",
            replay_output_path=tmp_path / "replay.jsonl",
            state_directory=tmp_path / "state",
            token_counter=forbidden_token_count,
        )
    assert secret_reads == 0
    assert client_constructions == 0
    assert provider_calls == 0
    assert token_counts == 0
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / "receipt.json").exists()
    assert not (tmp_path / "replay.jsonl").exists()


def test_qwen_replay_load_resource_preflight_precedes_matrix_allocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    formatted = format_qwen3_query("원본")
    receipt_path = tmp_path / "provider-receipt.json"
    receipt = _provider_receipt(
        receipt_path,
        model="qwen/qwen3-embedding-8b",
        provider_id="deepinfra",
        inputs=[("gold-001", formatted, _unit_vector(4096))],
    )
    replay = tmp_path / "replay.jsonl"
    _embedding_replay(
        replay,
        lane="qwen_page",
        input_kind="query",
        source_commit="1" * 40,
        model="qwen/qwen3-embedding-8b",
        dimension=4096,
        profile_id=qwen3_embedding_profile_id("deepinfra", maximum_tokens=8192),
        query_policy="cardrag.qwen3-query.v1",
        document_policy=None,
        provider_receipt=receipt,
        inputs=[("gold-001", formatted, _unit_vector(4096))],
    )
    allocations = 0

    def forbidden_empty(*_args: object, **_kwargs: object) -> None:
        nonlocal allocations
        allocations += 1
        raise AssertionError("resource preflight must precede matrix allocation")

    monkeypatch.setattr(producer, "_QWEN_REPLAY_WORKING_SET_LIMIT_BYTES", 1)
    monkeypatch.setattr(np, "empty", forbidden_empty)
    with pytest.raises(GoldCaptureError, match="qwen_provider_capture_resource_limit_exceeded"):
        producer.load_embedding_replay(
            replay,
            provider_receipt_path=receipt_path,
            lane="qwen_page",
            input_kind="query",
            source_commit="1" * 40,
            embedding_profile_id=qwen3_embedding_profile_id("deepinfra", maximum_tokens=8192),
            expected_inputs=(("gold-001", formatted),),
        )
    assert allocations == 0


def test_external_release_docs_use_distinct_complete_bootstrap_and_final_commands() -> None:
    repository = Path(__file__).resolve().parents[3]
    external = (repository / "docs/V1_0_10_EXTERNAL_GOLD_PRODUCER.md").read_text(encoding="utf-8")
    evaluation = (repository / "docs/V1_0_10_GOLD_EVALUATION.md").read_text(encoding="utf-8")

    for document in (external, evaluation):
        assert "/bootstrap/" in document
        assert "/final/" in document
        assert "같은 출력 경로" not in document
        assert "--source-generation-manifest" in document
        assert "--source-database" in document
        for argument in (
            "--answer-input",
            "--expected-answer-input-sha256",
            "--answer-producer-receipt",
            "--expected-answer-producer-receipt-sha256",
            "--answer-artifact",
            "--expected-answer-artifact-sha256",
            "--answer-call-ledger",
            "--answer-state-identity",
            "--answer-state-bundle",
            "--answer-profile-id",
            "--answer-retrieval-run",
            "--expected-answer-retrieval-run-sha256",
            "--answer-retrieval-capture-receipt",
            "--expected-answer-retrieval-capture-receipt-sha256",
            "--answer-retrieval-attestation",
            "--expected-answer-retrieval-attestation-sha256",
            "--answer-retrieval-raw-score",
            "--expected-answer-retrieval-raw-score-sha256",
            "--answer-retrieval-corpus-inventory",
            "--expected-answer-retrieval-corpus-inventory-sha256",
            "--answer-retrieval-dense-score-matrix",
            "--expected-answer-retrieval-dense-score-matrix-sha256",
            "--answer-retrieval-query-vector-matrix",
            "--expected-answer-retrieval-query-vector-matrix-sha256",
        ):
            assert argument in document
    for argument in (
        "--expected-v109-run-id",
        "--expected-v109-generation-id",
        "--expected-v109-manifest-sha256",
        "--expected-v109-database-sha256",
        "--answer-retrieval-lexical-ranks",
    ):
        assert argument in external
    assert "1,024..67,108,864" in external
    assert "0 < value <= 3,600" in external
    assert "exact integer `1..128`" in external
    assert "document-aggregation-query-vector-matrix.f32" not in evaluation
    assert evaluation.count("document-aggregation-query-vectors.f32") == 6


@pytest.mark.asyncio
async def test_qwen_live_capture_binds_raw_response_and_resumes_without_network(
    tmp_path: Path,
) -> None:
    commit = "1" * 40
    profile_id = qwen3_embedding_profile_id("deepinfra", maximum_tokens=8192)
    formatted = format_qwen3_query("혜택은?")
    input_path = tmp_path / "qwen-inputs.jsonl"
    input_path.write_bytes(
        producer._qwen_input_payload(
            source_commit=commit,
            input_kind="query",
            embedding_profile_id=profile_id,
            provider_id="deepinfra",
            maximum_tokens=8192,
            values=(("gold-001", formatted),),
            token_counter=lambda _value: 3,
        )
    )
    key_path = tmp_path / "openrouter.key"
    key_path.write_text("fixture-secret-key", encoding="utf-8")
    calls = 0
    vector = _unit_vector(4096).tolist()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.headers["authorization"] == "Bearer fixture-secret-key"
        return httpx.Response(
            200,
            headers={"x-openrouter-provider": "DeepInfra"},
            json={
                "data": [{"embedding": vector, "index": 0}],
                "model": "Qwen/Qwen3-Embedding-8B",
            },
        )

    async with httpx.AsyncClient(
        base_url="https://openrouter.ai/api/v1/",
        transport=httpx.MockTransport(handler),
    ) as client:
        receipt_binding, replay_binding = await capture_qwen_embedding_replay(
            input_path=input_path,
            openrouter_base_url="https://openrouter.ai/api/v1",
            openrouter_api_key_file=key_path,
            provider_receipt_output_path=tmp_path / "provider-receipt.json",
            replay_output_path=tmp_path / "query-replay.jsonl",
            state_directory=tmp_path / "state",
            batch_size=1,
            client=client,
            token_counter=lambda _value: 3,
        )
    assert calls == 1
    assert receipt_binding == _binding(tmp_path / "provider-receipt.json")
    assert replay_binding == _binding(tmp_path / "query-replay.jsonl")
    assert b"fixture-secret-key" not in (tmp_path / "provider-receipt.json").read_bytes()
    receipt = ProviderReceipt.model_validate_json((tmp_path / "provider-receipt.json").read_bytes())
    assert len(receipt.response_artifacts) == 1
    raw_path = tmp_path / receipt.response_artifacts[0].file_name
    assert b"fixture-secret-key" not in raw_path.read_bytes()
    reservations = list((tmp_path / "state").glob("**/reservations/request-*.json"))
    state_responses = list((tmp_path / "state").glob("**/responses/qwen-response-*.json"))
    assert len(reservations) == 1
    assert len(state_responses) == 1
    reservation = producer.ProviderRequestRecord.model_validate_json(reservations[0].read_bytes())
    assert reservation == receipt.requests[0]
    loaded, matrix, _binding_value = producer.load_embedding_replay(
        tmp_path / "query-replay.jsonl",
        provider_receipt_path=tmp_path / "provider-receipt.json",
        lane="qwen_page",
        input_kind="query",
        source_commit=commit,
        embedding_profile_id=profile_id,
        expected_inputs=(("gold-001", formatted),),
    )
    assert loaded.provider_receipt == receipt_binding
    assert np.array_equal(matrix[0], _unit_vector(4096))

    def forbidden(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("resumed capture must not call the provider")

    async with httpx.AsyncClient(
        base_url="https://openrouter.ai/api/v1/",
        transport=httpx.MockTransport(forbidden),
    ) as client:
        resumed = await capture_qwen_embedding_replay(
            input_path=input_path,
            openrouter_base_url="https://openrouter.ai/api/v1",
            openrouter_api_key_file=key_path,
            provider_receipt_output_path=tmp_path / "provider-receipt.json",
            replay_output_path=tmp_path / "query-replay.jsonl",
            state_directory=tmp_path / "state",
            batch_size=1,
            client=client,
            token_counter=lambda _value: 3,
        )
    assert resumed == (receipt_binding, replay_binding)

    moved_receipt = tmp_path / "moved-output" / "provider-receipt.json"
    moved_replay = tmp_path / "moved-output" / "query-replay.jsonl"
    async with httpx.AsyncClient(
        base_url="https://openrouter.ai/api/v1/",
        transport=httpx.MockTransport(forbidden),
    ) as client:
        moved = await capture_qwen_embedding_replay(
            input_path=input_path,
            openrouter_base_url="https://openrouter.ai/api/v1",
            openrouter_api_key_file=key_path,
            provider_receipt_output_path=moved_receipt,
            replay_output_path=moved_replay,
            state_directory=tmp_path / "state",
            batch_size=1,
            client=client,
            token_counter=lambda _value: 3,
        )
    assert moved == (receipt_binding, replay_binding)
    assert _binding(moved_receipt) == receipt_binding
    assert _binding(moved_replay) == replay_binding


@pytest.mark.parametrize("lane", ("qwen", "v109"))
@pytest.mark.parametrize(
    "unsafe_url",
    (
        "https://example.invalid/api/v1",
        "https://openrouter.ai:443/api/v1",
        "https://user:secret@openrouter.ai/api/v1",
        "https://openrouter.ai/api/v1?destination=evil",
        "https://openrouter.ai/api/v1#fragment",
        "https://openrouter.ai\\@example.invalid/api/v1",
        "https://openrouter.ai/api/v1\x01",
        "https://openrouter.ai/api/v1/embeddings",
    ),
)
@pytest.mark.asyncio
async def test_live_capture_rejects_nonofficial_url_before_secret_or_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lane: str,
    unsafe_url: str,
) -> None:
    secret_reads = 0
    client_constructions = 0

    def forbidden_secret(_path: Path) -> str:
        nonlocal secret_reads
        secret_reads += 1
        raise AssertionError("unsafe URL must fail before the credential is read")

    def forbidden_client(*_args: object, **_kwargs: object) -> None:
        nonlocal client_constructions
        client_constructions += 1
        raise AssertionError("unsafe URL must fail before HTTP client construction")

    monkeypatch.setattr(producer, "_read_secret", forbidden_secret)
    monkeypatch.setattr(producer.httpx, "AsyncClient", forbidden_client)
    with pytest.raises(GoldCaptureError, match="openrouter_base_url_invalid"):
        if lane == "qwen":
            await capture_qwen_embedding_replay(
                input_path=tmp_path / "missing-input",
                openrouter_base_url=unsafe_url,
                openrouter_api_key_file=tmp_path / "missing-key",
                provider_receipt_output_path=tmp_path / "receipt.json",
                replay_output_path=tmp_path / "replay.jsonl",
                state_directory=tmp_path / "state",
                token_counter=lambda _value: 1,
            )
        else:
            await capture_v109_query_embedding_replay(
                gold_path=tmp_path / "missing-gold",
                expected_gold_sha256="a" * 64,
                generation_manifest_path=tmp_path / "missing-manifest",
                database_path=tmp_path / "missing-database",
                openrouter_base_url=unsafe_url,
                openrouter_api_key_file=tmp_path / "missing-key",
                provider_receipt_output_path=tmp_path / "receipt.json",
                replay_output_path=tmp_path / "replay.jsonl",
                state_directory=tmp_path / "state",
                release_gate=False,
            )
    assert secret_reads == 0
    assert client_constructions == 0


@pytest.mark.asyncio
async def test_qwen_live_rejects_injected_client_destination_before_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    formatted = format_qwen3_query("혜택은?")
    input_path = tmp_path / "qwen-inputs.jsonl"
    input_path.write_bytes(
        producer._qwen_input_payload(
            source_commit="1" * 40,
            input_kind="query",
            embedding_profile_id=qwen3_embedding_profile_id(
                "deepinfra",
                maximum_tokens=8192,
            ),
            provider_id="deepinfra",
            maximum_tokens=8192,
            values=(("gold-001", formatted),),
            token_counter=lambda _value: 3,
        )
    )
    secret_reads = 0
    calls = 0

    def forbidden_secret(_path: Path) -> str:
        nonlocal secret_reads
        secret_reads += 1
        raise AssertionError("injected client must be rejected before secret read")

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise AssertionError("injected client mismatch must make zero requests")

    monkeypatch.setattr(producer, "_read_secret", forbidden_secret)
    async with httpx.AsyncClient(
        base_url="https://example.invalid/api/v1/",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(GoldCaptureError, match="openrouter_injected_client_invalid"):
            await capture_qwen_embedding_replay(
                input_path=input_path,
                openrouter_base_url="https://openrouter.ai/api/v1",
                openrouter_api_key_file=tmp_path / "missing-key",
                provider_receipt_output_path=tmp_path / "receipt.json",
                replay_output_path=tmp_path / "replay.jsonl",
                state_directory=tmp_path / "state",
                client=client,
                token_counter=lambda _value: 3,
            )
    assert secret_reads == 0
    assert calls == 0


@pytest.mark.parametrize("echo_location", ("body", "header"))
@pytest.mark.asyncio
async def test_qwen_live_rejects_response_that_echoes_api_key(
    tmp_path: Path,
    echo_location: str,
) -> None:
    commit = "1" * 40
    profile_id = qwen3_embedding_profile_id("deepinfra", maximum_tokens=8192)
    formatted = format_qwen3_query("혜택은?")
    input_path = tmp_path / "qwen-inputs.jsonl"
    input_path.write_bytes(
        producer._qwen_input_payload(
            source_commit=commit,
            input_kind="query",
            embedding_profile_id=profile_id,
            provider_id="deepinfra",
            maximum_tokens=8192,
            values=(("gold-001", formatted),),
            token_counter=lambda _value: 3,
        )
    )
    key_path = tmp_path / "openrouter.key"
    key_path.write_text("fixture-secret-key", encoding="utf-8")

    def handler(_request: httpx.Request) -> httpx.Response:
        headers = {"x-openrouter-provider": "DeepInfra"}
        if echo_location == "header":
            headers["x-fixture-echo"] = "fixture-secret-key"
        payload: dict[str, object] = {
            "data": [{"embedding": _unit_vector(4096).tolist(), "index": 0}],
            "model": "Qwen/Qwen3-Embedding-8B",
        }
        if echo_location == "body":
            payload["echo"] = "fixture-secret-key"
        return httpx.Response(
            200,
            headers=headers,
            json=payload,
        )

    async with httpx.AsyncClient(
        base_url="https://openrouter.ai/api/v1/",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(GoldCaptureError, match="qwen_provider_response_contains_api_key"):
            await capture_qwen_embedding_replay(
                input_path=input_path,
                openrouter_base_url="https://openrouter.ai/api/v1",
                openrouter_api_key_file=key_path,
                provider_receipt_output_path=tmp_path / "provider-receipt.json",
                replay_output_path=tmp_path / "query-replay.jsonl",
                state_directory=tmp_path / "state",
                batch_size=1,
                client=client,
                token_counter=lambda _value: 3,
            )
    assert not (tmp_path / "provider-receipt.json").exists()
    assert not list(tmp_path.glob("qwen-response-*.json"))
    assert not list((tmp_path / "state").glob("**/qwen-response-*.json"))
