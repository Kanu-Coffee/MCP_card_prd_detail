# 020 FIX_01 — child reap 중 취소 보존과 FIFO lock의 유한 거부

2026-10-27 Reviewer Codex. 사용자 요청에 따라 020 PLAN/REPORT, 현재 Git diff/status, 새 `paddle_cleanup.py`와 관련 테스트를 검토했다. 기존 correction은 없다. **수정 후 재검토가 필요하며 현재 구현은 승인하지 않는다.** 이번 리뷰는 아래 두 문제의 재현과 본 수정 지시 작성까지다. 구현·운영 배포·링크 정리·timer 변경은 하지 않았다.

## 검토 범위와 인정한 변경

영구 state 밖의 부모 소유 alias, explicit dry-run/apply, 기존 Worker lock 사용, descriptor 기반 no-follow 경로 검사, 삭제 대상·diagnostics의 제한은 PLAN의 방향에 맞는다. 기존 sweep 제거에 따른 명시 복구 요구도 REPORT와 운영 문서에 기록됐다. 수정 전 존재한 예약/OCR 문서 변경과 종료된 019 이력을 보존했다.

REPORT의 1,964건 전체 테스트 통과는 Executor의 증거로 읽었다. Reviewer가 다시 실행한 검사는 다음과 같다.

```text
uv run --no-sync pytest apps/cardrag-worker/tests/test_paddle_inputs_v130.py apps/cardrag-worker/tests/test_cli_settings_provider.py apps/cardrag-worker/tests/test_capacity_v5.py apps/cardrag-worker/tests/test_capacity_v5_export_regression.py -q
227 passed in 9.08s

uv run --no-sync pytest apps/cardrag-worker/tests/test_pipeline.py -k 'cancel or shutdown' -q
9 passed, 124 deselected in 0.46s

uv run --no-sync ruff check apps/cardrag-worker/
All checks passed!

git diff --check
통과
```

별도 임시 fixture 두 개에서 아래 결함을 확인했다. 운영 데이터·실제 OCR 모델을 사용하지 않았다. 기존 테스트 통과는 이 경계 조건에 대한 검증이 아니다.

## 1. [P1] timeout 후 reap 중 들어온 취소를 전파할 것

대상: `apps/cardrag-worker/src/cardrag_worker/providers.py`, `PaddleOCRVLProvider.recognize_document`, 현재 1414–1427행.

현재 `except (TimeoutError, asyncio.CancelledError)`는 child kill 후 shield한 `process.wait()`가 끝날 때까지 새 `CancelledError`를 잡아 무시한다. 처음 예외가 TimeoutError였고 **reap 중 최초 Task.cancel()이 들어온 경우**에도 마지막 bare `raise`는 원래 TimeoutError를 재발행한다. 취소 요청이 호출자에게 도달하지 않는다.

재현 순서:

1. `communicate()`가 대기하고 `timeout_seconds=0.01`인 fake Process를 사용한다.
2. timeout으로 `kill()`이 호출되고 `wait()`가 시작됐다는 Event를 기다린다. wait는 별도 Event가 풀릴 때까지 대기한다.
3. 이때 provider Task에 `cancel()`을 호출하고 event loop를 한 번 진행한다.
4. wait Event를 풀고 provider Task의 결과를 확인한다.

실제 결과:

```text
timeout_then_cancel: TimeoutError
task_cancelled=False
pending_cancel_count=1
alias_remaining=False
```

alias는 정리됐지만 취소 의미가 사라졌다. 이 오류는 CLI의 `_operation_with_signal_shutdown`이 SIGTERM/SIGINT를 한 번만 Task.cancel()로 전달하고 추가 signal을 병합하는 계약과 충돌한다. caller가 TimeoutError를 문서 오류·재시도로 처리하면 종료 요청 뒤 후속 처리를 계속할 수 있고, shutdown 결과 대신 일반 실패/성공 경로로 끝날 수 있다.

필요한 수정:

- child 종료·reap 완료 전에 alias나 Worker lock을 해제하지 않는 fence는 유지한다.
- 처음 발생한 예외와 drain 도중 새로 관측한 취소를 구분해 기록한다. drain 중 취소가 한 번이라도 들어왔다면 reap·정리 뒤 취소를 전파한다. 취소가 없으면 원래 TimeoutError를 유지한다.
- 취소의 예외 context에 원시 provider 출력·예외가 노출되지 않게 기존 취소 진단 계약을 유지한다. timeout을 성공으로 바꾸거나 취소 횟수를 임의로 지워 계속 실행하지 않는다.
- 기존 timeout 없이 시작된 cancellation, repeated cancellation, `ProcessLookupError`, 실제 child 종료와 alias 삭제 순서를 보존한다.

인수 테스트:

1. timeout → reap 시작 → **첫 취소** → 추가 취소 → reap 완료 순서를 Event로 제어한다. reap 전 alias 존재, Task 미완료, 완료 후 alias 소멸 및 `CancelledError` 전파를 확인한다.
2. 같은 timeout 경로에서 취소가 없으면 여전히 TimeoutError다.
3. 기존 실제 subprocess timeout/cancel/실패·재시도 테스트와 signal wrapper/취소 회귀를 유지한다. 가능하면 signal wrapper가 이 시나리오를 일반 terminal payload로 취급하지 않음을 외부 signal 없이 제어 가능한 테스트로 연결한다.

## 2. [P2] FIFO worker.lock을 검사 전에 blocking open하지 말 것

대상: `apps/cardrag-worker/src/cardrag_worker/paddle_cleanup.py`, `recover_paddle_aliases`, 현재 61–65행.

현재 `os.open('worker.lock', O_RDONLY | O_NOFOLLOW | O_CLOEXEC)` 다음에야 fstat으로 regular file을 확인한다. lock 항목이 FIFO이고 writer가 없으면 **open 자체가 무기한 대기**하므로 `paddle_cleanup_unsafe_lock`까지 도달하지 않는다. 복구 명령이 손상된 상태를 진단하려다 멈춘다. `LOCK_NB`는 이후 flock에만 적용되므로 방지하지 못한다.

재현 순서:

1. 임시 state 디렉터리를 만들고 `os.mkfifo(state / 'worker.lock')`를 실행한다. writer는 열지 않는다.
2. 별도 subprocess에서 `recover_paddle_aliases(state, 'a' * 32)`를 호출한다. 이 경우 run 파일조차 필요하지 않다.
3. 부모가 1초 deadline으로 child를 기다린다.

실제 결과:

```text
fifo_lock: BLOCKED for 1s before safety validation
```

재현 fixture subprocess는 deadline 후 kill·wait하여 회수했다. 운영 lock은 변경하지 않았다.

필요한 수정:

- descriptor 기반 no-follow open과 기존 lock inode/regular/nlink 확인을 유지하면서, 잘못된 FIFO 등 특수 항목을 blocking 없이 거부한다. 예를 들어 open에 `O_NONBLOCK`을 포함하고 fstat으로 regular를 확인한 뒤 기존 flock을 수행할 수 있다. lstat 선행만 추가해 교체 경합을 다시 만들지 않는다.
- dry-run/apply 모두에서 적용하고 missing/symlink/directory/hardlink/FIFO lock은 명확한 bounded nonzero 결과를 낸다. 잘못된 lock을 새 regular file로 교체하거나 생성하지 않는다.
- 실제 regular Worker lock과의 기존 flock 경합은 계속 `worker_busy`로 즉시 실패해야 한다.

인수 테스트:

1. FIFO lock은 별도 subprocess에서 짧은 deadline 안에 safe error로 종료하고 파일 변경 0개다. 테스트 실패 시 child를 반드시 회수해 테스트 자체가 멈추지 않게 한다.
2. dry-run/apply의 missing/symlink/directory/hardlink lock 거부와 regular lock의 사용 중/정상 상태를 검사한다. 외부 sentinel·기존 alias·lock 항목 자체를 보존한다.

## 수정 범위·검증·보고

두 수정은 child 자원 정리와 복구 도구의 경계에 한정한다. OCR 모델·fallback 허용 정책·cache identity·capacity 허용 여부·DB/순회·timer·운영 상태를 바꾸지 않는다. 본 FIX만으로 운영 배포나 실제 링크 삭제를 시작하지 않는다.

우선 신규 재현 테스트가 수정 전 실패하는지 확인하고, 구현 후 전용 테스트와 관련 CLI/provider/capacity/취소 테스트를 실행한다. 이어 PLAN의 Worker/Core/runtime 관련 전체 회귀, 변경 파일의 ruff·format/type 검사와 diff 검사를 수행한다. 검사 명령과 실제 결과, 변경 파일, 취소와 lock 경계의 의미, 남은 운영 미검증 사항을 **새 `FIX_01_REPORT.md`**에 기록한다. PLAN/REPORT/본 FIX를 덮어쓰지 않는다. Reviewer가 현재 diff와 새 보고서를 다시 검토한 뒤 코드 인수 여부를 결정한다.
