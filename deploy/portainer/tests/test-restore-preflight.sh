#!/bin/sh
set -eu

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
temporary_root=$(mktemp -d)
trap 'rm -rf "$temporary_root"' EXIT HUP INT TERM

runtime=$temporary_root/runtime
archive=$temporary_root/archive
tools=$temporary_root/tools
mkdir -p "$runtime/objects" "$runtime/generations" "$runtime/build" \
    "$runtime/page-cache" "$archive" "$tools"
chmod 0750 "$runtime" "$runtime/objects" "$runtime/generations" "$runtime/build"
chmod 0700 "$runtime/page-cache"
package_id=abcdef123456
export CARDRAG_STATE_PACKAGE_PATH=$archive/cardrag-state-20260813T120000Z-$package_id
ln -s "$repository_root/deploy/portainer/scripts/storage-preflight.sh" \
    "$tools/storage-preflight.sh"

CARDRAG_RUNTIME_UID=$(id -u) \
CARDRAG_RUNTIME_GID=$(id -g) \
CARDRAG_MINIMUM_FREE_GIB=0 \
CARDRAG_MINIMUM_FREE_PERCENT=0 \
CARDRAG_MAXIMUM_USED_PERCENT=100 \
CARDRAG_WARNING_USED_PERCENT=99 \
PATH=$PATH \
    sed "s#/opt/cardrag-portainer/storage-preflight.sh#$tools/storage-preflight.sh#" \
    "$repository_root/deploy/portainer/scripts/restore-preflight.sh" >"$tools/restore-preflight.sh"
chmod 0755 "$tools/restore-preflight.sh"
if grep -Fq '"$runtime_root" "$archive_root" --' "$tools/restore-preflight.sh"; then
    echo "restore capacity wrapper still checks the read-only archive" >&2
    exit 1
fi

CARDRAG_RUNTIME_UID=$(id -u) \
CARDRAG_RUNTIME_GID=$(id -g) \
CARDRAG_MINIMUM_FREE_GIB=0 \
CARDRAG_MINIMUM_FREE_PERCENT=0 \
CARDRAG_MAXIMUM_USED_PERCENT=100 \
CARDRAG_WARNING_USED_PERCENT=99 \
    "$tools/restore-preflight.sh" "$runtime" "$archive" -- /bin/true >/dev/null

owned_staging=$runtime/.objects.$package_id.restore-incoming
owned_marker=$runtime/.objects.$package_id.restore-owner.json
mkdir -p "$owned_staging"
printf '%s\n' \
    "{\"export_id\":\"$package_id\",\"schema_version\":\"cardrag-state-restore-owner.v1\",\"target\":\"objects\"}" \
    >"$owned_marker"
chmod 0700 "$owned_staging"
chmod 0600 "$owned_marker"
CARDRAG_RUNTIME_UID=$(id -u) \
CARDRAG_RUNTIME_GID=$(id -g) \
CARDRAG_MINIMUM_FREE_GIB=0 \
CARDRAG_MINIMUM_FREE_PERCENT=0 \
CARDRAG_MAXIMUM_USED_PERCENT=100 \
CARDRAG_WARNING_USED_PERCENT=99 \
    "$tools/restore-preflight.sh" "$runtime" "$archive" -- \
    /bin/sh -c 'rmdir "$1" && rm -f "$2"' sh \
    "$owned_staging" "$owned_marker" >/dev/null

mkdir -p "$owned_staging"
printf '%s\n' \
    "{\"export_id\":\"$package_id\",\"schema_version\":\"cardrag-state-restore-owner.v1\",\"target\":\"objects\"}" \
    >"$owned_marker"
chmod 0700 "$owned_staging"
chmod 0600 "$owned_marker"
chmod 0622 "$owned_marker"
if CARDRAG_RUNTIME_UID=$(id -u) \
   CARDRAG_RUNTIME_GID=$(id -g) \
   CARDRAG_MINIMUM_FREE_GIB=0 \
   CARDRAG_MINIMUM_FREE_PERCENT=0 \
   CARDRAG_MAXIMUM_USED_PERCENT=100 \
   CARDRAG_WARNING_USED_PERCENT=99 \
       "$tools/restore-preflight.sh" "$runtime" "$archive" -- /bin/true \
       >/dev/null 2>&1; then
    echo "restore preflight accepted a writable staging marker" >&2
    exit 1
fi
chmod 0600 "$owned_marker"
rm -rf "$owned_staging" "$owned_marker"

# A process killed at the atomic install boundary may leave the exact target
# absent while the external marker remains.  Preflight must let the same export
# ID reach Python so it can complete recovery, then require the target again.
printf '%s\n' \
    "{\"export_id\":\"$package_id\",\"schema_version\":\"cardrag-state-restore-owner.v1\",\"target\":\"objects\"}" \
    >"$owned_marker"
chmod 0600 "$owned_marker"
rmdir "$runtime/objects"
CARDRAG_RUNTIME_UID=$(id -u) \
CARDRAG_RUNTIME_GID=$(id -g) \
CARDRAG_MINIMUM_FREE_GIB=0 \
CARDRAG_MINIMUM_FREE_PERCENT=0 \
CARDRAG_MAXIMUM_USED_PERCENT=100 \
CARDRAG_WARNING_USED_PERCENT=99 \
    "$tools/restore-preflight.sh" "$runtime" "$archive" -- \
    /bin/sh -c 'mkdir -m 0750 "$1" && rm -f "$2"' sh \
    "$runtime/objects" "$owned_marker" >/dev/null

mkdir -p "$runtime/.objects.000000000000.restore-incoming"
if CARDRAG_RUNTIME_UID=$(id -u) \
   CARDRAG_RUNTIME_GID=$(id -g) \
   CARDRAG_MINIMUM_FREE_GIB=0 \
   CARDRAG_MINIMUM_FREE_PERCENT=0 \
   CARDRAG_MAXIMUM_USED_PERCENT=100 \
   CARDRAG_WARNING_USED_PERCENT=99 \
       "$tools/restore-preflight.sh" "$runtime" "$archive" -- /bin/true \
       >/dev/null 2>&1; then
    echo "restore preflight accepted staging for a different export ID" >&2
    exit 1
fi
rm -rf "$runtime/.objects.000000000000.restore-incoming"

mkdir -m 0700 "$runtime/.generations.$package_id.restore-incoming"
if CARDRAG_RUNTIME_UID=$(id -u) \
   CARDRAG_RUNTIME_GID=$(id -g) \
   CARDRAG_MINIMUM_FREE_GIB=0 \
   CARDRAG_MINIMUM_FREE_PERCENT=0 \
   CARDRAG_MAXIMUM_USED_PERCENT=100 \
   CARDRAG_WARNING_USED_PERCENT=99 \
       "$tools/restore-preflight.sh" "$runtime" "$archive" -- /bin/true \
       >/dev/null 2>&1; then
    echo "restore preflight accepted unmarked staging" >&2
    exit 1
fi
rm -rf "$runtime/.generations.$package_id.restore-incoming"

if CARDRAG_RUNTIME_UID=$(id -u) \
   CARDRAG_RUNTIME_GID=$(id -g) \
   CARDRAG_MINIMUM_FREE_GIB=0 \
   CARDRAG_MINIMUM_FREE_PERCENT=0 \
   CARDRAG_MAXIMUM_USED_PERCENT=100 \
   CARDRAG_WARNING_USED_PERCENT=99 \
       "$tools/restore-preflight.sh" "$runtime" "$archive" -- \
       /usr/bin/touch "$runtime/build/unexpected" >/dev/null 2>&1; then
    echo "restore preflight accepted a changed build workspace" >&2
    exit 1
fi
rm -f "$runtime/build/unexpected"

chmod 0722 "$runtime/objects"
if CARDRAG_RUNTIME_UID=$(id -u) \
   CARDRAG_RUNTIME_GID=$(id -g) \
   CARDRAG_MINIMUM_FREE_GIB=0 \
   CARDRAG_MINIMUM_FREE_PERCENT=0 \
   CARDRAG_MAXIMUM_USED_PERCENT=100 \
   CARDRAG_WARNING_USED_PERCENT=99 \
       "$tools/restore-preflight.sh" "$runtime" "$archive" -- /bin/true \
       >/dev/null 2>&1; then
    echo "restore preflight accepted a group/other-writable runtime directory" >&2
    exit 1
fi

echo "restore preflight tests passed"
