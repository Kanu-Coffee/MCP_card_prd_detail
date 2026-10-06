# PLAN 007 — PDF 내용 기준 OCR 캐시(Content-Addressed) 전면 개편과 기존 산출물 이전

작성 역할: Planner. 2026-10-06 KST. 기준: `feat/006-opencode-ocr-provider`(base `2bcd514`), 운영 `/opt/cardrag/current -> v1.0.29`, 운영 이미지 `…candidate@sha256:2853…`(v1.0.30-candidate). 코드·운영 상태는 **읽기 전용**으로만 조사했다(운영 state는 `--network none`, `:ro` 마운트로 manifest 1건 확인).

## 1. 사용자 요구사항 (확정 해석)

1. OCR 캐시 적중 판단에 `provider`, `model`, `processor_version`, `reasoning_effort`를 **쓰지 않는다**. 같은 원본 PDF(내용 동일)로 한 번이라도 만들어진 유효한 OCR 산출물이 있으면 적중이다.
2. 위 네 값은 **참고·기록(provenance)** 으로만 적재·관리한다.
3. run마다 provider/model/effort를 **자유롭게 바꿔도** 기존 캐시·기존 generation 식별이 흔들리지 않아야 한다.
4. 현존 OCR 산출물도 이 구조로 **이전(migration)** 해 재사용한다.

## 2. 결론: 가능하다. 단, 캐시 키만 바꾸면 부족하다

"provider를 바꿀 때마다 같은 문제가 반복"되는 근본 원인은 **세 곳**이 OCR contract(provider·model·effort·processor_version 포함)에 묶여 있기 때문이다.

| # | 결합 지점 | 위치 | 영향 |
|---|---|---|---|
| A | OCR 캐시 키 | `cardrag_core/ocr.py: native_ocr_reuse_key(contract, source)` → `v1/ocr-cache/native/<xx>/<key>` | provider 변경 시 전 문서 miss |
| B | **Generation(Worker) identity** | `pipeline.py: contract_sha256`의 `"ocr_contract": self.ocr.contract` (v4/v2 두 payload 모두) | provider 변경 시 corpus가 같아도 `ready_publish(corpus, contract)`, no-change 판정, prior seal/resume/cache-healing(`pipeline.py ~3590–3840`)이 전부 다른 generation으로 취급 |
| C | 호환 계약 탐색 우회책 | `discover_compatible_contracts`(model/effort만 치환, provider 불변, run당 manifest 1건만 스캔) + `CARDRAG_OCR_COMPATIBLE_MODELS` | 부분 적중만 가능, 예측 불가 |

즉 A만 고치면 캐시는 맞아도 **B 때문에 provider 변경 run이 새 generation을 전체 재게시**하려 한다. A+B를 함께 바꾸고 C(호환 모델 목록)는 폐기한다.

핵심 발견: 코어에 **`content_addressed_ocr_reuse_key(source)`(PDF sha256+size+page_count만 사용)가 이미 존재하나 어디서도 쓰이지 않는다.** 새 키 함수를 처음부터 만들 필요가 없다. 또 MCP는 OCR contract를 참조하지 않으며(`grep` 결과 0건), `GenerationDocument`는 `ocr_cache_kind: Literal["native","adopted"]` + `ocr_reuse_key`로만 캐시 항목을 가리킨다 → 변경 범위가 Worker+core에 한정되고 MCP는 Literal 확장만 필요하다.

## 3. 목표 구조

### 3.1 캐시 키
- 새 kind `"content"`: key = `content_addressed_ocr_reuse_key(OCRInput)` = f(pdf_sha256, pdf_size_bytes, page_count). 경로 `v1/ocr-cache/content/<xx>/<key>/{manifest.json,READY.json}`. 기존 `native`/`adopted`는 **삭제·수정하지 않고** 읽기 전용 레거시로 남긴다.
- **미결정 D1**: 운영자 수동 무효화용 `cache_epoch`를 키에 남길지. 권장: 키 함수 v2 = `{input, cache_epoch}`(기본 0, 평소 불변). 요구사항 문면은 "PDF만"이므로 사용자 확인 필요. 확인 전 기본값은 epoch 포함(0).
- prompt_version, renderer, render_scale, segmentation 설정도 키에서 제외한다(사용자 지시 취지). 대신 provenance에 기록. **귀결**: 프롬프트·분할을 개선해도 기존 문서는 자동 재OCR되지 않는다 → 강제 재처리는 epoch 증가 또는 문서 단위 invalidate 명령(§3.5)으로만.

### 3.2 manifest (신규 스키마, 추가형)
`cardrag.ocr-content-artifact.v1`: `reuse_key`, `source(OCRInput)`, `output(ArtifactRef, 기존 CAS 객체)`, `ocr_chars`, `page_output_sha256`, `created_at`, **`provenance`**(= 기존 `NativeOCRContract` 전체: provider/model/processor_version/reasoning_effort/prompt/renderer/segmentation, 단 검증 동등성·키에 불참여), `migrated_from`(선택: 레거시 kind+reuse_key). 출력 검증은 기존 `verify_ocr_bytes`(페이지 마커·해시·표준 join)를 그대로 쓴다. 불변 PUT(first-writer-wins): 동시 run이 서로 다른 LLM 변형을 만들면 먼저 게시된 쪽이 정본이며, 기존 코드의 "remote entry already holds a different variant" 경로가 오류가 아닌 정상 재사용으로 바뀐다.

### 3.3 Resolver 조회 순서
1. (기존) seed ledger/retained seal → 2. 원격 `content` → 3. 로컬 run 산출물(content 인덱스) → 4. **레거시 `native`/`adopted`에서 content key로 역조회**(마이그레이션 전환기 안전망, 읽기 전용; 적중 시 content 항목을 승격 게시) → 5. provider 호출, 성공 시 `content` 게시.
`compatible_contracts`/`CARDRAG_OCR_COMPATIBLE_MODELS` 경로는 제거(또는 deprecated no-op).

### 3.4 Generation identity 분리
`contract_sha256` payload의 `"ocr_contract"`를 **`"ocr_cache_policy"`**로 대체: `{schema:"cardrag.ocr-content-cache.v1", output_profile, validation_profile, key_schema}` + (D1 결정 시 epoch). provider/model/effort/prompt는 불포함. Worker contract 스키마를 `v5`로 올린다(`worker-contract.v4/v2` payload 식별자 유지·신규 payload 추가, 기존 seal 검증 코드는 구버전 payload를 계속 해석).
- **일회성 영향**: payload가 바뀌므로 개편 배포 직후 첫 run은 corpus가 같아도 새 generation 식별로 게시된다(문서 100% 캐시 적중이므로 어제 run과 같은 약 2.6시간·provider 호출 0건). 이후 provider 변경은 generation identity를 바꾸지 않는다. stable 게시 승인 필요(PLAN 단계 게이트).
- `exporter*.py`, `corpus_baseline.py`, `legacy_v4_audit.py`, `state_seed_v122.py`, `gc.py`, `state.py`의 reuse_key/contract 참조 점검·갱신(§5 표). `GenerationDocument.ocr_cache_kind`를 `"content"` 포함으로 확장(additive Literal). **배포 순서: MCP 이미지(새 Literal 인지) → Worker.** 구 MCP가 새 manifest를 거부하면 갱신이 막히고 기존 generation은 계속 서비스되지만, 순서를 지켜 방지한다.

### 3.5 provenance 관리와 운영 도구
- 문서별 provenance는 content manifest에 보존. run 단위 지표는 `performance`에 `ocr_provider_called_count`를 provider/model별로 추가(본문·키 제외).
- 신규 CLI(읽기·계획 기본, 실행은 승인 플래그): `ocr-cache inventory`(종류·contract별 건수·다중 변형 PDF), `ocr-cache migrate --dry-run|--apply`, `ocr-cache verify`, `ocr-cache invalidate <document|pdf_sha256>`(content manifest를 쓰지 않고 로컬 quarantine 기록 + 다음 run 강제 재OCR; epoch와 별개).

## 4. 기존 OCR 산출물 이전 설계

대상(조사 근거): 원격 `v1/ocr-cache/native|adopted` 항목, 로컬 `runs/*/documents/*/ocr/native-manifest.json`, **원격 캐시에 게시되지 못한 generation 전용 OCR**(어제 run 로그 `cache_publication_deferred=120`). 운영 manifest 표본은 `provider=local-paddleocr`(Paddle) 계열이 존재 → 한 PDF에 여러 계약의 산출물이 공존한다고 가정한다.

원칙: **추가만 한다(원본 불변·삭제 없음).** CAS 출력 객체는 재사용하고 작은 manifest/READY만 새 경로에 쓴다. 멱등·재개 가능.

1. **M0 인벤토리(읽기 전용).** 모든 manifest를 열거(`candidate_acceptance`의 PROPFIND 열거 패턴 재사용), 건수/계약/다중 변형/고아 CAS를 보고. 각 출력은 `verify_ocr_bytes`로 재검증하고 실패 항목은 이전 제외·명단 출력.
2. **M1 정본 선택 규칙(PDF당 1개).** 우선순위: ① 현재 stable generation의 `documents[].ocr`가 가리키는 산출물(서비스 중인 텍스트와 동일 보장) → ② 검증 통과 중 `created_at` 최신 → ③ `contract_sha256` 사전순. 다른 변형들은 `migrated_from`/`alternates`로 provenance에만 남기고 키에는 쓰지 않는다. **미결정 D2**: 정본 선택 정책 확인.
3. **M2 generation 전용 OCR 승격.** stable generation 매니페스트의 `ocr` ArtifactRef(CAS 존재)에서 PDF 정보와 함께 content 항목 생성(120건 + 이후 발견분).
4. **M3 로컬 인덱스.** 로컬 state(SQLite)에 `content_key → (run, document, 경로, sha)` 인덱스 테이블을 추가해 로컬 재개(`_load_local_native` 경로)도 content key로 동작. 기존 `native-manifest.json` 파일은 그대로 두고 읽기만.
5. **M4 적용.** `--apply`는 사용자 승인 후, 격리 채널에서 먼저 리허설(복제 state/WebDAV 쓰기 비활성 또는 테스트 prefix), 그다음 운영 prefix에 불변 PUT. 중단 후 재실행 시 이미 있는 항목은 건너뜀. 원격 GC(`CARDRAG_REMOTE_GC_APPROVED`)는 이전·전환 기간 내내 **false 유지**하고, GC가 `content` kind를 live 참조로 인식하도록 먼저 수정·테스트한 뒤에야 재허용.
6. **롤백.** 레거시 경로를 건드리지 않으므로, 구 이미지+구 env로 복귀하면 이전 동작이 그대로다. content 항목은 무해한 추가분이다.

## 5. 변경 범위 (조사된 결합 건수)

| 영역 | 파일 | 결합 참조 수 | 작업 |
|---|---|---|---|
| core | `ocr.py`, `manifests.py`(`OCRCacheKind`, 신규 manifest, `GenerationDocument`), `paths.py`, `__init__.py` | — | 키 v2·신규 모델·Literal 확장(additive) |
| worker | `ocr.py` | 131 | 조회 순서 재작성, 게시, compat 경로 제거 |
| worker | `pipeline.py` | 64 | contract payload 분리, no-change/seal/healing 판정 정리 |
| worker | `state.py` / `state_seed_v122.py` / `gc.py` | 24 / 20 / 19 | content 인덱스·seed·GC 인식 |
| worker | `corpus_baseline.py` / `adoption.py` / `legacy_v4_audit.py` / `exporter*.py` / `cli.py` / `ocr_recovery.py` / `webdav.py` / `cache_seed*.py` | 12 / 10 / 5 / 5 / 2 / 2 / 5 / 2 | 개별 점검 |
| mcp | `models`/`schema*` 소비 경로 | — | Literal 확장 수용(코드 변경 최소) |
| 006 산출물 | `OpenCodeOCRProvider`·compose overlay·Dockerfile | — | **그대로 유효**. 이 개편 이후에야 의미 있는 운영 반영 가능 |

규모: 중대형(약 10개 worker 모듈, core 스키마). 데이터 손실 위험은 추가형 설계로 낮다. 가장 큰 위험은 pipeline의 seal/resume/healing 불변식(`contract_sha256` 동등성 가정)이 깨지는 것이며, 이는 테스트 매트릭스로 통제한다.

## 6. 단계 게이트

0. 읽기 전용 사전 점검: 브랜치(006 위 또는 main 기반 신규 브랜치 `feat/007-content-ocr-cache`), 운영 timer·stable generation·디스크.
1. **설계 확정 + 결정 사항 D1~D4 사용자 확인.**
2. core 구현(키·manifest·Literal) + 단위 테스트. 기존 provider contract 해시 **불변** 테스트(레거시 키 함수는 보존).
3. worker 구현: Resolver 조회 순서, 게시, contract 분리(v5), state 인덱스, GC 인식. 테스트 매트릭스(§7).
4. 마이그레이션 도구 구현 + **M0/dry-run만 운영 실데이터에 읽기 전용 실행**해 건수·다중 변형·검증 실패 보고(쓰기 없음).
5. 격리 리허설: 운영 state 복제 대신 테스트 prefix WebDAV/임시 state에 샘플로 M1~M4, provider A→B 교체 run으로 호출 0건 입증.
6. (사용자 승인) MCP 새 이미지 → Worker 새 이미지, `ocr-cache migrate --apply`, 첫 run 관찰(예상: provider 호출 0, 새 generation 게시, `ocr_cache_reused=expected`).
7. (사용자 승인) 그 후 006 단계 4: opencode 활성화. 이때는 provider 교체가 캐시·generation에 영향이 없으므로 위험이 사라진다.

## 7. 필수 테스트 매트릭스

- provider/model/effort/processor_version만 다른 두 설정으로 같은 PDF: 2번째 provider 호출 0, 같은 OCR sha, 같은 `contract_sha256`.
- 레거시 `native`/`adopted`만 있는 PDF: content로 역조회 적중 + content 승격, 재실행 시 content 직접 적중.
- 한 PDF에 다중 변형: 정본 선택 규칙·결정성·`alternates` 기록.
- 동시 2 run이 서로 다른 출력 게시 시도: first-writer-wins, 나머지는 재사용·오류 아님.
- 페이지 수/해시/마커 불량 항목은 적중 대상에서 제외(재OCR).
- generation 전용 OCR 120건류 승격, 승격 후 seal/healing/resume 경로(`test_pipeline`, `test_ocr_recovery`, seal 테스트 군)가 새 contract에서도 통과.
- 구 manifest/seal(v4 payload) 해석 보존, 신 payload와의 공존.
- GC가 content kind를 live로 보호, 마이그레이션 중단·재개 멱등.
- 구 MCP/신 MCP가 `ocr_cache_kind="content"` manifest를 어떻게 처리하는지(신 MCP 필수, 구 MCP 거부 시 보고).
- 비밀/OCR 원문이 로그·보고서에 없음, `ruff`/`mypy`/전체 테스트/`gitleaks`.

## 8. 수용 기준

1. provider 변경 run이 기존 문서에 대해 provider 호출 0건, generation 식별 불변임이 테스트와 격리 샘플로 입증.
2. M0 인벤토리 보고서가 운영 실데이터(읽기 전용)에서 이전 대상·제외 대상·다중 변형 건수를 제시.
3. 마이그레이션은 추가형·멱등·재개 가능하며 롤백 경로가 문서화·검증됨. 운영 stable/timer/state는 단계 6 승인 전까지 쓰이지 않음(전후 비교).
4. 006의 opencode 코드는 변경 없이 새 구조 위에서 동작(opencode provider 단일 run → 2회차 호출 0).

## 9. 사용자 결정 필요 사항

- **D1** `cache_epoch`를 키에 남길지(권장: 남김, 기본 0·수동 무효화 전용).
- **D2** 다중 변형 PDF의 정본 선택 정책(권장: stable 서비스 중 산출물 → 최신 검증 통과).
- **D3** 첫 배포 시 generation 식별 1회 변경(동일 문서·100% 캐시 적중, 약 2.6시간 run)을 허용하는지.
- **D4** 프롬프트/분할/렌더 설정 변경이 자동 재OCR을 일으키지 않는다는 귀결을 수용하는지(강제 재처리는 epoch 또는 invalidate 명령).

## 10. 관련 파일

`packages/cardrag-core/src/cardrag_core/{ocr.py,manifests.py,paths.py,__init__.py,candidate_acceptance.py}`, `apps/cardrag-worker/src/cardrag_worker/{ocr.py,pipeline.py,state.py,state_seed_v122.py,gc.py,corpus_baseline.py,adoption.py,legacy_v4_audit.py,exporter.py,exporter_v5.py,cli.py,settings.py,webdav.py}`, `apps/cardrag-worker/tests/{test_ocr.py,test_pipeline.py,test_ocr_recovery.py,test_seal.py,test_gc.py,test_state*.py}`, `.handoff/006_opencode-ocr-provider/`, `.handoff/001`~`005`(캐시·전환 규칙).

---

# 개정 부록 A — 사용자 결정 반영 (2026-10-06 23:07 KST)

Planner 추가 기록. 핸드오프 이력 보존 규칙에 따라 본문은 수정하지 않고 이 부록을 덧붙인다. **본문과 충돌하는 §3.1(epoch)·§3.2(first-writer-wins)·§3.3·§4-2·§9는 이 부록이 우선한다.**

## A.1 확정된 결정

| 항목 | 결정 |
|---|---|
| D1 | `cache_epoch`를 키에 남긴다(키 v2 = `{input, cache_epoch}`, 기본 0, 수동 전체 무효화 전용). 사용자 채택. |
| D2 | **한 PDF에 여러 OCR(variant) 공존을 허용**, 기본 채택은 **가장 최근 생성된 유효 variant**, 캐시는 항상 적중. 특정 문서·전체를 **다른 provider로 재작업 지시** 가능해야 함. |
| D3 | 첫 배포 시 generation 식별 1회 변경 승인(동일 문서·캐시 100% 적중, 약 2.6시간 run). |
| D4 | 프롬프트/분할/렌더 설정 변경이 자동 재OCR을 일으키지 않음을 허용. 추가 OCR은 수동 지시로만. |

## A.2 variant 모델 (본문 §3.2 대체)

first-writer-wins(단일 불변 manifest)를 폐기하고 **불변 variant 레코드 집합 + 읽기 시 최신 선택**으로 바꾼다. 가변 포인터(`LATEST`)는 WebDAV에서 경합 위험이 있어 쓰지 않는다.

- 경로: `v1/ocr-cache/content/<xx>/<key>/variants/<created_utc>-<manifest_sha12>/{manifest.json,READY.json}`. variant마다 고유 경로이므로 동시 run이 서로를 덮어쓸 수 없다(기존 불변 PUT 규칙 유지).
- 게시 순서: CAS 출력(기존 재사용) → manifest → READY. READY가 있고 `verify_ocr_bytes`를 통과한 variant만 후보.
- 선택 규칙: 후보 중 `created_at` 최신, 동률은 `manifest_sha256` 사전순. 선택은 읽기 시점의 결정적 함수이며 부작용이 없다.
- manifest 필드: §3.2의 필드 + `variant_id`(= manifest sha256), `provenance`(NativeOCRContract 전체), 선택적 `reprocess_request_id`, `restored_from`(variant_id).
- 목록 비용: 문서마다 PROPFIND하지 않고 run 시작 시 기존 `prefetch_local_native` 패턴으로 **일괄 인덱스**(원격 목록 1회 + 로컬 SQLite 인덱스 테이블)를 만든다.
- **run 범위 고정(selection snapshot)**: run 시작 시 문서별 선택 variant를 run 로컬 state에 기록하고 그 run 동안 고정한다. 진행 중 다른 프로세스가 새 variant를 게시해도 이어달리기·seal 불변식(`expected_ocr_identity`)이 흔들리지 않는다. 새 variant는 **다음 run**부터 채택된다.
- **Generation은 정확한 variant를 고정**: `GenerationDocument`에 `ocr_variant_id`(선택·추가형)를 더해, 어떤 generation이 어떤 OCR 텍스트로 만들어졌는지 재현 가능하게 한다. 구 MCP 호환은 §3.4 배포 순서(MCP 먼저)를 따른다.
- 보존: 구 variant는 삭제하지 않는다. GC는 variant 전부를 live로 보호하고, 정리는 별도 승인된 보존 정책(예: PDF당 최근 N개)으로만 한다.

## A.3 수동 재작업 지시 (신규 요구)

원칙: **요청은 "대상"만 정하고 provider·model·effort는 그 run의 설정을 그대로 쓴다.** 즉 운영자가 `compose.opencode.yaml` 같은 overlay/env로 원하는 provider를 지정해 run을 띄우고, 재작업 대상은 요청으로 지정한다. 별도 요청별 provider 오버라이드를 만들지 않아(이중 resolver 복잡도 회피) 06 단계에서 만든 provider 전환 경로를 그대로 쓴다.

1. 요청 접수: `cardrag-worker ocr-cache reprocess` (기본 **계획만 출력**, `--apply` 필요)
   - 대상: `--document-id ID`(반복 가능) / `--pdf-sha256 H` / `--issuer I` / `--all`.
   - 안전장치: 문서·페이지 수와 예상 호출 수를 먼저 보고, `--all`은 `--confirm-all`과 `--max-documents N`(분할 처리, 재개 가능)을 요구.
   - 저장: worker 락과 충돌하지 않도록 `state/ocr-requests/<request_id>.json` 스풀 파일(불변, append-only).
2. 소비: 다음 run 시작 시 스풀을 읽어 대상 문서의 **캐시 읽기를 우회**하고 현재 run의 provider로 호출, 결과를 새 variant로 게시(`reprocess_request_id` 기록). 이후 최신이므로 자동 채택. 멱등: 같은 `request_id`+PDF로 이미 variant가 있으면 건너뜀. 부분 실패 문서는 요청에 남아 다음 run에서 재시도.
3. 되돌리기: `ocr-cache restore --document-id ID --variant V` — 과거 variant의 CAS 출력을 가리키는 **새 variant**를 만들어 "최신"으로 승격(원본 variant 불변). 재작업 결과가 더 나쁠 때의 안전판이다.
4. 조회: `ocr-cache inventory`/`show --document-id ID`로 variant 목록과 provenance(provider/model/effort/processor/생성시각), 현재 선택 표시.
5. 영향: 재작업된 문서는 다음 generation에서 OCR 텍스트가 바뀌므로 임베딩·게시가 따른다. `--all`은 사실상 전체 새 generation이므로 candidate 채널 리허설과 stable 게시 승인 필요(문서 단위 소량은 일반 run으로 충분).

## A.4 기존 산출물 이전 규칙 변경 (본문 §4-2 대체)

- **모든 검증 통과 레거시 산출물을 각각 variant로 이전**한다(`created_at`은 원 manifest 값 보존, `restored_from`이 아닌 `migrated_from=<legacy kind/key>`). 다중 계약(Paddle/codex 등)이 한 PDF에 있으면 variant가 여러 개가 되며 최신이 선택된다.
- **서비스 중 텍스트 보존이 기본값(D5, 권장)**: 단순 "최신 채택"이면 현재 stable generation이 서비스 중인 산출물이 더 오래된 경우 첫 generation에서 텍스트가 바뀐다. M0 인벤토리가 그 건수를 계산해 보고하고, 마이그레이션은 기본으로 해당 PDF에 대해 **서비스 중 산출물을 가리키는 새 variant(`restored_from`)를 추가 게시**해 최신=서비스 중이 되게 한다. 그러면 D3의 identity 1회 변경은 있어도 **OCR 텍스트·임베딩은 변하지 않는다.** 순수 최신 채택을 원하면 `--adopt-latest`로 전환(사용자 승인 시). 이후 새 OCR은 전부 "최신 채택" 규칙을 따른다.
- 120건류(원격 캐시에 게시되지 못하고 generation에만 있는 OCR)는 generation 매니페스트의 CAS 참조에서 variant로 승격한다.
- 로컬 `runs/*/documents/*/ocr/native-manifest.json`은 로컬 content 인덱스에 등록(읽기 전용, 파일 불변).

## A.5 추가 테스트·수용 기준

- 같은 PDF에 provider A→B→C 순차 run: 모두 호출 0(캐시), 선택=최신 유효 variant.
- 재작업 요청(문서 1건/issuer/`--all`·`--max-documents` 분할)이 대상에서만 provider 호출, 나머지는 호출 0, 결과가 새 최신 variant이며 다음 run에서 채택, 멱등·재개.
- `restore`가 과거 텍스트를 새 최신으로 복원(원 variant 불변), 이후 run에서 그 텍스트 채택.
- run 진행 중 외부 variant 게시가 해당 run의 선택을 바꾸지 않고(snapshot), 다음 run에서 반영.
- 동시 게시 경합에서 variant 경로 충돌 없음, 선택 결정성(동시각 동률 처리).
- D5: 마이그레이션 전후 서비스 중 문서의 OCR sha·임베딩이 동일(인벤토리 보고와 일치), `--adopt-latest`는 보고된 건수만큼만 변경.
- 기존 수용 기준(§8) 1·3·4 유지, 2는 variant 건수까지 포함해 보고.

## A.6 미결정(차단 아님)

- **D5** 마이그레이션 기본값을 "서비스 중 텍스트 보존"으로 둔다(위 권장). 이견이 있으면 알려 달라. 이견이 없으면 Executor는 이 기본값으로 진행한다.
- variant 보존 한도(PDF당 최근 N개)는 GC 재허용 시점에 별도 결정(현재는 무제한 보존).
