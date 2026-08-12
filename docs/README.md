# CardRAG MCP 개발 문서 하네스

## 문서세트의 목적

이 디렉터리는 카드상품안내장 PDF를 수집하고, 원문 충실도 중심으로 OCR·구조화·색인한 뒤, 외부 LLM이 근거와 출처를 포함해 조회할 수 있는 MCP 시스템을 구현하기 위한 개발 기준을 제공한다.

현재 산출물은 **설계·개발·운영 가이드**다. 신규 MCP 서버, 수집기, OCR·임베딩 파이프라인, Docker 이미지가 구현되었다는 의미가 아니다.

기준이 되는 레거시 조사 결과는 [LEGACY_PROJECT_ANALYSIS.md](../LEGACY_PROJECT_ANALYSIS.md)다. 레거시는 약 9.51 GiB의 배치형 CardRAG 데이터 패키지이며, 신규 시스템은 이를 그대로 이전하거나 컨테이너 이미지에 포함하지 않는다.

## 프로젝트 목표

- 카드사별 상품코드를 기준으로 최신 카드상품안내장과 변경 이력을 수집한다.
- PDF 원문 구조와 문구를 최대한 보존한 OCR Markdown을 만든다.
- 혜택, 조건, 전월실적, 제외조건, 유의사항의 관계를 잃지 않도록 구조화한다.
- 초기 전체 처리와 일일 신규·변경 문서의 증분 처리를 모두 지원한다.
- 검증된 색인 세대만 상시 실행되는 읽기 전용 MCP 서비스에 게시한다.
- 검색 결과에 카드사, 상품코드, 문서 버전·기준일, 관련 섹션과 원문 근거를 포함한다.
- 장기 작업의 중단·재개, 중복 방지, 실패 재처리와 rollback이 가능하도록 한다.

## 현재 상태

기준일: 2026-08-12

| 영역 | 상태 | 설명 |
|---|---|---|
| 레거시 조사 | 검증 완료 | 코드·manifest·주요 SQLite와 데이터 규모를 읽기 전용으로 분석 |
| 목표 아키텍처 | 문서화 완료 | 오프라인 처리와 온라인 MCP 분리 원칙 수립 |
| 구성요소 개발 가이드 | 문서화 완료 | 입력·출력·실패 처리·완료조건 정의 |
| 레거시 데이터 재사용 계획 | 문서화 완료 | 원본을 변경하지 않는 분류·이전·검증 방향 정의 |
| 신규 시스템 구현 | 미착수 | 실제 서비스·worker·수집기·DB schema 없음 |
| OCR·구조 분석·임베딩 실행 | 미수행 | 외부 모델/API 호출 없음 |
| Docker Hub repository | 생성 완료 | public `ymtop59/mcp-card-prd-detail`, 아직 image 없음 |
| Docker 빌드·배포 | 미수행 | Dockerfile·Compose·image build/push 없음 |

상세 상태의 단일 기준은 [08_COMPLETION_CHECKLIST.md](08_COMPLETION_CHECKLIST.md)다. 실제 파일·코드·시험 증거가 없는 항목은 완료로 표시하지 않는다.

## 문서 목차

1. [프로젝트 개요](01_PROJECT_OVERVIEW.md)
   배경, 레거시 요약, 범위·비범위, 핵심 설계 원칙
2. [목표 아키텍처](02_TARGET_ARCHITECTURE.md)
   오프라인/온라인 경계, 전체 데이터 흐름, 저장소와 장애 경계
3. [구성요소 개발 가이드](03_COMPONENT_DEVELOPMENT_GUIDE.md)
   수집기, OCR, 구조 분석, 임베딩·색인, MCP 서비스의 개발 기준
4. [레거시 데이터 재사용 가이드](04_LEGACY_DATA_REUSE_GUIDE.md)
   자산 분류, 목표 디렉터리, 복사·변환·검증 방향
5. [LLM 및 데이터 품질 정책](05_LLM_AND_DATA_QUALITY_POLICY.md)
   Codex exec/OpenRouter 역할, 품질 gate, 손실·환각 방지
6. [운영 및 배포 가이드](06_OPERATIONS_AND_DEPLOYMENT_GUIDE.md)
   초기 대량·일일 증분 처리, 재시작, Docker·볼륨·인증·백업 계획
7. [구현 로드맵](07_IMPLEMENTATION_ROADMAP.md)
   개발 순서, 의존관계, 단계별 산출물과 인수 기준
8. [완료 체크리스트](08_COMPLETION_CHECKLIST.md)
   영역·카드사별 미착수/진행 중/검증 완료 상태

## 권장 읽기 순서

처음 참여하는 개발자는 다음 순서로 읽는다.

1. 이 문서에서 현재 상태와 용어를 확인한다.
2. 프로젝트 개요와 목표 아키텍처로 시스템 경계를 이해한다.
3. 구성요소 개발 가이드와 품질 정책으로 구현 기준을 확인한다.
4. 레거시 데이터를 다룰 때만 재사용 가이드를 함께 적용한다.
5. 운영·배포 가이드에서 장기 작업, 인증, 볼륨과 복구 조건을 확인한다.
6. 로드맵에서 현재 단계의 선행조건과 인수 기준을 확인한다.
7. 작업 시작·종료 시 완료 체크리스트를 증거와 함께 갱신한다.

## 확정된 공통 원칙

- 원본 PDF와 canonical OCR은 불변 자산으로 보존한다.
- 카드사와 상품코드를 모든 식별자·검색 필터·근거에 유지한다.
- 오프라인 데이터 처리와 온라인 MCP 서비스의 프로세스·권한·저장소를 분리한다.
- 단계별 산출물은 입력 hash, 설정·모델 식별자, 처리 상태와 연결한다.
- 색인은 별도 경로에서 완성·검증한 뒤 세대 단위로 게시한다.
- 온라인 MCP는 게시된 세대를 읽기 전용으로 사용하며 수집·OCR을 직접 실행하지 않는다.
- 대용량 데이터와 인증정보는 Git과 Docker 이미지에 포함하지 않는다.
- 정보 부족, 버전 충돌, 근거 불일치는 숨기지 않고 응답에 표시한다.

## 사전 결정 기록 (2026-08-12)

- 1차 지원 대상은 우리카드와 KB국민카드다. 신한카드는 개인 신용·체크카드 상품안내장의 현재본과 과거 이력을 신규 adapter로 수집해 BULK 처리 시험에 포함한다. 법인·선불카드는 1차 신한 범위에서 제외한다.
- 기본 검색은 최신 문서를 대상으로 한다. 과거 버전은 모두 보존하고 사용자가 버전 또는 기준일을 명시한 경우에만 조회한다.
- 운영 MCP는 HTTP 기반으로 제공한다. 접속 정보는 endpoint URL과 OAuth token이며, 운영에서는 HTTPS와 `Authorization` header를 사용한다. token을 URL query·path·log에 넣지 않는다. client별 `search`·`source_pdf` scope를 분리한다.
- 최초 승인 후에는 client가 짧은 수명의 access token을 자동 갱신하고 refresh token을 회전해, 정상적으로 계속 사용하는 동안 별도 token 재입력 없이 연결을 유지한다. 90일은 고정 접속 만료가 아니라 비활성 만료 기준이다. refresh token 폐기·분실, 보안사고 또는 client 미지원 시에는 재인증이 필요하다.
- 별도 기존 OAuth/OIDC provider는 없으므로 v1 authorization server는 self-hosted Keycloak 단일 tenant로 구성한다. 승인 사용자와 client만 등록하고 `search`·`source_pdf` scope를 분리하며 애플리케이션 운영 권한은 local CLI로 유지한다.
- 사용자가 명시적으로 요청하면 보존된 전체 원본 PDF를 streaming file로 제공한다. 페이지 조회는 OCR text와 요청 시 생성한 렌더 PNG를 제공하고, PNG는 7일 cache 후 제거하며 영구 보존하지 않는다. 별도 분할 PDF와 임의 외부 URL 다운로드 기능은 제공하지 않는다.
- 검색은 lexical과 vector를 공통 stable evidence key로 결합하는 hybrid 방식을 채택한다. 구체 엔진과 ranking 값은 품질·부하 시험으로 정한다.
- GitHub 저장소는 private, Docker Hub image repository는 public으로 운영한다. 공개 image에는 corpus·secret·인증 상태를 포함하지 않으며, image에 포함된 애플리케이션 코드와 dependency metadata는 외부에서 열람 가능하다는 점을 전제로 한다.
- Gmail·이메일 Agent는 신규 범위에서 제외한다. 원본 PDF와 OCR 버전은 모두 보존하고, 검색 generation은 최소 3개를 보존한다.
- 초기 온라인 동시 요청 기준은 5개로 시작한다. 응답 품질을 지연시간보다 우선하며, 수치 latency 목표는 BULK pilot과 부하 시험 후 정한다. 모든 요청에는 운영 보호를 위한 유한 timeout과 cancellation을 적용한다.
- image tag는 버전과 Git SHA를 포함하고, 실제 배포와 rollback은 image digest를 기준으로 한다.
- 최초 배포는 단일 Linux host의 Docker Compose로 운영하며 online MCP와 offline worker를 별도 컨테이너로 분리한다.
- PDF·OCR·generation은 외부 불변 file volume에 두고, durable 작업 상태와 catalog는 PostgreSQL에 저장한다. vector/lexical 검색 엔진은 신한카드 BULK benchmark 후 선정한다.
- query embedding 또는 vector 검색 장애 시 lexical-only 결과는 caller가 `allow_degraded=true`로 명시한 경우에만 `degraded` 상태로 반환한다. 그렇지 않으면 품질 저하를 숨기지 않고 요청을 실패시킨다.
- reverse proxy와 TLS는 개발 완료 후 별도 Nginx Proxy Manager에서 운영자가 연결하는 hosting 과제로 두며 이 Compose stack에는 포함하지 않는다. 현재 개발은 MCP application이 container 내부 `0.0.0.0:8000`에서 수신하고 Docker가 host의 `127.0.0.1:8000`에만 publish하는 데까지 책임진다.
- 원본 PDF는 이용조건 검토 전까지 승인 사용자에게만 제공하고 파일당 100 MB, HTTP Range, 다운로드 감사 metadata 90일 보존을 적용한다.
- 일일 수집은 03:00 KST에 우리카드 → KB국민카드 → 신한카드 순으로 실행하고 각 카드사 job 종료 후 10분 대기한다. 한 카드사 실패는 다음 카드사 실행을 막지 않는다.
- 최신 문서의 OCR·구조·색인 누락 또는 실패가 있으면 candidate generation 게시를 차단하고 이전 generation을 계속 서비스한다. 과거 이력 실패는 quarantine과 보고서에 명시한 뒤 최신 문서 coverage가 100%일 때만 게시를 허용한다.
- backup·restore 구현은 현재 v1 개발 범위에서 제외하고 추후 개선 과제로 관리한다. 현재 개발에서는 불변 artifact와 명확한 volume 경계를 유지해 후속 backup 도입을 막지 않는다.
- 접근·권한·PDF 감사 metadata는 90일 보존하고 질의 원문은 기본 저장하지 않는다. 비식별 집계 metric은 1년 보존한다.
- 1차 관리자 표면은 운영 CLI와 scheduled job만 제공하며 공개 관리자 API·웹 UI는 만들지 않는다.
- public Docker Hub repository는 `ymtop59/mcp-card-prd-detail`로 생성했으며 향후 검증 image만 이 경로에 push한다. image는 Cosign으로 서명한다.

## 결정이 필요한 공통 항목

다음 항목은 요구사항이나 레거시만으로 확정할 수 없다.

- Keycloak client 등록 방식과 초기 관리자 bootstrap 세부값
- PostgreSQL schema·migration 방식과 vector/lexical 검색 엔진
- BULK pilot 이후 목표 QPS, latency, resource 한도와 가용성
- OCR·구조 분석·임베딩 모델 및 정량 품질 기준
- 카드사 공시자료의 수집·재배포·상업적 이용 조건
- Cosign identity·key 관리 방식과 image promotion 승인 절차

결정 전에는 특정 제품이나 모델을 사실상 확정된 것으로 구현 문서에 기록하지 않는다.

## 문서 변경 규칙

- 설계가 바뀌면 관련 가이드와 체크리스트를 같은 변경에서 갱신한다.
- 완료 상태에는 파일 경로, 시험 결과, image digest 등 재검증 가능한 증거를 남긴다.
- 가정은 `가정`, 미확정 사항은 `결정 필요`, 기술 검증이 필요한 사항은 `검토 필요`로 표시한다.
- 레거시 자산을 조사하더라도 원본 파일과 DB는 수정하지 않는다.
- 운영 명령과 구체 API schema는 실제 구현 저장소가 생긴 뒤 해당 코드와 가까운 문서에서 관리한다.
