# 목표 아키텍처

> 문서 상태: 구현·개발환경 검증된 v1 아키텍처 기준선
> 기준일: 2026-08-12
> 구현 상태: 아래 구성요소와 배포 단위는 코드·fixture/mock·자동 통합시험으로 구현·검증했다. 실제 카드사·provider·전체 corpus·운영 host 검증은 운영 인계로 분리한다.

## 1. 목적과 설계 전제

이 문서는 카드상품안내장 처리 시스템의 전체 구조와 구성요소 경계를 정의한다. 핵심 전제는 다음과 같다.

- 수집·OCR·구조화·임베딩은 장시간 실행되는 쓰기 중심 오프라인 작업이다.
- MCP 검색·상세조회는 상시 실행되는 짧은 읽기 중심 온라인 작업이다.
- 두 영역은 같은 live DB를 동시에 읽고 쓰지 않는다.
- 두 영역 사이에서 전달되는 것은 검증 완료 후 불변으로 발행된 검색 `generation`이다.
- 레거시 corpus는 원본 보존 상태에서 선별 복사·검증·변환하며 레거시 작업 디렉터리를 신규 런타임으로 사용하지 않는다.

여기서 generation은 동일한 기준시점과 처리 설정으로 생성되어 함께 발행되는 catalog, evidence,
structured 데이터와 검색 색인의 논리적 묶음이다. v1은 PostgreSQL 17의 catalog·FTS·pgvector
snapshot과 checksum·`READY` seal을 가진 불변 file generation을 사용한다. 실제 corpus가 단일 host
한도를 넘는 경우에만 새 benchmark와 ADR로 저장 경계를 재검토한다.

## 2. 전체 구조

```text
                         오프라인 데이터 처리 영역

  카드사 공시 사이트
          │
          ▼
  [카드사별 수집 adapter] ──→ [원본 PDF·버전 보관소]
          │                            │
          │ 수집·변경 상태             ▼
          └──────────────────→ [OCR 처리기]
                                       │ OCR Markdown + provenance
                                       ▼
                               [구조 분석기]
                                       │ 구조 단위 + 원문 연결
                                       ▼
                               [임베딩·색인 빌더]
                                       │ staging generation
                                       ▼
                               [generation 검증기]
                                       │ 검증 성공 시에만
                                       ▼
                               [원자적 발행·rollback]
                                       │
                 ──────────────────────┼──────────────────────
                                       │ 불변 검색 snapshot
                                       ▼
                           온라인 MCP 서비스 영역

  외부 LLM/MCP client → [HTTPS MCP+token 경계] → [질의 서비스]
                                                   │
                                  ┌────────────────┼───────────────┐
                                  ▼                ▼               ▼
                            [catalog 조회]   [검색·ranking]   [근거 조립]
                                  └────────────────┼───────────────┘
                                                   ▼
                                    버전·출처 포함 읽기 응답
```

운영 제어와 데이터 이동은 한 방향을 원칙으로 한다. 오프라인 영역은 새 generation을 만들 수 있지만 서비스 중인 generation을 직접 고치지 않는다. 온라인 영역은 generation을 읽을 수 있지만 수집·OCR·색인 작업을 시작하거나 상태를 변경할 수 없다.

### 2.1 구현된 프로젝트 구조

논리 경계는 하나의 `cardrag` package 아래 다음과 같이 구현했다. 컨테이너는 같은 검증된
distribution을 사용하지만 역할별 entrypoint, PostgreSQL role과 volume 권한으로 실행 경계를
강제한다.

```text
project/
├── docs/                       # 개발·품질·운영 기준
├── src/cardrag/
│   ├── domain/                 # issuer-aware 상품·문서·근거 계약
│   ├── issuers/                # 우리·KB·신한 adapter
│   ├── acquisition/            # 제한 download와 PDF 검증
│   ├── pipeline/               # OCR·구조 분석·chunk·durable worker
│   ├── search/                 # embedding, PostgreSQL hybrid, 세대 reader
│   ├── service/                # query/auth/source-file 경계
│   ├── storage/                # content-addressed object와 안전한 경로
│   ├── legacy/                 # read-only 이관 pilot
│   └── db/                     # PostgreSQL migration과 role 계약
├── tests/                      # 단위·통합·품질·복구·성능 검증
├── deploy/                     # Keycloak·PostgreSQL·systemd·관측성
├── Dockerfile                  # MCP/worker/admin 역할별 target
└── compose.yaml                # 단일-host 배포 경계
```

`domain`은 카드사별 웹 구조나 특정 검색 엔진에 의존하지 않는다. `pipeline`은 domain artifact를
만들고 `service`는 published generation만 읽는다. 저장 제품, LLM provider와 MCP transport는
Protocol·adapter 경계 뒤에 두어 fixture와 운영 구현을 교체할 수 있다.

## 3. 배포와 권한 경계

### 3.1 오프라인 데이터 처리 영역

이 영역은 초기 대량 처리와 일일 증분 처리를 담당한다. 우리카드·KB국민카드를 우선 처리하고, 신한카드는 개인 신용·체크카드 상품안내장의 현재본과 과거 이력을 BULK 시험 대상으로 추가한다. 신한 법인·선불카드는 1차 범위에서 제외한다. 카드사 공개 endpoint, Codex CLI와 OpenRouter에 접근할 수 있으며 원본·파생 artifact와 작업 상태를 쓸 수 있다.

특징은 다음과 같다.

- 장시간 작업과 부분 실패를 전제로 한다.
- 동일 입력과 설정을 다시 처리해도 중복 artifact를 만들지 않는 멱등성이 필요하다.
- 단계별 checkpoint, lease 또는 동등한 작업 소유권, retry 이력을 유지한다.
- 실패한 문서가 다른 문서 처리나 현재 온라인 generation에 영향을 주지 않도록 격리한다.
- build 완료 후 검증을 통과한 generation만 발행 권한을 가진다.

수집·OCR·build는 worker target, 게시·운영 명령은 admin target에 두며 generation volume은
worker read-only/admin read-write다. 온라인 MCP target은 수집·OCR 명령을 실행할 수 없고
published object/generation volume만 읽는다.

### 3.2 온라인 MCP 서비스 영역

이 영역은 외부 LLM의 카드상품 검색과 상세조회 요청을 처리한다. 발행된 generation을 읽기 전용으로 열며 다음 권한을 갖지 않는다.

- 카드사 사이트 discovery 및 PDF 다운로드
- Codex OCR 실행
- corpus·색인·작업 상태 변경
- generation build·검증·발행
- 이메일 읽기·발송
- 임의 경로 파일 출력

온라인 vector 검색은 OpenRouter query embedding에 제한적으로 통신한다. request timeout, bounded retry와
circuit breaker를 개발 기준선으로 구현했고 질의 원문은 보존하지 않는다. 실제 provider quota·비용과
운영 우선순위는 실환경 pilot에서 보정한다. 장애 시에는 caller가 `allow_degraded=true`로 허용한
요청만 lexical-only 결과를 받는다. 그 외 오프라인 endpoint와 비밀정보는 온라인 컨테이너에
제공하지 않는다.

온라인 MCP는 HTTPS endpoint URL과 OAuth access token으로 접속한다. token은 `Authorization: Bearer` header에서만 받고 URL query·path와 일반 log에는 기록하지 않는다. client별로 `search`와 `source_pdf` scope를 분리한다. 최초 승인 이후 client는 access token을 자동 갱신하고 refresh token을 회전한다. 90일은 고정 연결 만료가 아니라 비활성 만료 기준이며, 정상적으로 계속 사용하는 동안 수동 token 재입력을 요구하지 않는다. refresh token 폐기·분실, 보안사고 또는 client의 refresh 미지원 시에는 재인증한다.

기존 조직 OAuth/OIDC provider는 사용하지 않고 self-hosted Keycloak을 authorization server로 채택한다. Keycloak은 같은 Docker Compose의 별도 service이며 PostgreSQL server를 공유하되 애플리케이션과 별도 database·user를 사용한다. v1 realm은 `cardrag` 단일 tenant다. 사용자 self-registration과 dynamic client registration은 끄고 승인 client만 수동 사전등록한다. 사람용 client는 Authorization Code+PKCE, service client는 Client Credentials를 사용한다. 초기 admin credential은 Docker secret으로 1회 bootstrap한 뒤 회전·제거한다. 애플리케이션 scope는 `search`·`source_pdf`로 제한하고 운영 명령 권한은 local CLI로 분리한다.

온라인 서비스는 승인 사용자가 명시적으로 요청한 경우에 한해 게시 대상 문서와 연결된 보존 원본 PDF 전체를 인증 후 streaming file로 제공할 수 있다. `search` scope는 페이지 OCR text, `source_pdf` scope는 전체 PDF와 요청 시 생성하는 렌더 PNG를 허용한다. 렌더 PNG는 7일 cache 후 제거하고 영구 artifact나 generation 구성요소로 보존하지 않는다. PDF는 파일당 100 MB로 제한하고 HTTP Range를 지원하며 다운로드 감사 metadata를 90일 보존한다. 분할 PDF는 생성하지 않는다. 이는 카드사 사이트에서 새 PDF를 내려받는 권한과 다르며, 임의 URL·임의 host path는 받지 않는다.

### 3.3 운영 제어 영역

스케줄, 작업 제출, 재시도 승인, generation 전환과 rollback은 일반 카드 조회 요청과 분리한다. 1차는 운영 CLI와 scheduled job만 제공하고 공개 관리자 API·웹 UI는 만들지 않는다.

어떤 형태를 선택해도 다음 원칙은 유지한다.

- 일반 MCP client는 운영 명령에 접근할 수 없다.
- 모든 상태 변경에는 실행 주체, 대상 문서·generation, 시각과 결과가 남는다.
- 같은 카드사·동일 작업 범위의 중복 실행을 차단하거나 명시적으로 직렬화한다.
- 발행은 검증 결과와 연결되고 rollback 대상이 명확해야 한다.

## 4. 구성요소와 책임

### 4.1 오프라인 구성요소

| 구성요소 | 책임 | 입력 | 출력 | 경계 |
|---|---|---|---|---|
| 수집 orchestrator | 초기·증분 실행 계획, 단계 상태, 재시도 조정 | 일정, 카드사 범위, 이전 상태 | 문서별 작업과 실행 기록 | OCR·검색 로직을 직접 포함하지 않는다. |
| 카드사별 수집 adapter | 공시 discovery, 상품코드 추출, PDF 위치 확인 | 카드사 설정·공개 응답 | 정규화된 공시 후보 | 카드사별 HTML·API 차이를 adapter 내부에 가둔다. |
| 변경 판정기 | 신규·변경·동일·소실 후보 분류 | discovery, 이전 문서 이력 | 처리 계획 | filename만으로 동일성을 확정하지 않고 hash·size·버전을 활용한다. |
| PDF 취합기 | 제한된 endpoint에서 PDF 다운로드·검증·원자 저장 | 검증된 공시 후보 | 버전별 원본 PDF와 metadata | redirect host, 크기, timeout, PDF 유효성을 검증한다. |
| OCR 처리기 | 페이지 렌더링, OCR 실행, 결과 조립·검증 | 원본 PDF, OCR 설정 | OCR Markdown, 페이지 artifact, provenance | 부분 chunk를 완료본으로 발행하지 않는다. |
| 구조 분석기 | 문서 구조와 조건 관계를 검색 단위로 표현 | 검증된 OCR Markdown | section·sentence 등 구조 단위와 원문 좌표 | 원문을 대체하지 않으며 엔진 선택은 별도 품질 평가 대상이다. |
| 임베딩 생성기 | 검색 단위와 query가 호환되는 벡터 생성 | 구조 단위, 모델 설정 | 벡터와 model/dimension metadata | OpenRouter 임베딩 전용 모델을 사용하고 응답 count·차원을 검증한다. |
| 색인 빌더 | catalog, text/vector 검색 색인을 독립 작업 경로에 구축 | 검증된 문서·구조·벡터 | staging generation | 서비스 중인 파일을 제자리 수정하지 않는다. |
| generation 검증기 | 무결성·완전성·검색 smoke test 수행 | staging generation과 build manifest | 발행 승인 또는 실패 보고서 | 검증 실패 데이터는 온라인에 노출하지 않는다. |
| generation 발행기 | 검증본을 불변으로 등록하고 active 참조 전환 | 승인된 generation | active generation과 rollback 이력 | build와 분리된 제한 권한을 사용한다. |

구조 분석 v1은 canonical OCR을 변경하지 않는 결정론적 rule baseline과 exact source-span validator로
확정했다. schema-guided LLM 보강은 같은 gold set에서 baseline 개선을 증명하기 전까지 기본 off다.
downstream 계약은 향후 엔진 교체와 무관하게 유지한다.

### 4.2 온라인 구성요소

| 구성요소 | 책임 | 허용되는 데이터 접근 | 금지되는 책임 |
|---|---|---|---|
| MCP protocol adapter | MCP client 연결, 입력 제한, 오류 변환, 취소 처리 | 질의 서비스만 호출 | DB 직접 변경, 배치 실행 |
| 질의 application service | 상품·문서·근거 조회 흐름과 정책 조정 | active generation read-only | 외부 URL 수집, generation 전환 |
| catalog/product reader | 카드사·상품·문서 버전과 최신본 조회 | catalog snapshot | 최신본 상태 변경 |
| retrieval service | 검색어와 filter를 적용해 후보 검색·ranking | text/vector index snapshot | index 쓰기·재임베딩 |
| evidence reader | stable 근거 ID로 전체 원문 구간·출처 조회 | evidence snapshot | 임의 filesystem 탐색 |
| source artifact reader | 정확한 document version과 hash로 원본 PDF·페이지 근거 제공 | 게시 승인된 원본의 read-only view | 카드사 재다운로드, 임의 path 접근 |
| response assembler | 중복 제거, 버전 충돌 표시, 근거 묶음 구성 | 조회된 product·evidence | 원문에 없는 조건 추론 |
| snapshot manager | active generation 확인, read-only open, 안전한 세대 교체 | published generation | staging·원본 영역 접근 |
| 상태·관측 adapter | build ID, generation, readiness, latency와 오류 집계 | 비민감 상태 metadata | 비밀정보·질의 원문 무제한 노출 |

구체적인 MCP tool 이름, 인자와 응답 schema는 이 단계에서 정의하지 않는다. 역할 수준에서 최소한 상품 탐색, 상품 상세·버전, 조건별 근거 검색, 페이지 단위 OCR text·요청 시 생성하는 PNG 조회, 명시적 원본 PDF 전체 파일 요청과 index 상태 확인이 필요하다. 페이지 PNG는 7일 cache 후 제거한다. PDF 응답은 exact document version, content hash, MIME type과 크기를 포함하고 인증된 streaming을 사용한다. 별도 분할 PDF는 생성하지 않는다.

### 4.3 공통 기반 구성요소

- **설정 관리:** 모든 storage root, provider, 모델, timeout과 제한을 명시적 설정으로 받는다. cwd 또는 HOME 탐색을 암묵적 기본값으로 삼지 않는다.
- **비밀정보 관리:** OpenRouter key, Codex 인증과 향후 인증 secret은 런타임에 주입하며 artifact metadata에는 값이 아닌 provider·모델·설정 식별자만 남긴다.
- **관측성:** run ID, issuer, document version, stage, generation을 공통 문맥으로 로그·지표에 포함한다.
- **schema·migration 관리:** manifest, job state, published generation schema에 명시적 버전을 둔다. 호환되지 않는 변경은 새 generation으로 만든다.
- **정책 관리:** 데이터 보존, 외부 전송, 로그 redaction, 카드사 이용 조건과 접근 제한을 코드 밖 운영 정책으로 추적한다.

## 5. 데이터 흐름

### 5.1 최초 대량 처리

1. 카드사별 adapter가 상품과 상품안내장 후보를 발견하고 issuer-scoped 상품코드로 정규화한다.
2. 상품별 최신 PDF를 우선 처리하고 과거 버전도 모두 보존 대상으로 수집한다. 검색의 기본 범위는 최신본이며, 과거본은 version 또는 as-of가 명시된 조회에서만 노출한다.
3. PDF 취합기가 허용된 카드사 endpoint에서 원본을 받아 content hash와 출처 metadata를 기록한다.
4. OCR 처리기가 PDF를 페이지 단위로 렌더링하고 Codex exec를 통해 OCR Markdown을 만든다. 초기 대량 OCR은 Codex exec만 사용한다.
5. 품질 검사가 페이지 누락, 비정상적으로 짧은 결과, hash와 필수 metadata를 확인한다. 실패 문서는 격리하고 성공 문서만 다음 단계로 넘긴다.
6. 구조 분석기가 섹션·문장·조건 관계를 표시하면서 각 단위를 OCR 원문 구간에 연결한다.
7. 임베딩 생성기가 OpenRouter 임베딩 전용 모델로 벡터를 만들고 model, dimension, 입력 hash를 함께 기록한다.
8. 색인 빌더가 별도의 staging 경로에서 하나의 일관된 generation을 만든다.
9. generation 검증기가 schema, 참조 무결성, 문서·구조·벡터 coverage와 대표 검색을 검사한다.
10. 검증 성공 시 발행기가 generation을 불변으로 등록하고 active 참조를 전환한다.
11. 온라인 서비스가 새 generation을 안전하게 열고 새 요청부터 사용한다. 이전 generation은 rollback 기간 동안 보존한다.

초기 작업이 3~4일 이상 걸릴 수 있다는 요구를 고려해, 각 단계의 완료 artifact와 실패 상태는 컨테이너 수명보다 오래 유지한다. 프로세스 재시작은 완료 문서를 다시 OCR하거나 임베딩하지 않아야 한다.

### 5.2 일일 증분 처리

scheduled run은 매일 03:00 KST에 우리카드, KB국민카드, 신한카드 순으로 실행하고 각 issuer job 종료 후 10분 대기한다. issuer별 job과 실패 상태를 격리해 앞선 카드사 실패가 다음 카드사 시작을 막지 않게 한다.

1. 카드사별 discovery snapshot을 이전 snapshot과 비교한다.
2. 신규·변경 후보만 원본 확인과 OCR 대상으로 선정한다.
3. 변경된 document version과 그 downstream 구조·임베딩만 새로 만든다.
4. 변경되지 않은 artifact는 hash와 처리 설정이 같을 때 새 generation에서 참조하거나 검증된 방식으로 재사용한다.
5. 삭제·비공개로 보이는 문서는 즉시 물리 삭제하지 않고 상태 변경으로 기록하며 PDF·OCR 이력 보존 원칙을 적용한다. 기본 latest 검색에서는 제외하되 명시적 version/as-of와 승인 범위에서만 조회한다.
6. 증분 결과도 완전한 generation 단위로 검증·발행한다. 최신 문서에 OCR·구조·색인 누락 또는 실패가 있으면 게시를 차단한다. 과거 이력 실패는 quarantine과 보고서에 남기되 최신 문서 coverage가 100%인 경우에만 게시를 허용한다. active generation에 행 단위로 직접 반영하지 않는다.

### 5.3 온라인 조회

1. MCP protocol adapter가 요청 크기, 필수 필드와 허용 범위를 검증한다.
2. 질의 서비스가 active generation과 corpus 기준시점을 고정한다.
3. issuer, 문서 기준일, section 등 filter를 검색 전에 적용한다.
4. text/vector 후보를 검색하고 공통 stable evidence key로 결합한다.
5. stable evidence ID를 통해 원문 구간과 문서 metadata를 다시 읽어 결과를 검증한다.
6. 상품·문서 버전·관련 섹션·원문 근거·출처·generation을 함께 반환한다. 페이지 요청은 OCR text와 원본 PDF에서 요청 시 생성해 7일 cache하는 PNG로 제공하고, 명시적 PDF 요청은 정확한 version과 hash를 확인한 전체 streaming file로 분리한다.
7. 최신본과 과거본이 충돌하거나 충분한 근거가 없으면 그 상태를 명시한다.

lexical/vector hybrid는 PostgreSQL FTS와 pgvector HNSW 후보를 공통 stable evidence ID로 RRF 결합한다.
issuer·version/as-of·section filter는 두 후보 SQL에 동일하게 적용하고 query embedding은 요청당 한 번만
생성한다. 후보 한도와 RRF의 개발 기본값은 자동시험으로 고정했으며 실제 corpus 품질·지연 측정에서
보정한다. 레거시의 Python exact full scan과 서로 다른 ID 공간을 사용한 hybrid는 채택하지 않는다.

## 6. 식별자와 provenance 경계

모든 단계에서 다음 식별 축을 잃지 않아야 한다.

| 식별 축 | 목적 |
|---|---|
| 카드사 | 카드사 간 상품코드 충돌과 filter 오류 방지 |
| 상품코드 | 공시 원천과 상품의 기준키 |
| 문서 유형 | 상품안내장과 향후 다른 공시 문서 구분 |
| 효력일·버전 | 최신본, 과거본과 as-of 조회 구분 |
| 원본 content hash | 동일 파일·변경 파일 판정과 무결성 확인 |
| 처리 설정 식별자 | OCR·구조·임베딩 결과 재현과 재처리 판정 |
| generation 식별자 | 온라인 응답이 사용한 corpus snapshot 확인 |
| stable evidence 식별자 | 검색 결과에서 전체 원문·출처로 재조회 |

식별 문자열과 schema는 ADR-0002 및 migration 1~15로 versioning했다. product dedupe, 검색 filter와
응답 조립에서 카드사를 제거하지 않으며 검색 순위나 generation처럼 바뀌는 값을 stable evidence ID의
재료로 사용하지 않는다.

## 7. 저장소와 상태 경계

| 저장 영역 | 주요 내용 | 쓰기 주체 | 읽기 주체 | 수명·운영 원칙 |
|---|---|---|---|---|
| 원본 보관 영역 | PDF, 출처 metadata, content hash | PDF 취합기 | OCR·감사 작업, 제한된 source artifact reader | 전 버전 보존, 수정 대신 새 버전 추가, 온라인에는 게시 승인된 read-only view만 제공 |
| 파생 artifact 영역 | 렌더 페이지, OCR Markdown, 구조 결과 | 해당 offline worker | downstream worker·검증기 | 입력 hash·설정과 연결, 부분 결과와 완료본 구분 |
| 작업 상태 영역 | PostgreSQL의 run, stage, retry, lease, 오류 | orchestrator·worker | 운영 제어·관측 | mutable, 원자 갱신, crash recovery 지원 |
| staging build 영역 | 미완성 catalog·evidence·색인 | 색인 빌더 | 검증기 | online 접근 금지, 실패 시 격리·정리 가능 |
| published generation 영역 | 검증된 검색 snapshot·manifest | 제한된 발행기 | 온라인 MCP read-only | 불변, 세대별 보존, rollback 가능 |
| active 참조 | 현재 서비스할 generation 식별자 | 발행기 | snapshot manager | 원자 전환, generation 본체와 분리 |
| 운영 로그·지표 | 상태 전이, 오류, 성능, 감사 event | 모든 구성요소 | 운영자·monitoring | 민감정보 redaction과 보존 기간 적용 |
| 비밀정보 영역 | API key, OAuth·Codex 인증 | 배포·secret manager | 필요한 역할만 | Git·이미지·일반 volume에 저장 금지 |

원본 PDF·OCR·published generation은 단일 Linux host의 외부 불변 file volume에 둔다. durable 작업 상태와 catalog는 PostgreSQL에 저장한다. 하나의 공유 쓰기 볼륨을 모든 컨테이너에 마운트하지 않고 온라인 서비스에는 게시된 generation과 승인된 source artifact view만 read-only로 제공한다.

저장 경계는 SHA-256 content-addressed PDF/OCR object, PostgreSQL 17.11 schema·migration 1~15,
PostgreSQL FTS+pgvector HNSW와 불변 file generation으로 구현했다. 실제 전체 corpus의 index size와
host resource 한도는 실환경 BULK에서 측정·보정한다. 0.2 운영 배치는 object/generation을
명시적 host bind로 두고 PostgreSQL 두 DB, 전체 CAS와 generation pointer를 같은 maintenance
epoch의 portable package로 묶는다. 상세 계약은
[레거시 Import·호스트 영속 저장·서버 이전 운영서](09_LEGACY_IMPORT_AND_PORTABLE_STATE.md)에 있다.

## 8. generation build와 발행 경계

generation 발행은 데이터 처리와 서비스 운영을 분리하는 핵심 계약이다.

### 8.1 발행 전 최소 검증

- manifest와 schema version을 읽을 수 있다.
- catalog의 모든 검색 문서가 존재하는 OCR·원본 metadata로 연결된다.
- structured 단위가 유효한 문서와 원문 구간을 참조한다.
- 임베딩 count, dimension, 입력 hash와 model 설정이 manifest와 일치한다.
- text/vector index가 예상 문서 범위를 포함한다.
- 대표 상품·혜택·전월실적·제외조건 질의의 smoke test가 통과한다.
- 금지된 이메일·인증정보·임시 chunk·실패 artifact가 포함되지 않는다.
- generation 전체에 필요한 checksum 또는 동등한 무결성 정보가 있다.

개발 합성 gate는 source-span 100%, Recall@10 95% 이상, critical Recall@10 100%, MRR·nDCG@10
0.90 이상과 filter 정확도 100%로 확정했다. 실제 카드사 layout·provider·전체 corpus에는 같은
evaluator를 적용하고 필요하면 새 ADR로 임계값을 보정한다.

### 8.2 발행과 전환

- staging 경로에서 검증 중인 generation은 온라인에서 보이지 않아야 한다.
- 검증 완료 후 generation 본체는 더 이상 수정하지 않는다.
- active 참조 전환은 중간 상태가 보이지 않는 원자적 방법을 사용한다.
- 온라인의 진행 중 요청은 시작할 때 선택한 generation을 끝까지 사용한다.
- 새 generation open 또는 readiness가 실패하면 active 참조를 이전 generation으로 되돌린다.
- 성공한 검색 generation은 최근 3개를 보존한다. 실패 candidate는 조사 가능하도록 7일 보존한 뒤 정리하고, 수동 pin한 generation은 명시적 unpin 전까지 보존한다.

v1은 같은 filesystem의 atomic `current.json` 교체와 PostgreSQL `active_generation` row를 함께
대사하는 publication protocol을 사용한다. 실패 시 DB/file 상태를 보상하고 readiness가 두 권위의
불일치를 차단한다. 다른 storage나 다중 node로 전환할 때만 별도 alias/control-plane 방식을 재검토한다.

## 9. 실패 격리

| 실패 지점 | 격리 단위 | 온라인 서비스 영향 | 처리 방향 |
|---|---|---|---|
| 카드사 discovery 실패 | 카드사·run | 없음 | 이전 generation 유지, backoff 후 재시도, markup 변경 경보 |
| PDF 다운로드·검증 실패 | document version | 없음 | 실패 원인·시도 기록, 다른 문서 계속 처리 |
| OCR chunk 실패 | PDF·page chunk | 없음 | 완료 checkpoint 보존, 실패 chunk 재시도, 불완전 OCR 미발행 |
| 구조 분석 실패 | document version | 없음 | OCR 원문 보존, 해당 문서 downstream 보류 |
| OpenRouter 임베딩 장애 | batch·document set | 없음 | backoff·재개, 불완전 벡터 generation 미발행 |
| 색인 build 실패 | staging generation | 없음 | staging 격리, active generation 변경 금지 |
| generation 검증 실패 | staging generation | 없음 | 실패 보고서 보존, 발행 차단 |
| active 전환 실패 | generation pointer | 제한적 또는 없음 | 이전 참조 유지·rollback, readiness 실패 표시 |
| 온라인 query embedding·vector 장애 | 개별 요청 | opt-in degraded | caller가 `allow_degraded=true`인 경우에만 lexical-only와 `degraded` 상태를 반환하고, 그 외에는 요청 실패 |
| MCP replica 장애 | replica | 가용성 정책에 따름 | 재시작·traffic 제외; replica 수와 목표 가용성은 부하 시험 후 Codex 결정 |
| published snapshot 손상 | generation | 영향 가능 | checksum 감지와 이전 generation rollback; 필요하면 verified portable state를 빈 target에 복원 |

오프라인 실패가 현재 온라인 generation을 손상시키지 않는 것이 최우선이다. 최신 문서의 OCR·구조·색인 누락 또는 실패는 generation 게시를 차단하고 이전 generation을 계속 서비스한다. 과거 이력 실패는 quarantine·보고서에 명시하며 최신 문서 coverage가 100%인 경우에만 게시할 수 있다.

## 10. 일관성과 동시성 원칙

- 작업 claim은 조회 후 갱신하는 두 단계 경쟁이 아니라 원자적 소유권 획득이어야 한다.
- 장시간 worker에는 lease, heartbeat 또는 동등한 crash recovery 수단이 필요하다.
- 동일 문서·단계의 중복 작업은 content hash와 설정 식별자로 감지한다.
- online request 하나는 catalog, evidence와 vector index를 같은 generation에서 읽는다.
- 발행 도중 replica별로 서로 다른 generation을 사용할 수는 있지만, 각 응답에는 자신이 사용한 generation을 명시한다.
- read path는 schema 생성이나 빈 DB 자동 생성을 수행하지 않는다. 필요한 파일이 없으면 readiness 실패로 처리한다.
- 스케줄 중복과 여러 카드사 동시 실행이 서로의 run report를 오인하지 않도록 모든 상태를 issuer와 run ID로 범위화한다.

## 11. Docker 운영 형태

최초 배포는 단일 Linux host의 Docker Compose를 사용하고 최소 논리 배포 단위는 다음 두 가지다.

1. **offline worker 계열:** 수집·OCR·구조·임베딩·build를 수행하고 writable volume을 사용한다.
2. **online MCP server:** 상시 실행하며 published generation을 read-only로 사용한다.

scheduler, 발행기, 관측 agent는 초기에는 worker의 제한 entrypoint 또는 Compose job으로 운영할 수 있다. 이후 부하·운영 복잡도에 따라 별도 컨테이너로 분리한다. 어떤 구성을 선택해도 다음은 지켜야 한다.

- 대용량 PDF·OCR·색인은 이미지에 포함하지 않는다.
- 작업 상태와 artifact는 컨테이너 재생성 뒤에도 유지된다.
- Codex CLI 인증과 OpenRouter key는 역할별 secret으로 주입한다.
- 헤드리스 Codex device-code 흐름은 exact CLI 버전에서 지원 여부를 먼저 검증한다. 지원되는 경우에만 전용 offline auth job의 제한된 로그로 필요한 URL·user code를 노출하고 로그 보존·접근을 제한한다.
- online 이미지는 수집·OCR용 실행파일과 권한을 갖지 않는 구성을 우선한다.
- 공개 Docker Hub image에는 corpus·PDF·OCR·secret·인증 상태를 포함하지 않는다. GitHub가 private여도 image에 패키징된 애플리케이션 코드와 dependency metadata는 외부에서 열람 가능함을 전제로 한다.
- readiness는 프로세스 생존뿐 아니라 generation open, schema, FTS/vector 사용 가능성을 확인한다.
- graceful shutdown 시 새 작업 claim을 중단하고 현재 checkpoint 또는 질의를 안전하게 마친다.
- MCP application은 container 내부 `0.0.0.0:8000`에서 수신하고 Docker가 host `127.0.0.1:8000`에만 publish한다. TLS·외부 hostname·Nginx Proxy Manager 연결은 개발 완료 후 별도 hosting 과제이며 stack에 reverse proxy를 포함하지 않는다.

Docker Hub public repository는 `ymtop59/mcp-card-prd-detail`로 생성했다. v1 platform은 `linux/amd64`로
한정한다. 일반 `main` push는 build·test까지만 수행하고 공개 registry에 push하지 않는다. `vX.Y.Z`
release tag 대상 수동 workflow와 exact confirmation을 모두 통과한 MCP/worker/admin target만 역할별 version+Git SHA tag로
push한다. GitHub Actions OIDC 기반 keyless Cosign으로 각 역할 digest를 서명하고, private GitHub
repository·workflow URI가 transparency log에 공개될 수 있음을 승인한다. 배포·rollback은 역할별
digest를 기준으로 한다. base image와 Compose CPU·memory 개발 기본값은 고정했고 운영 host 한도는
실제 BULK·질의 측정으로 보정한다.

## 12. 외부 의존성 경계

| 외부 대상 | 접근 주체 | 용도 | 필수 통제 |
|---|---|---|---|
| 카드사 공시 endpoint | 수집 adapter·PDF 취합기 | discovery와 PDF 다운로드 | host allowlist, rate limit, timeout, redirect·크기 검증 |
| Codex CLI | OCR 처리기 | 고품질 OCR, 선택적 구조 분석 | 실제 모델·설정 provenance, 격리 권한, 인증정보 보호 |
| OpenRouter | 임베딩 생성기 | 문서 임베딩 | model·dimension 검증, retry, 비용·rate 관측 |
| OpenRouter | 온라인 질의 서비스 후보 | query embedding | cache·circuit breaker, opt-in lexical-only degraded 표시 |
| self-hosted Keycloak | MCP client·resource server | 단일 tenant client 등록, access/refresh token 발급·회전·폐기 | OAuth 2.1/OIDC discovery, audience, `search`·`source_pdf`, secure token storage |
| MCP client | HTTP MCP protocol adapter | 검색·상세·페이지·원본 PDF 조회 | HTTPS, Bearer token, `search`·`source_pdf` scope, 요청·파일 크기 제한 |

OCR 일반 실행의 OpenRouter 페일오버와 구조 분석 provider는 품질 동등성 검증을 통과하기 전에는 활성화하지 않는다. 초기 대량 OCR은 Codex exec만 사용한다. 구조 분석에서 LLM을 사용하기로 결정하면 Codex exec를 우선하고 OpenRouter를 페일오버로 사용한다. provider 또는 model을 바꾸는 페일오버는 문서 단위 새 attempt이며, 일부 성공 결과가 있어도 전체 문서를 다시 처리해 한 문서 안에서 backend 결과를 혼합하지 않는다.

## 13. 개발 중 확정한 아키텍처 항목

개발 중 선택한 기술값과 근거는 `docs/adr/`에 기록했다. 실제 provider·전체 corpus·운영 host에서만
측정 가능한 값은 기본값을 유지하되 운영 인계 후 새 ADR로 보정한다.

| 항목 | 상태 | 결정이 영향을 주는 영역 |
|---|---|---|
| 목표 latency, QPS, 가용성 | 개발 기준선 확정 | 동시 5, timeout 45초, 초기 P95 30초; 실제 corpus에서 보정 |
| hybrid 엔진과 ranking | 결정 완료 | PostgreSQL FTS+pgvector HNSW, 공통 evidence ID RRF, query embedding 1회 |
| vector/lexical 검색 엔진 | 결정 완료 | 1,536차원 vector와 prefilter; Python BLOB full scan 금지 |
| 구조 분석 엔진 | 결정 완료 | canonical OCR 불변, 결정론적 rule baseline+exact span validator; LLM 보강 기본 off |
| scheduled job 세부 구현 | 결정 완료 | host systemd one-shot, DB lease heartbeat, 03:00/04:00 KST |
| file layout·PostgreSQL 운영 방식 | 결정 완료 | content-addressed object, immutable generation, migration 1~15, host bind+portable state |
| generation 전환 방식 | 결정 완료 | DB/file 대사, request pinning, compensation 가능한 publish/rollback |
| 원본 PDF 이용조건 | 일부 결정 | 승인 사용자·100 MB·Range·감사 90일은 확정, 저작권·재배포 조건은 별도 확인 |
| generation 부분 실패 | 결정 완료 | 최신 문서 실패는 게시 차단, 과거 실패는 격리·보고하고 최신 coverage 100%일 때만 게시 |

원본 PDF 이용조건과 실제 provider·전체 corpus 성능은 공개 운영 전 확인할 외부 gate다. 나머지
기술 선택은 ADR·fixture/load/integration 증거로 확정했다. 제품 범위나 외부 공개 권한을 바꾸는
결정만 사용자에게 다시 확인한다.

## 14. 아키텍처 검증 조건

아키텍처 구현 완료는 최소 다음 증거가 있을 때만 인정한다.

- offline worker가 active generation에 쓰기 권한이 없고 online server가 build·작업 상태에 쓰기 권한이 없다는 배포 검증
- 중단·재시작 후 동일 문서를 중복 처리하지 않고 실패 단계에서 재개하는 통합 테스트
- build 중에도 기존 generation의 검색 결과가 변하지 않는 동시 실행 테스트
- 실패 generation이 발행되지 않고 이전 generation으로 rollback되는 테스트
- 모든 검색 결과가 같은 generation의 문서 버전과 stable evidence로 역추적되는 검증
- 카드사·문서 버전 filter가 검색 전 단계부터 적용되는 다중 카드사 테스트
- 선택한 검색 엔진이 확정된 corpus 규모와 목표 지연시간·QPS를 충족한다는 benchmark
- 컨테이너 재생성 후 원본·OCR·작업 상태·generation이 유지되는 복구 테스트
- secret과 대용량 데이터가 Git 이력·Docker image layer·일반 로그에 없다는 점검

위 조건 중 개발환경에서 재현 가능한 항목은 fixture/load/integration 및 배포 검증으로 수행했다.
실제 카드사 endpoint·외부 모델·전체 corpus 성능, 운영 host와 public release는
[실환경 검증 및 운영 인계](REAL_ENV_HANDOFF.md)의 성공 조건으로 남긴다.
