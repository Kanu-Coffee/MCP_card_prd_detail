# 022 FIX_04 실행 보고서 — Cookie Path 후행 슬래시 보존 및 경로 정합성 완료

작성일: 2026-10-30 KST  
역할: Executor  
상태: FIX_04 지적사항(쿠키 경로 rstrip 슬래시 제거 및 개별 path identity 보존) 구현, 신규 테스트 17 추가(총 17종), 174개 통합 회귀 검증 완료 (운영 배포 및 run 재개는 미수행)

---

## 1. 개요 및 배경

Reviewer가 `FIX_04.md`에서 지적한 회귀 결함([P2] `_normalize_cookie_path`의 `rstrip('/')`로 인해 `/dcpc`와 `/dcpc/`처럼 후행 슬래시 유무만 다른 서로 다른 Cookie Path가 하나의 키로 병합되어 세션 쿠키가 유실되는 문제)을 해결했다.

---

## 2. 작업 내역 및 구현 상세

### 2.1 [P2] Cookie Path 후행 슬래시 보존 (`woori.py`)
- **과도한 슬래시 제거 로직 폐기**:
  - `_normalize_cookie_path(path)`에서 `rstrip('/')` 호출을 제거하고, `return path or "/"`로 변경했다.
  - Python `CookieJar`가 부여한 비어 있지 않은 path 문자열을 있는 그대로 유지하며, 빈 path나 None인 경우에만 기본 루트 경로(`"/"`)로 대체하도록 축소했다.
- **쿠키 식별자 독립성 보장**:
  - `_cookie_key(name, domain, path)`에서 `(name, domain, "/dcpc")`와 `(name, domain, "/dcpc/")`가 서로 다른 고유 키로 분리된다.
  - capture 시 두 쿠키가 모두 스냅샷에 보존되며, 파이프라인의 문서 간 경계 `client.cookies.clear()` 후 `_restore_session_cookies_to_client`에서도 두 쿠키의 고유 속성(`path`, `secure`, `HttpOnly` 등)이 각각 온전히 복원된다.
  - 삭제 처리 시에도 `/dcpc`에 대한 `Max-Age=0` 삭제가 발생하더라도 `/dcpc/` 쿠키는 스냅샷에 독립적으로 보존된다.

### 2.2 신규 회귀 테스트 추가 (`test_022_woori_raonk_session_auth.py`)
- **테스트 17 (`test_woori_cookie_paths_with_and_without_trailing_slash_preserved`)**:
  - landing에서 동일한 name/domain을 가지며 path만 다른 2개 쿠키(`Path=/dcpc`, `Path=/dcpc/`)를 동시 발급.
  - 초기 capture 시 스냅샷에 2개의 쿠키가 고유한 path로 각각 분리 저장됨을 확인(`snapshot count == 2`).
  - `client.cookies.clear()` 후 `_ensure_session`을 통한 restore 시 `CookieJar`에 2개의 쿠키가 원본 path와 속성 그대로 복원됨을 검증.
  - `/dcpc` 요청 시 `SID=plain_1`만 전송되고, `/dcpc/` 요청 시 `SID=slash_1; SID=plain_1`이 전송되어 RFC 경로 매칭이 정확히 유지됨을 확인.
  - `/dcpc` 쿠키만 단독 삭제(`Max-Age=0`)되었을 때 `/dcpc/` 쿠키는 스냅샷에 온전히 보존되면서도, landing 필수 쿠키 결손으로 인해 세션이 올바르게 무효화(`is_valid() == False`)되고 다음 문서 prepare 시 fresh landing 재인증(`KEY_2`, `plain_2`, `slash_2`)으로 안전하게 회복됨을 검증.

---

## 3. 검증 명령 및 실행 결과

### 3.1 Reviewer 재현 스크립트 실행
Reviewer가 `FIX_04.md`에서 제시한 재현 코드를 실행:
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
**출력 결과**:
```text
initial jar: [('SID', '/dcpc', 'plain'), ('SID', '/dcpc/', 'slash')]
snapshot count: 2
restored jar: [('SID', '/dcpc', 'plain'), ('SID', '/dcpc/', 'slash')]
cookie to /dcpc: SID=plain
```
*(해설: Reviewer 지적 시 snapshot count가 1로 축소되고 `/dcpc` 쿠키가 유실되던 문제가 해결되어 snapshot count 2, 복원 후 `/dcpc` 요청에 `SID=plain`이 정상 전송됨)*

### 3.2 022 전용 테스트 스위트 (17개 테스트)
```bash
uv run --no-sync pytest apps/cardrag-worker/tests/test_022_woori_raonk_session_auth.py -v
```
**결과**:
```text
============================== 17 passed in 3.08s ==============================
```

### 3.3 Reviewer 지정 174개 통합 회귀 스위트
```bash
uv run --no-sync pytest apps/cardrag-worker/tests/test_issuers.py apps/cardrag-worker/tests/test_issuer_http.py apps/cardrag-worker/tests/test_issuer_collection.py apps/cardrag-worker/tests/test_downloader.py apps/cardrag-worker/tests/test_fix_30_origin_retry.py apps/cardrag-worker/tests/test_022_woori_raonk_session_auth.py apps/cardrag-worker/tests/test_017_scope.py apps/cardrag-worker/tests/test_pdf_cache.py -q
```
**결과**:
```text
174 passed in 14.75s
```

### 3.4 정적 분석 및 diff 검사
```bash
uv run --no-sync ruff check apps/cardrag-worker/src/cardrag_worker/issuers/woori.py apps/cardrag-worker/src/cardrag_worker/downloader.py apps/cardrag-worker/src/cardrag_worker/pipeline.py apps/cardrag-worker/tests/test_issuers.py apps/cardrag-worker/tests/test_022_woori_raonk_session_auth.py apps/cardrag-worker/tests/test_017_scope.py
# All checks passed!

git diff --check
# 통과 (오류 없음)
```

---

## 4. Git 작업 트리 상태 및 변경 범위

- **022 FIX_04 수정 파일**:
  - `apps/cardrag-worker/src/cardrag_worker/issuers/woori.py`: `_normalize_cookie_path`에서 `rstrip('/')` 제거
  - `apps/cardrag-worker/tests/test_022_woori_raonk_session_auth.py`: 테스트 17 추가 (총 17종)
  - `.handoff/022_woori-raonk-session-auth/FIX_04_REPORT.md`: 본 보고서 신규 생성
- **기존 보존 파일 (020, 021 관련 미커밋/미추적)**:
  - 선행 작업의 모든 파일(`capacity_v5.py`, `cli.py`, `ocr.py`, `paddleocr_runner.py`, `providers.py`, `ocr_logging.py`, `paddle_cleanup.py`, `deploy/worker/cardrag-worker.timer`, `docs/*`, `.handoff/020_*`, `.handoff/021_*`)을 훼손 없이 안전하게 유지.

---

## 5. 범위 및 잔여 한계

- **원격 실사 미수행**: 이번 수정은 내부 쿠키 path 식별자 보존에 국한된 변경이므로, Reviewer 지침에 따라 원본 실사는 추가 수행하지 않고 이전 FIX_03의 실사 결과를 유지한다.
- **운영 배포 및 서비스 재시작 미수행**: 운영 이미지 빌드 및 타이머/서비스 재기동은 사용자의 명시적 배포 지시 전까지 수행하지 않는다.
- **실패 run(`df29d8e5dbb34048942a5296bb2d6ac8`) 재개 미수행**: 운영 데이터베이스에 대한 변경이나 run resume은 Reviewer 최종 승인 및 사용자 승인 후 진행된다.
- **문서 불변성 준수**: 기존의 `PLAN.md`, `REPORT.md`, `FIX_01~03` 및 각 보고서는 수정하거나 삭제하지 않고 그대로 보존했다.
