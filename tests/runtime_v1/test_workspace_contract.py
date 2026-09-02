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
    assert versions == {"1.0.13"}
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


def test_v113_patch_candidate_deployment_isolated_from_stable_runtime() -> None:
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
    assert "cardrag-worker-v113-candidate-state" in worker
    assert "cardrag-worker-v113-candidate-codex-home" in worker
    assert "cardrag-mcp-v113-candidate-state" in mcp
    assert "CARDRAG_CANDIDATE_MCP_PUBLISHED_PORT:-18013" in mcp
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
    assert "cardrag-worker-v113-candidate-state" not in worker_base
    assert "cardrag-worker-v113-candidate-codex-home" not in worker_base
    assert "cardrag-mcp-v113-candidate-state" not in mcp_base


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


def test_candidate_capacity_and_issuer_contract_reject_ambient_overrides() -> None:
    docker = shutil.which("docker")
    if docker is None:
        raise AssertionError("docker compose is required to verify the release candidate config")
    environment = os.environ.copy()
    environment.update(
        {
            "CARDRAG_WEBDAV_BASE_URL": "https://shared.invalid/cardrag",
            "CARDRAG_CANDIDATE_WEBDAV_BASE_URL": "https://attacker.invalid/isolated-base",
            "CARDRAG_CANDIDATE_MCP_PUBLIC_BASE_URL": "http://127.0.0.1:18013",
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
    assert worker_config["volumes"]["worker-state"]["name"] == ("cardrag-worker-v113-candidate-state")
    assert worker_config["volumes"]["codex-home"]["name"] == ("cardrag-worker-v113-candidate-codex-home")
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
    assert mcp_config["volumes"]["mcp-state"]["name"] == ("cardrag-mcp-v113-candidate-state")
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
