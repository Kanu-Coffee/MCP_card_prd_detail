# 019 FIX_06–11 운영 반영 — 수동 테스트 준비 완료

2026-10-20 Codex. 사용자의 운영 반영·복붙 테스트 명령 요청에 따라 REVIEW_02 통과 작업트리를 새 release/image로 고정하고 활성화했다. 기존 보고서와 018 종료 상태는 보존한다.

## 반영 결과

- 활성 경로: `/opt/cardrag/current` → `/opt/cardrag/019-ba273106158e`.
- 이미지: `cardrag-worker:019-ba273106158e`.
- 이미지 ID: `sha256:29d34c19b0c4288f56756c31560cb50a3491e598769d2e715256833a9c57ba0a`.
- worker/core Python 소스 80개 SHA256: `ba273106158ebc21b0ec71b0c8953bd732939634d6f361f872fb72c145ddf128`.
- digest 정의는 정렬된 상대 경로+NUL+원문+NUL이다. unchanged `py.typed`와 dependency 선언은 별도 보존했다. 이전 배포의 파일 목록은 typing marker를 포함한 81개이므로 집계 범위를 혼동하지 않는다.
- HEAD `aaeb2c4` 위 검토된 미커밋 소스를 고정했으며 임의 commit/push는 하지 않았다. 이미지 내부 80개 파일과 최종 작업트리의 digest를 대조했다.
- 이전 `cardrag-worker:019-4dba44664659`의 runtime/dependency를 그대로 사용한 offline source overlay build다. root/worker/core pyproject 및 uv.lock 동일성을 확인했다.
- 실제 compose config는 image pin과 release build context 외에 동일하다. 기존 state/auth/model/serving external 볼륨, 비공개 overlay, 환경·네트워크·사용자·entrypoint를 유지했다. 비공개 설정은 저장소/보고서에 복사하지 않았다.
- timer와 Worker service는 inactive이며 실행 중인 Worker 컨테이너도 없다. 실제 배치·원격 flush·provider 추론은 시작하지 않았다. 자동 예약 경로 전환/재개는 이번 작업에 포함하지 않았다.

## 복구점과 serving 보존

worker.lock 및 backup.lock 아래 SQLite mode=ro source의 online backup을 생성했다. 복구점은 `cardrag-worker-state:/deployment-backups/019-ba273106158e`다. source 업무 SQL을 수작업 변경하지 않았다.

| 파일 | 크기(bytes) | SHA256 | quick_check |
|---|---:|---|---|
| worker-state.sqlite3 | 7291777024 | 0b32a0e6337ad20ae3545caecdc197a46b86db7e2ab9e7b6d862d1e3c8aa851d | ok |
| backup-ledger.sqlite3 | 24416256 | 14dd534737d092391917583dcc391f8806af8fa7f79bb9fc6223b34d31a1e2f2 | ok |

현재 stable pointer/manifest/READY도 같은 복구점에 보존하고 manifest hash를 검증했다. 이는 배포 복구용 로컬 사본이며 원격 backup threshold 정책을 바꾼 것이 아니다.

serving generation은 `g-41ce678e665b47179f8bfb2d-e884484ab66b` 그대로다. 8사 5537문서(우리980/하나735/삼성766/롯데647/신한931/현대605/KB751/BC122)를 읽기 전용으로 확인했다. 이전 release와 live 증거는 그대로 보존했다.

## 운영 도구 호환성 및 검증

- 기존 physical path 처리, live directory mode2750/cardrag group 상속과 uid10001 읽기 권한을 유지·검증했다.
- `status-issuer.sh`의 실행 결과에 새 backup scope 필드를 보존하고 종료 시점 snapshot임을 표시했다. summary에도 scope_complete를 추가했다. 현재 백업 상태는 별도 `backup status --run`으로 조회한다.
- 기존 monitor는 `BackupLedger.scope_remote_paths`를 static 방식으로 호출하고 있었다. 새 인스턴스 메서드와 맞게 mode=ro 연결을 사용하는 host helper로 보완했다. 제품 코드는 변경하지 않았다.
- monitor에 elapsed/누적 구간/export 내부 계측/cache 조회/RSS·IO를 추가해 속도 재시험 결과를 조회할 수 있게 했다.
- 새 monitor를 실제 이전 우리 run에 읽기 전용으로 적용한 smoke: succeeded, 비선택 문서 변경0, 비선택 stage0, 기존 elapsed5397.323초를 정상 조회했다. 이 수치는 이전 run의 관측이며 새 버전 성능 측정이 아니다.
- compose config 검증, 모든 host shell helper의 shellcheck, monitor compile, 이미지 CLI `run --help`/`backup flush --help`/`backup status --help`, 실제 `backup status --run` 진입을 확인했다. 새 live 디렉터리는 아직 실행하지 않아 8사 모두 NOT_FINISHED다.
- 제품 소스는 REVIEW_02의 1447 tests/ruff 통과본과 일치한다. 이번 host helper 변경에 전체 제품 테스트를 불필요하게 반복하지 않았다.

## 실환경 읽기 전용 백업 사전 점검 — 별도 receipt 충돌 발견

대상 run `41ce678e665b47179f8bfb2d82273882`, selected=woori, backup_mode=immediate.

- 기존 보류 metadata 2건(1791+285=2076bytes)은 새 scope에 포함되며 pending SHA/size와 expected가 모두 일치한다. unbound0, lost_source0, 범위 밖 pending0이다.
- **별도로 기존 native cache 한 쌍의 manifest/READY receipt digest 충돌 2건이 탐지됐다.** 해당 key는 `94cdb144c916a6cae490f6cae6fe89139bf6dfc3f7ec32643149261efa73352f`이며 기존 보류 2건의 key와 다르다. 현재 run-local manifest에서 유도한 SHA와 같은 canonical root의 저장 receipt SHA가 다르다. 이 충돌 두 경로는 missing으로도 집계된다.
- 충돌 원인을 실제 원격 객체와 대조한 것은 아니며, remote 손상/새 코드 버그로 단정하지 않는다. 이번에는 원격 GET/PUT 또는 receipt 수정으로 해소하지 않았다. 단순 pending0이나 exit0으로 최종 완료 판정하지 않는다.
- 실제 새 CLI 출력: backup_in_scope_remaining2/conflicts2/missing2/unbound0, scope_complete=null(read_only). flush는 별도 전송 테스트이고 이때 기존 충돌 때문에 선택 전체 complete=False가 남을 수 있다. 충돌 경로를 강제 overwrite하거나 원장 행을 삭제하지 않는다.
- 증거: 새 release `operations/preflight-scoped-backup.json`, `scoped-integrity-detail.json`, `backup-status-pretest.json`. private URL/인증값은 이 자료에 포함하지 않았다.

## 복붙 테스트 명령

먼저 이전 run의 백업만 재시험한다. 수집/OCR/embedding을 반복하지 않는다.

```bash
OPS=/opt/cardrag/current/operations
RUN_ID=41ce678e665b47179f8bfb2d82273882
"$OPS/worker-compose.sh" run --rm --no-deps worker backup status --run "$RUN_ID" --issuer woori
"$OPS/worker-compose.sh" run --rm --no-deps worker backup flush --run "$RUN_ID" --issuer woori
"$OPS/worker-compose.sh" run --rm --no-deps worker backup status --run "$RUN_ID" --issuer woori
```

flush의 실제 전송·receipt/index 결과와 scope conflict/complete를 함께 확인한다. status는 읽기 전용 진단이므로 원격 index 확인 없이 complete=null을 유지할 수 있다. 정상 정책상 pending과 검증 충돌을 구분한다.

우리카드 단독 배치 재시험은 위 백업 테스트와 동시에 실행하지 않는다.

```bash
/opt/cardrag/current/operations/run-issuer.sh woori
/opt/cardrag/current/operations/logs-issuer.sh woori
/opt/cardrag/current/operations/status-issuer.sh woori
/opt/cardrag/current/operations/summary-issuers.sh
```

run은 detached이므로 SSH가 끊겨도 계속된다. 로그의 Ctrl+C는 보기만 종료한다. 현재 전체5537/우리980 및 비선택 문서/stage 보존, 새 scope 진단과 performance를 대조한다. export 전체 속도 개선율은 아직 실측하지 않았으며 단일 카드사 실행이 전체의1/8이 된다고 보장하지 않는다.

같은 명령 안내를 host `operations/TEST_COMMANDS.md`에도 저장했다. 실제 pending 회수/충돌 원인과 복구/새 배치 성능/나머지7사 검증/8사 평가 후 timer 재개는 019 후속 운영 항목으로 남는다.
