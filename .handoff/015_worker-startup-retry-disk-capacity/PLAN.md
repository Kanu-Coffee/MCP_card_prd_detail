# 015 — Worker 초기검증 복구·디스크 확보·용량 예측 재검토

- 작성: 2026-10-15 06:34 KST, Planner Codex.
- 조사 기간: 전일 2026-10-14 00:00–24:00 및 당일 2026-10-15 00:00–06:34, Asia/Seoul.
- 상태: 조사 및 실행 계획 발행. 이번 Planner 작업은 읽기 전용 운영 조사와 새 handoff 작성이며, 디스크 정리·제품 코드 변경·배포·복구 실행은 아직 수행하지 않았다. 아래 Executor 범위에 실제 정리 및 확보 결과 보고를 포함한다.

## 1. 목표·범위·보존 조건

1. 초기 연결/품질 검증 실패를 원인별로 구분하고, 제한된 재시도와 영속 이력으로 예약 실행의 무복구 종료를 개선한다.
2. 현재 개발 잔재를 실제로 정리하여 다음 배치에 필요한 공간을 확보한다. 삭제 대상·실제 회수량·전후 여유 공간을 REPORT에 남긴다.
3. 용량 검사가 실측에 비해 과대인지 검증하고, 합리적인 예측과 실제 쓰기 제한을 함께 유지한다.
4. 반복 수집 실패, 오래된 서빙 데이터, 보존/백업 및 실패 관측의 추가 문제를 조사·개선한다.

보존: 운영 MCP/웹 정상 서빙, current 및 검증된 rollback generation, Worker state/SQLite/WAL, PDF/OCR/embedding 캐시, auth, Paddle 모델, secret, 기존 handoff와 운영 증거. 기존 임베딩 모델·4096D FP32·정밀도·cosine/tokenizer gate를 우회하지 않는다. 다른 프로젝트 데이터·이미지를 미사용이라는 이유만으로 정리하지 않는다. 원격 전체 GC·새 모델·전체 OCR 재처리·공개 릴리스는 본 작업에 자동 포함하지 않는다.

## 2. 조사 시점 Git·운영 상태

- 저장소 `/home/lee/projects/MCP_card_prd_detail`, branch `main`, HEAD `f8453d0` (`docs(release): record v1.0.36 deployment and publication`). 시작 시 tracked/untracked 변경 없음.
- 기존 handoff 최대 번호 014. `014/PLAN.md`, `REPORT.md`, `RELEASE_REPORT_v1.0.36.md` 및 현재 소스를 확인했다. 014는 과거 경위를 제공하며 현재 로그가 이번 판단의 근거다. 기존 문서를 수정하지 않는다.
- `/opt/cardrag/current -> /opt/cardrag/013-50a0129`.
- 운영 Worker `cardrag-worker:014-v1.0.36`, image ID prefix `3a8c74ffdc2e`. 이전 startup-check 컨테이너의 allowlist 환경값도 확인했으나 Executor는 실행 당시 최신 Compose 조합을 다시 확인한다.
- MCP `cardrag-mcp:013-50a0129`, 조사 시 `healthy`. 웹/edge도 Docker 상태상 healthy.
- timer active, 다음 예약 2026-10-16 03:00 KST. 현재 실행 중 Worker 없음. service 최종 실행은 실패.
- systemd 실제 unit의 ExecStart는 Compose `run --rm worker run`; `Restart=` 지정 없음. journal이 종료 컨테이너의 주 증거이며 컨테이너 자동 삭제 자체가 무로그의 원인은 아니다.
- sudo 비대화식 인증 불가: `sudo -n true`는 authentication required. `/var/lib/docker` 직접 `du`는 권한 부족으로 유효한 측정이 아니므로 사용하지 않았다. Docker API 및 실행 중 MCP의 읽기 전용 파일 크기 조회로 보완했다. 구현/정리는 가능한 Docker 권한으로 진행하고, 실제로 필요한 root 작업만 정확한 명령과 함께 제약을 보고한다.

## 3. 당일·전일 실행에서 확인한 사실

원본: `journalctl -u cardrag-worker.service --since '2026-10-14 00:00:00' --until '2026-10-16 00:00:00' --no-pager` (조회 당시 미래 로그는 없음). 증거 재조회 시 동일 KST 경계를 사용한다.

| 실행 | 초기화·처리 | 종료 및 판단 |
|---|---|---|
| 10/14 03:00 예약 | free 75,380,293,632 B, 최소 2 GiB 통과. OCR resolver/tokenizer 통과. 03:00:24 Qwen preflight 실패: `category=contract status_code=None retryable=None attempts=None`, 검증 소요 9.394초 | 실행 23.101초, exit 1. JSON의 `phase=qwen_endpoint_preflight`, `reason_code=worker_unexpected_failure`. journal 이력은 있으나 정확한 계약 실패 종류가 사라짐. 관측 로그에 재시도는 없음 |
| 10/14 05:58:40 재기동 | Qwen preflight 17.463초 후 통과. 신한/롯데 수집 실패, 1,578 문서 carry. 06:42:59 용량 검사 통과 | 07:45:03–04 성공, 6,381.569초. run `49ebccf8f87b4233be1197fba8cf15e7`, generation `g-49ebccf8f87b4233be1197fb-5fd3b4018ab1`. 이 재기동이 자동 재시도였다는 근거는 없음 |
| 10/15 03:00 예약 | free 68,409,933,824 B, 최소 2 GiB 통과. Qwen preflight 10.426초 후 통과. 신한/롯데 실패, carry 1,578. 03:42:37 OCR 5,521/5,521 성공 | 03:47:18 `v5_capacity_preflight_failed`, derived_views 616,466 / unique_cache_misses 41 / DB payload 3,537,308,728 B / rows 12,606,613. 2,836.415초 후 실패, systemd 03:47:31 exit 1. 최종 JSON은 다시 generic `worker_unexpected_failure`, category capacity |

**요청 1의 정정 및 남은 결함:** 현재 버전에서 완전한 무로그 종료는 아니다. 014의 단계 로그·안전 분류는 적용돼 있다. 하지만 계약 오류가 모두 `contract`로 축약돼 원인/복구 가능성/횟수가 불명확하고, 예약 실행을 복구하는 상위 정책이 없다. 10/14의 실패를 네트워크 단절·Codex 버전 문제·특정 provider 장애로 확정하지 않는다. 나중 성공했다는 사실만으로 transient로 확정하지 않는다.

**당일 용량 실패의 확정 범위:** 용량 예측/검사 경계의 실패는 확정. 현재 로그는 파일 상한, state quota, free-space, WAL baseline 중 어떤 거절인지 및 그때 free/required를 기록하지 않으므로 정확한 하위 gate는 미확정이다. 실제 ENOSPC가 발생했다거나 디스크가 가득 찼다고 표현하지 않는다.

## 4. 디스크 및 용량 예측 근거

### 4.1 현재 측정

- `/dev/sda2` ext4: `df -B1 /` 기준 total 420,497,469,440 B, used 337,360,052,224 B, available **64,393,441,280 B (약 59.97 GiB)**, 84%. 변동 가능한 조사 시점 값.
- `/tmp`는 별도 tmpfs, 약 1.52GB 사용. `/tmp` 파일 삭제는 root 디스크 공간을 확보하지 못하며 tmpfs를 대형 export 우회 공간으로 사용하지 않는다.
- Docker `system df`: images 53.04GB, reclaimable 22.93GB; local volumes 155.3GB, reclaimable 3.244GB; build cache 27.37GB, reclaimable 표시 78.88MB. 공유 레이어/빌드 참조가 있으므로 단순 합산한 회수량을 약속하지 않는다.
- 주요 볼륨 (Docker 표기 decimal GB): Worker state 53.54GB, MCP state 49.53GB, serving 34.6GB, Paddle 모델 2.069GB. 모두 운영 데이터이며 개발 찌꺼기로 취급하지 않는다.
- `grype-cache` 3.244GB, link 0: 소유·사용 중 스캐너와 재생성 가능성을 확인할 정리 후보. `exciting_montalcini`는 grype Created 컨테이너로 확인됐으나 캐시 연관성은 별도 검증 필요.
- 사용자 cache 약 3.1GiB: uv 약 2.3GiB, Playwright 약 658MiB. 다른 프로젝트 공유 cache이므로 무차별 삭제 금지. 저장소 전체는 약 153MiB, `.mypy_cache` 약 32MiB, `.venv` 약 28MiB로 저장소 cache만 정리해서는 해결되지 않는다.
- `/opt/cardrag` 약 88MiB. 설치 디렉터리 삭제의 효과는 작으며 rollback/증거 보존을 우선한다.

### 4.2 예측과 실측

10/14 성공 실행의 같은 검사 로그:

```text
derived_views=616407 unique_cache_misses=0 wal_baseline_bytes=4152992
sidecar_bytes=10099212288 database_bytes=15339245568
logical_growth_bytes=25450999456 peak_growth_bytes=71468736160
state_usage_bytes=53528081480 filesystem_free_bytes=73993420800
```

- 필요 free = peak 71,468,736,160 + reserve 2,147,483,648 = **73,616,219,808 B**, 당시 free와 차이는 약 377MB에 불과했다.
- 조사 시점 available은 전일의 필요량보다 약 9.22GB 부족. 당일 정확한 필요량과 동일하다고 가정하지 않으며, 정리 목표는 최신 예측으로 다시 산출한다.
- 실행 중 MCP의 해당 성공 generation 파일 stat: `index.sqlite3` **4,911,407,104 B**, `vectors.f32` **10,099,212,288 B**. DB 예측 15,339,245,568 B는 sealed DB 실측의 **약 3.12배**. sidecar 예측은 실측과 일치.
- 식: DB = page-align(1MiB + payload + rows×768 + FTS-indexed-text×4 + secondary-index-text). DB export peak는 DB 예측×4. 예측/실제 sealed 크기 차이는 확인됐지만 export 전체 peak가 과대라는 결론은 아직 아니다.
- 소스 주석과 exporter는 overflow-page·unique-token 회귀 근거, 작업 DB 최대 2×예측, `VACUUM INTO` 목적지 실패 경로 최대 2×예측을 이유로 합계 4×를 잡는다. 단순히 계수를 1로 바꾸면 실패 중 디스크를 보호하는 계약이 깨진다.
- MCP local에는 약 15.01GB씩 3개 generation 존재. current는 10/14 generation, 나머지는 `g-cb12f9988100445eb7ecd873-035c4632e085`, `g-09b1b5342b9449e4879e8f55-f77a3ec18077`. `generation-gc-roots` 조회는 비어 있었으나 활성 reader/rollback/retention까지 없다는 뜻은 아니다. 기존 안전한 보존 정책과 일치하는지 검토할 후보이며 수동 삭제 대상으로 확정하지 않는다.

## 5. Executor 구현·운영 순서

### A. 실패 증거 및 세부 원인 보존 (우선순위 P0)

1. 시작 전 Git/status, 본 PLAN 및 014 관련 최종 보고서를 확인하고 사용자 변경을 보존한다. 최신 서비스 상태/Worker·backup writer 유무/current generation을 다시 확인한다.
2. 두 날짜 journal, systemd invocation ID, 이미지/소스 SHA 및 allowlist 설정을 private operations 기록으로 보존한다. 공개 handoff에는 필요한 수치·분류·해시만 남긴다. 전체 env/Compose 렌더/토큰/인증 URL/응답 본문을 기록하지 않는다.
3. `EmbeddingV5Error`, `V5CapacityError`에 안전한 typed reason code와 구조화 필드를 추가하고 CLI까지 보존한다. raw `str/repr(exc)` 출력으로 해결하지 않는다. 계약 실패는 metadata parse/missing provider/precision/model/dimension/response/count/norm/repeat cosine/cross-provider gate 등 실제 발생 경계를 분리한다. metric은 allowlist provider/model과 허용 범위 숫자만 기록한다.
4. 용량 거절은 sidecar limit / serving DB limit / state quota / filesystem free / WAL baseline을 구분하고 prediction 항목, limit, usage/free/required/deficit, probe filesystem을 성공·실패 모두 기록한다. 경로 검사 등 별도 안전 실패는 원래 의미를 보존한다. available 미측정이면 null과 이유를 남기고 임의 수치를 만들지 않는다.
5. 10/15 `WorkerState` 실패 run/report를 기존 읽기 전용 경로로 확인한다. 더 상세한 원인이 있다면 근거로 하위 gate를 확정하고, 없으면 미확정으로 유지한다. SQLite 조사 시 live DB+WAL 일관성을 보존하고 immutable 옵션을 live WAL DB에 오용하지 않는다.

### B. 개발 잔재 실제 정리·공간 확보 (P0)

1. build 작업·scanner·Worker/backup 동시 실행을 확인하고 Docker object별 ID/소유/참조/크기/보존 이유를 표로 만든다. 현재 이미지, 실행 컨테이너가 사용하는 이미지, 검증된 rollback 이미지, 기록이 아직 필요한 완료 컨테이너는 보호한다.
2. 우선 CardRAG에서 생성한 재생성 가능한 미참조 빌드 캐시·임시 이미지·검증 임시파일부터 정리한다. BuildKit cache는 공유/in-use/reclaimable 관계를 조회하고 범위를 제한한다. 27.37GB 전체를 회수 가능하다고 가정하지 않는다.
3. `grype-cache` 및 스캔 임시 컨테이너는 소유/현재 참조/실행 작업 부재를 확인한 뒤 정리한다. 다른 프로젝트 scanner cache인 경우 그 프로젝트 자산으로 분리 보고한다. uv cache는 설치된 도구의 안전한 prune 기능/범위가 맞는지 확인 후 사용하고 현재 Python 환경/lockfile을 보존한다.
4. `docker system prune -a --volumes`, 전체 image/volume/cache 삭제, `rm -rf /var/lib/docker`, 홈 전체 cache 삭제는 실행하지 않는다. 기존 운영 volume은 개발 잔재 정리 대상이 아니다. 단순 `<none>` 이미지도 실행 컨테이너가 참조할 수 있다.
5. 안전한 개발 잔재만으로 부족하면 C의 보정 결과와 함께 추가 storage 또는 검증된 generation GC를 별도 조치로 제시한다. current/rollback/active reader/미완료 backup에서 참조하는 generation을 삭제하지 않는다. 삭제 후 되돌릴 수 있다는 가정을 하지 않는다.
6. 각 정리 단계 전후 `df -B1`, Docker 수치, 삭제한 정확한 ID/경로 및 실제 회수 byte를 기록한다. 최신 free ≥ 최신 peak+reserve+다른 서비스의 관측된 동시 증가분을 확보해야 복구 배치를 시작한다. 전일 수치 기준 최소 약 9.22GB 추가가 필요하고, 실무상 15GB 이상 회수를 우선 목표로 삼되 최신 검사 통과가 최종 기준이다. inode도 확인한다.

### C. 예측 적정성 검증 및 필요한 보정 (P1)

1. DB ledger의 실제 row count/FTS/secondary-index binding 중복 여부를 exporter schema와 대조한다. 논리 크기·allocated bytes·추가 증가량·최대 동시 점유량을 구분하고 같은 파일을 중복 charge하는지 확인한다.
2. 기존 `test_capacity_v5_export_regression.py`의 실제 exporter 회귀를 먼저 활용한다. 대표 한국어 텍스트·긴 key·고유 token·overflow 경계·FTS/span/link cardinality로 예측/작업 DB/VACUUM 목적지/sealed 크기를 비교한다. 임의 대형 corpus 재생성이나 유료 재임베딩은 필요 없다.
3. 전일 generation 실측 4.91GB 대비 예측 15.34GB를 calibration 기준 하나로 사용하되 단일 정상 corpus에만 맞추지 않는다. DB×4 완화는 작업 DB·VACUUM 실패 경로의 하드 cap/쓰기 중 free 재검사 등 동등한 보호를 함께 증명할 때만 허용한다. 기존 768B row/FTS×4/peak×4 계수의 각 근거와 보정 여부를 REPORT에 남긴다.
4. state/serving/MCP가 같은 host filesystem에 있는지 actual mounts로 확인한다. export 후 local publication 복사, MCP `.incoming`/activation, retention, 실패 임시파일과 backup staging의 겹치는 peak도 계산한다. 기존 prediction이 모든 게시 peak를 포함한다고 가정하지 않는다.
5. 시작 시 2GiB gate는 단순 최소 기동 조건임을 로그/문서에 표시한다. 직전 성공 run 크기로 사전 경고를 추가하고, 정확한 계획이 확정되는 현재 embedding 이전 gate는 유지한다. 과거 예측만으로 안전한 실행을 오탐 차단하지 않도록 경고와 hard gate를 구분한다.
6. 충분한 공간이 있으면 안전 기준을 유지한 채 복구한다. 필요량이 크다는 이유만으로 reserve/state/file limit을 0 또는 과대한 값으로 바꾸지 않는다.

### D. 초기검증 재시도·영속 이력 (P1)

1. 현재 metadata GET와 embedding POST는 이미 transient retry/backoff를 구현한다. 환경 증거는 attempts=12, base=1초, cap=60초. 실제 최신 설정과 요청 timeout을 확인하고 기존 재시도와 중첩되는 횟수/최악 총 시간을 계산한다.
2. 네트워크/408/425/429/5xx 등은 기존 retry를 재사용하고 매 attempt/다음 retry 시간/최종 소진을 기록한다. 잘못된 인증·설정·FP8·차원/model/tokenizer/cosine 등 결정적인 계약 위반은 자동 재시도하지 않는다.
3. metadata 갱신 등으로 회복 가능한 상태를 typed reason으로 입증했을 때만 초기검증 전체를 제한적으로 재시도한다. 10/14의 generic contract를 통째로 retryable로 바꾸지 않는다. 필요한 경우 총 2–3회와 전체 deadline을 명시하고 HTTP 12회 재시도와 곱해지는 폭증을 방지한다.
4. 재시도는 초기검증 범위에서 수행해 corpus/게시를 재실행하지 않는다. unconditional systemd Restart로 당일 capacity 실패나 영구 오류를 반복하지 않는다. 프로세스 재기동 정책이 필요하면 전용 exit 분류와 최대 횟수/시간을 둔다.
5. run 생성 전 startup attempt에도 correlation ID, phase, reason code, attempt/max, elapsed, retry 결정/다음 시각/최종 결과를 남긴다. `run_id`와 혼동하지 않는다. journal 영속 보존 설정을 확인하고 필요하면 private JSONL/atomic report를 제한된 크기·기간으로 보완한다. 저장/디스크 자체 실패 시 stderr/journal fallback을 유지한다.
6. 기존 worker lock-before-DB, busy, SIGTERM 취소·정리 계약을 보존한다. startup-check도 production 구현을 재사용하며 corpus/게시/백업을 시작하지 않는다. 종료 후 실패 요약은 운영자가 조회할 수 있게 하며 외부 알림 전송은 별도 사용자 지시가 있을 때만 수행한다.

### E. 추가 발견 문제 및 해결 (P1/P2)

- **신한·롯데 연속 degraded:** 전일 재기동과 당일 모두 신한 `issuer_origin_network`, 롯데 `issuer_source_invalid`, carry 1,578. adapter의 안전한 재현/응답 fixture로 origin retry와 parser/source 계약을 나눠 조사한다. carry는 성공 실행에서도 데이터 freshness가 갱신되지 않는 문제를 숨길 수 있으므로 issuer별 마지막 정상 수집 시각·carry 수·연속 실패를 보고한다. 발견만으로 전체 adapter 교체를 시작하지 않는다.
- **서빙 freshness:** MCP healthy는 새 배치 성공의 증거가 아니다. current generation 시각/마지막 성공 batch/최신 실패를 함께 보여주고 10/15 미게시 상태를 운영 요약에 반영한다.
- **보존·GC:** MCP 3개 generation과 serving 2개 보존 코드, Worker state 보존 정책의 차이를 확인한다. `local_publisher.py`에서 retention/staging 삭제 오류를 suppress하는 부분은 안전한 실패 로그/회수량을 추가할 후보. 삭제가 실패해도 성공처럼 보이지 않게 하고 reader/lock/rollback 불변성을 유지한다.
- **백업 deferred:** 전일 성공 JSON은 `backup_status=deferred`, 업로드 0, 마지막 backup `2026-10-12T01:48:50.064632+00:00`, runs_since=2. 현재 정책에 따른 정상 유예인지 backlog인지 확인하고 미보호 generation을 GC에서 보호한다. 이 값만으로 백업 장애로 확정하거나 즉시 전량 업로드하지 않는다.
- **orphan 경고:** 예약 실행마다 기존 증거 컨테이너 3개가 경고를 낸다. 실패 원인은 아니다. 필요한 로그/inspect를 private 기록으로 보존한 뒤 개별 정리 또는 별도 Compose project 관리로 경고를 줄인다. `--remove-orphans` 일괄 적용으로 증거를 먼저 없애지 않는다.

## 6. 주요 파일 및 검증

| 파일 | 확인/수정 목적 |
|---|---|
| `apps/cardrag-worker/src/cardrag_worker/cli.py` | 안전한 typed reason 보존, 초기화 attempt/종료 이력, startup-check |
| `apps/cardrag-worker/src/cardrag_worker/embedding_v5.py` | 기존 HTTP retry와 metadata/응답/품질 계약 세분화 |
| `apps/cardrag-worker/src/cardrag_worker/capacity_v5.py` | 예측/상한/잔여공간 gate와 구조화 거절 근거 |
| `apps/cardrag-worker/src/cardrag_worker/pipeline.py` | 용량 실패 근거 유실 방지, 실제 ledger·run 실패 보고 |
| `apps/cardrag-worker/src/cardrag_worker/exporter_v5.py` | 작업 DB·VACUUM·sealed 실제 cap/peak 검증 |
| `apps/cardrag-worker/src/cardrag_worker/local_publisher.py` | 복사 peak, retention/staging 정리 관측 |
| Worker `settings.py`, `deploy/worker/compose.yaml`, systemd unit | retry budget와 운영 설정 정합성 |
| `apps/cardrag-mcp/src/cardrag_mcp/` 관련 storage/lifecycle 구현 | 실제 generation retention/reader/GC 보호; 파일은 구현 전 탐색 |
| `docs/OPERATIONS.md` | 진단/정리/복구 명령 및 최소 기동 공간과 배치 공간 차이 |

수정 내용에 맞는 기존 테스트부터 실행한다:

```sh
uv run --no-sync pytest -q \
  apps/cardrag-worker/tests/test_cli_settings_provider.py \
  apps/cardrag-worker/tests/test_embedding_v5.py \
  apps/cardrag-worker/tests/test_capacity_v5.py \
  apps/cardrag-worker/tests/test_capacity_v5_export_regression.py \
  apps/cardrag-worker/tests/test_exporter_v5.py
```

첫 실행은 관련 `-k` 선택으로 좁혀도 된다. 수정 범위에 따라 pipeline/local publisher/MCP lifecycle 및 runtime contract 테스트를 추가하고 기존 lint/type 검사 스크립트를 따른다. 의미 없는 문서 전용 테스트는 추가하지 않는다.

필수 검증: transient 성공/소진·영구 오류 즉시 중단·계약 reason 보존·전체 retry deadline·취소/busy/lock·비밀정보 미노출, 용량별 거절 사유·정확 경계·overflow·실제 export 예측/하드 cap·VACUUM 실패·복사 중 공간 부족 시 current 보존. 정리 검증은 실제 byte 전후 비교 및 보호 자산/ready 확인으로 한다.

이번 Planner는 제품 테스트/유료 API probe/full batch/build/cleanup을 실행하지 않았다. 근거는 journal·현재 소스/과거 보고서·Git·df/du·Docker inventory·MCP 파일 stat이며, 실제 export peak와 당일 거절 하위 gate는 Executor 검증 항목이다.

## 7. 배포·복구·완료 기준

1. REPORT에 실제 조사된 하위 원인과 미확정 사항을 구분하고, 삭제 ID/회수량/잔여 공간/예측 보정 근거 및 모든 명령·결과를 기록한다.
2. 안전한 개발 잔재 정리를 실제 수행한다. 목표 공간을 못 채웠으면 확보 완료로 표시하지 말고 부족 byte와 남은 조치를 보고한다.
3. 코드 변경 시 필요한 Worker 이미지 한 번만 빌드하고 운영 source/image SHA를 검증한다. MCP 변경이 없으면 MCP를 재빌드하지 않는다. 빌드 자체의 공간 소모도 확보량에 반영한다.
4. 현재 helper/env/overlays/volumes로 startup-check 한 번을 실행해 새 진단·초기검증을 확인한다. 실패 시 분류에 따라 처리하고 무조건 반복하지 않는다.
5. 실제 mount에서 최신 용량 gate가 통과하고 다른 writer가 없을 때만 복구 배치를 1회 실행한다. journal 또는 명명 컨테이너로 증거를 보존하고 monitor 명령을 REPORT에 제공한다. 길게 실행되는 동안 계속 폴링하지 않는다. 초기화 통과와 전체 완료를 구분해 보고한다.
6. 완료 후 run/status/generation·각 issuer carry/freshness·OCR/embedding 캐시 재사용·게시/MCP current/ready·backup pending·용량 및 잔재를 확인한다. 검증된 rollback과 다음 03시 timer를 유지한다.
7. 초기검증 실패가 구체 reason/attempt/복구 결정으로 남고, 재시도가 bounded이며 결정적 품질 실패가 통과되지 않아야 한다. 용량 거절도 수치와 하위 reason으로 재현 가능해야 한다.
8. 개발 검증/배포 준비/실운영 batch 완료 상태를 각각 명시한다. Planner 완료나 startup-check 성공을 운영 복구 완료로 대체하지 않는다. 기존 PLAN을 수정하지 말고 실행 후 이 디렉터리에 최초 `REPORT.md`를 작성한다. 필요한 correction은 `FIX_01.md`부터 handoff 절차를 따른다.
