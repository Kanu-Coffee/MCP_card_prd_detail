# 019 FIX_02 수정 결과 보고서 (FIX_02_REPORT.md)

- **작업 식별자**: `.handoff/019_selected-issuer-unresolved-history-preservation/`
- **역할**: Executor
- **수정 요구 기준**: `FIX_02.md` (Reviewer: Codex)
- **우선 적용 정책**: `PLAN_AMENDMENT_01.md` (최우선 적용) 및 `PLAN.md` (불변 제약 및 격리 보존)
- **작업 일시**: 2026-10-19
- **결과 요약**: FIX_02의 4대 핵심 요구사항(P1 3건, P2 1건) 코드 구현 및 파이프라인/CLI 경계 수정 완료. 신규 파이프라인 통합 테스트를 포함한 전용 테스트 12건 전원 통과 (`12 passed in 2.30s`), 전체 워커 테스트 1,382건 100% 통과 (`1382 passed in 51.64s`), ruff check/format 125개 파일 검증 통과, `git diff --check` 클린 통과.

---

## 1. FIX_02 검토 의견별 정정 및 조치 상세

### 1.1 [P1] Ledger 검증 실패 삼킴 및 손상된 해시 재생성·통과 제거
- **FIX_02 지적**:
  - `partial_execution.py:759–776`에서 `validate_unresolved_ledger_in_seal` 예외 발생 시 `raw_unresolved`로 되돌리고 새 SHA를 생성해 통과시키던 결함 존재.
  - `pipeline.py`의 사전 검증에서 hash/count 검증 오류 시 warning만 남기고 계속 진행하거나, `current_serving_baseline_source`에서 `except Exception: continue`로 `None`을 반환하여 비정상 baseline이 신규 실행으로 은폐되던 문제.
- **조치 사항**:
  1. **Fail-Fast 사전 무결성 검증 강제**:
     - `pipeline.py` (시작 시점, 라인 3652–3664)에서 `baseline_pre_check`(`_preserved_source` 또는 `current_serving_baseline_source`)의 `source.seal`에 `v5_metrics`가 존재할 경우 즉시 `validate_unresolved_ledger_in_seal()`을 호출합니다.
     - 검증 실패(SHA 불일치, 순서/중복 오류, 카운트 불일치 등) 시 즉시 `PartialExecutionError("issuer_scope_baseline_incompatible", "scope")`를 발생시켜 origin crawl 및 OCR 호출 전에 실행을 중단합니다.
  2. **Baseline 조회 함수 예외 전파 보장**:
     - `issuer_scope.py`의 `current_serving_baseline_source()`에서 `validate_unresolved_ledger_in_seal` 실패 시 예외를 삼키고 `continue`하던 루프를 제거하고, `PartialExecutionError("issuer_scope_baseline_incompatible", "scope")`로 즉시 fail-fast 발생하도록 수정했습니다.
     - `preservation_source()`와 동일하게 손상된 baseline seal은 조용히 무시되지 않고 즉시 거부됩니다.
  3. **재생(replay) 경로의 Fallback 완전 제거**:
     - `partial_execution.py:replay_pdf_source()`에서 `try ... except Exception: unresolved_ledger = raw_unresolved` 블록을 완전히 제거했습니다.
     - 검증 실패 시 `PartialExecutionError("issuer_scope_baseline_incompatible", "export")`를 즉시 발생시키며, 유효하지 않은 입력을 정상 canonical hash로 재포장하는 경로를 차단했습니다.

### 1.2 [P1] OCR 이전 조기 최신본 확정 제거 및 실패 시 격리 보장
- **FIX_02 지적**:
  - 수집(discovery/acquisition) 직후 개정 확장(revision expansion) 단계에서 `plan_unresolved_remediation`과 `apply_unresolved_remediation`을 전면 적용하여, OCR이 아직 실행되지 않았음에도 `updated_to_latest`가 영구 DB에 등록되었음.
  - OCR 실패 또는 collect phase 반환 시에도 잘못된 `updated_to_latest`가 남아 `is_revision_remediated == True`가 되던 결함.
- **조치 사항**:
  1. **개정 확장(수집/다운로드) 단계의 처분 범위 한정**:
     - 개정 확장 시점(`pipeline.py:4425–4455`)에서는 `apply_unresolved_remediation(..., only_dispositions=("past_excluded",))`를 강제했습니다.
     - 이 단계에서는 비선택 카드사의 과거 이력 또는 현재 수집에서 발견되지 않은 레거시 항목(`past_excluded`)만 적용되며, `updated_to_latest` 처분은 DB에 절대 조기 커밋되지 않습니다.
     - 개정 확장 단계의 처분 내역은 `phase="expansion"` 메타데이터와 함께 `runs/<run_id>/remediation-inventory.json`에 감사 아티팩트로 기록됩니다.
  2. **OCR 및 최종 처리 완료 후 2차 재평가 및 확정**:
     - OCR, 구조화, 임베딩이 완료된 후(`pipeline.py:6330–6370`) 실제 유효본인 `processed` 문서, `failed_documents`(OCR 및 다운로드 실패 목록), `unsupported`(DRM/ZIP) 목록을 수집하여 `plan_unresolved_remediation(..., current_processed_products=processed_map, current_failed_products=failed_map, current_unsupported_products=unsupported_map, phase="final")`을 재실행합니다.
     - 실제로 OCR을 성공적으로 마친 상품에 대해서만 `updated_to_latest` 처분을 확정하고 DB에 커밋합니다.
     - OCR 단계에서 실패(`failed_documents`)하거나 DRM/ZIP으로 저하(`unsupported`)된 상품은 `current_error`로 평가되어 DB에 커밋되지 않고, `state.is_revision_remediated(...)`는 `False`로 보존됩니다.
     - 최종 산출된 unresolved ledger와 canonical SHA-256을 `_build_v5_generation()`에 전달하여 최종 게시 seal에 정확히 반영합니다.
  3. **Collect Phase의 안전성**:
     - `ExecutionPlan(phase="collect")`는 OCR을 수행하지 않고 반환하므로, `updated_to_latest`가 절대 등록되지 않으며 `phase="expansion"`의 인벤토리 아티팩트만 남깁니다.

### 1.3 [P1] CLI의 실제 서빙 결합 검증 및 비서빙/조작 seal 원천 차단
- **FIX_02 지적**:
  - CLI `--apply`가 `run.status in ('succeeded', 'no_change')`만 확인하고, 해당 run이 실제 게시되었는지, current serving과 결합되었는지, seal이 조작되지 않았는지 검증하지 않아 미게시/조작 run에 대해 `past_excluded` 처분을 적용할 수 있었음.
- **조치 사항**:
  1. **`require_verified_serving` 정책 도입**:
     - `unresolved_remediation.py`의 `validate_unresolved_ledger_in_seal()`에 `require_verified_serving=True` 검증 모드를 추가했습니다.
     - SQLite `publish` 테이블을 조회하여 해당 `run_id` 및 `generation_id`가 `status == 'ready'`로 등록된 실제 유효 게시물인지 확인합니다.
     - `publish` 테이블에 행이 없거나(미게시), 다른 generation_id이거나, 최신 ready 게시물과 일치하지 않는 경우 `ValueError("seal is not a verified ready publication in the database")`를 발생시킵니다.
  2. **CLI 적용 강제**:
     - `apps/cardrag-worker/src/cardrag_worker/cli.py`의 `remediate-unresolved-history`에서 `--apply` 옵션 활성화 시 `require_verified_serving=True`를 필수 검사하도록 강제했습니다.
     - 미게시 run, 조작된 generation, 서빙과 무관한 seal에 대한 apply 요청은 즉시 exit=1과 함께 에러 JSON을 출력하고 DB를 전혀 변경하지 않습니다.
     - `--dry-run`은 진단 분석용으로 서빙 결합과 무관하게 분석 리포트를 출력하지만, 영구 적용(`--apply`)은 엄격한 서빙 검증을 통과해야만 가능합니다.
  3. **출처 없는 조작 Identity 방어**:
     - `analyze_unresolved_entry()`에서 `pdf_cache_source` DB와 스냅샷 소스 목록 어디에도 존재하지 않는 identity는 `disposition = "current_error"`로 강제 분류하여 조작된 entry가 임의로 제외 등록되는 것을 차단했습니다.

### 1.4 [P2] Process/Reprocess 경로 최종 재평가 및 감사 인벤토리 아티팩트 보완
- **FIX_02 지적**:
  - `replay_pdf_source()`에 최신 OCR/처리 결과로 정비 처분을 재평가·확정하는 과정이 없고 기존 suppression 필터링만 존재했음.
  - 실행 판단 시점, 처분 근거, baseline 정보를 담은 `remediation-inventory.json` 감사 아티팩트가 생성되지 않았음.
  - 단위 helper 호출에만 의존한 테스트 assertion 보완 요구.
- **조치 사항**:
  1. **`replay_pdf_source` 내 정비 재평가 및 반영**:
     - `partial_execution.py:replay_pdf_source()` (라인 764–796)에 사후 재평가 블록을 추가했습니다.
     - 재생 및 OCR/구조화가 완료된 `processed`, `failed`, `unsupported` 문서를 기반으로 `processed_map`, `failed_map`, `unsupported_map`을 구성하고 `plan_unresolved_remediation(..., phase="process")`를 호출합니다.
     - `analyze_unresolved_entry()`에서 `is_product_processed is True`인 경우(문서가 재생/처리되어 본문에 수록됨) `current_discovery` 존재 여부와 무관하게 `updated_to_latest` 처분을 확정하고 DB에 적용합니다.
     - 정비 적용 후 `state.is_revision_remediated(...)`를 재평가하여 최종 unresolved ledger를 필터링하고 canonical SHA-256을 생성합니다.
  2. **인벤토리 감사 아티팩트 (`remediation-inventory.json`) 작성**:
     - `write_remediation_inventory_artifact()` 함수를 구현했습니다 (`schema_version: cardrag.remediation-inventory.v1`).
     - 각 run 디렉터리(`runs/<run_id>/remediation-inventory.json` 및 `runs/<run_id>/reports/remediation-inventory.json`)에 저장됩니다.
     - baseline generation ID, run ID, 현재 run ID, phase (`expansion`, `process`, `final`), 총 identity 수, disposition별 카운트, issuer별 카운트, 사유별 카운트, 개별 항목 상세(source_id, pdf_sha256, reason_code, issuer, product_code, disposition, explanation)를 완전하게 영구 기록합니다.

---

## 2. 검증 결과 및 증거

### 2.1 전용 테스트 스위트 (`apps/cardrag-worker/tests/test_019_unresolved_history.py`)
총 12개 테스트 전체 통과 (`12 passed in 2.30s`):

1. `test_plan_and_apply_unresolved_remediation_all_three_reasons`: 3대 사유별 정상 평가 및 처분 검증.
2. `test_preserve_inputs_with_missing_source_metadata_does_not_fail`: 메타데이터 누락 레거시 이력에 대한 크래시 방어 검증.
3. `test_normal_absence_vs_failure_distinction`: 정상 미존재(`past_excluded`)와 어댑터 수집 실패(`current_error`) 분기 검증.
4. `test_cli_remediate_unresolved_history_dry_run_and_apply`: CLI dry-run(exit=0), 손상 seal 거부(exit=1), apply(exit=0) 검증.
5. `test_selected_run_preserves_serving_and_skips_nonselected_adapters`: 단일 카드사 실행 시 비선택 어댑터 미호출, 서빙 보존, 레거시 격리 검증.
6. `test_disk_validation_active_vs_historical_revision`: CAS 객체 디스크 유실 시 `current_error` 유지 및 재평가 un-suppress 검증.
7. `test_validate_unresolved_ledger_security_and_authenticity`: 해시 불일치, DB 미등록 run, 미지원 reason, 비정렬 entry 거부 검증.
8. `test_pipeline_discovery_and_acquisition_failures`: 수집 실패 및 미획득 시 `current_error` 유지 검증.
9. `test_corrupted_baseline_seal_ledger_fails_fast_in_pipeline` (**신규 파이프라인 테스트**):
   - baseline seal의 ledger SHA를 의도적으로 손상시킨 후 `WorkerPipeline.run()` 실행 시 `PartialExecutionError`로 즉시 fail-fast하며, crawl/OCR가 진행되지 않고 DB가 불변임을 검증.
10. `test_ocr_failure_does_not_persist_updated_to_latest` (**신규 파이프라인 테스트**):
    - 새로운 개정본 수집 후 active OCR 단계에서 `ProviderSystemicError` 발생 시 `OCRSystemicFailureError`로 중단되며, 과거 identity가 `updated_to_latest`로 잘못 등록되지 않고 `is_revision_remediated == False`로 안전하게 보존됨을 검증.
11. `test_collect_then_process_remediation_lifecycle` (**신규 파이프라인 테스트**):
    - `phase="collect"` 실행: 다운로드 완료 후 OCR 생략, `updated_to_latest`가 커밋되지 않음(`is_revision_remediated == False`), `phase="expansion"` 감사 아티팩트 발행 확인.
    - `phase="process"` 실행: replay 및 OCR 처리 완료 후 `updated_to_latest`가 확정(`is_revision_remediated == True`), `phase="process"` 감사 아티팩트 발행 및 처분 항목 확인.
12. `test_cli_rejects_unpublished_and_fabricated_seals` (**신규 CLI 테스트**):
    - `publish` 테이블에 없는 미게시 run 또는 조작된 generation seal을 `--apply`로 실행 시 exit=1로 즉시 거부되고, 에러 JSON 반환 및 DB 변형 0건 검증.

### 2.2 전체 회귀 검증 및 코드 품질
- **전체 워커 테스트 스위트 (`apps/cardrag-worker/tests/`)**:
  ```text
  1382 passed, 10 warnings in 51.64s
  ```
  기존 모든 테스트 및 회귀 테스트 100% 통과 (0 regression).
- **Linter & Code Formatter**:
  ```text
  uv run --no-sync ruff check apps/cardrag-worker/
  All checks passed!

  uv run --no-sync ruff format --check apps/cardrag-worker/
  125 files already formatted
  ```
- **Git Diff 무결성**:
  ```text
  git diff --check
  (클린 통과, 공백 및 충돌 없음)
  ```

---

## 3. 운영 제약 및 다음 단계 준수

1. **불변 제약 준수**:
   - 운영 서버 배포 없음.
   - 실제 카드사 origin 실시간 호출 없음.
   - 시스템 타이머 활성화 없음.
   - CAS 디스크 객체 무결성, WebDAV serving baseline 바인딩, 비선택 카드사 완전 격리 원칙 유지.
2. **운영 인계 경로**:
   - 사용자 수동 우리카드 단독 검증 → 나머지 7사 순차 검증 → 백업 및 degraded 상태 확인 → 타이머 재활성화 순으로 기존 계획을 그대로 따릅니다.
