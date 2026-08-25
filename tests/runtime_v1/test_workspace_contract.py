from __future__ import annotations

import re
import tomllib
from pathlib import Path

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
    assert len({project["version"] for project in projects}) == 1


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
        "CARDRAG_MCP_BEARER_TOKEN_FILE",
        "CARDRAG_ENABLED_ISSUERS",
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
