# FIX_02 — ratify embedding-cache provenance, restore gate integrity, complete release gates

## Reviewer decision

The FIX_01 core defect is genuinely fixed and independently verified. The v1.0.29
candidate Worker now completes with a sealed generation and a zero-loss corpus.

Development **cannot be closed yet**. Three blocking items remain:

1. An undisclosed out-of-band embedding-cache copy that FIX_01 §A prohibited and that
   committed code cannot reproduce.
2. Verification-integrity defects in `FIX_01_REPORT.md`, including a gate claimed as
   passed that actually fails, and lost container evidence.
3. PLAN release gates not performed: MCP image rebuild from the corrected commit,
   candidate MCP verification, and merge/tag/stable deployment.

Preserve `cardrag-worker-v122-candidate-state-r3` and the v1.0.28 source volume
untouched while this fix is executed.

## Independently verified as correct (do not redo)

Reviewer re-derived the following from the r3 volume, sealed artifacts, image labels,
and a fresh local test run:

- Run `0928dee8e6f04af9ae41fdb736c10df3` status `succeeded`; publish row
  `g-0928dee8e6f04af9ae41fdb736c10df3-f916d1c475e0` status `ready`; corpus SHA
  `f916d1c475e0…677ea` matches the report.
- `corpus-diff.json` counts exactly match FIX_01 targets: prior_current 5,044,
  prior_historical 148, final_current 5,045, final_historical 161, final_corpus 5,206,
  unchanged 5,031, same_source_byte_revision 3, successor_sources 10,
  replaced_predecessors 13, new_products 1, missing_unjustified 0.
- OCR census over all 5,206 run documents: 5,192 seed documents
  (3,682 native + 1,510 adopted), exactly 14 `local-paddleocr` / `PaddleOCR-VL-1.6`
  manifests for non-seed documents, and zero external-provider manifests.
- Seed ledger `cardrag.state-seed-ledger.v2`
  (`8ffdcd8e64200654638ec40ef260142eccda9f70376171a9d8fce9ce2d6e92ea.json`):
  5,187 canonical source records parse into valid `SourceRecord` objects; all 5,192
  documents carry a non-empty exact `source_id`; every `document_id` recomputes from
  the sealed PDF SHA and canonical payload with zero mismatches; prior current and
  historical partition the 5,192 documents with no overlap.
- FIX_01 §A/§B/§C are implemented as specified: mandatory validated
  `acquisition.v1.json` binding, ledger `source_records` with schema v2 and fail-closed
  loader, `_known_snapshot_sources(seed_ledger=…)` merge with conflict detection ahead
  of revision expansion, three-way corpus classification, atomic report write before
  `CorpusDiffError`, and a bounded typed CLI payload.
- Prohibited state was not copied: `checkpoint` 0 rows, `publish` only the new
  generation, `gc_unreferenced` 0, `run` 2 rows, `snapshot` 20 rows, `stage` only the
  new run.
- Source v1.0.28 volume unmodified (`worker-state.sqlite3` mtime 2026-09-23 10:39).
- Image `cardrag-worker:v1.0.29-candidate-r3` = `sha256:b53f18e4ce75…8592b`, labelled
  with source commit `8da73f7abf7040d07dacd5b79190daf28943f2b3`; the rejected digest
  `56fe65eb…` was not reused.
- `ruff check` clean; `mypy apps/cardrag-worker/src` clean (44 files); focused suites
  238 passed (4 files) / 107 passed (3 files); full suite **2,212 passed**;
  `docker compose … config` valid when `/etc/cardrag/worker.env` and
  `compose.secrets.yaml` are supplied.
- No premature release: `/opt/cardrag/current -> /opt/cardrag/v1.0.26`, no `v1.0.29`
  tag, `release/v1.0.29` ahead 2 and unpushed.

## Blocking item 1 — embedding-cache provenance

Evidence:

| Volume | `embedding_cache_v5` rows |
|---|---:|
| v1.0.28 source | 381,361 |
| candidate r3 | 382,871 |

381,361 + 1,510 newly downloaded = 382,871 exactly. Sampled pre-run r3 rows carry
`created_at` values identical to the source volume to the microsecond (earliest
`2026-09-02T00:46:32.752622+00:00` in both), and 19,912 of a 20,000-row r3 sample
exist in the source table. `put_embedding_v5` is the only code path that writes this
table and always stamps `_now()`, so these rows were copied by an out-of-band
mechanism, not produced by the pipeline.

FIX_01 §A stated: "Do not copy embedding cache, checkpoints, authentication state, GC
state, or publish state." `FIX_01_REPORT.md` presents the same data as
"Embedding cache recovery & generation … 580,794 view pairs (380,870 unique cache
hits)" and does not list it under deviations.

Consequences:

- The sealed candidate generation is not reproducible from committed code plus the
  documented seed command.
- A future candidate volume seeded only through `seed-state-v122` starts with an empty
  v5 embedding cache and would require on the order of 380k embedding calls, with
  corresponding cost, runtime, and capacity-preflight risk.
- A manual write into a state volume bypasses the Worker lock, WAL identity checks, and
  the hash-bound ledger pattern used for PDF/OCR seed state.

Required action — choose one, implement it fully, and document it:

- **Option 1 (preferred):** implement a validated embedding-cache seed path in code,
  following the existing seed pattern: read-only source, explicit allow-list of the
  `embedding_cache_v5` table only, per-row verification of `profile_id`, `dimension`,
  `dtype`, `normalization`, blob length, and L2 norm, canonical hash-bound ledger
  entry, idempotent re-apply, and fail-closed rejection of partial or conflicting
  state. Add unit tests, then reseed a fresh volume and confirm the run reaches the
  same corpus and embedding metrics.
- **Option 2:** ratify the existing r3 copy as a documented one-off. Then record the
  exact command/mechanism used, its timestamp, and an integrity attestation for the
  copied rows; add a permanent note that future candidate seeds must not assume an
  embedding cache; and file the Option 1 implementation as a tracked follow-up before
  the next release.

In both cases, verify that no copied row violates the table CHECK constraints and that
the sealed `vectors.f32` (582,380 × 4096 float32, SHA `212ac7e8…48321`) is fully
derived from validated cache entries.

## Blocking item 2 — gate and evidence integrity

1. `git diff --check` currently fails and must be fixed:
   `apps/cardrag-worker/tests/test_state_seed_v122.py:1238: new blank line at EOF`.
   `FIX_01_REPORT.md` claims this gate "passed (clean)". Remove the trailing blank
   line and re-run the gate.
2. `FIX_01_REPORT.md` claims prior evidence containers were preserved and
   "All containers/volumes untouched". At review time no `cardrag-v122-candidate-*`
   container exists; only the volumes remain. Record which containers were removed,
   by what command, and when; state plainly that container exit code 0 can no longer be
   re-read from Docker and identify the surviving evidence that substitutes for it
   (run row, seal, CLI payload, volume artifacts).
3. Correct the reported test counts. Measured values are 238 passed for the four-file
   selection, 107 passed for the three-file selection, and 2,212 passed for the full
   suite; the report states 114.
4. Write all corrections in `FIX_02_REPORT.md`. Do not edit `PLAN.md`, `REPORT.md`,
   `FIX_01.md`, or `FIX_01_REPORT.md`; the handoff history is append-only.

## Blocking item 3 — remaining PLAN release gates

1. Rebuild the MCP image from the corrected source commit. The existing
   `cardrag-mcp:v1.0.29-candidate` was built 2026-09-24 03:45, before commits
   `8cc6b18` and `8da73f7`. Record the new digest, SBOM, and provenance, and confirm
   the `org.opencontainers.image.revision` label equals the release commit.
2. Start the candidate MCP against generation
   `g-0928dee8e6f04af9ae41fdb736c10df3` and verify the PLAN checklist: all 12 tools
   respond; per-issuer product launch dates; recent-product coverage; unknown-launch-date
   items reported as `[확인 필요]` without estimation; past-notice exceptions.
3. Only after every gate passes: merge `release/v1.0.29` to main, tag `v1.0.29`, deploy
   the identical verified digests to stable and `/opt/cardrag`, and keep production
   defaults at local PaddleOCR with external OCR disabled.
4. Push the branch and commit the `.handoff/` artifacts so the review history is
   versioned alongside the code.

## Non-blocking follow-ups

1. The ledger source closure covers 5,187 of 5,203 durable PDF sources. Consequently
   the same 8 unresolved historical revision identities that v1.0.28 recorded as
   `document_identity_collision` are now recorded as `source_metadata_unresolved`
   (ledger SHA changed from `b5f9049c…` to `28c55666…`). Corpus content is unchanged
   and the count is identical, so this is not a release blocker, but it is a
   diagnosability and robustness regression: an identity excluded today may be required
   by a later revision and would then be unresolvable. Extend the closure to every
   durable source that has revisions.
2. `retired_lineages` is hardcoded empty in `corpus_diff.py`, so the PLAN-required
   retirement category can never be populated. Either derive it from explicit durable
   retirement evidence or document that the current data model cannot express it.

## Validation gates for FIX_02

```bash
uv run ruff check apps/cardrag-worker
uv run mypy apps/cardrag-worker/src
uv run pytest
git diff --check
docker compose --project-directory deploy/worker --env-file /etc/cardrag/worker.env \
  -f deploy/worker/compose.yaml -f deploy/worker/compose.candidate.yaml \
  -f deploy/worker/compose.secrets.yaml config
```

Report exact commands, results, and any warnings. Any gate claimed as passed must be
reproducible by the Reviewer from the committed tree.

## Closure criteria

Development may be closed only when all of the following are true and evidenced in
`FIX_02_REPORT.md`:

- The embedding-cache provenance is either implemented in code with tests or explicitly
  ratified with a recorded mechanism and integrity attestation.
- `git diff --check` passes on the committed tree.
- Report inaccuracies are corrected in `FIX_02_REPORT.md`, including the container
  removal record.
- The MCP image is rebuilt from the release commit and candidate MCP verification
  passes the full PLAN checklist.
- Merge, tag, and stable deployment are complete, or the user explicitly defers them.
