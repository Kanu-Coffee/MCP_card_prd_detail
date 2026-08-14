#!/bin/sh
set -eu

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
unit_root=$repository_root/deploy/portainer/systemd

systemd-analyze verify \
    "$unit_root/cardrag-portainer-daily.service" \
    "$unit_root/cardrag-portainer-daily.timer" \
    "$unit_root/cardrag-portainer-retention.service" \
    "$unit_root/cardrag-portainer-retention.timer"

python3 - "$unit_root" "$repository_root/deploy/portainer/cardrag-stack.yaml" <<'PY'
import json
import subprocess
import sys
import stat
from pathlib import Path

unit_root = Path(sys.argv[1])
stack_path = Path(sys.argv[2])
portainer_root = stack_path.parent
for executable in (
    portainer_root / "render-bootstrap-stack.sh",
    portainer_root / "tests" / "test-systemd.sh",
):
    assert executable.stat().st_mode & stat.S_IXUSR, executable

expected = {
    "cardrag-portainer-daily.service": "run-daily",
    "cardrag-portainer-retention.service": "retention-prune",
}
for filename, operation in expected.items():
    text = (unit_root / filename).read_text(encoding="utf-8")
    exec_line = next(line for line in text.splitlines() if line.startswith("ExecStart="))
    required = (
        "/usr/bin/docker compose ",
        "--project-name cardrag ",
        "--env-file /etc/cardrag/stack.env ",
        "--file /opt/cardrag/deploy/portainer/cardrag-stack.yaml ",
        "--profile ops run --rm --no-deps --pull never -T ",
        "--env CARDRAG_ADMIN_OPERATION_ENABLED=true ",
        f"--env CARDRAG_ADMIN_OPERATION={operation} admin",
    )
    assert all(fragment in exec_line for fragment in required), (filename, exec_line)
    assert "compose.yaml" not in exec_line.replace("cardrag-stack.yaml", ""), exec_line
    assert "--build" not in exec_line, exec_line
    assert "User=cardrag" in text and "Group=docker" in text, filename

environment = {
    "CARDRAG_ADMIN_IMAGE": "admin@sha256:" + "a" * 64,
    "CARDRAG_WORKER_IMAGE": "worker@sha256:" + "b" * 64,
    "CARDRAG_MCP_IMAGE": "mcp@sha256:" + "c" * 64,
}
rendered = subprocess.run(
    ["docker", "compose", "-f", str(stack_path), "--profile", "ops", "config", "--format", "json"],
    check=True,
    capture_output=True,
    text=True,
    env={**__import__("os").environ, **environment},
)
stack = json.loads(rendered.stdout)
admin = stack["services"]["admin"]
assert "build" not in admin
assert admin["image"].startswith("admin@sha256:")
mounts = {item["target"]: item for item in admin["volumes"]}
assert mounts["/var/lib/cardrag/objects"]["type"] == "bind"
assert mounts["/var/lib/cardrag/generations"]["type"] == "bind"
assert mounts["/var/lib/cardrag-build"]["type"] == "bind"
assert mounts["/var/cache/cardrag-pages"]["type"] == "bind"
assert all(item["bind"]["create_host_path"] is False for item in mounts.values())
entrypoint = admin["entrypoint"][2]
assert "run-daily) set -- cardrag run daily" in entrypoint
assert "retention-prune) set -- cardrag retention prune" in entrypoint
PY

echo "Portainer systemd timer tests passed"
