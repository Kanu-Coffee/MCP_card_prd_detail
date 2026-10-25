# 016 FIX_02 REPORT — 롯데 수집 격리·호스트 용량 보호·임베딩 품질 gate 재설계 및 복원 범위 정정

- 작성일시: 2026-10-16 09:44 KST
- 역할: Executor (Antigravity)
- 참조 문서: `.handoff/016_worker-repeat-cosine-preflight-failure/FIX_02.md`, `FIX_01.md`, `REPORT.md`, `PLAN.md`
- 구현 대상 커밋 베이스: `1764aef` (HEAD)

---

## 1. 개요 및 요약

Reviewer의 `FIX_02.md` 요청 사항에 따라 아래 4가지 영역에 대한 구현 및 검증을 완료했습니다:

1. **[P1] 롯데카드 수집 경로 분리 및 장애 격리**:
   - `LotteAdapter`의 요청 단계를 세분화하고 예외 처리 명확화: landing GET, listing POST, JSON parse, pagination, attachment fetch.
   - 호스트 환경에서 `www.lottecard.co.kr` (landing 및 listing POST)에 대한 TCP connect timeout(exit 28, `httpx.ConnectTimeout`) 발생 확인. 이는 파싱 오류가 아닌 origin network timeout(`issuer_origin_timeout`)으로 정확히 분류됨.
   - 공식 PDF CDN (`image.lottecard.co.kr`)의 안정적 200 OK 응답 및 PDF 바이트 유효성 확인.
   - 목록 조회 장애 시 기존 647개 문서를 carryover하고 단종(retirement) 처리를 동결하는 기존 파이프라인 보호 유지 및 degraded CDN 재검증 경로(`revalidate_degraded_sources`) 구현.
2. **[P1/P2] 전체 호스트 용량 관리 및 볼륨 보호 가이드라인**:
   - Reviewer가 수행한 `docker builder prune --all --force` 조치로 36.99 GB (fs 기준 ~33.97 GB, 84.4 GB → 118.4 GB, 사용률 79% → 71%) 안전 회수 확인.
   - Docker CLI에서 dangling으로 보이는 Worker state(45.89 GB) 및 Paddle 모델(2.07 GB), auth 볼륨, 등록된 HAOS VM(21 GB)은 핵심 영구 운영 자산이므로 `docker volume prune` 절대 금지 지침을 `docs/OPERATIONS.md`에 명시.
3. **[P1] 0.999 repeat cosine 하드 게이트 재설계**:
   - **일일 운영 smoke**와 **배포 자격평가(qualification)** 분리: 일일 배치는 활성 provider(`settings.embedding_provider_id`)만 단독 검사하여 비활성 provider 변동으로 인한 배치 차단을 차단.
   - 단일 scalar 0.999를 **재현성 경고(warning) 기준**으로 전환: [0.990, 0.999) 구간 관측 시 `quality_status="warning"` 기록 및 경고 로그 발생, Worker 시작 허용.
   - 0.990 미만 또는 non-finite, 비정상 차원에 대해 `repeat_cosine_below_gate` fail-closed 하드 게이트 유지.
   - 정적 상수 더미 벡터 반환을 차단하기 위한 의미 축퇴 검증(`SEMANTIC_COLLAPSE_THRESHOLD = 0.980`, `embedding_semantic_collapsed`) 구현.
4. **[P2] 복원 범위 정정 (structure/views partial reuse 한계 명시)**:
   - 복원된 run `ec1255d9`는 publication seal 및 서빙 산출물(DB, vectors, raw PDF/OCR)의 무결성은 완전히 복원되었으나, unsealed intermediate 디렉터리(`documents/<id>/structure/structure.v2.json`, `views.v1.json`)는 보존 대상이 아니었음.
   - 따라서 해당 run을 source로 `--skip structure`를 시도하면 preflight 단계에서 `PartialExecutionError("skip_artifact_missing", "structure")`로 fail-closed 방어됨을 확인하고 관련 회귀 테스트를 작성.

---

## 2. 세부 구현 내용

### 2.1 롯데카드 수집 경로 분리 및 degraded 재검증 (`issuers/lotte.py`)

- **단계 분리 및 예외 분류 보존**:
  - `LotteAdapter.discover_current()`는 landing GET 및 listing POST에서 발생하는 `httpx.ConnectTimeout`을 파서 오류(`IssuerMarkupChanged`)로 덮어쓰지 않고 상위로 전파합니다.
  - `origin_failure(exc)`에 의해 `httpx.ConnectTimeout`은 정확히 `issuer_origin_timeout`으로 분류됩니다.
- **degraded CDN 재검증 메서드 추가**:
  - `revalidate_degraded_sources(client, sources)`: 목록 조회가 불가능한 상태에서 기존 검증된 inventory의 공식 CDN(`image.lottecard.co.kr`) PDF attachment URL을 직접 HEAD/GET probe하여 파일 가용성을 검증합니다.
  - 이를 fresh discovery 성공으로 위장하지 않으며, 목록 장애 시 파이프라인은 `_carry_failed_issuers`를 통해 기존 generation의 647개 문서를 승계하고 단종 처리를 동결합니다.
- **테스트 추가 (`tests/test_issuers_lotte_bc.py`)**:
  - `test_lotte_connect_timeout_on_landing_classifies_as_origin_timeout`: landing 타임아웃 시 `issuer_origin_timeout` 분류 확인.
  - `test_lotte_connect_timeout_on_listing_classifies_as_origin_timeout`: listing 타임아웃 시 `issuer_origin_timeout` 분류 확인.
  - `test_lotte_revalidate_degraded_sources_cdn`: CDN HEAD probe를 통한 200 정상 및 404 제외 동작 확인.

### 2.2 임베딩 품질 게이트 정책 재설계 (`embedding_v5.py`, `cli.py`)

- **상수 및 스키마 추가**:
  - `REPEAT_COSINE_WARNING_THRESHOLD = 0.999`: 재현성 경고 임계치.
  - `REPEAT_COSINE_HARD_REJECT_THRESHOLD = 0.990`: 비정상 수치 오차 및 이상 드리프트 하드 거부 임계치.
  - `SEMANTIC_COLLAPSE_THRESHOLD = 0.980`: 이종 텍스트 간 유사도 비정상 축퇴 탐지 임계치.
  - `QUALITY_DETAIL_SCHEMAS`: `repeat_cosine_warning`, `embedding_semantic_collapsed` 스키마 추가.
- **`ProviderPreflightReport` 및 `ProviderComparisonReport` 확장**:
  - `quality_status`: `"passed"` 또는 `"warning"` (기본 `"passed"`).
  - `warning_count`: 경고 구간에 속한 샘플 수.
  - `cross_provider_cosines`: 단일 provider 검사 시 빈 튜플, cross cosine은 1.0으로 안전 기본값 처리.
- **`preflight_openrouter_qwen_providers` 기능 분리**:
  - `provider_ids`: 평가할 프로바이더 지정 가능 (기본값은 전체 candidate).
  - `mode`: `"qualification"`(기본, 0.999 하드 게이트) 또는 `"daily_smoke"`(0.990 하드 게이트, [0.990, 0.999) 경고 처리).
  - `check_semantic_discriminability`: 이종 의미 샘플 간 유사도가 축퇴되었는지 검증.
- **CLI Startup Check 반영 (`cli.py`)**:
  - 일일 스타트업 검사(`_run_startup_check`) 시:
    ```python
    comparison = await asyncio.wait_for(
        preflight_openrouter_qwen_providers(
            ...,
            provider_ids=(settings.embedding_provider_id,),
            mode="daily_smoke",
            minimum_repeat_cosine=0.999,
            hard_reject_repeat_cosine=0.990,
        ),
        timeout=remaining,
    )
    ```
  - 선택된 프로바이더의 `quality_status == "warning"`인 경우 경고 로그와 함께 `Qwen provider preflight passed ... quality_status=warning`을 기록하고 정상 통과(exit 0).
  - 0.990 미만 또는 의미 축퇴 시 fail-closed 종료.
- **테스트 추가 (`tests/test_embedding_v5.py`)**:
  - `test_preflight_daily_smoke_warning_band_passes_with_warning_status`: 0.9972 변동 시 `quality_status="warning"`으로 통과.
  - `test_preflight_daily_smoke_hard_reject_fails_closed`: 0.985 변동 시 `repeat_cosine_below_gate` 거절.
  - `test_preflight_evaluates_only_specified_provider`: 단일 프로바이더 격리 검증.
  - `test_preflight_detects_semantic_collapse_when_enabled`: 축퇴된 더미 벡터 탐지 거절.

### 2.3 복원 범위 정정 (`partial_execution.py`)

- **구조물 partial reuse 한계 명시**:
  - `ReuseSource.structure`는 source run의 `documents/<id>/structure/structure.v2.json`을 직접 참조합니다.
  - `ec1255d9` 복원 시 publication seal의 산출물(DB, vectors, CAS object PDFs/OCRs)은 복원되었으나 intermediate structure json 파일은 존재하지 않습니다.
  - 따라서 해당 run에 대해 `--skip structure`를 지정하면 `ReuseSource.preflight()`에서 `PartialExecutionError("skip_artifact_missing", "structure")`를 발생시키며 즉시 중단(fail-closed)됩니다.
- **테스트 추가 (`tests/test_partial_execution.py`)**:
  - `test_reuse_source_structure_skip_fails_closed_when_intermediate_missing`: intermediate structure 파일이 부재한 run에 대해 `--skip pdf,ocr`은 통과하지만 `--skip structure`는 사전 preflight에서 안전하게 거절됨을 확인.

### 2.4 운영 문서 갱신 (`docs/OPERATIONS.md`)

- 호스트 전체 디스크 정리와 볼륨 보호 정책(Worker state, Paddle 모델 볼륨 보호, VM disk 보호, `docker builder prune` 활용) 수록.
- Qwen 임베딩 재현성 게이트 정책(daily smoke vs qualification 분리, 0.999 경고 구간, 0.990 하드 거부, 의미 축퇴 검증) 수록.
- 롯데카드 공시실 장애 대응 및 carryover/degraded 모드 가이드 수록.

---

## 3. 검증 결과 및 증거

### 3.1 롯데카드 네트워크 진단 실측

호스트에서 직접 실행한 curl 프로브 결과:
```bash
# www 공식 공시실 landing
$ curl -sI -m 5 https://www.lottecard.co.kr/app/LPCMNPD_V100.lc
curl: (28) Operation timed out after 5001 milliseconds with 0 bytes received

# www 공식 공시실 listing 직접 POST
$ curl -s -m 5 -X POST https://www.lottecard.co.kr/app/LPSCHAA_V100.lc -d "collection=disclosure&listcount=10&startcount=0&query="
curl: (28) Operation timed out after 5001 milliseconds with 0 bytes received

# 공식 이미지 CDN 직접 GET (Driving Pass / LOCA Lite)
$ curl -sI -m 5 https://image.lottecard.co.kr/UploadFiles/cardProvisionPath/P13490-A13490.pdf
HTTP/1.1 200 OK
Content-Type: application/pdf
Content-Length: 975948
Last-Modified: Fri, 22 Apr 2022 08:46:22 GMT
ETag: "ee44c-5dd3a42cb1380"
```
- `www.lottecard.co.kr` 호스트에 대한 TCP SYN 연결 단계에서 타임아웃 발생 (로컬 egress 방화벽 또는 롯데 origin 필터링).
- `image.lottecard.co.kr` CDN은 즉시 200 OK 응답 및 완전한 PDF 바이너리 전송.
- 따라서 코드 변경만으로 fresh inventory를 조회할 수는 없으며, 네트워크 egress/origin 해소 전까지는 기존 inventory carryover 및 degraded 모드로 운영하는 것이 정당함.

### 3.2 테스트 스위트 실행 결과

- **수정 관련 핵심 테스트**:
  ```bash
  uv run pytest apps/cardrag-worker/tests/test_embedding_v5.py \
                apps/cardrag-worker/tests/test_cli_settings_provider.py \
                apps/cardrag-worker/tests/test_issuers_lotte_bc.py \
                apps/cardrag-worker/tests/test_partial_execution.py
  # 결과: 292 passed in 21.55s
  ```
- **전체 Worker 테스트 스위트**:
  ```bash
  uv run pytest apps/cardrag-worker/tests/
  # 결과: 1307 passed, 10 warnings in 43.47s
  ```
- **코드 스타일 및 린트 검증**:
  ```bash
  uv run ruff check apps/cardrag-worker/ && uv run ruff format --check apps/cardrag-worker/
  # 결과: All checks passed! 116 files already formatted.
  ```

---

## 4. 잔여 과제 및 권고사항

1. **롯데카드 네트워크 egress/origin 연결성 해소**:
   - `124.243.36.21:443` (`www.lottecard.co.kr`)로의 아웃바운드 TCP 443 패킷 도달 여부에 대한 네트워크 인프라 점검 필요.
   - 인프라 연결성이 복원되면 Worker는 별도의 코드 수정 없이 자동으로 fresh discovery 전체 페이지 수집으로 복귀합니다.
2. **소규모 anchor gold calibration 유지**:
   - 향후 gold capture 및 retrieval evaluation 파이프라인 가동 시 관측된 cosine drift 분포와 실제 nDCG@10 / Recall@10 검색 품질 영향도를 지속 모니터링하여 임계치를 정밀 교정합니다.
3. **호스트 용량 유지**:
   - 정기적인 `docker builder prune --all --force` 스케줄링을 통해 미사용 BuildKit 캐시를 주기적으로 정리하고, `docker volume prune`은 계속 엄격히 금지합니다.
