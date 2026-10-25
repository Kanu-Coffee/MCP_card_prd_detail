# 019 FIX_21_REPORT — 빈 index와 restore 읽기 실패의 판정 보완

작성: 2026-10-21, Executor OpenCode. `FIX_21.md`의 두 P2를 구현했다. REVIEW_09의 재현
두 건은 수정 후 각각 unreadable/commit 차단, restore failed/CLI nonzero로 전환됨을
확인했다. 기존 handoff 문서는 보존했고 이 파일만 신규 산출물이다. 기준: HEAD
`aaeb2c4` + 019 미커밋 작업 트리(FIX_01–20 변경 보존). 코드 변경:
`apps/cardrag-worker/src/cardrag_worker/backup.py`,
`apps/cardrag-worker/src/cardrag_worker/cli.py`,
`apps/cardrag-worker/tests/test_fix_06_scoped_backup.py`,
`apps/cardrag-worker/tests/test_local_serving_and_backup.py`, `docs/OPERATIONS.md`.
운영 배포·원격 접근/쓰기·cache 복원·scoped flush·배치/timer 기동·원격 index/receipt/
seal/DB 수정은 수행하지 않았다.

## 1. [P2] HTTP200 빈 body는 부재가 아니라 읽을 수 없는 index — 해결

`_read_backup_index`의 absent 판정을 **명시적 404/None 계약**으로 한정했다.

- `raw is None` → `absent` (실제 WebDAVClient의 HTTP404).
- `b""`/`bytearray()` (HTTP200 zero-byte) → `unreadable` / `error_kind=invalid_json`
  (정상적인 `items=[]` JSON과 구별).
- 결과: 빈 body가 있는 기존 index는 새 index로 취급되지 않는다.
  - no-pending scoped flush: `status=unchanged`, `backup_scope_complete=null`,
    `index_read_error=invalid_json`.
  - pending 있는 commit: `status=pending_commit`, `index_read_error=invalid_json`,
    `error`에 "refusing to overwrite" 포함, index/batch PUT 0, 기존 zero-byte body와
    pending 행 보존.
  - 실제 404/None 최초 commit은 기존대로 새 index를 생성하고, 유효한 `items=[]`
    문서는 `present`로 읽는다.
- 부재를 `b""`로 반환하던 로컬 테스트 fake는 실제 계약(`None`)으로 보완했다.
  실제 프로토콜 판정을 fake 편의에 맞추지 않는다.
- 실제 `WebDAVClient` facade + 로컬 core stub으로 HTTP200 zero-byte / HTTP404 /
  유효 items=[]를 검증했고, `get_bytes(max_bytes=67108864)` 전달도 확인했다.
  공통 8MiB 기본값은 변경하지 않았다.

REVIEW_09 재현 스크립트 재실행(현재 코드):

```text
{"case": "HTTP200_EMPTY_REAL_FACADE", "state": "unreadable",
 "error_kind": "invalid_json", "max_bytes": [67108864]}
{"case": "EMPTY_EXISTING_INDEX_COMMIT", "status": "pending_commit",
 "scope_complete": false, "index_overwritten": false, "index_error": "invalid_json"}
```

## 2. [P2] receipt 없는 restore의 index 조회 장애를 empty로 숨기지 않음 — 해결

- receipt가 없는 restore discovery에서 `_read_backup_index`가 `unreadable`이면
  `status=failed`, `error="remote backup index is unreadable (<kind>)"`,
  `index_read_error=<kind>`, `restored_count=0`을 반환한다. empty 분기로 흘리지 않는다.
- `backup restore` CLI는 `failed`/`locked`에서 exit 1(config_error 기존 동작 유지).
  복구 자동화가 장애를 정상 empty로 오인하지 않는다.
- 실패/부재/정상 모든 조기 반환에서 공유 recovery lock을 해제하고, 소유 client만
  close한다(injected client는 닫지 않음). 실패 직후 정상 재시도가 lock에 막히지 않고
  `empty`로 완료되는 것을 회귀로 확인했다.
- 정상 empty(404, 유효 `items=[]`)와 >8MiB 정상 index discovery/복원은 유지한다.
  403/인증/연결/한도/손상/빈 body는 각각 typed error로 구분한다.

REVIEW_09 재현 스크립트 재실행:

```text
{"case": "RESTORE_HTTP403", "result": {"status": "failed",
 "error": "remote backup index is unreadable (http_403)",
 "index_read_error": "http_403", "restored_count": 0, "restored_bytes": 0}}
```

## 3. 회귀 테스트 (신규 10건)

- 실제 facade/core stub: HTTP200 zero-byte → `invalid_json`, HTTP404 → `absent`,
  유효 `items=[]` → `present`, `max_bytes=64MiB` 전달.
- zero-byte index: no-pending completion 미확정(`null`), pending commit 차단·index/batch
  PUT 0·기존 body/pending 보존.
- 404 최초 index 생성(scoped complete=true).
- restore 실패 matrix(6): `http_403`, transport, `size_limit`, empty body, invalid JSON,
  client contract(int 반환) → `status=failed`, typed `index_read_error`, 대상 파일/
  receipt 쓰기 0, lock 해제 후 재시도 정상 empty.
- 소유 client `from_env` 경로: 실패 시 close, CLI restore exit 1; 404는 exit 0·empty.
- FIX_20의 64MiB bounded read, >8MiB completion/repair, remote-only merge 보존,
  FIX_18–19 pair/cache 경계 회귀 유지.

## 4. 실행 명령과 결과

```text
uv run --no-sync pytest apps/cardrag-worker/tests/ packages/cardrag-core/tests/ -q
1791 passed, 10 warnings in 59.76s   # FIX_20 시점 1781 + 신규 10건

uv run --no-sync ruff check packages/cardrag-core/ apps/cardrag-worker/
All checks passed!

uv run --no-sync ruff format --check packages/cardrag-core/ apps/cardrag-worker/
156 files already formatted

git diff --check
pass
```

## 5. 운영 경계 / 다음 단계

- 현재 운영 `/opt/cardrag/current`는 이전 FIX_19 release이고 timer/service inactive다.
  이번 변경은 아직 배포되지 않았다.
- 후속 Reviewer 통과 → 기존 승인 범위의 새 release → 실제 현대/BC scoped flush에서
  `backup_scope_complete=true`, `uploads=0`, 재시도 멱등, committed index/receipt 불변을
  정식 CLI로 확인한다. fixture/staging 결과로 운영 완료를 선언하지 않는다.
- 롯데/신한 batch 완주, timer 재개, 8사 인수는 이번 범위가 아니며 선언하지 않는다.

## 6. 남은 리스크

- restore discovery 실패는 이제 `failed`+nonzero이므로 운영 스크립트가 명시적으로
  처리해야 한다(정상 empty와 구분됨). 의도된 동작이다.
- HTTP200 zero-byte는 손상으로만 취급한다. 향후 원격이 실제로 zero-byte index를
  정상 상태로 쓰는 설계로 바뀌면 별도 계약 정의가 필요하다(현재는 아님).
- get-only fake는 `max_bytes`를 강제하지 않으므로 반환 후 길이 검사에 의존한다. 실제
  WebDAVClient는 전송 중 한도 초과 시 integrity 오류를 낸다.