# 011 FIX_01_REPORT — 연회비 보존 및 부정 표 후보 최종 차단 구현 보고서

- **작업 일시**: 2026-10-08
- **담당자**: Executor (Antigravity)
- **대상 브랜치**: `codex/011-summary-semantics`
- **참조 계획**: `.handoff/011_cross-issuer-summary-semantics/FIX_01.md` 및 `PLAN_REVISION_01.md`
- **상태**: 완료 (P1 연회비 총액 보존, P2 부정 표 후보 최종 차단, 회귀 테스트 추가 및 64개 상품 전수 대조 완료)

---

## 1. 개요 및 Reviewer 지침 반영 요약

Reviewer의 `FIX_01.md`에서 발행된 핵심 보완 요구사항 2건(P1 연회비 표 보존, P2 부정/미제공 표 후보의 최종 차단)을 카드사/상품별 하드코딩 없이 범용 구조로 구현하였습니다:

1. **[P1] 연회비 표의 node 단위 보존**: `_is_fee_node`를 통해 연회비 표 및 연회비 문맥의 `TABLE_ROW`는 혜택용 셀 쪼개기(`_table_fragments`) 대상에서 제외하여 원래 행 단위(구분/등급/기본/제휴/합계)의 완전한 텍스트와 순위를 보존하였습니다. 또한 기본료 면제 안내 조항이 총연회비 행을 대신하지 않도록 가중치를 정비하였습니다.
2. **[P2] 부정/미제공 후보의 최종 의미 제약 강제**: `_NEGATED_OFFER`에 `미적립`을 포함하고, 셀·행 단위 `_is_cell_unavailable` 검사를 추가하여 미제공(`×`, `미제공`, `미적립`) 표 후보가 후속 `column_role` 분기에서 `benefit=True`로 번복되지 않도록 최종 의미 제약(`not is_negated`)을 모든 경로에 강제하였습니다. 레이블 기반 혜택 제목 생성 시에도 실제 제공되는 혜택 셀이 존재할 때만 제목으로 등록되도록 차단하였습니다.
3. **증거 및 검증**:
   - `evidence/reviewer-repro.py` 실행 결과: 5건(연회비 3건, 부정 후보 2건) 모두 `RESOLVED` 확인 (`evidence/fix01-repro-results.json`).
   - 64개 상품(고정 34 + 무작위 30) 전수 재비교: 연회비 왜곡 0건, 단순 `<br>` 공백 정규화 3건을 제외한 61건 기준치와 100% 일치 (`evidence/fix01-comparison.md`, `evidence/fix01-comparison.json`).
   - MCP 단위/통합 테스트: 883/883건 전체 통과, Ruff 및 Mypy 100% 통과.

---

## 2. 세부 구현 내용

### A. 연회비 표에 혜택용 cell 분류 배제 및 총액 행 보존 [P1]

#### 1) 문제 원인
`summary_fields.py`의 `summary_candidates` 전처리 단계에서 모든 `TABLE_ROW`를 일괄적으로 `_table_fragments`를 통해 개별 셀 단위로 분할하여, KB00917의 합계 13,000원 대신 기본 3,000원 셀이 선택되거나 신한00549의 총 15,000원 대신 서비스 10,000원 셀이 선택되는 회귀가 발생했습니다. 또한 신한00157에서는 분할된 연회비 셀보다 '본인' 키워드가 포함된 기본연회비 5천원 면제 footnote가 높은 점수를 받는 문제가 있었습니다.

#### 2) 수정 내용
- **`_is_fee_node` 헬퍼 구현**: 대상 노드의 `display_text`, `table_headers_json`, 그리고 부모 체인(ancestors)의 `raw_heading`, `table_headers_json`, `display_text`에 `'연회비'`가 포함되어 있는지 범용적으로 판별합니다.
- **분할 배제**: `expanded` 리스트 생성 시 `_is_fee_node`에 해당하는 `TABLE_ROW`는 `_table_fragments` 분할을 건너뛰고 원래 노드(`[dict(node)]`) 상태를 그대로 유지합니다.
- **면제 안내 조항 가중치 감점**: `기본\s*연회비.{0,15}면제` 또는 `추가\s*발급\s*시`와 같은 면제 조건 안내 footnote는 연회비 후보 점수를 5점 감점하여 실제 상품 총연회비 행보다 우선 선택되지 않도록 방어했습니다.

```python
def _is_fee_node(node: Mapping[str, Any], by_id: Mapping[str, Mapping[str, Any]]) -> bool:
    if str(node.get("node_type")) != "TABLE_ROW":
        return False
    text = str(node.get("display_text") or "")
    headers = str(node.get("table_headers_json") or "")
    if "연회비" in headers or "연회비" in text:
        return True
    parent = node.get("parent_id")
    visited = {str(node.get("node_id"))}
    while parent and str(parent) in by_id and str(parent) not in visited and len(visited) < 64:
        visited.add(str(parent))
        anc = by_id[str(parent)]
        if (
            "연회비" in str(anc.get("raw_heading") or "")
            or "연회비" in str(anc.get("table_headers_json") or "")
            or "연회비" in str(anc.get("display_text") or "")
        ):
            return True
        parent = anc.get("parent_id")
    return False
```

---

### B. 부정/미제공 판정의 최종 의미 제약 유지 [P2]

#### 1) 문제 원인
1. `_NEGATED_OFFER` 정규식에 `'미적립'`이 누락되어 `'5% 포인트 미적립'`이 부정 표현으로 인식되지 못했습니다.
2. `benefit = False`로 판정된 노드가 후속 `column_role == "offer"` 블록의 `benefit = benefit or (...)` 구문에서 다시 `True`로 뒤집히는 제어 흐름 오류가 있었습니다.
3. `column_role == "label"` 블록에서 형제 셀(`siblings`)에 혜택 키워드가 존재하기만 하면 `SummaryCandidate(..., heading=True)`를 발행하여, 혜택 값이 `×`인 마일리지 적립 행의 레이블이 혜택 제목으로 잘못 추출되었습니다.

#### 2) 수정 내용
- **`_NEGATED_OFFER` 보강**: `미적립` 패턴 추가.
- **`_is_cell_unavailable` 함수 추가**: `[×xX\-―ㅡ_]|미제공|불가|제외` 및 실질적인 긍정 혜택이 남지 않는 부정 오퍼(`미적립`, `미제공`, `제공하지 않음`)를 판별.
- **행/셀 단위 `_unavailable` 플래그 설정**: 표의 오퍼 열 전체가 미제공이거나 해당 오퍼 셀이 미제공일 때 `fragment["_unavailable"] = True` 설정.
- **최종 부정 제약 `is_negated` 선언**: `is_negated`를 정의하고 `benefit` 평가 시 및 `benefit` 후보 추가 시 `and not is_negated`를 최종 제약으로 강제. `is_negated`인 항목은 혜택에서 영구 배제되며 조건(`restricted = True`)으로만 수용 가능.
- **레이블 제목 가드**: `column_role == "label"`에서 `not node.get("_unavailable")`을 검사하고, 오퍼 열 형제 셀 중 실제로 제공되는(`not _is_cell_unavailable`) 유효 혜택 셀이 존재할 때만 혜택 제목으로 발행.

```python
is_negated = bool(
    node.get("_unavailable")
    or negated_offer_only
    or re.search(r"미적립|미제공|제공되지|제공하지|면제되지|제공\s*불가", text)
    or re.search(r"\[예시\]|^예시|계산\s*예|연체\s*시|유이자|할부금리|연체이자", text)
)
benefit = (
    ((bool(_BENEFIT.search(text)) and concrete) or table_offer or offer_label)
    and not restriction_only
    and not exclusion_context
    and not is_negated
)
if is_negated:
    benefit = False
    if re.search(r"미적립|미제공|제공되지|제공하지|면제되지|제외", text):
        restricted = True
```

---

## 3. 검증 결과 대조

### 3.1 Reviewer 재현 케이스 검증 (`reviewer-repro.py`)

| 케이스 구분 | 대상 식별자 | Baseline Before | Prev After (584f4ac) | FIX_01 After | 결과 |
|---|---|---|---|---|---|
| **P1 연회비 총액** | KB 00917 | `\| 국내전용 \| 실버 \| 3,000원 \| 10,000원 \| 13,000원 \|` | `3,000원` | `\| 국내전용 \| 실버 \| 3,000원 \| 10,000원 \| 13,000원 \|` | **정상 복원** |
| **P1 연회비 총액** | 신한 00549 | `\| MASTER \| 1만 5천원 \| 5천원 \| 1만원 \|` | `1만원` | `\| MASTER \| 1만 5천원 \| 5천원 \| 1만원 \|` | **정상 복원** |
| **P1 연회비 총액** | 신한 00157 | `\| 본인 \| 11만 5천원 \| 12만원 \|` | `기본연회비 5천원 면제 footnote` | `\| 본인 \| 11만 5천원 \| 12만원 \|` | **정상 복원** |
| **P2 부정 표 차단** | `\| 온라인 \| 5% 포인트 미적립 \|` | N/A | `benefit: '온라인 \| 5% 포인트 미적립'` | `condition: '온라인 \| 5% 포인트 미적립'` | **혜택 차단, 조건 수용** |
| **P2 부정 표 차단** | `\| 마일리지 적립 \| × \|` | N/A | `benefit heading: '마일리지 적립'` | `None` (완전 차단) | **혜택 제목 차단** |

### 3.2 64개 상품 전수 재비교 (`evidence/fix01-comparison.json`)

- **검증 상품군**: 기존 고정 34개 + 무작위 30개 = 총 64개 상품
- **연회비 텍스트 일치율**:
  - Baseline Before와 100% 완전 일치: **61개**
  - 단순 공백 정규화 (`<br>` 태그를 단일 공백으로 치환): **3개**
    - `bc_p-8c233...`: 마스터 10,000원 / BC 5,000원 내용 완전 보존
    - `bc_p-0efa...`: 10,000원 / 제휴 1,500원 내용 완전 보존
    - `hana_13725`: 5천원 / 1만원 기본/제휴 내역 완전 보존
  - **금액 축소, 등급/브랜드/총액 누락, 안내 조항 오선택**: **0건**
- **기존 혜택 개선 유지**:
  - 신한 00447: 거래 예시 제외 및 0.7% 적립 정상 유지
  - 신한 00942: 거래 예시 제외 및 0.3% 적립 정상 유지
  - KB 09570: 업종별 할인 표 분리 추출 정상 유지
  - 롯데 1506: 월 50만원 예시 제외 정상 유지
- **긍정 혜택 + 부분 예외 보존**:
  - `| 쇼핑 | 국내 5% 할인(무이자할부 제외) |`: `5% 할인` 혜택 유지 및 `무이자할부 제외` 조건 동시 보존 확인

---

## 4. 테스트 및 품질 지표

1. **의미 회귀 전용 테스트 (`apps/cardrag-mcp/tests/test_summary_semantics.py`)**:
   - `test_fee_preservation_for_three_reviewer_cases` (KB00917, 신한00549, 신한00157 연회비 총액 검증) 추가
   - `test_negative_table_offers_blocked_from_benefit_headings_and_details` (미적립 조건화, × 혜택 제목 차단) 추가
   - `test_positive_offer_with_partial_exception_preserves_both` (부분 예외와 긍정 혜택 공존 검증) 추가
   - 결과: **16/16 통과** (0.49s)
2. **MCP 전체 회귀 테스트**:
   - `PYTHONPATH=apps/cardrag-mcp/src:packages/cardrag-core/src .venv/bin/pytest apps/cardrag-mcp/tests`
   - 결과: **883/883 통과** (20.80s)
3. **정적 검사 및 타입 체크**:
   - `ruff check apps/cardrag-mcp packages/cardrag-core`: All checks passed!
   - `ruff format --check apps/cardrag-mcp packages/cardrag-core`: 93 files already formatted
   - `mypy apps/cardrag-mcp/src packages/cardrag-core/src`: Success: no issues found in 51 source files
4. **운영 영향도**:
   - 실제 운영 프로세스, SQLite DB, WebDAV generation 변경 없음 (무영향).
   - Worker 재실행, 신규 OCR, 외부 LLM 호출 없음.

---

## 5. 결론 및 잔여 사항

- Reviewer가 FIX_01에서 요구한 필수 보완 2건(P1, P2)이 모두 충족되었으며, 64개 상품 비교 및 883개 회귀 테스트를 통해 안정성이 입증되었습니다.
- Reviewer 승인 후, 기존 generation 데이터를 그대로 유지한 상태에서 MCP 컨테이너의 소스 갱신을 통해 운영 반영(cutover)을 진행할 수 있습니다.
