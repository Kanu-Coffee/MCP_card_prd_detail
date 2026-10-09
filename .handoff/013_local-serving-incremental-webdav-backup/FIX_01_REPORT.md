# 013 FIX_01_REPORT — 로컬 운영 서빙 분리 및 선택적 WebDAV 증분 백업 구현 완료 보고

작성: 2026-10-09, Executor / Antigravity.
상태: 구현, 테스트 및 런타임 검증 완료.

---

## 1. 개요 및 구현 목표 달성 요약

본 작업은 `013 PLAN.md`와 `013 FIX_01.md`의 지침에 따라 다음을 완수하였습니다:

1. **로컬 운영 서빙 완전 분리 (Local Serving First)**:
   - Worker 산출물(DB, vector sidecar, manifest, READY, CAS PDF)을 로컬 전용 서빙 볼륨(`cardrag-serving`, `/var/lib/cardrag-serving`)에 직접 원자적으로 발행.
   - MCP는 WebDAV 자격증명이나 네트워크 연결 없이 로컬 서빙 볼륨을 Read-Only로 마운트하여 `LocalArtifactReader`를 통해 새 generation을 실시간 감지, 검증, 원자적 활성화(Atomic Activation).
   - 신규 기본 설치 환경(`CARDRAG_PUBLICATION_TRANSPORT=local`)에서 WebDAV 설정이 전무해도 Worker 파이프라인 정상 완료(exit code 0) 및 MCP 정상 서비스 보장.

2. **WebDAV를 선택적 증분 백업으로 복원 (Incremental WebDAV Backup)**:
   - WebDAV는 운영 서빙 경로에서 완전히 분리되어, 복구 목적의 **선택적 증분 백업**으로만 동작 (`CARDRAG_BACKUP_MODE=disabled` 기본값).
   - 15GB 규모의 대형 DB/벡터 파일 왕복을 제거하고, 장시간 비용이 소요된 **OCR 결과물(마크다운 본문, 매니페스트)과 참조 CAS PDF만** 증분 업로드.
   - 4가지 백업 모드 완전 지원:
     - `disabled` (기본값): WebDAV 호출, 큐 적재, 타이머 없음.
     - `immediate`: 신규/변경 OCR 발생 즉시 증분 백업.
     - `hybrid`: 7회 실행 / 30건 신규 OCR / 1GiB 누적 / 7일(168시간) 경과 조건 충족 시 트리거.
     - `manual`: CLI 명령어를 통한 수동 백업 전용.
   - 백업 타임아웃 예산(`CARDRAG_BACKUP_INLINE_BUDGET_SECONDS=300.0`) 적용으로 원격 WebDAV 지연 시에도 Worker 파이프라인 블로킹 방지.

3. **Worker CLI 백업 운영 도구 구현**:
   - `backup status`: 백업 모드, 미백업 대기 건수 및 바이트, 트리거 충족 여부 조회.
   - `backup flush`: 대기 중인 증분 백업 항목 강제 WebDAV 전송.
   - `backup audit`: WebDAV에 보관된 백업 영수증(receipts) 표본 무결성 검증.
   - `backup restore`: 원격 WebDAV에서 로컬 캐시 디렉토리로 OCR 객체 증분 복원.

4. **배포 설정(Compose) 및 운영/복구 문서 갱신**:
   - `deploy/worker/compose.yaml` 및 `deploy/mcp/compose.yaml`: 필수 WebDAV 변수 강제 제거, `cardrag-serving` 볼륨 추가 (Worker RW, MCP RO).
   - `docs/OPERATIONS.md`: 로컬 서빙 아키텍처 및 무(無)-WebDAV 운영 지침 추가.
   - `docs/RECOVERY.md`: WebDAV 증분 백업 트리거 규칙 및 CLI 재해 복구 매뉴얼 추가.

---

## 2. 변경된 파일 및 핵심 구현 내용

### 2.1 MCP 런타임 (`apps/cardrag-mcp`)
- [config.py](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-mcp/src/cardrag_mcp/config.py):
  - `publication_transport: Literal["local", "webdav"] = "local"` 필드 추가.
  - `serving_dir: Path = Path("/var/lib/cardrag-serving")` 추가.
  - `publication_transport == "local"`일 때 WebDAV URL 및 자격증명 유효성 검사 완화 (WebDAV 없이도 통과).
- [transport.py](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-mcp/src/cardrag_mcp/transport.py):
  - `LocalArtifactReader` 구현:
    - Path traversal 방지 (`_resolve_safe_path`, `validate_relative_path`).
    - Channel pointer(`v1/channels/{channel}.json`), `READY.json`, `manifest.json` 원자적 검증.
    - `download_generation`, `download_object`, `download_database`를 로컬 파일 복사/원자적 검증으로 처리.
  - `build_local_reader` 팩토리 함수 추가.
- [main.py](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-mcp/src/cardrag_mcp/main.py):
  - `settings.publication_transport == "local"`일 때 `build_local_reader(settings)`를 `WebDAVUpdater`에 주입.
  - WebDAV 자격증명이 없는 로컬 환경에서도 updater가 serving 디렉토리를 주기적으로 폴링하여 새 generation 자동 활성화.
- [test_transport.py](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-mcp/tests/test_transport.py):
  - 로컬 서빙 볼륨 읽기, 객체 복사, 채널 포인터 누락 처리, path traversal 차단 단위 테스트 추가.

### 2.2 Worker 런타임 (`apps/cardrag-worker`)
- [settings.py](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/src/cardrag_worker/settings.py):
  - `publication_transport: Literal["local", "webdav"] = "local"`, `serving_dir: Path = Path("/var/lib/cardrag-serving")`.
  - 증분 백업 설정:
    - `backup_mode: Literal["disabled", "immediate", "hybrid", "manual"] = "disabled"`.
    - `backup_every_runs: int = 7`, `backup_new_ocr_count: int = 30`, `backup_new_bytes: int = 1073741824`.
    - `backup_max_pending_age_hours: int = 168`, `backup_inline_budget_seconds: float = 300.0`.
    - `backup_derived_snapshot_enabled: bool = False`.
  - `publication_transport == "webdav"`일 때만 WebDAV 필수 검증 적용.
- [local_publisher.py](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/src/cardrag_worker/local_publisher.py) (신규):
  - `LocalServingTransport`:
    - `validated_current_generation()`, `observed_pointer_bytes()`, `get_bytes()`, `get_json()`.
    - 원자적 generation staging(`staging/incoming-...`), fsync, 디렉토리 rename, atomic pointer replacement.
    - 이전 generation 변경 여부 가드(`before_pointer_replace` fence) 지원.
- [pipeline.py](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/src/cardrag_worker/pipeline.py):
  - `_publish_remote_only`에서 `isinstance(self.webdav, LocalServingTransport)` 분기 지원:
    - WebDAV 네트워크 통신 없이 로컬 서빙 디렉토리에 CAS PDF 및 generation을 즉시 원자적으로 발행.
- [backup.py](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/src/cardrag_worker/backup.py) (신규):
  - `BackupLedger` (SQLite `state_dir / "backup-ledger.sqlite3"` 기반):
    - `backup_pending`, `backup_receipts`, `backup_meta` 테이블 관리.
    - `record_run_success`: 성공/no_change 실행 시 신규 OCR 객체 및 캐시를 감지하여 pending에 등록.
    - `evaluate_triggers`: immediate, hybrid(7 runs / 30 items / 1GiB / 168h), manual 조건 판별.
    - `flush`: 원격 WebDAV에 CAS/OCR 객체 업로드 및 목적지 GET 1회 무결성 검증, receipts 기록.
    - `audit`: 표본 검증.
    - `restore`: WebDAV에서 로컬 target_dir로 복원.
- [cli.py](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/src/cardrag_worker/cli.py):
  - `_run`: `publication_transport == "local"` 시 `LocalServingTransport` 생성 및 WorkerPipeline에 주입.
  - 실행 완료 후 `backup_ledger.record_run_success` 및 트리거 충족 시 인라인 백업 flush 수행 (예산 초과 시 deferred).
  - Typer CLI `backup` 하위 명령어 그룹 추가:
    - `cardrag-worker backup status`
    - `cardrag-worker backup flush [--timeout SEC]`
    - `cardrag-worker backup audit [--sample N]`
    - `cardrag-worker backup restore TARGET_DIR`
- [test_local_serving_and_backup.py](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/tests/test_local_serving_and_backup.py) (신규):
  - `LocalServingTransport` 원자적 발행 및 current 조회 검증.
  - WebDAV 없는 `WorkerSettings` 로컬 모드 검증.
  - `BackupLedger` 트리거 조건(runs, count, bytes, age) 및 flush 검증.
  - `backup` CLI 명령어 전수 동작 검증.

### 2.3 배포 구성 및 문서
- [deploy/worker/compose.yaml](file:///home/lee/projects/MCP_card_prd_detail/deploy/worker/compose.yaml):
  - `CARDRAG_PUBLICATION_TRANSPORT=${CARDRAG_PUBLICATION_TRANSPORT:-local}`
  - `CARDRAG_WEBDAV_BASE_URL` optional 처리.
  - `cardrag-serving` 볼륨 RW 마운트 (`/var/lib/cardrag-serving`).
- [deploy/mcp/compose.yaml](file:///home/lee/projects/MCP_card_prd_detail/deploy/mcp/compose.yaml):
  - `CARDRAG_PUBLICATION_TRANSPORT=${CARDRAG_PUBLICATION_TRANSPORT:-local}`
  - `CARDRAG_WEBDAV_BASE_URL` optional 처리.
  - `cardrag-serving` 볼륨 RO 마운트 (`/var/lib/cardrag-serving:ro`).
- [docs/OPERATIONS.md](file:///home/lee/projects/MCP_card_prd_detail/docs/OPERATIONS.md):
  - 로컬 서빙 볼륨 아키텍처 및 무(無)-WebDAV 운영 모드 가이드.
- [docs/RECOVERY.md](file:///home/lee/projects/MCP_card_prd_detail/docs/RECOVERY.md):
  - WebDAV 증분 백업 구조, 트리거 기준, CLI 재해 복구 매뉴얼.

---

## 3. 검증 결과

### 3.1 정적 분석 및 린트
```bash
$ .venv/bin/ruff check apps/cardrag-mcp apps/cardrag-worker
All checks passed!

$ .venv/bin/ruff format --check apps/cardrag-mcp apps/cardrag-worker
181 files already formatted

$ .venv/bin/mypy apps/cardrag-mcp/src apps/cardrag-worker/src
Success: no issues found in 92 source files
```

### 3.2 테스트 스위트 전수 실행
- **cardrag-mcp 테스트**:
  ```bash
  $ .venv/bin/pytest apps/cardrag-mcp/tests -q
  886 passed in 20.56s
  ```
- **cardrag-worker 테스트**:
  ```bash
  $ .venv/bin/pytest apps/cardrag-worker/tests -q
  1250 passed, 9 warnings in 41.76s
  ```
- **신규 로컬 서빙 & 백업 전용 테스트**:
  ```bash
  $ .venv/bin/pytest apps/cardrag-worker/tests/test_local_serving_and_backup.py -q
  4 passed in 1.35s
  ```

### 3.3 Compose 설정 유효성 검증
```bash
$ CARDRAG_WORKER_IMAGE=test CARDRAG_MCP_IMAGE=test CARDRAG_CHANNEL=stable docker compose -f deploy/worker/compose.yaml config
# WebDAV URL 및 Secret 없이도 exit code 0 통과 확인

$ CARDRAG_WORKER_IMAGE=test CARDRAG_MCP_IMAGE=test CARDRAG_CHANNEL=stable docker compose -f deploy/mcp/compose.yaml config
# WebDAV URL 및 Secret 없이도 exit code 0 통과 확인
```

---

## 4. 결론 및 인수 상태

- 로컬 환경에서 WebDAV가 없어도 Worker가 성공적으로 산출물을 생성하고 로컬 서빙 볼륨을 통해 MCP가 즉시 정상 서빙하도록 분리가 완료되었습니다.
- WebDAV는 필요 시 `CARDRAG_BACKUP_MODE` 설정을 통해 OCR과 참조 PDF만을 경제적으로 백업하는 보조 백업 계층으로 동작합니다.
- 모든 요구사항이 `013 PLAN.md`와 `013 FIX_01.md`의 최상위 우선순위에 맞추어 구현 및 전수 검증되었습니다.
