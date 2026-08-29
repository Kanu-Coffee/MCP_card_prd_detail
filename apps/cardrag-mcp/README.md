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
and the v1.0.10 candidate uses `candidate-v1.0.10`.

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
immutable object automatically. These defaults are configured with the
`CARDRAG_MCP_MAX_SERVING_DATABASE_BYTES`,
`CARDRAG_MCP_MAX_GENERATION_DOWNLOAD_BYTES`, `CARDRAG_MCP_MAX_STATE_BYTES`, and
`CARDRAG_MCP_RESERVED_FREE_SPACE_BYTES` settings. They are independent of the
sidecar and resident-memory limits above.

Durable audit storage is independently bounded before a new unique query is
written. Exhaustive jobs default to 32 jobs, 2 GiB total, and 256 MiB per
artifact; reranker shadow jobs default to 1,024 jobs, 512 MiB total, and 8 MiB
per artifact. Existing immutable audit artifacts remain readable when a limit
is reached, and no quota performs automatic cleanup. The embedding and reranker
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
only on `candidate-v1.0.10`. When enabled, it sends only already exact-scored
dense evidence to OpenRouter `POST /api/v1/rerank`, pins
`qwen/qwen3-reranker-8b` to Fireworks with `order` and `only`, and disables
fallbacks. Its scores never reorder or remove primary evidence. A canonical
immutable artifact bound to generation, query hash, model, provider, and the
dense-candidate hash is stored under `audit-reports/reranker-shadow/`; provider
failures are isolated into bounded shadow diagnostics and do not fail primary
search. Configure only candidate MCP env files with
`CARDRAG_RERANKER_SHADOW_ENABLED=true`; model/provider/max-count and timeout have
dedicated `CARDRAG_RERANKER_SHADOW_*` settings.

`cardrag-gold-capture` is an offline/candidate-only CLI and is not imported by
the server. It produces the five evaluation lane JSONLs from hash-bound raw
provenance. Native v5 capture calls the real exact, lexical, and reranker APIs
and resumes from immutable per-query shards. The v1.0.9 and Qwen page lanes
require independently reproducible external observations with source DB/vector,
query-vector, every-row score, and complete coverage bindings; a self-asserted
lane JSONL is rejected. See `docs/V1_0_10_GOLD_EVALUATION.md` for the artifact
schemas and release revalidation commands.

The process listens on `127.0.0.1:8000` by default.  `/health/live` and the
minimal `/health/ready` response are public; MCP, resources, metrics, and PDF
downloads require the configured static bearer token.
