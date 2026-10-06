# FIX_01_REPORT — OpenCode OCR 후보 격리 샘플 파이프라인 및 인수 요건 완료 보고

- **작성 역할**: Executor
- **일자**: 2026-10-06 KST
- **기준 브랜치**: `feat/006-opencode-ocr-provider` (commit `2bcd514` 기반)
- **대상 handoff**: `.handoff/006_opencode-ocr-provider/FIX_01.md`

---

## 1. 개요 및 인수 범위 요약

Reviewer의 `FIX_01.md`에서 지적된 5대 개선 항목(P1 4건, P2 1건)을 전부 이행하였으며, PLAN Stage 3(격리 샘플 파이프라인 및 재실행 100% 캐시 히트 검증)을 완전히 통과하였습니다.
본 보고서는 PLAN 단계 3까지의 완료 상태를 보고하며, **운영 stable 전환(단계 4) 및 운영 timer/볼륨 변경은 일체 수행하지 않고 안전하게 멈추었습니다.**

---

## 2. 수정 사항 세부 내역

### 2.1 [P1] Worker 이미지 패키징 및 컨테이너 런타임 격리
- **바이너리 고정 및 검증**:
  - `Dockerfile`에 npm 공식 registry tarball `https://registry.npmjs.org/opencode-linux-x64/-/opencode-linux-x64-1.18.34.tgz` (sha256 `b83e8ac66d752d05ead4b6a439d3a2cfa32bcd9817c708825a389b5d5cba4f19`)를 다운로드 및 sha256 체크섬 검증 후 `/usr/local/bin/opencode` (ELF 64-bit binary sha256: `9ca0b9953d49997601655e54f846a3efa464f237e47c6f1b04716d0f2e64c4c2`, 권한 `0755`)로 설치.
  - `hadolint Dockerfile` 무오류 통과.
- **후보 이미지 빌드**:
  - `docker build --target worker -t cardrag-worker:candidate-opencode .`
  - 이미지 ID: `sha256:cd68ab72674c722fead128f995f50cc14218d7d751856169df83bdcec582804b`
- **보안/권한 런타임 검증**:
  - 컨테이너 UID 10001, `--read-only` rootfs, `--tmpfs /tmp` 환경에서 실행 검증:
    ```bash
    docker run --rm --read-only --tmpfs /tmp --user 10001:10001 \
      -e HOME=/tmp -e XDG_CONFIG_HOME=/tmp -e XDG_DATA_HOME=/tmp \
      cardrag-worker:candidate-opencode opencode --version
    # 출력: 1.18.34
    ```
  - 컨테이너 내부 격리 환경에서 합성 1페이지 이미지 실사 성공 (`## Page 1` 마커 및 전사 확인).

### 2.2 [P1] Compose Opt-in Overlay 및 독립 설정 배선
- **기본 compose 불변성 유지**:
  - `deploy/worker/compose.yaml`의 기본 `CARDRAG_OCR_MODEL`(`gpt-5.6-sol`) 및 `CARDRAG_OCR_REASONING_EFFORT`(`high`)는 기존 provider(codex-exec 등)를 위해 보존.
- **Opt-in Overlay 신설**:
  - `deploy/worker/compose.opencode.yaml` 신설:
    - `CARDRAG_OCR_PROVIDER=opencode`
    - `CARDRAG_OCR_MODEL=${CARDRAG_OPENCODE_OCR_MODEL:-alibaba-token-plan/qwen3.8-flash}`
    - `CARDRAG_OCR_REASONING_EFFORT=${CARDRAG_OPENCODE_OCR_REASONING_EFFORT:-medium}`
    - `CARDRAG_EXTERNAL_OCR_ALLOWED=true`
- **Compose Config 검증**:
  - `docker compose --env-file deploy/simple.env.example -f deploy/worker/compose.yaml -f deploy/worker/compose.opencode.yaml config` 검증 결과, `model=alibaba-token-plan/qwen3.8-flash`, `effort=medium`이 정상 반영됨을 확인 (비밀 노출 없음).
- **Fallback 독립 배선**:
  - `apps/cardrag-worker/src/cardrag_worker/cli.py` 및 `settings.py`에서 `opencode_ocr_reasoning_effort`(`medium`)를 별도 정의하여, primary가 codex-exec(`high`)이더라도 fallback opencode는 항상 `medium`과 전용 모델을 사용하도록 분리.

### 2.3 [P1] 도구 격리 및 보안 Gate 강화
- **전용 에이전트 강제**:
  - `apps/cardrag-worker/src/cardrag_worker/providers.py`의 `OpenCodeOCRProvider` 기본 agent를 `ocr`로 지정.
- **전체 도구 및 권한 Deny 주입**:
  - 자동 생성되는 격리 config에 root 및 agent 레벨 모두 `tools: {"*": false}` 및 `permission: {"*": "deny"}` 강제.
  - CLI 인자로 `--agent ocr --pure --format json` 주입.
- **외부 Config 보안성 사전 검증**:
  - `config_path` 전달 시에도 `tools: {"*": False}`와 `permission: {"*": "deny"}`가 선언되어 있지 않으면 `ProviderSystemicError("provider_systemic_failure")`로 fail-closed 거부.
- **Settings 사전 검증 (`require_providers=True`)**:
  - API 키 존재 여부 검증.
  - 실행 파일 존재 및 실행 비트 검증 (`os.access(..., os.X_OK)`).
  - Config 보안성 사전 검증.

### 2.4 [P2] 응답 스트림 파싱 및 프로세스 정리
- **스트림 수신 상한 (`_communicate_bounded`)**:
  - 메모리 버퍼 수신 도중 `MAX_PROVIDER_RESPONSE_BYTES` 초과 시 프로세스를 즉시 kill하고 `ProviderSystemicError("provider_response_too_large")` 발생.
- **Timeout / 취소 시 프로세스 정리**:
  - `with contextlib.suppress(ProcessLookupError): process.kill()`, `await process.wait()`로 좀비/고아 프로세스 방지.
- **엄격한 JSON 스트림 파싱**:
  - 비 JSON 행, UTF-8 디코딩 실패, 빈 텍스트, 스키마 불일치, 명시적 `error` 이벤트 시 `provider_contract_invalid`로 엄격 실패 처리.
  - 비 JSON 원문 fallback 코드 완전 제거.
- **종료 코드 진단 분류**:
  - OpenCode가 비정상 종료(exit code != 0)할 경우 stderr뿐만 아니라 stdout의 JSON error 이벤트까지 결합 분석하여 `provider_process_rate_limited`, `provider_process_authentication_failed`, `provider_process_provider_unavailable` 등의 allowlist reason으로 분류.

---

## 3. PLAN Stage 3 격리 샘플 파이프라인 검증 결과

운영 WebDAV 및 운영 state와 완전히 분리된 임시 state DB와 격리 디렉터리(`stage_3_verification`)에서 3가지 상이한 PDF 유형을 `OCRResolver`를 통해 순차 검증하였습니다.

### 3.1 검증 대상 문서군 (3개 유형)
1. **Type 1 (Short 1p)**: 카드 기본 안내 요약 (전체 문서 직렬 경로)
2. **Type 2 (Table & Footnotes 2p)**: 혜택 구간 할인표 및 주요 유의사항 각주 (복합 표/각주 경로)
3. **Type 3 (Chunked 5p)**: 5페이지 다중 페이지 문서 (`whole_document_max_pages=4`, `chunk_pages=2` 분할 호출 경로)

### 3.2 2회차 실행 실측 지표 (Run 1 vs Run 2)

```json
{
  "run1 (초기 OCR 호출)": {
    "type1_short (1p)": {
      "pages": 1,
      "provider_called": true,
      "cache_reused": false,
      "elapsed_seconds": 17.94,
      "ocr_sha256": "de04eb9f94a85a6bc279c512261b208a7f433493b70d35ae207d91a96aa90b9a",
      "ocr_bytes_len": 295,
      "marker_verified": "## Page 1"
    },
    "type2_table (2p)": {
      "pages": 2,
      "provider_called": true,
      "cache_reused": false,
      "elapsed_seconds": 19.82,
      "ocr_sha256": "533379a737155e9489237be37f08c1a535fef85933e5122f7c83de04b9a8eaa6",
      "ocr_bytes_len": 624,
      "marker_verified": "## Page 2"
    },
    "type3_chunked (5p)": {
      "pages": 5,
      "provider_called": true,
      "cache_reused": false,
      "elapsed_seconds": 63.64,
      "ocr_sha256": "60967c42525ba5db79d9882bb8e179f7aafa090824f8c09c633f8048b6892751",
      "ocr_bytes_len": 1077,
      "marker_verified": "## Page 5"
    }
  },
  "run2 (재실행 캐시 검증)": {
    "type1_short (1p)": {
      "pages": 1,
      "provider_called": false,
      "cache_reused": true,
      "elapsed_seconds": 0.0014,
      "identical_sha256": true
    },
    "type2_table (2p)": {
      "pages": 2,
      "provider_called": false,
      "cache_reused": true,
      "elapsed_seconds": 0.0026,
      "identical_sha256": true
    },
    "type3_chunked (5p)": {
      "pages": 5,
      "provider_called": false,
      "cache_reused": true,
      "elapsed_seconds": 0.0022,
      "identical_sha256": true
    }
  }
}
```

### 3.3 검증 평가
- **100% 캐시 적중**: Run 2에서 3개 문서 모두 `provider_called == False`, `cache_reused == True`, 소요시간 ≤0.003초로 완벽한 캐시 재사용 달성.
- **산출물 무결성**: Run 1과 Run 2의 산출물 SHA256 해시가 100% 일치.
- **페이지 정합성**: `split_ocr_pages` 검증을 통과하였으며 페이지 누락 또는 중복 0건.

---

## 4. 운영 문서 및 환경 설정 반영

- **`deploy/simple.env.example`**:
  - `CARDRAG_OPENCODE_OCR_MODEL`, `CARDRAG_OPENCODE_OCR_REASONING_EFFORT`, `CARDRAG_OPENCODE_EXECUTABLE`, `CARDRAG_OPENCODE_AGENT`, `ALIBABA_TOKEN_PLAN_API_KEY` 옵션 상세 문서화.
- **`docs/OPERATIONS.md`**:
  - OpenCode OCR provider 도입 배경 및 `compose.opencode.yaml` overlay 구동 가이드 추가.
  - 전용 agent(`ocr`), 도구 deny 격리, 비밀 마운트 규칙 명시.
  - 후보 검증(`compose.candidate.yaml`) 시 운영 timer/WebDAV 무영향 공존 구조 기술.

---

## 5. 정적 분석, 테스트 및 보안 검증

1. **테스트 스위트 전체 통과**:
   - `apps/cardrag-worker`: **1,158개 테스트 통과** (0 fail, 9 expected warnings)
   - `packages/cardrag-core` & `apps/cardrag-mcp`: **1,057개 테스트 통과** (0 fail)
   - **총 2,215개 테스트 통과** (기존 provider들의 contract hash 및 동작 완전 보존).
2. **코드 린트 및 포맷팅**:
   - `uv run ruff check apps packages tests`: All checks passed!
   - `git diff --check`: 0 whitespace / EOF errors.
3. **정적 타입 검사**:
   - `uv run mypy packages/cardrag-core/src apps/cardrag-worker/src apps/cardrag-mcp/src`: `Success: no issues found in 98 source files` (`strict = true`).
4. **비밀 유출 검사**:
   - diff 내 API key 패턴 정규식 검사 완료 (매칭 0건, 평문 키 및 자격증명 커밋/로그 없음).

---

## 6. 운영 무영향 실증 (Zero-Downtime Evidence)

- **Systemd Timer 상태**:
  ```bash
  systemctl status cardrag-worker.timer
  # Active: active (waiting) since Sun 2026-10-04 11:47:55 KST; 2 days ago
  # Trigger: Wed 2026-10-07 03:00:00 KST; 11h left
  ```
  운영 타이머는 중단되거나 변경되지 않고 지속 대기 중입니다.
- **운영 릴리스 링크**:
  `/opt/cardrag/current -> /opt/cardrag/v1.0.29` (수정일 9월 27일 상태 불변).
- **운영 볼륨 및 WebDAV**:
  격리된 임시 scratch 디렉터리만 사용하였으며, 프로덕션 볼륨 및 WebDAV stable/candidate 경로를 일체 건드리지 않았습니다.

---

## 7. 차단점 및 다음 단계 제언

- **Stage 3 완료 및 승인 대기**:
  - FIX_01의 모든 수정 요건과 PLAN Stage 3의 수용 기준을 완전히 충족하였습니다.
  - **PLAN Stage 4 (운영 반영)는 사용자의 명시적 승인 후 진행되어야 하므로 현재 안전하게 정지한 상태입니다.**
- **Stage 4 진행 시 제언**:
  1. 사용자 승인 후 `cardrag-worker:candidate-opencode` 태그를 운영 후보로 등록.
  2. `/etc/cardrag/worker.env` 백업 후 `compose.opencode.yaml` overlay를 opt-in하고, `CARDRAG_OCR_FALLBACK_PROVIDER=codex-exec` 설정으로 안전망 구축.
  3. 신규 카드가 등록되는 주기 동안 며칠간 Stage 5 관찰 지표(성공률, latency, 캐시 적중률) 모니터링.
