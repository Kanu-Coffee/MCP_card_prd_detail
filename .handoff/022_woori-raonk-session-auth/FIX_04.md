# 022 FIX_04 — Cookie Path 후행 슬래시 보존

작성일: 2026-10-30 KST  
역할: Reviewer  
판정: 신규 회귀 1건 수정 필요. 운영 인수 보류.

## 검토 및 확인된 개선

PLAN, REPORT, FIX_01~03와 각각의 보고서, 현재 Git status 및 어댑터·테스트 코드를 검토했다. FIX_03의 기존 재현 조건은 해결됐다. 전체 쿠키 key 비교와 discovery/prepare 초기화 단일화는 타당하다. 이번 요청은 FIX_03 수정 중 새로 도입된 Path 정규화 한 곳에 한정한다.

Reviewer가 직접 확인한 결과:

```bash
uv run --no-sync pytest apps/cardrag-worker/tests/test_issuers.py apps/cardrag-worker/tests/test_issuer_http.py apps/cardrag-worker/tests/test_issuer_collection.py apps/cardrag-worker/tests/test_downloader.py apps/cardrag-worker/tests/test_fix_30_origin_retry.py apps/cardrag-worker/tests/test_022_woori_raonk_session_auth.py apps/cardrag-worker/tests/test_017_scope.py apps/cardrag-worker/tests/test_pdf_cache.py -q
# 173 passed in 14.74s
uv run --no-sync ruff check apps/cardrag-worker/src/cardrag_worker/issuers/woori.py apps/cardrag-worker/src/cardrag_worker/downloader.py apps/cardrag-worker/src/cardrag_worker/pipeline.py apps/cardrag-worker/tests/test_issuers.py apps/cardrag-worker/tests/test_022_woori_raonk_session_auth.py apps/cardrag-worker/tests/test_017_scope.py
# All checks passed!
git diff --check
# 통과
```

이번 리뷰는 로컬 검증만 수행했다. 원본 실사·운영 이미지 빌드·배포·resume은 수행하지 않았다.

## [P2] 서로 다른 Cookie Path를 합쳐 세션 쿠키가 유실됨

위치: `apps/cardrag-worker/src/cardrag_worker/issuers/woori.py:213–214`.

새 `_normalize_cookie_path()`의 `rstrip('/')`는 `/dcpc`와 `/dcpc/`를 같은 key로 만든다. 그러나 현재 httpx CookieJar는 이 두 쿠키를 별도 identity로 보존하며 요청 path에 따라 전송 범위도 달라진다. capture에서 마지막 쿠키 하나만 남고, pipeline의 jar clear 후 restore 시 다른 쿠키는 사라진다. 삭제 처리 역시 한 path에 대한 삭제가 다른 path의 snapshot까지 제거할 수 있다. `/other/`를 `/other`로 바꾸는 동작은 안전한 cookie identity 정규화가 아니다.

## 직접 재현

```bash
uv run --no-sync python - <<'PY'
import sys, asyncio, httpx
sys.path.insert(0, 'apps/cardrag-worker/src')
from cardrag_worker.issuers.woori import WooriAdapter

async def main():
    def handler(request):
        return httpx.Response(200, headers=[
            ('Set-Cookie', 'SID=plain; Path=/dcpc; Secure'),
            ('Set-Cookie', 'SID=slash; Path=/dcpc/; Secure'),
        ], text='<input id="raonkAuthKey" value="KEY"><input id="raonkAuthPageId" value="PAGE">')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = WooriAdapter()
        await adapter._ensure_session(client)
        print('initial jar:', [(c.name, c.path, c.value) for c in client.cookies.jar])
        print('snapshot count:', len(adapter._sessions[client].cookies))
        client.cookies.clear()
        await adapter._ensure_session(client)
        print('restored jar:', [(c.name, c.path, c.value) for c in client.cookies.jar])
        print('cookie to /dcpc:', client.build_request('GET', 'https://pc.wooricard.com/dcpc').headers.get('cookie'))

asyncio.run(main())
PY
```

현재 출력:

```text
initial jar: [('SID', '/dcpc', 'plain'), ('SID', '/dcpc/', 'slash')]
snapshot count: 1
restored jar: [('SID', '/dcpc/', 'slash')]
cookie to /dcpc: None
```

실제 우리카드의 현재 live cookie 구성이 이렇다는 주장은 아니다. Cookie identity 보존을 변경한 부분에서 직접 재현한 회귀다.

## 수정 요구·완료 조건

1. CookieJar가 제공한 비어 있지 않은 path는 그대로 key에 사용한다. 빈 path를 `/`로 다루는 기존 기본값 이상의 URL 정규화나 슬래시 제거를 하지 않는다. domain/session 로직을 새로 재설계할 필요는 없다.
2. 같은 name/domain에 `/dcpc`와 `/dcpc/` 두 path를 설정한 fixture를 추가한다. capture/문서 경계 clear/restore 후 두 Cookie 객체의 속성과 각 요청의 Cookie 헤더가 전과 같음을 확인한다. 한 path만 삭제할 때 다른 path snapshot이 보존되는 것도 확인한다. 기존 FIX_03의 필수 key 삭제 시 fresh landing 동작은 유지한다.
3. 위 173개 묶음에 신규 테스트를 더해 재실행하고 ruff/diff check를 확인한다. 작은 cookie identity 수정이므로 원본 3샘플 재호출은 필수로 요구하지 않으며, 미실행 범위는 보고서에 명시한다.
4. 기존 문서는 보존하고 신규 `FIX_04_REPORT.md`에 변경·결과·운영 미수행 범위를 기록한다. 이번 수정은 운영 배포·run 재개를 승인하는 작업이 아니다.
