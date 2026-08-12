# CardRAG MCP

Card-product disclosure PDFs are acquired, OCR'd and converted into versioned,
evidence-addressable search generations by an offline worker.  A separate,
read-only MCP process exposes only sealed generations over Streamable HTTP.

The authoritative product and acceptance requirements are in
[`docs/README.md`](docs/README.md).  This source tree is deliberately separate
from the read-only legacy hatch directory.

## Local development

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

For the Compose stack, first create the file-backed secrets exactly as described
in [`deploy/secrets/README.md`](deploy/secrets/README.md) and export its absolute
directory as `CARDRAG_SECRETS_DIR`.  `.env.example` documents setting names; it is
not a ready-to-run credential file.  The first Keycloak start uses the one-time
bootstrap overlay:

```bash
docker compose \
  -f compose.yaml \
  -f deploy/keycloak/bootstrap.compose.yaml \
  up -d --wait postgres keycloak
docker compose run --rm migrate
```

Create and verify the permanent Keycloak administrator, revoke the bootstrap
account, remove `keycloak_admin_password.txt`, then recreate Keycloak from base
Compose as specified in the secret guide.  Start the loopback-only application
hand-off with `docker compose up -d mcp`; it is published only at
`127.0.0.1:8000`.  Do not put credentials or corpus files in the image or Git.
The remaining real-account and host checks are in
[`docs/REAL_ENV_HANDOFF.md`](docs/REAL_ENV_HANDOFF.md).
