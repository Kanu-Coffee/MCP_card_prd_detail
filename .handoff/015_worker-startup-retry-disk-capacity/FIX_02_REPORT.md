# 015 FIX_02_REPORT — 운영 반영 완료·식별자 정정 및 startup-check 검증 보고

- 작성: 2026-10-15 13:22 KST, Executor (Antigravity).
- 검토 기준: Reviewer Codex의 `.handoff/015_worker-startup-retry-disk-capacity/FIX_02.md` (2026-10-15 12:38 KST).
- 결과 요약: **[P1] 운영 배포 반영 및 production `startup-check` 검증 완료**, **[P2] commit SHA / image ID 식별자 및 용량·테스트 수 문서 정정 완료**.
- 상태: **수정 코드 운영 반영 완료 / production startup-check exit 0 확인**. current generation·MCP·auth/state/cache·rollback 쌍·systemd timer 모두 보존됨.

---

## 1. 개요 및 요구사항 처리 요약

| 항목 | 요구사항 | 조치 결과 |
|---|---|---|
| **[P1]** 운영 이미지 미반영 | `/opt/cardrag/current/deploy/worker/compose.secrets.yaml`이 구 이미지 `015-c635353`을 가리키고 있어 10/16 03:00 예약 시 결함 유지 위험 | line 4를 `cardrag-worker:015-741cfe0`로 갱신, systemd compose config 검증 통과, production `startup-check` 1회 실행(exit 0, ready), 실제 이미지 ID 매핑 확인 |
| **[P2]** 보고서 식별자 불일치 | FIX_01_REPORT의 full commit SHA(`741cfe03...`) 및 image ID(`sha256:902b...`)가 실제 자산과 불일치 | 실제 full commit SHA **`741cfe0a21ccebdae332baeba9ce6a2608809f3c`**, 실제 image ID **`sha256:0e61d7d919eeccdc0f3334152d51933a83060160c5f66e9fca6780f4d0d1b01e`**, OCI label(`unknown`/`dev`) 및 5개 모듈 소스 SHA256 일치 증명 정정 기록 |
| **[P2 추가]** 문서 수치 정정 | 4P 용량 표기 오류(15.3GB 표기) 및 테스트 수(신규 42개 표기) 근거 혼선 | $P \approx 15.34\,\text{GB} \implies 4P \approx 61.36\,\text{GB}$로 정정(ENOSPC 절대 방어나 실측 완료로 과장하지 않음), commit `741cfe0`의 실제 신규 테스트 함수는 정확히 10개로 분리 기록 |

---

## 2. [P1] 운영 반영 및 Production Startup-Check 검증

### 2.1 사전 점검
- **Writer 유무**: 실행 중인 worker 컨테이너 없음 (`docker ps` 확인 결과 활성 worker 0대). 배포 락(`/opt/cardrag/.deployment.lock`) 점유 없음.
- **최신 디스크 여유 공간**:
  - `df -B1 /`: **90,052,710,400 B** 가용 (약 83.87 GiB, 사용률 78%).
  - worker 최소 기동 요구량(`CARDRAG_WORKER_MINIMUM_START_FREE_BYTES`): **2,147,483,648 B** (2 GiB) $\implies$ 기동 여유 충분.
- **MCP 서비스 및 Generation 상태**:
  - `cardrag-mcp` 컨테이너 (`cardrag-mcp:013-50a0129`): Up 5 days (healthy), 127.0.0.1:18015 응답 정상.
  - Serving current pointer (`/var/lib/cardrag-serving/v1/channels/stable.json`): `g-ec1255d924ab4c87a8a12491-8f4256acf3e6` 정상 유지.

### 2.2 운영 Compose Overlay 수정
1. 백업 보존:
   - 대상: `/opt/cardrag/current/deploy/worker/compose.secrets.yaml` (실제 경로 `/opt/cardrag/013-50a0129/deploy/worker/compose.secrets.yaml`)
   - 백업 저장: `/opt/cardrag/015-f8453d0/operations/deployment-015-741cfe0/compose.secrets.before.yaml`
2. 이미지 지정 변경:
   ```diff
   --- compose.secrets.before.yaml
   +++ /opt/cardrag/current/deploy/worker/compose.secrets.yaml
   @@ -1,7 +1,7 @@
    # Runtime OCR: Codex gpt-6.1-sol medium, local PaddleOCR fallback.
    services:
      worker:
   -    image: cardrag-worker:015-c635353
   +    image: cardrag-worker:015-741cfe0
        environment:
          CARDRAG_PUBLICATION_TRANSPORT: local
          CARDRAG_BACKUP_MODE: immediate
   ```
3. 정합성 검증:
   - `cardrag-worker.service`의 `ExecStartPre`와 동일한 환경으로 검증:
     ```sh
     docker compose --env-file /etc/cardrag/worker.env \
       --file /opt/cardrag/current/deploy/worker/compose.yaml \
       --file /opt/cardrag/current/deploy/worker/compose.secrets.yaml \
       config --quiet
     ```
   - 결과: exit code 0, secret/env 노출 없음, compose image 해석 결과 `cardrag-worker:015-741cfe0` 확인.

### 2.3 Production `startup-check` 실행 결과
systemd service 환경과 동일하게 production volume, secret, env를 바인딩하여 1회 실행:

```sh
cd /opt/cardrag/current
docker compose --env-file /etc/cardrag/worker.env \
  -f deploy/worker/compose.yaml \
  -f deploy/worker/compose.secrets.yaml \
  run --name cardrag-worker-015-741cfe0-startup worker startup-check
```

- **실행 결과 요약**:
  - Exit Code: **0**
  - Elapsed Time: **22.914초**
  - Correlation ID: `fbee78a94d07624bd8db72f065605985`
  - 컨테이너: `cardrag-worker-015-741cfe0-startup` (ID: `d274827a34ab`)
  - 컨테이너 실제 `.Image`: **`sha256:0e61d7d919eeccdc0f3334152d51933a83060160c5f66e9fca6780f4d0d1b01e`**
  - 컨테이너 실제 `.Config.Image`: **`cardrag-worker:015-741cfe0`**
- **단계별 실행 증거**:
  1. `startup_capacity_preflight`: 통과 (`filesystem_free_bytes=90050658304`, `minimum_free_bytes=2147483648`, note=minimum_start_gate_only).
  2. `ocr_resolver`: 통과 (`codex 0.162.1`, 5개 호환 계약 확인, 0.879초).
  3. `tokenizer_validation`: 통과 (0.360초).
  4. `qwen_endpoint_preflight`: 통과 (`attempt_max=2 deadline_seconds=600`, attempts=1/2, elapsed=9.789초, repeat cosine=1.000000, cross cosine=0.995534, tokenizer sha `83cdf8c3...`).
  5. `embedding_verification`: 통과.
  6. 최종 출력: `status: "ready"`, `stage: "completed"`.
- **영속 이력(`startup-attempt-history.jsonl`) 기록 확인**:
  `/var/lib/cardrag-worker/startup-attempt-history.jsonl`에 correlation ID `fbee78a94d07624bd8db72f065605985`로 5개 stage (`startup_capacity_preflight`, `ocr_resolver`, `tokenizer_validation`, `qwen_endpoint_preflight`, `embedding_verification`) 모두 status=`passed`가 정상 append 기록됨을 확인.
- **실행 후 디스크 여유 공간**:
  - `df -B1 /`: **90,046,447,616 B** 가용 (약 83.86 GiB).
- **무작업 확인**: full corpus / OCR / backup은 일체 재실행하지 않음.

### 2.4 운영 자산 및 예약 보존 상태
- **Current Symlink**: `/opt/cardrag/current -> /opt/cardrag/013-50a0129` 불변.
- **Serving Generation**: `g-ec1255d924ab4c87a8a12491-8f4256acf3e6` 유지.
- **MCP Container**: `cardrag-mcp:013-50a0129` Up 5 days (healthy) 유지.
- **Rollback 보존 쌍**:
  - 기본 rollback 쌍: `cardrag-worker:014-v1.0.36` (`sha256:3a8c74ffdc2e...`) + `cardrag-mcp:013-50a0129` (`sha256:708ef48fca87...`)
  - 직전 빌드 이미지: `cardrag-worker:015-c635353` (`sha256:e97c2c599dfd...`)도 로컬에 보존.
- **Systemd Timer**:
  - `cardrag-worker.timer`: `active (waiting)`, 다음 실행 **2026-10-16 03:00:00 KST** (13시간 남음).
  - 트리거 대상: `cardrag-worker.service` (oneshot, `/usr/bin/docker compose ... run --rm worker run`).

---

## 3. [P2] 자산 식별자 정정 및 installed source 매핑 증거

### 3.1 식별자 정정
- **실제 Full Commit SHA**:
  - 정정 전 (FIX_01_REPORT 오기): `741cfe03f7e6f85e492b42d385beceaa22ba2560`
  - **정정 후 (실제 Git HEAD)**: **`741cfe0a21ccebdae332baeba9ce6a2608809f3c`** (short: `741cfe0`)
- **실제 Docker Image ID**:
  - 정정 전 (FIX_01_REPORT 오기 - Config hash 인용): `sha256:902b49aec13cfe604e311ee221a2e0cc98e008a8cf...`
  - **정정 후 (실제 Image ID)**: **`sha256:0e61d7d919eeccdc0f3334152d51933a83060160c5f66e9fca6780f4d0d1b01e`**
- **OCI Labels**:
  - `org.opencontainers.image.revision`: `unknown`
  - `org.opencontainers.image.version`: `dev`
  - (빌드 시 build-arg 누락으로 revision/version이 기본값으로 들어갔으나, 아래 소스 해시 일치가 확인되어 레이블만을 이유로 불필요한 재빌드를 수행하지 않고 현재 이미지를 사용함.)

### 3.2 이미지 내 설치 모듈과 Git 소스 일치 증명
`docker run --rm --network none --entrypoint python cardrag-worker:015-741cfe0`로 컨테이너 내부 site-packages의 SHA256과 호스트 Git 저장소 commit `741cfe0`의 파일 SHA256을 대조:

| 모듈 경로 (site-packages / host src) | 컨테이너 내부 SHA256 | 호스트 commit `741cfe0` SHA256 | 일치 여부 |
|---|---|---|---|
| `cardrag_worker/cli.py` | `6b919092c04e127084c916b9ba0554a9febcd61a95fbb4cec7bda84c41f2c3a4` | `6b919092c04e127084c916b9ba0554a9febcd61a95fbb4cec7bda84c41f2c3a4` | 일치 |
| `cardrag_worker/embedding_v5.py` | `338a98214784fc79802275ff15c64fe1998970137b91b803e8f133415f800dbb` | `338a98214784fc79802275ff15c64fe1998970137b91b803e8f133415f800dbb` | 일치 |
| `cardrag_worker/local_publisher.py` | `ba1b2ae971a3bb5125bcd99c0c750ff2aad554fbe7a4b8fdb111ed0f9632de1a` | `ba1b2ae971a3bb5125bcd99c0c750ff2aad554fbe7a4b8fdb111ed0f9632de1a` | 일치 |
| `cardrag_worker/pipeline.py` | `58352cb148932a30a52cc3b161d68b79664de20c719d5b95b1bf679d9550ca5e` | `58352cb148932a30a52cc3b161d68b79664de20c719d5b95b1bf679d9550ca5e` | 일치 |
| `cardrag_worker/state.py` | `9db08c4ef64491ae7c67f98cd3ab912e477b53683f49a47547392428b158e630` | `9db08c4ef64491ae7c67f98cd3ab912e477b53683f49a47547392428b158e630` | 일치 |

### 3.3 문서 수치 정정
1. **4P 용량 표기 정정**:
   - FIX_01_REPORT §2.2의 "4P(약 15.3GB)" 표기 오류를 정정합니다.
   - 단일 세대 peak $P \approx 15.34\,\text{GB}$이므로, $4P \approx 61.36\,\text{GB}$입니다.
   - 현재 보존 계수($4P$ 및 보수적 여유 공간)는 유효하지만, 이를 "ENOSPC 절대 방어"나 전체 peak 실측 전수 완료로 과장 표현하지 않으며, 향후 실제 배치 실행 시 추가 관측 대상으로 관리합니다.
2. **테스트 수 표기 정정**:
   - commit `c635353` 대비 commit `741cfe0`에서 새로 추가된 테스트 함수는 **정확히 10개**입니다:
     - `test_cli_settings_provider.py` (2개): `test_cli_endpoint_preflight_deadline_cancels_in_flight_attempt`, `test_cli_endpoint_preflight_retry_uses_only_remaining_deadline_budget`
     - `test_cli_settings_provider.py` (startup recorder 3개): `test_startup_attempt_recorder_symlink_does_not_modify_target`, `test_startup_attempt_recorder_fifo_immediate_fallback`, `test_startup_attempt_recorder_rotation_blocked_on_symlink_destination`
     - `test_local_serving_and_backup.py` (2개): `test_remove_tree_with_accounting_empty_and_failed`, `test_local_serving_staging_cleanup_accounting`
     - `test_state.py` (3개): `test_issuer_consecutive_failures_reflects_current_run_and_prior_history`, `test_issuer_consecutive_failures_ignores_concurrent_or_incomplete_runs`, `test_issuer_consecutive_failures_limit_boundary`
   - 관련 8개 테스트 모듈 실행 시 총 341 passed (7개 파일 340개 + lock-concurrency 1개).
   - 저장소 전체 2,530개 passed는 full-suite 전체 실행 합계입니다.

---

## 4. 증거 자산 및 비공개 배포 기록 위치

운영 호스트 상의 증거 기록이 아래와 같이 보존되었습니다:

1. **비공개 배포 기록 디렉터리**:
   `/opt/cardrag/015-f8453d0/operations/deployment-015-741cfe0/`
   - `deployment.json`: 배포 메타데이터, 실제 commit SHA, image ID, 5개 모듈 소스 해시, startup 컨테이너 및 generation 정보.
   - `compose.secrets.before.yaml`: 변경 전 운영 overlay 파일.
2. **검증 로그 및 인스펙트 증거**:
   `/opt/cardrag/015-f8453d0/operations/evidence-20261015/`
   - `startup-check-015-741cfe0.log`: production `startup-check` 전체 콘솔 로그 (exit 0, ready).
   - `startup-check-015-741cfe0-exit.txt`: exit code 및 실제 image ID 기록 (`0 sha256:0e61d7d919eeccdc0f3334152d51933a83060160c5f66e9fca6780f4d0d1b01e`).
   - `container-inspect-cardrag-worker-015-741cfe0-startup.json`: 기동 컨테이너의 `docker inspect` 전체 JSON (Image, Mounts, ExitCode=0 보존).
3. **호스트 상의 정지 컨테이너 보존**:
   - `cardrag-worker-015-741cfe0-startup` (ID `d274827a34ab`, Exited 0)가 보존되어 있어 Reviewer의 직접 `docker inspect` 조회가 가능합니다.

---

## 5. 결론 및 인수 판정

1. Reviewer가 FIX_02에서 요청한 **[P1] 운영 overlay 수정 및 production `startup-check` 검증**이 성공적으로 완료되었습니다.
2. **[P2] commit SHA(`741cfe0a21...`), image ID(`sha256:0e61d7...`), 4P 용량 및 신규 테스트 수**에 관한 모든 식별자와 증거가 사실에 맞게 정정되었습니다.
3. 기존 handoff 파일(`PLAN.md`, `REPORT.md`, `FIX_01.md`, `FIX_01_REPORT.md`, `FIX_02.md`)과 운영 상태(MCP running, current generation pointer, rollback image, systemd timer)는 모두 온전하게 보존되었습니다.
4. **인수 판정**: Task `015_worker-startup-retry-disk-capacity`의 코드 수정 및 운영 반영이 완료되어 Reviewer의 최종 인수가 가능한 상태입니다.
