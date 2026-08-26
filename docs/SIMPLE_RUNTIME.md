# CardRAG v1.0.4 실운영 가이드

이 문서는 처음 운영 서버를 준비하는 사람을 위한 순서형 안내서입니다. 명령은
별도 표시가 없으면 immutable staging인 `/opt/cardrag-v1.0.4`에서 실행합니다.

## 1. 운영 구조 이해하기

CardRAG v1.0.4는 Worker 하나와 MCP 하나로 동작합니다.

```text
카드사 PDF
    |
    v
one-shot Worker -- 게시 --> WebDAV -- 동기화 --> always-on MCP
                                                       |
                                                       v
                                              MCP/HTTP 클라이언트
```

Worker는 한 번 실행된 뒤 종료됩니다. 실행할 때마다 다음 작업을 수행합니다.

1. 활성화된 카드사의 현재 PDF를 찾고 다운로드합니다.
2. 검증된 OCR 캐시를 재사용하거나 OCR을 실행합니다.
3. 검색용 임베딩과 SQLite 세대를 만듭니다.
4. PDF, OCR, SQLite, manifest를 WebDAV에 불변 객체로 게시합니다.
5. 모든 파일 게시가 끝난 뒤 `stable.json` 포인터를 갱신합니다.

MCP는 계속 실행됩니다. 백그라운드에서 WebDAV를 확인하고, SQLite와 그 세대가
참조하는 모든 PDF를 로컬 볼륨에 내려받아 해시를 검증한 뒤 한 번에
활성화합니다. 검색 요청을 처리하는 동안에는 WebDAV에 접속하지 않습니다.

주요 WebDAV 객체는 다음 경로 아래에 만들어집니다.

```text
/v1/objects/sha256/<prefix>/<sha256>
/v1/ocr-cache/native/<prefix>/<reuse-key>/manifest.json
/v1/ocr-cache/native/<prefix>/<reuse-key>/READY.json
/v1/ocr-cache/adopted/<prefix>/<reuse-key>/manifest.json
/v1/ocr-cache/adopted/<prefix>/<reuse-key>/READY.json
/v1/generations/<generation-id>/index.sqlite3
/v1/generations/<generation-id>/manifest.json
/v1/generations/<generation-id>/READY.json
/v1/channels/stable.json
```

Worker만 WebDAV에 씁니다. Worker와 MCP는 같은 WebDAV Basic Auth 계정을
사용하지만 MCP 애플리케이션은 읽기 동작만 노출합니다.
OCR 조회는 native, adopted v2, adopted v1 순서이며 각 항목은 manifest와
`READY.json`, OCR 객체 해시가 모두 일치할 때만 재사용됩니다.

### 처리 불가능한 최신 DRM 문서

일부 카드사는 `application/pdf`라고 응답하면서 실제로는 PDF가 아닌 암호화된
컨테이너를 게시할 수 있습니다. CardRAG는 이를 복호화하거나 과거 PDF로 바꾸지
않습니다. 운영자가 코드에 승인한 `source_id`, 상품코드, 버전, URL, magic,
SHA-256, 크기가 모두 일치하는 현재 원본만 `unsupported_drm`으로 분류합니다.

- 해당 상품은 검색 evidence와 PDF 다운로드가 없지만 `get_product` 및 상품 목록에
  `availability: "unsupported_drm"`과 원본 감사 지문이 표시됩니다.
- 승인된 바이트나 URL이 바뀌거나 새로운 DRM 상품이 나타나면 Worker는 재시도 후
  실패하고 새 세대를 게시하지 않습니다.
- 카드사가 정상 PDF로 다시 게시하면 그 최신 PDF를 일반 문서처럼 처리합니다.

따라서 `unsupported_drm`은 조용한 누락이나 성공으로 위장한 과거 문서가 아니라,
현재 공식 원본을 안전하게 처리할 수 없다는 명시적 서비스 상태입니다.

v1.0.4는 우리카드 운영 점검에서 확인한 정확한 원본 3건(두 Fasoo DRMONE, 한
SCDSA004)을 승인 목록으로 제한합니다. 이 항목을 표현하기 위해 Worker는
`cardrag.generation.v3`/`cardrag.serving-db.v3`를 게시합니다. 해시, 크기, URL,
버전 중 하나라도 달라지면 승인 목록으로 간주하지 않습니다.

### 신한카드 운영 상태

신한카드 adapter는 제거되지 않았습니다. v1.0.4는 검색 목록의 회전 토큰을 문서
정체성에서 제외하고, 실제 다운로드 직전에 공식 목록에서 새 토큰을 다시 가져와
같은 현재 문서인지 검증합니다. 그러나 2026-08-26 실운영 점검에서는 공식
다운로드 경로가 현재 상품 모두에 PDF가 아닌 서비스 오류 HTML을 반환했습니다.

따라서 운영 기본값은 `CARDRAG_ENABLED_ISSUERS=woori,kb`입니다. 공식 경로가 현재
PDF 바이트를 다시 반환하고 시험 실행이 성공한 것을 확인하기 전에는 `shinhan`을
추가하지 마십시오. 이는 adapter 지원 제거가 아니라 upstream 수집 장애에 대한
일시적인 fail-closed 운영 조치입니다.

## 2. 시작 전 준비물

다음을 모두 준비한 뒤 진행합니다.

- Linux AMD64 운영 서버와 `sudo` 권한
- systemd가 실행 중인 운영체제
- Docker Engine과 `docker compose` 명령
- `curl`과 웹 브라우저
- HTTPS WebDAV 주소, 사용자 이름, 비밀번호
- OpenRouter API 키
- MCP가 외부에서 사용할 HTTPS 주소와 TLS 리버스 프록시
- 기존 `/opt/cardrag`을 변경하지 않고 `/opt/cardrag-v1.0.4`에 배치한 v1.0.4 저장소 파일

WebDAV 계정에는 `PROPFIND`, `MKCOL`, `PUT`, `GET`, `HEAD`, `MOVE`, `DELETE`
권한이 필요합니다. `MOVE`의 `Overwrite:F` 요청도 올바르게 거부해야 합니다.

운영 이미지는 다음 두 태그로 고정합니다.

```text
ymtop59/mcp-card-prd-detail:1.0.4-worker
ymtop59/mcp-card-prd-detail:1.0.4-mcp
```

서버에서 배포 파일 위치를 확인합니다.

```bash
sudo chown -R root:root /opt/cardrag-v1.0.4
sudo chmod -R a-w /opt/cardrag-v1.0.4
sudo find /opt/cardrag-v1.0.4 -xdev -type d -exec chmod a+rx {} +
sudo find /opt/cardrag-v1.0.4 -xdev -type f -exec chmod a+r {} +
test -z "$(sudo find /opt/cardrag-v1.0.4 -xdev \
  \( ! -user root -o ! -group root -o -perm /0222 \) -print -quit)"
cd /opt/cardrag-v1.0.4
test -f deploy/worker/compose.yaml
test -f deploy/mcp/compose.yaml
test -f deploy/simple.env.example
docker compose version
```

이 명령은 실행 비트를 유지하면서 모든 쓰기 비트를 제거합니다. 비밀값과 운영 env는
이 디렉터리에 복사하지 말고 계속 `/etc/cardrag`에 둡니다. 모든 명령이 종료 코드
0이어야 합니다.

## 3. Docker와 전용 계정 준비

Docker를 시작하고 부팅 시 자동 시작하도록 설정합니다.

```bash
sudo systemctl enable --now docker
sudo docker info >/dev/null
```

컨테이너와 secret 파일은 숫자 UID/GID 10001을 사용합니다. 먼저 충돌 여부를
확인합니다.

```bash
getent passwd 10001
getent group 10001
```

아무것도 출력되지 않으면 전용 계정을 만듭니다.

```bash
sudo groupadd --gid 10001 cardrag
sudo useradd --uid 10001 --gid 10001 --no-create-home \
  --home-dir /nonexistent --shell /usr/sbin/nologin cardrag
sudo usermod --append --groups docker cardrag
```

이미 `cardrag` 계정이 있다면 새로 만들지 말고 UID/GID가 모두 10001인지
확인합니다.

```bash
id cardrag
```

10001이 다른 계정에 사용 중이면 그대로 진행하지 말고 계정 충돌을 먼저
해결해야 합니다. Docker 그룹은 호스트 관리자 수준 권한을 제공하므로 이
계정에는 로그인 셸을 부여하지 않습니다.

전용 계정이 Docker를 사용할 수 있는지 확인합니다.

```bash
sudo -u cardrag docker info >/dev/null
```

## 4. env 파일과 secret 파일 준비

### 4.1 디렉터리와 env 파일 만들기

```bash
sudo install -d -o root -g cardrag -m 0750 /etc/cardrag
sudo install -d -o root -g cardrag -m 0750 /etc/cardrag/secrets
sudo install -o root -g cardrag -m 0640 \
  deploy/simple.env.example /etc/cardrag/worker.env
sudo install -o root -g cardrag -m 0640 \
  deploy/simple.env.example /etc/cardrag/mcp.env
```

두 env 파일은 서로 독립적입니다. Worker 값은 `worker.env`, MCP 값은
`mcp.env`에서 관리합니다.

### 4.2 secret 파일 만들기

먼저 권한이 제한된 빈 파일을 만듭니다.

```bash
sudo install -o root -g cardrag -m 0440 /dev/null \
  /etc/cardrag/secrets/webdav_username
sudo install -o root -g cardrag -m 0440 /dev/null \
  /etc/cardrag/secrets/webdav_password
sudo install -o root -g cardrag -m 0440 /dev/null \
  /etc/cardrag/secrets/openrouter_api_key
sudo install -o root -g cardrag -m 0440 /dev/null \
  /etc/cardrag/secrets/mcp_bearer_token
```

다음 세 파일에는 실제 값을 각각 한 줄로 입력합니다. 값을 명령행 인수에 직접
넣지 마십시오.

```bash
sudoedit /etc/cardrag/secrets/webdav_username
sudoedit /etc/cardrag/secrets/webdav_password
sudoedit /etc/cardrag/secrets/openrouter_api_key
```

MCP Bearer 토큰은 공백 없이 32자 이상이어야 합니다. 다음 명령은 64자리 hex
토큰을 생성합니다.

```bash
sudo sh -c 'openssl rand -hex 32 > /etc/cardrag/secrets/mcp_bearer_token'
```

편집 후 소유권, 권한, 읽기 가능 여부를 다시 확인합니다.

```bash
sudo chown root:cardrag /etc/cardrag/secrets/*
sudo chmod 0440 /etc/cardrag/secrets/*
sudo -u cardrag test -s /etc/cardrag/secrets/webdav_username
sudo -u cardrag test -s /etc/cardrag/secrets/webdav_password
sudo -u cardrag test -s /etc/cardrag/secrets/openrouter_api_key
sudo -u cardrag test -s /etc/cardrag/secrets/mcp_bearer_token
```

각 secret은 일반 UTF-8 파일이어야 하며 비어 있지 않은 값 한 줄만 포함해야
합니다. 저장소나 셸 기록에 실제 값을 남기지 마십시오.

### 4.3 Worker env 편집

```bash
sudoedit /etc/cardrag/worker.env
```

최소한 다음 값을 실제 환경에 맞게 확인하거나 수정합니다.

```dotenv
CARDRAG_ENVIRONMENT=production
CARDRAG_WORKER_IMAGE=ymtop59/mcp-card-prd-detail:1.0.4-worker
CARDRAG_ENABLED_ISSUERS=woori,kb

CARDRAG_WEBDAV_BASE_URL=https://YOUR_WEBDAV_HOST/cardrag
CARDRAG_WEBDAV_USERNAME_SECRET_FILE=/etc/cardrag/secrets/webdav_username
CARDRAG_WEBDAV_PASSWORD_SECRET_FILE=/etc/cardrag/secrets/webdav_password
CARDRAG_WEBDAV_CONNECT_TIMEOUT_SECONDS=10
CARDRAG_WEBDAV_TRANSFER_TIMEOUT_SECONDS=600

CARDRAG_OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
CARDRAG_OPENROUTER_API_KEY_SECRET_FILE=/etc/cardrag/secrets/openrouter_api_key
CARDRAG_EMBEDDING_MODEL=openai/text-embedding-3-small
CARDRAG_OCR_PROVIDER=codex-exec
CARDRAG_OCR_MODEL=gpt-5.6-sol
CARDRAG_OCR_REASONING_EFFORT=high
CARDRAG_OCR_PROVIDER_TIMEOUT_SECONDS=1800
CARDRAG_OCR_CACHE_EPOCH=0
CARDRAG_OCR_PROMPT_VERSION=cardrag-ocr.ko.v2
CARDRAG_OCR_RENDER_SCALE_MILLI=6000
CARDRAG_OCR_WHOLE_DOCUMENT_MAX_PAGES=4
CARDRAG_OCR_CHUNK_PAGES=2
CARDRAG_OCR_CONTEXT_PAGES_BEFORE=1
CARDRAG_OCR_CONTEXT_PAGES_AFTER=1
CARDRAG_STAGE_MAX_ATTEMPTS=4
CARDRAG_RETRY_CAP_SECONDS=30
```

OpenRouter 키는 OCR provider가 `codex-exec`이어도 임베딩 생성에 필요합니다.
이번 운영 계약에는 OCR fallback을 사용하지 않으므로
`CARDRAG_OCR_FALLBACK_PROVIDER`와 `CARDRAG_OCR_FALLBACK_MODEL`은 둘 다 설정하지
마십시오.
기본 OCR은 `gpt-5.6-sol`/`high`이며, 4페이지 이하는 상품 전체를 한 호출로 처리합니다.
더 긴 상품은 상품 전체 문맥을 제공하지 않고 2개 대상 페이지마다 인접한 앞뒤 1페이지만
문맥 전용 이미지로 함께 보내며, 대상 페이지 텍스트만 저장합니다. 모델, prompt, render
scale, 분할·문맥 값, cache epoch를
변경하면 OCR 재사용 계약이 달라지므로 운영 중 임의로 바꾸지 마십시오.

### 4.4 MCP env 편집

```bash
sudoedit /etc/cardrag/mcp.env
```

최소한 다음 값을 실제 환경에 맞게 확인하거나 수정합니다.

```dotenv
CARDRAG_ENVIRONMENT=production
CARDRAG_MCP_IMAGE=ymtop59/mcp-card-prd-detail:1.0.4-mcp

CARDRAG_WEBDAV_BASE_URL=https://YOUR_WEBDAV_HOST/cardrag
CARDRAG_WEBDAV_USERNAME_SECRET_FILE=/etc/cardrag/secrets/webdav_username
CARDRAG_WEBDAV_PASSWORD_SECRET_FILE=/etc/cardrag/secrets/webdav_password
CARDRAG_WEBDAV_CONNECT_TIMEOUT_SECONDS=10
CARDRAG_WEBDAV_TRANSFER_TIMEOUT_SECONDS=600

CARDRAG_OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
CARDRAG_OPENROUTER_API_KEY_SECRET_FILE=/etc/cardrag/secrets/openrouter_api_key
CARDRAG_MCP_BEARER_TOKEN_SECRET_FILE=/etc/cardrag/secrets/mcp_bearer_token
CARDRAG_MCP_BIND_ADDRESS=127.0.0.1
CARDRAG_MCP_PUBLISHED_PORT=8000
CARDRAG_MCP_PUBLIC_BASE_URL=https://YOUR_MCP_HOST
CARDRAG_MCP_UPDATE_INTERVAL_SECONDS=300
CARDRAG_MCP_MAX_VECTOR_BYTES=1073741824
CARDRAG_MCP_RETAIN_GENERATIONS=3
CARDRAG_MAXIMUM_CANDIDATE_COUNT=250
CARDRAG_MAXIMUM_PDF_BYTES=104857600
CARDRAG_EMBEDDING_TIMEOUT_SECONDS=60
```

`CARDRAG_MCP_PUBLIC_BASE_URL`은 PDF 설명에 표시할 실제 외부 HTTPS origin입니다.
MCP도 벡터 검색을 실행하므로 OpenRouter 키가 필요합니다.

### 4.5 secret 변수 규칙

env 파일에는 호스트 경로를 나타내는 `*_SECRET_FILE`만 설정합니다.
`compose.secrets.yaml`이 이를 컨테이너 내부의
`*_FILE=/run/secrets/...`로 바꿉니다.

다음과 같은 직접 값은 파일 기반 secret과 동시에 설정하지 마십시오.

```text
CARDRAG_WEBDAV_USERNAME
CARDRAG_WEBDAV_PASSWORD
CARDRAG_OPENROUTER_API_KEY
CARDRAG_MCP_BEARER_TOKEN
```

production 환경의 WebDAV와 OpenRouter URL은 HTTPS여야 합니다. URL에 사용자
정보, query string, fragment를 넣으면 설정 검증이 실패합니다.

### 4.6 사설 WebDAV CA가 있을 때만

공인 CA를 사용한다면 이 절은 건너뜁니다. 사설 CA 파일은 다음 위치와 권한으로
준비합니다.

```bash
sudo install -o root -g cardrag -m 0440 YOUR_CA_FILE.pem \
  /etc/cardrag/secrets/webdav_ca.pem
```

두 env 파일에 다음 값을 추가합니다.

```dotenv
CARDRAG_WEBDAV_CA_SECRET_FILE=/etc/cardrag/secrets/webdav_ca.pem
```

Worker systemd가 CA overlay를 사용하도록 `worker.env`에만 다음 줄도
추가합니다.

```dotenv
CARDRAG_WORKER_COMPOSE_OVERLAYS=--file deploy/worker/compose.ca.yaml
```

이후 수동 Worker 명령에는 `-f deploy/worker/compose.ca.yaml`을, MCP 명령에는
`-f deploy/mcp/compose.ca.yaml`을 secret overlay 뒤에 추가합니다.

## 5. 이미지와 Compose 설정 확인

v1.0.4 이미지를 미리 내려받습니다.

```bash
sudo docker pull ymtop59/mcp-card-prd-detail:1.0.4-worker
sudo docker pull ymtop59/mcp-card-prd-detail:1.0.4-mcp
```

Worker 설정을 실제 systemd 실행 사용자로 검증합니다.

```bash
sudo -u cardrag /usr/bin/docker compose \
  --env-file /etc/cardrag/worker.env \
  -f deploy/worker/compose.yaml \
  -f deploy/worker/compose.secrets.yaml \
  config --quiet
```

MCP 설정도 검증합니다.

```bash
sudo -u cardrag /usr/bin/docker compose \
  --env-file /etc/cardrag/mcp.env \
  -f deploy/mcp/compose.yaml \
  -f deploy/mcp/compose.secrets.yaml \
  config --quiet
```

두 명령 모두 출력 없이 종료 코드 0이면 Compose 변수와 overlay 구성이
유효합니다. 이 단계는 실제 WebDAV 자격증명까지 시험하지는 않습니다.

사설 CA를 사용한다면 각 명령의 `config --quiet` 앞에 해당 역할의 CA
overlay를 마지막 `-f`로 추가합니다.

## 6. Worker 컨테이너에서 Codex 로그인

기본 OCR provider는 `codex-exec`입니다. 첫 Worker 실행 전에 다음 명령을 한
번 실행합니다.

```bash
sudo -u cardrag /usr/bin/docker compose \
  --env-file /etc/cardrag/worker.env \
  -f deploy/worker/compose.yaml \
  run --rm --entrypoint codex worker login --device-auth
```

화면에 표시된 주소와 코드를 브라우저에서 승인합니다. 인증 정보는
`cardrag-worker` Compose 프로젝트의 `worker-state` 볼륨에 저장됩니다. 호스트의
다른 Codex 로그인은 이 인증을 대신하지 않습니다.

Worker 볼륨을 삭제했다면 이 로그인을 다시 해야 합니다.

## 7. WebDAV 사전검사

운영 데이터를 게시하기 전에 다음 검사를 반드시 통과해야 합니다.

```bash
sudo -u cardrag /usr/bin/docker compose \
  --env-file /etc/cardrag/worker.env \
  -f deploy/worker/compose.yaml \
  -f deploy/worker/compose.secrets.yaml \
  run --rm worker webdav-check
```

성공 결과에는 다음 내용이 포함됩니다.

```json
{
  "operations": [
    "MKCOL",
    "PROPFIND",
    "PUT",
    "GET",
    "HEAD",
    "MOVE",
    "MOVE_OVERWRITE_F_CONFLICT",
    "DELETE"
  ],
  "overwrite_false_conflict_status": 412,
  "reachable": true
}
```

`overwrite_false_conflict_status`는 WebDAV 구현에 따라 409 또는 412일 수
있습니다. 종료 코드가 0이고 `reachable`이 `true`이며 모든 작업 이름이 있어야
합니다. 검사는 고유 임시 경로만 만들고 정리합니다. 하나라도 실패하면 첫
Worker 게시를 진행하지 말고 WebDAV 권한, URL, TLS 인증서, 프록시의 WebDAV
지원 여부를 먼저 수정합니다.

## 8. 첫 Worker 세대 게시

### 8.1 systemd unit 설치

Worker service는 `/opt/cardrag-v1.0.4`와 `/etc/cardrag/worker.env`를 사용하도록 이미
정의되어 있습니다.

```bash
sudo install -o root -g root -m 0644 \
  deploy/worker/cardrag-worker.service /etc/systemd/system/
sudo install -o root -g root -m 0644 \
  deploy/worker/cardrag-worker.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

### 8.2 첫 실행

첫 실행은 카드사 PDF 수와 OCR 양에 따라 오래 걸릴 수 있습니다. 서비스를
비동기로 시작합니다.

```bash
sudo systemctl start --no-block cardrag-worker.service
```

다른 터미널에서 로그를 봅니다.

```bash
sudo journalctl -fu cardrag-worker.service -o cat
```

완료 후 `Ctrl+C`로 로그 보기를 끝내고 systemd 결과를 확인합니다.

```bash
systemctl show cardrag-worker.service -p Result -p ExecMainStatus
```

성공 기준은 `Result=success`, `ExecMainStatus=0`입니다. 서비스 로그의 마지막
Worker 결과는 다음 필드를 갖습니다.

```json
{
  "documents": 1,
  "evidence": 1,
  "gc_deleted": 0,
  "gc_error": null,
  "gc_status": "succeeded",
  "generation_id": "g-...",
  "run_id": "...",
  "status": "succeeded"
}
```

실제 문서와 근거 수는 달라집니다. 첫 게시의 정상 상태는 `succeeded`입니다.
WebDAV에 이미 완전히 같은 세대가 있으면 `no_change`도 정상입니다.
`generation_id`와 `run_id`가 있어야 하며 `gc_error`가 있으면 별도로
조사합니다.

`already_running`은 다른 Worker가 잠금을 보유한 상태입니다. 새 실행을
중복으로 시작하지 말고 기존 실행 로그를 확인합니다.

## 9. 매일 03:00 Worker 실행 활성화

첫 게시가 성공한 뒤 timer를 활성화합니다.

```bash
sudo systemctl enable --now cardrag-worker.timer
systemctl list-timers cardrag-worker.timer
```

목록의 다음 실행 시각이 03:00 Asia/Seoul인지 확인합니다. timer는 다음 값을
사용합니다.

```text
OnCalendar=*-*-* 03:00:00 Asia/Seoul
Persistent=true
```

서버가 예약 시각에 꺼져 있었다면 `Persistent=true` 때문에 다음 부팅 후 놓친
실행을 보충합니다. 로컬 파일 잠금은 동시에 두 Worker가 게시하는 것을
방지합니다.

## 10. MCP 시작

MCP는 첫 Worker 게시가 끝난 뒤 시작합니다. 먼저 시작하면 검증할 검색 세대가
없어 readiness가 503이 됩니다.

```bash
sudo /usr/bin/docker compose \
  --env-file /etc/cardrag/mcp.env \
  -f deploy/mcp/compose.yaml \
  -f deploy/mcp/compose.secrets.yaml \
  up -d --wait
```

`--wait`는 MCP가 WebDAV에서 완전한 세대를 내려받고 검증해 healthcheck가
통과할 때까지 기다립니다. 이어서 두 endpoint를 확인합니다.

```bash
curl --fail --silent --show-error http://127.0.0.1:8000/health/live
echo
curl --fail --silent --show-error http://127.0.0.1:8000/health/ready
echo
```

정상 출력은 다음과 같습니다.

```json
{"live":true}
{"ready":true}
```

Compose 상태도 확인합니다.

```bash
sudo /usr/bin/docker compose \
  --env-file /etc/cardrag/mcp.env \
  -f deploy/mcp/compose.yaml \
  -f deploy/mcp/compose.secrets.yaml \
  ps
```

MCP 컨테이너가 `healthy`여야 합니다.

`/health/live`와 `/health/ready`만 인증 없이 접근할 수 있습니다. `/mcp`,
`/resources/*`, `/sources/*`, `/metrics`는 다음 헤더가 없거나 토큰이 다르면
HTTP 401을 반환합니다.

```text
Authorization: Bearer <MCP bearer token>
```

Compose는 컨테이너 내부에서만 `0.0.0.0:8000`을 사용하고 호스트에는 기본적으로
`127.0.0.1:8000`으로 게시합니다. 인터넷에 연결된 호스트에서
`CARDRAG_MCP_BIND_ADDRESS=0.0.0.0`으로 바꾸지 마십시오. TLS 리버스 프록시는
Authorization 헤더와 PDF의 `Range` 헤더를 MCP에 전달해야 합니다.

## 11. 평상시 상태 확인

### Worker

timer 상태와 다음 실행 시각을 확인합니다.

```bash
systemctl status cardrag-worker.timer --no-pager
systemctl list-timers cardrag-worker.timer
```

오늘 실행 로그와 마지막 종료 상태를 확인합니다.

```bash
sudo journalctl -u cardrag-worker.service -o cat --since today
systemctl show cardrag-worker.service -p Result -p ExecMainStatus
```

정상 Worker 결과는 `status`가 `succeeded` 또는 `no_change`이고,
`gc_status`가 `succeeded`이며 `gc_error`가 `null`입니다.

### MCP

```bash
sudo /usr/bin/docker compose \
  --env-file /etc/cardrag/mcp.env \
  -f deploy/mcp/compose.yaml \
  -f deploy/mcp/compose.secrets.yaml \
  ps

curl --fail http://127.0.0.1:8000/health/live
curl --fail http://127.0.0.1:8000/health/ready
```

최근 MCP 로그는 다음 명령으로 확인합니다.

```bash
sudo /usr/bin/docker compose \
  --env-file /etc/cardrag/mcp.env \
  -f deploy/mcp/compose.yaml \
  -f deploy/mcp/compose.secrets.yaml \
  logs --tail=200 mcp
```

Docker healthcheck가 실패해도 Docker는 그 이유만으로 컨테이너를 자동
재시작하지 않습니다. `live`는 200인데 `ready`가 503이면 프로세스는 살아
있지만 검증된 로컬 세대를 서비스하지 못하는 상태입니다.

## 12. 복구

### 12.1 Worker 실행 실패

먼저 로그에서 원인과 `run_id`를 확인합니다.

```bash
sudo journalctl -u cardrag-worker.service -o cat --since today
```

env, secret, Codex 인증, 카드사 연결, OpenRouter, WebDAV 문제를 해결한 뒤 실패한
실행 ID를 그대로 재개합니다.

단, 이 v1.0.4 OCR 계약 마이그레이션에서는 v1.0.4 이전 Worker가 만들었거나 이전
OCR 계약으로 실패한 실행을 절대 `resume`하지 않습니다. 그런 과거 `run_id`는
폐기 대상으로만 남겨 두고, v1.0.4 이미지와 설정을 검증한 뒤 반드시 새 실행을
시작합니다. 아래 `resume` 절차는 마이그레이션 완료 후 v1.0.4가 새로 만든 실행에만
사용할 수 있습니다.

```bash
RUN_ID=로그에서_확인한_run_id
sudo -u cardrag /usr/bin/docker compose \
  --env-file /etc/cardrag/worker.env \
  -f deploy/worker/compose.yaml \
  -f deploy/worker/compose.secrets.yaml \
  run --rm worker resume "$RUN_ID"
```

재개는 카드사 검색과 현재 PDF를 다시 확인합니다. 완료된 OCR과 로컬 chunk
checkpoint는 재사용합니다. 설정 단계에서 실패해 `run_id`가 만들어지지 않았다면
문제를 수정한 뒤 새 실행을 시작합니다.

```bash
sudo systemctl start cardrag-worker.service
```

Codex 인증이 없거나 Worker 상태 볼륨을 새로 만든 경우에는 6장의 device login을
다시 실행합니다.

### 12.2 MCP가 ready가 아님

다음 순서로 확인합니다.

1. `/health/live`가 200인지 확인합니다.
2. 첫 Worker 게시가 `succeeded`인지 확인합니다.
3. MCP 로그에서 WebDAV, secret, CA, 다운로드 해시 오류를 확인합니다.
4. env 또는 secret을 수정했다면 컨테이너를 다시 만듭니다.

```bash
sudo /usr/bin/docker compose \
  --env-file /etc/cardrag/mcp.env \
  -f deploy/mcp/compose.yaml \
  -f deploy/mcp/compose.secrets.yaml \
  up -d --force-recreate --wait
```

새 WebDAV poll이 실패하거나 세대가 손상되었거나 호환되지 않으면 MCP는 기존에
검증된 로컬 세대를 계속 활성 상태로 둡니다. 첫 세대가 아직 없다면
`/health/ready`는 문제가 해결될 때까지 503을 반환합니다.

### 12.3 지우면 안 되는 볼륨

다음 명령은 사용하지 마십시오.

```text
docker compose down -v
```

Worker 볼륨에는 Codex 인증과 재개 checkpoint가 있고, MCP 볼륨에는 검증된 로컬
세대가 있습니다. 일반적인 재시작에는 `-v`가 필요하지 않습니다.

## 13. 이미지 업그레이드

v1.0.4는 Worker 게시 형식을 `cardrag.generation.v3`와
`cardrag.serving-db.v3`로 올립니다. v1.0.4 MCP는 v2와 v3를 모두 읽지만 v1.0.2
MCP는 v3를 읽지 못합니다. 따라서 v1.0.2에서 올릴 때의 순서는 반드시
**MCP v1.0.4 먼저, Worker v1.0.4 나중**입니다. 순서를 바꾸지 마십시오.

이 업그레이드는 v3 세대가 `stable.json`에 게시되는 순간부터 MCP 바이너리에
대해서는 forward-only 경계입니다. v3 게시 전에는 MCP 이미지를 v1.0.2로 되돌릴
수 있습니다. 게시 후 v1.0.2 MCP로 내리면 v3 manifest와 SQLite를 읽지 못합니다.
로컬 포인터가 이미 v3이면 readiness가 503이 되고, 우연히 로컬 v2가 남아 200을
반환하더라도 원격 v3 갱신은 계속 실패하므로 지원되는 복구가 아닙니다. v3 게시
후에는 상태 볼륨이나 WebDAV 포인터를 수동으로 바꾸지 말고, v1.0.4 MCP의 마지막
정상 세대 유지 기능을 사용한 채 같은 스키마를 읽는 수정 버전으로 전진
복구하십시오. 기존 `/opt/cardrag` v1.0.2 보존본은 이 시점 이후 데이터 rollback 수단이
아니라 파일 감사·재설치 참고본입니다.

작업 중 예약 Worker가 먼저 실행되지 않도록 timer를 멈추고, 실행 중인 Worker가
없음을 확인합니다. 진행 중인 Worker가 있으면 완료될 때까지 기다립니다.

```bash
sudo systemctl stop cardrag-worker.timer
sudo systemctl status cardrag-worker.service --no-pager
```

새 배포 파일은 기존 `/opt/cardrag`을 덮어쓰지 말고 immutable
`/opt/cardrag-v1.0.4`에 설치한 뒤 두 고정 이미지를 미리 내려받습니다. `latest`
태그는 사용하지 않습니다.

```bash
sudo docker pull ymtop59/mcp-card-prd-detail:1.0.4-mcp
sudo docker pull ymtop59/mcp-card-prd-detail:1.0.4-worker
```

먼저 `/etc/cardrag/mcp.env`의 MCP 이미지만 다음 값으로 바꿉니다. 아직
`worker.env`는 바꾸지 않습니다.

```dotenv
CARDRAG_MCP_IMAGE=ymtop59/mcp-card-prd-detail:1.0.4-mcp
```

MCP Compose 설정을 검증하고 교체합니다.

```bash
sudo -u cardrag /usr/bin/docker compose \
  --env-file /etc/cardrag/mcp.env \
  -f deploy/mcp/compose.yaml \
  -f deploy/mcp/compose.secrets.yaml \
  config --quiet

sudo /usr/bin/docker compose \
  --env-file /etc/cardrag/mcp.env \
  -f deploy/mcp/compose.yaml \
  -f deploy/mcp/compose.secrets.yaml \
  up -d --wait

curl --fail http://127.0.0.1:8000/health/live
curl --fail http://127.0.0.1:8000/health/ready
```

이 시점의 v1.0.4 MCP는 WebDAV와 로컬에 있던 v2 세대를 그대로 서비스해야
합니다. readiness가 200이 아니면 Worker를 교체하지 말고 MCP 로그를 먼저
확인합니다.

MCP 확인이 끝난 뒤 `/etc/cardrag/worker.env`를 다음과 같이 바꿉니다. 신한카드는
공식 PDF 응답 복구가 별도로 검증될 때까지 넣지 않습니다.

```dotenv
CARDRAG_WORKER_IMAGE=ymtop59/mcp-card-prd-detail:1.0.4-worker
CARDRAG_ENABLED_ISSUERS=woori,kb
```

새 배포 파일의 systemd unit도 다시 설치합니다.

```bash
sudo install -o root -g root -m 0644 \
  deploy/worker/cardrag-worker.service /etc/systemd/system/
sudo install -o root -g root -m 0644 \
  deploy/worker/cardrag-worker.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

Worker Compose와 WebDAV 기능을 다시 검증한 뒤 v3 Worker를 한 번 실행합니다.

```bash
sudo -u cardrag /usr/bin/docker compose \
  --env-file /etc/cardrag/worker.env \
  -f deploy/worker/compose.yaml \
  -f deploy/worker/compose.secrets.yaml \
  config --quiet

sudo -u cardrag /usr/bin/docker compose \
  --env-file /etc/cardrag/worker.env \
  -f deploy/worker/compose.yaml \
  -f deploy/worker/compose.secrets.yaml \
  run --rm worker webdav-check

sudo systemctl start cardrag-worker.service
sudo journalctl -u cardrag-worker.service -o cat --since today
```

Worker 결과가 `succeeded` 또는 `no_change`인지 확인합니다. 새 v3 세대가 게시된
경우 MCP가 다음 poll에서 이를 검증하고 활성화합니다.

```bash
curl --fail http://127.0.0.1:8000/health/live
curl --fail http://127.0.0.1:8000/health/ready
sudo journalctl -u cardrag-worker.service -o cat --since today
sudo systemctl enable --now cardrag-worker.timer
systemctl list-timers cardrag-worker.timer
```

모든 확인이 끝날 때까지 Worker와 MCP 상태 볼륨을 유지합니다. 새 WebDAV 세대가
손상되거나 호환되지 않으면 v1.0.4 MCP는 마지막 정상 v2 또는 v3 세대를 계속
제공합니다. 실패 시 볼륨을 삭제하지 말고 로그와 manifest를 확인하십시오.

## 14. 자주 하는 실수

- `/opt/cardrag-v1.0.4`가 아닌 위치에 배포해 Worker service가 파일을 찾지 못함
- 운영 이미지 변수를 설정하지 않아 로컬 이미지 이름을 사용함
- `*_SECRET_FILE`과 직접 secret 값을 동시에 설정함
- secret 파일이 비어 있거나 UID/GID 10001에서 읽히지 않음
- MCP Bearer 토큰이 32자 미만이거나 공백을 포함함
- HTTP WebDAV 또는 URL 안의 사용자 정보, query, fragment를 사용함
- Worker 컨테이너가 아닌 호스트에서만 Codex 로그인함
- `webdav-check` 실패를 무시하고 첫 게시를 시작함
- 첫 Worker 게시 전에 MCP를 시작함
- `/health/live`만 보고 검색 준비가 끝났다고 판단함
- MCP 포트를 `0.0.0.0`으로 인터넷에 직접 노출함
- 사설 CA overlay를 마지막에 추가하지 않음
- `docker compose down -v`로 영속 상태를 삭제함
