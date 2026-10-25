# 019 REVIEW_10 — FIX_21 구현 리뷰 통과

2026-10-21 Reviewer: Codex. **FIX_21 구현 리뷰 통과, 추가 FIX 없음.** PLAN/PLAN_AMENDMENT_01, FIX_20/REPORT, REVIEW_09, FIX_21/REPORT와 현재 Git 상태·backup/CLI/테스트/운영 문서를 대조했다. HEAD aaeb2c4 위 기존 미커밋 변경과 과거 handoff를 보존했다. 이번 리뷰에서는 운영 배포·원격 접근/쓰기·cache 복원·scoped flush·카드사 배치/timer 기동을 하지 않았다.

## 이전 두 차단 문제 재검증

1. **존재하는 zero-byte index의 보존**: absent는 명시적 HTTP404/None으로 한정됐다. HTTP200 b''/빈 bytearray는 invalid_json/unreadable이다. 실제 WebDAVClient facade를 통한 로컬 core stub과 이전 재현 스크립트에서 64MiB max_bytes 전달 및 unreadable 판정을 확인했다. pending 있는 scoped flush는 pending_commit/complete=false, index/batch 게시 없음, 기존 body·pending 유지다. pending 없는 완료는 null로 남는다. HTTP404 최초 index 생성과 유효 items=[]는 유지된다. fake의 부재 반환만 None으로 보완했으며 실제 프로토콜을 약화시키지 않았다.
2. **restore discovery 오류 전달**: receipt 없는 호스트에서 index 조회가 unreadable이면 failed와 typed index_read_error를 반환한다. CLI는 failed/locked에 exit1로 종료하며 정상 부재·빈 index는 empty/exit0이다. 오류 조기 반환에서 recovery lock을 해제하고 소유 client만 close한다. HTTP403/transport/size_limit/빈 body/invalid JSON/client contract 회귀 및 오류 직후 재시도, owned client·CLI 경로를 확인했다.

별도 Reviewer 재현 명령: `uv run --no-sync python /tmp/review-fix20-repro.py`. 임시 state/fixture만 사용했다.

```text
HTTP200_EMPTY_REAL_FACADE state=unreadable error_kind=invalid_json max_bytes=[67108864]
EMPTY_EXISTING_INDEX_COMMIT status=pending_commit scope_complete=false index_overwritten=false index_error=invalid_json
RESTORE_HTTP403 status=failed index_read_error=http_403 restored_count=0
```

FIX_20의 backup index 전용 64MiB 유한 한도/공통8MiB 기본 유지, >8MiB 정상 index completion/repair·restore, remote-only merge 보존과 읽기 오류 시 게시 중단·pending 보존은 관련 회귀에서 유지됐다. FIX_18–19의 cache/pair 보호 회귀도 포함했다. 검토 범위에서 새로 재현 가능한 차단 결함은 확인하지 못했다.

## Reviewer 실행 검사

```text
uv run --no-sync pytest apps/cardrag-worker/tests/test_fix_06_scoped_backup.py apps/cardrag-worker/tests/test_fix_14_bc_samsung_backup.py apps/cardrag-worker/tests/test_fix_16_snapshot_lock.py apps/cardrag-worker/tests/test_local_serving_and_backup.py -q
142 passed in 3.33s

uv run --no-sync ruff check packages/cardrag-core/ apps/cardrag-worker/
All checks passed!

uv run --no-sync ruff format --check packages/cardrag-core/ apps/cardrag-worker/
156 files already formatted

git diff --check
통과
```

전체 Worker/Core 1791 passed/10 warnings는 FIX_21 Executor 보고를 확인했으며 이번 리뷰에서 전체를 반복하지 않았다. 이 통과는 코드/fixture 검증이고 실제 운영 backup_scope_complete=true의 새 증거는 아니다.

## 후속 운영 단계

현재 `/opt/cardrag/current`는 `/opt/cardrag/019-fix19-5d0b40a24a6f`이며 timer/service inactive다. FIX_20–21은 아직 운영 미반영이다.

기존 사용자 승인 범위의 다음 운영 단계에서 현재 state/복구점/용량/실행 lock 및 source 일치가 검증된 새 release로 전환한다. 현대 `10f236331a2744f78980f2b878475b64`, BC `8810e405d952441faf9e560ac19fd7b0`는 retained run을 다시 식별한 뒤 정식 scoped flush에서 backup_scope_complete=true, 실제 uploads0/재시도 멱등, remote committed index·receipt 불변을 확인한다. 원격 상태를 읽기 위해 receipt/seal/완료 필드를 수작업 수정하지 않는다.

그 뒤 롯데→신한을 사용자가 순차 수동 실행하고 각각 비선택 문서 불변·선택 scope 백업 완료·live MCP serving을 확인한다. 두 카드사 배치 완주·전체8사 인수 전에 019를 닫거나 timer를 재개하지 않는다.
