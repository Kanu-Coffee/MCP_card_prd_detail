# 019 FIX_24_REPORT — OCR 미분류 프로세스 종료의 안전한 진단 보완

2026-10-21 Executor. `PLAN.md`, `FIX_24.md`, `LIVE_REVIEW_09.md`, 기존 019 FIX/REPORT를 확인한 뒤 코드와 테스트를 수정했다. 기존 handoff와 미커밋 변경은 보존했으며 운영 run·DB·checkpoint·volume·배포를 변경하거나 resume을 시작하지 않았다.

## 확인한 현재 구조

- 실제 systemic failure record는 `contracts.py`가 아니라 `apps/cardrag-worker/src/cardrag_worker/pipeline.py`의 `OCRSystemicFailureRecord`다.
- Codex nonzero 종료는 `providers.CodexOCRProvider.recognize()`에서 stderr만 allowlist 분류하고 exit code·stderr 길이/해시만 보존했다.
- OpenCode nonzero 종료는 stderr와 stdout 전체를 합쳐 분류하여, 일반 OCR text event의 단어도 재분류에 사용될 수 있었다.
- OCR provider 호출 지점(`ocr.OCRResolver`)은 chunk index와 target page 범위를 알고 있었고, `FailoverOCRResolver`는 primary/fallback 단계를 알고 있었지만 실패 record에 전달하지 않았다.
- FIX_24의 “contracts.py systemic failure record” 설명과 실제 파일 구조가 달라, 기존 record가 있는 `pipeline.py`를 확장했다.

## 변경 내용

### 1. Provider 안전 진단 확장 — `providers.py`

`ProviderSystemicError`에 선택 진단 필드를 추가했다.

- `provider`: 검증된 provider 식별자
- `phase`: 요청 단계. provider 실패에서는 `primary`/`fallback`
- `stdout_size_bytes`, `stdout_sha256`
- `classification_basis`: allowlist 분류 근거 또는 unknown 근거
- `chunk_index`, `page_start`, `page_end`

모든 신규 필드는 선택 사항이며 형식·길이·page 범위를 검증한다. 원시 stdout/stderr, prompt, OCR 본문, URL, 예외 text/args/notes/context는 저장하지 않는다. `with_request_context()`는 실패 경로에서 진단 부착이 원래 provider 오류를 덮어쓰지 않도록 best-effort로 동작한다.

Codex process 종료는 stderr allowlist 결과와 근거(`stderr_allowlist`, `stderr_allowlist_no_match`)를 함께 보존하고 stdout은 길이/해시만 남긴다. exit 0이면 stderr에 오해 가능한 문구가 있어도 분류하지 않는다.

OpenCode process 종료는 다음처럼 좁혔다.

- stderr는 기존 allowlist로 분류
- stdout은 공식 `type="error"` JSON event의 `message`/`error`/`data`만 분류에 사용
- 일반 `type="text"` OCR 본문은 분류에서 제외
- 공식 error event가 없거나 allowlist와 맞지 않으면 `provider_process_exit_unknown` 유지

Paddle process 종료도 stdout 길이/해시와 provider를 보존하되, 기존 fixed exit 정책(`provider_process_configuration_failed`)은 변경하지 않았다.

### 2. 요청 문맥 부착 — `ocr.py`

- chunk provider 호출과 sparse-page corrective 호출에 `provider`, `phase="primary"`, `chunk_index`, target `page_start`/`page_end`를 부착한다.
- document provider 호출에는 `page_start=1`, `page_end=page_count`를 부착한다.
- `FailoverOCRResolver`는 primary 실패를 `primary`, fallback 실패를 `fallback`으로 표시한다.
- FIX_23의 `codex-exec` → `local-paddleocr` quota fallback만 유지하고, unknown·인증·설정·credential·무결성·취소·fallback 실패 정책은 변경하지 않았다.

### 3. 기록·보고서·CLI — `pipeline.py`, `cli.py`

- `OCRSystemicFailureRecord`에 동일한 선택 필드와 검증을 추가했다.
- `_classify_ocr_systemic_failure()`가 신규 필드를 canonicalize한다. 선택 진단이 손상되어도 유효한 기존 reason/exit/stderr를 지우지 않는다.
- report payload와 CLI systemic failure echo에 값이 있을 때만 신규 필드를 포함한다.
- schema version, OCR contract, cache key, epoch, model, settings, 성공 결과 형식, checkpoint 형식을 변경하지 않았다. DB migration도 없다.
- 기존 report는 신규 필드가 없는 상태로 그대로 읽을 수 있다.

### 4. 운영 문서

`docs/OPERATIONS.md`에 nonzero provider 종료 report가 provider/phase, chunk/page, stdout/stderr 길이·SHA-256, allowlist 근거만 남기고 원문을 저장하지 않는다는 설명을 추가했다.

## 테스트

신규 `apps/cardrag-worker/tests/test_fix_24_provider_diagnostics.py` 11건과 기존 provider 테스트 보완으로 다음을 검증했다.

- nonzero unknown Codex 종료의 stdout/stderr 길이·해시·provider·분류 근거
- 기존 typed provider 오류의 분류 근거
- exit 0 + 오해 가능한 stderr header가 실패로 분류되지 않음
- OpenCode 일반 OCR text event의 quota 단어가 재분류를 일으키지 않음
- 공식 OpenCode `type="error"` event만 quota로 분류됨
- 임의 OCR 본문·secret sentinel이 문자열/traceback/report에 유출되지 않음
- chunk 실패의 provider/phase/chunk/page 정확성
- document provider 실패의 전체 page 범위
- fallback 실패의 `phase="fallback"` 및 unknown 차단
- quota→Paddle fallback 허용과 unknown primary 차단
- pipeline report와 CLI echo의 신규 필드
- 구형 record payload의 선택 필드 호환성 및 신규 필드 검증
- 손상된 선택 진단이 기존 unknown reason을 지우지 않음
- 기존 cancellation/fallback 회귀 테스트 유지

실행 결과:

```text
uv run --no-sync pytest apps/cardrag-worker/tests/test_fix_24_provider_diagnostics.py -q
11 passed, 1 warning in 0.51s

uv run --no-sync pytest apps/cardrag-worker/tests/ packages/cardrag-core/tests/ -q
1819 passed, 11 warnings in 58.30s

uv run --no-sync ruff check apps/cardrag-worker/ packages/cardrag-core/
All checks passed!

uv run --no-sync ruff format --check apps/cardrag-worker/ packages/cardrag-core/
159 files already formatted

git diff --check
통과
```

경고는 기존 cache/fallback 진단과 신규 fallback 테스트의 의도된 `RuntimeWarning`이다.

## 범위 판단과 제한

- 이번 변경은 **관측성 보완**이다. 원래 2,512바이트 stderr는 복구되지 않았고, 실제 upstream 원인을 quota·transient·기타 중 하나로 확정하지 않았다. 다음 재발 시에도 분류 불가면 `provider_process_exit_unknown`으로 정확히 남는다.
- Codex는 현재 `exec` 호출에 검증된 JSON error stdout 계약이 없으므로 stdout을 실패 분류에 사용하지 않고 길이/해시만 보존한다. OpenCode의 공식 error event만 구조화 채널로 사용했다. 이 차이는 의도된 보수적 선택이다.
- 기존 `OCRSystemicFailureRecord.phase` 필드는 이미 cache publication 단계에 사용되고 있었다. provider 실패에서는 기존에 값이 없었으므로 같은 선택 필드를 요청 단계(`primary`/`fallback`)로 사용했고, cache report의 기존 값과 의미는 바꾸지 않았다.
- 실제 Paddle 모델 로딩·CPU 추론, 롯데 resume 완주, 517건/174 checkpoint의 운영 후속 상태는 이번 코드 테스트로 검증하지 않았다. 운영 run은 failed 상태로 두었고 자동 배치를 시작하지 않았다.
- 배포·release 교체·timer 활성화·실제 카드사 호출은 수행하지 않았다. 다음 단계는 Reviewer 검토 후 기존 운영 절차에 따라 이미지/release를 식별해 반영하고, 사용자가 동일 run/volume/lock/복구점/저장공간을 확인한 뒤 resume하는 것이다.