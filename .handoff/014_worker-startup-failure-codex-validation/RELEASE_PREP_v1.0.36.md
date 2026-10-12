# v1.0.36 발행 준비

2026-10-12 Codex. 사용자가 운영 반영, GitHub 커밋·머지·릴리스 완료를 명시적으로 요청했다.

- 인수 기준: a50a815 및 ACCEPTANCE_FIX_01.md. 패키지 3종, Worker 표시 버전, workspace 버전 검사 및 uv.lock을 1.0.36으로 맞춘다. 의존성 변경은 없다.
- 기존 v1.0.35와 같은 GitHub 소스 릴리스 방식이다. annotated v1.0.36 태그, 공개 GitHub 릴리스 및 로컬 운영 Worker 빌드를 포함한다. v1.0.34 전용 공개 이미지 workflow는 실행하지 않는다.
- README/OPERATIONS/RELEASING/docs index 및 새 릴리스 노트를 갱신한다. 014 PLAN/REPORT/FIX/인수 및 최소 공개 증거를 이력대로 커밋한다.
- Handoff REPORT의 공개 고정 tokenizer SHA가 로그의 `tokenizer_sha256=` 문법에서 Gitleaks generic-api-key 오탐 2건을 유발했다. 기존 공개 SHA allowlist에 그 정확한 field/value 문법만 추가한다. 다른 값이나 전체 경로를 제외하지 않는다.
- 운영 Worker는 최종 소스 commit으로 `cardrag-worker:014-v1.0.36`을 빌드하고 기존 host-local Compose image만 교체한다. secrets/볼륨/인증/MCP/타이머/백업 정책은 유지한다. 실제 설정의 startup-check로 검증하되 장시간 전체 배치를 다시 실행하지 않는다.
- PR CI가 통과한 commit을 merge하고 병합 commit에 새 annotated tag를 발행한다. 기존 태그·릴리스는 이동하거나 덮어쓰지 않는다.
- 실제 commit, 이미지 ID, startup 결과, CI/PR/릴리스 URL은 별도 완료 보고서에 기록한다. 본 문서는 완료 주장이 아니다.
