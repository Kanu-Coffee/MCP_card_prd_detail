from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from conftest import FakeEmbedder, create_database
from v5_fixtures import install_v5_fixture

import cardrag_mcp.gold_capture as capture_module
from cardrag_mcp.aggregation_profile import QueryScoreCoverage, RowScore, ScoreArtifactManifest
from cardrag_mcp.evaluation import (
    EvaluatedAnswer,
    QueryRunResult,
    RetrievedContract,
    RetrievedSpan,
    RunArtifactManifest,
    ShadowObservation,
    V109BaselineObservation,
)
from cardrag_mcp.exact import V5ExactRepository
from cardrag_mcp.gold_capture import (
    AnswerArtifactManifest,
    AnswerRecord,
    ArtifactBinding,
    CorpusInventoryManifest,
    CorpusInventoryRow,
    ExternalObservationManifest,
    ExternalQueryObservation,
    GoldCaptureError,
    LaneCaptureReceipt,
    NativeV5AttestationManifest,
    NativeV5QueryAttestation,
    PageGenerationManifest,
    capture_native_v5_lanes,
    seal_external_observation,
    validate_capture_set,
    validate_native_v5_capture,
)
from cardrag_mcp.reranker import (
    RerankerScore,
    RerankerShadowLane,
    RerankerShadowStore,
)
from cardrag_mcp.store import GenerationStore


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def test_gold_capture_provider_url_is_normalized_before_credentials() -> None:
    assert (
        capture_module._validated_openrouter_base_url("https://openrouter.ai/api/v1/")
        == "https://openrouter.ai/api/v1"
    )


@pytest.mark.parametrize(
    "value",
    (
        "",
        "http://openrouter.ai/api/v1",
        "https://user:secret@openrouter.ai/api/v1",
        "https://openrouter.ai/api/v1?redirect=1",
        "https://openrouter.ai/api/v1#fragment",
        "https://openrouter.ai:invalid/api/v1",
        "//openrouter.ai/api/v1",
        " https://openrouter.ai/api/v1",
        "https://openrouter.ai/api/v1\n",
        "https:\\evil.example\\api",
    ),
)
def test_gold_capture_rejects_unsafe_provider_url(value: str) -> None:
    with pytest.raises(GoldCaptureError, match="openrouter_base_url_invalid"):
        capture_module._validated_openrouter_base_url(value)


def _write_jsonl(path: Path, records: list[object]) -> ArtifactBinding:
    body = b"".join(_canonical(record) + b"\n" for record in records)
    path.write_bytes(body)
    return ArtifactBinding(sha256=hashlib.sha256(body).hexdigest(), size_bytes=len(body))


def _gold_record(contract_id: str, span_id: str, *, query: str = "혜택") -> dict[str, object]:
    return {
        "condition_groups": [],
        "contracts": [{"contract_revision_id": contract_id, "relevance": 3}],
        "expected_numeric_facts": [],
        "expected_revision_ids": [],
        "high_risk": False,
        "no_answer": False,
        "query_id": "gold-001",
        "question": query,
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
                "text_sha256": "a" * 64,
            }
        ],
    }


def _qwen_page_fixture(tmp_path: Path) -> dict[str, object]:
    gold = tmp_path / "gold.jsonl"
    gold_binding = _write_jsonl(gold, [_gold_record("contract-a", "chunk-a")])
    dimension = 4096
    matrix = np.zeros((2, dimension), dtype="<f4")
    matrix[0, 0] = 1.0
    matrix[1, 1] = 1.0
    vectors = tmp_path / "vectors.f32"
    vectors.write_bytes(matrix.tobytes())
    vector_binding = ArtifactBinding(
        sha256=hashlib.sha256(vectors.read_bytes()).hexdigest(),
        size_bytes=vectors.stat().st_size,
    )
    database = tmp_path / "page.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) STRICT, WITHOUT ROWID;
            CREATE TABLE evaluation_chunks(
              row_index INTEGER PRIMARY KEY,
              chunk_id TEXT NOT NULL UNIQUE,
              contract_revision_id TEXT NOT NULL,
              span_id TEXT NOT NULL UNIQUE,
              input_sha256 TEXT NOT NULL
            ) STRICT;
            """
        )
        connection.executemany(
            "INSERT INTO metadata VALUES(?,?)",
            (
                ("schema_id", "cardrag.evaluation-page.v1"),
                ("generation_id", "qwen-page-generation"),
                ("embedding_model", "qwen/qwen3-embedding-8b"),
                ("embedding_dimension", "4096"),
                ("chunking_policy", "cardrag.page-window-1600.v1"),
                ("maximum_chars", "1600"),
            ),
        )
        connection.executemany(
            "INSERT INTO evaluation_chunks VALUES(?,?,?,?,?)",
            (
                (0, "chunk-a", "contract-a", "chunk-a", "1" * 64),
                (1, "chunk-b", "contract-b", "chunk-b", "2" * 64),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    database_binding = ArtifactBinding(
        sha256=hashlib.sha256(database.read_bytes()).hexdigest(),
        size_bytes=database.stat().st_size,
    )
    inventory = tmp_path / "inventory.jsonl"
    inventory_manifest = CorpusInventoryManifest(
        schema_version="cardrag.gold-corpus-inventory.v1",
        lane="qwen_page",
        generation_id="qwen-page-generation",
        serving_database_sha256=database_binding.sha256,
        vector_artifact_sha256=vector_binding.sha256,
        embedding_dimension=4096,
        row_count=2,
    )
    inventory_rows = [
        CorpusInventoryRow(
            schema_version="cardrag.gold-corpus-row.v1",
            row_index=index,
            evidence_id=f"chunk-{chr(ord('a') + index)}",
            contract_revision_id=f"contract-{chr(ord('a') + index)}",
            span_id=f"chunk-{chr(ord('a') + index)}",
            input_sha256=str(index + 1) * 64,
            embedding_f32_sha256=hashlib.sha256(matrix[index].tobytes()).hexdigest(),
        )
        for index in range(2)
    ]
    inventory_binding = _write_jsonl(
        inventory,
        [
            inventory_manifest.model_dump(mode="json"),
            *(row.model_dump(mode="json") for row in inventory_rows),
        ],
    )
    page_manifest_path = tmp_path / "page-manifest.json"
    page_manifest = PageGenerationManifest(
        schema_version="cardrag.evaluation-page-generation.v1",
        source_commit="1" * 40,
        generation_id="qwen-page-generation",
        serving_schema="cardrag.evaluation-page.v1",
        serving_database=database_binding,
        vector_artifact=vector_binding,
        embedding_model="qwen/qwen3-embedding-8b",
        embedding_dimension=4096,
        embedding_profile_id="qwen-page-profile",
        chunking_policy="cardrag.page-window-1600.v1",
        maximum_chars=1600,
        row_count=2,
        corpus_inventory_sha256=inventory_binding.sha256,
    )
    page_manifest_path.write_bytes(page_manifest.canonical_bytes())
    page_manifest_binding = ArtifactBinding(
        sha256=hashlib.sha256(page_manifest_path.read_bytes()).hexdigest(),
        size_bytes=page_manifest_path.stat().st_size,
    )
    query_vector = matrix[0].tobytes()
    result = {
        "answer": {
            "citation_span_ids": [],
            "no_answer": False,
            "numeric_facts": [],
            "selected_revision_ids": [],
            "text": "실제 캡처 답변",
        },
        "contracts": [
            {"contract_revision_id": "contract-a", "rank": 1, "score": 1.0},
            {"contract_revision_id": "contract-b", "rank": 2, "score": 0.0},
        ],
        "lane": "qwen_page",
        "query_id": "gold-001",
        "schema_version": "cardrag.gold-run-result.v1",
        "spans": [
            {"contract_revision_id": "contract-a", "rank": 1, "score": 1.0, "span_id": "chunk-a"},
            {"contract_revision_id": "contract-b", "rank": 2, "score": 0.0, "span_id": "chunk-b"},
        ],
    }
    observation = tmp_path / "observation.jsonl"
    observation_manifest = ExternalObservationManifest(
        schema_version="cardrag.gold-external-observation-artifact.v1",
        lane="qwen_page",
        capture_mode="external_reproducible",
        synthetic=False,
        gold_sha256=gold_binding.sha256,
        query_count=1,
        source_version="v1.0.10-candidate",
        source_commit="1" * 40,
        generation_id="qwen-page-generation",
        generation_manifest=page_manifest_binding,
        serving_schema="cardrag.evaluation-page.v1",
        serving_database=database_binding,
        vector_artifact=vector_binding,
        embedding_model="qwen/qwen3-embedding-8b",
        embedding_dimension=4096,
        embedding_profile_id="qwen-page-profile",
        retrieval_policy="qwen_page_window",
        scoring_contract="cardrag.qwen-page-exact-capture.v1",
        row_count=2,
        corpus_inventory_sha256=inventory_binding.sha256,
        approximate=False,
    )
    query_observation = ExternalQueryObservation.model_validate_json(
        _canonical(
            {
                "expected_contracts": 2,
                "expected_rows": 2,
                "lane": "qwen_page",
                "query_id": "gold-001",
                "query_sha256": hashlib.sha256("혜택".encode()).hexdigest(),
                "query_vector_f32_base64": base64.b64encode(query_vector).decode(),
                "query_vector_sha256": hashlib.sha256(query_vector).hexdigest(),
                "raw_rows": [
                    {
                        "contract_revision_id": "contract-a",
                        "dense_rank": 1,
                        "dense_score": 1.0,
                        "evidence_id": "chunk-a",
                        "input_sha256": "1" * 64,
                        "lexical_rank": None,
                        "row_index": 0,
                        "span_id": "chunk-a",
                    },
                    {
                        "contract_revision_id": "contract-b",
                        "dense_rank": 2,
                        "dense_score": 0.0,
                        "evidence_id": "chunk-b",
                        "input_sha256": "2" * 64,
                        "lexical_rank": None,
                        "row_index": 1,
                        "span_id": "chunk-b",
                    },
                ],
                "result": result,
                "schema_version": "cardrag.gold-external-query-observation.v1",
                "scored_contracts": 2,
                "scored_rows": 2,
            }
        )
    )
    observation_binding = _write_jsonl(
        observation,
        [observation_manifest.model_dump(mode="json"), query_observation.model_dump(mode="json")],
    )
    return {
        "database": database,
        "gold": gold,
        "gold_binding": gold_binding,
        "inventory": inventory,
        "inventory_binding": inventory_binding,
        "manifest": page_manifest_path,
        "observation": observation,
        "observation_binding": observation_binding,
        "vectors": vectors,
    }


def test_external_qwen_page_capture_recomputes_every_score_and_is_atomic(tmp_path: Path) -> None:
    fixture = _qwen_page_fixture(tmp_path)
    output = tmp_path / "qwen_page.jsonl"
    receipt_path = tmp_path / "qwen_page.receipt.json"

    receipt = seal_external_observation(
        gold_path=fixture["gold"],
        expected_gold_sha256=fixture["gold_binding"].sha256,
        observation_path=fixture["observation"],
        expected_observation_sha256=fixture["observation_binding"].sha256,
        inventory_path=fixture["inventory"],
        expected_inventory_sha256=fixture["inventory_binding"].sha256,
        generation_manifest_path=fixture["manifest"],
        database_path=fixture["database"],
        vector_path=fixture["vectors"],
        output_path=output,
        receipt_path=receipt_path,
        release_gate=False,
    )
    resumed = seal_external_observation(
        gold_path=fixture["gold"],
        expected_gold_sha256=fixture["gold_binding"].sha256,
        observation_path=fixture["observation"],
        expected_observation_sha256=fixture["observation_binding"].sha256,
        inventory_path=fixture["inventory"],
        expected_inventory_sha256=fixture["inventory_binding"].sha256,
        generation_manifest_path=fixture["manifest"],
        database_path=fixture["database"],
        vector_path=fixture["vectors"],
        output_path=output,
        receipt_path=receipt_path,
        release_gate=False,
    )

    assert receipt == resumed
    assert receipt.capture_mode == "external_reproducible"
    assert not receipt.release_eligible
    assert receipt.run_artifact.sha256 == hashlib.sha256(output.read_bytes()).hexdigest()


def test_external_qwen_page_capture_rejects_raw_score_tamper_and_symlink(tmp_path: Path) -> None:
    fixture = _qwen_page_fixture(tmp_path)
    records = [json.loads(line) for line in fixture["observation"].read_bytes().splitlines()]
    records[1]["raw_rows"][0]["dense_score"] = 0.5
    tampered = tmp_path / "tampered.jsonl"
    tampered_binding = _write_jsonl(tampered, records)

    with pytest.raises(GoldCaptureError, match="external_raw_score_provenance_mismatch"):
        seal_external_observation(
            gold_path=fixture["gold"],
            expected_gold_sha256=fixture["gold_binding"].sha256,
            observation_path=tampered,
            expected_observation_sha256=tampered_binding.sha256,
            inventory_path=fixture["inventory"],
            expected_inventory_sha256=fixture["inventory_binding"].sha256,
            generation_manifest_path=fixture["manifest"],
            database_path=fixture["database"],
            vector_path=fixture["vectors"],
            output_path=tmp_path / "bad.jsonl",
            receipt_path=tmp_path / "bad.receipt.json",
            release_gate=False,
        )

    symlink = tmp_path / "observation-link.jsonl"
    symlink.symlink_to(fixture["observation"])
    with pytest.raises(GoldCaptureError, match="external_observation_not_regular"):
        seal_external_observation(
            gold_path=fixture["gold"],
            expected_gold_sha256=fixture["gold_binding"].sha256,
            observation_path=symlink,
            expected_observation_sha256=fixture["observation_binding"].sha256,
            inventory_path=fixture["inventory"],
            expected_inventory_sha256=fixture["inventory_binding"].sha256,
            generation_manifest_path=fixture["manifest"],
            database_path=fixture["database"],
            vector_path=fixture["vectors"],
            output_path=tmp_path / "link.jsonl",
            receipt_path=tmp_path / "link.receipt.json",
            release_gate=False,
        )


def test_external_v109_capture_recomputes_dense_and_lexical_rrf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = create_database(
        tmp_path / "generation-v109" / "index.sqlite3",
        "generation-v109",
        schema_id="cardrag.serving-db.v4",
    )
    database = fixture.database
    database_binding = ArtifactBinding(
        sha256=hashlib.sha256(database.read_bytes()).hexdigest(),
        size_bytes=database.stat().st_size,
    )
    connection = sqlite3.connect(database)
    try:
        source_rows = connection.execute(
            """SELECT e.evidence_id,e.document_id,e.text,e.embedding
                 FROM evidence AS e ORDER BY e.evidence_id"""
        ).fetchall()
    finally:
        connection.close()
    inventory_path = tmp_path / "v109-inventory.jsonl"
    inventory_manifest = CorpusInventoryManifest(
        schema_version="cardrag.gold-corpus-inventory.v1",
        lane="v109_baseline",
        generation_id="generation-v109",
        serving_database_sha256=database_binding.sha256,
        vector_artifact_sha256=None,
        embedding_dimension=1536,
        row_count=len(source_rows),
    )
    inventory_rows = [
        CorpusInventoryRow(
            schema_version="cardrag.gold-corpus-row.v1",
            row_index=index,
            evidence_id=str(row[0]),
            contract_revision_id=str(row[1]),
            span_id=str(row[0]),
            input_sha256=hashlib.sha256(str(row[2]).encode()).hexdigest(),
            embedding_f32_sha256=hashlib.sha256(bytes(row[3])).hexdigest(),
        )
        for index, row in enumerate(source_rows)
    ]
    inventory_binding = _write_jsonl(
        inventory_path,
        [
            inventory_manifest.model_dump(mode="json"),
            *(row.model_dump(mode="json") for row in inventory_rows),
        ],
    )
    gold_path = tmp_path / "v109-gold.jsonl"
    gold_binding = _write_jsonl(
        gold_path,
        [_gold_record(str(source_rows[0][1]), str(source_rows[0][0]), query="airport lounge")],
    )
    generation_binding = ArtifactBinding(sha256="f" * 64, size_bytes=123)
    external_manifest = ExternalObservationManifest(
        schema_version="cardrag.gold-external-observation-artifact.v1",
        lane="v109_baseline",
        capture_mode="external_reproducible",
        synthetic=False,
        gold_sha256=gold_binding.sha256,
        query_count=1,
        source_version="v1.0.9",
        source_commit="fee8f65a9fda7ae0c286ac92cf4c3f55c1a6f113",
        generation_id="generation-v109",
        generation_manifest=generation_binding,
        serving_schema="cardrag.serving-db.v4",
        serving_database=database_binding,
        vector_artifact=None,
        embedding_model="openai/text-embedding-3-small",
        embedding_dimension=1536,
        embedding_profile_id="cardrag.embedding.v109-small.v1",
        retrieval_policy="small_rrf",
        maximum_candidates=250,
        scoring_contract="cardrag.v109-small-dense-rrf-capture.v1",
        row_count=len(source_rows),
        corpus_inventory_sha256=inventory_binding.sha256,
        approximate=False,
    )
    query_vector = np.zeros((1536,), dtype="<f4")
    query_vector[0] = 1.0
    scored = [
        (float(np.frombuffer(bytes(row[3]), dtype="<f4") @ query_vector), index, row)
        for index, row in enumerate(source_rows)
    ]
    dense_order = sorted(scored, key=lambda item: (-item[0], str(item[2][0])))
    dense_rank = {index: rank for rank, (_score, index, _row) in enumerate(dense_order, start=1)}
    lexical = capture_module._v109_lexical_ranks(database, "airport lounge", limit=250)
    raw_rows = [
        {
            "contract_revision_id": str(row[1]),
            "dense_rank": dense_rank[index],
            "dense_score": score,
            "evidence_id": str(row[0]),
            "input_sha256": inventory_rows[index].input_sha256,
            "lexical_rank": lexical.get(str(row[0])),
            "row_index": index,
            "span_id": str(row[0]),
        }
        for score, index, row in scored
    ]
    dense_spans = [
        {
            "contract_revision_id": str(row[1]),
            "rank": rank,
            "score": score,
            "span_id": str(row[0]),
        }
        for rank, (score, _index, row) in enumerate(dense_order, start=1)
    ]

    def contracts(spans: list[dict[str, object]]) -> list[dict[str, object]]:
        output: list[dict[str, object]] = []
        seen: set[str] = set()
        for span in spans:
            contract_id = str(span["contract_revision_id"])
            if contract_id not in seen:
                seen.add(contract_id)
                output.append(
                    {
                        "contract_revision_id": contract_id,
                        "rank": len(output) + 1,
                        "score": span["score"],
                    }
                )
        return output

    fused = sorted(
        (
            (
                (0.0 if row["lexical_rank"] is None else 1 / (60 + int(row["lexical_rank"])))
                + 1 / (60 + int(row["dense_rank"])),
                row,
            )
            for row in raw_rows
        ),
        key=lambda item: (-item[0], str(item[1]["evidence_id"])),
    )
    primary_spans = [
        {
            "contract_revision_id": row["contract_revision_id"],
            "rank": rank,
            "score": score,
            "span_id": row["span_id"],
        }
        for rank, (score, row) in enumerate(fused, start=1)
    ]
    query_observation = ExternalQueryObservation.model_validate_json(
        _canonical(
            {
                "expected_contracts": len({str(row[1]) for row in source_rows}),
                "expected_rows": len(source_rows),
                "lane": "v109_baseline",
                "query_id": "gold-001",
                "query_sha256": hashlib.sha256(b"airport lounge").hexdigest(),
                "query_vector_f32_base64": base64.b64encode(query_vector.tobytes()).decode(),
                "query_vector_sha256": hashlib.sha256(query_vector.tobytes()).hexdigest(),
                "raw_rows": raw_rows,
                "result": {
                    "answer": {
                        "citation_span_ids": [],
                        "no_answer": False,
                        "numeric_facts": [],
                        "selected_revision_ids": [],
                        "text": "봉인된 v1.0.9 답변",
                    },
                    "contracts": contracts(primary_spans),
                    "lane": "v109_baseline",
                    "query_id": "gold-001",
                    "schema_version": "cardrag.gold-run-result.v1",
                    "spans": primary_spans,
                    "v109_baseline": {
                        "dense_contracts": contracts(dense_spans),
                        "dense_spans": dense_spans,
                        "kind": "v109_small_rrf",
                        "rrf_k": 60,
                    },
                },
                "schema_version": "cardrag.gold-external-query-observation.v1",
                "scored_contracts": len({str(row[1]) for row in source_rows}),
                "scored_rows": len(source_rows),
            }
        )
    )
    observation_path = tmp_path / "v109-observation.jsonl"
    observation_binding = _write_jsonl(
        observation_path,
        [external_manifest.model_dump(mode="json"), query_observation.model_dump(mode="json")],
    )
    fake_generation = SimpleNamespace(
        schema_version="cardrag.generation.v4",
        serving_schema="cardrag.serving-db.v4",
        generation_id="generation-v109",
        serving_database=database_binding,
    )
    monkeypatch.setattr(
        capture_module,
        "_load_generation_manifest",
        lambda _path: (fake_generation, generation_binding),
    )

    receipt = seal_external_observation(
        gold_path=gold_path,
        expected_gold_sha256=gold_binding.sha256,
        observation_path=observation_path,
        expected_observation_sha256=observation_binding.sha256,
        inventory_path=inventory_path,
        expected_inventory_sha256=inventory_binding.sha256,
        generation_manifest_path=tmp_path / "v109-manifest.json",
        database_path=database,
        vector_path=None,
        output_path=tmp_path / "v109.jsonl",
        receipt_path=tmp_path / "v109.receipt.json",
        release_gate=False,
    )

    assert receipt.lane == "v109_baseline"
    assert receipt.capture_mode == "external_reproducible"


class _FakeRerankerClient:
    def __init__(self) -> None:
        self.calls = 0

    async def rerank(self, _query: str, documents: list[str]) -> tuple[RerankerScore, ...]:
        self.calls += 1
        return tuple(
            RerankerScore(index=index, relevance_score=1.0 - index / 100)
            for index in range(len(documents))
        )

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_native_v5_capture_calls_actual_apis_resumes_and_revalidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = GenerationStore(tmp_path / "runtime", maximum_vector_bytes=2 * 1024 * 1024)
    fixture, handle = install_v5_fixture(store)
    gold_path = tmp_path / "gold.jsonl"
    span_id = f"{fixture.current_revision_id}-item"
    query_text = "알파 카드 혜택"
    gold_binding = _write_jsonl(
        gold_path,
        [_gold_record(fixture.current_revision_id, span_id, query=query_text)],
    )
    query_vector = np.zeros((4096,), dtype=np.float32)
    query_vector[0] = 1.0
    embedder = FakeEmbedder(query_vector)
    capture = await V5ExactRepository(store, embedder).capture_unscoped_current_scores(
        query_text,
        handle=handle,
    )
    database_binding = ArtifactBinding(
        sha256=hashlib.sha256(fixture.database.read_bytes()).hexdigest(),
        size_bytes=fixture.database.stat().st_size,
    )
    sidecar_path = fixture.database.parent / "vectors.f32"
    sidecar_binding = ArtifactBinding(
        sha256=hashlib.sha256(sidecar_path.read_bytes()).hexdigest(),
        size_bytes=sidecar_path.stat().st_size,
    )
    generation_sha256 = "e" * 64
    source_commit = "1" * 40
    score_path = tmp_path / "scores.jsonl"
    score_manifest = ScoreArtifactManifest(
        schema_version="cardrag.document-aggregation-score-artifact.v1",
        gold_sha256=gold_binding.sha256,
        query_count=1,
        row_count=capture.scored_rows,
        source_commit=source_commit,
        generation_id=fixture.generation_id,
        generation_manifest_sha256=generation_sha256,
        serving_database_sha256=database_binding.sha256,
        vector_sidecar_sha256=sidecar_binding.sha256,
        exact_row_corpus_sha256=handle.metadata.exact_row_corpus_sha256,
        embedding_profile_id=handle.metadata.primary_embedding_profile_id,
        embedding_model="qwen/qwen3-embedding-8b",
        embedding_dimension=4096,
        exact=True,
        approximate=False,
        scoring_contract="cardrag.v5-exact-row-score.v1",
        temporal_scope_policy="gold-query.v1",
        runtime_document_aggregation_status=handle.metadata.document_aggregation_status,
        runtime_document_aggregation_policy=handle.metadata.document_aggregation_policy,
        runtime_sealed_profile_sha256=handle.metadata.sealed_profile_sha256,
    )
    coverage = QueryScoreCoverage(
        schema_version="cardrag.document-aggregation-query-coverage.v1",
        query_id="gold-001",
        query_sha256=capture.query_sha256,
        query_vector_sha256=capture.query_vector_sha256,
        expected_rows=capture.expected_rows,
        scored_rows=capture.scored_rows,
        active_contracts=capture.expected_active_contracts,
    )
    score_rows = [
        RowScore(
            schema_version="cardrag.document-aggregation-row-score.v1",
            query_id="gold-001",
            ordinal=ordinal,
            row_index=row.row_index,
            contract_revision_id=row.contract_revision_id,
            node_id=row.node_id,
            view_type=row.view_type,
            input_sha256=row.input_sha256,
            embedding_profile_id=row.embedding_profile_id,
            score=row.score,
        )
        for ordinal, row in enumerate(capture.rows)
    ]
    score_binding = _write_jsonl(
        score_path,
        [
            score_manifest.model_dump(mode="json"),
            coverage.model_dump(mode="json"),
            *(row.model_dump(mode="json") for row in score_rows),
        ],
    )
    answer_path = tmp_path / "answers.jsonl"
    answer_manifest = AnswerArtifactManifest(
        schema_version="cardrag.gold-answer-artifact.v1",
        lane="qwen_structure_exact",
        gold_sha256=gold_binding.sha256,
        query_count=1,
        generation_id=fixture.generation_id,
        generation_manifest_sha256=generation_sha256,
        answer_profile_id="sealed-answer-profile",
        synthetic=False,
    )
    answer_record = AnswerRecord(
        schema_version="cardrag.gold-answer.v1",
        query_id="gold-001",
        query_sha256=hashlib.sha256(query_text.encode()).hexdigest(),
        answer=EvaluatedAnswer(
            text="실제 캡처 답변",
            no_answer=False,
            citation_span_ids=(),
            numeric_facts=(),
            selected_revision_ids=(),
        ),
    )
    answer_binding = _write_jsonl(
        answer_path,
        [answer_manifest.model_dump(mode="json"), answer_record.model_dump(mode="json")],
    )
    fake_manifest = SimpleNamespace(
        schema_version="cardrag.generation.v5",
        generation_id=fixture.generation_id,
        serving_database=database_binding,
        vector_sidecar=SimpleNamespace(artifact=sidecar_binding),
        exact_row_corpus_sha256=handle.metadata.exact_row_corpus_sha256,
        primary_embedding_profile_id=handle.metadata.primary_embedding_profile_id,
        embedding_contract=SimpleNamespace(count=handle.metadata.embedding_count),
    )
    monkeypatch.setattr(
        capture_module,
        "_load_generation_manifest",
        lambda _path: (fake_manifest, ArtifactBinding(sha256=generation_sha256, size_bytes=123)),
    )
    monkeypatch.setattr(capture_module, "load_generation_handle", lambda *_args, **_kwargs: handle)
    state = tmp_path / "capture-state"
    state.mkdir()
    reranker_client = _FakeRerankerClient()
    reranker_lane = RerankerShadowLane(
        reranker_client,  # type: ignore[arg-type]
        RerankerShadowStore(
            state,
            maximum_jobs=10,
            maximum_total_bytes=8 * 1024 * 1024,
            maximum_artifact_bytes=1024 * 1024,
        ),
        maximum_candidates=64,
    )
    output = tmp_path / "output"

    first = await capture_native_v5_lanes(
        gold_path=gold_path,
        expected_gold_sha256=gold_binding.sha256,
        score_artifact_path=score_path,
        expected_score_artifact_sha256=score_binding.sha256,
        answer_artifact_path=answer_path,
        expected_answer_artifact_sha256=answer_binding.sha256,
        generation_manifest_path=tmp_path / "manifest.json",
        generation_directory=fixture.database.parent,
        object_root=handle.object_root,
        output_directory=output,
        state_directory=state,
        source_commit=source_commit,
        embedder=embedder,
        reranker_lane=reranker_lane,
        release_gate=False,
    )
    calls_after_first = len(embedder.calls)
    second = await capture_native_v5_lanes(
        gold_path=gold_path,
        expected_gold_sha256=gold_binding.sha256,
        score_artifact_path=score_path,
        expected_score_artifact_sha256=score_binding.sha256,
        answer_artifact_path=answer_path,
        expected_answer_artifact_sha256=answer_binding.sha256,
        generation_manifest_path=tmp_path / "manifest.json",
        generation_directory=fixture.database.parent,
        object_root=handle.object_root,
        output_directory=output,
        state_directory=state,
        source_commit=source_commit,
        embedder=embedder,
        reranker_lane=reranker_lane,
        release_gate=False,
    )

    assert first.resumed_queries == 0
    assert second.resumed_queries == 1
    assert len(embedder.calls) == calls_after_first
    assert reranker_client.calls == 1
    native_attestation = [
        json.loads(line) for line in first.attestation_path.read_bytes().splitlines()
    ]
    assert (
        native_attestation[1]["raw_expected_embedding_rows"]
        > native_attestation[1]["expected_embedding_rows"]
    )
    receipts = validate_native_v5_capture(
        gold_path=gold_path,
        expected_gold_sha256=gold_binding.sha256,
        score_artifact_path=score_path,
        answer_artifact_path=answer_path,
        generation_manifest_path=tmp_path / "manifest.json",
        generation_directory=fixture.database.parent,
        object_root=handle.object_root,
        attestation_path=first.attestation_path,
        run_paths=first.run_paths,
        receipt_paths=first.receipt_paths,
        reranker_state_root=state,
        release_gate=False,
    )
    assert set(receipts) == {"qwen_structure_exact", "lexical_shadow", "reranker_shadow"}
    assert all(not receipt.release_eligible for receipt in receipts.values())

    shard = state / "query-000.json"
    raw = json.loads(shard.read_bytes())
    raw["attestation"]["raw_score_rows_sha256"] = "0" * 64
    shard.chmod(0o600)
    shard.write_bytes(_canonical(raw))
    with pytest.raises(GoldCaptureError, match="native_capture_resume_shard_mismatch"):
        await capture_native_v5_lanes(
            gold_path=gold_path,
            expected_gold_sha256=gold_binding.sha256,
            score_artifact_path=score_path,
            expected_score_artifact_sha256=score_binding.sha256,
            answer_artifact_path=answer_path,
            expected_answer_artifact_sha256=answer_binding.sha256,
            generation_manifest_path=tmp_path / "manifest.json",
            generation_directory=fixture.database.parent,
            object_root=handle.object_root,
            output_directory=output,
            state_directory=state,
            source_commit=source_commit,
            embedder=embedder,
            reranker_lane=reranker_lane,
            release_gate=False,
        )


def test_capture_set_revalidates_all_attestations_and_complete_query_coverage(
    tmp_path: Path,
) -> None:
    gold_path = tmp_path / "set-gold.jsonl"
    gold_binding = _write_jsonl(gold_path, [_gold_record("contract-a", "span-a")])
    query_sha256 = hashlib.sha256("혜택".encode()).hexdigest()
    answer = EvaluatedAnswer(
        text="봉인 답변",
        no_answer=False,
        citation_span_ids=("span-a",),
        numeric_facts=(),
        selected_revision_ids=("contract-a",),
    )
    contract = RetrievedContract(contract_revision_id="contract-a", rank=1, score=1.0)
    span = RetrievedSpan(
        span_id="span-a",
        contract_revision_id="contract-a",
        rank=1,
        score=1.0,
    )
    dense_trace = V109BaselineObservation(
        kind="v109_small_rrf",
        rrf_k=60,
        dense_contracts=(contract,),
        dense_spans=(span,),
    )
    results = {
        "v109_baseline": QueryRunResult(
            schema_version="cardrag.gold-run-result.v1",
            query_id="gold-001",
            lane="v109_baseline",
            contracts=(contract,),
            spans=(span,),
            answer=answer,
            v109_baseline=dense_trace,
        ),
        "qwen_page": QueryRunResult(
            schema_version="cardrag.gold-run-result.v1",
            query_id="gold-001",
            lane="qwen_page",
            contracts=(contract,),
            spans=(span,),
            answer=answer,
        ),
        "qwen_structure_exact": QueryRunResult(
            schema_version="cardrag.gold-run-result.v1",
            query_id="gold-001",
            lane="qwen_structure_exact",
            contracts=(contract,),
            spans=(span,),
            answer=answer,
        ),
        "lexical_shadow": QueryRunResult(
            schema_version="cardrag.gold-run-result.v1",
            query_id="gold-001",
            lane="lexical_shadow",
            contracts=(contract,),
            spans=(span,),
            answer=answer,
            shadow=ShadowObservation(
                kind="lexical",
                influenced_primary_ordering=False,
                contracts=(contract,),
                spans=(span,),
            ),
        ),
        "reranker_shadow": QueryRunResult(
            schema_version="cardrag.gold-run-result.v1",
            query_id="gold-001",
            lane="reranker_shadow",
            contracts=(contract,),
            spans=(span,),
            answer=answer,
            shadow=ShadowObservation(
                kind="reranker",
                influenced_primary_ordering=False,
                contracts=(contract,),
                spans=(span,),
            ),
        ),
    }
    external_bindings = {
        "v109_baseline": {
            "generation": ArtifactBinding(sha256="1" * 64, size_bytes=1),
            "database": ArtifactBinding(sha256="2" * 64, size_bytes=1),
            "vector": None,
            "generation_id": "v109-generation",
        },
        "qwen_page": {
            "generation": ArtifactBinding(sha256="3" * 64, size_bytes=1),
            "database": ArtifactBinding(sha256="4" * 64, size_bytes=1),
            "vector": ArtifactBinding(sha256="5" * 64, size_bytes=1),
            "generation_id": "qwen-page-generation",
        },
    }
    attestation_paths: dict[str, Path] = {}
    attestation_bindings: dict[str, ArtifactBinding] = {}
    for lane in ("v109_baseline", "qwen_page"):
        binding = external_bindings[lane]
        dimension = 1536 if lane == "v109_baseline" else 4096
        query_vector = np.zeros((dimension,), dtype="<f4")
        query_vector[0] = 1.0
        manifest = ExternalObservationManifest(
            schema_version="cardrag.gold-external-observation-artifact.v1",
            lane=lane,  # type: ignore[arg-type]
            capture_mode="external_reproducible",
            synthetic=False,
            gold_sha256=gold_binding.sha256,
            query_count=1,
            source_version="v1.0.9" if lane == "v109_baseline" else "v1.0.10-candidate",
            source_commit=(
                "fee8f65a9fda7ae0c286ac92cf4c3f55c1a6f113" if lane == "v109_baseline" else "6" * 40
            ),
            generation_id=str(binding["generation_id"]),
            generation_manifest=binding["generation"],  # type: ignore[arg-type]
            serving_schema=(
                "cardrag.serving-db.v4" if lane == "v109_baseline" else "cardrag.evaluation-page.v1"
            ),
            serving_database=binding["database"],  # type: ignore[arg-type]
            vector_artifact=binding["vector"],  # type: ignore[arg-type]
            embedding_model=(
                "openai/text-embedding-3-small"
                if lane == "v109_baseline"
                else "qwen/qwen3-embedding-8b"
            ),
            embedding_dimension=dimension,  # type: ignore[arg-type]
            embedding_profile_id=f"{lane}-profile",
            retrieval_policy="small_rrf" if lane == "v109_baseline" else "qwen_page_window",
            maximum_candidates=250 if lane == "v109_baseline" else None,
            scoring_contract=(
                "cardrag.v109-small-dense-rrf-capture.v1"
                if lane == "v109_baseline"
                else "cardrag.qwen-page-exact-capture.v1"
            ),
            row_count=1,
            corpus_inventory_sha256="7" * 64,
            approximate=False,
        )
        observation = ExternalQueryObservation(
            schema_version="cardrag.gold-external-query-observation.v1",
            lane=lane,  # type: ignore[arg-type]
            query_id="gold-001",
            query_sha256=query_sha256,
            query_vector_sha256=hashlib.sha256(query_vector.tobytes()).hexdigest(),
            query_vector_f32_base64=base64.b64encode(query_vector.tobytes()).decode(),
            expected_rows=1,
            scored_rows=1,
            expected_contracts=1,
            scored_contracts=1,
            raw_rows=(
                capture_module.ExternalRawRow(
                    row_index=0,
                    evidence_id="span-a",
                    contract_revision_id="contract-a",
                    span_id="span-a",
                    input_sha256="8" * 64,
                    dense_score=1.0,
                    dense_rank=1,
                    lexical_rank=1 if lane == "v109_baseline" else None,
                ),
            ),
            result=results[lane],
        )
        path = tmp_path / f"{lane}.capture-attestation.jsonl"
        attestation_paths[lane] = path
        attestation_bindings[lane] = _write_jsonl(
            path,
            [manifest.model_dump(mode="json"), observation.model_dump(mode="json")],
        )

    native_score_path = tmp_path / "native-scores.jsonl"
    native_score_manifest = ScoreArtifactManifest(
        schema_version="cardrag.document-aggregation-score-artifact.v1",
        gold_sha256=gold_binding.sha256,
        query_count=1,
        row_count=1,
        source_commit="a" * 40,
        generation_id="native-generation",
        generation_manifest_sha256="b" * 64,
        serving_database_sha256="c" * 64,
        vector_sidecar_sha256="d" * 64,
        exact_row_corpus_sha256="e" * 64,
        embedding_profile_id="qwen-structure-profile",
        embedding_model="qwen/qwen3-embedding-8b",
        embedding_dimension=4096,
        exact=True,
        approximate=False,
        scoring_contract="cardrag.v5-exact-row-score.v1",
        temporal_scope_policy="gold-query.v1",
        runtime_document_aggregation_status="candidate_default",
        runtime_document_aggregation_policy="max_child",
        runtime_sealed_profile_sha256=None,
    )
    native_coverage = QueryScoreCoverage(
        schema_version="cardrag.document-aggregation-query-coverage.v1",
        query_id="gold-001",
        query_sha256=query_sha256,
        query_vector_sha256="0" * 64,
        expected_rows=1,
        scored_rows=1,
        active_contracts=1,
    )
    native_row = RowScore(
        schema_version="cardrag.document-aggregation-row-score.v1",
        query_id="gold-001",
        ordinal=0,
        row_index=0,
        contract_revision_id="contract-a",
        node_id="span-a",
        view_type="RAW_ITEM",
        input_sha256="8" * 64,
        embedding_profile_id="qwen-structure-profile",
        score=1.0,
    )
    native_score_binding = _write_jsonl(
        native_score_path,
        [
            native_score_manifest.model_dump(mode="json"),
            native_coverage.model_dump(mode="json"),
            native_row.model_dump(mode="json"),
        ],
    )
    raw_score_rows_sha256 = hashlib.sha256(
        _canonical(native_coverage.model_dump(mode="json"))
        + b"\n"
        + _canonical(native_row.model_dump(mode="json"))
        + b"\n"
    ).hexdigest()
    native_manifest = NativeV5AttestationManifest(
        schema_version="cardrag.gold-native-v5-attestation.v1",
        capture_mode="native_v5",
        synthetic=False,
        gold_sha256=gold_binding.sha256,
        query_count=1,
        source_commit="a" * 40,
        generation_id="native-generation",
        generation_manifest=ArtifactBinding(sha256="b" * 64, size_bytes=1),
        serving_database=ArtifactBinding(sha256="c" * 64, size_bytes=1),
        vector_sidecar=ArtifactBinding(sha256="d" * 64, size_bytes=1),
        exact_row_corpus_sha256="e" * 64,
        embedding_profile_id="qwen-structure-profile",
        embedding_model="qwen/qwen3-embedding-8b",
        embedding_dimension=4096,
        score_artifact=native_score_binding,
        answer_artifact=ArtifactBinding(sha256="f" * 64, size_bytes=1),
        raw_score_api="cardrag_mcp.exact.V5ExactRepository.capture_unscoped_current_scores",
        exact_api="cardrag_mcp.exact.V5ExactRepository.search",
        lexical_api="cardrag_mcp.exact.V5ExactRepository.search.lexical_shadow",
        reranker_api="cardrag_mcp.reranker.RerankerShadowLane.observe",
        reranker_model="qwen/qwen3-reranker-8b",
    )
    native_query = NativeV5QueryAttestation(
        schema_version="cardrag.gold-native-v5-query-attestation.v1",
        query_id="gold-001",
        query_sha256=query_sha256,
        query_vector_sha256="0" * 64,
        raw_score_rows_sha256=raw_score_rows_sha256,
        raw_expected_embedding_rows=1,
        raw_scored_embedding_rows=1,
        raw_active_contracts=1,
        expected_embedding_rows=1,
        scored_embedding_rows=1,
        expected_active_contracts=1,
        scored_contracts=1,
        exact_blocks=1,
        exact_response_sha256="2" * 64,
        lexical_status="succeeded",
        lexical_additional_evidence_count=0,
        reranker_artifact_sha256="3" * 64,
        qwen_structure_exact_result_sha256=capture_module.canonical_sha256(
            results["qwen_structure_exact"]
        ),
        lexical_shadow_result_sha256=capture_module.canonical_sha256(results["lexical_shadow"]),
        reranker_shadow_result_sha256=capture_module.canonical_sha256(results["reranker_shadow"]),
    )
    native_attestation_path = tmp_path / "native-v5-attestation.jsonl"
    native_attestation_binding = _write_jsonl(
        native_attestation_path,
        [native_manifest.model_dump(mode="json"), native_query.model_dump(mode="json")],
    )
    for lane in ("qwen_structure_exact", "lexical_shadow", "reranker_shadow"):
        attestation_paths[lane] = native_attestation_path
        attestation_bindings[lane] = native_attestation_binding

    run_paths: dict[str, Path] = {}
    receipt_paths: dict[str, Path] = {}
    expected_receipts: dict[str, str] = {}
    profile = {
        "v109_baseline": ("cardrag.eval.v109-small-rrf.v1", "small_rrf"),
        "qwen_page": ("cardrag.eval.qwen-page.v1", "qwen_page_window"),
        "qwen_structure_exact": (
            "cardrag.eval.qwen-structure-exact.v1",
            "qwen_structure_exact",
        ),
        "lexical_shadow": (
            "cardrag.eval.lexical-shadow.v1",
            "qwen_structure_exact_lexical_shadow",
        ),
        "reranker_shadow": (
            "cardrag.eval.reranker-shadow.v1",
            "qwen_structure_exact_reranker_shadow",
        ),
    }
    for lane in capture_module.LANES:
        native = lane in capture_module.NATIVE_V5_LANES
        external = external_bindings.get(lane)
        generation_id = "native-generation" if native else str(external["generation_id"])
        generation_sha256 = "b" * 64 if native else external["generation"].sha256
        manifest = RunArtifactManifest(
            schema_version="cardrag.gold-run-artifact.v1",
            lane=lane,
            profile_id=profile[lane][0],  # type: ignore[arg-type]
            gold_sha256=gold_binding.sha256,
            query_count=1,
            source_version="v1.0.10-candidate" if lane != "v109_baseline" else "v1.0.9",
            source_commit=(
                "fee8f65a9fda7ae0c286ac92cf4c3f55c1a6f113"
                if lane == "v109_baseline"
                else ("a" * 40 if native else "6" * 40)
            ),
            generation_id=generation_id,
            generation_manifest_sha256=generation_sha256,
            serving_schema=(
                "cardrag.serving-db.v5"
                if native
                else (
                    "cardrag.serving-db.v4"
                    if lane == "v109_baseline"
                    else "cardrag.evaluation-page.v1"
                )
            ),
            embedding_model=(
                "openai/text-embedding-3-small"
                if lane == "v109_baseline"
                else "qwen/qwen3-embedding-8b"
            ),
            embedding_dimension=1536 if lane == "v109_baseline" else 4096,
            retrieval_policy=profile[lane][1],  # type: ignore[arg-type]
            rrf_k=60 if lane == "v109_baseline" else None,
            shadow_only=lane in {"lexical_shadow", "reranker_shadow"},
            primary_lane=(
                "qwen_structure_exact" if lane in {"lexical_shadow", "reranker_shadow"} else None
            ),
            shadow_model="qwen/qwen3-reranker-8b" if lane == "reranker_shadow" else None,
        )
        run_path = tmp_path / f"{lane}.jsonl"
        run_binding = _write_jsonl(
            run_path,
            [manifest.model_dump(mode="json"), results[lane].model_dump(mode="json")],
        )
        receipt = LaneCaptureReceipt(
            schema_version="cardrag.gold-lane-capture-receipt.v1",
            lane=lane,
            capture_mode="native_v5" if native else "external_reproducible",
            release_eligible=True,
            gold_sha256=gold_binding.sha256,
            query_count=1,
            run_artifact=run_binding,
            attestation_artifact=attestation_bindings[lane],
            source_generation_id=generation_id,
            source_generation_manifest_sha256=generation_sha256,
            source_database_sha256=("c" * 64 if native else external["database"].sha256),
            source_vector_sha256=(
                "d" * 64
                if native
                else (None if external["vector"] is None else external["vector"].sha256)
            ),
            raw_score_artifact_sha256=(
                native_score_binding.sha256 if native else attestation_bindings[lane].sha256
            ),
        )
        receipt_path = tmp_path / f"{lane}.capture-receipt.json"
        receipt_path.write_bytes(receipt.canonical_bytes())
        run_paths[lane] = run_path
        receipt_paths[lane] = receipt_path
        expected_receipts[lane] = hashlib.sha256(receipt_path.read_bytes()).hexdigest()

    set_receipt = validate_capture_set(
        gold_path=gold_path,
        expected_gold_sha256=gold_binding.sha256,
        run_paths=run_paths,  # type: ignore[arg-type]
        receipt_paths=receipt_paths,  # type: ignore[arg-type]
        attestation_paths=attestation_paths,  # type: ignore[arg-type]
        native_score_artifact_path=native_score_path,
        expected_receipt_sha256=expected_receipts,  # type: ignore[arg-type]
        output_path=tmp_path / "capture-set.json",
        release_gate=False,
    )
    assert tuple(receipt.lane for receipt in set_receipt.lanes) == capture_module.LANES

    unsafe_attestation = tmp_path / "qwen-page-attestation-link.jsonl"
    unsafe_attestation.symlink_to(attestation_paths["qwen_page"])
    attestation_paths["qwen_page"] = unsafe_attestation
    with pytest.raises(GoldCaptureError, match="external_release_attestation_not_regular"):
        validate_capture_set(
            gold_path=gold_path,
            expected_gold_sha256=gold_binding.sha256,
            run_paths=run_paths,  # type: ignore[arg-type]
            receipt_paths=receipt_paths,  # type: ignore[arg-type]
            attestation_paths=attestation_paths,  # type: ignore[arg-type]
            native_score_artifact_path=native_score_path,
            expected_receipt_sha256=expected_receipts,  # type: ignore[arg-type]
            release_gate=False,
        )
