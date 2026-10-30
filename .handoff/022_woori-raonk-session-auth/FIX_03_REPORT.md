# 022 FIX_03 실행 보고서 — 필수 쿠키 전체 식별자 검증 통일 및 세션 생성 단일화

작성일: 2026-10-30 KST  
역할: Executor  
상태: FIX_03 지적사항(쿠키 전체 식별자 정규화 및 유효성 검증, discovery/prepare 세션 생성 단일화) 구현, 신규 테스트 추가(총 16종), 173개 통합 회귀 검증 및 실사 검증 완료 (운영 배포 및 run 재개는 미수행)

---

## 1. 개요 및 배경

Reviewer가 `FIX_03.md`에서 지적한 결함([P2] 필수 쿠키 삭제 검증이 이름(`name`) 집합만으로 수행되어 동일 이름의 다른 path/domain 쿠키가 잔존할 때 세션이 무효화되지 않고 재사용되는 문제, 및 `discover_current`에서 `landing_cookie_keys`가 누락되어 동일 클라이언트 재사용 시 유효성 검사가 생략되는 문제)을 해결했다.

---

## 2. 작업 내역 및 구현 상세

### 2.1 [P2] 쿠키 전체 식별자 `(name, domain, path)` 정규화 및 엄격한 유효성 검증 (`woori.py`)
- **도메인 및 경로 정규화 헬퍼 도입**:
  - `_normalize_cookie_domain(domain)`: 선행 dot(`.pc.wooricard.com` vs `pc.wooricard.com`)을 제거하고 소문자화하여 RFC 6265 호환 정규화를 적용.
  - `_normalize_cookie_path(path)`: 후행 슬래시 및 빈 경로를 통일 (`"/other/"` -> `"/other"`, `""` -> `"/"`).
  - `_cookie_key(name, domain, path)`: capture, update, delete, required-key 검사 전 과정에서 일관된 3원 튜플 식별자(`(name, norm_domain, norm_path)`)를 키로 사용하도록 통일.
- **`_WooriSession.is_valid()`의 전체 식별자 엄격 평가**:
  - 기존의 `landing_names.issubset(current_names)`(이름만 비교)를 폐기하고, `self.landing_cookie_keys.issubset(self.cookies.keys())`(정규화된 전체 식별자 튜플 비교)로 변경했다.
  - 동일한 이름(`SID`)이라도 landing에서 요구한 루트 경로(`/`) 쿠키가 서버에 의해 삭제된 경우, 하위 경로(`/other`)에 같은 이름의 쿠키가 남아 있더라도 결코 유효한 세션으로 인정하지 않는다.
  - 삭제 대상이 아닌 다른 path/domain 쿠키는 스냅샷에 온전히 보존되면서도, 필수 인증 쿠키 결손 시 즉시 `is_valid() == False`로 판정된다.

### 2.2 discovery와 prepare_download의 세션 초기화 단일화 (`woori.py`)
- `discover_current`에서 중복으로 존재하던 landing GET, HTML 파싱, 토큰 추출, 세션 생성 코드를 제거하고, `await self._ensure_session(client)`를 직접 호출하도록 단일화했다.
- 이로써:
  1. discovery 시점에도 `landing_cookie_keys`가 100% 동일하게 캡처되어 동일 클라이언트를 discovery와 download에서 연이어 사용하는 환경에서도 완벽한 쿠키 유효성 검증이 동작한다.
  2. 세션 잠금(Lock) 및 자동 응답 쿠키 추적기 연결이 discovery와 prepare download 간에 일관되게 공유된다.

### 2.3 신규 회귀 및 공유 클라이언트 테스트 추가 (`test_022_woori_raonk_session_auth.py`)
- **테스트 16 (`test_woori_same_name_different_path_cookie_deletion_and_discovery_download_shared_client`)**:
  - landing에서 동일한 이름 `SID`를 갖는 2개 쿠키(`Path=/`, `Path=/other`)와 부모 도메인 쿠키(`Domain=.wooricard.com`)를 동시 발급.
  - 동일 클라이언트 인스턴스로 `discover_current`를 수행하여 `landing_cookie_keys`가 3개 모두 캡처됨을 확인.
  - 서버 응답으로 루트 경로 `SID`(`Path=/; Max-Age=0`)가 삭제되었을 때, `Path=/other` 쿠키는 스냅샷에 보존되지만 `is_valid()`는 `False`로 판정됨을 실증.
  - 동일 클라이언트로 `prepare_download` 호출 시 파이프라인의 `client.cookies.clear()`를 거쳐 fresh landing이 **정확히 1회** 재수행(`landing_calls == 2`)되고, 새로 취득된 `KEY_2`와 `SID=required_2`가 실제 downloader 요청에 사용됨을 검증.

---

## 3. 검증 명령 및 실행 결과

### 3.1 Reviewer 재현 스크립트 실행
Reviewer가 `FIX_03.md`에서 제시한 재현 코드를 실행:
```bash
uv run --no-sync python - <<'PY'
import asyncio, sys, httpx
sys.path.insert(0, 'apps/cardrag-worker/src')
from cardrag_worker.issuers.woori import WooriAdapter, NOTICE_PATH

async def main():
    calls = 0
    def handler(request):
        nonlocal calls
        if request.url.path == NOTICE_PATH:
            calls += 1
            return httpx.Response(200, headers=[
                ('Set-Cookie', 'SID=required; Path=/; Secure'),
                ('Set-Cookie', 'SID=unrelated; Path=/other; Secure'),
            ], text=f'<input id="raonkAuthKey" value="K{calls}"><input id="raonkAuthPageId" value="P">')
        return httpx.Response(200, headers={'Set-Cookie': 'SID=; Path=/; Max-Age=0'})
    adapter = WooriAdapter()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await adapter._ensure_session(client)
        await client.get('https://pc.wooricard.com/delete')
        print('after deletion:', [(c.name, c.path, c.value) for c in adapter._sessions[client].cookies.values()])
        print('valid?', adapter._sessions[client].is_valid())
        client.cookies.clear()
        session = await adapter._ensure_session(client)
        request = client.build_request('POST', 'https://pc.wooricard.com/dcpc/kupload/handler/raonkhandler.jsp')
        print('landing calls:', calls, 'cached key:', session.auth_key, 'download cookie:', request.headers.get('cookie'))
asyncio.run(main())
PY
```
**출력 결과**:
```text
after deletion: [('SID', '/other', 'unrelated')]
valid? False
landing calls: 2 cached key: K2 download cookie: SID=required
```
*(해설: Reviewer 지적 시 `valid? True, landing calls: 1, download cookie: None`이던 문제가 `valid? False, landing calls: 2, download cookie: SID=required`로 완벽히 해결됨)*

### 3.2 022 전용 테스트 스위트 (16개 테스트)
```bash
uv run --no-sync pytest apps/cardrag-worker/tests/test_022_woori_raonk_session_auth.py -v
```
**결과**:
```text
============================== 16 passed in 2.98s ==============================
```

### 3.3 Reviewer 지정 173개 통합 회귀 스위트
```bash
uv run --no-sync pytest apps/cardrag-worker/tests/test_issuers.py apps/cardrag-worker/tests/test_issuer_http.py apps/cardrag-worker/tests/test_issuer_collection.py apps/cardrag-worker/tests/test_downloader.py apps/cardrag-worker/tests/test_fix_30_origin_retry.py apps/cardrag-worker/tests/test_022_woori_raonk_session_auth.py apps/cardrag-worker/tests/test_017_scope.py apps/cardrag-worker/tests/test_pdf_cache.py -q
```
**결과**:
```text
173 passed in 14.86s
```

### 3.4 정적 분석 및 diff 검사
```bash
uv run --no-sync ruff check apps/cardrag-worker/src/cardrag_worker/issuers/woori.py apps/cardrag-worker/src/cardrag_worker/downloader.py apps/cardrag-worker/src/cardrag_worker/pipeline.py apps/cardrag-worker/tests/test_issuers.py apps/cardrag-worker/tests/test_022_woori_raonk_session_auth.py apps/cardrag-worker/tests/test_017_scope.py
# All checks passed!

git diff --check
# 통과 (오류 없음)
```

### 3.5 실제 원본 서버(Live Origin) 3종 카드 실사
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
기존과 100% 동일한 해시 및 바이트 수로 실시간 다운로드 검증 완료.

---

## 4. Git 작업 트리 상태 및 변경 범위

- **022 FIX_03 수정 파일**:
  - `apps/cardrag-worker/src/cardrag_worker/issuers/woori.py`: 정규화 헬퍼(`_normalize_cookie_domain`, `_normalize_cookie_path`, `_cookie_key`), `_WooriSession.is_valid()` 전체 식별자 튜플 평가, `discover_current`의 `_ensure_session` 단일화
  - `apps/cardrag-worker/tests/test_022_woori_raonk_session_auth.py`: 테스트 16 추가 (총 16종)
  - `.handoff/022_woori-raonk-session-auth/FIX_03_REPORT.md`: 본 보고서 신규 생성
- **기존 보존 파일 (020, 021 관련 미커밋/미추적)**:
  - 선행 작업의 모든 파일(`capacity_v5.py`, `cli.py`, `ocr.py`, `paddleocr_runner.py`, `providers.py`, `ocr_logging.py`, `paddle_cleanup.py`, `deploy/worker/cardrag-worker.timer`, `docs/*`, `.handoff/020_*`, `.handoff/021_*`)을 훼손 없이 유지.

---

## 5. 범위 및 잔여 한계

- **운영 배포 및 서비스 재시작 미수행**: 운영 이미지 빌드 및 타이머/서비스 재기동은 사용자의 명시적 배포 지시 전까지 수행하지 않는다.
- **실패 run(`df29d8e5dbb34048942a5296bb2d6ac8`) 재개 미수행**: 운영 데이터베이스에 대한 변경이나 run resume은 Reviewer 최종 승인 및 사용자 승인 후 진행된다.
- **문서 불변성 준수**: 기존의 `PLAN.md`, `REPORT.md`, `FIX_01.md`, `FIX_01_REPORT.md`, `FIX_02.md`, `FIX_02_REPORT.md`, `FIX_03.md`는 수정하거나 삭제하지 않고 그대로 보존했다.
