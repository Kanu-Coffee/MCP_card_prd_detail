# 019 REVIEW_11 — FIX_22 및 직접 보완한 FIX_23 검증 통과

2026-10-21 Codex, Reviewer. FIX_22/REPORT, LIVE_REVIEW_08, 기존 PLAN/변경계획/수정·리뷰 이력과 현재 Git/source를 대조했다. FIX_22의 variant 재구성 오류 및 own OCR 존재를 무조건 충돌로 보는 문제의 수정 방향을 확인했다. 추가로 원본 full manifest와 seal variant ID 대조 누락, discovery DB 진단의 strict document ID 충돌 두 건을 확인하여 FIX_23에 기록하고, 사용자가 명시 허용한 작은 수정 범위에서 직접 보완·검증했다. 이전 handoff/구현은 보존했다.

## 판단

**FIX_22와 FIX_23을 함께 검증 통과로 판단한다. 추가 미보고 FIX 없음.** FIX_22 단독을 그대로 배포하는 판단이 아니다. 본 턴의 Codex quota fallback은 현재 run의 contract/checkpoint/state schema를 변경하지 않는 국소 정책 예외로, 다음 resume 전에 적용 가능하다.

- full content manifest는 source/body/epoch/page hash/canonical bytes뿐 아니라 sealed content variant ID와도 일치해야 원본으로 복원한다. 다른 valid sibling은 원본으로 취급하지 않는다. 없거나 손상된 원본 metadata에 과거 variant ID를 덧붙이는 기존 오류를 되살리지 않았다.
- own OCR과 partner seal digest가 같으면 prior binding을 허용하고 실제 상이 바이트/비정상 leaf는 거부하는 FIX_22 경로를 확인했다. 입력이 달라져도 succeeded라고 무조건 건너뛰는 변경은 없다.
- stage/문서/reason을 제한된 구조로 진단하고 discovery의 non-doc ID에서2차 ValueError를 내지 않는다. 원시 provider 메시지는 공개하지 않는다.
- quota 예외는 codex-exec의 typed provider_process_rate_limited이며 fallback이 local-paddleocr일 때만 허용한다. 다른 systemic 오류는 차단하고 fallback 실패/취소는 전파한다. primary.partial chunks 보존과 fallback 전체 본문 분리를 실제 OCRResolver/가짜 provider의 회귀로 확인했다.

## 실제 검사

관련281 passed/9 warnings, 전체 Worker/Core **1808 passed/10 warnings**, ruff check 및 format158 files, diff check 통과. 신규12개 fixture와 실제 코드/변경 diff를 확인했다. 상세 명령은 FIX_23_REPORT에 기록했다. 테스트 대역을 실제 모델 추론·운영 완주의 증거로 쓰지 않는다.

이번에는 사용자 요청에 따라 Reviewer가 작은 보완 구현도 직접 수행했으며 별도 Executor 세션이 작성한 변경인 것처럼 기술하지 않는다. 큰 provider 전환 정책 재설계·무한 재시도·인증 실패 fallback은 도입하지 않았다.

## 후속 운영 경계

현재 run49845932e05849d3a0110a2bc82d6c1c는 failed 상태이며 완료330/707·checkpoint97을 보존해야 한다. 이번 사용자 요청의 "다음 resume 적용"에 맞춰 검증 소스로 식별 가능한 새 image/release를 준비·반영한다. current source 일치, 최신 복구점·용량·worker/backup lock과 단일 독립 복구본 정책을 지킨다. 실제 반영 증거는 OPERATIONS_REPORT_08에 기록한다. 롯데 재개·Paddle 실제 추론·백업/게시 완주·신한 batch·timer/019 인수는 이번 검토 통과만으로 선언하지 않는다.
