# 레거시 Import·호스트 영속 저장·서버 이전 운영서

새 Docker Standalone 서버의 초기 설치는
[`deploy/portainer/QUICKSTART.ko.md`](../deploy/portainer/QUICKSTART.ko.md), 기존
PDF/OCR의 bundle 준비와 one-shot import는
[`deploy/portainer/LEGACY_IMPORT_QUICKSTART.ko.md`](../deploy/portainer/LEGACY_IMPORT_QUICKSTART.ko.md)의
간편 절차를 먼저 사용할 수 있다. 이 문서는 기존 volume 전환, portable state와 서버
이전까지 포함한 전체 운영 계약이다.

이 문서는 CardRAG `0.2.x`에서 레거시 PDF/OCR을 현재 처리 계약으로 재색인하고,
Portainer Stack의 운영 데이터를 Docker host에 영속화하며, 다른 서버로 일관되게
이전하는 절차의 기준이다. 원본 레거시 디렉터리를 application volume에 직접 복사하는
방식은 지원하지 않는다.

## 1. 데이터 경계

검색 가능한 운영 상태는 다음 세 항목이 같은 시점에 있어야 완전하다.

1. PostgreSQL `cardrag`와 `keycloak` database
2. `/var/lib/cardrag/objects`의 전체 content-addressed object store
3. `/var/lib/cardrag/generations`의 전체 generation store와 `current.json`

`objects`에는 PDF, OCR, 구조화 결과, durable page checkpoint와 artifact manifest가
들어 있다. OCR page text, evidence, FTS와 pgvector index, 작업 ledger와 활성 generation은
PostgreSQL에 있다. `generations`에는 게시 manifest, 품질 보고서, `READY`, publication
history와 현재 pointer가 있다. 어느 하나만 복사한 것은 복구본이 아니다.

`build`와 page cache는 재생성 가능하므로 정상 종료한 portable export에서 제외한다.
Codex device credential도 일반 corpus export에서 제외하고 새 서버에서 다시 로그인한다.

## 2. Portainer host layout

Docker Engine host에 다음 경로를 먼저 만든다. Portainer UI가 실행되는 PC나 Portainer
Server container의 경로가 아니라, 대상 Docker endpoint host의 절대경로다.

```text
/srv/cardrag/
├── runtime/
│   ├── objects/
│   ├── generations/
│   ├── build/
│   └── page-cache/
├── imports/
├── migration/reports/
├── config/
└── secrets/
```

초기화는 repository의 `deploy/portainer/prepare-host-storage.sh`를 root로 한 번 실행한다.
이 스크립트는 UID/GID `10001:10001`, runtime/import `0750`, page cache `0700`을 적용하고
PostgreSQL·Codex external volume이 실제로 존재하는지 확인한다. 경로 중간에 symlink가
있거나 너무 넓은 경로가 지정되면 중단한다.

Portainer용 환경변수의 기준값은 다음과 같다.

```text
CARDRAG_DATA_ROOT=/srv/cardrag/runtime
CARDRAG_IMPORT_ROOT=/srv/cardrag/imports
CARDRAG_ARCHIVE_ROOT=/mnt/cardrag-backup/cardrag
CARDRAG_CONFIG_ROOT=/srv/cardrag/config
CARDRAG_SECRETS_DIR=/srv/cardrag/secrets
CARDRAG_POSTGRES_VOLUME=cardrag-postgres-v1
CARDRAG_CODEX_AUTH_VOLUME=cardrag-codex-auth-v1
```

운영 Stack은 `deploy/portainer/cardrag-stack.yaml`을 사용한다. 모든 host bind는 long
syntax와 `create_host_path: false`를 사용하므로 오타 난 host path를 빈 디렉터리로
자동 생성하지 않는다. PostgreSQL과 Codex auth는 `external: true` volume이라 Stack을
삭제해도 Stack lifecycle에 의해 함께 삭제되지 않는다. 그렇더라도 volume 자체는
backup이 아니며 host disk 장애에 대비한 portable state export가 필요하다.

빈 서버의 최초 Keycloak 관리자는 별도 2-service
`deploy/portainer/cardrag-bootstrap-stack.yaml`로 한 번만 만들고, 영구 관리자를 검증한
뒤 bootstrap Stack과 secret을 제거한다. 03:00/04:00 정기 작업은 개발 named volume용
기본 unit이 아니라 `deploy/portainer/systemd/cardrag-portainer-*` unit으로 실행한다.
설치·Codex device login·timer quiesce의 정확한 순서는
`deploy/portainer/RUNBOOK.md`를 따른다.

## 3. 기존 named volume을 host bind로 전환

현재 volume 이름은 project 이름에 따라 달라질 수 있다. 먼저
`deploy/portainer/discover-legacy-volumes.sh`로 Docker가 보고한 실제 volume 이름과
mountpoint를 기록한다. `/var/lib/docker/volumes` 아래 경로를 추측하거나 실행 중 직접
수정하지 않는다.

전환 전에 scheduler와 retention timer를 끄고 admin one-shot이 끝났는지 확인한다.
`cardrag run list --state running`과 `cardrag job status`에 nonterminal 작업이 없어야 한다.
worker, MCP, Keycloak을 중지하고 PostgreSQL만 유지한 뒤, 먼저 아래 6절의 portable
export를 만든다.

그 다음 `deploy/portainer/storage-migrate.compose.yaml`의 one-shot job을 실행한다. 이
job은 명시적으로 지정한 이전 objects/generations volume을 read-only로, 새 host
디렉터리를 read-write로 mount한다. CAS 이름과 실제 SHA-256, generation manifest,
`READY`, `current.json`을 원본과 대상에서 각각 검사하고 결과가 동일할 때만 target을
활성화한다. 실패한 target은 Stack에 연결하지 않는다.

새 Stack에서는 다음을 확인한다.

- DB `active_generation`과 `current.json`의 generation ID 및 manifest SHA가 같다.
- `cardrag generation verify GENERATION_ID`가 성공한다.
- MCP readiness, 대표 검색, citation과 PDF Range 조회가 성공한다.
- 기존 named volumes는 최소 7일 동안 read-only rollback 자산으로 유지한다.

## 4. 레거시 bundle 준비

원본 9.51 GiB archive는 이름, 내용과 mtime을 바꾸지 않고 read-only로 보존한다. source
PC에서 다음 명령으로 별도 normalized bundle을 만든다.

```bash
cardrag legacy prepare \
  --source /absolute/path/to/cardrag-conveyor-data \
  --manifest /absolute/path/to/cardrag_master_manifest.json \
  --output /absolute/path/to/cardrag-imports \
  --dry-run

cardrag legacy prepare \
  --source /absolute/path/to/cardrag-conveyor-data \
  --manifest /absolute/path/to/cardrag_master_manifest.json \
  --output /absolute/path/to/cardrag-imports
```

bundle ID는 canonical document/source/object inventory의 SHA-256에서 결정된다. 같은
입력은 host path나 실행시각과 무관하게 같은 ID를 만든다. 상품명과 원래 파일명은
record metadata에만 남고 경로에는 issuer, product code, 문서종류, 날짜, 안전한 version과
content hash만 사용한다. symlink, special file, 경로 이탈, hash 불일치, 미기록 파일은
거부한다. `checksums.sha256`과 manifest가 완성된 뒤 `READY`가 마지막에 쓰인다.

검증된 기준 archive의 dry-run 기준은 다음과 같다.

| 항목 | 수량 |
|---|---:|
| document records | 1,592 |
| unique PDF objects | 1,369 |
| unique OCR objects | 1,573 |
| 자동 OCR adoption | 1,590 |
| 재-OCR | 2 |
| normalized payload | 1,262,879,104 bytes |

두 재처리 대상은 OCR hash 불일치 1건과 PDF page count에 비해 OCR page marker가 부족한
1건이다. manifest 밖 orphan PDF/OCR, PNG, SQLite, embedding과 provider temporary file은
정상 payload에 포함하지 않는다.

완성된 `bundle-<digest12>` 전체를 `/srv/cardrag/imports`와 별도 NAS에 복사한다. Portainer
import job에는 import root만 `/mnt/cardrag-imports:ro`로 mount하고 선택한
`CARDRAG_LEGACY_BUNDLE_NAME` 하나만 manifest/READY로 연다. worker와 MCP에는 레거시
source나 bundle을 mount하지 않는다.

## 5. 레거시 Import 실행

import 전에 disk와 inode 여유를 확인한다. 기본 정책은 50 GiB free 미만 또는 사용률
85% 이상이면 신규 import/export를 차단하고, 70%부터 경고한다. 작업 뒤 byte와 inode가
각각 20% 이상 남아야 한다.

Portainer에서는 worker가 실행 중인 상태에서 `legacy-import` one-shot을 시작한다. CLI
직접 실행 기준은 다음과 같다.

```bash
cardrag legacy import --bundle /mnt/cardrag-imports/bundle-<digest12> --wait --no-publish
cardrag legacy status IMPORT_ID
cardrag legacy resume IMPORT_ID
cardrag legacy cancel IMPORT_ID
cardrag legacy finalize IMPORT_ID
```

첫 로그는 실제 `import_id`, `run_id`, candidate generation ID를 JSON 한 줄로 즉시
출력한다. 이후 본문이나 절대 source path를 출력하지 않고 document/byte 진행량, 처리율,
ETA, candidate 상태를 출력한다. 재시작은 같은 import ID와 idempotency key를 사용한다.

채택된 OCR에는 `cardrag.legacy-ocr-adoption.v1` provenance를 기록한다. bundle/import
ledger, PDF SHA, OCR SHA, page coverage가 모두 일치할 때만 현재 OCR 단계를 생략한다.
이를 현재 Codex/PDFium attempt로 가장하지 않는다. 구조화, chunk, embedding과 index는
항상 현재 코드로 새로 만든다. 알려진 2건은 OCR부터 실행한다.

seed가 끝나면 우리·KB current discovery로 신규·변경 PDF를 대사하고, 신한은 신용·체크
history discovery와 OCR을 수행한다. 세 issuer snapshot, current expected/materialized,
latest PDF/OCR/structure/index coverage가 모두 통과하기 전에는 finalize가 게시하지 않는다.
`--no-publish` 실행은 candidate만 준비하며 운영자가 report를 확인한 뒤 별도 finalize한다.

## 6. Portable state export

NAS mountpoint에는 사전에 다음 sentinel을 둔다.

```text
/mnt/cardrag-backup/cardrag/.cardrag-archive-root
cardrag-archive-v1
```

`deploy/portainer/init-archive-root.sh`는 `findmnt`가 보고한 source가 운영자가 지정한 NAS
source와 정확히 같을 때만 sentinel을 만든다. host bind 역시
`create_host_path: false`이므로 NAS가 없을 때 로컬 빈 디렉터리로 fallback하지 않는다.

export는 점검시간에 수행한다.

1. current, 직전 generation을 pin한다.
2. scheduler/retention/admin/worker/MCP/Keycloak을 중지한다.
3. PostgreSQL 외부 session과 nonterminal run/job/import가 없는지 확인한다.
4. `state-export` one-shot 또는 아래 명령을 실행한다.

```bash
cardrag state export --destination /mnt/cardrag-archive
# sealed legacy bundle까지 같은 package에 넣을 때만 추가:
cardrag state export --destination /mnt/cardrag-archive --self-contained
cardrag state verify --source /mnt/cardrag-archive/cardrag-state-...
```

admin image에는 배포 PostgreSQL과 같은 major 17의 `pg_dump`, `pg_restore`, `psql`만
포함한다. password는 file-backed secret에서 child process 환경으로만 전달하며 argv와
로그에 넣지 않는다. export는 두 database를 custom format으로 dump한 뒤 전체 CAS와
전체 generation root를 복사한다. `.incoming`, `.publish.lock`, build, page cache, Codex
auth와 secrets는 제외한다. 기본 Portainer export는 bundle 없이도 동작한다.
`CARDRAG_STATE_SELF_CONTAINED=true`를 명시한 self-contained 모드에서는 sealed legacy
bundle도 포함하며, 이때 요청된 bundle 집합이 비었거나 불완전하면 거부한다.

DB active pointer, filesystem pointer, generation manifest, 모든 DB object key와 실제 CAS
hash를 대사한다. export 전후 DB epoch가 다르거나 파일이 복사 중 바뀌면 실패한다.
package는 marker-owned `.incoming`에서 만들어지고 모든 checksum과 report를 검증한 뒤
`READY`를 마지막에 기록하여 하나의 directory rename으로 확정한다. 성공한 package만
최근 검증본 3개 보존은 NAS snapshot/lifecycle 정책으로 시행한다. v0.2 CLI는 검증되지
않은 package를 잘못 지우지 않도록 자동 삭제를 수행하지 않으며, 운영자는 `READY`가 있고
`cardrag state verify`를 통과한 package만 별도 보관 정책으로 정리한다.

## 7. 다른 서버 Restore

restore target은 비어 있어야 하며 기존 운영 경로를 in-place로 덮지 않는다.

1. 새 host에 runtime 디렉터리, external PostgreSQL/Codex volume과 config/secrets를 만든다.
2. export에 기록된 동일 image digest를 pull한다.
3. 빈 PostgreSQL 17 cluster에서 init script로 네 DB role과 두 빈 DB를 만든다.
4. `state-restore` one-shot 또는 아래 명령을 실행한다.

```bash
cardrag state verify --source /mnt/cardrag-archive/cardrag-state-...
cardrag state restore \
  --source /mnt/cardrag-archive/cardrag-state-... \
  --empty-target \
  --verify-restored
# 동일 검증만 다시 실행할 때
cardrag state verify-restored \
  --source /mnt/cardrag-archive/cardrag-state-...
```

restore는 package 전체를 먼저 검증하고 object/generation을 sibling staging에서 준비한다.
파일을 만들거나 권한을 바꾸기 전에 두 filesystem target, 두 target database, export ID로
결정되는 두 staging database의 ownership, 네 file-backed role secret을 모두 검사한다.
CardRAG와 Keycloak dump는 둘 다 staging database에서 복원·검증된 뒤에만 target 이름으로
활성화하고, 새 host의 secret 값으로 `cardrag`, `cardrag_worker`, `cardrag_mcp`, `keycloak`
role password를 회전한다. `--verify-restored`는 활성화 후에만 CardRAG DB inspector를 열어
DB epoch와 모든 filesystem byte/reference를 즉시 대사한다. Portainer 전용 restore Stack은
이 옵션을 강제하므로 복원만 성공하고 사후 검증을 건너뛴 상태를 성공으로 표시하지 않는다.
같은 export ID 재실행은 이미 설치된 byte/DB epoch가 정확히 같을 때만 해당 단계를 건너뛴다.

복원 뒤에는 migration checksum, pgvector version, 모든 CAS hash, DB object reference,
`active_generation == current.json`, manifest/READY를 다시 검사한다. build/page cache는 빈
상태로 둔다. Keycloak과 MCP만 먼저 시작하여 인증, readiness, 검색, citation, 원 PDF와
Range를 시험한다. 원래 current generation ID를 기록하고 이전 generation으로 rollback한
뒤 읽기 시험을 반복하며, 다시 기록한 원래 ID로 roll-forward하고 DB/current pointer를
대사한 뒤에만 worker/scheduler와 proxy를
전환한다. Codex auth는 복원하지 않으므로 worker OCR 전 device login 상태를 확인한다.

## 8. Rollback과 금지사항

- 새 서버 검증 전 실패하면 새 Stack과 새 target을 폐기하고 기존 server를 재시작한다.
- cutover 뒤 corpus만 문제이면 server rollback보다 generation rollback을 우선한다.
- 첫 generation이라 rollback 대상이 없으면 `cardrag generation deactivate --expected ID`로
  DB와 file serving authority를 함께 제거하여 readiness를 503으로 만든다.
- 이전 server와 새 server의 writer를 동시에 실행하지 않는다.
- 새 server의 audit/session/job 데이터를 이전 server에 자동 병합하지 않는다.
- live PostgreSQL PGDATA를 tar/cp하지 않는다.
- 전체 object reference catalog와 GC mark가 완성되기 전 CAS를 선별 export하거나 삭제하지
  않는다.
- 원본 레거시 archive, sealed bundle과 portable state package를 수동 rename/수정하지 않는다.

운영 완료는 export 로그만으로 인정하지 않는다. 다른 빈 host에서 정기 restore drill을
수행하고 readiness, 인증, 대표 검색/citation/PDF 조회까지 통과한 report를 보존한다.
