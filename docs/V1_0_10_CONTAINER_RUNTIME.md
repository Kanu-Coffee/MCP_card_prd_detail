# v1.0.10 Chainguard/Wolfi 컨테이너 런타임

이 문서는 v1.0.10 Worker와 MCP의 Bookworm 제거 설계와 release 전 필수 검증을
고정합니다. Dockerfile의 digest pin을 변경하는 일은 보안 업데이트이며, 여기 적힌
검증을 다시 수행하지 않은 tag 이동이나 package fallback을 허용하지 않습니다.

## 고정된 공급망

| 용도 | Dockerfile reference | linux/amd64 child digest |
| --- | --- | --- |
| Dockerfile frontend | `docker/dockerfile:1.7@sha256:b5f3b260a9678e1d83d2fce86eeddf79420b79147eaba2a25986f47133d73720` | 같은 linux/amd64 manifest digest |
| SBOM generator | `docker.io/docker/buildkit-syft-scanner:stable-1@sha256:ae4f3b554449e7e25548e7d8ccc029d17357348e30c6e3df01b92bc93654d6a9` | `sha256:187e1892a7752c9384c59aba9517dd8e40610b748c72773e87b63720514463c2` |
| Python builder와 Worker final | `cgr.dev/chainguard/python:latest-dev@sha256:4e2adecf67a1d18773c55b5526b47436392b9816ae6b8d92575979a2ab9de8b2` | `sha256:48060899de1ce8c95d987a2fc0da2a3ca1ef28d4aac5073bff2068a63f3ccce0` |
| MCP minimal final | `cgr.dev/chainguard/python:latest@sha256:f47d995d001c1f949d560b1158d7f3ae556aad75a1044e72a125c900c1f05332` | `sha256:e410b1cf97a99710ca1393cd4640e97e2784b0b2f3f2455ac38a3eda9b7e74ce` |
| uv build tool | `ghcr.io/astral-sh/uv:0.8.17@sha256:e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1` | Dockerfile pin을 기준으로 검증 |

Dockerfile frontend pin은 2026-08-30에 read-only `buildx imagetools inspect`로
`docker/dockerfile:1.7`의 linux/amd64 descriptor를 해석한 값입니다. provenance의 exact
frontend material과 invocation source/cmdline도 같은 digest에 결속하며 tag drift는
release에서 실패합니다.

두 Chainguard Python image는 이 snapshot에서 CPython `3.14.7-r4`와 glibc 2.44를
공유합니다. 저장소의 `requires-python = ">=3.12, <3.15"` 범위 안이며, build stage는
`UV_PYTHON=3.14`와 `UV_PYTHON_DOWNLOADS=never`로 image 밖 Python 다운로드를
차단합니다. 저장소가 고정한 uv 0.8.17로 Worker와 MCP 각각
`uv sync --frozen --no-dev --no-editable`을 실행합니다.

linux/amd64 host CPython 3.14.4에서 uv 0.8.17과 현재 `uv.lock`으로 두 venv sync 및
CPython 3.14 native wheel import를 별도 임시 경로에서 확인했습니다. pinned image의
CPython 3.14.7에서 Docker layer와 최종 entrypoint가 동작한다는 증명은 아니므로 아래
final-image gate를 대체하지 않습니다.

dev/minimal package inventory에는 모두 `wolfi-baselayout=20230201-r29`,
`python-3.14=3.14.7-r4`, `python-3.14-base=3.14.7-r4`가 있습니다. uv가 생성한 venv의
`bin/python`은 `/usr/bin/python3.14`를 가리키고, 그 경로와 같은 Python/glibc ABI가 두
image에 존재합니다. `runtime-layout`은 동일한 baselayout의 dev passwd/group에 cardrag
10001 항목만 추가해 minimal final에 복사합니다. Python은 별도 service account를
요구하지 않고 최종 `USER`는 숫자로 고정되므로, minimal 기본 계정 이름에는 의존하지
않습니다. 이 정적 ABI/path 검사는 실제 final-image 실행 gate를 대체하지 않습니다.

## 이미지 역할과 런타임 계약

MCP final은 shell과 package manager가 없는 Chainguard minimal image에서 시작합니다.
`runtime-layout` stage가 생성한 numeric UID/GID `10001:10001`, `/etc/passwd`,
`/etc/group`, mode 0700의 `/var/lib/cardrag-mcp`만 복사합니다. `runtime`과 그 MCP
descendant에는 `RUN`을 두지 않습니다. 기존 OCI labels, `cardrag-mcp` entrypoint,
port 8000, `/health/ready` healthcheck를 유지합니다.

Worker final은 bubblewrap과 기존 운영 wrapper 요구 때문에 pinned `latest-dev`를
사용합니다. numeric UID/GID `10001:10001`, mode 0700의
`/var/lib/cardrag-worker`, 기존 labels와 `cardrag-worker run` entrypoint를 유지합니다.
Compose의 read-only rootfs, tmpfs, `cap_drop: [ALL]`, `no-new-privileges` 조건은 별도
runtime smoke에서 그대로 검증해야 합니다.

Worker는 official signed Wolfi index에서 다음 직접 dependency를 exact version으로
설치합니다.

- `bubblewrap=0.11.2-r0`; 확인한 APK SHA-256은
  `92e4644298be253e29f66793e087b5c77e569a6dd39b7d3238302508874a427d`입니다.
- `libcap=2.78-r0`; 확인한 APK SHA-256은
  `0a6dc8ec3226500e7a3546fb19bbcbc3afa270f77d7a91b9f47ee6c6318e7224`입니다.

Wolfi index는 rolling이므로 exact version이 사라지면 `apk add`가 실패하는 것이 의도한
fail-closed 동작입니다. unpinned 새 version, 다른 배포판 package, backport 또는 수동
shared-library 복사로 fallback하지 않습니다. 반복 가능성을 장기간 보장해야 할 때는
위 APK와 Wolfi 서명을 승인된 내부 immutable artifact store에 함께 보관하고, SHA-256과
서명을 모두 검사하는 별도 변경으로 전환합니다.

검사한 bubblewrap ELF는 `/lib64/ld-linux-x86-64.so.2`, `libc.so.6`, `libcap.so.2`에
동적으로 연결됩니다. 따라서 `libcap`을 명시적으로 설치하며, bwrap binary만 다른
runtime으로 복사하지 않습니다. Codex `0.147.0` linux-musl archive는
`0246e2e773834e07f0fb5249ed6ebad12e4591e608f8c7bb97dd6a9690544c36`으로 검사하고,
정적 PIE `codex`와 `codex-linux-sandbox` symlink를 설치합니다.

## 보안 스캔 snapshot과 한계

2026-08-30 조사에서는 Docker daemon이나 local image store를 사용하지 않았습니다.
Trivy 0.74.0, DB `UpdatedAt=2026-08-29T18:58:09Z`, linux/amd64,
`vuln,secret`, `HIGH,CRITICAL`, unfixed 포함 조건의 임시 remote scan 결과는 다음과
같습니다.

| 대상 | CRITICAL | HIGH |
| --- | ---: | ---: |
| pinned Chainguard Python `latest-dev` source | 0 | 0 |
| pinned Chainguard Python minimal `latest` source | 0 | 0 |
| Wolfi bubblewrap 0.11.2-r0 component SBOM | 0 | 0 |
| Wolfi libcap 2.78-r0 component SBOM | 0 | 0 |
| Codex 0.147.0 extracted filesystem | 0 | 0 |

비교 대상인 기존 pinned Bookworm image는 같은 정책에서 CRITICAL 5, HIGH 29였고 모두
unfixed였습니다. 위 0/0은 source와 component 결과일 뿐, 합성된 CardRAG Worker/MCP
final image의 0/0 증명이 아닙니다.

## Release 전에 반드시 통과할 gate

이 변경 작업에서는 저장 공간과 운영 격리를 위해 `docker pull`, `docker build`,
container 실행, prune, service/volume/WebDAV 변경을 전혀 수행하지 않았습니다. release는
다음 항목을 모두 CI에서 fail-closed로 통과하기 전까지 차단합니다.

1. exact source commit에서 `worker`와 `mcp` target을 실제로 빌드하고 OCI version,
   revision, entrypoint label/metadata를 검사합니다.
2. Trivy 0.74.0으로 두 final image를 `vuln,secret`, `HIGH,CRITICAL`, unfixed 포함,
   `--exit-code 1`로 검사해 각각 0/0임을 증명합니다. ignore-unfixed, allowlist,
   severity 완화는 금지합니다.
3. Worker에서 `codex --version`, symlink 해석, `bwrap --version`, 동적 loader와
   `libcap.so.2` 해석을 검사합니다.
4. 실제 Worker Compose 보안 옵션과 UID 10001로 bubblewrap user-namespace smoke와
   Codex read-only sandbox smoke를 실행합니다.
5. MCP를 UID 10001, read-only rootfs로 기동하고 `/health/ready`가 200을 반환하는지
   healthcheck와 함께 검사합니다.

digest나 exact APK version을 갱신할 때는 manifest의 linux/amd64 child, Python/glibc
ABI, uv frozen sync와 native imports, component scan, 두 final-image scan, Worker sandbox,
MCP readiness를 모두 다시 검증합니다. tag 문자열만 갱신하거나 예외 정책으로 gate를
우회하지 않습니다.
