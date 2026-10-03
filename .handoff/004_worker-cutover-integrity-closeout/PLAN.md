# 004 — 운영 Worker 전환과 003 무결성·무인 실행 마무리

작성: Planner/Reviewer, 2026-10-03 KST. 이 계획은 미완료 003의 후속 과제다. 003의 기존 `PLAN.md`, `REPORT.md`, `HANDOFF_20261003_EXECUTOR.md`와 증거를 수정하지 않는다. 실제 구현·실행 결과는 004 `REPORT.md`에 기록한다.

## 목표와 완료 판정

건전한 v130 상태와 이미 발행된 stable 세대를 기반으로 운영 Worker를 복구하고, 10월 3일 사고와 은퇴 판정의 재발 경로를 고친 뒤 실제 03:00 예약 배치 **연속 2회**를 관찰한다. MCP 서비스와 WebDAV의 기존 OCR 바이트를 보존한다. 불필요한 이전 운영 볼륨을 치우고 검증된 롤백 근거는 최대 1개만 남긴다. 검증 후 코드의 `main` 반영과 릴리스를 마친다.

운영 복구와 데이터 무결성에 필요한 수정은 첫 예약 실행 전에 처리한다. 관측성·문서 정리는 정상 운영을 지연시키지 않는 범위에서 이어서 완료한다. 특정 발급사의 은퇴 건수나 OCR 변형 건수를 합격을 위한 고정 숫자로 사용하지 않는다. 실제 upstream 및 실행 결과에 따라 계산한다.

## 003 전수검토 결과 — 004의 출발 상태

| 영역 | 확인된 사실 / 근거 | 004 판단 |
|---|---|---|
| Git | `feature/003-retirement-rolling-baseline`의 `4ca3929`가 `main` `ac21cea`보다 20커밋 앞서 있다. 롤링 기준선, 은퇴 판정, Hana 파서, OCR 캐시 수정을 포함하지만 `main`에는 아직 없다. 004 브랜치는 이 HEAD에서 분기했다. | 003 코드를 기초로 이어서 개발한다. |
| 003 보고의 성공 횟수 | `003/REPORT.md` §2·§4 및 후속 HANDOFF §8-6은 candidate run-1/run-2를 성공으로 썼으나 `evidence/v130-run1-watch.out`, `v130-run2-watch.out` 첫 줄은 각각 **exit 1**이다. v130 DB도 `380d0212`, `4a87bd56`을 failed로 기록한다. Candidate의 확인된 성공은 `c622d3c4…`(10/2) 1건이다. | 기존 문서는 보존하고 004 보고서에서 정정한다. 기존 성공 주장에 기대어 무인 2회 기준을 통과 처리하지 않는다. |
| 10/3 수동 승격 | 로컬 `003/evidence/prod-promote-4-watch.out`(원본 SHA-256 `e172c5dc7eaffe63f94004da9ad9ab0d59ec913e916fbb8f1bae7e9a1cb355db`, Git 미추적)과 이 계획의 `evidence/prod-promote-4-watch.redacted.out`: patch11/v130의 `785c632447c94b059777a0eace3c8741`이 14:20:43 KST exit 0. OCR 5,509/5,509, provider 호출 0, cache publication deferred 120, source coverage 100%, 누락 0, GC 임시 비활성(`gc_status=null`). v130 SQLite `quick_check=ok`, run `succeeded`, publish `ready`, 최신 baseline 해시 일치(current 5,060 / historical 449). | 실제 수동 프로덕션 성공이다. 03:00 무인 성공으로 세지 않는다. deferred 120건은 손상 120건이라는 뜻이 아니다. |
| 서비스와 예약 | WebDAV stable pointer → READY → manifest SHA가 `g-785c632447c94b059777a0ea-659ad1aa55d0`에 결속되고 manifest는 5,509문서다. MCP는 14:38 KST 같은 세대를 활성화, `/health/ready=true`, 인증된 `tools/list` 12개. 10/3 03:00 timer는 `worker_busy`로 실제 배치를 하지 않았다. Timer는 **enabled이지만 inactive**, 다음 발화 없음. `/etc/cardrag/worker.env`는 아직 patch7 이미지와 손상된 `cardrag-worker-v129-state`를 지정한다. | 서빙은 정상이나 Worker 자동 운영은 중단 상태다. 환경 전환과 timer 재개가 급선무다. |
| 디스크·복구 | `/` 여유 약 89 GiB, v130 약 48 GiB. v129 원본과 손상 DB 증거 복제본이 추가 공간을 사용한다. 002에서 WebDAV OCR 5,207건/고유 CAS 4,922개 및 요청된 Paddle 15/15 바이트 검증·무호출 복원이 입증됐고, 003에서 과거 stable의 5,213문서 `restore-ocr-seed` dry-run이 성공했다. **새 5,509문서 세대의 모든 OCR CAS 바이트는 아직 별도로 전수 검증하지 않았다.** | 현재 stable과 직전 1개 세대의 OCR 복원성을 확인한 뒤 구형 볼륨을 정리한다. 설정·임베딩의 백업 완전성은 복구 계약에 포함하지 않는다. |
| 은퇴 상태 | 8개 Lotte lineage는 재게시되어 최신 ledger에서 `reinstated`. 별도 AAP1543 계열 1건은 `first_absent_run_id=retired_run_id=c622d3c4…`, `consecutive_absences=2`로 기록됐다. 이전 Lotte 8건의 최초 결석·은퇴 실행은 모두 실패 run이었다. 현재 공개 corpus-diff에는 부당 누락 0이다. | 9/30에 예상한 Lotte 8건의 실제 은퇴를 합격 조건으로 요구하지 않는다. 잘못 센 과거 ledger를 감사·교정한다. 이미 발행된 세대는 소급 변경하지 않는다. |
| CI 비밀 검사 | 004 Planner의 `gitleaks detect --source .`는 기존 003 커밋 3개의 evidence에서 `generic-api-key` 209건을 보고해 exit 1이다. 209개 해당 줄은 전부 `reuse_key`(204) 또는 `tokenizer_sha256`(5)에 들어간 **64자리 SHA-256 식별자**임을 검사했다. 004의 식별자만 가린 증거 디렉터리는 `gitleaks dir` 0건, Trivy secret 0건이다. | 실제 비밀값을 허용하지 않는 좁은 예외/증거 정리로 CI를 복구해야 한다. 이번 004 계획 커밋의 새 누출은 확인되지 않았다. |

## 확인된 코드 결함과 우선순위

1. **P0 — 락 경쟁 구간:** `cli.py` 약 396행의 `with worker_lock(...): pass`는 DB 개방 전에 락을 즉시 놓는다. `pipeline.py` 약 2369행에서 다시 잡기까지 두 Worker가 SQLite를 동시에 열 수 있다. 10/3 DB 손상 사건의 위험 경로가 남는다. 기존 테스트는 이미 락이 점유된 경우만 검사한다.
2. **P0 — 은퇴 grace의 조기 승인:** `pipeline.py` 약 3412행에서 OCR·봉인·성공 이전에 retirement ledger를 기록한다. `retirement.py` 약 440행은 `last_checked_run_id`나 성공 상태를 보지 않고 횟수를 올린다. 같은 run 재개만으로 `candidate/1 → retired/2`가 재현됐으며 실패 run도 누적됐다. `days >= grace_days` 단독 조건 역시 003의 최소 2개 성공 run 요구와 어긋난다.
3. **P0 — 롤링 기준선 뒤 은퇴 추적 누락:** `corpus_diff.py` 약 304행은 당일 `missing`이 있을 때만 resolver를 호출한다. 후보가 새 기준선에서 빠진 다음에는 `missing=0`이 되어 후보의 결석 누적과 재게시 반영이 멈출 수 있다.
4. **P1 — OCR 원격 손상 오분류:** `ocr.py` 약 1959–2009행은 캐시 조회가 `None`을 반환하거나 검증 예외가 발생해도 retained seal이 있으면 “검증된 다른 변형”으로 간주해 발행 유예할 수 있다. 손상 READY를 넣은 메모리 재현에서도 `cache_publication_deferred=True`가 나왔다. 현재 deferred 120건에 손상이 있다는 증거는 없다. 유효한 다른 OCR 본만 R2 정책으로 유예해야 한다.
5. **P1 — 은퇴 OCR 증거 불충분:** `pipeline.py` 약 2607행은 OCR SHA 필드 또는 기존 기준선 항목만 있으면 실제 바이트 확인 없이 증거를 참으로 본다. PDF CAS는 재해시하지만 OCR 복원성은 보증하지 못한다.
6. **P2 — 진단·설정 설명:** OCR 실패가 원인 예외를 가려 조사 비용을 높였고, deferred 건수는 `warnings.warn` 중복 억제 때문에 경고 줄 수로 셀 수 없다. `CARDRAG_OCR_COMPATIBLE_MODELS`는 **현재 primary 모델 목록이 아니라 과거/대체 계약 탐색 목록**이므로 qwen이 없다는 사실 자체는 결함이 아니다(`ocr.py` 약 757–789행).

## 제약

- 안정적으로 서빙 중인 `cardrag-stable-v1026-mcp-1`의 이름·볼륨·프록시를 바꾸거나 불필요하게 재시작하지 않는다. stable pointer는 검증된 publish 절차로만 변경한다.
- v130은 현재 성공/ready 발행을 가진 사실상 운영 상태다. 별도 실험용으로 손상시키거나 이름만 바꾸기 위해 48 GiB를 복제하지 않는다. 동일 볼륨의 Worker는 어떤 시점에도 1개만 실행한다.
- `/etc/cardrag/worker.env`에는 자격증명이 있다. 값 전체를 로그·Git·보고서에 싣지 않는다. 소유권 `root:cardrag`, 모드 `0640`을 유지하며 변경 전 스냅샷은 **직전 1개**만 보유한다.
- Timer는 `Persistent=true`다. 활성화 직후 catch-up 실행 여부를 확인해야 한다. 낡은 v129 설정으로 재활성화하지 않는다.
- WebDAV OCR만 재난 복구의 필수 보존 대상이다. 현재 세대는 stable pointer→READY→manifest, 직전 세대가 남아 있다면 이전 봉인/DB의 generation ID→READY→manifest로 검증한다. **현재 stable OCR CAS의** SHA/크기와 `restore-ocr-seed` 무호출 복원이 구형 로컬 볼륨 삭제의 필수 선행 조건이다. 직전 세대가 남아 있으면 OCR CAS도 검증해 유일한 롤백 근거로 선택한다. 이미 GC로 사라졌다면 이를 보고하고 현재 OCR 보존이 확인된 경우 정리를 영구 중단하지 않는다. 남아 있는 현재·직전 generation의 참조 객체를 수동 삭제하지 않는다.
- 작업 전과 각 대용량 단계 후 `df -B1 /`, 동적 startup capacity 측정치를 기록한다. 여유 80 GiB 미만 또는 사전검사 실패 시 새 전체 볼륨/배치를 시작하지 말고 정확한 구형 대상만 선별 정리한다. 포괄적인 `docker system prune --volumes` 사용 금지.
- 003의 user-approved `codex-exec/qwen3.8-flash` 및 R2 “기존 세대 seal 우선” 정책을 유지한다. 새 세대에서의 provider 호출은 0을 목표로 측정하되 실제 신규 PDF가 있으면 건수·사유를 설명한다. OCR 출력의 바이트 결정성을 가정하지 않는다.
- 001–003의 기존 handoff 문서·증거는 변경/삭제/덮어쓰기 금지. 004의 보고와 교정 artifact를 새로 남긴다.

## Executor 작업 순서

### 1. 증거 고정과 운영 전환 준비

- 원본 `003/evidence/prod-promote-4-watch.out`의 SHA를 확인하고, 이 PLAN과 함께 커밋된 **SHA 식별자 241개만 가린** `004/evidence/prod-promote-4-watch.redacted.out`을 증거로 쓴다. 원본을 덮어쓰지 않는다. 종료 코드, DB `quick_check`, run/publish, baseline hash, remote pointer/READY/manifest, MCP generation/12 tools, 여유 공간을 재측정한다. 003 REPORT의 run-1/run-2와 HANDOFF의 “연속 성공” 오기를 004 REPORT의 **정정 표**로 남긴다.
- `worker.env`에서 image digest·state volume·채널·OCR 정책·GC 스위치만 값 노출 없이 확인한다. timer/service와 수동 컨테이너의 실행 여부, `worker.lock`, systemd `Persistent`와 다음 발화를 확인한다. WebDAV의 직전 정상 세대를 롤백 근거 **1개**로 지정한다.

### 2. 첫 정기 실행 전 무결성 수정

- CLI 진입부터 DB가 닫히고 pipeline이 끝날 때까지 **하나의 Worker 락 소유권을 연속 보유**하도록 재구성한다. state를 바꾸는 preflight(예: resume symlink sweep)도 이 소유권 안에 둔다. 내부 pipeline에서 별도 fd로 같은 `flock`을 다시 잡지 않도록 락 전달 또는 단일 진입 경로를 택한다. 성공·예외·취소에서 해제가 정확히 한 번 이뤄져야 한다. 별도 두 프로세스를 장벽으로 동시에 시작하는 회귀 시험에서 패자는 DB를 열거나 변경하지 않고 `worker_busy`로 끝나야 한다.
- Retirement resolver는 새 baseline의 `missing`이 0이어도 열린 candidate/retired 항목 및 재게시 lineage를 재평가한다. lineage·successor·발급사 retention·상한·PDF CAS·OCR 증거를 계속 검사하고, 이전에 은퇴한 항목을 누락 경로로 숨기지 않는다.
- 결석 관찰은 run 로컬 report에 먼저 기록한다. **서로 다른 정상 완료 run 최소 2회**에 결속된 관찰만 durable ledger에 반영한다. 정상 완료는 `succeeded` 또는 fresh discovery·corpus gate와 기존 remote READY 결속을 완료한 `no_change`를 뜻한다. `no_change`에는 새 publish row가 없으므로 run ID와 검증된 기존 ready generation을 함께 남긴다. 같은 `run_id`의 resume는 멱등, 실패/중단 run은 grace 증가 0이다. 정상 관찰 사이에 재게시가 있으면 연속성은 리셋한다. 시간 grace만으로 최소 2회를 대체하지 않는다. 후보→은퇴 판정이 바뀌면 동일 corpus라도 새 generation 봉인/발행을 강제하거나 동등한 불변 원격 판정 증거를 설계·검증한다. 성공 발행과 ledger 기록 사이의 중단은 재실행 때 검증·재조정되게 하고, 관찰을 두 번 세지 않는다. 실패 run 진단은 별도 report로 유지한다.
- 기존 v130 ledger를 읽기 전용 감사해 실패 run Lotte 8건과 단일 run AAP1543의 판정 근거를 재구성한다. 새 corrective ledger는 이전 artifact의 해시·오류 근거와 유효한 성공 관찰을 연결한다. AAP의 c622/785c 두 성공에서 실제 연속 결석·OCR 증거가 확인되면 정당한 은퇴로 재판정할 수 있다. 입증되지 않으면 candidate/부당 누락으로 명시하고 조용히 통과시키지 않는다. Lotte 8건은 현재 재게시 상태를 유지한다.
- 은퇴 판단 대상에 한해 원격 또는 로컬 retained OCR seal의 실제 바이트/해시·문서 결속을 확인한다. 이전 baseline에 이름이 있다는 이유만으로 통과시키지 않는다. 5천여 전체 문서를 매일 중복 다운로드하는 검증은 요구하지 않는다.
- OCR 발행 충돌은 **검증 완료된 다른 출력**과 손상·검증 실패를 분리한다. READY→manifest→CAS를 해시/크기로 확인하되 다른 본을 run 디렉터리에 물질화하지 않는다. 확인된 변형이면 기존 seal 보존+명시적 defer, 손상 control/CAS·권한·네트워크 오류는 기존 fail-closed. deferred 사유·key 수를 구조화해 집계한다.
- 003의 Git 이력에 들어간 해시 식별자 209건 때문에 현 CI 비밀 검사가 실패한다. 각 finding의 파일/필드/값을 다시 확인한 뒤 **확인된 증거에만 한정**한 fingerprint 또는 값 단위 예외를 마련하고 `gitleaks detect --source .`를 통과시킨다. 전체 handoff 경로나 `generic-api-key` 규칙을 통째로 제외하지 않는다. 새 토큰 모양 fixture가 여전히 검출되는지도 검증한다. 새 증거는 004의 가린 사본처럼 게시 전 검사한다.

### 3. 좁은 회귀와 패키지 검증

- 실제 결함을 재현하는 테스트: 두 Worker 동시 시작/패자 DB 무접촉; 동일 run resume·실패 후 성공·정상 완료 run 2회(`succeeded` 및 `no_change`)·3일만 경과·rolling baseline 이후 `missing=0`·재게시·후보→은퇴 판정만 바뀐 동일 corpus; 기존 잘못된 ledger 교정; OCR seal 바이트 결손; verified divergent와 corrupt READY/manifest/CAS 및 401/403/timeout 별 결과. 기존 `test_retirement_v130.py`, `test_corpus_gate_v130_integration.py`, `test_corpus_baseline_v130.py`, OCR/CLI 테스트를 확장한다. 현재 기존 타깃 23건은 통과하나 위 경계를 검증하지 않는다.
- 전체 프로젝트 테스트, ruff, format, mypy, `git diff --check`, **gitleaks/Trivy**, worker Compose 렌더를 실행한다. 테스트 숫자는 실행 결과로 기록한다. 실패 원인 노출 개선은 비밀을 로그에 싣지 않는 구조화 사유로 하고, 관련 테스트를 추가한다. compatible-models의 현재 모델 생략은 문서 또는 작은 설정 테스트로 설명한다.
- 새 이미지를 GHCR에 올린 뒤 **원격 OCI digest, pull 결과, revision label과 소스 커밋**을 대조한다. patch11의 성공을 새 코드의 검증으로 오인하지 않는다. 용량을 소모하는 v131 전체 복제 대신 작은 격리 fixture와 실제 v130의 감독 하 1회로 검증하며, 추가 전체 복제가 필요하면 먼저 디스크 예산을 확보한다.

### 4. 운영 cutover, OCR 보존, 용량 회수

- 자동 timer는 계속 정지한 상태로, 최신 코드 이미지 digest와 **v130**을 가리키게 `worker.env`를 원자적으로 전환하고 Compose `config --quiet`·실효 환경을 점검한다. 현재 GC는 promote-4와 같이 명시적으로 off로 두고 별도 검증 후 복구한다. 낡은 patch7/v129로 돌아가는 설정은 사용하지 않는다.
- 현재 stable의 OCR 객체와, 직전 1개 세대가 남아 있다면 그 세대의 OCR 객체를 인증된 읽기 전용 GET으로 전수 스트리밍·SHA/크기 확인하고, `restore-ocr-seed`를 격리된 작은 복원 대상으로 시험한다. 002에서 요구된 **Paddle 작업 15건의 원래 OCR 바이트**도 WebDAV에서 15/15 해시·크기 검증하고 독립 복원 가능성을 확인한다. 최신 qwen 세대가 그 바이트를 사용해야 한다고 가정하지 않는다. 현재 stable의 WebDAV 보존이 입증되기 전에는 v129 자료를 지우지 않는다.
- v129 원본과 `cardrag-v129-corrupt-state-20261003`는 장애 증거(원인·SHA `199b9e1ad1616d5cb578ec64ada859a19b9762c1c4c965b7be7022b988ccf051`, 크기, 필요 최소 진단본)로 요약한 뒤, 더 이상 실행 설정·컨테이너 참조·복구 근거가 아님을 확인하고 **정확한 이름으로 하나씩** 삭제한다. 빈 `cardrag-v131-probe-state`, 종료된 promote 컨테이너와 불필요한 과거 이미지도 참조를 확인한 뒤 선별 정리한다. 현재 v130·MCP·실제 인증/모델 볼륨은 유지한다. 각 삭제 전후 `df`, `docker system df`, MCP health를 기록한다.
- 새 코드로 timer 시간창 밖에서 감독 하 1회 stable 배치를 실행한다. 상태 `succeeded`면 새 publish ready, `no_change`면 기존 ready 세대의 무변경 결속을 확인한다. DB `quick_check`/baseline/retirement ledger, OCR 100%와 실패 0, 실제 provider·deferred 수, source coverage, MCP 최신 세대/12 tools, 여유 공간을 확인한다. 실행 전 이번 discovery·새 PDF 수에 근거해 신규 OCR 호출 예산을 선언하고 예상 밖의 대량 재OCR가 시작되면 중단·진단한다. 오류 시 마지막 정상 세대는 계속 서빙하고 timer를 켜지 않는다.
- `systemctl start cardrag-worker.timer` 후 **active/next elapse**와 즉시 catch-up 기동 여부를 확인한다. `Persistent=true` 때문에 시작 직후 발화하면 동일 DB에 다른 Worker가 없는지 확인하고 그 실행을 별도로 관찰한다. 예약 실행과 수동 실행이 겹치지 않게 한다.

### 5. 무인 검증, GC, 릴리스

- 감독 run과 로컬 게이트가 통과하면 운영 중인 소스를 리뷰해 `main`에 병합하고 `main` CI를 확인한다. CI 소요 때문에 예약 실행 복구를 불필요하게 멈추지는 않되, 배포 digest의 revision label과 `main` 커밋 일치를 확인한다.
- 03:00 KST의 실제 timer 기동 2회를 **연속** 관찰한다. 이 계획 작성 시 다음 예정은 10/4와 10/5이지만 활성화 시각에 따라 실제 날짜를 기록한다. 각 회차마다 systemd trigger/run ID/exit, DB `succeeded` **또는 검증된 `no_change`**, 해당 새 publish `ready` **또는 기존 READY·pointer 불변**, MCP 세대/health, corpus diff, OCR 보존/새 호출, deferred 집계, 디스크를 대조한다. 정상 `no_change`는 무인 성공으로 센다. 감독 수동 run이나 `worker_busy` exit 0은 세지 않는다. 한 번 실패하면 연속 횟수는 다시 센다.
- 원격 GC는 우선 dry-run으로 현재/직전 세대 및 OCR CAS 보호 목록을 검증한다. 최근 120건의 다른 OCR 본을 직접 삭제/덮어쓰지 않는다. dry-run과 실제 적용의 참조·바이트 결속이 안전하고 정당한 제거 대상만 있을 때 감독 하 apply 및 다음 배치의 `gc_status`를 확인해 GC를 복구한다. 불확실하면 GC만 off로 유지하고 사유·추적 과제를 보고하되 03:00 배치 성공 자체를 숨기지 않는다.
- 정기 2회 관찰 결과를 004 REPORT에 기록하고, 전체 수용 증거가 모이면 새 버전을 발행한다. 기존 v1.0.29와 공개 GitHub Release 태그 이력은 보존한다. 병합된 003/004 작업 브랜치만 참조 확인 후 정리한다.

## 수용 기준과 보고 형식

1. 동일 DB에 두 Worker가 동시에 접근할 수 없고, 손상 원격 OCR control이 유효한 다른 본으로 오분류되지 않는다. 은퇴는 실제 **서로 다른 정상 완료 run 최소 2회**의 연속 결석과 바이트 증거로만 결정된다. `no_change`도 완전한 discovery·gate 및 기존 ready generation 검증을 통과해야 한다. 과거 잘못된 9건의 판정을 감사·교정했고 재게시가 반영된다.
2. 최신 Worker 설정은 정상 v130과 새 이미지 digest를 사용하고 timer가 active이며 다음 03:00 발화가 있다. 실제 예약 배치 2회가 연속 `succeeded` 또는 검증된 `no_change`로 끝났다. 각 run의 missing_unjustified는 0이며, 실제 upstream 변화에 맞는 candidate/retired/reinstated 결과가 불변 증거로 남는다.
3. MCP는 작업 중 healthy이고 최신 stable generation과 도구 12개를 서빙한다. 현재 세대 및 남아 있는 직전 세대의 OCR CAS와 요청된 Paddle 15건의 복원성이 확인되며, 새 OCR provider 호출·deferred 건수와 사유가 정확히 보고된다. 원격 GC는 안전하게 복구됐거나 별도 명시된 보류 사유와 재개 조건이 있다.
4. 불필요한 구형 로컬 볼륨/컨테이너를 안전하게 정리하고 종료 시 `/` 여유 ≥80 GiB. 운영 자료와 검증된 롤백 근거는 최대 1세트이며, 자격증명은 Git/로그에 없다. 전체 테스트·정적 검사·Compose 및 CI 결과를 기록한다.
5. 004 `REPORT.md`는 003의 잘못된 성공 주장과 실제 run/DB 증거, 코드·이미지 digest, 운영 설정의 비밀 없는 변경 전후, WebDAV OCR 검증, 삭제 대상별 근거와 용량, 감독·무인 각 실행 결과, GC 결과, `main`/릴리스 상태 및 남은 위험을 기록한다. 기준을 충족하지 못한 경우 완료라고 쓰지 않는다.

## 관련 파일과 증거

- 003: `.handoff/003_daily-batch-retirement-rolling-baseline/{PLAN.md,REPORT.md,HANDOFF_20261003_EXECUTOR.md,evidence/}`. 특히 `v130-run1-watch.out`, `v130-run2-watch.out`, `v130-run3r12-watch.out`, 로컬 미추적 `prod-promote-4-watch.out`, `prod-vs-candidate-ocr-divergence.jsonl`. 004에 추적되는 복제본: `evidence/prod-promote-4-watch.redacted.out`(원본 SHA 및 가린 필드 명시).
- 002 복구 계약: `.handoff/002_production-recovery-gc-release-disk/{PLAN.md,REPORT.md,FIX_01_REPORT.md,FIX_02_REPORT.md}`, `docs/RECOVERY.md`.
- 코드: `apps/cardrag-worker/src/cardrag_worker/{cli.py,state.py,pipeline.py,corpus_diff.py,retirement.py,ocr.py,settings.py,gc.py}`; 관련 테스트는 `apps/cardrag-worker/tests/`; 운영 렌더는 `deploy/worker/compose*.yaml` 및 `/etc/cardrag/worker.env`(Git 외부).
- 런타임: Docker volume `cardrag-worker-v130-candidate-state`, `cardrag-worker-v129-state`, `cardrag-v129-corrupt-state-20261003`; systemd `cardrag-worker.service`/`.timer`; MCP `cardrag-stable-v1026-mcp-1`; WebDAV `v1/channels/stable.json`.
