# 019 FIX_12_REPORT — generation-only OCR variant 백업과 기존 유효 index 보존

2026-10-20 Executor: Antigravity. Reviewer(Codex)의 `FIX_12.md` 요구사항 및 `LIVE_REVIEW_02.md`의 실환경 근거에 따라 동일 native key를 공유하는 복수 정상 OCR variant 공존 백업 경로 및 누락 index 회수 메커니즘을 구현하고 검증을 완료했다.

---

## 1. 해결 요약 및 설계 결정

### [P1-1] 동일 native key의 정상 OCR variant 공존 경로 분기 (`resolve_ocr_bundle_paths`)

- **문제 원인**:
  - `native_ocr_reuse_key`는 PDF 원문 SHA256과 OCR Contract(processor, prompt, renderer)에 의해 결정된다.
  - 동일한 PDF와 Contract를 사용하더라도 provider 비결정론(OCR 모델 출력 차이, 줄바꿈, 토큰 생성 시점 등)으로 인해 서로 다른 정상 OCR 출판물(variant)이 생성될 수 있다.
  - 실환경 key `94cdb144c916a6cae490f6cae6fe89139bf6dfc3f7ec32643149261efa73352f`의 경우, 원격에는 10월 14일 18:39에 생성된 정상 canonical pair(A)가 영수증 및 원격 객체로 보존되어 있고, 현재 seal은 10월 14일 23:41에 생성된 generation-only local variant(B)를 참조하고 있었다.
  - 기존 코드는 generation-only variant B를 canonical cache 경로(`v1/ocr-cache/native/{prefix}/{key}/manifest.json`)에 직접 매핑하여 영수증 SHA 불일치로 인한 `in_scope_conflicts`가 발생하고 전송이 차단되었다.
- **해결 방식**:
  1. **Core 경로 확장**:
     - `packages/cardrag-core/src/cardrag_core/paths.py`에 `ocr_variant_root_path`, `ocr_variant_manifest_path`, `ocr_variant_ready_path` 구현 및 `packages/cardrag-core/src/cardrag_core/__init__.py`에 공개 API로 등록.
     - 경로 구조: `v1/ocr-cache/{kind}/{key[:2]}/{key}/variants/{ocr_sha}/manifest.json` 및 `READY.json`.
  2. **증거 기반 정상 variant 판별 (`_is_valid_remote_ocr_variant`)**:
     - 원격 영수증의 SHA 불일치를 무조건 variant로 우회하면 가짜/손상 영수증에 대한 방어가 무력화되므로, canonical 영수증이 다음 조건을 모두 만족할 때만 정상 variant로 인정:
       - `READY.json` 영수증 동반.
       - Spool, 로컬 캐시, Client 객체 또는 `backup_meta`의 `doc_info`를 통해 실제 바이트를 검증하여 `OCRArtifactManifest`의 유효성, canonical 바이트 일치성, 동일 `reuse_key`, 동일 `source.pdf_sha256`이 입증됨.
       - `output.sha256`이 로컬 variant와 다름.
  3. **경로 라우팅 (`resolve_ocr_bundle_paths`)**:
     - 문서가 generation-only(null seal reuse key)이고, 원격 canonical cache 경로에 이미 다른 정상 variant가 영수증으로 검증되어 있는 경우에만 variant 경로로 라우팅.
     - 정상 영수증이 없거나 손상된 영수증인 경우에는 기존대로 canonical 경로를 유지하여 기존 복구/충돌 차단 메커니즘이 정상 동작하도록 보장.
     - `verified_document_ocr_bundles`, `BackupLedger.record_document_ocr`, `scoped_backup_scope` Phase 1/Phase 2에 일관 적용.
  4. **Phase 2 Supersede 한정**:
     - variant 경로로 바인딩된 문서에 한해서만, 동일 `reuse_key`의 canonical 경로를 superseded로 처리하여 위조/미등록 키에 대한 orphan 검출 계약을 완벽히 보존.
  5. **복원 연계 (`restore`)**:
     - `v1/ocr-cache/native/.../variants/{ocr_sha}/manifest.json` 복원 시 로컬 캐시 `cache/ocr/{key}/variants/{ocr_sha}/` 경로에도 시딩하고, 영수증 대조 시 exact manifest SHA를 매칭.

---

### [P1-2] 기존 유효 variant의 index 항목 보존 및 자동 회수 (`_publish_index_and_pointer`)

- **문제 원인**:
  - `flush` 시점의 index 필터가 `expected_artifacts`에 포함된 경로의 SHA와 `items_by_path`의 SHA가 다르면 해당 항목을 `pop(rel_p)`하여 제거했음.
  - 이전에는 generation-only variant B가 canonical 경로를 expected로 등록했기 때문에, 원격에 이미 존재하던 유효 canonical pair A가 index에서 삭제되는 부작용이 발생했음.
  - 또한 pending이 0건인 상태에서는 `flush`가 조기 반환(`status: unchanged`)되어 누락된 index 항목을 회수할 기회가 없었음.
- **해결 방식**:
  1. **Variant 분기를 통한 Index 항목 보존**:
     - variant B가 variant 경로(`v1/ocr-cache/native/.../variants/{ocr_sha}/...`)를 expected로 가지므로, canonical 경로 항목(A)과 충돌하지 않음.
     - 결과적으로 unrelated pending 전송이나 variant B 전송 시 기존 A 항목이 `index.json`에서 제거되지 않고 보존됨.
  2. **Index 누락 자동 감지 및 회수 (`needs_index_repair`)**:
     - `flush` 실행 시, pending 큐가 비어 있거나 이번 배치에서 새로 업로드된 항목이 없더라도 `force=True`인 경우 원격 `v1/backup/index.json`을 검사.
     - `backup_receipts`에 등록된 유효 영수증 중 원격 index에 누락되었거나 SHA가 불일치하는 항목이 발견되면 `needs_index_repair = True` 플래그를 활성화.
     - 조기 반환하지 않고 `_publish_index_and_pointer`를 호출하여 전체 유효 영수증이 온전히 반영된 새 index 배치 및 포인터를 원자적으로 갱신 발행.

---

## 2. 변경된 파일 목록

1. `packages/cardrag-core/src/cardrag_core/paths.py`:
   - `ocr_variant_root_path(reuse_key, ocr_sha, kind)`
   - `ocr_variant_manifest_path(reuse_key, ocr_sha, kind)`
   - `ocr_variant_ready_path(reuse_key, ocr_sha, kind)` 구현
2. `packages/cardrag-core/src/cardrag_core/__init__.py`:
   - 위 3개 함수 export 및 `__all__` 등록
3. `apps/cardrag-worker/src/cardrag_worker/backup.py`:
   - `_is_valid_remote_ocr_variant`: canonical 영수증의 유효 variant 여부 다계층 증거 검증
   - `resolve_ocr_bundle_paths`: canonical vs variant 번들 경로 결정
   - `verified_document_ocr_bundles`: variant 경로 적용
   - `BackupLedger.record_document_ocr`: variant 경로 등록
   - `_cache_bound_ocr_pair`: variant 캐시 경로(`cache/ocr/{key}/variants/{ocr_sha}/manifest.json`) 탐색 추가
   - `scoped_backup_scope`: 8개 segment variant 경로 rebind 지원 및 Phase 2 supersede 한정
   - `flush`: 누락 영수증 자동 회수를 위한 `needs_index_repair` 검사 및 `_publish_index_and_pointer` 보강
   - `restore`: variant 경로 복원 및 로컬 variant 캐시 시딩 지원
4. `apps/cardrag-worker/tests/test_fix_06_scoped_backup.py`:
   - FIX_12 인수 기준을 충족하는 5개 신규 테스트 추가:
     - `test_generation_only_ocr_variant_coexists_with_canonical_remote_pair`
     - `test_unrelated_pending_flush_preserves_canonical_remote_pair_in_index`
     - `test_missing_index_entry_recovered_on_flush`
     - `test_variant_restored_to_local_cache_and_seed`
     - `test_corrupted_canonical_receipt_still_blocks_transport_and_reports_conflict`

---

## 3. 검증 결과

### 1) 단위 및 회귀 테스트
- `apps/cardrag-worker/tests/test_fix_06_scoped_backup.py`:
  - **62 passed** (기존 57개 + FIX_12 신규 5개 전부 통과, 2.19초)
- 전체 Worker 테스트 스위트 (`apps/cardrag-worker/tests/`):
  - **1452 passed**, 10 warnings (54.15초)
- Core 테스트 스위트 (`packages/cardrag-core/tests/`):
  - **267 passed** (2.54초)

### 2) 린터 및 포맷팅 검증
- `uv run --no-sync ruff check packages/cardrag-core/ apps/cardrag-worker/`:
  - **All checks passed!**
- `git diff --check`:
  - Whitespace 오류 없음.

---

## 4. 실환경 읽기 전용 대조 및 안전 복구 가이드

운영 환경(배포 후)에서 key `94cdb144c916a6cae490f6cae6fe89139bf6dfc3f7ec32643149261efa73352f` 및 누락된 index 항목을 안전하게 확인하고 정식 회수하기 위한 절차:

### 1) 읽기 전용 대조 명령
```bash
# 1. 원장 영수증 및 메타 상태 조회 (읽기 전용)
python3 -c "
import sqlite3, json
conn = sqlite3.connect('file:/opt/cardrag/state/backup-ledger.sqlite3?mode=ro', uri=True)
c = conn.cursor()
key = '94cdb144c916a6cae490f6cae6fe89139bf6dfc3f7ec32643149261efa73352f'
print('Receipts:')
for row in c.execute('SELECT remote_path, sha256, size_bytes FROM backup_receipts WHERE remote_path LIKE ?', (f'%{key}%',)):
    print(row)
print('Pending:')
for row in c.execute('SELECT item_id, remote_path, sha256, status FROM backup_pending WHERE remote_path LIKE ?', (f'%{key}%',)):
    print(row)
"

# 2. 선택 실행 백업 상태 점검 (읽기 전용)
python -m cardrag_worker.cli backup status --run 41ce678e665b47179f8bfb2d82273882 --issuer woori
```

### 2) 정식 회수 및 백업 완료 절차
수작업 SQL 수정이나 원격 index 파일 임의 편집을 절대 수행하지 않고, 정식 CLI 명령으로 회수:
```bash
# 누락된 index 항목 자동 복구 및 variant B 전송 수행
python -m cardrag_worker.cli backup flush --run 41ce678e665b47179f8bfb2d82273882 --issuer woori
```
- **기대 동작**:
  1. Canonical pair A 영수증이 원격 index에 누락되어 있음을 감지하여 `_publish_index_and_pointer`가 A를 index에 자동 회수.
  2. Local variant B는 canonical 경로를 덮어쓰지 않고 `v1/ocr-cache/native/94/94cdb1.../variants/{ocr_sha}/manifest.json` 및 `READY.json`으로 안전하게 전송.
  3. `backup_scope_complete: true`, `conflicts: 0`, `missing: 0` 판정으로 완료.

---

## 5. 제약 준수 및 보고

- 본 작업은 로컬 mock/단위 테스트 환경에서만 수행되었으며, 실제 운영 배포, 운영 WebDAV 대상 쓰기, 운영 원장 수정, 배치/timer 기동은 일체 수행하지 않았습니다.
- 비선택 7개 카드사 및 현재 serving 5537건 / 우리카드 980건 계약을 온전히 보존했습니다.
- HEAD `aaeb2c4` 위의 기존 미커밋 작업트리 및 `.handoff/` 내 모든 이전 문서를 손상 없이 보존했습니다.
