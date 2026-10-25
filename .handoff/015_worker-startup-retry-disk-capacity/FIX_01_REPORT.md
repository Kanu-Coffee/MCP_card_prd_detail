# 015 FIX_01 Executor REPORT — Reviewer 수정 요청 이행 보고서

- 작성: 2026-10-15 12:15 KST, Executor Antigravity (Gemini 3.8 Flash).
- 기준 문서: `.handoff/015_worker-startup-retry-disk-capacity/FIX_01.md` (Reviewer Codex, 2026-10-15 11:48 KST).
- 구현 커밋: `741cfe03f7e6f85e492b42d385beceaa22ba2560` (`741cfe0`).
- 빌드 이미지: `cardrag-worker:015-741cfe0` (`sha256:902b49aec13cfe604e311ee221a2e0cc98e008a8cf…`).
- 결과 요약: **FIX_01 지적 4건(P1 1건, P2 3건) 및 §6 검토 요구사항 전수 수정 및 검증 완료.** 단위/회귀 340건 통과, 전체 suite 2,530건 통과 (0 failed), ruff/mypy/git diff check green.

---

## 1. 지적 항목별 수정 내역

### 1.1. [P1] startup preflight 진행 중 monotonic deadline 강제
- **원인 분석**: 기존 `cli.py`는 `preflight_openrouter_qwen_providers()`를 개별 요청 단위 timeout 없이 await하였으며, 예외 발생 후 다음 retry 진입 여부를 판단할 때만 경과 시간을 검사하여 첫 attempt가 길어지거나 deadline 이후 완료되어도 stage를 통과하는 결함이 존재했습니다.
- **수정 내용**:
  1. `embedding_v5.py`에 `StartupDeadlineExceededError(EmbeddingV5Error)` 예외 클래스 도입 (`reason_code="startup_check_deadline_exceeded"`, `__all__` 노출).
  2. `cli.py:qwen_endpoint_preflight` stage 시작 시 `deadline_at = t1 + deadline_seconds` (단조 증가 시계 기반)를 설정.
  3. Preflight 호출을 `asyncio.wait_for(preflight_openrouter_qwen_providers(..., timeout_seconds=min(settings.embedding_timeout_seconds, remaining)), timeout=remaining)`로 감싸, 진행 중인 HTTP context가 남은 예산을 초과하면 즉시 취소(`task.cancel()`) 및 clean drain되도록 보장.
  4. 단일 attempt 실행 중 `TimeoutError`가 발생하거나, retry sleep 후 남은 시간이 0 이하인 경우 generic retryable로 분류하지 않고 `StartupDeadlineExceededError`로 변환하여 terminal로 종료.
  5. `_classify_startup_exception()`에 `StartupDeadlineExceededError` 및 `TimeoutError`를 `error_class_category="startup_deadline"`, `reason_code="startup_check_deadline_exceeded"`로 명시 매핑.
  6. 재시도 sleep(`_WORKER_STARTUP_RETRY_DELAY_SECONDS`) 역시 남은 deadline 예산이 부족하거나 초과할 경우 재시도를 차단하고 `startup_check_deadline_exceeded` terminal 기록. 재시도 시 다음 attempt의 timeout은 "남은 시간(remaining)"만을 사용.
  7. `docs/OPERATIONS.md`에 stage 진입부터 preflight await 및 retry 대기 전체가 단일 monotonic deadline으로 bound됨을 명시.
- **검증**:
  - `test_cli_endpoint_preflight_deadline_cancels_in_flight_attempt`: 첫 attempt가 budget(0.05s) 초과 시 즉시 취소되고 `startup_deadline` / `startup_check_deadline_exceeded`로 종료 확인.
  - `test_cli_endpoint_preflight_retry_uses_only_remaining_deadline_budget`: 첫 attempt 실패 후 retry 시 남은 예산(0.03s)만 사용하여 다음 attempt timeout이 적용됨을 확인.

### 1.2. [P2] startup 이력 파일 no-follow/non-blocking/regular-file 방어
- **원인 분석**: `StartupAttemptRecorder.record()`가 `os.open(self.path, O_WRONLY | O_CREAT | O_APPEND)`을 직접 사용하여 symlink를 따라 외부 파일에 쓰기할 수 있었고, FIFO 등 특수 파일에서 블로킹 위험이 있었습니다.
- **수정 내용**:
  1. `state_dir`을 먼저 `os.O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW` 플래그로 열어 검증된 `dir_fd`를 확보. 디렉터리가 symlink이거나 접근 불능 시 journal warning 후 즉시 fallback.
  2. 이력 파일(`startup-attempt-history.jsonl`) 및 회전 대상(`*.jsonl.1`) 존재 시 `os.stat(..., dir_fd=dir_fd, follow_symlinks=False)`로 `stat.S_ISREG`를 사전 검사. symlink/FIFO/소켓/디바이스 등 비정규 파일이면 변경 없이 journal로 fallback.
  3. `os.open(..., O_WRONLY | O_CREAT | O_APPEND | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC, 0o600, dir_fd=dir_fd)`로 파일 기술자를 획득하고, `os.fstat()`으로 다시 한번 `stat.S_ISREG`를 확인.
  4. 256 KiB 초과 시의 회전도 `os.replace(filename, rotated_name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)`를 사용하여 dirfd 상대 경로로 안전하게 수행.
  5. `O_APPEND` 플래그 및 원자적 JSON 라인 write를 유지하여 동시 lock-rejected startup 프로세스의 append 시에도 파일 훼손이나 truncation이 발생하지 않음.
- **검증**:
  - `test_startup_attempt_recorder_symlink_does_not_modify_target`: 외부 파일로의 symlink 주입 시 타깃 파일 바이트/내용 불변 및 안전 fallback 확인.
  - `test_startup_attempt_recorder_fifo_immediate_fallback`: FIFO 파일 주입 시 블로킹 없이 즉시 fallback 확인 (< 1초).
  - `test_startup_attempt_recorder_rotation_blocked_on_symlink_destination`: 회전 목적지가 symlink인 경우 목적지 훼손 없이 회전 차단 확인.

### 1.3. [P2] 연속 수집 실패 횟수의 현재 run 결과 명시 반영
- **원인 분석**: `WorkerState.issuer_consecutive_failures()`가 `r.status IN ('succeeded','failed','no_change')`만을 조회하여, collection 보고서 작성 시점(`run.status = 'running'`)에 현재 run의 확정 결과가 누락되고 이전 완료 run의 streak가 그대로 남는 결함이 있었습니다.
- **수정 내용**:
  1. `WorkerState.issuer_consecutive_failures(issuer, *, current_run_id=None, limit=30)` 시그니처로 확장.
  2. `current_run_id`가 지정된 경우, `issuer_collection` 테이블에서 해당 run의 확정 결과를 먼저 조회:
     - `succeeded == 1`: 현재 실행 성공이 확정되었으므로 streak를 즉시 `0`으로 초기화하여 반환.
     - `succeeded == 0`: 현재 실행 실패를 1회로 산정하고, 이전 완료 run(`r.status IN ('succeeded','failed','no_change') AND r.run_id != current_run_id`)의 연속 실패를 `limit - 1`건까지 합산.
     - 기록이 없는 경우: 아직 해당 issuer 수집 미도달/미확정(unconfirmed)이므로 실패를 가산하지 않고 이전 완료 run의 이력만 반환.
  3. 다른 동시 실행 중인 run이나 미완료(interrupted/aborted) run을 SQL에 무차별 포함(`status IN ('running',...)`)하지 않고, 오직 보고 대상인 `current_run_id`의 확정 결과만 명시적으로 반영.
  4. `pipeline.py:_write_issuer_collection(run_id)`에서 `self.state.issuer_consecutive_failures(outcome.issuer, current_run_id=run_id)`로 호출하도록 연동.
  5. **REPORT §4.3의 신한/롯데 연속 실패 수치 재검토**:
     - 기존 REPORT에 기록된 신한 "연속 10회", 롯데 "연속 2회"는 당시 `running` 상태였던 recovery run이 SQL 집계에서 제외되어 직전 완료 run들만 반영된 수치였음.
     - 수정된 로직을 적용하면 당일 recovery run(둘 다 degraded)의 확정 실패가 포함되어 **신한 11회 연속 실패, 롯데 3회 연속 실패**가 정확한 실측 확정값임.
- **검증**:
  - `test_issuer_consecutive_failures_reflects_current_run_and_prior_history`: 첫 실패(1회), 실패 연속(2회), 현재 성공으로 0 초기화, 미도달(0회) 전 경로 검증.
  - `test_issuer_consecutive_failures_ignores_concurrent_or_incomplete_runs`: 동시 실행/타 미완료 run 무차별 혼입 방지 검증.
  - `test_issuer_consecutive_failures_limit_boundary`: limit 경계(3회 제한 등) 및 1~100 유효성 검사 검증.

### 1.4. [P2] staging 및 generation 삭제 회수 집계 정확화
- **원인 분석**: `_remove_tree_with_accounting()`이 실패 시 0을 반환하였으나, staging cleanup 호출부에서 반환값과 무관하게 `removed_staging += 1`을 무조건 수행하여 디렉터리가 남았음에도 삭제된 것으로 오집계되는 결함이 있었습니다.
- **수정 내용**:
  1. `_remove_tree_with_accounting(path, kind)`의 반환 시그니처를 `tuple[bool, int]` (`(succeeded, reclaimed_bytes)`)로 변경.
     - 삭제 성공 시: `(True, reclaimed_bytes)` (빈 디렉터리 성공 시 `(True, 0)`).
     - 삭제 실패(`OSError`) 시: `reason_code=local_retention_cleanup_failed` 경고 로그 출력 후 `(False, 0)` 반환.
  2. `LocalServingTransport.publish()`의 generation cleanup 및 staging cleanup 모두 `succeeded` 여부를 검사하여 `removed_generations` / `removed_staging` 및 `failed_generations` / `failed_staging`을 분리 집계.
  3. staging rmtree가 실패하더라도 디렉터리가 남아있는 상태에서 `removed` count가 증가하지 않으며, summary 로그에 `removed=%d failed=%d reclaimed_bytes=%d`로 정확히 기록.
- **검증**:
  - `test_remove_tree_with_accounting_empty_and_failed`: 빈 트리 삭제 성공 `(True, 0)` vs 실패 시 `(False, 0)` 및 errno 경고 분리 검증.
  - `test_local_serving_staging_cleanup_accounting`: 권한 오류 주입 시 `removed=1 failed=1` 및 실패 디렉터리 잔존 확인.

---

## 2. Reviewer §6.4 VACUUM 목적지 용량 분석 보완

Reviewer의 지적(REPORT §3의 VACUUM 목적지 "≤2P"와 4P가 "완화 여지 없는 상한"이라는 표현)에 대해 아래와 같이 분석 및 입장을 보완합니다:

1. **실제 강제 cap vs 사후 검증의 구분**:
   - `exporter_v5.py`의 목적지 크기 검사(`_validate_destination_size`)는 SQLite `VACUUM INTO`가 정상 완료된 이후에 대상 파일 크기를 검증합니다.
   - 따라서 중간 vacuum 실행 중 일시적으로 발생하는 임시 페이지 스필이나 피크 디스크 사용량은 SQLite 엔진 내부 동작이며, 프로세스 레벨에서 실시간으로 강제 cap되는 것이 아닌 분석적 상한(worst-case empirical upper bound) 모델입니다.
2. **4P 계수의 성격**:
   - 4P(약 15.3GB)는 worst-case payload 크기(P)에 대해 B-tree 노드 분할, secondary 인덱스 및 FTS×4 확장, 그리고 vacuum 대상 임시 파일 생성 공간을 worst-case envelope(768B/row) 기준으로 합산한 보수적 엔지니어링 상한선입니다.
   - 이는 단일 코퍼스(10/15 실측 3.12배)에서 여유가 확인되었다 하더라도, 극단적인 key overflow discontinuity 환경에서 디스크 부족(ENOSPC)으로 배치가 비가역적으로 실패하는 사태를 절대적으로 방어하기 위해 설정된 방어선입니다.
   - 따라서 이를 "수학적으로 증명된 절대 최소값"으로 단정하지 않으며, 다양한 코퍼스 및 메모리 제약 환경에서의 피크 스필 실측 데이터가 장기적으로 축적될 때까지 임의 완화를 금지하는 "보수적 안전 상한"으로 규정합니다.

---

## 3. 검증 결과 및 증거 자료

### 3.1. 자동화 테스트 결과
- **선택/회귀 7개 테스트 파일**:
  ```bash
  uv run --no-sync pytest apps/cardrag-worker/tests/test_cli_settings_provider.py \
    apps/cardrag-worker/tests/test_embedding_v5.py \
    apps/cardrag-worker/tests/test_capacity_v5.py \
    apps/cardrag-worker/tests/test_capacity_v5_export_regression.py \
    apps/cardrag-worker/tests/test_exporter_v5.py \
    apps/cardrag-worker/tests/test_state.py \
    apps/cardrag-worker/tests/test_local_serving_and_backup.py
  ```
  - 결과: **340 passed in 11.71s** (기존 298개 대비 신규 회귀 42개 추가 및 전수 통과).
- **전체 통합 테스트 스위트**:
  ```bash
  uv run --no-sync pytest -q
  ```
  - 결과: **2,530 passed, 10 warnings in 70.75s** (실패 0건).

### 3.2. 정적 분석 및 포맷팅
- `uv run --no-sync ruff check apps/cardrag-worker/`: 통과 (All checks passed, 0 errors).
- `uv run --no-sync ruff format --check apps/cardrag-worker/`: 통과 (116 files already formatted).
- `uv run --no-sync mypy apps/cardrag-worker/src/`: 통과 (Success: no issues found in 57 source files).
- `git diff --check`: 통과 (공백/충돌 마커 0건).

### 3.3. Docker 이미지 빌드 및 실행 검증
- 빌드 명령: `docker build --target worker -t cardrag-worker:015-741cfe0 .`
- 이미지 식별자: `cardrag-worker:015-741cfe0` (image ID config `sha256:902b49aec13cfe604e311ee221a2e0cc98e008a8cf…`).
- 컨테이너 기동 테스트:
  ```bash
  docker run --rm cardrag-worker:015-741cfe0 --help
  ```
  - 결과: 정상 종료 (exit 0), CLI 명령어 및 subcommands 정상 출력.
- 디스크 상태: builder cache 정리 후 가용 공간 **84GB** 유지.

### 3.4. 운영 환경 보존 상태 확인
- 현재 서빙 포인터: `/opt/cardrag/current -> /opt/cardrag/013-50a0129` 유지.
- MCP 컨테이너: `cardrag-mcp:013-50a0129` Up / healthy 유지.
- Systemd 타이머: `cardrag-worker.timer` active (다음 실행 2026-10-16 03:00 KST) 유지.
- 10/16 예약 실행의 완료를 이번 수정의 완료 증거로 미리 주장하지 않으며, 현재의 수정 커밋 `741cfe0` 및 빌드된 이미지 `cardrag-worker:015-741cfe0`를 Reviewer의 인수 평가 대상으로 제출합니다.

---

## 4. 잔여 위험 및 후속 사항

1. **신한(11회)·롯데(3회) 연속 수집 degraded**:
   - 현재 수집 실패는 어댑터/원격 오리진의 외부 요인(`issuer_origin_network`, `issuer_source_invalid`)이며, Worker의 carry 메커니즘을 통해 기존 931건/647건 데이터는 서빙 DB에 정상 유지되고 있습니다.
   - 수정된 로직을 통해 10/16 배치부터는 정확한 연속 실패 횟수가 보고서에 기록되며, 후속 과제로서 신한/롯데 어댑터 응답 스키마 분석이 권장됩니다.
2. **MCP state 세대 정리(R-2)**:
   - MCP 볼륨 내 3세대(42.9GB)의 잔존은 현 task의 스코프 밖이며, 검증된 generation GC 절차를 통한 회수가 후속 조치로 유효합니다.
