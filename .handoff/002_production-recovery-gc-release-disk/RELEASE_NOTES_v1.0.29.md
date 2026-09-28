# CardRAG v1.0.29

CardRAG v1.0.29 is a production-hardened stable release featuring launch-date parser v3, secure runtime defaults (local PaddleOCR-VL-1.6 fallback with external OCR disabled by default), verified state and embedding-seed recovery tooling, and multi-binding OCR cache retention.

## Highlights

### 1. Launch-Date Parser v3 & Product Metadata
- **Source-Bound Metadata**: Card launch dates are strictly derived from verified source-bound metadata and legal PDF revision headers.
- **No Speculative Defaults**: Unconfirmed launch dates are reported explicitly as `null` / `[확인 필요]` without speculative fallback inference.
- **Coverage**: Verified coverage across 4,418+ products from 8 card issuers (Shinhan, Samsung, KB, Woori, Hyundai, Hana, Lotte, BC).

### 2. Secure Runtime Defaults & Local PaddleOCR Fallback
- **PaddleOCR-VL-1.6 Integration**: Integrated isolated, local PaddleOCR-VL-1.6 as the primary / fallback provider for documents requiring visual language processing.
- **Zero External OCR Leaks**: `CARDRAG_EXTERNAL_OCR_ALLOWED=false` enforces complete network air-gapping against external OCR API providers in production.
- **Verified Offline Processing**: Successfully processed 15 complex visual layout documents via local PaddleOCR without external network calls.

### 3. State & Embedding Seed Provenance Tooling
- **`seed-state-v122`**: Validates and imports legacy lineage, documents, and historical revisions into destination state volumes under Merkle-sealed ledgers (`cardrag.state-seed-ledger.v2`).
- **`seed-embedding-cache-v122`**: Dedicated fail-closed transfer for `embedding_cache_v5` with per-row verification of dimension, dtype, L2 norm, and finite values under `audit-reports/embedding-seed/` ledgers.
- **`restore-ocr-seed`**: Bare-metal disaster recovery tool directly restoring and verifying OCR markdown artifacts from WebDAV generation manifests (5,207 documents, 4,922 unique CAS objects) into empty state volumes with 0 external OCR provider calls.

### 4. Remote Garbage Collection Multi-Binding Fix
- Resolved `GCError: retained generations disagree on OCR cache` by admitting multiple valid OCR CAS bindings per reuse key across retained generations while maintaining fail-closed integrity checks against uncommitted or divergent artifacts.

## Verified Release Artifacts

- **Source Commit**: `fdf87e602335d27b3e381e6e8648fec7fd0d0beb` (tag `v1.0.29`)
- **Worker Image Index Digest**: `sha256:80eb7aa23a51fa3f2a0c453ab71d5ed9318942581ae0eb151397114ab7581a03`
- **MCP Image Index Digest**: `sha256:53382bc35c12758b05a39bedd80d525db91ff66eff20d4e7e0731eb8905a3e30`
- **Candidate Image Repository**: `ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate`
- **Production Serving Generation**: `g-7ea0625531c447a8a3ae4368-7a7b0b5057d0` (5,207 documents, healthy on port 18015)
