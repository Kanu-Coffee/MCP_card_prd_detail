# 021 FIX_02 — 전체 문서 검증 오류를 마지막 청크에 잘못 연결하지 않기

2026-10-28 KST Reviewer Codex. PLAN, REPORT, FIX_01, FIX_01_REPORT, 현재 Git diff/status와 실제 재현 결과를 검토했다. **FIX_01의 최종 검증 로그 누락은 해결됐지만, 아래 P2를 수정한 후 인수한다.** 기존 문서·코드·운영 상태는 보존했다.

## P2: `final_chunk_combine` 이벤트의 호출 ID·페이지 범위가 오류 위치를 잘못 표시한다

위치: `apps/cardrag-worker/src/cardrag_worker/ocr_logging.py:314`의 `validation_failed`와 `ocr.py:3044`의 최종 청크 결합 검증 호출.

`validation_failed`는 검증 단계와 무관하게 `doc.last_call`을 전체 복사한다. 여러 청크를 합친 문서 전체를 검사할 때에도 마지막 호출의 `call_id`, `chunk_index`, `page_start`, `page_end`가 실패 이벤트의 문맥으로 나온다. 따라서 첫 청크가 잘못되고 마지막 청크는 정상이어도 오류 로그가 마지막 청크를 가리킨다. 이는 실패 지점·호출을 추적하려는 021의 목적에 맞지 않는다.

### Reviewer 재현

임시 WorkerState, 외부 호출 없는 fake chunk provider, 2페이지 PDF의 fake renderer로 확인했다. `whole_document_max_pages=1`, `chunk_pages=1`로 두 번 호출하며 **1페이지 본문에만 NUL**을 넣고 2페이지는 정상 응답을 반환한다. 마지막 전체 검증이 `OCRVerificationError`로 실패한다.

```text
started A: chunk_index=0, page_start=1, page_end=1  (NUL 포함)
started B: chunk_index=1, page_start=2, page_end=2  (정상)
validation_failed:
  reason_code=final_ocr_verification_failed
  validation_phase=final_chunk_combine
  call_id=B, chunk_index=1, page_start=2, page_end=2
```

최종 전체 검증은 실패한 개별 청크를 특정하지 않았는데 정상인 B 호출을 검증 실패의 단일 호출로 표시한다. FIX_01의 새 테스트는 모든 청크에 NUL을 넣고 reason/validation_phase만 검사하므로 이 오인을 잡지 못한다.

## 필요한 수정

1. 개별 응답 검증과 문서 전체 최종 검증의 문맥을 구분한다. `final_chunk_combine`에는 문서 전체 범위 또는 `validation_scope=document`를 명시하고 마지막 청크의 범위를 실패 범위로 복사하지 않는다.
2. 실제로 원인 호출/페이지를 특정할 수 없다면 `call_id`/`chunk_index`는 null 또는 생략한다. 직전 호출을 참고 정보로 남기고 싶으면 `last_call_id` 등 **원인 호출을 뜻하지 않는 별도 필드**로 명시한다. 모든 청크의 본문/해시를 새로 조사해서 원인 위치를 억지로 알아낼 필요는 없다.
3. whole-document의 최종 검증은 실제 단일 호출과 연결할 수 있다. 단일 응답 검증·corrective·fallback의 기존 call_id 연결을 유지한다. checkpoint-only 최종 검증에도 새로운 provider attempt를 만들거나 이전 시도의 호출을 이번 검증의 원인으로 표시하지 않는다.
4. exception type, 유한 retry/fallback 정책, cache/manifest/결과 bytes, 순번·집계와 020 alias/reap/cancellation은 그대로 유지한다. 추가 DB 조회나 provider 호출은 필요하지 않다.

## 검증과 보고

- 첫 청크만 NUL이고 마지막 청크는 정상인 재현을 회귀 테스트로 추가한다. 오류 이벤트가 문서 전체 검증임을 보여주고, 정상인 마지막 호출/페이지를 원인으로 단정하지 않는지 검사한다.
- 같은 문서의 checkpoint-only 또는 부분 재사용 경계에서도 호출 집계와 검증 문맥이 정확한지 확인한다. 원인을 알 수 없으면 unknown/null을 허용한다.
- 기존 whole-document 오류, chunk 단위 validation, corrective/fallback, 민감정보 비노출과 logger failure/cancellation 테스트를 유지한다.
- 보완 후 관련 회귀·정적 검사를 실행하고 새 `FIX_02_REPORT.md`에 결과를 기록한다. 기존 PLAN/REPORT/FIX 문서는 덮어쓰지 않는다.

## Reviewer 확인 결과

- FIX_01의 whole-document 및 chunk 최종 validation 이벤트와 고정 reason code, 원래 예외 재전파를 확인했다. 현재 테스트 `test_ocr_provider_logging.py` 및 provider/Paddle/CLI 관련 검사는 **187 passed, 1 warning in 4.60s**다.
- Ruff, 변경 production 4개 파일의 mypy, `git diff --check`는 통과했다.
- 현재 환경에서는 `gitleaks`가 `/home/lee/.local/bin/gitleaks`에 있고, FIX_01_REPORT가 실패로 기록한 `test_repository_gitleaks_policy_rejects_openrouter_and_github_tokens`는 Reviewer 재실행에서 **1 passed in 2.56s**다. 과거 Executor 환경의 바이너리 유무까지 소급 확인한 것은 아니다. 최종 correction 보고서는 당시 실제 환경과 결과를 기록하면 된다.
- Reviewer는 운영 배포·실제 외부 OCR·데이터 변경을 수행하지 않았다. 이번 문서는 로그의 귀속 오류를 수정하도록 요청하며 운영 복구나 릴리스 판단을 변경하지 않는다.
