# 019 OPERATIONS_REPORT_13 — 매일14:30 예약 전환 완료

2026-10-22 14:15 KST. FIX_34_REPORT의 실제계정 접근 gate 검증·사용자 sudo 적용 후 다음 상태를 확인했다.

```text
active release=/opt/cardrag/019-fix33-7d2e39edbf59
ActiveState=active
UnitFileState=enabled
Persistent=no
NextElapseUSecRealtime=Thu 2026-10-22 14:30:00 KST
```

repo/active/installed timer는14:30이며 이후도 매일14:30이다. 새 Worker 이미지는 만들지 않았고 실서비스 계정 gate를 install-systemd/activation 절차에 반영했다. 현재 service는 inactive/dead, container 없음. 오늘13시 권한 실패는 새 회차를 소비하지 않아 오늘14:30 우리2회차, 내일14:30 국민이 예정된다. 실제 배치 결과/백업 성공은 아직 확인 전이다.

```bash
journalctl -u cardrag-worker.service --since '2026-10-22 14:30:00' -n 100 -f --no-pager
```

```bash
systemctl show cardrag-worker.service -p ActiveState -p SubState -p Result -p ExecMainStatus
systemctl list-timers cardrag-worker.timer --all --no-pager
```

Ctrl+C는 journal 보기만 종료한다. 자동 실행은 수동 named container와 다르므로 과거 status-issuer 결과를 이번 실행 결과로 보지 않는다. Type=oneshot의 activating/start는 실행 중 정상 상태일 수 있고 종료 후 Result/exit/최종JSON/백업 결과를 함께 확인한다.
