#!/bin/sh
set -eu

umask 077

script_root=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
config_file=
interaction=auto
dry_run=false
check_only=false
next_steps_only=false
bootstrap_complete_mode=false
permanent_admin_confirmed=false
bootstrap_admin_revoked=false

usage() {
    cat <<'EOF'
usage: cardrag-portainer-setup.sh [OPTIONS]

Prepare a fresh Docker Standalone host for the CardRAG Portainer Stacks.
Secret values are never accepted as arguments or environment variables. The
OpenRouter key must already be in a private regular file.

Options:
  --config FILE       read non-secret KEY=value settings from FILE
  --interactive       prompt for every setting (secret file paths only)
  --non-interactive   use settings from FILE and/or the environment
  --dry-run           validate a fresh setup without changing host state
  --check             read-only verification of an installer-owned setup
  --bootstrap-complete
                      retire the one-time Keycloak bootstrap credential
  --confirmed-permanent-admin
                      required confirmation for --bootstrap-complete
  --confirmed-bootstrap-admin-revoked
                      confirm the bootstrap account was revoked, disabled, or
                      rotated and its old login was rejected
  --print-next-steps  print the Portainer hand-off steps and exit
  -h, --help          show this help

Accepted settings:
  CARDRAG_STATE_ROOT, CARDRAG_DATA_ROOT, CARDRAG_IMPORT_ROOT,
  CARDRAG_ARCHIVE_ROOT, CARDRAG_ARCHIVE_EXPECTED_SOURCE,
  CARDRAG_CONFIG_ROOT, CARDRAG_SECRETS_DIR, CARDRAG_DEPLOYMENT_ROOT,
  CARDRAG_POSTGRES_VOLUME, CARDRAG_CODEX_AUTH_VOLUME,
  KEYCLOAK_PUBLIC_URL, KEYCLOAK_BOOTSTRAP_ADMIN_USERNAME,
  CARDRAG_OPENROUTER_KEY_FILE, CARDRAG_RELEASE_MANIFEST,
  CARDRAG_STACK_ENV_FILE, CARDRAG_SETUP_LOCK_ROOT

The configuration is deliberately non-secret. Never put a key, password,
token, database URL, or other credential in it.
EOF
}

die() {
    echo "cardrag quick setup: $*" >&2
    exit 1
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --config)
            [ "$#" -ge 2 ] || die "--config requires a file"
            config_file=$2
            shift 2
            ;;
        --config=*) config_file=${1#*=}; shift ;;
        --interactive) interaction=interactive; shift ;;
        --non-interactive) interaction=non-interactive; shift ;;
        --dry-run) dry_run=true; shift ;;
        --check) check_only=true; shift ;;
        --bootstrap-complete) bootstrap_complete_mode=true; shift ;;
        --confirmed-permanent-admin) permanent_admin_confirmed=true; shift ;;
        --confirmed-bootstrap-admin-revoked) bootstrap_admin_revoked=true; shift ;;
        --print-next-steps) next_steps_only=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "unknown option: $1" ;;
    esac
done
[ "$dry_run" != true ] || [ "$check_only" != true ] ||
    die "--dry-run and --check are mutually exclusive"
if [ "$bootstrap_complete_mode" = true ]; then
    [ "$permanent_admin_confirmed" = true ] ||
        die "--bootstrap-complete requires --confirmed-permanent-admin"
    [ "$bootstrap_admin_revoked" = true ] ||
        die "--bootstrap-complete requires --confirmed-bootstrap-admin-revoked"
    [ "$dry_run" != true ] && [ "$check_only" != true ] &&
        [ "$next_steps_only" != true ] ||
        die "--bootstrap-complete cannot be combined with dry-run, check, or next-steps"
elif [ "$permanent_admin_confirmed" = true ] || [ "$bootstrap_admin_revoked" = true ]; then
    die "bootstrap completion confirmations are valid only with --bootstrap-complete"
fi

data_root_explicit=false
import_root_explicit=false
config_root_explicit=false
secret_root_explicit=false
[ "${CARDRAG_DATA_ROOT+x}" = x ] && data_root_explicit=true
[ "${CARDRAG_IMPORT_ROOT+x}" = x ] && import_root_explicit=true
[ "${CARDRAG_CONFIG_ROOT+x}" = x ] && config_root_explicit=true
[ "${CARDRAG_SECRETS_DIR+x}" = x ] && secret_root_explicit=true

state_root=${CARDRAG_STATE_ROOT:-/srv/cardrag}
data_root=${CARDRAG_DATA_ROOT:-$state_root/runtime}
import_root=${CARDRAG_IMPORT_ROOT:-$state_root/imports}
archive_root=${CARDRAG_ARCHIVE_ROOT:-/mnt/cardrag-backup/cardrag}
archive_expected_source=${CARDRAG_ARCHIVE_EXPECTED_SOURCE:-}
config_root=${CARDRAG_CONFIG_ROOT:-$state_root/config}
secret_root=${CARDRAG_SECRETS_DIR:-$state_root/secrets}
deployment_root=${CARDRAG_DEPLOYMENT_ROOT:-}
postgres_volume=${CARDRAG_POSTGRES_VOLUME:-cardrag-postgres-v1}
codex_volume=${CARDRAG_CODEX_AUTH_VOLUME:-cardrag-codex-auth-v1}
keycloak_public_url=${KEYCLOAK_PUBLIC_URL:-}
bootstrap_username=${KEYCLOAK_BOOTSTRAP_ADMIN_USERNAME:-cardrag-bootstrap}
openrouter_key_file=${CARDRAG_OPENROUTER_KEY_FILE:-}
release_manifest=${CARDRAG_RELEASE_MANIFEST:-}
stack_env_file=${CARDRAG_STACK_ENV_FILE:-/etc/cardrag/stack.env}
lock_root=${CARDRAG_SETUP_LOCK_ROOT:-/run/lock/cardrag}

assign_config_value() {
    config_name=$1
    config_value=$2
    case "$config_name" in
        CARDRAG_STATE_ROOT) state_root=$config_value ;;
        CARDRAG_DATA_ROOT) data_root=$config_value; data_root_explicit=true ;;
        CARDRAG_IMPORT_ROOT) import_root=$config_value; import_root_explicit=true ;;
        CARDRAG_ARCHIVE_ROOT) archive_root=$config_value ;;
        CARDRAG_ARCHIVE_EXPECTED_SOURCE) archive_expected_source=$config_value ;;
        CARDRAG_CONFIG_ROOT) config_root=$config_value; config_root_explicit=true ;;
        CARDRAG_SECRETS_DIR) secret_root=$config_value; secret_root_explicit=true ;;
        CARDRAG_DEPLOYMENT_ROOT) deployment_root=$config_value ;;
        CARDRAG_POSTGRES_VOLUME) postgres_volume=$config_value ;;
        CARDRAG_CODEX_AUTH_VOLUME) codex_volume=$config_value ;;
        KEYCLOAK_PUBLIC_URL) keycloak_public_url=$config_value ;;
        KEYCLOAK_BOOTSTRAP_ADMIN_USERNAME) bootstrap_username=$config_value ;;
        CARDRAG_OPENROUTER_KEY_FILE) openrouter_key_file=$config_value ;;
        CARDRAG_RELEASE_MANIFEST) release_manifest=$config_value ;;
        CARDRAG_STACK_ENV_FILE) stack_env_file=$config_value ;;
        CARDRAG_SETUP_LOCK_ROOT) lock_root=$config_value ;;
        *) die "unsupported or secret-bearing configuration key: $config_name" ;;
    esac
}

state_root_before_config=$state_root
if [ -n "$config_file" ]; then
    [ -f "$config_file" ] && [ ! -L "$config_file" ] ||
        die "configuration must be a regular non-symlink file"
    while IFS= read -r config_line || [ -n "$config_line" ]; do
        case "$config_line" in
            ''|'#'*) continue ;;
            *=*) ;;
            *) die "configuration lines must use unquoted KEY=value syntax" ;;
        esac
        config_name=${config_line%%=*}
        config_value=${config_line#*=}
        case "$config_name" in
            ''|*[!A-Z0-9_]*) die "invalid configuration key" ;;
        esac
        # These values are later written as an unquoted Portainer env file.
        case "$config_value" in
            *" "*|*"	"*|*"#"*|*"'"*|*'"'*)
                die "configuration values must not contain whitespace, comments, or quotes"
                ;;
        esac
        assign_config_value "$config_name" "$config_value"
    done <"$config_file"
fi
if [ "$state_root" != "$state_root_before_config" ]; then
    [ "$data_root_explicit" = true ] || data_root=$state_root/runtime
    [ "$import_root_explicit" = true ] || import_root=$state_root/imports
    [ "$config_root_explicit" = true ] || config_root=$state_root/config
    [ "$secret_root_explicit" = true ] || secret_root=$state_root/secrets
fi
[ -n "$deployment_root" ] || deployment_root=$config_root/deployment
handoff_config=${config_file:-SETUP-CONFIG}
python3 - "$stack_env_file" "$handoff_config" <<'PY'
import re
import sys

for value in sys.argv[1:]:
    if not value or not value.isascii() or re.fullmatch(r"[A-Za-z0-9._/@:+-]+", value) is None:
        raise SystemExit("handoff paths contain unsafe output characters")
PY

print_next_steps() {
    handoff_stage=${1:-bootstrap}
    if [ "$handoff_stage" = main ]; then
        cat <<EOF
CardRAG bootstrap credential retirement is complete. No secret value is
printed here.

1. In Portainer (Docker Standalone), create Stack 'cardrag' from:
   $script_root/deploy/portainer/cardrag-stack.yaml
2. Load this non-secret environment file into that Stack:
   $stack_env_file
3. External step: in the worker console run 'codex login --device-auth',
   verify login status, and restart only the worker container.
4. Keep mutation and maintenance profiles disabled until the normal Stack is
   healthy and the Codex login step is complete.
EOF
        return
    fi
    cat <<EOF
CardRAG host preparation is complete. No secret value is printed here.

1. In Portainer (Docker Standalone), create Stack 'cardrag-bootstrap' from:
   $script_root/deploy/portainer/cardrag-bootstrap-stack.yaml
2. Load this non-secret environment file into that Stack:
   $stack_env_file
3. External step: expose the configured Keycloak origin through HTTPS. After
   Keycloak is healthy, sign in as the bootstrap user, create and verify a
   permanent administrator. Revoke, disable, or rotate the bootstrap account
   credential and verify its old login is rejected. Then remove the bootstrap
   Stack without deleting the external PostgreSQL volume.
4. Record completion and retire the one-time credential with:
   sudo $script_root/deploy/portainer/cardrag-portainer-setup.sh \\
     --non-interactive --config $handoff_config \\
     --bootstrap-complete --confirmed-permanent-admin \\
     --confirmed-bootstrap-admin-revoked
   Then create Stack 'cardrag' from:
   $script_root/deploy/portainer/cardrag-stack.yaml
   Load the same environment file: $stack_env_file
5. External step: in the worker console run 'codex login --device-auth',
   verify login status, and restart only the worker container.
6. Keep mutation and maintenance profiles disabled until those two external
   steps are complete and the normal Stack is healthy.
EOF
}

if [ "$next_steps_only" = true ]; then
    print_next_steps
    exit 0
fi

prompt_setting() {
    prompt_label=$1
    prompt_default=$2
    if [ -n "$prompt_default" ]; then
        printf '%s [%s]: ' "$prompt_label" "$prompt_default" >&2
    else
        printf '%s: ' "$prompt_label" >&2
    fi
    IFS= read -r prompt_answer || die "interactive input ended"
    prompt_result=${prompt_answer:-$prompt_default}
}

if [ "$interaction" = auto ]; then
    if [ -t 0 ]; then interaction=interactive; else interaction=non-interactive; fi
fi
if [ "$interaction" = interactive ]; then
    previous_state_root=$state_root
    prompt_setting "State root" "$state_root"; state_root=$prompt_result
    if [ "$state_root" != "$previous_state_root" ]; then
        [ "$data_root" != "$previous_state_root/runtime" ] || data_root=$state_root/runtime
        [ "$import_root" != "$previous_state_root/imports" ] || import_root=$state_root/imports
        [ "$config_root" != "$previous_state_root/config" ] || config_root=$state_root/config
        [ "$secret_root" != "$previous_state_root/secrets" ] || secret_root=$state_root/secrets
        [ "$deployment_root" != "$previous_state_root/config/deployment" ] ||
            deployment_root=$state_root/config/deployment
    fi
    prompt_setting "Runtime data root" "$data_root"; data_root=$prompt_result
    prompt_setting "Legacy import root" "$import_root"; import_root=$prompt_result
    prompt_setting "Portable archive root" "$archive_root"; archive_root=$prompt_result
    prompt_setting "Archive mount source (optional)" "$archive_expected_source"
    archive_expected_source=$prompt_result
    prompt_setting "Configuration root" "$config_root"; config_root=$prompt_result
    prompt_setting "Secret root" "$secret_root"; secret_root=$prompt_result
    prompt_setting "Deployment metadata root" "$deployment_root"; deployment_root=$prompt_result
    prompt_setting "PostgreSQL external volume" "$postgres_volume"; postgres_volume=$prompt_result
    prompt_setting "Codex auth external volume" "$codex_volume"; codex_volume=$prompt_result
    prompt_setting "Public Keycloak HTTPS origin" "$keycloak_public_url"
    keycloak_public_url=$prompt_result
    prompt_setting "Keycloak bootstrap username" "$bootstrap_username"
    bootstrap_username=$prompt_result
    prompt_setting "Private OpenRouter key file" "$openrouter_key_file"
    openrouter_key_file=$prompt_result
    prompt_setting "Release manifest file" "$release_manifest"; release_manifest=$prompt_result
    prompt_setting "Generated Portainer environment file" "$stack_env_file"
    stack_env_file=$prompt_result
fi

for required_value in "$keycloak_public_url" "$openrouter_key_file" "$release_manifest"; do
    [ -n "$required_value" ] || die \
        "KEYCLOAK_PUBLIC_URL, CARDRAG_OPENROUTER_KEY_FILE, and CARDRAG_RELEASE_MANIFEST are required"
done
state_stage=$state_root.cardrag-quick-setup.pending

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "required command is unavailable: $1"
}
for command_name in docker python3 sha256sum find cmp install getent flock git; do
    require_command "$command_name"
done
if [ "$dry_run" != true ] && [ "$(id -u)" -ne 0 ]; then
    die "host preparation must run as root (use --dry-run to validate without root)"
fi
docker_gid=$(getent group docker | awk -F: 'NR == 1 {print $3}')
[ -n "$docker_gid" ] || die "Docker group is unavailable"

check_absolute_safe() {
    checked_path=$1
    checked_kind=$2
    case "$checked_path" in /*) ;; *) die "$checked_kind must be absolute" ;; esac
    case "$checked_path" in
        /|/srv|/mnt|/var|/var/lib|/etc|/opt|/tmp) die "$checked_kind is too broad" ;;
    esac
    path_cursor=/
    old_ifs=$IFS
    IFS=/
    # shellcheck disable=SC2086
    set -- $checked_path
    IFS=$old_ifs
    for path_component do
        [ -n "$path_component" ] || continue
        if [ "$path_cursor" = / ]; then
            path_cursor=/$path_component
        else
            path_cursor=$path_cursor/$path_component
        fi
        [ ! -L "$path_cursor" ] || die "$checked_kind must not traverse a symlink"
    done
}

for root_path in "$state_root" "$data_root" "$import_root" "$archive_root" \
    "$config_root" "$secret_root" "$deployment_root" "$lock_root" \
    "$state_stage"; do
    check_absolute_safe "$root_path" "host root"
done
check_absolute_safe "$stack_env_file" "Stack environment file"
check_absolute_safe "$openrouter_key_file" "OpenRouter key file"
check_absolute_safe "$release_manifest" "release manifest"

python3 - "$state_root" "$data_root" "$import_root" "$archive_root" \
    "$config_root" "$secret_root" "$deployment_root" "$stack_env_file" \
    "$openrouter_key_file" "$release_manifest" "$lock_root" \
    "$state_stage" <<'PY'
import os
import sys

for value in sys.argv[1:]:
    if value != os.path.normpath(value):
        raise SystemExit("host paths must use lexical-normal absolute form")
PY

python3 - "$state_stage" "$archive_root" "$lock_root" "$stack_env_file" <<'PY'
import os
import sys

stage, archive, lock, environment = (os.path.normpath(value) for value in sys.argv[1:])
for protected in (archive, lock):
    if os.path.commonpath((stage, protected)) in {stage, protected}:
        raise SystemExit("quick-setup staging root overlaps another protected root")
if os.path.commonpath((stage, environment)) in {stage, environment}:
    raise SystemExit("Stack environment path must be outside the setup staging root")
PY

for volume_name in "$postgres_volume" "$codex_volume"; do
    case "$volume_name" in
        ''|?|[!A-Za-z0-9]*|*[!A-Za-z0-9_.-]*)
            die "Docker volume name must start alphanumeric and contain at least two safe characters"
            ;;
        *) ;;
    esac
done
[ "$postgres_volume" != "$codex_volume" ] || die "external volume names must be distinct"
case "$bootstrap_username" in
    ''|*[!A-Za-z0-9_.-]*) die "bootstrap username contains unsafe characters" ;;
esac

python3 - "$keycloak_public_url" "$state_root" "$data_root" "$import_root" \
    "$archive_root" "$config_root" "$secret_root" "$deployment_root" \
    "$stack_env_file" "$lock_root" <<'PY'
import os
import sys
from urllib.parse import urlsplit

public_url = sys.argv[1]
parsed = urlsplit(public_url)
if (
    parsed.scheme != "https"
    or not parsed.hostname
    or parsed.username is not None
    or parsed.password is not None
    or parsed.path not in {"", "/"}
    or parsed.query
    or parsed.fragment
    or public_url.endswith("/")
    or parsed.hostname == "localhost"
    or parsed.hostname.endswith((".example", ".invalid", ".local", ".test"))
):
    raise SystemExit("KEYCLOAK_PUBLIC_URL must be a live HTTPS origin without a path")

state_root, data_root, import_root, archive_root, config_root, secret_root, deployment_root = (
    os.path.normpath(value) for value in sys.argv[2:9]
)
isolated = (data_root, import_root, config_root, secret_root)
for child_root in isolated:
    if os.path.dirname(child_root) != state_root:
        raise SystemExit("runtime, import, configuration, and secret roots must be direct state-root children")
    if os.path.basename(child_root) == "migration":
        raise SystemExit("migration is a reserved state-root child name")
for index, left in enumerate(isolated):
    for right in isolated[index + 1 :]:
        if os.path.commonpath((left, right)) in {left, right}:
            raise SystemExit("runtime, import, configuration, and secret roots must not overlap")
if os.path.commonpath((archive_root, state_root)) in {archive_root, state_root}:
    raise SystemExit("archive root must be outside the CardRAG state root")
if deployment_root != os.path.join(config_root, "deployment"):
    raise SystemExit("deployment metadata root must be CONFIG_ROOT/deployment")
stack_env_file = os.path.normpath(sys.argv[9])
lock_root = os.path.normpath(sys.argv[10])
for protected_root in (state_root, archive_root):
    if os.path.commonpath((lock_root, protected_root)) in {lock_root, protected_root}:
        raise SystemExit("lock root must not overlap state or archive roots")
for protected_root in (state_root, archive_root, lock_root):
    if os.path.commonpath((stack_env_file, protected_root)) in {
        stack_env_file,
        protected_root,
    }:
        raise SystemExit("Stack environment path must be outside state and archive roots")
if os.path.isdir(stack_env_file):
    raise SystemExit("Stack environment path must name a file")
PY

environment_parent=$(dirname -- "$stack_env_file")
validate_environment_parent() {
    [ -d "$environment_parent" ] && [ ! -L "$environment_parent" ] &&
        [ "$(stat -c '%a' "$environment_parent")" = 750 ] &&
        [ "$(stat -c '%u:%g' "$environment_parent")" = "0:$docker_gid" ] ||
        die "Stack environment parent must be mode 0750 and owned by root:docker"
}
if [ -e "$environment_parent" ] || [ -L "$environment_parent" ]; then
    validate_environment_parent
fi

python3 - "$state_root" "$data_root" "$import_root" "$archive_root" \
    "$archive_expected_source" "$config_root" "$secret_root" "$deployment_root" \
    "$postgres_volume" "$codex_volume" "$keycloak_public_url" \
    "$bootstrap_username" "$stack_env_file" "$lock_root" <<'PY'
import sys
import re

for value in sys.argv[1:]:
    if (
        not value.isascii()
        or re.fullmatch(r"[A-Za-z0-9._/@:+-]*", value) is None
    ):
        raise SystemExit("Portainer environment values contain unsafe characters")
PY

[ -f "$openrouter_key_file" ] && [ ! -L "$openrouter_key_file" ] ||
    die "OpenRouter key source must be a regular non-symlink file"
[ -f "$release_manifest" ] && [ ! -L "$release_manifest" ] ||
    die "release manifest must be a regular non-symlink file"
python3 - "$openrouter_key_file" <<'PY'
import os
import stat
import sys

path = sys.argv[1]
metadata = os.stat(path, follow_symlinks=False)
if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
    raise SystemExit("OpenRouter key source must have owner-only permissions")
value = open(path, "rb").read()
if not value or b"\0" in value or value.count(b"\n") > 1:
    raise SystemExit("OpenRouter key source must contain exactly one non-empty line")
if value.endswith(b"\n"):
    value = value[:-1]
if not value or any(byte <= 0x20 or byte >= 0x7f for byte in value):
    raise SystemExit("OpenRouter key source contains invalid whitespace or control bytes")
PY

work_root=$(mktemp -d)
trap 'rm -rf "$work_root"' EXIT HUP INT TERM
openrouter_key_snapshot=$work_root/openrouter-key
python3 - "$openrouter_key_file" "$openrouter_key_snapshot" <<'PY'
import os
import stat
import sys

source_path, snapshot_path = sys.argv[1:3]
source = os.open(source_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
try:
    before = os.fstat(source)
    value = bytearray()
    while True:
        chunk = os.read(source, 4096)
        if not chunk:
            break
        value.extend(chunk)
    after = os.fstat(source)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_mode & 0o077
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise SystemExit("OpenRouter key source changed or has unsafe metadata")
finally:
    os.close(source)
raw_value = bytes(value)
if not raw_value or b"\0" in raw_value or raw_value.count(b"\n") > 1:
    raise SystemExit("OpenRouter key source must contain exactly one line")
normalized_value = raw_value[:-1] if raw_value.endswith(b"\n") else raw_value
if not normalized_value or any(byte <= 0x20 or byte >= 0x7f for byte in normalized_value):
    raise SystemExit("OpenRouter key source contains invalid bytes")
target = os.open(snapshot_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
try:
    view = memoryview(raw_value)
    while view:
        view = view[os.write(target, view):]
    os.fchmod(target, 0o400)
    os.fsync(target)
finally:
    os.close(target)
PY
role_image_file=$work_root/role-images
release_manifest_snapshot=$work_root/release-manifest.json
python3 - "$release_manifest" "$release_manifest_snapshot" <<'PY'
import os
import stat
import sys

source_path, snapshot_path = sys.argv[1:3]
source = os.open(source_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
try:
    before = os.fstat(source)
    if not stat.S_ISREG(before.st_mode):
        raise SystemExit("release manifest source is not a regular file")
    content = bytearray()
    while True:
        chunk = os.read(source, 1024 * 1024)
        if not chunk:
            break
        content.extend(chunk)
    after = os.fstat(source)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise SystemExit("release manifest changed while it was being snapshotted")
finally:
    os.close(source)
target = os.open(snapshot_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
try:
    os.write(target, content)
    os.fchmod(target, 0o400)
    os.fsync(target)
finally:
    os.close(target)
PY
git -C "$script_root" diff --quiet HEAD -- \
    pyproject.toml \
    src/cardrag/__init__.py \
    deploy/portainer/cardrag-stack.yaml \
    deploy/portainer/cardrag-bootstrap-stack.yaml \
    deploy/portainer/prepare-host-storage.sh \
    deploy/portainer/install-deployment-metadata.sh \
    deploy/portainer/scripts \
    deploy/postgres/init-databases.sh \
    deploy/keycloak/cardrag-realm.json \
    deploy/keycloak/entrypoint.sh \
    src/cardrag/db/migrations ||
    die "release-critical checkout files differ from HEAD"
critical_untracked=$(git -C "$script_root" ls-files --others --exclude-standard -- \
    pyproject.toml \
    src/cardrag/__init__.py \
    deploy/portainer/cardrag-stack.yaml \
    deploy/portainer/cardrag-bootstrap-stack.yaml \
    deploy/portainer/prepare-host-storage.sh \
    deploy/portainer/install-deployment-metadata.sh \
    deploy/portainer/scripts \
    deploy/postgres/init-databases.sh \
    deploy/keycloak/cardrag-realm.json \
    deploy/keycloak/entrypoint.sh \
    src/cardrag/db/migrations)
[ -z "$critical_untracked" ] ||
    die "release-critical checkout contains untracked files"
if git -C "$script_root" cat-file -e \
    HEAD:deploy/portainer/cardrag-portainer-setup.sh >/dev/null 2>&1; then
    git -C "$script_root" diff --quiet HEAD -- \
        deploy/portainer/cardrag-portainer-setup.sh ||
        die "the tracked quick installer differs from HEAD"
fi
checkout_revision=$(git -C "$script_root" rev-parse --verify HEAD)
project_version=$(python3 - "$script_root/src/cardrag/__init__.py" <<'PY'
import re
import sys

value = open(sys.argv[1], encoding="utf-8").read()
match = re.fullmatch(r'\s*""".*?"""\s*__version__\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"\s*', value, re.DOTALL)
if match is None:
    raise SystemExit("could not read the exact CardRAG version declaration")
print(match.group(1))
PY
)
python3 - "$release_manifest_snapshot" "$role_image_file" "$checkout_revision" \
    "$project_version" <<'PY'
import json
import re
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    manifest = json.load(stream)
if not isinstance(manifest, dict) or manifest.get("schema") != "cardrag.container-release.v3":
    raise SystemExit("release manifest must use cardrag.container-release.v3")
version = manifest.get("version")
revision = manifest.get("git_sha")
roles = manifest.get("roles")
if not isinstance(version, str) or not version or not re.fullmatch(r"[0-9a-f]{40}", str(revision)):
    raise SystemExit("release manifest has an invalid version or Git revision")
if revision != sys.argv[3] or version != sys.argv[4]:
    raise SystemExit("release manifest does not match the current release checkout")
if not isinstance(roles, dict) or set(roles) != {"admin", "worker", "mcp"}:
    raise SystemExit("release manifest must contain exactly admin, worker, and mcp")
images = []
for role in ("admin", "worker", "mcp"):
    value = roles[role]
    digest = value.get("digest") if isinstance(value, dict) else None
    image = value.get("image") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or value.get("schema") != "cardrag.container-release-part.v3"
        or value.get("role") != role
        or value.get("version") != version
        or value.get("git_sha") != revision
        or not isinstance(image, str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9._/:+-]*", image)
        or not isinstance(digest, str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
    ):
        raise SystemExit(f"release manifest role is invalid: {role}")
    images.append(f"{image}@{digest}")
if (
    len(set(images)) != 3
    or len({roles[role]["digest"] for role in roles}) != 3
):
    raise SystemExit("release manifest runtime roles must resolve to distinct digests")
with open(sys.argv[2], "x", encoding="utf-8") as stream:
    stream.write("\n".join(images) + "\n")
PY
admin_image=$(sed -n '1p' "$role_image_file")
worker_image=$(sed -n '2p' "$role_image_file")
mcp_image=$(sed -n '3p' "$role_image_file")
[ -n "$admin_image" ] && [ -n "$worker_image" ] && [ -n "$mcp_image" ] ||
    die "release manifest did not yield all runtime images"

manifest_digest=$(sha256sum "$release_manifest_snapshot" | awk '{print $1}')
openrouter_key_digest=$(sha256sum "$openrouter_key_snapshot" | awk '{print $1}')
setup_fingerprint=$(
    printf '%s\n' \
        cardrag-portainer-quick-setup.v1 \
        "$state_root" "$data_root" "$import_root" "$archive_root" \
        "$archive_expected_source" "$config_root" "$secret_root" \
        "$deployment_root" "$postgres_volume" "$codex_volume" \
        "$keycloak_public_url" "$bootstrap_username" "$openrouter_key_file" \
        "$openrouter_key_digest" \
        "$manifest_digest" "$stack_env_file" "$lock_root" |
        sha256sum | awk '{print $1}'
)
guard_root=$secret_root/.cardrag-quick-setup
state_file=$guard_root/state
state_pending=$guard_root/.state.pending
expected_state=$work_root/expected-state
printf '%s\n%s\n' 'schema=cardrag-portainer-quick-setup.v1' \
    "fingerprint=$setup_fingerprint" >"$expected_state"

has_entries() {
    inspected_root=$1
    if [ ! -e "$inspected_root" ] && [ ! -L "$inspected_root" ]; then
        return 1
    fi
    [ -d "$inspected_root" ] && [ ! -L "$inspected_root" ] ||
        die "expected an absent or non-symlink directory: $inspected_root"
    [ -r "$inspected_root" ] && [ -x "$inspected_root" ] ||
        die "directory is not readable/searchable: $inspected_root"
    if ! inspected_entry=$(find "$inspected_root" -mindepth 1 -maxdepth 1 -print -quit); then
        die "could not inspect directory: $inspected_root"
    fi
    [ -n "$inspected_entry" ]
}

validate_state_stage() {
    python3 - "$state_stage" "$expected_state" "${secret_root##*/}" \
        "${guard_root##*/}" <<'PY'
import os
import stat
import sys
from pathlib import Path

stage = Path(sys.argv[1])
expected = Path(sys.argv[2]).read_bytes()
secret_name, guard_name = sys.argv[3:5]
owner = os.geteuid()


def require_directory(path, mode):
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != owner
        or stat.S_IMODE(metadata.st_mode) != mode
    ):
        raise SystemExit(f"unsafe quick-setup staging directory: {path.name}")


require_directory(stage, 0o755)
stage_entries = {item.name: item for item in stage.iterdir()}
if set(stage_entries) - {secret_name}:
    raise SystemExit("quick-setup staging root contains foreign entries")
secret = stage / secret_name
if not os.path.lexists(secret):
    raise SystemExit(0)
require_directory(secret, 0o700)
secret_entries = {item.name: item for item in secret.iterdir()}
if set(secret_entries) - {guard_name}:
    raise SystemExit("quick-setup staging secret root contains foreign entries")
guard = secret / guard_name
if not os.path.lexists(guard):
    raise SystemExit(0)
require_directory(guard, 0o700)
guard_entries = {item.name: item for item in guard.iterdir()}
if set(guard_entries) - {"state", ".state.pending"}:
    raise SystemExit("quick-setup staging guard contains foreign entries")
for name, item in guard_entries.items():
    metadata = item.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != owner
        or stat.S_IMODE(metadata.st_mode) not in {0o400, 0o600}
        or metadata.st_size > 4096
    ):
        raise SystemExit("quick-setup staging state file is unsafe")
    if name == "state" and item.read_bytes() != expected:
        raise SystemExit("quick-setup staging state belongs to different settings")
PY
}

setup_resume=false
state_needs_recovery=false
state_stage_present=false
if [ -e "$state_stage" ] || [ -L "$state_stage" ]; then
    [ ! -e "$state_root" ] && [ ! -L "$state_root" ] ||
        die "setup staging root conflicts with an existing state root"
    validate_state_stage
    state_stage_present=true
fi
if [ -e "$guard_root" ] || [ -L "$guard_root" ]; then
    [ "$state_stage_present" != true ] ||
        die "completed and pending setup ownership claims conflict"
    [ -d "$state_root" ] && [ ! -L "$state_root" ] &&
        [ "$(stat -c '%a' "$state_root")" = 755 ] &&
        [ "$(stat -c '%u:%g' "$state_root")" = 0:0 ] ||
        die "installer-owned state root must be mode 0755 and owned by root"
    [ -d "$secret_root" ] && [ ! -L "$secret_root" ] &&
        [ "$(stat -c '%a' "$secret_root")" = 700 ] &&
        [ "$(stat -c '%u:%g' "$secret_root")" = 0:0 ] ||
        die "installer-owned secret root must be mode 0700 and owned by root"
    [ -d "$guard_root" ] && [ ! -L "$guard_root" ] ||
        die "quick-setup ownership marker is unsafe"
    [ "$(stat -c '%a' "$guard_root")" = 700 ] &&
        [ "$(stat -c '%u:%g' "$guard_root")" = 0:0 ] ||
        die "quick-setup ownership marker must be mode 0700 and owned by root"
    if [ -f "$state_file" ] && [ ! -L "$state_file" ]; then
        [ "$(stat -c '%a' "$state_file")" = 600 ] &&
            [ "$(stat -c '%u:%g' "$state_file")" = 0:0 ] ||
            die "quick-setup state marker has unsafe metadata"
        cmp -s "$expected_state" "$state_file" ||
            die "existing quick-setup state belongs to different settings"
    elif [ -f "$state_pending" ] && [ ! -L "$state_pending" ] &&
        { [ "$(stat -c '%a' "$state_pending")" = 400 ] ||
          [ "$(stat -c '%a' "$state_pending")" = 600 ]; } &&
        [ "$(stat -c '%u:%g' "$state_pending")" = 0:0 ]; then
        state_needs_recovery=true
    else
        die "quick-setup ownership marker has no durable matching state"
    fi
    setup_resume=true
elif has_entries "$secret_root"; then
    die "refusing to overwrite a non-empty secret root"
fi

if [ -e "$state_pending" ] || [ -L "$state_pending" ]; then
    if ! { [ -f "$state_pending" ] && [ ! -L "$state_pending" ] &&
        { [ "$(stat -c '%a' "$state_pending")" = 400 ] ||
          [ "$(stat -c '%a' "$state_pending")" = 600 ]; } &&
        [ "$(stat -c '%u:%g' "$state_pending")" = 0:0 ]; }; then
        die "quick-setup pending state is unsafe or inconsistent"
    fi
    state_needs_recovery=true
fi

if [ "$setup_resume" != true ]; then
    [ ! -e "$state_root" ] && [ ! -L "$state_root" ] ||
        die "refusing to claim an existing markerless state root"
    [ ! -e "$stack_env_file" ] && [ ! -L "$stack_env_file" ] ||
        die "refusing to overwrite an existing Stack environment file"
    for volume_name in "$postgres_volume" "$codex_volume"; do
        if docker volume inspect "$volume_name" >/dev/null 2>&1; then
            die "fresh-install external volume already exists: $volume_name"
        fi
    done
fi

if [ "$dry_run" = true ]; then
    if [ "$setup_resume" = true ]; then
        echo "dry-run passed for the matching quick-setup resume; no host data was changed"
    else
        echo "dry-run passed for a fresh host; no host data was changed"
    fi
    exit 0
fi

if { [ "$check_only" = true ] || [ "$bootstrap_complete_mode" = true ]; } &&
    [ "$setup_resume" != true ]; then
    die "no installer-owned quick setup exists to check"
fi

# Serialize every mutating run by the target secret root. A matching durable
# state marker remains the authority after reboot; this flock only prevents
# two concurrent installers from passing the same fresh-host preflight.
lock_key=$(printf '%s\n' "$secret_root" | sha256sum | awk '{print $1}')
lock_file=$lock_root/cardrag-portainer-setup-$lock_key.lock
if [ "$check_only" != true ]; then
    if [ -e "$lock_root" ] || [ -L "$lock_root" ]; then
        [ -d "$lock_root" ] && [ ! -L "$lock_root" ] ||
            die "quick-setup lock root is unsafe"
    else
        install -d -o root -g root -m 0755 "$lock_root"
    fi
    [ "$(stat -c '%a' "$lock_root")" = 755 ] &&
        [ "$(stat -c '%u:%g' "$lock_root")" = 0:0 ] ||
        die "quick-setup lock root must be mode 0755 and owned by root"
    python3 - "$lock_file" "$lock_root" <<'PY'
import os
import stat
import sys

path, root = sys.argv[1:3]
flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
descriptor = os.open(path, flags, 0o600)
try:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        raise SystemExit("quick-setup lock path is not a regular file")
    os.fchmod(descriptor, 0o600)
    if os.geteuid() == 0:
        os.fchown(descriptor, 0, 0)
    os.fsync(descriptor)
finally:
    os.close(descriptor)
directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory)
finally:
    os.close(directory)
PY
    [ "$(stat -c '%a' "$lock_file")" = 600 ] &&
        [ "$(stat -c '%u:%g' "$lock_file")" = 0:0 ] ||
        die "quick-setup lock file is unsafe"
    exec 9<>"$lock_file"
    flock -n 9 || die "another quick setup is running for this secret root"
    # Repeat the destructive-boundary checks after acquiring the lock.
    if [ "$setup_resume" != true ]; then
        [ ! -e "$state_root" ] && [ ! -L "$state_root" ] ||
            die "state root appeared while waiting for the setup lock"
        if [ "$state_stage_present" = true ]; then
            validate_state_stage
        else
            [ ! -e "$state_stage" ] && [ ! -L "$state_stage" ] ||
                die "setup staging root appeared while waiting for the setup lock"
        fi
        if [ -e "$guard_root" ] || has_entries "$secret_root"; then
            die "secret root changed while waiting for the setup lock"
        fi
        for volume_name in "$postgres_volume" "$codex_volume"; do
            ! docker volume inspect "$volume_name" >/dev/null 2>&1 ||
                die "external volume appeared while waiting for the setup lock"
        done
    fi
fi

if [ "$state_needs_recovery" = true ]; then
    [ "$check_only" != true ] ||
        die "quick-setup state commit is pending; run the normal resume first"
    python3 - "$expected_state" "$state_file" "$state_pending" "$guard_root" <<'PY'
import os
import stat
import sys

expected_path, state_path, pending_path, root = sys.argv[1:5]
expected = open(expected_path, "rb").read()
metadata = os.lstat(pending_path)
if (
    not stat.S_ISREG(metadata.st_mode)
    or metadata.st_uid != os.geteuid()
    or stat.S_IMODE(metadata.st_mode) not in {0o400, 0o600}
):
    raise SystemExit("quick-setup pending state cannot be recovered safely")
flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
descriptor = os.open(pending_path, flags)
try:
    if os.read(descriptor, len(expected) + 1) != expected:
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.ftruncate(descriptor, 0)
        os.write(descriptor, expected)
    os.fchmod(descriptor, 0o600)
    os.fsync(descriptor)
finally:
    os.close(descriptor)
try:
    state = open(state_path, "rb").read()
except FileNotFoundError:
    os.link(pending_path, state_path)
else:
    if state != expected:
        raise SystemExit("quick-setup state and pending commit differ")
directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory)
finally:
    os.close(directory)
os.unlink(pending_path)
directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory)
finally:
    os.close(directory)
PY
fi

phase_present() {
    phase_path=$guard_root/$1
    [ -f "$phase_path" ] && [ ! -L "$phase_path" ]
}

write_phase() {
    phase_path=$guard_root/$1
    if [ -e "$phase_path" ] || [ -L "$phase_path" ]; then
        [ -f "$phase_path" ] && [ ! -L "$phase_path" ] ||
            die "quick-setup phase marker is unsafe"
        return
    fi
    python3 - "$phase_path" "$guard_root" <<'PY'
import os
import sys

descriptor = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
try:
    os.fchmod(descriptor, 0o600)
    os.fsync(descriptor)
finally:
    os.close(descriptor)
directory = os.open(sys.argv[2], os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory)
finally:
    os.close(directory)
PY
}

if [ "$setup_resume" != true ]; then
    python3 - "$state_stage" "$state_root" "$expected_state" \
        "${secret_root##*/}" "${guard_root##*/}" \
        "$state_stage_present" <<'PY'
import ctypes
import errno
import os
import stat
import sys
from pathlib import Path

stage = Path(sys.argv[1])
target = Path(sys.argv[2])
expected = Path(sys.argv[3]).read_bytes()
secret_name, guard_name, preexisting = sys.argv[4:7]
owner = os.geteuid()


def fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def ensure_directory(path, mode):
    try:
        os.mkdir(path, mode)
    except FileExistsError:
        pass
    else:
        os.chmod(path, mode)
        if owner == 0:
            os.chown(path, 0, 0)
        fsync_directory(path.parent)
    metadata = os.lstat(path)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != owner
        or stat.S_IMODE(metadata.st_mode) != mode
    ):
        raise SystemExit("quick-setup staging directory metadata changed")


parent = target.parent
parent_metadata = os.lstat(parent)
if not stat.S_ISDIR(parent_metadata.st_mode):
    raise SystemExit("state-root parent must already be a real directory")
if os.path.lexists(target):
    raise SystemExit("state root appeared while claiming setup ownership")
if os.path.lexists(stage) and preexisting != "true":
    raise SystemExit("setup staging root appeared while waiting for the lock")
ensure_directory(stage, 0o755)
secret = stage / secret_name
ensure_directory(secret, 0o700)
guard = secret / guard_name
ensure_directory(guard, 0o700)
state = guard / "state"
pending = guard / ".state.pending"
if state.exists():
    metadata = state.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != owner
        or stat.S_IMODE(metadata.st_mode) not in {0o400, 0o600}
        or state.read_bytes() != expected
    ):
        raise SystemExit("quick-setup staging state is inconsistent")
    os.chmod(state, 0o600)
else:
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(pending, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != owner:
            raise SystemExit("quick-setup staging pending state is unsafe")
        os.ftruncate(descriptor, 0)
        os.write(descriptor, expected)
        os.fchmod(descriptor, 0o600)
        if owner == 0:
            os.fchown(descriptor, 0, 0)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.link(pending, state)
    fsync_directory(guard)
if pending.exists():
    os.unlink(pending)
    fsync_directory(guard)
if {item.name for item in stage.iterdir()} != {secret_name}:
    raise SystemExit("quick-setup staging root inventory changed")
if {item.name for item in secret.iterdir()} != {guard_name}:
    raise SystemExit("quick-setup staging secret inventory changed")
if {item.name for item in guard.iterdir()} != {"state"}:
    raise SystemExit("quick-setup staging guard inventory changed")
state_metadata = state.lstat()
if (
    not stat.S_ISREG(state_metadata.st_mode)
    or state_metadata.st_uid != owner
    or stat.S_IMODE(state_metadata.st_mode) != 0o600
    or state.read_bytes() != expected
):
    raise SystemExit("quick-setup staging state changed before commit")
fsync_directory(guard)
fsync_directory(secret)
fsync_directory(stage)
libc = ctypes.CDLL(None, use_errno=True)
renameat2 = getattr(libc, "renameat2", None)
if renameat2 is None:
    raise SystemExit("host libc does not provide atomic no-replace rename")
renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
renameat2.restype = ctypes.c_int
if renameat2(-100, os.fsencode(stage), -100, os.fsencode(target), 1) != 0:
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise SystemExit("state root appeared during atomic ownership claim")
    raise OSError(error, os.strerror(error))
fsync_directory(parent)
PY
    [ "$(stat -c '%a' "$state_root")" = 755 ] &&
        [ "$(stat -c '%u:%g' "$state_root")" = 0:0 ] ||
        die "fresh state root must be mode 0755 and owned by root"
else
    [ "$(stat -c '%a' "$secret_root")" = 700 ] ||
        die "existing secret root must have mode 0700"
fi

validate_guard_inventory() {
    [ "$(stat -c '%a' "$secret_root")" = 700 ] &&
        [ "$(stat -c '%u:%g' "$secret_root")" = 0:0 ] ||
        die "secret root must be mode 0700 and owned by root"
    [ -d "$guard_root" ] && [ ! -L "$guard_root" ] ||
        die "quick-setup ownership marker is missing or unsafe"
    [ "$(stat -c '%a' "$guard_root")" = 700 ] ||
        die "quick-setup ownership marker must have mode 0700"
    [ "$(stat -c '%u:%g' "$guard_root")" = 0:0 ] ||
        die "quick-setup ownership marker must be owned by root"
    for guard_item in "$guard_root"/* "$guard_root"/.[!.]* "$guard_root"/..?*; do
        [ -e "$guard_item" ] || [ -L "$guard_item" ] || continue
        guard_name=${guard_item##*/}
        case "$guard_name" in
            state|storage-ready|secrets-ready|environment-ready|metadata-ready|complete|bootstrap-completing|bootstrap-complete) ;;
            *) die "quick-setup ownership marker contains an unknown entry" ;;
        esac
        [ -f "$guard_item" ] && [ ! -L "$guard_item" ] ||
            die "quick-setup marker entry is unsafe"
        [ "$(stat -c '%u:%g' "$guard_item")" = 0:0 ] ||
            die "quick-setup marker entry must be owned by root"
        case "$guard_name" in
            state)
                [ "$(stat -c '%a' "$guard_item")" = 600 ] ||
                    die "quick-setup state marker has unsafe permissions"
                cmp -s "$expected_state" "$guard_item" ||
                    die "quick-setup state fingerprint changed"
                ;;
            *)
                [ "$(stat -c '%a' "$guard_item")" = 600 ] && [ ! -s "$guard_item" ] ||
                    die "quick-setup phase marker has unsafe contents or permissions"
                ;;
        esac
    done
}
validate_guard_inventory

require_phase_chain() {
    successor=$1
    shift
    if phase_present "$successor"; then
        for predecessor do
            phase_present "$predecessor" ||
                die "impossible quick-setup phase order: $successor requires $predecessor"
        done
    fi
}
require_phase_chain secrets-ready storage-ready
require_phase_chain environment-ready storage-ready secrets-ready
require_phase_chain metadata-ready storage-ready secrets-ready environment-ready
require_phase_chain complete storage-ready secrets-ready environment-ready metadata-ready
require_phase_chain bootstrap-completing storage-ready secrets-ready environment-ready metadata-ready complete
require_phase_chain bootstrap-complete storage-ready secrets-ready environment-ready metadata-ready complete bootstrap-completing

volume_label_template='{{ index .Labels "com.cardrag.quick-setup.schema" }}|{{ index .Labels "com.cardrag.quick-setup.fingerprint" }}|{{ index .Labels "com.cardrag.quick-setup.role" }}'
validate_owned_volume() {
    owned_volume=$1
    owned_role=$2
    owned_labels=$(docker volume inspect --format "$volume_label_template" "$owned_volume" 2>/dev/null) ||
        die "installer-owned external volume is missing: $owned_volume"
    [ "$owned_labels" = "v1|$setup_fingerprint|$owned_role" ] ||
        die "external volume lacks the exact quick-setup ownership labels: $owned_volume"
}

ensure_owned_volume() {
    owned_volume=$1
    owned_role=$2
    if docker volume inspect "$owned_volume" >/dev/null 2>&1; then
        validate_owned_volume "$owned_volume" "$owned_role"
        return
    fi
    if phase_present storage-ready || [ "$check_only" = true ]; then
        die "installer-owned external volume disappeared: $owned_volume"
    fi
    docker volume create \
        --label com.cardrag.quick-setup.schema=v1 \
        --label com.cardrag.quick-setup.fingerprint="$setup_fingerprint" \
        --label com.cardrag.quick-setup.role="$owned_role" \
        --name "$owned_volume" >/dev/null
    validate_owned_volume "$owned_volume" "$owned_role"
}

ensure_owned_volume "$postgres_volume" postgres
ensure_owned_volume "$codex_volume" codex-auth

validate_storage_ready() {
    assert_host_metadata() {
        metadata_path=$1
        metadata_mode=$2
        metadata_owner=$3
        [ -e "$metadata_path" ] && [ ! -L "$metadata_path" ] ||
            die "prepared host path is missing or unsafe: $metadata_path"
        [ "$(stat -c '%a' "$metadata_path")" = "$metadata_mode" ] ||
            die "prepared host path has the wrong mode: $metadata_path"
        [ "$(stat -c '%u:%g' "$metadata_path")" = "$metadata_owner" ] ||
            die "prepared host path has the wrong owner: $metadata_path"
    }

    [ -d "$state_root" ] && [ ! -L "$state_root" ] ||
        die "state root is missing or unsafe"
    assert_host_metadata "$state_root" 755 0:0
    for application_directory in "$data_root" "$data_root/objects" \
        "$data_root/generations" "$data_root/build" "$import_root"; do
        [ -d "$application_directory" ] && [ ! -L "$application_directory" ] ||
            die "prepared application directory is missing or unsafe"
        assert_host_metadata "$application_directory" 750 10001:10001
    done
    [ -d "$data_root/page-cache" ] && [ ! -L "$data_root/page-cache" ] ||
        die "prepared page cache is missing or unsafe"
    assert_host_metadata "$data_root/page-cache" 700 10001:10001
    for root_directory in "$state_root/migration" "$state_root/migration/reports"; do
        [ -d "$root_directory" ] && [ ! -L "$root_directory" ] ||
            die "prepared migration directory is missing or unsafe"
        assert_host_metadata "$root_directory" 750 0:0
    done
    for configuration_directory in "$config_root" "$config_root/postgres" \
        "$config_root/keycloak" "$config_root/portainer" \
        "$config_root/portainer/migrations" "$deployment_root"; do
        [ -d "$configuration_directory" ] && [ ! -L "$configuration_directory" ] ||
            die "prepared configuration directory is missing or unsafe"
        assert_host_metadata "$configuration_directory" 755 0:0
    done

    python3 - "$state_root" "$data_root" "$import_root" "$config_root" \
        "$secret_root" "$script_root/src/cardrag/db/migrations" <<'PY'
import sys
from pathlib import Path

state_root = Path(sys.argv[1])
data_root = Path(sys.argv[2])
import_root = Path(sys.argv[3])
root = Path(sys.argv[4])
secret_root = Path(sys.argv[5])
source_migrations = Path(sys.argv[6])
if {item.name for item in state_root.iterdir()} != {
    data_root.name,
    import_root.name,
    root.name,
    secret_root.name,
    "migration",
}:
    raise SystemExit("state root inventory differs from quick-setup contract")
if {item.name for item in data_root.iterdir()} != {
    "objects",
    "generations",
    "build",
    "page-cache",
}:
    raise SystemExit("runtime root inventory differs from quick-setup contract")
if {item.name for item in root.iterdir()} != {"postgres", "keycloak", "portainer", "deployment"}:
    raise SystemExit("configuration root inventory differs from quick-setup contract")
if {item.name for item in (root / "postgres").iterdir()} != {"init-databases.sh"}:
    raise SystemExit("PostgreSQL configuration inventory differs from quick-setup contract")
if {item.name for item in (root / "keycloak").iterdir()} != {
    "cardrag-realm.json",
    "entrypoint.sh",
}:
    raise SystemExit("Keycloak configuration inventory differs from quick-setup contract")
expected_portainer = {
    "archive-preflight.sh",
    "restore-preflight.sh",
    "schema13-transition.sh",
    "storage-migrate.sh",
    "storage-preflight.sh",
    "verify-runtime-storage.py",
    "migrations",
}
if {item.name for item in (root / "portainer").iterdir()} != expected_portainer:
    raise SystemExit("Portainer configuration inventory differs from quick-setup contract")
expected_migrations = {item.name for item in source_migrations.glob("[0-9][0-9][0-9]_*.sql")}
installed_migrations = {item.name for item in (root / "portainer" / "migrations").iterdir()}
if len(expected_migrations) != 14 or installed_migrations != expected_migrations:
    raise SystemExit("installed migration inventory differs from exact release inventory")
PY

    assert_host_metadata "$config_root/postgres/init-databases.sh" 555 0:0
    cmp -s "$script_root/deploy/postgres/init-databases.sh" \
        "$config_root/postgres/init-databases.sh" ||
        die "installed PostgreSQL initializer differs from the release checkout"
    assert_host_metadata "$config_root/keycloak/cardrag-realm.json" 444 0:0
    cmp -s "$script_root/deploy/keycloak/cardrag-realm.json" \
        "$config_root/keycloak/cardrag-realm.json" ||
        die "installed Keycloak realm differs from the release checkout"
    assert_host_metadata "$config_root/keycloak/entrypoint.sh" 555 0:0
    cmp -s "$script_root/deploy/keycloak/entrypoint.sh" \
        "$config_root/keycloak/entrypoint.sh" ||
        die "installed Keycloak entrypoint differs from the release checkout"

    for configuration_script in archive-preflight.sh restore-preflight.sh \
        schema13-transition.sh storage-migrate.sh storage-preflight.sh \
        verify-runtime-storage.py; do
        assert_host_metadata "$config_root/portainer/$configuration_script" 555 0:0
        cmp -s "$script_root/deploy/portainer/scripts/$configuration_script" \
            "$config_root/portainer/$configuration_script" ||
            die "installed Portainer helper differs from the release checkout"
    done
    migration_count=$(find "$config_root/portainer/migrations" -mindepth 1 \
        -maxdepth 1 -type f -name '*.sql' | wc -l)
    [ "$migration_count" -eq 14 ] ||
        die "installed migration inventory must contain exactly 001 through 014"
    migration_version=1
    while [ "$migration_version" -le 14 ]; do
        migration_prefix=$(printf '%03d_' "$migration_version")
        set -- "$script_root/src/cardrag/db/migrations/$migration_prefix"*.sql
        [ "$#" -eq 1 ] && [ -f "$1" ] ||
            die "release checkout migration inventory is invalid"
        migration_target=$config_root/portainer/migrations/${1##*/}
        assert_host_metadata "$migration_target" 444 0:0
        cmp -s "$1" "$migration_target" ||
            die "installed database migration differs from the release checkout"
        migration_version=$((migration_version + 1))
    done

    for required_volume in "$postgres_volume" "$codex_volume"; do
        docker volume inspect "$required_volume" >/dev/null 2>&1 ||
            die "prepared external volume is missing: $required_volume"
    done
}

if phase_present storage-ready; then
    validate_storage_ready
elif [ "$check_only" = true ]; then
    die "host storage preparation is incomplete"
else
    if has_entries "$data_root/objects" || has_entries "$data_root/generations" ||
        has_entries "$data_root/build" || has_entries "$data_root/page-cache" ||
        has_entries "$import_root"; then
        die "runtime data appeared before host preparation completed"
    fi
    env \
        CARDRAG_STATE_ROOT="$state_root" \
        CARDRAG_DATA_ROOT="$data_root" \
        CARDRAG_IMPORT_ROOT="$import_root" \
        CARDRAG_CONFIG_ROOT="$config_root" \
        CARDRAG_POSTGRES_VOLUME="$postgres_volume" \
        CARDRAG_CODEX_AUTH_VOLUME="$codex_volume" \
        "$script_root/deploy/portainer/prepare-host-storage.sh"
    validate_storage_ready
    write_phase storage-ready
fi

secret_action=install
if [ "$check_only" = true ] || [ "$bootstrap_complete_mode" = true ] ||
    phase_present secrets-ready; then
    secret_action=check
fi
bootstrap_secret_policy=required
if phase_present bootstrap-complete; then
    bootstrap_secret_policy=retired
elif phase_present bootstrap-completing; then
    if [ "$bootstrap_complete_mode" = true ]; then
        bootstrap_secret_policy=transition
    else
        die "bootstrap credential retirement is incomplete; resume --bootstrap-complete"
    fi
fi
python3 - "$secret_action" "$secret_root" "$openrouter_key_snapshot" \
    "$bootstrap_secret_policy" <<'PY'
import os
import re
import secrets
import stat
import sys
from pathlib import Path

action = sys.argv[1]
root = Path(sys.argv[2])
openrouter_source = Path(sys.argv[3])
bootstrap_policy = sys.argv[4]
password_names = (
    "postgres_admin_password",
    "cardrag_db_password",
    "cardrag_worker_db_password",
    "cardrag_mcp_db_password",
    "keycloak_db_password",
    "keycloak_admin_password",
)
database_urls = {
    "cardrag_database_url": ("cardrag", "cardrag_db_password"),
    "cardrag_worker_database_url": ("cardrag_worker", "cardrag_worker_db_password"),
    "cardrag_mcp_database_url": ("cardrag_mcp", "cardrag_mcp_db_password"),
}
expected_files = {
    *(f"{name}.txt" for name in password_names),
    *(f"{name}.txt" for name in database_urls),
    "openrouter_api_key.txt",
}
pending_files = {f".{name}.cardrag-pending" for name in expected_files}
if (
    not root.is_dir()
    or root.is_symlink()
    or root.stat().st_uid != os.geteuid()
    or stat.S_IMODE(root.stat().st_mode) != 0o700
):
    raise SystemExit("secret root is missing or has unsafe type/permissions")
allowed_pending = pending_files if action == "install" else set()
unknown = (
    {item.name for item in root.iterdir()}
    - expected_files
    - allowed_pending
    - {".cardrag-quick-setup"}
)
if unknown:
    raise SystemExit("secret root contains an unexpected entry")


def fsync_root() -> None:
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def pending_path(path: Path) -> Path:
    return root / f".{path.name}.cardrag-pending"


def pending_value(path: Path):
    pending = pending_path(path)
    if not pending.exists():
        return None
    metadata = pending.lstat()
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or mode not in {0o400, 0o444}
    ):
        raise SystemExit(f"secret pending file has unsafe metadata: {pending.name}")
    if mode == 0o400:
        os.chmod(pending, 0o444, follow_symlinks=False)
        descriptor = os.open(pending, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return pending.read_bytes()


def create_exclusive(path: Path, value: bytes) -> None:
    pending = pending_path(path)
    existing_pending = pending_value(path)
    if existing_pending is not None and existing_pending != value:
        pending.unlink()
        fsync_root()
        existing_pending = None
    if existing_pending is None:
        descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(value)
                stream.flush()
            os.fchmod(descriptor, 0o444)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        fsync_root()
    os.link(pending, path)
    fsync_root()
    pending.unlink()
    fsync_root()


def read_secret(path: Path) -> bytes:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o444
    ):
        raise SystemExit(f"secret has unsafe type or permissions: {path.name}")
    return path.read_bytes()


passwords = {}
for name in password_names:
    path = root / f"{name}.txt"
    if name == "keycloak_admin_password" and bootstrap_policy in {"retired", "transition"}:
        if bootstrap_policy == "retired" and path.exists():
            raise SystemExit("retired Keycloak bootstrap secret unexpectedly exists")
        if not path.exists():
            continue
    if not path.exists() and action == "install":
        staged_password = pending_value(path)
        if staged_password is None or not re.fullmatch(rb"[0-9a-f]{64}\n", staged_password):
            if staged_password is not None:
                pending_path(path).unlink()
                fsync_root()
            staged_password = (secrets.token_hex(32) + "\n").encode("ascii")
        create_exclusive(path, staged_password)
    if not path.exists():
        raise SystemExit(f"required secret is missing: {path.name}")
    value = read_secret(path)
    if not re.fullmatch(rb"[0-9a-f]{64}\n", value):
        raise SystemExit(f"generated password is invalid: {path.name}")
    passwords[name] = value.decode("ascii").strip()
if len(set(passwords.values())) != len(passwords):
    raise SystemExit("generated role passwords must be distinct")

for name, (role, password_name) in database_urls.items():
    path = root / f"{name}.txt"
    expected = (
        f"postgresql://{role}:{passwords[password_name]}@postgres:5432/cardrag\n"
    ).encode("ascii")
    if not path.exists() and action == "install":
        create_exclusive(path, expected)
    if not path.exists() or read_secret(path) != expected:
        raise SystemExit(f"database URL does not match its role password: {path.name}")

openrouter_target = root / "openrouter_api_key.txt"
openrouter_value = openrouter_source.read_bytes()
if not openrouter_target.exists() and action == "install":
    create_exclusive(openrouter_target, openrouter_value)
if not openrouter_target.exists() or read_secret(openrouter_target) != openrouter_value:
    raise SystemExit("installed OpenRouter secret differs from its source file")

for expected_file in expected_files:
    committed = root / expected_file
    pending = pending_path(committed)
    if pending.exists():
        if not committed.exists() or read_secret(committed) != pending.read_bytes():
            raise SystemExit(f"secret pending state is inconsistent: {pending.name}")
        pending.unlink()
        fsync_root()
fsync_root()
PY
for installed_secret in "$secret_root"/*.txt; do
    [ -f "$installed_secret" ] && [ ! -L "$installed_secret" ] ||
        die "installed secret inventory is unsafe"
    [ "$(stat -c '%a' "$installed_secret")" = 444 ] &&
        [ "$(stat -c '%u:%g' "$installed_secret")" = 0:0 ] ||
        die "installed secret has unsafe mode or ownership"
done
if [ "$check_only" != true ] && [ "$bootstrap_complete_mode" != true ]; then
    write_phase secrets-ready
fi

archive_source_value=${archive_expected_source:-NOT-CONFIGURED}
expected_env=$work_root/stack.env
cat >"$expected_env" <<EOF
# Generated by cardrag-portainer-setup.sh; contains no secret values.
CARDRAG_STATE_ROOT=$state_root
CARDRAG_DATA_ROOT=$data_root
CARDRAG_IMPORT_ROOT=$import_root
CARDRAG_ARCHIVE_ROOT=$archive_root
CARDRAG_ARCHIVE_EXPECTED_SOURCE=$archive_source_value
CARDRAG_CONFIG_ROOT=$config_root
CARDRAG_SECRETS_DIR=$secret_root
CARDRAG_DEPLOYMENT_ROOT=$deployment_root
KEYCLOAK_PUBLIC_URL=$keycloak_public_url
CARDRAG_OIDC_ISSUER=$keycloak_public_url/realms/cardrag
KEYCLOAK_BOOTSTRAP_ADMIN_USERNAME=$bootstrap_username
CARDRAG_POSTGRES_VOLUME=$postgres_volume
CARDRAG_CODEX_AUTH_VOLUME=$codex_volume
COMPOSE_PROFILES=
CARDRAG_ADMIN_OPERATION_ENABLED=false
CARDRAG_ADMIN_OPERATION=
CARDRAG_ADMIN_OPERATION_ID=
CARDRAG_LEGACY_IMPORT_ENABLED=false
CARDRAG_LEGACY_OPERATION=import
CARDRAG_LEGACY_IMPORT_ID=
CARDRAG_LEGACY_BUNDLE_NAME=READY-NOT-SET
CARDRAG_STATE_EXPORT_ENABLED=false
CARDRAG_STATE_SELF_CONTAINED=false
CARDRAG_STATE_RESTORE_ENABLED=false
CARDRAG_STATE_PACKAGE_PATH=/mnt/cardrag-archive/READY-NOT-SET
CARDRAG_SCHEMA13_TRANSITION_ENABLED=false
CARDRAG_SCHEMA13_TRANSITION_ID=READY-NOT-SET
CARDRAG_SCHEMA13_TRANSITION_RESUME=false
CARDRAG_VALIDATION_ROLLBACK_ENABLED=false
CARDRAG_VALIDATION_ROLLBACK_GENERATION_ID=
CARDRAG_MINIMUM_FREE_GIB=50
CARDRAG_MINIMUM_FREE_PERCENT=20
CARDRAG_MAXIMUM_USED_PERCENT=85
CARDRAG_WARNING_USED_PERCENT=70
CARDRAG_STORAGE_MIGRATION_ID=
CARDRAG_STORAGE_MIGRATION_RESUME=false
CARDRAG_ADMIN_IMAGE=$admin_image
CARDRAG_WORKER_IMAGE=$worker_image
CARDRAG_MCP_IMAGE=$mcp_image
EOF

validate_environment_file() {
    [ -f "$stack_env_file" ] && [ ! -L "$stack_env_file" ] ||
        die "Stack environment file is missing or unsafe"
    cmp -s "$expected_env" "$stack_env_file" ||
        die "Stack environment file differs from the selected setup"
    [ "$(stat -c '%a' "$stack_env_file")" = 640 ] ||
        die "Stack environment file must have mode 0640"
    [ "$(stat -c '%u:%g' "$stack_env_file")" = "0:$docker_gid" ] ||
        die "Stack environment file must be owned by root:docker"
    validate_environment_parent
}

environment_pending=$stack_env_file.cardrag-pending
if [ -e "$stack_env_file" ] || [ -L "$stack_env_file" ]; then
    validate_environment_file
    if [ -e "$environment_pending" ] || [ -L "$environment_pending" ]; then
        if [ "$check_only" = true ] || [ "$bootstrap_complete_mode" = true ]; then
            die "Stack environment file has an incomplete pending commit"
        fi
        if ! { [ -f "$environment_pending" ] && [ ! -L "$environment_pending" ] &&
            cmp -s "$expected_env" "$environment_pending"; }; then
            die "Stack environment pending commit is inconsistent"
        fi
        rm -f "$environment_pending"
        python3 - "$(dirname -- "$stack_env_file")" <<'PY'
import os
import sys
descriptor = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
    fi
elif [ "$check_only" = true ] || [ "$bootstrap_complete_mode" = true ]; then
    die "Stack environment file is missing"
else
    env_parent=$environment_parent
    if [ -e "$env_parent" ] || [ -L "$env_parent" ]; then
        validate_environment_parent
    else
        install -d -o root -g docker -m 0750 "$env_parent"
        validate_environment_parent
    fi
    python3 - "$expected_env" "$stack_env_file" "$environment_pending" \
        "$docker_gid" <<'PY'
import os
import stat
import sys

source_path, target_path, pending_path, group = sys.argv[1:5]
expected = open(source_path, "rb").read()
try:
    pending = open(pending_path, "rb").read()
except FileNotFoundError:
    pending = None
if pending != expected:
    if pending is not None:
        os.unlink(pending_path)
    descriptor = os.open(pending_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as target:
            target.write(expected)
            target.flush()
        os.fchmod(descriptor, 0o640)
        if os.geteuid() == 0:
            os.fchown(descriptor, 0, int(group))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
else:
    metadata = os.lstat(pending_path)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) not in {0o600, 0o640}
    ):
        raise SystemExit("Stack environment pending file has unsafe metadata")
    descriptor = os.open(pending_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fchmod(descriptor, 0o640)
        if os.geteuid() == 0:
            os.fchown(descriptor, 0, int(group))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
directory_path = os.path.dirname(target_path)
directory = os.open(directory_path, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory)
finally:
    os.close(directory)
os.link(pending_path, target_path)
directory = os.open(directory_path, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory)
finally:
    os.close(directory)
os.unlink(pending_path)
directory = os.open(directory_path, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory)
finally:
    os.close(directory)
PY
    validate_environment_file
fi
if [ "$check_only" != true ] && [ "$bootstrap_complete_mode" != true ]; then
    write_phase environment-ready
fi

validate_metadata() {
    python3 - "$deployment_root" "$release_manifest_snapshot" "$admin_image" \
        "$worker_image" "$mcp_image" <<'PY'
import hashlib
import json
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
expected_names = {
    "deployment-set.json",
    "image-digests.json",
    "release-manifest.json",
    "stack-redacted.yaml",
}
if not root.is_dir() or root.is_symlink() or {item.name for item in root.iterdir()} != expected_names:
    raise SystemExit("deployment metadata directory has an unexpected inventory")
for path in root.iterdir():
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o444:
        raise SystemExit("deployment metadata has an unsafe type or mode")
commit = json.loads((root / "deployment-set.json").read_text(encoding="utf-8"))
if commit.get("schema_version") != "cardrag-deployment-set.v1":
    raise SystemExit("deployment metadata commit record is invalid")
committed_files = commit.get("files")
if not isinstance(committed_files, dict) or set(committed_files) != expected_names - {"deployment-set.json"}:
    raise SystemExit("deployment metadata commit inventory is invalid")
for name, digest in committed_files.items():
    if hashlib.sha256((root / name).read_bytes()).hexdigest() != digest:
        raise SystemExit("deployment metadata checksum mismatch")
if (root / "release-manifest.json").read_bytes() != Path(sys.argv[2]).read_bytes():
    raise SystemExit("installed release manifest differs from the selected release")
images = json.loads((root / "image-digests.json").read_text(encoding="utf-8"))
if images.get("images") != dict(zip(("admin", "worker", "mcp"), sys.argv[3:6])):
    raise SystemExit("installed image evidence differs from the selected release")
stack = (root / "stack-redacted.yaml").read_text(encoding="utf-8")
if any(image not in stack for image in sys.argv[3:6]):
    raise SystemExit("installed Stack metadata lacks a selected release image")
PY
    for metadata_file in "$deployment_root"/*; do
        [ -f "$metadata_file" ] && [ ! -L "$metadata_file" ] ||
            die "deployment metadata inventory is unsafe"
        [ "$(stat -c '%a' "$metadata_file")" = 444 ] &&
            [ "$(stat -c '%u:%g' "$metadata_file")" = 0:0 ] ||
            die "deployment metadata has unsafe mode or ownership"
    done
}

if phase_present metadata-ready; then
    validate_metadata
elif [ "$check_only" = true ] || [ "$bootstrap_complete_mode" = true ]; then
    die "deployment metadata installation is incomplete"
else
    metadata_stage=$work_root/deployment
    env \
        CARDRAG_DEPLOYMENT_ROOT="$metadata_stage" \
        CARDRAG_ADMIN_IMAGE="$admin_image" \
        CARDRAG_WORKER_IMAGE="$worker_image" \
        CARDRAG_MCP_IMAGE="$mcp_image" \
        "$script_root/deploy/portainer/install-deployment-metadata.sh" \
        "$script_root/deploy/portainer/cardrag-stack.yaml" "$release_manifest_snapshot"
    install -d -o root -g root -m 0755 "$deployment_root"
    python3 - "$metadata_stage" "$deployment_root" <<'PY'
import os
import stat
import sys
from pathlib import Path

source_root = Path(sys.argv[1])
target_root = Path(sys.argv[2])
names = ("stack-redacted.yaml", "image-digests.json", "release-manifest.json", "deployment-set.json")
pending_names = {f".{name}.cardrag-pending" for name in names}
unexpected = {item.name for item in target_root.iterdir()} - set(names) - pending_names
if unexpected:
    raise SystemExit("deployment metadata target contains unexpected files")


def fsync_root():
    directory = os.open(target_root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


for name in names:
    source = source_root / name
    target = target_root / name
    pending = target_root / f".{name}.cardrag-pending"
    expected = source.read_bytes()
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_file() or target.read_bytes() != expected:
            raise SystemExit(f"refusing to overwrite different deployment metadata: {name}")
        if pending.exists() or pending.is_symlink():
            if pending.is_symlink() or not pending.is_file() or pending.read_bytes() != expected:
                raise SystemExit(f"deployment pending commit is inconsistent: {name}")
            pending.unlink()
            fsync_root()
        continue
    try:
        pending_value = pending.read_bytes()
    except FileNotFoundError:
        pending_value = None
    if pending_value != expected:
        if pending_value is not None:
            if pending.is_symlink() or not pending.is_file():
                raise SystemExit(f"deployment pending path is unsafe: {name}")
            pending.unlink()
            fsync_root()
        descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(expected)
                stream.flush()
            os.fchmod(descriptor, 0o444)
            if os.geteuid() == 0:
                os.fchown(descriptor, 0, 0)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        fsync_root()
    else:
        metadata = pending.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) not in {0o400, 0o444}
        ):
            raise SystemExit(f"deployment pending metadata is unsafe: {name}")
        descriptor = os.open(pending, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fchmod(descriptor, 0o444)
            if os.geteuid() == 0:
                os.fchown(descriptor, 0, 0)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    os.link(pending, target)
    fsync_root()
    pending.unlink()
    fsync_root()
PY
    validate_metadata
    write_phase metadata-ready
fi

if [ "$bootstrap_complete_mode" = true ]; then
    for completed_phase in storage-ready secrets-ready environment-ready metadata-ready complete; do
        phase_present "$completed_phase" ||
            die "quick setup is not complete: missing $completed_phase"
    done
    if ! phase_present bootstrap-complete; then
        write_phase bootstrap-completing
        python3 - "$secret_root/keycloak_admin_password.txt" "$secret_root" <<'PY'
import os
import stat
import sys

path, root = sys.argv[1:3]
try:
    metadata = os.lstat(path)
except FileNotFoundError:
    pass
else:
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o444:
        raise SystemExit("Keycloak bootstrap secret is unsafe and was not removed")
    os.unlink(path)
directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory)
finally:
    os.close(directory)
PY
        write_phase bootstrap-complete
    fi
    validate_guard_inventory
    echo "Keycloak bootstrap account retirement confirmation and local credential removal are recorded"
    echo "The permanent Keycloak administrator remains an external operator responsibility"
    print_next_steps main
    exit 0
fi

if [ "$check_only" = true ]; then
    for completed_phase in storage-ready secrets-ready environment-ready metadata-ready complete; do
        phase_present "$completed_phase" ||
            die "quick setup is not complete: missing $completed_phase"
    done
    validate_guard_inventory
    echo "CardRAG quick setup check passed; prepared data was not changed"
    if phase_present bootstrap-complete; then
        print_next_steps main
    else
        print_next_steps bootstrap
    fi
    exit 0
fi

write_phase complete
validate_guard_inventory
echo "CardRAG Portainer host preparation completed"
echo "non-secret Portainer environment: $stack_env_file"
echo "durable runtime root: $data_root"
if phase_present bootstrap-complete; then
    print_next_steps main
else
    print_next_steps bootstrap
fi
