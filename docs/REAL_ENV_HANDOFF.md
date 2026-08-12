# 실환경 검증 및 운영 인계

이 문서는 현재 개발 환경에서 실제 계정·별도 기기·외부 네트워크·운영
host·수동 승인이 없어 완료할 수 없는 검증을 분리한다. 아래 항목은 코드,
fixture/mock 통합시험과 정적 배포 검증이 완료된 뒤에는 개발 goal 완료를
막지 않는다. 검증하지 않은 항목을 완료로 표시하지 않으며, 실제 실행 결과와
식별자만 이 문서에 추가한다.

## 개발 환경에서 완료해야 하는 대체 검증

- `docker compose config --quiet`로 Compose와 secret mount를 렌더링한다.
- 1회 bootstrap overlay를 함께 렌더링하고, bootstrap 이후 base Compose에는
  Keycloak 초기 admin secret이 mount되지 않는지 확인한다.
- `mcp`, `worker`, `admin` target을 `linux/amd64`로 build하고 non-root 사용자,
  read-only root filesystem, loopback port 및 금지 파일 부재를 검사한다.
- fixture PostgreSQL·Keycloak에서 DB 분리, realm 설정, issuer/audience/scope,
  refresh rotation과 revoke 계약을 검증한다.
- mock 카드사와 provider로 scheduler 순서, 10분 대기, issuer 실패 격리,
  BULK 재시작과 secret redaction을 검증한다.
- release workflow가 일반 `main` event에서는 실행되지 않고 정확한 `vX.Y.Z`
  tag, protected environment, dependency-license attestation, OIDC와 digest 서명을 요구하는지
  정적 검사한다.

## 운영 인계 항목

### 1. 카드사 live endpoint와 이용조건

- **상태:** `실환경 검증 대기`
- **현재 환경에서 검증할 수 없는 이유:** 공개 endpoint의 실제 markup·rate
  limit·redirect와 수집·재배포·상업적 이용조건은 fixture로 확정할 수 없다.
- **개발 단계 대체 검증:** 세 카드사 fixture/contract test, allowlist,
  streaming 크기 제한, PDF 구조검사, markup 급감 감지와 idempotency test.
- **실환경 절차:** 승인된 운영 host에서 issuer별 제한 pilot을 실행하고 run
  report의 발견·신규·동일·실패 합계를 대사한다. 이용조건 검토가 완료되기
  전에는 source PDF를 승인 사용자 밖으로 공개하지 않는다.
- **성공 조건:** 허용 범위 기록, issuer별 accounting 일치, 비정상 redirect
  없음, rate/backoff 준수, PDF hash·page 검증 통과.
- **실패 진단:** issuer run ID, stable error code, 최종 허용 host, HTTP status,
  rate-limit header와 fixture/live parser diff를 확인한다. 응답 본문과 token은
  일반 로그에 남기지 않는다.

### 2. Codex headless device authorization

- **상태:** `실환경 검증 대기`
- **현재 환경에서 검증할 수 없는 이유:** 실제 계정 승인과 별도 사용자
  기기가 필요하고 OAuth credential을 자동화 시험에 넣을 수 없다.
- **개발 단계 대체 검증:** 승인 CLI version 고정, 제한 auth volume,
  `codex login --device-auth` 지원 여부·login status contract, worker와 MCP의
  auth volume 격리를 확인한다. worker image의 system-owned `ocr` permission
  profile과 bubblewrap canary로 rendered page 읽기만 허용되고 workspace 쓰기,
  `/run/secrets`, workspace 밖 파일, network socket이 거부되는지도 확인한다.
- **실환경 절차:** 일회성 auth container에서 `CODEX_HOME`을 제한 volume으로
  지정하고 `codex login --device-auth`를 시작한다. 운영자가 표시된 URL과
  단기 code로 별도 기기에서 승인한 후 `codex login status`와 무해한 최소
  `codex exec`를 실행한다. container 재생성 뒤 status와 token 갱신을 다시
  확인한다. 이 검증은 `docker compose --profile worker run --rm worker codex ...`
  형태로 worker target·전용 auth volume을 사용하며 MCP/admin image에는
  volume을 mount하지 않는다.
- **성공 조건:** secret token 출력 없음, worker만 auth volume 접근, 재생성
  후 인증 유지, 비대화형 실행과 장기 갱신 성공. OCR sandbox canary 4종이
  fail-closed이고 worker가 non-root/cap-drop/NNP/read-only rootfs를 유지한다.
- **실패 진단:** CLI exact version, TTY 요구, `CODEX_HOME` ownership, clock,
  redacted exit code를 확인한다. credential을 로그나 Compose 환경변수로
  복사해 우회하지 않는다.

### 3. OpenRouter 실제 provider·quota

- **상태:** `실환경 검증 대기`
- **현재 환경에서 검증할 수 없는 이유:** 운영 key·과금·quota와 실제 모델
  가용성은 repository에 제공하지 않는다.
- **개발 단계 대체 검증:** mock 200/401/429/5xx, Retry-After, timeout,
  circuit breaker, vector count/dimension/finite 값과 redaction test.
- **실환경 절차:** service별 제한 key를 secret file로 주입하고 작은 gold
  batch에서 모델 ID, dimension, provider route, latency와 비용을 기록한다.
- **성공 조건:** gold gate 통과, dimension 일치, bounded retry, secret/log
  누출 없음, 승인 quota 내 동작.
- **실패 진단:** provider request ID, HTTP/error class, Retry-After, model route,
  circuit 상태와 redacted metric을 확인한다.

### 4. 실제 장시간 BULK와 운영 host 용량

- **상태:** `실환경 검증 대기`
- **현재 환경에서 검증할 수 없는 이유:** 전체 corpus, 수일의 실행시간과
  운영 host CPU·RAM·disk·inode가 필요하다.
- **개발 단계 대체 검증:** synthetic/fixture BULK, 강제 worker 종료,
  lease fencing·resume·idempotency, 동시 요청 5개 load와 resource report.
- **실환경 절차:** preflight 후 issuer별 pilot로 ETA와 resource 한도를 정하고
  전체 BULK를 실행한다. 중간에 worker를 한 번 재생성해 checkpoint resume을
  확인하고 candidate 검증 전 active generation을 바꾸지 않는다.
- **성공 조건:** latest coverage 100%, 과거 실패 quarantine/report, 중복 처리
  없음, 품질 gate 통과, active generation 무중단, disk/inode 여유 유지.
- **실패 진단:** run/job/generation ID, queue age, lease, provider quota,
  volume·inode, stage별 latency와 dead-letter 분포를 확인한다.

### 5. Docker Hub 공개 release와 Cosign transparency log

- **상태:** `운영 인계`
- **현재 환경에서 검증할 수 없는 이유:** Docker Hub secret, private GitHub
  environment의 수동 승인과 외부 registry 변경이 필요하다. 또한 Proprietary project/image에
  포함된 PyMuPDF 1.28.2는 AGPL-3.0 또는 Artifex commercial license의 dual license이므로
  적용 license와 의무를 법무가 확정해야 한다.
- **개발 단계 대체 검증:** 로컬 Docker에서 세 target을 `linux/amd64`로 no-push build하고
  MCP/worker/admin의 SBOM·vulnerability·image content를 검사했다. GitHub CI/release workflow는
  동일 gate, 역할별 tag·semver·protected environment·OIDC·digest-signing 계약을 코드로
  검증하며 최초 원격 성공 run은 source push 뒤 별도 확인한다.
- **실환경 절차:** GitHub `dockerhub-public` environment에 required reviewer와
  `DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN`을 설정한다. 현재 Proprietary image에는 PyMuPDF
  Artifex commercial license 증빙을 확인한 경우에만 protected environment secret
  `CARDRAG_DEPENDENCY_LICENSE_ATTESTATION`을 policy 파일의 exact attestation 값으로 설정한다.
  AGPL 선택 시 이 secret으로 우회하지 않는다. 먼저 project/image license, notice,
  corresponding-source 공개 경로와 policy/gate를 별도 reviewed commit에서 구현·검증한다.
  psycopg·psycopg-binary·psycopg-pool의 LGPL-3.0-only 적용 의무와 notice도 release 기록에서
  확인하고 certifi의 MPL-2.0 notice·배포 의무도 기록한다. 증빙 자체·계약번호는 Git, image,
  log에 넣지 않는다. 승인된 commit에 정확한
  `vX.Y.Z` tag를 만들고 workflow를 승인한다. 생성 manifest에 기록된 세 역할의
  digest로 각각 pull한 후 아래와 같이 서명을 확인한다.

  `environment: dockerhub-public` 선언만으로 reviewer가 자동 설정되지는 않는다.
  private repository에서 required reviewer를 사용할 수 있는지 현재 GitHub plan과
  repository 설정에서 확인한다. reviewer 보호가 실제로 설정되지 않았거나 해당
  plan에서 지원되지 않으면 tag를 만들지 말고 release를 차단한다.

  ```bash
  cosign verify \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com \
    --certificate-identity-regexp '^https://github.com/Kanu-Coffee/MCP_card_prd_detail/.github/workflows/release.yml@refs/tags/v[0-9]+\.[0-9]+\.[0-9]+$' \
    ymtop59/mcp-card-prd-detail@sha256:REPLACE_WITH_ROLE_DIGEST
  ```

- **성공 조건:** tag push 뒤 publish job이 실제 reviewer 승인 전 `Waiting`이고,
  license-approval job이 exact attestation을 검증했으며,
  공개 tag는 `${version}-{mcp|worker|admin}`과 역할별 Git SHA tag를 제공한다.
  통합 manifest는 세 역할의 서로 다른 digest와 `linux/amd64` platform을 기록하고,
  deployment는 digest를 사용하며 각 `cosign-{role}.bundle.json`의 transparency-log
  material이 검증된다. `latest`만으로 식별하지 않는다.
- **실패 진단:** environment approval, dependency-license policy/version/attestation, tag pattern, Docker Hub permission,
  workflow OIDC subject, pushed digest와 Cosign certificate identity를 확인한다.

### 6. 운영 client refresh 호환성과 hosting

- **상태:** `운영 인계`
- **현재 환경에서 검증할 수 없는 이유:** 실제 MCP client, Nginx Proxy Manager,
  public hostname과 TLS certificate는 개발 project 밖의 hosting 자산이다.
- **개발 단계 대체 검증:** fixture Keycloak client의 PKCE/Client Credentials,
  refresh rotation·reuse reject·revoke, token header·scope와 host
  `127.0.0.1:8000` endpoint 통합시험. 기본 local issuer는 host와 Compose
  network 양쪽에서 같은 이름을 쓰는
  `http://cardrag-keycloak.localhost:8080/realms/cardrag`이다.
- **실환경 절차:** 승인 client를 수동 등록하고 Nginx Proxy Manager에서 MCP와 Keycloak을
  각각 TLS proxy한다. NPM이 host process 또는 host-network container라면 upstream은
  `127.0.0.1:8000`과 `127.0.0.1:8080`이다. NPM이 별도 bridge-network container라면 그
  container의 `127.0.0.1`은 NPM 자신이므로 사용하지 않는다. 운영자가 NPM을 CardRAG의 명시적
  shared Docker network에 연결한 뒤 `mcp:8000`과 `keycloak:8080`을 upstream으로 사용하거나,
  별도로 승인한 host-network topology를 사용한다. 이 project는 NPM container/network를
  자동 변경하지 않는다. 최초 기동에만 `deploy/keycloak/bootstrap.compose.yaml`을 함께
  사용하고 영구 admin 생성·확인 뒤 bootstrap 계정을 회전 또는 폐기한다. 이후
  base Compose만으로 Keycloak을 재생성하고 초기 admin secret 파일을 제거한다.
  `KEYCLOAK_PUBLIC_URL`과 `CARDRAG_OIDC_ISSUER`를 동일한 public HTTPS
  origin(`/realms/cardrag` 포함 여부만 다름)으로 설정하고 MCP public URL도
  override한다. 승인 client에 optional `search`·`source_pdf` scope를 최소한으로
  할당하고 access token의 `iss`, `aud=cardrag-mcp`, `scope`를 확인한다. 최초 승인
  후 access token 만료를 기다리거나 test lifespan을 줄여 자동 refresh를 확인한다.

- 사람 사용자의 PKCE client는 최초 승인 때 `offline_access`를 요청해야 한다. 이 offline refresh
  token은 계속 사용하면 회전되어 90일 비활성 만료가 연장되고, realm에는 절대 max lifespan을
  두지 않는다. 반면 service client는 refresh token을 보관하지 않고 client credentials로 짧은
  access token을 다시 발급받는다.
- **성공 조건:** token이 URL/log에 없고 90일 비활성 전 정상 사용에는 수동
  재입력이 필요 없으며 revoke·refresh reuse·scope 위반이 거부된다.
- **실패 진단:** OIDC discovery, issuer/audience/scope, redirect URI, PKCE,
  proxy forwarded header, client refresh 지원과 Keycloak event를 확인한다.

### 7. 03:00 KST pipeline과 04:00 KST retention timer 설치

- **상태:** `운영 인계`
- **현재 환경에서 검증할 수 없는 이유:** 최종 host 사용자·checkout 경로와
  Docker daemon 권한에 대한 운영자 승인이 필요하다.
- **개발 단계 대체 검증:** daily·retention service/timer 4개 unit을
  `systemd-analyze verify`하고 one-shot CLI·PostgreSQL 중복 lock 및 owner-only
  retention을 fixture로 검증한다. 단일 retention one-shot은 최신 성공 3세대와
  active/pin 세대를 보존하고 7일 지난 실패 세대 및 만료된 audit/metric을 함께 정리한다.
  timer는 `Persistent=true`로 host 중단 뒤 catch-up한다.
- **실환경 절차:** `deploy/systemd/README.md`의 경로와 전용 계정을 검토한 뒤
  unit을 설치한다. worker를 먼저 상시 기동하고 다음 실행시각을 확인한 후
  두 timer를 enable한다. 의도적인 한 번의 daily 수동 start로 우리 → KB → 신한 순서,
  issuer 종료 후 10분 대기와 실패 격리를 확인하고 retention 수동 start 결과도 검토한다.
- **성공 조건:** pipeline 03:00·retention 04:00 KST 실행, 중복 daily run 거부,
  host 재시작 후 catch-up, worker job graph 완료 뒤 one-shot 종료, worker 계정의
  generation/audit/metric 삭제 거부, token·본문 없는 journal.
- **실패 진단:** `systemctl status`, timer calendar, Compose project·secret dir,
  PostgreSQL scheduler lock, worker queue/lease와 issuer run ID를 확인한다.

### 8. 운영자 generation 게시·rollback rehearsal

- **상태:** `운영 인계`
- **현재 환경에서 검증할 수 없는 이유:** 최종 운영 volume·host와 운영
  담당자의 독립 수행이 필요하다.
- **개발 단계 대체 검증:** fixture generation의 verify/publish/failed publish
  차단/atomic rollback 및 진행 중 요청의 generation pinning test.
- **실환경 절차:** 운영 CLI로 candidate를 검증·게시하고 generation ID를
  확인한 뒤 직전 READY generation으로 rollback한다. image도 기록된 이전
  digest로 재배포하고 health/smoke query를 실행한다.
- **성공 조건:** 부분 generation 노출 없음, 요청 내 generation 혼합 없음,
  rollback 뒤 readiness와 근거 hash 일치, 감사 event 존재.
- **실패 진단:** current pointer, READY/checksum, application-generation 호환성,
  replica generation과 image digest를 대사한다.

## v1 범위 밖

- backup·restore 구현과 RPO/RTO 검증
- Nginx Proxy Manager container 또는 public TLS 구성 자체
- ARM64 image
- public admin API와 운영 웹 UI

이 항목은 v1 개발 goal의 미완료로 계산하지 않는다. 단, 구현 범위를
확대하는 별도 요청이 생기면 독립 요구사항과 인수 기준을 작성한다.
