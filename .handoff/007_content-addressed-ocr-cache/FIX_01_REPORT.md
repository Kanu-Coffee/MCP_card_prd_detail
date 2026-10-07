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
