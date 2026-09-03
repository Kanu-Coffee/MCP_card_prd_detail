# cardrag-worker

Finite CardRAG batch worker. It discovers disclosure PDFs for each explicitly
enabled issuer, downloads and verifies PDFs through one SSRF-safe downloader,
resumes OCR checkpoints, builds contract-local structure views, embeds them with
the sealed Qwen 4,096D profile, and publishes immutable
`cardrag.generation.v5`/`cardrag.serving-db.v5` SQLite plus `vectors.f32`.
The legacy v4 contracts remain available to the MCP for rollback. PDFs are
deduplicated in a local SHA-256 CAS whose source/revision
dictionary tracks renewed guides. A cached source is origin-revalidated at
least every seven days by default. Exact allowlisted SCDSA and Fasoo DRMONE
containers are exported as auditable `unsupported_drm` products; any changed
or unknown protected bytes fail the generation closed.

An exhausted document-scoped OCR error does not stop later PDFs. A generation
is published only when every enabled issuer has at least 95 percent OCR
success; successful documents alone are embedded and failed products are
explicitly exposed as `ocr_failed`. Systemic OCR, embedding, database, and
publication errors remain fail-closed.

Native OCR cache publication is phase-aware (`cas`, `manifest`, `ready`).
Timeout/network and remote protocol/proxy failures, plus HTTP
408/423/425/429/5xx, receive three total attempts with 0.25- and 1.0-second
delays. Exhaustion keeps the atomically verified local seal and returns
generation-only OCR without claiming a native cache binding; its bounded
diagnostic is
`runs/<run-id>/documents/<document-id>/ocr/native-cache-publication-diagnostic.json`.
A later `read-write` run strictly validates a partial remote manifest and OCR
CAS, publishes only the missing READY, and does not call the provider again.
The default and candidate `read-only` mode treats the partial entry as a strict
miss and never repairs it. HTTP 401/403/407,
local/unsupported protocol, immutable conflict, integrity, and contract
failures remain terminal and write the secret-safe
`runs/<run-id>/reports/ocr-systemic-failure.json` report.

The always-on MCP process is deliberately not part of this package. It downloads
the selected generation's DB, vector sidecar and CAS objects, validates them,
and opens them read-only.

```console
cardrag-worker webdav-check
cardrag-worker seed-cache-v109 /mnt/cardrag-v109-state     # candidate only
cardrag-worker run
cardrag-worker resume <run-id>
cardrag-worker gc                    # dry-run
cardrag-worker gc --apply
```

The legacy regression audit is a separate read-only module. It accepts only an
absolute, non-symlink regular file, opens SQLite with `immutable=1` and
`query_only`, and emits canonical self-hashed JSON to stdout:

```console
python -m cardrag_worker.legacy_v4_audit --database /absolute/index.sqlite3
python -m cardrag_worker.legacy_v4_audit \
  --validate-release-artifact /absolute/v109-kb-v4-structure-reaudit.json \
  --historical-artifact /absolute/v109-kb-real-regression-baseline.json \
  --historical-source-artifact /absolute/v109-structure-audit-execution.json
```

The second command binds the historical counters to the exact preserved
read-only command-execution record and parses that record's stdout again. This
is a repository-review integrity record, not an independent timestamp or a
hash snapshot of the underlying in-progress Worker run. The separate sealed-v4
DB audit remains authoritative for its database-bound corpus counts.

Issuer activation is explicit:

```console
export CARDRAG_ENABLED_ISSUERS=woori,kb,shinhan,samsung
```

The Shinhan adapter refreshes its rotating token just in time through the
official mobile disclosure endpoint, then binds product code, name, effective
date, and source version back to the stable desktop discovery record before it
downloads any bytes.

Live discovery, OCR, embeddings, and WebDAV publishing require real issuer and
provider credentials/endpoints. The v1.0.14 reliability-patch worker accepts
`candidate-v1.0.11` by default; `stable` remains blocked unless the separately
approved cutover explicitly sets `CARDRAG_STABLE_PUBLICATION_APPROVED=true`.
No provided environment or Compose file enables that flag. Shared native or
adopted OCR cache writes independently require stable channel,
`CARDRAG_OCR_CACHE_MODE=read-write`, and the separate
`CARDRAG_OCR_CACHE_PUBLICATION_APPROVED=true`; candidate forces read-only and
false. Neither approval grants the other. A
successful/no-change approved stable run performs remote GC only when the
independent deletion approval `CARDRAG_REMOTE_GC_APPROVED=true` and
`CARDRAG_COLLECT_REMOTE_GARBAGE=true` are also set. Both default to false;
enabling collection without the stable publication and deletion approvals
fails during settings validation. The Worker retains two local publication
seals and prunes unreferenced local PDF CAS bytes. Up to two failed or
interrupted run directories remain separately for diagnosis.

OpenRouter endpoint metadata and embedding batches retry transient HTTP `408`,
`425`, `429`, `5xx` (including `524` and `529`), timeout, network, proxy, remote
protocol, and response-decoding failures locally before the finite corpus stage
is restarted. The defaults are 12 attempts with exponential backoff from one
to 60 seconds; `Retry-After` is honored within that bound. Authentication,
billing, authorization, request-shape, local/unsupported protocol, and
model/provider/vector contract failures are never retried. Configure the bounded
policy with `CARDRAG_EMBEDDING_REQUEST_MAX_ATTEMPTS`,
`CARDRAG_EMBEDDING_RETRY_BASE_SECONDS`, and
`CARDRAG_EMBEDDING_RETRY_CAP_SECONDS`.

The `codex-exec` OCR child is a text-only agent boundary even when a disclosure
image contains prompt-injection instructions. Page images are attached directly
with `codex exec --image`; the tool-side `view_image` capability is therefore
disabled together with Codex 0.151.0's shell, unified exec, shell snapshot,
browser/computer, app/plugin/skill, hook, and sub-agent surfaces. The invocation
also uses `--strict-config`, `--ignore-user-config`, `--ignore-rules`,
`--sandbox read-only`, `shell_environment_policy.inherit="none"`, and disables
login shells, web search, plan, and user-input tools. A replaced Codex binary
that no longer recognizes this pinned contract fails before OCR instead of
silently restoring a tool.

The child process receives only the allowlisted locale/TLS/PATH variables and,
when configured, `CODEX_HOME` so the Codex parent can authenticate. It never
inherits CardRAG OpenRouter or WebDAV secrets. The OAuth files necessarily
remain readable to the Codex parent running as the Worker UID; the containment
claim is that untrusted OCR content has no model-callable general file-read or
command-execution route to `/run/secrets` or `CODEX_HOME`, not that a compromised
Codex executable cannot read its own authentication store.

Stable container deployments persist Codex data in the dedicated
`cardrag-worker-v111-codex-home` volume at `/var/lib/cardrag-codex-home`; the
v1.0.14 candidate overlay instead uses `cardrag-worker-v114-candidate-codex-home`.
`CARDRAG_CODEX_AUTH_ROOT` and `CODEX_HOME` equal that mount root, while `HOME`
is its owned mode-0700 `home/` child. The Worker recovery volume remains mounted
only at `/var/lib/cardrag-worker`; settings reject an auth root that is equal to,
inside, or above the Worker state path. Migration copies only `auth.json` and
does not copy Codex logs, cache, sessions, or the former general home.

As defense in depth, fresh provider stdout, resumable checkpoints, retained
local OCR, and both native/adopted remote-cache bodies are rejected systemically
before a local seal or downstream publication when they match a standard
credential token form (OpenRouter, GitHub, JWT, or long `sk-` tokens). The
typed error contains only a constant reason code; it never records the matched
value, byte offset, pattern, or surrounding OCR text.

`worker-state.sqlite3` and its revision metadata remain Worker-only recovery
state; the MCP and external progress monitors never open the live database.
Operational progress comes from the Worker's structured container/journal logs.
Candidate runs use the shared canonical WebDAV root but a separate
`candidate-v1.0.11` channel and v114 state volume, and never perform remote GC.
Unit tests use local deterministic fakes.

The v5 path has fail-closed local capacity gates; legacy v1-v4 behavior is
unchanged. Before provider or WebDAV work, the Worker checks the filesystem
containing its state path without creating the configured directory and
requires `CARDRAG_WORKER_MINIMUM_START_FREE_BYTES` free (2 GiB by default; the
candidate Compose overlay fixes release launches at 32 GiB). After all derived views and
read-only embedding-cache hits are known, but before downloading an embedding
miss, it conservatively checks the projected state and peak filesystem growth.
The SQLite forecast counts exact UTF-8 bindings and logical rows plus calibrated
FTS, secondary-index, overflow-page, and WAL envelopes; it is a fail-closed
planning estimate, not a proof of SQLite's final byte size. The exporter also
hard-limits the working database, checks the vacuumed database and vector
sidecar before either final target is installed, and removes only its own temp
artifacts on failure. A forecast above a configured cap therefore rejects the
attempt until an operator explicitly resumes with a fitting corpus or policy.
The defaults are 128 GiB for `CARDRAG_WORKER_MAX_STATE_BYTES`, 2 GiB for
`CARDRAG_WORKER_RESERVED_FREE_SPACE_BYTES`, 16 GiB for
`CARDRAG_WORKER_MAX_VECTOR_SIDECAR_BYTES`, and 32 GiB for
`CARDRAG_WORKER_MAX_SERVING_DATABASE_BYTES`. Values must be canonical decimal
integers in the supported signed-64-bit byte range. The state, sidecar, and DB
limits must be positive; the two free-space settings may be zero.

Startup walks path components with no-follow directory descriptors and scans
an existing state tree for regular files only. It seals the deepest existing
ancestor's device/inode and filesystem identity, then revalidates the created
or existing state root immediately before WorkerState opens it. The database,
WAL, any crash-leftover SHM, and lock leaves are opened with `O_NOFOLLOW` and
rechecked as regular files. Post-connect verification descriptors stay open
until after the SQLite connection closes, so their close cannot discard
process-wide POSIX record locks. A process-local path/inode registry rejects a
second `WorkerState` before it can open an active database. The Worker uses the `unix-excl` VFS: WAL
remains durable while no file-backed SHM is created or mapped, and a second
process opening the live database fails with `database is locked`. Compose runs one Worker
under its dedicated UID and the Worker lock. A hostile process already running
as that same UID that can continuously swap an ancestor or leaf during
SQLite's unavoidable pathname-open window remains outside this local gate's
threat boundary; detected replacements still fail closed.

V5 cache hit/miss metrics count derived sidecar rows. Equal exact embedding
inputs share one cache key, so the provider is called once per unique miss and
`downloads` counts unique keys (attributed to the first canonical view type),
while export still writes one 4,096D row for every derived view. Counters are
attempt-local except `embedding_provider_call_count`, which counts every actual
OpenRouter wire attempt made during the generation, including request-local and
whole-stage retries. A later explicit resume starts a new provider baseline;
already cached vectors do not add calls.

The cache forecast includes per-unique-miss persistent/WAL growth and one copy
of the sealed existing WAL allocation in case SQLite's automatic checkpoint
extends the main database. Before and after every paid provider batch, the
Worker performs a stat-only identity/size check and rejects a WAL larger than
the sealed baseline plus that attempt's predicted growth; the capacity gate
does not trigger a checkpoint or truncate operator state.

When Worker and MCP state live on the same host filesystem, capacity planning
must count their local copies separately. The Worker retains PDF/OCR and
embedding-cache bytes plus its generated DB and sidecar; the MCP independently
downloads/stages the serving DB, sidecar, and source CAS/PDF bytes. Size the
startup floor for those duplicate sidecar/PDF/DB bytes and both services'
reserved-free-space commitments. The candidate 32 GiB floor is an early guard,
not a replacement for the Worker's corpus-derived preflight or the MCP's own
state/download gates.

Qwen provider bodies are bounded before JSON parsing: embedding responses are
streamed with a 32 MiB default maximum and model-metadata responses with a 2 MiB
default maximum. Both enforce `Content-Length` when present and an incremental
body limit when absent. Configure them with
`CARDRAG_EMBEDDING_MAX_RESPONSE_BYTES` and
`CARDRAG_EMBEDDING_METADATA_MAX_RESPONSE_BYTES`; invalid or oversized values
fail closed without including provider bodies or credentials in errors.

The supported v1.0.11 data-publication path uses the v1.0.14 Worker
CLI/container. The v5
bundle publisher also denies a stable pointer move unless the caller carries
the explicit stable-publication capability; calling the primitive directly is
not an approval bypass. Legacy v1-v4 publisher behavior is unchanged.

Stable-channel GC also tracks abandoned publisher leaves only at the exact
`v1/.incoming/{publish,channels}/<32-lowercase-hex>.tmp` shape. A leaf must stay
in the unreferenced ledger for the configured grace period; GC then rechecks
the stable pointer and the leaf shape immediately before each DELETE. Unknown
or ambiguous incoming paths fail closed. Standalone `cardrag-worker gc` exits
1 with fixed structured JSON on every failure; if a later DELETE fails after
earlier successes, it reports `reason_code=remote_gc_partial_failure` and only
the known successful `deleted_count`; no raw URL, credential, response, or
exception text is printed. `cardrag-worker gc --apply` requires collection to
be enabled and both stable-publication and remote-GC approvals; dry-run remains
non-destructive.
