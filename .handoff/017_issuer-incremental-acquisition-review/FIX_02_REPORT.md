# FIX_02 Implementation & Verification Report

- **Task**: `017_issuer-incremental-acquisition-review`
- **Role**: Executor
- **Source Handoff**: `.handoff/017_issuer-incremental-acquisition-review/FIX_02.md`
- **Date**: 2026-10-19
- **Status**: Complete

---

## 1. Summary of Completed Fixes

In response to `.handoff/017_issuer-incremental-acquisition-review/FIX_02.md`, four targeted runtime and lifecycle fixes (A1–A4) were implemented and verified with strict regression tests.

| Item | Problem Description | Resolution / Implementation Detail | Modified Files |
|---|---|---|---|
| **A1** | `--phase process --resume <run_id>` did not normalize plan against persisted `issuer-scope.json` when omitted, causing identity rejection | Restores `selected_issuers` from `issuer-scope.json` in `cli.py` during resume validation and normalizes `plan.selected_issuers` in `partial_cli.py` against `source.selected` / `saved_plan` before validating `execution-plan.json` identity. | `apps/cardrag-worker/src/cardrag_worker/cli.py`<br>`apps/cardrag-worker/src/cardrag_worker/partial_cli.py` |
| **A2** | Automatic daily rotation resume lost persisted scope, falling back to configured pool and violating `freeze_scope` invariants | In `rotation.py`, `target_run_id` checks explicit resume run and slot assignment run; restores `assignment.scope` from persisted `issuer-scope.json` before performing freeze check. | `apps/cardrag-worker/src/cardrag_worker/rotation.py` |
| **A3** | Process replay crashed with `KeyError` / `AttributeError` when manifest contained prior retained issuers no longer in configured pool | In `issuer_scope.py:preserve_inputs`, synthesized `_PreservedDummyAdapter` with full `IssuerSpec` (`code`, `display_name`, `sort_order`, `allowed_hosts`, `categories`) and `parser_version` for missing non-configured issuers so `pipeline.contract_sha256` and `_known_snapshot_sources` execute smoothly without network/OCR/embedding calls. | `apps/cardrag-worker/src/cardrag_worker/issuer_scope.py` |
| **A4** | OCR reprocess request partition and eligibility calculation was skipped when `selected == configured pool` | In `pipeline.py`, removed conditional guard so partition and eligibility calculation run unconditionally across retained generations, guarded prior manifest parsing against symlinks and malformed JSON, and added debug logging for unreadable seals. | `apps/cardrag-worker/src/cardrag_worker/pipeline.py` |

---

## 2. Regression Test Suites Added

All four reproduction test scenarios outlined in `FIX_02.md` were formalized into `apps/cardrag-worker/tests/test_017_scope.py`:

1. `test_process_explicit_resume_restores_inferred_scope`:
   - Validates that `--phase process --resume <run_id>` without explicit `--issuers` successfully restores `selected_issuers` from `issuer-scope.json` without failing execution plan identity checks.
2. `test_automatic_resume_keeps_original_roster`:
   - Validates that daily automatic rotation resume keeps the original roster when pool configuration changes mid-cycle.
3. `test_process_preserves_issuer_removed_from_pool`:
   - Validates that process replay preserves prior issuer documents even when the issuer adapter has been removed from configured pool.
4. `test_ocr_queue_excludes_retained_issuer_outside_configured_pool`:
   - Validates that OCR reprocess requests are partitioned unconditionally across retained runs and documents from unselected issuers are excluded from active run processing.

---

## 3. Verification Commands & Results

### 3.1. Targeted Scope & Pipeline Tests
```bash
uv run --no-sync pytest apps/cardrag-worker/tests/test_017_scope.py apps/cardrag-worker/tests/test_pipeline.py -q
# Result: 159 passed, 9 warnings in 6.63s
```

### 3.2. Full Worker Test Suite
```bash
uv run --no-sync pytest apps/cardrag-worker/tests/ -q --tb=short
# Result: 1339 passed, 10 warnings in 45.26s
```

### 3.3. Linting, Formatting, and Git Diff Check
```bash
uv run --no-sync ruff check apps/cardrag-worker/
# Result: All checks passed!

uv run --no-sync ruff format --check apps/cardrag-worker/
# Result: 123 files already formatted

git diff --check
# Result: Clean (exit code 0, no whitespace errors or merge markers)
```

### 3.4. Security Leak Detection
```bash
gitleaks detect --no-git -v --source apps/cardrag-worker/src/cardrag_worker/
gitleaks detect --no-git -v --source .handoff/017_issuer-incremental-acquisition-review/
# Result: 0 leaks found
```

---

## 4. Status of Open Findings & Carryovers

- **Lotte ZIP Payload Download Failure (`lotte.py`)**:
  - As noted in `FIX_02.md`, the live server 500 error on ZIP payload downloads from Lotte Card's remote endpoint is an external endpoint/network issue rather than a scope lifecycle regression.
  - The worker's resilience logic safely flags uncollected documents without crashing the pipeline, and this item remains documented as a live-environment observation rather than a code blocker for this task.

---

## 5. Conclusion & Next Steps

All items raised in `FIX_02.md` have been implemented, verified, and protected by automated regression tests. The codebase is clean, all 1339 worker tests pass, and this task is ready for final review.
