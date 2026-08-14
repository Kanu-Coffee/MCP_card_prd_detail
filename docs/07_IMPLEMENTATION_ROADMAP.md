# 구현 로드맵

## 1. 목적과 상태 원칙

이 문서는 문서 하네스를 실제 시스템으로 전환하는 순서와 단계별 검증 기준을 정의한다. 단계 번호는 권장 의존순서이며 일정 약속이 아니다.

- `검증 완료`: 산출물과 검증 증거가 모두 존재한다.
- `진행 중`: 구현 또는 검증이 시작되었으나 인수 기준을 충족하지 못했다.
- `미착수`: 구현 증거가 없다.
- `결정 필요`: 사용자나 외부 권한자의 제품·운영 결정이 선행되어야 한다. 현재 개발 착수 P0에는 해당 항목이 없다.
- `착수 가능`: 제품·운영 결정이 완료됐고 남은 기술값은 개발 중 검증한다.

로드맵의 개발환경 범위는 구현과 자동검증을 마쳤다. 실제 카드사 endpoint, Codex/OpenRouter
계정, 전체 장시간 BULK, 운영 host·TLS와 public registry 승인은 개발환경에서 재현할 수 없으므로
`docs/REAL_ENV_HANDOFF.md`에 분리한다. 이 항목들은 완료로 표시하지 않지만 fixture/mock 대체검증이
끝난 뒤에는 개발 goal을 반복 대기시키지 않는다.

## 2. 단계와 의존관계

| 단계 | 이름 | 주요 선행조건 | 현재 상태 |
|---:|---|---|---|
| 0 | 기준 문서와 의사결정 체계 | 없음 | 검증 완료 |
| 1 | 요구사항·품질 기준 확정 | 단계 0 | 검증 완료 — ADR 0001~0005, 합성 gold/load 기준선 |
| 2 | 신규 프로젝트 골격과 도메인 계약 | 단계 1 핵심 결정 | 검증 완료 |
| 3 | 레거시 자산 이전 pilot | 단계 2, 품질 표본 정의 | 5문서 read-only pilot 검증 완료; 전체 이전은 실환경 검증 대기 |
| 4 | 카드사별 PDF 수집기 | 단계 2 | 세 issuer fixture/contract 검증 완료; live는 실환경 검증 대기 |
| 5 | OCR worker와 초기 대량 처리 | 단계 2, 3, 품질 gate | fake backend·resume·sandbox 검증 완료; 실제 모델 BULK는 실환경 검증 대기 |
| 6 | 구조 분석기 | 단계 3, 5의 검증 표본 | 결정론적 span·관계·validator 검증 완료 |
| 7 | 임베딩·검색 색인과 세대 게시 | 단계 1, 3, 6 | fake embedding, pgvector hybrid, generation gate 검증 완료 |
| 8 | 온라인 MCP 조회 서비스 | 단계 2, 7 | HTTP MCP·source file·auth contract 검증 완료 |
| 9 | Docker 운영·인증·관측 | 단계 4~8 | Compose/Keycloak/3-role image/관측성 개발 검증 완료 |
| 10 | 통합 검증과 운영 release | 단계 3~9 | local·remote CI 검증 완료; registry push·host 설치는 인계 |

단계 4의 수집기 개발과 단계 5의 OCR worker 기반 개발은 도메인·상태 계약이 확정된 뒤 일부 병행할 수 있다. 다만 대량 실행은 pilot 품질 gate 통과 이후에만 시작한다.

## 3. 단계별 개발 계획

### 단계 0. 기준 문서와 의사결정 체계

목표는 구현 범위, 설계 원칙과 완료 상태를 한곳에서 추적할 수 있게 하는 것이다.

산출물:

- 레거시 분석 문서
- `docs/` 개발가이드 문서세트
- 완료 체크리스트와 의사결정 기록

인수 기준:

- 요구된 9개 문서가 존재하고 상호 링크가 유효하다.
- 미래 작업이 완료로 표시되어 있지 않다.
- 레거시와 신규 목표가 명확히 구분된다.

### 단계 1. 요구사항·품질 기준 확정

다음 제품 방향은 2026-08-12에 사전 확정됐다.

- 우리카드·KB국민카드를 우선 지원하고 신한카드 개인 신용·체크카드 상품안내장의 현재본·과거 이력을 신규 BULK 시험 대상으로 추가한다. 신한 법인·선불카드는 1차에서 제외한다.
- 기본 검색은 latest이며 과거 PDF/OCR version은 모두 보존하고 명시적 version/as-of 요청에서만 조회한다.
- 운영 MCP는 HTTPS 기반 HTTP endpoint와 OAuth token으로 접속한다. client별 `search`·`source_pdf` scope, 자동 access token refresh, refresh token rotation과 90일 비활성 만료를 적용한다.
- 기존 provider가 없으므로 같은 Compose의 별도 Keycloak service와 PostgreSQL 별도 database·user를 사용한다. `cardrag` 단일 realm에서 self-registration·dynamic client registration을 끄고 승인 client만 수동 등록한다. 사람용은 Authorization Code+PKCE, service용은 Client Credentials를 사용하며 초기 admin은 Docker secret으로 1회 bootstrap 후 회전·제거한다.
- 승인된 `source_pdf` 사용자 요청에는 저장된 전체 원본 PDF를 streaming file로 제공한다. 100 MB 상한과 HTTP Range를 적용하고 접근 감사 metadata를 90일 보존한다. 페이지 PNG는 요청 시 생성해 7일 cache하고 분할 PDF는 만들지 않는다.
- 검색은 공통 stable evidence key 기반 lexical/vector hybrid로 한다.
- GitHub는 private, Docker Hub repository는 public **ymtop59/mcp-card-prd-detail**로 한다. v1 `linux/amd64` image는 일반 `main`·tag push가 아니라 `vX.Y.Z` tag 대상 수동 workflow와 exact confirmation 때만 공개 push하며 version+Git SHA tag, GitHub Actions OIDC keyless Cosign 서명과 digest 배포를 사용한다. `0.1.0`과 `0.1.1`은 통합 release manifest 전에 중단된 부분 release이므로 운영에서 제외하고 `0.1.2`부터 완결된 release manifest를 요구한다.
- 원본 PDF·OCR 전 버전을 보존한다. 성공 generation 최근 3개, 실패 candidate 7일, 수동 pin은 unpin 전까지 보존하고 Gmail·이메일 Agent는 제외한다.
- 온라인은 초기 동시 요청 5개로 시작하며 수치 latency SLO보다 결과 품질을 우선한다.
- 최초 배포는 단일 Linux host의 Docker Compose로 하고 online MCP와 offline worker를 분리한다.
- reverse proxy·TLS·Nginx Proxy Manager 연결은 개발 완료 후 별도 hosting 과제로 둔다. 이 Compose에는 proxy를 포함하지 않고 application은 container `0.0.0.0:8000`, host `127.0.0.1:8000`으로 노출한다.
- PDF·OCR·generation은 외부 불변 file volume, durable state·catalog는 PostgreSQL을 사용한다.
  검색은 PostgreSQL FTS+pgvector HNSW 후보를 stable evidence ID로 RRF 결합한다.
- vector 경로 장애 시 `allow_degraded=true` 요청만 lexical-only 결과를 `degraded`로 반환한다.
- 일일 증분은 매일 03:00 KST에 우리카드 → KB국민카드 → 신한카드 순으로 실행하고 각 job 종료 후 10분 대기하며 issuer 장애를 격리한다.
- v1 운영 관리면은 CLI와 scheduled job만 사용하며 public admin API와 web UI는 만들지 않는다.
- 최신 문서 처리 실패·누락은 generation 게시를 차단하고, 과거 이력 실패는 격리·보고 후 최신 coverage 100%일 때만 게시를 허용한다.
- v1에서 제외했던 backup·restore를 0.2 범위로 승격하고 host bind, normalized legacy import와
  PostgreSQL+CAS+generation portable state를 구현한다.
- query 원문은 저장하지 않고 접근·인증·PDF 감사 metadata는 90일, 비식별 집계 metric은 1년 보존한다.
- OCR·구조 분석 provider/model 전환 시 부분 결과를 섞지 않고 전체 문서를 새 attempt로 처리한다.

다음 개발 기준은 gold set, 부하·장애 시험과 ADR-0001~0005로 확정했다. 실제 카드사·provider와
전체 corpus가 필요한 수치만 운영 인계에서 보정한다.

- 동시 요청 5, request timeout 45초, 초기 검색 P95 30초와 Compose resource 개발 기본값;
  실제 BULK 이후 QPS·가용성·host sizing 보정
- PostgreSQL 17 schema·migration 1~14, content-addressed file layout와 FTS+pgvector/RRF
- Codex `gpt-5.4`, 결정론적 구조 baseline, OpenRouter `text-embedding-3-small` 1,536차원;
  실제 모델 비용·quota·품질 재측정
- 문자 99.5%+, critical/page/source-span 100%, Recall@10 95%+, critical Recall/filter 100%,
  MRR·nDCG@10 0.90+와 중대 오류 zero-tolerance

공시자료의 이용 조건은 공개 운영 전 별도 확인할 외부 gate다. 승인 사용자 한정 개발·검증은 진행할 수 있으며, 확인 전 공개 범위를 확대하지 않는다.

산출물:

- 승인된 decision record
- 대표 PDF와 질문으로 구성된 gold evaluation set
- 기능·품질·운영 SLO 초안
- 위협 모델과 데이터 등급

인수 기준:

- 모든 제품·운영 P0 결정과 기술 의사결정 위임 기록이 있다.
- gold set에 표, 각주, 전월실적, 제외조건, 복수 버전 사례가 포함된다.
- 품질 지표의 계산 방법과 합격선이 재현 가능하다.

### 단계 2. 신규 프로젝트 골격과 도메인 계약

레거시 패키지를 복사하지 않고 신규 경계부터 만든다.

개발 범위:

- issuer adapter와 공통 document identity
- 원본·OCR·구조화·색인 artifact manifest
- 입력 hash와 처리 설정을 포함하는 lineage
- durable job, attempt, lease, retry, terminal/dead-letter 상태
- generation build·verify·publish·rollback 계약
- typed configuration, secret reference, storage root
- PostgreSQL durable state·catalog와 외부 불변 file volume 계약

산출물:

- 신규 source tree와 versioned schema
- schema migration 및 호환성 정책
- unit/contract test 기반

인수 기준:

- issuer가 없는 document/evidence ID를 생성할 수 없다.
- 동일 입력과 설정은 같은 identity를 만들고, 변경 입력은 새 version으로 구분된다.
- 상태 전이, lease 만료와 재시도 예산이 테스트된다.
- 경로는 root containment와 symlink 정책까지 검증된다.

### 단계 3. 레거시 자산 이전 pilot

전체 9.51 GiB를 먼저 복사하지 않는다. 우리카드와 KB에서 정상·복잡표·다중 페이지·hash 예외 사례를 포함한 작은 표본을 선택한다.

개발 범위:

- read-only source inventory와 파일별 checksum manifest
- PDF/OCR/metadata의 목표 구조 변환
- 누락된 `raw_pdf_rel_path` 보완 규칙
- legacy structured/embedding의 비교용 import 또는 재색인 경로
- 원본과 변환본의 수량·hash·참조 무결성 검증

산출물:

- pilot migration report
- 변환·제외·예외 목록
- rollback 가능한 target generation

인수 기준:

- source 파일은 변경되지 않았음이 확인된다.
- 모든 target artifact가 source와 lineage로 연결된다.
- 알려진 OCR hash 불일치 1건과 manifest 문자수 불일치 55건이 숨겨지지 않는다.
- 재실행해도 중복 artifact나 상태 오염이 발생하지 않는다.

### 단계 4. 카드사별 PDF 수집기

우리카드와 KB 레거시 adapter는 동작 이해와 fixture의 출발점으로만 사용하고, 신규 adapter 계약에 맞게 재구현한다. 신한카드는 레거시가 없는 신규 adapter로 구현해 개인 신용·체크카드 현재본과 과거 이력 BULK corpus를 만들며 법인·선불카드는 제외한다.

개발 범위:

- 카드사별 discovery·download adapter
- 우리카드·KB국민카드 우선 지원과 신한카드 BULK 시험 adapter
- 상품코드 확보 및 정규화
- 최신본 우선 bootstrap과 이후 변경 이력 보존
- content hash 중심의 중복·변경 판정
- allowlist, redirect, 크기, timeout, PDF 유효성 검증
- rate limit, retry, terminal 분류와 사이트 변경 탐지

산출물:

- 카드사별 fixture와 contract test
- discovery/download manifest
- 이용 조건 검토 기록

인수 기준:

- 같은 공시를 반복 실행해도 중복 문서가 생기지 않는다.
- 동일 파일명 내용 변경을 감지한다.
- 실패한 문서만 안전하게 재시도할 수 있다.
- 사이트 markup 변경 시 조용히 빈 성공으로 끝나지 않는다.

### 단계 5. OCR worker와 초기 대량 처리

초기 대량 OCR은 Codex exec만 사용한다. 3~4일 이상 소요될 수 있다는 요구는 계획 가정이며, 실제 처리율은 pilot으로 다시 산정한다.

개발 범위:

- 격리된 PDF 렌더·OCR worker
- page/chunk별 checkpoint와 atomic artifact write
- model·prompt·render 설정·input/output hash provenance
- 중단 후 lease 회수, 이어서 처리, 실패 선택 재처리
- 원문 충실도·페이지 완전성·숫자·표·각주 품질 gate
- 카드사별 신규·변경 문서 증분 처리

산출물:

- canonical OCR Markdown과 metadata
- 진행률·실패·처리율 report
- OCR gold-set 평가 결과

인수 기준:

- worker 종료·재시작 후 성공 chunk를 중복 호출하지 않는다.
- 모든 페이지가 순서와 원본 위치로 추적된다.
- 모델 label과 실제 invocation이 일치한다.
- 품질 기준 미달 문서는 게시 대상에서 제외되고 재검토 상태가 된다.

### 단계 6. 구조 분석기

권장 구조는 canonical OCR을 변경하지 않는 다층 방식이다.

1. 결정론적으로 heading, page, table, 문단과 line 범위를 보존한다.
2. schema-guided LLM 보강은 선택 경계로 두고 gold에서 rule baseline 개선을 입증할 때만 활성화한다.
3. validator가 모든 구조화 값이 원문 근거와 연결되는지 검사한다.

산출물:

- versioned structured document
- source span과 confidence를 가진 section/condition 관계
- 규칙-only 개발 기준선 평가와 향후 LLM-assisted 비교용 동일 evaluator

인수 기준:

- 모든 추출값이 canonical OCR의 source span으로 돌아간다.
- LLM이 만든 원문 비존재 값은 gate에서 거부된다.
- 혜택과 전월실적·제외조건·유의사항의 연결 정확도가 gold set 기준을 충족한다.
- 구조 분석 실패가 canonical OCR 사용 가능성을 훼손하지 않는다.

### 단계 7. 임베딩·검색 색인과 세대 게시

개발 범위:

- section·조건 관계와 문맥을 보존한 chunk 전략
- configurable OpenRouter embedding provider/model
- current text hash 기반 증분 임베딩과 stale row 격리
- lexical+vector 검색 및 공통 evidence key fusion
- vector 장애 시 `allow_degraded` opt-in lexical-only 정책
- generation 단위 build, 검증, immutable publish와 rollback

산출물:

- versioned search generation
- generation manifest와 model/dimension/schema 정보
- retrieval benchmark와 성능 report

인수 기준:

- 동일 query embedding을 한 요청에서 재사용한다.
- issuer·문서 버전·section filter가 후보 추출 전에 적용되거나 동등성이 검증된다.
- 이전 세대와 새 세대가 섞이지 않는다.
- 최신 문서의 OCR·구조·임베딩·색인 coverage가 100%가 아니면 게시가 차단된다. 과거 이력 실패는 quarantine·보고서에 남는다.
- recall, 근거 정확성, latency와 resource 사용량이 승인 기준을 충족한다.

### 단계 8. 온라인 MCP 조회 서비스

온라인 서비스는 게시된 검색 세대만 읽는다. 카드사 사이트 PDF 다운로드, OCR, DB rebuild, Gmail 발송은 일반 조회 경로에 포함하지 않는다. 이미 보존된 원본 PDF의 인증된 읽기 전용 제공은 명시적 사용자 요청에만 허용한다.

역할 범위:

- 카드상품 검색
- issuer+상품코드 기반 상세·버전 조회
- 혜택 조건, 전월실적, 제외조건과 유의사항 조회
- stable evidence 원문과 출처 조회
- 페이지 단위 OCR text와 원본 PDF에서 요청 시 생성해 7일 cache하는 PNG 조회
- 승인된 `source_pdf` 사용자에게 exact version·hash의 보존 원본 PDF 전체 streaming file 제공; 100 MB 상한, HTTP Range, 감사 metadata 90일, 분할 PDF 미생성
- index generation과 readiness 조회

산출물:

- HTTP MCP server와 역할별 tool/resource·file 전달 계약
- OAuth 자동 refresh·rotation, `search`·`source_pdf` scope, 오류·limit·timeout 정책
- contract/integration/security test

인수 기준:

- 응답 근거가 stable ID, generation, 문서 버전과 source span을 포함한다.
- 정보 부족과 버전 충돌이 명시적으로 표현된다.
- 임의 URL·경로·배치 실행을 조회 tool로 유발할 수 없다.
- 과도한 결과는 손실 없이 pagination/resource로 이어진다.
- 원본 PDF와 페이지 응답이 요청한 document version·hash·source span과 일치한다.
- 100 MB 초과 PDF 거부, Range 전송·취소와 PDF 접근 감사 metadata 보존이 검증된다.
- 페이지 PNG가 7일 후 cache에서 제거되고 generation에는 영구 저장되지 않는다.
- vector 장애 시 `allow_degraded` flag에 따라 명시적 lexical-only 또는 실패로 동작한다.

### 단계 9. Docker 운영·인증·관측

개발 범위:

- online MCP와 offline worker의 별도 image/process
- 단일 Linux host Docker Compose와 PostgreSQL service
- MCP container `0.0.0.0:8000` listen과 host `127.0.0.1:8000` publish; Nginx Proxy Manager 연결은 후속 hosting 과제
- read-only snapshot, writable work/state/output, secret volume 분리
- Codex CLI 설치와 OAuth credential 지속성
- headless device-code 로그인 흐름의 실제 지원 여부 검증
- OpenRouter key secret 주입
- HTTP MCP endpoint와 self-hosted Keycloak 단일 tenant, OAuth discovery·Bearer header·자동 refresh/rotation·revoke
- 운영 CLI·scheduled job과 매일 03:00 KST 우리카드 → KB국민카드 → 신한카드 순차 실행, 각 job 종료 후 10분 대기·격리
- public Docker Hub **ymtop59/mcp-card-prd-detail**와 private GitHub 경계, v1 `linux/amd64`, `vX.Y.Z`+manual approval 공개 push, version+Git SHA tag, GitHub Actions OIDC keyless Cosign 서명, digest 배포
- health/readiness, structured logs, metrics, alerting
- 접근·인증·PDF audit metadata 90일과 비식별 metric 1년 보존
- generation rollback rehearsal, portable state export/empty-target restore와 다른-host cutover drill

산출물:

- 재현 가능한 image와 배포 manifest
- 운영 runbook과 generation rollback report
- image SBOM·취약점 점검 결과

인수 기준:

- container 재생성 후 데이터와 durable 상태가 보존된다.
- secret과 대용량 artifact가 image layer·Git·일반 log에 없다.
- public image에 포함된 code·dependency metadata가 공개됨을 점검하고 비공개 corpus·secret이 없는지 검증한다.
- Compose에 reverse proxy를 포함하지 않고 host `127.0.0.1:8000`에서 MCP·health endpoint를 검증한다.
- Keycloak 단일 tenant의 token 발급·자동 refresh·rotation·scope·revoke를 검증한다.
- OAuth device-code가 지원된다면 Docker log에서 필요한 정보만 안전하게 확인된다.
- 지원되지 않는다면 승인된 대체 bootstrap 절차가 문서화된다.
- 이전 generation으로 rollback하고 조회 정상화를 입증한다.

### 단계 10. 통합 검증과 운영 release

개발 범위:

- 신규/변경 공시부터 MCP 근거 응답까지 end-to-end 시험
- 장기 OCR 중단·재개, worker crash, 외부 API 장애 시험
- rebuild/publish 중 온라인 무중단 조회 시험
- load, 보안, prompt injection, SSRF, 권한 시험
- `vX.Y.Z` tag 대상 수동 workflow와 exact confirmation을 통과한 `linux/amd64` candidate만 public Docker Hub **ymtop59/mcp-card-prd-detail**에 version+Git SHA tag와 keyless Cosign 서명으로 게시한 뒤 digest 기반 promotion

산출물:

- release candidate와 image digest
- 통합·부하·보안·rollback 시험 report
- 운영 승인 및 rollback 기준

인수 기준:

- [완료 체크리스트](08_COMPLETION_CHECKLIST.md)의 필수 항목에 증거가 연결된다.
- 미해결 P0/P1 위험이 없거나 승인된 예외와 만료일이 있다.
- Docker Hub public 대상·tag 정책이 확인되고 digest로 재배포 가능하다.
- 운영 담당자가 신규 세대 게시와 rollback을 독립 수행해 검증한다.

## 4. 주요 gate

| Gate | 통과 전 금지되는 작업 | 최소 증거 |
|---|---|---|
| 요구사항 gate | 대량 처리·공개 운영 | 승인된 제품 결정, Codex 기술 ADR와 측정 가능한 품질 기준 |
| 데이터 pilot gate | 전체 레거시 이전 | checksum·lineage·예외 report |
| OCR 품질 gate | 초기 전체 OCR | gold-set 원문 충실도 평가 |
| 구조 품질 gate | 전체 구조화 | source-span 및 관계 정확성 평가 |
| 검색 품질 gate | MCP 외부 공개 | retrieval·grounding·latency report |
| 보안·rollback gate | 운영 image promotion | 권한·secret·SSRF·generation rollback 시험 |

## 5. 우선순위

P0:

- 온라인/오프라인 권한과 저장소 분리
- document/evidence/generation identity
- 품질 기준과 gold set
- durable 상태와 atomic generation publish
- self-hosted Keycloak 단일 tenant와 MCP token 검증 구현
- vector full scan과 레거시 hybrid 결함을 계승하지 않는 검색 방식

P1:

- 우리카드·KB 수집기와 migration pilot, 신한카드 BULK 시험 adapter
- 재시작 가능한 OCR worker
- 원문 근거를 강제하는 구조 분석
- stable evidence를 제공하는 MCP 조회
- Docker volume·secret·OAuth·generation rollback 검증

P2:

- 신한카드 외 지원 카드사 확대
- 모델·검색 engine 교체 자동화
- provider 비용 최적화
- 품질 regression dashboard와 운영 자동화
- 증분 backup/PITR와 자동 주기화(0.2의 점검시간 full export/restore 이후 단계)

우선순위는 난이도가 아니라 잘못 결정했을 때의 재작업·데이터 손실·운영 위험을 기준으로 한다.
