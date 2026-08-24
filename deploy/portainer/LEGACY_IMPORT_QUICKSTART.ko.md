# 레거시 PDF·OCR 간편 이전 가이드

레거시 파일을 `/var/lib/cardrag/objects`에 직접 복사해도 검색 catalog나 generation에
연결되지 않는다. 이 가이드는 원본을 읽기 전용으로 유지하면서 다음 흐름을 간단히
수행한다.

```text
원본 archive(RO) -> normalized bundle -> 검증·원자 설치 -> import(no-publish)
                  -> status/resume -> 명시적 finalize
```

helper는 SSH password, Portainer password 또는 API token을 요구하지 않는다. 원본 PC와
Docker host가 다르면 bundle directory를 rsync, NAS 또는 USB로 옮긴 뒤 Docker host에서
다시 전체 검증한다.

## 1. 원본 PC에서 bundle 만들기

GNU/Linux의 검증된 CardRAG release checkout에서 실행한다. 원본 directory와 master
manifest는 변경되지 않으며 source 내부 symlink, special file, hash 불일치는 거부된다.

먼저 dry-run으로 예상 문서 수와 예외를 확인한다.

```sh
cd /opt/cardrag
deploy/portainer/cardrag-legacy-transfer.sh prepare \
  --source /absolute/path/to/cardrag-conveyor-data \
  --manifest /absolute/path/to/cardrag-conveyor-data/artifacts/manifests/cardrag_master_manifest.json \
  --output /absolute/path/to/cardrag-bundles \
  --dry-run
```

기대 기준은 document 1,592건, unique PDF 1,369개, unique OCR 1,573개,
OCR 자동 채택 1,590건과 재-OCR 2건이다. 차이가 있으면 실제 bundle을 만들기 전에
원인을 확인한다.

```sh
deploy/portainer/cardrag-legacy-transfer.sh prepare \
  --source /absolute/path/to/cardrag-conveyor-data \
  --manifest /absolute/path/to/cardrag-conveyor-data/artifacts/manifests/cardrag_master_manifest.json \
  --output /absolute/path/to/cardrag-bundles
```

결과는 `/absolute/path/to/cardrag-bundles/bundle-<digest12>`이다. helper는 처리 전후
path/type/mode/owner/size/mtime metadata fingerprint가 같은지 검사하고, 선택된 payload
내용은 `legacy prepare/verify`의 SHA-256 검증으로 확인한다.

## 2. Docker host로 전달하고 설치

bundle directory 전체를 임시 위치로 복사한다. 아래 rsync는 예시일 뿐이며, 전송
자격증명은 CardRAG가 관리하지 않는다.

```sh
rsync -a --numeric-ids \
  /absolute/path/to/cardrag-bundles/bundle-<digest12>/ \
  docker-host:/var/tmp/bundle-<digest12>/
```

Docker host에서 quick installer가 만든 import root에 설치한다.

```sh
cd /opt/cardrag
sudo deploy/portainer/cardrag-legacy-transfer.sh install \
  --bundle /var/tmp/bundle-<digest12> \
  --import-root /srv/cardrag/imports
```

helper는 source bundle을 검증하고 `${CARDRAG_IMPORT_ROOT}/.incoming`에 복사한 뒤 다시
검증한다. `READY`를 마지막에 복사하고 같은 filesystem에서
`bundle-<digest12>`로 원자적으로 publish한다. 중단되면 같은 명령을 재실행한다.
완료 상태는 다음 명령으로 확인한다.

```sh
sudo deploy/portainer/cardrag-legacy-transfer.sh transfer-status \
  --bundle bundle-<digest12> \
  --import-root /srv/cardrag/imports
```

원본 PC와 Docker host가 같으면 `prepare-install` 하나로 두 단계를 실행할 수도 있다.

## 3. Portainer에서 import one-shot 실행

`install`의 마지막 출력은 Portainer에 한 번만 적용할 non-secret 환경변수 block이다.
`cardrag` Stack의 environment에서 그 값을 적용하고 Stack을 한 번 재배포한다.
`legacy-import` container의 첫 JSON log에서 `import_id`, `run_id`,
`generation_id`를 기록한다.

작업이 시작된 직후 helper가 안내한 `portainer-env disable` 값을 적용하여 one-shot
selector를 다시 비활성화한다. import는 PostgreSQL ledger와 worker queue에서 계속된다.
정상 연결이 끊겨도 새 import를 만들지 않는다.

```sh
deploy/portainer/cardrag-legacy-transfer.sh portainer-env disable
```

상태 조회에 필요한 일회성 env는 다음처럼 만든다.

```sh
deploy/portainer/cardrag-legacy-transfer.sh portainer-env status \
  --import-id IMPORT_ID
```

중단된 작업은 같은 ID와 같은 bundle로 재개한다.

```sh
deploy/portainer/cardrag-legacy-transfer.sh portainer-env resume \
  --bundle bundle-<digest12> \
  --import-id IMPORT_ID
```

각 one-shot이 시작된 뒤에는 항상 `portainer-env disable` 값을 다시 적용한다.

## 4. 검증 후 명시적으로 게시

import 기본값은 `--no-publish`다. `ready_to_finalize`가 되기 전에는 current generation을
바꾸지 않는다. status report에서 issuer reconciliation, coverage, 알려진 재-OCR 2건,
failure 0건을 확인한 뒤에만 finalize env를 만든다.

```sh
deploy/portainer/cardrag-legacy-transfer.sh portainer-env finalize \
  --import-id IMPORT_ID
```

Portainer에서 이 `ops` one-shot을 한 번 실행하고 log의 published generation ID를
기록한다. 즉시 `portainer-env disable`을 다시 적용한 뒤 MCP readiness, 대표 검색,
citation과 PDF Range 조회를 시험한다.

## 실패 시 원칙

- source archive를 rename, chmod, move하거나 runtime CAS에 직접 복사하지 않는다.
- 손상되거나 `READY`가 없는 bundle을 수동으로 고쳐서 import하지 않는다.
- 실패한 import 대신 새 import를 만들지 말고 같은 `IMPORT_ID`를 resume한다.
- finalize 전에는 운영 current generation이 바뀌지 않는다.
- bundle은 NAS에 장기 보존하되 portable 운영 state와 동일한 것으로 취급하지 않는다.

상세한 reconciliation, portable export/restore 및 server migration 계약은
[../../docs/09_LEGACY_IMPORT_AND_PORTABLE_STATE.md](../../docs/09_LEGACY_IMPORT_AND_PORTABLE_STATE.md)를
참조한다.
