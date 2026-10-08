# 009 FIX_02 REPORT — 수동 OCR 요청 완료 판정 보정

- 작성일: 2026-10-08. 역할: Executor.
- 브랜치: `codex/009-summary-stage-reuse`. 착수 기준 `3ca2654` / 구현 기준 `614b2e5`.
- 대상: FIX_02의 수동 OCR 실패 요청 false completion 1건.
- 결과: 구현 및 검증 완료. Reviewer 재검토에 제출한다.

## 1. 구현

### 요청에 결합된 성공 근거

`pipeline.py`의 정상 전체 경로와 `partial_execution.py`의 frozen PDF replay 경로에서 수동 요청 대상의 성공을 공통 `_record_reprocess_success`로 저장한다. 기존 resolver가 해당 reprocess_request_id로 반환한 durable content 결과를 확인한 뒤에만 근거를 작성한다. OCRResolver의 request-specific content lookup/검증과 provider 정책은 변경하지 않았다.

위치: `runs/<run_id>/ocr-reprocess-proofs/<document_id>.json`.

근거에는 immutable 요청 전체의 SHA256, target의 document/PDF SHA·size·page identity, OCR SHA·size, content reuse key와 variant ID를 포함한다. 기존 atomic 파일 작성 방식을 사용하며 새 네트워크 서비스/queue/모델을 추가하지 않았다.

### 완료 기록 직전의 공통 검증

전체 게시 결과가 succeeded일 때 바로 완료 receipt를 쓰지 않는다. 기존 `_validate_local_seal`로 seal/산출물을 검증하고, seal의 generation과 게시 결과가 같은지 확인한 뒤 `ocr_requests.py`의 `pending_reprocess_targets`로 모든 target을 대조한다.

- target이 없거나 OCR-failed/원문 없음이면 pending.
- PDF SHA/size/page가 요청과 다르면 pending.
- content variant가 없거나 요청에 결합된 성공 근거가 없거나 다르면 pending.
- 모든 target에 검증된 성공 근거와 일치하는 게시 산출물이 있을 때만 completed receipt 작성.

따라서 19/20 batch 게시가 성공해도 실패한 수동 요청은 pending으로 유지된다. `load_next_reprocess_request`가 다시 반환할 수 있다. 같은 partial run이 seal부터 재개해도 동일 검증을 적용하여 in-memory set 초기화로 실패 요청이 완료로 바뀌지 않는다.

진단: `runs/<run_id>/reports/reprocess-completion.json`에 pending/complete 상태 및 document_id와 고정 reason_code를 기록한다. 실패/미검증이면 배치 게시의 성공 사실은 유지하고 요청 완료만 보류한다.

### 정상 전체 수동 요청의 게시 유지

정상 전체 실행의 seal 게시 단계에서 corpus/contract가 같다는 이유로 no-change를 반환하던 분기에 `not self._has_active_reprocess_targets` 조건을 추가했다. 수동 재처리가 선택됐으면 새 content variant를 실제 게시하고 완료 검증까지 진행한다. 이는 정상 전체 수동 요청의 인수 기준을 검증하면서 필요한 보정이었다. 수동 요청이 없는 기본 전체 실행의 no-change 정책은 그대로다.

## 2. 검증

모두 fake OCR/HTTP/WebDAV와 임시 Worker state로 검증했다. 실제 provider 호출 및 운영 쓰기0이다.

1. Reviewer의 기존 재현을 변경 없이 재실행: **1 passed / 2.86초**. 19/20 게시 성공 후 실패 target의 completed receipt가 생성되지 않는다.
2. 정상 전체 실행으로 생성한 source(20상품 중19 OCR 성공/1실패)를 사용한 회귀 테스트:
   - 실패 target의 OCR를 실제 fake로 시도한 뒤 batch 게시 succeeded.
   - seal manifest의 failed1 보존, 요청 pending 유지, 구체적 pending 진단 확인.
   - exact committed generation으로 head 설정 후 interrupted 상태에서 같은 partial run 재개: succeeded, OCR/원격 쓰기 증가0, 요청은 여전히 pending.
3. fake durable content variant의 정상 성공:
   - partial 및 기본 전체 실행 모두 새 결과 게시 및 completed receipt 정상.
   - 정상 partial의 exact-commit 재개는 OCR 추가 호출 없이 완료 receipt 복원.
4. partial 재개 전에 성공 proof 삭제/변조: batch exact commit은 succeeded로 확인하지만 요청 receipt는 생성하지 않고 pending으로 유지.
5. local-only + 정상 durable fake 응답: 요청은 pending, 게시 완료로 인정하지 않음.
6. proof 단위 테스트: 정상 일치, proof 누락, 요청 hash/PDF/OCR/variant binding 변경, OCR-failed disposition의 7가지 경우 확인.
7. 기존 OCR skip 차단, variant 없는 결과 차단, stable fence, 재시도/95%/historical 정책 및 요약 다상품 테스트도 회귀 통과.

관련 명령:

```sh
.venv/bin/pytest -q apps/cardrag-worker/tests/test_partial_execution.py \
  apps/cardrag-worker/tests/test_ocr_requests.py \
  apps/cardrag-worker/tests/test_seal.py apps/cardrag-worker/tests/test_webdav.py
```

**84 passed / 18.13초**. 로그: `evidence/fix02-focused-tests.txt`.

최종 검사:

```sh
.venv/bin/ruff check packages/cardrag-core apps/cardrag-worker apps/cardrag-mcp tests/runtime_v1 tools
.venv/bin/ruff format --check packages/cardrag-core apps/cardrag-worker apps/cardrag-mcp tests/runtime_v1 tools
.venv/bin/mypy packages/cardrag-core/src apps/cardrag-worker/src apps/cardrag-mcp/src
.venv/bin/pytest -q packages/cardrag-core/tests apps/cardrag-worker/tests apps/cardrag-mcp/tests tests/runtime_v1
```

- Ruff 성공, 220 files format 확인, mypy 106 source files 성공.
- 최종 전체 **2462 passed, 9 warnings / 68.97초**. 기존 OCR 충돌/명시적 fallback 회귀의 예상 warning9다.
- 전체 로그: `evidence/fix02-tests.txt`.
- `git diff --check` 성공.
- 운영 readiness read-only 확인: `{"ready":true}`. 수정본 배포 검증을 의미하지 않는다.

## 3. 범위·제한 및 마감

- MCP 요약 분류 코드는 이번 FIX에서 변경하지 않았다. 이전 8개 카드사30상품 fixture 검증이 전체 회귀에 포함됐다.
- 기존 PLAN/REPORT/FIX/증거는 변경하지 않고 본 보고서와 fix02 로그를 새로 추가했다. 사용자 Excel3개 및 운영 volume/state를 유지했다.
- proof는 수동 재처리 대상에만 소규모로 작성한다. 큰 운영 사본/전체 OCR 반복/새 paid 평가를 만들지 않았다.
- FIX_02 적용 전 실행에는 새 proof가 없을 수 있다. 그런 실행의 완료 receipt가 없는 상태에서 seal만 재개하면 완료를 추정하지 않고 요청을 pending으로 유지한다. 다음 정상 수동 실행에서 기존 resolver의 request-specific content cache를 재사용할 수 있다.
- 이미 succeeded였던 **정상 전체 run**을 테스트에서 임의로 interrupted로 바꾸면 그 run에 연결된 corpus baseline까지 충돌하는 별도 상태가 생긴다. 이번 완료 판정 보정을 위해 baseline 정책을 변경하지 않았다. 정상 전체 요청의 게시/완료는 검증했고, FIX에서 요구한 동일 partial seal의 실패·정상·proof 누락/변조 resume를 직접 검증했다.
- 코드·회귀 테스트·새 증거·본 보고서를 검토 브랜치에 커밋/push한다. main 병합/운영 교체/정식 릴리스는 수행하지 않는다.

다음 역할: Reviewer의 FIX_02 및 009 최종 인수 검토.
