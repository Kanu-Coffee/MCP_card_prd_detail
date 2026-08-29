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
A later run strictly validates a partial remote manifest and OCR CAS, publishes
only the missing READY, and does not call the provider again. HTTP 401/403/407,
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
  --historical-artifact /absolute/v109-kb-real-regression-baseline.json
```

The second command remains fail-closed while the historical Worker-run source
artifact has no independently preserved SHA-256 binding.

Issuer activation is explicit:

```console
export CARDRAG_ENABLED_ISSUERS=woori,kb,shinhan,samsung
```

The Shinhan adapter refreshes its rotating token just in time through the
official mobile disclosure endpoint, then binds product code, name, effective
date, and source version back to the stable desktop discovery record before it
downloads any bytes.

Live discovery, OCR, embeddings, and WebDAV publishing require real issuer and
provider credentials/endpoints. The v1.0.10 worker accepts
`candidate-v1.0.10` by default; `stable` remains blocked unless the separately
approved cutover explicitly sets `CARDRAG_STABLE_PUBLICATION_APPROVED=true`.
No provided environment or Compose file enables that flag. A
successful/no-change approved stable run performs remote GC only when the
independent deletion approval `CARDRAG_REMOTE_GC_APPROVED=true` and
`CARDRAG_COLLECT_REMOTE_GARBAGE=true` are also set. Both default to false;
enabling collection without the stable publication and deletion approvals
fails during settings validation. The Worker retains two local publication
seals and prunes unreferenced local PDF CAS bytes. Up to two failed or
interrupted run directories remain separately for diagnosis.
`worker-state.sqlite3` and its revision metadata remain Worker-only recovery
state; the MCP never reads them. Candidate runs use a separate channel, WebDAV
root, and state volume and never perform remote GC. Unit tests use local
deterministic fakes.

Qwen provider bodies are bounded before JSON parsing: embedding responses are
streamed with a 32 MiB default maximum and model-metadata responses with a 2 MiB
default maximum. Both enforce `Content-Length` when present and an incremental
body limit when absent. Configure them with
`CARDRAG_EMBEDDING_MAX_RESPONSE_BYTES` and
`CARDRAG_EMBEDDING_METADATA_MAX_RESPONSE_BYTES`; invalid or oversized values
fail closed without including provider bodies or credentials in errors.

The supported v1.0.10 publication path is the Worker CLI/container. The v5
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
