# 013 FIX_08_REPORT — native 5건 provenance 반환 보완

작성일: 2026-10-09  
작성자: Executor (Antigravity)  
관련 결함 보고: `.handoff/013_local-serving-incremental-webdav-backup/FIX_08.md`  
대상 브랜치: `codex/013-local-serving-incremental-webdav-backup`  
기준 커밋: `c34eef0`  

---

## 1. 개요 및 판정 요약

Reviewer의 FIX_08 인수 보완 요구사항에 따라 `_lookup_prior_local_sealed_ocr`에서 알려진 native manifest의 `contract` 필드로부터 `provider` 및 `model`을 안전하게 복원하도록 구현하고 엄격한 실물 검증 및 테스트를 완료하였다:

1. **native manifest의 contract 필드로부터 provenance 복원**:
   - 기존 구현은 `native-manifest.json` 파싱 시 `p_man.get("provenance", {})`를 조회하여, 실제 `OCRArtifactManifest`의 `contract.provider` 및 `contract.model`에 저장된 OpenCode provenance(`opencode`, `alibaba-token-plan/qwen3.8-flash`)를 읽지 못하고 `generation-only` / `unrecorded`로 누락하던 결함을 수정함.
   - `prior_manifest_path`를 `OCRArtifactManifest.model_validate_json()`으로 엄격 검증하며, canonical 바이트 일치, source PDF 일치(`pdf_sha256`, `pdf_size_bytes`, `page_count`), OCR identity 일치(`sha256`, `size_bytes`)를 확인한 뒤 `contract.provider` 및 `contract.model`을 추출하도록 보완함.
   - `prior.provider` 및 `prior.model`이 이미 제공된 경우에는 기존 값을 우선 보존함.
   - source에 정보가 없는 323건 content 자료는 기존과 동일하게 `generation-only` / `unrecorded`를 유지함.

2. **표본 및 전수 검증 보완**:
   - Reviewer가 지적한 표본 오류(seed 제외 기준 선별)를 수용하여, seed 밖의 실제 328건(323건 content + 5건 native)에 대해 Docker `--network none -v cardrag-worker-state:/state:ro` 환경에서 전수 실물 resolver 검증을 수행함.
   - 323건 content: provider 호출 0건, 동일 OCR SHA 100%, 동일 variant 100%, cache hit 100%.
   - 5건 native: provider 호출 0건, 동일 OCR SHA 100%, 동일 variant 100%, 반환 `provider == "opencode"`, `model == "alibaba-token-plan/qwen3.8-flash"`를 엄격 assert하여 100% 통과함.
   - 검증 스크립트(`evidence/executor-fix08-verification.py`) 및 결과(`evidence/executor-fix08-verification.json`)를 신규 보존함 (기존 handoff 문서 및 Reviewer 증적 일체 보존).

3. **테스트 및 타입 검증**:
   - 단위/회귀 테스트 추가: `test_sealed_prior_native_manifest_recovers_contract_provenance_provider_zero`
   - 지정 5파일 pytest: **53 passed, 1 warning in 3.24s**
   - 전체 테스트 스위트: **2,146 passed, 10 warnings in 62.31s** (0 deselected, 0 failed)
   - mypy (4파일 및 전체 108파일): **Success: no issues found**
   - ruff check: **All checks passed!**

---

## 2. 세부 변경 사항

### 2.1 `apps/cardrag-worker/src/cardrag_worker/ocr.py`
- `_lookup_prior_local_sealed_ocr`:
  - `(provider is None or model is None)`이고 `prior_manifest_path.is_file()`일 때,
  - `OCRArtifactManifest.model_validate_json(manifest_bytes)`를 통해 정식 스키마 및 canonical bytes를 검증하고,
  - source PDF(`pdf_sha256`, `pdf_size_bytes`, `page_count`)와 OCR 산출물(`sha256`, `size_bytes`) identity가 일치할 경우 `manifest.contract.provider` 및 `manifest.contract.model`을 할당함.
  - JSON 형식 불일치 시 fallback dictionary 파싱에서도 `contract` / `provenance`를 안전하게 조회하도록 방어 조치.

### 2.2 `apps/cardrag-worker/tests/test_fix_05_06_local_content_and_backup.py`
- 신규 회귀 테스트 추가: `test_sealed_prior_native_manifest_recovers_contract_provenance_provider_zero`
  - pipeline과 같이 `prior.provider=None`, `prior.model=None`인 상태에서 `native-manifest.json`이 존재할 때,
  - provider 호출 0건(`provider_called is False`, `provider.calls == []`), cache hit, 동일 variant 유지와 함께
  - `result.provider == "opencode"`, `result.model == "alibaba-token-plan/qwen3.8-flash"`가 반환됨을 검증.

---

## 3. 검증 결과

### 3.1 Docker 실물 볼륨 Read-only 전수 검증 (328건)
실행 명령:
```sh
docker run --rm --network none --user 0:0 \
  -v cardrag-worker-state:/state:ro \
  -v /home/lee/projects/MCP_card_prd_detail:/workspace:ro \
  --entrypoint python cardrag-worker:013-3eaa35d \
  /workspace/.handoff/013_local-serving-incremental-webdav-backup/evidence/executor-fix08-verification.py \
  > .handoff/013_local-serving-incremental-webdav-backup/evidence/executor-fix08-verification.json
```

결과 요약 (`executor-fix08-verification.json`):
```json
{
  "count": 328,
  "content_count": 323,
  "native_count": 5,
  "calls": 0,
  "hits": 328,
  "same_ocr": 328,
  "variant_matches": 328,
  "exceptions": [],
  "native_results": [
    {
      "document_id": "doc_0e4b7f9e9f9f5d26269563bd9ed9846445946be7c8d2fac928004c71f111900e",
      "native": true,
      "cache_hit": true,
      "provider_called": false,
      "same_ocr": true,
      "same_variant": true,
      "cache_kind": "content",
      "provider": "opencode",
      "model": "alibaba-token-plan/qwen3.8-flash"
    },
    {
      "document_id": "doc_36ccc79a452843549c896126b620d293fdcee1ced9c19d0eb5848a7b29a8a56c",
      "native": true,
      "cache_hit": true,
      "provider_called": false,
      "same_ocr": true,
      "same_variant": true,
      "cache_kind": "content",
      "provider": "opencode",
      "model": "alibaba-token-plan/qwen3.8-flash"
    },
    {
      "document_id": "doc_55086e8736bd18279b84736878c27863731a3f3853fbf5236f77aeb7241ebd49",
      "native": true,
      "cache_hit": true,
      "provider_called": false,
      "same_ocr": true,
      "same_variant": true,
      "cache_kind": "content",
      "provider": "opencode",
      "model": "alibaba-token-plan/qwen3.8-flash"
    },
    {
      "document_id": "doc_55f60361cf7cda5cbfcca403e66191ddb09c2843f8a1035c008f30a76909554f",
      "native": true,
      "cache_hit": true,
      "provider_called": false,
      "same_ocr": true,
      "same_variant": true,
      "cache_kind": "content",
      "provider": "opencode",
      "model": "alibaba-token-plan/qwen3.8-flash"
    },
    {
      "document_id": "doc_eb9a7a10463c2e72501d8e8d7d028cd7c94c7a63c05e29d4d1c3500b3de8c733",
      "native": true,
      "cache_hit": true,
      "provider_called": false,
      "same_ocr": true,
      "same_variant": true,
      "cache_kind": "content",
      "provider": "opencode",
      "model": "alibaba-token-plan/qwen3.8-flash"
    }
  ],
  "content_variant_mismatches": []
}
```

- 5건 native 모두: `provider == "opencode"`, `model == "alibaba-token-plan/qwen3.8-flash"` 정확 반환 확인.
- 323건 content: provider 호출 0건, variant 불일치 0건.

### 3.2 지정 5파일 pytest
```sh
uv run --no-sync --all-packages pytest \
  apps/cardrag-worker/tests/test_fix_05_06_local_content_and_backup.py \
  apps/cardrag-worker/tests/test_content_cache.py \
  apps/cardrag-worker/tests/test_ocr_prefetch.py \
  apps/cardrag-worker/tests/test_pipeline_v5.py \
  apps/cardrag-worker/tests/test_local_serving_and_backup.py -q
```
**결과:** `53 passed, 1 warning in 3.24s`

### 3.3 전체 테스트 스위트
```sh
uv run --no-sync --all-packages pytest apps/cardrag-worker/tests apps/cardrag-mcp/tests -q
```
**결과:** `2146 passed, 10 warnings in 62.31s` (0 deselected, 0 failed)

### 3.4 정적 분석
- mypy (4파일): `uv run --no-sync --all-packages mypy apps/cardrag-worker/src/cardrag_worker/content_cache.py apps/cardrag-worker/src/cardrag_worker/ocr.py apps/cardrag-worker/src/cardrag_worker/backup.py apps/cardrag-worker/src/cardrag_worker/pipeline.py`
  - `Success: no issues found in 4 source files`
- mypy (전체): `uv run --no-sync --all-packages mypy packages/cardrag-core/src apps/cardrag-worker/src apps/cardrag-mcp/src`
  - `Success: no issues found in 108 source files`
- ruff: `uv run --no-sync --all-packages ruff check apps/ packages/`
  - `All checks passed!`

---

## 4. 운영 상태 및 격리 확인

- 유료 API / 외부 OCR 호출: 0건.
- 백업 및 Worker: 정지 상태 유지, 재기동하지 않음.
- 운영 서빙 경로(`/var/lib/cardrag-serving`): 변경 없음.
- Docker 볼륨 `cardrag-worker-state`: `:ro` (read-only) 마운트로만 접근하여 운영 데이터 보존.
- 기존 handoff 문서(`PLAN.md`, `REPORT.md`, `FIX_01`~`FIX_07`, `FIX_01_REPORT`~`FIX_07_REPORT`) 및 증적 파일 보존.
