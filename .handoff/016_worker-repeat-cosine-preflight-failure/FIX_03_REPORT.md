# 016 FIX_03_REPORT — FIX_03 수정 요구사항 구현 및 정합성 검증 보고서

- **작성 일시:** 2026-10-16
- **작성 역할:** Executor
- **대상 검토 문서:** `.handoff/016_worker-repeat-cosine-preflight-failure/FIX_03.md`, `STORAGE_REVIEW_20261016.md`, `docs/OPERATIONS.md`
- **검증 환경:** HEAD `1764aef` + working tree diff, Python 3.12.14, uv, pytest-8.4.2

---

## 1. 개요 및 요약

FIX_03에서 Reviewer가 지적한 핵심 결함 4건과 저장소 자원 판정을 전면 수용하고 구현·검증을 완료했습니다.

1. **[P1] 임계치 성격 정직화 (Calibration 분리 및 정책 후보 명시):** 0.990 hard cutoff 및 0.980 semantic collapse는 업무 gold 셋 기반 nDCG@10 / Recall@10 calibration이 아직 수행되지 않은 **운영 관측 기반의 후보 정책 (미인수 / uncalibrated candidate)**으로 명확히 규정했습니다. 평가 완료 전까지 운영 Compose는 `016-bb56d20` 직전 검증 코드를 유지합니다.
2. **[P1] 의미 축퇴 방어 Production 경로 연결:** `preflight_openrouter_qwen_providers`의 `check_semantic_discriminability` 기본값을 `True`로 변경하고 CLI startup 경로(`cli.py:_qwen_embedding_provider`)에 명시 연결했습니다. Reviewer의 상수 더미 벡터 MockTransport 재현 시나리오를 CLI 기동 회귀 테스트로 작성하여 fail-closed 차단(`embedding_semantic_collapsed`)을 검증했습니다.
3. **[P2] 단일 프로바이더 미측정 cross 값 정합화 및 Warning 종단 영속화:** 단일 프로바이더 smoke 검사 시 `minimum_cross_provider_cosine` 및 `mean_cross_provider_cosine`을 허위 1.0 대신 `None`으로 반환하고, CLI 로그에는 `not_evaluated`, JSON에는 `null`로 기록합니다. 경고 구간 [0.990, 0.999) 진입 시 위반 샘플 인덱스·SHA256 해시·수치를 포함하는 구조화된 `quality_details`가 `startup-attempt-history.jsonl` 및 CLI ready JSON에 종단 보존됩니다.
4. **[P2] 롯데카드 미사용 helper 제거 및 범위 정정:** pipeline에 연결되지 않고 실효성이 없던 dead helper `revalidate_degraded_sources`를 완전히 제거했습니다. `www.lottecard.co.kr` 목록 단계의 TCP 타임아웃은 호스트/네트워크 egress 장애로 미복구 상태임을 정직하게 명시하고, 파이프라인의 기존 `_carry_failed_issuers` 메커니즘을 통해 직전 세대(`ec1255d9`) 647개 문서를 승계하고 단종을 동결하는 운영 보호 방식을 문서화했습니다.
5. **저장소 실사 및 회수 확인:** Reviewer의 qcow2 무손실 압축으로 9.02 GB를 안전하게 회수하여 가용 디스크 127.36 GB (사용률 69%)가 확보되었음을 확인했습니다.

---

## 2. 상세 구현 내용

### 2.1 [P1] 0.990 / 0.980 임계치의 정책 성격 규정 (`docs/OPERATIONS.md`)

- **도입 배경과 관측 근거:**
  - 2026-10-16 일일 배치 기동 중 DeepInfra 프로바이더의 첫 번째 샘플에서 0.9972 repeat cosine이 관측되어 기존 단일 0.999 fail-closed 게이트에 차단됨.
  - Upstream 호스팅의 BF16/FP16 연산 비결정성에 의한 미세 drift를 허용하기 위해 일일 smoke에 경고 밴드 [0.990, 0.999)를 두고, 0.990 미만은 fail-closed 하드 거부로 설정함.
  - 정적 더미 벡터 반환으로 인한 거짓 repeat=1.0 통과를 방어하기 위해 이종 문장 간 유사도 상한 0.980을 도입함.
- **Calibration 현황 및 미인수 명시:**
  - 본 임계치들은 합성 테스트 및 운영 관측치에 기반한 **정책 후보 (Candidate Policy)**이며, 실제 구 캐시 임베딩과 신규 임베딩 간의 유사도 분포 실측, 그리고 업무 gold 쿼리/문서 셋에 대한 nDCG@10, Recall@10, MRR@10 평가를 거친 공인 품질 임계치가 아님을 명시했습니다.
  - `docs/OPERATIONS.md` 및 본 보고서에 정식 calibration 완료 전까지 본 정책을 정규 품질 합격으로 확정 발표하지 않으며, 운영 Compose는 `016-bb56d20`을 유지함을 명시했습니다.

### 2.2 [P1] 의미 축퇴 방어 Production 경로 통합 및 회귀 검증

- **`embedding_v5.py`:**
  - `preflight_openrouter_qwen_providers` 매개변수 `check_semantic_discriminability: bool = True` (기본 활성화).
  - `semantic_collapse_threshold: float = SEMANTIC_COLLAPSE_THRESHOLD` (0.980) 전달.
- **`cli.py`:**
  - `_qwen_embedding_provider`에서 `preflight_openrouter_qwen_providers` 호출 시 `check_semantic_discriminability=True`를 명시 전달.
  - 이종 샘플(혜택 문장 vs 제외 문장)의 cosine 유사도가 0.980 이상(상수 벡터의 경우 1.0)이면 `EmbeddingV5Error(reason_code="embedding_semantic_collapsed")` 발생.
  - `_classify_startup_exception`을 통해 category `"contract"`, `failure_reason_code="embedding_semantic_collapsed"`, `quality_details`(`cross_sample_cosine`, `sample_count`, `gate_threshold`)로 안전하게 격리 분류.
- **회귀 테스트 (`test_cli_settings_provider.py:test_cli_startup_rejects_constant_dummy_vector_collapse`):**
  - 모든 문장에 동일한 단위 벡터 `[1.0, 0, 0, ...]`를 반환하는 MockTransport를 주입하고 CLI 기동 경로(`_qwen_embedding_provider`)를 직접 실행.
  - `WorkerStartupFailure` (stage `"qwen_endpoint_preflight"`, reason `"embedding_semantic_collapsed"`)로 fail-closed 거부됨을 확인.
  - `startup-attempt-history.jsonl`에 `status="terminal"`, `reason_code="embedding_semantic_collapsed"`, `quality_details={"cross_sample_cosine": 1.0, ...}`가 누출 없이 영속 기록됨을 검증.

### 2.3 [P2] 미측정 Cross Cosine의 Explicit Null 표기 및 Warning 종단 이력

- **`ProviderComparisonReport` 정합화:**
  - 단일 프로바이더 smoke 모드에서 cross cosine을 허위 `1.0`으로 채우지 않고, `minimum_cross_provider_cosine: float | None = None`, `mean_cross_provider_cosine: float | None = None`으로 반환.
  - `cross_evaluated: bool = False`, `mode: str`, `evaluated_provider_count: int` 명시.
- **CLI 로깅 및 Ready JSON:**
  - `cli.py`: `minimum_cross_provider_cosine`이 `None`일 때 로그에 `minimum_cross_provider_cosine=not_evaluated`로 출력.
  - `startup-only` CLI 실행 완료 시 반환되는 summary JSON에 `"quality_status": "warning"`, `"quality_details": {...}`가 포함되며, readiness(`"status": "ready"`)와 품질 상태(`"quality_status"`)를 독립 분리.
- **Warning 이력 영속화 (`StartupAttemptRecorder`):**
  - `StartupAttemptRecorder.record`에 `quality_status` 매개변수를 추가하여 stage pass 시에도 warning 상태를 영속화.
  - `validated_quality_details("repeat_cosine_warning", ...)` 화이트리스트 스키마를 통해 검증된 딕셔너리만 `startup-attempt-history.jsonl`에 기록.
  - `test_cli_settings_provider.py:test_cli_startup_records_warning_details_and_not_evaluated_cross_cosine` 회귀 테스트로 검증 완료.

### 2.4 [P2] 롯데카드 Dead Helper 제거 및 범위 정정

- **`issuers/lotte.py`:**
  - 프로덕션 파이프라인에서 참조되지 않던 `revalidate_degraded_sources` 메소드와 관련 테스트(`test_lotte_revalidate_degraded_sources_cdn`)를 완전히 제거.
  - 원본 `lotte.py`를 깨끗한 베이스라인 상태로 복원.
- **실효성 분석 및 운영 보호:**
  - 롯데카드 목록 조회(`www.lottecard.co.kr`)의 TCP connect timeout(exit 28, `httpx.ConnectTimeout`)은 호스트 네트워크 egress 또는 원본 방화벽/가용성 경계로 인한 장애임.
  - CardRAG 파이프라인은 이미 `_carry_failed_issuers`를 통해 직전 세대(`ec1255d9`) 647개 유효 문서를 캐시에서 그대로 승계하고 단종을 동결하므로, 647개 URL을 네트워크로 HEAD 조회하는 것은 불필요한 라운드트립과 오탐 위험만 가중시킴.
  - `test_issuers_lotte_bc.py`에서 landing과 listing의 connect timeout이 각각 `issuer_origin_timeout`으로 정확하게 분류됨을 확인.

---

## 3. 검증 결과

### 3.1 정적 검사 및 diff 무결성

- `git diff --check`: **0 errors** (docs/OPERATIONS.md의 trailing blank line 해결).
- `uv run ruff check apps/cardrag-worker/`: **All checks passed!**
- `uv run ruff format --check apps/cardrag-worker/`: **116 files already formatted.**

### 3.2 집중 회귀 테스트 (4개 파일)

```bash
uv run pytest apps/cardrag-worker/tests/test_embedding_v5.py \
              apps/cardrag-worker/tests/test_cli_settings_provider.py \
              apps/cardrag-worker/tests/test_issuers_lotte_bc.py \
              apps/cardrag-worker/tests/test_partial_execution.py
```
- **결과:** **293 passed in 22.04s** (모든 신규 회귀 테스트 포함 전원 통과).

### 3.3 전체 테스트 스위트

```bash
uv run pytest apps/cardrag-worker/tests/
```
- **결과:** **1308 passed, 10 warnings in 44.54s** (0 regressions).

---

## 4. 변경 파일 목록 및 상태

| 파일 | 변경 내역 |
|---|---|
| `apps/cardrag-worker/src/cardrag_worker/embedding_v5.py` | `ProviderComparisonReport` null cross cosine 지원, warning_details 추가, semantic collapse 기본 활성화(True), validated_quality_details 연동 |
| `apps/cardrag-worker/src/cardrag_worker/cli.py` | preflight semantic 검증 명시 전달, `StartupAttemptRecorder` quality_status/details 영속화, not_evaluated cross cosine 로그, startup-only JSON 품질 필드 연동 |
| `apps/cardrag-worker/tests/test_embedding_v5.py` | 샘플별 독립 벡터 모의, null cross cosine 및 warning_details 검증, 더미 상수 벡터 의미 축퇴 fail-closed 테스트 |
| `apps/cardrag-worker/tests/test_cli_settings_provider.py` | CLI 시작 경로를 통한 상수 더미 벡터 거절 회귀 테스트 1건, warning details 영속화 및 not_evaluated 로그 회귀 테스트 1건 추가 |
| `apps/cardrag-worker/tests/test_issuers_lotte_bc.py` | 미사용 degraded helper 테스트 제거, landing/listing 타임아웃 분류 테스트 유지 |
| `apps/cardrag-worker/tests/test_partial_execution.py` | FIX_01에서 추가된 intermediate structure skip fail-closed 회귀 테스트 유지 |
| `docs/OPERATIONS.md` | 임계치 정책 후보(미인수) 성격 명시, 롯데 degraded carryover 보호 범위 정정, EOF blank line 제거 |

---

## 5. 결론 및 다음 단계

- FIX_03의 모든 요구사항([P1] 2건, [P2] 2건, 저장소 실사 확인, 정적 검사 무결성)을 완벽히 이행했습니다.
- 본 작업은 저장소 작업 트리의 미커밋 상태로 안전하게 보존되어 있으며, Reviewer의 최종 검토를 준비합니다.
