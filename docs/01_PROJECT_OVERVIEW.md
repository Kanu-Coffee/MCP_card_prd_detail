# 프로젝트 개요

> 문서 상태: 설계 가이드 초안
> 기준일: 2026-08-12
> 구현 상태: 문서화 단계이며, 이 문서에 설명된 신규 시스템은 아직 구현되지 않았다.

## 1. 문서 목적

이 문서는 카드상품안내장 기반 신규 MCP 서비스의 배경, 목표, 범위와 핵심 설계 원칙을 정의한다. 이후 개발자는 이 문서를 통해 “무엇을 만들 것인지”와 “무엇을 그대로 가져오지 않을 것인지”를 먼저 이해하고, 상세한 구성요소·데이터 재사용·운영 문서로 이동한다.

레거시의 세부 조사 결과와 수치 근거는 [CardRAG 레거시 프로젝트 분석 기록](../LEGACY_PROJECT_ANALYSIS.md)을 기준으로 한다. 이 문서는 레거시를 현재 운영 중인 서비스로 간주하거나, 조사 시점의 데이터를 실시간 최신 정보로 간주하지 않는다.

## 2. 배경

대상 레거시는 카드사 공시 자료를 수집해 PDF를 내려받고, 페이지 이미지 렌더링과 OCR을 수행한 뒤 SQLite 기반 검색 데이터로 만드는 배치형 CardRAG 파이프라인이다. CLI·운영 스크립트·이메일 Agent는 존재하지만 MCP 서버와 Docker 운영 환경은 없다.

확인된 데이터 흐름은 다음과 같다.

```text
카드사 공시 수집
  → 문서 정규화 및 변경 판단
  → PDF 다운로드
  → 페이지 렌더링
  → OCR Markdown 생성
  → evidence/구조화 데이터 생성
  → 임베딩 및 검색 DB 생성
  → CLI·이메일 Agent 조회
```

레거시에는 재사용 가치가 있는 corpus와 도메인 계약이 있지만, 대용량 배치와 온라인 질의를 같은 작업 디렉터리와 SQLite 파일에 결합한 구조다. 신규 프로젝트는 이를 그대로 컨테이너에 포장하는 작업이 아니라, 자산을 선별해 데이터 처리와 조회 서비스를 분리하는 재설계다.

## 3. 레거시 기준선 요약

아래 수치는 2026-07-12 생성 릴리스를 2026-08-11에 읽기 전용으로 조사한 결과다. 향후 신규 시스템의 처리량 산정과 이전 검증을 위한 기준선이며 현재 카드사 상품의 실시간 현황을 뜻하지 않는다.

| 항목 | 확인 결과 | 해석 |
|---|---:|---|
| 데이터 패키지 | 약 9.51 GiB, 9,551개 파일 | 전체를 Git이나 Docker 이미지에 포함하기에 부적절하다. |
| 지원 카드사 | 우리카드, KB국민카드 | 다른 카드사는 신규 adapter가 필요하다. |
| 성공 문서 버전 | 1,592건 | 우리카드 915건, KB국민카드 677건이다. |
| 최신 표시 문서/상품 | 1,567건 | 과거 버전과 최신본을 구분해 다뤄야 한다. |
| evidence chunk | 18,669건 | 원문 근거 검색의 기존 자산이다. |
| structured section | 20,318건 | 상품·혜택·조건 단위 구조화의 출발점이다. |
| sentence unit | 118,418건 | 세밀한 근거 검색 후보지만 검색 비용을 검토해야 한다. |
| 현재 hash와 차원이 맞는 임베딩 단위 | 138,736건 | section과 sentence의 합계이며 재사용 전 모델·세대 검증이 필요하다. |

확인된 주요 한계는 다음과 같다.

- MCP SDK, MCP 전송 계층, 장기 실행 서버, 인증·인가가 없다.
- Dockerfile, 컨테이너 상태 점검, 볼륨·비밀정보 운영 구성이 없다.
- 현행 vector 검색은 SQLite의 벡터 BLOB 전체를 Python에서 순회하므로 온라인 질의 경로에 그대로 사용하기 어렵다.
- 현행 hybrid 검색은 FTS와 structured 데이터의 식별자 공간이 달라 의도한 결합이 충분히 일어나지 않는다.
- evidence와 structured DB를 같은 파일에서 drop/rebuild하므로 온라인 조회와 배치를 안전하게 병행할 수 없다.
- 상태가 manifest, retry ledger, run report와 status 파일로 분산되어 있고 일부 상태 파일은 오래된 실행 정보를 담고 있다.
- OCR·검색 결과의 세대, 실제 사용 모델, 페이지·구간 단위 출처가 모든 경로에서 일관되게 보존되지는 않는다.
- 레거시 데이터에는 이메일 본문과 작업 결과 등 카드 공시 조회에 불필요한 운영 데이터도 섞여 있다.

이 한계는 레거시 자산의 가치와 별개다. 신규 시스템은 검증된 원본·OCR·도메인 메타데이터를 활용하되, 실행 구조와 운영 경계는 새로 만든다.

## 4. 프로젝트 목표

신규 시스템의 목표는 카드상품안내장 PDF에서 정보 손실을 최소화한 검색 corpus를 만들고, 외부 LLM이 MCP를 통해 해당 corpus를 빠르고 일관되게 조회할 수 있도록 하는 것이다.

목표 상태는 다음과 같다.

1. 카드사별 수집기가 상품코드와 문서 버전을 기준으로 신규·변경 PDF를 식별한다.
2. OCR 처리기가 한글 원문, 표, 혜택 조건, 제외조건, 각주 사이의 관계를 가능한 한 보존한 Markdown을 만든다.
3. 구조 분석기가 원문을 대신 요약하지 않고 검색에 필요한 문서 구조와 근거 구간을 연결한다.
4. 임베딩·색인 파이프라인이 초기 전체 처리와 일일 증분 처리를 모두 지원한다.
5. 검증을 통과한 검색 데이터만 불변 세대로 발행한다.
6. 온라인 MCP 서비스는 발행된 세대를 읽기 전용으로 조회하고, 응답에 상품·문서 버전·근거·출처를 함께 제공한다.
7. 컨테이너를 재생성해도 원본, OCR, 색인, 작업 상태가 외부 볼륨에 보존된다.

정확한 원문 재현과 근거 추적성이 처리 비용이나 단순 처리 속도보다 우선한다. 다만 온라인 조회에서는 목표 지연시간과 동시성에 맞는 검색 엔진을 선택해야 한다.

## 5. 범위

### 5.1 목표 시스템의 제품 범위

- 우리카드와 KB국민카드 우선 지원, 신한카드 개인 신용·체크카드 현재본·과거 이력의 신규 adapter 및 BULK 처리 시험
- 카드사별 상품공시 discovery와 PDF 수집
- 상품코드·카드사·문서 유형·효력일·버전을 포함한 정규 문서 식별
- 원본 PDF 변경 이력과 content hash 관리
- 고품질 OCR과 중단 후 재개 가능한 작업 상태 관리
- OCR 결과의 구조 분석과 검색 단위 생성
- OpenRouter 임베딩 전용 모델을 이용한 초기·증분 임베딩
- 키워드, 구조 필터, 의미 검색을 조합할 수 있는 검색 계층
- 카드상품 검색, 상품 상세, 혜택 조건, 전월실적, 제외조건과 원문 근거를 제공하는 읽기 전용 MCP 역할
- 명시적 사용자 요청에 대한 보존 원본 PDF 파일 제공과 페이지 단위 OCR·근거 조회
- 독립적인 오프라인 작업과 상시 실행 온라인 서비스
- Docker 기반 실행, 외부 볼륨, 비밀정보 주입, 상태 점검, 백업·복구

### 5.2 이번 문서화 작업의 범위

- 목표 구조와 구성요소 경계 정의
- 레거시 자산의 재사용·변환·제외 분류
- LLM 호출과 데이터 품질 원칙 정의
- 초기 대량 처리와 일일 증분 처리 운영 방향 정의
- 단계별 구현 로드맵, 검증 기준과 완료 체크리스트 작성

이번 산출물은 향후 구현을 위한 개발 하네스다. 설계 문서가 존재한다는 사실만으로 어느 구성요소도 완료된 것으로 보지 않는다.

## 6. 비범위

### 6.1 이번 단계에서 수행하지 않는 작업

- MCP 서버, 수집기, OCR worker, 색인기 또는 검색 코드 구현
- OCR·구조 분석·임베딩 실행과 카드사·Codex·OpenRouter 등 외부 호출
- Python 또는 시스템 의존성 설치
- Docker 이미지 빌드·실행·레지스트리 배포
- 대용량 레거시 데이터 복사·이동·변환
- API key, OAuth token, Codex 인증정보 생성·변경
- 성능·정확도·복구 테스트 실행

### 6.2 신규 공개 MCP 서비스에서 제외할 영역

다음 기능은 카드 정보 조회용 공개 MCP 도구에 직접 포함하지 않는다.

- 임의 URL에서 PDF 다운로드
- OCR과 대량 재색인 실행
- DB drop/rebuild 또는 generation 발행
- retry ledger·작업 상태의 임의 수정
- Gmail 읽기·답장과 범용 이메일 Agent
- 임의 파일시스템 경로로 내보내기

이 기능은 별도의 관리자 권한과 durable job 경계를 가진 오프라인 운영 영역으로 설계한다. v1 관리자 표면은 local 운영 CLI와 scheduled job으로 한정하며 공개 관리자 API·웹 UI는 만들지 않는다.

## 7. 핵심 설계 원칙

### 7.1 온라인 읽기와 오프라인 쓰기의 분리

수집·OCR·구조 분석·임베딩은 외부 네트워크, 장시간 작업, 높은 권한과 대용량 쓰기를 요구한다. MCP 질의 처리는 짧고 예측 가능한 읽기 작업이어야 한다. 두 영역은 프로세스, 권한, 실행 주기, 저장소와 장애 영향을 분리한다.

### 7.2 검증 후 불변 generation 발행

배치는 현재 서비스 중인 검색 DB를 직접 수정하지 않는다. 격리된 작업 공간에서 새 generation을 완성하고, schema·건수·hash·임베딩 차원·기본 검색을 검증한 뒤 발행한다. 온라인 서비스는 발행된 generation만 읽기 전용으로 연다. 이전 generation은 정책에 따라 일정 기간 보존해 rollback할 수 있어야 한다.

### 7.3 원문과 출처 우선

OCR Markdown을 일차 원문으로 유지하고, 구조 분석·검색 단위·임베딩이 원문 구간으로 역추적되어야 한다. 응답에는 가능한 범위에서 카드사, 상품코드, 상품명, 문서 버전 또는 기준일, 관련 섹션, 원문 근거와 corpus generation을 포함한다. 부족한 정보와 버전 충돌은 숨기지 않는다.

### 7.4 카드사와 버전을 포함한 안정적인 식별

상품코드만으로 dedupe하지 않는다. 카드사 범위의 상품 식별자와 문서 버전 식별자를 사용하고, 같은 상품의 과거본과 최신본을 구분한다. 수집부터 MCP 응답까지 같은 식별 축을 보존한다.

### 7.5 멱등성, 재개 가능성, 중복 방지

각 단계는 입력 content hash와 처리 설정을 바탕으로 완료 여부를 판정해야 한다. 초기 대량 처리가 중단되어도 완료 artifact는 재사용하고, 실패한 단계부터 재개할 수 있어야 한다. 실패는 자동 재시도 가능, 사람 검토 필요, 영구 제외를 구분한다.

### 7.6 품질 관문을 통과한 데이터만 검색에 노출

파일이 존재한다는 이유만으로 성공으로 간주하지 않는다. PDF 유효성, OCR 페이지·문자·hash, 필수 메타데이터, 구조 단위의 원문 연결, 임베딩 count·차원·유한값, 색인 조회를 단계별로 확인한다. 검증 실패 generation은 발행하지 않는다.

### 7.7 최소 권한과 명시적 외부 통신

온라인 컨테이너에는 PDF 수집, OCR 실행, 이메일 발송, 작업 DB 수정 권한을 주지 않는다. 비밀정보는 Git·이미지·corpus에 넣지 않고 런타임에 주입한다. 외부 통신은 역할별 allowlist와 timeout·크기·redirect 정책으로 제한한다.

### 7.8 데이터와 애플리케이션 수명주기 분리

약 9.51 GiB의 레거시 전체나 향후 corpus를 이미지 layer에 포함하지 않는다. 애플리케이션 이미지, 원본·OCR 보존 데이터, build 작업 공간, 발행 검색 generation, 작업 상태와 비밀정보를 별도 수명주기로 관리한다.

### 7.9 설정 가능성과 provenance

OCR·구조 분석·임베딩 엔진, 모델, prompt·설정 버전과 실행 시각을 artifact metadata에 기록한다. 모델과 provider를 코드에 고정하지 않는다. 설정값을 기록하는 것과 실제 실행에 적용되는 것을 검증해야 한다.

### 7.10 측정 가능한 완료 기준

“구현함”이 아니라 실제 artifact, 테스트 결과와 운영 증거로 완료를 판단한다. 데이터 품질, 검색 품질, 지연시간, 복구와 보안 기준을 사전에 정의하고 체크리스트에 증거 위치를 남긴다.

## 8. 현재 확정된 방향과 구현 중 기술 결정

### 8.1 이 문서세트에서 채택한 방향

- 오프라인 처리와 온라인 MCP 서비스는 논리적·운영적으로 분리한다.
- 온라인 MCP는 발행된 검색 generation에 대해 읽기 전용이다.
- 레거시 원본은 수정하지 않고 선별 복사·변환 대상으로만 취급한다.
- PDF, OCR, 검색 데이터와 작업 상태는 Docker 이미지 밖에 둔다.
- 초기 수집은 상품별 최신 PDF를 우선하고 이후 신규·변경본을 증분 처리한다.
- OCR은 Codex exec 우선이며 초기 대량 처리는 Codex exec만 사용한다. 일반 실행의 OpenRouter 페일오버는 별도 품질 동등성 검증 후 허용한다.
- 구조 분석에 LLM을 사용하면 Codex exec를 우선하고 OpenRouter를 페일오버로 사용한다. 구체 엔진은 Codex가 gold set의 규칙-only·LLM 보강 비교로 구현 중 결정한다.
- 임베딩은 OpenRouter의 임베딩 전용 모델을 사용하되 모델은 설정으로 교체 가능해야 한다.
- 모든 조회 결과는 문서 버전과 근거로 역추적 가능해야 한다.
- 1차 지원 대상은 우리카드와 KB국민카드이며, 신한카드는 개인 신용·체크카드 상품안내장의 현재본과 과거 이력을 신규 adapter로 수집해 BULK 처리 시험에 포함한다. 법인·선불카드는 신한 1차 범위에서 제외한다.
- 기본 검색은 최신본으로 제한하고 과거본은 모두 보존한다. 과거본은 명시적 version 또는 as-of 요청에서만 조회한다.
- 운영 MCP는 HTTP endpoint로 제공하고 HTTPS URL과 OAuth token으로 접속한다. client별 `search`·`source_pdf` scope를 분리하고 token은 인증 header로 전달하며 URL·log에 노출하지 않는다. 최초 승인 후 access token 갱신과 refresh token 회전은 client가 자동 수행하고, 90일 비활성·폐기·분실·미지원 상황에서만 재인증을 요구한다.
- 기존 OAuth/OIDC provider가 없으므로 self-hosted Keycloak 단일 tenant를 사용한다. 같은 Compose의 별도 service와 PostgreSQL 별도 database·user로 격리하고 realm은 `cardrag`로 한다. self-registration·dynamic client registration은 끄며 승인 client만 수동 등록한다. 사람용 client는 Authorization Code+PKCE, service client는 Client Credentials를 사용하고 초기 admin credential은 Docker secret으로 1회 bootstrap한 뒤 회전·제거한다. 애플리케이션 운영 권한은 local CLI로 분리한다.
- 검색은 공통 stable evidence key를 사용하는 lexical/vector hybrid를 기본 정책으로 한다.
- 명시적으로 요청된 보존 원본 PDF 전체를 인증 후 streaming file로 제공한다. 페이지 조회용 PNG는 요청 시 렌더링하고 7일 cache 후 제거하며 영구 저장하지 않는다. 분할 PDF는 만들지 않고 임의 URL 다운로드는 계속 금지한다.
- GitHub는 private, Docker Hub image repository는 public으로 운영한다. 공개 image에는 corpus와 secret을 포함하지 않는다.
- 원본 PDF와 OCR 버전은 모두 보존한다. 성공 generation 최근 3개, 실패 candidate 7일, 수동 pin generation은 해제 시까지 보존한다. Gmail·이메일 Agent는 신규 범위에서 제외한다.
- 초기 동시 요청 기준은 5개이며 응답 품질을 지연시간보다 우선한다. 수치 latency 목표는 BULK pilot 후 정하되 유한 timeout과 cancellation은 항상 둔다.
- image tag는 version과 Git SHA를 포함하고 배포·rollback은 digest 기준으로 수행한다.
- 최초 운영 topology는 단일 Linux host의 Docker Compose이며 online MCP와 offline worker를 별도 컨테이너로 둔다.
- PDF·OCR·generation은 외부 불변 file volume, durable 작업 상태와 catalog는 PostgreSQL을 사용한다. vector/lexical engine은 신한 BULK benchmark 후 정한다.
- vector 경로 장애 시 caller가 `allow_degraded=true`를 명시한 요청만 lexical-only 결과를 `degraded`로 반환하고, 나머지는 실패시킨다.
- reverse proxy·TLS와 Nginx Proxy Manager 연결은 개발 완료 후 운영자가 수행하는 hosting 과제다. stack은 proxy를 포함하지 않고 현재 개발은 container `0.0.0.0:8000`을 host `127.0.0.1:8000`에만 publish한다.
- 원본 PDF는 승인 사용자에게만 제공하고 100 MB 상한, HTTP Range와 90일 감사 metadata 보존을 적용한다.
- 일일 수집은 03:00 KST에 우리카드 → KB국민카드 → 신한카드 순으로 실행하고 각 카드사 job 종료 후 10분 대기하며 issuer 실패를 격리한다.
- 최신 문서 처리 실패 또는 누락은 generation 게시를 차단한다. 과거 이력 실패는 quarantine·보고 후 최신 coverage가 100%일 때 게시를 허용한다.
- backup·restore 구현은 현재 v1 개발 범위에서 제외하고 추후 개선 과제로 보류한다.
- 접근·권한·PDF 감사 metadata는 90일, 비식별 집계 metric은 1년 보존하고 질의 원문은 기본 저장하지 않는다.
- 관리자 기능은 운영 CLI와 scheduled job으로 제한하고 공개 관리자 API·웹 UI는 만들지 않는다.
- public Docker Hub repository `ymtop59/mcp-card-prd-detail`을 생성했다. v1은 `linux/amd64`만 build한다. 일반 `main` push에는 공개 image를 push하지 않고 `vX.Y.Z` release tag와 manual approval을 모두 통과한 digest만 공개한다. GitHub Actions OIDC keyless Cosign으로 서명하며 transparency log에 private repository·workflow identity가 드러날 수 있음을 승인한다.
- OCR·구조 분석 provider 또는 model 전환 시 한 문서 안의 결과를 혼합하지 않는다. 부분 성공분이 있어도 전체 문서를 새 attempt로 재실행한다.

### 8.2 구현 중 Codex 결정과 외부 gate

다음 기술 항목은 개발 시작을 막지 않는다. Codex가 pilot·benchmark·시험 결과로 선택하고 ADR과 체크리스트에 근거를 남긴다.

| 주제 | 상태 | 결정 시 필요한 기준 |
|---|---|---|
| 목표 지연시간·QPS·가용성 | pilot 후 Codex 결정 | 품질 우선 원칙과 초기 동시 요청 5개를 기준으로 BULK·부하 시험에서 측정 |
| vector/lexical 검색 엔진 | BULK 후 Codex 결정 | PostgreSQL 상태·catalog와 외부 file volume은 확정, 검색 엔진은 corpus benchmark로 선정 |
| hybrid 구현·ranking | 구현 중 Codex 결정 | 공통 evidence key 결합은 확정, 엔진·가중치·후보 수는 카드 도메인 benchmark로 결정 |
| 구조 분석 엔진 | 구현 중 Codex 결정 | 규칙 기반, LLM 보조, 혼합 방식의 정확도·재현성·비용 평가 |
| 온라인 query embedding | 구현 중 Codex 결정 | 장애 시 opt-in lexical-only 정책은 확정, OpenRouter 호출·cache·회로 차단 상세를 시험으로 결정 |
| 원문·PDF 이용 조건 | 일부 결정 | 승인 사용자·100 MB·Range·감사 90일은 확정, 재배포·상업적 이용 조건은 별도 확인 |
| 보존·삭제·감사 정책 | 결정 완료 | PDF/OCR 전 버전, 성공 generation 최근 3개, 실패 candidate 7일, pin은 해제 시까지, 감사 90일·metric 1년, PNG cache 7일 |
| backup·restore | v1 범위 밖 | 추후 개선 과제로 별도 설계·구현 |

개발 착수에 필요한 제품·운영 P0 결정은 완료됐다. 공시자료 이용조건은 공개 운영 범위를 넓히기 전 확인하는 외부 gate이며, 승인 사용자 한정 개발과 품질 검증의 시작을 막지 않는다.

## 9. 성공 상태

프로젝트의 최종 성공은 다음 상태가 증거로 확인될 때 판단한다.

- 지원 카드사의 최신·변경 문서를 반복 가능하게 수집하고 버전 이력을 보존한다.
- 중단 후 재개한 OCR 결과가 페이지와 핵심 조건을 누락하지 않는다는 품질 검증을 통과한다.
- 구조화·임베딩 단위가 원문 근거와 일관되게 연결된다.
- 발행 generation이 무결성 및 검색 품질 관문을 통과하고 안전하게 전환·rollback된다.
- 온라인 MCP가 발행 generation만 읽고, 버전·근거·불확실성을 포함한 결과를 목표 성능 내에서 반환한다.
- 권한이 있는 사용자의 명시적 요청에 정확한 version·hash의 원본 PDF를 제공하고 페이지 근거를 조회할 수 있다.
- 컨테이너 재생성, worker 실패, 외부 provider 장애가 서비스 중인 generation을 손상시키지 않는다.
- 비밀정보와 대용량 corpus가 Git 및 Docker 이미지에 포함되지 않는다.
- 모든 완료 항목에 자동 테스트, 검증 보고서 또는 운영 기록이 연결된다.

현재는 위 조건을 위한 문서가 작성되는 단계이며, 어느 조건도 구현 완료로 표시하지 않는다.
