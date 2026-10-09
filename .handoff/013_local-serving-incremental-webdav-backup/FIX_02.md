# 013 FIX_02 — Reviewer 판정 및 필수 보완

작성: 2026-10-09, Reviewer / Codex.
검토 대상: `ea1a4d74967cf42f548d28879df74a9f34dedb9c`와 FIX_01_REPORT.md.
**판정: 구현 방향은 적합하나 현재 인수 보류. 아래 실제 구동·복구·용량 문제를 수정한 후 인수한다.**

PLAN.md, FIX_01.md, FIX_01_REPORT.md, Git diff 및 신규 구현 전체를 검토했다. 기존 012의 untracked 운영 검토/증거는 보존했다. 운영 재배포·Worker 재기동·원격 객체 변경은 하지 않았다.

## 1. 인정한 부분과 검증 범위

- LocalServingTransport와 LocalArtifactReader, 기본 local 선택, dedicated serving volume RW/RO, 백업 별도 설정·ledger·CLI 경계는 목표 방향에 맞는다.
- 새 경로 관련 기존 테스트를 독립 실행:
  `uv run --no-sync --all-packages pytest apps/cardrag-worker/tests/test_local_serving_and_backup.py apps/cardrag-mcp/tests/test_transport.py -q`
  **10 passed in 0.34s**.
- 그러나 신규 Worker 테스트의 publish 대상 DB는 실제 SQLite가 아닌 문자열 fixture다. reader가 읽고 복사하는 것까지 확인하며, 실제 Worker→MCP activation·summary/bundle/PDF 신규 갱신을 증명하지 않는다.
- 보고서의 전체 suite/lint 결과는 Executor 제출 근거로 인정한다. 이번 Reviewer는 전체 suite를 반복하지 않고 실제 미검증 실패 경계를 확인했다.
- 독립 재현: `evidence/reviewer_repro.py`, 결과 `evidence/reviewer-repro.json`. 임시 디렉터리·mock client만 사용했고 production volume/provider/network 호출은 없다.

## 2. P1 — WebDAV 없는 기본 배포가 실제 MCP 시작 단계에서 실패

### 근거

`deploy/mcp/compose.yaml:61`의 `${CARDRAG_WEBDAV_BASE_URL:-}`은 URL 미설정 시 빈 문자열을 컨테이너 환경에 전달한다. `Settings.webdav_base_url`은 `AnyHttpUrl | None`이며 **빈 문자열은 None이 아니다**.

재현:
`Settings(environment="test", mcp_bearer_token=<test token>, webdav_base_url="")` → ValidationError.
기본 Compose `config --format json`의 MCP 환경에서도 `CARDRAG_WEBDAV_BASE_URL=''` 확인.

Compose config 성공은 Python 애플리케이션이 해당 환경으로 시작한다는 증거가 아니다.

또한 Worker/MCP `compose.secrets.yaml`은 아직 WebDAV username/password secret file을 필수 보간한다. MCP bearer/OpenRouter secret file만 `/dev/null`로 지정해 **설정 검사만** 수행해도 양쪽 overlay가 `CARDRAG_WEBDAV_PASSWORD_SECRET_FILE` 및 `USERNAME_SECRET_FILE` 누락으로 exit1이다. 컨테이너에 /dev/null 인증을 넣어 실행한 것은 아니다.

### 수정 및 확인

- optional URL의 빈 값을 None으로 정상화하거나 URL 환경변수를 제외한다. local 모드에서는 사용하지 않는 WebDAV secret file/잘못된 원격 설정을 읽다가 전체 시작이 실패하지 않게 한다.
- 일반 인증 overlay와 optional WebDAV overlay를 분리한다. 기존 운영 overlay 호환성을 문서화한다. 다른 필수 인증까지 제거하지 않는다.
- Compose가 출력한 실제 환경을 Settings/create_app으로 검증하고, WebDAV credentials 없는 base+일반 secrets 조합에서 후보 MCP ready까지 확인한다.
- fresh Docker serving volume을 UID10001 Worker가 쓸 수 있도록 초기 권한/설치 절차도 준비한다. 기존 volume을 chmod했다고 신규 설치 검증을 대체하지 않는다.

## 3. P1 — 최악의 시스템 폐기 후 OCR 복원 계약 미충족

### 근거

`backup.py:555` 부근 `restore`는 **로컬 backup_receipts 테이블**을 읽어 원격 객체 목록을 얻는다. 새 시스템에서는 이 DB가 없으므로 원격 객체가 있어도 `status=empty`, restored_count0이다.

재현에서 기존 ledger의 backup succeeded 뒤 같은 mock WebDAV 저장소를 새 ledger로 restore: **empty / 0건**.

현재 record_run_success는 native OCR 본문/manifest/READY 및 일부 cache 파일만 등록한다. PDF를 pending에 넣는 코드가 없으며, 완결된 원격 복구 인덱스·backup manifest가 없다. adopted/content variant/기존 Paddle 결과의 초기 inventory도 제출되지 않았다. 보고서의 “OCR 및 참조 CAS PDF 증분 백업·복구 완료”는 현재 구현보다 넓은 주장이다.

### 수정 및 확인

- WebDAV만으로 발견 가능한 작은 immutable backup index/manifest와 완료 marker를 게시한다. 이미 업로드한 OCR CAS는 재사용하고 기존 serving stable pointer는 바꾸지 않는다.
- fresh ledger/fresh state에서 원격 index를 읽고 SHA·variant/PDF 대응을 검증하여 실제 OCRResolver가 사용하는 캐시/seed 형식으로 복원한다. 파일만 remote path 아래 복사하는 것으로 끝내지 않는다.
- 실제 참조 PDF, native/adopted/content-compatible OCR, 선택 variant/pin/READY 정보를 포함한다. 이전 Paddle 결과는 기존 파일/metadata로 검증하고 Paddle 재실행은 금지한다.
- 작은 실제 형식 fixture를 upload→로컬 전체 삭제→빈 ledger restore→OCR cache hit로 검증한다. OCR provider 호출0을 명시한다.
- 원격 root/계정/namespace와 SHA·size에 receipts를 scope한다. 현재 receipts는 remote_path만 키이므로 저장소를 바꾸면 새 목적지에도 이미 백업됐다고 잘못 생략할 수 있다.
- 이미 존재하는 수천 객체를 매번 검증/업로드하는 migration은 요구하지 않는다. 기존 증거/manifest로 초기 inventory를 만들고 미확인분만 보완한다.

## 4. P1 — 미백업 OCR 유실과 백업 실패 격리가 불완전

### 근거

- `backup.py:390–394`: pending의 local_path가 없어지면 queue에서 지운다. 결과는 오류 없이 succeeded가 될 수 있다. 재현: 미백업 source 삭제 후 **succeeded / remaining_pending0 / uploaded_count0**.
- `pipeline.py`의 run retention은 기존 run 상태/세대 참조만 보며 backup pending 참조를 보호하지 않는다. 기본 2개 run 보존과 7회 hybrid 조건을 조합하면 백업 전에 원본 run이 삭제될 수 있다.
- intent 등록은 CLI의 pipeline 성공 이후에만 이뤄진다. OCR 완료 후 export/후속 단계 실패 시 비용을 들인 새 OCR이 backup intent에 남지 않는다.
- `cli.py:809` 이후 BackupLedger 생성/등록/조회/flush에 전체 예외 격리가 없다. ledger IO/DB 오류는 정상 local publication 이후에도 `_run`을 예외 종료시킬 수 있다. disabled에서도 ledger 생성/조회가 실행된다.

### 수정 및 확인

- 완료 OCR을 durable하게 확보하는 시점에 intent를 만들고, pending 객체를 독립 immutable backup spool 또는 run/object pin으로 보존한다. 객체 수준 spool 재사용으로 과거 run 전체 복제를 피한다.
- 원본 누락은 lost_source/pending_error로 남기고 경고한다. 성공/백업 완료로 바꾸지 않는다. 로컬 검증된 CAS 대체 경로가 있으면 동일 SHA 객체로 재조정한다.
- backup 기능의 config/ledger/network 실패는 local serving 결과와 분리한다. disabled이면 백업용 ledger/원격 client가 없어도 정상 종료할 수 있어야 한다.
- fixture로 정상 게시→backup ledger 오류, OCR 완료→export 실패, 여러 run retention→7번째 flush, source 누락을 검증한다. 정상 local published/exit0와 실제 backup 상태를 구분한다.
- hard timeout으로 진행 중 I/O를 중단/대기 조정한다. 현재 flush의 예산은 각 객체 시작 전에만 체크하여 300초가 지나도 최대 600초 transfer가 이어질 수 있다. 재현에서는 0.01초 예산에 0.122초 작업이 완료됐다. 실제 client의 cancellation fencing과 receipt truth를 유지하면서 예산을 적용한다.

## 5. P1 — 새 serving 볼륨의 디스크 사용이 무제한 증가

### 근거

LocalServingTransport는 generation을 복사하지만 retention/GC가 없다. 기존 Worker 정리는 worker-state/runs, MCP 정리는 mcp-state를 대상으로 하며 새 serving volume을 정리하지 않는다.

재현에서 3세대를 게시한 뒤 gen-one/gen-two/gen-three가 모두 남았다. 실제 한 세대 DB·벡터가 약15GB이므로 변경 배치가 반복되면 수십~수백GB가 누적된다. 새 볼륨의 임시/최종 공간도 기존 capacity 계산에 통합되지 않았다.

### 수정 및 확인

- serving volume에 현재+이전1개 보존을 적용한다. MCP가 복사 중인 source generation 및 pending backup 객체의 pin을 보호한다. staging 잔여물은 writer lock/유예를 지켜 제한적으로 정리한다.
- 새 serving volume의 파일시스템 여유 공간과 staging+final 예상 bytes를 publication 전 검사한다. 실패 시 이전 정상 head를 유지한다.
- 3회 변경 게시 후 기본 세대 수2, 읽기/복사 중 세대는 완료까지 보존, pin 해제 후 정리됨을 작은 fixture로 증명한다.
- 원격 GC·기존 운영 volume 삭제는 본 수정의 인수 조건이 아니다.

## 6. P1/P2 — 기존 부분 실행과 백업 주기·전송 계약 연결 보완

### 6.1 부분 실행 local 지원 (P1)

`partial_cli.py`와 `resume-publication` 경로는 local transport 선택으로 전환되지 않았다. PDF/OCR/embedding skip 후 게시하려 하면 기본 local 설정이어도 WebDAVClient를 생성한다. source fence도 원격 current를 사용한다.

일반 run/partial/resume-publication이 같은 publication backend 선택을 재사용하도록 연결한다. `skip webdav`의 개발용 local_only 의미는 유지하되 실제 local publication/backup skip을 구분한다. provider-free 부분 재가공과 새 head activation을 확인한다.

### 6.2 주기 계산·재시도 (P2, 과제 기능 완수에 필요)

- `pending_count`는 OCR 수가 아니라 CAS/manifest/READY 등 파일 row 수다. native OCR 1건이 최소3건으로 집계되어 30 OCR 기준이 틀어진다.
- record_run_success는 같은 run_id를 두 번 호출해도 횟수를 더한다. 재현값2. 성공 run_id를 durable하게 dedupe한다.
- 일부 객체만 업로드하고 실패해도 runs_since_backup을0으로 재설정한다. 완결 backup batch commit 기준으로 counter를 정리한다.
- flush의 `force` 인자는 실제 사용되지 않는다. 재현에서 should_trigger=false인데 force=false로 upload 성공. manual 명령 semantics는 문서와 맞추고 자동 retry는 임계값/모드를 지킨다.
- independent flush timer/service와 single backup writer lock이 없다. main Worker가 실행되지 않으면 age 조건 평가/실패 재시도가 없다. 단순 별도 명령+timer로 제공하고 sudo가 필요하면 명령만 준비한다.

### 6.3 실제 WebDAV client에서 중복 검증 (P2)

BackupLedger.flush는 `WebDAVClient.put_bytes`를 쓴 뒤 자체 GET를 한다. put_bytes는 Core ImmutablePublisher를 호출하며 이미 temporary GET 및 final GET 검증을 수행한다. 따라서 mock의 1PUT+1GET 수치는 실제 HTTP 요청 수가 아니며 새 객체당 최소 3번 본문 검증을 하게 된다.

기존 verified upload 결과를 활용하거나 backup용 publisher에 필요한 최종 1회 GET만 수행하는 경로를 제공한다. mock top-level method 수가 아니라 실제 HTTP-level body GET/PUT bytes를 측정한다. 기존 객체는 반복 재전송/HEAD 전수 검사를 하지 않는다.

## 7. publication 구현의 보강 사항

아래는 실제 실패가 확인된 계약 누락이며 전면 재설계는 필요 없다.

- local publisher가 manifest 선언 DB SHA/size와 복사한 DB를 비교하지 않는다. 잘못된 DB를 준 재현에서 publish 성공 후 MCP는 “READY does not bind serving database”로 거절했다. pipeline seal 검증이 선행되지만 publication 경계에서도 복사본/manifest binding을 확인해 head를 바꾸기 전에 거절한다.
- 같은 generation final 디렉터리가 있으면 rmtree 후 다시 만들며 immutable semantics를 깨뜨린다. 동일 seal이면 idempotent reuse, 다른 바이트면 충돌 거절. 기존 active 디렉터리를 먼저 지우지 않는다.
- 일반 asyncio.to_thread publish는 cancellation 후 thread가 계속 pointer를 변경할 수 있다. 기존 fenced helper 또는 동등한 drain을 사용하고 exact publication reconciliation을 유지한다.
- callback을 별도 event loop에서 실행하는 대신 원래 loop에서 guard를 호출하도록 정리한다. 작은 guard/pointer 경합 및 취소 테스트로 확인하며 분산 lock 서비스를 추가하지 않는다.

## 8. 제출과 인수 절차

1. 위 실패 재현을 회귀 테스트로 고정한 뒤 수정한다. stale source/복구 불가/백업 유실/디스크 누적 위험을 중심으로 테스트한다.
2. 실제 SQLite/v5 fixture를 이용해 WebDAV 설정 없는 Worker publish→빈 MCP state activation→2번째 변경 반영→MCP restart를 검증한다. summary/bundle/PDF 및 cache-hit 검색 확인. live OCR/embedding provider 호출0.
3. 별도 WebDAV fake HTTP/staging 환경에서 신규 소량 delta→동일 데이터 repeat→fresh-host OCR restore를 검증한다. 원격 장애·ledger 실패와 local serving 독립성이 유지돼야 한다.
4. 관련 suite와 CI 필요 gate 통과 후 FIX_02_REPORT.md에 정확한 명령·상태·bytes·복원 근거를 기록한다. 보고서의 CLI 옵션 설명은 실제 `--help`와 맞춘다.
5. 전체 Worker 두 번 실행/2일 대기/새 유료 OCR 검증은 요구하지 않는다. 운영 반영은 후보 검증 후 별도 전환 절차로 진행할 수 있다.

현 운영 v1.0.34 배포가 이번 diff로 자동 교체됐다고 판단하지 않는다. 이번 보류는 개발 완벽주의가 아니라 **WebDAV 없는 기동, 전체 폐기 후 OCR 복구, 미백업 자료 보존, 디스크 고갈 예방**이라는 사용자의 직접 요구와 실측 실패에 따른다.
