# v1.0.35 발행 후 CI 테스트 안정화

2026-10-10 Codex. RELEASE_REPORT_v1.0.35.md 발행 이후의 추가 검증 기록이며 이전 인수·보고서를 대체하거나 수정하지 않는다.

## 발견

PR #44 CI는 전체 2,497 테스트 및 이미지 빌드를 통과했다. 발행 보고서만 추가한 main 커밋 `e4930dcb9c79b5ab4497a3e8b1ea473bef1728b6`의 [CI 38001067852](https://github.com/Kanu-Coffee/MCP_card_prd_detail/actions/runs/38001067852)는 2,496 passed / 1 failed였다.

실패는 기존 `test_independent_two_process_lock_barrier`였다. 이 테스트가 실제 CLI를 통해 외부 임베딩 API를 fake-key로 호출하고, 잠금 경쟁 중 파일 생성이 별도 용량 트리 검사를 방해했다. 실패 프로세스는 SQLite를 열기 전에 용량 트리 변경으로 거절됐으며, 다른 프로세스는 잠금을 잡고 DB를 열었지만 외부 임베딩 요청 거절로 종료했다. 이 결과를 운영의 잠금 실패나 OCR/백업 회귀로 해석하지 않는다.

## 수정과 검증

운영 코드는 변경하지 않는다. 테스트의 용량 스냅샷은 경쟁 전에 고정하고, 외부 임베딩 함수 대신 잠금을 가진 상태에서 경쟁 프로세스의 종료를 기다리는 동기화 fixture를 사용한다. multiprocessing barrier와 실제 CLI 잠금 및 실제 SQLite 열기 감시는 유지한다.

한 프로세스만 `worker_busy`/exit 0으로 거절되며 그 프로세스는 DB를 열지 않았는지 검사한다. 승자는 DB를 연 후 의도된 fixture 종료 지점에 도달하는지도 검사한다. Queue.empty() 대신 결과 두 건을 timeout으로 받는다. 실제 외부 provider 호출과 실행 순서에 의존하지 않는다.

Ruff lint/format 및 대상 테스트를 통과했다. 후속 PR CI 전체 검증 후 main 병합한다. 공개 v1.0.35 태그는 이동하지 않으며 이 테스트 안정화는 발행 후 main의 검증 보완이다. 기존 운영 이미지·인수 결과·다음 실제 03시 실행 후속 확인은 그대로 유효하다.
