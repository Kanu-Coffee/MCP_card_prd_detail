# 설치와 운영

## 구성과 준비물

Worker는 PDF 수집·OCR·임베딩·게시를 마치고 종료하며, MCP는 검증된 generation을
계속 서비스합니다. 같은 호스트에 설치해도 상태 디렉터리와 Compose 프로젝트를 분리합니다.
Worker는 공유 서빙 볼륨에 쓰고 MCP는 이를 읽기 전용으로 사용합니다.
스케줄러, TLS 프록시와 MCP 클라이언트는 운영자가 준비합니다. WebDAV 서버는 백업이나
WebDAV 게시 호환 모드를 사용할 때만 필요합니다.

- Linux amd64, Docker Engine, Docker Compose 2.24.4 이상
- 선택한 OCR provider의 실행 환경·인증과 OpenRouter 임베딩 API 키
- 백업 또는 WebDAV 게시 사용 시 `HEAD`, `GET`, `PUT`, `MKCOL`, `MOVE`, `PROPFIND`를 지원하는 HTTPS WebDAV
- 고유 MCP Bearer token, 공개 접속 시 HTTPS 프록시

WebDAV 게시 모드에서는 MCP에 읽기 전용 계정을 사용합니다. Worker의 `webdav-check`는
시험 객체를 쓰고 확인하는 외부 작업이므로 대상 저장소를 확인한 뒤 실행합니다.
디스크에는 Worker와 MCP가 각각 보관하는 PDF·DB·vector 복사본과 임시 공간을 모두 계산합니다.

## 관리 운영 호스트의 배포 식별자

현재 운영 검증은 [013 인수 마감](../.handoff/013_local-serving-incremental-webdav-backup/CLOSEOUT_v1.0.35.md)을 기준으로 확인합니다. v1.0.34 공개 이미지 발행·과거 MCP 전환 이력은 [012 보고서](../.handoff/012_v1034-release-cutover/REPORT.md)에 보존합니다.

- 설치 진입점: `/opt/cardrag/current -> /opt/cardrag/013-50a0129`.
- 인수된 운영 이미지: Worker/MCP 각각 `cardrag-worker:013-50a0129`, `cardrag-mcp:013-50a0129`. v1.0.35는 이를 구현한 소스와 운영 검증 기록의 GitHub 릴리스이며 새 Docker Hub 이미지 발행을 뜻하지 않습니다.
- MCP 컨테이너/Compose project: `cardrag-mcp`, 네트워크: `cardrag-mcp_default`.
- 운영 볼륨: `cardrag-mcp-state`, `cardrag-worker-state`, `cardrag-worker-auth`, `cardrag-worker-paddleocr-models`, `cardrag-serving`.
- Worker는 로컬 서빙 볼륨에 게시하고 MCP는 이를 read-only로 읽습니다. WebDAV 장애는 MCP 게시 성공을 취소하지 않습니다.
- WebDAV 백업은 `immediate`, Worker 성공 후 대기분이 있을 때 최대 300초 처리합니다. 대기가 없으면 네트워크 백업을 생략합니다. `hybrid` 선택 시 7회/대기 OCR 항목 30건/1 GiB/7일 중 하나를 충족하면 처리합니다.
- 초기 관리대장 구축은 2026-10-10 00:01에 전량 완료했습니다. 보조 `cardrag-backup`은 정지 상태이며 상시 반복 백업이 가동 중인 것은 아닙니다. 수동 bootstrap 컨테이너도 exit 0으로 완료됐습니다.
- Worker 예약은 매일 03:00 Asia/Seoul입니다. 10월 10일 03시 시도는 경로 권한 오류로 시작 전에 실패했고 권한 수정 및 수동 재실행을 완료했습니다. 다음 실제 systemd 기동 결과는 확인 대상입니다.
- host-local `compose.secrets.yaml`의 이미지·secret 경로·external 볼륨을 보존하십시오.
- systemd `cardrag` 사용자(UID 10001)가 설치 root와 deploy 하위 디렉터리를 탐색하고 Compose/env를 읽을 수 있어야 합니다. 공개 코드 디렉터리는 0755, 비밀 파일은 별도 제한 권한을 유지합니다. 수동 lee 계정의 성공만으로 systemd 접근을 판정하지 않습니다.

아래 신규 설치용 기본 볼륨 이름은 기존 설치의 운영 볼륨을 자동 선택하는 이름이 아닙니다. 기존 호스트의 전환에서는 실제 external 볼륨을 유지합니다.

## 새 설치의 설정

아래 `/opt/cardrag`, `/etc/cardrag`는 예시 경로입니다. 환경에 맞게 선택하고, 인증 파일은
저장소 밖에서 관리하십시오. 기존 설치의 업그레이드는 [RELEASING](RELEASING.md)을 따릅니다.

소스를 설치할 디렉터리에서 역할별 설정을 준비합니다.

```bash
sudo install -d -m 0750 /etc/cardrag /etc/cardrag/secrets
sudo cp deploy/simple.env.example /etc/cardrag/worker.env
sudo cp deploy/simple.env.example /etc/cardrag/mcp.env
sudo chmod 0600 /etc/cardrag/worker.env /etc/cardrag/mcp.env
```

호스트의 Docker 실행 계정이 env 파일을 읽을 수 있고 컨테이너 UID `10001:10001`이
마운트된 비밀 파일을 읽을 수 있도록 소유권·권한을 설정합니다. 비밀 값을 명령줄 인자로
전달하거나 저장소에 기록하지 않습니다. 파일은 줄 하나의 값으로 준비합니다.

| 설정 | Worker | MCP |
|---|---|---|
| `CARDRAG_PUBLICATION_TRANSPORT` | `local` (기본값) 또는 `webdav` | `local` (기본값) 또는 `webdav` |
| `CARDRAG_SERVING_DIR` | 로컬 서빙 볼륨 마운트 경로 (`/var/lib/cardrag-serving`, rw) | 로컬 서빙 볼륨 마운트 경로 (`/var/lib/cardrag-serving`, ro) |
| `CARDRAG_BACKUP_MODE` | `disabled`(기본), `immediate`, `hybrid`, `manual` | 사용하지 않음 |
| `CARDRAG_WEBDAV_BASE_URL` | 선택 사항 (백업 시 HTTPS root) | 선택 사항 (`publication_transport=webdav` 시 사용) |
| `CARDRAG_WEBDAV_USERNAME_SECRET_FILE` / `PASSWORD_SECRET_FILE` | 선택 사항 (백업용 계정 파일) | 선택 사항 (WebDAV 서빙용 조회 계정 파일) |
| `CARDRAG_OPENROUTER_API_KEY_SECRET_FILE` | 문서 임베딩 | 질의 임베딩 |
| `CARDRAG_MCP_BEARER_TOKEN_SECRET_FILE` | 사용하지 않음 | 접속 인증 파일 |
| `CARDRAG_MCP_PUBLIC_BASE_URL` | 사용하지 않음 | 사용자가 접근하는 HTTPS origin |
| `CARDRAG_ENABLED_ISSUERS` | 쉼표로 구분한 8개 canonical 코드 중 선택 | 사용하지 않음 |

기본 운영 모드(`publication_transport: local`)에서는 Worker가 생성한 generation이 로컬 공유 볼륨(`cardrag-serving`)에 즉시 원자적으로 게시되고 MCP가 이를 읽어 서빙하므로, WebDAV 서버나 자격증명 없이도 독립적으로 완결됩니다.
WebDAV는 선택적 증분 백업 용도로 분리되어 동작하며, 기본 백업 모드는 `disabled`입니다. 백업 활성화 시(`hybrid`, `immediate`) SQLite 원장(`backup-ledger.sqlite3`)을 통해 OCR 결과와 참조 CAS PDF만 증분 업로드됩니다.

MCP의 조회 범위는 도구의 `issuer`·`issuers` 인자로 선택합니다. `mcp.env`의
`CARDRAG_ENABLED_ISSUERS`로 서버의 노출 범위를 제한할 수는 없습니다.

Compose 설정의 `*_SECRET_FILE`은 **호스트 파일 경로**입니다. 비밀 overlay가 이를
`/run/secrets/*`로 마운트하고 애플리케이션의 `*_FILE`에 연결합니다. 소스를 직접 실행할
때는 [`.env.example`](../.env.example)의 애플리케이션 설정을 참고하십시오. 이 파일은
자동 로그인이나 실제 인증을 제공하지 않습니다.

설정을 생략한 OCR 기본값은 `local-paddleocr` / `PaddleOCR-VL-1.6`입니다.
`simple.env.example`은 Codex 예시 값을 명시하므로 사용할 provider에 맞춰 수정하십시오.
현재 관리 운영 환경은 `opencode` / `alibaba-token-plan/qwen3.8-flash` / reasoning `medium`을 사용합니다.
Qwen 임베딩의 모델·4,096차원·tokenizer와 provider profile은 generation identity에 묶이므로 기존 vector와
다른 모델을 섞지 않습니다. API의 제공 모델·권한은 해당 계정에서 확인합니다.

로컬 CPU OCR은 `local-paddleocr` provider와 별도 Compose overlay를 사용합니다. 이 경로는
PaddleOCR 3.7.0의 PaddleOCR-VL 1.6 full document pipeline을 약 300 DPI로 실행하며 OCR용
외부 API를 호출하지 않습니다. 후속 임베딩은 기존 OpenRouter 설정을 계속 사용합니다.

OpenCode OCR은 `opencode` provider와 `deploy/worker/compose.opencode.yaml` overlay를 사용합니다.
기본 모델은 `alibaba-token-plan/qwen3.8-flash`이며 reasoning effort는 `medium`입니다.
CLI 실행 환경은 pinned binary(`1.18.34`)를 사용하며, 도구 비활성화(`tools: {"*": false}`) 및
권한 거부(`permission: {"*": "deny"}`)가 강제된 전용 에이전트(`agent="ocr"`, `--pure --format json`)로
동작합니다. API 키는 `ALIBABA_TOKEN_PLAN_API_KEY` 환경 변수 또는 비밀 마운트로 전달합니다.

선택한 호스트와 경로로 env 예시를 수정한 후, 출력에 비밀이 포함되지 않는 설정 검사를
수행합니다. `config --quiet`는 실제 자격증명이나 원격 저장소까지 검증하지 않습니다.

```bash
docker compose --env-file /etc/cardrag/worker.env \
  -f deploy/worker/compose.yaml -f deploy/worker/compose.secrets.yaml config --quiet
docker compose --env-file /etc/cardrag/mcp.env \
  -f deploy/mcp/compose.yaml -f deploy/mcp/compose.secrets.yaml config --quiet
```

백업 또는 WebDAV 게시를 사용하면 해당 역할의 `compose.secrets.webdav.yaml`을 추가합니다.
로컬 서빙만 사용하는 MCP에는 WebDAV 계정 overlay를 추가하지 않습니다.
사설 WebDAV CA가 있으면 역할별 `compose.ca.yaml`을 마지막 overlay로 추가하고
`CARDRAG_WEBDAV_CA_SECRET_FILE`에 호스트 인증서 경로를 지정합니다. TLS 검증을 끄지 않습니다.

## 이미지와 초기 인증

검증된 release digest가 있다면 `CARDRAG_WORKER_IMAGE`와 `CARDRAG_MCP_IMAGE`에 각각
고정하십시오. 소스 빌드에는 아래 명령을 사용합니다.

```bash
docker compose --env-file /etc/cardrag/worker.env -f deploy/worker/compose.yaml build worker
docker compose --env-file /etc/cardrag/mcp.env -f deploy/mcp/compose.yaml build mcp
```

로컬 PaddleOCR Worker는 Python 3.13/glibc 기반의 선택적 이미지입니다. 모델 cache를 먼저
준비하면 이후 OCR은 네트워크 없이 실행할 수 있습니다.

```bash
docker compose --env-file /etc/cardrag/worker.env \
  -f deploy/worker/compose.yaml -f deploy/worker/compose.paddleocr.yaml \
  build worker
docker compose --env-file /etc/cardrag/worker.env \
  -f deploy/worker/compose.yaml -f deploy/worker/compose.paddleocr.yaml \
  run --rm worker paddleocr-prefetch
```

`worker.env`에는 `CARDRAG_OCR_PROVIDER=local-paddleocr`와
`CARDRAG_OCR_MODEL=PaddleOCR-VL-1.6`을 설정합니다. 기본 cache volume은
`cardrag-worker-paddleocr-models`이며 `CARDRAG_PADDLEOCR_MODEL_VOLUME`으로 바꿀 수 있습니다.
완전 오프라인 실행 전에는 온라인 환경에서 위 prefetch를 성공시키고 같은 volume을 옮긴 뒤,
`--network none`에서도 로컬 PDF에 대한 OCR smoke test가 통과하는지 확인합니다.

`codex-exec`을 선택한 경우에만 처음 사용하는 **새 Codex 인증 볼륨**에서 로그인합니다.

```bash
docker compose --env-file /etc/cardrag/worker.env -f deploy/worker/compose.yaml \
  run --rm --entrypoint codex worker login --device-auth
```

인증은 Worker 상태가 아닌 별도의 `codex-home` 볼륨에 저장됩니다. 다른 호스트 프로세스의
로그인을 자동으로 공유하지 않습니다. 기본 신규 볼륨은 다음과 같습니다.

| 역할 | 신규 기본 볼륨 |
|---|---|
| Worker 상태 | `cardrag-worker-v122-state` |
| Codex 인증 | `cardrag-worker-v122-codex-home` |
| MCP 상태 | `cardrag-mcp-v122-state` |

`CARDRAG_WORKER_STATE_VOLUME`, `CARDRAG_WORKER_CODEX_HOME_VOLUME`,
`CARDRAG_MCP_STATE_VOLUME`으로 명시할 수 있습니다. 기존 설치에 새 기본값을 적용하면
새 빈 볼륨이 선택될 수 있습니다. 운영 중인 볼륨의 이름과 사용 프로세스를 먼저 확인하십시오.

## 실행

운영자가 새 설치의 stable 게시를 허용하기로 결정한 시점에만 **worker.env**의
`CARDRAG_STABLE_PUBLICATION_APPROVED=true`를 설정합니다. 기본값은 false여서
실수로 stable 포인터를 바꾸지 않습니다. 공유 OCR cache 쓰기와 GC는 별도 권한입니다.

```bash
docker compose --env-file /etc/cardrag/worker.env \
  -f deploy/worker/compose.yaml -f deploy/worker/compose.secrets.yaml \
  run --rm worker webdav-check
docker compose --env-file /etc/cardrag/worker.env \
  -f deploy/worker/compose.yaml -f deploy/worker/compose.secrets.yaml \
  run --rm worker run
```

PaddleOCR를 선택한 경우 위 두 명령 모두 `compose.paddleocr.yaml`을 마지막 `-f` 인자로
추가합니다. CPU 추론은 문서당 직렬 실행되고 기본 timeout은 4시간입니다. 결과 위치와
`ocr.md`/manifest/CAS 계약은 다른 OCR provider와 동일합니다. 300 DPI 실제 3페이지
상품안내장 검증에서는 약 34분과 최대 약 8.4 GiB 메모리가 관찰됐으므로, 운영 호스트에는
문서 복잡도에 따른 추가 여유를 확보합니다.

OpenCode를 선택한 경우 `compose.opencode.yaml`을 overlay로 추가합니다.
```bash
docker compose --env-file /etc/cardrag/worker.env \
  -f deploy/worker/compose.yaml -f deploy/worker/compose.opencode.yaml \
  run --rm worker run
```
후보 검증 시 기존 stable 운영 환경의 systemd timer(`cardrag-worker.timer`)나 프로덕션 WebDAV/state는
영향을 받지 않으며, 격리된 candidate overlay(`deploy/worker/compose.candidate.yaml`) 및
독립된 state 볼륨을 사용하여 무중단 공존을 유지합니다.

Worker의 종료 코드와 terminal 결과, 검증된 게시 결과를 확인한 뒤 MCP를 실행합니다.

```bash
docker compose --env-file /etc/cardrag/mcp.env \
  -f deploy/mcp/compose.yaml -f deploy/mcp/compose.secrets.yaml up -d --no-deps mcp
curl --fail http://127.0.0.1:8000/health/live
curl --fail http://127.0.0.1:8000/health/ready
```

`live`는 프로세스 생존, `ready`는 유효한 generation 제공 가능 여부입니다. 아직 게시된
데이터가 없으면 ready는 503입니다. 포트가 다르면 health URL도 맞춰야 합니다.
MCP 외부 주소는 TLS 프록시를 통해 연결하고 `/mcp`에 Bearer token을 전달합니다.
클라이언트별 조사 템플릿과 예약·전송 이력은 CardRAG 저장소와 분리하십시오.

## 예약과 상태 확인

systemd 템플릿은 선택 사항입니다. 설치 경로, 실행 계정, env 소유권을 확인한 뒤
`deploy/worker/cardrag-worker.service`와 `.timer`를 설치합니다. 기본 예약은 한국 시간
03:00이며 필요에 맞게 변경하십시오. 기존 실행의 전환 중에는 예약을 즉시 활성화하지 않습니다.

```bash
systemctl status cardrag-worker.service cardrag-worker.timer
journalctl -u cardrag-worker.service -n 100 --no-pager
```

외부 모니터가 live `worker-state.sqlite3`를 열면 안 됩니다. Worker는 단일 writer lock을
보유합니다. 기존 실행이 진행 중일 때 추가 실행의 예약 실패는 기존 writer의 실패를
뜻하지 않습니다. 로그와 실제 컨테이너 상태를 함께 확인하십시오.

SIGTERM 이후 Worker는 진행 중인 변경을 정리하고 게시 정합성을 확인한 뒤 lock을
해제합니다. `TimeoutStopSec=infinity`, `SendSIGKILL=no`는 이 수명주기를 보존합니다.
이미지·설치 symlink·상태·인증·예약 변경은 실행이 자연 종료한 후 별도 전환 절차로 수행합니다.

## 주요 한도와 외부 변경 권한

| 설정 | 기본값과 의미 |
|---|---|
| Worker/MCP `MAX_STATE_BYTES` | 역할별 128 GiB |
| Worker/MCP `RESERVED_FREE_SPACE_BYTES` | 역할별 2 GiB |
| `WORKER_MINIMUM_START_FREE_BYTES` | 2 GiB, 후보 overlay는 32 GiB |
| Worker/MCP `MAX_VECTOR_SIDECAR_BYTES` | 16 GiB |
| Worker/MCP `MAX_SERVING_DATABASE_BYTES` | 32 GiB |
| `MCP_MAX_GENERATION_DOWNLOAD_BYTES` | 64 GiB |
| `MCP_MAX_RESIDENT_VECTOR_BYTES` | 1 GiB |
| `PDF_CONCURRENCY` / `PDF_CONCURRENCY_PER_ISSUER` | 전체 8 / 카드사별 2 |
| `LOCAL_PROCESSING_WORKERS` | 4 |
| `PDF_CACHE_REFRESH_HOURS` | 168시간 |

표의 설정명에는 `CARDRAG_` 접두사가 붙습니다.
운영 Worker의 시작 하한은 기본 **2 GiB**(`WORKER_MINIMUM_START_FREE_BYTES=2147483648`)이며,
후보 검증 overlay(`deploy/worker/compose.candidate.yaml`)에서만 격리 및 중복 구동을 위해 32 GiB를 강제합니다.
실제 런타임 디스크 보호는 고정 하한이 아닌 파생 뷰·캐시 miss 기반의
**동적 `peak_growth + 2 GiB reserve` preflight** 및 작업 진행 중 재검사로 이루어집니다.
호스트 여유 공간(`< 80 GB`) 도달 시 수행하는 유지보수 점검은 런타임 거부 preflight 오류와 구분되는
운영자 점검 기준입니다. 용량 부족 시 작업을 거부하며 여유 공간을 자동으로 확보하지 않습니다.
MCP의 지속 quota 정책을 바꾸거나 남은 reservation을 정리할 때는 [RECOVERY](RECOVERY.md)를 따릅니다.

백업의 주기·시간 예산·대기 항목 처리는 [증분 백업 안내](RECOVERY.md#증분-webdav-백업-운영)를 따릅니다.
아래 원격 generation 검증 정책은 `CARDRAG_PUBLICATION_TRANSPORT=webdav`를 선택한 호환 모드에 적용됩니다.
WebDAV는 검증 이력이 있는 기존 객체를 재사용하고 성공 실행 14회·7일·신규 CAS 10 GiB 중
먼저 도달한 조건에서 전체 검증합니다. 신규 DB·vector는 최종 경로에서 크기·해시를
검증합니다. `CARDRAG_WEBDAV_FORCE_FULL_VERIFY=true`는 강제 검증,
`CARDRAG_WEBDAV_VERIFICATION_MODE=strict`와 `GENERATION_READBACK_MODE=double`은
항상 전체 검증과 이중 readback을 선택합니다. 후자에도 `CARDRAG_WEBDAV_` 접두사가 붙습니다.

`CARDRAG_OCR_CACHE_MODE=read-only`가 기본입니다. 공유 cache 쓰기는 stable 게시 권한과
별도로 `CARDRAG_OCR_CACHE_PUBLICATION_APPROVED=true` 및 `read-write`를 요구합니다.
이미 처리된 corpus를 OCR 공급자 호출 없이 재기동해야 할 때는
`CARDRAG_OCR_CACHE_REQUIRE_HIT=true`를 함께 설정합니다. 이 모드는 `read-only`에서만
허용되며 cache miss가 발생하면 OCR을 호출하지 않고 Worker를 실패시킵니다. 성공 실행의
`ocr_provider_documents=0`과 `ocr_cache_reused=ocr_expected`를 확인하기 전에는 완전한
cache 재사용으로 간주하지 않습니다. 이 보호 모드의 기본값은 `false`이며, 일별 신규 OCR을
수집하는 정상 배치에서는 설정하지 않습니다.
원격 삭제는 `CARDRAG_REMOTE_GC_APPROVED=true`, `CARDRAG_COLLECT_REMOTE_GARBAGE=true`,
stable 게시 권한이 모두 있어야 수행합니다. 설정 예시는 세 권한을 기본으로 끕니다.

인증된 `/metrics`에서 처리 결과·시간·응답 크기·캐시 사용량을 확인합니다. 질의문,
상품명과 사용자는 metric label에 넣지 않습니다. 상세 실패 원문과 운영 증빙은 접근을
제한한 별도 위치에 보관하십시오.

## 카드사 병렬 수집과 일부 카드사 장애

Worker는 카드사 목록을 최대 `CARDRAG_ISSUER_DISCOVERY_CONCURRENCY`개(기본 4, 범위 1~8)
병렬 수집합니다. 카드사별 목록 수집은 재시도 시간을 포함해
`CARDRAG_ISSUER_DISCOVERY_TIMEOUT_SECONDS`초(기본 300) 안에 종료됩니다.
PDF 다운로드는 기존 전역 8개/카드사별 2개 제한과 요청 간격을 유지합니다.
모든 카드사의 수집이 성공 또는 격리로 종료된 뒤 OCR을 시작합니다.

목록·다운로드 단계에서 카드사 origin 연결 또는 파싱이 실패하면 해당 카드사의
새 부분 결과를 이번 게시에서 제외합니다. 나머지 카드사의 수집은 계속하며,
실패 카드사는 실제 서비스 중 generation의 PDF/OCR과 현재·과거 개정 관계를
검증하여 유지합니다. 실패를 빈 정상 목록이나 상품 단종으로 기록하지 않고,
마지막 origin 성공 시각도 갱신하지 않습니다. 다음 배치에서 다시 수집합니다.
원격 PDF 복원이 필요한 경우 WebDAV CAS를 검증해 받으며 카드사 origin은 호출하지 않습니다.

`runs/<run_id>/reports/issuer-collection.json`에서 issuer별 단계, 실패 reason,
시도 수, 건수, carry 기준 generation을 확인합니다. 수집 중 보고서는
`terminal=false`이며, 수집 종료 뒤 `terminal=true`입니다.
일부 실패 + 나머지 정상 작업 완료는 `collection_status=degraded`와 경고를 남기고
run은 `succeeded` 또는 `no_change`, 종료 코드 0을 반환합니다.
이 결과는 실패 카드사의 최신화 성공을 의미하지 않습니다.
모든 카드사가 실패하면 nonzero로 종료하고 OCR이나 게시를 수행하지 않습니다.
SQLite·디스크 및 서빙 산출물의 무결성 실패는 전체 실행 실패로 처리합니다.
WebDAV 게시 모드의 게시 오류도 실패하지만, 로컬 게시 모드의 선택적 백업 오류는 별도 백업 상태로 기록합니다.

기존 OCR content 캐시는 모델 변경 후에도 재사용합니다. 실패 카드사 대상
수동 재OCR 요청은 대기 상태로 유지하며 완료 영수증을 만들지 않습니다.
원격 GC 승인과 epoch는 이 장애 대응을 위해 변경하지 않습니다.

수동 장기 배치는 고유한 컨테이너 이름을 지정하고 `--rm` 없이 detached로 시작합니다.
예: 검증된 운영 wrapper의 `run -d --no-deps --name cardrag-prod-008-first worker run`.
`docker logs -f --tail 50 cardrag-prod-008-first`로 관찰하고,
`docker inspect cardrag-prod-008-first --format '{{.State.Status}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}}'`
에서 `exited`와 exit 0을 함께 확인합니다. running 중 ExitCode 0은 완료 근거가 아닙니다.
예약 배치와 같은 Worker state/lock을 사용하고 중복 기동하지 않습니다.
