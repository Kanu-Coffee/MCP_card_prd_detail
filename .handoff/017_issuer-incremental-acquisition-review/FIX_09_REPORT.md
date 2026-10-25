# FIX_09_REPORT — publication-only 재개의 ZIP 오류 증거 게시 전 영속화 완료 보고

작성일시: 2026-10-19
작성자: Executor Antigravity
대상: `.handoff/017_issuer-incremental-acquisition-review/FIX_09.md` 요구사항

---

## 1. 개요 및 요약

FIX_09에서 제기된 P2 결함(A1: 성공 후 결과 파일(`execution-result.json`)에 의존하지 않고 첫 remote 게시 전에 오류 증거를 영속화)을 해결하고 전체 검증을 완료했습니다.

### 핵심 문제 분석
1. **완료 직전 중단 시 결과 파일 부재**: process 실행 중 remote pointer 게시 완료 후 `finish_run(status=succeeded)` 단계 직전에 프로세스가 중단되면, `execution-result.json`이 아직 기록되지 않은 상태가 됩니다. 또한 collect 영수증(`acquisition.json`)은 별도의 collect run 디렉터리에 위치하므로, process run 디렉터리만을 조회하는 publication-only 재개(`SealedPublicationResumer`)는 오류 수를 0으로 초기화해 `complete`로 오보고했습니다.
2. **일반 all-stage run의 결과 파일 부재**: 단일 실행(all-stage run)은 `execution_plan`이 없어 `execution-result.json` 자체를 생성하지 않으며 `acquisition.json`도 쓰지 않습니다. 따라서 이 run의 seal을 publication-only로 재개하면 오류 수가 0으로 유실되었습니다.

### 해결 방식
1. **첫 remote 게시 전 seal(`sealed/publish.json`)에 오류 증거 영속화**:
   - `pipeline.py:_build_v5_generation`에서 seal envelope(`sealed`)를 직렬화할 때, `unsupported_attachment_count`, `collection_status`, 및 각 source별 `unsupported_attachments` 목록을 포함하도록 수정했습니다.
   - 이 seal은 `_atomic_write`를 통해 remote WebDAV 호출(`_publish_sealed`) **직전**에 로컬 CAS 및 DB와 원자적으로 고정됩니다.
2. **`_publish_sealed` 및 재개 경로에서 seal의 오류 증거 직접 복원**:
   - `_publish_sealed` 진입 시 `sealed` 내의 `unsupported_attachment_count`를 즉시 복원합니다.
   - `SealedPublicationResumer._resume_publication_locked` 및 `WorkerPipeline._run_locked`(retained seal 재개)에서도 `sealed`에 기록된 오류 카운트를 우선 복원합니다.
3. **구형 seal 호환성을 위한 정확한 계획 계승 fallback**:
   - `sealed`에 해당 필드가 없는 이전 버전 seal의 경우, `execution-plan.json`에 영속화된 `source_run_id`(collect run)를 추적하여 해당 run의 `acquisition.json` 및 `execution-result.json`을 안전하게 참조하도록 계승 경로를 강화했습니다.
4. **`partial_execution.py:replay_pdf_source` 연동**:
   - `AcquisitionSource` 또는 `ReuseSource.seal`로부터 source별 첨부 오류 목록(`_unsupported_attachments`)을 파이프라인에 주입하여 신규 seal에 완전한 source별 오류 이력이 보존되도록 연계했습니다.

---

## 2. 변경 파일 상세

1. `apps/cardrag-worker/src/cardrag_worker/pipeline.py`:
   - `WorkerPipeline`: `_unsupported_attachments: tuple[Any, ...] = ()` 속성 선언 및 초기화.
   - `pdf_acquisition`: `unsupported_attachments` 수집 시 `self._unsupported_attachments`에 튜플 저장.
   - `_build_v5_generation`: seal 생성 시 `unsupported_attachment_count`, `collection_status`, `unsupported_attachments`를 직렬화하여 `publish.json`에 기록.
   - `_publish_sealed`: `sealed` 맵에 `unsupported_attachment_count`가 존재하면 `self._unsupported_attachment_count`로 복원.
   - `_run_locked` (retained seal): `sealed`에서 카운트 우선 복원, 부재 시 `execution-plan.json`의 `source_run_id`를 추적해 `acquisition.json` 확인.
   - `SealedPublicationResumer._resume_publication_locked`: `sealed`에서 카운트 우선 복원, 부재 시 `execution-plan.json`의 `source_run_id`를 추적해 `acquisition.json` 확인.
2. `apps/cardrag-worker/src/cardrag_worker/partial_execution.py`:
   - `replay_pdf_source`: `source`(`AcquisitionSource` 또는 `ReuseSource.seal`)로부터 `pipeline._unsupported_attachments`를 주입하여 process run의 seal 생성 시 누락되지 않도록 연계.
3. `apps/cardrag-worker/tests/test_017_scope.py`:
   - `test_publication_only_resume_before_execution_result_written`: `finish_run(succeeded)` 직전에 `ProcessExit`을 주입하여 `execution-result.json`이 없는 실제 충돌 상태를 생성하고, `SealedPublicationResumer`가 `unsupported_attachment_count=1`, `collection_status="degraded"`를 정상 복원함을 검증.
   - `test_normal_run_publication_resume_keeps_zip_error`: 결과 파일이 생성되지 않는 일반 all-stage run의 seal을 `SealedPublicationResumer`로 재개 시 `count=1`, `collection_status="degraded"`가 보존됨을 검증.

---

## 3. 검증 증거

### 3.1 Reviewer 재현 스크립트 검증
```bash
PYTHONPATH=apps/cardrag-worker/tests uv run --no-sync pytest -c apps/cardrag-worker/pyproject.toml /tmp/test_zip_crash_review.py -q --tb=short
```
- 결과: **2 passed in 1.80s**

### 3.2 FIX_08 재현 스크립트 재검증
```bash
PYTHONPATH=apps/cardrag-worker/tests uv run --no-sync pytest -c apps/cardrag-worker/pyproject.toml /tmp/test_zip_second_review.py -q --tb=short
```
- 결과: **3 passed in 1.88s**

### 3.3 정식 범위 테스트 스위트 (`test_017_scope.py`)
```bash
uv run --no-sync pytest apps/cardrag-worker/tests/test_017_scope.py -q --tb=short
```
- 결과: **57 passed in 9.31s**

### 3.4 전체 Worker 테스트 스위트
```bash
uv run --no-sync pytest apps/cardrag-worker/tests/ -q --tb=short
```
- 결과: **1,370 passed, 10 warnings in 51.92s** (기존 OCR fallback/native cache 경고 외 전건 통과)

### 3.5 정적 분석 및 포맷팅 검사
```bash
uv run --no-sync ruff check apps/cardrag-worker/
```
- 결과: **All checks passed!**

```bash
uv run --no-sync ruff format --check apps/cardrag-worker/
```
- 결과: **123 files already formatted.**

```bash
git diff --check
```
- 결과: **clean (공백 및 충돌 문제 없음)**

### 3.6 보안 검사
```bash
gitleaks detect --no-git -v --source apps/cardrag-worker/src/cardrag_worker/
```
- 결과: **no leaks found (4.55 MB 검사 완료)**

---

## 4. 잔여 위험 및 범위 경계

1. **ZIP 압축 해제 미수행 보장**: 어떠한 경우에도 ZIP 아카이브를 열거나 내부 PDF를 추출하지 않으며, `zipfile` 모듈 호출 금지 spy 검사가 상시 유지됩니다.
2. **원격 게시 무결성 유지**: generation manifest, serving DB, vector sidecar, CAS 오브젝트 및 remote pointer 교체 순서와 무결성 검증은 이전과 동일하게 보존됩니다.
3. **기존 Handoff 이력 보존**: 기존의 모든 handoff 문서(`PLAN.md`, `REPORT.md`, `FIX_01.md` ~ `FIX_09.md`, 이전 REPORT 파일 등)는 불변으로 보존되었습니다.
