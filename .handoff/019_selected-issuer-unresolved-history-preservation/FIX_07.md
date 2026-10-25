# 019 FIX_07 — 검증 번들과 pending 내용 바인딩·최종 진단·성능 검증 보완

2026-10-20 Reviewer: Codex. **FIX_06 재검토 결과 보완 필요.** 기존 PLAN, PLAN_AMENDMENT_01, REPORT, FIX_01–06 및 각 REPORT, REVIEW_01, 운영 보고서와 BACKUP_POLICY_RECHECK_01, 현재 Git 상태/소스/테스트를 대조했다. 기존 문서는 변경하지 않는다. Git HEAD는 `aaeb2c4`이며 017–019 미커밋 변경이 공존한다.

## 판정과 경계

- 정상적인 정책상 pending은 결함이 아니다. hybrid/manual 임계값, inline budget, 선택 범위 밖 artifact 등으로 남는 pending과 선택 artifact의 연결·내용 검증 실패를 구분한다. pending을 일괄 전송하거나 삭제하여 수치를 맞추지 않는다.
- 새 `verified_document_ocr_bundles`가 실제 우리 run `41ce678e665b47179f8bfb2d82273882`의 해당 문서에서 번들 1개를 찾는 것을 확인했다. 현 운영 state volume 및 새 소스를 read-only로 마운트하고 network를 차단한 임시 컨테이너에서 수행했다. manifest/READY 연결 누락에 대한 개선 자체는 확인됐다.
- 이 확인은 원격 전송·receipt/index commit 완료의 증거가 아니다. 본 리뷰에서 운영 배포, ledger 수정, 원격 쓰기, Worker 배치 또는 timer 기동은 하지 않았다.
- 비선택 7사 보존, 전체 8사 serving, unresolved 정비 및 기존 hash/root/원자성/재개 계약을 유지한다. 이번 FIX는 운영 데이터 손상이 발생했다는 판정이 아니라 아래 재현된 구현 결함과 검증 공백에 대한 지시다.

## 1. [P1] 같은 경로의 기존 pending이 검증된 manifest와 다른 내용을 전송할 수 있음

위치: `backup.py:_register_verified_ocr_bundle`(현재 194–271행), `record_run_success`, `scope_remote_paths`, `flush`.

번들 함수는 manifest/body/READY를 검증하지만 허용 근거가 `frozenset[remote_path]`로 축약된다. 등록 함수는 기존 pending/receipt의 **경로 존재만** 확인하고 해당 행의 SHA·크기·내용을 검증된 번들과 대조하지 않는다. flush는 허용된 경로에 있는 기존 행의 local_path/SHA/size를 사용하므로 검증된 번들과 다른 내용도 자체 전송 검증을 통과한다.

### 재현 증거

임시 state와 `RecordingClient`만 사용한 회귀 재현 1건 통과. 실제 원격/운영 원장은 변경하지 않았다.

```text
PYTHONPATH=apps/cardrag-worker/tests:apps/cardrag-worker/src:packages/cardrag-core/src \
  uv run --no-sync pytest /tmp/cardrag019-review7.ufeKef/test_review7.py -q -s
REPRO: same-path conflicting pending uploads wrong manifest and reports succeeded
1 passed in 0.27s
```

임시 파일은 영구 검증 자료가 아니므로 Executor는 아래 재현을 저장소 회귀 테스트로 옮긴다.

1. `test_fix_06_scoped_backup.py`의 `_make_document`, `_settings`, `_write_seal`로 선택 문서와 정상 번들을 만든다.
2. `record_run_success(..., selected_issuers=('woori',))`로 등록한다.
3. 임시 DB에서 해당 manifest pending의 **remote_path는 유지**하고 local_path를 `b'{"not":"the verified manifest"}'` 파일로, sha256/size_bytes도 그 바이트 값으로 바꾼다.
4. 다시 `record_run_success` 후 `scope_remote_paths`로 얻은 범위에 mock flush한다.
5. 현재 결과는 `status == 'succeeded'`, `client.objects[manifest_path] == wrong_bytes`다. 검증된 canonical manifest가 전송되지 않는다.

기존 forged 테스트의 다른 reuse_key/path는 이 충돌을 검증하지 않는다.

### 수정 및 인수 기준

- 선택 허용 근거를 실제 전송 artifact의 경로·SHA·크기·계약과 바인딩한다. 같은 경로라는 이유만으로 기존 pending/receipt를 정상 번들의 대체물로 신뢰하지 않는다. 등록과 flush 사이에도 최종 바이트 정합성이 유지되어야 한다.
- 충돌 행/동일 경로 중복 행/receipt 불일치는 명확히 진단하고 안전하게 중단하거나 증거가 있는 정식 복구 절차로 처리한다. 임의 원격 overwrite, 원장 수작업 삭제, 허위 receipt로 통과시키지 않는다.
- manifest뿐 아니라 body/READY의 같은 경로 충돌, 잘못된 receipt hash, spool 손상, 정상 기존 pending의 멱등 재시도 및 index commit 실패를 테스트한다. 원격 root 바인딩과 비선택 미전송을 유지한다.

## 2. [P2] 선택 백업 진단이 최종 결과로 전달되지 않으며 lost_source가 누락됨

위치: `backup.py:flush`(현재 1029행 및 1360–1401행), `cli.py`(1715–1717행), `partial_cli.py`의 flush 후 payload 구성.

- 새 `in_scope_remaining`, `pending_out_of_scope`, `pending_out_of_scope_bytes`는 flush 반환값에만 있다. main CLI는 status/uploaded bytes/requests만 복사하고 partial CLI도 새 값을 전달하지 않는다. 전역 status_dict로는 이 구분을 복원할 수 없어 사용자 최종 run/status 진단에서 정책상 정상 pending을 구분할 수 없다.
- `in_scope_remaining`은 `status='pending'`만 센다. 선택 artifact가 `lost_source`면 미완료인데도 0이 될 수 있다. 전역 lost_source 수와 선택 범위의 완료 판정은 별개다. 조기 반환/보류/실패 분기에서도 진단 스키마가 일관되지 않다.

### 수정 및 인수 기준

- 선택 pending, 선택 lost_source, 범위 밖 backlog, 정책/budget 보류, 전송·검증·index commit 상태의 의미를 정의하고 main/collect→process 최종 결과까지 전달한다. 기존 전역 카운트 의미와 과거 결과 호환은 유지한다.
- 0이라는 값만으로 선택 백업 완료를 표시하지 않는다. 필요한 원격 검증과 receipt/index commit 완료를 함께 반영한다. 기존 degraded 로그나 종료 시점 cached 결과를 현재 복구 상태로 rewrite하지 않는다.
- 실제 CLI 결과 경로를 통해 선택 완료+비선택 pending, hybrid/manual/budget 보류, 선택 lost_source, index commit 실패, 변경 없는 실행을 검증한다. 정상 정책상 pending을 일괄 오류로 취급하지 않는다.

## 3. [P2] bounded memo의 실제 성능 개선·메모리 검증이 미완료

위치: `pipeline.py:BoundedVectorFetchMemo` 및 export lazy loader, `exporter_v5.py`, `test_fix_06_scoped_backup.py`, `FIX_06_REPORT.md`의 성능 주장.

FIFO 상한과 계측 추가는 확인했다. 그러나 현재 테스트는 memo 클래스 자체의 경계 검사와 단순 lambda loader를 사용하는 exporter의 profile 유무 출력 비교다. 실제 pipeline/state 조회에서 memo 전후 fetch 수가 줄고 같은 결과가 나오는지 검증하지 않는다. 동일 fixture의 반복 시간·peak RSS·임시 디스크 비교도 없으며 REPORT는 이를 다음 운영 run으로 미뤘다. 이는 FIX_06 §2의 완료 조건을 충족하지 않는다. 현재 코드가 느려졌다는 판정은 아니다.

### 수정 및 인수 기준

- 반복 키를 가진 여러 문서/선택+비선택 조합의 bounded fixture로 실제 lazy loader와 state fetch 경로를 실행한다. memo 비활성 기준과 활성 결과의 실제 DB fetch 수, hit/eviction, output/vector hash 정합성을 비교한다. 손상·키/벡터 hash 불일치 차단이 유지되는지도 검사한다.
- 같은 입력/설정의 반복 측정으로 wall time, 중첩 계측, fetch 수, peak RSS와 임시 디스크 증가를 보고한다. 전체 운영 DB 복제나 90분 배치 재실행은 요구하지 않는다. 모든 embedding을 메모리에 적재하지 않는다.
- 운영 export 41분 전체의 개선이나 이론상 최대 dedup 수를 실측 결과로 표현하지 않는다. 개선되지 않은 공통 비용과 검증 한계를 명시한다.

## 보고서·운영 인계 보완

`FIX_06_REPORT.md`는 보존하고 새 `FIX_07_REPORT.md`에 정정한다. 기존 보고서의 `/opt/cardrag/state`, `/opt/cardrag/current/src` 예시는 실제 named-volume/image 운영 구조와 맞지 않는다. 실제 release/helper/volume 구조에 맞는 복붙 가능한 읽기 전용 확인 명령을 제공한다. 읽기 전용 확인에 별도 운영 승인이 필요하다고 쓰지 않는다.

선택 2건 복구를 위해 전역 `backup flush --force`를 기본 절차로 삼지 않는다. 기존 run의 검증된 선택 artifact만 provider 호출 없이 정식 재시도할 수 있는 구체적 절차와 그 제한을 제시한다. 실제 배포·원격 쓰기·배치 실행은 이 리뷰 요청 범위에 포함하지 않는다.

## 리뷰 검증 결과와 다음 산출물

```text
uv run --no-sync pytest apps/cardrag-worker/tests/ -q
1408 passed, 10 warnings in 53.79s
uv run --no-sync ruff check apps/cardrag-worker/
All checks passed!
uv run --no-sync ruff format --check apps/cardrag-worker/
126 files already formatted
git diff --check
pass
```

기존 suite 통과가 위 같은 경로 충돌 재현을 무효화하지 않는다. Executor는 기존 미커밋 변경을 보존하고 FIX_07을 구현한 뒤 `FIX_07_REPORT.md`를 새로 작성한다. 수정별 회귀와 실제 CLI/전후 측정 증거, 전체 Worker suite·ruff·diff 검증, 수행하지 않은 운영 작업 및 남은 제한을 기록한다. 과거 handoff 문서를 덮어쓰지 않는다.
