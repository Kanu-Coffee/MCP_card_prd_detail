# v1.0.37 릴리스 완료 보고

2026-10-25 Executor. 사용자의 운영테스트 정상 판정에 따라 테스트를 종료하고, handoff 전체 마감·버전 상승·커밋·main 병합·공개 GitHub 릴리스를 완료했다.

## Git과 발행

- 기능·종료 정리 commit: `0e82f3287ffd2a0a8eef5399b30b73eff80501e2`.
- CI 경로 보완 commit: `2762b7e84e68eec88040d1ca7edc4a2244c7ba23`.
- 이미지 보안 보완 commit: `a4b11befe63edc3d2be5be9a8bc6d46076e02a8f`.
- [PR #48](https://github.com/Kanu-Coffee/MCP_card_prd_detail/pull/48) main 병합 완료. 병합/source commit `000a119a59d3086c5a25a5ca4fc302b9dfb7b4e8`.
- Annotated `v1.0.37` tag object `38a068aa983d9da495f7ea538ef4fe89ffe918a2`, peeled commit은 위 source commit과 일치한다. 기존 태그를 이동하지 않았다.
- [CardRAG v1.0.37](https://github.com/Kanu-Coffee/MCP_card_prd_detail/releases/tag/v1.0.37)은 2026-10-25 21:41:32 KST 공개 발행했다. draft/prerelease가 아니며 latest로 설정했다.
- 첨부 `release-manifest.json`과 `SHA256SUMS`를 원격에서 내려받아 체크섬 일치를 확인한 뒤 공개했다. manifest는 source/tree/PR/CI와 소스 발행 범위를 결속한다.

## 검증

- 전체 로컬 회귀: 2,836 passed, 11 warnings. 보안 의존성 갱신 후에도 84.75초로 통과했다. mypy 115개 소스, Ruff lint/format 244개 파일, lock, actionlint, shellcheck, hadolint 통과.
- [최종 PR CI 40193193509](https://github.com/Kanu-Coffee/MCP_card_prd_detail/actions/runs/40193193509) 전체 성공. 원격 회귀 2,836 passed, 11 warnings, 150.37초. Compose/systemd, Worker/MCP 빌드와 HIGH/CRITICAL 보안 정책 검사를 포함한다.
- [발행 source/main CI 40193711317](https://github.com/Kanu-Coffee/MCP_card_prd_detail/actions/runs/40193711317)도 전체 성공 후 태그를 발행했다.
- 전체 Git 이력 Gitleaks 누출 없음. 공개 digest 오탐 3개만 정확한 값과 해당 규칙에 한정해 예외 처리했다.
- 첫 CI의 설치 경로 fixture 오류와 두 번째 CI의 수정 가능한 이미지 취약점은 RELEASE_CI_FIX_01/02에 보존했다. 검사 생략/새 취약점 예외 추가로 통과시키지 않았다. 기존 역사 기록의 Markdown hard-break 공백 3줄은 보존했다.

## 완료 범위와 운영 유지

- 패키지 3종·Worker 표시 버전·uv.lock을 1.0.37로 동기화했다.
- [현재 종료 인덱스](../STATUS_v1.0.37.md)가 001–019 전체 CLOSED와 활성 계획 없음의 기준이다. 옛 README/PLAN/REPORT/FIX의 상태 표현은 당시 기록으로 보존했다. 누락된 과거 FIX 보고서를 사후 작성해 실행 이력을 만들지 않았다.
- 검증 과정에서 Pillow 13.0.0, aiohttp 3.14.5, certifi 2026.10.23, filelock 4.2.4 및 CA bundle 수정 floor를 반영했다. 새로운 의존성의 코드/이미지 CI 검증과 이전 운영 이미지 실배치 증거를 구별한다.
- 발행은 GitHub 소스 릴리스다. 새 Docker Hub/GHCR 이미지·서명·v1.0.34 qualification은 발행하지 않았다.
- 검증된 운영 release `/opt/cardrag/019-fix33-7d2e39edbf59`, Worker image ID `sha256:71eb18e6538de2e0b00fd3c5173045263980322c623b17e3debee7d782d51450`, 데이터·인증·서빙 볼륨과 매일14:30 KST 예약을 유지했다. 버전 태그 발행을 운영 이미지 교체로 표현하지 않는다.
- 우리카드 외부 일시 오류, 범위 밖 기존 백업 대기 3건, 경고 band와 export 성능은 인수된 운영 참고사항이다. 열린 plan이나 미완료 릴리스 작업으로 남기지 않는다.

본 문서는 공개 발행 후 결과를 추가하는 문서 커밋이다. 공개 태그와 발행 source commit은 고정하며, 후속 문서 커밋 CI는 source 릴리스의 검증 근거와 구분한다.
