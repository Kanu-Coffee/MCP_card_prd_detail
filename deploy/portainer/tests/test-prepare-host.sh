#!/bin/sh
set -eu

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
temporary_root=$(mktemp -d)
trap 'rm -rf "$temporary_root"' EXIT HUP INT TERM
fake_bin=$temporary_root/bin
mkdir -p "$fake_bin"

printf '%s\n' '#!/bin/sh' 'printf "%s\n" 0' >"$fake_bin/id"
printf '%s\n' '#!/bin/sh' \
    'case "$1:$2" in volume:inspect) exit 0 ;; *) exit 2 ;; esac' \
    >"$fake_bin/docker"
printf '%s\n' '#!/usr/bin/python3' \
    'import os, sys' \
    'source = iter(sys.argv[1:])' \
    'arguments = []' \
    'for argument in source:' \
    '    if argument in {"-o", "-g"}:' \
    '        next(source)' \
    '    else:' \
    '        arguments.append(argument)' \
    'os.execv("/usr/bin/install", ["install", *arguments])' \
    >"$fake_bin/install"
chmod 0755 "$fake_bin/id" "$fake_bin/docker" "$fake_bin/install"

PATH=$fake_bin:$PATH \
CARDRAG_DATA_ROOT=$temporary_root/state/runtime \
CARDRAG_IMPORT_ROOT=$temporary_root/state/imports \
CARDRAG_STATE_ROOT=$temporary_root/state \
CARDRAG_CONFIG_ROOT=$temporary_root/state/config \
    "$repository_root/deploy/portainer/prepare-host-storage.sh" >/dev/null

test "$(stat -c '%a' "$temporary_root/state/runtime")" = 750
test "$(stat -c '%a' "$temporary_root/state/runtime/objects")" = 750
test "$(stat -c '%a' "$temporary_root/state/runtime/generations")" = 750
test "$(stat -c '%a' "$temporary_root/state/runtime/build")" = 750
test "$(stat -c '%a' "$temporary_root/state/runtime/page-cache")" = 700
test "$(stat -c '%a' "$temporary_root/state/imports")" = 750
test "$(stat -c '%a' "$temporary_root/state/migration/reports")" = 750
test "$(stat -c '%a' "$temporary_root/state/config/portainer/archive-preflight.sh")" = 555
test "$(stat -c '%a' "$temporary_root/state/config/portainer/schema13-transition.sh")" = 555
test "$(find "$temporary_root/state/config/portainer/migrations" -maxdepth 1 \
    -type f -name '*.sql' | wc -l)" = 14
test "$(stat -c '%a' "$temporary_root/state/config/portainer/migrations/014_legacy_import_and_portability.sql")" = 444
test -d "$temporary_root/state/config/deployment"
test -z "$(find "$temporary_root/state/config/deployment" -mindepth 1 -print -quit)"
if [ "$(id -u)" = 10001 ] && [ "$(id -g)" = 10001 ]; then
    for path in runtime runtime/objects runtime/generations runtime/build \
                runtime/page-cache imports; do
        test "$(stat -c '%u:%g' "$temporary_root/state/$path")" = 10001:10001
    done
fi

echo "host storage preparation tests passed"
