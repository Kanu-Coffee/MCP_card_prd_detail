# 019 FIX_29_REPORT — 복구점 검증기의 필수 DB·offline WAL 조건 검증

2026-10-22 Executor. FIX_29의 P2 두 건을 보완했다. repo verifier와 active/rollback operations 복사본을 함께 수정했고, 실제 protected snapshot은 읽기 전용으로 재검증했다. 새 image, snapshot 재생성, 실제 rollback/DB 복원, timer 변경, Worker 실행은 하지 않았다.

## P2-1 — 필수 DB 집합 검증

- `evidence.databases`가 정확히 `worker-state.sqlite3`와 `backup-ledger.sqlite3` 두 항목을 포함하도록 고정 집합으로 검사한다.
- 추가 항목/누락 항목/임의 경로를 거부한다.
- 각 DB metadata의 `size`는 non-bool nonnegative int, `sha256`은 64자 lowercase hex인지 검사한다.
- 각 DB 파일은 regular file·non-symlink·snapshot 경계 내부인지 확인한 뒤 size/SHA-256을 대조한다.
- snapshot `snapshot.json`도 regular file·non-symlink·guidance hash 일치를 확인한다.

## P2-2 — offline WAL/sidecar 조건

- immutable read 전에 각 필수 DB의 `-wal`, `-shm`, `-journal` sidecar를 검사한다.
- nonempty WAL은 fail closed하며, nonempty rollback journal도 실패한다.
- 0-byte WAL은 허용하고 기존 SHM 읽기 동작은 유지한다.
- DB/SHM/sidecar/ evidence의 `lstat` identity(dev/inode/size/mtime_ns)를 검사 전후 비교해 검증 중 변경이 감지되면 실패한다.
- snapshot DB/sidecar를 열어 checkpoint·삭제·절단하지 않는다. live Worker DB를 immutable 검사하는 도구로 확대하지 않았다.

## 문서

- `docs/RECOVERY.md`에 두 DB 필수 집합과 nonempty WAL/journal fail-closed 조건을 명시했다.

## 테스트

신규 `apps/cardrag-worker/tests/test_fix_29_recovery_verifier.py` 17건:

- 정상 두 DB snapshot/latest-run
- worker-state 또는 backup-ledger 누락
- evidence 필수 항목 누락, 추가·경로 이탈 항목
- metadata size/hash 형식 오류
- 실제 size/hash 불일치
- DB symlink
- 각 DB의 nonempty WAL
- 0-byte WAL 허용
- nonempty journal
- 존재하는 historical snapshot 거부
- CLI가 traceback 없이 `verified=false`/exit1로 종료

```text
uv run --no-sync pytest apps/cardrag-worker/tests/test_fix_29_recovery_verifier.py -q
17 passed in 0.10s

uv run --no-sync pytest apps/cardrag-worker/tests/ packages/cardrag-core/tests/ -q
1836 passed, 11 warnings in 58.98s

uv run --no-sync ruff check apps/cardrag-worker/ packages/cardrag-core/ deploy/worker/verify-recovery-point.py
All checks passed!

uv run --no-sync ruff format --check apps/cardrag-worker/ packages/cardrag-core/ deploy/worker/verify-recovery-point.py
161 files already formatted

git diff --check
통과
```

## 실제 snapshot 재검증

active/rollback release의 verifier가 바이트 일치함을 확인하고 각각 read-only/network-none container에서 실행했다.

```text
active  : verified=true, protected=019-fix24-current-20261022,
          latest=666c2d2ee9e24d04acf76eed58123a15/succeeded,
          wal_bytes={worker-state:0, backup-ledger:0},
          restore_performed=false, state_written=false
rollback: verified=true, protected=019-fix24-current-20261022,
          latest=666c2d2ee9e24d04acf76eed58123a15/succeeded,
          wal_bytes={worker-state:0, backup-ledger:0},
          restore_performed=false, state_written=false
```

- active/rollback verifier와 `recovery-point.json`은 각 release 내에서 동일하다.
- 삭제된 historical snapshot을 current로 지정한 음수 fixture는 `verified=false`, exit1로 거부됐다.
- active/rollback 양쪽에서 동일 protected snapshot 하나를 선택했다.
- 결과 증거: `operations/live/fix29.recovery-active.json`, `fix29.recovery-rollback.json`

## 운영 보존

- active release/image: `/opt/cardrag/019-fix24-11d1321cc9e6` / `sha256:d08b61a6...`
- rollback release/image: `/opt/cardrag/019-fix23-e9b9cdca1a22` / `sha256:15124ebc...`
- protected snapshot: `019-fix24-current-20261022` 하나, 재생성·복제 없음
- latest run/stable: `666c2d2ee9e24d04acf76eed58123a15` / `g-666c2d2ee9e24d04acf76eed-d6ffd9a927e9`
- timer: active/enabled, `Persistent=no`, next `2026-10-22 10:00 KST`
- MCP smoke: 2 source page 성공, current=Worker stable
- 실행 중 Worker: 0
- 실제 rollback/DB restore/overwrite, backup flush, provider 배치: 미실행

## 한계

- 검증기는 snapshot의 내부 상태와 WAL 부재를 확인하지만, 실제 DB restore 후 rollback image와의 schema/contract 호환성을 증명하지 않는다. FIX_28의 `database_restore` 미승인/미검증 경계를 유지한다.
- state-change 감지는 검증 구간의 lstat identity 비교이며, 외부 writer를 차단하는 lock을 새로 도입한 것은 아니다. snapshot은 현재 offline 상태와 read-only mount에서 검사했다.
- 실제 10시 자동 배치와 backup 완주는 계속 런타임 후속 검증 항목이다.