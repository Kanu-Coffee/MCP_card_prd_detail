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

과거 Worker 초기화 구현 검증은 [014 인수](../.handoff/014_worker-startup-failure-codex-validation/ACCEPTANCE_FIX_01.md)를 기준으로 확인합니다. 로컬 서빙·백업 기준은 [013 인수 마감](../.handoff/013_local-serving-incremental-webdav-backup/CLOSEOUT_v1.0.35.md)에 보존합니다. v1.0.34 공개 이미지 발행·과거 MCP 전환 이력은 [012 보고서](../.handoff/012_v1034-release-cutover/REPORT.md)에 보존합니다.

- 설치 진입점: `/opt/cardrag/current -> /opt/cardrag/019-fix33-7d2e39edbf59`.
- 운영 Worker: `cardrag-worker:019-fix33-7d2e39edbf59`, image ID `sha256:71eb18e6538de2e0b00fd3c5173045263980322c623b17e3debee7d782d51450`. FIX_34 배포 계정 접근 검증과 매일14:30 예약을 반영했습니다. v1.0.37은 이 운영테스트를 인수하는 GitHub 소스 릴리스이며, 버전 발행 자체가 운영 이미지를 교체하거나 새 registry 이미지를 발행하지 않습니다.
- 운영테스트는 2026-10-25 사용자 승인으로 종료했습니다. 최신 근거는 [019 종료](../.handoff/019_selected-issuer-unresolved-history-preservation/CLOSURE.md), 전체 계획 상태는 [종료 인덱스](../.handoff/STATUS_v1.0.37.md)를 참조하십시오. 이후 예약은 실제 운영으로 계속됩니다.
- MCP 컨테이너/Compose project: `cardrag-mcp`, 네트워크: `cardrag-mcp_default`.
- 운영 볼륨: `cardrag-mcp-state`, `cardrag-worker-state`, `cardrag-worker-auth`, `cardrag-worker-paddleocr-models`, `cardrag-serving`.
- Worker는 로컬 서빙 볼륨에 게시하고 MCP는 이를 read-only로 읽습니다. WebDAV 장애는 MCP 게시 성공을 취소하지 않습니다.
- WebDAV 백업은 `immediate`, Worker 성공 후 대기분이 있을 때 최대 300초 처리합니다. 대기가 없으면 네트워크 백업을 생략합니다. `hybrid` 선택 시 7회/대기 OCR 항목 30건/1 GiB/7일 중 하나를 충족하면 처리합니다.
- 초기 관리대장 구축은 2026-10-10 00:01에 전량 완료했습니다. 보조 `cardrag-backup`은 정지 상태이며 상시 반복 백업이 가동 중인 것은 아닙니다. 수동 bootstrap 컨테이너도 exit 0으로 완료됐습니다.
- Worker 예약은 매일 14:30 Asia/Seoul입니다(2026-10-22 변경). 과거 10월 10일 03시 시도는 경로 권한 오류로 시작 전에 실패했고 권한 수정 및 수동 재실행을 완료했습니다. 실제 배치 결과는 별도로 확인합니다.
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
현재 관리 운영 환경은 `codex-exec` / `gpt-6.1-sol` / reasoning `medium`을 사용하며, fallback은 `local-paddleocr` / `PaddleOCR-VL-1.6`입니다.
Codex가 `provider_process_rate_limited`로 사용한도/요청 제한을 보고하면 설정된 로컬
Paddle fallback으로 문서 전체를 처리합니다. Codex 부분 chunk는 보존하며 두 provider의
본문을 섞지 않습니다. 이 예외는 `codex-exec` → `local-paddleocr` 조합에만 적용합니다.
인증·설정·자격증명 검출·계약/무결성 및 기타 systemic 오류는 계속 배치를 중단합니다.
nonzero provider 종료의 systemic failure report에는 provider·요청 phase·chunk/page 범위,
stdout/stderr 길이와 SHA-256, allowlist 분류 근거만 남기며 원문 출력은 저장하지 않습니다.
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
14:30입니다. 기존 실행의 전환 중에는 예약을 즉시 활성화하지 않습니다.

서비스는 compose 파일을 직접 호출하지 않고 활성 release의
`operations/worker-compose.sh`를 사용합니다. 이 helper가 private runtime overlay와 실제
image·state/auth/model volume을 고정하므로 예약 실행이 candidate image나 다른 state
volume으로 우회하지 않습니다. timer는 `Persistent=false`이므로 재부팅 후 놓친 예약을
자동 catch-up하지 않습니다. 누락된 배치가 필요하면 검증된 수동 명령으로 별도 실행합니다.

```bash
# service/timer 설치, daemon-reload, 그리고 timer의 영속 disable까지 수행한다.
# 이 script는 Worker를 시작하거나 timer를 enable하지 않는다.
sudo deploy/worker/install-systemd.sh
systemctl is-enabled cardrag-worker.timer   # disabled
systemctl is-active cardrag-worker.timer    # inactive
```

release를 복제할 때 `operations`의 그룹은 `cardrag`, 디렉터리는 2750,
실행 helper는 0750, private overlay는 0640을 유지합니다. 복사 도구가 그룹을
`lee`로 바꾸지 않았는지 확인합니다. 비밀 파일을 공개 권한으로 바꾸지 않습니다.
후보 활성화·timer 중지·설치 이전에 아래 검증을 반드시 통과해야 합니다.

```bash
# RELEASE에는 활성화할 후보의 절대 경로를 지정합니다.
sudo /path/to/release/deploy/worker/verify-service-access.sh /path/to/release
```

이 검증은 실제 서비스 계정 `cardrag`와 `docker` 그룹으로 후보 helper의
`config --quiet`를 실행하여 경로 탐색·실행·private overlay/env 읽기를 확인합니다.
원본·OCR·배치를 호출하지 않습니다. root/lee의 config 성공만으로 대체하지 않습니다.
새 release의 `activate.sh`도 이 검증을 포인터 전환 전에 실행해야 합니다.
서비스 `203/EXEC`는 Worker 시작 전 오류로 회차를 소비하지 않으며,
`ExecMainStatus=0`만으로 성공을 판단하지 말고 `Result`와 journal을 함께 확인합니다.

수동으로 같은 작업을 하려면 다음 순서를 사용합니다.

```bash
sudo deploy/worker/verify-service-access.sh /opt/cardrag/current
sudo install -m 0644 deploy/worker/cardrag-worker.service /etc/systemd/system/cardrag-worker.service
sudo install -m 0644 deploy/worker/cardrag-worker.timer /etc/systemd/system/cardrag-worker.timer
sudo systemctl daemon-reload
sudo systemctl disable --now cardrag-worker.timer
```

검증·설정 교정이 끝나고 사용자가 예약 자동운영 전환을 승인한 뒤에만
`sudo systemctl enable --now cardrag-worker.timer`로 재활성화합니다. 재활성화 직후
`systemctl list-timers cardrag-worker.timer`로 다음 실행 시각을 확인하고, 과거 slot을
자동 catch-up하지 않는지 확인합니다.

```bash
systemctl status cardrag-worker.service cardrag-worker.timer
journalctl -u cardrag-worker.service -n 100 --no-pager
```

배치 전 운영 점검은 실제 mount의 여유량·inode·Docker 회수 가능량 확인과 초기검증으로 구성합니다.

```bash
df -B1 / ; df -i /
docker system df
/opt/cardrag/current/operations/worker-compose.sh run --rm --no-deps worker startup-check
```

`startup-check`는 production 초기화 경로(lock, capacity preflight, OCR resolver, Qwen
provider/tokenizer preflight)만 실행하고 corpus·게시·백업 없이 종료합니다. 실패 시
`Startup stage failed ... reason_code=...`와 최종 JSON payload의 분류를 먼저 확인하고,
무조건 반복 실행하지 않습니다. 개발 잔재 정리는 CardRAG가 생성한 미참조 빌드 캐시·임시
이미지부터 수행하며 `docker system prune -a --volumes`, 전체 image/volume 삭제, 운영
volume 정리는 사용 금지입니다(보존 대상 판정은 각 실행의 rollback/reader/backup 증빙 후 개별 수행).
run 디렉터리 정리 전 read-only로 `SELECT run_id FROM publish WHERE status='ready'
ORDER BY published_at DESC`(retained publication)와 각 run의 `execution-plan.json`
transitive `source_run_id`·백업 pending을 확인합니다. retention 기본값이 보호하는
current/previous publication run과 최신 incomplete run은 공간 회수 목적으로 수동 삭제하지
않습니다. 정리 대상이 되는 명명 컨테이너는 삭제 전에 반드시 `docker logs`와
`docker inspect`를 private evidence로 저장한 뒤 제거합니다(저장 생략 시 stdout 원문이
소실됩니다).

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
| `PDF_CACHE_REFRESH_HOURS` | 2160시간(90일), source별 분산 원본 감사 |
| `ISSUER_EXECUTION_MODE` | `all` (기본값: 전체 8사 실행, `round_robin`: 일일 1사 순회) |
| `ISSUER_REQUEST_BUDGET` | 10000 (카드사/host/run별 일일 최대 요청 수) |
| `ISSUER_BYTE_BUDGET` | 4294967296 (4 GiB, 카드사/host/run별 일일 최대 수신 body bytes) |
| `ISSUER_PROXY_URL` | 빈 값 (직접 연결, 설정 시 신한·롯데 3개 approved HTTPS origin 우회) |

표의 설정명에는 `CARDRAG_` 접두사가 붙습니다.
운영 Worker의 시작 하한은 기본 **2 GiB**(`WORKER_MINIMUM_START_FREE_BYTES=2147483648`)이며,
후보 검증 overlay(`deploy/worker/compose.candidate.yaml`)에서만 격리 및 중복 구동을 위해 32 GiB를 강제합니다.
실제 런타임 디스크 보호는 고정 하한이 아닌 파생 뷰·캐시 miss 기반의
**동적 `peak_growth + 2 GiB reserve` preflight** 및 작업 진행 중 재검사로 이루어집니다.
호스트 여유 공간(`< 80 GB`) 도달 시 수행하는 유지보수 점검은 런타임 거부 preflight 오류와 구분되는
운영자 점검 기준입니다. 용량 부족 시 작업을 거부하며 여유 공간을 자동으로 확보하지 않습니다.
MCP의 지속 quota 정책을 바꾸거나 남은 reservation을 정리할 때는 [RECOVERY](RECOVERY.md)를 따릅니다.

### 초기검증·용량 실패 진단과 제한된 재시도

시작 시점의 2 GiB gate는 **최소 기동 조건(minimum_start_gate_only)**일 뿐이며 배치 전체의
hard gate는 파생 뷰·cache miss가 확정된 직후 embedding 이전의 dynamic
`peak_growth + reserve` preflight입니다. 실패 분류와 diagnostic은 다음과 같이 남습니다.

- 용량 거절 reason code: `capacity_sidecar_limit`, `capacity_database_limit`,
  `capacity_state_quota`, `capacity_free_space`, `capacity_remaining_free_space`,
  `capacity_startup_free_space`, `capacity_wal_bound_exceeded`, `capacity_wal_baseline_unsealed`,
  `capacity_wal_reseal_failed`, `capacity_probe_unavailable`, `capacity_path_invalid`,
  `capacity_state_root_changed`, `capacity_available_unmeasured`, `capacity_policy_lost`,
  `capacity_prediction_contract_invalid`, `capacity_bytes_range_exceeded`.
  실패·성공 모두 `capacity_reason=`, prediction·limit·usage/free/required/deficit과
  `probe_path`가 journal에 기록되고, 최종 JSON payload의 `capacity_details`에도 보존됩니다.
- 계약(quality/route/response) 실패 reason code: `metadata_not_json`, `metadata_shape_invalid`,
  `metadata_model_id_mismatch`, `metadata_no_allowlisted_endpoint`,
  `provider_endpoint_missing|fp8|ambiguous`, `profile_fp8_forbidden`, `profile_endpoint_model_mismatch`,
  `response_*`, `token_counter_invalid`, `token_limit_exceeded`, `repeat_cosine_below_gate` 등.
  원문 exception 텍스트는 남지 않으며 allowlist provider와 수치 필드만 기록됩니다.
- 재시작된 startup attempt는 run 생성 전 `correlation_id`로 journal에 남고, state dir의
  `startup-attempt-history.jsonl`(256 KiB 초과 시 1세대 rotate)에도 제한된 JSONL 이력이
  남습니다. 저장 실패는 journal로만 대체되고 Worker를 실패시키지 않습니다.
- 초기검증 stage 재시도는 `CARDRAG_WORKER_STARTUP_CHECK_MAX_ATTEMPTS`(기본 2, 1~3)와
  `CARDRAG_WORKER_STARTUP_CHECK_DEADLINE_SECONDS`(기본 600)로 bound됩니다.
  이 deadline은 `qwen_endpoint_preflight` stage 진입 시점부터 공유되는 monotonic deadline으로,
  진행 중인 preflight 실행(HTTP 요청 및 내부 request 재시도)과 재시도 대기(기본 30초) 전체를
  포괄하여 제한합니다. 실행 중 deadline을 초과하면 진행 중 HTTP 작업이 즉시 취소·정리되고
  `startup_check_deadline_exceeded` typed reason(`startup_deadline` 범주)으로 실패 처리되며,
  재시도 발생 시 남은 deadline 예산만을 다음 attempt timeout으로 사용합니다.
  재시도 대상은 metadata 재조회로 회복 가능한 typed reason
  (`metadata_not_json`, `metadata_shape_invalid`, `metadata_no_allowlisted_endpoint`,
  `provider_endpoint_missing`)뿐이며, FP8·dimension·norm·cosine·model 등
  결정적 계약 위반과 capacity 거절은 자동 재시도하지 않습니다. systemd unit에는 `Restart=`를
  주지 않습니다.
- 직전 성공 배치의 요구량을 초과하는 여유 공간 감소는 `capacity-observation.json`을 기준으로
  시작 직후 **warning only**로 출력하며 실행을 차단하지 않습니다.

### 반복 임베딩 품질 실패(repeat_cosine_below_gate) 대응

- 품질 gate는 의도적으로 자동 재시도하지 않습니다. 실패 시 실측이 남깁니다:
  `gate_threshold`, `minimum_cosine`, `mean_cosine`, `failed_sample_count`, `sample_count`,
  `first_failed_index`, `lowest_failed_index`, `first_failed_input_sha256`,
  `lowest_failed_input_sha256`, `profile_endpoint_metadata_sha256` — stage 로그, 최종 JSON의
  `quality_details`, `startup-attempt-history.jsonl`의 terminal 이력(`retry_eligible=false`)에
  동일하게 보존됩니다. 품질 거절에서 `status_code`/`attempts`가 null인 것은 HTTP 재시도 계층이
  적용되지 않았다는 뜻이며, 소진이나 미실행과 구별됩니다.
- 운영 대응 순서: (1) writer 없음 확인, (2) production `startup-check` **1회** 재검증
  (24 sample×2회×2 provider = **96개 sample embedding**; 정상 통과 기준 HTTP는 metadata GET 1회 +
  provider·회차별 batch POST 4회이며, transient 발생 시 provider 내부 request retry(최대 12회)로
  실제 요청 수가 늘어납니다. corpus/게시/백업 없음), (3) 통과하면
  "현재 재검증 통과, 과거 수치·원인 미확정"으로 기록 후 배치 진행, (4) 재실패하면
  `quality_details`의 index/sha 분포와 metadata hash를 근거로 provider 변동·route 교체 여부를
  판단하고, 같은 sample 반복 미달 시 가설·추가 1회 한도·시간/호출 예산을 명시한 probe 1회만
  추가합니다. 성공할 때까지 반복하는 방식은 금지입니다.
- 금지: 0.999 기본 threshold 완화, gate 생략, 이전 성공 결과 재사용, deepinfra→nebius 등
  provider/model 자동 전환(캐시/profile/serving query 호환 영향), `Restart=` 주입.
- 미갱신 상태 요약: `systemctl status cardrag-worker.service cardrag-worker.timer`,
  `journalctl -u cardrag-worker.service -n 50 --no-pager`, readiness, MCP current generation,
  `cardrag-worker backup status`(read-only). MCP healthy는 구 세대 서빙 유지의 증거일 뿐
  당일 갱신 성공의 증거가 아닙니다.

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
origin이 HTTP 200 HTML 서비스 오류를 반환하면 PDF로 인정하지 않고
`issuer_origin_service_error`로 분류해 공통 stage retry budget 안에서 재시도합니다.
collection 보고서에는 `origin_status_code`, `origin_mime_type`,
`origin_service_signal`이 제한적으로 남고, credential·cookie·원문 body는 저장하지 않습니다.
로그인·challenge·알 수 없는 HTML과 서명/구조 손상은 transient로 승격하지 않습니다.
정상 control PDF가 성공하고 실패가 특정 source/URL의 공식 등록 오류로 확인된 경우에만
DRM/ZIP과 같은 개별 unsupported/degraded 격리를 적용할 수 있습니다. 같은 endpoint의
여러 source가 함께 실패하는 동안에는 단일 문서 오류로 간주해 pass 처리하지 않습니다.
403/429와 Retry-After가 있는 503은 기존 host/issuer cooldown을 사용하며,
run 결과의 `pdf_succeeded_count`/`pdf_skipped_count`와 progress 로그의
`succeeded`/`skipped`를 구분해 확인합니다. `completed`/`total`에는 격리·미지원으로
건너뛴 문서가 포함될 수 있으므로 전체 다운로드 성공 수로 해석하지 않습니다.
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

## 호스트 전체 디스크 정리와 볼륨 보호 정책

호스트 디스크 공간 부족 시 운영 자산과 dangling 캐시를 명확히 구분하여 정리합니다.

1. **dangling 볼륨 일괄 삭제 금지 (`docker volume prune` 절대 금지)**:
   - Worker 컨테이너가 기동 중이지 않은 상태에서는 Worker state 볼륨(`cardrag-worker-state`, 약 45.9 GB), PaddleOCR 모델 볼륨(`cardrag-worker-paddleocr-models`, 약 2.07 GB), auth 볼륨이 Docker CLI에서 `dangling`으로 표시됩니다.
   - 이는 스케줄된 Worker가 다음 실행 시 사용하는 핵심 영구 운영 자산이며, 삭제 시 전체 데이터베이스 및 캐시 유실이 발생합니다.
   - 다른 프로젝트의 데이터베이스 볼륨(PostgreSQL, MongoDB 등) 및 시스템에 등록된 중지 VM(예: libvirt 등록 HAOS VM `antigravity-haos-vm-*`, 약 21 GB) 역시 꺼져 있다는 이유로 임의 삭제해서는 안 됩니다.

2. **안전한 BuildKit 빌드 캐시 정리**:
   - BuildKit cache는 호스트의 다른 프로젝트와 공유됩니다. 기본은 `docker buildx du`와 `docker builder prune --filter until=168h`처럼 기간·크기를 좁힌 표적 정리이며, 정리 전후 `docker system df`와 실제 파일시스템 여유를 기록합니다.
   - `docker builder prune --all --force`는 다른 프로젝트 cache까지 제거할 수 있으므로 사용자 명시 승인과 전체 영향 확인이 있는 일회성 작업으로만 취급합니다. timer·자동 정리 정책에 넣지 않습니다.
   - 실행 중인 Docker 빌드가 없는 것을 확인한 뒤 수행하고, 컨테이너·볼륨·이미지·MCP 서비스는 중단하지 않습니다.
   - CardRAG 전용 빌드 시 캐시 상한 및 retention 정책을 준수하며, 타 프로젝트 데몬 설정은 건드리지 않습니다.

3. **중복 run 및 publication 정리**:
   - 로컬 상태 디렉터리의 대형 run 정리는 pipeline의 native retention(`retained_generations`, `retained_incomplete_runs`)과 publication closure 검증을 거쳐 안전하게 수행합니다.
   - 검증된 publication seal 및 참조 체인(`execution-plan.json` source run)이 있는 디렉터리는 수동 삭제하지 않습니다.

4. **배포 복구본(`deployment-backups`) 보존·의존성 및 안전 정리 기준**:
   - **용도별 용량 조회 및 진단 명령**:
     - 루트 파일시스템 및 주요 디렉터리 사용량: `df -h /`
     - Worker state 영구 볼륨 세부 분석:
       ```bash
       docker run --rm --network none --read-only --entrypoint /bin/sh \
         -v cardrag-worker-state:/state:ro cardrag-worker:019-e7fb8a0ba60c \
         -c 'du -sk /state/* /state/deployment-backups/* | sort -nr'
       ```
   - **복구 단위 목록 및 외부 디렉터리 의존성 체인**:
     - `deployment-backups/{release_tag}/` 복구본은 배포 롤백을 위해 `worker-state.sqlite3` (~7GB) 및 `backup-ledger.sqlite3` (~25MB)을 보존합니다.
     - **의존성 체인 보호**: 특정 복구점(예: `019-e7fb8a0ba60c`)은 불필요한 7GB 중복 복제를 피하기 위해 직전 복구점(`019-ba273106158e`)의 `worker-state.sqlite3`를 참조하도록 구성될 수 있습니다.
     - **단순 '최신 2개 유지' 식의 일괄 삭제 금지**: 최신 복구점이 이전 디렉터리의 불변 DB를 참조하고 있을 경우, 이전 디렉터리가 삭제되면 최신 복구점의 롤백 능력이 상실됩니다.
   - **불변 DB 복구본 재사용 기준 (다음 배포 시 중복 복제 방지)**:
     - 코드 패치 배포라는 이유만으로 과거 DB를 현재 복구점에 재사용하지 않습니다. timer/service를 비활성으로 유지하고 수동 Worker까지 중지한 뒤, 기존 Worker와 backup lock을 모두 비차단 획득하여 snapshot/전환 동안 유지합니다. 실행 중이면 기다려 끼어들지 말고 중단합니다.
     - SQLite online backup API로 worker DB와 backup ledger의 일관된 snapshot을 같은 정지 구간에 보존합니다. WAL이 있으면 main 파일 복사만으로 대체하지 않습니다. 각 snapshot의 `PRAGMA quick_check`, SHA-256, 크기와 마지막 run, serving generation, lock/시점을 함께 기록합니다.
     - 같은 정지 구간의 현재 snapshot과 기존 검증된 복구본의 **내용 동일성**을 입증한 경우에만 이전 파일을 참조합니다. 크기·mtime·같은 Git HEAD는 동일성 증명이 아닙니다. 동일성이 불명하거나 배치로 DB가 변했으면 새 snapshot을 보호합니다. backup ledger도 별도로 일관성을 확인하며 임의 시점의 두 DB를 현재 복구점으로 표시하지 않습니다.
     - 현재 snapshot을 비교하기 위한 임시 사본의 공간도 필요합니다. 약 7GB 절약은 동일 내용일 때만 적용하는 최적화이며, 최신 상태의 복구 가능성보다 우선하지 않습니다.
   - **정리 후보의 안전한 Dry-run 및 복구 보호 집합(Protected Set)**:
     - **단일 최신 복구본 유지 정책 (1-Backup Retention Policy)**:
       - 사용자의 명시적 지침("복구용으로 유지할것은 1개면 충분하다")에 따라, `/state/deployment-backups/`에는 현재 활성 배포 기준의 단일 독립 복구본 1개만 유지합니다.
       - 유지 대상은 활성 release의 `operations/deployment.json`에 기록된 독립 `snapshot_directory`입니다. 새 복구본의 두 DB 무결성·SHA와 독립성을 검증한 뒤에만 이전 복구본을 정리합니다.
       - 현재 사용 가능한 복구점은 각 release의 `operations/recovery-point.json`으로 안내합니다. `deployment.json.snapshot_directory`는 배포 당시 역사값이며, `historical_snapshot_status=pruned`인 경로를 현재 복구점으로 사용하지 않습니다. active/rollback 양쪽에서 동일한 guidance와 `verify-recovery-point.py`로 존재·SHA·quick_check를 확인합니다.
       - 코드/image rollback과 DB snapshot restore는 별개입니다. snapshot DB를 rollback image에 복원하는 것은 schema/contract 호환성 검증과 사용자 승인 전 수행하지 않습니다.
       - 이전 복구본(`019-ba273106158e`, `019-e7fb8a0ba60c` 등)은 정리 완료되어 약 6.84 GiB가 추가 회수되었습니다.
     - **보호 집합**:
       1. 현재 활성 릴리스(`active release`) 및 위 metadata가 가리키는 단일 독립 복구본
       2. 활성 Serving 및 MCP 세대
     - 과거 누적된 다중 복구본은 위 단일 복구본 정책에 따라 정리하며, `docker volume prune` 및 전역 `docker system prune`은 여전히 절대 금지합니다.

## Qwen 임베딩 재현성 게이트 정책 (일일 운영 smoke와 자격평가 분리)

1. **일일 운영 smoke (Daily Smoke)**:
   - 일일 배치 시작 시 설정된 활성 provider(`CARDRAG_EMBEDDING_PROVIDER_ID`)만 단독 검사합니다.
   - 단일 provider 검사 시 상호 비교는 미수행 상태이므로 `minimum_cross_provider_cosine=not_evaluated`(JSON payload는 `null`)로 명시하여 허위 1.0 표기를 방지합니다.
   - 단일 scalar 0.999는 **재현성 경고(warning) 기준**으로 운용합니다.
   - **경고 및 하드 거부 기준 (운영 정책 후보 / 미인수 uncalibrated candidate)**:
     - 검사 결과 cosine이 [0.990, 0.999) 구간일 경우:
       - `quality_status="warning"`으로 기록하고, 경고 로그와 함께 `startup-attempt-history.jsonl` 및 CLI ready JSON에 구조화된 `quality_details`(위반 샘플 인덱스, SHA256 해시, 임계값)를 영속 보존하되 Worker 시작을 차단하지 않습니다.
     - cosine이 0.990 미만이거나, 비정상 non-finite, 차원 불일치(4096), norm 오차 초과 시 `repeat_cosine_below_gate`로 fail-closed 차단합니다.
     - **주의**: 0.990 하한과 0.980 의미 축퇴 임계치는 운영 관측 및 upstream 비결정성(FP16/BF16 drift) 방어를 위한 후보 정책(미인수)이며, 업무 gold 셋에 대한 nDCG@10 / Recall@10 / anchor calibration 실측 전까지 정규 품질 인수로 확정하지 않습니다. 2026-10-16 사용자가 운영 테스트를 통한 평가를 명시 승인하여 신규 정책을 운영에 적용합니다. 이는 calibration 완료를 뜻하지 않으며, 일배치의 품질 상태·repeat cosine·실패 사유와 실제 검색 결과를 누적 평가합니다. 반복 오류나 검색 품질 저하가 확인되면 보존된 `016-bb56d20` 이미지로 복귀합니다.
   - **의미 축퇴(Semantic Collapse) 검증**:
     - 정적 더미 벡터 반환으로 인한 거짓 repeat=1.0 통과를 방지하기 위해 CLI production 시작 경로에서 이종 의미 샘플 간 유사도가 비정상 축퇴(>= 0.980)될 경우 `embedding_semantic_collapsed`로 즉시 fail-closed 거부합니다.

2. **배포/제공자 자격평가 (Provider Qualification)**:
   - 프로바이더 교체, 신규 엔드포인트 도입 시 모든 지원 프로바이더(DeepInfra, Nebius)를 대상으로 24개 전 샘플 2회 호출 및 상호 프로바이더 간 일치도를 종합 평가(`mode="qualification"`, repeat cosine >= 0.999 강제)합니다.
   - 일일 운영 배치 중 임의 프로바이더 자동 전환(failover)은 수행하지 않습니다.

## 롯데카드 공시실 장애 대응 및 Degraded 수집

1. **연결 장애 이력과 검증 범위**:
   - 과거에는 공식 landing·목록 POST 직접 연결에서 timeout이 발생했습니다. 별도 구성된 카드사별 프록시 경유로 목록 HTTP 200과 정상 PDF 단건 취득을 확인했습니다. 이를 신규 이미지의 운영 배치 성공으로 확대하지 않습니다.
   - 실제 연결 장애가 발생하면 기존 issuer-level cooldown/carry를 적용하며, 불완전한 목록을 단종·삭제로 해석하지 않습니다.

2. **ZIP 첨부는 문서 단위 unsupported 오류**:
   - 현재 017은 ZIP을 열거나 내부 PDF를 선택·OCR·게시하지 않습니다. 특정 DOCID·파일명 예외 없이 카드사 공통 정책을 적용합니다.
   - 목록에 명시된 ZIP은 일반 배치에서 본문을 다운로드하지 않고 해당 source를 오류로 기록하며 다른 정상 상품을 처리합니다. ZIP 한 건 때문에 카드사 전체를 실패 처리하지 않습니다.
   - 기존 검증된 상품문서가 있으면 보존합니다. 신규 source에 이전 자료가 없으면 미수집으로 표시하고 정상 PDF를 취득한 것으로 기록하지 않습니다. 관측된 ZIP 상품을 단종·삭제 후보로 만들지 않습니다.
   - `unsupported_attachment_count`와 `collection_status=degraded`를 확인합니다. 정상 문서 게시 성공과 모든 첨부의 정상 취득을 구분하며, ZIP 오류는 seal과 재개 결과에도 유지합니다.
   - 첨부 metadata가 정상 PDF로 바뀌면 재취득하고, 같은 URL의 내용만 바뀐 경우도 만료되는 원본 재확인으로 탐지합니다. 원본이 ZIP이면 오류를 유지하며 압축을 해제하지 않습니다. 확인하지 않은 원본을 fresh로 기록하지 않습니다.
   - 2026-10-19 확인 사례는 toss place 카드 DOCID 1893의 ZIP 한 건입니다. 잘못 업로드됐다는 해석은 추정이며 이 고유값을 처리 조건으로 사용하지 않습니다.

ZIP 해제 기능은 운영 재개의 선행 조건이 아닙니다. 신규 이미지의 운영 인수는 카드사별 실제 실행·자료 보존·오류 가시화·백업을 따로 검증해 판정합니다.


## 017 카드사 선택·순회·증분 수집

기본 `run`은 기존 configured pool 전체를 처리합니다. `CARDRAG_ENABLED_ISSUERS`는
허용 pool이며 실행 선택은 별도입니다. 새 설정 `CARDRAG_ISSUER_EXECUTION_MODE`는
빈 값/`all`이 기본이고 `round_robin`으로 일일 한 카드사 배정을 켭니다. 배포 시 기존
private env의 `CARDRAG_PDF_CACHE_REFRESH_HOURS=168`을 유지하면 7일 정책이 우선하므로,
90일 정책을 사용할 때는 비공개 설정도 2160으로 이행해야 합니다.

```bash
uv run --package cardrag-worker cardrag-worker run --issuer shinhan
uv run --package cardrag-worker cardrag-worker run --issuer shinhan --issuer lotte
uv run --package cardrag-worker cardrag-worker run --round-robin
uv run --package cardrag-worker cardrag-worker run --all
uv run --package cardrag-worker cardrag-worker run --issuer shinhan --phase collect
uv run --package cardrag-worker cardrag-worker run --phase process --reuse-from-run COLLECTION_RUN_ID
```

명시 선택/순회/전체 옵션은 서로 배타적입니다. 선택은 목록·PDF뿐 아니라 OCR·구조·뷰·벡터
생성에 적용합니다. 비선택 자료는 검증된 현재 serving의 행·산출물을 보존합니다. 이전
산출물이나 embedding cache가 없거나 처리 계약이 맞지 않으면 게시를 중단합니다.
전체 기준선 없는 부분 설치에서 선택 게시를 시작하지 마십시오. `reports/issuer-collection.json`
의 `not_scheduled`는 장애가 아니며, 기존 원본 확인일·수집 성공일·단종 관측을 갱신하지 않습니다.

개발·운영 인수에서 8사를 같은 날 검증하려면 위 `run --issuer CODE`를 카드사별로 한 번씩
순차 실행합니다. 순서는 `woori → kb → shinhan → samsung → hyundai → hana → lotte → bc`입니다.
같은 날 `--round-robin`을 여덟 번 호출해도 여덟 카드사로 진행하지 않습니다. 명시 수동 실행은
자동 순회 cursor를 소비하지 않으므로 이후 예약 순회는 기존 slot/cursor부터 시작합니다.
수동 실행 전 자동 timer를 `systemctl disable --now cardrag-worker.timer`로 영속 중지하고
마지막 Worker 종료 후 사용자 승인 아래 예약을 복구합니다. `Persistent=false` timer는 중지 중
놓친 예약을 복구 직후 자동 실행하지 않습니다. 누락 배치가 필요하면 같은 검증 release의
수동 `run` 경로로 명시 실행합니다.

순회는 기존 worker lock 안에서 KST 실제 날짜·channel별 한 slot을 SQLite에 저장합니다.
cursor는 배정 시 한 번 전진하며 실패해도 다음 날짜에는 다음 카드사를 배정합니다.
같은 날 자동 순회는 같은 카드사로 최대3회 실행할 수 있습니다. 완료/실패 뒤2·3회차는 새 run을
만들고 이전 결과는 `issuer_rotation_attempt`에 보존합니다.4번째는 `rotation_daily_attempt_limit`으로
차단하며 provider startup 전에 종료합니다. 다음날에만 다음 카드사로 전진합니다. 중단된 미완료
run의 재개는 같은 시도이며, 명시적인 수동 `--issuer` 실행은 이 자동 순회 회수를 소비하지 않습니다.
기존 단일 day 기록은 실제 배치1회로 이행합니다. 오늘12시처럼 저장 결과만 반환한 과거 호출은
추가 실배치로 세지 않습니다. 일시 오류가 지속되면 원인을 확인하고 제한 안에서 재실행합니다.
cursor/day/result 행을 SQL로 지우거나 성공으로 수정하지 않습니다. 미완료 작업은 같은 run과 HTTP 예산을
사용합니다. 게시 seal이 있으면 provider 없이 게시 증거를 대조하거나 같은 날 게시만
재개합니다. 최신 수동 게시를 덮는 전환은 기존 predecessor fence가 거부합니다.
전날 미완료 작업의 origin/OCR은 자동 재개하지 않습니다. 수동 실행/수집/처리는 slot을
소비하지 않으며 missed 날짜 수만큼 몰아서 배정하지 않습니다. pool 추가는 다음 cycle,
제거는 남은 배정에서 제외하고 migration 이력을 남기며 serving 데이터는 보존합니다.

`--phase collect`는 provider 초기화 없이 `runs/<id>/acquisition.json`을 남깁니다.
`--phase process`는 해당 receipt의 고정된 카드사 범위를 사용하고 원본 HTTP를 하지 않습니다.
수집 이후 serving 기준선이 바뀌면 충돌로 중단합니다. 이때 새 수집으로 기준선을 다시
고정해야 합니다. 초기 버전은 receipt 일부 카드사만 따로 처리하거나 phase와 기존
stage skip을 조합하지 않습니다. 순회에는 phase=all만 허용합니다. 기존 stage skip/reuse는
별도 전체 실행 경로로 유지합니다. 미처리 receipt와 참조 PDF는 retention/prune에서
보호하므로 공간 확보 목적으로 수동 삭제하지 마십시오.

정상 캐시·동일 내용 metadata·감사 기한 전에는 PDF prepare/GET/HEAD를 생략합니다.
상품명만 바뀌고 첨부 identity의 나머지 필드가 모두 같으면 원본 확인 시각을 유지해
재사용합니다. 신규/다른 URL·버전/손상은 strict PDF 검증을 거칩니다. 기존 캐시 감사일은
초기 이행 시 분산되며 이미 90일을 넘긴 자료는 overdue로 드러낸 채 한 번의 이행 창을
사용합니다. 일반 재확인 기한은 실제 원본 확인에서 81–90일이고 순회에서는 다음 방문
전에 만료될 대상을 최대 pool 방문 간격만큼 앞당깁니다. 목록/로컬 검사는 원본 확인으로
계산하지 않습니다. 알려진 DRM·정확히 확인한 롯데 404는 bounded negative cache를
사용하고 알려지지 않은 DRM은 1일 후 재확인합니다.

우리·현대는 매번 전체 상위 목록을 확인하되 저장 상세를 합성해 complete inventory를
만듭니다. 신규/상위 metadata 변경은 즉시 상세 조회하며 기존 상세는 상품별 15–30일
순환 확인합니다. 상위 목록에 표시되지 않은 개정 탐지는 그만큼 늦을 수 있습니다.
우리 목록이 고정 요청 한도 1,000건에 도달하면 성공으로 자르지 않고 중단합니다.
다른 카드사의 경량 전체 목록 조회는 유지합니다. 신한의 `+` 포함 이름 검색이 정확히
빈 결과일 때만 짧은 literal prefix로 재검색하고 최종 선택은 전체 stable identity를
검사합니다. 세션 token 전역 공유/영구 캐시는 도입하지 않습니다.

`CARDRAG_ISSUER_REQUEST_BUDGET` 기본 10000회와 `CARDRAG_ISSUER_BYTE_BUDGET` 기본
4294967296 bytes는 issuer/host/run 누적 상한입니다. 초기 실측용 보수적 상한이며
운영 계측 후 낮추십시오. 예산 소진은 deferred 원인으로 격리·이전 데이터 보존합니다.
403/429·Retry-After와 연결 장애 cooldown은 run 간 유지합니다. 원본 HTTP 지표는
`reports/performance.json`의 `issuer_http`, 감사 기한은 `origin_audit`에 기록합니다.
body bytes는 앱이 읽은 response stream 기준이며 TCP overhead/읽기 전 버퍼를 포함하는
패킷 계측값이 아닙니다. 선택 실행의 증분 백업은 선택 artifact만 등록·flush하며 관련 없는
전역 OCR cache/pending을 자동 소진하지 않습니다.

### 선택 실행 백업 완결 semantics (019 FIX_06/07)

- 선택 문서의 OCR 본문·manifest·READY는 하나의 검증 가능한 backup 단위입니다. 허용 경로는
  run 문서의 `native-manifest.json`이 sealed PDF/OCR identity(pdf sha/size/page, ocr sha/size,
  body hash)와 암호학적으로 연결될 때만 본문·`manifest.json`·`READY.json`을 포함합니다.
  seal의 optional `ocr_reuse_key`가 없는 generation-only 결과도 같은 linkage로 백업됩니다.
  prefix·파일명·doc_map 문자열 하나만으로 귀속을 판단하지 않습니다.
- flush 결과의 `in_scope_remaining`은 선택 run 고유의 남은 artifact(미전송 pending와
  in-scope `lost_source` 포함), `pending_out_of_scope`는 범위 밖 전역 backlog,
  `in_scope_conflicts`는 검증 번들과 SHA/크기가 어긋난 경로·receipt·local bytes 충돌이다.
  선택 완료는 `backup_scope_complete=true`(전송·원격 검증·index commit 후 판정)로만 확인하고,
  `in_scope_remaining=0`이나 전역 status 문자열 하나만으로 완료를 판정하지 않는다. 전역 상태
  문자열과 `remaining_pending` 의미는 하위 호환을 유지하며, 비선택 backlog가 남으면 선택
  범위가 완료돼도 전역 status는 `degraded`일 수 있다. 미선택 flush는 이 필드들이 `null`이다.
- 같은 경로의 기존 pending/receipt가 검증된 번들과 다르면 전송 대상이 아니라 충돌이다.
  등록 시 검증 근거로 ledger 행은 정식 수복되지만, receipt·원격 내용은 덮어쓰지 않고
  차단·보고한다(운영자 검토 필요). 선택 허용 경로는 항상 검증 근거(run-local manifest 또는
  로컬 cache 디렉터리 + seal 대조)에 바인딩되며, 근거가 없는 경로는 무바인딩 상태로
  `in_scope_unbound`로 보류되어 완료를 막는다(경로를 조용히 제외한 뒤 완료로 처리하지 않는다).
- `backup_scope_complete`는 선택 필수 artifact 전체가 canonical root에서 일치하는
  receipt + 커밋된 원격 `v1/backup/index.json` 항목으로 확인할 때만 true다. pending이
  없는 receipt SHA/size 충돌과 필수 항목 누락(`in_scope_missing`)도 완료 판정을 막는다.
  다른 root의 receipt은 현 root 완료 근거가 아니다.
- backup index는 corpus 증가에 맞춘 **전용 유한 한도(현재 64MiB)**로 읽는다. 공통
  control object 기본 8MiB 한도는 올리지 않으며 무제한 읽기도 허용하지 않는다. 부재(404)와
  한도 초과/인증/연결/JSON 손상을 구분해 `index_read_error`/`backup_index_read_error`로
  보고하고, 읽을 수 없는 index를 완료 근거로 쓰지 않는다(완료 `null`). commit merge가
  기존 index를 읽지 못하면 빈 index로 간주해 local receipt만 재게시하지 않고 **게시를
  중단**하여 원격 전용 항목/다른 세대 항목을 보존한다. 부재는 명시적 HTTP404/None만
  의미하며, HTTP200 zero-byte body는 손상된 index로 취급해 새 index로 덮어쓰지 않는다.
- receipt 없는 새 호스트의 `backup restore`가 index를 읽지 못하면(403/인증/연결/한도/손상)
  `status=failed`와 `index_read_error`를 반환하고 CLI는 비정상 종료(1)한다. 정상 empty는
  HTTP404 또는 유효한 `items=[]`뿐이며, 실패를 empty로 숨기지 않는다.
- 선택 run만 정식 재시도: `cardrag-worker backup flush --run <RUN_ID> [--issuer <code>...]`.
  --run 지정 시 force되며, 그 run의 고정 seal과 run-local manifest로 검증된 artifact만
  allowed+expected로 재시도한다(provider·OCR·수집 재실행 없음). image entrypoint가 이미
  `cardrag-worker`이므로 compose 일회성 실행에서는 CLI 이름을 반복하지 않는다:
  `operations/worker-compose.sh run --rm --no-deps worker backup flush --run <RUN_ID> --issuer <code>`.
  run 산출물·spool이 retention으로 정리됐으면 그 경로는 full-batch flush 또는 재수집으로
  회수한다. 전역 수치는 맞기기 위해 pending을 일괄 전송·삭제하지 않는다.
- native metadata pair 원격 검증 회수(019 FIX_18): 로컬 cache 근거가 사라졌지만 현 root의
  원격 canonical manifest/READY와 receipt가 남아 있으면, GET-only 정식 CLI로 pair를 검증해
  로컬 cache 증거를 복원한다:
  `operations/worker-compose.sh run --rm --no-deps worker backup recover-metadata --run <RUN_ID> --issuer <code> [--key <64-hex>...]`.
  manifest/READY canonical bytes, root-bound receipt SHA/size, 현재 seal의 PDF/OCR identity가
  모두 일치할 때만 복원하며 receipt·seal·pending·원격 객체를 수정하지 않고 이미 일치하는
  본문을 재업로드하지 않는다. 후보는 manifest/READY/receipt/현재 identity/scope 라우팅을
  묶은 **전체 pair 단위**로 평가하므로 불완전한 variant가 정상 canonical 복구를 막지 않는다.
  쓰기는 cache root부터 O_NOFOLLOW로 경계를 검사하고 no-replace 원자 게시·최종 바이트
  재검증을 사용하며, 동시 cache 쓰기는 `ocr-cache-recovery.lock`으로 직렬화한다. symlink/
  기존 상이 바이트/쓰기 실패는 덮어쓰지 않고 오류로 보고한다. pair가 없거나 손상/불일치면
  unbound로 남아 완료를 막는다.
- 검증된 run 소유 native bundle의 필수 metadata 경로는 원장의 run별 ownership 기록으로도 보존합니다. 이 기록은 업로드 권한이나 SHA/size 검증 근거가 아니라 누락 가시성만 제공합니다. pending/lost_source/receipt-only/대기 행 소실에서도 독립 검증 근거가 없으면 required/unbound로 남습니다. 과거 원장에 ownership 기록이 없을 때는 기존 native seal과 pending/lost_source를 보수적으로 보존하며, content/adopted의 과거 shared-cache 태그를 현재 소유 증거로 추정하지 않습니다. 정상 제외 경로 수는 `backup_excluded_shared_cache`로 표시하고 현재 PDF/OCR CAS는 제외 수에 세지 않습니다. 과거 revision의 OCR CAS(`v1/objects/...`)는 현재 seal/다른 현재 문서/검증된 bundle·ownership이 요구하지 않으면 필수 scope에서 제외하며, receipt·doc_map·원격 객체는 이력으로 보존합니다.
- 필수 artifact 판정은 경로 단위·pending 독립이다. 선택 문서의 native reuse-key 경로에
  검증 가능한 로컬 근거(run manifest 또는 로컬 cache 재바인딩)가 없으면 행/receipt 유무와
  관계없이 `in_scope_unbound`(경로 수)에 남고 완료를 금지한다. receipt만 있거나 행이 전부
  없는 경우도 같다. 중복 행이 판정을 바꾸지 않는다. adopted/content cache 정체성은
  공용 cache 산물이므로 선택 백업이 주장·전송하지 않고 필수 카운트에도 포함하지 않는다.
- run payload(`run`/`--phase process` 결과 JSON)에도 위 구분(`backup_in_scope_remaining`,
  `backup_in_scope_conflicts`, `backup_in_scope_missing`, `backup_in_scope_unbound`,
  `backup_pending_out_of_scope(_bytes)`, `backup_scope_complete`, `backup_defer_reasons` 등)이
  포함된다. hybrid/manual 임계값 미달로 전송하지 않는 정상 보류에서도 force flush 없이
  읽기 전용 스코프 진단(`backup_diagnostics_mode: read_only`, 완료 여부는 `null`)이 채워진다.
- 현재 상태 확인: `cardrag-worker backup status`(전역) 및
  `cardrag-worker backup status --run <RUN_ID> [--issuer <code>]`(읽기 전용 선택 스코프;
  네트워크·원격 호출 없음). run 종료 시점의 cached 결과(JSON·rotation result)는 snapshot임을 명확히 하고, 이후 백업
  재시도 확인은 `cardrag-worker backup status`로 원장을 별도로 읽는다.
- 호스트 operations 도구는 release 준비 때마다 다음 수정이 유지되는지 확인합니다:
  `run-issuer.sh` 등 helper의 물리 경로 해석(`pwd -P`, current symlink 경유 비교 금지)과
  group 실행 bit 상속(mode 2750/group cardrag), status/summary의 `backup_status`·pending 표시.
  비공개 overlay·proxy/auth 값은 저장소에 넣지 않습니다.
- 수동 `--issuer` 실행의 frozen scope는 이제 실제 mode(`selected`)로 기록되며 rotation
  reclaim은 `round_robin`을 보존합니다. 과거에 mode=all로 고정된 run의 resume은 기존 고정
  값을 복원해 차단하지 않습니다. 수동 실행이 날짜 cursor를 소비하지 않는 계약은 불변입니다.

## 017 선택 프록시

`CARDRAG_ISSUER_PROXY_URL`이 비어 있으면 직접 연결합니다. 설정하면 HTTPS/443의 정확한
`www.shinhancard.com`, `www.lottecard.co.kr`, `image.lottecard.co.kr`만 HTTP 프록시를
사용합니다. 모바일 롯데 host, 다른 port/카드사/OCR/embedding/백업은 대상이 아닙니다.
프록시 장애 시 직접 연결로 fallback하지 않으며 ambient HTTP(S)_PROXY도 사용하지 않습니다.
기존 현대 전용 TLS 예외와 downloader의 origin/DNS/TLS/redirect 검증은 유지합니다.
실제 endpoint는 호스트/컨테이너 접근성을 구분하여 서버 비공개 env에만 두십시오.
저장소 예시는 빈 값이며 SSH 키·VPN·터널 인자는 넣지 않습니다. 페이지 HTTP 200은
전체 목록 schema·pagination·PDF 검증 성공을 뜻하지 않습니다.
