# Docker Hub 운영 테스트

이 절차는 소스에서 애플리케이션 이미지를 다시 빌드하지 않고 public Docker Hub의
역할별 이미지를 사용한다. PostgreSQL과 Keycloak은 `compose.yaml`에 고정된 upstream
digest를 그대로 사용한다.

## 1. 이미지 선택

기본 운영 테스트 버전은 `0.1.2`이다. `deploy/dockerhub.compose.yaml`은 아래 세 역할
태그를 사용한다.

- `ymtop59/mcp-card-prd-detail:0.1.2-mcp`
- `ymtop59/mcp-card-prd-detail:0.1.2-worker`
- `ymtop59/mcp-card-prd-detail:0.1.2-admin`

`0.1.0`과 `0.1.1`은 공개 과정에서 image push 뒤 통합 release manifest 생성 전에 중단된
부분 release다. 운영 테스트에는 사용하지 않고, OCI signature와 통합 release manifest까지
검증된 `0.1.2`만 사용한다.

Docker Hub 저장소는 SemVer 역할 tag(`X.Y.Z-{mcp|worker|admin}`와 대응 SHA alias)를
immutable 정규식으로 설정한다. 동일 이름 tag는 덮어쓰거나 삭제하지 않고, 새 소스 revision은
반드시 새 SemVer를 사용한다. Cosign의 digest 기반 signature/attestation 보조 tag는 서명
재시도를 위해 이 정규식에서 제외한다.

처음 pull한 뒤에는 Docker Hub 또는 release manifest에서 확인한 digest로 고정하는 것을
권장한다.

```bash
export CARDRAG_MCP_IMAGE='ymtop59/mcp-card-prd-detail@sha256:REPLACE_MCP_DIGEST'
export CARDRAG_WORKER_IMAGE='ymtop59/mcp-card-prd-detail@sha256:REPLACE_WORKER_DIGEST'
export CARDRAG_ADMIN_IMAGE='ymtop59/mcp-card-prd-detail@sha256:REPLACE_ADMIN_DIGEST'
```

digest를 아직 고정하지 않은 최초 smoke에서는 위 세 변수를 생략할 수 있다.

## 2. Secret과 인증 준비

`deploy/secrets/README.md`에 적힌 전용 디렉터리와 파일을 먼저 만든 뒤 그 절대 경로를
설정한다. secret 파일이나 실제 PDF는 Git 또는 이미지에 넣지 않는다.

```bash
export CARDRAG_SECRETS_DIR=/absolute/path/to/cardrag-secrets
```

Codex OCR을 시험할 worker는 별도의 device authorization이 필요하다. 인증 volume 소유권을
먼저 준비해야 하므로 실제 login 명령은 다음 절의 `volume-init` 뒤에서 실행한다.

## 3. Pull과 최초 bootstrap

모든 명령에 release overlay를 함께 사용한다. overlay 자체가 애플리케이션 서비스의
`build` 정의를 reset하고 `pull_policy: always`를 강제하며, 명령에도 `--no-build`를 함께
사용해 로컬 소스 이미지가 섞이지 않게 한다.

```bash
docker compose \
  -f compose.yaml \
  -f deploy/dockerhub.compose.yaml \
  --profile '*' pull

docker compose \
  -f compose.yaml \
  -f deploy/dockerhub.compose.yaml \
  run --rm --no-deps --pull always volume-init

docker compose \
  -f compose.yaml \
  -f deploy/dockerhub.compose.yaml \
  --profile worker run --rm --no-deps worker codex login --device-auth

docker compose \
  -f compose.yaml \
  -f deploy/dockerhub.compose.yaml \
  --profile worker run --rm --no-deps worker codex login status

docker compose \
  -f compose.yaml \
  -f deploy/dockerhub.compose.yaml \
  -f deploy/keycloak/bootstrap.compose.yaml \
  up -d --no-build --wait postgres keycloak

docker compose \
  -f compose.yaml \
  -f deploy/dockerhub.compose.yaml \
  run --rm --pull always migrate
```

브라우저에 표시된 URL과 단기 code로 승인을 마친 뒤 `codex login status`가 로그인 상태를
확인해야 한다. token 내용은 출력·복사하지 않는다. 검색/MCP만 시험할 때는 두 Codex login
명령을 생략할 수 있다.

영구 Keycloak 관리자와 필요한 client를 만든 다음 bootstrap 관리자를 폐기하고
`keycloak_admin_password.txt`를 삭제한다. 이후 base stack으로 Keycloak을 재생성한다.

```bash
docker compose \
  -f compose.yaml \
  -f deploy/dockerhub.compose.yaml \
  up -d --no-build --force-recreate --no-deps keycloak
```

## 4. MCP와 worker 시작

MCP는 host의 `127.0.0.1:8000`에만 게시된다. 외부 TLS와 hostname은 별도 Nginx Proxy
Manager에서 연결한다.

```bash
docker compose \
  -f compose.yaml \
  -f deploy/dockerhub.compose.yaml \
  up -d --no-build mcp

curl --fail http://127.0.0.1:8000/health/live
ready_status=$(curl --silent --show-error --output /dev/null \
  --write-out '%{http_code}' http://127.0.0.1:8000/health/ready)
case "$ready_status" in
  200|503) ;;
  *) echo "unexpected readiness status: $ready_status" >&2; exit 1 ;;
esac
```

published generation이 아직 없으면 readiness `503`은 정상이다. 초기 BULK를 수행할 때만
worker profile을 시작한다. generation 게시 후에는 아래 명령으로 readiness `200`을
필수 확인한다.

```bash
docker compose \
  -f compose.yaml \
  -f deploy/dockerhub.compose.yaml \
  up -d --no-build --wait mcp

curl --fail http://127.0.0.1:8000/health/ready
```

초기 BULK worker는 다음과 같이 시작한다.

```bash
docker compose \
  -f compose.yaml \
  -f deploy/dockerhub.compose.yaml \
  --profile worker up -d --no-build worker
```

worker가 정상 기동한 뒤 admin one-shot으로 초기 BULK를 실행한다. 이 명령은 우리→KB→신한
순서와 issuer 사이 10분 대기를 포함하며, candidate 품질 검증·seal·publish가 끝날 때까지
foreground에서 기다린다.

```bash
docker compose \
  -f compose.yaml \
  -f deploy/dockerhub.compose.yaml \
  --profile ops run --rm --no-build admin cardrag run bulk

curl --fail http://127.0.0.1:8000/health/ready
```

중간에 운영 terminal이 끊겼다면 새 BULK를 만들지 말고 `cardrag run list --state running`으로
기존 run ID를 찾은 뒤 `cardrag run status RUN_ID`, `cardrag run finalize RUN_ID` 순서로
동일 run을 복구한다. 자세한 절차는 운영 RUNBOOK을 따른다.

초기 migration, Keycloak/OIDC client, Codex device token, issuer 실사이트와 generation
게시 절차는 `docs/REAL_ENV_HANDOFF.md`의 성공 조건을 함께 따른다.
