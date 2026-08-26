# cardrag-mcp

Read-only FastAPI and Streamable HTTP MCP service for immutable
`cardrag.serving-db.v2` generations.  A background WebDAV updater verifies a
complete SQLite generation and every referenced PDF CAS object before an
atomic local activation.  Request handlers pin the selected generation, so an
activation never mixes data within a request.

The process listens on `127.0.0.1:8000` by default.  `/health/live` and the
minimal `/health/ready` response are public; MCP, resources, metrics, and PDF
downloads require the configured static bearer token.
