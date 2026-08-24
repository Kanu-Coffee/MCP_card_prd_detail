#!/bin/sh
set -eu

umask 027

data_root=${CARDRAG_DATA_ROOT:-/srv/cardrag/runtime}
import_root=${CARDRAG_IMPORT_ROOT:-/srv/cardrag/imports}
state_root=${CARDRAG_STATE_ROOT:-/srv/cardrag}
config_root=${CARDRAG_CONFIG_ROOT:-$state_root/config}
postgres_volume=${CARDRAG_POSTGRES_VOLUME:-cardrag-postgres-v1}
codex_volume=${CARDRAG_CODEX_AUTH_VOLUME:-cardrag-codex-auth-v1}
create_volumes=false

if [ "${1:-}" = "--create-empty-external-volumes" ]; then
    create_volumes=true
    shift
fi
if [ "$#" -ne 0 ]; then
    echo "usage: $0 [--create-empty-external-volumes]" >&2
    exit 64
fi
if [ "$(id -u)" -ne 0 ]; then
    echo "host storage preparation must run as root" >&2
    exit 77
fi

check_absolute_safe() {
    candidate=$1
    case "$candidate" in
        /*) ;;
        *)
            echo "storage path must be absolute: $candidate" >&2
            exit 78
            ;;
    esac
    case "$candidate" in
        /|/srv|/mnt|/var|/var/lib)
            echo "storage path is too broad: $candidate" >&2
            exit 78
            ;;
    esac

    cursor=/
    old_ifs=$IFS
    IFS=/
    # Intentional word splitting walks each absolute path component.
    # shellcheck disable=SC2086
    set -- $candidate
    IFS=$old_ifs
    for component do
        [ -n "$component" ] || continue
        if [ "$cursor" = / ]; then
            cursor=/$component
        else
            cursor=$cursor/$component
        fi
        if [ -L "$cursor" ]; then
            echo "storage path must not traverse a symlink: $cursor" >&2
            exit 78
        fi
    done
}

check_absolute_safe "$data_root"
check_absolute_safe "$import_root"
check_absolute_safe "$state_root"
check_absolute_safe "$config_root"

install -d -o 10001 -g 10001 -m 0750 \
    "$data_root" \
    "$data_root/objects" \
    "$data_root/generations" \
    "$data_root/build" \
    "$import_root"
install -d -o root -g root -m 0750 \
    "$state_root/migration" \
    "$state_root/migration/reports"
install -d -o 10001 -g 10001 -m 0700 "$data_root/page-cache"
install -d -o root -g root -m 0755 \
    "$config_root" \
    "$config_root/postgres" \
    "$config_root/keycloak" \
    "$config_root/portainer" \
    "$config_root/portainer/migrations" \
    "$config_root/deployment"

script_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
install -o root -g root -m 0555 \
    "$script_root/deploy/postgres/init-databases.sh" \
    "$config_root/postgres/init-databases.sh"
install -o root -g root -m 0555 \
    "$script_root/deploy/postgres/upgrade-vector.sh" \
    "$config_root/postgres/upgrade-vector.sh"
install -o root -g root -m 0444 \
    "$script_root/deploy/keycloak/cardrag-realm.json" \
    "$config_root/keycloak/cardrag-realm.json"
install -o root -g root -m 0555 \
    "$script_root/deploy/keycloak/entrypoint.sh" \
    "$config_root/keycloak/entrypoint.sh"
install -o root -g root -m 0555 \
    "$script_root/deploy/portainer/scripts/archive-preflight.sh" \
    "$script_root/deploy/portainer/scripts/restore-preflight.sh" \
    "$script_root/deploy/portainer/scripts/schema13-transition.sh" \
    "$script_root/deploy/portainer/scripts/storage-migrate.sh" \
    "$script_root/deploy/portainer/scripts/storage-preflight.sh" \
    "$script_root/deploy/portainer/scripts/verify-runtime-storage.py" \
    "$config_root/portainer/"

# The schema-13 bridge validates an exact trusted 001..015 inventory before it
# takes either dump or migration action. Install only that bounded set; a future
# release must deliberately update this bridge instead of silently adding SQL.
version=1
while [ "$version" -le 15 ]; do
    prefix=$(printf '%03d_' "$version")
    set -- "$script_root/src/cardrag/db/migrations/$prefix"*.sql
    if [ "$#" -ne 1 ] || [ ! -f "$1" ] || [ -L "$1" ]; then
        echo "expected exactly one migration file for version $version" >&2
        exit 66
    fi
    install -o root -g root -m 0444 "$1" \
        "$config_root/portainer/migrations/${1##*/}"
    version=$((version + 1))
done

if ! command -v docker >/dev/null 2>&1; then
    echo "docker is required to validate external volumes" >&2
    exit 69
fi

for volume_name in "$postgres_volume" "$codex_volume"; do
    case "$volume_name" in
        ''|*[!A-Za-z0-9_.-]*)
            echo "unsafe Docker volume name: $volume_name" >&2
            exit 78
            ;;
    esac
    if docker volume inspect "$volume_name" >/dev/null 2>&1; then
        continue
    fi
    if [ "$create_volumes" != true ]; then
        echo "external volume does not exist: $volume_name" >&2
        echo "for a fresh install, rerun with --create-empty-external-volumes" >&2
        exit 66
    fi
    docker volume create --name "$volume_name" >/dev/null
done

echo "host runtime roots and external volumes are ready"
