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
    '015_pgvector_086.sql' \
    "ALTER EXTENSION vector UPDATE TO ''0.8.6''" \
    'REINDEX INDEX public.evidence_vector_idx' \
    '--single-transaction' \
    'require_schema_inventory "$expected15"' \
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

postgres_image=${CARDRAG_TRANSITION_TEST_POSTGRES_IMAGE:-pgvector/pgvector:0.8.6-pg17-bookworm@sha256:cf134a767f474095eeba57e0117be8e568e011a63f33fbf252f14c9b760f8e6f}
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
transition_id=integration-$suffix
package_name=schema13-safety-$transition_id
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
    --command "CREATE EXTENSION vector;
               UPDATE pg_extension SET extversion='0.8.2' WHERE extname='vector'" \
    >/dev/null
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
pre_upgrade_index_node=$(query_cardrag \
    "SELECT relfilenode FROM pg_class WHERE oid='public.evidence_vector_idx'::regclass")

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
        --env CARDRAG_SCHEMA13_TRANSITION_ID="$transition_id" \
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
assert m["schema_version"] == "cardrag-schema13-safety.v2"
assert m["source_schema_max"] == 13
assert m["databases"] == ["cardrag", "keycloak"]
assert m["source_identity"]["system_identifier"].isdigit()
assert [item["name"] for item in m["source_identity"]["databases"]] == ["cardrag", "keycloak"]
assert all(item["oid"] > 0 for item in m["source_identity"]["databases"])
assert r == {
    "schema_version": "cardrag-schema13-safety-ready.v2",
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
expected15=$(seq -s, 1 15)
actual15=$(query_cardrag "SELECT string_agg(version::text, ',' ORDER BY version) FROM schema_migrations")
if [ "$actual15" != "$expected15" ]; then
    echo "controlled upgrade did not produce the exact 1 through 15 inventory" >&2
    exit 1
fi
expected_migration15_checksum=$(sha256sum \
    "$migration_root/015_pgvector_086.sql" | awk '{print $1}')
actual_migration15_checksum=$(query_cardrag \
    'SELECT checksum FROM schema_migrations WHERE version=15')
if [ "$actual_migration15_checksum" != "$expected_migration15_checksum" ]; then
    echo "migration 015 checksum differs from the trusted migration file" >&2
    exit 1
fi
if [ "$(query_cardrag "SELECT extversion FROM pg_extension WHERE extname='vector'")" != 0.8.6 ]; then
    echo "controlled upgrade did not install pgvector 0.8.6" >&2
    exit 1
fi
post_upgrade_index_node=$(query_cardrag \
    "SELECT relfilenode FROM pg_class WHERE oid='public.evidence_vector_idx'::regclass")
if [ "$post_upgrade_index_node" = "$pre_upgrade_index_node" ]; then
    echo "controlled upgrade did not rebuild the HNSW index" >&2
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
if [ "$(query_cardrag "SELECT string_agg(version::text, ',' ORDER BY version) FROM schema_migrations")" != "$expected15" ]; then
    echo "transition rerun changed the exact migration inventory" >&2
    exit 1
fi

# Rebuild a schema-14 source from the sealed pre-upgrade dump and prove the
# same bridge backs it up before applying only vector 0.8.6/migration 015.
schema13_package=$package
docker exec "$postgres_container" psql --username postgres --dbname postgres \
    --set ON_ERROR_STOP=1 --command \
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity
      WHERE datname='cardrag' AND pid <> pg_backend_pid()" >/dev/null
docker exec "$postgres_container" psql --username postgres --dbname postgres \
    --set ON_ERROR_STOP=1 --command 'DROP DATABASE cardrag' >/dev/null
docker exec "$postgres_container" psql --username postgres --dbname postgres \
    --set ON_ERROR_STOP=1 --command 'CREATE DATABASE cardrag OWNER cardrag' >/dev/null
docker run --rm --user 10001:10001 --network "$network" \
    --env PGPASSWORD="$admin_password" --volume "$schema13_package:/package:ro" \
    --entrypoint pg_restore "$transition_image" --no-password \
    --host "$postgres_container" --port 5432 --username postgres \
    --dbname cardrag /package/database/cardrag.dump >/dev/null
docker exec "$postgres_container" psql --username postgres --dbname cardrag \
    --set ON_ERROR_STOP=1 --command \
    "UPDATE pg_extension SET extversion='0.8.2' WHERE extname='vector'" >/dev/null
migration14=$migration_root/014_legacy_import_and_portability.sql
migration14_checksum=$(sha256sum "$migration14" | awk '{print $1}')
docker exec --interactive --env PGPASSWORD="$cardrag_password" \
    "$postgres_container" psql --no-password --no-psqlrc \
    --username cardrag --dbname cardrag --set ON_ERROR_STOP=1 \
    >/dev/null <"$migration14"
docker exec --env PGPASSWORD="$cardrag_password" "$postgres_container" \
    psql --no-password --no-psqlrc --username cardrag --dbname cardrag \
    --set ON_ERROR_STOP=1 --command \
    "INSERT INTO schema_migrations(version, name, checksum) VALUES
     (14, '014_legacy_import_and_portability.sql', '$migration14_checksum')" \
    >/dev/null
expected14=$(seq -s, 1 14)
test "$(query_cardrag "SELECT string_agg(version::text, ',' ORDER BY version) FROM schema_migrations")" = \
    "$expected14"

# The original transition ID is sealed with a schema-13 dump. It must never be
# reused to authorize mutation of this schema-14 source.
if run_transition backup >"$temporary_root/mismatched-backup.log" 2>&1; then
    echo "schema-13 package was reused for a schema-14 backup" >&2
    exit 1
fi
grep -F 'does not match the live pre-upgrade schema' \
    "$temporary_root/mismatched-backup.log" >/dev/null
if run_transition upgrade >"$temporary_root/mismatched-upgrade.log" 2>&1; then
    echo "schema-13 package authorized a schema-14 upgrade" >&2
    exit 1
fi
grep -F 'does not match the live pre-upgrade schema' \
    "$temporary_root/mismatched-upgrade.log" >/dev/null
test "$(query_cardrag "SELECT string_agg(version::text, ',' ORDER BY version) FROM schema_migrations")" = \
    "$expected14"
test "$(query_cardrag "SELECT extversion FROM pg_extension WHERE extname='vector'")" = 0.8.2

transition_id=schema14-integration-$suffix
package_name=schema13-safety-$transition_id
package=$archive_root/$package_name
run_transition backup >/dev/null
docker run --rm --user 10001:10001 --volume "$package:/package:ro" \
    --entrypoint python3 "$transition_image" -c \
    'import json,pathlib; assert json.loads(pathlib.Path("/package/transition-manifest.json").read_text())["source_schema_max"] == 14'

# Restore the same schema-14 dump into a newly created CardRAG database. The
# schema and contents match, but the DB OID changes, so the sealed transition ID
# must not authorize either backup reuse or mutation of this replacement DB.
schema14_package=$package
original_cardrag_oid=$(docker exec "$postgres_container" psql --username postgres \
    --dbname postgres --tuples-only --no-align --command \
    "SELECT oid FROM pg_database WHERE datname='cardrag'")
docker exec "$postgres_container" psql --username postgres --dbname postgres \
    --set ON_ERROR_STOP=1 --command \
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity
      WHERE datname='cardrag' AND pid <> pg_backend_pid()" >/dev/null
docker exec "$postgres_container" psql --username postgres --dbname postgres \
    --set ON_ERROR_STOP=1 --command 'DROP DATABASE cardrag' >/dev/null
docker exec "$postgres_container" psql --username postgres --dbname postgres \
    --set ON_ERROR_STOP=1 --command 'CREATE DATABASE cardrag OWNER cardrag' >/dev/null
docker run --rm --user 10001:10001 --network "$network" \
    --env PGPASSWORD="$admin_password" --volume "$schema14_package:/package:ro" \
    --entrypoint pg_restore "$transition_image" --no-password \
    --host "$postgres_container" --port 5432 --username postgres \
    --dbname cardrag /package/database/cardrag.dump >/dev/null
docker exec "$postgres_container" psql --username postgres --dbname cardrag \
    --set ON_ERROR_STOP=1 --command \
    "UPDATE pg_extension SET extversion='0.8.2' WHERE extname='vector'" >/dev/null
replacement_cardrag_oid=$(docker exec "$postgres_container" psql --username postgres \
    --dbname postgres --tuples-only --no-align --command \
    "SELECT oid FROM pg_database WHERE datname='cardrag'")
test "$replacement_cardrag_oid" != "$original_cardrag_oid"
test "$(query_cardrag "SELECT string_agg(version::text, ',' ORDER BY version) FROM schema_migrations")" = \
    "$expected14"
if run_transition backup >"$temporary_root/mismatched-identity-backup.log" 2>&1; then
    echo "schema-14 package was reused for a replacement database" >&2
    exit 1
fi
grep -F 'does not match the live database identity' \
    "$temporary_root/mismatched-identity-backup.log" >/dev/null
if run_transition upgrade >"$temporary_root/mismatched-identity-upgrade.log" 2>&1; then
    echo "schema-14 package authorized a replacement database upgrade" >&2
    exit 1
fi
grep -F 'does not match the live database identity' \
    "$temporary_root/mismatched-identity-upgrade.log" >/dev/null
test "$(query_cardrag "SELECT extversion FROM pg_extension WHERE extname='vector'")" = 0.8.2

transition_id=schema14-replacement-integration-$suffix
package_name=schema13-safety-$transition_id
package=$archive_root/$package_name
pre_upgrade_index_node=$(query_cardrag \
    "SELECT relfilenode FROM pg_class WHERE oid='public.evidence_vector_idx'::regclass")
run_transition backup >/dev/null
run_transition upgrade >/dev/null
test "$(query_cardrag "SELECT string_agg(version::text, ',' ORDER BY version) FROM schema_migrations")" = \
    "$expected15"
test "$(query_cardrag "SELECT extversion FROM pg_extension WHERE extname='vector'")" = 0.8.6
post_upgrade_index_node=$(query_cardrag \
    "SELECT relfilenode FROM pg_class WHERE oid='public.evidence_vector_idx'::regclass")
test "$post_upgrade_index_node" != "$pre_upgrade_index_node"
run_transition backup >/dev/null

echo "schema-13/schema-14 to 15 and pgvector 0.8.6 transition integration passed"
