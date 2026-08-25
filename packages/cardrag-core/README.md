# cardrag-core

`cardrag-core` is the small shared contract package used by the CardRAG Worker
and the read-only MCP service. It owns canonical hashing, strict artifact
manifests, safe WebDAV paths, verified WebDAV transfers, and OCR reuse
validation. It deliberately contains no crawler, OCR provider, database
query, or MCP protocol implementation.

The WebDAV publication contract is immutable and `READY`-last:

- `v1/channels/stable.json`
- `v1/generations/<generation-id>/manifest.json`
- `v1/generations/<generation-id>/READY.json`
- `v1/generations/<generation-id>/index.sqlite3`
- `v1/objects/sha256/<first-two>/<sha256>`
- `v1/ocr-cache/{native|adopted}/<first-two>/<reuse-key>/{manifest.json,READY.json}`

The Worker uses `WebDAVClient` and the publishers. The MCP service should use
only `MCPArtifactReader`, which exposes verified read/download operations and
does not expose mutation methods.
