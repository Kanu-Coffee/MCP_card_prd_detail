# 020 종료 — Paddle 임시 입력 복구 및 운영 인수

2026-10-28 사용자 종료 지시에 따라 020을 **CLOSED**로 처리한다. 기존 PLAN, 보고서, 리뷰와 운영 증거는 이력으로 보존한다. 새 plan이나 correction은 이 종료 기록에서 만들지 않는다.

## 인수 근거

- 수정 구현은 `REPORT.md`, 보완 사항은 `FIX_01_REPORT.md`, 코드 인수와 회귀 검증은 `REVIEW_01.md`에 기록했다. 인수된 저장소 코드의 전체 관련 테스트는 1,970 passed이며 실제 배포 이미지 조합은 1,969 passed, 버전 메타데이터 불일치 1 failed였다(`OPERATIONS_REPORT_01.md`).
- 문제의 기존 stale alias 1건을 정리하고 원본 CAS의 크기·hash 보존, recovery dry-run 잔존 0건, startup-check 통과와 운영 release 전환을 확인했다. snapshot과 적용 과정의 최초 오류 및 마무리는 `OPERATIONS_REPORT_02.md`, `OPERATIONS_REPORT_03.md`에 기록했다.
- 실패한 하나카드 run `9fa3b10def034fa8b65029e6df2d29ae`은 명시 resume 후 08:54 KST exit 0, `succeeded`, OCR 745/745, 재사용 742건, provider 호출 문서 3건으로 완료됐다. 게시 generation과 실제 MCP generation 일치 및 하나카드 범위 백업 완료는 `OPERATIONS_REPORT_06.md`에 기록했다.
- 10:00 KST 예약 롯데 run `007ba6966e6b4395b5564afb0c5614fc`은 13:25 KST systemd exit 0, `succeeded`, local generation `g-007ba6966e6b4395b5564afb-94b74bf0ef7b` 게시. OCR 725/725 성공, 재사용 707건, provider 호출 문서 18건이다. MCP가 해당 generation을 제공하고 페이지 조회 2건이 성공했음을 확인했다. 실제 TELLO NE 문서는 Codex primary 처리 후 PaddleOCR-VL-1.6 fallback 산출물로 완주했다. 이는 수정 이미지에서 실제 fallback 성공의 관측이며 과거 child가 종료된 원인을 증명하지는 않는다.
- 롯데 실행은 `collection_status=degraded`(원본 404의 캐시 사용 경고 및 PDF 1건 건너뜀), `quality_status=warning`(반복 임베딩 유사도 경고), `backup_status=degraded`를 기록했다. 선택 백업 `backup_scope_complete=true`이고 범위 내 잔여·누락·충돌·소실·미바인딩은 0, 범위 밖 기존 대기 4건 300,444 bytes다. 읽기 전용 재조회에서는 `backup_scope_complete=null`이며 원격 완료를 새로 검증한 값이 아니다. 경고를 성공으로 바꿔 기록하지 않는다.
- timer는 활성이고 다음 예약은 2026-10-29 10:00 KST다. 이후 예약 결과나 자동 8일 전수 관측은 주장하지 않는다.

## 인수 시 한계와 다음 계획의 맥락

- 최초 Paddle child의 종료 이유와 primary OCR fallback의 세부 원인은 당시 보존된 진단만으로 확정할 수 없다. primary 경고는 원인 분류를 남기지 않아 troubleshooting이 어렵다. 사용자는 이 provider 진단·로그 개선을 다음 021 계획 주제로 지정했다. 021 계획의 범위·보안·검증 기준은 별도 계획 작성 시 조사하고 정의한다.
- 020 코드와 이 handoff는 현재 Git 작업 트리의 미커밋 변경이다. 운영에는 기존 이미지에 Worker 6개 파일을 overlay한 이미지가 적용돼 있으며 GitHub 소스 릴리스나 공개 이미지를 이 작업으로 새로 발행하지 않았다. 소스 추적·재현을 위한 commit/PR·CI/릴리스 판단은 별도 작업이다.
- 각 비선택 카드사의 보존 및 미해결 이력 전체를 이번 종료 기록에서 새로 독립 재검증했다고 주장하지 않는다. 배포·실행 당시의 증거 범위는 각 운영 보고서와 Worker 결과를 따른다.
