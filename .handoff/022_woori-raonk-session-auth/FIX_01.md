# 022 FIX_01 — 세션 쿠키 정합성과 인증 실패 폐쇄 보완

작성일: 2026-10-30 KST  
역할: Reviewer  
판정: 수정 필요. 운영 적용 인수 보류.

## 리뷰 범위와 증거

PLAN.md, REPORT.md, 현재 Git status/diff, 신규 7개 테스트 전체, 어댑터·downloader·pipeline의 실제 호출 경로를 확인했다. 019 LIVE_REVIEW_12/REVIEW_17 및 021 운영 보고서의 기존 장애·운영 경계도 대조했다. 022에는 기존 FIX가 없으므로 이번 문서가 첫 correction이다. 기존 020/021 미커밋 변경은 이번 리뷰 결함으로 간주하지 않았다.

Reviewer가 직접 수행한 검증:

```bash
uv run --no-sync pytest apps/cardrag-worker/tests/test_issuers.py apps/cardrag-worker/tests/test_issuer_http.py apps/cardrag-worker/tests/test_issuer_collection.py apps/cardrag-worker/tests/test_downloader.py apps/cardrag-worker/tests/test_fix_30_origin_retry.py apps/cardrag-worker/tests/test_022_woori_raonk_session_auth.py apps/cardrag-worker/tests/test_017_scope.py apps/cardrag-worker/tests/test_pdf_cache.py -q
# 164 passed in 13.87s
uv run --no-sync ruff check apps/cardrag-worker/src/cardrag_worker/issuers/woori.py apps/cardrag-worker/src/cardrag_worker/downloader.py apps/cardrag-worker/src/cardrag_worker/pipeline.py apps/cardrag-worker/tests/test_issuers.py apps/cardrag-worker/tests/test_022_woori_raonk_session_auth.py
# All checks passed!
git diff --check
# 통과
```

이번 Reviewer는 원본 서버 재호출·배포·resume을 수행하지 않았다. Executor의 실사 표는 보고된 결과이며, 이 문서의 재현은 로컬 MockTransport만 사용했다.

## 1. [P2] 쿠키 복원이 보안·만료 속성을 잃고 최신 서버 값을 덮어씀

위치: `issuers/woori.py:257–290` (`_capture_cookies`, `_restore_cookies`, `_ensure_session`).

현재 snapshot은 name/value/domain/path 네 값만 보관하고 `Cookies.set()`으로 매번 복원한다. `Secure`, 만료 시각, host-only 여부, HttpOnly 등의 원본 Cookie 속성이 사라진다. 또한 현재 jar에 서버가 갱신한 쿠키가 있어도 landing 시점 값으로 덮어쓴다. 인증 갱신·장시간 배치에서 서버가 교체하거나 만료시킨 세션을 다시 사용하게 되며, 유효한 쿠키를 복원한다는 계약을 지키지 못한다.

Reviewer가 `httpx.MockTransport`에서 `Set-Cookie: SID=old; Path=/; Secure; Max-Age=3600`을 반환하고 `_ensure_session`을 호출하여 재현했다.

1. 최초 cookie: `domain_specified=False`, `secure=True`, expires 존재.
2. pipeline처럼 `client.cookies.clear()` 후 다음 `_ensure_session`: `domain_specified=True`, `secure=False`, expires 없음.
3. 별도 경우에서 client jar의 SID를 서버 갱신을 모사해 `rotated`로 변경한 뒤 다음 `_ensure_session`을 호출하면 SID가 `old`로 되돌아감.

필수 수정:

- Cookie 객체의 유효한 속성을 보존하는 복원 방식 또는 명시적인 세션 소유 설계를 사용한다. snapshot을 복원하더라도 이미 갱신된 현재 jar를 과거 값으로 무조건 덮지 않는다.
- pipeline의 문서별 jar clear와 함께 동작하도록 설계한다. 다른 카드사/다른 문서의 쿠키 격리를 제거하는 식으로 해결하지 않는다. wrapper가 달라도 실제 client identity로 분리하는 장점은 유지한다.
- 만료된 cookie는 영구 session cookie로 부활시키지 않는다. 만료 시 제한된 재취득으로 연결하고, cookie 처리 실패를 광범위 suppress로 성공처럼 숨기지 않는다.
- 두 문서를 같은 실제 pipeline slot/client로 순차 처리하는 회귀 테스트를 추가한다. 서버 cookie 회전, clear 후 Secure/expiry 속성 보존, 만료 후 fresh landing을 포함한다. 준비 함수만 반복 호출한 테스트로 대체하지 않는다.

## 2. [P2] 인증 없는 요청 반환 및 client 간 공유 fallback 경로 제거

위치: `issuers/woori.py:225–248`, `390–398`.

`prepare_download(None, source)`는 인증 확보 없이 정상 DownloadRequest를 반환한다. Reviewer가 직접 호출해 `RAONK_AUTH_KEY`가 없는 요청이 반환됨을 확인했다. REPORT의 “prepare_download 단계에서 엄격히 토큰 존재를 강제”라는 설명과 다르며, PLAN의 인증 실패 폐쇄 조건에도 맞지 않는다. 기존 test_017_scope의 None 호출은 production API를 느슨하게 만드는 근거가 될 수 없다.

또한 weakref/hash를 지원하지 않는 client는 모두 `_fallback_session` 하나를 공유한다. 두 개의 서로 다른 대상이 같은 cached token/cookie snapshot을 사용할 수 있어 client별 격리라는 계약에 예외가 생긴다. 실제 httpx client는 weakref 가능하지만, 지원하지 않는 입력을 전역 공유 session으로 조용히 처리할 이유는 없다.

필수 수정:

- client가 없거나 session 소유를 안전하게 제공할 수 없으면 토큰을 포함하지 않는 제한된 예외로 거부한다. 또는 준비 전용 계약을 별도 순수 함수로 분리하고 다운로드 요청 생성은 인증이 필수인 구조로 만든다.
- 공유 fallback을 제거하거나 모든 지원 대상에서 실client별 격리가 보장되는 방식으로 대체한다. 테스트용 mock 역시 올바른 계약을 구현하게 한다.
- None/지원 불가 client 실패, 새 wrapper/같은 actual client 재사용, 서로 다른 client 격리를 검증한다.

## 3. [P2] 요구 검증 누락과 보고서 과장 보완

`test_woori_client_isolation_and_detail_inventory_reuse`는 client A/B/C 분리만 검사한다. 실제 `DetailInventory`를 생성·주입하거나 hit을 만들지 않으므로 detail inventory 재이용 검증으로 인정할 수 없다. concurrency 테스트도 빠른 동기 MockTransport에서 prepare만 gather하므로 실제 landing 대기·다운로드 중 refresh와 cookie의 정합성을 충분히 검증하지 않는다.

신규 테스트에는 landing 실패 시나리오와 로그/저장 상태 sentinel 검사도 없다. 추출 예외의 `str`에 sentinel이 없다는 검사만으로 로그·DB·seal 보안까지 검증했다고 표현할 수 없다. `discover_current`에서 mock 호환만을 이유로 모든 `IssuerMarkupChanged`를 suppress한 결정도 운영 오류 가시성과 cache-only 경로의 동작을 설명해야 한다.

필수 수정·보고:

- 실제 DetailInventory hit에서 상세 API를 건너뛰어도 download client에 fresh 인증이 확보됨을 검증한다.
- landing 요청을 명시적으로 suspend시키는 async fixture로 single-flight와 서로 다른 client 동시 격리를 검증한다. 갱신 실패·지속 HTML의 호출 상한과 취소 후 lock 회복도 확인한다.
- landing GET 실패/잘못된 markup 시 미인증 download 0회, 제한된 예외, 기존 origin/markup 분류를 검증한다. discovery의 suppress는 실제 의도한 cache-only 수집 정책으로 정당화하고 테스트하거나 제거한다. fixture 호환성은 필요한 fixture를 고쳐 해결한다.
- fixture token sentinel이 실제 run의 log, 보고서, SourceRecord/cache identity·DB 저장 payload에 섞이지 않는지 변경 경로에 맞게 검증한다. unsupported/304/cache 재이용은 기존 회귀 증거와 신규 변경 증거를 구분해 보고한다.
- 실사 명령·환경·코드 hash와 민감정보가 제거된 결과를 재실행 가능한 작은 스크립트 또는 구체 명령으로 남긴다. 기존 보고서는 보존하고 `FIX_01_REPORT.md`에서 검증한 것/미검증인 것을 정확히 정정한다.

## 완료 조건

위 결함을 수정한 뒤 신규 테스트와 위 164개 회귀 묶음, ruff, diff check를 재실행한다. 세 상품 샘플 실사는 PLAN의 제한과 안전한 downloader 경로로 필요한 만큼만 재검증하며 실행 근거를 남긴다. 이번 수정은 배포·실패 run 재개의 승인이 아니다. 기존 운영 이미지와 타이머를 유지하고, 결과를 신규 `FIX_01_REPORT.md`에 기록한다. 재리뷰에서 session/cookie 정합성과 인증 실패 폐쇄를 확인하기 전에는 운영 인수 완료로 표시하지 않는다.
