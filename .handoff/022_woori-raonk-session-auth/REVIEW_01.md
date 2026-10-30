# 022 구현 재리뷰 — FIX_04 인수

작성일: 2026-10-30 KST  
역할: Reviewer  
판정: **구현·오프라인 검증 범위 인수. 추가 수정 지적 없음. 운영 배포·배치 완주 인수는 별도.**

## 검토 범위

PLAN.md, REPORT.md, FIX_01~04 및 각 FIX 보고서, 현재 Git status와 변경 코드·테스트를 이전 리뷰 증거와 대조했다. 기존 020/021 관련 미커밋 변경이 있는 상태임을 확인했으며, 이를 이번 022 변경으로 일괄 인수하거나 되돌리지 않았다. 이번 리뷰는 코드와 검증 증거를 대상으로 하며 운영 변경을 수행하지 않았다.

## 해결 확인

- landing에서 얻은 RAONK 인증 헤더를 다운로드 세션에 연결하고, client별 캐시 및 bounded retry의 무효화 경로를 유지한다.
- Cookie 객체 속성을 복제·복원하며, 서버 갱신과 삭제를 반영한다. 필수 쿠키 전체 key의 결손 시 fresh landing으로 회복한다.
- None client와 격리를 지원하지 않는 client를 거부하고 전역 fallback session을 제거했다.
- discovery와 prepare가 동일 `_ensure_session` 경로를 사용하므로 필수 cookie key 초기화가 일관된다.
- **FIX_04**: `_normalize_cookie_path`는 `path or '/'`만 적용한다. `/dcpc`와 `/dcpc/`의 별도 Cookie identity를 보존한다. 신규 테스트는 capture/clear/restore, 경로별 요청 Cookie 헤더, 한 path 삭제 시 나머지 보존, 필수 key 결손 후 한 번의 fresh landing 회복을 검증한다.
- 실제 DetailInventory hit, client 분리·wrapper 재사용, single-flight와 취소 후 회복, 성공 PDF/304의 cookie 삭제, 실제 WorkerPipeline 동일 slot의 두 문서 순차 처리, 로그·DB·보고서 sentinel 검사가 포함돼 있다.

이전 FIX_01~04에 남겼던 수정 요구는 현재 검토 범위에서 해결된 것으로 판정한다. 추가 FIX를 발행하지 않는다.

## Reviewer 직접 실행한 검증

```bash
uv run --no-sync pytest apps/cardrag-worker/tests/test_issuers.py apps/cardrag-worker/tests/test_issuer_http.py apps/cardrag-worker/tests/test_issuer_collection.py apps/cardrag-worker/tests/test_downloader.py apps/cardrag-worker/tests/test_fix_30_origin_retry.py apps/cardrag-worker/tests/test_022_woori_raonk_session_auth.py apps/cardrag-worker/tests/test_017_scope.py apps/cardrag-worker/tests/test_pdf_cache.py -q
# 174 passed in 14.75s

uv run --no-sync ruff check apps/cardrag-worker/src/cardrag_worker/issuers/woori.py apps/cardrag-worker/src/cardrag_worker/downloader.py apps/cardrag-worker/src/cardrag_worker/pipeline.py apps/cardrag-worker/tests/test_issuers.py apps/cardrag-worker/tests/test_022_woori_raonk_session_auth.py apps/cardrag-worker/tests/test_017_scope.py
# All checks passed!

git diff --check
# 통과
```

검토 시 파일 SHA-256:

| 파일 | SHA-256 |
|---|---|
| `src/cardrag_worker/issuers/woori.py` | `cc454a9ad01db4ad29a45592b6efdc2839744f8fe34bd593a7bbe97eb1d95039` |
| `src/cardrag_worker/downloader.py` | `b3d9e4021d086f9866e99944c7fc5c6890ccf109454a06840a801b5f9800a2d1` |
| `src/cardrag_worker/pipeline.py` | `c3f060cc2665b8596524f8491d48f7667ee19192bfad4ead9ce48ced60f72e2e` |
| `tests/test_022_woori_raonk_session_auth.py` | `9dc0bd6bb192a28ec3dd90c7e927a8345ee3fe7ecc3f7f1cf017dd4ad8e5876e` |

파일 경로는 모두 `apps/cardrag-worker/` 기준이다. pipeline hash는 기존 선행 변경도 포함한 현재 파일 식별용이며, 선행 변경 전체를 022 범위로 승인한다는 뜻은 아니다.

## 실사·운영 범위와 다음 단계

3상품 원본 다운로드의 크기·page 수·hash 성공 증거는 Executor의 FIX_03_REPORT.md에 있다. 이번 Reviewer는 원본 서버를 다시 호출하지 않았다. FIX_04는 cookie path 보존 변경이므로 기존 수정 요청에서 추가 실사를 필수로 요구하지 않았으며, 그 범위에 맞게 오프라인 회귀로 검증했다.

174개 테스트 통과가 우리카드 922건 전체 수집, 장시간 token 만료 관측, seal·게시·MCP 전환·backup 완결까지 입증하는 것은 아니다. 운영 배포와 실패 run `df29d8e5dbb34048942a5296bb2d6ac8`의 재개는 아직 미수행이다. 사용자의 운영 적용 지시가 있으면 PLAN 6절에 따라 현재 release·writer·timer·포인터·용량을 재확인한 뒤 진행한다. timer 유지에 대한 기존 사용자 지시와 기존 OCR/Paddle/복구 자료 보존을 따른다.

현재 인수는 다음 운영 적용 준비로 진행할 수 있다는 판정이며, task 전체의 운영 완료나 closure 선언은 아니다. 기존 handoff 문서는 변경하지 않고 이 리뷰 문서만 새로 작성했다.
