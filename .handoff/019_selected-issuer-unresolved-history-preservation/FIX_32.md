# 019 FIX_32 — HTML 오류 판별의 영역 결합 및 void 요소 처리

2026-10-22 Codex, Reviewer. FIX_31/REPORT 검토에서 원래 script template 오탐은 해소됐으나 아래 두 P2를 임시 HTML fixture로 재현했다. 실제 운영 image는 아직FIX_24이며 이번 변경은 미배포다. 호스트별 예외를 추가하지 말고 동일 공통 parser를 국소 보완한다.

## P2-1 — 빈 오류 영역과 무관한 상품 설명의 잘못된 결합

`_VisibleHtmlSummary._start`는 class/id에 error/fail/unavailable이 있으면 전역 error_container=True로 고정한다. classifier는 이후 페이지 어디에나 서비스 문구가 있으면 두 증거를 결합한다. 따라서 정상 목록의 빈 UI 오류 slot과 특정 상품 기능의 일시 중단 안내를 origin 전체 장애로 오인한다.

```html
<html><head><title>Card catalog</title></head><body>
<h1>Credit cards</h1><div class="error-message"></div>
<ul><li data-card="A">Card A</li>
<li data-card="B">Card B - airport pickup currently unavailable</li></ul>
</body></html>
```

실제 반환값=visible_service_error_page, 기대값=None. 빈/무관한 class marker를 페이지 전역 근거로 삼지 말고 가시 오류 문구와 해당 오류 구조의 연관성을 확인한다. 단순 관련 class가 있는 개별 상품 안내를 페이지 수준 서비스 장애로 확대하지 않는다. 실제 우리 finishBox.fail/Error 페이지·명확한 일반 service-error 문서는 계속 검출한다.

## P2-2 — HTML void 요소를 stack에 남겨 이후 전체 본문을 숨김

handle_starttag는 input/br/meta/img 등 종료 태그 없는 void 요소까지 push한다. 아래 정상 HTML의 hidden input이 stack에 남아 실제 Error 본문을 모두 숨긴다.

```html
<html><head><title>Bank</title></head><body>
<input hidden name="csrf"><h1>Error</h1><p>Service unavailable</p>
</body></html>
```

실제 반환값=None, 기대값=visible_service_error_page. 표준 void 요소는 subtree를 열지 않게 처리한다. 일반 hidden 컨테이너의 실제 하위 요소 제외와 script/style/template 제외는 유지한다. hidden input의 형제 본문까지 제외하지 않는다.

## 완료 기준

두 재현의 양성/음성, 무관한 빈 error slot/개별 unavailable 상품, hidden void와 일반 void 뒤의 실제 service error, hidden subtree 및 script/comment 정상 페이지, UTF-8/cp949 실제 Error 페이지를 테스트한다. RateLimitedClient 목록 경계에서 잘못된 issuer 장애가 발생하지 않는지 확인하고 기존 bounded retry/다운로드 validation/원문 비노출을 유지한다.

FIX_32_REPORT에 수정 전후 재현·관련 및 Worker/Core 회귀·ruff/diff 결과를 남긴다. 새 origin probe/배포/우리 resume/DB·cursor·timer 변경을 이 parser 보완의 전제로 하지 않는다. 실제 origin 복구 및 단일 문서 등록 오류 격리 정책은 FIX_30/31의 운영 경계를 유지하며, 공통 origin 장애를 pass로 감추지 않는다.
