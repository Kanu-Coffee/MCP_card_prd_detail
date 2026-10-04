# 005 — 공개 릴리스 증거 봉인과 발행

## 목적과 출발 상태

004의 **운영 인수는 완료**한다. 2026-10-04 09:00·11:47 KST 운영 Worker 실행은 서로 다른 run으로 성공했고, 최신 5,512문서 generation이 ready/활성이다. 두 실행의 신규 외부 OCR 호출은 0건이다. 운영 timer는 다음 2026-10-05 03:00 KST 실행을 기다리고, MCP는 healthy이며 `/` 여유 공간은 114 GiB이다. 이 과제는 이미 입증한 운영 cutover를 다시 수행하지 않고 **Docker Hub 이미지와 GitHub Release의 정식 공개 발행**만 다룬다. 공개 릴리스 전에도 운영 서비스를 정상 운영한다.

2026-10-04 기준 `main`은 `bfe7474032c474020773189abd90dad0f29a668a`이고 CI run `37190894306`이 성공했다. 최신 공개 GitHub Release는 `v1.0.29`다. `v1.0.30`과 `v1.0.31` annotated tag는 보존한다. `v1.0.31`은 `2224decbf9ce4e06b228cc0679cea4696c0d9fda`에 이미 게시되어 있으며, 해당 tag에서 dispatch한 release run `37190811161`은 validator 설치 단계에서 `uv 0.8.17`이 중복 `--package` 인자를 거부해 실패했다. 이 명령은 이후 `main`의 `9cce31c`에서 `--all-packages`로 수정됐지만 기존 tag 트리에는 반영되지 않는다. 현재 `release-evidence/v1.0.31/`은 없다. `dockerhub-public` 환경에는 `DOCKERHUB_USERNAME`과 `DOCKERHUB_TOKEN` secret 이름이 등록돼 있다. 저장소 수준 secret 목록이 비어 있다는 사실은 환경 secret 부재를 뜻하지 않는다. 실제 Docker Hub 게시 권한은 publish job 전까지 확인해야 한다. `v1.0.31`을 이동하거나 기존 릴리스처럼 표시하지 않는다.

004 `FIX_04_REPORT.md` 표의 `unchanged_documents=5512`는 의미가 섞인 표기다. 5,512는 매니페스트 문서 수 및 OCR 캐시 재사용 수이고, `FIX_02_REPORT.md`의 corpus gate `unchanged`는 5,060건이다. 그 표에서 이전 보고서의 첫 run ID를 `0928…`로 적은 것도 실제 `FIX_03_REPORT.md`의 `707da1b37f…`와 다르다. 같은 보고서가 portable evidence를 54개로 기술했지만 현재 release workflow의 목록은 **56개 경로**다. 새 결과를 쓸 때 원문·운영 DB·로그의 필드 의미와 workflow의 실제 목록을 구분하고, 과거 handoff 문서를 덮어쓰지 않는다. 이 문서상 오기는 운영 인수를 취소할 사유가 아니다.

003·004 작업 브랜치는 `main` 포함 여부와 열린 PR 부재를 확인한 뒤 로컬·원격에서 삭제했다. 공개 Release에 연결된 과거 tag와 기존 이미지 digest는 보존한다.

## 범위와 선행 입력

- 새 **미사용** 패치 버전은 `1.0.32`를 기본으로 한다. 작업 시작 전 원격 tag·Release·Docker Hub immutable tag 충돌을 다시 조회하고, 충돌하면 다음 미사용 버전을 선택한다.
- 공개 가능한 실제 gold/legacy baseline 입력, 승인된 답변 평가와 provider capture, 평가용 자원 및 Docker Hub 게시 권한이 선행 입력이다. 자료 부재를 성공 증거로 대체하지 않는다. 운영 중인 Worker DB나 WebDAV stable channel을 평가·후보 실험의 쓰기 대상으로 사용하지 않는다.
- `docs/RELEASING.md`, `docs/EVALUATION.md`, `.github/workflows/release.yml`, `tests/runtime_v1/test_candidate_acceptance_release_gate.py`, `tests/runtime_v1/test_candidate_oci_supply_chain.py`, `packages/cardrag-core/src/cardrag_core/candidate_acceptance.py`와 세 패키지의 `pyproject.toml` 및 `uv.lock`을 출발점으로 한다. 버전 고정 문자열은 `rg '1\.0\.31|v1\.0\.31'`로 전수 확인한다. `v1.0.31` 호환성을 의도한 분기는 근거를 기록하고 유지한다.
- 각 외부 입력과 secret의 보유자·가용 여부를 먼저 확인한다. 자료가 없거나 게시 권한이 없으면 현재 사실을 보고하고 해당 단계에서 멈춘다. 임의 합성 fixture, 과거 자료의 이름 변경, 수동 GitHub Release, tag 강제 이동으로 검증을 우회하지 않는다.

## 실행 계획

1. **사전 점검과 소스 고정.** 원격 tag/Release, `main` CI, 운영 timer·MCP·디스크를 짧게 재확인한다. 공개 gold와 baseline의 권한·해시·source identity 및 Docker Hub 저장소/secret 설정을 점검한다. `1.0.32` 패키지·lock·workflow·검증기·문서 계약을 일치시키고, `uv 0.8.17` validator 환경 설치를 로컬 또는 격리 CI에서 검증한다. 린트·타입·runtime·Compose·이미지·보안 CI가 성공한 **정확한 source commit**을 기록한다.
2. **격리 후보를 검증한다.** 그 source commit의 원격 Git context에서 Worker/MCP OCI 후보를 빌드하고 GHCR의 공개 권한, index/platform/config digest, provenance, SBOM 및 pin된 build 인자를 확인한다. 후보는 별도 state·channel에서 평가하며 stable 포인터, 공유 OCR cache, 운영 timer에 영향을 주지 않는다. 실제 MCP 12도구 호출, generation·원격 객체·기준선 복귀, OCR 재사용과 무단 쓰기 0건을 receipt로 남긴다. 소스 변경 시 새 commit과 후보 검증부터 반복한다.
3. **실제 평가 증거를 만든다.** `docs/EVALUATION.md`의 승인 gold, 다섯 retrieval lane, 문서 집계 profile, bootstrap/final capture, 답변 및 candidate acceptance를 동일 source·이미지·generation에 결속한다. `release.yml`의 `portable_evidence_relative_paths` 전체와 각 검증기가 요구하는 파일·해시·크기·schema를 충족한다. 공개 권한 없는 PDF/OCR 본문, 자격 증명, 사설 URL 또는 조사자 입력은 공개 번들에서 제외하고 필요한 평가는 공개 가능한 corpus로 다시 수행한다. 생성된 증거를 정식 검증기로 검증한다.
4. **증거만 봉인한다.** source commit 다음의 별도 commit에는 `release-evidence/v1.0.32/`의 공개 가능한 증거만 추가한다. 코드·workflow·lock·문서는 이 commit에서 바꾸지 않는다. workflow가 요구하는 source/봉인 commit 관계, 필수 file 목록, SHA-256 및 OCI digest를 독립적으로 검사한다. 두 commit의 CI와 변경 범위를 확인한 뒤 봉인 commit에 새 annotated tag `v1.0.32`를 **한 번만** 생성·게시한다.
5. **공식 workflow로 발행한다.** `dockerhub-public` 환경과 `DOCKERHUB_USERNAME`·`DOCKERHUB_TOKEN`, 공개 저장소 변수와 후보 GHCR package 권한을 확인한다. 인증정보는 GitHub secret에만 설정하며 handoff·로그·증거에는 쓰지 않는다. 봉인 tag의 `.github/workflows/release.yml`을 실제 receipt hash·candidate image digest로 dispatch한다. validate, 보안/출처, Docker Hub digest 복사·서명, GitHub Release 및 원격 asset checksum의 완료를 확인한다. 실패 시 단계·원인·수정 뒤 새 source/증거/tag 필요 여부를 보고하고 `published`로 표시하지 않는다.
6. **결과를 대조한다.** GitHub Release URL, tag commit, Docker Hub Worker/MCP digest, 후보 digest, asset checksum과 CI/workflow run ID를 한 표로 보고한다. 운영 이미지 교체는 이 과제의 자동 결과로 추정하지 않는다. 운영 전환이 별도 요청될 때 `docs/RELEASING.md`의 안정 전환 절차와 단일 rollback 근거 정책을 따른다.

## 수용 기준과 검증

1. 공개 Release와 Docker Hub Worker/MCP 이미지는 **같은 봉인 tag의 통과한 공식 workflow**가 발행했고, 이미지 digest·서명·출처·SBOM·asset checksum이 receipt와 일치한다. 과거 공개 tag는 이동·삭제되지 않았다.
2. 현재 workflow가 열거한 portable evidence **56개 경로 전체**가 존재하고, 현재 source·generation·이미지에 결속된 실제 평가/후보 검증기와 release gate를 통과한다. 다음 버전으로 workflow가 바뀌면 새 목록을 기준으로 다시 계수한다. 단순 파일 개수만으로 합격 처리하지 않는다.
3. GitHub Actions secret이 게시 전에 존재하고 권한이 검증되며, secret 값은 저장소와 공개 artifact에 남지 않는다. 운영 timer, MCP health, stable 포인터와 OCR 캐시는 후보·릴리스 작업의 영향을 받지 않는다.
4. `REPORT.md`는 수정 commit과 봉인 commit, tag, 후보 및 공개 digest, 명령/테스트/CI 결과, 증거의 출처와 파일 hash, 릴리스 URL, 미실행 항목과 잔여 위험을 명시한다. 선행 입력 부족으로 발행하지 못했다면 미완료로 명확히 보고한다.

## 관련 기록

- `.handoff/004_worker-cutover-integrity-closeout/{PLAN.md,REPORT.md,FIX_01.md,FIX_01_REPORT.md,FIX_02.md,FIX_02_REPORT.md,FIX_03.md,FIX_03_REPORT.md,FIX_04.md,FIX_04_REPORT.md}`와 `evidence/`
- 실패한 공식 release run: `https://github.com/Kanu-Coffee/MCP_card_prd_detail/actions/runs/37190811161`
- 성공한 현재 `main` CI: `https://github.com/Kanu-Coffee/MCP_card_prd_detail/actions/runs/37190894306`
- 공개 Release 목록: `https://github.com/Kanu-Coffee/MCP_card_prd_detail/releases`
