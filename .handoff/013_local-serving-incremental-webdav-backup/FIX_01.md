# 013 FIX_01 — WebDAV를 선택적 백업으로 복원하는 요구 보정과 상세 조사

작성: 2026-10-09, Reviewer/Planner / Codex. 상태: 조사·계획 보정 완료, 구현 전.

## 1. 사용자 확정 요구와 본 문서의 우선순위

사용자: “초기 프로젝트의 의도는 로컬 프로젝트의 운영현황과 관계없이 WebDAV는 옵셔널하게 프로젝트를 백업하는 용도다. WebDAV가 없어도 Worker는 구동완료하고 MCP가 정상서빙 되어야 한다.”

이는 013의 최상위 기능요건이다. PLAN.md를 보존하고 이 문서로 보정한다. **현재 WebDAV 의존은 유지해야 할 제품 요구가 아니라 수정해야 할 구현 상태다.** 단순히 현 서버만 local 모드를 명시하는 것으로 끝내지 않는다. 신규 기본 설치도 WebDAV가 필수가 아니어야 한다.

이번 수정은 계획과 조사 결과만 작성한다. 실제 구현·운영 반영 완료로 보고하지 않는다. Executor는 PLAN.md와 본 FIX_01.md를 함께 구현하고, 변경·검증 결과를 FIX_01_REPORT.md에 기록한다. PLAN.md의 일반적인 구현 범위·증분 백업 정책은 유효하며 충돌하는 설정/인수 기준은 본 문서를 우선한다.

## 2. 상세 조사: 언제, 어떻게 의존이 생겼는가

### 2.1 초기 설계는 로컬 운영과 백업을 분리했다

Git `8982934`(2026-08-12)의 문서:

- `docs/02_TARGET_ARCHITECTURE.md` §7: PDF·OCR·published generation을 단일 Linux host의 외부 불변 file volume에 보관. 발행기가 active 참조를 전환하고 online MCP가 승인된 로컬 generation/source view를 read-only로 읽는다.
- `docs/01_PROJECT_OVERVIEW.md`: backup·restore를 v1 개발 범위에서 제외하고 후속 개선 과제로 보류.
- `docs/06_OPERATIONS_AND_DEPLOYMENT_GUIDE.md` §13: backup를 현재 release 차단조건/구현 완료조건/운영 보장으로 사용하지 않음.

초기 문서는 당시 PostgreSQL 등을 포함하지만 **운영 publication과 backup을 분리하는 구조**를 확인할 수 있다. 그 자료가 “WebDAV optional”을 직접 명시했다고 주장하지 않는다. 현재 사용자가 명확히 한 WebDAV의 역할은 이 경계에 맞는다.

### 2.2 2026-08-25 분리 개편에서 WebDAV가 전달 경로로 들어갔다

커밋 `20ee3451a803fed33eabcf7c6a69044f9713c2ba`:
`refactor: split CardRAG worker and MCP runtime`.

이 커밋의 README는 Worker가 immutable SQLite generation을 WebDAV에 게시하고 MCP가 WebDAV에서 generation 및 PDF를 다운로드한 뒤 전환한다고 명시한다. 같은 커밋의 Worker `_run`도 이미 `require_webdav=True`였고 MCP `create_app`도 WebDAV reader/updater를 연결했다. README에서 WebDAV가 처음 추가된 커밋을 `git log --reverse -S 'WebDAV' -- README.md`로 확인했다.

따라서 이번 009/011 요약 분류나 10월 9일 배치에서 새로 생긴 의존이 아니다. **8월 25일 런타임 분리 개편부터 현재까지 이어진 설계 변경**이다. Git 문서·코드에서 변경 시점은 확인되지만 당시 사용자의 승인 여부까지 추정하지 않는다.

### 2.3 현재 동작: 요청 서빙과 신규 갱신을 구분해야 한다

| 상황 | 현재 구현 |
|---|---|
| MCP에 유효한 local generation과 PDF가 이미 있음 | WebDAV 없이 기존 데이터를 서빙 가능 |
| WebDAV poll 실패, 기존 local generation 정상 | 실패 로그를 남기고 last-good 서빙 유지 |
| Worker 기본 run에서 WebDAV 설정 없음 | 시작 설정 검증 실패 |
| Worker 처리 후 원격 publication 실패 | 기본 성공 publication 완료로 처리되지 않음 |
| Worker WebDAV skip | local_only/published=false. MCP 자동 activation 없음 |
| MCP local state가 비어 있고 WebDAV 없음 | 시작 자체와 별개로 ready=false, 데이터를 서빙하지 못함 |
| 새 Worker 결과를 MCP에 자동 반영 | 현재 기본 연결은 WebDAV reader/updater |

MCP의 전체 조회가 매번 WebDAV를 호출한다는 의미는 아니다. 기존 데이터 서빙은 로컬이다. 문제는 **새 결과의 기본 전달 경로와 Worker 완료 기준이 원격 게시에 묶인 것**이다.

### 2.4 구체적인 코드 근거

1. `apps/cardrag-worker/src/cardrag_worker/cli.py:677`:
   `_run`에서 `WorkerSettings.from_env(require_providers=True, require_webdav=True)`.
   이후 `WebDAVClient.from_env`, aggregation head 확인, OCRResolver와 WorkerPipeline에 같은 client를 전달한다.
2. `settings.py:335` 부근: required 조건에서 base URL이 없으면 ValueError. username/password도 require_webdav에 따라 필수다.
3. `pipeline.py:7576` 부근 `_publish_sealed`: skip이면 local_only 및 published=false, 내부 run은 interrupted. 기본 경로는 remote current 확인·gate·remote publication/reconcile·ready publish 기록을 완료 기준으로 사용한다.
4. `ocr.py`의 `_publish_native_cache`/`_publish_native_ready`: OCR cache read-write 경로에도 원격 게시/재시도가 있다. 최종 generation publisher만 바꿔서는 OCR 중간 단계의 원격 의존이 남을 수 있다.
5. `apps/cardrag-mcp/src/cardrag_mcp/main.py:43` 부근: `store.load_current()`가 먼저 로컬 데이터를 읽는다. `:125` 부근은 URL이 있을 때만 `CoreArtifactReader`와 `WebDAVUpdater`를 추가한다.
6. `updater.py:1075` 부근 `run_forever`: poll 실패는 last_good_available 로그를 남기고 기존 active generation이 있으면 ready를 유지한다.
7. `store.py:440` 부근 `load_current`: local pointer/manifest/DB/vector/PDF로 복원한다. WebDAV 호출 없음. `app.py:598` readiness는 repository.ready 기준이다.
8. `partial_cli.py`는 require_webdav=false와 `LocalWebDAV`를 지원하지만 `LocalWebDAV`는 current=None을 반환하는 local-only stub다. 실제 serving publisher나 MCP local reader가 아니다.
9. `deploy/worker/compose.yaml`, `deploy/mcp/compose.yaml`은 URL의 `${...:?required}` 보간을 사용한다. 양쪽 `compose.secrets.yaml`도 WebDAV secret file을 필수로 요구한다. Python 설정만 수정해도 기본 Compose 단계에서 차단될 수 있다.

## 3. 수행한 제한 검증

- 환경에서 CARDRAG_*만 제거한 별도 Python 프로세스에 `WorkerSettings.from_env(require_providers=False, require_webdav=True)` 실행:
  `ValueError: CARDRAG_WEBDAV_BASE_URL is required` 재현. 실제 OCR/임베딩/원격 요청 없음.
- 기존 MCP 로컬 restart 테스트를 실행한다:
  `uv run --no-sync --all-packages pytest apps/cardrag-mcp/tests/test_updater.py::test_restart_refuses_current_when_a_referenced_pdf_is_missing -q`.
  이 테스트는 로컬 generation이 정상일 때 remote reader 없이 load_current가 성공하고, 필수 로컬 PDF가 없을 때 실패하는 경계를 확인한다. Worker→MCP 자동 local 전달까지 구현됐다는 증거로 사용하지 않는다.
- 실제 운영 WebDAV 연결을 끊거나 live state/타이머를 변경하지 않았다. 운영 전체 Worker 재실행은 조사에 필요하지 않다.

## 4. PLAN에 적용할 필수 보정

### 4.1 기본 동작은 local, WebDAV는 명시적 선택

- PLAN 설정표의 `CARDRAG_PUBLICATION_TRANSPORT` “호환 기본 webdav”를 **기본 local**로 보정한다. legacy WebDAV serving은 명시적 remote-distribution 옵션으로만 남긴다.
- WebDAV URL/인증 파일/서버가 없는 새 설치도 Worker와 MCP의 기본 Compose 및 app 설정이 유효해야 한다.
- backup enable/disable을 명시적으로 지원한다. 신규 기본 설치는 disabled, 운영 전환에서는 기존 설정을 확인해 사용자가 원한 OCR 증분 백업을 명시적으로 enabled한다. URL 존재만으로 MCP remote updater를 켜지 않는다.
- `CARDRAG_BACKUP_MODE` immediate/hybrid/manual은 backup enabled일 때의 주기 옵션이다. **disabled와 manual을 구분한다.** disabled는 원격 client 생성·원격 점검·백업 timer 실행·원격 metadata 조회를 하지 않는다.
- backup disabled에서는 영구 pending queue/pin을 쌓아 디스크가 증가하지 않게 한다. 정상 로컬 OCR 보존은 기존 데이터 정책으로 유지하며, 향후 enable 시 명시적 inventory/bootstrap로 백업 대상을 구성할 수 있다.
- backup enabled인데 URL/인증이 없거나 잘못된 경우에도 serving 전체 시작을 실패시키지 않는다. backup_config_error 등 백업 상태에만 반영하고 경고한다. 잘못된 local publication/root 설정이나 실제 OCR/export 오류는 계속 정상 실패로 처리한다.

### 4.2 기본 Worker의 완료 정의

- 정상 수집·OCR·구조화·임베딩·local export/publication이 끝나면 succeeded/exit0로 완료한다. WebDAV 유무가 이 완료를 좌우하지 않는다.
- WebDAV disabled이면 `backup_status=disabled`, pending 수는 해당 정책에 맞게 0/비대상으로 표시한다. enabled 상태의 실패는 pending/failed/degraded 및 미백업량으로 노출하되 local published를 되돌리지 않는다.
- no_change, 재시도, partial 실행, aggregation head 검증, 수동 OCR 재처리 완료 receipt, retirement baseline은 local serving source를 사용한다. remote backup receipt를 이들 기능의 성공 근거로 요구하지 않는다.
- local-only 개발 실행과 정상 local serving publication을 명시적으로 구분한다. 정상 local 설치를 기존 interrupted/local_only semantics에 억지로 넣지 않는다.

### 4.3 MCP의 독립성

- MCP는 로컬 serving volume의 완료 generation을 읽고 기존과 같은 검증·원자적 activation을 수행한다. summary/bundle/PDF/query 경로에 WebDAV 자격증명이 필요하지 않다.
- 로컬 state가 비어 있더라도 Worker가 이미 게시한 serving volume에서 시작하여 ready가 될 수 있어야 한다. 이는 호스트 완전 폐기 시 복구와 다른 시나리오다.
- MCP 재시작 때도 정상 serving source/local state만으로 서비스가 살아난다. WebDAV backup 장애가 readiness를 낮추지 않는다.
- 아직 Worker 데이터가 전혀 없으면 ready=false가 정상이다. “WebDAV가 없어도 정상”은 빈 corpus를 성공 데이터로 위장하라는 요구가 아니다.
- backup는 Worker 산출물의 별도 소비자다. MCP의 기본 설정·secrets overlay에서 WebDAV 항목을 제거/별도 optional overlay로 분리한다. embedding 질의 API 같은 다른 필수 기능 설정은 이번 요구와 구분한다.

### 4.4 백업을 운영 publication 전제조건으로 재도입하지 않기

- backup intent 기록/flush는 local 결과의 소비 단계로 둔다. 특정 remote pointer/remote current baseline이 없으면 로컬 publish를 막는 조건은 금지한다.
- backup queue 장애·ledger 불가용도 local serving 완료와 분리해 보여준다. 알려진 OCR 미백업을 “안전한 원격 백업 완료”로 숨기지는 않는다.
- 이전 WebDAV generation은 전환/복구 참고자료로만 사용한다. local migration 이후 baseline은 local committed head다.
- 대형 derived backup 비활성화, 7회/30건/1GiB/age 선택형 정책, 신규 객체 1회 최종 검증, 정기 표본 audit는 PLAN 그대로 유지한다.

## 5. 보강 인수 기준 — 모두 WebDAV 없는 환경을 먼저 검증

아래는 작은 fixture/mock provider로 자동 검증 가능하다. 실제 2시간 Worker나 유료 모델 호출을 인수 조건으로 추가하지 않는다.

1. URL/username/password/secret file을 전부 제외한 기본 Compose 설정이 성공한다. 기본 local source는 명시되어 있고 WebDAV secret 미존재가 설정 검증에 걸리지 않는다.
2. 빈 Worker state에서 WebDAV 설정 없이 정상 fixture 작업을 끝내고 local generation을 게시한다. exit0/succeeded, backup disabled, WebDAV client/network 호출0.
3. 빈 MCP state + 위 serving volume + WebDAV 설정 없음으로 ready=true. summary·bundle·PDF·기존 검색 fixture 정상.
4. 두 번째 변경 generation을 게시하여 MCP가 자동 반영한다. old local snapshot을 서빙한 것만으로 성공 처리하지 않는다.
5. Worker/MCP를 재시작해도 같은 데이터가 로드되고, 다음 no_change 및 변경 작업이 원격 baseline 없이 동작한다.
6. WebDAV backup enabled + unreachable/mock503에서도 local publication/MCP activation 성공. pending/실패 상태는 별도 표시하고 후속 backup flush로 provider 호출0 복구한다.
7. backup disabled는 pending pin을 무한 누적시키지 않음, 다시 enabled 시 현재 OCR inventory를 백업할 수 있음. 기존 Paddle/adopted OCR도 재호출 없이 복구 가능.
8. remote-distribution legacy 모드는 명시적 선택일 때만 기존 조건으로 작동한다. 해당 모드 회귀 테스트가 local 기본 경로에 원격 필수설정을 강요하지 않아야 한다.

## 6. 판정

사용자가 지적한 최초 운영/백업 분리 의도와 현재 구현 사이에 차이가 확인된다. 이전 답변의 “WebDAV가 MCP 갱신 경로다”는 **현재 코드 상태 설명**으로는 맞지만, 이를 유지해야 하는 설계 요구로 받아들여서는 안 된다.

013은 단순 백업 주기 조절이 아니라 **로컬 운영의 독립성 복원 + 선택적 WebDAV 백업 경량화**로 인수한다. 본 조사에서는 코드/운영 변경 없이 이 보정 문서를 추가한다.

### 제한 검증 완료 결과

§3의 MCP 로컬 restart 테스트: **1 passed in 0.04s**, exit0. 원격 접속이나 운영 재기동 없이 확인했다. 새 local publication/reader 구현은 아직 없으므로 §5의 전체 독립 운영 인수 기준은 구현 후 검증 대상이다.
