from __future__ import annotations

import hashlib
import io
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pytest
from pydantic import ValidationError

import cardrag_mcp.aggregation_profile as aggregation_module
from cardrag_mcp.aggregation_profile import (
    CONTRACT_PLUS_CHILD,
    MAX_CHILD,
    MAX_PORTABLE_ARTIFACT_BYTES,
    MAX_SCORE_COUNT,
    REQUIRED_AGGREGATION_SLICES,
    SCORE_ARTIFACT_SCHEMA_VERSION,
    TOP3_MEAN,
    AggregationProfileError,
    ArtifactBinding,
    ScoreArtifactManifest,
    build_aggregation_profile,
    open_score_artifact,
    validate_aggregation_profile,
)

SHA_A = "a" * 64
SOURCE_COMMIT = "1" * 40
EMBEDDING_PROFILE_ID = "cardrag.qwen3-embedding-8b.deepinfra.test"
ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True, slots=True)
class ScorePaths:
    scores: Path
    inventory: Path
    matrix: Path
    vectors: Path


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def _write_jsonl(path: Path, records: Sequence[dict[str, Any]]) -> str:
    payload = b"".join(_canonical(record) + b"\n" for record in records)
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def _binding(payload: bytes) -> dict[str, object]:
    return {"sha256": hashlib.sha256(payload).hexdigest(), "size_bytes": len(payload)}


@pytest.mark.parametrize("reader_kind", ("whole", "jsonl", "mapped"))
def test_profile_readers_reject_same_inode_growth_between_lstat_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reader_kind: str,
) -> None:
    target = tmp_path / "race.jsonl"
    payload = _canonical({}) + b"\n"
    target.write_bytes(payload)
    original_inode = target.stat().st_ino
    original_open = aggregation_module.os.open
    read_calls = 0
    fdopen_calls = 0
    raced = False

    def racing_open(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal raced
        if not raced and dir_fd is None and Path(os.fspath(path)) == target:
            raced = True
            with target.open("ab") as stream:
                stream.write(payload)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    def forbidden_read(descriptor: int, count: int) -> bytes:
        nonlocal read_calls
        read_calls += 1
        raise AssertionError("read must not run after the opened-size preflight fails")

    def forbidden_fdopen(*args: object, **kwargs: object) -> object:
        nonlocal fdopen_calls
        fdopen_calls += 1
        raise AssertionError("fdopen must not run after the opened identity check fails")

    monkeypatch.setattr(aggregation_module.os, "open", racing_open)
    if reader_kind == "whole":
        monkeypatch.setattr(aggregation_module.os, "read", forbidden_read)
        with pytest.raises(AggregationProfileError, match="fixture_size_invalid"):
            aggregation_module._read_regular(
                target,
                maximum_bytes=len(payload),
                error_prefix="fixture",
            )
    elif reader_kind == "jsonl":
        monkeypatch.setattr(aggregation_module.os, "fdopen", forbidden_fdopen)
        with pytest.raises(AggregationProfileError, match="fixture_changed_during_read"):
            with aggregation_module._ScoreLineReader(target, code="fixture"):
                pass
    else:
        binding = ArtifactBinding(
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
        )
        with pytest.raises(AggregationProfileError, match="fixture_changed_during_open"):
            with aggregation_module._MappedArtifact(target, binding, code="fixture"):
                pass

    assert raced
    assert target.stat().st_ino == original_inode
    assert read_calls == 0
    assert fdopen_calls == 0


@pytest.mark.skipif(not hasattr(os, "O_NONBLOCK"), reason="requires POSIX nonblocking open")
@pytest.mark.parametrize(
    ("reader_kind", "expected_error"),
    (
        ("whole", "fixture_not_regular"),
        ("jsonl", "fixture_not_regular"),
        ("mapped", "fixture_changed_during_open"),
    ),
)
def test_profile_readers_nonblocking_open_rejects_fifo_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reader_kind: str,
    expected_error: str,
) -> None:
    target = tmp_path / "artifact.bin"
    payload = b"bounded artifact"
    target.write_bytes(payload)
    binding = ArtifactBinding(
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
    )
    original_open = aggregation_module.os.open
    raced = False
    observed_flags = 0

    def racing_open(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal observed_flags, raced
        if not raced and dir_fd is None and Path(os.fspath(path)) == target:
            raced = True
            observed_flags = flags
            target.unlink()
            os.mkfifo(target)
            assert flags & os.O_NONBLOCK
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(aggregation_module.os, "open", racing_open)

    with pytest.raises(AggregationProfileError, match=expected_error):
        if reader_kind == "whole":
            aggregation_module._read_regular(
                target,
                maximum_bytes=len(payload),
                error_prefix="fixture",
            )
        elif reader_kind == "jsonl":
            with aggregation_module._ScoreLineReader(target, code="fixture"):
                raise AssertionError("FIFO must be rejected during context entry")
        else:
            with aggregation_module._MappedArtifact(target, binding, code="fixture"):
                raise AssertionError("FIFO must be rejected during context entry")

    assert raced
    assert observed_flags & os.O_NONBLOCK


@pytest.mark.parametrize("failure_point", ("first_fstat", "second_fstat", "pread", "mmap"))
def test_mapped_artifact_closes_descriptor_when_enter_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    target = tmp_path / "mapped.bin"
    payload = b"bounded mapped artifact"
    target.write_bytes(payload)
    binding = ArtifactBinding(
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
    )
    artifact = aggregation_module._MappedArtifact(target, binding, code="fixture")
    original_open = aggregation_module.os.open
    original_fstat = aggregation_module.os.fstat
    opened_descriptors: list[int] = []
    fstat_calls = 0

    def tracking_open(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        opened_descriptors.append(descriptor)
        return descriptor

    def failing_fstat(descriptor: int) -> os.stat_result:
        nonlocal fstat_calls
        fstat_calls += 1
        expected_call = 1 if failure_point == "first_fstat" else 2
        if fstat_calls == expected_call:
            raise OSError(f"synthetic {failure_point} failure")
        return original_fstat(descriptor)

    with monkeypatch.context() as context:
        context.setattr(aggregation_module.os, "open", tracking_open)
        if failure_point in {"first_fstat", "second_fstat"}:
            context.setattr(aggregation_module.os, "fstat", failing_fstat)
        elif failure_point == "pread":
            context.setattr(
                aggregation_module.os,
                "pread",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("synthetic pread failure")),
            )
        else:
            context.setattr(
                aggregation_module.mmap,
                "mmap",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("synthetic mmap failure")),
            )
        with pytest.raises(OSError, match=f"synthetic {failure_point} failure"):
            artifact.__enter__()

    assert len(opened_descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(opened_descriptors[0])
    assert artifact._descriptor is None
    assert artifact._mapping is None
    assert artifact._listed is None
    assert artifact._before is None


def test_score_line_reader_enforces_running_artifact_cap(tmp_path: Path) -> None:
    target = tmp_path / "bounded.jsonl"
    payload = _canonical({}) + b"\n"
    target.write_bytes(payload)

    original_limit = aggregation_module.MAX_SCORE_ARTIFACT_BYTES
    aggregation_module.MAX_SCORE_ARTIFACT_BYTES = len(payload)
    try:
        with aggregation_module._ScoreLineReader(target, code="fixture") as reader:
            assert reader.next_record() == {}
            original_stream = reader._stream
            injected = io.BytesIO(payload)
            reader._stream = injected
            with pytest.raises(AggregationProfileError, match="fixture_size_invalid"):
                reader.next_record()
            injected.close()
            reader._stream = original_stream
            assert reader.next_record() is None
    finally:
        aggregation_module.MAX_SCORE_ARTIFACT_BYTES = original_limit


def test_profile_readers_accept_stable_two_link_snapshot(tmp_path: Path) -> None:
    target = tmp_path / "artifact.jsonl"
    linked = tmp_path / "artifact-hardlink.jsonl"
    payload = _canonical({}) + b"\n"
    target.write_bytes(payload)
    os.link(target, linked)
    assert target.stat().st_nlink == linked.stat().st_nlink == 2

    assert (
        aggregation_module._read_regular(
            linked,
            maximum_bytes=len(payload),
            error_prefix="fixture",
        )
        == payload
    )
    with aggregation_module._ScoreLineReader(linked, code="fixture") as reader:
        assert reader.next_record() == {}
        assert reader.next_record() is None
    binding = ArtifactBinding(
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
    )
    with aggregation_module._MappedArtifact(linked, binding, code="fixture"):
        pass


@pytest.mark.parametrize("reader_kind", ("jsonl", "mapped"))
def test_profile_context_readers_check_current_path_when_body_raises(
    tmp_path: Path,
    reader_kind: str,
) -> None:
    target = tmp_path / "artifact.jsonl"
    replacement = tmp_path / "replacement.jsonl"
    payload = _canonical({}) + b"\n"
    target.write_bytes(payload)
    replacement.write_bytes(payload)
    binding = ArtifactBinding(
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
    )

    with pytest.raises(AggregationProfileError, match="fixture_changed_during_read"):
        if reader_kind == "jsonl":
            with aggregation_module._ScoreLineReader(target, code="fixture"):
                replacement.replace(target)
                raise RuntimeError("consumer failed after replacing the pathname")
        else:
            with aggregation_module._MappedArtifact(target, binding, code="fixture"):
                replacement.replace(target)
                raise RuntimeError("consumer failed after replacing the pathname")


def _score_paths(root: Path) -> ScorePaths:
    return ScorePaths(
        scores=root / "scores.jsonl",
        inventory=root / "inventory.jsonl",
        matrix=root / "scores.f32",
        vectors=root / "query-vectors.f32",
    )


def _gold_query(index: int, slices: Sequence[str], *, no_answer: bool = False) -> dict[str, Any]:
    query_id = f"query-{index:03d}"
    if no_answer:
        return {
            "condition_groups": [],
            "contracts": [],
            "expected_numeric_facts": [],
            "expected_revision_ids": [],
            "high_risk": False,
            "no_answer": True,
            "query_id": query_id,
            "question": f"정답 없음 질문 {index}",
            "schema_version": "cardrag.gold-query.v1",
            "slices": sorted(set(slices).union({"no_answer"})),
            "spans": [],
        }
    relevant = "relevant"
    first_span = {
        "contract_revision_id": relevant,
        "page": 1,
        "relevance": 3,
        "roles": ["benefit"],
        "source_end": 10,
        "source_start": 0,
        "span_id": f"benefit-{index:03d}",
        "text_sha256": SHA_A,
    }
    spans = [first_span]
    condition_groups: list[dict[str, Any]] = []
    expected_numeric_facts: list[str] = []
    expected_revision_ids: list[str] = []
    high_risk = index == 1
    if high_risk:
        condition_span = {
            "contract_revision_id": relevant,
            "page": 1,
            "relevance": 3,
            "roles": ["condition", "numeric", "revision"],
            "source_end": 20,
            "source_start": 10,
            "span_id": f"condition-{index:03d}",
            "text_sha256": SHA_A,
        }
        spans.append(condition_span)
        condition_groups.append(
            {"at_k": 10, "span_ids": [first_span["span_id"], condition_span["span_id"]]}
        )
        expected_numeric_facts.append("월 10,000원")
        expected_revision_ids.append(relevant)
    return {
        "condition_groups": condition_groups,
        "contracts": [{"contract_revision_id": relevant, "relevance": 3}],
        "expected_numeric_facts": expected_numeric_facts,
        "expected_revision_ids": expected_revision_ids,
        "high_risk": high_risk,
        "no_answer": False,
        "query_id": query_id,
        "question": f"카드 혜택 질문 {index}",
        "schema_version": "cardrag.gold-query.v1",
        "slices": sorted(set(slices)),
        "spans": spans,
    }


def _write_gold(path: Path, *, count: int, release: bool) -> str:
    required = sorted(REQUIRED_AGGREGATION_SLICES)
    records: list[dict[str, Any]] = []
    for index in range(count):
        if release and index == 0:
            records.append(_gold_query(index, ["no_answer"], no_answer=True))
        else:
            slices = [required[(index - 1) % len(required)]] if release else ["benefit"]
            records.append(_gold_query(index, slices))
    return _write_jsonl(path, records)


def _inventory_rows() -> tuple[list[dict[str, Any]], np.ndarray[Any, Any]]:
    contracts = (
        ("relevant", 1.0, (0.5, 0.5, 0.5)),
        ("distractor-a", -1.0, (0.9, 0.9, 0.9)),
        ("distractor-b", -0.9, (0.8, 0.8, 0.8)),
    )
    rows: list[dict[str, Any]] = []
    scores: list[float] = []
    for contract_id, contract_score, children in contracts:
        rows.append(
            {
                "contract_revision_id": contract_id,
                "embedding_profile_id": EMBEDDING_PROFILE_ID,
                "input_sha256": SHA_A,
                "node_id": f"{contract_id}:contract",
                "ordinal": len(rows),
                "row_index": len(rows),
                "schema_version": "cardrag.document-aggregation-corpus-row.v1",
                "view_type": "CONTRACT",
            }
        )
        scores.append(contract_score)
        for child_index, child_score in enumerate(children):
            rows.append(
                {
                    "contract_revision_id": contract_id,
                    "embedding_profile_id": EMBEDDING_PROFILE_ID,
                    "input_sha256": SHA_A,
                    "node_id": f"{contract_id}:child:{child_index}",
                    "ordinal": len(rows),
                    "row_index": len(rows),
                    "schema_version": "cardrag.document-aggregation-corpus-row.v1",
                    "view_type": "RAW_ITEM",
                }
            )
            scores.append(child_score)
    return rows, np.asarray(scores, dtype="<f4")


def _write_scores(
    paths: ScorePaths,
    gold_path: Path,
    *,
    count: int,
    generation_manifest_sha256: str,
    validation_profile: Literal["release_grade", "fixture_only"] = "fixture_only",
) -> list[dict[str, Any]]:
    inventory_rows, one_query_scores = _inventory_rows()
    inventory_manifest = {
        "corpus_row_count": len(inventory_rows),
        "embedding_profile_id": EMBEDDING_PROFILE_ID,
        "exact_row_corpus_sha256": "d" * 64,
        "generation_id": "generation-v110-test",
        "schema_version": "cardrag.document-aggregation-corpus-inventory.v1",
        "serving_database_sha256": "b" * 64,
        "vector_sidecar_sha256": "c" * 64,
    }
    inventory_payload = b"".join(
        _canonical(record) + b"\n" for record in [inventory_manifest, *inventory_rows]
    )
    paths.inventory.write_bytes(inventory_payload)
    score_payload = np.tile(one_query_scores, count).astype("<f4", copy=False).tobytes()
    paths.matrix.write_bytes(score_payload)
    vectors: list[bytes] = []
    for index in range(count):
        vector = np.zeros((4096,), dtype="<f4")
        vector[index % 4096] = 1.0
        vectors.append(vector.tobytes())
    vector_payload = b"".join(vectors)
    paths.vectors.write_bytes(vector_payload)
    records: list[dict[str, Any]] = [
        {
            "approximate": False,
            "byte_order": "little-endian",
            "corpus_inventory": _binding(inventory_payload),
            "corpus_row_count": len(inventory_rows),
            "embedding_dimension": 4096,
            "embedding_model": "qwen/qwen3-embedding-8b",
            "embedding_profile_id": EMBEDDING_PROFILE_ID,
            "exact": True,
            "exact_row_corpus_sha256": "d" * 64,
            "generation_id": "generation-v110-test",
            "generation_manifest_sha256": generation_manifest_sha256,
            "gold_sha256": hashlib.sha256(gold_path.read_bytes()).hexdigest(),
            "matrix_order": "row-major",
            "query_count": count,
            "query_vector_matrix": _binding(vector_payload),
            "runtime_document_aggregation_policy": "max_child",
            "runtime_document_aggregation_status": "candidate_default",
            "scalar_type": "float32",
            "schema_version": SCORE_ARTIFACT_SCHEMA_VERSION,
            "score_count": count * len(inventory_rows),
            "score_matrix": _binding(score_payload),
            "scoring_contract": "cardrag.v5-exact-row-score.v1",
            "serving_database_sha256": "b" * 64,
            "source_commit": SOURCE_COMMIT,
            "temporal_scope_policy": "gold-query.v1",
            "validation_profile": validation_profile,
            "vector_sidecar_sha256": "c" * 64,
        }
    ]
    for index in range(count):
        question = (
            f"정답 없음 질문 {index}" if index == 0 and count >= 300 else f"카드 혜택 질문 {index}"
        )
        segment_start = index * len(inventory_rows) * 4
        segment_end = (index + 1) * len(inventory_rows) * 4
        score_segment = score_payload[segment_start:segment_end]
        records.append(
            {
                "active_contracts": 3,
                "expected_rows": len(inventory_rows),
                "ordinal": index,
                "query_id": f"query-{index:03d}",
                "query_sha256": hashlib.sha256(question.encode()).hexdigest(),
                "query_vector_count": 4096,
                "query_vector_offset_bytes": index * 4096 * 4,
                "query_vector_sha256": hashlib.sha256(vectors[index]).hexdigest(),
                "query_vector_size_bytes": 4096 * 4,
                "schema_version": "cardrag.document-aggregation-query-coverage.v2",
                "score_count": len(inventory_rows),
                "score_offset_bytes": index * len(inventory_rows) * 4,
                "score_sha256": hashlib.sha256(score_segment).hexdigest(),
                "score_size_bytes": len(inventory_rows) * 4,
                "scored_rows": len(inventory_rows),
            }
        )
    _write_jsonl(paths.scores, records)
    return records


def _profile(paths: ScorePaths, gold: Path, **kwargs: Any) -> Any:
    return build_aggregation_profile(
        gold,
        paths.scores,
        paths.inventory,
        paths.matrix,
        paths.vectors,
        **kwargs,
    )


def _generation_manifest(directory: Path) -> str:
    directory.mkdir()
    payload = b'{"schema_version":"cardrag.generation.v5-test"}\n'
    digest = hashlib.sha256(payload).hexdigest()
    (directory / f"{digest}.json").write_bytes(payload)
    return digest


def test_three_aggregation_policies_are_deterministic_and_fixture_is_not_release_ready(
    tmp_path: Path,
) -> None:
    gold = tmp_path / "gold.jsonl"
    paths = _score_paths(tmp_path)
    _write_gold(gold, count=3, release=False)
    _write_scores(
        paths,
        gold,
        count=3,
        generation_manifest_sha256=hashlib.sha256(b"generation").hexdigest(),
    )

    first = _profile(paths, gold, release_gate=False, bootstrap_samples=100, bootstrap_seed=7)
    second = _profile(paths, gold, release_gate=False, bootstrap_samples=100, bootstrap_seed=7)

    assert first.canonical_bytes == second.canonical_bytes
    assert set(first.payload["definitions"]) == {MAX_CHILD, TOP3_MEAN, CONTRACT_PLUS_CHILD}
    assert first.payload["selection"]["winner"] == CONTRACT_PLUS_CHILD
    assert first.payload["release_gate"]["status"] == "not_evaluated"
    assert first.payload["sealed_profile"] is None
    assert (
        first.payload["policies"][CONTRACT_PLUS_CHILD]["overall"]["ndcg_at_10"]["value"]
        > first.payload["policies"][MAX_CHILD]["overall"]["ndcg_at_10"]["value"]
    )


def test_release_profile_is_hash_bound_recomputed_and_passes_only_with_release_evidence(
    tmp_path: Path,
) -> None:
    gold = tmp_path / "gold.jsonl"
    paths = _score_paths(tmp_path)
    profile_path = tmp_path / "profile.json"
    manifests = tmp_path / "generation-manifests"
    generation_sha256 = _generation_manifest(manifests)
    gold_sha256 = _write_gold(gold, count=300, release=True)
    _write_scores(
        paths,
        gold,
        count=300,
        generation_manifest_sha256=generation_sha256,
        validation_profile="release_grade",
    )

    artifact = _profile(
        paths,
        gold,
        expected_gold_sha256=gold_sha256,
        expected_source_commit=SOURCE_COMMIT,
        bootstrap_samples=2_000,
        bootstrap_seed=1010,
    )
    profile_bytes = artifact.canonical_bytes + b"\n"
    profile_path.write_bytes(profile_bytes)
    validated = validate_aggregation_profile(
        profile_path,
        gold,
        paths.scores,
        paths.inventory,
        paths.matrix,
        paths.vectors,
        manifests,
        expected_profile_sha256=hashlib.sha256(profile_bytes).hexdigest(),
        expected_source_commit=SOURCE_COMMIT,
        bootstrap_samples=2_000,
        bootstrap_seed=1010,
    )

    assert artifact.payload["release_gate"]["status"] == "passed"
    assert artifact.payload["selection"]["winner"] == CONTRACT_PLUS_CHILD
    assert validated.canonical_bytes == artifact.canonical_bytes


def test_fixture_profile_can_never_be_a_release_trust_root(tmp_path: Path) -> None:
    gold = tmp_path / "gold.jsonl"
    paths = _score_paths(tmp_path)
    gold_sha = _write_gold(gold, count=300, release=True)
    _write_scores(
        paths,
        gold,
        count=300,
        generation_manifest_sha256="e" * 64,
        validation_profile="fixture_only",
    )
    with pytest.raises(AggregationProfileError, match="score_validation_profile_not_release_grade"):
        _profile(
            paths,
            gold,
            expected_gold_sha256=gold_sha,
            expected_source_commit=SOURCE_COMMIT,
            bootstrap_samples=2_000,
        )


def test_legacy_v1_score_artifact_is_rejected_even_in_fixture_mode(tmp_path: Path) -> None:
    gold = tmp_path / "gold.jsonl"
    paths = _score_paths(tmp_path)
    _write_gold(gold, count=1, release=False)
    records = _write_scores(paths, gold, count=1, generation_manifest_sha256="e" * 64)
    records[0]["schema_version"] = "cardrag.document-aggregation-score-artifact.v1"
    _write_jsonl(paths.scores, records)
    with pytest.raises(AggregationProfileError, match="score_manifest_invalid"):
        _profile(paths, gold, release_gate=False, bootstrap_samples=100)


def test_reader_preserves_one_ulp_and_rejects_truncation_trailing_and_segment_swap(
    tmp_path: Path,
) -> None:
    gold = tmp_path / "gold.jsonl"
    paths = _score_paths(tmp_path)
    _write_gold(gold, count=2, release=False)
    records = _write_scores(paths, gold, count=2, generation_manifest_sha256="e" * 64)
    values = np.frombuffer(paths.matrix.read_bytes(), dtype="<f4").copy()
    values[0] = np.nextafter(values[0], np.float32(0.0), dtype=np.float32)
    payload = values.astype("<f4", copy=False).tobytes()
    paths.matrix.write_bytes(payload)
    records[0]["score_matrix"] = _binding(payload)
    segment_size = int(records[1]["score_size_bytes"])
    records[1]["score_sha256"] = hashlib.sha256(payload[:segment_size]).hexdigest()
    _write_jsonl(paths.scores, records)
    with open_score_artifact(paths.scores, paths.inventory, paths.matrix, paths.vectors) as opened:
        actual = opened.scores_for(0)
        assert actual[0].tobytes() == values[0].tobytes()
        del actual

    paths.matrix.write_bytes(payload[:-1])
    with pytest.raises(AggregationProfileError, match="score_matrix_size_mismatch"):
        with open_score_artifact(paths.scores, paths.inventory, paths.matrix, paths.vectors):
            pass
    paths.matrix.write_bytes(payload + b"\0")
    with pytest.raises(AggregationProfileError, match="score_matrix_size_mismatch"):
        with open_score_artifact(paths.scores, paths.inventory, paths.matrix, paths.vectors):
            pass

    swapped = payload[segment_size:] + payload[:segment_size]
    paths.matrix.write_bytes(swapped)
    records[0]["score_matrix"] = _binding(swapped)
    _write_jsonl(paths.scores, records)
    with pytest.raises(AggregationProfileError, match="score_matrix_segment_sha256_mismatch"):
        with open_score_artifact(paths.scores, paths.inventory, paths.matrix, paths.vectors):
            pass


def test_inventory_and_query_vector_tamper_fail_closed(tmp_path: Path) -> None:
    gold = tmp_path / "gold.jsonl"
    paths = _score_paths(tmp_path)
    _write_gold(gold, count=1, release=False)
    records = _write_scores(paths, gold, count=1, generation_manifest_sha256="e" * 64)
    inventory = paths.inventory.read_bytes().replace(b'"row_index":0', b'"row_index":9', 1)
    paths.inventory.write_bytes(inventory)
    with pytest.raises(AggregationProfileError, match="corpus_inventory_row_order_invalid"):
        with open_score_artifact(paths.scores, paths.inventory, paths.matrix, paths.vectors):
            pass

    _write_scores(paths, gold, count=1, generation_manifest_sha256="e" * 64)
    bad_vector = np.zeros((4096,), dtype="<f4").tobytes()
    paths.vectors.write_bytes(bad_vector)
    records[0]["query_vector_matrix"] = _binding(bad_vector)
    records[1]["query_vector_sha256"] = hashlib.sha256(bad_vector).hexdigest()
    _write_jsonl(paths.scores, records)
    with pytest.raises(AggregationProfileError, match="query_vector_norm_invalid"):
        with open_score_artifact(paths.scores, paths.inventory, paths.matrix, paths.vectors):
            pass


@pytest.mark.parametrize("invalid", [np.float32(np.nan), np.float32(1.0000001)])
def test_score_matrix_rejects_non_finite_and_out_of_range_float32(
    tmp_path: Path,
    invalid: np.float32,
) -> None:
    gold = tmp_path / "gold.jsonl"
    paths = _score_paths(tmp_path)
    _write_gold(gold, count=1, release=False)
    records = _write_scores(paths, gold, count=1, generation_manifest_sha256="e" * 64)
    scores = np.frombuffer(paths.matrix.read_bytes(), dtype="<f4").copy()
    scores[0] = invalid
    payload = scores.astype("<f4", copy=False).tobytes()
    paths.matrix.write_bytes(payload)
    records[0]["score_matrix"] = _binding(payload)
    records[1]["score_sha256"] = hashlib.sha256(payload).hexdigest()
    _write_jsonl(paths.scores, records)
    with pytest.raises(AggregationProfileError, match="score_matrix_value_invalid"):
        with open_score_artifact(paths.scores, paths.inventory, paths.matrix, paths.vectors):
            pass


def test_schema_caps_reject_oversized_shape_without_allocating() -> None:
    base: dict[str, Any] = {
        "approximate": False,
        "byte_order": "little-endian",
        "corpus_inventory": {"sha256": "a" * 64, "size_bytes": 1},
        "corpus_row_count": MAX_SCORE_COUNT + 1,
        "embedding_dimension": 4096,
        "embedding_model": "qwen/qwen3-embedding-8b",
        "embedding_profile_id": EMBEDDING_PROFILE_ID,
        "exact": True,
        "exact_row_corpus_sha256": "d" * 64,
        "generation_id": "generation-v110-test",
        "generation_manifest_sha256": "e" * 64,
        "gold_sha256": "f" * 64,
        "matrix_order": "row-major",
        "query_count": 1,
        "query_vector_matrix": {"sha256": "1" * 64, "size_bytes": 4096 * 4},
        "runtime_document_aggregation_policy": "max_child",
        "runtime_document_aggregation_status": "candidate_default",
        "scalar_type": "float32",
        "schema_version": SCORE_ARTIFACT_SCHEMA_VERSION,
        "score_count": MAX_SCORE_COUNT + 1,
        "score_matrix": {
            "sha256": "2" * 64,
            "size_bytes": MAX_PORTABLE_ARTIFACT_BYTES,
        },
        "scoring_contract": "cardrag.v5-exact-row-score.v1",
        "serving_database_sha256": "b" * 64,
        "source_commit": SOURCE_COMMIT,
        "temporal_scope_policy": "gold-query.v1",
        "validation_profile": "fixture_only",
        "vector_sidecar_sha256": "c" * 64,
    }
    with pytest.raises(ValidationError):
        ScoreArtifactManifest.model_validate(base)
    with pytest.raises(ValidationError):
        ArtifactBinding(sha256="a" * 64, size_bytes=MAX_PORTABLE_ARTIFACT_BYTES + 1)


def test_artifact_is_canonical_source_bound_and_symlink_safe(tmp_path: Path) -> None:
    gold = tmp_path / "gold.jsonl"
    paths = _score_paths(tmp_path)
    _write_gold(gold, count=1, release=False)
    _write_scores(paths, gold, count=1, generation_manifest_sha256="e" * 64)
    with pytest.raises(AggregationProfileError, match="candidate_source_commit_mismatch"):
        _profile(
            paths,
            gold,
            release_gate=False,
            expected_source_commit="2" * 40,
            bootstrap_samples=100,
        )
    link = tmp_path / "scores-link.jsonl"
    link.symlink_to(paths.scores)
    with pytest.raises(AggregationProfileError, match="score_artifact_not_regular"):
        with open_score_artifact(link, paths.inventory, paths.matrix, paths.vectors):
            pass
    lines = paths.scores.read_bytes().splitlines(keepends=True)
    lines[1] = lines[1].replace(b'"query_id":', b'"query_id" :')
    paths.scores.write_bytes(b"".join(lines))
    with pytest.raises(AggregationProfileError, match="score_not_canonical_bytes"):
        with open_score_artifact(paths.scores, paths.inventory, paths.matrix, paths.vectors):
            pass


@pytest.mark.parametrize(
    ("target_name", "error"),
    [
        ("scores", "score_artifact_changed_during_read"),
        ("inventory", "corpus_inventory_changed_during_read"),
    ],
)
def test_json_evidence_path_replacement_is_detected_at_context_exit(
    tmp_path: Path,
    target_name: str,
    error: str,
) -> None:
    gold = tmp_path / "gold.jsonl"
    paths = _score_paths(tmp_path)
    _write_gold(gold, count=1, release=False)
    _write_scores(paths, gold, count=1, generation_manifest_sha256="e" * 64)
    target = getattr(paths, target_name)
    original = target.read_bytes()
    displaced = tmp_path / f"{target.name}.displaced"
    with pytest.raises(AggregationProfileError, match=error):
        with open_score_artifact(paths.scores, paths.inventory, paths.matrix, paths.vectors):
            target.replace(displaced)
            target.write_bytes(original)


def test_release_workflow_names_all_compact_profile_inputs() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    for contract in (
        "aggregation_profile_sha256:",
        'aggregation_profile="$evidence_dir/document-aggregation-profile.json"',
        'aggregation_scores="$evidence_dir/document-aggregation-scores.jsonl"',
        ".venv/bin/python -m cardrag_mcp.aggregation_profile",
        '--scores "$aggregation_scores"',
        '--validate-profile "$aggregation_profile"',
        "--bootstrap-samples 2000",
        "--bootstrap-seed 1010",
    ):
        assert contract in workflow
