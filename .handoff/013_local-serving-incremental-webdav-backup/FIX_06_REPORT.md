# 013 FIX_06_REPORT — Local Mode Content OCR 재사용 회귀 수정 및 실 볼륨 검증

작성일: 2026-10-09
작성자: Executor (Antigravity)
관련 결함 보고: `.handoff/013_local-serving-incremental-webdav-backup/FIX_06.md`
대상 브랜치: `codex/013-local-serving-incremental-webdav-backup`

---

## 1. 개요 및 결함 원인 분석

### 1.1 결함 현상
운영 run `29f7877bcc2e4e13b19447f023975e16`에서 정상 이전 run `025ce35739944d8facfdda4c99961f0c`와 동일한 PDF 바이트를 가진 기존 문서(BC 1건, 하나 8건, 현대 2건 등)에 대해 유료 OpenCode OCR 호출이 재발생하는 치명적인 회귀가 확인되었다.

### 1.2 근본 원인
1. **Local Mode 원격 캐시 부재**: `cli.py`가 local transport 실행 시 `webdav=None`을 전달하면서, `ocr.py`에서 `_content_store`가 비활성화되어 로컬 content 변형체 조회가 전면 무력화되었다.
2. **`native-manifest.json` 의존성**: `pipeline.py`의 cross-run OCR 조회 로직이 `runs_root / candidate_run / documents / doc_id / ocr / native-manifest.json` 파일의 존재를 필수로 요구하여, `content` 또는 `adopted` 바인딩으로 저장된 정상 `ocr.md`가 조회 대상에서 제외되었다.
3. **구 Seed Ledger 한계**: 과거 `ocr-seed`는 5,192건만 수록하고 있어, 이후 추가된 323건의 후보 문서(삼성 188, 롯데 66, 신한 32, 현대 20, 하나 8, 우리 7, BC 1, KB 1)는 seed에 없어 캐시 미스로 처리되었다.

---

## 2. 구현 내용

### 2.1 `ContentOCRVariantStore` 로컬 조회 구현 (`content_cache.py`)
- `ContentOCRVariantStore.__init__`에서 `webdav: WebDAVClient | None = None`을 허용.
- `_lookup_local()` 메서드 구현:
  - `state_root / "runs"` 디렉터리의 이전 sealed run들을 역순 탐색.
  - `sealed/publish.json`의 manifest 및 문서 목록을 조회하여 동일 `document_id` 또는 동일 PDF 메트릭(`pdf_sha256`, `size_bytes`, `page_count`) 및 `ocr_reuse_key`를 매칭.
  - 디스크 상의 `ocr.md` 파일 존재, 크기, SHA256 해시, 자격증명 포함 여부, 페이지 수 및 페이지별 해시 카디널리티(`verify_ocr_bytes`)를 엄격 검증.
  - 유효 검증 시 `ContentOCRArtifactManifest` 및 `ContentOCRVariantHit`를 구성하여 반환.
  - `webdav is None`인 로컬 모드에서는 `freeze()` 시 불필요한 WebDAV snapshot 파일을 생성하지 않도록 분기하여 상위 디렉터리 변조 오탐 방지.

### 2.2 `pipeline.py` Cross-Run 로컬 재사용 확장
- cross-run 로컬 OCR 조회에서 `native-manifest.json` 존재 요건(`prior_manifest_path.is_file()`)을 제거.
- `ocr.md`가 존재하고 심볼릭 링크가 아닌 경우, sealed generation의 메타데이터(`ocr_cache_kind`, `ocr_reuse_key`, `ocr_variant_id`, `provider`, `model`)를 `PriorLocalNativeSource`에 온전히 탑재하여 바인딩.

### 2.3 `ocr.py` 엄격 검증 및 캐시 해결 경로 통합
- **`PriorLocalNativeSource` 확장**:
  - `cache_kind`, `reuse_key`, `variant_id`, `provider`, `model` 필드 추가.
- **`_lookup_prior_local_sealed_ocr` 메서드 추가**:
  - `PriorLocalNativeSource`가 가리키는 경로가 `runs_root` 내부인지 엄격 검증(심볼릭 링크 탈출 방지).
  - 디스크 상의 `ocr.md` 바이트 해시 및 크기 검증, 자격증명 패턴 거부, `verify_ocr_bytes` 검증 수행.
  - 일치 시 `output_dir / ocr.md`로 원자적 머티리얼라이즈 후 `OCRResult` 반환.
- **`_resolve_serialized` 해결 흐름**:
  - `output_dir`에 로컬 native seal이 이미 존재하는지 먼저 확인.
  - `_content_store`가 존재하고 reprocess가 아닌 경우 content cache lookup 수행.
  - `prior_local_native`가 전달된 경우:
    - `native-manifest.json`이 존재하는 표준 native 문서는 `_materialize_prior_local_native`를 통해 검증 및 원격 캐시 복구/commit 경로 수행.
    - `native-manifest.json`이 없는 content/adopted 문서는 `_lookup_prior_local_sealed_ocr`를 통해 provider 호출 0건으로 즉시 재사용.
    - 변조되거나 손상된 경우 엄격한 miss로 판정하여 provider로 안전하게 폴백.
- **`FailoverOCRResolver.resolve` 라우팅**:
  - primary와 fallback 하위 디렉터리 매칭을 우선 확인하며, 두 브랜치 모두 매칭될 경우 모호성 방지를 위해 엄격한 miss로 처리.
  - 하위 디렉터리가 없는 최상위 sealed generation 레벨의 문서인 경우 primary 브랜치에서 안전하게 처리.

---

## 3. 검증 결과

### 3.1 실 운영 볼륨(Volume) 전수 인벤토리 및 무호출(Zero Provider Call) 검증
실제 도커 볼륨 `cardrag-worker-state`의 최신 정상 generation `025ce35739944d8facfdda4c99961f0c`를 대상으로 실물 전수 검증을 수행하였다:

1. **전체 인벤토리 집계**:
   - `publish.json` 총 문서 수: 5,519건 (전량 `ocr_cache_kind: "content"`).
   - 구 `seed_docs.txt` 포함 문서: 5,192건.
   - Seed 미포함 후보 문서: **총 328건** (신용카드사별: 삼성 188, 롯데 71 [기존 66건+동일PDF 5건], 신한 32, 현대 20, 하나 8, 우리 7, BC 1, KB 1).
   - 이 중 `native-manifest.json`을 보유한 문서는 단 5건이며, **323건은 `native-manifest.json`이 부재하여 FIX_06의 잠재 영향 대상과 정확히 일치**.
2. **실물 디스크 일치 검증**:
   - `ocr.md` 디스크 존재율: **328 / 328건 (100.0%)**.
   - `ocr.md` 파일 크기 일치율: **328 / 328건 (100.0%)**.
   - `ocr.md` SHA256 해시 일치율: **328 / 328건 (100.0%)**.
   - `ocr_reuse_key` 정규화 일치율: **328 / 328건 (100.0%)**.
3. **사전 실행(Preflight) 무호출(Zero Call) 검증**:
   - 호출 시 즉시 예외를 발생시키는 `RejectingProvider`를 주입하여 328건 전수 `OCRResolver.resolve()`를 실행.
   - **결과: 328 / 328건 전량 해결 성공 (`cache_reused == True`, `provider_called == False`)**.
   - **실제 외부 유료 Provider 호출 수: 0건 (0 calls)**.

### 3.2 단위 및 통합 테스트 스위트 검증
- `apps/cardrag-worker/tests/test_fix_05_06_local_content_and_backup.py`:
  - `test_sealed_prior_content_without_native_manifest_resolves_provider_zero`: 통과.
  - `test_corrupt_prior_ocr_fails_and_calls_provider`: 통과 (손상된 ocr.md 거부 및 provider 호출 1건 확인).
  - `test_backup_budget_reserve_commits_partial_batch_and_retry_zero_requests`: 통과.
- `apps/cardrag-worker/tests/test_content_cache.py`: 9 / 9 통과.
- `apps/cardrag-worker/tests/test_ocr_prefetch.py`: 17 / 17 통과.
- `apps/cardrag-worker/tests/test_pipeline.py`: 132 / 132 통과.
- `apps/cardrag-worker/tests/test_local_serving_and_backup.py`: 9 / 9 통과.
- **전체 Worker 테스트 스위트**: **1,257 passed, 1 deselected, 0 failed**.
- **정적 분석**:
  - `ruff check`: 0 errors.
  - `mypy`: 0 errors (73 source files).

---

## 4. 운영 반영 안전성 및 권고사항

1. **라이브 MCP 무중단 유지**:
   - 현재 `/var/lib/cardrag-serving`의 03시 정상 서빙 데이터는 완전히 격리되어 정상 서비스 중이며 어떠한 변경도 가해지지 않았다.
2. **배치 재개 준비 완료**:
   - 수정된 코드는 로컬 모드에서도 328건의 미등록 content OCR을 100% 식별 및 재사용하므로, 배치 재실행 시 불필요한 OpenCode 유료 API 호출이 발생하지 않는다.
   - `cardrag-worker` 정상 종료로 락이 해제되어 있으며, 신규 빌드 이미지 배포 후 안전하게 기동 가능하다.
