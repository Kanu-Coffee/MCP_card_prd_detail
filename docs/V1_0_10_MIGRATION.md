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
| Codex home volume | `cardrag-worker-v110-codex-home` |
| MCP volume | `cardrag-mcp-v110-state` |
| MCP bind | `127.0.0.1:18010` |
| remote OCR cache | verified GET only (`read-only`) |
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
전체 Compose JSON을 터미널, 파일 또는 `tee`로 보내지 않고 아래 비민감 assertion의
boolean 결과만 출력합니다.

먼저 canonical candidate acceptance validation에서 role별 OCI index reference와 platform
config digest를 옮깁니다. tag, local image, public repository, short digest는 허용하지 않습니다.
아래 네 변수는 같은 shell과 이후 candidate 명령 전체에서 유지해야 합니다.

```bash
set -euo pipefail
: "${CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST:?receipt-bound Worker index digest is required}"
: "${CARDRAG_CANDIDATE_WORKER_CONFIG_DIGEST:?receipt-bound Worker config digest is required}"
: "${CARDRAG_CANDIDATE_MCP_IMAGE_DIGEST:?receipt-bound MCP index digest is required}"
: "${CARDRAG_CANDIDATE_MCP_CONFIG_DIGEST:?receipt-bound MCP config digest is required}"
[[ "$CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "$CARDRAG_CANDIDATE_MCP_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "$CARDRAG_CANDIDATE_WORKER_CONFIG_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "$CARDRAG_CANDIDATE_MCP_CONFIG_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
candidate_repository=ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate
CARDRAG_CANDIDATE_WORKER_IMAGE="$candidate_repository@$CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST"
CARDRAG_CANDIDATE_MCP_IMAGE="$candidate_repository@$CARDRAG_CANDIDATE_MCP_IMAGE_DIGEST"
export CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST CARDRAG_CANDIDATE_WORKER_CONFIG_DIGEST
export CARDRAG_CANDIDATE_MCP_IMAGE_DIGEST CARDRAG_CANDIDATE_MCP_CONFIG_DIGEST

docker compose \
  -f deploy/worker/compose.yaml \
  -f deploy/worker/compose.candidate.yaml \
  -f deploy/worker/compose.cache-seed.yaml config --format json | jq -e \
  --arg expected_image "$CARDRAG_CANDIDATE_WORKER_IMAGE" '
    .name == "cardrag-v110-candidate" and
    .services.worker.image == $expected_image and
    (.services.worker.build? == null) and
    .services.worker.pull_policy == "always" and
    .services.worker.user == "10001:10001" and
    .services.worker.read_only == true and
    .services.worker.cap_drop == ["ALL"] and
    .services.worker.security_opt == [
      "no-new-privileges:true", "seccomp=unconfined", "apparmor=unconfined"
    ] and
    .services.worker.environment.CARDRAG_CHANNEL == "candidate-v1.0.10" and
    .services.worker.environment.CARDRAG_OCR_CACHE_MODE == "read-only" and
    .services.worker.environment.CARDRAG_OCR_CACHE_PUBLICATION_APPROVED == "false" and
    .services.worker.environment.CARDRAG_COLLECT_REMOTE_GARBAGE == "false" and
    .services.worker.environment.CARDRAG_REMOTE_GC_APPROVED == "false" and
    .services.worker.environment.CARDRAG_ENABLED_ISSUERS == "kb,samsung,shinhan,woori" and
    .services.worker.environment.CARDRAG_CODEX_AUTH_ROOT == "/var/lib/cardrag-codex-home" and
    .services.worker.environment.CODEX_HOME == "/var/lib/cardrag-codex-home" and
    .services.worker.environment.HOME == "/var/lib/cardrag-codex-home/home" and
    .services.worker.environment.CARDRAG_EMBEDDING_DIMENSION == "4096" and
    .services.worker.environment.CARDRAG_WORKER_MAX_STATE_BYTES == "68719476736" and
    .services.worker.environment.CARDRAG_WORKER_RESERVED_FREE_SPACE_BYTES == "2147483648" and
    .services.worker.environment.CARDRAG_WORKER_MAX_VECTOR_SIDECAR_BYTES == "17179869184" and
    .services.worker.environment.CARDRAG_WORKER_MAX_SERVING_DATABASE_BYTES == "4294967296" and
    .services.worker.environment.CARDRAG_WORKER_MINIMUM_START_FREE_BYTES == "34359738368" and
    .volumes["worker-state"].name == "cardrag-worker-v110-state" and
    .volumes["codex-home"].name == "cardrag-worker-v110-codex-home" and
    .volumes["v109-worker-state"].name == "cardrag-worker-v109-state" and
    ([.services.worker.volumes[] |
      select(.target == "/var/lib/cardrag-worker")][0].source == "worker-state") and
    ([.services.worker.volumes[] |
      select(.target == "/var/lib/cardrag-codex-home")][0].source == "codex-home") and
    ([.services.worker.volumes[] |
      select(.target == "/mnt/cardrag-v109-state")][0].read_only == true)'

docker compose \
  -f deploy/mcp/compose.yaml \
  -f deploy/mcp/compose.candidate.yaml config --format json | jq -e \
  --arg expected_image "$CARDRAG_CANDIDATE_MCP_IMAGE" '
    .name == "cardrag-v110-candidate" and
    .services.mcp.image == $expected_image and
    (.services.mcp.build? == null) and
    .services.mcp.pull_policy == "always" and
    .services.mcp.user == "10001:10001" and
    .services.mcp.read_only == true and
    .services.mcp.cap_drop == ["ALL"] and
    .services.mcp.security_opt == ["no-new-privileges:true"] and
    .services.mcp.environment.CARDRAG_CHANNEL == "candidate-v1.0.10" and
    .services.mcp.environment.CARDRAG_MCP_MAX_VECTOR_BYTES == "1073741824" and
    .services.mcp.environment.CARDRAG_MCP_MAX_RESIDENT_VECTOR_BYTES == "1073741824" and
    .services.mcp.environment.CARDRAG_MCP_MAX_VECTOR_SIDECAR_BYTES == "17179869184" and
    .services.mcp.environment.CARDRAG_MCP_MAX_SERVING_DATABASE_BYTES == "4294967296" and
    .services.mcp.environment.CARDRAG_MCP_MAX_GENERATION_DOWNLOAD_BYTES == "34359738368" and
    .services.mcp.environment.CARDRAG_MCP_MAX_STATE_BYTES == "68719476736" and
    .services.mcp.environment.CARDRAG_MCP_RESERVED_FREE_SPACE_BYTES == "2147483648" and
    .services.mcp.environment.CARDRAG_MCP_EXHAUSTIVE_AUDIT_MAX_JOBS == "32" and
    .services.mcp.environment.CARDRAG_MCP_EXHAUSTIVE_AUDIT_MAX_TOTAL_BYTES == "2147483648" and
    .services.mcp.environment.CARDRAG_MCP_EXHAUSTIVE_AUDIT_MAX_ARTIFACT_BYTES == "268435456" and
    .services.mcp.environment.CARDRAG_MCP_RERANKER_AUDIT_MAX_JOBS == "1024" and
    .services.mcp.environment.CARDRAG_MCP_RERANKER_AUDIT_MAX_TOTAL_BYTES == "536870912" and
    .services.mcp.environment.CARDRAG_MCP_RERANKER_AUDIT_MAX_ARTIFACT_BYTES == "8388608" and
    .volumes["mcp-state"].name == "cardrag-mcp-v110-state" and
    .services.mcp.ports == [{
      mode: "ingress", host_ip: "127.0.0.1", target: 8000,
      published: "18010", protocol: "tcp"
    }]'
```

Worker source mount의 `read_only=true`, 두 destination volume 이름, project 이름,
channel, exact receipt-bound private image index, inherited build 없음, `pull_policy=always`,
UID/GID 10001, read-only rootfs, all-cap drop, no-new-privileges,
exact four-issuer set, port, `CARDRAG_OCR_CACHE_MODE=read-only`,
`CARDRAG_OCR_CACHE_PUBLICATION_APPROVED=false`,
`CARDRAG_COLLECT_REMOTE_GARBAGE=false`,
`CARDRAG_REMOTE_GC_APPROVED=false`, Qwen 4,096D, Worker capacity, MCP
legacy/resident/sidecar/DB/download/state/reserve 및 두 audit job/byte cap을 모두
assert합니다. 이 assertion이 성공한 sanitized effective-config 결과만 candidate acceptance
receipt의 config SHA 근거로 결속합니다. canonical effective-config evidence에는 위 image
reference와 config digest도 함께 기록합니다. candidate overlay는 public repository를
YAML에 고정하고 receipt-bound index digest 변수가 없으면 render를 거부하며 base의 local
`image`/`build` fallback을 제거합니다. capacity와 issuer 값을
ambient environment에서 상속하지 않고 release launch에 exact literal을 고정합니다.

## 3. Codex OAuth/home 분리 (resume 전 WRITE-STOP)

Worker state를 수정하기 전에 exact candidate image와 두 volume 이름을 고정합니다. 이 단계는
`cardrag-worker-v110-state`의 기존 `/codex` 및 `/home`을 read-only로 조사하며 파일 내용은
출력하지 않습니다. bounded scanner는 filename, mode, UID/GID, size와 검출된 token-form의
규칙 이름만 redacted JSON으로 남겨야 합니다. symlink, non-regular file, scan cap 초과 또는
`codex/auth.json` 외의 auth/token-shaped 결과가 있으면 복사·삭제하지 말고 사용자에게
목록만 보고합니다. `auth.json` 값, 일부 문자열, SHA-256은 터미널·journal·receipt에 남기지
않습니다.

새 volume은 exact candidate Worker image의 owned mode-0700 mountpoint를 copy-up하여 먼저
초기화합니다. source는 read-only, destination만 read-write, network/capability는 모두
차단합니다. 아래 copy runner는 source를 `O_NOFOLLOW` regular file로 열고 2 MiB 이하인지
검사한 뒤 UID/GID 10001 process가 mode 0600 temp file을 만들고 `fsync`/atomic replace합니다.
대상에는 이 한 파일만 만들며 logs, cache, sessions와 기존 `/home`은 복사하지 않습니다.

```bash
set -euo pipefail
: "${CARDRAG_CANDIDATE_WORKER_IMAGE:?exact receipt-bound Worker image is required}"
: "${CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST:?validated receipt index digest is required}"
: "${CARDRAG_CANDIDATE_WORKER_CONFIG_DIGEST:?validated receipt config digest is required}"
[[ "$CARDRAG_CANDIDATE_WORKER_IMAGE" =~ ^ghcr\.io/kanu-coffee/mcp-card-prd-detail-candidate@sha256:[0-9a-f]{64}$ ]]
[[ "$CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "$CARDRAG_CANDIDATE_WORKER_CONFIG_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
test "${CARDRAG_CANDIDATE_WORKER_IMAGE##*@}" = \
  "$CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST"
test "$(docker image inspect "$CARDRAG_CANDIDATE_WORKER_IMAGE" \
  --format '{{.Id}}')" = "$CARDRAG_CANDIDATE_WORKER_CONFIG_DIGEST"
docker image inspect "$CARDRAG_CANDIDATE_WORKER_IMAGE" \
  --format '{{json .RepoDigests}}' | jq -e \
  --arg expected "$CARDRAG_CANDIDATE_WORKER_IMAGE" \
  'index($expected) != null' >/dev/null
source_volume=cardrag-worker-v110-state
codex_volume=cardrag-worker-v110-codex-home

docker run --rm --interactive --pull never --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --user 10001:10001 \
  --entrypoint python \
  --volume "$source_volume:/source:ro" \
  --volume "$codex_volume:/var/lib/cardrag-codex-home" \
  "$CARDRAG_CANDIDATE_WORKER_IMAGE" - <<'PY'
import os
import stat
from pathlib import Path

source = Path("/source/codex/auth.json")
destination_root = Path("/var/lib/cardrag-codex-home")
destination_home = destination_root / "home"
destination = destination_root / "auth.json"
temporary = destination.with_name(".auth.json.migration")

def identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        value.st_mode,
        value.st_uid,
        value.st_gid,
        value.st_nlink,
    )

def require_private_directory(path: Path) -> None:
    value = path.lstat()
    if (
        not stat.S_ISDIR(value.st_mode)
        or stat.S_ISLNK(value.st_mode)
        or stat.S_IMODE(value.st_mode) != 0o700
        or (value.st_uid, value.st_gid) != (10001, 10001)
    ):
        raise SystemExit("destination_directory_metadata_invalid")

require_private_directory(destination_root)
require_private_directory(destination_home)
if {entry.name for entry in os.scandir(destination_root)} != {"home"}:
    raise SystemExit("destination_inventory_invalid")
source_stat = source.lstat()
if (
    not stat.S_ISREG(source_stat.st_mode)
    or stat.S_ISLNK(source_stat.st_mode)
    or stat.S_IMODE(source_stat.st_mode) != 0o600
    or (source_stat.st_uid, source_stat.st_gid) != (10001, 10001)
    or source_stat.st_nlink != 1
    or not 1 <= source_stat.st_size <= 2 * 1024 * 1024
):
    raise SystemExit("source_auth_metadata_invalid")
for path in (destination, temporary):
    try:
        path.lstat()
    except FileNotFoundError:
        pass
    else:
        raise SystemExit("destination_not_empty")
source_fd = os.open(source, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
try:
    before = os.fstat(source_fd)
    if identity(before) != identity(source_stat) or not stat.S_ISREG(before.st_mode):
        raise SystemExit("source_auth_identity_invalid")
    target_fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        while block := os.read(source_fd, 1024 * 1024):
            view = memoryview(block)
            while view:
                view = view[os.write(target_fd, view):]
        os.fchmod(target_fd, 0o600)
        os.fsync(target_fd)
    finally:
        os.close(target_fd)
    if identity(os.fstat(source_fd)) != identity(before):
        raise SystemExit("source_auth_changed_during_copy")
    os.replace(temporary, destination)
    directory_fd = os.open(
        destination_root,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    destination_fd = os.open(
        destination,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    try:
        copied = os.fstat(destination_fd)
        if (
            not stat.S_ISREG(copied.st_mode)
            or stat.S_IMODE(copied.st_mode) != 0o600
            or (copied.st_uid, copied.st_gid) != (10001, 10001)
            or copied.st_nlink != 1
            or copied.st_size != before.st_size
        ):
            raise SystemExit("destination_auth_metadata_invalid")
        os.lseek(source_fd, 0, os.SEEK_SET)
        while True:
            source_block = os.read(source_fd, 1024 * 1024)
            destination_block = os.read(destination_fd, 1024 * 1024)
            if source_block != destination_block:
                raise SystemExit("destination_auth_content_mismatch")
            if not source_block:
                break
        if identity(os.fstat(source_fd)) != identity(before):
            raise SystemExit("source_auth_changed_during_verification")
    finally:
        os.close(destination_fd)
finally:
    os.close(source_fd)
    temporary.unlink(missing_ok=True)
if {entry.name for entry in os.scandir(destination_root)} != {"auth.json", "home"}:
    raise SystemExit("destination_inventory_invalid")
print("codex-auth-copy-verified")
PY
```

그 다음 metadata만 검사하고 login-status 출력은 모두 버립니다. 성공 문구는 고정 문자열만
허용합니다. 실패하면 새 volume만 폐기할 수 있고 source `/codex`는 그대로이므로 rollback이
가능합니다.

```bash
set -euo pipefail
: "${CARDRAG_CANDIDATE_WORKER_IMAGE:?exact receipt-bound Worker image is required}"
: "${CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST:?validated receipt index digest is required}"
: "${CARDRAG_CANDIDATE_WORKER_CONFIG_DIGEST:?validated receipt config digest is required}"
[[ "$CARDRAG_CANDIDATE_WORKER_IMAGE" =~ ^ghcr\.io/kanu-coffee/mcp-card-prd-detail-candidate@sha256:[0-9a-f]{64}$ ]]
[[ "$CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "$CARDRAG_CANDIDATE_WORKER_CONFIG_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
test "${CARDRAG_CANDIDATE_WORKER_IMAGE##*@}" = \
  "$CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST"
test "$(docker image inspect "$CARDRAG_CANDIDATE_WORKER_IMAGE" \
  --format '{{.Id}}')" = "$CARDRAG_CANDIDATE_WORKER_CONFIG_DIGEST"
docker image inspect "$CARDRAG_CANDIDATE_WORKER_IMAGE" \
  --format '{{json .RepoDigests}}' | jq -e \
  --arg expected "$CARDRAG_CANDIDATE_WORKER_IMAGE" \
  'index($expected) != null' >/dev/null
codex_volume=cardrag-worker-v110-codex-home

docker run --rm --pull never --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --user 10001:10001 \
  --entrypoint python \
  --volume "$codex_volume:/var/lib/cardrag-codex-home:ro" \
  "$CARDRAG_CANDIDATE_WORKER_IMAGE" -c \
  'import os,stat; r="/var/lib/cardrag-codex-home"; h=f"{r}/home"; p=f"{r}/auth.json"; rs=os.stat(r,follow_symlinks=False); hs=os.stat(h,follow_symlinks=False); s=os.stat(p,follow_symlinks=False); assert stat.S_ISDIR(rs.st_mode) and stat.S_IMODE(rs.st_mode)==0o700 and (rs.st_uid,rs.st_gid)==(10001,10001); assert stat.S_ISDIR(hs.st_mode) and stat.S_IMODE(hs.st_mode)==0o700 and (hs.st_uid,hs.st_gid)==(10001,10001); assert stat.S_ISREG(s.st_mode) and stat.S_IMODE(s.st_mode)==0o600 and (s.st_uid,s.st_gid)==(10001,10001) and s.st_nlink==1; assert set(os.listdir(r))=={"auth.json","home"}; print("codex-auth-metadata-verified")'

docker run --rm --pull never --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --user 10001:10001 \
  --env CODEX_HOME=/var/lib/cardrag-codex-home \
  --env HOME=/var/lib/cardrag-codex-home/home \
  --volume "$codex_volume:/var/lib/cardrag-codex-home:ro" \
  --entrypoint /usr/local/bin/codex \
  "$CARDRAG_CANDIDATE_WORKER_IMAGE" login status >/dev/null 2>&1
echo codex-login-status-verified
```

**WRITE-STOP:** 여기까지 성공해도 이 문서 실행자는 기존 state를 삭제하지 않습니다. 정확한
두 고정 성공 문자열과 redacted `/codex`·`/home` audit 결과를 검토한 뒤에만 live migration
담당자가 auth cutover를 선언합니다. 최종 receipt-bound image와 candidate Compose의 별도
state/auth mount로 read-only login-status, Codex sandbox, 실제 OCR provider argv smoke가 모두
성공하고 Worker가 정지했음을 다시 확인한 경우에만 old state의 exact `/codex` subtree를
targeted 삭제합니다. `/home`은 audit 결과가 0이어도 이 승인으로 삭제하지 않습니다. 삭제
뒤 metadata-only로 legacy auth entry가 0인지, 새 auth volume inventory가 그대로인지 다시
확인하고 나서만 preserved run을 `resume`합니다. source는 이 cutover 선언 전까지 그대로
남으므로 검증 실패 시 old Compose로 rollback할 수 있습니다.

## 4. v1.0.9 PDF/OCR seed

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
candidate는 공용 native OCR cache를 GET-only로 사용합니다. cache miss에서 생성한 OCR은
candidate local checkpoint와 generation에만 결속하며 native-cache publication
transaction의 CAS, manifest, READY를 생성하거나 불완전 native entry의 READY를
repair하지 않습니다. 여기서 CAS 금지는 OCR resolver의
native-cache publication transaction을 뜻합니다. 성공 generation은 MCP의 source OCR
제공을 위해 같은 bytes를 전역 content-addressed object path에 idempotent하게 upload할
수 있지만, native manifest/READY가 없으면 그 object 자체는 공유 OCR cache entry가
아닙니다. 이 경계는
`CARDRAG_OCR_CACHE_MODE=read-only` 기본값, candidate Compose 강제값, Worker startup
validation과 worker-contract hash에 함께 결속됩니다. `read-write`는 stable channel과
별도 `CARDRAG_OCR_CACHE_PUBLICATION_APPROVED=true`가 모두 없으면 startup 전에
거부됩니다. stable generation publication approval은 이 권한을 대신하지 않습니다.

### 같은 run의 native OCR 중복 제거

동일 run의 서로 다른 document가 같은 PDF SHA/size/page count와 완전히 같은
`NativeOCRContract`를 가지면 native reuse key도 같습니다. Resolver는 provider 호출 전에
현재 run의 `documents/*/ocr/{,primary,fallback}/native-manifest.json`을 contract별로 한 번
색인하고, 요청된 key의 manifest와 `ocr.md`를 source, contract, canonical manifest,
전체 SHA/size/char count, page SHA, credential-token 규칙까지 다시 검증합니다. 따라서 패치
전에 성공한 document A가 실행 순서상 뒤에 있더라도 pending document B는 A의 완결 seal을
먼저 재사용합니다. adopted cache는 document ID가 receipt에 결속되므로 이 색인에 들어가지
않습니다.

같은 process의 `(run_id, native reuse key)`는 하나의 lock으로 remote lookup부터 최종
document-local seal까지 직렬화합니다. 여러 유효한 local seal의 OCR output이
비결정적으로 다르면 가장 낮은 `(output SHA-256, size)`를 선택하고 그 process의 후속
unbound duplicate를 같은 winner로 수렴시킵니다. 이미 게시된 generation의
`prior_local_native`가 document별 SHA/size를 결속한 cache-healing에서는 현재 local과 prior의
정확한 identity를 먼저 보존하고, sibling winner로 덮어쓰지 않습니다. 불완전 body-only 상태, 오래되거나 다른
source/contract, symlink/hardlink, hash 불일치는 miss이며 삭제하거나 원문을 로그에
노출하지 않습니다. credential 형태가 검출된 bytes는 miss로 낮추지 않고 systemic
failure로 중단합니다.

공유를 위한 두 번째 OCR body나 별도 persistent cache subtree는 만들지 않습니다. 완결
`ocr.md`와 `native-manifest.json` 자체가 유일한 durable restart boundary이고, runtime
색인/lock은 process memory에만 존재합니다. 따라서 추가 steady-state disk growth는 0이며
수명과 정리는 기존 run retention과 같습니다. Candidate의 remote 우선순위
`native -> adopted -> run-local native -> provider`와 `read-only` GET-only 계약도 그대로라
local cross-document hit에서 native CAS/manifest/READY PUT 또는 READY repair는 0건이고,
generation manifest의 `(ocr_cache_kind, ocr_reuse_key)`는 `(null, null)`로 유지됩니다.

## 5. candidate generation과 MCP

candidate Worker는 v1.0.10 이미지를 사용하고 candidate channel에만 publish합니다.
실행 로그에서 `Remote OCR cache access mode=read-only`를 확인하고, 신규 local OCR
성공 전후 정확한 remote reuse-key의 native manifest/READY 경로가 모두 동일한 GET
결과를 유지했는지 확인합니다. generation CAS publication은 이 control-path 증거와
분리해 기록합니다.
Worker는 corpus discovery/embedding 전에 두 allowlisted Qwen route의 credentialed live
preflight를 수행합니다. 2026-08-29 재검증에서는 `deepinfra`와 `nebius`가 모두 고정
24-sample·2회 반복·4,096D finite·cosine gate를 통과했습니다. 이때 확인된 OpenRouter
계약은 response model의 case-only canonicalization,
`max_prompt_tokens=null`일 때 양의 `context_length` 사용, pinned route에서
`require_parameters=false`입니다. 마지막 항목은 fallback 허용이 아니며 request의
`order`/`only`, `allow_fallbacks=false`와 response provider 검증을 함께 요구합니다.
설정 maximum token과 live metadata가 다르거나 어느 route라도 실패하면 corpus 처리 전에
중단합니다. 이 preflight pass만으로 full generation/gold 합격을 주장하지 않습니다.

Worker startup은 state directory를 만들기 전에 가장 가까운 기존 ancestor의 filesystem을
read-only로 검사하며 candidate 기본 free-space floor 32 GiB보다 작으면 provider와
WebDAV에 접근하지 않고 중단합니다. 이후 모든 derived view와 read-only embedding-cache
hit가 확정되면 embedding miss 다운로드 전에 다음 canonical decimal byte 한도로 v5
logical/peak growth를 검증합니다.

embedding `hits`/`misses`는 sidecar derived-view 행 기준이고, 같은 exact input의
`downloads`와 provider 입력은 unique cache key 기준입니다. 같은 key의 vector는 모든
해당 행에 streaming fan-out되며 retry/resume metrics는 마지막 완결 attempt만 기록합니다.

| 설정 | candidate 기본값 | 경계 |
|---|---:|---|
| `CARDRAG_WORKER_MAX_STATE_BYTES` | 68719476736 (64 GiB) | 기존 Worker state + 새 logical growth |
| `CARDRAG_WORKER_RESERVED_FREE_SPACE_BYTES` | 2147483648 (2 GiB) | peak write 뒤 남길 filesystem 공간 |
| `CARDRAG_WORKER_MAX_VECTOR_SIDECAR_BYTES` | 17179869184 (16 GiB) | 단일 sealed `vectors.f32` |
| `CARDRAG_WORKER_MAX_SERVING_DATABASE_BYTES` | 4294967296 (4 GiB) | 단일 sealed serving DB |
| `CARDRAG_WORKER_MINIMUM_START_FREE_BYTES` | 34359738368 (32 GiB) | provider/WebDAV 전 startup floor |

Worker와 candidate MCP volume이 같은 host filesystem을 사용하면 Worker의 PDF/OCR/cache,
DB, sidecar와 MCP가 별도로 download/stage/retain하는 PDF/source CAS, DB, sidecar를 각각
한 벌로 계산합니다. 두 runtime의 reserved-free-space 약속까지 합산해 startup floor와
host provisioning을 정해야 합니다. 32 GiB floor는 이 중 명백히 작은 host를 일찍
거부하는 값이며, corpus 기반 Worker preflight와 MCP quota/download gate를 대체하지
않습니다. 이 용량 gate는 v5 전용이고 v4 rollback artifact/reader 동작은 바뀌지 않습니다.

```bash
set -euo pipefail
: "${CARDRAG_CANDIDATE_WORKER_IMAGE:?exact receipt-bound Worker image is required}"
: "${CARDRAG_CANDIDATE_WORKER_CONFIG_DIGEST:?exact Worker config digest is required}"
: "${CARDRAG_PRESERVED_RUN_ID:?the audited interrupted run ID is required}"
[[ "$CARDRAG_PRESERVED_RUN_ID" =~ ^[0-9a-f]{32}$ ]]

# Fail closed before mutation: the exact run must exist in the read-only state DB,
# be resumable, and no Worker run may still be marked running.
docker run --rm -i --pull never --network none --read-only \
  --cap-drop ALL --security-opt no-new-privileges=true \
  --user 10001:10001 \
  --volume cardrag-worker-v110-state:/state:ro \
  --entrypoint python "$CARDRAG_CANDIDATE_WORKER_IMAGE" \
  - "$CARDRAG_PRESERVED_RUN_ID" <<'PY'
import os
import sqlite3
import sys
from pathlib import Path

run_id = sys.argv[1]
database_path = Path("/state/worker-state.sqlite3")
if any(os.path.lexists(f"{database_path}{suffix}") for suffix in ("-wal", "-shm")):
    raise SystemExit("worker-state-not-checkpointed")
connection = sqlite3.connect(
    "file:/state/worker-state.sqlite3?mode=ro&immutable=1",
    uri=True,
)
try:
    connection.execute("PRAGMA query_only=ON")
    row = connection.execute(
        "SELECT status FROM run WHERE run_id=?",
        (run_id,),
    ).fetchone()
    running = connection.execute(
        "SELECT COUNT(*) FROM run WHERE status='running'",
    ).fetchone()
    if row is None or row[0] not in {"failed", "interrupted"}:
        raise SystemExit("preserved-run-not-resumable")
    if running is None or running[0] != 0:
        raise SystemExit("worker-run-still-running")
finally:
    connection.close()
print("preserved-run-resume-verified")
PY

worker_container=cardrag-v110-candidate-worker-acceptance
docker compose \
  -f deploy/worker/compose.yaml \
  -f deploy/worker/compose.candidate.yaml \
  -f deploy/worker/compose.secrets.yaml \
  run --name "$worker_container" worker resume "$CARDRAG_PRESERVED_RUN_ID"
test "$(docker inspect --format '{{.Image}}' "$worker_container")" = \
  "$CARDRAG_CANDIDATE_WORKER_CONFIG_DIGEST"
docker image inspect "$CARDRAG_CANDIDATE_WORKER_IMAGE" \
  --format '{{json .RepoDigests}}' | jq -e \
  --arg expected "$CARDRAG_CANDIDATE_WORKER_IMAGE" 'index($expected) != null'
```

`run` 또는 인자 없는 `worker run`으로 바꾸면 새 run ID가 만들어져 기존 PDF/OCR seal과
chunk checkpoint의 explicit-resume 경계를 잃습니다. 위 read-only gate와 exact
`CARDRAG_PRESERVED_RUN_ID`를 그대로 사용합니다.

검증 컨테이너는 image identity evidence를 읽은 뒤에도 자동 삭제하지 않습니다. candidate
container cleanup은 별도 승인 대상으로 남깁니다. Worker metrics의
`runtime_container_image_id`와 `runtime_image_repo_digest`에는 위 두 관측값을 기록합니다.

성공 후 manifest와 READY에서 `cardrag.generation.v5`,
`cardrag.serving-db.v5`, Qwen 4,096D, all view counts와 `vectors.f32` 결속을
검사합니다. candidate MCP는 별도 volume과 18010 포트로 시작합니다.

```bash
docker compose \
  -f deploy/mcp/compose.yaml \
  -f deploy/mcp/compose.candidate.yaml \
  -f deploy/mcp/compose.secrets.yaml \
  up -d --wait
mcp_container=$(docker compose \
  -f deploy/mcp/compose.yaml \
  -f deploy/mcp/compose.candidate.yaml \
  -f deploy/mcp/compose.secrets.yaml ps -q mcp)
test -n "$mcp_container"
test "$(docker inspect --format '{{.Image}}' "$mcp_container")" = \
  "$CARDRAG_CANDIDATE_MCP_CONFIG_DIGEST"
docker image inspect "$CARDRAG_CANDIDATE_MCP_IMAGE" \
  --format '{{json .RepoDigests}}' | jq -e \
  --arg expected "$CARDRAG_CANDIDATE_MCP_IMAGE" 'index($expected) != null'
```

health/readiness, 8개 tool discovery, `search_contracts`, bundle/revision API,
legacy adapter와 source PDF range를 smoke합니다. exact response의 expected/scored
contract/row count가 같아야 합니다. MCP smoke의 `runtime_container_image_id`와
`runtime_image_repo_digest`에도 위 두 관측값을 기록합니다.

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

## 6. rollback와 운영 무변경

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
