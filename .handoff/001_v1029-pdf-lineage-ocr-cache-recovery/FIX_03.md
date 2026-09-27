# FIX_03 — promote to real daily-batch production and reclaim legacy disk

## Reviewer decision

FIX_02 is accepted. All three blocking items are closed and were independently
re-verified. Two carry-over code items remain open as scheduled work, not blockers.

Additional execution **is required**, with a changed objective: this round is not
development or candidate verification. It must leave a **working daily production
batch** running under systemd, and it must **reclaim disk** by removing legacy data.
Both objectives are mandatory and are the acceptance criteria of this fix.

Do not enable `cardrag-worker.timer` until sections A1–A5 are complete and evidenced.

## FIX_02 verification (already accepted — do not redo)

Re-measured by the Reviewer on the current tree `26a993b`:

- `git diff --check` clean; `ruff check` clean; `mypy apps/cardrag-worker/src` clean
  (45 files); full suite **2,220 passed**; `test_embedding_seed_v122.py` **8 passed**.
- Option 1 delivered in code: `embedding_seed_v122.py` (21,655 bytes) plus CLI
  `seed-embedding-cache-v122`, per-row validation, hash-bound ledger, idempotent
  re-apply, destination-conflict rejection.
- Reproducibility proven on a fresh volume `cardrag-worker-v122-candidate-state-r4`
  using committed code only (state seed + embedding seed).
- Candidate MCP verification attested: 12 tools responding, generation
  `g-0928dee8e6f04af9ae41fdb736c10df3`-bound, 8 issuers, DRM unsupported 8,
  `unknown_launch_date_count=627` reported without estimation, `as_of` ambiguity
  rejected rather than guessed.
- Release executed after user approval: `/opt/cardrag/current -> /opt/cardrag/v1.0.29`,
  tag `v1.0.29` present, branch pushed, served MCP healthy on image `53382bc3…`.
- `/etc/cardrag/worker.env` is now byte-identical to the staged
  `worker.env.v1.0.29.proposed` (applied 2026-09-27 08:23), and contains no plaintext
  credentials — all secrets use `*_SECRET_FILE` references.
- Handoff history preserved: `PLAN.md`, `REPORT.md`, `FIX_01.md`, `FIX_01_REPORT.md`,
  `FIX_02.md` byte sizes unchanged; corrections recorded in `FIX_02_REPORT.md`.

Carry-over (schedule, do not block this round): ledger source closure 5,187 of 5,203
durable sources and the resulting 8 `source_metadata_unresolved` identities;
`retired_lineages` permanently empty; ghcr candidate package visibility for anonymous
release verification.

## Current production state (measured)

| Item | Value | Verdict |
|---|---|---|
| `cardrag-worker.timer` | `disabled`, inactive | daily batch never runs |
| `CARDRAG_CHANNEL` (worker + served MCP) | `candidate-v1.0.11` | still candidate mode |
| `CARDRAG_STABLE_PUBLICATION_APPROVED` | `false` | no stable-channel publication |
| `CARDRAG_OCR_CACHE_MODE` / publication approval | `read-only` / `false` | local-only OCR reuse |
| `CARDRAG_REMOTE_GC_APPROVED` / `COLLECT_REMOTE_GARBAGE` | `true` / `true` | valid on candidate channel |
| `CARDRAG_WORKER_STATE_VOLUME` | `cardrag-worker-v114-candidate-state` | **unsafe — see A1** |
| worker image | ghcr candidate `80eb7aa2…` = `cardrag-worker:v1.0.29-release` | pinned, correct |
| served MCP | `cardrag-stable-v1026-mcp-1`, image `53382bc3…`, volume `cardrag-mcp-v129-candidate-state`, healthy | serving v1.0.29 under v1026 naming |
| host free space | 108 GB of 392 GB (72 % used) | tight for daily growth |
| docker reclaimable | images 23.07 GB, volumes 61.01 GB | cleanup required |

## Mandate A — lift development constraints and run as a real daily batch

### A1. Stop pointing the daily batch at the v1.0.28 baseline (blocking, highest risk)

`CARDRAG_WORKER_STATE_VOLUME=cardrag-worker-v114-candidate-state` makes the first
production run write into the 48.14 GB v1.0.28 seed baseline. That volume contains no
`audit-reports/state-seed` ledger and no `ocr-seed` directory (verified), so:

- the new fail-closed corpus-diff zero-loss gate would be **inert** (`seed_ledger`
  resolves to `None`, every document is treated as new, no disappearance protection);
- the immutable seed/rollback baseline used by `seed-state-v122` and
  `seed-embedding-cache-v122` would be destroyed by the first run.

Required: establish a dedicated production state volume created only by committed code
paths — either promote `cardrag-worker-v122-candidate-state-r4` or seed a new
`cardrag-worker-v129-state` with `seed-state-v122 --apply --expected-documents 5192`
followed by `seed-embedding-cache-v122 --apply --expected-rows 381361`, then point
`CARDRAG_WORKER_STATE_VOLUME` at it. Keep `cardrag-worker-v114-candidate-state`
read-only and untouched until A6 cleanup explicitly releases it.

Verify before any run: the production volume contains the state-seed ledger
(`cardrag.state-seed-ledger.v2`) and the embedding-seed ledger, all 5,192 seed
documents, and the 381,361 seeded embedding rows.

### A2. Move from candidate channel to the stable production channel

Set, in `/etc/cardrag/worker.env` (and matching MCP env):

- `CARDRAG_CHANNEL=stable`
- `CARDRAG_STABLE_PUBLICATION_APPROVED=true`
- keep `CARDRAG_REMOTE_GC_APPROVED=true` and `CARDRAG_COLLECT_REMOTE_GARBAGE=true`
  (settings validation requires both on the stable channel)
- keep `CARDRAG_ENVIRONMENT=production`, `CARDRAG_EXTERNAL_OCR_ALLOWED=false`,
  `CARDRAG_OCR_PROVIDER=local-paddleocr`, `CARDRAG_OCR_MODEL=PaddleOCR-VL-1.6`
- leave `CARDRAG_PDF_CACHE_FORCE_REVALIDATE` unset/false so the 168 h TTL applies
  (force revalidation is a release-verification tool, not a daily-batch setting)

Ordering is mandatory because the channel determines the remote pointer namespace:

1. back up `/etc/cardrag/worker.env` and `/etc/cardrag/mcp.env` with timestamps;
2. apply the stable-channel worker env and run one supervised batch (A3);
3. confirm the generation is published and readable in the stable channel namespace;
4. only then switch the served MCP to the stable channel, re-sync, and verify
   `/health/ready` 200 plus `tools/list` = 12 and the new generation id;
5. keep the previous MCP volume and env backup for rollback, and record the rollback
   command sequence in the deployment README.

### A3. One supervised end-to-end production run before scheduling

FIX_02_REPORT §5.1 already flags that no live pipeline run has ever executed against a
code-produced embedding seed. Run it now, on the A1 production volume, with the A2
stable env, and record:

- container/service exit status 0 and terminal run status `succeeded`;
- `corpus-diff.json` present with `missing_unjustified: 0`, and the current/historical
  split reconciled against the previous generation (5,045 / 161 / 5,206 unless origins
  changed, in which case seal the recomputed categories);
- OCR: seed/prior-run reuse for unchanged documents, PaddleOCR only for genuinely new
  PDF identities, **zero** external OCR provider calls;
- embedding: cache hits dominate, provider batches bounded and itemised, no repeat of
  the 380 k-call scenario;
- capacity preflight passed with measured peak growth and free-space headroom recorded;
- publication reached the stable pointer and the served MCP resolves the new generation.

If any invariant fails, stop, preserve the volume and logs, and report instead of
enabling the timer.

### A4. Enable and verify the schedule

- `systemctl enable --now cardrag-worker.timer` (03:00 Asia/Seoul, `Persistent=true`);
- evidence: `systemctl list-timers cardrag-worker.timer` next-elapse value,
  `systemctl is-enabled` = `enabled`;
- confirm `ExecStartPre` compose render passes with the live env
  (`docker compose --env-file /etc/cardrag/worker.env -f deploy/worker/compose.yaml
  -f deploy/worker/compose.secrets.yaml config --quiet`) from
  `/opt/cardrag/current`;
- confirm journald retention is bounded (`journalctl --disk-usage` currently 1.2 GB) so
  daily runs cannot fill the disk through logs.

### A5. Decide and document the OCR-cache publication policy

Cross-run local OCR reuse exists (retained prior-run, seal-bound artifacts), so
`read-only` is workable. State the decision explicitly in the deployment README:

- keep `CARDRAG_OCR_CACHE_MODE=read-only` with
  `CARDRAG_RETAIN_GENERATIONS=2` / `CARDRAG_RETAIN_INCOMPLETE_RUNS=2` and document that
  pruning prior runs forces local PaddleOCR re-processing; or
- enable `read-write` with `CARDRAG_OCR_CACHE_PUBLICATION_APPROVED=true` (requires the
  stable channel per settings validation) and document the shared-cache growth and GC
  interaction.

Also document whether production images continue to be pulled from the ghcr *candidate*
package by pinned digest, or are promoted to a release package path.

### A6. Operational hygiene

- restore least-privilege permissions on `/etc/cardrag/worker.env`: currently
  `0644 root:root`, should match `mcp.env` at `0640 root:cardrag`;
- record in the deployment README that the served MCP container is named
  `cardrag-stable-v1026-mcp-1` (host nginx hardcodes that name) while serving v1.0.29,
  and that its volume is `cardrag-mcp-v129-candidate-state`; likewise note the legacy
  `cardrag-worker-v120-recovery-auth-20260910` codex-home volume name, so operators do
  not misread the running version;
- add a pre-run free-space guard/alert consistent with the worker's 32 GiB startup
  minimum and measured ~59 GiB peak growth.

## Mandate B — reclaim disk by removing unnecessary legacy data

Current cardrag footprint: volumes ≈ 164.6 GB, images 43.91 GB (23.07 GB reclaimable),
build cache already pruned, journald 1.2 GB. Host free space is 108 GB, while each
sealed generation costs ≈ 14.1 GB (4.6 GB index + 9.5 GB vectors) with
`RETAIN_GENERATIONS=2`. Cleanup is therefore a **precondition** for daily operation.

Rules for every deletion: confirm the target by exact name, confirm it is not
referenced by a running container (`docker ps -a --filter volume=<name>` /
`docker image inspect`), delete one target at a time, re-measure `df -h /` and
`docker system df` after each step, and record before/after sizes in
`FIX_03_REPORT.md`. Never use broad recursive deletes, globs, or `$HOME`-relative
targets. Exited evidence containers must be removed explicitly by name before their
volumes become deletable.

### B1. Immediate — no remaining evidence value (≈ 56 GB + images)

| Target | Size | Precondition |
|---|---:|---|
| volume `cardrag-mcp-v114-candidate-hashcompat-state` | 37.19 GB | 0 links; superseded by `cardrag-mcp-v129-candidate-state`; served MCP healthy |
| volume `cardrag-worker-v122-candidate-state-r2` | 4.44 GB | failed-run evidence already analysed and sealed in FIX_01/FIX_02 |
| volume `cardrag-worker-v122-candidate-state` (r1) | 4.20 GB | same as r2 |
| volume `cardrag-paddle-test-models` | 2.07 GB | duplicate of `cardrag-worker-paddleocr-models` (verify the live model volume is intact first) |
| volume `buildx_buildkit_cardrag-release-v10260_state` | 8.15 GB | build cache only; rebuilds become slower — optional, do last in this group |
| volumes `cardrag-worker-resume-20260914-200402`, `cardrag-worker-data`, `cardrag-mcp-v114-candidate-state`, `cardrag-mcp-v1026-stable-state` | ~0 B | empty/obsolete; remove for hygiene |

Images (≈ 10–13 GB): remove the superseded/dangling builds — ghcr candidate
`5934b102df51` and `f05cda3ef735` (rejected digests), `cardrag-worker:v1.0.29-source-53c25f99c76f`,
`cardrag-mcp:v1.0.29-source-53c25f99c76f`, `cardrag-mcp:v1.0.29-candidate` (2026-09-24),
`cardrag-mcp:v1.0.28-audit`. Also untag the duplicate aliases of image `24b985226ec2`
(`latest`, `paddle-v1.0.26`, `paddle-v1.0.27`, `v1.0.20-multi-provider-20260914`,
`v1.0.27`) while **keeping** `cardrag-worker:v1.0.28` for rollback.

Must keep: ghcr worker `80eb7aa2…` and MCP `53382bc3…` (pinned in live env),
`cardrag-worker:v1.0.29-release`, `cardrag-mcp:v1.0.29-candidate-r2`,
`cardrag-worker:v1.0.28` and `cardrag-mcp:v1.0.26` (rollback), and
`cardrag-worker-paddleocr-models`.

### B2. After the A3 production run succeeds (≈ 40 GB)

| Target | Size | Precondition |
|---|---:|---|
| volume `cardrag-worker-v122-candidate-state-r3` | 28.8 GB | production run succeeded on the A1 volume **and** generation `g-0928dee8…` confirmed present/readable in the remote namespace; remove exited container `cardrag-emb-attest` by name first |
| volume `cardrag-worker-v122-candidate-state-r4` | 10.99 GB | only if r4 was **not** promoted as the production volume; if it was promoted, it is production state and must be kept |
| image `cardrag-worker:v1.0.29-candidate-r3` (`b53f18e4…`) | 3.25 GB | after r3 volume release; keep `…-r4` while reproducibility evidence is cited |

### B3. Last, and only with an explicit decision (≈ 48 GB)

Volume `cardrag-worker-v114-candidate-state` (48.14 GB) is the v1.0.28 seed baseline
and the only source for `seed-state-v122` / `seed-embedding-cache-v122` re-seeding.
Delete it only when all of the following hold, and record the decision:

- it is no longer referenced by `CARDRAG_WORKER_STATE_VOLUME` (A1 complete);
- at least one stable-channel daily batch has succeeded on the production volume;
- the v1.0.29 generation is independently restorable from the remote namespace, so a
  future re-seed does not depend on this local baseline;
- the rollback procedure no longer names it, or an archived copy exists.

If any condition is unmet, keep it and state that explicitly instead of deleting.

### B4. Standing disk budget for daily operation

Document and enforce: expected per-run growth (sealed generation ≈ 14.1 GB, PDF CAS
growth, journald), `RETAIN_GENERATIONS=2` local pruning behaviour, remote GC cadence
with `CARDRAG_GARBAGE_GRACE_DAYS=1`, a free-space floor that aborts before the batch
rather than mid-run, and a periodic review trigger (for example when host free space
drops below 80 GB).

## Validation gates

```bash
uv run ruff check apps/cardrag-worker
uv run mypy apps/cardrag-worker/src
uv run pytest
git diff --check
cd /opt/cardrag/current && docker compose --env-file /etc/cardrag/worker.env \
  -f deploy/worker/compose.yaml -f deploy/worker/compose.secrets.yaml config --quiet
systemctl list-timers cardrag-worker.timer --no-pager
docker system df && df -h /
```

Plus the runtime evidence listed in A3 and the before/after disk measurements from B.

## Closure criteria

This fix is complete only when all of the following are evidenced in
`FIX_03_REPORT.md`:

1. The daily batch runs from a dedicated, code-seeded production state volume with an
   active seed ledger; the v1.0.28 baseline was not mutated by any run.
2. Worker and served MCP operate on the stable channel with stable publication
   approved, and the served MCP resolves the generation produced by the supervised run.
3. One supervised end-to-end production run succeeded with zero unexplained corpus
   loss, zero external OCR calls, bounded embedding calls, and a sealed generation.
4. `cardrag-worker.timer` is enabled with a verified next elapse, and the compose
   preflight renders from `/opt/cardrag/current` with the live env.
5. B1 cleanup executed and measured; B2 executed or explicitly deferred with reasons;
   B3 deleted only if all its preconditions were met, otherwise explicitly retained.
6. Host free space after cleanup is recorded, together with the standing disk budget
   and the rollback procedure (env backups, images, volumes, pointer).
7. Env permission drift corrected and the naming caveats documented.
8. `PLAN.md`, `REPORT.md`, `FIX_01*.md`, `FIX_02*.md`, and this file remain unmodified;
   all corrections are recorded in `FIX_03_REPORT.md`.
