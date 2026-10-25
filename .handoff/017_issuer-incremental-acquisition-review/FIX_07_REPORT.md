# FIX_07_REPORT: Real Adapter ZIP Integration, Error State Inheritance, and Expiring Recheck

- **Task**: `017_issuer-incremental-acquisition-review`
- **Role**: Executor
- **Status**: Complete
- **Date**: 2026-10-19
- **Reference**: `.handoff/017_issuer-incremental-acquisition-review/FIX_07.md`

---

## 1. Executive Summary

`FIX_07.md` identified three P2 gaps in the initial implementation of the common unsupported archive policy:
1. **A1 (Real Adapter Rejection/Omission)**: Real issuer adapters (Hyundai, Woori, KB, Shinhan, Hana, Samsung, BC) either raised markup errors (`IssuerMarkupChanged`) or silently continued when encountering `.zip` attachments, preventing those products from ever reaching the common unsupported attachment policy and lineage preservation.
2. **A2 (Collect -> Process Error State Inheritance & CLI Exposure)**: In split-phase execution (`--phase collect` followed by `--phase process`), `replay_pdf_source` failed to propagate `unsupported_attachments` into `pipeline._unsupported_attachment_count`, causing the process phase to report `unsupported_attachment_count=0` and `collection_status="complete"`. Additionally, `_pipeline_result_payload` in `cli.py` lacked `unsupported_attachment_count`.
3. **A3 (Permanent Exclusion of Explicit ZIP vs. Expiring Recheck)**: Explicit `.zip` attachments bypassed the cache/audit check unconditionally, preventing any bounded origin recheck even after 100 days when the origin replaced the `.zip` with a valid PDF at the same URL.

All three issues have been fully resolved, verified against the Reviewer's independent reproduction suite (`/tmp/test_zip_review.py`), integrated into permanent test suite `test_017_scope.py` (52 passed), and validated across the full 1,365-test worker regression suite without failure.

---

## 2. Implementation Details

### 2.1 A1: Real Adapter ZIP Support Across All Issuers
Each adapter was audited and updated to recognize `.zip` attachments as valid discovery records without raising markup errors or dropping items, while strictly preserving canonical bytes for standard `.pdf` records:
- **Hyundai (`issuers/hyundai.py`)**:
  - `parse_detail`: Updated latest attachment check to `name.casefold().endswith((".pdf", ".zip"))`.
  - Added `**({"attachment_type": "zip"} if name.casefold().endswith(".zip") else {})` to metadata.
- **Woori (`issuers/woori.py`)**:
  - `parse_detail_records`: Allowed `.zip` in `file_name.casefold().endswith((".pdf", ".zip"))`.
  - Added `"attachment_type": "zip"` to metadata for zip attachments.
- **KB (`issuers/kb.py`)**:
  - `parse_listing`: Expanded attachment selector to accept `a[href$=".zip"]` and updated `_attachment_date` regex pattern to `(?=\.(?:pdf|zip)(?:$|\?))`.
  - Added `"attachment_type": "zip"` to metadata for zip attachments.
- **Shinhan (`issuers/shinhan.py`)**:
  - `parse_listing`: Retained `.zip` extension in `file_name` without suffixing `.pdf`, and added `"attachment_type": "zip"` to metadata.
  - `_validate_download_source_identity`: Updated expected file name and metadata to accept `.zip`.
- **Hana (`issuers/hana.py`)**:
  - `parse_listing`: Updated filename validation to allow `.zip` and set `"attachment_type": "zip"` in metadata.
- **Samsung (`issuers/samsung.py`)**:
  - `_semantic_guide_identity`, `_selected_pdf_attachments`, `_parse_attachment`: Supported `.zip` attachments and set `"attachment_type": "zip"` in metadata.
- **BC (`issuers/bc.py`)**:
  - `parse_listing`: Allowed `path.endswith((".pdf", ".zip"))` and added `"attachment_type": "zip"` to metadata.

### 2.2 A2: Error State Inheritance in Replay, Resume, and CLI
- **`partial_execution.py:replay_pdf_source`**:
  - When `reuse_source` is an `AcquisitionSource`:
    `pipeline._unsupported_attachment_count = len(source.unsupported_attachments)`
  - When `reuse_source` has a previous `execution-result.json`:
    Restored `pipeline._unsupported_attachment_count` from serialized execution results.
  - Ensures `_run_measured` and `_publish_sealed` preserve `unsupported_attachment_count` and `collection_status="degraded"` during the process phase.
- **`cli.py:_pipeline_result_payload`**:
  - Added `"unsupported_attachment_count": result.unsupported_attachment_count` to the operator-facing CLI payload dictionary.

### 2.3 A3: Bounded Audit Recheck for Explicit ZIP Attachments
- **`pipeline.py:acquire_source`**:
  - When `_is_explicit_zip(source)` is detected:
    1. Checks `self.origin_policy.negative(source.source_id, now=checked_at)`.
    2. Checks if an audit due is pending: `is_due = (negative is None and has_prior_record_in_origin_negative)`.
    3. If not due and not force-revalidating:
       - On initial discovery (`negative is None` and not `has_prior`): records in `origin_negative` with 90-day expiry (`known=True`) and skips body download (0 GETs).
       - On subsequent runs within 90 days: returns unexpired `_UnsupportedAttachment` with 0 origin GETs.
    4. If due (`now >= expires_at`, e.g. 100 days later) or `pdf_cache_force_revalidate`:
       - Does NOT return early; proceeds to `acquire()`.
       - Calls origin with rate limiting and download policy.
       - If origin serves a valid PDF: strict validation succeeds, ingests into cache, clears `origin_negative`, and acquires as normal document.
       - If origin still serves a ZIP: downloader detects PKZip magic bytes / MIME, raises `UnsupportedAttachmentError`, which refreshes `origin_negative` for another 90 days and returns `_UnsupportedAttachment` without unzipping.

---

## 3. Verification & Test Evidence

### 3.1 Reviewer Reproduction Suite (`/tmp/test_zip_review.py`)
Direct execution of the Reviewer's independent reproduction script:
```bash
PYTHONPATH=apps/cardrag-worker/tests uv run --no-sync pytest -c apps/cardrag-worker/pyproject.toml /tmp/test_zip_review.py -q --tb=short
```
- **Result**: **4 passed in 1.81s** (100% pass)
  - `test_process_retains_zip_error_status`: PASSED
  - `test_explicit_zip_rechecked_after_audit_window`: PASSED
  - `test_real_hyundai_zip_reaches_common_policy`: PASSED
  - `test_real_woori_zip_reaches_common_policy`: PASSED

### 3.2 Permanent Scope Test Suite (`tests/test_017_scope.py`)
Integrated all reproduction test cases and additional real adapter verification tests:
- `test_process_retains_zip_error_status`: Verified collect->process state inheritance.
- `test_explicit_zip_rechecked_after_audit_window`: Verified 100-day simulated audit recheck to origin.
- Real adapter verification for all 7 issuers:
  - `test_real_hyundai_zip_reaches_common_policy`: Hyundai `parse_detail` produces `is_zip=True`.
  - `test_real_woori_zip_reaches_common_policy`: Woori `parse_detail_records` produces `is_zip=True`.
  - `test_real_kb_zip_reaches_common_policy`: KB `parse_listing` produces `is_zip=True`.
  - `test_real_shinhan_zip_reaches_common_policy`: Shinhan `parse_listing` produces `is_zip=True`.
  - `test_real_hana_zip_reaches_common_policy`: Hana `parse_listing` produces `is_zip=True`.
  - `test_real_samsung_zip_reaches_common_policy`: Samsung `parse_listing_page` produces zip attachment.
  - `test_real_bc_zip_reaches_common_policy`: BC `parse_bc_listing` produces `is_zip=True`.
- `test_cli_payload_exposes_unsupported_attachment_count`: Verified CLI JSON payload output.
- **Result**: **52 passed in 8.28s** (100% pass)

### 3.3 Publication & Seal Suite (`tests/test_seal.py`)
- **Result**: **27 passed in 0.58s** (100% pass)

### 3.4 Full Test Suite Regression (`apps/cardrag-worker/tests/`)
- Total tests executed: **1,365**
- Passed: **1,365 (100%)**
- Execution time: 49.93s
- Zero test regressions across the repository.

### 3.5 Code Quality & Security Verification
- **Linter**: `uv run --no-sync ruff check apps/cardrag-worker/` -> **All checks passed!**
- **Formatter**: `uv run --no-sync ruff format --check apps/cardrag-worker/` -> **123 files already formatted.**
- **Git diff**: `git diff --check` -> **Clean.**
- **Gitleaks**: `gitleaks detect --no-git -v --source apps/cardrag-worker/src/cardrag_worker/` -> **No leaks found.**

---

## 4. Summary of Changed Files

| File | Changes Made |
| :--- | :--- |
| `issuers/hyundai.py` | Allowed `.zip` in `parse_detail`, added `attachment_type` metadata. |
| `issuers/woori.py` | Allowed `.zip` in `parse_detail_records`, added `attachment_type` metadata. |
| `issuers/kb.py` | Allowed `.zip` in `_attachment_date` and `parse_listing`, added `attachment_type` metadata. |
| `issuers/shinhan.py` | Allowed `.zip` in `parse_listing` and `_validate_download_source_identity`. |
| `issuers/hana.py` | Allowed `.zip` in `parse_listing`, added `attachment_type` metadata. |
| `issuers/samsung.py` | Allowed `.zip` in `_semantic_guide_identity`, `_selected_pdf_attachments`, `_parse_attachment`. |
| `issuers/bc.py` | Allowed `.zip` in `parse_listing`, added `attachment_type` metadata. |
| `partial_execution.py` | Restored `_unsupported_attachment_count` from `AcquisitionSource` or `execution-result.json` in `replay_pdf_source`. |
| `cli.py` | Exposed `unsupported_attachment_count` in `_pipeline_result_payload`. |
| `pipeline.py` | Added bounded 90-day negative cache and expiration audit recheck logic to `_is_explicit_zip(source)` in `acquire_source`. |
| `tests/test_017_scope.py` | Added tests for A1 (all issuers), A2 (collect->process and CLI), and A3 (100-day audit recheck). |

---

## 5. Conclusion

All items in `FIX_07.md` (A1, A2, A3) have been addressed, verified via the Reviewer's independent reproduction suite and permanent tests, and validated with zero regressions across all 1,365 tests. `FIX_07_REPORT.md` is complete.
