#!/bin/sh
set -eu

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
upgrade_script=$repository_root/deploy/postgres/upgrade-vector.sh
migration_root=$repository_root/src/cardrag/db/migrations
old_postgres_image=pgvector/pgvector:0.8.2-pg17-bookworm@sha256:feb68f4f15446397d8cac7f4fe48fe4586de83160d1fc48b46283312d1a33966
postgres_image=pgvector/pgvector:0.8.6-pg17-bookworm@sha256:cf134a767f474095eeba57e0117be8e568e011a63f33fbf252f14c9b760f8e6f

sh -n "$upgrade_script"
for contract in \
    'CARDRAG_POSTGRES_EXTENSION_UPGRADE_ENABLED' \
    'CARDRAG_POSTGRES_EXTENSION_UPGRADE_ID' \
    "ALTER EXTENSION vector UPDATE TO ''0.8.6''" \
    'REINDEX INDEX public.evidence_vector_idx' \
    "server_number\" != 170011" \
    "installed_version\" = 0.8.6"
do
    grep -Fq "$contract" "$upgrade_script"
done

if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    echo "PostgreSQL extension upgrade integration skipped (Docker unavailable)"
    exit 0
fi
for image in "$old_postgres_image" "$postgres_image"; do
    if ! docker image inspect "$image" >/dev/null 2>&1; then
        docker pull "$image" >/dev/null
    fi
done

temporary_root=$(mktemp -d)
suffix=$$
network=cardrag-vector-upgrade-$suffix
postgres_container=cardrag-vector-postgres-$suffix
postgres_volume=cardrag-vector-pgdata-$suffix
admin_password=vector-upgrade-fixture
cardrag_password=cardrag-vector-fixture
secret_file=$temporary_root/postgres-admin-password

cleanup() {
    docker rm --force "$postgres_container" >/dev/null 2>&1 || true
    docker network rm "$network" >/dev/null 2>&1 || true
    docker volume rm "$postgres_volume" >/dev/null 2>&1 || true
    rm -rf "$temporary_root"
}
trap cleanup EXIT HUP INT TERM

chmod 0755 "$temporary_root"
printf '%s\n' "$admin_password" >"$secret_file"
chmod 0444 "$secret_file"
docker network create "$network" >/dev/null
docker volume create "$postgres_volume" >/dev/null
docker run --detach --name "$postgres_container" --network "$network" \
    --env POSTGRES_PASSWORD="$admin_password" \
    --env POSTGRES_USER=postgres --env POSTGRES_DB=cardrag \
    --volume "$postgres_volume:/var/lib/postgresql/data" \
    "$old_postgres_image" >/dev/null

ready=false
attempt=0
while [ "$attempt" -lt 45 ]; do
    if docker exec "$postgres_container" \
        pg_isready --username postgres --dbname cardrag >/dev/null 2>&1; then
        ready=true
        break
    fi
    attempt=$((attempt + 1))
    sleep 1
done
if [ "$ready" != true ]; then
    docker logs "$postgres_container" >&2 || true
    exit 1
fi

docker exec --interactive "$postgres_container" psql --username postgres --dbname cardrag \
    --set ON_ERROR_STOP=1 >/dev/null <<'SQL'
CREATE ROLE cardrag LOGIN PASSWORD 'cardrag-vector-fixture';
CREATE ROLE cardrag_worker LOGIN NOINHERIT PASSWORD 'worker-vector-fixture';
CREATE ROLE cardrag_mcp LOGIN NOINHERIT PASSWORD 'mcp-vector-fixture';
ALTER DATABASE cardrag OWNER TO cardrag;
CREATE EXTENSION vector;
GRANT USAGE, CREATE ON SCHEMA public TO cardrag;
SQL

version=1
while [ "$version" -le 14 ]; do
    prefix=$(printf '%03d_' "$version")
    set -- "$migration_root/$prefix"*.sql
    test "$#" -eq 1 && test -f "$1"
    migration=$1
    migration_name=${migration##*/}
    migration_checksum=$(sha256sum "$migration" | awk '{print $1}')
    docker exec --interactive --env PGPASSWORD="$cardrag_password" \
        "$postgres_container" psql --no-password --no-psqlrc \
        --username cardrag --dbname cardrag --set ON_ERROR_STOP=1 \
        >/dev/null <"$migration"
    docker exec --env PGPASSWORD="$cardrag_password" "$postgres_container" \
        psql --no-password --no-psqlrc --username cardrag --dbname cardrag \
        --set ON_ERROR_STOP=1 --command \
        "INSERT INTO schema_migrations(version, name, checksum) VALUES
         ($version, '$migration_name', '$migration_checksum')" >/dev/null
    version=$((version + 1))
done

docker exec --interactive --env PGPASSWORD="$cardrag_password" \
    "$postgres_container" psql --no-password --no-psqlrc \
    --username cardrag --dbname cardrag --set ON_ERROR_STOP=1 >/dev/null <<'SQL'
INSERT INTO generations(
    generation_id, state, manifest_sha256, root_uri, schema_version,
    embedding_provider, embedding_model, embedding_dimension
) VALUES (
    'old-pgdata-fixture', 'building', repeat('a', 64),
    'generations/old-pgdata-fixture', 'fixture-v1',
    'fixture', 'fixture-model', 1536
);
INSERT INTO evidence(
    generation_id, evidence_id, document_id, issuer, product_code,
    product_name, document_type, effective_date, source_version, section_type,
    page_start, page_end, span_start, span_end, text, text_sha256, confidence,
    is_latest, embedding, source_spans
) VALUES (
    'old-pgdata-fixture', 'old-evidence', 'old-document', 'woori', 'old-card',
    'Old volume fixture', 'terms', DATE '2026-01-01', 'v1', 'benefit',
    1, 1, 0, 7, 'fixture', repeat('b', 64), 1.0, true,
    array_fill(0::real, ARRAY[1536])::vector,
    '[{"page":1,"start":0,"end":7,"quote_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}]'::jsonb
);
SQL
docker exec "$postgres_container" psql --username postgres --dbname cardrag \
    --set ON_ERROR_STOP=1 --command 'VACUUM public.evidence' >/dev/null

query() {
    docker exec "$postgres_container" psql --username postgres --dbname cardrag \
        --tuples-only --no-align --set ON_ERROR_STOP=1 --command "$1"
}

test "$(query 'SHOW server_version_num')" = 170010
test "$(query "SELECT extversion FROM pg_extension WHERE extname='vector'")" = 0.8.2
test "$(query "SELECT string_agg(version::text, ',' ORDER BY version) FROM schema_migrations")" = \
    "$(seq -s, 1 14)"
test "$(query "SELECT count(*) FROM evidence WHERE evidence_id='old-evidence'")" = 1
before_node=$(query "SELECT relfilenode FROM pg_class WHERE oid='evidence_vector_idx'::regclass")

# Exercise the production minor-image transition: cleanly stop the real
# PostgreSQL 17.10/pgvector 0.8.2 server, then attach that unchanged PGDATA to
# the pinned PostgreSQL 17.11/pgvector 0.8.6 image. Never start the old image
# against this volume again after the extension upgrade.
docker stop --time 30 "$postgres_container" >/dev/null
docker rm "$postgres_container" >/dev/null
docker run --detach --name "$postgres_container" --network "$network" \
    --env POSTGRES_PASSWORD="$admin_password" \
    --env POSTGRES_USER=postgres --env POSTGRES_DB=cardrag \
    --volume "$postgres_volume:/var/lib/postgresql/data" \
    "$postgres_image" >/dev/null
ready=false
attempt=0
while [ "$attempt" -lt 45 ]; do
    if docker exec "$postgres_container" \
        pg_isready --username postgres --dbname cardrag >/dev/null 2>&1; then
        ready=true
        break
    fi
    attempt=$((attempt + 1))
    sleep 1
done
if [ "$ready" != true ]; then
    docker logs "$postgres_container" >&2 || true
    exit 1
fi
test "$(query 'SHOW server_version_num')" = 170011
test "$(query "SELECT extversion FROM pg_extension WHERE extname='vector'")" = 0.8.2
test "$(query "SELECT count(*) FROM evidence WHERE evidence_id='old-evidence'")" = 1

run_upgrade() {
    enabled=$1
    transition_id=$2
    docker run --rm --read-only --user 999:999 --cap-drop ALL \
        --security-opt no-new-privileges --network "$network" \
        --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m,uid=999,gid=999,mode=0700 \
        --env CARDRAG_POSTGRES_ADMIN_HOST="$postgres_container" \
        --env CARDRAG_POSTGRES_ADMIN_PORT=5432 \
        --env CARDRAG_POSTGRES_ADMIN_USER=postgres \
        --env CARDRAG_POSTGRES_ADMIN_PASSWORD_FILE=/run/secrets/postgres_admin_password \
        --env CARDRAG_POSTGRES_EXTENSION_UPGRADE_ENABLED="$enabled" \
        --env CARDRAG_POSTGRES_EXTENSION_UPGRADE_ID="$transition_id" \
        --volume "$secret_file:/run/secrets/postgres_admin_password:ro" \
        --volume "$upgrade_script:/opt/cardrag-postgres/upgrade-vector.sh:ro" \
        --entrypoint /opt/cardrag-postgres/upgrade-vector.sh "$postgres_image"
}

if run_upgrade false READY-NOT-SET >/dev/null 2>&1; then
    echo "disabled extension gate changed an old pgvector database" >&2
    exit 1
fi
test "$(query "SELECT extversion FROM pg_extension WHERE extname='vector'")" = 0.8.2

# A concurrent admin session is also a writer-capable session. The gate must
# fail closed rather than racing it; the normal postgres healthcheck never
# connects to the cardrag database.
docker exec "$postgres_container" psql --username postgres --dbname cardrag \
    --command 'SELECT pg_sleep(3)' >/dev/null &
session_pid=$!
session_visible=false
attempt=0
while [ "$attempt" -lt 20 ]; do
    if [ "$(query "SELECT count(*) FROM pg_stat_activity WHERE datname='cardrag' AND pid <> pg_backend_pid()")" != 0 ]; then
        session_visible=true
        break
    fi
    attempt=$((attempt + 1))
    sleep 0.1
done
if [ "$session_visible" != true ]; then
    echo "concurrent PostgreSQL fixture session did not appear" >&2
    exit 1
fi
if run_upgrade true integration-race-$suffix >/dev/null 2>&1; then
    echo "extension gate raced a concurrent CardRAG database session" >&2
    exit 1
fi
wait "$session_pid"
test "$(query "SELECT extversion FROM pg_extension WHERE extname='vector'")" = 0.8.2

run_upgrade true integration-$suffix >/dev/null
test "$(query "SELECT extversion FROM pg_extension WHERE extname='vector'")" = 0.8.6
after_node=$(query "SELECT relfilenode FROM pg_class WHERE oid='evidence_vector_idx'::regclass")
test "$after_node" != "$before_node"

migration15=$migration_root/015_pgvector_086.sql
migration15_checksum=$(sha256sum "$migration15" | awk '{print $1}')
docker exec --interactive --env PGPASSWORD="$cardrag_password" \
    "$postgres_container" psql --no-password --no-psqlrc \
    --username cardrag --dbname cardrag --set ON_ERROR_STOP=1 \
    >/dev/null <"$migration15"
docker exec --env PGPASSWORD="$cardrag_password" "$postgres_container" \
    psql --no-password --no-psqlrc --username cardrag --dbname cardrag \
    --set ON_ERROR_STOP=1 --command \
    "INSERT INTO schema_migrations(version, name, checksum) VALUES
     (15, '015_pgvector_086.sql', '$migration15_checksum')" >/dev/null
test "$(query "SELECT string_agg(version::text, ',' ORDER BY version) FROM schema_migrations")" = \
    "$(seq -s, 1 15)"
test "$(query 'SELECT checksum FROM schema_migrations WHERE version=15')" = \
    "$migration15_checksum"
test "$(query "SELECT count(*) FROM evidence WHERE evidence_id='old-evidence'")" = 1
test "$(query "SELECT evidence_id FROM evidence
               ORDER BY embedding <=> array_fill(0::real, ARRAY[1536])::vector LIMIT 1")" = \
    old-evidence
test "$(query "SELECT indisvalid::text || ':' || indisready::text
               FROM pg_index WHERE indexrelid='evidence_vector_idx'::regclass")" = true:true

# Once upgraded, the disabled gate is a read-only verifier and must not rebuild
# the index on every ordinary Stack restart.
run_upgrade false READY-NOT-SET >/dev/null
test "$(query "SELECT relfilenode FROM pg_class WHERE oid='evidence_vector_idx'::regclass")" = \
    "$after_node"

echo "PostgreSQL extension upgrade integration passed"
