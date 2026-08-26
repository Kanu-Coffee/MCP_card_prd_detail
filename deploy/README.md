# CardRAG v1.0.8 배포 파일

운영 배포는 Worker와 MCP 두 Compose 프로젝트만 사용합니다.

```text
deploy/
├── simple.env.example
├── worker/
│   ├── compose.yaml
│   ├── compose.secrets.yaml
│   ├── compose.ca.yaml
│   ├── compose.adoption.yaml
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
- Worker의 `compose.adoption.yaml`은 sealed legacy OCR export와 그 export에
  기록된 원본 source root를 컨테이너의 같은 절대경로에 read-only로 연결합니다.
  평상시 Worker 실행에는 추가하지 않습니다.
- Worker service와 timer는 immutable `/opt/cardrag-v1.0.8`에서 Worker를 매일 03:00
  Asia/Seoul에 실행합니다.

`/opt/cardrag-v1.0.8`은 운영 계정이 소유해도 됩니다. 런타임은 특정 소유자를
요구하지 않지만, 설치가 끝난 트리의 모든 쓰기 비트는 제거해야 합니다. 운영 env와
secret은 이 트리에 두지 않고 `/etc/cardrag`에서만 주입합니다.

## 운영 이미지

`/etc/cardrag/worker.env`와 `/etc/cardrag/mcp.env`에 다음 태그를 각각
지정합니다.

```dotenv
CARDRAG_WORKER_IMAGE=ymtop59/mcp-card-prd-detail:1.0.8-worker
CARDRAG_MCP_IMAGE=ymtop59/mcp-card-prd-detail:1.0.8-mcp
```

운영에서 기본값인 `cardrag-worker:local`, `cardrag-mcp:local`에 의존하지
마십시오.

v1.0.2에서 올릴 때는 MCP 이미지만 먼저 v1.0.4 이상으로 바꾸고 기존 v2 세대를
정상 제공하는지 확인한 뒤 Worker 이미지를 v1.0.4 이상으로 바꿉니다. Worker를 먼저
실행하면 v1.0.2 MCP는 새 v3 세대를 활성화할 수 없습니다.

v1.0.7에서 여러 줄의 짧은 가시 요소가 있는 정상 희소 페이지를 추가로 허용했고,
v1.0.8도 v1.0.4 native OCR processor contract를 그대로 사용합니다. v1.0.8 Worker는
한 문서가 OCR 재시도를 모두 소진하면 길이가 제한된 안전한 실패 이유를 기록하고
나머지 PDF를 계속 처리합니다. 완료·검증된 문서별 native cache는 보존하지만, 실패가
하나라도 남으면 임베딩과 generation seal 및 `stable.json` 게시 전에 배치를 실패시켜
부분 세대가 공개되지 않게 합니다. v1.0.4부터 v1.0.7까지 게시된 완료 OCR은 v1.0.8
새 실행에서 재사용하며 이전 실패 실행 ID 자체를 재개하지 않습니다.
HTTP 상태로 명확히 분류되는 인증·설정 응답, 직접 연결 실패, 로컬 파일·상태 DB·코드
불변식 오류는 문서 오류로 격리하지 않고 첫 발생에서 즉시 전체 실행을 중단합니다.

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
