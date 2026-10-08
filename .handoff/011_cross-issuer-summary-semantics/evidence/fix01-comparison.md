# FIX_01 Evaluation Comparison across 64 Products

## 1. Reviewer Reproduction Verification

| Case | Issuer / Code | Baseline Before | Prev After (584f4ac) | FIX_01 After | Status |
|---|---|---|---|---|---|
| 연회비 총액 복원 | kb 00917 | `| 국내전용 | 실버 | 3,000원 | 10,000원 | 13,000원 |` | `3,000원` | `| 국내전용 | 실버 | 3,000원 | 10,000원 | 13,000원 |` | **RESOLVED** |
| 연회비 총액 복원 | shinhan 00549 | `| MASTER | 1만 5천원 | 5천원 | 1만원 |` | `1만원` | `| MASTER | 1만 5천원 | 5천원 | 1만원 |` | **RESOLVED** |
| 연회비 총액 복원 | shinhan 00157 | `| 본인 | 11만 5천원 | 12만원 |` | `기본연회비 5천원 면제 footnote` | `| 본인 | 11만 5천원 | 12만원 |` | **RESOLVED** |
| 부정 표 후보 차단 | `| 온라인 | 5% 포인트 미적립 |` | N/A | `benefit: '온라인 | 5% 포인트 미적립'` | `condition: '온라인 | 5% 포인트 미적립'` | **RESOLVED** |
| 부정 표 후보 차단 | `| 마일리지 적립 | × |` | N/A | `benefit heading: '마일리지 적립'` | `None (Blocked)` | **RESOLVED** |

## 2. 64 Products Annual Fee Preservation Analysis

- 총 검증 상품: 64개 (고정 34개 + 무작위 30개)
- 기준치(Baseline Before)와 완전 일치: 61개
- 단순 포맷팅 차이 (`<br>` 태그 공백 정규화): 3개
- 실제 금액 또는 적용 대상 왜곡: **0개**

### 포맷팅 정규화 3건 상세
- **bc p-8c233ba2f2376879af558bd4c61f0c2d5e870b25ae4edb2c9b6fecb695a4f64a--v-e3b0c44298fc1c14**:
  - Baseline: `| 연회비 | 국내외겸용(Mastercard): 10,000원<br>(기본연회비 10,000원 + 제휴연회비 0원)<br><br>국내전용(BC): 5,000원<br>(기본연회비 5,000원 + 제휴연회비 0원) |`
  - FIX_01: `| 연회비 | 국내외겸용(Mastercard): 10,000원 (기본연회비 10,000원 + 제휴연회비 0원) 국내전용(BC): 5,000원 (기본연회비 5,000원 + 제휴연회비 0원) |`
- **bc p-0efa4f17aa3b6f8e6bd8476d1104bedf1bd46bc01291f5e32e77abf3a4a3a858--v-e3b0c44298fc1c14**:
  - Baseline: `| 연회비 | 10,000원<br>/ 제휴 1,500원 |`
  - FIX_01: `| 연회비 | 10,000원 / 제휴 1,500원 |`
- **hana 13725**:
  - Baseline: `| 연회비 | 5천원<br>기본연회비 5,000원<br>제휴연회비 0원 | 1만원<br>기본연회비 10,000원<br>제휴연회비 0원 |`
  - FIX_01: `| 연회비 | 5천원 기본연회비 5,000원 제휴연회비 0원 | 1만원 기본연회비 10,000원 제휴연회비 0원 |`

## 3. Benefits and Conditions Metrics

| 지표 | Baseline Before | Prev After (584f4ac) | FIX_01 After |
|---|---|---|---|
| 총 혜택 상세 수 (Benefit Texts) | 254 | 207 | 207 |
| 총 조건 상세 수 (Condition Texts) | 312 | 299 | 299 |
