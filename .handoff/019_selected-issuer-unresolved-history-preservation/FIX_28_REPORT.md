# 019 FIX_28_REPORT — 정리 후 롤백 release의 복구점 안내 정합성

2026-10-22 Executor. FIX_28의 P2에 따라 active/rollback release가 현재 protected snapshot을 동일하게 발견·검증하도록 안내를 보완했다. 실제 rollback, DB 복원, timer 변경, Worker 실행은 하지 않았다. active/latest/MCP/10시 예약은 보존했다.

## 문제 재확인

- active `019-fix24-11d1321cc9e6` metadata는 새 snapshot `019-fix24-current-20261022`를 가리킨다.
- rollback `019-fix23-e9b9cdca1a22`의 `snapshot_directory`는 삭제된 `/state/deployment-backups/019-fix23-e9b9cdca1a22`를 계속 가리킨다.
- rollback의 `previous_release`는 이미 host에서 제거된 `/opt/cardrag/019-fix21-afdcabf41ea2`를 가리킨다.
- 따라서 역사 metadata를 현재 복구점으로 오인하지 않도록 별도 guidance와 읽기 전용 검증 경로를 추가했다.

## 변경

### Repo

- `deploy/worker/verify-recovery-point.py` 신규.
  - `recovery-point.json`의 protected snapshot 하나만 선택.
  - snapshot 디렉터리 symlink/경로 이탈 거부.
  - `snapshot.json` SHA-256, 두 DB 크기/SHA-256, SQLite immutable `quick_check`, latest run 일치 검증.
  - code/image rollback과 DB restore를 분리해 출력하며 복원·쓰기를 수행하지 않음.
- `docs/RECOVERY.md`에 현재 복구점 확인 명령과 `deployment.json.snapshot_directory`가 역사값일 수 있다는 경계를 추가.
- `docs/OPERATIONS.md`에 active/rollback 동일 guidance, 역사 경로 `pruned`, DB restore 별도 호환성 검증 원칙을 추가.
- `docs/OPERATIONS.md`의 BuildKit cache 안내를 수정. 공유 builder cache의 타 프로젝트 영향을 명시하고 `--filter until=...`/`buildx du` 기반 표적 정리를 기본으로 했으며, `docker builder prune --all --force`는 명시 승인된 일회성 작업으로 한정했다. 자동 정책에는 추가하지 않는다.
- `deploy/README.md`에 verifier 항목 추가.

### Operations (release 내부)

active와 rollback release 양쪽 `operations/`에 동일한 파일을 배치했다.

- `recovery-point.json`
  - current protected snapshot: `019-fix24-current-20261022`
  - `snapshot_directory`: `/state/deployment-backups/019-fix24-current-20261022`
  - `snapshot_evidence_sha256`: `6407208adce9d5565183eca1ba7d89dd88af2f9e1ec4822bd59a6e33ef38ca0f`
  - historical rollback snapshot: `/state/deployment-backups/019-fix23-e9b9cdca1a22` (`pruned_missing`)
  - historical previous release: `/opt/cardrag/019-fix21-afdcabf41ea2` (`removed_from_host`)
  - code rollback: fix23 release/image, `scope=code/image rollback only`
  - DB restore: `authorized=false`, `performed=false`, `compatibility_verified=false`
- `verify-recovery-point.py`: repo script와 바이트 동일.

Deployment metadata는 역사값을 덮어쓰지 않고 다음을 추가했다.

- active `deployment.json`: `recovery_guidance`, `historical_snapshot_directory`(fix24), `historical_snapshot_status=pruned`, `previous_release_status=available_rollback`.
- rollback `deployment.json`: 기존 `snapshot_directory`(fix23) 유지, `current_recovery_snapshot_directory`/`recovery_guidance` 추가, `historical_snapshot_status=pruned`, `previous_release_status=removed_from_host`.
- rollback `snapshot-retention.json`: 기존 `protected_snapshot`을 역사값으로 유지하고 `current_protected_snapshot`/`current_snapshot_directory`/`guidance` 추가.
- active `snapshot-retention.json`: protected current snapshot과 guidance 경로 추가.

## 검증

두 release의 guidance를 각각 실제 state volume에 대해 검증했다.

```text
active  : verified=true, protected=019-fix24-current-20261022,
          latest=666c2d2ee9e24d04acf76eed58123a15/succeeded,
          stable=g-666c2d2ee9e24d04acf76eed-d6ffd9a927e9,
          historical_snapshot_exists=false, restore_performed=false, state_written=false

rollback: verified=true, protected=019-fix24-current-20261022,
          latest=666c2d2ee9e24d04acf76eed58123a15/succeeded,
          stable=g-666c2d2ee9e24d04acf76eed-d6ffd9a927e9,
          historical_snapshot_exists=false, restore_performed=false, state_written=false
```

- 두 guidance가 동일한 protected snapshot과 evidence hash를 선택함.
- `worker-state.sqlite3` 7,403,823,104 bytes / `28d64d2c...` / quick_check ok
- `backup-ledger.sqlite3` 24,416,256 bytes / `d138d3eb...` / quick_check ok
- 삭제된 fix23 snapshot을 current로 지정한 음수 fixture는 `verified=false`, `protected snapshot directory is unavailable`로 exit1. 삭제된 역사 경로가 현재 복구점으로 선택되지 않음을 확인.
- repo script: `python3 -m py_compile`, `ruff check`, `ruff format --check`, `git diff --check` 통과.
- repo script와 active/rollback operations 복사본은 `cmp` 일치.

## 운영 보존

- active release/image: `/opt/cardrag/019-fix24-11d1321cc9e6` / `sha256:d08b61a6...`
- rollback release/image: `/opt/cardrag/019-fix23-e9b9cdca1a22` / `sha256:15124ebc...`
- protected snapshot: `019-fix24-current-20261022` 하나
- latest run: `666c2d2ee9e24d04acf76eed58123a15` (shinhan, succeeded)
- stable generation: `g-666c2d2ee9e24d04acf76eed-d6ffd9a927e9`
- MCP smoke: source page 2건 성공, MCP current=Worker stable
- timer: active/enabled, `Persistent=no`, next `2026-10-22 10:00 KST`
- 실행 중 Worker: 0

## 한계

- protected snapshot은 현재 fix24/shinhan state이고 rollback image는 fix23이다. 두 schema/contract의 호환성을 실제 복원으로 검증하지 않았으므로 DB snapshot restore는 안전하다고 선언하지 않는다. 안내에도 `compatibility_verified=false`, `authorized=false`, `performed=false`로 기록했다.
- code/image rollback과 DB restore는 별개다. rollback release의 역사 `snapshot_directory`와 삭제된 previous release는 기록으로만 남기고 현재 복구 경로로 사용하지 않는다.
- 이번 턴에는 실제 rollback, DB overwrite, timer 중지/재활성화, Worker 실행, backup flush를 수행하지 않았다.
- FIX_26의 builder cache `--all` prune은 이미 완료됐다. 재생성하지 않았고, 향후에는 기간/크기 표적 정리와 명시 승인 경계를 문서화했다.