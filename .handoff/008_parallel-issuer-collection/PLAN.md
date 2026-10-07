# PLAN 008 — 카드사 병렬 수집·장애 격리 및 006/007 통합 운영 마무리

작성 역할: Planner. 2026-10-07 KST. 저장소 `/home/lee/projects/MCP_card_prd_detail`. 기준 브랜치 `feat/007-content-ocr-cache`, HEAD `ff943472715424e3fd1ee3337fee5a0d0b6bdf2c`(계획 작성 전 clean). 배포 제품 코드 `31edb1da1033b85e81bb30e0cf98e7e055e5d892`. **후임 Executor의 기본 작업 문서는 이 PLAN 하나다.** 과거 문서 재독은 필수가 아니며 아래에 현황·요구·배포·검증을 통합한다. Git 코드와 실제 운영 상태가 변경됐다면 먼저 최소 diff를 확인하고 편차를 REPORT에 쓴다.

## 1. 목적과 이번 범위

신한 연결 장애가 장기화되어도 다른 카드사 수집과 OCR·게시가 진행되어야 한다. 8개 카드사의 adapter를 제한된 병렬로 실행하고, 특정 카드사의 discovery/prepare/download 실패는 해당 카드사 결과와 경고로 남긴 뒤 나머지를 계속 수집한다. **모든 수집 작업이 성공 또는 격리로 종료된 다음 OCR 단계로 넘어간다.**

실패한 카드사는 이미 서비스 중인 검증된 자료를 유지한다. 접속 실패를 단종이나 빈 정상 목록으로 간주하지 않는다. 성공한 카드사의 신규 PDF만 정상 OCR 경로로 처리하고, 기존 동일 PDF의 OCR은 content 캐시로 재사용한다. 현재 신한 reset을 실제 장애 표본으로 사용하되 자동 테스트는 결정적인 가짜 reset으로 재현한다.

006 OpenCode 및 007 content cache 코드는 이미 이 브랜치에 있다. 재작성·별도 재병합하지 않는다. 이번 개발·운영 검증에서 함께 작동함을 확인하고 007의 미완료 배치 인수를 마무리한다. OpenCode 운영 활성화는 §10의 별도 마지막 게이트로 분리한다. 계획 작성 요청 자체는 이번 세션에서 Worker 실행·provider 변경·PR 병합을 하라는 요청이 아니다.

## 2. 현재 구현·운영 기준 — 과거 문서 대신 이 절 사용

### 2.1 006 이미 구축된 기능

- `providers.py`의 `OpenCodeOCRProvider`, 설정/팩토리/CLI primary 및 fallback 배선.
- provider `opencode`, 모델 기본 `alibaba-token-plan/qwen3.8-flash`, effort `medium`. 외부 OCR 허용 게이트 적용. 기존 codex/openrouter/Paddle 기본값 유지.
- Worker 이미지의 고정 OpenCode CLI 1.18.34, npm tarball SHA 검증. UID 10001/read-only rootfs 환경 실행 지원.
- 전용 agent `ocr`, 전체 tools deny/permission deny, `--pure`, 최소 설정·환경·secret. 엄격한 JSON 이벤트 파싱, 출력 크기 한도, timeout/cancel 처리. 키·OCR 원문 로그 금지.
- `deploy/worker/compose.opencode.yaml` opt-in overlay가 기본 Compose의 모델·effort 주입을 교정한다. primary codex의 high와 fallback opencode의 medium을 혼용하지 않는다.
- 이전 Executor 보고: 격리 합성 PDF 1p/표·각주 2p/분할 5p 실사, 각각 약 18/20/64초, 두 번째 실행 provider 호출 0 및 OCR SHA 동일. 이는 기존 보고 근거이며 실제 운영 활성화 증거는 아니다. 필요 이상으로 다시 실호출하지 않는다.

### 2.2 007 이미 구축된 기능과 유지할 계약

- content key v2 = PDF SHA256 + PDF 바이트 크기 + 페이지 수 + cache_epoch(운영 0). provider/model/effort/processor/prompt/render/segmentation은 provenance이며 적중 키에 포함되지 않는다.
- 경로 `v1/ocr-cache/content/<xx>/<key>/variants/<created_utc>-<manifest_sha12>/`의 불변 manifest/READY, CAS 출력, `content-index` 평면 인덱스. variant_id는 자기 필드를 제외한 canonical manifest SHA이며 전체 manifest SHA는 별도다.
- run 시작 원격 index 목록을 JSON snapshot으로 고정하고 문서별 선택 파일을 원자적으로 기록한다. 유효 최신 variant를 선택하며 동일 run 재개는 선택 ID/바이트를 유지한다. 새 variant는 다음 run에만 반영된다.
- GenerationDocument는 content kind와 정확한 `ocr_variant_id`를 기록한다. worker identity는 OCR provider contract 대신 cache policy를 사용한다(v5 경로 worker-contract.v5, 레거시 경로 v3). 모델/effort 변경만으로 전량 재처리·새 generation을 만들지 않는다.
- 기존 stable 동일 PDF의 OCR SHA/크기로 문서별 pin. 같은 PDF에 여러 기존 OCR이 있을 때도 각각 유지한다. 명시적 reprocess/restore 및 정상 계약의 새 variant 채택은 기존 코드 규칙을 따른다.
- CLI `ocr-cache inventory`, `migrate --dry-run`, `migrate --apply --confirm-stable-generation ID`, `verify`, `show`, `reprocess`, `restore`. 재처리 요청은 로컬 불변 spool·run 고정·완료 영수증 사용. 기본 CLI는 계획/조회이고 apply는 별도다.
- GC는 모든 variant 및 참조 CAS/원 레거시를 보호하지만 **운영 원격 GC는 계속 false**. SQLite content 인덱스나 실시간 레거시 역조회는 구현하지 않았다. 평면 index/run snapshot과 전량 migration 게이트가 현재 채택된 기능적 대안이다. 이를 새로 구축하지 않는다.
- CI run `37565367823`: 배포 제품 코드의 2,327 테스트 및 lint/type/Compose/build 통과. 006+007 주요 테스트는 §9에 기재한다.

### 2.3 실제 운영 상태와 증거

| 항목 | 기준 상태 |
|---|---|
| current | `/opt/cardrag/007-31edb1d` |
| MCP image | `cardrag-mcp:007-31edb1d`, local ID `sha256:1ddf263a7024c4ddf982ee5956938fd7fa195f9318e97901293301ebbaf9f6f7` |
| Worker image | `cardrag-worker:007-31edb1d`, local ID `sha256:2b1a1257f3348c1a8b2465282847ef633c0ca6514025f394c646ae344c598522` |
| MCP | project `cardrag-stable-v1026`, `cardrag-stable-v1026-mcp-1` healthy, localhost port 18015 |
| Worker | project `cardrag-worker`, state volume `cardrag-worker-v130-candidate-state`, auth `cardrag-worker-v120-recovery-auth-20260910` |
| MCP state | `cardrag-mcp-v129-candidate-state` |
| OCR | `codex-exec / qwen3.8-flash`, read-write, epoch 0, stable/cache publication approvals true |
| GC | `CARDRAG_REMOTE_GC_APPROVED=false`, `CARDRAG_COLLECT_REMOTE_GARBAGE=false` |
| stable | `g-03fbc4f18a3c450bb017e2fd-36bae25dd8cd`, OCR 문서 5,512개 |
| migration | apply Docker exit 0, 독립 verify exit 0, 5,172 variant / stable 5,512문서 대응 |
| 마지막 batch | `cardrag-prod-007-fix01`, `bb471003c6844c7394aae97fd9f85f26`, exit 1, 신한 discovery reset, OCR 전 중단 |
| timer | `cardrag-worker.timer` active, 새벽 03:00 예약. 수동 배치와 과거 systemd failed 상태 구분 |
| rollback | `/opt/cardrag/v1.0.29` + 이전 이미지/설정 1세트. 추가 과거 운영 사본 만들지 않음 |
| disk | 계획 작성 재점검 약 69G available. 실행 전 재측정하고 실제 dynamic preflight 적용 |

`/etc/cardrag/worker.env`, `mcp.env`는 현재 사용자에게 읽기 가능하나 쓰기 권한이 없다. 기존 배포 디렉터리의 역할별 `compose.secrets.yaml`에 image override가 있어 env의 이전 이미지 문자열보다 우선한다. 새 배포에서도 실제 resolved image/project/volume을 확인한다. 전체 config/env/secret 원문 출력 금지.

증거는 `/opt/cardrag/007-31edb1d/operations/`의 `rollback.json`, `stable-ocr-baseline.json`(문서별 PDF/OCR SHA·크기), `migration-verify.json`, `migration-recovery-verified.json`, `worker-result.json`, `worker-first-run.log`, `post-failure-stable.json`, `shinhan-connectivity-diagnostic.json`에 있다. wrapper `worker-compose.sh`/`mcp-compose.sh`는 해당 project와 `/etc/cardrag/<role>.env`, base+secrets overlay를 사용한다. 마이그레이션 apply JSON은 stdout 연결 소실로 비었으나 Docker exit 0과 실제 전량 verify를 별도로 보존했다. 빈 JSON을 성공 값으로 채우거나 이전을 다시 수행하지 않는다.

## 3. 신한 장애 표본

공식 모바일 URL: `https://www.shinhancard.com/mob/MOBFM12051N/MOBFM12051R01.shc?page=CRE`. 기존 discovery는 PC `.../hpp/HPPCARDN/HPPPdPbnA01C.shc?creChkCcd=2`와 목록 `.../hpp/HPPCUSTMN/CrdPdPbn02.ahtml`. 다운로드 refresh는 모바일 landing/`MOBFM12051R01C.ajax`/`MOBFM12051R03.shc`를 이미 사용한다.

현 운영 호스트는 curl/Chrome/Firefox 모두 HTTP 응답 전 reset. DNS 210.112.177.1, TLS 인증서 검증은 정상. 사용자 확인으로 다른 회선은 정상, 같은 공유기 별도 Ubuntu 서버에서도 curl (56) reset. 공통 출구 IP 접근 제한 또는 회선 경로가 의심되지만 신한 IP 차단 확정은 아니다.

이번 목적은 신한 복구를 기다리지 않고 나머지 수집을 완수하는 것이다. 신한을 enabled 목록에서 제거하거나 URL/source identity를 임의 변경하지 않는다. 실HTTP는 짧고 유한하게 실행한다. 당일 연결이 복구돼도 가짜 reset 테스트로 기능을 검증할 수 있으므로 외부 장애 지속을 필수 조건으로 삼지 않는다. 프록시 설치·공인 IP 변경·TLS 검증 해제는 범위 밖이다.

## 4. 코드 조사 결과와 작업 위치

| 파일(저장소 상대 경로) | 현재 동작 / 변경 지점 |
|---|---|
| `apps/cardrag-worker/src/cardrag_worker/pipeline.py` | `_run_locked` 약 2865: discovery 순차 for loop, 예외 전체 전파. 약 3082~3290: `acquire_source` + `bounded_ordered_map`, PDF는 이미 전역/issuer별 병렬이지만 실패 시 전체 중단. 이후 revision expansion→corpus diff/retirement→acquisition receipt→OCR→export/seal/publish |
| `.../bounded.py` | 기존 제한 병렬 scheduler. PDF용 동작을 무작정 바꾸지 말고 issuer 결과 경계/안전한 outcome으로 재사용 |
| `.../contracts.py`, `issuers/registry.py`, `issuers/*.py` | IssuerAdapter/IssuerSpec/SourceSnapshot. 등록 8개: woori,kb,shinhan,samsung,hyundai,hana,lotte,bc. adapter 파서·source ID 계약 보존 |
| `.../issuer_http.py`, `rate_limit` 관련 실제 모듈 | client pools, 현대 exact-origin TLS 예외, issuer/host rate limit. discovery cookie/session도 issuer별 분리 |
| `.../settings.py`, `cli.py`, `performance.py`, `state.py` | 설정 배선, 결과 요약/실패 분류, run receipt/resume. 기존 stage 상태 체계 유지 |
| `.../corpus_diff.py`, `corpus_baseline.py`, `retirement.py` | 실패 issuer를 missing/retired로 오판하지 않도록 명시적 fresh issuer 집합 연결 |
| `.../exporter.py`, `exporter_v5.py`, `ocr.py`, `webdav.py`, core manifests | 기존 문서·CAS 재사용, 정확한 OCR variant 유지, merged corpus/DB/vector/manifest 일치 |
| `deploy/worker/compose*.yaml`, `deploy/simple.env.example`, `docs/OPERATIONS.md` | 새 설정, partial 결과/감시/롤백 안내. OpenCode overlay는 opt-in 유지 |

`...`는 `apps/cardrag-worker/src/cardrag_worker/`이다. 관련 코드 정의를 먼저 좁게 검색한다. 이전 handoff 증거 스크립트 전체를 lint/스캔 대상으로 넓히지 않는다.

## 5. 필수 설계 — 실패를 로그로 남기고 계속하되 서비스 데이터 보존

### 5.1 수집 단계 경계와 제한 병렬

1. discovery를 issuer별 task로 실행한다. 각 task는 독립 client/cookie jar, 해당 issuer limiter, 유한 retry/deadline을 갖는다. 완료 결과는 registry 순으로 정렬하여 corpus identity에 task 완료 순서가 영향을 주지 않게 한다.
2. 정상 discovery 후 PDF 수집은 기존 global `pdf_concurrency`/`pdf_concurrency_per_issuer` scheduler를 재사용한다. 최소 구현은 **병렬 discovery barrier → 병렬 PDF barrier → OCR**이다. 새 issuer task와 기존 PDF semaphore를 곱하여 동시 요청 수를 늘리지 않는다. 단계 전체를 새 framework로 교체할 필요는 없다.
   기존 `bounded_ordered_map`은 operation 예외를 받으면 TaskGroup의 나머지를 취소한다. 따라서 issuer 로컬 오류는 operation 안에서 typed failure outcome으로 변환하여 반환해야 한다. scheduler의 전체 fail-fast 동작을 제거해 공용 오류까지 삼키지 않는다. 이미 격리된 issuer의 대기 PDF 항목은 origin 호출 없이 명시적 skipped-by-issuer outcome을 반환하게 하여 기존 전체 입력 drain 검증과 정합성을 유지할 수 있다.
3. 실패 outcome을 데이터로 반환하여 한 issuer의 예외가 다른 issuer task를 취소하지 않게 한다. `CancelledError`, 프로세스 종료·worker.lock·디스크·SQLite·공용 WebDAV 오류는 issuer 실패로 삼키지 않는다. catch 범위는 adapter discovery/prepare/origin download/목록 품질 판정에 둔다. exception 타입만 보고 내부 cache/state 오류까지 네트워크 오류로 분류하지 않는다.
4. origin reset/timeout/HTTP 접근제한, 파서가 정상 목록을 만들지 못함, minimum_records/retention 비율 미달은 issuer 수집 실패로 격리한다. 파서 버그는 reason과 진단 경고를 남기되 다른 issuer를 막지 않는다. 알려진 DRM unsupported는 기존 명시적 처리 유지.
5. discovery와 download 양쪽 실패에 대응한다. discovery만 catch하면 PDF 단계에서 같은 장애가 전체 중단되는 문제는 남는다. download 중 issuer 장애가 확인되면 새 해당 issuer 작업 투입을 중지하고 진행 중 작업은 안전하게 정리/회수한다. 유한 retry budget 안의 일시 오류는 기존대로 재시도하며, 소진 후 그 issuer를 격리한다. cache 적중이 아닌 수천 origin 요청으로 장애를 확인하지 않는다.
6. issuer가 도중 실패하면 그 issuer의 신규 부분 결과는 이번 generation 입력에서 제외하고 마지막 서비스 상태를 유지하는 **issuer 단위 원자적 채택**을 기본으로 한다. 이미 다운로드한 캐시 파일은 정상 캐시로 남겨 다음 run 재사용하되 fresh discovery/success로 확정하지 않는다. 다른 성공 issuer의 자료는 채택한다.

### 5.2 설정 기본값과 시간 경계

- `CARDRAG_ISSUER_DISCOVERY_CONCURRENCY=4`(범위 1~8): 신규 설정. 기존 PDF 기본 전역 8, issuer별 2 및 issuer rate limit 유지(실제 settings 확인).
- `CARDRAG_ISSUER_DISCOVERY_TIMEOUT_SECONDS=300`: issuer의 discovery 전체 retry 포함 상한. 검증에서 짧게 override 가능. 정상 카드사의 목록 크기를 고려하여 설정 가능하게 한다.
- download는 기존 요청 timeout/유한 stage attempts에 더해 issuer 장애 시 신규 투입 차단으로 요청 폭증을 막는다. 이 범위에서는 전체 issuer 다운로드에 임의의 짧은 고정 시간 상한을 걸어 수백 정상 PDF를 중단시키지 않는다.
- 설정/env/Compose/CLI에 일관되게 연결하고 잘못된 값은 startup validation에서 거부한다. issuer 로컬 실패 격리가 기본 동작이며 8개 등록은 그대로 유지한다.

### 5.3 실패 issuer의 기존 자료 carry-forward

1. run 시작에 **실제 서비스 중 stable generation**을 검증된 기준으로 고정한다. rolling baseline/seed는 비교 보조이며 서비스된 적 없는 실패 run snapshot을 carry 기준으로 삼지 않는다.
2. 실패 issuer의 stable 문서 전부(current·historical·availability와 temporal/supersedes 관계 포함)를 유지한다. SourceRecord는 기존 canonical snapshot/manifest/검증된 seed identity에서 복원한다. 누락 metadata를 추측하거나 신규 source/document ID를 만들지 않는다.
3. 가능한 기존 pipeline 경로를 사용하되 origin 재접속 없이 local PDF CAS 또는 stable PDF CAS를 SHA/크기 검증 후 사용한다. OCR은 stable ArtifactRef/variant pin으로 재사용하며 embedding/chunk도 동일 계약이면 기존 검증된 결과를 재사용한다. exporter에 들어가는 문서·검색 DB·vector·aggregation·generation manifest가 동일 corpus를 가리켜야 한다. manifest만 뒤에 붙여 검색 인덱스에서 빠지는 구현은 불합격이다.
4. carry 문서에 origin freshness 성공 시각을 새로 쓰지 않는다. collection report에 stale/carry 기준 generation/마지막 실제 성공 시각을 명시한다. 첫 007 계약 전환에서 레거시 OCR 참조를 새 content variant로 연결해야 하면 **문서별 동일 OCR SHA를 보존**한다. 기존 pin과 migration 매핑을 재사용한다.
5. 이전 자료가 없는 신규 issuer가 실패하면 해당 issuer 0건·unavailable로 보고하고 성공 issuer만 계속한다. stable에 해당 issuer 자료가 있는데 보존에 필요한 CAS/identity가 손상돼 검증할 수 없으면 이것은 공용 데이터 무결성 실패다. 기존 stable은 그대로 두고 새 generation 게시를 막으며 성공 issuer의 수집/캐시는 보존한다. 임의 누락 게시나 재OCR로 우회하지 않는다.
6. baseline과 PDF pruning은 carry 문서까지 포함한 **완성된 published corpus**로 갱신한다. 장애 issuer의 PDF/OCR CAS를 이번 run에서 사용하지 않았다는 이유로 정리하지 않는다.

### 5.4 단종·변경 판정·요청 재개

- `fresh_successful_issuers`만 absence/retirement 신규 관측을 만들 수 있다. 실패 issuer는 단종 후보·grace 성공횟수·단종 전이를 이번 run에서 진행하지 않는다. wall-clock 경과만으로 복구 첫 run에 단종시키지 않도록 실제 성공 목록 관측 조건도 보존한다.
- carry된 문서를 가짜 정상 discovery snapshot으로 저장하거나 `last_successful_snapshot_count`를 갱신하지 않는다. 정상 issuer의 완전 수집만 freshness 성공으로 확정한다.
- corpus diff는 carry를 포함한 전체 corpus로 수행하고, missing guard를 전역으로 끄지 않는다. fresh 성공 issuer의 실제 누락은 기존 단종 정책으로 처리한다.
- collection 상태와 실패 이유/시각 자체는 OCR cache key나 내용이 동일한 generation identity를 변경하지 않는다. 단, 실제 corpus/OCR/정책 schema 변화는 기존 규칙대로 반영한다. no-change에서도 이번 collection report를 반드시 남긴다.
- reprocess 요청이 실패 issuer 문서를 포함하면 그 대상은 미완료로 남기고 carry한다. 성공 issuer 대상은 처리 가능해야 한다. 기존 all-target 검증과 영수증 로직을 점검하여 일부 미처리를 전체 완료로 표시하지 않는다. 이번 계획에서는 실제 운영 재처리 요청을 만들지 않는다.
- resume는 성공/실패 issuer outcome과 채택/보존 경계를 영속 receipt로 고정한다. 이미 sealed된 run을 새 discovery로 바꾸지 않는다. 미sealed 재개 시 새 수집이 필요하면 기존 refresh_sources 규칙과 pin/snapshot 정합성을 보존하고 outcome을 명시적으로 새로 확정한다. 복구 issuer가 다음 신규 run에서 자동 재시도되는지 검증한다.

### 5.5 운영 결과·로그

`runs/<run_id>/reports/issuer-collection.json`에 schema_version, run_id, collection_status(`complete|degraded|failed`), issuer별 discovery/download status, reason_code, exception_class, attempts, elapsed_seconds, discovered/acquired/adopted/carry 문서 수, origin freshness 시각, carry 기준 generation을 기록한다. 본문·쿠키·자격·민감 query/raw response는 넣지 않는다. 실패 즉시 issuer/단계/reason 경고를 남기고 마지막 요약에도 실패 issuer를 보여준다.

- 하나 이상 issuer의 수집이 완전히 성공했고 전체 결과 검증·게시 또는 정당한 no-change를 완료하면 기존 run status `succeeded`/`no_change`, process exit 0, **collection_status=degraded 경고**를 남긴다. 기존 DB status enum을 무리하게 확장하지 않는다. exit 0을 모든 issuer 최신화 성공이라고 보고하지 않는다.
- 모든 enabled issuer가 실패하면 `collection_status=failed`, nonzero 종료, OCR/게시를 하지 않는다. carry 자료만으로 가짜 성공하지 않는다.
- OCR provider systemic 실패, 공용 storage/export/publication 무결성 실패는 기존대로 배치 실패다. 이번 변경이 모든 오류를 무조건 무시하는 동작이 되어서는 안 된다.

## 6. 구현 순서 — Executor 작업 지시

1. Git/운영 상태를 읽기 전용 확인. 신규 브랜치가 필요하면 현재 HEAD에서 `codex/008-parallel-issuer-collection`을 만든다. `main`에서 시작해 006/007을 빠뜨리지 않는다. 실행 중 Worker가 있으면 배포/동일 state 테스트는 하지 않는다.
2. typed issuer outcome/collection receipt와 가짜 adapter 최소 시나리오를 먼저 만든다. 기존 discovery loop를 bounded parallel로 바꾸고 issuer별 client/session 분리·terminal 결과 보존을 연결한다.
3. 기존 PDF scheduler에 origin 실패 outcome/issuer 격리/결정적 결과 채택을 연결한다. 기존 completed==len(records), terminal-stage 검증, acquisition receipt가 성공·DRM·격리 결과를 정확히 표현하도록 변경한다. 슬롯 client 쿠키 격리 유지.
4. stable carry-forward를 corpus gate 이전에 통합한다. prior OCR/variant/PDF/embedding 재사용 및 temporal 관계 보존. 결과는 deterministic sort. retirement·baseline·pruning·reprocess/resume에 fresh issuer 집합을 연결한다.
5. 설정/Compose/CLI/report/OPERATIONS 문서를 연결하고 §9의 필수 테스트를 통과시킨다. 새 framework, 의존성, 카드사 파서 개편은 도입하지 않는다.
6. 격리된 소형 WebDAV/가짜 HTTP pipeline에서 새 PDF 한 건을 가진 성공 issuer + Shinhan reset + carry 자료 조합을 end-to-end 검증한다. 재실행 OCR 0건, provider 교체 OCR 0건, no-change report까지 확인한다.
7. exact commit 후보 이미지로 실제 운영 설정을 검증하고 §10에 따라 1회 운영 배치를 시작한다. 장시간 실행 중 폴링하지 않고 컨테이너/감시 기준을 사용자에게 전달하고 REPORT에 진행 상태를 기록한 뒤 턴을 종료한다.
8. 사용자가 종료/오류를 알려주면 결과 검증·보고·인수 요청을 한다. 자체적으로 2일 예약 run 또는 수일 관찰을 새 필수 조건으로 추가하지 않는다.

## 7. 비용·자료·권한 경계

- 기존 5,172 variant 이전/5,512 stable 매핑을 동일 stable에 대해 다시 apply하지 않는다. stable 변경 또는 cache 손상이라는 구체적 이유가 있을 때만 필요한 검증을 반복한다.
- 전량 OCR/Paddle 재처리, 전량 강제 재임베딩, epoch 증가, `reprocess --apply`, `restore --apply`, `--adopt-latest`는 이번 검증 범위 밖이다. 신규 PDF 정상 OCR만 허용한다.
- 새 provider 품질 실사는 기존 근거를 재사용한다. 필요시 공개 합성 1페이지 1회만, 시간 상한 60초·자동 재시도 없음. 전체 corpus discovery/OCR로 OpenCode를 재평가하지 않는다.
- 큰 state/WebDAV 전체 clone을 만들지 않는다. 작은 fixture/격리 prefix 사용, 불변 OCR/CAS 원본 삭제 금지, GC false 유지, 운영 rollback 최대 1세트.
- 공용 WebDAV/SQLite 오류, 불충분 disk preflight는 숨기지 않는다. 시작 여유 공간은 discovery 결과에 따라 동적으로 계산한다.
- `/opt/cardrag`는 sudo 없이 조작 가능. `/etc/cardrag`/systemd 쓰기 권한은 있다고 가정하지 않는다. 쓰기가 불가능하면 기존처럼 새 배포 디렉터리의 운영 overlay/image override로 구성한다. 비밀값 복사/출력 금지.

## 8. 인수 기준

1. 동시 discovery가 실제로 겹쳐 실행되고 상한을 지킨다. PDF 전역/issuer 동시성·rate limit·cookie isolation도 보존된다.
2. Shinhan reset/discovery timeout/목록 불량/prepare 실패/download 실패를 격리하고 다른 issuer 수집→OCR→export→게시 또는 no-change가 끝난다. OCR은 모든 collection task가 terminal된 뒤 시작한다.
3. 실패 issuer의 기존 서비스 문서가 누락·단종되지 않고 PDF/OCR SHA/크기·temporal 관계가 유지된다. 신규 실패 issuer는 false success로 기록하지 않는다.
4. 실제 신한 실패 상태에서도 나머지 성공 issuer의 run이 exit 0 + degraded 경고로 정상 마감될 수 있다. 모두 실패하면 nonzero이고 OCR/게시 없음.
5. 기존 동일 PDF의 OCR provider 호출 0, 문서별 OCR SHA 보존, provider 변경만으로 cache/generation 흔들림 없음. 신규 PDF 호출은 별도 집계.
6. generation manifest와 검색 DB/vector/aggregation corpus가 일치하고 새 MCP가 해당 generation을 서비스한다. 정당한 no-change면 게시를 강제로 만들지 않는다. 007 계약 첫 전환이면 새 generation과 기존 OCR 보존을 확인한다.
7. 다음 신규 run에서 실패 issuer를 다시 수집하며 복구 시 정상 채택·freshness/retirement 판단으로 복귀한다. resume/reprocess 미완료 대상과 cache 선택 결정성이 유지된다.
8. 로그와 REPORT에서 partial과 complete를 구분하며 timer/중복 실행/rollback/비밀 보호 조건이 유지된다. 006 provider 운영 활성화 여부는 별도 항목으로 보고한다.

## 9. 필요한 테스트와 검증 — 과잉 실사 금지

### 자동 테스트 필수 시나리오

- asyncio Event/barrier로 두 issuer discovery가 동시에 시작했음을 확인; 실행시간 추측 기반 flaky 테스트 금지. 완료 순서가 달라도 동일 corpus/identity.
- 한 issuer reset 및 무응답 deadline, 여러 issuer 동시 실패, 모든 issuer 실패. 정상 issuer 작업이 취소되지 않음. deadline/cancel 후 잔류 task 없음.
- discovery 최소건수/retention 판정 실패, prepare_download 실패, mid-download 실패. issuer 부분 신규 자료는 게시에서 제외, 다운로드 캐시는 다음 run 재사용.
- 기존 stable Shinhan PDF/OCR/variant/current/history가 carry되고 검색 결과에 남음. 새 정상 issuer PDF는 OCR 처리. 실패 issuer의 단종 후보/grace/last-success 시각 불변.
- 다음 run Shinhan 복구·실제 성공 목록의 진짜 누락·신규 PDF는 정상 기존 정책으로 판정. 실패 issuer baseline/prune 보호.
- 손상된 carry CAS/identity 또는 공용 SQLite/WebDAV 오류는 partial 성공으로 숨기지 않고 기존 stable 유지. known DRM 경로 회귀 없음.
- no-change에서도 degraded report 존재, status/CLI exit 일관성. reprocess 요청의 실패 issuer 대상 미완료와 재개 결정성.
- codex→opencode 모델/effort 변경, legacy→content 첫 계약 전환, 동일 PDF 여러 OCR pin 모두 재사용/바이트 불변. fake provider 호출 횟수 직접 검증.

기존 테스트 중심: `apps/cardrag-worker/tests/test_pipeline.py`, `test_pipeline_v5.py`, `test_corpus_gate_v130_integration.py`, `test_corpus_baseline_v130.py`, `test_retirement_v130.py`, `test_issuers.py`, `test_issuer_http.py`, `test_content_cache.py`, `test_content_migration.py`, `test_content_webdav_rehearsal.py`, `test_ocr_recovery.py`, `test_cli_settings_provider.py`, `test_gc.py`. scheduler 테스트는 실제 `bounded.py`의 기존 테스트를 검색하여 추가한다.

수정 단계에서는 관련 테스트만 실행한다. 코드 완성 후 저장소 CI와 같은 Worker/core/MCP 전체 suite 1회, Ruff check/format, mypy, `git diff --check`, 실제 Compose config를 검증한다. secret/overlay 변경이 있으면 변경분 gitleaks, shell 변경 shellcheck, Dockerfile 변경 hadolint를 도구가 있을 때 실행한다. 같은 commit CI가 필수 gate를 완료했다면 동일 전체 검증을 반복하지 않는다. 외부 신한 통신을 unit test 성공 조건으로 두지 않는다.

## 10. 운영 반영과 사용자 감시

### 10.1 통합 코드 배포와 007 잔여 인수

1. 현재 stable baseline과 실행중 Worker 유무·timer 다음 시각·disk·config를 확인한다. 기존 007 migration/verify 증거가 같은 stable인지 비교한다. 유효하면 재apply/전수 스캔 생략. 데이터 상태가 바뀌었으면 필요한 현재 매핑만 검증한다.
2. exact commit의 새 MCP/Worker 후보 이미지를 고유 태그로 빌드하고 OCI revision/local ID를 기록한다. `/opt/cardrag/008-<shortsha>`에 소스/운영 overlay를 준비한다. **MCP schema 소비 변경이 있을 때 MCP 먼저** 준비 상태를 확인하고 Worker 전환한다. MCP 변경이 없으면 현재 healthy MCP를 불필요하게 재시작하지 않는다.
3. 기존 Compose project/state/auth volume과 secrets를 유지한다. image 선택은 env·secrets overlay의 우선순위까지 확인한다. OCR provider는 우선 기존 codex/qwen을 유지한다. GC false, epoch 0, pending reprocess 없음 확인.
4. timer 중지가 권한상 불가능하면 같은 worker.lock, 다음 예약 시각, 기존 실행 유무로 중복 방지한다. 잠금 충돌은 두 번째 Worker 기동 이유가 아니다. timer 활성 상태를 유지/복구하고 적용 변경을 기록한다.
5. 현재 symlink와 overlay를 새 코드로 맞춘 뒤 **일반 run을 딱 1회**, detached named container `cardrag-prod-008-first`로 시작한다. `--rm` 금지. 기존 failed 007 컨테이너는 새 실행 감시 대상으로 안내하지 않는다. 새 실행이 신한 전체 접근 성공을 기다리게 하지 않는다.
6. 시작 명령 형태는 검증된 새 wrapper `worker-compose.sh run -d --no-deps --name cardrag-prod-008-first worker run`이다. 실제 image/command/volumes/AutoRemove=false/running을 한 번 확인한다. 장기 기동은 여기서 턴 종료한다. 자동 반복 restart/wait/poll controller를 만들지 않는다.
7. 사용자 안내: `docker logs -f --tail 50 cardrag-prod-008-first`; 종료 판정 `docker inspect cardrag-prod-008-first --format '{{.State.Status}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}}'`. running의 ExitCode 0은 완료가 아니다. `docker ps -a`에서 종료 결과 보존 확인.
8. 사용자 종료 통보 뒤 collection report/exit/신한 carry/기존 OCR SHA·호출수/신규 PDF 수/단종 영향/전체 corpus/MCP loaded generation을 확인한다. 성공해도 신한 최신화는 미완료로 표시한다. 007 결함·자료 누락·무리한 OCR 재호출이면 보존된 직전 코드/이미지/설정 1세트로 롤백한다. 추가형 OCR 캐시는 삭제하지 않는다.

### 10.2 006 운영 활성화의 별도 마지막 게이트

코드/격리 검증 통합은 이번 과제에 포함되지만 **기존 사용자는 007 단계 7(OpenCode 운영 활성화)을 별도 승인 대상으로 두었다.** 현재 계획 작성 요청을 해당 운영 provider 변경 승인으로 간주하지 않는다. 10.1 성공 후 REPORT에 바로 적용 가능한 overlay·모델·effort·secret 제공 여부·fallback·롤백 변경 목록을 정리한다. 새 승인이 이미 도착했다면 반복 질문하지 않고 진행한다. 승인 전에는 이 항목을 미실행으로 명시한다.

활성화 시 `compose.opencode.yaml`을 opt-in하고 opencode/`alibaba-token-plan/qwen3.8-flash`/medium/external_allowed와 도구 deny·secret을 확인한다. 기존 provider를 fallback으로 사용할 경우 실제 codex 모델과 provider별 effort가 정확한지 검증한다. Paddle fallback으로 장시간 과제를 새로 만들지 않는다. 캐시된 문서는 그대로 재사용하고 신규 PDF만 OpenCode 호출을 허용한다. env/overlay 원복과 직전 image/config로 rollback하며 variant 원본은 보존한다. 신규 PDF가 없고 호출 0건인 run도 정상이다. 승인되더라도 전량 재OCR로 실호출 증거를 만들지 않는다. 수일 대기·신규 10건 의무는 이번 통합 인수 gate로 추가하지 않는다.

## 11. REPORT 작성과 종료

Executor는 이 디렉터리의 **새 `REPORT.md`**에 역할/commit/diff/코드 변경, 정확한 테스트 명령·결과, image/config/volume 대응, 단계별 시각, collection 요약, stable/generation 전후, 문서별 OCR 보존 비교·호출수, 실패 issuer stale 상태·단종 영향, timer/disk/rollback, 006 활성화 여부, 편차·미완료·사용자 다음 감시 기준을 작성한다. 장기 Worker 실행 중이면 REPORT를 진행 보고로 남기고 사용자 종료 알림 후 추가 기록한다. 실행 성공을 기다린 것처럼 쓰지 않는다. 기존 handoff 문서는 덮어쓰지 않는다.

완료 판정은 **신한 장애가 있어도 나머지 작업 완료·신한 기존 자료 보존·007 content 재사용·서비스 정상**에 근거한다. 공인 IP 차단 해제, 정식 GitHub/Docker Hub 릴리스, PR main 병합은 별도 과제이며 이번 기능 인수를 지연시키는 필수 조건으로 만들지 않는다.
