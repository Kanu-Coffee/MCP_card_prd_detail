# 022 — 우리카드 RAONK 다운로드 세션 인증 개선 실행 보고서

작성일: 2026-10-30 KST  
역할: Executor  
상태: 구현 및 오프라인/샘플 검증 완료 (운영 배포 및 실행 재개는 미수행)

---

## 1. 개요 및 변경 이유

우리카드 공시 상세 공시문서 다운로드 시, 공식 진입 페이지(`https://pc.wooricard.com/dcpc/yh1/cct/cct11/prdntc/H1CCT211S09.do`)에서 발행하는 세션 쿠키와 `RAONK_AUTH_PAGE_ID`, `RAONK_AUTH_KEY` HTTP 헤더가 누락되어 원본 서버가 HTTP 200 HTML 서비스 오류 페이지를 반환하고, 결과적으로 우리카드 전체 수집이 중단되던 문제를 해결했다.

공식 진입 페이지의 정상 요청 규약을 충실히 준수하며, 인증 우회나 특정 상품의 임의 skip 없이 정상 다운로드 계약을 완성했다.

---

## 2. 세션 소유 및 갱신/무효화 설계

1. **토큰 추출 및 엄격한 검증 (`extract_raonk_auth_tokens`)**:
   - `BeautifulSoup` (lxml 파서) 기반으로 landing HTML 내 hidden input `raonkAuthKey`, `raonkAuthPageId`를 추출한다. (id 및 name 속성, 대소문자 무관 탐색)
   - 속성 순서, 공백, 따옴표 형태에 견고하며, 결손/공백/중복 충돌/허용되지 않는 문자(CRLF, 따옴표, 공백 등 헤더 인젝션 위험 문자) 탐지 시 `IssuerMarkupChanged`로 즉시 fail-closed 처리한다.
   - 예외 및 로그 메시지에 실제 보안 토큰 값이 노출되지 않도록 마스킹/보호했다.

2. **클라이언트 단위 세션 소유 (`_WooriSession`, `WeakKeyDictionary`)**:
   - 세션(`page_id`, `auth_key`, `cookies`)은 HTTP 클라이언트 인스턴스 단위로 격리 관리된다 (`weakref.WeakKeyDictionary`).
   - discovery 클라이언트와 download 클라이언트가 상이하거나 재개(resume) 등으로 새로운 클라이언트가 투입되어도 다른 세션 값이 혼입되지 않으며, 클라이언트 수명 주기 종료 시 메모리 누수 없이 자동 회수된다.
   - `pipeline.py`에서 각 문서 처리 시 호출하는 `client.cookies.clear()`에 대응하여, 동일 클라이언트 내에서 캐시된 세션이 유효한 경우 매번 불필요한 landing GET을 호출하지 않고 캐시된 쿠키를 클라이언트에 복원(restore)하여 다운로드를 수행한다.

3. **단일 비행(Single-Flight) 동시성 제어**:
   - 동일 클라이언트에서 복수의 다운로드 준비가 동시에 발생하더라도 클라이언트별 `asyncio.Lock`을 통해 최초 1회만 landing GET을 발행하고 후속 태스크는 완료된 세션을 공유하여 중복 호출 및 쿠키 불일치를 방지한다.

4. **실패 무효화 및 단계적 재시도 연계 (`invalidate_session`)**:
   - `SecurePDFDownloader`에서 원본 서비스 오류(`OriginServiceError`) 발생 시, `pipeline.py`의 `acquire` 예외 경로에서 `adapter.invalidate_session(current_client)`를 호출하여 무효화한다.
   - 공통 `_finite_stage`의 재시도 예산 범위(`maximum_retries`) 내에서 다음 시도 시 새로운 landing GET을 통해 신선한 세션과 토큰을 획득하여 재시도한다.
   - 복수 계층에서의 retry 증폭 없이 기존 bounded retry 정책을 유지한다.

5. **다운로더 호스트 간 리다이렉트 보호 (`SecurePDFDownloader`)**:
   - 다운로드 스트림 중 리다이렉트 발생 시, 대상 URL의 호스트가 기존 요청 호스트와 다를 경우 `RAONK_AUTH_PAGE_ID`, `RAONK_AUTH_KEY` 헤더를 제거하여 외부/타 호스트로의 인증 정보 유출을 차단한다.

---

## 3. 변경 및 추가 파일

- `apps/cardrag-worker/src/cardrag_worker/issuers/woori.py`:
  - `_SAFE_HEADER_VALUE` 정규식 추가.
  - `extract_raonk_auth_tokens(html: str) -> tuple[str, str]` 함수 추가.
  - `_WooriSession` 데이터클래스 정의.
  - `WooriAdapter`에 세션 캐시, 락, 쿠키 캡처/복원, `_ensure_session`, `invalidate_session` 구현.
  - `prepare_download`에 세션 보장 및 `RAONK_AUTH_PAGE_ID`, `RAONK_AUTH_KEY` 헤더 설정 추가.
  - `discover_current`에서 landing GET 시 세션 사전 초기화 연결.
- `apps/cardrag-worker/src/cardrag_worker/downloader.py`:
  - `download` 루프 내 리다이렉트 호스트 변경 시 RAONK 세션 헤더 필터링 로직 추가.
- `apps/cardrag-worker/src/cardrag_worker/pipeline.py`:
  - `acquire` 내 다운로드 실패 시 `adapter.invalidate_session(current_client)` 호출 연결.
- `apps/cardrag-worker/tests/test_issuers.py`:
  - `WooriClient` mock landing HTML에 RAONK input 태그 추가 및 계약 테스트에서 인증 헤더 검증 단언문 추가.
- `apps/cardrag-worker/tests/test_022_woori_raonk_session_auth.py` (신규):
  - PLAN 요구 6개 시나리오 전체를 검증하는 단위/통합 테스트 7건 작성.

---

## 4. 검증 결과

### 4.1 신규 및 핵심 회귀 테스트
실행 명령:
```bash
uv run --no-sync pytest apps/cardrag-worker/tests/test_issuers.py apps/cardrag-worker/tests/test_issuer_http.py apps/cardrag-worker/tests/test_issuer_collection.py apps/cardrag-worker/tests/test_downloader.py apps/cardrag-worker/tests/test_fix_30_origin_retry.py apps/cardrag-worker/tests/test_022_woori_raonk_session_auth.py -v
```
결과: **83 passed in 5.75s** (100% 통과)

검증된 세부 항목:
1. `test_woori_download_with_cookies_and_auth_headers_via_downloader`: 실제 `RateLimitedClient` 및 `SecurePDFDownloader`를 통한 쿠키 및 2개 헤더 연계 다운로드 성공.
2. `test_woori_client_isolation_and_detail_inventory_reuse`: discovery client A → download client B 간 격리, client C 재개, detail inventory 재이용 시 신선한 세션 획득.
3. `test_woori_concurrent_downloads_single_flight`: 동일 클라이언트 5개 병렬 요청 시 landing GET 정확히 1회만 호출(single-flight).
4. `test_woori_token_extraction_fail_closed_and_sentinel_leakage`: 결손/공백/중복/CRLF/특수문자 fail-closed 및 센티널 보안값 누출 차단.
5. `test_woori_retry_recovers_after_token_refresh`: 첫 토큰 HTML 오류 시 세션 무효화 후 신선한 토큰으로 파이프라인 다운로드 성공.
6. `test_woori_persistent_service_error_fails_bounded_by_maximum_retries`: 지속적 원본 오류 시 `maximum_retries` 한도 내 종료 및 `issuer_origin_service_error` 반환.
7. `test_woori_downloader_cross_host_redirect_strips_auth_headers`: 크로스 호스트 리다이렉트 시 세션 인증 헤더 박탈 검증.

### 4.2 기존 스코프 테스트 확인
실행 명령:
```bash
uv run --no-sync pytest apps/cardrag-worker/tests/test_017_scope.py -k "woori" -q
```
결과: **3 passed in 1.29s** (100% 통과)

### 4.3 정적 분석 및 포맷 검사
실행 명령:
```bash
uv run --no-sync ruff check apps/cardrag-worker/src/cardrag_worker/issuers/woori.py apps/cardrag-worker/src/cardrag_worker/downloader.py apps/cardrag-worker/src/cardrag_worker/pipeline.py apps/cardrag-worker/tests/test_issuers.py apps/cardrag-worker/tests/test_022_woori_raonk_session_auth.py
git diff --check
```
결과: **All checks passed!** (위반 0건, 공백 오류 0건)

---

## 5. 원본 진단 실측 (Sample Live Diagnostic)

운영 DB/캐시/포인터에 일체 쓰지 않고 임시 디렉터리(`tempfile.TemporaryDirectory`)에서 운영 동등 경로(`WooriAdapter` + `RateLimitedClient` + `SecurePDFDownloader`)를 통해 3개 샘플 카드 원본 다운로드를 직접 수행했다.

| 코드 | 상품명 / 파일명 | 크기 (bytes) | 페이지 수 | SHA-256 | 결과 |
|---|---|---|---|---|---|
| 100637 | 배달의민족 비장의카드 V.2<br>`1.(100637)배달의민족 비장의카드 V2_202310.pdf` | 1,141,253 | 2 | `54aacbe92971458c61970e8c7b84aa871319a0ee95b73759329d996e21447b3f` | 일치 (성공) |
| 500107 | 카드의정석2 SOLID<br>`카드의정석2 SOLID 상품안내장.pdf` | 328,332 | 8 | `5f144c6e5dc3b1f18e598e98eef3ef969d93c91db60ca775c6fe7ae1f56cdb69` | 일치 (성공) |
| 104149 | the WON<br>`the WON 상품안내장.pdf` | 4,040,084 | 9 | `0f9589481d5c08e10583d9c78b070b81aa86d9685a9cd869312288b896cedc6d` | 일치 (성공) |

모든 샘플의 크기, 페이지 수, 해시가 PLAN.md 3절의 증거와 정확히 일치함을 확인했다.

---

## 6. 계획과의 차이점 (Deviations)

- `discover_current` 중 mock landing HTML이 주어지는 기존 회귀 테스트(`test_woori_1000_limit_raises_markup_changed` 등)와의 호환성을 위해, `discover_current`에서는 landing HTML에 RAONK input이 없는 경우 조용히 통과하도록 `with suppress(IssuerMarkupChanged):`로 감싸고, 실제 다운로드 계약인 `prepare_download` 단계에서 엄격히 토큰 존재를 강제(`_ensure_session`)하도록 처리했다.

---

## 7. 미수행 운영 범위 및 후속 안내

- **미수행 작업**: 신규 운영 이미지 빌드, 컨테이너 교체 배포, 실패 run `df29d8e5dbb34048942a5296bb2d6ac8`의 재개 실행은 PLAN 지침에 따라 본 과제에서 수행하지 않았다.
- **후속 운영 적용 시 주의점**:
  - 사용자 지시에 따라 timer는 활성 상태를 유지한다.
  - 기존 021 운영 이미지의 OCR call logging 및 020 Paddle 수정사항을 보존한 상태에서 빌드/배포를 수행한다.
  - 당일 실패 run의 일반 resume을 수행하여 922건의 우리카드 수집 및 로컬 배포를 완료하도록 한다.
