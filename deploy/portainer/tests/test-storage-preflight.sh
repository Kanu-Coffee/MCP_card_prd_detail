#!/bin/sh
set -eu

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
temporary_root=$(mktemp -d)
trap 'rm -rf "$temporary_root"' EXIT HUP INT TERM

fake_bin=$temporary_root/bin
target=$temporary_root/target
mkdir -p "$fake_bin" "$target"

printf '%s\n' '#!/bin/sh' \
    'mode=$TEST_DF_MODE' \
    'if [ "$mode" = transition ] && [ -e "$TEST_DF_MARKER" ]; then mode=full; elif [ "$mode" = transition ]; then mode=pass; fi' \
    'if [ "$mode" = transition-low ] && [ -e "$TEST_DF_MARKER" ]; then mode=lowbytes; elif [ "$mode" = transition-low ]; then mode=pass; fi' \
    'case "$1:$mode" in' \
    '-Pk:pass) printf "%s\n" "Filesystem 1024-blocks Used Available Capacity Mounted on" "fixture 104857600 10485760 94371840 10% /fixture" ;;' \
    '-Pi:pass) printf "%s\n" "Filesystem Inodes IUsed IFree IUse% Mounted on" "fixture 1000000 100000 900000 10% /fixture" ;;' \
    '-Pk:full) printf "%s\n" "Filesystem 1024-blocks Used Available Capacity Mounted on" "fixture 104857600 94371840 10485760 90% /fixture" ;;' \
    '-Pi:full) printf "%s\n" "Filesystem Inodes IUsed IFree IUse% Mounted on" "fixture 1000000 900000 100000 90% /fixture" ;;' \
    '-Pk:warning) printf "%s\n" "Filesystem 1024-blocks Used Available Capacity Mounted on" "fixture 104857600 73400320 31457280 70% /fixture" ;;' \
    '-Pi:warning) printf "%s\n" "Filesystem Inodes IUsed IFree IUse% Mounted on" "fixture 1000000 700000 300000 70% /fixture" ;;' \
    '-Pk:lowbytes) printf "%s\n" "Filesystem 1024-blocks Used Available Capacity Mounted on" "fixture 104857600 73400320 31457280 70% /fixture" ;;' \
    '-Pi:lowbytes) printf "%s\n" "Filesystem Inodes IUsed IFree IUse% Mounted on" "fixture 1000000 700000 300000 70% /fixture" ;;' \
    '*) exit 2 ;;' \
    'esac' >"$fake_bin/df"
chmod 0755 "$fake_bin/df"

PATH=$fake_bin:$PATH TEST_DF_MODE=pass \
    "$repository_root/deploy/portainer/scripts/storage-preflight.sh" \
    "$target" -- /bin/sh -c 'exit 0' >/dev/null

if PATH=$fake_bin:$PATH TEST_DF_MODE=full \
   "$repository_root/deploy/portainer/scripts/storage-preflight.sh" \
   "$target" -- /bin/sh -c 'exit 0' >/dev/null 2>&1; then
    echo "storage preflight accepted a 90%-used filesystem" >&2
    exit 1
fi

warning_output=$(PATH=$fake_bin:$PATH TEST_DF_MODE=warning \
    CARDRAG_MINIMUM_FREE_GIB=1 \
    "$repository_root/deploy/portainer/scripts/storage-preflight.sh" \
    "$target" -- /bin/sh -c 'exit 0' 2>&1)
printf '%s\n' "$warning_output" | grep -q 'storage warning:'

if PATH=$fake_bin:$PATH TEST_DF_MODE=transition TEST_DF_MARKER=$temporary_root/postflight \
   "$repository_root/deploy/portainer/scripts/storage-preflight.sh" \
   "$target" -- /usr/bin/touch "$temporary_root/postflight" >/dev/null 2>&1; then
    echo "storage postflight accepted a filesystem that crossed 85%" >&2
    exit 1
fi

PATH=$fake_bin:$PATH TEST_DF_MODE=transition-low \
TEST_DF_MARKER=$temporary_root/lowbytes-marker \
    "$repository_root/deploy/portainer/scripts/storage-preflight.sh" \
    "$target" -- /usr/bin/touch "$temporary_root/lowbytes-marker" >/dev/null

echo "storage preflight tests passed"
