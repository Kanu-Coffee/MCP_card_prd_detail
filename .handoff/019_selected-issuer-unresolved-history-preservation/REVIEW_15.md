# 019 REVIEW_15 — FIX_28 경로 안내 보완 확인, 신규 검증기 P2 보완 필요

2026-10-22 Codex, Reviewer. FIX_28/REPORT, REVIEW_14, 기존 PLAN/변경계획·수정 이력과 현재 source/Git/운영 guidance를 대조했다. 구현 수정·snapshot/DB 쓰기·예약 변경·Worker 시작은 하지 않았다.

## 판단과 발견 사항

FIX_28의 원래 문제인 active/rollback 복구점 안내 불일치는 해소됐다. 두 release의 recovery-point.json이 동일하고, historical snapshot/removed previous release를 현재 복구점으로 오인하지 않게 구분한다. 실제 DB restore 미승인·호환성 미검증 경계도 명시했다.

다만 새 verify-recovery-point.py에서 **P2 두 건을 재현하여 최종 통과는 보류하고 FIX_29를 발행한다.** 현재 정상 protected snapshot을 손상됐다고 판정하거나 오늘10시 운영 배치를 막는 결함은 아니다.

1. deploy/worker/verify-recovery-point.py:54–67은 evidence.databases에 기재된 항목만 확인한다. 별도 worker-state 조회는 있지만 backup-ledger 필수 존재 확인이 없어, worker DB만 있는 fixture도 verified=true였다. 안내가 약속하는 두DB 검증의 누락이다.
2. 같은 파일:63–74는 nonempty WAL 유무를 확인하지 않고 immutable main DB를 검사한다. 두 정상 DB fixture에29bytes worker-state WAL을 추가해도 verified=true였다. immutable은 WAL을 무시하므로 실제 미checkpoint snapshot을 잘못 통과시킬 수 있다.

이는 임시 디렉터리의 자체 SQLite/evidence/guidance로 직접 재현했다. metadata hash는 각 fixture에 맞게 정상 생성하여 hash 불일치가 검사 누락을 가리지 않게 했다. 원본 운영 파일을 손상시키거나 수정하지 않았다.

```text
fixture=missing_backup_ledger verified=true backup_database_exists=false
fixture=nonempty_snapshot_wal verified=true wal_bytes=29
```

## 정상 경로 및 현재 운영 확인

- 실제 active guidance로 신규 검증기를 read-only/network-none container에서 실행: verified=true, protected=019-fix24-current-20261022, latest=666c2d2ee9e24d04acf76eed58123a15/succeeded, restore_performed=false/state_written=false. 결과 `/tmp/reviewer-fix28-current-recovery.json`.
- active/rollback guidance 파일 cmp 일치, repo와 양쪽 deployed verifier 파일 cmp 일치. 동일 guidance/스크립트이므로 실제 snapshot 전체 검사는 active 경로1회 수행했으며 양쪽을 별도2회 실행했다고 주장하지 않는다.
- ruff check/format check, git diff --check 통과. lint가 위 복구 의미 검증을 대신하지 않는다. 이전 REVIEW_14에서 현재 두DB SHA/quick_check 및 WAL0을 직접 확인한 증거와도 일치한다.
- timer active/enabled, next2026-10-22 10:00KST 유지. 현재 snapshot에 필수 DB 누락/비정상 WAL이 발견된 것은 아니다. code/image rollback과 DB restore는 계속 분리하고 실제 복원은 하지 않았다.

FIX_29는 필수 DB 고정집합/형식·경로 검증, nonempty WAL 거부와 회귀 fixture를 요구한다. 새 image·snapshot 복제·자동 순회/프록시 정책 변경 없이 repo/deployed verifier를 함께 보완하면 된다. 오늘10시 실제 배치·backup 완주는 아직 미실행 검증 항목이며019는 열린 상태다.
