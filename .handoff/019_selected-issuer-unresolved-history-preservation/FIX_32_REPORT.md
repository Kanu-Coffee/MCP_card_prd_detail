# 019 FIX_32_REPORT — HTML 오류 판별의 영역 결합 및 void 요소 처리

2026-10-22 Executor. FIX_32의 P2 두 건을 공통 HTML parser에서 보완했다. 실제 origin probe·배포·resume·DB/cursor/timer 변경은 하지 않았다. FIX_30/31의 공통 retry·typed 진단·운영 경계는 유지한다.

## P2-1 — 빈 오류 영역과 무관한 상품 설명 결합

변경 전에는 class/id에 `error`/`fail`/`unavailable`이 있으면 전역 `error_container=True`가 되어, 페이지 어디의 서비스 문구와도 결합했다. 따라서 정상 목록의 빈 `<div class="error-message"></div>`와 다른 상품의 `airport pickup currently unavailable` 설명이 페이지 전체 장애로 오인됐다.

변경 후:

- 전역 `error_container`를 제거했다.
- 오류 판정 근거를 다음 두 가지로 분리했다.
  1. 가시 `Error`/`Service Unavailable`/`오류` heading 또는 title
  2. `service-error`, `error-page`, `error-container`, `finishbox` 등 **페이지 수준 오류 영역 내부의 가시 서비스 문구**
- 오류 영역은 `body/main/section/article/div`에 한정하고, `li` 같은 개별 상품 항목의 class marker는 페이지 수준 오류 근거로 사용하지 않는다.
- 빈 오류 영역과 상품 설명의 `currently unavailable`/`temporarily unavailable`만으로는 실패 처리하지 않는다.
- 실제 Woori `finishBox fail` + `h1 Error` 페이지와 명확한 503형 HTML은 계속 양성이다.

## P2-2 — HTML void 요소의 stack 잔류

변경 전에는 `input`, `br`, `img`, `meta` 등 종료 태그 없는 void 요소도 stack에 push되어, `<input hidden>` 뒤의 형제 Error 본문 전체가 hidden subtree로 오인됐다.

변경 후 표준 void 요소 집합(`area/base/br/col/embed/hr/img/input/link/meta/param/source/track/wbr`)은 stack에 push하지 않는다. hidden container의 실제 하위 요소 제외, script/style/template 제외, hidden input 자체의 비표시는 유지한다. 따라서 hidden/일반 void 뒤의 실제 서비스 Error 본문을 다시 검출한다.

## 테스트

`test_fix_30_origin_retry.py`는 17건이다. FIX_32에서 다음을 추가했다.

- 빈 `error-message` slot + 개별 상품의 currently unavailable → `None`
- hidden input/br/img 등 void 뒤의 실제 `h1 Error` + Service unavailable → `visible_service_error_page`
- error slot이 있는 정상 catalog를 issuer `kb`/`woori` RateLimitedClient가 통과
- 기존 script/comment/hidden subtree, UTF-8/cp949, 503형, raw HTML/secret 비노출 회귀 유지

```text
uv run --no-sync pytest apps/cardrag-worker/tests/test_fix_30_origin_retry.py -q
17 passed

uv run --no-sync pytest apps/cardrag-worker/tests/ packages/cardrag-core/tests/ -q
1853 passed, 11 warnings in 63.02s

uv run --no-sync ruff check apps/cardrag-worker/ packages/cardrag-core/
All checks passed!
uv run --no-sync ruff format --check apps/cardrag-worker/ packages/cardrag-core/
161 files already formatted
git diff --check
통과
```

실제 failed source를 새 parser로 1회 read-only probe한 결과도 positive를 유지했다.

```text
status_code=200
mime_type=text/html
reason_code=issuer_origin_service_error
service_signal=visible_service_error_page
origin_recovered=false
state_written=false
```

원본 HTML/서비스 문구/secret sentinel은 오류 문자열이나 evidence에 저장하지 않는다.

## 운영 상태

- active release/image: `/opt/cardrag/019-fix24-11d1321cc9e6` (FIX_30/31/32 미배포)
- failed run `e81c17e69b5f428f8d6314ab2b8762d4`: 유지
- stable serving: `g-666c2d2ee9e24d04acf76eed-d6ffd9a927e9`, MCP `current=Worker stable`
- timer: active/enabled, `Persistent=no`, next `2026-10-23 10:00 KST`
- 실행 중 Worker: 0
- 새 image 배포, 실제 우리 resume, backup flush, cursor/day/result 변경: 미실행

## 남은 단계

- Reviewer 승인 후 FIX_30/31/32 Python을 포함한 새 Worker image를 배포해야 다음 예약 실행에 적용된다.
- 실제 origin이 계속 HTTP200 HTML이면 bounded retry 뒤 `issuer_origin_service_error`로 실패한다. origin 회복/공식 protocol 변경 전에는 성공을 선언하지 않는다.
- 실패한 same-day slot은 `systemctl start` 반복이 아니라 원인 교정 후 명시 resume로 처리한다. 10/23 국민 자동 배치와 019 최종 인수는 런타임 후속 검증 항목이다.