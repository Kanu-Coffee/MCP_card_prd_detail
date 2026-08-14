#!/bin/sh
set -eu

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
transition_script=$repository_root/deploy/portainer/scripts/schema13-transition.sh
migration_root=$repository_root/src/cardrag/db/migrations

if [ ! -f "$transition_script" ]; then
    echo "schema-13 transition script is missing" >&2
    exit 1
fi
sh -n "$transition_script"
for required_contract in \
    'backup|upgrade' \
    '--dbname cardrag --format custom' \
    '--dbname keycloak --format custom' \
    'pg_restore --list "$package/database/cardrag.dump"' \
    'pg_restore --list "$package/database/keycloak.dump"' \
    '014_legacy_import_and_portability.sql' \
    '--single-transaction' \
    'require_schema_inventory "$expected14"' \
    'mv "$incoming" "$package"'
do
    if ! grep -Fq -- "$required_contract" "$transition_script"; then
        echo "schema-13 transition contract is missing: $required_contract" >&2
        exit 1
    fi
done

transition_image=${CARDRAG_TRANSITION_TEST_IMAGE:-}
if [ -z "$transition_image" ]; then
    echo "schema-13 transition integration skipped (CARDRAG_TRANSITION_TEST_IMAGE is unset)"
    exit 0
fi
if ! command -v docker >/dev/null 2>&1; then
    echo "Docker is required when CARDRAG_TRANSITION_TEST_IMAGE is set" >&2
    exit 1
fi
if ! docker info >/dev/null 2>&1; then
    echo "Docker daemon is unavailable" >&2
    exit 1
fi
if ! docker image inspect "$transition_image" >/dev/null 2>&1; then
    echo "transition test image is unavailable: $transition_image" >&2
    exit 1
fi

postgres_image=${CARDRAG_TRANSITION_TEST_POSTGRES_IMAGE:-pgvector/pgvector:0.8.2-pg17-bookworm@sha256:feb68f4f15446397d8cac7f4fe48fe4586de83160d1fc48b46283312d1a33966}
if ! docker image inspect "$postgres_image" >/dev/null 2>&1; then
    docker pull "$postgres_image" >/dev/null
fi

temporary_root=$(mktemp -d)
suffix=$$
network=cardrag-schema13-transition-$suffix
postgres_container=cardrag-schema13-postgres-$suffix
archive_root=$temporary_root/archive
secret_root=$temporary_root/secrets
transition_fixture=$temporary_root/schema13-transition.sh
transition_migration_root=$temporary_root/migrations
package_name=schema13-safety-integration-$suffix
package=$archive_root/$package_name
admin_password=admin-transition-fixture
cardrag_password=cardrag-transition-fixture
keycloak_password=keycloak-transition-fixture

cleanup() {
    docker rm --force "$postgres_container" >/dev/null 2>&1 || true
    docker network rm "$network" >/dev/null 2>&1 || true
    if [ -n "$transition_image" ] && [ -d "$temporary_root" ]; then
        docker run --rm --user 0:0 \
            --volume "$temporary_root:/cleanup" \
            --entrypoint /bin/sh "$transition_image" \
            -c 'chmod -R ugo+rwx /cleanup' >/dev/null 2>&1 || true
    fi
    rm -rf "$temporary_root"
}
trap cleanup EXIT HUP INT TERM

mkdir -p "$archive_root" "$secret_root" "$transition_migration_root"
chmod 0777 "$archive_root"
cp "$transition_script" "$transition_fixture"
chmod 0444 "$transition_fixture"
cp "$migration_root"/[0-9][0-9][0-9]_*.sql "$transition_migration_root/"
chmod 0555 "$transition_migration_root"
chmod 0444 "$transition_migration_root"/*.sql
printf '%s\n' "$admin_password" >"$secret_root/postgres-admin-password"
printf '%s\n' "$cardrag_password" >"$secret_root/cardrag-password"
chmod 0444 "$secret_root/postgres-admin-password" "$secret_root/cardrag-password"

docker network create "$network" >/dev/null
docker run --detach \
    --name "$postgres_container" \
    --network "$network" \
    --env POSTGRES_PASSWORD="$admin_password" \
    --env POSTGRES_USER=postgres \
    --env POSTGRES_DB=postgres \
    "$postgres_image" >/dev/null

postgres_ready=false
attempt=0
while [ "$attempt" -lt 45 ]; do
    if docker exec "$postgres_container" \
        pg_isready --username postgres --dbname postgres >/dev/null 2>&1; then
        postgres_ready=true
        break
    fi
    attempt=$((attempt + 1))
    sleep 1
done
if [ "$postgres_ready" != true ]; then
    docker logs "$postgres_container" >&2 || true
    echo "PostgreSQL fixture did not become ready" >&2
    exit 1
fi

docker exec --interactive "$postgres_container" \
    psql --username postgres --dbname postgres --set ON_ERROR_STOP=1 >/dev/null <<SQL
CREATE ROLE cardrag LOGIN PASSWORD '$cardrag_password';
CREATE ROLE cardrag_worker LOGIN NOINHERIT PASSWORD 'worker-transition-fixture';
CREATE ROLE cardrag_mcp LOGIN NOINHERIT PASSWORD 'mcp-transition-fixture';
CREATE ROLE keycloak LOGIN PASSWORD '$keycloak_password';
CREATE DATABASE cardrag OWNER cardrag;
CREATE DATABASE keycloak OWNER keycloak;
GRANT CONNECT ON DATABASE cardrag TO cardrag_worker, cardrag_mcp;
SQL
docker exec "$postgres_container" \
    psql --username postgres --dbname cardrag --set ON_ERROR_STOP=1 \
    --command 'CREATE EXTENSION IF NOT EXISTS vector' >/dev/null
docker exec "$postgres_container" \
    psql --username postgres --dbname cardrag --set ON_ERROR_STOP=1 \
    --command 'GRANT USAGE, CREATE ON SCHEMA public TO cardrag' >/dev/null

version=1
while [ "$version" -le 13 ]; do
    prefix=$(printf '%03d_' "$version")
    set -- "$migration_root/$prefix"*.sql
    if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then
        echo "expected exactly one migration fixture for version $version" >&2
        exit 1
    fi
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
        "INSERT INTO schema_migrations(version, name, checksum) VALUES ($version, '$migration_name', '$migration_checksum')" \
        >/dev/null
    version=$((version + 1))
done

legacy_generation_id=gen-schema13-transition-fixture
legacy_root=/srv/legacy/runtime/generations/generations/$legacy_generation_id
docker exec --env PGPASSWORD="$cardrag_password" "$postgres_container" \
    psql --no-password --no-psqlrc --username cardrag --dbname cardrag \
    --set ON_ERROR_STOP=1 --command \
    "INSERT INTO generations(
        generation_id, state, manifest_sha256, root_uri, schema_version,
        embedding_provider, embedding_model, embedding_dimension
     ) VALUES (
        '$legacy_generation_id', 'ready', repeat('a', 64), '$legacy_root',
        'fixture-v1', 'fixture', 'fixture-embedding', 1536
     )" >/dev/null
docker exec --env PGPASSWORD="$keycloak_password" "$postgres_container" \
    psql --no-password --no-psqlrc --username keycloak --dbname keycloak \
    --set ON_ERROR_STOP=1 --command \
    "CREATE TABLE realm_fixture(marker text PRIMARY KEY);
     INSERT INTO realm_fixture(marker) VALUES ('keycloak-safety-fixture')" \
    >/dev/null

query_cardrag() {
    docker exec --env PGPASSWORD="$cardrag_password" "$postgres_container" \
        psql --no-password --no-psqlrc --username cardrag --dbname cardrag \
        --set ON_ERROR_STOP=1 --tuples-only --no-align --command "$1"
}

expected13=$(seq -s, 1 13)
if [ "$(query_cardrag "SELECT string_agg(version::text, ',' ORDER BY version) FROM schema_migrations")" != "$expected13" ]; then
    echo "schema fixture is not exactly migrations 1 through 13" >&2
    exit 1
fi
if [ "$(query_cardrag "SELECT count(*) FROM information_schema.columns WHERE table_schema='public' AND table_name='generations' AND column_name='root_key'")" != 0 ]; then
    echo "schema-13 fixture unexpectedly has generations.root_key" >&2
    exit 1
fi

run_transition() {
    operation=$1
    docker run --rm --read-only \
        --user 10001:10001 \
        --cap-drop ALL \
        --security-opt no-new-privileges \
        --network "$network" \
        --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m,uid=10001,gid=10001,mode=0700 \
        --env CARDRAG_ARCHIVE_ROOT=/mnt/cardrag-archive \
        --env CARDRAG_TRANSITION_MIGRATION_ROOT=/opt/cardrag-transition/migrations \
        --env CARDRAG_SCHEMA13_TRANSITION_ID="integration-$suffix" \
        --env CARDRAG_SCHEMA13_TRANSITION_RESUME=false \
        --env CARDRAG_POSTGRES_ADMIN_HOST="$postgres_container" \
        --env CARDRAG_POSTGRES_ADMIN_PORT=5432 \
        --env CARDRAG_POSTGRES_ADMIN_USER=postgres \
        --env CARDRAG_POSTGRES_ADMIN_PASSWORD_FILE=/run/secrets/postgres_admin_password \
        --env CARDRAG_DB_PASSWORD_FILE=/run/secrets/cardrag_db_password \
        --volume "$archive_root:/mnt/cardrag-archive" \
        --volume "$transition_migration_root:/opt/cardrag-transition/migrations:ro" \
        --volume "$transition_fixture:/opt/cardrag-transition/schema13-transition.sh:ro" \
        --volume "$secret_root/postgres-admin-password:/run/secrets/postgres_admin_password:ro" \
        --volume "$secret_root/cardrag-password:/run/secrets/cardrag_db_password:ro" \
        --entrypoint /bin/sh \
        "$transition_image" /opt/cardrag-transition/schema13-transition.sh "$operation"
}

run_transition backup >/dev/null
if [ ! -d "$package" ]; then
    echo "schema-13 safety package was not atomically published" >&2
    exit 1
fi

docker run --rm --user 10001:10001 \
    --volume "$package:/package:ro" --entrypoint /bin/sh "$transition_image" \
    -c 'cd /package && sha256sum --check --strict checksums.sha256 >/dev/null' \
    >/dev/null
docker run --rm --user 10001:10001 \
    --volume "$package:/package:ro" --entrypoint python3 "$transition_image" \
    -c 'import hashlib,json,pathlib,sys
p=pathlib.Path("/package")
m=json.loads((p/"transition-manifest.json").read_text())
r=json.loads((p/"READY").read_text())
assert m["schema_version"] == "cardrag-schema13-safety.v1"
assert m["source_schema_max"] == 13
assert m["databases"] == ["cardrag", "keycloak"]
assert r == {
    "schema_version": "cardrag-schema13-safety-ready.v1",
    "transition_id": m["transition_id"],
    "manifest_sha256": hashlib.sha256((p/"transition-manifest.json").read_bytes()).hexdigest(),
}'

cardrag_dump_list=$(docker run --rm --user 10001:10001 \
    --volume "$package:/package:ro" --entrypoint pg_restore "$transition_image" \
    --list /package/database/cardrag.dump)
keycloak_dump_list=$(docker run --rm --user 10001:10001 \
    --volume "$package:/package:ro" --entrypoint pg_restore "$transition_image" \
    --list /package/database/keycloak.dump)
printf '%s\n' "$cardrag_dump_list" | grep -Eq 'TABLE( DATA)? public (generations|schema_migrations)'
printf '%s\n' "$keycloak_dump_list" | grep -Eq 'TABLE( DATA)? public realm_fixture'

docker exec "$postgres_container" \
    psql --username postgres --dbname postgres --set ON_ERROR_STOP=1 \
    --command 'CREATE DATABASE cardrag_restore_check' >/dev/null
docker exec "$postgres_container" \
    psql --username postgres --dbname postgres --set ON_ERROR_STOP=1 \
    --command 'CREATE DATABASE keycloak_restore_check' >/dev/null
docker run --rm --user 10001:10001 --network "$network" \
    --env PGPASSWORD="$admin_password" --volume "$package:/package:ro" \
    --entrypoint pg_restore "$transition_image" --no-password \
    --host "$postgres_container" --port 5432 --username postgres \
    --dbname cardrag_restore_check /package/database/cardrag.dump >/dev/null
docker run --rm --user 10001:10001 --network "$network" \
    --env PGPASSWORD="$admin_password" --volume "$package:/package:ro" \
    --entrypoint pg_restore "$transition_image" --no-password \
    --host "$postgres_container" --port 5432 --username postgres \
    --dbname keycloak_restore_check /package/database/keycloak.dump >/dev/null

restored_inventory=$(docker exec "$postgres_container" \
    psql --username postgres --dbname cardrag_restore_check \
    --tuples-only --no-align --command \
    "SELECT string_agg(version::text, ',' ORDER BY version) FROM schema_migrations")
if [ "$restored_inventory" != "$expected13" ]; then
    echo "restored safety dump does not contain the exact schema-13 inventory" >&2
    exit 1
fi
restored_root=$(docker exec "$postgres_container" \
    psql --username postgres --dbname cardrag_restore_check \
    --tuples-only --no-align --command \
    "SELECT root_uri FROM generations WHERE generation_id='$legacy_generation_id'")
if [ "$restored_root" != "$legacy_root" ]; then
    echo "schema-13 safety dump did not preserve the pre-upgrade generation root" >&2
    exit 1
fi
restored_keycloak=$(docker exec "$postgres_container" \
    psql --username postgres --dbname keycloak_restore_check \
    --tuples-only --no-align --command 'SELECT marker FROM realm_fixture')
if [ "$restored_keycloak" != keycloak-safety-fixture ]; then
    echo "Keycloak safety dump did not restore its fixture data" >&2
    exit 1
fi

package_fingerprint_before=$(docker run --rm --user 10001:10001 \
    --volume "$package:/package:ro" --entrypoint python3 "$transition_image" \
    -c 'import hashlib,pathlib
h=hashlib.sha256()
for p in sorted(x for x in pathlib.Path("/package").rglob("*") if x.is_file()):
    h.update(str(p.relative_to("/package")).encode()+b"\0")
    h.update(hashlib.sha256(p.read_bytes()).digest())
print(h.hexdigest())')

run_transition upgrade >/dev/null
expected14=$(seq -s, 1 14)
actual14=$(query_cardrag "SELECT string_agg(version::text, ',' ORDER BY version) FROM schema_migrations")
if [ "$actual14" != "$expected14" ]; then
    echo "controlled upgrade did not produce the exact 1 through 14 inventory" >&2
    exit 1
fi
expected_migration14_checksum=$(sha256sum \
    "$migration_root/014_legacy_import_and_portability.sql" | awk '{print $1}')
actual_migration14_checksum=$(query_cardrag \
    'SELECT checksum FROM schema_migrations WHERE version=14')
if [ "$actual_migration14_checksum" != "$expected_migration14_checksum" ]; then
    echo "migration 014 checksum differs from the trusted migration file" >&2
    exit 1
fi
portable_root=generations/$legacy_generation_id
actual_roots=$(query_cardrag \
    "SELECT root_key || '|' || root_uri FROM generations WHERE generation_id='$legacy_generation_id'")
if [ "$actual_roots" != "$portable_root|$portable_root" ]; then
    echo "migration 014 did not canonicalize the legacy generation root" >&2
    exit 1
fi

run_transition upgrade >/dev/null
run_transition backup >/dev/null
package_fingerprint_after=$(docker run --rm --user 10001:10001 \
    --volume "$package:/package:ro" --entrypoint python3 "$transition_image" \
    -c 'import hashlib,pathlib
h=hashlib.sha256()
for p in sorted(x for x in pathlib.Path("/package").rglob("*") if x.is_file()):
    h.update(str(p.relative_to("/package")).encode()+b"\0")
    h.update(hashlib.sha256(p.read_bytes()).digest())
print(h.hexdigest())')
if [ "$package_fingerprint_after" != "$package_fingerprint_before" ]; then
    echo "verified transition rerun mutated the sealed schema-13 safety package" >&2
    exit 1
fi
if [ "$(query_cardrag "SELECT string_agg(version::text, ',' ORDER BY version) FROM schema_migrations")" != "$expected14" ]; then
    echo "transition rerun changed the exact migration inventory" >&2
    exit 1
fi

echo "schema-13 to 14 transition integration passed"
