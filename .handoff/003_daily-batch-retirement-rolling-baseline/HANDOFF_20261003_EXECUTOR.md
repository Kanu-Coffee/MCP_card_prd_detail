# HANDOFF — 003 Executor 작업 전체 인계 (2026-10-03 12:00 KST 기준)

작성자: Executor(Codex) 세션. 수신자: 새 세션의 Planner/Executor/Reviewer.
이 문서는 `.handoff/003_.../PLAN.md`(Planner), `REPORT.md`(Executor 1차), 본 파일 이전의
`evidence/*` 를 대체하지 않는다. **append-only 규약에 따라 기존 산출물은 수정·삭제하지 않았다.**

---

## 0. 한 장 요약

| 항목 | 상태 |
|---|---|
| 003 원 목표(롤링 baseline + 정당한 은퇴 분류) | **코드·검증 완료**, candidate 런 `c622d3c4`에서 실증(9/29 이후 candidate 채널 generation 발행, baseline/retirement 봉인) |
| 프로덕션 상태 DB(v129) | **파괴됨(복구 불가 판단)**. 원인·보존 증거 아래 §4-1 |
| 운영 worker.env | patch7 + codex-exec/qwen3.8-flash + `OCR_CACHE_MODE=read-write` + `PUBLICATION_APPROVED=true` + API 키 1줄(640 root:cardrag). **아직 v129 지정(승격 미적용)** |
| 타이머 | `cardrag-worker.timer` **stopped**(사용자가 정지). active로 되돌리면 손상 v129를 연다 |
| MCP 서빙 | 무중단. `cardrag-stable-v1026-mcp-1` healthy, `/health/ready` 200, stable pointer = 9/29 `g-5f74295db4b64f10837eba3e-a102228926a6` |
| 진행 중 | `cardrag-prod-promote-4`(patch11, 새 런) 11:51 기동, 11:56 discovery 4/8 통과. ETA ~14:30± |
| 디스크 | free 91G (buildx prune 21G 회수 후). V5 capacity preflight는 `peak_growth_bytes≈71.2GB` 필요 → 70G까지 내려가면 실패하므로 모니터링 필요 |
| 재발 방지 코드 | 4건 커밋(§2). 전체 게이트 green(2,267 tests / ruff / format / mypy) |
| 승인 대기 | main 병합·tag, stable 승격 블록(§7), v129 볼륨 처분(§8) |

---

## 1. 저장소·브랜치·런타임 좌표

- repo `/home/lee/projects/MCP_card_prd_detail`, branch `feature/003-retirement-rolling-baseline`(upstream 설정됨, push 완료)
- 배포 트리는 `/opt/cardrag/current → /opt/cardrag/v1.0.29` (git 무관, compose 사본). `deploy/worker/compose.yaml`은 저장소와 동기화됨(CODEX provider 4종 + bare `ALIBABA_TOKEN_PLAN_API_KEY` 패스스루 존재)
- systemd: `/etc/systemd/system/cardrag-worker.service`(User=cardrag, SupplementaryGroups=docker, EnvironmentFile=/etc/cardrag/worker.env, ExecStartPre=config --quiet, ExecStart=`compose run --rm worker run`, TimeoutStopSec=infinity, SuccessExitStatus=130 143) + `cardrag-worker.timer`(03:00 KST daily). 저장소 버전과 Documentation 줄만 상이
- 볼륨: `cardrag-worker-v129-state`(prod, DB 파괴), `cardrag-worker-v130-candidate-state`(46G, **승격 후보, DB healthy**), `cardrag-v129-corrupt-state-20261003`(손상 DB 보존본), `cardrag-worker-v120-recovery-auth-20260910`(codex auth), `cardrag-worker-paddleocr-models`, `cardrag-v131-probe-state`(빈 probe 용량, 정리 대상)
- 이미지(ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate): patch2 `b42dfb61…`(구 prod), patch7 `a9fa96eb…`, patch8 `5703b2ab…`, patch9 `8eef9cbb…`, patch10 `9c814074…`, **patch11 `927195723726377b81420ccb5f52d75955ed42b4c48c675913fbfa1db0dc46be`(최신)**, tag `v1.0.29-patch11`
- 이미지 빌드/배포 recipe:
  `docker buildx build --target worker --platform linux/amd64 --build-arg SOURCE_URL=https://github.com/kanu-coffee/MCP_card_prd_detail -t <ref>:v1.0.29-patchN --load .` → `docker push` → 다이제스트는 `docker buildx imagetools inspect <ref> | grep -i ^Digest`
- 런칭 recipe(볼륨·채널·GC·이미지는 셸 env로 오버라이드; compose 보간은 셸 env가 --env-file보다 우선):
```
cd /opt/cardrag/current && set -a && . /home/lee/.codex/alibaba-token-plan.env && set +a && \
export CARDRAG_WORKER_STATE_VOLUME=cardrag-worker-v130-candidate-state CARDRAG_CHANNEL=stable \
       CARDRAG_REMOTE_GC_APPROVED=false CARDRAG_COLLECT_REMOTE_GARBAGE=false \
       CARDRAG_WORKER_IMAGE=ghcr.io/...@sha256:92719572... && \
docker compose --env-file /etc/cardrag/worker.env -f deploy/worker/compose.yaml \
  -f deploy/worker/compose.secrets.yaml run -d --name <이름> worker run   # 또는 `worker resume <run_id>`
```
- 워처 recipe(`docker wait` → evidence + flag. 세션 폴링이 불안정할 때 유용): 스크립트를 `/tmp/…-watcher.sh`에 쓰고 `setsid … &`로 detach, 종료 시 `evidence/<name>-watch.out`, `<name>-full.log`, `/tmp/<name>-complete.flag` 생성

## 2. 커밋 인벤토리 (이 라운드, 모두 push)

| SHA | 내용 |
|---|---|
| `c529729` | codex OCR model-provider 주입(`CARDRAG_CODEX_MODEL_PROVIDER{,_BASE_URL,_ENV_KEY,_WIRE_API}`, `CARDRAG_CODEX_MODEL_CATALOG_JSON`). Alibaba token-plan(qwen3.8-flash) 환경에서 env-only 전환이 불가능해 코드화 |
| `4f5566f` | patch4: run-dir에 자체 ocr.md가 있으면 partner fallback bind 생략 |
| `35d05f2` | patch5: `_retained_identity_conflict()` — 자체 파일 또는 seed-ledger seal이 candidate seal과 다르면 bind skip |
| `0f59ddb` | patch6: 남은 publication guard 2곳을 `_observed_pointer_bytes()`로 정규화(회수된 dangling pointer → absent) |
| `9da9669` | patch7: hana 커서 드리프트(교차페이지 단일 row 재현) dedupe, 나머지는 fail-closed 유지 |
| `58b50b4` | 003 REPORT.md + run-3r12 증거 |
| `886a627` | 승격 런 실패 + qwen OCR divergence(114건) 증거 |
| `b252aa2` | **SQLite 열기 전 worker lock 프로브**(§4-1 사고의 재발 방지) + 회귀 테스트 |
| `a58ca9d` | **R2 1차**: retained generation seal 우선 계약(원격 변형 거부, 발행 충돌 스킵, 파이프라인 가드는 최후 트리프와어로 유지) |
| `a406ee9` | **R2 2차**: 거부 지점을 CAS 다운로드·materialize **이전**으로 이동(요청 시 관찰 경고) |
| `dc76d49` | **R2 3차(현행)**: 발행 충돌 처리부의 adoption 조회에도 identity 전달 → 거부된 원격 변형이 런 디렉터리에 기록되는 것 차단. 스킵 범위는 "retained seal 존재 시"로 한정 |

게이트(전 커밋 공통): `pytest -q` **2,267 passed**, `ruff check`/`ruff format --check`(CI 대상 집합) clean, `mypy`(core+worker+mcp src 95 files) clean, `git diff --check` clean.

## 3. 검증된 운영 팩트 (재확인 불필요하게 수치로)

- hana discovery는 patch7 이후 **724 records / warnings=0**로 3회(promote-1/2/3) 및 prod patch7 런에서 반복 관측. `IssuerMarkupChanged` 재발 0. live probe 근거: 73p/726row 중 정확히 1건 cross-page replay(page66→67, product 13773), `listCount=725`.
- candidate run `c622d3c4…`: status succeeded, generation `g-c622d3c4b1fb4df5a74a4b13-e95cb9ce7d7f`, documents 5,499, evidence 614,909, OCR 5,499/5,499 failed=0, provider 호출 1, export coverage 100%, publish `ready`, baseline `audit-reports/corpus-baseline/949a65f85d…json`(current 5,059 / historical 440), retirement ledger entries 9(reinstated 8 / retired 1), quick_check ok, embedding_cache_v5 396,213행, stage 미완은 download retry 2건(모두 skipped 형제 있는 unsupported 문서).
- prod 수동 런 `d9de4b24…`(patch7): discovery 8/8(hana 724), PDF 5,066(hits 4,838/misses 228/dl 212), **OCR 5,500/5,500 failed=0**, 신규 provider 호출 96 = 95 중복 + 1 legit, 03:01:58 embed_views에서 sqlite I/O 오류로 사망.
- `restore-ocr-seed` dry-run(stable generation `g-5f74295d…`) 실측: `status=verified`, exit 0, documents 5,213, unique CAS 4,928, 전송 71.4MB, `unbound_cache_documents 21` → 무상태 볼륨 재구성은 기술적으로 가능(OCR 0호출 시작).
- 승격 후보 볼륨의 로컬 실링 바인딩: `Discovered 3994 reusable local OCR artifacts from prior run c622d3c4… (remaining unbound: 1515)` (promote-1/3/4 공통).

## 4. 사고 타임라인 (원인 → 증거 → 조치)

### 4-1. 10-03 03:02 프로덕션 상태 DB 파괴 (복구 불가)
- 증상: `sqlite3.OperationalError: disk I/O error`(embed_views 읽기). 현재 `worker-state.sqlite3` 6,892,044,288B, page1 magic 유실(`0a00000008008000…`), `-wal/-shm` 없음, 샘플 페이지 94%가 all-zero → 스키마(sqlite_master는 page1) 소실.
- 원인: **락 거부 프로세스가 SQLite를 먼저 열었다**. `cli.py:_run()` preflight가 `WorkerState(state_database)`를 열고(`cli.py:388` 영역), 락은 `pipeline.py:2369 _run_locked`에서 뒤늦게 획득. 03:00:00 타이머 컨테이너가 preflight(capacity→OCR→embedding, 30s) 수행 후 03:00:32 `worker_busy`(already_running)로 종료 → 실제 writer는 86초 후 I/O 오류. 저널: `evidence/incident-20261003-timer-journal.txt`.
- Executor 판정 오류 기록: "락 거부는 무해"라고 두 번 판단했고 둘 다 틀렸다.
- 보존: 손상 DB 원본 무변경, 격리 복제 `cardrag-v129-corrupt-state-20261003`/`worker-state.sqlite3.corrupt-20261003T0302`, **SHA-256 `199b9e1ad1616d5cb578ec64ada859a19b9762c1c4c965b7be7022b988ccf051`**.
- 조치: `b252aa2`(락 프로브, patch8) + 회귀 테스트 `test_run_probes_worker_lock_before_opening_state_database`. 문서 자산(runs 37G/pdf-cache 4.2G/ocr-seed 121M/audit-reports 7.2M)과 MCP 서빙은 무영향.

### 4-2. 10-03 08:18 승격 런(promote-1) 시스템적 중단
- `doc_8356d2d2…`(bc, 2p)에서 `ocr_cache_healing_identity_mismatch`(pipeline.py:4009, retryable=false).
- 바이트 증거: 같은 `pdf_sha256`·같은 `reuse_key 15432bcb81af…`에 두 개의 봉인본 존재
  - candidate(c622d3c4, 9/30): `263410c5…` 18,691B — **retain된 세대 봉인**
  - prod block3(d9de4b24, 10/2): `a707b713…` 16,534B — block3-run1이 `read-write`+`PUBLICATION_APPROVED=true`로 **공용 WebDAV OCR CAS에 발행한 변형**
- 근본: **qwen3.8-flash OCR은 바이트 결정적이지 않다.** 두 볼륨 교차 비교 3,988건 중 **114건(2.9%) 상이**(list: `evidence/prod-vs-candidate-ocr-divergence.jsonl`).
- 사용자 결정(R2): "LLM 사용은 품질의 완전 보장을 내포한다. 힐링 가드의 의미를 변경하고 모델 사용을 유지한다." → §5 계약.

### 4-3. 10-03 10:48 promote-2(resume) 90건 실패
- `attempts exhausted reason_code=generic_validation_error` 90건, refusal 경고는 존재.
- 원인: 1차 R2는 **원격 본을 materialize한 뒤** 거부 → promote-1이 남긴 오염 런 디렉터리(06d89090)와 충돌, retained 재물질화 실패 → 프로바이더 재호출(최대 4회) → 실패. 즉 **오염된 런은 resume 대상이 아님**.
- 조치: `a406ee9`(사전 거부) + promote-2 정지(10:48, exit 143).

### 4-4. 10-03 11:37 promote-3(patch10, 새 런) 59건 실패
- 진단 파일 `native-cache-publication-diagnostic.json`(phase=manifest, error_kind=integrity, `artifact_sha256=263410c5…`=retained 봉인본)가 남고, 런 디렉터리엔 원격 변형(16,534B)이 기록됨.
- 원인: 발행 충돌 처리부의 **adoption 조회가 identity 없이 `_lookup_cache`를 재호출** → 거부된 변형을 내려받아 materialize → 커밋 후 `OCRValidationError: committed local native OCR identity is unavailable` → 문서 실패.
- 조치: `dc76d49`(patch11) — adoption 조회에 `expected_ocr_identity` 전달, 스킵 범위를 "retained seal 존재"로 한정(corrupt control·auth(407)·network·timeout·CAS-phase는 계속 fail-closed), 사전 거부 경고에 document/key 식별자 기록.

## 5. 현행 OCR 캐시 계약 (R2, 코드 수준)

- 식별: `reuse_key`는 입력 신원(PDF·계약·prompt·epoch 등)에서 유도되고 **출력 바이트를 포함하지 않는다** → 비대결정 LLM은 같은 키에 여러 본을 만들 수 있다.
- 우선순위(OCRResolver): ① seed ledger(단, retained seal과 다르면 **사전 거부+경고**) → ② 로컬(run-dir/index된 네이티브, identity 필터) → ③ `prior_local_native` 재물질화(`_materialize_prior_local_native`) → ④ 원격 캐시 조회(`_lookup_cache`에 `expected_ocr_identity` 전달, **불일치 시 다운로드 전 return None + 경고**) → ⑤ 프로바이더.
- 발행: `read-write`+승인 시 CAS→manifest→READY 발행. 충돌( phase manifest/ready)에서
  - winner가 유효하고 retained seal과 일치/정체 불명 → 채택(기존 first-writer 승인 동작),
  - **retained seal이 존재하는데 winner가 없거나 다르면** → 채택·덮어쓰기 금지, diagnostic 기록, 경고 `native OCR cache publication skipped because the remote entry already holds a different variant`, `cache_publication_deferred=True`로 계속 진행.
  - 그 외(CAS phase, network/timeout/http/auth, 검증 불가 control) → 기존 fail-closed.
- `pipeline.py:4009 OCRCacheHealingIdentityError`는 유지되나 이제 **최후의 트리프와어**(주석 명시).
- 잔여 오염: 공용 WebDAV에는 114개 키의 두 번째 변형이 남아 있다(삭제·수정 안 함). 매 런에서 "스킵"으로 관측되며 `cache_publication_deferred`/diagnostic가 감사 경로다.

## 6. 지금 진행 중인 것과 확인 체크리스트

`cardrag-prod-promote-4` = patch11, 새 런(run id: 로그 `during run <id>` 또는 `docker inspect`로 확인), 볼륨 v130, channel stable, 원격 GC off, cache read-write. 11:52 preflight 완료, 11:56 discovery woori/kb/shinhan/samsung warnings=0.

완료 후 반드시 확인:
1. `docker logs` 에 `attempts exhausted` **0**, `ERROR|Traceback` **0**, `refusing remote OCR cache variant before download` 경고 ≈114, `publication skipped …` 경고 ≈114, `OCR progress … failed=0`
2. 마지막 페이로드: `status:"succeeded"`, `generation_id`, `retired_count`, `retirement_candidate_count`, `pdf_cache_*`, `v5_metrics.source_coverage_percent: 100.0`, `ocr_provider_called_count`(≈0-2 기대), `ocr_cache_publication_deferred`(≈114 예상)
3. publish 테이블 `status=ready` + **stable pointer가 새 세대로 전진**(WebDAV `v1/channels/stable.json`), `audit-reports/corpus-baseline` 갱신 및 `corpus_sha256` 일치
4. MCP: `/health/ready` 200 유지 + 서빙 generation 전진 확인(최신 세대 동기화), `tools/list` 12
5. `df -h /` ≥ 20G 여유 유지 확인(V5 preflight `peak_growth_bytes≈71.2GB`)
6. `evidence/prod-promote-4-*` 커밋

## 7. 미적용 sudo 블록 (성공 확인 후에만)

`worker.env`는 현재 patch7·v129 지정이다. 승격 확정:
```bash
cat > /tmp/apply-cutover.sh <<'SCRIPT_END'
set -euo pipefail
TS=$(date +%Y%m%d%H%M%S)
cp -p /etc/cardrag/worker.env /etc/cardrag/worker.env.bak-$TS
sed -i \
 -e 's|^CARDRAG_WORKER_IMAGE=.*|CARDRAG_WORKER_IMAGE=ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate@sha256:927195723726377b81420ccb5f52d75955ed42b4c48c675913fbfa1db0dc46be|' \
 -e 's|^CARDRAG_WORKER_STATE_VOLUME=.*|CARDRAG_WORKER_STATE_VOLUME=cardrag-worker-v130-candidate-state|' \
 /etc/cardrag/worker.env
chown root:cardrag /etc/cardrag/worker.env; chmod 640 /etc/cardrag/worker.env
cd /opt/cardrag/current && docker compose --env-file /etc/cardrag/worker.env \
  -f deploy/worker/compose.yaml -f deploy/worker/compose.secrets.yaml config --quiet
echo CUTOVER_OK
SCRIPT_END
sudo bash /tmp/apply-cutover.sh
# 승인 의도에 따라: 원격 GC는 기본 true 유지(§8-3 참고), 타이머 재시작
sudo systemctl start cardrag-worker.timer
```
주의: 타이머는 promote-4가 **끝난 뒤에** 재시작해야 한다(동일 DB 동시 오픈 = §4-1 재발). patch11에는 락 프로브가 있어 거부 측은 DB를 열지 않지만, 원칙적으로 회피한다.

## 8. 잔여 과제·리스크 (새 plan의 입력)

1. **v129 처분**: 원본·보존본 모두 유지 중(합계 ~53G). 복구가 아니라 폐기 결정 + 삭제는 사용자 명시 승인 필요. 폐기 전 `docker run --rm -v cardrag-worker-v129-state:/p:ro …` 인벤토리 커밋 권장.
2. **qwen 결정성 정책**: 114/3,988(2.9%) 재현률. 선택지 (a) 현재 R2 유지(세대 봉인 정답화 + 스킵 감시) (b) 동일 문서 재-OCR 금지 정책 명문화 (c) 결정적 OCR(paddle) 회귀 — 사용자가 기각한 적 있음 (d) 세대 봉인과 다른 본을 **감사 리포트로 집계**하는 메트릭 신설(현재는 warning+diagnostic만). 
3. **원격 캐시 오염 정돈**: 114키의 두 번째 변형. 삭제는 `docs/RECOVERY.md`가 금지하므로, 필요 시 GC/pointer 재발행 경로로만 정돈. 또한 이번 promote-4에서 발행 스킵이 실제 동작하는지 확인(=원격에 retained 본이 새로 안 올라감 → 다른 볼륨이 같은 충돌을 반복할 수 있음).
4. **`CARDRAG_OCR_COMPATIBLE_MODELS`에 `qwen3.8-flash`가 없다** — 그런데도 qwen 실링이 재사용되는 중. 이 목록이 실제로 어떤 경로에 적용되는지(호환 contract 조회 vs 재사용 허용) 새 세션에서 코드 확인 필요. §5의 ④와 상호작용하므로 오픈 퀘스천.
5. **관측성 갭**: 실패 분류기가 원문을 버림(`OCRFailureBookkeepingError`의 `raise ... from None`, pipeline.py:4061 / `_classify_ocr_failure`의 generic 마스킹) → 이번 3연속 디버깅 비용의 직접 원인. `from exc` 또는 원인 필드화 권장. `warnings.warn`은 동일 메시지당 1회만 출력되므로(프로모션 3에서 1건만 보임) 카운터/메트릭이 필요.
6. **003 수용기준 상태**: candidate에서 2회 연속 성공(run-2 9/30 + 3r12 10/2) + lotte 8 은퇴 봉인 + missing_unjustified 0 + baseline 전진 + 무중단 모두 충족. **운영已连续 무인 2회 성공은 §4-1 사고로 미완** → promote-4 성공 + 10/4 03:00 타이머 성공으로 갈음 가능. 디스크 ≥80GB는 충족(91G).
7. **롤아웃 승인 대기**: main 병합/tag, patch11의 stable 승격(§7), 후보 볼륨 명칭 정리(v130-candidate-state가 prod 상태가 됨 →将来 v131 사본 승격 검토, 단 §3의 V5 용량 프리플라이트 때문에 디스크 여유 확인 필수).
8. **청소 후보**: `cardrag-v131-probe-state`(빈 볼륨), exited 후보 컨테이너 `cardrag-prod-promote-1/2/3`, `/tmp/*watcher.sh`·플래그, 저장소 밖 `deploy/worker/compose.v130debug.yaml`(삭제됨), image prune(20.7GB 회수 가능, 단 patch 다이제스트는 registry에 있어 안전).

## 9. 재현·진단 커맨드 (읽기 전용 중심)

```bash
# 상태/진행
docker ps -a --filter name=cardrag-prod --format '{{.Names}} {{.Status}}'
docker logs cardrag-prod-promote-4 2>&1 | grep -E "attempts exhausted|refusing remote|publication skipped|ERROR|Traceback" | wc -l
docker logs cardrag-prod-promote-4 2>&1 | grep 'OCR progress completed=' | tail -1
# 볼륨 DB (immutable read-only; -wal 없음)
docker run -i --rm -v cardrag-worker-v130-candidate-state:/c:ro python:3.13-slim python - <<'PY'
import sqlite3
c=sqlite3.connect('file:/c/worker-state.sqlite3?mode=ro&immutable=1',uri=True)
print(c.execute('pragma quick_check').fetchone())
print(c.execute('select run_id,status from run order by started_at desc limit 3').fetchall())
print(c.execute('select generation_id,status,published_at from publish order by rowid desc limit 2').fetchall())
print(c.execute("select stage_name,status,count(*) from stage where status!='succeeded' group by 1,2").fetchall())
PY
# OCR 실링 비교 (결정성 감사)
docker run -i --rm -v cardrag-worker-v129-state:/p:ro -v cardrag-worker-v130-candidate-state:/c:ro python:3.13-slim python - <<'PY'
# runs/<rid>/documents/*/ocr/native-manifest.json 의 output.sha256/size_bytes를 reuse_key로 조인
PY
# 이미지 다이제스트 / 디스크
docker buildx imagetools inspect ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate:v1.0.29-patch11 | grep -i ^Digest
df -BG / | tail -1
```
주의(이번 세션에서 실측한 함정):
- `sudo su` 후 실행하면 `$HOME=/root` → 사용자 키 파일 경로는 `/home/lee/.codex/alibaba-token-plan.env`(절대경로 사용).
- 다중 `EOF` 마커가 겹치는 heredoc은 조기 종결돼 본문이 셸로 실행될 수 있다(§REPORT §5 사고). 대용량 문서는 `python - <<'PY'` 단일 마커나 파일 경유.
- `write_stdin` 세션 id 폴링이 불안정 → 짧은 exec 반복 또는 워처 플래그 사용.
- 진행도 카운터는 세션 누적(스킵 포함)이라 잔여 작업량으로 읽으면 안 된다. 신규 호출 수는 native-manifest(created_at·model)로 실측.

## 10. 새 plan 초안 (Planner용 제안 — Executor가 작성한 입력 자료)

**가제 004: 프로덕션 상태 복구 승격과 LLM OCR 비대결정성 정책 봉인**
- Objective: (1) 승격된 v130 상태를 정식 prod로 확정하고 무인 daily 2회 연속 성공을 증명, (2) LLM OCR 비대결정성을 운영 계약(관찰 가능한 스킵 + 감사 리포트)으로 제도화, (3) 파기된 v129와 원격 캐시 잔류 변형의 처분 기준 확정.
- Scope: §7 적용 → §6 체크리스트 → 10/4 03:00 daily 관측 → §8-2/8-4/8-5 코드 항목(결정성 감사 메트릭, 관측성 갭, compatible-models 정책) → §8-7/8-8 롤아웃·청소.
- Constraints: 무중단(MCP 포인터/이름 불변, librechat 프록시 하드는 `cardrag-stable-v1026-mcp-1`), 수동 런과 타이머의 동일 DB 동시 오픈 금지, 기존 handoff 산출물 불변, 프로바이더 지출 상한(신규 OCR 0~2건 기대 이상이면 중단), 디스크 하한(≥80G 권장, V5 preflight 71.2G).
- Acceptance: promote-4 성공 지표 전체 충족 + stable generation 전진 + MCP 서빙 최신화 + daily 2회 연속 성공 + 결정성 감사 리포트 신설(114건 스키마) + 호환모델 목록 정책 결정 기록.
