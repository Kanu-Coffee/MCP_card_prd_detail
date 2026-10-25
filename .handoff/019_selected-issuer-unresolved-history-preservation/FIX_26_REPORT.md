# 019 FIX_26_REPORT — 과거 이미지·복구본·개발 잔여물 정리

2026-10-22 Executor. FIX_26 범위에 따라 CardRAG 잔여물을 조사하고 명시적 대상만 삭제했다. 활성/rollback release, 현재 DB·serving·backup 원장, Git/handoff는 보존했다. 타이머는 현재 KST10:00 예약 상태로 유지했고 Worker를 실행하지 않았다.

## 정리 전 상태

- 현재 release `/opt/cardrag/019-fix24-11d1321cc9e6`, rollback `/opt/cardrag/019-fix23-e9b9cdca1a22`.
- 최신 run `666c2d2ee9e24d04acf76eed58123a15` (shinhan, succeeded), stable `g-666c2d2ee9e24d04acf76eed-d6ffd9a927e9`.
- timer: active/enabled, `Persistent=no`, next `2026-10-22 10:00 KST`.
- 독립 snapshot 2개: `019-fix23-e9b9cdca1a22`, `019-fix24-11d1321cc9e6`.
- Docker before: images 57/46.59GB/reclaimable22.26GB, containers33, volumes59/168.8GB, build cache8.02GB.
- 파일시스템 가용 85,732,212,736 bytes (79% used).

## 삭제한 대상

### Snapshot

현재 DB를 SQLite online backup으로 새로 보존한 뒤 기존 snapshot 2개를 제거했다.

- 신규 보존: `/state/deployment-backups/019-fix24-current-20261022`
  - worker-state: 7,403,823,104 bytes, SHA-256 `28d64d2c9d16a79854c1f497fe40a1adb876ea0ca0af95469d20efff0184c8e2`
  - backup-ledger: 24,416,256 bytes, SHA-256 `d138d3eb55536bf41d3adf552bf6151a7245635a4d7e1a1f78ead7b2d6ea4427`
  - quick_check=ok, latest run=shinhan succeeded, stable `g-666c2d2ee9e24d04acf76eed-d6ffd9a927e9`
- 제거: `019-fix23-e9b9cdca1a22` (7,339,466,752 bytes), `019-fix24-11d1321cc9e6` (7,339,769,856 bytes)
- snapshot gross 제거 14,679,236,608 bytes, 신규 snapshot 포함 net 약 7,275,413,504 bytes 감소
- 기존 snapshot `snapshot.json`은 `operations/live/cleanup-20261022/snapshots/`에 보존
- active `operations/deployment.json`의 `snapshot_directory`/`state_backup`/`snapshot_retention`을 새 복구점으로 갱신

### Container

종료된 CardRAG 테스트 container 중 최신 per-issuer 8개는 증거로 유지하고, 오래된 중복 8개만 logs/inspect를 먼저 보존한 뒤 제거했다.

제거:

- `cardrag-eval-016-d8a9018`
- `cardrag-live-bc-20261020T052637Z`
- `cardrag-live-bc-20261020T072112Z`
- `cardrag-live-lotte-20261021T005408Z`
- `cardrag-live-lotte-resume-20261021T021643Z`
- `cardrag-live-lotte-resume-20261021T053259Z`
- `cardrag-live-woori-20261019T063550Z`
- `cardrag-live-woori-20261019T112845Z`

보존한 최신 8개: woori `...233215Z`, kb `...011943Z`, shinhan `...134616Z`, samsung `...035809Z`, hyundai `...111518Z`, hana `...101645Z`, lotte `...104220Z`, bc `...083321Z`. logs/inspect는 `operations/live/cleanup-20261022/containers/`에 gzip 보존했다.

### Image

참조 container가 없는 CardRAG 이미지만 명시적으로 제거했다.

- `cardrag-control:015`
- `cardrag-mcp:015`
- `cardrag-web:015`
- `cardrag-worker:015-741cfe0`
- `cardrag-worker:016-afaef39`
- `cardrag-worker:016-bb56d20`
- `cardrag-worker:016-d8a9018`
- `cardrag-worker:017-fca5ef313a0b`
- `cardrag-worker:019-4dba44664659`
- `cardrag-worker:019-ba273106158e`
- `cardrag-worker:019-fix19-5d0b40a24a6f`
- `cardrag-worker:019-fix21-afdcabf41ea2`
- GHCR candidate worker `285326379739...`
- GHCR/ymtop candidate MCP `371895b40861...`

보존 image: active `019-fix24`, rollback `019-fix23`, active MCP `013-50a0129`, active web `016-current`. `019-e7fb8a0ba60c`와 `019-fix16-4108e3dac5f5`는 최신 per-issuer container가 참조하므로 남겼다.

### Release directory

`operations/deployment.json` 등 작은 metadata를 `cleanup-20261022/releases/`에 보존한 뒤 다음 7개를 제거했다.

- `017-fca5ef313a0b`
- `019-4dba44664659`
- `019-ba273106158e`
- `019-e7fb8a0ba60c`
- `019-fix16-4108e3dac5f5`
- `019-fix19-5d0b40a24a6f`
- `019-fix21-afdcabf41ea2`

실제 release directory 회수량 합은 554,016,768 bytes였다. `019-fix24`/`019-fix23`, `013-50a0129`, `015-f8453d0`, `016-evidence`, `017-evidence`, `v1.0.34`는 보존했다.

### Build cache

진행 중 build가 없음을 확인한 뒤 `docker builder prune --all --force`로 8.02GB를 회수했다.

## 결과

- 파일시스템 가용: **85,732,212,736 → 110,419,898,368 bytes** (+24,687,685,632 bytes, 약 22.99GiB)
- 사용률: 79% → 73%
- Docker: images 57→43 (46.59→35.54GB), containers 33→25, build cache 8.02GB→0
- volumes: 59→59, 168.8→161.5GB (snapshot gross 제거와 신규 snapshot 순효과)
- 현재 snapshot은 1개: `019-fix24-current-20261022`

## 검증

- snapshot: SHA/크기 일치, `PRAGMA quick_check=ok`, latest run succeeded
- latest run/stable: `666c2d2ee9e24d04acf76eed58123a15` / `g-666c2d2ee9e24d04acf76eed-d6ffd9a927e9`
- MCP: `test-live-mcp.sh` 2개 source page 성공, live generation=Worker stable
- backup: scoped read-only `in_scope_remaining/lost_source/conflicts/missing/unbound=0`; out-of-scope pending 3/30,862 bytes 유지
- timer: active/enabled, Persistent=no, next `2026-10-22 10:00 KST`; Worker container 0
- active/rollback release와 image ID 유지

## 남긴 대상과 이유

- `cardrag-worker-v122-state`, `cardrag-worker-v130-candidate-state`, `cardrag-worker-v120-recovery-auth-20260910`은 각각 4–8KB의 빈 volume이며 container 참조가 없다. 그러나 `/etc/cardrag/worker.env`의 legacy state/auth 기본값과 compose label에 연결돼 있어 이름만으로 삭제하지 않았다. 공간 회수 효과도 없다.
- `019-e7fb8a0ba60c`, `019-fix16-4108e3dac5f5` image는 최신 per-issuer status/log 증거 container가 참조하므로 유지했다. release directory는 제거했다.
- `cardrag-worker-state`, `cardrag-worker-auth`, `cardrag-worker-paddleocr-models`, `cardrag-serving`, `cardrag-mcp-state`와 다른 서비스 volume은 건드리지 않았다.
- Git 작업 트리/미커밋 파일과 `.handoff` 전체는 보존했다.
- `lotte.before-fix24-resume.json` 등 과거 attempt metadata는 archive이며, 현재 `lotte.json`은 보존한 최신 container를 가리킨다.

정리 중 전체 `docker system prune -a`, volume prune, DB 수정, Worker 실행, backup flush, 예약 변경은 하지 않았다.