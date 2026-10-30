# 022 FIX_02 실행 보고서 — 서버 세션 쿠키 삭제 반영 및 파이프라인 순차 정합성 보완

작성일: 2026-10-30 KST  
역할: Executor  
상태: FIX_02 지적사항(5개 항목) 전체 구현, 신규 테스트 2종 추가, 172개 통합 회귀 검증 및 실사 검증 완료 (운영 배포 및 run 재개는 미수행)

---

## 1. 개요 및 배경

Reviewer가 `FIX_02.md`에서 제기한 핵심 결함([P2] 서버의 `Set-Cookie: ... Max-Age=0` 또는 만료 삭제가 세션 snapshot에 반영되지 않아 다음 문서 경계에서 폐기된 세션이 부활하는 문제)과 관련 요구사항 5건을 모두 반영하여 구현 및 검증을 완료했다.

---

## 2. 작업 내역 및 구현 상세

### 2.1 [P2] 서버 Set-Cookie 삭제의 세션 스냅샷 실시간 반영 및 세션 무효화 (`woori.py`)
- **저수준 `CookieJar.clear` 인터셉트 및 타깃 삭제 감지**:
  - `_attach_cookie_tracker`에서 대상 클라이언트의 `target.cookies.jar.clear` 메서드를 래핑하여 서버 응답 처리 시 `CookieJar._cookie_from_cookie_tuple`이 호출하는 `self.clear(domain, path, name)`을 실시간 감지하도록 했다.
  - 파이프라인의 문서 간 경계 초기화인 `client.cookies.clear()`는 인자 없이(`domain=None, path=None, name=None`) 호출되므로, 세션 스냅샷을 건드리지 않고 클라이언트 jar만 비우도록 명확히 분기했다.
  - 서버 응답에 의한 삭제나 명시적 삭제(`domain is not None`)인 경우에만 `session.remove_cookie(name, domain, path)`를 호출하도록 했다.
  - 강한 참조 순환(Client -> Jar -> TrackedClear -> Client)에 의한 메모리 누수를 방지하기 위해 `weakref.ref(target)`와 `weakref.ref(self)`를 적용했다.
- **쿠키 `(name, domain, path)` 식별자 기반 정확한 삭제**:
  - `_WooriSession.remove_cookie(name, domain, path)`를 구현하여 동일한 이름을 갖더라도 path나 domain이 다른 쿠키, 또는 다른 도메인의 쿠키는 절대 삭제되지 않도록 엄격하게 매칭했다.
  - 도메인의 선행 dot(`.pc.wooricard.com` vs `pc.wooricard.com`) 차이를 정규화하여 RFC 6265 호환성을 보장했다.
- **세션 유효성 판정 (`is_valid()`) 및 Fresh Landing 재인증**:
  - landing GET 시점에 수신된 필수 세션 쿠키 키 집합(`landing_cookie_keys`)을 `_WooriSession`에 기록하도록 했다.
  - `_WooriSession.is_valid()` 메서드를 추가하여:
    1. 쿠키 만료 여부(`is_expired()`)
    2. landing 쿠키 중 하나라도 서버에 의해 삭제되고 회전되지 않았는지 여부(`landing_names.issubset(current_names)`)
    를 엄격히 평가한다.
  - 서버가 세션 쿠키를 삭제하여 `is_valid()`가 `False`가 되면 캐시를 즉시 파기(`_sessions.pop`)하고, 다음 문서 준비 시 신선한 landing GET을 통해 새로운 세션과 RAONK 인증 토큰을 안전하게 재취득한다.

### 2.2 304 Not Modified 응답 세션 동기화 보강 (`pipeline.py`)
- `downloader.download`가 304 응답을 수신하여 `PDFNotModified` 예외를 발생시킬 때도 `adapter.sync_session(current_client)`가 호출되도록 `pipeline.py`의 `except PDFNotModified as not_modified:` 블록에 명시적 동기화를 추가했다.

### 2.3 합성 테스트 픽스처 토큰 정규화 (`test_017_scope.py`, `test_issuers.py`)
- `test_017_scope.py` 및 `test_issuers.py`의 모의 landing HTML에 존재하던 실제 발급 번호 형태의 숫자 문자열(`2026103014070794786691031`)을 명백한 합성 토큰인 `SYNTHETIC_AUTH_KEY_FIXTURE_017`, `SYNTHETIC_AUTH_KEY_FIXTURE_001`로 전면 교체했다.
- 저장소 전체 grep 검색을 통해 해당 형태의 의사 토큰이 남아있지 않음을 확인했다.

### 2.4 신규 회귀 및 파이프라인 순차 테스트 추가 (`test_022_woori_raonk_session_auth.py`)
- **테스트 14 (`test_woori_server_set_cookie_deletion_on_pdf_and_304_responses`)**:
  - 200 PDF 응답 및 304 Not Modified 응답에서 서버가 `Set-Cookie: SID=; Path=/; Max-Age=0`을 반환할 때 클라이언트 jar 및 세션 스냅샷에서 `SID`가 정확히 제거됨을 검증.
  - 다른 path(`/other`)나 parent domain(`.wooricard.com`)의 쿠키는 그대로 보존됨을 확인.
  - 다음 문서 다운로드 준비 시 삭제된 이전 쿠키가 부활하지 않고, fresh landing으로 새 세션을 획득함을 검증.
- **테스트 15 (`test_woori_worker_pipeline_sequential_documents_same_slot_recovers_after_server_deletion`)**:
  - **실제 `WorkerPipeline`**을 단일 슬롯(`pdf_concurrency=1`, `pdf_concurrency_per_issuer=1`)으로 구동하여 동일 슬롯의 2개 문서가 순차 처리되는 과정을 검증.
  - 문서 1 다운로드 성공 후 서버의 `Max-Age=0` 쿠키 삭제가 발생.
  - 파이프라인의 문서 간 경계인 `client.cookies.clear()`를 거쳐 문서 2가 동일 클라이언트로 처리될 때, 이전 세션 부활 없이 새로운 landing 호출(`PIPELINE_KEY_2`, `landing_sess_2`)을 정상 수행함을 실증.

---

## 3. 검증 명령 및 실행 결과

### 3.1 Reviewer 재현 스크립트 실행
Reviewer가 `FIX_02.md`에서 제시한 재현 코드를 실행하여 결함 해결을 확인:
```bash
uv run --no-sync python - <<'PY'
import asyncio, sys, httpx
sys.path.insert(0, 'apps/cardrag-worker/src')
from cardrag_worker.issuers.woori import WooriAdapter, NOTICE_PATH

async def main():
    def handler(request):
        if request.url.path == NOTICE_PATH:
            return httpx.Response(
                200,
                headers={'set-cookie': 'SID=old; Path=/; Secure; HttpOnly'},
                text='<input id="raonkAuthKey" value="K"><input id="raonkAuthPageId" value="P">',
            )
        return httpx.Response(200, headers={'set-cookie': 'SID=; Path=/; Max-Age=0'}, text='ok')

    adapter = WooriAdapter()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await adapter._ensure_session(client)
        await client.get('https://pc.wooricard.com/delete')
        print('jar after server deletion:', list(client.cookies))
        print('snapshot after deletion:', [c.value for c in adapter._sessions[client].cookies.values()])
        client.cookies.clear()  # pipeline의 다음 문서 경계
        await adapter._ensure_session(client)
        print('jar after next prepare:', dict(client.cookies))

asyncio.run(main())
PY
```
**출력 결과**:
```text
jar after server deletion: []
snapshot after deletion: []
jar after next prepare: {'SID': 'old'}
```
*(해설: delete 후 snapshot이 정상적으로 `[]`로 비워졌으며, 이후 `prepare` 시 서버 폐기 세션을 되살리지 않고 fresh landing을 재호출하여 새 landing 세션 쿠키를 정상 취득함)*

### 3.2 022 전용 테스트 스위트 (15개 테스트)
```bash
uv run --no-sync pytest apps/cardrag-worker/tests/test_022_woori_raonk_session_auth.py -v
```
**결과**:
```text
============================== 15 passed in 3.20s ==============================
```

### 3.3 Reviewer 지정 172개 통합 회귀 스위트
```bash
uv run --no-sync pytest apps/cardrag-worker/tests/test_issuers.py apps/cardrag-worker/tests/test_issuer_http.py apps/cardrag-worker/tests/test_issuer_collection.py apps/cardrag-worker/tests/test_downloader.py apps/cardrag-worker/tests/test_fix_30_origin_retry.py apps/cardrag-worker/tests/test_022_woori_raonk_session_auth.py apps/cardrag-worker/tests/test_017_scope.py apps/cardrag-worker/tests/test_pdf_cache.py -q
```
**결과**:
```text
172 passed in 15.40s
```

### 3.4 정적 분석 및 diff 검사
```bash
uv run --no-sync ruff check apps/cardrag-worker/src/cardrag_worker/issuers/woori.py apps/cardrag-worker/src/cardrag_worker/downloader.py apps/cardrag-worker/src/cardrag_worker/pipeline.py apps/cardrag-worker/tests/test_issuers.py apps/cardrag-worker/tests/test_022_woori_raonk_session_auth.py apps/cardrag-worker/tests/test_017_scope.py
# All checks passed!

git diff --check
# 통과 (오류 없음)
```

### 3.5 실제 원본 서버(Live Origin) 3종 카드 진단
다운로드 핸드셰이크 정합성 확인을 위해 원본 서버 3종 카드에 대해 재실사를 수행:
```bash
uv run --no-sync python - <<'PY'
import asyncio, tempfile
from datetime import UTC, date, datetime
from pathlib import Path
import httpx

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
PY
```
**출력 결과**:
```text
DIAG: code=100637 bytes=1141253 pages=2 sha256=54aacbe92971458c61970e8c7b84aa871319a0ee95b73759329d996e21447b3f
DIAG: code=500107 bytes=328332 pages=8 sha256=5f144c6e5dc3b1f18e598e98eef3ef969d93c91db60ca775c6fe7ae1f56cdb69
DIAG: code=104149 bytes=4040084 pages=9 sha256=0f9589481d5c08e10583d9c78b070b81aa86d9685a9cd869312288b896cedc6d
```
모든 카드가 기존과 100% 동일한 SHA-256 해시 및 페이지 수로 결손 없이 정상 다운로드됨을 재확인했다.

---

## 4. Git 작업 트리 상태 및 변경 범위

현재 Git 작업 트리에는 선행 작업인 `020`(paddle fallback/용량 복구) 및 `021`(OCR 로깅)과 관련된 미커밋/미추적 파일들이 존재하며, 이번 `022 FIX_02` 작업 중에도 해당 기존 변경 사항들을 일체 훼손하지 않고 완전히 보존했다.

- **022 FIX_02 수정 파일**:
  - `apps/cardrag-worker/src/cardrag_worker/issuers/woori.py`: `_WooriSession` 쿠키 삭제/유효성 판정 및 `CookieJar.clear` 훅 적용
  - `apps/cardrag-worker/src/cardrag_worker/pipeline.py`: `PDFNotModified` 예외 경로 세션 동기화
  - `apps/cardrag-worker/tests/test_issuers.py`: 합성 픽스처 토큰 정규화
  - `apps/cardrag-worker/tests/test_017_scope.py`: 합성 픽스처 토큰 정규화
  - `apps/cardrag-worker/tests/test_022_woori_raonk_session_auth.py`: 신규 테스트 2종 추가 (총 15종)
  - `.handoff/022_woori-raonk-session-auth/FIX_02_REPORT.md`: 본 보고서 신규 생성
- **기존 보존 파일 (020, 021 관련 미커밋/미추적)**:
  - `apps/cardrag-worker/src/cardrag_worker/capacity_v5.py`
  - `apps/cardrag-worker/src/cardrag_worker/cli.py`
  - `apps/cardrag-worker/src/cardrag_worker/ocr.py`
  - `apps/cardrag-worker/src/cardrag_worker/paddleocr_runner.py`
  - `apps/cardrag-worker/src/cardrag_worker/providers.py`
  - `apps/cardrag-worker/src/cardrag_worker/ocr_logging.py`
  - `apps/cardrag-worker/src/cardrag_worker/paddle_cleanup.py`
  - `apps/cardrag-worker/tests/test_cli_settings_provider.py`
  - `apps/cardrag-worker/tests/test_paddle_inputs_v130.py`
  - `apps/cardrag-worker/tests/test_ocr_provider_logging.py`
  - `deploy/worker/cardrag-worker.timer`
  - `docs/OPERATIONS.md`, `docs/SIMPLE_RUNTIME.md`, `docs/WORKER_RUNTIME_OPTIONS.md`
  - `.handoff/020_...`, `.handoff/021_...`

---

## 5. 범위 및 잔여 한계

- **운영 배포 및 서비스 재시작 미수행**: 운영 이미지 빌드 및 타이머/서비스 재기동은 사용자의 명시적 배포 지시 전까지 수행하지 않는다.
- **실패 run(`df29d8e5dbb34048942a5296bb2d6ac8`) 재개 미수행**: 운영 데이터베이스에 대한 변경이나 run resume은 Reviewer 최종 승인 및 사용자 승인 후 진행된다.
- **타이머 활성 유지**: 배포 타이머는 활성 상태를 지속한다.
- **문서 불변성 준수**: 기존의 `PLAN.md`, `REPORT.md`, `FIX_01.md`, `FIX_01_REPORT.md`, `FIX_02.md`는 수정하거나 삭제하지 않고 그대로 보존했다.
