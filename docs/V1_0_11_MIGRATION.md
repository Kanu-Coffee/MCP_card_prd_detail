# CardRAG v1.0.11 candidate migration

이 문서는 v1.0.10 candidate의 OCR provider process 종료를 수정한 v1.0.11을
격리 검증하고, 합격한 경우에만 stable로 전환하는 절차입니다. 구조·임베딩·gold
평가의 상세 계약은 v1.0.10 문서를 historical baseline으로 유지합니다.

**현재 gate 상태: candidate acceptance 미통과.** v1.0.11 source image, full four-issuer
run, 12개 runtime evidence와 canonical receipt가 아직 봉인되지 않은 상태에서는
Docker Hub release, stable WebDAV pointer, `/opt/cardrag/current`, systemd unit/timer와
LibreChat 소비 경로를 변경하지 않습니다. 이 상태는 TODO가 아니라 기술적 release
blocker입니다.

## 1. public source candidate image

Candidate image는 공개된 source repository의 정확한 40-hex commit을 remote Git
context로 사용합니다. local worktree나 untracked 파일은 build context가 아닙니다.

```bash
set -euo pipefail
[[ "$CANDIDATE_SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ ]]
test "$(docker buildx version | awk '{print $2}')" = "v0.36.1"
mapfile -t buildkit_versions < <(
  docker buildx inspect | sed -n 's/^[[:space:]]*BuildKit version: //p'
)
((${#buildkit_versions[@]} == 1))
test "${buildkit_versions[0]}" = "v0.32.2"

candidate_repository=ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate
source_context="https://github.com/Kanu-Coffee/MCP_card_prd_detail.git#$CANDIDATE_SOURCE_COMMIT"
for role in worker mcp; do
  docker buildx build \
    --platform linux/amd64 \
    --target "$role" \
    --build-arg APP_VERSION=1.0.11 \
    --build-arg "VCS_REF=$CANDIDATE_SOURCE_COMMIT" \
    --build-arg PYTHON_DEV_IMAGE=cgr.dev/chainguard/python:latest-dev@sha256:4e2adecf67a1d18773c55b5526b47436392b9816ae6b8d92575979a2ab9de8b2 \
    --build-arg PYTHON_RUNTIME_IMAGE=cgr.dev/chainguard/python:latest@sha256:f47d995d001c1f949d560b1158d7f3ae556aad75a1044e72a125c900c1f05332 \
    --build-arg UV_IMAGE=ghcr.io/astral-sh/uv:0.8.17@sha256:e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1 \
    --build-arg CODEX_VERSION=0.151.0 \
    --build-arg CODEX_SHA256=605b4b183f22c645f5def63a5b7191767407fb66a6feaec4eaf10b5b7e0058f6 \
    --attest type=provenance,mode=max,version=v0.2 \
    --attest type=sbom,generator=docker.io/docker/buildkit-syft-scanner:stable-1@sha256:ae4f3b554449e7e25548e7d8ccc029d17357348e30c6e3df01b92bc93654d6a9 \
    --output "type=registry,name=$candidate_repository:candidate-v1.0.11-$role-$CANDIDATE_SOURCE_COMMIT,oci-mediatypes=true,oci-artifact=true" \
    "$source_context"
done
```

허용 build arg는 위 일곱 개뿐입니다. `--build-context`, local/SSH input, alternate
Dockerfile, entitlement와 `--secret`은 금지합니다. BuildKit raw provenance가 공개 Git
fetch용 두 Git auth ID의 optional 내장 선언을 기록할 수 있습니다. 이 선언 자체는 token 값의 미전달을 증명하지 않으므로
producer command와 실행 기록에서도 credential
전달이 없음을 별도로 확인합니다.

이 수동 producer는 외부 trust boundary입니다. raw SLSA statement만으로 producer
identity가 서명되는 것은 아닙니다. public OCI index, platform/config/attestation digest,
raw provenance/SBOM, 실행 버전과 candidate runtime evidence가 모두 canonical receipt에
일치하지 않으면 기술적 release blocker입니다.

Codex 0.151.0의 `codex-x86_64-unknown-linux-musl.tar.gz` 공식 asset SHA-256은
`605b4b183f22c645f5def63a5b7191767407fb66a6feaec4eaf10b5b7e0058f6`입니다. Dockerfile과
provenance validator는 이 값과 release URL을 함께 고정합니다.

## 2. 격리 값과 cache 보존

| 항목 | v1.0.11 candidate |
|---|---|
| Compose project | `cardrag-v111-candidate` |
| WebDAV channel | `candidate-v1.0.11` |
| Worker state | `cardrag-worker-v111-candidate-state` |
| Codex home | `cardrag-worker-v111-candidate-codex-home` |
| MCP state | `cardrag-mcp-v111-candidate-state` |
| MCP bind | `127.0.0.1:18011` |
| native OCR cache | verified GET only (`read-only`) |
| remote GC | disabled |

`cardrag-worker-v109-state`, `cardrag-worker-v110-state`와 각 Codex/MCP volume은 v1.0.11의
RW destination으로 사용하지 않습니다. v1.0.10 실패 state를 회복에 활용하려면 먼저
Worker가 terminal이고 SQLite `-wal`/`-shm`이 없음을 read-only로 확인한 뒤,
candidate-only v111 volume에 독립 snapshot을 복사하고 source/destination tree digest를
대조합니다. 원본 v110 volume은 incident evidence로 유지합니다. 복사 실패나 hash
불일치에서는 destination만 격리하고 source는 변경하지 않습니다.

Codex 인증도 state와 섞지 않습니다. v111 Codex volume에는 mode 0600, UID/GID
10001:10001인 `auth.json`과 mode 0700 `home/`만 복사하고, login status 출력은 버립니다.
logs, sessions, cache와 일반 home은 이관하지 않습니다. remote native OCR cache는 계속
read-only이므로 기존 verified hit를 재사용하되 manifest/READY를 생성하거나 repair하지
않습니다.

Candidate와 stable은 `CARDRAG_WEBDAV_BASE_URL`의 canonical `/home/cardrag` root를
공유합니다. Resume checkpoint와 native cache object는 이 root 아래의 기존 identity를
유지해야 합니다. 격리 단위는 `candidate-v1.0.11` channel과 위 세 named volume이며,
별도 candidate base URL로 바꾸면 안 됩니다. Shared root에서도 candidate Worker는
HEAD/GET만 허용하고 OCR cache write/repair, stable pointer write와 remote GC는 계속 0건이어야
합니다. Archive용 WebDAV namespace는 이 runtime root와 별도입니다.

## 3. candidate 실행 gate

다음 순서가 모두 성공해야 candidate receipt를 만들 수 있습니다.

1. v109/v110 운영·사고 자산과 stable pointer의 before inventory를 봉인합니다.
2. v111 state/auth volume snapshot 및 sanitized Compose JSON을 검증합니다.
3. exact receipt-bound Worker OCI index/config digest로 실패 run을 `resume`하거나,
   별도 승인된 새 full run을 실행합니다.
4. 네 카드사 모두 `acquired=succeeded`, `failed=0`인지 확인합니다.
5. native cache before/after와 GET-only audit가 동일한지 확인합니다.
6. candidate MCP를 18011에서 시작해 readiness, 8개 tool, source PDF range,
   v4→v5→restart→v4→v5 rollback을 검증합니다.
7. `release-evidence/v1.0.11/`에 12개 runtime evidence와 gold/aggregation portable
   evidence를 봉인하고 `candidate-acceptance-receipt.json`을 검증합니다.

```bash
set -euo pipefail
: "${CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST:?receipt-bound Worker index digest is required}"
: "${CARDRAG_CANDIDATE_MCP_IMAGE_DIGEST:?receipt-bound MCP index digest is required}"
: "${CARDRAG_PRESERVED_RUN_ID:?audited terminal run ID is required}"
[[ "$CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "$CARDRAG_CANDIDATE_MCP_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "$CARDRAG_PRESERVED_RUN_ID" =~ ^[0-9a-f]{32}$ ]]

docker compose --env-file /etc/cardrag/candidate-worker-v111.env \
  -f deploy/worker/compose.yaml \
  -f deploy/worker/compose.candidate.yaml \
  -f deploy/worker/compose.secrets.yaml \
  config --quiet

docker compose --env-file /etc/cardrag/candidate-worker-v111.env \
  -f deploy/worker/compose.yaml \
  -f deploy/worker/compose.candidate.yaml \
  -f deploy/worker/compose.secrets.yaml \
  run --name cardrag-v111-candidate-worker-acceptance \
  worker resume "$CARDRAG_PRESERVED_RUN_ID"

docker compose --env-file /etc/cardrag/candidate-mcp-v111.env \
  -f deploy/mcp/compose.yaml \
  -f deploy/mcp/compose.candidate.yaml \
  -f deploy/mcp/compose.secrets.yaml \
  up -d --wait
```

Candidate failure는 stable cutover나 cleanup 근거가 아닙니다. 실패한 v111 container,
state와 reports를 먼저 보존하고 v109 MCP, v110 incident state, stable WebDAV와
`/opt/cardrag/current`의 before/after identity가 같은지 다시 검사합니다.

## 4. release와 stable cutover

Release workflow는 현재 `1.0.11`만 허용하며 다른 version은 evidence conditional 앞에서
즉시 거부합니다. Candidate source commit 이후 tag commit의 변경은
`release-evidence/v1.0.11/**`만 허용됩니다. annotated `v1.0.11` tag, successful exact
commit CI, canonical receipt SHA와 Worker/MCP OCI index digest가 모두 있어야 Docker Hub
copy와 signing을 시작합니다.

Candidate receipt 합격은 candidate volume을 stable service에 직접 mount할 권한이 아닙니다.
Stable 전환에는 다음 exact offline mapping을 사용합니다.

| stopped source | 새 stable destination | copy 계약 |
|---|---|---|
| `cardrag-worker-v111-candidate-state` | `cardrag-worker-v111-state` | 전체 독립 byte copy + tree digest + 모든 SQLite integrity |
| `cardrag-mcp-v111-candidate-state` | `cardrag-mcp-v111-state` | 전체 독립 byte copy + tree digest + 모든 SQLite integrity |
| `cardrag-worker-v111-candidate-codex-home/auth.json` | `cardrag-worker-v111-codex-home/auth.json` | 새 volume에 `auth.json`만 descriptor copy |

먼저 candidate Worker가 terminal인지 확인하고 candidate MCP를 stop합니다. 그 뒤 세 source
volume 각각에 대해 `docker ps --quiet --filter volume=<exact-name>` 출력이 비어 있어야
합니다. Candidate Worker 또는 MCP 중 하나라도 running이면 copy를 시작하지 않습니다.
세 stable destination은 모두 존재하지 않아야 하며, 이전 실패에서 남은 destination을
재사용하거나 merge하지 않습니다. Verifier의 descriptor identity 재확인은 live snapshot을
대체하지 않으므로 source와 destination에는 전체 scan 동안 다른 writer가 없어야 합니다.

```bash
set -euo pipefail
candidate_volumes=(
  cardrag-worker-v111-candidate-state
  cardrag-worker-v111-candidate-codex-home
  cardrag-mcp-v111-candidate-state
)
stable_volumes=(
  cardrag-worker-v111-state
  cardrag-worker-v111-codex-home
  cardrag-mcp-v111-state
)

for volume in "${candidate_volumes[@]}"; do
  docker volume inspect "$volume" >/dev/null
  test -z "$(docker ps --quiet --filter "volume=$volume")"
done
for volume in "${stable_volumes[@]}"; do
  if docker volume inspect "$volume" >/dev/null 2>&1; then
    printf 'stable destination already exists: %s\n' "$volume" >&2
    exit 1
  fi
  docker volume create "$volume" >/dev/null
done

for mapping in \
  cardrag-worker-v111-candidate-state:cardrag-worker-v111-state \
  cardrag-mcp-v111-candidate-state:cardrag-mcp-v111-state
do
  source_volume=${mapping%%:*}
  destination_volume=${mapping#*:}
  source_mount=$(docker volume inspect --format '{{.Mountpoint}}' "$source_volume")
  destination_mount=$(docker volume inspect --format '{{.Mountpoint}}' "$destination_volume")
  sudo rsync --archive --numeric-ids --no-hard-links --one-file-system \
    -- "$source_mount/" "$destination_mount/"
  sudo /usr/bin/python3 tools/cardrag_offline_volume_verify.py state \
    --source "$source_mount" --destination "$destination_mount"
done
```

`rsync`에 `--hard-links`를 추가하거나 `cp -al`, hardlink farm, union/overlay merge를 쓰면
안 됩니다. Verifier는 relative path/type/mode/UID/GID/size/content로 canonical tree digest를
다시 계산하고, source/destination inode 중복과 어느 regular file의 link count 2 이상도
거부합니다. SQLite `-wal`/`-shm`, symlink/special node, digest 차이 또는
`PRAGMA quick_check`/`integrity_check`의 `ok` 이외 결과와 `foreign_key_check` 1행 이상도
promotion blocker입니다.

Codex stable destination은 exact v1.0.11 Worker image의 mode-0700/UID 10001 mountpoint
copy-up으로 초기화합니다. [v1.0.10 migration의 hardened descriptor copy](V1_0_10_MIGRATION.md#3-codex-oauthhome-분리-resume-전-write-stop)를
그대로 사용하되 source volume/path를
`cardrag-worker-v111-candidate-codex-home:/source:ro`와 `/source/auth.json`, destination을
`cardrag-worker-v111-codex-home:/var/lib/cardrag-codex-home`으로 고정합니다. 일반 `cp`나
Codex home 전체 복사는 금지합니다. 복사 뒤에는 아래 검증이 성공해야 하며 출력에는
credential content나 digest가 포함되지 않습니다.

```bash
source_codex_mount=$(docker volume inspect --format '{{.Mountpoint}}' \
  cardrag-worker-v111-candidate-codex-home)
stable_codex_mount=$(docker volume inspect --format '{{.Mountpoint}}' \
  cardrag-worker-v111-codex-home)
sudo /usr/bin/python3 tools/cardrag_offline_volume_verify.py codex-home \
  --source "$source_codex_mount" --destination "$stable_codex_mount"
```

모든 offline 검증 뒤에도 candidate 세 source volume의 running mount가 0인지 다시
확인합니다. 하나라도 실패하면 stable Worker/MCP를 시작하지 않고 candidate source와
운영 v1.0.9를 그대로 보존합니다. 실패 destination의 격리/삭제는 exact identity 확인을
거친 별도 승인 작업입니다.

Volume promotion이 모두 통과한 뒤 stable cutover는 다음 순서를 지킵니다.

1. timer가 비활성이고 기존 운영 Worker가 terminal인지 확인합니다.
2. immutable `/opt/cardrag/v1.0.11` 설치 tree와 systemd unit을 준비하되 `current`는
   아직 바꾸지 않습니다.
3. 새 `cardrag-mcp-v111-state`만 mount한 v1.0.11 MCP를 shared stable WebDAV channel에
   연결해 기존 v4 readback과 readiness/tool smoke를 통과시킵니다.
4. 새 `cardrag-worker-v111-state`와 `cardrag-worker-v111-codex-home`만 mount한 v1.0.11
   Worker를 stable channel에서 한 번 실행하고 새 v5 generation readback을 확인합니다.
   stable publication approval과 OCR cache write/remote GC approval은 각각 독립된 값입니다.
5. 그 뒤에만 `/opt/cardrag/current`, 서비스 env와 timer를 v1.0.11로 전환합니다.

현재 서버의 `/opt/cardrag/current`와 설치된 unit/env가 실제 실행 중인 v1.0.9 candidate
MCP와 일치하지 않으므로, cutover 전에 이 drift를 별도 inventory로 봉인하고 한 번의
원자적 변경으로 교정해야 합니다. env 파일을 부분적으로 섞거나 existing volume 이름을
바꾸지 않습니다.

## 5. rollback과 cleanup 금지선

Stable v5 게시 전 실패하면 v1.0.9 MCP/Worker image와 기존 stable pointer를 그대로
유지합니다. Stable v5 게시 뒤에는 v1.0.9 MCP로 단순 downgrade하지 않습니다. v1.0.11
dual-reader MCP를 유지하고 봉인된 last-good v4를 활성화한 뒤 v1.0.11 Worker를 멈춰
추가 publication을 차단합니다.

Rollback 기간 동안 v1.0.9 image/volume, v1.0.10 incident state, v1.0.11 candidate state,
stable/candidate WebDAV generation과 `/opt/cardrag` 설치 tree를 삭제하지 않습니다.
최소 두 번의 연속 stable 성공, MCP readback, backup 복구시험과 exact reference-free
inventory가 모두 있어야 cleanup을 별도 승인할 수 있습니다. Remote GC는 stable
publication과 별도 승인입니다.
