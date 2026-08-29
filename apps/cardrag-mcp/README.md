# cardrag-mcp

Read-only FastAPI and Streamable HTTP MCP service for immutable
`cardrag.serving-db.v2`, `cardrag.serving-db.v3`, and
`cardrag.serving-db.v4` generations. A background
WebDAV updater verifies a complete SQLite generation and every referenced PDF
CAS object before an atomic local activation. Request handlers pin the selected
generation, so an activation never mixes data within a request. Schema v3 adds
the bounded Fasoo DRMONE `unsupported_drm` value. Schema v4 additionally exposes
an OCR-failed product with its verified PDF and bounded failure disposition but
no OCR pages or evidence. v2 and v3 remain readable so this MCP can be deployed
before a v4 Worker publishes.

The local generation/PDF CAS retention default is two. `CARDRAG_CHANNEL`
selects an isolated, traversal-safe WebDAV pointer; production uses `stable`
and the v1.0.9 candidate uses `candidate-v1.0.9`.

The process listens on `127.0.0.1:8000` by default.  `/health/live` and the
minimal `/health/ready` response are public; MCP, resources, metrics, and PDF
downloads require the configured static bearer token.
