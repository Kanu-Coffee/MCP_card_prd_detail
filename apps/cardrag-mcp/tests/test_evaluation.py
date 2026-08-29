from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from cardrag_mcp.evaluation import (
    LANES,
    EvaluationError,
    _blind_release_gate_reasons,
    evaluate_gold_runs,
    load_blind_evaluation_jsonl,
    load_gold_jsonl,
    load_run_jsonl,
    main,
    validate_evaluation_report,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
GENERATION_MANIFEST_BYTES = {
    "baseline": b'{"generation":"v109-baseline"}\n',
    "exact": b'{"generation":"v110-exact"}\n',
    "page": b'{"generation":"v110-page"}\n',
}
GENERATION_MANIFEST_SHA256 = {
    name: hashlib.sha256(body).hexdigest() for name, body in GENERATION_MANIFEST_BYTES.items()
}


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> Path:
    body = b"".join(
        json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
        for record in records
    )
    path.write_bytes(body)
    return path


def _run_manifest(lane: str, gold_sha256: str, query_count: int) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "embedding_dimension": 4096,
        "embedding_model": "qwen/qwen3-embedding-8b",
        "generation_id": "generation-v110-exact",
        "generation_manifest_sha256": GENERATION_MANIFEST_SHA256["exact"],
        "gold_sha256": gold_sha256,
        "lane": lane,
        "primary_lane": None,
        "profile_id": "cardrag.eval.qwen-structure-exact.v1",
        "query_count": query_count,
        "retrieval_policy": "qwen_structure_exact",
        "rrf_k": None,
        "schema_version": "cardrag.gold-run-artifact.v1",
        "serving_schema": "cardrag.serving-db.v5",
        "shadow_model": None,
        "shadow_only": False,
        "source_commit": "1" * 40,
        "source_version": "v1.0.10-candidate",
    }
    if lane == "v109_baseline":
        manifest.update(
            {
                "embedding_dimension": 1536,
                "embedding_model": "openai/text-embedding-3-small",
                "generation_id": "generation-v109-baseline",
                "generation_manifest_sha256": GENERATION_MANIFEST_SHA256["baseline"],
                "profile_id": "cardrag.eval.v109-small-rrf.v1",
                "retrieval_policy": "small_rrf",
                "rrf_k": 60,
                "serving_schema": "cardrag.serving-db.v4",
                "source_commit": "fee8f65a9fda7ae0c286ac92cf4c3f55c1a6f113",
                "source_version": "v1.0.9",
            }
        )
    elif lane == "qwen_page":
        manifest.update(
            {
                "generation_id": "generation-qwen-page",
                "generation_manifest_sha256": GENERATION_MANIFEST_SHA256["page"],
                "profile_id": "cardrag.eval.qwen-page.v1",
                "retrieval_policy": "qwen_page_window",
                "serving_schema": "cardrag.evaluation-page.v1",
            }
        )
    elif lane == "lexical_shadow":
        manifest.update(
            {
                "primary_lane": "qwen_structure_exact",
                "profile_id": "cardrag.eval.lexical-shadow.v1",
                "retrieval_policy": "qwen_structure_exact_lexical_shadow",
                "shadow_only": True,
            }
        )
    elif lane == "reranker_shadow":
        manifest.update(
            {
                "primary_lane": "qwen_structure_exact",
                "profile_id": "cardrag.eval.reranker-shadow.v1",
                "retrieval_policy": "qwen_structure_exact_reranker_shadow",
                "shadow_model": "qwen/qwen3-reranker-8b",
                "shadow_only": True,
            }
        )
    return manifest


def _write_run_jsonl(
    path: Path,
    lane: str,
    gold_sha256: str,
    records: list[dict[str, Any]],
) -> Path:
    return _write_jsonl(path, [_run_manifest(lane, gold_sha256, len(records)), *records])


def _gold_records() -> list[dict[str, Any]]:
    return [
        {
            "condition_groups": [{"at_k": 10, "span_ids": ["benefit-1", "condition-1"]}],
            "contracts": [{"contract_revision_id": "contract-kb-current", "relevance": 3}],
            "expected_numeric_facts": ["월 10,000원"],
            "expected_revision_ids": ["contract-kb-current"],
            "high_risk": True,
            "no_answer": False,
            "query_id": "q-benefit",
            "question": "KB 카드 혜택과 월 한도는?",
            "schema_version": "cardrag.gold-query.v1",
            "slices": ["benefit", "issuer:kb", "limit"],
            "spans": [
                {
                    "contract_revision_id": "contract-kb-current",
                    "page": 1,
                    "relevance": 3,
                    "roles": ["benefit"],
                    "source_end": 20,
                    "source_start": 0,
                    "span_id": "benefit-1",
                    "text_sha256": SHA_A,
                },
                {
                    "contract_revision_id": "contract-kb-current",
                    "page": 2,
                    "relevance": 3,
                    "roles": ["condition", "numeric", "revision"],
                    "source_end": 45,
                    "source_start": 21,
                    "span_id": "condition-1",
                    "text_sha256": SHA_B,
                },
            ],
        },
        {
            "condition_groups": [],
            "contracts": [],
            "expected_numeric_facts": [],
            "expected_revision_ids": [],
            "high_risk": False,
            "no_answer": True,
            "query_id": "q-no-answer",
            "question": "존재하지 않는 우주여행 적립 혜택은?",
            "schema_version": "cardrag.gold-query.v1",
            "slices": ["issuer:samsung", "no_answer"],
            "spans": [],
        },
        {
            "condition_groups": [],
            "contracts": [
                {"contract_revision_id": "contract-shinhan-a", "relevance": 3},
                {"contract_revision_id": "contract-shinhan-b", "relevance": 2},
            ],
            "expected_numeric_facts": [],
            "expected_revision_ids": [],
            "high_risk": False,
            "no_answer": False,
            "query_id": "q-comparison",
            "question": "신한 카드 두 상품의 적립 혜택을 비교해줘.",
            "schema_version": "cardrag.gold-query.v1",
            "slices": ["comparison", "issuer:shinhan"],
            "spans": [
                {
                    "contract_revision_id": "contract-shinhan-a",
                    "page": 3,
                    "relevance": 3,
                    "roles": ["benefit"],
                    "source_end": 18,
                    "source_start": 0,
                    "span_id": "shinhan-a-benefit",
                    "text_sha256": SHA_C,
                },
                {
                    "contract_revision_id": "contract-shinhan-b",
                    "page": 4,
                    "relevance": 2,
                    "roles": ["benefit"],
                    "source_end": 40,
                    "source_start": 19,
                    "span_id": "shinhan-b-benefit",
                    "text_sha256": SHA_D,
                },
            ],
        },
        {
            "condition_groups": [],
            "contracts": [
                {"contract_revision_id": "contract-woori-current", "relevance": 3},
                {"contract_revision_id": "contract-woori-old", "relevance": 1},
            ],
            "expected_numeric_facts": [],
            "expected_revision_ids": ["contract-woori-current"],
            "high_risk": True,
            "no_answer": False,
            "query_id": "q-revision",
            "question": "우리 카드의 현재 개정 혜택은?",
            "schema_version": "cardrag.gold-query.v1",
            "slices": ["current_history", "issuer:woori"],
            "spans": [
                {
                    "contract_revision_id": "contract-woori-current",
                    "page": 5,
                    "relevance": 3,
                    "roles": ["revision"],
                    "source_end": 30,
                    "source_start": 5,
                    "span_id": "woori-current",
                    "text_sha256": SHA_A,
                }
            ],
        },
    ]


def _contract(contract_id: str, rank: int, score: float) -> dict[str, Any]:
    return {"contract_revision_id": contract_id, "rank": rank, "score": score}


def _span(span_id: str, contract_id: str, rank: int, score: float) -> dict[str, Any]:
    return {
        "contract_revision_id": contract_id,
        "rank": rank,
        "score": score,
        "span_id": span_id,
    }


def _perfect_result(query_id: str, lane: str) -> dict[str, Any]:
    if query_id == "q-benefit":
        contracts = [_contract("contract-kb-current", 1, 0.99)]
        spans = [
            _span("benefit-1", "contract-kb-current", 1, 0.99),
            _span("condition-1", "contract-kb-current", 2, 0.98),
        ]
        answer = {
            "citation_span_ids": ["benefit-1", "condition-1"],
            "no_answer": False,
            "numeric_facts": ["월 10,000원"],
            "selected_revision_ids": ["contract-kb-current"],
            "text": "월 10,000원 한도와 적용 조건을 함께 확인해야 합니다.",
        }
    elif query_id == "q-no-answer":
        contracts, spans = [], []
        answer = {
            "citation_span_ids": [],
            "no_answer": True,
            "numeric_facts": [],
            "selected_revision_ids": [],
            "text": "안내장에서 답을 확인할 수 없습니다.",
        }
    elif query_id == "q-comparison":
        contracts = [
            _contract("contract-shinhan-a", 1, 0.98),
            _contract("contract-shinhan-b", 2, 0.88),
        ]
        spans = [
            _span("shinhan-a-benefit", "contract-shinhan-a", 1, 0.98),
            _span("shinhan-b-benefit", "contract-shinhan-b", 2, 0.88),
        ]
        answer = {
            "citation_span_ids": ["shinhan-a-benefit", "shinhan-b-benefit"],
            "no_answer": False,
            "numeric_facts": [],
            "selected_revision_ids": [],
            "text": "두 카드의 혜택과 조건을 계약별로 비교했습니다.",
        }
    else:
        contracts = [
            _contract("contract-woori-current", 1, 0.97),
            _contract("contract-woori-old", 2, 0.60),
        ]
        spans = [_span("woori-current", "contract-woori-current", 1, 0.97)]
        answer = {
            "citation_span_ids": ["woori-current"],
            "no_answer": False,
            "numeric_facts": [],
            "selected_revision_ids": ["contract-woori-current"],
            "text": "현재 개정 안내장의 혜택입니다.",
        }
    result = {
        "answer": answer,
        "contracts": contracts,
        "lane": lane,
        "query_id": query_id,
        "schema_version": "cardrag.gold-run-result.v1",
        "spans": spans,
    }
    if lane in {"lexical_shadow", "reranker_shadow"}:
        result["shadow"] = {
            "contracts": contracts,
            "influenced_primary_ordering": False,
            "kind": "lexical" if lane == "lexical_shadow" else "reranker",
            "spans": spans,
        }
    return result


def _baseline_result(query_id: str, lane: str) -> dict[str, Any]:
    hard_contract = f"irrelevant-{query_id}"
    contracts = [_contract(hard_contract, 1, 0.80)]
    spans = [_span(f"irrelevant-span-{query_id}", hard_contract, 1, 0.80)]
    answer = {
        "citation_span_ids": [],
        "no_answer": False,
        "numeric_facts": [],
        "selected_revision_ids": [],
        "text": f"기준 답변 {query_id}",
    }
    return {
        "answer": answer,
        "contracts": contracts,
        "lane": lane,
        "query_id": query_id,
        "schema_version": "cardrag.gold-run-result.v1",
        "spans": spans,
        "v109_baseline": {
            "dense_contracts": contracts,
            "dense_spans": spans,
            "kind": "v109_small_rrf",
            "rrf_k": 60,
        },
    }


def _answer_text_hash(result: dict[str, Any]) -> str:
    return hashlib.sha256(result["answer"]["text"].encode()).hexdigest()


def _write_blind_evaluation(
    path: Path,
    gold: Path,
    run_paths: dict[str, Path],
    *,
    naturalness: str = "tie",
    factual_completeness: str = "candidate",
) -> Path:
    def results_by_id(run_path: Path) -> dict[str, dict[str, Any]]:
        records = [json.loads(line) for line in run_path.read_text().splitlines()]
        return {record["query_id"]: record for record in records[1:]}

    baseline = results_by_id(run_paths["v109_baseline"])
    candidate = results_by_id(run_paths["qwen_structure_exact"])
    query_ids = list(baseline)

    def preference(outcome: str, candidate_position: str) -> str:
        if outcome == "tie":
            return "tie"
        if outcome == "candidate":
            return candidate_position
        return "right" if candidate_position == "left" else "left"

    ratings: list[dict[str, Any]] = []
    for index, query_id in enumerate(query_ids):
        candidate_position = "left" if index % 2 == 0 else "right"
        baseline_hash = _answer_text_hash(baseline[query_id])
        candidate_hash = _answer_text_hash(candidate[query_id])
        left_hash, right_hash = (
            (candidate_hash, baseline_hash)
            if candidate_position == "left"
            else (baseline_hash, candidate_hash)
        )
        ratings.append(
            {
                "candidate_position": candidate_position,
                "factual_completeness_preference": preference(
                    factual_completeness, candidate_position
                ),
                "left_answer_sha256": left_hash,
                "naturalness_preference": preference(naturalness, candidate_position),
                "pair_id": f"pair-{index:04d}",
                "query_id": query_id,
                "rater_key": "anonymous-rater-01",
                "right_answer_sha256": right_hash,
                "schema_version": "cardrag.blind-pairwise-rating.v1",
            }
        )
    manifest = {
        "baseline_lane": "v109_baseline",
        "baseline_run_sha256": hashlib.sha256(run_paths["v109_baseline"].read_bytes()).hexdigest(),
        "candidate_lane": "qwen_structure_exact",
        "candidate_run_sha256": hashlib.sha256(
            run_paths["qwen_structure_exact"].read_bytes()
        ).hexdigest(),
        "gold_sha256": hashlib.sha256(gold.read_bytes()).hexdigest(),
        "lane_identity_exposed_to_raters": False,
        "pair_count": len(ratings),
        "presentation_protocol": "anonymous-a-b.v1",
        "query_count": len(ratings),
        "ratings_per_query": 1,
        "rubric_id": "cardrag.blind-rubric.naturalness-factual-completeness.v1",
        "schema_version": "cardrag.blind-evaluation-artifact.v1",
    }
    return _write_jsonl(path, [manifest, *ratings])


def _write_generation_manifests(directory: Path) -> Path:
    directory.mkdir()
    for name, body in GENERATION_MANIFEST_BYTES.items():
        (directory / f"{GENERATION_MANIFEST_SHA256[name]}.json").write_bytes(body)
    return directory


@pytest.fixture
def evaluation_files(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    gold = _write_jsonl(tmp_path / "gold.jsonl", _gold_records())
    gold_sha256 = hashlib.sha256(gold.read_bytes()).hexdigest()
    paths: dict[str, Path] = {}
    query_ids = [record["query_id"] for record in _gold_records()]
    for lane in LANES:
        if lane == "v109_baseline":
            records = [_baseline_result(query_id, lane) for query_id in query_ids]
        else:
            records = [_perfect_result(query_id, lane) for query_id in query_ids]
        paths[lane] = _write_run_jsonl(
            tmp_path / f"{lane}.jsonl",
            lane,
            gold_sha256,
            records,
        )
    return gold, paths


def test_offline_report_is_deterministic_and_covers_all_required_metrics(
    evaluation_files: tuple[Path, dict[str, Path]],
    tmp_path: Path,
) -> None:
    gold, paths = evaluation_files
    blind = _write_blind_evaluation(tmp_path / "blind.jsonl", gold, paths)

    first = evaluate_gold_runs(
        gold,
        paths,
        blind_evaluation_path=blind,
        release_gate=False,
        bootstrap_samples=200,
        bootstrap_seed=77,
    )
    second = evaluate_gold_runs(
        gold,
        paths,
        blind_evaluation_path=blind,
        release_gate=False,
        bootstrap_samples=200,
        bootstrap_seed=77,
    )

    assert first.canonical_bytes == second.canonical_bytes
    assert first.sha256 == hashlib.sha256(first.canonical_bytes).hexdigest()
    assert json.loads(first.canonical_bytes) == first.payload
    exact = first.payload["lanes"]["qwen_structure_exact"]["overall"]
    assert exact["contract_recall_at_10"]["value"] == 1.0
    assert exact["contract_recall_at_50"]["value"] == 1.0
    assert exact["contract_recall_at_100"]["value"] == 1.0
    assert exact["span_recall_at_5"]["value"] == 1.0
    assert exact["span_recall_at_10"]["value"] == 1.0
    assert exact["ndcg_at_10"]["value"] == 1.0
    assert exact["mrr_at_10"]["value"] == 1.0
    assert exact["condition_coretrieval"]["value"] == 1.0
    assert exact["numeric_fact_exact_match"]["value"] == 1.0
    assert exact["revision_accuracy"]["value"] == 1.0
    assert exact["no_answer_accuracy"]["value"] == 1.0
    assert exact["citation_precision"]["value"] == 1.0
    assert exact["citation_recall"]["value"] == 1.0
    assert first.payload["release_gate"]["status"] == "not_evaluated"
    assert first.payload["blind_evaluation"]["overall"]["naturalness"]["delta"] == 0.0
    assert first.payload["blind_evaluation"]["overall"]["factual_completeness"]["delta"] == 1.0
    assert first.payload["lanes"]["v109_baseline"]["baseline_trace"] == {
        "dense_raw_query_count": 4,
        "rrf_k": 60,
    }
    delta = first.payload["comparisons"]["qwen_structure_exact_vs_v109_baseline"]["overall"][
        "contract_recall_at_10"
    ]
    assert delta["delta"] == 1.0
    assert delta["ci95"]["low"] == 1.0


def test_release_mode_rejects_fixture_sized_gold_without_fabricating_labels(
    evaluation_files: tuple[Path, dict[str, Path]],
) -> None:
    gold, paths = evaluation_files

    with pytest.raises(EvaluationError, match="gold_count_out_of_release_range") as error:
        evaluate_gold_runs(gold, paths, release_gate=True, bootstrap_samples=100)
    assert error.value.code == "gold_count_out_of_release_range"


def test_v109_baseline_requires_an_exact_dense_raw_trace(
    evaluation_files: tuple[Path, dict[str, Path]],
    tmp_path: Path,
) -> None:
    gold, paths = evaluation_files
    gold_sha256 = hashlib.sha256(gold.read_bytes()).hexdigest()
    query_ids = [record["query_id"] for record in _gold_records()]
    records = [_baseline_result(query_id, "v109_baseline") for query_id in query_ids]
    records[0].pop("v109_baseline")
    invalid = _write_run_jsonl(
        tmp_path / "v109-missing-dense-trace.jsonl",
        "v109_baseline",
        gold_sha256,
        records,
    )

    with pytest.raises(EvaluationError, match="run_schema_invalid"):
        load_run_jsonl(invalid, lane="v109_baseline")


def test_gold_schema_rejects_unsorted_slices_and_duplicate_query_ids(tmp_path: Path) -> None:
    records = _gold_records()
    records[0]["slices"] = ["issuer:kb", "benefit"]
    invalid = _write_jsonl(tmp_path / "invalid.jsonl", records)
    with pytest.raises(EvaluationError, match="gold_schema_invalid"):
        load_gold_jsonl(invalid)

    duplicates = _gold_records()
    duplicates.append(dict(duplicates[0]))
    duplicate = _write_jsonl(tmp_path / "duplicate.jsonl", duplicates)
    with pytest.raises(EvaluationError, match="gold_query_duplicate"):
        load_gold_jsonl(duplicate)


def test_gold_loader_rejects_duplicate_json_keys_and_missing_final_newline(tmp_path: Path) -> None:
    duplicate_key = tmp_path / "duplicate-key.jsonl"
    duplicate_key.write_text('{"query_id":"one","query_id":"two"}\n')
    with pytest.raises(EvaluationError, match="json_duplicate_key"):
        load_gold_jsonl(duplicate_key)

    no_newline = tmp_path / "no-newline.jsonl"
    no_newline.write_text(json.dumps(_gold_records()[0]))
    with pytest.raises(EvaluationError, match="jsonl_not_canonical_lines"):
        load_gold_jsonl(no_newline)


def test_run_requires_exact_query_coverage_and_lane_identity(
    evaluation_files: tuple[Path, dict[str, Path]], tmp_path: Path
) -> None:
    gold, paths = evaluation_files
    shortened = _gold_records()[:-1]
    paths["qwen_page"] = _write_run_jsonl(
        tmp_path / "short.jsonl",
        "qwen_page",
        hashlib.sha256(gold.read_bytes()).hexdigest(),
        [_perfect_result(record["query_id"], "qwen_page") for record in shortened],
    )
    with pytest.raises(EvaluationError, match="run_query_coverage_mismatch"):
        evaluate_gold_runs(gold, paths, release_gate=False, bootstrap_samples=100)

    mismatch = _write_run_jsonl(
        tmp_path / "wrong-lane.jsonl",
        "reranker_shadow",
        hashlib.sha256(gold.read_bytes()).hexdigest(),
        [_perfect_result("q-benefit", "reranker_shadow")],
    )
    with pytest.raises(EvaluationError, match="run_lane_mismatch"):
        load_run_jsonl(mismatch, lane="qwen_page")


def test_run_schema_rejects_noncontiguous_ranks(tmp_path: Path) -> None:
    result = _perfect_result("q-comparison", "qwen_page")
    result["contracts"][1]["rank"] = 3
    path = _write_run_jsonl(tmp_path / "bad-rank.jsonl", "qwen_page", "a" * 64, [result])

    with pytest.raises(EvaluationError, match="run_schema_invalid"):
        load_run_jsonl(path, lane="qwen_page")


def test_v109_baseline_manifest_is_profile_bound_and_sealed(tmp_path: Path) -> None:
    manifest = _run_manifest("v109_baseline", "a" * 64, 1)
    manifest["embedding_dimension"] = 4096
    path = _write_jsonl(
        tmp_path / "invalid-baseline.jsonl",
        [manifest, _baseline_result("q-benefit", "v109_baseline")],
    )

    with pytest.raises(EvaluationError, match="run_manifest_invalid"):
        load_run_jsonl(path, lane="v109_baseline")


def test_shadow_artifact_cannot_change_primary_result_or_generation(
    evaluation_files: tuple[Path, dict[str, Path]], tmp_path: Path
) -> None:
    gold, paths = evaluation_files
    gold_sha256 = hashlib.sha256(gold.read_bytes()).hexdigest()
    query_ids = [record["query_id"] for record in _gold_records()]
    changed = [_perfect_result(query_id, "lexical_shadow") for query_id in query_ids]
    changed[0]["contracts"][0]["score"] = 0.25
    paths["lexical_shadow"] = _write_run_jsonl(
        tmp_path / "changed-primary.jsonl",
        "lexical_shadow",
        gold_sha256,
        changed,
    )
    with pytest.raises(EvaluationError, match="shadow_changed_primary_result"):
        evaluate_gold_runs(gold, paths, release_gate=False, bootstrap_samples=100)

    # Rebuild a clean lane, but claim a different generation from exact.
    records = [_perfect_result(query_id, "lexical_shadow") for query_id in query_ids]
    manifest = _run_manifest("lexical_shadow", gold_sha256, len(records))
    manifest["generation_id"] = "different-generation"
    paths["lexical_shadow"] = _write_jsonl(
        tmp_path / "changed-generation.jsonl", [manifest, *records]
    )
    with pytest.raises(EvaluationError, match="shadow_primary_generation_mismatch"):
        evaluate_gold_runs(gold, paths, release_gate=False, bootstrap_samples=100)


def test_run_manifest_must_bind_the_exact_gold_artifact(
    evaluation_files: tuple[Path, dict[str, Path]], tmp_path: Path
) -> None:
    gold, paths = evaluation_files
    query_ids = [record["query_id"] for record in _gold_records()]
    paths["qwen_page"] = _write_run_jsonl(
        tmp_path / "wrong-gold.jsonl",
        "qwen_page",
        "f" * 64,
        [_perfect_result(query_id, "qwen_page") for query_id in query_ids],
    )

    with pytest.raises(EvaluationError, match="run_manifest_gold_mismatch"):
        evaluate_gold_runs(gold, paths, release_gate=False, bootstrap_samples=100)


def test_expected_gold_sha_and_bootstrap_floor_fail_closed(
    evaluation_files: tuple[Path, dict[str, Path]],
) -> None:
    gold, paths = evaluation_files
    with pytest.raises(EvaluationError, match="gold_sha256_mismatch"):
        evaluate_gold_runs(
            gold,
            paths,
            release_gate=False,
            expected_gold_sha256="0" * 64,
            bootstrap_samples=100,
        )
    evaluate_gold_runs(
        gold,
        paths,
        release_gate=False,
        expected_source_commit="1" * 40,
        bootstrap_samples=100,
    )
    with pytest.raises(EvaluationError, match="candidate_source_commit_mismatch"):
        evaluate_gold_runs(
            gold,
            paths,
            release_gate=False,
            expected_source_commit="2" * 40,
            bootstrap_samples=100,
        )
    with pytest.raises(EvaluationError, match="expected_source_commit_invalid"):
        evaluate_gold_runs(
            gold,
            paths,
            release_gate=False,
            expected_source_commit="not-a-commit",
            bootstrap_samples=100,
        )
    with pytest.raises(EvaluationError, match="bootstrap_samples_too_small"):
        evaluate_gold_runs(gold, paths, release_gate=False, bootstrap_samples=99)
    with pytest.raises(EvaluationError, match="bootstrap_samples_too_large"):
        evaluate_gold_runs(gold, paths, release_gate=False, bootstrap_samples=10_001)


def test_all_five_lanes_are_mandatory(evaluation_files: tuple[Path, dict[str, Path]]) -> None:
    gold, paths = evaluation_files
    paths.pop("reranker_shadow")

    with pytest.raises(EvaluationError, match="required_run_lane_missing"):
        evaluate_gold_runs(gold, paths, release_gate=False, bootstrap_samples=100)


def test_blind_pairwise_artifact_is_strict_and_answer_hash_bound(
    evaluation_files: tuple[Path, dict[str, Path]],
    tmp_path: Path,
) -> None:
    gold, paths = evaluation_files
    blind = _write_blind_evaluation(tmp_path / "blind.jsonl", gold, paths)

    loaded = load_blind_evaluation_jsonl(blind)
    assert loaded.manifest.query_count == len(_gold_records())
    evaluate_gold_runs(
        gold,
        paths,
        blind_evaluation_path=blind,
        release_gate=False,
        bootstrap_samples=100,
    )

    records = [json.loads(line) for line in blind.read_text().splitlines()]
    records[1]["left_answer_sha256"] = "0" * 64
    wrong_answer_hash = _write_jsonl(tmp_path / "wrong-answer-hash.jsonl", records)
    with pytest.raises(EvaluationError, match="blind_answer_hash_mismatch"):
        evaluate_gold_runs(
            gold,
            paths,
            blind_evaluation_path=wrong_answer_hash,
            release_gate=False,
            bootstrap_samples=100,
        )

    records = [json.loads(line) for line in blind.read_text().splitlines()]
    records[1]["unexpected_lane_hint"] = "candidate"
    extra_field = _write_jsonl(tmp_path / "extra-field.jsonl", records)
    with pytest.raises(EvaluationError, match="blind_rating_invalid"):
        load_blind_evaluation_jsonl(extra_field)

    records = [json.loads(line) for line in blind.read_text().splitlines()]
    records[0]["query_count"] = str(records[0]["query_count"])
    coerced_count = _write_jsonl(tmp_path / "coerced-count.jsonl", records)
    with pytest.raises(EvaluationError, match="blind_manifest_invalid"):
        load_blind_evaluation_jsonl(coerced_count)

    records = [json.loads(line) for line in blind.read_text().splitlines()]
    records[1]["right_answer_sha256"] = records[1]["left_answer_sha256"]
    identical_but_preferred = _write_jsonl(tmp_path / "identical-preferred.jsonl", records)
    with pytest.raises(EvaluationError, match="blind_identical_answer_preference_invalid"):
        load_blind_evaluation_jsonl(identical_but_preferred)

    noncanonical = tmp_path / "noncanonical-blind.jsonl"
    noncanonical.write_text(blind.read_text().replace("{", "{ ", 1))
    with pytest.raises(EvaluationError, match="jsonl_not_canonical_bytes"):
        load_blind_evaluation_jsonl(noncanonical)

    blind_symlink = tmp_path / "blind-symlink.jsonl"
    blind_symlink.symlink_to(blind)
    with pytest.raises(EvaluationError, match="jsonl_not_regular"):
        load_blind_evaluation_jsonl(blind_symlink)


def test_blind_release_gate_requires_naturalness_nonregression_and_completeness_gain(
    evaluation_files: tuple[Path, dict[str, Path]],
    tmp_path: Path,
) -> None:
    gold, paths = evaluation_files
    passing_blind = _write_blind_evaluation(tmp_path / "passing.jsonl", gold, paths)
    passing = evaluate_gold_runs(
        gold,
        paths,
        blind_evaluation_path=passing_blind,
        release_gate=False,
        bootstrap_samples=100,
    )
    assert _blind_release_gate_reasons(passing.payload["blind_evaluation"]) == []

    failing_blind = _write_blind_evaluation(
        tmp_path / "failing.jsonl",
        gold,
        paths,
        naturalness="baseline",
        factual_completeness="tie",
    )
    failing = evaluate_gold_runs(
        gold,
        paths,
        blind_evaluation_path=failing_blind,
        release_gate=False,
        bootstrap_samples=100,
    )
    assert _blind_release_gate_reasons(failing.payload["blind_evaluation"]) == [
        "blind_naturalness_regression",
        "blind_factual_completeness_not_improved",
    ]


def test_strict_report_validator_recomputes_every_artifact_and_rejects_tampering(
    evaluation_files: tuple[Path, dict[str, Path]],
    tmp_path: Path,
) -> None:
    gold, paths = evaluation_files
    blind = _write_blind_evaluation(tmp_path / "blind.jsonl", gold, paths)
    manifests = _write_generation_manifests(tmp_path / "generation-manifests")
    report = evaluate_gold_runs(
        gold,
        paths,
        blind_evaluation_path=blind,
        release_gate=False,
        bootstrap_samples=100,
        bootstrap_seed=77,
    )
    report_path = tmp_path / "report.json"
    report_path.write_bytes(report.canonical_bytes + b"\n")
    report_sha256 = hashlib.sha256(report_path.read_bytes()).hexdigest()

    validated = validate_evaluation_report(
        report_path,
        gold,
        paths,
        blind,
        manifests,
        expected_report_sha256=report_sha256,
        expected_source_commit="1" * 40,
        release_gate=False,
        bootstrap_samples=100,
        bootstrap_seed=77,
    )
    assert validated.canonical_bytes == report.canonical_bytes

    with pytest.raises(EvaluationError, match="candidate_source_commit_mismatch"):
        validate_evaluation_report(
            report_path,
            gold,
            paths,
            blind,
            manifests,
            expected_report_sha256=report_sha256,
            expected_source_commit="2" * 40,
            release_gate=False,
            bootstrap_samples=100,
            bootstrap_seed=77,
        )

    with pytest.raises(EvaluationError, match="report_sha256_mismatch"):
        validate_evaluation_report(
            report_path,
            gold,
            paths,
            blind,
            manifests,
            expected_report_sha256="0" * 64,
            release_gate=False,
            bootstrap_samples=100,
            bootstrap_seed=77,
        )

    tampered_paths = dict(paths)
    baseline_records = [
        json.loads(line) for line in paths["v109_baseline"].read_text().splitlines()
    ]
    baseline_records[1]["answer"]["text"] = "사후 교체한 기준 답변"
    tampered_paths["v109_baseline"] = _write_jsonl(
        tmp_path / "tampered-baseline.jsonl", baseline_records
    )
    with pytest.raises(EvaluationError, match="blind_artifact_binding_mismatch"):
        validate_evaluation_report(
            report_path,
            gold,
            tampered_paths,
            blind,
            manifests,
            expected_report_sha256=report_sha256,
            release_gate=False,
            bootstrap_samples=100,
            bootstrap_seed=77,
        )

    pretty_report = tmp_path / "pretty-report.json"
    pretty_report.write_text(json.dumps(report.payload, ensure_ascii=False, indent=2) + "\n")
    with pytest.raises(EvaluationError, match="report_not_canonical_bytes"):
        validate_evaluation_report(
            pretty_report,
            gold,
            paths,
            blind,
            manifests,
            expected_report_sha256=hashlib.sha256(pretty_report.read_bytes()).hexdigest(),
            release_gate=False,
            bootstrap_samples=100,
            bootstrap_seed=77,
        )

    tampered_payload = dict(report.payload)
    tampered_payload["unvalidated_claim"] = True
    tampered_report = tmp_path / "tampered-report.json"
    tampered_report.write_bytes(
        json.dumps(
            tampered_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    with pytest.raises(EvaluationError, match="report_recomputation_mismatch"):
        validate_evaluation_report(
            tampered_report,
            gold,
            paths,
            blind,
            manifests,
            expected_report_sha256=hashlib.sha256(tampered_report.read_bytes()).hexdigest(),
            release_gate=False,
            bootstrap_samples=100,
            bootstrap_seed=77,
        )

    exact_manifest = manifests / f"{GENERATION_MANIFEST_SHA256['exact']}.json"
    exact_manifest.write_bytes(b'{"generation":"tampered"}\n')
    with pytest.raises(EvaluationError, match="generation_manifest_sha256_mismatch"):
        validate_evaluation_report(
            report_path,
            gold,
            paths,
            blind,
            manifests,
            expected_report_sha256=report_sha256,
            release_gate=False,
            bootstrap_samples=100,
            bootstrap_seed=77,
        )


def test_fixture_mode_cli_emits_canonical_report(
    evaluation_files: tuple[Path, dict[str, Path]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    gold, paths = evaluation_files
    blind = _write_blind_evaluation(tmp_path / "blind-cli.jsonl", gold, paths)
    arguments = [
        "--gold",
        str(gold),
        "--blind-evaluation",
        str(blind),
        "--fixture-mode",
        "--expected-source-commit",
        "1" * 40,
        "--bootstrap-samples",
        "100",
    ]
    for lane in LANES:
        arguments.extend(("--run", f"{lane}={paths[lane]}"))

    assert main(arguments) == 0
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert payload["schema_version"] == "cardrag.gold-evaluation-report.v1"
    assert payload["blind_evaluation"]["evaluated"] is True
    assert payload["release_gate"]["status"] == "not_evaluated"
    assert output.err == ""
