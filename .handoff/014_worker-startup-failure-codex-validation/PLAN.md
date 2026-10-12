# 014 — 03시 Worker 초기화 실패 진단 및 Codex 업데이트 검증

- 작성: 2026-10-12, Planner Codex
- 상태: 계획 발행. 구현·운영 복구 완료를 뜻하지 않는다.
- 사용자 요청: 당일 예약 실행 실패 원인 조사·개선. 특히 전일 Worker 내부 Codex 버전 상승이 유효성 실패를 유발했는지 확인.

## 1. 목표와 완료 범위

1. 실패 경계를 좁히고 재현 가능한 원인은 수정한다. 재현되지 않는 과거 실패는 근거의 한계를 명시한다.
2. 초기화 실패가 다시 발생해도 원인 분류·단계·소요시간을 알 수 있도록 진단을 보완한다.
3. Codex 0.162.1 + `gpt-6.1-sol` + `medium`, fallback `local-paddleocr` + `PaddleOCR-VL-1.6`의 실제 운영 적합성을 확인한다.
4. 짧은 검증으로 배포 준비를 마치고 Worker 1회 구동까지 진행한다. 긴 배치는 기동 후 턴을 종료하고 사용자가 완료를 알려주면 최종 확인한다. 2일간 예약 실행을 기다리는 조건은 두지 않는다.
5. 기존 MCP 정상 서빙, OCR 재사용, 로컬 게시 및 선택적 증분 WebDAV 백업 구조를 보존한다.

이번 작업은 초기화 안정성 개선이다. 새로운 OCR 전량 처리, 전체 원격 검증, 카드사 어댑터 개편, 모델·임베딩 차원 변경, 새 공개 릴리스는 범위 밖이다. 확실한 결함과 연관되지 않은 개발 요건을 인수 차단 조건으로 추가하지 않는다.

## 2. 확정된 현황과 진단 근거

### 2.1 실패 실행

- systemd invocation: `dd14bc17a5f24a588f5ec7af75310015`.
- 2026-10-12 03:00:00 KST 타이머/Compose 기동, config 사전검사 exit 0.
- 03:00:08 시작 용량 검사 통과: free 147,615,694,848 bytes, minimum 2 GiB.
- 03:00:11 OCR 호환 contract 6개 발견.
- 03:00:12 `Remote OCR cache access mode=read-write require_hit=False` 출력.
- 03:00:17 `Worker execution finished elapsed_seconds=15.914`, `worker_unexpected_failure`, exit 1.
- `Qwen provider preflight passed` 및 `Worker startup completed`는 출력되지 않았다.
- 본 실행의 PDF/OCR/임베딩/게시/백업 처리 시작 증거가 없다. 실패한 초기화에서도 외부 임베딩 사전검사 요청은 발생할 수 있다. 모든 네트워크 호출이 없었다고 주장하지 않는다.
- 종료 컨테이너는 `run --rm`이므로 사라졌다. journal이 원본 로그다. 기존 orphan 경고는 이전 정상 완료 컨테이너를 가리키며 이 실패 원인으로 확인되지 않았다.
- MCP는 healthy이고 기존 generation으로 서빙한다. 타이머 active, 다음 실행 10월 13일 03:00.

### 2.2 Planner가 수행한 짧은 비교 검증

- 10월 11일 새 이미지에서 실제 `CodexOCRProvider.recognize()`로 작은 합성 이미지 1장을 호출: `gpt-6.1-sol`, medium, `CARD OCR TEST` / `DISCOUNT 1.2%` 정상 반환.
- 10월 12일 약 08:46, 같은 운영 이미지/설정에서 `_qwen_embedding_provider(WorkerSettings.from_env())` 실제 재시험은 통과했다. 임베딩 요청 및 품질 사전검사까지 실행된 결과다.
- 해당 임시 probe는 성공 출력 후 존재하지 않는 `provider.close()` 호출로 AttributeError를 추가 출력했다. 이는 probe 정리 코드 오류이며 새벽 Worker 오류도, 임베딩 사전검사 실패도 아니다. 실행자가 그대로 복사하지 않는다.
- 10월 12일 08:50, `_run(None)`의 `_qwen_embedding_provider`를 경계 도달 시 중단하는 함수로 대체해 초기화 경로를 이미지별 순차 실행했다. 전량 파이프라인·OCR·실제 임베딩 요청은 이 비교에서 실행하지 않았다.
  - 기존 Codex 0.151.0 이미지: 용량/설정/State/호환 OCR/fallback 구성 및 임베딩 진입 직전까지 통과.
  - 새 Codex 0.162.1 이미지: 동일 경계까지 통과.
  - 두 이미지 `cardrag_worker.cli` 파일 SHA256 동일: `77d2b561e9f44fbc031408d9e8bc353df8d2de4bf6433252ea375ce87d684401`.
  - 새 이미지 RootFS는 기존 레이어 모두를 그대로 포함하고 Codex 교체 레이어가 추가된 형태다.
- 관련 컨테이너 계약/기존 OCI 공급망 테스트 20개 통과.

### 2.3 현재 결론의 한계

코드 순서상 마지막 로그 다음은 `_qwen_embedding_provider(settings)`이며, 내부 단계는 tokenizer 확보/검증 → OpenRouter endpoint metadata 및 Qwen 비교검사 → 선택 provider/모델/차원/maximum_tokens 확인이다. 이 구간과 예외 전파/정리 경로가 우선 조사 대상이다.

`CodexOCRProvider` 생성자는 설정만 구성하고, `codex exec`는 OCR recognize에서 시작한다. 당일 로그는 그 이전에 끊겼다. 따라서 Codex CLI 유효성 실패가 직접 원인이라는 증거는 현재 없다. 다만 이 사실이 전체 새 이미지 회귀가 없다는 완전한 증명은 아니므로 실제 subprocess 설정·인증·출력 계약을 별도 확인한다.

당시 generic except가 원인 정보를 버렸으므로 과거 정확한 HTTP 상태/예외를 확정할 수 없다. 현재 재시험 성공을 과거 성공이나 확정된 외부 API 장애로 바꾸어 기록하지 않는다.

## 3. Git 및 운영 상태 — 반드시 유지할 내용

- repository: `/home/lee/projects/MCP_card_prd_detail`.
- `main`/`origin/main`: `1cad9f0`; 현재 branch `codex/worker-codex-01621`, HEAD `5961345`.
- 해당 branch는 main보다 1커밋 앞서며 main 쪽 추가 커밋은 없었다. 변경은 Dockerfile Codex pin/checksum, 그 테스트, OPERATIONS 현재 OCR 설명의 3파일이다. 충돌 없이 fast-forward 가능. 구현 시작 전 fetch/status를 다시 확인하고 이 변경을 보존한다.
- 이번 PLAN은 이 branch에 추가된다. branch가 둘이라는 이유로 변경을 reset/delete하지 않는다. 별도 사용자 지시 없이 GitHub main 병합·새 릴리스 작업을 확대하지 않는다.
- 운영 `/opt/cardrag/current -> /opt/cardrag/013-50a0129`.
- 현재 Worker: `cardrag-worker:013-50a0129-codex-01621`, ID `sha256:aa3ae39a8d80a31b1033db99039e49b85dbac4b62637632aa17bea857702ea9c`.
- 비교용 기존 Worker: `cardrag-worker:013-50a0129`, ID `sha256:a3b93b8dcc3e91c3a35c80af9f1d01922388ca2d965244f9a2007d79686fc0f2`.
- MCP: `cardrag-mcp:013-50a0129`, container `cardrag-mcp`.
- Codex 0.162.1 artifact SHA256: `86f268d81b898f3e144c5ecff2ad2fda2c5802e042d6195b72538fe0fa125057`。
- operational helper: `/opt/cardrag/current/operations/worker-compose.sh`。
- actual settings: `/etc/cardrag/worker.env` + `deploy/worker/compose.yaml` + `deploy/worker/compose.secrets.yaml` under current installation. Host override explicitly clears old Codex custom provider/base URL/env key/model catalog. Preserve this: old env file includes legacy provider settings.
- publication local; existing optional incremental backup immediate, inline budget 300 seconds. Do not change backup policy as part of this fix.
- volumes: `cardrag-worker-state`, `cardrag-worker-auth`, `cardrag-worker-paddleocr-models`, `cardrag-serving`, `cardrag-mcp-state`. Do not remove/reset/recreate them or clear OCR variants.
- private local evidence (not credentials to copy into Git):
  - `/opt/cardrag/current/operations/scheduled-review-20261012.json`
  - `/opt/cardrag/current/operations/codex-update-0.162.1/verification.json`
  - `/opt/cardrag/current/operations/scheduled-review-20261011.json`
- prior normal Oct 11 batch: 03:00 → 04:32:18, exit 0; 5,520 OCR cache hits, zero OCR provider calls, no backup requests. This is a practical comparison baseline, not a requirement that changed documents always cause zero calls.

## 4. Relevant source files and current failure mechanism

| File | Purpose |
|---|---|
| `apps/cardrag-worker/src/cardrag_worker/cli.py` | `_run`, `_ocr_resolver`, `_qwen_embedding_provider`, generic run_command catch; initial failures have no run_id/report |
| `apps/cardrag-worker/src/cardrag_worker/embedding_v5.py` | metadata retrieval, pinned routes, repeat/cross-provider preflight, typed request errors and existing retry/backoff |
| `apps/cardrag-worker/src/cardrag_worker/tokenizer_v5.py` | pinned tokenizer hash/size checks and download |
| `apps/cardrag-worker/src/cardrag_worker/providers.py` | Codex execution flags/env filtering/error classification; Paddle fallback provider |
| `apps/cardrag-worker/src/cardrag_worker/settings.py` | runtime model/timeout/retry configuration; verify exact settings module path before editing |
| `apps/cardrag-worker/src/cardrag_worker/pipeline.py` | existing sanitized unexpected failure report/classification, reuse when appropriate |
| `apps/cardrag-worker/tests/test_cli_settings_provider.py` | startup ordering, generic error output, Codex argv/env, locking regression |
| `apps/cardrag-worker/tests/test_embedding_v5.py` | embedding metadata/response/quality/retry tests |
| `tests/runtime_v1/test_container_runtime_contract.py` | container CLI pin/permissions/security contract |
| `tests/runtime_v1/test_candidate_oci_supply_chain.py` | historical OCI fixtures; do not rewrite historical releases' 0.151.0 fixtures globally |
| `Dockerfile`, `docs/OPERATIONS.md` | reproducible version pin and current runtime guidance |

Current CLI catches generic Exception without passing the exception into `_echo_worker_unexpected_failure()`. Pipeline errors may have rich sanitized reports, but startup errors before WorkerPipeline do not. This is the confirmed diagnostic defect to fix even if the original failure is no longer reproducible.

## 5. Execution instructions

### Step A — Preserve evidence and narrow cause

1. Read this PLAN, inspect status/branch and applicable instructions; do not require rereading all 013 historical artifacts.
2. Capture Oct 12 journal and systemd effective configuration, image IDs and allowlisted nonsecret settings into a private operations record. Never dump complete env/auth/Compose secrets into Git/logs.
3. Distinguish tokenizer, endpoint metadata, embedding sample requests, route selection, contract gates and cleanup exceptions. Inspect existing RequestError `kind`, `status_code`, `attempts`; metadata absence may also raise StopIteration/RuntimeError through selected-provider lookup.
4. First use current evidence. Avoid repeating paid full preflights or A/B calls unless a specific unresolved difference remains. Reproduction need not run the corpus.
5. If it does not reproduce, retain the conclusion “historical exact cause unavailable; failed boundary narrowed; diagnostics missing.” Do not wait days or claim an unobserved cause.

### Step B — Add bounded, secret-safe startup diagnostics

1. Emit stage start/pass/fail with elapsed seconds for meaningful startup boundaries. At minimum distinguish OCR resolver construction, tokenizer validation, Qwen endpoint/provider preflight, embedding configuration verification and pipeline entry.
2. Record resolved Codex version/image identity if available without launching inference. CLI availability/version check must have a small timeout and must not expose auth contents. Do not duplicate checks per document.
3. Startup failures must emit an actionable structured record including stable reason code, phase/stage, sanitized exception category and, where available, HTTP status, retryability and attempts. Preserve existing top-level CLI contract where possible with additive fields. Preserve `worker_busy`, cancellation and pipeline error handling.
4. Use an existing safe error classifier or an explicit allowlist. Do not use raw `str(exc)`, `repr(exc)`, response bodies, argv containing secrets, unrestricted traceback locals or all environment variables. Synthetic exception tests must prove tokens/URLs with credentials do not leak.
5. journal output alone can be the durable record for errors before state is available. If adding a local report, write atomically/private and keep bounded retention; do not require DB/network availability to report a startup failure or create fake successful run IDs.
6. Expose a focused startup-check command or small repository script that runs the same checks and exits before WorkerPipeline. State/provider logic should reuse production implementation. No alternate simulated production path. Keep locking before any state DB access; never allow two initializers to open live state concurrently.
7. Classify invalid configured provider/metadata absence explicitly rather than an opaque next() failure if this route is confirmed. Add only minimal justified code changes.

### Step C — Improve recovery according to classified cause

- Existing embedding requests already have retry/backoff. Inspect them before introducing additional retries. Retry bounded transient network/429/5xx failures using existing mechanisms; report exhausted retries clearly.
- Do not retry authentication/403, bad pinned tokenizer, model/dimension mismatch, endpoint precision violations or quality-gate failures indiscriminately. Never bypass pinned metadata, precision, cosine/tokenizer/model gates, or silently switch embedding provider/model.
- Do not retry the entire corpus or publish old artifacts as a new successful run merely to hide initialization failure.
- If evidence shows Codex flags/config are rejected at 0.162.1, implement a minimal compatible change preserving sandbox, tool/env restrictions and output contract; otherwise retain requested version/model. No automatic downgrade based solely on temporal correlation.
- Only if a confirmed new-version regression prevents operation and cannot be corrected promptly, use the one prior accepted Worker image as a documented temporary rollback. Check actual current auth/model compatibility first; never overwrite auth or reset cache.

### Step D — Small Codex and fallback validation

1. Confirm Worker-internal CLI version 0.162.1 and model/reasoning/fallback effective values from the actual Compose combination.
2. Run at most one small synthetic image via actual `CodexOCRProvider.recognize()` at `gpt-6.1-sol`/medium if new implementation/image changes warrant rechecking. A `codex --version` or text-only call does not establish image OCR compatibility. Use existing dedicated auth volume and timeout about 120 seconds.
3. Preserve/check sandbox/link permissions, required CLI flags, auth root, env filtering and output header contract. Current successful Oct 11 call is valid supporting evidence.
4. Check Paddle fallback construction and existing installed modules/model cache; use controlled provider-failure tests to verify real fallback policy. Do not force a 12-page heavy scan or download all models to prove fallback. Authentication/configuration failures should follow existing policy, not be silently reclassified as transient.
5. Distinguish image update, requested primary model switch and clearing legacy custom-provider settings. If further A/B needed, change only one variable at a time, never share live writer state concurrently. Old-image boundary success is not proof that its actual gpt-6.1-sol inference succeeds.

### Step E — Targeted tests and operational run

Minimum relevant checks (add tests under existing files, not a separate broad framework):

```sh
uv run --no-sync pytest -q apps/cardrag-worker/tests/test_cli_settings_provider.py \
  apps/cardrag-worker/tests/test_embedding_v5.py \
  tests/runtime_v1/test_container_runtime_contract.py
```

Use targeted selections first when the complete provider test file is unnecessarily broad. Verify:
- tokenizer failure, metadata permanent failure, exhausted transient retry, configured provider missing and contract mismatch are distinguishable;
- phase/time/status/attempts are correct; failure remains nonzero; startup success cannot be reported before real checks complete;
- no credential leakage; successful path unchanged; cancellation/worker_busy unchanged;
- no startup-check corpus processing, backup upload, generation publication or unsolicited Codex inference;
- Codex actual small call and fallback configuration verified.

Build only needed Worker image once after fixes. Do not rebuild MCP for Worker-only changes. If derivative-image source replacement is used, keep it reproducible, record exact source commit/image ID, and prove correct installed files/dependencies; prefer existing Dockerfile workflow. Reuse cache and remove temporary large binaries/artifacts after validation.

Apply after short checks pass, keeping volumes, credentials, timer schedule and old completed logs. Use actual same env/Compose overlays as systemd. A short helper check may differ in project name, so verify this doesn't alter volume bindings/effective settings. If systemd operations require sudo this session cannot obtain, finish preparation and give the exact necessary sudo command; do not falsely claim systemd execution.

Before Worker launch ensure no active Worker/backup writer, and start only one full run. Use a named container with retained logs (not --rm for this recovery) or systemd journal, provide exact name/log command. Do not poll a 90–150 minute run continuously. Stop the turn after startup has passed and give the user the monitoring command; when completion is reported, verify exit status, run ID/generation, issuer degraded/carry handling, OCR reuse/new calls, local publication, MCP activation, backup pending/lost/status and stage timings.

## 6. Acceptance criteria

### Development / ready to deploy

- Narrowed historical failure boundary and honest cause confidence recorded; any reproduced cause fixed.
- Future initialization failures report stage and useful safe classification, with tests for relevant failure branches.
- Existing quality/security gates and OCR cache/optional backup behavior preserved.
- Codex update assessed independently: current 0.162.1 actual small image OCR works, construction/image comparison passes, any unresolved regression clearly stated.
- Targeted tests pass and effective deployment is verified.
- No two scheduled successes or unnecessary multi-day waiting required.

### Operational acceptance (follow-up after run)

- One real full Worker run exits 0 and local generation becomes healthy in MCP. It can be manual now; record scheduled versus manual separately.
- Normal issuer partial failure/carry behavior may remain; it is not a new blocker unless this task caused a regression.
- Backup absence/failure does not block local publication. Existing pending/lost baseline respected.
- Phase timings and complete run duration recorded.
- If only startup passed and batch is still running, label operational acceptance pending; never claim complete.

## 7. Required Executor REPORT.md

Create the next artifact in this directory without changing this PLAN/history. Include exact Git commit/status, source modifications, commands/results, image/CLI/model/config, timestamped stage evidence, old/new image comparisons already available, original cause confidence, injected error tests and sanitized output, actual small OCR result, operation changes/rollback, full run status or monitoring handoff and remaining limitations. Do not invent a definitive historical cause if the original exception cannot be recovered.
