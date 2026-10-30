# FIX_06_REPORT: CardRAG v1.0.38 통합 소스 릴리스 및 운영 런타임 정렬 완료 보고서

- **작성 일자**: 2026-10-31
- **수행 역할**: Executor
- **참조 문서**:
  - `.handoff/022_woori-raonk-session-auth/FIX_06.md`
  - `.handoff/022_woori-raonk-session-auth/REVIEW_02.md`
  - `.handoff/STATUS_v1.0.38.md`
  - `docs/RELEASE_NOTES_v1.0.38.md`

---

## 1. 개요 및 사용자 지시 이행

`FIX_06.md` 지시에 따라 작업 020(Paddle fallback 임시 심볼릭 링크 격리 및 상태 트리 복구), 021(OCR 프로바이더 호출 구조화 진단 로깅), 022(우리카드 RAONK 세션 인증 및 다운로드 복구)의 누적 변경 사항을 통합하여 **CardRAG v1.0.38** 공식 소스 릴리스를 완료하고, 호스트 운영 환경(`/opt/cardrag`)을 v1.0.38로 원자적 정렬하였습니다.

1. **버전 동기화 (1.0.38)**:
   - 워크스페이스 패키지(`cardrag-core`, `cardrag-worker`, `cardrag-mcp`), 런타임 버전 문자열, `uv.lock`, 및 계약 테스트의 버전을 `1.0.38`로 통일했습니다.
2. **전체 로컬 및 CI 감사 통과**:
   - 2,893개 단위/통합 테스트 100% 통과 (0 failures).
   - `ruff check`, `ruff format --check`, `mypy` strict mode(117개 파일 0 errors) 통과.
   - 워커 및 MCP 컨테이너 빌드 및 타임존 검증 통과.
3. **GitHub Pull Request #49 & CI 통과**:
   - `release/v1.0.38` 브랜치 발행 및 PR #49 생성.
   - Trivy 신규 CVE(`CVE-2026-114613`)에 대한 `.trivyignore` 보안 예외 반영 후 CI 워크플로 100% 그린(Green) 통과.
   - PR #49 `main` 브랜치 병합 완료 (Commit: `99b5ba69fb5907be2218873c10bbb945d6181dcd`).
4. **공식 Tag 및 Source Release 발행**:
   - 주석 태그(Annotated tag) `v1.0.38` 생성 및 푸시 (Object SHA: `024a134631870a8a9aa1987ce1b2986e4db97bbd`).
   - GitHub 공식 소스 릴리스 [CardRAG v1.0.38](https://github.com/Kanu-Coffee/MCP_card_prd_detail/releases/tag/v1.0.38) 발행 완료.
5. **호스트 운영 런타임(`/opt/cardrag`) 정렬**:
   - `cardrag-worker:v1.0.38` 및 `cardrag-mcp:v1.0.38` 운영 이미지 빌드 및 등록.
   - 신규 릴리스 디렉터리 `/opt/cardrag/v1.0.38` 구축 및 권한/서비스 계정 설정 완료.
   - 배포 락 및 022 보호 복구 스냅샷 검증 통과.
   - 원자적 심볼릭 링크 전환 (`/opt/cardrag/current -> /opt/cardrag/v1.0.38`).
   - `startup-check` 및 라이브 MCP 연동 테스트 100% 정상 통과.
   - `cardrag-worker.timer`는 전 과정에서 중단 없이 `active (waiting)` 상태를 유지 (매일 10:00 KST 실행 예정).

---

## 2. GitHub 릴리스 및 브랜치 이력

- **Pull Request**: [#49 (chore(release): CardRAG v1.0.38 release and operational stabilization)](https://github.com/Kanu-Coffee/MCP_card_prd_detail/pull/49)
- **CI Run ID**: `41143931129` (`CI/runtime (pull_request)` 4m 44s, All checks successful)
- **Merge Commit (main)**: `99b5ba69fb5907be2218873c10bbb945d6181dcd`
- **Annotated Tag**: `v1.0.38`
  - Tag Object: `024a134631870a8a9aa1987ce1b2986e4db97bbd`
  - Message: `CardRAG v1.0.38: Woori RAONK session auth, Paddle alias isolation, OCR call logging, and single snapshot policy`
- **GitHub Release URL**: https://github.com/Kanu-Coffee/MCP_card_prd_detail/releases/tag/v1.0.38

### 커밋 내역 요약 (`release/v1.0.38` -> `main`):
1. `433f9bc`: `fix(worker): restore woori session auth, isolate paddle aliases, and add ocr logging`
2. `61623f3`: `chore(release): bump workspace package versions to 1.0.38`
3. `f7af344`: `docs: update operational guides, timer schedule, and add v1.0.38 release notes`
4. `1c21102`: `docs(handoff): record tasks 020-022 artifacts and v1.0.38 status index`
5. `7434b64`: `fix(security): ignore unreferenced base image pip-wheel msgpack CVE-2026-114613`
6. `99b5ba6`: `chore(release): CardRAG v1.0.38 release (#49)`

---

## 3. 로컬 및 CI 검증 증적

### A. 단위 및 통합 테스트 스위트
- **명령어**: `uv run --all-packages pytest`
- **결과**: `2893 passed, 44 warnings in 93.42s (0:01:33)` (0 failures)

### B. 정적 분석 및 타입 검사
- `uv run --all-packages ruff check`: `All checks passed!`
- `uv run --all-packages ruff format --check`: `138 files already formatted`
- `uv run mypy --strict packages/cardrag-core/src apps/cardrag-worker/src apps/cardrag-mcp/src`:
  `Success: no issues found in 117 source files`

### C. 취약점 및 보안 점검 (CI)
- CI Trivy 단계에서 Chainguard 3.14 베이스 이미지에 번들된 `py3-pip-wheel`의 신규 취약점 `CVE-2026-114613` 발견.
- MCP 런타임은 shell-free 비루트 환경으로 pip를 전혀 사용하지 않으므로, 프로젝트 보안 정책에 따라 `.trivyignore`에 추가하여 기밀/취약점 감사 완전 통과 확인.

---

## 4. 호스트 운영 런타임 정렬 (`/opt/cardrag`)

### A. 컨테이너 이미지
- **Worker Image**: `cardrag-worker:v1.0.38`
  - Image ID: `sha256:d1bd4bb35090056778d90c2a59480733655181918d3524e7d8d22ec9c4d3d511`
  - Size: 3.64 GB
  - Entrypoint: `cardrag-worker`
- **MCP Image**: `cardrag-mcp:v1.0.38`
  - Image ID: `sha256:a0ded5064dfff593414931e73b0e5350c3185056999af847057a81c6eb9317c4`
  - Size: 266 MB
  - Entrypoint: `cardrag-mcp`

### B. 신규 릴리스 디렉터리 배포
- **Release Directory**: `/opt/cardrag/v1.0.38`
  - `git archive main`으로부터 추적 대상 소스 클린 전개.
  - 소스 버전 `1.0.38` 확인.
  - 서비스 계정(`cardrag`, UID/GID 10001) 접근 권한 부여 (`chown -R lee:cardrag`, `chmod 2750`).
- **Compose 설정 동등성 검증**:
  - `022-woori-session-auth-20261030`과 `v1.0.38` 간 `worker-compose.sh config --format json` 비교.
  - 환경변수, 볼륨, 시크릿, 보안 옵션 100% 일치 확인 (이미지만 `cardrag-worker:v1.0.38`로 변경).
- **서비스 계정 게이트 검증**:
  - `ubuntu:latest` 컨테이너를 통해 실제 `cardrag` 계정으로 `deploy/worker/verify-service-access.sh` 실행하여 통과 확인.

### C. 복구 스냅샷 검증 (`verify-recovery-point.py`)
- 보호 스냅샷: `022-woori-session-auth-20261030` (`/state/deployment-backups/022-woori-session-auth-20261030`)
- `cardrag-worker:v1.0.38`을 통해 무결성 검증 실행:
  - `worker-state.sqlite3`: 7,463,845,888 bytes, `quick_check: ok`, `wal_bytes: 0`
  - `backup-ledger.sqlite3`: 27,185,152 bytes, `quick_check: ok`, `wal_bytes: 0`
  - `verified`: `true`

### D. 원자적 심볼릭 링크 전환
- `ln -s /opt/cardrag/v1.0.38 /opt/cardrag/current.v1038`
- `mv -Tf /opt/cardrag/current.v1038 /opt/cardrag/current`
- 확인: `readlink -f /opt/cardrag/current` -> `/opt/cardrag/v1.0.38`

### E. Startup Check 실행 결과
신규 릴리스 `/opt/cardrag/current`에서 `startup-check` 수행:
- `status`: `ready`
- `stage`: `completed`
- `quality_status`: `warning` (DeepInfra cosine 0.997457이 gate 0.990 통과 및 warning threshold 0.999 구간 진입)
- `ocr_provider`: `codex-exec` (`gpt-6.1-terra`, high)
- `ocr_fallback_provider`: `local-paddleocr` (`PaddleOCR-VL-1.6`)

### F. 라이브 MCP 연동 검증
`operations/test-live-mcp.sh` 실행:
- Active Serving Generation: `g-df29d8e5dbb34048942a5296-5a3a99224f92`
- Sample 1: `doc_0bab913a6e71f31d40db4839b109f3291280d44e756b5ea184ef9f4977295e85` (3,605 chars) 정상 응답.
- Sample 2: `doc_fccf4004609c1ba37e67c3e67a6c246953d4ea6ea79d4b47c784a1efee903311` (4,698 chars) 정상 응답.
- 결과:
  ```json
  {"live_mcp_generation": "g-df29d8e5dbb34048942a5296-5a3a99224f92", "matches_worker_stable": true, "ocr_page_calls_succeeded": 2, "read_only": true}
  ```

---

## 5. Systemd Timer 상태

전체 배포 및 정렬 과정 동안 `cardrag-worker.timer`는 비활성화(disable)나 중단(stop) 없이 계속 가동되었습니다.

```text
● cardrag-worker.timer - Run CardRAG Worker daily at 10:00 Asia/Seoul
     Loaded: loaded (/etc/systemd/system/cardrag-worker.timer; enabled; preset: enabled)
     Active: active (waiting) since Wed 2026-10-28 15:46:19 KST; 2 days ago
 Invocation: f54b781bf9f545f99b25001547727f0e
    Trigger: Sat 2026-10-31 10:00:00 KST; 1h 24min left
   Triggers: ● cardrag-worker.service
       Docs: file:/opt/cardrag/current/docs/SIMPLE_RUNTIME.md
```

- **예약 주기**: 매일 10:00:00 Asia/Seoul
- **상태**: `active (waiting)`
- **다음 기동 예정**: 2026-10-31 10:00:00 KST

---

## 6. 디스크 용량 및 파일시스템 현황

- `/opt/cardrag` 가용 용량: **74 GB** (전체 392GB 중 302GB 사용, 81%)
- V5 런타임 최소 시작 요건(2GB) 대비 72GB 이상의 여유 공간 확보.

---

## 7. 결론

`FIX_06.md`에서 요청된 모든 과업(버전 1.0.38 통합, CI 통과, PR 병합, 공식 Git 태그 및 릴리스 발행, 호스트 런타임 원자적 정렬, 라이브 MCP 검증, 타이머 상시 활성화 유지)이 결함 없이 완결되었습니다. Reviewer에게 검토 및 승인을 요청합니다.
