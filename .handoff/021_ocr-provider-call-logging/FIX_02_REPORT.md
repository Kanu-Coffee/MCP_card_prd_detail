# 021 FIX_02 Executor 구현 보고

2026-10-28 KST. `FIX_02.md`를 읽고 전체 문서 검증 오류가 마지막 청크에 오귀속되던 문제를 해결했다. 운영 배포나 실제 외부 OCR 호출은 수행하지 않았다.

## 시작 상태와 보존

- 시작 시점 HEAD `06af791b6db3fa250b8b543deed46f77f3a4a7d7`. 020 Worker 변경, 021 구현 및 FIX_01 수정사항이 작업 트리에 미커밋 상태로 존재했다. 이를 보존하고 FIX_02 수정과 테스트를 추가했다.
- 기존 `PLAN.md`, `REPORT.md`, `FIX_01.md`, `FIX_01_REPORT.md`, `FIX_02.md`는 덮어쓰지 않았다.

## 문제 원인과 해결 내용

### 문제 원인

`ocr_logging.py`의 `validation_failed`가 검증 단계와 무관하게 `doc.last_call`의 모든 필드를 그대로 복사했다. 이로 인해 여러 청크를 결합한 후 수행되는 최종 전체 문서 검증(`final_chunk_combine`)에서 실패할 때도 직전에 실행된 마지막 청크의 `call_id`, `chunk_index`, `page_start`, `page_end`가 오류 이벤트로 출력되었다. 그 결과, 첫 번째 청크가 잘못되고 마지막 청크는 정상이었음에도 오류 로그가 정상인 마지막 청크를 원인 호출/위치로 오인하게 만드는 문제가 있었다.

### 구현

1. **`apps/cardrag-worker/src/cardrag_worker/ocr_logging.py` (`validation_failed`)**:
   - `validation_scope: str | None = None` 및 `page_count: int | None = None` 인자를 추가했다.
   - `validation_phase == "final_chunk_combine"` 또는 `validation_scope == "document"`일 때 문서 전체 범위 검증 모드로 전환:
     - `validation_scope = "document"` 명시.
     - 원인 호출로 단정할 수 없으므로 `call_id = None`, `chunk_index = None`으로 설정.
     - 개별 청크 종속 필드(`call_kind`, `call_ordinal`, `chunk_index`, `page_start`, `page_end`, `input_page_count`)를 제거.
     - 직전 호출이 있었을 경우 참고 정보로서 `last_call_id`에만 남기고 원인 호출 필드로 사용하지 않음.
     - 청크 범위(예: 2..2) 대신 문서 전체 페이지 범위(`page_start=1`, `page_end=page_count`, `input_page_count=page_count`)를 명시.
   - 단일 응답 검증(개별 chunk, corrective) 및 `final_whole_document`의 기존 `call_id` 연결 동작은 그대로 유지.

2. **`apps/cardrag-worker/src/cardrag_worker/ocr.py` (`OCRResolver.resolve`)**:
   - chunk 결합 후 `verify_ocr_bytes` 검증 실패 시:
     `ocr_logging.validation_failed(exc, validation_phase="final_chunk_combine", validation_scope="document", page_count=page_count)` 전달.
   - 원래 예외(`OCRVerificationError`, `ProviderSystemicError` 등)는 그대로 전파.

3. **`apps/cardrag-worker/tests/test_ocr_provider_logging.py` (회귀 테스트 추가 및 보강)**:
   - `test_fix02_first_chunk_error_does_not_attribute_to_last_chunk`:
     2페이지 문서에서 1번째 청크 본문에만 NUL이 있고 2번째 청크는 정상인 경우, 검증 실패 이벤트가 정상인 2번째 청크의 `call_id`/`chunk_index`를 복사하지 않고 `call_id=None`, `chunk_index=None`, `last_call_id=B의_call_id`, `validation_scope=document`, `page_start=1`, `page_end=2`로 기록되는지 검증.
   - `test_fix02_checkpoint_only_combine_failure_has_no_call_attribution`:
     모든 청크가 체크포인트에서 재사용되어 신규 provider 호출이 0회인 상태에서 결합 검증 실패 시, `starts` 0회, `call_id=None`, `last_call_id=None`, `validation_scope=document`, `page_start=1`, `page_end=2`, `provider_attempts_total=0` 검증.
   - 기존 `test_fix01_chunk_combine_final_verify_emits_validation_failed`에 `call_id is None`, `validation_scope == "document"`, `page_start == 1`, `page_end == 2` 검증 추가.

## 검증 결과

```bash
# 1. 신규 FIX_02 테스트 포함 전용 로깅 테스트
uv run --no-sync pytest apps/cardrag-worker/tests/test_ocr_provider_logging.py -v
20 passed in 0.57s

# 2. 지정 관련 스위트 (logging + diagnostics + paddle + cli)
uv run --no-sync pytest apps/cardrag-worker/tests/test_ocr_provider_logging.py apps/cardrag-worker/tests/test_fix_24_provider_diagnostics.py apps/cardrag-worker/tests/test_paddle_inputs_v130.py apps/cardrag-worker/tests/test_cli_settings_provider.py -q
189 passed, 1 warning in 4.63s

# 3. 전체 회귀 테스트 (worker + core + runtime_v1)
PATH="/home/lee/.local/bin:$PATH" uv run --no-sync pytest apps/cardrag-worker/tests/ packages/cardrag-core/tests/ tests/runtime_v1/ -q
1990 passed, 11 warnings in 66.90s
(Reviewer 확인대로 PATH에 /home/lee/.local/bin을 추가하여 gitleaks 테스트 포함 전체 1990건 100% 통과)

# 4. 정적 분석 및 포맷팅 검사
uv run --no-sync ruff check apps/cardrag-worker/ packages/cardrag-core/
All checks passed!

uv run --no-sync ruff format --check apps/cardrag-worker/src/cardrag_worker/ocr_logging.py apps/cardrag-worker/src/cardrag_worker/ocr.py apps/cardrag-worker/tests/test_ocr_provider_logging.py
3 files already formatted

uv run --no-sync mypy apps/cardrag-worker/src/cardrag_worker/ocr_logging.py apps/cardrag-worker/src/cardrag_worker/ocr.py apps/cardrag-worker/src/cardrag_worker/pipeline.py apps/cardrag-worker/src/cardrag_worker/providers.py
Success: no issues found in 4 source files

git diff --check
통과
```

## 불변 조건 및 남은 위험

- 예외 타입, fallback/retry 조건, 020 alias/reap/cancellation 동작, cache/manifest 계약은 변경되지 않았다.
- 결합 검증 실패 시 특정 청크를 임의로 지목하지 않고 `validation_scope=document`, `call_id=None`으로 기록하므로, 잘못된 청크/호출로 오진하는 위험이 해소되었다.
- 실제 운영 배포 및 외부 유료 OCR API 호출은 수행하지 않았다.
