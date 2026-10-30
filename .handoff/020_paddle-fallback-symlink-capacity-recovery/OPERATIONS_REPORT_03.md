# 020 운영 반영 및 예약 복구 확인

2026-10-28 06:16 KST 운영 점검. 사용자가 `finish-020-paddle-20261028.sh`를 sudo로 실행했다. 링크 정리와 선행 오류는 `OPERATIONS_REPORT_02.md`에 기록돼 있다.

- active release `/opt/cardrag/020-paddle-20261028`, image `cardrag-worker:020-paddle-2bfed5c195e3`, image ID `sha256:943becf993a6cc8348e7a25526b5b5d6cce7a54385542bf8f6be4970965df93a`를 확인했다.
- cardrag 서비스 계정 recovery dry-run은 `status=ok`, 잔존 alias 0건. 서비스 계정 startup-check는 `status=ready`, `stage=completed`, `quality_status=passed`다. 적용 환경은 `round_robin`, `codex-exec`/`gpt-6.1-terra`/`high`, 폴백 `local-paddleocr`/`PaddleOCR-VL-1.6`이다.
- 새 보호 snapshot `020-paddle-20261028`은 별도 verifier에서 두 DB quick_check 및 SHA 통과. stable generation은 전환 전과 같은 `g-75e5a7f5c75e44b581bdf81d-6b81ea507cc4`; MCP `/health/ready`는 `ready=true`다.
- timer는 `enabled`, `active`, 단일 `OnCalendar=*-*-* 10:00:00 Asia/Seoul`, `Persistent=no`, 다음 실행 `2026-10-28 10:00:00 KST`다. service는 `inactive/dead`, `Result=success`이며 이는 `reset-failed` 이후 대기 상태이지 오늘 배치 성공이 아니다. 수동 배치는 시작하지 않았다.
- 마무리 스크립트의 마지막 `systemctl show`가 사용자 터미널에서 `less` pager를 열어 대기 중인 것을 확인했다. 예약 검증과 enable은 이미 완료됐고 사용자에게 `q`로 pager를 종료하도록 안내했다. 스크립트 최종 종료 코드 자체는 이 문서 작성 시점에는 아직 확인하지 않았다.

10:00 이후 journal 및 Worker terminal 결과, 새 generation·MCP·해당 범위 backup을 확인해야 운영 완주를 판정할 수 있다. startup-check와 timer 활성화만으로 실제 OCR 요청·폴백·배치 완주를 보증하지 않는다. 코드 변경은 현재 미커밋이며 GitHub 소스 릴리스는 별도 작업이다.
