# 019 FIX_08 REPORT — 바인딩 우회 차단·receipt/index 완결성·보류 시 읽기 전용 진단

작성: 2026-10-20 Executor (OpenCode). `FIX_08.md` 3개 항목을 구현·검증했다. 기존 문서
(PLAN·PLAN_AMENDMENT_01·REPORT·FIX_01–07/각 REPORT·REVIEW/LIVE/OPERATIONS/RECHECK 자료)는
수정하지 않았다. Git HEAD `aaeb2c4` 위 017–019 미커밋 worktree를 보존하고 그 위에 변경을
추가했다. 배포·원격 쓰기·운영 원장 수정·Worker 배치/timer 기동은 수행하지 않았다.

## 1. [P1] reuse-key 무바인딩 fallback 차단

### 구현 (`backup.py`)

- `scoped_backup_scope`가 인스턴스 메서드가 되며 허용 경로와 expected 바인딩을 함께 구성한다:
  1. run-local `native-manifest.json` 번들 검증(FIX_06/07 경로) — bound.
  2. **두 번째 정식 근거: 워커 로컬 OCR cache** `state/cache/ocr/{reuse_key}/manifest.json`
     (+`READY.json`). 신규 모델 재검증(canonical 바이트, `reuse_key == manifest.reuse_key`,
     model contract binding)과 seal 교차 대조(pdf sha/size/page, ocr sha)를 통과해야만
     manifest/READY 경로가 bound된다. 짝 READY 파일이 유도값과 다르면 근거를 무효화(bound
     하지 않음). 임의 변환 없음.
  3. 그 외(근거 소실/손상/content-kind 등)는 경로를 허용 집합에 **가시적으로 남기되
     unbound**로 둔다. `flush`는 scoped 모드(expected 전달 시)에서 unbound 행을 전송하지
     않고 `in_scope_unbound`로 보류하며, 보류가 남아 있으면 `backup_scope_complete`는
     성립하지 않는다. 경로 문자열/pending/receipt 단독 신뢰 없음.
- 문서 유지 자료로 남은 행의 조용한 범위 제외을 막기 위해, 선택 문서의 `doc_map` 태깅
  pending은 evidence 소실 시에도 허용 집합에 남아 unbound 보류·진단 대상으로 표시된다.
  doc_map은 가시성 근거일 뿐 전송 권위가 아니다(회귀로 고정).
- legacy 경로(allowed만 전달·expected 없음)의 전송 동작은 불변: 미선택 flush와 구 call-site
  하위 호환.
- Reviewer 재현 A가 저장소 회귀로 이동:
  - `test_seal_reuse_key_without_any_evidence_held_not_transported` — run manifest 삭제 후
    `path in allowed`, `path not in expected`, 위조/잔존 바이트 미전송, unbound ≥1,
    complete False, 상태는 succeeded가 아님, CAS 아티팩트는 정상 전송.
  - `test_seal_reuse_key_rebinds_from_local_cache_and_spool` — 로컬 cache 근거로 재바인딩,
    run 파일 삭제 상태에서도 spool 원문으로 canonical manifest 전송, complete True.
  - 기존 forged/doc_map 회귀 업데이트: 선택 문서 태깅 행은 allowed에 보이며 unbound로
    보류·완료 차단(`transported ⊆ expected`).

## 2. [P1] receipt·index·필수 artifact 완결성(pending 유무와 무관)

### 구현

- `_scoped_integrity(expected, canonical_root)` 신설: expected 전체를 대상으로 현 root의
  receipt SHA/size 정합성(일치 = 완료 근거, 불일치 = 충돌)과 행 유무를 대조하고
  (충돌 경로, 완료 쌍, 누락 경로)를 반환. pending 루프와 독립적으로 매 진단 분기에서
  실행되므로 pending 없는 receipt 충돌·누락도 탐지된다. 다른 root의 receipt는 현 root
  완료 근거로 인정하지 않는다.
- `flush` 완료 판정(`backup_scope_complete`)을 결과 선언으로 강화: `in_scope_remaining=0`
  & `in_scope_conflicts=0` & `in_scope_unbound=0` & `in_scope_missing=0` &
  **모든 바인드 완료 경로가 커밋된 원격 `v1/backup/index.json`에 동일 digest로 존재**.
  이번 batch가 다른 항목을 commit한 사실만으로는 완료하지 않는다. 원격 index를 읽을 수
  없으면 `None`(미확인)이고 true가 아니다.
- index 구성 시 expected와 충돌하는 receipt/원격 항목을 새 index 문서에서 제거한다(잘못된
  receipt SHA가 committed index로 퍼지지 않게). 충돌 경로의 원격 객체는 덮어쓰지 않는다.
- 상태 정합화: 글로벌 `succeeded`인데 scope complete가 False면 상태를 `degraded`로 강등
  (선택 미완료를 성공으로 보고하지 않음). `unchanged` 재실행도 receipt+index 근거로
  complete True/False를 일관 산출한다.
- 등록 시 기존 receipt 존재만 보고 생략하던 경로는 receipt 내용과 expected를 대조하는
  flush·integrity 관문이 뒤에서 차단하므로 등록은 원격 receipt를 절대 변경하지 않는다.
- Reviewer 재현 B/C 저장소 회귀:
  - `test_receipt_conflict_detected_without_any_pending_row` — pending 삭제 + 충돌 receipt
    (`'7'*64`/42): conflicts ≥1(pending 없이 탐지), complete False, `man_rel` 미전송, 커밋
    index에서 해당 항목 제거 확인.
  - `test_missing_required_artifact_blocks_completion_until_reregistration` — pending/
    receipt 모두 삭제: 나머지가 commit돼도 `in_scope_missing=1`·complete False; 정식
    재등록(record_run_success) 후 재시도에서 missing 0·complete True·canonical 전송.
  - `test_receipt_at_other_root_is_not_completion_evidence` — 타 root 정상 receipt만 있으면
    현 root missing 1·complete False.
  - `test_unchanged_rerun_reports_completion_from_receipts_and_index` — 기완료 receipt +
    동일 원격 index 상태에서 재실행 `unchanged`, complete True.
  - index commit 실패/재시도·비선택 미전송·remote root 바인딩 기존 회귀 유지.
- scope 증거 소실(doc_map 태깅 행)·cache 짝 불일치·local bytes 변조 차단 회귀는 §1과
  기존 `test_local_bytes_corruption_beyond_ledger_record_blocks_transport`(updated)/
  `test_conflicting_same_path_pending_blocks_transport_without_repair`가 겸한다.
- "정상 legacy/native/content 재사용" 영향 없음: content-kind는 백업 flush가 아니라
  pipeline cache publication이 담당하는 기존 경로 불변, native 재사용은 cache 근거
  rebind 또는 기완료 receipt reuse로 정상 완료.

## 3. [P2] 임계값 미달 실행의 읽기 전용 선택 진단

- `BackupLedger.scoped_pending_diagnostics(settings, run_id, selected)` 신설 — 전송·원격·
  provider 호출 없이 scope+integrity 로컬 계산만 수행. 키: `backup_in_scope_remaining/
  lost/conflicts/missing/unbound`, `backup_pending_out_of_scope(_bytes)`,
  `backup_scope_complete: null`(커밋 근거 없음 = 미확인, 실패·완료 아님),
  `backup_diagnostics_mode: "read_only"`, `backup_scope_run_id`.
- `cli.py` run_command: `deferred` 분기에서 `backup_defer_reasons`(trigger_reasons)와
  함께 위 필드를 선택 실행 시 병합. 기본 백업_info에도 전 키 None 초기화(진단 불가의 None
  의미 명시: disabled·무스코프·미실행). `partial_cli.py` process payload 동일 처리
  (deferred 시 read_only 스코프 병합). flush 실행 분기는 기존대로 flush 결과가 병합된다.
- 새 읽기 전용 명령: `cardrag-worker backup status --run <RUN_ID> [--issuer <code>]`
  (전역 status에 같은 read_only 진단 병합). 회귀:
  `test_scoped_pending_diagnostics_visible_when_threshold_defers`(hybrid 미달 + 비선택
  backlog 3건 상태에서 잔여/미전송 카운트 노출·complete null·무변경 확인)과
  `test_cli_backup_status_run_read_only`(실제 CLI 결과 경로, frozen issuer-scope 복원).
- main run/collect→process의 deferred 병합 코드는 실제 CLI e2e 전체 재현 대신 위 공용
  메서드+status CLI로 검증하고, 운영 선택 run 관측은 배포 후 미검증 항목으로 남긴다(§6).
- 정책상 보류를 오류로 표시하지 않는다: status 문자열은 `deferred` 유지, 카운트는 보류
  이유와 함께 전달, force flush·threshold 우회 없음.

## 4. 운영 인계 자료 정정 (FIX_07_REPORT 후속)

- Fix_07 보고서 두 번째 Python 예시는 `import sqlite3` 없이 `sqlite3`를 사용해 그대로
  실행되지 않는다. 아래가 배포 후 사용하는 완성형 읽기 전용 확인이다(현재 운영 image는
  `cardrag-worker:019-4dba44664659`이며 **FIX_06/07/08을 포함하지 않는다** — 스코프/바인딩
  코드를 쓰려면 수정 image 배포가 선행돼야 한다. 원장 read 자체는 현 image에서도 가능):

  ```bash
  # 현 image 포함, 읽기 전용(네트워크 차단, :ro, 승인 불필요):
  docker run --rm --network none -v cardrag-worker-state:/var/lib/cardrag-worker:ro \
    --entrypoint python3 cardrag-worker:019-4dba44664659 -c '
  import sqlite3
  con = sqlite3.connect("file:/var/lib/cardrag-worker/backup-ledger.sqlite3?mode=ro", uri=True)
  for r in con.execute("SELECT item_id,item_type,size_bytes,remote_path,status FROM backup_pending"):
      print(r)
  '

  # 수정 image 배포 후, 바인딩/보류 관점의 선택 스코프 read-only 진단:
  docker run --rm --network none -v cardrag-worker-state:/var/lib/cardrag-worker:ro \
    --entrypoint python3 <수정-image> -c '
  import sqlite3
  from pathlib import Path
  from cardrag_worker.backup import BackupLedger
  from types import SimpleNamespace
  state = Path("/var/lib/cardrag-worker")
  settings = SimpleNamespace(state_dir=state, webdav_base_url="https://<backup-root>")
  ledger = BackupLedger(state / "backup-ledger.sqlite3")  # read-only use only
  print(ledger.scoped_pending_diagnostics(
      settings, "41ce678e665b47179f8bfb2d82273882", ("woori",)))
  '
  ```

- 배치가 종료돼 exited 상태인 현 운영 구조에서는 `docker exec`로 flush를 재시도할 수 없다.
  선택 재시도는 release의 compose 유틸로 1회성 컨테이너를 실행한다(비공개 overlay·secret
  경로는 호스트 tools가 계승하며 출력하지 않는다; helper 인자 형태는 host에서
  `worker-compose.sh` usage로 사전 확인):

  ```bash
  /opt/cardrag/current/operations/worker-compose.sh run --rm worker \
    cardrag-worker backup flush --run 41ce678e665b47179f8bfb2d82273882 --issuer woori
  ```

  이 경로는 provider·OCR·수집을 호출하지 않고, 해당 run의 검증된 선택 artifact만
  allowed+expected로 재시도한다. receipt 충돌은 원격 overwrite 없이 차단·보고된다.
- 제한(변경 없음): run 산출물/spool이 retention으로 정리되면 그 경로는 `lost_source`/
  unbound로 미완료 표시되며 full-batch 회수 또는 재수집이 정식 경로다. 원장 수작업 삭제·
  허위 receipt·무바인딩 전송은 계속 금지.

## 5. 변경 파일·검증 결과

- `backup.py`(cache 근거 rebind·scoped_backup_scope 인스턴스화·unbound 보류·
  `_scoped_integrity`/`_scoped_completion`/`_remote_index_entries`·index 필터·
  `scoped_pending_diagnostics`·결과 필드 확장)
- `cli.py`(deferred 시 read_only 스코프 병합·필드 기본값·`backup status --run`)
- `partial_cli.py`(deferred 병합·기본 필드)
- `tests/test_fix_06_scoped_backup.py`(FIX_08 재현 8건 추가 + 3건 semantics 갱신: 총 40)
- `docs/OPERATIONS.md`(unbound/missing/receipt·index 완결·보류 진단·status --run)

```text
$ uv run --no-sync pytest apps/cardrag-worker/tests/ -q
1430 passed, 10 warnings in 54.46s      # FIX_07 종료 시 1422 + 신규 8
$ uv run --no-sync pytest apps/cardrag-worker/tests/test_fix_06_scoped_backup.py -q
40 passed in 2.02s
$ uv run --no-sync ruff check apps/cardrag-worker/
All checks passed!
$ uv run --no-sync ruff format --check apps/cardrag-worker/
126 files already formatted
$ git diff --check
(통과)
```

## 6. 편차·남은 한계·운영 미검증

1. 편차 없음. Reviewer 재현 A/B/C 절차대로 저장소 회귀를 작성·통과시켰다.
2. `scoped_backup_scope`가 DB(doc_map/pending 존재)를 참조하는 인스턴스 메서드가 됐다.
   read-only 동작이고, 전송 권위는 계속 expected 바인딩에서만 발생한다(doc_map은 표시용).
3. main run/collect→process의 deferred 병합은 공용 메서드·`backup status --run` CLI·flush
   분기 테스트로 검증했고, 임계값 미달을 지나는 실운영 배치 e2e는 배포 후 관측 항목이다.
4. 원격 index 검증은 클라이언트가 index를 읽을 수 있어야 complete=true; 읽기 실패는
   `null`(미확인)이다. 기존 WebDAVClient는 get_bytes를 제공한다.
5. 배포·원격 flush·운영 원장 확인·8사 라이브 검증 흐름(018/019 사용자 수동 절차)은 본
   세션에서 수행하지 않았다. 코드 변경은 미커밋 worktree로 Reviewer에게 넘긴다.
