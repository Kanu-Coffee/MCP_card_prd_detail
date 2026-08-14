#!/bin/sh
set -eu

umask 027

source_objects=${CARDRAG_MIGRATION_SOURCE_OBJECTS_ROOT:-/mnt/cardrag-source/objects}
source_generations=${CARDRAG_MIGRATION_SOURCE_GENERATIONS_ROOT:-/mnt/cardrag-source/generations}
target_objects=${CARDRAG_MIGRATION_TARGET_OBJECTS_ROOT:-/var/lib/cardrag/objects}
target_generations=${CARDRAG_MIGRATION_TARGET_GENERATIONS_ROOT:-/var/lib/cardrag/generations}
reports_root=${CARDRAG_MIGRATION_REPORTS_ROOT:-/var/lib/cardrag-migration-reports}
verifier=${CARDRAG_STORAGE_VERIFIER:-/opt/cardrag-portainer/verify-runtime-storage.py}
migration_id=${CARDRAG_STORAGE_MIGRATION_ID:-}
runtime_uid=${CARDRAG_RUNTIME_UID:-10001}
runtime_gid=${CARDRAG_RUNTIME_GID:-10001}
resume=${CARDRAG_STORAGE_MIGRATION_RESUME:-false}

if [ -z "${CARDRAG_LEGACY_OBJECTS_VOLUME:-}" ] || \
   [ -z "${CARDRAG_LEGACY_GENERATIONS_VOLUME:-}" ]; then
    echo "legacy volume names must be supplied from discovery output" >&2
    exit 64
fi
if [ -z "$migration_id" ]; then
    migration_id=$(date -u +%Y%m%dT%H%M%SZ)-$$
fi
case "$migration_id" in
    *[!A-Za-z0-9_.-]*)
        echo "unsafe migration ID" >&2
        exit 64
        ;;
esac
for numeric_id in "$runtime_uid" "$runtime_gid"; do
    case "$numeric_id" in
        ''|*[!0-9]*)
            echo "runtime UID/GID must be numeric" >&2
            exit 64
            ;;
    esac
done
case "$resume" in
    true|false) ;;
    *)
        echo "CARDRAG_STORAGE_MIGRATION_RESUME must be true or false" >&2
        exit 64
        ;;
esac

require_directory() {
    path=$1
    label=$2
    case "$path" in
        /*) ;;
        *)
            echo "$label path must be absolute" >&2
            exit 78
            ;;
    esac
    case "$path" in
        /|/var|/var/lib|/mnt|/srv|/tmp)
            echo "$label path is too broad" >&2
            exit 78
            ;;
    esac
    if [ ! -d "$path" ] || [ -L "$path" ]; then
        echo "$label must be an existing non-symlink directory" >&2
        exit 78
    fi
}

require_directory "$source_objects" "source objects"
require_directory "$source_generations" "source generations"
require_directory "$target_objects" "target objects"
require_directory "$target_generations" "target generations"
require_directory "$reports_root" "migration reports"
for target_root in "$target_objects" "$target_generations"; do
    target_owner=$(stat -c '%u:%g' "$target_root")
    target_mode=$(stat -c '%a' "$target_root")
    if [ "$target_owner" != "$runtime_uid:$runtime_gid" ] || [ "$target_mode" != 750 ]; then
        echo "migration target root must be owned by $runtime_uid:$runtime_gid with mode 0750: $target_root" >&2
        exit 77
    fi
done
reports_owner=$(stat -c '%u' "$reports_root")
if [ "$reports_owner" != "$(id -u)" ]; then
    echo "migration reports must be owned by the migration process UID" >&2
    exit 77
fi
reports_mode=$(stat -c '%a' "$reports_root")
case "$reports_mode" in
    700|750) ;;
    *)
        echo "migration reports must have mode 0700 or 0750" >&2
        exit 77
        ;;
esac
if ! command -v flock >/dev/null 2>&1; then
    echo "flock is required to serialize storage migrations" >&2
    exit 69
fi
exec 9>"$reports_root/.storage-migrate.lock"
if ! flock --nonblock 9; then
    echo "another storage migration holds the host lock" >&2
    exit 75
fi

incoming_name=.migration-incoming-$migration_id
incoming_objects=$target_objects/$incoming_name
incoming_generations=$target_generations/$incoming_name
owner_name=.cardrag-storage-migration-owner
commit_name=.cardrag-storage-migration-commit
owner_body=$(printf '%s\n%s\n%s\n%s' \
    cardrag-storage-migration-owner.v1 \
    "migration_id=$migration_id" \
    "objects_volume=$CARDRAG_LEGACY_OBJECTS_VOLUME" \
    "generations_volume=$CARDRAG_LEGACY_GENERATIONS_VOLUME")
complete_report=$reports_root/$migration_id-complete.json
if [ -e "$complete_report" ] || [ -L "$complete_report" ]; then
    report_exists=true
else
    report_exists=false
fi
attempt_reports=$(mktemp -d "$reports_root/.storage-migrate-$migration_id.XXXXXX")
chmod 0700 "$attempt_reports"
source_report=$attempt_reports/source.json
target_report=$attempt_reports/target.json
final_report=$attempt_reports/final.json

verify_marker() {
    marker=$1
    if [ ! -f "$marker" ] || [ -L "$marker" ]; then
        echo "migration ownership marker is missing or unsafe: $marker" >&2
        exit 73
    fi
    if [ "$(cat "$marker")" != "$owner_body" ]; then
        echo "migration ownership marker belongs to a different operation" >&2
        exit 73
    fi
}

verify_complete_report() {
    if [ ! -f "$complete_report" ] || [ -L "$complete_report" ]; then
        echo "migration completion report is unsafe" >&2
        exit 73
    fi
    python3 - "$complete_report" "$migration_id" \
        "$CARDRAG_LEGACY_OBJECTS_VOLUME" \
        "$CARDRAG_LEGACY_GENERATIONS_VOLUME" <<'PY'
import json
import os
import stat
import sys

path, migration_id, objects_volume, generations_volume = sys.argv[1:]
metadata = os.lstat(path)
if not stat.S_ISREG(metadata.st_mode):
    raise SystemExit("completion report is not regular")
with open(path, encoding="utf-8") as stream:
    payload = json.load(stream)
expected = {
    "schema_version": "cardrag-storage-migration.v1",
    "migration_id": migration_id,
    "source_objects_volume": objects_volume,
    "source_generations_volume": generations_volume,
    "state": "complete",
}
if payload != expected:
    raise SystemExit("completion report differs from this migration")
PY
}

fsync_directories() {
    python3 - "$@" <<'PY'
import os
import sys
for path in sys.argv[1:]:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
PY
}

fsync_payload_trees() {
    python3 - "$@" <<'PY'
import os
import stat
import sys

for root in sys.argv[1:]:
    for current, directory_names, file_names in os.walk(root, topdown=False, followlinks=False):
        for name in file_names:
            path = os.path.join(current, name)
            metadata = os.lstat(path)
            if not stat.S_ISREG(metadata.st_mode):
                raise SystemExit(f"unsafe payload entry before durable commit: {path}")
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        for name in directory_names:
            path = os.path.join(current, name)
            metadata = os.lstat(path)
            if not stat.S_ISDIR(metadata.st_mode):
                raise SystemExit(f"unsafe payload directory before durable commit: {path}")
            descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        descriptor = os.open(current, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
PY
}

verify_target_root_permissions() {
    for target_root in "$target_objects" "$target_generations"; do
        target_owner=$(stat -c '%u:%g' "$target_root")
        target_mode=$(stat -c '%a' "$target_root")
        if [ "$target_owner" != "$runtime_uid:$runtime_gid" ] || [ "$target_mode" != 750 ]; then
            echo "migration changed target root ownership or mode: $target_root" >&2
            exit 77
        fi
    done
}

if [ "$report_exists" = true ]; then
    verify_complete_report
    marker_seen=false
    for marker in "$target_objects/$commit_name" "$target_generations/$commit_name"; do
        if [ -e "$marker" ] || [ -L "$marker" ]; then
            verify_marker "$marker"
            marker_seen=true
        fi
    done
    if [ "$marker_seen" = true ] && [ "$resume" != true ]; then
        echo "verified completion needs marker finalization; retry with CARDRAG_STORAGE_MIGRATION_RESUME=true" >&2
        exit 75
    fi
    python3 "$verifier" \
        --objects-root "$source_objects" \
        --generations-root "$source_generations" \
        --output "$source_report"
    python3 "$verifier" \
        --objects-root "$target_objects" \
        --generations-root "$target_generations" \
        --allow-migration-markers \
        --output "$final_report"
    if ! cmp -s "$source_report" "$final_report"; then
        echo "completed target no longer matches its verified source" >&2
        exit 74
    fi
    rm -f -- "$target_objects/$commit_name" "$target_generations/$commit_name"
    fsync_directories "$target_objects" "$target_generations"
    verify_target_root_permissions
    echo "storage migration already complete: $migration_id"
    exit 0
fi

prepare_target() {
    root=$1
    label=$2
    commit=$root/$commit_name
    incoming=$root/$incoming_name
    owned_partial=false

    if [ -e "$commit" ] || [ -L "$commit" ]; then
        verify_marker "$commit"
        owned_partial=true
    elif [ -e "$incoming" ] || [ -L "$incoming" ]; then
        if [ ! -d "$incoming" ] || [ -L "$incoming" ]; then
            echo "$label migration staging is unsafe" >&2
            exit 73
        fi
        verify_marker "$incoming/$owner_name"
        owned_partial=true
    fi

    for entry in "$root"/* "$root"/.[!.]* "$root"/..?*; do
        if [ ! -e "$entry" ] && [ ! -L "$entry" ]; then
            continue
        fi
        name=${entry##*/}
        case "$label:$name" in
            objects:$incoming_name|objects:$commit_name|objects:.incoming|objects:sha256) ;;
            generations:$incoming_name|generations:$commit_name|generations:generations|generations:current.json|generations:publication-history.jsonl|generations:.publish.lock) ;;
            *)
                echo "$label target contains an unowned entry: $name" >&2
                exit 73
                ;;
        esac
        if [ "$name" != "$incoming_name" ] && [ "$name" != "$commit_name" ]; then
            if [ "$owned_partial" != true ]; then
                echo "$label target is not empty" >&2
                exit 73
            fi
        fi
    done

    if [ "$owned_partial" = true ]; then
        if [ "$resume" != true ]; then
            echo "$label has a marker-owned partial migration; retry with the same ID and CARDRAG_STORAGE_MIGRATION_RESUME=true" >&2
            exit 75
        fi
        for entry in "$root"/* "$root"/.[!.]* "$root"/..?*; do
            if [ ! -e "$entry" ] && [ ! -L "$entry" ]; then
                continue
            fi
            name=${entry##*/}
            case "$name" in
                "$incoming_name"|"$commit_name"|.incoming|sha256|generations|current.json|publication-history.jsonl|.publish.lock)
                    chmod -R u+rwX -- "$entry" 2>/dev/null || true
                    rm -rf -- "$entry"
                    ;;
            esac
        done
    fi

    install -d -m 0750 "$incoming"
    printf '%s\n' "$owner_body" >"$incoming/$owner_name"
    chmod 0600 "$incoming/$owner_name"
}

prepare_target "$target_objects" objects
prepare_target "$target_generations" generations

python3 "$verifier" \
    --objects-root "$source_objects" \
    --generations-root "$source_generations" \
    --output "$source_report"

cp -a "$source_objects/." "$incoming_objects/"
cp -a "$source_generations/." "$incoming_generations/"
rm -f -- "$incoming_generations/.publish.lock"

python3 "$verifier" \
    --objects-root "$incoming_objects" \
    --generations-root "$incoming_generations" \
    --allow-migration-markers \
    --output "$target_report"

if ! cmp -s "$source_report" "$target_report"; then
    echo "source and target verification reports differ" >&2
    exit 74
fi

printf '%s\n' "$owner_body" >"$target_objects/$commit_name"
printf '%s\n' "$owner_body" >"$target_generations/$commit_name"
chmod 0600 "$target_objects/$commit_name" "$target_generations/$commit_name"
# Persist the recovery ownership records before removing the staging owners or
# moving any payload. A power loss at any later point is therefore classifiable
# and retryable with the same migration ID.
fsync_payload_trees "$target_objects" "$target_generations"

find "$incoming_objects" -mindepth 1 -type d -exec chmod 0750 {} +
find "$incoming_objects" -type f -exec chmod 0444 {} +
# Keep the first-level generation collection writable until its atomic move;
# individual sealed generation directories remain read-only.
if [ -d "$incoming_generations/generations" ]; then
    find "$incoming_generations/generations" -mindepth 1 -type d -exec chmod 0550 {} +
    find "$incoming_generations/generations" -type f -exec chmod 0440 {} +
fi
# current.json is atomically replaced and publication-history.jsonl is
# append-only, so writable root metadata is distinct from sealed generations.
find "$incoming_generations" -mindepth 1 -maxdepth 1 -type f -exec chmod 0640 {} +
chown -R "$runtime_uid:$runtime_gid" "$incoming_objects" "$incoming_generations"

rm -f -- "$incoming_objects/$owner_name" "$incoming_generations/$owner_name"

find "$incoming_objects" -mindepth 1 -maxdepth 1 -exec mv -- {} "$target_objects/" \;
find "$incoming_generations" -mindepth 1 -maxdepth 1 -exec mv -- {} "$target_generations/" \;
rmdir "$incoming_objects" "$incoming_generations"

python3 "$verifier" \
    --objects-root "$target_objects" \
    --generations-root "$target_generations" \
    --allow-migration-markers \
    --output "$final_report"
if ! cmp -s "$source_report" "$final_report"; then
    echo "final runtime store differs from the verified source" >&2
    exit 74
fi
# The completion report is the durable commit record. Flush every copied byte
# and containing directory before publishing it so power loss cannot leave a
# verified report ahead of its payload.
fsync_payload_trees "$target_objects" "$target_generations"
python3 - "$complete_report" "$migration_id" \
    "$CARDRAG_LEGACY_OBJECTS_VOLUME" \
    "$CARDRAG_LEGACY_GENERATIONS_VOLUME" <<'PY'
import json
import os
import sys

path = sys.argv[1]
payload = json.dumps({
    "schema_version": "cardrag-storage-migration.v1",
    "migration_id": sys.argv[2],
    "source_objects_volume": sys.argv[3],
    "source_generations_volume": sys.argv[4],
    "state": "complete",
}, sort_keys=True, indent=2) + "\n"
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
descriptor = os.open(path, flags, 0o440)
try:
    encoded = payload.encode()
    offset = 0
    while offset < len(encoded):
        offset += os.write(descriptor, encoded[offset:])
    os.fsync(descriptor)
finally:
    os.close(descriptor)
directory = os.open(os.path.dirname(path), os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory)
finally:
    os.close(directory)
PY
rm -f -- "$target_objects/$commit_name" "$target_generations/$commit_name"
fsync_directories "$target_objects" "$target_generations"
verify_target_root_permissions
echo "storage migration complete: $migration_id"
