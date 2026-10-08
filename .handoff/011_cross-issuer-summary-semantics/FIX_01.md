# 011 FIX_01 — 연회비 보존과 부정 표 후보의 최종 차단

작성: 2026-10-08 Reviewer. 대상: branch codex/011-summary-semantics, HEAD7b35f07. 최신 기본 계획은 PLAN_REVISION_01.md다. 기존 PLAN/REPORT/증거를 덮지 않는다. 필수 보완은 아래 두 가지로 한정한다.

## 판정과 범위

범용 구조·4건 개선·新30개 검증은 인정한다. 모든 분류 오류0건을 요구하지 않는다. 다만 실제 원문보다 연회비를 적게 보여주는 회귀와 부정 표현을 제공 혜택으로 되살리는 경로는 수정 후 인수한다. 예시 일부/단독 금액/제목 표현/5개 슬롯 한계는 이번 필수 보완에 추가하지 않는다.

## A. 연회비 표에 혜택용 cell 분류를 적용하지 않기 [P1]

`summary_fields.py`의 expanded에서 모든 TABLE_ROW를 먼저 cell로 쪼갠다. 이후 fee_context에서는 쪼갠 금액이 같은 점수의 연회비 후보가 되고, 원래 row의 본인/등급/브랜드/총액 관계가 사라진다. `catalog._summary`는 점수 상위의 첫 후보를 선택한다.

실제 기존64개 source에서 재현:

- KB00917: headers 구분/등급/기본/제휴/합계(기본+제휴), row 국내전용/실버/3,000/10,000/13,000. 새 annual_fee_text=3,000원. 원래 총액13,000원을 포함한 row를 잃었다.
- 신한00549: headers 구분/총 연회비/기본 연회비/서비스 연회비, row MASTER/1만5천/5천/1만. 새 값1만원. 총액1만5천원이 필요하다.
- 신한00157: 본인 URS11만5천/아멕스12만 원래 row 대신 '기본연회비5천원이 면제됩니다'가 선택된다. 기본료 면제는 상품 총연회비 면제가 아니다.

최소 수정 방향:

- 이번 혜택·조건 개선의 전처리는 연회비 영역에 적용하지 않는다. node 자체/표 header/parent chain에서 연회비 영역을 확인해 원래 node 단위의 fee 판정·순위를 유지하는 것이 우선이다.
- 별도 fee classifier를 새로 만들거나 카드사/상품별 분기하지 않는다. 셀을 사용할 필요가 있다면 총액/브랜드/본인 대상 문맥과 source를 함께 보존해야 한다. 기본/제휴 일부만 총액처럼 반환하면 안 된다.
- before 문자열과 완전 동일한 포맷은 요구하지 않는다. 위 실제 총액 및 적용 대상이 보존되고 기본료 면제 설명이 총연회비를 대신하지 않아야 한다.
- 기존64개 before/after에서 annual_fee_text도 대조한다. 변한 건만 원문으로 확인하여 금액/대상 왜곡이 없는지 보고한다. 새로운64개를 다시 모으거나 재OCR할 필요가 없다.

## B. 부정/미제공 판정이 후속 table branch에서 뒤집히지 않기 [P2]

`benefit=False`로 처리한 '미적립' 등이 뒤의 column_role offer/unknown 재평가로 다시 True가 될 수 있다. label 제목 append 또한 unavailable 검사와 독립적이다.

공통 형식으로 재현:

1. headers 구분/혜택, cells 온라인/5% 포인트 미적립 → benefit '온라인 | 5% 포인트 미적립' 반환.
2. 같은 headers, cells 마일리지 적립/× → benefit heading '마일리지 적립' 반환.

수정 방향:

- 명백한 negative/unavailable/exclusion 판정은 모든 후보 경로의 최종 의미 제약으로 유지한다. 본문뿐 아니라 table label에서 만드는 혜택 제목도 같은 제약을 적용한다.
- 부정 표현을 키워드 하나로 전부 필드에서 삭제하는 방식은 금지한다. 제한은 조건에 남길 수 있고, '국내5% 할인(무이자할부 제외)' 같은 긍정 혜택+부분 예외는 보존한다.
- 카드사/상품코드/단일 실제 문자열 분기는 금지한다. 위 두 source fixture는 테스트용이며 범용 조건을 구현한다.

## 검증 / 완료

- `evidence/reviewer-repro.py`와 reviewer-repro-results.json이 재현 증거다. PYTHONPATH=apps/cardrag-mcp/src:packages/cardrag-core/src .venv/bin/python <script>로 실행 가능하다. production process/DB를 건드리지 않는다.
- 위 실제3건 fee 및 일반 표2건의 의미 회귀 테스트를 추가한다. 전체 혜택 제목/상세/조건뿐 아니라 annual_fee_text도 검증한다. 기존 신고4건, 긍정+예외, same-revision cache 검증은 유지한다.
- 기존 고정34개+무작위30개 source를 재사용하여 새 후보 결과를 산출하고 연회비/부정 제공/주요 혜택의 변화만 집중 대조한다. 파일과 보고서는 새 이름으로 작성하며 기존 증거를 덮지 않는다. 추가 random sampling은 이 두 수정으로 구체적 위험이 남을 때만 한다.
- 집중 테스트와 변경 MCP 전체 테스트, 변경 runtime Ruff/Mypy를 실행한다. Worker/PDF/OCR/embedding/LLM 호출·03시 대기·main 병합·운영 cutover는 불필요하다.
- FIX_01_REPORT.md 작성: commit, 실제 금액/제공 결과,64개 재사용 비교, 테스트와 잔여 한계. 두 오류가 해결되고 의미상 회귀가 없으면 인수 후 기존 generation으로 MCP 운영 반영할 수 있다.
