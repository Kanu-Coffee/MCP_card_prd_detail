# 019 Reviewer 코드 검토 — FIX_11 해결 확인

2026-10-20 Reviewer: Codex. **판정: 현재 코드·로컬 테스트 범위에서 리뷰 통과. 추가 FIX 발행 없음. 실제 운영 백업 복구·배포 검증 완료 판정은 별도다.**

## 검토 기준

PLAN/PLAN_AMENDMENT_01, REPORT, FIX_01–11과 각 REPORT, REVIEW_01 및 기존 운영·백업 정책 재확인 자료, 현재 Git 상태와 관련 구현·테스트를 대조했다. HEAD `aaeb2c4` 위 017–019 변경이 미커밋으로 공존한다. 이 판정은 현재 작업트리에 대한 검토이며 특정 배포 이미지의 검증이 아니다. 기존 문서는 보존한다.

이전 리뷰에서 FIX_10의 null reuse-key metadata 누락 P1은 해결 확인했다. 이번 검토는 남은 FIX_11의 과거 기록 분류 순서 의존 P2와 관련 회귀를 중심으로 수행했다.

## FIX_11 해결 근거

- `backup.py:scoped_backup_scope`는 먼저 선택 문서의 현재 cache 근거를 검증·재바인딩하고, 그 결과를 확정한 후 별도 순회에서 과거·미확인 기록의 가시성을 결정한다. 과거 기록이 먼저 읽혔다는 이유로 이미 제외 가능한 항목이 unbound에 남던 문제가 해소됐다.
- 현재 근거가 검증된 경우에만 명백히 다른 PDF/OCR identity의 과거 기록을 제외한다. 현재 cache 손상·근거 소실 시에는 미확인 항목을 남겨 완료를 차단한다. doc_map만으로 내용 바인딩이나 전송 권위를 부여하지 않는다.
- 새 회귀는 과거→현재/현재→과거, manifest/READY 순서, 현재 cache 손상, 과거 pending 보존·미전송을 검사한다. 기존 generation-only 누락, receipt/index 정합성, 정상 재시도, 비선택 미전송 및 deferred/status 회귀도 전체 suite에서 통과했다.
- Reviewer는 별도 임시 fixture로 현재·과거 manifest/READY 4개 doc_map의 **24가지 순열**을 검사했다. run-local manifest가 없는 상태에서 현재 cache로 재바인딩했으며, 모든 순열에서 allowed/expected가 동일하고 과거 경로가 제외됐다. mock flush 및 후속 unchanged에서도 `in_scope_unbound=0`, `backup_scope_complete=True`였다.

이번 수정과 관련해 추가 correction이 필요한 결함은 발견하지 않았다. 정상 정책상 pending 또는 비선택 backlog를 실패로 일괄 분류하지 않는 기존 경계를 유지한다.

## Reviewer 직접 검증

```text
uv run --no-sync pytest apps/cardrag-worker/tests/ -q
1447 passed, 10 warnings in 54.91s

uv run --no-sync ruff check apps/cardrag-worker/
All checks passed!

uv run --no-sync ruff format --check apps/cardrag-worker/
126 files already formatted

git diff --check
pass

PYTHONPATH=apps/cardrag-worker/tests:apps/cardrag-worker/src:packages/cardrag-core/src \
  uv run --no-sync pytest /tmp/cardrag019-review12/test_order_review.py -q -s
24 permutations: identical allowed/expected, zero unbound, complete=True
1 passed in 0.32s
```

임시 순열 검증은 mock 원격/임시 ledger만 사용했다. tmp 파일은 영구 산출물이 아니며 저장소의 FIX_11 회귀가 지속 검증을 담당한다. warning 10건은 기존 invalid prior OCR/원격 OCR variant 충돌/whole-document fallback 회귀에서 발생했다.

## 운영 인계와 남은 검증

리뷰 중 제품 코드·운영 원장/serving·이미지·원격 백업·timer를 변경하지 않았다. 현재 운영 image가 이 작업트리의 FIX_06–11을 포함한다고 간주하지 않는다.

후속 운영에서는 식별 가능한 수정 release/image를 준비한 뒤 기존 run `41ce678e665b47179f8bfb2d82273882`의 읽기 전용 `backup status --run`으로 현재 scope를 확인하고, 검증된 선택 artifact만 정식 재시도하여 실제 receipt/index 완료와 원장 상태를 대조한다. 과거 종료 시점 cached 결과를 현재 복구 상태로 rewrite하지 않는다. 운영 명령은 FIX_09_REPORT의 entrypoint 중복 없는 compose 일회성 형태를 따른다.

실제 pending 2건 회수, 임계값 미달 main/collect→process의 운영 결과 필드, WebDAV 검증, 다음 run의 export 계측 및 실제 성능은 아직 운영 미검증이다. 코드 리뷰 통과만으로 백업 완료나 운영 속도 개선율을 선언하지 않는다. 비선택 7사 보존·나머지 7사 수동 검증과 8사 평가 후 timer 재개/첫 자동 slot 확인도 019 후속 운영 항목으로 유지한다.

018은 사용자 지시에 따라 `../018_worker-manual-live-validation/CLOSURE.md`로 종료됐으며 재개하지 않는다. 019는 코드 correction 리뷰를 마무리하고 위 운영 검증을 이어가는 상태다.
