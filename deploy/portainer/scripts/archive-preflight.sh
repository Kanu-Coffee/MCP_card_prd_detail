#!/bin/sh
set -eu

archive_root=${1:-}
if [ -z "$archive_root" ]; then
    echo "usage: archive-preflight.sh ARCHIVE_ROOT -- COMMAND [ARG...]" >&2
    exit 64
fi
shift
if [ "${1:-}" != -- ]; then
    echo "usage: archive-preflight.sh ARCHIVE_ROOT -- COMMAND [ARG...]" >&2
    exit 64
fi
shift
if [ "$#" -eq 0 ]; then
    echo "archive preflight requires a command" >&2
    exit 64
fi

expected_source=${CARDRAG_ARCHIVE_EXPECTED_SOURCE:-}
if [ -z "$expected_source" ]; then
    echo "CARDRAG_ARCHIVE_EXPECTED_SOURCE is required" >&2
    exit 64
fi
case "$expected_source" in
    *'
'*|*''*)
        echo "archive expected source must be one line" >&2
        exit 64
        ;;
esac
if [ ! -d "$archive_root" ] || [ -L "$archive_root" ]; then
    echo "archive root must be an existing non-symlink directory" >&2
    exit 78
fi

check_exact_file() {
    path=$1
    expected=$2
    label=$3
    if [ ! -f "$path" ] || [ -L "$path" ]; then
        echo "$label is missing or is not a safe regular file" >&2
        exit 78
    fi
    mode=$(stat -c '%a' "$path")
    if [ "$mode" != 440 ]; then
        echo "$label must have mode 0440 (actual $mode)" >&2
        exit 77
    fi
    actual=$(cat "$path")
    bytes=$(wc -c <"$path" | tr -d '[:space:]')
    expected_bytes=$((${#expected} + 1))
    if [ "$actual" != "$expected" ] || [ "$bytes" -ne "$expected_bytes" ]; then
        echo "$label content mismatch" >&2
        exit 78
    fi
}

check_exact_file \
    "$archive_root/.cardrag-archive-root" \
    cardrag-archive-v1 \
    "archive sentinel"
check_exact_file \
    "$archive_root/.cardrag-archive-mount-source" \
    "$expected_source" \
    "archive mount identity record"

exec "$@"
