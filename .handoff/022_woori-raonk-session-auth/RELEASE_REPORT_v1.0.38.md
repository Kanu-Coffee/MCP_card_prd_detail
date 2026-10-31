# v1.0.38 릴리스 완료 보고

2026-10-31 Executor. 020(Paddle fallback 임시 심볼릭 링크 격리 및 상태 트리 복구), 021(OCR 프로바이더 호출 구조화 진단 로깅), 022(우리카드 RAONK 세션 인증 및 다운로드 복구)의 완주 및 운영 배포·재개 성공 판정에 따라 전체 handoff 마감·버전 상승·커밋·main 병합·공개 GitHub 소스 릴리스 및 운영 런타임 정렬을 완료했다.

---

## 1. Git과 발행

- **누적 기능 및 테스트 통합 commit**: `433f9bc4c0175bfa60ea0b0439db26e99bf88ff8`.
- **버전 상승 (1.0.38) commit**: `61623f39cf9e54a6c4b281fbfdbe149a42be4b60`.
- **문서 및 운영 가이드 commit**: `f7af344e6b12a3d740c08ec2e93ffdf68b6cf403`.
- **Handoff 상태 인덱스 commit**: `1c21102bfa7c588a44b1c55a2ee0ee67a6d967b5`.
- **Trivy base image pip-wheel CVE-2026-114613 예외 commit**: `7434b6495f4f1e116fea322fe90ceca7e330100d`.
- **[PR #49](https://github.com/Kanu-Coffee/MCP_card_prd_detail/pull/49) main 병합 완료**:
  - 병합 commit: `99b5ba69fb5907be2218873c10bbb945d6181dcd`.
- **Annotated `v1.0.38` Tag**:
  - Tag object: `024a134631870a8a9aa1987ce1b2986e4db97bbd`.
  - Tagger: `Kanu-Coffee <ymtop59@gmail.com>`.
  - Peeled commit: 위 병합 commit(`99b5ba69fb5907be2218873c10bbb945d6181dcd`)과 일치.
- **[CardRAG v1.0.38](https://github.com/Kanu-Coffee/MCP_card_prd_detail/releases/tag/v1.0.38)**:
  - 2026-10-31 08:26:44 KST (2026-10-30 23:26:44 UTC) 공개 발행 완료.
  - Draft=false, Prerelease=false, Latest 릴리스로 설정.

---

## 2. 검증 증적

- **전체 로컬 회귀 테스트**:
  - `uv run --all-packages pytest`: **2,893 passed, 44 warnings** in 93.42s (0 failures).
  - Reviewer 직접 재검증: `2893 passed, 11 warnings` in 92.16s 통과.
- **정적 분석 및 포맷팅**:
  - `uv run --all-packages ruff check`: All checks passed.
  - `uv run --all-packages ruff format --check`: All formatted.
  - `uv run mypy --strict packages/cardrag-core/src apps/cardrag-worker/src apps/cardrag-mcp/src`: Success (no issues found in 117 source files).
  - `git diff --check`: 통과.
- **CI 감사**:
  - [PR CI 41143931129](https://github.com/Kanu-Coffee/MCP_card_prd_detail/actions/runs/41143931129): 전체 통과 (4m 44s).
  - [main push CI 41144473379](https://github.com/Kanu-Coffee/MCP_card_prd_detail/actions/runs/41144473379): 전체 통과 (4m 33s).
  - static audit, secret audit(Gitleaks), Trivy fs/image vuln scan, 2-service deployment validation, Worker/MCP container build, launch-window clock test 전체 성공.

---

## 3. 운영 런타임 정렬 및 정합성 보완 (FIX_07 반영)

- **활성 릴리스**: `/opt/cardrag/current -> /opt/cardrag/v1.0.38`
- **Worker Image**:
  - `cardrag-worker:v1.0.38` (고유 태그: `cardrag-worker:v1.0.38-99b5ba69fb59`)
  - Image ID: `sha256:8399cd078ad468646959c3980581608e70d4dba7f6e538d5a7c5ebfa546a3a05`
  - OCI Version: `1.0.38`
  - OCI Revision: `99b5ba69fb5907be2218873c10bbb945d6181dcd`
  - Source: `https://github.com/Kanu-Coffee/MCP_card_prd_detail`
- **MCP Image**:
  - 실행 중인 라이브 MCP: 기존 `cardrag-mcp:013-50a0129` (패키지 1.0.34) 유지.
  - 신규 빌드 전용 이미지: `cardrag-mcp:v1.0.38` (`sha256:fd32cce9b7363279dd4481890fe8ccfc19edf2f301da34add29da089cfc9bb0a`, version=1.0.38, revision=`99b5ba69fb5907be2218873c10bbb945d6181dcd`).
- **신규 독립 복구 스냅샷**:
  - `v1038-aligned-20261031`: 우리카드 resume 완주(`succeeded`) 및 활성 서빙 generation `g-df29d8e5dbb34048942a5296-5a3a99224f92` 상태 반영.
  - `verify-recovery-point.py` 무결성 검증 완료 (`verified: true`).
  - 단일 독립 스냅샷 정책: 구 스냅샷 `022-woori-session-auth-20261030` 정리 완료.
- **코드 롤백 지점**:
  - 릴리스: `/opt/cardrag/022-woori-session-auth-20261030`
  - 이미지: `cardrag-worker:022-woori-auth-47ea2207b173`
  - 가이던스: `/opt/cardrag/v1.0.38`과 `/opt/cardrag/022-woori-session-auth-20261030` 상호 100% 동기화.
- **Systemd Timer**:
  - `cardrag-worker.timer`: 매일 **10:00 Asia/Seoul** 정상 대기 (`active (waiting)`). 배포 전 과정 무중단.
- **가용 용량**:
  - `/state` 가용 공간 74.02 GiB, 호스트 루트 파일시스템 75 GB 가용.

---

## 4. 인덱스 및 상태 표기 안내

현행 [STATUS_v1.0.38.md](../STATUS_v1.0.38.md)에 기재된 `RELEASE_PENDING` 표기는 릴리스 진행 시점의 역사적 기록이며, 본 보고서(`RELEASE_REPORT_v1.0.38.md`) 및 `FIX_07_REPORT.md`를 통해 022 과제의 릴리스와 운영 인수가 완결되었음을 공식 기록한다.
