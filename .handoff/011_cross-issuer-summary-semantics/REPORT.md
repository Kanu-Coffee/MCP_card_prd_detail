# 011 REPORT — 범용 상품 요약 분류 구현

작성: 2026-10-08 Executor. 실행 기준: PLAN_REVISION_01.md. 구현 commit **584f4ac**, branch `codex/011-summary-semantics`. **구현·읽기 전용 검증 완료, Reviewer 인수와 운영 반영은 아직 수행하지 않음.**

## 1. 구현 내용

- `summary_fields.py`: classifier v4. 문장과 source qualifier를 구분해 같은 혜택 문장 전체를 조건에도 복제하지 않는다. 혼합 원문은 혜택에 안전하게 보존하고 조건에는 입증되는 실적/횟수/대상/제외 조각을 선택한다.
- 표는 기존 cells/headers, 부모 표, 대소문자 정규화, 빈 병합 헤더를 활용해 label/offer/condition 역할로 처리한다. 혜택율과 서비스명을 연결하고 조건/실적 셀을 분리한다. 잘못된 metadata는 원래 display_text 경로로 처리한다.
- `catalog.py`의 요약 DB 조회에서 **table_cells_json을 읽지 않던 누락**을 고쳤다. DB에 정보가 이미 있었으므로 Worker/parser/serving generation 변경 없이 해결했다.
- 천/만원 청구할인, 원당 마일, 리터당 금액 등 공통 제공 표현을 인식한다. parent 및 앞선 동일 section의 제외 목록을 반영한다. 미적립/미제공/×를 긍정 혜택으로 만들지 않는다. 일반 고지와 예시/계산식을 제외한다.
- 후보 역할/정보량으로 순서를 정한다. 실제 상세가 있으면 제목 복제를 상세에서 제외하지만, 본문 없이 구체적 제공 내용이 제목에만 있는 경우는 원문 제목을 fallback으로 허용한다.5개/길이 제한을 유지한다.
- source fragment의 실제 excerpt/node/pages/revision을 보존한다. 공개 API 모델·연회비·출시일·identity/alias/contract bundle 변경 없음. classifier version을 cache key에 반영해 같은 revision에서도 구버전 캐시를 재사용하지 않는다.
- runtime classifier에는 카드사/issuer/상품코드/특정 node·revision 분기가 없다. 대표 상품 이름/코드는 fixture·검증·보고서에만 사용했다.

## 2. 신고4건 확인

| 요청 | 개선 결과 |
|---|---|
| 신한01208 | 공항10~15%/동반30% 할인은 혜택에 유지,1일1회/동반2인/성인 조건을 조각으로 선택. 혜택 문장 전체의 조건 반복 제거. |
| KB00917 | 마일리지 제목 확보. 아시아나1,000원당1마일 제공과 일시불/할부·무이자 제외의 확인사항 셀 분리. |
| BC BD001 | 요청 alias는 유지. 제목만 반복하던 상세 대신 실제 페이북1천원 청구할인 문장 선택. 결제1만원 이상/월1회 조건 분리. canonical code를 BD001로 바꾸지 않음. |
| KB09063 | 서비스별60원/ℓ 및10% 혜택과 실적 구간/금액 분리. `[예시: ...]` 제목 제거. |

실제 CatalogRepository._summary 및 source node/pages를 사용하는4건 회귀 테스트를 추가했다. 출력 문자열/순서를 특정 상품에 고정하는 production 규칙은 추가하지 않았다.

## 3. 검증 결과와 표본

- 최종 MCP 전체 테스트 **878 passed /20.42s**, 최종 집중 테스트 **113 passed /0.79s**. 마지막 검사에서 발견된 정규식 선언 줄 길이는 의미 변경 없이 줄바꿈으로 정리했다. 검증 SHA256은 실행 당시 선언 형식을 가리킨다. 전체 suite 결과는 `evidence/final-mcp-suite.txt`, 최종 focused 결과는 `evidence/final-focused-tests.txt`에 기록한다. focused: **113 passed / 0.79s**. 이전 중간 전체 suite도877 passed /20.32s(`mcp-tests.txt`). 마지막 원당 마일/제목 fallback 보완 뒤 최종 전체 suite를 다시 확인했다.
- 변경 runtime2개 및 tests3개 Ruff 통과, runtime2개 strict Mypy 통과. `git diff --check` 통과. staged Gitleaks 약16.44MB 검사에서 secret 발견 없음.
- 문장/표 혼합, JSON fallback/HEADER 대소문자, source evidence, 예시/계산식, 제외 목록, title fallback 보존, 기존 다사 혜택 회귀를 확인했다. 캐시 테스트는 같은 generation/revision에서 classifier version 변경 시 cache miss/recompute를 검증한다.
- 기존34개 + 신규 층화 무작위30개 = **64개**의 동일 generation/revision에 대해 운영 v3와 후보 v4 catalog를 비교했다. seed20261008, 카드사별2개부터 뽑고 나머지 무작위 추출. 기존34개를 제외했고 결과의 성공/실패를 보고 표본을 바꾸지 않았다. 전체 모집단 수·요청 identifier/canonical code/revision은 random30-manifest.json에 있다.
- 새30개 분포: BC2, 하나5, 현대2, KB5, 롯데4, 삼성3, 신한6, 우리3. 사용자 원래 무작위30개 목록은 제공되지 않았으므로 그 목록을 재검증했다고 주장하지 않는다.
- 직접 운영 DB를 읽기 전용/immutable로 조회하고, 별도 exec Python 프로세스의 메모리에 후보 모듈을 적재했다. 실제 MCP 서버 프로세스/이미지/파일·타이머는 바꾸지 않았다. OCR/embedding/LLM/vector load/전량 Worker 호출 없음.
- 같은 혜택 문장 전체가 조건에도 실리는 상품: **31 → 0**. 제목 빈 상품:7 →6. 혜택 상세 빈 상품:4 →6(잘못된 제외/한도 혜택 제거 결과도 포함). 이 수치는 분류 정확도나 전체 신규 상품 품질 보장이 아니다.
- 선택된 source evidence766개를 해당 node 원문과 정규화 대응 검사하여 **불일치0**. source 대응과 의미상 적절성은 다른 기준이며 전자를 후자의 완벽한 증거로 해석하지 않는다.
- 원문과 후보 출력을 함께 검토하여 네 신고 유형 및 추가로 발견된 제외/미제공/예시/제목 fallback 문제를 공통 규칙으로 보정했다. 동일 무작위 표본으로 보완 결과를 재확인했다. 표본을 새로 뽑아 오류를 숨기지 않았다.

## 4. 잔여 한계 / 인수 판단 자료

완벽한 요약을 목표로 특례를 늘리지 않았다. 다음은 실제 확인한 잔여 한계다:

- 하나11420의 거래 예시 표 일부가 혜택 상세에 남는다. 실제50% 혜택은 유지되지만 예시 금액이 추가 슬롯을 차지할 수 있다. 모든 예시의 구조 표현이 명시적인 label을 갖는 것은 아니다.
- KB01507/롯데 렌탈 등 일부 표는 원문의 실적별 할인 금액이 단독 금액처럼 간결하게 반환된다. 조건 셀과 evidence는 보존하나 전체 열 관계를 사용자에게 충분히 설명하지 못할 수 있다.
- 신한01199는 제외 목록 무이자를 혜택에서 제거했으나 번호형 할인 예시 제목/한도 중심 표현이 일부 남는다. 하나03622에는 계산 관련 단독 금액 조건이 남는다.
- 일부 상품은 전체5개 슬롯 때문에 조건 또는 다양한 서비스가 생략되거나, source가 목록/공통 안내 중심이라 제목·구체적 상세가 빈 값이다. 원문에 없는 제목이나 혜택을 만들어 채우지 않았다.
- 한 문장에 제한과 제공이 섞이면 혜택의 원문에는 제한도 남을 수 있다. 조건에는 분리된 제한이 표현될 수 있으나 문장 전체 중복과 구분한다.

Reviewer는 위 잔여 문제의 실무 영향과 공통성으로 판단한다. 특정 카드사/상품 맞춤 수정 또는 모든 표본 오류0건을 자동 인수 조건으로 제안하지 않는다. 제한된 표본의 결과를 모든 신규 카드상품의 정확도 보장으로 표현하지 않는다.

## 5. 증거 읽기와 운영 후속

- 빠른 검토: `validation-metrics.json`, `random30-manifest.json`, `product-comparison.md`의 첫 상품별 요약표 및 신고4건.
- 정밀 검토: readonly-comparison.json에64개 원문 nodes/운영 before/후보 after/후보 source SHA256 포함. 파일이 크므로 전체를 대화에 출력하지 말고 필요한 issuer/revision/node만 선택해서 읽는다. `product-fixtures.json`은 기존34개 source 재현용이다.
- 재현: `.venv/bin/python .handoff/011_cross-issuer-summary-semantics/evidence/evaluate-current.py`. 읽기 전용 결과를 `/tmp/cardrag011-evaluation.json`에 만든다. 당시 원문 검증에는 저장된 generation/revision fixture를 사용하고, 현재 generation이 다르면 동일 원문이라고 혼동하지 않는다.
- 운영은 `/opt/cardrag/009-b544a80`, readiness=true, timer active 그대로다. 인수 후 같은 generation을 사용하는 새 MCP 이미지만 교체할 수 있다. Worker를2시간반 실행하거나 재OCR할 이유가 없다. 배포 시 LibreChat 공통 이름/네트워크를 유지하고 boot DB 검사 시간은 최대15분 허용한다.
- 새 공개 Release/main 병합/운영 cutover는 이 Executor 구현 단계에서 수행하지 않았다. 사용자 Excel3개도 그대로 보존했다.
