# 009 FIX_01 REPORT — Executor 보정 완료

- 작성일: 2026-10-08. 작업 브랜치: `codex/009-summary-stage-reuse`.
- 착수 기준: `c985e2f` (009 구현 `f0cf8a9`와 Reviewer FIX_01 포함).
- 수행 범위: FIX_01의 4건 및 사용자가 추가 요청한 다상품 검증. 이전 문서/증거와 사용자 Excel 3개는 유지했다.
- 결과: 아래 보정과 검증을 완료하여 Reviewer 재검토에 제출한다. main 병합, 운영 교체, 정식 릴리스는 수행하지 않았다.

## 1. 구현 내용

### 1.1 frozen PDF source와 stable 게시의 결합

`pipeline.py`에서 predecessor 선정 직전, seal 게시 진입, immutable 업로드 완료 후 pointer 교체 직전에 source generation과 최신 stable generation을 비교한다. 실제 WebDAV에서는 force_refresh로 재조회한다. 다른 head를 발견하면 `skip_source_unavailable`로 중단하고 그 head를 유지한다. `webdav.py`의 기존 atomic 게시에 pointer 교체 직전 검증 callback을 추가했다.

동일 corpus/contract라는 이유만으로 **다른 partial generation**을 `no_change` 처리하던 게시 분기도 보완했다. partial 재처리 결과는 새 seal로 게시하며, 기본 전체 실행의 기존 no-change 정책은 유지한다.

재개 시 현재 head가 해당 run 자체의 generation이면 CLI가 seal 검증 단계까지 진입하도록 허용한다. pipeline이 로컬 seal과 원격 bundle의 exact commit을 입증한 경우에만 게시 완료로 조정한다. candidate/local-only에는 stable source 조건을 추가하지 않았다. 기존 atomic MOVE를 유지하며 전역 분산 writer lock은 신설하지 않았다.

검증: export 중 다른 writer의 H 생성, 업로드 중 H 생성 모두 차단되고 H pointer가 유지됐다. source==head 정상 게시 성공, exact committed partial의 interrupted 상태 재개는 추가 원격 쓰기 없이 succeeded로 복원됐다.

### 1.2 비율이 있는 제외 문장

`summary_fields.py`가 명시적으로 부정된 할인·적립·캐시백 절을 분리하여 판단한다. `1.2% 할인 대상에서 제외`, `5% 적립 미적용`, `2% 캐시백 불가`는 benefit에서 제외된다. 실제 혜택+괄호 예외, 실제 조건부 혜택, 실적 면제는 유지된다. 상품코드 특례나 LLM 분류는 추가하지 않았다.

classifier version을 `cardrag.product-summary.v3`으로 변경했다. 기존 Catalog의 version 포함 summary cache key를 통해 같은 generation의 v2 결과와 분리된다.

### 1.3 비율만 있는 표 데이터

TABLE_ROW의 조기 필터가 `%` 데이터 행을 조상/표 헤더 문맥 검사까지 전달하도록 변경했다. 할인·적립·캐시백 문맥 아래 실제 비율 행은 benefit으로 채택한다. header role, 제외 문맥, 연회비 처리와 기존 근거 연결은 유지한다. 금리·이자·수수료·통계 문맥을 단순 비율만으로 혜택 표로 판단하지 않는다.

검증: 할인/포인트/캐시백 표의 `| 국내 | 1.2% |` 채택, 할부금리/할인금리/수수료/통계 표의 동일 행 비채택. 실제 약관의 새 표 후보도 확인했다.

### 1.4 PDF-only skip의 OCR 실패 복구

`partial_execution.py`가 source의 기존 OCR-failed 문서를 snapshot/seed의 issuer·product·source identity와 PDF SHA/document ID에 결합하여 복원한다. 출처가 없거나 여러 개면 `skip_source_unavailable`로 차단한다. PDF 재수집은 없다.

OCR를 선택하면 기존 resolver와 finite retry를 사용하고, 성공 문서는 구조/view/export에 포함한다. 새 성공/실패로 issuer count를 재산정한다. OCR도 skip하면 기존 실패 상태와 calls0를 유지한다. 재실패는 bounded 실패 ledger를 기록하고, 카드사별 95% 성공률 및 historical failure 차단을 적용한다. systemic 오류는 document failure로 격리하거나 재시도하지 않는다. pending target의 PDF identity와 durable content variant 확인도 유지하며 variant 없는 응답을 완료로 인정하지 않는다.

**정상 전체 실행으로 만든 작은 source fixture**를 사용했다: 가짜 20상품 중 19 OCR 성공/1 실패로 seal/export 생성 → ReuseSource 검증 → 부분 실행. 분기 double만 검사한 것이 아니다.

- PDF-only skip + 가짜 성공: 기존 실패 문서에 OCR 수행, 새 manifest acquired20/succeeded20/failed0, 정상 문서20. PDF HTTP 증가0.
- PDF+OCR skip: OCR calls 증가0, acquired20/succeeded19/failed1 유지.
- 재실패: 2회 설정 시 해당 문서 2회 시도, ledger attempts2, 19/20 정책으로 local-only 완료.
- 2문서 실패: 18/20으로 export 차단.
- historical 1문서 실패: 19/20이어도 export 차단.
- metadata 부재: inference 전에 source 오류로 차단.
- systemic configuration 오류: 해당 문서 1회 호출 후 종료, 3회 설정에도 재시도하지 않음.
- 실패 문서 pending 수동 요청: OCR 대상에 포함되지만 durable variant 없는 fake 결과는 차단, completed receipt 없음.

## 2. 여러 실상품 검증

동일 저장 generation `g-eba5ca0d13924abdb1f36937-71a5fd98d58b`의 DB를 **read-only immutable**로 열어 현재 소스의 `CatalogRepository.summaries`를 실행했다. Docker는 network none, 운영 volume/worktree는 read-only로 연결했다. 새 PDF/OCR/LLM/임베딩 호출 및 운영 DB 쓰기는 0이다. 최신 수집이나 새 worker 완료를 주장하는 검증이 아니다.

BC·하나·현대·KB·롯데·삼성·신한·우리 **8개 카드사 30상품**을 비교하고, 모두의 실제 구조 node를 자동 회귀 fixture에 포함했다. 500107만 확인하지 않았다.

대표 명시 검증:

| 카드사/상품 | 확인 혜택 |
|---|---|
| 우리 500107 | 1.2% 할인, 변경 안내 제거, 본인15,000원, 정확한 node/page/revision 근거 |
| 우리 104022 / 104023 | 0.8% 할인 / 5% 캐시백 |
| 하나 15911 / 15758 | 캐시백 / 바우처 |
| 현대 149298 | M포인트 적립 |
| KB 04404 | 반려견 단체보험 |
| 롯데 1118 | 5~7% 할인 |
| 삼성 AAP1920 계열 | 10% 할인 |
| 신한 00368 | 최대15% 할인 |

30상품의 선택된 요약 근거는 해당 현재 revision의 실제 node/text/page에 연결됨을 확인한다. 출시일 근거가 과거 revision을 참조하는 기존 정상 동작은 별도로 선언된 launch_date_source_revision_ids와 검사한다.

v2 대비 요약 필드가 바뀐 상품은 **4개**다: 하나15911 택시5%, 우리500105 특별적립2%/1.5%, 우리500104 경기장30%/스토어20%, 우리100068 특별적립5%/3% 표 행이 추가됐다. 다른26상품의 해당 요약 필드는 동일하고 500107 개선도 유지됐다. 최대5개 요약 슬롯이므로 새 표 행이 들어가면 뒤의 다른 정상 혜택이 요약에서 밀릴 수 있다. 전체 약관의 제공 범위를 줄인 것은 아니다.

증거:

- `evidence/fix01-summary-sample.json`: 30상품 현재 Catalog 결과와 500107 원문/근거.
- `evidence/fix01-product-fixtures.json`: 30상품 실제 node fixture.
- `evidence/fix01-summary-comparison.json`: 동일 generation v2/v3 차이.
- `evidence/fix01-readonly-samples.py`: 저장 결과 재현용 read-only Catalog 실행 스크립트.
- `evidence/fix01-tests.txt`: 최종 전체 테스트 출력.

이 샘플은 다양한 실제 혜택 형식의 회귀 검증이며 전체 상품의 모든 절을 수작업 평가했다는 뜻은 아니다.

## 3. 명령과 결과

```sh
.venv/bin/ruff check packages/cardrag-core apps/cardrag-worker apps/cardrag-mcp tests/runtime_v1 tools
.venv/bin/ruff format --check packages/cardrag-core apps/cardrag-worker apps/cardrag-mcp tests/runtime_v1 tools
.venv/bin/mypy packages/cardrag-core/src apps/cardrag-worker/src apps/cardrag-mcp/src
.venv/bin/pytest -q packages/cardrag-core/tests apps/cardrag-worker/tests apps/cardrag-mcp/tests tests/runtime_v1
```

- Ruff check 성공, 220 files format 확인, mypy 106 source files 성공.
- 최종 전체 회귀 **2449 passed, 9 warnings, 66.84초**. warning9는 기존 OCR 충돌/명시적 fallback 테스트의 예상 경고다.
- 중간 관련 summary/partial 테스트 107건 성공 후 마지막 경계 테스트도 전체 suite에서 검증했다.
- `git diff --check` 성공.
- `gitleaks dir .handoff/009_product-summary-field-classification --no-banner --redact`: no leaks found.
- 운영 readiness 읽기 `http://127.0.0.1:18015/health/ready`: `{"ready":true}`. 실행 중 운영본을 수정본으로 교체했다는 뜻은 아니다.

실상품 재현 명령:

```sh
docker run --rm --network none --entrypoint python -i \
  -e PYTHONPATH=/workspace/apps/cardrag-mcp/src:/workspace/packages/cardrag-core/src \
  -v cardrag-mcp-v129-candidate-state:/var/lib/cardrag-mcp:ro \
  -v /home/lee/projects/MCP_card_prd_detail:/workspace:ro \
  cardrag-mcp:007-31edb1d - \
  < .handoff/009_product-summary-field-classification/evidence/fix01-readonly-samples.py
```

## 4. 마감 및 다음 역할

코드·회귀 테스트·새 증거·본 보고서를 검토 브랜치에 커밋하여 push한다. 수정 전 Reviewer 재현/기존 PLAN/REPORT/FIX_01은 변경하지 않는다. long Worker, Paddle 실행, 유료 실추론, 03시 대기 검증은 수행하지 않았다.

다음 작업은 Reviewer의 FIX_01 인수 검토다. FIX_01에서 지정한 대로 그 전에 main 병합/운영 교체/정식 릴리스는 진행하지 않는다.
