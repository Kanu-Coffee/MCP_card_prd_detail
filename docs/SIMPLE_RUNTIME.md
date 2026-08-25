# CardRAG simple runtime operations

## Runtime boundary

The supported runtime is one finite Worker and one always-on MCP process. The
only data contract between them is immutable content in WebDAV:

```text
/v1/objects/sha256/<prefix>/<sha256>
/v1/ocr-cache/native/<prefix>/<reuse-key>/manifest.json
/v1/ocr-cache/native/<prefix>/<reuse-key>/READY.json
/v1/ocr-cache/adopted/<prefix>/<reuse-key>/manifest.json
/v1/ocr-cache/adopted/<prefix>/<reuse-key>/READY.json
/v1/generations/<generation-id>/index.sqlite3
/v1/generations/<generation-id>/manifest.json
/v1/generations/<generation-id>/READY.json
/v1/channels/stable.json
```

The Worker is the sole writer. MCP uses a read-only WebDAV facade and never
accesses WebDAV while serving a request. It activates a generation only after
the SQLite file and every referenced PDF have been downloaded and verified.

## Configuration and secrets

Start from [`deploy/simple.env.example`](../deploy/simple.env.example), creating
separate `/etc/cardrag/worker.env` and `/etc/cardrag/mcp.env` files. The common
application contract is:

```text
CARDRAG_WEBDAV_BASE_URL
CARDRAG_WEBDAV_USERNAME | CARDRAG_WEBDAV_USERNAME_FILE
CARDRAG_WEBDAV_PASSWORD | CARDRAG_WEBDAV_PASSWORD_FILE
CARDRAG_WEBDAV_CA_FILE                         # optional
CARDRAG_WEBDAV_CONNECT_TIMEOUT_SECONDS=10
CARDRAG_WEBDAV_TRANSFER_TIMEOUT_SECONDS=600
```

Direct values and their `_FILE` variants are mutually exclusive. Production
mode accepts only HTTPS WebDAV URLs and rejects URL credentials, query strings,
and fragments. The Worker and MCP intentionally use the same Basic Auth
account, while MCP code exposes only read methods.

In production, both runtimes also require the OpenRouter base URL to be HTTPS
and reject credentials, query strings, and fragments in that URL. This prevents
embedding API keys from being sent to an ambiguous or plaintext endpoint.

The Compose secret overlays translate host-side `*_SECRET_FILE` paths from the
env file into container-side `/run/secrets/*` paths. Secret files must be
regular UTF-8 files containing one non-empty line. Make them readable by the
operator that invokes Docker Compose (the timer uses the `cardrag` user) and no
broader than necessary. The images run as numeric UID/GID 10001, so use host
group 10001 and mode `0440` for file-backed Compose secrets. This remains
readable if Compose implements a local secret as a bind mount. Do not commit
them. Add the role's `compose.ca.yaml` as the last overlay when a private CA is
required. For the Worker timer, also set
`CARDRAG_WORKER_COMPOSE_OVERLAYS=--file deploy/worker/compose.ca.yaml`; the
systemd unit expands this root-controlled value into the optional Compose
arguments.

Worker-specific settings are:

```text
CARDRAG_ENABLED_ISSUERS=woori,kb,shinhan
CARDRAG_OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
CARDRAG_OPENROUTER_API_KEY | CARDRAG_OPENROUTER_API_KEY_FILE
CARDRAG_EMBEDDING_MODEL=openai/text-embedding-3-small
CARDRAG_EMBEDDING_DIMENSION=1536
CARDRAG_OCR_PROVIDER=codex-exec
CARDRAG_OCR_MODEL=gpt-5.4
CARDRAG_OCR_REASONING_EFFORT=high
CARDRAG_OCR_CACHE_EPOCH=0
CARDRAG_OCR_PROMPT_VERSION=cardrag-ocr.ko.v1
CARDRAG_OCR_CHUNK_PAGES=2
CARDRAG_STAGE_MAX_ATTEMPTS=4
CARDRAG_RETRY_CAP_SECONDS=30
```

Set both `CARDRAG_OCR_FALLBACK_PROVIDER` and
`CARDRAG_OCR_FALLBACK_MODEL`, or neither. Changing the OCR contract fields is
an intentional cache invalidation. The OpenRouter key is required for document
embeddings even when Codex-exec is the OCR provider.

MCP requires `CARDRAG_MCP_BEARER_TOKEN` or its `_FILE` variant. The token must
contain at least 32 characters and no whitespace. It also needs the OpenRouter
key to run the vector branch of query search; the active generation supplies
the exact embedding model. Set `CARDRAG_MCP_PUBLIC_BASE_URL` to the external
HTTPS origin used in protected PDF descriptors. Query text and tokens are not
logged.

The default OCR provider is Codex-exec. Initialize its credentials once in the
persistent Worker volume before the first run:

```bash
docker compose --env-file /etc/cardrag/worker.env \
  -f deploy/worker/compose.yaml \
  run --rm --entrypoint codex worker login --device-auth
```

## WebDAV preflight

Before uploading production data, run the capability check. It creates and
deletes only a unique temporary prefix and verifies `PROPFIND`, `MKCOL`, `PUT`,
`GET`, `HEAD`, `MOVE`, `DELETE`, and overwrite rejection:

```bash
docker compose --env-file /etc/cardrag/worker.env \
  -f deploy/worker/compose.yaml \
  -f deploy/worker/compose.secrets.yaml \
  run --rm worker webdav-check
```

Append `-f deploy/worker/compose.ca.yaml` before `run` when using a private CA.
A failed check blocks cutover.

## Existing OCR adoption

Adoption is a two-runtime operation. First, run the read-only exporter in the
preserved v0.2.1 checkout/runtime with its existing PostgreSQL and
`CARDRAG_STORAGE_ROOT` configuration:

```bash
uv run --package cardrag-legacy cardrag-legacy legacy export-current-inventory \
  --output /archive/cardrag-cutover/current-published.jsonl
```

The output parent must already exist, the output must be outside
`CARDRAG_STORAGE_ROOT`, and an existing file is never overwritten. The exact DB
and CAS validations are documented in
[`V1_CURRENT_INVENTORY_EXPORT.md`](V1_CURRENT_INVENTORY_EXPORT.md).

If the preserved raw 9.51 GiB data-kit will fill gaps, validate it directly;
this path does not run the old import/finalize flow and does not write to its
SQLite databases or artifacts:

```bash
uv run --package cardrag-legacy cardrag-legacy legacy export-data-kit-inventory \
  --source /srv/cardrag-legacy/cardrag-conveyor-data \
  --output /archive/cardrag-cutover/legacy-data-kit.jsonl \
  --rejected-output /archive/cardrag-cutover/legacy-data-kit-rejected.jsonl
```

The exporter cross-checks all 1,592 master-manifest and read-only SQLite ledger
rows, requires the two inventory SQLite files to be byte-identical, and selects
the 1,567 rows marked `done` and `is_latest=1`. It validates every selected PDF;
when a row has no usable path, the raw-PDF tree is hash-scanned at most once via
a shared cache. It also verifies OCR metadata, UTF-8, hashes, page coverage, and
the exact v1 page-join form. Invalid candidate bytes are omitted from the Worker
inventory and recorded in the mandatory rejection ledger; source/control-plane
inconsistency aborts the whole export. Existing output files are never replaced.

For the archived 2026-07-12 bytes, the result is 727 reusable rows (KB 677,
Woori 50) and 840 explicit rejections (831 noncanonical joins, 9 short pages).
The source contains no signed full-file checksum manifest, so these hashes
identify the inspected archive snapshot but do not prove third-party
authenticity. Preserve the original read-only archive and the exported JSONL.

If a sealed `legacy prepare` bundle and its succeeded v0.2.1 import already
exist, `export-adoption-ledger` plus Worker `--legacy-bundle/--legacy-ledger`
remains an alternative. It is not required for the raw data-kit path above.

Next, mount the inventory and every absolute PDF/OCR path named by it read-only
at the same path inside the Worker container. Run once without `--publish` to
create reviewable receipts and a conflict/error report:

```bash
docker compose --env-file /etc/cardrag/worker.env \
  -f deploy/worker/compose.yaml \
  -f deploy/worker/compose.secrets.yaml \
  run --rm \
    --volume /archive/cardrag-cutover:/archive/cardrag-cutover:ro \
    --volume /srv/cardrag-v021:/srv/cardrag-v021:ro \
    --volume /srv/cardrag-legacy/cardrag-conveyor-data:/srv/cardrag-legacy/cardrag-conveyor-data:ro \
    worker adopt \
      --current-inventory /archive/cardrag-cutover/current-published.jsonl \
      --legacy-inventory /archive/cardrag-cutover/legacy-data-kit.jsonl \
      --receipts /var/lib/cardrag-worker/adoption-receipts.jsonl \
      --conflicts /var/lib/cardrag-worker/adoption-conflicts.json
```

Replace `/srv/cardrag-v021` with the actual preserved storage root. To fill
products absent from the current published inventory, mount the raw data-kit at
the same absolute path recorded by the exporter. For a current-only adoption,
omit `--legacy-inventory`. It must not be combined with `--legacy-bundle`.

Review both reports, then rerun the identical command with `--publish`.
Publication fails closed for malformed inventory/control data and blocking
identity conflicts. A document-level validation failure is reported and omitted
without preventing other independently verified receipts from being published.
Raw data-kit candidate failures were already removed into
`legacy-data-kit-rejected.jsonl`; all omitted documents are processed by the
normal Worker pipeline on its first run.
Current published data wins an otherwise valid current-versus-legacy product
collision; the decision remains visible in the report. Only verified OCR is
published as adopted cache. Failed candidates are left for normal OCR on the
first Worker run. A future adoption policy change requires a new policy version
and invalidates older adopted keys.

## Worker schedule and recovery

Install the repository at `/opt/cardrag`. Create the unprivileged timer user,
give it Docker access, and make the env/secrets readable by that user:

```bash
sudo groupadd --gid 10001 cardrag
sudo useradd --uid 10001 --gid 10001 --no-create-home \
  --home-dir /nonexistent --shell /usr/sbin/nologin cardrag
sudo usermod --append --groups docker cardrag
sudo install -d -o root -g cardrag -m 0750 /etc/cardrag
sudo install -o root -g cardrag -m 0640 deploy/simple.env.example /etc/cardrag/worker.env
sudo install -o root -g root -m 0644 deploy/worker/cardrag-worker.service /etc/systemd/system/
sudo install -o root -g root -m 0644 deploy/worker/cardrag-worker.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cardrag-worker.timer
systemctl list-timers cardrag-worker.timer
```

If the identity already exists, skip creation after verifying both IDs are
10001. Replace all example values before enabling the timer. Membership in the
Docker group is root-equivalent; dedicate this account to the Worker and do not
grant interactive login.

The unit runs daily at 03:00 Asia/Seoul. The local file lock makes an overlap
return a successful `already_running` result. The command emits a JSON object
containing `run_id`; retrieve it from the service log and resume a failed run:

```bash
journalctl -u cardrag-worker.service -o cat --since today

docker compose --env-file /etc/cardrag/worker.env \
  -f deploy/worker/compose.yaml \
  -f deploy/worker/compose.secrets.yaml \
  run --rm worker resume <run-id>
```

Every explicit resume refreshes issuer discovery and downloads the currently
advertised PDF bytes before it can publish. Completed OCR is reused from WebDAV,
and incomplete OCR chunks remain in `worker-state.sqlite3` and resume locally.
If the refreshed corpus is unchanged, a failed WebDAV publish retransmits the
same sealed artifact without repeating OCR or embedding. If it changed, the old
seal is superseded and only content-addressed checkpoints that still match are
reused.

After every successful or `no_change` run, Worker applies the fixed remote GC
policy under the same lock: retain three generations and give unreferenced
objects 30 days of grace. The command's structured result includes
`gc.status`, `gc.deleted`, and `gc.error`. Because publication is already
durable, a fail-closed GC error is reported without rewriting the run outcome.

The standalone GC command is available for inspection and maintenance and is
dry-run by default:

```bash
docker compose --env-file /etc/cardrag/worker.env \
  -f deploy/worker/compose.yaml \
  -f deploy/worker/compose.secrets.yaml \
  run --rm worker gc --retain 3 --grace-days 30

docker compose --env-file /etc/cardrag/worker.env \
  -f deploy/worker/compose.yaml \
  -f deploy/worker/compose.secrets.yaml \
  run --rm worker gc --apply --retain 3 --grace-days 30
```

GC fails closed on corrupt control metadata and never deletes a retained or
currently referenced object.

## MCP startup and probes

```bash
docker compose --env-file /etc/cardrag/mcp.env \
  -f deploy/mcp/compose.yaml \
  -f deploy/mcp/compose.secrets.yaml \
  up -d --wait

curl --fail http://127.0.0.1:8000/health/live
curl --fail http://127.0.0.1:8000/health/ready
```

The Compose healthcheck uses `/health/ready`, so `up --wait` completes only
after a verified local generation is serving. It returns only
`{"ready":true|false}` and returns 503 when no verified local generation is
available. Docker Compose records an unhealthy container but does not restart
it merely because a healthcheck failed; inspect logs and WebDAV connectivity if
the first synchronization cannot complete. `/health/live` remains a separate
process-liveness probe.

`/health/live` and `/health/ready` are public. `/mcp`, `/resources/*`,
`/sources/*`, and `/metrics` all require `Authorization: Bearer <token>` and
return 401 otherwise. A failed poll, incompatible model/schema, corrupt SQLite,
missing PDF, or vector matrix over 1 GiB leaves the previous generation active.
MCP serves PDF bytes only from its local CAS, including verified Range 206
responses; request handling never calls WebDAV.

Compose listens on `0.0.0.0` only inside the container and publishes to host
loopback by default. TLS termination belongs to an external reverse proxy. Do
not set `CARDRAG_MCP_BIND_ADDRESS=0.0.0.0` on an internet-facing host.

## Shadow, cutover, and rollback

Worker and MCP retain the latest three generations. Unreferenced remote CAS and
OCR cache content receives a 30-day grace period before mark-and-sweep. Never
delete the current generation or an object referenced by a retained generation.

Run the new MCP beside v0.2.1 and compare issuer/product/document counts,
deterministic fixture queries, sampled exact evidence spans, PDF hashes, and PDF
Range responses. Keep the old PostgreSQL, Keycloak, CAS, and deployment state
read-only until all of these are true:

1. seven consecutive scheduled Worker runs completed;
2. unchanged corpora caused zero OCR/embedding calls and no pointer update;
3. every MCP sync activated a complete generation or retained the previous one;
4. concurrency 5, search P95 under 1 second, and peak vector RSS under 1.5 GiB passed;
5. all adoption conflict/error reports were reviewed.

Only then decommission the v0.2.1 containers. Historical source data remains an
explicit read-only archive and is never automatically deleted by v1.
