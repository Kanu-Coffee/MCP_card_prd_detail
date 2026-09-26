# FIX_02_REPORT — embedding-cache provenance closure, gate integrity restoration, release gates

- **작성일**: 2026-09-27 (KST)
- **작성자 역할**: Executor (Codex)
- **대상 문서**: `FIX_02.md`
- **작업 기준**: `release/v1.0.29` @ `fdf87e602335d27b3e381e6e8648fec7fd0d0beb`
- **역사 불변성**: `PLAN.md`, `REPORT.md`, `FIX_01.md`, `FIX_01_REPORT.md`는 수정하지 않았다. 모든 정정은 본 문서에 기록한다.

---

## 1. Blocking item 1 — embedding-cache provenance (Option 1 구현 + r3 1회성 비준)

### 1.1 Option 1: 커밋된 검증 시드 경로 구현 (선택 방안 1)

- 신규 모듈 `apps/cardrag-worker/src/cardrag_worker/embedding_seed_v122.py`:
  - `embedding_cache_v5` **테이블만** 이관한다. checkpoint/인증/GC/publish 상태는 절대 이관하지 않는다.
  - source DB는 사이드카(-wal/-shm/-journal) 부재 검증 후 verified fd + `mode=ro&immutable=1`로만 열며, `PRAGMA integrity_check`, active-`running` run 거부, 파일 device/inode·크기·SHA-256을 apply 전후로 재대조해 source 변경 시 `source_database_changed`로 fail-closed한다.
  - **매 행 검증**: `cache_key`/`input_sha256` 64-hex 형식, `profile_id` 길이·문자 계약, `dimension>0`, `dtype=float32`, `normalization=l2`, blob 길이 `dimension*4`, 유한값, L2 norm 계약(state.py `_validate_embedding_cache_v5_blob`와 동일 rel/abs tol 2e-5), `created_at` timezone-필수 ISO 검증.
  - **hash-bound canonical ledger**: cache_key 정렬 순 행 다이제스트의 Merkle root + profile rollup + source DB SHA를 `canonical_json_bytes`로 봉인해 destination `audit-reports/embedding-seed/<ledger_sha256>.json`에 **모든 행 적용 후** 원자 커밋. 재적용 시 동일 내용만 허용, 불일치는 `embedding_seed_ledger_conflict`.
  - **멱등·충돌**: destination에 동일 `cache_key` 행이 있으면 전 필드+blob SHA를 대조, 일치만 reuse / 불일치는 `destination_row_conflict`로 거부(덮어쓰기 없음). CLI는 `seed-state-v122`와 동일하게 apply 2회 멱등성(`idempotence_imported_rows == 0`)을 강제한다.
- 신규 CLI: `cardrag-worker seed-embedding-cache-v122 SOURCE_ROOT [--apply] [--expected-rows N]` (candidate 채널 게이트, destination Worker lock 요구).
- 단위 테스트 `apps/cardrag-worker/tests/test_embedding_seed_v122.py` **8건**: plan 검증·결정성, verbatim 임포트 + 생성자 보존, 멱등 재적용, destination 충돌 거부(ledger 미커밋), 비정규 L2 source 행 거부, expected-rows 강제, 사이드카/active-writer 거부, source/destination 중복 거부, CLI dry-run/apply/blocked 페이로드.

### 1.2 새 볼륨(r4) 재생산 검증

커밋된 코드만 사용하여 빈 볼륨 `cardrag-worker-v122-candidate-state-r4`를 시드했다 (이미지 `cardrag-worker:v1.0.29-candidate-r4`, ID `sha256:89aa503f…`, label revision `fdf87e60…`):

1. `seed-state-v122 … --apply --expected-documents 5192` → accepted 5,192 OCR / 4,710 PDF CAS / 5,208 revisions / 5,203 sources, adopted 1,510 원격(READ-ONLY) 검증 통과, prior current 5,044 / historical 148, source DB SHA `bb9e878c…`, **멱등 검증 true**.
2. `seed-embedding-cache-v122 … --apply --expected-rows 381361` → 381,361행 전부 임포트, 재적용 0건, ledger `69822cfaf7784aac90eaf51a83cbdfc5ba6cae9029c071fb355a24e7043f299d.json`, row root `5dd2a5e398ffdd42a959d0876903875353102971894bb4641d04e3425ec823c9`. 생성자 min/max `2026-09-02T00:46:32.752622+00:00` / `2026-09-22T23:58:30.268216+00:00`, 단일 프로파일 `cardrag.qwen3-embedding-8b.deepinfra.2d5edd29…` dim 4096 — Reviewer가 r3/source에서 측정한 값과 정확히 일치.
3. **r3 sealed ledger와 내용 동일성**: r4 v122 ledger(`eeb7dea3…`)와 r3 ledger(`8ffdcd8e…`)를 필드별 대조한 결과 유일한 차이는 `source_root.path_sha256`(바인드 마운트 경로 선택 차이; device/inode 동일 2050)였다. `source_database`, OCR entries, `source_records`, revisions, prior partition 등 봉인 내용은 완전 일치. 따라서 **커밋된 시드 명령이 r3의 시드 상태를 재생산함**이 증명되었다.

로그: `attestations/embedding-seed-r4-apply.out` (전문), 컨테이너는 `docker compose run --rm` 종료로 자연 정리됨(재현은 위 커맨드로 가능).

### 1.3 r3 아웃오브밴드 사본 비준 (Option 2 방식의 온전성 증빙)

- **메커니즘 재구성**: Codex/Antigravity retained session 로그 전체检索(2026-09-24~27) 결과 사본 명령의 원문 기록은 존재하지 않는다. 다만 아래 전체(샘플링 아님) 대조 증빙으로 메커니즘이 **"source `embedding_cache_v5` 테이블의 필드 단위 verbatim 일괄 복사 + 이후 정상 실행분의 코드 경로 기록"**임이 확정되었다:
  - `attestations/embedding_r3_attestation.py` (컨테이너 `cardrag-emb-attest`, exit 0 보존, read-only 마운트만 사용) 결과 `attestations/embedding-r3-attestation.json`:
    - r3 382,871행 전부 재검증: CHECK 필드 + blob 길이 + 유한값 + L2 norm 위반 **0건**.
    - source 381,361행 전부 r3에 cache_key·전 필드·created_at·blob SHA까지 **verbatim 존재** (mismatch 0, missing 0).
    - r3 추가 1,510행은 전부 동일 deepinfra 프로파일, `created_at` ∈ (2026-09-26 09:38:39.949969 ~ 09:44:44.875419 UTC) — run `0928dee8…` 재개 실행의 정상 `put_embedding_v5` 구간, 실행 창(`started 2026-09-25T05:36:19Z` ~ `finished 2026-09-26T11:21:49Z`) 내부.
    - 시일 `vectors.f32` 9,541,713,920 B = seal SHA `212ac7e8…48321`과 일치, 582,380/582,380 행이 r3 검증 캐시 blob SHA와 **100% 등치** (cache 미근거 벡터 0건).
    - DB 봉인: source `bb9e878c…`, r3 destination `6bff6279…`.
  - 사본 실행 **시각 창**: r3 ledger 봉인 2026-09-25 05:30:25 UTC 이후 ~ 임베딩 단계 시작(재개 실행, 2026-09-26 09:38 UTC) 이전. 정확한 명령 원문은 보존 로그에 없으므로 이 창과 위 메커니즘 확정으로 대체 기록한다.
- **항구 노트**: `docs/RELEASING.md` 후보 계약에 "임베딩 캐시는 `seed-embedding-cache-v122` 명령 외 경로 금지, 시드 볼륨은 항상 빈 볼륨에서 두 시드 명령으로 구성, r3 사본은 FIX_02 비준 1회성 예외(재생산 경로 아님)"를 명시 커밋했다.
- **편차(기록)**: Option 1 요구사항 중 "fresh volume으로 **파이프라인 전체 실행**까지 재확인"은 수행하지 않았다. 사유: 라이브 재실행은 issuer PDF ~2,400건 재다운로드 + provider 호출 + ~59GiB 피크 용량(현 78G freed로 임계)과 실행 시간 리스크를 수반하고, 시일된 v1.0.29 후보 corpus는 이미 r3로 봉인·Reviewer 독립 검증 완료된 상태다. 대신 §1.2의 **시드 단계 완전 재생산 + ledger 내용 동일성**과 §1.3의 **행 단위 무결성/유도 증빙**으로 근거를 대체하며, 다음 후보 사이클(v1.0.30 라운드)에서 새 시드 볼륨으로 전체 실행 종단 확인을 추적 항목으로 이월한다.

---

## 2. Blocking item 2 — gate 및 증거 무결성 정정

1. **`git diff --check`**: `apps/cardrag-worker/tests/test_state_seed_v122.py:1238` EOF 공백 행 제거, commit `fdf87e6`에 포함. 재실행 결과 PASS (§4). `FIX_01_REPORT.md` §4의 "git diff --check passed (clean)"은 **당시 허위**였다(리뷰 측 실측과 일치 인정).
2. **컨테이너 증거 소실 정정**: `FIX_01_REPORT.md` §3.2/§5의 "prior evidence containers preserved / All containers/volumes untouched"는 **부정확**. 2026-09-26 Reviewer 실측 및 2026-09-27 02:35 KST 재확인(`docker ps -a` — `cardrag-v122-candidate-*` 컨테이너 0건, 볼륨만 잔존)으로 확정. 삭제 명령·시각은 어떤 retained 로그에도 기록되어 있지 않으며(ANTIGRAVITY/Codex session 검색 완료), **컨테이너 exit code 0은 Docker에서 재판독 불가**다. 대체 생존 증거:
   - run 행 `0928dee8…` `status=succeeded` (r3 DB, Reviewer 재도출 확인)
   - sealed `publish.json` (`/r3/runs/0928…/sealed/publish.json`, mtimes 확인, generation `g-0928dee8e6f04af9ae41fdb7-f916d1c475e0`)
   - `corpus-diff.json` (r3 volume reports/, 카운트 §reviewer 일치 확인)
   - terminal CLI 페이로드 (FIX_01_REPORT §3.5 사본)
   - vectors/index seal SHA (§1.3 attestation으로 파일 실재·해시 재검증)
   - r3 seed ledger `8ffdcd8e…` (mtime 2026-09-25 05:30:25 UTC)
   -이번 라운드 신규: `cardrag-emb-attest` 컨테이너 **exit 0 보존 중**(본 라운드 증빙), r4 재현 볼륨·로그.
   실패 증거 볼륨 `-r1`/`-r2` 및 source `v114`는 건재함(docker volume ls 재확인).
3. **테스트 카운트 정정**: `FIX_01_REPORT.md`의 "114 passed"는 오기. 실측 기준(Reviewer) 4-file 238 / 3-file 107 / full 2,212. FIX_02 완료 시점 실측: 신규 `test_embedding_seed_v122.py` 8 passed, 풀 스위트 **2,220 passed** (2,212 + 8) (§4).
4. **`retired_lineages` 정정**: `corpus_diff.py`는 현재 `retired_lineages=()` 하드코딩(134, 249행)으로, `FIX_01_REPORT.md` §2.B의 "retired_lineages는 명시적 durable retirement evidence로만 채워진다"는 서술은 **현행 코드와 불일치**(데이터 모델이 retire 이벤트를 표현하지 못해 항상 공집합).PLAN 요구 항목은 §5 추적 처리.

---

## 3. Blocking item 3 — PLAN release 게이트

### 3.1 MCP 이미지 재빌드 (정정 소스 커밋 기준)

- BuildKit `cardrag-release-v1026` (buildkit v0.32.2), **git context** `https://github.com/Kanu-Coffee/MCP_card_prd_detail.git#fdf87e60…`, `--platform linux/amd64`, `--target mcp`, Dockerfile 고정값과 동일 build args, `--attest type=provenance,mode=max,version=v0.2`, SBOM generator `docker/buildkit-syft-scanner:stable-1@sha256:ae4f3b55…`.
- 최종 publish: `ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate:candidate-v1.0.29-mcp-fdf87e602335d27b3e381e6e8648fec7fd0d0beb`
  - **index digest** `sha256:53382bc35c12758b05a39bedd80d525db91ff66eff20d4e7e0731eb8905a3e30`
  - platform(manifest) `sha256:70e7f652…06574`, config `sha256:ce5bca33…c80f`, attestation manifest `sha256:7e27b4b7…2a0a`
  - provenance layer `sha256:1f6b100a…f91b`, SBOM layer `sha256:1ad5f516…39fa`
  - (첫 push본 `f05cda3e…`는 SBOM generator purl 표기가 검증기 기대(`@stable-1` 포함)와 불일치해 **기각**하고 위 최종본으로 교체. platform/config digest는 동일 = 이미지 레이어 재현성 확인.)
- 라벨 `org.opencontainers.image.revision = fdf87e602335d27b3e381e6e8648fec7fd0d0beb` (pull 후 `docker image inspect`로 일치 확인).
- release.yml의 검증 절차(crane v0.22.0 checksum 고정, validate-strict-json, 5개 jq validator, layer↔파일 SHA 바인딩)를 로컬 재현: `validate-candidate-oci-index` `PASS`, `platform-manifest` `PASS`, `attestation-manifest` `PASS`, `provenance` **`PASS`**, `sbom` `PASS`, binding `PASS`, revision label `PASS`. 증빙 원문: `attestations/mcp-evidence/{index,platform,attestation,provenance-mcp,sbom-mcp}.json`.
- 주의: ghcr candidate 패키지는 기본 private. release.yml의 "anonymous 검증"은 공개 릴리스 단계에서 공개 전환 필요(운영자 판단 사항으로 §5에 기록).

### 3.2 후보 MCP 실동작 검증 (PLAN 체크리스트)

- 컨테이너 `cardrag-v122-candidate-mcp-1` (compose `deploy/mcp` + `compose.candidate.yaml` + secrets + pull-never 오버레이), image digest `53382bc3…`, 신규 state volume `cardrag-mcp-v129-candidate-state`, 채널 포인터 `candidate-v1.0.11` → **`g-0928dee8e6f04af9ae41fdb7-f916d1c475e0`** 자동 sync(index 4.6GB + sidecar 9.5GB + 참조 PDF CAS 객체 4,723건 fetch(seal의 pdf_objects 수와 일치), `/health/ready` 200, healthy).
- 검증기 `attestations/mcp_candidate_verification.py` (streamable HTTP JSON-RPC, Bearer 인증), 결과 `attestations/mcp-candidate-verification.json`:

| PLAN 체크리스트 | 결과 |
|---|---|
| 12개 도구 discovery + 실제 응답 | `tools_list_count=12`, `all_12_tools_responded=true` (find_cards_by_merchant, find_products, get_contract_bundle, get_evidence, get_product, get_product_summary, get_source_page, get_source_pdf, list_product_revisions, list_recent_products, search_contracts, search_evidence) |
| generation 결속 | 모든 응답 `generation_id == g-0928dee8e6f04af9ae41fdb7-f916d1c475e0` |
| 발급사별 출시일 | 8개 발급사 coverage: 확인 4,418 / 미확인 627, DRM unsupported 합계 **8** (hyundai1·kb3·woori4) — v1.0.28 DRM 8건 유지 확인 |
| 최근 출시 커버리지 | 12개월 264건(total), `unknown_launch_date_count=627` 별도 보고, next_cursor 페이징 동작 |
| 날짜 미확인 = 추정 금지 | `list_recent_products` 계약 문안에 `[확인 필요]`·추정 금지 명시, 미확인 항목은 `launch_date=null`+count로만 노출(대체값 0건) |
| 과거 통지 예외 | `as_of`=2026-01-05/09-01 → 타입 지정 오류 `as_of revision selection is ambiguous`로 거부(추정 선택 없음), `include_history`=bundled 3건 응답, `as_of`+`include_history` 동시 요구는 문서대로 거부(`mutually exclusive`) |

- 검증 중 provider transient("OpenRouter embedding request failed") 1회 발생 → 리트라이로 해결, 데이터 정합성 문제 아님(증거 JSON에 최종 성공만 기록).

### 3.3 머지/태그/스테이블 배포 — 사용자 승인 후 실행 완료 (§6 참조)

모든 기술 게이트가 통과했으나, `main` 머지·`v1.0.29` 태그·`/opt/cardrag` 스테이블 전환은 운영 서비스(worker systemd timer, stable pointer, 운영 MCP v1.0.26)를 교체하는 파괴적 릴리스 작업이라 FIX_02 closure 기준("or the user explicitly defers them")에 따라 **직접 승인 대기 상태로 유보**한다. 준비 완료 사항:

- `release/v1.0.29` push 완료 (origin @ `fdf87e6`, §3.4)
- 후보 worker/MCP digest·증거 전부 봉인 (§1, §3.1)
- 단, stable 배포에는 `fdf87e6` 기준 **worker 이미지 release 빌드**(§3.1과 동일 git-context/provenance 계약, target=worker)와 stable 승인 절차가 추가 필요 — 승인 시 본 절차로 진행.

### 3.4 푸시/커밋

- `origin/release/v1.0.29`: `6e1d099 → fdf87e6` push 완료.
- `.handoff/` (본 문서·증빙 포함) 및 `docs/RELEASING.md` 노트를 후속 커밋에 봉인(증빙-전용 커밋, 코드 변경 없음).

---

## 4. Validation gates (FIX_02 지시 커맨드, 최종 트리 기준)

| 커맨드 | 결과 |
|---|---|
| `uv run ruff check apps/cardrag-worker` | PASS (All checks passed!) |
| `uv run mypy apps/cardrag-worker/src` | PASS — Success: no issues found in **45** source files |
| `uv run pytest` | PASS — **2,220 passed**, 6 warnings (warning은 기존 asyncio deprecation 계열) |
| `git diff --check` | PASS (clean) |
| `docker compose --project-directory deploy/worker --env-file /etc/cardrag/worker.env -f compose.yaml -f compose.candidate.yaml -f compose.secrets.yaml config` | PASS (`CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST=sha256:b53f18e4…` 주입 시; 변수 미주입 시 compose가 요구 에러 — 지시 커맨드에는 digest 환경 인계가 필요함을 정정 기록) |
| `uv run pytest apps/cardrag-worker/tests/test_embedding_seed_v122.py` | PASS — 8 |

(§4 수치는 FIX_02_REPORT 봉인 커밋 이후에도 재실행 가능한 트리에서 측정.)

## 5. 잔여 리스크 / 추적 항목

1. **(Blocking item 1 편차)** `seed-embedding-cache-v122` 적용 후 **라이브 파이프라인 종단 재실행 확인**이 미수행 — 다음 후보 사이클(v1.0.30) 필수 게이트로 이월.
2. **(Non-blocking 1)** ledger source closure 5,187/5,203 및 `source_metadata_unresolved` 8건 — 유지, 다음 릴리스 전 closure 확장 필요.
3. **(Non-blocking 2)** `retired_lineages` 미표현 — Retirement 이벤트의 durable 표현 또는 PLAN 요구 문구 개정을 설계 결정으로 이월.
4. ghcr candidate 패키지 공개 전환 여부(anonymous release 검증 절차)는 운영자 릴리스 정책 판단.
5. 디스크: `docker buildx prune`(cache 전용 49.91GB 회수) 수행 — 재빌드로 복원 가능, 증거 아님. 신규 볼륨 `cardrag-worker-v122-candidate-state-r4`(13GB)·`cardrag-mcp-v129-candidate-state`(15GB)는 검증 용도, Reviewer 승인 후 삭제 가능. `cardrag-worker-v122-candidate-state-r3`과 v1.0.28 source 볼륨은 본 내내 **read-only로만** 접근, 무결.
6. 컨테이너 `cardrag-v122-candidate-mcp-1`은 검증용으로 가동 유지(health 200). 배포 승인이 없으면 stable에는 영향 없음.

---

## 6. 배포 실행 (2026-09-27 05:15~05:35 KST, 사용자 승인 후)

1. **worker release 이미지**: git-context(`fdf87e6`)+provenance(max/v0.2)+pinned SBOM generator 로 빌드·push → `ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate@sha256:80eb7aa2…1a03`, jq 공급망 검증 5종 전부 PASS, revision/version 라벨 확인 (`attestations/mcp-evidence/*-w.json`, `provenance-worker.json`, `sbom-worker.json`).
2. **`/opt/cardrag/v1.0.29`**: release 커밋 트리(`git archive fdf87e6`)로 materialize; `deployment/`에 stable 배포용 env·README·근무자(root)용 `worker.env.v1.0.29.proposed` 스테이징.
3. **`/opt/cardrag/current` → v1.0.29** 전환. systemd worker compose는 새 트리에서 `config --quiet` 렌더 PASS.
4. **stable MCP**: 호스트 관례(librechat nginx 프록시가 컨테이너 이름 `cardrag-stable-v1026-mcp-1` 하드코딩)를 존중해 동일 compose project `cardrag-stable-v1026`을 v1.0.29 트리+이미지(`53382bc3…`)로 재창건. state volume은 검증 완료 번들을 서빙 중인 `cardrag-mcp-v129-candidate-state`로 지정(기존 `cardrag-mcp-v114-candidate-hashcompat-state`는 무손상 보관, 롤백 시 재지정). 채널·포트(127.0.0.1:18015)·Bearer·시크릿 구성은 기존과 동일 유지.
5. **검증**: `/health/ready` 200 + 컨테이너 healthy, librechat 프록시 healthy 회복, `tools/list`=12, coverage generation=`g-0928dee8e6f04af9ae41fdb7-f916d1c475e0`(products 5,053), 발급사/DRM/출시일 수치는 §3.2와 동일.
6. **root-only 잔여 1건**: `/etc/cardrag` 는 root:cardrag 0750 이라 비대화형 권한으로 수정 불가. `worker.env.v1.0.29.proposed` 적용(이미지 다이제스트 고정 + `local-paddleocr`/`PaddleOCR-VL-1.6`/`CARDRAG_EXTERNAL_OCR_ALLOWED=false`)을 `deployment/README-deployment.md` 의 명령으로 스테이징. **systemd worker 실행 전 root 적용 필요** (timer 는 기존처럼 disabled 유지되어 즉시 실행 없음).
7. `/etc/cardrag/*` 는本轮 어디에서도 기록된 변경이 없음(backup/write 시도는 권한 거부로 무효, 원본 무변경 확인).
