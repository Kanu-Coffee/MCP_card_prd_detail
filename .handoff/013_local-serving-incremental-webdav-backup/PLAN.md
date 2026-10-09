# 013 PLAN — 로컬 운영 데이터 반영과 WebDAV 증분 백업 분리

작성: 2026-10-09, Planner / Codex. 상태: 계획 발행, 구현·운영 설정 변경 전.

## 1. 목표와 결정

동일 서버의 Worker→MCP 최신 데이터 반영을 WebDAV 왕복에서 분리한다. WebDAV는 장시간 작업한 OCR의 복구용 백업으로 사용하고, 변경된 객체만 업로드한다. 백업 지연·원격 장애가 정상 로컬 상품 정보 반영을 막지 않도록 한다.

권장 운영값은 **신규·변경 OCR 즉시 증분 백업, DB·벡터 원격 스냅샷 끄기**다. 사용자가 제안한 7회 실행·30건 변경·1GiB 증가 조건을 지원하는 배치 모드도 구현한다. 작은 OCR 몇 건을 백업하는 비용보다 15GB 운영 데이터 왕복 비용 제거가 우선이다. 이전에 합의한 최악의 시스템 폐기 시 OCR 유지라는 복구 목적을 지킨다.

이번 작업은 카드사별 수집·OCR 모델·상품 요약 분류를 바꾸지 않는다. 구현은 아래 내용을 기준으로 진행할 수 있으며 과거 PLAN들을 순서대로 읽을 필요가 없다. 현재 코드가 구현의 기준이다.

## 2. 확인한 현재 상태와 문제 원인

### 2.1 Git·운영 기준

- 조사 기준: main `44475271ff67d004b54ff580081faaca592dbe25`, v1.0.34 배포 완료.
- `/opt/cardrag/current`는 `/opt/cardrag/v1.0.34`를 가리킨다. MCP는 v1.0.34, Worker는 운영 검증된 `cardrag-worker:009-b544a80`다. 이번 구현의 Worker/MCP 새 이미지는 함께 준비해야 한다.
- 운영 이름: `cardrag-mcp`, `cardrag-mcp-state`, `cardrag-worker-state`, `cardrag-worker-auth`, `cardrag-worker-paddleocr-models`. 버전 숫자를 새 컨테이너/볼륨 이름에 넣지 않는다.
- Worker는 매일 03:00 KST 실행, UID/GID 10001. OpenCode / `alibaba-token-plan/qwen3.8-flash` / medium, fallback 비활성 상태다. 새 OCR·임베딩 호출을 검증용으로 반복하지 않는다.
- 정상 운영 스냅샷과 이전 롤백용 스냅샷 최대 1개를 유지한다. 기존 운영 볼륨을 정리 대상으로 간주하지 않는다.

### 2.2 10월 9일 예약 실행의 실측

run `025ce35739944d8facfdda4c99961f0c`, generation `g-025ce35739944d8facfdda4c-19895e1a91da`.

| 항목 | 실측 |
|---|---:|
| Worker 실행 | 03:00:00–06:00:08 KST, exit 0 |
| 문서 / 재사용 OCR / 새 OCR | 5,519 / 5,514 / 5문서·39페이지 |
| 새 CAS 객체 용량 | 13,058,986 bytes, 약 12.45MiB |
| WebDAV PUT | 15,016,128,128 bytes, 약 15.02GB |
| 검증 GET | 30,020,103,424 bytes, 약 30.02GB |
| 기존 generation 검증 GET | 14,996,676,608 bytes |
| 최종 검증 GET | 15,011,626,021 bytes |
| HEAD / 기존 검증 영수증 재사용 | 9,896 / 9,894회 |
| PUT / 검증 GET 누적 시간 | 약 18.4분 / 49.2분 |
| MCP 활성화 | 06:24:55 KST |

시간은 누적·병렬 지표가 포함되어 있어 단순 합산으로 실행 시간 단축을 보장하지 않는다. 원격 게시 구간은 대략 04:56–06:00이었다. 로컬 export·구조화·view 처리 시간도 남는다.

**전량 OCR 재업로드가 원인은 아니다.** PDF/OCR CAS와 OCR 캐시는 이미 상당량 재사용한다. 큰 DB·벡터 파일을 새 generation에 통째로 업로드하고 기존/신규 generation을 읽어 검증하는 비용이 주원인이다. 검증 영수증 미사용 원인은 객체별 로그로 추가 확인하되, 매번 무조건 전체 audit을 수행한다고 단정하지 않는다.

검증 설정은 이미 periodic / 14회 / 7일 / 신규 CAS 10GiB / generation readback final이다. 이 설정은 백업 실행 주기와 다른 개념이며, 숫자만 7회로 바꾸어도 대형 generation 게시 비용은 해결되지 않는다.

### 2.3 현재 WebDAV는 백업 외에 배포 경로 역할도 한다

요청 처리 시 MCP는 로컬 DB/PDF를 사용하지만, `main.py`가 WebDAV reader를 updater에 연결하여 새 DB·벡터·PDF를 가져온다. Worker의 이전 generation 판단·no_change·부분 실행·retirement 판단에도 원격 current가 사용된다.

따라서 현재 상태에서 WebDAV 게시를 7회에 한 번으로 줄이면 MCP 최신 정보도 지연된다. 먼저 같은 서버의 로컬 generation 전달 경로를 만들고, baseline을 그 경로로 일관되게 전환해야 한다. 기존 `--skip-stage webdav`는 local_only/published=false를 반환하며 이 기능만으로 운영 반영이 완료되지 않는다.

## 3. 방안 비교 및 채택 범위

| 방안 | 장점 | 제한 / 판단 |
|---|---|---|
| 기존 WebDAV 전체 게시를 7회·30건·1GiB마다 실행 | 변경 작음 | MCP 갱신까지 지연. 현재 구조의 기본 운영값으로 채택하지 않음 |
| 기존 검증 영수증·HEAD 정책만 개선 | 중복 GET 일부 감소 | 새 DB·벡터 PUT와 검증 비용은 남음. legacy 경로 개선 여지로 기록 |
| 로컬 generation 전달 + OCR 증분 백업 | MCP 최신성 유지, 큰 원격 왕복 제거 | Worker/MCP와 Compose를 함께 변경. 이번 과제 채택 |
| DB·벡터 블록 단위 원격 증분 동기화 | 대형 스냅샷에도 효과 가능 | 별도 전송·복구 형식 필요. 이번 인수 조건에 포함하지 않음 |

1차 구현은 기존 ArtifactReader/updater 계약을 활용해 **안전한 로컬 파일 복사**로 전달한다. 15GB의 로컬 복사까지 무조건 제거하려는 직접 DB 공유·제로 복사 개편은 후속이다. live SQLite나 Worker 작업 폴더를 MCP가 직접 열게 만들지 않는다.

## 4. 목표 구조와 데이터 계약

### 4.1 로컬 게시

1. Worker는 기존 export/seal 검증을 통과한 완결 산출물만 dedicated serving 볼륨에 게시한다. 예시 볼륨 이름 `cardrag-serving`, 경로 `/var/lib/cardrag-serving`.
2. generation별 immutable DB·벡터·manifest/READY와 필요한 PDF CAS를 제공한다. 임시 경로에 기록·검증·fsync 후 atomic rename/pointer 교체로 게시한다. source generation predecessor가 바뀌면 stale overwrite를 거절한다.
3. MCP는 serving 볼륨을 read-only로 마운트하고 LocalArtifactReader로 읽는다. 기존 updater의 용량·해시·schema·binding 검증과 원자적 activation을 재사용한다. MCP 자체 state/cache는 유지한다.
4. DB·벡터뿐 아니라 약관 PDF endpoint와 cache repair에 필요한 모든 PDF 객체가 로컬 reader에서 해결되어야 한다. PDF가 빠진 local generation을 ready로 표시하지 않는다.
5. 같은 generation polling에서는 pointer/manifest 변경 확인 정도만 수행한다. 매 poll마다 DB·벡터 전체 재해시, 전량 PDF 재복사·재검사를 추가하지 않는다. 최초 adoption 검증·누락 PDF 복구는 유지한다.
6. local 모드에서는 이전 generation, no_change, partial 실행 source, retirement, replay/resume의 기준을 로컬 committed head로 통일한다. 남아 있는 WebDAV current 참조를 전수 점검하여 혼합 baseline을 만들지 않는다.
7. 로컬 게시와 backup intent는 crash 후 재조정 가능하게 기록한다. OCR 완료 후 generation 게시 전에 중단되어도 비용을 들인 OCR 파일은 백업 후보로 남아야 한다.

Worker만 read-write, MCP는 read-only다. 전체 worker-state·OCR 인증 볼륨을 MCP에 노출하지 않는다. 경로 탈출·symlink·변경 중 파일·부분 manifest를 차단한다. 백업 프로세스가 사용하는 immutable 객체는 pin으로 보호한다.

### 4.2 백업에 반드시 포함할 것

- 완료된 OCR 결과 본문, 페이지/청크 대응, native/compatible/adopted OCR manifest 및 필요한 READY/pin/variant 정보.
- 원본 PDF SHA와 OCR variant/contract 식별자, 공급자·모델·작업 버전 등 캐시 적합성을 판별하는 메타데이터.
- PDF 자체도 증분 백업하여 원천 URL 폐기·개정 뒤에도 복구할 수 있도록 한다. 기존 검증된 PDF CAS는 다시 올리지 않는다.
- 현재·보존 대상 과거 약관의 OCR을 복구 인덱스에서 식별할 수 있어야 한다. 예전 Paddle 처리 결과도 보존하며 새 Paddle 실행은 요구하지 않는다.

환경변수·인증·운영 DB·벡터·임베딩의 완전 복구는 필수 목표가 아니다. GitHub 코드와 PDF/OCR 백업으로 OCR 재호출 없이 후속 구조화·임베딩을 재생성할 수 있으면 된다. 파일 몇 개만 백업하고 해당 OCR을 발견·선택할 manifest를 빠뜨리지 않는다.

### 4.3 권장 및 선택 가능한 백업 정책

새 설정은 기존 FULL_VERIFY 설정과 구별한다. 아래 이름은 구현 계약으로 제안하며 기존 naming convention에 맞춘 변경은 REPORT에 기록한다.

| 설정 | 기본값 / 의미 |
|---|---|
| `CARDRAG_PUBLICATION_TRANSPORT` | 호환 기본 webdav, 본 서버 전환값 local. transport를 명시하고 자동 fallback으로 baseline을 섞지 않음 |
| `CARDRAG_BACKUP_MODE` | local 운영의 기본 immediate. 선택 immediate / hybrid / manual |
| `CARDRAG_BACKUP_EVERY_RUNS` | 7 |
| `CARDRAG_BACKUP_NEW_OCR_COUNT` | 30 |
| `CARDRAG_BACKUP_NEW_BYTES` | 1073741824, 1GiB |
| `CARDRAG_BACKUP_MAX_PENDING_AGE_HOURS` | 168, 7일 |
| `CARDRAG_BACKUP_INLINE_BUDGET_SECONDS` | 300, 원격 장애로 본 배치를 무한 대기시키지 않음 |
| `CARDRAG_BACKUP_DERIVED_SNAPSHOT_ENABLED` | false. DB·벡터는 기본 백업 대상에서 제외 |

- **immediate:** 새 OCR이 durable하게 완료되면 증분 backup intent를 기록하고 소량 백업을 시도한다. 총 inline 예산을 넘거나 실패하면 pending으로 넘긴다. 이번처럼 새 5건이면 그 객체와 필요한 제어 파일만 전송한다.
- **hybrid:** 마지막 성공 백업 이후 성공한 Worker 실행 7회, 고유 미백업 OCR 30건, 고유 미백업 객체 1GiB, 가장 오래된 pending 7일 중 **하나라도 충족하면** 백업한다. 7번째 완료 시 실행하며 8번째로 밀리지 않는다. 조기 백업 필요 시 force flush를 지원한다.
- **manual:** intent/보존은 유지하되 명령으로만 전송한다. 운영자가 선택하는 옵션이며 현재 권장값은 아니다.
- 30건은 신규 상품 수가 아니라 `(PDF SHA, OCR variant/contract)` 기준 신규 또는 교체 결과 수다. 동일 variant를 여러 상품이 참조해도 한 번만 센다. 동일 PDF의 새 OCR variant는 별도 결과다.
- 1GiB는 대형 serving DB 전체 크기나 전체 폴더 크기 차이가 아니라 **미백업 고유 객체의 양수 누적 bytes**다. 삭제된 문서 수·bytes로 신규 변경량을 상쇄하지 않는다.
- 성공/no_change 실행은 run_id별 한 번만 횟수에 반영한다. degraded지만 local publication 성공한 run도 포함한다. 실패·replay/retry 중복은 제외한다. pending이 없으면 조건이 지나도 빈 업로드를 하지 않는다.
- 실행이 없더라도 age 조건을 평가할 독립 backup flush 스케줄을 둔다. OCR 즉시 모드 실패분 재시도에도 사용한다. 예시 15분 간격, 네트워크 장애 시 backoff. 이것은 임계값을 무시하는 full upload 작업이 아니다.
- batch 모드에서 29건을 며칠 보류하면 시스템 폐기 시 해당 OCR을 잃을 수 있다. 설정·로그·운영 문서에 최대 pending 나이를 보여준다. immediate도 원격 장애/미완료 동안은 백업 보장이 없다.
- derived snapshot을 옵션으로 구현할 경우 OCR flush와 별도 카운터·주기를 사용한다. 작은 OCR 백업마다 대형 스냅샷 타이머가 초기화되거나, OCR 1건마다 DB 15GB를 전송하게 만들지 않는다. 이 옵션은 기본 인수 필수 기능이 아니다.

### 4.4 증분 업로드·검증 최소화

- 신규 객체는 업로드 후 최종 주소에서 한 번 GET/SHA·size 검증한다. 임시+최종 본문을 모두 읽는 중복 검증은 피한다. HTTP 성공/ETag/크기만으로 최초 내용 검증을 대체하지 않는다.
- 이후 동일 immutable 객체는 성공 receipt를 재사용한다. 매 Worker 실행마다 기존 수천 개 객체에 HEAD/GET를 하지 않는다. remote inventory/control manifest는 작은 파일로 변경 시 확인한다.
- receipt는 원격 계정/root/namespace·path·SHA·size에 묶는다. remote destination 변경, 외부 변경 신호, 누락·복구 요청 시 재검증한다. ETag는 SHA-256 해시라는 가정을 하지 않는다.
- 새 backup batch manifest 및 완료 marker/pointer는 필요한 객체가 검증된 뒤 게시한다. 중간 실패는 incomplete/pending이며 복구 가능한 완결 백업으로 표시하지 않는다. ambiguous PUT/MOVE는 재시도 시 동일 객체·manifest로 reconcile한다.
- 기존 OCR CAS/native cache 형식과 호환 가능한 객체는 재사용한다. 신규 OCR 복구 인덱스/control namespace는 legacy serving `v1/channels/stable.json`과 분리한다. OCR 전용 인덱스를 serving generation pointer에 넣지 않는다.
- 정기 점검은 작은 인덱스 정합성 + 제한된 객체 표본 검사로 한다. 예시 주 1회, 파일 수와 전송 bytes 상한을 가진 표본; 매주 전체 약관/DB·벡터 재다운로드를 기본값으로 만들지 않는다. 전체 audit는 수동 명령·실제 복구·손상 의심 시 수행한다.
- 표본 검사는 모든 객체의 생존을 보장하지 않으므로 last uploaded / last sampled audit / last full audit를 구분해 기록한다. 데이터 미변경의 정상 실행에서 객체 PUT와 검증용 객체 body GET는 0이어야 한다.

### 4.5 장애·보존·결과 표시

- local publication 성공과 remote backup 성공은 별개다. 결과에 `publication_transport`, `local_generation_published`, `backup_status`, `pending_ocr_count/bytes`, `oldest_pending_at`, `last_backup_at`, `trigger_reasons`, 전송 bytes/요청 수를 기록한다.
- 기존 `published` 의미를 문서화하고 기존 소비자를 갱신한다. local mode의 published는 serving 반영 완료이며 remote backup 완료로 해석하지 않게 한다. 원격 실패는 별도 경고/backup_degraded 상태로 드러낸다.
- WebDAV unreachable/503/readback 오류 때도 로컬 generation과 MCP 갱신은 완료할 수 있다. OCR 캐시 miss 시 선택적 원격 조회 실패가 로컬 cache hit를 막지 않게 한다. 백업 pending을 OCR 재실행으로 해결하지 않는다.
- durable intent/receipt/counter는 재시작 후 유지한다. 독립 backup 프로세스는 별도 ledger + 단일 writer lock 등을 사용하여 실행 중 Worker SQLite를 동시에 변경하지 않게 한다. 실패 후 generation의 재출력 없이 `backup status`, `backup flush`, `backup audit`, `backup restore`에 해당하는 provider-free 작업을 제공한다.
- pending 객체를 로컬 GC에서 제외한다. backup backlog가 디스크를 압박하면 경고·재시도·수집 capacity gate로 대응하며 미백업 OCR을 몰래 삭제하지 않는다. pending pin은 완료 후 해제한다.
- local serving은 현재+이전 generation과 활성 request pin을 보존한다. 이는 운영 롤백 최대 1개 원칙에 맞춘다. pending 백업이 사용하는 공유 OCR/PDF는 별도 데이터 pin이며 과거 실행 디렉터리 전체를 무기한 보존하는 방식은 피한다.
- 원격 GC는 기본 비활성/별도 작업이다. 기존 backup/legacy namespace를 임의로 삭제하지 않는다. local source 전환 직후 이전 remote serving generation 정리까지 강제하지 않는다.

## 5. 구현 파일과 수행 순서

### Step 1 — source/backup 경계 도입

- Worker `apps/cardrag-worker/src/cardrag_worker/settings.py`, `cli.py`, `state.py`: transport/backup 설정·상태 계약, 로컬 모드에서 원격 접속이 serving 필수조건이 되지 않도록 설정 검증 분리.
- `pipeline.py`: `_publish_sealed`(현재 7576행 부근), `_publish_remote_only`, `validated_current_generation`, `_observed_pointer_bytes`, `verification_gate` 사용 지점 전수 점검. 작은 publication backend 계약으로 local/legacy source 판단을 분리한다. 메서드명만 바꾸고 실제 호출이 원격으로 남는 구현은 피한다.
- `ocr.py`: `OCRResolver`, `_publish_native_cache`, `_publish_native_ready`의 동기 원격 게시와 cache lookup을 점검한다. native OCR 로컬 완료와 원격 backup 실패를 분리하고, 기존 read-only/read-write/cache approval 정책의 의미를 명시한다.
- `partial_execution.py`, `partial_cli.py`: PDF/OCR/embedding skip·resume 지원을 유지한다. legacy `skip webdav`를 local publication 성공으로 위장하지 않는다. local 모드의 backup skip/force 옵션은 serving skip과 구별한다.
- Core `packages/cardrag-core/src/cardrag_core/{facade,manifests,paths,cas,ocr}.py`: 기존 manifest/domain 검증을 재사용한다. 새로운 backup 인덱스는 serving schema를 변조하지 않고 별도 schema로 추가한다.

### Step 2 — local publisher + MCP reader

- Worker에 local publisher 모듈을 추가하고 sealed export에서 serving volume으로 게시한다. immutable PDF CAS/DB/벡터와 atomic head를 제공한다.
- MCP `apps/cardrag-mcp/src/cardrag_mcp/transport.py`: LocalArtifactReader 추가. `CoreArtifactReader`의 manifest→RemoteGeneration 변환을 공통 함수로 분리할 수 있다.
- `config.py`, `main.py`: transport별 reader를 명시적으로 선택한다. local 모드에서 WebDAV 인증정보가 없어도 구동 가능해야 한다.
- `updater.py`의 `ArtifactReader` protocol, generation 검증/활성화, PDF repair를 재사용한다. `store.py`의 readonly handle/request pin/atomic activate를 유지한다.
- serving head 변경을 읽는 동안 manifest·파일 identity를 generation 단위로 pin한다. 새 head가 떠도 진행 중 요청은 기존 generation을 안전하게 사용한다.

### Step 3 — 증분 백업과 정책

- 새 backup 모듈/ledger/CLI를 구현한다. 강제 flush·status·표본/full audit·최소 OCR restore를 제공한다.
- `webdav.py`, `webdav_verification.py`의 CAS/client/retry/receipt 기능을 활용하되, 기존 full serving publication 검증 gate를 새 OCR 백업의 매 실행 gate로 호출하지 않는다.
- async/bounded backup는 새 provider 작업을 하지 않고 durable OCR/PDF 파일과 manifest만 처리한다. 독립 스케줄은 단일 실행 lock과 자원 상한을 갖는다.
- 최초 전환 시 현재 로컬 OCR와 기존 remote manifest/검증 receipt로 baseline을 만든다. 가능한 기존 verified 객체는 그대로 인정하고, 증거 없는 객체만 검증/보완한다. 초기 migration을 매일 반복하거나 5,519건 OCR 재실행으로 처리하지 않는다.

### Step 4 — 배포 및 문서

- `deploy/worker/compose.yaml`, `deploy/mcp/compose.yaml`, 각 secrets/override 계층, `compose.yaml`의 실제 포함 관계를 점검한다. `cardrag-serving` shared volume RW/RO, UID10001, 기존 state/external volume 유지, local 모드 URL 필수 보간 제거를 반영한다.
- `/opt/cardrag/current`의 실제 overlay와 `docker compose config`를 기준으로 배포한다. archive로 host용 `compose.secrets.yaml`을 덮어쓰지 않도록 주의한다. 인증 내용은 공개 로그/REPORT에 넣지 않는다.
- `docs/OPERATIONS.md`, `docs/RECOVERY.md`에 source와 backup 상태, 7/30/1GiB/age 의미, 실패 시 재시도, OCR-only 복구 절차를 작성한다. 오프라인 restore에 필요한 메타데이터와 GitHub 코드 외의 선택 설정을 구분한다.
- 기존 WebDAV serving 모드는 다른 서버의 MCP 및 롤백용으로 유지한다. 본 서버만 명시적 local 전환한다. 전환 전에 초기 local head와 PDF 존재를 준비해 blank startup을 피한다.
- sudo가 필요한 새 timer/service 설치는 정확한 경로·복붙 가능한 명령을 준비한다. 기존 03시 timer를 멋대로 바꾸거나 백업용 timer 때문에 Worker를 중복 실행하지 않는다.

## 6. 검증과 인수 기준

### 6.1 빠른 자동 검증

기존 테스트 관례를 사용한다. 범위는 새로운 정책·전달 경계·복구와 실제 회귀 위험이다.

1. 7회 경계(6회 미실행/7회 실행), 누적 30건(29/30), 1GiB(직전/동일), age, OR 조건, force/manual. no_change·degraded 성공 포함/실패 제외, 동일 run retry 중복 제외.
2. 상품 수와 OCR 수 구분, 중복 PDF·동일/새 variant, 여러 실행에 걸친 누적, 삭제와 신규량 상쇄 금지, 성공 backup 이후 정확한 counter reset. pending 없으면 0PUT/0bodyGET.
3. 기존 verified CAS는 body GET/PUT 없이 재사용하고 신규 객체는 final SHA 검증 1회. receiver 내용 손상·누락·receipt scope 변경을 안전하게 처리한다.
4. 503/connection reset/예산 초과/프로세스 중단/ambiguous MOVE 후 pending 복구. backup 실패에도 새 local generation 반영, pending pin 유지, 재시도 시 OCR·embedding provider 호출 0.
5. local partial publication/잘못된 SHA·manifest/predecessor/path traversal/symlink는 activation 거절·기존 head 유지. 정상 갱신 후 summary/bundle/search/PDF와 generation pin 일치.
6. 기존 partial-stage/replay/retirement/no_change·WebDAV legacy 경로 회귀 테스트 유지. 해당 테스트와 관련 core/MCP transport/updater 테스트를 실행한다. CI 전체가 필수 gate이면 실행하되 유료 provider 테스트를 만들지 않는다.
7. 작은 fixture를 빈 로컬 캐시로 복구하여 native와 호환/adopted OCR이 cache hit가 되는지 확인한다. DB/vector/이전 환경변수 없이도 OCR 재호출 0으로 후속 처리가 가능해야 한다.

### 6.2 운영 실증 — 전체 Worker 재실행은 인수 필수 아님

- 최근 정상 sealed 산출물을 쓰는 provider-free local publication/candidate MCP로 검증한다. 변경분을 가진 소형 fixture 또는 실제 새 seal을 써서 remote offline 상태에서 head 전환·MCP 응답을 확인한다. 오래된 동일 head를 읽는 것만으로 source 전환 성공을 주장하지 않는다.
- 우리500107, 최근 변경된 롯데1186/1640/1644/1634/1545, 다른 카드사 최소 1개에서 summary/bundle/PDF 응답과 generation/hash를 확인한다. 검색은 fixture 또는 기존 query cache 등 비용이 낮은 방식으로 별도 1회 확인한다.
- 제한된 신규 OCR/PDF backup batch 1회 + 즉시 재실행 no_change 1회를 실증한다. 두 번째는 객체 upload/body verification GET 0. remote unreachable 실증은 candidate config/mock endpoint로 격리한다.
- 신규 객체량이 작을 때 원격 DB/vector PUT 및 기존 DB/vector verification GET가 모두 0인지 counters로 증명한다. 실제 remote staging namespace를 사용했다면 생성한 test 객체 범위만 정리한다.
- 동작 검증이 충분하면 다음 정상 03시 run을 운영 관찰 대상으로 둔다. 같은 full Worker를 두 번 돌리거나 2일 기다리는 조건을 추가하지 않는다. 2시간 이상 작업이 불가피하면 기동/로그 위치/종료 기준만 알리고 사용자의 완료 통보 후 확인한다.

### 6.3 최종 판정

- WebDAV unavailable 상태에서도 로컬 데이터 갱신과 MCP 서비스가 정상이다.
- 일반 실행에 대형 DB·벡터 원격 전송/전체 readback이 없다. 미변경 실행은 기존 수천 객체의 HEAD/GET를 반복하지 않는다.
- OCR-only 복구가 가능하고 실패·배치 보류분은 미백업으로 명확히 표시한다.
- 제안된 7회/30건/1GiB/age/force 정책이 검증되며 사용자 선택에 따라 적용 가능하다.
- 운영 설정·관측·재시도·롤백 절차가 준비된다. 총 Worker 시간 단축률은 실제 실측으로 보고하며 임의 목표를 배포 차단 조건으로 만들지 않는다.

## 7. Executor 제출물

같은 디렉터리의 새 `REPORT.md`에 구현 commit/diff, 선택한 기본값, 테스트 명령과 결과, 객체/bytes/요청 수 before/after, 초기 migration과 복구 증거, 운영 전환 여부·정확한 명령·필요한 sudo를 작성한다. 완료/미완료 및 local published/remote backed up을 구분한다. 기존 handoff 문서는 수정하지 않는다.

현재 조사로 만드는 것은 이 PLAN뿐이며 코드·운영·타이머·WebDAV 객체 변경은 수행하지 않았다. 구현 시점에 Git/운영 상태를 재확인한다.

## 8. 근거

- 저장소 실측: `.handoff/012_v1034-release-cutover/evidence/scheduled-run-20261009.json`, `OPERATIONS_REVIEW_20261009.md`와 위에 기재한 Worker/MCP 구현. 실측 핵심은 본 PLAN에 포함했다.
- [WebDAV RFC 4918 §8.6–8.8](https://www.rfc-editor.org/rfc/rfc4918.html#section-8.6): ETag/namespace 변경 의미. ETag를 업로드 본문의 SHA 해시로 간주하지 않는다.
- [SQLite 공식 Backup API 문서](https://sqlite.org/backup.html): live DB 백업은 일관된 snapshot이 필요하다. 이번 전달에는 이미 sealed된 export를 사용하며 실행 중 DB 파일의 무조건 복사를 도입하지 않는다.
