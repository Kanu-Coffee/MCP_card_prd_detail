# cardrag-worker

Privileged, finite CardRAG batch worker. It discovers the current disclosure PDF
for each explicitly enabled issuer, downloads and verifies PDFs through one
SSRF-safe downloader, resumes OCR/chunk checkpoints, embeds evidence, and
publishes an immutable `cardrag.generation.v3`/`cardrag.serving-db.v3` SQLite
bundle. Exact allowlisted SCDSA and Fasoo DRMONE containers are exported as
auditable `unsupported_drm` products; any changed or unknown protected bytes
fail the generation closed.

The always-on MCP process is deliberately not part of this package. It only
downloads the stable generation's `index.sqlite3` and opens it read-only.

```console
cardrag-worker webdav-check
cardrag-worker run
cardrag-worker resume <run-id>
cardrag-worker gc                    # dry-run
cardrag-worker gc --apply
```

Issuer activation is explicit:

```console
export CARDRAG_ENABLED_ISSUERS=woori,kb,shinhan
```

The Shinhan adapter refreshes its rotating token just in time through the
official mobile disclosure endpoint, then binds product code, name, effective
date, and source version back to the stable desktop discovery record before it
downloads any bytes.

Live discovery, OCR, embeddings, and WebDAV publishing require real issuer and
provider credentials/endpoints. A successful/no-change daily run also performs
fail-closed remote GC (latest three generations, 30-day grace) and reports its
result separately. `worker-state.sqlite3` and the last three local publication
seals remain Worker-only recovery state; the MCP never reads them. Unit tests
use local deterministic fakes.
