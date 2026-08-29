from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from cardrag_core import (
    DocumentAggregationBootstrap,
    DocumentAggregationProfile,
    MaxChildAggregationDefinition,
    canonical_json_bytes,
    canonical_sha256,
)

from cardrag_worker.aggregation_profile_v5 import (
    AggregationProfileV5Error,
    load_verified_aggregation_profile_v5,
)
from cardrag_worker.settings import WorkerSettings


def _profile_artifact() -> tuple[dict[str, object], DocumentAggregationProfile]:
    profile = DocumentAggregationProfile(
        schema_version="cardrag.document-aggregation-profile.v1",
        profile_id="cardrag.document-aggregation.max-child.v1",
        aggregation_policy="max_child",
        aggregation_definition=MaxChildAggregationDefinition(
            child_view_types=(
                "CONTEXTUAL_ITEM",
                "DETAIL",
                "MAJOR_SECTION",
                "RAW_ITEM",
                "TITLE",
            ),
            formula="max(non-CONTRACT row score)",
        ),
        bootstrap=DocumentAggregationBootstrap(
            ci=0.95,
            method="paired-query-percentile-pcg64",
            samples=2_000,
            seed=1010,
        ),
        embedding_profile_id="qwen3-embedding-8b-deepinfra-4096d-v1",
        exact_row_corpus_sha256="e" * 64,
        generation_id="g-evaluated-m0",
        generation_manifest_sha256="1" * 64,
        gold_sha256="2" * 64,
        score_artifact_sha256="3" * 64,
        selection_objective="ndcg_at_10",
    )
    score_manifest: dict[str, object] = {
        "approximate": False,
        "embedding_dimension": 4096,
        "embedding_model": "qwen/qwen3-embedding-8b",
        "embedding_profile_id": profile.embedding_profile_id,
        "exact": True,
        "exact_row_corpus_sha256": profile.exact_row_corpus_sha256,
        "generation_id": profile.generation_id,
        "generation_manifest_sha256": profile.generation_manifest_sha256,
        "gold_sha256": profile.gold_sha256,
        "query_count": 300,
        "row_count": 900,
        "runtime_document_aggregation_policy": "max_child",
        "runtime_document_aggregation_status": "candidate_default",
        "runtime_sealed_profile_sha256": None,
        "schema_version": "cardrag.document-aggregation-score-artifact.v1",
        "scoring_contract": "cardrag.v5-exact-row-score.v1",
        "serving_database_sha256": "4" * 64,
        "source_commit": "5" * 40,
        "temporal_scope_policy": "gold-query.v1",
        "vector_sidecar_sha256": "6" * 64,
    }
    definition = profile.aggregation_definition.model_dump(mode="json")
    artifact: dict[str, object] = {
        "artifact_bindings": {
            "generation_manifest_sha256": profile.generation_manifest_sha256,
            "gold_sha256": profile.gold_sha256,
            "score_artifact_manifest_sha256": canonical_sha256(score_manifest),
            "score_artifact_sha256": profile.score_artifact_sha256,
            "score_artifact_size_bytes": 123_456,
        },
        "bootstrap": profile.bootstrap.model_dump(mode="json"),
        "comparisons": {"max_child_vs_top3_mean": {}},
        "coverage": {
            "all_queries_exact": True,
            "approximate": False,
            "maximum_active_contracts": 100,
            "minimum_active_contracts": 100,
            "query_count": 300,
            "row_count": 900,
        },
        "definitions": {"max_child": definition},
        "excluded_nonretrieval_slices": ["no_answer"],
        "policies": {"max_child": {}},
        "release_gate": {"evaluated": True, "failure_reasons": [], "status": "passed"},
        "schema_version": "cardrag.document-aggregation-profile-artifact.v1",
        "score_artifact_manifest": score_manifest,
        "sealed_profile": profile.model_dump(mode="json"),
        "sealed_profile_sha256": profile.profile_sha256,
        "selection": {
            "objective": "ndcg_at_10",
            "rule": "unique policy with paired CI95 lower bound > 0 against every alternative",
            "winner": "max_child",
        },
    }
    return artifact, profile


def _write_artifact(path: Path, payload: dict[str, object]) -> str:
    body = canonical_json_bytes(payload) + b"\n"
    path.write_bytes(body)
    return hashlib.sha256(body).hexdigest()


def test_load_verified_aggregation_profile_requires_canonical_passed_m0_artifact(
    tmp_path: Path,
) -> None:
    payload, profile = _profile_artifact()
    path = tmp_path / "document-aggregation-profile.json"
    artifact_sha256 = _write_artifact(path, payload)

    selected = load_verified_aggregation_profile_v5(
        path,
        expected_artifact_sha256=artifact_sha256,
    )

    assert selected.profile == profile
    assert selected.profile_sha256 == profile.profile_sha256
    assert selected.artifact_sha256 == artifact_sha256


@pytest.mark.parametrize(
    ("mutation", "reason"),
    (
        (lambda payload: payload["release_gate"].update(status="failed"), "not_passed"),
        (
            lambda payload: payload["score_artifact_manifest"].update(
                runtime_document_aggregation_status="sealed"
            ),
            "score_manifest",
        ),
        (lambda payload: payload["selection"].update(winner="top3_mean"), "selection"),
    ),
)
def test_load_verified_aggregation_profile_rejects_unsealed_or_inconsistent_artifact(
    tmp_path: Path,
    mutation: object,
    reason: str,
) -> None:
    payload, _profile = _profile_artifact()
    assert callable(mutation)
    mutation(payload)
    path = tmp_path / "document-aggregation-profile.json"
    artifact_sha256 = _write_artifact(path, payload)

    with pytest.raises(AggregationProfileV5Error, match=reason):
        load_verified_aggregation_profile_v5(path, expected_artifact_sha256=artifact_sha256)


def test_load_verified_aggregation_profile_rejects_hash_noncanonical_and_symlink(
    tmp_path: Path,
) -> None:
    payload, _profile = _profile_artifact()
    path = tmp_path / "document-aggregation-profile.json"
    artifact_sha256 = _write_artifact(path, payload)

    with pytest.raises(AggregationProfileV5Error, match="sha256_mismatch"):
        load_verified_aggregation_profile_v5(path, expected_artifact_sha256="0" * 64)

    noncanonical = tmp_path / "noncanonical.json"
    noncanonical.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    with pytest.raises(AggregationProfileV5Error, match="not_canonical"):
        load_verified_aggregation_profile_v5(
            noncanonical,
            expected_artifact_sha256=hashlib.sha256(noncanonical.read_bytes()).hexdigest(),
        )

    linked = tmp_path / "linked.json"
    linked.symlink_to(path)
    with pytest.raises(AggregationProfileV5Error, match="not_regular"):
        load_verified_aggregation_profile_v5(linked, expected_artifact_sha256=artifact_sha256)


def test_document_aggregation_settings_are_optional_and_all_or_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "CARDRAG_DOCUMENT_AGGREGATION_PROFILE_FILE",
        "CARDRAG_DOCUMENT_AGGREGATION_PROFILE_ARTIFACT_SHA256",
    ):
        monkeypatch.delenv(name, raising=False)
    default = WorkerSettings.from_env()
    assert default.document_aggregation_profile_path is None
    assert default.document_aggregation_profile_artifact_sha256 is None

    absolute = tmp_path / "document-aggregation-profile.json"
    monkeypatch.setenv("CARDRAG_DOCUMENT_AGGREGATION_PROFILE_FILE", str(absolute))
    with pytest.raises(ValueError, match="all-or-nothing"):
        WorkerSettings.from_env()

    monkeypatch.setenv(
        "CARDRAG_DOCUMENT_AGGREGATION_PROFILE_ARTIFACT_SHA256",
        "a" * 64,
    )
    configured = WorkerSettings.from_env()
    assert configured.document_aggregation_profile_path == absolute
    assert configured.document_aggregation_profile_artifact_sha256 == "a" * 64

    monkeypatch.setenv("CARDRAG_DOCUMENT_AGGREGATION_PROFILE_FILE", "relative.json")
    with pytest.raises(ValueError, match="absolute path"):
        WorkerSettings.from_env()
