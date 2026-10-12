# 014 FIX_01 — Reviewer 최종 검토

- 작성: 2026-10-12, Reviewer Codex
- 기준 commit: `a50a815c0fb4b44d953a5793714cd28e12b5e2f4`
- 검토: PLAN.md, REPORT.md, FIX_01.md, FIX_01_REPORT.md, 기존 reviewer 증거, Git 변경 및 실제 이미지/운영 상태.
- Git tracked 변경 없음. 014 handoff는 untracked 상태다.

## 판정

**FIX_01 구현 인수 가능. 추가 FIX는 필요하지 않다.**

기존 P2 두 건은 해결됐다. 사용하지 않는 import와 Ruff 위반이 제거됐고, 독립 `_run_startup_check()` 대신 `_run(None, startup_only=True)`로 운영 초기화를 공유한다. startup-only 반환은 provider 검증 이후, WorkerPipeline 및 BackupLedger 진입 이전에 위치하며 기존 lock-before-DB와 transport/state 정리 경로를 그대로 따른다.

실제 초기화 경로를 실행하는 테스트가 추가됐다. 정상 검사에서 pipeline/backup 생성 금지, 실제 flock 충돌에서 WorkerState/OCR resolver 접근 금지와 worker_busy 계약, tokenizer 실패에서 exit 1 및 안전한 phase/category 출력을 확인한다. 전체 check 함수를 mock하던 이전 테스트의 검증 공백이 해소됐다.

이미지 전체 ID와 과거 원인 신뢰도는 후속 보고서 및 별도 private correction 기록에서 정정됐다. 원본 private 기록은 이전 값으로 보존되어 있으며, 이력 보존 요구에 맞는다. 03시 정확한 원인은 미상이고 진단 결함 수정 및 현재 복구 성공을 구분해야 한다는 제한은 계속 유효하다.

## 독립 검증

```text
uv run --no-sync pytest -q apps/cardrag-worker/tests/test_cli_settings_provider.py apps/cardrag-worker/tests/test_embedding_v5.py tests/runtime_v1/test_container_runtime_contract.py
163 passed in 2.58s

uv run --no-sync ruff check apps/cardrag-worker/src/cardrag_worker/cli.py apps/cardrag-worker/tests/test_cli_settings_provider.py
All checks passed!

uv run --no-sync ruff format --check apps/cardrag-worker/src/cardrag_worker/cli.py apps/cardrag-worker/tests/test_cli_settings_provider.py
2 files already formatted

uv run --no-sync mypy apps/cardrag-worker/src/cardrag_worker/cli.py
Success: no issues found in 1 source file

git diff --check
exit 0
```

새 이미지 `cardrag-worker:013-50a0129-codex-01621`의 실제 ID:

```text
sha256:aaec660b6885c1f9e8ecc68ebb722c26a03b250571372344d305d6760accd7ee
```

네트워크 없이 생성만 한 임시 컨테이너에서 설치된 `/opt/cardrag/lib/python3.13/site-packages/cardrag_worker/cli.py`를 추출했다. Git 소스와 SHA-256이 일치한다:

```text
ead16158c1106e1099102295e53f66e7ca9198f91257b5bdbd2cd1d89e6c88fe
```

임시 컨테이너는 실행하지 않고 제거했다. 유료 OCR/embedding 검사, 실제 startup-check 및 전체 배치는 반복하지 않았다. Executor가 기록한 새 이미지의 `startup-check --help` 및 설정 없는 실패 검사는 CLI packaging/안전한 실패 검증이며 운영 인증 환경의 startup 성공 검증을 뜻하지 않는다.

## 운영 상태 및 증거 구분

기존 `cardrag-worker-014-recovery`는 exit 0을 유지한다. 이 전체 배치는 이전 구현 `7d07b1d` 이미지에서 완료됐으며 새 `a50a815` 이미지의 전체 배치 성공으로 바꿔 해석하지 않는다. 단순 초기화 경로 통합 보완에 대해 전체 배치를 재실행할 필요는 없다.

MCP healthy 및 `/health/ready`의 ready true를 재확인했다. MCP current pointer와 local stable pointer 모두 다음 generation으로 일치한다:

```text
g-cb12f9988100445eb7ecd873-035c4632e085
```

기존 독립 증거 `evidence/reviewer-20261012.json`에 기록된 OCR cache 5,520건 / 신규 OCR 0 / 백업 성공 / pending 및 lost 0의 완료 판정은 보존한다. timer 다음 실행은 2026-10-13 03:00 KST이며, 예약 성공을 새로 관찰한 것은 아니다.

## 기록 정정 — 추가 인수 차단 조건 없음

FIX_01_REPORT 2.4 및 private `startup-diagnosis-fix-01.json`에 기재된 recovery 시각/소요시간은 실제 inspect/log와 다르다. 정확한 값은 이전 독립 증거 및 이번 재확인에 따른 아래 값이다:

- 컨테이너 시작: 2026-10-12 **09:10:52.328729 KST**.
- 컨테이너 종료: 2026-10-12 **10:48:55.200512 KST**.
- 컨테이너 소요시간: 약 **5,882.872초**.
- 앱 `Worker execution finished`: **5,880.174초**. 보고서의 5,879.163초는 정정한다.

새 private correction의 `diagnostic_defect`에 있는 “leaked potential payload”는 실제 과거 비밀정보 유출의 증거로 해석하지 않는다. 원래 확인된 결함은 상세 예외를 버린 것이며, 새 구현은 안전한 진단을 추가했다. Codex 실제 이미지 OCR 근거는 10월 11일 실행이며 오늘 cache-only 배치가 새 추론 검증을 제공하지 않는다는 구분도 유지한다.

이 정정은 본 문서에 추가하여 기존 이력을 보존한다. 구현/운영 결과를 뒤집는 결함은 아니므로 별도 FIX나 배치 재실행을 요구하지 않는다.

Reviewer는 제품 코드, 운영 설정, 기존 컨테이너, 데이터 볼륨 및 타이머를 변경하지 않았다. 인수 문서만 추가했다.
