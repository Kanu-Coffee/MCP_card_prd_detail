#!/bin/sh
set -eu

project=${1:-${CARDRAG_LEGACY_PROJECT:-cardrag}}

if ! command -v docker >/dev/null 2>&1; then
    echo "docker is required" >&2
    exit 69
fi
case "$project" in
    ''|*[!A-Za-z0-9_.-]*)
        echo "unsafe Compose project name: $project" >&2
        exit 64
        ;;
esac

container_for_service() {
    service=$1
    matches=$(docker ps --all --quiet \
        --filter "label=com.docker.compose.project=$project" \
        --filter "label=com.docker.compose.service=$service")
    count=$(printf '%s\n' "$matches" | awk 'NF { count++ } END { print count+0 }')
    if [ "$count" -ne 1 ]; then
        echo "expected one $service container for project $project, found $count" >&2
        exit 66
    fi
    printf '%s\n' "$matches"
}

volume_for_destination() {
    container_id=$1
    destination=$2
    record=$(docker inspect --format \
        '{{range .Mounts}}{{printf "%s\t%s\t%s\n" .Destination .Type .Name}}{{end}}' \
        "$container_id" | awk -F '\t' -v wanted="$destination" '$1 == wanted { print $2 "\t" $3 }')
    count=$(printf '%s\n' "$record" | awk 'NF { count++ } END { print count+0 }')
    if [ "$count" -ne 1 ]; then
        echo "expected one mount at $destination, found $count" >&2
        exit 66
    fi
    mount_type=$(printf '%s' "$record" | cut -f1)
    volume_name=$(printf '%s' "$record" | cut -f2)
    if [ "$mount_type" != volume ] || [ -z "$volume_name" ]; then
        echo "$destination is not a Docker named volume" >&2
        exit 65
    fi
    case "$volume_name" in
        *[!A-Za-z0-9_.-]*)
            echo "unsafe discovered volume name" >&2
            exit 78
            ;;
    esac
    printf '%s\n' "$volume_name"
}

postgres_container=$(container_for_service postgres)
mcp_container=$(container_for_service mcp)
worker_container=$(container_for_service worker)

postgres_volume=$(volume_for_destination "$postgres_container" /var/lib/postgresql/data)
objects_volume=$(volume_for_destination "$mcp_container" /var/lib/cardrag/objects)
generations_volume=$(volume_for_destination "$mcp_container" /var/lib/cardrag/generations)
codex_volume=$(volume_for_destination "$worker_container" /run/cardrag-codex)

printf '%s\n' \
    "CARDRAG_POSTGRES_VOLUME=$postgres_volume" \
    "CARDRAG_CODEX_AUTH_VOLUME=$codex_volume" \
    "CARDRAG_LEGACY_OBJECTS_VOLUME=$objects_volume" \
    "CARDRAG_LEGACY_GENERATIONS_VOLUME=$generations_volume"
