# FIX_30 변경01 — 일시적 origin 오류 재시도를8사 공통 경계에서 보완

2026-10-22 Codex, Reviewer/Planner. 사용자가 우리 외 모든 카드사의 유사 일시 오류 가능성을 지적했다. 기존 FIX_30을 우리 전용 예외로 구현하지 않도록 범위를 명확히 한다. 충돌 시 이 문서가 우선한다. 기존 FIX_30/진단 이력은 보존하며 이번 턴에 구현·배포·배치 재개는 하지 않았다.

## 현재 공통 로직과 이번 누락

공통 `_finite_stage`/`_revalidate_download`는 stage별 유한 retry budget을 사용한다. IssuerSpec 기본 maximum_retries=4이고 지수 대기 기본1/2/4초(cap30초)다. 네트워크/timeout/HTTP 오류는 origin_failure의 typed issuer 오류로 전달돼 기존 finite retry에 들어간다. 단403/429와 연결 장애 cooldown·요청/byte budget이 재시도보다 우선할 수 있어 모든 오류를 항상4번 호출하는 구조는 아니다. 403/429 Retry-After는 host/issuer별 영속 cooldown에서 처리한다.

이번 HTTP200 HTML은 PDFValidationError→issuer_source_invalid가 되어 non_retryable_predicate에 걸렸고 attempt1에서 종료됐다. max4 설정이 없는 문제가 아니라 일시적인 서비스 오류 페이지와 잘못된 source/문서 검증 오류를 구분하는 공통 분류가 부족한 문제다. discovery의 서비스 오류 HTML/정상 JSON 대신 오류 응답도 비슷한 경계이므로 다운로드만이 아니라 목록/상세조회 경계도 함께 확인한다. PDF 내용의 일반 문구를 오류 신호로 스캔해서는 안 된다.

## 수정 방향

1. issuer_collection/downloader/adapter의 기존 경계를 활용해 공통 transient-origin 분류·유한 재시도 정책을 정리한다. 우리 host/product/source에 하드코딩한 bypass나 카드사별 중복 retry loop8개를 만들지 않는다. 원본별 service-error 식별 신호가 필요하면 공통 typed 결과로 변환하는 최소 adapter 차이만 둔다.
2. timeout/일시적 transport/408/429/5xx 및 검증된 HTTP200 service-error 응답을 분리해 취급한다. 4xx 인증/권한/일반404, 로그인·challenge 페이지, 구조/서명/해시 손상, 보안/DNS/redirect 위반을 transient로 일괄 승격하지 않는다. 우리 HTML도 근본 원인 미확정이므로 무한 retry로 장애를 가리지 않는다.
3. 기존 Retry-After/cooldown·전체 deadline·stage attempt·run HTTP/byte budget·host concurrency/rate limit과 연동한다. 특히503 Retry-After도 합리적으로 처리할지 검토하고, stage retry와 client retry가 곱해지는 중첩 폭증을 막는다. 필요 jitter/대기 정책은 설정/상한·취소 가능성을 명시한다. 과도한 장시간 sleep이나 수천문서별 반복으로 원본을 압박하지 않는다.
4. 반복 요청은 목록·상세·PDF 등 검증된 읽기 성격의 issuer operation에 한정한다. HTTP method가POST라는 이유만으로 무조건 금지하거나, 모든POST를 임의 재시도하도록 전역 client에 넣지 않는다. 중간 파일·partial body는 안전하게 정리하고 마지막 정상 cache/게시를 보호한다.
5. finite budget 소진 시 `origin_unavailable`류의 명확한 typed 보고와 source/stage/attempt·status/MIME·다음 조치가 남아야 한다. 성공처럼 freshness를 갱신하거나 origin 차단을 상품 소멸로 해석하지 않는다. 원문/쿠키/암호화 form/인증/민감 URL은 기록하지 않는다.
6. 같은 날짜 round-robin 재호출은 저장된 terminal 실패를 반환하는 기존 정책이다. 이를 HTTP retry와 혼동하지 않는다. 당일 지연 재시도/추가 예약 실행을 새로 도입하려면 중복·cursor·원본 budget·날짜 경계를 별도 설계해야 하며 이번 보완만으로 자동 무한 재실행을 추가하지 않는다. 우리 복구는 원인 대응 뒤 명시 resume 경로로 한다.

## 추가 인수 기준

서로 다른 issuer의 공통 fake transport에 첫 응답 일시 오류→다음 정상 응답을 주어 실제 재시도·성공을 확인한다. HTTP200 HTML→PDF,5xx→정상,timeout→정상,429/503 Retry-After/cooldown,예산·deadline 소진,취소,인증/무결성 nonretryable,원문 비노출,중첩 retry 상한을 검증한다. 목록/상세/PDF 각각의 경계를 포함하며 PDF/JSON이라는 이유만으로 서비스 오류 본문을 정상 수집하지 않게 한다.

FIX_30_REPORT는 8사 공통 경계 수정과 우리 실제 endpoint 장애 해결을 구분한다. 공통 retry가 작동하더라도 실제 우리 서버가 계속 HTML이면 origin 미복구로 남기고 배치 완주를 주장하지 않는다. 기존 FIX_30의 운영 데이터/순회/10시 예약 보존 및 실제 재개 전 검증 조건은 유지한다.
