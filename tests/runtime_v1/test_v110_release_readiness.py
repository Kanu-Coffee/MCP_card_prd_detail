from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_release_requires_a_sealed_current_full_gold_report() -> None:
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
        "CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST: sha256:" + "a" * 64,
        "CARDRAG_CANDIDATE_MCP_IMAGE_DIGEST: sha256:" + "b" * 64,
    )
    for command in required_commands:
        assert command in workflow


def test_live_qwen_preflight_corrections_are_pinned_and_scoped() -> None:
    implementation = (ROOT / "apps/cardrag-worker/src/cardrag_worker/embedding_v5.py").read_text(
        encoding="utf-8"
    )

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
