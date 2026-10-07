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

### M0 추가 조사: 동일 PDF의 서비스 중 OCR 충돌

현재 stable generation의 5,512개 OCR 문서는 PDF 내용 키 기준 4,846개 그룹이다. **87개 PDF 키의 320개 문서가 같은 PDF에 서로 다른 OCR SHA를 서비스 중**이며, PDF당 단일 최신 variant만 고르면 적어도 214개 문서의 기존 텍스트가 바뀐다. 따라서 PLAN 부록 A.4의 “서비스 중 텍스트 보존”을 PDF당 최신 variant 하나를 추가하는 방법만으로는 달성할 수 없다. Executor는 첫 전환 run에서 문서별 stable OCR SHA로 variant 선택을 고정하는 추가 경로를 설계·검증한 후에만 마이그레이션 apply 및 배포를 제안한다. 신규 문서와 전환 이후의 명시적 재작업은 일반 최신 선택 규칙을 따라야 한다. 이번 추가 조사는 읽기 전용이며 문서 ID·OCR 본문을 보고서에 기록하지 않았다.

### 첫 전환 run의 문서별 서비스 텍스트 고정 구현

현재 remote generation의 contract가 새 Worker contract와 다를 때, canonical generation manifest를 읽어 기존 문서의 PDF identity와 OCR SHA/크기를 묶는다. 새 run에서 PDF가 동일한 문서는 이 OCR identity와 일치하는 content variant만 선택하고, 없으면 provider를 호출하지 않고 실패시킨다. 같은 PDF의 다른 문서는 각자의 기존 OCR로 고정된다. 일반 run은 최신 유효 variant를 선택한다. Resolver 단위 테스트에서 같은 PDF의 과거 variant 고정·새 run 최신 선택·일치 variant 부재 시 차단을 검증했다. 이 경로가 실제 5,512개 문서에 적용되려면 모든 필요한 content variant의 마이그레이션이 먼저 완료돼야 한다.

- 문서별 전환 고정 적용 후 전체 테스트 **2,229 passed**, 기존 warning 9건. Ruff check/format, mypy 100 source files, `git diff --check` 통과.

## 2026-10-07 이전 계획과 격리 적용 경로

- `ContentOCRImportedProvenance`를 추가해 native 계약이 없는 **adopted**와 **generation-only** OCR을 실제 출처대로 기록한다. adopted는 원본 manifest SHA와 `migrated_from`을, generation-only는 stable generation/document ID를 보존한다. 기존 native provenance의 직렬화와 키는 변경하지 않았다.
- `ocr-cache migrate --dry-run`은 검증된 레거시 후보와 stable generation 전용 CAS에서 불변 variant 계획을 만든다. 동일 PDF·다른 OCR은 서로 다른 variant로 보존하고, stable 문서마다 기존 텍스트의 variant ID를 지정한다. 운영 전체 dry-run은 **레거시 1,818 + generation-only 고유 3,354 = 총 5,172 variant**, stable 문서 **5,512개 모두 선택 가능**, 충돌 PDF 키 87개였다. credential 유사 패턴 검사를 추가한 뒤 재실행해 같은 수치로 완료했다. PDF/OCR 본문은 출력하지 않았다.
- 내부 `apply_content_migration`은 기존 CAS를 재업로드하지 않고 검증한 다음 manifest → READY → index를 불변 게시한다. stable pointer가 계획과 다르거나 진행 중 바뀌면 중단하며, 같은 계획을 재실행할 수 있다. **운영 apply CLI는 열지 않았고 운영 WebDAV 쓰기도 하지 않았다.** 가짜 WebDAV에서 2회 적용 멱등성과 이후 opencode 설정 Resolver의 provider 호출 0건을 확인했다. 실제 격리 WebDAV prefix/후보 이미지 리허설은 남았다.
- GC는 content variant뿐 아니라 `migrated_from` 원본 레거시 manifest·READY·CAS도 보존하도록 했다. 원본이 사라지거나 binding이 다르면 DELETE 전에 실패한다.
- 서비스 텍스트 고정은 첫 계약 전환뿐 아니라 이후 corpus 변경 run의 기존 동일 PDF 문서에도 적용되도록 확장했다. 명시적 재작업에서 이 고정을 해제하는 경로는 아직 미구현이다.
- 전체 테스트 **2,235 passed**, 기존 warning 9건. Ruff check/format, mypy 101 source files, `git diff --check` 통과.

**남은 필수 개발:** 문서/issuer/전체 재작업 요청과 복원, 재작업 시 no-change 우회·pin 해제, 로컬 content 인덱스·레거시 역조회 안전망, 실제 격리 리허설과 운영 apply 절차. 적용 전 사용자 승인 단계는 PLAN §6에 따른다.

## 2026-10-07 후속 구현 — 명시적 재처리와 variant 복원

- `ocr-cache reprocess`를 추가했다. stable generation을 기준으로 문서 ID·PDF SHA·issuer·전체 대상을 선택하고, 기본은 문서/페이지/예상 호출 수만 보여준다. `--all`에는 `--confirm-all`이 필요하며 요청당 `--max-documents` 상한은 100개다. `--apply`는 운영 WebDAV가 아닌 **로컬 스풀**에 불변 요청을 원자적으로 등록한다.
- Worker는 run 시작 시 요청 1개를 선택해 해당 run에 고정한다. 재개 중 큐가 바뀌어도 원래 대상을 유지한다. 해당 문서만 기존 OCR 캐시와 서비스 중 텍스트 pin을 우회해 현재 run의 provider로 재처리한다. 같은 요청·PDF의 content variant가 이미 게시됐으면 재호출하지 않는다. 모든 대상 PDF identity를 새 acquisition과 대조하고 content variant 게시가 되지 않으면 실패한다. 성공한 generation 게시 뒤 완료 영수증을 기록하며 손상된 영수증은 무시하지 않고 실패한다.
- `ocr-cache restore --document-id ... --variant ...`는 기본적으로 검증된 과거 variant와 서비스 중 OCR SHA만 보여준다. `--apply` 시 과거 CAS 출력에 새 immutable variant를 붙여 최신으로 승격한다. `ocr-cache show --document-id ...`는 검증된 variant의 ID·생성시각·provider·복원/재처리 출처를 표시하며 OCR 본문은 출력하지 않는다.
- 가짜 WebDAV에서 재처리 1회 호출·동일 요청 재개 0회 호출, 과거 OCR 복원 뒤 다음 run의 최신 선택, 원본 variant 불변, 큐 변경 중 재개 고정, 손상된 완료 영수증 차단을 확인했다. 전체 suite는 이번 코드의 영수증 검증 추가 직전 **2,323 passed, 기존 warning 9건**이었고, 추가 후 관련 145개 테스트가 통과했다. Ruff(Worker/core 범위), mypy(Worker 52 source files), `git diff --check`도 통과했다. 저장소 전체 Ruff는 이전 handoff 증거 스크립트에서 기존 오류 16건을 보고하므로 프로젝트 코드 범위로 검사했다.

**아직 007 인수·배포 전:** 로컬 SQLite content 인덱스와 레거시 역조회 안전망, 실제 격리 WebDAV 후보 리허설, 운영 마이그레이션 apply/첫 generation 검증이 남았다. 특히 서비스 중 OCR을 문서별로 보존하는 규칙과 외부에서 새 variant가 게시된 다음 run의 최신 선택을 함께 검증해야 한다. 006 OpenCode 공급자는 코드에 통합됐지만 운영 활성화하지 않았다. 운영 WebDAV·state·timer·이미지·설정에는 쓰지 않았다. PLAN §6의 단계 6·7은 별도 승인 대상이다.

## 2026-10-07 격리 WebDAV 리허설 및 운영 적용 명령 준비

- `ocr-cache migrate`에 `--apply --confirm-stable-generation <dry-run의 ID>`를 추가했다. 전체 계획을 다시 검증한 뒤 확인한 stable ID와 다르면 거부하고, Worker 락을 잡아 불변 variant를 게시한다. 내부 apply는 처리 중 stable pointer 변경을 검사하며 재실행 가능하다. **이 명령을 운영 WebDAV에는 실행하지 않았다.**
- 실제 `cardrag_core.WebDAVClient`와 Worker WebDAV facade를 사용하되 네트워크를 격리한 HTTP MockTransport에서 PROPFIND/HEAD/GET/MKCOL/PUT/MOVE/DELETE를 통과시켰다. generation-only OCR 이전을 2회 반복해 멱등을 확인하고, 기존 원격 파일의 바이트가 모두 남았으며, 006 OpenCode 설정 Resolver가 OCR 공급자를 호출하지 않고 이전 variant에 적중했다. 복원 variant의 최신 선택 및 run snapshot도 같은 경로로 확인했다. 이는 WebDAV 프로토콜 경로 리허설이지 운영 서버/전체 5,172건 쓰기 리허설은 아니다.
- `restore` 등 별도 게시된 최신 variant가 다음 run에서 무시되는 문제를 발견했다. 같은 Worker contract의 stable generation 이후 생성된 **검증된** variant가 run 시작 snapshot에 있으면 그 PDF의 문서별 이전 OCR pin과 no-change 빠른 경로를 해제한다. 첫 contract 전환에서는 항상 이전 OCR pin을 유지해 D5의 기존 텍스트 보존을 우선한다. 새 variant가 run 도중 생기면 해당 run에는 반영되지 않는다.
- 읽기 전용 `ocr-cache verify`를 추가했다. 원격 content index 전체를 1회 열거해 각 variant의 manifest·READY·index·CAS/OCR 바이트를 검증하고, 현재 stable OCR 문서마다 동일 PDF·동일 OCR SHA/크기의 variant(새 generation은 정확한 variant ID)를 확인한다. 격리 리허설에서 이전 직후 검증이 통과했고, 손상된 READY는 실패했다.
- 계획의 로컬 SQLite content 인덱스는 현재 **원격 평면 index 1회 열거 → run 로컬 JSON snapshot → 메모리 key index → 문서별 불변 선택 파일**로 대체했다. 같은 run의 재개 결정성과 원격 목록 비용을 만족하면서 5,172행을 SQLite에 중복 적재하지 않는다. 레거시 역조회는 전체 5,172 variant를 먼저 이전하고 stable 5,512문서 매핑을 전량 dry-run과 소형 격리 apply로 확인한 뒤 Worker를 전환하는 게이트로 대체한다. 이전 variant가 누락된 서비스 문서는 provider를 대량 호출하지 않고 기존 OCR pin 검증에서 중단한다. 이 두 항목은 PLAN의 저장 방식/전환기 안전망에서 벗어난 **구현 편차**다.

**운영 전환은 계속 보류:** 전체 corpus에 대한 운영 `migrate --apply`, MCP→Worker 이미지 배포, 첫 generation 관찰, 006 공급자 활성화는 아직 수행하지 않았다. PLAN §6 단계 6·7의 승인과 운영 창구가 필요하다. 운영 시스템의 WebDAV·state·timer·이미지·환경변수는 변경하지 않았다.

검증: 전체 suite **2,327 passed, 기존 warning 9건**. 실행 중 독립 프로세스 lock 경합 테스트가 1회 실패했으나 단독 재실행 통과했고, 다음 전체 suite도 통과했다. 마지막 `verify` 추가 후 관련 144개 테스트, Ruff(Worker/core), mypy(Worker 52 source files), format, `git diff --check` 통과.
