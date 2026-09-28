# CardRAG v1.0.29

CardRAG v1.0.29 is a production-hardened stable release featuring launch-date parser v3, secure runtime defaults (local PaddleOCR-VL-1.6 fallback with external OCR disabled by default), and verified state and embedding-seed recovery tooling.

## Highlights (v1.0.29 Baseline @ fdf87e6)

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

---

## Operational Hotfix: v1.0.29-patch1 (Commit ffc3124)

Subsequent production operations and recovery hardening are delivered via immutable hotfix container image and tracked in `main` / `release/v1.0.29`:

### 1. WebDAV Disaster Recovery Tooling (`restore-ocr-seed`)
- Directly restores and cryptographically verifies OCR markdown artifacts from WebDAV generation manifests into empty state volumes without external OCR provider calls.
- Enforces strict generation control binding: `pointer -> READY -> manifest -> CAS`.
- Employs dedicated OCR recovery ledger contract (`cardrag.ocr-recovery-ledger.v1`) preventing false `missing_unjustified` corpus-diff aborts on initial crawlers.

### 2. Remote Garbage Collection Multi-Binding Fix
- Resolves `GCError: retained generations disagree on OCR cache` by admitting multiple valid OCR CAS bindings per reuse key across retained generations.
- Gracefully handles absent un-published OCR caches on WebDAV while maintaining 100% CAS object mark protection for active documents.

---

## Verified Artifacts & Deployment Digests

- **Tag `v1.0.29` Source Commit**: `fdf87e602335d27b3e381e6e8648fec7fd0d0beb` (immutable Git tag)
- **Hotfix `v1.0.29-patch1` Source Commit**: `ffc31247c09684efd1588f0af4751ca9fd448e9b`
- **Active Operational Worker Image Digest**:
  `ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate@sha256:25de74b83429ac588cb6e8136c7288364a1f3d9c68c19f8443178ed37ca61235`
- **Active MCP Image Digest**:
  `ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate@sha256:53382bc35c12758b05a39bedd80d525db91ff66eff20d4e7e0731eb8905a3e30`
- **Production Serving Generation**: `g-7ea0625531c447a8a3ae4368-7a7b0b5057d0` (5,207 documents, healthy on port 18015)
- **Distribution Note**: Release artifacts are distributed as container images via GitHub Container Registry (`ghcr.io`). Standalone binary release assets are not published for this repository.
