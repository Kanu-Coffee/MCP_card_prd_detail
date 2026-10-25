# 019 OPERATIONS_REPORT_12 — FIX_33 운영 활성화 및 오늘 13시 예약 확인

2026-10-22 Executor. 사용자가 sudo 적용 스크립트를 실행한 후 12:42–12:43 KST 실제 운영 상태를 확인했다. FIX_33_REPORT 작성 시 남았던 관리자 권한 적용 단계가 완료됐다.

- /opt/cardrag/current → /opt/cardrag/019-fix33-7d2e39edbf59
- image cardrag-worker:019-fix33-7d2e39edbf59, ID sha256:71eb18e6538de2e0b00fd3c5173045263980322c623b17e3debee7d782d51450. deployment activation=active, activated_at=2026-10-22T03:42:15.945441+00:00.
- /etc/systemd/system/cardrag-worker.timer는 active release timer와 byte 일치. ActiveState=active, UnitFileState=enabled, Persistent=no.
- NextElapseUSecRealtime=Thu 2026-10-22 13:00:00 KST. 오늘부터 적용됐으며 다음 정기 실행은 매일13시다.
- Worker service ActiveState=inactive/SubState=dead/ExecMainStatus=0, 실행 중 Worker container 없음. 지금 배치를 시작하거나 회차를 소비하지 않았다.
- systemd ExecStart는 active release operations/worker-compose.sh run --rm --no-deps worker run을 사용한다. 예약 실행은 수동 status-issuer.sh가 참조하는 과거 named container와 구별해야 한다. 예약 모니터링은 journalctl -u cardrag-worker.service와 systemctl show를 사용한다.
- 최종 source 대조 81개 일치, 보호 recovery snapshot verifier verified=true/state_written=false/restore_performed=false 재확인. 운영 DB 회차 migration은 13시 실제 실행 때 자동으로 수행된다.

13시 이후 확인 명령:

```bash
journalctl -u cardrag-worker.service --since '2026-10-22 13:00:00' -n 100 -f --no-pager
```

```bash
systemctl show cardrag-worker.service -p ActiveState -p SubState -p Result -p ExecMainStatus -p ExecMainStartTimestamp -p ExecMainExitTimestamp
journalctl -u cardrag-worker.service --since '2026-10-22 13:00:00' -n 150 --no-pager
systemctl list-timers cardrag-worker.timer --all --no-pager
```

Ctrl+C는 journal 모니터링만 종료하고 Worker는 계속 실행한다. Type=oneshot이므로 실행 중 activating/start는 정상일 수 있으며, 종료 후 Result=success/ExecMainStatus=0와 최종 JSON succeeded/no_change 및 backup 결과를 함께 확인한다. 오늘13시는 우리2회차, 내일13시는 국민 배정이다. 현시점 실제13시 배치 결과는 아직 발생하지 않았으므로 성공을 선언하지 않는다.
