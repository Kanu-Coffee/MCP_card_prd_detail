# 019 OPERATIONS_REPORT_11 — FIX_30–32 운영 반영 및 12:00 예약 전환

2026-10-22 Executor. REVIEW_19 승인 후 FIX_30–32를 포함한 새 release/image를 활성화하고, timer를 매일 12:00 Asia/Seoul로 전환했다. 사용자가 sudo 명령을 실행했으며 실제 설치·next elapse를 재검증했다. 우리 run resume/DB/cursor 변경은 하지 않았다.

## Release/image

- active release: `/opt/cardrag/019-fix32-55c579b986cb`
- previous/rollback release: `/opt/cardrag/019-fix24-11d1321cc9e6`
- image: `cardrag-worker:019-fix32-55c579b986cb`
- image ID: `sha256:80bd67033c6ce0443f5e936315c79ed9e8d3e8ba5a48328ddc86ac2a1a84c65a`
- base image ID: `sha256:d08b61a6f3cd3809be3ed46aaa560f48bf632f4d987685b2640bfeb1ea339597`
- source tree SHA-256: `55c579b986cbf7bd93a650b1044ae08de2cafd3d14db39fabc4c753fd27eeaad`
- build input SHA-256: `15f50220dc3edd764d9f186deed3a9b9cf735f3206d2bb0b1460895e00157179`
- 설치 source 81개를 release snapshot과 대조했고 CLI help, resolved compose, OCR contract before/after 일치를 확인했다.
- 전체 검사는 `1853 passed, 11 warnings`, ruff check/format 및 `git diff --check` 통과다.

## Snapshot

- 새 보호 snapshot: `019-fix32-current-20261022`
  - worker-state: 7,404,785,664 bytes, SHA-256 `875893c656d803f98d3a5622e2dbf2f5bc883fc59f9dc92416d26741785d637e`
  - backup-ledger: 24,416,256 bytes, SHA-256 `d138d3eb55536bf41d3adf552bf6151a7245635a4d7e1a1f78ead7b2d6ea4427`
  - quick_check=ok, latest run=failed e81c17e69b5f428f8d6314ab2b8762d4, stable=신한 generation
- FIX_31/32 변경 전 snapshot `019-fix24-current-20261022` (7,428,280,320 bytes allocated)는 archive 후 제거했다.
- active/rollback recovery guidance를 새 snapshot으로 갱신하고 verifier 양쪽 통과를 확인했다.

## Timer 전환

- repo/active release timer: `OnCalendar=*-*-* 12:00:00 Asia/Seoul`, `Persistent=false`
- 사용자가 root로 timer를 설치·daemon-reload·`enable --now`했다.
- 실제 확인:

```text
/etc/systemd/system/cardrag-worker.timer == /opt/cardrag/current/deploy/worker/cardrag-worker.timer
ActiveState=active
UnitFileState=enabled
Persistent=no
NextElapseUSecRealtime=Thu 2026-10-22 12:00:00 KST
```

- service는 계속 active release의 `operations/worker-compose.sh`를 사용하며 image/volume helper 경로가 유지된다.
- 현재 Worker container는 0이다.

## 운영 상태

- failed run `e81c17e69b5f428f8d6314ab2b8762d4`는 failed 상태를 유지한다. cursor/day/result를 수정하지 않았다.
- stable serving은 `g-666c2d2ee9e24d04acf76eed-d6ffd9a927e9`, MCP current=Worker stable, source page 2건 조회 성공이다.
- 우리 PDF origin은 마지막 확인 시에도 HTTP200 `text/html` 서비스 오류를 반환했다. 12:00 예약 실행은 새 bounded retry 후 `issuer_origin_service_error`로 실패할 수 있으며, 성공을 미리 선언하지 않는다.
- 새 run, 실제 resume, backup flush, DB migration, 수동 cursor seed는 실행하지 않았다.

## 다음 확인

12:00 이후:

```bash
systemctl status cardrag-worker.service --no-pager
journalctl -u cardrag-worker.service --since '2026-10-22 12:00:00' --no-pager
/opt/cardrag/current/operations/status-issuer.sh woori
```

실행 결과가 실패면 `worker-failure.json`과 `issuer-collection.json`의 `issuer_origin_service_error`/origin status/MIME을 확인한다. origin이 회복되지 않은 상태에서 수동 반복·cursor reset으로 성공을 만들지 않는다. 019 자동운영 최종 인수는 실제 실행/백업 완료 후 판정한다.