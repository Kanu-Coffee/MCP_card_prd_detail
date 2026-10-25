# 019 FIX_33 REPORT — CLI 단일 종료 출력과 일일 3회 실행 정책

2026-10-22 Executor. FIX_33과 사용자 변경사항 FIX_33_AMENDMENT_01을 구현했다. 작성 시점 새 release는 검증·준비 완료이며 실제 활성화와 systemd 변경은 사용자 sudo 명령 실행을 기다린다. 활성화 확인은 후속 OPERATIONS_REPORT에 별도 기록한다. 이전 handoff 문서는 수정하지 않았다.

## 구현

- CLI의 의도된 typer.Exit을 generic Exception보다 먼저 재전파한다. failed/interrupted/blocked는 원래 payload를 한 번만 출력하고 exit1을 유지한다. succeeded/no_change exit0과 실제 unexpected 오류 진단을 유지했다.
- KST 날짜·channel별 카드사 고정을 유지하면서 최대 3회 신규 자동 배치를 허용한다. 완료·실패 뒤 2·3회는 동일 카드사의 새 run이며, 4회는 provider startup 이전 rotation_daily_attempt_limit으로 차단한다. 자동으로 세 번 반복 실행하는 기능은 아니다.
- issuer_rotation_attempt 테이블에 회차별 run/result를 보존한다. 기존 day의 6개 컬럼은 유지하여 이전 이미지의 positional INSERT와 호환한다. 기존 day는 애플리케이션 최초 사용 시 1회차로 이행한다. SQLite transaction과 기존 worker lock으로 직렬화하고 이전 회차 assignment의 재바인딩/결과 덮어쓰기를 거부한다.
- 미완료 작업은 같은 run/회차로 재개한다. 실제 CLI에서 scope_session에 transport/게시 승인 설정을 전달하여 봉인된 게시의 reconciliation 경로를 연결했다. reconciliation으로 회복한 결과는 해당 호출에서 반환하고 추가 회차를 동시에 시작하지 않는다. 수동 issuer/명시 resume의 기존 비순회 계약을 유지한다.
- 다음 날짜에만 queue를 다음 카드사로 전진시킨다. 오늘 10시 우리 실배치=1회, 12시 저장 결과 반환=추가 회차 아님. 오늘 13시 신규 실행은 우리 2회차, 내일 13시는 국민이다. 운영 DB/cursor/result를 SQL로 초기화하지 않았다.
- repo와 새 release timer를 매일 13:00 Asia/Seoul, Persistent=false로 변경했다. docs/OPERATIONS.md에 정책·이력·재부팅 동작을 설명했다.

## 검증

- 최종 관련 검사: 70 passed in 9.30s. 3회 허용/4회 차단, DB reopen, 기존 day 이행, 다음날 국민, 미완료 재사용, terminal result reconciliation, 수동/취소, stale assignment, CLI 단일 JSON/exit 및 인접 봉인·재개 회귀를 포함한다.
- 최종 전체 Worker/Core: 1866 passed, 11 warnings in 59.62s. 로그 /tmp/fix33-full-tests-final.log.
- ruff check 통과, ruff format --check 162 files 통과, git diff --check 통과. 적용 스크립트 shellcheck 통과. systemd-analyze verify/calendar에서 오늘/내일 13시 확인.
- 최종 이미지 startup-check: ready, quality_status=passed, 20.098s. 기본 OCR codex-exec/gpt-6.1-sol과 fallback local-paddleocr/PaddleOCR-VL-1.6 유지. startup-check는 신규 배치/순회 회차를 소비하지 않는다.
- 최종 이미지의 격리 SQLite smoke에서 같은 카드사 3회 실행 기록과 4회 차단 및 기존 day 컬럼 6개 유지 확인. 원본 HTTP/OCR 호출 없음. 설치 source 81개가 frozen release와 일치한다.

## 운영 준비 및 권한 경계

- 준비 release: /opt/cardrag/019-fix33-7d2e39edbf59
- image: cardrag-worker:019-fix33-7d2e39edbf59
- image ID: sha256:71eb18e6538de2e0b00fd3c5173045263980322c623b17e3debee7d782d51450
- source tree SHA256: 7d2e39edbf596526daa3b061f75a9cda2b2cbfc514949323955fa5bbdae29262
- 이전 운영 release/image는 019-fix32-55c579b986cb, image ID sha256:80bd67033c6ce0443f5e936315c79ed9e8d3e8ba5a48328ddc86ac2a1a84c65a. 기존 runtime/dependency lock을 변경하지 않고 network=none으로 소스 overlay 이미지를 만들었다.
- 실제 state/serving/auth/OCR-model 볼륨과 프록시 off 설정 유지. stable generation g-666c2d2ee9e24d04acf76eed-d6ffd9a927e9와 기존 failed run e81c17e69b5f428f8d6314ab2b8762d4 유지.
- worker.lock/backup.lock 동시 fence 안에서 독립 SQLite online backup 및 quick_check/hash 검증. 보호 snapshot /state/deployment-backups/019-fix33-current-20261022. worker DB 7404785664 bytes SHA256 875893c656d803f98d3a5622e2dbf2f5bc883fc59f9dc92416d26741785d637e, ledger 24416256 bytes SHA256 d138d3eb55536bf41d3adf552bf6151a7245635a4d7e1a1f78ead7b2d6ea4427.
- 새 snapshot 검증 후 이전 fix32 snapshot만 제거하여 약 7.43GB allocated 공간 회수. 보호 snapshot은 하나이며 구/신 release 복구 안내를 최신 보호본으로 갱신했다. DB restore는 실행하거나 호환 승인하지 않았다. startup preflight 여유 공간 약 110GB.
- 사용자에게 아래 한 줄을 일반 출력 코드블록으로 제공했다. sudo 인증이 없으므로 이 단계는 아직 대기다. 스크립트는 현재 release/image/오늘13시전 조건, 실행 중 Worker 부재를 확인하고 timer 중지, writer/backup lock fence, atomic release 전환, timer 설치, daemon-reload, 실패 상태 초기화 및 enable --now를 수행한다. 지금 배치는 실행하지 않는다.

```bash
sudo /opt/cardrag/019-fix33-7d2e39edbf59/operations/apply-fix33-13h.sh
```

작성 시점 실제 /opt/cardrag/current는 fix32이고 next elapse는 내일12시다. 위 명령 실행 후 오늘13시 예약으로 바뀌는지 반드시 확인해야 한다. 오늘13시 전 실행을 안전 조건으로 검사하므로 시간이 지나면 재검토 없이 적용하지 않는다. 실제 원본 서비스 오류가 지속되면 새 정책과 별개로 배치는 실패할 수 있다. 오늘13시 실배치 결과/백업 성공은 아직 검증하지 않았으며 성공으로 선언하지 않는다.
