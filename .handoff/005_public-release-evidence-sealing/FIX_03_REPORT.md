# FIX_03 — Executor REPORT (task 005, 2026-10-06 KST)

## 결과

**v1.0.32 공개 발행 완료.** 공식 workflow(run `37416674686`)가 전 job success로 Docker
Hub 이미지 4 tag와 GitHub Release v1.0.32(자산 24개)를 발행했고, 원격에서 digest·
SHA256SUMS·cosign·manifest 결속을 재대조해 전부 일치했다. 운영 stable 전환은 하지
않았으며(이 발행은 운영 배포 승인이 아님), 운영 timer·MCP·WebDAV 포인터·OCR cache는
본 작업 전후 바이트 동일하다.

## 1. 중단 정리(FIX_03 §1)

- 09:33 KST 개시됐던 격리 후보 Worker 실행(PaddleOCR 직렬 추론으로 지연)은 사용자/
  Reviewer 지시로 종료 확인됨(컨테이너 잔존 없음). 중간 발행은 일어나지 않았다.
- 후보 채널 포인터는 기준선 그대로(`g-c622d3c4b1fb4df5a74a4b13-e95cb9ce7d7f`)이며
  stable 포인터도 동일(`g-03fbc4f18a3c450bb017e2fd-36bae25dd8cd`) — 재확인 완료.
- 본 세션 생성 후보 자원(코덥_HOME·MCP 상태 볼륨과 네트워크)은 삭제했고 **재생성하지
  않았다**. 운영 볼륨(`cardrag-worker-v130-candidate-state`, `cardrag-mcp-v129-candidate-state`,
  recovery-auth, paddle 모델)과 v1.0.29-era 볼륨은 무변경 보존.
- `/tmp/opencode/rr/{receipt.py,rollout.py,run_worker.sh}` 계약 불요 스크립트 삭제.
- 임시 하네스가 생산한 intermediate 산출물(native audit JSON, probe inspect 등)은
  `/tmp`에만 존재하며 공개 번들에 포함되지 않았다. 자격 증명·사설 URL·운영 로그 원문은
  그 어떤 push 대상 파일에도 들어가지 않았다(공개 파일은 qualification 1개뿐, grep 0건).

## 2. 계약 구현(final source commit)

| commit | 내용 | CI |
|---|---|---|
| `bcac9dffa57bf747af2febedd193f53421cf9189` | `cardrag_core.release_qualification`(v1 스키마·정규직렬화·중복키/NaN 거부·source/tag/digest/CI/저장소 결속, CLI) + release.yml 재설계: 13파일 readiness/receipt 경로 제거 → `release-evidence/v1.0.32/release-qualification.json` **단일 파일** 게이트(find -mindepth 1 카운트=1, symlink/추가파일 거부), dispatch 입력 `release_qualification_sha256`, validate에 **익명 OCI 신원 resolve 스텝**(고정 crane v0.22.0 + tag→digest 동일성 + jq 검증기 5종 + blob 해시 속박) 신설, publish 재검증·release 자산/SHA256SUMS까지 일관 교체, release notes 수행/참고/명시적 미수행 3단 구분, RELEASING/README 동기화. 연구·런타임 검증기(candidate_smoke/candidate_acceptance/release_readiness/EVALUATION)는 그대로 유지 | run `37414812870` success |

- 로컬 검증: ruff check/format(CI 경로), mypy 98 files, pytest **2,287 passed**(qualification
  동작 테스트 9 포함: 정합·변조·중복키·비정규·저장소/코밋/digest 불일치·비정규 파일 거부),
  actionlint 통과. workflow 6 job YAML 파싱 + 내장 python AST 전부 통과.

## 3. 봉인·tag·발행

| 항목 | 값 |
|---|---|
| 봉인 commit | `f15be2ae552cbc8d7b9124b9fb4db6f044b0e455` — diff는 `release-evidence/v1.0.32/release-qualification.json` 1파일뿐 |
| qualification SHA-256 | `8aace43e5760769b3c7dfa25238f557ac3ed29220f0c09dbd9c8f264569a47df`(873 bytes, canonical) |
| 봉인 commit CI | run `37416220543` success |
| tag | annotated `v1.0.32` → 봉인 commit, **생성 1회**, 미이동. `v1.0.30`(d6c3f93)/`v1.0.31`(2224dec) 무변경 확인 |
| 공식 workflow | run `37416674686` completed success(validate→fs/image strict scans→registry-preflight→publish×2→release) |

후보(final source 빌드, 원격 Git context + provenance v0.2 + pinned syft stable-1) 및 공개 digest:

| | Worker | MCP |
|---|---|---|
| 후보 GHCR index(`candidate-v1.0.32-<role>-<commit>` tag) | `sha256:d2904f8d9849dd0f2b7164aeb35b659600f4c7cf4cdd32ee50d22d86eb744073` | `sha256:3d5841a4a21e0978e2fcc28fc341ff9abeef1bf999ca2eb19e8d3ece66ab65fa1` |
| Docker Hub(`1.0.32-<role>` 및 `-sha-bcac9dffa57b` 4 tag) | 위와 동일 `d2904f8d…` | 위와 동일 `3d5841a4…` |
| Release | `https://github.com/Kanu-Coffee/MCP_card_prd_detail/releases/tag/v1.0.32` (HTTP 200, assets 24) | 동일 Release |

## 4. 원격 재대조 결과(본 세션 독립 검증)

- `crane digest ymtop59/mcp-card-prd-detail:1.0.32-{worker,mcp}{,-sha-bcac9dffa57b}` 4종 ==
  봉인 qualification의 후보 index digest 일치.
- `gh release download v1.0.32`: `sha256sum -c SHA256SUMS` **24/24 OK**,
  `release-qualification.json` 다운로드본 해시 == 8aace43e…(봉인본과 동일 bytes).
- `release-manifest.json`(v6): `candidate_source_commit=bcac9df…`, `git_sha=f15be2a…`(tag
  commit), worker/mcp digest 일치, `release_qualification.sha256` 결속 확인.
  `release-{worker,mcp}.json` part의 `release_qualification.document`에 명시적
  `not_performed` 4항목 실재 확인.
- cosign 검증 자산: `critical.identity.docker-reference ==
  index.docker.io/ymtop59/mcp-card-prd-detail@sha256:d2904f8d…`,
  `docker-manifest-digest ==` 공개 digest(worker/mcp 각 자산) — keyless 서명은 workflow의
  cosign verify 게이트가 통과해야 만 발행이 완료된 구조였고 success.
- 익명 이미지 라벨(`--network none` 부팅 확인 포함): revision=`bcac9df…`, version=1.0.32,
  entrypoint/user(10001:10001)/linux-amd64, `cardrag-mcp` 패키지 버전 1.0.32 출력.

## 5. 수행 검증 vs 미수행(FIX_03 §1 인수 기준으로 고정)

**수행**: 최종 소스/봉인 커밋 CI,qualification 단일 파일 canonical·SHA·결속
(validate+publish 2회 재검증), OCI index/platform/config/attestation·provenance·SBOM
jq 5종 + 태그 동일성, filesystem/image strict 스캔(secret·vuln, ignore-unfixed 없음),
Docker Hub immutable tag preflight/충돌 거부, 동일 digest 복사, cosign 서명 검증,
Release 자산 SHA256SUMS 원격 재검증, `--network none` 이미지 시작 확인.

**미수행(발행 게이트에서 철회, 파일·Release notes에 명시)**:
`candidate_worker_full_run`(격리 후보 Worker 일괄 실행), `candidate_mcp_12_tools`
(후보 환경 12도구 실호출·rollback 5단계·baseline 복귀), `gold_quality_evaluation`
(승인 gold·`v109_baseline`·5-lane 품질 비교), `production_cutover`(운영 stable 이미지
전환). 운영 03:00 run(`03fbc4f18a3c450bb017e2fd6f6442c4`, 이미지 source `1ba366d…`)는
**출처가 다른 참고 증거**로만 qualification에 기록했고 후보 실증으로 계상하지 않았다.

## 6. 운영 불변 확인(2026-10-06 14:1x KST)

`cardrag-worker.timer` active(다음 10-07 03:00 KST), `cardrag-stable-v1026-mcp-1` healthy,
stable/candidate WebDAV 포인터·OCR cache 기준값과 바이트 동일(읽기 probe), `/` 여유 95G.
본 FIX 실행 중 운영 자원 쓰기·재기동·GC는 0건이다.

## 7. 잔여 항목

- 운영 1.0.32 전환: 별도 작업(안 함). 전환 시 RELEASING 안정 절차 따름.
- 후속 전체 런타임/품질 검증이 필요하면 격리 후보 계약(유지된 검증기)으로 별도 과제 발행.
- GHCR 후보 패키지엔 이번 라운드 tag들이 남아 있음(공개 후보 namespace, Docker Hub와 무관).
