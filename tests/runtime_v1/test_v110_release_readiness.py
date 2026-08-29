import hashlib
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


def test_v109_prefix_only_cache_compatibility_evidence_is_live_canonical_and_self_bound() -> None:
    path = ROOT / "release-evidence/v1.0.10/v109-prefix-only-cache-compatibility.json"
    assert path.is_file() and not path.is_symlink()
    raw = path.read_bytes()
    payload = json.loads(raw)
    assert isinstance(payload, dict)
    assert raw == canonical_json_bytes(payload) + b"\n"

    expected_keys = {
        "artifacts",
        "candidate_revision",
        "compatibility_boundary",
        "evidence_sha256",
        "method",
        "observed_at",
        "ocr_contract",
        "run_id_sha256",
        "schema_version",
        "sensitive_material_included",
        "v109_worker_image",
    }
    assert set(payload) == expected_keys
    claimed_sha256 = payload["evidence_sha256"]
    hash_payload = {key: value for key, value in payload.items() if key != "evidence_sha256"}
    assert claimed_sha256 == "a7eec6534cd30a5dd284bf03b7e41216b29113f19b71839e16a8e4af9b38d5cb"
    assert canonical_sha256(hash_payload) == claimed_sha256

    assert payload["schema_version"] == "cardrag.v109-prefix-only-cache-compatibility.v1"
    assert payload["observed_at"] == "2026-08-29T15:07:10.351512+00:00"
    assert payload["candidate_revision"] == "531e70e33d73cbaafb1955c712e8f2cc9547614f"
    assert payload["run_id_sha256"] == ("6159744ade772946d17eabbb40a05b9ed0c47123c93284a0e477f970dc03a809")
    assert payload["sensitive_material_included"] is False
    assert payload["method"] == {
        "candidate_volume_access": "read-only",
        "network_operations": ["GET"],
        "raw_response_included": False,
        "remote_mutations": 0,
    }
    assert payload["v109_worker_image"] == {
        "image_digest": "sha256:a3be8e1b74cb310c3f0d00496db440a00f31d65a0851337205e627153ea103c8",
        "revision": "fee8f65a9fda7ae0c286ac92cf4c3f55c1a6f113",
        "version": "1.0.9",
    }
    assert payload["ocr_contract"] == {
        "contract_sha256": "782fb558fd0102a01406b00009c36a5c1c1a7ce851fe388425c0003b4fff536a",
        "model": "gpt-5.6-sol",
        "processor_version": "cardrag-worker/1.0.4",
        "prompt_sha256": "1448d7e530d4f8412102c67cd44dc9c9cdab9e3aa165eddb88b3a980a245b946",
        "prompt_version": "cardrag-ocr.ko.v2",
        "provider": "codex-exec",
    }

    boundary = payload["compatibility_boundary"]
    assert boundary == {
        "cache_consumer": "verify_ocr_bytes",
        "cache_consumer_calls_target_checkpoint_validator": False,
        "cache_namespace_bump_required": False,
        "common_core_ocr_source_sha256": ("00bc8379e1aae489e0240ffbc275caed15b4da86cecab7d34ddc3e42bb67c681"),
        "provider_checkpoint_validator": "_validate_and_normalize_target_page_values",
        "v109_cache_consumer_accepts_prefix_only": True,
        "v109_target_checkpoint_accepts_prefix_only": False,
        "v110_target_checkpoint_accepts_prefix_only": True,
    }
    core_source = ROOT / "packages/cardrag-core/src/cardrag_core/ocr.py"
    assert hashlib.sha256(core_source.read_bytes()).hexdigest() == boundary["common_core_ocr_source_sha256"]

    expected = {
        "doc_5fb7555579ad01c3e66f2777ddfbff7a47a33459656beeeeeeea230463fff1bb": {
            "manifest_sha256": "d815b1901291d140e2dbcbc04b8ac89016fd5791006e693d77a2ba96444f6d9b",
            "object_sha256": "16b3af55590dabc74d2d7da565840db7bc6b20b3f06db525d2b8335fdc7574a8",
            "object_size_bytes": 26023,
            "page_count": 24,
            "page_number": 22,
            "page_sha256": "ff88d687ed53d474bed4bff3f8567e63caa395f4c365deedaab9cb6ee09faa50",
            "ready_sha256": "87c22934d637f6fdac2cab38456a7893b735d3906e7b8287c88832660b99b274",
            "reuse_key": "f767775733b750061bb9f0fc53aef5193d15c49ea4f336d0be7b53f01fa13ecc",
        },
        "doc_69bd1e3d4f8686076ae0dfa27c0dc0e5cbff2eda3539be0432694285a97d6fd2": {
            "manifest_sha256": "6e763ab66dfb0495c0ba9cf747b8f51387f72095c67502c402414154d92d5443",
            "object_sha256": "deae37bb04dfc909bacee1a9fe1886b404e001d0b81423d5c884b5555d2910b7",
            "object_size_bytes": 21325,
            "page_count": 20,
            "page_number": 18,
            "page_sha256": "07d590ca221d1b38bf30daeaf0dd8883a8c68f17c5eeb490da0158482a6b9982",
            "ready_sha256": "f4affc3b158b71d32646bd8f2d77f20cada1eb705711265209f8f9b3b187109d",
            "reuse_key": "39836e8749c738227463ac730bc0bb15d3785ce836683aa0122dc5533ef18e17",
        },
    }
    artifacts = payload["artifacts"]
    assert [artifact["document_id"] for artifact in artifacts] == list(expected)
    for artifact in artifacts:
        sealed = expected[artifact["document_id"]]
        reuse_key = sealed["reuse_key"]
        object_sha256 = sealed["object_sha256"]
        cache_root = f"v1/ocr-cache/native/{reuse_key[:2]}/{reuse_key}"
        assert artifact["get_status"] == {"manifest": 200, "object": 200, "ready": 200}
        assert all(artifact["bindings"].values())
        assert artifact["reuse_key"] == reuse_key
        assert artifact["page_count"] == sealed["page_count"]
        assert artifact["object_size_bytes"] == sealed["object_size_bytes"]
        assert artifact["paths"] == {
            "manifest": f"{cache_root}/manifest.json",
            "object": f"v1/objects/sha256/{object_sha256[:2]}/{object_sha256}",
            "ready": f"{cache_root}/READY.json",
        }
        assert artifact["hashes"]["manifest_sha256"] == sealed["manifest_sha256"]
        assert artifact["hashes"]["object_sha256"] == object_sha256
        assert artifact["hashes"]["ready_sha256"] == sealed["ready_sha256"]
        assert artifact["hashes"]["sparse_page_sha256"] == sealed["page_sha256"]
        assert artifact["hashes"]["sparse_body_sha256"] == (
            "f9a01ad3946b781e2dfbf5cde288057b2609f889ddb5670269023cb157882993"
        )
        assert artifact["sparse_page"] == {
            "page_number": sealed["page_number"],
            "prefix_only": True,
            "visible_character_count": 0,
        }
        assert artifact["validation"] == {
            "v109_cache_bytes_accept": True,
            "v109_cache_control_accept": True,
            "v109_target_checkpoint_accept": False,
            "v110_target_checkpoint_accept": True,
        }

    lowered = raw.lower()
    for prohibited in (
        b"authorization",
        b"api_key",
        b"base_url",
        b"bearer ",
        b"credential",
        b"http://",
        b"https://",
        b"password",
        b"username",
    ):
        assert prohibited not in lowered


def test_sealed_v109_kb_regression_baseline_is_canonical_and_self_bound() -> None:
    from cardrag_worker.legacy_v4_audit import (
        load_audit_artifact,
        validate_audit_artifact,
        validate_historical_artifact,
        validate_historical_source_artifact,
    )

    evidence = ROOT / "release-evidence/v1.0.10"
    historical = load_audit_artifact(evidence / "v109-kb-real-regression-baseline.json")
    historical_source = load_audit_artifact(evidence / "v109-structure-audit-execution.json")
    sealed = load_audit_artifact(evidence / "v109-kb-v4-structure-reaudit.json")

    validate_historical_source_artifact(historical_source)
    validate_historical_artifact(
        historical,
        require_source_binding=True,
        source_artifact=historical_source,
    )
    validate_audit_artifact(sealed, require_release_binding=True)
    assert historical["provenance"]["binding"] == "execution_record_hash_bound"
    assert historical["provenance"]["source_artifact_sha256"] == (
        "260b8e5302f368e6f37b2e2556b0acfdcd4ee24b4b17c159bcb6f02bc1f7b1fe"
    )
    assert sealed["comparison_to_historical_run"]["match"] is False
    assert sealed["comparison_to_historical_run"]["mismatched_metrics"] == ["titleless_continuations"]
