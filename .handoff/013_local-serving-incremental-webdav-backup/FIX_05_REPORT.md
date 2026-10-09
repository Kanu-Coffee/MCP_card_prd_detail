# 013 FIX_05_REPORT — 백업 트래픽 분리 및 부분 배치 commit 예산 보장

작성일: 2026-10-09
작성자: Executor (Antigravity)
관련 결함 보고: `.handoff/013_local-serving-incremental-webdav-backup/FIX_05.md`
대상 브랜치: `codex/013-local-serving-incremental-webdav-backup`

---

## 1. 개요 및 배경

2026-10-09 16:10~16:15 KST 운영 관측에서 `cardrag-backup`의 WebDAV 트래픽이 집중되고, 16:12:28 flush에서 `pending_commit` 상태 및 timeout이 발생하였다.
원인 분석 결과:
1. `backup.py`의 `flush` 메서드가 전체 timeout(예: 300초)을 개별 객체 업로드 및 검증에 전부 소진한 뒤, 원격 batch/index commit 단계에는 `max(1, remaining)`만 남겨 원자적 원격 교체가 완료되기 전에 타임아웃이 발생하는 구조적 결함이 있었다.
2. `uploaded_bytes/count`가 실제 네트워크 PUT 바이트가 아니라 처리된 대상 객체의 `expected_size`를 단순 합산하여, 기존 객체 재사용/HEAD 검증까지 신규 업로드 바이트로 왜곡 보고되는 문제가 있었다.
3. commit 실패 시 안전하게 pending과 spool이 보존되어야 하며, 다음 retry 시 이미 검증된 동일 canonical remote root/hash/size 객체에 대해 불필요한 HEAD/GET/PUT 호출 없이 index 확정에 재사용되어야 했다.

---

## 2. 주요 변경 사항

### 2.1 객체 처리 예산과 Index Commit 예산 분리 (`COMMIT_RESERVE_SECONDS`)
- **파일**: `apps/cardrag-worker/src/cardrag_worker/backup.py`
- `COMMIT_RESERVE_SECONDS = 45.0` 상수를 도입.
- `data_budget_seconds`를 `max(10.0, timeout_seconds - COMMIT_RESERVE_SECONDS)` (단, 짧은 타임아웃의 경우 `max(0.01, timeout_seconds * 0.5)`)로 분리 계산.
- 객체 처리 루프 진입 시 경과 시간을 측정하여 `data_budget_seconds` 도달 시 루프를 즉시 break하고, 예약된 시간 동안 원격 batch manifest 게시 및 `v1/backup/index.json` 원자 교체를 완료하도록 보장.
- 이를 통해 부분 진행 배치라도 안전하게 index commit을 확정하고 pending 항목을 정리할 수 있게 됨.

### 2.2 동일 Remote Root Verified Receipts 재사용 (0 GET/PUT 재시도)
- **파일**: `apps/cardrag-worker/src/cardrag_worker/backup.py`
- 루프 시작 전 현재 `canonical_remote_root`에 대해 이미 성공적으로 검증된 영수증 목록(`backup_receipts`)을 조회.
- pending 항목 중 이미 동일 `remote_path`, `sha256`, `size_bytes`로 검증된 항목은 네트워크 I/O(GET/PUT) 없이 즉시 이번 배치의 검증 목록(`verified_in_this_batch`)으로 채택.
- commit이 완료되기 전에는 pending 및 spool 파일을 삭제하지 않음.
- index commit 실패 시 pending/spool이 온전히 보존되며, 다음 flush 재시도 시 이미 검증된 객체는 0 네트워크 요청으로 즉시 commit 배치에 합류함.

### 2.3 지표 분리 및 정확한 보고
- **파일**: `apps/cardrag-worker/src/cardrag_worker/backup.py`
- 기존 consumer와의 호환성을 유지하면서 실제 물리적 전송량과 재사용량을 명확히 분리하여 반환:
  - `actual_uploaded_count`: 실제 원격 WebDAV로 전송된 신규 객체 수.
  - `actual_uploaded_bytes`: 실제 원격 WebDAV로 전송된 신규 바이트 수.
  - `receipts_reused_count` / `verified_existing_count`: 기존 영수증 일치로 전송 생략된 객체 수.
  - `receipts_reused_bytes` / `verified_existing_bytes`: 재사용된 바이트 수.
  - `processed_count`: 이번 배치에서 처리 확정된 총 객체 수 (`len(verified_in_this_batch)`).
  - `processed_bytes`: 이번 배치에서 처리 확정된 총 바이트 수.
  - `uploaded_count` / `uploaded_bytes`: 하위 호환 필드 유지.

---

## 3. 검증 결과

### 3.1 회귀 및 신규 단위 테스트
- **테스트 파일**: `apps/cardrag-worker/tests/test_fix_05_06_local_content_and_backup.py`
  - `test_backup_budget_reserve_commits_partial_batch_and_retry_zero_requests`:
    - 느린 Mock WebDAV(업로드 지연 주입) 환경에서 작은 timeout 부여 시, data budget 소진 후 조기 탈출하여 partial batch index commit이 성공함을 확인.
    - 첫 번째 flush에서 4개 중 1개 항목이 확정되어 pending이 3개로 감소.
    - 두 번째 flush(재시도)에서 이미 확정된 항목은 재업로드되지 않고(PUT 3건만 수행), 모든 pending이 0으로 해소되어 `succeeded` 반환 검증 완료.
- **기존 백업 테스트 스위트**: `apps/cardrag-worker/tests/test_local_serving_and_backup.py`
  - 9/9 전건 통과.

### 3.2 린트 및 타입 체크
- `ruff check`: 통과 (오류 0건).
- `mypy`: 통과 (73 source files, 오류 0건).

---

## 4. 운영 안전성 확인

- 현재 운영 환경에서 `cardrag-backup`은 정지 상태이며, Worker 및 live MCP 서빙에는 영향을 주지 않음.
- 백업 락(`backup.lock`)을 통한 single-writer 상호 배제가 유지됨.
- 신규 Worker/Backup 런타임 이미지 적용 시 안전하게 재개 가능한 상태임.
