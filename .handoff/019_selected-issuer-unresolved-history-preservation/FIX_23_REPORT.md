# 019 FIX_23_REPORT — 리뷰 보완 및 Codex quota→로컬 Paddle 예외 적용

2026-10-21 Codex. 사용자가 Reviewer 검증과 작은 fallback 변경의 즉시 수정을 승인했다. FIX_22 구현을 검토한 뒤 FIX_23의 국소 보완을 직접 수행했다. 이전 handoff/기존 미커밋 변경은 보존했다.

## 변경 및 범위 판단

- FIX_22의 full content manifest 복원 조건에 sealed content variant ID 일치를 추가했다. 동일 body/PDF라도 metadata가 다른 정상 sibling manifest를 원본 variant로 반환하지 않는다. 원본 복원 불가 시 검증된 local imported 재사용 경로는 유지한다.
- 새 stage DB 진단이 discovery의 issuer-lotte를 strict doc ID로 처리하여 2차 ValueError를 내지 않게 했다. 실제 doc/source hash ID만 공개하고 나머지는 stage/reason/category로 진단한다.
- FailoverOCRResolver에서 **codex-exec→local-paddleocr + provider_process_rate_limited** 조합만 기존 whole-document fallback으로 보낸다. 다른 systemic 원인/다른 provider 조합은 전파한다.
- ProviderSystemicError 원시 args/notes/context를 경고로 출력하지 않는다. fallback 실패/취소도 전파한다. Codex 부분 chunks는 보존하고 Paddle 전체 문서 결과와 섞지 않는다.
- schema/settings/model/contract/reuse-key/checkpoint 형식·epoch를 변경하지 않았다. run 전체의 신규 circuit 상태·DB migration도 없다. 따라서 현재 run의 명시 resume 전에 반영할 수 있는 작은 변경으로 판단했다.
- 운영 문서에 좁은 예외를 명시했고, 단일 복구본 보존 대상을 낡은 fix16 고정명이 아닌 활성 deployment metadata의 독립 snapshot_directory로 바로잡았다.

변경 파일: content_cache.py, pipeline.py, ocr.py, docs/OPERATIONS.md, 신규 tests/test_fix_23_quota_fallback.py.

## 검사

신규12건: 실제 OCRResolver 조합의 quota→Paddle whole document(원시 secret sentinel 비노출), Paddle 실패 후 Codex chunk 재사용, 인증/설정/unknown exit/network/credential/contract 및 다른 provider 조합8건의 fallback 차단, full content sibling variant 원본 오인 차단, discovery DB 오류의 안전 진단.

```text
uv run --no-sync pytest apps/cardrag-worker/tests/test_fix_23_quota_fallback.py apps/cardrag-worker/tests/test_fix_22_ocr_identity_resume.py apps/cardrag-worker/tests/test_ocr.py apps/cardrag-worker/tests/test_content_cache.py apps/cardrag-worker/tests/test_pipeline.py -q
281 passed,9 warnings in4.40s

uv run --no-sync pytest apps/cardrag-worker/tests/ packages/cardrag-core/tests/ -q
1808 passed,10 warnings in58.72s

uv run --no-sync ruff check apps/cardrag-worker/ packages/cardrag-core/
All checks passed!
uv run --no-sync ruff format --check apps/cardrag-worker/ packages/cardrag-core/
158 files already formatted
 git diff --check
통과
```

최초 신규 테스트 실행에서는 discovery mock.state 설정 누락으로1건 실패했고 fixture를 수정한 뒤 관련/전체 검사를 위와 같이 통과했다. 런타임 코드를 실패에 맞춰 약화하지 않았다.

## 운영 인계 및 제한

본 보고서는 코드/fixture 검증이다. 로컬 Paddle 실제 모델 로딩·CPU 추론이나 롯데 완주를 테스트 대역으로 증명했다고 주장하지 않는다. 다음 단계는 REVIEW_11의 검토 기록에 따라 source가 일치하는 운영 image에 반영하는 것이며, 실제 배포·복구본 보존·현재 run/330건·97chunks의 전후 증거는 별도 OPERATIONS_REPORT_08에 남긴다. 롯데 resume 자체는 이번 요청에서 시작하지 않는다. 설정된 fallback이 없거나 다른 provider이면 이 예외는 활성화되지 않는다.
