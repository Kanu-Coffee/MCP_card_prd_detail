#!/bin/sh
set -eu

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
output=${1:-$repository_root/deploy/portainer/cardrag-restore-stack.yaml}

case "$output" in
    /*) ;;
    *) output=$(pwd)/$output ;;
esac
if [ -L "$output" ]; then
    echo "refusing to replace a symlink: $output" >&2
    exit 78
fi
image=${CARDRAG_ADMIN_IMAGE:-}
case "$image" in
    *@sha256:????????????????????????????????????????????????????????????????) ;;
    *)
        echo "CARDRAG_ADMIN_IMAGE must be an already-published digest-qualified image" >&2
        exit 64
        ;;
esac
digest=${image##*@sha256:}
case "$digest" in
    *[!0-9a-f]*)
        echo "CARDRAG_ADMIN_IMAGE must use a lowercase SHA-256 image digest" >&2
        exit 64
        ;;
esac
if ! command -v docker >/dev/null 2>&1; then
    echo "Docker Compose is required to render the restore Stack" >&2
    exit 69
fi

temporary=$output.tmp.$$
trap 'rm -f "$temporary"' EXIT HUP INT TERM
docker compose \
    --project-directory "$repository_root" \
    -f "$repository_root/deploy/portainer/restore-stack.compose.yaml" \
    config --no-interpolate --no-path-resolution --output "$temporary"
chmod 0444 "$temporary"
mv -f "$temporary" "$output"
trap - EXIT HUP INT TERM
echo "rendered dedicated Portainer restore Stack: $output"
