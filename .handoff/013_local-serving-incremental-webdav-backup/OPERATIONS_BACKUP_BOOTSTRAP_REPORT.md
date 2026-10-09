# 초기 백업 관리대장 수동 구축 — 기동 보고서

2026-10-09 Executor / Codex. 사용자 요청에 따라 기존 기록 승계 없이 현행 확인 절차로 대기분 전체를 처리하도록 수동 백업을 기동했다.

- 컨테이너 `cardrag-backup-bootstrap`, Worker 이미지 `cardrag-worker:013-50a0129` 재사용. 별도 이미지 빌드 없음. 23:14경 기동, 단발 실행, restart=no, 종료 후 컨테이너 유지.
- `operations/manual-backup-drain.py`에서 기존 BackupLedger.flush를 직접 호출. CLI backup flush는 timeout을 전달할 옵션 없이 기본300초를 사용하므로 전용 host script를 사용했다. 전체 Worker/OCR/embedding은 실행하지 않는다.
- 전체 시간 예산7200초. 정상적으로 pending/lost_source가 모두0이면 즉시 완료. 개별 파일30초 검증 제한과 index commit/receipt 절차는 유지한다. 부분 실패 후60초 대기하여 잔여 시간 내 재시도하며,3회 연속 commit 진행이 없으면 실패 종료한다. 시간 만료 시 exit2, 소스 분실/진행 불가 시 exit1, 전체 완료 시 exit0.
- 시작 pending12977개/1289084865bytes, OCR5065항목, lost_source0. HEAD/GET200으로 실제 초기 처리 확인. 이미 원격에 있는 파일의 확인 결과를 관리대장에 등록한다. pending 감소는 index commit 이후이므로 실행 중 목록 수가 바로 줄지 않을 수 있다.
- 기존 background `cardrag-backup`은 정지 유지. Worker는 종료 상태, MCPready=true. 새벽03시 timer는 다음2026-10-10 03:00 KST active. 이번 수동 작업 시간한도는 그보다 앞선 약01:15에 끝나도록 설정했다(마무리 처리 소폭 초과 가능).
- 정기 Worker의 immediate/inline300초 설정은 변경하지 않았다. 수동 작업 완료 후 pending0 상태에서 다음 배치의 신규/변경 여부, backup requests/상태, receipts 재사용 여부를 비교해 의도한 증분 동작을 검증한다. 신규 물량/연결에 따라5분 충분 여부는 달라지므로 무조건 완료를 보장하지 않는다.
- 기동/초기 진행까지만 확인하고 장시간 감시는 종료한다. 사용자 완료 통보 후 원격 index commit, pending/lost0, 관리대장 상태를 확인한다. 이번 보고서는 전체 백업 완료 보고서가 아니다.

## 모니터링

```sh
docker logs -f cardrag-backup-bootstrap
```

HTTP 상세 로그가 많으면 주요 이벤트만 확인:

```sh
docker logs -f cardrag-backup-bootstrap 2>&1 | rg --line-buffered 'backup_drain_|backup_flush_|WARNING|ERROR'
```

최종 성공 기준: `backup_drain_completed` / status ready / pending_count0 / lost_count0 / container exit0. `backup_flush_finished`의 일부 commit 성공만으로 전량 완료라 판단하지 않는다.

```sh
docker inspect cardrag-backup-bootstrap --format '{{.State.Status}} exit={{.State.ExitCode}}'
```

근거: `evidence/manual-backup-bootstrap-start.json`. 운영 코드와 기존 handoff는 변경하지 않았다.
