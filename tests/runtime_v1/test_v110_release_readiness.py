from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_release_seals_release_qualification_instead_of_runtime_receipts() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    validate_job = workflow.split("  validate:\n", 1)[1].split("  strict-filesystem-scan:\n", 1)[0]
    for contract in (
        'qualification_dir="release-evidence/v${version}"',
        'qualification="$qualification_dir/release-qualification.json"',
        'test "$(find "$qualification_dir" -mindepth 1 | wc -l)" -eq 1',
        '"cardrag.release-qualification-validation.v1"',
        "name: release-qualification-${{ steps.version.outputs.version }}",
        'test "$version" = "1.0.32"',
        'test "$(git cat-file -t "refs/tags/v$version")" = tag',
        'git ls-remote origin "refs/tags/v${version}^{}"',
    ):
        assert contract in validate_job
    for contract in (
        'test "$(git cat-file -t "refs/tags/v$VERSION")" = tag',
        'git ls-remote origin "refs/tags/v${VERSION}^{}"',
    ):
        assert contract in workflow
    for runtime_receipt_reference in (
        "release-readiness-receipt.json",
        "candidate-acceptance-receipt.json",
        "cardrag_mcp.candidate_smoke",
        "cardrag_core.release_readiness",
        "serving-generation-manifest.json",
        "rollback-ledger.json",
        "native-cache-audit.json",
        "mcp-smoke.json",
    ):
        assert runtime_receipt_reference not in validate_job

    notes_region = workflow[workflow.index("notes = [") :]
    assert "수행 검증" in notes_region
    assert "명시적 미수행" in notes_region
    assert "참고(출처가 다른 운영 증거)" in notes_region


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
