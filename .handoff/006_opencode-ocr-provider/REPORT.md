# 006 OpenCode OCR 호출자 신설 및 무중단 단계 도입 실행 보고서

작성 역할: Executor. 대상 저장소: `/home/lee/projects/MCP_card_prd_detail`, 작업 브랜치: `feat/006-opencode-ocr-provider`.

## 작업 개요 및 목적 달성 여부

[PLAN.md](PLAN.md)에 명시된 지침과 원칙에 따라, Worker의 OCR 호출자에 **`opencode` 경로**(`OpenCodeOCRProvider`)를 신설하고 캐시 격리·재사용, 환경 격리, CLI 설정 배선, 단위/통합 테스트, 실사 프로빙을 완료했습니다.

- **기본 모델/옵션**: `alibaba-token-plan/qwen3.8-flash`, reasoning effort `medium`.
- **호출자 선택 및 게이트**: `CARDRAG_OCR_PROVIDER=opencode` (또는 fallback provider) 지원 및 `CARDRAG_EXTERNAL_OCR_ALLOWED=true` 필수 게이트 적용.
- **캐시 계약 및 재사용**: 기존 호출자들과 동일하게 `NativeOCRContract` 기반으로 `provider="opencode"`, `model="alibaba-token-plan/qwen3.8-flash"`, `reasoning_effort="medium"` 조합이 캐시 키에 반영되어 **1회차 호출 후 2회차 실행 시 provider 호출 0건(100% 캐시 히트)**이 보장됨을 테스트로 입증.
- **운영 안정성**: 기존 stable 운영 환경(`cardrag-worker.timer` active 유지, stable 채널 및 기존 OCR 캐시 무변경)을 일체 침해하지 않고 격리된 상태에서 구현 및 검증 완료.

---

## 변경 파일 목록

1. [apps/cardrag-worker/src/cardrag_worker/providers.py](../../apps/cardrag-worker/src/cardrag_worker/providers.py):
   - `OPENCODE_OCR_INHERITED_ENVIRONMENT_KEYS` 상수 정의.
   - `_classify_opencode_process_exit`: 안전한 프로세스 종료 코드/stderr 분류 함수.
   - `OpenCodeOCRProvider` 구현:
     - `opencode run -m alibaba-token-plan/qwen3.8-flash --variant medium --format json --pure -f <image>...` 서브프로세스 호출.
     - 격리된 작업 디렉터리(`HOME`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME` 격리) 및 도구(`bash`, `write`, `edit`, `browse`)가 모두 비활성화된 최소 `.opencode-ocr.json` 설정 자동 주입.
     - stdin을 통한 프롬프트/페이지 지시문 전달.
     - 스트리밍된 JSON 이벤트 중 `type == "text"` 파트 안전 연결 및 `reject_credential_bearing_ocr` 검증.
     - 타임아웃 및 프로세스 강제 정리(`kill`, `wait`).
   - `make_ocr_provider`: `"opencode"` 분기 추가 및 관련 인자 배선.

2. [apps/cardrag-worker/src/cardrag_worker/settings.py](../../apps/cardrag-worker/src/cardrag_worker/settings.py):
   - `WorkerSettings`에 OpenCode 관련 설정 필드 추가:
     - `opencode_executable`: 기본값 `"opencode"` (`CARDRAG_OPENCODE_EXECUTABLE`)
     - `opencode_config`: 전용 config 파일 경로 (`CARDRAG_OPENCODE_CONFIG`)
     - `opencode_agent`: 전용 agent 이름 (`CARDRAG_OPENCODE_AGENT`)
     - `opencode_api_key_env_var`: API 키 환경변수명 (`CARDRAG_OPENCODE_API_KEY_ENV_KEY`, 기본값 `ALIBABA_TOKEN_PLAN_API_KEY`)
   - `CARDRAG_EXTERNAL_OCR_ALLOWED=false` 시 `opencode` 진입 차단 검증.
   - `CARDRAG_OCR_PROVIDER=opencode` 선택 시 `ocr_model` 기본값을 `"alibaba-token-plan/qwen3.8-flash"`, `ocr_reasoning_effort` 기본값을 `"medium"`으로 자동 설정.

3. [apps/cardrag-worker/src/cardrag_worker/cli.py](../../apps/cardrag-worker/src/cardrag_worker/cli.py):
   - `_provider()` 팩토리 호출 시 OpenCode 관련 인자 전달.
   - `CARDRAG_OCR_FALLBACK_PROVIDER=opencode` 지정 시 기본 fallback model로 `"alibaba-token-plan/qwen3.8-flash"` 지정.

4. [deploy/simple.env.example](../../deploy/simple.env.example):
   - OpenCode OCR provider 활성화 예시 환경변수 설정 블록 추가.

5. [deploy/worker/compose.yaml](../../deploy/worker/compose.yaml):
   - OpenCode 관련 환경변수(`CARDRAG_OPENCODE_*`, `ALIBABA_TOKEN_PLAN_API_KEY`) 전달 정의 추가.

6. [apps/cardrag-worker/tests/test_cli_settings_provider.py](../../apps/cardrag-worker/tests/test_cli_settings_provider.py):
   - `test_opencode_ocr_subprocess_invocation_and_json_parsing`: 서브프로세스 인자 전달, JSON 스트리밍 파싱, 환경변수 누출 차단, 도구 비활성화 최소 config 검증.
   - `test_opencode_ocr_rejects_credential_bearing_output`: 자격증명 패턴 검출 시 즉각 차단 검증.
   - `test_opencode_ocr_missing_api_key_fails_systemically`: API 키 부재 시 systemic 에러 검증.
   - `test_settings_opencode_provider_defaults_and_gates`: 기본값 및 `CARDRAG_EXTERNAL_OCR_ALLOWED` 게이트 검증.

7. [apps/cardrag-worker/tests/test_ocr.py](../../apps/cardrag-worker/tests/test_ocr.py):
   - `test_opencode_ocr_caching_and_contract_isolation`:
     - 1회차 실행 시 `provider_called=True`, `cache_reused=False`
     - 2회차 실행 시 동일 설정 `provider_called=False`, `cache_reused=True`, provider 호출 수 증가 없음 (완전한 캐시 히트) 검증.

---

## 검증 결과 및 증거

### 1. 단위/통합 테스트 (pytest)
- `uv run pytest apps/cardrag-worker/tests/test_ocr.py apps/cardrag-worker/tests/test_cli_settings_provider.py`
  - 결과: **225 passed in 3.38s**
- 전체 리포지토리 테스트 스위트:
  - 결과: 전체 패스 완료.

### 2. 정적 분석 및 보안 검사
- `uv run ruff check apps packages tests`:
  - 결과: **All checks passed!**
- `uv run mypy apps/cardrag-worker/src/cardrag_worker/providers.py apps/cardrag-worker/src/cardrag_worker/settings.py apps/cardrag-worker/src/cardrag_worker/cli.py`:
  - 결과: **Success: no issues found in 3 source files**
- `docker compose --env-file deploy/simple.env.example -f deploy/worker/compose.yaml config --quiet`:
  - 결과: 문법 오류 없이 검증 통과 (exit code 0).
- `gitleaks detect`:
  - 변경 디렉터리(`apps/cardrag-worker/src`, `apps/cardrag-worker/tests`, `deploy`) 검사 결과: **no leaks found**.

### 3. 실사(Live Probe) 결과
호스트에 등록된 실제 `opencode` CLI 및 `alibaba-token-plan/qwen3.8-flash` (reasoning medium) 모델로 `OpenCodeOCRProvider.recognize()`를 직접 실행하여 실사 완료:
- 입력: 카드 상품안내장 샘플(연회비, 전월 이용실적, 스타벅스 10% 청구할인 등 텍스트 포함 PNG)
- 출력:
  ```markdown
  ## Page 1

  신용카드 상품안내장 (테스트 카드)

  연회비: 국내전용 15,000원 / 해외겸용 18,000원

  혜택: 전월 이용실적 30만원 이상 시 스타벅스 10% 청구할인 (월 최대 5,000...
  ```
- 한국어 및 숫자/조건 정확 전사 확인.
- 도구가 비활성화된 순수 OCR 스트리밍으로 불필요한 도구 호출 0건 확인.

---

## 운영 환경 무중단 현황 및 향후 절차

- **운영 타이머 상태**:
  - `systemctl is-active cardrag-worker.timer` -> `active` 유지 중.
  - 현재 운영 환경(/opt/cardrag/current 등)과 운영 볼륨은 변경되지 않았습니다.
- **향후 단계 (단계 4 및 5)**:
  - 신규 카드사 PDF 등록 시 새 OCR 호출이 유입되므로, 운영 반영 시에는 `CARDRAG_OCR_PROVIDER=opencode` 및 `CARDRAG_EXTERNAL_OCR_ALLOWED=true`, `CARDRAG_OCR_FALLBACK_PROVIDER=local-paddleocr` (또는 codex-exec) 설정을 통해 장애 발생 시에도 무중단 fallback이 이뤄지도록 셋업을 권장합니다.
  - 운영 반영 후 수일간 호출 성공률, 지연 시간, 토큰 사용량을 모니터링합니다.
