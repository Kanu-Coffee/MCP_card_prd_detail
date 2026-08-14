#!/bin/sh
set -eu

umask 027

stack_source=${1:-}
release_manifest_source=${2:-}
deployment_root=${CARDRAG_DEPLOYMENT_ROOT:-/srv/cardrag/config/deployment}

if [ "$(id -u)" -ne 0 ]; then
    echo "deployment metadata installation must run as root" >&2
    exit 77
fi
if [ -z "$stack_source" ] || [ -z "$release_manifest_source" ] || [ "$#" -ne 2 ]; then
    echo "usage: $0 REDACTED_STACK RELEASE_MANIFEST" >&2
    exit 64
fi
for source in "$stack_source" "$release_manifest_source"; do
    if [ ! -f "$source" ] || [ -L "$source" ]; then
        echo "deployment metadata source must be a regular non-symlink file: $source" >&2
        exit 66
    fi
done
case "$deployment_root" in
    /*) ;;
    *)
        echo "CARDRAG_DEPLOYMENT_ROOT must be absolute" >&2
        exit 78
        ;;
esac
if [ -L "$deployment_root" ]; then
    echo "deployment metadata root must not be a symlink" >&2
    exit 78
fi

for variable in CARDRAG_ADMIN_IMAGE CARDRAG_WORKER_IMAGE CARDRAG_MCP_IMAGE; do
    case "$variable" in
        CARDRAG_ADMIN_IMAGE) value=${CARDRAG_ADMIN_IMAGE:-} ;;
        CARDRAG_WORKER_IMAGE) value=${CARDRAG_WORKER_IMAGE:-} ;;
        CARDRAG_MCP_IMAGE) value=${CARDRAG_MCP_IMAGE:-} ;;
    esac
    case "$value" in
        *@sha256:????????????????????????????????????????????????????????????????) ;;
        *)
            echo "$variable must be a digest-qualified published image" >&2
            exit 64
            ;;
    esac
    digest=${value##*@sha256:}
    case "$digest" in
        *[!0-9a-f]*)
            echo "$variable has an invalid SHA-256 digest" >&2
            exit 64
            ;;
    esac
done

resolved_root=$(mktemp -d)
resolved_stack=$resolved_root/stack-redacted.yaml
trap 'rm -rf "$resolved_root"' EXIT HUP INT TERM

python3 - "$release_manifest_source" "$stack_source" "$resolved_stack" <<'PY'
import json
import os
import re
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    value = json.load(stream)
if not isinstance(value, dict) or value.get("schema") != "cardrag.container-release.v3":
    raise SystemExit("release manifest must use cardrag.container-release.v3")
version = value.get("version")
revision = value.get("git_sha")
if not isinstance(version, str) or not version or not re.fullmatch(r"[0-9a-f]{40}", str(revision)):
    raise SystemExit("release manifest version or Git revision is invalid")
roles = value.get("roles")
if not isinstance(roles, dict) or set(roles) != {"admin", "worker", "mcp"}:
    raise SystemExit("release manifest must bind exactly the three runtime roles")
configured_images = {
    "admin": os.environ["CARDRAG_ADMIN_IMAGE"],
    "worker": os.environ["CARDRAG_WORKER_IMAGE"],
    "mcp": os.environ["CARDRAG_MCP_IMAGE"],
}
for role, configured in configured_images.items():
    part = roles.get(role)
    if (
        not isinstance(part, dict)
        or part.get("schema") != "cardrag.container-release-part.v3"
        or part.get("role") != role
        or part.get("version") != version
        or part.get("git_sha") != revision
    ):
        raise SystemExit(f"release manifest role is invalid: {role}")
    expected = f'{part.get("image")}@{part.get("digest")}'
    if configured != expected:
        raise SystemExit(f"configured image differs from signed release evidence: {role}")

sensitive = re.compile(
    r"(password|secret|token|api[_-]?key|database[_-]?url|authorization|private[_-]?key|dsn)",
    re.IGNORECASE,
)
credential_uri = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/@\s]+:[^/@\s]+@")
env_assignment = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
safe_reference = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*(?::\?[^}]*)?\}$")

def safe_value(candidate: str) -> bool:
    return (
        not candidate
        or safe_reference.fullmatch(candidate) is not None
        or candidate.startswith("/run/secrets/")
        or candidate.casefold() in {"redacted", "<redacted>"}
        or set(candidate) <= {"*"}
    )

def environment_assignment(raw: str):
    scalar = raw.strip()
    if scalar.startswith("-"):
        scalar = scalar[1:].lstrip()
    if scalar[:1] in {"'", '"'}:
        quote = scalar[0]
        closing = scalar.rfind(quote)
        if closing <= 0:
            return None
        trailing = scalar[closing + 1:].strip()
        if trailing and not trailing.startswith("#"):
            return None
        scalar = scalar[1:closing]
    return env_assignment.fullmatch(scalar)

def contains_secret(value: object) -> bool:
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key)
            if sensitive.search(key) and not key.casefold().endswith("_file"):
                if not safe_value(str(child).strip().strip("'\"")):
                    return True
            if contains_secret(child):
                return True
        return False
    if isinstance(value, list):
        return any(contains_secret(item) for item in value)
    if isinstance(value, str):
        scalar = value.strip().strip("'\"")
        assignment = environment_assignment(value)
        if assignment and sensitive.search(assignment.group(1)):
            return not safe_value(assignment.group(2).strip())
        return credential_uri.match(scalar) is not None
    return False

if contains_secret(value):
    raise SystemExit("release manifest appears to contain an unredacted secret")
with open(sys.argv[2], encoding="utf-8") as stream:
    stack = stream.read()

# The checked-in Portainer Stack is deliberately non-interpolated so it can be
# pasted into Portainer with environment variables.  Deployment evidence must
# instead record the exact immutable images from the signed release.  Resolve
# only the three image placeholders; every other variable remains redacted.
image_variables = {
    "admin": "CARDRAG_ADMIN_IMAGE",
    "worker": "CARDRAG_WORKER_IMAGE",
    "mcp": "CARDRAG_MCP_IMAGE",
}
for role, variable in image_variables.items():
    placeholder = re.compile(r"\$\{" + re.escape(variable) + r"(?::\?[^}]*)?\}")
    stack, replacements = placeholder.subn(configured_images[role], stack)
    if replacements == 0 and configured_images[role] not in stack:
        raise SystemExit(f"Stack has no resolvable signed release image: {role}")
    if variable in stack:
        raise SystemExit(f"Stack retains an unresolved image variable: {role}")

for line in stack.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        assignment = environment_assignment(stripped)
        if assignment and sensitive.search(assignment.group(1)):
            if not safe_value(assignment.group(2).strip().strip("'\"")):
                raise SystemExit(
                    f"Stack metadata appears to contain an unredacted secret: {assignment.group(1)}"
                )
        if ":" not in stripped:
            continue
        key, raw_value = stripped.split(":", 1)
        candidate = raw_value.strip().strip("'\"")
        if not sensitive.search(key) and not credential_uri.match(candidate):
            continue
        safe = (
            key.casefold().endswith("_file")
            or safe_value(candidate)
        )
        if not safe:
            raise SystemExit(f"Stack metadata appears to contain an unredacted secret: {key}")
for role, configured in configured_images.items():
    if configured not in stack:
        raise SystemExit(f"Stack does not contain the signed release image: {role}")
with open(sys.argv[3], "x", encoding="utf-8") as stream:
    stream.write(stack)
PY

install -d -o root -g root -m 0755 "$deployment_root"
staging=$deployment_root/.install.$$
trap 'rm -rf "$resolved_root" "$staging"' EXIT HUP INT TERM
install -d -o root -g root -m 0700 "$staging"
install -o root -g root -m 0444 "$resolved_stack" "$staging/stack-redacted.yaml"
install -o root -g root -m 0444 "$release_manifest_source" "$staging/release-manifest.json"
python3 - "$staging/image-digests.json" <<'PY'
import json
import os
import sys

with open(sys.argv[1], "w", encoding="utf-8") as stream:
    json.dump({
        "schema_version": "cardrag-image-digests.v1",
        "images": {
            "admin": os.environ["CARDRAG_ADMIN_IMAGE"],
            "worker": os.environ["CARDRAG_WORKER_IMAGE"],
            "mcp": os.environ["CARDRAG_MCP_IMAGE"],
        },
    }, stream, sort_keys=True, indent=2)
    stream.write("\n")
PY
chmod 0444 "$staging/image-digests.json"
python3 - "$staging" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
names = ("stack-redacted.yaml", "image-digests.json", "release-manifest.json")
payload = {
    "schema_version": "cardrag-deployment-set.v1",
    "files": {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in names
    },
}
(root / "deployment-set.json").write_text(
    json.dumps(payload, sort_keys=True, indent=2) + "\n",
    encoding="utf-8",
)
PY
chmod 0444 "$staging/deployment-set.json"
python3 - "$staging" <<'PY'
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
for path in root.iterdir():
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise SystemExit("deployment staging contains a non-regular payload")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
for name in stack-redacted.yaml image-digests.json release-manifest.json; do
    mv -f "$staging/$name" "$deployment_root/$name"
done
python3 - "$deployment_root" <<'PY'
import os
import sys
descriptor = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
# This checksum-bound file is the commit record. A crash before this final
# rename leaves a mismatched set which every export refuses.
mv -f "$staging/deployment-set.json" "$deployment_root/deployment-set.json"
rmdir "$staging"
rm -rf "$resolved_root"
python3 - "$deployment_root" <<'PY'
import os
import sys
descriptor = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
trap - EXIT HUP INT TERM
echo "installed verified deployment metadata: $deployment_root"
