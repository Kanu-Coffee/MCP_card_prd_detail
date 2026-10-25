# 017 최종 코드 리뷰 — FIX_04 반영 확인

작성: 2026-10-19, Reviewer Codex. 대상 HEAD `aaeb2c4`와 현재 미커밋 017/FIX_01–04 구현.

## 판정

**FIX_04 수정 승인. 이번 검토 범위에서 추가 필수 코드 수정 사항 없음.** 새 FIX_05는 작성하지 않는다. 이는 현재 코드와 mock 기반 회귀에 대한 판정이며 신규 이미지 배포·운영 인수 완료 판정은 아니다.

PLAN.md·ANALYSIS.md·REPORT.md, FIX_01–04와 각 REPORT, Git status/diff, 변경된 pipeline/보존/재개 경로 및 테스트를 대조했다. 기존 작업 트리 변경과 handoff 이력을 보존했다. 이번 Reviewer가 변경한 저장소 파일은 본 신규 리뷰 기록뿐이다. 제품 코드·정식 테스트·운영 설정·서비스·배포를 변경하지 않았다.

## 수정 확인

- `pipeline.py:_run_locked`의 skip-pdf 경로가 자체 retained seal을 먼저 읽고 publication issuer adapter와 계약을 복원한 뒤 `_publish_sealed`로 진입한다. prior serving을 `ReuseSource`로 읽는 경로는 자체 seal이 없을 때 실행하므로 자기 running/interrupted run을 완료 source로 열던 문제가 해소됐다.
- `ReuseSource`의 완료 source 제한, seal 무결성 검사, 동일 generation의 remote bundle 검증 및 기존 predecessor fence는 유지된다. 실제 구현의 계약 검사는 `sealed.contract_sha256 != self.contract_sha256` 비교이며 이후 `_publish_sealed`에서 `_validate_local_seal`을 수행한다. FIX_04_REPORT에 적힌 `_verify_contract_sha256(...)` 함수 호출은 실제 코드에 없으므로 구현 근거는 이 실제 경로로 읽어야 한다. 검증 자체가 빠진 것은 아니다.
- 이전 FIX_03의 seal 생성 후 게시 전 실패 재개, 실제 CLI 원래 명령/bare/matching issuer 재개, 선택 issuer 이외의 자료 보존 회귀가 Worker 전체 suite에 포함돼 통과했다.
- 롯데 HTTP 500 서술 철회는 FIX_03_REPORT에 반영됐으며, ZIP 목록 파싱 실패와 정상 PDF 단건 다운로드 성공을 구분한다. complete collection 운영 인수 보류는 그대로 유지한다.

## Reviewer 직접 수행한 검증

| 검사 | 결과 |
|---|---|
| `uv run --no-sync pytest apps/cardrag-worker/tests/ -q --tb=short` | **1,345 passed, 10 warnings, 49.82초** |
| FIX_04 부록 원본 독립 재현: `PYTHONPATH=apps/cardrag-worker/tests uv run --no-sync pytest -c pyproject.toml /tmp/test_017_review_after_commit_verify.py -q --tb=short` | **2 passed, 2.30초**, registry 등록/미등록 모두 정상 복구 |
| 완료 기록 지점 직접 중단 주입: `PYTHONPATH=apps/cardrag-worker/tests uv run --no-sync pytest -c pyproject.toml /tmp/test_017_review_actual_commit_interruption.py -q --tb=short` | **2 passed, 2.12초**, 아래 방법 참고 |
| `uv run --no-sync ruff check apps/cardrag-worker/` | 통과 |
| `uv run --no-sync ruff format --check apps/cardrag-worker/` | 123 files already formatted |
| `git diff --check` | 통과 |

10개 경고는 기존 OCR fallback/native cache 시나리오의 RuntimeWarning이다. 이번에는 전체 workspace suite나 gitleaks를 재실행하지 않았으며 과거 Executor 결과를 Reviewer의 신규 실행으로 합산하지 않는다.

### 직접 중단 주입 방법과 확인 범위

FIX_04 부록 fixture를 기반으로 `state.finish_run(..., 'succeeded')` 호출 시 전용 BaseException을 발생시켜 일반 예외 처리·완료 정리가 실행되지 않게 했다. pointer의 실제 저장 body가 해당 seal generation이고 publish 테이블에 ready 증거가 있으며 run은 running 상태임을 먼저 확인했다. finish_run을 복원한 뒤 새 pipeline 객체로 같은 run_id를 재개했다.

registry 등록/미등록 두 경우 모두 전체 testbank/otherbank 자료 보존, 추가 PDF/OCR/embedding 호출 0, ReuseSource로 읽을 수 있는 완료 상태 복구를 확인했다. 이는 성공 후 상태 문자열만 바꾸는 기존 재현 외에, 실제 완료 기록 직전 중단 지점도 검사한 근거다. 실제 OS 프로세스 kill이나 원격 WebDAV 장애 주입은 아니다. 검사 코드는 임시 파일만 사용했다.

## 남은 운영 확인

- 신규 이미지/실제 Docker 네트워크의 8사 전체·선택·순회 실행, 실제 current/previous 및 검색 결과·백업 소비 대조는 이번 리뷰에서 수행하지 않았다.
- 실제 proxy 목록/PDF/304/redirect와 장기 90일 감사·예산/overdue·동시 실행 복구는 운영 평가 대상으로 남는다. 코드 테스트 통과를 운영 완료라고 보고하지 않는다.
- 롯데 DOCID 1893 ZIP 첨부는 PDF 전용 목록 parser에서 여전히 거부된다. ZIP 지원 또는 완전성 보존 정책을 검증하기 전 롯데 complete collection은 정상 판정하지 않는다.
- 실제 프록시 주소·키·VPN 설정을 새로 기록하지 않았으며 운영 서비스·순회 설정을 활성화하지 않았다. 배포·자원 회수도 수행하지 않았다.
