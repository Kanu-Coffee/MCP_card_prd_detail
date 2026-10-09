# 013 FIX_03 — FIX_02 재검토 및 남은 복구 계약 수정

작성: 2026-10-09, Reviewer / Codex.
대상 commit: `ca6d5d7690ca5ea0cb2378bcb19279425bc5fcde`.
**판정: FIX_02에서 개선된 부분은 인정한다. 그러나 증분 백업 인덱스의 두 번째 갱신과 복원 OCR의 실제 재사용이 실패하여 013 최종 인수는 보류한다.**

이번 문서는 PLAN/FIX_01/FIX_02를 보존하며, FIX_02_REPORT의 과도한 완료 주장과 남은 필수 항목을 보정한다. 새로운 full Worker 실행·유료 OCR/embedding·2일 관찰 조건은 추가하지 않는다.

## 1. 인정한 개선 및 독립 검증

- optional WebDAV URL의 빈 문자열→None 처리, 일반/선택적 WebDAV secret overlay 분리.
- 동일 run_id 카운터 dedupe, missing source를 lost_source로 유지하는 flush 처리, backup disabled 시 ledger 호출 생략, backup ledger 예외 격리.
- PDF/CAS inventory 등록과 spool 추가, local DB/vector manifest binding, 기존 generation 삭제 대신 동일 READY의 멱등 재사용.
- 기본 세대 2개 보존과 publication 공간 검사, partial local transport 연결.
- 관련 suite 독립 실행:
  `uv run --no-sync --all-packages pytest apps/cardrag-worker/tests/test_local_serving_and_backup.py apps/cardrag-mcp/tests/test_transport.py -q`
  **13 passed in 0.35s**.
- Reviewer가 저장소의 유효한 serving DB fixture를 LocalServingTransport로 게시하고 **실제 WebDAVUpdater.poll_once→GenerationStore activation** 수행: true / `gen-valid`. WebDAV 접속 없이 local reader로 활성화되는 연결은 확인했다. 이 증거는 legacy serving schema의 소형 fixture이며 운영 v5 전체 검증으로 확대 주장하지 않는다.
- 재현 스크립트: `evidence/reviewer_fix02_repro.py`, JSON: `evidence/reviewer-fix02-repro.json`. 실제 Worker WebDAV facade + Core ImmutablePublisher를 사용하고 저장소 I/O만 memory fake로 대체했다. OCR 검증도 실제 OCRResolver cache-only 경로를 사용했다. production/원격 서버/유료 provider 호출0.

## 2. P1 — 두 번째 백업부터 원격 복구 인덱스가 갱신되지 않음

### 실제 실패

`backup.py:648–655`는 고정 주소 `v1/backup/index.json`을 `client.put_bytes`로 게시한다. 실제 WebDAVClient.put_bytes는 기본 immutable이며 Core ImmutablePublisher가 목적지의 기존 SHA를 검증한다. 새 내용으로 덮어쓰는 기능이 아니다.

두 차례 신규 OCR 객체 백업 재현:

- 첫 번째: succeeded, remote index items1.
- 두 번째: **succeeded로 반환**하지만 `immutable destination content does not match requested SHA` 경고.
- remote index items는 계속1. fresh-host restore는 첫 번째 객체1개만 복구.

복구 인덱스 갱신 예외를 warning으로 삼켜 마지막 정상 백업처럼 보이게 하는 것이 직접 원인이다. 객체별 receipts 기록과 spool 삭제가 index commit보다 앞서며, 다음 flush에서 pending이 없으면 unchanged로 반환하므로 index만 재시도할 경로도 없다.

### 수정 지시

- 복구 manifest는 batch별 immutable 주소로 게시하고, 별도 작은 current pointer를 기존 temp/MOVE 원자 게시 방식으로 갱신한다. 기존 serving stable pointer와 분리한다. 또는 동등한 검증된 mutable control publisher를 사용한다. WebDAVClient.put_bytes의 전체 immutable 정책을 해제하지 않는다.
- 객체 검증 완료와 **복구 batch commit 완료**를 구분한다. index/pointer commit 실패는 backup degraded/pending_commit으로 반환하고 재시작 후 재시도 가능해야 한다. last successful backup/counter reset은 완결 commit 이후다.
- index-only retry에서 기존 대형 객체를 다시 upload/readback하지 않는다. commit 완료 후 필요한 spool pin을 해제한다.
- 같은 remote root에서 두 번 서로 다른 OCR을 백업한 뒤 빈 ledger/호스트에서 두 건 모두 복원한다. 실제 ImmutablePublisher/MOVE 계약을 쓰는 fake HTTP 또는 Core test double로 확인한다. 무조건 overwrite하는 mock put만으로 검증하지 않는다.
- index/pointer 게시 자체도 timeout·전송 집계에 포함한다.

## 3. P1 — 복원 파일이 실제 OCR 캐시로 재사용되지 않음

### 실제 실패

현재 restore는 native OCR CAS, `v1/ocr-cache/native/.../manifest.json`, READY를 `target_dir / remote_path`에 복사한다. 실제 로컬 resolver가 쓰는 document-local native-manifest/run index/seed ledger 구조로 import하지 않는다. `v1/caches/ocr` 분기도 default target_dir와 cache/ocr를 다시 결합하여 중첩 경로를 만들 수 있다.

Reviewer는 실제 형식의 native OCR을 생성하여 업로드했고 새 state에 3개 파일을 복원했다:

- backup succeeded / 객체3.
- restore succeeded / 객체3.
- 그 state의 실제 OCRResolver cache-only resolve: **OCRCacheMissError**.
- provider 호출은0. 이는 재OCR을 하지 않아도 복구가 된다는 증거가 아니라, 캐시 miss를 차단하여 복구 불가를 드러낸 것이다.

### 수정 지시

- 기존 seed/cache import 계약을 활용하여 복원한 manifest/input/variant와 OCR 본문을 검증하고 local OCR cache로 등록한다. 복구 인덱스에 source PDF identity/문서 또는 content variant 선택에 필요한 정보를 포함한다.
- native, adopted/Paddle, content-compatible 결과를 로컬 모드에서 사용할 수 있는 형식으로 복구한다. 과거 OCR의 model/provenance를 현재 OCR 계약으로 임의 변경하지 않는다.
- PDF까지 복원한 **빈 state**에서 실제 Worker/OCRResolver가 cache hit로 후속 작업을 수행하도록 테스트한다. 파일 수만 세거나 restore.status만 확인하지 않는다.
- 한 객체 누락/SHA mismatch/잘못된 manifest는 incomplete/failed로 반환한다. 현재 restore는 검증 불일치 파일을 건너뛰어도 succeeded를 반환하므로 성공 정의도 정정한다.
- 원래 호스트의 source folder/ledger가 없는 상태로 테스트하고, live OCR/embedding 호출0을 명시한다. 작은 fixture면 충분하다.

## 4. P1 — 유실 상태·원격 목적지 변경을 아직 정상 백업으로 오인

### 4.1 lost_source가 status에서 제외됨

get_status SQL이 `WHERE status != 'lost_source'`를 적용한다. 파일 유실 flush는 failed/remaining1로 바뀌었지만 status는 **ready / pending_count0 / pending_ocr_count0**로 표시된다. 재현 JSON에 이 모순이 기록돼 있다.

lost_source/error 수와 bytes를 별도 노출하고 전체 백업 상태는 incomplete/failed로 표시한다. 자동 재시도 가능 항목과 유실 항목을 구분할 수는 있으나 유실 건을 “미백업 없음”으로 보고하지 않는다. 복구 가능한 spool/verified CAS가 있으면 해당 상태를 해제한다.

### 4.2 remote_root 컬럼만 추가했고 조회에 scope가 적용되지 않음

record_run_success의 existing_receipts, flush index 구성, restore/audit 조회는 remote_path만 보거나 모든 root의 receipt를 읽는다. 목적지를 바꾼 재현에서 새 root에 올려야 할 기존 객체가 **0건 등록**됐다.

root/account/namespace scope를 canonical하게 정하고 모든 receipt/pending/index 조회에 동일 적용한다. raw base URL 컬럼 추가만으로 해결됐다고 보고하지 않는다. 원격 root A에서 성공한 뒤 B로 변경했을 때 B의 필요한 backup inventory가 다시 구성되고 A의 index를 섞지 않아야 한다. 인증 값은 log/index에 넣지 않는다.

## 5. 이전 지시에서 남은 구현과 증거 정리

아래는 FIX_02에서 이미 요구한 사항이며 새 범위 확대가 아니다. 우선 2–4절을 회귀 테스트로 고정하고, 같은 correction에 필요한 부분만 보완한다.

- **OCR 완료 직후 intent 등록:** 현재도 CLI의 pipeline succeeded/no_change 이후에만 record_run_success가 실행된다. export/embedding 단계 실패 전에 완성된 OCR을 backup intent에 남기는 연결은 없다. spool 기능만으로 이 창이 해결되지는 않는다.
- **단일 backup writer + 재시도 스케줄:** 독립 flush timer/service와 writer lock이 아직 없다. 실패 후 다음 Worker가 없으면 age/재시도가 작동하지 않는다. 예시15분 간격의 단순 명령·timer를 제공하고 disabled/manual 모드를 지킨다. sudo가 필요하면 설치 명령을 REPORT에 제공한다.
- **실제 client timeout/중복 GET:** wait_for는 mock async sleep에는 효과가 있다. 실제 WebDAVClient는 to_thread_fenced로 blocking I/O를 drain하므로 wait_for만으로 요청 시간 상한이 보장되지는 않는다. 남은 예산에 맞는 transfer timeout/안전한 취소를 실제 client 경로에서 확인한다. put_bytes의 temporary/final GET 뒤 추가 GET도 그대로 남아 있으므로 검증 횟수·bytes를 실제 경로 기준으로 줄인다.
- **local publisher 취소·원래 loop guard:** 기존 asyncio.to_thread와 별도 loop callback 방식이 그대로다. 기존 fenced helper와 event loop 경계를 재사용해 중단 후 pointer가 뒤늦게 바뀌지 않도록 한다.
- **serving retention:** 현재 current/previous만 보고 즉시 삭제하며 MCP source-copy pin이 없다. 오래 읽는 source/새 publication이 겹치는 테스트에서 source를 보호한다. 동일 current 세대 재게시 시 previous가 current로 잡혀 직전 rollback 세대가 삭제되는 경계도 확인한다.
- **PublicationResumeSettings 기본값:** 일반 run은 기본 local인데 resume-publication은 기본 webdav다. transport 선택/검증을 통일하여 URL 없이 시작한 run이 URL 없이 재개되게 한다.

### 보고서 E2E 범위 정정

제출 `e2e_local_serving_mcp_test.py`는 create_app을 import하지만 호출하지 않고, GenerationStore/WebDAVUpdater activation·MCP readiness·도구 응답을 확인하지 않는다. products 테이블만 있는 SQLite를 복사하여 직접 SELECT하고 새 LocalArtifactReader로 head를 읽는다. JSON의 `mcp_activated_generation`은 reader가 읽은 ID다.

이를 “MCP 런타임 활성화·재기동 E2E 완료”라고 기록하지 않는다. 기존 소형 valid v5 fixture와 실제 updater/store/app을 활용해 2번째 generation 활성화·summary/bundle/PDF·재시작을 확인한다. Reviewer의 legacy activation 성공은 위1절에 별도 인정했다.

## 6. 제출·인수 기준

- `FIX_03_REPORT.md`에 각 실패의 수정 위치와 실제 결과를 기록한다. 기존 보고서를 덮어쓰지 않는다.
- 두 번 backup→fresh restore 두 건 이상→실제 OCR cache hit, index commit 실패/재시도, lost source 상태, 목적지 변경을 필수 검증한다.
- local transport 정상 활성화와 관련 테스트는 인정하고 불필요하게 반복하지 않는다. 부족한 v5/app 경계만 작은 fixture로 보완한다.
- full Worker 재실행, 새 paid OCR, 2회03시 배치, 2일 대기는 인수 조건이 아니다. 운영 전환은 이 복구 계약 확인 후 진행한다.
- 현재 review는 운영 설정·타이머·컨테이너·WebDAV 객체를 변경하지 않았다. **local 기능 개선을 부정하는 판정이 아니라, 실제 백업 성공·복구 가능 여부를 바로잡는 correction이다.**
