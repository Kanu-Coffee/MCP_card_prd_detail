# 005 — Executor REPORT (v1.0.32 공개 릴리스 증거 봉인과 발행)

## 요약

PLAN 005의 **Step 1(사전 점검과 소스 고정)을 완료**했다. `1.0.32` 계약을 일치시킨
source commit `f5b12d0a1deae7d3c849b027d62f37a85bb47be3`를 `main`에 push하고 CI
run `37210571160`의 전 성공을 확인했다.

Step 1의 선행 입력 가용성 점검(PLAN 16·18행)에서 **공개 가능한 승인 gold와
`v109_baseline` frozen legacy baseline 입력이 이 호스트·저장소에 존재하지 않음**을
확인했다. PLAN 18행의 정지 규칙("자료가 없거나 게시 권한이 없으면 현재 사실을
보고하고 해당 단계에서 멈춘다")에 따라 **Step 2~5를 실행하지 않고 중단**한다.
v1.0.32 tag·GitHub Release·Docker Hub 이미지는 **발행되지 않았다(미완료)**.
수용 기준 4에 따라 미완료 상태를 명확히 보고한다. 합성 fixture, 과거 자료 이름
변경, 수동 Release, tag 이동으로 검증을 우회하지 않았다.

## Step 1 완료 근거

### 사전 점검 (2026-10-04 22:40~23:00 KST)

| 항목 | 확인 결과 |
|---|---|
| 원격 git tag | 최신 `v1.0.31`; `v1.0.32` 부재(미사용 확인) |
| GitHub Release | 최신 공개 `v1.0.29`; `v1.0.30`/`v1.0.31` tag는 Release 미연결(보존) |
| Docker Hub `ymtop59/mcp-card-prd-detail` | 70 tags; `1.0.32`/`1.0.32-worker`/`1.0.32-mcp` 모두 HTTP 404(미사용). `1.0.30`/`1.0.31` 계열 tag도 404(미발행 상태 보존) |
| `main` CI 기점 | 출발 전 `bbf11fc` success(run 37196709414); `main`==`origin/main` |
| 운영 timer | `cardrag-worker.timer` 다음 실행 2026-10-05 03:00 KST, 마지막 11:47:55 KST |
| MCP | `cardrag-stable-v1026-mcp-1` Up 7 days (healthy) |
| 디스크 | `/` 여유 114G(df 392G 중 261G 사용) |
| GHCR 후보 package | `ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate` 존재, visibility=public |
| GitHub 환경 | `dockerhub-public` 환경 존재(`can_admins_bypass=false`) |
| Release workflow | release.yml 현대 게이트(봉인 tag dispatch)는 v1.0.31 run `37190811161`에서 validate 10초 실패한 기록 이후 성공 사례 없음(과거 Release는 무자산 수동 생성) |

Secret 확인 한계: 현재 `gh` OAuth token으로 `GET environments/dockerhub-public/secrets`는
404를 반환한다(토큰 scope 제한 추정). PLAN 7행이 기재한 `DOCKERHUB_USERNAME`·`DOCKERHUB_TOKEN`
등록 사실을 API로 재검증하지 못했고, **실제 Docker Hub 게시 권한은 publish job 전까지
확인 불가**라는 PLAN 7행 전제를 그대로 유지한다. 인증정보 값은 어디에도 읽지·쓰지 않았다.

### portable evidence 재계수

`release.yml`의 `portable_evidence_relative_paths`를 python으로 파싱해 **56개 고유 경로**를
재확인했다(004 `FIX_04_REPORT.md`의 54개 표기는PLAN 9행대로 부정확; 원문은 그대로 두고
이 문서에서 정정한다). `unchanged_documents=5512`는 매니페스트 문서 수/OCR 캐시 재사용 수이고,
`FIX_02_REPORT.md`의 corpus gate `unchanged`는 5,060건이며, 004 첫 run ID는 `707da1b37f…`다.
본 보고는 이 의미를 구분해 사용한다.

### 버전 계약 일치 (source commit `f5b12d0`)

2224dec(v1.0.31 bump) 전례를 미러링해 15개 파일의 현재 버전 고정 문자열을 `1.0.32`로
일치시켰다: 3개 패키지 `pyproject.toml`·`uv.lock`·`cardrag_worker/__init__.py`,
`release.yml`(버전 게이트 2·evidence 경로 exclude 2·artifact 조건·input 설명),
`validate-candidate-provenance.jq`·`validate-candidate-sbom.jq`,
`test_candidate_acceptance_release_gate.py`·`test_candidate_oci_supply_chain.py`·
`test_workspace_contract.py`, `README.md`·`docs/RELEASING.md`·`deploy/simple.env.example`.

유지한 의도적 v1.0.31 호환 분기(근거): `candidate_acceptance.py`의 version Literal 목록과
`verify_candidate_acceptance`의 generation v6 스키마 분기는 **과거 봉인 receipt들의
재검증 가능성**을 위해 이전 버전 문자열을 보존하며 `1.0.32`를 추가했다
(:193, :228, :913 Literal과 :1224 튜플). 전 트리 잔존 `1.0.31` 참조는 위 4곳뿐임을
grep으로 전수 확인했다(rg 부재로 grep 사용).

### 검증 결과 (모두 실제 실행)

| 검증 | 결과 |
|---|---|
| `ruff check`/`ruff format --check` (CI 경로: core/worker/mcp/runtime_v1/tools) | All checks passed / 199 files formatted |
| `mypy` (core/worker/mcp src) | Success, no issues in 95 source files |
| `pytest` (core/worker/mcp/runtime_v1) | **2278 passed**, 9 warnings, 49.47s |
| uv 0.8.17 pinned 설치 | checksum `920cbcaa…8203` **OK**, `uv --version == 0.8.17` |
| `uv lock --check` (0.8.17, `f5b12d0` detached worktree) | Resolved 143 packages — 통과 |
| `uv sync --frozen --all-packages --no-dev` (0.8.17, 동일 worktree) | 통과 — v1.0.31 run 37190811161의 실패 지점(중복 `--package`) 재현 없음 |
| `main` push 후 CI | run **37210571160** completed success (audit/lint/type/runtime/compose/image 포함) |

CI가 green인 정확한 source commit: **`f5b12d0a1deae7d3c849b027d62f37a85bb47be3`**
(`chore(release): bump version to v1.0.32`, https://github.com/Kanu-Coffee/MCP_card_prd_detail/actions/runs/37210571160)

## 정지 판정: 선행 입력 부재

PLAN 16행이 나열한 선행 입력 가용 여부를 먼저 점검한 결과:

1. **승인 gold 부재.** `cardrag-gold-review draft` 초안은 운영 DB 읽기 없이도 가능하지만,
   EVALUATION.md 16~22·34행대로 **사람 검토자 전체 corpus 승인과 `seal-gold` 봉인**이
   필요하다. 이 호스트 어디에도 봉인된 gold·gold-review state가 없다
   (`find` /home/lee·/opt·/srv·/var/lib·/data 검색: `gold*.jsonl`, `*gold-review*`,
   `*capture-receipt*` 0건; `/evaluation` 미존재). Executor는 검토자를 대신할 수 없고
   PLAN 18행은 합성 fixture·이전 자료 이름을 바꿔 대체하는 것을 금지한다.
2. **`v109_baseline` frozen legacy 입력 부재.** RELEASING.md 91~92행: 이전 운영자만
   보유한 비교 입력이 없으면 "검증 미완료로 남으며 release gate를 우회하지 않는다".
   정확히 호환되는 frozen generation·DB·manifest identity가 저장소 밖 어디에도 없고,
   004 `FIX_03_REPORT.md:120`·`FIX_04_REPORT.md:132`가 "골드 평가 데이터셋 재생 실행"
   미수행을 이미 기록한 것과 일치한다.
3. 따라서 5-lane capture·답변 artifact·집계 profile·`gold-capture-set-receipt`·
   `candidate-acceptance-receipt`를 **release-grade로 생성할 수 없다**. portable
   evidence 56개 경로 중 어떤 것도 실제 평가 없이 봉인 대상이 되지 못한다.
4. Docker Hub 게시 권한은 위 정지 사유보다 후순위이나, 실제 권한 실증 수단(publish job)
   자체가 봉인 tag 없이는 도달 불가능하다.

### 미실행 단계 (PLAN 기준 그대로)

- Step 2(격리 후보 OCI 빌드·GHCR push·12도구 후보 검증 receipt): **미실행.** 후보 digest는
  receipt·evidence와 결속될 때만 소비되므로 gold 부재 상태에서 공개 GHCR push는
  근거 없는 외부 가시 변경이 되어 PLAN 18행 취지에 반한다.
- Step 3(실제 평가 증거 생성): **불가** (위 1~3).
- Step 4(`release-evidence/v1.0.32/` 봉인 commit·annotated tag `v1.0.32`): **미실행.**
  tag·Release 생성 없음.
- Step 5(`release.yml` dispatch, Docker Hub 발행): **미실행.** 공개 Release 최신은
  `v1.0.29` 유지, Docker Hub 기존 tag/digest 불변, `v1.0.30`/`v1.0.31` annotated tag
  이동·삭제 없음.
- Step 6(결과 대조 표): 발행이 없어 발행 자산 대조 불가.

### 운영 불변 확인

후보 평가·빌드를 착수하지 않았으므로 운영 timer(다음 2026-10-05 03:00 KST), MCP healthy
상태, stable 포인터, 공유 OCR cache, Worker DB에 대한 쓰기·실험이 **0건**이다. 이번
작업의 저장소 변경은 `main`의 `f5b12d0`(순수 버전·계약 문자열)와 본 handoff 문서뿐이다.

## 차기 액션 (Planner/운영자 판단 필요)

1. 운영자-승인 gold 제작 경로 결정: `cardrag-gold-review draft/serve-gold/seal-gold`를
   사람 검토자와 수행할 일정·자료(카드사 8곳 source 충족), 또는 PLAN 단서 변경.
2. `v109_baseline` frozen legacy 입력 확보 가능 여부 — 불가하면 5-lane 계약 자체의
   재설계를 PLAN 후속 FIX로 결정할 것.
3. 확보 후: `f5b12d0`을 candidate source commit으로 Step 2(격리 후보 빌드·평가) →
   Step 3(56 evidence 생성·검증) → Step 4(봉인 commit+tag) → Step 5(dispatch) 재개.
   소스 변경 시 새 commit과 후보 검증부터 반복(PLAN 23행).
4. `dockerhub-public` secret 재등록·권한 실증이 필요하면 dispatch 전 확인(값 비공개).

## 잔여 위험

- CI green source commit은 이미 `main`에 존재하므로 릴리스 준비는 재사용 가능; 단 평가
   재수행 시점에 `main`이 전진하면 PLAN 23행에 따라 후보 검증 재반복 필요.
- 과거 handoff 문서의 수치 표기 혼동(5,512/5,060, 54/56, run ID)은 이 문서에서 정정
   기록했으나 원문을 덮어쓰지 않았다.
