# 021 운영 전환 결과

2026-10-28 15:46 KST 기준 운영 전환을 완료했다. 사용자가 로컬 터미널에서 `apply-021-ocr-logging.sh`를 실행했으며, 종료 후 아래 상태를 별도로 확인했다.

- 활성 release: `/opt/cardrag/021-ocr-logging-20261029`. 정규 서비스는 `/opt/cardrag/current/operations/worker-compose.sh`를 사용하며, 유효 compose 이미지는 `cardrag-worker:021-ocr-log-d01f2d908acc`, 이미지 ID는 `sha256:983b2196fb1f4b75d7f15c93248602e4c0d0ed50c944d9a7d471dc35b1234f92`다.
- 서비스 계정 시작 검사 결과는 `operations/live/021-startup-check.json`: `status=ready`, `stage=completed`, `quality_status=passed`. 주 OCR은 `codex-exec` / `gpt-6.1-terra`, fallback은 `local-paddleocr` / `PaddleOCR-VL-1.6`이다. 기존 설정 대조 결과 reasoning은 `high`다.
- `cardrag-worker.timer`는 active/enabled, 매일 10:00 Asia/Seoul, `Persistent=no`. 다음 실행은 2026-10-29 10:00 KST다. worker 서비스는 현재 inactive이고 전환 중 수동 배치는 실행하지 않았다. 전환 전 잠금을 확보한 rotation 조회에서 남은 대상은 `["bc"]`이므로 다음 대상은 BC카드다.
- 새 복구본 `021-ocr-logging-20261029`은 해시와 SQLite `quick_check`를 통과했다. worker DB SHA-256 `529a556e253b17881b56d5c2d987405535634752995222c7bebc99638b981566`, backup ledger SHA-256 `4a7289d941e974750474e118d2e21345c744f4253eb42c0a326ef3380899fde7`. 기존 020 복구본은 새 복구본 검증 이후 정리했고, 다른 release에서 참조하는 019 복구본은 보존했다. DB 복원은 수행하지 않았다.
- 읽기 전용 live MCP 검사는 정상이며 serving generation이 worker stable generation `g-007ba6966e6b4395b5564afb-94b74bf0ef7b`과 일치했다. 전환 후 디스크 가용량은 83,516,223,488 bytes였다.

실제 provider 호출에 따른 신규 `OCR_EVENT` 로그는 사용자의 요청에 따라 다음 정규 예약에서 확인해야 한다. 캐시 적중 등으로 provider 호출이 발생하지 않으면 해당 호출 로그도 발생하지 않는다. 구현과 사전 테스트 증거는 `REPORT.md`, 수정 보고서 및 `REVIEW_01.md`를 참조한다.
