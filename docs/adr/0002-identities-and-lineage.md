# ADR-0002: 식별자와 lineage

- 상태: Accepted
- 일자: 2026-08-12

## 결정

issuer는 `woori`, `kb`, `shinhan` enum이며 document·evidence identity에서 생략할 수 없다.
최종 document identity는 issuer, 상품코드, 문서종류, 기준일, 원본 version 문자열과 PDF
SHA-256을 canonical serialization한 뒤 SHA-256으로 만든다. discovery 중 hash를 알기 전의
provisional identity는 최종 catalog identity로 사용하지 않는다. version 원문은 보존하고
`v9 < v10`을 보장하는 자연 정렬 key를 별도로 저장한다.

Evidence identity는 document identity, 문서 순서대로 정렬된 하나 이상의 page-local Unicode
codepoint `[start,end)` 범위·각 인용문 hash의 배열, 그리고 조립된 evidence text hash로 만든다.
단일 fact도 길이 1 배열이라는 같은 계약을 사용한다. 검색 순위나 generation은 ID 재료가
아니므로 재랭킹과 세대 교체 후에도 동일 근거가 안정적으로 식별된다. PDF/OCR/구조/embedding/index manifest에는 input/output hash,
schema·processor·config·prompt·provider·model·시각·attempt를 기록한다.

경로는 명시적 absolute root 아래에서만 resolve하고 symlink 후 root containment를 다시 검사한다.
사용자 입력 URL·파일 경로는 public API 계약에 포함하지 않는다.

## 결과

동일 상품코드를 쓰는 카드사가 충돌하지 않고, 같은 파일명의 내용 변경도 새 document로
기록된다. content-addressed object는 물리적으로 dedupe되지만 문서 record와 provenance는
각 source/version별로 유지된다.

## 검증

- `tests/unit/test_identity.py`
- `tests/unit/test_manifests.py`
- `tests/unit/test_storage.py`
- `tests/unit/test_download.py`
