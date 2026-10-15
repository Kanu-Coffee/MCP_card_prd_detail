# 015 FIX_02 — 수정 코드 확인, 운영 반영·식별 증거 보완 요청

- 작성: 2026-10-15 12:38 KST, Reviewer Codex.
- 검토: PLAN, REPORT, FIX_01, FIX_01_REPORT, 현재 Git/status 및 수정 diff, 관련 테스트와 Docker/운영 설정.
- HEAD `4ba4b40`, 구현 실제 commit **`741cfe0a21ccebdae332baeba9ce6a2608809f3c`**. 시작 시 working tree clean. 기존 문서는 변경하지 않는다.
- 판정: **FIX_01의 4개 코드 결함은 수정 확인. 운영 인수는 보류 — P1 1건, P2 1건을 아래에 요청한다.** 새로운 전량 배치나 모델 변경은 필요하지 않다.

## 1. 이전 4건의 재검토 결과

| 항목 | 결과·근거 |
|---|---|
| 진행 중 preflight deadline | 공유 monotonic 잔여 시간을 `asyncio.wait_for`에 적용. deadline 초과 typed terminal, 다음 attempt 잔여 budget 반영. 관련 회귀 통과 |
| startup history symlink/FIFO | directory fd 상대 open, no-follow/nonblocking/regular-file 검사 및 rotate 목적지 검사 도입. symlink target 불변/FIFO fallback/rotate 회귀 통과 |
| 현재 run 실패 집계 | report의 current_run_id를 명시적으로 반영하고 과거 query에서 제외. 현재 성공 시 0, 실패 시 +1 및 limit 회귀 통과 |
| staging 삭제 집계 | `(succeeded, bytes)` 반환으로 removed/failed 분리. 빈 tree 성공 및 권한 실패 회귀 통과 |

Reviewer 실행 결과:

```sh
uv run --no-sync pytest -q \
  apps/cardrag-worker/tests/test_cli_settings_provider.py \
  apps/cardrag-worker/tests/test_embedding_v5.py \
  apps/cardrag-worker/tests/test_capacity_v5.py \
  apps/cardrag-worker/tests/test_capacity_v5_export_regression.py \
  apps/cardrag-worker/tests/test_exporter_v5.py \
  apps/cardrag-worker/tests/test_state.py \
  apps/cardrag-worker/tests/test_local_serving_and_backup.py \
  apps/cardrag-worker/tests/test_lock_concurrency_multiprocess.py
```

**341 passed / 12.74초** (Executor의 7파일에 lock-concurrency 1파일 추가). `ruff check apps/cardrag-worker/`, `mypy apps/cardrag-worker/src/`(57 files), `git diff --check c635353 741cfe0` 통과. 전체 2,530개 suite는 Executor 보고이며 Reviewer 재실행 결과와 구분한다. 미검증 concurrency/경로 경쟁까지 안전성을 전수 증명했다고 표현하지 않는다.

## 2. [P1] 예약 실행은 여전히 수정 전 이미지 사용

**근거 위치:** 실제 `/opt/cardrag/current/deploy/worker/compose.secrets.yaml:4`; FIX_01_REPORT §4.1.

운영 overlay는 아직 `image: cardrag-worker:015-c635353`이다. 수정 이미지 `015-741cfe0`를 빌드하고 `--help`로 실행한 것은 예약 실행 설정의 변경이나 production startup 검증이 아니다. timer는 active이며 다음 실행은 2026-10-16 03:00 KST. 현재 설정대로면 FIX_01의 4개 결함이 다음 예약에도 남는다. FIX_01_REPORT의 “10/16 배치부터 정확한 연속 실패 횟수가 기록”된다는 문장은 아직 성립하지 않는다.

**수정 요구:**

1. 기존 PLAN의 운영 복구 범위에 따라 writer 유무와 최신 free/required를 다시 확인하고, 기존 helper/env/overlays에 검증된 수정 이미지를 반영한다. current generation·MCP·auth/state/cache·검증된 rollback·timer를 보존한다.
2. systemd와 동일한 실제 Compose 조합에서 image 지정 및 volume/env 정합성을 확인한다. 전체 secret/env를 공개 기록에 출력하지 않는다.
3. production `startup-check`만 1회 실행해 exit/status, 실제 사용한 image ID, deadline/진단 코드 포함 모듈 식별, ready/current 보존 및 최신 여유 공간을 기록한다. 이 수정 검증을 위해 full corpus/OCR/backup을 재실행하지 않는다.
4. `FIX_02_REPORT.md`에 운영 반영 여부와 증거를 명시한다. 만약 운영 반영을 완료하지 못하면 정확한 사유/남은 명령을 기록하고 상태를 “개발 수정 완료·운영 미반영”으로 유지한다. 다음 예약 성공을 미리 확정하지 않는다.

## 3. [P2] 보고서 commit/image 식별자가 실제 자산과 다름

**근거 위치:** FIX_01_REPORT:5–6, §3.3; `git show 741cfe0`, `docker image inspect cardrag-worker:015-741cfe0`.

- 보고서 full commit: `741cfe03f7e6f85e492b42d385beceaa22ba2560`.
- 실제 full commit: **`741cfe0a21ccebdae332baeba9ce6a2608809f3c`**.
- 보고서 image ID prefix: `sha256:902b49aec13cfe604e311ee221a2e0cc98e008a8cf…`.
- Reviewer 조회한 실제 image ID: **`sha256:0e61d7d919eeccdc0f3334152d51933a83060160c5f66e9fca6780f4d0d1b01e`**.
- 실제 OCI label은 revision `unknown`, version `dev`. 태그 이름 자체는 source provenance가 아니다.

다만 Reviewer가 `docker run --rm --network none --entrypoint python cardrag-worker:015-741cfe0 …`로 installed module을 조회하여 아래 5개 source SHA256이 현재 Git working tree 파일과 정확히 일치함을 확인했다. 이미지가 잘못된 코드를 포함한다고 판정한 것은 아니며, 잘못 기록된 식별자로 배포/rollback 대상을 검증할 수 없는 점이 결함이다.

| 모듈 | 실제 image/host 공통 SHA256 |
|---|---|
| cli.py | `6b919092c04e127084c916b9ba0554a9febcd61a95fbb4cec7bda84c41f2c3a4` |
| embedding_v5.py | `338a98214784fc79802275ff15c64fe1998970137b91b803e8f133415f800dbb` |
| local_publisher.py | `ba1b2ae971a3bb5125bcd99c0c750ff2aad554fbe7a4b8fdb111ed0f9632de1a` |
| pipeline.py | `58352cb148932a30a52cc3b161d68b79664de20c719d5b95b1bf679d9550ca5e` |
| state.py | `9db08c4ef64491ae7c67f98cd3ab912e477b53683f49a47547392428b158e630` |

**수정 요구:** 기존 handoff를 덮어쓰지 않고 FIX_02_REPORT/private deployment record에 실제 full commit/image ID·installed source 검증을 정정 기록한다. rebuild가 필요하다면 Dockerfile의 `APP_VERSION`/`VCS_REF`를 실제 값으로 전달하되, label만을 이유로 무조건 rebuild하지 않는다. 기존 이미지를 사용한다면 현재 unknown/dev label과 실제 source hash 증거를 함께 명시한다. 운영에 지정한 image 및 startup-check container의 실제 `.Image`가 기록과 연결되어야 한다.

추가 문서 정정: FIX_01_REPORT §2.2의 “4P(약 15.3GB)”는 P가 약 15.3GB이므로 4P≈61.36GB이다. 계수 보존 선택은 허용하나 “ENOSPC 절대 방어”나 전체 peak 실측 완료로 표현하지 않는다. 기존 298개(5파일)→340개(7파일)는 비교 파일 범위가 달라 “신규 회귀 42개”의 근거가 아니며 구현 커밋의 실제 신규 test 수와 구분해 기록한다.

## 4. 인수 기준 및 현재 상태

- 코드 결함 4건은 해결 확인; 이번 요청은 운영 이미지 적용과 사실에 맞는 증거 정리가 중심이다.
- 검증된 image/source 매핑과 production startup-check 성공, 기존 ready/current·rollback·timer 보존을 확인하면 수정 라운드 인수 가능하다. 실제 예약 전체 실행은 이후 관측 사항이며 이번 인수를 위해 기다릴 필요는 없다.
- Reviewer 현재 `df -B1 /` available **90,093,551,616 B**, 다음 timer 10/16 03:00 KST. 변동값이므로 Executor가 조치 전에 다시 확인한다.
- 신규 제품 코드 변경이 없으면 이미 통과한 전체 테스트를 반복하지 않는다. 설정/이미지 반영에 필요한 검사만 수행한다.
- Executor는 이 문서를 실행하고 **`FIX_02_REPORT.md`**를 새로 작성한다. 기존 PLAN/REPORT/FIX/FIX_REPORT와 운영 증거를 보존한다.
