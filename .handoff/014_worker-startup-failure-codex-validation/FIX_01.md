# 014 FIX_01 — Reviewer 검토 및 다음 보완 요청

- 작성: 2026-10-12 12:01 KST, Reviewer Codex
- 검토 기준: `7d07b1ddfe2bed5310d771a1e18b6b717703e7c6`
- 검토 문서: PLAN.md, REPORT.md. 기존 FIX 문서는 없었다.
- Git: tracked 변경 없음, 014 handoff 디렉터리는 untracked. 기존 문서를 수정하지 않았다.
- 독립 증거: `evidence/reviewer-20261012.json`

## 판정

**실운영 복구 실행과 새 generation 서빙은 성공했다. 구현 최종 인수는 아래 보완 후 재검토한다.**

03시 실패의 정확한 원인은 여전히 미상이다. 실패 경계 및 진단 결함에 대한 PLAN의 판단은 원본 journal과 일치한다. 이번 정상 실행은 회복의 증거이며, 미상 원인을 제거했다는 증거는 아니다. 운영을 중단하거나 이전 이미지로 되돌릴 근거는 발견하지 않았다.

## 수정 요청

### 1. [P2] 새 코드가 기존 CI Ruff 검사를 실패시킨다

위치: `apps/cardrag-worker/src/cardrag_worker/cli.py:68`, `:466`; `apps/cardrag-worker/tests/test_cli_settings_provider.py:1028`, `:1061`, `:1108`.

독립 실행 결과:

```text
uv run --no-sync ruff check apps/cardrag-worker/src/cardrag_worker/cli.py apps/cardrag-worker/tests/test_cli_settings_provider.py --output-format concise
F401: unused EmbeddingV5PermanentRequestError import
S110: _probe_codex_version의 except Exception: pass
S105: 새 synthetic secret_sentinel 할당 3건
Found 5 errors.
```

`.github/workflows/ci.yml:96`의 필수 lint 단계가 이 두 파일을 포함한다. pytest 성공만으로 개발 검증 완료로 볼 수 없다. 사용하지 않는 import를 제거하고, 버전 프로브 실패는 예외 원문 없이 안전하게 처리한다. S105는 의도된 합성 테스트 데이터임을 해당 줄에 한정해 설명하거나 저장소의 기존 테스트 관례를 따른다. 전역 규칙 완화나 예외 문자열 로깅으로 해결하지 않는다.

### 2. [P2] startup-check가 운영 초기화 경로를 별도로 복제하고 실제 경로 테스트가 없다

위치: `apps/cardrag-worker/src/cardrag_worker/cli.py:1055`; `apps/cardrag-worker/tests/test_cli_settings_provider.py:1180`.

`_run_startup_check()`는 `_run()`의 settings/capacity/profile/transport/lock/WorkerState/OCR 초기화를 약 100줄 별도로 복제한다. tokenizer/provider helper는 공유하지만 초기화 전체가 동일 실행 경로라는 보장은 없다. 이미 종료 경로는 `_run()`의 `await transport.close()`와 check의 선택적 closer 호출로 달라져 있다. PLAN Step B.6은 실제 초기화 구현 재사용 및 별도 production 경로 금지를 명시한다.

추가한 `test_cli_startup_check_command_execution`은 `_run_startup_check` 전체를 mock하므로 실제 lock-before-DB 순서, 초기화 실패의 비정상 종료, corpus/게시/백업 미실행을 확인하지 못한다. 운영 check 성공은 happy path의 증거이나 이러한 경계를 대신 검증하지 않는다.

공통 초기화 실행 경로를 사용하도록 최소 범위로 정리한다. 예를 들어 `_run`의 startup-only 모드 또는 공통 async context manager로 자원 수명/락을 공유할 수 있다. 기존 resume 전용 처리와 pipeline/backup 동작은 보존한다. 실제 check 경로에서 외부 I/O 경계만 mock하여 다음을 검증한다:

- lock 거부 시 WorkerState와 provider가 열리지 않고 기존 worker_busy 계약을 유지한다.
- 정상 검사 시 pipeline/게시/백업을 호출하지 않는다.
- 초기화 실패 시 안전한 stage/classification 출력 및 exit 1을 유지한다.

실제 운영 DB를 테스트에 사용하거나 유료 preflight를 반복하지 않는다.

### 3. [P3] 이미지 식별 및 원인 해결 표현을 후속 보고서에서 정정한다

REPORT 4.1 및 private `startup-diagnosis-20261012.json`의 이미지 ID는 다음과 같다:

```text
reported: sha256:c2b50f46e73cf5633948a8a567d421d20ee28c460f948f2fa4818442fae38e6a
actual:   sha256:c2b50f46e73cf5633948a8a567d421d20ee935ce7c81121df1a0874e42793bee
```

복구 컨테이너 `.Image`와 현재 이미지 태그의 ID는 actual 값으로 일치한다. 컨테이너의 설치된 Python 3.13 `cardrag_worker/cli.py`와 repository 파일 SHA-256도 `d654286609b3e9993e19649f8ee20827b5a157eca68051d06b336560d681360e`로 일치하여 코드 배포 누락 증거는 없다. 잘못 적힌 전체 ID 때문에 추적 근거를 정정해야 한다.

REPORT:165의 “원인 불명의 exit 1 문제를 ... 완전히 해결함”은 확정할 수 없다. “진단 결함 수정, 과거 정확한 원인은 미상, 현재 복구 실행 성공”으로 구분한다. PLAN/REPORT/기존 private 기록을 덮어쓰지 말고 FIX_01_REPORT 및 필요 시 별도 private correction 기록에 정정을 추가한다. Codex 실제 이미지 OCR 근거는 10월 11일 실행이며, 오늘 배치는 OCR 호출 0건이므로 오늘 새 추론 검증으로 표현하지 않는다.

## 독립 운영 검증

- 수동 복구 컨테이너: `cardrag-worker-014-recovery`, exit **0**, OOM false.
- 시작 2026-10-12 09:10:52.329 KST → 종료 10:48:55.201 KST, 컨테이너 약 98분 2.9초. 앱 기록 `elapsed_seconds=5880.174`.
- startup **31.617초**: OCR resolver 0.610 / tokenizer 0.355 / endpoint preflight 27.623 / embedding verification 0.000초.
- run `cb12f9988100445eb7ecd87395abed6d`, status `succeeded`.
- generation `g-cb12f9988100445eb7ecd873-035c4632e085`; local published true.
- local stable pointer와 MCP `current.json`의 generation이 위 값으로 일치. MCP healthy, `/health/ready` → `{"ready":true}`.
- 문서 5,520건, OCR cache reuse 5,520 / OCR provider calls 0 / corpus embedding provider calls 0. startup embedding preflight는 실제 요청을 수행했으므로 전체 외부 호출 0을 뜻하지 않는다.
- collection degraded: 신한 discovery `issuer_origin_network`. 최종 parser metrics에 신한 931건이 유지되며 전체 5,520건, retired 0으로 carry가 동작했다. 기존 정책상 허용되는 카드사 부분 실패이며 이번 초기화 변경의 회귀로 볼 근거는 없다.
- structure failures 0 / fallback 2 / unsupported documents 8. 카드사 404 및 보호 문서 경고도 관찰했다.
- backup succeeded, helper requests 5 / 보고된 uploaded bytes 1,742 / pending OCR 0 / pending bytes 0. 완료 컨테이너의 backup ledger 복사본을 read-only 조회한 status rows는 0, lost_source 0. helper 지표를 전체 실제 HTTP 요청 수로 확대 해석하지 않는다.
- 설정은 Codex 0.162.1 / gpt-6.1-sol / medium / local-paddleocr / PaddleOCR-VL-1.6, legacy custom provider 설정은 빈 값. local publication, immediate backup, inline budget 300초 유지.
- timer 다음 실행 2026-10-13 03:00 KST. 이번 성공은 수동 recovery이며 03시 예약 실행 성공으로 기록하지 않는다.

## 독립 개발 검증 및 한계

- PLAN 지정 3파일 pytest: **161 passed in 2.58s**.
- 변경 2파일 Ruff: **5 errors**.
- 변경 2파일 Ruff format: **2 files already formatted**.
- `uv run --no-sync mypy apps/cardrag-worker/src/cardrag_worker/cli.py`: **Success**.
- Executor의 전체 Worker 1,266 passed 기록은 확인했으며, 전체 suite는 불필요하게 반복하지 않았다.
- 10월 11일 실제 CodexOCRProvider 이미지 OCR 성공 기록을 private verification.json에서 확인했다. 오늘 별도 유료 OCR/embedding 검사는 실행하지 않았다.
- 원본 03시 journal에서 마지막 OCR cache 로그 → 15.914초 종료 / worker_unexpected_failure를 재확인했다. 정확한 당시 예외/HTTP 상태는 복원할 수 없다.
- 모델 캐시 전체 검증, 원격 백업 전체 재다운로드, 장시간 새 배치는 수행하지 않았다.

## Executor 후속

FIX_01의 코드/검증/정정만 수행하고 `FIX_01_REPORT.md`를 새로 작성한다. 검사 통과 후 정확한 commit/image/source 관계를 기록한다. 이번 보완 때문에 전체 운영 배치를 다시 실행하거나 다일 예약 성공을 기다리는 조건을 추가하지 않는다. 이미 성공한 recovery 근거는 보존한다. 운영 변경이 필요하면 기존 볼륨/인증/설정/타이머를 유지한다.

Reviewer는 제품 코드, 운영 설정, 볼륨, 컨테이너 및 타이머를 변경하지 않았다. 새 FIX 문서와 최소 allowlist 증거 JSON만 추가했다.
