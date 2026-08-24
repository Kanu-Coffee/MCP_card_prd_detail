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
  tag 대상 수동 실행, `PUBLISH-vX.Y.Z` 확인 문자열, exact dependency-license gate, OIDC와 digest 서명을 요구하는지
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
- **현재 환경에서 검증할 수 없는 이유:** Docker Hub secret과 외부 registry 변경이 필요하다.
- **개발 단계 대체 검증:** 로컬 Docker에서 세 target을 `linux/amd64`로 no-push build하고
  MCP/worker/admin의 SBOM·vulnerability·image content를 검사했다. GitHub CI/release workflow는
  동일 gate, 역할별 tag·semver·수동 dispatch confirmation·environment secret·OIDC·digest-signing 계약을 코드로
  검증하며 최초 원격 성공 run은 source push 뒤 별도 확인한다.
- **실환경 절차:** GitHub `dockerhub-public` environment에
  `DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN`을 설정한다. 이 private repository의 현재 plan에서는
  required reviewer를 release gate로 가정하지 않는다. tag push는 release를 자동 실행하지 않으며,
  승인된 운영자가 해당 tag를 대상으로 `version=X.Y.Z`, `confirmation=PUBLISH-vX.Y.Z`를 입력해
  workflow를 수동 실행하는 것을 공개 push 승인으로 사용한다. 별도의 commercial-license attestation
  secret은 필요하지 않다. `pypdfium2 5.12.1`·PDFium의 고정 wheel metadata/hash와 wheel에
  포함된 build-specific license payload 전체가 `/usr/share/licenses/cardrag/pypdfium2`에,
  프로젝트 고지가 `/usr/share/licenses/cardrag/THIRD_PARTY_NOTICES.md`에 들어갔는지 image
  gate 결과를 확인한다. dependency를 갱신할 때는 새 wheel의 build별 license와 binary linkage를
  다시 검토하고 policy hash를 reviewed commit으로만 변경한다.
  psycopg·psycopg-binary·psycopg-pool의 LGPL-3.0-only 적용 의무와 notice도 release 기록에서
  확인하고 certifi의 MPL-2.0 notice·배포 의무도 기록한다. 증빙 자체·계약번호는 Git, image,
  log에 넣지 않는다. 승인된 commit에 정확한
  `vX.Y.Z` tag를 만들고 workflow를 승인한다. 생성 manifest에 기록된 세 역할의
  digest로 각각 pull한 후 아래와 같이 서명을 확인한다.

  `environment: dockerhub-public`은 Docker Hub secret의 범위를 제한하지만 현재 plan에서 별도
  reviewer gate를 제공한다고 가정하지 않는다. 정확한 tag ref, main ancestry, 성공한 동일 SHA CI,
  수동 confirmation이 하나라도 맞지 않으면 workflow가 공개 push 전에 실패해야 한다.
  workflow 재실행 시 기존 역할 tag는 OCI revision label이 동일 Git SHA이고 두 alias가 동일
  digest인 경우에만 재사용한다. 다른 SHA의 tag는 덮어쓰지 않으며, 이전 실행이 일부 역할에서
  중단된 경우 누락 alias만 동일 digest로 복구한다. Docker Hub 저장소의 SemVer 역할-tag
  immutable 설정(`enabled=true`, release workflow의 exact regex)이 시작 전에 확인되지 않으면
  fail-closed한다. Cosign digest signature/attestation tag는 재서명을 위해 regex 밖에 둔다.

  ```bash
  export RELEASE_VERSION=0.2.1
  export RELEASE_GIT_SHA=REPLACE_WITH_RELEASE_GIT_SHA
  cosign verify \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com \
    --certificate-identity "https://github.com/Kanu-Coffee/MCP_card_prd_detail/.github/workflows/release.yml@refs/tags/v${RELEASE_VERSION}" \
    --certificate-github-workflow-sha "$RELEASE_GIT_SHA" \
    --check-claims=true \
    ymtop59/mcp-card-prd-detail@sha256:REPLACE_WITH_ROLE_DIGEST
  ```

- **성공 조건:** tag push만으로 release run이 생성되지 않고, 해당 tag 대상 수동 run의 confirmation이
  일치하며 release-approval job이 exact dependency metadata·wheel hash·license payload를 검증했고,
  그 결과가 image 내부 manifest와 최종 release artifact의 SHA-256에 결속됐으며,
  공개 tag는 `${version}-{mcp|worker|admin}`과 역할별 Git SHA tag를 제공한다.
  통합 manifest는 세 역할의 서로 다른 digest와 `linux/amd64` platform을 기록하고,
  deployment는 digest를 사용하며 각 `cosign-{role}.verification.json`의 SHA-256과
  registry OCI signature의 transparency-log material이 검증된다. `latest`만으로 식별하지 않는다.
- **실패 진단:** environment approval, dependency-license policy/version/wheel·notice hash, tag pattern, Docker Hub permission,
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
- **실환경 절차:** 일반 Compose 배포는 `deploy/systemd/README.md`를 사용한다.
  Portainer host-bind 배포는 개발 named volume을 가리키는 해당 unit을 설치하지
  않고 `deploy/portainer/RUNBOOK.md`의 전용 `cardrag-portainer-*` unit만 사용한다.
  경로와 실행 계정을 검토한 뒤 worker를 먼저 상시 기동하고 다음 실행시각을 확인한 후
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

## v1 범위 밖 / v0.2에서 추가된 범위

- v0.1에서는 backup·restore 구현과 RPO/RTO 검증이 범위 밖이었다. v0.2에는 maintenance-window
  portable export/verify/empty-target restore 자동화를 추가했다. 실제 NAS·다른 물리 서버의
  RPO/RTO drill과 최근 검증본 3개 NAS lifecycle 적용은 여전히 운영 인계다.
- Nginx Proxy Manager container 또는 public TLS 구성 자체
- ARM64 image
- public admin API와 운영 웹 UI

이 항목은 v1 개발 goal의 미완료로 계산하지 않는다. 단, 구현 범위를
확대하는 별도 요청이 생기면 독립 요구사항과 인수 기준을 작성한다.
