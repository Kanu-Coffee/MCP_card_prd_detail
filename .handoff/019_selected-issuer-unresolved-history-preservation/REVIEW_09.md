# 019 REVIEW_09 — FIX_20 보완 필요

2026-10-21 Reviewer: Codex. **FIX_20은 보완 필요이며 FIX_21을 발행한다.** PLAN/PLAN_AMENDMENT_01, FIX_20/REPORT, 이전 FIX/리뷰·운영 인계, HEAD aaeb2c4 위 현재 Git 상태와 실제 FIX_19 release 대비 diff를 대조했다. 기존 문서/소스/미커밋 변경을 보존했다. 이번 리뷰에서는 구현 수정·운영 배포·원격 접근/쓰기·cache 복원·scoped flush·배치/timer 기동을 하지 않았다.

## 재현된 두 문제

### [P2] HTTP 200의 빈 index가 부재로 판정되어 덮어쓰기 허용

`apps/cardrag-worker/src/cardrag_worker/backup.py:2083`은 None과 빈 bytes/bytearray를 모두 absent로 반환한다. 그러나 실제 Worker WebDAVClient.get_bytes는 HTTP404에만 None을 반환하고 HTTP200 빈 body는 b''를 반환한다. fake의 빈 값 호환이 실제 프로토콜 판정으로 확장되어, 존재하지만 유효 JSON이 아닌 index를 새 index로 간주한다.

실제 WebDAVClient facade에 HTTP200/b''를 반환하는 로컬 core stub을 연결한 결과 `state=absent, error_kind=null`이다. 같은 빈 body를 사용한 retained-run scoped flush는 `status=succeeded, scope_complete=true, index_overwritten=true`다. FIX_20의 "실제로 없는 index만 생성; 불확실하면 기존 index를 덮어쓰지 않음" 계약에 어긋난다. 원격 정상 데이터가 유실됐다는 주장은 아니며, 로컬 fixture에서 잘못된 쓰기 허용을 재현한 것이다.

### [P2] receipt 없는 restore의 읽기 실패가 정상 empty 결과로 사라짐

`backup.py:3233`은 unreadable을 경고만 남기고 `:3239`의 빈 receipt 분기로 진행한다. HTTP403 재현 시 내부 error_kind는 http_403이지만 외부 반환은 `{"status":"empty","restored_count":0,"restored_bytes":0}`다. 새 상한 초과/연결/손상도 같은 경로다. CLI restore는 반환을 출력한 뒤 exit0으로 끝나므로 새 호스트에서 백업을 조회하지 못한 장애와 실제 빈 백업을 결과로 구별할 수 없다. 이전 구현에도 있었던 동작이지만 FIX_20이 명시적으로 다루는 restore discovery 및 실패 구분 요구가 아직 충족되지 않았다.

## 재현과 검사

`uv run --no-sync python /tmp/review-fix20-repro.py` — 임시 state와 테스트 fixture만 사용. 실제 WebDAVClient facade의 max_bytes 전달도 67108864로 확인했다.

```text
HTTP200_EMPTY_REAL_FACADE state=absent error_kind=null max_bytes=[67108864]
EMPTY_EXISTING_INDEX_COMMIT status=succeeded scope_complete=true index_overwritten=true index_error=null
RESTORE_HTTP403 result={status:empty,restored_count:0,restored_bytes:0} internal_index_error=http_403
```

기존 및 신규 정상 경로 검증:

```text
uv run --no-sync pytest apps/cardrag-worker/tests/test_fix_06_scoped_backup.py apps/cardrag-worker/tests/test_fix_14_bc_samsung_backup.py apps/cardrag-worker/tests/test_fix_16_snapshot_lock.py -q
121 passed in 3.07s
uv run --no-sync ruff check packages/cardrag-core/ apps/cardrag-worker/
All checks passed!
uv run --no-sync ruff format --check packages/cardrag-core/ apps/cardrag-worker/
156 files already formatted
 git diff --check
통과
```

64MiB 전용 상한, 공통 8MiB 유지, 기본 한도 초과 정상 index의 completion, remote-only 항목 merge 보존, 일반 invalid JSON/transport 오류의 commit 차단, pending 유지·재시도 계약은 확인했다. 전체 Worker/Core 1781 passed/10 warnings는 Executor 보고이며 이번에는 전체를 반복하지 않았다. 통과한 121건에 위 두 부정 경계가 빠져 있어 별도 재현했다.

## 운영 경계

현재 `/opt/cardrag/current`는 여전히 `/opt/cardrag/019-fix19-5d0b40a24a6f`, timer/service inactive다. 운영 현대/BC 실제 completion=true는 이번 리뷰에서 새로 검증하지 않았다. FIX_21과 보고서의 후속 리뷰를 거쳐 다음 배포·정식 현대/BC flush를 검증하고 롯데→신한 수동 검증으로 이어간다. 019/전체8사 인수는 닫지 않는다.
