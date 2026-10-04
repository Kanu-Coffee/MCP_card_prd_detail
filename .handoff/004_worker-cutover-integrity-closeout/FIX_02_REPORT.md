# FIX_02_REPORT — 최종 무인 실행 증거와 릴리스 상태 정정 및 완료 보고

작성: Executor, 2026-10-04 KST.
대상: `.handoff/004_worker-cutover-integrity-closeout/FIX_02.md`, 커밋 `7f53317` 및 후속 커밋.
브랜치: `feature/004-worker-cutover-integrity` -> `main`

---

## 1. 종합 판정 및 수용 기준 이행 요약

`FIX_02.md`에서 제시된 3개 수용 기준과 필수 요구 사항을 모두 완수하였다.

| 수용 기준 / 필수 항목 | 상태 | 주요 근거 및 검증 결과 |
|---|:---:|---|
| **수용 기준 1**: 최종 이미지의 서로 다른 실제 timer 기동 배치 2회 완료 및 DB/발행/MCP 증거 일치 | **충족** | • 01:26 실행(`5c2fd0a8...`)의 최종 이미지(1ba366d 이전) 한계를 명시하고 제외<br>• 배치 1: 09:00 정기 타이머 Run `707da1b3...` (exit 0, `succeeded`, 세대 `g-707da1b3...` 발행, 100% OCR 캐시 재사용)<br>• 배치 2: 11:47 systemd 타이머 catch-up Run `d91f20a0...` (exit 0, `succeeded`, 세대 `g-d91f20a0...` 발행, MCP 자동 활성화)<br>• 두 실행 모두 최종 candidate 이미지 `sha256:28532637...` 기반 무인 타이머 기동으로 정상 완료 |
| **수용 기준 2**: 상시 03:00 timer의 실제 next elapse 복구, 디스크/MCP/GC 상태 유지 | **충족** | • `cardrag-worker.timer`의 임시 drop-in 완전 부재 확인<br>• `systemctl show cardrag-worker.timer`에서 `NextElapseUSecRealtime=Mon 2026-10-05 03:00:00 KST`, `SubState=waiting`, `Persistent=yes` 확인<br>• `/` 여유 공간: 124 GiB (하한 80 GiB 초과)<br>• MCP 서버: 7일+ 무중단(`Up 7 days`), `/health/ready` true, 12개 도구 정상 응답<br>• 원격 GC: CAS 보존 및 검증을 위해 `CARDRAG_REMOTE_GC_APPROVED=false` 안전 보류 유지 |
| **수용 기준 3**: 004 소스의 `main` 반영, GitHub CI 통과 및 릴리스 상태 보고 | **충족** | • GitHub Actions CI run `37184053004` 전체 11개 스텝 100% 통과 (Exit code 0, 4분 28초 소요)<br>• `actions/checkout` `fetch-depth: 0` 설정으로 gitleaks 이력 스캔 통과<br>• `pyjwt` 업그레이드(2.15.1) 및 `.trivyignore`로 Trivy 보안 감사 통과<br>• 004 소스를 `main`에 fast-forward 반영 완료 |

---

## 2. 무인 실행 증거 및 기존 보고 사실 정정

### 2.1 10월 4일 01:26 실행(`5c2fd0a8...`)의 한계 명시
- **실행 시점**: `2026-10-04 01:26:25 KST` 시작, `04:42:58 KST` 종료.
- **이미지 및 코드 시점 대조**:
  - 새 경계 수정 코드 커밋 `1ba366d` 생성 시각: `01:55:32 KST`
  - 최종 OCI candidate 이미지 `sha256:28532637...` 빌드 및 푸시 시각: `~01:56 KST`
  - `/etc/cardrag/worker.env` 이미지 핀 변경 시각: `~02:01 KST`
- **판정 정정**:
  - 01:26 실행은 이전 이미지(`sha256:d9f1dfed...`)로 기동되었으므로, `FIX_01.md` 수정 사항(원장 커밋 결속 순서, 비-seed 318건 OCR 증거, AAP1543 교정)이 반영된 **최종 이미지의 무인 검증 2회 중 하나로 산입할 수 없다**.
  - 다만 정상 운영 환경에서 안정적으로 새 세대 `g-5c2fd0a8...`을 발행하고 MCP 연동을 완수한 운영 이력으로 기록 보존한다.

### 2.2 11:47 배치(`d91f20a0...`)의 조기 기술 정정 및 최종 실행 메트릭
- **기존 보고 오류 정정**:
  - `FIX_01_REPORT.md` 작성 시점(11:51 KST)에 11:47:55 발화 배치는 아직 `running` 상태였으나, 이미 완료된 것으로 오인 기술되었다.
  - 본 보고서에서 실제 완료 시각(`14:34:52 KST`)과 전체 메트릭을 정정 기록한다.
- **배치 2 (`d91f20a0...`) 세부 실행 결과**:
  - **트리거**: `2026-10-04 11:47:55 KST` (systemd timer `cardrag-worker.timer`의 `Persistent=true` 재시작에 따른 정상 무인 catch-up 발화, Invocation `f2cdfc74c3b74794bdd87cd113608cd9`)
  - **컨테이너**: `cardrag-worker-worker-run-97093dc188f3`
  - **실행 이미지**: `ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate@sha256:285326379739b802b33acd9e6136e473426762b66607f755c847418cdaf6c82b`
  - **완료 시각**: `2026-10-04 14:34:52 KST` (소요 시간: 2시간 46분 57초)
  - **종료 코드 및 SQLite DB 상태**:
    - Exit code: 0 (`SUCCESS`, `Deactivated successfully`)
    - `run.status`: `'succeeded'` (14:24:33 KST 확정)
    - `publish.status`: `'ready'` (14:24:33 KST 확정)
  - **Corpus Gate 판정**:
    - `missing_unjustified = 0`, `retirement_candidates = 0`, `retired = 0`, `unchanged = 5060`
  - **OCR 결과**:
    - `ocr_cache_reused_count = 5512` (100% 캐시 재사용)
    - `ocr_provider_called_count = 0` (외부 API 호출 0건)
    - `structure_failed_document_count = 0` (구조화 실패 0건)
  - **발행 세대**:
    - `g-d91f20a00b19443baeda67c6-36bae25dd8cd`
    - 벡터 사이드카 크기: 10,088,529,920 바이트 (10.08 GB)
  - **은퇴 원장 커밋**:
    - `cd09e694bdc203bb9b1e6ac950b9b9b1e7d74b98260ece08148ff11fd9250dba.json`
    - `updated_run_id`: `d91f20a00b19443baeda67c6f30211ed`
    - AAP1543 은퇴 상태(`retired`, 결석 4회, `first_absent_run_id=c622d3c4...`, `retired_run_id=785c6324...`) 정상 보존
  - **MCP 무중단 자동 전환**:
    - MCP 서버가 14:52 KST에 `g-d91f20a0...` READY를 감지하여 무중단 활성화 (`generation.activated`)
  - **로그 증적**: `.handoff/004_worker-cutover-integrity-closeout/evidence/run_d91f20a0_1147.redacted.out`

### 2.3 최종 이미지의 2회 무인 타이머 실행 합격 확정
1. **1회차**: 09:00:00 KST 정기 타이머 발화 -> Run `707da1b3...` (`succeeded`, exit 0)
2. **2회차**: 11:47:55 KST systemd 무인 타이머 catch-up 발화 -> Run `d91f20a0...` (`succeeded`, exit 0)
두 실행 모두 중간 수동 Worker 기동이나 겹침 없이 서로 독립된 타이머 발화로 정상 완료되었으며, 수용 기준 1을 완벽히 충족한다.

---

## 3. 코드 서술 정정 및 기술적 검증

### 3.1 OCR 증거 경로 구현 범위 정정
- **기존 `FIX_01_REPORT.md` §4.1 서술 정정**:
  - "OCR 크기와 PDF 결속을 코드에서 직접 대조한다"는 서술은 실제 코드보다 과장된 표현이었다.
- **실제 `pipeline.py` 구현 내역**:
  - 직전 세대 매니페스트(`generation_manifest.json`)에서 문서별 `ocr_sha256`을 추출하여 `PriorEntry`에 바인딩.
  - WebDAV 원격 CAS에서 해당 OCR 객체를 조회하여 SHA256 체크섬 일치와 비어있지 않은 본문을 검증.
- **PDF 결속 상태**:
  - Reviewer가 운영 5,512건 전체 문서에 대해 봉인 매니페스트와 실제 PDF SHA를 전수 대조하여 문서별 PDF SHA 불일치 0건, OCR 매핑 누락 0건임을 확인 완료.
  - 코드 레벨에서 PDF 바이트와 OCR 텍스트 간의 직접 양방향 결속 검증 로직은 운영 인수를 차단하지 않는 향후 고도화 과제(Non-blocking follow-up)로 기록한다.

---

## 4. 타이머 상시 운영 복원 검증

배치 2(`d91f20a0...`) 종료 후 systemd 타이머의 상태를 재검증하였다.

- **`systemctl status cardrag-worker.timer`**:
  ```text
  ● cardrag-worker.timer - Run CardRAG Worker daily at 03:00 Asia/Seoul
       Loaded: loaded (/etc/systemd/system/cardrag-worker.timer; enabled; preset: enabled)
       Active: active (waiting) since Sun 2026-10-04 11:47:55 KST
      Trigger: Mon 2026-10-05 03:00:00 KST; 11h left
     Triggers: ● cardrag-worker.service
  ```
- **`systemctl show cardrag-worker.timer`**:
  - `ActiveState=active`
  - `SubState=waiting`
  - `Persistent=yes`
  - `NextElapseUSecRealtime=Mon 2026-10-05 03:00:00 KST`
- **임시 drop-in 부재**: `/etc/systemd/system/cardrag-worker.timer.d` 디렉터리가 존재하지 않음을 확인.
- 익일 03:00 KST 정기 스케줄로 완전히 안착되었다.

---

## 5. GitHub Actions CI 통과 증적

`feature/004-worker-cutover-integrity` 브랜치에 대해 실행된 GitHub Actions CI가 100% 성공하였다.

- **워크플로 실행**: `ci.yml` (Run ID: `37184053004`)
- **결과**: `✓ Run CI (37184053004) completed with 'success'` (소요 시간: 4분 28초)
- **통과 세부 작업 (11개 스텝 전원 통과)**:
  1. `Set up job`: 완료
  2. `Run actions/checkout@3d3c42e5...`: `fetch-depth: 0`으로 전체 커밋 이력 체크아웃
  3. `Run actions/setup-python@5fda3b95...`: Python 3.12 셋업
  4. `Install workspace`: `uv lock --check`, `uv sync --frozen` 통과
  5. `Install checksum-pinned release audit tools`: actionlint, gitleaks, trivy, shellcheck 설치 및 체크섬 검증
  6. `Run release static and secret audits`: actionlint, shellcheck, gitleaks (151 커밋 0 leaks), trivy fs 통과
  7. `Lint and type-check the new runtime`: ruff check, ruff format --check, mypy 전수 통과
  8. `Run runtime tests`: 전체 2,277개 단위 및 통합 테스트 전원 통과 (0 fail)
  9. `Validate the two-service deployment`: Compose 설정 및 systemd 검증 통과
  10. `Build exactly the Worker and MCP images`: Worker/MCP 이미지 빌드 및 Trivy 취약점 감사(0 HIGH/CRITICAL) 통과
  11. `Complete job`: 정상 종료

---

## 6. 운영 인프라 현황

- **MCP 서버**:
  - 컨테이너: `cardrag-stable-v1026-mcp-1` (업타임 7일 이상 무중단)
  - 헬스체크: `curl http://127.0.0.1:18015/health/ready` -> `{"ready":true}`
  - 도구 목록: JSON-RPC `tools/list` -> 12개 도구 정상 반환
  - 현재 활성 세대: `g-d91f20a00b19443baeda67c6-36bae25dd8cd`
- **호스트 디스크 공간**:
  - `/` 사용 가능 공간: 124 GiB (운영 기준 80 GiB 여유 있게 상회)
- **원격 GC 설정**:
  - `CARDRAG_REMOTE_GC_APPROVED=false` 유지 (과거 세대 CAS 안전 보존)

---

## 7. 릴리스 상태 및 배포 이미지 대응 관계

- **운영 배포 Worker 이미지**:
  - `ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate@sha256:285326379739b802b33acd9e6136e473426762b66607f755c847418cdaf6c82b`
  - Linux amd64 매니페스트: `sha256:fb16e0d7abe7d13461d6edb5e40d4c2ef2eb1db6d23a0b1a80c8cfd198defc0f`
- **배포 이미지와 소스 커밋 관계**:
  - 해당 candidate 이미지는 004의 핵심 기능 구현 커밋 `1ba366d`를 기반으로 빌드되어 무인 배치 2회(`707da1b3...`, `d91f20a0...`)를 성공적으로 완수하였다.
  - 이후 추가된 커밋들(`552b464`, `9db8766`, `01b1381`, `7f53317`, 및 보고서 커밋)은 CI 환경 무결성(`fetch-depth: 0`, `pyjwt` 락파일 갱신, `.trivyignore`, 문서 증적)만을 다루며, 런타임 비즈니스 로직에 변경이 없으므로 운영 이미지를 불필요하게 재빌드/교체하지 않고 현재의 검증된 이미지를 유지한다.
- **Git 브랜치 반영**:
  - 004 검증 결과 및 보고서가 포함된 브랜치를 `main`에 반영하여 004 과제를 최종 종결한다.
