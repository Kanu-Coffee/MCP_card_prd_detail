#!/bin/sh
set -eu

# Docker named volumes start as root-owned. Grant only the application UID the
# roots it must write; never recurse through already-sealed corpus artifacts.
install -d -o 10001 -g 10001 -m 0750 \
    /var/lib/cardrag/objects \
    /var/lib/cardrag/generations \
    /var/lib/cardrag/generations/generations \
    /var/lib/cardrag-build
install -d -o 10001 -g 10001 -m 0700 \
    /var/cache/cardrag-pages \
    /run/cardrag-codex
