# 022 FIX_01 실행 보고서 — 세션 쿠키 정합성과 인증 실패 폐쇄 보완

작성일: 2026-10-30 KST  
역할: Executor  
상태: FIX_01 지적사항 3건 전체 구현 및 170개 통합 회귀 검증 완료 (운영 배포 및 run 재개는 미수행)

---

## 1. 개요 및 수정 내용

Reviewer가 `FIX_01.md`에서 제기한 세 가지 결함([P2] 쿠키 복원 및 만료/회전 정합성, [P2] 인증 없는 요청 허용 및 공유 fallback 제거, [P2] 요구 검증 누락 및 증거 보강)을 모두 반영하여 구현을 전면 보완했다.

---

## 2. 결함별 상세 조치 내역

### 2.1 [P2] 쿠키 복원이 보안·만료 속성을 잃고 최신 서버 값을 덮어쓰는 결함 해결

- **Cookie 원본 객체 전체 속성 보존**:
  - 기존의 `(name, value, domain, path)` 단순 문자열 튜플 저장 및 `Cookies.set()` 재설정 방식을 폐기했다.
  - `http.cookiejar.Cookie` 객체를 복제(`copy.copy`)하여 맵(`(name, domain, path) -> Cookie`)에 보관하고, 복원 시 `CookieJar.set_cookie()`를 사용하여 원본의 `secure`, `expires`, `domain_specified`, `discard`, `rest` (HttpOnly) 속성을 100% 보존하도록 수정했다.
- **서버 쿠키 갱신/회전(Rotation) 자동 감지 및 보존**:
  - `httpx.AsyncClient`의 `event_hooks["response"]`에 자동 세션 쿠키 추적기(`_attach_cookie_tracker`)를 연결하여, 다운로드 스트리밍을 포함해 서버가 응답에서 `Set-Cookie`로 쿠키를 회전하거나 갱신할 때 실시간으로 세션 스냅샷을 갱신한다.
  - `pipeline.py`의 `downloader.download` 직후에도 `adapter.sync_session(current_client)`를 명시적으로 호출하여 이중으로 안전성을 확보했다.
  - 파이프라인의 문서 간 쿠키 초기화(`client.cookies.clear()`) 후 다음 문서 다운로드 시, 과거 landing 값이 아닌 회전된 최신 쿠키를 복원한다.
- **쿠키 만료 감지 및 신선한 재취득**:
  - `_WooriSession.is_expired()` 메서드를 구현하여 `Cookie.is_expired()`를 평가한다.
  - 만료 시각이 경과한 쿠키가 존재하면 캐시된 세션을 영구 세션으로 되살리지 않고 즉시 폐기하며, 새로운 landing GET을 통해 신선한 세션과 토큰을 획득한다.

### 2.2 [P2] 인증 없는 요청 반환 및 client 간 공유 fallback 경로 제거

- **`client=None` 엄격 거부 (Fail-Closed)**:
  - `prepare_download(None, source)` 호출 시 조용히 비인증 요청을 반환하던 경로를 제거하고, `ValueError("client is required for Woori download session authentication")`를 즉시 발생시키도록 엄격히 차단했다.
  - `test_017_scope.py`에서 `prepare_download(None, ...)`를 호출하던 기존 2개 테스트는 올바른 mock client(`WooriClient()`)를 전달하도록 픽스처를 수정했다.
- **공유 fallback 세션 제거 및 엄격한 클라이언트 격리**:
  - `_fallback_session`, `_fallback_lock`을 완전히 삭제했다.
  - weakref를 지원하지 않는 잘못된 입력에 대해서는 `TypeError`를 명시적으로 발생시킨다.
  - 서로 다른 `httpx.AsyncClient` 인스턴스는 각각 독립적인 `_WooriSession`과 `asyncio.Lock`을 소유한다.
  - 동일한 `httpx.AsyncClient`를 감싼 서로 다른 `RateLimitedClient` 래퍼는 기저 클라이언트 식별자(`_resolve_target`)를 통해 안전하게 동일 세션과 락을 공유한다.

### 2.3 [P2] 요구 검증 누락 및 테스트 보강

- **`discover_current`의 suppress 제거**:
  - mock 호환성만을 위해 삽입되었던 `with suppress(IssuerMarkupChanged):`를 완전히 제거했다.
  - discovery 단계에서도 landing HTML에 RAONK 인증 토큰이 결손/오류인 경우 즉시 `IssuerMarkupChanged`로 fail-closed 된다.
  - `test_017_scope.py`의 모의 landing 응답 픽스처에 올바른 hidden input 태그를 포함하도록 수정했다.
- **실제 `DetailInventory` hit 검증 추가**:
  - 실제 `WorkerState` SQLite 기반 `DetailInventory` 인스턴스를 주입하여, 첫 번째 discovery에서 저장된 후 두 번째 discovery에서 상세 API를 건너뛰고(hit), 이후 신규 다운로드 클라이언트 투입 시 정상적으로 fresh 인증을 획득함을 검증했다 (`test_woori_detail_inventory_hit_ensures_fresh_session_for_download`).
- **Landing 비동기 일시 중단(Suspension) 및 취소 후 락 복구 검증**:
  - landing GET 응답을 `asyncio.Event`로 대기시키는 비동기 픽스처를 통해 단일 비행(single-flight) 대기열 및 Task 취소 시 락이 정상 해제되어 후속 태스크가 세션을 취득함을 검증했다 (`test_woori_single_flight_suspension_and_cancellation_recovery`).
- **미인증 다운로드 0회 및 예외 분류 검증**:
  - landing GET 실패(500 에러) 또는 마크업 파싱 실패 시 실제 다운로드 POST 호출 횟수가 정확히 0회임을 단언했다 (`test_woori_landing_failure_causes_zero_downloads_and_fails_closed`).
- **센티널 비밀값 누출 전방위 검증**:
  - 파이프라인 전체 실행 후 SQLite DB 전체 테이블의 모든 컬럼, `runs/{run_id}/reports/*.json` 보고서 전체, `SourceRecord` 메타데이터 및 디스커버리 페이로드, 캡처된 로그 전체에 센티널 비밀 토큰 문자열이 일체 잔존하지 않음을 검증했다 (`test_woori_sentinel_never_leaks_to_db_reports_or_logs`).

---

## 3. 변경 파일 목록

- `apps/cardrag-worker/src/cardrag_worker/issuers/woori.py`:
  - `_WooriSession` 쿠키 저장소를 `dict[tuple[str, str, str], Cookie]`로 개편 및 `is_expired()`, `update_cookie()` 구현.
  - `_resolve_target`에서 `client is None` 시 `ValueError`, weakref 미지원 시 `TypeError` 강제.
  - 공유 fallback 제거, `_get_cookie_jar`, `_restore_session_cookies_to_client`, `_attach_cookie_tracker`, `sync_session` 구현.
  - `discover_current` suppress 제거.
  - `prepare_download`에서 client 필수 강제.
- `apps/cardrag-worker/src/cardrag_worker/pipeline.py`:
  - `acquire` 내 `downloader.download` 직후 `adapter.sync_session(current_client)` 호출 연결.
- `apps/cardrag-worker/tests/test_017_scope.py`:
  - `test_woori_1000_limit_raises_markup_changed` landing HTML에 RAONK hidden input 추가.
  - `test_woori_zip_recheck_download_preparation`에 mock client 전달.
- `apps/cardrag-worker/tests/test_022_woori_raonk_session_auth.py`:
  - Reviewer 요구사항을 충족하는 6개 신규 테스트(테스트 7~12) 추가 (총 13개 테스트로 확장).

---

## 4. 검증 결과

### 4.1 Reviewer 지정 170개 통합 회귀 테스트 슈트
실행 명령:
```bash
uv run --no-sync pytest \
  apps/cardrag-worker/tests/test_issuers.py \
  apps/cardrag-worker/tests/test_issuer_http.py \
  apps/cardrag-worker/tests/test_issuer_collection.py \
  apps/cardrag-worker/tests/test_downloader.py \
  apps/cardrag-worker/tests/test_fix_30_origin_retry.py \
  apps/cardrag-worker/tests/test_022_woori_raonk_session_auth.py \
  apps/cardrag-worker/tests/test_017_scope.py \
  apps/cardrag-worker/tests/test_pdf_cache.py -q
```
결과: **170 passed in 14.11s** (100% 통과)

### 4.2 신규 작성 테스트 슈트 상세 (`test_022_woori_raonk_session_auth.py`)
실행 명령:
```bash
uv run --no-sync pytest apps/cardrag-worker/tests/test_022_woori_raonk_session_auth.py -v
```
결과: **13 passed in 2.27s**
- `test_woori_download_with_cookies_and_auth_headers_via_downloader`: PASSED
- `test_woori_client_isolation_and_detail_inventory_reuse`: PASSED
- `test_woori_concurrent_downloads_single_flight`: PASSED
- `test_woori_token_extraction_fail_closed_and_sentinel_leakage`: PASSED
- `test_woori_retry_recovers_after_token_refresh`: PASSED
- `test_woori_persistent_service_error_fails_bounded_by_maximum_retries`: PASSED
- `test_woori_downloader_cross_host_redirect_strips_auth_headers`: PASSED
- `test_woori_sequential_documents_preserve_cookie_attributes_and_handle_rotation_expiry`: PASSED (FIX_01 신규)
- `test_woori_prepare_download_rejects_none_and_unsupported_clients`: PASSED (FIX_01 신규)
- `test_woori_detail_inventory_hit_ensures_fresh_session_for_download`: PASSED (FIX_01 신규)
- `test_woori_single_flight_suspension_and_cancellation_recovery`: PASSED (FIX_01 신규)
- `test_woori_landing_failure_causes_zero_downloads_and_fails_closed`: PASSED (FIX_01 신규)
- `test_woori_sentinel_never_leaks_to_db_reports_or_logs`: PASSED (FIX_01 신규)

### 4.3 정적 분석 및 포맷 검사
실행 명령:
```bash
uv run --no-sync ruff check \
  apps/cardrag-worker/src/cardrag_worker/issuers/woori.py \
  apps/cardrag-worker/src/cardrag_worker/downloader.py \
  apps/cardrag-worker/src/cardrag_worker/pipeline.py \
  apps/cardrag-worker/tests/test_issuers.py \
  apps/cardrag-worker/tests/test_022_woori_raonk_session_auth.py \
  apps/cardrag-worker/tests/test_017_scope.py && git diff --check
```
결과: **All checks passed!** (위반 0건, git 공백 오류 0건)

---

## 5. 원본 진단 재실행 근거 (Reproducible Live Diagnostic)

운영 DB/캐시/포인터에 일체 쓰지 않고 독립된 임시 디렉터리에서 `RateLimitedClient`와 `SecurePDFDownloader`를 통해 3개 샘플 카드 원본 다운로드를 재검증했다.

재실행 스크립트 명령:
```bash
uv run --no-sync python -c '
import asyncio, tempfile, httpx
from pathlib import Path
from datetime import datetime, UTC, date
from cardrag_worker.contracts import SourceRecord
from cardrag_worker.downloader import DownloadPolicy, SecurePDFDownloader
from cardrag_worker.issuers.woori import WooriAdapter
from cardrag_worker.rate_limit import IssuerRateLimiter, RateLimitedClient

samples = [
    ("100637", "배달의민족 비장의카드 V.2", "/upload/cardClause/2023/10/13/08455444-775a-4e0c-a180-f43e872b3871.pdf", "1.(100637)배달의민족 비장의카드 V2_202310.pdf", "2"),
    ("500107", "카드의정석2 SOLID", "/upload/cardClause/2026/9/29/c1441ee5-c310-4177-b21f-f102d0f414a3.pdf", "카드의정석2 SOLID 상품안내장.pdf", "1"),
    ("104149", "the WON", "/upload/cardClause/2025/9/16/f224c9bb-287b-4567-8895-576d8b4938ca.pdf", "the WON 상품안내장.pdf", "1"),
]

async def run():
    adapter = WooriAdapter()
    limiter = IssuerRateLimiter(0.5)
    policy = DownloadPolicy(allowed_hosts=frozenset({"pc.wooricard.com"}))
    downloader = SecurePDFDownloader(policy)
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as raw_client:
            client = RateLimitedClient(raw_client, limiter, issuer="woori", purpose="pdf")
            for code, name, path, file_name, version in samples:
                source = SourceRecord(
                    issuer="woori", product_code=code, product_name=name,
                    effective_date=date(2023, 10, 13), source_version=version,
                    source_url=f"https://pc.wooricard.com{path}",
                    source_post_id="diag", file_name=file_name, category="personal",
                    discovered_at=datetime.now(UTC), metadata={"raonk_file_path": path},
                )
                req = await adapter.prepare_download(client, source)
                dest = tmp / f"{code}.pdf"
                d = await downloader.download(client, req, dest)
                print(f"DIAG: code={code} bytes={d.size_bytes} pages={d.page_count} sha256={d.sha256}")

asyncio.run(run())
'
```

실행 출력 결과:
```
DIAG: code=100637 bytes=1141253 pages=2 sha256=54aacbe92971458c61970e8c7b84aa871319a0ee95b73759329d996e21447b3f
DIAG: code=500107 bytes=328332 pages=8 sha256=5f144c6e5dc3b1f18e598e98eef3ef969d93c91db60ca775c6fe7ae1f56cdb69
DIAG: code=104149 bytes=4040084 pages=9 sha256=0f9589481d5c08e10583d9c78b070b81aa86d9685a9cd869312288b896cedc6d
```

3개 샘플 모두 PLAN.md 및 초기 REPORT.md와 동일하게 100% 정합성을 입증했다.

---

## 6. 결론 및 상태 요약

- Reviewer가 제기한 [P2] 3개 항목에 대한 기술적 보완과 신규 단위/통합 테스트 6건 추가 완료.
- 170개 전체 통합 회귀 테스트 통과 확인 (`170 passed in 14.11s`).
- `FIX_01.md` 완료 조건에 따라 본 수정은 배포/실패 run 재개를 포함하지 않으며, 현재 작업 트리는 클린한 상태로 유지되었다.
