# FIX_01 — 03:00·09:00 무인 검증과 은퇴 원장 마무리

작성: Reviewer, 2026-10-03 KST. 대상: 004 `PLAN.md`, Executor `REPORT.md`, 현재 `feature/004-worker-cutover-integrity`의 `1c992e3`. 이 문서는 기존 PLAN/REPORT를 수정하지 않고 남은 조치와 수용 기준을 정한다. Executor는 수행 후 `FIX_01_REPORT.md`를 새로 작성한다.

## 판정

**운영 승격은 유효하고, 004 최종 인수는 아직 보류한다.** 감독 stable run `56083821…`은 DB `succeeded`·publish `ready`, OCR 5,509/5,509·신규 provider 0, MCP 새 세대 `g-56083821…` 활성화와 healthy 상태로 확인됐다. WebDAV OCR 검증과 58.05 GB 정리 결과를 반복할 필요는 없다. 다만 계획의 무인 실행 2회가 없고 아래 은퇴 판정 경계가 남아 있다. 서비스 롤백이나 MCP 재시작을 요구하지 않는다.

보고서 작성 뒤 운영 상태가 바뀌었다. 2026-10-03 **20:49:44 KST에 `cardrag-worker.timer`가 이미 active/waiting으로 재개**됐고, 현재 다음 발화는 10월 4일 03:00 KST다. `/etc/cardrag/worker.env`는 새 OCI digest `sha256:d9f1dfed…`와 v130, stable, 원격 GC off를 지정한다. 따라서 REPORT §8의 “타이머 시작 필요”는 현재 사실과 다르다. 운영 설정을 다시 초기화하지 말고 이 상태를 기준으로 진행한다.

## 필수 수정 1 — 검증 일정을 같은 날 03:00·09:00으로 단축

004 PLAN §5의 “서로 다른 03:00 정기 실행 2회”를 **실제 timer가 기동한 서로 다른 무인 배치 2회**로 대체한다. 첫 기회는 10월 4일 03:00과 09:00 KST다. 6시간 간격은 운영 cadence 검증을 위한 임시 일정이며, 은퇴의 최소 2회 조건은 시간 간격이 아니라 서로 다른 정상 완료 run의 증거로 판정한다.

1. 현재 timer/service 상태와 마지막/다음 발화, 실행 중인 수동 Worker 부재, v130 볼륨·이미지 digest를 기록한다. 기존 `/etc/systemd/system/cardrag-worker.timer` 본문은 건드리지 않는다.
2. 이름을 고정한 임시 drop-in `/etc/systemd/system/cardrag-worker.timer.d/004-validation.conf`를 사용한다. `[Timer]`에 빈 `OnCalendar=`로 기본 03:00 일정을 리셋한 뒤 `OnCalendar=*-*-* 03:00:00 Asia/Seoul` 및 `OnCalendar=*-*-* 09:00:00 Asia/Seoul`을 각각 지정한다. 검증 기간에는 `Persistent=false`로 둔다. 지금은 당일 09:00이 이미 지났으므로 원래 `Persistent=true`를 유지한 채 재시작하면 **과거 09:00의 catch-up 실행이 즉시 발생할 위험**이 있다.
3. 설정 구문과 다음 발화를 검증하고 `daemon-reload` 후 timer만 재시작한다. `systemctl show`/`list-timers`에서 10월 4일 03:00, 03:00 발화 후 09:00이 다음 시각인지 확인한다. 이때 `cardrag-worker.service`가 이미 active거나 수동 컨테이너가 v130을 쓰면 timer 변경·수동 실행을 겹치지 않는다. MCP는 건드리지 않는다.
4. 03:00 run이 완전히 끝나고 DB/Worker 락이 해제됐는지 09:00 전 확인한다. 03:00 run이 09:00까지 계속되면 09:00 이벤트는 두 번째 성공으로 세지 않는다. Worker를 강제 종료하지 않고 그 결과를 기록한 뒤 다음 가능한 **서로 다른 timer run**으로 연속 기준을 채운다.
5. 각 회차의 systemd 실제 trigger·journal, 서로 다른 `run_id`, exit와 DB 상태, discovery/corpus-diff, 원격 pointer/READY 및 MCP 상태를 보존한다. `worker_busy`는 exit 0이어도 배치가 아니므로 제외한다. `succeeded`는 새 publish `ready`·pointer 결속을, 정상 `no_change`는 fresh discovery·corpus gate와 **기존 ready generation/pointer 불변**을 확인하면 성공으로 센다. 신규 OCR·deferred 건수는 실행값으로 기록하며 120을 고정 합격값으로 두지 않는다. MCP `/health/ready`와 `tools/list` 12개, 디스크 여유 ≥80 GiB를 확인한다.
6. 두 회차가 연속 통과하면 **임시 drop-in만 제거**, `daemon-reload` 및 timer 재시작 후 원래 **매일 03:00, Persistent=true** 일정으로 돌아왔는지 확인한다. 실패 또는 겹침이 있으면 성공 횟수를 다시 세되, 2일을 무조건 기다리는 규칙으로 되돌리지 않는다.

03:00·09:00은 현재 배포 이미지의 **무인 운영 증거**다. 아래 경계 수정이 그 뒤에 배포되더라도 동일한 timer·state·일반 배치 경로가 유지되고 회귀 테스트와 감독 하 1회가 통과하면 이 두 운영 증거를 다시 이틀에 걸쳐 수집하지 않아도 된다. 수정이 정상 배치/발행 경로를 실질적으로 바꾼 경우에는 임시 2회 일정으로 재검증한다. 가능하면 경계 수정과 새 이미지 전환을 첫 03:00 전에 마친다.

## 필수 수정 2 — 은퇴 원장은 실제 정상 완료에만 결속

`pipeline.py`의 `_commit_pending_retirement_ledger()`가 `finish_run(..., "succeeded"/"no_change")` **앞에서** 호출된다(약 3645, 6633, 6654, 6717행; 취소 재조정도 약 2755행). pointer는 `write_retirement_ledger()`에서 즉시 전진하고, `load_retirement_ledger()`는 `updated_run_id`의 DB 상태를 검사하지 않는다. 따라서 pointer 기록 직후 `finish_run`이 실패하면 완료하지 않은 run의 결석이 다음 실행에서 유효한 grace로 사용된다. PLAN §2의 “실패/중단 run은 증가 0”을 아직 보장하지 못한다.

- 정상 완료 상태를 durable하게 기록한 뒤 해당 run의 관찰을 활성 원장에 반영하거나, 로더가 run 완료·ready generation 결속을 확인한 원장만 선택하도록 바꾼다. DB/파일 두 저장소 사이의 crash window에서 **조기 은퇴보다 관찰 1회 누락**을 택하고, 누락은 run-local 진단으로 재조정한다. 모든 `succeeded`/`no_change`/취소 재조정 경로를 같은 규칙으로 처리한다.
- 동일 run resume는 멱등, 실패/중단은 불산입, `no_change`는 fresh discovery/gate와 기존 ready generation 확인 시 정상 관찰 1회라는 경계를 **pipeline 통합 테스트**로 증명한다. 특히 원장 pointer 쓰기 직후와 DB finish 직후 각각 실패를 주입해 조기 은퇴·중복 카운트가 없음을 검사한다.
- 최신 실제 원장 `47ce9ac6…`(updated run `56083821…`)에는 Lotte 8건의 과거 **failed** run 기반 은퇴 이력이 `reinstated`로, AAP1543 1건은 `first_absent_run_id=retired_run_id=c622d3c4…`, absences=2인 **동일 run 기반 retired**로 남아 있다. REPORT에는 이 교정 결과가 없다. 기존 봉인 파일은 보존하고, c622/785/560의 discovery·성공 기록과 OCR 증거를 대조해 AAP의 유효한 연속 결석을 재판정한다. 확인되면 새 교정 원장에 실제 2개 성공 run을 연결하고, 아니면 candidate/부당 누락으로 재분류한다. Lotte 8건의 현재 재게시 상태는 유지하면서 잘못된 옛 판단을 감사 기록에 명시한다. 교정 후 로더가 새 원장을 실제 사용함을 검증한다.

## 필수 수정 3 — 새 기준선 문서의 OCR 증거를 사용할 수 있게 함

`pipeline.py` 약 3380–3400행은 rolling baseline의 `PriorEntry.ocr_sha256`을 **v1.0.28 seed에 같은 document_id가 있을 때만** 채운다. 최신 baseline은 5,509문서인데 seed OCR 원장에 없는 document_id가 **318건**이다(운영 v130 읽기 전용 비교). 새 `_retirement_evidence_ok()`는 SHA가 없으면 즉시 `False`이므로 이 318건 중 정당하게 게시 중단된 문서는 WebDAV에 완전한 OCR이 있어도 은퇴 후보가 되지 못하고 배치를 막는다. REPORT §2.5의 “OCR 증거 해결”은 이 범위에서 성립하지 않는다.

- missing/retirement 대상에 한해 직전 봉인 generation의 문서별 OCR identity(SHA·크기·PDF/문서 결속)를 읽고 실제 로컬 또는 WebDAV OCR 바이트와 대조한다. seed에 없는 문서도 동일하게 처리한다. 5,509건 전체를 매일 다시 받는 방식은 피한다. 증거 없음·해시 불일치는 계속 fail-closed다.
- seed 밖 신규 문서가 다음 성공 run에서 후보, 두 번째 정상 run에서 정당 은퇴로 전환되는 회귀와 OCR CAS 손상 시 거부되는 회귀를 추가한다. 재게시 및 `no_change` 관찰 경로도 검증한다.

## 검증·보고 마무리

- 현재 락 회귀 테스트는 **한 프로세스에서 미리 점유한 락**을 CLI가 거부하는 테스트다(`test_cli_settings_provider.py` 약 358행). 코드의 연속 락 소유 방식은 타당하나 10/3 사고 재발 방지 증거로는 두 독립 프로세스를 장벽으로 동시 시작해 패자의 DB/WAL 무접촉을 확인하는 작은 테스트가 필요하다. 기존 전체 2,273 테스트와 별개로 추가한다.
- `git diff --check eecbd8a..HEAD`는 기존 `REPORT.md` 3·4행의 Markdown hard break 공백 2개 때문에 실패한다. 이는 운영 결함이 아니며 기존 REPORT를 덮어쓸 이유도 아니다. 새 FIX 구현 diff에 `git diff --check`를 적용하고 이 역사적 범위의 결과는 그대로 설명한다. `gitleaks detect --source .`는 현재 exit 0이다. GitHub CI는 PR 또는 `main`에서 실제 통과를 확인한다.
- `FIX_01_REPORT.md`에는 03:00/09:00 임시 timer 설정·적용/복원 시각, 각 run ID/DB 상태/원격·MCP 증거, 위 세 은퇴 경계의 수정과 테스트, 기존 잘못된 원장 교정, 실제 이미지 digest, 남은 GC off 사유를 기록한다. 전체 테스트·ruff·mypy·gitleaks·새 diff의 `git diff --check`와 Compose 렌더 결과도 남긴다. 변경한 로그는 자격증명·원문 식별자 없이 `.handoff/004_.../evidence/`에 보존한다.
- 004 PLAN의 `main` 병합·GitHub CI·새 릴리스는 아직 미완이다. 03:00/09:00 실증과 위 교정이 끝나면 **추가 03:00을 하루 더 기다리는 것을 인수 조건으로 삼지 않고** 최종 리뷰·병합·릴리스로 진행한다. 원격 GC는 PLAN이 허용한 대로 안전성 미확인 시 off 사유와 재개 조건을 기록하고 별도 후속으로 관리한다.

## 수용 기준

1. 다른 `run_id`의 timer 기동 2회가 연속해서 실제 배치를 수행하고 `succeeded` 또는 검증된 `no_change`로 끝난다. 03:00·09:00 같은 날 실행을 인정한다. 서비스 겹침/`worker_busy`/수동 run은 세지 않는다. 검증 뒤 일정은 매일 03:00으로 복원한다.
2. 은퇴 관찰은 서로 다른 정상 완료 run에만 결속되고, 실제 최신 원장의 AAP1543 오류가 교정·감사된다. Seed 밖 318문서도 결속된 OCR 바이트가 있을 때 후보·은퇴 가능하며 손상은 거부한다. 관련 경계 테스트가 통과한다.
3. 최신 이미지에서 MCP 서빙 연속, WebDAV OCR 보존, 디스크 하한을 유지한다. 전체 검사 및 GitHub CI가 통과하고 `main`/릴리스 상태가 004 REPORT/FIX_01_REPORT에 정확히 반영된다. 원격 GC off는 명시된 보류 조건으로 허용한다.
