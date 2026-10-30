# 020 운영 적용 재개 — 작업 위치 오류 및 링크 정리

2026-10-28 06시대 KST. 사용자 sudo 실행 중 `apply-020-paddle-20261028.sh`가 snapshot 완료 후 종료했다. `runuser -u cardrag`가 사용자의 `/home/lee` 작업 위치를 그대로 상속했고, cardrag 계정은 `/home/lee`를 탐색할 수 없어 Docker Compose가 `stat .: permission denied`로 실패했다. 이전 서비스 계정 config gate는 release 디렉터리로 `cd`했으나 실제 복구 호출에는 그 처리가 누락됐다. Executor가 작성한 스크립트의 오류다.

## 완료된 부분

- 10:00 timer는 06:07에 disabled/inactive로 중지됐다.
- Worker·backup 두 lock을 유지한 상태에서 `/state/deployment-backups/020-paddle-20261028` 두 DB online snapshot을 생성했다. 독립 recovery verifier가 양쪽 `quick_check=ok`, 크기·SHA 및 stable generation을 확인했다. 증거: 후보 release의 `operations/020-recovery-verified.json`, `020-snapshot-evidence.json`, `020-source-db-evidence.json`.
- worker DB 7,418,986,496 bytes SHA-256 `ff477f58faefa1dedb495e826361d3cb3286ef47072b691969ee8c278b2fab2c`; backup ledger 26,001,408 bytes SHA-256 `3cbb9dd44f7773e1d584b5dfe09bd92c83be2c0a872328dc8905c9fe14f5d6ef`. 당시 stable generation `g-75e5a7f5c75e44b581bdf81d-6b81ea507cc4`.
- active release는 lock을 보유한 상태에서 `/opt/cardrag/020-paddle-20261028`로 전환됐다. Worker batch는 기동되지 않았다. 기존 MCP readiness는 `ready=true`였다.
- 사용자의 운영 적용 지시에 따라 timer disabled와 service/backup 비실행을 확인하고 양쪽 manual-start lock을 획득했다. 후보 이미지 recovery dry-run에서 10월 27일 run의 예상 `.paddle-input-2788.pdf` symlink 1건을 확인한 뒤 `--apply`로 그 항목만 삭제했다. 기존 Worker lock은 recovery 도구가 획득했다.
- 정리 후 동일 run의 재조회는 0건이며, 대상 CAS PDF SHA `4e968eb9f8ecf90b7a18c806e15967971275007332cbdf3627ce25eec0106bed`와 크기 5,304,856 bytes가 정리 전후 일치했다. 증거는 `operations/live/020-resume-alias-{before,applied,after}.json`, `020-cas-before.json`, `020-resume-cas-after.json`이다.
- 후보 이미지로 읽기 전용 state 전체 기동 용량 검사를 통과했다(여유 `84279746560` bytes, 최소 `2147483648` bytes). 이어 후보 Compose의 실제 startup-check는 `status=ready`, `quality_status=passed`, 약 11.4초였으며 Codex Terra 및 Paddle fallback 설정을 확인했다. 이는 실제 배치·게시 성공을 뜻하지 않는다. 증거: `operations/live/020-pre-finish-startup-check.json`.

## 남은 sudo 단계

기존 스크립트를 다시 실행하지 않는다. active release와 snapshot이 이미 전환됐으므로 이를 전제로 하는 다음 일회성 마무리 스크립트를 준비했다.

```bash
sudo /opt/cardrag/020-paddle-20261028/operations/finish-020-paddle-20261028.sh
```

SHA-256 `1664e8577f31749c3fd0b23bbbedb2b88090c4385b1539b8cd19f82008a57932`; `shellcheck` 및 `bash -n` 통과. 이 스크립트는 active release/image, disabled timer, Worker/backup 상태, snapshot과 링크 정리 증거, 수동 lock을 검증한다. 이후 명시적으로 release 디렉터리로 이동해 cardrag 계정의 recovery dry-run과 startup-check를 다시 수행하고, stable pointer가 snapshot 시점과 동일한지 확인한다. 10월 28일 10:00 KST 전인 경우에만 timer를 enable하고 단일 10:00/Persistent=no 예약을 검증한다. 실패 시 timer는 disabled로 유지한다.

현재 세션의 `sudo -n true`는 여전히 인증을 요구하므로 root 단계는 사용자 터미널 실행이 필요하다. 명령 성공 후 실제 timer, service, active image, startup-check, MCP 상태를 독립 확인해야 한다. 10:00 배치의 terminal·게시·backup은 별도 라이브 검증으로 남는다.
