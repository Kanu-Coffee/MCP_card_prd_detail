import json
import math
from pathlib import Path

from cardrag_core import canonical_json_bytes, canonical_sha256

ROOT = Path(__file__).resolve().parents[2]


def test_v110_release_requires_a_sealed_full_gold_report() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    required_contracts = (
        "acceptance_report_sha256:",
        'evidence_dir="release-evidence/v${version}"',
        'acceptance_report="$evidence_dir/gold-evaluation-report.json"',
        '--gold "$evidence_dir/gold.jsonl"',
        '--blind-evaluation "$evidence_dir/blind-evaluation.jsonl"',
        '--validate-report "$acceptance_report"',
        '--expected-report-sha256 "$ACCEPTANCE_REPORT_SHA256"',
        '--generation-manifest-dir "$evidence_dir/generation-manifests"',
        "--bootstrap-samples 2000",
        "--bootstrap-seed 1010",
        'validator_args+=(--run "$lane=$evidence_dir/$lane.jsonl")',
        '.venv/bin/python -m cardrag_mcp.evaluation "${validator_args[@]}"',
        'test "$(git cat-file -t "refs/tags/v$version")" = tag',
        'test "$(git cat-file -t "refs/tags/v$VERSION")" = tag',
        'git ls-remote origin "refs/tags/v${version}^{}"',
        'git ls-remote origin "refs/tags/v${VERSION}^{}"',
    )
    for contract in required_contracts:
        assert contract in workflow


def test_ci_runs_release_security_and_image_scans() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    required_commands = (
        "actionlint -no-color",
        "shellcheck",
        "gitleaks detect --source . --no-banner --redact --exit-code 1",
        "trivy fs",
        "trivy image",
        "docker build --target worker",
        "docker build --target mcp",
    )
    for command in required_commands:
        assert command in workflow


def test_live_qwen_preflight_corrections_are_pinned_and_scoped() -> None:
    implementation = (ROOT / "apps/cardrag-worker/src/cardrag_worker/embedding_v5.py").read_text(
        encoding="utf-8"
    )
    acceptance = (ROOT / "docs/V1_0_10_ACCEPTANCE.md").read_text(encoding="utf-8")
    migration = (ROOT / "docs/V1_0_10_MIGRATION.md").read_text(encoding="utf-8")

    required_implementation_contracts = (
        "actual_model.casefold() != self.profile.model.casefold()",
        'maximum_tokens = row.get("max_prompt_tokens")',
        'maximum_tokens = row.get("context_length")',
        '"require_parameters": False',
        '"only": [self.profile.provider_id]',
        '"allow_fallbacks": False',
    )
    for contract in required_implementation_contracts:
        assert contract in implementation

    for document in (acceptance, migration):
        assert "deepinfra" in document and "nebius" in document
        assert "max_prompt_tokens=null" in document
        assert "context_length" in document
        assert "require_parameters=false" in document
        assert "full" in document and "gold" in document

    assert "실제 4개 카드사 full candidate run" in acceptance
    assert "gold 평가가 통과했다는 증거는 아닙니다" in acceptance


def test_sealed_qwen_preflight_evidence_is_canonical_and_self_bound() -> None:
    path = ROOT / "release-evidence/v1.0.10/qwen-provider-preflight.json"
    assert path.is_file() and not path.is_symlink()
    raw = path.read_bytes()
    payload = json.loads(raw)
    assert isinstance(payload, dict)
    assert raw == canonical_json_bytes(payload) + b"\n"

    expected_keys = {
        "cross_provider",
        "dimension",
        "evidence_sha256",
        "model",
        "observed_at",
        "providers",
        "schema_version",
        "tokenizer_revision",
        "tokenizer_sha256",
    }
    assert set(payload) == expected_keys
    claimed_sha256 = payload["evidence_sha256"]
    hash_payload = {key: value for key, value in payload.items() if key != "evidence_sha256"}
    assert claimed_sha256 == "b52c81be473d16b781aad5c4310389d51f9bde7930b7a197224a8979cc0ea666"
    assert canonical_sha256(hash_payload) == claimed_sha256

    assert payload["schema_version"] == "cardrag.qwen-provider-preflight-evidence.v1"
    assert payload["model"] == "qwen/qwen3-embedding-8b"
    assert payload["dimension"] == 4096
    assert payload["observed_at"] == "2026-08-29T06:22:03.255807+00:00"
    assert payload["tokenizer_revision"] == "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af"
    assert payload["tokenizer_sha256"] == ("83cdf8c3a34f68862319cb1810ee7b1e2c0a44e0864ae930194ddb76bb7feb8d")

    providers = payload["providers"]
    assert [provider["provider_id"] for provider in providers] == ["deepinfra", "nebius"]
    assert [provider["maximum_tokens"] for provider in providers] == [32768, 32000]
    assert all(provider["sample_count"] == 24 for provider in providers)
    assert all(
        0.999 <= provider["minimum_repeat_cosine"] <= provider["mean_repeat_cosine"] <= 1.0
        for provider in providers
    )
    cross_provider = payload["cross_provider"]
    assert cross_provider["sample_count"] == 24
    assert 0.999 <= cross_provider["minimum_cosine"] <= cross_provider["mean_cosine"] <= 1.0

    lowered = raw.lower()
    for prohibited in (b"authorization", b"api_key", b"bearer ", b"https://", b"secret"):
        assert prohibited not in lowered


def test_sealed_qwen_reranker_preflight_is_canonical_self_bound_and_scoped() -> None:
    path = ROOT / "release-evidence/v1.0.10/qwen-reranker-provider-preflight.json"
    assert path.is_file() and not path.is_symlink()
    raw = path.read_bytes()
    payload = json.loads(raw)
    assert isinstance(payload, dict)
    assert raw == canonical_json_bytes(payload) + b"\n"

    expected_keys = {
        "candidate_ranking_executed",
        "endpoint_metadata",
        "evidence_sha256",
        "gold_evaluation_executed",
        "request_contract",
        "response_contract",
        "schema_version",
        "sensitive_material_included",
    }
    assert set(payload) == expected_keys
    claimed_sha256 = payload["evidence_sha256"]
    hash_payload = {key: value for key, value in payload.items() if key != "evidence_sha256"}
    assert claimed_sha256 == "80a0602e7acaaa6e4d8d718570d3c588b72f45243f8331389befd19f44304b13"
    assert canonical_sha256(hash_payload) == claimed_sha256

    assert payload["schema_version"] == ("cardrag.qwen-reranker-provider-preflight-evidence.v1")
    assert payload["candidate_ranking_executed"] is False
    assert payload["gold_evaluation_executed"] is False
    assert payload["sensitive_material_included"] is False
    assert payload["endpoint_metadata"] == {
        "context_length": 40960,
        "max_prompt_tokens": None,
        "model": "qwen/qwen3-reranker-8b",
        "provider_name": "Fireworks",
        "provider_tags": ["fireworks"],
        "quantization": "unknown",
    }
    assert payload["request_contract"] == {
        "endpoint_path": "/api/v1/rerank",
        "model": "qwen/qwen3-reranker-8b",
        "provider": {
            "allow_fallbacks": False,
            "only": ["fireworks"],
            "order": ["fireworks"],
            "require_parameters": False,
        },
        "requested_document_count": 3,
        "top_n": 3,
    }
    response = payload["response_contract"]
    assert response["http_status"] == 200
    assert response["model"] == "accounts/fireworks/models/qwen3-reranker-8b"
    assert response["provider"] == "Fireworks"
    assert response["result_count"] == 3
    assert response["indices"] == [0, 1, 2]
    assert response["unique_indices"] is True
    assert response["finite_scores"] is True
    assert all(math.isfinite(score) for score in response["relevance_scores"])
    assert response["relevance_scores"] == sorted(response["relevance_scores"], reverse=True)

    implementation = (ROOT / "apps/cardrag-mcp/src/cardrag_mcp/reranker.py").read_text(encoding="utf-8")
    for contract in (
        '"rerank"',
        '"order": [RERANKER_PROVIDER_ID]',
        '"only": [RERANKER_PROVIDER_ID]',
        '"allow_fallbacks": False',
        '"require_parameters": False',
        'RERANKER_CANONICAL_RESPONSE_MODEL = "accounts/fireworks/models/qwen3-reranker-8b"',
    ):
        assert contract in implementation

    lowered = raw.lower()
    for prohibited in (
        b"authorization",
        b"api_key",
        b"base_url",
        b"bearer ",
        b"https://",
        b"password",
        b"query_text",
        b"document_text",
    ):
        assert prohibited not in lowered


def test_sealed_v109_cache_reuse_evidence_is_canonical_and_self_bound() -> None:
    path = ROOT / "release-evidence/v1.0.10/v109-cache-reuse-audit.json"
    assert path.is_file() and not path.is_symlink()
    raw = path.read_bytes()
    payload = json.loads(raw)
    assert isinstance(payload, dict)
    assert raw == canonical_json_bytes(payload) + b"\n"

    expected_keys = {
        "cache_reuse",
        "candidate_v109",
        "candidate_v110",
        "evidence_sha256",
        "inventory",
        "observed_at",
        "schema_version",
        "sensitive_material_included",
    }
    assert set(payload) == expected_keys
    claimed_sha256 = payload["evidence_sha256"]
    hash_payload = {key: value for key, value in payload.items() if key != "evidence_sha256"}
    assert claimed_sha256 == "f62e60824c7c9a5ef96d41f701616c4f9d76167d642c9c551aaafce82ec5c56f"
    assert canonical_sha256(hash_payload) == claimed_sha256

    assert payload["schema_version"] == "cardrag.v109-cache-reuse-audit.v1"
    assert payload["observed_at"] == "2026-08-29T06:31:52.288216+00:00"
    assert payload["sensitive_material_included"] is False
    assert payload["candidate_v109"]["counts"] == {
        "chunks": 4175,
        "documents": 747,
        "ocr_objects": 714,
        "pdf_objects": 652,
    }
    assert all(payload["candidate_v109"]["bindings"].values())
    assert payload["candidate_v110"] == {"pointer_status": 404}

    reuse = payload["cache_reuse"]
    assert reuse["control_records"] == {
        "manifest_ready_reuse_output_bindings_verified": 715,
        "missing": 0,
        "referenced": 715,
        "verified": 715,
    }
    assert reuse["ocr_cas_objects"] == {
        "full_get_status_200": 714,
        "full_sha256_size_verified": 714,
        "unique": 714,
    }
    assert reuse["pdf_cas_objects"]["manifest_size_verified"] == 652
    assert reuse["pdf_cas_objects"]["range_get_status_206"] == 652
    assert reuse["pdf_cas_objects"]["unique"] == 652
    assert len(reuse["pdf_cas_objects"]["full_sha256_samples"]) == 1
    sample = reuse["pdf_cas_objects"]["full_sha256_samples"][0]
    assert sample["verified"] is True
    assert sample["actual_sha256"] == sample["expected_sha256"]
    assert sample["actual_size_bytes"] == sample["expected_size_bytes"]

    lowered = raw.lower()
    for prohibited in (
        b"authorization",
        b"api_key",
        b"base_url",
        b"bearer ",
        b"https://",
        b"password",
        b"remote_path",
        b"username",
    ):
        assert prohibited not in lowered


def test_sealed_v109_kb_regression_baseline_is_canonical_and_self_bound() -> None:
    from cardrag_worker.legacy_v4_audit import (
        load_audit_artifact,
        validate_audit_artifact,
        validate_historical_artifact,
    )

    evidence = ROOT / "release-evidence/v1.0.10"
    historical = load_audit_artifact(evidence / "v109-kb-real-regression-baseline.json")
    sealed = load_audit_artifact(evidence / "v109-kb-v4-structure-reaudit.json")

    validate_historical_artifact(historical)
    validate_audit_artifact(sealed, require_release_binding=True)
    assert historical["provenance"]["binding"] == "observation_only"
    assert historical["provenance"]["source_artifact_sha256"] is None
    assert sealed["comparison_to_historical_run"]["match"] is False
    assert sealed["comparison_to_historical_run"]["mismatched_metrics"] == ["titleless_continuations"]
