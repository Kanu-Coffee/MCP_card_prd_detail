import asyncio
import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

from cardrag_core.candidate_acceptance import MCP_REQUIRED_ARGUMENTS, MCP_TOOLS
from cardrag_mcp.app import build_mcp_server
from cardrag_mcp.config import Settings

ROOT = Path(__file__).resolve().parents[2]
AUTH_VALUE = "test-discovery-bearer-00000000000000"


def _readiness_publish_verifier(workflow: str) -> str:
    step = workflow.split(
        "      - name: Revalidate complete release readiness evidence before registry mutation\n", 1
    )[1].split("\n      - name: Publish only the receipt-bound", 1)[0]
    embedded = step.split("            \"$GITHUB_SHA\" <<'PY'\n", 1)[1].rsplit("\n          PY", 1)[0]
    return "\n".join(line[10:] for line in embedded.splitlines()) + "\n"


def _run_readiness_verifier(
    workflow: str,
    bundle: Path,
    manifest_sha256: str,
    source_commit: str,
    tag_commit: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - interpreter and embedded workflow code are controlled
        (sys.executable, "-", str(bundle), manifest_sha256, source_commit, tag_commit),
        input=_readiness_publish_verifier(workflow),
        check=False,
        capture_output=True,
        text=True,
    )


def _local_image_identity_helper(workflow: str) -> str:
    strict_job = workflow.split("  strict-image-scan:\n", 1)[1].split("  registry-preflight:\n", 1)[0]
    marker = "          validate_local_image_identity() {\n"
    function_body = strict_job.split(marker, 1)[1].split("\n          }\n", 1)[0]
    indented = marker + function_body + "\n          }\n"
    return "\n".join(line[10:] for line in indented.splitlines()) + "\n"


def _run_local_image_identity_helper(
    workflow: str,
    inspect_payload: object,
    *,
    repository: str,
    index_digest: str,
    config_digest: str,
) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    assert bash is not None
    script = (
        _local_image_identity_helper(workflow)
        + r"""
docker() {
  test "$1" = "image"
  test "$2" = "inspect"
  test "$3" = "fixture:tag"
  cat
}
validate_local_image_identity "fixture:tag" "$1" "$2" "$3"
"""
    )
    return subprocess.run(  # noqa: S603 - interpreter and embedded workflow code are controlled
        (bash, "-c", script, "cardrag-local-identity-test", repository, index_digest, config_digest),
        input=json.dumps(inspect_payload),
        check=False,
        capture_output=True,
        text=True,
    )


def _public_package_is_valid(
    tmp_path: Path,
    package: object,
    *,
    owner: str = "Kanu-Coffee",
    owner_type: str = "User",
    package_name: str = "mcp-card-prd-detail-candidate",
) -> bool:
    jq = shutil.which("jq")
    assert jq is not None
    package_path = tmp_path / "package.json"
    package_path.write_text(json.dumps(package), encoding="utf-8")
    result = subprocess.run(  # noqa: S603 - executable and filter are repository-controlled
        (
            jq,
            "-e",
            "--arg",
            "owner",
            owner,
            "--arg",
            "owner_type",
            owner_type,
            "--arg",
            "package_name",
            package_name,
            "-f",
            str(ROOT / ".github/actions/verify-public-candidate-package/validate-package.jq"),
            str(package_path),
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _raw_evidence_is_gitleaks_clean(tmp_path: Path, payloads: dict[str, object]) -> bool:
    gitleaks = shutil.which("gitleaks")
    assert gitleaks is not None
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir(parents=True)
    for name, payload in payloads.items():
        (evidence_dir / name).write_text(json.dumps(payload), encoding="utf-8")
    result = subprocess.run(  # noqa: S603 - installed scanner and repository config are controlled
        (
            gitleaks,
            "detect",
            "--source",
            str(evidence_dir),
            "--no-git",
            "--config",
            str(ROOT / ".gitleaks.toml"),
            "--no-banner",
            "--redact",
            "--exit-code",
            "1",
            "--report-format",
            "json",
            "--report-path",
            str(tmp_path / "gitleaks-report.json"),
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def test_public_registry_jobs_use_environment_only_to_scope_secrets() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    validate_job = workflow.split("  validate:\n", 1)[1].split("  strict-filesystem-scan:\n", 1)[0]
    preflight_job = workflow.split("  registry-preflight:\n", 1)[1].split("  publish:\n", 1)[0]
    publish_job = workflow.split("  publish:\n", 1)[1].split("  release:\n", 1)[0]

    assert workflow.count("environment: dockerhub-public") == 2
    assert "environment: dockerhub-public" in preflight_job
    assert "environment: dockerhub-public" in publish_job
    assert "actions: read" in preflight_job
    assert "actions: read" in publish_job
    assert 'test "$GITHUB_ACTOR" = "$GITHUB_REPOSITORY_OWNER"' not in validate_job
    assert 'test "$GITHUB_TRIGGERING_ACTOR" = "$GITHUB_REPOSITORY_OWNER"' not in validate_job
    assert "verify-public-release-environment" not in workflow
    assert "single-maintainer" not in workflow
    assert "confirmation:" not in workflow
    assert "inputs.confirmation" not in workflow
    assert "RELEASE_CONFIRMATION" not in workflow
    assert "PUBLISH-v" not in workflow
    assert "Independently approved" not in workflow
    assert not (ROOT / ".github/actions/verify-public-release-environment").exists()
    assert workflow.count("${{ secrets.DOCKERHUB_USERNAME }}") == 2
    assert workflow.count("${{ secrets.DOCKERHUB_TOKEN }}") == 2
    assert "${{ secrets.DOCKERHUB_USERNAME }}" not in validate_job
    assert "${{ secrets.DOCKERHUB_TOKEN }}" not in validate_job


def test_public_candidate_package_filter_requires_exact_public_metadata(
    tmp_path: Path,
) -> None:
    candidate: dict[str, object] = {
        "id": 1,
        "name": "mcp-card-prd-detail-candidate",
        "package_type": "container",
        "visibility": "public",
        "owner": {"login": "Kanu-Coffee", "type": "User"},
    }
    assert _public_package_is_valid(tmp_path, candidate)

    linked_candidate = copy.deepcopy(candidate)
    linked_candidate["repository"] = {"full_name": "Kanu-Coffee/another-repository"}
    assert _public_package_is_valid(tmp_path, linked_candidate)

    mutations: list[object] = [None, [], [candidate], {"message": "not a package"}]
    for path, value in (
        (("id",), "1"),
        (("name",), "another-package"),
        (("visibility",), "private"),
        (("package_type",), "npm"),
        (("owner", "login"), "another-user"),
        (("owner", "type"), "Organization"),
    ):
        changed = copy.deepcopy(candidate)
        if len(path) == 1:
            changed[path[0]] = value
        else:
            changed[path[0]][path[1]] = value  # type: ignore[index]
        mutations.append(changed)

    assert all(not _public_package_is_valid(tmp_path, package) for package in mutations)


def test_release_scans_and_publishes_only_the_receipt_bound_oci_digests() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    strict_job = workflow.split("  strict-image-scan:\n", 1)[1].split("  registry-preflight:\n", 1)[0]
    preflight_job = workflow.split("  registry-preflight:\n", 1)[1].split("  publish:\n", 1)[0]
    publish_job = workflow.split("  publish:\n", 1)[1].split("  release:\n", 1)[0]

    assert "trivy_0.74.0_Linux-64bit.tar.gz" in strict_job
    assert "2ae6fe3ee734b7fdf11335663e18c75ea12dccc76062f09f164a3b0f8be4371a" in strict_job
    assert "sha256sum --check --strict" in strict_job
    assert "matrix:\n        target: [worker, mcp]" in strict_job
    assert "CANDIDATE_IMAGE_REPOSITORY: ${{ vars.CARDRAG_CANDIDATE_IMAGE_REPOSITORY" in workflow
    assert "packages: read" in strict_job
    assert strict_job.count("uses: ./.github/actions/verify-public-candidate-package") == 1
    assert "/orgs/Kanu-Coffee/packages/" not in strict_job
    assert "registry: ghcr.io" not in strict_job
    assert "Authenticate read-only to the private candidate registry" not in strict_job
    assert 'anonymous_docker_config="$RUNNER_TEMP/cardrag-anonymous-ghcr-docker-config"' in (strict_job)
    assert "printf '{\"auths\":{}}\\n'" in strict_job
    assert 'export DOCKER_CONFIG="$anonymous_docker_config"' in strict_job
    assert "GH_TOKEN: ${{ github.token }}" in strict_job
    assert 'image="${CANDIDATE_IMAGE_REPOSITORY}@${digest}"' in strict_job
    assert 'test "$observed_digest" = "$digest"' in strict_job
    assert "validate-candidate-oci-index.jq" in strict_job
    assert "validate-candidate-platform-manifest.jq" in strict_job
    assert "validate-candidate-attestation-manifest.jq" in strict_job
    assert "validate-candidate-provenance.jq" in strict_job
    assert "validate-candidate-sbom.jq" in strict_job
    assert '"https://spdx.dev/Document"' in strict_job
    assert '"https://slsa.dev/provenance/v0.2"' in strict_job
    assert '"$RUNNER_TEMP/cardrag-release-registry-tools/crane" blob' in strict_job
    assert "--format '{{json .SBOM}}'" not in strict_job
    assert "--format '{{json .Provenance}}'" not in strict_job
    assert "contains($source_commit)" not in strict_job
    assert 'docker pull --platform linux/amd64 "$image"' in strict_job
    assert 'crane" manifest' in strict_job
    assert '"$image" "$CANDIDATE_IMAGE_REPOSITORY" "$digest" "$config_digest"' in strict_job
    assert '"$CANDIDATE_SOURCE_COMMIT"' in strict_job
    assert "{{ .Config.User }}" in strict_job
    assert '"10001:10001"' in strict_job
    assert "--scanners vuln,secret" in strict_job
    assert "--platform linux/amd64" in strict_job
    assert "--exit-code 1" in strict_job
    assert "--severity HIGH,CRITICAL" in strict_job
    assert "--format json" in strict_job
    assert "database[name]" in strict_job
    assert "timedelta(hours=36)" in strict_job
    assert "timedelta(hours=2)" in strict_job
    assert "strict-scan-${{ needs.validate.outputs.version }}-${{ matrix.target }}" in strict_job
    assert "--ignore-unfixed" not in strict_job
    assert "docker build \\" not in strict_job
    assert "setup-buildx-action@" not in strict_job

    assert 'test "$revision" = "$CANDIDATE_SOURCE_COMMIT"' in preflight_job
    assert 'if [[ "$observed" != "$expected_digest" ]]' in preflight_job
    assert "local reference=$1 role=$2 expected_index=$3 expected_config=$4" in preflight_job
    assert 'validate_image "$reference" "$role" "$expected_digest" "$expected_config"' in preflight_job
    assert "setup-buildx-action@" not in preflight_job

    assert "actions/download-artifact@" in publish_job
    assert '.schema == "cardrag.strict-image-scan.v1"' in publish_job
    assert "go-containerregistry_Linux_x86_64.tar.gz" in publish_job
    assert "edb74d53fad9a596860f59d1c5d04a43dfb5f441dc71f57060dd0bf39483c833" in publish_job
    assert 'source_reference="${CANDIDATE_IMAGE_REPOSITORY}@${digest}"' in publish_job
    assert "packages: read" in publish_job
    assert publish_job.count("uses: ./.github/actions/verify-public-candidate-package") == 1
    assert "/orgs/Kanu-Coffee/packages/" not in publish_job
    assert "registry: ghcr.io" not in publish_job
    assert publish_job.count("uses: docker/login-action@") == 1
    assert '"$RUNNER_TEMP/cardrag-release-registry-tools/crane" copy' in publish_job
    assert 'test "$(resolve_digest "$reference")" = "$digest"' in publish_job
    assert '"${IMAGE_NAME}@${digest}" \\' in publish_job
    assert '"$RUNNER_TEMP/cardrag-release-registry-tools/crane" manifest' in publish_job
    assert "validate-candidate-oci-index.jq" in publish_job
    assert "validate-candidate-platform-manifest.jq" in publish_job
    assert "cmp --silent /tmp/source-platform.json /tmp/published-platform.json" in publish_job
    assert 'filesystem_receipt="strict-filesystem-scan/strict-filesystem-scan.json"' in publish_job
    assert publish_job.count("assert now - updated_at <= timedelta(hours=36)") == 2
    assert publish_job.count("assert now - downloaded_at <= timedelta(hours=2)") == 2
    assert 'test "$(git cat-file -t "refs/tags/v$VERSION")" = tag' in publish_job
    assert "fetch-depth: 0" in publish_job
    assert "persist-credentials: false" in publish_job
    assert "GH_TOKEN: ${{ github.token }}" in publish_job
    assert 'remote_tag_ref=$(gh api "repos/${GITHUB_REPOSITORY}/git/ref/tags/v${VERSION}")' in (publish_job)
    assert '"repos/${GITHUB_REPOSITORY}/git/tags/${remote_tag_object}"' in publish_job
    assert 'test "$remote_tag_commit" = "$GITHUB_SHA"' in publish_job
    assert publish_job.index('test "$remote_tag_commit" = "$GITHUB_SHA"') < publish_job.index(
        '"$RUNNER_TEMP/cardrag-release-registry-tools/crane" copy'
    )
    assert "docker/build-push-action@" not in publish_job
    assert "docker build \\" not in publish_job
    assert "setup-buildx-action@" not in publish_job
    assert "imagetools create" not in publish_job
    assert "VCS_REF=${{ github.sha }}" not in publish_job
    record_step = publish_job.split("      - name: Record role digest\n", 1)[1].split(
        "      - uses: actions/upload-artifact@", 1
    )[0]
    for binding in (
        "PLATFORM_DIGEST: ${{ steps.resolved.outputs.platform_digest }}",
        "CONFIG_DIGEST: ${{ steps.resolved.outputs.config_digest }}",
        "ATTESTATION_DIGEST: ${{ steps.resolved.outputs.attestation_digest }}",
    ):
        assert binding in record_step
    assert '"schema": "cardrag.container-release-part.v6"' in publish_job
    assert '"candidate_source_commit": os.environ["CANDIDATE_SOURCE_COMMIT"]' in publish_job
    assert "needs: [validate, strict-filesystem-scan, strict-image-scan]" in workflow
    assert workflow.count("-f .github/scripts/validate-candidate-oci-index.jq") == 3
    assert workflow.count("-f .github/scripts/validate-candidate-attestation-manifest.jq") == 3
    assert workflow.count("python3 .github/scripts/validate-strict-json.py") == 12
    assert workflow.count("-f .github/scripts/validate-candidate-platform-manifest.jq") == 4
    package_action = (ROOT / ".github/actions/verify-public-candidate-package/action.yml").read_text(
        encoding="utf-8"
    )
    package_filter = (ROOT / ".github/actions/verify-public-candidate-package/validate-package.jq").read_text(
        encoding="utf-8"
    )
    assert (
        '"https://api.github.com/${owner_route}/${GITHUB_REPOSITORY_OWNER}/packages/container/${package_name}"'
        in package_action
    )
    assert '"$package_owner" = "${GITHUB_REPOSITORY_OWNER,,}"' in package_action
    assert "curl --proto '=https' --tlsv1.2" in package_action
    assert 'test -n "${GH_TOKEN:-}"' in package_action
    assert '-H "Authorization: Bearer ${GH_TOKEN}"' in package_action
    assert 'gh api "repos/${GITHUB_REPOSITORY}" --jq ' in package_action
    assert '$ARGS.named.package_name // "mcp-card-prd-detail-candidate"' in package_filter
    assert '.visibility == "public"' in package_filter
    assert '.package_type == "container"' in package_filter
    assert "((.owner.login | ascii_downcase) == ($owner | ascii_downcase))" in package_filter
    assert '.owner.type == ($ARGS.named.owner_type // "User")' in package_filter
    assert ".repository" not in package_filter
    assert not (ROOT / ".github/actions/verify-private-candidate-package").exists()


def test_release_local_image_identity_is_portable_and_fail_closed() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    repository = "example/cardrag"
    index_digest = "sha256:" + "1" * 64
    config_digest = "sha256:" + "2" * 64
    platform_digest = "sha256:" + "3" * 64
    other_digest = "sha256:" + "4" * 64
    repo_digest = f"{repository}@{index_digest}"
    oci_index_descriptor = {
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "digest": index_digest,
        "size": 1234,
    }

    assert workflow.count("validate_local_image_identity() {") == 3
    helper = _local_image_identity_helper(workflow)
    indented_helper = "".join(f"          {line}\n" for line in helper.splitlines())
    assert workflow.count(indented_helper) == 3
    assert workflow.count("          validate_local_image_identity \\") == 4
    assert "docker image inspect \"$image\" --format '{{ .Id }}'" not in workflow
    assert "docker image inspect \"$reference\" --format '{{ .Id }}'" not in workflow
    assert "docker image inspect \"$source_reference\" --format '{{ .Id }}'" not in workflow
    assert "docker image inspect \"${IMAGE_NAME}@${digest}\" --format '{{ .Id }}'" not in workflow
    assert workflow.count('index($repository + "@" + $index_digest)') == 3
    assert workflow.count('== "application/vnd.oci.image.index.v1+json"') >= 3
    assert workflow.count("$image.Descriptor.digest == $index_digest") == 3
    assert workflow.count("validated local image identity kind=${identity_kind}") == 3

    valid_cases = (
        (
            [{"Id": config_digest, "RepoDigests": [repo_digest]}],
            "kind=platform-config",
        ),
        (
            [
                {
                    "Id": index_digest,
                    "RepoDigests": [repo_digest],
                    "Descriptor": oci_index_descriptor,
                }
            ],
            "kind=sealed-index",
        ),
    )
    for payload, expected_log in valid_cases:
        result = _run_local_image_identity_helper(
            workflow,
            payload,
            repository=repository,
            index_digest=index_digest,
            config_digest=config_digest,
        )
        assert result.returncode == 0, result.stderr
        assert expected_log in result.stderr
        assert index_digest not in result.stderr
        assert config_digest not in result.stderr

    invalid_cases = (
        # A platform-manifest or any unrelated/attestation digest is never a local image ID.
        [
            {
                "Id": platform_digest,
                "RepoDigests": [repo_digest],
                "Descriptor": oci_index_descriptor,
            }
        ],
        [{"Id": other_digest, "RepoDigests": [repo_digest]}],
        # RepoDigests must contain the exact repository-to-index binding.
        [{"Id": config_digest, "RepoDigests": [f"{repository}@{platform_digest}"]}],
        # A provided descriptor must be the exact OCI index, not another digest or media type.
        [
            {
                "Id": index_digest,
                "RepoDigests": [repo_digest],
                "Descriptor": {**oci_index_descriptor, "digest": platform_digest},
            }
        ],
        [
            {
                "Id": index_digest,
                "RepoDigests": [repo_digest],
                "Descriptor": {
                    **oci_index_descriptor,
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                },
            }
        ],
        [{"Id": config_digest, "RepoDigests": [repo_digest], "Descriptor": False}],
    )
    for payload in invalid_cases:
        result = _run_local_image_identity_helper(
            workflow,
            payload,
            repository=repository,
            index_digest=index_digest,
            config_digest=config_digest,
        )
        assert result.returncode != 0


def test_runtime_capture_accepts_classic_missing_descriptor() -> None:
    descriptor_filter = ".[0].ImageManifestDescriptor // null"

    jq = shutil.which("jq")
    assert jq is not None
    classic_inspect = [
        {
            "Image": "sha256:" + "2" * 64,
            "Config": {"Image": "example/cardrag@sha256:" + "1" * 64},
        }
    ]
    result = subprocess.run(  # noqa: S603 - executable and filter are repository-controlled
        (jq, "-c", descriptor_filter),
        input=json.dumps(classic_inspect),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "null\n"


def test_release_strict_filesystem_scan_is_sealed_and_published_as_evidence() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    filesystem_job = workflow.split("  strict-filesystem-scan:\n", 1)[1].split("  strict-image-scan:\n", 1)[0]
    release_job = workflow.split("  release:\n", 1)[1]

    for contract in (
        "trivy_0.74.0_Linux-64bit.tar.gz",
        "--scanners vuln,secret,misconfig",
        "--exit-code 1",
        "--severity HIGH,CRITICAL",
        "trivy-filesystem.json",
        "trivy-filesystem-version.json",
        "cardrag.strict-filesystem-scan.v1",
        'scope: "tagged-worktree-and-release-evidence.trivy-default-skips"',
        'language_dependency_completion: "exact-final-image-scans"',
        "strict-filesystem-scan-${{ needs.validate.outputs.version }}",
    ):
        assert contract in filesystem_job
    assert "--ignore-unfixed" not in filesystem_job
    assert "persist-credentials: false" in filesystem_job

    for asset in (
        "strict-filesystem-scan.json",
        "trivy-filesystem.json",
        "trivy-filesystem-version.json",
    ):
        assert asset in release_job
    assert "needs: [validate, publish, strict-filesystem-scan]" in workflow


def test_raw_oci_evidence_secret_scans_are_fail_closed_and_release_bound() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    strict_job = workflow.split("  strict-image-scan:\n", 1)[1].split("  registry-preflight:\n", 1)[0]
    publish_job = workflow.split("  publish:\n", 1)[1].split("  release:\n", 1)[0]
    release_job = workflow.split("  release:\n", 1)[1]

    for contract in (
        "gitleaks_8.30.1_linux_x64.tar.gz",
        "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb",
        'test "$("$audit_tools_dir/gitleaks" version)" = "8.30.1"',
        '--source "$evidence_secret_scan_dir"',
        "--no-git",
        "--config .gitleaks.toml",
        "--redact",
        '--report-path "gitleaks-evidence-${TARGET}.json"',
        '"$RUNNER_TEMP/cardrag-release-audit-tools/trivy" fs',
        "--scanners secret",
        '--output "trivy-evidence-${TARGET}.json"',
        '"$(sha256sum "gitleaks-evidence-${TARGET}.json" | cut -d\' \' -f1)"',
        '"$(sha256sum "trivy-evidence-${TARGET}.json" | cut -d\' \' -f1)"',
        "--arg gitleaks_version 8.30.1",
        "gitleaks_version: $gitleaks_version",
        "gitleaks_evidence_report_sha256: $gitleaks_evidence_report_sha256",
        "evidence_secret_report_sha256: $evidence_secret_report_sha256",
        "gitleaks-evidence-${{ matrix.target }}.json",
        "trivy-evidence-${{ matrix.target }}.json",
    ):
        assert contract in strict_job
    assert strict_job.index('install -m 0600 "provenance-${TARGET}.json"') < strict_job.index(
        'gitleaks" detect'
    )
    assert strict_job.index('install -m 0600 "sbom-${TARGET}.json"') < strict_job.index('gitleaks" detect')
    assert strict_job.index('gitleaks" detect') < strict_job.index(
        'docker pull --platform linux/amd64 "$image"'
    )
    assert strict_job.index('trivy" fs') < strict_job.index('docker pull --platform linux/amd64 "$image"')

    for contract in (
        'gitleaks_evidence_report="strict-scan/gitleaks-evidence-${TARGET}.json"',
        'evidence_secret_report="strict-scan/trivy-evidence-${TARGET}.json"',
        "gitleaks_evidence_report_sha256=$(sha256sum",
        "evidence_secret_report_sha256=$(sha256sum",
        'jq -e \'type == "array" and length == 0\' "$gitleaks_evidence_report"',
        "([.Results[]?.Secrets[]?] | length == 0)",
        ".gitleaks_evidence_report_sha256 == $gitleaks_evidence_report_sha256",
        ".evidence_secret_report_sha256 == $evidence_secret_report_sha256",
        '.gitleaks_version == "8.30.1"',
        '"gitleaks_evidence_report_path": f"strict-scan/gitleaks-evidence-{role}.json"',
        '"evidence_secret_report_path": f"strict-scan/trivy-evidence-{role}.json"',
        '"gitleaks_version": scan_receipt["gitleaks_version"]',
        "strict-scan/gitleaks-evidence-${{ matrix.target }}.json",
        "strict-scan/trivy-evidence-${{ matrix.target }}.json",
    ):
        assert contract in publish_job

    for contract in (
        "gitleaks_evidence_report_path = source / strict_scan[",
        '"gitleaks_evidence_report_path"',
        'strict_scan["gitleaks_evidence_report_sha256"]',
        "evidence_secret_report_path = source / strict_scan[",
        '"evidence_secret_report_path"',
        'strict_scan["evidence_secret_report_sha256"]',
        "gitleaks-evidence-worker.json",
        "gitleaks-evidence-mcp.json",
        "trivy-evidence-worker.json",
        "trivy-evidence-mcp.json",
    ):
        assert contract in release_job
    sha256sums_step = release_job.split("            sha256sum \\\n", 1)[1].split(" > SHA256SUMS", 1)[0]
    for report in (
        "gitleaks-evidence-worker.json",
        "gitleaks-evidence-mcp.json",
        "trivy-evidence-worker.json",
        "trivy-evidence-mcp.json",
    ):
        assert report in sha256sums_step


def test_repository_gitleaks_policy_rejects_openrouter_and_github_tokens(tmp_path: Path) -> None:
    config = (ROOT / ".gitleaks.toml").read_text(encoding="utf-8")
    assert "useDefault = true" in config
    assert 'targetRules = ["generic-api-key"]' in config
    assert "Keep the allowlist value-specific" in config
    assert 'id = "openrouter-api-key"' in config
    assert "sk-or-v1-[0-9A-Fa-f]{64}" in config
    assert 'id = "github-fine-grained-personal-access-token"' in config

    safe_payloads = {
        "provenance.json": {
            "predicate": {
                "invocation": {
                    "parameters": {
                        "secrets": [
                            {"id": "GIT_AUTH_HEADER", "optional": True},
                            {"id": "GIT_AUTH_TOKEN", "optional": True},
                        ]
                    }
                }
            }
        },
        "sbom.json": {
            "subject": [{"digest": {"sha256": "a" * 64}}],
            "predicateType": "https://spdx.dev/Document",
        },
    }
    assert _raw_evidence_is_gitleaks_clean(tmp_path / "safe", safe_payloads)

    injected_tokens = {
        "github": ("reuse_key", "".join(("github", "_pat_", "a" * 82))),
        "openrouter": (
            "tokenizer_sha256",
            "".join(("sk-or", "-v1-", "0123456789abcdef" * 4)),
        ),
    }
    for name, (field, token) in injected_tokens.items():
        payloads = copy.deepcopy(safe_payloads)
        payloads["provenance.json"]["predicate"]["metadata"] = {  # type: ignore[index]
            field: token
        }
        assert not _raw_evidence_is_gitleaks_clean(tmp_path / name, payloads)


def test_release_validator_tool_and_registry_readers_are_checksum_pinned() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "python -m pip install" not in workflow
    assert "uv-x86_64-unknown-linux-gnu.tar.gz" in workflow
    assert "920cbcaad514cc185634f6f0dcd71df5e8f4ee4456d440a22e0f8c0f142a8203" in workflow
    assert 'test "$("$validator_tools_dir/uv" --version)" = "uv 0.8.17"' in workflow


def test_release_metadata_preserves_scan_and_candidate_source_identity() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    for required in (
        '"schema": "cardrag.container-release.v6"',
        'part["digest"] == os.environ[f"{role.upper()}_DIGEST"]',
        'part["candidate_source_commit"] == os.environ["CANDIDATE_SOURCE_COMMIT"]',
        'part["platform_config_digest"] == os.environ[',
        'scan_receipt["digest"] == part["digest"]',
        "strict-scan-worker.json",
        "strict-scan-mcp.json",
        "trivy-worker.json",
        "trivy-mcp.json",
        "trivy-version-worker.json",
        "trivy-version-mcp.json",
        "sbom-worker.json",
        "sbom-mcp.json",
        "provenance-worker.json",
        "provenance-mcp.json",
        "CANDIDATE_SOURCE_COMMIT: ${{ needs.validate.outputs.candidate_source_commit }}",
        "WORKER_DIGEST: ${{ needs.validate.outputs.worker_digest }}",
        "MCP_DIGEST: ${{ needs.validate.outputs.mcp_digest }}",
    ):
        assert required in workflow

    assert "docker/build-push-action@" not in workflow
    assert "docker/setup-buildx-action@" not in workflow


def test_release_smoke_contract_matches_actual_default_mcp_discovery(tmp_path: Path) -> None:
    # Register the actual API without activating a generation or performing I/O.
    settings = Settings(environment="test", mcp_state_dir=tmp_path, mcp_bearer_token=AUTH_VALUE)
    server = build_mcp_server(Mock(), Mock(), settings)
    tools = asyncio.run(server.list_tools())
    assert tuple(tool.name for tool in tools) == MCP_TOOLS
    assert len(tools) == 12
    for tool in tools:
        assert tuple(tool.input_schema.get("required", ())) == MCP_REQUIRED_ARGUMENTS[tool.name]
    assert "experimental_long_context_audit" not in MCP_TOOLS


def test_public_candidate_package_supports_explicit_fork_ownership(tmp_path: Path) -> None:
    candidate = {
        "id": 2,
        "name": "cardrag-candidate",
        "package_type": "container",
        "visibility": "public",
        "owner": {"login": "ExampleOrg", "type": "Organization"},
    }
    options = {"owner": "ExampleOrg", "owner_type": "Organization", "package_name": "cardrag-candidate"}
    assert _public_package_is_valid(tmp_path, candidate, **options)
    assert not _public_package_is_valid(tmp_path, candidate)
    candidate["visibility"] = "private"
    assert not _public_package_is_valid(tmp_path, candidate, **options)


def test_release_requires_exact_qualification_and_evidence_only_sealing_commit() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    required = (
        "release_qualification_sha256:",
        "candidate_source_commit:",
        "candidate_worker_image_digest:",
        "candidate_mcp_image_digest:",
        '[[ "$CANDIDATE_SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ ]]',
        '[[ "$RELEASE_QUALIFICATION_SHA256" =~ ^[0-9a-f]{64}$ ]]',
        '[[ "$CANDIDATE_WORKER_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]',
        '[[ "$CANDIDATE_MCP_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]',
        'test "$CANDIDATE_SOURCE_COMMIT" != "$GITHUB_SHA"',
        'git merge-base --is-ancestor "$CANDIDATE_SOURCE_COMMIT" "$GITHUB_SHA"',
        "mapfile -d '' candidate_evidence_paths",
        'git diff --name-only -z "$CANDIDATE_SOURCE_COMMIT" "$GITHUB_SHA"',
        "((${#candidate_evidence_paths[@]} > 0))",
        'git diff --quiet "$CANDIDATE_SOURCE_COMMIT" "$GITHUB_SHA" --',
        'test "$version" = "1.0.34"',
        "if: ${{ inputs.version == '1.0.34' }}",
        "':(exclude)release-evidence/v1.0.34/**'",
        "release-evidence/v1.0.34/*) ;;",
        'qualification="$qualification_dir/release-qualification.json"',
        'test ! -L "$qualification_dir"',
        'test ! -L "$qualification"',
        'test "$(find "$qualification_dir" -mindepth 1 | wc -l)" -eq 1',
        'test "$(sha256sum "$qualification" | awk \'{print $1}\')" =',
        ".venv/bin/python -m cardrag_core.release_qualification",
        '--expected-sha256 "$RELEASE_QUALIFICATION_SHA256"',
        '--expected-source-commit "$CANDIDATE_SOURCE_COMMIT"',
        '--expected-repository "$GITHUB_REPOSITORY"',
        '"cardrag.release-qualification-validation.v1"',
        'test "$(jq -r \'.status\' <<<"$qualification_validation")" = qualified',
        'tag="${CANDIDATE_IMAGE_REPOSITORY}:candidate-v${VERSION}-${role}-${CANDIDATE_SOURCE_COMMIT}"',
        'test "$("$crane" digest "$tag")" = "$digest"',
    )
    for contract in required:
        assert contract in workflow

    assert workflow.count('--expected-source-commit "$CANDIDATE_SOURCE_COMMIT"') == 2
    assert '--expected-source-commit "$GITHUB_SHA"' not in workflow
    for stale in (
        "candidate-acceptance-receipt.json",
        "release-readiness-receipt",
        "readiness_evidence_manifest_sha256",
        "cardrag_mcp.candidate_smoke",
        "cardrag_mcp.release_readiness",
        "collect_evidence_files",
        "gold-evaluation-report",
        "gold-capture-set-receipt",
        "acceptance_report_sha256",
        "aggregation_profile_sha256",
        "capture_set_receipt_sha256",
    ):
        assert stale not in workflow

    notes_region = workflow[workflow.index("notes = [") :]
    for claim in (
        "candidate_worker_full_run",
        "candidate_mcp_12_tools",
        "gold_quality_evaluation",
        "production_cutover",
        "참고(출처가 다른 운영 증거)",
        "운영 배포 승인이",
    ):
        assert claim in notes_region


def test_publish_revalidates_qualification_before_any_registry_mutation() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    publish_job = workflow.split("  publish:\n", 1)[1].split("  release:\n", 1)[0]
    revalidate = "Revalidate the release qualification evidence before registry mutation"
    assert publish_job.index(revalidate) < publish_job.index(
        '"$RUNNER_TEMP/cardrag-release-registry-tools/crane" copy'
    )
    for contract in (
        "name: release-qualification-${{ needs.validate.outputs.version }}",
        ".venv/bin/python -m cardrag_core.release_qualification",
        '"cardrag.release-qualification.v1"',
    ):
        assert contract in publish_job
    assert '"release_qualification": {' in publish_job
    assert workflow.count("name: release-qualification-${{ needs.validate.outputs.version }}") == 2
    assert workflow.count("name: release-qualification-${{ steps.version.outputs.version }}") == 1
