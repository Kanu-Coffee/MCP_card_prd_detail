# 019 FIX_19 — metadata pair 선택과 cache 복구 쓰기 경계

2026-10-21 Reviewer: Codex. FIX_18 리뷰는 보완 필요다. REVIEW_07의 재현, FIX_18/REPORT, 이전 계획/변경계획·FIX/보고서·리뷰와 현재 Git/코드를 확인하고 본 가장 높은 미보고 FIX를 수행한다. 기존 문서는 보존하고 FIX_19_REPORT.md를 새로 작성한다. 019의 국소 보완을 유지한다.

## 1. [P2] 검증된 pair 단위로 후보를 선택

recover_verified_ocr_metadata는 variant manifest의 identity만 맞으면 탐색을 중단한 뒤 receipt/READY 검사를 한다. 부분 게시된 variant가 있으면 정상 canonical pair로 복구할 수 있는데도 partial로 끝난다.

- 각 후보의 manifest·READY·현재 seal identity·reuse-key/contract·current root receipt SHA/size를 하나의 단위로 검증한 후 선택한다. 무효 후보는 원격/receipt를 수정하지 않고 다음 적합 후보를 평가한다. scope가 실제 요구하는 canonical/variant 경로의 의미와 전체 pair linkage를 유지한다.
- canonical/variant의 manifest와 READY를 섞지 않는다. 한 후보가 손상/무receipt/부분 게시여도 다른 유효 pair가 정확한 현재 identity를 검증하면 정상 복구 가능해야 한다. 모든 후보가 무효이면 unbound/오류를 유지한다.
- 정상 canonical+불완전 variant(manifest만 존재, READY 손상, foreign/missing receipt), 정상 variant+무효 canonical, 모두 손상, 다른 output variant 조합을 검사한다. 실제 recover-metadata→status→flush CLI/factory 경로에서도 검증한다. 원격 쓰기0·정상 재업로드0·retry 멱등을 유지한다.

## 2. [P2] cache 경계와 기존 바이트를 보호하는 복구 쓰기

현재 write_bytes는 dangling file symlink 및 parent directory symlink를 따라 cache 밖으로 쓴다. 기존 local bytes의 읽기/쓰기 실패도 suppress하므로 존재 여부만으로 성공을 보고할 수 있다.

- cache root부터 key/variant 디렉터리 및 대상 파일까지 symlink/비정상 경계를 거부한다. 새 파일은 경계 안에 안전하게 생성하고, 검사 후 다른 writer가 바꾼 파일을 덮어쓰지 않게 한다. 기존 프로젝트의 no-follow/atomic-write/lock 방식을 우선 재사용한다.
- Worker/cache writer 및 동시 recovery와의 직렬화·충돌 의미를 명확히 한다. 정상 기존 canonical이 다른 variant이면 보존하고 적합 variant 디렉터리를 사용한다. 서로 다른 기존 바이트나 unreadable/부분 쓰기를 silently 성공으로 처리하지 않는다. 실제 최종 바이트 검증과 scope 재바인딩 결과를 보고한다.
- dangling file symlink, parent/key/variants symlink, 기존 일반 파일 충돌, permission/write failure·중단·동시 writer 조건에서 경계 밖 쓰기0·기존 바이트 불변·거짓 성공 금지를 검증한다. 작은 로컬 fixture로 재현하고 운영 파일에 symlink를 만들지 않는다.
- receipt/seal/pending을 조작하거나 provider/수집을 재실행하는 우회는 금지한다. 정상 경로의 GET-only 원격 계약도 유지한다.

## 검증·인계

기존 stale CAS scope 수정과 ownership/lost_source/receipt-only 보호를 유지한다. 관련 백업/CLI/경계 회귀 뒤 요구 Worker/Core 검사 및 ruff를 수행한다. 현재 정상 범위와 부정 경계의 실제 명령·결과를 새 보고서에 기록한다.

보고서의 운영 baseline은 이전5537이 아니라 LIVE_REVIEW_06의5545/우리980이며, 실제 운영 전 다시 식별한다. staging delta를 실제 운영 전체 completion으로 주장하지 않는다. Reviewer 통과 전 운영 배포·cache 복원·scoped flush·batch/timer 기동은 하지 않는다. 통과 후 기존 승인 범위에서 현대/BC의 required=bound 및 remote-index completion을 검증한다. 과거 handoff는 덮어쓰지 않는다.
