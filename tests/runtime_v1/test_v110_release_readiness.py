from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_release_seals_operational_readiness_instead_of_research_gold() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    validate_job = workflow.split("  validate:\n", 1)[1].split("  strict-filesystem-scan:\n", 1)[0]

    required_contracts = (
        'evidence_dir="release-evidence/v${version}"',
        'candidate_acceptance="$evidence_dir/candidate-acceptance-receipt.json"',
        '"schema": "cardrag.release-readiness-evidence.v1"',
        '"maximum_file_bytes": MAX_FILE_BYTES',
        "name: readiness-evidence-${{ steps.version.outputs.version }}",
        ".venv/bin/python -m cardrag_mcp.candidate_smoke \\\n",
        'test "$version" = "1.0.32"',
        'test "$(git cat-file -t "refs/tags/v$version")" = tag',
        'test "$(git cat-file -t "refs/tags/v$VERSION")" = tag',
        'git ls-remote origin "refs/tags/v${version}^{}"',
        'git ls-remote origin "refs/tags/v${VERSION}^{}"',
        "release-readiness-manifest.json",
    )
    for contract in required_contracts:
        assert contract in workflow
    for research_reference in (
        "gold.jsonl",
        "blind-evaluation",
        "v109_baseline",
        "cardrag_mcp.evaluation",
        "cardrag_mcp.gold_capture",
        "acceptance_report_sha256",
    ):
        assert research_reference not in validate_job

    notes_region = workflow[workflow.index("notes = [") :]
    assert "수행 검증" in notes_region
    assert "미수행" in notes_region
    assert "선택적 후속 검증" in notes_region


def test_release_preflight_rejects_conflicting_existing_dockerhub_tags() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    preflight_job = workflow.split("  registry-preflight:\n", 1)[1].split("  publish:\n", 1)[0]
    publish_job = workflow.split("  publish:\n", 1)[1].split("  release:\n", 1)[0]

    for contract in (
        "https://hub.docker.com/v2/repositories/${IMAGE_NAME}/tags/${tag}",
        "Docker Hub tag lookup failed for ${reference}: HTTP ${status}",
        "immutable alias is not the accepted ${role} digest",
        "404) continue ;;",
    ):
        assert contract in preflight_job
    for contract in (
        "https://hub.docker.com/v2/repositories/${IMAGE_NAME}/tags/${tag}",
        "Docker Hub tag lookup failed for ${reference}: HTTP ${status}",
    ):
        assert contract in publish_job


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
