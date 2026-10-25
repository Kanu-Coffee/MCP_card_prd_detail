# 019 FIX_09 REPORT — 필수 무바인딩 경로 pending 독립 판정·운영 명령 entrypoint 정정

작성: 2026-10-20 Executor (OpenCode). `FIX_09.md` 2개 항목을 구현·검증했다. 기존 handoff
문서(FIX_01–09 및 각 REPORT 포함)는 수정하지 않았고, HEAD `aaeb2c4` 위 017–019 미커밋
worktree를 덮어쓰지 않았다. 배포·원격 flush·운영 원장 수정·배치/timer 기동은 수행하지
않았다.

## 1. [P1] 필수 무바인딩 경로: 행 유무와 무관한 완료 오판 차단

### 구현 (`backup.py`)

- `_scope_diagnostics`의 `in_scope_unbound`를 **행 순회 카운트 → 경로 단위
  `allowed − expected.keys()`**로 재정의. 검증 근거가 없는 필수 native 경로는 pending
  행이 0개여도, receipt가 존재해도(내용 무관), 행이 모두 삭제돼도 미완료로 남고
  `_scoped_completion`이 complete=True를 거부한다. 중복 행·복사 행이 판정을 바꾸지
  않는다(경로 집합). `in_scope_remaining`(행 단위: pending+lost)과 `in_scope_missing`
  (바운드 expected 중 receipt/행 모두 없음)는 기존 의미대로 **행/바운드 단위**이며,
  세 카운트의 단위(경로 vs 행)를 docstring과 OPERATIONS.md에 명문화했다.
- 전송 루프의 per-row 보류는 계속 동작하되 지역 카운터는 `held_unbound_rows`(주석용)로
  분리 — 진단 값은 경로 판정과 독립적으로 일관된다.
- `scoped_backup_scope` kind 분기 계약: native reuse-key 경로만 필수로 주장(run 번들 →
  로컬 cache 재바인딩 → 근거 없으면 unbound 필수). **adopted/content cache 정체성은
  공용 cache 산물(문서 소유 backup unit 아님)이므로 allowed에서 제외** — 선택 백업이
  주장·전송하지 않고 필수 미완료 카운트도 부풀리지 않는다("정책상 제외/비선택 backlog를
  필수 미완료로 확대 금지" 준수). 기존 content publication 경로·full-batch legacy cache
  scan 동작은 무영향.
- doc_map은 가시성 근거(전송 권위 아님) 계약 유지. 정상 근거 재바인딩/재시도 정식 경로는
  FIX_08 그대로(cache 근거 재바인딩 + record_run_success 재등록) 사용.

### Reviewer 재현 → 저장소 회귀

Reviewer 절차(재사용 fixture: reuse-key native 등록 → run manifest 삭제 → manifest/READY
**pending 행까지 제거**, receipt 없음)를 `test_fix_06_scoped_backup.py`에 고정했다:

- `test_required_unbound_paths_block_completion_without_rows` — 재현 자체:
  `in_scope_unbound == 2`, `in_scope_missing == 0`, CAS는 전송되나
  `backup_scope_complete is False`, 상태 succeeded 아님, metadata 미전송.
- `test_receipt_present_does_not_satisfy_required_unbound_path` — manifest receipt가
  (정확한 바이트로) 있어도 무바인딩 필수 경로는 미완료 유지.
- `test_duplicate_rows_on_unbound_path_do_not_change_path_counts` — 같은 무바인딩 경로에
  행 3개 중복이라도 카운트는 경로 단위(2)로 불변.
- `test_one_sided_bound_metadata_missing_blocks_completion` — 한쪽 metadata만 누락:
  바운드 READY 행 삭제 → `in_scope_missing == 1`·unbound 0·complete False(manifest 등
  나머지는 정상 전송).
- `test_deferred_status_reports_required_unbound_without_rows` — 행 없는 필수 unbound
  상태에서도 `scoped_pending_diagnostics`(read-only/deferred 경로)가
  `backup_in_scope_unbound == 2`를 노출하고 `backup_scope_complete`는 None.
- 회귀 유지: cache 근거 재바인딩 후 완전 재시도(`test_seal_reuse_key_rebinds_from_local_cache_and_spool`,
  complete True), unchanged 재실행(`test_unchanged_rerun_reports_completion_from_receipts_and_index`),
  receipt 충돌·missing·타 root(FIX_08), forged/doc_map 보류·비선택 미전송,
  실제 CLI 호출부의 expected 전달(`test_cli_backup_flush_run_scopes_to_verified_artifacts`
  — complete True 경로 유지 확인) 등.
- `test_seal_cache_kind_paths_follow_the_sealed_contract` 갱신: content/adopted cache
  경로는 선택 allowed에서 제외(공용 cache 경계), native는 run 근거로 bound.

운영 원장 SQL 삭제·허위 receipt·무바인딩 전송으로 해결하는 절차는 계속 금지(문서·보고
불변).

## 2. [P2] 운영 재시도 명령의 entrypoint 중복 정정

- 저장소 `Dockerfile` 확인: `ENTRYPOINT ["cardrag-worker"]`, `CMD ["run"]`
  (Dockerfile:183–184) — compose `service=worker`의 `command: ["run"]`은 entrypoint 뒤
  argv로 이어붙는다. FIX_08 §4 예시처럼 `cardrag-worker`를 args에 반복하면 하위 명령이
  중복된다.
- 현재 운영 image로 인자 구조만 재현(운영 volume/env/네트워크 미연결, `--network none`):

  ```text
  $ docker image inspect cardrag-worker:019-4dba44664659 --format '{{json .Config.Entrypoint}}'
  ["cardrag-worker"]
  $ docker run --rm --network none cardrag-worker:019-4dba44664659 cardrag-worker backup --help
  No such command 'cardrag-worker'.        # 중복 재현(FIX_08 예시의 결함)
  ```

- 수정 image 배포 후 사용해야 할 올바른 형태(backup flush/status 동일 규칙):

  ```bash
  /opt/cardrag/current/operations/worker-compose.sh run --rm --no-deps worker \
    backup flush --run 41ce678e665b47179f8bfb2d82273882 --issuer woori

  /opt/cardrag/current/operations/worker-compose.sh run --rm --no-deps worker \
    backup status --run 41ce678e665b47179f8bfb2d82273882   # 읽기 전용 스코프 진단
  ```

  host helper의 정확한 인자 전달은 `worker-compose.sh` usage로 사전 확인하되, 이미지
  진입 argv 자체는 로컬 소스로 검증했다 — entrypoint 이후 argv와 동일 형태로:

  ```text
  $ uv run --no-sync python -m cardrag_worker.cli backup flush --help   # --run/--issuer 노출, parse 정상
  $ uv run --no-sync python -m cardrag_worker.cli backup status --help  # --run/--issuer 노출, parse 정상
  ```

  `backup --help`/`--run` argv만 검사했고 실제 flush·원격 연결은 하지 않았다(리뷰 경계).
- 현재 운영 image `cardrag-worker:019-4dba44664659`는 FIX_06–09를 포함하지 않으므로
  `--run`/`--issuer` 옵션 사용에는 수정 image 배포가 선행 조건이다(위 검증은 현 image의
  중복 결함과 소스 CLI 계약 확인까지). credential·private overlay는 출력하지 않는다.
- OPERATIONS.md에 compose 일회성 형태(entrypoint 이름 반복 금지)와 경로 단위 필수
  unbound/content cache 경계 semantics를 반영했다.

## 3. 변경 파일·검증 결과

- `backup.py`(`_scope_diagnostics` 경로 단위 unbound·`held_unbound_rows` 분리·kind
  분기 native 전용 필수화/content-adopted allowed 제외)
- `tests/test_fix_06_scoped_backup.py`(FIX_09 회귀 5건 + kind 테스트 갱신: 총 45)
- `docs/OPERATIONS.md`(경로 단위 판정·공용 cache 경계·worker-compose argv 형태)

```text
$ uv run --no-sync pytest apps/cardrag-worker/tests/ -q
1435 passed, 10 warnings in 54.57s      # FIX_08 종료 시 1430 + 신규 5
$ uv run --no-sync pytest apps/cardrag-worker/tests/test_fix_06_scoped_backup.py -q
45 passed in 2.01s
$ uv run --no-sync ruff check apps/cardrag-worker/
All checks passed!
$ uv run --no-sync ruff format --check apps/cardrag-worker/
126 files already formatted
$ git diff --check
(통과)
```

## 4. 편차·남은 한계·운영 미검증

1. 편차: adopted/content cache 경로의 allowed 제외 — FIX_09 §1은 "정책상 제외/비선택
   backlog의 필수 확대 금지"를 요구했고, 해당 경로는 백업 등록자가 없어 어느 쪽
   (영구 unbound 필수 vs allowed 제외)든 전송 차단과 미전송은 동일하다. 완료 오판 방향의
   재현(native 무바인딩)만 필수로 남기는 경계를 택했다. 회귀로 고정.
2. `docker inspect`/`docker run --help` 재현은 현 image의 argv 결함 확인용이며, 수정
   image 배포 전에는 새 옵션 자체가 존재하지 않는다. 배포 후 첫 선택 재시도 시
   `backup status --run`(read-only)을 먼저 돌려 카운트를 확인하는 순서를 권장한다.
3. 실운영 pending 2건의 최종 회수, 임계값 미달 main/collect→process e2e 스코프 필드,
   index 원격 검증 실 WebDAV 동작은 배포 후 관측 항목(변경 없음).
4. 코드 변경은 미커밋 worktree로 Reviewer에게 인계한다. commit/release 없음.
