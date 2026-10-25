# 019 FIX_27 — 매일 KST10시 자동 순회 예약 적용

2026-10-22 Codex. 사용자가 예약을 KST오전10시로 바꾸고 자동운영을 시작하도록 승인했다. 추가 질문에 **오늘10시 첫 예약 실행**을 선택했다. 지금 수동 Worker를 시작하여 오늘 slot을 소비하지 않는다. FIX_26 공간 정리 발행과 분리한다.

deploy/worker/cardrag-worker.timer의 OnCalendar/Description을10시로 바꾸고 운영 문서 현재 시각을 일치시킨다. 검증된 FIX_25 helper 기반 service와 round_robin pool/실제 운영 volume을 유지한다. DB cursor를 수작업 만들지 않으며 아직 자동 순회 기록이 없으므로 첫 정상 배정은 우리다. Persistent=false를 유지하고 root 권한으로 실제 timer 설치·daemon-reload·enable --now 후 오늘10시 next elapse를 확인한다. host 권한 획득이 안 되면 실제 적용 완료로 보고하지 않는다.

정적 unit/calendar 검사, 설치본/원본 비교, actual image/volume/round_robin 설정, Worker 미실행·운영 DB cursor 미소비를 확인하고 FIX_27_REPORT.md에 실제 적용 상태를 기록한다. timer 활성화는 즉시 Worker 실행과 구분한다. 오늘10시 실제 배치의 시작/결과는 런타임 증거로만 판정하며 미실행 시 성공을 미리 선언하지 않는다. 새로운 Worker image는 필요 없다.
