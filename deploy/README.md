# CardRAG 배포 파일

현재 보호 대상 운영은 v1.0.9이며, v1.0.10은 별도 후보 overlay로 검증합니다. 두
버전은 같은 Compose project·상태 volume·WebDAV base URL·채널 포인터·호스트
포트를 공유하지 않습니다.

```text
deploy/
├── simple.env.example
├── worker/
│   ├── compose.yaml
│   ├── compose.candidate.yaml
│   ├── compose.cache-seed.yaml
│   ├── compose.secrets.yaml
│   ├── compose.ca.yaml
│   ├── compose.adoption.yaml
│   ├── cardrag-worker.service
│   └── cardrag-worker.timer
└── mcp/
    ├── compose.yaml
    ├── compose.candidate.yaml
    ├── compose.secrets.yaml
    └── compose.ca.yaml
```

## 각 파일의 역할

- `simple.env.example`은 Worker와 MCP env 파일의 출발점입니다.
- 각 `compose.yaml`은 한 번 실행되는 Worker 또는 항상 실행되는 MCP를
  정의합니다. 상태 volume 이름은 `CARDRAG_WORKER_STATE_VOLUME`과
  `CARDRAG_MCP_STATE_VOLUME`로 명시할 수 있습니다. Worker base의 기본 목적지는
  v1.0.10 전용 `cardrag-worker-v110-state`이며 v1.0.9 운영 volume은 seed overlay의
  read-only source로만 연결합니다.
- `compose.candidate.yaml`은 `candidate-v1.0.10` 포인터, 후보 전용 volume 및
  WebDAV URL을 강제합니다. Worker 후보는 원격 GC를 하지 않고 MCP 후보는 기본
  `127.0.0.1:18010`만 사용합니다. MCP overlay의 안전한 port 교체에는 Docker
  Compose v2.24.4 이상이 필요합니다.
- Worker `compose.cache-seed.yaml`은 terminal 상태가 확인된 v1.0.9 Worker volume을
  `/mnt/cardrag-v109-state`에 read-only로 연결합니다. 평상시 Worker에는
  추가하지 않습니다.
- 각 `compose.secrets.yaml`은 host secret 파일을 `/run/secrets/*`에
  read-only로 연결합니다.
- 각 `compose.ca.yaml`은 사설 WebDAV CA가 필요할 때만 마지막 overlay로
  추가합니다.
- `compose.adoption.yaml`은 sealed legacy OCR export를 read-only로 연결할
  때만 사용합니다.

## v1.0.10 후보 실행

후보 Worker와 MCP에는 서로 같은 후보 WebDAV base URL을 설정합니다.

```dotenv
CARDRAG_CANDIDATE_WEBDAV_BASE_URL=https://webdav.example/cardrag-v110-candidate
CARDRAG_CANDIDATE_MCP_PUBLIC_BASE_URL=https://candidate-cardrag.example
CARDRAG_ENABLED_ISSUERS=woori,kb,shinhan,samsung
CARDRAG_EMBEDDING_PROVIDER_ID=deepinfra
CARDRAG_WORKER_MAX_STATE_BYTES=68719476736
CARDRAG_WORKER_RESERVED_FREE_SPACE_BYTES=2147483648
CARDRAG_WORKER_MAX_VECTOR_SIDECAR_BYTES=17179869184
CARDRAG_WORKER_MAX_SERVING_DATABASE_BYTES=4294967296
# Candidate startup floor: 32 GiB. Set it explicitly when copying the shared env example.
CARDRAG_WORKER_MINIMUM_START_FREE_BYTES=34359738368
# Off by default. Enable only in the candidate MCP env after provider preflight.
CARDRAG_RERANKER_SHADOW_ENABLED=false
```

```bash
docker compose --env-file /etc/cardrag/candidate-worker.env \
  -f deploy/worker/compose.yaml \
  -f deploy/worker/compose.candidate.yaml \
  -f deploy/worker/compose.secrets.yaml \
  run --rm worker run

docker compose --env-file /etc/cardrag/candidate-mcp.env \
  -f deploy/mcp/compose.yaml \
  -f deploy/mcp/compose.candidate.yaml \
  -f deploy/mcp/compose.secrets.yaml \
  up -d --wait
```

운영 stable channel이나 v1.0.9 volume을 후보의 RW base/overlay에 지정하지
마십시오. seed overlay도 운영 Worker가 terminal인 것을 확인한 뒤에만 사용합니다.
reranker shadow를 시험할 때는 candidate MCP env에서만
`CARDRAG_RERANKER_SHADOW_ENABLED=true`로 바꾸며, stable은 Settings 단계에서 이를
거부합니다. shadow artifact는 candidate MCP volume에만 기록되고 primary 검색 순위에는
적용되지 않습니다.
candidate Worker는 `CARDRAG_OCR_CACHE_MODE=read-only`와
`CARDRAG_OCR_CACHE_PUBLICATION_APPROVED=false`를 강제합니다. 검증된 remote OCR cache
hit는 GET으로 재사용하지만 native/adopted cache manifest와 READY를 생성·repair하지
않습니다. 이 별도 approval은 stable pointer 전환이나 remote GC approval로 대체할 수
없습니다.
`CARDRAG_PDF_CACHE_REFRESH_HOURS`는 양의 유한 시간만 허용하며 기본값 168시간입니다.
그 주기 전에는 검증된 로컬 CAS를 재사용하고, 만료 뒤에는 validator 조건부 확인 또는
validator가 없는 원본의 전체 재다운로드를 수행합니다.

v5 Worker는 provider나 WebDAV 접근 전에 state 경로가 속할 filesystem의 free bytes를
read-only로 확인합니다. 기본 Compose floor는 2 GiB이고 candidate overlay 기본값은
32 GiB입니다. 모든 derived view와 embedding cache hit가 확정된 뒤에는 embedding miss를
받기 전에 state 64 GiB, peak 뒤 reserved free space 2 GiB, 단일 vector sidecar 16 GiB,
단일 serving DB 4 GiB의 기본 한도를 각각 검사합니다. 이 설정은 부호나 단위 suffix가
없는 canonical decimal bytes이며 state/sidecar/DB는 양수, 두 free-space 값은 0 이상이어야
합니다. 이 gate는 v5에만 적용되고 v1-v4 rollback 경로는 그대로 유지됩니다.

Worker와 MCP volume이 같은 host filesystem에 있으면 remote generation을 한 벌로만
계산하면 안 됩니다. Worker의 PDF/OCR/cache와 생성 DB/sidecar에 더해 MCP가 별도로
download/stage/retain하는 PDF/source CAS, DB, sidecar 복사본 및 두 runtime의 reserved
free-space 약속을 합산해 host 여유 공간과
`CARDRAG_WORKER_MINIMUM_START_FREE_BYTES`를 정합니다. candidate 32 GiB는 조기 floor일
뿐이며 corpus 기반 Worker preflight나 MCP 자체 quota/download gate를 대신하지 않습니다.

stable publication 승인과 remote object 삭제 승인은 서로 다른 작업입니다. 기본
Compose는 `CARDRAG_COLLECT_REMOTE_GARBAGE=false`와
`CARDRAG_REMOTE_GC_APPROVED=false`를 사용합니다. 별도 cleanup 승인 뒤에만 stable
환경에서 `CARDRAG_STABLE_PUBLICATION_APPROVED=true`,
`CARDRAG_REMOTE_GC_APPROVED=true`, `CARDRAG_COLLECT_REMOTE_GARBAGE=true`를 함께
설정합니다. 하나라도 빠진 collection 설정은 Worker가 시작 전에 거부합니다.

Worker systemd unit은 stdout/stderr를 journal에 명시적으로 보냅니다. 후보 장시간
실행 중에는 다음처럼 INFO 진행 기록과 마지막 terminal 결과를 함께 확인합니다.

```bash
journalctl -fu cardrag-worker.service -o short-iso-precise
```

unit의 foreground Docker Compose 실행과 컨테이너 `init: true`,
`stop_signal: SIGTERM`이 systemd의 첫 종료 신호를 Worker까지 전달합니다. Worker는
pipeline 취소, 실행 중 blocking mutation drain, 게시 정합성 확인과 run terminal
기록을 마친 뒤 lock을 해제합니다. 반복 신호는 이 drain을 다시 취소하지 않습니다.
WebDAV timeout은 요청별 inactivity 상한이고 한 검증 thread에는 여러 요청이 있을 수
있으므로 unit은 잘못된 aggregate 강제종료 대신 `TimeoutStopSec=infinity`와
`SendSIGKILL=no`를 사용합니다. 계획된 130/143은 정상 service stop으로 인정됩니다.
종료 요청 뒤에는 JSON `reason_code=worker_signal_shutdown`과 상태 DB의
`interrupted`, `no_change` 또는 이미 검증된 `succeeded`를 함께 확인합니다. 장애 판단에 따른
수동 강제종료는 먼저 증거를 보존하고 project/service label로 exact container ID가
하나임을 확인한 뒤 컨테이너 전체에만 수행하며, 상세 절차는
[v1.0.10 migration](../docs/V1_0_10_MIGRATION.md)을 따릅니다.

기본 Worker 명령은 `docker compose run --rm`이므로 종료한 임시 컨테이너는 증거로
남지 않습니다. 장애가 발생하면 재시작하기 전에 journal, `systemctl show` 결과와
상태 volume의 read-only snapshot을 먼저 보존합니다. 컨테이너 보존을 위해 운영
명령에서 임의로 `--rm`을 제거하거나 고정 container name을 추가하지 않습니다.

## stable 운영 원칙

v1.0.10 candidate가 합격하고 전환이 승인될 때까지 v1.0.9 운영 image, container,
volume, 설치 pointer와 channel을 그대로 유지합니다.

```dotenv
CARDRAG_WORKER_IMAGE=ymtop59/mcp-card-prd-detail:1.0.9-worker
CARDRAG_MCP_IMAGE=ymtop59/mcp-card-prd-detail:1.0.9-mcp
```

v1.0.10 후보가 합격하고 전환이 승인되면 v4/v5 dual-reader MCP를 먼저 배치해 기존
v4를 제공하는지 확인하고, 이후에만 v1.0.10 Worker와 stable channel을 전환합니다.
PDF/OCR seed, 합격 기준, rollback 및 승인 경계는
[v1.0.10 migration](../docs/V1_0_10_MIGRATION.md)을 따릅니다.

MCP host port는 loopback으로만 bind하고 외부 접근은 TLS reverse proxy와 Bearer
token을 사용합니다. 실제 credential은 env 파일이나 저장소에 넣지 않고
`compose.secrets.yaml`을 통해 주입합니다.
