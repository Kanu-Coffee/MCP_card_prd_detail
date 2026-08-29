from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from conftest import FakeEmbedder
from v5_fixtures import install_v5_fixture

import cardrag_mcp.aggregation_capture as capture_module
from cardrag_mcp.aggregation import (
    aggregate_document_view_scores,
    exhaustive_profile_id,
)
from cardrag_mcp.aggregation_capture import AggregationCaptureError, capture_score_artifact
from cardrag_mcp.exact import V5ExactRepository
from cardrag_mcp.store import GenerationStore


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def test_three_runtime_policies_can_select_three_different_documents() -> None:
    documents = {
        "a": (("CONTRACT", 0.0), ("DETAIL", 0.9), ("TITLE", 0.0), ("RAW_ITEM", 0.0)),
        "b": (("CONTRACT", 0.0), ("DETAIL", 0.8), ("TITLE", 0.8), ("RAW_ITEM", 0.8)),
        "c": (("CONTRACT", 1.0), ("DETAIL", 0.7), ("TITLE", 0.7), ("RAW_ITEM", 0.7)),
    }

    winners = {
        policy: max(
            documents,
            key=lambda document: aggregate_document_view_scores(
                documents[document],
                policy,
            ),
        )
        for policy in ("max_child", "top3_mean", "contract_plus_child")
    }

    assert winners == {
        "max_child": "a",
        "top3_mean": "b",
        "contract_plus_child": "c",
    }


def test_exhaustive_resume_profile_is_bound_to_policy_and_sealed_hash() -> None:
    default = exhaustive_profile_id(policy="max_child", sealed_profile_sha256=None)
    selected_a = exhaustive_profile_id(
        policy="top3_mean",
        sealed_profile_sha256="a" * 64,
    )
    selected_b = exhaustive_profile_id(
        policy="top3_mean",
        sealed_profile_sha256="b" * 64,
    )

    assert len({default, selected_a, selected_b}) == 3
    with pytest.raises(ValueError, match="unsealed"):
        exhaustive_profile_id(policy="top3_mean", sealed_profile_sha256=None)


@pytest.mark.asyncio
async def test_actual_exact_score_capture_records_every_active_row(
    tmp_path: Path,
) -> None:
    store = GenerationStore(tmp_path / "state", maximum_vector_bytes=2 * 1024 * 1024)
    fixture, handle = install_v5_fixture(store)
    query = np.zeros((4096,), dtype=np.float32)
    query[0] = 1.0
    embedder = FakeEmbedder(query)
    repository = V5ExactRepository(store, embedder)

    captured = await repository.capture_unscoped_current_scores("혜택", handle=handle)

    assert captured.expected_active_contracts == 2
    assert captured.expected_rows == captured.scored_rows == len(captured.rows) == 4
    assert tuple(row.row_index for row in captured.rows) == (1, 2, 3, 4)
    assert {row.contract_revision_id for row in captured.rows} == {
        fixture.current_revision_id,
        fixture.ambiguous_revision_id,
    }
    assert captured.query_vector_sha256 == hashlib.sha256(query.astype("<f4").tobytes()).hexdigest()


@pytest.mark.asyncio
async def test_capture_artifact_resumes_without_provider_and_detects_shard_tamper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = GenerationStore(tmp_path / "runtime", maximum_vector_bytes=2 * 1024 * 1024)
    fixture, handle = install_v5_fixture(store)
    gold_path = tmp_path / "gold.jsonl"
    gold_record = {
        "condition_groups": [],
        "contracts": [{"contract_revision_id": fixture.current_revision_id, "relevance": 3}],
        "expected_numeric_facts": [],
        "expected_revision_ids": [],
        "high_risk": False,
        "no_answer": False,
        "query_id": "query-000",
        "question": "현재 혜택",
        "schema_version": "cardrag.gold-query.v1",
        "slices": ["benefit"],
        "spans": [
            {
                "contract_revision_id": fixture.current_revision_id,
                "page": 1,
                "relevance": 3,
                "roles": ["benefit"],
                "source_end": 1,
                "source_start": 0,
                "span_id": "span-000",
                "text_sha256": "b" * 64,
            }
        ],
    }
    gold_path.write_bytes(_canonical(gold_record) + b"\n")
    fake_manifest = SimpleNamespace(
        generation_id=fixture.generation_id,
        embedding_contract=SimpleNamespace(
            model="qwen/qwen3-embedding-8b",
            count=fixture.vector_count,
        ),
        document_aggregation_profile=None,
    )
    monkeypatch.setattr(
        capture_module,
        "_load_generation_manifest",
        lambda _path: (fake_manifest, "e" * 64),
    )
    monkeypatch.setattr(
        capture_module,
        "_verify_generation_files",
        lambda _manifest, _directory: ("f" * 64, "a" * 64),
    )
    monkeypatch.setattr(capture_module, "load_generation_handle", lambda *_args, **_kwargs: handle)
    query = np.zeros((4096,), dtype=np.float32)
    query[0] = 1.0
    output = tmp_path / "scores.jsonl"
    state = tmp_path / "capture-state"

    first_embedder = FakeEmbedder(query)
    first = await capture_score_artifact(
        gold_path=gold_path,
        generation_manifest_path=tmp_path / "manifest.json",
        generation_directory=fixture.database.parent,
        object_root=tmp_path / "objects",
        output_path=output,
        state_directory=state,
        source_commit="1" * 40,
        embedder=first_embedder,
        release_gate=False,
    )
    second_embedder = FakeEmbedder(query)
    resumed = await capture_score_artifact(
        gold_path=gold_path,
        generation_manifest_path=tmp_path / "manifest.json",
        generation_directory=fixture.database.parent,
        object_root=tmp_path / "objects",
        output_path=output,
        state_directory=state,
        source_commit="1" * 40,
        embedder=second_embedder,
        release_gate=False,
    )

    assert first.row_count == 4
    assert first.resumed_queries == 0
    assert resumed.resumed_queries == 1
    assert second_embedder.calls == []
    assert hashlib.sha256(output.read_bytes()).hexdigest() == first.artifact_sha256

    shard = state / "query-000.jsonl"
    records = [json.loads(line) for line in shard.read_bytes().splitlines()]
    records[1]["score"] = 0.123
    shard.write_bytes(b"".join(_canonical(record) + b"\n" for record in records))
    with pytest.raises(AggregationCaptureError, match="progress differs"):
        await capture_score_artifact(
            gold_path=gold_path,
            generation_manifest_path=tmp_path / "manifest.json",
            generation_directory=fixture.database.parent,
            object_root=tmp_path / "objects",
            output_path=output,
            state_directory=state,
            source_commit="1" * 40,
            embedder=FakeEmbedder(query),
            release_gate=False,
        )
