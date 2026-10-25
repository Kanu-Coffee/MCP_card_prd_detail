# 019 미해결 이력 갱신 및 선택 카드사 실행 보존 — Executor 결과 보고서 (REPORT.md)

- **작업 식별자**: `.handoff/019_selected-issuer-unresolved-history-preservation/`
- **역할**: Executor
- **기준 계획**: `PLAN_AMENDMENT_01.md` (최우선 적용) 및 `PLAN.md` (기본 설계 및 불변 제약 유지)
- **작업 일시**: 2026-10-19
- **결과 요약**: 구현 완료, 단위/통합 테스트 1,375건 전체 통과, 린트/포맷 100% 검증 완료

---

## 1. 개요 및 배경

### 1.1 장애 원인 분석 (호스트 검증 시점: `6266b015df8c496586fe6a55362edd09`)
호스트 환경에서 `--issuer woori` 단일 카드사 증분 수집 배치를 실행했을 때 다음과 같은 예외가 발생하며 중단되었습니다:
```text
cardrag_worker.partial_execution.PartialExecutionError: ('issuer_scope_revision_source_missing', 'scope')
```
- **근본 원인**: `issuer_scope.py`의 `preserve_inputs()` 함수가 `source.seal`의 모든 `unresolved_revision_entries`를 순회하면서 `known.get(entry["source_id"])`를 조회했습니다.
- 여기서 `source_metadata_unresolved` 사유를 가진 4건(하나 1건, 현대 3건)은 과거 레거시 `pdf_cache_source` 테이블에는 존재하나 현재의 `_known_snapshot_sources` (즉, 디스커버리 스냅샷 및 시드 메타데이터)에는 존재하지 않았습니다.
- 그 결과, 선택되지 않은 카드사(하나, 현대)의 과거 미해결 레거시 이력 때문에 우리카드 증분 배치가 시작 단계에서 실패하는 구조적 결함이 확인되었습니다.

### 1.2 사용자 정책 및 `PLAN_AMENDMENT_01.md` 반영
- 최신 상품의 정확성과 배치 정상 처리가 우선입니다.
- 현재 시점에 존재하지 않는 카드의 복구 불가능한 과거 이력(메타데이터 부재, 캐시 객체 부재, 현재 문서 충돌)을 복원하기 위해 과도하게 복잡한 리졸버나 하드코딩된 예외 목록을 도입하지 않습니다.
- 과거 불능 이력은 명시적인 정비(remediation) 테이블(`pdf_cache_remediated_revision`)에 기록하고, 활성 게시 및 개정 확장 대상에서 안전하게 제외합니다.
- 운영 DB 수작업 SQL 수정 금지, 불변 seal 및 DB 임의 덮어쓰기 금지, 비선택 카드사(7개사) 서빙 문서 및 CAS 객체 100% 보존 제약을 준수했습니다.

---

## 2. 주요 변경 사항 및 구현 상세

### 2.1 상태 지속성 (`apps/cardrag-worker/src/cardrag_worker/state.py`)
- 정비 이력을 영구 보존하는 전용 DDL 추가 (`STRICT`, `WITHOUT ROWID`):
  ```sql
  CREATE TABLE IF NOT EXISTS pdf_cache_remediated_revision (
      source_id TEXT NOT NULL,
      pdf_sha256 TEXT NOT NULL,
      reason_code TEXT NOT NULL,
      disposition TEXT NOT NULL CHECK (disposition IN ('updated_to_latest', 'past_excluded', 'current_error')),
      remediated_at TEXT NOT NULL,
      PRIMARY KEY (source_id, pdf_sha256)
  );
  CREATE INDEX IF NOT EXISTS idx_pdf_cache_remediated_lookup
      ON pdf_cache_remediated_revision(source_id, pdf_sha256, reason_code);
  ```
- `WorkerState` 헬퍼 메서드 추가:
  - `pdf_cache_source_issuer(source_id)`: `pdf_cache_source` 테이블에서 `source_id`에 해당하는 발급사 코드를 안전하게 조회 (스냅샷 메타데이터가 없는 레거시 source도 정확히 발급사 판별 가능).
  - `record_remediated_revision(...)`: 정비 결과 영구 저장 (`INSERT OR REPLACE`).
  - `is_revision_remediated(...)`: 특정 identity가 정비 완료되었는지 고속 조회.
  - `remediated_revisions()`: 전체 정비 목록 조회.

### 2.2 미해결 이력 정비 로직 모듈 (`apps/cardrag-worker/src/cardrag_worker/unresolved_remediation.py`)
- 신규 모듈 신설:
  - `UnresolvedInventoryItem`: 베이스라인 미해결 항목의 분석 모델.
  - `UnresolvedRemediationPlan`: 정비 대상 전체 계획 모델.
  - `analyze_unresolved_entry()`: 개별 identity의 최신 활성 문서 충돌, 캐시 객체 존재 여부, 메타데이터 유무를 판별하여 처분(`disposition`: `past_excluded`, `updated_to_latest`, `current_error`) 결정.
  - `plan_unresolved_remediation()`: 베이스라인 seal과 state를 비교하여 전체 정비 계획 수립.
  - `apply_unresolved_remediation()`: 정비 계획을 `pdf_cache_remediated_revision` 테이블에 원자적으로 반영.

### 2.3 비선택 카드사 보존 경계 수정 (`apps/cardrag-worker/src/cardrag_worker/issuer_scope.py`)
- `preserve_inputs()` 함수 개편:
  1. `source_id`의 발급사 식별 시 `known.get(source_id)` 뿐 아니라 `pipeline.state.pdf_cache_source_issuer(source_id)`를 fallback으로 조회.
  2. 선택된 발급사(`issuer in selected`)의 이력은 이번 배치에서 최신 목록을 기준으로 새로 평가되므로 보존 목록에서 제외.
  3. 비선택 발급사의 이력 중 `state.is_revision_remediated(...)`이거나 메타데이터가 없는 과거 레거시 이력은 안전하게 필터링(제외)하여 `PartialExecutionError` 발생 방지.
  4. 유효한 비선택 발급사 미해결 이력만 정상 계승.

### 2.4 파이프라인 및 개정 확장 방어 (`apps/cardrag-worker/src/cardrag_worker/pipeline.py` & `partial_execution.py`)
- `pipeline._run_locked()`:
  - 베이스라인 보존 소스(`_preserved_source`) 바인딩 직후 자동으로 `plan_unresolved_remediation()` 및 `apply_unresolved_remediation()`을 수행하여 런타임 자체 치유 보장.
- `pipeline.pdf_revision_expansion()`:
  - 정비 완료(`is_revision_remediated`)된 identity는 개정 확장 시 `unresolved_revision_entries`에 다시 추가되지 않도록 차단.
  - `document_identity_collision`, `pdf_cache_object_unavailable`, `source_metadata_unresolved` 재유입 완벽 방지.
- `partial_execution._build_preserved_v5_generation()`:
  - 정비된 과거 항목을 제외하고 `unresolved_revision_sha256` 해시를 재계산하여 불변 검증 통과.

### 2.5 운영 CLI 도구 (`apps/cardrag-worker/src/cardrag_worker/cli.py`)
- 명령어 추가: `cardrag-worker remediate-unresolved-history [--dry-run | --apply]`
  - 사전 진단(`--dry-run`) 및 확정 적용(`--apply`) 지원.
  - JSON 형식의 상세 인벤토리 및 처분 결과 출력.

---

## 3. 인수 조건(AC) 달성 증거

| 인수 조건 | 충족 내용 | 검증 테스트 |
| :--- | :--- | :--- |
| **AC1: 22건 베이스라인 인벤토리 및 처분 명시** | `document_identity_collision`(17건), `source_metadata_unresolved`(4건), `pdf_cache_object_unavailable`(1건)에 대해 각각 `past_excluded`, `updated_to_latest`, `current_error` 처분 분기를 정확히 구분하고 설명함. | `test_plan_and_apply_unresolved_remediation_all_three_reasons` |
| **AC2: 현재 문서 우선 및 공유 CAS 보호** | 과거 충돌 identity가 제외되더라도 현재 활성 문서의 `document_id`와 공유 CAS 본문은 100% 보존되며 파일이 임의 삭제되지 않음. | `test_preserve_inputs_with_missing_source_metadata_does_not_fail` |
| **AC3: 재실행 멱등성 및 재유입 방지 펜스** | `pdf_cache_remediated_revision` 테이블 및 개정 확장 필터를 통해 동일 배치 재실행, 다음 카드사 실행, 전체 재개 시 과거 오류 identity가 재유입되지 않음. | `test_cli_remediate_unresolved_history_dry_run_and_apply` |
| **AC4: 정상 미존재 vs 실패 구분** | 어댑터 목록 수집 실패/부분 수집은 에러로 처리되어 보존을 유지하며, 정상적으로 확인된 미존재 레거시 이력만 정비 제외됨. | `test_normal_absence_vs_failure_distinction` |
| **AC5: 8사 5,536 문서 보존 및 비선택 어댑터 호출 0건** | `--issuer woori` 실행 시 비선택 7개사 어댑터 네트워크 호출이 0건이며, 5,536개 서빙 문서 세트가 온전히 보존됨. | `test_selected_run_preserves_serving_and_skips_nonselected_adapters` |
| **AC6: 회귀 검증 및 코드 품질** | 기존 017 범위 테스트(57건), 파이프라인 v5 테스트(13건), 전체 워커 테스트(1,375건) 및 ruff 검사 100% 통과. | `test_017_scope.py`, 전체 워커 pytest, `ruff check/format` |

---

## 4. 검증 실행 결과

### 4.1 신규 전용 검증 테스트 (`test_019_unresolved_history.py`)
```bash
$ uv run --no-sync pytest apps/cardrag-worker/tests/test_019_unresolved_history.py -v
============================== 5 passed in 1.72s ===============================
```
- `test_plan_and_apply_unresolved_remediation_all_three_reasons`: 3대 사유별 처분 로직 검증 PASS
- `test_preserve_inputs_with_missing_source_metadata_does_not_fail`: 메타데이터 누락 레거시 이력이 있어도 크래시 없이 정상 보존 PASS
- `test_normal_absence_vs_failure_distinction`: 정상 미존재와 어댑터 수집 실패의 상태 구분 검증 PASS
- `test_cli_remediate_unresolved_history_dry_run_and_apply`: CLI `--dry-run` 및 `--apply` 멱등성 PASS
- `test_selected_run_preserves_serving_and_skips_nonselected_adapters`: 비선택사 호출 0건 및 서빙 유지 PASS

### 4.2 기존 핵심 기능 회귀 검증
```bash
$ uv run --no-sync pytest apps/cardrag-worker/tests/test_017_scope.py
============================= 57 passed in 8.79s ===============================

$ uv run --no-sync pytest apps/cardrag-worker/tests/test_pipeline_v5.py
============================= 13 passed in 2.53s ===============================

$ uv run --no-sync pytest apps/cardrag-worker/tests/test_revision_history_v5.py
============================== 5 passed in 0.17s ===============================
```

### 4.3 전체 워커 테스트 스위트
```bash
$ uv run --no-sync pytest apps/cardrag-worker/tests/
======================= 1375 passed, 10 warnings in 49.89s =======================
```

### 4.4 린트 및 코드 포맷
```bash
$ uv run --no-sync ruff check apps/cardrag-worker/
All checks passed!

$ uv run --no-sync ruff format --check apps/cardrag-worker/
125 files already formatted
```

---

## 5. 계획 변경 대비 편차 (Deviations from PLAN.md)

1. **무조건적 22건 identity 계승 철회 (`PLAN_AMENDMENT_01.md` 승인 반영)**:
   - 당초 `PLAN.md`는 22건의 원래 identity와 사유를 새 seal에 영구 계승하고 메타데이터 복원 리졸버를 구축하도록 제안했으나, `PLAN_AMENDMENT_01.md`에 따라 이를 전면 철회했습니다.
   - 대신 지속성 테이블(`pdf_cache_remediated_revision`)을 통한 과거 불능 이력의 배제 정책을 확립하여 런타임 안정성과 코드 단순성을 확보했습니다.
2. **비침습적 자동 정비 메커니즘**:
   - 별도 일회성 스크립트를 수동 실행하지 않아도 파이프라인의 `_run_locked` 진입 시 베이스라인의 미해결 항목을 자동 검사하여 안전하게 정비 테이블에 반영되도록 설계했습니다. 동시에 운영자가 수동으로 진단할 수 있도록 CLI 커맨드도 함께 제공합니다.

---

## 6. 운영 배포 및 핸드오프 절차

운영 환경(호스트)에서 배포 및 재검증을 진행할 때는 다음 절차를 권장합니다:

1. **워커 이미지 빌드 및 배포**:
   - 수정된 코드베이스로 워커 컨테이너 이미지를 빌드합니다.
2. **미해결 이력 사전 진단 (선택 사항)**:
   - 컨테이너 내부 또는 마운트된 환경에서 CLI 실행:
     ```bash
     cardrag-worker remediate-unresolved-history --dry-run
     ```
   - 처분 계획(disposition) 확인.
3. **우리카드 단일 증분 실행 재검증 (`--issuer woori`)**:
   - 호스트에서 중단되었던 우리카드 증분 배치를 재실행합니다:
     ```bash
     # 또는 compose / docker run 환경에 맞추어 실행
     cardrag-worker run --issuer woori
     ```
   - `issuer_scope_revision_source_missing` 에러 없이 정상 완료되는지 확인.
   - 서빙 문서 수량 및 우리카드 최신 개정본 반영 상태 검증.
4. **나머지 7개 카드사 순차 검증**:
   - `hana`, `hyundai`, `bc`, `kb`, `lotte`, `samsung`, `shinhan` 순차 실행.
   - 각 사 실행 시 과거 이력의 충돌 없이 최신 목록을 기준으로 정상 갱신되는지 확인.
5. **자동 스케줄러(Cron/Timer) 복원**:
   - 8개사 전체 수동 단일 배치가 안정적으로 완료된 후 타이머를 활성화.
