# 016 Executor REPORT — 10/16 반복 임베딩 품질 검증 실패 진단·복구

- 작성: 2026-10-16, Executor (OpenCode). 기준 PLAN: 본 디렉터리 `PLAN.md` (Planner Codex, 2026-10-16 05:17 KST).
- 결과 요약: **임시(intermittent) provider 품질 변동으로 재현·분류 완료.** 신규 계측이 05:38 시도에서 실패를 그 자리에서 수치로 확정 기록(minimum_cosine 0.9972087, sample 0/24 단독 미달, threshold 0.999 유지). 로컬 계산 결함은 fixture로 배제. 품질 gate·threshold·provider pinning 무변경, 자동 재시도 미구현(C.2 근거 불충분). 운영 이미지 `cardrag-worker:016-afaef39` 1회 빌드·반영, startup gate 통과 시점에 복구 배치 1회 **성공·게시·백업 flush 완료**(run `9270ff0ac2584c979b8b609137656ff7`, generation `g-9270ff0ac2584c979b8b6091-f2dacc76549c`, MCP current 전환, pending 0).
- 한 줄 주의: DeepInfra 반복 gate 실패 확률은 관측 5회 중 2회(40%)로 계속 남으며, 실패는 이제 run 생성 전 ~20초에 수치와 함께 즉시 중단된다(fail-fast). 03:00 실패 재발 시 운영 재검증 절차는 docs/OPERATIONS.md §반복 임베딩 품질 실패 대응.

## 1. 기준점·원본 증거 (A.1)

- 시작 HEAD `652924c`, 운영 `cardrag-worker:015-741cfe0`, current symlink/env/compose 조합 PLAN §2와 동일 재확인. 실행 중 Writer 없음, timer active, free 89,813,860,352 B(당시).
- private evidence(`/opt/cardrag/016-evidence/operations/`)에 보존: 당일 journal(4,329 B), systemd Invocation `3c964a38aeb242b47938…` 확인, correlation별 startup-attempt-history 전 line, backup ledger read-only + `backup status` read-only JSON, 이미지 baseline 목록, 빌드/정리/배포 로그, 실패 시도 stdout 전문(`evidence-20261016/recovery-attempt-1-failed.log`, 45 KB — quality 로그+최종 JSON 포함), 성공 run의 DB/publish/manifest 통합 증거(`run-db-state-9270.txt`), deployment.json.
- 공개 문서에는 식별자·수치·분류만 기록. env/토큰/응답 원문/벡터 원본 불용.

## 2. 진단 결과 (A.2–B.5): 원인 분류와 수치

### 2.1 관측 이력(같은 운영 경로·설정)

| 시각(KST) | 경로 | 결과 | repeat cosine 계측 |
|---|---|---|---|
| 10/16 03:00:20 | systemd 예약 run | **FAIL** `repeat_cosine_below_gate`(deepinfra, attempt 1/2, 5.192s, terminal 정상) | 미기록(016 계측 이전 코드 — 사후 수치는 존재하지 않으며 조작하지 않음) |
| 05:36 | startup-check(B.2 1회) | **PASS** | 최소 1.000000 / cross 0.995519 |
| 05:38 | recovery 배치 기동 시도로 gate | **FAIL** — 신규 계측이 즉시 확정 | **minimum 0.9972087**, mean 0.9998837, failed 1/24, **index 0**만 미달, sample0 input sha `0c692a4b22993dbb…9446`, endpoint metadata sha `06844e4ab7258870…d045`, 4.909s, run 미생성·exit 1 |
| 05:39 | 추가 probe 1회(B.3, 가설=임시 변동) | **PASS** | 최소 1.000000 / cross 0.995548 |
| 05:41–07:33 | recovery 배치(게이트 통과 후 full) | **SUCCESS** | preflight 통과, 전체 run 성공(§5) |

### 2.2 분류

- **provider 측 per-call 반복 응답 변동(intermittent, deepinfra)**: 동일 코드·동일 이미지·동일 설정이 3분 차이에 통과/실패/통과. 편차 크기는 cos 0.9972(기준 -1.8e-3, θ≈4.2°)로 '완전히 다른 벡터'가 아닌 single-sample 미량 변동. `failed_sample_count=1/24` 단독.
- **로컬 수치/계약/재바인딩 결함은 배제**: 오프라인 fixture 회귀(B.1 — index rebinding·역순 data·경계 `<` semantics·norm/finite/dimension·단일 sample 변동) 전부 통과, 동일 컨테이너가 05:39 통과. `_repeat_gate_failures` seam로 정확 경계(=threshold) 허용·미달 거절 회귀 고정.
- remaining gate는 의도된 비재시도 경로를 그대로 유지(PLAN §4 '재시도 기능 고장 아님' 확인: attempt=1/2, decision=terminal, retry_eligible=false).
- 한계: 03:00 실패의 실제 수치는 영구 미확정(계측 도입 전). 05:38 수치는 사후 재계산이 아니라 신규 코드 실행에서 확보된 원측정. DeepInfra가 선행 검사라 Nebius 비교는 실패 시도에 도달하지 못했음(§4와 동일 해석).

### 2.3 구현된 진단 보완(A.2–A.4)

- `embedding_v5.py`: `EmbeddingV5Error`에 `details` — snake-key ≤16, int |None| 유한 float(≤1e18) | 64hex/short-identifier 문자열 ≤8만 허용(NaN/inf/초대형 int/임의 문자열/dict/list/100+ keys sanitize로_DROP). repeat gate가 `_repeat_gate_failures` seam 사용, 거절 시 gate_threshold/minimum/mean/failed_count/sample_count/first·lowest index/두 입력 SHA256/profile endpoint metadata SHA256 기록. 임계값 0.999 기본 불변.
- `cli.py`: 분류/로그(`_classified_failure_fields`가 quality_details 수치도 렌더)/`quality_details`(최종 JSON)/`StartupAttemptRecorder`의 terminal line에 `quality_details`·`retry_eligible` 보존. HTTP 재시도 계층 미적용(null status/attempts)과 stage attempt(`attempt=1/2`)·retry 결정을 로그 문구로 구분 명시키화. 결정적 품질 실패는 allowlist 밖이므로 1회 후 terminal — 동작·사유 그대로.
- 문서는 §7.

## 3. C — 재시도/복구 정책: 미변경 유지(근거 불충분)

- C.2의 자동 재검증 도입 조건(재현 근거)은 "임시 변동" 수준에서 충족됐으나, 관측 변동률(≈40%/attempt)에서는 1회 재검증도 재실패 확률이 높아 실패 은폐 리스크가 크고, 전체 sample×2 라운드를 재실행해 통과본만 취하는 편향 위험(PLAN 금지)과 직결. **자동 재시도 코드 변경 없음**, threshold/provider 무변경. 대신 결정적 개선은 fail-fast + 수치 확정(20초 내 종료, run 미생성, 47분 배치 소모 방지)과 운영 재검증 절차 문서화로 달성.
- C.3 요약 절차: `docs/OPERATIONS.md` — 'quality 실패 → writer 확인 → startup-check 1회 → 통과 시 배치/재실패 시 quality_details 증거로 보고, 반복 금지' + 미갱신 상태 한눈 조회 명령 묶음.

## 4. D — 빌드·배포·검증·복구

- 커밋 `afaef39 feat(worker): structured measurements for repeat-cosine quality gate failures`(5 files). push/PR/tag 없음(P-4).
- 테스트: `ruff check`/`ruff format --check` clean, `mypy` strict 108 files clean, 전체 pytest **2,535 passed**. 신규 회귀: gate 수치/SHA/경계 4건(test_embedding_v5), quality terminal 무재시도·details·비노출·history(test_cli_settings_provider) + 기존 015 회귀 전부 유지.
- 빌드 1회: `cardrag-worker:016-afaef39`(ID `sha256:0f4360aa…`), OCI label revision = HEAD full SHA 일치, 이미지 설치 소스 SHA 5종과 repo 파일 해시 일치(`deployment-016-afaef39/`) — FIX_02 식별 요구 재사용.
- 배포: `compose.secrets.yaml` image 1줄만 015-741cfe0→016-afaef39(before-copy 보존), `config --quiet` 통과. rollback: worker `015-741cfe0` + MCP `013-50a0129` + serving 이전 generation `g-ec1255d9`(볼륨 내 14.3GB 유지) 보존.
- startup-check: B.2 1회(05:36 통과) + B.3 probe 1회(05:39 통과). recovery는 게이트 통과 관측 시점 05:41에 1회 실행 → 07:33 exit 0.

## 5. 복구 배치 결과(D.5, run `9270ff0a…`)

- status succeeded, elapsed 6,709.8s, published=true, publish row ready, generation `g-9270ff0ac2584c979b8b6091-f2dacc76549c`, MCP `current.json` 전환 확인, `/health/ready` `{"ready":true}`.
- 용량: gate 통과 — required 73,724,403,360 B vs free 78,939,230,208 B(margin 5.2GB), pre-export recheck 통과, 관측 파일 갱신. views 617,139(+664 vs 어제)/unique miss 432/OCR 5,524 성공·failed 0·cache reused 5,521·provider documents 신규 3·pdf_downloads 0.
- sealed 실측: DB 4,916,875,264 B(예측 15,356,215,296 = 3.12배 유지), sidecar 예측=실측 일치 — 015 C 검증 재확인.
- collection **degraded**: shinhan `issuer_origin_network` **연속 12**(last_ok 10-05, carry 931), lotte `issuer_origin_timeout` **연속 4**(last_ok 10-12, carry 647; today reason 변경: origin timeout — 기존 source_invalid와 구분).
- backup: `last_backup_at=2026-10-16T07:32:36KST`, pending_count/bytes 0, lost 0, runs_since 0 → immediate flush 성공. 10/16 데이터 게시·백업·서빙 모두 갱신.
- timer active(다음 **10/17 03:00 KST**). 초기화 통과/전체 완료/degraded 수집/백업 결과를 각각 구분 보고.

## 6. 디스크·retention 조치(빌드 증가분 반영, B.6 준용)

| 시점 | free | 사건 |
|---|---|---|
| 05:30 | 89.8 GB | baseline |
| 빌드 후 | 80.1 GB | 016 이미지+cache(+~9.7GB) |
| 07:35(정리1) | 75.5 GB | 014/015-c635353·grype/syft 이미지 삭제, builder prune 3.65GB, startup 증거 컨테이너(inspect+log 보존 후) 삭제 |
| 07:36(정리2) | **109.5 GB** | `runs/ec1255d9…`(18.2GB) 삭제(P-3) — reports tar 아카이브, run/status·PUB·manifest 해시 DB/볼륨 유지 |
| 게이트 여유 | 109.5 − gate-전 증가(~2GB) − required 73.7 | **≈ 33 GB** ✓ 10/17 gate 통과 여력 확보(다만 gate 실패 확률 자체는 §3 변동률과 무관) |

`rm -rf /var/lib/docker`, `prune -a --volumes`, 운영 volume 미접촉, 타 프로젝트 이미지 무건드림. serving 볼륨은 retention=2 유지(g-9270ff0a + g-ec1255d9), MCP state 3세대 42.9GB는 §7 R-2로 유지(본 라운드 불필요·미검증 삭제 금지).

## 7. 편차·잔여 위험·후속

편차:
- **P-1** recovery 시도는 'corpus에 진입한 배치 1회'로 해석: 05:38 시도는 startup gate에서 run 생성 전 종료(배치 미시작), 실제 full batch는 05:41 단일 시도. PLAN D.3 통과 조건 문구에 부합하며, 두 번째 startup 실패 시 3번째 기동은 하지 않는 규칙으로자기 제한.
- **P-2** 015 ACCEPTANCE 승인 이후 2-generation 이미지(grype/syft 포함)·startup 증거 컨테이너 삭제(로그·inspect 보존) — 015 round-2와 동일 관행.
- **P-3** retention floor 이전의 successful publication run 디렉터리(ec1255d9) 수동 삭제: native cleanup은 다음 성공 run에서만 3rd position을 회수하므로 오늘 07:35 시점 gate 여유 확보 불가(자기 시작 데드록 반복 회피). reports tar 아카이브 + rollback 경로(serving generation 볼륨 사본·objects·MCP state) 완전 무결 확인. 다음 run의 자동 회수는 이미 소진된 상태.
- **P-4** push·release 없음(로컬 commit 2개만). 커밋 이력: `afaef39` 구현 + 본 handoff docs 커밋(015 ACCEPTANCE/RUNTIME_REVIEW untracked 2종 및 016 PLAN 포함 보존 커밋, §2 지침).
- **P-5** 성공 run의 stdout 컨테이너 로그: 컨테이너 삭제 전 파일화하지 못해 유실. run DB·PUB·performance/issuer-collection 보고서·manifest 해시·backup ledger로 동일 사실을 재구성·보존(`run-db-state-9270.txt`). 실패 시도(05:38) stdout은 온전 보존. 재발 방지: 삭제 전 `docker logs > evidence` 고정 순서.

잔여(권장 우선순위):
1. **R-1 DeepInfra 반복 변동 40% 관측**: gate 자체는 유지. 재발 시 terminal 로그의 quality_details를 sample SHA·metadata hash와 함께 증거로 축적(동일 sha `0c692a4b…` 반복이면 provider 제보 팩). 03:00 gate 실패는 배치를 소모하지 않으므로 운영 리스크는 '미갱신 일수'뿐 — 자동 재검증 도입은 변동률 관측 축적 후 reviewer 결정.
2. **R-2 MCP state 3세대(42.9GB) 검증된 GC** — 여력 확보로 급하지 않으나 유지 제안(reader/backup 포괄 확인 후).
3. **R-3 issuer 연속 실패**: shinhan 12(last_ok 10-05!)/lotte 4(오늘 reason=`issuer_origin_timeout`으로 변화). 어댑터 교체 없이 관측만 갱신됨.
4. **R-4 run evidence 구조**: 배치마다 publication run 18GB가 생성되고 gate가 host free 73.7GB를 요구 — 회전 주기당 수동 회수가 반복되는 구조. retention 기본값(2)·run evidence archive 정책 검토를 FIX/후속 PLAN에서 다룰 것을 제안.
5. R-5 03:00 실패 수치 영구 미확정(2.2).

## 8. 재현·관측 명령

```bash
# gate 실패/통과 이력과 수치
journalctl -u cardrag-worker.service --since '2026-10-16 00:00:00' --no-pager | grep -E 'Startup stage (failed|retry decision)|reason_code=repeat'
docker run --rm -v cardrag-worker-state:/s:ro --entrypoint cat cardrag-worker:016-afaef39 /s/startup-attempt-history.jsonl | tail -4
# 상태 한눈 요약
systemctl is-active cardrag-worker.timer; systemctl status cardrag-worker.service --no-pager | head -4
curl -fsS http://127.0.0.1:18015/health/ready
docker run --rm -v cardrag-mcp-state:/s:ro --entrypoint cat cardrag-worker:016-afaef39 /s/current.json
docker run --rm --user 10001:10001 -v cardrag-worker-state:/s:ro --entrypoint cat cardrag-worker:016-afaef39 /s/capacity-observation.json
# quality 재검증(유료 96-call 고정 예산, 1회만)
/opt/cardrag/013-50a0129/operations/worker-compose.sh run --rm --no-deps worker startup-check
```

증거 루트: `/opt/cardrag/016-evidence/operations/{evidence-20261016,deployment-016-afaef39}`.
