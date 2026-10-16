# 016 FIX_04_REPORT — 품질 출력 경계 검증 및 일반 배치 품질 보존 구현 보고

- 작성: 2026-10-16 11:22 KST, Executor Antigravity.
- 대상: `.handoff/016_worker-repeat-cosine-preflight-failure/FIX_04.md` (Reviewer Codex 판정 및 수정 요구 2건).
- Git 상태: HEAD `1764aef` 기반 작업 트리 수정 완료.

---

## 1. 개요 및 이전 보고 범위 정정

`FIX_03_REPORT`에서 “모든 요구사항 완벽 이행”으로 기술했던 판정 범위를 정정합니다.
Reviewer Codex가 `FIX_04.md`에서 지적한 바와 같이, 당시 구현에는 다음 2가지 결함이 남아 있었습니다:
1. `startup_only=True` 준비 완료 JSON 출력 시점 및 Provider 생성 시점에서 비허용 임의 키/변조 데이터가 `validated_quality_details` 출력 경계 스키마 검증을 우회할 수 있었던 점 (`[P1]`).
2. 일반 일배치(`startup_only=False`) 실행 시, 파이프라인 결과(`PipelineResult`) 및 백업 정보로만 최종 JSON을 구성하여 startup preflight에서 발생한 `quality_status="warning"` 및 상세 메타데이터가 종단 JSON에서 탈락했던 점 (`[P2]`).

금번 FIX_04에서는 위 2가지 결함을 제품 코드 및 테스트 스위트에 걸쳐 완전하게 수정 및 검증했습니다.

---

## 2. 세부 변경 사항

### 2.1 [P1] 품질 직렬화 경계 스키마 검증 및 방어적 복사 (`apps/cardrag-worker/src/cardrag_worker/cli.py`, `embedding_v5.py`)

- **통합 품질 직렬화 헬퍼 `_project_embedding_quality_payload` 도입 (`cli.py:898–930`):**
  - 입력 `embeddings` 객체로부터 속성을 안전하게 추출하여 allowlist 스키마로 프로젝션하는 공통 함수를 추가했습니다.
  - `quality_status`: 유효 enum (`"passed"`, `"warning"`)으로 제한하며 비허용 값은 fail-safe로 `"passed"` 처리.
  - `preflight_mode`: `"daily_smoke"` 또는 `"qualification"`.
  - `evaluated_provider_count`: 1 이상의 양의 정수.
  - `cross_evaluated`: `bool`.
  - `minimum_cross_provider_cosine`: `cross_evaluated=True`이고 유한한 실수(-1.0 ~ 1.0)인 경우에만 float 유지, 단일 provider 등 미평가(`cross_evaluated=False`)인 경우 명시적 `None` (JSON `null`)으로 고정.
  - `quality_details`: `quality_status == "warning"`인 경우에만 `validated_quality_details("repeat_cosine_warning", raw_details)`를 출력 직렬화 직전에 재실행하여 허용된 필드(`gate_threshold`, `warning_threshold`, `minimum_cosine`, `mean_cosine`, `failed_sample_count`, `sample_count`, `first_failed_index`, `lowest_failed_index`, hex64 해시들)만 통과시키고, 비허용 키(`unexpected_private_field`), 비정상 문자열, `NaN`/`Inf`는 완전 탈락.
- **Provider 생성자 방어적 복사 및 검증 (`embedding_v5.py:782–847`):**
  - `OpenRouterQwenEmbeddingProviderV5.__init__`에서 `quality_details` 인입 시 `dict(validated_quality_details("repeat_cosine_warning", quality_details))`로 방어적 유효성 검사 및 얕은 복사를 수행.
  - `preflight_mode`, `evaluated_provider_count`, `cross_evaluated`, `minimum_cross_provider_cosine`, `mean_cross_provider_cosine` 속성을 안전하게 수용하고 유한 수치 검증 후 보관.
  - 생성 후 provider 인스턴스의 mutable dict가 변조되더라도, 종단 직렬화 시점의 `_project_embedding_quality_payload`에 의해 출력 경계에서 한 번 더 필터링됨.
- **Startup 이력(`StartupAttemptRecorder`) 보존 동기화 (`cli.py:522–585`, `1240–1255`):**
  - `record()` 메서드에 `preflight_mode`, `evaluated_provider_count`, `cross_evaluated`, `minimum_cross_provider_cosine` 인수를 추가.
  - `qwen_endpoint_preflight` stage가 passed/warning 통과 시 해당 메타데이터를 `startup-attempt-history.jsonl`에 종단 JSON과 동일한 스키마(미평가 시 `cross_evaluated=false`, `minimum_cross_provider_cosine=null`)로 영구 기록.

### 2.2 [P2] 일반 배치(`startup_only=False`) 실행 결과 품질 상태 보존 (`cli.py:1700–1715`)

- `_run(resume, startup_only=False)` 반환부에서 `_pipeline_result_payload(result)` 및 `backup_info`에 더해 `**_project_embedding_quality_payload(embeddings)`를 병합.
- 이로써 warning 상태로 preflight를 통과한 뒤 파이프라인이 정상 완료(`succeeded`)되거나 변경사항 없이 종료(`no_change`)된 일반 일배치에서도:
  - `status`: `"succeeded"` 또는 `"no_change"`
  - `quality_status`: `"warning"`
  - `quality_details`: 검증된 허용 스키마 dict
  - `preflight_mode`: `"daily_smoke"`
  - `evaluated_provider_count`: 1
  - `cross_evaluated`: `false`
  - `minimum_cross_provider_cosine`: `null`
  이 배치의 수집 상태/백업 상태와 분리된 독립 필드로 종단 JSON에 온전히 보존됩니다.
- 정상 통과(`quality_status="passed"`)인 배치에서는 `"quality_details"` 필드가 포함되지 않으며, `minimum_cross_provider_cosine`은 미측정 상태(`null`)로 일관되게 출력됩니다.

---

## 3. 테스트 및 검증 결과

### 3.1 회귀 테스트 추가 (`test_cli_settings_provider.py`, `test_embedding_v5.py`)

1. **`test_cli_quality_details_output_boundary_sanitization_and_tampering`:**
   - mock warning report에 `unexpected_private_field="synthetic-private-marker"`, `auth_token="secret-leak-marker"`, `minimum_cosine=NaN`, `mean_cosine=Inf`, 잘못된 길이의 sha 해시 주입.
   - `_run(None, startup_only=True)` 호출 후 최종 JSON 및 `startup-attempt-history.jsonl`에서 비허용 키와 NaN/Inf가 모두 제거되고 정상 필드만 보존됨을 검증.
   - Provider 생성 후 사후 주입 변조(`post_construction_leak`) 및 알 수 없는 `quality_status` enum 주입 시 `passed`로 안전 격하됨을 검증.
2. **`test_cli_normal_batch_preserves_quality_status_and_preflight_metadata`:**
   - pipeline status가 `succeeded` 및 `no_change`인 일반 배치(`startup_only=False`)에서 `quality_status="warning"`, `quality_details`, `cross_evaluated=False`, `minimum_cross_provider_cosine=None`이 종단 JSON에 보존됨을 검증.
   - JSON 직렬화/역직렬화 round-trip에서 `minimum_cross_provider_cosine`이 JSON `null`로 올바르게 표현됨을 확인.
3. **`test_cli_normal_batch_passed_quality_has_no_quality_details`:**
   - 정상 preflight 통과 일반 배치에서 `quality_status="passed"`, `quality_details` 누락, `cross_evaluated=False`, `minimum_cross_provider_cosine=None` 유지 확인.
4. **`test_openrouter_provider_defensively_copies_and_validates_quality_details_and_metadata`:**
   - Provider 생성자 레벨에서의 allowlist 필터링, 방어적 복사(인풋 dict 사후 수정 격리), 메타데이터 수용 단위 테스트.
5. **테스트 격리 보호 조치:**
   - `_configure_worker_logging` 실행 시 `cardrag_worker` logger의 `propagate = False` 설정으로 인해 이후 caplog를 사용하는 전체 테스트들이 영향을 받지 않도록, `test_cli_settings_provider.py`의 신규 테스트들에서 `logger.propagate` 복원 및 로깅 mock을 완비.

### 3.2 검증 명령어 실행 결과

- **Worker 전체 단위/통합 테스트:**
  ```bash
  uv run --no-sync pytest apps/cardrag-worker/tests/ -q
  # 결과: 1313 passed, 10 warnings in 43.75s
  ```
  (10 warnings는 의도된 fallback/cache conflict 테스트 경로이며 실패 0건)
- **Linter 및 Formatter 검사:**
  ```bash
  uv run --no-sync ruff check apps/cardrag-worker/
  # 결과: All checks passed!
  uv run --no-sync ruff format --check apps/cardrag-worker/
  # 결과: 116 files already formatted
  ```
- **Git diff 공백/포맷 검사:**
  ```bash
  git diff --check
  # 결과: clean (출력 없음, exit code 0)
  ```

---

## 4. 운영 정책 및 배포 유의사항 재확인

- **신규 품질 정책 운영 미승인 유지:**
  - 0.990 (hard reject) / 0.999 (warning) 임계값 정책은 gold/old-cache/search calibration 평가 근거가 확립되기 전까지 운영에 배포하지 않습니다.
  - 현재 실행 중인 프로덕션 컨테이너 이미지 `cardrag-worker:016-bb56d20` 및 MCP 서비스는 일체 수정하지 않고 정상 서비스 상태(`healthy`)를 유지했습니다.
- **저장소 및 파일 무결성:**
  - 기존 handoff 문서(`PLAN.md`, `REPORT.md`, `FIX_01~03`, `STORAGE_REVIEW_20261016.md`)를 보존하고 본 `FIX_04_REPORT.md`만을 신규 생성했습니다.
  - VM 저장소 9.02GB 회수 상태 및 패들 OCR 의존성 분리 기록은 `STORAGE_REVIEW_20261016.md`에 유지됩니다.
