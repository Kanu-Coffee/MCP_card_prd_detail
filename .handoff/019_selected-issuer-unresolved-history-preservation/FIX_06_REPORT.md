# 019 FIX_06 REPORT — 선택 백업 완결·공통 처리 비용·scope 진단 구현

작성: 2026-10-20 Executor (OpenCode). `FIX_06.md`의 3개 항목을 구현했다. `PLAN.md`,
`PLAN_AMENDMENT_01.md`, REPORT, FIX_01–05/각 REPORT 및 기존 문서는 수정하지 않았다.
Git HEAD `aaeb2c4` 위 미커밋 017–019 worktree 위에 변경을 얹었으며 기존 변경을 덮어쓰지
않았다. 배포·원격 백업 쓰기·Worker 실행·timer 활성화는 본 문서 작성 시점까지 수행하지
않았다(FIX_06 경계 준수).

## 0. 착수 시 재확인

- `git status`: 착수 시와 동일하게 017–019 변경(수정 28 + 신규 8 파일)이 미커밋으로 공존.
  운영 image 고정 digest `4dba44664659…`와의 대비는 Reviewer가 FIX 발행 시 확인한 대로
  유지 전제로 착수했다. 본 FIX 작업으로 worktree에 추가된 파일만 아래 "변경 파일" 목록이다.
- `verified_document_ocr_bundles`/`scope_remote_paths` 동작 재현의 기준 fixture는 Reviewer의
  readonly 재확인 값(`selected=[woori]`, ocr_manifest 1791B, ocr_ready 285B,
  `sealed ocr_reuse_key=None`)과 같은 형태다.

## 1. [P1] 선택 OCR metadata scoped backup 허용 경로 누락 수정

### 구현 (`apps/cardrag-worker/src/cardrag_worker/backup.py`)

1. **공용 근거 함수 `verified_document_ocr_bundles(state_dir, run_id, doc)` 신설.**
   seal 문서 dict 1건과 run-local `documents/{document_id}/ocr/**/native-manifest.json`을
   대조해 검증된 (body CAS, manifest, READY) 번들을 반환한다. 검증 항목:
   - manifest 원문이 `OCRArtifactManifest.canonical_bytes()`와 정확히 일치(손상/비정규 차단)
   - `source.pdf_sha256 == seal pdf.sha256`, size·page_count 정합(seal에 값이 있을 때)
   - `output.sha256 == seal ocr.sha256`, size 정합
   - 저장된 `ocr.md` 본문 바이트의 SHA-256이 `output.sha256`과 일치(본문 해시 불일치 차단)
   - READY는 해당 manifest로부터 결정적으로 유도(`reuse_key`/`manifest_sha256`/`ocr_sha256`
     짝). 파일명·prefix·doc_map 문자열 단독 신뢰 없음. 모델 검증이 `reuse_key =
     native_ocr_reuse_key(contract, source)` 바인딩을 내장하므로 계약(kind) 임의 변환 없음.
   - 실패는 항상 빈 결과(fail closed). 동일한 reuse_key 중복 manifest는 1회만 반환.
2. **`scope_remote_paths`**: 위 번들의 3경로를 선택 허용 집합에 추가. seal optional
   `ocr_reuse_key`가 null인 generation-only 결과도 이제 body/manifest/READY가 허용된다.
   reuse_key가 있는 경우에도 `ocr_cache_kind`를 따라가도록 정정(content면 content 경로;
   기존 코드처럼 무조건 native로 변환하지 않음). cache kind 누락 시에만 legacy native 사용.
3. **`record_run_success` 선택 브랜치**: 기존 loose doc 디렉터리 스캔 대신 같은
   `verified_document_ocr_bundles` 근거로 등록(recorder와 scope planner가 동일한 증거 원천).
   전체 배치(selected_issuers=None)는 기존 loose 스캔 경로 불변.
4. **`_register_verified_ocr_bundle`**: 바디·manifest·READY를 하나의 단위로 pending+spool에
   등록하고 3경로 전부에 `doc_map`·`doc_info` metadata를 쓴다. `record_document_ocr`(실행 중
   intent recorder)도 READY에 `doc_info`를 쓰도록 맞춰 인덱스 항목의 세트 완결성을 통일했다.
   기존 pending 행은 INSERT OR REPLACE/remote_path dedup으로 중복 없이 회수된다.
5. **`flush` 진단 구분**: `allowed_remote_paths` 사용 시 결과에
   `in_scope_remaining`(선택 run 고유의 남은 pending), `pending_out_of_scope`/
   `pending_out_of_scope_bytes`(범위 밖 전역 pending)을 추가. 완료 처리·카운트는 기존대로
   전송/검증 + index commit 이후에만 일어나며(receipts/index commit 실패 시 pending 보존),
   재시도는 receipt 재사용으로 멱등. 상태 문자열(succeeded/degraded/pending_commit 등)과
   전역 `remaining_pending` 의미는 불변이라 기존 판독자가 깨지지 않는다.

### 인수 기준 대응

- 라이브 fixture(신규 OCR 성공, seal reuse_key/kind null, run-local manifest 존재)에서
  선택 허용 경로에 body/manifest/READY 포함, mock flush 후 receipt·index·doc_info 세트
  일치, 해당 pending 0 — `test_live_like_generation_only_bundle_is_flushed_and_idempotent`
- 비선택 issuer pending·임의 doc_map 충돌 pending·연결 없는 READY·hash/PDF/body/
  noncanonical/page 불일치 모두 미전송/거부 — `test_scoped_backup_excludes_nonselected_and_forged_pending`,
  `test_bundle_linkage_fails_closed_on_inconsistent_evidence`(5 case)
- 기존 pending의 정식 재시도 회수, index commit 실패 시 pending 보존 + 재시도 시 본문
  재전송 없는 멱등 완료 — `test_pending_bundles_recovered_by_later_scoped_retry`,
  `test_scoped_flush_index_commit_failure_keeps_pending_and_retries_idempotently`
- 정상 키 존재/native/content 계약 경로 — `test_seal_cache_kind_paths_follow_the_sealed_contract`
- 전체 배치 등록 semantics 불변 — `test_full_batch_registration_semantics_unchanged`
- 운영 소스에 카드사명/이번 key/path 하드코딩 없음(신규 코드에 issuer 문자열 비교 없음;
  `partial_cli.py`/`cli.py`의 기존 DEFAULT_ENABLED_ISSUERS 판정 그대로 사용).
- 일반 선택/round-robin/전체 배치/collect→process는 모두 같은 `scope_remote_paths`를
  호출하므로(cli.py:1702, partial_cli.py:327) 동일한 backup semantics를 쓴다. 백업 보완으로
  비선택 adapter/OCR/provider를 호출하지 않는다(백업 코드는 run 산출물 파일과 원장만 읽는다).

### 이번 실제 pending 2건에 대한 읽기 전용 대상 확인 절차 (미수행, 운영 허가 필요)

root 권한으로만 실행; 모든 읽기는 SQLite `mode=ro`이고 쓰기는 없다.

```bash
# 1) 현재 ledger 전역 상태(read-only)
sudo sqlite3 "file:/opt/cardrag/state/backup-ledger.sqlite3?mode=ro" \
  "SELECT item_id,item_type,size_bytes,remote_path,status FROM backup_pending;"
# 기댓값: ocr_manifest(1791B)+ocr_ready(285B) 2건이 'pending', remote_path는
# v1/ocr-cache/native/{reuse[:2]}/{reuse}/manifest.json|READY.json

# 2) 우리 run의 seal과 run-local manifest 대조(read-only)
sudo python3 - <<'PY'
import sys; sys.path.insert(0, "/opt/cardrag/current/src")  # release의 venv/site-packages 경로는 호스트 실제 설치로 조정
from pathlib import Path
from cardrag_worker.backup import verified_document_ocr_bundles
import json
state = Path("/opt/cardrag/state"); run = "41ce678e665b47179f8bfb2d82273882"
sealed = json.loads((state/"runs"/run/"sealed/publish.json").read_text())
for doc in sealed["manifest"]["documents"]:
    if doc["issuer"] != "woori": continue
    print(doc["document_id"], verified_document_ocr_bundles(state, run, doc))
PY
# 기댓값: 해당 문서 1건에서 paths 3조가 반환되고, pending 2건의 remote_path가 그 paths에
# 포함돼야 한다. spool본도 read-only 확인: /opt/cardrag/state/backup/spool/{sha} 존재.
```

### 복구 절차 (실행 허가 시에만; 코드 배포 후)

수정 이미지 배포가 선행돼야 scope가 새 로직으로 계산된다. 두 정식 경로:

- A(권장, 정식 재시도): 다음 woori 선택 실행 또는 `cardrag-worker backup flush --force`
  (전역 flush) 실행. 2건은 spool/local 원본으로 전송→GET 검증→receipt→index commit 후에만
  pending에서 사라진다. 이미 전송된 body/PDF는 receipt 재사용으로 재전송되지 않는다.
- B(선택 재시도 범위): 재실행 없이 선택 범위 전송만 원하면 해당 run이 아직 retained 상태이므로
  새 release의 scope 계산으로 flush allowed를 재구성하는 경로는 현재 CLI에 없다 — 기존
  `backup flush`가 전역 pending을 처리하므로 별도 코드 경로 추가 없이 A로 충분하다.
  임의 SQL 삭제/receipt 삽입/remote overwrite는 금지(FIX_06 §1.4).

배포·flush 모두 본 세션에서는 수행하지 않았다.

## 2. [P2] export·cache 조회 계측과 최소 성능 개선

### 계측 (`exporter_v5.py`)

`export()`에 선택 파라미터 `profile: dict[str, float] | None` 신설. None(기본)이면 아무
오버헤드 없이 기존 코드와 바이트 단위 동일 동작. 전달 시 아래 구간을 누적 work seconds로 분리
기록하며, `pipeline.py`가 run별로 `export_internal_seconds` metrics로 performance.json에
보존한다(비계약 diagnostics; 중첩 구간은 wall time과 합산되지 않음을 주석 명기).

- `cache_loader_seconds`(lazy loader = cache fetch+identity/bytes 검증),
  `vector_digest_seconds`(행별 sealed digest 검사), `vector_encode_seconds`(FP32 인코딩·
  finite·L2 검증), `vector_sidecar_seconds`(사이드카 총), `sqlite_rows_seconds`(DDL+전체
  행 insertion), `fts_index_seconds`(FTS executemany), `working_verify_seconds`(구성 DB
  검증), `vacuum_seconds`, `final_verify_seconds`(immutable verify+revalidate+사이드카
  검증), `install_seconds`(seal/rename/fsync/final verify).

`_pipeline_result_payload`/report는 그대로 metrics/accumulated를 담으므로 운영 재실행
없이도 다음 run의 `reports/performance.json`에서 41분의 내부 분해가 관측된다. IO 횟수는
기존 `requests`·`body_requests`·`process_io_delta`가 유지된다.

### 최소 성능 개선 (`pipeline.py`)

- `BoundedVectorFetchMemo` 신설: export lazy loader가 sealed `(cache_key,
  input_sha256, vector_sha256)` triple 기준으로 중복 행의 **반복 DB fetch를 생략**한다.
  FIFO 상한 512개(4,096D f32 = 16KiB/개 → 상한 약 8MiB/execution)로 전체 embedding을
  메모리에 올리지 않는다. 재사용은 동일 triple(같은 expected vector hash)에만 발생한다.
- 계측 분리: 실제 cache fetch에만 `export_cache_lookups`, memo 재사용에
  `export_cache_fetch_deduped`. 운영 관측치 기준 unique embedding 조회 395,174 / export
  lazy 조회 619,134이므로 이론상 최대 약 224k 회 DB fetch+1회성 identity/bytes 검증이
  감소하고 나머지 행은 기존대로 사이드카 기록 전 sealed digest·finite·norm 검사를 그대로
  수행한다(검사 생략 없음, serving DB in-place 수정 없음, 게시 원자성·resume/fence 불변).
- `run`/`run --issuer`의 frozen·performance scope는 §3에서 정정됐다.

### 테스트

- memo 경계/격자/eviction: `TestBoundedVectorFetchMemo` 2건.
- 계측 존재 + 계측 유무 출력 동일성(동일 fixture, database_sha256·vector_sha256 일치):
  `test_export_profile_reports_internal_phases`.
- 기존 `test_exporter_v5.py`의 monkeypatch wrapper가 새 positional `profile` 인자를
  받도록 시그니처만 확장(의미 불변).

### 남는 지배 비용과 후속 과제

수정 전후 41분 분해를 특정 함수 비용으로 단정하지 않는다. 운영 관측(134k 반복 조회와
VACUUM·immutable 재검증·사이드카 재검증 합산)의 실제 비중은 **다음 운영 run의
`export_internal_seconds` 실측으로 확정**된다. 반복 cache fetch 상한 감소폭은 실측 전에는
`export_cache_fetch_deduped` 계측치로만 검증한다. 계측상 지배가 확인될 경우에만 후보:
(1) unique 키 제한 배치 조회(`get_embedding_v5` IN-chunk, identity/hash 검증 유지),
(2) 검증된 비선택 sealed 산출물 재사용은 preserve_inputs가 이미 담당 — cache 재사용과
다르다. serving DB 구조/샤딩 전환은 **이번 범위에 섞지 않는 별도 설계 과제**로 남긴다.
1 issuer = 전체의 1/8 목표는 세우지 않는다(FIX_06 §2.6 준수). peak RSS는 기존 memo가
없을 때보다 상한 8MiB 초과 유지되며 `process_peak_rss_bytes`가 매 run 기록한다.

## 3. [P2] scope 표시와 운영 진단 정정

- `cli.py` `run_command()`의 `WorkerPipeline(...)` 생성에
  `issuer_execution_mode=assignment.scope.mode` 전달 추가. 수동 `--issuer`는 `selected`,
  명시 `--all`은 `all`, rotation은 `round_robin`으로 frozen `issuer-scope.json`·
  performance의 `issuer_scope` payload·OriginPolicy visit_days가 실제 mode를 사용한다.
  **resume**: `scope_session`이 고정 파일에서 복원한 scope를 그대로 쓰므로 과거
  `mode=all, selected=subset` run의 resume/회전 호환은 기존 의미대로 차단 없이 동작한다.
  수동 실행이 날짜 cursor를 소비하지 않는 계약(scope_session의 rotation 미생성) 그대로다.
- `partial_cli.py`에 `resolve_partial_execution_mode(selected, saved_scope)` 신설:
  collect→process는 선택 시 `selected`, resume은 고정 scope 복원(bogus mode는
  `issuer_scope_changed`로 차단). 테스트: `TestPartialExecutionModeResolution` 4건 +
  기존 `test_017_scope.py` resume 3케이스가 복원 semantics 회귀 시험으로 통과.
- 호스트 운영 도구 유지 자료: 저장소에 helper 사본은 없고 host 전용(018 LIVE_FIX_01).
  `docs/OPERATIONS.md`에 (a) `pwd -P` 물리 경로 해석과 mode 2750/group cardrag 상속·
  backup_status 표시를 release 준비 시 확인할 것, (b) cached run 결과는 종료 시점 snapshot
  이고 현재 확인은 `backup status` 원장 별독, (c) `backup_recorded=True`/exit0/전역 pending0
  중 어느 하나만으로 전체 검증 완료를 판정하지 않음(`in_scope_remaining=0`+index commit),
  (d) secret 비보존 규칙을 신규 섹션으로 문서화했다. shellcheck 대상 변경은 없다(저장소
  shell 파일 수정 없음).

## 4. 보존 조건 확인

- 우리만 980/전체 5537 게시 결과와 비선택 7사 문서·OCR·구조·embedding 참조: 코드·데이터
  어디에도 삭제/덮어쓰기 없음. `preserve_inputs`·rotation·90일 freshness·lock·cursor·
  backup GC pin 경로 미변경(전체 테스트 통과).
- unresolved 정비(DRM/ZIP/최신 정상본 정책)·unresolved ledger 검증 불변.
- 기존 1390 tests 대신: 전 suite 재실행 증거 아래 §5.

## 5. 실행한 명령과 결과

```text
$ uv run --no-sync pytest apps/cardrag-worker/tests/ -q
1408 passed, 10 warnings in 53.39s          # baseline 1390 + 신규 18
$ uv run --no-sync pytest apps/cardrag-worker/tests/test_fix_06_scoped_backup.py -q
18 passed in 1.25s
$ uv run --no-sync ruff check apps/cardrag-worker/
All checks passed!
$ uv run --no-sync ruff format --check apps/cardrag-worker/
126 files already formatted                 # 신규 테스트 1개 파일 ruff format 적용 후
$ git diff --check
(출력 없음, 통과)
```

변경 파일(본 FIX 세션 추가·수정분만):
- `apps/cardrag-worker/src/cardrag_worker/backup.py`
- `apps/cardrag-worker/src/cardrag_worker/exporter_v5.py`
- `apps/cardrag-worker/src/cardrag_worker/pipeline.py`
- `apps/cardrag-worker/src/cardrag_worker/cli.py`
- `apps/cardrag-worker/src/cardrag_worker/partial_cli.py`
- `apps/cardrag-worker/tests/test_exporter_v5.py` (wrapper 시그니처 1줄)
- `apps/cardrag-worker/tests/test_fix_06_scoped_backup.py` (신규)
- `docs/OPERATIONS.md`

배포하지 않았다. 이미지는 현재 운영 `cardrag-worker:019-4dba44664659` 그대로다.

## 6. 편차와 미검증(운영) 항목

1. 편차 없음 — FIX_06 항목 1·2·3 모두 구현·검증. flush의 상태 문자열·전역 remaining
   semantics는 하위 호환을 위해 불변 유지하고 새 구분은 additive keys로만 추가한 점은
   FIX §1.5의 "구별해 결과 보고" 요구를 충족하는 최소 변경으로 판단했다.
2. 호스트 실측 미검증: 실제 pending 2건 회수, 다음 선택 run의 frozen `mode=selected`
   기록, `export_internal_seconds` 운영 분해, host/container UID/GID 다른 상태의 metadata
   read 확인, run-issuer 등 helper 동작은 배포 후 운영 단계에서 수행한다(읽기 전용 절차는
   §1에 기재).
3. 018 LIVE 검증 흐름(사용자 수동 우리→나머지 7사)은 본 세션에서 재실행하지 않았다.
4. 코드 변경은 worktree에 미커밋 상태로 두었다(017–019 공존 변경 보존). commit/merge/
   release는 사용자 승인 단계로 유보.
