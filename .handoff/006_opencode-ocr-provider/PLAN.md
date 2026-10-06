# 006 opencode OCR 호출자 신설 및 무중단 단계 도입

작성 역할: Planner. 대상 저장소: `/home/lee/projects/MCP_card_prd_detail`, 기준 브랜치 `main`(HEAD `2bcd514`, v1.0.32 게시 완료 상태). 작업 시작 시 `git status`/`git log`로 기준이 바뀌었는지 먼저 확인한다.

## 목적

Worker의 OCR 호출자에 **`opencode` 경로**를 추가한다. 기본 모델은 opencode에 이미 등록된 `alibaba-token-plan/qwen3.8-flash`, reasoning effort `medium`이다. 기존 호출자(`codex-exec`, `openrouter`, `local-paddleocr`)와 동일하게 **env로 선택**하고 **OCR 캐시 계약(contract)·재사용·게시 규칙을 그대로 따른다**. 새 호출자는 샘플 검증 → 운영 반영 → 수일 관찰 순으로 도입하며, 그동안 현재 stable 운영(timer, MCP, 기존 provider, OCR 캐시, stable 포인터)은 **무중단·무변경**이어야 한다.

## 현재 사실 (Planner가 확인)

- 호출자 구현: [providers.py](../../apps/cardrag-worker/src/cardrag_worker/providers.py)의 `OpenRouterOCRProvider`, `CodexOCRProvider`(`provider="codex-exec"`), `PaddleOCRVLProvider`(`provider="local-paddleocr"`, 문서 단위 `recognize_document`), 팩토리 `make_ocr_provider`(≈L986). 페이지 이미지 기반 호출자는 `recognize(images, *, page_numbers, target_page_numbers, total_pages, prompt) -> str`를 구현하고 `_ocr_call_instructions`로 지시문을 붙이며, 결과에 `reject_credential_bearing_ocr`를 적용하고, 실패는 `ProviderSystemicError(<allowlisted reason code>)`로 올린다. stderr 원문은 보존하지 않고 크기·sha256만 남긴다.
- 선택/설정: [settings.py](../../apps/cardrag-worker/src/cardrag_worker/settings.py) `CARDRAG_OCR_PROVIDER`(기본 `local-paddleocr`), `CARDRAG_OCR_FALLBACK_PROVIDER/MODEL`, `CARDRAG_OCR_MODEL`, `CARDRAG_OCR_REASONING_EFFORT`(기본 `high`), `CARDRAG_OCR_PROVIDER_TIMEOUT_SECONDS`, `CARDRAG_EXTERNAL_OCR_ALLOWED`(L338: `codex-exec`/`openrouter`는 이 값이 true여야 허용). 배선은 [cli.py](../../apps/cardrag-worker/src/cardrag_worker/cli.py) `_provider()`(≈L216)와 L410–L470의 primary/fallback `OCRResolver` + `FailoverOCRResolver`.
- 캐시 계약: [ocr.py (cardrag-core)](../../packages/cardrag-core/src/cardrag_core/ocr.py) `NativeOCRContract`는 `provider`, `model`, `reasoning_effort`, 프롬프트 해시, 렌더 설정 등을 포함한다. 즉 새 provider/model/effort 조합은 **자동으로 별도 contract**이며 기존 캐시를 덮어쓰거나 오염시키지 않는다. 호환 계약 탐색(`discover_compatible_contracts`, `compatible_ocr_models`)과 `CARDRAG_OCR_CACHE_MODE`(read-only/read-write), `..._REQUIRE_HIT`, `..._EPOCH`, `..._PUBLICATION_APPROVED` 규칙은 provider 중립이어야 한다.
- 컨테이너: Worker는 read-only rootfs, `cap_drop: ALL`, UID 10001 컨테이너에서 timer(`deploy/worker/cardrag-worker.timer/.service`, `/etc/cardrag/worker.env`)로 one-shot 실행된다. `codex`는 이미지 내부 실행 파일 + `codex-home` 볼륨을 쓴다. opencode는 **현재 이미지에 없다.** 호스트에는 `~/.npm-global/bin/opencode` v1.18.34, 설정 `~/.config/opencode/opencode.jsonc`, 자격 `~/.local/share/opencode/auth.json`이 있다(운영 서버 `/opt/cardrag`와는 별개 환경일 수 있음 — 실행 위치를 먼저 확인).
- 사용자 확인: **antigravity 호출자는 현재 존재하지 않는다**(기존 호출자는 codex-exec, openrouter, local-paddleocr). `CARDRAG_CODEX_MODEL_PROVIDER*`는 codex-exec의 커스텀 provider 설정일 뿐이며 변경하지 않는다. antigravity/`agy -p` 경로는 이 과제 범위에서 제외한다.

### opencode 사전 실험 결과 (Planner가 직접 수행, 호스트)

```
opencode run "<지시문>" -m alibaba-token-plan/qwen3.8-flash --variant medium --format json -f p.png
```

- 이미지 첨부 비전 입력 정상 동작(“CARD TEST 12345” 정확 전사), 약 8.5초.
- `--format json`은 JSON 이벤트 스트림을 낸다: `step_start` → `text`(`part.text`에 응답) → `step_finish`(`part.tokens`: total/input/output/reasoning, `part.cost`). 최종 텍스트는 `type=="text"` 파트들을 순서대로 이어 붙여 얻는다.
- **함정 1:** `-f/--file`은 배열 옵션이라 뒤따르는 위치 인자(메시지)를 파일로 먹는다(`File not found: <message>`). 메시지를 **먼저** 두고 옵션을 뒤에 둘 것. 대용량 프롬프트는 argv 한계/노출을 피하려면 stdin(`echo … | opencode run … -f …`)도 동작함을 확인했다(기본 포맷에서 확인) — `--format json`+stdin 조합은 Executor가 검증해 하나로 확정한다.
- **함정 2:** 이미지 1장·짧은 지시문인데 입력 토큰이 약 15.7k다. 기본 agent의 시스템 프롬프트·도구 정의 오버헤드다. OCR 전용으로 **도구 전부 비활성화한 전용 agent**(`--agent`)와 `--pure`(외부 plugin 차단) 사용으로 줄이고 보안도 확보한다. 실제 절감 폭을 측정해 기록한다.
- 호스트 `opencode-web`(port 4096 systemd user service)가 떠 있다. `--attach`로 공유하지 말고, Worker 호출은 독립 프로세스(기본 로컬 서버 임의 포트)로 한다.

## 범위

포함
1. `opencode` OCR provider 구현(페이지 이미지 단위 `recognize`), 설정/env, `_provider()`·fallback 기본 모델 배선, 팩토리 확장, 외부 OCR 허용 게이트 포함.
2. 보안 격리(도구 비활성 agent, 최소 환경 변수, 임시/읽기 전용 작업 디렉터리, 설정/자격 최소 노출).
3. 캐시 계약·재사용·게시가 기존 호출자와 동일 규칙으로 동작함을 테스트로 입증.
4. 배포 구성(별도 opt-in overlay), 문서(`docs/OPERATIONS.md`, `deploy/simple.env.example`), 단위·통합 테스트.
5. 단계적 검증·운영 반영·관찰 절차와 즉시 롤백 경로 문서화.

제외
- 기존 3개 호출자의 동작·기본값 변경, 기존 contract 해시/캐시 재작성, stable 이미지·포인터 교체(운영 전환은 아래 단계 게이트 통과 후 사용자 승인으로).
- 새 의존성을 `pyproject.toml`/`uv.lock`에 추가하는 것(opencode는 외부 실행 파일; Python 의존성 불필요). 필요 시 사유를 기록하고 멈춘다.

## 설계 지침

1. **Provider.** `OpenCodeOCRProvider` (`provider = "opencode"`, `reasoning_effort = settings값`, `model = "alibaba-token-plan/qwen3.8-flash"` 형태의 `provider/model` 전체 문자열을 contract `model`로 사용 — 다른 provider의 모델과 충돌하지 않고 opencode의 `-m` 인자와 일치). 별칭은 `opencode`만 허용(추가 별칭 금지).
2. **프로세스 호출.** `asyncio.create_subprocess_exec`로 `opencode run`을 호출한다. 기본 구성: 메시지(프롬프트+`_ocr_call_instructions`)를 stdin 또는 안전한 방식으로 전달, `-m <provider/model>`, `--variant <effort>`, `--format json`, `--pure`, 전용 `--agent`, 이미지는 `-f`로 전달(순서 함정 주의), `--dir`는 이미지가 있는 임시 디렉터리. shell 미사용, 인자 인젝션 검증(`CodexOCRProvider`의 검증 수준 이상). 타임아웃 시 프로세스 그룹까지 정리(`kill` 후 `wait`), `asyncio.CancelledError` 처리도 Codex와 동일.
3. **출력 파싱.** JSON 이벤트 스트림을 줄 단위로 파싱, `type=="text"` 파트만 순서대로 연결. `error` 이벤트, 비정상 종료, 빈 텍스트, 알 수 없는 스키마, 인코딩 오류, 크기 상한(기존 `MAX_PROVIDER_RESPONSE_BYTES` 정신) 초과는 `ProviderSystemicError`의 **기존 허용 reason code**로 분류(필요 시 허용 코드 추가는 최소화하고 테스트·문서 동기화). 응답 원문/stderr는 로그에 남기지 않고 크기·sha256만. 결과에 `reject_credential_bearing_ocr` 적용. 페이지 마커 형식·`split_ocr_pages` 계약을 만족하지 않으면 기존 검증 경로(`OCRValidationError`)에서 거부되어야 한다 — **검증 완화 금지**. 모델이 “설명/코드펜스/사고 내용”을 섞어 출력하는 경우를 샘플로 확인하고, 필요하면 프롬프트 공통부는 그대로 두고 provider 래퍼 수준에서만 안전하게 정리(프롬프트 변경은 `prompt_version`/contract를 바꾸므로 금지).
4. **환경/격리.** 자식 프로세스 환경은 화이트리스트(`PATH`, `LANG`, `LC_ALL`, `SSL_*`, opencode가 필요로 하는 `HOME`/`XDG_*`를 provider 전용 임시·읽기 전용 위치로 지정, 필요 시 `OPENCODE_CONFIG`/`OPENCODE_CONFIG_DIR` 사용). 호스트의 `~/.config/opencode` 전체(다른 provider·MCP·plugin)를 그대로 쓰지 말고, **OCR 전용 최소 config**(해당 provider/model/variant, 모든 tool deny, 전용 agent, autoupdate off, server 설정 불필요)를 생성·마운트하고 API 키는 secret 파일/환경(`ALIBABA_TOKEN_PLAN_API_KEY` 등, compose secrets 규칙에 맞춤)로만 주입한다. 키는 문서·로그·evidence에 쓰지 않는다. 사용자가 요청한 설정 조각(`provider.alibaba-token-plan.models.qwen3.8-flash.options.reasoningEffort=medium`)을 이 전용 config에 반영하고, `--variant`와의 중복/우선순위를 실측해 하나로 정한다.
5. **설정 env (신설, 기본값 명시).** `CARDRAG_OCR_PROVIDER=opencode`일 때: `CARDRAG_OCR_MODEL` 미지정 시 `alibaba-token-plan/qwen3.8-flash`, `CARDRAG_OCR_REASONING_EFFORT` 미지정 시 **`medium`**(기존 기본 `high`는 다른 provider용으로 유지 — provider별 기본값 처리 필요, fallback 포함), `CARDRAG_OPENCODE_EXECUTABLE`(기본 `opencode`), `CARDRAG_OPENCODE_CONFIG`(전용 config 경로), `CARDRAG_OPENCODE_AGENT`(전용 agent 이름), API 키 변수명. `CARDRAG_EXTERNAL_OCR_ALLOWED` 게이트에 `opencode` 포함(외부 API로 PDF 이미지가 나가므로 codex/openrouter와 동일 취급). `CARDRAG_OCR_FALLBACK_PROVIDER=opencode`도 지원하고 fallback 모델 기본값 분기(`cli.py` L440대)에 추가. `require_providers` 시 키·실행 파일 존재 확인.
6. **캐시.** contract의 `provider="opencode"`, `model`, `reasoning_effort="medium"`이 그대로 해시에 반영되게 하고 새 코드를 추가하지 않는다(우회 경로 금지). 샘플 단계에서는 반드시 `read-write`로 **격리된 별도 state**에서 쓰고, stable의 OCR 캐시/state/WebDAV 게시에는 쓰지 않는다. 운영 반영 후 캐시 게시는 기존 `CARDRAG_OCR_CACHE_PUBLICATION_APPROVED` 승인 규칙을 그대로 따른다. `compatible_ocr_models`에 다른 모델을 넣어 기존 캐시를 “호환”으로 재사용하는 동작은 이 과제에서 **켜지 않는다**(품질 비교 전에는 별도 contract로 유지).
7. **관측.** 호출 횟수/실패 reason/지연/토큰(가능하면 `step_finish.tokens`, `cost`를 집계용 메트릭으로만 — 응답 본문 제외)을 기존 성능·리포트 구조(`ocr_provider_called_count`, `performance.set(...)` 계열)에 맞춰 남긴다. 구조 확장이 크면 최소한 호출·실패·지연만 우선하고 REPORT에 기록한다.

## 실행 계획 (단계 게이트)

각 단계는 이전 단계의 합격 없이는 진행하지 않는다. 합격하지 못하면 해당 단계에서 멈추고 사실을 보고한다.

### 단계 0 — 사전 점검
- 기준 commit, 작업 트리, 운영 timer 상태(`systemctl status cardrag-worker.timer`, 최근 run), `/opt/cardrag/current` 버전과 디스크 여유를 **읽기 전용**으로 확인한다. 어떤 실험도 운영 timer·`/etc/cardrag/worker.env`·운영 state 볼륨·WebDAV stable 채널을 수정하지 않는다.
- 작업 브랜치를 새로 만들고(예: `feat/opencode-ocr-provider`), 구현은 그 브랜치에서만 한다.
- opencode 실행 위치 결정: (a) Worker 이미지에 pin된 버전으로 설치(권장, 재현성), (b) 호스트 바이너리 마운트. 이미지 설치 시 `Dockerfile` 변경은 hadolint, 고정 버전/체크섬, read-only rootfs·UID 10001에서의 쓰기 위치(`HOME`, XDG, tmpfs) 동작을 검증하고, **새 후보 이미지 tag로만** 빌드한다. 결정과 근거를 REPORT에 쓴다.

### 단계 1 — 구현과 단위/통합 테스트 (운영 무관)
- 위 설계 지침대로 구현. 기존 [tests/test_ocr.py](../../apps/cardrag-worker/tests/test_ocr.py), `test_cli_settings_provider.py`, `test_ocr_recovery.py`의 패턴을 따라 **실제 opencode 호출 없는 가짜 실행 파일(fake binary)** 기반 테스트를 추가한다. 필수 케이스: 정상 JSON 스트림 파싱, 여러 text 파트 연결, `error` 이벤트, 비정상 종료 코드, 빈 출력/비 UTF-8/스키마 불명, 타임아웃/취소 시 프로세스 정리, 자격 정보 유사 문자열 거부, 인자 순서(메시지 → 옵션 → `-f`)와 인젝션 방지, 환경 화이트리스트, 설정 검증(외부 OCR 미허용 시 거부, 기본 모델/effort, fallback 조합), **캐시 contract가 provider/model/effort 변화에 따라 달라지고 기존 3개 provider의 contract 해시는 바뀌지 않음**, 캐시 히트 시 provider 미호출.
- 기존 전체 worker/core 테스트, `ruff`, `mypy`(저장소 기존 설정; `uv run` 또는 `mise exec` 등 저장소 규칙 우선), shell 변경 시 `shellcheck`, compose 변경 시 `docker compose config --quiet`, Dockerfile 변경 시 `hadolint`, 자격/설정 변경이 있으므로 `gitleaks`로 변경분 검사. 

### 단계 2 — `opencode run` 직접 실사 (호스트, 저비용)
- 합성/공개 가능 샘플 PDF 페이지 이미지 몇 장으로 직접 호출해 (1) 한글 표·각주 전사 품질, (2) 페이지 마커 준수, (3) 다중 이미지 `-f` 첨부, (4) 토큰·지연, (5) 전용 agent로 도구 호출이 0건임, (6) 호스트 `opencode-web`와 충돌 없음을 확인한다. 결과물(본문 제외 지표·sha)만 REPORT에 남기고 PDF/OCR 본문은 저장소에 커밋하지 않는다.

### 단계 3 — 격리된 샘플 파이프라인 (운영 영향 0)
- **별도 state 디렉터리/볼륨, 별도 compose project, 별도 WebDAV 채널 또는 로컬 전용(게시 비활성) 설정**으로 Worker를 실행한다. `CARDRAG_OCR_PROVIDER=opencode`, `CARDRAG_EXTERNAL_OCR_ALLOWED=true`, `CARDRAG_OCR_CACHE_MODE=read-write`(격리 state 안에서만). stable 포인터·운영 캐시·timer에 쓰기 금지; 읽기 전용 입력(PDF 원본)만 stable과 공유할 수 있다.
- 샘플 선정 기준(**서로 다른 유형 3–5건**): 작은 문서(≤5쪽), 중간(10–30쪽), 큰 표/각주 중심, 가능하면 `OCR 분할 호출`(`whole_document_max_pages` 초과) 경로를 타는 큰 문서 1건. 신규 PDF 등록을 기다리지 않고, 이미 캐시된 문서의 PDF를 **격리 state에서** 새 contract로 다시 OCR하여 같은 문서의 기존 결과와 품질을 비교한다(비교는 페이지 수 일치, 표/숫자 보존, 누락/중복 페이지, 길이 비율; 본문 공개 금지).
- 확인 항목: 정상 기동, 문서당 소요/토큰, 실패율 0 또는 설명 가능한 사유, **같은 입력 재실행 시 캐시 히트로 provider 호출 0**, 중단 후 재시작 복구(`test_ocr_recovery` 계열 시나리오), `reject_credential_bearing_ocr` 오탐 여부, 결과가 검증기(`split_ocr_pages`, 페이지 수)를 통과하고 `ocr_validation` 실패가 없을 것.
- 합격 기준(예): 샘플 전부 완료, 검증 실패 0, 페이지 누락/중복 0, 재실행 캐시 히트 100%, 품질 비교에서 기존 provider 대비 **핵심 숫자/표 손실이 없거나 설명 가능**. 기준 미달 시 모델/프롬프트 래핑/분할 설정 조정 후 단계 3 재실행(프롬프트 변경이 필요하면 `prompt_version` 규칙을 따르고 별도 근거 기록).

### 단계 4 — 운영 반영 (무중단, 점진, 사용자 승인 후)
- 운영 반영은 **코드 배포(후보 이미지 → 승인 후 stable)와 provider 활성화를 분리**한다. 코드가 반영되어도 `CARDRAG_OCR_PROVIDER`가 기존 값인 한 동작은 바뀌지 않아야 한다(테스트로 보증).
- 활성화 방식(권장 순서): 기존 provider를 **primary로 유지**하고, 신규 PDF에 한해 opencode를 쓰려면 env/overlay로 전환하되 **fallback에 현 stable provider**(예: `CARDRAG_OCR_FALLBACK_PROVIDER`)를 지정해 opencode 실패 시 기존 경로로 즉시 이어지게 한다(`FailoverOCRResolver`). 반대 구성(기존 primary + opencode fallback)과의 차이를 문서화하고 선택 근거를 기록한다. 운영 env 변경은 `/etc/cardrag/worker.env` 백업 후 변경분만 적용, 적용 전후 `docker compose ... config --quiet`, timer 다음 실행 전 검증. 진행 중인 run이 있으면 끝난 뒤 적용(락 규칙 준수).
- 롤백: env 한 줄(`CARDRAG_OCR_PROVIDER` 원복 또는 overlay 제거) + 직전 이미지 digest로 복귀. 롤백 절차를 **적용 전에 문서화**하고, opencode로 만들어진 캐시/산출물이 stable 결과에 섞였을 때의 처리(별도 contract이므로 기존 hit에는 영향 없음, 문서 단위 되돌림 방법)를 적는다. 롤백 경로가 확인되지 않으면 적용하지 않는다.

### 단계 5 — 관찰 (수일)
- 관찰 기간 동안 매 run마다 확인할 지표와 중단 조건을 REPORT(또는 후속 `FIX_NN`)에 정의한다: opencode 호출 수·성공률, 실패 reason code 분포, 문서당 지연·토큰/비용, 검증 실패, fallback 발동 수, 캐시 히트율, 디스크·프로세스 잔류(좀비/고아 `opencode` 프로세스), 토큰 플랜 한도/인증 실패, 호스트·컨테이너 메모리. 신규 PDF 등록이 있을 때만 호출이 발생하므로 **호출 0건인 날을 실패로 오판하지 않는다** — 관찰 종료 기준은 “기간”이 아니라 “최소 N건 실호출(권장 ≥10건, 서로 다른 유형)과 중단 조건 미발생”으로 정한다.
- 중단 조건 예: 연속 systemic 실패 3회, 검증 실패 발생, 품질 비교에서 숫자/표 손실 재현, 토큰 한도 소진, 인증 만료. 중단 시 즉시 롤백 후 보고.

## 캐시 원칙 (사용자 확인 사항)

- **어떤 호출자(opencode, codex-exec, openrouter, local-paddleocr)든 같은 PDF를 같은 설정으로 두 번째 처리할 때는 OCR 호출 없이 캐시에서 읽어야 한다.** 이는 provider별 코드가 아니라 공통 `OCRResolver`/contract 경로에서 처리되므로, 새 호출자는 **캐시 코드를 따로 만들지 않고** 이 공통 경로만 거치게 한다.
- 캐시 키에는 provider·model·reasoning effort가 포함된다. 따라서 "같은 호출자로 재실행 → 항상 캐시 히트", "호출자를 바꾸면 새로 OCR(서로의 캐시를 덮어쓰지 않음)"이 기본 동작이다. 호출자 간 결과 공유는 이 과제 범위가 아니다(`compatible_ocr_models` 미사용).
- 테스트와 샘플 단계에서 **각 호출자마다 "1회차 호출 발생, 2회차 provider 호출 0건"**을 직접 확인한다(fake provider 테스트 + 실제 샘플).

## 수용 기준

1. `CARDRAG_OCR_PROVIDER=opencode`(및 fallback 사용)가 설정 검증→호출→파싱→검증→캐시 저장/재사용까지 기존 호출자와 동일한 계약으로 동작한다. 기본 모델 `alibaba-token-plan/qwen3.8-flash`, effort `medium`.
2. 기존 `codex-exec`/`openrouter`/`local-paddleocr`의 동작·기본값·contract 해시가 **변하지 않음**이 테스트로 입증된다. provider 미지정 시 기존 기본값 유지.
3. opencode 호출은 도구 비활성 전용 agent, 최소 환경, 최소 config로 격리되며 키/응답 원문이 로그·evidence·저장소에 남지 않는다. `gitleaks` 변경분 검사 통과.
4. 단계 3 샘플 합격 기준을 충족하고 증거(지표·sha·실행 명령)가 REPORT에 있다. 운영 stable state/캐시/포인터/timer는 단계 3까지 **한 번도 쓰이지 않았음**을 전후 비교(해시/mtime/run 이력)로 보인다.
5. 운영 반영은 사용자 승인 후 단계 4 절차로만 수행되며 롤백이 사전 검증된다. 관찰 지표·중단 조건·종료 기준이 정의되어 있다.
6. lint/type/전체 테스트/compose·Dockerfile 검증이 통과하고, 문서(`docs/OPERATIONS.md`, `deploy/simple.env.example`)가 새 env·격리·롤백·관찰 절차를 반영한다.

## 필수 검증 보고 (`REPORT.md`)

변경 파일과 설계 결정(opencode 실행 위치, stdin vs argv, agent/config 방식, variant 우선순위), 실행한 모든 명령과 결과, 사전 실험 대비 실측치(토큰·지연), 샘플 목록(유형·쪽수, 본문 제외)과 품질 비교 결과, 운영 무영향 증거, 승인이 필요한 미실행 항목(단계 4–5), 잔여 위험을 기록한다. 단계 4 이후는 사용자 승인 없이 진행하지 말고 단계 3까지 완료한 상태에서 보고해도 된다. 계획과 다른 점은 사유와 함께 명시한다.

## 관련 파일

- `apps/cardrag-worker/src/cardrag_worker/{providers.py,settings.py,cli.py,ocr.py,pipeline.py}`
- `packages/cardrag-core/src/cardrag_core/ocr.py`
- `apps/cardrag-worker/tests/{test_ocr.py,test_cli_settings_provider.py,test_ocr_recovery.py}`
- `deploy/worker/{compose.yaml,compose.paddleocr.yaml,compose.secrets.yaml,cardrag-worker.service}`, `deploy/simple.env.example`, `Dockerfile`, `docs/OPERATIONS.md`
- 선행 기록: `.handoff/001`~`005`(특히 OCR 캐시·운영 전환 규칙: 001, 002, 004)
