#!/bin/sh
set -eu

umask 027

operation=${1:-}
archive_root=${CARDRAG_ARCHIVE_ROOT:-/mnt/cardrag-archive}
migration_root=${CARDRAG_TRANSITION_MIGRATION_ROOT:-/opt/cardrag-transition/migrations}
transition_id=${CARDRAG_SCHEMA13_TRANSITION_ID:-}
resume=${CARDRAG_SCHEMA13_TRANSITION_RESUME:-false}
postgres_host=${CARDRAG_POSTGRES_ADMIN_HOST:-postgres}
postgres_port=${CARDRAG_POSTGRES_ADMIN_PORT:-5432}
postgres_user=${CARDRAG_POSTGRES_ADMIN_USER:-postgres}
admin_password_file=${CARDRAG_POSTGRES_ADMIN_PASSWORD_FILE:-/run/secrets/postgres_admin_password}

case "$operation" in
    backup|upgrade) ;;
    *)
        echo "usage: schema13-transition.sh backup|upgrade" >&2
        exit 64
        ;;
esac
case "$transition_id" in
    ''|READY-NOT-SET|*[!A-Za-z0-9_.-]*)
        echo "CARDRAG_SCHEMA13_TRANSITION_ID must be an explicit safe identifier" >&2
        exit 64
        ;;
esac
case "$resume" in
    true|false) ;;
    *)
        echo "CARDRAG_SCHEMA13_TRANSITION_RESUME must be true or false" >&2
        exit 64
        ;;
esac
for root in "$archive_root" "$migration_root"; do
    if [ ! -d "$root" ] || [ -L "$root" ]; then
        echo "transition root must be an existing non-symlink directory" >&2
        exit 78
    fi
done
for command in pg_dump pg_restore psql python3 sha256sum; do
    if ! command -v "$command" >/dev/null 2>&1; then
        echo "required transition command is missing: $command" >&2
        exit 69
    fi
done

read_secret() {
    path=$1
    if [ ! -f "$path" ] || [ -L "$path" ] || [ ! -r "$path" ]; then
        echo "required transition secret is unavailable" >&2
        exit 77
    fi
    value=$(tr -d '\r\n' <"$path")
    if [ -z "$value" ]; then
        echo "required transition secret is empty" >&2
        exit 77
    fi
    printf '%s' "$value"
}

escape_pgpass() {
    sed 's/\\/\\\\/g; s/:/\\:/g'
}

admin_password=$(read_secret "$admin_password_file")
pgpass=/tmp/cardrag-schema13-transition.pgpass
{
    printf '%s:%s:*:%s:' "$postgres_host" "$postgres_port" "$postgres_user"
    printf '%s' "$admin_password" | escape_pgpass
    printf '\n'
} >"$pgpass"
chmod 0600 "$pgpass"
unset admin_password
export PGPASSFILE=$pgpass
trap 'rm -f "$pgpass"' EXIT HUP INT TERM

psql_admin() {
    psql --no-password --host "$postgres_host" --port "$postgres_port" \
        --username "$postgres_user" --dbname postgres --no-psqlrc \
        --set ON_ERROR_STOP=1 "$@"
}

psql_cardrag_as_admin() {
    psql --no-password --host "$postgres_host" --port "$postgres_port" \
        --username "$postgres_user" --dbname cardrag --no-psqlrc \
        --set ON_ERROR_STOP=1 "$@"
}

client_version=$(pg_dump --version | sed -n \
    's/^pg_dump (PostgreSQL) \([0-9][0-9]*\.[0-9][0-9]*\).*/\1/p')
server_number=$(psql_admin --tuples-only --no-align --command 'SHOW server_version_num')
case "$server_number" in
    ''|*[!0-9]*)
        echo "PostgreSQL server version could not be determined" >&2
        exit 70
        ;;
esac
if [ "$client_version" != 17.11 ] || [ "$server_number" != 170011 ]; then
    echo "schema transition requires exact PostgreSQL 17.11 client and server" >&2
    exit 65
fi

active_sessions=$(psql_admin --tuples-only --no-align --command \
    "SELECT count(*) FROM pg_stat_activity
     WHERE datname IN ('cardrag','keycloak') AND pid <> pg_backend_pid()")
if [ "$active_sessions" != 0 ]; then
    echo "schema transition requires zero CardRAG/Keycloak database sessions" >&2
    exit 75
fi

expected_inventory() {
    maximum=$1
    output=$2
    : >"$output"
    version=1
    while [ "$version" -le "$maximum" ]; do
        prefix=$(printf '%03d_' "$version")
        set -- "$migration_root/$prefix"*.sql
        if [ "$#" -ne 1 ] || [ ! -f "$1" ] || [ -L "$1" ]; then
            echo "expected exactly one trusted migration for version $version" >&2
            exit 66
        fi
        name=${1##*/}
        checksum=$(sha256sum "$1" | awk '{print $1}')
        printf '%s\t%s\t%s\n' "$version" "$name" "$checksum" >>"$output"
        version=$((version + 1))
    done
}

database_inventory() {
    output=$1
    tab=$(printf '\t')
    psql_cardrag_as_admin --tuples-only --no-align --field-separator="$tab" \
        --command 'SELECT version, name, checksum FROM schema_migrations ORDER BY version' \
        >"$output"
}

database_identity() {
    output=$1
    system_identifier=$(psql_admin --tuples-only --no-align --command \
        'SELECT system_identifier::text FROM pg_control_system()')
    case "$system_identifier" in
        ''|*[!0-9]*)
            echo "PostgreSQL system identifier could not be determined" >&2
            exit 70
            ;;
    esac
    tab=$(printf '\t')
    {
        printf 'system_identifier\t%s\n' "$system_identifier"
        psql_admin --tuples-only --no-align --field-separator="$tab" --command \
            "SELECT datname, oid::text FROM pg_database
             WHERE datname IN ('cardrag', 'keycloak') ORDER BY datname"
    } >"$output"
    if [ "$(sed -n '2p' "$output" | cut -f1)" != cardrag ] || \
       [ "$(sed -n '3p' "$output" | cut -f1)" != keycloak ] || \
       [ "$(wc -l <"$output" | tr -d ' ')" != 3 ]; then
        echo "CardRAG/Keycloak database identity could not be determined" >&2
        exit 70
    fi
}

package_identity() {
    manifest=$1
    output=$2
    python3 - "$manifest" "$output" <<'PY'
import json
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text())
identity = manifest.get("source_identity")
if not isinstance(identity, dict) or set(identity) != {"system_identifier", "databases"}:
    raise SystemExit("legacy safety source identity is invalid")
system_identifier = identity["system_identifier"]
databases = identity["databases"]
if (
    not isinstance(system_identifier, str)
    or not system_identifier.isdigit()
    or int(system_identifier) <= 0
    or not isinstance(databases, list)
    or [item.get("name") for item in databases if isinstance(item, dict)]
       != ["cardrag", "keycloak"]
):
    raise SystemExit("legacy safety source identity is invalid")
for item in databases:
    if set(item) != {"name", "oid"} or not isinstance(item["oid"], int) or item["oid"] <= 0:
        raise SystemExit("legacy safety database identity is invalid")
Path(sys.argv[2]).write_text(
    "system_identifier\t" + system_identifier + "\n"
    + "".join(f'{item["name"]}\t{item["oid"]}\n' for item in databases)
)
PY
}

work=/tmp/cardrag-schema13-transition.$$
install -d -m 0700 "$work"
expected13=$work/expected13.tsv
expected14=$work/expected14.tsv
expected15=$work/expected15.tsv
actual=$work/actual.tsv
live_identity=$work/live-identity.tsv
sealed_identity=$work/sealed-identity.tsv
expected_inventory 13 "$expected13"
expected_inventory 14 "$expected14"
expected_inventory 15 "$expected15"

require_schema_inventory() {
    expected=$1
    label=$2
    database_inventory "$actual"
    if ! cmp -s "$expected" "$actual"; then
        echo "database migration inventory is not exactly $label" >&2
        exit 65
    fi
}

package=$archive_root/schema13-safety-$transition_id
incoming=$archive_root/.schema13-safety-$transition_id.incoming
owner=$archive_root/.schema13-safety-$transition_id.owner
owner_body=$(printf '%s\n%s' cardrag-schema13-transition-owner.v1 \
    "transition_id=$transition_id")

verify_owner() {
    if [ ! -f "$owner" ] || [ -L "$owner" ] || \
       [ "$(cat "$owner")" != "$owner_body" ] || \
       [ "$(stat -c '%u:%g' "$owner")" != "$(id -u):$(id -g)" ] || \
       [ "$(stat -c '%a' "$owner")" != 600 ]; then
        echo "schema-13 backup ownership record is missing or unsafe" >&2
        exit 73
    fi
}

fsync_archive_root() {
    python3 - "$archive_root" <<'PY'
import os
import sys
descriptor = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
}

verify_package() {
    if [ ! -d "$package" ] || [ -L "$package" ]; then
        echo "schema-13 safety package is missing or unsafe" >&2
        exit 78
    fi
    python3 - "$package" "$transition_id" <<'PY'
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
transition_id = sys.argv[2]
expected_root = {
    "database", "schema-migrations.tsv", "transition-manifest.json",
    "checksums.sha256", "READY",
}
if {path.name for path in root.iterdir()} != expected_root:
    raise SystemExit("schema-13 safety package inventory differs")
database = root / "database"
if database.is_symlink() or not database.is_dir():
    raise SystemExit("schema-13 database dump directory is unsafe")
if {path.name for path in database.iterdir()} != {"cardrag.dump", "keycloak.dump"}:
    raise SystemExit("schema-13 database dump inventory differs")
for path in root.rglob("*"):
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        raise SystemExit("symlink in schema-13 safety package")
    if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
        raise SystemExit("special file in schema-13 safety package")
for path in (root, *root.rglob("*")):
    metadata = path.lstat()
    if (metadata.st_uid, metadata.st_gid) != (os.getuid(), os.getgid()):
        raise SystemExit("schema-13 safety package owner differs")
    expected_mode = 0o550 if stat.S_ISDIR(metadata.st_mode) else 0o440
    if stat.S_IMODE(metadata.st_mode) != expected_mode:
        raise SystemExit("schema-13 safety package mode differs")
manifest_path = root / "transition-manifest.json"
manifest = json.loads(manifest_path.read_text())
source_identity = manifest.get("source_identity")
if (
    not isinstance(source_identity, dict)
    or set(source_identity) != {"system_identifier", "databases"}
    or not isinstance(source_identity.get("system_identifier"), str)
    or not source_identity["system_identifier"].isdigit()
    or int(source_identity["system_identifier"]) <= 0
    or not isinstance(source_identity.get("databases"), list)
    or [item.get("name") for item in source_identity["databases"] if isinstance(item, dict)]
       != ["cardrag", "keycloak"]
):
    raise SystemExit("legacy safety source identity is invalid")
for item in source_identity["databases"]:
    if set(item) != {"name", "oid"} or not isinstance(item["oid"], int) or item["oid"] <= 0:
        raise SystemExit("legacy safety database identity is invalid")
expected_files = []
for relative in (
    "database/cardrag.dump", "database/keycloak.dump", "schema-migrations.tsv"
):
    path = root / relative
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    expected_files.append({
        "path": relative,
        "sha256": digest.hexdigest(),
        "size_bytes": size,
    })
inventory_rows = (root / "schema-migrations.tsv").read_text().splitlines()
try:
    source_schema_max = int(inventory_rows[-1].split("\t", 1)[0])
except (IndexError, ValueError) as exc:
    raise SystemExit("legacy safety migration inventory is invalid") from exc
if source_schema_max not in (13, 14):
    raise SystemExit("legacy safety source schema must be 13 or 14")
expected_manifest = {
    "schema_version": "cardrag-schema13-safety.v2",
    "transition_id": transition_id,
    "postgres_major": 17,
    "source_schema_max": source_schema_max,
    "source_identity": source_identity,
    "databases": ["cardrag", "keycloak"],
    "files": expected_files,
}
if manifest != expected_manifest:
    raise SystemExit("schema-13 safety manifest differs")
manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
ready = json.loads((root / "READY").read_text())
if ready != {
    "schema_version": "cardrag-schema13-safety-ready.v2",
    "transition_id": transition_id,
    "manifest_sha256": manifest_sha,
}:
    raise SystemExit("schema-13 safety READY seal differs")
PY
    (CDPATH= cd -- "$package" && sha256sum --check --strict checksums.sha256 >/dev/null)
    pg_restore --list "$package/database/cardrag.dump" >/dev/null
    pg_restore --list "$package/database/keycloak.dump" >/dev/null
    if ! cmp -s "$expected13" "$package/schema-migrations.tsv" && \
       ! cmp -s "$expected14" "$package/schema-migrations.tsv"; then
        echo "legacy safety package migration inventory differs" >&2
        exit 74
    fi
}

if [ "$operation" = backup ]; then
    if [ -e "$package" ] || [ -L "$package" ]; then
        verify_package
        package_identity "$package/transition-manifest.json" "$sealed_identity"
        if [ -e "$owner" ] || [ -L "$owner" ]; then
            verify_owner
            rm -f -- "$owner"
            fsync_archive_root
        fi
        database_inventory "$actual"
        if cmp -s "$expected15" "$actual"; then
            database_identity "$live_identity"
            if ! cmp -s "$sealed_identity" "$live_identity"; then
                echo "sealed legacy backup does not match the live database identity" >&2
                exit 65
            fi
        elif { cmp -s "$expected13" "$actual" || cmp -s "$expected14" "$actual"; } && \
             cmp -s "$package/schema-migrations.tsv" "$actual"; then
            database_identity "$live_identity"
            if ! cmp -s "$sealed_identity" "$live_identity"; then
                echo "sealed legacy backup does not match the live database identity" >&2
                exit 65
            fi
        else
            echo "sealed legacy backup does not match the live pre-upgrade schema" >&2
            exit 65
        fi
        echo "schema-13 safety backup already verified: $transition_id"
        exit 0
    fi
    database_inventory "$actual"
    if cmp -s "$expected13" "$actual"; then
        source_inventory=$expected13
        source_schema_max=13
    elif cmp -s "$expected14" "$actual"; then
        source_inventory=$expected14
        source_schema_max=14
    else
        echo "legacy safety backup requires exactly migrations 1 through 13 or 14" >&2
        exit 65
    fi
    database_identity "$live_identity"
    if [ -e "$incoming" ] || [ -L "$incoming" ]; then
        if [ ! -d "$incoming" ] || [ -L "$incoming" ] || \
           [ ! -f "$owner" ] || [ -L "$owner" ]; then
            echo "schema-13 backup staging is unowned or unsafe" >&2
            exit 73
        fi
        verify_owner
        if [ "$resume" != true ]; then
            echo "retry interrupted schema-13 backup with CARDRAG_SCHEMA13_TRANSITION_RESUME=true" >&2
            exit 75
        fi
        python3 - "$incoming" <<'PY'
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
for path in (root, *root.rglob("*")):
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not (
        stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)
    ):
        raise SystemExit("schema-13 staging contains a symlink or special file")
    if (metadata.st_uid, metadata.st_gid) != (os.getuid(), os.getgid()):
        raise SystemExit("schema-13 staging is not owned by this operation user")
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise SystemExit("schema-13 staging is group/other writable")
PY
        chmod -R u+rwX -- "$incoming"
        rm -rf -- "$incoming"
    elif [ -e "$owner" ] || [ -L "$owner" ]; then
        verify_owner
        if [ "$resume" != true ]; then
            echo "retry interrupted schema-13 backup with CARDRAG_SCHEMA13_TRANSITION_RESUME=true" >&2
            exit 75
        fi
    else
        python3 - "$owner" "$owner_body" <<'PY'
import os
import sys
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
descriptor = os.open(sys.argv[1], flags, 0o600)
try:
    payload = (sys.argv[2] + "\n").encode()
    os.write(descriptor, payload)
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
        fsync_archive_root
    fi
    install -d -m 0750 "$incoming/database"
    cp "$source_inventory" "$incoming/schema-migrations.tsv"
    pg_dump --no-password --host "$postgres_host" --port "$postgres_port" \
        --username "$postgres_user" --dbname cardrag --format custom \
        --file "$incoming/database/cardrag.dump"
    pg_dump --no-password --host "$postgres_host" --port "$postgres_port" \
        --username "$postgres_user" --dbname keycloak --format custom \
        --file "$incoming/database/keycloak.dump"
    active_sessions=$(psql_admin --tuples-only --no-align --command \
        "SELECT count(*) FROM pg_stat_activity
         WHERE datname IN ('cardrag','keycloak') AND pid <> pg_backend_pid()")
    if [ "$active_sessions" != 0 ]; then
        echo "database writer appeared during schema-13 safety backup" >&2
        exit 75
    fi
    require_schema_inventory "$source_inventory" "migrations 1 through $source_schema_max"
    database_identity "$sealed_identity"
    if ! cmp -s "$live_identity" "$sealed_identity"; then
        echo "database identity changed during schema-13 safety backup" >&2
        exit 75
    fi
    python3 - "$incoming/transition-manifest.json" "$incoming" "$transition_id" \
        "$source_schema_max" "$live_identity" <<'PY'
import hashlib
import json
import os
import sys
from pathlib import Path
path = sys.argv[1]
root = sys.argv[2]
files = []
for relative in (
    "database/cardrag.dump", "database/keycloak.dump", "schema-migrations.tsv"
):
    digest = hashlib.sha256()
    size = 0
    with open(os.path.join(root, relative), "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    files.append({
        "path": relative,
        "sha256": digest.hexdigest(),
        "size_bytes": size,
    })
payload = {
    "schema_version": "cardrag-schema13-safety.v2",
    "transition_id": sys.argv[3],
    "postgres_major": 17,
    "source_schema_max": int(sys.argv[4]),
    "source_identity": {
        "system_identifier": Path(sys.argv[5]).read_text().splitlines()[0].split("\t")[1],
        "databases": [
            {"name": line.split("\t")[0], "oid": int(line.split("\t")[1])}
            for line in Path(sys.argv[5]).read_text().splitlines()[1:]
        ],
    },
    "databases": ["cardrag", "keycloak"],
    "files": files,
}
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
descriptor = os.open(path, flags, 0o440)
try:
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    os.write(descriptor, encoded)
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
    (CDPATH= cd -- "$incoming" && sha256sum \
        database/cardrag.dump database/keycloak.dump schema-migrations.tsv \
        transition-manifest.json >checksums.sha256)
    manifest_sha=$(sha256sum "$incoming/transition-manifest.json" | awk '{print $1}')
    python3 - "$incoming/READY" "$transition_id" "$manifest_sha" <<'PY'
import json
import os
import sys
payload = {
    "schema_version": "cardrag-schema13-safety-ready.v2",
    "transition_id": sys.argv[2],
    "manifest_sha256": sys.argv[3],
}
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
descriptor = os.open(sys.argv[1], flags, 0o440)
try:
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    os.write(descriptor, encoded)
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
    find "$incoming" -type d -exec chmod 0550 {} +
    find "$incoming" -type f -exec chmod 0440 {} +
    python3 - "$incoming" "$archive_root" <<'PY'
import os
import stat
import sys
incoming, archive = sys.argv[1:]
for current, directory_names, file_names in os.walk(incoming, topdown=False):
    for name in file_names:
        path = os.path.join(current, name)
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise SystemExit("unsafe schema-13 payload entry")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    for name in directory_names:
        path = os.path.join(current, name)
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            raise SystemExit("unsafe schema-13 payload directory")
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    descriptor = os.open(current, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
descriptor = os.open(archive, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
    mv "$incoming" "$package"
    fsync_archive_root
    verify_package
    rm -f -- "$owner"
    fsync_archive_root
    echo "schema-13 two-database safety backup complete: $transition_id"
    exit 0
fi

verify_package
package_identity "$package/transition-manifest.json" "$sealed_identity"
database_inventory "$actual"
if cmp -s "$expected15" "$actual"; then
    database_identity "$live_identity"
    if ! cmp -s "$sealed_identity" "$live_identity"; then
        echo "sealed legacy backup does not match the live database identity" >&2
        exit 65
    fi
    vector_version=$(psql_cardrag_as_admin --tuples-only --no-align --command \
        "SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    if [ "$vector_version" != 0.8.6 ]; then
        echo "schema 15 database has unexpected pgvector version: $vector_version" >&2
        exit 74
    fi
    echo "migrations 014 and 015 are already verified: $transition_id"
    exit 0
fi
if cmp -s "$expected13" "$actual"; then
    source_schema_max=13
elif cmp -s "$expected14" "$actual"; then
    source_schema_max=14
else
    echo "controlled upgrade requires exactly migrations 1 through 13 or 14" >&2
    exit 65
fi
if ! cmp -s "$package/schema-migrations.tsv" "$actual"; then
    echo "sealed legacy backup does not match the live pre-upgrade schema" >&2
    exit 65
fi
database_identity "$live_identity"
if ! cmp -s "$sealed_identity" "$live_identity"; then
    echo "sealed legacy backup does not match the live database identity" >&2
    exit 65
fi
migration14=$migration_root/014_legacy_import_and_portability.sql
migration14_sha=$(sha256sum "$migration14" | awk '{print $1}')
migration15=$migration_root/015_pgvector_086.sql
migration15_sha=$(sha256sum "$migration15" | awk '{print $1}')
{
    printf "%s\n" "SELECT pg_advisory_xact_lock(hashtext('cardrag-schema-migration'));"
    cat <<'SQL'
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_stat_activity
        WHERE datname IN ('cardrag', 'keycloak')
          AND pid <> pg_backend_pid()
    ) THEN
        RAISE EXCEPTION 'database session appeared before migrations 014 and 015';
    END IF;
END $$;
DO $$
DECLARE
    installed_version text;
    extension_owner text;
BEGIN
    SELECT extversion, pg_get_userbyid(extowner)
      INTO installed_version, extension_owner
      FROM pg_extension
     WHERE extname = 'vector';
    IF installed_version IS NULL THEN
        RAISE EXCEPTION 'vector extension is not installed';
    END IF;
    IF extension_owner <> current_user THEN
        RAISE EXCEPTION 'vector extension owner % differs from transition role %',
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
END $$;
SET ROLE cardrag;
SQL
    if [ "$source_schema_max" -eq 13 ]; then
        cat "$migration14"
        printf "%s\n" \
            "INSERT INTO schema_migrations(version, name, checksum) VALUES" \
            "(14, '014_legacy_import_and_portability.sql', '$migration14_sha');"
    fi
    cat "$migration15"
    printf "%s\n" \
        "INSERT INTO schema_migrations(version, name, checksum) VALUES" \
        "(15, '015_pgvector_086.sql', '$migration15_sha');"
} | psql_cardrag_as_admin \
    --single-transaction >/dev/null
require_schema_inventory "$expected15" 'migrations 1 through 15'
vector_version=$(psql_cardrag_as_admin --tuples-only --no-align --command \
    "SELECT extversion FROM pg_extension WHERE extname = 'vector'")
if [ "$vector_version" != 0.8.6 ]; then
    echo "migration 015 did not verify pgvector 0.8.6" >&2
    exit 74
fi
portable_roots=$(psql_cardrag_as_admin --tuples-only --no-align --command \
    "SELECT count(*) FROM generations
     WHERE root_key = 'generations/' || generation_id AND root_uri = root_key")
generation_count=$(psql_cardrag_as_admin --tuples-only --no-align \
    --command 'SELECT count(*) FROM generations')
if [ "$portable_roots" != "$generation_count" ]; then
    echo "migration 014 did not canonicalize every generation root" >&2
    exit 74
fi
echo "controlled schema $source_schema_max to 15 and pgvector 0.8.6 upgrade complete: $transition_id"
