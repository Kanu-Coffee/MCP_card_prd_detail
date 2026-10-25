# 019 FIX_20_REPORT — 커진 backup index의 bounded read와 실패 시 보존

작성: 2026-10-21, Executor OpenCode. `FIX_20.md`의 index 읽기/merge 보완을 구현했다.
기존 handoff 문서는 보존했고 이 파일만 신규 산출물이다. 기준: HEAD `aaeb2c4` +
019 미커밋 작업 트리(FIX_01–19 변경 보존). 코드 변경:
`apps/cardrag-worker/src/cardrag_worker/backup.py`,
`apps/cardrag-worker/tests/test_fix_06_scoped_backup.py`, `docs/OPERATIONS.md`.
새 운영 배포·원격 쓰기·cache 복원·scoped flush·배치/timer 기동·원장/원격 index 수정은
이번 턴에서 수행하지 않았다. 019와 timer 비활성 상태를 유지한다.

## 1. 실제 운영 재현과 원인

REVIEW_08/OPERATIONS_REPORT_05의 실제 증거를 그대로 전제로 삼았다.

- `v1/backup/index.json` 실제 크기 **8,632,661 bytes / 16,888 items**
  (SHA256 `bff1e91f61571ed95cc194e5a3b5b0f993905ab6d1492bfb0aad5610a5056d64`).
- 공통 control object 기본 한도 `CONTROL_OBJECT_MAX_BYTES = 8,388,608` 때문에
  기본 `get_bytes`는 `WebDAVIntegrityError`다.
- 그 결과 정식 scoped flush가 `status=unchanged`,
  `backup_scope_complete=null`로 남았다. 64MiB 한도 독립 읽기에서는 현대
  required=bound=1894, BC=482, index mismatch=0으로 complete 조건이 성립했다.
- 이번 변경은 완료 필드를 수동 true로 바꾸지 않고, 같은 원격 증거를 정식 CLI가
  읽을 수 있게 만드는 것이다. **원격 항목 유실이 발생했다고 주장하지 않는다**
  (운영 flush 원격 쓰기 0, receipt fingerprint 전후 동일).

## 2. 변경 내용

### 2.1 backup index 전용 bounded read

- `BACKUP_INDEX_PATH`와 전용 상한 `BACKUP_INDEX_MAX_BYTES = 64 MiB`를 추가했다.
  공통 control object 8MiB 기본값은 변경하지 않았고 무제한 읽기도 없다.
- `_read_backup_index()`가 다음을 구분한다:
  `present` / `absent`(404, None, 빈 bytes) / `unreadable` with
  `error_kind` ∈ `size_limit`, `http_<code>`, `transport`, `invalid_json`,
  `client_contract`.
- 실제 `WebDAVClient.get_bytes`에는 `max_bytes=BACKUP_INDEX_MAX_BYTES`를 명시 전달한다.
  `max_bytes`를 받지 않는 get-only fake 계약은 유지하며(서명 검사), 이 경우에도
  반환 바이트 길이를 전용 상한과 비교한다.
- 진단: flush 결과에 `index_read_error`, scoped run payload에는
  `backup_index_read_error`(값이 있을 때만)를 추가했다. 읽을 수 없는 index는
  `backup_scope_complete=null`로 남고 true가 되지 않는다.

### 2.2 index 읽기 경로 전수 보완

- `_remote_index_entries`: 전용 reader 사용. absent/unreadable → None(완료 근거 없음).
- `_remote_index_repair_needed`: **읽힌 경우에만** receipt drift를 판정한다. 부재/오류는
  transport-less repair를 실행하지 않는다(기존 계약 유지).
- `_publish_index_and_pointer` (commit merge): 기존 index 읽기 실패를 suppress하고 빈
  index로 간주하던 경로를 제거했다. 이제 unreadable(size/auth/transport/parse)이면
  **batch manifest/index 게시를 중단**하고 `last_error`에 사유를 남긴다. 업로드된
  객체의 pending 행은 삭제하지 않아 재시도 시 다시 merge한다. 읽힌 경우 remote-only
  항목/다른 세대 항목을 그대로 보존하고 local receipt 항목을 merge한다. 실제 부재(404)만
  새 index 생성으로 진행한다.
- receipt 없는 restore discovery: 같은 reader를 사용하며, unreadable이면 discovery를
  수행하지 않고(빈 index로 오인하지 않음) 경고만 남긴다.

## 3. 테스트 (신규 6건)

- **>8MiB 정상 index**: 8.5MB index(8MiB 초과)에서 pending 0 scoped flush가
  `backup_scope_complete=true`, `index_read_error=None`, `get_bytes(max_bytes=64MiB)`
  전달을 확인.
- **전용 상한 초과**: 실제 client의 integrity 오류 / get-only client가 반환한
  `64MiB+1` 바이트 / monkeypatched 소한도 valid 초과 — 모두 `size_limit`,
  완료 `null`, 원격 쓰기 0.
- **unreadable commit 보존**: invalid JSON과 transport 오류 각각에서 업로드 후에도
  index 원본 바이트 불변, `v1/backup/index.json`/batch PUT 0, pending 행 유지,
  `status=pending_commit`, `error`에 "refusing to overwrite" 포함.
- **remote-only merge**: 기존 index의 다른 세대 항목이 재게시 후에도 유지되고
  현재 required 항목이 추가됨.
- **no-pending repair**: 8.5MB index에서 누락 receipt 항목이 repair로 복구되고
  remote-only 항목이 유지됨(전용 한도 사용 확인).
- **restore discovery**: local receipt 0 + 8.5MB index에서 객체 1건을 discovery/restore.
- 기존 get-only/fake client(`get_bytes(path)` 서명) 계약과 FIX_18/19 회귀,
  stale CAS scope/ownership/lost_source/receipt-only 보호를 유지한다.

## 4. 실행 명령과 결과

```text
uv run --no-sync pytest apps/cardrag-worker/tests/ packages/cardrag-core/tests/ -q
1781 passed, 10 warnings in 58.05s   # FIX_19 시점 1775 + 신규 6건

uv run --no-sync ruff check packages/cardrag-core/ apps/cardrag-worker/
All checks passed!

uv run --no-sync ruff format --check packages/cardrag-core/ apps/cardrag-worker/
156 files already formatted

git diff --check
pass
```

## 5. 운영 baseline / 다음 단계

- 최신 실제 serving은 **5545건, 현대 generation**이며 라이브 MCP OCR 2건 호출도
  정상이라는 REVIEW_08/FIX_20 기록을 그대로 인용한다. 이번 턴에서 새 배치/원격
  접근은 하지 않았다.
- Reviewer 통과 후 기존 승인 범위에서 새 release를 배포하고, 현대/BC의
  `backup_scope_complete=true`, `uploads=0`, 재시도 멱등, 원격 index/receipt 불변을
  실제 CLI로 확인한다. 그 전에는 staging/fixture 결과로 운영 완료를 선언하지 않는다.
- 신한/롯데 batch 완주·timer 재개·8사 인수는 이번 범위가 아니며 선언하지 않는다.

## 6. 남은 리스크

- 64MiB는 현재 16,888 items(8.6MB)의 약 7배 여유다. 향후 index가 이 한도를 넘으면
  완료는 `null` + `index_read_error=size_limit`로 남고 commit은 기존 index를 덮어쓰지
  않는다. 상한 상향은 데이터 성장 증거와 함께 별도 판단이 필요하다.
- get-only fake는 max_bytes를 강제하지 않으므로 반환 후 길이 검사에 의존한다
  (실제 WebDAVClient는 전송 중 한도 초과 시 integrity 오류).
- commit 중단 시 이미 업로드된 객체는 receipt로 남고 pending 행도 유지된다. 재시도는
  전송 없이 merge만 수행한다(기존 idempotent 경로).