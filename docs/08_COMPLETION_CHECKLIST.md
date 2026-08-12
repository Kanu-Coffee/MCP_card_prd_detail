# 완료 체크리스트

## 1. 사용 방법

이 문서는 신규 CardRAG MCP 시스템의 실제 완료 상태를 추적하는 단일 체크리스트다.

상태 표기:

- `[x] [검증 완료]`: 산출물과 재현 가능한 검증 증거가 존재한다.
- `[x] [결정 완료]`: 사용자가 제품·운영 방향을 승인했지만 구현 완료를 뜻하지 않는다.
- `[ ] [진행 중]`: 작업은 시작됐지만 완료조건을 모두 충족하지 않았다.
- `[ ] [미착수]`: 신규 구현 또는 검증 증거가 없다.
- `[ ] [결정 필요]`: 제품·운영 결정이 선행되어야 한다.
- `[ ] [검토 필요]`: 기술 지원 여부나 현실성을 실제 환경에서 확인해야 한다.

체크할 때는 항목 끝에 증거를 기록한다. 인정 가능한 증거는 versioned 파일 경로, test report, run ID, corpus/index generation ID, image digest, 복구 훈련 report 등이다. 계획 문서만 존재하는 것은 구현 완료 증거가 아니다.

기준일: 2026-08-12

## 2. 문서와 요구사항

- [x] [검증 완료] 레거시 구조·데이터·검색·운영 위험을 읽기 전용으로 분석했다.
  증거: `LEGACY_PROJECT_ANALYSIS.md`
- [x] [검증 완료] 요구된 개발 문서 9개를 작성하고 상호 링크·Markdown 구조·완료 상태 표현을 검토했다.
  증거: `docs/README.md`, `docs/01_PROJECT_OVERVIEW.md`부터 `docs/08_COMPLETION_CHECKLIST.md`
- [x] [결정 완료] 우리카드·KB국민카드를 우선 지원하고 신한카드를 BULK 시험 대상으로 추가한다.
  증거: 사용자 결정 2026-08-12, `docs/01_PROJECT_OVERVIEW.md`
- [x] [결정 완료] 기본 latest 조회, 전 버전 보존, 명시적 version/as-of 과거 조회를 승인한다.
  증거: 사용자 결정 2026-08-12, `docs/01_PROJECT_OVERVIEW.md`
- [x] [결정 완료] 운영 MCP의 HTTP 접속과 URL+token 방식을 승인한다.
  증거: 사용자 결정 2026-08-12, `docs/02_TARGET_ARCHITECTURE.md`
- [ ] [결정 필요] token 발급·만료·회전·폐기, 사용자/tenant와 tool별 권한을 승인한다.
- [x] [결정 완료] 초기 동시 요청 5개와 품질 우선 원칙을 승인한다.
  증거: 사용자 결정 2026-08-12, `docs/06_OPERATIONS_AND_DEPLOYMENT_GUIDE.md`
- [ ] [결정 필요] BULK pilot 후 목표 QPS, latency, 가용성과 resource 한도를 승인한다.
- [x] [결정 완료] 명시적 요청 시 저장 원본 PDF 파일 제공과 페이지 단위 원문 조회를 승인한다.
  증거: 사용자 결정 2026-08-12, `docs/03_COMPONENT_DEVELOPMENT_GUIDE.md`
- [x] [결정 완료] 공통 evidence key 기반 lexical/vector hybrid 검색을 승인한다.
  증거: 사용자 결정 2026-08-12, `docs/05_LLM_AND_DATA_QUALITY_POLICY.md`
- [ ] [결정 필요] 공시자료 수집·재배포·상업적 이용 조건을 확인한다.
- [ ] [미착수] 대표 PDF·질문·정답 근거로 gold evaluation set을 만든다.
- [ ] [미착수] OCR·구조·검색별 정량 합격선을 승인한다.

## 3. 신규 프로젝트 기반과 도메인 계약

- [ ] [미착수] 레거시와 분리된 신규 source tree와 package를 만든다.
- [ ] [미착수] issuer adapter 공통 계약을 versioning한다.
- [ ] [미착수] issuer+상품코드+문서종류+기준일+버전을 포함한 document ID를 강제한다.
- [ ] [미착수] PDF, OCR, 구조화, embedding과 index artifact manifest를 정의한다.
- [ ] [미착수] 각 artifact에 input hash, 설정·모델 식별자, 생성시각과 lineage를 저장한다.
- [ ] [미착수] 경로 root containment와 symlink 정책을 구현·시험한다.
- [ ] [미착수] schema migration 및 backward compatibility 정책을 시험한다.
- [ ] [미착수] typed configuration을 만들고 cwd·HOME 암묵 의존을 제거한다.
- [ ] [미착수] durable job, attempt, lease, heartbeat, retry와 dead-letter 상태를 구현한다.
- [ ] [미착수] 동일 job의 다중 worker 원자 claim을 concurrency test로 검증한다.

## 4. 카드사별 PDF 수집기

레거시 adapter 존재는 신규 수집기 완료를 의미하지 않는다.

| 카드사 | 레거시 근거 | 신규 수집기 | fixture/contract test | live 제한 검증 |
|---|---|---|---|---|
| 우리카드 | 확인 완료 | 미착수 | 미착수 | 미착수 |
| KB국민카드 | 확인 완료 | 미착수 | 미착수 | 미착수 |
| 신한카드 | adapter·corpus 없음 | BULK 시험 대상으로 결정, 구현 미착수 | 미착수 | 미착수 |
| 삼성카드 | adapter·corpus 없음 | 대상 아님 | 미착수 | 미착수 |
| 그 외 카드사 | 확인하지 않음 | 결정 필요 | 미착수 | 미착수 |

- [ ] [미착수] 카드사별 상품코드를 안정적으로 수집·정규화한다.
- [ ] [미착수] bootstrap은 상품별 최신 PDF를 우선 수집한다.
- [ ] [미착수] 이후 신규·변경 PDF와 과거 version을 구분해 보존한다.
- [ ] [미착수] content hash로 중복과 동일 파일명 내용 변경을 판정한다.
- [ ] [미착수] discovery에서 사라진 상품을 삭제 대신 명시적 상태로 기록한다.
- [ ] [미착수] host allowlist, redirect, timeout, streaming, 최대 크기 제한을 시험한다.
- [ ] [미착수] PDF magic뿐 아니라 열기·페이지 수 등 구조 유효성을 검사한다.
- [ ] [미착수] rate limit과 카드사별 backoff를 적용한다.
- [ ] [미착수] 사이트 markup 변경이 빈 정상 결과로 오인되지 않게 감시한다.
- [ ] [미착수] 재실행 시 성공 문서를 중복 download하지 않는 것을 검증한다.
- [ ] [미착수] terminal 실패 해제·재처리 절차를 제공한다.

## 5. OCR 처리

- [ ] [미착수] PDF 렌더와 OCR을 온라인 MCP와 분리된 worker로 구현한다.
- [ ] [미착수] 초기 대량 OCR에서 Codex exec만 사용하도록 정책을 강제한다.
- [ ] [미착수] 일반 증분 OCR에서 Codex exec 우선·OpenRouter fallback을 추적한다.
- [ ] [미착수] page/chunk별 durable checkpoint를 저장한다.
- [ ] [미착수] 중단 후 성공 chunk를 재호출하지 않고 이어서 처리한다.
- [ ] [미착수] output을 임시 경로에서 완성한 뒤 atomic publish한다.
- [ ] [미착수] 실제 호출 model, prompt version, reasoning, render 설정을 기록한다.
- [ ] [미착수] 모든 PDF 페이지가 OCR Markdown의 page marker와 연결된다.
- [ ] [미착수] 제목·본문·표·각주·혜택 조건·제외조건 관계 보존을 검사한다.
- [ ] [미착수] 금액·비율·기간·횟수 등 숫자 필드를 원문과 대조한다.
- [ ] [미착수] 품질 미달 결과를 성공 색인 대상으로 게시하지 않는다.
- [ ] [미착수] 실패 유형별 retry budget과 수동 재검토 경로를 검증한다.
- [ ] [미착수] 장기 실행의 처리율, 예상 완료시간, 성공·실패 수를 관측한다.
- [ ] [미착수] 초기 대량 처리 전에 gold-set OCR gate를 통과한다.

## 6. 구조 분석

- [ ] [미착수] canonical OCR을 구조 분석과 무관하게 불변 보존한다.
- [ ] [미착수] heading, page, table, 문단과 line/source span을 결정론적으로 보존한다.
- [ ] [미착수] 상품·연회비, 혜택, 이용조건, 전월실적, 제외조건, 필수안내, 유의사항 taxonomy를 versioning한다.
- [ ] [미착수] 규칙-only baseline을 gold set에서 평가한다.
- [ ] [미착수] schema-guided LLM 보강 방식을 같은 표본에서 평가한다.
- [ ] [미착수] 모든 구조화 값에 원문 source span을 요구한다.
- [ ] [미착수] 원문에 없는 LLM 생성값을 validator가 거부한다.
- [ ] [미착수] 혜택과 조건·제외조건·각주의 관계를 명시적으로 표현한다.
- [ ] [미착수] confidence와 extraction method를 저장한다.
- [ ] [미착수] 분석 실패가 canonical OCR을 덮어쓰지 않는 것을 시험한다.
- [ ] [결정 필요] LLM 보강 적용 범위와 수동 검수 대상을 승인한다.

## 7. 임베딩과 검색 색인

- [ ] [결정 필요] OpenRouter embedding 모델 선택 기준과 최초 모델을 승인한다.
- [ ] [결정 필요] lexical/vector 저장 엔진과 운영 방식을 승인한다.
- [ ] [미착수] section·조건 관계와 문맥을 보존하는 chunking을 구현한다.
- [ ] [미착수] chunk에 issuer, 상품코드·명, 문서 version·기준일, section과 source span을 포함한다.
- [ ] [미착수] token upper bound와 overlap 정책을 시험한다.
- [ ] [미착수] embedding count, dimension, finite 값과 model identity를 검증한다.
- [ ] [미착수] current text hash 기준으로 신규·변경 단위만 임베딩한다.
- [ ] [미착수] stale embedding을 current 후보에서 격리하고 보존·폐기 정책을 적용한다.
- [ ] [미착수] 한 질의의 query embedding을 검색 branch에서 재사용한다.
- [ ] [미착수] lexical/vector fusion이 공통 stable evidence key를 사용한다.
- [ ] [미착수] issuer·version·section filter가 정확히 적용되는 것을 시험한다.
- [ ] [미착수] 검색 세대를 별도 경로에서 build하고 checksum·schema·coverage를 검증한다.
- [ ] [미착수] 검증된 세대만 atomic publish하며 이전 세대로 rollback한다.
- [ ] [미착수] card-domain recall, 근거 정확성, latency와 resource 사용량을 측정한다.

## 8. MCP 서비스

- [x] [결정 완료] 운영 transport를 HTTP로 하고 HTTPS URL과 token으로 접속한다.
  증거: 사용자 결정 2026-08-12, `docs/02_TARGET_ARCHITECTURE.md`
- [ ] [결정 필요] token 수명주기, 인증정보 저장, tenant와 tool별 권한을 정한다.
- [ ] [미착수] MCP SDK dependency와 server entrypoint를 추가한다.
- [ ] [미착수] 카드상품 검색 역할을 구현·시험한다.
- [ ] [미착수] issuer+상품코드 상세 및 version 목록 조회 역할을 구현·시험한다.
- [ ] [미착수] 혜택 조건·전월실적·제외조건·유의사항 조회 역할을 구현·시험한다.
- [ ] [미착수] stable evidence와 full source pagination/resource를 제공한다.
- [ ] [미착수] 페이지 단위 OCR·원문 조회를 exact document version과 source span으로 제공한다.
- [ ] [미착수] 명시적 사용자 요청에 exact version·SHA-256의 보존 원본 PDF 파일을 인증 후 제공한다.
- [ ] [미착수] 모든 근거에 issuer, 상품코드, 문서 version·기준일, generation과 source span을 포함한다.
- [ ] [미착수] 정보 부족, 상충 version과 낮은 confidence를 숨기지 않는다.
- [ ] [미착수] online process가 published generation을 read-only로 연다.
- [ ] [미착수] 일반 조회 tool에서 카드사 재다운로드·OCR·rebuild·Gmail·임의 URL/path를 호출할 수 없다.
- [ ] [미착수] limit, pagination, timeout, cancellation과 concurrency 제한을 시험한다.
- [ ] [미착수] health/readiness가 schema, generation, model/dimension, coverage와 FTS 기능을 확인한다.
- [ ] [미착수] contract, integration, load와 authorization test를 통과한다.

## 9. 레거시 데이터 재사용

- [x] [검증 완료] data-kit이 약 9.51 GiB이며 원본·OCR·DB·보고서가 혼재함을 확인했다.
  증거: `LEGACY_PROJECT_ANALYSIS.md` 2절·9절
- [x] [검증 완료] 성공 manifest 1,592건과 주요 DB row·hash coverage를 확인했다.
  증거: `LEGACY_PROJECT_ANALYSIS.md` 9절·14절
- [x] [검증 완료] master manifest 문자수 불일치 55건과 OCR hash 불일치 1건을 기록했다.
  증거: `LEGACY_PROJECT_ANALYSIS.md` 9.3절
- [ ] [미착수] source data-kit을 read-only로 고정하고 별도 target root를 준비한다.
- [ ] [미착수] 모든 이관 대상의 file-level checksum inventory를 만든다.
- [ ] [미착수] `raw_pdf_rel_path` 없는 731건의 provenance를 보완한다.
- [ ] [미착수] pilot 표본을 copy한 뒤 수량·hash·참조 무결성을 검증한다.
- [ ] [미착수] 현재 structured data를 비교 기준으로 보존하고 신규 schema로 재구조화한다.
- [ ] [미착수] historical/stale embedding을 신규 current index에 혼합하지 않는다.
- [ ] [미착수] legacy archive, email job·report, 임시 PNG/OCR를 runtime corpus에서 제외한다.
- [ ] [미착수] 전체 이관 전 pilot rollback을 검증한다.

## 10. 운영, Docker와 인증

- [ ] [미착수] offline worker와 online MCP를 별도 container/process로 구성한다.
- [ ] [미착수] raw/OCR/build/state/index/output volume을 목적과 권한별로 분리한다.
- [ ] [미착수] 대용량 PDF·OCR·index가 Git과 image layer에 없음을 검사한다.
- [ ] [미착수] OpenRouter API key를 secret으로 주입하고 log redaction을 시험한다.
- [ ] [미착수] Codex CLI를 승인된 버전으로 설치하고 version을 기록한다.
- [ ] [검토 필요] Codex OAuth device-code가 target headless Docker 환경에서 지원되는지 검증한다.
- [ ] [미착수] 지원된다면 device code만 Docker log로 노출하고 credential은 persistent secret volume에 저장한다.
- [ ] [검토 필요] 지원되지 않을 경우 승인된 대체 bootstrap·credential 전달 방식을 정한다.
- [ ] [미착수] container 재생성 후 durable job과 artifact가 유지되는 것을 시험한다.
- [ ] [미착수] structured logs, metrics, trace correlation과 alert를 구현한다.
- [ ] [미착수] 장기 OCR의 진행률과 ETA, retry/dead-letter를 운영 화면 또는 report로 확인한다.
- [ ] [미착수] backup 대상·주기·보존·암호화·복구 목표를 승인한다.
- [ ] [미착수] DB·artifact·generation을 일관된 시점으로 backup한다.
- [ ] [미착수] 빈 환경에서 restore와 generation rollback을 rehearsal한다.
- [ ] [미착수] image SBOM, 취약점, non-root, read-only filesystem 정책을 검증한다.
- [x] [결정 완료] GitHub private, Docker Hub public 운영과 version+Git SHA tag·digest 배포를 승인한다.
  증거: 사용자 결정 2026-08-12, `docs/06_OPERATIONS_AND_DEPLOYMENT_GUIDE.md`
- [x] [결정 완료] PDF·OCR 전 버전과 검색 generation 최소 3개 보존, Gmail·이메일 Agent 제외를 승인한다.
  증거: 사용자 결정 2026-08-12, `docs/README.md`

## 11. 테스트와 배포

- [ ] [미착수] unit test가 ID, hash, 상태 전이, path, chunk와 validator를 검증한다.
- [ ] [미착수] 카드사 fixture test가 parser 변경과 빈 결과를 탐지한다.
- [ ] [미착수] 외부 API failure, rate limit과 timeout을 integration test한다.
- [ ] [미착수] worker crash 후 lease 회수·resume·중복 방지를 시험한다.
- [ ] [미착수] generation publish 중 online 무중단·일관 조회를 시험한다.
- [ ] [미착수] retrieval과 grounded-answer regression을 gold set으로 시험한다.
- [ ] [미착수] prompt injection, SSRF, path traversal, 권한 우회, secret 노출을 시험한다.
- [ ] [미착수] 목표 QPS/latency에서 load와 resource 한도를 검증한다.
- [ ] [결정 필요] Docker Hub namespace·lowercase repository slug, image signing과 promotion 승인 절차를 정한다.
- [ ] [미착수] staging image를 Docker Hub에 push하고 digest를 기록한다.
- [ ] [미착수] 검증된 digest만 운영 tag로 promotion한다.
- [ ] [미착수] 배포 후 health, smoke query, evidence provenance와 rollback을 검증한다.

## 12. 현재 차단사항과 다음 갱신 조건

현재 신규 구현을 시작하기 전의 주요 차단사항은 다음과 같다.

- HTTP transport와 token 접속은 확정됐지만 token 수명주기·인가·사용자/tenant 범위가 남아 있다.
- 저장·검색 engine, BULK pilot 이후 수치 SLO와 세부 데이터 보존정책이 결정되지 않았다.
- 신한카드 BULK 시험의 상품·문서·기간 범위와 운영 편입 gate가 남아 있다.
- 원본 PDF 기술 제공은 확정됐지만 카드사 자료의 재배포·서비스 이용 조건과 파일 한도가 남아 있다.
- OCR·구조·검색 품질 gold set과 합격선이 없다.
- 카드사 공시자료 이용 조건 확인이 남아 있다.
- Codex OAuth의 headless Docker device-code 흐름은 실제 환경 검증이 필요하다.

다음 작업자는 [구현 로드맵](07_IMPLEMENTATION_ROADMAP.md)의 단계 1부터 시작하고, 항목을 완료할 때 이 문서의 상태와 증거를 같은 변경에서 갱신해야 한다.
