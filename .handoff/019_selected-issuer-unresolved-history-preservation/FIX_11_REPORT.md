# 019 FIX_11 REPORT — 캐시 재바인딩과 과거 metadata 제외의 2단계 순서 무관 판정

작성: 2026-10-20 Executor (Antigravity). `FIX_11.md`의 [P2] 항목을 구현하고 검증했다. 기존 handoff 문서(PLAN, REPORT, FIX_01–11 및 각 REPORT 포함)는 일체 수정하지 않았으며, HEAD `aaeb2c4` 위 017–019 미커밋 작업트리를 보존했다. 리뷰 지침에 따라 배포·원격 flush·운영 원장 수정·배치/timer 실행은 수행하지 않았다.

## 1. [P2] 캐시 재바인딩과 과거 metadata 제외의 조회 순서 의존 제거

### 결함 원인 분석

- 기존 구현에서는 `doc_map_%` 순회 시 단일 패스(single-pass) 루프에서 `doc_has_bound_metadata`를 갱신하며 과거 기록 제외 여부를 판정했다.
- run-local native manifest가 없고 현재 metadata를 로컬 캐시(`cache/ocr/{key}`) 근거로 재바인딩해야 하는 경우, 현재 항목을 읽기 전까지는 `doc_has_bound_metadata.get(doc_id)`가 `False`였다.
- 따라서 SQLite 원장 행 조회 순서상 과거 기록이 현재 기록보다 먼저 읽히면, 과거 항목이 `paths`(allowed)에 unbound로 추가되어 버렸고, 이후 현재 항목이 캐시에서 정상 바인딩되어도 이미 추가된 과거 경로 때문에 `in_scope_unbound=1`, `backup_scope_complete=False`로 남아버리는 순서 의존성(P2)이 발생했다.

### 구현 (`apps/cardrag-worker/src/cardrag_worker/backup.py`)

`scoped_backup_scope`의 `doc_map` 처리 루프를 **2단계(Two-phase) 순서 무관 구조**로 리팩토링했다:

1. **Phase 1 (Rebinding Pass)**:
   - `tagged` 행들을 순회하며 `is_stale_revision`이 아닌 현재 리비전 후보들에 대해 로컬 캐시(`_cache_bound_ocr_pair`) 재바인딩을 우선 수행.
   - 유효한 캐시 증거(canonical manifest, matching READY, matching PDF/OCR hash)가 확인되면 `expected`에 바인딩하고 `doc_has_bound_metadata[doc_id] = True`를 확정.
   - stale revision은 해시 불일치로 Phase 1 재바인딩 대상에서 자연스럽게 제외됨.
2. **Phase 2 (Visibility & Scope Pass)**:
   - 전체 선택 문서의 바인딩 상태가 확정된 후 `tagged_items`를 순회하며 가시성을 결정.
   - `is_stale_revision and doc_has_bound_metadata.get(doc_id)`: 현재 리비전의 검증 근거가 이미 바인딩된 경우, 과거 기록은 정책상 scope allowance에서 제외(`continue`).
   - 현재 리비전의 검증 근거가 없거나(`doc_has_bound_metadata`가 False), 증거만으로 귀속을 판정할 수 없는 경우: 완료 단정을 방지하기 위해 `paths.add(rel)`하여 unbound로 유지하고 scope complete를 차단.
3. **순서 무관성 보장**:
   - SQLite 행 순서, 문자열 정렬, 과거→현재 vs 현재→과거, manifest vs READY 순열과 무관하게 항상 동일한 `allowed`, `expected`, `backup_scope_complete` 판정을 산출.

## 2. Reviewer 재현 및 저장소 회귀 검증

Reviewer의 순서 의존 결함 재현 시나리오와 인수 기준에 따른 순열·손상 테스트 4건을 `test_fix_06_scoped_backup.py`에 추가했다:

1. `test_stale_history_ordering_does_not_affect_scope_resolution`:
   - **Reviewer 재현 검증**: 과거 기록 우선 삽입(`past_first`)과 현재 기록 우선 삽입(`current_first`) 두 순서 모두에서 동일하게 과거 기록 `old_man`이 `allowed`에서 제외되고, 현재 캐시 항목이 바인딩되어 `backup_scope_complete is True` 달성.
2. `test_manifest_ready_permutations_order_independent`:
   - `doc_map` 내 manifest 우선 vs READY 우선 삽입 순열에서 동일하게 정상 바인딩 및 완료 달성.
3. `test_stale_history_retained_when_current_cache_corrupted`:
   - 현재 항목의 캐시가 변조/손상된 경우 재바인딩 실패로 `doc_has_bound_metadata`가 False로 유지되며, 과거 기록도 판정 불가로 `allowed`에 남아 `backup_scope_complete is False`로 완료 차단 유지.
4. `test_stale_history_with_pending_or_receipt_not_force_transported`:
   - 과거 기록에 pending 행이나 receipt가 남아 있더라도, 현재 리비전이 정상 바인딩된 경우 과거 기록은 scope에서 안전하게 제외되어 강제 전송되거나 원장에서 부당하게 삭제되지 않음.

## 3. 검증 결과

```text
$ uv run --no-sync pytest apps/cardrag-worker/tests/ -q
1447 passed, 10 warnings in 53.97s     # FIX_10 종료 시 1443 + 신규 4건

$ uv run --no-sync pytest apps/cardrag-worker/tests/test_fix_06_scoped_backup.py -v
57 passed in 2.19s

$ uv run --no-sync ruff check apps/cardrag-worker/
All checks passed!

$ uv run --no-sync ruff format --check apps/cardrag-worker/
126 files already formatted

$ git diff --check
(통과)
```

## 4. 편차·남은 한계·운영 미검증

1. **편차**: 없음. Reviewer의 FIX_11 요구사항 및 인수 기준을 충족했다.
2. **운영 복구 명령 및 진단 계약 유지**: `worker-compose.sh run --rm --no-deps worker backup flush --run <run_id> --issuer <issuer>` 및 `backup status --run` 읽기 전용 진단 계약 보존.
3. **운영 미검증**: 실제 배포, 운영 WebDAV 대상 원격 쓰기, 운영 원장 수정, 타이머/배치 재개는 수행하지 않았다.
4. **인계**: 모든 변경사항은 미커밋 작업트리 상태로 Reviewer(Codex)에게 인계한다.
