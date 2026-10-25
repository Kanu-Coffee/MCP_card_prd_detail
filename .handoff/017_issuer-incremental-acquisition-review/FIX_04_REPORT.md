# FIX_04_REPORT — 게시 완료 후 process 재개의 자체 seal 복구 순서 해결 및 전체 검증

작성: 2026-10-19, Executor Antigravity. 대상: `.handoff/017_issuer-incremental-acquisition-review/FIX_04.md` 요구사항 이행.

---

## 1. 개요 및 핵심 수정 사항

Reviewer Codex의 `FIX_04.md` 지시에 따라 다음 핵심 결함(A1)을 해결하고, 독립 재현 검사를 회귀 스위트에 편입한 뒤 전체 테스트 및 무결성 검증을 완료하였습니다.

### A1 [P2] 이미 게시된 자체 seal 재개를 `preservation_source` 조회보다 먼저 처리

- **원인 분석**:
  - `WorkerPipeline._run_locked`의 `skip("pdf")` 블록에서 원격 포인터가 이미 해당 세대로 전환되었으나 로컬 완료 기록(`state.finish_run`) 전에 중단된 경우(pointer commit과 local acknowledgement 사이 중단),
  - 기존 코드는 자체 `retained_seal` 파일 존재 여부를 확인하기 **전에** `preservation_source(self)`를 먼저 호출하였습니다.
  - 원격 WebDAV의 `current` 포인터가 이미 이 재개 대상 `run_id`의 세대와 일치하고 비선택 issuer도 포함되어 있으므로, `preservation_source`는 원격 현재 세대를 읽고 해당 `run_id`를 `ReuseSource`로 열려고 시도했습니다.
  - 그러나 현재 재개 중인 run은 완료되지 않은 상태(`running` 또는 `interrupted`)이므로, `ReuseSource`의 상태 무결성 검증(`run_status == "succeeded"`)에 걸려 `skip_source_unavailable: stage=pdf` 예외가 발생했습니다.
  - 이로 인해 뒤쪽에 위치하던 `_publish_sealed`의 자체 동일 세대 원격 게시 reconciliation 분기에 도달하지 못하는 순서적 모순이 발생했습니다.

- **수정 내용** (`apps/cardrag-worker/src/cardrag_worker/pipeline.py:3533-3551`):
  - `self.execution_plan.skips("pdf")` 진입 즉시, 최우선으로 `retained_seal = self.state_dir / "runs" / run_id / "sealed/publish.json"` 존재 여부를 검사하도록 순서를 재배치하였습니다.
  - 자체 `retained_seal.is_file()`이 확인되면:
    1. 보존 issuer 계약 복원: `restore_preserved_adapters(self, sealed.get("manifest", {}).get("issuer_codes", ()))`
    2. 불변 계약 검증: `_verify_contract_sha256(canonical_json_bytes(sealed["manifest"]))`
    3. 즉시 `_publish_sealed(run_id, sealed)`로 진입하여 게시 reconciliation 수행.
  - `_publish_sealed` 내부의 기존 동일 세대 검증 로직(`dav.current.generation_id == local_manifest.generation_id`)에 의해:
    - 원격과 로컬 generation의 `corpus_sha256`, `contract_sha256`, `schema_version`, `serving_schema` 일치 여부를 대조.
    - 불필요한 원격 재업로드 및 프로바이더/네트워크 호출 없이 로컬 run 상태를 `succeeded`로 reconciliation 완결.
  - `preservation_source(self)`는 자체 `retained_seal`이 없는 최초 실행 또는 seal 이전 replay 경로에서만 호출되도록 격리되었습니다.
  - `ReuseSource`의 `succeeded` 상태 검증 완결 제약은 그대로 엄격하게 유지되었습니다.

---

## 2. 코드 변경 내역

| 파일 | 변경 요약 |
|---|---|
| `apps/cardrag-worker/src/cardrag_worker/pipeline.py` | `_run_locked`의 `skip("pdf")` 블록 진입부에서 `retained_seal.is_file()` 검사 및 `_publish_sealed` 호출을 `preservation_source(self)` 호출보다 선행하도록 순서 변경. |
| `apps/cardrag-worker/tests/test_017_scope.py` | Reviewer의 A1 회귀 테스트(`test_process_reduced_pool_resume_after_commit`, `registered: [False, True]` 2종)를 정식 테스트 스위트에 추가 및 ruff 포맷 적용. |

---

## 3. 검증 결과 및 증거

### 3.1 Reviewer A1 독립 재현 검사 (회귀 스위트 편입)

- Reviewer가 제시한 `/tmp/test_017_review_after_commit.py`의 2가지 매개변수(`registered=False`, `registered=True`)를 `apps/cardrag-worker/tests/test_017_scope.py`의 `test_process_reduced_pool_resume_after_commit`으로 정식 통합.
- 검증 결과:
  - `registered=False`: **PASSED** (추가 PDF 0, OCR 0, embedding 0, 전체 testbank/otherbank 보존 확인)
  - `registered=True`: **PASSED** (추가 PDF 0, OCR 0, embedding 0, 전체 testbank/otherbank 보존 확인)
- 임시 테스트 파일 `/tmp/test_017_review_after_commit.py` 정리 완료.

### 3.2 핵심 영역 단위/통합 테스트

```bash
uv run --no-sync pytest apps/cardrag-worker/tests/test_017_scope.py apps/cardrag-worker/tests/test_partial_execution.py -q --tb=short
```
- 결과: **75 passed in 23.73s**

### 3.3 Worker 전체 테스트 스위트

```bash
uv run --no-sync pytest apps/cardrag-worker/tests/ -q --tb=short
```
- 결과: **1,345 passed, 10 warnings in 48.99s**
- 경고 10건은 기존 OCR fallback/native cache 테스트에서 의도된 RuntimeWarning이며 신규 실패나 리그레션 없음.

### 3.4 린트, 코드 스타일, Git 및 시크릿 무결성

1. **Ruff 린트**:
   ```bash
   uv run --no-sync ruff check apps/cardrag-worker/
   ```
   - 결과: **All checks passed!**
2. **Ruff 포맷**:
   ```bash
   uv run --no-sync ruff format --check apps/cardrag-worker/
   ```
   - 결과: **123 files already formatted**
3. **Git diff 무결성**:
   ```bash
   git diff --check
   ```
   - 결과: 공백/trailing whitespace 오류 없음 (exit code 0).
4. **Gitleaks 비밀정보 스캔**:
   ```bash
   gitleaks detect --no-git -v --source apps/cardrag-worker/src/cardrag_worker/
   ```
   - 결과: **no leaks found** (~4.51 MB 스캔 완료).

---

## 4. 검증 범위 구분 및 잔여 한계

1. **실제 수행된 검증**:
   - Python 3.12 환경에서 Worker 전체 1,345개 테스트 100% 통과.
   - Mock WebDAV 및 SQLite 상태 기반의 exact committed seal 재개, seal 이전 중단 재개, 불일치 CLI 옵션 거부, 보존 issuer 계약 무결성 검증.
   - Ruff 린트/포맷 및 gitleaks 보안 검사 통과.
2. **미수행 검증 및 운영 한계**:
   - 실환경 8개사 라이브 네트워크 호출 및 실제 원격 WebDAV 서버 대상 포인터 원자적 교체.
   - 롯데카드 ZIP 공시 목록 파싱(DOCID 1893)을 위한 신규 ZIP 압축 해제 아키텍처는 이번 범위에 포함되지 않으며, complete collection 운영 인수는 계속 **보류(HOLD)** 상태를 유지합니다.
