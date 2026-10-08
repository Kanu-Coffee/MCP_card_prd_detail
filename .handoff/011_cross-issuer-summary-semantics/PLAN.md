# 011 — 카드사 공통 상품 요약의 문장·표 셀 분류 개선

작성: 2026-10-08 Planner. 상태: **계획 발행, 구현 전**.

## 1. 목표와 범위

`get_product_summary`의 benefit_headings / benefit_summary_texts / condition_summary_texts가 카드사별 약관 형식에 상관없이 의미에 맞게 분류되도록 개선한다. 사용자가 신고한4건을 수정하고, 8개 카드사에 걸친 고정 표본으로 회귀를 확인한다. 혜택 문장 전체의 조건 필드 중복, 제목만 있는 혜택 요약, 조건 표의 혜택 혼입, 예시 제목의 혜택 제목 혼입을 제거한다. 단순히 조건이나 표를 모두 빼서 빈 결과로 만드는 해결은 불허한다.

주 변경은 MCP의 분류·후보 추출·선택 로직이다. 기존 serving DB/OCR/embedding/PDF와 공개 API schema를 재사용한다. 새 LLM 분류기·카드사별 상품코드 hardcode·전량 Worker·유료 추론·재OCR·재임베딩·새 공개 Release는 필요하지 않다. 운영 전환은 구현과 검증 후 Reviewer 인수를 받아 별도 단계로 진행한다. Planner 단계에서는 실제 운영 코드/설정을 변경하지 않는다.

## 2. 현재 상태와 자료 — 이 문서와 이 폴더로 작업 가능

- 저장소 `/home/lee/projects/MCP_card_prd_detail`, main50cd7d7 기준.009 runtime 구현은 b544a80이며010에서 운영 반영했다. 구현 착수 시 Git/status를 다시 확인하고 `codex/011-summary-semantics` 브랜치를 사용한다.
- `/opt/cardrag/current` → `/opt/cardrag/009-b544a80`, 실제 MCP container/project `cardrag-mcp`, image `cardrag-mcp:009-b544a80`, readiness `http://127.0.0.1:18015/health/ready`, MCP state `cardrag-mcp-state`.
- Worker state/auth/model: cardrag-worker-state / cardrag-worker-auth / cardrag-worker-paddleocr-models. 현재 OCR opencode / alibaba-token-plan/qwen3.8-flash / medium. 타이머 매일03시. **이번 구현을 위해 건드리지 않는다.**
- 현재 generation: `g-eba5ca0d13924abdb1f36937-71a5fd98d58b`. 컨테이너 DB는 `/var/lib/cardrag-mcp/generations/<generation_id>/index.sqlite3`, pointer `/var/lib/cardrag-mcp/current.json`.
- 루트의 사용자 Excel3개는 수정/삭제/커밋하지 않는다. 토큰·secret/full environment·운영 journal도 출력/커밋하지 않는다.
- `evidence/reported-four-baseline.json`: 운영 MCP의 실제 CatalogRepository를 읽기 전용 DB로 실행한4건의 현재 summary, 전체 structure nodes/table cells/headers, source spans.
- `evidence/collect-reported-four.py`: 위 수집기의 재현 코드. `docker exec -i cardrag-mcp python < ...`로 읽기 전용 실행하며 Worker/vector/LLM을 호출하지 않는다. stderr와 생성 JSON은 별도 파일로 저장한다. 기존 증거를 덮지 않는다.
- `evidence/product-fixtures.json`: 기존009의8개 카드사30개 고정 표본에 신고4건을 합친 **34개 고유 revision**의 structure nodes. 외부 handoff 파일을 다시 읽을 필요가 없다.
- `evidence/sample-manifest.json`: 요청 issuer/identifier/revision 및 표본 출처. 기존30개는 우리9개+나머지7사 각3개로 뽑은 표본이며 **사용자가 새로 무작위 확인한30개와 동일하다고 주장하지 않는다.**
- 사용자 원래30개 목록/결과 파일을 요청한 상태다. 제공되면 이를 별도 고정 manifest에 추가하고 전수 재검증한다. 미제공이면 신고4건+이 폴더34개를 검증하고 REPORT에 원래30개 동일 표본 재확인은 아직 불가능했다고 명시한다. 목록이 없다는 이유로 구현을 멈추지 않는다.

주 파일:

| 파일 | 역할 |
|---|---|
| apps/cardrag-mcp/src/cardrag_mcp/summary_fields.py | 후보 추출/정규식/문맥 판정, SUMMARY_CLASSIFIER_VERSION |
| apps/cardrag-mcp/src/cardrag_mcp/catalog.py | CatalogRepository.summaries/_summary, 캐시, 5개 제한, evidence |
| apps/cardrag-mcp/src/cardrag_mcp/models.py | ProductSummary/SummaryEvidence 공개 모델, 읽고 호환 유지 |
| apps/cardrag-mcp/tests/test_summary_fields.py | 기존 semantic/실상품 fixture 회귀 |
| apps/cardrag-mcp/tests/ | 실제 catalog 결과/증거/캐시 검증 추가 위치 |

기존 REPORT/PLAN은 역사 자료이며 덮어쓰지 않는다. 작업 완료는 이 task의 새 REPORT.md에 기록한다.

## 3. 확인된 원인과 반드시 고칠 사례

Planner가 실제 운영 코드와 읽기 전용 DB에서4건을 재현했다. 이 결과는 추측이나 사용자 문구만으로 만든 fixture가 아니다.

| 상품 요청 | 실제 관찰 / 원인 | 수용 결과 |
|---|---|---|
| 신한01208 | 공항 할인 두 문장이 혜택과 조건에 동일하게 실린다. `_CONDITION`이 1일1회/동반2인까지 등을 잡으면 `summary_candidates`가 원문 전체를 benefit와 condition에 둘 다 append한다. 첫 조건 슬롯에는 일반 소비자 고지/기간 문장도 들어간다. | 공항 할인은 혜택에 유지. 조건에는 1일1회, 동반2인/성인 요금 등 실제 제한만 근거와 함께 분리한다. 같은 할인 문장 전체를 반복하지 않는다. 일반 고지가 핵심 조건을 밀어내지 않는다. |
| KB00917 | 혜택 제목이 비고 마일리지 표 한 행 전체가 양쪽에 반복된다. 표의 `구분/내용/확인사항` 셀이 DB에 이미 구분돼 있으나 classifier는 display_text 전체로 판정한다. `● 마일리지 서비스(아시아나카드만 해당)`의 끝부분 때문에 offer_label도 실패한다. | 원문의 `마일리지 적립` 등 정당한 제목 확보. 내용 셀의 아시아나항공1,000원당1마일 제공은 혜택, 확인사항 셀의 일시불/할부 및 무이자 제외는 조건에 분리. 원문의 대상 카드 제한을 보존. |
| BC BD001(Be Digital) | 제목 `1천원 할인`과 같은 텍스트의 자식 PARAGRAPH가 혜택 요약으로 선택된다. 실제 상세 문장은 조건에만 있다. concrete 패턴은 `천원`, `청구할인`을 제대로 인식하지 못한다. | 제목은 제목 필드, 페이북/쇼핑몰1천원 청구할인은 실제 혜택 내용에 포함. 결제1만원 이상/월1회/전월 실적 관계없음/제외 항목은 조건에 표현. 제목만으로 혜택 상세가 충족됐다고 판정하지 않는다. |
| KB09063 | 전월 구간 보조 헤더가 BODY라서 TABLE_ROW offer로 통과한다. 부모 header에 할인율이 있으면 금액 행을 benefit로 폭넓게 인정한다. `[예시: ...]`는 `[예시]` 또는 `^예시` 패턴을 피해 제목/조건에 들어간다. | 보조 구간 헤더/전월 실적 금액은 조건 문맥에 배치. 주유60원/ℓ, 통신/마트/교통10% 등 실제 혜택률은 보존. 예시 계산 문장은 제목/실제 혜택 요약/핵심 조건에서 배제. |

**BC 식별 주의:** BD001은 정상 조회되는 요청 identifier이지만 반환 product_code는 `p-8774e6f41277e4177bd29fcbdd0714656e1e457e557ca66fca79f2e0126211a2--v-e3b0c44298fc1c14`다. alias 해석은 정상 동작이므로 코드를 바꾸거나 `summary.product_code == 'BD001'` 검증을 쓰지 않는다. manifest에 요청 identifier와 반환 code/revision을 별도로 기록한다.

## 4. 구현 지침

### 4.1 분류 단위를 문장 전체에서 근거 있는 조각으로 확장

현재 SummaryCandidate는 node_id/text/field/heading/score다. 공개 schema를 바꾸지 않고 내부에 필요한 조각 역할·문맥·원문 위치/셀 정보를 추가할 수 있다. 다음 순서로 후보를 만든다:

1. 구조/원문 정규화: heading marker, bullet, HTML br, 표 cell 및 parent chain. 원문-정규화 대응을 보존한다. 잘못된 JSON/누락 cell/순환 parent는 안전한 display_text fallback으로 처리한다.
2. 고지·참고·예시·연회비 영역 구별. 기존 연회비/출시일 보정을 회귀시키지 않는다. 예시/계산 가정 표기 `[예시:]`, `【예시】`, `(예시)` 등은 실제 원문에서 확인되는 표현을 포함해 처리한다. 단어 일부를 포함한다는 이유로 정상 상품명을 통째로 배제하지 않는다.
3. 혜택 제공 내용, eligibility/실적/한도/횟수/제외, 실제 혜택 제목을 각각 후보화한다. 사용 가능한 source cell/문장 조각을 우선한다.
4. 후보의 역할·정보량·서비스 다양성에 따라 선택하고 최종5개 제한과 길이 제한을 적용한다. 제목 복제/표 보조 헤더/일반 고지가 앞에 나온다고 핵심 내용의 슬롯을 차지하지 않게 한다.

같은 node에 혜택과 제한이 함께 있는 것은 정상이다. **node_id가 같다는 이유로 한 필드 후보를 버리지 않는다.** 역할별 source 조각을 분리한다. 같은 혜택 문장 전체의 양쪽 반복은 금지하되 원문상의 동일 제한이 복합 혜택 문장 안에도 남는 것은 무조건 오류로 보지 않는다. 분리가 불가능하면 원문 전체를 혜택에 안전하게 보존하고 조건에는 별도 입증 가능한 제한만 넣거나 생략 사유를 기록한다. 중요한 부정/제외/대상 조건을 잘라내 의미를 바꾸는 축약은 금지한다. 임의 생성한 요약 문장을 넣지 않는다.

### 4.2 표를 열 역할과 계층으로 읽기

`table_cells_json`, `table_headers_json`, table_role, TABLE parent, 인접 보조 헤더를 사용한다. headers/roles의 대소문자는 정규화한다. parser가 BODY라고 기록한 행도 값 패턴과 parent header를 함께 봐서 보조 헤더인지 판정한다.

- 구분/서비스명 → 제목 후보; 내용/혜택/할인율/적립률 → 제공 내용; 확인사항/조건/제외/실적/이용금액 한도 → 조건.
- 실제 offer rate/단위가 있는 셀과 실적 구간/한도 금액 셀을 한 행 안에서도 분리한다. 할인율 열과 금액 열이 같은 표에 있다는 이유로 행 전체를 혜택으로 인정하지 않는다.
- 다중 헤더/빈 셀/colspan 흔적, `<br>` 조건 여러 줄, `%`만 있는 셀, `원/ℓ` 및 `원당 마일` 등을 다룬다.
- 표의 실제 혜택을 모두 삭제하는 방식은 불허한다. 부모 문맥만으로 긍정 혜택을 만들어 내지 않는다.
- 우선 MCP에서 기존 table metadata로 해결한다. Worker parser 변경은 실제 source에서 필요한 정보가 없다는 증거가 있을 때만 검토하며, 그 경우 새 generation/부분 실행 필요성을 REPORT에 명시한다. 현재4건은 필요한 cell/header가 DB에 있으므로 재수집/재OCR 사유가 없다.

### 4.3 제공 표현과 제목 보강

- 숫자 단위: 천/만, 원·마일·포인트·리터당, 청구할인·적립·제공·면제 등 긍정 제공을 해석한다. 1천원/월한도1천원/1만원이상결제의 의미를 구분한다.
- 대상/횟수/부정 표현을 함께 처리한다. `서비스 제외`, `적립 대상 아님`은 긍정 제공으로 만들지 않는다. 마일리지/보험/무료 서비스는 퍼센트가 없다고 빠뜨리지 않는다.
- MAJOR_SECTION/ITEM뿐 아니라 source table label이나 혜택을 입증하는 상위 heading도 제목 후보로 사용할 수 있다. 괄호 대상 조건이 붙었다고 유효 제목 전체를 버리지 않는다. 부모 제목을 재사용할 때 node/page evidence는 그 제목의 실제 source를 가리킨다.
- 제목이 존재하고 근거가 있는 신고 상품은 빈 제목을 해소한다. 원문에 유효 제목/명칭이 없는 모든 상품에 억지 제목을 만드는 요구는 아니다.
- source label만 반복한 PARAGRAPH는 상세 benefit로 취급하지 않는다. 공통 notice heading/example는 정규화 후 제외한다.

### 4.4 선택·캐시·증거

CatalogRepository._summary의 현재 field별 문자열 중복 제거는 field 내부만 처리한다. 구조 후보와 내용 후보를 구분하고 정규화 중복/정보량을 반영해 선택한다. 필드 간 단순 문자열 삭제로만 해결하지 않는다.

- 각 필드 최대5개와 현재 공개 응답 구조 유지. 의미 단위를180자에서 단순 자르지 않는다. 여러 source 조각을 쓰면 각 조각의 근거를 남기고 어떤 제한을 생략했는지 검토한다.
- SummaryEvidence의 node_id/pages/excerpt/contract_revision_id를 source와 일치시킨다. table cell의 HTML/whitespace 정규화는 source 대응을 검증한다. 출력의 혜택·조건·제목이 어느 source fragment인지 테스트한다.
- `SUMMARY_CLASSIFIER_VERSION`을 현재 `cardrag.product-summary.v3`에서 새 버전으로 올리고 catalog cache key가 이를 사용함을 검증한다. generation/revision 그대로일 때도 예전 요약 캐시를 재사용하지 않는다.
- 현재 launch_date/annual_fee/status/identity/aliases/contract_bundle 동작은 회귀 대상으로 유지한다.

## 5. 표본 검증 — 수정 후 동일 원문과 비교

### 5.1 착수 시 기준선과 정답표

이 폴더의34개 fixture를 읽고 모든 상품별 혜택 제목/실제 제공/조건의 주요 source를 검토해 `evidence/expected-classification.json` 또는 이에 준하는 표를 새로 작성한다. 정답은 수정 코드 출력에서 역으로 만들지 말고 원문 node/cell 및 필요시 같은 revision의 get_contract_bundle에서 확인한다.4건은 위 수용 결과를 필수로 넣는다. 나머지도 금액/율/대상/제외/한도를 점검한다.

실제 현재 catalog baseline을34개 요청으로 다시 수집해 before 파일을 만든다. 같은 generation/revision에서 local 후보 구현을 적용한 after를 비교한다. API 별칭은 요청 identifier와 canonical code를 구별한다. 원래 사용자30개 목록이 도착하면 이를 별도 primary panel로 고정하고 정확히 같은 목록을 재검증한다. 기존34개는 추가 회귀 panel로 유지한다. 표본이 바뀌었다면 변경 이유와 수를 따로 기록한다.

### 5.2 단위/통합 테스트

기존 tests를 유지하되 '혼합 문장 전체가 benefit와 condition에 둘 다 있어야 성공'이라는 기존 테스트 기대는 새 계약에 맞게 변경한다. 대신 혜택과 **분리된 실제 제한**이 둘 다 보존되는지 검증한다. 불리한 테스트를 삭제만 하거나 expected를 현재 출력 그대로 교체하지 않는다.

필수 테스트 범주:

-4건 실 source fixture 회귀, KB table cells 역할, 굿데이 보조 헤더/예시, BC 천원청구할인/조건 분리.
- 범용 문장형/표형/목록형, HEADER/BODY 대소문자·누락·다단헤더·빈 셀·잘못된 JSON·순환 parent.
- 혜택의 횟수/한도/대상/제외 보존, 부정 및 연회비/출시·고지 영역 회귀.
- 표 cell과 상위 heading에서 제목 확보, 제목 복제 제거, 실제 제목이 없는 경우의 근거 있는 빈 값.
- `_summary`의 최종5개 선택, 180자 초과·단위/부정 잘림 방지, source evidence와 version cache 무효화.
- 기존500107/104022/104023/하나15911/신한00368 회귀 포함.

예시 실행(실제 테스트 파일명에 맞춘다):

```sh
.venv/bin/pytest apps/cardrag-mcp/tests/test_summary_fields.py -q
.venv/bin/pytest apps/cardrag-mcp/tests -q
```

영향이 MCP에 한정되면 Worker 전체 테스트나 전량 배치를 반복하지 않는다. Ruff/타입 검사는 저장소 기존 실행 방식을 따르고 변경 파일을 우선한다.

### 5.3 표본 수용 기준과 보고

34개 고정 revision 모두 before/after 및 원문 근거 검토를 수행하고 카드사별 결과를 작성한다. primary 사용자30개가 제공되면 그30개도 전수 확인한다. 자동 문자열 중복률만으로 분류 품질을 합격시키지 않는다.

수용 조건:

1. 신고4건의 구체적 문제 전부 해소; 주요 제공 내용/율/단위/실적/횟수/제외가 다른 필드에서 유실되지 않는다.
2. 표본의 잘못된 필드 배치, 제목만 있는 상세, 예시/보조 헤더 혼입, 같은 혜택 전체의 조건 중복을 원문 기준으로0건까지 수정한다. 발견 건을 제외/대체해 합격시키지 않는다.
3. 유효 제목이 원문에 있는 표본에서는 제목을 제공한다. 진짜 source 부재/손상은 node/source 증거와 영향, 우회 여부를 공개하고 Reviewer가 실무 영향으로 판단한다.
4. 공개 schema/identity/annual_fee/launch_date/기존 정상 혜택 회귀 없음, evidence 정확성, 새 cache version 확인.
5. REPORT에 각 상품 요청/code/revision, 세 필드 before/after, source node/cell, 실제 오류 유형과 개선 여부를 표로 기록. 생성 fixture/tests 통과와 실제 운영 반영을 구분한다. 원래 사용자30개 미제공 시 '동일30개 재검증 완료'라고 쓰지 않는다.

## 6. 단계와 완료 보고

1. Git/status/해당 instructions 확인 → baseline/정답표 고정.
2. source fragment / table role / heading / 숫자 표현 개선 → candidate selection/evidence/cache 반영.
3.4건 + 범용·회귀 테스트 →34개 및 제공된 primary30개 수동 원문 대조 → 남은 오분류 수정.
4. REPORT.md 작성: commit/변경 파일/검증 명령과 수/상품별 비교/원래 표본 확보 여부/회귀/운영 반영 필요 사항. 기존 handoff 문서는 변경하지 않는다.
5. Reviewer 인수 후 MCP 중심 운영 반영을 계획한다. 기존 generation을 쓰는 코드 변경이면 MCP만 교체하면 되고 Worker를2시간반 돌릴 이유가 없다. 향후 부분 Worker 실행이 정말 필요하면 기존 skip/reuse 옵션으로 호환 source run을 확인해 최소 stage만 실행하며 실제 launch 후 장시간 모니터링 턴을 유지하지 않는다.

원래30개 목록 부재는 표본 식별의 한계이며 코드 구현의 blocker가 아니다. 추가 PDF/LLM/OCR를 쓰기 전에 현재 자료로 해결할 수 없는 구체적 사유를 먼저 제시한다. 인수 조건은 실제 필드 품질과 근거이며03시2회·2일 대기·형식적 추가 검증을 새 gate로 만들지 않는다.
