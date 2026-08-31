# cardrag-mcp

Read-only FastAPI and Streamable HTTP MCP service for immutable
`cardrag.serving-db.v2`, `cardrag.serving-db.v3`, `cardrag.serving-db.v4`, and
`cardrag.serving-db.v5` generations. A background
WebDAV updater verifies a complete SQLite generation and every referenced PDF
CAS object before an atomic local activation. Request handlers pin the selected
generation, so an activation never mixes data within a request. Schema v3 adds
the bounded Fasoo DRMONE `unsupported_drm` value. Schema v4 additionally exposes
an OCR-failed product with its verified PDF and bounded failure disposition but
no OCR pages or evidence. Schema v5 adds contract revision trees, source coverage
ledgers, profile-bound Qwen 4,096D vectors in a read-only FP32 mmap sidecar, and
exact exhaustive scoring diagnostics. Older schemas remain readable for
dual-read and rollback.

The local generation/PDF CAS retention default is two. `CARDRAG_CHANNEL`
selects an isolated, traversal-safe WebDAV pointer; production uses `stable`
and the v1.0.11 candidate uses `candidate-v1.0.11`.

Vector capacity has three fail-closed bounds. The backward-compatible
`CARDRAG_MCP_MAX_VECTOR_BYTES` limits v1-v4 inline matrices and remains the
fallback for the resident limit. `CARDRAG_MCP_MAX_VECTOR_SIDECAR_BYTES`
(default 16 GiB) limits each verified v5 mmap file, while
`CARDRAG_MCP_MAX_RESIDENT_VECTOR_BYTES` (default/fallback 1 GiB) limits
heap-backed v1-v4 matrices plus norm arrays across active, candidate, and
pinned handles. A v5 mmap address range is not charged as eagerly resident RAM.

Disk and network capacity use separate fail-closed gates. The serving SQLite
file is capped at 4 GiB, and the declared SQLite + v5 sidecar + unique PDF
objects for one generation must fit the 32 GiB aggregate download quota before
the updater downloads any artifact. Before creating a new local object, the
updater also requires the whole state tree to remain within 64 GiB and the
filesystem to retain 2 GiB free after peak temporary growth. State accounting
accepts regular non-symlink files only; quota pressure never deletes an existing
immutable object automatically. The canonical quota policy and in-flight
reservation records are durable, counted state bytes; a restart with a different
policy fails closed, and an interrupted reservation remains charged. Policy,
lock, and reservation records live under
`audit-reports/.state-quota/`, and their regular bytes are included in the
whole-state limit. Each v2 reservation holds an exclusive flock on its exact
published inode for its complete lifetime. Startup never guesses that a record
is abandoned. The explicit
`cardrag_mcp.quota.reconcile_abandoned_state_reservations(state_root)` operator
API takes the global quota lock, revalidates canonical token/inode identity, and
removes only records whose nonblocking exclusive lease proves the creator is
gone; live reservations are left untouched and already materialized partial or
complete files remain counted by tree usage. Operators should quiesce MCP and
updater processes before invoking recovery so the returned removed-token list
is an auditable maintenance boundary. These
defaults are configured with the
`CARDRAG_MCP_MAX_SERVING_DATABASE_BYTES`,
`CARDRAG_MCP_MAX_GENERATION_DOWNLOAD_BYTES`, `CARDRAG_MCP_MAX_STATE_BYTES`, and
`CARDRAG_MCP_RESERVED_FREE_SPACE_BYTES` settings. They are independent of the
sidecar and resident-memory limits above.

Durable audit storage is independently bounded before a new unique query is
written. Primary exhaustive audits and default-off experimental map-reduce
audits share one cap: 32 unique jobs and 2 GiB total by default, with 256 MiB
per
artifact; reranker shadow jobs default to 1,024 jobs, 512 MiB total, and 8 MiB
per artifact. Existing immutable audit artifacts remain readable when a limit
is reached, and no quota performs automatic cleanup. Shared accounting includes
both job subtrees, unique root-only map claims, generation-root JSON, and the
map-reduce provider-coordination policy; zero-byte bounded slot files have zero
logical-byte charge. All count/byte/temporary-peak checks and their writes share
one cross-process quota transaction. The embedding and reranker
provider responses are streamed with a 1 MiB maximum, including an early
`Content-Length` gate and an incremental body limit before JSON parsing.

The v5 path scores every active structure view before contract aggregation. FTS
audits all active contracts and may add evidence but cannot influence dense
ordering; its enabled/status/error and global matched/additional counts are
reported separately. Product names are resolved from the SQL catalog with
Unicode NFKC, case folding, and collapsed whitespace; a unique longest match
loads that lineage's complete current contract, while zero or tied matches are
reported without guessing. Explicit lineage IDs always take precedence. Search
accepts up to 100 contracts, but sealed node and context-character budgets can
truncate only a dense-ranked suffix; an oversized full-contract context falls
back to its matched evidence bundle.

`mode=exhaustive` is a durable bounded-step job rather than one unbounded MCP
call. Each identical request resumes its generation/query-bound checkpoint and
returns `running` progress plus the stable job ID until all contracts are
scored. Partial runs expose neither bundles nor a completion artifact; only the
`complete` response does. RRF remains a v4-only compatibility path. The public
MCP surface remains the five legacy tools plus `search_contracts`,
`get_contract_bundle`, and `list_product_revisions`.

The Qwen reranker lane is disabled by default and settings validation permits it
only on `candidate-v1.0.11`. When enabled, it sends only already exact-scored
dense evidence to OpenRouter `POST /api/v1/rerank`, pins
`qwen/qwen3-reranker-8b` to Fireworks with `order` and `only`, and disables
fallbacks. Its scores never reorder or remove primary evidence. A canonical
immutable artifact bound to generation, query hash, model, provider, and the
dense-candidate hash is stored under `audit-reports/reranker-shadow/`; provider
failures are isolated into bounded shadow diagnostics and do not fail primary
search. Configure only candidate MCP env files with
`CARDRAG_RERANKER_SHADOW_ENABLED=true`; model/provider/max-count and timeout have
dedicated `CARDRAG_RERANKER_SHADOW_*` settings.

The additional long-context LLM map-reduce lane is also disabled by default and
candidate-only. Enabling it requires an explicit reasoning model, provider ID,
and SHA-256 of the gold evaluation artifact; those values, prompt policy, input
characters, per-call completion-token cap, per-job provider-call/input/output
budgets, and response-byte cap derive an immutable profile ID. The separate
`experimental_long_context_audit` tool advances at most one provider call per
poll. It reads a whole contract when bounded, otherwise sequential major
sections packed at canonical leaf boundaries; an individually oversized leaf is
deterministically subdivided at exact paragraph/character offsets. Strict JSON
rejects duplicate keys and nonstandard constants, and only byte-for-byte OCR spans at the declared
page/character offsets survive. Reduce starts only after every active contract
map completes and uses resumable, bounded hierarchical batches whose outputs
must remain subsets of accepted map spans. Jobs are generation + query hash +
profile bound, quota-limited, cancellable, and completed through an immutable
artifact. Every attempted call permanently charges its full sealed completion
cap, and durable cross-process quota reservations plus a slot policy bound state
growth and global provider concurrency. The policy is restart-immutable; a
different configured concurrency fails closed. Its bounded provider/job flock
slots are regular, non-symlink, zero-byte coordination files, while their
canonical policy bytes remain part of state quota accounting. A nonterminal job publishes a durable
generation GC root before its first ledger. Selection reads the authoritative
durable current pointer and atomically resumes the unique query/profile root
across generation rollover; a handled pre-ledger failure under the exclusive
job lease releases the exact still-unstarted root, while a hard crash
intentionally leaves a bounded,
cancelable recovery root. Lifecycle mutations share one
cross-process lock, so polling/cancellation pins that exact inactive generation
even after active-generation rollover and process restart.
If a provider returns bytes that fail strict decision parsing, those untrusted
bytes cannot form a valid receipt: the charged call remains durably ambiguous,
is never retried automatically, and subsequent start/poll calls return its
stable pending job ID so the caller can cancel it. This is an intentional
forensic boundary rather than treating invalid JSON as evidence.
This lane never mutates, reorders, removes, or augments primary exact
search results; when disabled, its provider client and ninth MCP tool are not
constructed.

`cardrag-gold-capture` is an offline/candidate-only CLI and is not imported by
the server. It produces the five evaluation lane JSONLs from hash-bound raw
provenance. Native v5 capture calls the real exact, lexical, and reranker APIs
and resumes from immutable per-query shards. The v1.0.9 and Qwen page lanes
require independently reproducible external observations with source DB/vector,
query-vector, every-row score, and complete coverage bindings; a self-asserted
lane JSONL is rejected. See `docs/V1_0_10_GOLD_EVALUATION.md` for the artifact
schemas and release revalidation commands.

The installed offline authoring helpers are `cardrag-gold-review` for the
corpus-only gold draft and loopback human-review flows,
`cardrag-gold-external-producer` for the historical v1.0.9 and Qwen page
artifacts, and `cardrag-gold-answer-artifact` for source-extractive answer
artifacts. They are not imported by the MCP server and do not expose additional
MCP tools. Their detailed operator contracts are in
[`V1_0_10_GOLD_REVIEW_TOOL.md`](../../docs/V1_0_10_GOLD_REVIEW_TOOL.md),
[`V1_0_10_EXTERNAL_GOLD_PRODUCER.md`](../../docs/V1_0_10_EXTERNAL_GOLD_PRODUCER.md),
and
[`V1_0_10_GOLD_ANSWER_ARTIFACT.md`](../../docs/V1_0_10_GOLD_ANSWER_ARTIFACT.md).

The process listens on `127.0.0.1:8000` by default.  `/health/live` and the
minimal `/health/ready` response are public; MCP, resources, metrics, and PDF
downloads require the configured static bearer token.
