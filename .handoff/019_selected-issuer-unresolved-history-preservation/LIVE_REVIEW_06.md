# 019 LIVE_REVIEW_06 — 6사 중간점검 및 신한·롯데 프록시 확인

2026-10-20 Codex Reviewer. 사용자 요청에 따라 현재 운영 결과를 점검했다. **6사 본 작업은 정상 종료, 전체 백업 인수는 보완 필요**다. 새 배치·provider·백업 flush·원격 쓰기·DB 수정·배포·삭제는 하지 않았다. 프록시 확인에 한해 사용자 요청 범위에서 두 공개 홈페이지 GET을 수행했고, 백업 조사에는 원격 metadata 4개 GET을 사용했다.

## 6사 실행/게시

활성 release `/opt/cardrag/019-fix16-4108e3dac5f5`. timer/service inactive, 실행 중 Worker 없음.

| issuer | run_id | 결과 | 컨테이너 경과 | 핵심 결과 |
|---|---|---|---|---|
| 우리 | f43e8c37e00f419e984fe15b119fda46 | succeeded | 85분07초 | collection complete, 비선택 변경0 |
| KB | 14706e77682c437c9604f426f313ceaa | succeeded | 100분00초 | 문서751→757, 비선택 변경0 |
| 삼성 | 44d55df9db8645c8b9060dc4631ff463 | succeeded | 87분14초 | collection complete, 비선택 변경0 |
| BC | 8810e405d952441faf9e560ac19fd7b0 | succeeded | 81분25초 | PDF cache122, OCR 재사용122/provider0, embedding provider1 |
| 하나 | f449b5247ed3447ea801fb006a47d177 | no_change | 19분19초 | 목록726 완전 수집·다운로드0, 새 게시 불필요 |
| 현대 | 10f236331a2744f78980f2b878475b64 | succeeded | 105분32초 | OCR provider2/재사용605, embedding provider4, 문서605→607 |

컨테이너6건 모두 exit0/OOM 없음. 경과에는 종료 후 정리 등도 포함돼 pipeline 내부 시간과 다르다. 현대 새 문서2건은 the Purple osée, GENESIS CARD 선택제공서비스 관련 current 문서다. 이것을 원천 사이트의 신규 카드 출시2건으로 해석하지 않는다.

현재 stable은 `g-10f236331a2744f78980f2b8-9d16cfdbae66`, manifest SHA를 pointer와 직접 대조했다. 전체 **5545**: 하나735·삼성766·롯데647·우리980·신한931·현대607·KB757·BC122. 각 실행 당시 baseline/자체 게시의 비선택 변경0이다. 신한/롯데가 아직 검증 전이라는 이유로 기존 문서가 삭제되거나 다른 카드사로 덮인 증거는 없다. unsupported8건은 기존 결과에 남아 있으므로 이 점검을 미지원0 판정으로 해석하지 않는다.

## 백업은 본 작업과 분리해서 판정

summary는 **실행 종료 시점 snapshot**이다. deferred/null은 정상 임계값/읽기 전용 진단일 수 있고, pending0만으로 완료를 뜻하지 않는다.

- 우리/KB/삼성: 보존된 실행 당시 결과 및 이전 운영 검증 이력을 사용했다. KB는 종료 시 complete=True. 삼성 cached unbound2는 FIX_14 이전 snapshot이며 FIX_16 운영 RO 조회에서 정상 정책 제외2로 해소된 이력이 있다. 이 세 run의 폴더는 이후 retention으로 정리되어 이번에 같은 run을 원격 재검증했다고 주장하지 않는다.
- 하나: no_change, 새 게시/백업 단위 없음. scope0/null은 실패가 아니다.
- 현대: **현재도 required1897/bound1894, unbound3**, remaining/lost/missing/conflicts0. 종료 시 degraded/complete=False.
- BC: **현재도 required483/bound478, unbound5**, remaining/lost/missing/conflicts0. deferred/null에 가려져 있지만 완전성 확인이 남아 있다.

### 현대3건 및 BC1건: 과거 OCR CAS 객체의 scope 오탐

현대 세 경로는 과거 OCR SHA `757f2418…`, `a7a981a2…`, `aa02136a…`, BC 한 경로는 `ce35c0b7…`의 `v1/objects/sha256/...`이다. doc_map/doc_info가 현재 문서 ID를 가리키지만 현재 seal의 OCR SHA는 각각 다르다. 현재 cache_kind는 content, PDF identity는 같으며 **현재 run의 실제 OCR 본문 SHA가 현재 seal과 일치**함을 확인했다. 옛 객체 receipt도 존재한다.

`scoped_backup_scope`는 stale tag를 제외할 때 doc_has_bound_metadata를 요구한다. 정상 content에는 문서 소유 native metadata가 없으므로 이 값이 False인 채, 과거 CAS는 마지막 paths.add로 required/unbound가 된다. 이것은 현재 OCR 유실이나 신규 업로드 대기가 아니다. 현재 required CAS의 검증 근거와 과거 객체의 소유 범위를 구분해야 하며, 옛 receipt·객체·다른 보존 세대는 삭제하면 안 된다.

### BC4건: 현재 native pair의 로컬 검증 근거 부족

현재 BC 두 문서가 아래 native key를 사용한다. 각 key의 manifest/READY가 scope에 있지만 로컬 pair가 없어 unbound다. 이번에 실제 원격 pair를 **GET만 하여** canonical JSON, READY 연결, 현재 seal의 PDF SHA/size/page 및 OCR SHA/size를 검증했다. 원격 SHA는 현재 root의 원장 receipt와도 일치했다.

| reuse key | 현재 document_id | manifest SHA | READY SHA |
|---|---|---|---|
| bba8ef70412a10d5589d560964ba75f8b256fea551dfe89eb8641d2050303dd2 | doc_8525c5b21cba2f54cdcbe3be95b1c02589dfcb2bbf3bc92a93012afd8e577760 | f658cf459ea4efac74b6cd007187604792132f98121bd741330c4bdf5ead144c | a04f628d4cb823a6d5fce90bfd750b8ada69e2d9725d6945c0bba2d4d521a218 |
| e7ad55525a3f18d0a9e7a2e7980755a771b6e8952ec76d11d61a2e3587536faa | doc_0d3baaab7cdcdf03c487604146ff3628a021c7cb6f727b66f8f0d37e2b33cf08 | a916db9cbeba87f255d9240ff30c48bfcebf818cf18755eb1371dee8eab73ff7 | 352cef701fc30b2949b913aa80805f276a717413438dccb1741bc2e9e43d245b |

이 4건은 원격 자료가 사라진 상태가 아니라, 정식 검증 근거의 회수·재바인딩이 필요한 상태다.

위 pair를 로컬 cache에 복원하거나 정식 scope 진단에서 검증 회수하는 작업은 이번 리뷰에서 수행하지 않았다. 원격 metadata가 존재하고 현재 문서 identity와 일치한다는 사실과 전체 backup completion=True는 구분한다. 원격 전체 index/body를 새로 전수 다운로드한 것도 아니다.

## 신한·롯데 프록시 간단 확인

실제 운영 worker-compose/env에서 `issuer_proxy_url` 설정 존재를 확인했다. 주소/인증은 출력하지 않았다. 실제 create_issuer_client의 선택 transport:

- `https://www.shinhancard.com:443` → `_IssuerProxyTransport`
- `https://www.lottecard.co.kr:443` → `_IssuerProxyTransport`
- `https://image.lottecard.co.kr:443` → `_IssuerProxyTransport`
- 모바일 롯데 및 우리 host는 일반 transport. 환경 HTTP(S)_PROXY에 의존하지 않는 trust_env=False 및 장애 시 직접 fallback 없음.

실제 운영 설정으로 신한/롯데 홈페이지에 한 번씩 GET: **둘 다 HTTP200**. 목록 수집과 PDF 다운로드 client 생성 두 경로 모두 같은 proxy 설정을 전달하는 코드도 확인했다. 따라서 설정 존재·대상 host route·기본 연결은 정상이다. image host는 route 선택만 확인했고 실제 PDF GET은 하지 않았다. 홈페이지200만으로 전체 목록 파싱·세션·PDF/DRM·모든 redirect의 성공을 보증하지 않는다. 신한/롯데 batch 완주는 여전히 미검증이다.

## 공간·증거·후속

filesystem 약89GiB 여유, 사용77%로 현재 용량 여유가 회복됐다. 자동 timer는 계속 inactive다. 배치 준비 때는 갱신된 capacity observation+전처리 여유를 다시 확인한다.

RO 원장/retained seal·현재 pointer·컨테이너 inspect/log·원격 metadata·실제 proxy transport를 대조했다. 중간 증거는 `/opt/cardrag/current/operations/live/mid-review-20261020/`에 보존했다. 두 retained run의 publish seal·performance 및 audit 출력 약14MB를 저장하여 이후 native retention으로 원본 run이 정리돼도 현재 조사 근거를 남겼다. 대형 DB 복구본은 추가하지 않았다. old run 전부를 retention에 pin하거나 과거 seal을 다시 쓰지 않았다.

FIX_18로 현재 CAS/과거 OCR 객체의 scope 분리와 BC metadata 검증 회수를 국소 보완한다. 신한/롯데 실행 자체의 프록시 차단 징후는 없지만 전체8사 인수·백업 완료·timer 재개는 아직 선언하지 않는다.
