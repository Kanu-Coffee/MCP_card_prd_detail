# 021 FIX_01 — 최종 OCR 검증 실패의 진단 누락

2026-10-28 KST, Reviewer Codex. PLAN, REPORT, 현재 Git status/diff, 새 로깅 모듈 및 테스트를 대조했다. **현 구현은 추가 수정 후 재검토한다.** 본 문서는 구현 지시이며 Reviewer가 production 코드를 수정하지 않았다.

## P2: 최종 `verify_ocr_bytes` 오류도 validation 이벤트로 남겨야 한다

위치: `apps/cardrag-worker/src/cardrag_worker/ocr.py`의 whole-document 분기 약 2800–2812행과 chunk 결합 후 약 3033–3036행, `ocr_logging.py`의 안전한 오류 분류.

현재 `_validate_and_normalize_target_page_values` 및 `_validated_provider_target_values`의 일부 예외만 `ocr_output_validation_failed`로 기록한다. 이후 `reject_credential_bearing_ocr(body)` 및 `verify_ocr_bytes(body, expected_page_count=page_count)` 경계에는 대응 이벤트가 없다. `verify_ocr_bytes`는 `cardrag_core.ocr.OCRVerificationError`를 발생시키며 새 diagnostic classifier도 이 타입의 원인을 분류하지 않는다.

특히 whole-document provider가 각 페이지의 본문 문자열에 추가 `## Page 99` 마커를 포함하면 page-value 정규화는 통과하지만 최종 전체 바이트 검증은 실패한다. 이때 실제 로그에는 provider 응답 반환과 partial summary만 있고 어느 문서/호출의 어떤 검증이 실패했는지 빠진다. chunk 결과의 forbidden NUL/CR 등 최종 검증 오류에도 동일한 누락 가능성이 있다. primary 응답 반환을 OCR 완료로 해석해서는 안 되는 이번 과제의 핵심 인수 기준을 충족하지 못한다.

## 재현 증거

외부 API 및 실제 Paddle 모델 없이 임시 디렉터리의 WorkerState와 runtime-checkable DocumentOCRProvider fake로 재현했다.

- fake provider: `provider=local-paddleocr`, `model=PaddleOCR-VL-1.6`, `renderer_id=paddlex-pdfium/300dpi`, `render_scale_milli=4167`.
- `recognize_document` 반환: `('## Page 99\n\nThis is a sufficiently long OCR page body.',)`.
- `OCRResolver.resolve` 인자: page_count=1, run/document scope 설정, 임시 PDF와 임시 output_dir.
- 발생 예외: `OCRVerificationError`.
- 관측 이벤트 순서:

```text
ocr_provider_call_started
ocr_provider_call_returned
OCRVerificationError 발생
ocr_invocation_summary (partial=true)
```

`ocr_output_validation_failed` 또는 해당 원인의 이벤트가 없다. 이 예외는 현재 FailoverOCRResolver의 OCRValidationError 분기에도 해당하지 않는다. **이 FIX를 이유로 예외 타입을 바꾸거나 새로운 fallback/retry를 허용하지 않는다.** 기존 예외 정책을 그대로 유지하면서 진단 누락만 보완한다.

## 필요한 수정

1. whole-document 및 chunk 결합 후의 최종 credential/byte 검증을 관측하고, 검증 실패 시 `ocr_output_validation_failed`에 안전한 이유·검증 단계·문서/페이지 문맥 및 가능한 관련 call_id를 남긴 뒤 원래 예외를 그대로 전파한다. storage/cache publication 실패를 validation 실패로 묶지 않는다.
2. `OCRVerificationError`에는 원문 exception/body를 출력하지 않고 허용된 고정 코드로 최종 검증 실패를 분류한다. 세부 원인을 안전하게 얻기 어려우면 `final_ocr_verification_failed` 같은 고정 코드만으로도 충분하다. arbitrary `str(exc)` 출력이나 원문 기반 분류 확대를 요구하지 않는다. credential 오류는 기존 안전 reason code를 보존한다.
3. 집계는 provider의 반환과 최종 검증 실패를 구분한다. 이미 반환된 시도를 provider transport 실패로 다시 세지 않는다. cache/checkpoint-only 검증이면 새 호출/순번을 생성하지 말고 과거 attempt를 이번 실패의 호출로 오인시키지 않는다.
4. 오류 로깅·분류 실패가 원래 예외를 가리지 않아야 한다. 기존 020 alias/reap/cancellation, fallback 허용 조건, 반환 bytes/hash 및 cache/manifest 계약은 유지한다.

## 검증

- 위 whole-document 반환 fixture에서 `call_returned` 이후 검증 실패 이벤트가 한 번 발생하고 reason/phase/run/document/page와 관련 call_id를 확인한다. 최종 예외는 동일한 `OCRVerificationError`이며 결과가 정상 확정되지 않아야 한다.
- chunk provider의 길이가 충분한 정상 마커 응답에 forbidden NUL/CR을 포함한 fixture로 최종 검증 경로도 확인한다. 원문 sentinel·OCR 본문은 실제 stderr formatter와 caplog에 없어야 한다.
- logger failure, cached/checkpoint 재사용, 실패/취소 집계, fallback 원인 연결, 기존 Paddle 테스트를 재검증한다. 실제 provider 호출/운영 변경은 필요하지 않다.
- 보완 후 PLAN의 관련 회귀·ruff/format/type 검사를 실행하고 새 `FIX_01_REPORT.md`에 실제 결과, 변경 및 한계를 기록한다. 기존 PLAN/REPORT/본 FIX는 보존한다.

## Reviewer가 확인한 검사

```text
uv run --no-sync pytest apps/cardrag-worker/tests/test_ocr_provider_logging.py apps/cardrag-worker/tests/test_fix_24_provider_diagnostics.py apps/cardrag-worker/tests/test_paddle_inputs_v130.py apps/cardrag-worker/tests/test_cli_settings_provider.py -q
185 passed, 1 warning in 4.55s

uv run --no-sync ruff check apps/cardrag-worker/ packages/cardrag-core/
All checks passed!

uv run --no-sync mypy apps/cardrag-worker/src/cardrag_worker/ocr_logging.py apps/cardrag-worker/src/cardrag_worker/ocr.py apps/cardrag-worker/src/cardrag_worker/pipeline.py apps/cardrag-worker/src/cardrag_worker/providers.py
Success: no issues found in 4 source files

git diff --check
통과
```

위 테스트 통과와 별개로 새 최종 검증 실패 fixture는 기존 전용 테스트에 없었다. REPORT의 전체 회귀 수치를 Reviewer가 새로 수행한 것으로 재기록하지 않는다. 020 및 사용자 변경은 보존했고, 운영 배포·릴리스·배치 실행은 수행하지 않았다.
