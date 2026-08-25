# CardRAG

CardRAG has two runtime images and no online database:

- `cardrag-worker` runs once, downloads the current issuer PDFs, reuses or produces OCR,
  builds embeddings and an immutable SQLite generation, and publishes it to WebDAV.
- `cardrag-mcp` continuously serves the last verified local generation. It checks WebDAV
  in the background, downloads a complete generation and every referenced PDF, then
  switches atomically.

Shared hashes, manifests, OCR validation, and the WebDAV protocol live in the
`cardrag-core` workspace package. PostgreSQL, pgvector, Keycloak, and an admin service
are not dependencies of the new runtime.

```text
packages/cardrag-core   immutable artifact and OCR contracts
apps/cardrag-worker     issuer adapters and one-shot publisher
apps/cardrag-mcp        local SQLite MCP server
```

## Development

Python 3.12 and `uv` are required.

```bash
uv sync --all-packages --all-extras
uv run --all-packages pytest \
  packages/cardrag-core/tests \
  apps/cardrag-worker/tests \
  apps/cardrag-mcp/tests
```

The v0.2.1 package under `src/cardrag` and its PostgreSQL deployment files are retained
only as a read-only rollback and migration source during the seven-run shadow window.
They are not copied into either new image.

## Deployment

Build exactly the two runtime images:

```bash
docker build --target worker -t cardrag-worker:local .
docker build --target mcp -t cardrag-mcp:local .
```

Use [`deploy/worker/compose.yaml`](deploy/worker/compose.yaml) for the one-shot Worker
and [`deploy/mcp/compose.yaml`](deploy/mcp/compose.yaml) for the always-on MCP service.
File-backed Docker secrets are provided by each directory's `compose.secrets.yaml`
overlay. Full setup, WebDAV preflight, migration, shadow, and rollback instructions are
in [`docs/SIMPLE_RUNTIME.md`](docs/SIMPLE_RUNTIME.md).

The root [`compose.yaml`](compose.yaml) is a local convenience include and also resolves
to exactly the `worker` and `mcp` services. Production operators should use the two
independent Compose projects above so a one-shot Worker lifecycle cannot affect MCP.

MCP is published to `127.0.0.1:8000` by default. Put a TLS reverse proxy in front of it
and protect every MCP/resource/PDF/metrics request with the configured Bearer token.
Only `/health/live` and the detail-free `/health/ready` are unauthenticated.

The read-only v0.2.1 current-inventory exporter used during OCR adoption is documented
in [`docs/V1_CURRENT_INVENTORY_EXPORT.md`](docs/V1_CURRENT_INVENTORY_EXPORT.md).
