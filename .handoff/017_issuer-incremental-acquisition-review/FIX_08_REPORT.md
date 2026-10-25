# FIX_08_REPORT — ZIP 오류의 재개 계승·복구 유지·실제 재확인 준비 경로 완료 보고

작성일시: 2026-10-19
작성자: Executor Antigravity
대상: `.handoff/017_issuer-incremental-acquisition-review/FIX_08.md` 요구사항

---

## 1. 개요 및 요약

FIX_08에서 제기된 P2 결함 3건(A1, A2, A3)을 모두 해결하고 검증을 완료했습니다.
ZIP 파일을 직접 압축 해제하거나 내부를 열지 않는다는 사용자 방침과 공통 PDF 계약은 그대로 보존되었습니다.

- **A1 자체 seal 재개 및 publication-only 재개 상태 복원**:
  - `pipeline.py:2859-2868`: `execution-result.json` writer에 `unsupported_attachment_count`와 `collection_status` 직렬화를 추가하여 reader/writer 계약을 일치시켰습니다.
  - `pipeline.py:3565-3610`: retained seal 우선 분기 진입 시 `AcquisitionSource`, `acquisition.json`, `execution-result.json`으로부터 `self._unsupported_attachment_count`를 복원하도록 구현했습니다.
  - `pipeline.py:8520-8555`: `SealedPublicationResumer._resume_publication_locked`에서도 게시 직전 `acquisition.json` 및 `execution-result.json`을 검사하여 `self._unsupported_attachment_count`를 복원하도록 보완했습니다.
- **A2 복구된 PDF의 다음 배치 상태 유지**:
  - `pipeline.py:3948-3987`: `acquire_source`에서 명시 ZIP 조기 반환(`_is_explicit_zip`) 전에 `PDFSourceIdentity.from_source_record(source)`로 `self.pdf_cache.lookup`을 먼저 조회하도록 변경했습니다.
  - 이미 origin에서 strict PDF로 취득·검증되어 CAS에 바인딩된 문서(`cached_pdf is not None`)는 조기 ZIP 오류 반환을 우회하고 정상 PDF 취득 파이프라인(`acquire`)으로 진입합니다.
  - `acquire` 내에서는 이미 조회된 `cached_pdf`를 재활용하여 중복 조회를 방지하며, 감사 주기(`origin_policy.due`) 도래 전까지는 origin body 요청 0건으로 정상 cache hit을 반환(`unsupported_attachment_count=0`, `collection_status="complete"`)합니다.
  - 메타데이터 변경 또는 CAS 로컬 손상 시에는 캐시 lookup이 `None`을 반환하므로 명시 ZIP 오류로 안전하게 재분류됩니다.
- **A3 우리카드 ZIP 원본 재확인 prepare_download 허용**:
  - `issuers/woori.py:141-145`: `raonk_plaintext(file_path, file_name)`의 파일명 검증 가드를 확장하여 `.pdf`뿐만 아니라 `.zip`도 허용하도록 보완(`file_name.casefold().endswith((".pdf", ".zip"))`)했습니다.
  - 비허용 확장자(예: `.exe` 등)나 경로 탐색(`..`, 시작 슬래시 누락)은 계속해서 `ValueError`로 엄격히 차단됩니다.
  - 우리카드 adapter가 90일 만료 후 ZIP source의 재확인 단계에서 유효한 POST `DownloadRequest`를 생성할 수 있게 되었습니다.

---

## 2. 변경 파일 상세

1. `apps/cardrag-worker/src/cardrag_worker/issuers/woori.py`:
   - `raonk_plaintext`: `file_name.casefold().endswith((".pdf", ".zip"))`로 허용 확장자 보완.
2. `apps/cardrag-worker/src/cardrag_worker/pipeline.py`:
   - `execution-result.json` 기록 시 `unsupported_attachment_count`, `collection_status` 직렬화.
   - `_run_locked`의 retained seal 분기 및 `SealedPublicationResumer._resume_publication_locked`에서 `self._unsupported_attachment_count` 복원 로직 추가.
   - `acquire_source`: `_is_explicit_zip(source)` 검사 전에 `self.pdf_cache.lookup(cache_identity)`를 선행하여, 검증된 PDF 캐시가 있는 경우 조기 ZIP 오류를 발생시키지 않고 정상 PDF 흐름으로 이행.
   - `from .pdf_cache import PDFCacheHit` 추가 및 예외 디버그 로깅 정비.
3. `apps/cardrag-worker/tests/test_017_scope.py`:
   - `test_zip_error_survives_own_seal_resume`: Collect → Process → retained seal 재개 및 `SealedPublicationResumer`에서 `unsupported_attachment_count=1`, `collection_status="degraded"` 유지 검증.
   - `test_recovered_pdf_does_not_revert_to_zip_error`: 100일 후 정상 PDF 복구 → 3차 실행에서 오류 0 / status complete 유지 및 원본 호출 0건 확인.
   - `test_woori_zip_recheck_download_preparation`: 우리카드 `.zip` 및 `.pdf` 정상 `prepare_download` POST 확인 및 비인가 확장자 차단 검증.

---

## 3. 검증 증거

### 3.1 Reviewer 재현 스크립트 검증
```bash
PYTHONPATH=apps/cardrag-worker/tests uv run --no-sync pytest -c apps/cardrag-worker/pyproject.toml /tmp/test_zip_second_review.py -q --tb=short
```
- 결과: **3 passed in 1.82s** (A1, A2, A3 모두 통과)

### 3.2 정식 범위 단위/통합 테스트 (`test_017_scope.py`)
```bash
uv run --no-sync pytest apps/cardrag-worker/tests/test_017_scope.py -q --tb=short
```
- 결과: **55 passed in 8.71s**

### 3.3 전체 Worker 테스트 스위트
```bash
uv run --no-sync pytest apps/cardrag-worker/tests/ -q --tb=short
```
- 결과: **1,368 passed, 10 warnings in 50.34s** (기존 OCR fallback/native cache 경고 외 회귀 없음)

### 3.4 정적 분석 및 포맷팅 검사
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

### 3.5 보안 검사
```bash
gitleaks detect --no-git -v --source apps/cardrag-worker/src/cardrag_worker/
```
- 결과: **no leaks found (4.54 MB 검사 완료)**

---

## 4. 잔여 위험 및 경계 확인

1. **ZIP 압축 해제 미수행 원칙 유지**: ZIP 파일은 계속해서 지원되지 않는 첨부파일로 분류되며, 압축을 풀거나 내부 PDF를 추출하는 행위는 일절 발생하지 않습니다.
2. **실제 금융사 망 연동**: 단위/통합 테스트는 mock origin 및 fixture 환경에서 수행되었으며, 실제 운영망의 원격 카드사 라이브 다운로드는 배포 단계에서 검증되어야 합니다.
3. **기존 Handoff 이력 보존**: 기존 handoff 파일(`PLAN.md`, `REPORT.md`, `FIX_01.md` ~ `FIX_08.md`, `REVIEW.md` 등)은 수정·삭제·이동 없이 그대로 보존되었습니다.
