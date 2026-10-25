# 019 FIX_31 — 공통 HTML 서비스 오류 판별의 정상 페이지 오탐 차단

2026-10-22 Codex, Reviewer. FIX_30/REPORT/AMENDMENT_01과 신규 공통 판별기를 검토한 결과 아래 P2를 임시 HTTP fixture로 재현했다. 현재 배포 image에는 FIX_30이 아직 없으며 실제 우리 배치 실패의 원인은 별도 LIVE_REVIEW_12와 REVIEW_17을 따른다.

## P2 — 정상 목록의 비활성 오류 템플릿까지 서비스 장애로 분류

issuer_collection.py classify_html_service_error는 HTML 전체 prefix를 decode하고 단순 문자열 검색한다. 다음처럼 정상 목록과 script 내부의 공통 UI 오류 메시지를 함께 담은 HTTP200 HTML도 OriginServiceError가 되어 기존 adapter parser가 정상 목록을 읽기 전에 차단된다.

```html
<html><head><title>Card catalog</title>
<script>const errors={temporary:"Service unavailable"};</script>
</head><body><h1>Credit cards</h1><div data-card="a">Normal product listing</div></body></html>
```

직접 재현 결과 classifier=service_unavailable_phrase, RateLimitedClient(issuer=kb)의 정상 목록 GET도 incorrectly blocked=true다. 8사 공통 client에 적용되므로 우리 전용 현상이 아니고, 원래 parser 전에 거짓 장애를 만들어 bounded retry 후 수집 실패시킬 수 있다. 실제 국민 사이트에 이 HTML이 있다는 주장은 아니다.

## 수정 경계

- 기존64KiB 제한/UTF-8·cp949 지원·safe MIME 및 원문 비보존 정책을 유지한다.
- script/style/comment/공통 숨김 템플릿 등에 들어 있는 오류 문구를 실제 응답 장애 표시로 간주하지 않는다. 페이지가 명확한 service-error 응답인지 가시 본문/오류 구조 등 보수적 근거로 판별한다. 정상 페이지의 일부 상품/기능 설명에 currently unavailable 등이 들어 있는 경우도 전체 origin 장애로 단정하지 않는다.
- parser가 정상 목록/상세를 검증할 수 있는 일반 HTML은 그대로 전달한다. 반면 실제 우리 Error/서비스 비정상 페이지와 명확한 일반503식 HTML 오류는 typed retry 대상으로 유지한다. JSON/정상 PDF 본문의 단어를 HTML 장애 신호로 스캔하지 않는다.
- 공통 retry budget/backoff/503 cooldown/문서무결성·보안 및 typed 진단 개선은 유지한다. false negative/unknown HTML을 정상 PDF로 통과시키지 않는다.
- 정상 오류 템플릿 HTML, 주석/style/hidden template, 일반 상품 설명, 정상 JSON/PDF, 명확한 실제 오류 HTML(UTF-8/cp949), 적어도 다른2사 client 경계의 양성/음성 fixture를 추가한다. 원시 HTML·secret sentinel 비노출, bounded retry/취소 회귀를 포함한다.

## 이번 사용자 조건: 단일 문서 등록 오류는 격리 가능

사용자는 특정 단일 PDF URL의 카드사 등록 오류로 확인되면 DRM/ZIP처럼 건너뛰고 다른 문서를 계속 처리해도 된다고 승인했다. 이 원칙은 타당하며 개별 source 오류를 보고서에 남기고 마지막 정상본·미지원 증거·재확인 가능성을 유지해야 한다. 오류 문서를 정상 수집/정상 freshness로 꾸미는 의미는 아니다.

하지만10:47 재확인에서 실패 BCTOP와 별도 최신 상품500108이 모두 같은 MIME 오류였다. 현재 건은 단일 source 문제로 확정되지 않았으므로 이번 공통 origin 오류를 무조건 개별 unsupported/pass로 바꾸지 않는다. 정상 control PDF가 성공하고 실패가 특정 source에 한정된다는 근거가 있을 때 적용할 정책으로 문서에 명시한다. 단일 source 격리 기능을 새로 크게 설계하는 것을 이 오탐 수정의 선행 조건으로 삼지 않는다.

## 검증 및 인계

FIX_31_REPORT.md에 오탐 재현의 변경 전/후, 관련/Worker-Core 회귀 및 ruff/diff 결과를 기록한다. 현재 실제 원본 서비스 장애 해결과 공통 판별기 검증을 구분한다. 신규 image 배포·우리 resume·timer/cursor 변경은 이번 리뷰에서 수행하지 않았다. 원본이 계속 오류면 실패 상태를 유지하며 가짜 성공을 만들지 않는다.
