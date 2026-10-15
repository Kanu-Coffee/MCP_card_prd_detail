# 015 Executor REPORT — Worker 초기검증 복구·디스크 확보·용량 예측 재검토

- 작성: 2026-10-15, Executor (OpenCode, qwen3.8-flash). 기준 PLAN: 본 디렉터리 `PLAN.md` (Planner Codex, 2026-10-15 06:34 KST).
- 결과 요약: **P0/P1 구현·정리·복구 배치 완료.** 실패 하위 gate 확정( reserved free-space ), 계약/용량 실패의 typed reason + 수치 이력 보존 구현, CardRAG 개발 잔재 실제 정리(+순회수 이하 기록), 예측 보정 검증(계수는 보존 근거 확정), bounded startup 재시도·영속 이력 도입, Worker 이미지 `cardrag-worker:015-c635353` 1회 빌드·운영 반영, startup-check 통과, 실제 mount에서 최신 용량 gate 통과 확인 후 **복구 배치 1회 성공·게시·백업 flush 완료**. 다음 10/16 03:00 타이머 유지, 검증된 rollback 쌍 유지.
- 주의할 점 한 줄: 내일 게이트 통과 추정 여유는 약 19GB이나 신한 10회 연속 수집 실패(origin network)와 MCP state 3세대(42.9GB) 잔존은 별도 후속 조치로 남음(§8, §9).

## 1. 시작 상태 검증 (PLAN §5-A.1)

- Git: `main`, HEAD `f8453d0`, tracked/untracked 변경 없음(015 handoff 디렉터리만 신규). §7.3·§7.5 진행 후 `c635353` 커밋으로 이미지 빌드(§6).
- `/opt/cardrag/current -> /opt/cardrag/013-50a0129`, Worker `cardrag-worker:014-v1.0.36`(image ID `sha256:3a8c74ffdc2e…`), MCP `cardrag-mcp:013-50a0129` healthy, readiness `{"ready":true}` 확인.
- timer active(다음 10/16 03:00 KST), service 최종 `failed`(10/15 03:47:31 exit 1, InvocationID `01d0c8fd0ac342ef9f9b71c128da8401`), 실행 중 Worker 없음.
- 시작 free: `df -B1 /` → 64,401,887,232 B (59.97 GiB), inode 사용률 10%.
- sudo 비대화식 인증 불가 제약은 PLAN과 동일하게 유효. 모든 운영 작업은 lee/docker 권한 + Docker API/compose로 수행, root 전용 작업 불요.

## 2. A — 실패 증거 보존과 하위 gate 확정 (P0)

### 2.1 증거 보존 (private operations)

`/opt/cardrag/015-f8453d0/operations/evidence-20261015/` 에 저장(전체 env/토큰/응답 본문 없음, 수치·분류·해시만):

- `journal-worker-20261014_15.log` — `journalctl -u cardrag-worker.service --since '2026-10-14 00:00:00' --until '2026-10-16 00:00:00'`(PLAN과 동일 KST 경계, 232,261 B)
- `docker-system-df-before/after.txt`, `docker-images-before.tsv`, `docker-volumes-before.txt`, `docker-containers-cardrag-before.tsv`
- `container-inspect-*.json`/`container-log-*.txt`(013-retry, 014-startup, 014-recovery, exciting_montalcini), `volume-inspect-grype.json`
- `state-ro-inspection.txt` — 10/15 실패 run의 WorkerState 읽기 전용 조사 결과(§2.2)
- `cleanup-removals.log`, `cleanup-removals-round2.log`, `build-worker-015.log`, `startup-check-015(.log)`, `recovery-batch-015.log`, `d6f3399d-reports.tar`
- `deployment-015-c635353/`(compose.secrets.before.yaml, deployment.json)

journalctl 영속 보존 확인: `/var/log/journal` 존재(persistent), 디스크 사용 1.5G — journal이 주 증거 저장소로 유효. JSONL 보완(§4.2)은 그 위에 구조화 이력 추가.

### 2.2 10/15 실패 하위 gate 확정

기존 읽기 전용 경로로 확정했다: state volume을 `docker run -v …:ro` + 컨테이너 내 `/tmp` 복사본(`mode=ro` sqlite, WAL absent·checkpointed 상태, live DB에 immutable 미사용, 복사 전후 md5 동일성 확인 후 사용)에서 `run` 테이블 조회:

- run `d6f3399d9cc84caba0a6deddbc26f65f`, status `failed`, `error = "Worker reserved free-space gate rejected v5 artifacts"` — **하위 gate = reserved free-space gate 확정** (sidecar/DB file limit/state quota/WAL baseline 아님).
- stage 행: shinhan/lotte discovery `failed`(attempt_count=4, category=runtime), unsupported_drm skipped 6건 등. embedding-v5 stage는 preflight 거절로 미도달(127.05s는 cache lookup 구간).
- 다만 당시 로그는 free/required 수치와 deficit을 남기지 않았다 → 그 부분(측정값)은 미확정으로 유지했고, §3의 구조화 기록으로 재현 가능하게 했다. 10/14 generic contract는 사후 분류 불능(원문 소실) — future recurrence만 typed code로 확정 가능.

### 2.3 코드 구현 (typed reason·구조화 필드, raw str/repr 미사용)

- `capacity_v5.py`: `V5CapacityError(reason_code=…, details={int|None만, snake-key만, MAX_SAFE 범위})` 신규. 게이트별 상수: `capacity_sidecar_limit`, `capacity_database_limit`, `capacity_state_quota`, `capacity_free_space`, `capacity_remaining_free_space`, `capacity_startup_free_space`, `capacity_wal_*`, `capacity_probe_unavailable`, `capacity_path_invalid`, `capacity_state_root_changed`, `capacity_available_unmeasured`(미측정 시 `available_bytes=None, probe_errno`), `capacity_policy_lost`, `capacity_prediction_contract_invalid`, `capacity_bytes_range_exceeded`, 기본 `capacity_unclassified`. 각 거절 지점은 prediction·limit·usage/free/required/deficit 수치를 details로 동반. `V5CapacitySnapshot`에 `required_free_bytes/policy/probe_path` 추가(성공 기록용, 기본값호환).
- `pipeline.py`: 실패/성공 모두 `_capacity_failure_fields`(reason+수치) / `_capacity_gate_fields`(sidecar/database/state limit·reserve·required) + `probe_path` 기록(log 6441/6456/6487/6564 블록). WAL seal/bound/reseal 래퍼에 typed code. 종단 에러 문자열(`_safe_v5_embedding_terminal_error`)도 reason+수치 포함(비밀 없음).
- `embedding_v5.py`: `EmbeddingV5Error(reason_code=…, provider_id=allowlist만)`. 계약 경계 분리: `metadata_not_json/shape_invalid/model_id_mismatch/endpoint_incomplete/endpoint_model_mismatch/maximum_tokens_invalid/supported_parameters_invalid/no_allowlisted_endpoint`, `provider_endpoint_missing|fp8|ambiguous`, `profile_fp8_forbidden/endpoint_model_mismatch`, `response_*`(not_json/shape/model/provider/count/index/indices/dimension/non-numeric/non-finite/norm/normalization/content-length/byte-cap), `token_counter_invalid`, `token_limit_exceeded`, `repeat_cosine_below_gate`, `cosine_*`; request 계층은 `request_<kind>`. 미상 코드/비-allowlist provider는 sanitize되어 유실 없이 `embedding_contract_unclassified`.
- `cli.py`: `_classify_startup_exception`이 category+`reason_code`+`capacity_details`+`provider_id`를 반환, `_log_startup_stage_failure`가 `correlation_id`·`attempt/max`와 함께 기록, `_echo_worker_unexpected_failure` payload가 `failure_reason_code`/`capacity_details`/`provider_id`를 보존(`reason_code:"worker_unexpected_failure"` 하위 호환 유지). raw exception text는 어디에도 흐르지 않음.
- 원 10/14 형태 비교(하위 gate는 §2.2, contract 경계는 재발 시에만 확정): 이후 동일 실패는 journal·JSON payload에 경계별 reason+수치가 남는다.

## 3. C — 예측 적정성 검증 (P1): 계수 유지 결정과 근거

실측 대조(operations/DB/MCP manifest, 유료 재임베딩·대형 corpus 재생성 없음):

- sidecar: 예측 10,100,326,400 B = 실측 manifest `vector_sidecar` 정확 일치.
- DB: 예측 15,341,322,240 B vs sealed `serving_database` 4,912,087,040 B = **3.12배**(PLAN §4.2의 3.12 재확인). 예측 지배항: row envelope 12.6M×768B≈9.68GB + payload 3.54GB + FTS×4 + secondary index — 실측 390B/row 대비 envelope는 명시적 worst-case 상한(500B key overflow-page discontinuity fixture로 256/512B는 실패한 이력이 있어 `test_capacity_v5_export_regression.py`·`test_database_prediction_is_calibrated_*`가 하한을 회귀 고정).
- peak×4 근거는 코드와 함께 검증 유지(완화 불가 결론): exporter는 작업 DB를 `2× effective_limit` page cap으로 **쓰기 중** 강제(`exporter_v5.py:2135`, 초과 시 `ServingDatabaseV5Error`:2469), `VACUUM INTO` 직전 `2×limit+reserve` free 재검사(2474–2481), 목적지 사후 cap(2487–2492). vacuum 중 작업 DB(≤2P)와 목적지(≤2P)가 물리적으로 동시 존재하므로 4P는 완화 여지가 없는 상한. ×4→×2 완화는 SQLite 단일 문장 destination cap을 직접 강제할 수 없어 "동등 보호"를 증명 불가 → **계수(768B row / FTS×4 / peak×4) 전부 유지**, REPORT 기록으로 근거·보정 여부 명시(C.3 충족).
- mount 실측: worker state(`/var/lib/cardrag-worker` probe)·serving·MCP state 모두 `/dev/sda2` 동일 host filesystem 확인(df와 게이트 free 일치). publication 복사 peak는 local_publisher preflight(`db*2+vectors*2+objects+10MiB`) + 4P envelope이 위 phase 순서상 봉인 이후라 포함 관계 검증됨(중복 charge 아님).
- 예측 수치 실측 확인(성공 배치): free 82.58GB ≥ required 73.63GB 통과, peak 실측 도달 없음(작업 DB/목적지 cap 내) → "검사 과대"는 상한으로서 정당, 그러나 시작 gate와의 조합으로 운영 deadlock을 유발한 점이 실제 리스크였고 §4·§5의 디스크 확보가 해법.
- C.5 시작 gate 성격 표시 구현: `note=minimum_start_gate_only` 로그 + 직전 성공 run 요구량(`capacity-observation.json`, 게이트 통과 시 atomic 기록, 실패 시 diagnostic-only) 대비 부족 시 **warning only** 사전 경고(하드 gate는 embedding 이전 게이트 유지, 과거 예측만으로 안전 실행 오탐 차단 안 함).

## 4. B — 개발 잔재 실제 정리 (P0): 삭제 대상·회수 byte·전후 free

대상 표(Docker object별 ID/참조/크기/근거)는 `docker-system-df-before.txt` + §4.1 로그 참조. 금지 작업(`system prune -a --volumes`, 운영 volume 정리, `<none>` 일괄 삭제, tmpfs 기대기)은 일절 미사용.

### 4.1 1차 정리 (06:59, `cleanup-removals.log`)

| 대상 | ID/이름 | 회수 | 근거 |
|---|---|---|---|
| grype 스캔 잔재 | container `exciting_montalcini`(Created, 0 logs), volume `grype-cache` `grype-db-cache` | 3.244 GB | 생성시각 10-13 16:45 KST=014 릴리스 스캔 용도, link 0, 실행 스캐너 없음, 재생성 가능 cache |
| CardRAG 구 이미지 | `cardrag-worker:013-50a0129-codex-01621`(aaec660b6885), `cardrag-worker:009-b544a80`(bb457d7d6752), dangling 371895b40861/2325bb286ec3 | ~7.6 GB | 미참조·재생성 가능, rollback 쌍(당시 014/013) 보존 |
| 증거 컨테이너 | `cardrag-worker-013-retry-20261010`, `cardrag-worker-014-v1036-startup` | 0 (inspect+logs 보존) | 수명주기 종료 기록, 원문 보존 후 개별 삭제 |
| unused 빌드 cache | `docker builder prune -f`(default 범위만) | 3.653 GB | in-use/shared 레이어 제외, 범위 제한 |
| uv cache | `uv cache prune` | 0.12 MB | 대부분 현 환경/타 프로젝트 참조 → prune-safe분만(무차별 삭제 금지) |

전후 free: **64,401,887,232 → 76,763,537,408 B (+12.36 GB)**. inodes 무변화(10%→9%).

### 4.2 빌드 후 재현상과 2차 정리 (09:43)

빌드 자체 소비 +BuildKit 자동 GC 산출은 §6에 기록. 배치 성공 후:

| 대상 | 회수 | 근거 |
|---|---|---|
| 미참조 빌드 cache 재축적분 `docker builder prune -f` | 6.801 GB | unused only |
| 실패 run 디렉터리 `runs/d6f3399d…` | 3.85 GB | reports는 `d6f3399d-reports.tar`로 아카이브 후 삭제(§9 편차 P-1), run/error/status DB 행·journal 보존, generation 파일 아님·재사용 없음 |
| `cardrag-worker:013-50a0129` 이미지 | 3.63 GB | 015 적용 후 2-generation 이전, immediate rollback은 014 유지 |
| 마지막 orphan 증거 컨테이너 `cardrag-worker-014-recovery` | 0 | inspect+90KB 로그 보존 후 삭제 → orphan warning 소멸(재배치 로그 0건 확인) |

**순회수(실제 disk) 최종: free = 93,324,283,904 B (≈86.9 GiB, 77%).** 내일 03:00 게이트 예상 required(≈73.63 GB)+run 자체 gate-전 checkpoint 증가(관측 3.8~4.1 GB) 대비 **여유 약 15~19 GB** → 목표 공간 확보 완료(B.6 최신 검사 통과 기준으로 판정).

## 5. D — 초기검증 재시도·영속 이력 (P1)

- 기존 재시도 감사(D.1): HTTP layer는 attempts=12, base=1s, cap=60s, retry-after 존중(metadata GET/embedding POST) — 운영 compose/env와 일치 확인. worst-case 단일 stage attempt ≈ 12×(timeout 30s) + backoff ≈ 21분(연결 유지). 기존 transient는 그대로 재사용하고 stage 단위 재시도와 곱셈 폭증을 방지(D.2): stage 재시도 allowlist는 metadata content-availability 4종(`metadata_not_json`, `metadata_shape_invalid`, `metadata_no_allowlisted_endpoint`, `provider_endpoint_missing`; HTTP 2xx content glitch라 internal retry가 전혀 커버하지 못한 계층)뿐 — generic contract·FP8·dimension·norm·cosine·model·token 등 결정적 위반과 capacity 거절은 즉시 terminal(D.3, 10/14 generic은 통째로 retryable화하지 않음: 미분류 코드 `embedding_contract_unclassified`는 allowlist 부재로 1회도 재시도 안 함).
- budget: `CARDRAG_WORKER_STARTUP_CHECK_MAX_ATTEMPTS` 기본 **2**(범위 1–3), `…_DEADLINE_SECONDS` 기본 **600**(60–3600). attempt 간 지연 30s, 새 attempt 시작 전 누적경과+delay가 deadline 미만일 때만. 최악 총 시간: 12회 HTTP retries가 사실상 전부 소진된 attempt는 deadline(600s) 이전에 종료 판정되므로 second attempt 시작 불가 → bound ≈ deadline + 진행 중 attempt 잔여 ≈ 21~25분/invocation. compose.yaml 기본값 노출 + ops 사본 동일 갱신.
- 재시도는 초기검증 범위 내(metadata refetch)에서만 — corpus/게시/백업 재실행 없음, systemd `Restart=` 미추가(D.4). lock-before-DB, busy, SIGTERM 계약 불변(startup-check는 production 경로 재사용, run 생성 전 단계만).
- attempt 이력(D.5): invocation별 `correlation_id`(uuid 16B hex) 생성, 모든 startup 로그에 동반. run 생성 전 `startup-attempt-history.jsonl`(state dir, 256KiB 초과 시 1세대 rotate, O_APPEND 기록, 저장 실패 시 stderr/journal fallback만) — 실물 검증: startup-check 6건 + recovery run 기록, 합계 10 lines, 첫 행 stage=`startup_capacity_preflight` status=passed attempt=1/1 포함.
- 운영 확인 로그(10/15 08:01~08:02 KST, `recovery-batch-015.log`): `Startup stage starting stage=qwen_endpoint_preflight … attempt_max=2 deadline_seconds=600` → `passed … attempts=1/2`(11.458s). 10/14 유사 glitch 재발 시 경계별 reason code+attempt 이력이 남는다.

## 6. 빌드·배포·startup-check·복구 배치 (§7.3–7.6)

- 커밋: `c635353 feat(worker): typed capacity/contract reasons, bounded startup retry, cleanup observability` (14 files, tracked test/lint/type/full-suite green 후). push/PR/tag 미수행(스โค프).
- 빌드 1회: `docker build --target worker -t cardrag-worker:015-c635353` exit 0, image ID `sha256:e97c2c599dfd…`, label revision `c6353533…` = HEAD 일치 확인. 빌드 소비(~12.1GB) 직후 free 64.6GB로 내려갔으나 BuildKit free-space policy가 unused cache/이미지를 자동 회수(+containerd 참조 보호로 실행 이미지 전수 무사 확인: current·rollback·실행 컨테이너 이미지 모두 잔존)하여 startup-check 시점 free 79.86GB.
- 배포(014 pattern, image-only): `compose.secrets.yaml` worker image 014→015, 이전 사본 보존, 두 env 기본값 라인 추가, `compose config --quiet` 통과. current symlink/volumes/secrets/MCP 무변경. rollback: `cardrag-worker:014-v1.0.36` + `cardrag-mcp:013-50a0129` 유지.
- startup-check 1회(`cardrag-worker-015-startup`, exit 0, 36.5s): capacity startup gate passed(`note=minimum_start_gate_only`, correlation_id), ocr_resolver/tokenizer/qwen preflight(attempts=1/2)/embedding_verification 통과, provider=deepinfra, 24 samples, repeat cosine 1.0, cross cosine 0.9957, tokenizer sha `83cdf8c3…`. 실패 분류에 따라 처리하며 무한 반복하지 않음(§7.4).
- 복구 배치 1회(writer 없음 확인 후, `cardrag-worker-015-recovery`, exit 0, 5,832.2s):
  - 실패 증거 구간 재현 없이 통과: `V5 capacity preflight passed … filesystem_free_bytes=82,583,404,544 … required_free_bytes=73,628,921,528 probe_path=/var/lib/cardrag-worker`(views 616,475 / misses 50 / limits 전부 기록). `V5 pre-export capacity recheck passed` 동일 형식. remaining/WAL boundary 로그 정상.
  - run `ec1255d924ab4c87a8a12491b762fc02`, generation `g-ec1255d924ab4c87a8a12491-8f4256acf3e6`, corpus_sha `8f4256ac…`(10/14 생성 corpus 동일 — 신규 문서 변경 없음 확인), status succeeded, published true, `pdf_downloads=0`, OCR 5,521/5,521 succeeded·cache reused 5,520·provider documents 신규 1·failed 0, embedding unique miss 50(유료 API는 preflight/샘플 + 50 miss batch만).
  - 게시: serving에 신규 generation, `Local serving retention cleanup retained=2 removed=1 reclaimed_bytes=15,014,669,136`(§7 observability가 실운영에서 동작 확인), MCP `current.json` = g-ec1255d9, `/health/ready` `{"ready":true}`, web/edge healthy(총 healthy 10).
  - 백업: `backup_status=succeeded`, `last_backup_at=2026-10-15T00:38:39Z`, `runs_since_last_backup=0`, upload 235,835 B / requests 8 → 즉시 flush. **이로써 E "backup deferred"는 정상 유예로 판정**(§7.4) — 직전 상태 `deferred`는 threshold 미달(policy 정상)이었고 미보호 generation 잔존 없음(백업은 CAS 단위라 신규 generation 즉시 반영).
  - timer active(다음 10/16 03:00 KST), 검증된 rollback 이미지 유지. 초기화 통과와 전체 완료를 구분 보고: 둘 다 성공.
  - monitor 재현 명령: `docker logs -f cardrag-worker-015-recovery`(완료 후에도 로그 보존), `systemctl list-timers cardrag-worker.timer`, `journalctl -u cardrag-worker.service --since today`.
  - 배치 중 폴링은 상태 변경 시점 샘플링만 수행(장시간 무의미 계속 폴링 없음).

## 7. E — 추가 발견과 처리

- **신한·롯데 연속 degraded**: 어댑터 교체는 시작하지 않았다(fixture 재현 검증 범위 초과). 대신 운영 가시화 요구를 구현: `state.WorkerState.issuer_consecutive_failures()`(WITH-ROWID 없는 history 테이블을 `run` join recent-30으로 scan)를 `issuer-collection.json` 각 issuer의 `consecutive_failed_runs`로 발행. 실측: shinhan `issuer_origin_network` **연속 10**, last_ok 10-05 18:05, carry 931; lotte `issuer_source_invalid` 연속 2, last_ok 10-12 18:06, carry 647. carry는성공 run에도 freshness 미갱신을 숨길 수 있다는 지적대로 "마지막 정상 수집시각"과 함께 이제 매 run 보고서에 명시된다.
- **서빙 freshness**: healthy ≠ 신규 게시. 현재 확인됨: MCP current=10/15 배치 generation, readiness true. 운영 요약 반영은 위 실행 로그·current.json 근거로 대체(독립 대시보드 추가는 스코프 외).
- **보존·GC 3체제 정합**: serving local_retention = current+prev 2(코드로 동작 확인, 15GB 회수 관측). MCP state는 `generation-gc-roots` snapshot **empty**(owner 없음)인데 generations 3개×14,316MB = 42.9GB 잔존(§ evidence): cb12f9988100445…·09b1b5342b9449e…·49ebccf8… 중 현재는 g-ec1255d9로 switch되었으나 MCP의 검증된 GC 경로/backup 포괄성은 이번 작업에서 삭제 근거를 확보하지 못해 **수동 삭제 금지 유지** — PLAN B.5/E가 요구한 "검증된 generation GC는 별도 조치로 제시"를 본 보고서로 이행(§9 R-2). Worker state retention은 §4.2 참조.
- **백업 deferred 판정**: §6 백업 줄 참조 — 정상 유예였고 이번 run에서 flush로 해소. 백업 미커버 생성이 GC 대상에서 보호되는지 추가 확인은 R-2의 일부.
- **local_publisher suppress 지점 개선**: retention/staging rmtree 실패를 silent pass 대신 `_remove_tree_with_accounting`으로 errno+attempted_bytes 경고(`reason_code=local_retention_cleanup_failed`)와 회수량/삭제수 summary 로그로 교체. 삭제 실패 시 성공처럼 집계되지 않음(removed count는 실패 시 증가 안 함). current+prev/reader/lock 불변성 유지, CAS temp clean-up 동작 무변경(test suite 통과로 회귀 확인).
- **orphan warning**: 3개 증거 컨테이너 모두 로그/inspect private 보존 후 개별 정리 → 오늘 recovery 재실행 로그에 `orphan containers` warning 0건. `--remove-orphans` 일괄 미사용.

## 8. 검증·테스트·정적 검사

- PLAN §6 대상 5개 파일 우선: `test_cli_settings_provider.py test_embedding_v5.py test_capacity_v5.py test_capacity_v5_export_regression.py test_exporter_v5.py` → 통과(수정 필요 test 1건은 payload에 `failure_reason_code` 추가 반영, `test_lock_concurrency_multiprocess.py` fixture는 신규 `_qwen_embedding_provider` kwargs 허용으로 정합화).
- 신규 회귀(총 12 cases): capacity gate별 reason_code·deficit 수치·details sanitize·snapshot gate-context(5), embedding metadata/response typed codes·sanitize(3), CLI retry allowlist·결정적 비재시도·attempt/deadline bound·history rotate/fallback·capacity echo·observation validation(6 그룹).
- 전체 suite: **2,520 passed / 0 failed** (`uv run --no-sync pytest -q`, runtime_v1·MCP 포함, 69.9s).
- `ruff check` 전 패키지 통과, `ruff format --check` 225 files clean, `mypy` strict **Success: 108 source files, no issues**.
- 기밀 위생: secret/env/응답본문/토큰 무기록 확인(로그 샘플 grep), reason_code·provider_id·errno·byte 정수만 외부화. `uv lock`/의존성 변경 없음.

## 9. 편차·잔여 위험·후속 제안

편차(PLAN 허용 재량 범위):
- **P-1**: `runs/d6f3399d…`(3.85GB) 디렉터리 수동 삭제. native incomplete-keep(=2) 정책상 다음 실패 전까지는 자동 회수 불가이고, 그대로 두면 내일 gate가 자기 checkpoint 증가분(~4GB)과 합산되어 실패하는 deadlock이 재현됨. reports tar로 보존, DB run/error/status 행·journal 원본 유지, resume source 아님. 편차 근거를 cleanup-removals-round2.log에 기록.
- **P-2**: push·PR·tag·공개릴리스 미수행(PLAN §7 스코프에 없음). local commit만.
- **P-3**: 배포 시 ops `deploy/worker/compose.yaml`에 신규 env 기본값 2줄 추가(repo와 동일) — image-only 변경 원칙에 대한 최소한의 설정 가시화.

잔여/후속(권장 우선순위):
1. **R-1 10/16 03:00 예약 실행 관찰** — 현재 free 93.3GB vs 예상 required 73.6GB + run 자기 증가분 ~4GB ⇒ 통과 추정. 실패 시 새 로그가 하위 reason+수치를 즉시 확정(`journalctl -u cardrag-worker.service`).
2. **R-2 검증된 MCP generation GC(후속 과제 추천)** — `generation-gc-roots` empty·retention 2 정책 하에 잔존 3세대의 삭제 안전성(active reader/rollback/backup 포괄) 확인 후 14.3~28.6GB 회수 여지. 본 작업에서 미검증 삭제는 금지 지침 준수.
3. **R-3 신한 origin 네트워크 10연속 실패** — 어댑터 교체 없이 fixture 재현·응답 비교로 원인 구분(신한 측 장애 vs 출구 제한). carry 931건 데이터는 유지.
4. R-4 예측 tighten 후보: row envelope/FTS×4의 실측 분포 추가 교정 데이터 축적(현재 회귀 fixture가 하한 방어). single-corpus 3.12배만으로 계수 완화는 금지 조항대로 미시행.
5. v1.0.37 릴리스 봉인(문서/태그)은 별도 과제.

## 10. 재현/관측 명령 (monitor)

```bash
# 예약 실행 관찰
systemctl list-timers cardrag-worker.timer --no-pager
journalctl -u cardrag-worker.service --since '2026-10-16 00:00:00' --no-pager | tail -60

# 게이트 근거·이력
grep -E 'capacity preflight (passed|failed)|capacity_reason|Startup stage (failed|retry)' \
  <(docker logs cardrag-worker-015-recovery 2>&1)

# 상태/용량
df -B1 / ; df -i /
docker run --rm -v cardrag-worker-state:/s:ro --entrypoint cat cardrag-worker:015-c635353 /s/capacity-observation.json
docker run --rm -v cardrag-mcp-state:/s:ro --entrypoint cat cardrag-worker:015-c635353 /s/current.json
curl -fsS --max-time 10 http://127.0.0.1:18015/health/ready
```

증거 루트: `/opt/cardrag/015-f8453d0/operations/` (evidence-20261015, deployment-015-c635353).
