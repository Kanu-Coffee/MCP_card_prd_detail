# 021 — OCR provider 호출·실패·폴백 로깅 개선

작성: 2026-10-28 KST, Planner Codex. 사용자는 현재 대화와 운영 관측을 바탕으로 본 계획 작성을 요청했다. 이번 산출물은 계획이며 구현·배포·추가 유료 OCR 실행 결과를 뜻하지 않는다.

## 1. 목표

OCR provider를 호출할 때 어느 run의 어떤 상품·문서·페이지에 어떤 provider/model/reasoning을 사용했는지, 그 실행에서 몇 번째 시도인지와 누적 건수가 얼마인지 운영 로그로 확인할 수 있게 한다. 실패 시 안전하게 분류한 원인과 폴백·재시도·취소의 연결을 남겨, 사후에 체크포인트 시간이나 OCR 파일을 역추적하지 않고도 troubleshooting할 수 있어야 한다.

상품코드·상품명은 기존 호출 문맥에서 쉽게 얻을 수 있는 범위에서 전달한다. 누락된 정보를 채우기 위한 원본 재조회, OCR 본문 파싱, 데이터 모델·DB schema·캐시 계약 개편은 하지 않는다. 알 수 없는 원인은 명시적으로 unknown으로 남기며 근거 없이 추정하지 않는다.

## 2. 조사한 현재 상태

### Git와 기존 변경

- HEAD: `06af791b6db3fa250b8b543deed46f77f3a4a7d7` (`docs: record v1.0.37 release and handoff closeout`).
- 020은 `../020_paddle-fallback-symlink-capacity-recovery/CLOSURE.md`로 종료됐다. 001–019의 종료 근거는 `../STATUS_v1.0.37.md`다. 오래된 `../README.md`의 활성 과제 표시는 현재 상태 기준이 아니다.
- 020의 `capacity_v5.py`, `cli.py`, `paddleocr_runner.py`, `pipeline.py`, `providers.py`, 테스트 변경과 새 `paddle_cleanup.py`가 미커밋 상태다. timer 및 운영 문서의 기존 사용자 변경도 있다. Executor는 시작 시 실제 diff를 다시 확인하고 이 변경을 보존한다. 021 diff와 검증을 기존 변경 위에서 구분해 보고한다.
- 운영 release는 `/opt/cardrag/020-paddle-20261028`, image는 `cardrag-worker:020-paddle-2bfed5c195e3`다. 기존 운영 이미지 위에 6개 Worker 파일을 overlay한 조합이며 저장소 전체와 동일하지 않다. 인수된 저장소 테스트는 1,970 passed, 운영 이미지 조합 검증은 1,969 passed 및 기존 버전 metadata 불일치 1 failed였다. 상세 근거는 020 보고서에 있다. 이 계획에서 기존 검증을 새로 수행했다고 주장하지 않는다.
- 저장소 및 확인한 상위 경로에 별도 AGENTS.md/GEMINI.md 파일은 없었다. 사용자가 제공한 global handoff 지침을 적용했다. Executor는 새 지침 파일 유무를 다시 확인한다.

### 실제 운영에서 드러난 문제

1. 하나카드 새 지정 실행에서 `primary OCR failed; starting fresh whole-document fallback`만 출력돼 해당 예외가 시간 초과, 사용량 제한, 문서 오류, 출력 검증 실패 중 무엇인지 확정하지 못했다. 이후 어제 회차를 명시 resume하여 745건 중 재사용 742건, provider 호출 문서 3건으로 정상 게시했다.
2. 2026-10-28 10:00 롯데 예약 run `007ba6966e6b4395b5564afb0c5614fc`은 OCR 725건 중 재사용 707건, provider 호출 문서 18건으로 13:25 KST에 성공했다. 최종 산출물과 체크포인트를 대조해야 17건은 Codex, TELLO NE 1건은 Paddle fallback 결과임을 식별할 수 있었다. 그 primary 실패의 세부 원인은 일반 경고에서 확인할 수 없었다.
3. 기존 `ocr_provider_called_count`는 `OCRResult.provider_called`를 문서별로 더한다. 실제 provider 호출 시도 수·청크 수·내부 HTTP 요청 수·과금 건수를 의미하지 않는다. `cache_reused`와 `provider_called`가 모두 참인 부분 체크포인트 재사용도 가능하다.

### 코드상 관측 지점

| 파일/경로 | 현재 동작과 개선 지점 |
|---|---|
| `apps/cardrag-worker/src/cardrag_worker/pipeline.py` / `process_document`, 내부 `recognize`, `_finite_stage` | `SourceRecord`의 issuer, product_code, product_name, source_id 및 run/document/PDF 정보가 이미 있다. 유한 stage attempt와 run/문서 집계의 문맥을 전달할 수 있다. |
| `apps/cardrag-worker/src/cardrag_worker/contracts.py` / `SourceRecord` | 상품코드·상품명은 기존 필드다. 출시일 등 별도 메타데이터 추가는 필요하지 않다. |
| `apps/cardrag-worker/src/cardrag_worker/ocr.py` / `OCRResolver.resolve` | 캐시·prior native·seed·체크포인트 조회 후 실제 `recognize` 또는 `recognize_document`를 호출한다. 청크와 sparse-page corrective 호출, 호출 뒤 검증을 관측할 수 있다. |
| 같은 파일 / `FailoverOCRResolver.resolve` | 일부 systemic 오류·문서/validation/timeout/HTTP 오류를 잡은 후 원인을 버리고 일반 RuntimeWarning을 낸다. fallback이 성공하면 primary 예외의 진단이 결과에서 사라진다. |
| `apps/cardrag-worker/src/cardrag_worker/providers.py` | Codex/OpenCode subprocess, OpenRouter HTTP, Paddle whole-document 경계. `ProviderSystemicError`에 reason_code, error_kind, retryable, exit_code, stdout/stderr 길이·SHA 및 classification_basis가 이미 있다. |
| 같은 파일 / `OpenRouterOCRProvider` | 기본 모델 요청에 모델 목록을 보낼 수 있고, 예외 시 fallback_model로 두 번째 HTTP 요청을 할 수 있다. resolver 호출 1회와 HTTP 요청 횟수·요청 모델을 구별해야 한다. 실제 upstream/model 식별 정보는 현재 반환 계약에 없다. |
| `apps/cardrag-worker/src/cardrag_worker/cli.py` / `_configure_worker_logging` | `cardrag_worker` 전용 INFO StreamHandler가 stderr에 시각/level/logger/message를 출력한다. stdout은 최종 CLI JSON에 사용한다. startup correlation_id는 현재 run 이전에 생성된다. |

현재 `DocumentOCRProvider`/`OCRProvider` protocol에 상품 문맥은 없으며 일부 테스트 fake에는 reasoning 속성이 없다. 전역 mutable current-product나 provider 객체의 공유 문맥에 의존하지 말고 작은 선택적 문맥 전달 또는 scoped context를 사용한다. 새 하위 모듈이 필요하면 `ocr_logging.py` 정도의 한정된 책임으로 둔다.

## 3. 범위와 불변 조건

- 대상: OCR의 호출 시작·반환·검증 실패·timeout·process/HTTP 오류·fallback·stage 재시도·취소와 run OCR 요약. Codex/Paddle을 우선 검증하되 기존 OpenCode/OpenRouter OCR 경로도 같은 상관 정보와 안전한 원인을 남긴다.
- 로그 개선을 위해 OCR 실행·재시도 budget·fallback 허용 조건·모델/프롬프트/reasoning·캐시 epoch/reuse key·provider 출력·상품 scope·round-robin cursor를 바꾸지 않는다.
- 020의 부모 소유 임시 alias, reap 완료 전 cleanup 금지, timeout 중 취소 보존, Worker lock과 DB 접근 계약을 유지한다. diagnostics를 남기기 위해 child를 더 오래 살리거나 종료 경로의 예외를 다른 오류로 바꾸지 않는다.
- 신규 DB/마이그레이션·원격 로그 저장소·새 서비스·의존성 설치·원문 stderr 보관 기능은 범위 밖이다. 기존 terminal JSON schema와 v5 집계의 의미는 유지한다. 로깅 보조 context/counter는 cache/manifest/seal/hash 입력에 포함시키지 않는다.
- 운영 배포·수동 실사·timer 변경·기존 데이터 정리·GitHub 릴리스는 본 구현의 필수 단계가 아니다. 수행하게 되면 구체적인 배포 범위와 실제 이미지/소스 차이를 별도 보고한다. 동작 검증을 위해 운영 문서를 대량 재OCR하지 않는다.

## 4. 로그 계약

### 4.1 출력 형식과 공통 문맥

기존 logger/handler를 사용해 기본 INFO에서 확인 가능한 한 줄 이벤트를 남긴다. 권장 형식은 메시지에 `OCR_EVENT `와 작은 JSON 객체를 직렬화하는 것이다. `extra` 속성만 추가하고 기존 formatter가 버리는 구현은 불충분하다. 동일 이벤트를 여러 handler에서 중복 출력하지 않는다.

필드 이름은 아래 의미를 유지하면서 Executor가 작은 조정을 할 수 있다. 실제 이름과 누락 규칙은 문서·테스트에 일치시킨다.

| 필드 | 의미/제약 |
|---|---|
| `schema_version`, `event`, `timestamp` | 로그 schema v1과 안정적인 event 이름. 시각은 기존 formatter 또는 UTC ISO-8601 중 해석이 명확한 방식으로 제공. |
| `run_id`, `invocation_id` | 영속 run 식별자와 **이번 프로세스의 해당 run 실행** 식별자. 동일 run resume를 구분한다. 기존 startup correlation_id를 쉽게 전달할 수 있으면 함께 사용하되 필수 구조 개편은 하지 않는다. |
| `issuer`, `product_code`, `product_name`, `source_id`, `document_id` | pipeline에서 이미 보유한 source 문맥. 상품명은 선택 필드로 bounded escaping과 credential 검사를 거친다. 없으면 null/생략하며 lookup하지 않는다. 직접 resolver/provider를 호출하는 테스트·진단에는 run/product 문맥이 없을 수 있다. |
| `pdf_sha256`, `pdf_size_bytes`, `page_count` | 이미 확보된 원본 식별/크기. raw 파일 경로나 URL 대신 사용한다. |
| `provider`, `model`, `reasoning_effort` | 이번에 호출하는 객체/요청의 실효 설정. Paddle/OpenRouter에서 reasoning이 없으면 null/not_applicable, fake에 속성이 없으면 unknown. primary 설정을 fallback에 복사하지 않는다. |
| `phase`, `call_kind`, `call_id` | primary/fallback, chunk/whole_document/corrective 등, 시작과 결과를 묶는 식별자. OpenRouter 내부 모델 전환은 resolver의 fallback과 구분. |
| `stage_attempt`, `chunk_index`, `page_start`, `page_end`, `input_page_count` | 현재 유한 재시도 횟수와 목표 청크/페이지. 가능한 경우 context 입력 범위를 목표 출력 범위와 구별한다. chunk는 기존 0-based 기준, page는 1-based 기준을 문서화한다. |
| `call_ordinal`, `provider_attempts_total`, `provider_documents_total` | 아래 정의의 호출 시도 순서/건수와 고유 호출 문서 수. cache hit나 progress completed와 혼동하지 않는다. |
| `elapsed_seconds`, `timeout_seconds`, `outcome` | 단조 시계로 계산한 경과 시간, 실제 적용 timeout, 결과. 없는 값은 만들어내지 않는다. |

문맥 전달은 `process_document`에 있는 기존 정보에서 시작해 resolver의 실제 호출까지 이어지게 한다. `ContextVar`를 사용한다면 token 복구를 finally에 두고 sibling task/후속 문서에 문맥이 새지 않도록 한다. 선택적 keyword context를 쓴다면 기존 fake·직접 호출을 깨뜨리지 않는 경계를 마련한다. 두 방식을 중복 도입하지 않는다. source 문맥 때문에 core domain이나 provider public protocol 전체를 크게 바꾸지 않는다.

### 4.2 순번과 건수 정의

1. **provider attempt**: resolver가 provider `recognize` 또는 `recognize_document`를 실제로 호출하려는 한 번의 시도. 첫 시도부터 `call_ordinal=1`; 청크별·corrective·재시도·fallback을 각각 센다. 로컬 spawn 실패도 시작된 시도의 실패로 센다. 이는 요청 성공/과금 건수의 보장이 아니다.
2. **provider documents**: 이번 invocation에서 한 번 이상 provider attempt를 시작한 서로 다른 document_id 수. 같은 문서의 여러 청크·fallback·재시도로 중복 증가하지 않는다.
3. **wire attempt**: provider 내부의 개별 HTTP 요청 또는 subprocess dispatch. 기존 경계에서 쉽게 관측할 수 있는 경우 `parent_call_id`와 별도 `wire_ordinal`로 연결한다. 특히 OpenRouter의 기본 요청→내부 fallback 두 요청을 1건으로 위장하지 않는다. 모든 provider에 불필요한 별도 구조를 강요하지 않으며 관측하지 못하는 내부 서비스 재시도/라우팅은 unknown이다.
4. invocation 시작에서 메모리 counter를 초기화한다. 같은 run의 resume도 새 invocation에서 1부터 시작한다. 과거 run 전체 누적값을 계산하려고 DB schema나 기존 stage 상태를 바꾸지 않는다. invocation_id와 함께 조회·집계한다. 동시 task가 있어도 순번이 중복되지 않아야 한다.
5. 기존 `ocr_cache_reused_count`, `ocr_provider_called_count`는 그대로 둔다. 새 summary는 이름을 분리한다. 실패했던 primary 뒤 cached fallback이 성공하는 등 기존 최종-result 집계와 live attempt 수가 다를 수 있음을 문서화한다. 두 수치를 무조건 동일하게 맞추지 않는다.
6. 캐시 hit·체크포인트 재사용은 provider 시작 이벤트/attempt 증가를 만들지 않는다. 순회 순서 N번째 문서나 `completed=650`을 OCR 호출회차로 표현하지 않는다. 필요하면 `document_ordinal`과 `ocr_documents_total`을 별도 표시한다.

### 4.3 이벤트 흐름

- `ocr_provider_call_started` INFO: 실제 provider method 호출 직전에 한 번. 위 공통 문맥을 담는다. 모델 prefetch, provider discovery, startup 인증/임베딩 probe는 문서 OCR 호출로 세지 않는다.
- `ocr_provider_call_returned` INFO / `ocr_provider_call_failed` WARNING 또는 ERROR / `ocr_provider_call_cancelled` INFO: call_id로 결과 연결. 반환 성공은 **provider 응답이 반환됐다는 뜻**이며 OCR 검증·캐시 publication·게시 성공을 보장하지 않는다. 시작된 시도는 정상 종료·오류·관측 가능한 취소 중 하나의 결과를 남긴다. SIGKILL/전원 차단에는 종료 이벤트를 보장할 수 없다.
- `ocr_output_validation_failed` WARNING: 호출 반환 뒤 target page marker·blank/sparse·페이지 수·출력 계약 등의 검증이 실패하면 **원인과 call_id, 페이지**를 남긴다. corrective 호출 및 fallback/terminal 오류와 연결한다. post-provider cache publication/storage 오류를 provider 자체의 실패라고 기록하지 않는다.
- `ocr_fallback_started` WARNING: primary의 안전한 failure 요약과 primary/fallback provider/model 및 마지막 관련 call_id를 남긴다. `FailoverOCRResolver`가 선택한 전체 문서 fallback과 OpenRouter 내부 모델 retry를 분리한다. fallback이 cache로 해결되면 실제 provider 호출이 없다는 결과를 남긴다.
- `ocr_document_resolved` INFO: source 식별과 최종 provider/model, 가능한 provenance/cache 상태, 이번 invocation의 해당 문서 호출 여부·횟수, 결과 크기/페이지 수. 대규모 cache-only 문서는 매건 INFO를 강제하지 말고 호출한 문서의 완료 결과 및 요약 중심으로 제한한다. 출처를 확정할 수 없는 재사용 결과의 provider를 현재 설정으로 채우지 않는다.
- `ocr_stage_retry` WARNING: 실제 retry 결정 시 attempt/한도와 안전한 이유. 기존 stage retry 경계에서 쉽게 연결하고 새 재시도를 만들지 않는다.
- `ocr_invocation_summary` INFO: 호출 시도·반환·실패·취소·고유 호출 문서 수, fallback·corrective 수, 가능한 cache/checkpoint 재사용 집계를 남긴다. 정상 OCR stage 종료뿐 아니라 실패/정상 cancellation drain에서 가능한 요약과 partial 표시를 제공한다. CLI 최종 JSON 출력은 유지한다. 이미 있는 pipeline 종료 경계와 일관되게 구현하고 강제 종료 때 summary를 보장하지 않는다.

전체 캐시 조회 하나하나를 INFO로 남기지 않는다. 기존 `OCR progress`는 유지하며, 새 호출 로그는 실제 외부 호출 시도당 bounded O(1) 이벤트와 문서/실행 요약으로 제한한다. 로그를 생성하려고 PDF/이미지/응답을 추가로 읽거나 해시하지 않는다. 오류 시 이미 수집된 stdout/stderr 길이·SHA는 재사용할 수 있다.

### 4.4 provider/model 정보의 정확성

- Codex/OpenCode는 실제 프로세스에 넘긴 model/reasoning과 연결한다. 단순히 환경변수 전체를 덤프하지 않는다.
- Paddle은 `local-paddleocr`, 실제 `model`, `reasoning_effort=null`과 whole-document page_count, timeout을 기록한다.
- OpenRouter는 실제 요청한 model과 내부 fallback 요청 model, 요청한 routing model 목록이 있으면 bounded 설정값을 기록할 수 있다. 현재 provider 반환형이 문자열이므로 **요청 모델을 실제 처리 모델이라고 단정하지 않는다**. `requested_model`/`model_source=configured` 등 의미를 명확히 한다. 실제 응답 model을 안전하게 쉽게 얻을 수 있으면 선택적으로 기록하되 반환 계약 개편을 강제하지 않는다. 물리적인 upstream provider는 알 수 없으면 unknown이다.
- provider adapter 이름과 실제 backend의 의미를 혼동하지 않는다. 임베딩 provider/model은 OCR 이벤트에 섞지 않는다.

## 5. 실패 원인과 진단 보존

### 안전한 분류

- `ProviderSystemicError`의 기존 reason_code, 고정된 reason, error_kind, retryable, classification_basis 및 유효한 exit_code/status_code를 재사용한다. 중복 classifier를 만들어 같은 예외가 로그와 terminal report에서 다르게 분류되지 않도록 한다. 순환 import는 피한다.
- timeout은 provider 대기 timeout과 HTTP connect/read timeout 등 현재 타입에서 확정할 수 있는 범위로 구분하고 실제 timeout_seconds/elapsed를 남긴다. 취소는 timeout과 구분한다.
- validation은 최소 page marker/count mismatch, empty/too-short page, sparse wrapper invalid, provider response contract invalid를 구분할 수 있게 한다. 기존 validation 지점의 고정된 작은 reason_code 또는 allowlist 세부 코드로 연결하며 임의 예외 문자열 매칭에 기대지 않는다. 예외 hierarchy/생성자 전체 개편이 부담되면 optional code 또는 검증 경계에서의 분류로 제한한다.
- HTTP 오류는 status와 안전한 원인 분류만 기록한다. subprocess 실패는 exit_code 및 이미 존재하는 stdout/stderr 길이·SHA, allowlist 분류 근거를 기록한다. Paddle의 문서 거부 exit 65나 음수 signal code를 관측했을 때 기존 정책에 맞는 이유를 남길 수 있지만 메모리 부족·OS kill 원인을 근거 없이 단정하지 않는다.
- known network/authentication/quota/configuration 오류와 unknown exit를 구별한다. `unknown`이면 unknown임을 보존한다. SDK가 안전하게 제공하는 request ID/Retry-After 같은 선택 필드는 획득이 쉽고 엄격히 제한할 수 있을 때만 추가하며 전체 header/body를 기록하지 않는다.

### fallback 때 사라지는 예외

`FailoverOCRResolver`의 except 안에서 **허용된 스칼라 값으로 이루어진 진단 snapshot만** 만든다. except 밖에서 fallback 이벤트를 출력하고 실행해, 기존의 예외 context/credential 비노출 의도를 유지한다. exception 객체·traceback·응답 body를 보존하지 않는다. primary가 validation에서 실패했는지 provider에서 실패했는지, corrective 이후인지, 관련 attempt/page를 끝까지 연결한다.

기존 RuntimeWarning은 structured event로 대체하거나 호환성을 위해 유지할 수 있다. 유지할 경우 문맥 없는 경고 한 줄만 남는 상황은 없어야 하며 Python warning의 동일 위치 중복 억제와 무관하게 **각 fallback 전환마다** logger 이벤트를 남긴다. 기존 pytest.warns 기대를 바꾸면 해당 의미와 테스트를 함께 갱신한다.

### 비밀·출력 안전성과 오류 보존

- 원문 exception `str/repr`, traceback/`exc_info`, provider stdout/stderr, OCR 본문, 이미지/base64, 프롬프트, 전체 argv/env/config, 원본 URL·query·auth/header·쿠키를 출력하지 않는다. 잘라낸 stderr도 원문 수집이다. 단순 redaction을 원문 출력 허용 근거로 사용하지 않는다.
- 문자열은 bounded allowlist/escape를 적용한다. product_code/name, model 등 외부 입력에도 개행·탭·ANSI·credential sentinel이 들어올 수 있다. 안전하지 않은 선택 필드는 생략/마스킹하고 식별자와 누락 이유만 남긴다. 한 줄 최대 크기(예: 8 KiB), 상품명 길이(예: 160자), 코드 길이(예: 256자)와 필드 제한을 고정하고 테스트한다.
- logger serialization/diagnostic 부가정보 오류가 원래 OCR 예외·취소·결과를 가리지 않도록 한다. 기존 RuntimeWarning을 logger로 바꾸더라도 예외 및 retry/fallback 정책은 동일해야 한다. 취소를 잡아 로그만 남기고 삼키지 않는다.
- 기존 journal/docker stderr에 남기므로 별도 event 파일을 state 안에 추가하지 않는다. journal/container 로그 보존 정책 때문에 과거 기록이 없을 수 있음을 운영 문서에 명시한다. 이 개선으로 이미 소실된 과거 실패 원인을 복원할 수는 없다.

## 6. 권장 구현 순서와 관련 파일

1. 시작 Git diff, 020 변경, source/실행 이미지 차이와 현재 테스트 기반을 확인한다. fake provider와 직접 resolver 호출을 포함해 선택한 context/counter 설계를 확정한다.
2. bounded log event builder와 invocation counter를 작은 모듈 또는 기존 코드에 추가한다. pipeline의 기존 source/run 문맥과 stage_attempt 전달을 연결한다. 로그 외 identity에 영향이 없는지 먼저 확인한다.
3. resolver의 chunk/whole-document/corrective 실제 호출 경계, 반환 후 validation 경계, fallback 결정에 관측을 추가한다. 같은 call_id에 start/result를 연결하고 cache/checkpoint hit에서 호출 로그가 나오지 않는지 검증한다.
4. provider 내부에서만 얻을 수 있는 기존 안전 진단과 OpenRouter 내부 요청 정보를 연결한다. Codex/Paddle/OpenCode의 종료·timeout·spawn 실패 경로를 확인하며 020 reap/alias/취소 동작을 보존한다. 모델 prefetch는 문서 OCR과 구별한다.
5. retry·문서 완료·invocation summary를 연결하고 실제 INFO handler에서 JSON 필드가 보이는지 확인한다. 로그의 중복/양/한 줄 길이와 동시 문맥 누출을 확인한다.
6. `docs/OPERATIONS.md`에 필드·순번·집계·null/unknown 의미, 정상/validation→fallback/timeout 예시와 조회법을 추가한다. 기존 사용자 문서 변경은 보존한다. `docs/WORKER_RUNTIME_OPTIONS.md`는 설명 보충이 실제로 필요할 때만 수정하고 새 필수 옵션은 만들지 않는다.

관련 기존 테스트: `tests/test_ocr.py`, `test_ocr_prefetch.py`, `test_ocr_recovery.py`, `test_fix_22_ocr_identity_resume.py`, `test_fix_24_provider_diagnostics.py`, `test_paddle_inputs_v130.py`, `test_cli_settings_provider.py`, `test_pipeline.py`, `tests/runtime_v1/`. 새 전용 테스트는 예를 들어 `apps/cardrag-worker/tests/test_ocr_provider_logging.py`에 둔다. runtime fixture나 테스트만을 위한 production 경로를 추가하지 않는다.

## 7. 검증 및 인수 기준

### 필수 동작·회귀 검증

1. pipeline fixture의 상품코드·상품명/run/document가 실제 `OCR_EVENT`에 보인다. 문맥이 없는 직접 호출도 동작한다. 병렬 문서 A/B와 다음 문서에 문맥이 섞이지 않는다. 모델과 reasoning 속성이 없는 fake도 안전하다.
2. cache-only와 완료 체크포인트 재사용은 provider start 0, attempt 증가 0. 부분 checkpoint 재사용은 빠진 청크만 시도한다. 여러 문서·청크·corrective·유한 retry·fallback에서 고유 문서 수와 순번을 검증한다. 같은 run을 resume한 별도 invocation의 순번 reset이 명시된다.
3. provider 반환 성공 후 validation 실패 fixture에서 returned→validation_failed→fallback 또는 terminal 흐름과 페이지·call_id가 보인다. 같은 문서에 fallback이 여러 번 발생해도 logger가 누락하지 않는다. 성공적으로 검증된 OCR과 단순 응답 반환을 구별한다.
4. Codex/OpenCode auth/quota/network/config/unknown exit, spawn 실패, HTTP 429/5xx, validation 오류, timeout의 안전한 분류를 확인한다. known classification이 있으면 terminal 진단과 일치한다. 없는 정보는 추정하지 않는다.
5. Paddle fallback 성공, exit 65, timeout, 최초 취소 및 timeout→reap 중 추가 취소를 검사한다. alias 수명·child reap·CancelledError 전파는 020 기대와 동일하며 stderr 원문은 로그/traceback에 없다. startup prefetch는 provider document 집계에서 제외한다.
6. OpenRouter 첫 요청 실패→내부 fallback 성공/실패 fixture에서 실제 두 요청 모델과 parent_call_id를 연결한다. 외부 upstream/실제 선택 모델을 확인하지 못하면 requested/unknown으로 표현한다.
7. 비밀 sentinel을 raw stdout/stderr, HTTP URL/header/body, exception text, 상품명·모델/코드, 개행/ANSI에 주입한다. caplog뿐 아니라 실제 설정한 stderr formatter 출력·최종 stdout JSON·기존 report를 확인해 비노출과 한 줄/길이 제한을 검증한다. 직렬화 실패에서도 원래 예외·반환·취소가 유지된다.
8. 성공·실패·SIGTERM drain의 summary가 partial 여부와 실제 관측 수치를 제공한다. SIGKILL 후 미완료 start를 성공으로 보정하지 않는다. OCR 결과 bytes/hash, cache/manifest/seal identity, 기존 stage retry 및 rotation 계약이 변경되지 않는다.

### 명령

구현 후 변경 범위에 맞게 실행하고 실제 명령과 결과를 REPORT에 적는다. 외부 API·실제 Paddle 모델 다운로드 없이 fake subprocess/HTTP 및 기존 signal fixture를 사용한다.

```bash
uv run --no-sync pytest apps/cardrag-worker/tests/test_ocr_provider_logging.py apps/cardrag-worker/tests/test_fix_24_provider_diagnostics.py apps/cardrag-worker/tests/test_paddle_inputs_v130.py apps/cardrag-worker/tests/test_cli_settings_provider.py -q
uv run --no-sync pytest apps/cardrag-worker/tests/ packages/cardrag-core/tests/ tests/runtime_v1/ -q
uv run --no-sync ruff check apps/cardrag-worker/ packages/cardrag-core/
uv run --no-sync ruff format --check <변경한 Python 파일>
uv run --no-sync mypy <변경한 production Python 파일>
git diff --check
```

새 테스트 경로는 Executor가 실제 선택한 이름으로 바꾼다. 문서 계획 작성 자체에는 코드 테스트를 수행하지 않았다. 운영 실사는 mock 검증과 구분해 이미지 ID·run·원본·로그·비용/외부호출 여부를 기록하고, 배포 전까지 운영 로깅 개선이 적용됐다고 주장하지 않는다.

## 8. Executor 보고와 Reviewer 인계

Executor는 본 계획과 현재 Git 상태를 읽고 구현·검증 후 이 디렉터리에 새 `REPORT.md`를 작성한다. 시작/종료 Git 상태, 변경 파일, 최종 이벤트 schema/예시, 순번·건수·resume 규칙, 실제 provider/요청 model의 한계, 노출 방지와 취소·fallback 회귀 검사, 명령/결과, 계획 대비 조정·남은 위험을 기록한다. 관측할 수 없는 선택 정보는 이유와 함께 명시한다. 기존 020 및 본 PLAN은 덮어쓰지 않는다.

Reviewer는 실제 diff와 실행 증거를 대조해 로그로 문서→시도→오류→fallback→최종 결과를 추적할 수 있는지, 호출/문서/HTTP 건수 혼동이 없는지, 원문 노출과 실행 정책 변경이 없는지 확인한다. 수정이 필요하면 다음 `FIX_01.md`부터 지시하고 Executor는 대응 보고서를 추가한다. 운영 배포·릴리스는 코드 인수와 구분한다.
