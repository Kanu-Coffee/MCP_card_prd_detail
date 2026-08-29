from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from cardrag_mcp.aggregation_profile import (
    CONTRACT_PLUS_CHILD,
    MAX_CHILD,
    REQUIRED_AGGREGATION_SLICES,
    SCORE_ARTIFACT_SCHEMA_VERSION,
    TOP3_MEAN,
    AggregationProfileError,
    build_aggregation_profile,
    validate_aggregation_profile,
)

SHA_A = "a" * 64
SOURCE_COMMIT = "1" * 40
EMBEDDING_PROFILE_ID = "cardrag.qwen3-embedding-8b.deepinfra.test"
ROOT = Path(__file__).resolve().parents[3]


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
    relevant = f"relevant-{index:03d}"
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
            {
                "at_k": 10,
                "span_ids": [first_span["span_id"], condition_span["span_id"]],
            }
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
            continue
        slices = [required[(index - 1) % len(required)]] if release else ["benefit"]
        records.append(_gold_query(index, slices))
    return _write_jsonl(path, records)


def _score_rows(query_id: str, index: int, *, first_ordinal: int = 0) -> list[dict[str, Any]]:
    contracts = (
        (f"relevant-{index:03d}", 1.0, (0.5, 0.5, 0.5)),
        (f"distractor-a-{index:03d}", -1.0, (0.9, 0.9, 0.9)),
        (f"distractor-b-{index:03d}", -0.9, (0.8, 0.8, 0.8)),
    )
    records: list[dict[str, Any]] = []
    ordinal = first_ordinal
    row_index = 0
    for contract_id, contract_score, children in contracts:
        records.append(
            {
                "contract_revision_id": contract_id,
                "embedding_profile_id": EMBEDDING_PROFILE_ID,
                "input_sha256": SHA_A,
                "node_id": f"{contract_id}:contract",
                "ordinal": ordinal,
                "query_id": query_id,
                "row_index": row_index,
                "schema_version": "cardrag.document-aggregation-row-score.v1",
                "score": contract_score,
                "view_type": "CONTRACT",
            }
        )
        ordinal += 1
        row_index += 1
        for child_index, child_score in enumerate(children):
            records.append(
                {
                    "contract_revision_id": contract_id,
                    "embedding_profile_id": EMBEDDING_PROFILE_ID,
                    "input_sha256": SHA_A,
                    "node_id": f"{contract_id}:child:{child_index}",
                    "ordinal": ordinal,
                    "query_id": query_id,
                    "row_index": row_index,
                    "schema_version": "cardrag.document-aggregation-row-score.v1",
                    "score": child_score,
                    "view_type": "RAW_ITEM",
                }
            )
            ordinal += 1
            row_index += 1
    return records


def _write_scores(
    path: Path,
    gold_path: Path,
    *,
    count: int,
    generation_manifest_sha256: str,
) -> list[dict[str, Any]]:
    gold_sha256 = hashlib.sha256(gold_path.read_bytes()).hexdigest()
    records: list[dict[str, Any]] = [
        {
            "approximate": False,
            "embedding_dimension": 4096,
            "embedding_model": "qwen/qwen3-embedding-8b",
            "embedding_profile_id": EMBEDDING_PROFILE_ID,
            "exact": True,
            "generation_id": "generation-v110-test",
            "generation_manifest_sha256": generation_manifest_sha256,
            "gold_sha256": gold_sha256,
            "serving_database_sha256": "b" * 64,
            "vector_sidecar_sha256": "c" * 64,
            "exact_row_corpus_sha256": "d" * 64,
            "query_count": count,
            "row_count": count * 12,
            "runtime_document_aggregation_policy": "max_child",
            "runtime_document_aggregation_status": "candidate_default",
            "schema_version": SCORE_ARTIFACT_SCHEMA_VERSION,
            "scoring_contract": "cardrag.v5-exact-row-score.v1",
            "source_commit": SOURCE_COMMIT,
            "temporal_scope_policy": "gold-query.v1",
        }
    ]
    for index in range(count):
        query_id = f"query-{index:03d}"
        question = (
            f"정답 없음 질문 {index}" if index == 0 and count >= 300 else f"카드 혜택 질문 {index}"
        )
        records.append(
            {
                "active_contracts": 3,
                "expected_rows": 12,
                "query_id": query_id,
                "query_sha256": hashlib.sha256(question.encode()).hexdigest(),
                "query_vector_sha256": hashlib.sha256(f"query-vector-{index}".encode()).hexdigest(),
                "schema_version": "cardrag.document-aggregation-query-coverage.v1",
                "scored_rows": 12,
            }
        )
        records.extend(_score_rows(query_id, index))
    _write_jsonl(path, records)
    return records


def _generation_manifest(directory: Path) -> str:
    directory.mkdir()
    payload = b'{"schema_version":"cardrag.generation.v5-test"}\n'
    digest = hashlib.sha256(payload).hexdigest()
    (directory / f"{digest}.json").write_bytes(payload)
    return digest


def test_three_aggregation_policies_are_deterministic_and_fixture_is_not_release_ready(
    tmp_path: Path,
) -> None:
    gold_path = tmp_path / "gold.jsonl"
    scores_path = tmp_path / "scores.jsonl"
    _write_gold(gold_path, count=3, release=False)
    generation_sha256 = hashlib.sha256(b"generation").hexdigest()
    _write_scores(
        scores_path,
        gold_path,
        count=3,
        generation_manifest_sha256=generation_sha256,
    )

    first = build_aggregation_profile(
        gold_path,
        scores_path,
        release_gate=False,
        bootstrap_samples=100,
        bootstrap_seed=7,
    )
    second = build_aggregation_profile(
        gold_path,
        scores_path,
        release_gate=False,
        bootstrap_samples=100,
        bootstrap_seed=7,
    )

    assert first.canonical_bytes == second.canonical_bytes
    assert set(first.payload["definitions"]) == {MAX_CHILD, TOP3_MEAN, CONTRACT_PLUS_CHILD}
    assert first.payload["selection"]["winner"] == CONTRACT_PLUS_CHILD
    assert first.payload["release_gate"] == {
        "evaluated": False,
        "failure_reasons": [],
        "status": "not_evaluated",
    }
    assert first.payload["sealed_profile"] is None
    assert first.payload["sealed_profile_sha256"] is None
    contract_ndcg = first.payload["policies"][CONTRACT_PLUS_CHILD]["overall"]["ndcg_at_10"]
    max_child_ndcg = first.payload["policies"][MAX_CHILD]["overall"]["ndcg_at_10"]
    assert contract_ndcg["value"] == 1.0
    assert contract_ndcg["value"] > max_child_ndcg["value"]


def test_release_profile_is_hash_bound_recomputed_and_passes_only_with_full_gold(
    tmp_path: Path,
) -> None:
    gold_path = tmp_path / "gold.jsonl"
    scores_path = tmp_path / "scores.jsonl"
    profile_path = tmp_path / "profile.json"
    manifests = tmp_path / "generation-manifests"
    generation_sha256 = _generation_manifest(manifests)
    gold_sha256 = _write_gold(gold_path, count=300, release=True)
    _write_scores(
        scores_path,
        gold_path,
        count=300,
        generation_manifest_sha256=generation_sha256,
    )

    artifact = build_aggregation_profile(
        gold_path,
        scores_path,
        expected_gold_sha256=gold_sha256,
        expected_source_commit=SOURCE_COMMIT,
        bootstrap_samples=2_000,
        bootstrap_seed=1010,
    )
    profile_bytes = artifact.canonical_bytes + b"\n"
    profile_path.write_bytes(profile_bytes)
    profile_sha256 = hashlib.sha256(profile_bytes).hexdigest()

    assert artifact.payload["release_gate"]["status"] == "passed"
    assert artifact.payload["selection"]["winner"] == CONTRACT_PLUS_CHILD
    sealed = artifact.payload["sealed_profile"]
    assert sealed["aggregation_policy"] == CONTRACT_PLUS_CHILD
    assert (
        artifact.payload["sealed_profile_sha256"] == hashlib.sha256(_canonical(sealed)).hexdigest()
    )
    validated = validate_aggregation_profile(
        profile_path,
        gold_path,
        scores_path,
        manifests,
        expected_profile_sha256=profile_sha256,
        expected_source_commit=SOURCE_COMMIT,
        bootstrap_samples=2_000,
        bootstrap_seed=1010,
    )
    assert validated.canonical_bytes == artifact.canonical_bytes


def test_incomplete_coverage_and_non_float_scores_fail_closed(tmp_path: Path) -> None:
    gold_path = tmp_path / "gold.jsonl"
    scores_path = tmp_path / "scores.jsonl"
    _write_gold(gold_path, count=1, release=False)
    generation_sha256 = hashlib.sha256(b"generation").hexdigest()
    records = _write_scores(
        scores_path,
        gold_path,
        count=1,
        generation_manifest_sha256=generation_sha256,
    )

    incomplete = [dict(record) for record in records]
    incomplete[1]["expected_rows"] = 13
    _write_jsonl(scores_path, incomplete)
    with pytest.raises(AggregationProfileError, match="score_query_coverage_incomplete"):
        build_aggregation_profile(
            gold_path,
            scores_path,
            release_gate=False,
            bootstrap_samples=100,
        )

    invalid_score = [dict(record) for record in records]
    invalid_score[2]["score"] = True
    _write_jsonl(scores_path, invalid_score)
    with pytest.raises(AggregationProfileError, match="score_row_invalid"):
        build_aggregation_profile(
            gold_path,
            scores_path,
            release_gate=False,
            bootstrap_samples=100,
        )


def test_score_artifact_is_canonical_profile_bound_and_symlink_safe(tmp_path: Path) -> None:
    gold_path = tmp_path / "gold.jsonl"
    scores_path = tmp_path / "scores.jsonl"
    link_path = tmp_path / "scores-link.jsonl"
    _write_gold(gold_path, count=1, release=False)
    _write_scores(
        scores_path,
        gold_path,
        count=1,
        generation_manifest_sha256=hashlib.sha256(b"generation").hexdigest(),
    )

    with pytest.raises(AggregationProfileError, match="candidate_source_commit_mismatch"):
        build_aggregation_profile(
            gold_path,
            scores_path,
            release_gate=False,
            expected_source_commit="2" * 40,
            bootstrap_samples=100,
        )
    with pytest.raises(AggregationProfileError, match="expected_source_commit_invalid"):
        build_aggregation_profile(
            gold_path,
            scores_path,
            release_gate=False,
            expected_source_commit="not-a-commit",
            bootstrap_samples=100,
        )
    link_path.symlink_to(scores_path)

    with pytest.raises(AggregationProfileError, match="score_artifact_not_regular"):
        build_aggregation_profile(
            gold_path,
            link_path,
            release_gate=False,
            bootstrap_samples=100,
        )

    rows = scores_path.read_bytes().splitlines(keepends=True)
    rows[2] = rows[2].replace(b'"node_id":', b'"node_id" :')
    scores_path.write_bytes(b"".join(rows))
    with pytest.raises(AggregationProfileError, match="score_not_canonical_bytes"):
        build_aggregation_profile(
            gold_path,
            scores_path,
            release_gate=False,
            bootstrap_samples=100,
        )


def test_release_workflow_recomputes_the_external_hash_bound_profile() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    for contract in (
        "aggregation_profile_sha256:",
        "AGGREGATION_PROFILE_SHA256: ${{ inputs.aggregation_profile_sha256 }}",
        'aggregation_profile="$evidence_dir/document-aggregation-profile.json"',
        'aggregation_scores="$evidence_dir/document-aggregation-scores.jsonl"',
        'serving_generation_manifest="$evidence_dir/serving-generation-manifest.json"',
        ".venv/bin/python -m cardrag_mcp.aggregation_profile",
        '--scores "$aggregation_scores"',
        '--validate-profile "$aggregation_profile"',
        '--expected-profile-sha256 "$AGGREGATION_PROFILE_SHA256"',
        '--generation-manifest-dir "$evidence_dir/generation-manifests"',
        '--serving-generation-manifest "$serving_generation_manifest"',
        "--bootstrap-samples 2000",
        "--bootstrap-seed 1010",
    ):
        assert contract in workflow
    for source_binding in (
        "candidate_source_commit:",
        "CANDIDATE_SOURCE_COMMIT: ${{ inputs.candidate_source_commit }}",
        '[[ "$CANDIDATE_SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ ]]',
        'test "$CANDIDATE_SOURCE_COMMIT" != "$GITHUB_SHA"',
        'git merge-base --is-ancestor "$CANDIDATE_SOURCE_COMMIT" "$GITHUB_SHA"',
        'git diff --name-only -z "$CANDIDATE_SOURCE_COMMIT" "$GITHUB_SHA"',
        "((${#candidate_evidence_paths[@]} > 0))",
        "release-evidence/v1.0.10/*) ;;",
    ):
        assert source_binding in workflow
    assert workflow.count('--expected-source-commit "$CANDIDATE_SOURCE_COMMIT"') >= 3
    assert '--expected-source-commit "$GITHUB_SHA"' not in workflow
