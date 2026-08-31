# CardRAG v1.0.11 archive and state management

이 문서는 2026-08-31 KST의 read-only 조사 결과와 v1.0.11 전환 시 보존 규칙을
기록한다. 결론은 **운영 volume과 cache를 한 디렉터리로 합치지 않고**, immutable
source/archive만 `/home/lee/cardrag-archive`에서 관리하는 것이다. 이 문서와
`tools/cardrag_archive_inventory.py`는 이동·삭제·Docker volume 조작·WebDAV 쓰기를
수행하지 않는다.

## 관측된 자산과 분류

| 분류 | 자산 | 관측 크기/상태 | 지금의 처리 |
|---|---|---:|---|
| active | `librechat-reporting_postgres_data` | 9,451,647,409 B, running ref 1 | 이동·복사·삭제 금지 |
| active | PostgreSQL `cardrag` DB | 8,346,112,483 B, `items` 138,736행 | `pg_dump`/복구시험으로만 백업 |
| active | `cardrag-mcp-v109-candidate-state` | 601,560,621 B, running ref 1 | 절대 변경 금지 |
| protected seed | `cardrag-worker-v109-state` | 3,182,430,079 B, exited refs 2 | v1.0.11 seed/rollback 종료 전 보존 |
| candidate forensic | `cardrag-worker-v110-state` | 3,663,895,400 B, exited ref 1 | v1.0.11 독립 clone의 read-only source |
| secret state | `cardrag-worker-v110-codex-home` | 1,153,373 B, exited ref 1 | 전체 clone/일반 archive 금지 |
| active pointer | `/opt/cardrag/current` | `/opt/cardrag/v1.0.8`을 가리킴 | pointer와 v1.0.8 삭제 금지 |
| rollback | `/opt/cardrag/v1.0.9` | immutable, 2,207,744 B | v1.0.11 rollback window까지 보존 |
| canonical archive | `/home/lee/cardrag-archive` | 10,327,982,080 B | 현 위치·권한·내용 보존 |
| canonical source | archive의 `v0.2.1/` | 10,286,198,784 B | 이동 금지 |
| sealed export | `cardrag-data-kit-adoption-v2-runtime-verification-20260826/` | 20,897,792 B, non-writable | 현 절대 경로 보존 |
| quarantine candidate | `cardrag-data-kit-adoption-v2-verification/` | 20,881,408 B, writable entries 존재 | 비교·승인 전 삭제 금지 |
| duplicate import | LibreChat `import/` | 3,511,750,656 B | 아래 gate 뒤에만 정리 후보 |
| old install copies | `/opt/cardrag-v1.0.0`~`v1.0.6` | 합계 9,584,640 B | 이득이 작으므로 cutover 뒤 quarantine |

`/home/lee/.openclaw/workspace/librechat-reporting/import/sqlite-rag/current`의 SQLite
6개(논리 합계 3,434,033,152 B)는 canonical archive의 `data/db` 6개와 파일별
SHA-256이 모두 일치한다. `import/source-ocr` 3,266개/25,569,186 B와
`import/source-markdown` 1,592개/21,711,269 B도 canonical archive의 대응 tree와
byte-for-byte 동일하며 서로 hardlink가 아닌 독립 inode다. 다만 migration Compose와
운영 문서가 현재 import 경로를 참조하므로 즉시 삭제할 수 없다.

WebDAV 운영 base는 credential을 제외하면 `/home/cardrag`이고, 그 아래 `v1/`에
`.health`, `.incoming`, `channels`, `generations`, `objects`, `ocr-cache`가 있다.
`ocr-cache/adopted`에는 2,285 reuse-key directory(기존 727 + v2 1,558)가 관측되었다.
Depth-infinity 조회는 403이며 quota는 보고되지 않았다. 별도
`/home/cardrag-archive/` collection은 아직 404이고 OPTIONS는 MKCOL/PUT을 광고하지만,
용량·ACL·restore 가능성은 쓰기 전에 별도로 증명해야 한다.

## canonical local layout

기존 두 경로는 manifest의 `source_root`와 운영 절차에 절대 경로로 결속되어 있으므로
재배치하지 않는다.

```text
/home/lee/cardrag-archive/
├── v0.2.1/                                      # pinned legacy root; keep in place
├── cardrag-data-kit-adoption-v2-runtime-verification-20260826/
│                                                   # pinned sealed export; keep in place
├── catalog/                                      # 새 inventory/승인 receipt
├── snapshots/<version>/<role>/<UTC>/             # 비운영 복구 snapshot
├── install-snapshots/<version>/<revision>/       # 필요 시 설치 tree 증거
└── quarantine/<UTC>/<exact-source-id>/            # 승인된 보존 단계만
```

archive는 runtime mount의 read-write destination이 아니며 systemd `WorkingDirectory`,
Docker named volume, Worker state path로 사용하지 않는다. archive 내부 deduplication은
content-addressed 단일 object와 manifest reference로 표현하고 hardlink로 구현하지 않는다.

## v1.0.11 state clone gate

현재 host 여유 공간은 89,489,960,960 B(83.34 GiB)다. v1.0.10 Worker와 Codex volume을
모두 독립 복사해도 85,824,912,187 B(79.93 GiB)가 남아 32 GiB startup floor보다
51,465,173,819 B(47.93 GiB) 많다. 따라서 용량상 clone은 가능하지만 다음 순서를
원자적으로 지켜야 한다.

1. v1.0.10 Worker가 terminal이고 source volume을 RW mount한 running container가 0인지
   다시 확인한다.
2. Candidate용 `cardrag-worker-v111-candidate-state`와
   `cardrag-worker-v111-candidate-codex-home`을 새 이름으로 만든다. Stable 기본값인
   `cardrag-worker-v111-state`와 `cardrag-worker-v111-codex-home`을 candidate에
   사용하지 않는다.
   기존 volume 이름을 재사용하거나 두 version을 같은 destination에 mount하지 않는다.
3. v1.0.10 state 전체를 source `ro`, destination `rw`, network disabled인 pinned helper로
   독립 복사한다. SQLite DB와 WAL/SHM, run seal, PDF cache를 선택적으로 합치지 않는다.
4. 복사 전후 manifest의 상대 경로·type·mode·UID/GID·size·SHA-256으로 계산한
   `content_tree_sha256`가 같아야 한다. device/inode가 같은 파일은 hardlink이므로
   실패시킨다. destination의 application DB에 `PRAGMA integrity_check`를 수행하고 run
   checkpoint/seal을 애플리케이션 검증기로 다시 읽는다.
5. Codex volume은 새로 초기화하고 현재 4,035 B `auth.json` 한 파일만 기존 hardened
   `O_NOFOLLOW`, size cap, mode-0600, fsync/atomic-copy 절차로 옮긴다. goals/logs/memories/
   queue/state SQLite와 WAL, model cache, skills, tmp symlink는 복사하지 않는다.
   `auth.json`은 local archive나 WebDAV에 업로드하지 않는다.
6. v1.0.11은 `candidate-v1.0.11`, v111 전용 volume, OCR cache `read-only`, remote GC
   disabled로 시작한다. v1.0.9 source는 seed 시에도 `ro`만 허용한다.

이 clone은 candidate 전용이며 stable volume이 아니다. Stable promotion은 candidate
Worker와 MCP가 모두 stopped이고 세 candidate volume의 running mount가 0일 때만 시작한다.
Worker/MCP state는 각각 새 `cardrag-worker-v111-state`와 `cardrag-mcp-v111-state`로
독립 offline copy한 뒤 no-hardlink tree digest와 모든 SQLite integrity를 검증한다. Stable
`cardrag-worker-v111-codex-home`은 새로 초기화하고 `auth.json`만 descriptor copy한다.
Candidate volume을 stable Compose에 직접 지정하거나 candidate/stable state를 merge하지
않는다. Canonical 명령은 [v1.0.11 migration](V1_0_11_MIGRATION.md#4-release와-stable-cutover)을
따른다.

## copy, verify, retain, delete-later

1. **Inventory:** source를 멈추거나 application-consistent snapshot을 얻고 immutable
   manifest와 owner/승인자를 기록한다. 여러 root의 단순 순차 scan은 전역 atomic
   snapshot이 아니다.
2. **Copy:** staging destination에 독립 byte copy한다. live SQLite/PostgreSQL 디렉터리를
   일반 파일 복사하지 않고 SQLite backup/정지 snapshot, `pg_dump` 또는
   `pg_basebackup`을 사용한다.
3. **Verify:** 전 파일 readback SHA-256, tree content hash, SQLite integrity, PostgreSQL
   scratch restore, expected row count와 애플리케이션 MCP readback을 모두 통과한다.
   WebDAV는 payload를 먼저 올리고 manifest와 `COMMITTED.json`을 마지막에 올린다.
4. **Retain:** source는 v1.0.11 stable full run 2회, MCP readback, 실제 restore drill을
   모두 통과한 뒤에도 최소 30일 보존한다. v1.0.8/v1.0.9 rollback 자산과 v1.0.10
   장애 증거는 이 기간보다 긴 기존 rollback/incident 보존 정책을 따른다.
5. **Delete later:** 새 exact inventory, 연결 container 0, process/ref 0, 승인된 path/volume
   이름, secondary copy 복구 성공을 다시 확인한 별도 변경에서만 삭제한다. glob,
   `docker volume prune`, archive tool 출력만으로 삭제를 승인하지 않는다.

### 절대 금지

- live cache/CAS/SQLite/WAL/state와 archive 사이 hardlink, snapshot 사이 hardlink
- v1.0.9/v1.0.10/v1.0.11 state를 한 volume이나 한 directory로 merge
- remote `v1/objects` 또는 `v1/ocr-cache`를 archive namespace로 재사용
- active MCP volume, PostgreSQL volume, `/opt/cardrag/current`의 이동/삭제
- Codex home 전체 또는 credential/token을 일반 archive/WebDAV에 저장
- stable publication 승인을 remote GC/legacy cleanup 승인으로 간주

## WebDAV secondary archive

WebDAV를 쓸 경우 live `/home/cardrag/v1`의 하위가 아니라 별도 ACL과 credential을 가진
`/home/cardrag-archive/cardrag-archive-v1/`을 사용한다.

```text
cardrag-archive-v1/
├── bundles/sha256/<prefix>/<sha256>
├── snapshots/<source-id>/<UTC>/manifest.json
├── snapshots/<source-id>/<UTC>/COMMITTED.json
└── catalog/<UTC>.json
```

quota, TLS, credential scope, PUT 후 GET hash, partial upload 복구, scratch restore가 모두
검증되기 전에는 primary archive로 간주하지 않는다. WebDAV 한 벌은 같은 서버 local
disk의 장애를 막는 백업이 아닐 수 있으므로 local canonical과 별도 failure domain의
secondary copy를 함께 둔다.

## 실제 정리 후보와 현재 결론

- LibreChat import duplicate 약 3.5 GB: Compose의 `CARDRAG_SQLITE_DIR`와 문서를 canonical
  archive read-only 경로로 바꾸고 scratch migration/row-count/rollback을 시험한 뒤 후보.
- `/opt/cardrag-v1.0.0`~`v1.0.6`와 대응 symlink: live ref는 관측되지 않았지만 9.6 MB에
  불과하며 v1.0.11 cutover/rollback gate 전에는 정리하지 않는다.
- writable v2 verification export: 835개 파일 중 sealed runtime export와 833개가 같고
  2개가 다르다. 차이를 승인·기록한 뒤 quarantine 후보이지 즉시 삭제 대상이 아니다.
- v1.0.10 image/state와 Docker build cache: 장애 재현과 v1.0.11 build가 끝나기 전에는
  prune하지 않는다. image/cache 정리는 raw-data archive와 별도 승인 작업이다.

현재 즉시 폐기 가능한 운영/raw/cache 자산은 **없다**. 먼저 중복 import의 참조 전환과
복구시험을 수행하는 것이 가장 큰(약 3.5 GB) 안전한 정리 기회다.

## read-only inventory tool

도구는 absolute allowlist 아래의 명시적 root만 scan하고 root 경로의 symlink component를
거부한다. tree 안 symlink는 따라가지 않으며 target 원문 대신 SHA-256, byte length,
absolute 여부만 기록한다. special node와 cross-filesystem traversal도 실패한다. Docker
volume은 Docker/API 및 application-aware snapshot 절차로 별도 inventory한다.

```bash
uv run python tools/cardrag_archive_inventory.py \
  --allow-root /home/lee/cardrag-archive \
  --root /home/lee/cardrag-archive > /secure/operator/cardrag-archive-inventory.json
```

기본은 stdout만 사용하므로 filesystem mutation이 없다. `--output`은 scan root 밖의
존재하지 않는 absolute file을 mode 0600으로 한 번만 생성하며 overwrite하지 않는다.
쓰기 오류 시 tool은 임의 경로를 삭제하지 않으므로 성공 receipt가 없는 output은
불완전한 것으로 격리하고 운영자가 exact identity를 확인해야 한다.

inventory manifest와 catalog에는 absolute path, UID/GID, mode, inode, timestamp 등
민감한 운영 metadata가 포함된다. 이를 public CI artifact, 공개 object storage 또는
공개 WebDAV에 업로드하지 않는다. 저장 위치는 운영자 전용 ACL로 제한하고, WebDAV
secondary archive도 별도 credential과 최소권한 ACL을 적용하며 manifest 열람 권한을
payload 쓰기 권한과 분리한다.
