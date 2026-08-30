from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from cardrag_core import (
    DocumentAggregationBootstrap,
    DocumentAggregationProfile,
    MaxChildAggregationDefinition,
    canonical_json_bytes,
    canonical_sha256,
)

import cardrag_worker.aggregation_profile_v5 as profile_module
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
        "byte_order": "little-endian",
        "corpus_inventory": {"sha256": "7" * 64, "size_bytes": 98_765},
        "corpus_row_count": 3,
        "embedding_dimension": 4096,
        "embedding_model": "qwen/qwen3-embedding-8b",
        "embedding_profile_id": profile.embedding_profile_id,
        "exact": True,
        "exact_row_corpus_sha256": profile.exact_row_corpus_sha256,
        "generation_id": profile.generation_id,
        "generation_manifest_sha256": profile.generation_manifest_sha256,
        "gold_sha256": profile.gold_sha256,
        "matrix_order": "row-major",
        "query_count": 300,
        "query_vector_matrix": {
            "sha256": "8" * 64,
            "size_bytes": 300 * 4096 * 4,
        },
        "runtime_document_aggregation_policy": "max_child",
        "runtime_document_aggregation_status": "candidate_default",
        "runtime_sealed_profile_sha256": None,
        "scalar_type": "float32",
        "schema_version": "cardrag.document-aggregation-score-artifact.v2",
        "score_count": 900,
        "score_matrix": {"sha256": "9" * 64, "size_bytes": 900 * 4},
        "scoring_contract": "cardrag.v5-exact-row-score.v1",
        "serving_database_sha256": "4" * 64,
        "source_commit": "5" * 40,
        "temporal_scope_policy": "gold-query.v1",
        "validation_profile": "release_grade",
        "vector_sidecar_sha256": "6" * 64,
    }
    definition = profile.aggregation_definition.model_dump(mode="json")
    artifact: dict[str, object] = {
        "artifact_bindings": {
            "corpus_inventory_sha256": "7" * 64,
            "corpus_inventory_size_bytes": 98_765,
            "generation_manifest_sha256": profile.generation_manifest_sha256,
            "gold_sha256": profile.gold_sha256,
            "query_vector_matrix_sha256": "8" * 64,
            "query_vector_matrix_size_bytes": 300 * 4096 * 4,
            "score_artifact_manifest_sha256": canonical_sha256(score_manifest),
            "score_artifact_sha256": profile.score_artifact_sha256,
            "score_artifact_size_bytes": 123_456,
            "score_matrix_sha256": "9" * 64,
            "score_matrix_size_bytes": 900 * 4,
        },
        "bootstrap": profile.bootstrap.model_dump(mode="json"),
        "comparisons": {"max_child_vs_top3_mean": {}},
        "coverage": {
            "all_queries_exact": True,
            "approximate": False,
            "corpus_row_count": 3,
            "maximum_active_contracts": 100,
            "minimum_active_contracts": 100,
            "query_count": 300,
            "score_count": 900,
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


def test_profile_reader_rejects_same_inode_growth_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "race.json"
    payload = b"{}\n"
    target.write_bytes(payload)
    original_inode = target.stat().st_ino
    original_open = profile_module.os.open
    raced = False
    read_calls = 0

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

    monkeypatch.setattr(profile_module, "MAX_PROFILE_ARTIFACT_BYTES", len(payload))
    monkeypatch.setattr(profile_module.os, "open", racing_open)
    monkeypatch.setattr(profile_module.os, "read", forbidden_read)
    with pytest.raises(AggregationProfileV5Error, match="profile_artifact_size_invalid"):
        profile_module._read_regular(target)

    assert raced
    assert target.stat().st_ino == original_inode
    assert read_calls == 0


@pytest.mark.skipif(not hasattr(os, "O_NONBLOCK"), reason="requires POSIX nonblocking open")
def test_profile_reader_nonblocking_open_rejects_fifo_swap_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "profile.json"
    target.write_bytes(b"{}\n")
    original_open = profile_module.os.open
    raced = False
    observed_flags = 0
    read_calls = 0

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

    def forbidden_read(*_args: object, **_kwargs: object) -> bytes:
        nonlocal read_calls
        read_calls += 1
        raise AssertionError("FIFO must be rejected before any descriptor read")

    monkeypatch.setattr(profile_module.os, "open", racing_open)
    monkeypatch.setattr(profile_module.os, "read", forbidden_read)

    with pytest.raises(AggregationProfileV5Error, match="profile_artifact_not_regular"):
        profile_module._read_regular(target)

    assert raced
    assert observed_flags & os.O_NONBLOCK
    assert read_calls == 0


def test_profile_reader_rejects_path_replacement_during_descriptor_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "mutable.json"
    replacement = tmp_path / "replacement.json"
    payload = b"{}\n"
    target.write_bytes(payload)
    replacement.write_bytes(payload)
    original_read = profile_module.os.read
    replaced = False

    def racing_read(descriptor: int, count: int) -> bytes:
        nonlocal replaced
        block = original_read(descriptor, count)
        if block and not replaced:
            replaced = True
            replacement.replace(target)
        return block

    monkeypatch.setattr(profile_module.os, "read", racing_read)
    with pytest.raises(AggregationProfileV5Error, match="profile_artifact_changed_during_read"):
        profile_module._read_regular(target)
    assert replaced


def test_profile_reader_accepts_stable_two_link_snapshot(tmp_path: Path) -> None:
    target = tmp_path / "artifact.json"
    linked = tmp_path / "artifact-hardlink.json"
    payload = b"{}\n"
    target.write_bytes(payload)
    os.link(target, linked)
    assert target.stat().st_nlink == linked.stat().st_nlink == 2
    assert profile_module._read_regular(linked) == payload


def _refresh_score_manifest_binding(payload: dict[str, object]) -> None:
    payload["artifact_bindings"]["score_artifact_manifest_sha256"] = canonical_sha256(
        payload["score_artifact_manifest"]
    )


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
        (
            lambda payload: payload["score_artifact_manifest"].update(validation_profile="fixture_only"),
            "score_manifest",
        ),
        (
            lambda payload: payload["artifact_bindings"].update(score_matrix_sha256="a" * 64),
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


@pytest.mark.parametrize(
    "mutation",
    (
        lambda payload: payload["score_artifact_manifest"].update(score_count=901),
        lambda payload: payload["score_artifact_manifest"].update(validation_profile="fixture_only"),
        lambda payload: (
            payload["score_artifact_manifest"]["score_matrix"].update(size_bytes=3_596),
            payload["artifact_bindings"].update(score_matrix_size_bytes=3_596),
        ),
        lambda payload: payload["score_artifact_manifest"]["corpus_inventory"].update(sha256="a" * 64),
    ),
)
def test_load_verified_aggregation_profile_rejects_rehashed_compact_score_drift(
    tmp_path: Path,
    mutation: object,
) -> None:
    payload, _profile = _profile_artifact()
    assert callable(mutation)
    mutation(payload)
    _refresh_score_manifest_binding(payload)
    path = tmp_path / "document-aggregation-profile.json"
    artifact_sha256 = _write_artifact(path, payload)

    with pytest.raises(AggregationProfileV5Error, match="score_manifest"):
        load_verified_aggregation_profile_v5(path, expected_artifact_sha256=artifact_sha256)


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
