# 019 운영 반영 — 수동 실테스트 대기

2026-10-19 Codex. 사용자가 검토 완료한 019를 운영에 반영하고 기존 `/opt/cardrag/current/operations/run-issuer.sh woori` 방식으로 실테스트할 수 있게 준비하도록 요청했다.

## 반영 결과

- `/opt/cardrag/current`를 `/opt/cardrag/019-4dba44664659`로 전환했다.
- 이미지: `cardrag-worker:019-4dba44664659`.
- 이미지 ID: `sha256:d29c441fc4d37fcf250cb9d73ebc4b28a0bdf5608380d03f23354afa1be37dcf`.
- worker/core 소스 digest: `4dba4466465948c04fb07491b2f0191d69ab43665b77ad3981a37a6a80ec6a6b`.
- Git HEAD `aaeb2c4` 위의 검토된 worktree snapshot을 고정했다. 임의 commit/push는 하지 않았다.
- 기존 운영 017 image/runtime을 기반으로 worker/core 소스만 교체하는 offline Docker build를 수행했다. `uv.lock` 및 두 패키지 dependency 선언이 이전 release와 동일함을 확인했다. 설치된 소스 81개를 다시 해시해 위 digest와 대조했다.
- 실제 환경변수·마운트·네트워크·실행 사용자·entrypoint/command는 이전 수동 실행과 동일하고 image만 갱신했다. external 볼륨 `cardrag-worker-state`, `cardrag-worker-auth`, `cardrag-worker-paddleocr-models`, `cardrag-serving`을 유지했다.
- 비공개 runtime overlay는 host에만 보존했다. 프록시 주소/인증/키를 Git 또는 이 보고서에 기록하지 않았다.
- Worker service와 timer는 모두 inactive이며 실제 카드사 배치는 시작하지 않았다. 사용자가 아래 명령으로 시작한다.

## 복구점과 사전 검증

- 이전 release `/opt/cardrag/017-fca5ef313a0b` 및 image를 보존하고 activation helper의 previous-release 기록을 갱신했다.
- Worker lock 아래 SQLite online backup을 만들었다. 위치는 `cardrag-worker-state:/deployment-backups/019-4dba44664659`다.
- `worker-state.sqlite3` 7,288,918,016 bytes, `backup-ledger.sqlite3` 24,416,256 bytes를 저장했고 두 복구본의 `PRAGMA quick_check=ok` 및 SHA256을 확인했다. metadata는 같은 디렉터리 `backup.json`과 host `operations/backup-verified.log`에 있다.
- serving stable pointer/manifest/READY도 복구점에 보존했다. 초기 metadata 복사에서 READY 파일명을 소문자로 잘못 조회한 오류가 있었으나 DB 복사 자체는 완료됐다. 기존 복구본을 다시 검증하고 실제 `generation_ready_path`의 `READY.json`을 복사해 metadata 작성을 완료했다.
- 실제 serving은 기존 `g-f1871a51eff549ea9aee95b7-76c89918e569`, baseline run은 `f1871a51eff549ea9aee95b73eaad99d`다. 마지막 run `6266b015df8c496586fe6a55362edd09`는 이전 실패 run이며 baseline으로 혼동하지 않았다.
- 8사 5,536문서 유지: hana735/samsung766/lotte647/woori979/shinhan931/hyundai605/kb751/bc122.
- 새 image에서 네트워크 없이 SQLite mode=ro로 baseline ReuseSource 및 5,558개 known source를 읽고 unresolved 22건의 schema/count/hash와 정비 계획을 검사했다. WorkerState 초기화/migration 또는 apply를 하지 않았다.
- offline 계획 대상은 collision17/metadata부재4/cache객체부재1이며 카드사 분포는 현대17/하나3/우리1/BC1이다. 이 계획은 실제 현재 수집/최종 처리의 처분 결과를 대신하지 않는다. 실제 실행 inventory를 확인해야 한다.
- Compose config, 이미지 CLI `--issuer`/`--round-robin`, helpers shellcheck, monitor 구문/기존 실패 run 조회 smoke를 통과했다. monitor의 기존 실패 run 검증에서 비선택 문서 변경0/비선택 stage0/해당 run 백업 미등록을 정확히 표시했다.
- 새 release의 live 결과 디렉터리는 비어 있고 summary는 8사 모두 NOT_FINISHED다. 이전 실패/로그는 이전 release에 그대로 남아 있다.

## 사용자 복붙 명령

```bash
/opt/cardrag/current/operations/run-issuer.sh woori
/opt/cardrag/current/operations/logs-issuer.sh woori
/opt/cardrag/current/operations/status-issuer.sh woori
```

run helper는 detached container를 시작하고 종료하므로 SSH가 끊겨도 Worker가 계속된다. 로그 follow에서 Ctrl+C는 로그 보기만 종료한다. 별도 터미널에서 status를 실행해 진행/종료 결과를 볼 수도 있다.

```bash
/opt/cardrag/current/operations/summary-issuers.sh
```

상태 도구에 019 remediation inventory, unresolved ledger/report, 비선택 stage 기록 및 해당 run 백업 등록/receipt 관측치를 추가했다. 기존 비선택 문서 비교와 신규 문서/OCR 예시도 유지했다. receipt 관측은 원격 재검증이 아니며 pending이 없다는 사실만으로 백업 성공을 선언하지 않는다. 정상 종료·선택 카드사 수집 결과·비선택 미호출/서빙 불변·8사 보존·정비 근거·degraded/unsupported·이번 run 백업 등록 및 flush 완료를 함께 확인한다.

## 자동 예약과 남은 운영 검증

이번 반영은 기존 수동 helper 경로를 새 image로 전환한 것이다. `/etc/cardrag/worker.env`는 쓰기 권한이 없는 관리자 소유이며 기존 systemd ExecStart는 이 파일과 별도 compose overlay 설정을 사용한다. 자동 timer 경로를 새 수동 runtime으로 전환한 것으로 보고하지 않는다. timer는 inactive로 유지했고, 8사 수동 검증 뒤 자동 실행 설정/이미지/overlay와 일정은 별도로 맞춰야 한다. 지금 timer를 단순 enable하지 않는다.

사용자가 우리 실행을 시작한 뒤 신규 run/정비/백업 결과를 검토하고 나머지 7사를 한 번에 하나씩 실행한다. 실제 8사 정상 완료, 새로운 PDF/OCR 결과와 백업 완료는 아직 미검증이다. 이번 준비 과정에서 실제 카드사 origin/provider 추론을 호출하거나 serving pointer/과거 immutable seal/운영 데이터 SQL을 수작업 수정하지 않았다.
