#!/bin/sh
set -eu

umask 077

if [ -n "${CARDRAG_DATABASE_URL_FILE:-}" ]; then
    if [ ! -r "$CARDRAG_DATABASE_URL_FILE" ]; then
        echo "database URL secret is not readable" >&2
        exit 1
    fi
    CARDRAG_DATABASE_URL=$(tr -d '\r\n' < "$CARDRAG_DATABASE_URL_FILE")
    if [ -z "$CARDRAG_DATABASE_URL" ]; then
        echo "database URL secret is empty" >&2
        exit 1
    fi
    export CARDRAG_DATABASE_URL
    unset CARDRAG_DATABASE_URL_FILE
fi

if [ "$#" -eq 0 ]; then
    set -- cardrag --help
fi

case "${CARDRAG_CONTAINER_ROLE:-}:$1" in
    mcp:cardrag-mcp|worker:cardrag-worker|worker:codex|admin:cardrag|admin:cardrag-volume-init)
        exec "$@"
        ;;
    *)
        echo "unsupported ${CARDRAG_CONTAINER_ROLE:-unset} container command: $1" >&2
        exit 64
        ;;
esac
