# CardRAG v1.0.10 candidate migration

이 절차는 v1.0.9 운영 자산을 변경하지 않는 candidate 전용 절차입니다. stable
cutover, 운영 Worker 재시작, 운영 volume 정리, LibreChat 소비 경로 변경은 포함하지
않으며 각각 별도 명시적 승인이 필요합니다.

## 고정 격리 값

| 항목 | v1.0.10 candidate |
|---|---|
| Git branch | `codex/cardrag-v1.0.10` |
| WebDAV channel | `candidate-v1.0.10` |
| Compose project | `cardrag-v110-candidate` |
| Worker volume | `cardrag-worker-v110-state` |
| MCP volume | `cardrag-mcp-v110-state` |
| MCP bind | `127.0.0.1:18010` |
| remote GC | disabled |

candidate가 운영 v1.0.9 volume을 RW로 mount하거나 stable channel을 사용하면 즉시
중단합니다. `cardrag-worker-v109-state`는 seed 시에만
`/mnt/cardrag-v109-state:ro`로 연결합니다. 기존 v1.0.9 Small embedding cache와
legacy Qwen vectors는 복사하지 않습니다.

## 1. 변경 전 read-only inventory

실행 직전에 다음을 다시 기록합니다.

```bash
git status --short --branch
git rev-parse HEAD
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}'
docker volume ls --format '{{.Name}}'
systemctl show cardrag-worker.service \
  -p ActiveState -p SubState -p Result -p ExecMainStatus
readlink -f /opt/cardrag/current
```

운영 Worker process가 실행 중이거나 terminal 여부가 불명확하면 seed, snapshot,
copy와 prune을 하지 않습니다. secret 값과 tokenized URL은 inventory에 기록하지
않습니다. stable pointer는 GET/hash만 허용하고 PUT/DELETE하지 않습니다.

## 2. Compose 렌더링 gate

실제 credential을 출력하지 않고 effective configuration에서 다음을 확인합니다.

```bash
docker compose \
  -f deploy/worker/compose.yaml \
  -f deploy/worker/compose.candidate.yaml \
  -f deploy/worker/compose.cache-seed.yaml config --format json

docker compose \
  -f deploy/mcp/compose.yaml \
  -f deploy/mcp/compose.candidate.yaml config --format json
```

Worker source mount의 `read_only=true`, 두 destination volume 이름, project 이름,
channel, port, `CARDRAG_COLLECT_REMOTE_GARBAGE=false`,
`CARDRAG_REMOTE_GC_APPROVED=false`와 Qwen 4,096D를 모두 assert합니다.

## 3. v1.0.9 PDF/OCR seed

운영 Worker가 terminal인 것이 확인된 뒤 v1.0.9 source를 read-only로 inventory합니다.
seed 구현은 source state DB와 PDF CAS의 regular-file, path, size와 SHA를 검증하고
전건 ledger를 candidate destination에 기록합니다. destination에는 새 v1.0.10 PDF
objects와 source revision만 만듭니다.

```bash
# Read-only plan/inventory pass. This does not import into the candidate volume.
docker compose \
  -f deploy/worker/compose.yaml \
  -f deploy/worker/compose.candidate.yaml \
  -f deploy/worker/compose.cache-seed.yaml \
  run --rm worker seed-cache-v109 /mnt/cardrag-v109-state

# Candidate-only import after reviewing the dry-run report.
docker compose \
  -f deploy/worker/compose.yaml \
  -f deploy/worker/compose.candidate.yaml \
  -f deploy/worker/compose.cache-seed.yaml \
  run --rm worker seed-cache-v109 /mnt/cardrag-v109-state --apply
```

`--apply`를 뺀 명령으로 먼저 dry-run report를 검토하고, 실제 import에는
반드시 `--apply`를 사용합니다. 적용 경로는 동일 plan을 내부에서 두 번 적용하여
두 번째 pass의 신규 object/revision 수가 0이 아니면 실패합니다. report의
`idempotence_verified=true`, `idempotence_imported_pdf_objects=0`,
`idempotence_imported_revisions=0`을 확인합니다. source volume before/after
inventory와 DB/CAS hash가 같아야 합니다.
seed ledger의 PDF digest는 첫 full v1.0.10 seal까지 prune pin으로 유지합니다.
부분 issuer smoke에서는 local prune을 비활성화하거나 pin을 적용합니다.

동일 PDF의 native/adopted OCR object는 기존 reuse key와 READY 검증을 통과한 경우만
재사용합니다. 재사용 문서에는 새 OCR provider checkpoint가 생성되지 않아야 합니다.

## 4. candidate generation과 MCP

candidate Worker는 v1.0.10 이미지를 사용하고 candidate channel에만 publish합니다.
Worker는 corpus discovery/embedding 전에 두 allowlisted Qwen route의 credentialed live
preflight를 수행합니다. 2026-08-29 재검증에서는 `deepinfra`와 `nebius`가 모두 고정
24-sample·2회 반복·4,096D finite·cosine gate를 통과했습니다. 이때 확인된 OpenRouter
계약은 response model의 case-only canonicalization,
`max_prompt_tokens=null`일 때 양의 `context_length` 사용, pinned route에서
`require_parameters=false`입니다. 마지막 항목은 fallback 허용이 아니며 request의
`order`/`only`, `allow_fallbacks=false`와 response provider 검증을 함께 요구합니다.
설정 maximum token과 live metadata가 다르거나 어느 route라도 실패하면 corpus 처리 전에
중단합니다. 이 preflight pass만으로 full generation/gold 합격을 주장하지 않습니다.

```bash
docker compose \
  -f deploy/worker/compose.yaml \
  -f deploy/worker/compose.candidate.yaml \
  -f deploy/worker/compose.secrets.yaml \
  run --rm worker run
```

성공 후 manifest와 READY에서 `cardrag.generation.v5`,
`cardrag.serving-db.v5`, Qwen 4,096D, all view counts와 `vectors.f32` 결속을
검사합니다. candidate MCP는 별도 volume과 18010 포트로 시작합니다.

```bash
docker compose \
  -f deploy/mcp/compose.yaml \
  -f deploy/mcp/compose.candidate.yaml \
  -f deploy/mcp/compose.secrets.yaml \
  up -d --wait
```

health/readiness, 8개 tool discovery, `search_contracts`, bundle/revision API,
legacy adapter와 source PDF range를 smoke합니다. exact response의 expected/scored
contract/row count가 같아야 합니다.

reranker shadow는 기본적으로 꺼져 있습니다. sealed provider preflight를 확인한 뒤에도
`candidate-v1.0.10` MCP env에서만 아래처럼 명시적으로 켭니다. stable channel에서
true이면 Settings가 시작을 거부합니다.

```dotenv
CARDRAG_RERANKER_SHADOW_ENABLED=true
CARDRAG_RERANKER_SHADOW_MODEL=qwen/qwen3-reranker-8b
CARDRAG_RERANKER_SHADOW_PROVIDER_ID=fireworks
CARDRAG_RERANKER_SHADOW_MAX_CANDIDATES=64
CARDRAG_RERANKER_SHADOW_TIMEOUT_SECONDS=60
```

동일 query의 disabled/enabled 응답에서 bundle, dense score와 순서가 같고
`reranker_influenced_ranking=false`인지 비교합니다. enabled 응답은 optional shadow
status/candidate/rank-change/artifact SHA를 내며, provider failure smoke에서도 primary
응답은 성공하고 bounded failure artifact만 남아야 합니다. artifact는 candidate MCP
state의 `audit-reports/reranker-shadow/`에 있으며 query/evidence 원문을 저장하지 않습니다.
이 smoke는 300~500개 gold reranker-shadow lane 평가를 대체하지 않습니다.

candidate의 v5 `vectors.f32`가 1 GiB를 넘을 수 있으므로 용량 gate를 RAM gate와
혼동하지 않습니다. `CARDRAG_MCP_MAX_VECTOR_SIDECAR_BYTES`는 검증·mmap할 단일
sidecar 파일 크기(기본 16 GiB), `CARDRAG_MCP_MAX_RESIDENT_VECTOR_BYTES`는 active,
candidate, pinned handle의 실제 heap 배열(v1-v4 matrix와 모든 norm 배열, 기본
1 GiB)을 제한합니다. 기존 `CARDRAG_MCP_MAX_VECTOR_BYTES`는 v1-v4 inline matrix
제한이며 새 resident 값을 생략했을 때의 호환 fallback입니다. promotion 전 실제
sidecar 크기와 두 제한의 여유를 candidate report에 기록합니다.

## 5. rollback와 운영 무변경

candidate MCP local store에서 v4→v5→restart→v4 activation을 시험합니다. corrupt
sidecar와 cross-paired READY/manifest는 last-good를 바꾸지 않아야 합니다.

시험 뒤 다음 값을 최초 inventory와 비교합니다.

- 운영 v1.0.9 container image ID, start time, mounts와 process 상태
- 운영 volume identity와 read-only로 산출한 inventory/hash
- `/opt/cardrag/current`, systemd unit/timer state
- stable channel GET 결과의 status, bytes hash와 validators
- LibreChat container/network/endpoint

candidate 종료나 실패를 이유로 운영 자산을 cleanup하지 않습니다.

## 승인 뒤에만 가능한 단계

다음 단계는 이 문서의 candidate 절차가 자동으로 승인하지 않습니다.

1. PR/CI merge, annotated `v1.0.10` tag와 signed immutable images 발행
2. v5 reader를 stable MCP에 배치하고 기존 v4 readback 확인
3. stable Worker/channel pointer 전환
4. LibreChat에 신규 MCP endpoint 추가 또는 소비 경로 전환
5. rollback 기간 종료 뒤 v1.0.9 image/volume/pointer snapshot 정리

stable publication 승인은 remote GC 승인을 포함하지 않습니다. 원격 cleanup은 위
cutover와 별도의 명시적 승인 뒤 stable 환경에서만
`CARDRAG_STABLE_PUBLICATION_APPROVED=true`, `CARDRAG_REMOTE_GC_APPROVED=true`,
`CARDRAG_COLLECT_REMOTE_GARBAGE=true`를 동시에 설정해 수행합니다. 기본값과 candidate
overlay는 모두 false이며, 승인 조합이 불완전하면 Settings와 `gc --apply`가 mutation
전에 실패합니다.
