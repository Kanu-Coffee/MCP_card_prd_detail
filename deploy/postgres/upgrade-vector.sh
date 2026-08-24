#!/bin/sh
set -eu

umask 077

postgres_host=${CARDRAG_POSTGRES_ADMIN_HOST:-postgres}
postgres_port=${CARDRAG_POSTGRES_ADMIN_PORT:-5432}
postgres_user=${CARDRAG_POSTGRES_ADMIN_USER:-postgres}
password_file=${CARDRAG_POSTGRES_ADMIN_PASSWORD_FILE:-/run/secrets/postgres_admin_password}
upgrade_enabled=${CARDRAG_POSTGRES_EXTENSION_UPGRADE_ENABLED:-false}
upgrade_id=${CARDRAG_POSTGRES_EXTENSION_UPGRADE_ID:-READY-NOT-SET}

case "$upgrade_enabled" in
    true|false) ;;
    *)
        echo "CARDRAG_POSTGRES_EXTENSION_UPGRADE_ENABLED must be true or false" >&2
        exit 64
        ;;
esac

if [ ! -f "$password_file" ] || [ -L "$password_file" ] || [ ! -r "$password_file" ]; then
    echo "PostgreSQL extension upgrade secret is unavailable" >&2
    exit 77
fi
password=$(tr -d '\r\n' <"$password_file")
if [ -z "$password" ]; then
    echo "PostgreSQL extension upgrade secret is empty" >&2
    exit 77
fi
for command in psql sed; do
    if ! command -v "$command" >/dev/null 2>&1; then
        echo "required PostgreSQL extension upgrade command is missing: $command" >&2
        exit 69
    fi
done

pgpass=/tmp/cardrag-postgres-extension-upgrade.pgpass
{
    printf '%s:%s:%s:%s:' "$postgres_host" "$postgres_port" cardrag "$postgres_user"
    printf '%s' "$password" | sed 's/\\/\\\\/g; s/:/\\:/g'
    printf '\n'
} >"$pgpass"
chmod 0600 "$pgpass"
unset password
export PGPASSFILE=$pgpass
trap 'rm -f "$pgpass"' EXIT HUP INT TERM

psql_cardrag() {
    psql --no-password --no-psqlrc --set ON_ERROR_STOP=1 \
        --host "$postgres_host" --port "$postgres_port" \
        --username "$postgres_user" --dbname cardrag "$@"
}

client_version=$(psql --version | sed -n \
    's/^psql (PostgreSQL) \([0-9][0-9]*\.[0-9][0-9]*\).*/\1/p')
server_number=$(psql_cardrag --tuples-only --no-align \
    --command 'SHOW server_version_num')
if [ "$client_version" != 17.11 ] || [ "$server_number" != 170011 ]; then
    echo "pgvector upgrade requires exact PostgreSQL 17.11 client and server" >&2
    exit 65
fi

installed_version=$(psql_cardrag --tuples-only --no-align --command \
    "SELECT extversion FROM pg_extension WHERE extname = 'vector'")
if [ "$installed_version" = 0.8.6 ]; then
    echo "PostgreSQL 17.11 and pgvector 0.8.6 are ready"
    exit 0
fi
if [ "$upgrade_enabled" != true ]; then
    echo "pgvector is $installed_version; create and verify a same-epoch backup, then explicitly enable the extension upgrade" >&2
    exit 78
fi
case "$upgrade_id" in
    ''|READY-NOT-SET|*[!A-Za-z0-9_.-]*)
        echo "CARDRAG_POSTGRES_EXTENSION_UPGRADE_ID must be an explicit safe identifier" >&2
        exit 64
        ;;
esac

active_sessions=$(psql_cardrag --tuples-only --no-align --command \
    "SELECT count(*) FROM pg_stat_activity
     WHERE datname = 'cardrag'
       AND pid <> pg_backend_pid()")
if [ "$active_sessions" != 0 ]; then
    echo "pgvector upgrade requires zero other CardRAG database sessions" >&2
    exit 75
fi

psql_cardrag --single-transaction >/dev/null <<'SQL'
SELECT pg_advisory_xact_lock(hashtext('cardrag-vector-extension-upgrade'));
DO $cardrag$
DECLARE
    installed_version text;
    extension_owner text;
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_stat_activity
         WHERE datname = 'cardrag'
           AND pid <> pg_backend_pid()
    ) THEN
        RAISE EXCEPTION 'database session appeared before pgvector upgrade';
    END IF;
    SELECT extversion, pg_get_userbyid(extowner)
      INTO installed_version, extension_owner
      FROM pg_extension
     WHERE extname = 'vector';
    IF installed_version IS NULL THEN
        RAISE EXCEPTION 'vector extension is not installed';
    END IF;
    IF extension_owner <> current_user THEN
        RAISE EXCEPTION 'vector extension owner % differs from upgrade role %',
            extension_owner, current_user;
    END IF;
    IF installed_version NOT IN ('0.8.2', '0.8.3', '0.8.4', '0.8.5', '0.8.6') THEN
        RAISE EXCEPTION 'unsupported vector extension upgrade source: %', installed_version;
    END IF;
    IF installed_version <> '0.8.6' THEN
        EXECUTE 'ALTER EXTENSION vector UPDATE TO ''0.8.6''';
        IF to_regclass('public.evidence_vector_idx') IS NOT NULL THEN
            EXECUTE 'REINDEX INDEX public.evidence_vector_idx';
        END IF;
    END IF;
    SELECT extversion INTO installed_version
      FROM pg_extension WHERE extname = 'vector';
    IF installed_version <> '0.8.6' THEN
        RAISE EXCEPTION 'vector extension upgrade verification failed: %', installed_version;
    END IF;
END
$cardrag$;
SQL

echo "PostgreSQL 17.11 and pgvector 0.8.6 upgrade complete: $upgrade_id"
