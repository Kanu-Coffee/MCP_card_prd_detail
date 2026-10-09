# 013 FIX_07_REPORT — FIX_05/06 인수 보완 및 최종 검증

작성일: 2026-10-09
작성자: Executor (Antigravity)
관련 결함 보고: `.handoff/013_local-serving-incremental-webdav-backup/FIX_07.md`
대상 브랜치: `codex/013-local-serving-incremental-webdav-backup`
기준 커밋: `6004526`

---

## 1. 개요 및 판정 요약

Reviewer의 FIX_07 인수 보완 요구사항에 따라 다음 5가지 핵심 항목을 충실히 구현하고 오프라인 전수 검증을 완료하였다:

1. **기존 캐시 variant 및 provenance 보존**:
   - `_resolve_serialized`에서 `prior_local_native`가 전달된 경우 generic `_content_store.lookup()`을 건너뛰고 확정된 prior 후보를 사용하도록 정리하여 원래의 `provider`, `model`, `variant_id`(`c*64`)가 온전히 보존됨.
   - `_lookup_local`에서 `created_at` 타임스탬프를 `publish.json`의 `st_mtime` 기반으로 결정론적(deterministic) 처리하여 동일 run/동일 입력 반복 조회 시 variant ID가 영구 불변하도록 수정.
   - `publish.json`의 `generation_id`와 문서 메타데이터의 `ocr_variant_id`를 보존.
2. **엄격한 Cache Epoch 경계 및 입력 일치**:
   - `_lookup_local` 및 `_lookup_prior_local_sealed_ocr`에서 `pdf_matches`는 필수이며, `ocr_cache_kind == 'content'`인 경우 `ocr_reuse_key == key`가 반드시 일치해야만 hit하도록 수정.
   - non-content(native/adopted)의 content 캐시 폴백은 `cache_epoch == 0` 경계에서만 허용하여, epoch 1 요청 시 epoch 0 자료가 완전히 miss되도록 보장 (`epoch1_hit_of_epoch0 == False`).
3. **백업 실제 전송량 표시 및 예산 타임아웃 정비**:
   - `backup.py`에서 OCR/PDF 본체 전송(`body_requests`)과 control JSON(`control_requests`)을 명확히 분리하여 카운트.
   - transport(WebDAVClient)가 CAS verify-existing으로 PUT을 생략하는 특성을 반영하여, 실제 전송량이 미측정인 경우 `actual_uploaded_count/bytes`를 `None`(미측정)으로 반환하고 `processed_count/bytes`, `receipts_reused_count/bytes`로 처리 상태를 정확히 표현.
   - 단일 아이템 타임아웃을 `min(30.0, max(0.01, remaining_data_time))`로 조정하여 잔여 데이터 예산을 초과하지 않도록 정리.
   - 인덱스 커밋 예산 `rem_time = max(10.0, timeout_seconds - elapsed)`로 원자적 인덱스 반영을 안전하게 보장하되 무제한 연장 방지.
4. **실패 테스트 정정 및 과거 deselected 사유 명시**:
   - `test_backup_budget_reserve_commits_partial_batch_and_retry_zero_requests`: `body_puts`와 `control_puts`를 구분하여 본체 업로드 회수(`4 - flushed_first_round`)를 정확히 assert.
   - 신규 테스트 `test_backup_index_commit_failure_preserves_pending_and_retry_zero_body_transfers` 추가: 인덱스 커밋 실패 시 pending 유지 확인 및 다음 재시도에서 verified receipt를 통해 본체 GET/PUT 0건으로 성공함을 검증.
   - 과거 FIX_06에서 1개 제외되었던 테스트(`test_independent_two_process_lock_barrier`)의 사유 및 현황 명시: 독립 2개 프로세스의 디렉터리 동시 생성 경합 격리를 위한 것이었으며, 현재는 **0 deselected** 상태로 전량 정상 통과.
5. **실 볼륨 328건 대상 Read-only Preflight 검증 및 증적 보존**:
   - Docker 볼륨 `cardrag-worker-state`의 `025ce35739944d8facfdda4c99961f0c` 대상 100% 읽기 전용 검증 스크립트 작성 (`preflight_volume_328.py`).
   - 5건의 native 문서 provenance(opencode, qwen3.8-flash, exact variant IDs) 보존 확인.
   - 323건의 content 문서: `RejectingProvider` 주입 상태에서 **323 / 323건 (100%) provider 호출 0건으로 해결**, epoch 1 miss 100%, 동일 입력 반복 조회 안정성 100% 확인.
   - 검증 결과 JSON 영구 보존 (`evidence/reviewer-fix07-volume-preflight.json`).

---

## 2. 세부 변경 사항

### 2.1 `apps/cardrag-worker/src/cardrag_worker/content_cache.py`
- `_lookup_local`:
  - `cache_epoch: int` 매개변수 수신.
  - 엄격한 PDF 일치(`pdf_matches: sha256, size_bytes, page_count`)를 기본 통과 조건으로 강제.
  - `ocr_cache_kind == 'content'`인 경우 `doc.get("ocr_reuse_key") == key`를 요구하여 epoch 불일치 자료를 철저히 miss 처리.
  - non-content(native/adopted) 자료는 `cache_epoch == 0`일 때만 폴백 허용.
  - `created_at` 결정론적 fallback: `publish_path.stat().st_mtime` 기반으로 고정하여 동일 run 내 반복 조회 시 variant ID가 변하지 않도록 수정.
  - `native-manifest.json`이 존재할 경우 `NativeOCRContract`의 provenance를 로드하고, 문서의 `ocr_variant_id`가 64자리 hex일 경우 신규 생성 대신 기존 variant ID를 온전히 보존.

### 2.2 `apps/cardrag-worker/src/cardrag_worker/ocr.py`
- `_resolve_serialized`:
  - `prior_local_native`가 이미 파이프라인에서 검증/전달된 경우, `_content_store.lookup()`을 건너뛰도록 가드(`if self._content_store is not None and prior_local_native is None and reprocess_request_id is None:`).
  - WebDAV 원격 캐시(`_lookup_cache`)가 원격 충돌을 감지하고 `"refusing remote OCR cache variant outside the retained generation seal"` 경고를 정상 방출한 뒤 `prior_local_native` 해결로 진입할 수 있도록 순서 정렬.
- `_lookup_prior_local_sealed_ocr`:
  - `cache_kind == "content"`인 경우 `prior.reuse_key == current_content_key`를 검증하여 캐시 epoch 경계 보장.
  - `prior.provider` 및 `prior.model`이 None이고 디스크에 `native-manifest.json`이 있는 경우 이를 읽어 원본 provenance 복원.
  - `variant_id`를 64자리 hex 검증 후 보존(`prior.variant_id if re.fullmatch(...) else prior.ocr_sha256`).

### 2.3 `apps/cardrag-worker/src/cardrag_worker/backup.py`
- `flush`:
  - `body_requests`와 `control_requests` 분리 집계.
  - 단일 아이템 타임아웃을 `min(30.0, max(0.01, remaining_data_time))`로 조정하여 잔여 예산 초과 방지.
  - 인덱스 커밋 타임아웃을 `max(10.0, timeout_seconds - elapsed)`로 부여하여 원자적 인덱스 갱신 안정성 확보.
  - 결과 딕셔너리에 `body_requests`, `control_requests`, `receipts_reused_count/bytes` 반환, `actual_uploaded_*`는 클라이언트 측정값이 없을 시 `None`으로 반환하여 GET만 수행한 자료가 업로드로 오인되지 않도록 수정.

### 2.4 `apps/cardrag-worker/tests/test_fix_05_06_local_content_and_backup.py`
- `test_sealed_prior_content_without_native_manifest_resolves_provider_zero`:
  - `variant_id`를 64자리 hex(`'c' * 64`)로 설정하여 Pydantic 모델 검증 준수.
- `test_backup_budget_reserve_commits_partial_batch_and_retry_zero_requests`:
  - `SlowMockWebDAV`에서 `body_puts`와 `control_puts`를 구분 카운트.
  - 재시도 라운드에서 본체 업로드 회수가 잔여 pending 개수(`4 - flushed_first_round`)와 정확히 일치함을 검증.
- `test_backup_index_commit_failure_preserves_pending_and_retry_zero_body_transfers`:
  - 신규 작성: 인덱스 커밋 실패 시 `backup_pending`이 보존되고, 2차 플러시에서 verified receipt를 재사용하여 본체 PUT/GET 0건으로 플러시 성공함을 완벽 검증.

---

## 3. 검증 결과

### 3.1 Reviewer 재현 스크립트 실행
```sh
uv run --no-sync --all-packages python .handoff/013_local-serving-incremental-webdav-backup/evidence/reviewer-fix06-repro.py
```
**결과:**
```json
{
  "provider_calls": 0,
  "cache_hit": true,
  "expected_variant": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "returned_variant": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "expected_provider": "local-paddleocr",
  "returned_provider": "local-paddleocr",
  "expected_model": "PaddleOCR-VL-1.6",
  "returned_model": "PaddleOCR-VL-1.6",
  "same_input_variant_stable": true,
  "epoch1_hit_of_epoch0": false,
  "epoch1_returned_key": null,
  "epoch1_expected_key": "9195f9a06744ac63326491fac4f4b9069cc4c8862333f2cc20c098085eeba3b8"
}
```
- Variant 일치 (`c*64`), Provider 일치 (`local-paddleocr`), Model 일치 (`PaddleOCR-VL-1.6`).
- 반복 조회 시 variant 불변 (`same_input_variant_stable: true`).
- Epoch 1 조회 시 Epoch 0 자료 miss (`epoch1_hit_of_epoch0: false`).
- Provider 호출 0건.

### 3.2 실 운영 볼륨 Read-only Preflight 검증
실제 도커 볼륨 `cardrag-worker-state`의 `025ce35739944d8facfdda4c99961f0c` generation을 마운트하여 실행 (`.handoff/013_local-serving-incremental-webdav-backup/evidence/preflight_volume_328.py`):
```json
{
  "status": "success",
  "volume_run_id": "025ce35739944d8facfdda4c99961f0c",
  "total_documents": 5519,
  "native_documents_count": 5,
  "native_documents_provenance": [
    {
      "document_id": "doc_0e4b7f9e9f9f5d26269563bd9ed9846445946be7c8d2fac928004c71f111900e",
      "provider": "opencode",
      "model": "alibaba-token-plan/qwen3.8-flash",
      "variant_id": "2f788ab85e8bef149a3d2f87f4660c34fef3526f395efada9007f800cfb228e7",
      "reuse_key": "7b23441dbce06c0b80e97a7d2bdb25029ec0c4c44bd709e10c665f34a3dd090f"
    },
    ...
  ],
  "content_tested_count": 323,
  "zero_provider_calls_count": 323,
  "zero_provider_calls_rate": 1.0,
  "provider_calls_total": 0,
  "epoch1_miss_count": 323,
  "epoch1_miss_rate": 1.0,
  "variant_stable_count": 323,
  "variant_stable_rate": 1.0
}
```
- **기지 5 native 문서**: 원래 `opencode` / `alibaba-token-plan/qwen3.8-flash` provenance 및 variant ID 100% 보존.
- **323 candidate content 문서**: `RejectingProvider` 주입 상태에서 323건 전수 provider 호출 0건으로 로컬 해결 (`cache_reused == True`).
- **Epoch 1 경계**: 323건 전수 miss 확인.
- **반복 조회 안정성**: 323건 전수 100% 동일 variant ID 유지.
- 검증 결과 파일: `.handoff/013_local-serving-incremental-webdav-backup/evidence/reviewer-fix07-volume-preflight.json`.

### 3.3 단위 및 통합 테스트 스위트
- **Reviewer 지정 타깃 테스트**:
  ```sh
  uv run --no-sync --all-packages pytest apps/cardrag-worker/tests/test_fix_05_06_local_content_and_backup.py apps/cardrag-worker/tests/test_content_cache.py apps/cardrag-worker/tests/test_ocr_prefetch.py apps/cardrag-worker/tests/test_pipeline_v5.py apps/cardrag-worker/tests/test_local_serving_and_backup.py -q
  # 52 passed, 1 warning in 3.18s (신규 테스트 1건 추가 포함 전량 통과)
  ```
- **전체 통합 테스트 스위트 (Worker & MCP)**:
  ```sh
  uv run --no-sync --all-packages pytest apps/cardrag-worker/tests apps/cardrag-mcp/tests -q
  # 2145 passed, 10 warnings in 62.78s (0 deselected, 0 failed)
  ```
  > **Deselected 사유 명시**: 과거 FIX_06에서 1개 deselected되었던 테스트는 `test_independent_two_process_lock_barrier` (`test_lock_concurrency_multiprocess.py`)였다. 이는 `multiprocessing.Process` 기반의 독립 2개 프로세스가 빈 임시 디렉터리에서 동시 기동할 때 디렉터리 생성 I/O 경합을 격리하기 위해 분리 실행했던 것이며, 이번 FIX_07 스위트에서는 **제외(-k) 없이 2,145건 전량 통과**하였다.
- **정적 분석**:
  - `uv run --no-sync --all-packages ruff check apps/ packages/`: `All checks passed!`
  - `uv run --no-sync --all-packages mypy packages/cardrag-core/src apps/cardrag-worker/src apps/cardrag-mcp/src`: `Success: no issues found in 108 source files`

---

## 4. 운영 반영 안전성 및 결론

1. **라이브 MCP 무중단 유지**:
   - `/var/lib/cardrag-serving`의 03시 정상 서빙 볼륨은 격리 상태를 유지하고 있으며 일체 변경되지 않았다.
2. **무과금(Zero Provider Call) 보장**:
   - 328건(5 native + 323 content) 실물 볼륨 자료가 로컬 모드에서 100% 재사용되므로 향후 배치 기동 시 유료 API 호출이 발생하지 않는다.
3. **인수 준비 완료**:
   - FIX_07의 모든 요구사항(provenance 보존, epoch 경계, 백업 메트릭/예산, 실패 테스트 정정, 증적 저장)이 완전하게 충족되었다.
