# CardRAG v1.0.36

## 변경 사항

- Worker 초기화의 OCR resolver, tokenizer, Qwen provider 사전검사, 임베딩 설정 및 pipeline 진입 단계를 구분해 소요시간과 성공·실패를 기록합니다.
- 초기화 오류에 안전한 분류, 단계, HTTP 상태, 재시도 가능 여부와 시도 횟수를 추가합니다. 예외 원문이나 자격증명을 출력하지 않습니다.
- `startup-check`는 운영과 동일한 초기화를 수행하고 corpus 처리·게시·백업 전에 종료합니다. 상태 DB 접근 전 Worker 락을 획득합니다.
- Worker 내부 Codex CLI를 checksum으로 고정한 0.162.1로 업데이트했습니다. 운영 OCR은 `gpt-6.1-sol` / medium, fallback은 local PaddleOCR / `PaddleOCR-VL-1.6`입니다.

## 검증과 운영 근거

- 수정본 독립 검증: 지정 테스트 163개, Ruff lint/format 및 mypy 통과.
- 2026-10-12 수동 복구 배치: exit 0, 문서 5,520건, OCR 캐시 5,520건 재사용, 신규 OCR 호출 0건, 로컬 generation 게시 및 MCP 활성화 확인.
- 신한 discovery 네트워크 실패는 기존 자료 carry로 처리했습니다. 백업 성공, pending/lost 0을 확인했습니다.
- 복구 배치는 초기 진단 구현에서 완료됐습니다. 이후 공통 초기화 경로 통합은 독립 테스트 및 이미지 소스 일치로 검증했습니다. Codex 실제 이미지 OCR 호출 근거는 10월 11일 실행입니다.
- 10월 12일 03시 예약 실패의 정확한 예외는 과거 로그에 남지 않아 원인이 미상입니다. 이번 변경은 확인된 진단 결함을 수정했으며 미상 원인 제거를 보증하지 않습니다.

## 발행 범위

패키지 3종과 Worker 표시 버전을 1.0.36으로 맞춘 annotated 태그 및 GitHub 소스 릴리스입니다. 운영 Worker는 릴리스 소스로 로컬 빌드하여 적용하고 기존 MCP, 볼륨, 인증 및 타이머를 유지합니다. 새 Docker Hub/GHCR 이미지나 서명·qualification 자산은 발행하지 않습니다.

인수 근거: [014 최종 리뷰](../.handoff/014_worker-startup-failure-codex-validation/ACCEPTANCE_FIX_01.md). 배포·머지·릴리스의 실제 결과는 별도 완료 보고서에 기록합니다.
