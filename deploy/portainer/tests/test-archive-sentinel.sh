#!/bin/sh
set -eu

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
temporary_root=$(mktemp -d)
trap 'rm -rf "$temporary_root"' EXIT HUP INT TERM

archive=$temporary_root/archive
fake_bin=$temporary_root/bin
mkdir -p "$archive" "$fake_bin"

printf '%s\n' '#!/bin/sh' 'printf "%s\n" 0' >"$fake_bin/id"
printf '%s\n' '#!/bin/sh' 'printf "%s\n" "${TEST_FINDMNT_SOURCE:?}"' >"$fake_bin/findmnt"
printf '%s\n' '#!/bin/sh' 'exit 0' >"$fake_bin/chown"
chmod 0755 "$fake_bin/id" "$fake_bin/findmnt" "$fake_bin/chown"

PATH=$fake_bin:$PATH \
TEST_FINDMNT_SOURCE=nas.example:/cardrag \
CARDRAG_ARCHIVE_ROOT=$archive \
CARDRAG_ARCHIVE_EXPECTED_SOURCE=nas.example:/cardrag \
    "$repository_root/deploy/portainer/init-archive-root.sh" >/dev/null

test "$(cat "$archive/.cardrag-archive-root")" = cardrag-archive-v1
test "$(stat -c '%a' "$archive/.cardrag-archive-root")" = 440
test "$(cat "$archive/.cardrag-archive-mount-source")" = nas.example:/cardrag
test "$(stat -c '%a' "$archive/.cardrag-archive-mount-source")" = 440

CARDRAG_ARCHIVE_EXPECTED_SOURCE=nas.example:/cardrag \
    "$repository_root/deploy/portainer/scripts/archive-preflight.sh" \
    "$archive" -- /bin/true

if CARDRAG_ARCHIVE_EXPECTED_SOURCE=/dev/local-disk \
       "$repository_root/deploy/portainer/scripts/archive-preflight.sh" \
       "$archive" -- /bin/true >/dev/null 2>&1; then
    echo "archive preflight accepted a different expected source" >&2
    exit 1
fi

chmod 0640 "$archive/.cardrag-archive-root"
printf '%s\n\n' cardrag-archive-v1 >"$archive/.cardrag-archive-root"
chmod 0440 "$archive/.cardrag-archive-root"
if CARDRAG_ARCHIVE_EXPECTED_SOURCE=nas.example:/cardrag \
       "$repository_root/deploy/portainer/scripts/archive-preflight.sh" \
       "$archive" -- /bin/true >/dev/null 2>&1; then
    echo "archive preflight accepted non-exact sentinel content" >&2
    exit 1
fi
chmod 0640 "$archive/.cardrag-archive-root"
printf '%s\n' cardrag-archive-v1 >"$archive/.cardrag-archive-root"
chmod 0440 "$archive/.cardrag-archive-root"

if PATH=$fake_bin:$PATH \
   TEST_FINDMNT_SOURCE=/dev/local-disk \
   CARDRAG_ARCHIVE_ROOT=$archive \
   CARDRAG_ARCHIVE_EXPECTED_SOURCE=nas.example:/cardrag \
       "$repository_root/deploy/portainer/init-archive-root.sh" >/dev/null 2>&1; then
    echo "archive initializer accepted the wrong mount source" >&2
    exit 1
fi

echo "archive sentinel tests passed"
