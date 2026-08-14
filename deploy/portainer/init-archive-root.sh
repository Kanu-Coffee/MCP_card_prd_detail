#!/bin/sh
set -eu

umask 027

archive_root=${CARDRAG_ARCHIVE_ROOT:-/mnt/cardrag-backup/cardrag}
expected_source=${CARDRAG_ARCHIVE_EXPECTED_SOURCE:-}
sentinel=$archive_root/.cardrag-archive-root
source_record=$archive_root/.cardrag-archive-mount-source

if [ "$(id -u)" -ne 0 ]; then
    echo "archive initialization must run as root" >&2
    exit 77
fi
if [ -z "$expected_source" ]; then
    echo "CARDRAG_ARCHIVE_EXPECTED_SOURCE must name the mounted NAS source" >&2
    exit 64
fi
case "$expected_source" in
    *'
'*|*''*)
        echo "CARDRAG_ARCHIVE_EXPECTED_SOURCE must be one line" >&2
        exit 64
        ;;
esac
if ! command -v findmnt >/dev/null 2>&1; then
    echo "findmnt is required to verify the archive mount" >&2
    exit 69
fi
if [ ! -d "$archive_root" ] || [ -L "$archive_root" ]; then
    echo "archive root must be an existing non-symlink directory" >&2
    exit 78
fi
actual_source=$(findmnt --noheadings --output SOURCE --target "$archive_root" | sed -n '1p')
if [ "$actual_source" != "$expected_source" ]; then
    echo "archive mount source mismatch" >&2
    echo "expected: $expected_source" >&2
    echo "actual:   $actual_source" >&2
    exit 78
fi
chown 10001:10001 "$archive_root"
chmod 0750 "$archive_root"
for metadata_file in "$sentinel" "$source_record"; do
    if [ -e "$metadata_file" ] && [ -L "$metadata_file" ]; then
        echo "archive identity file must not be a symlink" >&2
        exit 78
    fi
done

temporary=$archive_root/.cardrag-archive-root.tmp.$$
source_temporary=$archive_root/.cardrag-archive-mount-source.tmp.$$
trap 'rm -f "$temporary" "$source_temporary"' EXIT HUP INT TERM
printf '%s\n' cardrag-archive-v1 >"$temporary"
printf '%s\n' "$expected_source" >"$source_temporary"
chown 10001:10001 "$temporary" "$source_temporary"
chmod 0440 "$temporary" "$source_temporary"
mv -f "$temporary" "$sentinel"
mv -f "$source_temporary" "$source_record"
trap - EXIT HUP INT TERM
echo "CardRAG archive sentinel initialized on verified mount"
