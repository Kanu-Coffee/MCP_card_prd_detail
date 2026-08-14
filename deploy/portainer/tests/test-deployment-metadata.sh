#!/bin/sh
set -eu

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
temporary_root=$(mktemp -d)
trap 'rm -rf "$temporary_root"' EXIT HUP INT TERM

fake_bin=$temporary_root/bin
metadata=$temporary_root/metadata
mkdir -p "$fake_bin" "$metadata"
printf '%s\n' '#!/bin/sh' 'printf "%s\n" 0' >"$fake_bin/id"
printf '%s\n' '#!/bin/sh' \
    'args=""' \
    'for arg do' \
    '  case "$arg" in -o|-g) skip=1 ;; root) if [ "${skip:-0}" = 1 ]; then skip=0; else args="$args root"; fi ;; *) args="$args $arg" ;; esac' \
    'done' \
    'exec /usr/bin/install $args' >"$fake_bin/install"
chmod 0755 "$fake_bin/id" "$fake_bin/install"
printf '%s\n' \
    'services:' \
    '  admin:' \
    '    image: repo/admin@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' \
    '  worker:' \
    '    image: repo/worker@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' \
    '  mcp:' \
    '    image: repo/mcp@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc' \
    '    environment:' \
    '      CARDRAG_DATABASE_URL_FILE: /run/secrets/cardrag_database_url' \
    >"$temporary_root/stack.yaml"
python3 - "$temporary_root/release.json" <<'PY'
import json
import sys

roles = {}
for role, digest in (("admin", "a" * 64), ("worker", "b" * 64), ("mcp", "c" * 64)):
    roles[role] = {
        "schema": "cardrag.container-release-part.v3",
        "role": role,
        "image": f"repo/{role}",
        "digest": f"sha256:{digest}",
        "version": "0.2.0",
        "git_sha": "f" * 40,
    }
with open(sys.argv[1], "w", encoding="utf-8") as stream:
    json.dump(
        {
            "schema": "cardrag.container-release.v3",
            "version": "0.2.0",
            "git_sha": "f" * 40,
            "roles": roles,
        },
        stream,
    )
PY

PATH=$fake_bin:$PATH \
CARDRAG_DEPLOYMENT_ROOT=$metadata \
CARDRAG_ADMIN_IMAGE=repo/admin@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
CARDRAG_WORKER_IMAGE=repo/worker@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb \
CARDRAG_MCP_IMAGE=repo/mcp@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc \
    "$repository_root/deploy/portainer/install-deployment-metadata.sh" \
    "$temporary_root/stack.yaml" "$temporary_root/release.json" >/dev/null

test "$(find "$metadata" -mindepth 1 -maxdepth 1 -type f | wc -l)" -eq 4
python3 - "$metadata/image-digests.json" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as stream:
    value = json.load(stream)
assert value["schema_version"] == "cardrag-image-digests.v1"
assert set(value["images"]) == {"admin", "worker", "mcp"}
assert all("@sha256:" in image for image in value["images"].values())
PY
python3 - "$metadata" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
commit = json.loads((root / "deployment-set.json").read_text(encoding="utf-8"))
assert commit["schema_version"] == "cardrag-deployment-set.v1"
for name, digest in commit["files"].items():
    assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
PY

# The real checked-in Stack contains fail-closed image placeholders.  The
# installer must resolve exactly those placeholders to the signed digests in
# the stored redacted deployment snapshot.
checked_metadata=$temporary_root/checked-metadata
PATH=$fake_bin:$PATH \
CARDRAG_DEPLOYMENT_ROOT=$checked_metadata \
CARDRAG_ADMIN_IMAGE=repo/admin@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
CARDRAG_WORKER_IMAGE=repo/worker@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb \
CARDRAG_MCP_IMAGE=repo/mcp@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc \
    "$repository_root/deploy/portainer/install-deployment-metadata.sh" \
    "$repository_root/deploy/portainer/cardrag-stack.yaml" \
    "$temporary_root/release.json" >/dev/null
for image in \
    repo/admin@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
    repo/worker@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb \
    repo/mcp@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
do
    grep -F "$image" "$checked_metadata/stack-redacted.yaml" >/dev/null
done
if grep -F 'CARDRAG_ADMIN_IMAGE' "$checked_metadata/stack-redacted.yaml" >/dev/null \
   || grep -F 'CARDRAG_WORKER_IMAGE' "$checked_metadata/stack-redacted.yaml" >/dev/null \
   || grep -F 'CARDRAG_MCP_IMAGE' "$checked_metadata/stack-redacted.yaml" >/dev/null; then
    echo "deployment metadata retained an unresolved runtime image variable" >&2
    exit 1
fi

if PATH=$fake_bin:$PATH \
   CARDRAG_DEPLOYMENT_ROOT=$temporary_root/rejected \
   CARDRAG_ADMIN_IMAGE=repo/admin:0.2.0 \
   CARDRAG_WORKER_IMAGE=repo/worker@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb \
   CARDRAG_MCP_IMAGE=repo/mcp@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc \
       "$repository_root/deploy/portainer/install-deployment-metadata.sh" \
       "$temporary_root/stack.yaml" "$temporary_root/release.json" >/dev/null 2>&1; then
    echo "deployment metadata installer accepted a mutable image tag" >&2
    exit 1
fi

printf '%s\n' 'services:' '  app:' '    environment:' \
    '      - DATABASE_PASSWORD=supersecret' >"$temporary_root/stack-list-secret.yaml"
if PATH=$fake_bin:$PATH \
   CARDRAG_DEPLOYMENT_ROOT=$temporary_root/rejected-list-secret \
   CARDRAG_ADMIN_IMAGE=repo/admin@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
   CARDRAG_WORKER_IMAGE=repo/worker@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb \
   CARDRAG_MCP_IMAGE=repo/mcp@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc \
       "$repository_root/deploy/portainer/install-deployment-metadata.sh" \
       "$temporary_root/stack-list-secret.yaml" "$temporary_root/release.json" >/dev/null 2>&1; then
    echo "deployment metadata installer accepted a list-form environment secret" >&2
    exit 1
fi

printf '%s\n' 'services:' '  app:' '    environment:' \
    '      - "DATABASE_PASSWORD=supersecret" # must be rejected' \
    >"$temporary_root/stack-quoted-list-secret.yaml"
if PATH=$fake_bin:$PATH \
   CARDRAG_DEPLOYMENT_ROOT=$temporary_root/rejected-quoted-list-secret \
   CARDRAG_ADMIN_IMAGE=repo/admin@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
   CARDRAG_WORKER_IMAGE=repo/worker@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb \
   CARDRAG_MCP_IMAGE=repo/mcp@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc \
       "$repository_root/deploy/portainer/install-deployment-metadata.sh" \
       "$temporary_root/stack-quoted-list-secret.yaml" "$temporary_root/release.json" \
       >/dev/null 2>&1; then
    echo "deployment metadata installer accepted a quoted list-form secret" >&2
    exit 1
fi

printf '%s\n' 'services:' '  app:' \
    '    DATABASE_PASSWORD: ${DB_PASSWORD:-supersecret}' \
    >"$temporary_root/stack-default-secret.yaml"
if PATH=$fake_bin:$PATH \
   CARDRAG_DEPLOYMENT_ROOT=$temporary_root/rejected-default-secret \
   CARDRAG_ADMIN_IMAGE=repo/admin@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
   CARDRAG_WORKER_IMAGE=repo/worker@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb \
   CARDRAG_MCP_IMAGE=repo/mcp@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc \
       "$repository_root/deploy/portainer/install-deployment-metadata.sh" \
       "$temporary_root/stack-default-secret.yaml" "$temporary_root/release.json" >/dev/null 2>&1; then
    echo "deployment metadata installer accepted a secret interpolation default" >&2
    exit 1
fi

printf '%s\n' '{"env":["API_TOKEN=supersecret"]}' >"$temporary_root/release-secret.json"
if PATH=$fake_bin:$PATH \
   CARDRAG_DEPLOYMENT_ROOT=$temporary_root/rejected-release-secret \
   CARDRAG_ADMIN_IMAGE=repo/admin@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
   CARDRAG_WORKER_IMAGE=repo/worker@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb \
   CARDRAG_MCP_IMAGE=repo/mcp@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc \
       "$repository_root/deploy/portainer/install-deployment-metadata.sh" \
       "$temporary_root/stack.yaml" "$temporary_root/release-secret.json" >/dev/null 2>&1; then
    echo "deployment metadata installer accepted a release-manifest secret" >&2
    exit 1
fi

printf '%s\n' 'services:' '  app:' \
    '    CARDRAG_DATABASE_URL: postgresql://cardrag:plain-password@postgres/cardrag' \
    >"$temporary_root/stack-secret.yaml"
if PATH=$fake_bin:$PATH \
   CARDRAG_DEPLOYMENT_ROOT=$temporary_root/rejected-secret \
   CARDRAG_ADMIN_IMAGE=repo/admin@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
   CARDRAG_WORKER_IMAGE=repo/worker@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb \
   CARDRAG_MCP_IMAGE=repo/mcp@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc \
       "$repository_root/deploy/portainer/install-deployment-metadata.sh" \
       "$temporary_root/stack-secret.yaml" "$temporary_root/release.json" >/dev/null 2>&1; then
    echo "deployment metadata installer accepted a credential-bearing database URL" >&2
    exit 1
fi

echo "deployment metadata tests passed"
