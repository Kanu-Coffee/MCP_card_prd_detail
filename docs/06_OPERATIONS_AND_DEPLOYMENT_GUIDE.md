# 운영 및 배포 가이드

## 1. 문서 상태와 범위

이 문서는 신규 CardRAG MCP 시스템의 초기 대량 처리, 일일 증분 처리, 장애 복구, 관측성, Docker 운영과 배포 절차가 갖춰야 할 조건을 정의한다.

- 작성일: 2026-08-12
- 현재 상태: 구현·검증된 v1 운영 계약과 실환경 인계 기준
- 구현 상태: scheduler/worker/MCP, PostgreSQL·Keycloak, 관측성, Docker Compose와 `linux/amd64` 3-role image를 개발 환경에서 구현·검증했다.
- 수행하지 않은 작업: 실제 카드사·Codex/OpenRouter 계정 호출, 전체 9.51 GiB 장시간 BULK, 운영 host 설치, public image push와 Nginx Proxy Manager 연결
- 외부 상태: public `ymtop59/mcp-card-prd-detail` repository는 생성했지만, 정책대로 `vX.Y.Z` tag 대상 수동 workflow와 exact confirmation 전에는 image를 push하지 않았다.

문서의 container, image, volume, metric과 상태 필드는 신규 `src/cardrag`, `compose.yaml`, `Dockerfile`, `deploy/`와 자동시험의 구현 계약이다. 레거시에는 해당 기능이 없으며 계속 read-only로 유지한다. 실계정·운영 host가 필요한 검증 결과를 개발 검증과 혼동하지 않는다.

## 2. 운영 경계

### 2.1 실행 단위

최초 운영 topology는 단일 Linux host의 Docker Compose다. online MCP와 offline worker는 별도 컨테이너로 실행하고 PostgreSQL, 외부 불변 file volume과 운영 job을 같은 Compose project에서 명시적으로 연결한다. 다중 node 전환은 BULK·부하·가용성 측정 후 검토한다.

| 실행 단위 | 주기 | 권한·network | storage | 장애 영향 |
|---|---|---|---|---|
| 온라인 MCP service | 상시 | HTTPS MCP endpoint와 query embedding에 필요한 최소 egress | 게시 generation과 승인 원본 PDF view read-only | 장애 시 조회 중단, ingestion에는 영향 없음 |
| ingestion worker | 초기 대량·일일 증분 | 카드사 endpoint, Codex CLI, OpenRouter 접근 | raw/OCR/build/state read-write | 온라인은 이전 generation으로 계속 서비스 |
| scheduler/controller | 일일 또는 운영자 실행 | job 제출 권한만 | durable state read-write | 새 작업 지연, 현재 MCP 조회는 유지 |
| generation publisher | candidate 검증 후 | current pointer 변경 권한 | generations와 publish metadata write | 실패 시 이전 generation 유지 |

온라인 MCP process에서 카드사 사이트 PDF 다운로드, OCR, DB drop/rebuild, OAuth login, Gmail 전송을 실행하지 않는다. 다만 승인된 사용자 중 `source_pdf` scope를 가진 사용자가 명시적으로 요청하면 게시 대상 document ID에 연결된 보존 원본 PDF 전체를 streaming file로 전달한다. 단일 응답의 원본 PDF 상한은 100 MB이며 HTTP Range 요청을 지원한다. PDF 접근 감사 metadata는 90일간 보존한다. 페이지 PNG는 원본 PDF에서 요청 시 생성하고 7일 cache 후 제거하며 분할 PDF는 만들지 않는다. ingestion worker도 게시된 generation을 in-place 변경하지 않는다.

### 2.2 최소 장애 격리 원칙

- application image와 data generation의 release lifecycle을 분리한다.
- 초기 대량 작업이 멈춰도 MCP는 마지막 검증 generation을 계속 제공한다.
- OpenRouter 또는 Codex 장애가 기존 read-only generation을 훼손하지 않게 한다.
- scheduler와 worker를 여러 개 실행하더라도 같은 job을 중복 claim하지 않게 한다.
- 한 generation 안의 catalog, structured, lexical, vector index는 같은 source snapshot을 가리킨다.
- 외부 volume을 잃거나 container를 재생성해도 job state와 완료 artifact가 유지되어야 한다.

## 3. 초기 대량 처리 운영

### 3.1 예상시간의 해석

초기 OCR·구조화·임베딩은 약 3~4일 이상 걸릴 수 있다는 계획 가정이다. 문서 수, 페이지 수, API rate limit, Codex 처리시간, 재시도와 품질 검증에 따라 더 길어질 수 있으므로 3~4일을 SLA로 약속하지 않는다.

예상 완료시간은 작은 pilot batch의 다음 실측치로 다시 산정한다.

- 문서·페이지당 render와 OCR 시간
- OCR 성공률과 재시도율
- 구조화·embedding 처리량
- 카드사 및 외부 provider rate limit
- worker concurrency별 CPU·memory·disk I/O
- 품질검사와 수동 검토 대기시간

### 3.2 시작 전 preflight

초기 작업은 다음 조건이 모두 확인된 후 시작한다.

1. 레거시 또는 신규 discovery snapshot이 read-only 입력으로 고정되어 있다.
2. PDF·OCR·build·state volume 용량과 inode 여유가 측정되어 있다.
3. source 이용조건, 보존기간과 외부 provider 전송 범위가 승인되어 있다.
4. Codex CLI exact version, 인증상태, OCR model/prompt, timeout이 기록되어 있다.
5. OpenRouter key, model ID, dimension과 retry/rate limit 설정이 주입되어 있다.
6. durable state store와 lease 회수가 실제 restart test를 통과했다.
7. 소수 PDF의 pilot이 원문 충실도와 artifact checksum gate를 통과했다.
8. 게시 중인 generation이 있다면 rollback pointer와 이전 READY generation이 확인되어 있다.

preflight 실패는 처리 실패 document로 세지 않고 run 시작 실패로 기록한다.

### 3.3 대량 처리 순서

1. source snapshot과 document catalog를 만들고 issuer-scoped idempotency key를 부여한다.
2. 최신 문서를 먼저 queue에 넣고 과거 버전도 보존 대상으로 뒤이어 처리한다. 기본 검색은 최신본이며 과거본은 명시적 version/as-of 요청에서만 조회한다.
3. PDF 다운로드·검증, render, OCR, 구조화, embedding을 독립 stage로 실행한다.
4. 각 stage가 검증된 artifact hash를 durable state에 commit한 뒤 다음 stage를 연다.
5. 실패는 retry 대기 또는 dead-letter로 보내고 다른 문서 처리를 계속한다.
6. processing 완료 집합으로 candidate generation을 별도 build 경로에 만든다.
7. 데이터·검색 품질 gate가 모두 통과한 generation만 게시한다.

장기 실행 중에도 처리율, 남은 queue, oldest job age, dead-letter, provider quota와 예상 완료시간을 볼 수 있어야 한다.

초기 대량 OCR은 Codex exec만 사용한다. OpenRouter를 OCR 자동 fallback으로 사용해 실패를 성공으로 바꾸지 않으며, 실패 문서는 retry 또는 dead-letter 상태로 남긴다. OpenRouter key는 이 초기 run에서도 문서·query embedding에 필요할 수 있으므로 OCR 인증과 별개로 preflight한다.

우리카드와 KB국민카드를 우선 처리한다. 신한카드는 개인 신용·체크카드 상품안내장의 현재본과 과거 이력을 신규 BULK 시험 대상으로 포함하고 법인·선불카드는 1차 범위에서 제외한다. 신한카드 adapter는 레거시 재사용이 아니라 신규 구현이며 fixture/live 제한·품질 gate를 통과한 뒤 동일한 일일 schedule에 포함한다.

일일 증분 run은 매일 03:00 KST에 우리카드 → KB국민카드 → 신한카드 순으로 실행한다. 각 issuer job 종료 후 10분 대기하고, 한 카드사의 실패는 다음 카드사 실행을 막지 않으며 독립적인 retry/dead-letter와 경보를 남긴다.

## 4. Durable 작업상태

### 4.1 상태의 단일 기준

분산 JSON과 directory 존재 여부를 완료 판단에 사용하지 않는다. Docker volume의 PostgreSQL을 durable 작업 상태와 catalog의 단일 기준으로 사용한다. job claim은 PostgreSQL transaction과 row locking 또는 동등한 원자적 방식으로 구현하며, file 존재만으로 성공을 판정하지 않는다.

최소 상태 필드는 다음과 같다.

| 범주 | 필드 |
|---|---|
| 식별 | run_id, job_id, issuer, doc_version_id, source snapshot, source PDF hash |
| stage | stage name, processor version, config hash, model/provider ID |
| lifecycle | queued, running, retry_wait, succeeded, dead_letter, cancelled |
| claim | lease owner, lease acquired/expires, heartbeat |
| retry | attempt, max attempts, next attempt, error class, last error code |
| artifact | output URI, output hash, byte/char/page count, validation status |
| generation | target generation ID, publish eligibility |
| 시간 | discovered, queued, started, heartbeat, finished, updated timestamps |

민감한 OCR 원문, API key, OAuth token과 전체 provider response를 상태 row에 넣지 않는다. 오류 메시지는 secret과 문서 본문을 redaction한 뒤 저장한다.

### 4.2 상태 전이

정상 문서는 discovery에서 download, OCR, structure, embedding, validation 순으로 진행한다. 각 stage에는 queued/running/retry_wait/succeeded/dead_letter 상태가 있고, 전체 문서 완료는 요구 stage가 모두 succeeded이고 artifact 검증을 통과했을 때만 성립한다.

- 성공 artifact가 존재해도 입력 hash 또는 processor version이 달라지면 재사용하지 않는다.
- 같은 idempotency key의 succeeded job은 중복 실행하지 않는다.
- 상태 전이는 DB constraint 또는 service layer transaction으로 강제하고 감사 event를 남긴다.
- completed 상태를 파일 존재만으로 복구하지 않는다. checksum과 metadata 검증이 필요하다.

### 4.3 Atomic claim과 lease

1. 다음 queued job 선택과 running 전환을 하나의 transaction으로 처리한다.
2. claim에는 worker ID와 만료시각을 기록한다.
3. worker는 처리 중 heartbeat를 갱신한다.
4. lease 만료 후에만 다른 worker가 job을 회수할 수 있다.
5. 원 worker의 늦은 결과는 lease token 또는 fencing version이 다르면 commit하지 않는다.
6. 긴 OCR 한 건의 정상 처리시간보다 lease가 짧지 않게 하되 무한 lease는 허용하지 않는다.

레거시처럼 SELECT와 UPDATE를 나누어 수행하면 같은 job이 두 worker에 배정될 수 있으므로 재사용하지 않는다.

### 4.4 Checkpoint와 중단 후 재개

- stage 출력은 temporary 경로에 쓰고 검증 후 immutable 최종 경로로 atomic rename한다.
- artifact hash와 성공 event를 같은 논리적 commit 단위로 연결한다.
- process 재시작 시 queued, retry due, expired lease만 회수한다.
- 성공한 PDF/OCR stage는 입력 hash와 processor version이 같으면 다시 실행하지 않는다.
- OCR page/chunk 단위 checkpoint를 지원하되 최종 문서 성공은 모든 페이지가 검증된 후 기록한다.
- SIGTERM을 받으면 새 claim을 멈추고 진행 중 작업을 checkpoint하거나 lease가 안전하게 만료되도록 종료한다.
- 중단된 동일 durable run ID의 상태를 조회해 미완료 issuer부터 idempotent하게 재개하고,
  기존 job·attempt 이력과 generation 연결을 유지한다.

systemd one-shot이 run 생성 후 중단되면 새 daily run을 시작하지 않는다. owner/admin container에서
다음 순서로 PostgreSQL의 기존 run ID를 발견·점검·재개한다.

```bash
cardrag run list --state running
cardrag run status RUN_ID
cardrag run finalize RUN_ID
```

`finalize`는 동일 run ID와 issuer별 idempotency key를 유지해 누락 issuer부터 재개하고 모든 terminal
상태와 품질 gate를 확인한 뒤에만 게시한다. 둘 이상의 예상하지 못한 running daily run이 보이면
임의로 새 실행이나 게시를 진행하지 않고 scheduler lease와 journal을 먼저 조사한다.

container 재생성 후 resume test는 필수 인수 항목이다.

## 5. Retry와 dead-letter

### 5.1 오류 분류

| 오류 유형 | 기본 방향 | 예 |
|---|---|---|
| transient external | 지수 backoff와 jitter 후 재시도 | timeout, 429, 일시적 5xx |
| infrastructure | 운영 경보 후 제한된 pause/retry | volume full, DNS, provider outage |
| authentication | 즉시 pause, 운영자 조치 | OAuth 만료, OpenRouter 401 |
| invalid input | 자동 반복 금지, dead-letter | PDF가 아님, hash 불일치, unsafe path |
| incomplete quality | 설정된 횟수만 재처리 후 dead-letter | 페이지 누락, OCR 불완전 |
| deterministic transform | processor 수정 전 dead-letter | schema 위반, parser 재현 오류 |
| publish validation | candidate 게시 금지 | count/hash/coverage/검색 품질 gate 실패 |

레거시의 download 2회, OCR 3회, transient 5회 budget은 관찰 가능한 참고값일 뿐 신규 기본값으로 자동 승계하지 않는다. provider 비용, 오류 유형과 복구 가능성에 따라 stage별로 결정한다.

### 5.2 재시도 원칙

- 같은 설정을 무한 반복하지 않는다.
- provider의 Retry-After가 있으면 존중한다.
- 재시도마다 attempt, 지연, model/config, 요약 오류를 기록한다.
- 인증·quota 전체 장애에는 document별 재시도 폭주 대신 circuit breaker를 둔다.
- 부분 artifact는 final 경로에 남기지 않거나 명시적 incomplete 상태로 격리한다.
- 수동 재처리는 새 processor/config version 또는 명시적 승인 사유와 연결한다.

### 5.3 Dead-letter와 redrive

dead-letter에는 원본 document ID, 실패 stage, attempt history, 입력·부분 출력 hash, 마지막 검증 결과, 운영자 조치가 저장되어야 한다. 원문이나 secret을 그대로 복제하지 않는다.

redrive 시 기존 row를 succeeded로 덮어 쓰지 않는다. 새 attempt/event를 만들고 이전 dead-letter와 연결한다. terminal 해제와 재처리 권한은 일반 MCP caller에게 주지 않는다.

## 6. 일일 증분 처리

### 6.1 Schedule과 동시 실행

- 카드사별 실행창과 endpoint rate limit을 정한다.
- issuer별로 한 개의 active discovery/run만 허용하거나 분산 lock을 사용한다.
- scheduler 이중 실행에도 같은 snapshot과 문서가 중복 queue되지 않게 한다.
- 수동 catch-up run과 정기 run이 충돌하지 않도록 priority와 lock 범위를 정의한다.
- 신규 카드사 adapter는 독립적으로 실패하고 다른 issuer run을 막지 않아야 한다.

### 6.2 변경 판정

증분 key는 최소 issuer, product code, document type, effective date, numeric version, source identifier와 PDF hash를 사용한다.

- 새 key 또는 새 PDF hash는 신규/변경 문서로 처리한다.
- filename과 filesize만 같은 경우를 변경 없음으로 단정하지 않는다.
- discovery에서 사라진 문서는 즉시 삭제하지 않고 tombstone 후보와 관찰시각을 기록한다.
- 동일 PDF hash는 OCR 재사용 후보가 될 수 있지만 metadata/version record는 별도로 유지한다.
- OCR·structure·embedding processor version이 바뀌면 source PDF가 같아도 영향 stage를 재처리한다.

### 6.3 일일 run 결과

run report에는 run ID, issuer, source snapshot, 발견·신규·변경·변경없음·성공·retry·dead-letter 수, 시작·종료시각과 candidate generation ID를 포함한다. no-change run은 성공 이력을 남기되 불필요한 전체 DB rebuild와 generation 게시를 하지 않는다.

일일 처리 중에는 온라인 MCP가 이전 generation을 사용한다. candidate가 모든 gate를 통과한 뒤에만 한 번의 publish로 신규 문서를 노출한다.

## 7. Immutable generation 게시와 rollback

### 7.1 Generation manifest

각 generation은 최소 다음을 고정한다.

- generation ID와 생성시각
- source snapshot ID와 포함 document ID/hash 집합
- catalog·structured·lexical·vector schema version
- chunk/structure processing version
- embedding provider/model/dimension과 config hash
- 파일별 checksum과 row/count summary
- known exception과 제외 문서
- 품질 평가 결과와 승인 정보
- application compatibility 범위

generation 디렉터리는 게시 후 변경하지 않는다. hotfix도 새 generation으로 만든다.

### 7.2 게시 gate

publisher는 다음을 순서대로 검증한다.

1. manifest와 실제 파일의 checksum·size 일치
2. schema migration/version과 application compatibility
3. PostgreSQL schema·FTS와 pgvector extension/HNSW index integrity
4. 최신 문서의 current text hash+model+dimension embedding·색인 coverage 100%
5. issuer/document/version count reconciliation
6. stable evidence ID와 source span 역추적
7. retrieval benchmark, no-result와 원문 citation 표본
8. candidate를 read-only mount한 MCP smoke test
9. READY marker와 이전 rollback generation 존재

하나라도 실패하면 current pointer를 바꾸지 않는다. 최신 문서의 OCR·구조·색인 누락 또는 실패는 예외 없이 게시를 차단한다. 과거 이력 실패는 quarantine과 보고서에 남기고 최신 문서 coverage가 100%일 때만 게시를 허용한다.

### 7.3 Atomic publish

- candidate를 build 경로에서 완성하고 검증한다.
- 같은 filesystem의 immutable generations 경로로 seal한다.
- `current.json` pointer를 temporary file·fsync 후 atomic replace하고 PostgreSQL
  `active_generation`과 대사한다.
- MCP instance는 요청 경계에서 새 generation을 열고 진행 중 요청은 이전 handle로 완료한다.
- 모든 replica가 적용한 generation ID를 보고할 때까지 이전 generation을 유지한다.

publisher는 PostgreSQL publication을 먼저 commit한 뒤 filesystem pointer를 교체하며, 후속 단계가
실패하면 이전 DB/file 권위를 복원하는 compensation을 실행한다. readiness는 두 권위와 manifest
checksum을 매번 대사한다. 요청은 시작 시 generation handle을 pin하므로 교체 중 old/new를 섞지
않는다.

### 7.4 Rollback

- 데이터 회귀는 current pointer를 이전 READY generation으로 되돌린다.
- application 회귀는 이전 image digest를 배포한다.
- schema 호환성이 깨지면 image와 generation의 검증된 조합으로 함께 rollback한다.
- mutable job state는 data generation과 함께 과거로 되돌리지 않는다.
- rollback 사유, 영향, generation/image 전후 값과 실행자를 감사 로그에 남긴다.
- 실패 generation은 조사 전 삭제하지 않고 서비스 대상에서만 제외한다.

성공한 검색 generation은 최근 3개를 보존한다. 실패 candidate는 조사 가능하도록 7일 보존한 뒤 정리하고, 수동 pin한 generation은 명시적 unpin 전까지 보존한다.

## 8. 로그, metric과 경보

### 8.1 구조화 로그

application log는 JSON 등 machine-readable 형식을 사용하고 최소 다음 context를 포함한다.

- timestamp, level, service, environment, event name
- run ID, job ID, worker ID, issuer, doc version ID
- stage, attempt, lease token의 비민감 식별값
- generation ID와 application version/image digest
- provider/model ID, duration, 입력 page 수와 출력 문자 수
- 결과 상태, 안정적인 error code, retry/dead-letter 여부

API key, OAuth access/refresh token, Authorization header, 전체 이메일·OCR 본문, query 원문과 signed URL은 기록하지 않는다. 접근·인증·원본 PDF 감사 metadata는 90일, 비식별 집계 metric은 1년 보존한다. 문서 식별자도 개인정보 가능성을 검토하고 필요한 경우 hash 또는 제한된 형태로 남긴다.

로그에는 stdout/stderr를 무분별하게 합치지 않는다. 외부 CLI output은 secret redaction 후 구조화 event로 감싼다. stack trace와 provider response는 접근 제한된 오류 저장소에 보관하고 일반 로그에는 요약 code만 남긴다.

### 8.2 핵심 metric

| 영역 | metric 예시 |
|---|---|
| queue | 상태별 depth, oldest age, lease 만료·회수, dead-letter 수 |
| 처리량 | issuer/stage별 시작·성공·실패 건수, 문서·페이지/시간 |
| 지연 | stage별 duration histogram, end-to-end document latency |
| retry | error class별 attempt, backoff, circuit breaker 상태 |
| OCR | page completeness, 문자 수, hash mismatch, 품질 gate 실패 |
| embedding | request/error/rate-limit, latency, item 수, dimension, 예상 비용 |
| generation | build duration, coverage, publish/rollback, replica 적용상태 |
| MCP | request 수, latency, error, timeout, no-result, active generation |
| storage | volume 사용량·inode, generation 수, page PNG cache age·size |
| 인증 | Codex/OpenRouter auth failure, token 만료 임박 여부(비밀값 제외) |

provider 비용과 token/page 사용량은 provider 정책이 허용하는 범위에서 집계하되 요청 원문과 결합하지 않는다.

온라인 성능은 품질 우선으로 운영한다. 개발 기준선은 동시 요청 5개, 요청 timeout 45초,
검색 P95 경고 30초다. 합성 admission probe에서 제한 동작을 검증했지만 이는 실제 corpus SLO를
주장하지 않는다. 운영 BULK corpus와 실제 질의로 latency·timeout·근거 품질·resource 사용량을
함께 측정해 후속 ADR로 보정한다.

### 8.3 경보

최소 경보 대상은 다음과 같다.

- queue oldest age가 일일 주기를 넘음
- retry 또는 dead-letter가 평시 기준보다 급증
- 동일 issuer discovery가 연속 실패
- lease 회수 반복 또는 running job 정체
- OCR page 누락·hash mismatch 발생
- 최신 문서 embedding·색인 coverage가 100% 미만인 generation 게시 시도
- current generation 로드 실패 또는 replica 간 generation 불일치
- OpenRouter/Codex 인증 실패와 quota 고갈
- volume 임계치 초과와 page PNG cache 정리 실패
- MCP error/latency/no-result 비율 급증

개발 alert는 15분 window의 검색 P95 30초와 error/degraded 5% 등
`deploy/monitoring/cardrag-alerts.yml`의 초기값을 사용한다. paging route와 threshold는 실제 운영
baseline 이후 조정한다.

## 9. Docker 운영 설계

### 9.1 Image 경계

권장 image 역할은 다음과 같다.

| image/target | 포함 기능 | 포함하지 않을 기능 |
|---|---|---|
| cardrag-mcp | MCP transport, read-only 검색, health/readiness | 수집기, Codex OAuth/login, OCR, publisher write 권한 |
| cardrag-worker | 카드사 adapter, PDF 검증, OCR, 구조화, embedding | 외부 공개 MCP endpoint |
| cardrag-admin 또는 동일 worker의 제한 entrypoint | 운영 CLI, scheduled job, migration, generation 검증·게시 | 상시 공개 service, public admin API와 web UI |
| keycloak | 단일 tenant OAuth/OIDC authorization server | 애플리케이션 운영 명령과 corpus 접근 |

하나의 source repository에서 multi-stage target으로 만들 수 있지만 runtime package와 Linux capability, user, egress, volume mount는 역할별로 다르게 한다. v1 운영 관리면은 운영 CLI와 명시적 scheduled job으로 한정하며 public admin API와 web UI는 만들지 않는다. scheduler는 worker container 안의 무한 loop보다 host scheduler나 명시적 scheduled job으로 분리한다.

### 9.2 External volume

| volume | MCP | worker/publisher | 용도 |
|---|---|---|---|
| published generations/current pointer | read-only | publisher만 write | 온라인 검색 snapshot |
| PDF/OCR object store | 게시 승인 원본의 제한 view만 read-only | read-write | 불변 source artifact와 명시적 PDF·페이지 조회 |
| build workspace | 미마운트 | read-write | candidate generation과 임시 파일 |
| PostgreSQL data | catalog 조회에 필요한 최소 권한 | scheduler/worker read-write | durable catalog, queue, lease, run event |
| quarantine | 미마운트 | 제한 read-write | 실패 artifact와 조사자료 |
| Codex auth state | 미마운트 | OCR worker만 제한 read-write | container 재생성 후 OAuth 상태 유지 |
| temporary scratch | 미마운트 | ephemeral | render·CLI 임시 파일, 재생성 가능 |
| page PNG cache | read-write | 미마운트 | 원본 PDF 요청 렌더 결과, TTL 7일 후 제거 |

PDF, OCR과 generation은 외부 불변 file volume에 두고 작업상태·catalog는 PostgreSQL에 둔다. index, PostgreSQL data와 OAuth state에는 서로 다른 접근정책을 적용하며 하나의 data root를 모든 container에 read-write로 mount하지 않는다. page PNG cache는 불변 원본과 분리하고 7일 TTL 정리를 적용한다. backup volume은 v1에 만들지 않는다.

### 9.3 Image와 runtime hardening

- 버전과 digest가 고정된 최소 base image를 사용한다.
- Python·Codex CLI와 system dependency 버전을 lock하고 SBOM을 생성한다.
- non-root user, read-only root filesystem, 제한된 writable mount를 기본으로 한다.
- 필요 없는 Linux capability를 제거하고 privileged mode를 사용하지 않는다.
- worker의 OCR sandbox에는 필요한 PDF/임시 경로만 mount한다.
- memory/CPU/PID와 임시 disk 한도를 정하고 graceful termination 시간을 둔다.
- init/reaping, health check, timezone과 clock sync를 명시한다.
- Git, source PDF/OCR, build cache, local env, OAuth/token 파일은 build context에서 제외한다.
- image 취약점 scan과 dependency license 검토를 release gate에 포함한다. locked runtime
  inventory는 CI에서 생성한다. PDF 렌더러는 permissive license의 `pypdfium2 5.12.1`과
  PDFium으로 고정하며, 선택한 Linux x86-64 wheel에 포함된 license payload 전체를
  `/usr/share/licenses/cardrag/pypdfium2`에 보존한다. policy가 고정한 wheel hash, license
  metadata, 고지 파일 목록·내용 hash 또는 `THIRD_PARTY_NOTICES.md` 필수 문구가 달라지면
  build와 release를 fail closed한다. 이 검사는 법률 자문을 대신하지 않으며 dependency
  갱신 때 실제 binary와 의무를 다시 검토한다.
- Docker Hub public image는 누구나 layer와 패키징된 애플리케이션 코드·dependency metadata를 검사할 수 있음을 전제로 한다. private GitHub의 비공개성만으로 image 내부 코드를 숨길 수 있다고 가정하지 않는다.

레거시 OCR의 **danger-full-access** 설정은 그대로 계승하지 않는다. Codex CLI가 필요로 하는 최소 filesystem·network 권한을 exact version으로 검증하고 worker 격리 경계를 정의해야 한다.

### 9.4 Network 경계와 hosting 인계

- MCP application은 container 내부에서 `0.0.0.0:8000`을 listen하고 Compose는 host의 `127.0.0.1:8000`에만 publish한다.
- public hostname, TLS 인증서, 443 진입점과 Nginx Proxy Manager 연결은 개발 완료 후 운영자가 처리하는 별도 hosting 과제다. 이 project의 Compose에는 reverse proxy container나 Nginx Proxy Manager network 설정을 포함하지 않는다.
- MCP service의 개발 인계점은 host `127.0.0.1:8000`의 HTTP MCP endpoint와 health endpoint다.
- MCP가 query embedding에 OpenRouter를 사용한다면 해당 egress만 허용하고 timeout/circuit breaker를 둔다.
- worker는 승인된 카드사 domain, Codex 인증·실행 endpoint, OpenRouter만 egress allowlist 후보로 둔다.
- discovery가 돌려준 URL은 host·redirect·size·PDF 검증을 거친다.
- publisher는 외부 공개 port를 갖지 않는다.
- container network와 log 접근 권한을 운영자 role로 제한한다.
- token은 `Authorization` header에서만 받고 URL query·path, access log와 error log에 남기지 않는다.
- 원본 PDF 전달은 게시 catalog의 document ID를 통해서만 허용하고 임의 URL·host path·object key를 외부 입력으로 사용하지 않는다.
- 승인된 `source_pdf` 사용자의 원본 PDF 응답은 100 MB로 제한하고 HTTP Range와 전송 취소를 지원하며 접근 감사 metadata를 90일 보존한다.

HTTP+OAuth token 접속과 운영 HTTPS 사용은 확정이다. `search`·`source_pdf` scope, 자동 access token 갱신, refresh token rotation과 90일 비활성 만료를 적용한다. authorization server는 self-hosted Keycloak 단일 tenant로 확정한다. TLS termination과 Nginx Proxy Manager 연결은 별도 hosting 과제다.

## 10. 인증과 secret

### 10.1 MCP OAuth authorization server의 역할

authorization server는 사용자를 로그인시키거나 client를 식별하고, access token과 refresh token을 발급·갱신·회전·폐기하는 별도 보안 구성요소다. MCP server는 이 token을 직접 만들어 장기 보관하는 대신 서명, 발급자, 대상, 만료와 `search`·`source_pdf` scope를 검증한다. 따라서 한번 승인한 뒤 별도 조작 없이 계속 사용하는 자동 갱신과 refresh token rotation을 적용하려면 이를 지원하도록 설정된 authorization server와 호환 MCP client가 필요하다. refresh token 폐기·분실, 90일 비활성, 보안사고 또는 client 미지원 때만 재인증한다.

기존 OAuth/OIDC provider가 없으므로 v1 authorization server는 self-hosted Keycloak으로 확정한다. 같은 Docker Compose의 별도 service로 구성하고 PostgreSQL server는 공유하되 애플리케이션과 별도 database·user를 사용한다. realm은 `cardrag` 단일 tenant다. 사용자 self-registration과 dynamic client registration은 끄고 승인 client만 수동 사전등록한다. 사람용 client는 Authorization Code+PKCE, service client는 Client Credentials를 사용한다. 모든 승인 사용자가 같은 카드 공시 corpus를 조회하며 `search`·`source_pdf` scope만 분리한다. 운영 권한은 외부 MCP token에 싣지 않고 local CLI 실행 권한으로 유지한다. 초기 Keycloak admin credential은 Docker secret으로 1회 bootstrap하고 즉시 회전·제거하며 admin console을 공개 MCP endpoint와 함께 노출하지 않는다.

### 10.2 Codex CLI OAuth

worker image에는 checksum-pinned Codex CLI 0.147.0과 전용 `CODEX_HOME` volume을 설치했다.
`ocr` permission profile, tool-less prompt input과 bubblewrap canary가 rendered input 읽기만 허용하고
secret/outside/write/socket을 거부함을 자동 검증했다. `codex login --device-auth` command와 auth
volume 경계도 마련했다.

실제 계정 승인은 별도 사용자 기기가 필요하므로 다음 항목만 실환경 검증 대기다.

1. 실제 device URL·user code로 운영 계정을 승인한다.
2. token을 출력하지 않은 채 `codex login status`와 무해한 비대화형 실행을 확인한다.
3. worker container 재생성 후 auth 상태 유지와 장시간 OCR 중 token 갱신을 확인한다.
4. 실패 시 exact version, TTY, `CODEX_HOME` ownership, clock과 redacted exit code를 진단한다.

device login을 Docker 로그로 제공해야 한다면 전용 1회성 auth job을 사용한다.

- 로그인 URL, 짧은 수명의 user code와 상태만 operator가 볼 수 있게 한다.
- access token, refresh token, session payload는 절대 stdout/stderr에 출력하지 않는다.
- device user code도 유효시간 동안 인증 수단이므로 log viewer RBAC와 짧은 retention을 적용한다.
- 중앙 로그 수집기로 전달할지 여부를 보안 검토한다. 전달하지 않는 전용 log channel이 더 적절할 수 있다.
- login 완료 후 auth job을 종료하고 OCR worker에서 최소 무해 요청으로 인증상태를 점검한다.
- auth volume 권한은 OCR worker UID만 읽고 쓸 수 있게 하며 MCP service와 공유하지 않는다.
- 로그아웃·token 폐기·계정 변경과 사고 시 회수 절차를 runbook에 둔다.

CLI가 요구한 headless/device-code 동작을 제공하지 않는다면 log scraping이나 token 복사로 우회한다고 확정하지 않는다. 공식 지원 방식 또는 별도 안전한 bootstrap 절차를 선택해야 한다.

### 10.3 OpenRouter

- **OPENROUTER_API_KEY**는 environment 또는 orchestrator secret file로 주입한다.
- repository의 env 파일, image layer, Compose 평문, job DB와 로그에 key를 넣지 않는다.
- worker와 MCP가 모두 key를 필요로 하면 서비스별 key·quota 분리를 우선한다.
- key rotation과 폐기 절차를 마련하고 provider 401/429를 구분한다.
- model ID, dimension, provider endpoint와 timeout은 secret이 아닌 versioned 설정으로 관리한다.
- 실제 사용 model/config hash를 OCR 또는 embedding provenance와 generation manifest에 기록한다.
- 외부 전송 데이터 범위와 provider 보존정책을 보안·법무 관점에서 승인받는다.

### 10.4 MCP OAuth token

HTTP MCP 인증은 [MCP 2026-07-28 Authorization specification](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)의 OAuth 2.1 discovery, Bearer token, audience와 refresh 지침을 따른다.

- client에는 HTTPS endpoint URL을 설정하고 최초 1회 authorization을 완료한다.
- access token은 짧게 유지하고 `Authorization: Bearer` header로만 보낸다. URL query·path, image, Git, Compose 평문과 일반 log에 넣지 않는다.
- 일반 검색과 페이지 OCR text에는 `search`, 원본 PDF 전체 파일과 페이지 PNG에는 `source_pdf` scope를 요구한다.
- client는 access token 만료 전에 refresh하고, authorization server는 refresh token을 사용할 때마다 새 refresh token으로 회전한다.
- 90일은 token을 90일마다 수동 교체한다는 뜻이 아니라 **비활성 만료**다. 정상적으로 계속 사용하는 client는 자동 refresh/rotation으로 수동 조작 없이 연결을 유지한다.
- refresh token이 폐기·분실됐거나 보안사고가 발생한 경우, 90일간 사용하지 않은 경우, 또는 client가 refresh를 지원하지 않는 경우에는 재인증이 필요하다. 임의의 MCP client가 자동 갱신을 지원한다고 가정하지 않고 호환성 시험을 한다.
- 비대화형 service client는 지원되는 경우 client credentials로 짧은 수명의 access token을 만료 전에 자동 재발급한다.
- authorization server는 token audience를 MCP resource에 고정하고 즉시 revoke, refresh 재사용 탐지와 secure token storage를 제공해야 한다.
- 원본 PDF·페이지 조회를 포함한 모든 요청에 호출자, 허용 scope, document ID, 결과 상태와 비민감 request ID를 감사 event로 남긴다.

### 10.5 그 밖의 secret

Docker Hub credential은 build/push host 또는 CI secret store에만 둔다. runtime container에 mount하지 않는다. TLS private key, remote storage credential과 monitoring token도 용도별로 분리하고 secret scan을 release gate에 포함한다.

## 11. Health와 readiness

| probe | 목적 | 성공 조건 |
|---|---|---|
| liveness | process가 응답 가능한지 | event loop/transport가 제한시간 내 응답 |
| readiness | 안전하게 query를 받을 수 있는지 | current pointer, READY, checksum/schema 호환, index open 성공 |
| dependency status | 외부 의존 상태 | OpenRouter 등 선택적 의존의 정상·degraded 구분 |
| worker status | 처리 가능 여부 | state/volume/auth/provider와 lease 기능 정상 |

MCP readiness는 current generation ID, schema, embedding model/dimension, document count와 FTS/vector open 결과를 내부적으로 검사한다. 민감 path나 secret은 응답하지 않는다. 외부 embedding 또는 vector 장애 시 `allow_degraded=true` 요청만 lexical-only 결과와 `degraded` 상태를 받으며, flag가 없거나 false인 요청은 실패한다.

## 12. Docker Hub 배포 계획

public repository [`ymtop59/mcp-card-prd-detail`](https://hub.docker.com/r/ymtop59/mcp-card-prd-detail)은 2026-08-12 생성·공개 조회를 확인했다. v1 image platform은 `linux/amd64`다. MCP/worker/admin
local image build, SBOM, content·sandbox·HIGH/CRITICAL 취약점 gate는 개발환경에서 통과했다.
registry push와 promotion은 의도적으로 수행하지 않았으며 아래 release 절차를 운영 인계한다.

1. test, dependency license inventory, secret scan, SBOM, vulnerability scan을 통과한 image를
   재현 가능하게 build한다. `pypdfium2 5.12.1` Linux x86-64 wheel의 PDFium 및 제3자
   `BUILD_LICENSES` 전체와 프로젝트 `THIRD_PARTY_NOTICES.md`가 image에 포함되는지 exact
   hash로 검사한다. psycopg 계열 LGPL 및 certifi의 MPL-2.0 의무도 inventory의 exact
   version/metadata와 함께 검토하고 notice·재링크 가능성 등 적용 의무를 release 기록에
   남긴다.
2. MCP/worker/admin 역할별 release version과 Git SHA를 포함한 immutable tag를 붙이고 각 image digest를 기록한다.
3. GitHub Actions OIDC 기반 keyless Cosign으로 image digest를 서명하고 release manifest에 서명 identity와 transparency-log reference를 기록한다. long-lived signing key는 두지 않는다. private GitHub repository·workflow URI가 공개 log에 나타날 수 있음을 전제로 한다.
4. 일반 `main` push와 tag push는 build·test 또는 ref 생성만 하며 공개 registry에는 push하지 않는다. `vX.Y.Z` tag를 대상으로 `PUBLISH-vX.Y.Z` 확인 문자열을 입력한 수동 release workflow를 통과한 candidate만 `ymtop59/mcp-card-prd-detail`에 push한다. 이 시점부터 image에 패키징된 code와 metadata는 공개된 것으로 취급한다.
   일부 역할 push 뒤 실행이 중단되면 동일 Git SHA·동일 digest로 확인된 기존 tag만 재사용하고
   누락 alias를 복구한다. 다른 revision의 기존 tag는 절대 덮어쓰지 않는다. dependency-license
   release report는 image manifest와 최종 release manifest artifact에 hash로 결속한다. Docker
   Hub의 SemVer 역할 tag와 SHA alias는 immutable regex로 설정하고 workflow가 이 외부 설정을
   push 전에 검증한다. Cosign digest signature/attestation tag는 서명 재시도를 위해 제외한다.
5. 깨끗한 host에서 digest로 pull하여 data를 포함하지 않았는지, non-root/read-only 실행과 smoke test를 확인한다.
6. 승인된 역할별 digest만 운영에 사용하고 deployment 기록에 세 image digest와 호환 generation을 남긴다.
7. 실패 시 이전 digest로 rollback한다.

**latest** tag만으로 배포 상태를 식별하지 않는다. repository는 public
**ymtop59/mcp-card-prd-detail**, 서명은 GitHub Actions OIDC keyless Cosign으로 확정됐으며 GitHub
repository는 private로 유지한다. v1은 `linux/amd64`만 제공하고 ARM64는 실제 필요가 생기면
후속 지원한다. local build 완료와 public push 미수행을 구분해 보고한다.

## 13. 후속 개선 과제: Backup과 restore

backup·restore는 현재 v1 개발·인수 범위에 포함하지 않는다. 아래 내용은 후속 개선 과제의 설계 참고이며 현재 release 차단조건, 구현 완료조건 또는 운영 보장으로 사용하지 않는다. RPO·RTO, 대상 저장소, 주기와 암호화 방식은 후속 과제를 시작할 때 다시 결정한다.

### 13.1 Backup 대상

| 대상 | 중요도 | 방법 원칙 |
|---|---|---|
| PDF/OCR content objects | 최고 | immutable, checksum 포함, 암호화된 별도 storage |
| canonical catalog/source manifest | 최고 | object와 같은 snapshot ID로 보존 |
| published generations | 높음 | 재생성 가능하지만 비용이 크므로 manifest와 함께 보존 |
| PostgreSQL catalog·job/migration state | 최고 | PostgreSQL consistent backup과 WAL 정책 사용 |
| quarantine와 audit event | 정책에 따름 | 민감도·조사 필요기간에 맞춰 암호화·보존 |
| application image digest·설정 | 높음 | registry와 release manifest에 보존 |
| API key/OAuth token | 일반 data backup에서 제외 | secret manager 또는 별도 암호화·회수 정책 |
| build scratch/render temp | 낮음 | 재생성 가능, 기본 backup 제외 |

PostgreSQL data directory를 실행 중 일반 file copy로 backup하지 않는다. PostgreSQL의 일관된 backup과 WAL 정책을 사용하고 복원 시험을 수행한다. immutable generation은 checksum manifest와 함께 copy하면 일관성을 검증하기 쉽다.

### 13.2 RPO·RTO와 보존

후속 구현 전 다음 사항을 새로 결정한다.

- RPO·RTO와 backup·restore 시험 주기
- raw PDF/OCR과 과거 generation 보존기간
- 일일·주간 backup의 실제 target과 별도 failure domain 구성
- 암호화 key 관리, 개인정보·원문 삭제 요청 처리

후속 backup을 도입할 때는 최소한 current와 직전 검증 generation, 해당 source catalog, job state의 같은 시점 snapshot을 함께 복구할 수 있어야 한다.

### 13.3 Restore 절차

1. 원인을 확인하고 writer·publisher를 중지한다.
2. 기존 운영 경로를 덮어 쓰지 않고 격리된 새 restore 경로에 복원한다.
3. checksum, manifest, catalog 참조, DB/index integrity를 검증한다.
4. candidate를 read-only로 열어 MCP와 대표 검색 smoke test를 수행한다.
5. durable state의 lease를 만료·회수하고 이미 성공한 artifact와 중복 실행 여부를 점검한다.
6. 승인 후 current pointer 또는 deployment를 교체한다.
7. 복구시각, RPO/RTO 실제값, 손실·재처리 범위와 후속조치를 기록한다.

후속 과제에서도 backup 성공 로그만으로 복구 가능성을 인정하지 않고 정기 restore drill과 다른 host 검증을 인수 기준으로 삼는다.

## 14. 운영 runbook 최소 목록

- 초기 bulk run 시작·pause·resume·cancel
- issuer별 일일 run 수동 재실행
- expired lease 회수와 stuck job 조사
- dead-letter 조회·승인·redrive
- Codex OAuth bootstrap·갱신·폐기
- OpenRouter key rotation과 quota/rate-limit 대응
- volume 부족과 artifact cleanup
- candidate generation 검증·publish·rollback
- MCP instance의 generation reload 실패
- Docker Hub candidate push·검증·promotion·rollback
- secret 또는 원문 로그 노출 사고 대응

각 runbook에는 승인 역할, 영향범위, 사전 확인, 성공조건, rollback과 남겨야 할 증거를 포함한다.

## 15. 배포 전 검증 matrix

| 영역 | 필수 검증 |
|---|---|
| restart | worker/container 강제 종료 후 중복 없이 lease 회수·resume |
| idempotency | 같은 snapshot/run 재제출 시 성공 stage 중복 실행 없음 |
| retry | 429/5xx/auth/invalid PDF/OCR incomplete가 올바른 상태로 전이 |
| generation | 부분 build 미노출, atomic publish와 이전 generation rollback |
| data | checksum, schema, current embedding coverage, citation 역추적 |
| MCP | HTTPS+OAuth, 자동 refresh/rotation, scope, read-only mount, 동시 요청 5개, timeout, PDF·페이지 권한, degraded opt-in |
| Docker | non-root, read-only root, resource limit, health probe |
| auth | MCP OAuth 자동 refresh·revoke·비활성 만료, Codex headless login, OpenRouter secret 비노출 |
| logs | token·본문 redaction, correlation ID, retention/RBAC |
| registry | public `ymtop59/mcp-card-prd-detail`, `linux/amd64`, `vX.Y.Z`+manual approval만 push, version+Git SHA tag, keyless Cosign 서명, digest pull/run, data·secret 미포함, 공개 code 검토 |

## 16. 개발 중 결정 결과와 외부 검증

PostgreSQL migration·scheduler, durable lease/retry, generation ID/publish protocol,
PostgreSQL FTS+pgvector hybrid, 동시 요청 5·timeout 45초·초기 검색 P95 30초,
Codex CLI 0.147.0 sandbox, Keycloak, 역할별 `linux/amd64` image와 release promotion은 ADR과
자동 검증으로 확정했다.

운영에서만 정할 값은 실제 provider quota·비용, 수일 BULK concurrency와 ETA, 전체 corpus의
host sizing·SLO, Codex device 계정의 장기 token 갱신, public TLS/client와 registry 승인이다.
이 항목들은 [실환경 검증 및 운영 인계](REAL_ENV_HANDOFF.md)에 절차와 성공 조건을 기록했다.

## 17. 이 문서 작성 시점의 완료 상태

| 항목 | 상태 |
|---|---|
| 초기·증분 운영 원칙 문서화 | 문서화 완료 |
| durable state·lease·retry·dead-letter 계약 문서화 | 문서화 완료 |
| immutable generation publish·rollback 방향 문서화 | 문서화 완료 |
| Docker volume·secret 방향 문서화 | 문서화 완료 |
| backup·restore | v1 범위 밖, 후속 개선 과제 |
| 신규 scheduler/worker/MCP 구현 | 구현 및 자동 검증 완료 |
| fixture 3-card BULK·재시작 | 개발환경 통합 검증 완료 |
| 초기 3~4일 실제 대량 처리 실행 | 실환경 검증 대기 |
| 일일 증분 run 구현 | 구현·fixture 검증 완료, 운영 timer 설치는 운영 인계 |
| Codex CLI 설치·sandbox 검증 | 0.147.0 설치·tool-less/bubblewrap canary 완료, 실제 device login은 실환경 검증 대기 |
| OpenRouter adapter·장애 검증 | mock 검증 완료, 실제 key·quota 호출은 실환경 검증 대기 |
| Dockerfile/Compose 작성·image build | 3-role `linux/amd64` local build·보안 검증 완료 |
| Docker Hub `ymtop59/mcp-card-prd-detail` 생성 | 완료, image 없음 |
| Docker Hub image push | `vX.Y.Z`+manual approval 전 의도적으로 미수행, 운영 인계 |

개발환경 상태는 [완료 체크리스트](08_COMPLETION_CHECKLIST.md)를 단일 기준으로 한다. 운영 인계
항목은 실제 generation ID, role image digest와 운영 로그가 생길 때 해당 인계 기록만 갱신한다.
