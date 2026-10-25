# FIX_06_REPORT: Common Issuer Attachment Policy & Unsupported Archive Isolation

- **Task**: `017_issuer-incremental-acquisition-review`
- **Role**: Executor
- **Status**: Complete
- **Date**: 2026-10-19
- **Reference**: `.handoff/017_issuer-incremental-acquisition-review/FIX_06.md` (superseding and retracting `FIX_05.md`)

---

## 1. Executive Summary & Policy Retraction

`FIX_06.md` fully retracted and superseded the temporary directive in `FIX_05.md` regarding unzipping or extracting child PDFs from ZIP archives. Under `FIX_06`:
1. **Zero Archive Parsing / Zero Unzipping**: ZIP files are treated strictly as **unsupported attachments** (`unsupported_attachment`). No `zipfile` extraction, child PDF parsing, or synthetic member handling is performed anywhere in the pipeline.
2. **General Multi-Issuer Policy**: The behavior is implemented as a company-wide common attachment policy across all card issuers (Lotte, Shinhan, KB, Hyundai, Woori, etc.) without hardcoding specific card products, document IDs, filenames, or content hashes.
3. **Resource Waste Prevention**: Explicit ZIP attachments (via filename, URL, or metadata) bypass body download entirely (0 network GETs, 0 OCR, 0 embeddings). Ambiguous URLs returning ZIP content (detected via MIME `application/zip` or ZIP magic signatures `PK\x03\x04`, `PK\x05\x06`, `PK\x07\x08`) stream and measure payload size safely, isolate the error, and allow sibling downloads to continue uninterrupted.
4. **Prior Document Preservation & Zero-Loss Retirement Freeze**: For existing card products that previously had verified PDF documents, a new ZIP attachment does not overwrite or destroy prior corpus documents. All discovered items (including unsupported attachments) are recognized as active lineages in `discovered_lineages`, preventing erroneous retirement or retirement-candidate marking.
5. **Transparency & Degraded Status Exposure**: The pipeline surfaces `unsupported_attachment_count` and sets `collection_status="degraded"` (or reports in `acquisition.json`), ensuring operational visibility without falsely claiming 100% complete acquisition.
6. **Resilience & Recovery**: Unchanged ZIP attachments are skipped via negative cache; once an issuer re-publishes a standard `.pdf`, the pipeline strictly re-acquires and processes the new document.

---

## 2. Implementation Details

### 2.1 Contracts (`contracts.py`)
- **`SourceRecord`**:
  - Relaxed validation to permit `.zip` file extensions alongside `.pdf` (`file_name.casefold().endswith((".pdf", ".zip"))`).
  - Added helper properties:
    - `@property def is_pdf(self) -> bool`: returns `True` if `.pdf`.
    - `@property def is_zip(self) -> bool`: returns `True` if `.zip`.

### 2.2 Downloader & Attachment Validation (`downloader.py`)
- **`UnsupportedAttachmentError`**:
  - Subclassed from `PDFValidationError` to isolate non-PDF attachments cleanly.
- **Sniffing & Early Termination**:
  - Magic byte detection: `validate_pdf` checks for PKZip magic signatures (`b"PK\x03\x04"`, `b"PK\x05\x06"`, `b"PK\x07\x08"`). If found, raises `UnsupportedAttachmentError("ZIP archive attachment is not supported")`.
  - Content-Type check: `SecurePDFDownloader.download` inspects response headers for `application/zip` and `application/x-zip-compressed`, reads and measures bytes for isolation metrics, and immediately raises `UnsupportedAttachmentError`.

### 2.3 Issuer Adapters (e.g., `issuers/lotte.py`)
- **Common Discovery**:
  - Updated URL builder `_pdf_url(file_name)` to support `.zip` extensions alongside `.pdf`.
  - Updated `parse_listing_page`: adds `"attachment_type": "zip"` to metadata only when `file_name.casefold().endswith(".zip")`, strictly preserving canonical bytes and `source_id` calculation for standard `.pdf` records to prevent invalidating historical `SOURCE_UNAVAILABLE_ALLOWANCES`.

### 2.4 Acquisition Layer (`acquisition.py`)
- **Storage & State Record**:
  - Added `unsupported_attachments` tracking to `AcquisitionSource` and `write_acquisition()`.
  - Records structured payload (`source_id`, `issuer`, `file_name`, `url`, `reason`, `format`, `sha256`, `size_bytes`) into `acquisition.json`.
  - Sets `unsupported_attachment_count` and marks `collection_status="degraded"` when unsupported attachments are present.

### 2.5 Corpus Diff & Retirement Prevention (`corpus_diff.py`)
- **`discovered_lineages` Parameter**:
  - Added optional `discovered_lineages: frozenset[LineageKey] | None = None` parameter to `generate_corpus_diff_report()`.
  - Active lineages passed to `retirement_resolver` include all discovered items (including unsupported attachments), ensuring that existing valid card products are never treated as disappeared, missing, or eligible for retirement candidates.

### 2.6 Worker Pipeline Orchestration (`pipeline.py`)
- **Attachment Classification & Body Download Bypass**:
  - Implemented `_is_explicit_zip(source)` to detect `.zip` extensions from filename, URL, or metadata.
  - In `acquire_source`, if explicit ZIP is detected, immediately yields `_UnsupportedAttachment(source, reason="unsupported_archive", format="zip", ...)` without issuing any network request or invoking OCR/embedding providers.
  - Negative cache stores `format="zip"` and bypasses repeated body fetches during rechecks.
  - Catches `UnsupportedAttachmentError` from content sniffing or header inspection, isolating the error to that specific item while sibling tasks proceed concurrently.
- **Prior Verified Document Preservation**:
  - In `pdf_revision_expansion_seconds`, prior verified documents are retained for items whose current attachment is an unsupported archive. Newly discovered products with unsupported attachments simply have no document in the corpus, preventing any generation of fake or corrupt artifacts.
- **Reporting & Pipeline Outcomes**:
  - Surfaced `unsupported_attachment_count` on `PipelineResult`.
  - Set `collection_status="degraded"` if `unsupported_attachment_count > 0` or if any issuer collection failed.
  - Added class default attributes `_unsupported_attachment_count: int = 0` and `_unsupported_count: int = 0` on `WorkerPipeline`, and initialized them in `SealedPublicationResumer.__init__` to ensure sealed publication resume remains robust without unexpected attribute errors.

---

## 3. Verification & Test Evidence

### 3.1 Targeted Test Suite (`apps/cardrag-worker/tests/test_017_scope.py`)
All 42 test cases pass cleanly (100%):
1. **`test_zip_attachment_multiple_fixtures_no_unzip_spy`**:
   - Parametrized over 4 distinct fixtures across issuers (`lotte`, `shinhan`, `kbcard`).
   - Monitored with `zipfile.ZipFile` monkeypatch spy raising on any call. Proves zero ZIP inspection or member extraction occurs.
   - Proves 0 body downloads (0 network requests) when `.zip` extension is explicit in metadata.
2. **`test_mixed_pdf_and_zip_pipeline_execution`**:
   - Pipeline runs with 1 standard PDF and 1 ZIP attachment.
   - Verified:
     - PDF successfully processed, OCR'd, embedded, and published.
     - ZIP attachment isolated as unsupported attachment (0 OCR calls, 0 embeddings).
     - `result.unsupported_attachment_count == 1`.
     - `result.collection_status == "degraded"`.
     - `result.document_count == 1`.
3. **`test_sniffed_zip_signature_isolation_and_sibling_continuation`**:
   - Ambiguous URL `.bin` returning `PK\x03\x04` ZIP magic header.
   - Verified that `UnsupportedAttachmentError` is caught, measured bytes are recorded, and sibling `.pdf` succeeds concurrently.
4. **`test_prior_document_preservation_and_retirement_freeze`**:
   - Existing card product with verified prior document encounters a ZIP replacement in recurrence.
   - Verified:
     - Prior document is retained in active corpus (`document_count == 1`).
     - Card lineage is NOT marked as retirement candidate or retired (`retirement_candidate_count == 0`, `retired_count == 0`).
5. **`test_recheck_and_recovery_to_pdf`**:
   - First run discovers `.zip` -> cached negatively.
   - Recheck with unchanged metadata -> 0 network calls.
   - Subsequent run replaces attachment with valid `.pdf` -> strictly re-acquired, verified, and updated in corpus.
6. **`test_collect_and_process_phase_handoff_with_unsupported_attachment`**:
   - Verified phase split execution (`--phase collect` followed by `--phase process`).
   - `acquisition.json` serializes `unsupported_attachments`, and `process` phase properly reads and preserves state.
7. **`test_lotte_adapter_discovers_zip_records`**:
   - Verifies Lotte adapter parses `.zip` links alongside `.pdf` links.

### 3.2 Publication Resume & Seal Tests (`test_seal.py`)
- All 27 tests in `test_seal.py` passed cleanly (100%), including publication-only resume tests (`test_publication_only_resume_*`).

### 3.3 Full Test Suite Regression (`apps/cardrag-worker/tests/`)
- Total tests executed: **1,355**
- Passed: **1,355 (100%)**
- Execution time: 49.71s
- Zero test regressions across the entire worker repository.

### 3.4 Code Quality & Security Verification
- **Linter**: `uv run --no-sync ruff check apps/cardrag-worker/` -> **All checks passed!**
- **Formatter**: `uv run --no-sync ruff format --check apps/cardrag-worker/` -> **123 files already formatted.**
- **Git diff**: `git diff --check` -> **Clean (no trailing whitespace or whitespace errors).**
- **Gitleaks**: `gitleaks detect --no-git -v --source apps/cardrag-worker/src/cardrag_worker/` -> **No leaks found.**

---

## 4. Summary of Changed Files

| File | Changes Made |
| :--- | :--- |
| `contracts.py` | Added `.zip` support in `SourceRecord`, added `is_pdf` and `is_zip` properties. |
| `downloader.py` | Added `UnsupportedAttachmentError`, PKZip magic detection, Content-Type checks. |
| `issuers/lotte.py` | Allowed `.zip` in `_pdf_url`, added attachment type in discovery without altering `.pdf` canonical bytes. |
| `acquisition.py` | Added `unsupported_attachments` payload and counting to `AcquisitionSource` and `write_acquisition()`. |
| `corpus_diff.py` | Added `discovered_lineages` parameter to preserve active lineages in retirement analysis. |
| `pipeline.py` | Skipped body fetch for explicit ZIPs, isolated sniffed ZIPs, preserved prior documents, exposed degraded status and unsupported counts, provided safe class defaults for `SealedPublicationResumer`. |
| `tests/test_017_scope.py` | Added 10 comprehensive tests covering multi-issuer zero-unzip spy, mixed pipelines, sniffing isolation, preservation, recovery, and handoff. |

---

## 5. Conclusion

All requirements of `FIX_06.md` have been fully implemented, rigorously tested with a zero-unzip spy and cross-issuer fixtures, and verified against the full 1,355-test suite without regression. Handoff artifact `FIX_06_REPORT.md` is complete.
