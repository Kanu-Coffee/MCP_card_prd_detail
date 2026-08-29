# CardRAG v1.0.9 후보 검증·전환 절차

이 절차의 최우선 조건은 **현재 v1.0.8 Worker가 정상 종료하기 전에는 운영
컨테이너, `/opt/cardrag/current`와 그 실제 target, `stable.json`, 운영 WebDAV 및
`cardrag-worker_worker-state`를 변경하지 않는 것**입니다. 현재 확인한 active
target은 `/opt/cardrag/v1.0.8`입니다. 과거 가이드의 staging 경로
`/opt/cardrag-v1.0.8`과 혼동하지 않고, 전환 시점에 `readlink`로 다시 확인한 exact
target을 inventory에 기록합니다. v1.0.9 후보는 별도 WebDAV 경로, 별도 채널
포인터, 별도 Docker 볼륨과 별도 MCP 포트를 사용합니다.

## v1.0.9 변경 범위

- PDF를 `pdf-cache/objects/sha256/<prefix>/<sha256>` 한 벌로 보존하고 SQLite
  dictionary가 카드사·상품·문서종류·공시 URL·source version·게시물 ID와 PDF
  revision 이력을 결속합니다. 새 URL/새 source identity와 같은 URL의 변경된
  바이트 모두 추적하며 이전 revision의 metadata/hash를 지우지 않습니다.
- 실행 디렉터리의 다운로드 사본 대신 검증된 PDF cache 객체를 재사용합니다.
- 원본 확인 주기는 기본 168시간입니다. 주기 안의 cache hit는 네트워크 없이
  재사용하고, 만료 뒤 ETag/Last-Modified가 있으면 조건부 요청합니다. `304`는 원본
  확인 시각만 갱신하고, `200`의 같은 바이트는 기존 revision을 유지하며, 변경된
  바이트는 같은 source chain의 새 SHA revision이 됩니다. validator가 없는 서버도
  만료 시 전체 다운로드하므로 동일 URL의 변경을 무기한 놓치지 않습니다.
- 성공 또는 `no_change` 뒤 현재 실행 PDF와 최근 publication seal 2개가 참조하는 PDF
  SHA만 보호하고 나머지 CAS 바이트를 정리합니다. SQLite source/revision 이력은 남겨
  다음 lookup을 안전한 miss로 만들며, 실패 실행과 누락·손상·symlink seal/CAS에서는
  삭제를 시작하지 않습니다.
- OCR 실패는 문서 단위로 격리합니다. 카드사별 OCR 성공률이 각각 95% 이상일
  때만 성공 문서를 임베딩하고 v4 세대를 게시합니다. 실패 상품은 누락시키지 않고
  `ocr_failed` 상태와 제한된 사유로 조회됩니다. 임베딩·DB·게시·인증·설정처럼
  문서 하나에 한정할 수 없는 오류는 계속 전체 실행을 중단합니다.
- 게시 실행 디렉터리, WebDAV/MCP 세대 및 PDF publication seal 보호 기본값은 2입니다.
  미완료 진단 실행은 별도로 최대 2개를 보존하고 실행 중인 현재 run은 항상 제외합니다.
- 삼성카드를 기본 활성 카드사로 추가합니다. [공식 상품별 약관·설명서 공시](https://www.samsungcard.com/company/IR/announce/product-conditions/UHPPCI0261M0.jsp)의
  API 결과와 첨부파일을 결속하며, 안내장을 우선하고 모호한 복수 후보는 안전하게
  거부합니다.

### 2026-08-28 OCR 종료 반영 계약

[v1.0.8 OCR Worker 종료 조사](V1_0_8_OCR_INCIDENT_2026_08_28.md)에서 provider
인식과 local seal, 원격 OCR CAS·native manifest는 완료됐지만 `READY.json`이 없는
publication 경계를 확인했습니다. v1.0.9 후보는 이 경계에 다음 계약을 적용합니다.

- native cache의 CAS, manifest, READY 게시와 READY-only 복구는 최초 시도를 포함해
  최대 3회만 시도하며 retry delay는 0.25초, 1.0초입니다. timeout/network,
  remote `ProtocolError`/`ProxyError`와 HTTP `408`, `423`, `425`, `429`, `5xx`만
  일시 오류입니다.
- 일시 오류가 3회를 소진하면 검증된 OCR과 local native manifest는 그대로 두고,
  cache binding이 없는 generation-only OCR로 해당 문서를 계속 처리합니다. Worker
  최종 결과와 INFO 진행 로그의 `ocr_cache_publication_deferred`가 이 문서 수를
  표시합니다.
- 각 deferred 문서는
  `runs/<run-id>/documents/<document-id>/ocr/native-cache-publication-diagnostic.json`에
  최대 4,096 bytes의 canonical
  `cardrag.ocr-cache-publication-diagnostic.v1`을 남깁니다. phase(`cas`, `manifest`,
  `ready`), error kind, 제한된 reason code, HTTP status가 있으면 그 status, retry
  가능 여부, 실제 publication attempts와 `outcome=generation-only`만 기록하며
  credential, URL/path, response body와 원예외 문자열은 기록하지 않습니다.
- 다른 run에서 manifest와 OCR CAS만 남은 partial native cache를 만나면 source,
  processor contract, canonical manifest, OCR hash/size/page hash를 모두 검증한 뒤
  READY만 게시해 복구합니다. 같은 run의 재시도는 local seal을 재검증해 복구합니다.
  두 경로 모두 OCR provider를 다시 호출하지 않으며, 복구 성공 뒤 diagnostic을
  제거하고 native cache binding을 다시 부여합니다.
- HTTP `401`/`403`/`407`, `LocalProtocolError`/`UnsupportedProtocol`, HTTP
  `409`/`412` immutable conflict, contract 오류, manifest·READY·CAS canonical
  bytes 또는 hash 무결성 오류는 deferred 대상으로 낮추지 않고 즉시 fail-closed로
  처리합니다. 이때
  `runs/<run-id>/reports/ocr-systemic-failure.json`의 canonical
  `cardrag.ocr-systemic-failure-report.v1`에 run/document/source 식별자, phase,
  제한된 reason/error kind, status, retry 가능 여부, OCR stage attempt와 cache
  publication attempts를 안전하게 기록합니다. run과 stage의 terminal error는 이
  보고서 상대경로를 포함합니다.

v1.0.8의 일반화된 `non_document_scoped_error`와 달리 이 보고서만으로 어느
publication phase와 안전한 실패 분류인지 판별할 수 있어야 합니다. 다만 영구 오류의
원문이나 secret을 traceback에 복원하는 것은 합격 조건이 아닙니다.

### Worker 안전 종료 계약

v1.0.9의 `run`과 `resume`은 asyncio loop에서 `SIGTERM`과 `SIGINT`를 직접
처리합니다. 첫 신호는 현재 pipeline task의 취소 요청으로 바꾸고, 이미 시작한
WebDAV/PDF cache blocking mutation은 thread fence 안에서 끝까지 drain합니다. 그 뒤
게시가 실제 commit됐는지 재검증해 run을 `succeeded`, `no_change` 또는 `interrupted`로 종결한
다음에만 Worker lock을 풉니다. drain 중 들어오는 두 번째 이후 신호는 추가
`Task.cancel()`로 바꾸지 않습니다. 정상 취소 완료 시 stdout에는 credential이나 원
예외가 없는 `worker_signal_shutdown` JSON이 출력되며 `SIGTERM`은 143, `SIGINT`는
130으로 종료합니다. 이 JSON의 `status=shutdown_complete`는 process 종료 상태이며,
run의 terminal truth를 `interrupted`로 단정하지 않습니다. 게시 직후 취소를 정확히
재검증한 경우 run은 `succeeded`일 수 있고, 이미 동일 세대를 확정한 뒤의 취소라면
`no_change`일 수 있으므로 SQLite 상태가 최종 기준입니다.

systemd unit의 foreground Docker Compose 실행과 컨테이너 `init: true`,
`stop_signal: SIGTERM`은 신호를 Worker까지 전달합니다. unit은
`KillSignal=SIGTERM`, `TimeoutStopSec=infinity`, `SendSIGKILL=no`를 사용하고 계획된
130/143을 `SuccessExitStatus`로 인정합니다. WebDAV 600초는 **요청별 inactivity
timeout**이지 전체 thread 상한이 아닙니다. 현재 세대의 DB와 모든 PDF/OCR CAS를
검증하는 한 blocking callable에는 여러 요청이 있으므로 900초 같은 추정 aggregate
timeout을 두면 systemd가 thread drain 중인 Compose client만 죽여 컨테이너를
고아로 남길 수 있습니다. 따라서 정상 `systemctl stop`은 정합성 drain 완료를
기다리는 것이 명시적 기본 계약입니다.

운영자가 별도 장애 판단으로 강제 종료해야 할 때는 두 번째 SIGTERM을 반복하지 말고,
먼저 journal·`systemctl show`·상태 volume snapshot을 보존합니다. 다음 read-only
명령으로 project/service label과 전체 container ID를 확인하고 **해당 실행의 ID가
정확히 하나임을 대조한 뒤에만** Docker에서 그 ID 전체를 종료합니다.

```bash
sudo docker ps --no-trunc \
  --filter label=com.docker.compose.project=cardrag-worker \
  --filter label=com.docker.compose.service=worker
sudo docker kill --signal=KILL <확인한-정확한-container-id>
```

이 비정상 절차는 Python thread만 남기지 않고 컨테이너 전체를 끝내지만 terminal
bookkeeping을 생략할 수 있습니다. 재실행 전에 최신 run과 원격 pointer/READY를
검사하고, 다음 run이 남은 `running`을 `interrupted`로 종결하는 것을 확인합니다.
정상 중지 후에는 journal의 `worker_signal_shutdown`과 SQLite의 `interrupted`,
`no_change` 또는 이미 검증된 `succeeded` terminal status를 함께 확인합니다.

## 1. 현재 v1.0.8 종료 확인

아래 항목을 모두 만족하기 전에는 다음 단계로 넘어가지 않습니다.

1. v1.0.8 Worker 컨테이너가 종료되었고 재시작 중이 아닙니다.
2. `worker-state.sqlite3`의 최신 run이 `succeeded`, `no_change` 또는 명시적으로
   조사 완료한 `failed` 중 하나이며 `running`으로 남지 않았습니다.
3. `/opt/cardrag/current`의 link text와 `readlink -f` 실제 target을 각각 기록하고,
   그 target이 종료한 v1.0.8 프로세스의 설치 source와 일치함을 확인했습니다.
4. 운영 WebDAV의 `v1/channels/stable.json`과 현재 MCP 응답을 기록했습니다.
5. v1.0.8 상태 볼륨의 읽기 전용 snapshot/backup을 만들고 복구 시험 대상을
   기록했습니다.

v1.0.9은 새 실행을 시작할 때 이전 프로세스가 남긴 `running` run을
`interrupted`로 종결하므로 모니터링에서 영구 실행처럼 보이는 상태도 정리됩니다.

## 2. 후보 환경 준비

운영 설치 트리와 다른 checkout에서 v1.0.9 이미지를 빌드합니다. 후보용 환경
파일에는 운영과 다른 WebDAV base URL을 지정합니다.

```dotenv
CARDRAG_CANDIDATE_WEBDAV_BASE_URL=https://webdav.example/cardrag-v109-candidate
CARDRAG_CANDIDATE_MCP_PUBLIC_BASE_URL=https://candidate-cardrag.example
CARDRAG_CANDIDATE_MCP_BIND_ADDRESS=127.0.0.1
CARDRAG_CANDIDATE_MCP_PUBLISHED_PORT=18009
CARDRAG_ENABLED_ISSUERS=woori,kb,shinhan,samsung
CARDRAG_PDF_CACHE_REFRESH_HOURS=168
CARDRAG_RETAIN_GENERATIONS=2
CARDRAG_RETAIN_INCOMPLETE_RUNS=2
CARDRAG_MCP_RETAIN_GENERATIONS=2
```

후보 overlay는 다음 불변조건을 강제합니다.

- Worker/MCP 채널: `candidate-v1.0.9`
- Worker 상태: `cardrag-worker-v109-candidate-state`
- MCP 상태: `cardrag-mcp-v109-candidate-state`
- 후보 Worker 원격 GC: 비활성
- 후보 MCP 기본 포트: `127.0.0.1:18009`

base Compose의 Worker 기본 상태 volume도 v1.0.9 전용
`cardrag-worker-v109-state`입니다. `cardrag-worker_worker-state`는 오직
`CARDRAG_V108_WORKER_STATE_VOLUME`로 지정하는 read-only seed source이며 v1.0.9
destination으로 사용하지 않습니다.

Compose v2.24.4 이상에서 다음 구성을 검증합니다.

```bash
docker compose --env-file /etc/cardrag/candidate-worker.env \
  -f deploy/worker/compose.yaml \
  -f deploy/worker/compose.candidate.yaml \
  -f deploy/worker/compose.secrets.yaml \
  config --quiet

docker compose --env-file /etc/cardrag/candidate-mcp.env \
  -f deploy/mcp/compose.yaml \
  -f deploy/mcp/compose.candidate.yaml \
  -f deploy/mcp/compose.secrets.yaml \
  config --quiet
```

## 3. v1.0.8 PDF cache seed

정상 종료한 v1.0.8 상태 볼륨만 `/mnt/cardrag-v108-state`에 read-only로
연결합니다. `cache-seed`는 legacy SQLite snapshot과
`runs/<run-id>/{downloads,resume-downloads}/source_<sha256>.pdf`의 identity가
정확히 맞고 PDF 전체 구조가 검증되는 자료만 후보 CAS에 넣습니다. 기본 실행은
dry-run입니다.

```bash
docker compose --env-file /etc/cardrag/candidate-worker.env \
  -f deploy/worker/compose.yaml \
  -f deploy/worker/compose.candidate.yaml \
  -f deploy/worker/compose.secrets.yaml \
  -f deploy/worker/compose.cache-seed.yaml \
  run --rm worker cache-seed /mnt/cardrag-v108-state
```

dry-run 보고서의 `status=verified`, `ledger_path=null`인지 확인합니다.
`ledger_sha256`/`ledger_size_bytes`, `ledger_accepted_candidates`,
`ledger_unique_pdf_hashes`, `ledger_missing_sources`와 `legacy_database_sha256`를 운영
inventory에 기록합니다. `candidate_files`/`unique_pdf_objects`/용량 수치가 합리적인지,
bounded `missing_sample`의 `reason=no_exact_legacy_pdf`가 DRM·미완료 문서 등 예상한
사유와 일치하는지도 감사합니다. 더 늦은 terminal run으로 중단이 증명된 과거
`running` run은 seed에서 제외되므로 `skipped_stale_runs`도 운영 이력과 대조합니다.
최신 run이 여전히 `running`이면 명령은 차단됩니다.

감사 후 같은 명령 끝에 `--apply`를 붙입니다. 성공하면 후보 상태 볼륨 내부의
`audit-reports/cache-seed/<ledger_sha256>.json`에 canonical ledger가 atomic/fsync로
보존되고 보고서 `ledger_path`가 이 상대 경로를 가리킵니다. ledger에는 모든 수용
candidate의 legacy run/path, source identity, issuer/product, PDF hash/size/pages/관측
시각, 중복 제거한 전체 수용 hash, 모든 누락 run/source와 사유, stale run ID 및
legacy DB content hash가 들어갑니다. 첫 적용 보고서의 `status=applied`,
`applied_candidates + reused_candidates = candidate_files`를 확인합니다. 같은
`--apply` 명령을 한 번 더 실행해 `reused_candidates = candidate_files`,
`applied_candidates=0`, `created_pdf_objects=0`, `created_revisions=0`이고 두 보고서의
`ledger_path`/`ledger_size_bytes`/`ledger_sha256`와 실제 ledger bytes가 동일하면 모든
허용 항목의 CAS와 revision이 재검증된 것입니다. ledger 경로나 파일이 symlink,
special node, 비정상 크기 또는 동일 hash 이름의 다른 bytes이면 적용은 차단됩니다.
원본 볼륨은 이 단계에서 삭제하거나 수정하지 않습니다.

## 4. 후보 전체 실행과 합격 기준

```bash
docker compose --env-file /etc/cardrag/candidate-worker.env \
  -f deploy/worker/compose.yaml \
  -f deploy/worker/compose.candidate.yaml \
  -f deploy/worker/compose.secrets.yaml \
  run --rm worker run

docker compose --env-file /etc/cardrag/candidate-mcp.env \
  -f deploy/mcp/compose.yaml \
  -f deploy/mcp/compose.candidate.yaml \
  -f deploy/mcp/compose.secrets.yaml \
  up -d --wait
```

다음을 모두 확인합니다.

- 운영 `stable.json`의 bytes/hash와 v1.0.8 컨테이너·볼륨이 전후 동일합니다.
- 후보 포인터만 `candidate-v1.0.9.json`에 생성됩니다.
- 우리·KB·신한·삼성 discovery가 각 카드사의 최소 개수 및 이전 성공 baseline
  감소율 검사를 통과합니다.
- 카드사별 OCR 성공률이 각각 95% 이상이고 실패 상품은 MCP 상품 응답에
  `ocr_failed`로 보입니다.
- OCR 성공 문서만 evidence/embedding에 포함되고 검색 결과의 PDF hash와 원문
  링크가 일치합니다.
- 후보 MCP `/health/live`, `/health/ready`가 모두 HTTP 200이며 재시작 뒤에도
  마지막 검증 세대를 제공합니다.
- 최종 Worker/MCP image를 vulnerability scanner로 검사해 수정 가능한
  `HIGH`/`CRITICAL` 항목이 0인지 확인합니다. 배포판이 아직 수정 패키지를 제공하지
  않는 항목은 image digest, scanner DB 시각, 영향·완화·재검사 기한을 예외 기록에
  남기고 승인하며, release tag 직전 고정 base digest를 다시 빌드·검사합니다.
- cache hit/miss/revalidation/not-modified/download/revision 수와 단계별 진행 로그가
  장시간 정지 없이 증가합니다. 설정한 주기 안에는 fresh hit가 네트워크를 사용하지
  않고, 만료 후 `304`, `200` 동일 바이트, `200` 변경 바이트가 각각 기대한 카운트와
  revision 이력을 만듭니다.
- transient native READY 게시를 3회 실패시키는 fault injection에서 generation-only
  OCR와 bounded diagnostic이 생기고 run이 계속됩니다. 다음 run은 남은 정확한
  manifest/CAS를 READY-only로 복구하며 provider 호출 수가 0입니다. 별도
  `401`/`403`/`407`, local/unsupported protocol 및 integrity/contract fault는 즉시
  중단되고 secret 없는 systemic report와
  terminal state를 남깁니다.
- 로컬에는 최근 게시 run 2개, 미완료 진단 run 최대 2개와 실행 중인 현재 run만
  남습니다. MCP/WebDAV generation은 최신 2개를 유지하고, PDF CAS는 현재 실행과
  최근 publication seal 2개가 참조하는 SHA만 보호합니다. 같은 SHA의 바이트 파일은
  한 벌뿐이며 prune 상태·삭제 객체 수·삭제 byte 수를 실행 결과에서 확인합니다.
- stable 전용 `cardrag-worker gc` dry-run에서
  `v1/.incoming/{publish,channels}/<32자리 소문자 hex>.tmp`만 orphan 후보가 되고,
  첫 관측 뒤 30일 grace 전에는 삭제되지 않는지 확인합니다. `--apply`는 DELETE마다
  stable pointer를 다시 fence하고 temp leaf를 재검증합니다. 모호한 PROPFIND/path는
  삭제 전에 fail-closed하며, 임의 예외는 raw URL·credential·response·traceback 대신
  고정 JSON과 exit 1만 출력합니다. 일부 DELETE 뒤 실패한 경우에는
  `reason_code=remote_gc_partial_failure`와 이미 성공한 `deleted_count`만 보고합니다.

## 5. 운영 반영

v4를 읽을 수 있는 **v1.0.9 MCP를 먼저** 운영 stable 채널에 배치하고 기존 v3
세대를 정상 제공하는지 확인합니다. 그 다음 v1.0.9 Worker를 stable 채널에서 한 번
실행합니다. 후보 상태 볼륨을 승격할 경우 다음처럼 명시하며, 후보 WebDAV URL과
candidate overlay는 사용하지 않습니다.

```dotenv
CARDRAG_CHANNEL=stable
CARDRAG_WORKER_STATE_VOLUME=cardrag-worker-v109-candidate-state
CARDRAG_ENABLED_ISSUERS=woori,kb,shinhan,samsung
CARDRAG_PDF_CACHE_REFRESH_HOURS=168
CARDRAG_RETAIN_GENERATIONS=2
CARDRAG_RETAIN_INCOMPLETE_RUNS=2
CARDRAG_COLLECT_REMOTE_GARBAGE=true
```

첫 stable v4 세대 게시와 MCP readback, 검색 smoke test가 모두 끝난 뒤에만 v1.0.9
timer를 활성화합니다. 실패하면 v1.0.8 이미지·설치 트리·상태 볼륨과 기존 stable
포인터를 유지한 채 원인을 수정하며, 부분 세대를 강제로 승격하지 않습니다.

## 6. 구버전 2단계 정리

정리는 운영 전환과 분리된 승인 작업입니다. 설치 source와 Git 이력을 구분하며 Git
commit, tag(`v1.0.0`~`v1.0.8`) 및 원격 저장소 이력은 계속 유지합니다.

1. cutover 직후 v1.0.9 stable 게시와 MCP readback을 확인한 다음, 서명한 설치
   inventory에 실제로 존재하는 v1.0.0~v1.0.7 항목과 불필요한 실행 데이터만
   정리합니다. 현재 관측한 물리 source는 `/opt/cardrag-v1.0.0`부터
   `/opt/cardrag-v1.0.6`까지이고 `/opt/cardrag/v1.0.0`부터
   `/opt/cardrag/v1.0.6`까지는 그 source를 가리키는 link입니다. v1.0.7은 현재
   관측되지 않았으므로 생성하거나 삭제 대상으로 추정하지 않습니다. 각 물리 경로와
   link를 별도 exact 항목으로 승인하며 v1.0.8 rollback 자산은 이 단계의 대상이
   아닙니다.
2. 현재 active target인 `/opt/cardrag/v1.0.8`과 legacy v1.0.8 상태 volume은 명시한
   rollback window 동안만 보존합니다. window가 끝나고 backup 복구시험, 최소 2회
   연속 stable 성공, MCP readback을 모두 증빙한 뒤 exact inventory를 다시
   승인합니다. `/opt/cardrag/current`가 승인된 v1.0.9 target으로 바뀌었고 해당
   v1.0.8 경로·volume에 연결된 프로세스나 컨테이너가 없으며 v1.0.9 Worker/MCP의
   source 또는 destination으로 사용되지 않음을 확인한 경우에만 정확히 기록한
   v1.0.8 설치 source와 legacy volume을 최종 제거합니다. 과거 가이드의
   `/opt/cardrag-v1.0.8`이 별도로 존재하면 같은 경로로 간주하지 말고 inventory와
   사용 여부를 따로 검사합니다.

- rollback window의 시작·종료 시각, 책임자, 복구시험 결과와 두 stable run ID를
  변경 기록에 남깁니다. 이 증빙 전에는 v1.0.8 자산을 제거하지 않습니다.
- legacy run PDF 정리 inventory는 50개짜리 CLI sample이 아니라 보존된 canonical
  seed ledger의 `accepted_candidates`와 `unique_accepted_pdf_sha256` 전체를 기준으로
  작성합니다. ledger의 실제 bytes를 `ledger_size_bytes`/`ledger_sha256`로 검증하고
  `legacy_database.sha256`가 승인 당시 DB와 같은지 확인합니다. `missing_sources`와
  `skipped_stale_run_ids`에만 있는 자료는 수용·재사용된 것으로 간주하지 않습니다.
- 이름 패턴이나 glob만으로 Docker volume을 삭제하지 않습니다. `docker volume
  inspect`로 mountpoint와 연결 컨테이너가 없음을 확인하고, 승인된 정확한 volume
  이름만 정리합니다.

현재 운영 완료 전에는 위 정리 항목을 실행하지 않습니다.
