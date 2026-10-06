# 007 Executor 중간 보고 — 구현 진행 중, 인수 요청 아님

작성: 2026-10-07 KST. 브랜치 `feat/007-content-ocr-cache`, 기준 commit `2bcd514`. 006의 미커밋 변경은 그대로 보존한 채 브랜치만 새로 만들었다. **007 전체 구현·마이그레이션·운영 전환은 완료되지 않았다.**

## 이번 작업에서 구현한 범위

1. Core의 기존 `native_ocr_reuse_key`는 변경하지 않고, `content_addressed_ocr_reuse_key(source, cache_epoch=0)`를 v2 키로 만들었다. 새 키에는 PDF SHA/크기/쪽수와 epoch만 들어간다.
2. `ContentOCRArtifactManifest`, `ContentOCRReady`, `ContentOCRMigrationSource`, `content` 경로 helper를 추가했다. provenance는 새 manifest에 보존하지만 키에 참여하지 않는다. `GenerationDocument.ocr_cache_kind`는 `content`를 허용하며, 이 종류에는 `ocr_variant_id`를 필수로 해 generation이 정확한 산출물을 가리키게 했다. 기존 manifest에서는 새 선택 필드가 직렬화되지 않아 구 바이트를 보존한다.
3. `WorkerPipeline.contract_sha256`에서 OCR 호출자 계약을 제거하고 `{key_schema, cache_epoch, output_profile, validation_profile}` 정책을 사용하도록 바꿨다. 신규 payload 식별자는 v5 경로에 `cardrag.worker-contract.v5`, 레거시 경로에 `v3`이다. 코어의 구 OCR contract와 구 native 키 해시는 보존된다.
4. `OCRResult` → `_ProcessedDocument` → `GenerationDocument`로 정확한 `cache_variant_id`를 전달하는 필드를 추가했다. 현재 resolver는 아직 content 결과를 생산하지 않는다.

### 설계 세부 결정

PLAN 부록 A의 “manifest 필드 `variant_id` = manifest 전체 SHA-256”은 자기 자신의 해시를 같은 manifest에 넣어야 하는 순환 정의다. 구현은 `variant_id = SHA-256(variant_id 필드만 제외한 정규화 manifest)`로 정의했고, **전체 manifest SHA-256은 별도로** 계산한다. 경로의 12자리 접미사는 전체 manifest SHA-256에서 얻는다. 이 차이를 이후 reader/마이그레이션 구현에도 일관되게 적용해야 한다.

## 수행 검증

- `uv run pytest packages/cardrag-core/tests/test_ocr_manifests.py apps/cardrag-worker/tests/test_ocr.py apps/cardrag-worker/tests/test_pipeline.py -q`: **267 passed**, 기존 예상 warning 9건.
- 필드 전달 변경 전 전체 테스트: `uv run pytest packages/cardrag-core/tests apps/cardrag-worker/tests apps/cardrag-mcp/tests -q`: **2,217 passed**, 기존 예상 warning 9건. 마지막 필드 전달 변경 뒤에는 관련 267개만 재실행했다.
- 변경 영역 Ruff check/format 및 mypy: 통과. `git diff --check`: 통과.
- 읽기 전용 운영 확인: `cardrag-worker.timer` active, `/opt/cardrag/current`는 `/opt/cardrag/v1.0.29`. 운영 설정·state·WebDAV·timer에는 쓰기/재기동을 하지 않았다.

## 남은 단계와 배포 금지 상태

**현재 브랜치는 배포하면 안 된다.** Resolver는 아직 레거시 `native`/`adopted`만 조회·게시한다. 새 계약으로 generation 식별이 달라지는 동안 content cache 적중은 발생하지 않으므로 PLAN의 핵심 수용 기준이 미달이다.

다음 구현 순서는 (1) WebDAV content variant 검증·일괄 인덱스·최신 선택 및 run snapshot, (2) 로컬 content 인덱스·레거시 역조회/승격·새 variant 게시, (3) 재작업/restore 명령과 GC/seed/seal 경로, (4) M0 실데이터 읽기 전용 inventory와 격리 리허설, (5) 전체 검증·운영 불변 재확인이다. 운영 마이그레이션 적용과 이미지 전환은 PLAN 단계 6·7의 별도 승인 범위다. 전체 corpus OCR·Paddle 재처리·운영 WebDAV 쓰기는 이번 작업에서 하지 않았다.
