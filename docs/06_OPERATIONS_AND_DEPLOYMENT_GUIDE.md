# 운영 및 배포 가이드

## 1. 문서 상태와 범위

이 문서는 신규 CardRAG MCP 시스템의 초기 대량 처리, 일일 증분 처리, 장애 복구, 관측성, Docker 운영과 배포 절차가 갖춰야 할 조건을 정의한다.

- 작성일: 2026-08-12
- 현재 상태: 운영·배포 계획 문서
- 구현 상태: 미착수
- 수행하지 않은 작업: OCR·임베딩 실행, Codex/OpenRouter 인증, Docker build·login·push, container 기동, Docker Hub repository 생성·배포

문서에 나오는 container, image, volume, metric과 상태 필드는 목표 계약이다. 존재하거나 검증된 것처럼 해석하지 않는다. 레거시에는 Dockerfile, Compose, MCP server, health check, durable queue 또는 immutable generation 게시 기능이 없다.

## 2. 운영 경계

### 2.1 실행 단위

최초 운영 topology는 단일 Linux host의 Docker Compose다. online MCP와 offline worker는 별도 컨테이너로 실행하고 PostgreSQL, 외부 불변 file volume과 운영 job을 같은 Compose project에서 명시적으로 연결한다. 다중 node 전환은 BULK·부하·가용성 측정 후 검토한다.

| 실행 단위 | 주기 | 권한·network | storage | 장애 영향 |
|---|---|---|---|---|
| 온라인 MCP service | 상시 | HTTPS MCP endpoint와 query embedding에 필요한 최소 egress | 게시 generation과 승인 원본 PDF view read-only | 장애 시 조회 중단, ingestion에는 영향 없음 |
| ingestion worker | 초기 대량·일일 증분 | 카드사 endpoint, Codex CLI, OpenRouter 접근 | raw/OCR/build/state read-write | 온라인은 이전 generation으로 계속 서비스 |
| scheduler/controller | 일일 또는 운영자 실행 | job 제출 권한만 | durable state read-write | 새 작업 지연, 현재 MCP 조회는 유지 |
| generation publisher | candidate 검증 후 | current pointer 변경 권한 | generations와 publish metadata write | 실패 시 이전 generation 유지 |
| backup/restore job | 정책에 따른 주기 | backup target 접근 | snapshot read, backup write | 실행 중 일관성 확보 필요 |

온라인 MCP process에서 카드사 사이트 PDF 다운로드, OCR, DB drop/rebuild, OAuth login, Gmail 전송을 실행하지 않는다. 다만 `source_pdf` scope를 가진 사용자가 명시적으로 요청하면 게시 대상 document ID에 연결된 보존 원본 PDF 전체를 streaming file로 전달한다. 페이지 조회는 OCR text와 선택적 렌더 PNG를 사용하고 분할 PDF는 만들지 않는다. ingestion worker도 게시된 generation을 in-place 변경하지 않는다.

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
2. PDF·OCR·build·state·backup volume 용량과 inode 여유가 측정되어 있다.
3. source 이용조건, 보존기간과 외부 provider 전송 범위가 승인되어 있다.
4. Codex CLI exact version, 인증상태, OCR model/prompt, timeout이 기록되어 있다.
5. OpenRouter key, model ID, dimension과 retry/rate limit 설정이 주입되어 있다.
6. durable state store와 lease 회수가 실제 restart test를 통과했다.
7. 소수 PDF의 pilot이 원문 충실도와 artifact checksum gate를 통과했다.
8. 게시 중인 generation이 있다면 backup과 rollback pointer가 확인되어 있다.

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

우리카드와 KB국민카드를 우선 처리한다. 신한카드는 개인 신용·체크카드 상품안내장의 현재본과 과거 이력을 신규 BULK 시험 대상으로 포함하고 법인·선불카드는 1차 범위에서 제외한다. 신한카드 adapter는 레거시 재사용이 아니라 신규 구현이며, 카드사별 rate limit과 오류 격리가 확인되기 전에는 운영 주기 편입으로 간주하지 않는다.

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
- run을 재개해도 새 run ID와 원래 parent run ID를 남겨 이력을 잃지 않는다.

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
3. DB/index integrity와 Docker runtime의 SQLite FTS5 지원
4. current text hash+model+dimension embedding coverage
5. issuer/document/version count reconciliation
6. stable evidence ID와 source span 역추적
7. retrieval benchmark, no-result와 원문 citation 표본
8. candidate를 read-only mount한 MCP smoke test
9. READY marker와 이전 rollback generation 존재

하나라도 실패하면 current pointer를 바꾸지 않는다.

### 7.3 Atomic publish

- candidate를 build 경로에서 완성하고 검증한다.
- 같은 filesystem의 immutable generations 경로로 seal한다.
- current pointer를 temporary file 작성 후 atomic replace한다.
- MCP instance는 요청 경계에서 새 generation을 열고 진행 중 요청은 이전 handle로 완료한다.
- 모든 replica가 적용한 generation ID를 보고할 때까지 이전 generation을 유지한다.

shared volume에서 symlink 교체를 사용할지 pointer file과 control plane을 사용할지는 배포환경에 따라 **결정 필요**다. 어느 방식이든 부분 generation을 관찰할 수 없어야 한다.

### 7.4 Rollback

- 데이터 회귀는 current pointer를 이전 READY generation으로 되돌린다.
- application 회귀는 이전 image digest를 배포한다.
- schema 호환성이 깨지면 image와 generation의 검증된 조합으로 함께 rollback한다.
- mutable job state는 data generation과 함께 과거로 되돌리지 않는다.
- rollback 사유, 영향, generation/image 전후 값과 실행자를 감사 로그에 남긴다.
- 실패 generation은 조사 전 삭제하지 않고 서비스 대상에서만 제외한다.

검색 generation은 최소 3개를 보존한다. 3개를 초과하는 보존 기간은 storage 비용, 재처리시간과 복구 목표를 기준으로 **결정 필요**다.

## 8. 로그, metric과 경보

### 8.1 구조화 로그

application log는 JSON 등 machine-readable 형식을 사용하고 최소 다음 context를 포함한다.

- timestamp, level, service, environment, event name
- run ID, job ID, worker ID, issuer, doc version ID
- stage, attempt, lease token의 비민감 식별값
- generation ID와 application version/image digest
- provider/model ID, duration, 입력 page 수와 출력 문자 수
- 결과 상태, 안정적인 error code, retry/dead-letter 여부

API key, OAuth access/refresh token, Authorization header, 전체 이메일·OCR 본문, query 원문과 signed URL은 기록하지 않는다. 문서 식별자도 개인정보 가능성을 검토하고 필요한 경우 hash 또는 제한된 형태로 남긴다.

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
| storage | volume 사용량·inode, generation 수, backup age |
| 인증 | Codex/OpenRouter auth failure, token 만료 임박 여부(비밀값 제외) |

provider 비용과 token/page 사용량은 provider 정책이 허용하는 범위에서 집계하되 요청 원문과 결합하지 않는다.

온라인 성능은 품질 우선으로 운영한다. 초기 동시 요청 상한은 5개로 시작하고, 특정 P95 값을 구현 전에 약속하지 않는다. BULK corpus와 실제 질의로 latency 분포·timeout률·근거 품질·resource 사용량을 함께 측정한 뒤 SLO를 정한다. 다만 무제한 대기는 장애 격리를 해치므로 요청 timeout, cancellation과 서버측 작업 한도는 항상 유한해야 한다.

### 8.3 경보

최소 경보 대상은 다음과 같다.

- queue oldest age가 일일 주기를 넘음
- retry 또는 dead-letter가 평시 기준보다 급증
- 동일 issuer discovery가 연속 실패
- lease 회수 반복 또는 running job 정체
- OCR page 누락·hash mismatch 발생
- embedding coverage가 100% 미만인 generation 게시 시도
- current generation 로드 실패 또는 replica 간 generation 불일치
- OpenRouter/Codex 인증 실패와 quota 고갈
- volume 임계치 초과, backup 지연, restore 검증 실패
- MCP error/latency/no-result 비율 급증

구체 threshold와 paging 대상은 운영 SLO와 pilot 수치를 기준으로 **결정 필요**다.

## 9. Docker 운영 설계

### 9.1 Image 경계

권장 image 역할은 다음과 같다.

| image/target | 포함 기능 | 포함하지 않을 기능 |
|---|---|---|
| cardrag-mcp | MCP transport, read-only 검색, health/readiness | 수집기, Codex OAuth/login, OCR, publisher write 권한 |
| cardrag-worker | 카드사 adapter, PDF 검증, OCR, 구조화, embedding | 외부 공개 MCP endpoint |
| cardrag-admin 또는 동일 worker의 제한 entrypoint | migration, generation 검증·게시, backup 보조 | 상시 공개 service |

하나의 source repository에서 multi-stage target으로 만들 수 있지만 runtime package와 Linux capability, user, egress, volume mount는 역할별로 다르게 한다. scheduler는 worker container 안의 무한 loop보다 host scheduler나 명시적 scheduled job으로 분리하는 방향을 우선 검토한다.

### 9.2 External volume

| volume | MCP | worker/publisher | 용도 |
|---|---|---|---|
| published generations/current pointer | read-only | publisher만 write | 온라인 검색 snapshot |
| PDF/OCR object store | 게시 승인 원본의 제한 view만 read-only | read-write | 불변 source artifact와 명시적 PDF·페이지 조회 |
| build workspace | 미마운트 | read-write | candidate generation과 임시 파일 |
| PostgreSQL data | catalog 조회에 필요한 최소 권한 | scheduler/worker read-write | durable catalog, queue, lease, run event |
| quarantine | 미마운트 | 제한 read-write | 실패 artifact와 조사자료 |
| Codex auth state | 미마운트 | OCR worker만 제한 read-write | container 재생성 후 OAuth 상태 유지 |
| backup staging | 미마운트 | backup job만 | 일관된 snapshot 준비 |
| temporary scratch | 미마운트 | ephemeral | render·CLI 임시 파일, 재생성 가능 |

PDF, OCR과 generation은 외부 불변 file volume에 두고 작업상태·catalog는 PostgreSQL에 둔다. index, PostgreSQL data와 OAuth state에는 서로 다른 접근정책을 적용하며 하나의 data root를 모든 container에 read-write로 mount하지 않는다. container 삭제 후에도 필요한 volume은 유지하되 auth volume과 일반 backup의 정책은 분리한다.

### 9.3 Image와 runtime hardening

- 버전과 digest가 고정된 최소 base image를 사용한다.
- Python·Codex CLI와 system dependency 버전을 lock하고 SBOM을 생성한다.
- non-root user, read-only root filesystem, 제한된 writable mount를 기본으로 한다.
- 필요 없는 Linux capability를 제거하고 privileged mode를 사용하지 않는다.
- worker의 OCR sandbox에는 필요한 PDF/임시 경로만 mount한다.
- memory/CPU/PID와 임시 disk 한도를 정하고 graceful termination 시간을 둔다.
- init/reaping, health check, timezone과 clock sync를 명시한다.
- Git, source PDF/OCR, build cache, local env, OAuth/token 파일은 build context에서 제외한다.
- image 취약점 scan과 dependency license 검토를 release gate에 포함한다.
- Docker Hub public image는 누구나 layer와 패키징된 애플리케이션 코드·dependency metadata를 검사할 수 있음을 전제로 한다. private GitHub의 비공개성만으로 image 내부 코드를 숨길 수 있다고 가정하지 않는다.

레거시 OCR의 **danger-full-access** 설정은 그대로 계승하지 않는다. Codex CLI가 필요로 하는 최소 filesystem·network 권한을 exact version으로 검증하고 worker 격리 경계를 정의해야 한다.

### 9.4 Network 경계

- MCP service의 inbound는 HTTPS 기반 HTTP MCP endpoint와 health endpoint만 연다.
- MCP가 query embedding에 OpenRouter를 사용한다면 해당 egress만 허용하고 timeout/circuit breaker를 둔다.
- worker는 승인된 카드사 domain, Codex 인증·실행 endpoint, OpenRouter만 egress allowlist 후보로 둔다.
- discovery가 돌려준 URL은 host·redirect·size·PDF 검증을 거친다.
- publisher와 backup job은 외부 공개 port를 갖지 않는다.
- container network와 log 접근 권한을 운영자 role로 제한한다.
- token은 `Authorization` header에서만 받고 URL query·path, access log와 error log에 남기지 않는다.
- 원본 PDF 전달은 게시 catalog의 document ID를 통해서만 허용하고 임의 URL·host path·object key를 외부 입력으로 사용하지 않는다.

HTTP+OAuth token 접속과 운영 HTTPS 사용은 확정이다. `search`·`source_pdf` scope, 자동 access token 갱신, refresh token rotation과 90일 비활성 만료를 적용한다. TLS termination 위치, authorization server 제품·배포, 사용자/tenant와 운영자 권한은 **결정 필요**다.

## 10. 인증과 secret

### 10.1 Codex CLI OAuth

목표는 OCR worker image에 Codex CLI를 설치하고 OAuth 상태를 외부 제한 volume에 보존하여 container 재생성 후에도 인증을 재사용하는 것이다. 그러나 현재 문서 작성 단계에서는 CLI 설치·로그인을 실행하지 않았다.

다음 항목은 exact Codex CLI version과 깨끗한 test container에서 확인해야 한다.

1. **검토 필요:** 해당 버전이 headless Linux에서 device-code OAuth flow를 공식 지원하는지 확인한다.
2. **검토 필요:** 로그인 command, TTY 필요 여부, device URL·user code가 stdout/stderr 중 어디에 출력되는지 확인한다.
3. **검토 필요:** Docker 로그만으로 operator가 URL과 device code를 확인하고 다른 기기에서 승인을 완료할 수 있는지 end-to-end 시험한다.
4. **검토 필요:** OAuth state 저장경로를 명시적으로 설정·mount할 수 있는지, 갱신 token이 재시작 후 정상 사용되는지 확인한다.
5. **검토 필요:** 비대화형 **codex exec**가 장시간 OCR 중 token 갱신·만료를 어떻게 처리하는지 확인한다.

device login을 Docker 로그로 제공해야 한다면 전용 1회성 auth job을 사용한다.

- 로그인 URL, 짧은 수명의 user code와 상태만 operator가 볼 수 있게 한다.
- access token, refresh token, session payload는 절대 stdout/stderr에 출력하지 않는다.
- device user code도 유효시간 동안 인증 수단이므로 log viewer RBAC와 짧은 retention을 적용한다.
- 중앙 로그 수집기로 전달할지 여부를 보안 검토한다. 전달하지 않는 전용 log channel이 더 적절할 수 있다.
- login 완료 후 auth job을 종료하고 OCR worker에서 최소 무해 요청으로 인증상태를 점검한다.
- auth volume 권한은 OCR worker UID만 읽고 쓸 수 있게 하며 MCP service와 공유하지 않는다.
- 로그아웃·token 폐기·계정 변경과 사고 시 회수 절차를 runbook에 둔다.

CLI가 요구한 headless/device-code 동작을 제공하지 않는다면 log scraping이나 token 복사로 우회한다고 확정하지 않는다. 공식 지원 방식 또는 별도 안전한 bootstrap 절차를 선택해야 한다.

### 10.2 OpenRouter

- **OPENROUTER_API_KEY**는 environment 또는 orchestrator secret file로 주입한다.
- repository의 env 파일, image layer, Compose 평문, job DB와 로그에 key를 넣지 않는다.
- worker와 MCP가 모두 key를 필요로 하면 서비스별 key·quota 분리를 우선한다.
- key rotation과 폐기 절차를 마련하고 provider 401/429를 구분한다.
- model ID, dimension, provider endpoint와 timeout은 secret이 아닌 versioned 설정으로 관리한다.
- 실제 사용 model/config hash를 OCR 또는 embedding provenance와 generation manifest에 기록한다.
- 외부 전송 데이터 범위와 provider 보존정책을 보안·법무 관점에서 승인받는다.

### 10.3 MCP OAuth token

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

### 10.4 그 밖의 secret

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

아래는 최종 구현 단계의 계획이며 이번 작업에서 build, login, push 또는 promotion을 수행하지 않았다.

1. 현재 로그인된 Docker 계정과 namespace를 실제로 확인한다.
2. 대상 public repository의 namespace와 lowercase slug를 확인하거나 승인 후 생성한다. 후보 slug는 **mcp_card_prd_detail**이다.
3. test, license/secret scan, SBOM, vulnerability scan을 통과한 image를 재현 가능하게 build한다.
4. release version과 Git SHA를 포함한 immutable tag를 붙이고 image digest를 기록한다.
5. candidate tag를 public repository에 push한다. 이 시점부터 image에 패키징된 code와 metadata는 공개된 것으로 취급한다.
6. 깨끗한 host에서 digest로 pull하여 data를 포함하지 않았는지, non-root/read-only 실행과 smoke test를 확인한다.
7. 승인된 digest만 운영 tag로 promotion하고 deployment 기록에 image digest와 호환 generation을 남긴다.
8. 실패 시 이전 digest로 rollback한다.

**latest** tag만으로 배포 상태를 식별하지 않는다. repository 가시성은 public으로 확정됐으며 GitHub repository는 private로 유지한다. Docker Hub 로그인 상태, namespace·lowercase repository 이름, multi-architecture 필요성, image signing·provenance 방식은 구현 시 확인한다. 확인·push하지 않은 결과를 완료로 보고하지 않는다.

## 13. Backup과 restore

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

다음은 운영 요구에 따라 **결정 필요**다.

- 일일 discovery/job state가 허용하는 RPO
- 마지막 정상 generation으로 복구하는 RTO
- raw PDF/OCR과 과거 generation 보존기간
- off-site와 다른 failure domain의 backup 개수
- 암호화 key 관리, 개인정보·원문 삭제 요청 처리

최소한 current와 직전 검증 generation, 해당 source catalog, job state의 최근 consistent backup을 함께 복구할 수 있어야 한다.

### 13.3 Restore 절차

1. 원인을 확인하고 writer·publisher를 중지한다.
2. 기존 운영 경로를 덮어 쓰지 않고 격리된 새 restore 경로에 복원한다.
3. checksum, manifest, catalog 참조, DB/index integrity를 검증한다.
4. candidate를 read-only로 열어 MCP와 대표 검색 smoke test를 수행한다.
5. durable state의 lease를 만료·회수하고 이미 성공한 artifact와 중복 실행 여부를 점검한다.
6. 승인 후 current pointer 또는 deployment를 교체한다.
7. 복구시각, RPO/RTO 실제값, 손실·재처리 범위와 후속조치를 기록한다.

backup 성공 로그만으로 복구 가능성을 인정하지 않는다. 정기 restore drill과 다른 host에서의 검증이 인수 기준이다.

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
- backup 실행·restore drill
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
| backup | consistent snapshot과 격리 경로 restore drill |
| registry | public repository, version+Git SHA tag, digest pull/run, data·secret 미포함, 공개 code 검토 |

## 16. 결정 필요 항목

- OAuth authorization server, 사용자/tenant 모델과 운영자 권한
- PostgreSQL schema·migration·backup과 scheduler 세부 방식
- offline BULK worker concurrency, provider quota와 비용한도
- worker별 lease·timeout·retry budget
- 일일 실행시각과 issuer별 rate limit
- generation ID 형식, 최소 3개 초과 보존기간, RPO·RTO와 backup target
- BULK pilot 이후 SLO와 단일 host를 넘어설 autoscaling 기준
- vector/lexical engine과 degraded mode의 정량 품질 합격선
- Codex CLI exact version과 공식 headless/device-code 지원 여부
- OAuth state 저장·갱신·회수와 device-code log 보안정책
- Docker Hub namespace·lowercase repository slug, image signing·promotion 승인 정책

## 17. 이 문서 작성 시점의 완료 상태

| 항목 | 상태 |
|---|---|
| 초기·증분 운영 원칙 문서화 | 문서화 완료 |
| durable state·lease·retry·dead-letter 계약 문서화 | 문서화 완료 |
| immutable generation publish·rollback 방향 문서화 | 문서화 완료 |
| Docker volume·secret·backup 방향 문서화 | 문서화 완료 |
| 신규 scheduler/worker/MCP 구현 | 미착수 |
| 초기 3~4일 대량 처리 실행 | 미수행 |
| 일일 증분 run 실행 | 미수행 |
| Codex CLI 설치·OAuth/device login 검증 | 미수행, 검토 필요 |
| OpenRouter key 주입·호출 | 미수행 |
| Dockerfile/Compose 작성·image build | 미수행 |
| Docker Hub login 확인·repository 생성·push | 미수행 |
| backup·restore drill | 미수행 |

향후 체크리스트 상태는 실제 code, test report, generation ID, image digest와 운영 로그로 증명된 경우에만 “검증 완료”로 바꾼다.
