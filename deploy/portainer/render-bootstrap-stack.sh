#!/bin/sh
set -eu

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
output=${1:-$repository_root/deploy/portainer/cardrag-bootstrap-stack.yaml}

case "$output" in
    /*) ;;
    *) output=$(pwd)/$output ;;
esac
if [ -L "$output" ]; then
    echo "refusing to replace a symlink: $output" >&2
    exit 78
fi
if ! command -v docker >/dev/null 2>&1; then
    echo "Docker Compose is required to render the bootstrap Stack" >&2
    exit 69
fi

temporary=$output.tmp.$$
trap 'rm -f "$temporary"' EXIT HUP INT TERM
docker compose \
    --project-directory "$repository_root" \
    -f "$repository_root/compose.yaml" \
    -f "$repository_root/deploy/portainer/host-storage.compose.yaml" \
    -f "$repository_root/deploy/portainer/bootstrap.compose.yaml" \
    config --no-interpolate --no-path-resolution --output "$temporary"
chmod 0444 "$temporary"
mv -f "$temporary" "$output"
trap - EXIT HUP INT TERM
echo "rendered one-time Portainer Keycloak bootstrap Stack: $output"
