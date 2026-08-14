#!/bin/sh
set -eu

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
temporary_root=$(mktemp -d)
trap 'rm -rf "$temporary_root"' EXIT HUP INT TERM

CARDRAG_DATA_ROOT=/srv/cardrag/runtime \
CARDRAG_IMPORT_ROOT=/srv/cardrag/imports \
CARDRAG_ARCHIVE_ROOT=/mnt/cardrag-backup/cardrag \
CARDRAG_ARCHIVE_EXPECTED_SOURCE=nas.example:/cardrag \
CARDRAG_ADMIN_IMAGE=cardrag-admin@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
CARDRAG_WORKER_IMAGE=cardrag-worker@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb \
CARDRAG_MCP_IMAGE=cardrag-mcp@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc \
CARDRAG_LEGACY_OBJECTS_VOLUME=fixture-objects \
CARDRAG_LEGACY_GENERATIONS_VOLUME=fixture-generations \
docker compose \
    -f "$repository_root/compose.yaml" \
    -f "$repository_root/deploy/dockerhub.compose.yaml" \
    -f "$repository_root/deploy/portainer/host-storage.compose.yaml" \
    -f "$repository_root/deploy/portainer/storage-migrate.compose.yaml" \
    --profile '*' config --format json >"$temporary_root/config.json"

CARDRAG_ADMIN_IMAGE=cardrag-admin@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
CARDRAG_WORKER_IMAGE=cardrag-worker@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb \
CARDRAG_MCP_IMAGE=cardrag-mcp@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc \
docker compose \
    -f "$repository_root/compose.yaml" \
    -f "$repository_root/deploy/dockerhub.compose.yaml" \
    -f "$repository_root/deploy/portainer/host-storage.compose.yaml" \
    config --services >"$temporary_root/default-services.txt"

for forbidden in admin legacy-import state-export state-restore; do
    if grep -Fxq "$forbidden" "$temporary_root/default-services.txt"; then
        echo "maintenance service was enabled by the default Portainer profile: $forbidden" >&2
        exit 1
    fi
done
grep -Fxq worker "$temporary_root/default-services.txt"

CARDRAG_ADMIN_IMAGE=cardrag-admin@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
CARDRAG_WORKER_IMAGE=cardrag-worker@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb \
CARDRAG_MCP_IMAGE=cardrag-mcp@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc \
    "$repository_root/deploy/portainer/render-stack.sh" \
    "$temporary_root/rendered-main.yaml" >/dev/null
CARDRAG_ADMIN_IMAGE=cardrag-admin@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
    "$repository_root/deploy/portainer/render-export-stack.sh" \
    "$temporary_root/rendered-export.yaml" >/dev/null
CARDRAG_ADMIN_IMAGE=cardrag-admin@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
    "$repository_root/deploy/portainer/render-restore-stack.sh" \
    "$temporary_root/rendered-restore.yaml" >/dev/null
CARDRAG_ADMIN_IMAGE=cardrag-admin@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
    "$repository_root/deploy/portainer/render-pre-migration-export-stack.sh" \
    "$temporary_root/rendered-pre-export.yaml" >/dev/null
CARDRAG_ADMIN_IMAGE=cardrag-admin@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
CARDRAG_WORKER_IMAGE=cardrag-worker@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb \
CARDRAG_MCP_IMAGE=cardrag-mcp@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc \
    "$repository_root/deploy/portainer/render-validation-stack.sh" \
    "$temporary_root/rendered-validation.yaml" >/dev/null
"$repository_root/deploy/portainer/render-bootstrap-stack.sh" \
    "$temporary_root/rendered-bootstrap.yaml" >/dev/null

# Docker Compose 2.38 omits explicit false values from its JSON rendering.
# Inspect every render input, distributed Stack, and freshly rendered Stack so
# the fail-closed host-path contract is proven independently of the serializer.
python3 - "$repository_root" "$temporary_root" <<'PY'
import re
import sys
from pathlib import Path

repository_root = Path(sys.argv[1])
temporary_root = Path(sys.argv[2])
portainer_root = repository_root / "deploy" / "portainer"
paths = [
    repository_root / "compose.yaml",
    repository_root / "deploy" / "dockerhub.compose.yaml",
    *sorted(portainer_root.glob("*.yaml")),
    *sorted(temporary_root.glob("rendered-*.yaml")),
]
checked_bind_mounts = 0
for path in paths:
    lines = path.read_text(encoding="utf-8").splitlines()
    for line_number, line in enumerate(lines):
        item = re.match(r"^(\s*)-\s+", line)
        if item is None:
            continue
        indent = len(item.group(1))
        block_end = len(lines)
        for candidate in range(line_number + 1, len(lines)):
            if not lines[candidate].strip():
                continue
            candidate_indent = len(lines[candidate]) - len(lines[candidate].lstrip())
            if candidate_indent <= indent:
                block_end = candidate
                break
        block = lines[line_number:block_end]
        if not any(
            re.match(r"^\s*(?:-\s*)?type:\s*bind\s*$", block_line)
            for block_line in block
        ):
            continue
        checked_bind_mounts += 1
        explicit_false = sum(
            bool(re.match(r"^\s*create_host_path:\s*false\s*$", block_line))
            for block_line in block
        )
        assert explicit_false == 1, (path, line_number + 1, explicit_false)

assert checked_bind_mounts > 0
PY

# Checked-in Portainer artifacts are the files operators paste into Portainer.
# Compare their normalized models because Compose versions wrap strings and
# quote booleans differently. The raw-YAML gate above preserves explicit
# create_host_path:false while this semantic comparison detects stale services,
# commands, dependencies, and mounts.
for pair in \
    "rendered-main.yaml:cardrag-stack.yaml" \
    "rendered-export.yaml:cardrag-export-stack.yaml" \
    "rendered-restore.yaml:cardrag-restore-stack.yaml" \
    "rendered-pre-export.yaml:cardrag-pre-migration-export-stack.yaml" \
    "rendered-validation.yaml:cardrag-validation-stack.yaml" \
    "rendered-bootstrap.yaml:cardrag-bootstrap-stack.yaml"
do
    rendered=${pair%%:*}
    checked=${pair#*:}
    docker compose -f "$temporary_root/$rendered" \
        config --no-interpolate --no-path-resolution --format json \
        >"$temporary_root/$rendered.json"
    docker compose -f "$repository_root/deploy/portainer/$checked" \
        config --no-interpolate --no-path-resolution --format json \
        >"$temporary_root/$checked.json"
    python3 - "$temporary_root/$rendered.json" \
        "$temporary_root/$checked.json" "$checked" <<'PY'
import json
import sys
from pathlib import Path

rendered = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
checked = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
assert rendered == checked, f"checked-in Portainer Stack is stale: {sys.argv[3]}"
PY
done
CARDRAG_ADMIN_IMAGE=cardrag-admin@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
CARDRAG_WORKER_IMAGE=cardrag-worker@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb \
CARDRAG_MCP_IMAGE=cardrag-mcp@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc \
docker compose -f "$temporary_root/rendered-main.yaml" --profile '*' \
    config --format json >"$temporary_root/rendered-main.json"
CARDRAG_ADMIN_IMAGE=cardrag-admin@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
CARDRAG_WORKER_IMAGE=cardrag-worker@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb \
CARDRAG_MCP_IMAGE=cardrag-mcp@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc \
docker compose -f "$temporary_root/rendered-validation.yaml" \
    config --format json >"$temporary_root/rendered-validation.json"
docker compose -f "$temporary_root/rendered-bootstrap.yaml" \
    config --format json >"$temporary_root/rendered-bootstrap.json"

CARDRAG_ADMIN_IMAGE=cardrag-admin@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
CARDRAG_ARCHIVE_EXPECTED_SOURCE=nas.example:/cardrag \
docker compose \
    -f "$repository_root/deploy/portainer/restore-stack.compose.yaml" \
    config --format json >"$temporary_root/restore-config.json"

CARDRAG_ADMIN_IMAGE=cardrag-admin@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
CARDRAG_ARCHIVE_EXPECTED_SOURCE=nas.example:/cardrag \
docker compose \
    -f "$repository_root/deploy/portainer/export-stack.compose.yaml" \
    config --format json >"$temporary_root/export-config.json"

CARDRAG_ADMIN_IMAGE=cardrag-admin@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
CARDRAG_ARCHIVE_EXPECTED_SOURCE=nas.example:/cardrag \
CARDRAG_LEGACY_OBJECTS_VOLUME=fixture-objects \
CARDRAG_LEGACY_GENERATIONS_VOLUME=fixture-generations \
docker compose \
    -f "$repository_root/deploy/portainer/export-stack.compose.yaml" \
    -f "$repository_root/deploy/portainer/legacy-export-storage.compose.yaml" \
    config --format json >"$temporary_root/pre-migration-export-config.json"

# Base Compose remains compatible with systemd/direct admin command overrides;
# the Portainer-only overlay owns the fail-closed operation allow-list.
docker compose -f "$repository_root/compose.yaml" --profile ops \
    config --format json >"$temporary_root/base-config.json"

python3 - "$temporary_root/config.json" "$temporary_root/restore-config.json" \
    "$temporary_root/export-config.json" \
    "$temporary_root/pre-migration-export-config.json" \
    "$temporary_root/rendered-main.json" \
    "$temporary_root/rendered-validation.json" \
    "$temporary_root/base-config.json" \
    "$temporary_root/rendered-bootstrap.json" <<'PY'
import json
import sys
from pathlib import Path

config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
services = config["services"]
base_services = json.loads(Path(sys.argv[7]).read_text(encoding="utf-8"))["services"]
assert base_services["admin"].get("entrypoint") is None
assert base_services["admin"]["command"] == ["cardrag", "--help"]

def volumes(service: str) -> dict[str, dict]:
    return {entry["target"]: entry for entry in services[service].get("volumes", [])}

expected_bind_sources = {
    "/var/lib/cardrag/objects": "/srv/cardrag/runtime/objects",
    "/var/lib/cardrag/generations": "/srv/cardrag/runtime/generations",
    "/var/lib/cardrag-build": "/srv/cardrag/runtime/build",
    "/var/cache/cardrag-pages": "/srv/cardrag/runtime/page-cache",
}
for service in ("volume-init", "admin", "worker", "mcp"):
    mounted = volumes(service)
    for target in set(mounted) & set(expected_bind_sources):
        entry = mounted[target]
        assert entry["type"] == "bind", (service, target, entry)
        assert entry["source"] == expected_bind_sources[target], (service, target, entry)
        assert entry.get("bind", {}).get("create_host_path", False) is False, (
            service, target, entry
        )

assert volumes("worker")["/var/lib/cardrag/generations"]["read_only"] is True
assert "/var/cache/cardrag-pages" not in volumes("worker")
assert volumes("mcp")["/var/lib/cardrag/objects"]["read_only"] is True
assert volumes("mcp")["/var/lib/cardrag/generations"]["read_only"] is True
assert "/var/lib/cardrag-build" not in volumes("mcp")
assert volumes("postgres")["/docker-entrypoint-initdb.d/10-cardrag-databases.sh"]["source"] == \
    "/srv/cardrag/config/postgres/init-databases.sh"
assert volumes("keycloak")["/opt/keycloak/data/import/cardrag-realm.json"]["source"] == \
    "/srv/cardrag/config/keycloak/cardrag-realm.json"

legacy = volumes("legacy-import")
assert legacy["/mnt/cardrag-imports"]["read_only"] is True
assert "/mnt/cardrag-archive" not in legacy
assert services["legacy-import"]["environment"]["CARDRAG_IMPORT_ROOT"] == \
    "/mnt/cardrag-imports"
assert services["legacy-import"]["environment"]["CARDRAG_LEGACY_OPERATION"] == \
    "import"
entrypoint_script = services["legacy-import"]["entrypoint"][2]
assert 'set -- cardrag legacy resume "$${CARDRAG_LEGACY_IMPORT_ID}"' in entrypoint_script
assert "/var/lib/cardrag-build /mnt/cardrag-imports" not in entrypoint_script

assert "state-export" not in services
assert "state-restore" not in services

migration = volumes("storage-migrate")
assert migration["/mnt/cardrag-source/objects"]["read_only"] is True
assert migration["/mnt/cardrag-source/generations"]["read_only"] is True
assert migration["/var/lib/cardrag/objects"]["type"] == "bind"
assert migration["/var/lib/cardrag/objects"].get("bind", {}).get(
    "create_host_path", False
) is False
assert services["storage-migrate"]["network_mode"] == "none"

assert config["volumes"]["postgres_data"]["external"] is True
assert config["volumes"]["postgres_data"]["name"] == "cardrag-postgres-v1"
assert config["volumes"]["codex_auth"]["external"] is True
assert config["volumes"]["codex_auth"]["name"] == "cardrag-codex-auth-v1"
assert services["admin"].get("profiles") == ["ops"]
admin_script = services["admin"]["entrypoint"][2]
assert services["admin"]["environment"]["CARDRAG_ADMIN_OPERATION_ENABLED"] == "false"
assert "unsupported CARDRAG_ADMIN_OPERATION" in admin_script
for operation in (
    "legacy-status", "legacy-finalize", "run-list", "run-bulk", "run-daily",
    "run-status", "run-finalize", "job-status", "generation-verify",
    "retention-prune",
):
    assert operation in admin_script
assert services["legacy-import"].get("profiles") == ["legacy-import"]
assert not services["worker"].get("profiles"), services["worker"].get("profiles")
for service in ("admin", "legacy-import", "worker", "mcp"):
    assert all(entry["target"] != "/mnt/cardrag-archive" for entry in volumes(service).values())
assert "/mnt/cardrag-imports" not in volumes("worker")
assert "/mnt/cardrag-imports" not in volumes("mcp")

restore_stack = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
assert set(restore_stack["services"]) == {"postgres", "state-restore"}
assert restore_stack["services"]["postgres"]["restart"] == "no"
assert "--dbname cardrag" not in " ".join(
    restore_stack["services"]["postgres"]["healthcheck"]["test"]
)
dedicated_restore = restore_stack["services"]["state-restore"]
assert not dedicated_restore.get("profiles")
assert dedicated_restore["image"] == \
    "cardrag-admin@sha256:" + "a" * 64
assert dedicated_restore["command"][-2:] == ["--empty-target", "--verify-restored"]
assert dedicated_restore["environment"]["CARDRAG_STATE_PACKAGE_PATH"] == \
    "/mnt/cardrag-archive/READY-NOT-SET"
dedicated_volumes = {
    item["target"]: item for item in dedicated_restore.get("volumes", [])
}
assert dedicated_volumes["/mnt/cardrag-runtime"]["source"] == "/srv/cardrag/runtime"
assert not dedicated_volumes["/mnt/cardrag-runtime"].get("read_only", False)
assert dedicated_volumes["/mnt/cardrag-archive"]["read_only"] is True
assert dedicated_volumes["/mnt/cardrag-deployment"]["read_only"] is True
assert all(item["type"] == "bind" for item in dedicated_volumes.values())
assert all(
    item.get("bind", {}).get("create_host_path", False) is False
    for item in dedicated_volumes.values()
)
assert restore_stack["networks"]["backend"]["internal"] is True
assert restore_stack["volumes"]["postgres_data"]["external"] is True

export_stack = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
assert set(export_stack["services"]) == {"postgres", "state-export"}
assert export_stack["services"]["postgres"]["restart"] == "no"
assert "--dbname cardrag" not in " ".join(
    export_stack["services"]["postgres"]["healthcheck"]["test"]
)
dedicated_export = export_stack["services"]["state-export"]
assert not dedicated_export.get("profiles")
assert dedicated_export["image"] == \
    "cardrag-admin@sha256:" + "a" * 64
assert "--self-contained" not in dedicated_export["command"]
assert dedicated_export["environment"]["CARDRAG_STATE_SELF_CONTAINED"] == "false"
assert 'set -- "$$@" --self-contained' in dedicated_export["entrypoint"][2]
dedicated_export_volumes = {
    item["target"]: item for item in dedicated_export.get("volumes", [])
}
assert dedicated_export_volumes["/var/lib/cardrag/objects"]["read_only"] is True
assert dedicated_export_volumes["/var/lib/cardrag/generations"]["read_only"] is True
assert dedicated_export_volumes["/mnt/cardrag-imports"]["read_only"] is True
assert dedicated_export_volumes["/mnt/cardrag-deployment"]["read_only"] is True
assert not dedicated_export_volumes["/mnt/cardrag-archive"].get("read_only", False)
assert all(item["type"] == "bind" for item in dedicated_export_volumes.values())
assert all(
    item.get("bind", {}).get("create_host_path", False) is False
    for item in dedicated_export_volumes.values()
)
assert export_stack["networks"]["backend"]["internal"] is True
assert {
    item["source"] for item in export_stack["services"]["postgres"].get("secrets", [])
} == {"postgres_admin_password"}
assert all(
    item["target"] != "/docker-entrypoint-initdb.d/10-cardrag-databases.sh"
    for item in export_stack["services"]["postgres"].get("volumes", [])
)

pre_migration_export = json.loads(Path(sys.argv[4]).read_text(encoding="utf-8"))
assert set(pre_migration_export["services"]) == {
    "postgres", "schema13-safety-backup", "schema14-upgrade", "state-export"
}
backup = pre_migration_export["services"]["schema13-safety-backup"]
upgrade = pre_migration_export["services"]["schema14-upgrade"]
assert backup["restart"] == "no" and upgrade["restart"] == "no"
assert backup["environment"]["CARDRAG_SCHEMA13_TRANSITION_ENABLED"] == "false"
assert backup["environment"]["CARDRAG_SCHEMA13_TRANSITION_ID"] == "READY-NOT-SET"
assert set(backup["depends_on"]) == {"postgres"}
assert set(upgrade["depends_on"]) == {"postgres", "schema13-safety-backup"}
assert set(pre_migration_export["services"]["state-export"]["depends_on"]) == {
    "postgres", "schema14-upgrade"
}
assert pre_migration_export["services"]["state-export"]["environment"][
    "CARDRAG_STATE_EXPORT_ENABLED"
] == "false"
for service in (backup, upgrade):
    assert {item["source"] for item in service.get("secrets", [])} == {
        "postgres_admin_password"
    }
    assert all(item["type"] == "bind" for item in service.get("volumes", []))
    assert all(
        item.get("bind", {}).get("create_host_path", False) is False
        for item in service.get("volumes", [])
    )
upgrade_mounts = {item["target"]: item for item in upgrade["volumes"]}
assert upgrade_mounts["/mnt/cardrag-archive"]["read_only"] is True
backup_mounts = {item["target"]: item for item in backup["volumes"]}
assert not backup_mounts["/mnt/cardrag-archive"].get("read_only", False)
assert backup_mounts["/opt/cardrag-transition/migrations"]["read_only"] is True
pre_export_volumes = {
    item["target"]: item
    for item in pre_migration_export["services"]["state-export"]["volumes"]
}
assert pre_export_volumes["/var/lib/cardrag/objects"] == {
    "type": "volume",
    "source": "legacy_objects",
    "target": "/var/lib/cardrag/objects",
    "read_only": True,
}
assert pre_export_volumes["/var/lib/cardrag/generations"]["type"] == "volume"
assert pre_export_volumes["/var/lib/cardrag/generations"]["read_only"] is True
assert pre_migration_export["volumes"]["legacy_objects"] == {
    "name": "fixture-objects", "external": True
}
assert pre_migration_export["volumes"]["legacy_generations"] == {
    "name": "fixture-generations", "external": True
}

rendered_main = json.loads(Path(sys.argv[5]).read_text(encoding="utf-8"))
assert "state-export" not in rendered_main["services"]
assert "state-restore" not in rendered_main["services"]
for service in ("volume-init", "admin", "legacy-import", "migrate", "worker", "mcp"):
    assert "build" not in rendered_main["services"][service], service

validation = json.loads(Path(sys.argv[6]).read_text(encoding="utf-8"))
assert validation["name"] == "cardrag-validation"
assert set(validation["services"]) == {
    "postgres", "keycloak", "volume-init", "migrate", "admin", "mcp"
}
assert "worker" not in validation["services"]
assert "legacy-import" not in validation["services"]
assert validation["services"]["admin"]["environment"][
    "CARDRAG_VALIDATION_ROLLBACK_ENABLED"
] == "false"

bootstrap = json.loads(Path(sys.argv[8]).read_text(encoding="utf-8"))
assert bootstrap["name"] == "cardrag-bootstrap"
assert set(bootstrap["services"]) == {"postgres", "keycloak"}
assert bootstrap["volumes"] == {
    "postgres_data": {"name": "cardrag-postgres-v1", "external": True}
}
assert set(bootstrap["secrets"]) == {
    "postgres_admin_password",
    "cardrag_db_password",
    "cardrag_worker_db_password",
    "cardrag_mcp_db_password",
    "keycloak_db_password",
    "keycloak_admin_password",
}
bootstrap_keycloak = bootstrap["services"]["keycloak"]
assert bootstrap_keycloak["environment"]["KC_BOOTSTRAP_ADMIN_USERNAME"] == \
    "cardrag-bootstrap"
assert bootstrap_keycloak["environment"]["KC_BOOTSTRAP_ADMIN_PASSWORD_FILE"] == \
    "/run/secrets/keycloak_admin_password"
assert {item["source"] for item in bootstrap_keycloak["secrets"]} == {
    "keycloak_db_password", "keycloak_admin_password"
}
assert all(item["type"] == "bind" for item in bootstrap_keycloak["volumes"])
assert all(
    item.get("bind", {}).get("create_host_path", False) is False
    for item in bootstrap_keycloak["volumes"]
)
assert {item["source"] for item in bootstrap["services"]["postgres"]["secrets"]} == {
    "postgres_admin_password",
    "cardrag_db_password",
    "cardrag_worker_db_password",
    "cardrag_mcp_db_password",
    "keycloak_db_password",
}
assert "keycloak_admin_password" not in rendered_main["secrets"]
assert "KC_BOOTSTRAP_ADMIN_PASSWORD_FILE" not in \
    rendered_main["services"]["keycloak"]["environment"]
PY

echo "Portainer Compose tests passed"
