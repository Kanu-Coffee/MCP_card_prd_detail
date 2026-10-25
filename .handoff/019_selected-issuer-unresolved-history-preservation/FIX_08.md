# 019 FIX_08 — 선택 백업 바인딩 우회와 완료 판정 보완

2026-10-20 Reviewer: Codex. **FIX_07 재검토 결과 보완 필요.** PLAN/PLAN_AMENDMENT_01, REPORT, FIX_01–07/각 REPORT, 기존 리뷰·운영 자료·BACKUP_POLICY_RECHECK_01 및 현재 Git/소스를 대조했다. HEAD `aaeb2c4`의 017–019 미커밋 변경을 보존한다. 기존 handoff 문서는 수정하지 않는다.

## 확인된 개선과 검토 경계

- 로컬 검증 번들이 있는 경로의 pending 내용 충돌 차단/근거 기반 재등록, flush 진단 스키마, 호출부 expected 전달 및 선택 재시도 CLI 추가는 확인했다.
- 실제 WorkerState fetch 경로의 bounded memo 검증도 확인했다. Reviewer 재측정: 900 loads/300 keys에서 fetch 900→300, baseline 0.0284초, memo 0.0146/0.0149/0.0147초, 관측 peak RSS 증가 4736KiB. 이는 작은 synthetic loader fixture 결과이며 전체 운영 export 개선율은 아니다. 이 항목을 다시 미구현으로 분류하지 않는다.
- 정상 정책상 pending은 오류가 아니다. hybrid/manual/budget 보류와 비선택 backlog를 보존하고 별도로 진단한다. 아래 재현은 모두 임시 원장·mock 원격이며 운영 손상이 확인됐다는 의미가 아니다. 리뷰 중 배포·원격 쓰기·운영 원장 수정·Worker 배치/timer 기동은 수행하지 않았다.

## 1. [P1] reuse key 경로의 무바인딩 fallback으로 같은 경로 내용 검증을 우회함

위치: `backup.py:scoped_backup_scope` 현재 850–854행, `flush` 1282행 이후.

seal에 `ocr_reuse_key`가 있으면 manifest/READY 경로를 `verified=False`로 허용하고 expected에는 넣지 않는다. run-local manifest가 없거나 검증에 실패하면 그 경로는 끝까지 무바인딩으로 남는다. flush는 binding이 None이면 모든 새 내용 검사를 건너뛰고 pending 자체의 SHA/size/local_path만 신뢰한다. 따라서 FIX_07의 같은 경로 충돌 문제가 정상 reuse-key 경로에는 남아 있다. 구형 호환은 임의 metadata 전송의 근거가 될 수 없다.

### Reviewer 재현 A

`_make_document(..., reuse_key_present=True, cache_kind='native')`로 fixture 작성 → 정상 등록 → run-local `native-manifest.json` 제거 → 같은 manifest remote_path의 pending을 다른 바이트/SHA/size/local_path로 변경 → 새 `scoped_backup_scope`와 `expected_artifacts`를 사용하는 flush 실행.

```text
path in allowed == True
path in expected == False
client.objects[path] == b'{"not":"the verified manifest"}'
status == 'succeeded', backup_scope_complete == True
```

### 요구와 인수 기준

- 선택 전송 대상 metadata는 실제 읽을 바이트와 seal의 PDF/OCR/contract/kind/reuse key/READY 짝을 정합성 있게 검증한 근거에 묶는다. pending/receipt/path 문자열 단독을 신뢰하지 않는다.
- run-local 증거가 없으면 검증 가능한 cache/spool 등 정식 근거를 사용하거나 명시적인 미확인/미완료로 보류한다. 무바인딩 전송 허용이나 범위에서 조용히 제외한 뒤 완료 판정으로 대체하지 않는다. 정상 legacy/native/content 재사용은 유지한다.
- key 존재/부재, local manifest 없음/손상, native/content, 같은 경로 manifest/READY 내용 충돌, 정상 spool 복구를 회귀 테스트한다. 실제 main/partial/`backup flush --run` 경로에 적용한다.

## 2. [P1] pending 행만 검사하므로 receipt 충돌·필수 artifact 누락도 선택 완료로 판정함

위치: `backup.py:flush` pending loop의 receipt 검사(현재 1293–1304행), index 구성, `backup_scope_complete` 판정(1545–1550행). 등록도 기존 receipt가 경로상 존재하면 내용을 대조하지 않고 생략한다.

내용 충돌 검사는 pending loop 내부에서만 수행된다. expected에 포함된 선택 artifact라도 pending이 없으면 해당 receipt를 검증하지 않는다. 완료 조건도 pending/lost_source 잔여 0, 이번 index commit, loop에서 발견한 conflict 0일 뿐, **필수 artifact별 검증 receipt와 index 항목의 완결성**을 확인하지 않는다.

### Reviewer 재현 B/C

정상 generation-only 번들 등록 후:

- B: manifest pending만 제거하고 같은 root/path에 다른 SHA(`'7' * 64`), size 42의 receipt를 넣는다. expected에는 정상 manifest SHA가 있다. 나머지 정상 artifact를 scoped flush하면 `in_scope_conflicts=0`, `backup_scope_complete=True`이며 원격 mock index에 잘못된 receipt SHA가 포함된다.
- C: manifest pending을 제거하고 receipt도 없는 상태에서 나머지를 flush하면 manifest가 전송되지 않았는데 `backup_scope_complete=True`다.

원장 변형은 격리된 결함 주입이다. 실제 원장에 수작업 삭제를 하라는 복구 지시가 아니다. 이미 존재하는 충돌 receipt를 등록이 생략하는 경우도 같은 검사 공백에 들어간다.

### 요구와 인수 기준

- 선택 필수 artifact 전체를 대상으로 현재 canonical root의 receipt SHA/size 및 committed index 정합성을 검사한다. pending 유무와 무관하게 충돌을 탐지하고 원격 overwrite 없이 안전하게 보고한다.
- expected artifact가 pending/유효 receipt 어느 쪽에도 없으면 근거 기반 정식 등록/복구 또는 명시적 미완료로 처리한다. 필요한 artifact가 scope 증거 소실 때문에 빠진 경우도 완료 증거로 삼지 않는다.
- 이번 batch가 다른 항목을 commit했다는 사실만으로 선택 전체 완료를 선언하지 않는다. 같은 root의 검증+index 완료 근거를 사용하여 정상 재시도/unchanged에서도 완료·미확인·실패 의미를 일관되게 유지한다.
- pending 없는 receipt SHA/size 충돌, 필수 항목 누락, 다른 root receipt, 정상 기완료 receipt, index 실패/재시도, 증거 소실을 테스트한다. 비선택 정상 backlog와 정책상 제외 대상은 필수 artifact로 오인하지 않는다.

## 3. [P2] 실제 run/process의 임계값 미달 경로에서는 선택 진단이 전부 null로 남음

위치: `cli.py` 현재 1705–1727행, `partial_cli.py` 341–355행.

두 호출부 모두 `should_trigger`가 false면 scope 계산/진단을 수행하지 않고 `backup_status='deferred'`만 넣는다. 새 필드는 초기 None 그대로다. FIX_07_REPORT는 hybrid/manual threshold 보류도 위 필드로 최종 전달된다고 설명하지만, 테스트는 flush 자체의 deferred 분기와 별도 `backup flush --run`만 검증한다. 실제 run/process는 그 flush 분기를 호출하지 않는다.

### 요구와 인수 기준

- 전송하지 않는 정상 보류에서도 읽기 전용 진단으로 선택 잔여/lost/conflict·범위 밖 backlog와 보류 이유를 제공한다. 완료 여부가 미확인이면 그렇게 명시하되 알 수 있는 카운트까지 전부 null로 숨기지 않는다.
- 진단을 얻기 위해 force flush하거나 threshold/budget 정책을 무시하지 않는다. 원격/provider 호출 없이 계산 가능한 값만 읽는다.
- 실제 main run 및 collect→process의 최종 JSON 경로에서 hybrid/manual 임계값 미달+비선택 backlog를 검증한다. disabled/없는 seal 등 진단 불가능한 경우의 None 의미를 명확히 하고 정상 보류를 오류로 표시하지 않는다.

## 재현과 검증 증거

Reviewer 임시 회귀 파일 `/tmp/cardrag019-review8/test_review8.py`의 A/B/C 모두 재현됐다. tmp 파일은 영구 산출물이 아니므로 위 재현 절차를 저장소 회귀로 옮긴다.

```text
PYTHONPATH=apps/cardrag-worker/tests:apps/cardrag-worker/src:packages/cardrag-core/src \
  uv run --no-sync pytest /tmp/cardrag019-review8/test_review8.py -q -s
3 passed in 0.28s  # 현 결함을 assert한 재현이며 수정 통과 테스트가 아님

uv run --no-sync pytest apps/cardrag-worker/tests/ -q
1422 passed, 10 warnings in 54.16s
uv run --no-sync pytest apps/cardrag-worker/tests/test_fix_06_scoped_backup.py -q -s
32 passed in 1.92s
uv run --no-sync ruff check apps/cardrag-worker/
All checks passed!
uv run --no-sync ruff format --check apps/cardrag-worker/
126 files already formatted
git diff --check
pass
```

## 보고서와 운영 인계

새 `FIX_08_REPORT.md`를 작성하고 기존 보고서는 보존한다. FIX_07_REPORT 운영 예시의 두 번째 Python 명령에는 `import sqlite3`가 없어 그대로 실행되지 않는다. 복구 예시의 `docker exec <worker-container>`도 이번처럼 이미 exited인 배치 컨테이너에는 사용할 수 없다. 실제 `/opt/cardrag/current/operations/worker-compose.sh`의 compose 일회성 실행 등 운영 구조에 맞게, 배포 후 사용할 구체적 provider-free 선택 재시도 절차를 제공한다. credential/private overlay는 출력하거나 저장소에 넣지 않는다. 현재 운영 image가 FIX_06/07을 포함한다는 잘못된 가정도 정정한다.

코드 검토·로컬 회귀 범위에서 수행하고 실제 배포/원격 flush/전체 배치는 실행하지 않는다. 비선택 7사와 전체 serving, 기존 hash/root/lock/원자성/재개/GC pin 계약을 보존한다. 수정별 테스트·전체 suite·ruff·diff 결과와 남은 운영 미검증을 새 보고서에 기록한다.
