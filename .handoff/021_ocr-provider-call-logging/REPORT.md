# 021 Executor 구현 보고

2026-10-28 KST. `PLAN.md`를 읽고 로컬 작업 트리에 OCR provider 진단 이벤트를 구현했다. 운영 배포나 실제 외부 OCR 호출은 수행하지 않았다. Reviewer 검토 대기다.

## 시작 상태와 보존

- 시작 HEAD `06af791b6db3fa250b8b543deed46f77f3a4a7d7`. 020의 Worker 구현 및 테스트, 새 `paddle_cleanup.py`, 운영 문서/timer 사용자 변경과 020 handoff가 미커밋 상태였다. 이를 보존하고 같은 작업 트리 위에 021 변경을 추가했다.
- 021 변경: 신규 `apps/cardrag-worker/src/cardrag_worker/ocr_logging.py`, `apps/cardrag-worker/tests/test_ocr_provider_logging.py`; 수정 `ocr.py`, `pipeline.py`, `providers.py`, `docs/OPERATIONS.md`. `pipeline.py`, `providers.py`, `docs/OPERATIONS.md`에는 시작 전 변경도 있으므로 HEAD 대비 전체 diff가 021만의 diff는 아니다. 020 provider의 alias·reap 코드는 유지했다.
- 운영 이미지는 `/opt/cardrag/020-paddle-20261028`의 기존 overlay 이미지이며 이번 소스 변경은 그 이미지에 적용하지 않았다. 신규 파일을 포함한 image build, 운영 배포, Git commit/PR/릴리스는 수행하지 않았다.

## 구현

- 기존 stderr INFO logger에 `OCR_EVENT`와 한 줄 `cardrag.ocr-event.v1` JSON을 출력한다. `run_id`, 프로세스 실행별 `invocation_id`, 문서/issuer/상품코드/가능한 상품명/source/PDF 식별, stage attempt, provider/model/reasoning, phase, chunk/페이지, timeout과 경과 시간을 연결한다. 스코프는 ContextVar로 task별 분리하고 counter는 invocation별로 초기화한다. model/reasoning 등의 속성이 없는 fake에도 적용 가능하다.
- 실제 resolver의 chunk·corrective·whole-document 호출에서 start/returned/failed/cancelled를 call_id로 연결한다. 반환과 출력 validation을 구별하며, marker·빈 페이지·짧은 페이지·sparse/blank 오류는 고정 reason code를 남긴다. 기존 검증 예외 메시지와 OCR 결과는 유지한다.
- fallback에서 exception 객체나 원문을 보관하지 않고 안전한 reason snapshot을 남긴다. primary 호출 결과 및 fallback provider/model과 연결하고, fallback이 provider를 호출했는지와 최종 산출물의 계약 정보를 별도 이벤트로 남긴다. 기존 RuntimeWarning과 retry/fallback 조건은 유지한다.
- Codex/OpenCode의 기존 구조화된 process 진단과 HTTP status/timeout, Paddle document rejection exit 65를 기록한다. OpenRouter 내부의 첫 요청과 fallback 요청은 `ocr_wire_*`와 parent_call_id, wire_ordinal, requested_model로 구분한다. 실제 upstream 모델은 반환 계약에서 확인할 수 없어 `actual_model=null`이며 결과 이벤트의 `model_source=artifact_contract`도 실제 upstream 선택을 증명하지 않는다.
- provider method 호출은 chunk/corrective/fallback마다 순번을 부여하고, 고유 호출 문서 수는 중복 없이 센다. cache hit/checkpoint 재사용은 호출을 만들지 않는다. 같은 run resume는 새 invocation으로 1부터 다시 센다. `ocr_invocation_summary`는 반환·실패·취소·호출 문서·fallback·corrective 건수와 partial을 남긴다. 기존 `ocr_provider_called_count`와 JSON 출력은 변경하지 않았다.
- 비밀/원문 보호를 위해 허용된 문자와 길이 및 기존 credential 형식 검사를 적용하고, 안전하지 않은 선택 필드는 null과 `unsafe_or_too_long`을 기록한다. exception text, OCR 응답, stderr/stdout 원문, URL, prompt, credential은 이벤트에 넣지 않는다. 예외 분류가 불가능하면 `unknown`이며 logger 오류는 삼켜 원래 결과/예외를 보존한다. 별도 state 파일이나 DB migration은 없다.
- stage attempt는 기존 `stage_started`/완료 stage 조회 결과를 재사용한다. 최초 전체 회귀에서 로그용 추가 `get_stage`가 오류 처리 순서를 변경하는 실패 1건을 발견했고, 추가 조회를 제거한 후 해당 테스트와 전체 회귀가 통과했다.

## 검증

- 전체 관련 회귀 `uv run --no-sync pytest apps/cardrag-worker/tests/ packages/cardrag-core/tests/ tests/runtime_v1/ -q`: **1,986 passed, 11 warnings in 66.11s**. 이 검사는 마지막 `model_source=artifact_contract` 표시 보완 직전에 수행했다. 11 warnings는 기존 손상 OCR/원격 캐시 충돌 및 테스트 fallback 경고다.
- 표시 보완 후 최종 관련 회귀 `uv run --no-sync pytest apps/cardrag-worker/tests/test_ocr_provider_logging.py apps/cardrag-worker/tests/test_ocr.py apps/cardrag-worker/tests/test_fix_24_provider_diagnostics.py apps/cardrag-worker/tests/test_paddle_inputs_v130.py apps/cardrag-worker/tests/test_cli_settings_provider.py apps/cardrag-worker/tests/test_pipeline.py -q`: **440 passed, 10 warnings in 8.56s**.
- 전용 테스트는 pipeline 상품 문맥, 실행별 재개 순번, 병렬 task 격리, validation→fallback 연결, 부분 checkpoint 재사용, OpenRouter 내부 두 요청, 정상/실패/취소 집계, logger 실패 시 본 동작 유지, 실제 stderr formatter와 GitHub token 형태·개행 차단, 대표 오류 분류를 검사한다. 기존 Paddle timeout/reap/cancellation 및 provider 진단 테스트도 통과했다.
- `uv run --no-sync ruff check apps/cardrag-worker/ packages/cardrag-core/`, 변경 Python 5개 파일의 `ruff format --check`, 변경 production 4개 파일의 `mypy`, `git diff --check`: 모두 통과했다.

## 제한과 Reviewer 확인 사항

- 시도 수는 provider method 호출 수다. 내부 SDK의 숨은 retry, 과금 횟수 또는 OpenRouter 실제 선택 모델의 완전한 계측은 제공하지 않는다. cache-only 문서의 개별 INFO 로그와 cache/checkpoint 총계는 추가하지 않았다. summary는 정상 종료 및 포착 가능한 취소/오류에서 기록하며 SIGKILL/전원 차단에는 결과·summary가 없을 수 있다.
- 신규 취소 summary는 `Pipeline.run` cancellation fixture로 확인했다. 실제 systemd SIGTERM에서 신규 이벤트의 종단 간 출력, 실제 Paddle 모델 및 외부 Codex/OpenRouter 호출은 수행하지 않았으며 020의 기존 signal/Paddle 테스트 통과와 구분한다.
- 안전하지 않거나 너무 긴 상품명·상품코드·모델은 누락 이유를 남기고 null로 표시한다. 이름을 추가 획득하기 위한 origin 조회·schema 변경은 없다. 실제 provider가 정확한 사유를 제공하지 않는 경우 여전히 `unknown`이다. 과거에 저장되지 않은 primary 오류 원인은 복원되지 않는다.
- 실행 종료·게시·OCR 계약/캐시·순회 변경 없이 로깅만을 목적으로 했지만, 예외에 안전한 diagnostic code 및 Paddle document exit 정보를 추가했다. Reviewer는 기존 예외/취소 및 cache 계약, 다중 청크/폴백/내부 재시도 카운터, OpenRouter 모델 표현과 로그 비밀 경계를 중점 확인한다.
