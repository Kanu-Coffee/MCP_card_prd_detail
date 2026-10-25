# 019 REVIEW_19 — FIX_30–32 공통 origin retry·HTML 판별 보완 검증 통과

2026-10-22 Codex, Reviewer. FIX_32/REPORT, FIX_30/REPORT/AMENDMENT, FIX_31/REPORT, REVIEW_17–18 및 현재 shared classifier/client/downloader/관련 테스트를 대조했다. 운영 구현·설정·배치·DB·cursor를 수정하지 않았다.

## 판단

**FIX_32 통과. FIX_30–32를 함께 배포 가능한 코드 변경으로 판단하며 추가 필수 FIX는 발견하지 않았다.** 원래 서비스 오류 HTTP200이 bounded retry에 들어가는 공통 경계와 typed 진단을 유지하면서, 이전 script template/빈 오류 slot 오탐 및 hidden void 미탐을 해소했다. 실제 우리 origin 장애 해결/실배치 성공/자동8사 인수 완료와 구별한다.

- 전역 error_container boolean 대신 명시된 page error region의 가시 문구를 기록한다. 정상 목록의 빈 error slot과 다른 상품 설명은 결합하지 않는다. li 등 개별 상품 marker를 전체 응답 장애의 근거로 쓰지 않는다.
- 표준 void 요소는 stack을 열지 않으므로 hidden input/br/img 뒤 형제 Error 본문이 가려지지 않는다. 실제 hidden subtree/script/style/comment/template 제외는 유지한다.
- UTF-8/cp949의 Error heading 또는 명확한 page error 영역·가시 service 문구는 typed error를 반환한다. 원문/암호화 form/cookie/secret이 error payload에 들어가지 않는 회귀를 확인했다.64KiB 및 MIME 경계,기존 PDF/DRM/ZIP validation은 유지한다.
- 공통 유한 retry budget·503 Retry-After cooldown·typed worker/collection 보고·PDF succeeded/skipped 분리를 유지했다. 이를 반복 systemctl start나 무한 재시도 정책으로 바꾸지 않았다.

## Reviewer 직접 재현과 검사

이전과 같은3개 standalone fixture를 다시 실행했다.

```text
normal_script_template => None (기대값 일치)
normal_empty_error_slot => None (기대값 일치)
error_after_hidden_void => visible_service_error_page (기대값 일치)

uv run --no-sync pytest apps/cardrag-worker/tests/test_fix_30_origin_retry.py apps/cardrag-worker/tests/test_downloader.py apps/cardrag-worker/tests/test_017_scope.py -q
87 passed in9.37s

ruff check/format check (issuer_collection.py/test_fix_30_origin_retry.py)
All checks passed! / 2 files already formatted
git diff --check
통과
```

테스트의 서로 다른 issuer client에서 정상 목록 통과/실제 Error 차단, HTML→PDF retry 성공/소진,503 cooldown,기존 source/rotation/download 보안 경계를 확인했다. Executor 보고의 전체1853 passed/11 warnings는 별도 증거이며 Reviewer가 전체 suite를 다시 실행한 것은 아니다. HTML parser는 브라우저 CSS 전체를 실행하는 도구가 아니며, 모든 가능한 웹사이트 오류를 완벽히 구분한다고 주장하지 않는다. 이번 확인된 반례와 명확한 오류 경계의 보완을 승인한다.

## 운영 상태와 인계

actual compose image는 `cardrag-worker:019-fix24-11d1321cc9e6`, round_robin/프록시 빈 값 유지. FIX_30–32는 아직 image에 미반영이다. 배포 시에는 검증된 소스를 포함한 식별 가능한 image/release 및 실제 운영 volume·contract·재개 근거·복구점·lock·용량을 확인해야 한다. 이번 Reviewer 턴에는 새 image/배포/resume/백업 flush를 수행하지 않았다.

MCP source page2건 조회 성공, current=Worker stable `g-666c2d2ee9e24d04acf76eed-d6ffd9a927e9` 유지. timer active/enabled/Persistent=no, next10/23 10:00KST다. 실패한 오늘 우리 slot과 다음 국민 차례를 수작업 변경하지 않았다.

FIX_32_REPORT는 서두에 실제 origin probe 미수행이라고 쓰면서 검증 부분에는1회 probe 결과를 기록해 시행 여부 서술이 일치하지 않는다. 이번 검토는 실제 origin 요청을 반복하지 않았으며 해당 결과를 최신 origin 복구/해결의 독립 증거로 사용하지 않는다. 코드 검증 통과와 별개로 실제 다운로드 회복 여부는 재개 전에 다시 확인해야 한다. 현재까지 여러 source의 실패 관찰을 단일 PDF 등록 오류로 축소하지 않는다. 향후 단일 source만의 등록 오류로 검증되는 경우에는 사용자 승인된 격리/정상본 보호 정책을 적용한다.

배포 승인 가능한 코드와 origin 복구 미확정은 구분한다. 재시도 보완을 적용해도 원본이 계속 HTML이면 유한 retry 후 명확히 실패해야 하며 자동운영 성공을 미리 선언하지 않는다.019는 실제 운영 후속 검증까지 열어 둔다.
