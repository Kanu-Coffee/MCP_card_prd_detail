# 022 FIX_03 — 필수 쿠키의 전체 식별자 검증 통일

작성일: 2026-10-30 KST  
역할: Reviewer  
판정: 수정 필요. 동일 이름의 다른 path/domain 쿠키가 있는 경우의 인증 갱신 결함 1건으로 인수 보류.

## 검토·직접 검증

PLAN과 이전 REPORT/FIX_01/FIX_01_REPORT/FIX_02의 기록, 신규 FIX_02_REPORT, 현재 Git status 및 코드·테스트를 대조했다. 기존 문서를 변경하지 않았고 운영 배포·run 재개·원본 서버 실사를 이번 리뷰에서 수행하지 않았다.

FIX_02의 기본 쿠키 삭제는 snapshot에 반영된다. 성공 PDF와 304 삭제 테스트, 실제 WorkerPipeline의 동일 slot 두 문서 순차 실행 테스트도 추가됐다. 합성 token 교체와 Git 상태 보고도 보완됐다.

Reviewer 실행 결과:

```bash
uv run --no-sync pytest apps/cardrag-worker/tests/test_issuers.py apps/cardrag-worker/tests/test_issuer_http.py apps/cardrag-worker/tests/test_issuer_collection.py apps/cardrag-worker/tests/test_downloader.py apps/cardrag-worker/tests/test_fix_30_origin_retry.py apps/cardrag-worker/tests/test_022_woori_raonk_session_auth.py apps/cardrag-worker/tests/test_017_scope.py apps/cardrag-worker/tests/test_pdf_cache.py -q
# 172 passed in 14.79s
uv run --no-sync ruff check apps/cardrag-worker/src/cardrag_worker/issuers/woori.py apps/cardrag-worker/src/cardrag_worker/downloader.py apps/cardrag-worker/src/cardrag_worker/pipeline.py apps/cardrag-worker/tests/test_issuers.py apps/cardrag-worker/tests/test_022_woori_raonk_session_auth.py apps/cardrag-worker/tests/test_017_scope.py
# All checks passed!
git diff --check
# 통과
```

## [P2] 필수 쿠키 삭제를 이름 집합만으로 검증하여 세션을 잘못 재사용함

위치: `issuers/woori.py:246–254`의 `_WooriSession.is_valid()` 및 `430–432`의 discovery session 생성.

`landing_cookie_keys`에는 `(name, domain, path)`가 저장되지만 `is_valid()`는 이름만 비교한다. 예를 들어 landing에서 `SID`가 `/`와 `/other`에 각각 발급되고 서버가 `/` 쿠키만 삭제하면 `/other`의 SID가 남아 있다는 이유로 세션이 계속 유효하다. 이 쿠키는 `/dcpc/kupload/handler/raonkhandler.jsp` 요청에 전송되지 않는다. 따라서 fresh landing 없이 과거 RAONK key와 필수 cookie가 없는 요청을 만들어 다시 원본 HTML 오류를 유발할 수 있다.

현재 삭제 테스트는 보존할 다른 path/domain 쿠키의 이름을 `PATH_COOKIE`, `PARENT_COOKIE`로 다르게 지정하여 이 조건을 검증하지 않는다. 실제 우리카드의 현재 cookie 구성이 반드시 이렇다는 주장이 아니라, FIX_02가 요구한 전체 식별자 처리와 다른 path/domain 보존 조건에서 확인한 결함이다.

추가로 `discover_current()`는 `_WooriSession` 생성 시 `landing_cookie_keys`를 전달하지 않아 기본 빈 집합으로 남는다. discovery와 download가 같은 client일 때 landing cookie가 전부 삭제돼도 `is_valid()`가 참이 된다. production pipeline의 별도 download client 경로뿐 아니라 지원하는 동일 client 경로도 통일해야 한다.

### 로컬 재현

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

현재 출력:

```text
after deletion: [('SID', '/other', 'unrelated')]
valid? True
landing calls: 1 cached key: K1 download cookie: None
```

## 수정 요구와 검증

1. 필수 쿠키 유효성을 전체 cookie identity로 평가한다. 정규화가 필요하면 capture/update/delete/required-key 비교에서 일관되게 적용하며, 같은 이름의 다른 domain/path를 대체 쿠키로 인정하지 않는다. 해당 쿠키들은 그대로 보존한다.
2. discovery와 prepare에서 만드는 session 모두 같은 필수 key 집합을 설정한다. 공통 helper 등 작은 수정으로 중복 초기화가 다르게 동작하지 않게 한다.
3. 동일 이름·다른 path/domain의 쿠키가 남은 삭제 사례와 실제 discovery → 같은 client 다운로드 준비 사례를 추가한다. 필요 cookie가 삭제되면 fresh landing이 정확히 한 번 수행되고 새 key/cookie가 실제 downloader 요청에 사용되는지 확인한다. 이미 존재하는 single-flight·호출 예산·다른 cookie 보존·속성 보존 테스트도 유지한다.
4. 위 172개 회귀 묶음과 ruff, diff check를 재실행하고 신규 `FIX_03_REPORT.md`에 결과를 남긴다. 추가 원본 실사는 코드 변경 영향에 따라 필요한 경우에만 PLAN의 3상품 한도에서 수행하며, 미실행이면 범위를 명시한다.

이번 correction은 쿠키 식별자·초기화 보완으로 범위를 한정한다. 서버 호출 방식 자체를 새로 설계하거나 운영 배포·실패 run 재개를 수행하라는 지시가 아니다. 기존 Git 변경과 handoff 이력을 보존한다.
