# 009 FIX_02 — 수동 OCR 재처리 실패의 잘못된 완료 판정 보정

- 작성일: 2026-10-08. 역할: Reviewer.
- 검토 대상: `614b2e5`, `codex/009-summary-stage-reuse`.
- 검토 자료: PLAN, REPORT, FIX_01, FIX_01_REPORT, 실제 Git/코드와 보관 증거.
- 판정: **요약 분류 개선 및 FIX_01의 기본 보정은 인정한다. 009 전체 인수에는 아래 실제 재현된 1건의 최소 보정이 남았다.**

## 1. 인정한 완료 사항과 재검증

- stable frozen source의 export/업로드 경합 차단, 정상 stable 게시, exact committed resume를 확인했다.
- 비율 포함 제외 문구의 benefit 제거, 실제 할인+예외 유지, 비율 표 행 복구 및 금리/수수료 문맥 제외를 확인했다.
- OCR-failed 재처리의 출처 결합, 정상 복구, OCR skip 상태 유지, finite retry, 95% 및 historical 실패 차단을 확인했다.
- 8개 카드사 30상품 원문 fixture/요약 증거, 500107 외 다양한 혜택의 테스트와 4상품의 정상 표 행 추가를 확인했다. 전체 상품 전수평가라고 해석하지 않는다.
- Executor 전체 테스트2449건 성공 로그 확인. Reviewer가 관련 suite를 다시 실행하여 **147 passed / 14.65초**를 확인했다:

```sh
.venv/bin/pytest -q apps/cardrag-worker/tests/test_partial_execution.py apps/cardrag-mcp/tests/test_summary_fields.py apps/cardrag-mcp/tests/test_metadata_tools.py
```

- 운영 readiness 읽기 `{"ready":true}`. 현재 검토 브랜치에 대한 `gh run list`는 빈 목록이므로 GitHub CI 통과를 주장하지 않는다. 로컬 검사/테스트 성공과 분리한다.
- 추적 파일은 검토 시작 시 깨끗했고 사용자 Excel3개는 untracked 상태 그대로다. 운영 쓰기/유료 호출은 없다.

## 2. [P2] 수동 재처리 대상의 OCR 실패도 요청 완료로 기록됨

### 코드와 원인

`partial_execution.py:547-568`은 OCR가 유한 재시도 후 실패하면 실패 문서로 남기고 다음 문서를 처리한다. 이는 95% 게시 정책에서는 정상이다. 그러나 실패한 문서가 `requested_ids`에 포함되어도 해당 수동 요청의 미완료 상태를 남기지 않는다.

`pipeline.py:2721-2734`는 전체 result가 succeeded이고 `_deferred_reprocess_ids`가 비어 있으면 요청의 모든 target이 성공했는지 확인하지 않고 `complete_reprocess_request`를 호출한다. 부분 replay에서는 그 set이 처음의 빈 상태로 남는다. 그래서 **batch 게시 성공과 수동 요청 완료가 혼동**된다.

`load_next_reprocess_request`는 completed receipt가 있는 요청을 건너뛴다. 따라서 실패한 대상의 재처리 요청이 자동 재실행 대상에서 빠진다. 기존 OCR/PDF를 삭제하는 오류는 아니다.

### 실제 작은 통합 재현

기존 테스트의 정상 전체 실행 fixture(20상품, OCR19성공/1실패)를 사용했다. 실패 문서에 정상 `ocr-` prefix의 수동 요청을 queue하고, PDF skip/OCR 선택/WebDAV 선택으로 실행했다. WebDAV·OCR·HTTP는 전부 fake이고 운영 자료는 사용하지 않는다.

- 해당 target의 OCR는 다시 실패했다.
- 전체19/20은 게시 기준을 충족하여 result.status=succeeded였다.
- 그런데 `ocr-requests/completed/ocr-review-pending-failure.json`도 생성됐다.
- ‘실패 target의 completed receipt가 없어야 한다’는 올바른 기대값으로 **1 failed / 2.72초**를 확인했다. 수정 성공 테스트가 아니다.

재현 파일: `evidence/reviewer-fix01-pending-repro.py`.

```sh
PYTHONPATH=apps/cardrag-worker/tests .venv/bin/pytest \
  -c pyproject.toml -o asyncio_mode=auto -q \
  .handoff/009_product-summary-field-classification/evidence/reviewer-fix01-pending-repro.py --tb=short
```

기존 pending 테스트는 local-only 종료 또는 성공 응답의 variant 부재를 검사했다. **실패가 격리되고 게시가 성공하는 조합**을 검사하지 않아 이 문제가 통과했다.

## 3. 필요한 최소 수정

1. 전체 batch 게시 성공과 수동 OCR 요청 완료를 분리한다. 요청 target 중 하나라도 OCR 실패/미처리/identity 불일치/검증되지 않은 variant이면 completed receipt를 기록하지 않는다.
2. 19/20 게시를 일률적으로 막을 필요는 없다. 정상 자료는 기존 95% 정책으로 게시하되 실패한 수동 요청을 pending으로 유지하고 대상 및 이유를 bounded 진단으로 남긴다.
3. 단순히 실패 직후 in-memory set을 채우는 구현만으로 끝내지 않는다. 같은 partial run이 seal부터 resume하는 경로에서도 미완료 target이 완료로 바뀌지 않게 한다. 저장된 요청·seal/게시 generation의 검증된 target disposition, PDF identity, durable content variant를 완료 근거로 사용하거나 이에 준하는 지속 가능한 검증을 한다.
4. target이 모두 실제로 성공하고 요청에 결합된 durable content variant가 검증된 경우의 정상 완료/재개는 유지한다. 요청 전체가 미완료이면 이미 성공한 target이 다음 요청 실행에서 다시 처리될 수 있으며, 이번 보정에 새 부분별 queue 체계를 도입할 필요는 없다.
5. 기본 정상 전체 실행, OCR skip 차단, local-only에서는 완료 receipt가 없는 의미를 유지한다. 완료 기록 직전 검증을 공용화할 경우 기존 정상 수동 요청 경로도 회귀 검증한다.

## 4. 인수 기준과 수행 범위

- 정상 source fixture + pending 실패 target + PDF-only skip + fake 게시 성공: published generation은19/20 실패 표시를 보존하고 completed receipt는 없으며, load_next_reprocess_request가 해당 요청을 다시 반환한다.
- 같은 실패 포함 seal을 interrupted/exact-commit resume하는 경우에도 false completed receipt가 없다.
- fake durable content variant로 target 모두 성공: 게시 및 완료가 정상이다. variant 없는 결과를 정상 완료로 바꾸지 않는다.
- 관련 pending/partial/게시/요약 회귀와 Ruff/mypy 수행. 전체 suite는 수정 후 통상 경로를1회 확인하면 충분하다.
- long Worker, 실제 Paddle/유료 추론, 03시 배치/수일 대기, 전체 PDF 재수집은 요구하지 않는다.
- 수정 파일은 필요한 최소 범위로 한정한다. 이전 문서/증거와 Excel/운영 state를 보호한다.
- 완료 후 **새 FIX_02_REPORT.md**를 작성하여 제출한다. 기존 REPORT/FIX_01_REPORT를 수정하지 않는다. 인수 전 main 병합/운영 교체/릴리스는 수행하지 않는다.

이 1건 외에 이번 검토에서 새 인수 차단 사항은 발견하지 않았다. 요약 분류 개선을 위해 장시간 Worker 검증을 추가할 필요는 없다.
