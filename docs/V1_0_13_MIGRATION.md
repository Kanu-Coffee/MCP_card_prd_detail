# CardRAG v1.0.13 SIGBUS recovery candidate migration

이 문서는 v1.0.12 candidate의 SQLite SHM `SIGBUS`를 수정한 v1.0.13 exact image를
만들고, 사고 source를 보존한 채 새 volume에서 동일 run을 복구·resume하는 절차입니다.
v1.0.13은 `candidate-v1.0.11` 데이터 계약을 유지하는 runtime patch입니다.

## 1. Public source exact-image build

Candidate image는 공개된 source repository의 merge 전 exact PR-head 40-hex commit을
remote Git context로 사용합니다. 첫 producer는 local worktree, untracked file 또는
credential-bearing context를 읽지 않습니다.

```bash
set -euo pipefail
: "${CANDIDATE_SOURCE_COMMIT:?exact v1.0.13 PR-head commit is required}"
[[ "$CANDIDATE_SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ ]]
test "$(docker buildx version | awk '{print $2}')" = "v0.36.1"
mapfile -t buildkit_versions < <(
  docker buildx inspect | sed -n 's/^[[:space:]]*BuildKit version: //p'
)
((${#buildkit_versions[@]} == 1))
test "${buildkit_versions[0]}" = "v0.32.2"

candidate_repository=ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate
source_context="https://github.com/Kanu-Coffee/MCP_card_prd_detail.git#$CANDIDATE_SOURCE_COMMIT"
build_metadata_root=$(mktemp -d /tmp/cardrag-v113-build-metadata.XXXXXX)
for role in worker mcp; do
  role_metadata="$build_metadata_root/$role.json"
  docker buildx build \
    --platform linux/amd64 \
    --target "$role" \
    --build-arg APP_VERSION=1.0.13 \
    --build-arg "VCS_REF=$CANDIDATE_SOURCE_COMMIT" \
    --build-arg PYTHON_DEV_IMAGE=cgr.dev/chainguard/python:latest-dev@sha256:4e2adecf67a1d18773c55b5526b47436392b9816ae6b8d92575979a2ab9de8b2 \
    --build-arg PYTHON_RUNTIME_IMAGE=cgr.dev/chainguard/python:latest@sha256:f47d995d001c1f949d560b1158d7f3ae556aad75a1044e72a125c900c1f05332 \
    --build-arg UV_IMAGE=ghcr.io/astral-sh/uv:0.8.17@sha256:e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1 \
    --build-arg CODEX_VERSION=0.151.0 \
    --build-arg CODEX_SHA256=605b4b183f22c645f5def63a5b7191767407fb66a6feaec4eaf10b5b7e0058f6 \
    --attest type=provenance,mode=max,version=v0.2 \
    --attest type=sbom,generator=docker.io/docker/buildkit-syft-scanner:stable-1@sha256:ae4f3b554449e7e25548e7d8ccc029d17357348e30c6e3df01b92bc93654d6a9 \
    --metadata-file "$role_metadata" \
    --output "type=registry,name=$candidate_repository:candidate-v1.0.13-$role-$CANDIDATE_SOURCE_COMMIT,oci-mediatypes=true,oci-artifact=true" \
    "$source_context"
done
printf 'build metadata: %s\n' "$build_metadata_root"
```

허용 build arg는 위 일곱 개뿐입니다. `--build-context`, local/SSH input, alternate
Dockerfile, entitlement와 `--secret`은 금지합니다. BuildKit raw provenance가 공개 Git
fetch용 두 Git auth ID의 optional 내장 선언을 기록할 수 있습니다. 이 선언 자체는 token 값의 미전달을 증명하지 않으므로
producer command와 실행 기록에서도 credential 전달이 없음을 별도로 확인합니다.

이 수동 producer는 외부 trust boundary입니다. raw SLSA statement만으로 producer
identity가 서명되는 것은 아닙니다. OCI index, linux/amd64 platform/config/attestation
digest, raw provenance/SBOM, exact source와 runtime identity가 서로 일치하지 않으면
기술적 release blocker입니다. 두 role의 public index digest와 platform config digest는
별도로 봉인하며 이후 모든 helper와 Compose는 tag가 아닌 index digest를 사용합니다.

다음 gate는 registry raw JSON, Buildx metadata와 local pull을 교차검증합니다. Index에는
정확히 linux/amd64 application manifest 하나와 그 manifest를 subject로 하는 attestation
manifest 하나가 있어야 합니다. 출력한 네 digest 값과 metadata directory는 배포 기록에
보존합니다. `containerimage.digest` metadata는 필수입니다. Docker driver에 따라
`containerimage.config.digest` metadata가 생략될 수 있으므로, 이 필드는 존재하면 raw
platform manifest의 config digest와 일치해야 하고 raw manifest 검증 자체는 항상 필수입니다.

```bash
set -euo pipefail
: "${repository_root:?absolute repository root is required}"
: "${build_metadata_root:?build metadata directory is required}"
: "${CANDIDATE_SOURCE_COMMIT:?exact v1.0.13 PR-head commit is required}"
[[ "$repository_root" = /* && "$repository_root" != */ ]]
[[ "$CANDIDATE_SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ ]]
test "$(git -C "$repository_root" rev-parse HEAD)" = "$CANDIDATE_SOURCE_COMMIT"
test -z "$(git -C "$repository_root" status --porcelain=v1 --untracked-files=all)"
for policy_relative in \
  .github/scripts/validate-strict-json.py \
  .github/scripts/validate-candidate-oci-index.jq \
  .github/scripts/validate-candidate-platform-manifest.jq \
  .github/scripts/validate-candidate-attestation-manifest.jq \
  .github/scripts/validate-candidate-provenance.jq \
  .github/scripts/validate-candidate-sbom.jq; do
  policy_path="$repository_root/$policy_relative"
  test -f "$policy_path" && test ! -L "$policy_path"
  test "$(stat --format='%h' "$policy_path")" = "1"
  policy_git_sha256=$(git -C "$repository_root" show \
    "$CANDIDATE_SOURCE_COMMIT:$policy_relative" | sha256sum | awk '{print $1}')
  test "$(sha256sum "$policy_path" | awk '{print $1}')" = "$policy_git_sha256"
done
candidate_repository=ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate
registry_token=$(curl --fail --silent --show-error \
  'https://ghcr.io/token?scope=repository:kanu-coffee/mcp-card-prd-detail-candidate:pull' |
  jq -er '.token | select(type == "string" and length > 0)')

for role in worker mcp; do
  tag="$candidate_repository:candidate-v1.0.13-$role-$CANDIDATE_SOURCE_COMMIT"
  index_json="$build_metadata_root/$role-index.json"
  platform_json="$build_metadata_root/$role-platform.json"
  attestation_json="$build_metadata_root/$role-attestation.json"
  metadata_json="$build_metadata_root/$role.json"

  test -f "$metadata_json"
  docker buildx imagetools inspect "$tag" --raw >"$index_json"
  python3 "$repository_root/.github/scripts/validate-strict-json.py" "$index_json"
  index_digest="sha256:$(sha256sum "$index_json" | awk '{print $1}')"
  observed_index_digest=$(docker buildx imagetools inspect "$tag" \
    --format '{{json .Manifest}}' | jq -er '.digest')
  test "$observed_index_digest" = "$index_digest"
  test "$(jq -er '."containerimage.digest"' "$metadata_json")" = "$index_digest"

  platform_digest=$(jq -er '
    [.manifests[] | select(
      .mediaType == "application/vnd.oci.image.manifest.v1+json" and
      .platform == {architecture:"amd64",os:"linux"}
    )] | if length == 1 then .[0].digest else error("linux/amd64 manifest count") end
  ' "$index_json")
  attestation_digest=$(jq -er '
    [.manifests[] | select(
      .mediaType == "application/vnd.oci.image.manifest.v1+json" and
      .platform == {architecture:"unknown",os:"unknown"}
    )] | if length == 1 then .[0].digest else error("attestation manifest count") end
  ' "$index_json")
  jq -e --arg platform_digest "$platform_digest" \
    --arg attestation_digest "$attestation_digest" \
    -f "$repository_root/.github/scripts/validate-candidate-oci-index.jq" \
    "$index_json" >/dev/null

  docker buildx imagetools inspect "$candidate_repository@$platform_digest" \
    --raw >"$platform_json"
  python3 "$repository_root/.github/scripts/validate-strict-json.py" "$platform_json"
  config_digest=$(jq -er '.config.digest' "$platform_json")
  if metadata_config_digest=$(jq -er '
    ."containerimage.config.digest" | select(type == "string")
  ' "$metadata_json"); then
    test "$metadata_config_digest" = "$config_digest"
  fi
  jq -e --arg config_digest "$config_digest" \
    -f "$repository_root/.github/scripts/validate-candidate-platform-manifest.jq" \
    "$platform_json" >/dev/null

  docker buildx imagetools inspect "$candidate_repository@$attestation_digest" \
    --raw >"$attestation_json"
  python3 "$repository_root/.github/scripts/validate-strict-json.py" "$attestation_json"
  jq -e --arg platform_digest "$platform_digest" \
    -f "$repository_root/.github/scripts/validate-candidate-attestation-manifest.jq" \
    "$attestation_json" >/dev/null

  provenance_digest=$(jq -er '
    [.layers[] | select(
      .mediaType == "application/vnd.in-toto+json" and
      .annotations["in-toto.io/predicate-type"] ==
        "https://slsa.dev/provenance/v0.2"
    )] | if length == 1 then .[0].digest else error("provenance layer count") end
  ' "$attestation_json")
  provenance_size=$(jq -er --arg digest "$provenance_digest" '
    .layers[] | select(.digest == $digest) | .size
  ' "$attestation_json")
  sbom_digest=$(jq -er '
    [.layers[] | select(
      .mediaType == "application/vnd.in-toto+json" and
      .annotations["in-toto.io/predicate-type"] == "https://spdx.dev/Document"
    )] | if length == 1 then .[0].digest else error("SBOM layer count") end
  ' "$attestation_json")
  sbom_size=$(jq -er --arg digest "$sbom_digest" '
    .layers[] | select(.digest == $digest) | .size
  ' "$attestation_json")
  provenance_json="$build_metadata_root/$role-provenance.json"
  sbom_json="$build_metadata_root/$role-sbom.json"
  for layer in \
    "$provenance_digest|$provenance_size|$provenance_json" \
    "$sbom_digest|$sbom_size|$sbom_json"; do
    IFS='|' read -r layer_digest layer_size layer_path <<<"$layer"
    [[ "$layer_digest" =~ ^sha256:[0-9a-f]{64}$ && "$layer_size" =~ ^[1-9][0-9]*$ ]]
    curl --fail --silent --show-error --location \
      --header "Authorization: Bearer $registry_token" \
      "https://ghcr.io/v2/kanu-coffee/mcp-card-prd-detail-candidate/blobs/$layer_digest" \
      --output "$layer_path"
    test "$(stat --format='%s' "$layer_path")" = "$layer_size"
    test "sha256:$(sha256sum "$layer_path" | awk '{print $1}')" = "$layer_digest"
  done
  python3 "$repository_root/.github/scripts/validate-strict-json.py" \
    "$provenance_json" "$sbom_json"
  platform_digest_hex=${platform_digest#sha256:}
  source_uri="https://github.com/Kanu-Coffee/MCP_card_prd_detail.git#$CANDIDATE_SOURCE_COMMIT"
  jq -e --arg source_commit "$CANDIDATE_SOURCE_COMMIT" \
    --arg source_uri "$source_uri" --arg platform_digest_hex "$platform_digest_hex" \
    --arg role "$role" \
    -f "$repository_root/.github/scripts/validate-candidate-provenance.jq" \
    "$provenance_json" >/dev/null
  jq -e --arg source_commit "$CANDIDATE_SOURCE_COMMIT" \
    --arg platform_digest_hex "$platform_digest_hex" --arg role "$role" \
    -f "$repository_root/.github/scripts/validate-candidate-sbom.jq" \
    "$sbom_json" >/dev/null

  exact_image="$candidate_repository@$index_digest"
  docker pull --platform linux/amd64 "$exact_image" >/dev/null
  test "$(docker image inspect "$exact_image" --format \
    '{{ index .Config.Labels "org.opencontainers.image.version" }}')" = "1.0.13"
  test "$(docker image inspect "$exact_image" --format \
    '{{ index .Config.Labels "org.opencontainers.image.revision" }}')" = \
    "$CANDIDATE_SOURCE_COMMIT"
  test "$(docker image inspect "$exact_image" --format '{{.Config.User}}')" = "10001:10001"
  test "$(docker image inspect "$exact_image" --format '{{json .Config.Entrypoint}}')" = \
    "[\"cardrag-$role\"]"

  case "$role" in
    worker)
      export CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST="$index_digest"
      export CARDRAG_CANDIDATE_WORKER_CONFIG_DIGEST="$config_digest"
      ;;
    mcp)
      export CARDRAG_CANDIDATE_MCP_IMAGE_DIGEST="$index_digest"
      export CARDRAG_CANDIDATE_MCP_CONFIG_DIGEST="$config_digest"
      ;;
  esac
  printf '%s index=%s platform=%s config=%s attestation=%s\n' \
    "$role" "$index_digest" "$platform_digest" "$config_digest" "$attestation_digest"
done
```

## 2. 격리와 보존 경계

| 항목 | v1.0.13 candidate |
|---|---|
| application/runtime/OCI label | `1.0.13` |
| source branch | `codex/cardrag-v1.0.13` |
| data/publication channel | `candidate-v1.0.11` |
| Compose project | `cardrag-v113-candidate` |
| Worker state | `cardrag-worker-v113-candidate-state` |
| Codex home | `cardrag-worker-v113-candidate-codex-home` |
| MCP state | `cardrag-mcp-v113-candidate-state` |
| MCP bind | `127.0.0.1:18013` |
| preserved run | `1f1763a9cd474a81952a6eb6ffb6e397` |
| native OCR cache | verified GET only (`read-only`) |
| stable runtime | v1.0.9, 변경 없음 |

다음 사고 증거는 write, restart, checkpoint 또는 cleanup하지 않습니다.

- `cardrag-v112-candidate-worker-acceptance` container;
- `cardrag-worker-v112-candidate-state`와 v1.0.12 Codex volume;
- host apport/core dump와 forensic report;
- v1.0.12 image/index digest와 source revision
  `e40dc2577541438ab9a87db7b2d18801fae1b24f`.

Live Worker DB를 직접 열던 cron monitor는 이미 disable된 상태여야 합니다. v1.0.13
운영 관측은 container/journal log와 terminal result를 사용합니다. Source와 destination
어느 쪽에도 별도 SQLite reader가 붙어 있으면 이 절차를 시작하지 않습니다.

## 3. Offline recovery gate

### 3.1 사고 source와 exact image 결속

다음 gate는 source container가 정확히 exit 135/OOM false이고 source volume을 실행 중인
container가 mount하지 않음을 확인합니다. Destination 세 volume은 새로 만들어야 하며
기존 volume을 merge하거나 비우고 재사용하지 않습니다.

```bash
set -euo pipefail
: "${CANDIDATE_SOURCE_COMMIT:?exact v1.0.13 PR-head commit is required}"
: "${CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST:?v1.0.13 Worker index digest is required}"
: "${CARDRAG_CANDIDATE_WORKER_CONFIG_DIGEST:?v1.0.13 Worker config digest is required}"
: "${CARDRAG_CANDIDATE_MCP_IMAGE_DIGEST:?v1.0.13 MCP index digest is required}"
: "${CARDRAG_CANDIDATE_MCP_CONFIG_DIGEST:?v1.0.13 MCP config digest is required}"
[[ "$CANDIDATE_SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ ]]
[[ "$CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "$CARDRAG_CANDIDATE_WORKER_CONFIG_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "$CARDRAG_CANDIDATE_MCP_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "$CARDRAG_CANDIDATE_MCP_CONFIG_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]

incident_container=cardrag-v112-candidate-worker-acceptance
incident_repository=ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate
incident_index_digest=sha256:1e5ed2f45eb230d4581803bff504714da28f559adc571a4e13d9b3f543eb0469
incident_platform_digest=sha256:2b5afe1302145a85a2f2a7dc2b282094519f933d5d01d28229056cc2872bfcc7
incident_image="$incident_repository@$incident_index_digest"
source_state=cardrag-worker-v112-candidate-state
source_codex=cardrag-worker-v112-candidate-codex-home
destination_state=cardrag-worker-v113-candidate-state
destination_codex=cardrag-worker-v113-candidate-codex-home
destination_mcp=cardrag-mcp-v113-candidate-state
preserved_run_id=1f1763a9cd474a81952a6eb6ffb6e397
candidate_repository=ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate
candidate_worker_image="$candidate_repository@${CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST}"
candidate_mcp_image="$candidate_repository@${CARDRAG_CANDIDATE_MCP_IMAGE_DIGEST}"

incident_inspect=$(docker inspect "$incident_container")
jq -e \
  --arg image "$incident_image" \
  --arg index_digest "$incident_index_digest" \
  --arg platform_digest "$incident_platform_digest" '
    type == "array" and length == 1 and
    .[0].State.Status == "exited" and .[0].State.ExitCode == 135 and
    .[0].State.OOMKilled == false and .[0].RestartCount == 0 and
    .[0].Config.Image == $image and .[0].Image == $index_digest and
    .[0].ImageManifestDescriptor == {
      mediaType:"application/vnd.oci.image.manifest.v1+json",
      digest:$platform_digest,
      size:4495,
      platform:{architecture:"amd64",os:"linux"}
    } and
    .[0].Config.User == "10001:10001" and
    .[0].Config.Labels["org.opencontainers.image.version"] == "1.0.12" and
    .[0].Config.Labels["org.opencontainers.image.revision"] ==
      "e40dc2577541438ab9a87db7b2d18801fae1b24f" and
    .[0].Config.Labels["com.docker.compose.project"] == "cardrag-v112-candidate" and
    .[0].Config.Labels["com.docker.compose.service"] == "worker" and
    .[0].Config.Labels["com.docker.compose.oneoff"] == "True" and
    ([.[0].Mounts[] | select(.Type == "volume") |
      {Name,Destination,RW}] | sort_by(.Destination)) == [
        {Name:"cardrag-worker-v112-candidate-codex-home",
         Destination:"/var/lib/cardrag-codex-home",RW:true},
        {Name:"cardrag-worker-v112-candidate-state",
         Destination:"/var/lib/cardrag-worker",RW:true}
      ] and
    ([.[0].Mounts[] | select(.Type == "bind") |
      {Source,Destination,RW}] | sort_by(.Destination)) == [
        {Source:"/etc/cardrag/secrets/openrouter_api_key",
         Destination:"/run/secrets/openrouter_api_key",RW:false},
        {Source:"/etc/cardrag/secrets/webdav_password",
         Destination:"/run/secrets/webdav_password",RW:false},
        {Source:"/etc/cardrag/secrets/webdav_username",
         Destination:"/run/secrets/webdav_username",RW:false}
      ]
  ' <<<"$incident_inspect" >/dev/null

for identity in \
  "worker|$candidate_worker_image|$CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST|$CARDRAG_CANDIDATE_WORKER_CONFIG_DIGEST" \
  "mcp|$candidate_mcp_image|$CARDRAG_CANDIDATE_MCP_IMAGE_DIGEST|$CARDRAG_CANDIDATE_MCP_CONFIG_DIGEST"; do
  IFS='|' read -r role image index_digest config_digest <<<"$identity"
  image_inspect=$(docker image inspect "$image")
  jq -e --arg image "$image" --arg index_digest "$index_digest" \
    --arg config_digest "$config_digest" --arg role "$role" \
    --arg revision "$CANDIDATE_SOURCE_COMMIT" '
      type == "array" and length == 1 and
      (.[0].Id == $index_digest or .[0].Id == $config_digest) and
      (.[0].RepoDigests | index($image)) != null and
      .[0].Config.User == "10001:10001" and
      .[0].Config.Entrypoint == ["cardrag-" + $role] and
      .[0].Config.Labels["org.opencontainers.image.version"] == "1.0.13" and
      .[0].Config.Labels["org.opencontainers.image.revision"] == $revision
    ' <<<"$image_inspect" >/dev/null
done

for volume in "$source_state" "$source_codex"; do
  docker volume inspect "$volume" >/dev/null
  test -z "$(docker ps --quiet --filter "volume=$volume")"
done
for volume in "$destination_state" "$destination_codex" "$destination_mcp"; do
  if docker volume inspect "$volume" >/dev/null 2>&1; then
    printf 'v113 destination already exists: %s\n' "$volume" >&2
    exit 1
  fi
done

docker_root=$(docker info --format '{{.DockerRootDir}}')
available_before_copy=$(df --output=avail -B1 "$docker_root" | awk 'NR==2 {print $1}')
[[ "$available_before_copy" =~ ^[0-9]+$ ]]
((available_before_copy >= 43002721764))

for volume in "$destination_state" "$destination_codex" "$destination_mcp"; do
  docker volume create "$volume" >/dev/null
  test -z "$(docker ps --quiet --filter "volume=$volume")"
done
```

### 3.2 State와 Codex auth 복사

State copy helper는 source를 read-only, destination만 read-write로 mount하고 network,
capability와 privilege escalation을 모두 차단합니다. 다음 조건을 모두 만족한 sealed
copy helper만 사용합니다.

1. Source와 destination root, 모든 path component를 `lstat`/`O_NOFOLLOW`로 검사하고
   symlink, special node, hardlink, cross-filesystem entry와 UID/GID 불일치를 거부합니다.
2. Main DB와 `worker-state.sqlite3-wal`은 stopped source의 같은 snapshot에서 byte-for-byte
   복사하고 copy 전후 size/SHA-256을 대조합니다.
3. `worker-state.sqlite3-shm`만 destination에서 제외합니다. SHM은 transient
   wal-index이며, 알려진 사고 상태의 32 KiB 파일을 새 runtime에 전달하지 않습니다.
4. 각 file과 directory를 `fsync`하고 destination inventory와 digest를 봉인합니다.
5. Source는 어떤 시점에도 writable로 mount하거나 SQLite로 열지 않습니다.

일반 `tools/cardrag_offline_volume_verify.py state`를 recovery 전 source에 적용하지
않습니다. 그 verifier는 정상 checkpoint snapshot을 위해 WAL/SHM을 거부하고
`immutable=1`로 main DB만 읽으므로, 이번 사고의 아직 checkpoint되지 않은 WAL을
검증할 도구가 아닙니다.

Codex volume에서는 source 전체가 아니라 mode 0600, UID/GID 10001:10001인 bounded
`auth.json`만 destination에 atomic copy합니다. Destination의 mode 0700 `home/`은 비어
있어야 하며 token content나 digest를 log에 출력하지 않습니다.

복사에는 PR-head에 포함된 incident-specific helper만 사용합니다. Working tree의 도구
bytes를 exact PR-head Git blob SHA-256과 먼저 결속하고, 그 단일 helper file만
read-only mount합니다. 전체 mutable `tools/` directory를 mount하면
Python import shadowing이 가능하므로 금지합니다. Production CLI의 owner는
10001:10001로 고정되어 있습니다. State CLI는
사고 source의 main DB 3,713,409,024 bytes, WAL 19,071,512 bytes, SHM 32,768 bytes,
regular file 15,872개, directory entry 10,422개와 total regular bytes 8,643,016,164를
모두 exact하게 검사합니다.

```bash
set -euo pipefail
: "${repository_root:?absolute repository root is required}"
: "${CANDIDATE_SOURCE_COMMIT:?exact v1.0.13 PR-head commit is required}"
: "${source_state:?offline v1.0.12 state volume is required}"
: "${destination_state:?new v1.0.13 state volume is required}"
: "${source_codex:?offline v1.0.12 Codex volume is required}"
: "${destination_codex:?new v1.0.13 Codex volume is required}"
: "${candidate_worker_image:?exact v1.0.13 Worker digest reference is required}"
[[ "$repository_root" = /* && "$repository_root" != */ ]]
[[ "$CANDIDATE_SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ ]]
[[ "$candidate_worker_image" =~ ^ghcr\.io/kanu-coffee/mcp-card-prd-detail-candidate@sha256:[0-9a-f]{64}$ ]]

recovery_copy_relative=tools/cardrag_v113_recovery_copy.py
recovery_copy="$repository_root/$recovery_copy_relative"
test -f "$recovery_copy"
test ! -L "$recovery_copy"
test "$(stat --format='%a %h' "$recovery_copy")" = "644 1"
git -C "$repository_root" cat-file -e "$CANDIDATE_SOURCE_COMMIT^{commit}"
test "$(git -C "$repository_root" ls-tree "$CANDIDATE_SOURCE_COMMIT" \
  "$recovery_copy_relative" | awk '{print $1}')" = "100644"
git_tool_sha256=$(
  git -C "$repository_root" show \
    "$CANDIDATE_SOURCE_COMMIT:$recovery_copy_relative" | sha256sum | awk '{print $1}'
)
disk_tool_sha256=$(sha256sum "$recovery_copy" | awk '{print $1}')
test "$disk_tool_sha256" = "$git_tool_sha256"

state_copy_receipt=$(docker run --rm --pull never --network none --read-only \
  --cap-drop ALL --security-opt no-new-privileges=true --user 10001:10001 \
  --env PYTHONDONTWRITEBYTECODE=1 --entrypoint python \
  --volume "$source_state:/source:ro" \
  --volume "$destination_state:/var/lib/cardrag-worker" \
  --volume "$recovery_copy:/opt/cardrag-v113-recovery-copy.py:ro" \
  "$candidate_worker_image" \
  /opt/cardrag-v113-recovery-copy.py state \
  --source /source --destination /var/lib/cardrag-worker)
jq -e '
  .schema_version == "cardrag.v113-recovery-copy.v1" and
  .status == "passed" and .mode == "state" and
  .excluded_entries == ["worker-state.sqlite3-shm"] and
  .incident_source_file_count == 15872 and
  .incident_source_directory_entry_count == 10422 and
  .incident_source_total_file_bytes == 8643016164 and
  .main_database_size_bytes == 3713409024 and
  .wal_size_bytes == 19071512 and .shm_excluded_size_bytes == 32768 and
  .file_count == 15871 and .directory_count == 10423 and
  .bytes_copied == 8642983396 and
  (.content_tree_sha256 | test("^[0-9a-f]{64}$")) and
  .main_database_sha256 ==
    "0c963e6317979c610697c07603b9896c3dd00d566fea572780a78e8e4ad916ae" and
  .wal_sha256 ==
    "aad1a45f0c3be2fee507a571c810a5217056710f4eeeaadfeb80130c24755a06" and
  .shm_excluded_sha256 ==
    "2ae18281d101cd39dc09b438047be8620b2456e947f4ffb4fa8f64f7e20cc473"
' <<<"$state_copy_receipt" >/dev/null
printf '%s\n' "$state_copy_receipt"

codex_copy_receipt=$(docker run --rm --pull never --network none --read-only \
  --cap-drop ALL --security-opt no-new-privileges=true --user 10001:10001 \
  --env PYTHONDONTWRITEBYTECODE=1 --entrypoint python \
  --volume "$source_codex:/source:ro" \
  --volume "$destination_codex:/var/lib/cardrag-codex-home" \
  --volume "$recovery_copy:/opt/cardrag-v113-recovery-copy.py:ro" \
  "$candidate_worker_image" \
  /opt/cardrag-v113-recovery-copy.py codex \
  --source /source --destination /var/lib/cardrag-codex-home)
jq -e '
  .schema_version == "cardrag.v113-recovery-copy.v1" and
  .status == "passed" and .mode == "codex" and
  .destination_entry_count == 2 and .destination_home_empty == true and
  (.auth_bytes_copied > 0 and .auth_bytes_copied <= 2097152) and
  ([keys[] | select(test("sha"; "i"))] | length == 0)
' <<<"$codex_copy_receipt" >/dev/null
printf '%s\n' "$codex_copy_receipt"

available_after_copy=$(df --output=avail -B1 "$docker_root" | awk 'NR==2 {print $1}')
[[ "$available_after_copy" =~ ^[0-9]+$ ]]
((available_after_copy >= 34359738368))
```

어느 invocation이라도 nonzero로 끝나면 destination에 생긴 일부 파일을 지우거나 같은
volume으로 재시도하지 않습니다. 실패 destination은 격리하고 새 이름의 empty volume을
만든 뒤 source offline gate부터 다시 수행합니다.

### 3.3 Destination-only SQLite recovery

Recovery helper는 exact v1.0.13 Worker image를 사용하며 source volume은 mount하지
않습니다. Stale SHM을 제외한 main/WAL pair를 열어 crash recovery를 수행하고
`quick_check`, `integrity_check`, `foreign_key_check`와 `wal_checkpoint(TRUNCATE)`를 모두
통과시킵니다. Network는 차단하고 helper가 끝날 때까지 destination의 다른 mount는 0개여야
합니다.

```bash
set -euo pipefail
: "${CANDIDATE_SOURCE_COMMIT:?exact v1.0.13 PR-head commit is required}"
: "${CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST:?sealed Worker index digest is required}"
: "${CARDRAG_CANDIDATE_WORKER_CONFIG_DIGEST:?sealed Worker config digest is required}"
: "${destination_state:?new v1.0.13 state volume is required}"
: "${candidate_worker_image:?exact v1.0.13 Worker digest reference is required}"
: "${preserved_run_id:?preserved run ID is required}"
[[ "$CANDIDATE_SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ ]]
[[ "$CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "$CARDRAG_CANDIDATE_WORKER_CONFIG_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
test "$destination_state" = "cardrag-worker-v113-candidate-state"
test "$preserved_run_id" = "1f1763a9cd474a81952a6eb6ffb6e397"
test "$candidate_worker_image" = \
  "ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate@$CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST"
test -z "$(docker ps --quiet --filter "volume=$destination_state")"
recovery_image_inspect=$(docker image inspect "$candidate_worker_image")
jq -e --arg index_digest "$CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST" \
  --arg config_digest "$CARDRAG_CANDIDATE_WORKER_CONFIG_DIGEST" \
  --arg revision "$CANDIDATE_SOURCE_COMMIT" '
  type == "array" and length == 1 and
  (.[0].Id == $index_digest or .[0].Id == $config_digest) and
  .[0].Config.Labels["org.opencontainers.image.version"] == "1.0.13" and
  .[0].Config.Labels["org.opencontainers.image.revision"] == $revision
' <<<"$recovery_image_inspect" >/dev/null

docker run --rm --interactive --pull never --network none --read-only \
  --cap-drop ALL --security-opt no-new-privileges=true --user 10001:10001 \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m --entrypoint python \
  --volume "$destination_state:/var/lib/cardrag-worker" \
  "$candidate_worker_image" - "$preserved_run_id" <<'PY'
import os
import sqlite3
import stat
import sys
from pathlib import Path

run_id = sys.argv[1]
root = Path("/var/lib/cardrag-worker")
database = root / "worker-state.sqlite3"
wal = root / "worker-state.sqlite3-wal"
shm = root / "worker-state.sqlite3-shm"
journal = root / "worker-state.sqlite3-journal"
expected_owner = (10001, 10001)

def require_absent(path: Path, reason: str) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    raise SystemExit(reason)

root_stat = root.lstat()
if (not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode)
        or (root_stat.st_uid, root_stat.st_gid) != expected_owner
        or stat.S_IMODE(root_stat.st_mode) != 0o700):
    raise SystemExit("recovery_root_metadata_invalid")
for path, expected_size in ((database, 3713409024), (wal, 19071512)):
    value = path.lstat()
    if (not stat.S_ISREG(value.st_mode) or stat.S_ISLNK(value.st_mode)
            or value.st_nlink != 1
            or (value.st_uid, value.st_gid) != expected_owner
            or stat.S_IMODE(value.st_mode) != 0o644
            or value.st_size != expected_size):
        raise SystemExit("recovery_sqlite_input_metadata_invalid")
require_absent(shm, "stale_shm_was_not_excluded")
require_absent(journal, "stale_journal_present")

uri = database.absolute().as_uri() + "?mode=rw&vfs=unix-excl"
connection = sqlite3.connect(uri, uri=True, timeout=30, isolation_level=None)
try:
    require_absent(shm, "unix_excl_created_shm")
    require_absent(journal, "unix_excl_created_journal")
    maps = Path("/proc/self/maps").read_text(encoding="utf-8")
    if "worker-state.sqlite3-shm" in maps:
        raise SystemExit("unix_excl_mapped_shm")
    connection.execute("PRAGMA query_only=ON")
    if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise SystemExit("sqlite_quick_check_failed")
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise SystemExit("sqlite_integrity_check_failed")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise SystemExit("sqlite_foreign_key_check_failed")
    row = connection.execute(
        "SELECT status FROM run WHERE run_id=?", (run_id,)
    ).fetchone()
    if row != ("running",):
        raise SystemExit("preserved_run_not_resumable")
    connection.execute("PRAGMA query_only=OFF")
    checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if checkpoint is None or checkpoint[0] != 0:
        raise SystemExit("sqlite_checkpoint_busy")
finally:
    connection.close()

for transient in (wal, shm, journal):
    try:
        value = transient.lstat()
    except FileNotFoundError:
        continue
    if (not stat.S_ISREG(value.st_mode) or value.st_nlink != 1
            or (value.st_uid, value.st_gid) != (10001, 10001)
            or value.st_size != 0):
        raise SystemExit("recovery_transient_not_empty_regular")
    transient.unlink()
root_descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
try:
    os.fsync(root_descriptor)
finally:
    os.close(root_descriptor)

immutable_uri = database.absolute().as_uri() + "?mode=ro&immutable=1"
verified = sqlite3.connect(immutable_uri, uri=True, timeout=30)
try:
    verified.execute("PRAGMA query_only=ON")
    if verified.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise SystemExit("post_recovery_quick_check_failed")
    if verified.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise SystemExit("post_recovery_integrity_check_failed")
    if verified.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise SystemExit("post_recovery_foreign_key_check_failed")
    if verified.execute("SELECT status FROM run WHERE run_id=?", (run_id,)).fetchone() != (
        "running",
    ):
        raise SystemExit("post_recovery_run_identity_failed")
finally:
    verified.close()
for transient in (wal, shm, journal):
    require_absent(transient, "post_recovery_transient_present")
database_stat = database.lstat()
if (not stat.S_ISREG(database_stat.st_mode) or stat.S_ISLNK(database_stat.st_mode)
        or database_stat.st_nlink != 1
        or (database_stat.st_uid, database_stat.st_gid) != expected_owner
        or stat.S_IMODE(database_stat.st_mode) != 0o644):
    raise SystemExit("post_recovery_database_metadata_invalid")

print("v113-destination-sqlite-recovery-passed")
PY
```

위 한 helper가 checkpoint 뒤 남은 WAL/SHM의 metadata를 검사해 empty regular file만
제거하고 directory `fsync`, WAL/SHM 부재, read-only `immutable=1` 재-integrity와 exact
preserved run/status까지 확인한 뒤에만 sentinel을 출력합니다. 어느 gate라도 실패하면
v113 destination을 격리하고 새 이름으로 다시 복사합니다. Source나 core dump를 수정해
수습하지 않습니다.

## 4. 동일 run resume와 candidate 배치

Candidate digest와 loopback MCP 값은 `/etc/cardrag/*.env`를 수정하지 않고 caller shell에서
봉인한 값으로 명시합니다. Compose render에서 local `build`가 없고 Worker image, project,
channel과 v113 volume이 exact한지 확인한 뒤 동일 run을 detached resume합니다. 새 run을
만드는 인자 없는 `worker run`으로 대체하지 않습니다.

```bash
set -euo pipefail
: "${CANDIDATE_SOURCE_COMMIT:?exact v1.0.13 PR-head commit is required}"
: "${CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST:?sealed Worker index digest is required}"
: "${CARDRAG_CANDIDATE_WORKER_CONFIG_DIGEST:?sealed Worker config digest is required}"
: "${CARDRAG_CANDIDATE_MCP_IMAGE_DIGEST:?sealed MCP index digest is required}"
: "${CARDRAG_CANDIDATE_MCP_CONFIG_DIGEST:?sealed MCP config digest is required}"
[[ "$CANDIDATE_SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ ]]
[[ "$CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "$CARDRAG_CANDIDATE_WORKER_CONFIG_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "$CARDRAG_CANDIDATE_MCP_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "$CARDRAG_CANDIDATE_MCP_CONFIG_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
candidate_repository=ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate
candidate_worker_image="$candidate_repository@$CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST"
candidate_mcp_image="$candidate_repository@$CARDRAG_CANDIDATE_MCP_IMAGE_DIGEST"
destination_state=cardrag-worker-v113-candidate-state
preserved_run_id=1f1763a9cd474a81952a6eb6ffb6e397
export CARDRAG_CANDIDATE_MCP_PUBLIC_BASE_URL=http://127.0.0.1:18013
export CARDRAG_CANDIDATE_MCP_BIND_ADDRESS=127.0.0.1
export CARDRAG_CANDIDATE_MCP_PUBLISHED_PORT=18013
test "$preserved_run_id" = "1f1763a9cd474a81952a6eb6ffb6e397"
test -z "$(docker ps --quiet --filter volume=cardrag-worker-v113-candidate-state)"
if docker container inspect cardrag-v113-candidate-worker-acceptance >/dev/null 2>&1; then
  printf 'v113 acceptance container already exists\n' >&2
  exit 1
fi

worker_compose=(docker compose --env-file /etc/cardrag/worker.env \
  -f deploy/worker/compose.yaml \
  -f deploy/worker/compose.candidate.yaml \
  -f deploy/worker/compose.secrets.yaml)
worker_render=$("${worker_compose[@]}" config --format json)
jq -e --arg image "$candidate_worker_image" '
  .name == "cardrag-v113-candidate" and
  .services.worker.image == $image and
  (.services.worker | has("build") | not) and
  .services.worker.user == "10001:10001" and
  .services.worker.restart == "no" and
  .services.worker.environment.CARDRAG_CHANNEL == "candidate-v1.0.11" and
  .services.worker.environment.CARDRAG_STABLE_PUBLICATION_APPROVED == "false" and
  .services.worker.environment.CARDRAG_OCR_CACHE_PUBLICATION_APPROVED == "false" and
  .services.worker.environment.CARDRAG_REMOTE_GC_APPROVED == "false" and
  .volumes["worker-state"].name == "cardrag-worker-v113-candidate-state" and
  .volumes["codex-home"].name == "cardrag-worker-v113-candidate-codex-home"
' <<<"$worker_render" >/dev/null

worker_container_id=$("${worker_compose[@]}" run --detach --no-deps --pull never \
  --name cardrag-v113-candidate-worker-acceptance \
  worker resume "$preserved_run_id")
test "$(docker inspect --format '{{.Id}}' cardrag-v113-candidate-worker-acceptance)" = \
  "$worker_container_id"
worker_runtime=$(docker inspect cardrag-v113-candidate-worker-acceptance)
jq -e --arg image "$candidate_worker_image" \
  --arg index_digest "$CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST" \
  --arg config_digest "$CARDRAG_CANDIDATE_WORKER_CONFIG_DIGEST" \
  --arg revision "$CANDIDATE_SOURCE_COMMIT" '
  type == "array" and length == 1 and .[0].State.Running == true and
  .[0].RestartCount == 0 and .[0].State.OOMKilled == false and
  .[0].Config.Image == $image and
  (.[0].Image == $index_digest or .[0].Image == $config_digest) and
  .[0].Config.Labels["org.opencontainers.image.version"] == "1.0.13" and
  .[0].Config.Labels["org.opencontainers.image.revision"] == $revision and
  .[0].Config.Labels["com.docker.compose.project"] == "cardrag-v113-candidate" and
  .[0].Config.Labels["com.docker.compose.service"] == "worker" and
  ([.[0].Mounts[] | select(.Type == "volume") | {Name,Destination,RW}] |
    sort_by(.Destination)) == [
      {Name:"cardrag-worker-v113-candidate-codex-home",
       Destination:"/var/lib/cardrag-codex-home",RW:true},
      {Name:"cardrag-worker-v113-candidate-state",
       Destination:"/var/lib/cardrag-worker",RW:true}
    ]
' <<<"$worker_runtime" >/dev/null
```

이 시점에는 MCP를 시작하지 않습니다. Worker 진행은 `docker logs`로만 관측하고 live
SQLite에는 접속하지 않습니다. Worker가 terminal이 된 뒤 아래 gate가 exit 0/OOM false,
동일 run `succeeded`, 이 run의 ready publication 정확히 하나, WAL/SHM/journal 부재와
immutable integrity를 모두 확인한 뒤에만 MCP 배치를 허용합니다.

```bash
set -euo pipefail
: "${CANDIDATE_SOURCE_COMMIT:?exact v1.0.13 PR-head commit is required}"
: "${CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST:?sealed Worker index digest is required}"
: "${CARDRAG_CANDIDATE_WORKER_CONFIG_DIGEST:?sealed Worker config digest is required}"
: "${CARDRAG_CANDIDATE_MCP_IMAGE_DIGEST:?sealed MCP index digest is required}"
: "${CARDRAG_CANDIDATE_MCP_CONFIG_DIGEST:?sealed MCP config digest is required}"
[[ "$CANDIDATE_SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ ]]
[[ "$CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "$CARDRAG_CANDIDATE_WORKER_CONFIG_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "$CARDRAG_CANDIDATE_MCP_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "$CARDRAG_CANDIDATE_MCP_CONFIG_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
candidate_repository=ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate
candidate_worker_image="$candidate_repository@$CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST"
candidate_mcp_image="$candidate_repository@$CARDRAG_CANDIDATE_MCP_IMAGE_DIGEST"
destination_state=cardrag-worker-v113-candidate-state
preserved_run_id=1f1763a9cd474a81952a6eb6ffb6e397
export CARDRAG_CANDIDATE_MCP_PUBLIC_BASE_URL=http://127.0.0.1:18013
export CARDRAG_CANDIDATE_MCP_BIND_ADDRESS=127.0.0.1
export CARDRAG_CANDIDATE_MCP_PUBLISHED_PORT=18013
test "$(docker inspect --format \
  '{{.State.Status}} {{.State.ExitCode}} {{.State.OOMKilled}} {{.RestartCount}}' \
  cardrag-v113-candidate-worker-acceptance)" = "exited 0 false 0"
test -z "$(docker ps --quiet --filter volume=cardrag-worker-v113-candidate-state)"

worker_terminal_receipt=$(docker run --rm --pull never --network none --read-only \
  --cap-drop ALL --security-opt no-new-privileges=true --user 10001:10001 \
  --entrypoint python --volume "$destination_state:/state:ro" \
  "$candidate_worker_image" - "$preserved_run_id" <<'PY'
import json
import os
import sqlite3
import sys
from pathlib import Path

run_id = sys.argv[1]
database = Path("/state/worker-state.sqlite3")
if any(os.path.lexists(f"{database}{suffix}") for suffix in ("-wal", "-shm", "-journal")):
    raise SystemExit("worker_terminal_transient_present")
connection = sqlite3.connect(database.absolute().as_uri() + "?mode=ro&immutable=1", uri=True)
try:
    connection.execute("PRAGMA query_only=ON")
    if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise SystemExit("worker_terminal_quick_check_failed")
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise SystemExit("worker_terminal_integrity_check_failed")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise SystemExit("worker_terminal_foreign_key_check_failed")
    run = connection.execute("SELECT status FROM run WHERE run_id=?", (run_id,)).fetchone()
    publications = connection.execute(
        "SELECT generation_id,status FROM publish WHERE run_id=?", (run_id,)
    ).fetchall()
finally:
    connection.close()
if run != ("succeeded",) or len(publications) != 1 or publications[0][1] != "ready":
    raise SystemExit("worker_terminal_publication_not_ready")
print(json.dumps({
    "generation_id": publications[0][0],
    "publish_status": publications[0][1],
    "run_id": run_id,
    "run_status": run[0],
    "status": "passed",
}, separators=(",", ":"), sort_keys=True))
PY
)
jq -e --arg run_id "$preserved_run_id" '
  .status == "passed" and .run_id == $run_id and
  .run_status == "succeeded" and .publish_status == "ready" and
  (.generation_id | type == "string" and length > 0)
' <<<"$worker_terminal_receipt" >/dev/null
printf '%s\n' "$worker_terminal_receipt"

test -z "$(docker ps --quiet --filter publish=18013)"
mcp_compose=(docker compose --env-file /etc/cardrag/mcp.env \
  -f deploy/mcp/compose.yaml \
  -f deploy/mcp/compose.candidate.yaml \
  -f deploy/mcp/compose.secrets.yaml)
mcp_render=$("${mcp_compose[@]}" config --format json)
jq -e --arg image "$candidate_mcp_image" '
  .name == "cardrag-v113-candidate" and
  .services.mcp.image == $image and (.services.mcp | has("build") | not) and
  .services.mcp.user == "10001:10001" and
  .services.mcp.environment.CARDRAG_CHANNEL == "candidate-v1.0.11" and
  .services.mcp.environment.CARDRAG_MCP_PUBLIC_BASE_URL == "http://127.0.0.1:18013" and
  .volumes["mcp-state"].name == "cardrag-mcp-v113-candidate-state" and
  .services.mcp.ports == [{
    mode:"ingress",host_ip:"127.0.0.1",target:8000,published:"18013",protocol:"tcp"
  }]
' <<<"$mcp_render" >/dev/null
"${mcp_compose[@]}" up --detach --wait --no-build --pull never mcp
mcp_container_id=$("${mcp_compose[@]}" ps --quiet mcp)
[[ "$mcp_container_id" =~ ^[0-9a-f]{64}$ ]]
mcp_runtime=$(docker inspect "$mcp_container_id")
jq -e --arg image "$candidate_mcp_image" \
  --arg index_digest "$CARDRAG_CANDIDATE_MCP_IMAGE_DIGEST" \
  --arg config_digest "$CARDRAG_CANDIDATE_MCP_CONFIG_DIGEST" \
  --arg revision "$CANDIDATE_SOURCE_COMMIT" '
  type == "array" and length == 1 and .[0].State.Running == true and
  .[0].State.Health.Status == "healthy" and .[0].RestartCount == 0 and
  .[0].State.OOMKilled == false and .[0].Config.Image == $image and
  (.[0].Image == $index_digest or .[0].Image == $config_digest) and
  .[0].Config.Labels["org.opencontainers.image.version"] == "1.0.13" and
  .[0].Config.Labels["org.opencontainers.image.revision"] == $revision and
  .[0].Config.Labels["com.docker.compose.project"] == "cardrag-v113-candidate" and
  .[0].Config.Labels["com.docker.compose.service"] == "mcp" and
  ([.[0].Mounts[] | select(.Type == "volume") | {Name,Destination,RW}]) == [{
    Name:"cardrag-mcp-v113-candidate-state",Destination:"/var/lib/cardrag-mcp",RW:true
  }] and
  .[0].NetworkSettings.Ports["8000/tcp"] == [{HostIp:"127.0.0.1",HostPort:"18013"}]
' <<<"$mcp_runtime" >/dev/null
```

배치 직후 다음을 확인해 receipt에 기록합니다.

- Worker/MCP container `.Config.Image`, application version/revision과 local
  RepoDigest가 봉인한 exact OCI identity와 일치합니다.
- Worker는 `cardrag-worker-v113-candidate-state`와 v113 Codex home만 RW mount합니다.
- MCP는 v113 MCP state만 RW mount하고 `127.0.0.1:18013`에만 bind합니다.
- 두 role의 channel은 `candidate-v1.0.11`, stable publication/cache publication/remote
  GC는 모두 false입니다.
- Worker log에서 same run resume와 진행이 확인되고 즉시 crash loop가 없습니다.
- `SIGBUS`, `BUS_ADRERR`, core dump 증가, OOM 또는 unexpected restart가 없습니다.
- Live SQLite를 여는 cron/process가 없고 장시간 observer transaction도 없습니다.

Worker가 generation을 성공적으로 봉인한 뒤에만 candidate MCP readiness와 8-tool smoke,
source PDF range, v4/v5 activation/rollback을 수행합니다. Candidate 합격은 stable 승격이
아닙니다.

## 5. 중단과 rollback

Recovery, identity, integrity 또는 runtime gate가 하나라도 실패하면 v1.0.13 Worker/MCP를
중단하고 v113 destination과 log를 보존합니다. v1.0.12 source를 재시작하거나
v113 변경분을 source volume에 역복사하지 않습니다.

Stable v1.0.9 image, container, volume, `/opt/cardrag/current`, WebDAV stable pointer와
LibreChat 경로는 그대로 유지됩니다. Data/publication channel도 계속
`candidate-v1.0.11`입니다. Stable cutover, release tag와 구버전 cleanup은 운영
acceptance 결과 및 별도 승인 뒤에만 수행합니다.
