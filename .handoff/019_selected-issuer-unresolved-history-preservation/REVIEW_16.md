# 019 REVIEW_16 — FIX_29 복구점 검증 보완 통과

2026-10-22 Codex, Reviewer. FIX_29/REPORT, REVIEW_15 및 기존 계획·FIX_28 경계와 현재 verifier/테스트·운영 복사본·실제 snapshot을 대조했다. 구현/설정 변경 없이 검증했다.

## 판단

**FIX_29 통과. 앞선 DB 누락/nonempty WAL의 잘못된 통과 두 건은 해소됐고 추가 필수 FIX는 발견하지 않았다.** FIX_28의 복구 안내와 FIX_26 정리 후 복구점 안내에 대한 REVIEW_14–15 보완도 함께 종결할 수 있다. 실제 rollback/DB restore 호환성이나 오늘10시 배치 성공까지 승인한 의미는 아니다.

- evidence의 DB key 집합을 worker-state.sqlite3/backup-ledger.sqlite3로 고정하며 metadata 형식·실제 파일/비symlink/크기·SHA를 검사한다. 필수 파일·항목 누락/추가 경로가 성공 처리되지 않는다.
- immutable 검사 전 nonempty WAL/journal을 거부하고0bytes WAL은 허용한다. DB/evidence/sidecar lstat identity를 검사 전후 비교한다. offline snapshot/read-only mount 조건을 유지하며 실제 외부 writer lock이나 DB restore 호환성 검증으로 확대해 주장하지 않는다.
- active/rollback의 현재 guidance와 verifier는 각각 cmp 일치하며 repo verifier와도 바이트 일치한다. 단일 최신 snapshot·역사 경로 구분·실제 복원 미수행 경계를 유지한다.

## Reviewer 직접 검사

```text
uv run --no-sync pytest apps/cardrag-worker/tests/test_fix_29_recovery_verifier.py -q
17 passed in0.10s

uv run --no-sync ruff check deploy/worker/verify-recovery-point.py apps/cardrag-worker/tests/test_fix_29_recovery_verifier.py
All checks passed!
uv run --no-sync ruff format --check deploy/worker/verify-recovery-point.py apps/cardrag-worker/tests/test_fix_29_recovery_verifier.py
2 files already formatted
git diff --check / systemd-analyze verify / timer cmp
통과
```

17건의 정상/필수 DB 누락/evidence 누락/경로·metadata·identity 오류/symlink/각DB nonempty WAL/zero WAL/journal/CLI 실패 동작을 코드와 대조했다. Executor의 전체1836 passed 보고는 별도 근거이며 이번 Reviewer 턴에서 전체 suite를 반복하지 않았다.

실제 active guidance를 read-only/network-none container에서 신규 verifier로 다시 실행했다. protected=019-fix24-current-20261022, verified=true, 두DB의 size/SHA 및 quick_check=ok/WAL0, latest=666c2d2ee9e24d04acf76eed58123a15/succeeded, restore_performed=false/state_written=false다. `/tmp/reviewer-fix29-current-recovery.json`에 결과 보관. 별도로 배포 보고의 active/rollback 검증 JSON도 두DB/zero WAL/무쓰기 통과를 확인했다. Reviewer의 실제 전체 snapshot 검사는 active 경로1회다.

## 현재 운영 경계

image는 기존FIX_24, mode=round_robin, proxy 빈 값 유지. timer active/enabled/Persistent=no, next2026-10-22 10:00KST이며 installed timer와 repo가 일치한다. 이번 검증으로 Worker나 provider 배치를 시작하지 않았다. 현재07시대이므로 오늘10시의 실제 우리 배정·실행·백업은 아직 후속 확인 대상이다. 019 최종 자동운영 인수는 그 런타임 증거 전까지 열어 둔다.
