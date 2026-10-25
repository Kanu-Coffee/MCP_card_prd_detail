# 019 FIX_03_REPORT — FIX_03 조치 및 최종 정합성 검증 완료 보고

- **작성일**: 2026-10-19
- **작성자**: Antigravity (Executor)
- **대상 작업**: `019_selected-issuer-unresolved-history-preservation`
- **검토 문서**: `.handoff/019_selected-issuer-unresolved-history-preservation/FIX_03.md` (Reviewer: Codex)

---

## 1. 개요 및 처리 요약

Reviewer Codex의 `FIX_03.md`에서 지적된 3건의 P1 결함 및 테스트 근거 보완 요구사항을 모두 수정하고, Worker 전체 테스트 슈트(1385 passed) 및 전용 단위/수명주기 테스트(15 passed)로 검증을 완료했다.

| 번호 | 항목 | 판정 | 조치 요약 |
|---|---|---|---|
| 1 | [P1] DRM 항목 최종 정비 시 `AttributeError`로 인한 게시 실패 | **해결** | `UnsupportedProductRecord.source` 및 `_UnsupportedAttachment.source` 계약에 맞추어 issuer/product_code 접근 수정, DRM 및 ZIP 첨부파일 모두 `unsupported_map`에 집계 |
| 2 | [P1] 최종 ledger 변경 후 `corpus_sha256` 미재계산으로 인한 Hash Drift | **해결** | 최종 정비(`final`) 및 replay 단계에서 `_v5_corpus_identity_payload()`를 통해 최종 ledger와 count/SHA를 결합한 `corpus_sha256` 재계산 및 manifest/seal 반영, 재실행 시 `no_change` 안정성 확보 |
| 3 | [P1] CLI baseline authentication 및 actual current serving 미인증 | **해결** | CLI `--apply` 시 WebDAV의 `current_serving_generation_id`를 획득하여 rollback 포인터 불일치 차단, on-disk 등록 seal 원본 대조, DB에 revision 연결이 없는 미인증 항목을 `current_error`로 격리하여 허위 suppression 차단 |
| 보완 | `test_collect_then_process_remediation_lifecycle` 테스트 보완 | **해결** | `ReuseSource` 대신 실제 `AcquisitionSource` 및 어댑터 source identity를 사용하도록 갱신하여 수집(collect)→처리(process) 수명주기 및 hash 일치 검증 완료 |

---

## 2. 세부 조치 내역

### 2.1 [P1] DRM / ZIP 지원불가 항목 최종 정비 계약 수정

- **문제 위치**: `apps/cardrag-worker/src/cardrag_worker/pipeline.py:6318–6322`, `partial_execution.py:776–781`.
- **원인**: `unsupported` 목록의 항목 타입은 `UnsupportedProductRecord`이며, 속성은 `.record`가 아니라 `.source: SourceRecord`임. `.record` 접근 시 `AttributeError`가 발생하여 정상 문서까지 배치가 중단됨. 또한 `unsupported_attachments`(ZIP 등)가 누락되어 DRM/ZIP 통합 격리 계약이 누락됨.
- **수정 내용**:
  1. `pipeline.py`:
     - 개정 확장 단계(4453행) 및 최종 정비 단계(6318행):
       `doc.source.issuer` 및 `doc.source.product_code`로 속성 접근 수정.
     - `self._unsupported_attachments` 순회 시 `hasattr(doc, "source")`를 통해 `doc.source.issuer` 및 `doc.source.product_code`를 `unsupported_map`에 추가.
  2. `partial_execution.py`:
     - `replay_pdf_source`: `unsupported` 및 `_unsupported_attachments`를 `doc.source` 기반으로 `unsupported_map`에 구성.
- **테스트 추가 및 검증**:
  - `test_normal_document_plus_drm_succeeds_without_attribute_error`:
    정상 문서 1건 + DRM 보호 문서 1건(`SCDSA002` 매직바이트)을 혼합한 파이프라인 실행 시 `AttributeError`나 `WorkerUnexpectedFailureError` 없이 `succeeded` 완료되고, 정상 문서는 게시되며 DRM 문서는 unsupported/degraded로 격리됨을 검증.

### 2.2 [P1] 최종 Ledger 반영 시 `corpus_sha256` 재계산 및 Hash Drift 방지

- **문제 위치**: `apps/cardrag-worker/src/cardrag_worker/pipeline.py:6355–6375`, `partial_execution.py:820–855`.
- **원인**: 수집 직후 계산된 `corpus_sha256`이 post-OCR 최종 정비(1→0 ledger 축소) 이후 재계산되지 않은 채 `_build_v5_generation()`에 전달됨. 그 결과 manifest의 `corpus_sha256`과 최종 ledger의 canonical payload가 불일치하여, 다음 실행 시 입력 변화가 없음에도 hash drift로 인해 불필요한 재게시(`succeeded`)가 발생하는 결함.
- **수정 내용**:
  1. `pipeline.py`:
     - 최종 정비 필터링 직후, 새 `unresolved_revision_ledger`와 `unresolved_revision_sha256`을 인수로 `_v5_corpus_identity_payload()`를 호출하여 `corpus_payload` 및 `corpus_sha256`을 재계산.
     - `self.performance.set("corpus_sha256", corpus_sha256)` 갱신 후 `_build_v5_generation`에 갱신된 `corpus_sha256`을 전달.
  2. `partial_execution.py`:
     - `replay_pdf_source`: `unresolved_ledger != raw_unresolved`인 경우, `(*processed, *failed)` 모든 획득 문서를 포함한 `replayed_acquired`를 생성하여 `_v5_corpus_identity_payload()`로 `replayed_corpus_sha256`을 재계산. 변경이 없는 경우에는 `source.manifest.corpus_sha256`을 그대로 보존.
- **테스트 추가 및 검증**:
  - `test_final_ledger_and_corpus_hash_agreement_and_no_change`:
    과거 미해결 1건이 현재 OCR 완료로 정비(1→0)된 후 게시된 manifest의 `corpus_sha256`이 0건의 canonical payload와 정확히 일치함을 확인. 동일 입력으로 즉시 재실행 시 `status == "no_change"`로 불필요한 재게시 없이 안정됨을 검증.

### 2.3 [P1] CLI Baseline Authentication 및 Actual Current Serving 인증

- **문제 위치**: `apps/cardrag-worker/src/cardrag_worker/unresolved_remediation.py:133–180, 440–445`, `cli.py:remediate_unresolved_history_command`.
- **원인**:
  1. SQLite `published_at DESC` 최신 행만 비교하여, 롤백 등으로 WebDAV current 포인터와 DB 최신 행이 어긋난 경우 잘못된 baseline이 적용될 위험.
  2. `pdf_cache_source` 행만 존재하고 `pdf_cache_source_revision` 연결이 없는 임의 입력 entry가 offline 모드에서 `past_excluded`로 분류되어 suppression DB를 오염시키는 위험.
- **수정 내용**:
  1. `cli.py`:
     - `--apply` 적용 시 WebDAV / LocalPublisherWebDAV에서 `cur_gen = await webdav.validated_current_generation()`을 조회하여 `current_serving_generation_id`를 획득, `plan_unresolved_remediation`에 전달.
     - 중복 실패 출력을 유발하던 CLI 상위의 임의 디스크 검사를 제거하고 검증 로직을 `unresolved_remediation.py`에 일원화.
  2. `unresolved_remediation.py`:
     - `validate_unresolved_ledger_in_seal`:
       `current_serving_generation_id`가 전달된 경우 seal의 `generation_id`와 일치하는지 엄격 검증(불일치 시 ValueError).
       seal에 `manifest`가 포함된 경우 `GenerationManifest` pydantic 모델 검증 및 `publish` 테이블의 `corpus_sha256`, `contract_sha256`, `serving_sha256`과 암호학적 해시 대조.
       디스크에 해당 run의 `runs/<run_id>/sealed/publish.json`이 존재하는 경우 `historical_revision_unresolved_sha256`의 일치 여부 대조.
     - `analyze_unresolved_entry`:
       오프라인 CLI 모드(`current_discovery is None`)에서 `rev_row is None`인 경우(DB revision 연결이 없는 항목):
       `disposition = "current_error"`, `explanation = "Unverified revision identity not found in database source revisions; error preserved"`로 설정하여 `past_excluded`나 DB suppression 반영을 원천 차단.
- **테스트 추가 및 검증**:
  - `test_cli_serving_rollback_and_unlinked_revision_protection`:
    1. 롤백 상황(`current_serving_generation_id="gen_1"`, 대상 seal="gen_2"): `ValueError("not the current serving publication")` 발생 확인.
    2. 미연결 revision(source는 있으나 revision row 부재): `current_error` 판정 및 `applied_count == 0`, suppression 미반영 확인.
  - `test_cli_rejects_unpublished_and_fabricated_seals`:
    미게시 run/generation seal 거부 확인.

### 2.4 테스트 보완: 실제 `AcquisitionSource` 수명주기 검증

- **수정 파일**: `apps/cardrag-worker/tests/test_019_unresolved_history.py`
- **조치 내용**:
  `test_collect_then_process_remediation_lifecycle`에서 이전 baseline `ReuseSource`를 재사용하던 부분을 실제 `AcquisitionSource(baseline_source.root, collect_res.run_id, state.path)`로 교체하고, 어댑터의 실제 `rec.issuer`, `rec.product_code` 기반 과거 이력을 주입하여:
  - 수집(collect) 단계: expansion 인벤토리 생성 및 `is_revision_remediated == False` 확인.
  - 처리(process) 단계: 실제 acquisition receipt 기반 OCR 처리 및 최종 정비 완료 후 `is_revision_remediated == True`, `updated_to_latest` 확인.

---

## 3. 검증 결과 및 증거

### 3.1 Worker 전체 테스트 슈트

```text
export PATH="$HOME/.local/bin:$PATH" && uv run --no-sync pytest apps/cardrag-worker/tests/ -q
1385 passed, 10 warnings in 51.24s
```

### 3.2 019 전용 테스트 슈트

```text
export PATH="$HOME/.local/bin:$PATH" && uv run --no-sync pytest apps/cardrag-worker/tests/test_019_unresolved_history.py -v
============================== 15 passed in 2.66s ==============================
- test_plan_and_apply_unresolved_remediation_all_three_reasons PASSED
- test_preserve_inputs_with_missing_source_metadata_does_not_fail PASSED
- test_normal_absence_vs_failure_distinction PASSED
- test_cli_remediate_unresolved_history_dry_run_and_apply PASSED
- test_selected_run_preserves_serving_and_skips_nonselected_adapters PASSED
- test_disk_validation_active_vs_historical_revision PASSED
- test_validate_unresolved_ledger_security_and_authenticity PASSED
- test_pipeline_discovery_and_acquisition_failures PASSED
- test_corrupted_baseline_seal_ledger_fails_fast_in_pipeline PASSED
- test_ocr_failure_does_not_persist_updated_to_latest PASSED
- test_collect_then_process_remediation_lifecycle PASSED
- test_cli_rejects_unpublished_and_fabricated_seals PASSED
- test_normal_document_plus_drm_succeeds_without_attribute_error PASSED
- test_final_ledger_and_corpus_hash_agreement_and_no_change PASSED
- test_cli_serving_rollback_and_unlinked_revision_protection PASSED
```

### 3.3 부분 실행(Partial Execution) 테스트 슈트

```text
export PATH="$HOME/.local/bin:$PATH" && uv run --no-sync pytest apps/cardrag-worker/tests/test_partial_execution.py -q
43 passed in 17.67s
```

### 3.4 린트 및 서식 검사

```text
export PATH="$HOME/.local/bin:$PATH" && uv run --no-sync ruff check apps/cardrag-worker/
All checks passed!

export PATH="$HOME/.local/bin:$PATH" && uv run --no-sync ruff format --check apps/cardrag-worker/
125 files already formatted

export PATH="$HOME/.local/bin:$PATH" && git diff --check
(출력 없음 - 통과)
```

---

## 4. 변경 파일 목록

| 파일 경로 | 변경 내용 |
|---|---|
| `apps/cardrag-worker/src/cardrag_worker/pipeline.py` | expansion/final 단계의 unsupported DRM 속성 접근 수정(`.source`), 최종 정비 후 `_v5_corpus_identity_payload`로 `corpus_sha256` 재계산 및 `performance.set` 반영 |
| `apps/cardrag-worker/src/cardrag_worker/partial_execution.py` | `replay_pdf_source`에서 `doc.source` 접근 수정 및 `unresolved_ledger` 변경 시 `(*processed, *failed)` 포함하여 `replayed_corpus_sha256` 재계산 |
| `apps/cardrag-worker/src/cardrag_worker/unresolved_remediation.py` | `current_serving_generation_id` 검증, seal 암호학적 해시 대조, on-disk 원본 대조, offline 모드 unlinked revision의 `current_error` 격리 |
| `apps/cardrag-worker/src/cardrag_worker/cli.py` | `--apply` 시 WebDAV `current_serving_generation_id` 연동 및 상위 중복 검사 정리 |
| `apps/cardrag-worker/tests/test_019_unresolved_history.py` | DRM 비충돌, corpus hash 일치 및 no-change 재실행 안정성, CLI 롤백 및 미연결 리비전 보호, `AcquisitionSource` 수명주기 테스트 추가 및 갱신 |

---

## 5. 잔여 위험 및 운영 권고사항

- **실운영 미적용**: 본 작업에서는 실제 배포, 라이브 카드사 스크래핑, systemd timer 수정을 일체 수행하지 않았음.
- **운영 롤아웃 절차 유지**:
  1. 단일 issuer 수동 실행: `uv run cardrag-worker run --selected-issuers woori`
  2. degraded/unsupported 및 백업 상태 확인: `uv run cardrag-worker backup status`
  3. 나머지 7개 카드사 순차 검증
  4. 정상 확인 후 timer 주기 정상화
