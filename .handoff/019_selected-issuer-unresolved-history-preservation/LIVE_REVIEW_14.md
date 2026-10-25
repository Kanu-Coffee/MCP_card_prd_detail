# 019 LIVE_REVIEW_14 — 13시 예약은 발화했으나 서비스 계정 권한 오류로 배치 미시작

2026-10-22 14:09–14:12 KST 확인. 사용자 요청: 13시 정상 처리 여부 점검.

## 결론과 증거

정상 처리되지 않았다. timer LastTriggerUSec=2026-10-22 13:00:00 KST, active/enabled 유지, next=2026-10-23 13:00:00 KST다. service Result=exit-code, ActiveState=failed다. journal에는 worker-compose.sh Permission denied, Failed at step EXEC, status=203/EXEC가 기록됐다. 이는 ExecStartPre 스크립트 실행 자체의 실패이며 Worker 런타임/원본/PDF/OCR/백업 작업은 시작되지 않았다. ExecMainStatus=0만으로 성공 판정하면 안 된다.

활성 release는 019-fix33-7d2e39edbf59다. operations 디렉터리는 2750 lee:lee, worker-compose.sh는 0750 lee:lee였으나 systemd User=cardrag(uid/gid10001, docker그룹)이다. 이전 fix32 operations는 lee:cardrag였다. release 복제 단계에서 service group 보존 및 실제 서비스 계정 접근 검증을 놓쳤다. OPERATIONS_REPORT_12의 timer 활성 확인은 사실이지만 서비스 계정 실행 가능성을 확인하지 못한 검증 공백이었다.

## 즉시 복구

작업자 lee는 cardrag 그룹 구성원이므로 sudo 없이 새 active release operations 하위 소유 그룹만 재귀적으로 cardrag로 복원했다. chmod로 공개 권한을 늘리지 않았다. 새 이미지/DB 수정/회차 변경/배치 시작은 하지 않았다.

UID10001/GID10001/docker supplementary group 컨테이너에서 host active release와 /etc/cardrag/worker.env를 읽기전용 mount하여 operations 탐색, helper 실행 접근, private compose 및 env/compose 읽기 접근을 확인했다. 모두 true다. helper compose config --quiet도 통과했다. 실제 systemd 재실행은 아직 안 했으므로 end-to-end 성공을 선언하지 않는다.

현재 실행 중 Worker 없음. 운영 DB는 readonly immutable 연결로 확인했다(실행 중 writer 없음). 최신 run e81c17e69b5f428f8d6314ab2b8762d4 failed, 오늘 day는 우리·기존 결과 그대로, issuer_rotation_attempt 테이블 없음. 즉13시 실패는 새 회차를 소비하지 않았고 애플리케이션 이행도 실행되지 않았다. 다음 실제 자동모드 호출이 우리2회차다. 내일 예약은13시 국민이며 Persistent=false이므로 오늘 누락을 자동 catch-up하지 않는다.

sudo -n은 interactive authentication required로 거부됐다. 이번 명령은 systemd Worker 재실행이므로 사용자에게 아래 복붙 명령을 제공한다. 비동기 시작 뒤 journal로 확인해야 한다.

```bash
sudo systemctl --no-block start cardrag-worker.service
journalctl -u cardrag-worker.service --since '2026-10-22 14:00:00' -n 100 -f --no-pager
```

## 재발 방지

별도 FIX_34에 release 복제 시 group/보안 mode 보존과 실제 service identity의 config-only 접근/실행 검증을 활성화 전 필수 gate로 남긴다. 전체 Worker 테스트 통과나 lee 계정 config 검증은 이 gate의 대체가 아니다. 원본 서비스 오류와 이번203/EXEC를 혼동하지 않는다.
