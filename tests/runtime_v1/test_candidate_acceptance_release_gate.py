import copy
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _portable_publish_verifier(workflow: str) -> str:
    step = workflow.split(
        "      - name: Revalidate complete portable evidence before registry mutation\n", 1
    )[1].split("\n      - name: Publish only the receipt-bound", 1)[0]
    embedded = step.split("            \"$GITHUB_SHA\" <<'PY'\n", 1)[1].rsplit("\n          PY", 1)[0]
    return "\n".join(line[10:] for line in embedded.splitlines()) + "\n"


def _run_portable_verifier(
    workflow: str,
    bundle: Path,
    manifest_sha256: str,
    source_commit: str,
    tag_commit: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - interpreter and embedded workflow code are controlled
        (sys.executable, "-", str(bundle), manifest_sha256, source_commit, tag_commit),
        input=_portable_publish_verifier(workflow),
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


def _public_package_is_valid(tmp_path: Path, package: object) -> bool:
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
            "Kanu-Coffee",
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


def test_release_requires_exact_candidate_receipt_and_evidence_only_sealing_commit() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    required = (
        "candidate_source_commit:",
        "candidate_acceptance_sha256:",
        "candidate_worker_image_digest:",
        "candidate_mcp_image_digest:",
        '[[ "$CANDIDATE_SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ ]]',
        '[[ "$CANDIDATE_ACCEPTANCE_SHA256" =~ ^[0-9a-f]{64}$ ]]',
        '[[ "$CANDIDATE_WORKER_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]',
        '[[ "$CANDIDATE_MCP_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]',
        'test "$CANDIDATE_SOURCE_COMMIT" != "$GITHUB_SHA"',
        'git merge-base --is-ancestor "$CANDIDATE_SOURCE_COMMIT" "$GITHUB_SHA"',
        "mapfile -d '' candidate_evidence_paths",
        'git diff --name-only -z "$CANDIDATE_SOURCE_COMMIT" "$GITHUB_SHA"',
        "((${#candidate_evidence_paths[@]} > 0))",
        'git diff --quiet "$CANDIDATE_SOURCE_COMMIT" "$GITHUB_SHA" --',
        'test "$version" = "1.0.12"',
        "if: ${{ inputs.version == '1.0.12' }}",
        "':(exclude)release-evidence/v1.0.12/**'",
        "release-evidence/v1.0.12/*) ;;",
        'candidate_acceptance="$evidence_dir/candidate-acceptance-receipt.json"',
        'test "$(sha256sum "$candidate_acceptance" | awk \'{print $1}\')" =',
        "candidate_validation=$(",
        ".venv/bin/python -m cardrag_core.candidate_acceptance",
        '--expected-receipt-sha256 "$CANDIDATE_ACCEPTANCE_SHA256"',
        '--expected-image-repository "$CANDIDATE_IMAGE_REPOSITORY"',
        "worker_image.platform_manifest_digest",
        "worker_image.platform_config_digest",
        "worker_image.attestation_manifest_digest",
        "mcp_image.platform_manifest_digest",
        "mcp_image.platform_config_digest",
        "mcp_image.attestation_manifest_digest",
        'test "$(jq -r \'.worker_image.digest\' <<<"$candidate_validation")" =',
        'test "$(jq -r \'.mcp_image.digest\' <<<"$candidate_validation")" =',
    )
    for contract in required:
        assert contract in workflow

    assert workflow.count('--expected-source-commit "$CANDIDATE_SOURCE_COMMIT"') == 4
    assert '--expected-source-commit "$GITHUB_SHA"' not in workflow
    assert 'test "$version" = "1.0.11"' not in workflow
    assert "release-evidence/v1.0.11" not in workflow


def test_release_legacy_validator_binds_the_contemporaneous_execution_record() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    assert 'legacy_historical_source="$evidence_dir/v109-structure-audit-execution.json"' in workflow
    assert '--historical-source-artifact "$legacy_historical_source"' in workflow


def test_release_validates_the_complete_compact_v2_score_evidence() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    validate_job = workflow.split("  validate:\n", 1)[1].split("  strict-filesystem-scan:\n", 1)[0]
    operator_documents = (
        (ROOT / "docs/V1_0_10_AGGREGATION_PROFILE.md").read_text(encoding="utf-8"),
        (ROOT / "docs/V1_0_10_GOLD_EVALUATION.md").read_text(encoding="utf-8"),
    )

    for contract in (
        'aggregation_corpus_inventory="$evidence_dir/document-aggregation-corpus-inventory.jsonl"',
        'aggregation_score_matrix="$evidence_dir/document-aggregation-score-matrix.f32"',
        'aggregation_query_vector_matrix="$evidence_dir/document-aggregation-query-vectors.f32"',
        '--corpus-inventory "$aggregation_corpus_inventory"',
        '--score-matrix "$aggregation_score_matrix"',
        '--query-vector-matrix "$aggregation_query_vector_matrix"',
        '--native-score-artifact "$aggregation_scores"',
        '--native-score-corpus-inventory "$aggregation_corpus_inventory"',
        '--native-score-matrix "$aggregation_score_matrix"',
        '--native-score-query-vector-matrix "$aggregation_query_vector_matrix"',
        '"v109_baseline=$evidence_dir/v109_baseline.corpus.jsonl"',
        '"qwen_page=$evidence_dir/qwen_page.corpus.jsonl"',
        '"v109_baseline=$evidence_dir/v109_baseline.dense-scores.f32"',
        '"qwen_page=$evidence_dir/qwen_page.dense-scores.f32"',
        '"v109_baseline=$evidence_dir/v109_baseline.query-vectors.f32"',
        '"qwen_page=$evidence_dir/qwen_page.query-vectors.f32"',
        '"v109_baseline=$evidence_dir/v109_baseline.lexical-ranks.jsonl"',
    ):
        assert contract in validate_job

    assert validate_job.count("--external-inventory") == 2
    assert validate_job.count("--external-score-matrix") == 2
    assert validate_job.count("--external-query-vector-matrix") == 2
    assert validate_job.count("--external-lexical-ranks") == 1
    assert "qwen_page.lexical-ranks" not in validate_job
    assert "document-aggregation-query-vector-matrix.f32" not in validate_job
    for document in operator_documents:
        assert "document-aggregation-query-vectors.f32" in document
        assert "document-aggregation-query-vector-matrix.f32" not in document


def test_release_binds_exactly_three_portable_answer_evidence_chains() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    validate_job = workflow.split("  validate:\n", 1)[1].split("  strict-filesystem-scan:\n", 1)[0]
    answer_loop = validate_job.split("            for answer_lane in \\\n", 1)[1].split(
        "            # validate-set revalidates canonical bindings", 1
    )[0]

    assert answer_loop.startswith(
        "              v109_baseline \\\n              qwen_page \\\n              qwen_structure_exact; do\n"
    )
    assert "lexical_shadow" not in answer_loop
    assert "reranker_shadow" not in answer_loop
    for suffix in (
        "input.jsonl",
        "producer-receipt.json",
        "answers.jsonl",
        "call-ledger.jsonl",
        "state-identity.json",
        "state-bundle.jsonl",
    ):
        assert f"$evidence_dir/answers/${{answer_lane}}.{suffix}" in answer_loop
    for flag in (
        "--answer-input",
        "--expected-answer-input-sha256",
        "--answer-producer-receipt",
        "--expected-answer-producer-receipt-sha256",
        "--answer-artifact",
        "--expected-answer-artifact-sha256",
        "--answer-call-ledger",
        "--answer-state-identity",
        "--answer-state-bundle",
        "--answer-profile-id",
        "--answer-retrieval-run",
        "--expected-answer-retrieval-run-sha256",
        "--answer-retrieval-capture-receipt",
        "--expected-answer-retrieval-capture-receipt-sha256",
        "--answer-retrieval-attestation",
        "--expected-answer-retrieval-attestation-sha256",
        "--answer-retrieval-raw-score",
        "--expected-answer-retrieval-raw-score-sha256",
        "--answer-retrieval-corpus-inventory",
        "--expected-answer-retrieval-corpus-inventory-sha256",
        "--answer-retrieval-dense-score-matrix",
        "--expected-answer-retrieval-dense-score-matrix-sha256",
        "--answer-retrieval-query-vector-matrix",
        "--expected-answer-retrieval-query-vector-matrix-sha256",
    ):
        assert answer_loop.count(flag) == 1

    assert '"$answer_lane=cardrag.answer.extractive-k8.v1"' in answer_loop
    assert answer_loop.count("--answer-retrieval-lexical-ranks") == 1
    assert answer_loop.count("--expected-answer-retrieval-lexical-ranks-sha256") == 1
    assert '[[ "$answer_lane" == "v109_baseline" ]]' in answer_loop
    assert "--answer-decision" not in answer_loop
    assert "--expected-answer-decision-sha256" not in answer_loop
    assert "--database " not in validate_job
    assert "--vectors " not in validate_job


def test_release_caps_packages_and_publishes_every_portable_evidence_file() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    path_block = workflow.split("            portable_evidence_relative_paths=(\n", 1)[1].split(
        "\n            )", 1
    )[0]
    portable_paths = tuple(line.strip() for line in path_block.splitlines() if line.strip())
    assert len(portable_paths) == len(set(portable_paths))
    assert all("*" not in path and "?" not in path for path in portable_paths)

    compact_paths = {
        "document-aggregation-scores.jsonl",
        "document-aggregation-corpus-inventory.jsonl",
        "document-aggregation-score-matrix.f32",
        "document-aggregation-query-vectors.f32",
        "v109_baseline.corpus.jsonl",
        "v109_baseline.dense-scores.f32",
        "v109_baseline.query-vectors.f32",
        "v109_baseline.lexical-ranks.jsonl",
        "qwen_page.corpus.jsonl",
        "qwen_page.dense-scores.f32",
        "qwen_page.query-vectors.f32",
        "bootstrap/v109_baseline.jsonl",
        "bootstrap/v109_baseline.capture-receipt.json",
        "bootstrap/qwen_page.jsonl",
        "bootstrap/qwen_page.capture-receipt.json",
        "bootstrap/qwen_structure_exact.jsonl",
        "bootstrap/qwen_structure_exact.capture-receipt.json",
        "bootstrap/native-v5-attestation.jsonl",
    }
    for lane in ("v109_baseline", "qwen_page", "qwen_structure_exact"):
        compact_paths.update(
            {
                f"answers/{lane}.input.jsonl",
                f"answers/{lane}.producer-receipt.json",
                f"answers/{lane}.answers.jsonl",
                f"answers/{lane}.call-ledger.jsonl",
                f"answers/{lane}.state-identity.json",
                f"answers/{lane}.state-bundle.jsonl",
            }
        )
    assert compact_paths.issubset(portable_paths)

    for contract in (
        'portable_validation_dir="$RUNNER_TEMP/cardrag-portable-validation-evidence"',
        "MAX_FILE_BYTES = 95_000_000",
        'getattr(os, "O_NOFOLLOW", 0)',
        "os.O_DIRECTORY",
        "value.st_dev",
        "value.st_ino",
        "value.st_mode",
        "value.st_nlink",
        "value.st_size",
        "value.st_mtime_ns",
        "value.st_ctime_ns",
        "hash_pinned_descriptor",
        "os.fsync(destination)",
        "os.link(",
        '"schema": "cardrag.portable-evaluation-evidence.v1"',
        '"maximum_file_bytes": MAX_FILE_BYTES',
        "name: portable-evidence-${{ steps.version.outputs.version }}",
        "name: portable-evidence-${{ needs.validate.outputs.version }}",
        '"portable_evidence": portable_evidence',
        '"${portable_release_assets[@]}" > SHA256SUMS',
        'assets+=("release-assets/$portable_asset")',
    ):
        assert contract in workflow
    assert workflow.count('"portable_evidence": portable_evidence') == 2
    assert workflow.count("Download the exact portable evaluation evidence") == 1
    assert workflow.count("Download exact portable evaluation evidence") == 1
    assert "portable-evidence-manifest.json" in workflow


def test_release_validators_read_the_manifest_bound_preserved_path_snapshot() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    validate_job = workflow.split("  validate:\n", 1)[1].split("  strict-filesystem-scan:\n", 1)[0]
    snapshot_binding = 'evidence_dir=$(realpath --canonicalize-existing "$portable_validation_dir")'
    assert snapshot_binding in validate_job
    binding_index = validate_job.index(snapshot_binding)
    for invocation in (
        "candidate_validation=$(",
        ".venv/bin/python -m cardrag_worker.legacy_v4_audit",
        ".venv/bin/python -m cardrag_mcp.aggregation_profile",
        '.venv/bin/python -m cardrag_mcp.evaluation "${validator_args[@]}"',
        '.venv/bin/python -m cardrag_mcp.gold_capture "${capture_args[@]}"',
    ):
        assert binding_index < validate_job.index(invocation)
    for contract in (
        '--evidence-root "$evidence_dir"',
        '--generation-manifest-dir "$evidence_dir/generation-manifests"',
        'candidate_acceptance="$evidence_dir/candidate-acceptance-receipt.json"',
        'capture_set_receipt="$evidence_dir/gold-capture-set-receipt.json"',
        "src_dir_fd=files_directory",
        "dst_dir_fd=snapshot_parent",
        "snapshot_listed.st_nlink == 2",
        "os.fchmod(snapshot_descriptor, 0o400)",
    ):
        assert contract in validate_job


def test_portable_preparse_includes_all_candidate_acceptance_evidence() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    validate_job = workflow.split("  validate:\n", 1)[1].split("  strict-filesystem-scan:\n", 1)[0]
    expected_keys = {
        "candidate_pointer",
        "effective_config",
        "generation_cas",
        "generation_manifest",
        "generation_ready",
        "mcp_smoke",
        "native_cache_after",
        "native_cache_audit",
        "native_cache_before",
        "rollback_ledger",
        "v109_identity",
        "worker_metrics",
    }
    key_block = validate_job.split("                  expected_evidence_keys = {\n", 1)[1].split(
        "\n                  }", 1
    )[0]
    observed_keys = {line.strip().strip('",') for line in key_block.splitlines() if line.strip()}
    assert observed_keys == expected_keys
    for contract in (
        "assert set(evidence_bindings) == expected_evidence_keys",
        "assert isinstance(binding, dict) and set(binding) == {",
        "candidate_artifacts[candidate_path] = (",
        "candidate_binding = candidate_artifacts.get(raw_relative)",
        "assert (digest, size) == candidate_binding",
        'f"generation-manifests/{sha256}.json"',
    ):
        assert contract in validate_job


def test_publish_revalidates_the_entire_portable_bundle_before_any_copy() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    publish_job = workflow.split("  publish:\n", 1)[1].split("  release:\n", 1)[0]
    verifier_index = publish_job.index("Revalidate complete portable evidence before registry mutation")
    assert verifier_index < publish_job.index('"$RUNNER_TEMP/cardrag-release-registry-tools/crane" copy')
    verifier = _portable_publish_verifier(workflow)
    for contract in (
        "assert set(os.listdir(bundle)) == {",
        "assert set(os.listdir(files)) == asset_names",
        'assert manifest["file_count"] == len(entries)',
        'assert manifest["source_commit"] == expected_source_commit',
        'assert manifest["tag_commit"] == expected_tag_commit',
        "assert manifest_sha256 == expected_manifest_sha256",
        "assert identity(os.fstat(descriptor)) == expected",
        "follow_symlinks=False",
        "assert 0 < listed.st_size <= maximum_bytes",
    ):
        assert contract in verifier


def test_portable_publish_verifier_rejects_extra_symlink_and_same_inode_tamper(
    tmp_path: Path,
) -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    source_commit = "a" * 40
    tag_commit = "b" * 40
    bundle = tmp_path / "portable-evidence"
    files = bundle / "files"
    files.mkdir(parents=True)
    relative_path = "answers/qwen_page.state-bundle.jsonl"
    asset_name = "evaluation-evidence--answers--qwen_page.state-bundle.jsonl"
    payload = b'{"schema":"fixture"}\n'
    artifact = files / asset_name
    artifact.write_bytes(payload)
    manifest = {
        "file_count": 1,
        "files": [
            {
                "relative_path": relative_path,
                "release_asset_name": asset_name,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
        ],
        "maximum_file_bytes": 95_000_000,
        "schema": "cardrag.portable-evaluation-evidence.v1",
        "source_commit": source_commit,
        "tag_commit": tag_commit,
    }
    manifest_payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    manifest_path = bundle / "portable-evidence-manifest.json"
    manifest_path.write_text(manifest_payload, encoding="utf-8")
    manifest_sha256 = hashlib.sha256(manifest_payload.encode()).hexdigest()

    assert (
        _run_portable_verifier(workflow, bundle, manifest_sha256, source_commit, tag_commit).returncode == 0
    )

    extra = files / "unexpected"
    extra.write_bytes(b"extra")
    assert (
        _run_portable_verifier(workflow, bundle, manifest_sha256, source_commit, tag_commit).returncode != 0
    )
    extra.unlink()

    real_files = bundle / "real-files"
    files.rename(real_files)
    files.symlink_to(real_files.name, target_is_directory=True)
    assert (
        _run_portable_verifier(workflow, bundle, manifest_sha256, source_commit, tag_commit).returncode != 0
    )
    files.unlink()
    real_files.rename(files)

    original_inode = artifact.stat().st_ino
    artifact.write_bytes(b'{"schema":"tampered"}\n')
    assert artifact.stat().st_ino == original_inode
    assert (
        _run_portable_verifier(workflow, bundle, manifest_sha256, source_commit, tag_commit).returncode != 0
    )


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
    assert "CANDIDATE_IMAGE_REPOSITORY: ghcr.io/kanu-coffee/" in workflow
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
    assert workflow.count("-f .github/scripts/validate-candidate-oci-index.jq") == 2
    assert workflow.count("-f .github/scripts/validate-candidate-attestation-manifest.jq") == 2
    assert workflow.count("python3 .github/scripts/validate-strict-json.py") == 8
    assert workflow.count("-f .github/scripts/validate-candidate-platform-manifest.jq") == 3
    package_action = (ROOT / ".github/actions/verify-public-candidate-package/action.yml").read_text(
        encoding="utf-8"
    )
    package_filter = (ROOT / ".github/actions/verify-public-candidate-package/validate-package.jq").read_text(
        encoding="utf-8"
    )
    assert (
        '"https://api.github.com/users/${GITHUB_REPOSITORY_OWNER}/packages/container/'
        'mcp-card-prd-detail-candidate"' in package_action
    )
    assert "curl --proto '=https' --tlsv1.2" in package_action
    assert 'test -n "${GH_TOKEN:-}"' in package_action
    assert '-H "Authorization: Bearer ${GH_TOKEN}"' in package_action
    assert "gh api" not in package_action
    assert '.name == "mcp-card-prd-detail-candidate"' in package_filter
    assert '.visibility == "public"' in package_filter
    assert '.package_type == "container"' in package_filter
    assert "((.owner.login | ascii_downcase) == ($owner | ascii_downcase))" in package_filter
    assert '.owner.type == "User"' in package_filter
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


def test_migration_runtime_capture_accepts_classic_missing_descriptor() -> None:
    migration = (ROOT / "docs/V1_0_11_MIGRATION.md").read_text(encoding="utf-8")
    descriptor_filter = ".[0].ImageManifestDescriptor // null"

    assert "--format '{{json .ImageManifestDescriptor}}'" not in migration
    assert 'container_inspect=$(docker container inspect "$candidate_container")' in migration
    assert descriptor_filter in migration

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
