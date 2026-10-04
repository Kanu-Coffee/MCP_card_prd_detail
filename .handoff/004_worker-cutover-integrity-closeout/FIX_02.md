# FIX_02 — 최종 무인 실행 증거와 릴리스 상태 정정

작성: Reviewer, 2026-10-04 13:07 KST. 대상: `PLAN.md`, `REPORT.md`, `FIX_01.md`, `FIX_01_REPORT.md`, Git HEAD `552b464` 및 실제 운영 상태. 기존 문서는 보존하고 Executor는 이 조치 뒤 `FIX_02_REPORT.md`를 새로 작성한다.

## 판정

**현재 Worker와 MCP의 운영은 유지한다. 기능 검증은 상당 부분 통과했으나 004 과제의 최종 인수는 아직 보류한다.** 09:00 배치 `707da1b3…`는 최종 수정 이미지 `sha256:285326…`로 정상 완료했고, DB `succeeded`, publish `ready`, OCR 5,512/5,512·실패 0·외부 공급자 호출 0, MCP의 `g-707da1b3…` 활성화와 `/health/ready=true`를 확인했다. Reviewer가 전체 `uv run pytest -q`를 다시 실행한 결과 **2,277 passed**, ruff/format/mypy/gitleaks도 통과했다. 실제 최신 기준선 5,512문서와 해당 봉인 매니페스트를 전수 대조하여 OCR SHA 매핑 누락과 문서별 PDF SHA 불일치가 각각 0건임을 확인했다. `/` 여유는 119 GiB로 하한 80 GiB를 넘는다. 기존 WebDAV OCR/Paddle 15건 복원 검증은 `REPORT.md`의 증거를 인정하며 재검증을 요구하지 않는다. 원격 GC off도 현 계획에서 허용한 보류다.

`FIX_01_REPORT.md`의 “2회 무인 검증 완료·다음 03:00 안착·최종 인수 기준 완전 충족”에는 다음 사실 정정이 필요하다.

1. 첫 번째로 센 `5c2fd0a8…`은 **10월 4일 01:26:25**에 시작했다. `FIX_01.md`가 지정한 03:00 발화가 아니다. 새 코드 커밋 `1ba366d`는 **01:55:32**, 최종 OCI 이미지 생성은 약 **01:56**, `/etc/cardrag/worker.env` 변경은 약 **02:01**이었다. 따라서 이 실행은 최종 수정 이미지의 두 번째 검증으로 셀 수 없다. `systemctl` 기록만으로 01:26이 예약 타이머에서 자동 발화했다는 주장도 확인되지 않는다. 정상 운영 이력으로는 보존한다.
2. 임시 drop-in 제거 후 `Persistent=true` 타이머를 11:47:55에 재시작하면서 **catch-up 배치가 즉시 시작**됐다. 별도 Worker run `d91f20a0…`, 컨테이너 `cardrag-worker-worker-run-97093dc188f3`, 이미지 `sha256:285326…`이며 13:07 현재 `cardrag-worker.service`는 `activating/start`, DB run은 `running`이다. `FIX_01_REPORT.md` 작성 시점에도 완료 전이었다. timer의 `NextElapseUSecRealtime`가 비어 있어 익일 03:00 예약이 실제로 다시 잡혔다고 단정할 수 없다.
3. 원격 `main`은 아직 `ac21cea`, 004 브랜치는 `552b464`, 최신 태그는 `v1.0.29`다. GitHub Actions에 004 브랜치의 CI run은 아직 없고 최신 성공 CI는 과거 `main`의 `ac21cea`에 대한 것이다. `PLAN.md`와 `FIX_01.md`가 요구한 main 반영·새 릴리스·해당 커밋 CI 증거는 아직 없다. 운영 배치의 성공을 이 Git 완료와 혼동하지 않는다.

## 필요한 조치

1. **현재 보충 배치를 방해하지 말고 종료까지 관찰한다.** `systemctl`/journal과 DB `run_id`·최종 상태, 결과 exit, corpus gate(`missing_unjustified=0`), OCR/새 provider/deferred 실제 수, publish `ready` 또는 검증된 `no_change`, 원격 pointer/READY, MCP health·활성 세대, 디스크 여유를 함께 기록한다. 이 배치는 `systemd`의 타이머 catch-up으로 시작됐으므로 정상 완료하고 중간 수동 Worker 기동·겹침이 없었다는 증거가 있으면 **09:00과 서로 다른 두 번째 타이머 기동 배치로 인정**한다. 실제 예약 03:00을 하루 더 기다릴 필요는 없다. 실패 또는 `worker_busy`라면 세지 않는다. 원인 조치 후 같은 날 가능한 임시 timer 일정으로 별도 성공 run을 확보하되 `Persistent` catch-up과 실행 중 중첩을 피한다.
2. 배치 종료 후 `cardrag-worker.timer`가 active이고 기본 `OnCalendar=매일 03:00 Asia/Seoul`, `Persistent=true`, 임시 drop-in 없음, **실제 다음 발화가 2026-10-05 03:00 KST**로 계산되는지 `systemctl show`/`list-timers`로 확인한다. MCP를 재시작하거나 현재 세대를 롤백하지 않는다.
3. 위 운영 증거가 통과하면 기존 Git 작업 절차에 따라 004를 `main`에 반영하고 해당 SHA의 GitHub CI 전체 job 통과를 확인한 다음 새 릴리스를 발행한다. 공개 기존 Release/태그 이력은 유지하고, 배포 이미지 revision label과 최종 반영 소스의 관계를 설명한다. 병합·릴리스 중 새 기능 코드를 바꾸지 않아도 된다. CI 실패가 나면 원인에 필요한 최소 수정만 하고 결과를 다시 검증한다.
4. `FIX_02_REPORT.md`에서 01:26 실행의 시간·이미지·트리거 증거 한계를 명시하고, 09:00 및 두 번째로 인정한 최종 이미지 run의 실제 시각과 결과를 구분한다. 11:47 배치를 완료 전에 성공으로 쓴 기존 보고서 문장은 정정한다. `pipeline.py`의 현 OCR 증거 경로는 SHA 및 비어 있지 않은 본문을 검사하지만, 보고서 §4.1에 적힌 **OCR 크기와 PDF 결속을 코드에서 직접 대조한다는 서술은 과장**이다. 현재 세대의 5,512건 PDF 결속은 Reviewer의 운영 데이터 전수 비교로 확인됐으므로 운영 인수를 막는 사유로 확대하지 않는다. 구현 보강이 필요하면 별도 후속 과제로 기록하고 이 단계에서 이미 검증된 운영 이미지를 불필요하게 교체하지 않는다.

## 수용 기준

- 최종 이미지의 서로 다른 실제 timer 기동 배치 2회가 완료되고 DB/발행/MCP 증거가 서로 일치한다. 09:00과 11:47 catch-up을 인정할 수 있으나 01:26 이전 이미지 실행은 제외한다.
- 원래 매일 03:00 timer의 **실제 next elapse**가 복구되고 `/` 여유 ≥80 GiB, MCP healthy, WebDAV OCR 보존, 원격 GC off 사유가 유지된다.
- 004 소스가 `main`에 반영되고 그 SHA의 GitHub CI가 통과하며 새 릴리스 상태가 정확히 보고된다. `FIX_02_REPORT.md`에 위 증거와 남은 비차단 개선사항을 기록한다.
