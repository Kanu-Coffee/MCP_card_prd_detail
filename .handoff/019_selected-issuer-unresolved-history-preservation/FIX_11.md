# 019 FIX_11 — 캐시 재바인딩과 과거 metadata 제외의 조회 순서 의존 제거

2026-10-20 Reviewer: Codex. **FIX_10의 P1은 해결 확인. 새 과거 기록 분리 로직에 P2 한 건 보완 필요.** PLAN/PLAN_AMENDMENT_01, REPORT, FIX_01–10/각 REPORT, 기존 리뷰·운영·정책 자료 및 현재 Git/관련 소스를 대조했다. HEAD `aaeb2c4` 위 017–019 미커밋 변경과 기존 handoff는 보존한다. 018은 CLOSURE.md대로 종료 상태를 유지하며 후속 운영은 019에서 관리한다.

## 해결 확인과 경계

- null seal reuse key의 generation-only 결과도 pending 없이 metadata 귀속을 보존하고, 근거가 없으면 unbound로 완료를 차단한다. 기존 FIX_10 재현은 저장소 회귀에서 해결됐다.
- 로컬 cache 근거 재바인딩, receipt/index 완료 검사 및 읽기 전용 deferred/status 진단 회귀가 통과했다.
- 정상 정책상 pending은 결함이 아니다. 아래 문제는 현재 단위의 metadata가 검증됐는데도 과거 기록을 필수 미완료로 잘못 포함하는 경우다. 운영 데이터 손상이나 잘못된 바이트 전송이 확인된 것은 아니다.
- 본 리뷰에서 운영 배포·원격 flush·운영 원장 수정·배치/timer 기동은 하지 않았다.

## [P2] 현재 metadata가 캐시로 재바인딩되기 전에 읽힌 과거 기록이 scope에 남음

위치: `backup.py:scoped_backup_scope`, 현재 983–1037행.

같은 doc_map 순회에서 `doc_has_bound_metadata`를 갱신하며 과거 기록 제외 여부를 판단한다. run-local manifest가 없고 현재 metadata를 doc_map/cache 근거로 재바인딩해야 하는 경우, 현재 항목을 읽기 전까지 이 값은 False다. 따라서 먼저 읽힌 과거 항목은 allowed에 unbound로 추가된다. 뒤에서 현재 항목이 정상 바인딩돼도 앞서 추가한 과거 경로를 재평가하지 않는다.

doc_map 조회에 의미상 순서 계약이 없으며, 일반적으로 과거 기록이 먼저 저장돼 있을 수 있다. 동일한 의미의 원장 레코드라도 저장/조회 순서에 따라 scope가 달라지고, 검증된 현재 백업이 과거 unbound 때문에 계속 미완료가 된다. 기존 stale 테스트는 현재 run-local 번들이 처음부터 바인딩된 경우만 확인한다.

### Reviewer 재현

1. null key 문서 fixture/seal을 만든다. 현재 선택 등록 **전에** 같은 document_id의 과거 manifest doc_map/doc_info를 넣는다. 과거 info의 PDF/OCR SHA는 현재 값과 다르다.
2. 현재 `record_run_success`로 정상 등록하고, 현재 canonical manifest를 `cache/ocr/{key}/manifest.json`에 둔다. run-local native manifest는 제거한다.
3. `scoped_backup_scope` 실행 결과: 현재 manifest/READY는 cache 근거로 expected에 정상 바인딩되지만, 과거 manifest도 allowed에 남는다. mock scoped flush는 현재 항목을 백업하고도 `in_scope_unbound=1`, `backup_scope_complete=False`다.
4. 임시 DB에서 과거 doc_map의 key/value를 동일하게 유지하면서 행을 맨 뒤에 다시 넣으면 과거 경로가 allowed에서 제외된다. expected는 동일하다. 저장 순서만으로 판정이 달라진다.

```bash
PYTHONPATH=apps/cardrag-worker/tests:apps/cardrag-worker/src:packages/cardrag-core/src \
  uv run --no-sync pytest /tmp/cardrag019-review11/test_review11.py -q -s
# REPRO: same doc_map records reordered changes allowed scope; valid current cache still held by stale history
# 1 passed in 0.27s (현 결함을 assert한 재현)
```

이는 임시 원장과 mock 원격을 사용한 검증이다. 실제 원장 행을 재삽입하라는 운영 복구 지시가 아니다. tmp 파일은 영구 산출물이 아니므로 위 절차를 저장소 회귀로 옮긴다.

## 수정 요구와 인수 기준

- 현재 문서의 모든 검증 근거를 수집/재바인딩한 뒤 과거·불명 기록을 분류하거나, 동등한 방식으로 판정을 입력 순서와 독립적으로 만든다. doc_map 문자열 정렬이나 우연한 SQLite 행 순서를 정확성 근거로 삼지 않는다. 기존 함수 경계에서 최소 수정한다.
- 과거임이 확인되고 현재 필수 단위가 검증된 항목은 정책상 제외한다. 현재 증거가 없거나 귀속을 판정할 수 없으면 기존처럼 미확인·미완료로 남긴다. 모든 과거 기록을 무조건 제외하거나 doc_map만으로 전송 권위를 부여하지 않는다.
- 과거→현재/현재→과거 및 manifest/READY 순열에서 allowed/expected/진단/완료가 동일한지 검사한다. 현재 근거는 run-local/cache 양쪽을 포함하고, 캐시 손상/현재 근거 없음에서는 완료 차단을 유지한다. 과거 pending/receipt가 있더라도 강제 전송하거나 제거하지 않는다.
- null key와 key 존재, generation-only 누락 방지, 비선택 미전송, 정상 정책상 pending, receipt/index 충돌·missing·재시도 회귀를 유지한다.

## Reviewer 검증 및 다음 산출물

```text
uv run --no-sync pytest apps/cardrag-worker/tests/ -q
1443 passed, 10 warnings in 54.03s
uv run --no-sync pytest apps/cardrag-worker/tests/test_fix_06_scoped_backup.py -q
53 passed in 2.09s
uv run --no-sync ruff check apps/cardrag-worker/
All checks passed!
uv run --no-sync ruff format --check apps/cardrag-worker/
126 files already formatted
git diff --check
pass
```

Executor는 `FIX_11_REPORT.md`를 새로 작성하고, 수정별 회귀·전체 suite·ruff·diff 결과와 운영 미검증을 기록한다. 비선택 7사/전체 serving, 기존 hash/root/lock/원자성/재개/GC pin 계약을 보존한다. 이번 correction에서 실제 배포·원격 쓰기·운영 원장 수정·배치/timer 실행은 하지 않는다.
