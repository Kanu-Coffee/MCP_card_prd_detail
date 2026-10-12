# v1.0.36 전체 검증 보완

2026-10-12 Codex. 릴리스 준비 전체 suite 첫 실행은 39 failed / 2,466 passed였다.

새 startup-check 테스트 3건이 실제 `_configure_worker_logging()`을 호출하면서 `cardrag_worker` logger에 CliRunner의 일시 stderr handler와 propagate=False 설정을 남겼다. 뒤따르는 OCR/pipeline caplog 테스트에서 로그가 수집되지 않고 종료된 stream 쓰기 오류가 발생했다. 지정 3파일 검증으로는 발견되지 않은 테스트 간 상태 오염이다.

새 테스트 3건에 한정해 logger handlers/level/propagate를 저장·복원하는 fixture를 적용한다. 추가 handler는 테스트 종료 전에 제거·close한다. 운영 로깅/초기화 코드는 변경하지 않으며 로그 검증을 비활성화하지 않는다. 같은 전체 suite를 다시 실행하고 결과를 완료 보고서에 기록한다.
