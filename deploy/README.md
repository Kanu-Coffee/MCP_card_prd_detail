# CardRAG v1.0.3 배포 파일

운영 배포는 Worker와 MCP 두 Compose 프로젝트만 사용합니다.

```text
deploy/
├── simple.env.example
├── worker/
│   ├── compose.yaml
│   ├── compose.secrets.yaml
│   ├── compose.ca.yaml
│   ├── cardrag-worker.service
│   └── cardrag-worker.timer
└── mcp/
    ├── compose.yaml
    ├── compose.secrets.yaml
    └── compose.ca.yaml
```

## 각 파일의 역할

- [환경변수 예제](simple.env.example)는 Worker와 MCP env 파일의 출발점입니다.
- [Worker Compose](worker/compose.yaml)는 한 번 실행되고 종료되는 Worker와
  영속 상태 볼륨을 정의합니다.
- [MCP Compose](mcp/compose.yaml)는 항상 실행되는 MCP, healthcheck, 로컬 상태
  볼륨, 호스트 loopback 포트를 정의합니다.
- 각 역할의 `compose.secrets.yaml`은 호스트 `*_SECRET_FILE`을 컨테이너의
  `/run/secrets/*`로 연결합니다.
- 각 역할의 `compose.ca.yaml`은 사설 WebDAV CA가 있을 때만 마지막 overlay로
  추가합니다.
- Worker service와 timer는 `/opt/cardrag`에서 Worker를 매일 03:00
  Asia/Seoul에 실행합니다.

## 운영 이미지

`/etc/cardrag/worker.env`와 `/etc/cardrag/mcp.env`에 다음 태그를 각각
지정합니다.

```dotenv
CARDRAG_WORKER_IMAGE=ymtop59/mcp-card-prd-detail:1.0.3-worker
CARDRAG_MCP_IMAGE=ymtop59/mcp-card-prd-detail:1.0.3-mcp
```

운영에서 기본값인 `cardrag-worker:local`, `cardrag-mcp:local`에 의존하지
마십시오.

v1.0.2에서 올릴 때는 MCP 이미지만 먼저 v1.0.3으로 바꾸고 기존 v2 세대를
정상 제공하는지 확인한 뒤 Worker 이미지를 v1.0.3으로 바꿉니다. Worker를 먼저
실행하면 v1.0.2 MCP는 새 v3 세대를 활성화할 수 없습니다.

## Compose 사용 원칙

Worker 명령에는 항상 Worker base와 secret overlay를 함께 사용합니다.

```bash
docker compose --env-file /etc/cardrag/worker.env \
  -f deploy/worker/compose.yaml \
  -f deploy/worker/compose.secrets.yaml \
  run --rm worker run
```

MCP는 별도 프로젝트로 시작합니다.

```bash
docker compose --env-file /etc/cardrag/mcp.env \
  -f deploy/mcp/compose.yaml \
  -f deploy/mcp/compose.secrets.yaml \
  up -d --wait
```

사설 CA를 사용할 때만 해당 역할의 `compose.ca.yaml`을
`compose.secrets.yaml` 뒤, 동작 명령 앞에 추가합니다. Worker systemd에도
적용하려면 `/etc/cardrag/worker.env`에 다음 값을 넣습니다.

```dotenv
CARDRAG_WORKER_COMPOSE_OVERLAYS=--file deploy/worker/compose.ca.yaml
```

MCP의 기본 호스트 바인딩은 `127.0.0.1:8000`입니다. 인터넷에 직접 노출하지
말고 TLS 리버스 프록시를 사용하십시오.

전체 설치, 검증, 상태 확인, 복구, 업그레이드는
[실운영 가이드](../docs/SIMPLE_RUNTIME.md)를 따릅니다.
