# 019 FIX_10 REPORT — generation-only OCR metadata 가시성 보존 및 로컬 캐시 재바인딩

작성: 2026-10-20 Executor (Antigravity). `FIX_10.md`의 [P1] 항목을 구현하고 검증했다. 기존 handoff 문서(PLAN, REPORT, FIX_01–10 및 각 REPORT 포함)는 일체 수정하지 않았으며, HEAD `aaeb2c4` 위 017–019 미커밋 작업트리를 보존했다. 리뷰 지침에 따라 배포·원격 flush·운영 원장 수정·배치/timer 실행은 수행하지 않았다.

## 1. [P1] generation-only(null seal reuse key) OCR metadata 가시성 및 재바인딩

### 결함 원인 분석

- 신규 OCR을 수행한 문서(generation-only)는 seal의 `ocr_reuse_key` 및 `ocr_cache_kind`가 `null`이다.
- run-local native manifest(`native-manifest.json`)가 유실/삭제된 경우, key 기반 필수 경로 생성 분기(`if key:`)를 타지 못한다.
- 기존 recorder에 의해 `backup_meta`에 `doc_map_<man_rel>` 및 `doc_map_<ready_rel>`이 선택 문서 ID로 귀속되어 기록되어 있었으나, `scoped_backup_scope`의 doc_map 처리 루프가 `if rel in pending_paths:`로 제한되어 있었다.
- 따라서 pending 행이 없거나 삭제된 경우 metadata 경로가 `paths`(allowed)에서 완전히 누락되었고, 경로 단위 unbound 검사에서도 제외되어 CAS만 전송된 후 `backup_scope_complete=True`로 잘못 판정되었다.

### 구현 (`apps/cardrag-worker/src/cardrag_worker/backup.py`)

1. **pending 독립적인 가시성 보존 (`scoped_backup_scope`)**:
   - `doc_map_%` 태그 순회 시 `if rel in pending_paths:` 제약을 제거하여, pending 행의 유무와 무관하게 선택 문서(`selected_docs`)에 기록되었던 모든 metadata 경로를 `paths`(allowed)에 보존.
   - `doc_map` 자체는 가시성 근거일 뿐 전송 권위를 부여하지 않으므로, 검증 근거 없는 단독 바인딩은 수행하지 않음.
2. **로컬 캐시 기반 정식 재바인딩**:
   - `rel not in expected`인 경우, `doc_info_%` 메타데이터(또는 `v1/ocr-cache/native/...` 경로 구조)로부터 `reuse_key`를 확인.
   - worker의 로컬 OCR 캐시(`cache/ocr/{reuse_key}`)에 대해 `_cache_bound_ocr_pair`를 호출하여 엄격한 암호학적 검증(canonical manifest, matching READY, sealed doc의 PDF/OCR SHA256 및 크기 일치)을 수행.
   - 검증 통과 시 `expected`에 바인딩(`_bind`)하여 정식 전송 권위를 부여하고, 실패 또는 증거 부재 시에는 `paths`에만 남겨 `unbound`로 유지.
3. **과거 리비전 메타데이터 분리 (인수 기준 4)**:
   - 동일 문서 ID(`document_id`)에 대해 과거 run/리비전의 `doc_map` 레코드가 남아 있는 경우, `doc_info`의 `pdf_sha256`/`ocr_sha256` 대조.
   - 현재 리비전이 이미 유효한 metadata로 바인딩된 경우 과거 리비전 메타데이터는 scope에 불필요하게 포함되지 않도록 무시.
   - 현재 리비전의 검증 근거가 없어 판정할 수 없는 경우, 완료를 단정하지 않도록 unbound로 유지하여 scope complete를 차단.
4. **비선택/정책 경계 보존**:
   - `doc_id not in selected_docs`로 비선택 7사 및 타 문서 기록을 철저히 차단.
   - adopted/content cache 제외 정책 유지.

## 2. Reviewer 재현 및 저장소 회귀 검증

Reviewer의 결함 재현 시나리오를 `test_fix_06_scoped_backup.py`에 영구 회귀 테스트로 이관하고, 인수 기준 3에 따라 null key와 key 존재 두 형태 모두에서 다양한 변형 시나리오를 추가했다:

1. `test_generation_only_metadata_retains_required_unbound_without_rows`:
   - **Reviewer 재현**: generation-only 문서 등록 후 run manifest 삭제 및 pending 행 제거 (receipt 없음).
   - manifest/READY가 `allowed`에 유지되고 `expected`에는 제외됨을 검증.
   - flush 결과: `in_scope_unbound == 2`, `in_scope_missing == 0`, `backup_scope_complete is False`, `status != "succeeded"`.
2. `test_generation_only_metadata_with_receipt_remains_unbound_without_evidence`:
   - pre-existing receipt가 있더라도 로컬 검증 근거가 없으면 unbound 유지 및 완료 차단.
3. `test_generation_only_metadata_rebinds_from_local_cache`:
   - run manifest가 유실되었으나 로컬 캐시(`cache/ocr/{key}`)에 완전무결한 증거가 존재할 때, 정식 재바인딩되어 `client.objects`에 전송되고 `backup_scope_complete is True`.
4. `test_generation_only_metadata_tampered_cache_remains_unbound`:
   - 캐시 증거가 변조(invalid json/불일치)된 경우 재바인딩 거부 및 unbound 유지.
5. `test_generation_only_metadata_unchanged_rerun_completes`:
   - generation-only flush 성공 후 재실행 시 unchanged 상태에서 receipts+index 기반 `backup_scope_complete is True`.
6. `test_generation_only_metadata_deferred_status_reports_unbound`:
   - 읽기 전용/deferred 진단(`scoped_pending_diagnostics`)에서 쓰기 없이 `backup_in_scope_unbound == 2`, `backup_scope_complete is None` 정확히 보고.
7. `test_stale_past_revision_metadata_ignored_when_current_bound`:
   - 현재 리비전이 정상 바인딩된 경우, 동일 문서의 과거 리비전 doc_map 기록은 scope allowance에 불필요하게 유입되지 않음.
8. `test_stale_past_revision_metadata_unbound_when_current_not_bound`:
   - 현재 리비전 미바인딩 시 과거 기록 판정 불가로 완료 단정 방지 (`backup_scope_complete is False`).

## 3. 검증 결과

```text
$ uv run --no-sync pytest apps/cardrag-worker/tests/ -q
1443 passed, 10 warnings in 54.24s     # FIX_09 종료 시 1435 + 신규 8건

$ uv run --no-sync pytest apps/cardrag-worker/tests/test_fix_06_scoped_backup.py -v
53 passed in 2.16s

$ uv run --no-sync ruff check apps/cardrag-worker/
All checks passed!

$ uv run --no-sync ruff format --check apps/cardrag-worker/
126 files already formatted

$ git diff --check
(통과)
```

## 4. 편차·남은 한계·운영 미검증

1. **편차**: 없음. Reviewer의 FIX_10 인수 기준 1~4를 정확히 충족했다.
2. **운영 복구 명령 계약 유지**: `FIX_09_REPORT.md`에서 확인된 entrypoint 규격(`/opt/cardrag/current/operations/worker-compose.sh run --rm --no-deps worker backup flush --run <run_id> --issuer <issuer>`) 및 `backup status --run` 읽기 전용 진단 계약을 그대로 보존한다.
3. **운영 미검증**: 본 작업은 로컬 SQLite 원장 및 mock client 회귀 범위에서 수행되었으며, 실제 운영 배포, 운영 WebDAV 대상 원격 쓰기, 운영 원장 수정, 타이머/배치 재개는 수행하지 않았다.
4. **인계**: 모든 변경사항은 미커밋 작업트리 상태로 Reviewer(Codex)에게 인계한다.
