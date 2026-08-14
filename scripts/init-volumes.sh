#!/bin/sh
set -eu

# Runtime roots may be external Docker volumes or explicit host bind mounts.
# Refuse symlinks and non-directories before changing ownership so a bad host
# path cannot redirect initialization outside the configured mount.
for path in \
    /var/lib/cardrag/objects \
    /var/lib/cardrag/generations \
    /var/lib/cardrag-build \
    /var/cache/cardrag-pages \
    /run/cardrag-codex
do
    if [ -L "$path" ]; then
        echo "runtime root must not be a symlink: $path" >&2
        exit 78
    fi
    if [ -e "$path" ] && [ ! -d "$path" ]; then
        echo "runtime root must be a directory: $path" >&2
        exit 78
    fi
done

for root in /var/lib/cardrag/objects /var/lib/cardrag/generations; do
    if [ -e "$root/.cardrag-storage-migration-commit" ]; then
        echo "runtime storage has an unfinished migration commit: $root" >&2
        exit 75
    fi
    if find "$root" -mindepth 1 -maxdepth 1 -name '.migration-incoming-*' -print -quit | grep -q .; then
        echo "runtime storage has an unfinished migration staging tree: $root" >&2
        exit 75
    fi
done

# Grant only the application UID the roots it must write. Never recurse through
# existing CAS objects or already-sealed generation artifacts.
install -d -o 10001 -g 10001 -m 0750 \
    /var/lib/cardrag/objects \
    /var/lib/cardrag/generations \
    /var/lib/cardrag/generations/generations \
    /var/lib/cardrag-build
install -d -o 10001 -g 10001 -m 0700 \
    /var/cache/cardrag-pages \
    /run/cardrag-codex
