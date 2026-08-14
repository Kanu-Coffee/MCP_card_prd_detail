#!/bin/sh
set -eu

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
temporary_root=$(mktemp -d)
trap 'rm -rf "$temporary_root"' EXIT HUP INT TERM
fake_bin=$temporary_root/bin
mkdir -p "$fake_bin"

printf '%s\n' '#!/bin/sh' \
    'case "$1:$2" in' \
    'volume:inspect) [ "${TEST_VOLUME_EXISTS:-true}" = true ] ;;' \
    'ps:--all) [ -n "${TEST_VOLUME_USERS:-}" ] && printf "%s\n" "$TEST_VOLUME_USERS"; exit 0 ;;' \
    '*) exit 2 ;;' \
    'esac' >"$fake_bin/docker"
printf '%s\n' '#!/bin/sh' 'printf "%s\n" "${TEST_FINDMNT_SOURCE:?}"' \
    >"$fake_bin/findmnt"
chmod 0755 "$fake_bin/docker" "$fake_bin/findmnt"

PATH=$fake_bin:$PATH \
TEST_FINDMNT_SOURCE=nas.example:/cardrag \
CARDRAG_ARCHIVE_ROOT=/mnt/cardrag-backup/cardrag \
CARDRAG_ARCHIVE_EXPECTED_SOURCE=nas.example:/cardrag \
    "$repository_root/deploy/portainer/check-maintenance-volume-exclusive.sh" \
    >/dev/null

if PATH=$fake_bin:$PATH \
   TEST_FINDMNT_SOURCE=/dev/local-disk \
   CARDRAG_ARCHIVE_ROOT=/mnt/cardrag-backup/cardrag \
   CARDRAG_ARCHIVE_EXPECTED_SOURCE=nas.example:/cardrag \
       "$repository_root/deploy/portainer/check-maintenance-volume-exclusive.sh" \
       >/dev/null 2>&1; then
    echo "maintenance check accepted the wrong live archive mount" >&2
    exit 1
fi

if PATH=$fake_bin:$PATH \
   TEST_VOLUME_USERS='abc123 cardrag-postgres-1 Exited (0)' \
       "$repository_root/deploy/portainer/check-maintenance-volume-exclusive.sh" \
       >/dev/null 2>&1; then
    echo "volume exclusivity check accepted an attached stopped container" >&2
    exit 1
fi

if PATH=$fake_bin:$PATH TEST_VOLUME_EXISTS=false \
       "$repository_root/deploy/portainer/check-maintenance-volume-exclusive.sh" \
       >/dev/null 2>&1; then
    echo "volume exclusivity check accepted a missing external volume" >&2
    exit 1
fi

echo "maintenance volume exclusivity tests passed"
