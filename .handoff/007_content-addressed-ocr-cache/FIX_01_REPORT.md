# FIX 01 REPORT — 운영 이전 진행 중, 백그라운드 후속 절차 인계

작성 역할: Executor. 2026-10-07 13:40 KST 기준. **FIX_01은 아직 완료되지 않았다.** 사용자는 Worker가 장시간 걸리므로 구동 후 지속 모니터링하지 말고, 오류 또는 완료를 사용자가 알려준 다음 재개하도록 지시했다. 현재는 Worker 첫 배치에 선행하는 WebDAV 마이그레이션 자체가 장시간 실행 중이다. 이를 토큰을 쓰며 기다리지 않도록, 완료 후 검증과 Worker 구동까지 수행하는 일회성 백그라운드 절차를 시작했다. 이 보고서는 현재까지의 실행 기록이고, 최종 인수 결과를 뜻하지 않는다.

## 배포 준비와 완료된 검증

- 코드 기준: `31edb1da1033b85e81bb30e0cf98e7e055e5d892`, 브랜치 `feat/007-content-ocr-cache`. PR #40의 CI run `37565367823` / runtime job `112611722697` **pass**, 4분 47초. 제품 코드는 변경하지 않았고 전체 suite를 추가 반복하지 않았다.
- `git archive HEAD`로 `/opt/cardrag/007-31edb1d`에 운영 소스 snapshot을 준비했다. 기존 `/opt/cardrag/v1.0.29`는 롤백용 1세트로 유지한다. `/etc/cardrag/*.env`는 수정하지 않았으며 checksum과 기존 포인터·이미지를 `operations/rollback.json`에 기록했다.
- 다음 두 이미지를 그 커밋에서 `APP_VERSION=1.0.30-007`, `VCS_REF=<full SHA>`로 빌드했다. 두 빌드 모두 종료 코드 0이며, OCI revision도 일치한다. 내부 배포 이미지이고 정식 공개 릴리스는 수행하지 않았다.

| 역할 | 태그 | 로컬 image ID |
|---|---|---|
| MCP | `cardrag-mcp:007-31edb1d` | `sha256:1ddf263a7024c4ddf982ee5956938fd7fa195f9318e97901293301ebbaf9f6f7` |
| Worker | `cardrag-worker:007-31edb1d` | `sha256:2b1a1257f3348c1a8b2465282847ef633c0ca6514025f394c646ae344c598522` |

- 새 배포 디렉터리의 역할별 `compose.secrets.yaml`에 각 이미지 태그를 고정했다. systemd가 이미 사용하는 base+secrets overlay 구조를 유지하면서 `/etc/cardrag` 쓰기 권한 없이 전환하기 위한 호스트 설정이다. 원본 저장소 코드·Compose 파일은 변경하지 않았다. 기존 env의 image 값은 이전 이미지 참조로 남는다. **실제 이미지 선택은 새 배포 디렉터리의 secrets overlay가 우선**한다.
- Worker Compose project는 `cardrag-worker`, MCP는 기존 `cardrag-stable-v1026`다. 기존 Worker state `cardrag-worker-v130-candidate-state`, 인증 `cardrag-worker-v120-recovery-auth-20260910`, MCP state `cardrag-mcp-v129-candidate-state`를 그대로 사용한다. 새 빈 state volume을 만들지 않았다. 기존 Paddle 모델 volume은 마운트 구성을 유지했으나 Paddle 작업을 실행하지 않았다. Compose `config --quiet` 두 역할 모두 통과했다.
- 기존 OCR 설정은 `codex-exec / qwen3.8-flash`, cache `read-write`, epoch 0이다. 006 overlay는 활성화하지 않았다. `CARDRAG_REMOTE_GC_APPROVED=false`, `CARDRAG_COLLECT_REMOTE_GARBAGE=false`, stable/cache publication 승인 true를 확인했다. 운영 state를 읽기 전용 마운트해 `load_next_reprocess_request()` 결과 **pending 없음**도 확인했다. 강제 재OCR·재임베딩 요청은 만들지 않았다.
- 기존 stable generation의 문서별 PDF SHA/크기와 OCR SHA/크기를 `operations/stable-ocr-baseline.json`에 저장했다. generation `g-03fbc4f18a3c450bb017e2fd-36bae25dd8cd`, OCR 문서 5,512개다. OCR 본문·자격정보는 저장하지 않았다.
- 새 이미지의 운영 `ocr-cache migrate --dry-run` 종료 코드 **0**. 결과: `legacy_variants=1818`, `generation_only_variants=3354`, `total_variants=5172`, `stable_documents_pinned=5512`, `conflicting_pdf_keys=87`. stable ID는 위 기준과 같다. 원본 SHA/바이트 검증 실패 없이 전량 계획을 구성했다.
- MCP를 기존 project/volume/포트(127.0.0.1:18015)로 교체했다. 기존 대용량 DB·PDF·vector 검증 때문에 시작 후 준비 응답까지 약 7분이 걸렸으며 그동안 준비 응답은 실패했다. 이후 **healthy / `/health/ready` HTTP 200**, 인증된 `/resources/issuers` **8개**, `/resources/products?issuer=hana` **724개** 응답을 확인했다. 새 MCP가 기존 stable을 계속 제공한다.
- 디스크는 이미지 빌드 전 약 76G, 빌드 후 약 67G available로 관측했다. 고정 숫자로 Worker preflight를 우회하지 않았고, 실제 첫 run의 동적 preflight 결과는 아직 없다. 운영 volume·OCR/CAS를 삭제하지 않았다.

## 운영 마이그레이션 및 백그라운드 절차

실행 명령 wrapper는 `/opt/cardrag/007-31edb1d/operations/worker-compose.sh`와 `mcp-compose.sh`다. 각 wrapper는 위 project, `/etc/cardrag/<role>.env`, 새 디렉터리의 base+secrets Compose를 사용한다.

```text
worker-compose.sh run --rm --no-deps worker ocr-cache migrate --dry-run
mcp-compose.sh up -d --no-deps --no-build --pull never mcp
worker-compose.sh run --rm --no-deps worker ocr-cache migrate --apply --confirm-stable-generation g-03fbc4f18a3c450bb017e2fd-36bae25dd8cd
```

`--apply`는 **13:32:03 KST에 시작**했고, 현재 컨테이너 `cardrag-worker-worker-run-06c76327a77e` / ID `3c1e472d6754857bc6c2dab43a377bfee6401eb582770767c80f61c11619cb51`가 실행 중이다. 약 6분 시점에 content index **319개 게시**를 1회 확인했다. 아직 완료 JSON/종료 코드가 없으므로 이전 성공으로 판정하지 않았다.

일회성 controller `/opt/cardrag/007-31edb1d/operations/finish-transition.py`를 독립 세션으로 실행했다. PID `1624913`; 보고 시점 `transition-status.json`은 **`waiting_for_migration`**이다. controller는 다음 순서로 수행하며 재시도나 Worker 완료 모니터링 루프가 없다.

1. 현재 migration 컨테이너에 `docker wait`하여 종료 코드 0을 확인하고 apply 요약 JSON의 stable ID·전량 매핑을 검사한다.
2. 같은 이미지·설정으로 `ocr-cache verify`를 1회 실행한다. `verified_variants >= 5172`, `stable_ocr_documents_covered=5512`, 같은 stable ID, 종료 코드 0을 요구한다. 실패하면 다음 단계로 가지 않는다.
3. 배포 이미지 ID가 그대로이고 새 MCP가 healthy/ready인지 확인한다.
4. 신한카드의 실제 discovery URL을 가볍게 1회 확인한다. 여전히 reset/오류면 **`source_blocked_no_worker`**로 기록하고 Worker를 시작하지 않는다. 해당 장애는 이번 전환 이전에도 있었고, 작업 중 호스트 GET에서도 재현됐다.
5. 실행 중인 다른 Worker가 없고 `/opt/cardrag/current`가 기존 경로인지 확인한 뒤, 현재 symlink를 새 배포 경로로 원자적으로 바꾼다.
6. `worker-compose.sh run -d --no-deps --name cardrag-prod-007-fix01 worker run`으로 Worker를 한 번 시작한다. **`--rm`을 사용하지 않아 종료 결과를 보존**한다. 최초 running 상태·컨테이너 ID·이미지·시작 시각을 `worker-start.json`에 기록하고 controller는 종료한다. Worker 결과를 주기적으로 확인하지 않는다.

controller의 문법 검사와 wrapper `shellcheck`는 통과했다. 백그라운드 PID와 초기 상태 파일을 확인했다. controller 전체의 실제 성공은 아직 확인하지 않았다.

## FIX 지시와의 편차 및 안전 근거

- systemd timer 중지는 `systemctl --no-ask-password`에서 interactive authentication 필요로 거부됐고, `sudo -n`도 거부됐다. **timer를 수정/중지하지 않았다.** 기존 timer는 active이며 다음 발화는 **2026-10-08 03:00 KST**다. 현재 기존 정상 Worker는 실행 중이 아니며, migration과 첫 Worker는 기존 state의 같은 `worker.lock`을 사용한다. 예약 서비스 제어 권한을 얻거나 임의로 systemd 설정을 우회하지 않았다. 현재 scheduled Worker의 `failed` 상태는 이전 신한 수집 실패 기록이다.
- MCP가 기존 데이터 로딩 중일 때 추가형 migration을 시작했다. 이 작업은 stable 포인터·기존 OCR/CAS를 바꾸지 않는다. Worker 구동은 migration 전량 검증과 MCP readiness를 모두 충족한 뒤로 제한했다. MCP 준비 상태는 이후 실제로 확인했다.
- 사용자 토큰 절약 지시를 반영해, migration 종료·verify·source gate·첫 Worker 시작을 위 일회성 controller로 이어간다. 기존 원본은 삭제하지 않으며 stable 변경을 발견하면 migration이 중단한다. **첫 Worker는 아직 구동되지 않았다.** source gate를 통과하지 못하면 기존 current/예약 Worker는 그대로 유지되고, 새 MCP 및 추가형 content만 남는다.

## 사용자가 상태를 알려준 뒤 이어서 할 일

먼저 `/opt/cardrag/007-31edb1d/operations/transition-status.json`과 `transition.log`를 읽는다. controller나 migration을 중복 실행하지 않는다. `waiting_for_migration` 또는 `verifying_content`면 아직 선행 작업 중이다. `source_blocked_no_worker`면 신한 외부 연결 오류가 남은 것이므로 Worker를 반복 기동하거나 issuer를 제거하지 않는다. `transition_failed_no_retry`면 기록된 실패 게이트만 조사한다.

`worker_started`이면 Docker 컨테이너 **`cardrag-prod-007-fix01`**이 첫 배치다. 사용자는 `docker logs -f cardrag-prod-007-fix01`로 관찰할 수 있다. 종료 뒤에는 `docker inspect cardrag-prod-007-fix01 --format '{{.State.Status}} {{.State.ExitCode}}'`로 결과를 확인한다. Executor는 사용자의 완료/오류 알림을 받은 다음 로그·종료 코드·최종 run 지표를 점검하고, 기존 동일 PDF 문서의 OCR 호출 0·baseline SHA/크기 불변·새 generation 게시·MCP 반영·timer 상태를 검증한다. 종료 결과를 확인하기 전까지 컨테이너를 삭제하지 않는다.

최종 결과는 이 보고서에 **추가 기록**한다. 기존 내용을 덮어쓰거나 FIX_01 완료/인수로 미리 표시하지 않는다. 공개 릴리스, PR 병합, 006 OpenCode 활성화는 이 작업에 포함되지 않는다.

## 2026-10-07 14:56 KST — 중단 원인 확인 및 전량 검증 재개

사용자가 "worker 중지됨"을 알려와 실제 Docker·controller 상태를 확인했다. **정상 배치 Worker `cardrag-prod-007-fix01`은 아직 만들어진 적이 없다.** migration 컨테이너는 Docker `wait`에서 종료 코드 **0**을 반환했고 `operations/migration-exit-code.txt`에 보존됐다. 하지만 `migration-apply.json`이 0바이트여서 controller는 **14:51:20 `Migration success JSON was not written`**으로 중단했다. 원래 연결 실행 세션도 더 이상 존재하지 않았다. 결과가 실행 세션의 연결 stdout 파일에 의존했던 구조가 문제였으며, WebDAV 이전 실패나 OCR 작업 실패로 판정한 것이 아니다.

- **마이그레이션 apply를 다시 실행하지 않았다.** 기존 종료 코드 0과 기록된 dry-run 계획을 사용하고, 실제 원격 전량 검증을 추가 완료해야 다음 단계로 진행하도록 controller를 수정했다. 비어 있는 apply JSON에 성공 결과를 만들어 넣지 않았다.
- `/opt/cardrag/007-31edb1d/operations/finish-transition.py`의 최초 버전을 `finish-transition.initial.py`로 보존했다. 수정본은 검증을 `run -d --no-deps --name cardrag-007-content-verify-fix01 worker ocr-cache verify`로 실행한다. `--rm`을 쓰지 않고 **Docker가 종료 코드·로그를 보존**하도록 했다. controller는 `docker wait` 후 `docker logs`로 결과 JSON과 stderr를 수집한다. 기존 동일 이름의 검증 컨테이너가 있으면 이미지·명령을 확인하고 이어받아 중복 구동을 방지한다.
- controller 문법 검사를 통과했고 독립 세션으로 재개했다. **14:56:13 KST**, controller PID **1706261**, 검증 컨테이너 **`cardrag-007-content-verify-fix01` running**을 확인했다. 사용 이미지 ID와 명령은 기존 승인된 새 Worker 이미지 / `["ocr-cache", "verify"]`다. 상태 파일은 **`verifying_content`**다. 이 검증은 OCR·LLM 추론을 호출하거나 WebDAV에 쓰지 않는다.
- 검증 성공 시 여전히 같은 stable ID, 최소 5,172 verified variant, stable OCR 문서 5,512개 전량 대응을 요구한다. 그 근거는 별도 `migration-recovery-verified.json`에 기록한다. MCP·이미지·신한 source gate 이후의 Worker 구동 절차는 유지한다. 최초의 잘못된 migration stdout 의존 게이트만 제거했다.
- 재확인 시 MCP ready는 HTTP **200**, root available 약 **67G**, `/opt/cardrag/current`는 아직 `/opt/cardrag/v1.0.29`다. 신한 discovery URL의 GET은 여전히 **connection reset / HTTP 000**으로 실패했다. source gate가 회복되지 않으면 검증 후 `source_blocked_no_worker`로 종료하는 것이 예상되며, issuer 제외나 실패 은폐는 하지 않는다.

현재 **FIX_01 진행 중**이다. 사용자의 비용 절약 지시에 따라 검증/배치를 계속 폴링하지 않고 턴을 종료한다. 다음 완료·오류 알림 때 `transition-status.json`, 검증 컨테이너의 종료 코드/로그, 수집된 `migration-verify.json`을 먼저 확인한다. 기존 마이그레이션이나 검증을 무조건 재실행하지 않는다.

## 2026-10-07 15:12 KST — 전량 검증 성공 확인 및 실제 배치 구동

사용자가 기존 컨테이너 `3c1e472d6754`도 Worker였음을 지적했다. **그 컨테이너는 Worker 이미지로 `ocr-cache migrate --apply`를 실행했고 `--rm`에 따라 종료 후 삭제됐다.** 이전의 "Worker는 기동된 적 없다"는 설명은 부정확했으며, 정확히는 **일반 배치 명령 `worker run`이 아직 시작되지 않았던 것**이다. migration의 Docker 종료 코드 0은 보존되어 있다.

전량 검증 컨테이너 `cardrag-007-content-verify-fix01` / `d56551eadf39`는 **14:56:13~14:58:22 KST**, 종료 코드 **0**으로 완료됐고 `docker ps -a`에 남아 있다. 현재 실행 목록에 안 보이는 것은 종료됐기 때문이다. 결과: `verified_variants=5172`, `stable_ocr_documents_covered=5512`, stable ID `g-03fbc4f18a3c450bb017e2fd-36bae25dd8cd`, `read_only=true`. **이전 산출물 전량 검증은 통과**했으며 `migration-recovery-verified.json`에 기록됐다. controller는 이후 신한 source GET의 `ConnectionResetError` 때문에 14:58:26에 `source_blocked_no_worker`로 종료한 상태였다.

사용자가 해당 상태 확인 후 **실제 배치를 구동하라고 지시**했으므로, 외부 source 사전검사 때문에 구동을 보류하던 규칙 대신 정상 배치에서 실제 결과를 확인하도록 진행했다. 신한 오류가 남아 있어 배치가 discovery에서 실패할 수 있다는 점을 사용자에게 설명했다. source 구현이나 issuer 목록은 변경하지 않았다.

- 전량 검증 성공, 배포 이미지 ID, 새 MCP healthy, 다른 실행 중 Worker 없음, 기존 current 포인터를 확인했다.
- `/opt/cardrag/current`를 `/opt/cardrag/007-31edb1d`로 원자적으로 전환했다. 기존 `/opt/cardrag/v1.0.29`는 롤백용으로 남아 있다.
- `operations/worker-compose.sh run -d --no-deps --name cardrag-prod-007-fix01 worker run` 실행이 종료 코드 **0**으로 컨테이너를 구동했다.
- **구동 시각 15:12:14 KST**, 실제 배치 컨테이너 **`cardrag-prod-007-fix01`**, ID **`c735cee66ba086ef45baa25d79bd80285d91bbc5edd166cc9846c5873c2fe724`**, 명령 **`["run"]`**, **`AutoRemove=false`**, running 상태를 한 번 확인했다. `worker-start.json`과 `transition-status.json`에 기록했다.
- 시작 로그의 capacity preflight는 통과했다(`filesystem_free_bytes=71075037184`, 최소 하한 2 GiB). OCR 설정과 기존 state/auth 볼륨, GC false, timer active는 그대로다. 호환 계약 탐색 로그에 Paddle 이름이 나타나지만 운영 provider는 기존 `codex-exec / qwen3.8-flash`이며 강제 Paddle 재처리는 요청하지 않았다.

**사용자 감시 기준:** `docker logs -f --tail 50 cardrag-prod-007-fix01`로 실행 로그를 보고, 종료하면 `docker inspect cardrag-prod-007-fix01 --format '{{.State.Status}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}}'`로 판정한다. **`exited`와 exit 0이 함께 나올 때 정상 종료**이고, nonzero/OOM이면 오류다. running 중의 ExitCode 0은 완료 근거가 아니다. 실행 목록에서 사라지면 `docker ps -a`에서 종료 컨테이너를 찾는다. `transition-status.json`은 구동 시점 기록이므로 Worker 완료 여부를 자동 갱신하지 않는다. 이번 수동 배치는 Docker 컨테이너 기준이며 systemd service의 과거 failed 기록과 구분한다.

장시간 배치 모니터링은 수행하지 않는다. 사용자의 오류/완료 알림 뒤에 실제 exit·로그·run 지표·OCR SHA 보존·새 generation·MCP 반영을 확인해 이 보고서에 추가한다. **FIX_01 최종 완료 및 운영 인수는 아직 미판정**이다.

## 2026-10-07 15:20 KST — 첫 실제 배치 실패 확인

사용자가 제공한 로그와 보존된 Docker 컨테이너를 대조했다. 실제 배치 `cardrag-prod-007-fix01` / `c735cee66ba0` / run **`bb471003c6844c7394aae97fd9f85f26`**은 **15:17:16 KST에 종료 코드 1**, `OOMKilled=false`, `AutoRemove=false`로 종료됐다. 로그 전체를 `operations/worker-first-run.log`, 결과 요약을 `worker-result.json`에 보존하고 상태 파일을 **`worker_failed`**로 갱신했다.

배치는 startup/capacity preflight를 통과하고 우리 discovery **915 records**, KB **751 records**를 수집했다. 다음 신한 `discover_current()`의 첫 공지 landing GET에서 응답 헤더를 읽기 전에 **`httpcore.ReadError` → `httpx.ReadError`**가 발생했다. CLI는 `error_class_category=network`, `reason_code=worker_unexpected_failure`, `status=failed`를 기록했다. 실패 보고서 경로는 `runs/bb471003c6844c7394aae97fd9f85f26/reports/worker-failure.json`이다. **OCR 처리·새 generation 게시 검증 단계에 도달하지 못했다.** 이번 로그를 content cache 무재호출·첫 generation 전환 성공 근거로 사용하지 않는다.

호스트에서도 실제 신한 discovery URL을 IPv4/TLS 1.2로 요청했으나 원격 IP `210.112.177.1`에서 connection reset / HTTP 000이었다. HTTP/2 및 브라우저 User-Agent/Accept로 신한 홈페이지를 요청한 경우도 connection reset이었다. 기존 설정과 별도의 읽기 요청에서도 같은 증상이 재현되며, 서버 자체 장애와 이 호스트/네트워크에 대한 접근 차단 중 어느 것인지는 아직 구분되지 않았다. 연결 실패를 timeout·메모리·Paddle OCR 과부하로 판정할 근거는 없다.

실패 후 운영 WebDAV stable 포인터를 읽기 전용으로 확인했고 **`g-03fbc4f18a3c450bb017e2fd-36bae25dd8cd` 그대로**다. MCP **healthy / ready HTTP 200**이며 기존 서비스가 유지된다. 앞서 이전·전량 검증된 **5,172 variant / 5,512 stable OCR 문서**의 성공 근거는 유효하다. 현재 새 배포 디렉터리와 추가형 캐시는 유지하며 원격 GC는 켜지 않는다. 정상 배치의 자동 반복 재기동은 수행하지 않았다.

남은 단계는 **신한 수집 연결 복구 후 새 Worker 정상 run 1회**, 기존 동일 PDF OCR SHA/크기·호출 0·새 generation·MCP 반영 검증이다. 현재는 외부 수집 연결에서 막혀 FIX_01 완료/인수로 판정할 수 없다. 재개 전에 가벼운 신한 연결 확인으로 반복 실패를 피하고, 종료 컨테이너와 로그는 최종 확인까지 보존한다.
