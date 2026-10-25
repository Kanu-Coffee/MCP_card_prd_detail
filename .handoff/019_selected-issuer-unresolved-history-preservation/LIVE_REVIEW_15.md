# 019 LIVE_REVIEW_15 — 14:30 우리 원본 서비스 오류의 의도된 재시도 종료 확인

2026-10-22 14:37 이후 Reviewer. 사용자는 직접 우리 홈페이지에서도 다운로드 불가를 관찰했고, 의도된 오류 종료라면 추가 조치 없이 내일 국민 배치를 보겠다고 지시했다. 외부 원본 재호출·재실행·설정/코드/DB 변경은 하지 않았다.

- timer14:30 발화 후 정상 컨테이너 기동 및 provider startup 통과. 13시203/EXEC 권한 오류는 재발하지 않았다.
- run5241db5c48c84c59a2d640b8e58c8fb6, 우리만 selected, 922건 discovery 성공. HTTP200이나 PDF 대신 text/html visible_service_error_page를 수신했다.
- 실패 source_c363707aef7be75165e225bf96369ac88cd7784f3393116411535400083a3c0f의 운영 stage를 readonly mode=ro로 확인: download failed, attempt_count=4/max_attempts=4. 최초1회+재시도3회 소진 후 issuer_origin_service_error로 종료한 것이 맞다. issuer-collection attempts=5는 카드사 단위 합산이므로 실패 PDF 단독5회로 오해하지 않는다. performance는 pdf 요청7회, catalog2회 기록이다.
- 14:30:42 종료, CLI는 단일 실패 JSON, status=failed/reason_code=issuer_origin_service_error, error_class_category=issuer_origin, stage=download, status_code=200, mime_type=text/html, exit1. 실패를 정상으로 숨기거나 오류 페이지를PDF로 게시하지 않는 의도된 동작이다.
- PDF 캐시24건 확보, 이후898건은 해당 카드사 다운로드 격리로 skipped. 다른7사는 not_scheduled다. OCR/신규 serving 게시는 진행하지 않았고 stable은 g-666c2d2ee9e24d04acf76eed-d6ffd9a927e9 그대로다.
- 최신 상태는 WAL을 포함해 검증했다. 최초 immutable 조회는 아직 체크포인트되지 않은 attempt result=null을 보였으나 WAL 포함 독립 읽기/readonly 조회에서 run=failed 및2회차 result failed 저장 확인. 이를 미완료 작업으로 판정하거나 DB를 보정하지 않았다.
- 자동순회 오늘2회차 실패 이력 보존, 남은 queue 선두kb. timer는 active/enabled, Persistent=no, next2026-10-23 14:30 KST. 실패 service 상태는 남겨두어도 다음 timer 실행을 막지 않는다.

사용자 지시대로 우리카드 측 일시적인 다운로드 서비스 오류로 취급하고 그대로 둔다. 추가 retry/flush/reset/우회/신규FIX/운영변경을 하지 않는다. 내일10/23 14:30 국민 배치를 확인한다. 우리 origin 복구 시점은 확정하지 않으며 다음주 확인은 필요 시 별도 수행한다.
