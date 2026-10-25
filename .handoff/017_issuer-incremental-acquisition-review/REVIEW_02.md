# 017 ZIP 정책 추가분 코드 리뷰 — FIX_09 반영 확인

작성: 2026-10-19, Reviewer Codex. 대상 HEAD `aaeb2c4`와 현재 미커밋 017/FIX_06–09 구현.

## 판정

**코드 리뷰 승인. 이번 검토 범위에서 추가 필수 코드 수정 요청 없음.** FIX_09 A1의 게시 전 오류 증거 영속화와 재개 계승을 확인했다. 문서 정정 권고 1건(P3)은 아래에 기록한다. 이 판정은 신규 운영 이미지·실환경 정상 동작 인수나 배포 완료를 뜻하지 않는다.

PLAN/ANALYSIS/REPORT, FIX_01–09 및 존재하는 각 REPORT, 기존 REVIEW.md, Git status/diff 및 수정 함수·회귀를 대조했다. FIX_05의 ZIP 해제 요구는 FIX_06에 의해 철회된 상태다. 기존 문서와 작업 트리 변경을 보존했으며 이번 Reviewer가 저장소에 추가한 것은 본 리뷰 기록뿐이다. 제품·정식 테스트·운영 설정·배포를 변경하지 않았다.

## 확인한 변경과 정책

- `_build_v5_generation`이 source별 첨부 오류 identity/사유/유형, 오류 수 및 collection 상태를 local publication seal에 포함한다. seal 원자적 저장 후 remote 게시를 시작하므로 성공 후에만 생성되는 execution-result.json에 의존하던 누락이 해소됐다.
- `_publish_sealed`와 publication-only resumer가 해당 seal의 오류 수를 복원한다. 기존 seal/contract/object 검사와 최신 pointer 보호 경로는 유지된다. 과거 seal의 fallback은 현재 run 및 저장 계획의 source를 참조하는 호환 경로다.
- 기존 실패 재현을 원본 handoff 부록에서 추출해 다시 실행했다. finish_run(succeeded) 직전에 BaseException을 주입해 remote pointer는 게시됐지만 execution-result.json은 없는 실제 코드 경계를 검사했으며 count=1/degraded가 유지됐다.
- execution-result.json을 생성하지 않는 일반 all-stage run의 publication-only 재개도 같은 오류 상태를 보존한다.
- Worker 전체 회귀에는 실제 adapter ZIP 목록 관측, ZIP을 열지 않는 spy, 정상 sibling 계속 처리, collect/process 계승, 우리 RAONK 준비, 같은 URL의 정상 PDF 복구 후 다음 배치 유지와 앞선 seal 재개 검사가 포함돼 통과했다.
- ZIP은 계속 unsupported 문서 오류다. 다른 정상 상품 처리는 계속하고 이전 검증 자료를 보존한다. ZIP 안의 파일을 열거나 대표 PDF를 선택하지 않으며 특정 DOCID/상품/파일명 예외를 추가한 정책이 아니다. 오류가 남은 결과는 complete와 구분한다.

## Reviewer 직접 검증

| 검사 | 결과 |
|---|---|
| `uv run --no-sync pytest apps/cardrag-worker/tests/ -q --tb=short` | **1,370 passed, 10 warnings, 51.39초** |
| FIX_09 부록 원본 재현: `PYTHONPATH=apps/cardrag-worker/tests uv run --no-sync pytest -c pyproject.toml /tmp/test_zip_crash_review_verify.py -q --tb=short` | **2 passed, 1.75초** |
| `uv run --no-sync ruff check apps/cardrag-worker/` | 통과 |
| `uv run --no-sync ruff format --check apps/cardrag-worker/` | 123 files already formatted |
| `git diff --check` | 통과 |

10개 경고는 기존 OCR cache/fallback 경로다. 이번에는 전체 workspace suite·gitleaks를 재실행하지 않았으며 이전 Executor 결과를 Reviewer 신규 실행으로 합산하지 않는다.

## 비차단 문서 정정 권고 [P3]

`docs/OPERATIONS.md:437-441`에는 아직 ZIP 한 건 때문에 롯데 전체 목록 parser가 실패하며 archive-aware 구현 전 정상 수집을 재개할 수 없다는 이전 설명이 남아 있다. 이번 사용자 정책은 ZIP 자체를 오류로 격리하고 다른 상품 처리를 계속하는 것이며 압축 해제는 철회됐다.

배포 시 이 운영 안내를 새 정책에 맞춰 정정하는 것을 권고한다. source별 ZIP 오류/기존 자료 보존/degraded/만료 재확인/metadata 교체 복구를 설명하고, 과거 네트워크 장애 증거와 아직 배포·실환경 검증하지 않은 신규 구현을 구분해야 한다. ZIP 내부 처리가 운영 재개의 선행 조건인 것처럼 안내하지 않는다. 코드 수정 승인을 차단하는 새 런타임 결함으로 판정한 것은 아니다.

## 운영 확인 경계

신규 Docker 이미지·실제 카드사 목록/PDF/프록시 304/redirect, 실제 8사 serving 검색·current/previous·백업 대조, 90일 실시간 순회와 장애 복구는 이번 리뷰에서 수행하지 않았다. 실제 원본의 ZIP이 여전히 남아 있다면 문서 오류/degraded는 정상적인 정책 결과이며, 그 문서까지 정상 PDF로 수집됐다고 판정해서는 안 된다. 운영 적용·라이브 검증은 별도 단계로 남는다.
