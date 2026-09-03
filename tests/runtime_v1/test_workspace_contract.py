from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

from cardrag_worker import __version__ as worker_runtime_version

ROOT = Path(__file__).resolve().parents[2]


def _project(path: str) -> dict[str, object]:
    return tomllib.loads((ROOT / path / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def test_workspace_has_three_independent_packages_at_one_version() -> None:
    root = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "project" not in root
    assert root["tool"]["uv"]["workspace"]["members"] == [
        "packages/cardrag-core",
        "apps/cardrag-worker",
        "apps/cardrag-mcp",
    ]
    projects = [
        _project("packages/cardrag-core"),
        _project("apps/cardrag-worker"),
        _project("apps/cardrag-mcp"),
    ]
    assert [project["name"] for project in projects] == [
        "cardrag-core",
        "cardrag-worker",
        "cardrag-mcp",
    ]
    versions = {project["version"] for project in projects}
    assert versions == {"1.0.14"}
    assert worker_runtime_version == versions.pop()


def test_workspace_is_distributed_under_apache_2_0() -> None:
    package_paths = [
        "packages/cardrag-core",
        "apps/cardrag-worker",
        "apps/cardrag-mcp",
    ]
    projects = [_project(path) for path in package_paths]
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")

    for path, project in zip(package_paths, projects, strict=True):
        assert project["license"] == "Apache-2.0"
        assert project["license-files"] == ["LICENSE"]
        assert "License :: OSI Approved :: Apache Software License" in project["classifiers"]
        assert (ROOT / path / "LICENSE").read_text(encoding="utf-8") == license_text

    assert "Apache License\n                           Version 2.0, January 2004" in license_text
    assert "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION" in license_text
    assert "END OF TERMS AND CONDITIONS" in license_text

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert "[Apache License 2.0](LICENSE)" in readme
    assert "Apache License, Version 2.0" in notices
    assert "Proprietary" not in notices


def test_default_deployment_has_only_worker_and_mcp() -> None:
    root_compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "deploy/worker/compose.yaml" in root_compose
    assert "deploy/mcp/compose.yaml" in root_compose

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^FROM worker-runtime AS worker$", dockerfile, re.MULTILINE)
    assert re.search(r"^FROM runtime AS mcp$", dockerfile, re.MULTILINE)
    lowered = dockerfile.casefold()
    assert "postgres" not in lowered
    assert "keycloak" not in lowered
    assert "from runtime as admin" not in lowered


def test_v114_patch_candidate_deployment_isolated_from_stable_runtime() -> None:
    root_env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    worker_base = (ROOT / "deploy/worker/compose.yaml").read_text(encoding="utf-8")
    mcp_base = (ROOT / "deploy/mcp/compose.yaml").read_text(encoding="utf-8")
    worker = (ROOT / "deploy/worker/compose.candidate.yaml").read_text(encoding="utf-8")
    mcp = (ROOT / "deploy/mcp/compose.candidate.yaml").read_text(encoding="utf-8")
    cache_seed = (ROOT / "deploy/worker/compose.cache-seed.yaml").read_text(encoding="utf-8")

    for manifest in (worker, mcp):
        assert "candidate-v1.0.11" in manifest
        assert "shared CARDRAG_WEBDAV_BASE_URL is required" in manifest
        assert "CARDRAG_CANDIDATE_WEBDAV_BASE_URL" not in manifest
        assert "candidate-v1.0.9" not in manifest
    assert 'CARDRAG_COLLECT_REMOTE_GARBAGE: "false"' in worker
    assert 'CARDRAG_OCR_CACHE_PUBLICATION_APPROVED: "false"' in worker
    assert 'CARDRAG_REMOTE_GC_APPROVED: "false"' in worker
    assert 'CARDRAG_OCR_CACHE_MODE: "read-only"' in worker
    assert "cardrag-worker-v114-candidate-state" in worker
    assert "cardrag-worker-v114-candidate-codex-home" in worker
    assert "${CARDRAG_CANDIDATE_MCP_STATE_VOLUME:-cardrag-mcp-v114-candidate-state}" in mcp
    assert "CARDRAG_CANDIDATE_MCP_PUBLISHED_PORT:-18014" in mcp
    assert "target: /mnt/cardrag-v109-state" in cache_seed
    assert "read_only: true" in cache_seed
    assert "external: true" in cache_seed
    assert "CARDRAG_WORKER_STATE_VOLUME" in worker_base
    assert "CARDRAG_WORKER_CODEX_HOME_VOLUME" in worker_base
    assert "CARDRAG_CODEX_AUTH_ROOT=/var/lib/cardrag-codex-home" in worker_base
    assert "CODEX_HOME=/var/lib/cardrag-codex-home" in worker_base
    assert "HOME=/var/lib/cardrag-codex-home/home" in worker_base
    assert "/var/lib/cardrag-worker/codex" not in worker_base
    assert "CARDRAG_CODEX_AUTH_ROOT=/var/lib/cardrag-codex-home" in root_env_example
    assert "/var/lib/cardrag-worker/codex" not in root_env_example
    assert "CARDRAG_COLLECT_REMOTE_GARBAGE:-false" in worker_base
    assert "CARDRAG_OCR_CACHE_PUBLICATION_APPROVED:-false" in worker_base
    assert "CARDRAG_REMOTE_GC_APPROVED:-false" in worker_base
    assert "CARDRAG_OCR_CACHE_MODE:-read-only" in worker_base
    for name in (
        "CARDRAG_WORKER_MAX_STATE_BYTES",
        "CARDRAG_WORKER_RESERVED_FREE_SPACE_BYTES",
        "CARDRAG_WORKER_MAX_VECTOR_SIDECAR_BYTES",
        "CARDRAG_WORKER_MAX_SERVING_DATABASE_BYTES",
    ):
        assert name in worker_base
        assert name in worker
    assert "CARDRAG_WORKER_MINIMUM_START_FREE_BYTES:-2147483648" in worker_base
    assert 'CARDRAG_WORKER_MINIMUM_START_FREE_BYTES: "34359738368"' in worker
    assert "CARDRAG_ENABLED_ISSUERS: kb,samsung,shinhan,woori" in worker
    assert "CARDRAG_WORKER_MINIMUM_START_FREE_BYTES:-34359738368" not in worker
    assert "CARDRAG_MCP_STATE_VOLUME" in mcp_base
    assert "CARDRAG_MCP_MAX_VECTOR_SIDECAR_BYTES" in mcp_base
    assert "CARDRAG_MCP_MAX_RESIDENT_VECTOR_BYTES" in mcp_base
    assert "CARDRAG_RERANKER_SHADOW_ENABLED=${CARDRAG_RERANKER_SHADOW_ENABLED:-false}" in (mcp_base)
    assert "CARDRAG_RERANKER_SHADOW_MODEL" in mcp_base
    assert "CARDRAG_RERANKER_SHADOW_PROVIDER_ID" in mcp_base
    assert "CARDRAG_RERANKER_SHADOW_MAX_CANDIDATES" in mcp_base
    assert "CARDRAG_RERANKER_SHADOW_TIMEOUT_SECONDS" in mcp_base
    assert "CARDRAG_RERANKER_SHADOW_ENABLED" in mcp
    assert 'CARDRAG_EXPERIMENTAL_MAP_REDUCE_ENABLED: "false"' in mcp
    assert "cardrag-worker_worker-state" not in worker_base
    assert "cardrag-mcp_mcp-state" not in mcp_base
    assert "cardrag-worker-v111-state" in worker_base
    assert "cardrag-worker-v111-codex-home" in worker_base
    assert "cardrag-mcp-v111-state" in mcp_base
    assert "cardrag-worker-v114-candidate-state" not in worker_base
    assert "cardrag-worker-v114-candidate-codex-home" not in worker_base
    assert "cardrag-mcp-v114-candidate-state" not in mcp_base


def test_v114_exact_security_gate_precedes_candidate_runtime_and_volumes() -> None:
    migration = (ROOT / "docs/V1_0_14_MIGRATION.md").read_text(encoding="utf-8")
    heading = "### 1.1 Exact image 및 attestation security gate"
    security_start = migration.index(heading)
    security_end = migration.index("## 2. 격리와 보존 경계", security_start)
    security = migration[security_start:security_end]
    security_command = security.split("```bash\n", 1)[1].split("```", 1)[0]

    assert 'test "$("$trivy_bin" --version --format json | jq -er \'.Version\')" = "0.74.0"' in security
    assert 'test "$gitleaks_version" = "8.30.1"' in security
    assert '"$CANDIDATE_SOURCE_COMMIT:.gitleaks.toml"' in security
    assert 'sha256sum "$gitleaks_config"' in security
    assert security.count("for role in worker mcp; do") == 2
    assert 'exact_image="$candidate_repository@$image_digest"' in security
    for role in ("worker", "mcp"):
        assert f"trivy-image-{role}.json" in security

    assert '"$trivy_bin" image --quiet \\' in security
    for argument in (
        "--platform linux/amd64",
        "--scanners vuln,secret",
        "--severity HIGH,CRITICAL",
        "--exit-code 1",
        '--format json \\\n    --output "$image_report"',
        '"$exact_image"',
    ):
        assert argument in security
    assert "--ignore-unfixed" not in security_command
    assert "ignore_unfixed: false" in security

    assert 'attestation_evidence_root="$security_receipt_root/attestation-evidence"' in security
    assert "worker-provenance.json worker-sbom.json mcp-provenance.json mcp-sbom.json" in security
    assert security.count('"$trivy_bin" fs --quiet') == 1
    assert "--scanners secret" in security
    assert '--output "$trivy_evidence_report" \\\n  "$attestation_evidence_root"' in security
    assert security.count('--source "$attestation_evidence_root"') == 1
    assert "--no-git" in security
    assert '--config "$gitleaks_config"' in security
    assert "--redact=100" in security
    assert "--log-level error" in security
    assert "--report-format json" in security
    assert '--report-path "$gitleaks_evidence_report"' in security
    assert "printf '%s\\n' \"$trivy_evidence_report\"" not in security
    assert "printf '%s\\n' \"$gitleaks_evidence_report\"" not in security

    for receipt_contract in (
        'trivy_version_json="$security_receipt_root/trivy-version.json"',
        'metadata.get("Version") != "0.74.0"',
        'timestamp("UpdatedAt")',
        'timestamp("DownloadedAt")',
        "timedelta(hours=36)",
        "timedelta(hours=2)",
        "worker_report_sha256",
        "mcp_report_sha256",
        "trivy_evidence_sha256",
        "gitleaks_evidence_sha256",
        "trivy_version_sha256",
        'schema_version: "cardrag.v114-exact-security-gate.v1"',
    ):
        assert receipt_contract in security

    gate_complete = security_start + security.index("printf 'security gate receipt: %s\\n'")
    for runtime_or_volume_command in (
        "docker volume create",
        "worker resume-publication",
        "up --detach --no-build",
    ):
        assert gate_complete < migration.index(runtime_or_volume_command, security_end)


def test_v114_candidate_mcp_retry_can_isolate_project_and_state_volume() -> None:
    docker = shutil.which("docker")
    if docker is None:
        raise AssertionError("docker compose is required to verify the release candidate config")
    environment = os.environ.copy()
    for name in ("COMPOSE_PROJECT_NAME", "CARDRAG_CANDIDATE_MCP_STATE_VOLUME"):
        environment.pop(name, None)
    environment.update(
        {
            "CARDRAG_WEBDAV_BASE_URL": "https://shared.invalid/cardrag",
            "CARDRAG_CANDIDATE_MCP_PUBLIC_BASE_URL": "http://127.0.0.1:18014",
            "CARDRAG_CANDIDATE_MCP_IMAGE_DIGEST": "sha256:" + "b" * 64,
            "CARDRAG_MCP_STATE_VOLUME": "attacker-stable-state",
        }
    )

    def render(render_environment: dict[str, str]) -> dict[str, object]:
        result = subprocess.run(  # noqa: S603 - executable and inputs are test-controlled
            [
                docker,
                "compose",
                "-f",
                "deploy/mcp/compose.yaml",
                "-f",
                "deploy/mcp/compose.candidate.yaml",
                "config",
                "--format",
                "json",
            ],
            cwd=ROOT,
            env=render_environment,
            text=True,
            capture_output=True,
            check=True,
        )
        return json.loads(result.stdout)

    default_config = render(environment)
    assert default_config["name"] == "cardrag-v114-candidate"
    assert default_config["volumes"]["mcp-state"]["name"] == ("cardrag-mcp-v114-candidate-state")

    retry_environment = environment | {
        "COMPOSE_PROJECT_NAME": "cardrag-v114-candidate-hashcompat",
        "CARDRAG_CANDIDATE_MCP_STATE_VOLUME": ("cardrag-mcp-v114-candidate-hashcompat-state"),
    }
    retry_config = render(retry_environment)
    assert retry_config["name"] == "cardrag-v114-candidate-hashcompat"
    assert retry_config["volumes"]["mcp-state"]["name"] == ("cardrag-mcp-v114-candidate-hashcompat-state")
    assert retry_config["services"]["mcp"]["volumes"] == [
        {
            "type": "volume",
            "source": "mcp-state",
            "target": "/var/lib/cardrag-mcp",
            "volume": {},
        }
    ]


def test_v114_mcp_activation_waits_for_large_sync_and_checks_active_generation() -> None:
    migration = (ROOT / "docs/V1_0_14_MIGRATION.md").read_text(encoding="utf-8")
    base = migration.split("### 4.2 MCP start와 runtime identity", 1)[1].split(
        "### 4.3 MCP health-gate 실패 뒤 hotfix 격리 재시도",
        1,
    )[0]
    retry = migration.split("### 4.3 MCP health-gate 실패 뒤 hotfix 격리 재시도", 1)[1].split(
        "배치 receipt에는",
        1,
    )[0]

    for section in (base, retry):
        assert "set -Eeuo pipefail" in section
        assert "up --detach --no-build --pull never --no-start mcp" in section
        assert "up --detach --wait" not in section
        assert "health_deadline=$((SECONDS + 3600))" in section
        assert "sleep 30" in section
        assert ".State.Running == true" in section
        assert ".State.OOMKilled == false" in section
        assert ".RestartCount == 0" in section
        assert "starting | unhealthy" in section
        assert 'Path("/var/lib/cardrag-mcp/current.json")' in section
        assert '"cardrag.mcp-local-pointer.v1"' in section
        assert 'test "$active_generation" = "$expected_generation"' in section
        assert "trap - EXIT ERR INT TERM" in section
        assert "observed_id=$(docker inspect --format '{{.Id}}'" in section
        assert 'test "$observed_id" =' in section
        assert "ps --all --quiet mcp" in section
        for direct_name in (
            "CARDRAG_WEBDAV_USERNAME",
            "CARDRAG_WEBDAV_PASSWORD",
            "CARDRAG_MCP_BEARER_TOKEN",
            "CARDRAG_OPENROUTER_API_KEY",
        ):
            assert f".{direct_name}" in section
        for secret_path in (
            "/run/secrets/webdav_username",
            "/run/secrets/webdav_password",
            "/run/secrets/mcp_bearer_token",
            "/run/secrets/openrouter_api_key",
        ):
            assert secret_path in section

    assert 'docker stop --timeout 30 "$failed_container_id"' in retry
    assert "docker volume create" in retry
    assert "com.docker.compose.project=$retry_project" in retry
    assert "com.docker.compose.volume=mcp-state" in retry
    assert '.Labels["com.cardrag.purpose"] == "v114-hashcompat-retry"' in retry
    assert "verify_retry_volume_empty" in retry
    assert retry.count("verify_retry_volume_empty") == 3
    assert '--mount type=volume,src="$retry_volume",dst=/var/lib/cardrag-mcp,readonly,volume-nocopy' in retry
    assert '--volume "$retry_volume:/var/lib/cardrag-mcp:ro"' not in retry
    assert "metadata.st_uid" not in retry
    assert "stat.S_IMODE" not in retry
    assert retry.count('docker ps --all --quiet --filter "volume=$retry_volume"') == 2
    assert "MCPArtifactReader" in retry
    assert "retry_remote_receipt=$(docker run" in retry
    assert 'retry_remote_receipt=$("${mcp_compose[@]}" run' not in retry
    remote_probe = retry.split("retry_remote_receipt=$(docker run", 1)[1].split(
        "unset retry_remote_receipt",
        1,
    )[0]
    assert "$retry_volume" not in remote_probe
    assert '--mount type=bind,src="$webdav_username_secret"' in remote_probe
    assert '--mount type=bind,src="$webdav_password_secret"' in remote_probe
    assert "printf '%s\\n' \"$retry_remote_receipt\"" not in retry
    assert "current.pointer.generation_id != expected_generation" in retry
    assert "generation_ready_path(expected_generation)" in retry
    assert "generation_manifest_path(expected_generation)" in retry
    assert "generation_database_path(expected_generation)" in retry
    assert "generation_vectors_path(expected_generation)" in retry
    assert ".generation_id == $generation and .head_count == 5" in retry
    assert "docker volume rm" not in retry
    assert "retry_activation_complete=true" in retry
    assert retry.index("docker volume create \\") < retry.index("verify_retry_volume_empty")
    assert retry.index("retry_remote_receipt=$(") < retry.rindex("verify_retry_volume_empty")
    assert retry.rindex("verify_retry_volume_empty") < retry.index('"${mcp_compose[@]}" up --detach')
    for section, captured_id, completion in (
        (base, "$mcp_container_id", "mcp_activation_complete=true"),
        (retry, "$retry_container_id", "retry_activation_complete=true"),
    ):
        assert f'docker stop --timeout 30 "{captured_id}"' in section
        assert section.index('test "$active_generation" = "$expected_generation"') < section.index(completion)
        start = section.index(f'docker start "{captured_id}"')
        prestart = section[:start]
        assert '.State.Status == "created"' in prestart
        assert ".State.Running == false" in prestart
        assert ".State.OOMKilled == false" in prestart
        assert ".RestartCount == 0" in prestart
        assert ".Config.Image == $image" in prestart
        assert "(.[0].Image == $index_digest or .[0].Image == $config_digest)" in prestart
        assert '.Config.Labels["org.opencontainers.image.revision"] == $revision' in prestart
        assert '.Config.Labels["com.docker.compose.project"]' in prestart
        assert '.Config.Labels["com.docker.compose.service"] == "mcp"' in prestart
        assert '.HostConfig.PortBindings["8000/tcp"]' in prestart
        assert 'Destination:"/var/lib/cardrag-mcp",RW:true' in prestart
    assert "docker stop --time 30" not in migration

    stop = migration.split("## 5. 중단, rollback과 불변 경계", 1)[1]
    assert "stop_project=${CARDRAG_V114_STOP_PROJECT:-cardrag-v114-candidate}" in stop
    assert "cardrag-v114-candidate-hashcompat)" in stop
    assert "stop_mcp_volume=cardrag-mcp-v114-candidate-state" in stop
    assert "stop_mcp_volume=cardrag-mcp-v114-candidate-hashcompat-state" in stop
    assert "refusing non-allowlisted candidate project" in stop
    assert "unset COMPOSE_PROJECT_NAME CARDRAG_CANDIDATE_MCP_STATE_VOLUME" in stop
    assert 'export COMPOSE_PROJECT_NAME="$stop_project"' in stop
    assert 'export CARDRAG_CANDIDATE_MCP_STATE_VOLUME="$stop_mcp_volume"' in stop
    assert ".name == $project" in stop
    assert ".services.mcp.image == $image" in stop
    assert "ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate" in stop
    assert '.services.mcp.environment.CARDRAG_CHANNEL == "candidate-v1.0.11"' in stop
    assert '.volumes["mcp-state"].name == $volume' in stop
    assert "ps --all --quiet mcp" in stop
    assert 'docker stop --timeout 30 "$stop_mcp_container_id"' in stop
    assert '"${mcp_compose[@]}" stop mcp' not in stop
    assert (
        stop.index('mcp_render=$("${mcp_compose[@]}" config')
        < stop.index('stop_mcp_container_id=$("${mcp_compose[@]}" ps')
        < stop.index('docker stop --timeout 30 "$stop_mcp_container_id"')
    )

    for evidence in (
        "sha256:a78512283a5d7fab3809a9a7229832ee240fed514fbdf3d55dc0660e7521747d",
        "sha256:21196d1f44553fd84485eb1d9287d14aa1804ad5820de0098f590a7d329b91f1",
        "dc41f7d79a8bc446e59dc45cebf883043f5b6634",
        '.Config.Labels["com.docker.compose.project"] == "cardrag-v114-candidate"',
        '.Config.Labels["com.docker.compose.service"] == "mcp"',
        '.HostConfig.PortBindings["8000/tcp"]',
        'Name:$volume,Destination:"/var/lib/cardrag-mcp",RW:true',
    ):
        assert evidence in retry
    assert 'docker stop --timeout 30 "$failed_container_id"' in retry


def test_v112_offline_snapshot_copies_during_each_destination_first_mount() -> None:
    migration = (ROOT / "docs/V1_0_11_MIGRATION.md").read_text(encoding="utf-8")
    section = migration.split("### 3.1 stopped v111 state/auth의 v112 offline snapshot", 1)[1]
    copy_phase = section.split("복사 뒤 verifier", 1)[0]
    copy_helpers = [
        block
        for block in re.findall(r"```bash\n(.*?)```", copy_phase, flags=re.DOTALL)
        if "docker run --rm --interactive" in block
    ]

    assert len(copy_helpers) == 2
    state_helper = next(block for block in copy_helpers if "$source_state:/source:ro" in block)
    codex_helper = next(block for block in copy_helpers if "$source_codex:/source:ro" in block)
    assert "$destination_state:/var/lib/cardrag-worker" in state_helper
    assert "$destination_codex:/var/lib/cardrag-codex-home" in codex_helper
    for helper in copy_helpers:
        for contract in (
            "--pull never",
            "--network none",
            "--read-only",
            "--cap-drop ALL",
            "--security-opt no-new-privileges=true",
            "--user 10001:10001",
        ):
            assert contract in helper

    assert "v112-worker-state-root-initialized" not in copy_phase
    assert "v112-codex-home-root-initialized" not in copy_phase
    state_order = (
        state_helper.index('raise SystemExit("state_destination_initialization_invalid")'),
        state_helper.index("os.chmod(destination, 0o700, follow_symlinks=False)"),
        state_helper.index("os.fsync(root_descriptor)"),
        state_helper.index("sync_directory(source, destination)"),
        state_helper.index('print("v112-worker-state-offline-snapshot-complete")'),
    )
    assert state_order == tuple(sorted(state_order))
    assert "stat.S_IMODE(destination_root.st_mode) not in {0o700, 0o755}" in state_helper
    assert "value.st_nlink == 1" in state_helper
    assert 'raise SystemExit("state_cross_filesystem_entry")' in state_helper
    assert 'raise SystemExit("state_destination_copy_empty")' in state_helper

    codex_order = (
        codex_helper.index('raise SystemExit("codex_destination_initialization_invalid")'),
        codex_helper.index("os.chmod(destination_root, 0o700, follow_symlinks=False)"),
        codex_helper.index("os.fsync(root_fd)"),
        codex_helper.index("os.replace(temporary, destination)"),
        codex_helper.index('print("v112-codex-auth-offline-snapshot-complete")'),
    )
    assert codex_order == tuple(sorted(codex_order))
    assert "stat.S_IMODE(destination_root_stat.st_mode) not in {0o700, 0o755}" in codex_helper
    assert '{item.name for item in destination_root.iterdir()} != {"home"}' in codex_helper
    assert "or any(home.iterdir())" in codex_helper
    assert 'getattr(os, "O_NOFOLLOW", 0)' in codex_helper
    assert "source_stat.st_nlink != 1" in codex_helper


def test_v113_incident_copy_uses_exact_tool_and_image_skeleton_mounts() -> None:
    migration = (ROOT / "docs/V1_0_13_MIGRATION.md").read_text(encoding="utf-8")
    recovery_tool = ROOT / "tools/cardrag_v113_recovery_copy.py"
    section = migration.split("### 3.2 State와 Codex auth 복사", 1)[1].split(
        "### 3.3 Destination-only SQLite recovery",
        1,
    )[0]
    copy_block = next(
        block
        for block in re.findall(r"```bash\n(.*?)```", section, flags=re.DOTALL)
        if "cardrag_v113_recovery_copy.py" in block
    )

    assert copy_block.count('--volume "$recovery_copy:/opt/cardrag-v113-recovery-copy.py:ro"') == 2
    assert '--volume "$source_state:/source:ro"' in copy_block
    assert '--volume "$destination_state:/var/lib/cardrag-worker"' in copy_block
    assert "--source /source --destination /var/lib/cardrag-worker" in copy_block
    assert '--volume "$source_codex:/source:ro"' in copy_block
    assert '--volume "$destination_codex:/var/lib/cardrag-codex-home"' in copy_block
    assert "--source /source --destination /var/lib/cardrag-codex-home" in copy_block
    assert '--volume "$repository_root/tools' not in copy_block
    assert recovery_tool.stat().st_mode & 0o777 == 0o644
    assert "stat --format='%a %h' \"$recovery_copy\"" in copy_block
    for contract in (
        "--pull never",
        "--network none",
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges=true",
        "--user 10001:10001",
    ):
        assert copy_block.count(contract) == 2


def test_codex_auth_migration_procedure_is_fail_closed_and_redacted() -> None:
    migration = (ROOT / "docs/V1_0_10_MIGRATION.md").read_text(encoding="utf-8")
    section = migration.split("## 3. Codex OAuth/home 분리", 1)[1].split("## 4. v1.0.9 PDF/OCR seed", 1)[0]

    for contract in (
        "CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST",
        "CARDRAG_CANDIDATE_WORKER_CONFIG_DIGEST",
        "ghcr\\.io/kanu-coffee/mcp-card-prd-detail-candidate@sha256:",
        'test "$(docker image inspect "$CARDRAG_CANDIDATE_WORKER_IMAGE"',
        '--arg expected "$CARDRAG_CANDIDATE_WORKER_IMAGE"',
        'if {entry.name for entry in os.scandir(destination_root)} != {"home"}',
        "os.O_NOFOLLOW",
        "before = os.fstat(source_fd)",
        "identity(os.fstat(source_fd)) != identity(before)",
        "source_block != destination_block",
        'set(os.listdir(r))=={"auth.json","home"}',
        "codex-auth-copy-verified",
        "codex-auth-metadata-verified",
        "codex-login-status-verified",
        "실제 OCR provider argv smoke",
    ):
        assert contract in section
    assert section.count("--pull never") == 3
    assert "CARDRAG_CANDIDATE_WORKER_INDEX_DIGEST" not in section
    assert section.count("set -euo pipefail") == 2
    assert section.count(': "${CARDRAG_CANDIDATE_WORKER_IMAGE:?') == 2
    assert section.count("codex_volume=cardrag-worker-v110-codex-home") == 2
    assert section.count('--volume "$codex_volume:/var/lib/cardrag-codex-home:ro"') == 2
    assert '--volume "$source_volume:/source:ro"' in section
    assert "auth.json` 값, 일부 문자열, SHA-256은" in section
    assert "old state의 exact `/codex` subtree" in section
    assert "`/home`은" in section


def test_candidate_migration_resumes_the_exact_preserved_run() -> None:
    migration = (ROOT / "docs/V1_0_10_MIGRATION.md").read_text(encoding="utf-8")
    deployment = (ROOT / "deploy/README.md").read_text(encoding="utf-8")
    section = migration.split("## 5. candidate generation과 MCP", 1)[1]

    for contract in (
        '"${CARDRAG_PRESERVED_RUN_ID:?the audited interrupted run ID is required}"',
        '[[ "$CARDRAG_PRESERVED_RUN_ID" =~ ^[0-9a-f]{32}$ ]]',
        "docker run --rm -i --pull never --network none --read-only",
        'for suffix in ("-wal", "-shm")',
        'raise SystemExit("worker-state-not-checkpointed")',
        "file:/state/worker-state.sqlite3?mode=ro&immutable=1",
        'row[0] not in {"failed", "interrupted"}',
        "\"SELECT COUNT(*) FROM run WHERE status='running'\"",
        'print("preserved-run-resume-verified")',
        'worker resume "$CARDRAG_PRESERVED_RUN_ID"',
    ):
        assert contract in section
    assert 'run --name "$worker_container" worker run' not in section
    assert 'worker resume "$CARDRAG_PRESERVED_RUN_ID"' in deployment
    assert "candidate-worker-acceptance worker run" not in deployment


def test_v114_incident_recovery_uses_sealed_publication_only_resume() -> None:
    migration = (ROOT / "docs/V1_0_14_MIGRATION.md").read_text(encoding="utf-8")
    section = migration.split("## 4. Same-run sealed-publication resume", 1)[1].split(
        "### 4.1 Worker terminal",
        1,
    )[0]

    assert 'worker resume-publication "$preserved_run_id"' in section
    assert 'worker resume "$preserved_run_id"' not in section
    assert "live embedding endpoint metadata" in section
    assert "Provider, issuer discovery, OCR, embedding" in section
    assert "full local seal 검증은 한\n번뿐" in section


def test_candidate_capacity_and_issuer_contract_reject_ambient_overrides() -> None:
    docker = shutil.which("docker")
    if docker is None:
        raise AssertionError("docker compose is required to verify the release candidate config")
    environment = os.environ.copy()
    environment.update(
        {
            "CARDRAG_WEBDAV_BASE_URL": "https://shared.invalid/cardrag",
            "CARDRAG_CANDIDATE_WEBDAV_BASE_URL": "https://attacker.invalid/isolated-base",
            "CARDRAG_CANDIDATE_MCP_PUBLIC_BASE_URL": "http://127.0.0.1:18014",
            "CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST": "sha256:" + "a" * 64,
            "CARDRAG_CANDIDATE_MCP_IMAGE_DIGEST": "sha256:" + "b" * 64,
            "CARDRAG_WORKER_IMAGE": "attacker.invalid/worker:local",
            "CARDRAG_WORKER_CODEX_HOME_VOLUME": "attacker-codex-home",
            "CARDRAG_MCP_IMAGE": "attacker.invalid/mcp:local",
            "CARDRAG_ENABLED_ISSUERS": "woori",
            "CARDRAG_STABLE_PUBLICATION_APPROVED": "true",
            "CARDRAG_OCR_CACHE_PUBLICATION_APPROVED": "true",
            "CARDRAG_REMOTE_GC_APPROVED": "true",
            "CARDRAG_COLLECT_REMOTE_GARBAGE": "true",
            "CARDRAG_EXPERIMENTAL_MAP_REDUCE_ENABLED": "true",
            "CARDRAG_EMBEDDING_REQUEST_MAX_ATTEMPTS": "1",
            "CARDRAG_EMBEDDING_RETRY_BASE_SECONDS": "999",
            "CARDRAG_EMBEDDING_RETRY_CAP_SECONDS": "999",
        }
    )
    capacity = {
        "CARDRAG_WORKER_MAX_STATE_BYTES": "137438953472",
        "CARDRAG_WORKER_RESERVED_FREE_SPACE_BYTES": "2147483648",
        "CARDRAG_WORKER_MAX_VECTOR_SIDECAR_BYTES": "17179869184",
        "CARDRAG_WORKER_MAX_SERVING_DATABASE_BYTES": "34359738368",
        "CARDRAG_WORKER_MINIMUM_START_FREE_BYTES": "34359738368",
        "CARDRAG_MCP_MAX_VECTOR_BYTES": "1073741824",
        "CARDRAG_MCP_MAX_RESIDENT_VECTOR_BYTES": "1073741824",
        "CARDRAG_MCP_MAX_VECTOR_SIDECAR_BYTES": "17179869184",
        "CARDRAG_MCP_MAX_SERVING_DATABASE_BYTES": "34359738368",
        "CARDRAG_MCP_MAX_GENERATION_DOWNLOAD_BYTES": "68719476736",
        "CARDRAG_MCP_MAX_STATE_BYTES": "137438953472",
        "CARDRAG_MCP_RESERVED_FREE_SPACE_BYTES": "2147483648",
        "CARDRAG_MCP_EXHAUSTIVE_AUDIT_MAX_JOBS": "32",
        "CARDRAG_MCP_EXHAUSTIVE_AUDIT_MAX_TOTAL_BYTES": "2147483648",
        "CARDRAG_MCP_EXHAUSTIVE_AUDIT_MAX_ARTIFACT_BYTES": "268435456",
        "CARDRAG_MCP_RERANKER_AUDIT_MAX_JOBS": "1024",
        "CARDRAG_MCP_RERANKER_AUDIT_MAX_TOTAL_BYTES": "536870912",
        "CARDRAG_MCP_RERANKER_AUDIT_MAX_ARTIFACT_BYTES": "8388608",
    }
    environment.update({name: "1" for name in capacity})

    def render(role: str) -> dict[str, object]:
        result = subprocess.run(  # noqa: S603 - executable and role are test-controlled
            [
                docker,
                "compose",
                "-f",
                f"deploy/{role}/compose.yaml",
                "-f",
                f"deploy/{role}/compose.candidate.yaml",
                "config",
                "--format",
                "json",
            ],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=True,
        )
        return json.loads(result.stdout)

    worker_config = render("worker")
    worker_service = worker_config["services"]["worker"]
    worker_environment = worker_service["environment"]
    worker_volumes = {volume["target"]: volume for volume in worker_service["volumes"]}
    assert worker_service["image"] == (
        "ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate@"
        + environment["CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST"]
    )
    assert worker_service.get("build") is None
    assert worker_service["pull_policy"] == "always"
    assert worker_service["user"] == "10001:10001"
    assert worker_service["read_only"] is True
    assert worker_service["cap_drop"] == ["ALL"]
    assert worker_service["security_opt"] == [
        "no-new-privileges:true",
        "seccomp=unconfined",
        "apparmor=unconfined",
    ]
    assert "systempaths=unconfined" not in worker_service["security_opt"]
    assert "cap_add" not in worker_service
    assert worker_service.get("privileged", False) is False
    assert worker_environment["CARDRAG_ENABLED_ISSUERS"] == "kb,samsung,shinhan,woori"
    assert worker_environment["CARDRAG_STABLE_PUBLICATION_APPROVED"] == "false"
    assert worker_environment["CARDRAG_OCR_CACHE_PUBLICATION_APPROVED"] == "false"
    assert worker_environment["CARDRAG_REMOTE_GC_APPROVED"] == "false"
    assert worker_environment["CARDRAG_COLLECT_REMOTE_GARBAGE"] == "false"
    assert worker_environment["CARDRAG_EMBEDDING_REQUEST_MAX_ATTEMPTS"] == "12"
    assert worker_environment["CARDRAG_EMBEDDING_RETRY_BASE_SECONDS"] == "1"
    assert worker_environment["CARDRAG_EMBEDDING_RETRY_CAP_SECONDS"] == "60"
    assert worker_environment["CARDRAG_WEBDAV_BASE_URL"] == "https://shared.invalid/cardrag"
    assert worker_environment["CARDRAG_CODEX_AUTH_ROOT"] == "/var/lib/cardrag-codex-home"
    assert worker_environment["CODEX_HOME"] == "/var/lib/cardrag-codex-home"
    assert worker_environment["HOME"] == "/var/lib/cardrag-codex-home/home"
    assert worker_volumes["/var/lib/cardrag-worker"]["source"] == "worker-state"
    assert worker_volumes["/var/lib/cardrag-codex-home"]["source"] == "codex-home"
    assert worker_config["volumes"]["worker-state"]["name"] == ("cardrag-worker-v114-candidate-state")
    assert worker_config["volumes"]["codex-home"]["name"] == ("cardrag-worker-v114-candidate-codex-home")
    for name, expected in capacity.items():
        if name.startswith("CARDRAG_WORKER_"):
            assert worker_environment[name] == expected

    mcp_config = render("mcp")
    mcp_service = mcp_config["services"]["mcp"]
    mcp_environment = mcp_service["environment"]
    assert mcp_service["image"] == (
        "ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate@"
        + environment["CARDRAG_CANDIDATE_MCP_IMAGE_DIGEST"]
    )
    assert mcp_service.get("build") is None
    assert mcp_service["pull_policy"] == "always"
    assert mcp_service["user"] == "10001:10001"
    assert mcp_service["read_only"] is True
    assert mcp_service["cap_drop"] == ["ALL"]
    assert mcp_service["security_opt"] == ["no-new-privileges:true"]
    assert mcp_environment["CARDRAG_EXPERIMENTAL_MAP_REDUCE_ENABLED"] == "false"
    assert mcp_environment["CARDRAG_WEBDAV_BASE_URL"] == "https://shared.invalid/cardrag"
    assert mcp_config["volumes"]["mcp-state"]["name"] == ("cardrag-mcp-v114-candidate-state")
    for name, expected in capacity.items():
        if name.startswith("CARDRAG_MCP_"):
            assert mcp_environment[name] == expected

    for role, required_name in (
        ("worker", "CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST"),
        ("mcp", "CARDRAG_CANDIDATE_MCP_IMAGE_DIGEST"),
    ):
        missing_image_environment = environment.copy()
        missing_image_environment.pop(required_name)
        result = subprocess.run(  # noqa: S603 - executable and role are test-controlled
            [
                docker,
                "compose",
                "-f",
                f"deploy/{role}/compose.yaml",
                "-f",
                f"deploy/{role}/compose.candidate.yaml",
                "config",
                "--format",
                "json",
            ],
            cwd=ROOT,
            env=missing_image_environment,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode != 0
        assert "receipt-bound" in result.stderr


def test_worker_service_keeps_terminal_and_progress_output_in_journal() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    service = (ROOT / "deploy/worker/cardrag-worker.service").read_text(encoding="utf-8")
    worker_cli = (ROOT / "apps/cardrag-worker/src/cardrag_worker/cli.py").read_text(encoding="utf-8")

    assert "PYTHONUNBUFFERED=1" in dockerfile
    assert "\n    _configure_worker_logging()\n" in worker_cli
    assert "logger.setLevel(logging.INFO)" in worker_cli
    assert "StandardOutput=journal" in service
    assert "StandardError=journal" in service
    assert "SyslogIdentifier=cardrag-worker" in service
    assert "KillSignal=SIGTERM" in service
    assert "TimeoutStopSec=infinity" in service
    assert "SendSIGKILL=no" in service
    assert "SuccessExitStatus=130 143" in service
    worker_compose = (ROOT / "deploy/worker/compose.yaml").read_text(encoding="utf-8")
    assert "init: true" in worker_compose
    assert "stop_signal: SIGTERM" in worker_compose


def test_new_runtime_packages_do_not_depend_on_removed_services() -> None:
    forbidden = {"psycopg", "pgvector", "keycloak"}
    for relative in ("packages/cardrag-core", "apps/cardrag-worker", "apps/cardrag-mcp"):
        dependencies = {
            str(item).split("[")[0].split("=")[0].split(">", 1)[0]
            for item in _project(relative)["dependencies"]
        }
        assert dependencies.isdisjoint(forbidden), (relative, dependencies & forbidden)


def test_public_environment_contract_is_documented_exactly() -> None:
    sample = (ROOT / ".env.example").read_text(encoding="utf-8")
    required = {
        "CARDRAG_WEBDAV_BASE_URL",
        "CARDRAG_WEBDAV_USERNAME_FILE",
        "CARDRAG_WEBDAV_PASSWORD_FILE",
        "CARDRAG_WEBDAV_CONNECT_TIMEOUT_SECONDS",
        "CARDRAG_WEBDAV_TRANSFER_TIMEOUT_SECONDS",
        "CARDRAG_OCR_CACHE_MODE",
        "CARDRAG_OCR_CACHE_PUBLICATION_APPROVED",
        "CARDRAG_WORKER_MAX_STATE_BYTES",
        "CARDRAG_WORKER_RESERVED_FREE_SPACE_BYTES",
        "CARDRAG_WORKER_MAX_VECTOR_SIDECAR_BYTES",
        "CARDRAG_WORKER_MAX_SERVING_DATABASE_BYTES",
        "CARDRAG_WORKER_MINIMUM_START_FREE_BYTES",
        "CARDRAG_MCP_BEARER_TOKEN_FILE",
        "CARDRAG_MCP_MAX_VECTOR_SIDECAR_BYTES",
        "CARDRAG_MCP_MAX_RESIDENT_VECTOR_BYTES",
        "CARDRAG_ENABLED_ISSUERS",
        "CARDRAG_RERANKER_SHADOW_ENABLED",
        "CARDRAG_RERANKER_SHADOW_MODEL",
        "CARDRAG_RERANKER_SHADOW_PROVIDER_ID",
        "CARDRAG_RERANKER_SHADOW_MAX_CANDIDATES",
        "CARDRAG_RERANKER_SHADOW_TIMEOUT_SECONDS",
    }
    assert all(f"{name}=" in sample for name in required)
    assert "CARDRAG_DATABASE_URL" not in sample
    assert "CARDRAG_OIDC_" not in sample


def test_removed_mcp_contract_is_absent_from_new_package() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in (ROOT / "apps/cardrag-mcp/src").rglob("*.py")
    )
    for removed in (
        "get_product_versions",
        "include_png",
        "conflicting_versions",
        "oidc",
        "keycloak",
    ):
        assert removed not in source.casefold()


def test_retired_runtime_and_operations_tree_is_absent() -> None:
    removed_paths = (
        "src/cardrag",
        "deploy/codex",
        "deploy/keycloak",
        "deploy/monitoring",
        "deploy/portainer",
        "deploy/postgres",
        "deploy/secrets",
        "deploy/systemd",
        "docs/adr",
        "reports",
        "scripts",
        "security",
        "tests/integration",
        "tests/load",
        "tests/unit",
    )
    assert all(not (ROOT / path).exists() for path in removed_paths)

    root_project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    lockfile = (ROOT / "uv.lock").read_text(encoding="utf-8")
    assert "cardrag-legacy" not in root_project
    assert 'name = "cardrag-legacy"' not in lockfile
    assert "psycopg" not in root_project
    assert "pgvector" not in root_project
