# 019 FIX_31_REPORT — 공통 HTML 서비스 오류 판별의 정상 페이지 오탐 차단

2026-10-22 Executor. FIX_31에 따라 FIX_30의 HTML classifier를 보완했다. 정상 HTML의 script/style/comment/hidden template에 있는 오류 문구를 서비스 장애로 오인하지 않으면서, 실제 보이는 Error 페이지와 명확한 503형 HTML은 typed retry 대상으로 유지한다. 실제 origin·배포·resume·timer/cursor는 변경하지 않았다.

## 변경 전 오탐과 변경 후

변경 전 `classify_html_service_error`는 HTML prefix 전체를 decode해 단순 문자열을 검색했다. 따라서 정상 목록에 포함된 비활성 UI template:

```html
<script>const errors={temporary:"Service unavailable"};</script>
```

만으로도 `service_unavailable_phrase`를 반환하고 RateLimitedClient가 정상 목록을 parser에 전달하지 못했다.

변경 후에는 stdlib `HTMLParser`로 64KiB prefix의 **가시 텍스트와 heading 구조만** 분석한다.

- `script`, `style`, `template`, `noscript`, `svg`, 주석은 제외
- `hidden`, `aria-hidden=true`, `display:none`, `visibility:hidden` 하위 subtree는 제외
- 가시 service-error 문구가 있어도 `Error`/`Service Unavailable`/`오류` 등의 heading/title 또는 명확한 error/fail/unavailable container가 있어야 서비스 오류로 판정
- 일반 상품 설명의 `currently unavailable`/`temporarily unavailable`만으로는 실패 처리하지 않음
- UTF-8과 cp949를 모두 시도하며, 64KiB 제한·safe MIME·원문 비보존 정책은 유지

실제 Woori 오류 페이지는 `h1 Error` + `.finishBox.fail` + 가시 한국어 서비스 오류 문구 구조라 계속 양성으로 판정된다. 명확한 `<title>503 Service Unavailable</title>`/`<h1>Service Unavailable</h1>`도 양성이다. 일반 HTML landing/목록/상세는 classifier를 통과해 기존 adapter parser가 처리한다.

## 단일 source 오류 정책

사용자가 승인한 “특정 단일 PDF URL의 카드사 등록 오류” 격리는 다음 조건에서만 적용하도록 `docs/OPERATIONS.md`에 명시했다.

- 정상 control PDF가 실제로 성공하고,
- 실패가 특정 source/URL에 한정되며 공식 등록 오류로 확인됨.

현재 BCTOP 실패 source와 별도 최신 상품500108이 모두 같은 HTML MIME 오류를 보였으므로 단일 source pass로 바꾸지 않았다. 같은 endpoint의 여러 source가 함께 실패하는 동안에는 실패/degraded를 유지한다.

## 테스트

`test_fix_30_origin_retry.py`를 14건으로 보강했다.

- script/comment/hidden template·hidden attribute·일반 상품 설명 오탐 없음
- UTF-8/cp949의 명확한 Error 페이지 양성
- 일반 503형 HTML 양성
- issuer=kb 정상 catalog는 RateLimitedClient 통과
- issuer=shinhan 명확한 Error page는 typed 차단
- raw HTML/서비스 문구/secret sentinel 비노출
- 기존 PDF/discovery 양성·bounded retry·cooldown 회귀

```text
uv run --no-sync pytest apps/cardrag-worker/tests/test_fix_30_origin_retry.py -q
14 passed

uv run --no-sync pytest apps/cardrag-worker/tests/ packages/cardrag-core/tests/ -q
1850 passed, 11 warnings in 60.83s

uv run --no-sync ruff check apps/cardrag-worker/ packages/cardrag-core/
All checks passed!
uv run --no-sync ruff format --check apps/cardrag-worker/ packages/cardrag-core/
161 files already formatted
git diff --check
통과
```

실제 failed source를 새 classifier로 1회 read-only probe한 결과:

```text
status_code=200
mime_type=text/html
reason_code=issuer_origin_service_error
service_signal=visible_service_error_page
origin_recovered=false
state_written=false
```

`FIX_30_REPORT.md`의 이전 `service_unavailable_phrase` 표기는 FIX_30 classifier 기준 역사 기록이며, 현재 evidence file은 `visible_service_error_page`로 갱신했다. 실제 origin은 계속 HTML 오류다.

## 운영 상태

- active release/image: `/opt/cardrag/019-fix24-11d1321cc9e6` (FIX_30/31 미배포)
- failed run `e81c17e69b5f428f8d6314ab2b8762d4`: 유지
- stable serving: `g-666c2d2ee9e24d04acf76eed-d6ffd9a927e9`, MCP `current=Worker stable`
- timer: active/enabled, `Persistent=no`, next `2026-10-23 10:00 KST`
- 실행 중 Worker: 0
- 새 image 배포·실제 우리 resume·backup flush·cursor/day/result 변경: 미실행

## 남은 단계

- Reviewer 승인 후 FIX_30/31 Python을 포함한 새 Worker image를 식별·배포해야 다음 예약 실행에 적용된다.
- 실제 origin이 계속 HTTP200 HTML이면 classifier 개선 후에도 bounded retry 뒤 `issuer_origin_service_error`로 정직하게 실패한다. endpoint 회복/공식 protocol 변경 확인 전에는 성공을 선언하지 않는다.
- 실패한 same-day slot은 `systemctl start` 반복이 아니라 원인 교정 후 명시 resume로 처리한다.
- 10/23 자동 국민 배치와 019 최종 자동운영 인수는 런타임 후속 검증 항목이다.