# 019 REVIEW_05 — FIX_15 보완 필요

2026-10-20 Reviewer: Codex. FIX_15/REPORT, 이전 PLAN/변경계획·FIX·리뷰 계약과 현재 Git/코드를 대조했다. HEAD `aaeb2c4` 위 기존 미커밋 변경을 보존했다. **FIX_15 전체 승인 보류, FIX_16 발행.** 배포·배치·timer 기동·운영 원장/원격 쓰기·삭제는 하지 않았다.

## 확인한 개선

- 현재 native bundle+과거 태그의 오탐, manifest 소실 후 **pending 상태**의 거짓 완료, 로컬 cache 증거 재바인딩, 제외 카운트의 CAS 중복 문제를 다루는 회귀가 추가됐다. 관련 106개 테스트와 변경 파일의 ruff check/format check, git diff --check는 이번 리뷰에서도 통과했다.
- BC variant 수정은 유지된다. FIX_15_REPORT는 현재/과거 DB 크기 차이를 인정하고 새 일관된 snapshot 필요성을 명시했다. 정리 후보를 제시했으나 이번 리뷰는 실제 삭제/회수량의 승인이 아니다.

## 1. [P1] lost_source인 현재 소유 metadata가 scope에서 사라짐

`backup.py:1238–1243`은 `status='pending'` 경로만 수집하고 `1354`에서 이 집합만 보호한다. 현재 run의 소유 metadata가 `lost_source`가 되면 content/adopted의 과거 cache 제외 분기로 들어간다. 필수 artifact의 ownership을 전송 대기 상태로 판단하므로 pending-independent 계약이 아직 충족되지 않는다.

새 `test_samsung_content_run_with_lost_native_bundle_blocks_completion`을 임시 디렉터리에서 그대로 실행한 다음, 그 fixture의 metadata 대기 2건만 `lost_source`로 전환했다. CAS receipt와 이에 일치하는 mock remote index를 두고 scope/flush를 호출했다. 운영 SQL을 변경한 재현이 아니다.

```text
LOST_SOURCE allowed=2 bound=2
  backup_in_scope_remaining=0
  backup_in_scope_lost_source=0
  backup_in_scope_unbound=0
  backup_excluded_shared_cache=4
FLUSH status=unchanged
  backup_scope_complete=true
  in_scope_lost_source=0
  remaining_pending=2
```

원본은 같은 문서의 현재 소유 bundle인데 transport 상태만 바꿔 완료 판정이 뒤집힌다. lost_source 보호는 필수이며, 행이 없거나 정리된 상태도 현재 ownership 근거와 구분해야 한다. 단순 `pending` 조건을 늘리는 것만으로 pending-independent 증명이 끝나지는 않는다. 정상 삼성의 과거 shared-cache 2건 제외는 계속 유지해야 한다.

## 2. [P2] 읽기 전용 모니터가 새 instance 상태 때문에 실패

운영 `operations/monitor-state.py:110–116`의 ReadOnlyBackupLedger는 쓰기를 피하려고 db_path만 설정한다. 이번 scope 함수는 `_last_excluded_shared_cache_by_run`이 반드시 초기화됐다고 가정한다. 기존 실제 운영 RO helper와 같은 subclass에 최신 backup.py를 RO overlay하여 network-none으로 삼성 scope를 조회하자 다음 오류가 발생했다.

```text
backup.py:1382
AttributeError: 'RO' object has no attribute '_last_excluded_shared_cache_by_run'
```

운영 helper를 그대로 복사해 새 이미지에 연결하면 status/summary에서 이 경로가 실패할 수 있다. 정상 BackupLedger 생성자를 호출해 쓰기를 유발하는 우회는 금지한다. pure scope 결과/명시적 진단 전달 또는 RO 초기화 호환성을 갖추고 실제 helper 경로를 검증해야 한다.

## 3. [P2] 새 보고서의 배포 절차가 여전히 불완전하며 운영 문서는 미수정

- 저장소에는 `operations/`가 없다. REPORT 4.2는 rsync 후 private overlay 한 파일만 복사하므로 worker-compose.sh/activate.sh/deployment.json을 준비하지 못한다. activate.sh는 이 파일들을 사용한다. private overlay의 이미지 변경도 없으므로 새 이미지로 전환된다는 보장이 없다.
- TREE_SHA가 `git diff HEAD`만 해시하므로 untracked source 파일을 포함하지 않는다. 현재 acquisition/issuer_scope 등 실제 제품 소스가 untracked다. 이 파일만 바뀌면 동일 식별자가 된다. 대안 code-image.Dockerfile의 label도 기존 소스 digest/release로 고정돼 있어 그대로 재사용하면 안 된다.
- rollback은 `worker-compose.sh ps -q`만 확인한다. 수동 run 컨테이너·별도 프로세스 lock·활성 timer/service를 모두 확인하는 절차가 아니다. 실제 activate.sh는 프로젝트/서비스 label로 docker ps를 검사하고 timer/service도 확인한다. 같은 검증된 경계를 사용해야 한다.
- `docs/OPERATIONS.md:410–431`은 기존 그대로다. 용량 조회 glob이 호스트에서 처리되고 mount도 RW이며, DB 재사용 조건도 snapshot 동일성 검증으로 개정되지 않았다. 새 보고서뿐 아니라 canonical 운영 문서도 보완해야 한다.

## 검증 기록

- `uv run --no-sync pytest apps/cardrag-worker/tests/test_fix_06_scoped_backup.py apps/cardrag-worker/tests/test_019_unresolved_history.py apps/cardrag-worker/tests/test_fix_14_bc_samsung_backup.py -q`: **106 passed, 6.10s**.
- 변경 backup.py/신규 회귀 파일 ruff check, format check: 통과. git diff --check: 통과.
- 임시 추가 재현 `/tmp/review_fix15_loss.py`: lost_source false completion 확인. 운영 state RO + network-none + 최신 backup.py overlay로 RO subclass 오류 확인.
- 전체 1743개 테스트는 Executor 보고 결과이며 이번 리뷰에서 재실행하지 않았다. 운영 재배포/BC 완주/삼성 remote-index 완료는 아직 새로 수행하지 않았다.
