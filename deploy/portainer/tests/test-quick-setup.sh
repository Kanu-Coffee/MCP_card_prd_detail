#!/bin/sh
set -eu

repository_root=$(CDPATH='' cd -- "$(dirname -- "$0")/../../.." && pwd)
temporary_root=$(mktemp -d)
trap 'rm -rf "$temporary_root"' EXIT HUP INT TERM
fake_bin=$temporary_root/bin
docker_state=$temporary_root/docker-state
mkdir -p "$fake_bin" "$docker_state"

printf '%s\n' '#!/bin/sh' 'printf "%s\n" 0' >"$fake_bin/id"
# The single quotes intentionally defer expansion to each generated fake.
# shellcheck disable=SC2016
printf '%s\n' '#!/bin/sh' \
    'arguments=""' \
    'skip=false' \
    'for argument do' \
    '  if [ "$skip" = true ]; then skip=false; continue; fi' \
    '  case "$argument" in -o|-g) skip=true ;; *) arguments="$arguments $argument" ;; esac' \
    'done' \
    'exec /usr/bin/install $arguments' >"$fake_bin/install"
# shellcheck disable=SC2016
printf '%s\n' '#!/bin/sh' \
    'case "$1:$2" in' \
    '  volume:inspect)' \
    '    if [ "$3" = --format ]; then' \
    '      test -f "$FAKE_DOCKER_STATE/$5"' \
    '      cat "$FAKE_DOCKER_STATE/$5.labels"' \
    '    else' \
    '      test -f "$FAKE_DOCKER_STATE/$3"' \
    '    fi' \
    '    ;;' \
    '  volume:create)' \
    '    schema= fingerprint= role= name=' \
    '    shift 2' \
    '    while [ "$#" -gt 0 ]; do' \
    '      case "$1" in' \
    '        --label)' \
    '          case "$2" in' \
    '            com.cardrag.quick-setup.schema=*) schema=${2#*=} ;;' \
    '            com.cardrag.quick-setup.fingerprint=*) fingerprint=${2#*=} ;;' \
    '            com.cardrag.quick-setup.role=*) role=${2#*=} ;;' \
    '          esac' \
    '          shift 2' \
    '          ;;' \
    '        --name) name=$2; shift 2 ;;' \
    '        *) exit 2 ;;' \
    '      esac' \
    '    done' \
    '    test -n "$name"' \
    '    : >"$FAKE_DOCKER_STATE/$name"' \
    '    printf "%s|%s|%s\n" "$schema" "$fingerprint" "$role" >"$FAKE_DOCKER_STATE/$name.labels"' \
    '    printf "%s\n" "$name"' \
    '    ;;' \
    '  *) exit 2 ;;' \
    'esac' >"$fake_bin/docker"
# shellcheck disable=SC2016
printf '%s\n' '#!/bin/sh' \
    'if [ "$1:$2" = group:docker ]; then' \
    '  printf "%s\n" "docker:x:0:"' \
    'else' \
    '  exit 2' \
    'fi' >"$fake_bin/getent"
# shellcheck disable=SC2016
printf '%s\n' '#!/bin/sh' \
    'if [ "$1:$2" = "-c:%u:%g" ]; then' \
    '  if [ -n "${FAKE_STAT_TAMPER_PATH:-}" ] && [ "$3" = "$FAKE_STAT_TAMPER_PATH" ]; then' \
    '    printf "%s\n" 999:999' \
    '  else' \
    '    case "$3" in */runtime|*/runtime/*|*/imports) printf "%s\n" 10001:10001 ;; *) printf "%s\n" 0:0 ;; esac' \
    '  fi' \
    'else' \
    '  exec /usr/bin/stat "$@"' \
    'fi' >"$fake_bin/stat"
chmod 0755 "$fake_bin/id" "$fake_bin/install" "$fake_bin/docker" \
    "$fake_bin/getent" "$fake_bin/stat"

create_release_manifest() {
    destination=$1
    revision=$(git -C "$repository_root" rev-parse HEAD)
    version=$(python3 - "$repository_root/src/cardrag/__init__.py" <<'PY'
import re
import sys
value = open(sys.argv[1], encoding="utf-8").read()
match = re.search(r'__version__\s*=\s*"([^"]+)"', value)
if match is None:
    raise SystemExit("version fixture unavailable")
print(match.group(1))
PY
)
    python3 - "$destination" "$revision" "$version" <<'PY'
import json
import sys

roles = {}
for role, digest in (("admin", "a" * 64), ("worker", "b" * 64), ("mcp", "c" * 64)):
    roles[role] = {
        "schema": "cardrag.container-release-part.v3",
        "role": role,
        "image": "ymtop59/mcp-card-prd-detail",
        "digest": f"sha256:{digest}",
        "version": sys.argv[3],
        "git_sha": sys.argv[2],
    }
with open(sys.argv[1], "w", encoding="utf-8") as stream:
    json.dump(
        {
            "schema": "cardrag.container-release.v3",
            "version": sys.argv[3],
            "git_sha": sys.argv[2],
            "roles": roles,
        },
        stream,
    )
PY
}

release_manifest=$temporary_root/release-manifest.json
openrouter_source=$temporary_root/openrouter-key
setup_config=$temporary_root/setup.conf
create_release_manifest "$release_manifest"
printf '%s\n' 'sk-or-v1-test-secret-must-not-leak' >"$openrouter_source"
chmod 0600 "$openrouter_source"

state_root=$temporary_root/cardrag
stack_env=$temporary_root/etc/cardrag/stack.env
mkdir -p "$temporary_root/lock-parent"
chmod 1777 "$temporary_root/lock-parent"
cat >"$setup_config" <<EOF
CARDRAG_STATE_ROOT=$state_root
CARDRAG_DATA_ROOT=$state_root/runtime
CARDRAG_IMPORT_ROOT=$state_root/imports
CARDRAG_ARCHIVE_ROOT=$temporary_root/archive/cardrag
CARDRAG_ARCHIVE_EXPECTED_SOURCE=test-nas:/cardrag
CARDRAG_CONFIG_ROOT=$state_root/config
CARDRAG_SECRETS_DIR=$state_root/secrets
CARDRAG_DEPLOYMENT_ROOT=$state_root/config/deployment
CARDRAG_POSTGRES_VOLUME=quick-postgres-v1
CARDRAG_CODEX_AUTH_VOLUME=quick-codex-v1
KEYCLOAK_PUBLIC_URL=https://auth.cardrag.kr
KEYCLOAK_BOOTSTRAP_ADMIN_USERNAME=cardrag-bootstrap
CARDRAG_OPENROUTER_KEY_FILE=$openrouter_source
CARDRAG_RELEASE_MANIFEST=$release_manifest
CARDRAG_STACK_ENV_FILE=$stack_env
CARDRAG_SETUP_LOCK_ROOT=$temporary_root/lock-parent/cardrag
EOF

setup_output=$temporary_root/setup-output
if ! PATH=$fake_bin:$PATH FAKE_DOCKER_STATE=$docker_state \
    "$repository_root/deploy/portainer/cardrag-portainer-setup.sh" \
    --non-interactive --config "$setup_config" >"$setup_output" 2>&1; then
    sed -n '1,160p' "$setup_output" >&2
    exit 1
fi

if grep -F 'sk-or-v1-test-secret-must-not-leak' "$setup_output" >/dev/null; then
    echo "quick setup printed the OpenRouter secret" >&2
    exit 1
fi
test "$(/usr/bin/stat -c '%a' "$temporary_root/lock-parent")" = 1777
test -d "$state_root/runtime/objects"
test -d "$state_root/runtime/generations"
test -d "$state_root/runtime/build"
test -d "$state_root/runtime/page-cache"
test -f "$docker_state/quick-postgres-v1"
test -f "$docker_state/quick-codex-v1"
grep -E '^v1\|[0-9a-f]{64}\|postgres$' "$docker_state/quick-postgres-v1.labels" >/dev/null
grep -E '^v1\|[0-9a-f]{64}\|codex-auth$' "$docker_state/quick-codex-v1.labels" >/dev/null
test "$(stat -c '%a' "$state_root/secrets")" = 700
test "$(find "$state_root/secrets" -maxdepth 1 -type f -name '*.txt' | wc -l)" -eq 10
for secret_file in "$state_root"/secrets/*.txt; do
    test "$(stat -c '%a' "$secret_file")" = 444
done
cmp -s "$openrouter_source" "$state_root/secrets/openrouter_api_key.txt"
grep -F 'KEYCLOAK_PUBLIC_URL=https://auth.cardrag.kr' "$stack_env" >/dev/null
grep -F 'CARDRAG_ADMIN_IMAGE=ymtop59/mcp-card-prd-detail@sha256:' \
    "$stack_env" >/dev/null
grep -F 'CARDRAG_WORKER_IMAGE=ymtop59/mcp-card-prd-detail@sha256:' \
    "$stack_env" >/dev/null
grep -F 'CARDRAG_MCP_IMAGE=ymtop59/mcp-card-prd-detail@sha256:' \
    "$stack_env" >/dev/null
grep -F 'CARDRAG_LEGACY_IMPORT_ENABLED=false' "$stack_env" >/dev/null
/bin/sh -n "$stack_env"
# shellcheck disable=SC2016
env -i /bin/sh -c 'set -a; . "$1"; set +a' sh "$stack_env"
python3 - "$repository_root/deploy/portainer/stack.env.example" "$stack_env" <<'PY'
import sys


def assignments(path):
    pairs = []
    for line in open(path, encoding="utf-8"):
        line = line.rstrip("\n")
        if line and not line.startswith("#"):
            key, separator, value = line.partition("=")
            if separator:
                pairs.append((key, value))
    if len({key for key, _ in pairs}) != len(pairs):
        raise SystemExit("Portainer environment contains duplicate keys")
    return dict(pairs)


example = assignments(sys.argv[1])
generated = assignments(sys.argv[2])
if set(generated) != set(example) | {"CARDRAG_STATE_ROOT"}:
    raise SystemExit("generated Portainer environment key set differs from the release example")
installation_values = {
    "CARDRAG_DATA_ROOT",
    "CARDRAG_IMPORT_ROOT",
    "CARDRAG_ARCHIVE_ROOT",
    "CARDRAG_ARCHIVE_EXPECTED_SOURCE",
    "CARDRAG_CONFIG_ROOT",
    "CARDRAG_SECRETS_DIR",
    "CARDRAG_DEPLOYMENT_ROOT",
    "KEYCLOAK_PUBLIC_URL",
    "CARDRAG_OIDC_ISSUER",
    "KEYCLOAK_BOOTSTRAP_ADMIN_USERNAME",
    "CARDRAG_POSTGRES_VOLUME",
    "CARDRAG_CODEX_AUTH_VOLUME",
    "CARDRAG_ADMIN_IMAGE",
    "CARDRAG_WORKER_IMAGE",
    "CARDRAG_MCP_IMAGE",
}
for key in set(example) - installation_values:
    if generated[key] != example[key]:
        raise SystemExit(f"generated operational default differs from stack.env.example: {key}")
PY
test -f "$state_root/config/deployment/deployment-set.json"
test -f "$state_root/config/deployment/stack-redacted.yaml"
test -f "$state_root/secrets/.cardrag-quick-setup/complete"

# Matching resume verifies and reuses the exact existing secrets.
find "$state_root/secrets" -maxdepth 1 -type f -name '*.txt' -exec sha256sum {} + |
    sort >"$temporary_root/secrets-before"
PATH=$fake_bin:$PATH FAKE_DOCKER_STATE=$docker_state \
    "$repository_root/deploy/portainer/cardrag-portainer-setup.sh" \
    --non-interactive --config "$setup_config" >"$temporary_root/resume-output" 2>&1
find "$state_root/secrets" -maxdepth 1 -type f -name '*.txt' -exec sha256sum {} + |
    sort >"$temporary_root/secrets-after"
cmp -s "$temporary_root/secrets-before" "$temporary_root/secrets-after"
if grep -F 'sk-or-v1-test-secret-must-not-leak' "$temporary_root/resume-output" >/dev/null; then
    echo "quick setup resume printed the OpenRouter secret" >&2
    exit 1
fi

# Crash-recovery: a partial durable-state commit is rewritten and atomically
# promoted only for the installer-owned pending filename.
mv "$state_root/secrets/.cardrag-quick-setup/state" \
    "$state_root/secrets/.cardrag-quick-setup/.state.pending"
: >"$state_root/secrets/.cardrag-quick-setup/.state.pending"
chmod 0600 "$state_root/secrets/.cardrag-quick-setup/.state.pending"
pending_before=$(sha256sum \
    "$state_root/secrets/.cardrag-quick-setup/.state.pending" | awk '{print $1}')
chmod 0777 "$state_root/secrets"
if PATH=$fake_bin:$PATH FAKE_DOCKER_STATE=$docker_state \
    "$repository_root/deploy/portainer/cardrag-portainer-setup.sh" \
    --non-interactive --config "$setup_config" >/dev/null 2>&1; then
    echo "quick setup recovered state inside an unsafe secret root" >&2
    exit 1
fi
test "$(sha256sum "$state_root/secrets/.cardrag-quick-setup/.state.pending" | awk '{print $1}')" = \
    "$pending_before"
test ! -e "$state_root/secrets/.cardrag-quick-setup/state"
chmod 0700 "$state_root/secrets"
PATH=$fake_bin:$PATH FAKE_DOCKER_STATE=$docker_state \
    "$repository_root/deploy/portainer/cardrag-portainer-setup.sh" \
    --non-interactive --config "$setup_config" >/dev/null
test -f "$state_root/secrets/.cardrag-quick-setup/state"
test ! -e "$state_root/secrets/.cardrag-quick-setup/.state.pending"

# A password pending file left before secrets-ready is adopted without changing
# its bytes; downstream phase markers are reconstructed from exact artifacts.
worker_secret=$state_root/secrets/cardrag_worker_db_password.txt
worker_digest=$(sha256sum "$worker_secret" | awk '{print $1}')
for phase in secrets-ready environment-ready metadata-ready complete; do
    rm "$state_root/secrets/.cardrag-quick-setup/$phase"
done
mv "$worker_secret" "$state_root/secrets/.cardrag_worker_db_password.txt.cardrag-pending"
chmod 0400 "$state_root/secrets/.cardrag_worker_db_password.txt.cardrag-pending"
PATH=$fake_bin:$PATH FAKE_DOCKER_STATE=$docker_state \
    "$repository_root/deploy/portainer/cardrag-portainer-setup.sh" \
    --non-interactive --config "$setup_config" >/dev/null
test "$(sha256sum "$worker_secret" | awk '{print $1}')" = "$worker_digest"
test ! -e "$state_root/secrets/.cardrag_worker_db_password.txt.cardrag-pending"

# stack.env and deployment metadata use the same pending-file recovery contract.
for phase in environment-ready metadata-ready complete; do
    rm "$state_root/secrets/.cardrag-quick-setup/$phase"
done
mv "$stack_env" "$stack_env.cardrag-pending"
chmod 0600 "$stack_env.cardrag-pending"
PATH=$fake_bin:$PATH FAKE_DOCKER_STATE=$docker_state \
    "$repository_root/deploy/portainer/cardrag-portainer-setup.sh" \
    --non-interactive --config "$setup_config" >/dev/null
test -f "$stack_env"
test ! -e "$stack_env.cardrag-pending"
rm "$state_root/secrets/.cardrag-quick-setup/metadata-ready"
rm "$state_root/secrets/.cardrag-quick-setup/complete"
metadata_target=$state_root/config/deployment/image-digests.json
mv "$metadata_target" \
    "$state_root/config/deployment/.image-digests.json.cardrag-pending"
chmod 0400 "$state_root/config/deployment/.image-digests.json.cardrag-pending"
PATH=$fake_bin:$PATH FAKE_DOCKER_STATE=$docker_state \
    "$repository_root/deploy/portainer/cardrag-portainer-setup.sh" \
    --non-interactive --config "$setup_config" >/dev/null
test -f "$metadata_target"
test ! -e "$state_root/config/deployment/.image-digests.json.cardrag-pending"

# --check is read-only and reports the exact prepared state.
find "$state_root" -type f -exec sha256sum {} + | sort >"$temporary_root/check-before"
PATH=$fake_bin:$PATH FAKE_DOCKER_STATE=$docker_state \
    "$repository_root/deploy/portainer/cardrag-portainer-setup.sh" \
    --non-interactive --check --config "$setup_config" >"$temporary_root/check-output" 2>&1
find "$state_root" -type f -exec sha256sum {} + | sort >"$temporary_root/check-after"
cmp -s "$temporary_root/check-before" "$temporary_root/check-after"
grep -F 'quick setup check passed' "$temporary_root/check-output" >/dev/null

# Ephemeral locks may disappear on reboot; read-only doctor mode must still
# verify the durable installer-owned state without recreating the lock.
mv "$temporary_root/lock-parent/cardrag" "$temporary_root/reboot-lock-saved"
PATH=$fake_bin:$PATH FAKE_DOCKER_STATE=$docker_state \
    "$repository_root/deploy/portainer/cardrag-portainer-setup.sh" \
    --non-interactive --check --config "$setup_config" >/dev/null
test ! -e "$temporary_root/lock-parent/cardrag"
mv "$temporary_root/reboot-lock-saved" "$temporary_root/lock-parent/cardrag"

# Permission, ownership, and installed-config tampering are rejected.
chmod 0777 "$state_root/runtime/objects"
if PATH=$fake_bin:$PATH FAKE_DOCKER_STATE=$docker_state \
    "$repository_root/deploy/portainer/cardrag-portainer-setup.sh" \
    --non-interactive --check --config "$setup_config" >/dev/null 2>&1; then
    echo "quick setup check accepted a world-writable runtime root" >&2
    exit 1
fi

chmod 0750 "$state_root/runtime/objects"
if PATH=$fake_bin:$PATH FAKE_DOCKER_STATE=$docker_state \
    FAKE_STAT_TAMPER_PATH=$state_root/secrets/cardrag_db_password.txt \
    "$repository_root/deploy/portainer/cardrag-portainer-setup.sh" \
    --non-interactive --check --config "$setup_config" >/dev/null 2>&1; then
    echo "quick setup check accepted a foreign-owned secret" >&2
    exit 1
fi
chmod 0755 "$state_root/config/portainer/storage-preflight.sh"
printf '%s\n' '# tampered' >>"$state_root/config/portainer/storage-preflight.sh"
if PATH=$fake_bin:$PATH FAKE_DOCKER_STATE=$docker_state \
    "$repository_root/deploy/portainer/cardrag-portainer-setup.sh" \
    --non-interactive --check --config "$setup_config" >/dev/null 2>&1; then
    echo "quick setup check accepted a modified installed helper" >&2
    exit 1
fi
cp "$repository_root/deploy/portainer/scripts/storage-preflight.sh" \
    "$state_root/config/portainer/storage-preflight.sh"
chmod 0555 "$state_root/config/portainer/storage-preflight.sh"

# A lock-file symlink is rejected without truncating its target.
setup_lock_file=$(find "$temporary_root/lock-parent/cardrag" -maxdepth 1 -type f -print -quit)
mv "$setup_lock_file" "$temporary_root/setup-lock.saved"
printf '%s\n' 'must-remain' >"$temporary_root/lock-victim"
ln -s "$temporary_root/lock-victim" "$setup_lock_file"
if PATH=$fake_bin:$PATH FAKE_DOCKER_STATE=$docker_state \
    "$repository_root/deploy/portainer/cardrag-portainer-setup.sh" \
    --non-interactive --config "$setup_config" >/dev/null 2>&1; then
    echo "quick setup resume accepted a symlink lock file" >&2
    exit 1
fi
test "$(cat "$temporary_root/lock-victim")" = must-remain
rm "$setup_lock_file"
mv "$temporary_root/setup-lock.saved" "$setup_lock_file"

# A completed setup never regenerates a missing database credential.
mv "$state_root/secrets/cardrag_worker_db_password.txt" \
    "$temporary_root/cardrag_worker_db_password.saved"
if PATH=$fake_bin:$PATH FAKE_DOCKER_STATE=$docker_state \
    "$repository_root/deploy/portainer/cardrag-portainer-setup.sh" \
    --non-interactive --config "$setup_config" >/dev/null 2>&1; then
    echo "quick setup regenerated a missing secret after secrets-ready" >&2
    exit 1
fi
test ! -e "$state_root/secrets/cardrag_worker_db_password.txt"
mv "$temporary_root/cardrag_worker_db_password.saved" \
    "$state_root/secrets/cardrag_worker_db_password.txt"

# Dry-run performs full preflight without creating state or volumes.
dry_root=$temporary_root/dry-cardrag
dry_config=$temporary_root/dry.conf
sed \
    -e "s|$state_root|$dry_root|g" \
    -e 's|quick-postgres-v1|dry-postgres-v1|' \
    -e 's|quick-codex-v1|dry-codex-v1|' \
    -e "s|$stack_env|$temporary_root/etc/dry/stack.env|" \
    "$setup_config" >"$dry_config"
PATH=$fake_bin:$PATH FAKE_DOCKER_STATE=$docker_state \
    "$repository_root/deploy/portainer/cardrag-portainer-setup.sh" \
    --non-interactive --dry-run --config "$dry_config" >"$temporary_root/dry-output" 2>&1
test ! -e "$dry_root"
test ! -e "$docker_state/dry-postgres-v1"
grep -F 'no host data was changed' "$temporary_root/dry-output" >/dev/null

# A config may provide only the state base; all non-explicit child roots are
# derived from that custom base before validation.
derived_config=$temporary_root/derived.conf
sed '/^CARDRAG_DATA_ROOT=/d; /^CARDRAG_IMPORT_ROOT=/d; /^CARDRAG_CONFIG_ROOT=/d; /^CARDRAG_SECRETS_DIR=/d; /^CARDRAG_DEPLOYMENT_ROOT=/d' \
    "$dry_config" >"$derived_config"
PATH=$fake_bin:$PATH FAKE_DOCKER_STATE=$docker_state \
    "$repository_root/deploy/portainer/cardrag-portainer-setup.sh" \
    --non-interactive --dry-run --config "$derived_config" >/dev/null

# A crash-created deterministic staging directory is completed and atomically
# claimed instead of leaving an unrecoverable empty ownership guard.
staged_root=$temporary_root/staged-cardrag
staged_env=$temporary_root/etc/staged/stack.env
staged_config=$temporary_root/staged.conf
sed \
    -e "s|$dry_root|$staged_root|g" \
    -e 's|dry-postgres-v1|staged-postgres-v1|' \
    -e 's|dry-codex-v1|staged-codex-v1|' \
    -e "s|$temporary_root/etc/dry/stack.env|$staged_env|" \
    "$dry_config" >"$staged_config"
mkdir "$staged_root.cardrag-quick-setup.pending"
chmod 0755 "$staged_root.cardrag-quick-setup.pending"
PATH=$fake_bin:$PATH FAKE_DOCKER_STATE=$docker_state \
    "$repository_root/deploy/portainer/cardrag-portainer-setup.sh" \
    --non-interactive --config "$staged_config" >/dev/null
test ! -e "$staged_root.cardrag-quick-setup.pending"
test -f "$staged_root/secrets/.cardrag-quick-setup/state"
test -f "$staged_root/secrets/.cardrag-quick-setup/complete"

# A markerless target is never adopted, even when it is empty.
empty_root=$temporary_root/empty-cardrag
empty_config=$temporary_root/empty.conf
sed \
    -e "s|$dry_root|$empty_root|g" \
    -e 's|dry-postgres-v1|empty-postgres-v1|' \
    -e 's|dry-codex-v1|empty-codex-v1|' \
    -e "s|$temporary_root/etc/dry/stack.env|$temporary_root/etc/empty/stack.env|" \
    "$dry_config" >"$empty_config"
mkdir "$empty_root"
if PATH=$fake_bin:$PATH FAKE_DOCKER_STATE=$docker_state \
    "$repository_root/deploy/portainer/cardrag-portainer-setup.sh" \
    --non-interactive --dry-run --config "$empty_config" >/dev/null 2>&1; then
    echo "quick setup adopted an existing markerless state root" >&2
    exit 1
fi
test -z "$(find "$empty_root" -mindepth 1 -maxdepth 1 -print -quit)"

# stack.env may be neither inside nor an ancestor of any protected root.
reverse_base=$temporary_root/reverse-env
reverse_config=$temporary_root/reverse.conf
sed \
    -e "s|$dry_root|$reverse_base/cardrag|g" \
    -e 's|dry-postgres-v1|reverse-postgres-v1|' \
    -e 's|dry-codex-v1|reverse-codex-v1|' \
    -e "s|$temporary_root/etc/dry/stack.env|$reverse_base|" \
    "$dry_config" >"$reverse_config"
if PATH=$fake_bin:$PATH FAKE_DOCKER_STATE=$docker_state \
    "$repository_root/deploy/portainer/cardrag-portainer-setup.sh" \
    --non-interactive --dry-run --config "$reverse_config" >/dev/null 2>&1; then
    echo "quick setup accepted stack.env as an ancestor of the state root" >&2
    exit 1
fi
test ! -e "$reverse_base"

shell_injection_config=$temporary_root/shell-injection.conf
sed 's|https://auth.cardrag.kr|https://auth.cardrag.kr;id|' \
    "$dry_config" >"$shell_injection_config"
if PATH=$fake_bin:$PATH FAKE_DOCKER_STATE=$docker_state \
    "$repository_root/deploy/portainer/cardrag-portainer-setup.sh" \
    --non-interactive --dry-run --config "$shell_injection_config" >/dev/null 2>&1; then
    echo "quick setup accepted shell grammar in a Portainer environment value" >&2
    exit 1
fi

# Interactive defaults are re-derived when the operator changes the state
# root, and Enter accepts those newly displayed paths.
interactive_root=$temporary_root/interactive-cardrag
interactive_stack_env=$temporary_root/etc/interactive/stack.env
PATH=$fake_bin:$PATH FAKE_DOCKER_STATE=$docker_state \
    "$repository_root/deploy/portainer/cardrag-portainer-setup.sh" \
    --interactive --dry-run >"$temporary_root/interactive-output" \
    2>"$temporary_root/interactive-prompts" <<EOF
$interactive_root


$temporary_root/interactive-archive/cardrag




interactive-postgres-v1
interactive-codex-v1
https://interactive.cardrag.kr

$openrouter_source
$release_manifest
$interactive_stack_env
EOF
grep -F "Runtime data root [$interactive_root/runtime]" \
    "$temporary_root/interactive-prompts" >/dev/null
grep -F "Configuration root [$interactive_root/config]" \
    "$temporary_root/interactive-prompts" >/dev/null
test ! -e "$interactive_root"

# A pre-existing custom stack.env parent with unsafe metadata is rejected
# without chmod/chown mutation.
parent_root=$temporary_root/parent-cardrag
parent_env=$temporary_root/etc/unsafe-parent/stack.env
parent_config=$temporary_root/parent.conf
sed \
    -e "s|$state_root|$parent_root|g" \
    -e 's|quick-postgres-v1|parent-postgres-v1|' \
    -e 's|quick-codex-v1|parent-codex-v1|' \
    -e "s|$stack_env|$parent_env|" \
    "$setup_config" >"$parent_config"
mkdir -p "$(dirname -- "$parent_env")"
chmod 0777 "$(dirname -- "$parent_env")"
if PATH=$fake_bin:$PATH FAKE_DOCKER_STATE=$docker_state \
    "$repository_root/deploy/portainer/cardrag-portainer-setup.sh" \
    --non-interactive --dry-run --config "$parent_config" >/dev/null 2>&1; then
    echo "quick setup accepted an unsafe existing stack.env parent" >&2
    exit 1
fi
test "$(stat -c '%a' "$(dirname -- "$parent_env")")" = 777
test ! -e "$parent_root"

# Invalid/unreadable host roots and Compose interpolation characters fail in
# preflight without creating a marker or volume.
regular_root=$temporary_root/regular-cardrag
regular_config=$temporary_root/regular.conf
sed \
    -e "s|$state_root|$regular_root|g" \
    -e 's|quick-postgres-v1|regular-postgres-v1|' \
    -e 's|quick-codex-v1|regular-codex-v1|' \
    -e "s|$stack_env|$temporary_root/etc/regular/stack.env|" \
    "$setup_config" >"$regular_config"
printf '%s\n' 'not-a-directory' >"$regular_root"
if PATH=$fake_bin:$PATH FAKE_DOCKER_STATE=$docker_state \
    "$repository_root/deploy/portainer/cardrag-portainer-setup.sh" \
    --non-interactive --dry-run --config "$regular_config" >/dev/null 2>&1; then
    echo "quick setup dry-run accepted a regular-file state root" >&2
    exit 1
fi
test ! -e "$docker_state/regular-postgres-v1"

unreadable_root=$temporary_root/unreadable-cardrag
unreadable_config=$temporary_root/unreadable.conf
sed \
    -e "s|$state_root|$unreadable_root|g" \
    -e 's|quick-postgres-v1|unreadable-postgres-v1|' \
    -e 's|quick-codex-v1|unreadable-codex-v1|' \
    -e "s|$stack_env|$temporary_root/etc/unreadable/stack.env|" \
    "$setup_config" >"$unreadable_config"
mkdir "$unreadable_root"
chmod 0000 "$unreadable_root"
if PATH=$fake_bin:$PATH FAKE_DOCKER_STATE=$docker_state \
    "$repository_root/deploy/portainer/cardrag-portainer-setup.sh" \
    --non-interactive --dry-run --config "$unreadable_config" >/dev/null 2>&1; then
    echo "quick setup dry-run accepted an unreadable state root" >&2
    exit 1
fi
chmod 0700 "$unreadable_root"
test ! -e "$docker_state/unreadable-postgres-v1"

selector_config=$temporary_root/selector.conf
# Literal interpolation syntax must reach the installer unchanged.
# shellcheck disable=SC2016
sed 's|^CARDRAG_DATA_ROOT=.*|CARDRAG_DATA_ROOT=/srv/${HOST_SECRET}/runtime|' \
    "$setup_config" >"$selector_config"
if PATH=$fake_bin:$PATH FAKE_DOCKER_STATE=$docker_state \
    "$repository_root/deploy/portainer/cardrag-portainer-setup.sh" \
    --non-interactive --dry-run --config "$selector_config" >/dev/null 2>&1; then
    echo "quick setup accepted a Compose-interpolated selector path" >&2
    exit 1
fi

invalid_volume_config=$temporary_root/invalid-volume.conf
sed 's|dry-postgres-v1|bad:postgres|' "$dry_config" >"$invalid_volume_config"
if PATH=$fake_bin:$PATH FAKE_DOCKER_STATE=$docker_state \
    "$repository_root/deploy/portainer/cardrag-portainer-setup.sh" \
    --non-interactive --dry-run --config "$invalid_volume_config" >/dev/null 2>&1; then
    echo "quick setup accepted an unsafe Docker volume name" >&2
    exit 1
fi

# --check on a pristine target is read-only and refuses to claim ownership.
if PATH=$fake_bin:$PATH FAKE_DOCKER_STATE=$docker_state \
    "$repository_root/deploy/portainer/cardrag-portainer-setup.sh" \
    --non-interactive --check --config "$dry_config" >/dev/null 2>&1; then
    echo "quick setup check accepted a pristine target" >&2
    exit 1
fi
test ! -e "$dry_root"
test ! -e "$docker_state/dry-postgres-v1"

# Unowned secrets fail closed before any external volume is created.
rejected_root=$temporary_root/rejected-cardrag
rejected_config=$temporary_root/rejected.conf
sed \
    -e "s|$state_root|$rejected_root|g" \
    -e 's|quick-postgres-v1|rejected-postgres-v1|' \
    -e 's|quick-codex-v1|rejected-codex-v1|' \
    -e "s|$stack_env|$temporary_root/etc/rejected/stack.env|" \
    "$setup_config" >"$rejected_config"
mkdir -p "$rejected_root/secrets"
printf '%s\n' 'do-not-overwrite' >"$rejected_root/secrets/existing.txt"
if PATH=$fake_bin:$PATH FAKE_DOCKER_STATE=$docker_state \
    "$repository_root/deploy/portainer/cardrag-portainer-setup.sh" \
    --non-interactive --config "$rejected_config" >/dev/null 2>&1; then
    echo "quick setup accepted a non-empty unowned secret root" >&2
    exit 1
fi
test "$(cat "$rejected_root/secrets/existing.txt")" = do-not-overwrite
test ! -e "$docker_state/rejected-postgres-v1"

# Even an empty look-alike guard without durable state cannot be taken over.
guard_root=$temporary_root/guard-cardrag
guard_config=$temporary_root/guard.conf
sed \
    -e "s|$state_root|$guard_root|g" \
    -e 's|quick-postgres-v1|guard-postgres-v1|' \
    -e 's|quick-codex-v1|guard-codex-v1|' \
    -e "s|$stack_env|$temporary_root/etc/guard/stack.env|" \
    "$setup_config" >"$guard_config"
mkdir -p "$guard_root/secrets/.cardrag-quick-setup"
chmod 0700 "$guard_root/secrets" "$guard_root/secrets/.cardrag-quick-setup"
if PATH=$fake_bin:$PATH FAKE_DOCKER_STATE=$docker_state \
    "$repository_root/deploy/portainer/cardrag-portainer-setup.sh" \
    --non-interactive --config "$guard_config" >/dev/null 2>&1; then
    echo "quick setup took over an empty state-less ownership guard" >&2
    exit 1
fi
test ! -e "$docker_state/guard-postgres-v1"

# Existing runtime payload is never adopted as a fresh install.
data_root=$temporary_root/data-cardrag
data_config=$temporary_root/data.conf
sed \
    -e "s|$state_root|$data_root|g" \
    -e 's|quick-postgres-v1|data-postgres-v1|' \
    -e 's|quick-codex-v1|data-codex-v1|' \
    -e "s|$stack_env|$temporary_root/etc/data/stack.env|" \
    "$setup_config" >"$data_config"
mkdir -p "$data_root/runtime/objects"
printf '%s\n' 'existing-object' >"$data_root/runtime/objects/object"
if PATH=$fake_bin:$PATH FAKE_DOCKER_STATE=$docker_state \
    "$repository_root/deploy/portainer/cardrag-portainer-setup.sh" \
    --non-interactive --config "$data_config" >/dev/null 2>&1; then
    echo "quick setup accepted existing runtime data" >&2
    exit 1
fi
test "$(cat "$data_root/runtime/objects/object")" = existing-object

# An existing PostgreSQL volume blocks fresh secret generation (no --force).
volume_root=$temporary_root/volume-cardrag
volume_config=$temporary_root/volume.conf
sed \
    -e "s|$state_root|$volume_root|g" \
    -e 's|quick-postgres-v1|existing-postgres-v1|' \
    -e 's|quick-codex-v1|existing-codex-v1|' \
    -e "s|$stack_env|$temporary_root/etc/volume/stack.env|" \
    "$setup_config" >"$volume_config"
: >"$docker_state/existing-postgres-v1"
if PATH=$fake_bin:$PATH FAKE_DOCKER_STATE=$docker_state \
    "$repository_root/deploy/portainer/cardrag-portainer-setup.sh" \
    --non-interactive --config "$volume_config" >/dev/null 2>&1; then
    echo "quick setup accepted an existing external PostgreSQL volume" >&2
    exit 1
fi
test ! -e "$volume_root/secrets"

# Exact ownership labels are verified on every resume/check.
cp "$docker_state/quick-codex-v1.labels" "$temporary_root/quick-codex.labels.saved"
printf '%s\n' 'v1|wrong-fingerprint|codex-auth' >"$docker_state/quick-codex-v1.labels"
if PATH=$fake_bin:$PATH FAKE_DOCKER_STATE=$docker_state \
    "$repository_root/deploy/portainer/cardrag-portainer-setup.sh" \
    --non-interactive --check --config "$setup_config" >/dev/null 2>&1; then
    echo "quick setup accepted a volume with foreign ownership labels" >&2
    exit 1
fi
mv "$temporary_root/quick-codex.labels.saved" "$docker_state/quick-codex-v1.labels"

# A different config fingerprint cannot reuse an installer-owned secret root.
changed_config=$temporary_root/changed.conf
sed 's|KEYCLOAK_PUBLIC_URL=https://auth.cardrag.kr|KEYCLOAK_PUBLIC_URL=https://other.cardrag.kr|' \
    "$setup_config" >"$changed_config"
if PATH=$fake_bin:$PATH FAKE_DOCKER_STATE=$docker_state \
    "$repository_root/deploy/portainer/cardrag-portainer-setup.sh" \
    --non-interactive --config "$changed_config" >/dev/null 2>&1; then
    echo "quick setup accepted a different resume fingerprint" >&2
    exit 1
fi

# The non-secret config allow-list rejects credential-bearing keys.
printf '%s\n' 'OPENROUTER_API_KEY=must-not-be-accepted' >"$temporary_root/secret-bearing.conf"
if PATH=$fake_bin:$PATH FAKE_DOCKER_STATE=$docker_state \
    "$repository_root/deploy/portainer/cardrag-portainer-setup.sh" \
    --non-interactive --config "$temporary_root/secret-bearing.conf" >/dev/null 2>&1; then
    echo "quick setup accepted a secret-bearing configuration key" >&2
    exit 1
fi

PATH=$fake_bin:$PATH FAKE_DOCKER_STATE=$docker_state \
    CARDRAG_STACK_ENV_FILE=$stack_env \
    "$repository_root/deploy/portainer/cardrag-portainer-setup.sh" \
    --print-next-steps >"$temporary_root/next-steps"
grep -F 'cardrag-bootstrap-stack.yaml' "$temporary_root/next-steps" >/dev/null
grep -F 'cardrag-stack.yaml' "$temporary_root/next-steps" >/dev/null
grep -F "$stack_env" "$temporary_root/next-steps" >/dev/null
grep -F 'codex login --device-auth' "$temporary_root/next-steps" >/dev/null

# The explicit, confirmed lifecycle action retires only the bootstrap secret;
# subsequent check/resume accepts that intentional absence but no other one.
bootstrap_secret_digest=$(sha256sum \
    "$state_root/secrets/keycloak_admin_password.txt" | awk '{print $1}')
if PATH=$fake_bin:$PATH FAKE_DOCKER_STATE=$docker_state \
    "$repository_root/deploy/portainer/cardrag-portainer-setup.sh" \
    --non-interactive --config "$setup_config" \
    --bootstrap-complete --confirmed-permanent-admin >/dev/null 2>&1; then
    echo "bootstrap completion accepted no bootstrap-account retirement confirmation" >&2
    exit 1
fi
test "$(sha256sum "$state_root/secrets/keycloak_admin_password.txt" | awk '{print $1}')" = \
    "$bootstrap_secret_digest"
PATH=$fake_bin:$PATH FAKE_DOCKER_STATE=$docker_state \
    "$repository_root/deploy/portainer/cardrag-portainer-setup.sh" \
    --non-interactive --config "$setup_config" \
    --bootstrap-complete --confirmed-permanent-admin \
    --confirmed-bootstrap-admin-revoked \
    >"$temporary_root/bootstrap-complete-output" 2>&1
test ! -e "$state_root/secrets/keycloak_admin_password.txt"
test -f "$state_root/secrets/.cardrag-quick-setup/bootstrap-complete"
grep -F 'bootstrap account retirement confirmation' \
    "$temporary_root/bootstrap-complete-output" >/dev/null
grep -F 'cardrag-stack.yaml' "$temporary_root/bootstrap-complete-output" >/dev/null
if grep -F 'cardrag-bootstrap-stack.yaml' \
    "$temporary_root/bootstrap-complete-output" >/dev/null; then
    echo "post-bootstrap hand-off repeated the retired bootstrap Stack" >&2
    exit 1
fi
PATH=$fake_bin:$PATH FAKE_DOCKER_STATE=$docker_state \
    "$repository_root/deploy/portainer/cardrag-portainer-setup.sh" \
    --non-interactive --check --config "$setup_config" \
    >"$temporary_root/post-bootstrap-check" 2>&1
if grep -F 'cardrag-bootstrap-stack.yaml' \
    "$temporary_root/post-bootstrap-check" >/dev/null; then
    echo "post-bootstrap check repeated the retired bootstrap Stack" >&2
    exit 1
fi
PATH=$fake_bin:$PATH FAKE_DOCKER_STATE=$docker_state \
    "$repository_root/deploy/portainer/cardrag-portainer-setup.sh" \
    --non-interactive --config "$setup_config" \
    >"$temporary_root/post-bootstrap-resume" 2>&1
if grep -F 'cardrag-bootstrap-stack.yaml' \
    "$temporary_root/post-bootstrap-resume" >/dev/null; then
    echo "post-bootstrap resume repeated the retired bootstrap Stack" >&2
    exit 1
fi
test ! -e "$state_root/secrets/keycloak_admin_password.txt"

echo "Portainer quick setup tests passed"
