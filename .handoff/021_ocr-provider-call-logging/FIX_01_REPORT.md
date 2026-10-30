# 021 FIX_01 Executor 구현 보고

2026-10-28 KST. `FIX_01.md`를 읽고 최종 OCR 검증 실패 진단 누락을 보완했다. 운영 배포·실제 외부 OCR 호출은 수행하지 않았다.

## 시작 상태와 보존

- 시작 시점 HEAD `06af791b6db3fa250b8b543deed46f77f3a4a7d7`. 021 구현(ocr_logging.py 신규, ocr.py/pipeline.py/providers.py/docs/OPERATIONS.md 수정, test_ocr_provider_logging.py 신규)과 020 Worker 변경이 미커밋 상태다. 이 모두를 보존하고 FIX_01 변경을 추가했다.
- 기존 PLAN.md, REPORT.md, FIX_01.md는 덮어쓰지 않았다.

## 수정 내용

### `apps/cardrag-worker/src/cardrag_worker/ocr_logging.py`

1. `_diagnostic` 함수에 `OCRVerificationError` 분류를 추가했다. `cardrag_core.ocr.OCRVerificationError`에 대해 고정 코드 `final_ocr_verification_failed`, `error_kind=validation`을 반환한다. import는 `_diagnostic` 내부에서 lazy하게 수행해 순환 import를 피했다. `cardrag_core`를 import하지 못하면 `unknown`으로 fall-through한다.
2. `validation_failed(exc, *, validation_phase=None)` 함수에 선택적 `validation_phase` 파라미터를 추가했다. 기존 호출(`ocr_logging.validation_failed(exc)`)은 그대로 동작하며, 새 경계에서는 `validation_phase="final_whole_document"` 또는 `"final_chunk_combine"`을 전달한다.

### `apps/cardrag-worker/src/cardrag_worker/ocr.py`

FIX_01이 지목한 두 경계에 `try/except`로 `ocr_logging.validation_failed` 호출을 추가했다. 예외는 그대로 전파한다.

1. **whole-document 경계** (OCRResolver.resolve 내 DocumentOCRProvider 분기, 구 2811행):
   ```python
   try:
       reject_credential_bearing_ocr(body)
       verified = verify_ocr_bytes(body, expected_page_count=page_count)
   except Exception as exc:
       ocr_logging.validation_failed(exc, validation_phase="final_whole_document")
       raise
   ```

2. **chunk 결합 후 경계** (OCRResolver.resolve 내 chunk 경로 결합 후, 구 3035행):
   ```python
   try:
       reject_credential_bearing_ocr(body)
       # Verify exactly the bytes that will be cached and referenced by generations.
       verified = verify_ocr_bytes(body, expected_page_count=page_count)
   except Exception as exc:
       ocr_logging.validation_failed(exc, validation_phase="final_chunk_combine")
       raise
   ```

`reject_credential_bearing_ocr`는 `ProviderSystemicError`를 발생시켜 기존 `_diagnostic`에서 이미 분류된다. `verify_ocr_bytes`는 `OCRVerificationError`를 발생시켜 이번에 추가된 분류로 처리된다. storage/cache publication 실패와 validation 실패를 혼동하지 않는다.

### `apps/cardrag-worker/tests/test_ocr_provider_logging.py`

FIX_01 재현 시나리오를 두 테스트로 추가했다.

1. `test_fix01_whole_document_final_verify_emits_validation_failed`: `FakeDocumentProvider`를 상속해 `recognize_document`가 `## Page 99\n\n...` 본문을 반환하도록 구현했다. page-value 정규화는 통과하지만 `verify_ocr_bytes`에서 `[1, 99]`≠`[1]` 마커 불일치로 `OCRVerificationError` 발생. `ocr_output_validation_failed` 이벤트의 `reason_code=final_ocr_verification_failed`, `validation_phase=final_whole_document`, `call_id` 연결, call_returned→validation_failed 순서, summary 수치를 검증한다.
2. `test_fix01_chunk_combine_final_verify_emits_validation_failed`: `FakeProvider`를 상속해 청크 응답에 NUL(`\x00`) 바이트를 주입. 결합 후 `verify_ocr_bytes`가 `OCRVerificationError` 발생. `validation_phase=final_chunk_combine`과 `reason_code=final_ocr_verification_failed` 이벤트를 확인하고, caplog 전체 기록에 NUL 원문이 없음을 검증한다.

## 검증

```
uv run --no-sync pytest apps/cardrag-worker/tests/test_ocr_provider_logging.py -q
18 passed in 0.52s
```

```
uv run --no-sync pytest apps/cardrag-worker/tests/test_ocr_provider_logging.py apps/cardrag-worker/tests/test_fix_24_provider_diagnostics.py apps/cardrag-worker/tests/test_paddle_inputs_v130.py apps/cardrag-worker/tests/test_cli_settings_provider.py -q
187 passed, 1 warning in 4.53s
```

```
uv run --no-sync pytest apps/cardrag-worker/tests/ packages/cardrag-core/tests/ tests/runtime_v1/ -q
1 failed, 1987 passed, 11 warnings in 64.63s
```

실패 1건은 `test_repository_gitleaks_policy_rejects_openrouter_and_github_tokens`로 이 환경에 `gitleaks` 바이너리가 없어 발생하는 환경 의존 실패다. 021 이전과 동일하며 본 FIX_01 변경과 무관하다.

```
uv run --no-sync ruff check apps/cardrag-worker/ packages/cardrag-core/
All checks passed!

uv run --no-sync ruff format --check <변경 파일 3개>
3 files already formatted

uv run --no-sync mypy apps/cardrag-worker/src/cardrag_worker/ocr_logging.py apps/cardrag-worker/src/cardrag_worker/ocr.py apps/cardrag-worker/src/cardrag_worker/pipeline.py apps/cardrag-worker/src/cardrag_worker/providers.py
Success: no issues found in 4 source files

git diff --check
통과
```

## 계획 대비 조정과 제한

- FIX_01이 지적한 두 경계(`whole-document`, `chunk 결합 후`)에만 관측을 추가했다. 다른 `reject_credential_bearing_ocr`/`verify_ocr_bytes` 호출 경계(chunk 단위 저장 직전 등)에는 FIX_01 범위에 해당하지 않아 수정하지 않았다.
- 집계는 변경하지 않았다. `ocr_provider_call_returned` 이후 최종 검증이 실패해도 `returned_total`은 이미 증가한 상태다. FIX_01이 "이미 반환된 시도를 transport 실패로 다시 세지 않는다"고 명시했으므로 집계를 소급 수정하지 않았다.
- 예외 타입·hierarchy, fallback 조건, 020의 alias/reap/cancellation, cache/manifest 계약은 유지했다. `ocr_logging.validation_failed` 내부 오류는 원래 예외를 가리지 않는다.
- 실제 운영 배포·릴리스는 수행하지 않았다.
