# 022 FIX_02 — 서버가 삭제한 세션 쿠키의 재생성 방지

작성일: 2026-10-30 KST  
역할: Reviewer  
판정: 수정 필요, 운영 인수 보류.

## 검토한 근거와 해결된 사항

PLAN.md, REPORT.md, FIX_01.md, FIX_01_REPORT.md, 현재 Git status/diff 및 변경 테스트를 확인했다. FIX_01_REPORT가 있으므로 FIX_01을 반복 실행하도록 요구하지 않고, 남은 문제만 이번 신규 artifact로 인계한다. 기존 020/021 작업의 미커밋 변경은 보존했다.

이전의 Cookie 속성 손실은 Cookie 객체 복제로 수정됐다. None client 거부와 전역 fallback 제거, 실제 DetailInventory hit 테스트, 비동기 landing 취소 후 lock 복구, DB/report/log sentinel 검사도 추가됐다. discovery의 markup 예외 suppress도 제거됐다.

직접 실행:

```bash
uv run --no-sync pytest apps/cardrag-worker/tests/test_issuers.py apps/cardrag-worker/tests/test_issuer_http.py apps/cardrag-worker/tests/test_issuer_collection.py apps/cardrag-worker/tests/test_downloader.py apps/cardrag-worker/tests/test_fix_30_origin_retry.py apps/cardrag-worker/tests/test_022_woori_raonk_session_auth.py apps/cardrag-worker/tests/test_017_scope.py apps/cardrag-worker/tests/test_pdf_cache.py -q
# 170 passed in 14.42s
uv run --no-sync ruff check apps/cardrag-worker/src/cardrag_worker/issuers/woori.py apps/cardrag-worker/src/cardrag_worker/downloader.py apps/cardrag-worker/src/cardrag_worker/pipeline.py apps/cardrag-worker/tests/test_issuers.py apps/cardrag-worker/tests/test_022_woori_raonk_session_auth.py apps/cardrag-worker/tests/test_017_scope.py
# All checks passed!
git diff --check
# 통과
```

원본 서버 호출, 운영 배포, resume은 이번 Reviewer가 수행하지 않았다.

## [P2] 서버 Set-Cookie 삭제가 세션 snapshot에 반영되지 않음

위치: `apps/cardrag-worker/src/cardrag_worker/issuers/woori.py:282–305`, `_attach_cookie_tracker` / `sync_session`, 그리고 `_restore_session_cookies_to_client`.

현재 response hook은 응답을 버린 뒤 jar에 **남아 있는 쿠키를 추가·덮어쓰기만** 한다. 서버가 `Set-Cookie: SID=; Path=/; Max-Age=0` 또는 과거 Expires로 쿠키를 삭제하면 httpx jar에서는 쿠키가 없어지지만 `_WooriSession.cookies`에는 이전 값이 남는다. 다음 준비 단계에서 이 이전 값을 다시 넣어 서버가 폐기한 세션을 부활시킨다. 원래 쿠키에 만료가 없으면 `is_expired()`로도 잡히지 않는다.

이 문제는 성공한 PDF 응답이나 304처럼 downloader 성공 뒤의 경로에서도 생길 수 있다. 일반 서비스 오류 예외에 의한 invalidate만으로 해결되지 않는다. 실제 우리카드가 현재 샘플에서 이 응답을 보내고 있다고 주장하는 것은 아니며, 서버 쿠키 삭제를 올바르게 처리하지 못하는 구현 결함이다.

### 직접 재현 명령

운영 상태나 원본 서버에 접근하지 않는 MockTransport 진단이다.

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

현재 출력:

```text
jar after server deletion: []
snapshot after deletion: ['old']
jar after next prepare: {'SID': 'old'}
```

## 필요한 보완

1. response에 의한 쿠키 삭제와 pipeline의 의도적인 jar clear를 구분한다. 응답 처리 시 삭제를 snapshot에 반영하거나 인증 session을 무효화하고, 다음 문서 경계의 jar clear에서는 유효한 session만 복원한다. 무조건 jar를 snapshot으로 교체해 기존 clear 이후 모든 session 재사용을 깨는 수정은 피한다.
2. 삭제는 cookie의 name/domain/path identity를 기준으로 정확히 처리한다. 같은 이름의 다른 path/domain 쿠키와 다른 카드사 쿠키까지 삭제하거나 복원하지 않는다. 세션 재취득이 필요한 경우 기존 요청 예산·제한된 재시도를 유지한다.
3. 성공 PDF 응답과 304 응답의 Set-Cookie 삭제를 테스트한다. 다음 문서에서 삭제된 값이 다시 전송되지 않고, 필요한 경우 fresh landing으로 인증을 회복하는지 확인한다. `expires`를 private session 객체에서 수동 변경하는 테스트만으로 서버 삭제 회귀를 대체하지 않는다.
4. 적어도 하나는 실제 WorkerPipeline의 동일 slot에서 두 문서를 순차 처리하는 fixture로 검증한다. 기존 sequential 테스트는 downloader 호출과 수동 `cookies.clear()`로 경계를 모사했으며, 실제 pipeline 두 문서 실행의 증거는 아니다. 테스트/보고서에서 구분한다.
5. `test_017_scope.py`에 새로 넣은 실제 발행값처럼 보이는 숫자 token은 명백한 합성 fixture 값으로 바꾼다. 민감한 실제 토큰을 fixture로 저장하지 않는 PLAN 원칙을 따른다.

## 검증 및 결과물

변경 경로의 신규 회귀와 위 170개 묶음, ruff, diff check를 재실행한다. live download 흐름이 달라진 경우에만 PLAN의 3상품 제한된 실사를 다시 수행하고, 그 외에는 재실사 미수행 범위를 정확히 밝힌다. `FIX_02_REPORT.md`에 코드 변경·명령·실제 결과와 남은 한계를 기록한다.

기존 handoff 문서는 수정하지 않는다. FIX_01_REPORT의 “작업 트리는 클린” 표현은 실제 다수 미커밋·미추적 파일이 있는 Git 상태와 다르므로, 다음 보고서에서는 기존 변경 보존과 이번 수정 범위를 정확히 설명한다. 이번 수정도 운영 배포나 실패 run 재개를 승인하지 않는다.
