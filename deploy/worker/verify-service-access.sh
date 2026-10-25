#!/bin/sh
# Config-only gate: run as the same account/groups as cardrag-worker.service.
set -eu
test "$(id -u)" -eq 0 || { echo 'Run this check as root.' >&2; exit 1; }
RELEASE=$(CDPATH='' cd -- "${1:?release directory required}" && pwd -P)
test -x /usr/sbin/runuser
# Variables in this block belong to the service account's shell.
# shellcheck disable=SC2016
/usr/sbin/runuser -u cardrag -- /bin/sh -eu -c '
    test "$(id -un)" = cardrag
    case " $(id -Gn) " in *" docker "*) ;; *) echo "docker group missing" >&2; exit 1;; esac
    test -x "$1/operations/worker-compose.sh"
    test -r "$1/operations/compose.runtime.private.yaml"
    test -r "$1/deploy/worker/compose.yaml"
    test -r /etc/cardrag/worker.env
    cd "$1"
    exec "$1/operations/worker-compose.sh" config --quiet
' service-access "$RELEASE"
printf 'Service account config check passed: %s\n' "$RELEASE"
