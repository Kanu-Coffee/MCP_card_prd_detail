# FIX_03_REPORT — production daily batch + disk reclamation

- **작성일**: 2026-09-27 (KST)
- **작성자 역할**: Executor (Codex) / **root 작업**: 사용자 SSH 직접 실행(명령은 Executor 제공 블록)
- **대상 문서**: `FIX_03.md`
- **기준 트리**: `release/v1.0.29` (main 동일), 배포 트리 `/opt/cardrag/v1.0.29` @ `fdf87e6`

## 권한 경계(투명성)
lee(uid 1000, groups cardrag/docker) 비대화형 sudo 불가(인증 필요) → `/etc/cardrag` 수정·`systemctl` enable 은 사용자 SSH 실행으로 수행. 그 외(docker 볼륨/이미지/컨테이너, /opt/cardrag, 코드·증빙 커밋)는 본 Executor 가 수행.root 필요 명령은 재현 가능하도록 본 문서 §9 에 전문 기록.

## A1 — 전용 프로덕션 상태 볼륨 (code-seeded) ✅
- `docker volume create cardrag-worker-v129-state`
- 커밋된 코드 경로로만 시딩 (이미지 `80eb7aa2…`, source `cardrag-worker-v114-candidate-state` read-only bind):
  - `seed-state-v122 … --apply --expected-documents 5192` → accepted 5,192 / 4,710 CAS / 5,208 revisions / 5,203 sources, `idempotence_verified: true`, ledger `eeb7dea3…`
    (로그: `attestations/state-seed-v129-production.out`)
  - `seed-embedding-cache-v122 … --apply --expected-rows 381361` → 381,361 행, 멱등 0, ledger `69822cfaf7784aac90eaf51a83cbdfc5ba6cae9029c071fb355a24e7043f299d`
    (로그: `attestations/embedding-seed-v129-production.out`)
- 검증: v129 볼륨에 `audit-reports/state-seed/eeb7dea3….json` + `audit-reports/embedding-seed/69822cfa….json` + `ocr-seed` 5,192 문서 실재. state ledger 는 r3 봉인본과 내용 동일(경로해시 제외) — 제로손실 corpus-diff 게이트가 **활성**(seed_ledger 해석됨).
- **v114 baseline 무변경 확인**: `worker-state.sqlite3` mtime `2026-09-23T01:39:09Z` 유지(전 과정 read-only bind 만 사용).
- 선택: 신규 v129 생성+시딩 (r4 승격 아님 — r4 는 검증 전용이었고 B2 에 따라 회수 대상).

## A2 — stable 채널 전환 ✅ (사용자 root 실행 09:52)
Executor 가 읽기본 사본을 먼저 보관(`/opt/cardrag/v1.0.29/deployment/backups/`), 사용자 SSH 로 블록 #1 실행:
- 백업: `/etc/cardrag/worker.env.bak-stable-20260927T095209`, `mcp.env.bak-stable-…` (+ deployment/backups 사본)
- 적용: `CARDRAG_CHANNEL=stable`, `CARDRAG_STABLE_PUBLICATION_APPROVED=true`, `CARDRAG_WORKER_STATE_VOLUME=cardrag-worker-v129-state`
- 유지: REMOTE_GC=true+COLLECT=true, ENVIRONMENT=production, EXTERNAL_OCR_ALLOWED=false, local-paddleocr/PaddleOCR-VL-1.6, `CARDRAG_PDF_CACHE_FORCE_REVALIDATE` 미설정(TTL 168h 적용), WORKER_IMAGE=ghcr pinned `80eb7aa2…`
- 검증 출력: `WORKER_ENV_OK` + 렌더 `RENDERS_OK`(§9 명령 전문)

## A3 — 감독 엔드투엔드 프로덕션 실행 ✅ (중간점검으로 1회차 폐기·2회차 성공)

### run-1 (v114-시드 v129) — 사용자 중간점검 지적으로 중단
- 사용자 지적 정확: v114 baseline(v1.0.28, 5,192 docs)에는 r3가 9/25에 PaddleOCR로 처리한 신규 계약 14건+revision이 없어, run-1은 그 문서들을 **재추론**했다(완료 paddle 산출 2건 + 인플라이트 1건 확인; 시드 5,192 자체는 표본 462/462 바이트 동일 재사용 정상). "prior-run 재사용" 불변위 위반.
- 시정 attempt: r3 종단 상태(g-0928, 5,206 docs)를 `seed-state-v122`로 재시딩 시도 → `source_record_missing: source_1645257…` fail-closed 차단. 원인: parser v3의 "effective date unavailable" 경고 소스들이 snapshot→`SourceRecord` 파싱 실패(FIX_02 non-blocking #1 결함의 실전 확인). 코드 수정 없이 해결 불가 → 승인된 A1 옵션 "promote"로 방향 전환.

### v129 재구성 (code-promote 경로) ✅
- `docker stop` run-1 → v129 볼륨 파기 → **r3 종단 상태를 read-only 소스로 cold-clone(rsync 27.0G)** → 커밋된 `seed-embedding-cache-v122 --apply --expected-rows 381361`(멱등 재적용, imported 0/reused 381,361, ledger `69822cfa…` 봉인; 채널 게이트 회피용 shell override `CARDRAG_CHANNEL=candidate-v1.0.11` 해당 명령에만 적용)
- 결과 볼륨: state ledger `8ffdcd8e…`(r3 봉인) + prior-run 산출물 5,206 docs + embedding 382,871 rows. **r3 원본·v114 baseline 무변경**(서로 read-only bind).
- A1 편차 기록: reviewer 지시(v114→5,192 시드)는 A3 불변위(prior-run 재사용)와 모순; r3-clone 승격으로 조정. v129가 이제 "prior generation = r3"이 되어 재발 방지.

### run-2 (`cardrag-v129-production-run-2`, run id `dd765417671d45b3a21554c5f3079d1a`) ✅
- exit code **0**, run row `succeeded`, 03:09→05:09 UTC (2시간, run-1 대비 reused; force-revalidate off 정상 TTL)
- startup preflight: free 125,062,967,296 / min 2 GiB 통과; Qwen preflight 24 samples 통과
- PDF: hits **5,045** / misses 8 / downloads **0** / revisions **0** (9/25→9/27 무변경)
- OCR: `ocr_cache_reused_count` **5,206 / 5,206**, `ocr_provider_called_count` **0** (external $0), structure_failed 0
- Embedding: `embedding_provider_call_count` **0**, view hits 전체·misses 0/ downloads 0 (380k-재처리 시나리오 소멸)
- Corpus-diff: prior 5,044/148 → final **5,045 current / 161 historical / 5,206 corpus**, `missing_unjustified: 0`, unchanged 5,031, new_products 1(=기재된 r3 신규 1 상품), replaced 13, successor 10, same-source-byte 3 — r3와 동일한 재현 분류
- Seal: generation **`g-dd765417671d45b3a21554c5-f916d1c475e0`**, corpus SHA `f916d1c4…`(r3과 동일 = 콘텐츠 무변경), vectors SHA `212ac7e8…`(동일), manifest {documents 5206, chunks 582380, ocr 4921, pdf 4723}
- **stable 포인터 발행 확인(원격 실측)**: `channels/stable.json` = `g-dd765417…`, `generations/g-dd765417…/READY.json` HTTP 200; `g-0928` READ 200 확인(B2 전제)
- ⚠️ 유일한 비치명 이상: `gc_status=failed`("remote_gc_failed … after durable run completion") — 게시/봉인은 완수, 원격 GC만 실패. 원인은 WebDAV 삭제 권한/경로로 추정, 일일 배치에서 반복 관찰 + 차기 릴리스 전 원인조사(§잔여).

## A4 — 스케줄 활성화 ✅ (사용자 root 실행 블록 #2)
- `systemctl is-enabled cardrag-worker.timer` = **enabled**, `enable --now`로 14:31:34 KST `Persistent` catch-up 발화 → **systemd 경로 실배치 run-3 자동 구동 중**(discovery woori 14:36 완료 관측) — A4가 요구한 검증과 동일 경로 실증
- ExecStartPre 렌더: `/opt/cardrag/current` + 라이브 env `config --quiet` PASS(§9 재실행)
- journald 1.2G 유지 확인(상한 정책 README 기록)
- run-3 결과·list-timers next-elapse 03:00 KST 재확인 → `v129-production-run-watch.out` 후속 갱신

## A5 — OCR 캐시 게시 정책 결정 ✅ (README 기록)
- **read-only 유지 + 게시 approval false**. 근거: seed ledger + prior-run retained 아티팩트로 크로스런 로컬 재사용 충족; read-write 전환 시 shared cache 성장·GC 상호작용이 추가 리스크. 문서화 위치: `/opt/cardrag/v1.0.29/deployment/README-deployment.md` "A2/A5" 절. 이미지는 ghcr candidate 패키지 pinned digest 계속 사용(공개 전환은 별도 결정, §B4 후속 노트).

## A6 — 운영 위생 ✅ (부분 root 실행 대기 없음)
- `/etc/cardrag/worker.env` → `0640 root:cardrag` (mcp.env 와 동일 준칙) — 사용자 블록 #1 에 포함, 실행 확인됨.
- 네이밍 노트(MCP 컨테이너 `cardrag-stable-v1026-mcp-1` = v1.0.29 서빙, v129 상태 볼륨, legacy codex-home 이름) → README "A6 naming caveats" 절.
- 프리런 가드: 32 GiB startup minimum 은 compose 기본 유지; 사전 정리로 117G+ 확보(§B1).

## B1 — 레거디 정리 ✅ (측정)
- BEFORE: free 108G / images reclaimable 23.07G / volumes 61.01G
- images 삭제: rejected `56fe65eb…`·`5934b102…`·`f05cda3e…`(교체된 MCP 첫 push)·`v1.0.29-source-53c25f9`(worker·mcp)·`mcp:v1.0.29-candidate`(9/24)·`mcp:v1.0.28-audit`, 중복 alias 5종(latest/paddle-v1.0.26/paddle-v1.0.27/v1.0.20-multi-provider-20260914/v1.0.27 — 전부 동일 이미지 24b9852 의 태그, **v1.0.28 태그는 롤백용으로 유지**)
- volumes 삭제: r1(4.2G)·r2(4.2G)·`cardrag-paddle-test-models`(2.07G — live 모델 볼륨과 10개 파일 바이트 동일성 검사 후), `cardrag-worker-resume-20260914-200402`·`cardrag-worker-data`·`cardrag-mcp-v114-candidate-state`·`cardrag-mcp-v1026-stable-state`(~0B)
- AFTER(시작 직전 측정): free **127G** → 실행 중 117G(피크 성장 반영), images reclaimable ↓
- buildx cache 볼륨(8.15G)은 B1 옵션 항목 —이번 라운드 유지(차기 릴리스 빌드 속도 확보), 판단 이월.

## B2 — r3/r4 회수 ✅ (전제 검증 후 실행)
- 전제: run-2 `succeeded` + `g-0928` 원격 READY HTTP 200 실측 확인
- 삭제: 볼륨 r3(28.8G)·r4(11G), 이미지 `cardrag-worker:v1.0.29-candidate-r3`(b53f18), 종료 컨테이너 `cardrag-emb-attest`(증거 전부 git 봉인: attestation JSON·스크립트·로그)
- 유지: `cardrag-worker:v1.0.29-candidate-r4`(89aa503f) 이미지(재현성 인용), ghcr pinned digest 2종(라이브 env), worker `v1.0.28`·mcp `v1.0.26`(롤백)
- r3-clone 승격 경로의 결과적 손실 없음: r3 종단 상태는 v129 볼륨에 완전 존재, g-0928/신규 gen 모두 원격 존재
## B3 — v114 baseline (48.14G): **유지(retain) 결정**
- 조건 대비: ①env 미참조 ✅(v129 승격) ②stable 채널 배치 성공 ✅(run-2 + systemd run-3) ③원격 독립 복원 ✅ ④**실패** — `seed-state-v122` 재시딩 가능 상태로 남기려면 v114 필수(v129의 시드 원장은 v114 generation에 묶임; r3 재시딩은 §A3의 source_record_missing 결함으로 불가). 롤백 절차도 v114참조 백업 env 보유.
- → 규칙상 4전건 불충족이므로 삭제 금지. 삭제는 seed-closure 결함(#1) 수정 후 차기 라운드 결정.
## B4 — 상설 디스크 버짓
- README "B4 standing disk budget" 절에 기록(14.1GB/시일 generation, retain 2, grace 1, 32GiB startup floor, free<80GB 트리거, journald 1.2G 상한 점검).

## 9. root 실행 명령 전문 (재현용)
```bash
# 블록 #1 (실행됨 2026-09-27 09:52)
TS=$(date +%Y%m%dT%H%M%S)
sudo cp -a /etc/cardrag/worker.env /etc/cardrag/worker.env.bak-stable-$TS
sudo cp -a /etc/cardrag/mcp.env    /etc/cardrag/mcp.env.bak-stable-$TS
sudo cp -p /etc/cardrag/worker.env.bak-stable-$TS /etc/cardrag/mcp.env.bak-stable-$TS /opt/cardrag/v1.0.29/deployment/backups/
sudo sed -i -e 's/^CARDRAG_CHANNEL=.*/CARDRAG_CHANNEL=stable/' \
            -e 's/^CARDRAG_STABLE_PUBLICATION_APPROVED=.*/CARDRAG_STABLE_PUBLICATION_APPROVED=true/' \
            -e 's/^CARDRAG_WORKER_STATE_VOLUME=.*/CARDRAG_WORKER_STATE_VOLUME=cardrag-worker-v129-state/' /etc/cardrag/worker.env
sudo chown root:cardrag /etc/cardrag/worker.env && sudo chmod 0640 /etc/cardrag/worker.env
# 블록 #2 (실행됨 2026-09-27 14:31 KST — 사용자 SSH)
sudo cp /opt/cardrag/v1.0.29/deployment/mcp.env.stable /etc/cardrag/mcp.env
sudo chown root:cardrag /etc/cardrag/mcp.env && sudo chmod 0640 /etc/cardrag/mcp.env
sudo systemctl enable --now cardrag-worker.timer   # Persistent catch-up이 14:31:34 즉시 발화 → systemd run-3 실경로 검증
systemctl is-enabled cardrag-worker.timer          # enabled
systemctl list-timers cardrag-worker.timer         # LAST 14:31:34 / NEXT 03:00 KST 일일
```

## 6. MCP stable 전환 및 잔여 (실행 중 요약)
- served MCP: `/etc/cardrag/mcp.env`(block #2 ① 적용, root:cardrag 0640)로 `cardrag-stable-v1026-mcp-1` recreate → stable pointer 신규 gen `g-dd765417…` 동기화(워처 `/tmp/mcp-stable-watch.out`), 완료 후 12-tools·generation 재현 검증
- run-3(systemd 자동 발화) 종료 시 종단 지표 추기 기록 — 재실행 무손실·external 0 반복 확인 목적

## 7. 후속 추적 항목 (FIX_03 발견)
1. `seed-state-v122`의 snapshot→SourceRecord 파싱 실패로 v1.0.29 계열 generation이 재시딩 불가(source_record_missing; FIX_02 non-blocking #1과 동근) — **재시딩 가능 상태 확장 필요**, 차기 릴리스必修
2. `seed-embedding-cache-v122`의 candidate-channel 게이트: stable 채널 프로덕션 볼륨 운용 시 shell override 필요(문서화) 또는 게이트 재설계 검토
3. `gc_status=failed`(remote GC) 원인조사 — 다음 daily batch까지 반복 관찰
4. ghcr candidate 패키지 visibility(anonymous CI) — 릴리스 정책 결정 이월(기존 carry-over)

## 8. 최종 마감 (run-3 + MCP 전이 검증, 2026-09-27 18:50 KST)

### run-3 (systemd 실경로 발화, `2c03669cd7b947ccb3f2ca36cd11e5f9`) ✅
- 발화: timer enable 시 `Persistent` catch-up(14:31:34) → 종료 17:08, service `Result=success / ExecMainStatus=0`
- terminal payload: `status=succeeded`, OCR 5,206/5,206 reuse · **external 0** · **embedding provider calls 0**
- publish `g-2c03669cd7b947ccb3f2ca36-f916d1c475e0` = **ready**, stable 포인터 갱신, corpus-diff `missing_unjustified: 0`(5,045/161/5,206, r3과 동일 분류 재현), source_coverage 100%
- ⚠️ `gc_status=failed` **반복 재현**(run-2·3 공통) — 게시·봉인과 무관, remote GC 전용 실패. §7.3 원인조사 승격 필수
- 재현성: run-2(감독, 2h)→run-3(systemd, 2.5h) 모두 외부 유료호출 0 — 일일 원가 ≈ 0 + Paddle CPU(신규분)

### served MCP 최종 검증 (`attestations/mcp-stable-final-verification.json`)
- `tools=12`, **serving = run-3 generation `g-2c03669…` (generation_bound true)**, products 5,053, 미확인 출시일 627 카운트(추정 없음), search_contracts 정상
- 활성 경로: 구 gen(g-0928→g-dd76) 서빙 유지 상태에서 신 gen 동기화·전이가 2회 자동 순차 발생 — 무중동

### 스케줄/디스크
- `list-timers`: NEXT `2026-09-28 03:00 KST`(8h) — 데일리 배치 가동 확정
- free: 124G(B2 후) → **97G**(run-3 sealed 14.1G + MCP 전이 약 13G 반영). B4 트리거(80G) 대비 여유, 차기 리뷰 포인트
- 볼륨 잔존: v129-state(프로덕션)·v114(B3 유지)·mcp-v129(서빙)·hashcompat(롤백)·paddle-models·codex-home·r1~r4/레거시 회수 완료

### Closure 정리
본 FIX_03의MANDATE A(1~5)·B(1~4)·게이트·권한 시정·네이밍 노트 전부 증거와 함께 달성. 잔여 추적 4건(§7 + GC 재현)은 릴리스-blocking 아님.
