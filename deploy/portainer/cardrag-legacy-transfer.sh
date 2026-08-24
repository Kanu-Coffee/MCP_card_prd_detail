#!/bin/sh
set -eu
set -f

umask 027

repository_root=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
temporary_root=$(mktemp -d)
trap 'rm -rf "$temporary_root"' EXIT HUP INT TERM

runtime_uid=${CARDRAG_RUNTIME_UID:-10001}
runtime_gid=${CARDRAG_RUNTIME_GID:-10001}
minimum_free_gib=${CARDRAG_MINIMUM_FREE_GIB:-50}
minimum_free_percent=${CARDRAG_MINIMUM_FREE_PERCENT:-20}
maximum_used_percent=${CARDRAG_MAXIMUM_USED_PERCENT:-85}
warning_used_percent=${CARDRAG_WARNING_USED_PERCENT:-70}

usage() {
    cat <<'EOF'
Usage:
  cardrag-legacy-transfer.sh prepare --source DIR --manifest FILE --output DIR [--dry-run]
  cardrag-legacy-transfer.sh install --bundle DIR [--import-root DIR] [--dry-run]
  cardrag-legacy-transfer.sh prepare-install --source DIR --manifest FILE --output DIR [--import-root DIR] [--dry-run]
  cardrag-legacy-transfer.sh verify --bundle DIR
  cardrag-legacy-transfer.sh transfer-status --bundle BUNDLE_ID [--import-root DIR]
  cardrag-legacy-transfer.sh portainer-env import --bundle BUNDLE_ID_OR_DIR
  cardrag-legacy-transfer.sh portainer-env resume --bundle BUNDLE_ID_OR_DIR --import-id UUID
  cardrag-legacy-transfer.sh portainer-env status --import-id UUID
  cardrag-legacy-transfer.sh portainer-env finalize --import-id UUID
  cardrag-legacy-transfer.sh portainer-env disable

Defaults:
  CARDRAG_IMPORT_ROOT=/srv/cardrag/imports
  CARDRAG_RUNTIME_UID=10001
  CARDRAG_RUNTIME_GID=10001

Host requirements:
  GNU findutils (the safety fingerprint uses find -printf)
  sha256sum, sort, awk, grep, stat, df, du, and flock (install only)

The source archive is only read. A prepared bundle is fully verified, copied
under IMPORT_ROOT/.incoming, verified again, and atomically published as
IMPORT_ROOT/bundle-<digest>. No secret value is printed by this tool.
EOF
}

fail() {
    status=$1
    shift
    printf '%s\n' "$*" >&2
    exit "$status"
}

require_gnu_find() {
    command -v find >/dev/null 2>&1 || fail 69 "GNU findutils is required"
    # GNU find exposes its implementation through this option without a path.
    # shellcheck disable=SC2185
    find --version 2>/dev/null | grep -q 'GNU findutils' || \
        fail 69 "GNU findutils with find -printf support is required"
}

require_value() {
    option=$1
    value=${2:-}
    [ -n "$value" ] || fail 64 "$option requires a value"
}

absolute_path() {
    raw=$1
    [ -n "$raw" ] || fail 64 "path must not be empty"
    case "$raw" in
        *'
'*) fail 78 "path must not contain a newline" ;;
    esac
    case "/$raw/" in
        */../*|*/./*) fail 78 "path traversal components are forbidden: $raw" ;;
    esac
    case "$raw" in
        /*) candidate=$raw ;;
        *) candidate=$PWD/$raw ;;
    esac
    while [ "$candidate" != / ] && [ "${candidate%/}" != "$candidate" ]; do
        candidate=${candidate%/}
    done
    printf '%s\n' "$candidate"
}

check_path_components() {
    candidate=$1
    case "$candidate" in
        /*) ;;
        *) fail 78 "path must be absolute after normalization: $candidate" ;;
    esac
    cursor=/
    old_ifs=$IFS
    IFS=/
    # Intentional splitting walks each component; pathname expansion is disabled.
    # shellcheck disable=SC2086
    set -- $candidate
    IFS=$old_ifs
    for component do
        [ -n "$component" ] || continue
        if [ "$cursor" = / ]; then
            cursor=/$component
        else
            cursor=$cursor/$component
        fi
        if [ -L "$cursor" ]; then
            fail 78 "path must not traverse a symlink: $cursor"
        fi
    done
}

require_safe_directory() {
    path=$1
    label=$2
    check_path_components "$path"
    [ -d "$path" ] && [ ! -L "$path" ] || fail 78 "$label must be an existing non-symlink directory: $path"
}

audit_tree() {
    root=$1
    label=$2
    unsafe=$(find "$root" -mindepth 1 \( -type l -o \( ! -type d -a ! -type f \) \) -print -quit)
    [ -z "$unsafe" ] || fail 78 "$label contains a symlink or special file: $unsafe"
}

metadata_fingerprint() {
    root=$1
    find "$root" -mindepth 1 \
        -printf '%P\034%y\034%m\034%u\034%g\034%s\034%T@\n' \
        | LC_ALL=C sort | sha256sum | awk '{print $1}'
}

directory_is_empty_or_only_file() {
    directory=$1
    allowed_file=$2
    first=$(find "$directory" -mindepth 1 -print -quit)
    [ -z "$first" ] && return 0
    [ "$first" = "$allowed_file" ] && [ -f "$allowed_file" ] && [ ! -L "$allowed_file" ] || return 1
    [ "$(find "$directory" -mindepth 1 -print | wc -l | tr -d ' ')" -eq 1 ]
}

owner_marker_body() {
    schema=$1
    bound_path=$2
    binding=$(printf '%s\n' "$bound_path" | sha256sum | awk '{print $1}')
    printf '%s:%s\n' "$schema" "$binding"
}

create_or_verify_owner_marker() {
    marker=$1
    expected=$2
    mode=$3
    if [ -e "$marker" ] || [ -L "$marker" ]; then
        [ -f "$marker" ] && [ ! -L "$marker" ] || fail 78 "owner marker is not a safe regular file"
        [ "$(cat "$marker")" = "$expected" ] || fail 78 "owner marker does not match this exact operation"
        chmod "$mode" "$marker"
        return
    fi

    set +e
    (set -C; printf '%s\n' "$expected" >"$marker") 2>/dev/null
    marker_status=$?
    set -e
    if [ "$marker_status" -ne 0 ]; then
        [ -f "$marker" ] && [ ! -L "$marker" ] || fail 75 "owner marker creation raced with an unsafe node"
        [ "$(cat "$marker")" = "$expected" ] || fail 75 "another operation owns this destination"
    fi
    chmod "$mode" "$marker"
}

python_executable() {
    if command -v python3 >/dev/null 2>&1; then
        command -v python3
    elif [ -x "$repository_root/.venv/bin/python" ]; then
        printf '%s\n' "$repository_root/.venv/bin/python"
    else
        fail 69 "python3 is required to inspect CardRAG JSON output"
    fi
}

run_cardrag() {
    if [ -n "${CARDRAG_LEGACY_CLI:-}" ]; then
        if ! command -v "$CARDRAG_LEGACY_CLI" >/dev/null 2>&1 && [ ! -x "$CARDRAG_LEGACY_CLI" ]; then
            fail 69 "CARDRAG_LEGACY_CLI is not executable: $CARDRAG_LEGACY_CLI"
        fi
        "$CARDRAG_LEGACY_CLI" "$@"
    elif command -v cardrag >/dev/null 2>&1; then
        cardrag "$@"
    elif [ -x "$repository_root/.venv/bin/cardrag" ]; then
        "$repository_root/.venv/bin/cardrag" "$@"
    elif command -v uv >/dev/null 2>&1; then
        (cd "$repository_root" && uv run --frozen cardrag "$@")
    else
        fail 69 "cardrag CLI is required; set CARDRAG_LEGACY_CLI to its executable path"
    fi
}

json_field() {
    json_path=$1
    dotted_field=$2
    python=$(python_executable)
    "$python" - "$json_path" "$dotted_field" <<'PY'
import json
import sys

value = json.loads(open(sys.argv[1], encoding="utf-8").read())
for component in sys.argv[2].split("."):
    value = value[component]
if value is None:
    print("")
elif isinstance(value, bool):
    print(str(value).lower())
else:
    print(value)
PY
}

validate_bundle_id() {
    bundle_id=$1
    case "$bundle_id" in
        bundle-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]) ;;
        *) fail 78 "invalid legacy bundle ID: $bundle_id" ;;
    esac
}

validate_uuid() {
    identifier=$1
    if ! printf '%s\n' "$identifier" | grep -Eq '^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[1-8][0-9A-Fa-f]{3}-[89ABab][0-9A-Fa-f]{3}-[0-9A-Fa-f]{12}$'; then
        fail 64 "import ID must be a UUID: $identifier"
    fi
}

verify_bundle_to() {
    bundle=$1
    output=$2
    require_safe_directory "$bundle" "bundle"
    audit_tree "$bundle" "bundle"
    run_cardrag legacy verify --bundle "$bundle" >"$output"
    bundle_id=$(json_field "$output" bundle_id)
    validate_bundle_id "$bundle_id"
    [ "${bundle##*/}" = "$bundle_id" ] || fail 78 "bundle directory name must equal its verified bundle ID"
}

prepare_internal() {
    source=$1
    manifest=$2
    output=$3
    dry_run=$4
    result=$5

    source=$(absolute_path "$source")
    manifest=$(absolute_path "$manifest")
    output=$(absolute_path "$output")
    require_safe_directory "$source" "legacy source"
    check_path_components "$manifest"
    [ -f "$manifest" ] && [ ! -L "$manifest" ] || fail 78 "manifest must be a regular non-symlink file: $manifest"
    case "$manifest" in
        "$source"/*) ;;
        *) fail 78 "manifest must be located inside the read-only legacy source" ;;
    esac
    case "$output" in
        /|"$source"|"$source"/*) fail 78 "bundle output must be outside the legacy source and must not be broad" ;;
    esac
    check_path_components "$output"
    audit_tree "$source" "legacy source"

    before=$(metadata_fingerprint "$source")
    if [ "$dry_run" = true ]; then
        [ -d "${output%/*}" ] || fail 78 "dry-run output parent must already exist: ${output%/*}"
        run_cardrag legacy prepare --source "$source" --manifest "$manifest" \
            --output "$output" --dry-run >"$result"
    else
        output_parent=${output%/*}
        [ -d "$output_parent" ] && [ ! -L "$output_parent" ] || \
            fail 78 "bundle output parent must be an existing non-symlink directory"
        output_marker=$output/.cardrag-legacy-prepare-root
        output_owner_body=$(owner_marker_body cardrag-legacy-prepare-owner.v1 "$output")
        output_owner_hash=${output_owner_body##*:}
        output_owner_prefix=$(printf '%.12s' "$output_owner_hash")
        output_owner_marker=$output_parent/.cardrag-legacy-prepare-owner-$output_owner_prefix
        if [ -e "$output" ] || [ -L "$output" ]; then
            [ -d "$output" ] && [ ! -L "$output" ] || fail 78 "bundle output is not a safe directory: $output"
            if [ -f "$output_marker" ] && [ ! -L "$output_marker" ] && \
               [ "$(cat "$output_marker")" = cardrag-legacy-prepare-root.v1 ]; then
                # Upgrade an already-owned root to the external crash marker.
                create_or_verify_owner_marker "$output_owner_marker" "$output_owner_body" 0400
            elif [ -f "$output_owner_marker" ] && [ ! -L "$output_owner_marker" ] && \
                 [ "$(cat "$output_owner_marker")" = "$output_owner_body" ] && \
                 directory_is_empty_or_only_file "$output" "$output_marker"; then
                # mkdir completed before the internal marker; the external
                # marker proves this exact empty root belongs to our operation.
                printf '%s\n' cardrag-legacy-prepare-root.v1 >"$output_marker"
                chmod 0440 "$output_marker"
            else
                fail 78 "existing bundle output is not an exact owned resumable root: $output"
            fi
        else
            # Commit ownership outside the directory before mkdir. A crash in
            # the following window is therefore safely resumable.
            create_or_verify_owner_marker "$output_owner_marker" "$output_owner_body" 0400
            mkdir -m 0750 "$output"
            printf '%s\n' cardrag-legacy-prepare-root.v1 >"$output_marker"
            chmod 0440 "$output_marker"
        fi
        run_cardrag legacy prepare --source "$source" --manifest "$manifest" \
            --output "$output" >"$result"
    fi
    after=$(metadata_fingerprint "$source")
    [ "$before" = "$after" ] || fail 74 "legacy source metadata changed while it was being read; no bundle was accepted"
}

validate_thresholds() {
    for value in "$runtime_uid" "$runtime_gid" "$minimum_free_gib" \
                 "$minimum_free_percent" "$maximum_used_percent" "$warning_used_percent"; do
        case "$value" in
            ''|*[!0-9]*) fail 64 "UID, GID, and storage thresholds must be non-negative integers" ;;
        esac
    done
    if [ "$minimum_free_percent" -gt 100 ] || [ "$maximum_used_percent" -gt 100 ] || \
       [ "$warning_used_percent" -gt 100 ]; then
        fail 64 "storage percentages must not exceed 100"
    fi
    [ "$warning_used_percent" -lt "$maximum_used_percent" ] || \
        fail 64 "storage warning threshold must precede the blocking threshold"
}

capacity_check() {
    bundle=$1
    import_root=$2
    validate_thresholds
    required_kib=$(du -sk "$bundle" | awk '{print $1}')
    required_inodes=$(find "$bundle" -printf x | wc -c | tr -d ' ')
    block_row=$(df -Pk "$import_root" | awk 'NR == 2 {print $2, $3, $4}')
    inode_row=$(df -Pi "$import_root" | awk 'NR == 2 {print $2, $3, $4}')
    # Intentional splitting extracts the machine-formatted df counters.
    # shellcheck disable=SC2086
    set -- $block_row
    block_total=${1:-}
    block_used=${2:-}
    block_available=${3:-}
    # shellcheck disable=SC2086
    set -- $inode_row
    inode_total=${1:-}
    inode_used=${2:-}
    inode_available=${3:-}
    for value in "$required_kib" "$required_inodes" "$block_total" "$block_used" \
                 "$block_available" "$inode_total" "$inode_used" "$inode_available"; do
        case "$value" in
            ''|*[!0-9]*) fail 70 "filesystem capacity counters are invalid" ;;
        esac
    done
    [ "$block_total" -gt 0 ] && [ "$inode_total" -gt 0 ] || fail 70 "filesystem capacity cannot be measured"
    [ "$required_kib" -le "$block_available" ] || fail 75 "legacy transfer needs more free bytes than are available"
    [ "$required_inodes" -le "$inode_available" ] || fail 75 "legacy transfer needs more free inodes than are available"

    projected_available=$((block_available - required_kib))
    projected_inode_available=$((inode_available - required_inodes))
    projected_used_percent=$(((block_used + required_kib) * 100 / block_total))
    projected_inode_used_percent=$(((inode_used + required_inodes) * 100 / inode_total))
    projected_free_percent=$((projected_available * 100 / block_total))
    projected_inode_free_percent=$((projected_inode_available * 100 / inode_total))
    minimum_free_kib=$((minimum_free_gib * 1024 * 1024))
    if [ "$projected_available" -lt "$minimum_free_kib" ] || \
       [ "$projected_free_percent" -lt "$minimum_free_percent" ] || \
       [ "$projected_inode_free_percent" -lt "$minimum_free_percent" ] || \
       [ "$projected_used_percent" -ge "$maximum_used_percent" ] || \
       [ "$projected_inode_used_percent" -ge "$maximum_used_percent" ]; then
        fail 75 "legacy transfer rejected by projected disk/inode admission thresholds"
    fi
    if [ "$projected_used_percent" -ge "$warning_used_percent" ] || \
       [ "$projected_inode_used_percent" -ge "$warning_used_percent" ]; then
        printf 'warning=projected-storage-usage-high block_used_percent=%s inode_used_percent=%s\n' \
            "$projected_used_percent" "$projected_inode_used_percent" >&2
    fi
    printf 'admission=passed required_kib=%s required_inodes=%s projected_free_percent=%s projected_inode_free_percent=%s\n' \
        "$required_kib" "$required_inodes" "$projected_free_percent" "$projected_inode_free_percent"
}

require_install_identity() {
    current_uid=$(id -u)
    current_gid=$(id -g)
    if [ "$current_uid" -ne 0 ] && { [ "$current_uid" -ne "$runtime_uid" ] || [ "$current_gid" -ne "$runtime_gid" ]; }; then
        fail 77 "install must run as root (or as CARDRAG_RUNTIME_UID:CARDRAG_RUNTIME_GID)"
    fi
}

remove_owned_stage() {
    stage=$1
    owner=$2
    expected=$3
    [ -f "$owner" ] && [ ! -L "$owner" ] || fail 78 "partial staging has no trusted owner marker: $stage"
    [ "$(cat "$owner")" = "$expected" ] || fail 78 "partial staging owner does not match this bundle: $stage"
    case "$stage" in
        "$incoming_root"/bundle-[0-9a-f]*) ;;
        *) fail 78 "refusing to remove staging outside the owned incoming root" ;;
    esac
    chmod -R u+w "$stage"
    rm -rf --one-file-system -- "$stage"
    rm -f -- "$owner"
}

install_internal() {
    bundle=$1
    import_root=$2
    dry_run=$3

    bundle=$(absolute_path "$bundle")
    import_root=$(absolute_path "$import_root")
    case "$import_root" in
        /|/srv|/mnt|/var|/var/lib) fail 78 "import root is too broad: $import_root" ;;
    esac
    require_safe_directory "$bundle" "bundle"
    require_safe_directory "$import_root" "Portainer import root"
    bundle=$(CDPATH='' cd -- "$bundle" && pwd -P)
    import_root=$(CDPATH='' cd -- "$import_root" && pwd -P)
    case "$bundle/" in
        "$import_root/"*) fail 78 "bundle and import root must be disjoint; bundle is inside import root" ;;
    esac
    case "$import_root/" in
        "$bundle/"*) fail 78 "bundle and import root must be disjoint; import root is inside bundle" ;;
    esac
    verify_json=$temporary_root/verify-source.json
    verify_bundle_to "$bundle" "$verify_json"
    bundle_id=$(json_field "$verify_json" bundle_id)
    content_sha256=$(json_field "$verify_json" content_sha256)
    final=$import_root/$bundle_id

    if [ -e "$final" ] || [ -L "$final" ]; then
        [ -d "$final" ] && [ ! -L "$final" ] || fail 78 "existing bundle target is not a safe directory: $final"
        existing_json=$temporary_root/verify-existing.json
        verify_bundle_to "$final" "$existing_json"
        existing_sha256=$(json_field "$existing_json" content_sha256)
        [ "$existing_sha256" = "$content_sha256" ] || fail 73 "existing bundle ID has different content; refusing overwrite"
        printf 'transfer_status=already-installed bundle_id=%s target=%s\n' "$bundle_id" "$final"
        return
    fi

    capacity_check "$bundle" "$import_root"
    if [ "$dry_run" = true ]; then
        printf 'transfer_status=dry-run bundle_id=%s target=%s\n' "$bundle_id" "$final"
        return
    fi

    require_install_identity
    command -v flock >/dev/null 2>&1 || fail 69 "flock is required for serialized legacy installation"
    lock_path=$import_root/.cardrag-legacy-transfer.lock
    if [ -e "$lock_path" ] || [ -L "$lock_path" ]; then
        [ -f "$lock_path" ] && [ ! -L "$lock_path" ] || fail 78 "legacy transfer lock path is unsafe"
    else
        : >"$lock_path"
        chmod 0600 "$lock_path"
    fi
    exec 9>"$lock_path"
    flock -n 9 || fail 75 "another legacy transfer is already using this import root"

    # Recheck under the lock so a concurrent invocation cannot replace a final.
    if [ -e "$final" ] || [ -L "$final" ]; then
        [ -d "$final" ] && [ ! -L "$final" ] || fail 78 "bundle target appeared as an unsafe node"
        existing_json=$temporary_root/verify-existing-locked.json
        verify_bundle_to "$final" "$existing_json"
        [ "$(json_field "$existing_json" content_sha256)" = "$content_sha256" ] || \
            fail 73 "bundle target appeared with different content; refusing overwrite"
        printf 'transfer_status=already-installed bundle_id=%s target=%s\n' "$bundle_id" "$final"
        return
    fi

    incoming_root=$import_root/.incoming
    incoming_sentinel=$incoming_root/.cardrag-legacy-transfer-v1
    incoming_owner=$import_root/.cardrag-legacy-incoming-owner
    incoming_owner_body=$(owner_marker_body cardrag-legacy-incoming-owner.v1 "$import_root")
    if [ -e "$incoming_root" ] || [ -L "$incoming_root" ]; then
        [ -d "$incoming_root" ] && [ ! -L "$incoming_root" ] || fail 78 "incoming root is unsafe"
        if [ -f "$incoming_sentinel" ] && [ ! -L "$incoming_sentinel" ] && \
           [ "$(cat "$incoming_sentinel")" = cardrag-legacy-transfer-v1 ]; then
            # Upgrade an already-owned root to the external crash marker.
            create_or_verify_owner_marker "$incoming_owner" "$incoming_owner_body" 0400
        elif [ -f "$incoming_owner" ] && [ ! -L "$incoming_owner" ] && \
             [ "$(cat "$incoming_owner")" = "$incoming_owner_body" ] && \
             directory_is_empty_or_only_file "$incoming_root" "$incoming_sentinel"; then
            # mkdir (or the sentinel create) completed before the internal
            # commit. External ownership makes this exact recovery safe.
            printf '%s\n' cardrag-legacy-transfer-v1 >"$incoming_sentinel"
            chmod 0400 "$incoming_sentinel"
        else
            fail 78 "incoming root has no exact resumable ownership proof"
        fi
    else
        create_or_verify_owner_marker "$incoming_owner" "$incoming_owner_body" 0400
        mkdir -m 0700 "$incoming_root"
        printf '%s\n' cardrag-legacy-transfer-v1 >"$incoming_sentinel"
        chmod 0400 "$incoming_sentinel"
    fi

    stage=$incoming_root/$bundle_id
    stage_owner=$incoming_root/$bundle_id.owner
    owner_body="cardrag-legacy-transfer-stage.v1:$bundle_id:$content_sha256"
    if [ -e "$stage" ] || [ -L "$stage" ]; then
        [ -d "$stage" ] && [ ! -L "$stage" ] || fail 78 "partial staging is not a safe directory: $stage"
        remove_owned_stage "$stage" "$stage_owner" "$owner_body"
    elif [ -e "$stage_owner" ] || [ -L "$stage_owner" ]; then
        [ -f "$stage_owner" ] && [ ! -L "$stage_owner" ] || fail 78 "partial staging owner marker is unsafe"
        [ "$(cat "$stage_owner")" = "$owner_body" ] || fail 78 "partial staging owner does not match this bundle"
    fi
    # Commit the exact bundle/content ownership before mkdir so the empty-dir
    # crash window can be retried without adopting any foreign directory.
    create_or_verify_owner_marker "$stage_owner" "$owner_body" 0400
    mkdir -m 0700 "$stage"

    # The verified top-level contract is fixed. READY is deliberately copied last.
    for name in bundle-manifest.json checksums.sha256 manifests objects records reports; do
        cp -pR "$bundle/$name" "$stage/$name"
    done
    cp -p "$bundle/READY" "$stage/READY"
    if [ "$(id -u)" -eq 0 ]; then
        chown -R "$runtime_uid:$runtime_gid" "$stage"
    fi
    find "$stage" -mindepth 1 -type d -exec chmod 0550 {} +
    find "$stage" -type f -exec chmod 0440 {} +
    # A non-root runtime owner needs write permission on the moved directory
    # inode itself for rename(2); seal the root immediately after publication.
    chmod 0750 "$stage"
    audit_tree "$stage" "copied bundle staging"
    stage_json=$temporary_root/verify-stage.json
    verify_bundle_to "$stage" "$stage_json"
    [ "$(json_field "$stage_json" content_sha256)" = "$content_sha256" ] || fail 74 "copied bundle identity differs from its source"
    if command -v sync >/dev/null 2>&1; then
        sync -f "$stage"
    fi

    # The exclusive lock makes this same-filesystem rename an atomic publish.
    [ ! -e "$final" ] && [ ! -L "$final" ] || fail 73 "bundle target appeared before atomic publish"
    if ! mv "$stage" "$final"; then
        printf 'atomic publish failed; directory ownership/modes follow:\n' >&2
        ls -ld "$import_root" "$incoming_root" "$stage" >&2
        fail 74 "atomic legacy bundle publish failed"
    fi
    chmod 0550 "$final"
    rm -f -- "$stage_owner"
    if command -v sync >/dev/null 2>&1; then
        sync -f "$import_root"
    fi
    final_json=$temporary_root/verify-final.json
    verify_bundle_to "$final" "$final_json"
    [ "$(json_field "$final_json" content_sha256)" = "$content_sha256" ] || fail 74 "published bundle identity differs from its source"
    printf 'transfer_status=installed bundle_id=%s target=%s\n' "$bundle_id" "$final"
}

resolve_bundle_id() {
    candidate=$1
    if [ -d "$candidate" ] && [ ! -L "$candidate" ]; then
        candidate=$(absolute_path "$candidate")
        environment_verify=$temporary_root/environment-verify.json
        verify_bundle_to "$candidate" "$environment_verify"
        json_field "$environment_verify" bundle_id
    else
        validate_bundle_id "$candidate"
        printf '%s\n' "$candidate"
    fi
}

print_portainer_environment() {
    operation=$1
    bundle_candidate=${2:-}
    import_id=${3:-}
    case "$operation" in
        import)
            bundle_id=$(resolve_bundle_id "$bundle_candidate")
            cat <<EOF
# Apply for one Stack redeployment only. Afterwards run portainer-env disable.
COMPOSE_PROFILES=legacy-import
CARDRAG_LEGACY_IMPORT_ENABLED=true
CARDRAG_LEGACY_OPERATION=import
CARDRAG_LEGACY_IMPORT_ID=
CARDRAG_LEGACY_BUNDLE_NAME=$bundle_id
CARDRAG_ADMIN_OPERATION_ENABLED=false
CARDRAG_ADMIN_OPERATION=
CARDRAG_ADMIN_OPERATION_ID=
EOF
            ;;
        resume)
            bundle_id=$(resolve_bundle_id "$bundle_candidate")
            validate_uuid "$import_id"
            cat <<EOF
# Apply for one Stack redeployment only. Afterwards run portainer-env disable.
COMPOSE_PROFILES=legacy-import
CARDRAG_LEGACY_IMPORT_ENABLED=true
CARDRAG_LEGACY_OPERATION=resume
CARDRAG_LEGACY_IMPORT_ID=$import_id
CARDRAG_LEGACY_BUNDLE_NAME=$bundle_id
CARDRAG_ADMIN_OPERATION_ENABLED=false
CARDRAG_ADMIN_OPERATION=
CARDRAG_ADMIN_OPERATION_ID=
EOF
            ;;
        status)
            validate_uuid "$import_id"
            cat <<EOF
# Apply for one Stack redeployment only. Afterwards run portainer-env disable.
COMPOSE_PROFILES=ops
CARDRAG_LEGACY_IMPORT_ENABLED=false
CARDRAG_LEGACY_OPERATION=import
CARDRAG_LEGACY_IMPORT_ID=
CARDRAG_LEGACY_BUNDLE_NAME=READY-NOT-SET
CARDRAG_ADMIN_OPERATION_ENABLED=true
CARDRAG_ADMIN_OPERATION=legacy-status
CARDRAG_ADMIN_OPERATION_ID=$import_id
EOF
            ;;
        finalize)
            validate_uuid "$import_id"
            cat <<EOF
# Finalize publishes the verified candidate. Apply once, then run portainer-env disable.
COMPOSE_PROFILES=ops
CARDRAG_LEGACY_IMPORT_ENABLED=false
CARDRAG_LEGACY_OPERATION=import
CARDRAG_LEGACY_IMPORT_ID=
CARDRAG_LEGACY_BUNDLE_NAME=READY-NOT-SET
CARDRAG_ADMIN_OPERATION_ENABLED=true
CARDRAG_ADMIN_OPERATION=legacy-finalize
CARDRAG_ADMIN_OPERATION_ID=$import_id
EOF
            ;;
        disable)
            cat <<'EOF'
COMPOSE_PROFILES=
CARDRAG_LEGACY_IMPORT_ENABLED=false
CARDRAG_LEGACY_OPERATION=import
CARDRAG_LEGACY_IMPORT_ID=
CARDRAG_LEGACY_BUNDLE_NAME=READY-NOT-SET
CARDRAG_ADMIN_OPERATION_ENABLED=false
CARDRAG_ADMIN_OPERATION=
CARDRAG_ADMIN_OPERATION_ID=
EOF
            ;;
        *) fail 64 "Portainer environment operation must be import, resume, status, finalize, or disable" ;;
    esac
}

command_name=${1:-}
[ -n "$command_name" ] || { usage >&2; exit 64; }
shift

case "$command_name" in
    -h|--help|help) ;;
    *) require_gnu_find ;;
esac

case "$command_name" in
    prepare)
        source=
        manifest=
        output=
        dry_run=false
        while [ "$#" -gt 0 ]; do
            case "$1" in
                --source) require_value "$1" "${2:-}"; source=$2; shift 2 ;;
                --manifest) require_value "$1" "${2:-}"; manifest=$2; shift 2 ;;
                --output) require_value "$1" "${2:-}"; output=$2; shift 2 ;;
                --dry-run) dry_run=true; shift ;;
                *) fail 64 "unknown prepare option: $1" ;;
            esac
        done
        [ -n "$source" ] && [ -n "$manifest" ] && [ -n "$output" ] || { usage >&2; exit 64; }
        result=$temporary_root/prepare.json
        prepare_internal "$source" "$manifest" "$output" "$dry_run" "$result"
        cat "$result"
        ;;
    install)
        bundle=
        import_root=${CARDRAG_IMPORT_ROOT:-/srv/cardrag/imports}
        dry_run=false
        while [ "$#" -gt 0 ]; do
            case "$1" in
                --bundle) require_value "$1" "${2:-}"; bundle=$2; shift 2 ;;
                --import-root) require_value "$1" "${2:-}"; import_root=$2; shift 2 ;;
                --dry-run) dry_run=true; shift ;;
                *) fail 64 "unknown install option: $1" ;;
            esac
        done
        [ -n "$bundle" ] || { usage >&2; exit 64; }
        install_internal "$bundle" "$import_root" "$dry_run"
        if [ "$dry_run" = true ]; then
            printf '%s\n' 'next_step=none dry-run never enables a Portainer mutation profile'
            print_portainer_environment disable "" ""
        else
            bundle_id=$(resolve_bundle_id "$bundle")
            print_portainer_environment import "$bundle_id" ""
        fi
        ;;
    prepare-install)
        source=
        manifest=
        output=
        import_root=${CARDRAG_IMPORT_ROOT:-/srv/cardrag/imports}
        dry_run=false
        while [ "$#" -gt 0 ]; do
            case "$1" in
                --source) require_value "$1" "${2:-}"; source=$2; shift 2 ;;
                --manifest) require_value "$1" "${2:-}"; manifest=$2; shift 2 ;;
                --output) require_value "$1" "${2:-}"; output=$2; shift 2 ;;
                --import-root) require_value "$1" "${2:-}"; import_root=$2; shift 2 ;;
                --dry-run) dry_run=true; shift ;;
                *) fail 64 "unknown prepare-install option: $1" ;;
            esac
        done
        [ -n "$source" ] && [ -n "$manifest" ] && [ -n "$output" ] || { usage >&2; exit 64; }
        result=$temporary_root/prepare-install.json
        prepare_internal "$source" "$manifest" "$output" "$dry_run" "$result"
        cat "$result"
        bundle_id=$(json_field "$result" manifest.bundle_id)
        validate_bundle_id "$bundle_id"
        if [ "$dry_run" = true ]; then
            import_root=$(absolute_path "$import_root")
            require_safe_directory "$import_root" "Portainer import root"
            printf 'transfer_status=dry-run bundle_id=%s target=%s/%s\n' "$bundle_id" "$import_root" "$bundle_id"
            printf '%s\n' 'next_step=none dry-run never enables a Portainer mutation profile'
            print_portainer_environment disable "" ""
        else
            bundle_path=$(json_field "$result" bundle_path)
            [ -n "$bundle_path" ] || fail 70 "prepare did not return a bundle path"
            install_internal "$bundle_path" "$import_root" false
            print_portainer_environment import "$bundle_id" ""
        fi
        ;;
    verify)
        bundle=
        while [ "$#" -gt 0 ]; do
            case "$1" in
                --bundle) require_value "$1" "${2:-}"; bundle=$2; shift 2 ;;
                *) fail 64 "unknown verify option: $1" ;;
            esac
        done
        [ -n "$bundle" ] || { usage >&2; exit 64; }
        bundle=$(absolute_path "$bundle")
        verify_bundle_to "$bundle" "$temporary_root/verify.json"
        cat "$temporary_root/verify.json"
        ;;
    transfer-status)
        bundle=
        import_root=${CARDRAG_IMPORT_ROOT:-/srv/cardrag/imports}
        while [ "$#" -gt 0 ]; do
            case "$1" in
                --bundle) require_value "$1" "${2:-}"; bundle=$2; shift 2 ;;
                --import-root) require_value "$1" "${2:-}"; import_root=$2; shift 2 ;;
                *) fail 64 "unknown transfer-status option: $1" ;;
            esac
        done
        [ -n "$bundle" ] || { usage >&2; exit 64; }
        validate_bundle_id "$bundle"
        import_root=$(absolute_path "$import_root")
        require_safe_directory "$import_root" "Portainer import root"
        final=$import_root/$bundle
        stage=$import_root/.incoming/$bundle
        if [ -e "$final" ] || [ -L "$final" ]; then
            verify_bundle_to "$final" "$temporary_root/status.json"
            printf 'transfer_status=ready bundle_id=%s target=%s staging=%s\n' \
                "$bundle" "$final" "$([ -e "$stage" ] && printf present || printf absent)"
        elif [ -e "$stage" ] || [ -L "$stage" ]; then
            [ -d "$stage" ] && [ ! -L "$stage" ] || fail 78 "partial staging is unsafe: $stage"
            printf 'transfer_status=partial bundle_id=%s target=%s staging=present\n' "$bundle" "$final"
        else
            printf 'transfer_status=missing bundle_id=%s target=%s staging=absent\n' "$bundle" "$final"
            exit 66
        fi
        ;;
    portainer-env)
        operation=${1:-}
        [ -n "$operation" ] || { usage >&2; exit 64; }
        shift
        bundle=
        import_id=
        while [ "$#" -gt 0 ]; do
            case "$1" in
                --bundle) require_value "$1" "${2:-}"; bundle=$2; shift 2 ;;
                --import-id) require_value "$1" "${2:-}"; import_id=$2; shift 2 ;;
                *) fail 64 "unknown portainer-env option: $1" ;;
            esac
        done
        case "$operation" in
            import) [ -n "$bundle" ] || fail 64 "portainer-env import requires --bundle" ;;
            resume) [ -n "$bundle" ] && [ -n "$import_id" ] || fail 64 "portainer-env resume requires --bundle and --import-id" ;;
            status) [ -n "$import_id" ] || fail 64 "portainer-env status requires --import-id" ;;
            finalize) [ -n "$import_id" ] || fail 64 "portainer-env finalize requires --import-id" ;;
            disable) [ -z "$bundle$import_id" ] || fail 64 "portainer-env disable accepts no options" ;;
            *) fail 64 "unknown portainer-env operation: $operation" ;;
        esac
        print_portainer_environment "$operation" "$bundle" "$import_id"
        ;;
    -h|--help|help)
        usage
        ;;
    *)
        usage >&2
        fail 64 "unknown command: $command_name"
        ;;
esac
