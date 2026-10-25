# 019 REVIEW_18 — FIX_31 원래 오탐 해소, HTML parser 경계2건 보완 필요

2026-10-22 Codex, Reviewer. FIX_31/REPORT, FIX_30/REPORT/AMENDMENT와 REVIEW_17 및 현재 source/test/운영 설정을 대조했다. 구현·설정·배치·실제 origin 호출을 추가하지 않았다.

## 판단

원래의 script 내부 공통 UI 오류 문구를 서비스 장애로 오인한 문제는 해소됐다. HTMLParser로 가시 본문과 구조를 확인하는 방향은 타당하다. 다만 **추가 P2 두 건을 독립 fixture로 재현해 최종 통과를 보류하고 FIX_32를 발행한다.**

1. issuer_collection.py:57–59/107–112: 빈 `div.error-message`가 전역 error_container를 켠다. 다른 영역의 정상 상품 설명 `airport pickup currently unavailable`와 결합하면 정상 카드 목록이 visible_service_error_page로 판정된다. 오류 구조와 문구가 같은 오류 응답을 설명하는지 연결하지 못한다.
2. 같은 파일:63–64/76–78: 모든 start tag를 stack에 push하여 종료 태그 없는 `<input hidden>` 뒤의 형제 Error 본문을 hidden subtree로 오인한다. 명확한 Error/Service unavailable 문서가 None으로 판정돼 새 retry 분류에서 빠진다.

이들 HTML이 실제 특정 카드사에 있다는 주장은 아니다. 공통 parser 자체의 표준 HTML 처리/증거 결합 경계 오류이며 신규 배포 전에 보완할 사항이다. 상세 fixture/기대값은 FIX_32에 기록했다.

## 직접 검사

```text
normal_script_template => None (수정 확인)
normal_catalog_empty_error_slot => visible_service_error_page (오탐 재현)
real_error_after_hidden_void => None (미탐 재현)
real_error => visible_service_error_page (기존 양성 유지)

uv run --no-sync pytest apps/cardrag-worker/tests/test_fix_30_origin_retry.py apps/cardrag-worker/tests/test_downloader.py apps/cardrag-worker/tests/test_017_scope.py -q
84 passed in9.48s
ruff check (issuer_collection.py/test_fix_30_origin_retry.py), git diff --check
통과
```

기존84건에 두 경계 fixture는 포함되지 않았다. Executor 전체1850건 통과 보고를 오탐/미탐이 없다는 근거로 사용하지 않으며, Reviewer가 전체 suite를 반복했다고 주장하지 않는다.

## 운영 경계

actual compose image는 여전히 cardrag-worker:019-fix24-11d1321cc9e6, proxy 빈 값이다. FIX_30/31 미배포 상태이므로 이번 신규 classifier 문제가 기존 운영 실패의 원인은 아니다. timer active/enabled, 다음10/23 10:00KST 유지. 현재 우리 origin의 해결 여부를 재검사하지 않았으며 LIVE_REVIEW_12/REVIEW_17의 원본 오류와 단일 source로 확정되지 않았다는 관찰을 유지한다. 실제 origin 장애를 DRM/ZIP식 개별 pass로 바꾸지 않는다. 배포·우리 resume·자동운영 최종 인수는 후속이다.
