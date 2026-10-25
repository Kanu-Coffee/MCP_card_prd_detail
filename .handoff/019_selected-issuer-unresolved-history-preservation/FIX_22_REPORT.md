# 019 FIX_22 이행 보고서 — 롯데 같은 run resume의 OCR identity 보존 및 진단

**작성일시**: 2026-10-21  
**역할**: Executor (Antigravity)  
**참조**: `FIX_22.md`, `LIVE_REVIEW_08.md`, `OPERATIONS_REPORT_07.md`, `PLAN.md`

---

## 1. 개요 및 요약

Reviewer(Codex)가 `LIVE_REVIEW_08.md`를 바탕으로 발행한 `FIX_22.md`의 3가지 핵심 결함을 모두 수정하고 전체 회귀 검증을 완료하였습니다.

1. **[P1] content variant ID와 원본 metadata의 정합성 (`content_cache.py`, `ocr.py`)**:
   - `ContentOCRVariantStore._lookup_local`에서 후보 run의 `publish.json`에 기록된 `ocr_variant_id`를 임의로 추출하여 재구성된 metadata(새로운 provenance/created_at)에 강제 부착하던 문제를 수정하였습니다.
   - 후보 문서 디렉터리(`runs/<cand_run>/documents/<cand_doc>/ocr/content-manifest.json`)에 엄격한 canonical byte 대조를 통과하는 정규 원본 manifest가 존재하면 이를 온전히 복원하여 재사용합니다.
   - 원본 full manifest가 없거나 대조에 실패한 경우, `ContentOCRArtifactManifest.create(...)`를 통해 일치하는 imported variant를 합법적으로 유도/생성하여 Core Pydantic validator(`content OCR variant ID does not match manifest fields`) 위반을 원천 차단하였습니다.
   - 또한 `ocr.py`에서 content OCR 생성, 검증 및 lookup 성공 시 `content-manifest.json`을 해당 run 디렉터리에 항상 기록하도록 보장하였습니다.

2. **[P2] 동일 run resume 시 정상 own OCR의 보존 (`pipeline.py`)**:
   - `_retained_identity_conflict`에서 `own.is_file()`이 존재한다는 이유만으로 무조건 충돌(`True`)로 판정하여 완료 문서 330건을 `prior_local_native_sources`에서 탈락시키던 결함을 수정하였습니다.
   - `own.is_file()` 파일의 SHA-256 바이트가 `partner_seal_sha`와 일치하면 충돌이 아닌 정상 보존/재사용(`False`)으로 처리하여, 같은 run resume 시 이전 완주 완료 문서 330건이 온전히 유지되고 provider 재호출 0건으로 미완료 문서에 즉시 진입하도록 보장하였습니다.
   - 실제 바이트가 불일치하거나 symlink/비정상 파일인 경우는 엄격히 충돌로 판정합니다.

3. **[P2] succeeded-stage OCR 예외 및 DB 기록 실패의 안전한 진단 (`pipeline.py`, `cli.py`)**:
   - `process_document`에서 완료 stage 재검증 예외 또는 비격리 실패가 발생했을 때, generic `OCRFailureBookkeepingError("OCR failure isolation bookkeeping failed")`로 뭉개지던 결함을 수정하였습니다.
   - stage가 이미 `succeeded` 상태인 경우, 또는 격리 불가능한 예외 발생 시 `WorkerUnexpectedFailureRecord`에 `stage="ocr"`, `document_id`, 허용된 reason code(`stage_revalidation_failed`, `cache_validation_error`, `ocr_validation_error`, `stage_bookkeeping_failed`) 및 category를 정확히 기록하고 `runs/<run_id>/reports/worker-failure.json`을 출력한 후 `WorkerUnexpectedFailureError`를 발생시키도록 정류하였습니다.
   - `cli.py`의 `_echo_worker_unexpected_failure`에서도 `stage`, `document_id`, `failure_reason_code`를 구조화하여 JSON 출력합니다.
   - provider stderr, 원시 토큰/자격증명, Core Pydantic의 전체 input_value는 보고서에 노출되지 않도록 기존 secret-safe 경계를 철저히 준수하였습니다.

---

## 2. 변경 파일 및 상세 내역

| 파일 경로 | 변경 내용 요약 |
|---|---|
| `apps/cardrag-worker/src/cardrag_worker/content_cache.py` | `_lookup_local`: `content-manifest.json` canonical bytes/fields 엄격 검증 후 원본 manifest 복원; 부재 시 `ContentOCRArtifactManifest.create`로 합법적 imported variant 유도 |
| `apps/cardrag-worker/src/cardrag_worker/ocr.py` | OCR 발행, lookup hit, verify 완료 시점마다 run의 document 디렉터리에 `content-manifest.json`을 원자적/내구성 있게 영구 기록 |
| `apps/cardrag-worker/src/cardrag_worker/pipeline.py` | 1) `_retained_identity_conflict`: own 파일 바이트가 partner seal SHA와 일치하면 reuse 허용 (`return False`)<br>2) `WorkerUnexpectedFailureRecord`: `document_id`, `stage`, 신규 reason code 필드 추가<br>3) `process_document`: succeeded stage 재검증 실패 및 DB 기록 실패를 분기하여 `worker-failure.json`에 구조화된 진단 출력 후 `WorkerUnexpectedFailureError` 발생 |
| `apps/cardrag-worker/src/cardrag_worker/cli.py` | `_echo_worker_unexpected_failure`에 `document_id`, `stage`, `failure_reason_code` 반영 |
| `apps/cardrag-worker/tests/test_fix_22_ocr_identity_resume.py` | FIX_22 전용 단위/통합 테스트 5종 추가 (content variant store 정합성, succeeded stage revalidation 진단, DB stage 기록 실패 진단, reason code allowlist, retained identity conflict 바이트 일치성) |

---

## 3. 최소 재현 수정 전/후 검증

### 3.1 Reviewer 재현 스크립트 (`/tmp/repro-lotte-content-resume.py`)
- **수정 전**: Core validator `ValidationError: content OCR variant ID does not match manifest fields` 발생.
- **수정 후**: 성공 (exit code 0, 예외 없이 정상 imported/restored variant 반환).

### 3.2 단위/기능 테스트
- `apps/cardrag-worker/tests/test_fix_22_ocr_identity_resume.py`:
  - `test_content_variant_store_lookup_local_with_and_without_original_manifest`: PASSED
  - `test_succeeded_stage_revalidation_failure_diagnostics`: PASSED
  - `test_stage_database_recording_failure_diagnostics`: PASSED
  - `test_worker_unexpected_failure_record_allows_structured_revalidation_codes`: PASSED
  - `test_retained_identity_conflict_behavior`: PASSED
- `apps/cardrag-worker/tests/test_pipeline.py`: 133 passed.
- 관련 OCR 및 백업 회귀 (276 passed):
  - `test_fix_22_ocr_identity_resume.py`
  - `test_content_cache.py`
  - `test_ocr.py`
  - `test_ocr_recovery.py`
  - `test_pipeline.py`
- 백업 및 미해결 보존 테스트 (227 passed):
  - `test_fix_05_06_local_content_and_backup.py`
  - `test_fix_06_scoped_backup.py`
  - `test_fix_14_bc_samsung_backup.py`
  - `test_fix_16_snapshot_lock.py`
  - `test_fix_17_capacity_readiness.py`
  - `test_local_serving_and_backup.py`
  - `test_017_scope.py`
  - `test_019_unresolved_history.py`

### 3.3 Lint, Format, Git Diff Check
- `ruff check apps/cardrag-worker/ packages/cardrag-core/`: All checks passed!
- `ruff format --check apps/cardrag-worker/ packages/cardrag-core/`: 157 files already formatted.
- `git diff --check`: 0 issues.

### 3.4 전체 테스트 스위트 (100% Pass)
- `pytest packages/cardrag-core/tests apps/cardrag-worker/tests -q`: **1,796 passed**, 10 warnings in 57.28s.

---

## 4. 운영 인계 및 현재 상태

1. **라이브 운영 상태 보존**:
   - 구현 과정에서 운영 SQLite DB, 원격 WebDAV, seal, cache 파일 일체를 조작하거나 삭제/재배치하지 않았습니다.
   - 단일 백업 유지 정책에 따라 `/state/deployment-backups/019-fix16-4108e3dac5f5` (6.8GB DB)가 정상 보존 중이며, 가용 디스크 용량은 ~85 GiB (78% 사용)로 안전 상태를 유지하고 있습니다.
   - 롯데 중단 run `49845932e05849d3a0110a2bc82d6c1c`의 완료 OCR 330/707 및 chunk checkpoint 97건이 온전히 유지되고 있습니다.

2. **다음 단계 안내 (Reviewer 승인 후)**:
   - 본 수정사항이 검토를 통과하면, 운영 승인 절차에 따라 Worker 컨테이너 이미지/코드를 갱신하고 동일한 run `49845932e05849d3a0110a2bc82d6c1c`을 resume 실행합니다.
   - 완료된 330건의 바이트 일치성 통과로 provider 호출 0건 재사용이 확인되고, 미완료 377건에 도달하여 완주 여부를 점검합니다.
