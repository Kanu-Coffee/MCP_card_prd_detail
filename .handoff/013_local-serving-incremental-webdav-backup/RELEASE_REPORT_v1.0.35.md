# v1.0.35 発행 완료 보고

2026-10-10 Codex Executor/Reviewer. 사용자가 운영 인수 및 커밋·main 병합·GitHub 릴리스 발행을 승인했다.

## 완료 결과

- 소프트웨어 버전: **1.0.35**. Worker/MCP/core pyproject, Worker 표시 버전, uv.lock 및 workspace 테스트를 동기화했다.
- 준비 커밋: `9797c7f310c2d761b52a43004af573ee67a71291`.
- CI 보완 커밋: `d1c257ca14a14aa8310469be86a58d35908d50c5`.
- [PR #44](https://github.com/Kanu-Coffee/MCP_card_prd_detail/pull/44): 2026-10-10 07:46:23 KST에 merge 방식으로 main 병합 완료. 기능 브랜치는 삭제했다.
- 병합 커밋 및 릴리스 대상: `8c4bf40090132b619ce00250cd289d2547ef915e`.
- Annotated tag `v1.0.35`를 원격에 게시했다. 기존 공개 태그·릴리스 이력을 보존했다.
- [GitHub Release v1.0.35](https://github.com/Kanu-Coffee/MCP_card_prd_detail/releases/tag/v1.0.35): 2026-10-10 07:46:36 KST 발행 완료. draft/prerelease가 아닌 최신 공개 소스 릴리스다.
- 이 보고서는 발행 후 main에 추가하는 문서 기록이다. 이미 게시한 릴리스 태그를 이동하지 않는다.

## 검증 및 릴리스 준비 보완

[PR CI 38000600032](https://github.com/Kanu-Coffee/MCP_card_prd_detail/actions/runs/38000600032)가 전체 성공했다.

- 전체 런타임 테스트 **2,497 passed**, 예상된 회귀 시나리오 RuntimeWarning 10건.
- Ruff lint/format, mypy **108 source files** 통과.
- actionlint, ShellCheck, Git 이력 전체 Gitleaks 및 Trivy 감사 통과.
- Worker/MCP Compose 구성 검증 및 두 이미지 빌드 통과.
- 로컬 uv lock 정합성, git diff 공백 검사 및 staged 비밀정보 검사 통과.

첫 로컬 전체 테스트에서 기존 candidate Compose의 볼륨 기대값 한 건이 실패했다. 새 read-only 로컬 serving 볼륨을 실제 배포 구성에 맞게 포함시켰고 후속 대상 테스트 16건 및 CI 전체 테스트를 통과했다. 기존 Worker 코드 5개 파일의 포맷과 테스트 import/UTC/encoding 스타일도 정리했다. 실행 로직은 변경하지 않았다.

첫 PR CI는 Handoff 공개 OCR 캐시 해시를 비밀정보로 오탐한 9건으로 실패했다. 실제 자격증명이 아닌 내용 기반 식별자임을 확인하고 `.gitleaks.toml`에 해당 필드와 정확한 7개 해시 값만 예외로 추가했다. 임의의 다른 값이나 전체 Handoff 경로를 제외하지 않았으며 Git 이력 전체 재검사가 통과했다. 초기 실패 이력은 보존했다.

## 운영 인수와 발행 범위

운영 인수 근거는 CLOSEOUT_v1.0.35.md, ACCEPTANCE_FIX_08.md 및 각 OPERATIONS 보고서·JSON 증거에 정리했다. Worker/MCP의 로컬 게시와 서빙, 초기 백업 관리대장 완료, 변경 없는 후속 배치의 OCR 재사용 및 백업 HTTP 0건을 확인했다.

현재 운영 설치는 `/opt/cardrag/013-50a0129`이며 Worker/MCP는 이미 인수한 `013-50a0129` 이미지다. 이번 발행은 GitHub 소스 릴리스이며 새 Docker Hub/GHCR 이미지나 서명·qualification 자산을 발행하지 않았다. 버전 표시 수정 때문에 장시간 배치를 추가 실행하지 않았다.

10월 10일 03시 systemd 실행은 경로 권한 오류로 시작 전 실패했으며, 권한 수정·운영 UID 접근 검증 후 수동 복구 배치를 완료했다. 다음 실제 03시 예약 실행 결과는 운영 후속 확인 사항으로 남긴다. 예정 실행의 성공을 현재 완료된 사실로 기재하지 않는다.

## 문서 정리

README, OPERATIONS, SIMPLE_RUNTIME, RELEASING 및 RELEASE_NOTES_v1.0.35.md를 갱신했다. Handoff 과거 PLAN/REPORT/FIX 이력은 수정·삭제하지 않고 운영 증거와 인수 마감 및 본 발행 보고서를 추가했다.
