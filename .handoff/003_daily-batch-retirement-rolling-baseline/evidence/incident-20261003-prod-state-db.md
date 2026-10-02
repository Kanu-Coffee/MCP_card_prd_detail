# 2026-10-03 프로덕션 Worker 상태 DB 손실 사고 (Executor 기록)

## 결과
- 수동 prod 런 `d9de4b240e804ae5a0e28a1c97cb9a31`(블록 #3, patch7, codex-exec/qwen3.8-flash)
  15:05→03:02 실패 종료. discovery 8/8(hana 724 warnings=0), PDF 5,066, OCR 5,500/5,500 failed=0
  후 **embed_views 읽기에서 `sqlite3.OperationalError: disk I/O error`**.
- `cardrag-worker-v129-state` 의 `worker-state.sqlite3`(6,892,044,288 B)은 **페이지 1 마법이
  소실되어**(`SQLite format 3\0` 자리에 `0a00000008008000…`) 열리지 않는다(`file is not a database`).
  `-wal`/`-shm` 없음, 샘플링한 페이지의 약 94%가 전체 0 → 복구보다 재구성 경로가 맞다.
- 문서/파일 자산(runs 37G, pdf-cache 4.2G, ocr-seed 121M, audit-reports 7.2M)과
  **MCP 서빙(`{"ready":true}`, stable 세대 `g-5f74295db4b64f10837eba3e-a102228926a6`)은 무영향**.

## 원인 (주문형 증거)
- `journalctl`(`incident-20261003-timer-journal.txt`): 03:00:00 타이머 컨테이너가 기동해
  capacity/OCR/embedding preflight(30 s)를 마친 뒤 03:00:32 `worker_busy`로 종료.
- 코드리딩: `apps/cardrag-worker/src/cardrag_worker/cli.py` 의 `_run()` preflight가
  `WorkerState(settings.state_database)`를 **워커 락 획득보다 먼저** 열고, 락은
  `pipeline.py:2369 _run_locked`에서 뒤늦게 잡는다. 즉 락을 못한 두 번째 프로세스가
  살아 있는 WAL DB를 열었다 닫았고, 86 s 뒤 실제 writer가 읽기에서 I/O 오류로 사망.
- Executor의 이전 판단("락 거부 충돌은 무해")는 **오류**였다.

## 보존·복구 조치
- 손상 DB를 읽기 전용 원본으로 보존하고 격리 볼륨으로 복제:
  volume `cardrag-v129-corrupt-state-20261003` / `worker-state.sqlite3.corrupt-20261003T0302`
  **SHA-256 `199b9e1ad1616d5cb578ec64ada859a19b9762c1c4c965b7be7022b988ccf051`**. 원본 볼륨 무변경.
- 복구 경로(O1, 사용자 승인): 검증 완료된 후보 상태 볼륨 `cardrag-worker-v130-candidate-state`
  (DB `quick_check ok`, run `c622d3c4…=succeeded`, publish `ready`
  `g-c622d3c4b1fb4df5a74a4b13-e95cb9ce7d7f`, embedding 396,213 행, OCR/PDF 캐시 완비)를
  정본 상태로 **승격**하고 stable 채널에서 배치 1회 완주(`cardrag-prod-promote-1`).
  - 시드 커맨드 미사용 사유: `seed-state-v122`/`seed-embedding-cache-v122`는 대상 채널이
    `candidate-v1.0.11`일 때만 동작(`cli.py:946/992`), 46 GB 사본은 잔여 디스크에서
    `peak_growth_bytes≈71 GB` 프리플라이트를 실패시킴.
  - 승격 런은 원격 GC만 임시 우회(`CARDRAG_REMOTE_GC_APPROVED=false`,
    `CARDRAG_COLLECT_REMOTE_GARBAGE=false`) — 서빙 중인 9/29 stable 세대를 삭제하지 않기 위해.

## 재발 방지 (커밋 `b252aa2`)
- `cli.py`: SQLite 열기 직전 `worker_lock(settings.lock_file)` 프로브 → 락 거부 프로세스는
  DB를 열지 않고 종료. 권위 있는 획득은 파이프라인에 그대로 남아 승자 동작 불변.
- 회귀 테스트 `test_run_probes_worker_lock_before_opening_state_database`.
- 이 라운드가 남긴 ruff check/format 위반 10개 파일 정리(CI 런타임 게이트 통과).
