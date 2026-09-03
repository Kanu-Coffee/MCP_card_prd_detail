# CardRAG v1.0.14 remote-publication recovery candidate migration

이 문서는 v1.0.13의 `/tmp` 용량 종속 WebDAV 검증 실패를 수정한 v1.0.14 exact
images를 만들고, 사고 source를 보존한 채 새 volume에서 동일 run을 복구·resume하는
절차입니다. v1.0.14는 data/publication channel `candidate-v1.0.11`을 유지하는 runtime
patch입니다.

## 1. Public source exact-image build

Candidate image는 공개된 source repository의 merge 전 exact PR-head 40-hex commit을
remote Git context로 사용합니다. 첫 producer는 local worktree, untracked file 또는
credential-bearing context를 읽지 않습니다.

```bash
set -euo pipefail
: "${CANDIDATE_SOURCE_COMMIT:?exact v1.0.14 PR-head commit is required}"
[[ "$CANDIDATE_SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ ]]
test "$(docker buildx version | awk '{print $2}')" = "v0.36.1"
mapfile -t buildkit_versions < <(
  docker buildx inspect | sed -n 's/^[[:space:]]*BuildKit version: //p'
)
((${#buildkit_versions[@]} == 1))
test "${buildkit_versions[0]}" = "v0.32.2"

candidate_repository=ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate
source_context="https://github.com/Kanu-Coffee/MCP_card_prd_detail.git#$CANDIDATE_SOURCE_COMMIT"
build_metadata_root=$(mktemp -d /tmp/cardrag-v114-build-metadata.XXXXXX)
for role in worker mcp; do
  role_metadata="$build_metadata_root/$role.json"
  docker buildx build \
    --platform linux/amd64 \
    --target "$role" \
    --build-arg APP_VERSION=1.0.14 \
    --build-arg "VCS_REF=$CANDIDATE_SOURCE_COMMIT" \
    --build-arg PYTHON_DEV_IMAGE=cgr.dev/chainguard/python:latest-dev@sha256:4e2adecf67a1d18773c55b5526b47436392b9816ae6b8d92575979a2ab9de8b2 \
    --build-arg PYTHON_RUNTIME_IMAGE=cgr.dev/chainguard/python:latest@sha256:f47d995d001c1f949d560b1158d7f3ae556aad75a1044e72a125c900c1f05332 \
    --build-arg UV_IMAGE=ghcr.io/astral-sh/uv:0.8.17@sha256:e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1 \
    --build-arg CODEX_VERSION=0.151.0 \
    --build-arg CODEX_SHA256=605b4b183f22c645f5def63a5b7191767407fb66a6feaec4eaf10b5b7e0058f6 \
    --attest type=provenance,mode=max,version=v0.2 \
    --attest type=sbom,generator=docker.io/docker/buildkit-syft-scanner:stable-1@sha256:ae4f3b554449e7e25548e7d8ccc029d17357348e30c6e3df01b92bc93654d6a9 \
    --metadata-file "$role_metadata" \
    --output "type=registry,name=$candidate_repository:candidate-v1.0.14-$role-$CANDIDATE_SOURCE_COMMIT,oci-mediatypes=true,oci-artifact=true" \
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
별도로 봉인하며 이후 모든 helper와 Compose는 source-revision tag가 아닌 index digest를
사용합니다.

다음 gate는 registry raw JSON, Buildx metadata와 local pull을 교차검증합니다. Index에는
정확히 linux/amd64 application manifest 하나와 그 manifest를 subject로 하는 attestation
manifest 하나가 있어야 합니다. 출력한 digest와 metadata directory는 배포 receipt에
보존합니다. `containerimage.digest` metadata는 필수입니다. Docker driver에 따라
`containerimage.config.digest`가 생략될 수 있으므로, 이 필드는 존재하면 raw platform
manifest의 config digest와 일치해야 하며 raw manifest 검증 자체는 항상 필수입니다.

```bash
set -euo pipefail
: "${repository_root:?absolute repository root is required}"
: "${build_metadata_root:?build metadata directory is required}"
: "${CANDIDATE_SOURCE_COMMIT:?exact v1.0.14 PR-head commit is required}"
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
  tag="$candidate_repository:candidate-v1.0.14-$role-$CANDIDATE_SOURCE_COMMIT"
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
      .annotations["in-toto.io/predicate-type"] == "https://slsa.dev/provenance/v0.2"
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
    '{{ index .Config.Labels "org.opencontainers.image.version" }}')" = "1.0.14"
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
unset registry_token
```

## 2. 격리와 보존 경계

| 항목 | v1.0.14 candidate |
|---|---|
| application/runtime/OCI label | `1.0.14` |
| source branch | `codex/cardrag-v1.0.14` |
| GHCR source-revision tags | `candidate-v1.0.14-worker-$CANDIDATE_SOURCE_COMMIT`, `candidate-v1.0.14-mcp-$CANDIDATE_SOURCE_COMMIT` |
| data/publication channel | `candidate-v1.0.11` |
| Compose project | `cardrag-v114-candidate` |
| Worker state | `cardrag-worker-v114-candidate-state` |
| Codex home | `cardrag-worker-v114-candidate-codex-home` |
| MCP state | `cardrag-mcp-v114-candidate-state` |
| MCP bind | `127.0.0.1:18014` |
| preserved run | `1f1763a9cd474a81952a6eb6ffb6e397` |
| stable runtime/publication | 변경 금지 |

다음 v1.0.13 사고 증거는 write, restart, checkpoint, rename 또는 cleanup하지 않습니다.

- `cardrag-v113-candidate-worker-acceptance` container;
- `cardrag-worker-v113-candidate-state`와
  `cardrag-worker-v113-candidate-codex-home` volume;
- exact OCI index
  `sha256:9703eddeb5e4b1f3423b250fd13978121b24ec4a5a2c3e8064db4a76bdbe0be9`;
- source revision `03a24f5e549e5466dfe99db61e9ebbf6b58f8410`;
- run `1f1763a9cd474a81952a6eb6ffb6e397`의 local seal과 failure record.

Live Worker DB를 여는 monitor는 금지합니다. 운영 관측은 container state/log와 WebDAV
`HEAD`만 사용합니다. Stable image/container/volume, `/opt/cardrag/current`, WebDAV stable
pointer와 LibreChat 소비 경로에는 어떤 write도 수행하지 않습니다.

## 3. Offline recovery gate

### 3.1 Source와 v1.0.14 exact images 결속

Source container는 정확히 exit 1/OOM false/restart 0이고 실행 중인 container가 source
volume을 mount하지 않아야 합니다. Destination 세 volume은 이름만 다른 기존 volume이나
비운 volume이 아니라 새로 생성한 빈 volume이어야 합니다.

```bash
set -euo pipefail
: "${repository_root:?absolute repository root is required}"
: "${CANDIDATE_SOURCE_COMMIT:?exact v1.0.14 PR-head commit is required}"
: "${CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST:?v1.0.14 Worker index digest is required}"
: "${CARDRAG_CANDIDATE_WORKER_CONFIG_DIGEST:?v1.0.14 Worker config digest is required}"
: "${CARDRAG_CANDIDATE_MCP_IMAGE_DIGEST:?v1.0.14 MCP index digest is required}"
: "${CARDRAG_CANDIDATE_MCP_CONFIG_DIGEST:?v1.0.14 MCP config digest is required}"
[[ "$repository_root" = /* && "$repository_root" != */ ]]
[[ "$CANDIDATE_SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ ]]
for digest in \
  "$CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST" \
  "$CARDRAG_CANDIDATE_WORKER_CONFIG_DIGEST" \
  "$CARDRAG_CANDIDATE_MCP_IMAGE_DIGEST" \
  "$CARDRAG_CANDIDATE_MCP_CONFIG_DIGEST"; do
  [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]
done

incident_container=cardrag-v113-candidate-worker-acceptance
incident_image=ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate@sha256:9703eddeb5e4b1f3423b250fd13978121b24ec4a5a2c3e8064db4a76bdbe0be9
source_state=cardrag-worker-v113-candidate-state
source_codex=cardrag-worker-v113-candidate-codex-home
destination_state=cardrag-worker-v114-candidate-state
destination_codex=cardrag-worker-v114-candidate-codex-home
destination_mcp=cardrag-mcp-v114-candidate-state
preserved_run_id=1f1763a9cd474a81952a6eb6ffb6e397
candidate_repository=ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate
candidate_worker_image="$candidate_repository@$CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST"
candidate_mcp_image="$candidate_repository@$CARDRAG_CANDIDATE_MCP_IMAGE_DIGEST"

test "$(docker inspect --format \
  '{{.State.Status}} {{.State.ExitCode}} {{.State.OOMKilled}} {{.RestartCount}} {{.HostConfig.RestartPolicy.Name}}' \
  "$incident_container")" = "exited 1 false 0 no"
test "$(docker inspect --format '{{.Config.Image}}' "$incident_container")" = "$incident_image"
test "$(docker inspect --format \
  '{{index .Config.Labels "org.opencontainers.image.version"}}' \
  "$incident_container")" = "1.0.13"
test "$(docker inspect --format \
  '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
  "$incident_container")" = "03a24f5e549e5466dfe99db61e9ebbf6b58f8410"

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
      .[0].Config.Labels["org.opencontainers.image.version"] == "1.0.14" and
      .[0].Config.Labels["org.opencontainers.image.revision"] == $revision
    ' <<<"$image_inspect" >/dev/null
done

for volume in "$source_state" "$source_codex"; do
  docker volume inspect "$volume" >/dev/null
  test -z "$(docker ps --quiet --filter "volume=$volume")"
done
for volume in "$destination_state" "$destination_codex" "$destination_mcp"; do
  if docker volume inspect "$volume" >/dev/null 2>&1; then
    printf 'v114 destination already exists: %s\n' "$volume" >&2
    exit 1
  fi
done

docker_root=$(docker info --format '{{.DockerRootDir}}')
available_before_copy=$(df --output=avail -B1 "$docker_root" | awk 'NR==2 {print $1}')
[[ "$available_before_copy" =~ ^[0-9]+$ ]]
required_after_copy=34359738368
sealed_state_copy_bytes=16697435477
((available_before_copy >= required_after_copy + sealed_state_copy_bytes))
for volume in "$destination_state" "$destination_codex" "$destination_mcp"; do
  docker volume create "$volume" >/dev/null
  test -z "$(docker ps --quiet --filter "volume=$volume")"
done
```

### 3.2 Exact recovery-copy helper

Source state에는 checkpoint가 필요한 WAL이 없고 main DB는 정상입니다. 32 KiB
`worker-state.sqlite3-shm`은 transient wal-index이므로 destination에서 제외합니다. 그
외의 state tree는 high-cost embedding cache, sealed DB/vector와 `publish.json`을 포함해
byte-for-byte 복사합니다. Source는 어느 순간에도 writable로 mount하거나 SQLite로 열지
않습니다.

`tools/cardrag_v114_recovery_copy.py`는 다음을 fail-closed로 검증합니다.

1. Docker inspect가 v1.0.13 source의 exact container, OCI digest, source revision,
   exit/OOM/restart 상태와 source volume 이름에 일치한다.
2. Source와 destination의 모든 path component를 descriptor 기반 `O_NOFOLLOW`/`lstat`로
   검사하고 symlink, special node, hardlink, cross-filesystem entry, owner drift와 race를
   거부한다.
3. Source filesystem은 read-only이고 destination은 empty이며 두 root의 inode가 다르다.
4. 모든 regular file을 copy 전후 size/SHA-256으로 대조하고 directory마다 `fsync`한 뒤
   canonical tree digest를 봉인한다.
5. Codex volume은 전체 복사가 아니라 owner `10001:10001`, mode `0600`, 최대 2 MiB의
   `auth.json`만 빈 mode `0700` home에 atomic copy한다. Credential content와 digest는
   stdout/stderr에 출력하지 않는다.

Helper와 그 기반 구현은 exact PR-head Git blob과 결속한 두 개의 regular file만 mount합니다.
Mutable repository directory 전체를 mount하지 않습니다. Docker inspect JSON도 출력하지
않고 mode 0700 임시 directory 안에서만 전달합니다.

```bash
set -euo pipefail
: "${repository_root:?absolute repository root is required}"
: "${CANDIDATE_SOURCE_COMMIT:?exact v1.0.14 PR-head commit is required}"
: "${candidate_worker_image:?exact v1.0.14 Worker digest reference is required}"
: "${incident_container:?v1.0.13 source container is required}"
: "${source_state:?offline v1.0.13 state volume is required}"
: "${source_codex:?offline v1.0.13 Codex volume is required}"
: "${destination_state:?fresh v1.0.14 state volume is required}"
: "${destination_codex:?fresh v1.0.14 Codex volume is required}"
incident_image=ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate@sha256:9703eddeb5e4b1f3423b250fd13978121b24ec4a5a2c3e8064db4a76bdbe0be9
docker_root=$(docker info --format '{{.DockerRootDir}}')
[[ "$repository_root" = /* && "$repository_root" != */ ]]
[[ "$CANDIDATE_SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ ]]
test "$(git -C "$repository_root" rev-parse HEAD)" = "$CANDIDATE_SOURCE_COMMIT"
test -z "$(git -C "$repository_root" status --porcelain=v1 --untracked-files=all)"

recovery_copy_relative=tools/cardrag_v114_recovery_copy.py
recovery_base_relative=tools/cardrag_v113_recovery_copy.py
recovery_copy="$repository_root/$recovery_copy_relative"
recovery_base="$repository_root/$recovery_base_relative"
for relative in "$recovery_copy_relative" "$recovery_base_relative"; do
  path="$repository_root/$relative"
  test -f "$path" && test ! -L "$path"
  test "$(stat --format='%a %h' "$path")" = "644 1"
  test "$(git -C "$repository_root" ls-tree "$CANDIDATE_SOURCE_COMMIT" \
    "$relative" | awk '{print $1}')" = "100644"
  git_blob_sha256=$(git -C "$repository_root" show \
    "$CANDIDATE_SOURCE_COMMIT:$relative" | sha256sum | awk '{print $1}')
  test "$(sha256sum "$path" | awk '{print $1}')" = "$git_blob_sha256"
done

inspection_root=$(mktemp -d /tmp/cardrag-v114-source-inspect.XXXXXX)
chmod 700 "$inspection_root"
container_inspect="$inspection_root/container.json"
image_inspect="$inspection_root/image.json"
docker inspect "$incident_container" >"$container_inspect"
docker image inspect "$incident_image" >"$image_inspect"
chmod 644 "$container_inspect" "$image_inspect"

inspection_receipt=$(docker run --rm --pull never --network none --read-only \
  --cap-drop ALL --security-opt no-new-privileges=true --user 10001:10001 \
  --env PYTHONDONTWRITEBYTECODE=1 --entrypoint python \
  --volume "$container_inspect:/evidence/container.json:ro" \
  --volume "$image_inspect:/evidence/image.json:ro" \
  --volume "$recovery_copy:/opt/cardrag_v114_recovery_copy.py:ro" \
  --volume "$recovery_base:/opt/cardrag_v113_recovery_copy.py:ro" \
  "$candidate_worker_image" /opt/cardrag_v114_recovery_copy.py inspect \
  --container-inspect /evidence/container.json --image-inspect /evidence/image.json)
jq -e '
  .schema_version == "cardrag.v114-recovery-copy.v1" and
  .status == "passed" and .mode == "inspect" and
  .source_container == "cardrag-v113-candidate-worker-acceptance" and
  .source_version == "1.0.13" and
  .source_revision == "03a24f5e549e5466dfe99db61e9ebbf6b58f8410" and
  .exit_code == 1 and .oom_killed == false
' <<<"$inspection_receipt" >/dev/null
printf '%s\n' "$inspection_receipt"

state_copy_receipt=$(docker run --rm --pull never --network none --read-only \
  --cap-drop ALL --security-opt no-new-privileges=true --user 10001:10001 \
  --env PYTHONDONTWRITEBYTECODE=1 --entrypoint python \
  --volume "$container_inspect:/evidence/container.json:ro" \
  --volume "$image_inspect:/evidence/image.json:ro" \
  --volume "$recovery_copy:/opt/cardrag_v114_recovery_copy.py:ro" \
  --volume "$recovery_base:/opt/cardrag_v113_recovery_copy.py:ro" \
  --volume "$source_state:/source:ro" \
  --volume "$destination_state:/var/lib/cardrag-worker" \
  "$candidate_worker_image" /opt/cardrag_v114_recovery_copy.py state \
  --container-inspect /evidence/container.json --image-inspect /evidence/image.json \
  --source /source --destination /var/lib/cardrag-worker)
jq -e '
  .schema_version == "cardrag.v114-recovery-copy.v1" and
  .status == "passed" and .mode == "state" and
  .excluded_entries == ["worker-state.sqlite3-shm"] and
  .incident_source_file_count == 15883 and
  .incident_source_directory_entry_count == 10427 and
  .incident_source_total_file_bytes == 16697468245 and
  .main_database_size_bytes == 3900289024 and
  .main_database_sha256 ==
    "f4941cc73f15a021f6606d837829dc96c90bc6eda2faad6f4fa33577265e04df" and
  .shm_excluded_size_bytes == 32768 and
  .shm_excluded_sha256 ==
    "31125591d630ebf62822a27764a37a81fdc5a8482334f462f7e93fdecec6ecd4" and
  .wal_present == false and .file_count == 15882 and
  .directory_count == 10428 and .bytes_copied == 16697435477 and
  (.content_tree_sha256 | test("^[0-9a-f]{64}$"))
' <<<"$state_copy_receipt" >/dev/null
printf '%s\n' "$state_copy_receipt"

codex_copy_receipt=$(docker run --rm --pull never --network none --read-only \
  --cap-drop ALL --security-opt no-new-privileges=true --user 10001:10001 \
  --env PYTHONDONTWRITEBYTECODE=1 --entrypoint python \
  --volume "$container_inspect:/evidence/container.json:ro" \
  --volume "$image_inspect:/evidence/image.json:ro" \
  --volume "$recovery_copy:/opt/cardrag_v114_recovery_copy.py:ro" \
  --volume "$recovery_base:/opt/cardrag_v113_recovery_copy.py:ro" \
  --volume "$source_codex:/source:ro" \
  --volume "$destination_codex:/var/lib/cardrag-codex-home" \
  "$candidate_worker_image" /opt/cardrag_v114_recovery_copy.py codex \
  --container-inspect /evidence/container.json --image-inspect /evidence/image.json \
  --source /source --destination /var/lib/cardrag-codex-home)
jq -e '
  .schema_version == "cardrag.v114-recovery-copy.v1" and
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

어느 invocation이라도 nonzero이면 일부 destination을 지우거나 같은 volume으로 재시도하지
않습니다. 실패한 v114 destination과 receipt를 격리하고 새 이름의 empty volume을 만든 뒤
source offline gate부터 다시 수행합니다. Source volume은 계속 read-only로 보존합니다.

### 3.3 Destination exact-image SQLite/seal validation

복사 뒤 destination만 v1.0.14 exact image에 read-only로 mount합니다. Root Worker DB에는
WAL/SHM/journal이 없어야 하고 `failed`인 동일 run은 resume 가능한 terminal state여야
합니다. 이어서 v1.0.14 자체의 seal validator로 DB/vector/object 전수 hash, SQLite
integrity/foreign-key, manifest binding과 319,459 x 4,096 vector finite/L2 계약을 다시
검증합니다. 검증 중 source volume과 network는 연결하지 않습니다.

```bash
set -euo pipefail
: "${destination_state:?fresh v1.0.14 state volume is required}"
: "${candidate_worker_image:?exact v1.0.14 Worker digest reference is required}"
: "${preserved_run_id:?preserved run ID is required}"
test "$destination_state" = "cardrag-worker-v114-candidate-state"
test "$preserved_run_id" = "1f1763a9cd474a81952a6eb6ffb6e397"
test -z "$(docker ps --quiet --filter "volume=$destination_state")"

destination_validation=$(docker run --rm --interactive --pull never \
  --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges=true --user 10001:10001 \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m --entrypoint python \
  --volume "$destination_state:/var/lib/cardrag-worker:ro" \
  "$candidate_worker_image" - "$preserved_run_id" <<'PY'
import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

from cardrag_worker.pipeline import WorkerPipeline

run_id = sys.argv[1]
root = Path("/var/lib/cardrag-worker")
state_database = root / "worker-state.sqlite3"
for suffix in ("-wal", "-shm", "-journal"):
    if os.path.lexists(f"{state_database}{suffix}"):
        raise SystemExit("destination_sqlite_transient_present")

connection = sqlite3.connect(
    state_database.absolute().as_uri() + "?mode=ro&immutable=1", uri=True
)
try:
    connection.execute("PRAGMA query_only=ON")
    if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise SystemExit("destination_quick_check_failed")
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise SystemExit("destination_integrity_check_failed")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise SystemExit("destination_foreign_key_check_failed")
    run = connection.execute(
        "SELECT status FROM run WHERE run_id=?", (run_id,)
    ).fetchone()
    publications = connection.execute(
        "SELECT generation_id,status FROM publish WHERE run_id=?", (run_id,)
    ).fetchall()
finally:
    connection.close()
if run != ("failed",) or publications:
    raise SystemExit("destination_run_state_not_resumable")

seal_path = root / "runs" / run_id / "sealed" / "publish.json"
sealed = json.loads(seal_path.read_bytes())
pipeline = object.__new__(WorkerPipeline)
pipeline.state_dir = root
pipeline.pdf_cache = SimpleNamespace(objects_root=root / "pdf-cache" / "objects" / "sha256")
pipeline.document_aggregation = None
validated = asyncio.run(pipeline._validate_local_seal(sealed))
if (
    validated.manifest.generation_id
    != "g-1f1763a9cd474a81952a6eb6-2405a03c6f8e"
    or validated.database_path.stat().st_size != 2647711744
    or validated.vector_path is None
    or validated.vector_path.stat().st_size != 5234016256
    or len(validated.objects) != 6276
):
    raise SystemExit("destination_seal_identity_mismatch")
print(json.dumps({
    "generation_id": validated.manifest.generation_id,
    "object_count": len(validated.objects),
    "run_id": run_id,
    "seal_sha256": validated.seal_sha256,
    "status": "passed",
}, separators=(",", ":"), sort_keys=True))
PY
)
jq -e --arg run_id "$preserved_run_id" '
  .status == "passed" and .run_id == $run_id and
  .generation_id == "g-1f1763a9cd474a81952a6eb6-2405a03c6f8e" and
  .object_count == 6276 and (.seal_sha256 | test("^[0-9a-f]{64}$"))
' <<<"$destination_validation" >/dev/null
printf '%s\n' "$destination_validation"
```

Validator 실패 시 destination을 수정해 맞추지 않습니다. 해당 v114 volume과 receipt를
격리하고 source read-only copy부터 새 destination 이름으로 다시 수행합니다.

## 4. Same-run sealed-publication resume와 candidate 배치

Candidate digest와 loopback MCP 값은 `/etc/cardrag/*.env`를 수정하지 않고 build receipt와
같은 caller shell에서 export합니다. Compose render에 local `build`가 없고 exact digest,
v114 project/volume, `candidate-v1.0.11` channel과 모든 destructive approval false가
확인된 뒤 동일 run을 provider-free `resume-publication`으로 detached resume합니다. 일반
`resume`은 latest-only 보장을 위해 live issuer discovery와 endpoint metadata preflight를
다시 수행합니다. 사고 뒤 live embedding endpoint metadata가 바뀌면 유효한 local seal의
fast path에 도달하지 못하고 OCR/embedding 경로로 재진입할 수 있으므로 이 복구에는
사용하지 않습니다. 인자 없는 새 `worker run`으로도 대체하지 않습니다.

```bash
set -euo pipefail
: "${CANDIDATE_SOURCE_COMMIT:?exact v1.0.14 PR-head commit is required}"
: "${CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST:?sealed Worker index digest is required}"
: "${CARDRAG_CANDIDATE_WORKER_CONFIG_DIGEST:?sealed Worker config digest is required}"
: "${CARDRAG_CANDIDATE_MCP_IMAGE_DIGEST:?sealed MCP index digest is required}"
: "${CARDRAG_CANDIDATE_MCP_CONFIG_DIGEST:?sealed MCP config digest is required}"
for digest in \
  "$CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST" \
  "$CARDRAG_CANDIDATE_WORKER_CONFIG_DIGEST" \
  "$CARDRAG_CANDIDATE_MCP_IMAGE_DIGEST" \
  "$CARDRAG_CANDIDATE_MCP_CONFIG_DIGEST"; do
  [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]
done
candidate_repository=ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate
candidate_worker_image="$candidate_repository@$CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST"
candidate_mcp_image="$candidate_repository@$CARDRAG_CANDIDATE_MCP_IMAGE_DIGEST"
destination_state=cardrag-worker-v114-candidate-state
preserved_run_id=1f1763a9cd474a81952a6eb6ffb6e397
export CARDRAG_CANDIDATE_MCP_PUBLIC_BASE_URL=http://127.0.0.1:18014
export CARDRAG_CANDIDATE_MCP_BIND_ADDRESS=127.0.0.1
export CARDRAG_CANDIDATE_MCP_PUBLISHED_PORT=18014
test -z "$(docker ps --quiet --filter "volume=$destination_state")"
if docker container inspect cardrag-v114-candidate-worker-acceptance >/dev/null 2>&1; then
  printf 'v114 acceptance container already exists\n' >&2
  exit 1
fi

worker_compose=(docker compose --env-file /etc/cardrag/worker.env
  -f deploy/worker/compose.yaml
  -f deploy/worker/compose.candidate.yaml
  -f deploy/worker/compose.secrets.yaml)
worker_render=$("${worker_compose[@]}" config --format json)
jq -e --arg image "$candidate_worker_image" '
  .name == "cardrag-v114-candidate" and
  .services.worker.image == $image and
  (.services.worker | has("build") | not) and
  .services.worker.pull_policy == "always" and
  .services.worker.user == "10001:10001" and
  .services.worker.restart == "no" and
  .services.worker.environment.CARDRAG_CHANNEL == "candidate-v1.0.11" and
  .services.worker.environment.CARDRAG_STABLE_PUBLICATION_APPROVED == "false" and
  .services.worker.environment.CARDRAG_OCR_CACHE_PUBLICATION_APPROVED == "false" and
  .services.worker.environment.CARDRAG_REMOTE_GC_APPROVED == "false" and
  .services.worker.environment.CARDRAG_COLLECT_REMOTE_GARBAGE == "false" and
  .volumes["worker-state"].name == "cardrag-worker-v114-candidate-state" and
  .volumes["codex-home"].name == "cardrag-worker-v114-candidate-codex-home"
' <<<"$worker_render" >/dev/null

worker_container_id=$("${worker_compose[@]}" run --detach --no-deps --pull never \
  --name cardrag-v114-candidate-worker-acceptance \
  worker resume-publication "$preserved_run_id")
test "$(docker inspect --format '{{.Id}}' \
  cardrag-v114-candidate-worker-acceptance)" = "$worker_container_id"
worker_runtime=$(docker inspect cardrag-v114-candidate-worker-acceptance)
jq -e --arg image "$candidate_worker_image" \
  --arg index_digest "$CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST" \
  --arg config_digest "$CARDRAG_CANDIDATE_WORKER_CONFIG_DIGEST" \
  --arg revision "$CANDIDATE_SOURCE_COMMIT" '
  type == "array" and length == 1 and
  (.[0].State.Running == true or
    (.[0].State.Status == "exited" and .[0].State.ExitCode == 0)) and
  .[0].RestartCount == 0 and .[0].State.OOMKilled == false and
  .[0].Config.Image == $image and
  (.[0].Image == $index_digest or .[0].Image == $config_digest) and
  .[0].Config.Labels["org.opencontainers.image.version"] == "1.0.14" and
  .[0].Config.Labels["org.opencontainers.image.revision"] == $revision and
  .[0].Config.Labels["com.docker.compose.project"] == "cardrag-v114-candidate" and
  .[0].Config.Labels["com.docker.compose.service"] == "worker" and
  ([.[0].Mounts[] | select(.Type == "volume") | {Name,Destination,RW}] |
    sort_by(.Destination)) == [
      {Name:"cardrag-worker-v114-candidate-codex-home",
       Destination:"/var/lib/cardrag-codex-home",RW:true},
      {Name:"cardrag-worker-v114-candidate-state",
       Destination:"/var/lib/cardrag-worker",RW:true}
    ]
' <<<"$worker_runtime" >/dev/null
```

Worker가 실행 중인 동안 `docker logs cardrag-v114-candidate-worker-acceptance`와 Docker
state만 관측합니다. Live SQLite를 열거나 source/destination volume에 별도 reader를
붙이지 않습니다. `resume-publication`은 failed/interrupted run 또는 worker lock을 새로
점유한 뒤의 stale-running run, canonical `publish.json`,
전체 local seal을 fail-closed 검증한 뒤 preserved DB/vector를 그대로
stream-verify/publish합니다. Provider, issuer discovery, OCR, embedding, cache healing,
retention cleanup과 remote GC는 구성하거나 호출하지 않으며 full local seal 검증은 한
번뿐입니다.

### 4.1 Worker terminal 및 WebDAV activation gate

MCP는 Worker가 exit 0으로 끝나기 전에 시작하지 않습니다. 다음 gate는 같은 run이
`succeeded`, publication이 정확히 하나의 `ready`인지 offline으로 확인한 뒤 candidate
pointer, `READY`, manifest, DB와 vector를 read-only GET/`HEAD`로 확인합니다. WebDAV
credential은 Compose secret file로만 주입되며 shell 변수, command argument 또는 receipt에
노출하지 않습니다.

```bash
set -euo pipefail
: "${CANDIDATE_SOURCE_COMMIT:?exact v1.0.14 PR-head commit is required}"
: "${CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST:?sealed Worker index digest is required}"
: "${CARDRAG_CANDIDATE_WORKER_CONFIG_DIGEST:?sealed Worker config digest is required}"
candidate_repository=ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate
candidate_worker_image="$candidate_repository@$CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST"
destination_state=cardrag-worker-v114-candidate-state
preserved_run_id=1f1763a9cd474a81952a6eb6ffb6e397
worker_compose=(docker compose --env-file /etc/cardrag/worker.env
  -f deploy/worker/compose.yaml
  -f deploy/worker/compose.candidate.yaml
  -f deploy/worker/compose.secrets.yaml)
test "$(docker inspect --format \
  '{{.State.Status}} {{.State.ExitCode}} {{.State.OOMKilled}} {{.RestartCount}}' \
  cardrag-v114-candidate-worker-acceptance)" = "exited 0 false 0"
test -z "$(docker ps --quiet --filter volume=cardrag-worker-v114-candidate-state)"

worker_terminal_receipt=$(docker run --rm --interactive --pull never \
  --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges=true --user 10001:10001 --entrypoint python \
  --volume "$destination_state:/state:ro" \
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
  .generation_id == "g-1f1763a9cd474a81952a6eb6-2405a03c6f8e"
' <<<"$worker_terminal_receipt" >/dev/null
printf '%s\n' "$worker_terminal_receipt"

expected_generation=$(jq -er '.generation_id' <<<"$worker_terminal_receipt")
remote_receipt=$("${worker_compose[@]}" run --rm --no-deps --pull never \
  --volume "$destination_state:/var/lib/cardrag-worker:ro" \
  --entrypoint python worker - "$expected_generation" <<'PY'
import json
import os
import sys

from cardrag_core import (
    MCPArtifactReader,
    WebDAVClient,
    WebDAVSettings,
    generation_database_path,
    generation_manifest_path,
    generation_ready_path,
    generation_vectors_path,
)

expected_generation = sys.argv[1]
client = WebDAVClient(WebDAVSettings.from_env())
try:
    read_only = client.read_only()
    reader = MCPArtifactReader(read_only, channel=os.environ["CARDRAG_CHANNEL"])
    current = reader.read_current_generation()
    if current.pointer.generation_id != expected_generation:
        raise SystemExit("candidate_pointer_generation_mismatch")
    paths = (
        reader.pointer_path,
        generation_ready_path(expected_generation),
        generation_manifest_path(expected_generation),
        generation_database_path(expected_generation),
        generation_vectors_path(expected_generation),
    )
    if not all(read_only.exists(path) for path in paths):
        raise SystemExit("candidate_generation_head_missing")
    print(json.dumps({
        "channel": os.environ["CARDRAG_CHANNEL"],
        "generation_id": current.pointer.generation_id,
        "head_count": len(paths),
        "status": "passed",
    }, separators=(",", ":"), sort_keys=True))
finally:
    client.close()
PY
)
jq -e --arg generation "$expected_generation" '
  .status == "passed" and .channel == "candidate-v1.0.11" and
  .generation_id == $generation and .head_count == 5
' <<<"$remote_receipt" >/dev/null
printf '%s\n' "$remote_receipt"
```

### 4.2 MCP start와 runtime identity

위 terminal 및 remote receipt가 모두 통과한 뒤에만 loopback `18014`에서 MCP를
시작합니다.

```bash
set -euo pipefail
: "${CANDIDATE_SOURCE_COMMIT:?exact v1.0.14 PR-head commit is required}"
: "${CARDRAG_CANDIDATE_MCP_IMAGE_DIGEST:?sealed MCP index digest is required}"
: "${CARDRAG_CANDIDATE_MCP_CONFIG_DIGEST:?sealed MCP config digest is required}"
candidate_repository=ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate
candidate_mcp_image="$candidate_repository@$CARDRAG_CANDIDATE_MCP_IMAGE_DIGEST"
export CARDRAG_CANDIDATE_MCP_PUBLIC_BASE_URL=http://127.0.0.1:18014
export CARDRAG_CANDIDATE_MCP_BIND_ADDRESS=127.0.0.1
export CARDRAG_CANDIDATE_MCP_PUBLISHED_PORT=18014
test -z "$(docker ps --quiet --filter publish=18014)"
mcp_compose=(docker compose --env-file /etc/cardrag/mcp.env
  -f deploy/mcp/compose.yaml
  -f deploy/mcp/compose.candidate.yaml
  -f deploy/mcp/compose.secrets.yaml)
mcp_render=$("${mcp_compose[@]}" config --format json)
jq -e --arg image "$candidate_mcp_image" '
  .name == "cardrag-v114-candidate" and
  .services.mcp.image == $image and
  (.services.mcp | has("build") | not) and
  .services.mcp.pull_policy == "always" and
  .services.mcp.user == "10001:10001" and
  .services.mcp.environment.CARDRAG_CHANNEL == "candidate-v1.0.11" and
  .services.mcp.environment.CARDRAG_MCP_PUBLIC_BASE_URL == "http://127.0.0.1:18014" and
  .volumes["mcp-state"].name == "cardrag-mcp-v114-candidate-state" and
  .services.mcp.ports == [{
    mode:"ingress",host_ip:"127.0.0.1",target:8000,published:"18014",protocol:"tcp"
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
  .[0].Config.Labels["org.opencontainers.image.version"] == "1.0.14" and
  .[0].Config.Labels["org.opencontainers.image.revision"] == $revision and
  .[0].Config.Labels["com.docker.compose.project"] == "cardrag-v114-candidate" and
  .[0].Config.Labels["com.docker.compose.service"] == "mcp" and
  ([.[0].Mounts[] | select(.Type == "volume") | {Name,Destination,RW}]) == [{
    Name:"cardrag-mcp-v114-candidate-state",Destination:"/var/lib/cardrag-mcp",RW:true
  }] and
  .[0].NetworkSettings.Ports["8000/tcp"] == [{HostIp:"127.0.0.1",HostPort:"18014"}]
' <<<"$mcp_runtime" >/dev/null
```

배치 receipt에는 Worker/MCP exact index/config identity, source revision, Compose render,
same-run terminal receipt, WebDAV five-object `HEAD`와 MCP health 결과를 함께 보존합니다.
그 다음 v1.0.11 acceptance baseline의 8-tool smoke, source PDF range와 v4/v5 local activation
및 rollback 시험을 v114 project/volume/port로 수행합니다. Candidate 합격은 stable 승격이
아닙니다.

## 5. 중단, rollback과 불변 경계

어느 identity, copy, integrity, publication 또는 health gate라도 실패하면 다음 순서로
candidate만 격리합니다.

1. 실행 중인 `cardrag-v114-candidate` MCP와 Worker만 stop합니다. Source v1.0.13
   container는 restart하지 않습니다.
2. 실패한 v114 container, 세 destination volume, build metadata와 receipt를 보존합니다.
   Partial destination을 지우거나 재사용하지 않고 다음 시도는 새 volume 이름으로
   source read-only copy부터 시작합니다.
3. Candidate pointer가 아직 없거나 이전 generation이면 remote write를 하지 않습니다.
   새 candidate generation으로 이미 전환되었다면 v114 MCP를 중단하고 봉인된 last-good
   candidate를 별도 검토된 pointer rollback 절차로만 활성화합니다. Generation/CAS를
   삭제하지 않으며 remote GC도 실행하지 않습니다.
4. Stable runtime/image/container/volume, `/opt/cardrag/current`, WebDAV stable pointer,
   systemd unit/timer와 LibreChat 경로가 배포 전 identity 그대로인지 확인합니다.

Candidate runtime 중단은 volume/container 제거 없이 다음처럼 수행합니다. `down -v`, image
prune, remote DELETE와 source container start는 사용하지 않습니다.

```bash
set -euo pipefail
: "${CARDRAG_CANDIDATE_MCP_IMAGE_DIGEST:?sealed MCP index digest is required}"
export CARDRAG_CANDIDATE_MCP_PUBLIC_BASE_URL=http://127.0.0.1:18014
export CARDRAG_CANDIDATE_MCP_BIND_ADDRESS=127.0.0.1
export CARDRAG_CANDIDATE_MCP_PUBLISHED_PORT=18014
mcp_compose=(docker compose --env-file /etc/cardrag/mcp.env
  -f deploy/mcp/compose.yaml
  -f deploy/mcp/compose.candidate.yaml
  -f deploy/mcp/compose.secrets.yaml)
"${mcp_compose[@]}" stop mcp
if docker container inspect cardrag-v114-candidate-worker-acceptance >/dev/null 2>&1 &&
   test "$(docker inspect --format '{{.State.Running}}' \
     cardrag-v114-candidate-worker-acceptance)" = "true"; then
  docker stop --time 30 cardrag-v114-candidate-worker-acceptance >/dev/null
fi
for volume in \
  cardrag-worker-v114-candidate-state \
  cardrag-worker-v114-candidate-codex-home \
  cardrag-mcp-v114-candidate-state; do
  docker volume inspect "$volume" >/dev/null
  test -z "$(docker ps --quiet --filter "volume=$volume")"
done
test "$(docker inspect --format '{{.State.Status}} {{.State.ExitCode}}' \
  cardrag-v113-candidate-worker-acceptance)" = "exited 1"
```

이 migration의 명령은 `candidate-v1.0.11`, `cardrag-v114-candidate`와 v114 전용 volume만
write 대상으로 삼습니다. Stable publication approval, OCR-cache publication approval와
remote-GC approval은 모두 false이며 stable cutover, release tag, DockerHub publication과
구버전 cleanup은 운영 acceptance 및 별도 승인 전까지 금지합니다.
