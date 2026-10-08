# 009 FIX_01 — Reviewer 검토 및 필요한 최소 보정

- 작성일: 2026-10-08. 역할: Reviewer.
- 검토 기준: `f0cf8a9`, `codex/009-summary-stage-reuse`.
- PLAN/REPORT/최종 추가 증거 및 현재 코드 확인. 이전 FIX 없음.
- 판정: **핵심 500107 개선과 부분 실행의 기본 경로는 인정한다. 009 전체 최종 인수는 아래 재현된 4건 보정 후 가능하다.**
- 이번 FIX는 인수에 필요한 해당 동작만 수정한다. 전량 PDF/OCR/재임베딩, Paddle 추가 검증, 03시 배치2회, 수일 대기, 새 평가 gate는 요구하지 않는다.

## 1. 인정한 완료 사항

500107의 1.2% 제목/혜택, 변경 고지 제거, 본인15,000원 및 근거 개선은 저장된 동일generation 30건 비교와 원문으로 확인했다. 독립 stage 옵션, 로컬 종료/게시 구분, source 유지, 기본 정상 경로 회귀, 인증된 작은 MCP HTTP 확인, 큰 운영DB clone/유료 추론 없이 검증한 방법은 타당하다.

Executor 전체2,381건 성공 기록과 후속160건 기록을 확인했다. Reviewer가 실제 관련 suite를 재실행하여 **82 passed / 4.32s**를 확인했다:

```sh
.venv/bin/pytest -q apps/cardrag-worker/tests/test_partial_execution.py apps/cardrag-mcp/tests/test_summary_fields.py apps/cardrag-mcp/tests/test_metadata_tools.py
```

추가 작은 재현4건은 **4 passed / 1.65s**다. 이 테스트들은 현재의 잘못된 동작이 발생한다는 assertion이 통과한 것이다. 수정 완료라는 뜻이 아니다. 재현은 `evidence/reviewer_repro.py`, 결과 요약은 `evidence/review-result.json`에 보존했다. fake provider/WebDAV 및 임시 state만 사용했고 운영 쓰기/유료 호출은0이다.

## 2. [P1] 처리 중 stable 변경 시 오래된 frozen source 재게시

### 확인 위치/재현

- `partial_cli.py:90-93`: source generation과 stable의 일치 검사는 시작 때1회다.
- `pipeline.py:6657-6665`: export 후 현재 원격 head를 다시 읽고 그 ID를 새 manifest의 predecessor로 채택한다.
- `_align_seal_to_current`는 이 새 predecessor와 현재head의 일치를 검사하므로, 처음의 frozen source와 최신head가 다르다는 사실을 차단하지 못한다.
- `test_stable_source_change_before_export_is_not_fenced`: 시작 시 source G 일치 확인 → fake 다른 writer가 H head 설정 → G의 동결 corpus로 새 seal 생성 → H를 previous_generation_id로 채택 → 게시가 `succeeded`로 끝남을 재현했다. 실제 운영 stable이 변경됐다는 보고가 아니라 해당 경합 경로의 작은 통합 재현이다.

### 필요한 수정

stable에 frozen PDF corpus를 게시할 때 **source generation/실행 시작 pointer를 게시 전까지 일관되게 결합**한다. 새head를 발견했다고 오래된 source의 seal을 새head 뒤에 연결하지 않는다. source와head 불일치이면 `skip_source_unavailable` 등 명확한 이유로 중단하고 stable pointer를 유지한다. immutable 출력의 생성 여부와 실제 stable 게시 여부를 구분한다.

검사를 시작 시점만에 두지 말고 predecessor 선정·게시 진입·같은 partial run의 resume 경계에도 적용한다. 기존 atomic 게시와 superseded-seal fence를 재사용한다. 완료 run의 재개에서 이미 해당 새generation의 exact commit이 입증되는 경우는 별도로 인정하여 안전한 idempotent reconciliation까지 막지 않는다. candidate/local-only에 불필요한 stable 조건을 추가하지 않는다. 전역 publisher 재설계나 신규 lock 서비스는 필요 없다.

### 인수 기준

fake 경합에서 stable H가 유지되고 G의 오래된 corpus를 게시하지 않는다. source==current인 정상 stable 재처리는 성공한다. local-only/candidate/기존 정상 resume는 유지된다. 원격 시스템 호출 없이 fake로 검증한다.

## 3. [P2] 비율 숫자가 있으면 명시적 할인 제외도 혜택으로 채택

### 확인 위치/재현

`summary_fields.py:109-115`는 제한 문구를 찾더라도 `%`가 있으면 restriction_only를 해제한다. `1.2% 할인 대상에서 제외`가 현재 `benefit`와 `condition` 양쪽에 들어간다. 실제 제공 혜택을 표현하지 않는 명백한 제외 문장이다. `test_exclusion_currently_appears_as_benefit`에 재현했다.

### 필요한 수정

비율 유무와 별개로 **해당 할인/적립 적용이 부정된 절인지** 판단한다. 제외-only 문장은 condition으로 보존하고 benefit에서는 제거한다. 단순히 ‘제외’ 단어가 있다는 이유로 전체 문장을 버리지 않는다. `국내1.2% 할인(무이자 할부 제외)`처럼 실제 혜택과 예외가 함께 있는 문장은 원문 그대로 양쪽 필드에 채택할 수 있다. 500107의 ‘조건·한도 없이’와 진짜 조건부 혜택은 보존한다.

### 인수 기준

명시적 `1.2% 할인 대상에서 제외`는 benefit에 없다. 실제 할인+예외, 실제 조건부 할인, 제한 면제, 무료 서비스의 원문/evidence는 유지된다. 새상품코드 특례/LLM 분류는 금지한다. 같은generation의 summary cache에 이전 결과가 남지 않도록 이번 수정의 classifier version/키 처리도 확인한다.

## 4. [P2] 비율만 있는 실제 표 데이터가 조상 문맥 검사 전에 탈락

### 확인 위치/재현

`summary_fields.py:65-71`의 TABLE_ROW 조기 필터는 `_AMOUNT`/혜택단어/조건단어만 보고 행을 제거한다. `_AMOUNT`에는 `%`가 없다. 뒤의 `table_offer`는 비율과 조상 혜택 제목을 다루지만 그 전에 행이 사라진다.

`국내외 할인` 제목 아래 `| 국내 | 1.2% |`를 두면 해당 행에 후보가0이다. `test_percentage_only_table_currently_lost`로 확인했다. 실제 표의 데이터 셀마다 ‘할인’이 반복되지 않는 정상 형식에 영향을 준다.

### 필요한 수정/인수 기준

표의 role/header는 계속 구분하되 비율 데이터 행을 조상/표 헤더 의미와 함께 평가할 수 있게 한다. 혜택 표 아래의 실제 비율 행을 benefit으로 채택하고 node/page evidence를 연결한다. 다른 통계·금리·수수료 표의 단순 숫자/비율을 전부 혜택으로 승격하지 않는다. 연회비 표, 표 헤더, 월 한도 row, 500107 및 기존30건 비교를 유지한다.

## 5. [P2] PDF만 스킵해도 기존 OCR-failed 문서의 OCR를 영구 건너뜀

### 확인 위치/재현

`partial_execution.py:426-448`에서 serving revision이 없는 OCR-failed 문서는 옛 failure record를 복사하고 무조건 continue 한다. `plan.skips('ocr')`를 검사하지 않는다. 따라서 `--skip-pdf`만 지정하여 OCR 단계가 실행 대상이어도 이전 실패 문서를 다시 처리하지 않는다.

`test_failed_document_is_carried_even_when_ocr_is_selected`는 실패 문서 흐름을 작은 source/pipeline double로 구성하여 OCR calls0, processed0, 옛 failed_documents1이 그대로 export로 전달됨을 확인했다. 이 재현은 분기 동작을 검사한 control-flow test이며 완전한 failed-generation 출처 검증/95% gate 통합 테스트라고 주장하지 않는다.

### 필요한 수정

OCR까지 명시적으로 스킵한 경우에는 실패 상태/원문 없음 표시를 유지한다. **PDF만 스킵하고 OCR는 실행할 때에는** source의 실패 문서도 snapshot/seed/source identity와 PDF SHA에 결합해 기존 정상 OCR 처리 정책으로 시도한다. 새 discovery/download는 하지 않는다.

성공 시 구조/view/export와 issuer별 성공/실패 count를 실제 새 결과에 맞게 갱신한다. 실패 시 기존 finite retry·현재 문서 성공률 정책/이력 실패 규칙·bounded failure ledger를 적용한다. pending 수동 재처리 대상의 identity 및 durable content variant를 검증하고 실제 처리 없이 완료 receipt를 쓰지 않는다. 실패 문서라는 이유만으로 source metadata가 필요 없다고 가정하지 말고, 출처를 복원할 수 없으면 reason_code로 차단한다. 기존 OCR provider 정책/명시적 fallback만 사용하며 새 Paddle 경로나 유료 실제 검증을 추가하지 않는다.

### 인수 기준

작은 fake fixture에서 PDF-only skip + 이전 OCR-failed 문서가 OCR를 시도하고, fake 성공이면 정상 구조/문서로 복구되는 것을 확인한다. OCR도 skip하면 calls0/실패 표시 유지. source ID/PDF 변경 또는 metadata 부재이면 숨은 수집/provider fallback 없이 차단. manifest/issuer count 정합성 및 pending receipt를 확인한다.

## 6. 수행 범위와 마감

1. 현재 source는 `f0cf8a9`이다. 위4건에 관련된 파일만 최소 수정한다. 사용자 Excel3개, 운영 volume/DB, 릴리스v1.0.33, 이전 handoff/REPORT/증거를 보호한다.
2. 재현 파일의 assertion은 ‘현재 버그 발생’을 확인하는 용도다. 실제 회귀 테스트는 패키지 tests에 올바른 기대값으로 추가한다. 해당 재현들을 통과시키려고 버그를 유지하지 않는다.
3. 관련 요약·partial·seal·CLI 테스트, Ruff/mypy를 수행하고 최종 통상 전체 CI test 경로를1회 확인한다. source==head 정상 stable과 changed-head 차단은 fake로 검증한다. long Worker 및 실제 provider 호출은 인수 조건이 아니다.
4. 실데이터 재확인은 같은generation 30건 read-only 비교로 가능하다. 기존 JSON을 덮어쓰지 말고 `fix01-*` 새 증거를 저장한다. 500107 필수 필드와 evidence가 유지되는지 확인한다.
5. 완료 후 새 `FIX_01_REPORT.md`를 작성한다. 변경/commit/명령·결과, 각4건의 수정 전후, 출처/게시/호출수/남는 제한을 명시한다. 기존 REPORT를 다시 쓰지 않는다.
6. Reviewer 인수 전 main 병합/운영 교체/정식 release는 수행하지 않는다. 공개 CI가 필요하면 검토 브랜치에서 기존 절차를 따른다. 코드 양식 개선만으로 새 인수 조건을 늘리지 않는다.

현재 운영 장애/데이터 훼손은 발견되지 않았다. 500107 중심 MCP 수정은 유효하며 별도로 검토 가능한 범위다. 009 전체 인수에 필요한 보정은 위 재현된 동작으로 한정한다.
