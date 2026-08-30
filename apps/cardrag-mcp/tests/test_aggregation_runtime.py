from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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


def test_compact_shape_preflight_accepts_twenty_million_scores_without_allocation() -> None:
    assert capture_module._predicted_shape(500, 40_000) == (
        20_000_000,
        80_000_000,
        8_192_000,
    )
    with pytest.raises(AggregationCaptureError, match="prediction exceeds"):
        capture_module._predicted_shape(500, 40_001)


@pytest.mark.asyncio
async def test_capture_cli_rejects_nonofficial_endpoint_before_reading_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_path = tmp_path / "key"
    key_path.write_text("must-not-be-read", encoding="utf-8")
    monkeypatch.setattr(
        capture_module,
        "_read_api_key_file",
        lambda _path: pytest.fail("hostile endpoint reached the secret-read boundary"),
    )
    arguments = SimpleNamespace(
        openrouter_base_url="https://attacker.invalid/api/v1",
        openrouter_api_key_file=key_path,
    )
    with pytest.raises(AggregationCaptureError, match="official API endpoint"):
        await capture_module._async_main(arguments)


def test_generation_checkpoint_rejects_atomic_same_byte_replacement(tmp_path: Path) -> None:
    artifact = tmp_path / "index.sqlite3"
    artifact.write_bytes(b"sealed-generation-bytes")
    checkpoint = capture_module._checkpoint_file(artifact, maximum_bytes=1024)
    displaced = tmp_path / "index.sqlite3.displaced"
    artifact.replace(displaced)
    artifact.write_bytes(displaced.read_bytes())

    with pytest.raises(AggregationCaptureError, match="identity changed"):
        capture_module._verify_checkpoint_identity(checkpoint)
    with pytest.raises(AggregationCaptureError, match="checkpoint changed"):
        capture_module._verify_checkpoint(checkpoint, maximum_bytes=1024)


def test_capture_hash_rejects_same_inode_metadata_change_before_open_without_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "capture-input.json"
    artifact.write_bytes(b"sealed-capture-input")
    inode = artifact.stat().st_ino
    real_open = capture_module.os.open
    mutated = False

    def open_after_chmod(path: Path, flags: int) -> int:
        nonlocal mutated
        if not mutated:
            artifact.chmod((artifact.stat().st_mode & 0o777) ^ 0o100)
            mutated = True
        return real_open(path, flags)

    monkeypatch.setattr(capture_module.os, "open", open_after_chmod)
    monkeypatch.setattr(
        capture_module.os,
        "read",
        lambda *_args: pytest.fail("capture reader consumed an input changed before open"),
    )

    with pytest.raises(AggregationCaptureError, match="changed during open"):
        capture_module._sha256_file(artifact, maximum_bytes=1024)

    assert mutated
    assert artifact.stat().st_ino == inode


def test_capture_regular_reader_does_not_reopen_path_between_validation_and_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "capture-input.json"
    replacement = tmp_path / "replacement.json"
    payload = b"sealed-capture-input"
    artifact.write_bytes(payload)
    replacement.write_bytes(payload)
    real_open = capture_module.os.open
    opens = 0

    def replace_before_second_open(path: Path, flags: int) -> int:
        nonlocal opens
        opens += 1
        if opens == 2:
            replacement.replace(artifact)
        return real_open(path, flags)

    monkeypatch.setattr(capture_module.os, "open", replace_before_second_open)

    assert capture_module._read_regular_bytes(artifact, maximum_bytes=1024) == payload
    assert opens == 1
    assert replacement.exists()


def test_capture_regular_reader_rejects_same_byte_path_replacement_while_pinned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "capture-input.json"
    replacement = tmp_path / "replacement.json"
    payload = b"sealed-capture-input"
    artifact.write_bytes(payload)
    replacement.write_bytes(payload)
    real_read = capture_module.os.read
    reads = 0

    def read_then_replace(descriptor: int, count: int) -> bytes:
        nonlocal reads
        block = real_read(descriptor, count)
        reads += 1
        if reads == 1:
            replacement.replace(artifact)
        return block

    monkeypatch.setattr(capture_module.os, "read", read_then_replace)

    with pytest.raises(AggregationCaptureError, match="changed during read"):
        capture_module._read_regular_bytes(artifact, maximum_bytes=1024)

    assert reads == 2


def test_capture_regular_reader_accepts_stable_hardlink(tmp_path: Path) -> None:
    source = tmp_path / "source.json"
    hardlink = tmp_path / "hardlink.json"
    payload = b"sealed-capture-input"
    source.write_bytes(payload)
    hardlink.hardlink_to(source)

    assert hardlink.stat().st_nlink == 2
    assert capture_module._read_regular_bytes(hardlink, maximum_bytes=1024) == payload
    assert capture_module._sha256_file(hardlink, maximum_bytes=1024) == (
        hashlib.sha256(payload).hexdigest(),
        len(payload),
    )


def _float32_binding(payload: bytes) -> capture_module.ArtifactBinding:
    return capture_module.ArtifactBinding(
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
    )


@pytest.mark.parametrize("mutation", ("grow", "metadata"))
def test_float32_validator_rejects_same_inode_change_before_open_without_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    shard = tmp_path / "scores.f32"
    payload = np.asarray([0.25, -0.5], dtype="<f4").tobytes()
    shard.write_bytes(payload)
    inode = shard.stat().st_ino
    real_open = capture_module.os.open
    mutated = False

    def open_after_mutation(path: Path, flags: int) -> int:
        nonlocal mutated
        if not mutated:
            if mutation == "grow":
                shard.write_bytes(payload + np.asarray([0.75], dtype="<f4").tobytes())
            else:
                shard.chmod((shard.stat().st_mode & 0o777) ^ 0o100)
            mutated = True
        return real_open(path, flags)

    monkeypatch.setattr(capture_module.os, "open", open_after_mutation)
    monkeypatch.setattr(
        capture_module.os,
        "read",
        lambda *_args: pytest.fail("validator consumed a shard that changed before open"),
    )

    with pytest.raises(AggregationCaptureError, match="changed during open"):
        capture_module._validate_float32_file(
            shard,
            expected=_float32_binding(payload),
            count=2,
            bounded_score=True,
            unit_norm=False,
        )

    assert mutated
    assert shard.stat().st_ino == inode


def test_float32_validator_bounds_growth_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shard = tmp_path / "scores.f32"
    payload = np.asarray([0.25, -0.5], dtype="<f4").tobytes()
    shard.write_bytes(payload)
    real_read = capture_module.os.read
    requested: list[int] = []

    def read_then_grow(descriptor: int, count: int) -> bytes:
        block = real_read(descriptor, count)
        requested.append(count)
        if len(requested) == 1:
            with shard.open("ab") as output:
                output.write(np.asarray([0.75], dtype="<f4").tobytes())
        return block

    monkeypatch.setattr(capture_module.os, "read", read_then_grow)

    with pytest.raises(AggregationCaptureError, match="changed during read"):
        capture_module._validate_float32_file(
            shard,
            expected=_float32_binding(payload),
            count=2,
            bounded_score=True,
            unit_norm=False,
        )

    assert requested == [len(payload), 1]


def test_float32_validator_rejects_path_replacement_after_bounded_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shard = tmp_path / "scores.f32"
    replacement = tmp_path / "replacement.f32"
    payload = np.asarray([0.25, -0.5], dtype="<f4").tobytes()
    shard.write_bytes(payload)
    replacement.write_bytes(payload)
    real_fstat = capture_module.os.fstat
    calls = 0

    def fstat_then_replace(descriptor: int) -> Any:
        nonlocal calls
        result = real_fstat(descriptor)
        calls += 1
        if calls == 2:
            replacement.replace(shard)
        return result

    monkeypatch.setattr(capture_module.os, "fstat", fstat_then_replace)

    with pytest.raises(AggregationCaptureError, match="binding is invalid"):
        capture_module._validate_float32_file(
            shard,
            expected=_float32_binding(payload),
            count=2,
            bounded_score=True,
            unit_norm=False,
        )

    assert calls == 2


def test_float32_validator_accepts_stable_hardlink(tmp_path: Path) -> None:
    source = tmp_path / "source.f32"
    hardlink = tmp_path / "hardlink.f32"
    payload = np.asarray([0.25, -0.5], dtype="<f4").tobytes()
    source.write_bytes(payload)
    hardlink.hardlink_to(source)

    assert hardlink.stat().st_nlink == 2
    capture_module._validate_float32_file(
        hardlink,
        expected=_float32_binding(payload),
        count=2,
        bounded_score=True,
        unit_norm=False,
    )


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
    assert captured.query_vector_f32 == query.astype("<f4").tobytes()


@pytest.mark.asyncio
async def test_exact_score_stream_is_ordered_byte_identical_bounded_and_single_call(
    tmp_path: Path,
) -> None:
    store = GenerationStore(tmp_path / "state", maximum_vector_bytes=2 * 1024 * 1024)
    _fixture, handle = install_v5_fixture(store)
    query = np.zeros((4096,), dtype=np.float32)
    query[0] = 1.0
    materialized_embedder = FakeEmbedder(query)
    materialized = await V5ExactRepository(
        store, materialized_embedder
    ).capture_unscoped_current_scores("혜택", handle=handle)
    streamed_embedder = FakeEmbedder(query)
    streamed_rows: list[Any] = []
    summary = await V5ExactRepository(
        store, streamed_embedder
    ).capture_unscoped_current_score_stream(
        "혜택",
        handle,
        score_sink=streamed_rows.append,
        block_rows=2,
    )

    assert tuple(streamed_rows) == materialized.rows
    assert summary.scored_rows == summary.expected_rows == 4
    assert summary.exact_blocks == 2
    assert summary.query_vector_f32 == materialized.query_vector_f32
    assert (
        np.asarray([row.score for row in streamed_rows], dtype="<f4").tobytes()
        == np.asarray([row.score for row in materialized.rows], dtype="<f4").tobytes()
    )
    assert len(materialized_embedder.calls) == len(streamed_embedder.calls) == 1

    for invalid in (False, 0, 4097):
        rejecting_embedder = FakeEmbedder(query)
        with pytest.raises(ValueError, match="block_rows"):
            await V5ExactRepository(
                store, rejecting_embedder
            ).capture_unscoped_current_score_stream(
                "혜택",
                handle,
                score_sink=lambda _row: None,
                block_rows=invalid,
            )
        assert rejecting_embedder.calls == []


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
        lambda _manifest, _directory: (
            SimpleNamespace(sha256="f" * 64),
            SimpleNamespace(sha256="a" * 64),
        ),
    )
    monkeypatch.setattr(capture_module, "_verify_checkpoint_identity", lambda _value: None)
    monkeypatch.setattr(
        capture_module,
        "_verify_checkpoint",
        lambda _value, *, maximum_bytes: None,
    )
    monkeypatch.setattr(capture_module, "load_generation_handle", lambda *_args, **_kwargs: handle)
    query = np.zeros((4096,), dtype=np.float32)
    query[0] = 1.0
    output = tmp_path / "scores.jsonl"
    inventory = tmp_path / "inventory.jsonl"
    score_matrix = tmp_path / "scores.f32"
    query_vectors = tmp_path / "query-vectors.f32"
    state = tmp_path / "capture-state"

    first_embedder = FakeEmbedder(query)
    first = await capture_score_artifact(
        gold_path=gold_path,
        generation_manifest_path=tmp_path / "manifest.json",
        generation_directory=fixture.database.parent,
        object_root=tmp_path / "objects",
        output_path=output,
        corpus_inventory_output_path=inventory,
        score_matrix_output_path=score_matrix,
        query_vector_matrix_output_path=query_vectors,
        state_directory=state,
        source_commit="1" * 40,
        embedder=first_embedder,
        release_gate=False,
    )
    # A crash can occur after the immutable query trio lands but before the
    # replaceable progress pointer is durable. The trio itself is authoritative.
    (state / "progress.json").unlink()
    second_embedder = FakeEmbedder(query)
    resumed = await capture_score_artifact(
        gold_path=gold_path,
        generation_manifest_path=tmp_path / "manifest.json",
        generation_directory=fixture.database.parent,
        object_root=tmp_path / "objects",
        output_path=output,
        corpus_inventory_output_path=inventory,
        score_matrix_output_path=score_matrix,
        query_vector_matrix_output_path=query_vectors,
        state_directory=state,
        source_commit="1" * 40,
        embedder=second_embedder,
        release_gate=False,
    )

    assert first.corpus_row_count == 4
    assert first.resumed_queries == 0
    assert resumed.resumed_queries == 1
    assert second_embedder.calls == []
    assert hashlib.sha256(output.read_bytes()).hexdigest() == first.artifact_sha256

    shard = state / "query-000.scores.f32"
    tampered = bytearray(shard.read_bytes())
    tampered[0] ^= 1
    shard.write_bytes(tampered)
    with pytest.raises(AggregationCaptureError, match="float32 capture shard binding is invalid"):
        await capture_score_artifact(
            gold_path=gold_path,
            generation_manifest_path=tmp_path / "manifest.json",
            generation_directory=fixture.database.parent,
            object_root=tmp_path / "objects",
            output_path=output,
            corpus_inventory_output_path=inventory,
            score_matrix_output_path=score_matrix,
            query_vector_matrix_output_path=query_vectors,
            state_directory=state,
            source_commit="1" * 40,
            embedder=FakeEmbedder(query),
            release_gate=False,
        )

    (state / "query-000.coverage.json").unlink()
    with pytest.raises(AggregationCaptureError, match="partial or orphaned"):
        await capture_score_artifact(
            gold_path=gold_path,
            generation_manifest_path=tmp_path / "manifest.json",
            generation_directory=fixture.database.parent,
            object_root=tmp_path / "objects",
            output_path=output,
            corpus_inventory_output_path=inventory,
            score_matrix_output_path=score_matrix,
            query_vector_matrix_output_path=query_vectors,
            state_directory=state,
            source_commit="1" * 40,
            embedder=FakeEmbedder(query),
            release_gate=False,
        )
