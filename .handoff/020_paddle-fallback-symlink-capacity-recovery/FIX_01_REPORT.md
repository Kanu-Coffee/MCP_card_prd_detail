# 020 FIX_01 보고 — reap 중 취소 전파 및 FIFO lock 유한 거부

작성: 2026-10-27 KST Executor Antigravity.
Reviewer Codex의 `FIX_01.md` 지적 사항([P1] timeout reap 중 취소 누락, [P2] FIFO worker.lock blocking open)을 검토하고 수정을 완료했다. 기존 `PLAN.md`, `REPORT.md`, `FIX_01.md`는 수정하지 않고 보존했다.

---

## 1. 구현 내용

### 1.1 [P1] Timeout 후 reap 중 취소 보존 및 전파 (`providers.py`)

- **원인 분석**:
  - `PaddleOCRVLProvider.recognize_document`에서 `wait_for(process.communicate(), timeout=...)`가 `TimeoutError`를 발생시킨 후, child reap 루프(`while not reap.done(): try: await asyncio.shield(reap) except asyncio.CancelledError: continue`) 동안 들어온 `Task.cancel()`이 `CancelledError`로 잡힌 뒤 무시되고, 루프 종료 후 bare `raise`로 인해 원래의 `TimeoutError`가 재발행되던 문제.
  - 이로 인해 호출자(`_operation_with_signal_shutdown`)가 취소 요청(SIGTERM/SIGINT)을 인지하지 못하고 타임아웃 오류로 오인할 위험이 존재함.
- **수정 사항**:
  - 최초 발생 예외(`exc`)와 child drain 중 새로 수신한 취소(`cancelled_during_drain`)를 명시적으로 구분하여 추적.
  - reap 완료(`reap.result()`) 및 임시 결과 파일 정리(`result_path.unlink()`)가 끝날 때까지 child kill & reap fence를 온전히 유지.
  - 최초 예외가 `asyncio.CancelledError`인 경우 원래의 취소 예외를 그대로 재전파.
  - 최초 예외가 `TimeoutError`였더라도 drain 중 취소가 관측되었거나 pending cancel이 존재하는 경우 `raise asyncio.CancelledError() from None`을 발생시켜 취소를 호출자에게 전파.
  - `from None`을 사용하여 원시 provider 출력이나 타임아웃 세부사항이 예외 체인에 누출되지 않도록 진단 계약을 준수.
  - drain 중 취소가 없었던 순수 타임아웃 경로에서는 원래의 `TimeoutError`를 그대로 유지.

### 1.2 [P2] Non-blocking `worker.lock` 검증 및 FIFO 무기한 대기 방지 (`paddle_cleanup.py`)

- **원인 분석**:
  - `recover_paddle_aliases`에서 `os.open("worker.lock", O_RDONLY | O_NOFOLLOW | O_CLOEXEC)` 호출 시 `O_NONBLOCK` 플래그가 없어, `worker.lock`이 writer가 없는 FIFO(명명된 파이프)일 경우 open 단계에서 무기한 blocking되어 이후의 `stat.S_ISREG` 검사나 `fcntl.flock(..., LOCK_NB)`에 도달하지 못함.
- **수정 사항**:
  - `os.open` 플래그에 `os.O_NONBLOCK`을 추가(`os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK`).
  - `worker.lock` 열기 실패(`FileNotFoundError`, `OSError` 등) 시 즉시 `PaddleCleanupError("paddle_cleanup_unsafe_lock")`로 거부.
  - descriptor `fstat`을 통해 regular file이 아니거나(`stat.S_ISREG` 거짓: FIFO, 디렉터리, 특수 파일 등) hardlink(`st_nlink != 1`)인 경우 즉시 `PaddleCleanupError("paddle_cleanup_unsafe_lock")` 발생.
  - dry-run(`apply=False`) 및 apply(`apply=True`) 양쪽 모두에서 경로 검사 시작 전에 lock safety 검증 및 `fcntl.flock(LOCK_EX | LOCK_NB)`을 수행.
  - 잘못된 lock을 regular file로 자동 생성하거나 덮어쓰지 않으며, 거부 시 기존 alias, sentinel 파일, lock 항목 자체를 변경하지 않고 보존.
  - 정규 Worker lock과의 경합 시 기존과 동일하게 `AlreadyRunning`(`worker_busy`)으로 즉시 실패.

---

## 2. 인수 테스트 및 검증 결과

### 2.1 신규 추가 테스트 (`test_paddle_inputs_v130.py`)

1. `test_timeout_then_cancel_during_reap_propagates_cancellation`:
   - timeout 발생 → fake child reap 시작(Event 제어) → 첫 취소 호출 → 추가 취소 호출.
   - reap 완료 전: alias가 온전히 존재하고 provider Task가 완료되지 않음을 검증.
   - reap 완료 후: alias가 삭제되고 Task가 `asyncio.CancelledError`로 종료하며 `task.cancelled() is True`임을 확인.
2. `test_timeout_without_cancellation_preserves_timeout_error`:
   - timeout 발생 → reap 완료까지 취소 없음.
   - Task가 `TimeoutError`로 완료되며 `task.cancelled() is False`이고 alias가 삭제됨을 확인.
3. `test_signal_wrapper_handles_timeout_reap_cancellation_without_external_signal`:
   - `_operation_with_signal_shutdown`과 연동하여 timeout 후 reap 대기 중 loop 신호 콜백(SIGTERM)을 외부 OS 시그널 없이 직접 호출.
   - signal wrapper가 해당 시나리오를 일반 terminal payload로 취급하지 않고 `WorkerSignalShutdown(SIGTERM)`을 정확히 발생시킴을 검증.
4. `test_fifo_lock_rejected_in_subprocess_within_deadline`:
   - `worker.lock`을 `os.mkfifo`로 생성하고 별도 subprocess에서 dry-run 및 apply 모드로 `recover_paddle_aliases` 실행.
   - 2초 타임아웃 deadline 내에 exit code 42(`paddle_cleanup_unsafe_lock`)로 안전하게 종료함을 확인.
   - 실패 시에도 child 프로세스를 `try...finally`로 회수하여 테스트 행 방지.
   - FIFO, sentinel 파일, 레거시 alias가 전혀 변경되지 않음(변경 0개)을 확인.
5. `test_lock_validation_refuses_unsafe_locks_and_preserves_state`:
   - missing lock, symlink lock, directory lock, hardlink lock(nlink > 1), FIFO lock, busy regular lock, idle regular lock 각각에 대해 dry-run 및 apply 모드 검증.
   - 비정상 lock은 `paddle_cleanup_unsafe_lock`으로 거부, busy lock은 `AlreadyRunning`으로 거부.
   - 거부 시 sentinel, alias, lock 파일 보존 확인. idle regular lock에서만 dry-run(보고/유지) 및 apply(삭제)가 정상 동작함을 확인.

### 2.2 검증 명령 및 결과

1. **타겟 테스트**:
   ```bash
   uv run --no-sync pytest apps/cardrag-worker/tests/test_paddle_inputs_v130.py -v
   # 22 passed in 2.41s
   ```

2. **관련 CLI/Provider/Capacity 결합 테스트**:
   ```bash
   uv run --no-sync pytest apps/cardrag-worker/tests/test_paddle_inputs_v130.py apps/cardrag-worker/tests/test_cli_settings_provider.py apps/cardrag-worker/tests/test_capacity_v5.py apps/cardrag-worker/tests/test_capacity_v5_export_regression.py -q
   # 233 passed in 9.72s
   ```

3. **파이프라인 취소/종료 테스트**:
   ```bash
   uv run --no-sync pytest apps/cardrag-worker/tests/test_pipeline.py -k 'cancel or shutdown' -q
   # 9 passed, 124 deselected in 0.41s
   ```

4. **코드 품질 검사 (Ruff check & format)**:
   ```bash
   uv run --no-sync ruff check apps/cardrag-worker/ packages/cardrag-core/
   # All checks passed!

   uv run --no-sync ruff format --check apps/cardrag-worker/src/cardrag_worker/capacity_v5.py apps/cardrag-worker/src/cardrag_worker/cli.py apps/cardrag-worker/src/cardrag_worker/paddleocr_runner.py apps/cardrag-worker/src/cardrag_worker/pipeline.py apps/cardrag-worker/src/cardrag_worker/providers.py apps/cardrag-worker/src/cardrag_worker/paddle_cleanup.py apps/cardrag-worker/tests/test_cli_settings_provider.py apps/cardrag-worker/tests/test_paddle_inputs_v130.py
   # 8 files already formatted
   ```

5. **타입 검사 (Mypy)**:
   ```bash
   uv run --no-sync mypy apps/cardrag-worker/src/cardrag_worker/capacity_v5.py apps/cardrag-worker/src/cardrag_worker/cli.py apps/cardrag-worker/src/cardrag_worker/paddleocr_runner.py apps/cardrag-worker/src/cardrag_worker/pipeline.py apps/cardrag-worker/src/cardrag_worker/providers.py apps/cardrag-worker/src/cardrag_worker/paddle_cleanup.py
   # Success: no issues found in 6 source files
   ```

6. **전체 회귀 테스트**:
   ```bash
   uv run --no-sync pytest apps/cardrag-worker/tests/ packages/cardrag-core/tests/ tests/runtime_v1/ -q
   # 1970 passed, 11 warnings in 65.34s
   ```

7. **Git diff 검사**:
   ```bash
   git diff --check
   # 출력 없음 (통과)
   ```

---

## 3. 변경 파일 목록

- `apps/cardrag-worker/src/cardrag_worker/providers.py`: timeout 후 drain 중 수신된 cancel 추적 및 안전한 전파 (`CancelledError() from None`)
- `apps/cardrag-worker/src/cardrag_worker/paddle_cleanup.py`: `O_NONBLOCK` 포함 열기 및 `worker.lock`의 FIFO/특수 파일 유한 거부
- `apps/cardrag-worker/tests/test_paddle_inputs_v130.py`: FIX_01 인수 테스트 5종 추가 (총 22개 테스트)
- `.handoff/020_paddle-fallback-symlink-capacity-recovery/FIX_01_REPORT.md`: 본 보고서

---

## 4. 운영 미검증 사항 및 인계

- 본 작업은 코드 레벨의 수정 및 단위/통합/회귀 테스트 완료까지 수행하였으며, 실제 운영 배포, 운영 환경에서의 `--apply` 실행, 시스템 타이머 변경은 수행하지 않았다.
- Reviewer Codex는 본 보고서와 Git diff를 바탕으로 `FIX_01`의 수정 사항 및 인수 여부를 검토한다.
