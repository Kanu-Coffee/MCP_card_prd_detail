# FIX 01 — 007 운영 전환 인계: content OCR 이전 및 첫 배치 검증

작성 역할: Reviewer. 2026-10-07 KST. 후임 Executor를 위한 지시. 사용자가 PLAN §6 단계 6 진행을 승인했으나, 장시간 작업을 현재 Codex가 직접 수행하지 않고 별도 에이전트에 맡기도록 지시했다. **이 문서를 작성하는 세션은 운영 전환을 실행하지 않는다.** 후임은 이 FIX만 보고도 작업 범위와 중단 기준을 알 수 있어야 한다.

## 1. 목표와 권한

007의 남은 **단계 6**을 실행한다. 새 MCP를 먼저 배포하고, 기존 OCR 산출물을 운영 WebDAV에 추가형으로 이전하고, 새 Worker의 첫 정상 배치에서 기존 문서의 OCR이 재호출되지 않으며 서비스 중 텍스트가 유지되는지 확인한다. 마친 뒤 `FIX_01_REPORT.md`에 실제 명령, 결과, 전후 지표, 실패/롤백 여부를 기록한다. 사용자 승인된 범위 안의 단계 6 실행에 다시 승인을 요구하지 않는다.

**범위 밖:** PLAN 단계 7의 006 OpenCode 공급자 운영 활성화, OCR 전체 재처리/Paddle 일괄 실행, 전체 재임베딩을 검증 목적으로 강행, 원격 GC 활성화, 과거 OCR·CAS·볼륨 삭제, Docker Hub/GitHub 정식 릴리스, PR 병합. `ocr-cache reprocess --apply`, `restore --apply`, `--all`, `--adopt-latest`도 이 FIX에서 실행하지 않는다. 별도 지시 없이 범위를 넓히지 않는다.

## 2. 읽어야 할 현재 근거와 구현 편차

1. `.handoff/007_content-addressed-ocr-cache/PLAN.md` 전체, 특히 §6·§7과 **개정 부록 A가 본문보다 우선**한다. `REPORT.md`의 마지막 종료 기록과 이 FIX를 읽는다. `.handoff/006_opencode-ocr-provider/`는 006 통합 사실 확인용으로만 읽는다.
2. Git 및 현재 운영 상태를 다시 확인한다. 인계 시점 코드: `feat/007-content-ocr-cache` `159c71a30cd663ac7cd445f14f0d76cd1fc710bb`, Draft PR #40, CI run `37563250492` success. 후임 작업 시작 때 head가 달라졌다면 diff와 CI를 다시 검토한다. 로컬 후보 이미지 태그는 `ccae2c3`에서 빌드된 것으로 **최종 head 배포 이미지라고 가정하지 않는다**.
3. 구현은 PLAN의 SQLite content 인덱스와 레거시 역조회 대신 **원격 평면 index 1회 열거, run JSON snapshot, 메모리 key index, 문서별 선택 고정**을 사용한다. 전량 마이그레이션과 stable 문서 매핑 검증이 전환의 안전장치다. 이 차이만을 이유로 운영 인수를 막거나 구조를 재작성하지 않는다. 실제 기능 기준인 무재OCR·OCR SHA 보존·재개 결정성·롤백으로 판정한다.
4. 2026-10-07 dry-run 기준 stable `g-03fbc4f18a3c450bb017e2fd-36bae25dd8cd`, 레거시 1,818개 + generation 전용 3,354개 = 5,172 variant, stable OCR 문서 5,512개 전량 매핑, 같은 PDF의 다른 OCR이 있는 key 87개. 이 수치는 **참고 기준**이며 후임은 최신 상태를 다시 측정한다. 5,512개 중 같은 PDF의 서로 다른 OCR을 쓰는 문서가 있으므로 **문서별 기존 OCR SHA** 보존을 비교해야 한다.
5. 운영 `/opt/cardrag/current`는 인계 시 `/opt/cardrag/v1.0.29`. Worker는 `/etc/systemd/system/cardrag-worker.service`와 `cardrag-worker.timer`가 실행하고, Compose 파일은 현재 운영 디렉터리의 `deploy/worker/compose.yaml`, `compose.secrets.yaml`, 환경은 `/etc/cardrag/worker.env`다. MCP는 systemd 서비스가 아니라 Docker Compose 프로젝트 `cardrag-stable-v1026`의 `mcp` 컨테이너가 healthy로 운영 중이었다. MCP 환경 `/etc/cardrag/mcp.env` 및 실제 Compose project/config/volume은 `docker inspect`와 `docker compose config`로 재확인한다. 자격정보 내용은 출력하거나 보고서에 복사하지 않는다.
6. 인계 시 Worker timer는 active, 마지막 Worker service는 **007 적용 전** 2026-10-07 03:00 run의 신한카드 `discover_current` 중 `httpx.ReadError`로 failed였다. 호스트에서 신한 도메인 GET도 TLS 뒤 reset됐다. 외부 수집 장애와 007 자체 결함을 분리한다. 수집 오류를 숨기려고 issuer를 빼거나 성공을 조작하지 않는다.

## 3. 실행 순서와 각 게이트

### A. 재실사와 안전한 배포 준비 — 읽기 전용

1. `git status`, head, PR CI, 코드·Compose·운영 service 설정을 확인한다. `/opt/cardrag/current`의 실제 경로, 실행 중 MCP 컨테이너 이미지 다이제스트·Compose project·volume, Worker timer/service, stable pointer, 디스크 여유, WebDAV 자격 파일 존재 여부를 **값을 노출하지 않고** 기록한다. 기존 `CARDRAG_REMOTE_GC_APPROVED=false`와 `CARDRAG_COLLECT_REMOTE_GARBAGE=false`를 확인하고 전환 내내 유지한다. 실제 Worker 실행에 쓰이는 OCR provider/overlay와 publication 승인 플래그도 확인하되 비밀값은 출력하지 않는다.
2. 007 head를 별도 운영 배포 디렉터리에 준비한다. 기존 `/opt/cardrag/v1.0.29`와 현재 MCP 이미지, 기존 env/Compose 설정은 **롤백 근거 1세트**로 유지한다. 현재 운영 volume 이름과 Compose project를 그대로 사용한다. 버전 디렉터리/현재 symlink를 바꿀 때 systemd의 `WorkingDirectory=/opt/cardrag/current`가 새 파일을 참조한다는 점을 고려한다. 기존 state volume을 새 이름의 빈 volume으로 바꾸지 않는다.
3. 정확한 대상 코드 커밋으로 MCP·Worker 이미지를 빌드 또는 식별하고 `org.opencontainers.image.revision`과 이미지 다이제스트를 기록한다. `docker compose ... config --quiet`로 **실제 운영과 동일한 env, secrets overlay, project, volume**의 유효성을 확인한다. 빌드 공간을 재측정하고 부족하면 본 과제의 이미지 빌드 캐시처럼 식별된 임시물만 정리한다. 운영 volume이나 WebDAV OCR 자료는 정리 대상이 아니다.
4. 새 Worker 이미지에서 `ocr-cache migrate --dry-run`을 실제 운영 WebDAV에 **읽기 전용**으로 실행한다. Compose의 secrets 및 `worker-state` volume을 같은 방식으로 연결해야 다음 `--apply`가 `worker.lock`을 공유한다. 예시 형태는 `docker compose --env-file /etc/cardrag/worker.env -f deploy/worker/compose.yaml -f deploy/worker/compose.secrets.yaml [기존 운영 overlay] run --rm worker ocr-cache migrate --dry-run`이며, 실제 `WorkingDirectory`·project·image 선택은 먼저 `config`로 검증한다. stable ID, 유효/제외 수, 예상 variant 수, stable 매핑 수, 충돌 PDF 수를 기록한다. 환경·자격정보를 셸 출력에 덤프하지 않는다.
5. **중단 기준:** 레거시 또는 stable CAS 검증 실패, stable OCR 문서 중 매핑 누락, 의도하지 않은 provider/overlay 변경, 볼륨/프로젝트 불일치, 필요한 디스크 여유 부족, 새 이미지와 CI 코드 불일치. 원인을 좁혀 해결한 뒤 dry-run을 반복한다. 5,172와 수치가 다르다는 사실만으로 실패 처리하지 않고 stable 변동·새 OCR 등 실제 차이를 설명한다.

### B. 짧은 운영 전환 창 — 쓰기 작업

1. 작업 직전 timer의 원래 활성 상태와 다음 예약 시간을 기록하고, timer를 임시 중지한다. Worker 프로세스·Compose one-off 컨테이너가 없는지 확인한다. 이전 실패 상태는 `reset-failed`로 정리할 수 있으나 실패 로그를 지우지 않는다. 예상치 못한 실행을 막기 위해 단계 B/C 동안 운영 run을 겹치지 않게 한다.
2. **MCP 먼저:** 기존 Compose project/volume/설정을 유지하면서 새 MCP 이미지로 교체한다. 기존 stable generation이 계속 로드되고 `/health/ready`가 200/healthy이며 기존 질의가 정상인지 확인한다. MCP가 준비되지 않으면 Worker 전환으로 가지 않고 기존 MCP 이미지로 되돌린다.
3. 새 Worker 이미지의 같은 설정으로 `ocr-cache migrate --dry-run`을 전환 직전에 다시 실행한다. 출력의 `stable_generation_id`를 그대로 `--confirm-stable-generation`에 사용해 `ocr-cache migrate --apply --confirm-stable-generation <방금 확인한 stable ID>`를 실행한다. 명령은 `worker.lock`을 잡아야 하며, 끝까지 **추가형·멱등**이어야 한다. 중간 장애로 재실행할 때도 dry-run과 현재 stable ID를 다시 확인한다. 기존 OCR CAS·레거시 항목·generation은 삭제하지 않는다.
4. `ocr-cache verify`를 같은 새 Worker 이미지와 운영 WebDAV 설정으로 실행한다. `verified_variants`가 계획 대상 전부를 포함하고, `stable_ocr_documents_covered`가 현재 stable의 OCR 문서 수와 정확히 같으며 오류가 0이어야 한다. 이전 기준 5,172/5,512는 참고값이다. 현재 stable ID가 사전확인과 다른지도 확인한다. 실패하면 새 Worker run을 시작하지 않는다. 새 content 기록은 그대로 두고 원인을 해결하거나 이미지/설정을 롤백한다.
5. MCP 교체와 이전 단계가 성공한 뒤에만 Worker 이미지를 새 것으로 고정한다. `CARDRAG_OCR_PROVIDER`와 006 overlay는 기존 운영값으로 유지한다. OCR provider 호출을 막으려고 production cache mode를 무리하게 바꾸지 않는다. 기존 문서에 대한 무재호출은 마이그레이션·pin이 충족해야 한다.

### C. 첫 정상 run과 인수

1. 첫 run 이전에 신한카드 수집 경로가 회복됐는지 가벼운 읽기 요청과 기존 장애 로그로 판단한다. 회복됐으면 timer와 겹치지 않는 시각에 **한 번만** 새 Worker의 정상 run을 수행한다. 03:00 예약을 이틀 기다릴 필요는 없다. run은 약 2.6시간 걸릴 수 있으므로 세션 종료 때문에 중복 기동하지 않는다. 이미 실행 중이면 로그·상태만 관찰한다.
2. 첫 run 결과에서 기존 문서와 **새로 발견한 PDF**를 분리한다. 기존 동일 PDF 문서: OCR provider 호출 0, 각 문서의 이전 stable OCR SHA/크기 불변, 누락 없이 새 generation이 게시되고 MCP가 이를 읽어야 한다. 새 PDF가 있다면 그 문서의 OCR 호출만 허용하고 건수를 따로 보고한다. OCR 원문·자격정보는 보고서에 쓰지 않는다. PDF discovery 증가에 따라 시작 여유 공간은 동적으로 다시 계산될 수 있으므로 이전 고정 디스크 수치만으로 판정하지 않는다.
3. 첫 run이 no-change를 내거나 새 generation이 없다면 코드 계약 변경/기존 상태와 로그를 조사해 실제 수용 기준을 검증한다. 거짓으로 새 generation을 만들기 위해 재처리하지 않는다. 이후 provider 전환에도 contract/generation identity가 불필요하게 바뀌지 않는다는 기존 CI·격리 테스트 근거를 같이 기록한다. 006을 운영에서 켜서 증명하지 않는다.
4. **신한카드가 계속 불통이면:** 반복 재시작·Paddle OCR·LLM 추론으로 우회하지 않는다. MCP 배포/마이그레이션/verify처럼 외부 수집과 무관한 단계만 완료할 수 있다. 새 Worker 첫 run은 성공 판정하지 않으며, timer는 작업 전 원래 상태로 반드시 복구해 정기 실행 기회를 유지한다. 외부 장애로 중단한 단계와 다음에 재개할 정확한 게이트를 `FIX_01_REPORT.md`에 남긴다. 이 경우 007 단계 6은 **부분 완료**이고 Reviewer 인수 전까지 닫지 않는다.
5. 성공·실패와 관계없이 마지막에 timer를 작업 전 원래 상태로 복구하고 다음 예약 및 MCP health를 확인한다. 새 Worker가 실패하면 로그와 stable pointer를 확인한다. 007 결함 또는 OCR SHA 변경/대량 provider 호출이 있으면 기존 **MCP/Worker 이미지와 env/Compose 설정**으로 되돌린다. 이전의 추가형 content variant는 삭제하지 않는다. 기존 stable/레거시 CAS가 유지되는지 확인한다. 외부 신한 장애만 원인이라면 007 결함으로 단정하지 말고 상태를 분리 보고한다.

## 4. 수용 기준과 보고 형식

- 운영 배포 코드의 정확한 커밋·이미지 다이제스트, 이전 이미지/설정 1세트, 동일 Compose project/volume 유지가 확인된다.
- 마이그레이션 dry-run → apply → verify가 정상 종료하며, **현재 stable OCR 문서 100%**가 검증된 content variant에 대응한다. 이전 OCR/CAS/레거시 원본은 불변이다. 원격 GC는 계속 꺼져 있다.
- 새 MCP가 old/new generation을 정상 수용하고 healthy다. 첫 성공 Worker run에서 기존 동일 PDF 문서의 OCR SHA/크기 불변, OCR provider 호출 0건, 새 generation 게시·MCP 반영이 확인된다. 신규 PDF OCR 호출은 별도 집계한다.
- timer는 종료 시 원래 활성 상태이고 중복 run이 없다. 실패 시 기존 이미지·설정으로 롤백 가능하며 실제 롤백했다면 상태가 복원됐음을 기록한다.
- `FIX_01_REPORT.md`를 새로 작성해 단계별 시각, 명령과 종료 코드, CI와 현재 head, dry-run/apply/verify 요약 JSON, stable ID 전후, OCR 호출 지표, MCP/Worker/timer 상태, 디스크 전후, 신한 장애 판정, 중단·재개 지점을 남긴다. 비밀값·OCR 본문·불필요한 전체 로그는 넣지 않는다. **후임 Executor는 REPORT를 작성하고 종료한다.** Reviewer가 그 증거로 최종 인수를 판정한다. 불필요한 전체 suite 재실행은 CI가 같은 커밋을 통과한 경우 생략할 수 있다.

## 5. 후임 에이전트용 작업 절약 규칙

읽기 전용 전수 인벤토리를 같은 stable ID에 대해 반복하지 않는다. 우선순위는 정확한 image/config 확인 → 1회 dry-run → 안전한 apply → verify → 정상 run 1회다. 신한 외부 장애에는 짧게 재확인하고 멈추며 무한 재시도하지 않는다. OCR/Paddle 작업을 검증 목적으로 새로 돌리지 않는다. 이 FIX와 코드가 모순되면 현재 Git 구현과 실제 운영 상태를 근거로 최소한만 조정하고 `FIX_01_REPORT.md`에 편차와 이유를 기록한다.
