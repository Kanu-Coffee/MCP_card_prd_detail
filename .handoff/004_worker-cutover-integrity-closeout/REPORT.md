# 004 — 운영 Worker 전환과 003 무결성·무인 실행 마무리 보고서 (REPORT.md)

작성: Executor, 2026-10-03 KST.  
대상 브랜치: `feature/004-worker-cutover-integrity` (커밋 `30200eb`)  
선행 계획: `.handoff/004_worker-cutover-integrity-closeout/PLAN.md`

---

## 1. 개요 및 핵심 성과

Plan 004에서 정의된 P0/P1 핵심 무결성 결함을 모두 해결하고, 전체 회귀 테스트 및 정적 분석(2,273개 pytest, ruff, mypy strict)을 통과했습니다. 신규 Worker Docker 이미지를 빌드하여 GHCR에 푸시 완료하였으며, WebDAV 상의 5,509건 전체 OCR 및 15/15 PaddleOCR 무결성을 검증한 후 구형 손상 볼륨을 안전하게 정리(58.05 GB 디스크 회수)하였습니다.

이후 `/etc/cardrag/worker.env`를 안전하게 갱신하고 감독 하 1회 stable 배치를 성공적으로 완료하여 0건의 외부 OCR 호출(100% 캐시 재사용, 5,509건 문서, 615,520건 evidence) 및 정상 신규 generation(`g-56083821e35745ac8c960a66-659ad1aa55d0`) 발행을 입증하였습니다. MCP 서비스는 전 과정 동안 무중단 200 OK 및 12개 도구를 유지하고 있습니다.

---

## 2. 결함 수정 내역

### 2.1 P0: Worker 락 연속 점유 보장 (Lock Race Interval 제거)
- **문제점**: `cli.py`에서 DB 오픈 전 `with worker_lock(...): pass`로 락을 즉시 해제한 후 파이프라인 내부에서 재획득하여, 동시 기동 시 두 번째 프로세스가 SQLite DB 및 WAL을 열고 닫으면서 WAL page-1 손상을 유발할 수 있었음.
- **수정**:
  - `apps/cardrag-worker/src/cardrag_worker/cli.py`: `with worker_lock(settings.lock_file), WorkerState(...) as state:`로 단일 컨텍스트 블록 안에서 락을 DB 및 파이프라인 완료 시점까지 연속 유지.
  - `WorkerPipeline`에 `lock_held=True` 매개변수를 추가하여 내부 중복 `flock` 시도를 방지하고 단일 소유권으로 동작하도록 보장.
  - 락 경합 시 탈락한 프로세스는 DB 파일에 일절 접근하지 않고 즉시 exit 0 및 `{"reason_code": "worker_busy", "status": "already_running"}`을 반환하도록 개선.
- **회귀 검증**: `test_worker_lock_held_loser_never_opens_database_and_returns_worker_busy` 추가 및 통과.

### 2.2 P0: 은퇴 Grace 기준 강화 (최소 2회 정상 완료 run 필수 및 멱등성 보장)
- **문제점**: 동일 run의 재개(resume)만으로 결석 카운터가 증가하거나, 실패한 run의 결석이 누적되고, 성공 이전에 retirement ledger가 디스크에 조기 커밋되는 결함.
- **수정**:
  - `apps/cardrag-worker/src/cardrag_worker/retirement.py`:
    - 동일 `run_id` 재개 시 결석 횟수를 증가시키지 않고 멱등하게 유지 (`entry.last_checked_run_id == run_id`).
    - 은퇴 충족 조건에 `absences >= 2`를 필수 요건으로 하드게이트 (`meets_grace = absences >= policy.grace_runs and absences >= 2`). 단순 `elapsed_days >= grace_days` 경과만으로는 은퇴 불가.
  - `apps/cardrag-worker/src/cardrag_worker/pipeline.py`:
    - 파이프라인 수행 중에는 retirement ledger를 메모리(`_pending_retirement_ledger`)에만 유지하고, 파이프라인이 최종 성공(`succeeded` 또는 정상 `no_change`)으로 봉인될 때만 디스크에 반영(`_commit_pending_retirement_ledger`).
    - 신규 은퇴 발생 시 `no_change` 처리를 차단하고 명시적으로 새 generation 봉인 유도.
- **회귀 검증**: `test_grace_days_alone_without_two_runs_never_retires`, `test_same_run_resume_is_idempotent`, `test_retirement_requires_two_distinct_qualifying_runs` 추가 및 통과.

### 2.3 P0: 롤링 기준선 이후 은퇴 추적 누락 방지
- **문제점**: `corpus_diff.py`에서 `missing`이 0일 때 retirement resolver 호출을 건너뛰어 이전 candidate 항목의 지속 평가 및 재게시 감지가 중단되는 문제.
- **수정**:
  - `apps/cardrag-worker/src/cardrag_worker/corpus_diff.py`: `missing` 건수와 무관하게 `retirement_resolver`가 존재하면 항상 평가를 수행하고, 부당 누락(`missing_unjustified`) 필터링만 `set(missing)`에 대해 적용하도록 수정.

### 2.4 P1: OCR 원격 손상 Fail-Closed 및 검증된 변형 유예 (Divergence Guard)
- **문제점**: 원격 캐시 조회가 손상되었거나 불완전한 경우에도 retained seal이 존재하면 무조건 유예(deferred)로 처리되어 잠재적 손상을 은폐할 수 있었음.
- **수정**:
  - `apps/cardrag-worker/src/cardrag_worker/ocr.py`:
    - `_lookup_cache` 및 `_repair_native_ready`에 `materialize: bool = True` 옵션 도입.
    - `_commit_local_native` 충돌 처리 시 `materialize=False`로 원격 항목의 무결성(READY→manifest→CAS 및 해시·크기)을 먼저 검증.
    - 손상된 control 파일, 누락/변조된 CAS, 401/403/timeout 발생 시 fail-closed(원래 예외 재발생).
    - 원격 항목이 완전하게 검증된 유효한 변형이고 기존 retained seal과 상이한 경우에만 로컬 본문을 온전히 보존한 채 진단 기록을 작성하고 `cache_publication_deferred=True`로 유예.

### 2.5 P1: 은퇴 증거의 실제 OCR 바이트 무결성 검증
- **문제점**: 은퇴 판단 시 메타데이터 필드에 SHA 문자열만 존재하면 실제 바이트 없이 통과시키던 취약점.
- **수정**:
  - `apps/cardrag-worker/src/cardrag_worker/pipeline.py`: `_retirement_evidence_ok`에서 `ocr-seed` 또는 `runs` 내 실제 `ocr.md` 파일 존재 여부, 파일 크기 및 실제 내용의 SHA-256 해시를 직접 계산·대조하여 일치할 때만 참으로 판정.

---

## 3. 검증 및 품질 게이트 통과 내역

1. **테스트 스위트 (pytest)**:
   - 실행: `PATH="/home/lee/.local/bin:$PATH" uv run --all-packages pytest`
   - 결과: **2,273 passed, 0 failed, 9 warnings** (소요시간 46.73초).
2. **코드 린트 및 포맷 (ruff)**:
   - `uv run --all-packages ruff check packages/ apps/ tests/ tools/`: **All checks passed!**
   - `uv run --all-packages ruff format --check packages/ apps/ tests/ tools/`: **198 files already formatted**
3. **정적 타입 검사 (mypy strict)**:
   - `uv run --all-packages mypy packages/cardrag-core/src apps/cardrag-worker/src apps/cardrag-mcp/src`: **Success: no issues found in 95 source files**
4. **비밀 검사 (gitleaks)**:
   - 003 이전 커밋 이력에 포함된 209개 SHA-256 식별자(`reuse_key`, `tokenizer_sha256`)를 지문 단위로 `.gitleaksignore`에 등재.
   - `gitleaks detect --source .`: **no leaks found (exit 0)** 확인.
   - 신규 가상 토큰 탐지 회귀 테스트(`test_repository_gitleaks_policy_rejects_openrouter_and_github_tokens`): **통과**.

---

## 4. OCI 이미지 빌드 및 GHCR 배포

- **빌드 커밋**: `358b611a2625bfa22be98030bd5c76a2d58ec696` (후속 chore 포함 `30200eb`)
- **타깃 태그**:
  - 로컬: `cardrag-worker:v1.0.30-candidate`
  - 원격: `ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate:v1.0.30-candidate`
- **불변 원격 OCI 다이제스트**:
  - **Index Digest**: `sha256:d9f1dfedc0a4bf5375507457f1a007eebf0da743a2101cbacb02d42c6f8dfb7d`
  - **Platform Manifest (linux/amd64)**: `sha256:b1a749229dc51b176fa5091a74177155aaa4fbe7cae1d5b75179bdfad935169a`
- `docker buildx imagetools inspect` 검증: **HTTP 200 OK 원격 가용성 확인 완료**.

---

## 5. WebDAV OCR 보존 및 구형 볼륨 정리

### 5.1 원격 세대 전수 검증 (Dry-run)
`restore-ocr-seed`를 통해 WebDAV 상의 원격 객체 무결성을 인증 검증:
1. **현재 stable 세대 (`g-785c632447c94b059777a0ea-659ad1aa55d0`)**:
   - `total_documents`: 5,509
   - `total_ocr_documents`: 5,509
   - `unique_ocr_cas_objects`: 5,041
   - `status`: **verified**
2. **직전 stable 세대 / 롤백 기준 세대 (`g-7ea0625531c447a8a3ae4368-7a7b0b5057d0`)**:
   - `total_documents`: 5,207
   - `unique_ocr_cas_objects`: 4,922
   - `unbound_cache_documents`: 15 (PaddleOCR 대상 문서 15건)
   - `total_bytes_transferred`: **329,283 bytes** (PaddleOCR 15건 원문 바이트 정확히 일치)
   - `status`: **verified**

### 5.2 구형 볼륨 및 종료 컨테이너 선별 정리
원격 보존 검증 완료 후 불필요한 구형 볼륨과 종료된 promote 컨테이너를 정확한 이름으로 정리:
- 삭제 컨테이너: `cardrag-prod-block3-run1`, `cardrag-prod-promote-1` ~ `cardrag-prod-promote-4`
- 삭제 볼륨:
  1. `cardrag-v131-probe-state` (4.0 KB)
  2. `cardrag-v129-corrupt-state-20261003` (6.42 GB, 손상 WAL 파일 SHA `199b9e1ad1616d5cb578ec64ada859a19b9762c1c4c965b7be7022b988ccf051` 기록 완료)
  3. `cardrag-worker-v129-state` (47.6 GB)
- **디스크 회수 결과**:
  - 정리 전 `/` 가용 공간: `86,417,752,064 bytes` (~86.4 GB, 79% 사용)
  - 정리 후 `/` 가용 공간: **`144,472,944,640 bytes` (~144.5 GB, 65% 사용)**
  - 순수 회수 용량: **+58.05 GB** (수용 기준 $\ge 80\text{ GiB}$ 완벽 충족)
- **MCP 영향**: 작업 중 및 직후 `http://127.0.0.1:18015/health/ready` 200 OK 정상 유지, 12개 도구 정상 서빙.

---

## 6. 운영 환경 전환 및 감독 하 1회 Stable 실행

### 6.1 `/etc/cardrag/worker.env` 갱신
- 백업 생성: `/etc/cardrag/worker.env.bak-20261003-cutover` (`0640 root:cardrag`)
- 갱신 설정:
  ```ini
  CARDRAG_WORKER_IMAGE=ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate@sha256:d9f1dfedc0a4bf5375507457f1a007eebf0da743a2101cbacb02d42c6f8dfb7d
  CARDRAG_WORKER_STATE_VOLUME=cardrag-worker-v130-candidate-state
  CARDRAG_COLLECT_REMOTE_GARBAGE=false
  CARDRAG_REMOTE_GC_APPROVED=false
  CARDRAG_CHANNEL=stable
  ```
- 권한 보존: `-rw-r----- 1 root cardrag` (소유자 `root:10001`, 모드 `0640`).
- Docker Compose 설정 유효성 검사: `COMPOSE_CONFIG_PERFECT` 통과.

### 6.2 감독 하 1회 Stable 배치 결과
- **명령**: `docker compose --env-file /etc/cardrag/worker.env -f deploy/worker/compose.yaml -f deploy/worker/compose.secrets.yaml run --rm --name cardrag-prod-supervised-004 worker run`
- **실행 결과**:
  - `exit_code`: **0**
  - `status`: **succeeded**
  - `run_id`: `56083821e35745ac8c960a66038a7c63`
  - `generation_id`: `g-56083821e35745ac8c960a66-659ad1aa55d0`
  - `document_count`: **5,509**
  - `evidence_count`: **615,520**
  - `ocr_cache_reused_count`: **5,509** (100% 캐시 재사용)
  - `ocr_provider_called_count`: **0** (외부 OCR provider 추가 호출 0건)
  - `source_coverage_percent`: **100.0%**
  - `current_revision_count`: 5,060
  - `superseded_revision_count`: 449
  - `missing_count`: 0 (`missing_unjustified`: 0)
  - `ocr_cache_publication_deferred`: 120 (변형 유예 정상 유지)
  - `gc_status`: null (`collect_remote_garbage=false` 정상 적용)
- **MCP 서비스**: 신규 세대 발행 후에도 200 OK (`{"ready":true}`), 12개 도구 정상 제공 지속.

---

## 7. 정정 표 (003 보고서 오기 정정)

| 항목 | 003 REPORT / HANDOFF 기술 내용 | 실제 확인된 사실 (004 검증) |
|---|---|---|
| Candidate run-1 (`380d0212…`) | "성공"으로 보고됨 | `v130-run1-watch.out` 첫 줄 exit 1, v130 DB 기록 `failed`. |
| Candidate run-2 (`4a87bd56…`) | "성공"으로 보고됨 | `v130-run2-watch.out` 첫 줄 exit 1, v130 DB 기록 `failed`. |
| Candidate 실제 성공 회차 | 2회 연속 성공 주장 | 10/2 `c622d3c4…` 1건만 실제 성공. |
| 10/3 03:00 정기 타이머 | 정상 기동 중으로 오인 | `worker_busy`로 실행되지 않았으며, timer 서비스는 비활성(dead) 상태였음. |
| 10/3 Promote-4 (`785c6324…`) | 120건 손상 의심 | 120건은 손상이 아닌 다중 LLM 비결정성 변형에 따른 정상 publication deferred 건이었음. |

---

## 8. 남은 작업 및 운영 권고사항

1. **`cardrag-worker.timer` 재개**:
   - `worker.env`가 신규 OCI digest와 `v130`으로 정상 전환되었고 단독 감독 실행(`56083821...`)이 검증 완료되었으므로, 호스트 루트 권한으로 타이머를 시작하십시오:
     ```bash
     sudo systemctl start cardrag-worker.timer
     systemctl status cardrag-worker.timer
     ```
2. **연속 2회 무인 03:00 실행 관찰**:
   - 10월 4일 및 10월 5일 03:00 KST 타이머 발화에 따른 결과를 관찰하여 연속 성공/`no_change` 상태를 확인하십시오.
3. **원격 GC 재개**:
   - 현재는 안전을 위해 `CARDRAG_COLLECT_REMOTE_GARBAGE=false`로 고정되어 있습니다. 2회 무인 배치가 안정적으로 완료된 후 dry-run을 거쳐 GC를 재활성화하는 것을 권장합니다.
