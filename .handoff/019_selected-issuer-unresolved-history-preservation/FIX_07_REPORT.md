# 019 FIX_07 REPORT — 내용 바인딩·최종 진단·memo 실측 보완

작성: 2026-10-20 Executor (OpenCode). `FIX_07.md` 3개 항목을 구현·검증했다. 기존
PLAN/PLAN_AMENDMENT_01/REPORT/FIX_01–06·각 REPORT/REVIEW_01/LIVE_REVIEW_01/
OPERATIONS_REPORT/BACKUP_POLICY_RECHECK_01/`FIX_06_REPORT.md`는 보존했고 수정하지 않았다.
Git HEAD `aaeb2c4` 위 017–019 미커밋 worktree에 본 FIX 변경을 추가했다. 배포·원격 쓰기·
Worker 배치·timer 기동은 수행하지 않았다.

## 1. [P1] 같은 경로 pending/receipt ↔ 검증 번들 내용 바인딩

### 재현 이전 결함(FIX_07 §1.5)을 저장소 회귀로 이식

`apps/cardrag-worker/tests/test_fix_06_scoped_backup.py`:

- `test_conflicting_same_path_pending_blocks_transport_without_repair`
  (manifest/body/READY 3 case): Reviewer 재현과 동일 절차 — 등록 후 같은 remote_path의
  pending 행 local/sha/size만 위조 바이트로 변경, 재등록 없이 flush → **위조 바이트 미전송**,
  `in_scope_conflicts >= 1`, `backup_scope_complete=False`, 상태는 succeeded 아님, 행은 원장에
  보존(증거 삭제 없음).
- `test_same_path_conflicting_pending_is_repaired_then_verified_bundle_transported`:
  정식 등록 경로(`record_run_success`) 재실행 시 검증 근거로 ledger 행을 수복하고 canonical
  manifest가 실제 전송됨(`client.objects[path] == canonical_bytes`), conflicts 0,
  `backup_scope_complete=True`, receipt/index 세트 일치.
- `test_receipt_disagreeing_with_verified_artifact_blocks_remote_overwrite`: 검증된 receipt의
  sha가 번들과 다르면 **원격 overwrite 없이 차단**·보고(운영자 검토 경계). 바인딩 없는 seal
  참조(pdf CAS)는 정상 전송돼 기존 경로를 깨지 않는다.
- `test_local_bytes_corruption_beyond_ledger_record_blocks_transport`: 등록 후 파일 본문이
  바뀐 경우 scope 계산 자체가 번들을 배제(fail closed), 구 allowed로 강제해도 flush 내용
  바인딩이 전송 차단.
- `test_scoped_backup_excludes_nonselected_and_forged_pending`, idempotent retry, index
  commit 실패→재시도, remote_root(`''`) 바인딩, 비선택 미전송 회귀 유지.

### 구현

- `BackupLedger.scoped_backup_scope(run_id, state_dir, selected) -> (frozenset[paths],
  dict[remote_path, (sha256, size|None)])` 신설: 허용 근거를 경로 집합이 아니라 **경로·SHA·
  크기·계약 바인딩**으로 확장. seal 참조와 bundle 3종(body/manifest/READY)은 검증 값으로
  바인딩(`verified=True`), seal의 cache-kind 경로(reuse key만 있는 경우)는 로컬 증거가 없어
  legacy 무바인딩 유지 — 임의 추론으로 신뢰하지 않는다. `scope_remote_paths`는 동일 함수의
  경로-only 호환 래퍼(기존 call-site/테스트 불변).
- `flush(..., expected_artifacts=...)`: 허용 경로 행도 (a) 행 SHA/크기가 바인딩과 다르면
  전송 차단 conflict, (b) 같은 root에 receipt sha가 바인딩과 다르면 overwrite 차단 conflict,
  (c) 등록 후 flush 사이 local/spool 원문이 바인딩 SHA와 다르면 읽은 바이트 해시 재검증으로
  차단. conflict 행은 원장·receipt를 건드리지 않고 남는다. 완료 처리는 기존대로 검증+index
  commit 이후에만. 바인딩None(전역 flush·레거시 call)은 동작과 출력은 불변.
- `_register_verified_ocr_bundle`의 등록이 같은 경로 content-conflict를 감지해 logger 경고와
  함께 **검증된 행으로 수복**(INSERT 경로는 item_id 기준, 원격·receipt는 불변). 동일 content
  duplicate item은 중복 삽입하지 않는다.
- cli/partial call-site가 `scoped_backup_scope`의 (allowed, expected)를 모두 flush에 전달한다.

## 2. [P2] 최종 진단 전달·lost_source·분기 스키마 일관성

- `flush` 모든 반환 분기(locked/unchanged/deferred/config_error×2/final)에 동일 키 세트:
  `in_scope_remaining`, `in_scope_lost_source`, `in_scope_conflicts`,
  `pending_out_of_scope(_bytes)`, `backup_scope_complete`. 선택 범위 `lost_source`는
  `in_scope_remaining`에 산입되므로 소실 artifact가 0으로 완료를 가장하지 못한다
  (`test_lost_source_inside_scope_is_not_completion`).
- 의미 구분(OPERATIONS.md에 문서화): 선택 완료 = 전송·원격 GET 검증·receipt·**index commit**
  후 `in_scope_remaining=0 && conflicts==0`의 명시 결론(`backup_scope_complete=true`).
  전역 status/`remaining_pending`의 과거 의미는 불변이라 비선택 backlog가 남으면 선택 완료
  상태에서도 전역 status는 `degraded`다(CLI 회귀 테스트가 이를 단정). 미선택 flush는
  필드 전부 `null` — 0 값만으로 완료를 표시하지 않는다(`test_full_batch_...`에서 None 확인).
- 전달 경로: `backup.scoped_backup_result_fields(flush_res)` 신설. cli `run_command`의
  `backup_info`는 flush 여부와 무관하게 기본 None 키를 갖고 flush 후 실값을 병합;
  `partial_cli` process payload도 flush 이전 None 기본값 + flush 실값을 병합한다.
  hybrid/manual threshold 미달 `deferred`, 예산/전송 실패 구분은 flush의 status·error와 위
  필드로 최종 결과까지 전달된다. `backup_recorded`/exit0/cached snapshot으로 현재 상태를
  rewrite하지 않는다(문서화 유지).
- 실제 CLI 결과 경로 검증: `test_cli_backup_flush_run_scopes_to_verified_artifacts` —
  `CliRunner`로 `cardrag-worker backup flush --run <id>` 실행(frozen issuer-scope에서
  selected 복원, mock WebDAV), stdout JSON의 `scoped_issuers/in_scope_remaining/
  in_scope_conflicts/backup_scope_complete/pending_out_of_scope` 확인. 비선택 artifact는
  client에 도달하지 않는다. seal 없는 run은 config_error exit 2
  (`test_cli_backup_flush_run_without_seal_exits_config_error`).
- 신설 정식 재시도 명령: `backup flush --run RUN_ID [--issuer CODE ...]` — 선택 run의
  검증된 artifact만 provider 호출 없이 allowed+expected로 force flush. `--issuer` 생략 시
  run의 `issuer-scope.json` selected 사용. 전역 `backup flush`(백로그 소진용)과 구분된다.

## 3. [P2] bounded memo 실제 조회 경로 성능·메모리 검증

- `pipeline.lazy_cache_vector` 클로저를 모듈 함수 `make_lazy_cache_vector(state,
  performance, memo, profile, cache_key, input_sha256, expected_vector_sha256)`로 추출
  (동작·출력 불변, 운영 `_build_v5_generation`이 동일 함수 사용). 테스트가 **실제
  `WorkerState.get_embedding_v5` DB 경로**와 `WorkerPerformance` 카운터를 통과한다.
- fixture: 실 WorkerState + QwenEmbeddingProfileV5 + 서로 다른 300개 normalized 벡터
  (unique 300 × 반복 3 = 900 loads, memo cap 512). 결과:
  - memo 비활성: `export_cache_lookups`=900, 실제 state fetch=900
  - memo 활성(3회 반복 측정 평균): fetch=300, `export_cache_fetch_deduped`=600,
    반환 바이트 = 시드 row 바이트와 완전 일치(모든 load에서 assert)
  - eviction 경계: cap 4, 5키 교차 시 `[0,1,2,3,4,1]` → lookups 5 / deduped 1, FIFO oldest
    eviction 확인(`test_memo_eviction_and_hit_counts_via_real_state`)
  - 차단 유지(memo 활성 상태): expected vector sha 불일치 → "changed bytes", row의
    input_sha256 불일치 → "different profile or input", row 소실 → "disappeared before
    export" — 모두 첫 fetch에서 raise하고 memo에 아무것도 남기지 않는다
    (`test_memo_does_not_weaken_mismatch_or_loss_blocking`)
- 반복 wall time·peak RSS(임시 디스크 없음 — memo는 in-memory, DB size 불변):

  ```text
  MEMO-BENCH {"loads": 900, "unique_keys": 300, "fetches_disabled": 900,
              "fetches_enabled": 300, "baseline_seconds": [0.0281],
              "memo_seconds": [0.0147, 0.0146, 0.0154], "peak_rss_growth_kib": 4736}
  ```

  이 fixture(900 loads) 기준 loader wall time 약 2배 개선, load 중 fetch 3분의 1로 감소.
  RSS 성장은 cap 512×16KiB=8MiB 상한 안에서 관측 4.6MiB(SQLite 페이지 캐시 포함),
  assert 상한 24MiB. 전체 embedding을 메모리에 적재하지 않는다.
- **표현 한계(FIX_07 §3.3 준수)**: 위 수치는 bounded synthetic fixture 실측이다. 운영
  export 약 41분 전체에 대한 개선율이나 "이론상 최대 224k dedup"을 실측으로 주장하지 않는다.
  운영 지배 비용의 분해는 배포 후 다음 run의 `export_internal_seconds`·
  `export_cache_fetch_deduped` 계측으로 확정되며(019 run 기준 unique/export 조회
  395,174/619,134의 중복 상한이 upper bound 관찰치), 개선되지 않은 공통 비용(VACUUM,
  immutable 재검증, 행별 finite/L2 numeric 검증)은 그대로 남는다. serving 구조 전환은
  별도 설계 과제로 유지한다.

## 4. FIX_06_REPORT 정정(운영 인계 자료)

`FIX_06_REPORT.md`는 원문 보존. 정정:

- 실제 운영 구조는 host `/opt/cardrag/<release>` + named volume `cardrag-worker-state`
  (컨테이너 경로 `/var/lib/cardrag-worker`) + image `cardrag-worker:019-4dba44664659`다.
  이전 보고서의 `/opt/cardrag/state`, `/opt/cardrag/current/src` 예시는 부정확하다.
- 읽기 전용 확인은 승인이 필요하지 않다. 복붙 가능한 read-only 절차(컨테이너 내부, 네트워크
  차단, :ro 마운트):

  ```bash
  docker run --rm --network none \
    -v cardrag-worker-state:/var/lib/cardrag-worker:ro \
    --entrypoint python3 cardrag-worker:019-4dba44664659 -c '
  import sqlite3
  con = sqlite3.connect("file:/var/lib/cardrag-worker/backup-ledger.sqlite3?mode=ro", uri=True)
  for r in con.execute("SELECT item_id,item_type,size_bytes,remote_path,status FROM backup_pending"):
      print(r)
  '

  # 같은 컨테이너 패턴으로 bundle 대조(신코드 배포 후; 코드 수정 없이 검증만):
  docker run --rm --network none \
    -v cardrag-worker-state:/var/lib/cardrag-worker:ro \
    --entrypoint python3 cardrag-worker:<수정-image> -c '
  import json
  from pathlib import Path
  from cardrag_worker.backup import BackupLedger
  state = Path("/var/lib/cardrag-worker")
  run = "41ce678e665b47179f8bfb2d82273882"
  sealed = json.loads((state/f"runs/{run}/sealed/publish.json").read_text())
  doc = next(d for d in sealed["manifest"]["documents"] if d["issuer"] == "woori")
  allowed, expected = BackupLedger.scoped_backup_scope(run, state, ("woori",))
  con = sqlite3.connect("file:/var/lib/cardrag-worker/backup-ledger.sqlite3?mode=ro", uri=True)
  for r in con.execute("SELECT item_id,sha256,size_bytes,remote_path FROM backup_pending"):
      ok = r[3] in allowed and expected.get(r[3], (None,))[0] == r[1]
      print(r[0], r[3], "bound-ok" if ok else "out-of-scope-or-conflict")
  '
  ```

- 2건(metadata pair)의 기본 복구 절차는 전역 `backup flush --force`가 아니다. 수정 image
  배포 후 **선택 범위 정식 재시도**:

  ```bash
  # worker 컨테이너의 정상 실행 환경(state 볼륨 RW, WebDAV creds, lockfree 단 flush는
  # backup.lock으로 배타)에서:
  docker exec <worker-container> cardrag-worker backup flush --run 41ce678e665b47179f8bfb2d82273882 --issuer woori
  # 또는 host operations helper가 제공하는 동일 CLI 경유
  ```

  이 경로는 provider/OCR/수집을 호출하지 않고, 해당 run의 검증된 선택 artifact만
  allowed+expected로 전송·GET 검증·receipt·index commit 후 pending에서 제거하며, receipt
  충돌은 차단한다. 제한: run 산출물과 spool이 retention으로 정리됐으면 그 행은
  `lost_source`로 남고 in-scope 미완료로 보고된다(완료를 가장하지 않음) — 그때는 full-batch
  run의 cache scan 또는 재수집이 정식 회수 경로다. 원장 수작업 삭제·허위 receipt 삽입·
  원격 overwrite 금지 조항은 불변.
- 배포 전 상태에서는 위 bundle 대조가 구 image에 `scoped_backup_scope`가 없어 실행 불가하다
  (현 image는 FIX_06 스코프까지). read-only 확인의 나머지(원장 pending 2건)는 현 image로도
  가능하다.

## 5. 변경 파일·명령 결과

- `backup.py`(scoped_backup_scope/등록 수복/flush 바인딩/진단 스키마/result fields)
- `cli.py`(inline flush가 expected 전달+필드 병합, 기본 None 키, `backup flush --run|--issuer`)
- `partial_cli.py`(payload None 기본값+flush 필드 병합)
- `pipeline.py`(`make_lazy_cache_vector` 추출, call-site 위임)
- `docs/OPERATIONS.md`(semantics/명령/필드, 오타·`냉결`→`고정` 포함 이전 문서 정합화)
- `tests/test_fix_06_scoped_backup.py`(FIX_07 회귀 14건 추가: 총 32건)

```text
$ uv run --no-sync pytest apps/cardrag-worker/tests/ -q
1422 passed, 10 warnings in 53.77s   # FIX_06 종료 시 1408 + 신규 14
$ uv run --no-sync pytest apps/cardrag-worker/tests/test_fix_06_scoped_backup.py -q
32 passed in ~2s
$ uv run --no-sync ruff check apps/cardrag-worker/
All checks passed!
$ uv run --no-sync ruff format --check apps/cardrag-worker/
126 files already formatted
$ git diff --check
(통과)
```

## 6. 편차·남은 한계·운영 미검증

1. 편차 없음. receipt·원격 내용 충돌은 FIX §1.3의 두 허용 처리 중 "안전 중단"을 선택했다
   (ledger 행만 검증 근거로 수복). global status 문자열은 하위 호환을 위해 유지하고 완료
   결론은 `backup_scope_complete` 명시 필드로만 노출 — 0 값 관대함을 제거한다.
2. 운영 미검증(배포 후): 실제 pending 2건의 `backup flush --run` 회수, 다음 선택 run의
   payload 진단 필드 실측, `export_internal_seconds` 41분 분해, host/container UID/GID
   다른 상태 metadata read, run-issuer 등 helper 동작.
3. benchmark는 900-load synthetic fixture다. 운영 규모(619k)에서 dedup 절감량은 run의
   `export_cache_lookups`·`export_cache_fetch_deduped`·`export_internal_seconds` 관측으로만 확정한다.
4. 코드 변경은 미커밋 worktree 유지(017–019 공존 보존). commit/release/배포/원격 쓰기 없음.
