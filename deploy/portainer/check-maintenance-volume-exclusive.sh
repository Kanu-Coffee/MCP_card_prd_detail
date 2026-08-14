#!/bin/sh
set -eu

volume=${CARDRAG_POSTGRES_VOLUME:-cardrag-postgres-v1}
case "$volume" in
    ''|*[!A-Za-z0-9_.-]*)
        echo "unsafe PostgreSQL volume name" >&2
        exit 64
        ;;
esac
if ! command -v docker >/dev/null 2>&1; then
    echo "docker is required to verify PostgreSQL volume exclusivity" >&2
    exit 69
fi
archive_root=${CARDRAG_ARCHIVE_ROOT:-}
expected_archive_source=${CARDRAG_ARCHIVE_EXPECTED_SOURCE:-}
if [ -n "$archive_root" ] || [ -n "$expected_archive_source" ]; then
    if [ -z "$archive_root" ] || [ -z "$expected_archive_source" ]; then
        echo "archive root and expected source must be supplied together" >&2
        exit 64
    fi
    if ! command -v findmnt >/dev/null 2>&1; then
        echo "findmnt is required to verify the live archive mount" >&2
        exit 69
    fi
    actual_archive_source=$(findmnt --noheadings --output SOURCE \
        --target "$archive_root" | sed -n '1p')
    if [ "$actual_archive_source" != "$expected_archive_source" ]; then
        echo "live archive mount source differs from CARDRAG_ARCHIVE_EXPECTED_SOURCE" >&2
        echo "expected: $expected_archive_source" >&2
        echo "actual:   $actual_archive_source" >&2
        exit 78
    fi
fi
if ! docker volume inspect "$volume" >/dev/null 2>&1; then
    echo "PostgreSQL external volume does not exist: $volume" >&2
    exit 66
fi

users=$(docker ps --all --filter "volume=$volume" \
    --format '{{.ID}} {{.Names}} {{.Status}}')
if [ -n "$users" ]; then
    echo "PostgreSQL volume is still attached to another container: $volume" >&2
    printf '%s\n' "$users" >&2
    echo "remove the normal/previous maintenance Stack containers before continuing" >&2
    exit 75
fi

echo "PostgreSQL volume is detached and exclusive: $volume"
