# Portainer host storage and server migration

For a fresh Docker Standalone host, the guided installer in
[`QUICKSTART.ko.md`](QUICKSTART.ko.md) prepares the same storage, secret, image,
and bootstrap contracts with fewer manual steps. Use this full runbook for an
existing installation, NAS export/restore, server migration, timers, or incident
recovery. Legacy PDF/OCR transfer has a shorter companion guide in
[`LEGACY_IMPORT_QUICKSTART.ko.md`](LEGACY_IMPORT_QUICKSTART.ko.md).

The production Stack stores durable files on the Docker host, not inside a
container or a Stack-scoped volume:

| Host | Container | Access |
|---|---|---|
| `${CARDRAG_DATA_ROOT}/objects` | `/var/lib/cardrag/objects` | admin/worker RW, MCP RO |
| `${CARDRAG_DATA_ROOT}/generations` | `/var/lib/cardrag/generations` | admin RW, worker/MCP RO |
| `${CARDRAG_DATA_ROOT}/build` | `/var/lib/cardrag-build` | admin/worker RW |
| `${CARDRAG_DATA_ROOT}/page-cache` | `/var/cache/cardrag-pages` | MCP RW |
| `${CARDRAG_IMPORT_ROOT}` | `/mnt/cardrag-imports` | legacy import/state export only, RO |
| `${CARDRAG_ARCHIVE_ROOT}` | `/mnt/cardrag-archive` | state export RW, restore RO |

PostgreSQL and Codex login state use explicit external volumes. Stack removal
does not delete them, but they are not a server backup. Move PostgreSQL with
`cardrag state export/restore`; log Codex in again on the new server.

## Supported Portainer environment

These Stacks target a **Docker Standalone** Portainer environment with Docker
Compose v2. They use Compose profiles, conditional dependencies,
`pull_policy`, and `bind.create_host_path: false`; do not paste them into a
Swarm or Kubernetes Stack. Install the exact reviewed release checkout at
`/opt/cardrag` (not a symlink) so Portainer and host timers use the same files.

## Download and verify release evidence

The three images must come from one successful `Public image release` run for
the exact `v0.2.1` tag. With an authenticated GitHub CLI, download its integrated
artifact instead of copying role digests from a web page:

```sh
export RELEASE_VERSION=0.2.1
export RELEASE_GIT_SHA=$(git -C /opt/cardrag rev-parse "v${RELEASE_VERSION}^{commit}")
test "$(git -C /opt/cardrag rev-parse HEAD)" = "$RELEASE_GIT_SHA"
release_run_id=$(gh run list \
  --repo Kanu-Coffee/MCP_card_prd_detail --workflow release.yml \
  --commit "$RELEASE_GIT_SHA" --status success --limit 1 \
  --json databaseId --jq '.[0].databaseId')
test -n "$release_run_id"
install -d -m 0700 "/var/tmp/cardrag-release-${RELEASE_VERSION}"
gh run download "$release_run_id" \
  --repo Kanu-Coffee/MCP_card_prd_detail \
  --name "release-manifest-${RELEASE_VERSION}" \
  --dir "/var/tmp/cardrag-release-${RELEASE_VERSION}"
```

Verify the tag commit and all three registry signatures. Preserve the complete
downloaded artifact in the operational release archive; an Actions artifact is
not a permanent backup.

```sh
release_manifest="/var/tmp/cardrag-release-${RELEASE_VERSION}/release-manifest.json"
jq -e --arg version "$RELEASE_VERSION" --arg sha "$RELEASE_GIT_SHA" '
  .schema == "cardrag.container-release.v3"
  and .version == $version and .git_sha == $sha
  and (.roles | keys | sort) == ["admin", "mcp", "worker"]
' "$release_manifest" >/dev/null
for role in admin worker mcp; do
  image=$(jq -r --arg role "$role" '.roles[$role].image' "$release_manifest")
  digest=$(jq -r --arg role "$role" '.roles[$role].digest' "$release_manifest")
  cosign verify \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com \
    --certificate-identity "https://github.com/Kanu-Coffee/MCP_card_prd_detail/.github/workflows/release.yml@refs/tags/v${RELEASE_VERSION}" \
    --certificate-github-workflow-sha "$RELEASE_GIT_SHA" \
    --check-claims=true "$image@$digest" >/dev/null
done
```

## Fresh host preparation

Create the non-secret environment file before `/srv/cardrag/config` exists.
Load this exact file into Portainer and use it as the host timer selector file.
Replace the paired reserved HTTPS example values with one real public Keycloak
origin, and fill the three digest-qualified image references from the manifest.

```sh
sudo install -d -o root -g docker -m 0750 /etc/cardrag
sudo install -o root -g docker -m 0640 \
  /opt/cardrag/deploy/portainer/stack.env.example /etc/cardrag/stack.env
sudoedit /etc/cardrag/stack.env
set -a
. /etc/cardrag/stack.env
set +a
case "$KEYCLOAK_PUBLIC_URL" in
  https://*.example|https://*.invalid|'')
    echo "replace KEYCLOAK_PUBLIC_URL with the live HTTPS origin" >&2
    exit 1
    ;;
esac
test "$CARDRAG_OIDC_ISSUER" = "$KEYCLOAK_PUBLIC_URL/realms/cardrag"
cd /opt/cardrag
sudo env \
  CARDRAG_DATA_ROOT="$CARDRAG_DATA_ROOT" \
  CARDRAG_IMPORT_ROOT="$CARDRAG_IMPORT_ROOT" \
  CARDRAG_CONFIG_ROOT="$CARDRAG_CONFIG_ROOT" \
  CARDRAG_POSTGRES_VOLUME="$CARDRAG_POSTGRES_VOLUME" \
  CARDRAG_CODEX_AUTH_VOLUME="$CARDRAG_CODEX_AUTH_VOLUME" \
  deploy/portainer/prepare-host-storage.sh --create-empty-external-volumes
```

Create the root-only secret directory. Database passwords are URL-safe
hexadecimal so the generated DSNs need no escaping. Enter the real OpenRouter
key with `sudoedit`; never put it in the environment file or Stack YAML.

```sh
sudo sh -eu -s -- "$CARDRAG_SECRETS_DIR" <<'SH'
secret_root=$1
case "$secret_root" in /*) ;; *) exit 78 ;; esac
test ! -L "$secret_root"
install -d -o root -g root -m 0700 "$secret_root"
if find "$secret_root" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
  echo "refusing to overwrite a non-empty secret directory" >&2
  exit 73
fi
umask 077
for name in \
  postgres_admin_password cardrag_db_password cardrag_worker_db_password \
  cardrag_mcp_db_password keycloak_db_password keycloak_admin_password
do
  openssl rand -hex 32 >"$secret_root/$name.txt"
done
cardrag_password=$(tr -d '\r\n' <"$secret_root/cardrag_db_password.txt")
worker_password=$(tr -d '\r\n' <"$secret_root/cardrag_worker_db_password.txt")
mcp_password=$(tr -d '\r\n' <"$secret_root/cardrag_mcp_db_password.txt")
printf 'postgresql://cardrag:%s@postgres:5432/cardrag\n' "$cardrag_password" \
  >"$secret_root/cardrag_database_url.txt"
printf 'postgresql://cardrag_worker:%s@postgres:5432/cardrag\n' "$worker_password" \
  >"$secret_root/cardrag_worker_database_url.txt"
printf 'postgresql://cardrag_mcp:%s@postgres:5432/cardrag\n' "$mcp_password" \
  >"$secret_root/cardrag_mcp_database_url.txt"
chmod 0444 "$secret_root"/*.txt
SH
sudoedit "$CARDRAG_SECRETS_DIR/openrouter_api_key.txt"
sudo chmod 0444 "$CARDRAG_SECRETS_DIR/openrouter_api_key.txt"
sudo test -s "$CARDRAG_SECRETS_DIR/openrouter_api_key.txt"
```

Prepare the host roots and render all checked Stacks. The pre-migration Stack
is needed only for an existing v0.1.2 installation.

```sh
cd /opt/cardrag
deploy/portainer/render-bootstrap-stack.sh
deploy/portainer/render-stack.sh
deploy/portainer/render-validation-stack.sh
deploy/portainer/render-export-stack.sh
deploy/portainer/render-restore-stack.sh
# Existing v0.1.2 only:
deploy/portainer/render-pre-migration-export-stack.sh
sudo env \
  CARDRAG_DEPLOYMENT_ROOT="$CARDRAG_DEPLOYMENT_ROOT" \
  CARDRAG_ADMIN_IMAGE="$CARDRAG_ADMIN_IMAGE" \
  CARDRAG_WORKER_IMAGE="$CARDRAG_WORKER_IMAGE" \
  CARDRAG_MCP_IMAGE="$CARDRAG_MCP_IMAGE" \
  deploy/portainer/install-deployment-metadata.sh \
  deploy/portainer/cardrag-stack.yaml "$release_manifest"
```

`install-deployment-metadata.sh` validates the v3 release schema and exact
role/version/revision/digest bindings. It writes the redacted Stack, image map,
and release manifest needed by portable export. The Stack refuses to create a
missing bind source.

## One-time Keycloak bootstrap in Portainer

On an empty PostgreSQL volume, create a Stack named `cardrag-bootstrap` from
`deploy/portainer/cardrag-bootstrap-stack.yaml` and load the exact
`/etc/cardrag/stack.env`. It contains only PostgreSQL and Keycloak. Once both
are healthy, sign in with `KEYCLOAK_BOOTSTRAP_ADMIN_USERNAME` and the value in
`keycloak_admin_password.txt`. Create a named permanent administrator, sign
out, and prove that account works in a fresh private browser session. Using the
permanent account, disable/delete the bootstrap administrator or rotate its
credential, then prove that the original bootstrap credential can no longer
sign in. Removing the password file alone does not revoke the account already
stored in Keycloak.

The public TLS proxy and certificate for `KEYCLOAK_PUBLIC_URL` must already be
working. A host proxy uses `127.0.0.1:8080`. A containerized proxy must be
explicitly attached to `cardrag-bootstrap_backend` and use `keycloak:8080`;
after bootstrap, detach it from that removed network, attach it to
`cardrag_backend`, and keep the same upstream name. Never publish port 8080 on
all host interfaces as a shortcut.

Remove the entire bootstrap Stack without deleting the external PostgreSQL
volume. Confirm its two containers are gone, then securely delete
`keycloak_admin_password.txt` according to host policy. The normal Stack does
not declare or mount that credential.

Create the normal Stack named `cardrag` from
`deploy/portainer/cardrag-stack.yaml` and load the same environment file. It
enables the worker process; admin and legacy import are disabled profiles, and
export/restore remain separate maintenance Stacks.

Before queuing import, BULK, or daily work, initialize Codex authentication:

1. Open the running `cardrag-worker-1` container in Portainer, select
   **Console**, and connect as its default user with `/bin/sh`.
2. Run `codex login --device-auth`, approve the displayed URL and short code on
   a separate trusted device, then run `codex login status`.
3. Exit the console and restart only the worker container in Portainer.
4. Open a new worker console and run `codex login status` again. It must remain
   authenticated through the external `CARDRAG_CODEX_AUTH_VOLUME`.

If either status fails, leave all mutation profiles and host timers disabled.
MCP and admin never receive the Codex auth volume.

## Portainer-aware daily and retention timers

Do not install the base `deploy/systemd` units for this deployment: they render
`compose.yaml` with the development named volumes. The Portainer units below
pin the `cardrag` project, `/etc/cardrag/stack.env`, and the checked generated
host-bind Stack. They use the admin allow-list, `--no-deps`, and `--pull never`;
the rendered admin service contains no `build` definition.

After Keycloak, Codex, the worker, and an initial published generation have all
passed live checks, install but do not start the timers:

```sh
getent group docker >/dev/null
id cardrag >/dev/null 2>&1 || \
  sudo useradd --system --no-create-home --shell /usr/sbin/nologin cardrag
sudo usermod -aG docker cardrag
id cardrag
id -nG cardrag | tr ' ' '\n' | grep -Fx docker
sudo -u cardrag test -r /opt/cardrag/deploy/portainer/cardrag-stack.yaml
sudo -u cardrag test -r /etc/cardrag/stack.env
sudo install -o root -g root -m 0644 \
  /opt/cardrag/deploy/portainer/systemd/cardrag-portainer-daily.service \
  /opt/cardrag/deploy/portainer/systemd/cardrag-portainer-daily.timer \
  /opt/cardrag/deploy/portainer/systemd/cardrag-portainer-retention.service \
  /opt/cardrag/deploy/portainer/systemd/cardrag-portainer-retention.timer \
  /etc/systemd/system/
sudo systemctl daemon-reload
systemd-analyze verify \
  /etc/systemd/system/cardrag-portainer-daily.service \
  /etc/systemd/system/cardrag-portainer-daily.timer \
  /etc/systemd/system/cardrag-portainer-retention.service \
  /etc/systemd/system/cardrag-portainer-retention.timer
```

`/etc/cardrag/stack.env` contains selectors and public URLs only, so it is
`0640 root:docker`. Keep the actual secret directory `0700 root:root`; do not
copy passwords or provider keys into the timer environment. Docker group access
is root-equivalent and must be limited to this reviewed service account and
approved operators.

Keep `COMPOSE_PROFILES`, `CARDRAG_ADMIN_OPERATION`, and its enable flag empty or
disabled in the normal Portainer deployment. The service command overrides only
the one operation: `run-daily` at 03:00 KST and owner-only
`retention-prune` at 04:00 KST. Review one manual daily run and one retention
result before enabling automatic catch-up:

```sh
sudo systemctl start cardrag-portainer-daily.service
sudo journalctl -u cardrag-portainer-daily.service --no-pager
sudo systemctl start cardrag-portainer-retention.service
sudo journalctl -u cardrag-portainer-retention.service --no-pager
sudo systemctl enable --now \
  cardrag-portainer-daily.timer cardrag-portainer-retention.timer
systemctl list-timers \
  cardrag-portainer-daily.timer cardrag-portainer-retention.timer
```

Before export, restore, storage migration, image rollback, or any other
maintenance window, quiesce these host writers first:

```sh
sudo systemctl disable --now \
  cardrag-portainer-daily.timer cardrag-portainer-retention.timer
sudo systemctl stop \
  cardrag-portainer-daily.service cardrag-portainer-retention.service
test "$(systemctl is-active cardrag-portainer-daily.service)" = inactive
test "$(systemctl is-active cardrag-portainer-retention.service)" = inactive
```

Then use the admin `run-list`/`job-status` operations to prove no durable run or
job remains nonterminal. Stopping the host supervisor does not erase a run that
was already committed to PostgreSQL; resume/finalize that same run instead of
starting a duplicate. Re-enable timers only after the normal Stack and writer
have passed post-maintenance checks.

## Archive mount guard

Mount the NAS at `CARDRAG_ARCHIVE_ROOT`, then initialize its sentinel while
also pinning the exact expected mount source:

```sh
sudo CARDRAG_ARCHIVE_EXPECTED_SOURCE='nas:/cardrag' \
  deploy/portainer/init-archive-root.sh
```

The export and restore containers require a regular file named
`.cardrag-archive-root` containing `cardrag-archive-v1`. A missing or wrong
sentinel fails closed. They also require the regular file
`.cardrag-archive-mount-source` to exactly match
`CARDRAG_ARCHIVE_EXPECTED_SOURCE`. The initializer uses `findmnt` to verify that
live mount identity before recording it, preventing a NAS outage from creating
backups on an ordinary local fallback directory. Re-run it only after an
intentional archive source change.

## Starting an admin or legacy import one-shot in Portainer

Never leave optional profiles enabled on the normal Stack. For exactly one
admin operation, set `COMPOSE_PROFILES=ops`,
`CARDRAG_ADMIN_OPERATION_ENABLED=true`, one allow-listed
`CARDRAG_ADMIN_OPERATION`, and its ID when required. Supported values are
`legacy-status`, `legacy-finalize`, `run-list`, `run-bulk`, `run-daily`,
`run-status`, `run-finalize`, `job-status`, `generation-verify`, and
`retention-prune`; arbitrary commands are rejected. The two scheduled values
are normally invoked only by the fixed host units above. For a legacy import use
`COMPOSE_PROFILES=legacy-import` and its dedicated enable flag instead. Redeploy
once, inspect the one-shot exit and JSON log, then clear every operation/profile
value and redeploy. Never add export or restore services to this Stack.

A profile only makes a service available. Before enabling it, satisfy the
operation-specific quiescence and empty-target checks below. Mutating operations
remain disabled unless the operator sets both the one-shot enable flag and the
exact operation.

## Legacy import

Copy a completed `bundle-<digest>` to `/srv/cardrag/imports`, verify `READY` and
the bundle checksums, and set `CARDRAG_LEGACY_BUNDLE_NAME` to its single
directory name (for example `bundle-a1b2c3d4e5f6`). Enable the `legacy-import`
profile and `CARDRAG_LEGACY_IMPORT_ENABLED=true` for one redeployment. The
import root is read-only and is never visible to worker or MCP.

`legacy-import` defaults to `--no-publish`. Inspect the import status and quality
gate with the admin operation `legacy-status`, then set
`CARDRAG_ADMIN_OPERATION=legacy-finalize` and
`CARDRAG_ADMIN_OPERATION_ID=<import-id>` for the deliberate finalize one-shot.
The fixed allow-list means enabling the `ops` profile alone only prints a
disabled message and cannot silently run an arbitrary admin command.

If an import was interrupted, keep the original normalized bundle directory in
`CARDRAG_IMPORT_ROOT`, set `CARDRAG_LEGACY_OPERATION=resume` and
`CARDRAG_LEGACY_IMPORT_ID=<existing UUID>`, then enable the same `legacy-import`
one-shot once. Its effective command is `cardrag legacy resume IMPORT_ID`; the
CLI reads the ledger's bundle ID and resolves
`/mnt/cardrag-imports/<bundle_id>`. It refuses a missing, renamed, or symlinked
bundle. Return the operation to `import`, clear the ID, disable the job, and
clear the profile after completion.

## Existing Stack-scoped volumes to host binds

Do not inspect or copy `/var/lib/docker/volumes` directly. Discover names while
the old Stack containers still exist, then quiesce scheduler, retention, admin,
worker, MCP, and Keycloak. Confirm no nonterminal work remains and remove the
old Stack containers without deleting its named volumes.

Discover the actual volume names from Compose labels and container mount
metadata:

```sh
set -a
. ./deploy/portainer/stack.env.example
set +a

deploy/portainer/discover-legacy-volumes.sh cardrag > legacy-volumes.env
. ./legacy-volumes.env
export CARDRAG_LEGACY_OBJECTS_VOLUME CARDRAG_LEGACY_GENERATIONS_VOLUME

# With all old containers removed, this must pass before any maintenance Stack.
deploy/portainer/check-maintenance-volume-exclusive.sh
```

With `CARDRAG_ARCHIVE_ROOT` and `CARDRAG_ARCHIVE_EXPECTED_SOURCE` exported, this
host check also re-runs `findmnt` against the live NAS mount immediately before
the maintenance Stack is created. Containers independently verify the two
0440 archive identity files; they never receive the Docker socket or host mount
namespace.

The v0.1.2 database ends at schema 13, while the v0.2 portable exporter requires
the relative generation root added by migration 014. Do not run `state export`
directly against schema 13. Set a unique maintenance-window transition ID and
enable the bridge only in the generated pre-migration Stack:

```sh
export CARDRAG_SCHEMA13_TRANSITION_ID=prod-20260813-a
export CARDRAG_SCHEMA13_TRANSITION_ENABLED=true
export CARDRAG_SCHEMA13_TRANSITION_RESUME=false
```

Deploy `cardrag-pre-migration-export-stack.yaml`. Its exact service set is
PostgreSQL, `schema13-safety-backup`, `schema14-upgrade`, and `state-export`.
Dependencies enforce this order:

1. Verify PostgreSQL 17, zero CardRAG/Keycloak sessions, and the exact trusted
   migration 001..013 checksums.
2. Write custom-format raw dumps of both `cardrag` and `keycloak` to
   `schema13-safety-<transition-id>`, verify both dump catalogs and checksums,
   and seal `READY` last.
3. Re-verify that sealed package and apply only migration 014 in one transaction
   under the `cardrag` role and an advisory lock.
4. Verify exact migration 001..014 and portable generation roots, then run the
   ordinary schema-14 portable exporter with both legacy volumes read-only.

Only the PostgreSQL admin secret is given to the bridge jobs; `SET ROLE`
preserves CardRAG object ownership for migration 014. The bridge is disabled by
default. A missing transition ID, incomplete raw dump, changed migration, wrong
PostgreSQL major, active DB session, or checksum failure stops downstream work.
On a same-ID retry after the raw backup sealed, set
`CARDRAG_SCHEMA13_TRANSITION_RESUME=true`; the package is verified and reused.

Migration 014 changes the live database. If portable export fails afterward,
fix the v0.2 exporter and redeploy the same bridge; do not restart v0.1.2 against
schema 14. To return to v0.1.2, remove this Stack and restore both raw dumps into
a separately prepared empty PostgreSQL 17 volume, verify schema 001..013, and
switch volumes during the maintenance window. Never overwrite the only database
copy in place.

After both raw and portable packages verify, remove the pre-migration Stack,
set `CARDRAG_SCHEMA13_TRANSITION_ENABLED=false`, run the exclusivity check again,
then start the network-isolated migration job:

```sh

docker compose --project-name cardrag-storage-migration \
  -f deploy/portainer/storage-migrate.compose.yaml up \
  --abort-on-container-exit --exit-code-from storage-migrate
```

Review `legacy-volumes.env` before sourcing it. It must contain exactly the
PostgreSQL, Codex, object, and generation volume names observed from containers.
Before starting the migration, confirm `cardrag run list`, `cardrag job status`,
and `cardrag legacy status` show no running/nonterminal work. Stop every writer;
the migration job cannot prove quiescence because it deliberately has no DB or
application network access.

The migration service mounts the discovered legacy object/generation volumes
read-only and host targets read-write. It rejects non-empty targets, symlinks,
special files, a non-empty object `.incoming`, invalid generation seals, or any
object/checksum difference. It writes reports to
`/srv/cardrag/migration/reports`. It has no application network and never
mounts the Docker data directory.

If interrupted, it keeps only migration-ID-owned staging and a commit marker.
Retry using the same `CARDRAG_STORAGE_MIGRATION_ID` and set
`CARDRAG_STORAGE_MIGRATION_RESUME=true`; a different ID or an unmarked partial
tree is never removed automatically. A host advisory lock rejects concurrent
migration attempts.

After a successful migration, deploy the validation Stack first, reconcile the
database active generation with `current.json`, and test readiness, search,
citation, PDF download, and rollback. Remove it and deploy the normal Stack only
after validation. Keep the old named volumes for seven days.

`build` and `page-cache` are deliberately not migrated; they are recreated.

Legacy import, state export, restore, and migration check writable destination
filesystems only; read-only bundle/archive sources are excluded from capacity
admission. Before mutation, each destination needs at least 50 GiB and 20% free
blocks/inodes and must remain below 85% use. At 70% use it warns. After the
command, the absolute 50 GiB gate is no longer applied, but both free blocks and
inodes must still be at least 20%.

## Whole-server move

During a maintenance window, stop every CardRAG/Keycloak writer and confirm no
nonterminal pipeline/import/job or non-backup database connection remains.
Remove the normal Portainer Stack containers; its host binds and external
volumes remain intact. Then prove that no running or stopped container still
attaches the PostgreSQL volume:

```sh
deploy/portainer/check-maintenance-volume-exclusive.sh
```

Deploy `cardrag-export-stack.yaml` as a separate Portainer Stack with
`CARDRAG_STATE_EXPORT_ENABLED=true`. It contains exactly PostgreSQL and
`state-export`; PostgreSQL has restart disabled and its healthcheck connects
only to the maintenance `postgres` database. Export sees the object/generation stores read-only,
the import bundles read-only, and the verified archive read-write. It receives
only the PostgreSQL admin backup secret in addition to the normal admin DSN.
`CARDRAG_STATE_SELF_CONTAINED=false` is the safe default and does not require a
legacy bundle. Set it to `true` only when `/srv/cardrag/imports` already contains
at least one sealed normalized bundle that must travel inside this state package;
an invalid boolean or an unsealed/empty requested bundle set fails closed.
After success, remove the entire export Stack so it detaches the external
PostgreSQL volume. Run the exclusivity check again before recreating the normal
Stack.

On the empty target server, prepare storage and empty external volumes but do
not deploy the normal Stack. Run the exclusivity check, set
`CARDRAG_STATE_PACKAGE_PATH` to a verified `READY` package within the archive,
and deploy only `cardrag-restore-stack.yaml` with
`CARDRAG_STATE_RESTORE_ENABLED=true`. It contains exactly PostgreSQL and
`state-restore`. Restore sees the archive
and target deployment compatibility metadata read-only, and empty object and
generation targets through one writable `/mnt/cardrag-runtime` parent mount;
this is required for atomic sibling staging and rename. Its preflight verifies
UID/GID ownership, safe permissions, an empty and unchanged `build`/`page-cache`,
and no unowned runtime siblings before and after the restore. It alone receives
the four new role-password secrets used to rotate target database role
passwords. The one-shot always invokes `state restore --verify-restored`: it
validates both filesystem targets, both target/staging database pairs, and all
four role secrets before the first mutation, stages both databases before
activation, then opens the CardRAG inspector only after activation to reconcile
the database epoch, generation pointers, object hashes, and references. A
post-restore reconciliation failure therefore makes the one-shot fail.

After restore and verification succeed, remove the restore Stack completely,
confirm the volume is detached, and deploy `cardrag-validation-stack.yaml`
first. That generated validation Stack is the normal host-bind deployment with
the worker service removed; it permits Keycloak/MCP read-path, authentication,
search, citation, PDF Range, and readiness checks without allowing a job claim.
Its admin one-shot exits disabled by default. For the deliberate rollback test,
first record the exported/current generation ID. Set
`CARDRAG_VALIDATION_ROLLBACK_ENABLED=true` with an empty target for exactly one
redeployment to move to the previous generation, then repeat the read-path
checks. Next redeploy the same validation Stack once more with
`CARDRAG_VALIDATION_ROLLBACK_GENERATION_ID=<recorded-original-current-id>` and
repeat readiness/search/citation/PDF checks. Confirm `current.json` and the DB
active row both name that original ID, then disable the one-shot. Never leave
the validation environment on the previous generation.
Remove the validation Stack after those checks,
confirm the PostgreSQL volume is detached again, and only then deploy
`cardrag-stack.yaml`. This ordering prevents worker, MCP, Keycloak, or migrations
from racing the restore and prevents the worker from starting before validation.

Do not start the old and new workers simultaneously. Validate Keycloak, MCP
readiness, representative search/citations, PDF range requests, and a generation
rollback before starting the new worker and switching traffic. Keep the old
server stopped but intact for seven days.
