# 019 FIX_01 수정 결과 보고서 (FIX_01_REPORT.md)

- **작업 식별자**: `.handoff/019_selected-issuer-unresolved-history-preservation/`
- **역할**: Executor
- **수정 요구 기준**: `FIX_01.md` (Reviewer: Codex)
- **우선 적용 정책**: `PLAN_AMENDMENT_01.md` (최우선 적용) 및 `PLAN.md` (불변 제약 및 격리 보존)
- **작업 일시**: 2026-10-19
- **결과 요약**: FIX_01 요구사항 100% 반영 완료. 전용 단위/통합 테스트 8건 통과, 전체 워커 테스트 1,378건 100% 통과, 린트/포맷 125개 파일 검증 완료.

---

## 1. FIX_01 수정 요구 항목별 조치 상세

### 1.1 [P1] 현재 수집 결과 없이 영구 제외 확정 및 오류 미해제 해결
- **문제점**: 사전 검사 단계(discovery 이전)에서 `plan_unresolved_remediation`과 apply가 조기 실행되어, 최신 유효본 확인 없이 `updated_to_latest`가 기록되고 이후 동일 identity가 실패하더라도 기존 suppression이 해제되지 않았음.
- **조치 사항**:
  1. **사전 검사와 최종 정비 확정 분리**:
     - `preserve_inputs` 단계에서는 비선택 카드사의 과거 레거시 ledger로 인한 `issuer_scope_revision_source_missing` 크래시만 방어하고 조기 정비 확정은 수행하지 않습니다.
     - 최종 정비 계획 및 반영 시점을 수집/다운로드/OCR 완료 후인 `pdf_revision_expansion` 직전(line 4405)으로 이전했습니다.
     - 파이프라인의 실제 런타임 결과인 `successful_issuers`, `current_discovery`(스냅샷 기반 수집 목록), `current_acquired_products`(다운로드 완료 문서), `failed_map`(`_IssuerSkippedDownload` 및 실패 목록), `pdf_cache`(디스크 검증기)를 주입하여 최종 처분을 확정합니다.
  2. **오류 은폐 방지 및 재평가 un-suppress 보장**:
     - `state.py`의 `is_revision_remediated()`가 단순 행 존재가 아닌 `disposition` 값을 검사하도록 수정했습니다.
     - `disposition == 'current_error'`인 경우 `is_revision_remediated()`는 항상 `False`를 반환합니다.
     - 이전에 `past_excluded`로 기록된 identity라도 현재 실행에서 `current_error`로 재평가되면 `pdf_cache_remediated_revision` 테이블의 `disposition`이 `'current_error'`로 갱신되어 즉시 un-suppress됩니다.
  3. **비선택 카드사 과거 ledger 제외 의미 명시**:
     - 비선택 카드사의 과거 이력 제외는 이번 스코프 실행 중 활성 게시 및 개정 확장 대상에서 제외하는 것일 뿐, 해당 카드사의 실제 상품 소멸이나 정상본 갱신의 증거가 아님을 인벤토리 explanation에 명시했습니다.

### 1.2 [P1] PDF 객체 존재 판정의 디스크 검증 부재 해결
- **문제점**: `pdf_cache_object` 테이블의 SQLite 행 존재만 확인하여, 실제 디스크 파일이 유실/손상되었음에도 `past_excluded`로 오분류되어 오류가 정비 완료로 처리되는 결함이 존재했음.
- **조치 사항**:
  1. `verify_pdf_cache_object_disk()` 함수를 구현했습니다.
     - `PDFCache.object_path(pdf_sha256)` 또는 CAS `objects_root` 디렉터리 경로를 확인합니다.
     - 파일 존재 여부, 심볼릭 링크 방지 검사, `_validate_regular_pdf()`를 통한 SHA-256 바이트 해시 및 PDF 규격 검증을 강제합니다.
  2. `is_current_in_cache`인 활성 개정본의 디스크 객체가 유실 또는 손상된 경우:
     - SQLite에 객체 행이 있더라도 `pdf_cache_object_disk_valid`가 `False`가 되며, 처분은 무조건 `current_error`로 분류됩니다 (`explanation`: "Active current cache revision object is missing or invalid on disk; error preserved").
     - `is_revision_remediated()`는 `False`를 반환하여 개정 확장 시 오류가 정상 보존됩니다.

### 1.3 [P1] 검증되지 않은 ledger 및 실제 serving 미결합 seal 영구 suppression 방지
- **문제점**: CLI나 자동 경로에서 서빙 결합 여부나 ledger의 schema, count, canonical 정렬, hash 검증 없이 seal을 읽고 apply할 수 있어 잘못된 입력이 영구 제외로 등록될 수 있었음.
- **조치 사항**:
  1. `validate_unresolved_ledger_in_seal()` 함수를 신설하여 다층 무결성 검증을 구현했습니다:
     - `generation_id` 및 `run_id` 유효성 검사.
     - `require_verified_run=True` 시 SQLite `run` 테이블에서 해당 `run_id`의 상태가 정상 완료(`succeeded` 또는 `no_change`)인지 검증 (미완료/실패/존재하지 않는 run 차단).
     - 각 entry의 schema(`source_id`, `pdf_sha256`, `reason_codes`), 허용 사유 3종(`_UNRESOLVED_REASONS`) 적합성 검사.
     - `canonical_unresolved_revision_ledger_v5`와의 일치 검사를 통한 정렬 및 중복 검증.
     - `historical_revision_unresolved_count`와 실제 ledger 길이 일치 검증.
     - `unresolved_revision_ledger_sha256_v5`를 통한 SHA-256 해시 일치 검증.
  2. CLI `remediate-unresolved-history`:
     - `--dry-run` 및 `--apply` 명시적 옵션을 Typer에 등록하여 `--dry-run` 호출 시 exit=0으로 정상 동작하도록 수정했습니다 (기본값 dry-run).
     - `--seal-path`에 손상된 해시, 중복 entry, 미검증 run을 가진 seal이 입력되면 즉시 에러(`status: failed`, exit=1)를 출력하고 DB를 전혀 변경하지 않습니다.
  3. `partial_execution.py`:
     - 재생(replay) 경로에서도 `validate_unresolved_ledger_in_seal`을 거쳐 검증된 entry만 필터링하고 새 canonical sha를 계산하도록 수정했습니다.

### 1.4 [P2] 최초 전체 배치 및 collect/process 경로의 자동 정비 일관성 확보
- **문제점**: `_preserved_source is not None` 블록 내부에만 자동 정비가 위치하여, 비선택 카드사가 없는 전체 배치(8사)나 collect/process, 정비 테이블이 비어 있는 최초 실행에서 정비가 누락되었음.
- **조치 사항**:
  1. `issuer_scope.py`에 `current_serving_baseline_source(pipeline)`를 분리 구현했습니다.
     - 비선택 카드사 존재 여부와 무관하게 WebDAV의 `validated_current_generation()`과 일치하는 서빙 baseline `ReuseSource`를 암호학적으로 검증하여 반환합니다.
     - `preservation_source(pipeline)`는 비선택 카드사가 있을 때만 입력 보존용으로 사용되고, 서빙 baseline 탐색은 독립적으로 동작합니다.
  2. `pipeline.py`:
     - 전체 배치(8사 선택), 단일 카드사 선택 배치, collect phase, process phase 모두에서 revision expansion 직전에 `current_serving_baseline_source`를 통해 서빙 baseline seal을 찾아 일관되게 정비를 평가하고 반영합니다.

---

## 2. 검증 결과 및 증거

### 2.1 신규 전용 검증 테스트 (`apps/cardrag-worker/tests/test_019_unresolved_history.py`)
총 8개 테스트 전체 통과 (`8 passed in 1.79s`):

1. `test_plan_and_apply_unresolved_remediation_all_three_reasons`:
   - 3대 사유별 정상 평가 및 처분, `current_error` 항목이 `is_revision_remediated() == False`임을 확인.
2. `test_preserve_inputs_with_missing_source_metadata_does_not_fail`:
   - 호스트 장애 원인이었던 메타데이터 누락 레거시 이력에 대해 크래시 없는 안전한 보존 확인.
3. `test_normal_absence_vs_failure_distinction`:
   - 정상 미존재(`past_excluded`)와 어댑터 수집 실패(`current_error`)의 상태 분기 검증.
4. `test_cli_remediate_unresolved_history_dry_run_and_apply`:
   - CLI 기본 실행(dry-run, exit=0), `--dry-run` 명시 실행(exit=0), 손상된 seal 입력 시 거부(exit=1, DB 변경 없음), `--apply` 실행(exit=0, DB 반영) 검증.
5. `test_selected_run_preserves_serving_and_skips_nonselected_adapters`:
   - 단일 카드사 실행 시 비선택 카드사 어댑터 미호출, 서빙 문서 유지, 비선택 레거시 오류 안전 배제 검증.
6. `test_disk_validation_active_vs_historical_revision` (신규):
   - **활성 개정본의 파일 유실**: `pdf_cache_object` DB 행이 있어도 디스크 파일이 없으면 `current_error`로 분류되고 apply 후 `is_revision_remediated == False` 유지.
   - **과거 개정본의 파일 유실**: `past_excluded`로 분류되고 apply 후 `is_revision_remediated == True`.
   - **재평가 un-suppression**: 과거에 `past_excluded`로 등록된 항목이라도 현재 파일 유실로 `current_error`로 재평가되면 즉시 un-suppress(`is_revision_remediated == False`)됨을 검증.
7. `test_validate_unresolved_ledger_security_and_authenticity` (신규):
   - 정상 seal 통과 검증.
   - SHA-256 불일치, SQLite 미등록/실패 run, 미지원 reason code, 중복/비정렬 entry 각각에 대해 `ValueError` 발생 및 DB 불변 검증.
8. `test_pipeline_discovery_and_acquisition_failures` (신규):
   - 선택 카드사 수집 실패(미완료) 시 `current_error` 분류 및 오류 보존 검증.

### 2.2 회귀 검증 스위트
- **관련 핵심 테스트 (`test_017_scope.py`, `test_pipeline_v5.py`, `test_revision_history_v5.py`)**:
  ```text
  75 passed, 10 warnings in 10.20s
  ```
- **전체 워커 테스트 스위트 (`apps/cardrag-worker/tests/`)**:
  ```text
  1378 passed, 10 warnings in 49.64s
  ```
- **코드 스타일 및 포맷 (`ruff check` & `ruff format`)**:
  ```text
  All checks passed!
  125 files already formatted
  ```

---

## 3. REPORT 한계 정정 및 실제 운영 검증 경계 (Clarifications)

1. **테스트 fixture와 실제 운영 데이터의 구분**:
   - 기존 `REPORT.md`에서 "5,536개 서빙 문서 보존"으로 표현된 항목은 단위 테스트 환경에서 2개 가상 카드사 fixture를 통해 "비선택 카드사 어댑터 호출 0건 및 매니페스트 서빙 보존 계약"을 검증한 것입니다.
   - 실제 운영 8개사 5,536개 라이브 문서 세트의 보존 여부는 호스트 운영 환경 배포 후 실제 WebDAV/SQLite 상태 검증을 통해 최종 확인되어야 합니다.
2. **22건 베이스라인 인벤토리 fixture의 한계**:
   - 단위 테스트는 운영 상품/source ID를 하드코딩하지 않고, 실제 baseline 22건에서 도출된 3대 사유(`document_identity_collision`, `source_metadata_unresolved`, `pdf_cache_object_unavailable`)를 격리된 환경에서 완벽히 재현하여 검증했습니다.
3. **운영 미검증 항목 및 운영자 인계 절차**:
   - Reviewer의 재검토 및 승인 전에는 실제 호스트 배포, 카드사 origin 호출, 타이머 활성화를 진행하지 않습니다.
   - 운영 적용 순서:
     1. 워커 컨테이너 이미지 빌드 및 배포
     2. `cardrag-worker remediate-unresolved-history --dry-run`으로 대상 22건 진단 확인
     3. 호스트에서 `--issuer woori` 단일 수동 배치 실행하여 정상 완료 및 서빙 무결성 확인
     4. 나머지 7개 카드사(`hana`, `hyundai`, `bc`, `kb`, `lotte`, `samsung`, `shinhan`) 순차 실행
     5. WebDAV 백업 및 degraded 상태 확인 후 자동 타이머 복원
