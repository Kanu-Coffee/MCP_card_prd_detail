# CardRAG v1.0.11 data candidate / v1.0.12 runtime patch migration

이 문서는 v1.0.10 candidate의 OCR provider process 종료를 수정한 v1.0.11 데이터
계약과 OpenRouter 임베딩 재시도를 보강한 v1.0.12 runtime patch를
격리 검증하고, 합격한 경우에만 stable로 전환하는 절차입니다. 구조·임베딩·gold
평가의 상세 계약은 v1.0.10 문서를 historical baseline으로 유지합니다.

**현재 gate 상태: candidate acceptance 미통과.** v1.0.11 source revision
`390d60bde13de2f7095da288a2226ed6ace7ba2c`의 four-issuer run은 acquisition/OCR/structure/view를
완결했지만, `embedding-v5`가 호출 단위가 아닌 stage 전체에 적용된 4회 재시도 예산을
소진해 fail-closed했습니다. v1.0.12 수정 source의 exact image, v1.0.11 실패 state를
독립 복사한 v112 volume에서의 성공한 explicit resume,
12개 runtime evidence와 canonical receipt가 아직 봉인되지 않은 상태에서는
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
    --build-arg APP_VERSION=1.0.12 \
    --build-arg "VCS_REF=$CANDIDATE_SOURCE_COMMIT" \
    --build-arg PYTHON_DEV_IMAGE=cgr.dev/chainguard/python:latest-dev@sha256:4e2adecf67a1d18773c55b5526b47436392b9816ae6b8d92575979a2ab9de8b2 \
    --build-arg PYTHON_RUNTIME_IMAGE=cgr.dev/chainguard/python:latest@sha256:f47d995d001c1f949d560b1158d7f3ae556aad75a1044e72a125c900c1f05332 \
    --build-arg UV_IMAGE=ghcr.io/astral-sh/uv:0.8.17@sha256:e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1 \
    --build-arg CODEX_VERSION=0.151.0 \
    --build-arg CODEX_SHA256=605b4b183f22c645f5def63a5b7191767407fb66a6feaec4eaf10b5b7e0058f6 \
    --attest type=provenance,mode=max,version=v0.2 \
    --attest type=sbom,generator=docker.io/docker/buildkit-syft-scanner:stable-1@sha256:ae4f3b554449e7e25548e7d8ccc029d17357348e30c6e3df01b92bc93654d6a9 \
    --output "type=registry,name=$candidate_repository:candidate-v1.0.12-$role-$CANDIDATE_SOURCE_COMMIT,oci-mediatypes=true,oci-artifact=true" \
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

| 항목 | v1.0.12 patch candidate |
|---|---|
| application/runtime/OCI label | `1.0.12` |
| data contract | `v1.0.11` |
| Compose project | `cardrag-v112-candidate` |
| WebDAV channel | `candidate-v1.0.11` |
| Worker state | `cardrag-worker-v112-candidate-state` |
| Codex home | `cardrag-worker-v112-candidate-codex-home` |
| MCP state | `cardrag-mcp-v112-candidate-state` |
| MCP bind | `127.0.0.1:18012` |
| native OCR cache | verified GET only (`read-only`) |
| remote GC | disabled |

v1.0.10 historical baseline의 4 GiB serving DB 한도는 actual v1.0.11 corpus의 exact
8,148,455,424 B prediction을 수용하지 못했습니다. 추가 카드사 4곳이 현재와 같은
규모·중복률이라고 가정해 exact input을 두 배로 재계산하면 DB 15.177 GiB, sidecar
9.615 GiB, generation download 29.973 GiB와 peak 97.406 GiB입니다.
v1.0.11의 Worker/MCP DB cap은 모두 32 GiB(`34359738368`), Worker/MCP state는 128 GiB
(`137438953472`), MCP generation download는 64 GiB(`68719476736`)이며 candidate
overlay와 acceptance receipt에서 ambient override가 불가능한 literal입니다. Sidecar
16 GiB, current four-issuer candidate startup floor 32 GiB와 reserved free space 2 GiB는
유지합니다. 이 8개 카드사 2배 projection에는 reserve 포함 99.406 GiB free-space가
필요합니다. 실제 신규 corpus에 따라 요구량은 더 커질 수 있습니다.
2026-09-02 capacity 조사 시 Docker backing filesystem은 76.48 GiB만 비어 있어 약
22.93 GiB 부족했습니다. 실제 실행 직전에 최신 free-space를 다시 측정하고 corpus
preflight를 통과해야 합니다. Worker/MCP volume이 같은 backing filesystem을 공유하면
8개 카드사 Worker state와 MCP retention/staging의 순간 합계가 약 150.84 GiB까지
예상됩니다. 현재 host의 다른 사용량과 두 서비스의 reserve까지 합치면 256 GiB도
부족하므로 최소 320 GiB급 shared backing filesystem을 사용하거나, Worker와 MCP를
분리해 각 filesystem의 quota와 physical free-space를 독립 검증합니다. 실행 전에는
당시 host baseline으로 다시 산정합니다.

`cardrag-worker-v109-state`, `cardrag-worker-v110-state` 및 v1.0.11 실패 source volume은
v1.0.12의 RW destination으로 사용하지 않습니다. 이번 explicit resume의 source는
`cardrag-worker-v111-candidate-state`와
`cardrag-worker-v111-candidate-codex-home`이고, destination은 각각
`cardrag-worker-v112-candidate-state`와
`cardrag-worker-v112-candidate-codex-home`입니다. 아래 3.1절처럼 v1.0.11 container의
terminal 상태와 writer 부재를 확인한 뒤 독립 snapshot을 만들고, source/destination
tree와 SQLite를 모두 대조합니다. 복사 실패나 hash 불일치에서는 v112 destination만
격리하고 v111 source/container/report는 incident evidence로 변경 없이 유지합니다.

Codex 인증도 state와 섞지 않습니다. v112 Codex volume에는 mode 0600, UID/GID
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
2. stopped v111 state/auth를 새 v112 전용 volume에 offline snapshot하고, SQLite/run/auth
   검증과 sanitized Compose JSON을 통과시킵니다.
3. exact receipt-bound Worker OCI index/config digest로 실패 run을 `resume`하거나,
   별도 승인된 새 full run을 실행합니다.
4. 네 카드사 모두 `acquired=succeeded`, `failed=0`인지 확인합니다.
5. native cache before/after와 GET-only audit가 동일한지 확인합니다.
6. candidate MCP를 18012에서 시작해 readiness, 8개 tool, source PDF range,
   v4→v5→restart→v4→v5 rollback을 검증합니다.
7. `release-evidence/v1.0.12/`에 12개 runtime evidence와 gold/aggregation portable
   evidence를 봉인하고 `candidate-acceptance-receipt.json`을 검증합니다.

### 3.1 stopped v111 state/auth의 v112 offline snapshot

이 절차는 실패 container를 다시 시작하지 않습니다. 먼저 실제 v1.0.11 실패 container와
두 source volume을 결속하고, 어느 running container도 source/destination을 mount하지
않는지 확인합니다. Destination은 기존 volume을 재사용하거나 merge하지 않고 이 실행에서
처음 만듭니다. Helper는 receipt-bound v1.0.12 Worker image만 `--pull never`,
`--network none`, read-only rootfs, UID/GID 10001:10001로 실행합니다.

```bash
set -euo pipefail
: "${CARDRAG_V111_FAILED_CONTAINER:?exact stopped v1.0.11 container is required}"
: "${CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST:?receipt-bound Worker index digest is required}"
: "${CANDIDATE_SOURCE_COMMIT:?v1.0.12 source commit is required}"
: "${CARDRAG_PRESERVED_RUN_ID:?failed v1.0.11 run ID is required}"
[[ "$CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "$CANDIDATE_SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ ]]
[[ "$CARDRAG_PRESERVED_RUN_ID" =~ ^[0-9a-f]{32}$ ]]
test "$CARDRAG_V111_FAILED_CONTAINER" = "cardrag-v111-candidate-worker-390d60b"
test "$CARDRAG_PRESERVED_RUN_ID" = "1f1763a9cd474a81952a6eb6ffb6e397"

candidate_worker_image="ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate@${CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST}"
source_state=cardrag-worker-v111-candidate-state
source_codex=cardrag-worker-v111-candidate-codex-home
destination_state=cardrag-worker-v112-candidate-state
destination_codex=cardrag-worker-v112-candidate-codex-home
repository_root=$(git rev-parse --show-toplevel)
test "$PWD" = "$repository_root"
test "$(git hash-object tools/cardrag_offline_volume_verify.py)" = \
  "$(git rev-parse "$CANDIDATE_SOURCE_COMMIT:tools/cardrag_offline_volume_verify.py")"

test "$(docker inspect --format '{{.State.Status}} {{.State.Running}} {{.State.ExitCode}} {{.State.OOMKilled}}' \
  "$CARDRAG_V111_FAILED_CONTAINER")" = "exited false 1 false"
test "$(docker inspect --format '{{ index .Config.Labels "org.opencontainers.image.version" }}' \
  "$CARDRAG_V111_FAILED_CONTAINER")" = "1.0.11"
test "$(docker inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
  "$CARDRAG_V111_FAILED_CONTAINER")" = \
  "390d60bde13de2f7095da288a2226ed6ace7ba2c"
test "$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project" }}' \
  "$CARDRAG_V111_FAILED_CONTAINER")" = "cardrag-v111-candidate"
source_mounts=$(docker inspect --format \
  '{{range .Mounts}}{{if eq .Type "volume"}}{{println .Name}}{{end}}{{end}}' \
  "$CARDRAG_V111_FAILED_CONTAINER")
grep -Fx "$source_state" <<<"$source_mounts" >/dev/null
grep -Fx "$source_codex" <<<"$source_mounts" >/dev/null

test "$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.version" }}' \
  "$candidate_worker_image")" = "1.0.12"
test "$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
  "$candidate_worker_image")" = "$CANDIDATE_SOURCE_COMMIT"

for volume in "$source_state" "$source_codex"; do
  docker volume inspect "$volume" >/dev/null
  test -z "$(docker ps --quiet --filter "volume=$volume")"
done
for volume in "$destination_state" "$destination_codex"; do
  if docker volume inspect "$volume" >/dev/null 2>&1; then
    printf 'v112 destination already exists: %s\n' "$volume" >&2
    exit 1
  fi
  docker volume create "$volume" >/dev/null
  test -z "$(docker ps --quiet --filter "volume=$volume")"
done
```

State copy helper는 source의 root·모든 entry가 UID/GID 10001:10001인지 확인하고,
symlink, special node, hardlink와 cross-filesystem entry를 거부합니다. 새 destination이
비어 있을 때만 mode와 bytes를 독립 복사합니다. 중간 실패에서는 destination을 다시
사용하지 않으며 source를 수정하지 않습니다.

```bash
docker run --rm --interactive --pull never --network none --read-only \
  --cap-drop ALL --security-opt no-new-privileges=true --user 10001:10001 \
  --entrypoint python \
  --volume "$source_state:/source:ro" \
  --volume "$destination_state:/var/lib/cardrag-worker" \
  "$candidate_worker_image" - <<'PY'
import os
import shutil
import stat
from pathlib import Path

source = Path("/source")
destination = Path("/var/lib/cardrag-worker")
expected_owner = (10001, 10001)

def metadata(path: Path) -> os.stat_result:
    value = path.lstat()
    if stat.S_ISLNK(value.st_mode) or (value.st_uid, value.st_gid) != expected_owner:
        raise SystemExit("state_source_metadata_invalid")
    return value

source_root = metadata(source)
destination_root = metadata(destination)
if not stat.S_ISDIR(source_root.st_mode) or not stat.S_ISDIR(destination_root.st_mode):
    raise SystemExit("state_root_invalid")
if stat.S_IMODE(source_root.st_mode) != 0o700 or stat.S_IMODE(destination_root.st_mode) != 0o700:
    raise SystemExit("state_root_mode_invalid")
if any(destination.iterdir()):
    raise SystemExit("state_destination_not_empty")
source_device = source_root.st_dev

def sync_directory(source_directory: Path, destination_directory: Path) -> None:
    for child in sorted(source_directory.iterdir(), key=lambda item: item.name):
        value = metadata(child)
        if value.st_dev != source_device:
            raise SystemExit("state_cross_filesystem_entry")
        target = destination_directory / child.name
        if stat.S_ISDIR(value.st_mode):
            target.mkdir(mode=stat.S_IMODE(value.st_mode))
            sync_directory(child, target)
            os.chmod(target, stat.S_IMODE(value.st_mode), follow_symlinks=False)
        elif stat.S_ISREG(value.st_mode) and value.st_nlink == 1:
            shutil.copyfile(child, target, follow_symlinks=False)
            os.chmod(target, stat.S_IMODE(value.st_mode), follow_symlinks=False)
            descriptor = os.open(target, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        else:
            raise SystemExit("state_entry_type_invalid")
        copied = target.lstat()
        if (copied.st_uid, copied.st_gid) != expected_owner:
            raise SystemExit("state_destination_owner_invalid")
    directory_descriptor = os.open(destination_directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)

sync_directory(source, destination)
print("v112-worker-state-offline-copy-complete")
PY
```

Codex destination은 image의 copy-up으로 생성된 mode-0700 root와 빈 `home/`만 허용합니다.
Source 전체를 복사하지 않고 bounded regular `auth.json` 한 파일만 mode 0600으로 atomic
copy합니다. Token 내용과 digest는 출력하지 않습니다.

```bash
docker run --rm --interactive --pull never --network none --read-only \
  --cap-drop ALL --security-opt no-new-privileges=true --user 10001:10001 \
  --entrypoint python \
  --volume "$source_codex:/source:ro" \
  --volume "$destination_codex:/var/lib/cardrag-codex-home" \
  "$candidate_worker_image" - <<'PY'
import os
import stat
from pathlib import Path

source_root = Path("/source")
destination_root = Path("/var/lib/cardrag-codex-home")
source = source_root / "auth.json"
home = destination_root / "home"
destination = destination_root / "auth.json"
temporary = destination_root / ".auth.json.snapshot"
expected_owner = (10001, 10001)

def require_directory(path: Path) -> None:
    value = path.lstat()
    if (not stat.S_ISDIR(value.st_mode) or stat.S_ISLNK(value.st_mode)
            or stat.S_IMODE(value.st_mode) != 0o700
            or (value.st_uid, value.st_gid) != expected_owner):
        raise SystemExit("codex_directory_metadata_invalid")

require_directory(source_root)
require_directory(destination_root)
require_directory(home)
if any(home.iterdir()) or set(item.name for item in destination_root.iterdir()) != {"home"}:
    raise SystemExit("codex_destination_not_fresh")
source_stat = source.lstat()
if (not stat.S_ISREG(source_stat.st_mode) or stat.S_ISLNK(source_stat.st_mode)
        or stat.S_IMODE(source_stat.st_mode) != 0o600
        or (source_stat.st_uid, source_stat.st_gid) != expected_owner
        or source_stat.st_nlink != 1 or not 1 <= source_stat.st_size <= 2 * 1024 * 1024):
    raise SystemExit("codex_source_auth_invalid")

read_flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
source_fd = os.open(source, read_flags)
destination_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
def identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_gid,
        value.st_nlink, value.st_size, value.st_mtime_ns, value.st_ctime_ns,
    )
source_open_stat = os.fstat(source_fd)
if identity(source_open_stat) != identity(source_stat):
    raise SystemExit("codex_source_auth_identity_changed")
try:
    remaining = source_stat.st_size
    while remaining:
        block = os.read(source_fd, min(1024 * 1024, remaining))
        if not block:
            raise SystemExit("codex_source_auth_short_read")
        view = memoryview(block)
        while view:
            written = os.write(destination_fd, view)
            view = view[written:]
        remaining -= len(block)
    if os.read(source_fd, 1):
        raise SystemExit("codex_source_auth_grew")
    os.fsync(destination_fd)
    if identity(os.fstat(source_fd)) != identity(source_open_stat):
        raise SystemExit("codex_source_auth_changed")
finally:
    os.close(destination_fd)
    os.close(source_fd)
os.replace(temporary, destination)
root_fd = os.open(destination_root, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(root_fd)
finally:
    os.close(root_fd)
print("v112-codex-auth-offline-copy-complete")
PY
```

복사 뒤 verifier도 같은 v1.0.12 image에서 network 없이 실행합니다. State verifier는
source/destination의 relative path, type, mode, UID/GID, size와 content digest가 동일하고
inode가 겹치지 않는지 확인합니다. 모든 SQLite에 read-only immutable
`quick_check`, `integrity_check`, `foreign_key_check`를 수행하며 `-wal`, `-shm`,
`-journal`이 하나라도 있으면 거부합니다. Codex verifier는 destination inventory가 정확히
`auth.json`과 빈 `home/`뿐인지, owner/mode와 auth bytes가 source와 같은지 확인합니다.

```bash
verifier="$repository_root/tools/cardrag_offline_volume_verify.py"
state_verification=$(docker run --rm --pull never --network none --read-only \
  --cap-drop ALL --security-opt no-new-privileges=true --user 10001:10001 \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m --entrypoint python \
  --volume "$source_state:/source:ro" \
  --volume "$destination_state:/destination:ro" \
  --volume "$verifier:/verify.py:ro" \
  "$candidate_worker_image" /verify.py state --source /source --destination /destination)
jq -e '.status == "passed" and .mode == "state" and .sqlite_database_count >= 1' \
  <<<"$state_verification" >/dev/null
printf '%s\n' "$state_verification"

codex_verification=$(docker run --rm --pull never --network none --read-only \
  --cap-drop ALL --security-opt no-new-privileges=true --user 10001:10001 \
  --entrypoint python \
  --volume "$source_codex:/source:ro" \
  --volume "$destination_codex:/destination:ro" \
  --volume "$verifier:/verify.py:ro" \
  "$candidate_worker_image" /verify.py codex-home \
    --source /source --destination /destination --expected-uid 10001 --expected-gid 10001)
jq -e '.status == "passed" and .mode == "codex-home"
  and .auth_content_equal and .destination_home_empty' \
  <<<"$codex_verification" >/dev/null
printf '%s\n' "$codex_verification"
```

마지막으로 destination DB에서 exact run이 `failed`, running run이 0개이고 exhausted
`embedding-v5` stage가 존재하는지 확인합니다. Error 문자열이나 credential은 출력하지
않습니다. 이 gate 뒤에도 v111 source와 v112 destination의 running mount가 0이어야 합니다.

```bash
run_verification=$(docker run --rm --pull never --network none --read-only \
  --cap-drop ALL --security-opt no-new-privileges=true --user 10001:10001 \
  --entrypoint python --volume "$destination_state:/state:ro" \
  "$candidate_worker_image" - "$CARDRAG_PRESERVED_RUN_ID" <<'PY'
import json
import os
import sqlite3
import sys
from pathlib import Path

run_id = sys.argv[1]
database = Path("/state/worker-state.sqlite3")
if any(os.path.lexists(f"{database}{suffix}") for suffix in ("-wal", "-shm", "-journal")):
    raise SystemExit("worker_state_not_checkpointed")
connection = sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True)
connection.execute("PRAGMA query_only=ON")
try:
    run = connection.execute("SELECT status FROM run WHERE run_id=?", (run_id,)).fetchone()
    running = connection.execute("SELECT count(*) FROM run WHERE status='running'").fetchone()[0]
    stage = connection.execute(
        """SELECT status,attempt_count,max_attempts FROM stage
           WHERE run_id=? AND document_id='corpus-v5' AND stage_name='embedding-v5'""",
        (run_id,),
    ).fetchone()
    cached = connection.execute("SELECT count(*) FROM embedding_cache_v5").fetchone()[0]
finally:
    connection.close()
if run != ("failed",) or running != 0 or stage is None:
    raise SystemExit("preserved_run_not_resumable_incident")
if stage != ("failed", 4, 4) or cached != 26992:
    raise SystemExit("embedding_stage_failure_not_sealed")
print(json.dumps({
    "embedding_cache_v5_rows": cached,
    "embedding_stage_attempts": stage[1],
    "run_id": run_id,
    "run_status": run[0],
    "running_runs": running,
    "status": "passed",
}, separators=(",", ":"), sort_keys=True))
PY
)
jq -e --arg run_id "$CARDRAG_PRESERVED_RUN_ID" \
  '.status == "passed" and .run_id == $run_id and .run_status == "failed"
   and .running_runs == 0 and .embedding_stage_attempts == 4
   and .embedding_cache_v5_rows == 26992' \
  <<<"$run_verification" >/dev/null
printf '%s\n' "$run_verification"
for volume in "$source_state" "$source_codex" "$destination_state" "$destination_codex"; do
  test -z "$(docker ps --quiet --filter "volume=$volume")"
done
```

`state_verification`, `codex_verification`, `run_verification` 세 JSON과 stopped container의
allowlisted identity를 candidate evidence에 봉인합니다. 어느 검증이든 실패하면 resume하지
않고 v112 destination을 격리합니다. Destination 삭제/재생성은 별도 exact-volume 승인
작업이며 이 절차가 자동으로 수행하지 않습니다.

### 3.2 v1.0.12 runtime으로 explicit resume

```bash
set -euo pipefail
: "${CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST:?receipt-bound Worker index digest is required}"
: "${CARDRAG_CANDIDATE_MCP_IMAGE_DIGEST:?receipt-bound MCP index digest is required}"
: "${CARDRAG_PRESERVED_RUN_ID:?audited terminal run ID is required}"
[[ "$CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "$CARDRAG_CANDIDATE_MCP_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "$CARDRAG_PRESERVED_RUN_ID" =~ ^[0-9a-f]{32}$ ]]

docker compose --env-file /etc/cardrag/worker.env \
  -f deploy/worker/compose.yaml \
  -f deploy/worker/compose.candidate.yaml \
  -f deploy/worker/compose.secrets.yaml \
  config --quiet

docker compose --env-file /etc/cardrag/worker.env \
  -f deploy/worker/compose.yaml \
  -f deploy/worker/compose.candidate.yaml \
  -f deploy/worker/compose.secrets.yaml \
  run --name cardrag-v112-candidate-worker-acceptance \
  worker resume "$CARDRAG_PRESERVED_RUN_ID"

docker compose --env-file /etc/cardrag/mcp.env \
  -f deploy/mcp/compose.yaml \
  -f deploy/mcp/compose.candidate.yaml \
  -f deploy/mcp/compose.secrets.yaml \
  config --quiet

docker compose --env-file /etc/cardrag/mcp.env \
  -f deploy/mcp/compose.yaml \
  -f deploy/mcp/compose.candidate.yaml \
  -f deploy/mcp/compose.secrets.yaml \
  up -d --wait
```

Sanitized effective-config evidence는
`cardrag.candidate-effective-config.v3`이어야 하며 project
`cardrag-v112-candidate`, v112 세 candidate volume, MCP port 18012, application/image
version 1.0.12와 data channel `candidate-v1.0.11`을 동시에 봉인합니다. 임베딩 재시도 값도
`embedding_request_max_attempts=12`, `embedding_retry_base_seconds=1`,
`embedding_retry_cap_seconds=60`으로 기록되어야 합니다. 하나라도 누락되거나 ambient env로
바뀌면 resume 성공 여부와 무관하게 acceptance를 거부합니다.

Runtime image identity는 Docker image store에 따라 같은 필드가 다른 OCI object를
가리킨다는 점을 명시적으로 봉인합니다. Registry에서 먼저 exact index digest가 유일한
linux/amd64 platform manifest를 가리키고 그 manifest의 config가 receipt config digest인지
검사합니다. 그 뒤 local image의 RepoDigests와 container `.Config.Image`가 모두 exact
`repository@index-digest`인지 확인합니다. Local/container ID의 허용 집합은 sealed index와
sealed config 두 개뿐이며 platform manifest, attestation manifest와 다른 digest는
허용하지 않습니다. Release workflow의 unqualified local image inspect가 `Descriptor`를
제공하면 media type과 digest가 exact OCI index여야 합니다. Container의 별도
`.ImageManifestDescriptor`는 아래와 같이 platform manifest를 결속합니다.

`cardrag.candidate-worker-metrics.v3`와 `cardrag.candidate-mcp-smoke.v2`는 다음 identity
필드를 필수로 기록합니다.

- `runtime_image_store_identity`: `classic-config-id` 또는 `containerd-index-id`
- `runtime_container_config_image`: exact Compose `repository@index-digest`
- `runtime_manifest_descriptor_digest`와 `runtime_manifest_descriptor_platform`: 둘 다 null이거나
  둘 다 존재해야 하는 pair
- `runtime_container_image_id`: 아래 store-specific exact ID

Classic은 container ID가 sealed platform config digest여야 합니다. Classic의 manifest
descriptor는 null을 허용하며, 제공되면 exact sealed platform manifest와
`linux/amd64` pair만 허용합니다. Containerd는 container ID가 sealed index digest이고
descriptor가 exact sealed platform manifest/`linux/amd64`여야 합니다. Store 이름만으로
판정하지 않고 아래 관측값을 함께 검사합니다.

```bash
set -euo pipefail
: "${candidate_reference:?exact repository@index reference is required}"
: "${candidate_index_digest:?sealed index digest is required}"
: "${candidate_platform_digest:?sealed linux/amd64 manifest digest is required}"
: "${candidate_config_digest:?sealed platform config digest is required}"
: "${candidate_container:?exact candidate container name is required}"
test "${candidate_reference##*@}" = "$candidate_index_digest"

repo_digests=$(docker image inspect "$candidate_reference" --format '{{json .RepoDigests}}')
jq -e --arg reference "$candidate_reference" 'index($reference) != null' \
  <<<"$repo_digests" >/dev/null
container_inspect=$(docker container inspect "$candidate_container")
jq -e 'type == "array" and length == 1 and (.[0] | type) == "object"' \
  <<<"$container_inspect" >/dev/null
container_config_image=$(jq -er '.[0].Config.Image | select(type == "string")' \
  <<<"$container_inspect")
test "$container_config_image" = "$candidate_reference"
container_image_id=$(jq -er '.[0].Image | select(type == "string")' \
  <<<"$container_inspect")
# Classic stores may omit ImageManifestDescriptor entirely. Normalize only that
# missing/null case; a present non-object value is rejected by the check below.
manifest_descriptor=$(jq -c '.[0].ImageManifestDescriptor // null' \
  <<<"$container_inspect")

case "$container_image_id" in
  "$candidate_config_digest") runtime_image_store_identity=classic-config-id ;;
  "$candidate_index_digest") runtime_image_store_identity=containerd-index-id ;;
  *) exit 1 ;;
esac
if test "$manifest_descriptor" != "null"; then
  jq -e --arg digest "$candidate_platform_digest" '
    .mediaType == "application/vnd.oci.image.manifest.v1+json"
    and .digest == $digest
    and .platform == {architecture:"amd64", os:"linux"}
  ' <<<"$manifest_descriptor" >/dev/null
elif test "$runtime_image_store_identity" != classic-config-id; then
  exit 1
fi
```

전체 `docker inspect` JSON은 env나 credential metadata를 포함할 수 있으므로 evidence에
저장하지 않습니다. 위 allowlisted identity 필드와 store identity만 canonical evidence에
기록합니다.

Candidate failure는 stable cutover나 cleanup 근거가 아닙니다. 실패한 v111 container,
state와 reports를 먼저 보존하고 v109 MCP, v110 incident state, stable WebDAV와
`/opt/cardrag/current`의 before/after identity가 같은지 다시 검사합니다.

## 4. release와 stable cutover

Release workflow는 `1.0.12`만 허용하며 다른 version은 evidence conditional 앞에서
즉시 거부합니다. Candidate source commit 이후 tag commit의 변경은
`release-evidence/v1.0.12/**`만 허용됩니다. 이 디렉터리의 runtime identity는 1.0.12이고
봉인된 data contract/channel 필드는 계속 v1.0.11이어야 합니다. annotated
`v1.0.12` tag, successful exact
commit CI, canonical receipt SHA와 Worker/MCP OCI index digest가 모두 있어야 Docker Hub
copy와 signing을 시작합니다.

Candidate receipt 합격은 candidate volume을 stable service에 직접 mount할 권한이 아닙니다.
Stable 전환에는 다음 exact offline mapping을 사용합니다.

| stopped source | 새 stable destination | copy 계약 |
|---|---|---|
| `cardrag-worker-v112-candidate-state` | `cardrag-worker-v111-state` | 전체 독립 byte copy + tree digest + 모든 SQLite integrity |
| `cardrag-mcp-v112-candidate-state` | `cardrag-mcp-v111-state` | 전체 독립 byte copy + tree digest + 모든 SQLite integrity |
| `cardrag-worker-v112-candidate-codex-home/auth.json` | `cardrag-worker-v111-codex-home/auth.json` | 새 volume에 `auth.json`만 descriptor copy |

먼저 candidate Worker가 terminal인지 확인하고 candidate MCP를 stop합니다. 그 뒤 세 source
volume 각각에 대해 `docker ps --quiet --filter volume=<exact-name>` 출력이 비어 있어야
합니다. Candidate Worker 또는 MCP 중 하나라도 running이면 copy를 시작하지 않습니다.
세 stable destination은 모두 존재하지 않아야 하며, 이전 실패에서 남은 destination을
재사용하거나 merge하지 않습니다. Verifier의 descriptor identity 재확인은 live snapshot을
대체하지 않으므로 source와 destination에는 전체 scan 동안 다른 writer가 없어야 합니다.

```bash
set -euo pipefail
candidate_volumes=(
  cardrag-worker-v112-candidate-state
  cardrag-worker-v112-candidate-codex-home
  cardrag-mcp-v112-candidate-state
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
  cardrag-worker-v112-candidate-state:cardrag-worker-v111-state \
  cardrag-mcp-v112-candidate-state:cardrag-mcp-v111-state
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

Codex stable destination은 exact v1.0.12 Worker image의 mode-0700/UID 10001 mountpoint
copy-up으로 초기화합니다. [v1.0.10 migration의 hardened descriptor copy](V1_0_10_MIGRATION.md#3-codex-oauthhome-분리-resume-전-write-stop)를
그대로 사용하되 source volume/path를
`cardrag-worker-v112-candidate-codex-home:/source:ro`와 `/source/auth.json`, destination을
`cardrag-worker-v111-codex-home:/var/lib/cardrag-codex-home`으로 고정합니다. 일반 `cp`나
Codex home 전체 복사는 금지합니다. 복사 뒤에는 아래 검증이 성공해야 하며 출력에는
credential content나 digest가 포함되지 않습니다.

```bash
source_codex_mount=$(docker volume inspect --format '{{.Mountpoint}}' \
  cardrag-worker-v112-candidate-codex-home)
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
2. immutable `/opt/cardrag/v1.0.12` 설치 tree와 systemd unit을 준비하되 `current`는
   아직 바꾸지 않습니다. Worker/MCP unit의 env 경로는 각각
   `/etc/cardrag/worker.env`, `/etc/cardrag/mcp.env`로 고정합니다.
3. 새 `cardrag-mcp-v111-state`만 mount한 v1.0.12 MCP를 shared stable WebDAV channel에
   연결해 기존 v4 readback과 readiness/tool smoke를 통과시킵니다.
4. 새 `cardrag-worker-v111-state`와 `cardrag-worker-v111-codex-home`만 mount한 v1.0.12
   Worker를 stable channel에서 한 번 실행하고 새 v5 generation readback을 확인합니다.
   stable publication approval과 OCR cache write/remote GC approval은 각각 독립된 값입니다.
5. 그 뒤에만 `/opt/cardrag/current`, 서비스 env와 timer를 v1.0.12로 전환합니다.

여기서 `v111` stable volume suffix는 데이터 계약 identity이고,
`release-evidence/v1.0.12/` 및 application/image/install identity는 runtime release
v1.0.12입니다. Volume 이름을 runtime 버전에 맞춘다는 이유로 바꾸거나 candidate volume을
stable에 직접 mount하지 않습니다.

현재 서버의 `/opt/cardrag/current`와 설치된 unit/env가 실제 실행 중인 v1.0.9 candidate
MCP와 일치하지 않으므로, cutover 전에 이 drift를 별도 inventory로 봉인하고 한 번의
원자적 변경으로 교정해야 합니다. env 파일을 부분적으로 섞거나 existing volume 이름을
바꾸지 않습니다.

## 5. rollback과 cleanup 금지선

Stable v5 게시 전 실패하면 v1.0.9 MCP/Worker image와 기존 stable pointer를 그대로
유지합니다. Stable v5 게시 뒤에는 v1.0.9 MCP로 단순 downgrade하지 않습니다. v1.0.12
dual-reader MCP를 유지하고 봉인된 last-good v4를 활성화한 뒤 v1.0.12 Worker를 멈춰
추가 publication을 차단합니다.

Rollback 기간 동안 v1.0.9 image/volume, v1.0.10 incident state, v1.0.11 source와 v1.0.12
candidate state, stable/candidate WebDAV generation과 `/opt/cardrag` 설치 tree를 삭제하지
않습니다.
최소 두 번의 연속 stable 성공, MCP readback, backup 복구시험과 exact reference-free
inventory가 모두 있어야 cleanup을 별도 승인할 수 있습니다. Remote GC는 stable
publication과 별도 승인입니다.
