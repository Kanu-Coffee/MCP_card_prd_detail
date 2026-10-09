# 013 FIX_06 — local mode content OCR 재사용 회귀 수정

작성: 2026-10-09, Reviewer / Codex. 기준 commit3eaa35d, 현재 운영 `/opt/cardrag/013-3eaa35d`.

## 실제 결함과 긴급 조치

운영 run29f7877bcc2e4e13b19447f023975e16에서 신규 상품0 / PDF byte revision1(하나15911), final5520 / unchanged5059를 관측했다. 현재 provider는 opencode, model alibaba-token-plan/qwen3.8-flash, medium, fallback없음. 실제 subprocess 역시 이 설정으로 실행했다. Paddle 프로세스는 확인되지 않았다.

이전 정상 run025ce35739944d8facfdda4c99961f0c와 비교하여 PDF SHA가 같은 기존 문서11건에서 새 OpenCode native 결과 생성 및 OCR SHA 변경을 확인했다(16:2x snapshot). BC1, Hana8, Hyundai2 사례가 포함되고 하나15911의 이전 PDF도 재OCR되었다. 당시 현대15p 문서도 실제 OpenCode 페이지 호출 중이었다. OCR progress는25건씩 출력되므로 오래 걸리는 문서 하나에서 로그가 수분 비는 현상이 나타났다. 이를 단순 cache 처리 지연으로 판단하지 않는다.

이전 generation5519건 중, 구 state-seed5192건에 없고 이전 run의 native-manifest도 없는 문서가323건이다(롯데66/삼성188/신한32/하나8/현대20/우리7/BC1/KB1). 이는 잠재 영향 후보이며323건이 모두 실제 provider 호출됐다는 뜻은 아니다. 앞의 재OCR 확인 문서는 이전 run에 유효한 content OCR binding과 ocr.md가 존재한다.

원인:
- cli.py `_run` local transport일 때 `_ocr_resolver(...,None)`으로 원격 캐시 클라이언트를 제거함.
- ocr.py 초기화에서 `_content_store`는 `webdav is not None`이며 list_children이 있을 때만 만들어짐. local mode에서는 로컬 content 조회도 사라짐.
- pipeline.py cross-run reuse는 prior `native-manifest.json` 존재를 요구해 content/adopted binding의 ocr.md를 건너뜀.
- seed ledger는 과거5192문서 기준이므로 이후 추가된323건을 커버하지 못함. 유효한 이전 OCR이 있는데 cache miss로 provider를 실행함.

정상 신규 OCR이라고 볼 수 없는 배포 회귀다. 추가 비용을 막도록 현재 Worker에 SIGTERM 정상 종료를 요청했다. SIGKILL/자료 삭제/운영 MCP 롤백은 하지 않았다. 종료 처리는 signal cancellation/drain과 기존 checkpoint 보존 경로에 맡긴다. 기존 MCP의03시 정상 generation은 계속 제공한다. 이번 배치를 정상완료/인수 근거로 사용하지 않는다.

## 수정 목표

WebDAV가 없더라도 모든 유효한 로컬 native/content/adopted OCR을 provider 독립적으로 재사용한다. 최신 sealed generation의 PDF/페이지/OCR identity로 검증하며 특정 카드사/상품 특례를 만들지 않는다. 기존 Paddle 결과도 provenance를 유지한다. native manifest를 위조하거나 OCR 내용을 새 모델의 결과로 재표기하지 않는다.

## 구현 지시

1. `apps/cardrag-worker/src/cardrag_worker/ocr.py`, `content_cache.py`, `pipeline.py`에서 local-only content variant lookup을 설계한다. 원격 client 유무와 로컬 reuse 가능성을 분리한다. 기존 immutable local selection/snapshot/seed 및 sealed prior `ocr.md`를 조회할 수 있게 한다.
2. 정상 sealed prior generation binding을 provider-independent source로 사용한다. document ID 우선, 동일 PDF SHA/size/page count fallback은 issuer/기존 conflicting identity guard를 유지한다. seal manifest 및 CAS에 묶인 OCR SHA/size/page cardinality를 검증한다. native-manifest 존재를 content/adopted의 필수조건으로 사용하지 않는다.
3. reused 결과의 provider/model/variant provenance와 expected_ocr_identity를 보존한다. 구 seed의 coverage 한계를 해결하되 최신 authoritative generation과 서로 다른 OCR 결과를 임의 혼합하지 않는다. 신한 carried 자료도 유지한다.
4. prior content가 유효하면 실제 provider_called=false/cache_reused=true. 새/변경 PDF나 의도된 reprocess만 provider 실행. cache miss/providercall reason을 credential-free 로그로 표시하여 재발 관측을 쉽게 한다. 휴일 여부를 하드코딩해 provider를 금지하지 않는다.
5. 새 runtime를 적용하기 전에 기존 Worker가 정상 종료되어 lock을 해제했는지 확인한다. 운영 자료/현재 MCP/state/auth/Paddle/기존WebDAV를 삭제하지 않는다. 이미 재OCR된 결과/체크포인트도 보존하되 prior sealed authoritative content를 무심코 대체하지 않는다. 전체 OCR 재생성으로 검증하지 않는다.

## 이번 correction에 함께 처리할 기존 FIX_05 항목

- backup data processing과 index commit budget을 구분한다.300초 모두 객체 처리 후 index 원자 게시에1초만 남는 상황을 없애고 bounded reserve/작은 batch로 부분진행을 commit한다.
- 동일 canonical remote root/hash/size verified receipts는 다음 retry에서 GET/PUT 없이 재사용하며 commit 전 pending/spool 보존.
- 현재 `uploaded_bytes/count`는 기존 객체 HEAD/GET 확인도 expected_size 누적하므로 processed/verified와 실제 전송량을 구분하거나 미측정임을 명시한다.
- Worker와 background backup의 자원 경합을 줄인다. 사용자에게 초기 정상 백업 트래픽 자체는 허용된다. 원격 백업을 필수 OCR/MCP 갱신 경로로 되돌리지 않는다. 외부 네트워크 단절은 본처리 성공과 격리한다. backup.lock single writer 유지.
- 기존 `cardrag-backup`은 관측 시점 stop 상태다. corrected runtime로 재개할 때 기존 pending/receipts/spool과 이전 remote backup을 유지한다.

## 필수 검증

오프라인 대표 fixture 및 실제 provider-free preflight로:
- seed에 없는 prior content/adopted 문서 + native-manifest 없음 + 유효 sealed ocr.md → 실제 OCRResolver cache hit/provider0.
- 동일 PDF의 새 source identity → 검증된 같은-content 재사용, conflicting prior OCR 거절.
- prior native/Paddle/content/adopted 각각 재사용, provenance 보존.
- 훼손 OCR SHA/size/pages는 거절, 진짜 변경 PDF는 miss/provider1.
- 실제 운영323 후보를 inventory하고 가능한 기존 자료가 provider-free로 모두 해소되는지 확인. 불가 건은 구체적 이유를 기록. live paid calls 금지인 preflight에서 검사한다.
- 느린 mock backup data budget 종료 후 partial index commit 성공 및 retry GET/PUT0; commit 실패 pending 유지; root변경 재검증.

운영 검증은 provider-free reuse inventory가 통과한 뒤 수행한다. 사용자의 운영 반영/Worker 기동 승인은 이미 있지만 잘못된 재OCR가 이어질 상태에서 무조건 재기동하지 않는다. 정상 재기동하면 startup와 최초 진행까지만 확인하고 턴을 종료한다. 배치 완료는 사용자가 통보한다. 별도 유료 전체 run/2일 대기 조건을 추가하지 않는다.

이 correction은 FIX_05의 작업도 포함한다. 완료 시 FIX_06_REPORT에 전체 결과를 기록하고, 실제로 해결한 FIX_05 사항의 대응 FIX_05_REPORT도 생성하여 과거 unreported fix가 반복 실행되지 않게 한다. 기존 문서/보고서를 덮어쓰지 않는다.
