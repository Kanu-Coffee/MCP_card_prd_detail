# FIX_01 — historical lineage seed closure and diagnosable corpus-diff failure

## Reviewer decision

The implementation is not release-ready. The latest candidate Worker
`cardrag-v122-candidate-worker-v1029-r2` exited with code 1 after PDF acquisition.
The fail-closed corpus guard correctly stopped the run, but the seed implementation
did not preserve enough canonical source metadata to rematerialize the historical
corpus, and the CLI hid the typed failure behind `worker_unexpected_failure`.

Do not rerun from `cardrag-worker-v122-candidate-state-r2`. Preserve that volume and
the failed container as evidence. Implement this fix, build a fresh image, and seed a
new empty candidate state volume from the read-only v1.0.28 source volume.

## Evidence reviewed

- `PLAN.md`, `REPORT.md`, current Git status/diff, and all existing handoff artifacts.
- Candidate container state:
  - started: `2026-09-25T00:55:45Z`
  - finished: `2026-09-25T01:21:46Z`
  - exit code: `1`
  - OOM: `false`
- The failed run row `5186b7d4f64b4dc5bbee03ce3ea5729a` stores:

  ```text
  Corpus diff check failed: 141 seed documents disappeared without justification
  ```

- Acquisition itself completed:
  - discovered inputs: 5,053
  - successful PDFs: 5,045
  - protected/unsupported: 8
  - force revalidation: enabled
  - newly observed current document identities: 14
  - same-source byte revisions logged: 3
- Reproducing revision planning from the failed state produced:
  - prior current missing: 0
  - prior historical missing: 141
  - planned corpus before the guard: 5,065
  - historical revision identities unresolved for missing source metadata: 136
- The remaining 5 missing historical documents have an incorrect `source_id` in the
  seed ledger. `build_state_seed_v122_plan` derives `source_id` with
  `pdf_to_sources[pdf_sha][0]`; these five PDFs are shared by more than one source.
  The sealed acquisition checkpoint already contains the exact source ID and was
  ignored.
- Original v1.0.28 state versus failed destination:

  | State | Snapshots | Known snapshot source IDs | PDF sources | PDF revisions |
  |---|---:|---:|---:|---:|
  | source | 135 | 5,214 | 5,203 | 5,208 |
  | r2 after failure | 20 | 5,069 | 5,214 | 5,222 |

- Of 138 unique historical source IDs represented by the current seed ledger, all
  138 exist in the original snapshot history, but only 10 are recoverable from the
  destination snapshot history.
- `runs/5186.../reports/corpus-diff.json` was not written. The implementation raises
  `CorpusDiffError` before constructing/persisting the failure report.
- The pipeline recognizes `CorpusDiffError` as a typed terminal failure, but the CLI
  does not. It falls through to the generic exception branch and emits only:

  ```json
  {
    "reason": "Worker pipeline failed unexpectedly.",
    "reason_code": "worker_unexpected_failure",
    "status": "failed"
  }
  ```

- Focused tests still pass (`99 passed in 2.13s`), demonstrating a coverage gap:
  the seed fixture marks a document historical while retaining its source metadata
  in the terminal fixture, and every fixture PDF SHA maps to only one source.

## Root causes

### 1. Incomplete canonical source-metadata closure

`build_state_seed_v122_plan` copies only snapshots belonging to the terminal publish
run, while `plan_revision_history_v5` requires canonical `SourceRecord` payloads from
snapshot history to reconstruct historical documents. PDF source/revision rows alone
intentionally cannot invent product name, effective date, category, filename, or
metadata. Consequently 136 historical documents become
`source_metadata_unresolved` and are omitted from the acquired corpus.

### 2. Ambiguous PDF-to-source binding in the seed ledger

The seed builder associates an OCR document with the first revision sharing its PDF
SHA. PDF SHA is not a unique source identity. The v1.0.28 sealed
`acquisition.v1.json` already binds every `document_id` to its exact `source_id`, PDF
SHA, size, page count, and historical flag. Ignoring that binding corrupted five
historical ledger entries.

### 3. Corpus-diff classifications are incomplete

`successor_sources` and `retired_lineages` are always empty. Every recognized
replacement is currently appended to `same_source_byte_revisions`, even when the
source ID changed. `prior_by_product` is built but not used meaningfully because the
ledger does not carry the required product/source metadata. This does not explain the
141-document stop, but it prevents the report from satisfying `PLAN.md`.

### 4. Typed failure observability is broken

The corpus report is not durably written on failure, and `CorpusDiffError` is not
translated into a bounded structured CLI payload. The run DB retained the real
reason, but operators only saw a generic unexpected failure.

### 5. The executor report overstates completion

`REPORT.md` records only detached startup/running status and concludes that the work
completed without defects. The final Worker result was not yet available. Preserve
the original report as history; correct the record in `FIX_01_REPORT.md` with final
runtime evidence.

## Required corrections

### A. Build and seal the exact source-metadata closure

Update `state_seed_v122.py` so every document from the sealed acquisition checkpoint
is bound to its exact source and canonical source payload.

1. Treat the sealed `acquisition.v1.json` as required input when it is present for
   the selected generation. Validate its schema, run ID, contract/corpus identity or
   payload hash as applicable, document count, and uniqueness.
2. For every manifest/acquisition document, require exact agreement for:
   `document_id`, `source_id`, PDF SHA, PDF size, page count, and historical status.
   Do not infer source ID from PDF SHA and do not use first-match behavior.
3. Search the complete read-only canonical snapshot history for the referenced
   source IDs. Restore and validate each `SourceRecord`; require:
   - `record.source_id == acquisition.source_id`
   - `record.document_id(pdf_sha256) == acquisition.document_id`
   - its durable PDF source/revision identity exists and matches
   - duplicate snapshot observations of one source ID have identical discovery
     payloads
4. Fail closed on a missing, ambiguous, conflicting, or non-canonical source record.
5. Persist the minimal complete set of canonical source payloads required by the
   5,192 seeded documents in the hash-bound seed ledger, or replay an equivalently
   validated snapshot/run closure. The preferred implementation is a dedicated
   source-record section in the ledger; it avoids importing unrelated historical run
   state solely to satisfy snapshot foreign keys.
6. Bump the ledger schema version because the new source mapping and metadata are
   release-critical. Reject an old/incomplete ledger rather than silently loading it
   as complete.
7. Extend `StateSeedLedger` and its loader to validate the new source records and
   exact document/source bindings on every load.
8. Move seed-ledger loading ahead of revision expansion in `pipeline.py`, and merge
   its validated source records into `_known_snapshot_sources`. Any disagreement
   with live/current snapshots must fail closed.

The source volume, remote OCR cache, and stable publication remain read-only. Do not
copy embedding cache, checkpoints, authentication state, GC state, or publish state.

### B. Make corpus-diff semantics complete and evidence-producing

Update `corpus_diff.py` and its caller.

1. Classify a new current document as:
   - `same_source_byte_revision` only when the exact source ID existed previously
     with a different PDF SHA;
   - `successor_source` when a new source ID replaces a prior source/product lineage
     through validated supersession/history evidence;
   - `new_product` only when no prior product lineage exists.
2. Populate `retired_lineages` only from explicit durable retirement/supersession
   evidence. Do not treat unexplained disappearance as retirement.
3. Preserve all prior historical documents. A historical document missing from the
   acquired corpus remains a hard failure unless a separately specified and tested
   policy explicitly permits removal.
4. Construct and atomically write `corpus-diff.json` before raising the fail-closed
   exception. The report must include the full canonical missing list and bounded
   counts; logs/CLI may expose only a bounded summary.
5. Include enough metadata in the report to independently verify category decisions
   without source URLs or secrets.

For the evidence captured on 2026-09-25, the corrected run should resolve the 141
historical omissions and produce the planned totals: prior current 5,044, prior
historical 148, final current 5,045, final historical 161, final corpus 5,206,
replaced predecessors 13, and new products 1. The observed three same-source byte
revisions imply ten successor-source replacements for this snapshot. If origins
change before rerun, recompute and seal the categories, but do not relax the no-loss
invariant.

### C. Preserve typed corpus-diff failure through the CLI

Add a safe typed terminal error contract carrying at least run ID, reason code,
report-relative path, and missing count. The CLI must emit a bounded payload such as
`corpus_diff_missing` rather than `worker_unexpected_failure`. Do not echo raw URLs,
remote response bodies, credentials, or an unbounded document list.

The run row, `corpus-diff.json`, performance report, container logs, and CLI payload
must agree on the terminal status and run ID.

### D. Correct deployment and handoff evidence

1. Preserve `cardrag-v122-candidate-worker-v1029-r2` and
   `cardrag-worker-v122-candidate-state-r2` until the corrected run is sealed.
2. Build a fresh Worker image from the corrected source and record its source commit
   and immutable digest. Do not reuse digest
   `sha256:56fe65eb737d35260958ddc733de75354f11306515bf666dea5976c0e411f8b1`.
3. Create a new empty candidate state volume (for example `...-r3`), then perform
   dry-run and apply from `cardrag-worker-v114-candidate-state:ro`.
4. Do not resume run `5186...`; its state was intentionally mutated by acquisition
   and is evidence, not a clean retry base.
5. Record all corrections and final runtime results in `FIX_01_REPORT.md`. Do not
   overwrite `PLAN.md`, `REPORT.md`, or this review artifact.

## Required tests

Add tests that fail against the current implementation and pass only after the
correction.

1. A sealed seed fixture where a historical document's `SourceRecord` exists only in
   an earlier snapshot/run, not in the terminal publish snapshots. After seed apply,
   revision expansion must materialize it as historical.
2. Two different sources/products sharing one PDF SHA. The acquisition checkpoint's
   source IDs must bind both document identities exactly; ordering revisions must not
   affect the result.
3. Missing, ambiguous, conflicting, or tampered acquisition-to-snapshot source
   mappings must fail closed during seed planning/loading.
4. A reduced end-to-end fixture covering unchanged current, prior historical,
   same-source byte revision, successor source, new product, protected document, and
   explicit retirement evidence. Assert all category counts and final union.
5. A corpus disappearance test that asserts `corpus-diff.json` exists and is
   canonical even though the run fails.
6. CLI tests for the typed corpus-diff payload, including secret/raw-content
   redaction and report path/run ID.
7. Schema compatibility tests proving incomplete old ledgers are rejected or handled
   by an explicit, fully validated migration path.
8. Seed idempotence, source immutability, destination collision, WAL/SHM, symlink,
   path traversal, and source database mutation tests must continue to pass.

## Validation gates

Run at minimum:

```bash
uv run ruff check apps/cardrag-worker
uv run mypy apps/cardrag-worker/src
uv run pytest apps/cardrag-worker/tests/test_state_seed_v122.py \
  apps/cardrag-worker/tests/test_corpus_diff.py \
  apps/cardrag-worker/tests/test_cli_settings_provider.py \
  apps/cardrag-worker/tests/test_pipeline.py
uv run pytest
docker compose --project-directory deploy/worker \
  -f deploy/worker/compose.yaml \
  -f deploy/worker/compose.candidate.yaml config
```

Also run `git diff --check`. If shell/Compose files change, run the applicable lint or
configuration validation. Report exact command results and any warnings.

## Runtime acceptance criteria

Before starting the corrected Worker, verify on the new volume:

- all 5,192 sealed documents have an exact acquisition `source_id`;
- all 5,192 document IDs recompute from the sealed PDF SHA and canonical source
  payload;
- all 148 prior historical document identities are materializable before OCR;
- seed apply is idempotent on its second pass;
- external OCR remains disallowed and both publication approvals remain false.

After completion, require:

- container exit code 0 and terminal run status `succeeded` or the expected successful
  no-change status;
- `corpus-diff.json` exists and reports no unexplained missing seed documents;
- final current/historical/corpus counts are 5,045 / 161 / 5,206 for the captured
  origin snapshot, or an explicitly reconciled newer snapshot;
- all 5,192 prior OCR documents are seed hits;
- only the 14 genuinely new document identities use PaddleOCR for the captured
  snapshot;
- Codex/OpenRouter/external OCR provider calls are 0;
- the source v1.0.28 volume fingerprint and remote read-only namespaces are unchanged;
- no stable pointer, candidate pointer, or shared OCR cache is published before every
  gate passes.

Only after these gates pass may the Executor proceed to candidate MCP verification
and release review.
