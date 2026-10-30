# 022 FIX_06 — 누적 변경 정리·v1.0.38 커밋/머지/릴리스 및 배포

작성일: 2026-10-31 KST  
역할: Reviewer / 릴리스 실행 지시  
상태: Executor 실행 대기

## 1. 목적과 승인 범위

사용자는 “현재까지 작업내용 정리하여 버전 올려서 배포”, “커밋 머지 릴리즈 하기 위한 새로운 fix”, “실제 실행은 executor”, “문서작업도 같이 정리”를 명시했다. 이 FIX는 그 승인에 따라 Executor에게 누적 변경 정리, 버전 갱신, 문서 정비, 로컬/CI 검증, GitHub PR·main 머지·annotated tag·정식 소스 릴리스, 필요한 운영 Worker 버전 정합화를 지시한다. 이 범위의 동일 승인 질문을 반복하지 않는다.

Reviewer는 이번 요청에서 조사와 이 FIX 작성만 수행했다. commit/branch 변경, push/PR/merge/tag/release, 운영 배포를 실행하지 않았다. Executor가 실제 작업을 맡는다. GitHub PR 작성·릴리스 발행은 명시 승인된 작업이며, 그 밖의 메일·메신저 통지는 포함하지 않는다.

목표 버전은 **1.0.38 / v1.0.38**이다. 착수 시 원격 tag/release를 확인해 이미 존재하면 덮어쓰지 않는다. 동일 내용이 이미 발행됐으면 그 증거를 확인하고 중복 발행하지 않으며, 다른 내용으로 해당 버전이 사용됐다면 다음 사용 가능한 patch 버전을 선택하고 근거·변경 번호를 보고한다. 이하 1.0.38은 그 충돌 확인을 전제로 한다.

## 2. 조사 기준과 작업 범위

- repository `Kanu-Coffee/MCP_card_prd_detail`, origin `https://github.com/Kanu-Coffee/MCP_card_prd_detail.git`.
- 현재 branch `main`, HEAD `06af791b6db3fa250b8b543deed46f77f3a4a7d7` (`docs: record v1.0.37 release and handoff closeout`). 로컬 최신 tag `v1.0.37`. Reviewer는 이번 작성 중 원격 refs/CI 상태를 갱신 조회하지 않았다. Executor가 원격을 다시 확인한다.
- core/worker/mcp `pyproject.toml`의 버전과 `uv.lock` workspace version은 1.0.37이다.
- 미커밋 수정: Worker `capacity_v5.py`, `cli.py`, `downloader.py`, `issuers/woori.py`, `ocr.py`, `paddleocr_runner.py`, `pipeline.py`, `providers.py`; 테스트 `test_017_scope.py`, `test_cli_settings_provider.py`, `test_issuers.py`, `test_paddle_inputs_v130.py`; timer와 운영 문서.
- 미추적: 020/021/022 handoff, `ocr_logging.py`, `paddle_cleanup.py`, `test_022_woori_raonk_session_auth.py`, `test_ocr_provider_logging.py`, `docs/WORKER_RUNTIME_OPTIONS.md`.
- 020: Paddle 임시 PDF alias의 상태 트리 오염 방지·명시적 복구·capacity 관련 개선. 정확한 최종 인수 범위는 020 REPORT/FIX/운영 증거에서 확인한다.
- 021: OCR provider 호출·문서·invocation 로그와 제한된 진단. 최종 보고서 및 실제 주/보조 provider 동작 증거를 확인한다.
- 022: 우리카드 RAONK 세션 헤더, cookie 보존·삭제·재취득, bounded retry 연결, redirect header 보호. `REVIEW_01.md`에서 코드 인수, `REVIEW_02.md`에서 운영 배포·resume·게시 범위를 인수했다.
- 마지막 운영 확인: 022 image `cardrag-worker:022-woori-auth-47ea2207b173`, ID `sha256:b0b765d9e1083fe5eb1649ee4d1918de6a87b54c656458285a68cc664f2064c0`, active release `/opt/cardrag/022-woori-session-auth-20261030`.
- 우리카드 run `df29d8e5dbb34048942a5296bb2d6ac8`은 10/31 00:19 KST에 정상 종료했다. Worker/MCP current generation `g-df29d8e5dbb34048942a5296-5a3a99224f92`, 비선택 7사 manifest 문서 변경 0, 해당 backup scope 완료. 범위 밖 pending 4건은 별도 대기다.
- 10/31 새벽 정리 후 보호 DB snapshot은 독립 022 한 개다. 019·021 이전 DB snapshot은 삭제됐고 code/image rollback은 보존했다. 실제 최신 복구 안내는 active 및 rollback release의 recovery-point/retention이며, 역사 deployment.json의 옛 snapshot 경로만 보고 복원점을 선택하지 않는다.
- timer는 매일 **10:00 Asia/Seoul**, Persistent=false, active/enabled 유지가 사용자 지시다. 실행 시 실제 상태를 다시 확인한다. 다음 예약의 성공은 아직 관측한 것으로 쓰지 않는다.

Executor는 위 파일을 일괄 `git add .` 하지 않고, 실제 Git diff와 각 인수 문서를 근거로 파일별 릴리스 포함 근거를 정리한다. 새로운 관련 변경은 보존·검토하고, 무관한 작업은 삭제하지 말고 별도로 남긴다. 이 FIX가 미검증 코드를 자동 승인하는 것은 아니다.

## 3. 버전과 문서 정리

### A. 버전 정합성

1. core/worker/mcp package version, lockfile의 workspace metadata, CLI/API 버전 표시, Docker APP_VERSION/OCI labels 등 실제 버전 제공 경로를 검색하고 1.0.38로 일치시킨다. 필요한 버전 테스트도 수정한다. 역사 릴리스·출처 고정값을 전역 치환하지 않는다.
2. 프로젝트의 기존 uv 경로로 lock을 갱신하고 `uv lock --check`를 확인한다. 버전 갱신에 무관한 대량 dependency upgrade가 생기면 원인을 제거하고 실제 필요 변경만 유지한다.
3. package 버전, CLI 표시 버전, image label이 서로 다르면 감추지 않는다. 문서상 1.0.38인 source와 실제 배포 image의 출처·버전 관계를 명시한다.

### B. 사용자 문서와 릴리스 노트

다음 현행 문서를 읽고 중복·불일치·오래된 현재형 설명을 정리한다.

- `README.md`, `docs/README.md`: 현재 버전·변경 사항·설치/운영/복구/로그 확인의 진입점 정리.
- `docs/RELEASE_NOTES_v1.0.38.md` 신규: 이번 누적 변경, 장애의 전후 동작, 주의할 계약과 검증 범위, 실제 소스/운영 릴리스 구분.
- `docs/OPERATIONS.md`, `docs/SIMPLE_RUNTIME.md`, `docs/WORKER_RUNTIME_OPTIONS.md`, `docs/RECOVERY.md`, `apps/cardrag-worker/README.md`, `deploy/README.md`: timer 10:00/미실행 slot 자동 catch-up 없음, 실제 runtime 옵션, worker 실패와 수동 resume 결과 구별, OCR 로그 읽기, RAONK 인증, explicit Paddle 복구, 단일 DB snapshot 정책·code rollback 분리 등을 서로 일치시킨다.
- `docs/RELEASING.md`: 1.0.38 발행 절차를 맨 앞에 추가하고 아래 v1.0.34 전용 공개 이미지 절차는 역사적·별도 범위임을 명확히 한다. v1.0.37 절의 14:30 예약 등은 당시 기록임을 표시하고 현재 예약 안내와 혼동되지 않게 한다.

필수 내용:

- 우리카드에서 HTTP200 HTML을 PDF로 통과시키는 것이 아니라 공식 landing session과 `RAONK_AUTH_PAGE_ID`/`RAONK_AUTH_KEY` 헤더를 같은 client에 연결해 복구했다는 점.
- cookie의 속성·전체 identity·서버 삭제·재취득·동시성과 예산 제한을 유지한다는 점. 인증 값·cookie·암호화 form은 로그/문서 예시에 넣지 않는다.
- run 성공과 모든 문서 지원은 다르다. 우리카드 수집 918 성공/4 skip, unsupported_attachment_count=0과 DRM 보호 문서 경고를 구분한다. 범위 밖 pending이나 구조 fallback을 “전체 오류 0”으로 과장하지 않는다.
- OCR 로그의 호출 횟수와 문서 수, resume invocation의 구분 및 재사용 시 provider 호출 0 가능성을 설명한다. 원문 OCR·자격증명·개인 데이터는 공개하지 않는다.
- 운영 DB가 실행 중일 때 외부 SQLite/복제 금지, lock 기반 정비, 원본 PDF/OCR/cache 보존, 검증 후 단일 독립 snapshot 유지. 과거 보고서의 옛 hash/크기는 REVIEW_02의 실제값과 구분한다.

현재 대형 handoff index `.handoff/README.md`와 `.handoff/STATUS_v1.0.37.md`는 역사 기록이다. 기존 PLAN/REPORT/FIX/REVIEW/종료 문서를 덮어써 재해석하지 않는다. 신규 `.handoff/STATUS_v1.0.38.md`에서 020~022의 최종 인수·운영·릴리스 근거와 남은 관측 범위를 연결하고 root README/docs index가 이를 가리키게 한다. 001~019가 다시 열린 것으로 표시하지 않는다. FIX_06의 외부 발행이 남아 있는 동안 022 전체 릴리스 종료를 미리 선언하지 않는다.

### C. 공개 범위 점검

소스·테스트·문서·handoff 기록 중 공개에 적합한 범위만 stage한다. `/opt/cardrag/.../operations`의 private compose/env/실계정 토큰·쿠키·원본 PDF/OCR·DB·전체 운영 로그·비공개 release evidence는 복사하지 않는다. 과거 handoff의 일반적인 run/hash/path 같은 재현 정보와 실제 secret을 구분한다. 실제 비밀이 발견되면 공개 stage에서 제외하고 기존 역사 파일을 임의 재작성하지 않은 채 구체 사유를 보고한다. 가짜 fixture key 때문에 보안 검사를 광범위 무력화하지 않는다.

## 4. 로컬·CI 검증

기준은 `.github/workflows/ci.yml`의 현재 실제 명령과 프로젝트 설정이다. 022의 174건 테스트는 부분 회귀 증거이지 누적 020/021 전체나 릴리스 CI 합격을 대체하지 않는다.

- `uv lock --check`, CI와 동일한 frozen workspace/extra 환경, 전체 Ruff check·format check, mypy, 전체 pytest(runtime/core/worker/mcp 포함)를 수행한다.
- 신규 022 세션 테스트, 020 Paddle/capacity/복구, 021 OCR 로깅, 버전 표시/packaging·설치 runtime 검증이 실행됐는지 확인한다.
- CI의 Compose·systemd·image smoke·보안 검사와 필수 status check를 통과시킨다. shell/Actions/Dockerfile을 수정했다면 기존 shellcheck/actionlint/관련 검사도 실행한다.
- 릴리스 대상 파일에 gitleaks 등 기존 보안 검사를 적용한다. 기존 보안·증빙 gate를 삭제하거나 skip해서 녹색으로 만들지 않는다. 새 취약점 등으로 실패하면 범위를 평가하고 필요한 최소 패치를 추가한 뒤 새 source로 다시 검증한다.
- Markdown 링크와 실제 파일·명령·옵션 이름·버전 검색을 확인한다. `git diff --check`를 완료한다.

실행하지 않은 검사는 합격으로 쓰지 않는다. 플랫폼/권한 등으로 로컬에서 불가능하면 해당 범위의 실제 CI 결과를 확인한다. formatter나 테스트 보완으로 동작이 변경되면 검증 근거를 갱신한다.

## 5. 커밋·PR·머지·태그·GitHub 릴리스

1. remote main, open PR, tag/release 충돌을 먼저 조회한다. 로컬 누적 변경을 보존한 상태에서 적절한 작업 branch로 옮기고, 원격 선행 commit이 있으면 정상적인 갱신·충돌 해결 후 검증한다. main 강제 push, hard reset, 타인 변경 덮어쓰기를 하지 않는다.
2. code/tests, version/lock, 사용자 문서와 공개 handoff 정리를 리뷰 가능한 commit으로 구성한다. stage 목록과 최종 diff를 확인하고 commit SHA를 기록한다. 빈 commit이나 증거와 무관한 정리를 섞지 않는다.
3. origin으로 push하고 main 대상 PR을 생성한다. PR 설명은 문제·최종 동작·릴리스 버전·검증·운영 적용 상태가 독립적으로 이해되게 작성한다. 실제 CI와 리뷰 정책을 확인하고 필요한 수정은 동일 PR에 반영한다.
4. 필수 CI/status가 정확한 최신 PR head에서 모두 성공하고 merge 가능하면 repository 관례에 맞춰 main에 머지한다. 보호 규칙을 우회하거나 실패 CI를 무시하지 않는다. 이 머지 작업은 사용자 승인 범위다.
5. merge 후 실제 main commit과 CI 성공을 확인한다. annotated `v1.0.38` tag를 검증된 최종 main commit에 만들고 push한다. tag 대상과 README/package/lock 버전이 일치해야 한다. 이미 공개된 tag를 이동·재작성하지 않는다.
6. `docs/RELEASE_NOTES_v1.0.38.md`를 바탕으로 GitHub 정식 소스 릴리스를 발행한다. prerelease/draft로 남기고 완료라 하지 않는다. target commit/tag·release URL·본문·필요한 자산을 API로 재조회해 확인한다. 권한/보호 조건이 막히면 구체 상태를 보고한다.
7. 기존 `.github/workflows/release.yml`은 **v1.0.34 annotated tag와 그 전용 증거에 결속된 공개 registry 이미지 workflow**다. 이번 새 source release에 무리하게 dispatch하거나 버전 gate를 제거하지 않는다. 1.0.38 신규 Docker Hub/GHCR 공개 image·서명 발행은 별도 공급망 계약이 필요한 범위이므로 이번 지시의 필수 산출물로 추가하지 않는다. GitHub source release와 로컬 운영 Worker 배포는 아래처럼 수행한다.

## 6. 운영 버전 정합화와 데이터 연속성

사용자의 버전 갱신·배포 지시를 충족하도록 최종 1.0.38 source와 운영 Worker의 관계를 확인하고 필요한 로컬 배포를 수행한다. 이미 022 동작이 운영 인수된 사실을 유지하며, 단순 label 변경을 최종 source에서 빌드한 image인 것처럼 주장하지 않는다.

1. 현재 운영 image/installed source/package metadata와 final tag source를 비교한다. 새 local Worker candidate를 고정된 최종 source에서 재현 가능하게 준비하고 source commit·version·input hash·image ID를 기록한다. 최소 overlay 방식이면 어떤 base와 최종 source 파일/패키지 metadata를 반영했는지 정확히 밝히고 그 최종 조합을 검증한다.
2. 020 Paddle/복구·021 OCR 로그·022 인증 계약과 기존 배포된 설정을 보존한다. 변경하지 않은 MCP를 소스 버전 갱신만으로 재시작하거나 전체 데이터 재처리하지 않는다. MCP 실행 버전이 이전이면 Worker/MCP/source 각각의 실제 상태를 보고한다.
3. FIX_05와 REVIEW_02의 검증된 운영 절차를 따른다. 현재 writer/backup·다음 예약을 재확인하고 기존 deployment/manual-start/worker/backup lock을 사용한다. **timer 유지**, 실제 기존 state/auth/model/serving volume 재사용, 포인터와 cursor 보존. 실행 중인 정상 worker를 중단하거나 lock을 삭제하지 않는다.
4. 현재 DB의 정비 구간 snapshot을 검증하고 단일 독립 복구본 정책을 지킨다. 코드 rollback과 DB restore는 분리한다. 새 snapshot 검증 후 이전 복구본과 참조 안내를 정리하며 7GB 복구본을 이유 없이 계속 누적하지 않는다. 다음 실행 peak와 build/snapshot 임시 공간을 포함해 용량을 재확인한다.
5. 실제 서비스 계정의 config 접근, image 내 package/CLI version, runtime contract와 startup-check, 기존 generation의 MCP 조회를 확인한 뒤 새 release로 원자 전환한다. 이전 022 image/release를 rollback으로 보존한다.
6. 우리카드 완료 run을 버전 검증 때문에 다시 resume하거나 8사 전체 배치를 돌리지 않는다. 기능 검증은 기존 인수 증거와 최종 candidate 회귀·필요한 제한된 샘플로 충족한다. 다음 예약은 그대로 이어가며 실행이 실제 발생한 경우에만 결과를 보고한다. activation 당시 기존 generation/data가 그대로인 것을 확인하고 다음 실행에서 비선택 데이터 보존이 계속되는지 관측한다.

## 7. 이력·최종 보고와 완료 조건

실행 결과는 신규 `FIX_06_REPORT.md` 및 필요한 신규 `RELEASE_REPORT_v1.0.38.md`로 남긴다. 기존 handoff 문서와 기존 release tag는 보존한다. 다음을 반드시 포함한다.

- 포함한 020/021/022 변경과 문서 정리 목록, 제외·보존한 파일 및 사유.
- 동기화한 버전 경로, lock 변경, 검사 명령·테스트 수·로컬/CI 결과와 실제 run URL.
- 작업 branch/commit, PR URL, merge commit, annotated tag object와 peeled commit, GitHub release URL 및 게시 상태.
- 공개 소스/공개 image/로컬 운영 image의 실제 범위와 출처. source-only인 부분을 공개 image 배포로 부풀리지 않는다.
- 운영 전후 release/image ID·실제 Worker/MCP version·startup/MCP generation·timer·보호 snapshot과 남은 공간. registry 공개 image를 발행하지 않았다면 명시한다.
- 이전 snapshot 정리와 data/volume/rotation 연속성, 운영 불일치나 권한·CI blocker가 있으면 그 구체 내용.

완료 기준은 문서와 누적 변경이 검증된 main에 병합되고 버전이 일관된 tag/정식 GitHub source release가 실제 게시됐으며, 운영 Worker 버전 정합화까지 수행·검증된 상태다. PR 생성·CI 시작·tag 생성만으로 릴리스 완료라 하지 않는다. 후속 이력 문서를 commit해야 하면 검증된 release tag를 이동하지 말고 문서 전용 후속 PR/commit으로 처리하고 그 SHA도 기록한다. 반복 승인 없이 승인된 작업을 끝까지 진행하되, 권한이나 repository 보호로 불가능한 경우 준비된 결과와 남은 정확한 조치만 보고한다.
