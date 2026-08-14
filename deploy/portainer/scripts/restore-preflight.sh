#!/bin/sh
set -eu

runtime_root=${1:-}
archive_root=${2:-}
shift 2 2>/dev/null || {
    echo "usage: restore-preflight.sh RUNTIME_ROOT ARCHIVE_ROOT -- COMMAND [ARG...]" >&2
    exit 64
}
if [ "${1:-}" != -- ]; then
    echo "usage: restore-preflight.sh RUNTIME_ROOT ARCHIVE_ROOT -- COMMAND [ARG...]" >&2
    exit 64
fi
shift
if [ "$#" -eq 0 ]; then
    echo "restore preflight requires a command" >&2
    exit 64
fi

expected_uid=${CARDRAG_RUNTIME_UID:-10001}
expected_gid=${CARDRAG_RUNTIME_GID:-10001}
package_path=${CARDRAG_STATE_PACKAGE_PATH:-}
for expected in "$expected_uid" "$expected_gid"; do
    case "$expected" in
        ''|*[!0-9]*)
            echo "runtime UID/GID must be numeric" >&2
            exit 64
            ;;
    esac
done

case "$package_path" in
    "$archive_root"/cardrag-state-*) ;;
    *)
        echo "CARDRAG_STATE_PACKAGE_PATH must name a direct archive package" >&2
        exit 64
        ;;
esac
package_name=${package_path##*/}
if ! printf '%s\n' "$package_name" | \
    grep -Eq '^cardrag-state-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}$'; then
    echo "CARDRAG_STATE_PACKAGE_PATH has an invalid package identity" >&2
    exit 64
fi
export_id=${package_name##*-}

require_owned_directory() {
    path=$1
    label=$2
    expected_mode=$3
    if [ ! -d "$path" ] || [ -L "$path" ]; then
        echo "$label must be an existing non-symlink directory" >&2
        exit 78
    fi
    owner=$(stat -c '%u:%g' "$path")
    if [ "$owner" != "$expected_uid:$expected_gid" ]; then
        echo "$label must be owned by $expected_uid:$expected_gid (actual $owner)" >&2
        exit 77
    fi
    mode=$(stat -c '%a' "$path")
    if [ "$mode" != "$expected_mode" ]; then
        echo "$label must have mode $expected_mode (actual $mode)" >&2
        exit 77
    fi
}

validate_runtime_root() {
    phase=$1
    allow_recovery=${2:-false}
    require_owned_directory "$runtime_root" "runtime root" 750
    for name in objects generations build; do
        if [ "$name" != build ] && [ ! -e "$runtime_root/$name" ] && \
           [ ! -L "$runtime_root/$name" ] && [ "$allow_recovery" = true ]; then
            validate_restore_owner_marker \
                "$runtime_root/.$name.$export_id.restore-owner.json" "$phase"
            continue
        fi
        require_owned_directory "$runtime_root/$name" "runtime $name" 750
    done
    require_owned_directory "$runtime_root/page-cache" "runtime page-cache" 700
    for name in build page-cache; do
        if find "$runtime_root/$name" -mindepth 1 -print -quit | grep -q .; then
            echo "runtime $name changed or was not empty during $phase" >&2
            return 73
        fi
    done

    # Restore is restart-safe only for the exact package export ID and only
    # while the Python state layer's ownership marker still seals the sibling.
    # Validate that marker here as a second boundary before command execution.
    for entry in "$runtime_root"/* "$runtime_root"/.[!.]* "$runtime_root"/..?*; do
        if [ ! -e "$entry" ] && [ ! -L "$entry" ]; then
            continue
        fi
        name=${entry##*/}
        case "$name" in
            objects|generations|build|page-cache) ;;
            ".objects.$export_id.restore-incoming"|".generations.$export_id.restore-incoming")
                if [ "$allow_recovery" != true ]; then
                    echo "restore staging remained after successful $phase: $name" >&2
                    return 73
                fi
                validate_restore_staging "$entry" "$phase"
                ;;
            ".objects.$export_id.restore-owner.json"|".generations.$export_id.restore-owner.json")
                if [ "$allow_recovery" != true ]; then
                    echo "restore ownership marker remained after successful $phase: $name" >&2
                    return 73
                fi
                validate_restore_owner_marker "$entry" "$phase"
                ;;
            *)
                echo "unexpected runtime entry during $phase: $name" >&2
                return 73
                ;;
        esac
    done
}

validate_restore_staging() {
    entry=$1
    phase=$2
    name=${entry##*/}
    if [ ! -d "$entry" ] || [ -L "$entry" ]; then
        echo "restore staging is not a regular directory during $phase: $name" >&2
        return 73
    fi
    owner=$(stat -c '%u:%g' "$entry")
    mode=$(stat -c '%a' "$entry")
    case "$mode" in 700|750) ;; *)
        echo "restore staging has unsafe mode during $phase: $name ($mode)" >&2
        return 73
        ;;
    esac
    if [ "$owner" != "$expected_uid:$expected_gid" ]; then
        echo "restore staging has the wrong owner during $phase: $name ($owner)" >&2
        return 73
    fi
    unsafe=$(find "$entry" -xdev \
        \( -type l -o \( ! -type d ! -type f \) -o \
           ! -user "$expected_uid" -o ! -group "$expected_gid" -o \
           -perm /022 \) -print -quit)
    if [ -n "$unsafe" ]; then
        echo "restore staging contains an unsafe entry during $phase: $name" >&2
        return 73
    fi
    marker=${entry%.restore-incoming}.restore-owner.json
    validate_restore_owner_marker "$marker" "$phase"
}

validate_restore_owner_marker() {
    marker=$1
    phase=$2
    name=${marker##*/}
    if [ ! -f "$marker" ] || [ -L "$marker" ] || \
       [ "$(stat -c '%u:%g' "$marker")" != "$expected_uid:$expected_gid" ] || \
       [ "$(stat -c '%a' "$marker")" != 600 ]; then
        echo "restore ownership marker is missing or unsafe during $phase: $name" >&2
        return 73
    fi
    marker_size=$(wc -c <"$marker")
    case "$name" in
        ".objects.$export_id.restore-owner.json") target=objects ;;
        ".generations.$export_id.restore-owner.json") target=generations ;;
        *)
            echo "restore ownership marker name differs during $phase: $name" >&2
            return 73
            ;;
    esac
    if [ "$marker_size" -gt 256 ] || \
       [ "$(cat "$marker")" != \
         "{\"export_id\":\"$export_id\",\"schema_version\":\"cardrag-state-restore-owner.v1\",\"target\":\"$target\"}" ]; then
        echo "restore ownership marker identity differs during $phase: $name" >&2
        return 73
    fi
}

validate_runtime_root preflight true
set +e
/opt/cardrag-portainer/storage-preflight.sh \
    "$runtime_root" -- "$@"
command_status=$?
set -e
if [ "$command_status" -eq 0 ]; then
    validate_runtime_root postflight false
else
    # A failed/killed restore may legitimately leave a marker-owned staging
    # tree at the exact atomic-install boundary.  Preserve it for same-ID retry.
    validate_runtime_root postflight true
fi
exit "$command_status"
