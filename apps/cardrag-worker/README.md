# cardrag-worker

Privileged, finite CardRAG batch worker. It discovers the current disclosure PDF
for each explicitly enabled issuer, downloads and verifies PDFs through one
SSRF-safe downloader, resumes OCR/chunk checkpoints, embeds evidence, and
publishes an immutable `cardrag.generation.v4`/`cardrag.serving-db.v4` SQLite
bundle. PDFs are deduplicated in a local SHA-256 CAS whose source/revision
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
A later run strictly validates a partial remote manifest and OCR CAS, publishes
only the missing READY, and does not call the provider again. HTTP 401/403/407,
local/unsupported protocol, immutable conflict, integrity, and contract
failures remain terminal and write the secret-safe
`runs/<run-id>/reports/ocr-systemic-failure.json` report.

The always-on MCP process is deliberately not part of this package. It only
downloads the stable generation's `index.sqlite3` and opens it read-only.

```console
cardrag-worker webdav-check
cardrag-worker cache-seed /mnt/cardrag-v108-state          # dry-run
cardrag-worker cache-seed /mnt/cardrag-v108-state --apply  # candidate only
cardrag-worker run
cardrag-worker resume <run-id>
cardrag-worker gc                    # dry-run
cardrag-worker gc --apply
```

Issuer activation is explicit:

```console
export CARDRAG_ENABLED_ISSUERS=woori,kb,shinhan,samsung
```

The Shinhan adapter refreshes its rotating token just in time through the
official mobile disclosure endpoint, then binds product code, name, effective
date, and source version back to the stable desktop discovery record before it
downloads any bytes.

Live discovery, OCR, embeddings, and WebDAV publishing require real issuer and
provider credentials/endpoints. A successful/no-change stable run performs
fail-closed remote GC (latest two generations, 30-day grace), retains two local
publication seals, and prunes unreferenced local PDF CAS bytes. Up to two failed
or interrupted run directories remain separately for diagnosis.
`worker-state.sqlite3` and its revision metadata remain Worker-only recovery
state; the MCP never reads them. Candidate runs use a separate channel, WebDAV
root, and state volume and never perform remote GC. Unit tests use local
deterministic fakes.

Stable-channel GC also tracks abandoned publisher leaves only at the exact
`v1/.incoming/{publish,channels}/<32-lowercase-hex>.tmp` shape. A leaf must stay
in the unreferenced ledger for the configured grace period; GC then rechecks
the stable pointer and the leaf shape immediately before each DELETE. Unknown
or ambiguous incoming paths fail closed. Standalone `cardrag-worker gc` exits
1 with fixed structured JSON on every failure; if a later DELETE fails after
earlier successes, it reports `reason_code=remote_gc_partial_failure` and only
the known successful `deleted_count`; no raw URL, credential, response, or
exception text is printed.
