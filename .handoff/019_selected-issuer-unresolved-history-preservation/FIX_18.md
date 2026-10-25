# 019 FIX_18 — 6사 중간점검의 과거 OCR CAS 오탐과 BC native pair 검증 회수

2026-10-20 Reviewer: Codex. LIVE_REVIEW_06, 이전 계획/변경계획·FIX/REPORT와 현재 Git/운영 상태를 읽고 수행한다. FIX_17은 보고서가 있으며 이번에는 새로 발견된 백업 범위 보완을 다룬다. 기존 문서는 보존하고 FIX_18_REPORT.md를 작성한다. 019를 유지하며 proxy/전체 GC 재설계는 하지 않는다.

## 1. [P2] 과거 OCR CAS를 현재 필수 백업으로 넣는 오탐

현재 현대 run `10f236331a2744f78980f2b878475b64` unbound3은 과거 OCR 객체다. BC run `8810e405d952441faf9e560ac19fd7b0` unbound5 중1도 같은 문제다. 해당 현재 문서는 content이며 실제 현재 OCR 바이트가 현재 seal과 일치하고 old/current PDF는 같으나 OCR SHA는 다르다. 옛 doc_map/doc_info가 남아 있다는 이유로 옛 CAS가 required/unbound가 된다.

- 현재 seal의 PDF/OCR CAS refs 및 검증된 현재 바이트와 과거 OCR 객체를 구분한다. content에 native metadata가 없다는 정상 상태 때문에 과거 CAS를 현재 필수로 주장하지 않는다. doc_info 문자열만으로 현재 byte evidence 권위를 부여하지 않는다.
- 현재 다른 문서나 현재 검증된 bundle/ownership 기록이 요구하는 CAS는 보호한다. 보존된 다른 세대의 객체와 receipt는 그대로 유지하고 **이번 selected scope만** 정확하게 계산한다. pending/lost_source·ownership·root·동일 PDF의 다른 OCR variant 회귀를 유지한다. 현재 항목까지 blanket 제외하지 않는다.
- 실제 현대3건, BC1건의 현재/과거 identity와 scope 변화를 RO로 제시한다. 숫자를 맞추기 위한 receipt/doc_map/원격 객체 삭제 금지.

## 2. [P2] BC native manifest/READY 4건의 검증 근거 회수

LIVE_REVIEW_06의 두 key에 대해 원격 canonical pair는 존재하며 현재 seal source/output identity와 원장 receipt SHA/size에 일치했다. 로컬 cache 근거가 없어 현재 scope가 unbound4로 남는다.

- 기존 정식 restore/audit/CLI 경로 중 최소한의 검증 회수 방식을 사용한다. canonical manifest/READY, reuse-key/contract/source, 현재 PDF/OCR size/hash/page, root-bound receipt 및 실제 바이트를 검증한 뒤 로컬 증거를 복원하거나 명시적 검증 결과로 scope를 재바인딩한다. receipt 존재나 doc_info만으로 True 처리하지 않는다.
- 정확히 이 두 pair만 필요하며 새 수집/OCR/provider 호출·원격 canonical 덮어쓰기·과거 seal 수정·운영 SQL 조작은 금지한다. 재시도 시 이미 일치하는 body/metadata를 불필요하게 재업로드하지 않는다. 원격 pair가 없거나 손상/불일치하면 완료를 막는다.
- 두 key 및 예상 manifest/READY SHA는 LIVE_REVIEW_06에 있고, 증거는 operations/live/mid-review-20261020에 보존했다. 추가 카드사 실행으로 원래 run이 retention 정리될 수 있으므로 live 상태를 다시 식별한다. 이미 정리된 run을 ad-hoc 재작성하지 말고 현재 보존된 검증 세대의 정식 범위/복구 계약을 사용한다.

## 검증과 운영 인계

두 경우의 최소 fixture 및 실제 backup status/flush CLI 경로를 검증한다. 현재 required CAS 손상·다른 문서의 참조·retained generation 보호·lost_source/receipt-only·native metadata 실제 소실 회귀를 유지한다. 관련/전체 Worker/Core 검사와 ruff를 수행하고 기록한다.

운영 RO 사전 대조 후 Reviewer 통과 시 기존 승인 범위에서 새 release와 필요한 검증 회수를 준비한다. 현대/BC의 bound scope와 remote index completion을 따로 확인한다. 단순 pending0/deferred/null로 백업 완료를 판정하지 않는다. 신규 origin/provider batch를 자동 실행하거나 신한/롯데 프록시를 바꾸지 않는다. 신한/롯데 홈페이지200은 기본 연결 확인이고 실제 두 배치 완주 검증은 아직 남아 있다.
