# CardRAG v1.0.10 candidate acceptance receipt

`cardrag_core.candidate_acceptance`는 실제 candidate 검증이 끝난 뒤 만들어진 증거를 하나의
canonical receipt로 결속하는 read-only release verifier입니다. 이 verifier는 Docker,
WebDAV, Worker/MCP runtime을 열거나 재실행하지 않으며, 증거를 생성하거나 자체 승인하지도
않습니다. 따라서 receipt SHA-256은 source replay와 운영 검토를 독립적으로 끝낸 승인자가
release workflow에 별도 입력해야 합니다.

현재 저장소에는 실제 candidate receipt와 그 12개 입력이 아직 없습니다. schema와 release
gate가 준비되었다는 사실은 candidate run의 합격을 뜻하지 않으며, 입력이 없거나 한 필드라도
다르면 publish는 의도대로 중단됩니다.

## Canonical receipt와 증거 묶음

receipt schema는 `cardrag.candidate-acceptance-receipt.v1`이고 release version, 정확한
40자리 `source_commit`, `cardrag-v110-candidate`, `candidate-v1.0.10`, generation ID,
정확히 `kb`, `samsung`, `shinhan`, `woori` 네 카드사와 아래 파일을 결속합니다.

| receipt binding | 증명해야 하는 계약 |
|---|---|
| `effective_config` | private candidate repository의 rendered `repository@OCI-index`, 정확히 2-descriptor OCI index/platform/config/attestation digest와 subject, revision/version/entrypoint/user, 별도 project/channel/volume/loopback port, read-only rootfs, all-cap drop, no-new-privileges, v1.0.9 RW mount 0, Worker/MCP state·reserve·DB·sidecar·download·audit exact capacity, Qwen 4,096D FP32 L2와 exact-all-active-rows 정책 |
| `generation_manifest` | canonical v5 manifest, sealed aggregation/retrieval profile, 구조 coverage 100%, cross-contract 0, vector sidecar |
| `generation_ready` | manifest, SQLite와 sidecar SHA/size |
| `candidate_pointer` | candidate generation의 manifest/READY |
| `worker_metrics` | rendered image RepoDigest와 실제 container image ID가 sealed index/config digest와 같은 exact effective-config/image에서 UID 10001, read-only rootfs/cap-drop/NNP, Codex·bubblewrap version, bubblewrap user namespace와 Codex read-only sandbox smoke, 카드사별 acquired=succeeded·failed=0인 full 4-card-company run, 문서/chunk/row/sidecar/구조 count, PDF/OCR cache hit/miss, provider 및 publication count |
| `mcp_smoke` | rendered image RepoDigest와 실제 container image ID가 sealed index/config digest와 같은 exact effective-config/image에서 UID 10001/read-only rootfs/all-cap drop/no-new-privileges/readiness, v5, 8개 tool discovery/call, active contract 및 embedding row 전수채점, exact block, cross-contract 0, bundle/revision/legacy adapter, PDF 206·`%PDF-`·Content-Range |
| `native_cache_before`, `native_cache_after` | 동일 native namespace의 정렬된 exact-path 200/404 control inventory |
| `native_cache_audit` | exact-path GET-only hit/miss, HEAD 0과 native create/modify/delete/write/publication 0 |
| `generation_cas` | manifest가 참조한 sorted unique PDF/OCR CAS 전건, logical publish와 실제 create 수, candidate DB/vector/manifest/READY success, pointer CAS, 별도 실제 HTTP write 수, native/stable write 0 |
| `rollback_ledger` | v4 → v5 → v5 restart → v4 rollback → 최종 v5, 각 단계의 health/tool 및 v4 legacy-hybrid·`search_contracts` rejection/v5 exact smoke |
| `v109_identity` | Docker runtime, image, volume, systemd unit, local stable pointer, WebDAV stable channel의 before/after hash equality와 운영 mutation 0 |

Worker의 `embedding_provider_calls`는 0 이상입니다. 최종 run에서 새 v5 embedding cache가
전건 hit일 수 있으므로 양수 자체를 합격 조건으로 만들지 않습니다. Native control의 별도
exact-path GET hit/miss와 Worker run의 OCR cache hit/miss도 서로 같은 관측 모집단이라고
가정하지 않고 각각 봉인합니다.

일반 ledger와 receipt는 canonical JSON 뒤 정확히 LF 하나를 둡니다. production
`GenerationManifest`, `GenerationReady`, `GenerationPointer` 파일은 기존 원격 계약 그대로 LF
없는 canonical bytes여야 합니다. 모든 파일은 receipt에 상대 경로, full-file SHA-256과 크기로
결속되고, 경로 재사용은 금지됩니다.

## Verifier의 fail-closed 범위

검증기는 다음을 모두 다시 계산하거나 교차 비교합니다.

- receipt의 독립 승인 SHA-256과 exact candidate source commit
- strict schema, extra field/duplicate key/non-finite/non-canonical JSON 거부
- `O_NOFOLLOW` descriptor walk, regular file/size/SHA와 read 전후 inode·mtime·ctime identity
- manifest → READY → pointer와 SQLite/vector artifact binding
- effective config → Worker/MCP, stable/OCR/GC approval false, experimental map-reduce false,
  sealed embedding provider/token profile 및 aggregation/retrieval policy binding
- Compose rendered `repository@OCI-index` → platform manifest의 config digest → 실제 Worker/MCP
  container `.Image` ID와 local RepoDigest의 exact binding. local/tag/build fallback은 허용하지 않음
- Worker 64 GiB state/2 GiB reserve/16 GiB sidecar/4 GiB DB/32 GiB startup floor와 MCP
  1 GiB legacy·resident vector/16 GiB sidecar/4 GiB DB/32 GiB download/64 GiB state/
  2 GiB reserve, exhaustive audit 32 jobs/2 GiB total/256 MiB artifact, reranker audit
  1,024 jobs/512 MiB total/8 MiB artifact exact literals; ambient override 하나라도 거부
- manifest issuer/count/structure/vector → Worker metrics binding
- 각 카드사의 acquired=succeeded와 failed=0; generic manifest의 95% publication 하한을
  candidate zero-defect 합격 기준으로 완화 해석하지 않음
- manifest vector row → MCP expected/scored row binding과 8-tool exact smoke
- native before/after inventory equality, 별도 zero-write audit와 generation-only CAS. 기존 CAS
  object의 verified reuse는 logical publish이지만 실제 create/write 0일 수 있고, MKCOL/temp
  PUT/MOVE/delete 때문에 실제 HTTP write 수를 logical call의 단순 합으로 유도하지 않음
- v4/v5/restart/rollback/final-v5 순서와 최종 receipt generation 복귀
- v1.0.9 운영 자산 전체의 before/after equality와 금지된 mutation count 0

이는 입력 파일의 integrity와 선언된 상호 계약만 검증합니다. source replay를 직접 수행하거나,
실행 장부에 기록되지 않은 네트워크·Docker·운영 mutation의 부재를 만들어 내지 않습니다.
승인자는 원본 command/result, runtime identity, 민감정보 제거와 capture completeness를 별도로
검토해야 합니다.

## Source commit과 evidence-only sealing commit

실데이터 증거가 자기 자신을 포함한 Git commit을 참조하는 순환을 피하기 위해 release tag
commit은 두 단계로 구성합니다.

1. `candidate_source_commit`에서 code, workflow, docs, lockfile과 Dockerfile을 고정하고 그
   exact source로 candidate replay를 수행합니다.
2. 이후 commit에는 `release-evidence/v1.0.10/**`만 추가합니다. release workflow는 source가
   tag commit의 strict ancestor인지, diff가 비어 있지 않은지, NUL-safe name list와 source-side
   exclusion diff 모두로 evidence 밖 변경이 0인지 검사합니다.

aggregation profile, gold evaluation/capture-set과 candidate receipt validator에는 모두
`candidate_source_commit`을 전달합니다. tag의 `GITHUB_SHA`를 evidence source로 사용하지
않습니다.

검증 명령은 다음과 같습니다.

```bash
python -m cardrag_core.candidate_acceptance \
  --receipt "$PWD/release-evidence/v1.0.10/candidate-acceptance-receipt.json" \
  --evidence-root "$PWD/release-evidence/v1.0.10" \
  --expected-receipt-sha256 "$CANDIDATE_ACCEPTANCE_SHA256" \
  --expected-source-commit "$CANDIDATE_SOURCE_COMMIT" \
  --expected-image-repository \
    ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate
```

성공 시 secret-free canonical `cardrag.candidate-acceptance-validation.v1` 한 줄을 출력하고,
실패 시 evidence 내용이나 경로를 출력하지 않은 채 nonzero로 종료합니다.

## Strict image gate

### Private candidate image producer

수락 대상 image를 public Docker Hub에 먼저 올리지 않습니다. 허용된 source repository는
`ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate` 하나뿐입니다. 이 package는 사전에
`private`로 만들고 이 GitHub repository에 연결해야 합니다. 현재 repository owner는 GitHub
API상 `User`이므로 workflow는 private package도 반환하는 authenticated, paginated user-package
list endpoint와 owner type을 함께 검증합니다. public package 전용 단건 endpoint는 사용하지
않습니다. 그 뒤 exact-name 결과가 하나뿐인지, `package_type=container`, `visibility=private`,
owner와 연결 repository identity가 같은지 확인하며
하나라도 다르면 image를 읽기 전에 실패합니다. package 생성·visibility 설정과 candidate push는 별도
승인된 producer 단계이고 public release 승인이 아닙니다. package의 GitHub Actions access에도 이
repository를 `Read`로 명시해야 하며 repository 연결만으로 read 권한을 추정하지 않습니다. release
jobs의 `packages: read` GITHUB_TOKEN이 metadata 조회와 pull을 둘 다 통과해야 합니다.

승인된 producer는 local checkout을 build context로 사용하지 않고 full 40-hex commit으로 고정한
remote Git context를 사용합니다. 따라서 staged/unstaged/untracked byte를 같은 VCS label로
위장할 수 없습니다. tag는 운반 수단일 뿐이며 acceptance authority는 build 결과의 exact
digest입니다.

```bash
set -euo pipefail
[[ "$CANDIDATE_SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ ]]
: "${GIT_AUTH_TOKEN:?contents-read token for the private Git context is required}"
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
    --build-arg APP_VERSION=1.0.10 \
    --build-arg "VCS_REF=$CANDIDATE_SOURCE_COMMIT" \
    --build-arg PYTHON_DEV_IMAGE=cgr.dev/chainguard/python:latest-dev@sha256:4e2adecf67a1d18773c55b5526b47436392b9816ae6b8d92575979a2ab9de8b2 \
    --build-arg PYTHON_RUNTIME_IMAGE=cgr.dev/chainguard/python:latest@sha256:f47d995d001c1f949d560b1158d7f3ae556aad75a1044e72a125c900c1f05332 \
    --build-arg UV_IMAGE=ghcr.io/astral-sh/uv:0.8.17@sha256:e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1 \
    --build-arg CODEX_VERSION=0.147.0 \
    --build-arg CODEX_SHA256=0246e2e773834e07f0fb5249ed6ebad12e4591e608f8c7bb97dd6a9690544c36 \
    --secret id=GIT_AUTH_TOKEN,env=GIT_AUTH_TOKEN \
    --attest type=provenance,mode=max,version=v0.2 \
    --attest type=sbom,generator=docker.io/docker/buildkit-syft-scanner:stable-1@sha256:ae4f3b554449e7e25548e7d8ccc029d17357348e30c6e3df01b92bc93654d6a9 \
    --output "type=registry,name=$candidate_repository:candidate-v1.0.10-$role-$CANDIDATE_SOURCE_COMMIT,oci-mediatypes=true,oci-artifact=true" \
    "$source_context"
done
```

허용된 explicit build arg는 위 일곱 개의 exact key/value뿐입니다. 이 값은 immutable Git
context의 Dockerfile default와 같아야 하며 omission, 다른 값 또는 추가 `build-arg:*`는 모두
실패합니다. `--build-context`/`context:*`, alternate `filename`, label, entitlement, local/SSH
input도 금지합니다. private Git fetch에 쓰는 `GIT_AUTH_TOKEN`은 contents-read scope로 제한하고
predefined BuildKit secret으로만 전달합니다. token byte는 로그, build arg, image 또는
provenance에 넣지 않으며 provenance에는 secret identity와 `optional=true` metadata만 남습니다.
token이 없거나 private commit을 읽을 수 없으면 build가 성공해서는 안 됩니다.

이 수동 producer와 private GHCR write 권한은 명시적인 외부 trust boundary입니다. 위
Buildx/BuildKit version 검사는 실행 계약일 뿐 binary issuer를 암호학적으로 인증하지 않으며,
candidate 안의 raw SLSA statement도 그 자체로 producer identity 서명이 아닙니다. public copy
뒤의 release Cosign 서명은 promotion workflow identity를 증명할 뿐 이전 private build의
issuer를 소급 증명하지 않습니다. 따라서 승인자는 private push 권한, producer command/log,
tool binary provenance와 exact output digest를 별도로 검토하고 승인해야 합니다. 현재는 이
private candidate digest에 결속된 독립 producer signature/attestation과 실제 raw BuildKit
fixture가 없으므로 `candidate_acceptance_sha256` 및 `dockerhub-public` 승인을 발급해서는 안
되는 hard external release blocker입니다. 이 경계를 raw provenance의 문자열 검사나 release
후 서명으로 완화하지 않습니다.

producer는 각 role의 OCI index digest, 유일한 `linux/amd64` child digest, 그 manifest의 config
digest와 child를 참조하는 BuildKit attestation-manifest digest를 기록합니다. 실제 candidate
Compose와 Worker/MCP smoke는 tag가 아니라 이 index digest를 사용해야 하며 candidate overlay는
private repository를 YAML에 고정하고 receipt-bound `sha256` digest 변수가 없으면 render
단계에서 실패하며 inherited `build:`를 제거합니다.
effective config와 receipt는 네 digest, rendered reference, revision, version, entrypoint, user를
봉인합니다. runtime 증거는 container `.Image`가 config digest이고 image RepoDigests가 rendered
index reference를 포함하는지도 봉인합니다. Worker receipt는 provider call이 0인 all-hit
run이어도 별도로 Codex/bubblewrap version, bubblewrap user namespace와 Codex read-only sandbox를
exact effective-config/image에서 통과했음을 요구합니다.

release dispatch의 `candidate_worker_image_digest`와 `candidate_mcp_image_digest`는 독립 승인된
receipt 출력과 정확히 같아야 합니다. verifier는 두 image repository가 위 private allowlist인지도
검사합니다. 불일치하거나 private package/digest/blob이 없으면 publish 전에 실패합니다.

### Exact scan and promotion

tagged worktree와 release evidence에는 별도의 checksum-pinned strict filesystem scan을
실행합니다. evidence-only tag와 candidate source 사이에 code/workflow/docs drift가 없다는
ancestor/diff gate를 먼저 통과하고 checkout credential persistence를 끈 뒤
`vuln,secret,misconfig`, `HIGH,CRITICAL`, unfixed 포함 정책으로 검사합니다. Trivy 0.74의
walker가 `.git` 등 자체 default skip 경로를 제외하므로 이 gate를 Git history scan이라고
주장하지 않으며 receipt scope에도 `trivy-default-skips`를 명시합니다. current `uv.lock`에 대해
Trivy가 언어 root package를 완전히 해석하지
못할 수 있으므로 filesystem scan은 source/secret/misconfiguration 보조 gate이고 dependency
completion authority는 receipt-bound final-image 두 건의 scan입니다. 실제 filesystem JSON,
Trivy/DB metadata와 receipt SHA를 public copy 직전에 다시 검증하고 release asset에 포함합니다.

strict job은 read-only `packages:read` token으로 receipt-bound private digest를 가져옵니다. OCI
index는 descriptor가 정확히 둘뿐이어야 하며, 하나는 sealed linux/amd64 child, 다른 하나는
그 child를 subject로 하는 OCI artifact attestation manifest여야 합니다. arm64/다른 platform,
unattested image, duplicate 또는 추가 attestation은 모두 거부합니다. attestation layer에도
SPDX와 SLSA provenance predicate가 정확히 하나씩 있어야 합니다. raw in-toto layer를
digest로 직접 읽고 두 statement의 subject가 sealed linux/amd64 child인지 확인합니다.
source/destination OCI index와 attestation manifest, raw provenance/SBOM은 jq 평가 전에
128 MiB bounded strict JSON parser로 duplicate key와 NaN/Infinity를 거부합니다.
Provenance는 raw v0.2 statement의 subject, `buildType`, manual Buildx builder identity,
exact Git `configSource` URI/SHA-1, `Dockerfile` entrypoint, linux/amd64 environment, role target,
exact seven build args, exact Git secret identity, 빈 local/SSH input과 complete materials를
구조적으로 검사합니다. material set도 role별로 정확히 Git source, digest-pinned Dockerfile
frontend, UV/Python base, SBOM generator, Worker의 Codex HTTP object 또는 MCP minimal runtime만
허용합니다. SBOM generator의 linux/amd64 child는
`sha256:187e1892a7752c9384c59aba9517dd8e40610b748c72773e87b63720514463c2`로
별도 결속합니다.
임의 문자열 위치에 commit이 한 번 등장하거나 추가 immutable material을 넣는 것만으로는
통과하지 않습니다. SBOM은
SPDX 2.3과 nonempty package inventory를 요구하며 두 본문을 각각 SHA-256으로 봉인합니다.

그 뒤 SHA-256으로 고정한 Trivy `0.74.0`을 다음 정책으로 실행합니다.

```text
scanners=vuln,secret
severity=HIGH,CRITICAL
exit-code=1
ignore-unfixed=false
```

`--ignore-unfixed` 예외는 없으며 어느 한 image에서 HIGH/CRITICAL 한 건이라도 발견되면
`registry-preflight`와 publish는 실행되지 않습니다. 실제 scanner JSON과 `trivy --version
--format json` DB metadata를 업로드하며, `UpdatedAt`은 실행 시각 기준 36시간 이내,
`DownloadedAt`은 2시간 이내여야 합니다. synthetic pass 문장만으로는 다음 단계가 열리지
않습니다.

public `dockerhub-public` environment 승인 뒤에만 checksum-pinned `crane 0.22.0`이 private
source index와 모든 child/attestation blob을 Docker Hub immutable alias로 재귀 복사합니다.
이 environment는 required reviewer가 1명 이상이어야 하고 `prevent_self_review=true`,
`can_admins_bypass=false`, custom deployment policy의 유일한 tag pattern이 `v*.*.*`여야 합니다.
reviewer의 `{type,id}` 집합은 source-controlled `approved-reviewers.json`과 정확히 같아야 하며 추가,
대체, 중복, malformed reviewer를 모두 거부합니다. 현재 allowlist는 비어 있으므로 독립 reviewer를
정하고 새 candidate source에서 numeric ID를 봉인하기 전에는 의도적으로 통과할 수 없습니다.
workflow는 repository `Actions: read` 권한으로 repository visibility와 이 상태를 Docker Hub 인증
전과 실제 public copy 직전에 REST API로 다시 읽습니다. 조회 실패나 drift는 모두 public write
전에 실패합니다.

2026-08-30 read-only audit에서 repository는 private personal-User 소유이고 environment는 protection
rule이 없으며 admin bypass가 허용됩니다. GitHub Free/Pro personal private repository에서는 이
required-reviewer/no-bypass 보호가 scheduler에 적용된다고 증명할 수 없으므로 verifier도 repository
visibility가 `public`이 아니면 명시적으로 실패합니다. repository 공개 전환은 승인 범위를 크게
넓히는 외부 변경이므로 이 문서는 이를 승인하지 않습니다. private 상태를 유지하려면
Enterprise-backed organization 이전과 package ownership/verifier 재감사 또는 별도 독립 서명 승인
설계가 필요합니다. 그 전에는 release를 dispatch하거나 승인해서는 안 됩니다.
승인이 지연되면 image와 filesystem Trivy `UpdatedAt<=36h`, `DownloadedAt<=2h`를 현재 UTC로
다시 검사하고 오래된 run은 public write 전에 실패합니다. destination OCI index digest,
linux/amd64 child, platform config, attestation subject/digest와 raw SPDX/provenance blob byte가 receipt/scan과 모두
같은지 재확인한 뒤 exact digest에 Cosign 서명합니다. tag commit `GITHUB_SHA`에서 image를 다시 빌드하지
않으며 `accepted == scanned == published` digest가 깨지면 중단합니다.

기존 `039453baf4ca` Bookworm Worker/MCP image는 각각 5 CRITICAL/29 HIGH로 strict policy에
실패했습니다.

교체 source는 linux/amd64에서 다음 digest로 고정했습니다.

- Worker builder/final:
  `cgr.dev/chainguard/python:latest-dev@sha256:4e2adecf67a1d18773c55b5526b47436392b9816ae6b8d92575979a2ab9de8b2`
- MCP minimal final:
  `cgr.dev/chainguard/python:latest@sha256:f47d995d001c1f949d560b1158d7f3ae556aad75a1044e72a125c900c1f05332`
- Worker 추가 패키지: signed Wolfi index의 `bubblewrap=0.11.2-r0`, `libcap=2.78-r0`

Trivy `0.74.0`, DB `UpdatedAt=2026-08-29T18:58:09Z`, vuln+secret,
HIGH/CRITICAL, unfixed 포함 조건에서 두 base와 Worker 추가 component SBOM은 모두 0/0입니다.
이는 아직 최종 CardRAG image 0/0 증명이 아닙니다. 새 private candidate OCI index를 실제로
build/push하고 pinned Buildx 0.36.1/BuildKit 0.32.2의 raw provenance가 synthetic adversarial
policy fixture와 같은 exact frontend args/material URI/digest/secret shape인지 확인해야 합니다.
이번 작업에서는 저장공간 안전 제약 때문에 그 build/raw fixture를 만들지 않았습니다. exact
final Worker/MCP strict scan과 receipt-bound runtime smoke까지 통과할 때까지 release blocker를
유지합니다. 별도 승인 전에는 scanner 예외, severity 완화, public candidate
선게시 또는 release 순서 변경으로 우회하지 않습니다.
