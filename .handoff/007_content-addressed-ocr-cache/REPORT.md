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

## 2026-10-07 후속 구현 — 원격 content 경로 연결, 계속 진행 중

- 불변 content variant를 CAS → manifest → READY → 평면 discovery index 순서로 게시한다. `v1/ocr-cache/content-index/`를 한 번 열거해 run 로컬 snapshot으로 저장하며, 조회 시 index만 믿지 않고 manifest·READY·CAS·OCR 바이트를 검증해 최신 유효 variant를 고른다. 평면 index는 WebDAV의 문서별 PROPFIND를 피하기 위한 구현 세부 조정이다.
- Resolver가 content hit를 레거시 캐시보다 먼저 조회하고, 새 OCR 결과의 content variant를 게시한다. provider A가 게시한 결과를 provider B가 다음 run에서 OCR 호출 없이 재사용하는 테스트를 추가했다. 정확한 `variant_id`는 generation document까지 전달된다.
- 기존 원격 GC는 content variant와 CAS 참조를 모른다. 완전한 mark 지원 전 사고를 막기 위해 content 경로 또는 index에 항목이 있으면 **삭제 전에 fail-closed**하도록 했다. 운영 원격 GC 승인은 계속 꺼둬야 한다.
- 검증: `test_gc.py`와 `test_content_cache.py` 24 passed. GC guard까지 포함한 전체 테스트 **2,224 passed**, 기존 warning 9건. Ruff check/format, mypy(99 source files), `git diff --check` 통과.

**아직 인수·배포 불가:** run의 index 목록은 고정되지만 문서별 선택 variant ID는 별도 영속화되지 않았다. 로컬 content 인덱스, 모든 레거시 variant 이전/M0, 서비스 중 OCR 텍스트 보존, 재처리·restore, 재처리 시 내용 동일성 판단, content aware GC mark, 운영 read-only inventory와 리허설이 남았다. 이번 후속 작업은 운영 WebDAV나 운영 state에 쓰지 않았다.

### 이어서 적용한 선택 고정

문서별 content hit 선택을 `runs/<run_id>/content-ocr-selections/<document_id>.json`에 원자적으로 기록한다. 재개 시 같은 index·variant ID·OCR 바이트를 재검증하고, 선택된 variant가 훼손되면 다른 변형으로 조용히 바꾸지 않고 중단한다. 이 경로의 재개·훼손 테스트 1건을 추가했다. 위의 “문서별 선택 variant ID는 별도 영속화되지 않았다”는 최초 후속 구현 시점의 상태이며, 이 변경으로 해결했다. 나머지 미완료 항목과 배포 금지는 유지된다.

- 선택 고정 적용 후 전체 테스트: **2,225 passed**, 기존 warning 9건. Ruff check/format, mypy 99 source files, `git diff --check` 통과.

## 2026-10-07 후속 구현 — 006 통합 및 content GC mark

- 006 OpenCode OCR 후보 구현과 PLAN/REPORT/FIX 이력을 007 브랜치에 별도 커밋 `1ac3cca`로 반영했다. 기존 기본 provider는 유지되며 opt-in `compose.opencode.yaml`만 추가된다. Compose `config --quiet`와 hadolint는 통과했다. gitleaks 전체 작업 트리 스캔은 551건을 보고했으나 **006 변경 파일에서는 0건**이었다. 전체 작업 트리 검출의 내용은 이번 과제에서 수정하지 않았다.
- 이전 단계의 content 존재 시 원격 GC 전체 중단 장치를, **모든 content variant의 manifest·READY·index·OCR CAS를 검증하고 전부 mark**하는 경로로 교체했다. 레거시 GC 규칙은 유지한다. 미완성 variant·index 불일치·retained generation의 variant ID 또는 OCR binding 불일치면 DELETE 전에 중단한다. variant 삭제 정책은 도입하지 않았다.
- 전체 테스트 **2,226 passed**, 기존 warning 9건. Ruff check/format, mypy 99 source files, `git diff --check`, GC 관련 20건 통과.

**계속 미완료:** 007 레거시 원격·로컬 역조회와 전체 산출물 이전, 운영 실데이터 M0 인벤토리, 재처리·복원, no-change/seed 경로 보완, 격리 리허설 및 운영 전환 검증. 006 운영 provider 활성화도 하지 않았다. 운영 timer·state·WebDAV·이미지·설정은 이번 단계에서 변경하지 않았다.

## 2026-10-07 M0 읽기 전용 인벤토리

`cardrag-worker ocr-cache inventory`를 추가했다. 레거시 native/adopted의 manifest·READY·OCR 바이트를 검증하고 현재 stable generation의 OCR CAS를 대조한다. 읽기 전용 구현이며 운영 WebDAV/state에 PUT·DELETE를 하지 않는다. 최초 실사에서는 stable OCR 5,512개를 순차 확인하는 부분이 180초 제한에 걸렸다. 검증된 레거시 바이트는 다시 받지 않고 남은 stable CAS를 최대 16개 병렬로 읽도록 개선한 후 전체 명령이 약 55초에 완료됐다.

| M0 항목 | 관측값 |
|---|---:|
| 레거시 경로 / 유효 / 검증 제외 | 1,818 / 1,818 / 0 |
| native / adopted | 308 / 1,510 |
| 다중 레거시 variant PDF | 81 |
| stable OCR 문서 | 5,512 |
| 같은 OCR을 가리키는 레거시 캐시가 없는 stable 문서 | **3,625** |
| 레거시 최신을 그대로 채택하면 서비스 중 텍스트가 달라질 문서 | **334** |
| stable OCR CAS 검증 실패 | 0 |

이 숫자는 **문서 수 기준**이며 PDF 고유 개수·마이그레이션 신규 variant 수와 같지 않다. 3,625개에 대한 generation 참조 승격과 334개의 서비스 중 텍스트 보존 승격이 필수다. 이 M0는 레거시 캐시와 현재 stable만 조사했다. 모든 과거 generation과 CAS 전체를 대조하는 고아 CAS 집계는 아직 하지 않았다. OCR 본문과 자격값은 보고서에 포함하지 않았다. 마이그레이션 적용은 하지 않았다.

- M0 추가 후 전체 테스트 **2,228 passed**, 기존 warning 9건. Ruff check/format, mypy 100 source files, `git diff --check` 통과. 운영 timer는 `active`.
