# 설치와 운영

## 구성과 준비물

Worker는 PDF 수집·OCR·임베딩·게시를 마치고 종료하며, MCP는 검증된 generation을
계속 서비스합니다. 같은 호스트에 설치해도 상태 디렉터리와 Compose 프로젝트를 분리합니다.
스케줄러, TLS 프록시, WebDAV 서버와 MCP 클라이언트는 운영자가 준비합니다.

- Linux amd64, Docker Engine, Docker Compose 2.24.4 이상
- `HEAD`, `GET`, `PUT`, `MKCOL`, `MOVE`, `PROPFIND` 등 게시 프로토콜을 지원하는 HTTPS WebDAV
- OCR provider용 Codex 인증과 OpenRouter 임베딩 API 키
- 고유 MCP Bearer token, 공개 접속 시 HTTPS 프록시

MCP에는 가능하면 읽기 전용 WebDAV 계정을 사용합니다. Worker의 `webdav-check`는
시험 객체를 쓰고 확인하는 외부 작업이므로 대상 저장소를 확인한 뒤 실행합니다.
디스크에는 Worker와 MCP가 각각 보관하는 PDF·DB·vector 복사본과 임시 공간을 모두 계산합니다.

## 새 설치의 설정

아래 `/opt/cardrag`, `/etc/cardrag`는 예시 경로입니다. 환경에 맞게 선택하고, 인증 파일은
저장소 밖에서 관리하십시오. 기존 설치의 업그레이드는 [RELEASING](RELEASING.md)을 따릅니다.

소스를 설치할 디렉터리에서 역할별 설정을 준비합니다.

```bash
sudo install -d -m 0750 /etc/cardrag /etc/cardrag/secrets
sudo cp deploy/simple.env.example /etc/cardrag/worker.env
sudo cp deploy/simple.env.example /etc/cardrag/mcp.env
sudo chmod 0600 /etc/cardrag/worker.env /etc/cardrag/mcp.env
```

호스트의 Docker 실행 계정이 env 파일을 읽을 수 있고 컨테이너 UID `10001:10001`이
마운트된 비밀 파일을 읽을 수 있도록 소유권·권한을 설정합니다. 비밀 값을 명령줄 인자로
전달하거나 저장소에 기록하지 않습니다. 파일은 줄 하나의 값으로 준비합니다.

| 설정 | Worker | MCP |
|---|---|---|
| `CARDRAG_WEBDAV_BASE_URL` | 사용할 HTTPS root | 같은 데이터 root |
| `CARDRAG_WEBDAV_USERNAME_SECRET_FILE` / `PASSWORD_SECRET_FILE` | 게시 계정 파일 | 조회 계정 파일 |
| `CARDRAG_OPENROUTER_API_KEY_SECRET_FILE` | 문서 임베딩 | 질의 임베딩 |
| `CARDRAG_MCP_BEARER_TOKEN_SECRET_FILE` | 사용하지 않음 | 접속 인증 파일 |
| `CARDRAG_MCP_PUBLIC_BASE_URL` | 사용하지 않음 | 사용자가 접근하는 HTTPS origin |
| `CARDRAG_ENABLED_ISSUERS` | 쉼표로 구분한 8개 canonical 코드 중 선택 | 사용하지 않음 |

MCP의 조회 범위는 도구의 `issuer`·`issuers` 인자로 선택합니다. `mcp.env`의
`CARDRAG_ENABLED_ISSUERS`로 서버의 노출 범위를 제한할 수는 없습니다.

Compose 설정의 `*_SECRET_FILE`은 **호스트 파일 경로**입니다. 비밀 overlay가 이를
`/run/secrets/*`로 마운트하고 애플리케이션의 `*_FILE`에 연결합니다. 소스를 직접 실행할
때는 [`.env.example`](../.env.example)의 애플리케이션 설정을 참고하십시오. 이 파일은
자동 로그인이나 실제 인증을 제공하지 않습니다.

OCR 기본값은 `codex-exec`, `gpt-5.6-sol`, reasoning `high`입니다. Qwen 임베딩의 모델,
4,096차원, tokenizer와 provider profile은 generation identity에 묶이므로 기존 vector와
다른 모델을 섞지 않습니다. API의 제공 모델·권한은 해당 계정에서 확인합니다.

선택한 호스트와 경로로 env 예시를 수정한 후, 출력에 비밀이 포함되지 않는 설정 검사를
수행합니다. `config --quiet`는 실제 자격증명이나 원격 저장소까지 검증하지 않습니다.

```bash
docker compose --env-file /etc/cardrag/worker.env \
  -f deploy/worker/compose.yaml -f deploy/worker/compose.secrets.yaml config --quiet
docker compose --env-file /etc/cardrag/mcp.env \
  -f deploy/mcp/compose.yaml -f deploy/mcp/compose.secrets.yaml config --quiet
```

사설 WebDAV CA가 있으면 역할별 `compose.ca.yaml`을 마지막 overlay로 추가하고
`CARDRAG_WEBDAV_CA_SECRET_FILE`에 호스트 인증서 경로를 지정합니다. TLS 검증을 끄지 않습니다.

## 이미지와 초기 인증

검증된 release digest가 있다면 `CARDRAG_WORKER_IMAGE`와 `CARDRAG_MCP_IMAGE`에 각각
고정하십시오. 소스 빌드에는 아래 명령을 사용합니다.

```bash
docker compose --env-file /etc/cardrag/worker.env -f deploy/worker/compose.yaml build worker
docker compose --env-file /etc/cardrag/mcp.env -f deploy/mcp/compose.yaml build mcp
```

처음 사용하는 **새 Codex 인증 볼륨**에서 로그인합니다.

```bash
docker compose --env-file /etc/cardrag/worker.env -f deploy/worker/compose.yaml \
  run --rm --entrypoint codex worker login --device-auth
```

인증은 Worker 상태가 아닌 별도의 `codex-home` 볼륨에 저장됩니다. 다른 호스트 프로세스의
로그인을 자동으로 공유하지 않습니다. 기본 신규 볼륨은 다음과 같습니다.

| 역할 | 신규 기본 볼륨 |
|---|---|
| Worker 상태 | `cardrag-worker-v122-state` |
| Codex 인증 | `cardrag-worker-v122-codex-home` |
| MCP 상태 | `cardrag-mcp-v122-state` |

`CARDRAG_WORKER_STATE_VOLUME`, `CARDRAG_WORKER_CODEX_HOME_VOLUME`,
`CARDRAG_MCP_STATE_VOLUME`으로 명시할 수 있습니다. 기존 설치에 새 기본값을 적용하면
새 빈 볼륨이 선택될 수 있습니다. 운영 중인 볼륨의 이름과 사용 프로세스를 먼저 확인하십시오.

## 실행

운영자가 새 설치의 stable 게시를 허용하기로 결정한 시점에만 **worker.env**의
`CARDRAG_STABLE_PUBLICATION_APPROVED=true`를 설정합니다. 기본값은 false여서
실수로 stable 포인터를 바꾸지 않습니다. 공유 OCR cache 쓰기와 GC는 별도 권한입니다.

```bash
docker compose --env-file /etc/cardrag/worker.env \
  -f deploy/worker/compose.yaml -f deploy/worker/compose.secrets.yaml \
  run --rm worker webdav-check
docker compose --env-file /etc/cardrag/worker.env \
  -f deploy/worker/compose.yaml -f deploy/worker/compose.secrets.yaml \
  run --rm worker run
```

Worker의 종료 코드와 terminal 결과, 검증된 게시 결과를 확인한 뒤 MCP를 실행합니다.

```bash
docker compose --env-file /etc/cardrag/mcp.env \
  -f deploy/mcp/compose.yaml -f deploy/mcp/compose.secrets.yaml up -d --no-deps mcp
curl --fail http://127.0.0.1:8000/health/live
curl --fail http://127.0.0.1:8000/health/ready
```

`live`는 프로세스 생존, `ready`는 유효한 generation 제공 가능 여부입니다. 아직 게시된
데이터가 없으면 ready는 503입니다. 포트가 다르면 health URL도 맞춰야 합니다.
MCP 외부 주소는 TLS 프록시를 통해 연결하고 `/mcp`에 Bearer token을 전달합니다.
클라이언트별 조사 템플릿과 예약·전송 이력은 CardRAG 저장소와 분리하십시오.

## 예약과 상태 확인

systemd 템플릿은 선택 사항입니다. 설치 경로, 실행 계정, env 소유권을 확인한 뒤
`deploy/worker/cardrag-worker.service`와 `.timer`를 설치합니다. 기본 예약은 한국 시간
03:00이며 필요에 맞게 변경하십시오. 기존 실행의 전환 중에는 예약을 즉시 활성화하지 않습니다.

```bash
systemctl status cardrag-worker.service cardrag-worker.timer
journalctl -u cardrag-worker.service -n 100 --no-pager
```

외부 모니터가 live `worker-state.sqlite3`를 열면 안 됩니다. Worker는 단일 writer lock을
보유합니다. 기존 실행이 진행 중일 때 추가 실행의 예약 실패는 기존 writer의 실패를
뜻하지 않습니다. 로그와 실제 컨테이너 상태를 함께 확인하십시오.

SIGTERM 이후 Worker는 진행 중인 변경을 정리하고 게시 정합성을 확인한 뒤 lock을
해제합니다. `TimeoutStopSec=infinity`, `SendSIGKILL=no`는 이 수명주기를 보존합니다.
이미지·설치 symlink·상태·인증·예약 변경은 실행이 자연 종료한 후 별도 전환 절차로 수행합니다.

## 주요 한도와 외부 변경 권한

| 설정 | 기본값과 의미 |
|---|---|
| Worker/MCP `MAX_STATE_BYTES` | 역할별 128 GiB |
| Worker/MCP `RESERVED_FREE_SPACE_BYTES` | 역할별 2 GiB |
| `WORKER_MINIMUM_START_FREE_BYTES` | 2 GiB, 후보 overlay는 32 GiB |
| Worker/MCP `MAX_VECTOR_SIDECAR_BYTES` | 16 GiB |
| Worker/MCP `MAX_SERVING_DATABASE_BYTES` | 32 GiB |
| `MCP_MAX_GENERATION_DOWNLOAD_BYTES` | 64 GiB |
| `MCP_MAX_RESIDENT_VECTOR_BYTES` | 1 GiB |
| `PDF_CONCURRENCY` / `PDF_CONCURRENCY_PER_ISSUER` | 전체 8 / 카드사별 2 |
| `LOCAL_PROCESSING_WORKERS` | 4 |
| `PDF_CACHE_REFRESH_HOURS` | 168시간 |

표의 설정명에는 `CARDRAG_` 접두사가 붙습니다. 용량은 여유 공간의 자동 확보를 뜻하지
않으며, 부족하면 작업을 거부합니다. MCP의 지속 quota 정책을 바꾸거나 남은 reservation을
정리할 때는 [RECOVERY](RECOVERY.md)를 따릅니다.

WebDAV는 검증 이력이 있는 기존 객체를 재사용하고 성공 실행 14회·7일·신규 CAS 10 GiB 중
먼저 도달한 조건에서 전체 검증합니다. 신규 DB·vector는 최종 경로에서 크기·해시를
검증합니다. `CARDRAG_WEBDAV_FORCE_FULL_VERIFY=true`는 강제 검증,
`CARDRAG_WEBDAV_VERIFICATION_MODE=strict`와 `GENERATION_READBACK_MODE=double`은
항상 전체 검증과 이중 readback을 선택합니다. 후자에도 `CARDRAG_WEBDAV_` 접두사가 붙습니다.

`CARDRAG_OCR_CACHE_MODE=read-only`가 기본입니다. 공유 cache 쓰기는 stable 게시 권한과
별도로 `CARDRAG_OCR_CACHE_PUBLICATION_APPROVED=true` 및 `read-write`를 요구합니다.
원격 삭제는 `CARDRAG_REMOTE_GC_APPROVED=true`, `CARDRAG_COLLECT_REMOTE_GARBAGE=true`,
stable 게시 권한이 모두 있어야 수행합니다. 설정 예시는 세 권한을 기본으로 끕니다.

인증된 `/metrics`에서 처리 결과·시간·응답 크기·캐시 사용량을 확인합니다. 질의문,
상품명과 사용자는 metric label에 넣지 않습니다. 상세 실패 원문과 운영 증빙은 접근을
제한한 별도 위치에 보관하십시오.
