# FIX_03_REPORT — process 게시 재개 계약 복원, 회귀 검증 및 사실관계 정정

작성: 2026-10-19, Executor Antigravity. 대상: `.handoff/017_issuer-incremental-acquisition-review/FIX_03.md` 요구사항 이행.

---

## 1. 개요 및 작업 요약

Reviewer Codex의 `FIX_03.md` 지시에 따라 다음 3가지 핵심 항목을 구현 및 검증 완료하였습니다.

1. **A1 [P2] 보존 issuer를 포함한 process의 seal 재개 전 계약 복원**
   - `apps/cardrag-worker/src/cardrag_worker/issuer_scope.py`: `restore_preserved_adapters(pipeline, issuer_codes)` 함수를 정의하여 `REGISTERED_ISSUERS` 등록 여부와 무관하게 `_PreservedDummyAdapter`(및 `IssuerSpec`) 또는 등록된 팩토리를 통해 pipeline의 adapter 풀을 복원하도록 구현.
   - `apps/cardrag-worker/src/cardrag_worker/pipeline.py`: `_run_locked` 진입 시 `self.execution_plan.skips("pdf")` 경로에서 `retained_seal`의 `contract_sha256` 검사 전에 `self._preserved_source` 및 `sealed["manifest"]["issuer_codes"]`를 참조하여 preserved adapter를 즉시 복원.
   - 결과: pointer 일시 장애 등으로 seal 생성 후 중단된 process를 재개할 때 `skip_artifact_incompatible: stage=export`로 거부되지 않고, 추가 네트워크/OCR/embedding 호출 0건으로 게시가 완결됨.

2. **B1 process CLI 재개 테스트의 실제 성공 및 publication 검증 보강**
   - `apps/cardrag-worker/tests/test_017_scope.py`:
     - `test_process_reduced_pool_resume_after_seal`: Reviewer의 A1 독립 재현 검사를 등록/미등록 발급사(`registered: [False, True]`) 매개변수화 테스트로 포함.
     - `test_process_explicit_resume_restores_inferred_scope`: 단순 plan mocking을 제거하고, 실제 baseline 발행 → 1개사 collect → process 실행 중 pointer failure 주입으로 seal 보존 실패 유도 후 CLI invoke를 통한 재개를 검증.
     - 다음 3가지 성공 케이스 모두 `exit_code == 0`, status `succeeded`, 그리고 전체 발급사(`{"testbank", "otherbank"}`) 보존 발행 완결을 검증:
       1) 최초 명령어 재실행: `--phase process --reuse-from-run <coll> --resume <proc>`
       2) bare resume: `--resume <proc>`
       3) 명시적 매칭 발급사 지정: `--phase process --reuse-from-run <coll> --resume <proc> --issuer <coll-issuer>`
     - 불일치 발급사, 불일치 소스 ID, 불일치 phase 지정 시 사전 거부(`exit_code != 0`) assertion 검증.
   - `apps/cardrag-worker/src/cardrag_worker/partial_cli.py`:
     - `LocalServingTransport` 및 mock transport 사용 시 `configure_verification`과 `close()` 호출 시 `hasattr` 가드를 적용하여 안전성 확보.

3. **B2 롯데 장애 근거 및 운영 인수 보류 상태 정정**
   - `FIX_02_REPORT.md`의 "ZIP 다운로드가 외부 endpoint/network HTTP 500이며 코드 블로커가 아니다"라는 부정확한 기술을 공식 철회하고, 확인된 사실관계를 아래와 같이 명확히 정정함.

---

## 2. B2 사실관계 정정 (Retraction & Correction)

`FIX_02_REPORT.md`에 기재되었던 롯데 수집 관련 서술을 다음과 같이 정정합니다.

1. **HTTP 500 진술 철회**:
   - `FIX_02_REPORT.md`에서 언급된 "ZIP 다운로드 시 외부 엔드포인트 HTTP 500 발생" 주장은 객관적 로그 근거가 없는 잘못된 기술이었으므로 이를 공식 철회합니다.
2. **확인된 기술적 사실관계**:
   - 롯데카드 공식 공시 목록 API는 정상적으로 HTTP 200을 반환하며, 공시 목록 데이터에 DOCID 1893(ZIP 포맷 압축 첨부파일)이 포함되어 있습니다.
   - 현재 구현된 PDF 전용 첨부파일 파서는 `.zip` 확장자를 수용하지 못하고 `Lotte disclosure attachment filename is invalid` 예외를 발생시키며 목록 파싱을 거부합니다.
   - 한편, 공시 목록 중 단일 PDF 파일 1건에 대한 엄격(strict) 다운로드 검증은 성공하였습니다.
3. **운영 인수 상태**:
   - 향후 ZIP 다중 파일 압축 해제, 파일 무결성 보존 및 파싱 아키텍처가 정립되기 전까지 롯데카드 전건 수집(complete collection)에 대한 운영 인수는 **보류(HOLD)** 상태를 유지합니다. (이번 작업 범위에서 ZIP 처리기 신규 구현을 포함하지 않음).

---

## 3. 코드 변경 내역

| 파일 | 변경 요약 |
|---|---|
| `apps/cardrag-worker/src/cardrag_worker/issuer_scope.py` | `restore_preserved_adapters(pipeline, issuer_codes)` 함수 추가. 미등록/등록 발급사 모두 `IssuerSpec`과 dummy/factory adapter를 복원. |
| `apps/cardrag-worker/src/cardrag_worker/pipeline.py` | `_run_locked` 진입부에서 `retained_seal` 호환성 검사(`contract_sha256`) 이전에 preserved adapter를 복원. |
| `apps/cardrag-worker/src/cardrag_worker/partial_cli.py` | `webdav.configure_verification` 및 `webdav.close()` 호출 시 `hasattr` 가드 추가. |
| `apps/cardrag-worker/src/cardrag_worker/cli.py` | CLI 옵션 및 scope 세션 복원 유지, 디버그 구문 정리, ruff 포맷팅 준수. |
| `apps/cardrag-worker/tests/test_017_scope.py` | A1 회귀 테스트(`test_process_reduced_pool_resume_after_seal`, unregistered/registered 2종) 및 B1 종합 CLI 재개 검증(`test_process_explicit_resume_restores_inferred_scope`, 3종 성공 및 거부 3종) 추가. |

---

## 4. 검증 결과 및 증거

### 4.1 신규 및 핵심 단위/회귀 테스트

```bash
uv run --no-sync pytest apps/cardrag-worker/tests/test_017_scope.py -v
```
- 결과: **30 passed in 5.88s**
- 상세:
  - `test_process_reduced_pool_resume_after_seal[False]`: PASSED
  - `test_process_reduced_pool_resume_after_seal[True]`: PASSED
  - `test_process_explicit_resume_restores_inferred_scope[original]`: PASSED
  - `test_process_explicit_resume_restores_inferred_scope[bare]`: PASSED
  - `test_process_explicit_resume_restores_inferred_scope[matching_issuer]`: PASSED

```bash
uv run --no-sync pytest apps/cardrag-worker/tests/test_partial_execution.py -q
```
- 결과: **43 passed in 18.12s**

### 4.2 전체 테스트 스위트 무결성 검증

```bash
uv run --no-sync pytest apps/cardrag-worker/tests/ -q --tb=short
```
- 결과: **1,343 passed, 10 warnings in 47.68s**
- 기존 경고 외 신규 경고 또는 실패 0건.

### 4.3 정적 분석, 포맷팅, Git 무결성 및 보안 검사

```bash
uv run --no-sync ruff check apps/cardrag-worker/
```
- 결과: **All checks passed!**

```bash
uv run --no-sync ruff format --check apps/cardrag-worker/
```
- 결과: **123 files already formatted**

```bash
git diff --check
```
- 결과: 공백/trailing whitespace 오류 없음 (exit code 0).

```bash
gitleaks detect --no-git -v --source apps/cardrag-worker/src/cardrag_worker/
```
- 결과: **no leaks found** (비밀키/토큰 노출 없음).

---

## 5. 검증 범위 구분 및 잔여 리스크

1. **실제 수행된 검증**:
   - Python 3.12 가상환경에서의 Worker 전체 테스트(1,343건).
   - Mock WebDAV 및 sqlite3 기반 pointer 실패/재개 시나리오, preserved adapter 복원, CLI runner 옵션별 분기 및 실제 DB 상태 확인.
   - Ruff 린트/포맷 및 gitleaks 시크릿 검사.
2. **미수행 검증 (Mock 한계)**:
   - 실제 상용 운영 환경(Docker Compose)에서의 8개 카드사 라이브 네트워크 호출.
   - 실제 원격 WebDAV 서버 대상 포인터 원자적 교체 및 롤백.
   - 롯데카드 ZIP 공시 아키텍처 구현 (별도 과제로 관리 필요).
