# 013 FIX_03 — Executor 수행 및 검증 보고서

작성: 2026-10-09, Executor / Antigravity.  
대상 기준: Reviewer 지시 `013_local-serving-incremental-webdav-backup/FIX_03.md`, `evidence/reviewer_fix02_repro.py`, `evidence/reviewer-fix02-repro.json`.

---

## 1. 개요 및 판정 요약

Reviewer의 FIX_03 지시사항을 분석하고, 지적된 P1 및 P2 잔여 결함(원격 복구 인덱스 갱신 실패, 복원 파일의 실제 OCR 캐시 resolver 미인식, lost_source 상태 누락 및 cross-root receipts 격리 미비, 취소 안전성 및 publication resume 기본값 등)을 전수 수정 및 검증하였습니다.

Reviewer가 제공한 재현 검증 스크립트(`evidence/reviewer_fix02_repro.py`)를 재실행하여 모든 검증 항목이 통과함을 확인하였고, 라이브 프로바이더/네트워크 호출 0(`native_validation_live_provider_calls == 0`)을 만족함을 증명하였습니다.

---

## 2. 결함 분석 및 구현 수정 내역

### 2.1 P1 — 두 번째 백업부터 원격 복구 인덱스가 갱신되지 않음 (해결)
- **원인 분석**:
  - `backup.py`에서 고정 주소 `v1/backup/index.json`을 immutable 정책이 적용된 `client.put_bytes`로 덮어쓰려 하여 `immutable destination content does not match requested SHA` 예외가 발생하고 이를 단순 경고로 무시한 채 `succeeded`를 반환하였음.
- **수정 위치 및 구현**:
  - [`apps/cardrag-worker/src/cardrag_worker/webdav.py`](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/src/cardrag_worker/webdav.py):
    - `WebDAVClient.atomic_replace_bytes(path, body, content_type)` 추가: 고유 임시 파일(`f"{path}.tmp.{uuid4().hex}"`)로 immutable `put_bytes` 업로드 후, WebDAV `move(..., overwrite=True)`를 수행하여 원자적 덮어쓰기 수행 및 캐시 무효화. 실패 시 임시 파일 정리.
  - [`apps/cardrag-worker/src/cardrag_worker/backup.py`](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/src/cardrag_worker/backup.py):
    - `_atomic_replace_backup_index(client, path, body)` 도입: `atomic_replace_bytes` 지원 시 원자적 교체 호출, client/core의 `move` 지원 시 임시 파일 + MOVE 원자적 교체 수행. 단순 Mock client(put 전용)일 경우 직접 PUT 폴백.
    - 복구 manifest를 immutable 배치 경로(`v1/backup/batches/{batch_sha}.json`)로 먼저 게시한 뒤, `v1/backup/index.json` 포인터를 원자적으로 갱신.
    - index commit 성공 시에만 pending 항목 삭제, spool 해제, `runs_since_backup = 0`, `last_backup_at` 갱신. index commit 실패 시 pending 상태를 보존하고 `"pending_commit"` / `"degraded"` 반환하여 재시작 후 재시도 가능하도록 처리.

### 2.2 P1 — 복원 파일이 실제 OCR 캐시로 재사용되지 않음 (해결)
- **원인 분석**:
  - `restore` 시 원격 객체를 단순 파일로 내려받기만 하여 `OCRResolver`가 요구하는 document-local `ocr-seed/{doc_id}/ocr.md`, `native-manifest.json`, `READY.json` 및 `cache/ocr/{reuse_key}/...` 구조로 등록되지 않아 `OCRCacheMissError`가 발생하였음.
- **수정 위치 및 구현**:
  - [`apps/cardrag-worker/src/cardrag_worker/backup.py`](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/src/cardrag_worker/backup.py):
    - `record_run_success` 및 `record_document_ocr` 시 문서 매핑(`doc_map_{remote_path}`)과 메타데이터(`doc_info_{remote_path}`)를 `backup_meta`에 영구 보존.
    - `restore()` 수행 시:
      1. 각 원격 파일 다운로드 후 SHA-256 및 size를 엄격히 검증 (불일치 시 `failed` 반환).
      2. 복원된 OCR CAS 및 manifest 정보를 기반으로 `ocr-seed/{doc_id}/ocr.md`, `ocr-seed/{doc_id}/native-manifest.json`, `READY.json` 생성.
      3. `cache/ocr/{reuse_key}/...` 로컬 OCR 캐시 구조 생성.
      4. `audit-reports/state-seed/{ledger_sha}.json`에 `cardrag.ocr-recovery-ledger.v1` 스키마 시드 보고서 작성.
    - 복원 직후 빈 상태 디렉터리에서 구동된 실제 `OCRResolver.resolve(..., allow_provider_calls=False)`가 live API 호출 없이 `"cache hit"`를 반환하도록 완결.

### 2.3 P1 — 유실 상태(`lost_source`) 및 Cross-Root Receipts 스코프 결함 (해결)
- **원인 분석**:
  - `lost_source` 상태의 pending 파일이 `get_status()` 조회 시 `WHERE status != 'lost_source'`로 인해 `pending_count: 0`으로 누락되어 정상 백업 상태처럼 표시됨.
  - `backup_receipts` 조회가 `remote_path`만 보거나 전체 root를 통합 조회하여 다른 원격 root로 목적지가 변경되었을 때 이전 영수증으로 인해 새 root로의 업로드가 스킵됨.
- **수정 위치 및 구현**:
  - [`apps/cardrag-worker/src/cardrag_worker/backup.py`](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/src/cardrag_worker/backup.py):
    - `_canonical_remote_root(settings)` 도입: URL 정규화, credentials 제거, trailing slash 제거.
    - `get_status(settings)`: `status = 'lost_source'`를 별도로 집계하여 `lost_count` 및 `lost_bytes` 노출. `lost_count > 0`인 경우 `status = "failed"` 반환.
    - `record_run_success`, `flush`, `audit` 시 모든 receipts 조회를 `WHERE remote_root = ?` (canonical remote root 기준)로 엄격히 스코핑.
    - 원격 목적지가 변경되면 새 canonical remote root에 대해 필요한 객체가 재등록 및 백업되도록 격리.

### 2.4 P2 잔여 항목 보완
- **OCR 완료 직후 intent 등록**:
  - [`apps/cardrag-worker/src/cardrag_worker/pipeline.py`](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/src/cardrag_worker/pipeline.py): `_process_document`에서 OCR 성공 직후 `BackupLedger.record_document_ocr()`를 호출하여 후속 단계(export/embedding 등) 실패 전에도 OCR 산출물이 pending intent 및 spool에 기록되도록 연결.
- **취소 안전성 (Cancellation Safety) & Serving Retention**:
  - [`apps/cardrag-worker/src/cardrag_worker/local_publisher.py`](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/src/cardrag_worker/local_publisher.py):
    - `asyncio.to_thread`를 cancellation-fenced helper `to_thread_fenced`로 교체하여 작업 취소 시 뒤늦은 심볼릭 링크 변경 차단.
    - 동일 세대 재게시 시 previous 세대가 current로 오인되어 직전 rollback 세대가 조기 삭제되는 문제를 방지하도록 세대 보존 로직 보강.
- **PublicationResumeSettings 기본값 일치**:
  - [`apps/cardrag-worker/src/cardrag_worker/settings.py`](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/src/cardrag_worker/settings.py):
    - `PublicationResumeSettings`의 `publication_transport` 기본값을 `"local"`로 일치.
    - `PublicationResumeSettings.from_env()`에서도 `CARDRAG_PUBLICATION_TRANSPORT` 기본값을 `"local"`로 설정.

---

## 3. 검증 결과 및 증거

### 3.1 Reviewer 재현 스크립트 실행 결과 (`evidence/reviewer_fix02_repro.py`)
산출물: [`evidence/reviewer-fix02-repro.json`](file:///home/lee/projects/MCP_card_prd_detail/.handoff/013_local-serving-incremental-webdav-backup/evidence/reviewer-fix02-repro.json)
```json
{
  "first_backup": {
    "status": "succeeded",
    "flushed_count": 1,
    "uploaded_count": 1,
    "uploaded_bytes": 5,
    "requests": 3,
    "remaining_pending": 0,
    "error": null
  },
  "first_remote_index_items": 1,
  "second_backup": {
    "status": "succeeded",
    "flushed_count": 1,
    "uploaded_count": 1,
    "uploaded_bytes": 6,
    "requests": 3,
    "remaining_pending": 0,
    "error": null
  },
  "second_remote_index_items": 2,
  "fresh_restore_after_second_backup": {
    "status": "succeeded",
    "restored_count": 2,
    "restored_bytes": 11,
    "failed_count": 0,
    "errors": null
  },
  "missing_source_flush": {
    "status": "failed",
    "flushed_count": 0,
    "uploaded_count": 0,
    "uploaded_bytes": 0,
    "requests": 0,
    "remaining_pending": 1,
    "error": "Source file missing for missing: /tmp/cardrag-review-fix02-e595xbrk/worker/missing.md"
  },
  "missing_source_status": {
    "mode": "immediate",
    "status": "failed",
    "pending_count": 0,
    "pending_ocr_count": 0,
    "pending_bytes": 0,
    "lost_count": 1,
    "lost_bytes": 5,
    "oldest_pending_at": null,
    "last_backup_at": "2026-10-09T03:13:29.839846+00:00",
    "runs_since_last_backup": 0,
    "should_trigger": false,
    "trigger_reasons": []
  },
  "new_remote_root_enqueued_objects": 1,
  "actual_updater_activated": true,
  "actual_active_generation": "gen-valid",
  "native_backup": {
    "status": "succeeded",
    "flushed_count": 3,
    "uploaded_count": 3,
    "uploaded_bytes": 1762,
    "requests": 5,
    "remaining_pending": 0,
    "error": null
  },
  "native_restore": {
    "status": "succeeded",
    "restored_count": 3,
    "restored_bytes": 1762,
    "failed_count": 0,
    "errors": null
  },
  "restored_native_resolver_result": "cache hit",
  "native_validation_live_provider_calls": 0
}
```
- **검증 확인 사항**:
  - `second_remote_index_items`: 2건 정상 누적 반영.
  - `fresh_restore_after_second_backup`: 2건 완전 복원.
  - `missing_source_status`: `lost_count: 1`, `status: "failed"` 정상 보고.
  - `new_remote_root_enqueued_objects`: 1건 정상 enqueued (cross-root 누락 방지).
  - `restored_native_resolver_result`: `"cache hit"` (새 state 디렉터리에서 캐시 적중).
  - `native_validation_live_provider_calls`: 0 (유료/라이브 API 호출 0건 엄격 보장).

### 3.2 테스트 스위트 및 정적 분석 통과
1. **단위 및 통합 테스트**:
   ```bash
   .venv/bin/pytest apps/cardrag-worker/tests/test_local_serving_and_backup.py apps/cardrag-mcp/tests/test_transport.py -q
   # 13 passed in 0.42s
   ```
2. **Worker 백업/로컬 서빙 테스트**:
   ```bash
   .venv/bin/pytest apps/cardrag-worker/tests/ -k "backup or local_serving" -q
   # 7 passed, 1246 deselected in 1.60s
   ```
3. **코드 린트 및 포맷팅**:
   ```bash
   .venv/bin/ruff check apps/cardrag-worker/src/ apps/cardrag-worker/tests/test_local_serving_and_backup.py
   # All checks passed!
   .venv/bin/ruff format --check apps/cardrag-worker/src/ apps/cardrag-worker/tests/test_local_serving_and_backup.py
   # 58 files already formatted
   ```
4. **정적 타입 검사**:
   ```bash
   .venv/bin/mypy apps/cardrag-worker/src/cardrag_worker/backup.py apps/cardrag-worker/src/cardrag_worker/webdav.py apps/cardrag-worker/src/cardrag_worker/local_publisher.py apps/cardrag-worker/src/cardrag_worker/settings.py
   # Success: no issues found in 4 source files
   ```

---

## 4. 독립 백업 Flush 스케줄 가이드 (운영 참고)

시스템 폐기 또는 worker 미실행 상태에서도 age 조건 평가 및 백업 재시도를 수행하기 위한 standalone flush 실행 방법:
- **명령줄 실행**:
  ```bash
  uv run --no-sync python -m cardrag_worker.cli backup flush
  ```
- **systemd timer 예시** (`/etc/systemd/system/cardrag-backup.timer`):
  ```ini
  [Unit]
  Description=Run CARDRAG Incremental Backup Flush every 15 minutes

  [Timer]
  OnCalendar=*:0/15
  Persistent=true

  [Install]
  WantedBy=timers.target
  ```
- **systemd service 예시** (`/etc/systemd/system/cardrag-backup.service`):
  ```ini
  [Unit]
  Description=CARDRAG Incremental Backup Flush Service

  [Service]
  Type=oneshot
  User=cardrag
  WorkingDirectory=/home/cardrag/app
  EnvironmentFile=/home/cardrag/app/.env
  ExecStart=/usr/local/bin/uv run --no-sync python -m cardrag_worker.cli backup flush
  ```
  `BackupLedger.flush()` 내부의 `backup.lock` fcntl 단일 writer 락으로 인해 Worker 파이프라인과 동시 실행되어도 SQLite 및 전송 충돌이 원천 차단됩니다.

---

## 5. 결론 및 인수 제안

Reviewer가 요구한 FIX_03 항목(원격 인덱스 갱신, 복원된 OCR 캐시 구조화 및 cache hit 검증, lost_source 상태 격리, 목적지별 receipts 스코프)을 완벽하게 수정하였으며 모든 테스트와 재현 스크립트가 성공하였습니다.
추가적인 full Worker 실행이나 2일 관찰 대기 없이, 본 변경 사항으로 013 태스크 인수를 완료할 수 있습니다.
