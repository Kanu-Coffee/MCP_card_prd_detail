# 015 FIX_01 — Reviewer 검토 및 수정 요청

- 작성: 2026-10-15 11:48 KST, Reviewer Codex.
- 검토 대상: `PLAN.md`, `REPORT.md`, 구현 `c6353533ea47c7ebe1595935f8cadb9558398668`, 문서 `96ce708c80f7b51e7308d6319ca26db8cb34329b`.
- 시작 시 working tree clean. 기존 FIX 없음. PLAN/REPORT는 보존하고 본 문서만 추가한다.
- 판정: **수정 요청 — P1 1건, P2 3건.** 실제 디스크 확보·복구 배치 성공은 인정한다. 아래 결함 때문에 구현 전체를 인수 완료로 판정하지 않는다.

## 1. 확인된 성과 및 검증 범위

- Docker inspect: `cardrag-worker-015-recovery` exit 0, image `sha256:e97c2c599dfdfed41ce9eb6890e6ddb47ef6aeb60408ae0225b4f801c9cdfe2d`, 종료 `2026-10-15T00:38:45.493815826Z`.
- 실제 로그: run `ec1255d924ab4c87a8a12491b762fc02`, generation `g-ec1255d924ab4c87a8a12491-8f4256acf3e6`, status succeeded, backup_status succeeded, collection_status degraded. capacity free 82,583,404,544 B ≥ required 73,628,921,528 B. serving retention removed=1 로그 확인.
- MCP healthy 및 current pointer를 읽기 전용으로 재조회. timer active, 다음 10/16 03:00 KST. systemd service의 마지막 실패 상태는 03시 실패 실행 기록이며, 별도 named recovery 성공과 모순되지 않는다.
- 현재 `df -B1 /` available 95,973,777,408 B. private cleanup 로그 및 deployment JSON을 대조하여 실제 삭제·운영 이미지 교체 증거 확인. 현재 여유 공간을 Executor 당시 수치로 소급하지 않는다.
- Reviewer 재실행: PLAN 지정 5개 테스트 파일 **298 passed / 10.07초**, `git diff --check f8453d0 c635353` 통과. 전체 2,520개/lint/mypy는 Executor 보고 내용이며 이번 Reviewer가 재실행한 것으로 표현하지 않는다.
- 추가로 임시 디렉터리·합성 SQLite·mock preflight/rmtree를 사용한 무네트워크 재현을 수행했다. 실제 corpus/유료 API/배포/삭제를 반복하지 않았다. 아래 4건은 테스트 통과와 별개로 확인된 결함이다.

## 2. [P1] deadline이 진행 중 검증을 제한하지 않는다

**위치:** `apps/cardrag-worker/src/cardrag_worker/cli.py:870–896`, 관련 `docs/OPERATIONS.md:282–287`.

`preflight_openrouter_qwen_providers()`를 timeout 없이 await하고, 예외가 반환된 뒤 다음 attempt를 시작할지 결정할 때만 elapsed를 검사한다. 첫 attempt가 오래 걸리거나 deadline 뒤 성공하면 그대로 통과한다. metadata GET + 여러 provider sample POST 각각에 기존 HTTP retry가 적용되므로 단일 요청 timeout은 stage 전체 deadline이 아니다. REPORT의 21~25분 상한 역시 이 구현으로 보장되지 않는다.

**재현:** production `_qwen_embedding_provider`에 fake tokenizer와 0.05초 뒤 성공하는 mock preflight를 주입하고, 시간만 축소한 settings 객체의 deadline을 0.01초로 설정했다. 결과 `elapsed_seconds=0.05`, `returned_success=true`. 설정 validator를 우회한 것은 검증 시간을 줄이기 위한 테스트 조건이며, 실제 60/600초에서도 동일한 await 경로다. 기존 deadline 테스트는 retry delay를 1000초로 바꿔 다음 attempt를 막는 경우만 검사한다.

**수정 요구:** stage 진입부터 공유하는 monotonic deadline으로 실행 중 preflight와 retry sleep을 제한한다. deadline 소진은 안전한 별도 typed reason/terminal 이력으로 남기고 종료를 nonzero로 유지한다. 진행 중 HTTP context 정리와 SIGTERM 취소를 drain하며, timeout을 generic retryable 오류로 다시 재시도하지 않는다. 어떤 구간을 deadline에 포함하는지 설정/문서에 명시한다. 운영 문서와 수정 보고서에 실제 보장과 일치하는 총 시간 설명을 남긴다.

**인수 테스트:** 첫 attempt가 budget을 초과하면 취소되어 실패; budget 직전 retry 후 남은 시간만 사용; 정상 완료·HTTP transient retry·외부 cancellation/worker lock 해제 유지. 실제 수십 초 기다릴 필요 없이 짧은 테스트 budget을 사용한다.

## 3. [P2] startup 이력 파일이 symlink를 따라 state 밖 파일에 쓰기한다

**위치:** `apps/cardrag-worker/src/cardrag_worker/cli.py:541–551`, `_run`의 lock 전 `startup_recorder.record()` 호출.

history를 `exists/stat` 후 일반 `os.open(O_APPEND|O_CREAT)`으로 열어 symlink 및 비정규 파일을 거부하지 않는다. 이 기록은 Worker lock/최종 state traversal 이전에도 실행된다. 따라서 뒤 단계에서 symlink를 거절하더라도 진단 기록이 이미 다른 파일을 수정할 수 있다. capacity의 no-follow 방어와 맞지 않으며, FIFO 등에서는 best-effort 진단이 실행을 막을 수도 있다.

**재현:** 임시 state의 `startup-attempt-history.jsonl`을 임시 외부 `outside.txt`에 symlink하고 `StartupAttemptRecorder.record('test','passed')` 호출. 외부 파일에 JSONL이 append되어 `outside_file_modified=true` 확인. 실제 운영 파일은 사용하지 않았다.

**수정 요구:** 검증된 state directory descriptor에 상대적으로 no-follow/nonblocking open하고 regular-file 여부를 확인한다. rotate/read/write 경로와 동시 startup 기록도 함께 검토한다. 부적합 파일/경로는 변경하지 않고 journal로 fallback하며 주 작업을 방해하지 않는다. 기존 permission 0600 및 보존 크기 제한을 유지한다.

**인수 테스트:** symlink target 바이트 불변, FIFO/비정규 파일에서 즉시 fallback, 정상 append/rotate 유지, 동시 lock-rejected startup의 이력이 활성 writer 파일을 훼손하지 않음.

## 4. [P2] 연속 수집 실패 횟수가 현재 run의 결과를 반영하지 않는다

**위치:** `apps/cardrag-worker/src/cardrag_worker/state.py:1263–1267`, `pipeline.py:3199–3203`, `pipeline.py:3992–4003`.

collection 보고서는 현재 run이 `running`일 때 기록되는데 새 SQL은 completed status만 조회한다. 현재 실패가 집계에서 빠지고 현재 성공으로 회복해도 지난 실패 streak가 남는다. 따라서 REPORT의 신한/롯데 연속 실패 수치도 현재 실행 포함 여부를 재검증해야 한다.

**재현:** 실제 `WorkerState` 임시 DB에 과거 실패 1회와 running 현재 run의 실패 collection을 기록했다. expected 2, actual 1. 현재 collection을 성공으로 바꾸면 expected 0, actual 1. 현재 run 외의 동시/미완료 run을 함부로 포함하는 방식으로 고치지 않는다.

**수정 요구:** report 대상 current run의 확정 collection 결과를 명시적으로 반영하고, 이전 completed 이력과 중복 집계하지 않는다. 아직 수집 중인 경우는 미확정으로 구분한다. 첫 실패/실패 연속/현재 성공으로 초기화/해당 issuer 미도달/조회 limit 경계를 검증한다. 이미 발행한 보고서는 덮어쓰지 않고 수정 보고서에 기존 수치의 의미와 재확인 값을 남긴다.

## 5. [P2] staging 삭제 실패를 removed로 집계한다

**위치:** `apps/cardrag-worker/src/cardrag_worker/local_publisher.py:447–450`.

`_remove_tree_with_accounting()`는 실패 시 warning과 0을 반환하지만 staging 호출자는 결과와 무관하게 `removed_staging += 1` 한다. 디렉터리가 남았는데 summary가 removed=1로 출력돼 REPORT의 “삭제 실패 시 removed count는 증가 안 함” 주장은 staging 경로에서 성립하지 않는다. 디스크 확보가 주목적인 변경에서 재발 시 운영 판단을 잘못 유도한다.

**재현:** 임시 staging tree의 `shutil.rmtree`를 PermissionError로 주입했다. helper 반환 0, tree 존재=true. 호출자의 무조건 증가 코드를 대조했다. generation 경로에는 existence 확인이 있으나 staging에는 없다.

**수정 요구:** helper가 삭제 성공 여부와 byte 추정치를 구분해 반환하거나 그에 준하는 명시적 결과를 사용한다. 빈 tree 성공과 실패를 모두 정확히 구분한다. 필요하면 attempted/failed/removed 별도 집계. 바이트는 논리 파일 크기 합계 추정이며 실제 filesystem 회수량은 df 전후 측정이라는 의미도 유지한다.

**인수 테스트:** 권한 실패는 removed=0/failed=1 및 안전한 warning, 빈 tree 성공 removed=1, 정상 tree 성공 및 current/previous 보존.

## 6. Executor 수정 범위·재검토 자료

1. 위 4건을 수정하고 관련 기존 테스트에 회귀를 추가한다. 기존 PLAN/REPORT/FIX 문서를 수정하지 않는다.
2. 우선 cli/state/pipeline/local publisher 관련 선택 테스트, 이후 PLAN의 5파일 및 영향 있는 lock/signal/retention 테스트, 기존 lint/type 검사를 수행한다. 변경 범위와 실패 여부에 맞게 확대한다.
3. deadline·이력·집계 수정 검증 때문에 full corpus/유료 OCR를 다시 실행할 필요는 없다. 운영 반영 시 현재 current/rollback/credentials/timer를 보존하고 source/image 식별자를 기록한다. 10/16 예약 완료를 이번 수정의 완료 증거로 미리 주장하지 않는다.
4. REPORT §3의 `VACUUM` 목적지 “≤2P”와 4P가 “완화 여지 없는 상한”이라는 표현은 실제 강제 cap·실패 경로 실측과 구분해 수정 보고서에서 보완한다. exporter의 목적지 크기는 완료 후 검사하며 Reviewer는 peak 실측을 확인하지 않았다. 예측 계수를 유지하는 선택은 허용하나 수학적으로 증명된 최소값이라고 단정할 근거는 부족하다.
5. `.handoff/015_worker-startup-retry-disk-capacity/FIX_01_REPORT.md`에 각 항목의 변경·명령/결과·운영 반영 여부·미해결 위험을 기록한다. Reviewer는 그 다음 수정 diff와 증거를 확인하여 인수를 판단한다.

이번 검토는 운영 복구 성과를 취소하거나 즉시 rollback을 요구하지 않는다. 완료된 디스크 정리/배치 복구는 확인됐고, 남은 문제는 위 수정 라운드의 대상이다.
