# 019 FIX_12 — generation-only OCR variant 백업과 기존 유효 index 보존

2026-10-20 Reviewer: Codex. 사용자 수동 백업 테스트 후 LIVE_REVIEW_02의 실환경 근거에 따라 발행한다. 기존 PLAN/PLAN_AMENDMENT_01, FIX_01–11/각 REPORT, REVIEW_02 및 운영 자료/현재 Git을 기준으로 한다. HEAD `aaeb2c4` 위 미커밋 변경과 기존 문서는 보존한다.

## [P1] 동일 native key의 정상 OCR variant를 canonical cache 경로 하나로 백업하려고 함

`verified_document_ocr_bundles`/등록/scope는 generation-only 결과의 native manifest/READY도 `v1/ocr-cache/native/{prefix}/{reuse_key}/...`에 매핑한다. 그러나 native key는 PDF/계약에 묶여 있고 OCR 출력까지 유일하게 구별하지 않는다. 현재 seal이 참조하는 정상 local OCR과 이미 원격 cache에 보존된 정상 OCR이 다르면 두 variant 모두 유효해도 경로가 충돌한다.

실제 key `94cdb1…73352f`의 source/contract는 같지만 output/page hashes가 다르며, 현재 seal은 local variant를 참조한다. 원격 pair는 receipt와 일치하고 자체 정합성도 정상이다. 단순 파일 손상·잘못된 receipt로 처리할 수 없다. 로컬 생성 시각도 10월14일로 이번 pending2건과 별개인 기존 variant다. 상세 hash 및 읽기 전용 대조는 LIVE_REVIEW_02에 있다.

### 수정 요구

1. 기존 canonical native cache의 immutable variant를 유지하면서 현재 generation-only variant의 검증 manifest/READY도 백업·복원 가능하게 한다. OCR 본문 CAS는 현재 seal SHA를 유지한다. hash에 묶인 최소한의 별도 metadata 보존 경로 등 기존 backup 계약 내 가장 작은 방법을 선택하고, cache identity와 backup artifact identity를 구분한다. 운영 key/문서/카드사 예외 하드코딩은 금지한다.
2. recorder/scope/expected/receipt/index/restore가 같은 variant identity를 사용하게 한다. null seal reuse key, legacy canonical backup, 정상 cache reuse 및 재개/retention 이후 복구를 유지한다. optional seal을 허위 key로 채우거나 과거 immutable seal을 rewrite하지 않는다.
3. 같은 key의 다른 정상 remote variant는 정상 cache 공존 사례로 분류하되, 실제 바이트 손상/잘못된 PDF·계약·output·READY/hash/root 충돌 차단은 완화하지 않는다. 현재 variant metadata를 정상 정책상 제외로 바꿔 완료시키지 않는다. 원격 metadata overwrite/receipt SQL 삭제/과거 OCR로 serving 교체/수집·provider 재실행은 해결책이 아니다.

## [P1] expected와 다르다는 이유만으로 기존 유효 variant의 index 항목을 제거함

현재 flush의 index 필터는 local expected와 SHA가 다른 경로를 제거한다. 이번 사용자 flush는 다른 pending2건을 처리하면서 기존 정상 canonical pair 두 항목을 backup index에서 제외했다. 원격 객체/receipt는 남아 있으나 index 기반 복원 가시성이 약화됐다.

- 검증된 기존 variant와 새 variant가 모두 복원 가능한 index/receipt 근거를 보존한다. 충돌을 숨기기 위해 기존 항목을 삭제하거나 현재 expected를 기존 항목으로 둔갑시키지 않는다.
- 실제 손상/검증 불가능한 receipt는 기존 안전 중단·진단 경계를 유지한다. 정상 variant 공존과 손상 충돌을 증거 기반으로 구별한다.
- 이번 이미 index에서 빠진 두 기존 항목도 정식 검증·재시도 경로에서 회수 가능하게 한다. 수작업 원격 index 편집/허위 receipt는 금지한다.

## 인수·검증 및 운영 경계

- 실환경 형태 fixture: source/contract/native key가 같은 두 canonical manifest/READY, 서로 다른 OCR output, remote pair A/receipt/index 존재, seal은 local B(null key) 참조. 선택 백업 후 A는 유지되고 B body+metadata도 복원 가능하며 선택 완료가 증거 기반으로 판정돼야 한다.
- 새 unrelated pending을 함께 처리해도 기존 A index가 사라지지 않게 한다. A index가 이미 없는 상태의 정식 회수, 중단/index commit 실패/멱등 재시도/restore, 비선택 scope 보존을 검사한다.
- source/contract/READY 불일치·바이트 손상·동일 artifact identity의 다른 바이트·root 변경은 여전히 차단한다. 이전 FIX의 bound/unbound/missing/순서 독립·정상 정책 보류 회귀를 유지한다.
- 전체 Worker suite, ruff, diff 검증 및 실제 운영 pair에 적용 가능한 읽기 전용 대조/복구 명령을 `FIX_12_REPORT.md`에 기록한다. 로컬 mock 검증과 운영 검증을 구분한다.
- 이번 correction은 코드 구현/회귀 범위다. 실제 배포·원격 쓰기/flush·운영 원장 수정·배치/timer 기동은 수행하지 않는다. 비선택 7사/현재 serving5537/우리980, hash/root/lock/원자성/재개/GC pin 계약을 보존한다.
