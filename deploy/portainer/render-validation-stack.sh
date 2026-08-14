#!/bin/sh
set -eu

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
output=${1:-$repository_root/deploy/portainer/cardrag-validation-stack.yaml}

case "$output" in
    /*) ;;
    *) output=$(pwd)/$output ;;
esac
if [ -L "$output" ]; then
    echo "refusing to replace a symlink: $output" >&2
    exit 78
fi
for variable in CARDRAG_ADMIN_IMAGE CARDRAG_WORKER_IMAGE CARDRAG_MCP_IMAGE; do
    case "$variable" in
        CARDRAG_ADMIN_IMAGE) value=${CARDRAG_ADMIN_IMAGE:-} ;;
        CARDRAG_WORKER_IMAGE) value=${CARDRAG_WORKER_IMAGE:-} ;;
        CARDRAG_MCP_IMAGE) value=${CARDRAG_MCP_IMAGE:-} ;;
    esac
    case "$value" in
        *@sha256:????????????????????????????????????????????????????????????????) ;;
        *)
            echo "$variable must be an already-published digest-qualified image" >&2
            exit 64
            ;;
    esac
    digest=${value##*@sha256:}
    case "$digest" in
        *[!0-9a-f]*)
            echo "$variable must use a lowercase SHA-256 image digest" >&2
            exit 64
            ;;
    esac
done
if ! command -v docker >/dev/null 2>&1; then
    echo "Docker Compose is required to render the validation Stack" >&2
    exit 69
fi

temporary=$output.tmp.$$
trap 'rm -f "$temporary"' EXIT HUP INT TERM
docker compose \
    --project-directory "$repository_root" \
    -f "$repository_root/compose.yaml" \
    -f "$repository_root/deploy/dockerhub.compose.yaml" \
    -f "$repository_root/deploy/portainer/host-storage.compose.yaml" \
    -f "$repository_root/deploy/portainer/validation.compose.yaml" \
    config --no-interpolate --no-path-resolution --output "$temporary"
chmod 0444 "$temporary"
mv -f "$temporary" "$output"
trap - EXIT HUP INT TERM
echo "rendered post-restore Portainer validation Stack: $output"
