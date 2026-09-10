# 백업과 복구

복구의 목적은 원본을 보존하면서 검증 가능한 새 상태를 만드는 것입니다. 아래 도구는 저장소의
오프라인 운영 도구이며 Worker/MCP 이미지에 포함되지 않습니다. 일반 시작·설정은
[운영 안내](OPERATIONS.md), 이미지 전환은 [릴리스 안내](RELEASING.md)를 참고하세요.

## 먼저 보존할 것

Worker state, MCP state, Codex 인증 home은 서로 다른 영속 볼륨입니다. 실행 중인 Worker의
볼륨을 복구용 컨테이너가 쓰도록 연결하지 않습니다. `docker compose down -v`, 광범위한
volume 삭제와 `docker system prune --volumes`를 복구 절차로 사용하지 않습니다.

조사 기록에는 source commit, 실행 이미지 digest와 container ID, Compose project, 볼륨 mount,
run ID, 상태·시작 시각·restart count, channel과 generation ID를 남깁니다. secret 본문,
Codex 인증 내용이나 인증 파일 해시를 출력·공개 증적에 넣지 않습니다.

실행 중인 SQLite 파일의 일반 파일 복사는 일관된 백업이 아닙니다. Writer가 자연 종료하거나
정상적으로 정지한 것을 확인한 뒤 전체 상태와 WAL/SHM 유무를 기록하고 원본을 읽기 전용으로
보존합니다. 파일 손상을 해결하려고 원본 DB/WAL/SHM을 삭제하거나 수정하지 않습니다.
저장 장치 snapshot도 writer와의 일관성 보장을 별도로 확인해야 합니다.

## 격리된 복사본 검증

1. 원본과 겹치지 않는 새 복구 경로 또는 새 named volume을 만들고 원본 inventory를 기록합니다.
2. Writer가 없는 동안 전체 보존본을 복사합니다. 복사 중 새 Worker 예약이 같은 source를 열 수 없게 합니다.
3. 원본과 목적지의 파일 목록·크기·SHA-256를 비교하고 목적지 DB의 SQLite integrity를 확인합니다.
4. 해당 DB/schema와 호환되는 정확한 이미지에서 run, checkpoint, 봉인 artifact와 generation 참조를 확인합니다.
5. 복구 결과가 검증될 때까지 원본과 직전 정상 세대를 보존합니다.

일반 검증 도구는 이미 생성된 오프라인 복사본 두 개를 비교합니다. 아래 경로는 운영 경로가 아닌
예시이며 source와 destination은 모두 검증이 끝날 때까지 writer가 없어야 합니다.

```bash
uv run python tools/cardrag_offline_volume_verify.py state \
  --source /recovery/source/worker-state \
  --destination /recovery/copy/worker-state
```

이 도구는 경로 겹침·symlink·special file·변경 중인 파일 등을 거부하고 SQLite를 읽기 전용으로
검사합니다. 실행 중 파일 변화 감지는 live snapshot의 일관성 보장을 대신하지 않습니다.
WAL/SHM 등 sidecar가 남아 일반 검증을 통과하지 못하면 원본을 그대로 보존하고 별도 복구본에서
SQLite의 복구 절차를 검토합니다. 과거 사고의 특정 파일을 생략한 복사 방식을 다른 DB에 적용하지 않습니다.

벡터 mmap 손상이나 SIGBUS 후에는 프로세스 상태와 원본 DB/vector를 먼저 보존합니다.
부분 파일 덮어쓰기나 무제한 자동 재시도를 피하고, 새 목적지에서 봉인 artifact와 원본 hash를
검증한 뒤 재개합니다. 상태 파일이 있다는 사실만으로 완료된 checkpoint라고 인정하지 않습니다.

Codex 인증은 state와 별도로 복사·검증합니다. 복구 대상 디렉터리는 private 권한을 유지하고
런타임 사용자 UID/GID를 맞춥니다. 기본 검증은 `10001:10001`, auth 파일 상한 2 MiB입니다.
이 검증 도구의 목적지는 0700 root 아래의 0600 `auth.json`과 비어 있는 0700 `home/`만
포함해야 합니다. 기존 인증 home 전체를 임의로 덮어쓰는 명령은 아닙니다.

```bash
uv run python tools/cardrag_offline_volume_verify.py codex-home \
  --source /recovery/source/codex-home \
  --destination /recovery/copy/codex-home \
  --expected-uid 10001 --expected-gid 10001
```

검증 결과에 인증 bytes나 인증 hash를 남기지 않습니다. 실패를 해결하기 위해 인증 home을
공개 권한으로 바꾸거나 정상 환경의 인증 파일을 출력하지 않습니다.

## Worker 재개와 MCP 복귀

`cardrag-worker resume RUN_ID`는 해당 run의 checkpoint를 검증하며 필요한 수집·OCR·provider
작업을 다시 수행할 수 있습니다. `cardrag-worker resume-publication RUN_ID`는 검증된 봉인
산출물을 게시하는 전용 경로입니다. 두 명령을 혼동하지 말고 정확한 run ID를 명시합니다.
부분 산출물을 완료된 seal로 추정하거나 다른 run의 산출물을 섞지 않습니다.
기존 run의 카드사 범위와 설정은 보존합니다. 수집 카드사 범위를 바꾸려면 새 run을 만듭니다.

이미 작동 중인 Worker 때문에 새 예약이 lock을 얻지 못한 경우 기존 run 상태를 확인합니다.
중복 실행을 위해 lock을 지우거나 정상 Worker를 재시작하지 않습니다. 재개 대상과 현재 writer가
같은 상태를 동시에 열지 않도록 확인합니다.

MCP의 `/health/live`는 프로세스 생존만 나타냅니다. 대용량 generation 최초 동기화에는 시간이
걸릴 수 있으므로 `/health/ready` 성공과 예상 generation ID를 함께 확인합니다. 이미지 digest,
활성 container ID와 실제 API의 generation도 일치해야 합니다. 실패한 후보를 중지할 때는
확인한 container/service만 대상으로 삼고 정상 Worker가 포함된 전체 stack을 내리지 않습니다.

롤백은 직전의 검증된 MCP 이미지와 호환 generation/config를 복원하는 작업입니다.
계속 진행 중인 Worker state 위에 과거 DB를 덮어쓰지 않습니다. 정상 generation을 참조하는
pointer를 복원하더라도 그 generation의 CAS·DB·vector·manifest·READY가 모두 남아 있어야 합니다.

## MCP 용량 정책과 남은 예약

MCP state의 quota 정책은 디스크에 봉인되며 재시작과 여러 프로세스 사이에서 공유됩니다.
환경변수만 바꿔 이미 생성한 state를 열면 `state quota policy differs from the durable policy`
오류가 날 수 있습니다. 정책 파일을 직접 편집·삭제해서 우회하지 않습니다. 용량 정책을 바꿔야
한다면 기존 state를 보존하고 원하는 정책의 새 MCP state에서 검증된 generation을 다시 받아
readiness와 generation을 확인한 뒤 전환합니다. Worker state는 이 작업의 대상이 아닙니다.

중단된 비동기 쓰기의 reservation은 creator lease와 결속됩니다. 남은 예약의 숫자나 생성 시각만
보고 파일을 삭제하지 않습니다. 운영자가 원인을 확인한 뒤 `cardrag_mcp.quota`의
`reconcile_abandoned_state_reservations`를 사용하면 coordination lock 아래에서 creator lease가
해제된 예약만 검증해 제거합니다. 활성 lease는 유지하고 잘못된 파일·identity는 실패합니다.
이 함수는 용량 정책 변경이나 데이터 정리 기능이 아니며 외부 provider의 미확정 호출을 재개하지 않습니다.
유지보수 시에는 해당 MCP와 updater를 정지한 뒤 제거된 token 목록을 기록합니다.
정책·lock·예약은 `audit-reports/.state-quota/` 아래에 보존되며, 이미 쓰인 부분 파일은
예약을 정리한 뒤에도 전체 state 사용량에 포함됩니다.

## WebDAV 검증과 미게시 파일

주기 검증의 기본 만기는 전체 검사 후 성공 실행 14회, 7일, 고유 신규 CAS 10 GiB 중 먼저
도달한 조건입니다. `no_change`는 성공 회수에 포함하고 실패는 포함하지 않습니다.
HEAD 성공만으로 전체 검증 시각을 갱신하지 않습니다. 증거는 endpoint·사용자·namespace·정책,
객체 경로·SHA-256·크기에 결속됩니다. 작은 pointer/READY/manifest는 항상 확인합니다.

크기와 ETag가 같으면 주기 정책에서 본문 GET을 생략할 수 있으므로 ETag에 드러나지 않는
같은 크기 손상은 전체 검사까지 탐지가 지연될 수 있습니다. NAS 복원이나 수동 원격 변경 뒤에는
`CARDRAG_WEBDAV_FORCE_FULL_VERIFY=true`로 전체 검사를 수행하고 완료 후 되돌립니다.
항상 전체 검증이 필요하면 `CARDRAG_WEBDAV_VERIFICATION_MODE=strict`, DB/vector 이중 read-back은
`CARDRAG_WEBDAV_GENERATION_READBACK_MODE=double`을 사용합니다. 검사 이력 테이블을 지울 필요는 없습니다.

기본 `final` read-back은 신규 `index.sqlite3`·`vectors.f32`에만 적용합니다. 신규 CAS와 제어
파일은 임시·최종 검증을 유지합니다. 최종 GET의 hash·크기 불일치가 나면 이번 시도의
MOVE 201로 생성했음이 확인되고, manifest/READY가 없으며 어떤 channel도 참조하지 않는 파일만
복구 journal에 따라 격리할 수 있습니다. 기존/충돌 목적지나 활성 세대를 덮어쓰지 않습니다.

격리는 `v1/.incoming/publish/<uuid>.tmp`로 `MOVE(Overwrite:F)`하며 즉시 삭제하지 않습니다.
같은 sealed 파일의 총 시도 상한은 2회이고 재시작·정책 변경으로 초기화하지 않습니다.
격리 도중 중단되면 원본/격리 경로 존재와 journal을 대조합니다. 양쪽 모두 존재하거나 모두 없는
모호한 상태에서는 멈춥니다. 로컬 파일 변화나 통신 오류를 원격 손상으로 단정하지 않습니다.
새 generation 검증이 끝나기 전에는 channel pointer를 바꾸지 않습니다.

## 아카이브 inventory

```bash
uv run python tools/cardrag_archive_inventory.py \
  --allow-root /recovery/source \
  --root /recovery/source/worker-state \
  --output /recovery/inventory.json
```

도구는 허용한 절대 경로 안에서만 탐색하고 symlink를 따라가지 않습니다. entry 상한과
descriptor identity 검사로 경로 교체·파일 변경을 탐지합니다. 삭제·Docker·WebDAV 작업은
수행하지 않으며 `--output`은 기존 파일을 덮어쓰지 않는 새 0600 파일만 생성합니다.
inventory는 복사·검증·보존의 근거이지 삭제 승인이 아닙니다.

## 호환 cache seed

선택적인 `deploy/worker/compose.cache-seed.yaml`에는 원본 볼륨 기본값이 없습니다.
`CARDRAG_SEED_WORKER_STATE_VOLUME`에 종료된 호환 Worker의 원본 볼륨명을 명시해야 하며
컨테이너에서는 `/mnt/cardrag-seed-state`에 읽기 전용·nocopy로 연결됩니다. 호환성 검증을
통과하는 cache만 seed CLI의 명시적인 입력 경로로 전달합니다. 기존 운영 볼륨을 이름만 보고
자동으로 연결하거나 진행 중인 writer의 상태를 seed하지 않습니다.

## Legacy OCR data-kit 가져오기

기존 data-kit이 있는 경우에만 오프라인 exporter를 사용합니다. 일반 Worker나 timer에
adoption overlay를 상시 추가하지 않습니다.

```bash
uv run python tools/legacy_data_kit_adoption_v2.py \
  --source /recovery/source/data-kit \
  --output /recovery/new-adoption-export
```

source는 읽기 전용이고 output은 새 디렉터리여야 합니다. 결과는 `inventory.jsonl`,
`normalization-receipts.jsonl`, `rejected.jsonl`, `export-manifest.json`과 normalized OCR CAS이며,
파일은 0600으로 생성합니다. 정책 `cardrag.legacy-ocr-adoption.v2`는 원문 그대로인 `exact`와
Woori의 `## Page 1` 바로 앞에 있는 정확한 24-byte UTF-8 prefix
`# OCR 처리 완료본\n\n`만 제거하는 `strip-exact-generated-prefix-v1`을 허용합니다.
다른 변환은 거부하며 원본/정규화 OCR SHA를 모두 보존합니다.
잘못된 page marker, 20문자 미만 페이지, symlink와 special file도 자동 보정하지 않습니다.

Worker에는 bare JSONL 대신 export 디렉터리를 전달합니다. export와 manifest의 source root는
컨테이너 안에서도 기록된 동일 절대 경로에 읽기 전용으로 mount해야 합니다. Worker는 원본
control 파일·SQLite inventory·master/OCR metadata·PDF·OCR와 전체 export hash를 다시 결속합니다.
일부 후보 오류, source 변경, 누락 문서나 대체된 PDF가 있으면 게시를 거부합니다.

`adoption-guard`는 `v1/channels/stable.json`의 부재를 읽기 전용으로 확인하며 pointer 본문을
출력하거나 쓰지 않습니다. 부재를 입증하지 못하면 실패합니다. `adopt-legacy --publish`는
첫 쓰기 직전에도 이 guard를 반복합니다. OCR cache 쓰기와 stable generation 게시는 각각
독립적인 권한·설정입니다. 기본 read-only cache 설정은 adoption 게시를 허용하지 않습니다.
`adoption-audit`는 봉인 export에 대응하는 remote READY/manifest/OCR CAS를 전체 hash·size로
확인하는 읽기 전용 검사입니다. 예상 수량은 해당 export에서 가져오며 특정 환경의 수를 고정하지 않습니다.
