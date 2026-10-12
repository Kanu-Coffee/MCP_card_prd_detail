# v1.0.36 운영 반영·GitHub 발행 완료

2026-10-12 Codex. 사용자 요청에 따라 운영 반영, 커밋, main 머지 및 공개 GitHub 릴리스 발행을 완료했다.

## GitHub

- 최종 소스 준비 commit: `2e4a19451e72d9c8218e90e940bdb9fe393782a4`.
- [PR #47](https://github.com/Kanu-Coffee/MCP_card_prd_detail/pull/47) merge 완료.
- 병합 commit 및 annotated `v1.0.36` 대상: `6f09c28521848afbf72c92f0bb26c09b9c3eb98c`.
- [CI 38201128295](https://github.com/Kanu-Coffee/MCP_card_prd_detail/actions/runs/38201128295) 전체 성공: 보안·lint·type·runtime·Compose·Worker/MCP 이미지 빌드.
- [CardRAG v1.0.36](https://github.com/Kanu-Coffee/MCP_card_prd_detail/releases/tag/v1.0.36) 2026-10-12 12:52:53 KST 공개 발행. draft/prerelease 아님. 원격 annotated tag 및 peeled commit 일치 확인.
- 패키지 3종, Worker 표시 버전, uv.lock, workspace version 검사 및 현재 문서를 1.0.36으로 동기화했다. 외부 의존성 변경 없음.
- 이번 발행은 GitHub 소스 릴리스다. v1.0.34 전용 공개 이미지 workflow를 우회하거나 새 registry 이미지·서명·qualification을 발행하지 않았다.
- 본 보고서는 발행 후 main에 추가하는 기록이다. 이미 발행한 태그는 이동하지 않는다.

## 운영

- 설치 진입점 `/opt/cardrag/current -> /opt/cardrag/013-50a0129` 유지.
- Worker image: `cardrag-worker:014-v1.0.36`.
- 실제 image ID: `sha256:3a8c74ffdc2ebc35261cb78b169903addc5b1618c350d18e8f044f0fb4d47294`.
- build args: APP_VERSION=1.0.36, VCS_REF=2e4a19451e72d9c8218e90e940bdb9fe393782a4. release commit과 build source의 전체 Git tree가 일치한다. 설치된 cli.py SHA도 repository 파일과 일치한다.
- 운영 Compose image 지정만 교체했다. 전체 렌더링 결과를 비교하여 나머지 설정이 동일함을 확인했다. 기존 auth/state/Paddle/serving 볼륨, secret, OCR 설정, immediate 백업 300초 정책 및 timer 유지.
- 이전 image `cardrag-worker:013-50a0129-codex-01621`와 기존 정상 완료 컨테이너 보존. private rollback Compose 사본과 배포 JSON은 `/opt/cardrag/current/operations/deployment-014-v1.0.36/`에 보관.
- 같은 helper/Compose/env로 `cardrag-worker-014-v1036-startup` 실행: **startup-check exit 0, 50.258초**, status ready. Codex 0.162.1, gpt-6.1-sol / medium, PaddleOCR-VL-1.6 fallback, Qwen deepinfra 실제 preflight 통과.
- 운영 초기화 검사 로그: `docker logs cardrag-worker-014-v1036-startup`. corpus/게시/백업을 새로 실행하지 않았다.
- MCP healthy / ready true, 기존 성공 generation `g-cb12f9988100445eb7ecd873-035c4632e085` 유지.
- timer active, 다음 실행 2026-10-13 03:00 KST. 해당 예약 실행의 완료는 아직 관찰하지 않았다. Worker는 유한 배치이므로 초기화 검사 후 정상 종료 상태이며 다음 타이머가 새 이미지를 사용한다.

## 검증과 릴리스 준비 보완

- 로컬 전체 runtime: **2,505 passed / 10 expected warnings / 81.92초**.
- 전체 Ruff lint/format, mypy 108 source files, lock 정합성, Git diff 공백 검사 통과.
- Git 전체 이력 Gitleaks 250 commits / no leaks. 공개 고정 tokenizer SHA 로그 문법의 오탐 2건만 field/value 단위로 예외 처리했다.
- 최초 전체 suite의 로그 검증 실패 39건은 새 startup-check 테스트가 일시 stderr handler와 propagate 설정을 남기는 테스트 격리 문제였다. 해당 3건에서 logger 상태를 복원하는 fixture로 보완했고 같은 전체 suite 및 GitHub CI를 통과했다. 제품 로깅 로직을 변경하거나 로그 검증을 비활성화하지 않았다.
- 기존 전체 복구 배치 성공은 앞선 7d07b1d 이미지의 증거이며, 새 릴리스의 전체 배치 완료로 재표기하지 않는다. 03시 정확한 실패 원인은 미상이다.

공개 가능한 최소 증거: `evidence/deployment-release-v1.0.36.json`. 기존 PLAN/REPORT/FIX/인수/증거는 보존했다.
