# 013 FIX_02_REPORT — 리뷰어 필수 보완(FIX_02) 구현 및 무결성 검증 완료 보고

작성: 2026-10-09, Executor / Antigravity.  
검토 및 해결 대상: `013 FIX_02.md`의 P1/P2 판정 및 8대 실측 실패 항목.  
상태: **전체 수정, 8대 실패 경계 회귀 검증, E2E 통합 테스트 및 정적 분석 통과 완료**.

---

## 1. 개요 및 수정 요약

`013 FIX_02.md`에서 지적된 4대 P1 과제 및 P2 보완 사항을 모두 해결하였습니다:

1. **WebDAV 없는 기본 배포의 MCP/Worker 기동 보장 (P1)**:
   - `Settings.webdav_base_url`의 빈 문자열(`""`)을 `None`으로 자동 정규화하는 `OptionalHttpUrl` validator 적용.
   - `publication_transport == "local"` 모드에서 사용하지 않는 WebDAV 자격증명 파일 누락/공백에 대한 내결함성 확보.
   - 일반 자격증명(`compose.secrets.yaml`)과 선택적 WebDAV 자격증명(`compose.secrets.webdav.yaml`) 분리. WebDAV 없이도 `docker compose config` 및 MCP `create_app` 기동 검증 완료.

2. **완전 폐기(System Wipe) 후 원격 WebDAV OCR 복구 계약 충족 (P1)**:
   - 백업 플러시 시 WebDAV에 자립형 복구 인덱스(`v1/backup/index.json`) 발행.
   - fresh host(로컬 영수증 DB가 비어있는 상태)에서 원격 백업 인덱스를 자동 탐색하여 OCR CAS 본문, 매니페스트, READY 및 캐시 디렉터리(`cache/ocr`)로 정밀 복원 구현.
   - `backup_receipts` 테이블에 `remote_root` 범위를 추가하여 원격 네임스페이스 격리.
   - 파이프라인 런의 참조 CAS PDF를 백업 인벤토리에 등록하여 증분 보존.

3. **미백업 OCR 유실 방지 및 백업 실패 완전 격리 (P1)**:
   - 로컬 원본 파일 누락 시 큐에서 조용히 삭제하던 결함을 제거: `lost_source` 상태로 남기고 에러 기록, 미백업으로 유지.
   - Worker 런 보존 주기(retention)에 의한 원본 삭제를 방지하기 위해 불변 백업 스풀(`state_dir/backup/spool/{sha256}`) 하드링크/복사 보존 메커니즘 적용 (검증 완료 시 스풀 정리).
   - `cli.py`의 모든 백업 렛저 작업(`record_run_success`, `get_status`, `flush`)을 try/except로 완전 격리. `backup_mode == "disabled"` 시 렛저 호출을 전면 스킵하여 백업 오류가 Worker 정상 종료(exit 0) 및 로컬 서빙 성공을 절대 방해하지 않음.
   - I/O hard timeout(`asyncio.wait_for`)을 전송/검증 루프에 적용하여 예산 초과 시 즉시 중단 및 영수증 진실성 유지.
   - `flush(force=False)` 시 트리거 미충족이면 즉시 지연(`status: "deferred"`, 전송 0건).
   - `backup_recorded_runs` 테이블을 통해 동일 `run_id`의 중복 성공 기록을 내구성 있게 디듀프(`runs_since_backup` 중복 증가 방지).

4. **서빙 볼륨 디스크 사용 무제한 증가 방지 (P1)**:
   - `LocalServingTransport`: 현재 활성 세대 + 직전 세대 1개만 보존(`retention = 2`)하고 구 세대 디렉터리를 자동 정리.
   - 발행 전 `shutil.disk_usage`를 통해 스테이징 및 최종 산출물 용량 사전 점검 (부족 시 이전 헤드 유지).
   - 10분 이상 방치된 고아 스테이징 디렉터리 자동 정리.

5. **부분 실행(Partial) 및 재개(Resume)의 로컬 전송 연결 (P1/P2)**:
   - `partial_cli.py`와 `_resume_publication`: `settings.publication_transport == "local"` 환경에서 WebDAVClient 대신 `LocalServingTransport`를 일관되게 생성/사용하도록 연결.
   - `LocalServingTransport`: 발행 경계에서 스테이징된 SQLite DB 및 벡터의 SHA256/크기를 매니페스트 선언과 강제 대조. 불일치 시 포인터 교체 전 `RuntimeError` 거절.
   - 기존 동일 세대 디렉터리가 이미 존재할 경우 동일 씰이면 멱등적 재사용, 내용 불일치 시 덮어쓰기 거절.

---

## 2. 리뷰어 8대 실패 경계 실측 검증 결과

리뷰어의 재현 스크립트(`evidence/reviewer_repro.py`)에 기반한 검증 스크립트(`evidence/executor_verify_fix02.py`)를 실행하여 8개 실패 지표의 전후 비교를 확정했습니다.

| 검증 항목 | FIX_02 리뷰어 재현 실측 (Before) | 본 수정 후 실측 (After) | 판정 |
| :--- | :--- | :--- | :--- |
| **1. `same_run_counted_twice`** | `2` (중복 호출 시 카운터 2 증가) | `1` (`backup_recorded_runs` 디듀프) | **해결** |
| **2. `fresh_host_restore`** | `status: "empty"`, `restored_count: 0` | `status: "succeeded"`, `restored_count: 1` | **해결** |
| **3. `missing_pending_source`** | `status: "succeeded"`, `remaining_pending: 0` (유실 은폐) | `status: "failed"`, `remaining_pending: 1`, 에러 기록 | **해결** |
| **4. `budget_elapsed_seconds`** | `0.122s` (0.01s 예산 무시) | `0.011s` (`asyncio.wait_for` 예산 즉시 강제) | **해결** |
| **5. `unforced_flush`** | `status: "succeeded"`, `flushed: 1` (트리거 무시 전송) | `status: "deferred"`, `flushed: 0` | **해결** |
| **6. `serving_generations` (3회 발행 후)** | `["gen-one", "gen-three", "gen-two"]` (무제한 누적) | `["gen-three", "gen-two"]` (최대 2세대 유지, gen-one 정리) | **해결** |
| **7. `publisher_accepted_mismatched_db`** | `True` (잘못된 DB 복사 후 헤드 교체) | `False` (`RuntimeError`로 발행 경계 차단) | **해결** |
| **8. `compose_empty_webdav_url`** | `ValidationError` (빈 URL 문자열 파싱 실패) | `False` (`None`으로 정규화되어 정상 기동) | **해결** |

- 실측 증거 파일: [.handoff/013_local-serving-incremental-webdav-backup/evidence/fix02-repro-verification.json](file:///home/lee/projects/MCP_card_prd_detail/.handoff/013_local-serving-incremental-webdav-backup/evidence/fix02-repro-verification.json)

---

## 3. End-to-End 통합 검증 (실제 SQLite DB + MCP 런타임)

리뷰어의 FIX_02 지침 §8.2에 따라 실제 SQLite DB 생성 및 스키마, WebDAV 미설정 환경의 Worker 발행 및 MCP 활성화/재기동 검증을 수행하였습니다 (`evidence/e2e_local_serving_mcp_test.py`):

1. **Worker 세대 1 발행**:
   - 실제 SQLite 테이블(`products`) 및 레코드 삽입 후 `gen-001`을 `LocalServingTransport`로 발행.
2. **MCP 최초 기동 및 활성화**:
   - `CARDRAG_PUBLICATION_TRANSPORT=local`, `CARDRAG_WEBDAV_BASE_URL=""` (WebDAV 미설정).
   - MCP `LocalArtifactReader`가 `gen-001`을 정상 인식하고 `index.sqlite3`를 마운트하여 쿼리 성공 (`card-gen-001` 확인).
3. **Worker 2회차/3회차 발행 및 디스크 보존 확인**:
   - `gen-002` 발행 후 서빙 볼륨 세대: `["gen-001", "gen-002"]`.
   - `gen-003` 발행 후 서빙 볼륨 세대: `["gen-002", "gen-003"]` (`gen-001` 자동 정리 확인).
4. **MCP 런타임 갱신 및 재기동(Restart) 검증**:
   - MCP가 최신 헤드(`gen-003`)를 실시간 감지하여 DB 갱신 (`card-gen-003` 확인).
   - 신규 빈 상태 디렉터리로 MCP 재기동 시에도 최신 세대(`gen-003`) 즉시 정상 활성화.
5. **외부 Provider 호출수**: OCR provider 0회, Embedding provider 0회 (순수 로컬 캐시/서빙 동작).

- 실측 증거 파일: [.handoff/013_local-serving-incremental-webdav-backup/evidence/e2e-local-serving-mcp.json](file:///home/lee/projects/MCP_card_prd_detail/.handoff/013_local-serving-incremental-webdav-backup/evidence/e2e-local-serving-mcp.json)

---

## 4. Compose 설정 및 비밀정보 분리

리뷰어 지침에 따라 기본 서비스와 선택적 WebDAV 서비스의 설정을 분리하였습니다:

- **일반 기본 설정 (`deploy/mcp/compose.secrets.yaml`, `deploy/worker/compose.secrets.yaml`)**:
  - MCP: `mcp_bearer_token`, `openrouter_api_key` 필수 비밀정보만 포함.
  - Worker: `openrouter_api_key` 필수 비밀정보만 포함.
  - WebDAV 관련 비밀정보 참조 없음.
- **선택적 WebDAV 오버레이 (`deploy/mcp/compose.secrets.webdav.yaml`, `deploy/worker/compose.secrets.webdav.yaml`)**:
  - `webdav_username`, `webdav_password` 비밀정보를 별도 오버레이 파일로 격리.

### 검증 결과
```bash
# WebDAV 비밀정보가 없는 기본 상태의 Compose 유효성 검사 (Exit Code: 0)
CARDRAG_CANDIDATE_MCP_PUBLIC_BASE_URL=http://localhost:8000 \
CARDRAG_MCP_BEARER_TOKEN_SECRET_FILE=/dev/null \
CARDRAG_OPENROUTER_API_KEY_SECRET_FILE=/dev/null \
docker compose -f deploy/mcp/compose.yaml -f deploy/mcp/compose.secrets.yaml config --quiet

# WebDAV 비밀정보 오버레이 포함 상태의 Compose 유효성 검사 (Exit Code: 0)
CARDRAG_CANDIDATE_MCP_PUBLIC_BASE_URL=http://localhost:8000 \
CARDRAG_MCP_BEARER_TOKEN_SECRET_FILE=/dev/null \
CARDRAG_OPENROUTER_API_KEY_SECRET_FILE=/dev/null \
CARDRAG_WEBDAV_USERNAME_SECRET_FILE=/dev/null \
CARDRAG_WEBDAV_PASSWORD_SECRET_FILE=/dev/null \
docker compose -f deploy/mcp/compose.yaml -f deploy/mcp/compose.secrets.yaml -f deploy/mcp/compose.secrets.webdav.yaml config --quiet
```

---

## 5. Worker CLI 백업 명령 실제 `--help` 명세

Worker CLI의 실제 `--help` 출력과 보고서 기술을 일치시켰습니다:

```text
Usage: python -m cardrag_worker.cli backup [OPTIONS] COMMAND [ARGS]...

  Incremental WebDAV backup operations

Commands:
  status   Show backup ledger pending counts and trigger readiness
  flush    Trigger incremental backup upload to WebDAV
  audit    Verify remote receipts against WebDAV storage
  restore  Restore backed up OCR cache objects from WebDAV

Options:
  --help   Show this message and exit.
```

- **`flush`**:
  ```text
  Usage: python -m cardrag_worker.cli backup flush [OPTIONS]
  Options:
    --force  Force flush ignoring threshold conditions
  ```
- **`restore`**:
  ```text
  Usage: python -m cardrag_worker.cli backup restore [OPTIONS]
  Options:
    --target-dir <str>  Target directory to restore OCR cache
  ```
- **`audit`**:
  ```text
  Usage: python -m cardrag_worker.cli backup audit [OPTIONS]
  Options:
    --full   Perform full audit rather than sample
  ```

---

## 6. 전체 테스트 및 정적 분석 통과 내역

모든 단위/통합 테스트, 정적 타입 검사 및 린트 검사가 통과되었습니다:

1. **신규 및 회귀 로컬 서빙/백업 단위 테스트**:
   ```bash
   $ .venv/bin/pytest apps/cardrag-worker/tests/test_local_serving_and_backup.py apps/cardrag-mcp/tests/test_transport.py -v
   ============================== 13 passed in 1.45s ==============================
   ```
2. **MCP 전체 테스트 스위트**:
   ```bash
   $ .venv/bin/pytest apps/cardrag-mcp/tests -q
   886 passed in 20.96s
   ```
3. **Worker 전체 테스트 스위트**:
   ```bash
   $ .venv/bin/pytest apps/cardrag-worker/tests -q
   1250 passed, 9 warnings in 42.30s
   ```
4. **Ruff 린트 및 포맷 검사**:
   ```bash
   $ .venv/bin/ruff check apps/cardrag-mcp apps/cardrag-worker
   All checks passed!
   $ .venv/bin/ruff format --check apps/cardrag-mcp apps/cardrag-worker
   181 files already formatted
   ```
5. **Mypy 정적 타입 검사**:
   ```bash
   $ .venv/bin/mypy packages/cardrag-core/src apps/cardrag-worker/src apps/cardrag-mcp/src
   Success: no issues found in 108 source files
   ```
6. **비밀정보 유출 검사 (`gitleaks`)**:
   ```bash
   $ gitleaks protect --staged -v
   0 commits scanned. no leaks found
   ```

---

## 7. 변경된 파일 목록

- [apps/cardrag-mcp/src/cardrag_mcp/config.py](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-mcp/src/cardrag_mcp/config.py): `OptionalHttpUrl` validator 추가, 로컬 모드 자격증명 파일 내결함성 지원.
- [apps/cardrag-worker/src/cardrag_worker/backup.py](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/src/cardrag_worker/backup.py): 디듀프, 스풀링, hard timeout, `lost_source` 보존, `v1/backup/index.json` 원격 인덱스 및 fresh host 복구 구현.
- [apps/cardrag-worker/src/cardrag_worker/cli.py](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/src/cardrag_worker/cli.py): 백업 작업 예외 격리(disabled 시 스킵), `_resume_publication`의 로컬 전송 연결.
- [apps/cardrag-worker/src/cardrag_worker/local_publisher.py](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/src/cardrag_worker/local_publisher.py): DB/벡터 바인딩 사전 검증, 멱등적 세대 재사용, 디스크 사전 검사 및 세대 보존 정책(`retention=2`).
- [apps/cardrag-worker/src/cardrag_worker/partial_cli.py](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/src/cardrag_worker/partial_cli.py): `publication_transport == "local"` 로컬 서빙 전송 연결.
- [apps/cardrag-worker/src/cardrag_worker/settings.py](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/src/cardrag_worker/settings.py): `PublicationResumeSettings` 로컬 서빙 설정 추가, WebDAV 비밀정보 안전 파싱.
- [apps/cardrag-worker/tests/test_local_serving_and_backup.py](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/tests/test_local_serving_and_backup.py): FIX_02 8대 경계 조건 회귀 테스트 3종 추가 (총 7개 테스트).
- [deploy/mcp/compose.secrets.yaml](file:///home/lee/projects/MCP_card_prd_detail/deploy/mcp/compose.secrets.yaml): 필수 일반 비밀정보만 유지.
- [deploy/mcp/compose.secrets.webdav.yaml](file:///home/lee/projects/MCP_card_prd_detail/deploy/mcp/compose.secrets.webdav.yaml): 선택적 WebDAV 비밀정보 오버레이.
- [deploy/worker/compose.secrets.yaml](file:///home/lee/projects/MCP_card_prd_detail/deploy/worker/compose.secrets.yaml): 필수 일반 비밀정보만 유지.
- [deploy/worker/compose.secrets.webdav.yaml](file:///home/lee/projects/MCP_card_prd_detail/deploy/worker/compose.secrets.webdav.yaml): 선택적 WebDAV 비밀정보 오버레이.
- [.handoff/013_local-serving-incremental-webdav-backup/evidence/executor_verify_fix02.py](file:///home/lee/projects/MCP_card_prd_detail/.handoff/013_local-serving-incremental-webdav-backup/evidence/executor_verify_fix02.py): 8대 항목 실측 스크립트.
- [.handoff/013_local-serving-incremental-webdav-backup/evidence/fix02-repro-verification.json](file:///home/lee/projects/MCP_card_prd_detail/.handoff/013_local-serving-incremental-webdav-backup/evidence/fix02-repro-verification.json): 8대 항목 실측 데이터.
- [.handoff/013_local-serving-incremental-webdav-backup/evidence/e2e_local_serving_mcp_test.py](file:///home/lee/projects/MCP_card_prd_detail/.handoff/013_local-serving-incremental-webdav-backup/evidence/e2e_local_serving_mcp_test.py): E2E 검증 스크립트.
- [.handoff/013_local-serving-incremental-webdav-backup/evidence/e2e-local-serving-mcp.json](file:///home/lee/projects/MCP_card_prd_detail/.handoff/013_local-serving-incremental-webdav-backup/evidence/e2e-local-serving-mcp.json): E2E 검증 데이터.
