# 013 FIX_04 — Executor 수행 및 검증 보고서

작성: 2026-10-09, Executor / Antigravity.  
대상 기준: Reviewer 지시 `013_local-serving-incremental-webdav-backup/FIX_04.md`.  
선행 근거: `FIX_03_REPORT.md`, `evidence/reviewer-fix03-independent.json`, `evidence/reviewer-fix03-default-restore.json`.

---

## 1. 개요 및 판정 요약

Reviewer가 `FIX_04.md`에서 지적한 두 건의 P1 기능 결함을 전수 해결하고 검증을 완수하였습니다:

1. **P1 — Worker 본처리 회귀 (`self.settings` 참조 AttributeError)**:
   - `WorkerPipeline.__init__`에 `settings: WorkerSettings | None = None`을 명시적으로 추가하고 `self.settings`로 보관.
   - CLI 실행부(`cli.py`, `partial_cli.py`)에서 `WorkerPipeline` 생성 시 `settings=settings`를 일관되게 주입하여 일반 run 및 resume 경로 모두 동일한 설정을 사용하도록 연결.
   - `pipeline.py`의 `recognize` 내에서 `self.settings is not None and getattr(self.settings, "backup_mode", "disabled") != "disabled"`로 안전하게 가드하고, 백업 intent 기록 실패 시 경고 로그(`LOGGER.warning`)로 격리하여 OCR 및 파이프라인 본처리가 `OCRSystemicFailureError`로 비정상 종료되지 않도록 보장.
2. **P1 — backup restore 기본 명령의 캐시 재사용 실패 (Target Dir 불일치)**:
   - `cli.py`의 `backup_restore_command` 기본 대상 경로를 `settings.state_dir / "cache" / "ocr"`(중첩 경로 오류)에서 **Worker state root**인 `settings.state_dir`로 일원화.
   - `--target-dir` CLI 옵션 도움말 및 `docs/RECOVERY.md`의 명령어 설명과 예시를 Worker state root(`/var/lib/cardrag-worker`)로 정정.
   - Typer CLI `backup restore`를 `--target-dir` 인자 없이 호출하는 신규 통합 테스트를 작성하여, 빈 Worker state root에서 복원 후 실제 `OCRResolver`가 `cache_reused == True`, provider 호출 0건(`len(provider.calls) == 0`)으로 캐시 적중함을 증명.

모든 정적 검사(mypy, ruff)와 단위·통합 테스트, 그리고 `cardrag-worker`의 전체 오프라인 테스트 스위트(1,255 passed in 42.65s)를 100% 통과하였습니다.

---

## 2. 결함 분석 및 구현 수정 내역

### 2.1 P1 — `self.settings` 참조 AttributeError 및 Worker 실행 회귀 해결

- **원인 분석**:
  - `WorkerPipeline.__init__`에 `settings` 속성이 없었음에도 `pipeline.py:4957`에서 `if getattr(self.settings, "backup_mode", "disabled") != "disabled":`를 호출하여 `self.settings` 속성 접근 시 `AttributeError`가 발생함.
  - 이로 인해 mypy 검사 실패(`WorkerPipeline has no attribute settings`) 및 일반 v5 파이프라인 fixture(`test_v5_pipeline_seals_publishes_resumes_and_reuses_profile_cache`)가 `OCRSystemicFailureError`로 실패함.
- **수정 위치 및 구현**:
  1. [`apps/cardrag-worker/src/cardrag_worker/pipeline.py`](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/src/cardrag_worker/pipeline.py):
     - `from .settings import WorkerSettings` 임포트.
     - `WorkerPipeline` 클래스에 `settings: WorkerSettings | None = None` 타입 애너테이션 추가.
     - `WorkerPipeline.__init__`에 `settings: WorkerSettings | None = None` 매개변수 추가 및 `self.settings = settings` 보관.
     - OCR 완료 직후 백업 인텐트 등록 블록을 안전하게 수정:
       ```python
       if self.settings is not None and getattr(self.settings, "backup_mode", "disabled") != "disabled":
           try:
               from .backup import BackupLedger

               b_ledger = BackupLedger(self.state_dir / "backup-ledger.sqlite3")
               b_ledger.record_document_ocr(
                   run_id, current_document_id, self.state_dir, self.settings
               )
           except Exception as b_exc:
               LOGGER.warning(
                   "Failed to record document OCR backup intent for %s: %s",
                   current_document_id,
                   b_exc,
               )
       ```
       - `self.settings is None`일 때 백업 원장 접근 0건 보장.
       - 백업 intent/ledger 실패 시 OCR 및 발행 본처리와 격리하여 경고로 기록.
  2. [`apps/cardrag-worker/src/cardrag_worker/cli.py`](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/src/cardrag_worker/cli.py):
     - `WorkerPipeline` 생성 호출부에 `settings=settings` 전달 (run 및 `--resume` 모두 적용).
  3. [`apps/cardrag-worker/src/cardrag_worker/partial_cli.py`](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/src/cardrag_worker/partial_cli.py):
     - `WorkerPipeline` 생성 호출부에 `settings=settings` 전달.

---

### 2.2 P1 — CLI backup restore 기본 경로 불일치 해결

- **원인 분석**:
  - `BackupLedger.restore`는 `target_dir`를 **Worker state root**로 인식하여 그 하위에 `ocr-seed/`, `cache/ocr/`, `audit-reports/state-seed/`를 복원함.
  - 그러나 `cli.py:1925`에서 `dest = Path(target_dir) if target_dir else settings.state_dir / "cache" / "ocr"`로 설정되어 있어, 기본 복원 실행 시 `settings.state_dir / "cache" / "ocr" / "cache" / "ocr"`로 중첩 생성되어 정상 Worker 기동 시 `OCRCacheMissError`가 발생함.
- **수정 위치 및 구현**:
  1. [`apps/cardrag-worker/src/cardrag_worker/cli.py`](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/src/cardrag_worker/cli.py):
     ```python
     @backup_app.command("restore")
     def backup_restore_command(
         target_dir: str | None = typer.Option(
             None,
             "--target-dir",
             help="Target Worker state root directory to restore OCR cache and seeds",
         ),
     ) -> None:
         settings = WorkerSettings.from_env(require_providers=False, require_webdav=False)
         dest = Path(target_dir) if target_dir else settings.state_dir
         ledger = BackupLedger(settings.state_dir / "backup-ledger.sqlite3")
         res = asyncio.run(ledger.restore(settings, target_dir=dest))
         _echo(res)
     ```
  2. [`docs/RECOVERY.md`](file:///home/lee/projects/MCP_card_prd_detail/docs/RECOVERY.md):
     - 251-253행의 안내를 정정:
       ```bash
       # 4. WebDAV로부터 백업된 OCR 캐시를 Worker state root 디렉터리로 복원 (기본값: $CARDRAG_STATE_DIR)
       cardrag-worker backup restore [--target-dir /var/lib/cardrag-worker]
       ```
     - `--target-dir`의 기본값이 Worker state root(`settings.state_dir`)임을 명시.

---

## 3. 신규 추가 및 보강된 테스트

[`apps/cardrag-worker/tests/test_local_serving_and_backup.py`](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/tests/test_local_serving_and_backup.py)에 2건의 핵심 통합 테스트를 추가:

1. `test_backup_cli_restore_default_target_dir_resolves_cache_hit`:
   - 원본 state에서 OCR 산출물 생성 후 `BackupLedger.flush`로 인메모리 WebDAV 스토리지에 백업.
   - 새 빈 디렉터리를 `CARDRAG_WORKER_STATE_DIR`로 설정하고 Typer `CliRunner`를 통해 `cardrag-worker backup restore`를 **`--target-dir` 없이** 실행.
   - 복원 완료 후 `cache/ocr/...`, `ocr-seed/...`가 Worker state root 하위에 정상 배치되었음을 확인.
   - 해당 디렉터리에서 `cache_mode="read-only"`, `_require_cache_hit=True`로 `OCRResolver.resolve` 호출 시:
     - `resolved.cache_reused is True`
     - `resolved.provider_called is False`
     - `len(fake_provider.calls) == 0`
     - **0 provider calls 캐시 적중** 확인.
2. `test_pipeline_backup_intent_recorded_and_isolated_on_error`:
   - **Case 1**: `backup_mode="immediate"` 활성화 상태에서 OCR 완료 후 후속 embedding 단계 실패를 유도. 파이프라인 예외 종료 후에도 `backup-ledger.sqlite3`에 pending intent(`pending_count > 0`) 및 `backup/spool` 파일이 영구 보존됨을 검증.
   - **Case 2**: `BackupLedger.record_document_ocr`가 예외를 발생시키더라도, 파이프라인이 `OCRSystemicFailureError`로 크래시되지 않고 경고 로그 기록 후 정상 완료(`result.published is True`)됨을 검증.

---

## 4. 검증 명령어 및 결과 증거

### 4.1 정적 검사 (Mypy)
```bash
$ uv run --no-sync --all-packages mypy apps/cardrag-worker/src/cardrag_worker/pipeline.py apps/cardrag-worker/src/cardrag_worker/cli.py apps/cardrag-worker/src/cardrag_worker/partial_cli.py
Success: no issues found in 3 source files
```

### 4.2 린트 및 코드 포맷 (Ruff)
```bash
$ uv run --no-sync ruff check apps/cardrag-worker/ apps/cardrag-mcp/
All checks passed!

$ uv run --no-sync ruff format --check apps/cardrag-worker/ apps/cardrag-mcp/
183 files already formatted
```

### 4.3 회귀 검증: v5 본처리 테스트
```bash
$ uv run --no-sync --all-packages pytest apps/cardrag-worker/tests/test_pipeline_v5.py::test_v5_pipeline_seals_publishes_resumes_and_reuses_profile_cache -q
.
1 passed in 1.47s
```

### 4.4 신규 및 관련 타깃 테스트 스위트
```bash
$ uv run --no-sync --all-packages pytest apps/cardrag-worker/tests/test_local_serving_and_backup.py apps/cardrag-mcp/tests/test_transport.py apps/cardrag-worker/tests/test_pipeline_v5.py -v
============================== 28 passed in 1.99s ==============================
```

### 4.5 부분 실행 테스트 스위트
```bash
$ uv run --no-sync --all-packages pytest apps/cardrag-worker/tests/test_partial_execution.py -q
42 passed in 17.10s
```

### 4.6 Worker 전체 오프라인 테스트 스위트
```bash
$ uv run --no-sync --all-packages pytest apps/cardrag-worker/tests -q
1255 passed, 9 warnings in 42.65s
```

---

## 5. 결론 및 인수 상태

- Reviewer가 `FIX_04.md`에서 요구한 두 건의 P1 결함(`self.settings` 누락 회귀 및 CLI 기본 복원 경로 불일치)이 완전히 해결되었습니다.
- 기존 FIX_03의 검증 결과(백업 2회 갱신, fresh host 복원, native cache hit, lost_source 처리, local serving 활성화) 및 증거 파일(`evidence/reviewer-fix03-independent.json`, `evidence/reviewer-fix03-default-restore.json`)은 그대로 보존되었습니다.
- 모든 단위 및 전체 오프라인 테스트가 성공하여 코드 인수 기준을 완벽히 만족합니다.
