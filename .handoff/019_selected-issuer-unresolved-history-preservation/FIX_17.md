# 019 FIX_17 — BC 용량 준비 누락과 늦은 실패 진단 보완

2026-10-20 Reviewer: Codex. LIVE_REVIEW_05의 실제 실패 증거에 따라 발행한다. 현재 `019-fix16-4108e3dac5f5`에서 OCR122건은 성공했으며 원인은 `capacity_free_space`다. PLAN/PLAN_AMENDMENT_01·기존 REPORT·FIX_01–16/REPORT·REVIEW_06·OPERATIONS_REPORT_04와 현재 Git/운영 상태를 대조하고 본 미보고 FIX를 수행한다. 과거 문서는 보존하고 FIX_17_REPORT.md를 새로 작성한다.

## 1. [P1 운영] 필요한 작업 공간을 확보한 뒤 테스트 준비 완료를 판정

실제 embedding/export gate의 available64.651GiB, required68.892GiB, deficit4.240GiB다. 배포에서 새 복구본 약6.827GiB를 추가한 뒤 여유를 확인했지만 실제 gate와 다음 전처리 증가분까지 검증하지 않아 사용자가26분 후 다시 실패했다.

- 시작 전 current/직전 복구점 및 의존성을 다시 산출한다. 보호 대상은 새 fix16 snapshot, e7fb8a0ba60c 복구본 및 그 DB 의존 ba273106158e다. 구형017/019-4dba 복구본은 보호 집합 밖 후보이며 현재 조사한 allocated 합 약13.624GiB다.
- 정리가 필요하면 대상 경로·개별 크기·inode/link·활성 사용 여부·삭제 후 예상/실제 free를 구체화하고 기존 사용자 승인 범위에 맞게 처리한다. 이 FIX는 임의 전역 삭제 권한이 아니다. 삭제 권한이 불명확하면 **구체 후보가 완성된 상태**에서 필요한 조치만 확인한다. 현재 요청은 트러블슈팅 검토였으므로 Reviewer는 삭제하지 않았다.
- 최신 snapshot/직전 복구 의존성, 현재 serving/MCP 세대, cache/CAS, 실패 run을 임의로 지우지 않는다. global prune 금지. 단순히 free4.24GiB만 추가해서 충분하다고 보고하지 않는다. 현재 required와 관측된 전처리 증가 및 여유를 반영해 다음 실행 시작 시 free를 점검한다.
- 이번 보완을 배포할 때도 운영 DB가 변경됐는지·새 snapshot이 필요한지를 판단하고 추가 snapshot 증가분을 최종 준비 공간에 반영한다. 같은 배포를 위해 대형 복구본을 기계적으로 반복 생성하지 않는다. 보호된 파일 내용이 실제 동일할 때만 재사용한다.

## 2. [P2] 수동 실행 전 용량 점검과 실패 사유 표시

- 기존 run-issuer.sh/operations 흐름 안에 provider/origin 호출 이전의 용량 readiness 점검을 마련한다. 최소-start 2GiB와 실제 게시 gate를 구별한다. 직전 검증된 capacity observation 또는 명시된 운영 준비 예산, 해당 수치의 신선도/한계, 관측 전처리 증가와 현재 free를 표시한다. 부족한 것이 이미 알려진 상태에서 긴 수집/재사용을 실행하고 뒤늦게 실패시키지 않게 한다.
- 과거 성공 observation만으로 모든 미래 corpus의 정확한 peak를 안다고 주장하지 않는다. stale/없음·corpus 감소 등에는 근거가 드러나는 보수적 정책을 선택하고 문서화한다. 실제 v5 hard gate는 계속 유지한다. 단순 threshold/reserve 완화로 통과시키지 않는다.
- status/summary/check에서 capacity 실패와 required/available/deficit를 알아볼 수 있게 한다. 최종 분류 JSON에 run_id가 없는 이번 형태도 **해당 컨테이너/해당 run에 바인딩된 증거**로만 읽는다. 과거 성공/실패 payload를 현재 결과로 오인하지 않는다. 민감한 raw exception/설정/URL을 노출하지 않는다.

## 검증·인계

- 작은 fixture로 공간 부족 시 Worker/origin/provider 미기동, 경계값·관측값 없음/낡음, 실패 payload의 run 바인딩, failed run의 백업 실행 금지 등을 검증한다. shell 수정은 shellcheck, 제품 코드 변경 시 해당 회귀 및 요구 테스트/ruff를 실행한다.
- 보존된 BC 122 OCR 성공과 기존 8사5543/stable·비선택 보존, 백업 정책을 유지한다. 배포 후 실제 부족량과 free를 다시 대조한 뒤 복붙 명령을 제공한다. 사용자 수동 BC 실행 전에 다시 준비 완료라고 표시하지 않는다.
- 실제 BC 수집/OCR을 자동 재실행하거나 timer를 켜지 않는다. 새 실행은 기존 절차대로 사용자가 수행하며 결과를 후속 리뷰한다. 필요한 것이 국소 운영 보완이므로 019를 유지한다.
