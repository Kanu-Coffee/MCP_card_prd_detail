# 완료 체크리스트

## 1. 판정 원칙과 현재 결론

이 문서는 CardRAG MCP v1의 완료 상태를 추적하는 단일 장부다. 계획 문서의 존재만으로
완료 처리하지 않고, 코드·설정과 재현 가능한 자동 검증을 함께 요구한다.

- `[x] [검증 완료]`: 현재 개발 환경에서 구현과 자동 검증 증거가 모두 존재한다.
- `[ ] [실환경 검증 대기]`: 실제 계정·외부 endpoint·장시간 corpus가 필요하다. 개발 목표를
  막지 않으며 [운영 인계 문서](REAL_ENV_HANDOFF.md)에 대체 증거와 실행 절차가 있다.
- `[ ] [운영 인계]`: 운영 host·secret·수동 승인처럼 운영자가 수행할 변경이다. 개발 목표를
  막지 않는다.
- `[x] [범위 제외]`: 사용자가 v1에서 명시적으로 제외했거나 후속 과제로 보류했다.

기준일: 2026-08-12

**현재 개발 환경에서 구현·자동 검증할 수 있는 v1 항목은 완료됐다.** 실제 카드사와
provider 계정, 운영 host, public image 승인 및 수일짜리 전체 corpus 실행만 남았으며 이를
완료로 가장하지 않는다. 최종 회귀 결과와 source revision은 이 문서와
`reports/deployment/dev-environment-verification.json`에 함께 기록한다.

## 2. 기준 문서와 기술 결정

- [x] [검증 완료] 레거시 코드·데이터·검색·운영 위험을 read-only로 분석했다.
  증거: `LEGACY_PROJECT_ANALYSIS.md`
- [x] [검증 완료] 요구사항 문서 9개, 운영 인계 문서와 ADR 5개를 단일 링크 구조로 유지한다.
  증거: `docs/README.md`, `docs/01_PROJECT_OVERVIEW.md`~`docs/08_COMPLETION_CHECKLIST.md`,
  `docs/REAL_ENV_HANDOFF.md`, `docs/adr/`
- [x] [검증 완료] 우리카드·KB국민카드와 신한카드 개인 신용·체크 현재본/과거 이력을 v1
  범위로 고정하고, issuer가 없는 document/evidence identity를 거부한다.
  증거: `src/cardrag/domain/`, `docs/adr/0002-identities-and-lineage.md`,
  `tests/unit/test_identity.py`
- [x] [검증 완료] PostgreSQL+pgvector/FTS, content-addressed object, immutable generation,
  1,536차원 embedding과 공통 evidence ID hybrid를 선택했다.
  증거: `docs/adr/0001-storage-search-and-generations.md`, `src/cardrag/search/`
- [x] [검증 완료] 결정론적 구조 분석, Codex/OpenRouter 역할, 품질 합격선과 합성 gold set을
  확정했다. 실제 파이프라인 산출 ID를 real hybrid 경로로 검색하며 정답 ranking을 주입하지 않는다.
  증거: `docs/adr/0003-models-structure-and-quality-gates.md`,
  `tests/fixtures/gold/`, `scripts/run_fixture_quality.py`,
  `reports/quality/fixture-gate.json`
- [x] [검증 완료] durable job·fencing·generation 게시 protocol과 HTTP/Auth/SLO 결정을 ADR로
  고정했다.
  증거: `docs/adr/0004-durable-jobs-and-publication.md`,
  `docs/adr/0005-http-auth-and-initial-slo.md`
- [ ] [실환경 검증 대기] 카드사 공시자료의 수집·재배포·상업적 이용 조건은 공개 운영 전
  확인한다. 확인 전 source PDF는 승인된 `source_pdf` 사용자에게만 제공한다.
  인계: `docs/REAL_ENV_HANDOFF.md` 1절

## 3. 신규 기반, 저장소와 durable 상태

- [x] [검증 완료] 레거시와 분리된 Python 3.12 package, locked dependency와 신규 source tree를
  만들었다. 레거시 package를 runtime import하지 않는다.
  증거: `pyproject.toml`, `uv.lock`, `src/cardrag/`
- [x] [검증 완료] issuer adapter, source record, document/evidence identity, artifact manifest와
  lineage를 strict typed contract로 구현했다.
  증거: `src/cardrag/domain/`, `src/cardrag/issuers/base.py`,
  `tests/unit/test_identity.py`, `tests/unit/test_manifests.py`
- [x] [검증 완료] PDF/OCR object는 SHA-256 content address로 불변 저장하며 경로 탈출,
  absolute path, NUL과 symlink escape를 거부한다.
  증거: `src/cardrag/storage/`, `tests/unit/test_storage.py`
- [x] [검증 완료] PostgreSQL migration 1~13의 checksum drift를 fail closed로 검사하고
  generation·artifact·evidence 불변성과 역할 권한을 DB에서도 강제한다.
  증거: `src/cardrag/db/`, `tests/integration/test_database_roles.py`,
  `tests/integration/test_generation_lifecycle.py`
- [x] [검증 완료] queued/running/retry_wait/succeeded/dead_letter/cancelled, attempt history,
  원자 claim, lease·heartbeat·fencing·retry·redrive·cancel을 구현했다.
  증거: `src/cardrag/jobs.py`, `tests/integration/test_postgres_jobs.py`
- [x] [검증 완료] lease 상실·cancel race에서 늦은 worker의 stage/finish/fail commit을 거부하고
  장기 worker 자체는 종료시키지 않는다.
  증거: `src/cardrag/pipeline/runtime.py`, `tests/unit/test_runtime.py`,
  `tests/unit/test_observability.py`
- [x] [검증 완료] 개별 job 취소를 issuer 성공이나 no-change로 오인하지 않고 run·candidate를
  실패 처리하며, 취소된 최신 문서 작업은 generation coverage gate에서도 다시 차단한다.
  증거: `src/cardrag/scheduler.py`, `src/cardrag/generation_builder.py`,
  `tests/integration/test_scheduler_recovery.py`, `tests/integration/test_generation_lifecycle.py`
- [x] [검증 완료] bulk supervisor가 중단돼도 run ID로 기다림·평가·seal·publish를 재개하는
  `cardrag run finalize` 경로가 있다.
  증거: `src/cardrag/cli.py`, `tests/integration/test_offline_pipeline_e2e.py`

## 4. 카드사 수집과 PDF 획득

| 카드사 | v1 구현 | fixture/contract | live 상태 |
|---|---|---|---|
| 우리카드 | 현재본·이력 discovery와 RAONK download protocol | 검증 완료 | 실환경 검증 대기 |
| KB국민카드 | 설정 가능한 5개 category, 현재본·이력 | 검증 완료 | 실환경 검증 대기 |
| 신한카드 | 개인 신용·체크 현재본·이력, 법인·선불 제외 | 검증 완료 | 실환경 검증 대기 |
| 그 외 카드사 | v1 범위 아님 | 해당 없음 | 범위 제외 |

- [x] [검증 완료] 세 adapter가 동일 Protocol과 normalized record를 사용하고 current/history,
  pagination, natural version, empty/anomalous markup, duplicate와 tombstone 계약을 통과한다.
  증거: `src/cardrag/issuers/`, `tests/unit/test_issuer_contract.py`
- [x] [검증 완료] content hash를 권위로 삼아 같은 파일명/크기의 내용 변경과 물리 중복을
  구분하며, 새 generation에서 동일 document는 안전하게 materialize한다.
  증거: `tests/unit/test_download.py`, `tests/integration/test_offline_pipeline_e2e.py`
- [x] [검증 완료] scheme/issuer host/redirect allowlist, SSRF 주소, timeout, streaming 상한,
  MIME·`%PDF`·PyMuPDF page 검증과 partial cleanup을 시험했다.
  증거: `src/cardrag/acquisition/download.py`, `tests/unit/test_download.py`
- [x] [검증 완료] issuer별 rate/backoff·retry와 markup 급감/0건 anomaly를 정상 성공으로
  숨기지 않으며 한 issuer 실패가 다음 issuer를 막지 않는다.
  증거: `src/cardrag/scheduler.py`, `tests/unit/test_scheduler.py`
- [x] [검증 완료] 세 issuer fixture 6문서를 실제 parser부터 download/job graph까지 BULK로
  처리하고 발견·성공 accounting을 검증했다.
  증거: `tests/integration/test_offline_pipeline_e2e.py`
- [ ] [실환경 검증 대기] 세 카드사의 실제 endpoint, markup, rate limit과 redirect를 제한
  pilot으로 확인한다.
  인계: `docs/REAL_ENV_HANDOFF.md` 1절

## 5. OCR, 구조 분석과 chunk

- [x] [검증 완료] OCR은 온라인 MCP와 분리된 worker에서 page/chunk checkpoint, 임시 파일,
  hash 검증과 atomic object finalize를 사용한다.
  증거: `src/cardrag/pipeline/ocr.py`, `src/cardrag/pipeline/runtime.py`
- [x] [검증 완료] BULK는 Codex-only, daily는 Codex 우선·승인 retry 뒤 OpenRouter fallback이며
  provider/model 변경 시 전체 문서를 새 attempt로 처리해 결과를 섞지 않는다.
  증거: `tests/unit/test_ocr.py`, `tests/unit/test_runtime.py`
- [x] [검증 완료] 실제 argv의 model·prompt·render 설정과 input/output hash를 lineage에 남기며
  모든 page marker, 순서, 숫자·단위·부정·제외 token gate를 강제한다.
  증거: `src/cardrag/pipeline/ocr.py`, `src/cardrag/quality.py`,
  `reports/quality/fixture-gate.json`
- [x] [검증 완료] worker crash 뒤 DB checkpoint와 content object의 input/output hash를 검증해
  새 fencing workspace로 복원하고 완료 page를 재호출하지 않는다.
  증거: `tests/integration/test_offline_pipeline_e2e.py`
- [x] [검증 완료] 동시 attempt는 generation/job/attempt/fencing별 workspace로 격리되어 다른
  backend의 페이지 결과를 섞지 않는다.
  증거: `tests/unit/test_ocr.py`, `tests/unit/test_runtime.py`
- [x] [검증 완료] canonical OCR은 불변이며 결정론적 분석이 heading/table/fact와 taxonomy,
  confidence, extraction method, exact ordered source spans를 만든다.
  증거: `src/cardrag/pipeline/structure.py`, `tests/unit/test_structure.py`
- [x] [검증 완료] 혜택·조건·전월실적·제외·각주 관계와 연회비 table context를 보존하고,
  원문에 없는 값·cross-document span·quote hash 불일치를 거부한다.
  증거: `tests/unit/test_structure.py`, `tests/unit/test_chunks.py`
- [x] [검증 완료] multi-span chunk는 비연속 원문을 단일 envelope로 가장하지 않고 ordered
  `source_spans`를 evidence와 MCP까지 유지한다.
  증거: migration 011, `src/cardrag/pipeline/chunks.py`,
  `tests/integration/test_pgvector_search.py`
- [x] [검증 완료] 합성 gold gate에서 page coverage/order, critical token recall,
  source-span accuracy가 모두 100%이고 중대 오류가 0이다.
  증거: `reports/quality/fixture-gate.json`
- [ ] [실환경 검증 대기] 실제 Codex device authorization, 외부 모델 OCR 품질·장기 갱신과
  실제 카드사 layout을 같은 evaluator로 재검증한다.
  인계: `docs/REAL_ENV_HANDOFF.md` 2~4절

## 6. 임베딩, hybrid 검색과 generation

- [x] [검증 완료] token-bounded 관계 문맥 chunk에 issuer/product/document/version/section와
  exact source spans를 포함한다.
  증거: `src/cardrag/pipeline/chunks.py`, `tests/unit/test_chunks.py`
- [x] [검증 완료] embedding 응답의 count/order/model/dimension/finite 값과 input policy를
  검증하고 model/hash가 다른 stale vector를 현재 세대에 혼합하지 않는다.
  증거: `src/cardrag/search/embeddings.py`, `tests/unit/test_embeddings.py`
- [x] [검증 완료] lexical과 pgvector ANN branch가 같은 stable evidence ID를 RRF 결합하고,
  issuer/version/as-of/section filter를 후보 SQL에 적용한다. Python BLOB 전수 scan은 없다.
  증거: `src/cardrag/search/`, `tests/integration/test_pgvector_search.py`
- [x] [검증 완료] 한 요청의 query embedding은 한 번만 사용하며 vector 장애는
  `allow_degraded=true`일 때만 명시적 lexical-only로 강등한다.
  증거: `tests/unit/test_hybrid.py`, `tests/unit/test_service.py`
- [x] [검증 완료] 검색 cursor는 query/filter/generation에 binding되고 다음 페이지가 비거나
  다른 질의에 재사용되지 않는다. evidence ID 조회는 요청 anchor부터 반환한다.
  증거: `tests/unit/test_service.py`, `tests/integration/test_pgvector_search.py`
- [x] [검증 완료] candidate는 snapshot expectation과 OCR/structure/embedding/index coverage를
  stage별 100% 대사하며 최신 실패·누락, 품질 report/hash 불일치와 generation split-brain을
  게시 전에 차단한다.
  증거: `src/cardrag/generation_builder.py`,
  `tests/integration/test_generation_lifecycle.py`
- [x] [검증 완료] seal·publish·in-flight pin·rollback·no-change·실패 7일·성공 최근 3개·pin
  retention과 DB/file compensation을 검증했다.
  증거: `src/cardrag/generation.py`, `src/cardrag/generation_builder.py`,
  `tests/unit/test_generation.py`, `tests/integration/test_generation_lifecycle.py`
- [x] [검증 완료] 실제 구조/chunk/deterministic embedding/hybrid 경로의 6개 gold query가
  Recall@10 1.0, critical Recall@10 1.0, filter accuracy 1.0, issuer collision 0,
  MRR 0.9167, nDCG@10 0.9385로 개발 gate를 통과했다.
  증거: `reports/quality/fixture-gate.json`
- [ ] [실환경 검증 대기] 실제 OpenRouter model/quota와 전체 corpus에서 품질·지연·resource를
  재측정해 초기 SLO를 조정한다.
  인계: `docs/REAL_ENV_HANDOFF.md` 3~4절

## 7. HTTP MCP, 인증과 source file

- [x] [검증 완료] MCP SDK 2.0 Streamable HTTP `/mcp`, public live/ready와 RFC 9728 protected
  resource metadata를 제공한다.
  증거: `src/cardrag/mcp_server.py`, `tests/unit/test_http_contract.py`
- [x] [검증 완료] `search_evidence`, `get_evidence`, `get_product_versions`,
  `get_source_page`, `get_source_pdf` tool과 catalog/document/evidence/OCR resource를 구현했다.
  증거: `tests/unit/test_service.py`, `tests/unit/test_http_contract.py`
- [x] [검증 완료] 응답은 issuer/product/document/version/effective date/generation/confidence,
  text/PDF hash와 ordered source spans를 보존하고 정보 부족·상충·degraded 상태를 명시한다.
  증거: `src/cardrag/service/models.py`, `tests/unit/test_service.py`
- [x] [검증 완료] Keycloak RS256 JWT의 signature/kid/issuer/audience/time/scope를 local JWKS로
  fail closed 검증하고 `search`와 `source_pdf`를 분리한다.
  증거: `src/cardrag/service/auth.py`, `tests/unit/test_auth.py`,
  `tests/integration/test_keycloak_oauth.py`
- [x] [검증 완료] 승인된 catalog ID만 PDF/OCR/PNG로 resolve하며 root containment,
  symlink, hash/MIME, 100 MB, GET/HEAD/Range 206·416, cache 7일과 감사 event를 시험했다.
  증거: `src/cardrag/service/source_files.py`, `tests/unit/test_http_contract.py`
- [x] [검증 완료] 일반 tool에는 임의 URL/path, 수집, OCR, rebuild, Gmail 또는 admin 동작이 없다.
  MCP DB role과 file volume은 read-only다.
  증거: migration 005/007, `compose.yaml`, `tests/integration/test_database_roles.py`
- [x] [검증 완료] limit/cursor, cancellation, 유한 request budget과 동시 요청 5개 admission을
  적용한다. 합성 100요청에서 오류 0, 관측 최대 동시성 5, P95 약 0.20초였다.
  증거: `tests/load/test_local_load.py`, `reports/benchmarks/local-search-load.json`
- [x] [검증 완료] readiness는 active DB/file generation, READY/checksum/schema/model/dimension과
  검색 저장소를 대사하고 불일치 시 503이다.
  증거: `tests/unit/test_http_contract.py`, `tests/integration/test_generation_lifecycle.py`
- [ ] [운영 인계] 실제 MCP client의 PKCE/offline refresh와 Nginx Proxy Manager/TLS hostname을
  운영 Keycloak에서 검증한다.
  인계: `docs/REAL_ENV_HANDOFF.md` 6절

## 8. 레거시 데이터 재사용

- [x] [검증 완료] 약 9.51 GiB, manifest 1,592건, unique PDF 1,405개, raw path 공백 731건,
  master `ocr_chars` drift 55건, OCR hash mismatch 1건과 historical embedding 10,967건을
  기준선으로 기록했다.
  증거: `LEGACY_PROJECT_ANALYSIS.md`, `docs/04_LEGACY_DATA_REUSE_GUIDE.md`
- [x] [검증 완료] source root를 read-only로 사용해 5문서 pilot을 별도 target에 복사했고
  hash lookup으로 누락 raw path 5건을 복원했다. source write 0, PDF/OCR object 각 5개,
  quarantine 0을 확인했다.
  증거: `src/cardrag/legacy/migration.py`, `reports/legacy-pilot-20260812.json`,
  `tests/unit/test_legacy_migration.py`
- [x] [검증 완료] OCR hash mismatch는 quarantine, character counter drift는 경고로 분리하며
  legacy structured/vector/email/archive를 current runtime corpus에 혼합하지 않는다.
  증거: `tests/unit/test_legacy_migration.py`, `.dockerignore`, `compose.yaml`
- [x] [검증 완료] pilot target rollback은 source를 건드리지 않고 생성 target만 명시적으로
  제거한다.
  증거: `cardrag legacy rollback`, `tests/unit/test_legacy_migration.py`
- [ ] [실환경 검증 대기] 전체 9.51 GiB 파일별 inventory·migration과 수일 BULK를 실제 volume
  용량 아래 수행한다.
  인계: `docs/REAL_ENV_HANDOFF.md` 4절

## 9. Docker, 운영, 관측성과 release

- [x] [검증 완료] MCP/worker/admin을 별도 `linux/amd64` target과 역할별 entrypoint allowlist,
  DB role, volume 권한으로 분리했다. non-root UID 10001, read-only rootfs, cap drop와
  no-new-privileges를 사용한다.
  증거: `Dockerfile`, `scripts/entrypoint.sh`, `compose.yaml`,
  `tests/integration/test_database_roles.py`
- [x] [검증 완료] MCP는 container `0.0.0.0:8000`, host `127.0.0.1:8000`에만 publish되고
  Keycloak도 loopback hand-off만 제공한다. Compose에 reverse proxy는 포함하지 않는다.
  증거: `compose.yaml`, `reports/deployment/dev-environment-verification.json`
- [x] [검증 완료] corpus·secret·Codex auth를 image/Git에서 제외하고 role별 read-only/RW
  volume과 Docker secret을 사용한다. Keycloak bootstrap secret은 1회 overlay 뒤 base
  Compose에서 제거된다.
  증거: `.dockerignore`, `.gitignore`, `deploy/secrets/README.md`,
  `deploy/keycloak/bootstrap.compose.yaml`
- [x] [검증 완료] Keycloak realm은 self-registration/DCR off, PKCE·Client Credentials,
  audience/scope, offline refresh rotation/reuse rejection/revoke와 90일 idle policy를 갖는다.
  증거: `deploy/keycloak/cardrag-realm.json`,
  `tests/integration/test_keycloak_oauth.py`
- [x] [검증 완료] Codex CLI 0.147.0은 system-owned OCR profile과 tool-less argv를 사용한다.
  bubblewrap canary가 rendered input만 읽고 secret/outside/write/socket을 거부한다.
  증거: `Dockerfile`, `deploy/codex/config.toml`, CI worker sandbox checks,
  `reports/deployment/dev-environment-verification.json`
- [x] [검증 완료] 매일 03:00 KST 우리→KB→신한, issuer 사이 10분 대기·실패 격리와 04:00
  retention one-shot을 CLI/systemd로 제공하고 scheduler lease heartbeat로 중복 실행을 막는다.
  증거: `src/cardrag/scheduler.py`, `deploy/systemd/`, `tests/unit/test_scheduler.py`
- [x] [검증 완료] allow-list JSON log, request/run/job/generation correlation, loopback metrics,
  queue·진행·ETA·retry/DLQ와 alert/runbook을 구현했다. query/token/body는 저장하지 않는다.
  증거: `src/cardrag/observability.py`, `deploy/monitoring/`,
  `tests/unit/test_observability.py`
- [x] [검증 완료] audit metadata는 90일, 익명 metric rollup은 1년 뒤 owner-only retention으로
  제거하고, 같은 one-shot이 최신 성공 3세대·active·pin을 보존하며 7일 지난 실패 generation을
  DB와 파일에서 제거한다. worker/MCP는 삭제 권한이 없다.
  증거: `src/cardrag/observability.py`, `src/cardrag/generation_builder.py`, migration 007,
  `tests/integration/test_observability_postgres.py`, `tests/integration/test_generation_retention.py`
- [x] [검증 완료] CI는 unit/integration/fixture/load, dependency license inventory, secret scan,
  3 image build, SBOM, Trivy와 Codex sandbox canary를 수행한다. release는 exact SHA의 성공 CI,
  semver tag, protected environment의 exact dependency-license attestation과 수동 승인 뒤
  역할별 digest를 push/sign한다. PyMuPDF legal 승인은 운영 인계이며 개발 Goal을 막지 않는다.
  증거: `.github/workflows/ci.yml`, `.github/workflows/release.yml`
- [x] [검증 완료] 개발환경의 세 이미지 취약점 검사 결과 HIGH/CRITICAL 0이고 local MCP
  100/100 동시 smoke가 통과했다. public registry push는 하지 않았다.
  증거: `reports/deployment/dev-environment-verification.json`
- [ ] [실환경 검증 대기] 실제 Codex 계정/device authorization과 장기 token 갱신을 검증한다.
  인계: `docs/REAL_ENV_HANDOFF.md` 2절
- [ ] [운영 인계] 운영 Keycloak/TLS client, systemd timer, generation rollback rehearsal을
  최종 host에서 수행한다.
  인계: `docs/REAL_ENV_HANDOFF.md` 6~8절
- [ ] [운영 인계] `vX.Y.Z` tag와 GitHub protected environment 수동 승인 뒤에만
  `ymtop59/mcp-card-prd-detail`에 역할별 digest를 push하고 Cosign을 검증한다.
  인계: `docs/REAL_ENV_HANDOFF.md` 5절
- [ ] [운영 인계] Proprietary 공개 image에 포함되는 PyMuPDF 1.28.2의 Artifex commercial
  license 증빙을 확인한 뒤에만 protected `CARDRAG_DEPENDENCY_LICENSE_ATTESTATION` secret을
  설정한다. AGPL 선택은 현재 attestation으로 승인할 수 없고 project/image license·notice·
  corresponding-source 경로와 policy를 별도 reviewed commit에서 먼저 바꿔야 한다. psycopg 계열
  LGPL 및 certifi MPL-2.0 의무도 release notice/compliance 기록으로 확인한다.
  인계: `docs/REAL_ENV_HANDOFF.md` 5절

## 10. 자동 검증과 완료 판정

- [x] [검증 완료] unit test가 identity, manifest, path, adapter, download, OCR, structure,
  chunk, embedding, hybrid, generation, auth, MCP, scheduler와 observability를 검증한다.
  증거: `tests/unit/`
- [x] [검증 완료] 깨끗한 PostgreSQL에서 migration 1~13, 원자 claim/lease, pgvector,
  generation lifecycle, 역할 권한, observability와 3 issuer full pipeline E2E가 통과한다.
  증거: `tests/integration/`
- [x] [검증 완료] E2E는 parser→download→fake Codex OCR→structure→embedding/index→worker
  restart/resume→seal/publish→authenticated HTTP MCP와 다음 세대 materialize까지 실행한다.
  증거: `tests/integration/test_offline_pipeline_e2e.py`
- [x] [검증 완료] gold quality, 합성 load, prompt/tool injection, SSRF/path, JWT 권한,
  secret redaction, image content와 sandbox 회귀를 CI gate에 포함했다.
  증거: `scripts/run_fixture_quality.py`, `tests/load/`, `.github/workflows/ci.yml`
- [x] [범위 제외] backup·restore/RPO·RTO 구현은 사용자 결정에 따라 v1 후속 과제다.
- [x] [범위 제외] Nginx Proxy Manager container와 public TLS 설정은 별도 hosting 과제다.
- [x] [범위 제외] ARM64 image, public admin API/web UI, 신한 법인·선불과 추가 카드사는 v1
  범위가 아니다.

### Goal 판정

개발 환경에서 구현하거나 fixture/mock/자동 통합시험으로 검증할 수 있는 체크리스트는 모두
완료했다. 남은 항목은 실제 계정·운영 host·법적 확인·장시간 corpus·수동 public release뿐이며,
각 항목의 불가 이유, 대체 검증, 실행 절차, 성공 조건과 실패 진단은
`docs/REAL_ENV_HANDOFF.md`에 기록했다. 따라서 이 목록 때문에 자동 작업을 반복하거나 실제 기기를
기다리지 않으며, 최종 회귀와 private GitHub source 게시가 성공하면 개발 Goal을 달성한 것으로
처리한다.
